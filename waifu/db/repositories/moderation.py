"""Admin/sudo surface: sudo admins, warnings, mutes/bans, cases, audit trail.

Summon-bot's ``/remove`` deleted messages but never recorded who removed what;
``/warn`` bumped a counter with no history. Both are recorded here, and every
privileged command calls :func:`audit` in the same transaction as its effect.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from waifu.db.models import AuditLog, BannedUser, ModerationCase, SudoAdmin, User, Warning
from waifu.errors import Locked, NotFound
from waifu.utils.time import now_utc

# Fine-grained capabilities so /editsudo can grant "spawn only" without god mode.
PERMISSIONS = (
    "spawn",
    "manage_chars",
    "moderate",
    "economy",
    "broadcast",
    "group_admin",
    "market",
    "view_audit",
)
ALL_PERMS = dict.fromkeys(PERMISSIONS, True)
DEFAULT_SUDO_PERMS = dict.fromkeys(("spawn", "moderate", "group_admin"), True)


async def sudo_list(session: AsyncSession, *, active_only: bool = True) -> list[SudoAdmin]:
    stmt = select(SudoAdmin).order_by(SudoAdmin.created_at.desc())
    if active_only:
        stmt = stmt.where(SudoAdmin.is_active.is_(True))
    return list((await session.execute(stmt)).scalars())


async def sudo_row(session: AsyncSession, user_id: int) -> SudoAdmin | None:
    return await session.get(SudoAdmin, user_id)


async def add_sudo(
    session: AsyncSession,
    user_id: int,
    *,
    added_by: int,
    username: str = "",
    permissions: dict | None = None,
) -> SudoAdmin:
    if await session.get(User, user_id) is None and not username:
        raise NotFound("that user has never used the bot — ask them to /start first")
    row = await session.get(SudoAdmin, user_id)
    if row is not None and row.is_active:
        raise Locked("already a sudo admin (use /editsudo to change)")
    if row is None:
        row = SudoAdmin(
            user_id=user_id,
            username=username[:64],
            added_by=added_by,
            permissions=permissions or dict(DEFAULT_SUDO_PERMS),
        )
        session.add(row)
    else:
        row.is_active = True
        row.permissions = permissions or dict(DEFAULT_SUDO_PERMS)
        row.username = username[:64] or row.username
    await session.flush()
    return row


async def update_sudo(
    session: AsyncSession,
    user_id: int,
    *,
    permissions: dict | None = None,
    active: bool | None = None,
) -> SudoAdmin:
    row = await session.get(SudoAdmin, user_id)
    if row is None:
        raise NotFound("not a sudo admin")
    if permissions is not None:
        unknown = set(permissions) - set(PERMISSIONS)
        if unknown:
            raise ValueError(f"unknown permission(s): {', '.join(sorted(unknown))}")
        row.permissions = permissions
    if active is not None:
        row.is_active = active
    await session.flush()
    return row


async def remove_sudo(session: AsyncSession, user_id: int) -> None:
    await session.execute(SudoAdmin.__table__.delete().where(SudoAdmin.user_id == user_id))
    await session.flush()


async def permissions_for(session: AsyncSession, user_id: int) -> dict[str, bool]:
    """Effective permission map (owner/admin → everything, sudo → its grants)."""
    user = await session.get(User, user_id)
    if user is not None and user.role in {"owner", "admin"}:
        return dict(ALL_PERMS)
    row = await sudo_row(session, user_id)
    if row is None or not row.is_active:
        return {}
    return {k: bool(v) for k, v in (row.permissions or {}).items() if k in PERMISSIONS}


# ---------------------------------------------------------------------- warnings
async def add_warning(
    session: AsyncSession,
    *,
    chat_id: int,
    user_id: int,
    moderator_id: int,
    reason: str = "",
) -> tuple[Warning, int]:
    row = Warning(
        chat_id=chat_id,
        user_id=user_id,
        moderator_id=moderator_id,
        reason=reason[:255],
        created_at=now_utc(),
    )
    session.add(row)
    await session.execute(
        update(User).where(User.id == user_id).values(warn_count=User.warn_count + 1)
    )
    await session.flush()
    total = (
        await session.execute(select(User.warn_count).where(User.id == user_id))
    ).scalar_one_or_none()
    return row, int(total or 0)


async def warnings_for(
    session: AsyncSession, chat_id: int, user_id: int, *, unresolved_only: bool = True
) -> list[Warning]:
    conds = [Warning.chat_id == chat_id, Warning.user_id == user_id]
    if unresolved_only:
        conds.append(Warning.is_resolved.is_(False))
    return list(
        (await session.execute(select(Warning).where(*conds).order_by(Warning.id.desc()))).scalars()
    )


async def remove_warning(
    session: AsyncSession, chat_id: int, user_id: int, *, count: int = 1
) -> int:
    rows = list(
        (
            await session.execute(
                select(Warning)
                .where(
                    Warning.chat_id == chat_id,
                    Warning.user_id == user_id,
                    Warning.is_resolved.is_(False),
                )
                .order_by(Warning.id.desc())
                .limit(count)
            )
        ).scalars()
    )
    for row in rows:
        row.is_resolved = True
    remaining = (
        await session.execute(
            select(func.count())
            .select_from(Warning)
            .where(
                Warning.chat_id == chat_id,
                Warning.user_id == user_id,
                Warning.is_resolved.is_(False),
            )
        )
    ).scalar_one()
    await session.execute(update(User).where(User.id == user_id).values(warn_count=int(remaining)))
    await session.flush()
    return int(remaining)


async def warning_counts(
    session: AsyncSession, chat_id: int, *, limit: int = 20
) -> list[tuple[int, int]]:
    rows = (
        await session.execute(
            select(Warning.user_id, func.count(Warning.id))
            .where(Warning.chat_id == chat_id, Warning.is_resolved.is_(False))
            .group_by(Warning.user_id)
            .order_by(func.count(Warning.id).desc())
            .limit(limit)
        )
    ).all()
    return [(int(r[0]), int(r[1])) for r in rows]


# ----------------------------------------------------------- cases (mute/ban/kick)
async def open_case(
    session: AsyncSession,
    *,
    chat_id: int,
    target_user_id: int,
    moderator_id: int,
    action: str,
    reason: str = "",
    duration: int = 0,
    message_ids: dict | None = None,
    member_tag: str = "",
) -> ModerationCase:
    row = ModerationCase(
        chat_id=chat_id,
        target_user_id=target_user_id,
        moderator_id=moderator_id,
        action=action,
        reason=reason[:255],
        duration_seconds=max(0, int(duration)),
        message_ids=message_ids or {},
        member_tag=member_tag[:48],
        expires_at=now_utc() + timedelta(seconds=duration) if duration else None,
    )
    session.add(row)
    await session.flush()
    return row


async def close_case(session: AsyncSession, case_id: int) -> None:
    await session.execute(
        update(ModerationCase).where(ModerationCase.id == case_id).values(is_active=False)
    )
    await session.flush()


async def close_active(
    session: AsyncSession, chat_id: int, user_id: int, *, action: str | None = None
) -> int:
    conds = [
        ModerationCase.chat_id == chat_id,
        ModerationCase.target_user_id == user_id,
        ModerationCase.is_active.is_(True),
    ]
    if action:
        conds.append(ModerationCase.action == action)
    result = await session.execute(update(ModerationCase).where(*conds).values(is_active=False))
    return int(result.rowcount or 0)


async def cases(
    session: AsyncSession, chat_id: int, *, user_id: int | None = None, limit: int = 25
) -> list[ModerationCase]:
    conds = [ModerationCase.chat_id == chat_id]
    if user_id:
        conds.append(ModerationCase.target_user_id == user_id)
    return list(
        (
            await session.execute(
                select(ModerationCase).where(*conds).order_by(ModerationCase.id.desc()).limit(limit)
            )
        ).scalars()
    )


async def due_unmutes(session: AsyncSession) -> list[ModerationCase]:
    return list(
        (
            await session.execute(
                select(ModerationCase).where(
                    ModerationCase.is_active.is_(True),
                    ModerationCase.action == "mute",
                    ModerationCase.expires_at <= now_utc(),
                )
            )
        ).scalars()
    )


# ------------------------------------------------------------------- global bans
async def ban_list(session: AsyncSession, *, limit: int = 50) -> list[BannedUser]:
    return list(
        (
            await session.execute(
                select(BannedUser).order_by(BannedUser.created_at.desc()).limit(limit)
            )
        ).scalars()
    )


async def is_globally_banned(session: AsyncSession, user_id: int) -> bool:
    row = await session.get(BannedUser, user_id)
    if row is None:
        return False
    if row.expires_at is not None and row.expires_at.replace(tzinfo=None) < now_utc():
        await session.execute(BannedUser.__table__.delete().where(BannedUser.user_id == user_id))
        await session.flush()
        return False
    return True


# ------------------------------------------------------------------- audit trail
async def audit(
    session: AsyncSession,
    *,
    actor_id: int,
    action: str,
    target: str = "",
    detail: str = "",
    chat_id: int | None = None,
    scope: str = "global",
) -> None:
    session.add(
        AuditLog(
            actor_id=actor_id,
            action=action[:48],
            target=target[:96],
            detail=detail[:4000],
            chat_id=chat_id,
            scope=scope,
        )
    )
    await session.flush()


async def audit_rows(
    session: AsyncSession,
    *,
    limit: int = 30,
    actor_id: int | None = None,
    chat_id: int | None = None,
    contains: str = "",
) -> list[AuditLog]:
    conds = []
    if actor_id:
        conds.append(AuditLog.actor_id == actor_id)
    if chat_id:
        conds.append(AuditLog.chat_id == chat_id)
    stmt = select(AuditLog).where(*conds) if conds else select(AuditLog)
    if contains:
        stmt = stmt.where(
            or_(AuditLog.action.ilike(f"%{contains}%"), AuditLog.detail.ilike(f"%{contains}%"))
        )
    return list((await session.execute(stmt.order_by(AuditLog.id.desc()).limit(limit))).scalars())


async def audit_purge(session: AsyncSession, *, older_than_days: int = 180) -> int:
    cutoff = now_utc() - timedelta(days=older_than_days)
    result = await session.execute(AuditLog.__table__.delete().where(AuditLog.created_at < cutoff))
    return int(result.rowcount or 0)


async def moderation_summary(session: AsyncSession, chat_id: int) -> dict[str, int]:
    row = (
        (
            await session.execute(
                select(
                    func.count(ModerationCase.id).label("cases"),
                    func.count(ModerationCase.id)
                    .filter(ModerationCase.is_active.is_(True))
                    .label("active"),
                    func.count(func.distinct(ModerationCase.target_user_id)).label("targets"),
                ).where(ModerationCase.chat_id == chat_id)
            )
        )
        .mappings()
        .one()
    )
    return {
        "cases": int(row["cases"] or 0),
        "active": int(row["active"] or 0),
        "targets": int(row["targets"] or 0),
    }
