"""Backup / restore — the "no data is ever lost" guarantee, proved end to end.

The whole database (every table, in foreign-key order) must survive a trip
through a single JSON file with every row and column intact; a restore must
be all-or-nothing (a bad file leaves the database exactly as it was);
retention prunes the oldest files; and the daily jobs pass, the CLI and the
owner command all produce the same snapshot.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import delete, func, select, update

from waifu.db import BackupError, Database, prune_backups
from waifu.db.models import Base, Character, Group, ScheduledBroadcast, User
from waifu.db.repo import economy as ledger
from waifu.db.repo import stats as stats_repo
from waifu.db.repo import users as user_repo
from waifu.utils.time import now_utc

#: Two test players so the ledger has FK children to survive the round trip.
ALICE, BOB = 70001, 70002


# ------------------------------------------------------------------ helpers
async def _seed_lively(db: Database) -> None:
    """Real rows in several tables: users, money, a group, a broadcast, kv state."""
    async with db.tx() as session:
        await user_repo.upsert(session, ALICE, username="alice", first_name="Alice")
        await user_repo.upsert(session, BOB, username="bob", first_name="Bob")
        await ledger.credit(session, ALICE, 123_456, "test", reference="backup-test")
        session.add(Group(chat_id=-100_999, title="Backup crew", spawn_enabled=True))
        session.add(
            ScheduledBroadcast(
                run_at=now_utc() + timedelta(days=1), text="hello from a test", created_by=ALICE
            )
        )
    async with db.tx() as session:
        await stats_repo.kv_set(session, "kv_for_backup", {"answer": 42})


async def _dump_all(db: Database) -> dict[str, list[dict[str, Any]]]:
    """An independent copy of every table, for before/after comparison."""
    out: dict[str, list[dict[str, Any]]] = {}
    async with db.engine.connect() as conn:
        for table in Base.metadata.sorted_tables:
            result = await conn.execute(select(table))
            rows = [
                {
                    key: (value.isoformat() if isinstance(value, datetime) else value)
                    for key, value in row._mapping.items()
                }
                for row in result
            ]
            rows.sort(key=lambda row: tuple(str(value) for value in row.values()))
            out[table.name] = rows
    return out


# ------------------------------------------------------------ the round trip
async def test_backup_restore_roundtrip_is_lossless(db: Database, tmp_path) -> None:
    """The core promise: every table, every row, every column — file and back."""
    await _seed_lively(db)
    before = await _dump_all(db)
    path, counts = await db.backup(tmp_path)
    assert path.exists() and path.name.startswith("waifu-backup-") and path.name.endswith(".json")
    assert counts == {name: len(rows) for name, rows in before.items()}
    total = sum(counts.values())

    # wreck everything a careless deploy could wreck
    async with db.tx() as session:
        await session.execute(update(Character).values(price=1))
        await session.execute(update(User).values(balance=0))
        await session.execute(delete(Group))
        await stats_repo.kv_set(session, "kv_for_backup", {"answer": 43})

    result = await db.restore(path)
    assert result["rows"] == total
    after = await _dump_all(db)
    assert after == before  # no table, no row, no column differs


async def test_backup_of_an_empty_db_round_trips(db: Database, tmp_path) -> None:
    path, counts = await db.backup(tmp_path)
    result = await db.restore(path)
    assert result["rows"] == sum(counts.values())
    assert await db.query(select(func.count()).select_from(Character))


# ------------------------------------------------------- all-or-nothing restore
async def test_restore_refuses_garbage_and_keeps_data(db: Database, tmp_path) -> None:
    await _seed_lively(db)
    before = await _dump_all(db)
    for name, body in (("garbage.json", "{not json"), ("plain.json", '{"hello": 1}')):
        bad = tmp_path / name
        bad.write_text(body, encoding="utf-8")
        with pytest.raises(BackupError):
            await db.restore(bad)
    assert await _dump_all(db) == before  # the failed attempts changed nothing


async def test_restore_refuses_a_backup_from_a_different_version(db: Database, tmp_path) -> None:
    """A backup missing (or holding) a table is from another version of the bot:
    restoring it would silently drop data, so it is refused outright."""
    await _seed_lively(db)
    before = await _dump_all(db)
    path, _ = await db.backup(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    removed = Group.__table__.name
    del payload["tables"][removed]
    del payload["meta"]["tables"][removed]
    drifted = tmp_path / f"{removed}-gone.json"
    drifted.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(BackupError, match="schema mismatch"):
        await db.restore(drifted)
    assert await _dump_all(db) == before


async def test_restore_refuses_column_drift(db: Database, tmp_path) -> None:
    await _seed_lively(db)  # the users table must hold rows for drift to exist
    path, _ = await db.backup(tmp_path)
    before = await _dump_all(db)
    payload = json.loads(path.read_text(encoding="utf-8"))
    name = User.__table__.name
    for row in payload["tables"][name]:
        del row["balance"]
    drifted = tmp_path / "no-balance.json"
    drifted.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(BackupError, match="column drift"):
        await db.restore(drifted)
    assert await _dump_all(db) == before


# ----------------------------------------------------------------- retention
def test_prune_backups_keeps_only_the_newest(tmp_path) -> None:
    dest = tmp_path / "backups"
    dest.mkdir()
    for i in range(13):
        (dest / f"waifu-backup-202609{i:02d}-120000Z.json").write_text("{}", encoding="utf-8")
    other = dest / "not-a-backup.txt"
    other.write_text("keep me", encoding="utf-8")

    removed = prune_backups(dest, keep=10)
    assert removed == 3
    remaining = sorted(p.name for p in dest.glob("waifu-backup-*.json"))
    assert len(remaining) == 10
    assert "waifu-backup-20260900-120000Z.json" not in remaining
    assert other.exists()  # only waifu-backup-*.json is ever pruned
    assert prune_backups(dest, keep=10) == 0  # idempotent


# ------------------------------------------------------------- the daily pass
async def test_backup_pass_snapshots_once_a_day(ctx, tx, tmp_path, player) -> None:
    """The jobs pass: one backup, then a stamp so a more frequent cron (or a
    second ``waifu jobs --name all``) cannot double it within 20 hours."""
    from waifu.core.jobs import run_pass

    ctx.settings = ctx.settings.model_copy(update={"backup_dir": tmp_path / "backups"})
    results = await run_pass(ctx, "backup")
    assert len(results) == 1 and results[0].error == ""
    assert results[0].counters.get("rows", 0) > 0
    files = list((tmp_path / "backups").glob("waifu-backup-*.json"))
    assert len(files) == 1

    async with ctx.db.tx() as session:
        stamp = await stats_repo.kv_get(session, "last_backup")
    assert stamp is not None and stamp.get("file") == files[0].name

    results2 = await run_pass(ctx, "backup")
    assert results2[0].counters == {}  # deduped — no second file
    assert len(list((tmp_path / "backups").glob("waifu-backup-*.json"))) == 1

    # the stamp is what a stale cron obeys: age it out and the pass runs again
    async with ctx.db.tx() as session:
        await stats_repo.kv_set(session, "last_backup", {"at": time.time() - 21 * 3600})
    results3 = await run_pass(ctx, "backup")
    assert results3[0].counters.get("rows", 0) > 0
    assert len(list((tmp_path / "backups").glob("waifu-backup-*.json"))) == 2


# ------------------------------------------------------------------- /backup
class _FakeChat:
    id = -100_123


class _FakeMessage:
    def __init__(self) -> None:
        self.chat = _FakeChat()
        self.message_id = 1
        self.message_thread_id = None
        self.answers: list[str] = []

    async def answer(self, text: str, *args: Any, **kwargs: Any) -> None:
        self.answers.append(text)


async def test_backup_command_is_owner_only_and_writes_a_file(ctx, tx, tmp_path) -> None:
    from tests.test_owner_log import Recorder
    from waifu.core.access import Access
    from waifu.enums import Role
    from waifu.plugins.sudo import backup_db

    ctx.settings = ctx.settings.model_copy(update={"backup_dir": tmp_path / "backups"})
    bot = Recorder()
    ctx.bot = bot

    message = _FakeMessage()
    await backup_db(message, ctx=ctx, access=Access(user_id=1, role=Role.OWNER))
    files = list((tmp_path / "backups").glob("waifu-backup-*.json"))
    assert len(files) == 1
    sent = [kwargs.get("text", "") for kwargs in bot.by("send_message")]
    assert any("backup written" in body for body in sent)
    assert any("waifu restore" in body for body in sent)

    await backup_db(message, ctx=ctx, access=Access(user_id=4242, role=Role.GUEST))
    assert len(list((tmp_path / "backups").glob("waifu-backup-*.json"))) == 1  # no second file
    assert any("owner" in answer for answer in message.answers)  # refuse() answers in-chat


def test_first_open_creates_the_missing_parent(tmp_path, monkeypatch) -> None:
    """The very first open of a one-file database must work even when the
    directory does not exist yet — "one file, zero setup" has to hold on the
    first command (a missing parent used to be an opaque 'unable to open
    database file')."""
    from sqlalchemy import text

    from waifu.db import Database

    monkeypatch.chdir(tmp_path)
    db = Database("sqlite+aiosqlite:///nested/dir/waifu.db")
    assert db.sqlite_file == tmp_path / "nested" / "dir" / "waifu.db"
    assert db.sqlite_file.parent.is_dir()  # the constructor made it

    async def _ping() -> None:
        async with db.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))

    asyncio.run(_ping())
    assert db.sqlite_file.exists()  # the entire database is now this one file
    asyncio.run(db.dispose())


def test_from_settings_ignores_the_process_cwd(tmp_path, monkeypatch) -> None:
    """The default relative DATABASE_URL must not follow the process cwd:
    `waifu migrate` run from anywhere writes the one file to the project's
    data/ directory (the settings.sqlite_path rule), not to wherever the
    shell happens to sit — a cwd-following engine made the deploy die with
    'unable to open database file'."""
    from waifu.db import Database

    project = tmp_path / "project"
    monkeypatch.chdir(tmp_path)  # the process runs somewhere else entirely

    class _Cfg:
        """The fields from_settings reads (the URL contract under test)."""

        database_url = "sqlite+aiosqlite:///data/waifu.db"
        db_echo = False
        db_pool_size = 1
        db_max_overflow = 1
        db_statement_timeout_ms = 1
        sqlite_path = project / "data" / "waifu.db"

    db = Database.from_settings(_Cfg)
    assert db.sqlite_file == project / "data" / "waifu.db"
    assert db.sqlite_file.parent.is_dir()  # created next to the project, not the cwd
    assert not (tmp_path / "data").exists()
    asyncio.run(db.dispose())


# ----------------------------------------------------------------- the CLI
@pytest.fixture
def one_file_deploy(tmp_path, monkeypatch):
    """The fastest possible deploy: one file database, no Postgres, no Redis."""
    from waifu import settings as settings_mod

    db_file = tmp_path / "waifu.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("BOT_TOKEN", "123456:cli-test-token")
    monkeypatch.delenv("REDIS_URL", raising=False)
    settings_mod.reload_settings()
    yield db_file, tmp_path
    settings_mod.reload_settings()


def test_fresh_install_is_one_file(one_file_deploy) -> None:
    """`waifu migrate` on a fresh checkout produces exactly one database file,
    and `waifu doctor` can read it back — the zero-infrastructure startup."""
    from waifu.__main__ import main

    db_file, _backup_root = one_file_deploy
    assert main(["migrate"]) == 0
    assert db_file.exists()  # the entire database is this one file
    assert main(["doctor"]) == 0


def test_cli_backup_then_restore_is_lossless(one_file_deploy) -> None:
    from waifu.__main__ import main

    _db_file, tmp_path = one_file_deploy
    assert main(["migrate"]) == 0
    assert main(["backup"]) == 0
    files = list((tmp_path / "backups").glob("waifu-backup-*.json"))
    assert len(files) == 1

    # preview: reports what would be restored, writes nothing
    assert main(["restore", str(files[0])]) == 0

    async def tier_count() -> int:
        from waifu.db import Database
        from waifu.db.models import RarityChance

        async with Database.from_settings() as db:
            count = await db.query(select(func.count()).select_from(RarityChance))
            return int(count[0]) if count else 0

    # wreck the pull ladders (18 tiers is a complete set)
    async def wreck() -> None:
        from waifu.db import Database
        from waifu.db.models import RarityChance

        async with Database.from_settings() as db, db.tx() as session:
            await session.execute(delete(RarityChance))

    asyncio.run(wreck())
    assert asyncio.run(tier_count()) == 0

    assert main(["restore", str(files[0]), "--yes"]) == 0
    assert asyncio.run(tier_count()) == 18  # db.query() confirms the rows are back


def test_cli_restore_rejects_a_non_backup(one_file_deploy, tmp_path) -> None:
    from waifu.__main__ import main

    _, root = one_file_deploy
    assert main(["migrate"]) == 0
    stray = root / "stray.json"
    stray.write_text('{"meta": {"app": "something-else"}}', encoding="utf-8")
    assert main(["restore", str(stray)]) == 1
    missing = root / "nope.json"
    assert main(["restore", str(missing)]) == 1
