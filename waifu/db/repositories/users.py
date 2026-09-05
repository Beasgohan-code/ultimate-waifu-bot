"""User rows, preferences, bans and leaderboards.

Leaderboard reads go to Redis ZSETs when warm (O(log N)) and fall back to SQL;
writes are done in the same transaction as the underlying change, then mirrored
to Redis by :mod:`waifu.services.stats` so the two never disagree for long.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as lite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from waifu.db.models import ActivityLog, BannedUser, Group, User, UserPref
from waifu.enums import LedgerReason, Role
from waifu.errors import MultipleMatches
from waifu.settings import Settings, get_settings
from waifu.utils.time import now_utc

_INVITE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def make_invite_code(user_id: int) -> str:
    digest = hashlib.sha256(f"{user_id}:{secrets.token_bytes(4).hex()}".encode()).hexdigest()
    return "".join(_INVITE_ALPHABET[int(c, 16)] for c in digest[:8])


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
    elif cfg.features.ai and user_id == 0:  # pragma: no cover - impossible, keeps mypy honest
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
        "invite_code": make_invite_code(user_id),
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
    if user is None:  # pragma: no cover - only if the insert vanished
        raise RuntimeError(f"user {user_id} could not be materialised")
    if result.rowcount and cfg.starting_balance:
        # Newcomer: the starting balance goes through the ledger (and the ledger owns
        # the column write), so no account can hold coins that /history cannot explain.
        from waifu.db.repositories import economy as ledger

        user.balance = 0
        await session.flush()
        await ledger.credit(
            session,
            user_id,
            int(cfg.starting_balance),
            LedgerReason.SIGNUP,
            reference="signup",
            idempotency_key=f"signup:{user_id}",
        )

    changed = False
    if username is not None and user.username != username:
        user.username, changed = username, True
    if first_name and user.first_name != first_name:
        user.first_name, changed = first_name, True
    if last_name and user.last_name != last_name:
        user.last_name, changed = last_name, True
    if locale and user.locale != locale:
        user.locale, changed = locale, True
    if changed or user.last_seen_at < now_utc() - timedelta(minutes=10):
        user.last_seen_at = now_utc()
        changed = True
    if changed:
        await session.flush()
    return user


async def get(session: AsyncSession, user_id: int) -> User | None:
    return await session.get(User, user_id)


async def get_many(session: AsyncSession, user_ids: list[int]) -> dict[int, User]:
    if not user_ids:
        return {}
    rows = (await session.execute(select(User).where(User.id.in_(user_ids)))).scalars()
    return {u.id: u for u in rows}


async def by_username(session: AsyncSession, username: str) -> User | None:
    name = username.strip().lstrip("@").lower()
    if not name:
        return None
    return (
        await session.execute(select(User).where(func.lower(User.username) == name).limit(1))
    ).scalar_one_or_none()


async def by_invite(session: AsyncSession, code: str) -> User | None:
    return (
        await session.execute(
            select(User).where(func.upper(User.invite_code) == code.strip().upper()).limit(1)
        )
    ).scalar_one_or_none()


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
    found = await by_username(session, raw)
    if found is not None:
        return found
    # Display names are the last resort, and only when unambiguous: "/rob Sakura"
    # must not silently pick one of five players called Sakura.
    hits = await search(session, raw, limit=2)
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        raise MultipleMatches(raw, len(hits))
    return None


# --------------------------------------------------------------------------- prefs
async def prefs(session: AsyncSession, user_id: int) -> UserPref:
    row = await session.get(UserPref, user_id)
    if row is None:
        row = UserPref(user_id=user_id, flags={})
        session.add(row)
        await session.flush()
    return row


async def set_pref(session: AsyncSession, user_id: int, **values: object) -> UserPref:
    row = await prefs(session, user_id)
    flags = dict(row.flags or {})
    for key, value in values.items():
        if hasattr(row, key):
            setattr(row, key, value)
        else:
            flags[key] = value
    row.flags = flags
    await session.flush()
    return row


# ---------------------------------------------------------------------- moderation
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
        await session.execute(BannedUser.__table__.delete().where(BannedUser.user_id == user_id))


async def is_banned(session: AsyncSession, user_id: int) -> bool:
    user = await session.get(User, user_id)
    if user is not None and user.banned:
        return True
    return (
        await session.execute(
            select(BannedUser.user_id).where(BannedUser.user_id == user_id).limit(1)
        )
    ).scalar_one_or_none() is not None


async def add_warning(session: AsyncSession, user_id: int, *, count: int = 1) -> int:
    await session.execute(
        update(User).where(User.id == user_id).values(warn_count=User.warn_count + count)
    )
    total = (
        await session.execute(select(User.warn_count).where(User.id == user_id))
    ).scalar_one_or_none()
    return int(total or 0)


async def clear_warnings(session: AsyncSession, user_id: int) -> None:
    await session.execute(update(User).where(User.id == user_id).values(warn_count=0))


async def set_role(session: AsyncSession, user_id: int, role: Role) -> None:
    await session.execute(update(User).where(User.id == user_id).values(role=str(role)))


# -------------------------------------------------------------------- leaderboards
LEADERBOARD_METRICS = {
    "balance": User.balance,
    "level": User.level,
    "exp": User.exp,
    "claims": User.pulls_total,
    "high": User.high_pulls,
    "streak": User.streak_count,
}


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


async def rank_of(
    session: AsyncSession, user_id: int, *, metric: str = "balance"
) -> tuple[int, int]:
    column = LEADERBOARD_METRICS.get(metric, User.balance)
    mine = (await session.execute(select(column).where(User.id == user_id))).scalar_one_or_none()
    if mine is None:
        return 0, 0
    better = (
        await session.execute(
            select(func.count()).select_from(User).where(User.banned.is_(False), column > mine)
        )
    ).scalar_one()
    total = (
        await session.execute(select(func.count()).select_from(User).where(User.banned.is_(False)))
    ).scalar_one()
    return int(better) + 1, int(total)


async def search(session: AsyncSession, query: str, *, limit: int = 12) -> list[User]:
    like = f"%{query.strip().lstrip('@')}%"
    stmt = (
        select(User)
        .where(
            or_(User.username.ilike(like), User.first_name.ilike(like), User.last_name.ilike(like))
        )
        .order_by(User.balance.desc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars())


# -------------------------------------------------------------------- aggregates
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
            select(func.count()).select_from(User).where(_as_naive(User.last_seen_at) >= day)
        )
    ).scalar_one()
    newest = (
        await session.execute(
            select(func.count()).select_from(User).where(_as_naive(User.created_at) >= day)
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


def _as_naive(column):
    """Postgres timestamptz vs SQLite text both compare fine against naive UTC."""
    return column


async def touch_activity(
    session: AsyncSession, chat_id: int, user_id: int, *, kind: str = "message"
) -> None:
    session.add(ActivityLog(chat_id=chat_id, user_id=user_id, kind=kind, created_at=now_utc()))


async def expire_stale(session: AsyncSession, *, days: int = 120) -> int:
    """Purge 24h-activity rows to keep the table small (Summon-bot never purged)."""
    cutoff = now_utc() - timedelta(days=days)
    result = await session.execute(
        ActivityLog.__table__.delete().where(
            ActivityLog.created_at < datetime.combine(cutoff.date(), datetime.min.time())
        )
    )
    return int(result.rowcount or 0)
