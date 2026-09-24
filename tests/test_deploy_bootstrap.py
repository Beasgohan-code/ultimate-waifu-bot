"""The deploy must boot into a working database by itself.

The Render deploys of 2026-09-24 started cleanly (settings, plugins, API)
and then failed *every* query — jobs, /start, /help — with
``OperationalError``: the platform never runs ``waifu migrate``, so the
process booted into an empty SQLite file and the first query died with
"no such table". ``build_app`` now applies the pending schema upgrades at
startup (idempotent, re-run safe); this test pins that a fresh deploy needs
no deploy step at all.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from waifu.core.app import build_app
from waifu.settings import Settings


@pytest.fixture()
def fresh_deploy(tmp_path) -> Settings:
    """Settings exactly as a platform gives them: no migrate step, one fresh
    SQLite file, no Redis."""
    db_file = tmp_path / "waifu.db"
    return Settings(
        _env_file=None,
        bot_token="123:abc",
        owner_id=1,
        database_url=f"sqlite+aiosqlite:///{db_file}",
    )


async def test_fresh_deploy_boots_into_a_current_schema(fresh_deploy) -> None:
    app = await build_app(fresh_deploy, with_bot=False, negotiate=False, with_plugins=False)
    try:
        tables = set(
            await app.ctx.db.query(text("SELECT name FROM sqlite_master WHERE type='table'"))
        )
        assert {"users", "characters", "schema_version"} <= tables
        assert (await app.ctx.db.query(text("SELECT COUNT(*) FROM schema_version")))[0] > 0
    finally:
        await app.ctx.db.dispose()


async def test_reboot_is_a_noop_and_stays_current(fresh_deploy) -> None:
    """Second start of the same database (every Render deploy): nothing to
    apply, no error, schema unchanged."""
    first = await build_app(fresh_deploy, with_bot=False, negotiate=False, with_plugins=False)
    before = await first.ctx.db.query(text("SELECT name FROM sqlite_master WHERE type='table'"))
    await first.ctx.db.dispose()

    second = await build_app(fresh_deploy, with_bot=False, negotiate=False, with_plugins=False)
    try:
        assert sorted(
            await second.ctx.db.query(text("SELECT name FROM sqlite_master WHERE type='table'"))
        ) == sorted(before)
    finally:
        await second.ctx.db.dispose()


async def test_doctor_stays_read_only(fresh_deploy) -> None:
    """The doctor *reports* pending migrations; it must not apply them
    (a diagnostic that mutates the schema hides drift it should name)."""
    from waifu.db.migrations.runner import plan

    app = await build_app(
        fresh_deploy, with_bot=False, negotiate=False, with_plugins=False, migrate=False
    )
    try:
        assert (
            await app.ctx.db.query(text("SELECT COUNT(*) FROM sqlite_master WHERE type='table'"))
        )[0] == 0
        # and the pending list still knows what is missing
        assert len((await plan(app.ctx.db.engine)).pending) > 0
    finally:
        await app.ctx.db.dispose()
