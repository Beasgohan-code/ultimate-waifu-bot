"""Monetisation persistence: Telegram Stars, subscriptions, boosts, raffles.

Delivery is **idempotent by payload**: ``star_purchases.invoice_payload`` is
UNIQUE, and the row is flipped ``pending → paid → delivered`` with a conditional
UPDATE. Telegram retries both ``pre_checkout_query`` and ``successful_payment``,
and paid-media purchases arrive as a separate update type — with this shape, a
retry credits coins exactly once. (Summon-bot's ``/premium`` was admin-granted
only, so a paid support flow had no record at all.)
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from waifu.db.models import BoostGrant, RaffleRound, StarPurchase, SubscriptionAccess, User
from waifu.enums import SubscriptionState
from waifu.errors import AlreadyClaimed, NotFound
from waifu.utils.time import now_utc


# --------------------------------------------------------------------- XTR orders
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


async def by_payload(session: AsyncSession, payload: str) -> StarPurchase | None:
    return (
        await session.execute(select(StarPurchase).where(StarPurchase.invoice_payload == payload))
    ).scalar_one_or_none()


async def mark_paid(
    session: AsyncSession, payload: str, *, charge_id: str = "", star_count: int | None = None
) -> StarPurchase:
    row = await by_payload(session, payload)
    if row is None:
        raise NotFound("unknown purchase payload")
    if row.status in {"paid", "delivered"}:
        return row  # retry: hand back the same row, never double-credit
    row.status = "paid"
    row.paid_at = now_utc()
    row.telegram_payment_charge_id = charge_id
    if star_count:
        row.star_count = star_count
    await session.flush()
    return row


async def mark_delivered(session: AsyncSession, payload: str) -> bool:
    """Flip paid → delivered. Returns False when someone else already delivered it."""
    result = await session.execute(
        update(StarPurchase)
        .where(StarPurchase.invoice_payload == payload, StarPurchase.status == "paid")
        .values(status="delivered", delivered_at=now_utc())
        .returning(StarPurchase.id)
    )
    return result.scalar_one_or_none() is not None


async def mark_refunded(session: AsyncSession, payload: str) -> StarPurchase | None:
    row = await by_payload(session, payload)
    if row is None or row.status == "refunded":
        return row
    row.status = "refunded"
    row.refunded_at = now_utc()
    await session.flush()
    return row


async def orders_for(session: AsyncSession, user_id: int, *, limit: int = 20) -> list[StarPurchase]:
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


async def charge_id_for(session: AsyncSession, payload: str) -> str:
    row = await by_payload(session, payload)
    return row.telegram_payment_charge_id if row else ""


# ------------------------------------------------------------------- subscriptions
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


async def has_subscription(session: AsyncSession, user_id: int) -> bool:
    return bool(await active_subs(session, user_id))


async def subscription_period_end(session: AsyncSession, user_id: int):
    return (
        await session.execute(
            select(func.max(SubscriptionAccess.current_period_end)).where(
                SubscriptionAccess.user_id == user_id,
                SubscriptionAccess.status == str(SubscriptionState.ACTIVE),
            )
        )
    ).scalar_one_or_none()


# ---------------------------------------------------------------------- boost perks
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


# --------------------------------------------------------------------------- raffle
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


async def raffle_counts(session: AsyncSession, raffle_id: int) -> int:
    row = await session.get(RaffleRound, raffle_id)
    return int(row.entrants or 0) if row else 0


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


# --------------------------------------------------------------- supporter badge
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


async def has_ever_paid(session: AsyncSession, user_id: int) -> bool:
    found = (
        await session.execute(
            select(StarPurchase.id)
            .where(StarPurchase.user_id == user_id, StarPurchase.status.in_(["paid", "delivered"]))
            .limit(1)
        )
    ).scalar_one_or_none()
    return found is not None


async def record_premium_granted(session: AsyncSession, payload: str, hours: int) -> None:
    """Annotate the order with the premium it bought (the grant itself is in
    :func:`waifu.db.repositories.economy.grant_premium`, so expiry stacking
    happens in exactly one place)."""
    row = await by_payload(session, payload)
    if row is None:
        return
    row.premium_hours = max(row.premium_hours, hours)
    await session.flush()


# ------------------------------------------------- charge ids + subscription ops
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


async def extend_subscription(session: AsyncSession, user_id: int, *, days: int = 30) -> datetime:
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
    if row is not None and row.current_period_end is not None and row.current_period_end > base:
        base = row.current_period_end
    period_end = base + timedelta(days=max(1, days))
    if row is not None:
        row.current_period_end = period_end
        row.updated_at = now_utc()
        await session.flush()
    else:
        await upsert_subscription(
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
