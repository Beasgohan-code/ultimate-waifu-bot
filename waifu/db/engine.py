"""Async Postgres engine (asyncpg) + session plumbing.

Why no SQLite at runtime: this bot's correctness depends on Postgres features —
``INSERT … ON CONFLICT``, ``UPDATE … WHERE`` compare-and-set with ``RETURNING``,
``SELECT … FOR UPDATE SKIP LOCKED`` for the spawn/raffle queues, and
``pg_advisory_lock`` so N shards run one scheduler. The test harness uses SQLite
(``WAIFU_TEST_MODE=1``) only to exercise pure logic, never those paths.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Self

from sqlalchemy import Select, text
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

log = get_logger("db.engine")


class Database:
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
        kwargs: dict[str, Any] = {"echo": echo, "future": True, "pool_pre_ping": True}
        if not self.is_sqlite:
            kwargs.update(pool_size=pool_size, max_overflow=max_overflow)
        self._engine: AsyncEngine = create_async_engine(url, **kwargs)
        self._sessionmaker = async_sessionmaker(
            self._engine, expire_on_commit=False, autoflush=False
        )
        if not self.is_sqlite:
            self._install_pg_defaults()
        else:  # pragma: no cover - test harness only
            self._install_sqlite_defaults()

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> Database:
        cfg = settings or get_settings()
        return cls(
            cfg.database_url,
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

    def _install_sqlite_defaults(self) -> None:  # pragma: no cover - tests
        from sqlalchemy import event

        @event.listens_for(self._engine.sync_engine, "connect")
        def _pragmas(dbapi_conn: Any, _rec: Any) -> None:
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=8000")
            cur.close()

    # ---------------------------------------------------------------- surface
    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    def session(self) -> AsyncSession:
        return self._sessionmaker()

    @asynccontextmanager
    async def tx(self) -> AsyncIterator[AsyncSession]:
        """Commit on success, roll back on error, never swallow the exception."""
        async with self._sessionmaker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    @asynccontextmanager
    async def job_tx(self, name: str, *, wait: bool = False) -> AsyncIterator[AsyncSession | None]:
        """Transactional unit guarded by a Postgres advisory lock.

        Two shards running the auction-closer would otherwise double-settle. When
        the lock is not acquired (another worker owns it) the context yields
        ``None`` and the caller skips its turn.
        """
        if self.is_sqlite:  # pragma: no cover - tests
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
                await conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": key})
            try:
                async with self._sessionmaker() as session:
                    try:
                        yield session
                        await session.commit()
                    except Exception:
                        await session.rollback()
                        raise
            finally:
                await conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[AsyncConnection]:
        async with self._engine.begin() as conn:
            yield conn

    async def skip_locked(self, stmt: Select, *, limit: int = 25) -> list[Any]:
        """Claim work items across workers without blocking (spawn queue, raffles)."""
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
            row = await conn.execute(text("SELECT count(*) FROM users"))
            info["users"] = int(row.scalar_one())
        return info

    async def table_sizes(self) -> list[tuple[str, int]]:
        if self.is_sqlite:  # pragma: no cover
            return []
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
