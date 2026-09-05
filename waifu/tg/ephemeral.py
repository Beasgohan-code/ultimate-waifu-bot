"""Ephemeral messages (Bot API 10.2) — private replies inside a public chat.

The single biggest UX problem of a group game bot: "you were outbid by 200 coins"
prints in front of 4,000 people. Ephemeral messages are visible only to one member
of the chat and are removed from history when they expire.

Two ways to use them:

* :func:`ephemeral_note` — a *reply target* id, so a normal message is threaded
  onto something only the addressee can see (``ReplyParameters.ephemeral_message_id``);
* :func:`send_ephemeral` — a rich/HTML message with ``ephemeral_message_parameters``
  set, i.e. addressed to ``receiver_user_id`` inside ``chat_id``.

``callback_query_id`` + ``replace_callback_query_message`` let a button's own
public "✖️" answer be *replaced* by the private version, which is exactly the
trade/auction confirmation flow.
"""

from __future__ import annotations

import contextlib
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.methods import SendMessage, SendPhoto, SendRichMessage
from aiogram.types import EphemeralMessageParameters, InputRichMessage, Message

from waifu.logging import get_logger

#: in-flight scheduled deletions — an unreferenced task can be garbage collected
#: before it runs (RUF006), which would leave the ephemeral message visible forever.
_pending: set = set()

log = get_logger("tg.ephemeral")


async def send_ephemeral(
    bot: Bot,
    chat_id: int,
    *,
    receiver_user_id: int,
    text: str,
    photo: str | None = None,
    rich_message: InputRichMessage | None = None,
    callback_query_id: str | None = None,
    replace: bool = True,
    thread_id: int | None = None,
    ttl_seconds: int | None = None,
) -> Message | None:
    """Send a card/message only ``receiver_user_id`` can see in ``chat_id``.

    Returns ``None`` when the server doesn't support it — callers then send the
    same content as a private DM, which is the fallback Summon-bot never had.
    """
    kwargs: dict[str, Any] = {
        "chat_id": chat_id,
        "message_thread_id": thread_id,
        "ephemeral_message_parameters": EphemeralMessageParameters(
            receiver_user_id=receiver_user_id,
            callback_query_id=callback_query_id,
            replace_callback_query_message=replace if callback_query_id else None,
        ),
    }
    try:
        if rich_message is not None:
            return await bot(SendRichMessage(rich_message=rich_message, **kwargs))
        if photo:
            return await bot(
                SendPhoto(photo=photo, caption=text[:1024] or None, parse_mode="HTML", **kwargs)
            )
        return await bot(SendMessage(text=text[:4096], parse_mode="HTML", **kwargs))
    except TelegramAPIError as exc:
        message = str(exc).lower()
        if "ephemeral" in message or "not found" in message or "unsupported" in message:
            log.debug("ephemeral messages unsupported here (%s)", exc)
            return None
        raise


async def ephemeral_note(bot: Bot, chat_id: int, *, receiver_user_id: int, text: str) -> int | None:
    """Cheap variant: an ephemeral text line whose id other messages can quote."""
    sent = await send_ephemeral(bot, chat_id, receiver_user_id=receiver_user_id, text=text)
    return sent.message_id if sent else None


async def reply_ephemerally(
    bot: Bot,
    message: Message,
    *,
    receiver_user_id: int,
    text: str,
    ephemeral_id: int | None = None,
) -> Message | None:
    """Quote an ephemeral message so the thread stays private (10.2 reply param)."""
    from aiogram.types import ReplyParameters

    try:
        return await bot(
            SendMessage(
                chat_id=message.chat.id,
                text=text[:4096],
                parse_mode="HTML",
                reply_parameters=ReplyParameters(
                    chat_id=message.chat.id,
                    message_id=ephemeral_id or message.message_id,
                    ephemeral_message_id=ephemeral_id or message.message_id,
                ),
                ephemeral_parameters=EphemeralMessageParameters(receiver_user_id=receiver_user_id),
            )
        )
    except (TelegramAPIError, TypeError) as exc:  # pragma: no cover - parameter naming drift
        log.debug("ephemeral reply unsupported: %s", exc)
        return None


async def safe_delete(bot: Bot, chat_id: int, message_id: int, *, delay: int = 0) -> None:
    """Fire-and-forget cleanup with a delay (used after every ephemeral notice)."""
    import asyncio

    async def _go() -> None:
        if delay:
            await asyncio.sleep(delay)
        with contextlib.suppress(TelegramAPIError):
            await bot.delete_message(chat_id, message_id)

    # Held in a module set on purpose: an unreferenced task can be garbage collected
    # mid-flight (RUF006), which would leave the ephemeral message undeleted forever.
    task = asyncio.create_task(_go())
    _pending.add(task)
    task.add_done_callback(_pending.discard)
