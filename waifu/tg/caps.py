"""Capability negotiation — "does *this* endpoint have *that* method?"

aiogram 3.31 ships types for Bot API 10.3, but a self-hosted Bot API server (the
default in the repo's compose file) can be older, and Telegram silently ignores
unknown parameters on some methods while hard-failing on others. Every optional
feature therefore asks :class:`Caps` first.

Detection strategy, in order of trust:

1. explicit env overrides (``FEATURE_RICH_MESSAGES=0``) — ops escape hatch;
2. ``getMe`` fields the server itself reports (``can_manage_guests``);
3. a probe call at startup, whose result is cached for the process lifetime.

The outcome is logged once so an operator sees *why* the bot is degrading.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from waifu.enums import ChatMode
from waifu.logging import get_logger
from waifu.settings import Settings

log = get_logger("tg.caps")


@dataclass(slots=True)
class Caps:
    """What this bot/API combination can actually do."""

    rich_messages: bool = False
    ephemeral: bool = False
    drafts: bool = False
    guest_mode: bool = False
    stories: bool = False
    member_tags: bool = True
    emoji_status: bool = False
    paid_media: bool = True
    verify: bool = False
    button_styles: bool = True
    checklist: bool = False
    reactions: bool = True
    raw: dict[str, bool] = field(default_factory=dict)
    forced: dict[str, bool] = field(default_factory=dict)

    @classmethod
    def negotiate(cls, settings: Settings, api_flags: dict[str, bool] | None = None) -> Caps:
        flags = api_flags or {}
        caps = cls(
            rich_messages=bool(flags.get("rich_message")),
            drafts=bool(flags.get("message_draft")),
            guest_mode=bool(flags.get("guest_mode")),
            stories=bool(flags.get("stories", True)),
            emoji_status=bool(flags.get("emoji_status", True)),
            verify=bool(flags.get("verify", True)),
            checklist=bool(flags.get("checklist", True)),
            ephemeral=True,  # plain send-side ephemeral is live on api.telegram.org
            member_tags=True,
            button_styles=True,
        )
        caps.raw = dict(flags)
        overrides = {
            "rich_messages": settings.features.rich_messages,
            "drafts": settings.features.draft_stream,
            "reactions": settings.features.reactions,
        }
        for name, value in overrides.items():
            if value is False:
                caps._set(name, False, why="feature flag")
            elif value is True:
                caps._set(name, True, why="feature flag")
        caps.reactions = settings.features.reactions
        return caps

    def _set(self, name: str, value: bool, *, why: str) -> None:
        setattr(self, name, value)
        self.forced[name] = value
        log.debug("capability %s := %s (%s)", name, value, why)

    # --------------------------------------------------------------- gate check
    #: Handler-side vocabulary → attribute name. Handlers say what they want to do,
    #: this class owns how that maps to a capability.
    ALIASES = {
        "rich": "rich_messages",
        "rich_messages": "rich_messages",
        "drafts": "drafts",
        "draft": "drafts",
        "message_draft": "drafts",
        "streaming": "drafts",
        "ephemeral": "ephemeral",
        "paid_media": "paid_media",
        "checklist": "checklist",
        "reactions": "reactions",
        "stories": "stories",
        "guest": "guest_mode",
        "guest_mode": "guest_mode",
        "member_tags": "member_tags",
        "tags": "member_tags",
        "emoji_status": "emoji_status",
        "verify": "verify",
        "button_styles": "button_styles",
    }

    def allow(self, name: str) -> bool:
        """``caps.allow("rich")`` — the gate every handler uses before trying a
        new-API flourish. Unknown names are False, never an exception, because a
        typo must degrade to the old behaviour rather than break the command."""
        return bool(getattr(self, self.ALIASES.get(name, name), False))

    # ------------------------------------------------------------------ modes
    def card_mode(
        self, requested: ChatMode | str | None = None, *, settings: Settings | None = None
    ) -> ChatMode:
        """Decide how a card should be delivered for this chat.

        ``ChatMode.RICH`` → rich message; ``PLAIN`` → caption + inline keyboard;
        ``OFF`` → text only. Falls back to PLAIN whenever rich messages are not
        available, so behaviour is identical on api.telegram.org and on an old
        local server.
        """
        mode = (
            requested
            if isinstance(requested, ChatMode)
            else ChatMode.from_value(str(requested or (settings.chat_mode if settings else "auto")))
        )
        if mode is ChatMode.OFF:
            return ChatMode.OFF
        if mode in (ChatMode.RICH, ChatMode.AUTO) and not self.rich_messages:
            return ChatMode.PLAIN
        return (
            mode
            if mode is not ChatMode.AUTO
            else (ChatMode.RICH if self.rich_messages else ChatMode.PLAIN)
        )

    def summary(self) -> str:
        on = sorted(k for k, v in self.as_dict().items() if v)
        return ", ".join(on) or "none"

    def as_dict(self) -> dict[str, bool]:
        return {
            "rich_messages": self.rich_messages,
            "ephemeral": self.ephemeral,
            "drafts": self.drafts,
            "guest_mode": self.guest_mode,
            "stories": self.stories,
            "member_tags": self.member_tags,
            "emoji_status": self.emoji_status,
            "paid_media": self.paid_media,
            "verify": self.verify,
            "button_styles": self.button_styles,
            "checklist": self.checklist,
            "reactions": self.reactions,
        }
