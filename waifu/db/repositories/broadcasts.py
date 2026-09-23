"""Queued announcements (``/broadcast at 20:00 …``).

A scheduled broadcast is a row, not a timer: the jobs loop claims due rows with
an atomic UPDATE (``sent_at IS NULL`` guard), so two instances — or the loop
and a manual ``waifu jobs --name broadcasts`` — cannot send the same line
twice. The same claim pattern the auction settlement uses.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from waifu.db.models import ScheduledBroadcast
from waifu.utils.time import now_utc


async def schedule(
    session: AsyncSession, *, run_at: datetime, text: str, created_by: int
) -> ScheduledBroadcast:
    row = ScheduledBroadcast(run_at=run_at, text=text[:4000], created_by=created_by)
    session.add(row)
    await session.flush()
    return row


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
        (await session.execute(select(ScheduledBroadcast).where(ScheduledBroadcast.id.in_(ids))))
        .scalars()
        .all()
    )
    return list(rows)


async def cancel_all(session: AsyncSession) -> int:
    """Drop every unsent announcement (sent rows stay as the audit trail)."""
    from sqlalchemy import delete

    result = await session.execute(
        delete(ScheduledBroadcast).where(ScheduledBroadcast.sent_at.is_(None))
    )
    return int(result.rowcount or 0)
