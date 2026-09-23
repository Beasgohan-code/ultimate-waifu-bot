"""The one database.

Everything the bot remembers — players, coins, collections, characters,
auctions, streaks, log state — lives in exactly **one database**. By default
that is a single file (``data/waifu.db``, SQLite): no server to install, the
fastest possible startup, and one file is all there is to back up. Point
``DATABASE_URL`` at ``postgresql+asyncpg://…`` when you scale to several
workers — same migrations, same code, same backups.

Two methods cover the access (everything else in the codebase builds on
them):

* :meth:`Database.tx` — **read/write.** One transaction: commit on success,
  roll back on error. Every service and every command goes through it.
* :meth:`Database.query` — **read only.** One statement, no commit.

And one promise: :meth:`Database.backup` / :meth:`Database.restore` move the
entire database to and from a single JSON file, all-or-nothing. The jobs loop
backs up daily (``BACKUP_KEEP`` of them are kept) and ``waifu backup`` /
``waifu restore`` / ``/backup`` do it on demand — so "the database vanished"
is a sentence this bot never says.
"""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Self

from sqlalchemy import (
    Boolean,
    DateTime,
    Select,
    func,
    select,
    text,
)
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from waifu.db.models import Base
from waifu.logging import get_logger
from waifu.settings import Settings, get_settings

log = get_logger("db.database")

#: Marker inside every backup file; a restore of anything else is refused.
BACKUP_MAGIC = {"app": "ultimate-waifu-bot", "backup": 1}


class BackupError(RuntimeError):
    """A backup or restore that could not be completed.

    Restore is all-or-nothing (one transaction), so this error always means
    *the database you had is exactly the database you still have*.
    """


def _jsonable(value: Any) -> Any:
    """Make one column value JSON-safe (datetimes → ISO, bytes → base64)."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (bytes, bytearray)):
        return base64.b64encode(bytes(value)).decode("ascii")
    return str(value)


def _restore_value(column: Any, value: Any) -> Any:
    """Reverse of :func:`_jsonable` for the column types this schema uses."""
    if value is None:
        return None
    if isinstance(column.type, DateTime) and isinstance(value, str):
        return datetime.fromisoformat(value)
    if isinstance(column.type, Boolean):
        return bool(value)
    return value


def _read_backup_payload(source: Path) -> dict[str, Any]:
    """Synchronous file read (kept out of the async path for the linter and the
    GIL alike); a failure becomes a :class:`BackupError`, never a traceback."""
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupError(f"cannot read {source}: {exc}") from exc
    if not isinstance(payload, dict):
        raise BackupError(f"{source} is not a waifu backup file (no matching header)")
    return payload


def prune_backups(dest_dir: str | Path, *, keep: int = 10) -> int:
    """Delete the oldest backups beyond ``keep``; returns how many were removed.

    The timestamp is in the filename, so name order is age order — no stat
    games, no clock skew.
    """
    dest = Path(dest_dir)
    if not dest.is_dir():
        return 0
    files = sorted(dest.glob("waifu-backup-*.json"), key=lambda path: path.name)
    extra = len(files) - keep
    if extra <= 0:
        return 0
    removed = 0
    for path in files[:extra]:
        try:
            path.unlink()
            removed += 1
        except OSError:  # pragma: no cover - permission oddities
            log.warning("could not prune backup %s", path)
    return removed


class Database:
    """Engine + session plumbing for the one database (file or Postgres)."""

    def __init__(
        self,
        url: str,
        *,
        echo: bool = False,
        pool_size: int = 10,
        max_overflow: int = 20,
        statement_timeout_ms: int = 8000,
        is_sqlite: bool = False,
    ) -> None:
        self.url = url
        self.is_sqlite = is_sqlite or url.startswith("sqlite")
        self.statement_timeout_ms = statement_timeout_ms
        #: The backing file in one-file mode (relative → project root), for
        #: ``/doctor`` and the operators who `ls` their deployment.
        self.sqlite_file: Path | None = None
        if self.is_sqlite:
            prefix = "sqlite+aiosqlite:///"
            if url.startswith(prefix):
                candidate = Path(url[len(prefix) :])
                if not candidate.is_absolute() and ":memory:" not in str(candidate):
                    # The engine resolves a relative sqlite path against the
                    # process cwd — mirror that exact rule here (and
                    # ``from_settings`` passes an absolute URL anchored to the
                    # project root, which is what a deployment actually runs).
                    candidate = Path.cwd() / candidate
                self.sqlite_file = candidate
                if ":memory:" not in str(candidate):
                    # One-file mode owns its file: the parent dir is created here
                    # (a missing directory would otherwise be an opaque
                    # "unable to open database file" on the first connect).
                    candidate.parent.mkdir(parents=True, exist_ok=True)
        kwargs: dict[str, Any] = {"echo": echo, "future": True, "pool_pre_ping": True}
        if not self.is_sqlite:
            kwargs.update(pool_size=pool_size, max_overflow=max_overflow)
        self._engine: AsyncEngine = create_async_engine(url, **kwargs)
        self._sessionmaker = async_sessionmaker(
            self._engine, expire_on_commit=False, autoflush=False
        )
        if not self.is_sqlite:
            self._install_pg_defaults()
        else:
            self._install_sqlite_defaults()

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> Database:
        cfg = settings or get_settings()
        # The engine resolves a relative sqlite path against the *process cwd*,
        # while the deployment (and this class) mean the project root — so the
        # URL is made absolute here, through the same rule the settings use.
        url = cfg.database_url
        if ":memory:" not in url and cfg.sqlite_path is not None:
            cfg.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
            url = f"sqlite+aiosqlite:///{cfg.sqlite_path}"
        return cls(
            url,
            echo=cfg.db_echo,
            pool_size=cfg.db_pool_size,
            max_overflow=cfg.db_max_overflow,
            statement_timeout_ms=cfg.db_statement_timeout_ms,
        )

    # ----------------------------------------------------------------- tuning
    def _install_pg_defaults(self) -> None:
        from sqlalchemy import event

        @event.listens_for(self._engine.sync_engine, "connect")
        def _on_connect(dbapi_conn: Any, _rec: Any) -> None:
            cur = dbapi_conn.cursor()
            # A stuck query must not wedge an update handler forever.
            cur.execute(f"SET statement_timeout = {int(self.statement_timeout_ms)}")
            cur.execute("SET timezone = 'UTC'")
            # Serializable-by-default is overkill here; read committed + CAS is the design.
            cur.execute("SET idle_in_transaction_session_timeout = '30s'")
            try:
                cur.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")  # fuzzy /search on names
                cur.execute("CREATE EXTENSION IF NOT EXISTS citext")
            except Exception:  # pragma: no cover - no superuser on managed PG
                dbapi_conn.rollback()
            cur.close()

    def _install_sqlite_defaults(self) -> None:
        """One-file mode (the default): WAL so readers never block on a writer.

        Matters when the daily backup pass or ``waifu backup`` reads the file
        while the bot keeps serving — without WAL the writer would hold the
        whole file.
        """
        from sqlalchemy import event

        @event.listens_for(self._engine.sync_engine, "connect")
        def _pragmas(dbapi_conn: Any, _rec: Any) -> None:
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=8000")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()

    # ---------------------------------------------------------------- surface
    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    def session(self) -> AsyncSession:
        return self._sessionmaker()

    @asynccontextmanager
    async def tx(self) -> AsyncIterator[AsyncSession]:
        """Method 1 — **read/write.** Commit on success, roll back on error,
        never swallow the exception."""
        async with self._sessionmaker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def query(self, stmt: Select) -> list[Any]:
        """Method 2 — **read only.** Run one statement, return its scalar rows.

        No commit, no write — the fast path for "look something up" (health
        checks, digests, the command menu).
        """
        async with self._sessionmaker() as session:
            return list((await session.execute(stmt)).scalars().all())

    @asynccontextmanager
    async def job_tx(self, name: str, *, wait: bool = False) -> AsyncIterator[AsyncSession | None]:
        """Transactional unit guarded by a Postgres advisory lock.

        Two shards running the auction-closer would otherwise double-settle. When
        the lock is not acquired (another worker owns it) the context yields
        ``None`` and the caller skips its turn. In one-file mode (single
        process by definition) it degrades to a plain transaction.
        """
        if self.is_sqlite:
            async with self.tx() as session:
                yield session
            return
        key = abs(hash(name)) % (2**63)
        async with self._engine.connect() as conn:
            locked = (
                await conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": key})
            ).scalar_one()
            if not locked:
                if not wait:
                    yield None
                    return
                await conn.execute(text("SELECT pg_advisory_lock(:k)"))
            try:
                async with self._sessionmaker() as session:
                    try:
                        yield session
                        await session.commit()
                    except Exception:
                        await session.rollback()
                        raise
            finally:
                await conn.execute(text("SELECT pg_advisory_unlock(:k)"))

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[AsyncConnection]:
        async with self._engine.begin() as conn:
            yield conn

    async def skip_locked(self, stmt: Select, *, limit: int = 25) -> list[Any]:
        """Claim work items across workers without blocking (spawn queue, raffles).

        In one-file mode the ``FOR UPDATE SKIP LOCKED`` clause is dropped by the
        SQLite dialect — correct, because there is only one writer anyway.
        """
        stmt = stmt.with_for_update(skip_locked=True).limit(limit)
        async with self.tx() as session:
            return list((await session.execute(stmt)).scalars())

    async def create_all(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def drop_all(self) -> None:  # pragma: no cover - tests
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)

    async def dispose(self) -> None:
        await self._engine.dispose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.dispose()

    # --------------------------------------------------------- backup / restore
    async def backup(self, dest_dir: str | Path | None = None) -> tuple[Path, dict[str, int]]:
        """Snapshot the **entire** database to one JSON file.

        Every table, in foreign-key order, every row, every column. The write
        is atomic (temp file + rename), so a crash mid-backup never leaves a
        torn file that a restore could read halfway through. Returns
        ``(path, counts)`` where ``counts`` maps table name → row count.
        """
        from waifu import __version__
        from waifu.utils.time import now_utc

        dest = Path(dest_dir) if dest_dir else Path("backups")
        dest.mkdir(parents=True, exist_ok=True)
        counts: dict[str, int] = {}
        tables_payload: dict[str, list[dict[str, Any]]] = {}
        async with self._engine.connect() as conn:
            for table in Base.metadata.sorted_tables:  # FK parents first
                result = await conn.execute(select(table))
                rows = [
                    {column: _jsonable(value) for column, value in row._mapping.items()}
                    for row in result
                ]
                counts[table.name] = len(rows)
                tables_payload[table.name] = rows
        meta = {**BACKUP_MAGIC, "version": __version__, "created_at": now_utc().isoformat()}
        meta["rows"] = sum(counts.values())
        meta["tables"] = counts
        final = dest / f"waifu-backup-{now_utc().strftime('%Y%m%d-%H%M%SZ')}.json"
        if final.exists():  # two backups inside one second must not overwrite
            final = (
                dest
                / f"waifu-backup-{now_utc().strftime('%Y%m%d-%H%M%SZ')}-{uuid.uuid4().hex[:8]}.json"
            )
        tmp = dest / f".{final.name}.tmp"
        tmp.write_text(json.dumps({"meta": meta, "tables": tables_payload}), encoding="utf-8")
        tmp.replace(final)
        log.info("backup written: %s (%d rows, %d tables)", final, meta["rows"], len(counts))
        return final, counts

    async def restore(self, path: str | Path) -> dict[str, Any]:
        """Replace the database with the contents of a backup file.

        All-or-nothing: every table is emptied and reinserted inside **one**
        transaction (parents before children), so a failure at any point rolls
        the whole thing back — the database you had is the database you still
        have. Returns ``{"rows": n, "meta": …}``.
        """
        source = Path(path)
        payload = _read_backup_payload(source)
        meta = payload.get("meta") if isinstance(payload, dict) else None
        tables = payload.get("tables") if isinstance(payload, dict) else None
        if (
            not isinstance(meta, dict)
            or meta.get("app") != BACKUP_MAGIC["app"]
            or meta.get("backup") != BACKUP_MAGIC["backup"]
            or not isinstance(tables, dict)
        ):
            raise BackupError(f"{source} is not a waifu backup file (no matching header)")
        current = list(Base.metadata.sorted_tables)  # FK parents first
        current_names = {table.name for table in current}
        if set(tables) != current_names:
            detail = ", ".join(sorted(current_names ^ set(tables)))
            raise BackupError(
                f"schema mismatch — {source} was made by a different version of the bot "
                f"(tables differ: {detail})"
            )
        for table in current:
            if not tables[table.name]:
                continue  # nothing dumped from this table, nothing to compare
            expected = {column.name for column in table.columns}
            if set(tables[table.name][0]) != expected:
                raise BackupError(
                    f"column drift in '{table.name}' — {source} was made by a different "
                    "version of the bot"
                )
        rows_restored = 0
        async with self.tx() as session:  # one commit — or nothing at all
            for table in reversed(current):
                await session.execute(table.delete())
            for table in current:
                rows = tables[table.name] or []
                if not rows:
                    continue
                values = [
                    {
                        name: _restore_value(table.columns[name], value)
                        for name, value in row.items()
                    }
                    for row in rows
                ]
                await session.execute(table.insert(), values)
                rows_restored += len(rows)
        log.info("restored %d rows from %s", rows_restored, source)
        return {"rows": rows_restored, "meta": meta}

    # ------------------------------------------------------------ diagnostics
    async def healthcheck(self) -> dict[str, Any]:
        async with self._engine.connect() as conn:
            ok = (await conn.execute(text("SELECT 1"))).scalar_one()
            info: dict[str, Any] = {"db": "ok" if ok == 1 else "bad"}
            if not self.is_sqlite:
                row = (
                    await conn.execute(
                        text(
                            "SELECT version(), current_setting('server_version_num')::int, "
                            "pg_size_pretty(pg_database_size(current_database()))"
                        )
                    )
                ).one()
                info.update(pg_version=row[1], pg_size=row[2])
            elif self.sqlite_file is not None and self.sqlite_file.exists():
                info.update(
                    sqlite_file=str(self.sqlite_file),
                    sqlite_size=f"{self.sqlite_file.stat().st_size:,} bytes",
                )
            row = await conn.execute(text("SELECT count(*) FROM users"))
            info["users"] = int(row.scalar_one())
            # The two counts an operator of *this* bot checks first, because the shape of
            # the product is unusual: players accumulate, characters are uploaded. A fresh
            # install therefore legitimately reports ``characters: 0`` — which is why the
            # CLI's doctor explains what to do about it instead of looking broken.
            from waifu.db.models import Character, RarityChance

            for model, key in ((Character, "characters"), (RarityChance, "tiers")):
                info[key] = int(
                    (
                        await conn.execute(
                            select(func.count()).select_from(model.__table__)  # no SQL strings
                        )
                    ).scalar_one()
                )
        return info

    async def table_sizes(self) -> list[tuple[str, int]]:
        if self.is_sqlite:
            # A file database has no per-table page stats worth showing — row
            # counts are what an operator actually wants to eyeball.
            async with self._engine.connect() as conn:
                rows = [
                    (
                        table.name,
                        int(
                            (
                                await conn.execute(select(func.count()).select_from(table))
                            ).scalar_one()
                            or 0
                        ),
                    )
                    for table in Base.metadata.sorted_tables
                ]
            return sorted(rows, key=lambda item: -item[1])[:40]
        async with self._engine.connect() as conn:
            # ``pg_total_relation_size`` covers the table plus its indexes and TOAST,
            # which is what an operator actually wants when /ping says "big".
            rows = (
                await conn.execute(
                    text(
                        "SELECT relname, pg_total_relation_size(c.oid) FROM pg_class c "
                        "JOIN pg_namespace n ON n.oid = c.relnamespace "
                        "WHERE n.nspname='public' AND c.relkind='r' ORDER BY 2 DESC LIMIT 15"
                    )
                )
            ).all()
        return [(str(r[0]), int(r[1])) for r in rows]


def db_url_from_parts(
    driver: str, host: str, database: str, user: str, password: str
) -> str:  # pragma: no cover
    return f"{driver}://{user}:{password}@{host}/{database}"
