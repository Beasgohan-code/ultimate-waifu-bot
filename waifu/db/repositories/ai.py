"""``ai_messages`` access: transcripts, per-character history, retention."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from waifu.db.models import AiMessageLog
from waifu.utils.time import now_utc


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


async def flag(session: AsyncSession, message_id: int, *, flagged: bool = True) -> None:
    from sqlalchemy import update

    await session.execute(
        update(AiMessageLog).where(AiMessageLog.id == message_id).values(flagged=flagged)
    )
    await session.flush()


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


async def purge(session: AsyncSession, user_id: int, *, character_id: int | None = None) -> int:
    conds = [AiMessageLog.user_id == user_id]
    if character_id is not None:
        conds.append(AiMessageLog.character_id == character_id)
    result = await session.execute(delete(AiMessageLog).where(*conds))
    return int(result.rowcount or 0)


async def purge_older_than(session: AsyncSession, days: int = 30) -> int:
    cutoff = now_utc().replace(microsecond=0, second=0, minute=0) - __import__(
        "datetime"
    ).timedelta(days=days)
    result = await session.execute(delete(AiMessageLog).where(AiMessageLog.created_at < cutoff))
    return int(result.rowcount or 0)
