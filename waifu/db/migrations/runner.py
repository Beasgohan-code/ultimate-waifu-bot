"""Migration runner: apply pending steps, print status, refuse to half-apply.

Each migration runs inside its own transaction, so a failure leaves the database
at the last successful version instead of mid-flight.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from waifu.db.migrations import MIGRATIONS, Migration
from waifu.db.models import SchemaVersion
from waifu.logging import get_logger
from waifu.utils.time import now_utc

log = get_logger("db.migrations")


@dataclass(slots=True)
class Plan:
    pending: list[Migration]
    applied: list[str]

    @property
    def is_current(self) -> bool:
        return not self.pending


async def _applied_names(engine: AsyncEngine) -> list[str]:
    async with engine.connect() as conn:
        exists = (
            (
                await conn.execute(text("SELECT to_regclass('schema_version') IS NOT NULL"))
            ).scalar_one()
            if not engine.dialect.name.startswith("sqlite")
            else (
                await conn.execute(
                    text("SELECT COUNT(*) FROM sqlite_master WHERE name='schema_version'")
                )
            ).scalar_one()
        )
        if not exists:
            return []
        rows = (await conn.execute(select(SchemaVersion.name))).scalars()
        return list(rows)


async def plan(engine: AsyncEngine) -> Plan:
    applied = await _applied_names(engine)
    return Plan(pending=[m for m in MIGRATIONS if m.name not in applied], applied=applied)


async def apply(engine: AsyncEngine, *, force: bool = False) -> list[str]:
    from waifu.db.models import Base

    # Greenfield: make sure tables exist before the version bookkeeping runs.
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    current = await plan(engine)
    done: list[str] = []
    for migration in current.pending:
        log.info("applying %s — %s", migration.name, migration.doc)
        async with engine.begin() as conn:
            await migration.step(conn)
            # ``applied_at`` is bound rather than ``now()``: the same command has to work
            # against SQLite in development (WAIFU_TEST_MODE), and SQLite has no now().
            await conn.execute(
                text(
                    "INSERT INTO schema_version (name, applied_at) VALUES (:n, :at) ON CONFLICT DO NOTHING"
                ),
                {"n": migration.name, "at": now_utc()},
            )
        done.append(migration.name)
    if not done and force:
        log.info("nothing to apply")
    return done


async def describe(engine: AsyncEngine) -> str:
    current = await plan(engine)
    lines = [
        f"{'✓' if not current.pending else '·'} migrations: {len(current.applied)} applied, {len(current.pending)} pending"
    ]
    for name in current.applied:
        lines.append(f"  ✓ {name}")
    for migration in current.pending:
        lines.append(f"  → {migration.name}: {migration.doc}")
    return "\n".join(lines)
