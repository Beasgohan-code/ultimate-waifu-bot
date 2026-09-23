"""The application context: one object holding every long-lived dependency.

aiogram injects ``workflow_data`` into every handler, so a handler signature is
``async def cmd(message: Message, ctx: AppContext, bot: Bot)`` — no globals, no
module-level singletons, and tests build an ``AppContext`` over a throwaway database
in three lines.

Services are attached by :func:`waifu.services.build` *after* construction (they keep a
back-reference to the context for cross-service calls), which is why every field is
optional here: the context is usable for routing before the services exist, and
``startup()`` verifies the wiring so a missing service is a clear error instead of an
``AttributeError`` three commands later.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from waifu.db.cache import Cache
from waifu.logging import get_logger
from waifu.settings import FeatureFlags, Settings
from waifu.tg.caps import Caps

log = get_logger("core.context")

if TYPE_CHECKING:  # pragma: no cover
    from aiogram import Bot

    from waifu.db import Database
    from waifu.db.redis_client import Redis
    from waifu.services.ai import AiService
    from waifu.services.auction import AuctionService
    from waifu.services.cards import CardService
    from waifu.services.collection import CollectionService
    from waifu.services.economy import EconomyService
    from waifu.services.gacha import GachaService
    from waifu.services.hstats import HStatsService
    from waifu.services.items import ItemService
    from waifu.services.moderation import ModerationService
    from waifu.services.premium import PremiumService
    from waifu.services.progress import ProgressService
    from waifu.services.spawn import SpawnService
    from waifu.services.stats import StatsService
    from waifu.services.trading import CodeService, GiftService, TradeService

#: Services every deployment needs (feature-flagged ones are checked separately).
REQUIRED_SERVICES = (
    "economy",
    "collection",
    "items",
    "progress",
    "gacha",
    "spawn",
    "stats",
    "moderation",
    "premium",
)


@dataclass(slots=True)
class LogChannelStats:
    """The log channel must report on itself.

    A misconfigured ``LOG_CHANNEL_ID`` (the bot is not an admin of the channel)
    used to fail *silently on every event*: gifts, payments, crashes — all
    "logged" into the void, and the owner only noticed weeks later. Every send
    is counted so ``/doctor`` can answer "is the channel alive?" and ``/logtest``
    can prove it with one command.
    """

    sent: int = 0
    failed: int = 0
    last_error: str = ""
    last_error_at: float = 0.0

    def record(self, ok: bool, error: str = "") -> None:
        if ok:
            self.sent += 1
            return
        self.failed += 1
        self.last_error = error[:200]
        self.last_error_at = time.time()


@dataclass(slots=True)
class AppContext:
    settings: Settings
    db: Database
    cache: Cache
    redis: Redis | None = None
    bot: Bot | None = None

    #: Capabilities negotiated with the API endpoint at startup (see core.bot).
    caps: Caps = field(default_factory=Caps)
    api_flags: dict[str, bool] = field(default_factory=dict)

    #: Send results for the owner's log channel since startup (see LogChannelStats).
    log_stats: LogChannelStats = field(default_factory=LogChannelStats)

    economy: EconomyService | None = None
    collection: CollectionService | None = None
    items: ItemService | None = None
    progress: ProgressService | None = None
    gacha: GachaService | None = None
    spawn: SpawnService | None = None
    auctions: AuctionService | None = None
    trades: TradeService | None = None
    gifts: GiftService | None = None
    codes: CodeService | None = None
    premium: PremiumService | None = None
    stats: StatsService | None = None
    hstats: HStatsService | None = None
    moderation: ModerationService | None = None
    cards: CardService | None = None
    ai: AiService | None = None

    started_at: float = field(default_factory=time.time)
    #: Anything a plugin wants to stash (job handles, the webhook secret, …).
    extra: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------ helpers
    @property
    def features(self) -> FeatureFlags:
        return self.settings.features

    def wants(self, name: str) -> bool:
        """Feature flag AND API support (e.g. ``ctx.wants("rich_messages")``)."""
        if not self.features.is_enabled(name):
            return False
        return self.caps.allow(name)

    def require(self, *names: str) -> None:
        """Fail fast on unwired services — called once during startup.

        A missing service is a programming error, not a runtime condition, so this
        raises :class:`RuntimeError` with the *list* of what is missing rather than
        letting the first handler hit ``AttributeError: 'NoneType'`` at 3am.
        """
        missing = [name for name in names if getattr(self, name, None) is None]
        if missing:
            raise RuntimeError(
                f"services not wired: {', '.join(missing)} (did build_services() run?)"
            )

    @property
    def uptime_seconds(self) -> int:
        return int(time.time() - self.started_at)

    @property
    def uptime_text(self) -> str:
        total = self.uptime_seconds
        days, rest = divmod(total, 86400)
        hours, rest = divmod(rest, 3600)
        minutes, seconds = divmod(rest, 60)
        if days:
            return f"{days}d {hours}h {minutes}m"
        if hours:
            return f"{hours}h {minutes}m {seconds}s"
        return f"{minutes}m {seconds}s"

    async def group_snapshot(self, chat_id: int) -> dict[str, Any] | None:
        """Per-group settings for gates + the spawn scheduler (cached 30 s)."""
        from waifu.db.repositories.spawns import group as get_group

        async with self.db.tx() as session:
            row = await get_group(session, chat_id)
            if row is None:
                return None
            data = row.data or {}
            return {
                "chat_id": row.chat_id,
                "title": row.title,
                "spawn_enabled": row.spawn_enabled,
                "spawn_limit": row.spawn_limit,
                "message_count": row.message_count,
                "log_channel_id": row.log_channel_id,
                "spam_limit": row.spam_limit,
                "auto_ban_spam": row.auto_ban_spam,
                "welcome_enabled": row.welcome_enabled,
                "disabled": list(data.get("disabled") or []),
            }

    async def react(
        self, chat_id: int, message_id: int, emoji: str = "🎉", *, big: bool = False
    ) -> bool:
        """``setMessageReaction`` on someone's message (Bot API 9.3).

        Kept here rather than in each plugin because the capability gate and the
        "bot has no rights in this chat" failure are the same everywhere: a raffle
        draw, a spawn winner, a milestone.
        """
        if not self.bot or not message_id or not self.caps.allow("reactions"):
            return False
        from aiogram.exceptions import TelegramAPIError

        from waifu.tg.interactions import emoji_reaction

        try:
            await self.bot.set_message_reaction(
                chat_id, message_id, reaction=[emoji_reaction(emoji)], is_big=big
            )
        except TelegramAPIError:  # pragma: no cover - depends on the chat's rights
            return False
        return True

    async def notify(
        self, text: str, *, silent: bool = False, html: bool = False, rich: Any | None = None
    ) -> bool:
        """Push a line to the owner's log channel (``LOG_CHANNEL_ID``), if configured.

        This used to delegate to ``ModerationService.notify`` — which is the
        *DM-a-player* method (first argument is a user id) — so every owner-log
        call raised ``TypeError`` and the channel stayed silent while gifts,
        auctions, joins and crashes went unrecorded. It now sends straight to
        the channel with :func:`waifu.tg.notify.safe_send`:

        * text is HTML-escaped unless ``html=True`` (a group name or a player
          note cannot markup-inject the owner feed);
        * ``rich`` is an optional :class:`InputRichMessage` (Bot API 9.5+)
          rendered natively when the server supports it — the plain ``text``
          stays as the fallback so old servers see the same line;
        * it never raises — a log line must not take down the flow that
          produced it; unconfigured channel / detached bot → ``False``.
        """
        return await self.notify_to(
            self.settings.log_channel_id, text, silent=silent, html=html, rich=rich
        )

    async def notify_to(
        self,
        chat_id: int,
        text: str,
        *,
        silent: bool = False,
        html: bool = False,
        rich: Any | None = None,
    ) -> bool:
        """The send itself — ``notify`` is this pointed at the global channel."""
        if not chat_id or self.bot is None:
            return False
        from waifu.tg.notify import safe_send
        from waifu.utils.text import esc

        result = await safe_send(
            self.bot,
            chat_id,
            text if html else esc(text),
            parse_mode="HTML",
            disable_notification=silent,
            rich=rich if self.caps.rich_messages else None,
        )
        self.log_stats.record(result.ok, result.error or f"outcome={result.outcome.value}")
        return result.ok

    async def group_notify(
        self,
        session: Any,
        chat_id: int,
        text: str,
        *,
        silent: bool = True,
        rich: Any | None = None,
    ) -> bool:
        """A second copy of a group-scoped event in *that group's own* log channel.

        ``Group.log_channel_id`` is settable from the group settings command but
        nothing ever read it — this is what it feeds: group admins who watch
        their own channel get joins, warnings and raffle draws there, while the
        global owner channel keeps receiving everything. Unconfigured group →
        no-op (the owner channel is the source of truth, never the group one).
        """
        from waifu.db.models import Group

        row = await session.get(Group, chat_id)
        if row is None or not row.log_channel_id:
            return False
        return await self.notify_to(row.log_channel_id, text, silent=silent, rich=rich)

    async def startup(self) -> None:
        self.require(*REQUIRED_SERVICES)
        await self._apply_runtime_overrides()
        if self.stats is not None:
            await self.stats.warm()

    async def _apply_runtime_overrides(self) -> None:
        """Database overrides that must survive a restart (``/setlogchannel``).

        The env value stays the default; a value written through the bot takes
        precedence until it is changed again. A failed read (locked database,
        fresh schema mid-migration) keeps the env value rather than breaking
        startup.
        """
        from waifu.db.repositories.stats import kv_get

        try:
            async with self.db.tx() as session:
                overrides = await kv_get(session, "runtime_overrides")
        except Exception as exc:  # pragma: no cover - startup must not die on a kv read
            log.warning("runtime overrides unreadable (%s) — env values stand", exc)
            return
        raw = str(overrides.get("log_channel_id") or "")
        if raw.lstrip("-").isdigit() and int(raw) != self.settings.log_channel_id:
            self.settings = self.settings.model_copy(update={"log_channel_id": int(raw)})
            log.info("log channel overridden from the database: %s", int(raw))

    async def shutdown(self) -> None:
        if self.ai is not None:
            await self.ai.close()
        if self.redis is not None:
            await self.redis.close()
        await self.cache.close()
        await self.db.dispose()
