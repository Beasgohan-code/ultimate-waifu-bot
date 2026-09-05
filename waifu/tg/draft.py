"""Message drafts (Bot API 9.5+) — stream long answers instead of editing 5 times.

``sendMessageDraft`` renders progressively into the user's *input field* and never
posts to the chat: perfect for /history, /stats and /h-stats, the three places
Summon-bot spammed eight successive ``editMessageText`` calls and got rate-limited
for its trouble. ``can_stop`` shows Telegram's stop control and
``keep_on_stop`` decides whether the partial draft survives stopping — both are
exposed per call so each command can pick.

aiogram 3.31 ships ``SendMessageDraft``/``SendRichMessageDraft`` and the
``MessageGenerationStopped`` update; both are absent from python-telegram-bot,
which is one reason this project is on aiogram.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from aiogram import Bot, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.methods import SendMessageDraft
from aiogram.types import MessageGenerationStopped

from waifu.logging import get_logger

log = get_logger("tg.draft")

MAX_DRAFT_CHARS = 4096

#: ``{(chat_id, draft_id)}`` for drafts the user explicitly stopped.
_STOPPED: set[tuple[int, int]] = set()


class DraftUnsupported(RuntimeError):
    """The endpoint has no draft support — callers must fall back to a message."""


@dataclass(slots=True)
class DraftHandle:
    bot: Bot
    chat_id: int
    draft_id: int
    thread_id: int | None = None
    keep_on_stop: bool = False
    min_interval: float = 0.7
    _text: str = field(default="", repr=False)
    _last_sent: float = field(default=0.0, repr=False)

    @property
    def key(self) -> tuple[int, int]:
        return (self.chat_id, self.draft_id)

    @property
    def stopped(self) -> bool:
        """True once the user tapped stop — long renderers check this per chunk."""
        return self.key in _STOPPED

    async def push(self, text: str, *, force: bool = False) -> bool:
        """Replace the draft body. Throttled: every push re-renders the client."""
        if self.stopped:
            return False
        text = text[:MAX_DRAFT_CHARS]
        if text == self._text:
            return True
        now = time.monotonic()
        self._text = text
        if not force and now - self._last_sent < self.min_interval:
            return False
        self._last_sent = now
        try:
            await self.bot(
                SendMessageDraft(
                    chat_id=self.chat_id,
                    draft_id=self.draft_id,
                    message_thread_id=self.thread_id,
                    text=text,
                    parse_mode="HTML",
                    can_stop=True,
                    keep_on_stop=self.keep_on_stop or None,
                )
            )
        except TelegramAPIError as exc:
            if _is_unknown_method(exc):
                raise DraftUnsupported(str(exc)) from exc
            log.debug("draft push failed: %s", exc)
            return False
        return True

    async def finish(self, text: str | None = None) -> bool:
        """Push the final body (always sent) and forget the stop flag."""
        _STOPPED.discard(self.key)
        return await self.push(text if text is not None else self._text, force=True)


def _is_unknown_method(exc: Exception) -> bool:
    text = str(exc).lower()
    return "not found" in text or "unknown method" in text or "unsupported" in text


def new_draft_id(chat_id: int) -> int:
    """Per-chat draft id. Telegram treats it as an opaque int in chat scope."""
    return int(time.time() * 1000) % 2_000_000_000 + (abs(chat_id) % 997)


async def open_draft(
    bot: Bot, chat_id: int, *, thread_id: int | None = None, keep_on_stop: bool = True
) -> DraftHandle:
    return DraftHandle(
        bot=bot,
        chat_id=chat_id,
        draft_id=new_draft_id(chat_id),
        thread_id=thread_id,
        keep_on_stop=keep_on_stop,
    )


async def stream_lines(
    bot: Bot,
    chat_id: int,
    lines: Iterable[str] | AsyncIterator[str],
    *,
    header: str = "",
    chunk_lines: int = 6,
    thread_id: int | None = None,
    keep_on_stop: bool = True,
    on_overflow: Callable[[str], Any] | None = None,
) -> bool:
    """Grow a draft as lines arrive. Returns ``False`` if a fallback message is needed.

    ``on_overflow`` is called with the finished text when the draft was rejected, so
    the caller can send it as a normal message instead of losing the output.
    """
    handle = await open_draft(bot, chat_id, thread_id=thread_id, keep_on_stop=keep_on_stop)
    buffer: list[str] = [header] if header else []

    async def _iterate() -> AsyncIterator[str]:
        if hasattr(lines, "__aiter__"):
            async for item in lines:  # type: ignore[union-attr]
                yield item
        else:
            for item in lines:  # type: ignore[assignment]
                yield item

    body = ""
    try:
        async for line in _iterate():
            buffer.append(line)
            body = "\n".join(buffer)[:MAX_DRAFT_CHARS]
            if len(buffer) % chunk_lines == 0:
                await handle.push(body)
                if handle.stopped:
                    break
        await handle.finish(body or "\n".join(buffer))
    except DraftUnsupported:
        log.info("drafts unsupported in chat %s — using a plain message", chat_id)
        if on_overflow is not None and body:
            await on_overflow(body)
        return False
    except TelegramAPIError as exc:  # pragma: no cover - transient
        log.debug("draft stream aborted: %s", exc)
        return False
    return True


def install_stop_handler(
    router: Router, *, extra: Callable[[MessageGenerationStopped], Any] | None = None
) -> None:
    """Subscribe to ``message_generation_stopped`` so stopped drafts are abandoned.

    Skipping this means the renderer keeps pushing into a draft the user dismissed:
    wasted API budget, and a UI that reappears after being stopped.
    """

    async def _on_stop(event: MessageGenerationStopped) -> None:
        key = (event.chat.id, event.draft_id)
        _STOPPED.add(key)
        log.debug("user stopped draft %s", key)
        if extra is not None:
            result = extra(event)
            if asyncio.iscoroutine(result):
                await result

    router.message_generation_stopped.register(_on_stop)


def render_table(rows: Sequence[Sequence[object]], *, header: Sequence[str] | None = None) -> str:
    """Monospace table for drafts/HTML where a real table block isn't available."""
    grid = [list(map(str, r)) for r in rows]
    if header:
        grid.insert(0, list(header))
    if not grid:
        return ""
    widths = [
        max(len(row[i]) if i < len(row) else 0 for row in grid)
        for i in range(max(len(r) for r in grid))
    ]
    out = []
    for index, row in enumerate(grid):
        out.append(
            "  ".join(
                (row[i] if i < len(row) else "").ljust(widths[i]) for i in range(len(widths))
            ).rstrip()
        )
        if index == 0 and header:
            out.append("-" * min(sum(widths) + 2 * (len(widths) - 1), 48))
    return "\n".join(out)
