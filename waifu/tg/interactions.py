"""Reactions, member tags, emoji status and verification — chat-level extras.

Each of these is a small API call that changes how the bot *looks* to a group,
which is why they share one module (all are "social" affordances rather than
message content):

``setMessageReaction``
    The bot reacts to the winning /summon message. Cheaper and quieter than a
    reply, and readable in a 4k-message scrollback.
``setChatMemberTag`` (10.1)
    Colours a member's name in the group by rank ("Top Collector", "Whale").
    The only way a bot can mark status without roles; requires ``can_manage_tags``.
``setUserEmojiStatus`` (9.4)
    A time-limited emoji status granted as a prestige reward (Celestial pull,
    100-day streak) — the flex mechanic Summon-bot's /glow faked with text.
``verifyChat`` / ``removeChatVerification``
    Official-verification style badge for partner communities (only works for bots
    Telegram has whitelisted; guarded, so the command degrades gracefully).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.methods import (
    RemoveChatVerification,
    SetChatMemberTag,
    SetMessageReaction,
    SetUserEmojiStatus,
    VerifyChat,
)
from aiogram.types import ChatFullInfo, Message, ReactionTypeCustomEmoji, ReactionTypeEmoji

from waifu.logging import get_logger

log = get_logger("tg.interactions")

#: Emoji the bot is allowed to react with (the public reaction set).
BOT_REACTIONS = {
    "thumbs": "👍",
    "fire": "🔥",
    "heart": "❤️",
    "party": "🎉",
    "wow": "😮",
    "triumph": "😤",
    "brain": "🧠",
    "boo": "👎",
    "cry": "😢",
    "ok": "👌",
}


def emoji_reaction(name: str) -> ReactionTypeEmoji:
    return ReactionTypeEmoji(emoji=BOT_REACTIONS.get(name, name))


def custom_reaction(emoji_id: str) -> ReactionTypeCustomEmoji:
    return ReactionTypeCustomEmoji(custom_emoji_id=emoji_id)


async def react(bot: Bot, message: Message, *reactions: str, big: bool = False) -> bool:
    """React to a message. ``reactions`` are names from :data:`BOT_REACTIONS` or raw emoji."""
    payload: list[Any] = [emoji_reaction(r) for r in reactions]
    if not payload:
        return False
    try:
        await bot(
            SetMessageReaction(
                chat_id=message.chat.id,
                message_id=message.message_id,
                reaction=payload,
                is_big=big or None,
            )
        )
    except TelegramAPIError as exc:
        log.debug("reaction failed in %s: %s", message.chat.id, exc)
        return False
    return True


async def clear_reactions(bot: Bot, message: Message) -> bool:
    try:
        await bot(
            SetMessageReaction(chat_id=message.chat.id, message_id=message.message_id, reaction=[])
        )
    except TelegramAPIError:
        return False
    return True


async def set_member_tag(bot: Bot, chat_id: int, user_id: int, tag: str) -> bool:
    """Colour a member's name in the group chat (Bot API 10.1).

    Needs ``can_manage_tags`` on the bot's admin rights; the caller (a scheduled
    job, not the /daily handler) is expected to skip silently when it is absent.
    """
    try:
        await bot(SetChatMemberTag(chat_id=chat_id, user_id=user_id, tag=tag[:64]))
    except TelegramAPIError as exc:
        log.debug("setChatMemberTag refused (%s): %s", chat_id, exc)
        return False
    return True


async def can_manage_tags(bot: Bot, chat_id: int) -> bool:
    info: ChatFullInfo | None = await bot.get_chat(chat_id)  # type: ignore[assignment]
    return bool(getattr(info, "can_manage_tags", False))


async def grant_emoji_status(
    bot: Bot, user_id: int, custom_emoji_id: str, *, days: int = 7
) -> bool:
    """Give a user a temporary emoji status as a prestige reward."""
    if not custom_emoji_id:
        return False
    from waifu.utils.time import now_utc

    expires = now_utc() + timedelta(days=max(1, days))
    try:
        await bot(
            SetUserEmojiStatus(
                user_id=user_id,
                emoji_status_custom_emoji_id=custom_emoji_id,
                emoji_status_expiration_date=expires,
            )
        )
    except TelegramAPIError as exc:
        log.info("setUserEmojiStatus refused for %s: %s", user_id, exc)
        return False
    return True


async def verify_community(bot: Bot, chat_id: int, description: str) -> bool:
    """Official verification badge for a partner community (usually unavailable)."""
    try:
        await bot(VerifyChat(chat_id=chat_id, custom_description=description[:160] or None))
    except TelegramAPIError as exc:
        log.info("verifyChat refused (%s): %s", chat_id, exc)
        return False
    return True


async def unverify_community(bot: Bot, chat_id: int, description: str = "") -> bool:
    try:
        await bot(
            RemoveChatVerification(chat_id=chat_id, custom_description=description[:160] or None)
        )
    except TelegramAPIError:
        return False
    return True
