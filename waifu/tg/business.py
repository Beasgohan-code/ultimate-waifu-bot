"""Business messages (Bot API 7.2+) — the bot as a support inbox.

When a user DMs a *business account* that has the bot attached as a chatbot, the
message arrives as ``business_message`` and any answer must carry
``business_connection_id``. Supporting it costs ~40 lines and buys a real support
channel: the owner's personal Telegram becomes the helpdesk, the bot answers the
easy questions, and everything is mirrored into ``/audit``.

Also used by /feedback: the bot can post into the business chat with
``send_message(business_connection_id=…)`` instead of asking the user to DM the bot.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BusinessConnection, BusinessMessagesDeleted, Message

from waifu.logging import get_logger

log = get_logger("tg.business")


@dataclass(slots=True)
class BusinessTicket:
    """One inbound customer message, normalised for the support pipeline."""

    connection_id: str
    chat_id: int
    user_id: int
    message_id: int
    text: str
    business_name: str = ""
    is_enabled: bool = True

    @classmethod
    def from_message(cls, message: Message) -> BusinessTicket | None:
        connection = getattr(message, "sender_business_bot", None)
        if connection is None or message.chat is None:
            return None
        return cls(
            connection_id=connection.id,
            chat_id=message.chat.id,
            user_id=message.from_user.id if message.from_user else message.chat.id,
            message_id=message.message_id,
            text=(message.text or message.caption or "")[:4000],
            business_name=connection.name or connection.username or "",
            is_enabled=bool(connection.is_enabled),
        )


def connection_id(update: Any) -> str | None:
    connection: BusinessConnection | None = getattr(update, "business_connection", None)
    return getattr(connection, "id", None)


def deleted_info(update: Any) -> BusinessMessagesDeleted | None:
    return getattr(update, "deleted_business_messages", None)


async def reply(
    bot: Bot, ticket: BusinessTicket, text: str, *, typing: bool = False
) -> Message | None:
    """Answer inside the business chat. Silent when the connection was revoked."""
    if typing:
        await bot.send_chat_action(
            ticket.chat_id, "typing", business_connection_id=ticket.connection_id
        )
    try:
        return await bot.send_message(
            ticket.chat_id,
            text[:4000],
            parse_mode="HTML",
            business_connection_id=ticket.connection_id,
        )
    except TelegramAPIError as exc:
        log.info("business reply refused (%s): %s", ticket.connection_id[:8], exc)
        return None


async def mirror_to_support(bot: Bot, support_chat_id: int, ticket: BusinessTicket) -> int | None:
    """Copy the customer's message into the owner's support chat for review."""
    if not support_chat_id:
        return None
    header = f"<b>Support ({ticket.business_name or 'business'})</b> from {ticket.user_id}\n"
    try:
        sent = await bot.send_message(
            support_chat_id, header + ticket.text[:3800], disable_web_page_preview=True
        )
    except TelegramAPIError as exc:
        log.debug("support mirror failed: %s", exc)
        return None
    return sent.message_id


async def resolve_connection(bot: Bot, connection_id_value: str) -> BusinessConnection | None:
    """Re-validate a stored connection id before replying (they expire)."""
    from aiogram.methods import GetBusinessConnection

    try:
        return await bot(GetBusinessConnection(business_connection_id=connection_id_value))
    except TelegramAPIError as exc:
        log.debug("business connection %s… invalid: %s", connection_id_value[:8], exc)
        return None


async def forward_ticket(bot: Bot, ticket: BusinessTicket, *, to_chat_id: int) -> bool:
    """Forward the original message so media/quotes survive (API 6.x semantics)."""
    try:
        await bot.forward_message(
            chat_id=to_chat_id,
            from_chat_id=ticket.chat_id,
            message_id=ticket.message_id,
            business_connection_id=ticket.connection_id,
        )
    except TelegramAPIError as exc:
        log.debug("business forward failed: %s", exc)
        return False
    return True
