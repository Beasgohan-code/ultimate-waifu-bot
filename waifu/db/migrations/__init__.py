"""Versioned migrations.

I deliberately did not add Alembic: this project's schema evolves with *data*
migrations (seed the rarity table, backfill file_ids, renumber pity counters) as
much as with DDL, and running both through one ordered list keeps a single
migration story. ``Base.metadata.create_all`` is the DDL for greenfield installs;
every later change is an explicit, idempotent step recorded in ``schema_version``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

Step = Callable[[AsyncConnection], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class Migration:
    name: str
    doc: str
    step: Step


async def _core_schema(conn: AsyncConnection) -> None:
    from waifu.db.models import Base

    await conn.run_sync(Base.metadata.create_all)


async def _odds_defaults(conn: AsyncConnection) -> None:
    """Rarity ladder + claim chances (Summon-bot kept these only in a Python dict)."""
    from waifu.db.seed import ensure_odds

    await ensure_odds(conn)


async def _personas(conn: AsyncConnection) -> None:
    """Load the optional catalogue — *only* when the operator asked for one.

    A fresh database deliberately ends up with **no characters**. The reference bot did
    the same: its shipped ``summon.db`` held exactly one row, because its roster was
    never source code — admins typed it in, media first, with ``/upload``. Art and names
    are content; the tier ladder and prices in ``0002`` are configuration, so those stay
    mandatory while the catalogue is opt-in (``SEED_CATALOGUE=1``, ``waifu seed
    --catalogue``, or ``waifu import-legacy`` for an existing deployment).
    """
    from waifu.settings import get_settings

    if not get_settings().seed_catalogue:
        return
    from waifu.db.seed import ensure_characters

    await ensure_characters(conn)


async def _file_id_columns(conn: AsyncConnection) -> None:
    """Idempotent ALTERs for installs created before media file_id caching.

    ``ADD COLUMN IF NOT EXISTS`` is Postgres-only syntax, and the whole point of testing
    against SQLite (``WAIFU_TEST_MODE``) is that the same commands run — so existence is
    checked through the inspector and the ALTER is issued only for what is missing.
    """
    from sqlalchemy import inspect as sa_inspect

    def _existing(sync_conn: object) -> set[str]:
        return {row["name"] for row in sa_inspect(sync_conn).get_columns("characters")}

    # An AsyncConnection cannot be inspected directly, so the read goes through run_sync.
    existing = await conn.run_sync(_existing)
    for column in ("photo_file_id", "video_file_id", "live_photo_file_id", "sticker_file_id"):
        if column in existing:
            continue
        await conn.execute(
            text(f"ALTER TABLE characters ADD COLUMN {column} VARCHAR(255) NOT NULL DEFAULT ''")
        )


async def _indexes(conn: AsyncConnection) -> None:
    """Hot-path indexes that ``create_all`` can't express (partial / expression)."""
    # ``USING gin`` + ``gin_trgm_ops`` are Postgres-only, and SQLite parses ``USING`` at
    # all — so the expression index is chosen by dialect instead of by trial and error.
    is_postgres = conn.dialect.name == "postgresql"
    statements = (
        # Active spawns per chat — the query every /summon hits.
        "CREATE INDEX IF NOT EXISTS ix_spawn_active_only ON spawn_events (chat_id, expires_at) WHERE status = 'active'",
        # Live auctions ordered by close time (the closer job's scan).
        "CREATE INDEX IF NOT EXISTS ix_auction_live_due ON auctions (ends_at) WHERE status = 'live'",
        # Open raffles by deadline.
        "CREATE INDEX IF NOT EXISTS ix_raffle_open_due ON raffle_rounds (ends_at) WHERE status = 'open'",
    )
    if is_postgres:
        # Trigram index for fuzzy /search (needs pg_trgm; skipped when unavailable).
        statements += (
            "CREATE INDEX IF NOT EXISTS ix_characters_name_trgm ON characters USING gin (name gin_trgm_ops)",
        )
    for statement in statements:
        try:
            await conn.execute(text(statement))
        except Exception as exc:  # pragma: no cover - managed PG without pg_trgm
            if "pg_trgm" in str(exc) or "operator class" in str(exc):
                await conn.rollback()
                continue
            raise


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        "2026_09_01_0001_core_schema", "create every table from the ORM metadata", _core_schema
    ),
    Migration(
        "2026_09_01_0002_odds_defaults",
        "seed rarity_chances + claim_list from the Rarity enum",
        _odds_defaults,
    ),
    # The name says "characters" because it once seeded a roster; it now loads one only if
    # the operator asked (SEED_CATALOGUE), and the name is frozen — schema_version rows on
    # live installs already record it, and a renamed migration is a re-run migration.
    Migration("2026_09_01_0003_characters", "optional catalogue load (opt-in)", _personas),
    Migration(
        "2026_09_05_0004_media_file_ids",
        "media file_id columns for cached Telegram handles",
        _file_id_columns,
    ),
    Migration(
        "2026_09_05_0005_hot_indexes",
        "partial + trigram indexes for spawn/auction/search",
        _indexes,
    ),
)


def names() -> list[str]:
    return [m.name for m in MIGRATIONS]
