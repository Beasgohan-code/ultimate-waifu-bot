"""Chat-type predicates.

aiogram 3.7 removed the ``Chat.is_private`` / ``is_group`` helpers (the fields moved to
``ChatFullInfo``, and ``Message.chat`` is still typed ``Chat``), so ``message.chat.is_private``
— the most natural thing in the world to write — raises ``AttributeError`` at runtime. Every
handler in this bot that has to say "group only" hit that, and no unit test noticed, because the
fixtures built chats that happened to carry the attribute.

``Chat.type`` is the field the API actually returns, so this module is the one place allowed to
interpret it. Prefer it over ``getattr(chat, "is_private", False)``, which would silently read
``False`` forever.
"""

from __future__ import annotations

from typing import Any

PRIVATE = "private"
GROUP = "group"
SUPERGROUP = "supergroup"
CHANNEL = "channel"


def _type_of(chat: Any) -> str:
    return str(getattr(chat, "type", "") or "").lower()


def is_private(chat: Any) -> bool:
    """True for a DM (and for ``None``, which only ever arrives from a detached callback)."""
    return chat is None or _type_of(chat) == PRIVATE


def is_group(chat: Any) -> bool:
    """Any non-private chat: the legacy group, a supergroup or a channel."""
    return not is_private(chat)


def is_channel(chat: Any) -> bool:
    return _type_of(chat) == CHANNEL


def forum_thread(chat: Any) -> int | None:
    """The topic id a message belongs to, if the chat is a forum.

    Forum topics are how a big waifu group keeps spawns and market noise apart, and the id is
    what :meth:`Bot.send_message` needs to answer in the right thread.
    """
    thread_id = getattr(chat, "message_thread_id", None)
    return int(thread_id) if thread_id else None
