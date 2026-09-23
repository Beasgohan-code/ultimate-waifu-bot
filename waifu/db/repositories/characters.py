"""Character catalogue + the two admin-tunable odds tables.

Summon-bot kept prices in a Python dict in ``config.py`` and the drop chances in
a table, so the two could disagree (a rarity could be unbuyable or unfreeable).
Here both live in the DB, are cached in Redis, and are edited only via /chance,
/chancelist, /setclaim and /claimlist.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import case, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from waifu.db.models import (
    Character,
    CharacterRequest,
    ClaimChance,
    Ownership,
    RarityChance,
    ShopPool,
)
from waifu.enums import Rarity
from waifu.errors import NotFound
from waifu.utils.rng import system_random
from waifu.utils.text import strip_md


def to_display(rarity: Rarity | int) -> str:
    tier = rarity if isinstance(rarity, Rarity) else Rarity.from_value(rarity)
    return tier.badge  # e.g. "⭐ Legendary" — matches Summon-bot's display strings


async def get(session: AsyncSession, character_id: int) -> Character | None:
    return await session.get(Character, character_id)


async def get_many(session: AsyncSession, ids: list[int]) -> dict[int, Character]:
    if not ids:
        return {}
    rows = (await session.execute(select(Character).where(Character.id.in_(ids)))).scalars()
    return {c.id: c for c in rows}


async def find_one(session: AsyncSession, query: str, *, rarity_id: int | None = None) -> Character:
    """Resolve ``/check 123``, ``/check zero-two``, ``/check darling:zero``.

    Exact name wins, then exact prefix, then fuzzy; ties break toward rarer.
    """
    raw = strip_md(query).strip()
    if not raw:
        raise NotFound("give me a name or an id")

    conds = []
    if rarity_id is not None:
        conds.append(Character.rarity_id == rarity_id)

    if raw.isdigit():
        found = await session.get(Character, int(raw))
        if found and (rarity_id is None or found.rarity_id == rarity_id):
            return found

    if ":" in raw:
        series, _, name = raw.partition(":")
        stmt = select(Character).where(
            Character.anime.ilike(f"%{series.strip()}%"),
            Character.name.ilike(f"%{name.strip()}%"),
            *conds,
        )
    else:
        stmt = (
            select(Character)
            .where(
                or_(
                    func.lower(Character.name) == raw.lower(),
                    Character.name.ilike(f"{raw}%"),
                    Character.name.ilike(f"%{raw}%"),
                ),
                *conds,
            )
            .order_by(
                case((func.lower(Character.name) == raw.lower(), 0), else_=1),
                case((Character.name.ilike(f"{raw}%"), 0), else_=1),
                Character.rarity_id.desc(),
            )
        )
    found = (await session.execute(stmt.limit(1))).scalar_one_or_none()
    if found is None:
        raise NotFound(f"no character matches “{raw[:60]}”")
    return found


async def search(
    session: AsyncSession,
    query: str,
    *,
    rarity_id: int | None = None,
    anime: str | None = None,
    limit: int = 20,
    offset: int = 0,
    active_only: bool = True,
) -> tuple[list[Character], int]:
    conds = []
    if query:
        like = f"%{query}%"
        conds.append(
            or_(Character.name.ilike(like), Character.anime.ilike(like), Character.tags.ilike(like))
        )
    if rarity_id is not None:
        conds.append(Character.rarity_id == rarity_id)
    if anime:
        conds.append(Character.anime.ilike(f"%{anime}%"))
    if active_only:
        conds.append(Character.is_active.is_(True))

    where = tuple(conds)
    total = (
        await session.execute(
            select(func.count(Character.id)).where(*where)
            if where
            else select(func.count(Character.id))
        )
    ).scalar_one()
    stmt = (
        select(Character)
        .where(*where)
        .order_by(Character.rarity_id.desc(), Character.name.asc())
        .limit(limit)
        .offset(offset)
    )
    return list((await session.execute(stmt)).scalars()), int(total)


async def by_name(session: AsyncSession, name: str, anime: str = "") -> Character | None:
    conds = [func.lower(Character.name) == name.strip().lower()]
    if anime:
        conds.append(func.lower(Character.anime) == anime.strip().lower())
    return (await session.execute(select(Character).where(*conds).limit(1))).scalar_one_or_none()


# --------------------------------------------------------------------- odds tables
async def rarity_chances(
    session: AsyncSession, *, enabled_only: bool = True
) -> list[tuple[Rarity, float]]:
    """``[(Rarity.COMMON, 62.0), …]`` — percentages from /chance (Summon-bot parity)."""
    stmt = select(RarityChance.rarity_id, RarityChance.chance)
    if enabled_only:
        stmt = stmt.where(RarityChance.is_enabled.is_(True))
    rows = (await session.execute(stmt.order_by(RarityChance.rarity_id))).all()
    out: list[tuple[Rarity, float]] = []
    for value, chance in rows:
        try:
            tier = Rarity(int(value))
        except ValueError:
            continue
        if float(chance) > 0:
            out.append((tier, float(chance)))
    return out


async def set_rarity_chance(session: AsyncSession, rarity: Rarity, chance: float) -> None:
    chance = max(0.0, min(100.0, float(chance)))
    values = {
        "rarity_id": int(rarity),
        "rarity_name": to_display(rarity),
        "chance": chance,
        "min_price": rarity.base_price,
        "is_enabled": chance > 0,
    }
    existing = (
        await session.execute(select(RarityChance).where(RarityChance.rarity_id == int(rarity)))
    ).scalar_one_or_none()
    if existing is None:
        session.add(RarityChance(**values))
    else:
        existing.chance = chance
        existing.rarity_name = values["rarity_name"]
        existing.is_enabled = values["is_enabled"]
    await session.flush()


async def claim_chances(
    session: AsyncSession, *, enabled_only: bool = True
) -> list[tuple[Rarity, float]]:
    stmt = select(ClaimChance.rarity_id, ClaimChance.chance)
    if enabled_only:
        stmt = stmt.where(ClaimChance.is_enabled.is_(True))
    rows = (await session.execute(stmt.order_by(ClaimChance.rarity_id))).all()
    out: list[tuple[Rarity, float]] = []
    for value, chance in rows:
        try:
            tier = Rarity(int(value))
        except ValueError:
            continue
        if float(chance) > 0:
            out.append((tier, float(chance)))
    return out


async def set_claim_chance(session: AsyncSession, rarity: Rarity, chance: float) -> None:
    chance = max(0.0, min(100.0, float(chance)))
    existing = (
        await session.execute(select(ClaimChance).where(ClaimChance.rarity_id == int(rarity)))
    ).scalar_one_or_none()
    if existing is None:
        session.add(
            ClaimChance(
                rarity_id=int(rarity),
                rarity_name=to_display(rarity),
                chance=chance,
                is_enabled=chance > 0,
            )
        )
    else:
        existing.chance = chance
        existing.rarity_name = to_display(rarity)
        existing.is_enabled = chance > 0
    await session.flush()


async def normalised_odds(
    session: AsyncSession, *, claim: bool = False
) -> list[tuple[Rarity, float]]:
    """Percentages renormalised to sum 100 (players must see honest odds)."""
    table = await (claim_chances(session) if claim else rarity_chances(session))
    if not table:
        total = sum(r.weight for r in Rarity)
        return [(r, r.weight / total * 100) for r in Rarity]
    total = sum(c for _r, c in table) or 1.0
    return [(r, c / total * 100.0) for r, c in table]


# ------------------------------------------------------------------------ pools
async def by_rarity(
    session: AsyncSession, rarity_id: int, *, active_only: bool = True, limit: int = 500
) -> list[Character]:
    conds = [Character.rarity_id == int(rarity_id)]
    if active_only:
        conds.append(Character.is_active.is_(True))
    return list(
        (
            await session.execute(
                select(Character).where(*conds).order_by(Character.name).limit(limit)
            )
        ).scalars()
    )


async def random_of_rarity(
    session: AsyncSession, rarity_id: int, *, banner_only: bool = False
) -> Character | None:
    """Random character inside a tier; rate-up (banner) chars are over-weighted."""

    async def _pick(with_banner_filter: bool) -> list[Character]:
        conds = [Character.is_active.is_(True), Character.rarity_id == int(rarity_id)]
        if with_banner_filter:
            conds.append(Character.banner_weight > 1.0)
        return list((await session.execute(select(Character).where(*conds).limit(800))).scalars())

    rows = await _pick(banner_only)
    if not rows and banner_only:
        rows = await _pick(False)
    if not rows:
        return None
    weights = [max(1.0, float(c.banner_weight or 1.0)) for c in rows]
    return system_random.choices(rows, weights=weights, k=1)[0]


async def random_any(session: AsyncSession) -> Character | None:
    """Uniform pick from the whole catalogue (manual /spawn parity with Summon-bot)."""
    row = (
        await session.execute(
            select(Character).where(Character.is_active.is_(True)).order_by(func.random()).limit(1)
        )
    ).scalar_one_or_none()
    return row


async def top_owned(session: AsyncSession, *, limit: int = 10) -> list[tuple[Character, int]]:
    rows = (
        await session.execute(
            select(Character, func.count(Ownership.user_id).label("holders"))
            .join(Ownership, Ownership.character_id == Character.id)
            .group_by(Character.id)
            .order_by(func.count(Ownership.user_id).desc())
            .limit(limit)
        )
    ).all()
    return [(r[0], int(r[1])) for r in rows]


async def rarity_distribution(session: AsyncSession) -> dict[int, int]:
    rows = (
        await session.execute(
            select(Character.rarity_id, func.count(Character.id))
            .where(Character.is_active.is_(True))
            .group_by(Character.rarity_id)
        )
    ).all()
    return {int(r): int(c) for r, c in rows}


# ------------------------------------------------------------------- admin mutate
async def next_free_id(session: AsyncSession) -> int:
    """The id a *new* row should take — the reference bot's ``new_upload_id`` rule.

    Summon-bot numbered its roster by hand (``01``, ``02``, ``03``…) and filled the lowest
    gap: those numbers are what admins quote in ``/delchar``, in captions, and in the log
    channel a database was rebuilt from, so a deleted ``07`` belongs to the next upload
    rather than being skipped forever. Autoincrement cannot do that, so ``/upload`` asks
    here and passes the answer to :func:`create_or_update`.
    """
    count = int(
        (await session.execute(select(func.count()).select_from(Character.__table__))).scalar() or 0
    )
    top = int((await session.execute(select(func.max(Character.id)))).scalar() or 0)
    if top == count:  # dense 1..n — the common case, no scan
        return top + 1
    used = {int(value) for value in (await session.execute(select(Character.id))).scalars()}
    candidate = 1
    while candidate in used:
        candidate += 1
    return candidate


async def create_or_update(
    session: AsyncSession,
    *,
    name: str,
    anime: str = "",
    rarity: Rarity = Rarity.COMMON,
    price: int | None = None,
    assign_id: int | None = None,
    image_url: str = "",
    photo_file_id: str = "",
    video_url: str = "",
    video_file_id: str = "",
    live_photo_file_id: str = "",
    description: str = "",
    voice_line: str = "",
    persona: str = "",
    tags: str = "",
    stat_power: int | None = None,
    banner_weight: float = 1.0,
) -> tuple[Character, bool]:
    """``/addchar`` and ``/updatechar`` share this; returns ``(character, created)``."""
    name, anime = strip_md(name)[:96], strip_md(anime)[:128]
    existing = await by_name(session, name, anime)
    created = existing is None
    char = existing or Character(
        name=name, anime=anime, rarity=to_display(rarity), rarity_id=int(rarity)
    )
    char.rarity = to_display(rarity)
    char.rarity_id = int(rarity)
    if price is not None:
        char.price = int(price)
    elif created:
        char.price = int(rarity.base_price)
    if stat_power is not None:
        char.stat_power = int(stat_power)
    elif created:
        char.stat_power = 10 * int(rarity)
    for field, value in (
        ("image_url", image_url),
        ("photo_file_id", photo_file_id),
        ("video_url", video_url),
        ("video_file_id", video_file_id),
        ("live_photo_file_id", live_photo_file_id),
        ("description", description),
        ("voice_line", voice_line),
        ("persona", persona),
        ("tags", tags[:255]),
    ):
        if value:
            setattr(char, field, value)
    char.banner_weight = banner_weight
    if created and assign_id is not None:
        # The admin chose this number (or the gap rule did) — the sequence must not drift
        # past it, or the next autoincrement insert would collide with a hand-numbered row.
        char.id = int(assign_id)
    if created:
        session.add(char)
    await session.flush()
    return char, created


async def attach_media(
    session: AsyncSession,
    character_id: int,
    *,
    photo_file_id: str = "",
    video_file_id: str = "",
    live_photo_file_id: str = "",
    sticker_file_id: str = "",
    image_url: str = "",
) -> None:
    values = {
        k: v
        for k, v in {
            "photo_file_id": photo_file_id,
            "video_file_id": video_file_id,
            "live_photo_file_id": live_photo_file_id,
            "sticker_file_id": sticker_file_id,
            "image_url": image_url,
        }.items()
        if v
    }
    if not values:
        return
    await session.execute(update(Character).where(Character.id == character_id).values(**values))
    await session.flush()


async def set_active(session: AsyncSession, character_id: int, active: bool) -> None:
    await session.execute(
        update(Character).where(Character.id == character_id).values(is_active=active)
    )


async def set_banner(session: AsyncSession, character_ids: list[int], weight: float = 3.0) -> int:
    await session.execute(
        update(Character).where(Character.banner_weight != 1.0).values(banner_weight=1.0)
    )
    if not character_ids:
        return 0
    result = await session.execute(
        update(Character).where(Character.id.in_(character_ids)).values(banner_weight=weight)
    )
    return int(result.rowcount or 0)


async def delete_character(session: AsyncSession, character_id: int) -> int:
    """Delete a character and drop it from every collection (admin-only tool).

    Ownership rows go with it via FK CASCADE, but they are removed explicitly so
    the statement also works on databases created before the FK existed.
    """
    await session.execute(delete(Ownership).where(Ownership.character_id == character_id))
    await session.execute(delete(ShopPool).where(ShopPool.user_id == 0))  # placeholder pools only
    result = await session.execute(delete(Character).where(Character.id == character_id))
    return int(result.rowcount or 0)


async def totals(session: AsyncSession) -> dict[str, int]:
    """Roster size, active count, distinct series and total owned copies.

    Two statements rather than one join: ``characters LEFT JOIN ownership`` multiplies
    the character rows by their copies, so ``count(*)`` would report "copies" as the
    roster size — and ``/chars`` would advertise a roster a hundred times bigger than it
    is. Aggregating each side separately is also index-only on Postgres.
    """
    row = (
        (
            await session.execute(
                select(
                    func.count(Character.id).label("total"),
                    func.coalesce(
                        func.sum(case((Character.is_active.is_(True), 1), else_=0)), 0
                    ).label("active"),
                    func.count(func.distinct(Character.anime)).label("series"),
                )
            )
        )
        .mappings()
        .one()
    )
    copies = (
        await session.execute(select(func.coalesce(func.sum(Ownership.count), 0)))
    ).scalar_one()
    return {
        "characters": int(row["total"]),
        "active": int(row["active"]),
        "series": int(row["series"] or 0),
        "copies": int(copies or 0),
    }


async def price_for(session: AsyncSession, char: Character) -> int:
    """Effective shop price; falls back to the rarity's base price if unset."""
    if char.price:
        return int(char.price)
    return int(Rarity.from_value(char.rarity_id).base_price)


async def save_pool(session: AsyncSession, user_id: int, rarity: str, ids: list[int]) -> None:
    row = (
        await session.execute(
            select(ShopPool).where(ShopPool.user_id == user_id, ShopPool.rarity == rarity)
        )
    ).scalar_one_or_none()
    if row is None:
        session.add(
            ShopPool(user_id=user_id, rarity=rarity, characters={"ids": ids}, refreshes_used=1)
        )
    else:
        row.characters = {"ids": ids}
        row.refreshes_used += 1
    await session.flush()


async def load_pool(session: AsyncSession, user_id: int, rarity: str) -> list[int] | None:
    row = (
        await session.execute(
            select(ShopPool).where(ShopPool.user_id == user_id, ShopPool.rarity == rarity)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    return list((row.characters or {}).get("ids", []))


# ------------------------------------------------------------- player requests
async def pending_requests(session: AsyncSession, *, limit: int = 30) -> list[Any]:
    rows = (
        (
            await session.execute(
                select(CharacterRequest)
                .where(CharacterRequest.status == "pending")
                .order_by(CharacterRequest.created_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def find_pending_request(session: AsyncSession, name: str, series: str) -> Any | None:
    """A pending request for the same character (case-insensitive) — one ask, not ten."""
    norm = name.strip().casefold()
    if not norm:
        return None
    rows = (
        (
            await session.execute(
                select(CharacterRequest).where(CharacterRequest.status == "pending")
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        if (
            row.name.strip().casefold() == norm
            and (row.series or "").casefold() == (series or "").casefold()
        ):
            return row
    return None


async def submit_request(
    session: AsyncSession, *, name: str, series: str, requester_id: int, note: str = ""
) -> Any:
    row = CharacterRequest(
        name=name[:96],
        series=series[:96],
        requester_id=requester_id,
        note=note[:200],
    )
    session.add(row)
    await session.flush()
    return row


async def decide_request(
    session: AsyncSession, request_id: int, *, decision: str, decided_by: int
) -> Any:
    """Approve / decline one request. ``decision`` must be approved|declined."""
    from waifu.utils.time import now_utc

    row = await session.get(CharacterRequest, request_id)
    if row is None or row.status != "pending":
        raise NotFound("that request is already decided (or gone)")
    row.status = decision
    row.decided_at = now_utc()
    row.decided_by = decided_by
    await session.flush()
    return row
