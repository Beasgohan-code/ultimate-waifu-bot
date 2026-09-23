"""Money: the only module permitted to write ``users.balance``.

Rules that make this safe under concurrency (Summon-bot had none of them):

* ``credit``/``debit`` are single statements. ``debit`` carries ``balance >= amount``
  **in the WHERE clause**, so overdraft is impossible even with two parallel calls.
* Every movement writes a ``transactions`` row (who, how much, resulting balance,
  why, idempotency key) → disputes are a SELECT.
* ``idempotency_key`` has a UNIQUE index, so a retried webhook delivery or a
  double-tapped /daily returns the original result instead of paying twice.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from waifu.db.models import DailyClaim, Premium, Transaction, User
from waifu.enums import LedgerReason
from waifu.errors import AlreadyClaimed, NotEnoughFunds
from waifu.utils.time import now_utc


@dataclass(slots=True)
class Entry:
    id: int
    user_id: int
    delta: int
    balance_after: int
    reason: str
    duplicate: bool = False


async def balance(session: AsyncSession, user_id: int) -> int:
    return int(
        (await session.execute(select(User.balance).where(User.id == user_id))).scalar_one_or_none()
        or 0
    )


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
        if (await get_user_or_raise(session, user_id)) is None:  # pragma: no cover
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
    entry = await _record(
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
        return Entry(0, user_id, 0, await balance(session, user_id), str(reason))

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
        raise NotEnoughFunds(have=await balance(session, user_id), needed=amount)
    entry = await _record(
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
    out = await debit(
        session,
        sender_id,
        amount,
        reason,
        reference=reference,
        counterparty=receiver_id,
        meta={"to": receiver_id, **(meta or {})},
    )
    inp = await credit(
        session,
        receiver_id,
        amount,
        reason,
        reference=reference,
        counterparty=sender_id,
        meta={"from": sender_id, **(meta or {})},
    )
    return out, inp


async def get_user_or_raise(session: AsyncSession, user_id: int) -> User | None:
    return await session.get(User, user_id)


class NotFoundUser(LookupError):
    def __init__(self, user_id: int) -> None:
        super().__init__(f"user {user_id} not found")


# ------------------------------------------------------------------- daily idempotency
async def claim_once(
    session: AsyncSession, user_id: int, kind: str, local_day: str, *, amount: int = 0
) -> bool:
    """True on the first claim of ``kind`` for that local day; False if already claimed.

    Relies on ``uq_daily_claims_kind_day`` — TOCTOU-free by construction.
    """
    # A savepoint, not the outer transaction: a duplicate claim must not roll back
    # the ledger rows written earlier in the same command.
    async with session.begin_nested():
        session.add(
            DailyClaim(
                user_id=user_id, kind=kind, local_day=local_day, amount=amount, created_at=now_utc()
            )
        )
        try:
            await session.flush()
        except IntegrityError:
            return False
    return True


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


# -------------------------------------------------------------------------- ledger
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
            await session.execute(stmt.order_by(Transaction.id.desc()).limit(limit).offset(offset))
        ).scalars()
    )


async def lifetime(session: AsyncSession, user_id: int, reason: str) -> int:
    value = (
        await session.execute(
            select(func.coalesce(func.sum(Transaction.delta), 0)).where(
                Transaction.user_id == user_id, Transaction.reason == str(reason)
            )
        )
    ).scalar_one()
    return int(value or 0)


async def total_circulating(session: AsyncSession) -> int:
    return int(
        (await session.execute(select(func.coalesce(func.sum(User.balance), 0)))).scalar_one() or 0
    )


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


# ------------------------------------------------------------------------- premium
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


async def is_premium(session: AsyncSession, user_id: int) -> bool:
    user = await session.get(User, user_id)
    if user is not None and user.premium_until is not None:
        from waifu.utils.time import to_naive_utc

        return to_naive_utc(user.premium_until) > now_utc()
    return (await premium_left_hours(session, user_id)) > 0


async def grant_premium(
    session: AsyncSession,
    user_id: int,
    hours: int,
    *,
    granted_by: int,
    source: str = "admin",
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
            user_id=user_id, hours=hours, granted_by=granted_by, source=source, expires_at=expires
        )
    )
    await session.execute(update(User).where(User.id == user_id).values(premium_until=expires))
    await session.flush()
    return int((expires - now_utc()).total_seconds() // 3600)


async def revoke_premium(session: AsyncSession, user_id: int) -> None:
    await session.execute(
        Premium.__table__.delete().where(Premium.user_id == user_id, Premium.expires_at > now_utc())
    )
    await session.execute(update(User).where(User.id == user_id).values(premium_until=None))
    await session.flush()


async def premium_grants(session: AsyncSession, user_id: int, *, limit: int = 10) -> list[Premium]:
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


async def integrity_report(session: AsyncSession) -> list[str]:
    """For ``waifu doctor``: any way the ledger and the balance disagree."""
    problems: list[str] = []
    negative = (
        await session.execute(select(func.count()).select_from(User).where(User.balance < 0))
    ).scalar_one()
    if negative:
        problems.append(f"{negative} users have a negative balance (CHECK constraint bypassed?)")
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
        # No "skip accounts with no history" carve-out: an unexplained balance is the
        # exact thing this report exists to find.
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
