"""Rarity ladder, roles and shared enums.

Summon-bot hardcoded rarity as display strings ("⚪ Common") inside price dicts in
three different files, so a rename silently broke shops, spawns and leaderboards.
Here a single ``Rarity`` enum is the source of truth; string keys are derived.
"""

from __future__ import annotations

from enum import IntEnum, StrEnum


class Rarity(IntEnum):
    """The 18-tier ladder, kept byte-identical to the reference bot's tables.

    Summon-bot ran 18 rarities (base tiers + holiday/event editions) and priced them
    in a dict keyed on the *decorated display string* ("⚪ Common"), which is why its
    odds, shop and market disagreed after any rename. Here the ladder is the single
    source of truth: ids 1-18 are stable, the emoji/label/price/weight hang off the
    member, and display strings are derived (``badge``), never parsed.

    Weights are the raw ``rarity_chances`` values from the live bot (out of 10 000),
    so a migration keeps the same drop rates instead of silently rebalancing the game.
    """

    COMMON = 1
    RARE = 2
    SPECIAL = 3
    LEGENDARY = 4
    MYTHIC = 5
    VALENTINE = 6
    SUMMER = 7
    RAINY = 8
    HALLOWEEN = 9
    CHRISTMAS = 10
    WINTER = 11
    NEWYEAR = 12
    FESTIVAL = 13
    AMV = 14
    EVENT = 15
    CELESTIAL = 16
    LUXURY = 17
    LIMITED = 18

    @property
    def emoji(self) -> str:
        return _RARITY_EMOJI[self]

    @property
    def label(self) -> str:
        return _RARITY_LABEL[self]

    @property
    def display(self) -> str:
        """The old bot's exact display key, e.g. ``💮 Special Edition``."""
        return self.badge

    @property
    def badge(self) -> str:
        return f"{self.emoji} {self.label}"

    @property
    def weight(self) -> int:
        """Raw pull weight out of 10 000 (normalised at roll time)."""
        return _RARITY_WEIGHT[self]

    @property
    def claim_weight(self) -> int:
        """Weight for the free ``/hclaim`` roll — high tiers are gated harder."""
        return _RARITY_CLAIM[self]

    @property
    def base_price(self) -> int:
        return _RARITY_PRICE[self]

    @property
    def stars(self) -> int:
        """Telegram Stars price for the paid-media 'motion card' of this tier."""
        return _RARITY_STARS[self]

    @property
    def color(self) -> tuple[int, int, int]:
        return _RARITY_COLOR[self]

    @property
    def is_high_tier(self) -> bool:
        """Everything from Valentine up — the reference bot's ``HIGH_TIER`` set."""
        return self.value >= _HIGH_TIER_FLOOR

    @property
    def refresh_allowed(self) -> bool:
        """Tiers the shop may re-roll for (the old bot hard-coded this set)."""
        return self.value <= _REFRESH_MAX_TIER

    @classmethod
    def from_value(cls, value: object) -> Rarity:
        try:
            return cls(int(value))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return cls.COMMON

    @classmethod
    def from_label(cls, value: str) -> Rarity:
        """Resolve "🎃 Halloween Edition" / "halloween" / "9" → tier.

        Migrated rows store the decorated string, so this is the compatibility door;
        it is intentionally strict (unknown labels fall back to COMMON only for legacy
        rows, which ``scripts/import_summon.py`` reports rather than hides).
        """
        raw = (value or "").strip()
        if raw.isdigit():
            return cls.from_value(int(raw))
        needle = raw.lower().strip()
        for member in cls:
            if needle in (
                member.label.lower(),
                member.name.lower(),
                member.badge.lower(),
                member.emoji.lower(),
                member.display.lower(),
            ):
                return member
        # "🎥 AMV Edition" / "AMV edition" / "amv": compare on the undecorated name,
        # because the legacy rows spell the tier every way but one.
        stripped = needle.replace("edition", "").strip()
        for member in cls:
            label = member.label.lower().replace(" edition", "")
            name = member.name.lower()
            emoji = member.emoji.strip()
            if not stripped:
                continue
            if (
                stripped in (label, name)
                or stripped
                in (f"{emoji} {label}".strip(), f"{emoji} {member.label.lower()}".strip())
                or stripped.endswith(label)
                or stripped.endswith(name)
            ):
                return member
        return cls.COMMON

    @classmethod
    def max_tier(cls) -> Rarity:
        return max(cls)

    @classmethod
    def high_tiers(cls) -> tuple[Rarity, ...]:
        return tuple(member for member in cls if member.is_high_tier)

    def next_tier(self) -> Rarity | None:
        nxt = self.value + 1
        return Rarity(nxt) if nxt <= max(Rarity).value else None

    def at_least(self, other: Rarity | int) -> bool:
        return self.value >= int(other)


#: 6 == Valentine: the first "event" tier, matching the reference bot's HIGH_TIER set.
_HIGH_TIER_FLOOR = 6
#: Common…Mythic could be re-rolled in the old shop (``REFRESH_ALLOWED``).
_REFRESH_MAX_TIER = 5

_RARITY_EMOJI: dict[Rarity, str] = {
    Rarity.COMMON: "⚪",
    Rarity.RARE: "🔵",
    Rarity.SPECIAL: "💮",
    Rarity.LEGENDARY: "⭐",
    Rarity.MYTHIC: "🛸",
    Rarity.VALENTINE: "💝",
    Rarity.SUMMER: "🏖️",
    Rarity.RAINY: "🌧️",
    Rarity.HALLOWEEN: "🎃",
    Rarity.CHRISTMAS: "🎄",
    Rarity.WINTER: "❄️",
    Rarity.NEWYEAR: "🎇",
    Rarity.FESTIVAL: "🎍",
    Rarity.AMV: "🎥",
    Rarity.EVENT: "🎉",
    Rarity.CELESTIAL: "🌌",
    Rarity.LUXURY: "💎",
    Rarity.LIMITED: "🔮",
}

_RARITY_LABEL: dict[Rarity, str] = {
    Rarity.COMMON: "Common",
    Rarity.RARE: "Rare",
    Rarity.SPECIAL: "Special Edition",
    Rarity.LEGENDARY: "Legendary",
    Rarity.MYTHIC: "Mythic Edition",
    Rarity.VALENTINE: "Valentine Edition",
    Rarity.SUMMER: "Summer Edition",
    Rarity.RAINY: "Rainy Edition",
    Rarity.HALLOWEEN: "Halloween Edition",
    Rarity.CHRISTMAS: "Christmas Edition",
    Rarity.WINTER: "Winter Edition",
    Rarity.NEWYEAR: "New Year Edition",
    Rarity.FESTIVAL: "Festival Edition",
    Rarity.AMV: "AMV Edition",
    Rarity.EVENT: "Event Edition",
    Rarity.CELESTIAL: "Celestial Edition",
    Rarity.LUXURY: "Luxury Edition",
    Rarity.LIMITED: "Limited Edition",
}

#: Live ``rarity_chances`` (per 10 000) — the reference bot's published pull rates.
_RARITY_WEIGHT: dict[Rarity, int] = {
    Rarity.COMMON: 4500,
    Rarity.RARE: 2500,
    Rarity.SPECIAL: 1200,
    Rarity.LEGENDARY: 600,
    Rarity.MYTHIC: 300,
    Rarity.VALENTINE: 50,
    Rarity.SUMMER: 50,
    Rarity.RAINY: 50,
    Rarity.HALLOWEEN: 50,
    Rarity.CHRISTMAS: 50,
    Rarity.WINTER: 50,
    Rarity.NEWYEAR: 50,
    Rarity.FESTIVAL: 40,
    Rarity.AMV: 10,
    Rarity.EVENT: 100,
    Rarity.CELESTIAL: 40,
    Rarity.LUXURY: 30,
    Rarity.LIMITED: 20,
}

#: Live ``claim_list`` — the free /hclaim table (0 means "not claimable for free").
_RARITY_CLAIM: dict[Rarity, int] = {
    Rarity.COMMON: 400,
    Rarity.RARE: 0,
    Rarity.SPECIAL: 1200,
    Rarity.LEGENDARY: 600,
    Rarity.MYTHIC: 300,
    Rarity.VALENTINE: 50,
    Rarity.SUMMER: 50,
    Rarity.RAINY: 50,
    Rarity.HALLOWEEN: 50,
    Rarity.CHRISTMAS: 0,
    Rarity.WINTER: 2,
    Rarity.NEWYEAR: 50,
    Rarity.FESTIVAL: 40,
    Rarity.AMV: 0,
    Rarity.EVENT: 100,
    Rarity.CELESTIAL: 40,
    Rarity.LUXURY: 0,
    Rarity.LIMITED: 20,
}

#: ``PRICE`` from the reference bot, in coins.
_RARITY_PRICE: dict[Rarity, int] = {
    Rarity.COMMON: 15000,
    Rarity.RARE: 25000,
    Rarity.SPECIAL: 50000,
    Rarity.LEGENDARY: 90000,
    Rarity.MYTHIC: 150000,
    Rarity.VALENTINE: 250000,
    Rarity.SUMMER: 250000,
    Rarity.RAINY: 250000,
    Rarity.HALLOWEEN: 350000,
    Rarity.CHRISTMAS: 350000,
    Rarity.WINTER: 400000,
    Rarity.NEWYEAR: 450000,
    Rarity.FESTIVAL: 550000,
    Rarity.AMV: 650000,
    Rarity.EVENT: 650000,
    Rarity.CELESTIAL: 1000000,
    Rarity.LUXURY: 1500000,
    Rarity.LIMITED: 2000000,
}

#: Stars price for the paid-media variant of each tier (0 = not sold).
_RARITY_STARS: dict[Rarity, int] = {
    member: 0 if not member.is_high_tier else max(5, round(member.base_price / 25000))
    for member in Rarity
}

_RARITY_COLOR: dict[Rarity, tuple[int, int, int]] = {
    Rarity.COMMON: (148, 163, 184),
    Rarity.RARE: (96, 165, 250),
    Rarity.SPECIAL: (244, 114, 182),
    Rarity.LEGENDARY: (250, 204, 21),
    Rarity.MYTHIC: (167, 139, 250),
    Rarity.VALENTINE: (249, 115, 136),
    Rarity.SUMMER: (56, 189, 248),
    Rarity.RAINY: (125, 155, 200),
    Rarity.HALLOWEEN: (251, 146, 60),
    Rarity.CHRISTMAS: (74, 222, 128),
    Rarity.WINTER: (191, 219, 254),
    Rarity.NEWYEAR: (253, 224, 71),
    Rarity.FESTIVAL: (240, 171, 252),
    Rarity.AMV: (226, 232, 240),
    Rarity.EVENT: (232, 121, 249),
    Rarity.CELESTIAL: (34, 211, 238),
    Rarity.LUXURY: (103, 232, 249),
    Rarity.LIMITED: (248, 113, 113),
}

#: Shop re-roll price (``REFRESH_PRICE`` in the reference bot).
SHOP_REFRESH_PRICE = 10000
#: The reaction pool /nguess uses for quick votes.
GUESS_REACTIONS = ("🔥", "🎉", "👍", "💯", "⚡", "🥳", "👀", "✨")


class Role(StrEnum):
    """Escalating permission level, resolved by ``waifu.core.access``."""

    GUEST = "guest"  # never registered / opted out
    USER = "user"
    MODERATOR = "moderator"  # group admin, or listed in DB
    ADMIN = "admin"  # ADMIN_IDS
    OWNER = "owner"  # OWNER_ID

    def at_least(self, other: Role) -> bool:
        order = [Role.GUEST, Role.USER, Role.MODERATOR, Role.ADMIN, Role.OWNER]
        return order.index(self) >= order.index(other)


class PullKind(StrEnum):
    SINGLE = "single"
    TEN = "ten"
    FREE_DAILY = "free_daily"
    QUEST = "quest"
    SPAWN = "spawn"
    GIFT = "gift"
    MARKET = "market"
    ADMIN = "admin"
    PAID = "paid"


class LedgerReason(StrEnum):
    DAILY = "daily"
    SPIN = "spin"
    PULL = "pull"
    DUPE = "dupe"
    SELL = "sell"
    BUY = "buy"
    LISTING = "listing"
    AUCTION_WIN = "auction_win"
    AUCTION_FEE = "auction_fee"
    TRADE = "trade"
    TRADE_TAX = "trade_tax"
    GIFT = "gift"
    QUEST = "quest"
    STREAK = "streak"
    ACHIEVEMENT = "achievement"
    BATTLE = "battle"
    GUILD = "guild"
    STARS = "stars"
    REFUND = "refund"
    REDEEM = "redeem"
    #: The opening balance is a ledger event, not a column default: keeping it on the
    #: books is what lets ``balance == sum(delta)`` hold for *every* account, which is
    #: the invariant /integrity and ``waifu doctor`` check.
    SIGNUP = "signup"
    ADMIN_GRANT = "admin_grant"
    ADMIN_TAKE = "admin_take"
    JACKPOT = "jackpot"
    PREMIUM = "premium"
    AI_TIP = "ai_tip"


class ChatMode(StrEnum):
    OFF = "off"
    RICH = "rich"
    PLAIN = "plain"


class TradeStatus(StrEnum):
    PROPOSED = "proposed"
    AWAITING_PARTNER = "awaiting_partner"
    ACCEPTED = "accepted"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    COMPLETED = "completed"


class ListingStatus(StrEnum):
    OPEN = "open"
    SOLD = "sold"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class AuctionStatus(StrEnum):
    LIVE = "live"
    SOLD = "sold"
    NO_SALE = "no_sale"
    CANCELLED = "cancelled"


class QuestKind(StrEnum):
    DAILY = "daily"
    WEEKLY = "weekly"
    EVENT = "event"


class SubscriptionState(StrEnum):
    ACTIVE = "active"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
