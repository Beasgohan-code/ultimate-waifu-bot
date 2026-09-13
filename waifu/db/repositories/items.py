"""Shop items, shields, cooldowns and the heist log (/market, /steal, /bomb, /skip).

Summon-bot modelled a shield as a row with ``uses_remaining`` and decremented it
with read-modify-write; two simultaneous /steal calls could burn one shield. Here
``Shield`` is one row per charge (consumed with ``UPDATE … WHERE is_used IS FALSE``),
which is correct by construction and also gives an exact audit trail.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from waifu.db.models import Cooldown, HeistLog, InventoryItem, Shield
from waifu.errors import Locked, NotFound
from waifu.utils.time import now_utc


def live():
    """SQLAlchemy clause: a stack that has not rotted yet.

    ``plugins/market.py`` appended ``AND expires_at > datetime('now')`` to *every* inventory query,
    which is the whole anti-hoarding rule: buy 5 bombs for tonight's raid or lose them. Rows with
    no expiry (an admin grant, or anything predating the migration) stay live forever, so the
    column being nullable is a deliberate door rather than an oversight.
    """
    return or_(InventoryItem.expires_at.is_(None), InventoryItem.expires_at > now_utc())


@dataclass(frozen=True, slots=True)
class ItemDef:
    """Catalogue entry for the /market (a.k.a. /cshop000000) item shop."""

    key: str
    name: str
    cost: int
    max_stack: int
    desc: str
    charges: int = 1

    @property
    def emoji(self) -> str:
        return self.name.split(" ", 1)[0]

    @property
    def label(self) -> str:
        return self.name.split(" ", 1)[1] if " " in self.name else self.name


ITEMS: dict[str, ItemDef] = {
    "bomb": ItemDef("bomb", "💣 Bomb", 100000, 1, "Steal a random character from someone's harem"),
    "lucky": ItemDef(
        "lucky", "🎟️ Lucky Ticket", 15000, 5, "+15% rarity chance for your next 5 claims", charges=5
    ),
    "skip": ItemDef("skip", "⏰ Skip Cooldown", 25000, 10, "Clear one cooldown or arm a shield"),
    "magnet": ItemDef(
        "magnet", "💰 Coin Magnet", 10000, 5, "+2,000 bonus coins on your next /daily", charges=1
    ),
    "sshield": ItemDef(
        "sshield", "🔒 Steal Shield", 8000, 5, "Blocks one incoming /steal", charges=1
    ),
    "bshield": ItemDef("bshield", "🛡️ Bomb Shield", 9000, 3, "Blocks one incoming /bomb", charges=1),
    "xp": ItemDef("xp", "⚡ XP Boost", 12000, 5, "2× EXP and coin rewards for 5 claims", charges=5),
}

SPECIAL_COOLDOWNS = {"bomb": 24 * 3600, "steal": 3600, "skip": 600}


def item(key: str) -> ItemDef:
    try:
        return ITEMS[key]
    except KeyError:
        raise NotFound(f"unknown item “{key}”") from None


# ------------------------------------------------------------------------ inventory
async def stacks(session: AsyncSession, user_id: int, item_id: str) -> int:
    return int(
        (
            await session.execute(
                select(func.coalesce(func.sum(InventoryItem.uses_remaining), 0)).where(
                    InventoryItem.user_id == user_id,
                    InventoryItem.item_id == item_id,
                    live(),
                )
            )
        ).scalar_one()
        or 0
    )


async def inventory(session: AsyncSession, user_id: int) -> dict[str, int]:
    rows = (
        await session.execute(
            select(
                InventoryItem.item_id,
                func.coalesce(func.sum(InventoryItem.uses_remaining), 0),
            )
            .where(InventoryItem.user_id == user_id, live())
            .group_by(InventoryItem.item_id)
        )
    ).all()
    return {str(r[0]): int(r[1]) for r in rows if int(r[1]) > 0}


async def expiry_hours(session: AsyncSession, user_id: int) -> dict[str, int]:
    """How long each stack lasts, in whole hours, for ``/inv``'s ``⌛ 7h left`` line."""
    rows = (
        await session.execute(
            select(
                InventoryItem.item_id,
                func.max(InventoryItem.expires_at),
            )
            .where(InventoryItem.user_id == user_id, InventoryItem.uses_remaining > 0, live())
            .group_by(InventoryItem.item_id)
        )
    ).all()
    now = now_utc()
    out: dict[str, int] = {}
    for key, until in rows:
        if until is None:
            continue
        seconds = (until - now).total_seconds()
        out[str(key)] = max(0, int(seconds // 3600) + (1 if seconds % 3600 else 0))
    return out


async def buy(
    session: AsyncSession, user_id: int, item_id: str, *, quantity: int = 1, ttl_hours: int = 0
) -> int:
    """Add uses to the player's inventory. Price/debit is the caller's job (economy.debit).

    Enforces the per-item stack cap so /market can't be used to hoard 400 bombs.
    """
    spec = item(item_id)
    owned = await stacks(session, user_id, item_id)
    allowed = max(0, spec.max_stack - owned)
    if allowed <= 0:
        raise Locked(
            f"you already have the maximum {allowed if allowed else spec.max_stack}× {spec.label}"
        )
    quantity = min(quantity, allowed)
    row = (
        await session.execute(
            select(InventoryItem)
            .where(InventoryItem.user_id == user_id, InventoryItem.item_id == item_id)
            .limit(1)
        )
    ).scalar_one_or_none()
    until = now_utc() + timedelta(hours=ttl_hours) if ttl_hours and ttl_hours > 0 else None
    if row is None:
        row = InventoryItem(
            user_id=user_id,
            item_id=item_id,
            uses_remaining=quantity * spec.charges,
            expires_at=until,
        )
        session.add(row)
    else:
        row.uses_remaining += quantity * spec.charges
        # A fresh purchase re-arms the clock (the reference inserted a new 24h row per buy; this
        # table stacks, so topping up *and* extending is what keeps the two behaviours equal).
        if until is not None and (row.expires_at is None or row.expires_at < until):
            row.expires_at = until
    await session.flush()
    return quantity


async def spend(session: AsyncSession, user_id: int, item_id: str, *, count: int = 1) -> int:
    """Burn N charges. Returns remaining; raises when the player has none."""
    rows = list(
        (
            await session.execute(
                select(InventoryItem)
                .where(
                    InventoryItem.user_id == user_id,
                    InventoryItem.item_id == item_id,
                    InventoryItem.uses_remaining > 0,
                    live(),
                )
                .order_by(InventoryItem.expires_at.asc().nulls_last())
                .with_for_update(skip_locked=True)
                .limit(20)
            )
        ).scalars()
    )
    if not rows:
        raise NotFound(f"you have no {item(item_id).label.lower()} — buy one in /market")
    left = count
    for row in rows:
        take = min(left, row.uses_remaining)
        row.uses_remaining -= take
        left -= take
        if left <= 0:
            break
    if left > 0:
        raise NotFound(f"you only had {count - left} charge(s) of {item(item_id).label.lower()}")
    await session.execute(
        InventoryItem.__table__.delete().where(
            InventoryItem.user_id == user_id,
            InventoryItem.item_id == item_id,
            InventoryItem.uses_remaining <= 0,
        )
    )
    await session.flush()
    return await stacks(session, user_id, item_id)


async def activate(session: AsyncSession, user_id: int, item_id: str) -> int:
    """Mark a stack as 'in effect' (Lucky/XP/Magnet consume user.*_charges counters)."""
    await spend(session, user_id, item_id)
    await session.execute(
        update(InventoryItem)
        .where(InventoryItem.user_id == user_id, InventoryItem.item_id == item_id)
        .values(activated_at=now_utc())
    )
    await session.flush()
    return await stacks(session, user_id, item_id)


# -------------------------------------------------------------------------- shields
async def add_shields(session: AsyncSession, user_id: int, kind: str, count: int = 1) -> int:
    if kind not in {"sshield", "bshield"}:
        raise ValueError("kind must be sshield|bshield")
    for _ in range(count):
        session.add(Shield(user_id=user_id, kind=kind))
    await session.flush()
    return await count_shields(session, user_id, kind)


async def count_shields(session: AsyncSession, user_id: int, kind: str) -> int:
    return int(
        (
            await session.execute(
                select(func.count(Shield.id)).where(
                    Shield.user_id == user_id, Shield.kind == kind, Shield.is_used.is_(False)
                )
            )
        ).scalar_one()
        or 0
    )


async def pop_shield(session: AsyncSession, user_id: int, kind: str) -> bool:
    """Consume one unused shield. ``is_used IS FALSE`` makes it a CAS."""
    result = await session.execute(
        update(Shield)
        .where(Shield.user_id == user_id, Shield.kind == kind, Shield.is_used.is_(False))
        .values(is_used=True, used_at=now_utc())
        .returning(Shield.id)
    )
    found = result.scalar_one_or_none()
    await session.flush()
    return found is not None


# ------------------------------------------------------------------------ cooldowns
async def set_cooldown(session: AsyncSession, user_id: int, command: str) -> None:
    stmt = select(Cooldown).where(Cooldown.user_id == user_id, Cooldown.command == command)
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        session.add(Cooldown(user_id=user_id, command=command, last_used=now_utc()))
    else:
        row.last_used = now_utc()
    await session.flush()


async def cooldown_left(session: AsyncSession, user_id: int, command: str, seconds: int) -> int:
    last = (
        await session.execute(
            select(Cooldown.last_used).where(
                Cooldown.user_id == user_id, Cooldown.command == command
            )
        )
    ).scalar_one_or_none()
    if last is None:
        return 0
    from waifu.utils.time import to_naive_utc

    elapsed = (now_utc() - to_naive_utc(last)).total_seconds()
    return max(0, int(seconds - elapsed))


async def clear_cooldown(session: AsyncSession, user_id: int, command: str | None = None) -> int:
    conds = [Cooldown.user_id == user_id]
    if command:
        conds.append(Cooldown.command == command)
    result = await session.execute(Cooldown.__table__.delete().where(and_(*conds)))
    return int(result.rowcount or 0)


# ------------------------------------------------------------------------ heist log
async def log_heist(
    session: AsyncSession,
    *,
    attacker_id: int,
    target_id: int,
    kind: str,
    outcome: str,
    amount: int = 0,
    character_id: int | None = None,
    note: str = "",
) -> None:
    session.add(
        HeistLog(
            attacker_id=attacker_id,
            target_id=target_id,
            kind=kind,
            outcome=outcome,
            amount=amount,
            character_id=character_id,
            note=note[:140],
            created_at=now_utc(),
        )
    )
    await session.flush()


async def heist_stats(session: AsyncSession, user_id: int) -> dict[str, int]:
    """Win/loss tallies for /pinfo and /hstats (attacker *and* victim side)."""
    attacking = (
        await session.execute(
            select(
                HeistLog.kind,
                HeistLog.outcome,
                func.count(HeistLog.id),
                func.coalesce(func.sum(HeistLog.amount), 0),
            )
            .where(HeistLog.attacker_id == user_id)
            .group_by(HeistLog.kind, HeistLog.outcome)
        )
    ).all()
    defending = (
        await session.execute(
            select(HeistLog.kind, HeistLog.outcome, func.count(HeistLog.id))
            .where(HeistLog.target_id == user_id)
            .group_by(HeistLog.kind, HeistLog.outcome)
        )
    ).all()
    out = {
        "steal_wins": 0,
        "steal_blocked": 0,
        "bomb_wins": 0,
        "bomb_blocked": 0,
        "looted": 0,
        "defended": 0,
    }
    for kind, outcome, count, amount in attacking:
        if outcome == "success":
            out[f"{kind}_wins"] += int(count)
            out["looted"] += int(amount or 0)
    for kind, outcome, count in defending:
        if outcome == "blocked":
            out["defended"] += int(count)
            out[f"{kind}_blocked"] += int(count)
    return out


async def recent_heists(session: AsyncSession, user_id: int, *, limit: int = 8) -> list[HeistLog]:
    return list(
        (
            await session.execute(
                select(HeistLog)
                .where(or_(HeistLog.attacker_id == user_id, HeistLog.target_id == user_id))
                .order_by(HeistLog.created_at.desc())
                .limit(limit)
            )
        ).scalars()
    )


async def protective_summary(session: AsyncSession) -> dict[str, int]:
    """Owner-facing: how much pain the item economy is causing (for balance tuning)."""
    row = (
        (
            await session.execute(
                select(
                    func.count(HeistLog.id).filter(HeistLog.outcome == "success").label("hits"),
                    func.count(HeistLog.id).filter(HeistLog.outcome == "blocked").label("blocks"),
                    func.coalesce(func.sum(HeistLog.amount), 0).label("moved"),
                )
            )
        )
        .mappings()
        .one()
    )
    return {
        "hits": int(row["hits"] or 0),
        "blocks": int(row["blocks"] or 0),
        "coins_moved": int(row["moved"] or 0),
    }


async def most_stolen_from(session: AsyncSession, *, limit: int = 10) -> list[tuple[int, int]]:
    rows = (
        await session.execute(
            select(HeistLog.target_id, func.sum(HeistLog.amount))
            .where(HeistLog.kind == "steal", HeistLog.outcome == "success")
            .group_by(HeistLog.target_id)
            .order_by(func.sum(HeistLog.amount).desc())
            .limit(limit)
        )
    ).all()
    return [(int(r[0]), int(r[1] or 0)) for r in rows]


async def purge_spent_items(session: AsyncSession) -> int:
    """Maintenance: spent stacks are recreated on demand, so drop them."""
    result = await session.execute(
        InventoryItem.__table__.delete().where(InventoryItem.uses_remaining <= 0)
    )
    return int(result.rowcount or 0)
