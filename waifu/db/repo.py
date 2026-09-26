"""The whole repository layer — every SQL access function, in one file.

Consolidated from the former ``waifu/db/repositories/`` package so the
database story stays literally one file pair: ``waifu/db/database.py``
(the engine — two methods, backup/restore) and this file (the queries).

Domains are namespace classes, called exactly as the old modules were::

    from waifu.db.repo import users as user_repo
    await user_repo.upsert(session, user_id=..., username=...)

Shared constants (``SNIPE_WINDOW``, ``HMODE_ORDERS``, ...) and the row
dataclasses (``Owned``, ``ItemDef``, ``PityState``, ...) live at module
level; the dataclasses — and the constants services address through a
domain (``items.ITEMS``, ``moderation.ALL_PERMS``) — are also exposed on
their domain (``collection.Owned``), so annotations and ``items_repo.ITEMS``
keep working unchanged."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, case, delete, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as lite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from waifu.db.models import (
    Achievement,
    ActivityLog,
    AiMessageLog,
    Auction,
    AuctionBid,
    AuditLog,
    BannedUser,
    BoostGrant,
    Character,
    CharacterRequest,
    ClaimChance,
    CodeClaim,
    Cooldown,
    DailyClaim,
    FairRoll,
    GiftLog,
    Group,
    GuessSession,
    GuessStreak,
    HeistLog,
    InventoryItem,
    KvState,
    ModerationCase,
    Ownership,
    Premium,
    RaffleRound,
    RarityChance,
    RedeemCode,
    ScheduledBroadcast,
    Shield,
    ShopPool,
    SpawnEvent,
    StarPurchase,
    StatsSnapshot,
    Streak,
    SubscriptionAccess,
    SudoAdmin,
    TradeOffer,
    Transaction,
    User,
    UserPref,
    Warning,
)
from waifu.enums import AuctionStatus, LedgerReason, Rarity, Role, SubscriptionState, TradeStatus
from waifu.errors import (
    AlreadyClaimed,
    BidTooLow,
    Locked,
    MultipleMatches,
    NotEnoughFunds,
    NotFound,
)
from waifu.settings import Settings, get_settings
from waifu.utils.rng import commit, derive_roll, redeem_code, system_random
from waifu.utils.text import strip_md
from waifu.utils.time import now_utc


@dataclass(slots=True)
class Owned:
    """Flattened (ownership, character) pair — what every UI renderer consumes."""

    character_id: int
    name: str
    anime: str
    rarity_id: int
    rarity: str
    count: int
    price: int
    is_favorite: bool
    is_locked: bool
    image: str
    stat_power: int = 0

    @property
    def tier(self) -> Rarity:
        return Rarity.from_value(self.rarity_id)

    @property
    def value(self) -> int:
        return self.price * max(1, self.count)


@dataclass(slots=True)
class Entry:
    id: int
    user_id: int
    delta: int
    balance_after: int
    reason: str
    duplicate: bool = False


class NotFoundUser(LookupError):
    def __init__(self, user_id: int) -> None:
        super().__init__(f"user {user_id} not found")


@dataclass(frozen=True, slots=True)
class ItemDef:
    """Catalogue entry for the /market (a.k.a. /cshop000000) item shop."""

    key: str
    name: str
    cost: int
    max_stack: int
    desc: str
    charges: int = 1

    @property
    def emoji(self) -> str:
        return self.name.split(" ", 1)[0]

    @property
    def label(self) -> str:
        return self.name.split(" ", 1)[1] if " " in self.name else self.name


@dataclass(slots=True)
class PityState:
    rare: int
    high: int
    celestial: int
    pulls_total: int

    def rare_remaining(self, threshold: int) -> int:
        return max(0, threshold - self.rare)

    def high_remaining(self, threshold: int) -> int:
        return max(0, threshold - self.high)


@dataclass(slots=True)
class StreakOutcome:
    current: int
    best: int
    broke: bool
    multiplier: float


@dataclass(slots=True)
class Spawn:
    id: int
    chat_id: int
    character_id: int
    name: str
    anime: str
    rarity_id: int
    rarity: str
    image: str
    message_id: int | None
    rich_message: bool
    source: str
    expected_name: str
    expires_at: datetime
    hint_used: bool
    status: str = "active"


SNIPE_WINDOW = 180
SNIPE_EXTENSION = 120
MAX_EXTENSIONS = 6
MIN_DURATION = 60 * 5
MAX_DURATION = 60 * 60 * 72
HMODE_ORDERS = {
    "rarity": [Character.rarity_id.desc(), Character.name.asc()],
    "anime": [Character.anime.asc(), Character.rarity_id.desc()],
    "name": [Character.name.asc()],
    "recent": [Ownership.last_obtained.desc()],
    "count": [Ownership.count.desc(), Character.rarity_id.desc()],
    "fav": [Ownership.is_favorite.desc(), Character.rarity_id.desc()],
    "value": [Character.price.desc()],
}
ITEMS: dict[str, ItemDef] = {
    "bomb": ItemDef("bomb", "💣 Bomb", 100000, 1, "Steal a random character from someone's harem"),
    "lucky": ItemDef(
        "lucky", "🎟️ Lucky Ticket", 15000, 5, "+15% rarity chance for your next 5 claims", charges=5
    ),
    "skip": ItemDef("skip", "⏰ Skip Cooldown", 25000, 10, "Clear one cooldown or arm a shield"),
    "magnet": ItemDef(
        "magnet", "💰 Coin Magnet", 10000, 5, "+2,000 bonus coins on your next /daily", charges=1
    ),
    "sshield": ItemDef(
        "sshield", "🔒 Steal Shield", 8000, 5, "Blocks one incoming /steal", charges=1
    ),
    "bshield": ItemDef("bshield", "🛡️ Bomb Shield", 9000, 3, "Blocks one incoming /bomb", charges=1),
    "xp": ItemDef("xp", "⚡ XP Boost", 12000, 5, "2× EXP and coin rewards for 5 claims", charges=5),
}
SPECIAL_COOLDOWNS = {"bomb": 24 * 3600, "steal": 3600, "skip": 600}
EARN_REASONS = ("daily", "work", "quest", "spin", "bonus", "claim", "sell")
PERMISSIONS = (
    "spawn",
    "manage_chars",
    "moderate",
    "economy",
    "broadcast",
    "group_admin",
    "market",
    "view_audit",
)
ALL_PERMS = dict.fromkeys(PERMISSIONS, True)
DEFAULT_SUDO_PERMS = dict.fromkeys(("spawn", "moderate", "group_admin"), True)
TRADE_TTL_SECONDS = 900
_INVITE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
LEADERBOARD_METRICS = {
    "balance": User.balance,
    "level": User.level,
    "exp": User.exp,
    "claims": User.pulls_total,
    "high": User.high_pulls,
    "streak": User.streak_count,
}


class ai:
    """``ai_messages`` access: transcripts, per-character history, retention."""

    @staticmethod
    async def log(
        session: AsyncSession,
        *,
        user_id: int,
        character_id: int | None,
        content: str,
        role: str = "user",
        chat_id: int | None = None,
        tokens_out: int = 0,
        streamed: bool = False,
        flagged: bool = False,
    ) -> AiMessageLog:
        row = AiMessageLog(
            user_id=user_id,
            character_id=character_id,
            chat_id=chat_id,
            role=role,
            content=(content or "")[:4000],
            tokens_out=max(0, int(tokens_out)),
            streamed=streamed,
            flagged=flagged,
        )
        session.add(row)
        await session.flush()
        return row

    @staticmethod
    async def recent(
        session: AsyncSession, *, user_id: int, character_id: int | None = None, limit: int = 10
    ) -> list[AiMessageLog]:
        """The last ``limit`` turns, oldest first — the shape a chat prompt wants."""
        conds = [AiMessageLog.user_id == user_id]
        if character_id is not None:
            conds.append(AiMessageLog.character_id == character_id)
        rows = list(
            (
                await session.execute(
                    select(AiMessageLog).where(*conds).order_by(AiMessageLog.id.desc()).limit(limit)
                )
            ).scalars()
        )
        return list(reversed(rows))

    @staticmethod
    async def all_for(session: AsyncSession, user_id: int) -> list[AiMessageLog]:
        return list(
            (
                await session.execute(
                    select(AiMessageLog)
                    .where(AiMessageLog.user_id == user_id)
                    .order_by(AiMessageLog.id)
                )
            ).scalars()
        )

    @staticmethod
    async def flag(session: AsyncSession, message_id: int, *, flagged: bool = True) -> None:
        from sqlalchemy import update

        await session.execute(
            update(AiMessageLog).where(AiMessageLog.id == message_id).values(flagged=flagged)
        )
        await session.flush()

    @staticmethod
    async def flagged_count(session: AsyncSession, *, since: datetime | None = None) -> int:
        conds = [AiMessageLog.flagged.is_(True)]
        if since is not None:
            conds.append(AiMessageLog.created_at >= since)
        return int(
            (
                await session.execute(select(func.count()).select_from(AiMessageLog).where(*conds))
            ).scalar_one()
            or 0
        )

    @staticmethod
    async def usage_since(session: AsyncSession, *, since: datetime) -> dict[str, int]:
        row = (
            (
                await session.execute(
                    select(
                        func.count(AiMessageLog.id).label("messages"),
                        func.coalesce(func.sum(AiMessageLog.tokens_out), 0).label("tokens"),
                        func.count(func.distinct(AiMessageLog.user_id)).label("users"),
                    ).where(AiMessageLog.created_at >= since)
                )
            )
            .mappings()
            .one()
        )
        return {
            "messages": int(row["messages"]),
            "tokens": int(row["tokens"] or 0),
            "users": int(row["users"]),
        }

    @staticmethod
    async def purge(session: AsyncSession, user_id: int, *, character_id: int | None = None) -> int:
        conds = [AiMessageLog.user_id == user_id]
        if character_id is not None:
            conds.append(AiMessageLog.character_id == character_id)
        result = await session.execute(delete(AiMessageLog).where(*conds))
        return int(result.rowcount or 0)

    @staticmethod
    async def purge_older_than(session: AsyncSession, days: int = 30) -> int:
        cutoff = now_utc().replace(microsecond=0, second=0, minute=0) - __import__(
            "datetime"
        ).timedelta(days=days)
        result = await session.execute(delete(AiMessageLog).where(AiMessageLog.created_at < cutoff))
        return int(result.rowcount or 0)


class auctions:
    """Auctions: /auction, /auctionlist, /mybids, /cancelauction.

    Improvements over Summon-bot:

    * the character is **escrowed** (``is_locked``) for the auction's duration, so it
      can't be sold or gifted mid-bid;
    * a bid inside the final 3 minutes extends the clock (anti-snipe), capped so an
      auction can't run forever;
    * settlement runs under a Postgres advisory lock (see :meth:`Database.job_tx`)
      so two workers can never both pay out the same auction.
    """

    @staticmethod
    async def create(
        session: AsyncSession,
        *,
        seller_id: int,
        character_id: int,
        start_price: int,
        minutes: int = 60,
        reserve_price: int = 0,
        min_increment: int = 1000,
        note: str = "",
    ) -> Auction:
        if start_price < 1:
            raise NotFound("start price must be at least 1 coin")
        duration = max(MIN_DURATION, min(MAX_DURATION, int(minutes) * 60))
        auction = Auction(
            seller_id=seller_id,
            character_id=character_id,
            status=str(AuctionStatus.LIVE),
            start_price=start_price,
            reserve_price=max(0, reserve_price),
            min_increment=max(1, min_increment),
            current_bid=0,
            ends_at=now_utc() + timedelta(seconds=duration),
            note=note[:140],
        )
        session.add(auction)
        await session.flush()
        await session.execute(
            update(Ownership)
            .where(Ownership.user_id == seller_id, Ownership.character_id == character_id)
            .values(is_locked=True)
        )
        return auction

    @staticmethod
    async def attach_message(
        session: AsyncSession, auction_id: int, message_id: int, chat_id: int
    ) -> None:
        await session.execute(
            update(Auction)
            .where(Auction.id == auction_id)
            .values(message_id=message_id, chat_id=chat_id)
        )
        await session.flush()

    @staticmethod
    async def get(session: AsyncSession, auction_id: int) -> Auction | None:
        return await session.get(Auction, auction_id)

    @staticmethod
    async def view(
        session: AsyncSession, auction_id: int
    ) -> tuple[Auction, Character, int, int] | None:
        row = (
            await session.execute(
                select(Auction, Character, Ownership.count, Auction.bids_count)
                .join(Character, Character.id == Auction.character_id)
                .join(
                    Ownership,
                    (Ownership.user_id == Auction.seller_id)
                    & (Ownership.character_id == Auction.character_id),
                    isouter=True,
                )
                .where(Auction.id == auction_id)
            )
        ).first()
        if row is None:
            return None
        auction, char, copies, _bids = row
        count = (
            await session.execute(
                select(func.count(AuctionBid.id)).where(AuctionBid.auction_id == auction_id)
            )
        ).scalar_one()
        return (auction, char, int(copies or 0), int(count))

    @staticmethod
    async def next_minimum(session: AsyncSession, auction_id: int) -> int:
        auction = await session.get(Auction, auction_id)
        if auction is None:
            raise NotFound("auction not found")
        return max(
            int(auction.start_price), int(auction.current_bid or 0) + int(auction.min_increment)
        )

    @staticmethod
    async def bid(
        session: AsyncSession,
        auction_id: int,
        bidder_id: int,
        amount: int,
        *,
        snipe_window: int = SNIPE_WINDOW,
        snipe_extension: int = SNIPE_EXTENSION,
        max_extensions: int = MAX_EXTENSIONS,
    ) -> tuple[tuple[Auction, int | None], bool]:
        """Place a bid.

        The three ``snipe_*`` arguments exist so the *settings* drive the guard: the module
        constants are defaults only, because a knob nobody reads is worse than no knob at all.

        Returns ``((auction, outbid_user_id), extended)`` — the service refunds the
        outbid player's stake in the *same* transaction, and uses ``extended`` to
        tell the chat the clock was pushed (anti-snipe).
        """
        auction = await session.get(Auction, auction_id)
        if auction is None:
            raise NotFound("auction not found")
        if auction.status != str(AuctionStatus.LIVE):
            raise Locked("auction is closed")
        if auction.seller_id == bidder_id:
            raise Locked("you can't bid on your own auction")
        minimum = max(
            int(auction.start_price), int(auction.current_bid or 0) + int(auction.min_increment)
        )
        if amount < minimum:
            raise BidTooLow(minimum)
        previous_top = auction.top_bidder_id if auction.current_bid else None
        result = await session.execute(
            update(Auction)
            .where(
                Auction.id == auction_id,
                Auction.status == str(AuctionStatus.LIVE),
                Auction.current_bid < amount,
            )
            .values(current_bid=amount, top_bidder_id=bidder_id, bids_count=Auction.bids_count + 1)
        )
        if not result.rowcount:
            raise Locked("you were outbid — try a higher amount")
        auction = await session.get(Auction, auction_id)
        assert auction is not None
        remaining = (auction.ends_at - now_utc()).total_seconds()
        extended = False
        if 0 < remaining <= snipe_window and auction.extensions < max_extensions:
            auction.ends_at = auction.ends_at + timedelta(seconds=snipe_extension)
            auction.extensions += 1
            auction.last_extend_at = now_utc()
            extended = True
        session.add(
            AuctionBid(
                auction_id=auction_id, bidder_id=bidder_id, amount=amount, created_at=now_utc()
            )
        )
        await session.flush()
        return (
            (auction, previous_top if previous_top and previous_top != bidder_id else None),
            extended,
        )

    @staticmethod
    async def cancel(
        session: AsyncSession, auction_id: int, actor_id: int, *, force: bool = False
    ) -> Auction:
        """Seller cancels while no bids exist; owner/admin may force-cancel anytime."""
        auction = await session.get(Auction, auction_id)
        if auction is None:
            raise NotFound("auction not found")
        if auction.status != str(AuctionStatus.LIVE):
            raise Locked("already settled")
        if not force and auction.seller_id != actor_id:
            raise Locked("only the seller can cancel")
        if not force and auction.bids_count > 0:
            raise Locked("bids already exist — let it finish (or ask an admin to force-cancel)")
        auction.status = str(AuctionStatus.CANCELLED)
        await session.execute(
            update(Ownership)
            .where(
                Ownership.user_id == auction.seller_id,
                Ownership.character_id == auction.character_id,
            )
            .values(is_locked=False)
        )
        await session.flush()
        return auction

    @staticmethod
    async def settle_candidate(session: AsyncSession) -> Auction | None:
        """Atomically claim one due auction (``FOR UPDATE SKIP LOCKED`` = one worker each)."""
        row = (
            (
                await session.execute(
                    select(Auction)
                    .where(Auction.status == str(AuctionStatus.LIVE), Auction.ends_at <= now_utc())
                    .order_by(Auction.ends_at)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .first()
        )
        return row

    @staticmethod
    async def settle_due(session: AsyncSession) -> list[Auction]:
        return list(
            (
                await session.execute(
                    select(Auction)
                    .where(Auction.status == str(AuctionStatus.LIVE), Auction.ends_at <= now_utc())
                    .order_by(Auction.ends_at)
                )
            ).scalars()
        )

    @staticmethod
    async def close(session: AsyncSession, auction_id: int, *, sold: bool, fee: int = 0) -> Auction:
        auction = await session.get(Auction, auction_id)
        if auction is None:
            raise NotFound("auction vanished")
        auction.status = str(AuctionStatus.SOLD if sold else AuctionStatus.NO_SALE)
        if sold:
            auction.winner_id = auction.top_bidder_id
            auction.sold_price = int(auction.current_bid or 0)
            auction.fee = max(0, fee)
        await session.execute(
            update(Ownership)
            .where(
                Ownership.user_id == auction.seller_id,
                Ownership.character_id == auction.character_id,
            )
            .values(is_locked=False)
        )
        await session.flush()
        return auction

    @staticmethod
    async def refund_bids(
        session: AsyncSession, auction_id: int, *, except_bidder_id: int | None = None
    ) -> list[tuple[int, int, int]]:
        """Return ``(bidder_id, amount, bid_row_id)`` for every stake that must go back.

        One row per *bid*, not per bidder: a player who bid twice and was outbid once
        is owed both stakes back (each bid debited them separately). The bid id goes
        into the refund's idempotency key, so a replay credits neither one twice.
        """
        conds = [AuctionBid.auction_id == auction_id, AuctionBid.is_refunded.is_(False)]
        if except_bidder_id is not None:
            conds.append(AuctionBid.bidder_id != except_bidder_id)
        rows = (
            await session.execute(
                select(AuctionBid).where(*conds).order_by(AuctionBid.created_at.asc())
            )
        ).scalars()
        out: list[tuple[int, int, int]] = []
        for row in rows:
            row.is_refunded = True
            out.append((row.bidder_id, row.amount, row.id))
        await session.flush()
        return out

    @staticmethod
    async def live(
        session: AsyncSession, *, limit: int = 20, offset: int = 0, sort: str = "ends"
    ) -> tuple[list[Auction], int, dict[int, Character]]:
        base = [Auction.status == str(AuctionStatus.LIVE), Auction.ends_at > now_utc()]
        total = (await session.execute(select(func.count(Auction.id)).where(*base))).scalar_one()
        order = Auction.ends_at.asc() if sort == "ends" else Auction.current_bid.desc()
        rows = (
            await session.execute(
                select(Auction, Character)
                .join(Character, Character.id == Auction.character_id)
                .where(*base)
                .order_by(order)
                .limit(limit)
                .offset(offset)
            )
        ).all()
        chars = {c.id: c for _a, c in rows}
        return ([a for a, _c in rows], int(total), chars)

    @staticmethod
    async def mine(
        session: AsyncSession, user_id: int, *, as_seller: bool = True, limit: int = 20
    ) -> list[Auction]:
        column = Auction.seller_id if as_seller else Auction.top_bidder_id
        return list(
            (
                await session.execute(
                    select(Auction)
                    .where(column == user_id)
                    .order_by(Auction.created_at.desc(), Auction.ends_at.desc())
                    .limit(limit)
                )
            ).scalars()
        )

    @staticmethod
    async def leading(session: AsyncSession, user_id: int) -> list[Auction]:
        return list(
            (
                await session.execute(
                    select(Auction)
                    .where(
                        Auction.top_bidder_id == user_id, Auction.status == str(AuctionStatus.LIVE)
                    )
                    .order_by(Auction.ends_at.asc())
                )
            ).scalars()
        )

    @staticmethod
    async def history_of(session: AsyncSession, user_id: int, *, limit: int = 10) -> list[Auction]:
        return list(
            (
                await session.execute(
                    select(Auction)
                    .where(Auction.winner_id == user_id, Auction.status == str(AuctionStatus.SOLD))
                    .order_by(Auction.created_at.desc())
                    .limit(limit)
                )
            ).scalars()
        )

    @staticmethod
    async def bid_log(
        session: AsyncSession, auction_id: int, *, limit: int = 12
    ) -> list[AuctionBid]:
        return list(
            (
                await session.execute(
                    select(AuctionBid)
                    .where(AuctionBid.auction_id == auction_id)
                    .order_by(AuctionBid.amount.desc())
                    .limit(limit)
                )
            ).scalars()
        )

    @staticmethod
    async def stats(session: AsyncSession) -> dict[str, int]:
        row = (
            (
                await session.execute(
                    select(
                        func.count(Auction.id)
                        .filter(Auction.status == str(AuctionStatus.LIVE))
                        .label("live"),
                        func.count(Auction.id)
                        .filter(Auction.status == str(AuctionStatus.SOLD))
                        .label("sold"),
                        func.coalesce(func.sum(Auction.sold_price), 0).label("volume"),
                        func.coalesce(func.sum(Auction.fee), 0).label("fees"),
                    )
                )
            )
            .mappings()
            .one()
        )
        return {
            "live": int(row["live"] or 0),
            "sold": int(row["sold"] or 0),
            "volume": int(row["volume"] or 0),
            "fees": int(row["fees"] or 0),
        }

    @staticmethod
    async def purge(session: AsyncSession, *, older_than_days: int = 120) -> int:
        cutoff = now_utc() - timedelta(days=older_than_days)
        result = await session.execute(
            Auction.__table__.delete().where(
                Auction.status.in_([str(AuctionStatus.SOLD), str(AuctionStatus.CANCELLED)]),
                Auction.created_at < cutoff,
            )
        )
        return int(result.rowcount or 0)


class broadcasts:
    """Queued announcements (``/broadcast at 20:00 …``).

    A scheduled broadcast is a row, not a timer: the jobs loop claims due rows with
    an atomic UPDATE (``sent_at IS NULL`` guard), so two instances — or the loop
    and a manual ``waifu jobs --name broadcasts`` — cannot send the same line
    twice. The same claim pattern the auction settlement uses.
    """

    @staticmethod
    async def schedule(
        session: AsyncSession, *, run_at: datetime, text: str, created_by: int
    ) -> ScheduledBroadcast:
        row = ScheduledBroadcast(run_at=run_at, text=text[:4000], created_by=created_by)
        session.add(row)
        await session.flush()
        return row

    @staticmethod
    async def pending(session: AsyncSession, *, limit: int = 20) -> list[ScheduledBroadcast]:
        rows = (
            (
                await session.execute(
                    select(ScheduledBroadcast)
                    .where(ScheduledBroadcast.sent_at.is_(None))
                    .order_by(ScheduledBroadcast.run_at)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return list(rows)

    @staticmethod
    async def due(session: AsyncSession) -> list[ScheduledBroadcast]:
        """Rows whose moment has come, claimed atomically (one send per row)."""
        result = await session.execute(
            update(ScheduledBroadcast)
            .where(ScheduledBroadcast.sent_at.is_(None), ScheduledBroadcast.run_at <= now_utc())
            .values(sent_at=now_utc())
            .returning(ScheduledBroadcast.id)
        )
        ids = [row[0] for row in result.all()]
        if not ids:
            return []
        rows = (
            (
                await session.execute(
                    select(ScheduledBroadcast).where(ScheduledBroadcast.id.in_(ids))
                )
            )
            .scalars()
            .all()
        )
        return list(rows)

    @staticmethod
    async def cancel_all(session: AsyncSession) -> int:
        """Drop every unsent announcement (sent rows stay as the audit trail)."""
        from sqlalchemy import delete

        result = await session.execute(
            delete(ScheduledBroadcast).where(ScheduledBroadcast.sent_at.is_(None))
        )
        return int(result.rowcount or 0)


class characters:
    """Character catalogue + the two admin-tunable odds tables.

    Summon-bot kept prices in a Python dict in ``config.py`` and the drop chances in
    a table, so the two could disagree (a rarity could be unbuyable or unfreeable).
    Here both live in the DB, are cached in Redis, and are edited only via /chance,
    /chancelist, /setclaim and /claimlist.
    """

    @staticmethod
    def to_display(rarity: Rarity | int) -> str:
        tier = rarity if isinstance(rarity, Rarity) else Rarity.from_value(rarity)
        return tier.badge

    @staticmethod
    async def get(session: AsyncSession, character_id: int) -> Character | None:
        return await session.get(Character, character_id)

    @staticmethod
    async def get_many(session: AsyncSession, ids: list[int]) -> dict[int, Character]:
        if not ids:
            return {}
        rows = (await session.execute(select(Character).where(Character.id.in_(ids)))).scalars()
        return {c.id: c for c in rows}

    @staticmethod
    async def find_one(
        session: AsyncSession, query: str, *, rarity_id: int | None = None
    ) -> Character:
        """Resolve ``/check 123``, ``/check zero-two``, ``/check darling:zero``.

        Exact name wins, then exact prefix, then fuzzy; ties break toward rarer.
        """
        raw = strip_md(query).strip()
        if not raw:
            raise NotFound("give me a name or an id")
        conds = []
        if rarity_id is not None:
            conds.append(Character.rarity_id == rarity_id)
        if raw.isdigit():
            found = await session.get(Character, int(raw))
            if found and (rarity_id is None or found.rarity_id == rarity_id):
                return found
        if ":" in raw:
            series, _, name = raw.partition(":")
            stmt = select(Character).where(
                Character.anime.ilike(f"%{series.strip()}%"),
                Character.name.ilike(f"%{name.strip()}%"),
                *conds,
            )
        else:
            stmt = (
                select(Character)
                .where(
                    or_(
                        func.lower(Character.name) == raw.lower(),
                        Character.name.ilike(f"{raw}%"),
                        Character.name.ilike(f"%{raw}%"),
                    ),
                    *conds,
                )
                .order_by(
                    case((func.lower(Character.name) == raw.lower(), 0), else_=1),
                    case((Character.name.ilike(f"{raw}%"), 0), else_=1),
                    Character.rarity_id.desc(),
                )
            )
        found = (await session.execute(stmt.limit(1))).scalar_one_or_none()
        if found is None:
            raise NotFound(f"no character matches “{raw[:60]}”")
        return found

    @staticmethod
    async def search(
        session: AsyncSession,
        query: str,
        *,
        rarity_id: int | None = None,
        anime: str | None = None,
        limit: int = 20,
        offset: int = 0,
        active_only: bool = True,
    ) -> tuple[list[Character], int]:
        conds = []
        if query:
            like = f"%{query}%"
            conds.append(
                or_(
                    Character.name.ilike(like),
                    Character.anime.ilike(like),
                    Character.tags.ilike(like),
                )
            )
        if rarity_id is not None:
            conds.append(Character.rarity_id == rarity_id)
        if anime:
            conds.append(Character.anime.ilike(f"%{anime}%"))
        if active_only:
            conds.append(Character.is_active.is_(True))
        where = tuple(conds)
        total = (
            await session.execute(
                select(func.count(Character.id)).where(*where)
                if where
                else select(func.count(Character.id))
            )
        ).scalar_one()
        stmt = (
            select(Character)
            .where(*where)
            .order_by(Character.rarity_id.desc(), Character.name.asc())
            .limit(limit)
            .offset(offset)
        )
        return (list((await session.execute(stmt)).scalars()), int(total))

    @staticmethod
    async def by_name(session: AsyncSession, name: str, anime: str = "") -> Character | None:
        conds = [func.lower(Character.name) == name.strip().lower()]
        if anime:
            conds.append(func.lower(Character.anime) == anime.strip().lower())
        return (
            await session.execute(select(Character).where(*conds).limit(1))
        ).scalar_one_or_none()

    @staticmethod
    async def rarity_chances(
        session: AsyncSession, *, enabled_only: bool = True
    ) -> list[tuple[Rarity, float]]:
        """``[(Rarity.COMMON, 62.0), …]`` — percentages from /chance (Summon-bot parity)."""
        stmt = select(RarityChance.rarity_id, RarityChance.chance)
        if enabled_only:
            stmt = stmt.where(RarityChance.is_enabled.is_(True))
        rows = (await session.execute(stmt.order_by(RarityChance.rarity_id))).all()
        out: list[tuple[Rarity, float]] = []
        for value, chance in rows:
            try:
                tier = Rarity(int(value))
            except ValueError:
                continue
            if float(chance) > 0:
                out.append((tier, float(chance)))
        return out

    @staticmethod
    async def set_rarity_chance(session: AsyncSession, rarity: Rarity, chance: float) -> None:
        chance = max(0.0, min(100.0, float(chance)))
        values = {
            "rarity_id": int(rarity),
            "rarity_name": characters.to_display(rarity),
            "chance": chance,
            "min_price": rarity.base_price,
            "is_enabled": chance > 0,
        }
        existing = (
            await session.execute(select(RarityChance).where(RarityChance.rarity_id == int(rarity)))
        ).scalar_one_or_none()
        if existing is None:
            session.add(RarityChance(**values))
        else:
            existing.chance = chance
            existing.rarity_name = values["rarity_name"]
            existing.is_enabled = values["is_enabled"]
        await session.flush()

    @staticmethod
    async def claim_chances(
        session: AsyncSession, *, enabled_only: bool = True
    ) -> list[tuple[Rarity, float]]:
        stmt = select(ClaimChance.rarity_id, ClaimChance.chance)
        if enabled_only:
            stmt = stmt.where(ClaimChance.is_enabled.is_(True))
        rows = (await session.execute(stmt.order_by(ClaimChance.rarity_id))).all()
        out: list[tuple[Rarity, float]] = []
        for value, chance in rows:
            try:
                tier = Rarity(int(value))
            except ValueError:
                continue
            if float(chance) > 0:
                out.append((tier, float(chance)))
        return out

    @staticmethod
    async def set_claim_chance(session: AsyncSession, rarity: Rarity, chance: float) -> None:
        chance = max(0.0, min(100.0, float(chance)))
        existing = (
            await session.execute(select(ClaimChance).where(ClaimChance.rarity_id == int(rarity)))
        ).scalar_one_or_none()
        if existing is None:
            session.add(
                ClaimChance(
                    rarity_id=int(rarity),
                    rarity_name=characters.to_display(rarity),
                    chance=chance,
                    is_enabled=chance > 0,
                )
            )
        else:
            existing.chance = chance
            existing.rarity_name = characters.to_display(rarity)
            existing.is_enabled = chance > 0
        await session.flush()

    @staticmethod
    async def normalised_odds(
        session: AsyncSession, *, claim: bool = False
    ) -> list[tuple[Rarity, float]]:
        """Percentages renormalised to sum 100 (players must see honest odds)."""
        table = await (
            characters.claim_chances(session) if claim else characters.rarity_chances(session)
        )
        if not table:
            total = sum(r.weight for r in Rarity)
            return [(r, r.weight / total * 100) for r in Rarity]
        total = sum((c for _r, c in table)) or 1.0
        return [(r, c / total * 100.0) for r, c in table]

    @staticmethod
    async def by_rarity(
        session: AsyncSession, rarity_id: int, *, active_only: bool = True, limit: int = 500
    ) -> list[Character]:
        conds = [Character.rarity_id == int(rarity_id)]
        if active_only:
            conds.append(Character.is_active.is_(True))
        return list(
            (
                await session.execute(
                    select(Character).where(*conds).order_by(Character.name).limit(limit)
                )
            ).scalars()
        )

    @staticmethod
    async def random_of_rarity(
        session: AsyncSession, rarity_id: int, *, banner_only: bool = False
    ) -> Character | None:
        """Random character inside a tier; rate-up (banner) chars are over-weighted."""

        async def _pick(with_banner_filter: bool) -> list[Character]:
            conds = [Character.is_active.is_(True), Character.rarity_id == int(rarity_id)]
            if with_banner_filter:
                conds.append(Character.banner_weight > 1.0)
            return list(
                (await session.execute(select(Character).where(*conds).limit(800))).scalars()
            )

        rows = await _pick(banner_only)
        if not rows and banner_only:
            rows = await _pick(False)
        if not rows:
            return None
        weights = [max(1.0, float(c.banner_weight or 1.0)) for c in rows]
        return system_random.choices(rows, weights=weights, k=1)[0]

    @staticmethod
    async def random_any(session: AsyncSession) -> Character | None:
        """Uniform pick from the whole catalogue (manual /spawn parity with Summon-bot)."""
        row = (
            await session.execute(
                select(Character)
                .where(Character.is_active.is_(True))
                .order_by(func.random())
                .limit(1)
            )
        ).scalar_one_or_none()
        return row

    @staticmethod
    async def top_owned(session: AsyncSession, *, limit: int = 10) -> list[tuple[Character, int]]:
        rows = (
            await session.execute(
                select(Character, func.count(Ownership.user_id).label("holders"))
                .join(Ownership, Ownership.character_id == Character.id)
                .group_by(Character.id)
                .order_by(func.count(Ownership.user_id).desc())
                .limit(limit)
            )
        ).all()
        return [(r[0], int(r[1])) for r in rows]

    @staticmethod
    async def rarity_distribution(session: AsyncSession) -> dict[int, int]:
        rows = (
            await session.execute(
                select(Character.rarity_id, func.count(Character.id))
                .where(Character.is_active.is_(True))
                .group_by(Character.rarity_id)
            )
        ).all()
        return {int(r): int(c) for r, c in rows}

    @staticmethod
    async def next_free_id(session: AsyncSession) -> int:
        """The id a *new* row should take — the reference bot's ``new_upload_id`` rule.

        Summon-bot numbered its roster by hand (``01``, ``02``, ``03``…) and filled the lowest
        gap: those numbers are what admins quote in ``/delchar``, in captions, and in the log
        channel a database was rebuilt from, so a deleted ``07`` belongs to the next upload
        rather than being skipped forever. Autoincrement cannot do that, so ``/upload`` asks
        here and passes the answer to :func:`create_or_update`.
        """
        count = int(
            (await session.execute(select(func.count()).select_from(Character.__table__))).scalar()
            or 0
        )
        top = int((await session.execute(select(func.max(Character.id)))).scalar() or 0)
        if top == count:
            return top + 1
        used = {int(value) for value in (await session.execute(select(Character.id))).scalars()}
        candidate = 1
        while candidate in used:
            candidate += 1
        return candidate

    @staticmethod
    async def create_or_update(
        session: AsyncSession,
        *,
        name: str,
        anime: str = "",
        rarity: Rarity = Rarity.COMMON,
        price: int | None = None,
        assign_id: int | None = None,
        image_url: str = "",
        photo_file_id: str = "",
        video_url: str = "",
        video_file_id: str = "",
        live_photo_file_id: str = "",
        description: str = "",
        voice_line: str = "",
        persona: str = "",
        tags: str = "",
        stat_power: int | None = None,
        banner_weight: float = 1.0,
    ) -> tuple[Character, bool]:
        """``/addchar`` and ``/updatechar`` share this; returns ``(character, created)``."""
        name, anime = (strip_md(name)[:96], strip_md(anime)[:128])
        existing = await characters.by_name(session, name, anime)
        created = existing is None
        char = existing or Character(
            name=name, anime=anime, rarity=characters.to_display(rarity), rarity_id=int(rarity)
        )
        char.rarity = characters.to_display(rarity)
        char.rarity_id = int(rarity)
        if price is not None:
            char.price = int(price)
        elif created:
            char.price = int(rarity.base_price)
        if stat_power is not None:
            char.stat_power = int(stat_power)
        elif created:
            char.stat_power = 10 * int(rarity)
        for field, value in (
            ("image_url", image_url),
            ("photo_file_id", photo_file_id),
            ("video_url", video_url),
            ("video_file_id", video_file_id),
            ("live_photo_file_id", live_photo_file_id),
            ("description", description),
            ("voice_line", voice_line),
            ("persona", persona),
            ("tags", tags[:255]),
        ):
            if value:
                setattr(char, field, value)
        char.banner_weight = banner_weight
        if created and assign_id is not None:
            char.id = int(assign_id)
        if created:
            session.add(char)
        await session.flush()
        return (char, created)

    @staticmethod
    async def attach_media(
        session: AsyncSession,
        character_id: int,
        *,
        photo_file_id: str = "",
        video_file_id: str = "",
        live_photo_file_id: str = "",
        sticker_file_id: str = "",
        image_url: str = "",
    ) -> None:
        values = {
            k: v
            for k, v in {
                "photo_file_id": photo_file_id,
                "video_file_id": video_file_id,
                "live_photo_file_id": live_photo_file_id,
                "sticker_file_id": sticker_file_id,
                "image_url": image_url,
            }.items()
            if v
        }
        if not values:
            return
        await session.execute(
            update(Character).where(Character.id == character_id).values(**values)
        )
        await session.flush()

    @staticmethod
    async def set_active(session: AsyncSession, character_id: int, active: bool) -> None:
        await session.execute(
            update(Character).where(Character.id == character_id).values(is_active=active)
        )

    @staticmethod
    async def set_banner(
        session: AsyncSession, character_ids: list[int], weight: float = 3.0
    ) -> int:
        await session.execute(
            update(Character).where(Character.banner_weight != 1.0).values(banner_weight=1.0)
        )
        if not character_ids:
            return 0
        result = await session.execute(
            update(Character).where(Character.id.in_(character_ids)).values(banner_weight=weight)
        )
        return int(result.rowcount or 0)

    @staticmethod
    async def delete_character(session: AsyncSession, character_id: int) -> int:
        """Delete a character and drop it from every collection (admin-only tool).

        Ownership rows go with it via FK CASCADE, but they are removed explicitly so
        the statement also works on databases created before the FK existed.
        """
        await session.execute(delete(Ownership).where(Ownership.character_id == character_id))
        await session.execute(delete(ShopPool).where(ShopPool.user_id == 0))
        result = await session.execute(delete(Character).where(Character.id == character_id))
        return int(result.rowcount or 0)

    @staticmethod
    async def totals(session: AsyncSession) -> dict[str, int]:
        """Roster size, active count, distinct series and total owned copies.

        Two statements rather than one join: ``characters LEFT JOIN ownership`` multiplies
        the character rows by their copies, so ``count(*)`` would report "copies" as the
        roster size — and ``/chars`` would advertise a roster a hundred times bigger than it
        is. Aggregating each side separately is also index-only on Postgres.
        """
        row = (
            (
                await session.execute(
                    select(
                        func.count(Character.id).label("total"),
                        func.coalesce(
                            func.sum(case((Character.is_active.is_(True), 1), else_=0)), 0
                        ).label("active"),
                        func.count(func.distinct(Character.anime)).label("series"),
                    )
                )
            )
            .mappings()
            .one()
        )
        copies = (
            await session.execute(select(func.coalesce(func.sum(Ownership.count), 0)))
        ).scalar_one()
        return {
            "characters": int(row["total"]),
            "active": int(row["active"]),
            "series": int(row["series"] or 0),
            "copies": int(copies or 0),
        }

    @staticmethod
    async def price_for(session: AsyncSession, char: Character) -> int:
        """Effective shop price; falls back to the rarity's base price if unset."""
        if char.price:
            return int(char.price)
        return int(Rarity.from_value(char.rarity_id).base_price)

    @staticmethod
    async def save_pool(session: AsyncSession, user_id: int, rarity: str, ids: list[int]) -> None:
        row = (
            await session.execute(
                select(ShopPool).where(ShopPool.user_id == user_id, ShopPool.rarity == rarity)
            )
        ).scalar_one_or_none()
        if row is None:
            session.add(
                ShopPool(user_id=user_id, rarity=rarity, characters={"ids": ids}, refreshes_used=1)
            )
        else:
            row.characters = {"ids": ids}
            row.refreshes_used += 1
        await session.flush()

    @staticmethod
    async def load_pool(session: AsyncSession, user_id: int, rarity: str) -> list[int] | None:
        row = (
            await session.execute(
                select(ShopPool).where(ShopPool.user_id == user_id, ShopPool.rarity == rarity)
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        return list((row.characters or {}).get("ids", []))

    @staticmethod
    async def pending_requests(session: AsyncSession, *, limit: int = 30) -> list[Any]:
        rows = (
            (
                await session.execute(
                    select(CharacterRequest)
                    .where(CharacterRequest.status == "pending")
                    .order_by(CharacterRequest.created_at.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return list(rows)

    @staticmethod
    async def find_pending_request(session: AsyncSession, name: str, series: str) -> Any | None:
        """A pending request for the same character (case-insensitive) — one ask, not ten."""
        norm = name.strip().casefold()
        if not norm:
            return None
        rows = (
            (
                await session.execute(
                    select(CharacterRequest).where(CharacterRequest.status == "pending")
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            if (
                row.name.strip().casefold() == norm
                and (row.series or "").casefold() == (series or "").casefold()
            ):
                return row
        return None

    @staticmethod
    async def submit_request(
        session: AsyncSession, *, name: str, series: str, requester_id: int, note: str = ""
    ) -> Any:
        row = CharacterRequest(
            name=name[:96], series=series[:96], requester_id=requester_id, note=note[:200]
        )
        session.add(row)
        await session.flush()
        return row

    @staticmethod
    async def decide_request(
        session: AsyncSession, request_id: int, *, decision: str, decided_by: int
    ) -> Any:
        """Approve / decline one request. ``decision`` must be approved|declined."""
        from waifu.utils.time import now_utc

        row = await session.get(CharacterRequest, request_id)
        if row is None or row.status != "pending":
            raise NotFound("that request is already decided (or gone)")
        row.status = decision
        row.decided_at = now_utc()
        row.decided_by = decided_by
        await session.flush()
        return row


class collection:
    """Collection ("harem") writes — the /summon claim, /gift, /sell, /fav, /hmode path.

    Every mutation is a conditional statement with a checked ``rowcount``:

    * ``grant`` → ``INSERT … ON CONFLICT DO UPDATE SET count = count + 1``
    * ``consume`` → ``UPDATE … WHERE count >= n AND is_locked IS FALSE``

    so Summon-bot's two classic bugs (self-buy in the market, and losing/duplicating
    copies when two handlers wrote the same row) cannot happen here.
    """

    Owned = Owned

    @staticmethod
    def _own(o: Ownership, c: Character) -> Owned:
        return Owned(
            character_id=c.id,
            name=c.name,
            anime=c.anime,
            rarity_id=int(c.rarity_id),
            rarity=c.rarity or Rarity.from_value(c.rarity_id).badge,
            count=int(o.count),
            price=int(c.price or 0),
            is_favorite=bool(o.is_favorite),
            is_locked=bool(o.is_locked),
            image=c.image_ref(),
            stat_power=int(c.stat_power or 0),
        )

    @staticmethod
    async def grant(
        session: AsyncSession,
        user_id: int,
        character_id: int,
        *,
        count: int = 1,
        source: str = "claim",
    ) -> Ownership:
        """Add copies; creates the row on first contact. Idempotent-friendly upsert."""
        dialect = session.bind.dialect.name if session.bind else "postgresql"
        insert = (pg_insert if dialect == "postgresql" else lite_insert)(Ownership)
        await session.execute(
            insert.values(
                user_id=user_id,
                character_id=character_id,
                count=count,
                source=source,
                first_obtained=now_utc(),
                last_obtained=now_utc(),
            ).on_conflict_do_update(
                index_elements=[Ownership.user_id, Ownership.character_id],
                set_={"count": Ownership.count + count, "last_obtained": now_utc()},
            )
        )
        row = (
            await session.execute(
                select(Ownership).where(
                    Ownership.user_id == user_id, Ownership.character_id == character_id
                )
            )
        ).scalar_one()
        await session.flush()
        return row

    @staticmethod
    async def consume(
        session: AsyncSession,
        user_id: int,
        character_id: int,
        *,
        count: int = 1,
        releasing: bool = False,
    ) -> None:
        """Remove copies. Raises ``NotFound``/``Locked`` with player-readable reasons.

        ``releasing=True`` is for escrow settlement (an auction that just closed, a trade
        both sides accepted). Those rows are locked *by* the escrow, so refusing to move
        them here would strand a character in an auction that can never complete — the
        coins are already escrowed and the item would exist in neither account. Clearing
        the flag in the same UPDATE also stops the buyer inheriting a copy they cannot
        sell, which a seller-only unlock would do.
        """
        conditions = [
            Ownership.user_id == user_id,
            Ownership.character_id == character_id,
            Ownership.count >= count,
        ]
        if not releasing:
            conditions.append(Ownership.is_locked.is_(False))
        values: dict[str, object] = {"count": Ownership.count - count, "last_obtained": now_utc()}
        if releasing:
            values["is_locked"] = False
        result = await session.execute(update(Ownership).where(*conditions).values(**values))
        if not result.rowcount:
            row = (
                await session.execute(
                    select(Ownership).where(
                        Ownership.user_id == user_id, Ownership.character_id == character_id
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                raise NotFound("you don't have that character")
            if row.is_locked:
                raise Locked("that character is locked — /sell unlock it first")
            raise NotFound(f"you only have {row.count} copy(ies) of that")
        remaining = (
            await session.execute(
                select(Ownership.count).where(
                    Ownership.user_id == user_id, Ownership.character_id == character_id
                )
            )
        ).scalar_one()
        if int(remaining) <= 0:
            await session.execute(
                Ownership.__table__.delete().where(
                    Ownership.user_id == user_id, Ownership.character_id == character_id
                )
            )
        await session.flush()

    @staticmethod
    async def random_owned(
        session: AsyncSession, user_id: int, *, min_rarity: int = 1, include_locked: bool = False
    ) -> tuple[Character, int] | None:
        """A random character the player actually holds, with the copy count.

        ``/bomb`` uses this (``ORDER BY RANDOM() LIMIT 1`` over ``user_collection`` in the reference),
        and so does every "steal something" effect. The join is on purpose: the caller needs the
        character row to name it in the receipt, and a second query there is a TOCTOU window in which
        the copy can be sold out from under the loot.

        ``include_locked`` defaults to False: the reference looted from ``user_collection`` with no
        lock check, so a /bomb could take a favourite a player had pinned. Here a locked copy is
        simply not a candidate, and if that leaves nothing the target is "safe".
        """
        row = (
            await session.execute(
                select(Ownership, Character)
                .join(Character, Character.id == Ownership.character_id)
                .where(
                    Ownership.user_id == user_id,
                    Ownership.count > 0,
                    Character.rarity_id >= int(min_rarity),
                    Character.is_active.is_(True),
                    *([] if include_locked else [Ownership.is_locked.is_(False)]),
                )
                .order_by(func.random())
                .limit(1)
            )
        ).first()
        if row is None:
            return None
        return (row[1], int(row[0].count))

    @staticmethod
    async def has_count(session: AsyncSession, user_id: int, character_id: int) -> int:
        value = (
            await session.execute(
                select(Ownership.count).where(
                    Ownership.user_id == user_id, Ownership.character_id == character_id
                )
            )
        ).scalar_one_or_none()
        return int(value or 0)

    @staticmethod
    async def owned_row(session: AsyncSession, user_id: int, character_id: int) -> Owned | None:
        row = (
            await session.execute(
                select(Ownership, Character)
                .join(Character, Character.id == Ownership.character_id)
                .where(Ownership.user_id == user_id, Ownership.character_id == character_id)
            )
        ).first()
        return collection._own(row[0], row[1]) if row else None

    @staticmethod
    async def set_flag(
        session: AsyncSession, user_id: int, character_id: int, flag: str, value: bool
    ) -> None:
        """``is_favorite`` (single) / ``is_locked`` (per copy) — /fav and /sell lock."""
        if flag not in {"is_favorite", "is_locked"}:
            raise ValueError(f"unsupported flag {flag!r}")
        if flag == "is_favorite" and value:
            await session.execute(
                update(Ownership)
                .where(Ownership.user_id == user_id, Ownership.is_favorite.is_(True))
                .values(is_favorite=False)
            )
        await session.execute(
            update(Ownership)
            .where(Ownership.user_id == user_id, Ownership.character_id == character_id)
            .values(**{flag: value})
        )
        await session.flush()

    @staticmethod
    async def list_owned(
        session: AsyncSession,
        user_id: int,
        *,
        hmode: str = "rarity",
        rarity_id: int | None = None,
        query: str = "",
        dupes_only: bool = False,
        page_size: int = 8,
        page: int = 0,
    ) -> tuple[list[Owned], int]:
        conds = [Ownership.user_id == user_id, Ownership.count > 0]
        if rarity_id is not None:
            conds.append(Character.rarity_id == int(rarity_id))
        if dupes_only:
            conds.append(Ownership.count > 1)
        if query:
            like = f"%{query}%"
            conds.append(or_(Character.name.ilike(like), Character.anime.ilike(like)))
        total = (
            await session.execute(
                select(func.count(Ownership.id))
                .select_from(Ownership)
                .join(Character, Character.id == Ownership.character_id)
                .where(*conds)
            )
        ).scalar_one()
        rows = (
            await session.execute(
                select(Ownership, Character)
                .join(Character, Character.id == Ownership.character_id)
                .where(*conds)
                .order_by(*HMODE_ORDERS.get(hmode, HMODE_ORDERS["rarity"]))
                .limit(page_size)
                .offset(page * page_size)
            )
        ).all()
        return ([collection._own(o, c) for o, c in rows], int(total))

    @staticmethod
    async def summary(session: AsyncSession, user_id: int) -> dict[str, int]:
        row = (
            (
                await session.execute(
                    select(
                        func.count(func.distinct(Ownership.character_id)).label("unique"),
                        func.coalesce(func.sum(Ownership.count), 0).label("total"),
                        func.coalesce(func.sum(Character.price * Ownership.count), 0).label(
                            "value"
                        ),
                        func.coalesce(
                            func.sum(
                                case(
                                    (Character.rarity_id >= int(Rarity.LEGENDARY), Ownership.count),
                                    else_=0,
                                )
                            ),
                            0,
                        ).label("high"),
                    )
                    .select_from(Ownership)
                    .join(Character, Character.id == Ownership.character_id)
                    .where(Ownership.user_id == user_id, Ownership.count > 0)
                )
            )
            .mappings()
            .one()
        )
        return {
            "unique": int(row["unique"] or 0),
            "total": int(row["total"] or 0),
            "value": int(row["value"] or 0),
            "high_tier": int(row["high"] or 0),
        }

    @staticmethod
    async def per_rarity(session: AsyncSession, user_id: int) -> dict[int, int]:
        rows = (
            await session.execute(
                select(
                    Character.rarity_id,
                    func.count(func.distinct(Ownership.character_id)),
                    func.coalesce(func.sum(Ownership.count), 0),
                )
                .select_from(Ownership)
                .join(Character, Character.id == Ownership.character_id)
                .where(Ownership.user_id == user_id, Ownership.count > 0)
                .group_by(Character.rarity_id)
                .order_by(Character.rarity_id.desc())
            )
        ).all()
        return {int(r): int(u) for r, u, _t in rows}

    @staticmethod
    async def favourite(session: AsyncSession, user_id: int) -> Owned | None:
        row = (
            await session.execute(
                select(Ownership, Character)
                .join(Character, Character.id == Ownership.character_id)
                .where(Ownership.user_id == user_id, Ownership.is_favorite.is_(True))
                .limit(1)
            )
        ).first()
        return collection._own(row[0], row[1]) if row else None

    @staticmethod
    async def best(session: AsyncSession, user_id: int) -> Owned | None:
        row = (
            await session.execute(
                select(Ownership, Character)
                .join(Character, Character.id == Ownership.character_id)
                .where(Ownership.user_id == user_id, Ownership.count > 0)
                .order_by(
                    Character.rarity_id.desc(), Character.price.desc(), Ownership.count.desc()
                )
                .limit(1)
            )
        ).first()
        return collection._own(row[0], row[1]) if row else None

    @staticmethod
    async def rarest_owned(
        session: AsyncSession, user_id: int, *, min_rarity_id: int = 1
    ) -> list[Owned]:
        rows = (
            await session.execute(
                select(Ownership, Character)
                .join(Character, Character.id == Ownership.character_id)
                .where(
                    Ownership.user_id == user_id,
                    Ownership.count > 0,
                    Character.rarity_id >= int(min_rarity_id),
                )
                .order_by(Character.rarity_id.desc(), Character.price.desc())
                .limit(6)
            )
        ).all()
        return [collection._own(o, c) for o, c in rows]

    @staticmethod
    async def sellable(
        session: AsyncSession, user_id: int, *, rarity_id: int | None = None, limit: int = 40
    ) -> list[Owned]:
        conds = [Ownership.user_id == user_id, Ownership.count > 0, Ownership.is_locked.is_(False)]
        if rarity_id is not None:
            conds.append(Character.rarity_id == int(rarity_id))
        rows = (
            await session.execute(
                select(Ownership, Character)
                .join(Character, Character.id == Ownership.character_id)
                .where(*conds)
                .order_by(Character.price.desc(), Ownership.count.desc())
                .limit(limit)
            )
        ).all()
        return [collection._own(o, c) for o, c in rows]

    @staticmethod
    async def locked_ids(session: AsyncSession, user_id: int) -> set[int]:
        rows = (
            await session.execute(
                select(Ownership.character_id).where(
                    Ownership.user_id == user_id, Ownership.is_locked.is_(True)
                )
            )
        ).scalars()
        return {int(r) for r in rows}

    @staticmethod
    async def collection_value(session: AsyncSession, user_id: int) -> int:
        value = (
            await session.execute(
                select(func.coalesce(func.sum(Character.price * Ownership.count), 0))
                .select_from(Ownership)
                .join(Character, Character.id == Ownership.character_id)
                .where(Ownership.user_id == user_id)
            )
        ).scalar_one()
        return int(value or 0)

    @staticmethod
    async def top_collectors(session: AsyncSession, *, limit: int = 15) -> list[tuple[User, int]]:
        rows = (
            await session.execute(
                select(User, func.count(func.distinct(Ownership.character_id)).label("unique"))
                .join(Ownership, Ownership.user_id == User.id)
                .where(Ownership.count > 0, User.banned.is_(False))
                .group_by(User.id)
                .order_by(func.count(func.distinct(Ownership.character_id)).desc())
                .limit(limit)
            )
        ).all()
        return [(r[0], int(r[1])) for r in rows]

    @staticmethod
    async def holders_of(
        session: AsyncSession, character_id: int, *, limit: int = 50
    ) -> list[tuple[User, int]]:
        rows = (
            await session.execute(
                select(User, Ownership.count)
                .join(Ownership, Ownership.user_id == User.id)
                .where(Ownership.character_id == character_id, Ownership.count > 0)
                .order_by(Ownership.count.desc())
                .limit(limit)
            )
        ).all()
        return [(r[0], int(r[1])) for r in rows]

    @staticmethod
    async def sweep_zero_rows(session: AsyncSession) -> int:
        result = await session.execute(Ownership.__table__.delete().where(Ownership.count <= 0))
        return int(result.rowcount or 0)


class economy:
    """Money: the only module permitted to write ``users.balance``.

    Rules that make this safe under concurrency (Summon-bot had none of them):

    * ``credit``/``debit`` are single statements. ``debit`` carries ``balance >= amount``
      **in the WHERE clause**, so overdraft is impossible even with two parallel calls.
    * Every movement writes a ``transactions`` row (who, how much, resulting balance,
      why, idempotency key) → disputes are a SELECT.
    * ``idempotency_key`` has a UNIQUE index, so a retried webhook delivery or a
      double-tapped /daily returns the original result instead of paying twice.
    """

    Entry = Entry
    NotFoundUser = NotFoundUser

    @staticmethod
    async def balance(session: AsyncSession, user_id: int) -> int:
        return int(
            (
                await session.execute(select(User.balance).where(User.id == user_id))
            ).scalar_one_or_none()
            or 0
        )

    @staticmethod
    async def _record(
        session: AsyncSession,
        *,
        user_id: int,
        delta: int,
        balance_after: int,
        reason: LedgerReason | str,
        reference: str,
        counterparty: int | None,
        meta: dict | None,
    ) -> Entry:
        row = Transaction(
            user_id=user_id,
            delta=delta,
            balance_after=balance_after,
            reason=str(reason),
            reference=reference[:96],
            counterparty=counterparty,
            meta=meta or {},
            created_at=now_utc(),
        )
        session.add(row)
        await session.flush()
        return Entry(row.id, user_id, delta, balance_after, str(reason))

    @staticmethod
    async def credit(
        session: AsyncSession,
        user_id: int,
        amount: int,
        reason: LedgerReason | str,
        *,
        reference: str = "",
        counterparty: int | None = None,
        meta: dict | None = None,
        idempotency_key: str | None = None,
    ) -> Entry:
        """Add coins. With an idempotency key, a repeat call is a no-op returning the first entry."""
        if amount <= 0:
            raise ValueError(f"credit needs a positive amount, got {amount}")
        if idempotency_key:
            existing = (
                await session.execute(
                    select(Transaction).where(Transaction.idempotency_key == idempotency_key)
                )
            ).scalar_one_or_none()
            if existing:
                return Entry(
                    existing.id,
                    existing.user_id,
                    existing.delta,
                    existing.balance_after,
                    existing.reason,
                    duplicate=True,
                )
            if await economy.get_user_or_raise(session, user_id) is None:
                raise NotFoundUser(user_id)
        result = await session.execute(
            update(User)
            .where(User.id == user_id)
            .values(balance=User.balance + amount)
            .returning(User.balance)
        )
        after = result.scalar_one_or_none()
        if after is None:
            raise NotFoundUser(user_id)
        entry = await economy._record(
            session,
            user_id=user_id,
            delta=amount,
            balance_after=int(after),
            reason=reason,
            reference=reference,
            counterparty=counterparty,
            meta=meta,
        )
        if idempotency_key:
            await session.execute(
                update(Transaction)
                .where(Transaction.id == entry.id)
                .values(idempotency_key=idempotency_key)
            )
        return entry

    @staticmethod
    async def debit(
        session: AsyncSession,
        user_id: int,
        amount: int,
        reason: LedgerReason | str,
        *,
        reference: str = "",
        counterparty: int | None = None,
        meta: dict | None = None,
        idempotency_key: str | None = None,
    ) -> Entry:
        if amount < 0:
            raise ValueError(f"debit amount must be >= 0, got {amount}")
        if amount == 0:
            return Entry(0, user_id, 0, await economy.balance(session, user_id), str(reason))
        if (
            idempotency_key
            and (
                await session.execute(
                    select(Transaction.id).where(Transaction.idempotency_key == idempotency_key)
                )
            ).scalar_one_or_none()
        ):
            raise AlreadyClaimed("that charge was already applied")
        result = await session.execute(
            update(User)
            .where(User.id == user_id, User.balance >= amount)
            .values(balance=User.balance - amount)
            .returning(User.balance)
        )
        after = result.scalar_one_or_none()
        if after is None:
            raise NotEnoughFunds(have=await economy.balance(session, user_id), needed=amount)
        entry = await economy._record(
            session,
            user_id=user_id,
            delta=-amount,
            balance_after=int(after),
            reason=reason,
            reference=reference,
            counterparty=counterparty,
            meta=meta,
        )
        if idempotency_key:
            await session.execute(
                update(Transaction)
                .where(Transaction.id == entry.id)
                .values(idempotency_key=idempotency_key)
            )
        return entry

    @staticmethod
    async def transfer(
        session: AsyncSession,
        *,
        sender_id: int,
        receiver_id: int,
        amount: int,
        reason: LedgerReason | str = LedgerReason.GIFT,
        reference: str = "",
        meta: dict | None = None,
    ) -> tuple[Entry, Entry]:
        """Move coins between players — both rows commit together, so coins can't vanish."""
        if sender_id == receiver_id:
            raise ValueError("cannot pay yourself")
        out = await economy.debit(
            session,
            sender_id,
            amount,
            reason,
            reference=reference,
            counterparty=receiver_id,
            meta={"to": receiver_id, **(meta or {})},
        )
        inp = await economy.credit(
            session,
            receiver_id,
            amount,
            reason,
            reference=reference,
            counterparty=sender_id,
            meta={"from": sender_id, **(meta or {})},
        )
        return (out, inp)

    @staticmethod
    async def get_user_or_raise(session: AsyncSession, user_id: int) -> User | None:
        return await session.get(User, user_id)

    @staticmethod
    async def claim_once(
        session: AsyncSession, user_id: int, kind: str, local_day: str, *, amount: int = 0
    ) -> bool:
        """True on the first claim of ``kind`` for that local day; False if already claimed.

        Relies on ``uq_daily_claims_kind_day`` — TOCTOU-free by construction.
        """
        async with session.begin_nested():
            session.add(
                DailyClaim(
                    user_id=user_id,
                    kind=kind,
                    local_day=local_day,
                    amount=amount,
                    created_at=now_utc(),
                )
            )
            try:
                await session.flush()
            except IntegrityError:
                return False
        return True

    @staticmethod
    async def claimed(session: AsyncSession, user_id: int, kind: str, local_day: str) -> bool:
        found = (
            await session.execute(
                select(DailyClaim.id).where(
                    DailyClaim.user_id == user_id,
                    DailyClaim.kind == kind,
                    DailyClaim.local_day == local_day,
                )
            )
        ).scalar_one_or_none()
        return found is not None

    @staticmethod
    async def claim_history(
        session: AsyncSession, user_id: int, kind: str, *, limit: int = 30
    ) -> list[DailyClaim]:
        return list(
            (
                await session.execute(
                    select(DailyClaim)
                    .where(DailyClaim.user_id == user_id, DailyClaim.kind == kind)
                    .order_by(DailyClaim.local_day.desc())
                    .limit(limit)
                )
            ).scalars()
        )

    @staticmethod
    async def history(
        session: AsyncSession,
        user_id: int,
        *,
        limit: int = 20,
        offset: int = 0,
        reasons: list[str] | None = None,
    ) -> list[Transaction]:
        stmt = select(Transaction).where(Transaction.user_id == user_id)
        if reasons:
            stmt = stmt.where(Transaction.reason.in_([str(r) for r in reasons]))
        return list(
            (
                await session.execute(
                    stmt.order_by(Transaction.id.desc()).limit(limit).offset(offset)
                )
            ).scalars()
        )

    @staticmethod
    async def lifetime(session: AsyncSession, user_id: int, reason: str) -> int:
        value = (
            await session.execute(
                select(func.coalesce(func.sum(Transaction.delta), 0)).where(
                    Transaction.user_id == user_id, Transaction.reason == str(reason)
                )
            )
        ).scalar_one()
        return int(value or 0)

    @staticmethod
    async def total_circulating(session: AsyncSession) -> int:
        return int(
            (await session.execute(select(func.coalesce(func.sum(User.balance), 0)))).scalar_one()
            or 0
        )

    @staticmethod
    async def reason_totals(
        session: AsyncSession, *, since, limit: int = 12
    ) -> list[tuple[str, int, int]]:
        rows = (
            await session.execute(
                select(
                    Transaction.reason,
                    func.count(Transaction.id),
                    func.coalesce(func.sum(Transaction.delta), 0),
                )
                .where(Transaction.created_at >= since)
                .group_by(Transaction.reason)
                .order_by(func.count(Transaction.id).desc())
                .limit(limit)
            )
        ).all()
        return [(str(r[0]), int(r[1]), int(r[2])) for r in rows]

    @staticmethod
    async def premium_left_hours(session: AsyncSession, user_id: int) -> int:
        expires = (
            await session.execute(
                select(func.max(Premium.expires_at)).where(
                    Premium.user_id == user_id, Premium.expires_at > now_utc()
                )
            )
        ).scalar_one_or_none()
        if expires is None:
            return 0
        if expires.tzinfo is not None:
            from waifu.utils.time import to_naive_utc

            expires = to_naive_utc(expires)
        return max(0, int((expires - now_utc()).total_seconds() // 3600))

    @staticmethod
    async def is_premium(session: AsyncSession, user_id: int) -> bool:
        user = await session.get(User, user_id)
        if user is not None and user.premium_until is not None:
            from waifu.utils.time import to_naive_utc

            return to_naive_utc(user.premium_until) > now_utc()
        return await economy.premium_left_hours(session, user_id) > 0

    @staticmethod
    async def grant_premium(
        session: AsyncSession, user_id: int, hours: int, *, granted_by: int, source: str = "admin"
    ) -> int:
        """Extend premium from max(now, current expiry) and denormalise onto the user row."""
        from datetime import timedelta

        current = (
            await session.execute(
                select(func.max(Premium.expires_at)).where(Premium.user_id == user_id)
            )
        ).scalar_one_or_none()
        base = now_utc()
        if current is not None:
            from waifu.utils.time import to_naive_utc

            current_naive = to_naive_utc(current)
            base = max(base, current_naive)
        expires = base + timedelta(hours=hours)
        session.add(
            Premium(
                user_id=user_id,
                hours=hours,
                granted_by=granted_by,
                source=source,
                expires_at=expires,
            )
        )
        await session.execute(update(User).where(User.id == user_id).values(premium_until=expires))
        await session.flush()
        return int((expires - now_utc()).total_seconds() // 3600)

    @staticmethod
    async def revoke_premium(session: AsyncSession, user_id: int) -> None:
        await session.execute(
            Premium.__table__.delete().where(
                Premium.user_id == user_id, Premium.expires_at > now_utc()
            )
        )
        await session.execute(update(User).where(User.id == user_id).values(premium_until=None))
        await session.flush()

    @staticmethod
    async def premium_grants(
        session: AsyncSession, user_id: int, *, limit: int = 10
    ) -> list[Premium]:
        return list(
            (
                await session.execute(
                    select(Premium)
                    .where(Premium.user_id == user_id)
                    .order_by(Premium.created_at.desc())
                    .limit(limit)
                )
            ).scalars()
        )

    @staticmethod
    async def integrity_report(session: AsyncSession) -> list[str]:
        """For ``waifu doctor``: any way the ledger and the balance disagree."""
        problems: list[str] = []
        negative = (
            await session.execute(select(func.count()).select_from(User).where(User.balance < 0))
        ).scalar_one()
        if negative:
            problems.append(
                f"{negative} users have a negative balance (CHECK constraint bypassed?)"
            )
        mismatches = (
            await session.execute(
                select(User.id, User.balance, func.coalesce(func.sum(Transaction.delta), 0))
                .join(Transaction, Transaction.user_id == User.id, isouter=True)
                .group_by(User.id, User.balance)
                .having(func.coalesce(func.sum(Transaction.delta), 0) != User.balance)
                .limit(10)
            )
        ).all()
        for user_id, bal, total in mismatches:
            problems.append(f"user {user_id}: balance {bal} != ledger sum {total}")
        dupes = (
            await session.execute(
                select(Transaction.idempotency_key, func.count())
                .where(Transaction.idempotency_key.is_not(None))
                .group_by(Transaction.idempotency_key)
                .having(func.count() > 1)
                .limit(5)
            )
        ).all()
        for key, _count in dupes:
            problems.append(f"duplicate idempotency key in ledger: {key}")
        return problems


class items:
    """Shop items, shields, cooldowns and the heist log (/market, /steal, /bomb, /skip).

    Summon-bot modelled a shield as a row with ``uses_remaining`` and decremented it
    with read-modify-write; two simultaneous /steal calls could burn one shield. Here
    ``Shield`` is one row per charge (consumed with ``UPDATE … WHERE is_used IS FALSE``),
    which is correct by construction and also gives an exact audit trail.
    """

    ItemDef = ItemDef
    SPECIAL_COOLDOWNS = SPECIAL_COOLDOWNS
    ITEMS = ITEMS

    @staticmethod
    def live():
        """SQLAlchemy clause: a stack that has not rotted yet.

        ``plugins/market.py`` appended ``AND expires_at > datetime('now')`` to *every* inventory query,
        which is the whole anti-hoarding rule: buy 5 bombs for tonight's raid or lose them. Rows with
        no expiry (an admin grant, or anything predating the migration) stay live forever, so the
        column being nullable is a deliberate door rather than an oversight.
        """
        return or_(InventoryItem.expires_at.is_(None), InventoryItem.expires_at > now_utc())

    @staticmethod
    def item(key: str) -> ItemDef:
        try:
            return ITEMS[key]
        except KeyError:
            raise NotFound(f"unknown item “{key}”") from None

    @staticmethod
    async def stacks(session: AsyncSession, user_id: int, item_id: str) -> int:
        return int(
            (
                await session.execute(
                    select(func.coalesce(func.sum(InventoryItem.uses_remaining), 0)).where(
                        InventoryItem.user_id == user_id,
                        InventoryItem.item_id == item_id,
                        items.live(),
                    )
                )
            ).scalar_one()
            or 0
        )

    @staticmethod
    async def inventory(session: AsyncSession, user_id: int) -> dict[str, int]:
        rows = (
            await session.execute(
                select(
                    InventoryItem.item_id, func.coalesce(func.sum(InventoryItem.uses_remaining), 0)
                )
                .where(InventoryItem.user_id == user_id, items.live())
                .group_by(InventoryItem.item_id)
            )
        ).all()
        return {str(r[0]): int(r[1]) for r in rows if int(r[1]) > 0}

    @staticmethod
    async def expiry_hours(session: AsyncSession, user_id: int) -> dict[str, int]:
        """How long each stack lasts, in whole hours, for ``/inv``'s ``⌛ 7h left`` line."""
        rows = (
            await session.execute(
                select(InventoryItem.item_id, func.max(InventoryItem.expires_at))
                .where(
                    InventoryItem.user_id == user_id, InventoryItem.uses_remaining > 0, items.live()
                )
                .group_by(InventoryItem.item_id)
            )
        ).all()
        now = now_utc()
        out: dict[str, int] = {}
        for key, until in rows:
            if until is None:
                continue
            seconds = (until - now).total_seconds()
            out[str(key)] = max(0, int(seconds // 3600) + (1 if seconds % 3600 else 0))
        return out

    @staticmethod
    async def buy(
        session: AsyncSession, user_id: int, item_id: str, *, quantity: int = 1, ttl_hours: int = 0
    ) -> int:
        """Add uses to the player's inventory. Price/debit is the caller's job (economy.debit).

        Enforces the per-item stack cap so /market can't be used to hoard 400 bombs.
        """
        spec = items.item(item_id)
        owned = await items.stacks(session, user_id, item_id)
        allowed = max(0, spec.max_stack - owned)
        if allowed <= 0:
            raise Locked(
                f"you already have the maximum {(allowed if allowed else spec.max_stack)}× {spec.label}"
            )
        quantity = min(quantity, allowed)
        row = (
            await session.execute(
                select(InventoryItem)
                .where(InventoryItem.user_id == user_id, InventoryItem.item_id == item_id)
                .limit(1)
            )
        ).scalar_one_or_none()
        until = now_utc() + timedelta(hours=ttl_hours) if ttl_hours and ttl_hours > 0 else None
        if row is None:
            row = InventoryItem(
                user_id=user_id,
                item_id=item_id,
                uses_remaining=quantity * spec.charges,
                expires_at=until,
            )
            session.add(row)
        else:
            row.uses_remaining += quantity * spec.charges
            if until is not None and (row.expires_at is None or row.expires_at < until):
                row.expires_at = until
        await session.flush()
        return quantity

    @staticmethod
    async def spend(session: AsyncSession, user_id: int, item_id: str, *, count: int = 1) -> int:
        """Burn N charges. Returns remaining; raises when the player has none."""
        rows = list(
            (
                await session.execute(
                    select(InventoryItem)
                    .where(
                        InventoryItem.user_id == user_id,
                        InventoryItem.item_id == item_id,
                        InventoryItem.uses_remaining > 0,
                        items.live(),
                    )
                    .order_by(InventoryItem.expires_at.asc().nulls_last())
                    .with_for_update(skip_locked=True)
                    .limit(20)
                )
            ).scalars()
        )
        if not rows:
            raise NotFound(f"you have no {items.item(item_id).label.lower()} — buy one in /market")
        left = count
        for row in rows:
            take = min(left, row.uses_remaining)
            row.uses_remaining -= take
            left -= take
            if left <= 0:
                break
        if left > 0:
            raise NotFound(
                f"you only had {count - left} charge(s) of {items.item(item_id).label.lower()}"
            )
        await session.execute(
            InventoryItem.__table__.delete().where(
                InventoryItem.user_id == user_id,
                InventoryItem.item_id == item_id,
                InventoryItem.uses_remaining <= 0,
            )
        )
        await session.flush()
        return await items.stacks(session, user_id, item_id)

    @staticmethod
    async def activate(session: AsyncSession, user_id: int, item_id: str) -> int:
        """Mark a stack as 'in effect' (Lucky/XP/Magnet consume user.*_charges counters)."""
        await items.spend(session, user_id, item_id)
        await session.execute(
            update(InventoryItem)
            .where(InventoryItem.user_id == user_id, InventoryItem.item_id == item_id)
            .values(activated_at=now_utc())
        )
        await session.flush()
        return await items.stacks(session, user_id, item_id)

    @staticmethod
    async def add_shields(session: AsyncSession, user_id: int, kind: str, count: int = 1) -> int:
        if kind not in {"sshield", "bshield"}:
            raise ValueError("kind must be sshield|bshield")
        for _ in range(count):
            session.add(Shield(user_id=user_id, kind=kind))
        await session.flush()
        return await items.count_shields(session, user_id, kind)

    @staticmethod
    async def count_shields(session: AsyncSession, user_id: int, kind: str) -> int:
        return int(
            (
                await session.execute(
                    select(func.count(Shield.id)).where(
                        Shield.user_id == user_id, Shield.kind == kind, Shield.is_used.is_(False)
                    )
                )
            ).scalar_one()
            or 0
        )

    @staticmethod
    async def pop_shield(session: AsyncSession, user_id: int, kind: str) -> bool:
        """Consume one unused shield. ``is_used IS FALSE`` makes it a CAS."""
        result = await session.execute(
            update(Shield)
            .where(Shield.user_id == user_id, Shield.kind == kind, Shield.is_used.is_(False))
            .values(is_used=True, used_at=now_utc())
            .returning(Shield.id)
        )
        found = result.scalar_one_or_none()
        await session.flush()
        return found is not None

    @staticmethod
    async def set_cooldown(session: AsyncSession, user_id: int, command: str) -> None:
        stmt = select(Cooldown).where(Cooldown.user_id == user_id, Cooldown.command == command)
        row = (await session.execute(stmt)).scalar_one_or_none()
        if row is None:
            session.add(Cooldown(user_id=user_id, command=command, last_used=now_utc()))
        else:
            row.last_used = now_utc()
        await session.flush()

    @staticmethod
    async def cooldown_left(session: AsyncSession, user_id: int, command: str, seconds: int) -> int:
        last = (
            await session.execute(
                select(Cooldown.last_used).where(
                    Cooldown.user_id == user_id, Cooldown.command == command
                )
            )
        ).scalar_one_or_none()
        if last is None:
            return 0
        from waifu.utils.time import to_naive_utc

        elapsed = (now_utc() - to_naive_utc(last)).total_seconds()
        return max(0, int(seconds - elapsed))

    @staticmethod
    async def clear_cooldown(
        session: AsyncSession, user_id: int, command: str | None = None
    ) -> int:
        conds = [Cooldown.user_id == user_id]
        if command:
            conds.append(Cooldown.command == command)
        result = await session.execute(Cooldown.__table__.delete().where(and_(*conds)))
        return int(result.rowcount or 0)

    @staticmethod
    async def log_heist(
        session: AsyncSession,
        *,
        attacker_id: int,
        target_id: int,
        kind: str,
        outcome: str,
        amount: int = 0,
        character_id: int | None = None,
        note: str = "",
    ) -> None:
        session.add(
            HeistLog(
                attacker_id=attacker_id,
                target_id=target_id,
                kind=kind,
                outcome=outcome,
                amount=amount,
                character_id=character_id,
                note=note[:140],
                created_at=now_utc(),
            )
        )
        await session.flush()

    @staticmethod
    async def heist_stats(session: AsyncSession, user_id: int) -> dict[str, int]:
        """Win/loss tallies for /pinfo and /hstats (attacker *and* victim side)."""
        attacking = (
            await session.execute(
                select(
                    HeistLog.kind,
                    HeistLog.outcome,
                    func.count(HeistLog.id),
                    func.coalesce(func.sum(HeistLog.amount), 0),
                )
                .where(HeistLog.attacker_id == user_id)
                .group_by(HeistLog.kind, HeistLog.outcome)
            )
        ).all()
        defending = (
            await session.execute(
                select(HeistLog.kind, HeistLog.outcome, func.count(HeistLog.id))
                .where(HeistLog.target_id == user_id)
                .group_by(HeistLog.kind, HeistLog.outcome)
            )
        ).all()
        out = {
            "steal_wins": 0,
            "steal_blocked": 0,
            "bomb_wins": 0,
            "bomb_blocked": 0,
            "looted": 0,
            "defended": 0,
        }
        for kind, outcome, count, amount in attacking:
            if outcome == "success":
                out[f"{kind}_wins"] += int(count)
                out["looted"] += int(amount or 0)
        for kind, outcome, count in defending:
            if outcome == "blocked":
                out["defended"] += int(count)
                out[f"{kind}_blocked"] += int(count)
        return out

    @staticmethod
    async def recent_heists(
        session: AsyncSession, user_id: int, *, limit: int = 8
    ) -> list[HeistLog]:
        return list(
            (
                await session.execute(
                    select(HeistLog)
                    .where(or_(HeistLog.attacker_id == user_id, HeistLog.target_id == user_id))
                    .order_by(HeistLog.created_at.desc())
                    .limit(limit)
                )
            ).scalars()
        )

    @staticmethod
    async def protective_summary(session: AsyncSession) -> dict[str, int]:
        """Owner-facing: how much pain the item economy is causing (for balance tuning)."""
        row = (
            (
                await session.execute(
                    select(
                        func.count(HeistLog.id).filter(HeistLog.outcome == "success").label("hits"),
                        func.count(HeistLog.id)
                        .filter(HeistLog.outcome == "blocked")
                        .label("blocks"),
                        func.coalesce(func.sum(HeistLog.amount), 0).label("moved"),
                    )
                )
            )
            .mappings()
            .one()
        )
        return {
            "hits": int(row["hits"] or 0),
            "blocks": int(row["blocks"] or 0),
            "coins_moved": int(row["moved"] or 0),
        }

    @staticmethod
    async def most_stolen_from(session: AsyncSession, *, limit: int = 10) -> list[tuple[int, int]]:
        rows = (
            await session.execute(
                select(HeistLog.target_id, func.sum(HeistLog.amount))
                .where(HeistLog.kind == "steal", HeistLog.outcome == "success")
                .group_by(HeistLog.target_id)
                .order_by(func.sum(HeistLog.amount).desc())
                .limit(limit)
            )
        ).all()
        return [(int(r[0]), int(r[1] or 0)) for r in rows]

    @staticmethod
    async def purge_spent_items(session: AsyncSession) -> int:
        """Maintenance: spent stacks are recreated on demand, so drop them."""
        result = await session.execute(
            InventoryItem.__table__.delete().where(InventoryItem.uses_remaining <= 0)
        )
        return int(result.rowcount or 0)


class metrics:
    """Per-player activity counters used by achievements, /profile and /stats.

    Why a separate module: these are the numbers that do not belong to one table
    ("how many spawns has this player claimed", "how many auctions have they won"), and
    every one of them is a plain ``COUNT`` the ORM cannot infer. In Summon-bot these
    existed as denormalised columns updated by hand from three different files — they
    were wrong within a week, and the achievement list was built on top of them.

    Counting from the source is a query per metric; they are grouped here so a caller
    pays one round-trip (``many``), and every number is consistent with the ledger by
    construction.
    """

    @staticmethod
    async def spawns_claimed(session: AsyncSession, user_id: int) -> int:
        return int(
            (
                await session.execute(
                    select(func.count(SpawnEvent.id)).where(
                        SpawnEvent.claimed_by == user_id, SpawnEvent.status == "claimed"
                    )
                )
            ).scalar_one()
            or 0
        )

    @staticmethod
    async def rolls(session: AsyncSession, user_id: int, *, since: datetime | None = None) -> int:
        conds = [FairRoll.user_id == user_id]
        if since is not None:
            conds.append(FairRoll.created_at >= since)
        return int(
            (await session.execute(select(func.count(FairRoll.id)).where(*conds))).scalar_one() or 0
        )

    @staticmethod
    async def rare_rolls(
        session: AsyncSession, user_id: int, *, min_rarity: Rarity = Rarity.RARE
    ) -> int:
        return int(
            (
                await session.execute(
                    select(func.count(FairRoll.id)).where(
                        FairRoll.user_id == user_id, FairRoll.rarity_id >= int(min_rarity)
                    )
                )
            ).scalar_one()
            or 0
        )

    @staticmethod
    async def work_count(session: AsyncSession, user_id: int) -> int:
        return int(
            (
                await session.execute(
                    select(func.count(Transaction.id)).where(
                        Transaction.user_id == user_id, Transaction.reason.in_(["work", "job"])
                    )
                )
            ).scalar_one()
            or 0
        )

    @staticmethod
    async def earned_since(session: AsyncSession, user_id: int, *, since: datetime) -> int:
        return int(
            (
                await session.execute(
                    select(func.coalesce(func.sum(Transaction.delta), 0)).where(
                        Transaction.user_id == user_id,
                        Transaction.delta > 0,
                        Transaction.created_at >= since,
                    )
                )
            ).scalar_one()
            or 0
        )

    @staticmethod
    async def auctions_won(session: AsyncSession, user_id: int) -> int:
        return int(
            (
                await session.execute(
                    select(func.count(Auction.id)).where(
                        Auction.seller_id == user_id, Auction.status == "sold"
                    )
                )
            ).scalar_one()
            or 0
        )

    @staticmethod
    async def auctions_bought(session: AsyncSession, user_id: int) -> int:
        return int(
            (
                await session.execute(
                    select(func.count(Auction.id)).where(
                        Auction.top_bidder_id == user_id, Auction.status == str(AuctionStatus.SOLD)
                    )
                )
            ).scalar_one()
            or 0
        )

    @staticmethod
    async def trades_done(session: AsyncSession, user_id: int) -> int:
        """Trades this player was on either side of and that actually executed.

        JSON containment is used instead of loading the offers: a user id can appear in
        either of two participant columns, and ``OR`` of two containment tests is one
        query rather than two loads plus a Python filter.
        """
        from sqlalchemy import or_

        stmt = select(func.count(TradeOffer.id)).where(
            TradeOffer.status == str(TradeStatus.COMPLETED),
            or_(TradeOffer.initiator_id == user_id, TradeOffer.partner_id == user_id),
        )
        return int((await session.execute(stmt)).scalar_one() or 0)

    @staticmethod
    async def gifts_sent(session: AsyncSession, user_id: int) -> int:
        return int(
            (
                await session.execute(
                    select(func.count(GiftLog.id)).where(GiftLog.sender_id == user_id)
                )
            ).scalar_one()
            or 0
        )

    @staticmethod
    async def gifts_received(session: AsyncSession, user_id: int) -> int:
        return int(
            (
                await session.execute(
                    select(func.count(GiftLog.id)).where(GiftLog.receiver_id == user_id)
                )
            ).scalar_one()
            or 0
        )

    @staticmethod
    async def heists_won(session: AsyncSession, user_id: int, *, kind: str | None = None) -> int:
        """Successful steal/bomb attempts by this player (``kind`` narrows it)."""
        conds = [HeistLog.attacker_id == user_id, HeistLog.outcome == "success"]
        if kind:
            conds.append(HeistLog.kind == kind)
        return int(
            (await session.execute(select(func.count(HeistLog.id)).where(*conds))).scalar_one() or 0
        )

    @staticmethod
    async def heists_lost(session: AsyncSession, user_id: int, *, kind: str | None = None) -> int:
        conds = [HeistLog.target_id == user_id, HeistLog.outcome == "success"]
        if kind:
            conds.append(HeistLog.kind == kind)
        return int(
            (await session.execute(select(func.count(HeistLog.id)).where(*conds))).scalar_one() or 0
        )

    @staticmethod
    async def shields_used(session: AsyncSession, user_id: int) -> int:
        return int(
            (
                await session.execute(
                    select(func.count(HeistLog.id)).where(
                        HeistLog.target_id == user_id, HeistLog.outcome == "blocked"
                    )
                )
            ).scalar_one()
            or 0
        )

    @staticmethod
    async def collection_size(session: AsyncSession, user_id: int) -> int:
        return int(
            (
                await session.execute(
                    select(func.count(Ownership.character_id)).where(
                        Ownership.user_id == user_id, Ownership.count > 0
                    )
                )
            ).scalar_one()
            or 0
        )

    @staticmethod
    async def dupe_count(session: AsyncSession, user_id: int) -> int:
        return int(
            (
                await session.execute(
                    select(func.coalesce(func.sum(Ownership.count - 1), 0)).where(
                        Ownership.user_id == user_id, Ownership.count > 1
                    )
                )
            ).scalar_one()
            or 0
        )

    @staticmethod
    async def reason_count(
        session: AsyncSession, user_id: int, reason: str, *, since: datetime | None = None
    ) -> int:
        """How many ledger lines of one reason (spin/sell/gift…) since an optional time."""
        conds = [Transaction.user_id == user_id, Transaction.reason == reason]
        if since is not None:
            conds.append(Transaction.created_at >= since)
        return int(
            (await session.execute(select(func.count(Transaction.id)).where(*conds))).scalar_one()
            or 0
        )

    @staticmethod
    async def ai_messages_today(session: AsyncSession, user_id: int, *, since: datetime) -> int:
        return int(
            (
                await session.execute(
                    select(func.count(AiMessageLog.id)).where(
                        AiMessageLog.user_id == user_id,
                        AiMessageLog.role == "user",
                        AiMessageLog.created_at >= since,
                    )
                )
            ).scalar_one()
            or 0
        )

    @staticmethod
    async def many(session: AsyncSession, user_id: int) -> dict[str, int]:
        """All counters, one call (used by /stats, achievements and the Mini App)."""
        day_start = now_utc().replace(hour=0, minute=0, second=0, microsecond=0)
        return {
            "spawns_claimed": await metrics.spawns_claimed(session, user_id),
            "rolls_total": await metrics.rolls(session, user_id),
            "rolls_today": await metrics.rolls(session, user_id, since=day_start),
            "rare_rolls": await metrics.rare_rolls(session, user_id),
            "work_count": await metrics.work_count(session, user_id),
            "earned_24h": await metrics.earned_since(session, user_id, since=day_start),
            "auctions_won": await metrics.auctions_won(session, user_id),
            "auctions_bought": await metrics.auctions_bought(session, user_id),
            "trades_done": await metrics.trades_done(session, user_id),
            "gifts_sent": await metrics.gifts_sent(session, user_id),
            "gifts_received": await metrics.gifts_received(session, user_id),
            "heists_won": await metrics.heists_won(session, user_id),
            "heists_lost": await metrics.heists_lost(session, user_id),
            "collection_size": await metrics.collection_size(session, user_id),
            "dupes": await metrics.dupe_count(session, user_id),
            "spin_count": await metrics.reason_count(session, user_id, "spin"),
            "sold_today": await metrics.reason_count(session, user_id, "sell", since=day_start),
            "ai_today": await metrics.ai_messages_today(session, user_id, since=day_start),
        }


class moderation:
    """Admin/sudo surface: sudo admins, warnings, mutes/bans, cases, audit trail.

    Summon-bot's ``/remove`` deleted messages but never recorded who removed what;
    ``/warn`` bumped a counter with no history. Both are recorded here, and every
    privileged command calls :func:`audit` in the same transaction as its effect.
    """

    ALL_PERMS = ALL_PERMS

    @staticmethod
    async def sudo_list(session: AsyncSession, *, active_only: bool = True) -> list[SudoAdmin]:
        stmt = select(SudoAdmin).order_by(SudoAdmin.created_at.desc())
        if active_only:
            stmt = stmt.where(SudoAdmin.is_active.is_(True))
        return list((await session.execute(stmt)).scalars())

    @staticmethod
    async def sudo_row(session: AsyncSession, user_id: int) -> SudoAdmin | None:
        return await session.get(SudoAdmin, user_id)

    @staticmethod
    async def add_sudo(
        session: AsyncSession,
        user_id: int,
        *,
        added_by: int,
        username: str = "",
        permissions: dict | None = None,
    ) -> SudoAdmin:
        if await session.get(User, user_id) is None and (not username):
            raise NotFound("that user has never used the bot — ask them to /start first")
        row = await session.get(SudoAdmin, user_id)
        if row is not None and row.is_active:
            raise Locked("already a sudo admin (use /editsudo to change)")
        if row is None:
            row = SudoAdmin(
                user_id=user_id,
                username=username[:64],
                added_by=added_by,
                permissions=permissions or dict(DEFAULT_SUDO_PERMS),
            )
            session.add(row)
        else:
            row.is_active = True
            row.permissions = permissions or dict(DEFAULT_SUDO_PERMS)
            row.username = username[:64] or row.username
        await session.flush()
        return row

    @staticmethod
    async def update_sudo(
        session: AsyncSession,
        user_id: int,
        *,
        permissions: dict | None = None,
        active: bool | None = None,
    ) -> SudoAdmin:
        row = await session.get(SudoAdmin, user_id)
        if row is None:
            raise NotFound("not a sudo admin")
        if permissions is not None:
            unknown = set(permissions) - set(PERMISSIONS)
            if unknown:
                raise ValueError(f"unknown permission(s): {', '.join(sorted(unknown))}")
            row.permissions = permissions
        if active is not None:
            row.is_active = active
        await session.flush()
        return row

    @staticmethod
    async def remove_sudo(session: AsyncSession, user_id: int) -> None:
        await session.execute(SudoAdmin.__table__.delete().where(SudoAdmin.user_id == user_id))
        await session.flush()

    @staticmethod
    async def permissions_for(session: AsyncSession, user_id: int) -> dict[str, bool]:
        """Effective permission map (owner/admin → everything, sudo → its grants)."""
        user = await session.get(User, user_id)
        if user is not None and user.role in {"owner", "admin"}:
            return dict(ALL_PERMS)
        row = await moderation.sudo_row(session, user_id)
        if row is None or not row.is_active:
            return {}
        return {k: bool(v) for k, v in (row.permissions or {}).items() if k in PERMISSIONS}

    @staticmethod
    async def add_warning(
        session: AsyncSession, *, chat_id: int, user_id: int, moderator_id: int, reason: str = ""
    ) -> tuple[Warning, int]:
        row = Warning(
            chat_id=chat_id,
            user_id=user_id,
            moderator_id=moderator_id,
            reason=reason[:255],
            created_at=now_utc(),
        )
        session.add(row)
        await session.execute(
            update(User).where(User.id == user_id).values(warn_count=User.warn_count + 1)
        )
        await session.flush()
        total = (
            await session.execute(select(User.warn_count).where(User.id == user_id))
        ).scalar_one_or_none()
        return (row, int(total or 0))

    @staticmethod
    async def warnings_for(
        session: AsyncSession, chat_id: int, user_id: int, *, unresolved_only: bool = True
    ) -> list[Warning]:
        conds = [Warning.chat_id == chat_id, Warning.user_id == user_id]
        if unresolved_only:
            conds.append(Warning.is_resolved.is_(False))
        return list(
            (
                await session.execute(select(Warning).where(*conds).order_by(Warning.id.desc()))
            ).scalars()
        )

    @staticmethod
    async def remove_warning(
        session: AsyncSession, chat_id: int, user_id: int, *, count: int = 1
    ) -> int:
        rows = list(
            (
                await session.execute(
                    select(Warning)
                    .where(
                        Warning.chat_id == chat_id,
                        Warning.user_id == user_id,
                        Warning.is_resolved.is_(False),
                    )
                    .order_by(Warning.id.desc())
                    .limit(count)
                )
            ).scalars()
        )
        for row in rows:
            row.is_resolved = True
        remaining = (
            await session.execute(
                select(func.count())
                .select_from(Warning)
                .where(
                    Warning.chat_id == chat_id,
                    Warning.user_id == user_id,
                    Warning.is_resolved.is_(False),
                )
            )
        ).scalar_one()
        await session.execute(
            update(User).where(User.id == user_id).values(warn_count=int(remaining))
        )
        await session.flush()
        return int(remaining)

    @staticmethod
    async def warning_counts(
        session: AsyncSession, chat_id: int, *, limit: int = 20
    ) -> list[tuple[int, int]]:
        rows = (
            await session.execute(
                select(Warning.user_id, func.count(Warning.id))
                .where(Warning.chat_id == chat_id, Warning.is_resolved.is_(False))
                .group_by(Warning.user_id)
                .order_by(func.count(Warning.id).desc())
                .limit(limit)
            )
        ).all()
        return [(int(r[0]), int(r[1])) for r in rows]

    @staticmethod
    async def open_case(
        session: AsyncSession,
        *,
        chat_id: int,
        target_user_id: int,
        moderator_id: int,
        action: str,
        reason: str = "",
        duration: int = 0,
        message_ids: dict | None = None,
        member_tag: str = "",
    ) -> ModerationCase:
        row = ModerationCase(
            chat_id=chat_id,
            target_user_id=target_user_id,
            moderator_id=moderator_id,
            action=action,
            reason=reason[:255],
            duration_seconds=max(0, int(duration)),
            message_ids=message_ids or {},
            member_tag=member_tag[:48],
            expires_at=now_utc() + timedelta(seconds=duration) if duration else None,
        )
        session.add(row)
        await session.flush()
        return row

    @staticmethod
    async def close_case(session: AsyncSession, case_id: int) -> None:
        await session.execute(
            update(ModerationCase).where(ModerationCase.id == case_id).values(is_active=False)
        )
        await session.flush()

    @staticmethod
    async def close_active(
        session: AsyncSession, chat_id: int, user_id: int, *, action: str | None = None
    ) -> int:
        conds = [
            ModerationCase.chat_id == chat_id,
            ModerationCase.target_user_id == user_id,
            ModerationCase.is_active.is_(True),
        ]
        if action:
            conds.append(ModerationCase.action == action)
        result = await session.execute(update(ModerationCase).where(*conds).values(is_active=False))
        return int(result.rowcount or 0)

    @staticmethod
    async def cases(
        session: AsyncSession, chat_id: int, *, user_id: int | None = None, limit: int = 25
    ) -> list[ModerationCase]:
        conds = [ModerationCase.chat_id == chat_id]
        if user_id:
            conds.append(ModerationCase.target_user_id == user_id)
        return list(
            (
                await session.execute(
                    select(ModerationCase)
                    .where(*conds)
                    .order_by(ModerationCase.id.desc())
                    .limit(limit)
                )
            ).scalars()
        )

    @staticmethod
    async def due_unmutes(session: AsyncSession) -> list[ModerationCase]:
        return list(
            (
                await session.execute(
                    select(ModerationCase).where(
                        ModerationCase.is_active.is_(True),
                        ModerationCase.action == "mute",
                        ModerationCase.expires_at <= now_utc(),
                    )
                )
            ).scalars()
        )

    @staticmethod
    async def ban_list(session: AsyncSession, *, limit: int = 50) -> list[BannedUser]:
        return list(
            (
                await session.execute(
                    select(BannedUser).order_by(BannedUser.created_at.desc()).limit(limit)
                )
            ).scalars()
        )

    @staticmethod
    async def is_globally_banned(session: AsyncSession, user_id: int) -> bool:
        row = await session.get(BannedUser, user_id)
        if row is None:
            return False
        if row.expires_at is not None and row.expires_at.replace(tzinfo=None) < now_utc():
            await session.execute(
                BannedUser.__table__.delete().where(BannedUser.user_id == user_id)
            )
            await session.flush()
            return False
        return True

    @staticmethod
    async def audit(
        session: AsyncSession,
        *,
        actor_id: int,
        action: str,
        target: str = "",
        detail: str = "",
        chat_id: int | None = None,
        scope: str = "global",
    ) -> None:
        session.add(
            AuditLog(
                actor_id=actor_id,
                action=action[:48],
                target=target[:96],
                detail=detail[:4000],
                chat_id=chat_id,
                scope=scope,
            )
        )
        await session.flush()

    @staticmethod
    async def audit_rows(
        session: AsyncSession,
        *,
        limit: int = 30,
        actor_id: int | None = None,
        chat_id: int | None = None,
        contains: str = "",
    ) -> list[AuditLog]:
        conds = []
        if actor_id:
            conds.append(AuditLog.actor_id == actor_id)
        if chat_id:
            conds.append(AuditLog.chat_id == chat_id)
        stmt = select(AuditLog).where(*conds) if conds else select(AuditLog)
        if contains:
            stmt = stmt.where(
                or_(AuditLog.action.ilike(f"%{contains}%"), AuditLog.detail.ilike(f"%{contains}%"))
            )
        return list(
            (await session.execute(stmt.order_by(AuditLog.id.desc()).limit(limit))).scalars()
        )

    @staticmethod
    async def audit_purge(session: AsyncSession, *, older_than_days: int = 180) -> int:
        cutoff = now_utc() - timedelta(days=older_than_days)
        result = await session.execute(
            AuditLog.__table__.delete().where(AuditLog.created_at < cutoff)
        )
        return int(result.rowcount or 0)

    @staticmethod
    async def moderation_summary(session: AsyncSession, chat_id: int) -> dict[str, int]:
        row = (
            (
                await session.execute(
                    select(
                        func.count(ModerationCase.id).label("cases"),
                        func.count(ModerationCase.id)
                        .filter(ModerationCase.is_active.is_(True))
                        .label("active"),
                        func.count(func.distinct(ModerationCase.target_user_id)).label("targets"),
                    ).where(ModerationCase.chat_id == chat_id)
                )
            )
            .mappings()
            .one()
        )
        return {
            "cases": int(row["cases"] or 0),
            "active": int(row["active"] or 0),
            "targets": int(row["targets"] or 0),
        }


class monetize:
    """Monetisation persistence: Telegram Stars, subscriptions, boosts, raffles.

    Delivery is **idempotent by payload**: ``star_purchases.invoice_payload`` is
    UNIQUE, and the row is flipped ``pending → paid → delivered`` with a conditional
    UPDATE. Telegram retries both ``pre_checkout_query`` and ``successful_payment``,
    and paid-media purchases arrive as a separate update type — with this shape, a
    retry credits coins exactly once. (Summon-bot's ``/premium`` was admin-granted
    only, so a paid support flow had no record at all.)
    """

    @staticmethod
    async def create_order(
        session: AsyncSession,
        *,
        user_id: int,
        invoice_payload: str,
        product: str,
        product_ref: str = "",
        star_count: int,
        coins_granted: int = 0,
        premium_hours: int = 0,
        character_id: int | None = None,
        source: str = "invoice",
    ) -> StarPurchase:
        row = StarPurchase(
            user_id=user_id,
            invoice_payload=invoice_payload,
            product=product,
            product_ref=product_ref,
            star_count=star_count,
            coins_granted=coins_granted,
            premium_hours=premium_hours,
            character_id=character_id,
            source=source,
            status="pending",
        )
        session.add(row)
        await session.flush()
        return row

    @staticmethod
    async def by_payload(session: AsyncSession, payload: str) -> StarPurchase | None:
        return (
            await session.execute(
                select(StarPurchase).where(StarPurchase.invoice_payload == payload)
            )
        ).scalar_one_or_none()

    @staticmethod
    async def mark_paid(
        session: AsyncSession, payload: str, *, charge_id: str = "", star_count: int | None = None
    ) -> StarPurchase:
        row = await monetize.by_payload(session, payload)
        if row is None:
            raise NotFound("unknown purchase payload")
        if row.status in {"paid", "delivered"}:
            return row
        row.status = "paid"
        row.paid_at = now_utc()
        row.telegram_payment_charge_id = charge_id
        if star_count:
            row.star_count = star_count
        await session.flush()
        return row

    @staticmethod
    async def mark_delivered(session: AsyncSession, payload: str) -> bool:
        """Flip paid → delivered. Returns False when someone else already delivered it."""
        result = await session.execute(
            update(StarPurchase)
            .where(StarPurchase.invoice_payload == payload, StarPurchase.status == "paid")
            .values(status="delivered", delivered_at=now_utc())
            .returning(StarPurchase.id)
        )
        return result.scalar_one_or_none() is not None

    @staticmethod
    async def mark_refunded(session: AsyncSession, payload: str) -> StarPurchase | None:
        row = await monetize.by_payload(session, payload)
        if row is None or row.status == "refunded":
            return row
        row.status = "refunded"
        row.refunded_at = now_utc()
        await session.flush()
        return row

    @staticmethod
    async def orders_for(
        session: AsyncSession, user_id: int, *, limit: int = 20
    ) -> list[StarPurchase]:
        return list(
            (
                await session.execute(
                    select(StarPurchase)
                    .where(StarPurchase.user_id == user_id)
                    .order_by(StarPurchase.id.desc())
                    .limit(limit)
                )
            ).scalars()
        )

    @staticmethod
    async def pending_orders(
        session: AsyncSession, *, older_than_minutes: int = 120
    ) -> list[StarPurchase]:
        cutoff = now_utc() - timedelta(minutes=older_than_minutes)
        return list(
            (
                await session.execute(
                    select(StarPurchase)
                    .where(StarPurchase.status == "pending", StarPurchase.created_at < cutoff)
                    .limit(200)
                )
            ).scalars()
        )

    @staticmethod
    async def owns_paid_media(session: AsyncSession, user_id: int, character_id: int) -> bool:
        found = (
            await session.execute(
                select(StarPurchase.id)
                .where(
                    StarPurchase.user_id == user_id,
                    StarPurchase.character_id == character_id,
                    StarPurchase.source == "paid_media",
                    StarPurchase.status.in_(["paid", "delivered"]),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        return found is not None

    @staticmethod
    async def paid_media_buyers(session: AsyncSession, character_id: int) -> list[int]:
        rows = (
            await session.execute(
                select(StarPurchase.user_id)
                .where(
                    StarPurchase.character_id == character_id,
                    StarPurchase.source == "paid_media",
                    StarPurchase.status.in_(["paid", "delivered"]),
                )
                .distinct()
            )
        ).scalars()
        return list(rows)

    @staticmethod
    async def revenue(session: AsyncSession, *, since=None) -> dict[str, int]:
        conds = [StarPurchase.status.in_(["paid", "delivered"])]
        if since:
            conds.append(StarPurchase.paid_at >= since)
        row = (
            (
                await session.execute(
                    select(
                        func.coalesce(func.sum(StarPurchase.star_count), 0).label("stars"),
                        func.count(StarPurchase.id).label("orders"),
                        func.coalesce(func.sum(StarPurchase.coins_granted), 0).label("coins_out"),
                    ).where(*conds)
                )
            )
            .mappings()
            .one()
        )
        return {
            "stars": int(row["stars"] or 0),
            "orders": int(row["orders"] or 0),
            "coins_sold": int(row["coins_out"] or 0),
        }

    @staticmethod
    async def charge_id_for(session: AsyncSession, payload: str) -> str:
        row = await monetize.by_payload(session, payload)
        return row.telegram_payment_charge_id if row else ""

    @staticmethod
    async def upsert_subscription(
        session: AsyncSession,
        *,
        user_id: int,
        subscription_id: str,
        chat_id: int | None,
        tier: str,
        status: SubscriptionState,
        amount: int,
        currency: str,
        current_period_end,
        is_from_gift: bool = False,
    ) -> SubscriptionAccess:
        row = (
            await session.execute(
                select(SubscriptionAccess).where(
                    SubscriptionAccess.user_id == user_id,
                    SubscriptionAccess.subscription_id == subscription_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            row = SubscriptionAccess(
                user_id=user_id,
                subscription_id=subscription_id,
                chat_id=chat_id,
                tier=tier,
                status=str(status),
                amount=amount,
                currency=currency,
                is_from_gift=is_from_gift,
                current_period_end=current_period_end,
            )
            session.add(row)
        else:
            row.status = str(status)
            row.amount = amount
            row.currency = currency
            row.current_period_end = current_period_end
            row.tier = tier
        await session.flush()
        return row

    @staticmethod
    async def active_subs(session: AsyncSession, user_id: int) -> list[SubscriptionAccess]:
        return list(
            (
                await session.execute(
                    select(SubscriptionAccess).where(
                        SubscriptionAccess.user_id == user_id,
                        SubscriptionAccess.status == str(SubscriptionState.ACTIVE),
                    )
                )
            ).scalars()
        )

    @staticmethod
    async def has_subscription(session: AsyncSession, user_id: int) -> bool:
        return bool(await monetize.active_subs(session, user_id))

    @staticmethod
    async def subscription_period_end(session: AsyncSession, user_id: int):
        return (
            await session.execute(
                select(func.max(SubscriptionAccess.current_period_end)).where(
                    SubscriptionAccess.user_id == user_id,
                    SubscriptionAccess.status == str(SubscriptionState.ACTIVE),
                )
            )
        ).scalar_one_or_none()

    @staticmethod
    async def record_boost(
        session: AsyncSession,
        *,
        user_id: int,
        chat_id: int,
        boost_id: str,
        boost_count: int,
        source: str,
        expires_at,
    ) -> bool:
        """False when this boost_id was already ingested (updates are at-least-once)."""
        exists = (
            await session.execute(
                select(BoostGrant.id).where(
                    BoostGrant.user_id == user_id, BoostGrant.boost_id == boost_id
                )
            )
        ).scalar_one_or_none()
        if exists:
            return False
        session.add(
            BoostGrant(
                user_id=user_id,
                chat_id=chat_id,
                boost_id=boost_id,
                boost_count=boost_count,
                source=source,
                expires_at=expires_at,
            )
        )
        await session.flush()
        return True

    @staticmethod
    async def pending_boost_count(session: AsyncSession, user_id: int, chat_id: int) -> int:
        value = (
            await session.execute(
                select(func.coalesce(func.sum(BoostGrant.boost_count), 0)).where(
                    BoostGrant.user_id == user_id,
                    BoostGrant.chat_id == chat_id,
                    BoostGrant.reward_state == "pending",
                    BoostGrant.expires_at > now_utc(),
                )
            )
        ).scalar_one()
        return int(value or 0)

    @staticmethod
    async def consume_boost_rewards(session: AsyncSession, user_id: int, chat_id: int) -> int:
        result = await session.execute(
            update(BoostGrant)
            .where(
                BoostGrant.user_id == user_id,
                BoostGrant.chat_id == chat_id,
                BoostGrant.reward_state == "pending",
            )
            .values(reward_state="redeemed")
        )
        return int(result.rowcount or 0)

    @staticmethod
    async def boosters_in(
        session: AsyncSession, chat_id: int, *, limit: int = 25
    ) -> list[tuple[int, int]]:
        rows = (
            await session.execute(
                select(BoostGrant.user_id, func.sum(BoostGrant.boost_count))
                .where(BoostGrant.chat_id == chat_id, BoostGrant.expires_at > now_utc())
                .group_by(BoostGrant.user_id)
                .order_by(func.sum(BoostGrant.boost_count).desc())
                .limit(limit)
            )
        ).all()
        return [(int(r[0]), int(r[1] or 0)) for r in rows]

    @staticmethod
    async def open_raffle(
        session: AsyncSession,
        *,
        chat_id: int,
        message_id: int | None,
        emoji: str,
        reward: int,
        seconds: int,
        max_winners: int = 3,
        theme: str = "",
    ) -> RaffleRound:
        await session.execute(
            update(RaffleRound)
            .where(RaffleRound.chat_id == chat_id, RaffleRound.status == "open")
            .values(status="superseded")
        )
        row = RaffleRound(
            chat_id=chat_id,
            message_id=message_id,
            emoji=emoji[:8],
            reward=reward,
            max_winners=max(1, max_winners),
            theme=theme[:64],
            status="open",
            ends_at=now_utc() + timedelta(seconds=seconds),
        )
        session.add(row)
        await session.flush()
        return row

    @staticmethod
    async def current_raffle(session: AsyncSession, chat_id: int) -> RaffleRound | None:
        return (
            await session.execute(
                select(RaffleRound)
                .where(
                    RaffleRound.chat_id == chat_id,
                    RaffleRound.status == "open",
                    RaffleRound.ends_at > now_utc(),
                )
                .limit(1)
            )
        ).scalar_one_or_none()

    @staticmethod
    async def raffle_by_message(
        session: AsyncSession, chat_id: int, message_id: int
    ) -> RaffleRound | None:
        return (
            await session.execute(
                select(RaffleRound)
                .where(RaffleRound.chat_id == chat_id, RaffleRound.message_id == message_id)
                .limit(1)
            )
        ).scalar_one_or_none()

    @staticmethod
    async def raffle_counts(session: AsyncSession, raffle_id: int) -> int:
        row = await session.get(RaffleRound, raffle_id)
        return int(row.entrants or 0) if row else 0

    @staticmethod
    async def finish_raffle(
        session: AsyncSession, raffle_id: int, *, winners: dict[int, int]
    ) -> RaffleRound:
        """CAS close so two scheduler passes can't draw twice."""
        result = await session.execute(
            update(RaffleRound)
            .where(RaffleRound.id == raffle_id, RaffleRound.status == "open")
            .values(status="drawn", winners={str(k): v for k, v in winners.items()})
            .returning(RaffleRound.id)
        )
        if result.scalar_one_or_none() is None:
            raise AlreadyClaimed("raffle already drawn")
        row = await session.get(RaffleRound, raffle_id)
        assert row is not None
        return row

    @staticmethod
    async def raffles_to_draw(session: AsyncSession) -> list[RaffleRound]:
        return list(
            (
                await session.execute(
                    select(RaffleRound).where(
                        RaffleRound.status == "open", RaffleRound.ends_at <= now_utc()
                    )
                )
            ).scalars()
        )

    @staticmethod
    async def raffle_history(
        session: AsyncSession, chat_id: int, *, limit: int = 10
    ) -> list[RaffleRound]:
        return list(
            (
                await session.execute(
                    select(RaffleRound)
                    .where(RaffleRound.chat_id == chat_id)
                    .order_by(RaffleRound.id.desc())
                    .limit(limit)
                )
            ).scalars()
        )

    @staticmethod
    async def supporter_count(session: AsyncSession) -> int:
        return int(
            (
                await session.execute(
                    select(func.count(func.distinct(StarPurchase.user_id))).where(
                        StarPurchase.status.in_(["paid", "delivered"])
                    )
                )
            ).scalar_one()
            or 0
        )

    @staticmethod
    async def top_supporters(session: AsyncSession, *, limit: int = 10) -> list[tuple[User, int]]:
        rows = (
            await session.execute(
                select(User, func.sum(StarPurchase.star_count).label("stars"))
                .join(StarPurchase, StarPurchase.user_id == User.id)
                .where(StarPurchase.status.in_(["paid", "delivered"]))
                .group_by(User.id)
                .order_by(func.sum(StarPurchase.star_count).desc())
                .limit(limit)
            )
        ).all()
        return [(r[0], int(r[1] or 0)) for r in rows]

    @staticmethod
    async def has_ever_paid(session: AsyncSession, user_id: int) -> bool:
        found = (
            await session.execute(
                select(StarPurchase.id)
                .where(
                    StarPurchase.user_id == user_id, StarPurchase.status.in_(["paid", "delivered"])
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        return found is not None

    @staticmethod
    async def record_premium_granted(session: AsyncSession, payload: str, hours: int) -> None:
        """Annotate the order with the premium it bought (the grant itself is in
        :func:`waifu.db.repo.economy.grant_premium`, so expiry stacking
        happens in exactly one place)."""
        row = await monetize.by_payload(session, payload)
        if row is None:
            return
        row.premium_hours = max(row.premium_hours, hours)
        await session.flush()

    @staticmethod
    async def payload_for_charge(session: AsyncSession, charge_id: str) -> str:
        """Reverse lookup used by the refund flow: Telegram's charge id → our payload."""
        row = (
            await session.execute(
                select(StarPurchase.invoice_payload)
                .where(StarPurchase.telegram_payment_charge_id == charge_id)
                .order_by(StarPurchase.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return str(row or "")

    @staticmethod
    async def subscription_rows(session: AsyncSession, user_id: int) -> list[dict[str, Any]]:
        rows = (
            await session.execute(
                select(SubscriptionAccess)
                .where(SubscriptionAccess.user_id == user_id)
                .order_by(SubscriptionAccess.id.desc())
                .limit(10)
            )
        ).scalars()
        return [
            {
                "tier": r.tier,
                "status": r.status,
                "amount": r.amount,
                "currency": r.currency,
                "until": r.current_period_end,
            }
            for r in rows
        ]

    @staticmethod
    async def active_subscription_charge(session: AsyncSession, user_id: int) -> str:
        row = (
            await session.execute(
                select(SubscriptionAccess)
                .where(SubscriptionAccess.user_id == user_id, SubscriptionAccess.status == "active")
                .order_by(SubscriptionAccess.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return str(row.subscription_id) if row else ""

    @staticmethod
    async def due_renewals(session: AsyncSession) -> list[tuple[int, int]]:
        """``(user_id, amount)`` for active subscriptions whose period has lapsed.

        Telegram renews silently — no ``subscription`` update arrives — so the nightly
        job compares ``current_period_end`` against the clock instead of trusting an
        event that never comes. Amount is the per-period price we stored, for the log.
        """
        rows = (
            await session.execute(
                select(SubscriptionAccess.user_id, SubscriptionAccess.amount).where(
                    SubscriptionAccess.status == str(SubscriptionState.ACTIVE),
                    SubscriptionAccess.current_period_end.is_not(None),
                    SubscriptionAccess.current_period_end <= now_utc(),
                )
            )
        ).all()
        return [(int(r[0]), int(r[1] or 0)) for r in rows]

    @staticmethod
    async def close_subscription(
        session: AsyncSession, user_id: int, *, status: str = "cancelled"
    ) -> int:
        """Flip every active row for this user — Telegram cancellation must not leave perks."""
        result = await session.execute(
            update(SubscriptionAccess)
            .where(SubscriptionAccess.user_id == user_id, SubscriptionAccess.status == "active")
            .values(status=status, updated_at=now_utc())
        )
        await session.execute(update(User).where(User.id == user_id).values(premium_until=None))
        return int(result.rowcount or 0)

    @staticmethod
    async def extend_subscription(
        session: AsyncSession, user_id: int, *, days: int = 30
    ) -> datetime:
        """Renewal: push ``current_period_end`` out from whichever is later, now or the old end."""
        row = (
            await session.execute(
                select(SubscriptionAccess)
                .where(SubscriptionAccess.user_id == user_id, SubscriptionAccess.status == "active")
                .order_by(SubscriptionAccess.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        base = now_utc()
        if (
            row is not None
            and row.current_period_end is not None
            and (row.current_period_end > base)
        ):
            base = row.current_period_end
        period_end = base + timedelta(days=max(1, days))
        if row is not None:
            row.current_period_end = period_end
            row.updated_at = now_utc()
            await session.flush()
        else:
            await monetize.upsert_subscription(
                session,
                user_id=user_id,
                subscription_id=f"sub-{user_id}",
                chat_id=None,
                tier="supporter",
                status="active",
                amount=0,
                currency="XTR",
                current_period_end=period_end,
            )
        return period_end

    @staticmethod
    async def paid_media_history(session: AsyncSession, user_id: int) -> list[dict[str, Any]]:
        """Every paid preview this player unlocked — /unlocks reads this."""
        rows = (
            await session.execute(
                select(StarPurchase)
                .where(
                    StarPurchase.user_id == user_id,
                    StarPurchase.source == "paid_media",
                    StarPurchase.status.in_(["paid", "delivered"]),
                )
                .order_by(StarPurchase.id.desc())
                .limit(50)
            )
        ).scalars()
        return [
            {
                "character_id": int(r.character_id or 0),
                "stars": int(r.star_count),
                "payload": r.invoice_payload,
                "at": r.paid_at or r.created_at,
            }
            for r in rows
        ]


class progress:
    """Progression: pity counters, streaks, achievements, and the fair-roll audit.

    ``record_roll`` is what makes "/hstats → Verify" real: every claim writes the
    commitment shown to the player *before* the roll plus the seed reveal after, so
    the outcome can be recomputed from (seed, user_id, sequence). Summon-bot's rolls
    were opaque ``random.choice()`` calls with no record at all.
    """

    PityState = PityState
    StreakOutcome = StreakOutcome

    @staticmethod
    async def pity(session: AsyncSession, user_id: int) -> PityState:
        row = (
            await session.execute(
                select(User.pity_rare, User.pity_high, User.pity_celestial, User.pulls_total).where(
                    User.id == user_id
                )
            )
        ).first()
        if row is None:
            return PityState(0, 0, 0, 0)
        return PityState(int(row[0]), int(row[1]), int(row[2]), int(row[3]))

    @staticmethod
    async def apply_pity(
        session: AsyncSession, user_id: int, *, got_rarity_id: int, rare_at: int, high_at: int
    ) -> None:
        """Reset the counters the pulled rarity satisfies; increment the rest.

        Single UPDATE with expressions — never read-modify-write.
        """
        tier = Rarity.from_value(got_rarity_id)
        values = {"pulls_total": User.pulls_total + 1}
        values["pity_rare"] = 0 if tier.value >= rare_at else User.pity_rare + 1
        values["pity_high"] = 0 if tier.value >= high_at else User.pity_high + 1
        values["pity_celestial"] = 0 if tier is Rarity.CELESTIAL else User.pity_celestial + 1
        values["high_pulls"] = User.high_pulls + (1 if tier.value >= high_at else 0)
        hist = select(User.rarity_histogram).where(User.id == user_id)
        current = (await session.execute(hist)).scalar_one_or_none() or {}
        updated = {**current, str(int(tier)): int(current.get(str(int(tier)), 0)) + 1}
        values["rarity_histogram"] = updated
        await session.execute(update(User).where(User.id == user_id).values(**values))
        await session.flush()

    @staticmethod
    async def record_roll(
        session: AsyncSession,
        *,
        user_id: int,
        sequence: int,
        index_in_batch: int,
        kind: str,
        commitment: str,
        seed: str,
        roll_value: float,
        rarity_id: int,
        character_id: int | None,
        was_dupe: bool,
        was_pity: bool,
        payout: int,
    ) -> FairRoll:
        row = FairRoll(
            user_id=user_id,
            sequence=sequence,
            index_in_batch=index_in_batch,
            kind=kind,
            commitment=commitment,
            seed_reveal=seed,
            roll_value=roll_value,
            rarity_id=int(rarity_id),
            character_id=character_id,
            was_dupe=was_dupe,
            was_pity=was_pity,
            payout=payout,
            created_at=now_utc(),
        )
        async with session.begin_nested():
            session.add(row)
            try:
                await session.flush()
            except IntegrityError:
                existing = (
                    await session.execute(
                        select(FairRoll).where(
                            FairRoll.user_id == user_id,
                            FairRoll.sequence == sequence,
                            FairRoll.index_in_batch == index_in_batch,
                        )
                    )
                ).scalar_one()
                return existing
        return row

    @staticmethod
    async def rotate_seed(session: AsyncSession, user_id: int) -> str:
        """After a reveal, issue a fresh seed and publish only its commitment."""
        import secrets

        seed = secrets.token_hex(16)
        await session.execute(
            update(User)
            .where(User.id == user_id)
            .values(server_seed=seed, seed_commitment=commit(seed))
        )
        await session.flush()
        return commit(seed)

    @staticmethod
    async def roll_rows(session: AsyncSession, user_id: int, *, limit: int = 20) -> list[FairRoll]:
        return list(
            (
                await session.execute(
                    select(FairRoll)
                    .where(FairRoll.user_id == user_id)
                    .order_by(FairRoll.id.desc())
                    .limit(limit)
                )
            ).scalars()
        )

    @staticmethod
    async def roll_detail(session: AsyncSession, user_id: int, sequence: int) -> list[FairRoll]:
        return list(
            (
                await session.execute(
                    select(FairRoll)
                    .where(FairRoll.user_id == user_id, FairRoll.sequence == sequence)
                    .order_by(FairRoll.index_in_batch)
                )
            ).scalars()
        )

    @staticmethod
    async def next_sequence(session: AsyncSession, user_id: int) -> int:
        await session.execute(
            update(User).where(User.id == user_id).values(roll_sequence=User.roll_sequence + 1)
        )
        value = (
            await session.execute(select(User.roll_sequence).where(User.id == user_id))
        ).scalar_one()
        await session.flush()
        return int(value)

    @staticmethod
    def recompute(seed: str, user_id: int, sequence: int, salt: str = "") -> float:
        """Verification without a DB (also used by ``waifu verify``)."""
        return derive_roll(seed=seed, player_id=user_id, sequence=sequence, salt=salt)

    @staticmethod
    def commitment_for(seed: str) -> str:
        return hashlib.sha256(seed.encode()).hexdigest()

    @staticmethod
    async def roll_stats(session: AsyncSession, user_id: int) -> dict[str, float | int]:
        row = (
            (
                await session.execute(
                    select(
                        func.count(FairRoll.id).label("rolls"),
                        func.count(FairRoll.id).filter(FairRoll.was_pity.is_(True)).label("pity"),
                        func.count(FairRoll.id).filter(FairRoll.was_dupe.is_(True)).label("dupes"),
                        func.max(FairRoll.rarity_id).label("best"),
                    ).where(FairRoll.user_id == user_id)
                )
            )
            .mappings()
            .one()
        )
        return {
            "rolls": int(row["rolls"] or 0),
            "pity_hits": int(row["pity"] or 0),
            "dupes": int(row["dupes"] or 0),
            "best_rarity": int(row["best"] or 0),
        }

    @staticmethod
    async def global_roll_stats(session: AsyncSession, *, since=None) -> dict[str, float]:
        conds = []
        if since:
            conds.append(FairRoll.created_at >= since)
        rows = (
            await session.execute(
                select(FairRoll.rarity_id, func.count(FairRoll.id))
                .where(*conds)
                .group_by(FairRoll.rarity_id)
            )
        ).all()
        counts = {int(r): int(c) for r, c in rows}
        total = sum(counts.values()) or 1
        return {f"r{key}": counts[key] / total * 100 for key in sorted(counts)} | {
            "total": float(total)
        }

    @staticmethod
    async def streak(session: AsyncSession, user_id: int) -> Streak:
        found = (
            (await session.execute(select(Streak).where(Streak.user_id == user_id)))
            .scalars()
            .first()
        )
        if found is None:
            found = Streak(user_id=user_id, current=0, highest=0, last_date="", freezes=0)
            session.add(found)
            await session.flush()
        return found

    @staticmethod
    async def bump_streak(
        session: AsyncSession, user_id: int, today: str, *, multiplier_curve: list[float]
    ) -> StreakOutcome:
        """Advance the streak for ``today`` (``YYYY-MM-DD`` in the server timezone).

        Yesterday → +1, today → no change (already claimed), older → reset to 1
        unless the player has a freeze, in which case the gap is bridged once.
        """
        from datetime import date, timedelta

        state = await progress.streak(session, user_id)
        if state.last_date == today:
            mult = progress._multiplier(state.current, multiplier_curve)
            return StreakOutcome(state.current, state.highest, False, mult)
        yesterday = (date.fromisoformat(today) - timedelta(days=1)).isoformat()
        broke = False
        if state.last_date == yesterday:
            state.current += 1
        elif state.freezes > 0 and state.last_date:
            state.freezes -= 1
            state.current += 1
        else:
            broke = state.current > 0
            state.current = 1
        state.last_date = today
        state.highest = max(state.highest, state.current)
        await session.execute(
            update(User)
            .where(User.id == user_id)
            .values(streak_count=state.current, streak_best=state.highest, streak_last_date=today)
        )
        await session.flush()
        return StreakOutcome(
            state.current,
            state.highest,
            broke,
            progress._multiplier(state.current, multiplier_curve),
        )

    @staticmethod
    def _multiplier(current: int, curve: list[float]) -> float:
        if not curve:
            return 1.0
        return float(curve[min(current, len(curve)) - 1] if current else 1.0)

    @staticmethod
    async def reset_streaks(session: AsyncSession) -> int:
        """Nightly sweep: anyone who missed yesterday loses the streak (freezes aside)."""
        from datetime import timedelta

        from waifu.utils.time import now_utc

        yesterday = (now_utc().date() - timedelta(days=1)).isoformat()
        result = await session.execute(
            update(Streak)
            .where(Streak.last_date < yesterday, Streak.current > 0, Streak.freezes == 0)
            .values(current=0)
        )
        return int(result.rowcount or 0)

    @staticmethod
    async def add_freeze(session: AsyncSession, user_id: int, count: int = 1) -> int:
        """Grant /spend streak freezes: one freeze bridges a single missed day."""
        state = await progress.streak(session, user_id)
        state.freezes += count
        await session.flush()
        return state.freezes

    @staticmethod
    async def spend_freeze(session: AsyncSession, user_id: int) -> bool:
        state = await progress.streak(session, user_id)
        if state.freezes <= 0:
            return False
        state.freezes -= 1
        from datetime import date, timedelta

        from waifu.utils.time import now_utc

        state.last_date = (
            date.fromisoformat(state.last_date or now_utc().date().isoformat()) + timedelta(days=1)
        ).isoformat()
        await session.flush()
        return True

    @staticmethod
    async def already_claimed(
        session: AsyncSession, user_id: int, kind: str, local_day: str
    ) -> bool:
        found = (
            await session.execute(
                select(DailyClaim.id).where(
                    DailyClaim.user_id == user_id,
                    DailyClaim.kind == kind,
                    DailyClaim.local_day == local_day,
                )
            )
        ).scalar_one_or_none()
        return found is not None

    @staticmethod
    async def mark_claimed(
        session: AsyncSession, user_id: int, kind: str, local_day: str, *, amount: int = 0
    ) -> bool:
        async with session.begin_nested():
            session.add(
                DailyClaim(
                    user_id=user_id,
                    kind=kind,
                    local_day=local_day,
                    amount=amount,
                    created_at=now_utc(),
                )
            )
            try:
                await session.flush()
            except IntegrityError:
                return False
        return True

    @staticmethod
    async def unlocked(session: AsyncSession, user_id: int) -> set[str]:
        rows = (
            await session.execute(
                select(Achievement.achievement_id).where(
                    Achievement.user_id == user_id, Achievement.unlocked_at.is_not(None)
                )
            )
        ).scalars()
        return {str(r) for r in rows}

    @staticmethod
    async def progress_rows(session: AsyncSession, user_id: int) -> dict[str, int]:
        rows = (
            await session.execute(
                select(Achievement.achievement_id, Achievement.progress).where(
                    Achievement.user_id == user_id, Achievement.unlocked_at.is_(None)
                )
            )
        ).all()
        return {str(r[0]): int(r[1]) for r in rows}

    @staticmethod
    async def unlock(session: AsyncSession, user_id: int, key: str, *, progress: int = 0) -> bool:
        """Idempotent (unique constraint): True only on the first unlock → notify once."""
        existing = (
            await session.execute(
                select(Achievement).where(
                    Achievement.user_id == user_id, Achievement.achievement_id == key
                )
            )
        ).scalar_one_or_none()
        if existing is not None and existing.unlocked_at is not None:
            return False
        if existing is None:
            session.add(
                Achievement(
                    user_id=user_id, achievement_id=key, progress=progress, unlocked_at=now_utc()
                )
            )
        else:
            existing.unlocked_at = now_utc()
            existing.progress = progress
        async with session.begin_nested():
            try:
                await session.flush()
            except IntegrityError:
                return False
        return True

    @staticmethod
    async def set_progress(session: AsyncSession, user_id: int, key: str, progress: int) -> None:
        existing = (
            await session.execute(
                select(Achievement).where(
                    Achievement.user_id == user_id, Achievement.achievement_id == key
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            session.add(Achievement(user_id=user_id, achievement_id=key, progress=progress))
        else:
            existing.progress = progress
        await session.flush()

    @staticmethod
    async def unlock_count_map(session: AsyncSession) -> dict[str, int]:
        """Achievement holder counts for every key, in one ``GROUP BY``.

        ``/achievements`` renders how rare each badge is; asking once per badge was 17
        queries per page view.
        """
        rows = (
            await session.execute(
                select(Achievement.achievement_id, func.count(Achievement.id))
                .where(Achievement.unlocked_at.is_not(None))
                .group_by(Achievement.achievement_id)
            )
        ).all()
        return {str(key): int(count) for key, count in rows}

    @staticmethod
    async def unlock_counts(session: AsyncSession, key: str) -> int:
        return int(
            (
                await session.execute(
                    select(func.count(Achievement.id)).where(Achievement.achievement_id == key)
                )
            ).scalar_one()
            or 0
        )

    @staticmethod
    async def rarest_unlocks(session: AsyncSession, *, limit: int = 10) -> list[tuple[str, int]]:
        rows = (
            await session.execute(
                select(Achievement.achievement_id, func.count(Achievement.id).label("holders"))
                .where(Achievement.unlocked_at.is_not(None))
                .group_by(Achievement.achievement_id)
                .order_by(func.count(Achievement.id).asc())
                .limit(limit)
            )
        ).all()
        return [(str(r[0]), int(r[1])) for r in rows]

    @staticmethod
    async def claim_bonus(session: AsyncSession, user_id: int, key: str, amount: int) -> None:
        """Achievement coin payouts are single-shot via the daily-claim unique index."""
        ok = await progress.mark_claimed(session, user_id, f"ach:{key}", "lifetime", amount=amount)
        if not ok:
            raise AlreadyClaimed("bonus already collected")


class spawns:
    """Group spawns: the /spawn + auto-spawn + /summon claim race.

    The claim is the interesting part. Summon-bot kept the active spawn in
    ``chat_data`` (in-process, lost on restart, and not shared between shards) and
    resolved ``/summon`` by matching a name against that dict, so two players typing
    at once both got the character. Here the spawn is a **row**, and claiming is
    ``UPDATE … WHERE status='active'`` with ``rowcount`` deciding the winner: exactly
    one /summon wins, the loser is told they were too slow, and the state survives a
    restart.
    """

    Spawn = Spawn

    @staticmethod
    def _hydrate(row: SpawnEvent, char: Character) -> Spawn:
        return Spawn(
            id=row.id,
            chat_id=row.chat_id,
            character_id=char.id,
            name=char.name,
            anime=char.anime,
            rarity_id=int(char.rarity_id),
            rarity=char.rarity,
            image=char.image_ref(),
            message_id=row.message_id,
            rich_message=bool(row.rich_message),
            source=row.source,
            expected_name=row.expected_name,
            expires_at=row.expires_at,
            hint_used=bool(row.hint_used),
            status=row.status,
        )

    @staticmethod
    async def open_spawn(
        session: AsyncSession,
        *,
        chat_id: int,
        character_id: int,
        source: str = "manual",
        ttl_seconds: int = 180,
        payload: dict | None = None,
    ) -> SpawnEvent:
        """Create the spawn row. Any previous *active* spawn in that chat is expired."""
        await session.execute(
            update(SpawnEvent)
            .where(SpawnEvent.chat_id == chat_id, SpawnEvent.status == "active")
            .values(status="expired")
        )
        char = await session.get(Character, character_id)
        row = SpawnEvent(
            chat_id=chat_id,
            character_id=character_id,
            source=source,
            status="active",
            expected_name=strip_md(char.name).lower() if char else "",
            expires_at=now_utc() + timedelta(seconds=ttl_seconds),
            payload=payload or {},
        )
        session.add(row)
        await session.flush()
        return row

    @staticmethod
    async def attach_message(
        session: AsyncSession, spawn_id: int, message_id: int, *, rich: bool
    ) -> None:
        await session.execute(
            update(SpawnEvent)
            .where(SpawnEvent.id == spawn_id)
            .values(message_id=message_id, rich_message=rich)
        )
        await session.flush()

    @staticmethod
    async def current(
        session: AsyncSession, chat_id: int
    ) -> tuple[Spawn | None, SpawnEvent | None]:
        pair = (
            await session.execute(
                select(SpawnEvent, Character)
                .join(Character, Character.id == SpawnEvent.character_id)
                .where(
                    SpawnEvent.chat_id == chat_id,
                    SpawnEvent.status == "active",
                    SpawnEvent.expires_at > now_utc(),
                )
                .order_by(SpawnEvent.id.desc())
                .limit(1)
            )
        ).first()
        if pair is None:
            return (None, None)
        return (spawns._hydrate(pair[0], pair[1]), pair[0])

    @staticmethod
    async def by_id(session: AsyncSession, spawn_id: int) -> Spawn | None:
        pair = (
            await session.execute(
                select(SpawnEvent, Character)
                .join(Character, Character.id == SpawnEvent.character_id)
                .where(SpawnEvent.id == spawn_id)
            )
        ).first()
        return spawns._hydrate(pair[0], pair[1]) if pair else None

    @staticmethod
    async def mark_hint_used(session: AsyncSession, spawn_id: int) -> bool:
        """One hint per spawn — CAS on ``hint_used`` so two taps can't both consume it."""
        result = await session.execute(
            update(SpawnEvent)
            .where(
                SpawnEvent.id == spawn_id,
                SpawnEvent.hint_used.is_(False),
                SpawnEvent.status == "active",
            )
            .values(hint_used=True)
        )
        await session.flush()
        return bool(result.rowcount)

    @staticmethod
    async def claim(session: AsyncSession, spawn_id: int, user_id: int) -> Spawn:
        """The race: only one UPDATE can flip ``active`` → ``claimed``."""
        result = await session.execute(
            update(SpawnEvent)
            .where(
                SpawnEvent.id == spawn_id,
                SpawnEvent.status == "active",
                SpawnEvent.expires_at > now_utc(),
            )
            .values(status="claimed", claimed_by=user_id, claimed_at=now_utc())
            .returning(SpawnEvent.chat_id)
        )
        if result.scalar_one_or_none() is None:
            row = await session.get(SpawnEvent, spawn_id)
            if row is None:
                raise NotFound("that spawn is gone")
            raise AlreadyClaimed(
                "someone reached it first" if row.status == "claimed" else "this spawn has expired"
            )
        pair = (
            await session.execute(
                select(SpawnEvent, Character)
                .join(Character, Character.id == SpawnEvent.character_id)
                .where(SpawnEvent.id == spawn_id)
            )
        ).first()
        assert pair is not None
        return spawns._hydrate(pair[0], pair[1])

    @staticmethod
    async def name_matches(spawn: Spawn, typed: str) -> bool:
        """Summon-bot accepted partial/substring guesses; keep that, but require ≥3 chars."""
        typed = strip_md(typed).strip().lower()
        if not typed:
            return False
        target = spawn.expected_name.lower().strip()
        return typed == target or (
            len(typed) >= 3 and (typed in target or target.startswith(typed))
        )

    @staticmethod
    async def expire_overdue(session: AsyncSession) -> int:
        result = await session.execute(
            update(SpawnEvent)
            .where(SpawnEvent.status == "active", SpawnEvent.expires_at <= now_utc())
            .values(status="expired")
        )
        return int(result.rowcount or 0)

    @staticmethod
    async def recent_for_chat(
        session: AsyncSession, chat_id: int, *, limit: int = 5
    ) -> list[SpawnEvent]:
        return list(
            (
                await session.execute(
                    select(SpawnEvent)
                    .where(SpawnEvent.chat_id == chat_id)
                    .order_by(SpawnEvent.id.desc())
                    .limit(limit)
                )
            ).scalars()
        )

    @staticmethod
    async def chat_activity(
        session: AsyncSession, chat_id: int, *, since: datetime | None = None
    ) -> dict[str, int]:
        conds = [SpawnEvent.chat_id == chat_id]
        if since:
            conds.append(SpawnEvent.spawns_at >= since)
        row = (
            (
                await session.execute(
                    select(
                        func.count(SpawnEvent.id).label("spawns"),
                        func.count(SpawnEvent.id)
                        .filter(SpawnEvent.status == "claimed")
                        .label("claimed"),
                    ).where(*conds)
                )
            )
            .mappings()
            .one()
        )
        spawns, claimed = (int(row["spawns"] or 0), int(row["claimed"] or 0))
        return {
            "spawns": spawns,
            "claimed": claimed,
            "claim_rate": round(claimed / spawns * 100) if spawns else 0,
        }

    @staticmethod
    async def group(
        session: AsyncSession, chat_id: int, *, create: bool = False, title: str = ""
    ) -> Group | None:
        row = await session.get(Group, chat_id)
        if row is None and create:
            row = Group(chat_id=chat_id, title=title[:255], message_count=0)
            session.add(row)
            await session.flush()
        elif row is not None and title and (row.title != title):
            row.title = title[:255]
            await session.flush()
        return row

    @staticmethod
    def _switches(row: Group) -> dict[str, bool]:
        return dict((row.data or {}).get("switches") or {})

    @staticmethod
    async def set_group_switch(
        session: AsyncSession, chat_id: int, key: str, *, value: bool, title: str = ""
    ) -> dict[str, bool]:
        """Flip a free-form per-group switch and return the whole set.

        ``set_group_flags`` deliberately refuses unknown keys, because a typo in
        ``/setgroup`` otherwise reads as "it didn't work". A switch with no column — the
        auto-add feed — lives in ``Group.data`` instead of forcing a migration for a
        boolean nobody queries.
        """
        row = await spawns.group(session, chat_id, create=True, title=title)
        if row is None:
            raise NotFound("group row missing")
        data = dict(row.data or {})
        switches = spawns._switches(row)
        switches[str(key)] = bool(value)
        data["switches"] = switches
        row.data = data
        await session.flush()
        return switches

    @staticmethod
    async def group_switch(
        session: AsyncSession, chat_id: int, key: str, *, default: bool = False
    ) -> bool:
        row = await session.get(Group, chat_id)
        if row is None:
            return default
        return bool(spawns._switches(row).get(key, default))

    @staticmethod
    async def bump_message_count(
        session: AsyncSession, chat_id: int, *, spawn_limit_default: int = 100
    ) -> tuple[int, int, bool]:
        """Count group chatter; returns ``(count, limit, threshold_hit)``.

        Replaces Summon-bot's ``UPDATE … SET message_count = <python int>`` (which
        lost counts under load) with a single atomic increment.
        """
        from sqlalchemy import text as sa_text

        result = await session.execute(
            update(Group)
            .where(Group.chat_id == chat_id, Group.spawn_enabled.is_(True))
            .values(message_count=Group.message_count + 1)
            .returning(Group.message_count, Group.spawn_limit)
        )
        row = result.first()
        if row is None:
            await session.execute(
                sa_text(
                    "INSERT INTO groups (chat_id, title, message_count, spawn_limit, is_registered, spawn_enabled, auto_ban_spam, spam_limit, welcome_enabled, created_at, updated_at) VALUES (:cid, '', 1, :lim, true, true, true, 20, false, now(), now()) ON CONFLICT (chat_id) DO NOTHING"
                ),
                {"cid": chat_id, "lim": spawn_limit_default},
            )
            await session.flush()
            return (1, spawn_limit_default, False)
        count, limit = (int(row[0]), int(row[1]))
        hit = count >= limit
        if hit:
            await session.execute(
                update(Group).where(Group.chat_id == chat_id).values(message_count=0)
            )
            await session.flush()
        return (count, limit, hit)

    @staticmethod
    async def set_spawn_limit(session: AsyncSession, chat_id: int, limit: int) -> None:
        await session.execute(
            update(Group)
            .where(Group.chat_id == chat_id)
            .values(spawn_limit=max(5, int(limit)), message_count=0)
        )
        await session.flush()

    @staticmethod
    async def schedule_next_spawn(session: AsyncSession, chat_id: int, when: datetime) -> None:
        await session.execute(
            update(Group)
            .where(Group.chat_id == chat_id)
            .values(next_spawn_at=when, last_spawn_at=now_utc(), message_count=0)
        )
        await session.flush()

    @staticmethod
    async def spawnable_groups(session: AsyncSession, *, due_only: bool = False) -> list[Group]:
        conds = [Group.is_registered.is_(True), Group.spawn_enabled.is_(True)]
        if due_only:
            conds.append(Group.next_spawn_at.is_not(None))
        return list(
            (
                await session.execute(
                    select(Group).where(*conds).order_by(Group.next_spawn_at.nulls_last())
                )
            ).scalars()
        )

    @staticmethod
    async def register_group(
        session: AsyncSession, chat_id: int, *, title: str = "", spawn_limit: int = 100
    ) -> Group:
        row = await spawns.group(session, chat_id, create=True, title=title)
        assert row is not None
        row.is_registered = True
        row.spawn_limit = max(5, int(spawn_limit))
        await session.flush()
        return row

    @staticmethod
    async def unregister_group(session: AsyncSession, chat_id: int) -> None:
        await session.execute(Group.__table__.delete().where(Group.chat_id == chat_id))

    @staticmethod
    async def groups_summary(session: AsyncSession) -> dict[str, int]:
        row = (
            (
                await session.execute(
                    select(
                        func.count(Group.chat_id).label("total"),
                        func.count(Group.chat_id)
                        .filter(Group.spawn_enabled.is_(True))
                        .label("spawning"),
                        func.coalesce(func.sum(Group.message_count), 0).label("messages"),
                    )
                )
            )
            .mappings()
            .one()
        )
        return {
            "groups": int(row["total"] or 0),
            "spawning": int(row["spawning"] or 0),
            "messages": int(row["messages"] or 0),
        }

    @staticmethod
    async def start_guess(
        session: AsyncSession,
        *,
        chat_id: int,
        character_id: int,
        reward: int,
        seconds: int,
        mode: str = "poll",
    ) -> GuessSession:
        await session.execute(
            GuessSession.__table__.delete().where(
                GuessSession.chat_id == chat_id, GuessSession.status == "open"
            )
        )
        row = GuessSession(
            chat_id=chat_id,
            character_id=character_id,
            status="open",
            mode=mode,
            reward=reward,
            closes_at=now_utc() + timedelta(seconds=seconds),
        )
        session.add(row)
        await session.flush()
        return row

    @staticmethod
    async def active_guess(session: AsyncSession, chat_id: int) -> GuessSession | None:
        return (
            await session.execute(
                select(GuessSession)
                .where(
                    GuessSession.chat_id == chat_id,
                    GuessSession.status == "open",
                    GuessSession.closes_at > now_utc(),
                )
                .limit(1)
            )
        ).scalar_one_or_none()

    @staticmethod
    async def attach_guess_message(session: AsyncSession, session_id: int, message_id: int) -> None:
        await session.execute(
            update(GuessSession).where(GuessSession.id == session_id).values(message_id=message_id)
        )
        await session.flush()

    @staticmethod
    async def resolve_guess(
        session: AsyncSession, session_id: int, *, winner_id: int | None, answers: int = 0
    ) -> GuessSession:
        row = await session.get(GuessSession, session_id)
        if row is None:
            raise NotFound("round is gone")
        row.status = "won" if winner_id else "expired"
        row.winner_id = winner_id
        row.answers = answers
        await session.flush()
        return row

    @staticmethod
    async def claim_guess(session: AsyncSession, session_id: int, user_id: int) -> GuessSession:
        """First correct answer wins — same CAS idea as the spawn claim."""
        result = await session.execute(
            update(GuessSession)
            .where(
                GuessSession.id == session_id,
                GuessSession.status == "open",
                GuessSession.closes_at > now_utc(),
            )
            .values(status="won", winner_id=user_id)
            .returning(GuessSession.reward)
        )
        reward = result.scalar_one_or_none()
        if reward is None:
            raise AlreadyClaimed("someone already won this round")
        row = await session.get(GuessSession, session_id)
        assert row is not None
        await session.flush()
        return row

    @staticmethod
    async def guesses_to_close(session: AsyncSession) -> list[GuessSession]:
        return list(
            (
                await session.execute(
                    select(GuessSession).where(
                        GuessSession.status == "open", GuessSession.closes_at <= now_utc()
                    )
                )
            ).scalars()
        )

    @staticmethod
    async def guess_streak(session: AsyncSession, chat_id: int) -> tuple[int, int | None]:
        row = await session.get(GuessStreak, chat_id)
        return (row.current_streak, row.last_correct_user) if row else (0, None)

    @staticmethod
    async def bump_guess_streak(
        session: AsyncSession, chat_id: int, *, user_id: int, won: bool
    ) -> int:
        row = await session.get(GuessStreak, chat_id)
        if row is None:
            row = GuessStreak(chat_id=chat_id, current_streak=0, total_rounds=0)
            session.add(row)
        row.total_rounds += 1
        if won:
            row.current_streak = row.current_streak + 1 if row.last_correct_user == user_id else 1
            row.last_correct_user = user_id
        else:
            row.current_streak = 0
        await session.flush()
        return row.current_streak

    @staticmethod
    async def last_guess_characters(
        session: AsyncSession, chat_id: int, *, limit: int = 8
    ) -> list[int]:
        rows = (
            await session.execute(
                select(GuessSession.character_id)
                .where(GuessSession.chat_id == chat_id)
                .order_by(GuessSession.id.desc())
                .limit(limit)
            )
        ).scalars()
        return [int(r) for r in rows]

    @staticmethod
    async def users_seen_in(session: AsyncSession, chat_id: int, *, since: datetime) -> int:
        from waifu.db.models import ActivityLog

        return int(
            (
                await session.execute(
                    select(func.count(func.distinct(ActivityLog.user_id))).where(
                        ActivityLog.chat_id == chat_id, ActivityLog.created_at >= since
                    )
                )
            ).scalar_one()
            or 0
        )

    @staticmethod
    async def top_guessers(session: AsyncSession, *, limit: int = 10) -> list[tuple[User, int]]:
        rows = (
            await session.execute(
                select(User, func.count(GuessSession.id).label("wins"))
                .join(GuessSession, GuessSession.winner_id == User.id)
                .group_by(User.id)
                .order_by(func.count(GuessSession.id).desc())
                .limit(limit)
            )
        ).all()
        return [(r[0], int(r[1])) for r in rows]


class stats:
    """Metrics, snapshots and job checkpoints.

    Two-tier design (this is what makes ``/stats`` instant instead of a table scan):

    * **live counters** — Redis hashes/ZSETs, incremented on the hot path;
    * **snapshots** — a hourly Postgres row, which is what the dashboard graphs and
      ``/stats`` trend line read; Redis may be flushed, history must not.
    """

    @staticmethod
    async def snapshot(session: AsyncSession, **fields: object) -> StatsSnapshot:
        row = StatsSnapshot(**fields)
        session.add(row)
        await session.flush()
        return row

    @staticmethod
    async def snapshots(session: AsyncSession, *, limit: int = 48) -> list[StatsSnapshot]:
        rows = (
            await session.execute(
                select(StatsSnapshot).order_by(StatsSnapshot.ts.desc()).limit(limit)
            )
        ).scalars()
        return list(rows)

    @staticmethod
    async def trend(session: AsyncSession, *, hours: int = 24) -> list[tuple[str, int, int]]:
        rows = (
            await session.execute(
                select(StatsSnapshot.ts, StatsSnapshot.users_active_24h, StatsSnapshot.claims_total)
                .where(StatsSnapshot.ts >= now_utc() - timedelta(hours=hours))
                .order_by(StatsSnapshot.ts.asc())
            )
        ).all()
        return [(r[0].strftime("%m-%d %H:%M"), int(r[1] or 0), int(r[2] or 0)) for r in rows]

    @staticmethod
    async def retention(session: AsyncSession, *, days: int = 7) -> dict[str, float]:
        """Rough retention: share of users from cohort day D who were seen within ``days``."""
        cutoff = now_utc() - timedelta(days=days)
        row = (
            (
                await session.execute(
                    select(
                        func.count(User.id).label("cohort"),
                        func.count(User.id).filter(User.last_seen_at >= cutoff).label("retained"),
                    ).where(User.created_at >= cutoff)
                )
            )
            .mappings()
            .one()
        )
        cohort = int(row["cohort"] or 0)
        retained = int(row["retained"] or 0)
        return {
            "cohort": cohort,
            "retained": retained,
            "rate": round(retained / cohort * 100, 1) if cohort else 0.0,
        }

    @staticmethod
    async def top_commands(
        session: AsyncSession, *, hours: int = 24, limit: int = 10
    ) -> list[tuple[str, int]]:
        """Aggregate the durable activity log by ``kind`` (kind stores ``cmd:/daily``)."""
        since = now_utc() - timedelta(hours=hours)
        rows = (
            await session.execute(
                select(ActivityLog.kind, func.count(ActivityLog.id))
                .where(ActivityLog.created_at >= since, ActivityLog.kind.like("cmd:%"))
                .group_by(ActivityLog.kind)
                .order_by(func.count(ActivityLog.id).desc())
                .limit(limit)
            )
        ).all()
        return [(str(r[0]).replace("cmd:", "/"), int(r[1])) for r in rows]

    @staticmethod
    async def chat_activity(
        session: AsyncSession, chat_id: int, *, hours: int = 24
    ) -> dict[str, int]:
        since = now_utc() - timedelta(hours=hours)
        row = (
            (
                await session.execute(
                    select(
                        func.count(ActivityLog.id).label("msgs"),
                        func.count(func.distinct(ActivityLog.user_id)).label("people"),
                    ).where(ActivityLog.chat_id == chat_id, ActivityLog.created_at >= since)
                )
            )
            .mappings()
            .one()
        )
        return {"messages": int(row["msgs"] or 0), "people": int(row["people"] or 0)}

    @staticmethod
    async def global_activity(session: AsyncSession, *, hours: int = 24) -> dict[str, int]:
        since = now_utc() - timedelta(hours=hours)
        row = (
            (
                await session.execute(
                    select(
                        func.count(ActivityLog.id).label("msgs"),
                        func.count(func.distinct(ActivityLog.user_id)).label("people"),
                        func.count(func.distinct(ActivityLog.chat_id)).label("chats"),
                    ).where(ActivityLog.created_at >= since)
                )
            )
            .mappings()
            .one()
        )
        return {
            "messages": int(row["msgs"] or 0),
            "people": int(row["people"] or 0),
            "chats": int(row["chats"] or 0),
        }

    @staticmethod
    async def purge_activity(session: AsyncSession, *, older_than_days: int = 14) -> int:
        cutoff = now_utc() - timedelta(days=older_than_days)
        result = await session.execute(
            ActivityLog.__table__.delete().where(ActivityLog.created_at < cutoff)
        )
        return int(result.rowcount or 0)

    @staticmethod
    async def kv_get(session: AsyncSession, key: str, default: dict | None = None) -> dict:
        row = await session.get(KvState, key)
        if row is None:
            return default or {}
        return dict(row.value or {})

    @staticmethod
    async def kv_set(session: AsyncSession, key: str, value: dict) -> None:
        row = await session.get(KvState, key)
        if row is None:
            session.add(KvState(key=key, value=value))
        else:
            row.value = value
        await session.flush()

    @staticmethod
    async def kv_bump(session: AsyncSession, key: str, field: str, amount: int = 1) -> int:
        """Counter stored inside the JSONB doc (used for job bookkeeping)."""
        row = await session.get(KvState, key)
        value = dict((row.value if row else None) or {})
        value[field] = int(value.get(field, 0)) + amount
        if row is None:
            session.add(KvState(key=key, value=value))
        else:
            row.value = value
        await session.flush()
        return int(value[field])

    @staticmethod
    async def table_sizes(session: AsyncSession) -> list[tuple[str, int]]:
        """Postgres-only; empty on the SQLite test harness."""
        try:
            rows = (
                await session.execute(
                    text(
                        "SELECT relname, pg_total_relation_size(c.oid) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relkind = 'r' ORDER BY 2 DESC LIMIT 20"
                    )
                )
            ).all()
        except Exception:
            return []
        return [(str(r[0]), int(r[1])) for r in rows]

    @staticmethod
    async def vacuum_analyze(session: AsyncSession, tables: list[str]) -> list[str]:
        """Cheap maintenance the owner can trigger from the /owner panel."""
        done: list[str] = []
        allowed = {t.name for t in StatsSnapshot.__table__.metadata.tables.values()}
        for table in tables:
            if table not in allowed:
                continue
            await session.execute(text(f"ANALYZE {table}"))
            done.append(table)
        return done

    @staticmethod
    async def week_summary(session: AsyncSession, *, since: object) -> dict[str, int]:
        """The week in six numbers — the material for the owner's weekly digest.

        Every count is one indexed range scan (each table's ``created_at`` is
        indexed), so the digest costs about nothing on a mid-size database.
        """
        from waifu.db.models import FairRoll, GiftLog, RaffleRound

        async def _count(stmt) -> int:
            value = await session.execute(stmt)
            return int(value.scalar_one() or 0)

        new_players = await _count(select(func.count(User.id)).where(User.created_at >= since))
        gifts = await _count(select(func.count(GiftLog.id)).where(GiftLog.created_at >= since))
        raffles = await _count(
            select(func.count(RaffleRound.id)).where(
                RaffleRound.status == "drawn", RaffleRound.ends_at >= since
            )
        )
        pulls = await _count(select(func.count(FairRoll.id)).where(FairRoll.created_at >= since))
        subs = await _count(select(func.count(User.id)).where(User.premium_until >= now_utc()))
        return {
            "new_players": new_players,
            "gifts": gifts,
            "raffles": raffles,
            "pulls": pulls,
            "subs_active": subs,
        }


class trades:
    """Redeem codes, gifts and escrow trade offers.

    Trades are a *feature of /gift* (buttons), not a new command: ``/gift @user``
    either hands a character over for free or opens an escrow offer when the other
    side is asked to add something. Both sides must accept before anything moves,
    and the confirmation lives in an **ephemeral** private message (Bot API 10.3),
    so the negotiation isn't shouted in the group.
    """

    @staticmethod
    async def create(
        session: AsyncSession,
        *,
        created_by: int,
        code: str | None = None,
        coins: int = 0,
        reward: int = 0,
        character_id: int | None = None,
        premium_hours: int = 0,
        uses: int = 1,
        hours: int = 72,
        note: str = "",
    ) -> RedeemCode:
        row = RedeemCode(
            code=(code or redeem_code()).upper(),
            coins=max(0, int(coins)),
            reward=max(0, int(reward)),
            character_id=character_id,
            premium_hours=max(0, int(premium_hours)),
            uses=max(1, int(uses)),
            created_by=created_by,
            note=note[:140],
            expires_at=now_utc() + timedelta(hours=hours) if hours else None,
        )
        async with session.begin_nested():
            session.add(row)
            try:
                await session.flush()
            except IntegrityError:
                raise Locked("that code already exists") from None
        return row

    @staticmethod
    async def find(session: AsyncSession, code: str) -> RedeemCode | None:
        return (
            await session.execute(select(RedeemCode).where(RedeemCode.code == code.strip().upper()))
        ).scalar_one_or_none()

    @staticmethod
    async def consume_use(session: AsyncSession, code: RedeemCode, user_id: int) -> None:
        """Burn one use + record the claim, both race-proof.

        ``used_count < uses`` is in the WHERE clause, so 500 players redeeming a
        1-use code at once produces exactly one winner.
        """
        async with session.begin_nested():
            session.add(CodeClaim(code=code.code, user_id=user_id, claimed_at=now_utc()))
            try:
                await session.flush()
            except IntegrityError:
                raise AlreadyClaimed("you already used this code") from None
        result = await session.execute(
            update(RedeemCode)
            .where(
                RedeemCode.id == code.id,
                RedeemCode.is_active.is_(True),
                RedeemCode.used_count < RedeemCode.uses,
            )
            .values(used_count=RedeemCode.used_count + 1)
        )
        if not result.rowcount:
            raise AlreadyClaimed("this code is spent or expired")
        await session.refresh(code)

    @staticmethod
    async def validate(session: AsyncSession, code_str: str) -> RedeemCode:
        row = await trades.find(session, code_str)
        if row is None or not row.is_active:
            raise NotFound("invalid code")
        if row.expires_at is not None and row.expires_at.replace(tzinfo=None) < now_utc():
            raise Locked("this code expired")
        if row.used_count >= row.uses:
            raise AlreadyClaimed("this code is fully redeemed")
        return row

    @staticmethod
    async def list_codes(
        session: AsyncSession, *, active_only: bool = True, limit: int = 30
    ) -> list[RedeemCode]:
        stmt = select(RedeemCode).order_by(RedeemCode.id.desc()).limit(limit)
        if active_only:
            stmt = stmt.where(RedeemCode.is_active.is_(True))
        return list((await session.execute(stmt)).scalars())

    @staticmethod
    async def claims_of(session: AsyncSession, code: str) -> list[CodeClaim]:
        return list(
            (await session.execute(select(CodeClaim).where(CodeClaim.code == code))).scalars()
        )

    @staticmethod
    async def disable(session: AsyncSession, code: str) -> None:
        await session.execute(
            update(RedeemCode)
            .where(RedeemCode.code == code.strip().upper())
            .values(is_active=False)
        )
        await session.flush()

    @staticmethod
    async def purge_expired(session: AsyncSession) -> int:
        result = await session.execute(
            update(RedeemCode)
            .where(
                RedeemCode.is_active.is_(True),
                RedeemCode.expires_at.is_not(None),
                RedeemCode.expires_at < now_utc(),
            )
            .values(is_active=False)
        )
        return int(result.rowcount or 0)

    @staticmethod
    async def code_stats(session: AsyncSession) -> dict[str, int]:
        row = (
            (
                await session.execute(
                    select(
                        func.count(RedeemCode.id).label("codes"),
                        func.coalesce(func.sum(RedeemCode.used_count), 0).label("redemptions"),
                    )
                )
            )
            .mappings()
            .one()
        )
        return {"codes": int(row["codes"] or 0), "redemptions": int(row["redemptions"] or 0)}

    @staticmethod
    async def log_gift(
        session: AsyncSession,
        *,
        sender_id: int,
        receiver_id: int,
        character_id: int,
        note: str = "",
    ) -> None:
        session.add(
            GiftLog(
                sender_id=sender_id,
                receiver_id=receiver_id,
                character_id=character_id,
                note=note[:140],
                created_at=now_utc(),
            )
        )
        await session.flush()

    @staticmethod
    async def gift_summary(session: AsyncSession, user_id: int) -> dict[str, int]:
        sent = (
            await session.execute(
                select(func.count()).select_from(GiftLog).where(GiftLog.sender_id == user_id)
            )
        ).scalar_one()
        received = (
            await session.execute(
                select(func.count()).select_from(GiftLog).where(GiftLog.receiver_id == user_id)
            )
        ).scalar_one()
        return {"sent": int(sent), "received": int(received)}

    @staticmethod
    async def recent_gifts(session: AsyncSession, user_id: int, *, limit: int = 8) -> list[GiftLog]:
        return list(
            (
                await session.execute(
                    select(GiftLog)
                    .where(or_(GiftLog.sender_id == user_id, GiftLog.receiver_id == user_id))
                    .order_by(GiftLog.created_at.desc())
                    .limit(limit)
                )
            ).scalars()
        )

    @staticmethod
    async def propose(
        session: AsyncSession,
        *,
        initiator_id: int,
        partner_id: int,
        initiator_offer: dict[int, int],
        partner_offer: dict[int, int],
        cash: int = 0,
    ) -> TradeOffer:
        """Replace any still-open offer between the same pair (one live deal each)."""
        await session.execute(
            TradeOffer.__table__.delete().where(
                TradeOffer.status.in_(
                    [str(TradeStatus.PROPOSED), str(TradeStatus.AWAITING_PARTNER)]
                ),
                or_(
                    (TradeOffer.initiator_id == initiator_id)
                    & (TradeOffer.partner_id == partner_id),
                    (TradeOffer.initiator_id == partner_id)
                    & (TradeOffer.partner_id == initiator_id),
                ),
            )
        )
        row = TradeOffer(
            code=secrets.token_hex(3).upper(),
            initiator_id=initiator_id,
            partner_id=partner_id,
            status=str(TradeStatus.PROPOSED),
            initiator_offer={str(k): int(v) for k, v in initiator_offer.items()},
            partner_offer={str(k): int(v) for k, v in partner_offer.items()},
            cash=max(0, int(cash)),
            expires_at=now_utc() + timedelta(seconds=TRADE_TTL_SECONDS),
        )
        session.add(row)
        await session.flush()
        return row

    @staticmethod
    async def by_code(session: AsyncSession, code: str) -> TradeOffer | None:
        return (
            await session.execute(select(TradeOffer).where(TradeOffer.code == code.strip().upper()))
        ).scalar_one_or_none()

    @staticmethod
    async def get(session: AsyncSession, trade_id: int) -> TradeOffer | None:
        return await session.get(TradeOffer, trade_id)

    @staticmethod
    async def open_for(session: AsyncSession, user_id: int) -> list[TradeOffer]:
        return list(
            (
                await session.execute(
                    select(TradeOffer)
                    .where(
                        or_(TradeOffer.initiator_id == user_id, TradeOffer.partner_id == user_id),
                        TradeOffer.status.in_(
                            [str(TradeStatus.PROPOSED), str(TradeStatus.AWAITING_PARTNER)]
                        ),
                    )
                    .order_by(TradeOffer.id.desc())
                )
            ).scalars()
        )

    @staticmethod
    async def set_accept(
        session: AsyncSession, trade_id: int, user_id: int, accepted: bool = True
    ) -> TradeOffer:
        trade = await session.get(TradeOffer, trade_id)
        if trade is None:
            raise NotFound("that offer expired")
        if trade.status not in (str(TradeStatus.PROPOSED), str(TradeStatus.AWAITING_PARTNER)):
            raise Locked("that offer is closed")
        if trade.expires_at.replace(tzinfo=None) < now_utc():
            trade.status = str(TradeStatus.EXPIRED)
            await session.flush()
            raise Locked("that offer timed out")
        if user_id == trade.initiator_id:
            trade.initiator_accepted = accepted
            if accepted and trade.status == str(TradeStatus.PROPOSED):
                trade.status = str(TradeStatus.AWAITING_PARTNER)
        elif user_id == trade.partner_id:
            trade.partner_accepted = accepted
            if accepted:
                trade.status = str(TradeStatus.ACCEPTED)
        else:
            raise Locked("this offer isn't for you")
        await session.flush()
        return trade

    @staticmethod
    async def ready_to_execute(session: AsyncSession, trade_id: int) -> TradeOffer | None:
        """Flip ACCEPTED → COMPLETED atomically; the second caller gets ``None``."""
        result = await session.execute(
            update(TradeOffer)
            .where(TradeOffer.id == trade_id, TradeOffer.status == str(TradeStatus.ACCEPTED))
            .values(status=str(TradeStatus.COMPLETED), completed_at=now_utc())
            .returning(TradeOffer.id)
        )
        claimed = result.scalar_one_or_none()
        if claimed is None:
            return None
        return await session.get(TradeOffer, trade_id)

    @staticmethod
    async def cancel(session: AsyncSession, trade_id: int, actor_id: int) -> TradeOffer:
        trade = await session.get(TradeOffer, trade_id)
        if trade is None:
            raise NotFound("offer gone")
        if actor_id not in (trade.initiator_id, trade.partner_id):
            raise Locked("not your offer")
        if trade.status not in (str(TradeStatus.PROPOSED), str(TradeStatus.AWAITING_PARTNER)):
            raise Locked("already closed")
        trade.status = str(TradeStatus.CANCELLED)
        await session.flush()
        return trade

    @staticmethod
    async def expire_stale(session: AsyncSession) -> list[TradeOffer]:
        rows = list(
            (
                await session.execute(
                    select(TradeOffer).where(
                        TradeOffer.status.in_(
                            [str(TradeStatus.PROPOSED), str(TradeStatus.AWAITING_PARTNER)]
                        ),
                        TradeOffer.expires_at < now_utc(),
                    )
                )
            ).scalars()
        )
        if rows:
            await session.execute(
                update(TradeOffer)
                .where(TradeOffer.id.in_([r.id for r in rows]))
                .values(status=str(TradeStatus.EXPIRED))
            )
            await session.flush()
        return rows

    @staticmethod
    async def attach_ephemeral(
        session: AsyncSession, trade_id: int, user_id: int, message_id: int
    ) -> None:
        """Remember the ephemeral confirm message so the buttons can edit *it*."""
        trade = await session.get(TradeOffer, trade_id)
        if trade is None:
            return
        ids = dict(trade.ephemeral_ids or {})
        ids[str(user_id)] = message_id
        await session.execute(
            update(TradeOffer).where(TradeOffer.id == trade_id).values(ephemeral_ids=ids)
        )
        await session.flush()

    @staticmethod
    async def purge_trade_history(session: AsyncSession, *, older_than_days: int = 60) -> int:
        cutoff = now_utc() - timedelta(days=older_than_days)
        result = await session.execute(
            TradeOffer.__table__.delete().where(
                TradeOffer.status.in_([str(TradeStatus.COMPLETED), str(TradeStatus.CANCELLED)]),
                TradeOffer.created_at < cutoff,
            )
        )
        return int(result.rowcount or 0)

    @staticmethod
    async def shop_pool_cleanup(session: AsyncSession, *, older_than_days: int = 30) -> int:
        """Refresh pools are Redis-first; this trims the durable mirror."""
        cutoff = now_utc() - timedelta(days=older_than_days)
        result = await session.execute(
            ShopPool.__table__.delete().where(ShopPool.updated_at < cutoff)
        )
        return int(result.rowcount or 0)


class users:
    """User rows, preferences, bans and leaderboards.

    Leaderboard reads go to Redis ZSETs when warm (O(log N)) and fall back to SQL;
    writes are done in the same transaction as the underlying change, then mirrored
    to Redis by :mod:`waifu.services.stats` so the two never disagree for long.
    """

    @staticmethod
    def make_invite_code(user_id: int) -> str:
        digest = hashlib.sha256(f"{user_id}:{secrets.token_bytes(4).hex()}".encode()).hexdigest()
        return "".join(_INVITE_ALPHABET[int(c, 16)] for c in digest[:8])

    @staticmethod
    async def upsert(
        session: AsyncSession,
        user_id: int,
        *,
        username: str | None = None,
        first_name: str = "",
        last_name: str = "",
        locale: str | None = None,
        settings: Settings | None = None,
    ) -> User:
        """INSERT … ON CONFLICT DO NOTHING, then fetch — race-free first-contact write."""
        cfg = settings or get_settings()
        role = Role.USER
        if cfg.owner_id and user_id == cfg.owner_id:
            role = Role.OWNER
        elif user_id in cfg.admin_ids:
            role = Role.ADMIN
        elif cfg.features.ai and user_id == 0:
            role = Role.GUEST
        seed = secrets.token_hex(16)
        values = {
            "id": user_id,
            "username": username,
            "first_name": first_name or "",
            "last_name": last_name or "",
            "balance": cfg.starting_balance,
            "role": str(role),
            "locale": locale or cfg.default_locale,
            "invite_code": users.make_invite_code(user_id),
            "server_seed": seed,
            "seed_commitment": hashlib.sha256(seed.encode()).hexdigest(),
            "prefs": {},
            "rarity_histogram": {},
            "last_seen_at": now_utc(),
        }
        insert = (
            pg_insert if session.bind and session.bind.dialect.name == "postgresql" else lite_insert
        )(User)
        result = await session.execute(
            insert.values(**values).on_conflict_do_nothing(index_elements=[User.id])
        )
        user = await session.get(User, user_id)
        if user is None:
            raise RuntimeError(f"user {user_id} could not be materialised")
        if result.rowcount and cfg.starting_balance:
            user.balance = 0
            await session.flush()
            await economy.credit(
                session,
                user_id,
                int(cfg.starting_balance),
                LedgerReason.SIGNUP,
                reference="signup",
                idempotency_key=f"signup:{user_id}",
            )
        changed = False
        if username is not None and user.username != username:
            user.username, changed = (username, True)
        if first_name and user.first_name != first_name:
            user.first_name, changed = (first_name, True)
        if last_name and user.last_name != last_name:
            user.last_name, changed = (last_name, True)
        if locale and user.locale != locale:
            user.locale, changed = (locale, True)
        if changed or user.last_seen_at < now_utc() - timedelta(minutes=10):
            user.last_seen_at = now_utc()
            changed = True
        if changed:
            await session.flush()
        return user

    @staticmethod
    async def get(session: AsyncSession, user_id: int) -> User | None:
        return await session.get(User, user_id)

    @staticmethod
    async def get_many(session: AsyncSession, user_ids: list[int]) -> dict[int, User]:
        if not user_ids:
            return {}
        rows = (await session.execute(select(User).where(User.id.in_(user_ids)))).scalars()
        return {u.id: u for u in rows}

    @staticmethod
    async def by_username(session: AsyncSession, username: str) -> User | None:
        name = username.strip().lstrip("@").lower()
        if not name:
            return None
        return (
            await session.execute(select(User).where(func.lower(User.username) == name).limit(1))
        ).scalar_one_or_none()

    @staticmethod
    async def by_invite(session: AsyncSession, code: str) -> User | None:
        return (
            await session.execute(
                select(User).where(func.upper(User.invite_code) == code.strip().upper()).limit(1)
            )
        ).scalar_one_or_none()

    @staticmethod
    async def resolve(
        session: AsyncSession, raw: str | None, *, reply_to_id: int | None = None
    ) -> User | None:
        """Resolve ``/give @name`` or ``/give 12345`` or a replied-to message."""
        if reply_to_id:
            found = await session.get(User, reply_to_id)
            if found:
                return found
        if not raw:
            return None
        raw = raw.strip().lstrip("@")
        if raw.isdigit():
            return await session.get(User, int(raw))
        found = await users.by_username(session, raw)
        if found is not None:
            return found
        hits = await users.search(session, raw, limit=2)
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise MultipleMatches(raw, len(hits))
        return None

    @staticmethod
    async def prefs(session: AsyncSession, user_id: int) -> UserPref:
        row = await session.get(UserPref, user_id)
        if row is None:
            row = UserPref(user_id=user_id, flags={})
            session.add(row)
            await session.flush()
        return row

    @staticmethod
    async def set_pref(session: AsyncSession, user_id: int, **values: object) -> UserPref:
        row = await users.prefs(session, user_id)
        flags = dict(row.flags or {})
        for key, value in values.items():
            if hasattr(row, key):
                setattr(row, key, value)
            else:
                flags[key] = value
        row.flags = flags
        await session.flush()
        return row

    @staticmethod
    async def set_banned(
        session: AsyncSession, user_id: int, *, banned: bool, reason: str = "", username: str = ""
    ) -> None:
        await session.execute(
            update(User)
            .where(User.id == user_id)
            .values(banned=banned, ban_reason=reason[:250] if banned else "")
        )
        insert = (
            pg_insert if session.bind and session.bind.dialect.name == "postgresql" else lite_insert
        )(BannedUser)
        if banned:
            await session.execute(
                insert.values(
                    user_id=user_id,
                    username=username or "",
                    reason=reason[:250],
                    banned_by=0,
                    created_at=now_utc(),
                ).on_conflict_do_update(
                    index_elements=[BannedUser.user_id],
                    set_={"reason": reason[:250], "username": username or ""},
                )
            )
        else:
            await session.execute(
                BannedUser.__table__.delete().where(BannedUser.user_id == user_id)
            )

    @staticmethod
    async def is_banned(session: AsyncSession, user_id: int) -> bool:
        user = await session.get(User, user_id)
        if user is not None and user.banned:
            return True
        return (
            await session.execute(
                select(BannedUser.user_id).where(BannedUser.user_id == user_id).limit(1)
            )
        ).scalar_one_or_none() is not None

    @staticmethod
    async def add_warning(session: AsyncSession, user_id: int, *, count: int = 1) -> int:
        await session.execute(
            update(User).where(User.id == user_id).values(warn_count=User.warn_count + count)
        )
        total = (
            await session.execute(select(User.warn_count).where(User.id == user_id))
        ).scalar_one_or_none()
        return int(total or 0)

    @staticmethod
    async def clear_warnings(session: AsyncSession, user_id: int) -> None:
        await session.execute(update(User).where(User.id == user_id).values(warn_count=0))

    @staticmethod
    async def set_role(session: AsyncSession, user_id: int, role: Role) -> None:
        await session.execute(update(User).where(User.id == user_id).values(role=str(role)))

    @staticmethod
    async def leaderboard(
        session: AsyncSession, *, metric: str = "balance", limit: int = 25, offset: int = 0
    ) -> list[tuple[User, int]]:
        column = LEADERBOARD_METRICS.get(metric, User.balance)
        stmt = (
            select(User, column.label("score"))
            .where(User.banned.is_(False))
            .order_by(column.desc(), User.last_seen_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return [(row[0], int(row[1] or 0)) for row in (await session.execute(stmt)).all()]

    @staticmethod
    async def rank_of(
        session: AsyncSession, user_id: int, *, metric: str = "balance"
    ) -> tuple[int, int]:
        column = LEADERBOARD_METRICS.get(metric, User.balance)
        mine = (
            await session.execute(select(column).where(User.id == user_id))
        ).scalar_one_or_none()
        if mine is None:
            return (0, 0)
        better = (
            await session.execute(
                select(func.count()).select_from(User).where(User.banned.is_(False), column > mine)
            )
        ).scalar_one()
        total = (
            await session.execute(
                select(func.count()).select_from(User).where(User.banned.is_(False))
            )
        ).scalar_one()
        return (int(better) + 1, int(total))

    @staticmethod
    async def search(session: AsyncSession, query: str, *, limit: int = 12) -> list[User]:
        like = f"%{query.strip().lstrip('@')}%"
        stmt = (
            select(User)
            .where(
                or_(
                    User.username.ilike(like),
                    User.first_name.ilike(like),
                    User.last_name.ilike(like),
                )
            )
            .order_by(User.balance.desc())
            .limit(limit)
        )
        return list((await session.execute(stmt)).scalars())

    @staticmethod
    async def counts(session: AsyncSession) -> dict[str, int]:
        row = (
            (
                await session.execute(
                    select(
                        func.count(User.id).label("users"),
                        func.coalesce(func.sum(User.balance), 0).label("coins"),
                        func.coalesce(func.sum(User.pulls_total), 0).label("claims"),
                        func.coalesce(func.sum(User.high_pulls), 0).label("high"),
                    )
                )
            )
            .mappings()
            .one()
        )
        day = now_utc() - timedelta(days=1)
        active = (
            await session.execute(
                select(func.count())
                .select_from(User)
                .where(users._as_naive(User.last_seen_at) >= day)
            )
        ).scalar_one()
        newest = (
            await session.execute(
                select(func.count())
                .select_from(User)
                .where(users._as_naive(User.created_at) >= day)
            )
        ).scalar_one()
        groups = (await session.execute(select(func.count()).select_from(Group))).scalar_one()
        return {
            "users": int(row["users"]),
            "active_24h": int(active),
            "new_24h": int(newest),
            "coins": int(row["coins"] or 0),
            "claims": int(row["claims"] or 0),
            "high_claims": int(row["high"] or 0),
            "groups": int(groups),
        }

    @staticmethod
    def _as_naive(column):
        """Postgres timestamptz vs SQLite text both compare fine against naive UTC."""
        return column

    @staticmethod
    async def touch_activity(
        session: AsyncSession, chat_id: int, user_id: int, *, kind: str = "message"
    ) -> None:
        session.add(ActivityLog(chat_id=chat_id, user_id=user_id, kind=kind, created_at=now_utc()))

    @staticmethod
    async def expire_stale(session: AsyncSession, *, days: int = 120) -> int:
        """Purge 24h-activity rows to keep the table small (Summon-bot never purged)."""
        cutoff = now_utc() - timedelta(days=days)
        result = await session.execute(
            ActivityLog.__table__.delete().where(
                ActivityLog.created_at < datetime.combine(cutoff.date(), datetime.min.time())
            )
        )
        return int(result.rowcount or 0)
