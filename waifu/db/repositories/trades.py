"""Redeem codes, gifts and escrow trade offers.

Trades are a *feature of /gift* (buttons), not a new command: ``/gift @user``
either hands a character over for free or opens an escrow offer when the other
side is asked to add something. Both sides must accept before anything moves,
and the confirmation lives in an **ephemeral** private message (Bot API 10.3),
so the negotiation isn't shouted in the group.
"""

from __future__ import annotations

import secrets
from datetime import timedelta

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from waifu.db.models import CodeClaim, GiftLog, RedeemCode, ShopPool, TradeOffer
from waifu.enums import TradeStatus
from waifu.errors import AlreadyClaimed, Locked, NotFound
from waifu.utils.rng import redeem_code
from waifu.utils.time import now_utc

TRADE_TTL_SECONDS = 900


# ------------------------------------------------------------------- redeem codes
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


async def find(session: AsyncSession, code: str) -> RedeemCode | None:
    return (
        await session.execute(select(RedeemCode).where(RedeemCode.code == code.strip().upper()))
    ).scalar_one_or_none()


async def consume_use(session: AsyncSession, code: RedeemCode, user_id: int) -> None:
    """Burn one use + record the claim, both race-proof.

    ``used_count < uses`` is in the WHERE clause, so 500 players redeeming a
    1-use code at once produces exactly one winner.
    """
    # Order matters: the unique ``code_claims`` row is the race guard, so it is
    # inserted *before* a use is burned. A loser's insert fails inside a savepoint
    # and leaves ``used_count`` untouched (the outer transaction rolls back).
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


async def validate(session: AsyncSession, code_str: str) -> RedeemCode:
    row = await find(session, code_str)
    if row is None or not row.is_active:
        raise NotFound("invalid code")
    if row.expires_at is not None and row.expires_at.replace(tzinfo=None) < now_utc():
        raise Locked("this code expired")
    if row.used_count >= row.uses:
        raise AlreadyClaimed("this code is fully redeemed")
    return row


async def list_codes(
    session: AsyncSession, *, active_only: bool = True, limit: int = 30
) -> list[RedeemCode]:
    stmt = select(RedeemCode).order_by(RedeemCode.id.desc()).limit(limit)
    if active_only:
        stmt = stmt.where(RedeemCode.is_active.is_(True))
    return list((await session.execute(stmt)).scalars())


async def claims_of(session: AsyncSession, code: str) -> list[CodeClaim]:
    return list((await session.execute(select(CodeClaim).where(CodeClaim.code == code))).scalars())


async def disable(session: AsyncSession, code: str) -> None:
    await session.execute(
        update(RedeemCode).where(RedeemCode.code == code.strip().upper()).values(is_active=False)
    )
    await session.flush()


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


# ------------------------------------------------------------------------ gifts
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


# ------------------------------------------------------------------- trade escrow
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
            TradeOffer.status.in_([str(TradeStatus.PROPOSED), str(TradeStatus.AWAITING_PARTNER)]),
            or_(
                (TradeOffer.initiator_id == initiator_id) & (TradeOffer.partner_id == partner_id),
                (TradeOffer.initiator_id == partner_id) & (TradeOffer.partner_id == initiator_id),
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


async def by_code(session: AsyncSession, code: str) -> TradeOffer | None:
    return (
        await session.execute(select(TradeOffer).where(TradeOffer.code == code.strip().upper()))
    ).scalar_one_or_none()


async def get(session: AsyncSession, trade_id: int) -> TradeOffer | None:
    return await session.get(TradeOffer, trade_id)


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


async def purge_trade_history(session: AsyncSession, *, older_than_days: int = 60) -> int:
    cutoff = now_utc() - timedelta(days=older_than_days)
    result = await session.execute(
        TradeOffer.__table__.delete().where(
            TradeOffer.status.in_([str(TradeStatus.COMPLETED), str(TradeStatus.CANCELLED)]),
            TradeOffer.created_at < cutoff,
        )
    )
    return int(result.rowcount or 0)


async def shop_pool_cleanup(session: AsyncSession, *, older_than_days: int = 30) -> int:
    """Refresh pools are Redis-first; this trims the durable mirror."""
    cutoff = now_utc() - timedelta(days=older_than_days)
    result = await session.execute(ShopPool.__table__.delete().where(ShopPool.updated_at < cutoff))
    return int(result.rowcount or 0)
