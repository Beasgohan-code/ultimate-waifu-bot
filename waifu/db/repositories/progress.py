"""Progression: pity counters, streaks, achievements, and the fair-roll audit.

``record_roll`` is what makes "/hstats → Verify" real: every claim writes the
commitment shown to the player *before* the roll plus the seed reveal after, so
the outcome can be recomputed from (seed, user_id, sequence). Summon-bot's rolls
were opaque ``random.choice()`` calls with no record at all.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from waifu.db.models import Achievement, DailyClaim, FairRoll, Streak, User
from waifu.enums import Rarity
from waifu.errors import AlreadyClaimed
from waifu.utils.rng import commit, derive_roll
from waifu.utils.time import now_utc


# ------------------------------------------------------------------------- pity
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


# ------------------------------------------------------------------- fair rolls
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
    # Savepoint: a duplicate (user, sequence, index) means a retried request whose
    # ledger rows are already in this transaction and must survive.
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


async def next_sequence(session: AsyncSession, user_id: int) -> int:
    await session.execute(
        update(User).where(User.id == user_id).values(roll_sequence=User.roll_sequence + 1)
    )
    value = (
        await session.execute(select(User.roll_sequence).where(User.id == user_id))
    ).scalar_one()
    await session.flush()
    return int(value)


def recompute(seed: str, user_id: int, sequence: int, salt: str = "") -> float:
    """Verification without a DB (also used by ``waifu verify``)."""
    return derive_roll(seed=seed, player_id=user_id, sequence=sequence, salt=salt)


def commitment_for(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


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


async def global_roll_stats(session: AsyncSession, *, since=None) -> dict[str, float]:
    conds = []
    if since:
        conds.append(FairRoll.created_at >= since)
    rows = (
        await session.execute(
            select(FairRoll.rarity_id, func.count(FairRoll.id))
            .where(*conds)
            .group_by(FairRoll.rarity_id)  # type: ignore[arg-type]
        )
    ).all()
    counts = {int(r): int(c) for r, c in rows}
    total = sum(counts.values()) or 1
    return {f"r{key}": counts[key] / total * 100 for key in sorted(counts)} | {
        "total": float(total)
    }


# ------------------------------------------------------------------------ streak
async def streak(session: AsyncSession, user_id: int) -> Streak:
    found = (
        (await session.execute(select(Streak).where(Streak.user_id == user_id))).scalars().first()
    )
    if found is None:
        found = Streak(user_id=user_id, current=0, highest=0, last_date="", freezes=0)
        session.add(found)
        await session.flush()
    return found


@dataclass(slots=True)
class StreakOutcome:
    current: int
    best: int
    broke: bool
    multiplier: float


async def bump_streak(
    session: AsyncSession, user_id: int, today: str, *, multiplier_curve: list[float]
) -> StreakOutcome:
    """Advance the streak for ``today`` (``YYYY-MM-DD`` in the server timezone).

    Yesterday → +1, today → no change (already claimed), older → reset to 1
    unless the player has a freeze, in which case the gap is bridged once.
    """
    from datetime import date, timedelta

    state = await streak(session, user_id)
    if state.last_date == today:
        mult = _multiplier(state.current, multiplier_curve)
        return StreakOutcome(state.current, state.highest, False, mult)

    yesterday = (date.fromisoformat(today) - timedelta(days=1)).isoformat()
    broke = False
    if state.last_date == yesterday:
        state.current += 1
    elif state.freezes > 0 and state.last_date:
        # A freeze bridges the gap: consume it, keep the streak alive.
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
        state.current, state.highest, broke, _multiplier(state.current, multiplier_curve)
    )


def _multiplier(current: int, curve: list[float]) -> float:
    if not curve:
        return 1.0
    return float(curve[min(current, len(curve)) - 1] if current else 1.0)


async def reset_streaks(session: AsyncSession) -> int:
    """Nightly sweep: anyone who missed yesterday loses the streak (freezes aside)."""
    from datetime import timedelta

    from waifu.utils.time import now_utc

    # ``now_utc().date()`` rather than ``date.today()``: the server's local day is not the
    # player's, and a streak rule that depends on where the process runs is unfixable
    # after the fact (the reference bot shipped exactly that).
    yesterday = (now_utc().date() - timedelta(days=1)).isoformat()
    result = await session.execute(
        update(Streak)
        .where(Streak.last_date < yesterday, Streak.current > 0, Streak.freezes == 0)
        .values(current=0)
    )
    return int(result.rowcount or 0)


async def add_freeze(session: AsyncSession, user_id: int, count: int = 1) -> int:
    """Grant /spend streak freezes: one freeze bridges a single missed day."""
    state = await streak(session, user_id)
    state.freezes += count
    await session.flush()
    return state.freezes


async def spend_freeze(session: AsyncSession, user_id: int) -> bool:
    state = await streak(session, user_id)
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


# ------------------------------------------------------------------ daily claims
async def already_claimed(session: AsyncSession, user_id: int, kind: str, local_day: str) -> bool:
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


async def mark_claimed(
    session: AsyncSession, user_id: int, kind: str, local_day: str, *, amount: int = 0
) -> bool:
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


# ------------------------------------------------------------------- achievements
async def unlocked(session: AsyncSession, user_id: int) -> set[str]:
    rows = (
        await session.execute(
            select(Achievement.achievement_id).where(
                Achievement.user_id == user_id, Achievement.unlocked_at.is_not(None)
            )
        )
    ).scalars()
    return {str(r) for r in rows}


async def progress_rows(session: AsyncSession, user_id: int) -> dict[str, int]:
    rows = (
        await session.execute(
            select(Achievement.achievement_id, Achievement.progress).where(
                Achievement.user_id == user_id, Achievement.unlocked_at.is_(None)
            )
        )
    ).all()
    return {str(r[0]): int(r[1]) for r in rows}


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
        except IntegrityError:  # concurrent unlock by another command in flight
            return False
    return True


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


async def unlock_counts(session: AsyncSession, key: str) -> int:
    return int(
        (
            await session.execute(
                select(func.count(Achievement.id)).where(Achievement.achievement_id == key)
            )
        ).scalar_one()
        or 0
    )


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


async def claim_bonus(session: AsyncSession, user_id: int, key: str, amount: int) -> None:
    """Achievement coin payouts are single-shot via the daily-claim unique index."""
    ok = await mark_claimed(session, user_id, f"ach:{key}", "lifetime", amount=amount)
    if not ok:
        raise AlreadyClaimed("bonus already collected")
