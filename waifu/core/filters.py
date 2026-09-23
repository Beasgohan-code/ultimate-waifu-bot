"""Reusable handlers/filters.

Filters read ``data`` filled by the middlewares, so they never touch the network
themselves — an update that reaches a filter has already had its access resolved.
"""

from __future__ import annotations

from aiogram.filters import BaseFilter
from aiogram.types import CallbackQuery, Message, TelegramObject
from aiogram.types import User as TgUser

from waifu.core.access import Access
from waifu.db.repositories.users import Role
from waifu.errors import PermissionDenied


class IsRegistered(BaseFilter):
    """Update belongs to a user who has a row (i.e. has been /started)."""

    async def __call__(self, event: TelegramObject, access: Access | None = None) -> bool:
        return access is not None and access.role is not Role.GUEST


class RequirePermission(BaseFilter):
    """Gate a handler on a permission slug (see repositories/users.PERMISSIONS)."""

    def __init__(
        self, permission: str | None = None, *, roles: tuple[Role, ...] | None = None
    ) -> None:
        self.permission = permission
        self.roles = roles

    async def __call__(
        self, event: TelegramObject, access: Access | None = None
    ) -> bool | dict[str, Access]:
        if access is None:
            return False
        if self.roles is not None and access.role not in self.roles:
            return False
        if self.permission is not None and not access.can(self.permission):
            return False
        # Returning the object injects it into the handler signature and makes the
        # filter visible in aiogram's debug output.
        return {"staff_access": access}


class IsStaff(BaseFilter):
    """Owner/admin/granular-sudo — anything that can touch other players' data."""

    def __init__(self, permission: str | None = None) -> None:
        self.permission = permission

    async def __call__(self, event: TelegramObject, access: Access | None = None) -> bool:
        return access is not None and (
            access.role in (Role.OWNER, Role.ADMIN) or access.can(self.permission)
        )


class GroupOnly(BaseFilter):
    async def __call__(self, event: TelegramObject) -> bool:
        chat = getattr(event, "chat", None) or getattr(
            getattr(event, "message", None), "chat", None
        )
        return bool(chat and chat.type in ("group", "supergroup"))


class PrivateOnly(BaseFilter):
    async def __call__(self, event: Message) -> bool:
        return bool(event.chat and event.chat.type == "private")


class FeatureEnabled(BaseFilter):
    """``F.feature("nguess")`` — lets an owner turn a subsystem off at runtime."""

    def __init__(self, name: str) -> None:
        self.name = name

    async def __call__(self, event: TelegramObject, ctx) -> bool:
        return ctx.features.is_enabled(self.name)


class HasCommand(BaseFilter):
    """Command match that also tolerates ``@BotName`` suffixes and `/mzh:` shorthands."""

    def __init__(self, *names: str) -> None:
        self.names = frozenset(names)

    async def __call__(self, event: Message, settings) -> bool:
        if not event.text or not event.text.startswith("/"):
            return False
        head = event.text.split(maxsplit=1)[0][1:]
        base = head.split("@", 1)[0].lower()
        prefixes = set(settings.command_prefixes or [])
        for prefix in prefixes:
            if base.startswith(prefix) and base[len(prefix) :]:
                base = base[len(prefix) :]
                break
        return base in self.names


class DevFilter(BaseFilter):
    """Owner-only debug surface, additionally gated by ``FEATURE_DEV_CONSOLE``."""

    async def __call__(self, event: TelegramObject, ctx, access: Access | None = None) -> bool:
        return bool(
            ctx.settings.features.dev_console and access is not None and access.role is Role.OWNER
        )


class StateIn(BaseFilter):
    def __init__(self, *states: str) -> None:
        self.states = frozenset(states)

    async def __call__(self, event: TelegramObject, state_name: str | None = None) -> bool:
        return state_name in self.states


class CallbackPrefix(BaseFilter):
    """Match this router's callback namespace without touching the network.

    ``CallbackPrefix("auc", "bid")`` ⇔ ``callback_data.startswith(("auc:", "bid:"))``.
    Encoded as a filter class instead of an ``F.data.startswith`` magic expression
    because ``F`` raises on ``data=None`` (edited/hidden callbacks) and that used to
    take down the whole update in Summon-bot.
    """

    def __init__(self, *prefixes: str) -> None:
        self.prefixes = tuple(p if p.endswith(":") else f"{p}:" for p in prefixes)

    async def __call__(self, query: CallbackQuery) -> bool:
        return bool(query.data) and query.data.startswith(self.prefixes)


def callback_data(*prefixes: str) -> CallbackPrefix:
    return CallbackPrefix(*prefixes)


class HasSenderChat(BaseFilter):
    """Anonymous channel-posted messages (``sender_chat``), e.g. group broadcasts."""

    async def __call__(self, event: TelegramObject) -> bool:
        return getattr(event, "sender_chat", None) is not None


async def assert_access(access: Access, permission: str | None) -> None:
    """Raise for handlers that need a message back instead of silent filtering."""
    if not access.can(permission):
        raise PermissionDenied(f"missing permission: {permission}")


def tg_user_id(event: TelegramObject) -> int | None:
    user: TgUser | None = None
    if isinstance(event, (Message, CallbackQuery)):
        user = getattr(event, "from_user", None)
    return user.id if user else None
