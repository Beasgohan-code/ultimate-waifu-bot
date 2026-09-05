"""Shared plumbing for the command layer.

Every plugin module in this package follows the same three rules, which is why they
can stay short:

1. **No database code.** A handler receives ``session`` from
   :class:`~waifu.core.middlewares.SessionMiddleware` (one transaction per update,
   committed if the handler returns, rolled back if it raises) and calls a service.
   The rule that makes this safe is that services never commit, so a handler that
   fails halfway cannot leave a payout behind.
2. **No formatting code duplicated per command.** Cards go through :func:`card`,
   which decides rich-vs-HTML from the negotiated :class:`~waifu.tg.caps.Caps`, so a
   deployment on an old API server gets readable text instead of a 400.
3. **No silent failures.** Player-triggerable problems are
   :class:`~waifu.errors.WaifuError` subclasses (the error handler renders
   ``user_message``); anything else is a bug and reaches the log channel.

Callback data is a compact, self-describing string — ``"col:page:3:rarity"`` —
instead of aiogram's ``CallbackData`` builder per router: 22 routers, one parser, and
the payloads stay short enough for Telegram's 64-byte limit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from aiogram import Bot, F, Router
from aiogram.filters import BaseFilter, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message, TelegramObject

from waifu.enums import ChatMode
from waifu.errors import PermissionDenied, WaifuError
from waifu.tg.buttons import callback, grid, markup, pager
from waifu.tg.messages import SendResult, edit_card, send_card
from waifu.tg.rich import RichButton, RichMessageBuilder
from waifu.utils.misc import bar as _bar

if TYPE_CHECKING:  # pragma: no cover
    from waifu.core.access import Access
    from waifu.core.context import AppContext

CB_LIMIT = 64
_USER_TOKEN = re.compile(r"^(?:@?(?P<username>[A-Za-z][A-Za-z0-9_]{3,31})|(?P<id>-?\d{1,20}))$")


# --------------------------------------------------------------------- rendering
def mode_of(ctx: AppContext) -> ChatMode:
    """Rich messages when the server supports them, HTML otherwise."""
    return ChatMode.RICH if ctx.wants("rich_messages") else ChatMode.HTML


async def card(
    event: Message | CallbackQuery,
    ctx: AppContext,
    *,
    builder: RichMessageBuilder | None = None,
    html: str = "",
    photo: str | None = None,
    buttons: list[list[InlineKeyboardButton]] | None = None,
    rich_buttons: list[RichButton] | None = None,
    protect: bool = False,
    silent: bool = False,
) -> SendResult | None:
    """Reply with a card, choosing rich or HTML per deployment.

    ``builder`` is preferred (it carries headings/tables/photos); ``html`` is its
    fallback text, and callers pass both — that pairing is the whole point of
    :meth:`RichMessageBuilder.fallback_html` and the reason a caption-only fallback
    never has to be re-derived in each plugin.
    """
    message = event.message if isinstance(event, CallbackQuery) else event
    if message is None:  # a callback with an inaccessible message: answer only
        await note(event, html or "…", alert=True)
        return None
    body = builder or RichMessageBuilder()
    if builder is None and html:
        body.paragraph(html=html)
    return await send_card(
        _bot(ctx),
        message.chat.id,
        builder=body,
        caption=html or None,
        photo=photo,
        buttons=buttons,
        rich_buttons=rich_buttons,
        mode=mode_of(ctx),
        disable_notification=silent,
        reply_to=message.message_id,
        protect_content=protect,
        message_thread_id=message.message_thread_id,
    )


async def edit(
    event: CallbackQuery,
    ctx: AppContext,
    *,
    builder: RichMessageBuilder,
    html: str = "",
    buttons: list[list[InlineKeyboardButton]] | None = None,
    photo: str | None = None,
) -> None:
    """Update an open menu in place (pages, tabs, results)."""
    if event.message is None:
        return
    await edit_card(
        _bot(ctx),
        event.message.chat.id,
        event.message.message_id,
        builder=builder,
        caption=html or None,
        markup=markup(buttons or []),
        mode=mode_of(ctx),
        photo=photo,
    )


async def text(
    event: Message | CallbackQuery,
    ctx: AppContext,
    html: str,
    *,
    buttons: list[list[InlineKeyboardButton]] | None = None,
    silent: bool = False,
    protect: bool = False,
) -> SendResult | None:
    """Plain(ish) reply — for the one-liners that do not deserve a card."""
    return await card(event, ctx, html=html, buttons=buttons, silent=silent, protect=protect)


async def note(
    event: Message | CallbackQuery, msg: str, *, alert: bool = False, url: str | None = None
) -> None:
    """``answerCallbackQuery`` — the toast that closes a button press.

    Toasts are how a group sees *nothing* while the presser sees the result, which is
    why half of this bot's confirmations are notes instead of messages.
    """
    if isinstance(event, CallbackQuery):
        await event.answer(msg[:200] if msg else None, show_alert=alert, url=url)
    elif msg:
        await event.answer(msg[:4096])


async def refuse(event: Message | CallbackQuery, msg: str) -> None:
    """A denial the player can act on, in the chat it belongs to."""
    if isinstance(event, CallbackQuery):
        await event.answer(msg[:200], show_alert=True)
    else:
        await event.answer(msg[:4096])


def _bot(ctx: AppContext) -> Bot:
    if ctx.bot is None:  # pragma: no cover - only in unit tests
        raise WaifuError("the bot is not attached to this context")
    return ctx.bot


# --------------------------------------------------------------------- arguments
@dataclass(slots=True, frozen=True)
class Args:
    """A command's argument list, parsed once.

    ``/sell 12x3`` and ``/sell 12 3`` both mean "three copies of #12", which is how
    players actually type; ``count`` folds that in so no plugin re-parses it.
    """

    raw: str = ""
    words: tuple[str, ...] = ()

    @classmethod
    def of(cls, command: CommandObject | None) -> Args:
        raw = (command.args or "").strip() if command else ""
        return cls(raw=raw, words=tuple(raw.split()) if raw else ())

    @property
    def first(self) -> str:
        return self.words[0] if self.words else ""

    @property
    def rest(self) -> str:
        return " ".join(self.words[1:]) if len(self.words) > 1 else ""

    @property
    def integer(self) -> int | None:
        match = re.match(r"^(\d+)", self.first)
        return int(match.group(1)) if match else None

    @property
    def count(self) -> int:
        match = re.search(r"[x*×](\d{1,3})$", self.raw, re.IGNORECASE) or re.match(
            r"^\d+\s+(\d{1,3})$", self.raw
        )
        return max(1, min(99, int(match.group(1)))) if match else 1

    @property
    def amount(self) -> int | None:
        """A coin amount with k/m suffixes (``500k``, ``2m``)."""
        match = re.match(r"^(\d+(?:[.,]\d+)?)\s*([km]?)$", self.first, re.IGNORECASE)
        if not match:
            return int(self.first) if self.first.isdigit() else None
        value = float(match.group(1).replace(",", "."))
        return int(
            value
            * (
                1_000
                if match.group(2).lower() == "k"
                else 1_000_000
                if match.group(2).lower() == "m"
                else 1
            )
        )

    def paged(self, default: int = 1) -> int:
        """``/collection 3`` → page 3, clamped to >= 1."""
        for word in self.words:
            if word.isdigit():
                return max(1, min(9999, int(word)))
        return default


async def resolve_user(session: Any, event: Message | CallbackQuery, raw: str = "") -> int | None:
    """Reply-target → @username → user id → display name, in that order.

    ``users_repo.resolve`` owns the matching (case-insensitive username, exact id, and
    a unique display-name hit); this function only decides *which* of the three inputs
    the player meant. The reference bot accepted ``@username`` and nothing else, so
    "/rob some one with a space in their name" simply failed.
    """
    from waifu.db.repositories import users as user_repo

    reply_to = event.reply_to_message if isinstance(event, Message) else None
    if reply_to is not None and reply_to.from_user is not None and not (raw or "").strip():
        return int(reply_to.from_user.id)
    if isinstance(event, Message) and event.forward_from is not None and not (raw or "").strip():
        return int(event.forward_from.id)
    token = (raw or "").strip()
    if not token:
        return None
    found = await user_repo.resolve(session, token, reply_to_id=None)
    return int(found.id) if found is not None else None


async def resolve_many(session: Any, event: Message | CallbackQuery, raw: str) -> list[int]:
    out: list[int] = []
    for token in (raw or "").replace(",", " ").split():
        user_id = await resolve_user(session, event, token)
        if user_id and user_id not in out:
            out.append(user_id)
    return out


# ------------------------------------------------------------------- callbacks
def cb(*parts: object) -> str:
    """Build callback data, keeping it under Telegram's 64-byte cap."""
    return ":".join(str(part) for part in parts if str(part))[:CB_LIMIT]


def split_cb(data: str | None) -> tuple[str, ...]:
    return tuple((data or "").split(":"))


async def page_of(callback_query: CallbackQuery, pages: int, direction: str, current: int) -> int:
    """Translate a pager button into a clamped page index."""
    if direction == "next":
        return min(pages - 1, current + 1)
    if direction == "prev":
        return max(0, current - 1)
    if direction == "first":
        return 0
    if direction == "last":
        return max(0, pages - 1)
    return max(0, min(pages - 1, current))


def pager_row(*, page: int, pages: int, prefix: str, extra: str = "") -> list[InlineKeyboardButton]:
    return pager(page=page + 1, pages=max(1, pages), prefix=prefix, extra=extra)


def tabs(
    active: str, options: list[tuple[str, str]], *, prefix: str
) -> list[list[InlineKeyboardButton]]:
    """One row of mutually exclusive tabs, the active one disabled (a no-op tap)."""
    row = [
        callback(label, cb(prefix, "tab", key), disabled=key == active) for key, label in options
    ]
    return [row]


# ----------------------------------------------------------------------- guards
class Registered(BaseFilter):
    """Reject updates from chats where the bot must stay quiet (``/mute``, no user)."""

    async def __call__(
        self, event: TelegramObject, ctx: AppContext, access: Access | None = None
    ) -> bool:
        del ctx
        return access is not None and not access.global_banned


class Staff(BaseFilter):
    """``/sudo``-grade gate: reads the resolved role, never a hard-coded id list."""

    async def __call__(self, event: TelegramObject, access: Access | None = None) -> bool:
        return bool(access and access.is_staff)


def require(access: Access | None, permission: str | None = None) -> Access:
    """``access.require`` as an expression, so a handler stays two lines."""
    if access is None:
        raise PermissionDenied("your access could not be resolved — /start first")
    access.require(permission)
    return access


def staff_of(access: Access | None) -> Access:
    require(access)
    if access is None or not access.is_staff:
        raise PermissionDenied("that command is for bot staff.")
    assert access is not None
    return access


# ------------------------------------------------------------------ formatting
def money(amount: int | float) -> str:
    return f"{int(amount):,}"


def pct(value: float, *, digits: int = 2) -> str:
    """``0.0625`` → ``"6.25%"`` — one place, so tables line up everywhere."""
    return f"{value * 100:.{digits}f}%"


def bar(value: float, total: float, *, width: int = 10, filled: str = "▰", empty: str = "▱") -> str:
    """A progress bar (``▰▰▰▱▱``) — used by quests, pity and collection %."""
    ratio = 0.0 if not total else float(value) / float(total)
    return _bar(ratio, width=width, filled=filled, empty=empty)


def tier_badge(rarity_id: int) -> str:
    from waifu.enums import Rarity

    try:
        rarity = Rarity.from_value(int(rarity_id))
    except ValueError:  # pragma: no cover - data from an older catalogue
        return "❔"
    return rarity.badge


def mention(user_id: int, name: str = "") -> str:
    label = (name or f"user {user_id}").replace("<", "").replace(">", "")
    return f'<a href="tg://user?id={user_id}">{label}</a>'


def shorten(value: str, limit: int = 34) -> str:
    value = (value or "").strip()
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


__all__ = [
    "CB_LIMIT",
    "Args",
    "F",
    "Registered",
    "RichMessageBuilder",
    "Router",
    "Staff",
    "bar",
    "callback",
    "card",
    "cb",
    "edit",
    "grid",
    "markup",
    "mention",
    "mode_of",
    "money",
    "note",
    "page_of",
    "pager_row",
    "pct",
    "refuse",
    "require",
    "resolve_many",
    "resolve_user",
    "send_card",
    "shorten",
    "split_cb",
    "staff_of",
    "tabs",
    "text",
    "tier_badge",
]
