"""Dispatcher construction + plugin registration.

Every plugin is an aiogram ``Router`` exposing a module-level ``router`` object
plus, optionally, extra routers for non-message updates. Registration order is
asserted here instead of being discovered in production, and an uninstalled
plugin is skipped with a warning rather than crashing the whole bot — the
failure mode that bit Summon-bot when one optional folder was missing.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from dataclasses import dataclass, field

from aiogram import Dispatcher, Router
from aiogram.fsm.storage.base import BaseStorage
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import DefaultKeyBuilder, RedisStorage
from aiogram.types import ErrorEvent

from waifu.core.context import AppContext
from waifu.logging import get_logger
from waifu.settings import Settings

log = get_logger("core.dp")

#: Fully-qualified plugin modules, in registration order.
PLUGIN_ROUTERS: tuple[str, ...] = (
    "waifu.plugins.misc",  # start / help / ping / settings / update
    "waifu.plugins.players",  # register / profile / bio / rename
    "waifu.plugins.characters",  # addchar / editchar / delchar / chars
    "waifu.plugins.uploads",  # upload / autoadd / uploads / roster — the ingestion pipeline
    "waifu.plugins.gacha",  # pull / hclaim / guarantee / pity / history
    "waifu.plugins.collection",  # collection / harem / stats / check / fav
    "waifu.plugins.economy",  # balance / daily / work / rob / give / pay
    "waifu.plugins.market",  # market / sell / buy / price / trends
    "waifu.plugins.shop",  # shop / bag / use / slot / slotclear
    "waifu.plugins.auctions",  # auction / bid / cancelauction
    "waifu.plugins.trades",  # trade / tgive / taccept / tcancel / escrow
    "waifu.plugins.codes",  # redeem / addcode / delcode
    "waifu.plugins.gifts",  # gift / sendgift / anonymousgift
    "waifu.plugins.premium",  # premium / boost / raffle / stars / pay
    "waifu.plugins.progress",  # streak / achievements / milestones
    "waifu.plugins.stats",  # top / lb / hstats-lite / server leaderboard
    "waifu.plugins.spawns",  # summon / spawn / autospan / changetime / hints
    "waifu.plugins.nguess",  # nguess / ngstats / ngtop / ngskip / ngmode / ngreset
    "waifu.plugins.moderation",  # warn / warnings / checkwarns / case / banlist
    "waifu.plugins.sudo",  # add/delete money, chance, givelb, broadcasts
    "waifu.plugins.ai",  # setai / ai / charai / chat / ask
    "waifu.plugins.hstats",  # h-stats / h-top / h-usage
    "waifu.plugins.webapp",  # mini-app data API + share
)

#: Extra routers keyed by the update type they subscribe to (all live in misc).
AUX_ROUTERS: dict[str, str] = {
    "member_bonus_router": "waifu.plugins.misc",
    "reaction_router": "waifu.plugins.misc",
    "poll_router": "waifu.plugins.misc",
    "business_router": "waifu.plugins.misc",
    "chat_member_router": "waifu.plugins.misc",
    "guest_router": "waifu.plugins.misc",
    # Last, like every aux router: ``AutoAddFeed`` matches any media message, and that is
    # only safe once the commands that also take media have had their turn.
    "autoadd_router": "waifu.plugins.uploads",
}


@dataclass(slots=True)
class RegistrationReport:
    loaded: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)

    def summary(self) -> str:
        if not self.skipped:
            return f"{len(self.loaded)} plugin routers registered"
        return f"{len(self.loaded)} registered, {len(self.skipped)} skipped ({', '.join(p for p, _ in self.skipped)})"


def build_storage(settings: Settings) -> BaseStorage:
    """FSM storage — Redis in production, memory only for tests.

    Memory storage loses a half-finished ``/addchar`` draft on every deploy and
    breaks with more than one process, which is why ``Settings.validate_runtime``
    treats Redis as mandatory unless ``WAIFU_TEST_MODE`` is on.
    """
    if settings.redis_dsn:
        return RedisStorage.from_url(
            settings.redis_dsn,
            key_builder=DefaultKeyBuilder(
                with_bot_id=True, with_destiny=True, global_prefix="waifu:fsm:"
            ),
        )
    log.warning("REDIS_URL not set — MemoryStorage only supports a single worker")
    return MemoryStorage()


def build_dispatcher(settings: Settings, ctx: AppContext) -> tuple[Dispatcher, RegistrationReport]:
    report = RegistrationReport()
    dp = Dispatcher(storage=build_storage(settings), ctx=ctx, settings=settings)
    # FSM strategy USER_IN_CHAT is the default; explicit because a wrong choice
    # here is the classic "two users in one group hijack each other's menus" bug.
    register_plugins(dp, ctx, report)
    dp.errors.register(on_error)
    return dp, report


def register_plugins(
    dp: Dispatcher, ctx: AppContext, report: RegistrationReport | None = None
) -> RegistrationReport:
    report = report or RegistrationReport()
    for path in PLUGIN_ROUTERS:
        router = _load(path, report)
        if router is None:
            continue
        dp.include_router(router)
        report.loaded.append(path)
    for attr, path in AUX_ROUTERS.items():
        module = _module(path, report)
        router = getattr(module, attr, None) if module else None
        if isinstance(router, Router):
            dp.include_router(router)
    return report


def _module(path: str, report: RegistrationReport):
    try:
        return importlib.import_module(path)
    except ModuleNotFoundError as exc:
        log.warning("plugin %s not installed: %s", path, exc)
        report.skipped.append((path, "not installed"))
        return None
    except Exception as exc:  # pragma: no cover - broken plugin
        log.exception("plugin %s failed to import", path)
        report.skipped.append((path, f"{type(exc).__name__}: {exc}"))
        return None


def _load(path: str, report: RegistrationReport) -> Router | None:
    module = _module(path, report)
    if module is None:
        return None
    router = getattr(module, "router", None)
    if not isinstance(router, Router):
        log.error("plugin %s has no `router` object", path)
        report.skipped.append((path, "no `router`"))
        return None
    if router.name is None:
        router.name = path.rsplit(".", 1)[-1]
    return router


async def on_error(event: ErrorEvent, ctx: AppContext, settings: Settings) -> None:
    """Last line of defence — replaces Summon-bot's 100-line mega-handler.

    Rules: never leak internals to a chat, always log with the update context,
    and never let an error handler itself raise.
    """
    from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError

    exc = event.exception
    handler = getattr(event.handler, "__qualname__", "?")
    chat = event.update.effective_chat if event.update else None
    user = event.update.from_user if event.update else None

    if isinstance(exc, TelegramForbiddenError):
        # Bot kicked from the group or DM closed: normal lifecycle, not a bug.
        log.info("%s: bot blocked/kicked in chat %s", handler, chat.id if chat else "?")
        if chat and not chat.is_private and ctx.moderation:
            await ctx.moderation.mark_unavailable(chat.id)
        return
    if isinstance(exc, TelegramAPIError):
        retry_after = getattr(exc, "retry_after", None)
        log.warning("%s: Telegram API error (retry_after=%s): %s", handler, retry_after, exc)
        return

    log.exception(
        "%s: unhandled error (user=%s chat=%s)",
        handler,
        getattr(user, "id", None),
        chat.id if chat else None,
    )
    if chat is None:
        return
    try:
        await ctx.bot.send_message(
            chat.id,
            "<b>That command hit a bug.</b> It has been logged — try again in a moment."
            + (f"\n<code>{handler}</code>" if settings.features.dev_console else ""),
        )
    except Exception:  # pragma: no cover - nothing left to do
        pass


__all__ = [
    "AUX_ROUTERS",
    "PLUGIN_ROUTERS",
    "RegistrationReport",
    "admin_command_menu",
    "build_dispatcher",
    "build_storage",
    "command_menu",
    "command_names",
    "is_staff_handler",
    "primary_commands",
    "public_commands",
    "register_plugins",
    "walk_routers",
]


def walk_routers(root: Router) -> Iterator[Router]:
    """``root`` and every router included below it, depth-first."""
    yield root
    for child in root.sub_routers:
        yield from walk_routers(child)


def command_names(handler: object) -> list[str]:
    """The ``/names`` a message handler listens for.

    aiogram keeps the :class:`~aiogram.filters.Command` instance on the filter object's
    ``callback``, so both places are checked — reading the wrong one is how "0 commands
    wired" looked like a broken bot rather than a broken introspector.
    """
    names: list[str] = []
    for filter_obj in getattr(handler, "filters", ()) or ():
        for target in (filter_obj, getattr(filter_obj, "callback", None)):
            for command in getattr(target, "commands", None) or []:
                text = str(getattr(command, "command", command)).lstrip("/").lower()
                if text and text not in names:
                    names.append(text)
    return names


def is_staff_handler(handler: object) -> bool:
    """True when the handler is gated on the staff filter (an admin-only command).

    Telegram caps every command-menu scope at 100 entries, so the private-chat menu shows
    what a player can type and the group-admin scope carries the moderation set on top.
    Reading the gate off the handler keeps that split honest instead of maintaining a
    second list that forgets to mention the next command added.
    """
    for filter_obj in getattr(handler, "filters", ()) or ():
        for target in (filter_obj, getattr(filter_obj, "callback", None)):
            if type(target).__name__ in {"Staff", "Owner"} or getattr(target, "__name__", "") in {
                "Staff",
                "Owner",
            }:
                return True
    return False


def _handler_doc(handler: object) -> str:
    r"""One-line description for a handler, taken from its own docstring.

    ``HandlerObject.__doc__`` is the *class* docstring, so the wrapped callback is asked
    first; the leading ``\`/cmd args\` —`` is dropped because the menu prints the name
    anyway, and Telegram truncates descriptions at 48 characters regardless.
    """
    callback = getattr(handler, "callback", None)
    raw = (getattr(callback, "__doc__", "") or "").strip()
    if not raw:
        return ""
    first = raw.splitlines()[0].strip().replace("``", "")
    if "—" in first:
        first = first.split("—", 1)[1].strip()
    elif first.startswith("/") and ":" in first:
        first = first.split(":", 1)[1].strip()
    return first


def _handlers() -> Iterator[tuple[str, object]]:
    """``(router name, handler)`` for every message handler in every shipped plugin."""
    for path in PLUGIN_ROUTERS:
        try:
            module = importlib.import_module(path)
        except Exception as exc:
            # The loader reports this at startup and the registry test fails on it; here a
            # broken plugin must simply contribute no commands.
            log.debug("skipping %s for the command index: %s", path, exc)
            continue
        root = getattr(module, "router", None)
        if not isinstance(root, Router):
            continue
        for router in walk_routers(root):
            for handler in router.message.handlers:
                yield str(getattr(router, "name", "") or "?"), handler


def primary_commands(*, staff_only: bool = False) -> dict[str, tuple[str, str]]:
    """Each handler's canonical command name → ``(router, one-line doc)``.

    Aliases are why the legacy bot's command list felt huge while its help file stayed
    small; anything that has to fit a limit (the ⊞ menu, the coverage test) works from
    canonical names.
    """
    out: dict[str, tuple[str, str]] = {}
    for owner, handler in _handlers():
        names = command_names(handler)
        if not names:
            continue
        if bool(staff_only) != is_staff_handler(handler):
            continue
        out.setdefault(names[0], (owner, _handler_doc(handler)[:110]))
    return out


def public_commands() -> dict[str, tuple[str, str]]:
    """Every command name the shipped plugins listen for, aliases included."""
    out: dict[str, tuple[str, str]] = {}
    for owner, handler in _handlers():
        doc = _handler_doc(handler)[:110]
        for name in command_names(handler):
            out.setdefault(name, (owner, doc))
    return out


def _menu_rows(*, staff_only: bool = False) -> list[tuple[str, str]]:
    """All primary commands, in plugin-registration order (player modules first)."""
    rows: list[tuple[str, str]] = []
    for name, (owner, doc) in primary_commands(staff_only=staff_only).items():
        clean = " ".join(doc.split()) or owner
        rows.append((name[:32], clean[:48]))
    return rows


def command_menu(limit: int = 100) -> list[tuple[str, str]]:
    """The ⊞ command menu for normal users, generated from the routers.

    Pushed at startup by :func:`waifu.core.app.run`; a hand-kept menu list is how the
    reference deployment ended up advertising commands it had removed months earlier.
    Registration order is the ranking, so if the list outgrows Telegram's 100-entry cap
    what gets dropped is the admin furniture, not ``/pull``.
    """
    return _menu_rows()[:limit]


def admin_command_menu(limit: int = 100) -> list[tuple[str, str]]:
    """The same menu for group admins: the overflow commands, then the usual list.

    Telegram replaces the whole menu per scope (it does not append), so the admin scope
    has to spend its 100 entries deliberately: moderation and roster commands come from
    the part that did not fit in the player menu, and the player commands fill the rest.
    """
    rows = _menu_rows()
    overflow = rows[limit:]
    return (overflow + rows[: limit - len(overflow)])[:limit]
