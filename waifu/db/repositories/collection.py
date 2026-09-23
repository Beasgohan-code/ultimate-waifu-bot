"""Collection ("harem") writes — the /summon claim, /gift, /sell, /fav, /hmode path.

Every mutation is a conditional statement with a checked ``rowcount``:

* ``grant`` → ``INSERT … ON CONFLICT DO UPDATE SET count = count + 1``
* ``consume`` → ``UPDATE … WHERE count >= n AND is_locked IS FALSE``

so Summon-bot's two classic bugs (self-buy in the market, and losing/duplicating
copies when two handlers wrote the same row) cannot happen here.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import case, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as lite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from waifu.db.models import Character, Ownership, User
from waifu.enums import Rarity
from waifu.errors import Locked, NotFound
from waifu.utils.time import now_utc


@dataclass(slots=True)
class Owned:
    """Flattened (ownership, character) pair — what every UI renderer consumes."""

    character_id: int
    name: str
    anime: str
    rarity_id: int
    rarity: str
    count: int
    price: int
    is_favorite: bool
    is_locked: bool
    image: str
    stat_power: int = 0

    @property
    def tier(self) -> Rarity:
        return Rarity.from_value(self.rarity_id)

    @property
    def value(self) -> int:
        return self.price * max(1, self.count)


def _own(o: Ownership, c: Character) -> Owned:
    return Owned(
        character_id=c.id,
        name=c.name,
        anime=c.anime,
        rarity_id=int(c.rarity_id),
        rarity=c.rarity or Rarity.from_value(c.rarity_id).badge,
        count=int(o.count),
        price=int(c.price or 0),
        is_favorite=bool(o.is_favorite),
        is_locked=bool(o.is_locked),
        image=c.image_ref(),
        stat_power=int(c.stat_power or 0),
    )


async def grant(
    session: AsyncSession,
    user_id: int,
    character_id: int,
    *,
    count: int = 1,
    source: str = "claim",
) -> Ownership:
    """Add copies; creates the row on first contact. Idempotent-friendly upsert."""
    dialect = session.bind.dialect.name if session.bind else "postgresql"
    insert = (pg_insert if dialect == "postgresql" else lite_insert)(Ownership)
    await session.execute(
        insert.values(
            user_id=user_id,
            character_id=character_id,
            count=count,
            source=source,
            first_obtained=now_utc(),
            last_obtained=now_utc(),
        ).on_conflict_do_update(
            index_elements=[Ownership.user_id, Ownership.character_id],
            set_={"count": Ownership.count + count, "last_obtained": now_utc()},
        )
    )
    row = (
        await session.execute(
            select(Ownership).where(
                Ownership.user_id == user_id, Ownership.character_id == character_id
            )
        )
    ).scalar_one()
    await session.flush()
    return row


async def consume(
    session: AsyncSession,
    user_id: int,
    character_id: int,
    *,
    count: int = 1,
    releasing: bool = False,
) -> None:
    """Remove copies. Raises ``NotFound``/``Locked`` with player-readable reasons.

    ``releasing=True`` is for escrow settlement (an auction that just closed, a trade
    both sides accepted). Those rows are locked *by* the escrow, so refusing to move
    them here would strand a character in an auction that can never complete — the
    coins are already escrowed and the item would exist in neither account. Clearing
    the flag in the same UPDATE also stops the buyer inheriting a copy they cannot
    sell, which a seller-only unlock would do.
    """
    conditions = [
        Ownership.user_id == user_id,
        Ownership.character_id == character_id,
        Ownership.count >= count,
    ]
    if not releasing:
        conditions.append(Ownership.is_locked.is_(False))
    values: dict[str, object] = {"count": Ownership.count - count, "last_obtained": now_utc()}
    if releasing:
        values["is_locked"] = False
    result = await session.execute(update(Ownership).where(*conditions).values(**values))
    if not result.rowcount:
        row = (
            await session.execute(
                select(Ownership).where(
                    Ownership.user_id == user_id, Ownership.character_id == character_id
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise NotFound("you don't have that character")
        if row.is_locked:
            raise Locked("that character is locked — /sell unlock it first")
        raise NotFound(f"you only have {row.count} copy(ies) of that")
    remaining = (
        await session.execute(
            select(Ownership.count).where(
                Ownership.user_id == user_id, Ownership.character_id == character_id
            )
        )
    ).scalar_one()
    if int(remaining) <= 0:
        await session.execute(
            Ownership.__table__.delete().where(
                Ownership.user_id == user_id, Ownership.character_id == character_id
            )
        )
    await session.flush()


async def random_owned(
    session: AsyncSession, user_id: int, *, min_rarity: int = 1, include_locked: bool = False
) -> tuple[Character, int] | None:
    """A random character the player actually holds, with the copy count.

    ``/bomb`` uses this (``ORDER BY RANDOM() LIMIT 1`` over ``user_collection`` in the reference),
    and so does every "steal something" effect. The join is on purpose: the caller needs the
    character row to name it in the receipt, and a second query there is a TOCTOU window in which
    the copy can be sold out from under the loot.

    ``include_locked`` defaults to False: the reference looted from ``user_collection`` with no
    lock check, so a /bomb could take a favourite a player had pinned. Here a locked copy is
    simply not a candidate, and if that leaves nothing the target is "safe".
    """
    row = (
        await session.execute(
            select(Ownership, Character)
            .join(Character, Character.id == Ownership.character_id)
            .where(
                Ownership.user_id == user_id,
                Ownership.count > 0,
                Character.rarity_id >= int(min_rarity),
                Character.is_active.is_(True),
                *([] if include_locked else [Ownership.is_locked.is_(False)]),
            )
            .order_by(func.random())
            .limit(1)
        )
    ).first()
    if row is None:
        return None
    return row[1], int(row[0].count)


async def has_count(session: AsyncSession, user_id: int, character_id: int) -> int:
    value = (
        await session.execute(
            select(Ownership.count).where(
                Ownership.user_id == user_id, Ownership.character_id == character_id
            )
        )
    ).scalar_one_or_none()
    return int(value or 0)


async def owned_row(session: AsyncSession, user_id: int, character_id: int) -> Owned | None:
    row = (
        await session.execute(
            select(Ownership, Character)
            .join(Character, Character.id == Ownership.character_id)
            .where(Ownership.user_id == user_id, Ownership.character_id == character_id)
        )
    ).first()
    return _own(row[0], row[1]) if row else None


async def set_flag(
    session: AsyncSession, user_id: int, character_id: int, flag: str, value: bool
) -> None:
    """``is_favorite`` (single) / ``is_locked`` (per copy) — /fav and /sell lock."""
    if flag not in {"is_favorite", "is_locked"}:
        raise ValueError(f"unsupported flag {flag!r}")
    if flag == "is_favorite" and value:
        await session.execute(
            update(Ownership)
            .where(Ownership.user_id == user_id, Ownership.is_favorite.is_(True))
            .values(is_favorite=False)
        )
    await session.execute(
        update(Ownership)
        .where(Ownership.user_id == user_id, Ownership.character_id == character_id)
        .values(**{flag: value})
    )
    await session.flush()


HMODE_ORDERS = {
    "rarity": [Character.rarity_id.desc(), Character.name.asc()],
    "anime": [Character.anime.asc(), Character.rarity_id.desc()],
    "name": [Character.name.asc()],
    "recent": [Ownership.last_obtained.desc()],
    "count": [Ownership.count.desc(), Character.rarity_id.desc()],
    "fav": [Ownership.is_favorite.desc(), Character.rarity_id.desc()],
    "value": [Character.price.desc()],
}


async def list_owned(
    session: AsyncSession,
    user_id: int,
    *,
    hmode: str = "rarity",
    rarity_id: int | None = None,
    query: str = "",
    dupes_only: bool = False,
    page_size: int = 8,
    page: int = 0,
) -> tuple[list[Owned], int]:
    conds = [Ownership.user_id == user_id, Ownership.count > 0]
    if rarity_id is not None:
        conds.append(Character.rarity_id == int(rarity_id))
    if dupes_only:
        conds.append(Ownership.count > 1)
    if query:
        like = f"%{query}%"
        conds.append(or_(Character.name.ilike(like), Character.anime.ilike(like)))

    total = (
        await session.execute(
            select(func.count(Ownership.id))
            .select_from(Ownership)
            .join(Character, Character.id == Ownership.character_id)
            .where(*conds)
        )
    ).scalar_one()
    rows = (
        await session.execute(
            select(Ownership, Character)
            .join(Character, Character.id == Ownership.character_id)
            .where(*conds)
            .order_by(*HMODE_ORDERS.get(hmode, HMODE_ORDERS["rarity"]))
            .limit(page_size)
            .offset(page * page_size)
        )
    ).all()
    return [_own(o, c) for o, c in rows], int(total)


async def summary(session: AsyncSession, user_id: int) -> dict[str, int]:
    row = (
        (
            await session.execute(
                select(
                    func.count(func.distinct(Ownership.character_id)).label("unique"),
                    func.coalesce(func.sum(Ownership.count), 0).label("total"),
                    func.coalesce(func.sum(Character.price * Ownership.count), 0).label("value"),
                    func.coalesce(
                        func.sum(
                            case(
                                (Character.rarity_id >= int(Rarity.LEGENDARY), Ownership.count),
                                else_=0,
                            )
                        ),
                        0,
                    ).label("high"),
                )
                .select_from(Ownership)
                .join(Character, Character.id == Ownership.character_id)
                .where(Ownership.user_id == user_id, Ownership.count > 0)
            )
        )
        .mappings()
        .one()
    )
    return {
        "unique": int(row["unique"] or 0),
        "total": int(row["total"] or 0),
        "value": int(row["value"] or 0),
        "high_tier": int(row["high"] or 0),
    }


async def per_rarity(session: AsyncSession, user_id: int) -> dict[int, int]:
    rows = (
        await session.execute(
            select(
                Character.rarity_id,
                func.count(func.distinct(Ownership.character_id)),
                func.coalesce(func.sum(Ownership.count), 0),
            )
            .select_from(Ownership)
            .join(Character, Character.id == Ownership.character_id)
            .where(Ownership.user_id == user_id, Ownership.count > 0)
            .group_by(Character.rarity_id)
            .order_by(Character.rarity_id.desc())
        )
    ).all()
    return {int(r): int(u) for r, u, _t in rows}


async def favourite(session: AsyncSession, user_id: int) -> Owned | None:
    row = (
        await session.execute(
            select(Ownership, Character)
            .join(Character, Character.id == Ownership.character_id)
            .where(Ownership.user_id == user_id, Ownership.is_favorite.is_(True))
            .limit(1)
        )
    ).first()
    return _own(row[0], row[1]) if row else None


async def best(session: AsyncSession, user_id: int) -> Owned | None:
    row = (
        await session.execute(
            select(Ownership, Character)
            .join(Character, Character.id == Ownership.character_id)
            .where(Ownership.user_id == user_id, Ownership.count > 0)
            .order_by(Character.rarity_id.desc(), Character.price.desc(), Ownership.count.desc())
            .limit(1)
        )
    ).first()
    return _own(row[0], row[1]) if row else None


async def rarest_owned(
    session: AsyncSession, user_id: int, *, min_rarity_id: int = 1
) -> list[Owned]:
    rows = (
        await session.execute(
            select(Ownership, Character)
            .join(Character, Character.id == Ownership.character_id)
            .where(
                Ownership.user_id == user_id,
                Ownership.count > 0,
                Character.rarity_id >= int(min_rarity_id),
            )
            .order_by(Character.rarity_id.desc(), Character.price.desc())
            .limit(6)
        )
    ).all()
    return [_own(o, c) for o, c in rows]


async def sellable(
    session: AsyncSession, user_id: int, *, rarity_id: int | None = None, limit: int = 40
) -> list[Owned]:
    conds = [Ownership.user_id == user_id, Ownership.count > 0, Ownership.is_locked.is_(False)]
    if rarity_id is not None:
        conds.append(Character.rarity_id == int(rarity_id))
    rows = (
        await session.execute(
            select(Ownership, Character)
            .join(Character, Character.id == Ownership.character_id)
            .where(*conds)
            .order_by(Character.price.desc(), Ownership.count.desc())
            .limit(limit)
        )
    ).all()
    return [_own(o, c) for o, c in rows]


async def locked_ids(session: AsyncSession, user_id: int) -> set[int]:
    rows = (
        await session.execute(
            select(Ownership.character_id).where(
                Ownership.user_id == user_id, Ownership.is_locked.is_(True)
            )
        )
    ).scalars()
    return {int(r) for r in rows}


async def collection_value(session: AsyncSession, user_id: int) -> int:
    value = (
        await session.execute(
            select(func.coalesce(func.sum(Character.price * Ownership.count), 0))
            .select_from(Ownership)
            .join(Character, Character.id == Ownership.character_id)
            .where(Ownership.user_id == user_id)
        )
    ).scalar_one()
    return int(value or 0)


async def top_collectors(session: AsyncSession, *, limit: int = 15) -> list[tuple[User, int]]:
    rows = (
        await session.execute(
            select(User, func.count(func.distinct(Ownership.character_id)).label("unique"))
            .join(Ownership, Ownership.user_id == User.id)
            .where(Ownership.count > 0, User.banned.is_(False))
            .group_by(User.id)
            .order_by(func.count(func.distinct(Ownership.character_id)).desc())
            .limit(limit)
        )
    ).all()
    return [(r[0], int(r[1])) for r in rows]


async def holders_of(
    session: AsyncSession, character_id: int, *, limit: int = 50
) -> list[tuple[User, int]]:
    rows = (
        await session.execute(
            select(User, Ownership.count)
            .join(Ownership, Ownership.user_id == User.id)
            .where(Ownership.character_id == character_id, Ownership.count > 0)
            .order_by(Ownership.count.desc())
            .limit(limit)
        )
    ).all()
    return [(r[0], int(r[1])) for r in rows]


async def sweep_zero_rows(session: AsyncSession) -> int:
    result = await session.execute(Ownership.__table__.delete().where(Ownership.count <= 0))
    return int(result.rowcount or 0)
