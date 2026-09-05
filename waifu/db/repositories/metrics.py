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

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from waifu.db.models import (
    AiMessageLog,
    Auction,
    FairRoll,
    GiftLog,
    HeistLog,
    Ownership,
    SpawnEvent,
    TradeOffer,
    Transaction,
)
from waifu.enums import AuctionStatus, Rarity, TradeStatus
from waifu.utils.time import now_utc

#: Transaction reasons that mean "the player worked for it".
EARN_REASONS = ("daily", "work", "quest", "spin", "bonus", "claim", "sell")


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


async def rolls(session: AsyncSession, user_id: int, *, since: datetime | None = None) -> int:
    conds = [FairRoll.user_id == user_id]
    if since is not None:
        conds.append(FairRoll.created_at >= since)
    return int(
        (await session.execute(select(func.count(FairRoll.id)).where(*conds))).scalar_one() or 0
    )


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


async def gifts_sent(session: AsyncSession, user_id: int) -> int:
    return int(
        (
            await session.execute(
                select(func.count(GiftLog.id)).where(GiftLog.sender_id == user_id)
            )
        ).scalar_one()
        or 0
    )


async def gifts_received(session: AsyncSession, user_id: int) -> int:
    return int(
        (
            await session.execute(
                select(func.count(GiftLog.id)).where(GiftLog.receiver_id == user_id)
            )
        ).scalar_one()
        or 0
    )


async def heists_won(session: AsyncSession, user_id: int, *, kind: str | None = None) -> int:
    """Successful steal/bomb attempts by this player (``kind`` narrows it)."""
    conds = [HeistLog.attacker_id == user_id, HeistLog.outcome == "success"]
    if kind:
        conds.append(HeistLog.kind == kind)
    return int(
        (await session.execute(select(func.count(HeistLog.id)).where(*conds))).scalar_one() or 0
    )


async def heists_lost(session: AsyncSession, user_id: int, *, kind: str | None = None) -> int:
    conds = [HeistLog.target_id == user_id, HeistLog.outcome == "success"]
    if kind:
        conds.append(HeistLog.kind == kind)
    return int(
        (await session.execute(select(func.count(HeistLog.id)).where(*conds))).scalar_one() or 0
    )


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


async def reason_count(
    session: AsyncSession, user_id: int, reason: str, *, since: datetime | None = None
) -> int:
    """How many ledger lines of one reason (spin/sell/gift…) since an optional time."""
    conds = [Transaction.user_id == user_id, Transaction.reason == reason]
    if since is not None:
        conds.append(Transaction.created_at >= since)
    return int(
        (await session.execute(select(func.count(Transaction.id)).where(*conds))).scalar_one() or 0
    )


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


async def many(session: AsyncSession, user_id: int) -> dict[str, int]:
    """All counters, one call (used by /stats, achievements and the Mini App)."""
    day_start = now_utc().replace(hour=0, minute=0, second=0, microsecond=0)
    return {
        "spawns_claimed": await spawns_claimed(session, user_id),
        "rolls_total": await rolls(session, user_id),
        "rolls_today": await rolls(session, user_id, since=day_start),
        "rare_rolls": await rare_rolls(session, user_id),
        "work_count": await work_count(session, user_id),
        "earned_24h": await earned_since(session, user_id, since=day_start),
        "auctions_won": await auctions_won(session, user_id),
        "auctions_bought": await auctions_bought(session, user_id),
        "trades_done": await trades_done(session, user_id),
        "gifts_sent": await gifts_sent(session, user_id),
        "gifts_received": await gifts_received(session, user_id),
        "heists_won": await heists_won(session, user_id),
        "heists_lost": await heists_lost(session, user_id),
        "collection_size": await collection_size(session, user_id),
        "dupes": await dupe_count(session, user_id),
        "spin_count": await reason_count(session, user_id, "spin"),
        "sold_today": await reason_count(session, user_id, "sell", since=day_start),
        "ai_today": await ai_messages_today(session, user_id, since=day_start),
    }
