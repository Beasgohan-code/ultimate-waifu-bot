"""Auctions: /auction, /auctionlist, /mybids, /cancelauction.

Improvements over Summon-bot:

* the character is **escrowed** (``is_locked``) for the auction's duration, so it
  can't be sold or gifted mid-bid;
* a bid inside the final 3 minutes extends the clock (anti-snipe), capped so an
  auction can't run forever;
* settlement runs under a Postgres advisory lock (see :meth:`Database.job_tx`)
  so two workers can never both pay out the same auction.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from waifu.db.models import Auction, AuctionBid, Character, Ownership
from waifu.enums import AuctionStatus
from waifu.errors import BidTooLow, Locked, NotFound
from waifu.utils.time import now_utc

SNIPE_WINDOW = 180
SNIPE_EXTENSION = 120
MAX_EXTENSIONS = 6
#: The reference allowed a 5-minute flash auction (``max(5, min(180, …))`` in ``cmd_auction``);
#: a 10-minute floor silently refused that, and an auction that cannot be short is an auction that
#: cannot be impulsive. The service clamps with the settings; this is the backstop.
MIN_DURATION = 60 * 5
MAX_DURATION = 60 * 60 * 72


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


async def attach_message(
    session: AsyncSession, auction_id: int, message_id: int, chat_id: int
) -> None:
    await session.execute(
        update(Auction)
        .where(Auction.id == auction_id)
        .values(message_id=message_id, chat_id=chat_id)
    )
    await session.flush()


async def get(session: AsyncSession, auction_id: int) -> Auction | None:
    return await session.get(Auction, auction_id)


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
    return auction, char, int(copies or 0), int(count)


async def next_minimum(session: AsyncSession, auction_id: int) -> int:
    auction = await session.get(Auction, auction_id)
    if auction is None:
        raise NotFound("auction not found")
    return max(int(auction.start_price), int(auction.current_bid or 0) + int(auction.min_increment))


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
        AuctionBid(auction_id=auction_id, bidder_id=bidder_id, amount=amount, created_at=now_utc())
    )
    await session.flush()
    # ``outbid`` is only for the chat notification; the authoritative refund set is
    # :func:`refund_bids`, which handles a bidder who beat their own bid too.
    return (auction, previous_top if previous_top and previous_top != bidder_id else None), extended


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
            Ownership.user_id == auction.seller_id, Ownership.character_id == auction.character_id
        )
        .values(is_locked=False)
    )
    await session.flush()
    return auction


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
            Ownership.user_id == auction.seller_id, Ownership.character_id == auction.character_id
        )
        .values(is_locked=False)
    )
    await session.flush()
    return auction


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
    return [a for a, _c in rows], int(total), chars


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


async def leading(session: AsyncSession, user_id: int) -> list[Auction]:
    return list(
        (
            await session.execute(
                select(Auction)
                .where(Auction.top_bidder_id == user_id, Auction.status == str(AuctionStatus.LIVE))
                .order_by(Auction.ends_at.asc())
            )
        ).scalars()
    )


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


async def bid_log(session: AsyncSession, auction_id: int, *, limit: int = 12) -> list[AuctionBid]:
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


async def purge(session: AsyncSession, *, older_than_days: int = 120) -> int:
    cutoff = now_utc() - timedelta(days=older_than_days)
    result = await session.execute(
        Auction.__table__.delete().where(
            Auction.status.in_([str(AuctionStatus.SOLD), str(AuctionStatus.CANCELLED)]),
            Auction.created_at < cutoff,
        )
    )
    return int(result.rowcount or 0)
