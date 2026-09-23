"""Typing/upload indicators and safe sending primitives.

``sendChatAction`` is free UX: a 400 ms card render feels instant with "uploading
photo…" and feels broken without it. It must be *paced* — one action per ~4 s — and
never sent after the reply, which is what this module encodes.

:func:`safe_send` is the wrapper every bulk operation (broadcasts, gift drops,
auction notices) must use: it converts "bot kicked / user blocked" into a typed
outcome the caller records, instead of an exception that aborts a 5,000-chat
broadcast halfway.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from aiogram import Bot
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import ErrorEvent, Message

from waifu.logging import get_logger

log = get_logger("tg.notify")

ACTION_TTL = 4.5


class SendOutcome(StrEnum):
    SENT = "sent"
    BLOCKED = "blocked"  # user revoked / bot kicked → stop trying this chat
    RETRY = "retry"  # flood wait → requeue with delay
    ERROR = "error"


@dataclass(slots=True)
class SendResult:
    outcome: SendOutcome
    message: Message | None = None
    retry_after: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome is SendOutcome.SENT


class TypingIndicator:
    """Keep a chat action alive while an awaitable runs.

    Usage::

        async with TypingIndicator(bot, chat_id, action="upload_photo"):
            image = await render(...)
            await bot.send_photo(...)
    """

    def __init__(
        self,
        bot: Bot,
        chat_id: int,
        *,
        action: str | ChatAction = "typing",
        thread_id: int | None = None,
        interval: float = ACTION_TTL,
    ) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.action = action.value if isinstance(action, ChatAction) else action
        self.thread_id = thread_id
        self.interval = interval
        self._task: asyncio.Task[None] | None = None

    async def _loop(self) -> None:
        with suppress(asyncio.CancelledError, TelegramAPIError):
            while True:
                await self.bot.send_chat_action(
                    self.chat_id, self.action, message_thread_id=self.thread_id
                )
                await asyncio.sleep(self.interval)

    def start(self) -> TypingIndicator:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())
        return self

    async def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        self._task = None

    async def __aenter__(self) -> TypingIndicator:
        return self.start()

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()


async def one_shot(
    bot: Bot, chat_id: int, action: str | ChatAction = "typing", *, thread_id: int | None = None
) -> None:
    """Single indicator, rate-limited per chat (used by short handlers)."""
    now = time.monotonic()
    if len(_LAST_ACTION) > 8192:  # bounded: a bot in 200k chats must not grow per chat
        _LAST_ACTION.clear()
    last = _LAST_ACTION.get(chat_id, 0.0)
    if now - last < ACTION_TTL:
        return
    _LAST_ACTION[chat_id] = now
    with suppress(TelegramAPIError):
        await bot.send_chat_action(
            chat_id,
            action.value if isinstance(action, ChatAction) else action,
            message_thread_id=thread_id,
        )


_LAST_ACTION: dict[int, float] = {}


async def safe_send(
    bot: Bot,
    chat_id: int,
    text: str,
    *,
    photo: str | None = None,
    reply_markup: Any = None,
    parse_mode: str | None = "HTML",
    thread_id: int | None = None,
    disable_notification: bool = False,
    protect_content: bool = False,
    retries: int = 1,
) -> SendResult:
    """Send with blocked/flood handling. Never raises for expected failures."""
    attempt = 0
    while True:
        attempt += 1
        try:
            if photo:
                message = await bot.send_photo(
                    chat_id,
                    photo=photo,
                    caption=text[:1024] or None,
                    parse_mode=parse_mode,
                    reply_markup=reply_markup,
                    disable_notification=disable_notification,
                    protect_content=protect_content,
                    message_thread_id=thread_id,
                )
            else:
                message = await bot.send_message(
                    chat_id,
                    text[:4000],
                    parse_mode=parse_mode,
                    reply_markup=reply_markup,
                    disable_notification=disable_notification,
                    protect_content=protect_content,
                    message_thread_id=thread_id,
                )
            return SendResult(SendOutcome.SENT, message=message)
        except TelegramForbiddenError as exc:
            # Blocked/kicked: the caller must *remove* this chat from rotation.
            return SendResult(SendOutcome.BLOCKED, error=str(exc)[:200])
        except TelegramRetryAfter as exc:
            if attempt > retries:
                return SendResult(SendOutcome.RETRY, retry_after=int(exc.retry_after))
            await asyncio.sleep(min(float(exc.retry_after), 30.0))
        except TelegramAPIError as exc:
            message_text = str(exc)
            if (
                "not enough rights" in message_text
                or "kicked" in message_text
                or "chat not found" in message_text.lower()
            ):
                return SendResult(SendOutcome.BLOCKED, error=message_text[:200])
            if attempt > retries:
                return SendResult(SendOutcome.ERROR, error=message_text[:200])
            await asyncio.sleep(0.5)


async def broadcast(
    bot: Bot,
    chat_ids: Iterable[int],
    text: str,
    *,
    photo: str | None = None,
    reply_markup: Any = None,
    concurrency: int = 8,
    on_result: Callable[[int, SendResult], Awaitable[None]] | None = None,
    throttle_per_second: float = 25.0,
) -> dict[str, int]:
    """Rate-limited fan-out for /broadcast — the thing that always gets bots banned.

    A broadcast is the single most dangerous command in a game bot. This one
    paces itself below Telegram's per-second ceiling, runs a bounded number of
    sends concurrently, classifies each failure, and reports counts so the owner
    sees "sent 412, blocked 9, retry 3" instead of a silent "done".
    """
    semaphore = asyncio.Semaphore(max(1, concurrency))
    delay = 1.0 / max(1.0, throttle_per_second)
    stats = {"sent": 0, "blocked": 0, "retry": 0, "error": 0}
    targets = list(chat_ids)

    async def _one(chat_id: int) -> None:
        async with semaphore:
            result = await safe_send(bot, chat_id, text, photo=photo, reply_markup=reply_markup)
            stats[result.outcome.value] = stats.get(result.outcome.value, 0) + 1
            if on_result is not None:
                await on_result(chat_id, result)

    tasks = []
    for chat_id in targets:
        tasks.append(asyncio.create_task(_one(chat_id)))
        await asyncio.sleep(delay)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    log.info("broadcast finished: %s of %d chats", stats, len(targets))
    return stats


async def answer_query_error(event: ErrorEvent) -> bool:  # pragma: no cover - handler helper
    """Ack a callback query even when its handler failed (stops the loading spinner)."""
    query = getattr(event.update, "callback_query", None)
    if query is None:
        return False
    with suppress(TelegramAPIError):
        await query.answer("Something went wrong — try again.", show_alert=False)
    return True
