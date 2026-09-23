"""Middleware stack (outermost → innermost).

Order is deliberate and load-bearing:

1. :class:`ContextMiddleware` — DB session + the :class:`Access` object.
2. :class:`ThrottleMiddleware` — cheap Redis-only guard *before* any DB work.
3. :class:`ReplayGuardMiddleware` — one-shot callback_data, so a double-tapped
   "confirm bid" cannot bid twice (Summon-bot's most reported bug).
4. :class:`FeatureGateMiddleware` — hard-disable /nguess, /ai etc. per guild.
5. :class:`LoggingMiddleware` — records the command for /stats + /hstats.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, Update

from waifu.core.access import Access, resolve
from waifu.core.context import AppContext
from waifu.errors import RateLimited
from waifu.logging import get_logger
from waifu.settings import Settings
from waifu.utils.time import now_utc

log = get_logger("core.middleware")

Next = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]


class ContextMiddleware(BaseMiddleware):
    """Opens a transaction-per-update and resolves who the caller is.

    A single session per update keeps handler code free of ``async with`` noise
    while still guaranteeing commit/rollback around every handler — including the
    ones that raise.
    """

    def __init__(self, ctx: AppContext, settings: Settings) -> None:
        self.ctx = ctx
        self.settings = settings
        self.redis = ctx.redis

    async def _ensure_player(self, session, event: TelegramObject, access: Access):
        """Create/refresh the player row, but only when it actually changed.

        An INSERT … ON CONFLICT per update is 60k pointless writes a day on a
        mid-size fleet, so the "have we seen this user this hour" flag lives in
        Redis and the DB write happens on first contact or after a profile edit.
        """
        tg_user = getattr(event, "from_user", None)
        if tg_user is None or self.redis is None:
            return access.user
        fresh = await self.redis.claim_once(("seen", tg_user.id), 3600, value="1")
        if fresh and access.user is None:
            # First contact from this account — the owner's log channel is where
            # "a new player walked in" belongs (id + name + @username, escaped).
            label = tg_user.first_name or tg_user.username or "unknown"
            handle = f" (@{tg_user.username})" if tg_user.username else ""
            from waifu.tg.rich import rich_log

            await self.ctx.notify(
                f"🆕 new player: {label}{handle} · id {tg_user.id}",
                silent=True,
                rich=rich_log("🆕 new player", f"{label}{handle}", detail=f"id {tg_user.id}"),
            )
            # A reaction on the newcomer's own message is the welcome: it shows
            # in their history without adding a fifth bot message to the group.
            if self.ctx.caps.allow("reactions") and isinstance(event, Message):
                from waifu.tg.interactions import react

                await react(self.ctx.bot, event, "heart")
        if not fresh and access.user is not None:
            return access.user
        if (
            access.user is not None
            and access.user.username == (tg_user.username or access.user.username)
            and not fresh
        ):
            return access.user
        from waifu.core.access import ensure_user

        return await ensure_user(
            session, tg_user, settings=self.settings, locale=tg_user.language_code
        )

    async def _count_group_message(self, session, chat_id: int, user) -> None:
        """Feed the auto-spawn counter (``groups.message_count``).

        Only messages in *registered* groups count, and the row is updated with a
        single atomic UPDATE … RETURNING, so 200 members talking at once cannot
        lose counts or double-fire a spawn.
        """
        from waifu.db.repositories.spawns import bump_message_count

        _count, limit, triggered = await bump_message_count(
            session, chat_id, spawn_limit_default=self.settings.spawn_default_limit
        )
        user.last_seen_at = now_utc()
        if triggered and self.ctx.spawn is not None:
            # The service decides *which* character spawns and arms the card send;
            # the middleware only reports "this chat just hit its message quota".
            await self.ctx.spawn.on_activity_threshold(session, chat_id=chat_id, limit=limit)

    async def __call__(self, handler: Next, event: TelegramObject, data: dict[str, Any]) -> Any:
        update: Update | None = data.get("update")
        chat, user_id, chat_type = _locate(event, update)
        data["is_group_message"] = isinstance(event, Message) and chat_type in (
            "group",
            "supergroup",
        )
        data["ctx"] = self.ctx
        data["settings"] = self.settings
        if user_id is None:
            return await handler(event, data)

        async with self.ctx.db.tx() as session:
            data["session"] = session
            access = await resolve(
                session=session,
                redis=self.ctx.redis,
                bot=self.ctx.bot,
                settings=self.settings,
                user_id=user_id,
                chat_id=chat,
                chat_type=chat_type,
            )
            data["access"] = access
            user = await self._ensure_player(session, event, access)
            data["user"] = user
            if user is not None and data.get("is_group_message"):
                await self._count_group_message(session, chat, user)
            if access.global_banned:
                # A globally banned account is ignored, not argued with: no error
                # message (that would teach them the ban is user-specific).
                log.info(
                    "ignored update from banned user %s (%s)", user_id, "; ".join(access.reasons)
                )
                return None
            try:
                return await handler(event, data)
            finally:
                data.pop("session", None)


class ThrottleMiddleware(BaseMiddleware):
    """Per-user + per-chat token buckets in Redis.

    Summon-bot relied on Telegram's own flood control and therefore produced
    bursts of 429 retries during auto-spawn storms. Sliding windows in Redis
    cost ~0.2 ms and stop the storm before it reaches the API.
    """

    def __init__(self, ctx: AppContext, settings: Settings) -> None:
        self.redis = ctx.redis
        self.settings = settings

    async def __call__(self, handler: Next, event: TelegramObject, data: dict[str, Any]) -> Any:
        if self.redis is None or self.settings.mode == "test":
            return await handler(event, data)
        user_id = data.get("access").user_id if isinstance(data.get("access"), Access) else None
        if user_id is None:
            return await handler(event, data)
        allowed, remaining, retry = await self.redis.sliding_hit(
            f"rate:user:{user_id}",
            self.settings.rate_limit_per_user,
            self.settings.rate_limit_window,
        )
        if not allowed:
            raise RateLimited(retry_after=retry)
        chat, _, chat_type = _locate(event, data.get("update"))
        if chat and chat_type in ("group", "supergroup"):
            allowed, _, retry = await self.redis.sliding_hit(
                f"rate:chat:{chat}",
                self.settings.rate_limit_per_chat,
                self.settings.rate_limit_window,
            )
            if not allowed:
                raise RateLimited(retry_after=retry)
        data["rate_remaining"] = remaining
        return await handler(event, data)


class ReplayGuardMiddleware(BaseMiddleware):
    """Idempotency for button presses.

    ``cb:{callback_query_id}`` is claimed with Redis ``SET NX`` (see
    :meth:`Redis.claim_once`), so a client that resends the same callback after a
    network hiccup is answered with an ack and dropped — no second payout, ever.
    """

    TTL = 120

    def __init__(self, ctx: AppContext) -> None:
        self.redis = ctx.redis

    async def __call__(self, handler: Next, event: TelegramObject, data: dict[str, Any]) -> Any:
        if not isinstance(event, CallbackQuery) or self.redis is None:
            return await handler(event, data)
        fresh = await self.redis.claim_once(("cb", event.id), self.TTL, value="1")
        if not fresh:
            await event.answer("Already handled ✅", show_alert=False)
            return None
        try:
            return await handler(event, data)
        finally:
            # Release on failure so the user can legitimately retry a broken tap.
            if data.get("_handler_failed"):
                await self.redis.delete("cb", event.id)


class FeatureGateMiddleware(BaseMiddleware):
    """Runtime kill-switches, including per-group overrides.

    Group owners can disable noisy subsystems (auto-spawn, nguess, AI chat) in
    *their* chat without affecting the rest of the fleet — the missing feature in
    Summon-bot that caused "please stop spawning in my server" complaints.
    """

    async def __call__(self, handler: Next, event: TelegramObject, data: dict[str, Any]) -> Any:
        ctx: AppContext | None = data.get("ctx")
        chat, _, chat_type = _locate(event, data.get("update"))
        if ctx is not None and chat_type in ("group", "supergroup") and chat:
            group = await ctx.cache.get_or_set(
                "groups", (chat,), lambda: ctx.group_snapshot(chat), ttl=30
            )
            if group and group.get("disabled"):
                data["disabled_features"] = frozenset(group["disabled"])
                command = _command_of(event)
                if command and command in data["disabled_features"]:
                    return None
        return await handler(event, data)


class LoggingMiddleware(BaseMiddleware):
    """Structured command log + the counters behind /stats and /h-stats.

    Command frequency is incremented in Redis (cheap) and periodically flushed
    into ``stats_snapshots`` by the stats job — writing a row per command would
    have been the next table Summon-bot bloated.
    """

    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    async def __call__(self, handler: Next, event: TelegramObject, data: dict[str, Any]) -> Any:
        command = _command_of(event) or type(event).__name__
        started = time.monotonic()
        try:
            return await handler(event, data)
        except Exception:
            data["_handler_failed"] = True
            raise
        finally:
            took_ms = int((time.monotonic() - started) * 1000)
            if self.ctx.redis is not None:
                await self.ctx.redis.incr(f"cmd:{command}", ttl=86_400)
            if took_ms > 1500:
                log.warning("slow handler %s took %d ms", command, took_ms)


def _locate(
    event: TelegramObject, update: Update | None
) -> tuple[int | None, int | None, str | None]:
    """Return ``(chat_id, user_id, chat_type)`` for any supported update.

    Never raises on unknown update shapes: aiogram 3.31 has no
    ``Update.effective_chat`` / ``effective_user``, and the fallback has to
    duck-type the typed payload (``subscription.user``,
    ``purchased_paid_media.from_user`` …) — see :mod:`waifu.utils.chats`.
    """
    from waifu.utils.chats import effective_chat, effective_user

    if isinstance(event, CallbackQuery):
        chat = event.message.chat if event.message and event.message.chat else None
        return (
            (chat.id if chat else None),
            (event.from_user.id if event.from_user else None),
            (chat.type if chat else None),
        )
    if isinstance(event, Message):
        chat = event.chat
        return (
            chat.id if chat else None,
            event.from_user.id if event.from_user else None,
            chat.type if chat else None,
        )
    chat = effective_chat(update if update is not None else event)
    user = effective_user(update if update is not None else event)
    return (
        getattr(chat, "id", None),
        getattr(user, "id", None),
        getattr(chat, "type", None),
    )


def _command_of(event: TelegramObject) -> str | None:
    if isinstance(event, Message) and event.text and event.text.startswith("/"):
        return event.text.split(maxsplit=1)[0][1:].split("@", 1)[0].lower()
    if isinstance(event, CallbackQuery) and event.data:
        return event.data.split(":", 1)[0]
    return None


__all__ = [
    "ContextMiddleware",
    "FeatureGateMiddleware",
    "LoggingMiddleware",
    "ReplayGuardMiddleware",
    "ThrottleMiddleware",
    "install_middlewares",
]


def install_middlewares(dp, ctx: AppContext, settings: Settings) -> None:
    """Apply the stack in the documented order (outermost first)."""
    dp.update.outer_middleware(ContextMiddleware(ctx, settings))
    dp.update.outer_middleware(ThrottleMiddleware(ctx, settings))
    dp.update.outer_middleware(ReplayGuardMiddleware(ctx))
    dp.update.outer_middleware(FeatureGateMiddleware())
    dp.update.outer_middleware(LoggingMiddleware(ctx))
