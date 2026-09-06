"""Permission resolution — one place that decides who you are.

Summon-bot scattered ``str(from_user.id) in ADMIN_IDS`` through ~40 handlers and
had no notion of "the admin of *this* group", so a sudo admin was either a god or
nothing. Here every privileged handler receives an :class:`Access` object built
from three sources — config owners/admins, ``sudo_admins`` rows with granular
flags, and live Telegram chat-admin status — and every check is
``access.can("spawn")``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from aiogram.enums import ChatMemberStatus
from aiogram.types import CallbackQuery, ChatMemberUpdated, Message

from waifu.db.models import User
from waifu.db.repositories import moderation as mod_repo
from waifu.db.repositories import users as user_repo
from waifu.enums import Role
from waifu.errors import PermissionDenied
from waifu.settings import Settings

if TYPE_CHECKING:  # pragma: no cover
    from aiogram import Bot

    from waifu.db.redis_client import Redis

#: Telegram's anonymous-admin channel; needed for ``/unban`` of channel admins.
ANONYMOUS_ADMIN_ID = 1087968824

#: Roles that may broadcast to every registered group.
BROADCAST_ROLES = (Role.OWNER, Role.ADMIN)

ADMIN_CACHE_TTL = 60


@dataclass(slots=True)
class Access:
    """Resolved identity + capabilities for the caller of the current update."""

    user_id: int
    role: Role = Role.GUEST
    permissions: dict[str, bool] = field(default_factory=dict)
    is_group_admin: bool = False
    global_banned: bool = False
    reasons: list[str] = field(default_factory=list)
    user: User | None = None

    def can(self, permission: str | None = None) -> bool:
        if permission is None:
            return not self.global_banned
        if self.global_banned:
            return False
        if self.role is Role.OWNER:
            return True
        if self.role is Role.ADMIN:
            # Admins hold every fine-grained permission but not owner-only ones.
            return permission not in OWNER_ONLY
        return bool(self.permissions.get(permission))

    def require(self, permission: str | None = None) -> None:
        """Raise a user-facing ``PermissionDenied`` (the handler stays a one-liner)."""
        if not self.can(permission):
            raise PermissionDenied(_denial_text(permission))

    @property
    def is_staff(self) -> bool:
        return (
            self.role in (Role.OWNER, Role.ADMIN)
            or self.role is Role.MODERATOR
            or bool(self.permissions)
        )

    @property
    def label(self) -> str:
        return "Bot Owner" if self.role is Role.OWNER else self.role.value.title()

    def as_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "role": str(self.role),
            "perms": self.permissions,
            "gadmin": self.is_group_admin,
        }


#: Permissions that even /admins do not get — owner-only, by design.
#: ``edit_roster`` is here because the character catalogue is the product: the reference
#: bot allowed only its owner to ``/upload`` characters, and a fresh install starts empty,
#: so whoever can write it decides what every player pulls. A sudo admin can still be
#: granted it explicitly (``/editsudo``), which is the point of a flag rather than a check.
OWNER_ONLY = frozenset({"broadcast_all", "set_odds_master", "wipe", "edit_owner", "edit_roster"})


def _denial_text(permission: str | None) -> str:
    if permission is None:
        return "You are not allowed to do that."
    return f"That command needs the <b>{permission.replace('_', ' ')}</b> permission."


async def ensure_user(session, tg_user, *, settings: Settings, locale: str | None = None):
    """Materialise the player row on first contact (also used by the FSM-less path)."""
    return await user_repo.upsert(
        session,
        tg_user.id,
        username=tg_user.username,
        first_name=tg_user.first_name or "",
        last_name=tg_user.last_name or "",
        locale=locale or (tg_user.language_code or None),
        settings=settings,
    )


async def resolve(
    *,
    session,
    settings: Settings,
    user_id: int,
    bot: Bot | None = None,
    redis: Redis | None = None,
    chat_id: int | None = None,
    chat_type: str | None = None,
    member: ChatMemberUpdated | None = None,
    db_user: User | None = None,
) -> Access:
    """Build the :class:`Access` for ``user_id`` in ``chat_id``.

    Telegram's ``getChatMember`` is cached in Redis for :data:`ADMIN_CACHE_TTL`
    seconds: calling it on every button press triples p99 latency and burns the
    global rate limit during spawn storms.
    """
    access = Access(user_id=user_id)
    user = db_user
    if user is None:
        user = await user_repo.get(session, user_id)
    access.user = user
    if user is not None:
        try:
            access.role = Role(user.role)
        except ValueError:  # pragma: no cover - row written by an older version
            access.role = Role.USER

    if settings.is_owner(user_id):
        access.role, access.permissions = Role.OWNER, dict(mod_repo.ALL_PERMS)
        return access
    if settings.is_admin(user_id) and access.role is not Role.OWNER:
        access.role = Role.ADMIN

    # Granular sudo grants (sudo_admins.permissions).
    access.permissions = await mod_repo.permissions_for(session, user_id)
    if access.permissions and access.role is Role.USER:
        access.role = Role.MODERATOR

    if chat_id is not None and chat_type in ("group", "supergroup") and bot is not None:
        access.is_group_admin = await _is_chat_admin(
            bot=bot, redis=redis, chat_id=chat_id, user_id=user_id, member=member
        )
        if access.is_group_admin and access.role is Role.USER:
            # Group staff may run group-scoped commands (spawn config, warnings)
            # but never global ones (broadcast, odds edits, economy grants).
            access.role = Role.MODERATOR

    if user is not None and (user.banned or await mod_repo.is_globally_banned(session, user_id)):
        access.global_banned = True
        access.reasons.append(user.ban_reason or "global ban")
    return access


async def _is_chat_admin(
    *, bot: Bot, redis: Redis | None, chat_id: int, user_id: int, member: ChatMemberUpdated | None
) -> bool:
    if redis is not None:
        cached = await redis.get("adm", chat_id, user_id)
        if cached is not None:
            return cached == "1"
    status: str | None = None
    if member is not None and member.chat.id == chat_id and member.new_chat_member is not None:
        status = member.new_chat_member.status
    if status is None:
        try:
            status = (await bot.get_chat_member(chat_id, user_id)).status
        except Exception:  # pragma: no cover - API hiccup: fail closed, never grant
            status = None
    allowed = status in (ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR)
    if redis is not None:
        await redis.set(("adm", chat_id, user_id), "1" if allowed else "0", ttl=ADMIN_CACHE_TTL)
    return allowed


async def invalidate_admin_cache(redis: Redis | None, chat_id: int, user_id: int) -> None:
    """Called from the chat_member handler so a demotion applies immediately."""
    if redis is not None:
        await redis.delete("adm", chat_id, user_id)


def actor_id(event: Message | CallbackQuery | Any) -> int | None:
    if isinstance(event, Message):
        return event.from_user.id if event.from_user else None
    if isinstance(event, CallbackQuery):
        return event.from_user.id
    return getattr(getattr(event, "from_user", None), "id", None)


async def deny(event: Message | CallbackQuery, reason: str = "Not allowed.") -> None:
    """Uniform rejection: an alert for button presses, a message for commands."""
    if isinstance(event, CallbackQuery):
        await event.answer(reason, show_alert=True)
        return
    await event.answer(reason)


__all__ = [
    "ADMIN_CACHE_TTL",
    "ANONYMOUS_ADMIN_ID",
    "BROADCAST_ROLES",
    "OWNER_ONLY",
    "Access",
    "actor_id",
    "deny",
    "ensure_user",
    "invalidate_admin_cache",
    "resolve",
]
