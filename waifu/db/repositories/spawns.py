"""Group spawns: the /spawn + auto-spawn + /summon claim race.

The claim is the interesting part. Summon-bot kept the active spawn in
``chat_data`` (in-process, lost on restart, and not shared between shards) and
resolved ``/summon`` by matching a name against that dict, so two players typing
at once both got the character. Here the spawn is a **row**, and claiming is
``UPDATE … WHERE status='active'`` with ``rowcount`` deciding the winner: exactly
one /summon wins, the loser is told they were too slow, and the state survives a
restart.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from waifu.db.models import Character, Group, GuessSession, GuessStreak, SpawnEvent, User
from waifu.errors import AlreadyClaimed, NotFound
from waifu.utils.text import strip_md
from waifu.utils.time import now_utc


@dataclass(slots=True)
class Spawn:
    id: int
    chat_id: int
    character_id: int
    name: str
    anime: str
    rarity_id: int
    rarity: str
    image: str
    message_id: int | None
    rich_message: bool
    source: str
    expected_name: str
    expires_at: datetime
    hint_used: bool
    status: str = "active"


def _hydrate(row: SpawnEvent, char: Character) -> Spawn:
    return Spawn(
        id=row.id,
        chat_id=row.chat_id,
        character_id=char.id,
        name=char.name,
        anime=char.anime,
        rarity_id=int(char.rarity_id),
        rarity=char.rarity,
        image=char.image_ref(),
        message_id=row.message_id,
        rich_message=bool(row.rich_message),
        source=row.source,
        expected_name=row.expected_name,
        expires_at=row.expires_at,
        hint_used=bool(row.hint_used),
        status=row.status,
    )


async def open_spawn(
    session: AsyncSession,
    *,
    chat_id: int,
    character_id: int,
    source: str = "manual",
    ttl_seconds: int = 180,
    payload: dict | None = None,
) -> SpawnEvent:
    """Create the spawn row. Any previous *active* spawn in that chat is expired."""
    await session.execute(
        update(SpawnEvent)
        .where(SpawnEvent.chat_id == chat_id, SpawnEvent.status == "active")
        .values(status="expired")
    )
    char = await session.get(Character, character_id)
    row = SpawnEvent(
        chat_id=chat_id,
        character_id=character_id,
        source=source,
        status="active",
        expected_name=strip_md(char.name).lower() if char else "",
        expires_at=now_utc() + timedelta(seconds=ttl_seconds),
        payload=payload or {},
    )
    session.add(row)
    await session.flush()
    return row


async def attach_message(
    session: AsyncSession, spawn_id: int, message_id: int, *, rich: bool
) -> None:
    await session.execute(
        update(SpawnEvent)
        .where(SpawnEvent.id == spawn_id)
        .values(message_id=message_id, rich_message=rich)
    )
    await session.flush()


async def current(session: AsyncSession, chat_id: int) -> tuple[Spawn | None, SpawnEvent | None]:
    pair = (
        await session.execute(
            select(SpawnEvent, Character)
            .join(Character, Character.id == SpawnEvent.character_id)
            .where(
                SpawnEvent.chat_id == chat_id,
                SpawnEvent.status == "active",
                SpawnEvent.expires_at > now_utc(),
            )
            .order_by(SpawnEvent.id.desc())
            .limit(1)
        )
    ).first()
    if pair is None:
        return None, None
    return _hydrate(pair[0], pair[1]), pair[0]


async def by_id(session: AsyncSession, spawn_id: int) -> Spawn | None:
    pair = (
        await session.execute(
            select(SpawnEvent, Character)
            .join(Character, Character.id == SpawnEvent.character_id)
            .where(SpawnEvent.id == spawn_id)
        )
    ).first()
    return _hydrate(pair[0], pair[1]) if pair else None


async def mark_hint_used(session: AsyncSession, spawn_id: int) -> bool:
    """One hint per spawn — CAS on ``hint_used`` so two taps can't both consume it."""
    result = await session.execute(
        update(SpawnEvent)
        .where(
            SpawnEvent.id == spawn_id,
            SpawnEvent.hint_used.is_(False),
            SpawnEvent.status == "active",
        )
        .values(hint_used=True)
    )
    await session.flush()
    return bool(result.rowcount)


async def claim(session: AsyncSession, spawn_id: int, user_id: int) -> Spawn:
    """The race: only one UPDATE can flip ``active`` → ``claimed``."""
    result = await session.execute(
        update(SpawnEvent)
        .where(
            SpawnEvent.id == spawn_id,
            SpawnEvent.status == "active",
            SpawnEvent.expires_at > now_utc(),
        )
        .values(status="claimed", claimed_by=user_id, claimed_at=now_utc())
        .returning(SpawnEvent.chat_id)
    )
    if result.scalar_one_or_none() is None:
        row = await session.get(SpawnEvent, spawn_id)
        if row is None:
            raise NotFound("that spawn is gone")
        raise AlreadyClaimed(
            "someone reached it first" if row.status == "claimed" else "this spawn has expired"
        )
    pair = (
        await session.execute(
            select(SpawnEvent, Character)
            .join(Character, Character.id == SpawnEvent.character_id)
            .where(SpawnEvent.id == spawn_id)
        )
    ).first()
    assert pair is not None
    return _hydrate(pair[0], pair[1])


async def name_matches(spawn: Spawn, typed: str) -> bool:
    """Summon-bot accepted partial/substring guesses; keep that, but require ≥3 chars."""
    typed = strip_md(typed).strip().lower()
    if not typed:
        return False
    target = spawn.expected_name.lower().strip()
    return typed == target or (len(typed) >= 3 and (typed in target or target.startswith(typed)))


async def expire_overdue(session: AsyncSession) -> int:
    result = await session.execute(
        update(SpawnEvent)
        .where(SpawnEvent.status == "active", SpawnEvent.expires_at <= now_utc())
        .values(status="expired")
    )
    return int(result.rowcount or 0)


async def recent_for_chat(
    session: AsyncSession, chat_id: int, *, limit: int = 5
) -> list[SpawnEvent]:
    return list(
        (
            await session.execute(
                select(SpawnEvent)
                .where(SpawnEvent.chat_id == chat_id)
                .order_by(SpawnEvent.id.desc())
                .limit(limit)
            )
        ).scalars()
    )


async def chat_activity(
    session: AsyncSession, chat_id: int, *, since: datetime | None = None
) -> dict[str, int]:
    conds = [SpawnEvent.chat_id == chat_id]
    if since:
        conds.append(SpawnEvent.spawns_at >= since)
    row = (
        (
            await session.execute(
                select(
                    func.count(SpawnEvent.id).label("spawns"),
                    func.count(SpawnEvent.id)
                    .filter(SpawnEvent.status == "claimed")
                    .label("claimed"),
                ).where(*conds)
            )
        )
        .mappings()
        .one()
    )
    spawns, claimed = int(row["spawns"] or 0), int(row["claimed"] or 0)
    return {
        "spawns": spawns,
        "claimed": claimed,
        "claim_rate": round(claimed / spawns * 100) if spawns else 0,
    }


# ------------------------------------------------------------------ group settings
async def group(
    session: AsyncSession, chat_id: int, *, create: bool = False, title: str = ""
) -> Group | None:
    row = await session.get(Group, chat_id)
    if row is None and create:
        row = Group(chat_id=chat_id, title=title[:255], message_count=0)
        session.add(row)
        await session.flush()
    elif row is not None and title and row.title != title:
        row.title = title[:255]
        await session.flush()
    return row


def _switches(row: Group) -> dict[str, bool]:
    return dict((row.data or {}).get("switches") or {})


async def set_group_switch(
    session: AsyncSession, chat_id: int, key: str, *, value: bool, title: str = ""
) -> dict[str, bool]:
    """Flip a free-form per-group switch and return the whole set.

    ``set_group_flags`` deliberately refuses unknown keys, because a typo in
    ``/setgroup`` otherwise reads as "it didn't work". A switch with no column — the
    auto-add feed — lives in ``Group.data`` instead of forcing a migration for a
    boolean nobody queries.
    """
    row = await group(session, chat_id, create=True, title=title)
    if row is None:  # pragma: no cover - create=True guarantees a row
        raise NotFound("group row missing")
    data = dict(row.data or {})
    switches = _switches(row)
    switches[str(key)] = bool(value)
    data["switches"] = switches
    row.data = data  # reassigned: JSON columns only persist a *new* object
    await session.flush()
    return switches


async def group_switch(
    session: AsyncSession, chat_id: int, key: str, *, default: bool = False
) -> bool:
    row = await session.get(Group, chat_id)
    if row is None:
        return default
    return bool(_switches(row).get(key, default))


async def bump_message_count(
    session: AsyncSession, chat_id: int, *, spawn_limit_default: int = 100
) -> tuple[int, int, bool]:
    """Count group chatter; returns ``(count, limit, threshold_hit)``.

    Replaces Summon-bot's ``UPDATE … SET message_count = <python int>`` (which
    lost counts under load) with a single atomic increment.
    """
    from sqlalchemy import text as sa_text

    result = await session.execute(
        update(Group)
        .where(Group.chat_id == chat_id, Group.spawn_enabled.is_(True))
        .values(message_count=Group.message_count + 1)
        .returning(Group.message_count, Group.spawn_limit)
    )
    row = result.first()
    if row is None:
        await session.execute(
            sa_text(
                "INSERT INTO groups (chat_id, title, message_count, spawn_limit, is_registered, spawn_enabled,"
                " auto_ban_spam, spam_limit, welcome_enabled, created_at, updated_at)"
                " VALUES (:cid, '', 1, :lim, true, true, true, 20, false, now(), now())"
                " ON CONFLICT (chat_id) DO NOTHING"
            ),
            {"cid": chat_id, "lim": spawn_limit_default},
        )
        await session.flush()
        return 1, spawn_limit_default, False
    count, limit = int(row[0]), int(row[1])
    hit = count >= limit
    if hit:
        await session.execute(update(Group).where(Group.chat_id == chat_id).values(message_count=0))
        await session.flush()
    return count, limit, hit


async def set_spawn_limit(session: AsyncSession, chat_id: int, limit: int) -> None:
    await session.execute(
        update(Group)
        .where(Group.chat_id == chat_id)
        .values(spawn_limit=max(5, int(limit)), message_count=0)
    )
    await session.flush()


async def schedule_next_spawn(session: AsyncSession, chat_id: int, when: datetime) -> None:
    await session.execute(
        update(Group)
        .where(Group.chat_id == chat_id)
        .values(next_spawn_at=when, last_spawn_at=now_utc(), message_count=0)
    )
    await session.flush()


async def spawnable_groups(session: AsyncSession, *, due_only: bool = False) -> list[Group]:
    conds = [Group.is_registered.is_(True), Group.spawn_enabled.is_(True)]
    if due_only:
        conds.append(Group.next_spawn_at.is_not(None))
    return list(
        (
            await session.execute(
                select(Group).where(*conds).order_by(Group.next_spawn_at.nulls_last())
            )
        ).scalars()
    )


async def register_group(
    session: AsyncSession, chat_id: int, *, title: str = "", spawn_limit: int = 100
) -> Group:
    row = await group(session, chat_id, create=True, title=title)
    assert row is not None
    row.is_registered = True
    row.spawn_limit = max(5, int(spawn_limit))
    await session.flush()
    return row


async def unregister_group(session: AsyncSession, chat_id: int) -> None:
    await session.execute(Group.__table__.delete().where(Group.chat_id == chat_id))


async def groups_summary(session: AsyncSession) -> dict[str, int]:
    row = (
        (
            await session.execute(
                select(
                    func.count(Group.chat_id).label("total"),
                    func.count(Group.chat_id)
                    .filter(Group.spawn_enabled.is_(True))
                    .label("spawning"),
                    func.coalesce(func.sum(Group.message_count), 0).label("messages"),
                )
            )
        )
        .mappings()
        .one()
    )
    return {
        "groups": int(row["total"] or 0),
        "spawning": int(row["spawning"] or 0),
        "messages": int(row["messages"] or 0),
    }


# ---------------------------------------------------------------------- /nguess game
async def start_guess(
    session: AsyncSession,
    *,
    chat_id: int,
    character_id: int,
    reward: int,
    seconds: int,
    mode: str = "poll",
) -> GuessSession:
    await session.execute(
        GuessSession.__table__.delete().where(
            GuessSession.chat_id == chat_id, GuessSession.status == "open"
        )
    )
    row = GuessSession(
        chat_id=chat_id,
        character_id=character_id,
        status="open",
        mode=mode,
        reward=reward,
        closes_at=now_utc() + timedelta(seconds=seconds),
    )
    session.add(row)
    await session.flush()
    return row


async def active_guess(session: AsyncSession, chat_id: int) -> GuessSession | None:
    return (
        await session.execute(
            select(GuessSession)
            .where(
                GuessSession.chat_id == chat_id,
                GuessSession.status == "open",
                GuessSession.closes_at > now_utc(),
            )
            .limit(1)
        )
    ).scalar_one_or_none()


async def attach_guess_message(session: AsyncSession, session_id: int, message_id: int) -> None:
    await session.execute(
        update(GuessSession).where(GuessSession.id == session_id).values(message_id=message_id)
    )
    await session.flush()


async def resolve_guess(
    session: AsyncSession, session_id: int, *, winner_id: int | None, answers: int = 0
) -> GuessSession:
    row = await session.get(GuessSession, session_id)
    if row is None:
        raise NotFound("round is gone")
    row.status = "won" if winner_id else "expired"
    row.winner_id = winner_id
    row.answers = answers
    await session.flush()
    return row


async def claim_guess(session: AsyncSession, session_id: int, user_id: int) -> GuessSession:
    """First correct answer wins — same CAS idea as the spawn claim."""
    result = await session.execute(
        update(GuessSession)
        .where(
            GuessSession.id == session_id,
            GuessSession.status == "open",
            GuessSession.closes_at > now_utc(),
        )
        .values(status="won", winner_id=user_id)
        .returning(GuessSession.reward)
    )
    reward = result.scalar_one_or_none()
    if reward is None:
        raise AlreadyClaimed("someone already won this round")
    row = await session.get(GuessSession, session_id)
    assert row is not None
    await session.flush()
    return row


async def guesses_to_close(session: AsyncSession) -> list[GuessSession]:
    return list(
        (
            await session.execute(
                select(GuessSession).where(
                    GuessSession.status == "open", GuessSession.closes_at <= now_utc()
                )
            )
        ).scalars()
    )


async def guess_streak(session: AsyncSession, chat_id: int) -> tuple[int, int | None]:
    row = await session.get(GuessStreak, chat_id)
    return (row.current_streak, row.last_correct_user) if row else (0, None)


async def bump_guess_streak(session: AsyncSession, chat_id: int, *, user_id: int, won: bool) -> int:
    row = await session.get(GuessStreak, chat_id)
    if row is None:
        row = GuessStreak(chat_id=chat_id, current_streak=0, total_rounds=0)
        session.add(row)
    row.total_rounds += 1
    if won:
        row.current_streak = row.current_streak + 1 if row.last_correct_user == user_id else 1
        row.last_correct_user = user_id
    else:
        row.current_streak = 0
    await session.flush()
    return row.current_streak


async def last_guess_characters(
    session: AsyncSession, chat_id: int, *, limit: int = 8
) -> list[int]:
    rows = (
        await session.execute(
            select(GuessSession.character_id)
            .where(GuessSession.chat_id == chat_id)
            .order_by(GuessSession.id.desc())
            .limit(limit)
        )
    ).scalars()
    return [int(r) for r in rows]


async def users_seen_in(session: AsyncSession, chat_id: int, *, since: datetime) -> int:
    from waifu.db.models import ActivityLog

    return int(
        (
            await session.execute(
                select(func.count(func.distinct(ActivityLog.user_id))).where(
                    ActivityLog.chat_id == chat_id, ActivityLog.created_at >= since
                )
            )
        ).scalar_one()
        or 0
    )


async def top_guessers(session: AsyncSession, *, limit: int = 10) -> list[tuple[User, int]]:
    rows = (
        await session.execute(
            select(User, func.count(GuessSession.id).label("wins"))
            .join(GuessSession, GuessSession.winner_id == User.id)
            .group_by(User.id)
            .order_by(func.count(GuessSession.id).desc())
            .limit(limit)
        )
    ).all()
    return [(r[0], int(r[1])) for r in rows]
