"""Postgres-first schema (Redis holds hot state; this is the durable record).

Mapped 1:1 onto Summon-bot's domain — same tables in spirit (users, characters,
collections, inventory, cooldowns, premium, claim_list, rarity chances, group
settings/spawn counters, sudo admins, warnings, auctions, redeem codes, streaks,
achievements, prefs) — with three structural upgrades:

1. **Integrity.** Real FKs, CHECK constraints on money, and unique constraints
   where Summon-bot relied on "SELECT then INSERT" (a double-tap could pay twice).
2. **JSONB + GIN.** Player prefs and spawn payloads are JSONB (indexable), not
   TEXT blobs, and character names get a trigram-ish index for ``/search``.
3. **Auditability.** Every coin/character movement writes a ledger row, so a
   player dispute is a ``SELECT`` instead of a guess.

Runtime is Postgres-only by design (``waifu.settings`` rejects other URLs);
SQLite is used by the unit-test harness only, hence the portable type variants.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

# JSONB on Postgres, plain JSON on the SQLite test harness. Same Python dict API.
JSONDoc = JSON().with_variant(JSONB, "postgresql")

#: Autoincrement id that still autoincrements on SQLite.
#:
#: ``BigInteger`` primary keys do **not** alias ``rowid`` in SQLite, so a plain
#: ``BigInteger`` id means every insert on the dev/test driver dies with
#: "NOT NULL constraint failed: <table>.id". The variant keeps 64-bit ids where the
#: ids actually come from Telegram and Postgres needs them, and degrades to INTEGER
#: (i.e. rowid) on SQLite.
BigIntPK = BigInteger().with_variant(Integer, "sqlite")

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _now() -> datetime:
    from waifu.utils.time import now_utc

    return now_utc()


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, server_default=func.now()
    )


# --------------------------------------------------------------------------- players
class User(Base, TimestampMixin):
    """One row per Telegram account (Summon-bot's ``users`` table, hardened)."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=False)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str] = mapped_column(String(128), default="")
    last_name: Mapped[str] = mapped_column(String(128), default="")
    locale: Mapped[str] = mapped_column(String(8), default="en", nullable=False)

    balance: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    exp: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    level: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    # --- gacha state (Summon-bot had no pity; bad luck was punishment) ---
    pulls_total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    high_pulls: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rarity_histogram: Mapped[dict] = mapped_column(JSONDoc, default=dict, nullable=False)
    pity_rare: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pity_high: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pity_celestial: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    lucky_charges: Mapped[int] = mapped_column(Integer, default=0, nullable=False)  # 🎟️ Lucky Ticket
    xp_boost_charges: Mapped[int] = mapped_column(Integer, default=0, nullable=False)  # ⚡ XP Boost
    magnet_charges: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )  # 💰 Coin Magnet

    # --- timers (Summon-bot stored these as TEXT and parsed two formats) ---
    last_daily: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_spin: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_hclaim: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_hclaim_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_pull: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    # --- streaks ---
    streak_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    streak_best: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    streak_last_date: Mapped[str] = mapped_column(String(10), default="", nullable=False)

    # --- moderation / roles ---
    role: Mapped[str] = mapped_column(String(16), default="user", nullable=False)
    banned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    ban_reason: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    warn_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # --- premium (mirrors `premium` table; denormalised for hot path reads) ---
    premium_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # --- provably fair per-player seed ---
    server_seed: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    seed_commitment: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    roll_sequence: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # --- AI persona budget (rides inside /hstats + /check; no new command) ---
    ai_chars_today: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ai_reset_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Display prefs: featured character, glow, card theme, privacy toggles.
    prefs: Mapped[dict] = mapped_column(JSONDoc, default=dict, nullable=False)
    referrer_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL")
    )
    invite_code: Mapped[str] = mapped_column(String(16), default="", nullable=False)

    __table_args__ = (
        CheckConstraint("balance >= 0", name="balance_not_negative"),
        CheckConstraint("level >= 1", name="level_positive"),
        UniqueConstraint("invite_code", name="uq_users_invite_code"),
        Index("ix_users_balance", "balance"),
        Index("ix_users_seen", "last_seen_at"),
        Index("ix_users_username", "username"),
    )

    @property
    def full_name(self) -> str:
        return " ".join(p for p in (self.first_name, self.last_name) if p).strip() or (
            self.username or "player"
        )

    @property
    def is_premium(self) -> bool:
        return bool(self.premium_until and self.premium_until.replace(tzinfo=None) > _now())

    @property
    def active_boosts(self) -> dict[str, int]:
        return {
            "lucky": self.lucky_charges,
            "xp": self.xp_boost_charges,
            "magnet": self.magnet_charges,
        }


class Character(Base, TimestampMixin):
    """The collectible catalogue."""

    __tablename__ = "characters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(96), nullable=False)
    anime: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    rarity: Mapped[str] = mapped_column(
        String(48), default="", nullable=False
    )  # display name, as in Summon-bot
    rarity_id: Mapped[int] = mapped_column(
        SmallInteger, default=1, nullable=False
    )  # ladder index (source of truth)

    # Media: file_id is preferred (fast, permanent, no hotlink 404s); URL is the fallback.
    photo_file_id: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    image_url: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    video_file_id: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    video_url: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    live_photo_file_id: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    sticker_file_id: Mapped[str] = mapped_column(String(255), default="", nullable=False)

    price: Mapped[int] = mapped_column(BigInteger, default=15000, nullable=False)
    stat_power: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    voice_line: Mapped[str] = mapped_column(Text, default="", nullable=False)
    persona: Mapped[str] = mapped_column(Text, default="", nullable=False)
    tags: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    banner_weight: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    meta: Mapped[dict] = mapped_column(JSONDoc, default=dict, nullable=False)

    __table_args__ = (
        UniqueConstraint("name", "anime", name="uq_characters_name_anime"),
        Index("ix_characters_rarity", "rarity_id"),
        Index("ix_characters_active", "is_active", "rarity_id"),
    )

    @property
    def searchable(self) -> str:
        return f"{self.name} {self.anime} {self.tags}".lower()

    @property
    def has_image(self) -> bool:
        return bool(self.photo_file_id or self.image_url)

    def image_ref(self) -> str:
        """What to hand to sendPhoto / InputMediaPhoto: file_id wins."""
        return self.photo_file_id or self.image_url


class RarityChance(Base):
    """/chance + /chancelist: admin-tunable drop weights (Summon-bot's rarity_chances)."""

    __tablename__ = "rarity_chances"

    rarity_id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, autoincrement=False)
    rarity_name: Mapped[str] = mapped_column(String(48), nullable=False)
    chance: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)  # percent, 0-100
    min_price: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    __table_args__ = (CheckConstraint("chance >= 0 AND chance <= 100", name="chance_percent"),)


class ClaimChance(Base):
    """/setclaim + /claimlist: per-rarity odds for the free /hclaim roll."""

    __tablename__ = "claim_list"

    rarity_id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, autoincrement=False)
    rarity_name: Mapped[str] = mapped_column(String(48), nullable=False)
    chance: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    __table_args__ = (
        CheckConstraint("chance >= 0 AND chance <= 100", name="claim_chance_percent"),
    )


# ------------------------------------------------------------------------ collection
class Ownership(Base):
    __tablename__ = "user_collection"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    character_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("characters.id", ondelete="CASCADE"), nullable=False
    )
    count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    is_favorite: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_locked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    first_obtained: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_obtained: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    source: Mapped[str] = mapped_column(String(24), default="claim", nullable=False)

    user: Mapped[User] = relationship(lazy="noload")
    character: Mapped[Character] = relationship(lazy="noload")

    __table_args__ = (
        UniqueConstraint("user_id", "character_id", name="uq_collection_user_character"),
        CheckConstraint("count >= 0", name="count_not_negative"),
        Index("ix_collection_user", "user_id", "count"),
        Index("ix_collection_character", "character_id"),
    )


class Transaction(Base):
    """Append-only coin ledger. The single source of truth for disputes."""

    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    delta: Mapped[int] = mapped_column(BigInteger, nullable=False)
    balance_after: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    reference: Mapped[str] = mapped_column(String(96), default="", nullable=False)
    counterparty: Mapped[int | None] = mapped_column(BigInteger)
    meta: Mapped[dict] = mapped_column(JSONDoc, default=dict, nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(96))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_transactions_idempotency_key"),
        Index("ix_tx_user_time", "user_id", "created_at"),
        Index("ix_tx_reason", "reason", "created_at"),
    )


class FairRoll(Base):
    """Provably-fair pull record: commitment published before, seed revealed after."""

    __tablename__ = "fair_rolls"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    index_in_batch: Mapped[int] = mapped_column(SmallInteger, default=0, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), default="hclaim", nullable=False)
    commitment: Mapped[str] = mapped_column(String(64), nullable=False)
    seed_reveal: Mapped[str] = mapped_column(String(96), default="", nullable=False)
    roll_value: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    rarity_id: Mapped[int] = mapped_column(SmallInteger, default=1, nullable=False)
    character_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("characters.id", ondelete="SET NULL")
    )
    was_dupe: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    was_pity: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    payout: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (
        UniqueConstraint("user_id", "sequence", "index_in_batch", name="uq_fair_rolls_sequence"),
        Index("ix_fair_rolls_user", "user_id", "created_at"),
    )


# ------------------------------------------------------------------ items & cooldowns
class InventoryItem(Base):
    """/market shop items with limited uses (Summon-bot's user_inventory)."""

    __tablename__ = "user_inventory"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    item_id: Mapped[str] = mapped_column(String(24), nullable=False)
    uses_remaining: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    #: Shop items rot. ``plugins/market.py`` wrote ``expires_at = datetime('now','+24 hours')`` on
    #: every purchase and filtered on it in *every* query — the anti-hoarding rule that also
    #: explains why a shield bought for tomorrow's raid is gone by tomorrow. ``None`` means
    #: permanent (an admin grant, or a pre-migration row), which is why it is nullable.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (
        CheckConstraint("uses_remaining >= 0", name="uses_not_negative"),
        Index("ix_inventory_lookup", "user_id", "item_id", "uses_remaining"),
        Index("ix_inventory_expiry", "user_id", "item_id", "expires_at"),
    )


class Cooldown(Base):
    """bomb/steal/etc. cooldowns (Summon-bot's ``cooldowns`` table)."""

    __tablename__ = "cooldowns"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    command: Mapped[str] = mapped_column(String(24), primary_key=True)
    last_used: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (UniqueConstraint("user_id", "command", name="uq_cooldowns_user_command"),)


class Shield(Base):
    """Steal/Bomb shield charge ledger — one row per charge so partial use is honest."""

    __tablename__ = "shields"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # sshield | bshield
    is_used: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_shield_available", "user_id", "kind", "is_used"),
        CheckConstraint("kind in ('sshield','bshield')", name="shield_kind"),
    )


class HeistLog(Base):
    """Success/Block history for /steal and /bomb (feeds /pinfo and /hstats)."""

    __tablename__ = "heist_log"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True)
    attacker_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    target_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kind: Mapped[str] = mapped_column(String(12), nullable=False)  # steal | bomb
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)  # success | blocked | failed
    amount: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    character_id: Mapped[int | None] = mapped_column(Integer)
    note: Mapped[str] = mapped_column(String(140), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (
        Index("ix_heist_attacker", "attacker_id", "created_at"),
        Index("ix_heist_target", "target_id", "created_at"),
    )


class Premium(Base):
    """Premium grants (Summon-bot's ``premium`` table); also written by Stars buys."""

    __tablename__ = "premium"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    hours: Mapped[int] = mapped_column(Integer, default=24, nullable=False)
    granted_by: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    source: Mapped[str] = mapped_column(
        String(16), default="admin", nullable=False
    )  # admin|stars|boost|gift
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (Index("ix_premium_lookup", "user_id", "expires_at"),)


class SubscriptionAccess(Base):
    """Recurring Telegram Stars subscription state (Bot API 10.1 ``subscription`` update)."""

    __tablename__ = "subscription_access"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    subscription_id: Mapped[str] = mapped_column(String(64), nullable=False)
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    tier: Mapped[str] = mapped_column(String(24), default="supporter", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    currency: Mapped[str] = mapped_column(String(8), default="XTR", nullable=False)
    amount: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_from_gift: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    __table_args__ = (UniqueConstraint("user_id", "subscription_id", name="uq_sub_user_subid"),)


class StarPurchase(Base):
    """XTR invoice + paid-media purchase, keyed by an opaque payload (idempotent)."""

    __tablename__ = "star_purchases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    invoice_payload: Mapped[str] = mapped_column(String(96), unique=True, nullable=False)
    source: Mapped[str] = mapped_column(
        String(16), default="invoice", nullable=False
    )  # invoice|paid_media
    product: Mapped[str] = mapped_column(String(32), default="coins", nullable=False)
    product_ref: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    star_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    coins_granted: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    premium_hours: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    character_id: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    telegram_payment_charge_id: Mapped[str] = mapped_column(String(96), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    refunded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_star_status", "status", "created_at"),
        Index("ix_star_user", "user_id", "created_at"),
    )


# ---------------------------------------------------------------------- groups & spawn
class Group(Base, TimestampMixin):
    """Registered groups (Summon-bot's ``groups`` + ``group_settings`` merged)."""

    __tablename__ = "groups"

    chat_id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=False)
    title: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    username: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    is_registered: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    message_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    spawn_limit: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    spawn_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_spawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_spawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    auto_ban_spam: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    spam_limit: Mapped[int] = mapped_column(Integer, default=20, nullable=False)
    log_channel_id: Mapped[int | None] = mapped_column(BigInteger)
    welcome_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    bot_id_tag: Mapped[int | None] = mapped_column(Integer)
    data: Mapped[dict] = mapped_column(JSONDoc, default=dict, nullable=False)

    __table_args__ = (
        CheckConstraint("spawn_limit > 0", name="spawn_limit_positive"),
        Index("ix_groups_next_spawn", "spawn_enabled", "next_spawn_at"),
    )


class SpawnEvent(Base):
    """The active spawn in a chat. Claiming is a compare-and-set on this row."""

    __tablename__ = "spawn_events"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    character_id: Mapped[int] = mapped_column(Integer, nullable=False)
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    rich_message: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    source: Mapped[str] = mapped_column(
        String(12), default="manual", nullable=False
    )  # manual|auto|admin
    status: Mapped[str] = mapped_column(
        String(12), default="active", nullable=False
    )  # active|claimed|expired
    hint_used: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    expected_name: Mapped[str] = mapped_column(String(96), default="", nullable=False)
    claimed_by: Mapped[int | None] = mapped_column(BigInteger)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    spawns_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONDoc, default=dict, nullable=False)

    __table_args__ = (
        Index("ix_spawn_active", "status", "expires_at"),
        Index("ix_spawn_chat_active", "chat_id", "status"),
    )


class GuessSession(Base):
    """/nguess — the in-chat guessing round (was an in-process dict in Summon-bot,
    so a restart erased every live round; now durable + Redis-cached)."""

    __tablename__ = "guess_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    character_id: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(12), default="open", nullable=False)
    mode: Mapped[str] = mapped_column(
        String(12), default="poll", nullable=False
    )  # free_text | poll
    reward: Mapped[int] = mapped_column(BigInteger, default=20, nullable=False)
    answers: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    winner_id: Mapped[int | None] = mapped_column(BigInteger)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    closes_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    data: Mapped[dict] = mapped_column(JSONDoc, default=dict, nullable=False)

    __table_args__ = (
        UniqueConstraint("chat_id", name="uq_guess_chat"),
        Index("ix_guess_open", "status", "closes_at"),
    )


class GuessStreak(Base):
    __tablename__ = "guess_streaks"

    chat_id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=False)
    current_streak: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_correct_user: Mapped[int | None] = mapped_column(BigInteger)
    total_rounds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


# --------------------------------------------------------------------------- auctions
class Auction(Base):
    __tablename__ = "auctions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    seller_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    character_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("characters.id", ondelete="CASCADE"), nullable=False
    )
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(12), default="live", nullable=False)
    start_price: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reserve_price: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    min_increment: Mapped[int] = mapped_column(BigInteger, default=500, nullable=False)
    current_bid: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    top_bidder_id: Mapped[int | None] = mapped_column(BigInteger)
    bids_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    extensions: Mapped[int] = mapped_column(SmallInteger, default=0, nullable=False)
    last_extend_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    winner_id: Mapped[int | None] = mapped_column(BigInteger)
    sold_price: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    fee: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    note: Mapped[str] = mapped_column(String(140), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    bids: Mapped[list[AuctionBid]] = relationship(cascade="all, delete-orphan", lazy="noload")

    __table_args__ = (
        Index("ix_auction_live", "status", "ends_at"),
        Index("ix_auction_seller", "seller_id", "status"),
        CheckConstraint("current_bid >= 0", name="bid_not_negative"),
    )


class AuctionBid(Base):
    __tablename__ = "auction_bids"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True)
    auction_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("auctions.id", ondelete="CASCADE"), nullable=False
    )
    bidder_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    is_outbid_notified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_refunded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (Index("ix_bid_auction_amount", "auction_id", "amount"),)


class TradeOffer(Base):
    """Escrow trade driven entirely from /gift + inline buttons (no new command)."""

    __tablename__ = "trade_offers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(12), unique=True, nullable=False)
    initiator_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    partner_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="proposed", nullable=False)
    initiator_offer: Mapped[dict] = mapped_column(JSONDoc, default=dict, nullable=False)
    partner_offer: Mapped[dict] = mapped_column(JSONDoc, default=dict, nullable=False)
    cash: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    initiator_accepted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    partner_accepted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    ephemeral_ids: Mapped[dict] = mapped_column(JSONDoc, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_trade_status", "status", "expires_at"),)


# --------------------------------------------------------------------------- codes
class RedeemCode(Base):
    __tablename__ = "redeem_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    character_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("characters.id", ondelete="SET NULL")
    )
    reward: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    coins: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    premium_hours: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    uses: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    used_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_by: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    note: Mapped[str] = mapped_column(String(140), default="", nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (
        CheckConstraint("uses > 0", name="uses_positive"),
        CheckConstraint("used_count >= 0", name="used_count_not_negative"),
        Index("ix_code_active", "is_active", "expires_at"),
    )


class CodeClaim(Base):
    __tablename__ = "code_claims"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (UniqueConstraint("code", "user_id", name="uq_code_claims_code_user"),)


class GiftLog(Base):
    __tablename__ = "gift_log"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True)
    sender_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    receiver_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    character_id: Mapped[int] = mapped_column(Integer, nullable=False)
    note: Mapped[str] = mapped_column(String(140), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (Index("ix_gift_sender", "sender_id", "created_at"),)


# ------------------------------------------------------------------------ progression
class Achievement(Base):
    __tablename__ = "user_achievements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    achievement_id: Mapped[str] = mapped_column(String(48), nullable=False)
    progress: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    unlocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("user_id", "achievement_id", name="uq_achievements_user_id"),
    )


class Streak(Base):
    """Per-user streak history (denormalised counters live on ``users``)."""

    __tablename__ = "user_streaks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    current: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    highest: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_date: Mapped[str] = mapped_column(String(10), default="", nullable=False)
    freezes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    __table_args__ = (UniqueConstraint("user_id", name="uq_streaks_user"),)


class DailyClaim(Base):
    """Idempotency for daily/spin/hclaim/free-claim (unique per user+kind+day)."""

    __tablename__ = "daily_claims"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    local_day: Mapped[str] = mapped_column(String(10), nullable=False)
    amount: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (
        UniqueConstraint("user_id", "kind", "local_day", name="uq_daily_claims_kind_day"),
    )


class UserPref(Base, TimestampMixin):
    """/hmode, /font, profile glow, privacy — one JSONB doc per user."""

    __tablename__ = "user_prefs"

    user_id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=False)
    hmode: Mapped[str] = mapped_column(String(24), default="rarity", nullable=False)
    font: Mapped[str] = mapped_column(String(16), default="default", nullable=False)
    glow: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    show_balance: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    featured_character_id: Mapped[int | None] = mapped_column(Integer)
    flags: Mapped[dict] = mapped_column(JSONDoc, default=dict, nullable=False)


# --------------------------------------------------------------------- admin & modding
class SudoAdmin(Base):
    __tablename__ = "sudo_admins"

    user_id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=False)
    username: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    added_by: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    permissions: Mapped[dict] = mapped_column(JSONDoc, default=dict, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class BannedUser(Base):
    """Kept for parity with Summon-bot's ``banned_users`` (fast global ban list)."""

    __tablename__ = "banned_users"

    user_id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=False)
    username: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    reason: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    banned_by: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Warning(Base):
    __tablename__ = "warnings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    moderator_id: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    reason: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    is_resolved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (Index("ix_warnings_chat_user", "chat_id", "user_id", "is_resolved"),)


class ModerationCase(Base):
    """Mute/ban/removed messages with the message ids that prove it."""

    __tablename__ = "moderation_cases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    target_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    moderator_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    action: Mapped[str] = mapped_column(String(24), nullable=False)
    reason: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    duration_seconds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    message_ids: Mapped[dict] = mapped_column(JSONDoc, default=dict, nullable=False)
    member_tag: Mapped[str] = mapped_column(String(48), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_mod_target", "chat_id", "target_user_id", "is_active"),)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True)
    actor_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    scope: Mapped[str] = mapped_column(String(12), default="global", nullable=False)
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    action: Mapped[str] = mapped_column(String(48), nullable=False)
    target: Mapped[str] = mapped_column(String(96), default="", nullable=False)
    detail: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (
        Index("ix_audit_time", "created_at"),
        Index("ix_audit_actor", "actor_id", "created_at"),
    )


class ActivityLog(Base):
    """Cheap per-message activity counter for /stats and the dashboard."""

    __tablename__ = "activity_log"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), default="message", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (
        Index("ix_activity_time", "created_at"),
        Index("ix_activity_chat", "chat_id", "created_at"),
    )


class AiMessageLog(Base):
    __tablename__ = "ai_messages"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    character_id: Mapped[int | None] = mapped_column(Integer)
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    role: Mapped[str] = mapped_column(String(12), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    streamed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    flagged: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (Index("ix_ai_user_time", "user_id", "created_at"),)


class BoostGrant(Base):
    """Community-channel boost -> daily perk, deduped by boost_id."""

    __tablename__ = "boost_grants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    boost_id: Mapped[str] = mapped_column(String(48), nullable=False)
    boost_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    source: Mapped[str] = mapped_column(String(24), default="premium", nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reward_state: Mapped[str] = mapped_column(String(12), default="pending", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (UniqueConstraint("user_id", "boost_id", name="uq_boost_user_boost"),)


class RaffleRound(Base):
    """Reaction raffle on spawn announcements: react 🔥 to enter, drawn at close."""

    __tablename__ = "raffle_rounds"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    emoji: Mapped[str] = mapped_column(String(16), default="🔥", nullable=False)
    status: Mapped[str] = mapped_column(String(12), default="open", nullable=False)
    theme: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    reward: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    max_winners: Mapped[int] = mapped_column(SmallInteger, default=3, nullable=False)
    entrants: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    winners: Mapped[dict] = mapped_column(JSONDoc, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("ix_raffle_open", "status", "ends_at"),)


class ShopPool(Base):
    """Last /shop refresh pool per user+rarity (durable mirror of the Redis cache)."""

    __tablename__ = "shop_pools"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    rarity: Mapped[str] = mapped_column(String(48), nullable=False)
    characters: Mapped[dict] = mapped_column(JSONDoc, default=list, nullable=False)  # type: ignore[assignment]
    refreshes_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    __table_args__ = (UniqueConstraint("user_id", "rarity", name="uq_shop_pool_user_rarity"),)


# -------------------------------------------------------------------------- platform
class StatsSnapshot(Base):
    __tablename__ = "stats_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    users_total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    users_active_24h: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    coins_circulating: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    characters_total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    claims_total: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    groups_total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    stars_total: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    extra: Mapped[dict] = mapped_column(JSONDoc, default=dict, nullable=False)


class SchemaVersion(Base):
    __tablename__ = "schema_version"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class KvState(Base):
    """Small durable key/value store for job checkpoints (last raffle id, etc.)."""

    __tablename__ = "kv_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONDoc, default=dict, nullable=False)  # type: ignore[assignment]
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


MODEL_NAMES = (
    "users",
    "characters",
    "rarity_chances",
    "claim_list",
    "user_collection",
    "transactions",
    "fair_rolls",
    "user_inventory",
    "cooldowns",
    "shields",
    "heist_log",
    "premium",
    "subscription_access",
    "star_purchases",
    "groups",
    "spawn_events",
    "guess_sessions",
    "guess_streaks",
    "auctions",
    "auction_bids",
    "trade_offers",
    "redeem_codes",
    "code_claims",
    "gift_log",
    "user_achievements",
    "user_streaks",
    "daily_claims",
    "user_prefs",
    "sudo_admins",
    "banned_users",
    "warnings",
    "moderation_cases",
    "audit_logs",
    "activity_log",
    "ai_messages",
    "boost_grants",
    "raffle_rounds",
    "shop_pools",
    "stats_snapshots",
    "schema_version",
    "kv_state",
)
