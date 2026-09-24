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
    """Load the shipped catalogue on a fresh database (the default install policy).

    A bot with an empty roster is a bot with nothing to summon: the reference
    deployment's players gacha'd against a full roster, so first boot loads the
    shipped 177 characters (``waifu/data/characters.seed.json``) together with
    the tier ladder. The ladder and prices in ``0002`` stay mandatory
    configuration; the catalogue is the one content step — and it can be
    switched off (``SEED_CATALOGUE=0``) for a roster built with ``/upload`` or
    ``waifu import-legacy``. ``ensure_characters`` dedupes on name+series, so
    re-running is always safe.
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


async def _inventory_expiry(conn: AsyncConnection) -> None:
    """``user_inventory.expires_at`` — the item TTL the reference always had.

    Same inspector dance as :func:`_file_id_columns`: SQLite has no ``IF NOT EXISTS`` for
    columns, and the step must be a no-op on a greenfield install that already built it from the
    ORM metadata.
    """
    from sqlalchemy import inspect as sa_inspect

    def _existing(sync_conn: object) -> set[str]:
        return {row["name"] for row in sa_inspect(sync_conn).get_columns("user_inventory")}

    if "expires_at" not in await conn.run_sync(_existing):
        await conn.execute(text("ALTER TABLE user_inventory ADD COLUMN expires_at TIMESTAMP"))
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_inventory_expiry "
            "ON user_inventory (user_id, item_id, expires_at)"
        )
    )


async def _create_table_portable(conn: AsyncConnection, sqlite_ddl: str, pg_ddl: str) -> None:
    """CREATE TABLE for both dialects in one step.

    Greenfield installs already built the table from the ORM metadata in
    ``0001`` (IF NOT EXISTS makes this a no-op); existing installs need the
    DDL, and SERIAL/BIGSERIAL vs INTEGER PRIMARY KEY is the only difference.
    """
    ddl = sqlite_ddl if conn.dialect.name.startswith("sqlite") else pg_ddl
    await conn.execute(text(ddl))


async def _scheduled_broadcasts(conn: AsyncConnection) -> None:
    """``/broadcast at 20:00 …`` — announcements fired by the jobs loop."""
    await _create_table_portable(
        conn,
        (
            "CREATE TABLE IF NOT EXISTS scheduled_broadcasts ("
            " id INTEGER PRIMARY KEY,"
            " run_at TIMESTAMP NOT NULL,"
            " text TEXT NOT NULL,"
            " created_by BIGINT NOT NULL DEFAULT 0,"
            " created_at TIMESTAMP,"
            " sent_at TIMESTAMP"
            ")"
        ),
        (
            "CREATE TABLE IF NOT EXISTS scheduled_broadcasts ("
            " id BIGSERIAL PRIMARY KEY,"
            " run_at TIMESTAMP NOT NULL,"
            " text TEXT NOT NULL,"
            " created_by BIGINT NOT NULL DEFAULT 0,"
            " created_at TIMESTAMP,"
            " sent_at TIMESTAMP"
            ")"
        ),
    )
    await conn.execute(
        text("CREATE INDEX IF NOT EXISTS ix_broadcasts_run ON scheduled_broadcasts (run_at)")
    )


async def _character_requests(conn: AsyncConnection) -> None:
    """``/request <Name> <Series>`` — the polite door to the admin-curated roster."""
    await _create_table_portable(
        conn,
        (
            "CREATE TABLE IF NOT EXISTS character_requests ("
            " id INTEGER PRIMARY KEY,"
            " name VARCHAR(96) NOT NULL,"
            " series VARCHAR(96) NOT NULL DEFAULT '',"
            " requester_id BIGINT NOT NULL,"
            " status VARCHAR(12) NOT NULL DEFAULT 'pending',"
            " note VARCHAR(200) NOT NULL DEFAULT '',"
            " created_at TIMESTAMP,"
            " decided_at TIMESTAMP,"
            " decided_by BIGINT"
            ")"
        ),
        (
            "CREATE TABLE IF NOT EXISTS character_requests ("
            " id BIGSERIAL PRIMARY KEY,"
            " name VARCHAR(96) NOT NULL,"
            " series VARCHAR(96) NOT NULL DEFAULT '',"
            " requester_id BIGINT NOT NULL,"
            " status VARCHAR(12) NOT NULL DEFAULT 'pending',"
            " note VARCHAR(200) NOT NULL DEFAULT '',"
            " created_at TIMESTAMP,"
            " decided_at TIMESTAMP,"
            " decided_by BIGINT"
            ")"
        ),
    )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_requests_status "
            "ON character_requests (status, created_at)"
        )
    )
    await conn.execute(
        text("CREATE INDEX IF NOT EXISTS ix_requests_name ON character_requests (name, series)")
    )


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
    Migration(
        "2026_09_13_0006_inventory_expiry",
        "user_inventory.expires_at (shop item TTL)",
        _inventory_expiry,
    ),
    Migration(
        "2026_09_23_0007_scheduled_broadcasts",
        "scheduled_broadcasts (``/broadcast at …``)",
        _scheduled_broadcasts,
    ),
    Migration(
        "2026_09_23_0008_character_requests",
        "character_requests (``/request <Name> <Series>``)",
        _character_requests,
    ),
)


def names() -> list[str]:
    return [m.name for m in MIGRATIONS]
