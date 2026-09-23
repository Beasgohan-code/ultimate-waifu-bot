"""Guest mode (Bot API 10.0) — let people play from a chat the bot isn't in.

A "guest" is a user who added the bot to a group where it has no rights; their
messages arrive as ``message.guest_query_id`` and must be answered with
``answerGuestQuery`` — a *query answer*, not a chat message, so it can't spam the
group and can't be sent at all unless Telegram asked for it.

Why it matters for this bot: /summon, /balance and /pull become usable in the
thousands of groups where nobody will grant the bot admin rights. That is free
growth and Summon-bot structurally could not do it (it polled
``allowed_updates=[message, edited_message, callback_query, inline_query]``).
"""

from __future__ import annotations

from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.methods import AnswerGuestQuery
from aiogram.types import InlineQueryResultArticle, InputTextMessageContent, Message

from waifu.logging import get_logger

log = get_logger("tg.guest")

#: Commands guests may run — everything else is politely refused.
GUEST_COMMANDS = frozenset(
    {
        "start",
        "help",
        "pull",
        "balance",
        "checkspawn",
        "summon",
        "daily",
        "search",
        "profile",
        "top",
    }
)


def is_guest_message(message: Message) -> bool:
    return bool(getattr(message, "guest_query_id", None))


def caller(message: Message) -> tuple[int | None, int | None]:
    """``(user_id, chat_id)`` of whoever triggered the guest call."""
    user = getattr(message, "guest_bot_caller_user", None)
    chat = getattr(message, "guest_bot_caller_chat", None)
    return (getattr(user, "id", None), getattr(chat, "id", None))


async def answer(
    bot: Bot,
    message: Message,
    text: str,
    *,
    title: str = "Waifu bot",
    markup: Any = None,
    cache_time: int = 0,
    show_alert: bool = False,
) -> bool:
    """Reply to a guest query with an article (rich text not allowed here)."""
    query_id = getattr(message, "guest_query_id", None)
    if not query_id:
        return False
    result = InlineQueryResultArticle(
        id=query_id[:64] or "guest",
        title=title[:64],
        description=text[:64],
        input_message_content=InputTextMessageContent(message_text=text[:4000], parse_mode="HTML"),
        reply_markup=markup,
    )
    try:
        await bot(AnswerGuestQuery(guest_query_id=query_id, result=result))
    except TelegramAPIError as exc:
        log.debug("guest answer failed: %s", exc)
        return False
    return True


async def refuse(
    bot: Bot,
    message: Message,
    reason: str = "That command needs the bot to be a member of this chat.",
) -> bool:
    return await answer(bot, message, reason, title="Not available")
