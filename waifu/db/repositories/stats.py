"""Metrics, snapshots and job checkpoints.

Two-tier design (this is what makes ``/stats`` instant instead of a table scan):

* **live counters** — Redis hashes/ZSETs, incremented on the hot path;
* **snapshots** — a hourly Postgres row, which is what the dashboard graphs and
  ``/stats`` trend line read; Redis may be flushed, history must not.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from waifu.db.models import ActivityLog, KvState, StatsSnapshot, User
from waifu.utils.time import now_utc


async def snapshot(session: AsyncSession, **fields: object) -> StatsSnapshot:
    row = StatsSnapshot(**fields)  # type: ignore[arg-type]
    session.add(row)
    await session.flush()
    return row


async def snapshots(session: AsyncSession, *, limit: int = 48) -> list[StatsSnapshot]:
    rows = (
        await session.execute(select(StatsSnapshot).order_by(StatsSnapshot.ts.desc()).limit(limit))
    ).scalars()
    return list(rows)


async def trend(session: AsyncSession, *, hours: int = 24) -> list[tuple[str, int, int]]:
    rows = (
        await session.execute(
            select(StatsSnapshot.ts, StatsSnapshot.users_active_24h, StatsSnapshot.claims_total)
            .where(StatsSnapshot.ts >= now_utc() - timedelta(hours=hours))
            .order_by(StatsSnapshot.ts.asc())
        )
    ).all()
    return [(r[0].strftime("%m-%d %H:%M"), int(r[1] or 0), int(r[2] or 0)) for r in rows]


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


async def chat_activity(session: AsyncSession, chat_id: int, *, hours: int = 24) -> dict[str, int]:
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


async def purge_activity(session: AsyncSession, *, older_than_days: int = 14) -> int:
    cutoff = now_utc() - timedelta(days=older_than_days)
    result = await session.execute(
        ActivityLog.__table__.delete().where(ActivityLog.created_at < cutoff)
    )
    return int(result.rowcount or 0)


# ------------------------------------------------------------------- kv checkpoint
async def kv_get(session: AsyncSession, key: str, default: dict | None = None) -> dict:
    row = await session.get(KvState, key)
    if row is None:
        return default or {}
    return dict(row.value or {})


async def kv_set(session: AsyncSession, key: str, value: dict) -> None:
    row = await session.get(KvState, key)
    if row is None:
        session.add(KvState(key=key, value=value))
    else:
        row.value = value
    await session.flush()


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


async def table_sizes(session: AsyncSession) -> list[tuple[str, int]]:
    """Postgres-only; empty on the SQLite test harness."""
    try:
        rows = (
            await session.execute(
                text(
                    "SELECT relname, pg_total_relation_size(c.oid) FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'public' AND c.relkind = 'r' ORDER BY 2 DESC LIMIT 20"
                )
            )
        ).all()
    except Exception:  # pragma: no cover - sqlite harness has no pg_class
        # Deliberately no rollback: the caller may hold writes in this transaction,
        # and a failed read-only size probe must not discard them.
        return []
    return [(str(r[0]), int(r[1])) for r in rows]


async def vacuum_analyze(session: AsyncSession, tables: list[str]) -> list[str]:
    """Cheap maintenance the owner can trigger from the /owner panel."""
    done: list[str] = []
    allowed = {t.name for t in StatsSnapshot.__table__.metadata.tables.values()}
    for table in tables:
        if table not in allowed:
            continue
        await session.execute(text(f"ANALYZE {table}"))  # table names come from our own metadata
        done.append(table)
    return done
