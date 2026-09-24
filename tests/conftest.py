"""Shared fixtures: a fully wired application over an in-memory database.

The point of this file is that **tests exercise the same object graph production
runs** — ``AppContext`` + every service from :func:`waifu.services.build` — against
SQLite. No mocks of our own layers, because the bugs this bot is designed to avoid
(double payouts, seed/migration drift, a service that only works when another one
happens to be wired) all live *between* layers.

The one place a test diverges from an install: gameplay needs a pool to pull from, so
:func:`db` asks for the shipped catalogue explicitly (``characters=True``). A fresh
database has none — that contract is asserted in :mod:`tests.test_uploads`, not assumed
here.

Telegram is the one thing stubbed: ``bot=None`` means any handler-side send is a
no-op, and the services below are chosen so the flows under test never need one.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

os.environ.setdefault("WAIFU_TEST_MODE", "1")

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from waifu.core.context import AppContext
from waifu.db import Database
from waifu.db.models import Character
from waifu.db.repo import users as user_repo
from waifu.db.seed import seed_all
from waifu.db.state import Cache
from waifu.services import build as build_services
from waifu.settings import Settings


def test_settings(tmp_path: object) -> Settings:
    """A Settings instance that passes production validation without Redis/Postgres."""
    return Settings(
        _env_file=None,
        bot_token="123456:TEST-token-not-a-real-bot",
        owner_id=1,
        admin_ids=[1],
        database_url=f"sqlite+aiosqlite:///{tmp_path}/waifu-test-{uuid.uuid4().hex}.db",
        redis_url="",
        data_dir=str(tmp_path),
        log_channel_id=0,
        support_chat_id=0,
    )


@pytest_asyncio.fixture
async def db(tmp_path) -> AsyncIterator[Database]:
    """A fresh database: ladders from the migration, roster asked for explicitly.

    ``characters=True`` is a *test* decision, not the product's: gameplay tests need a
    pool to pull from, and the shipped catalogue is the cheapest real one. A fresh install
    leaves the roster empty and fills it through ``/upload`` — the contract
    :mod:`tests.test_catalogue_seed` pins down.
    """
    settings = test_settings(tmp_path)
    database = Database.from_settings(settings)
    await database.create_all()
    await seed_all(database.engine, characters=True)
    try:
        yield database
    finally:
        await database.dispose()


@pytest_asyncio.fixture
async def tx(db) -> AsyncIterator:
    """One transaction per test, rolled back at the end.

    Service methods take a session so they can be composed inside a caller's
    transaction — which is exactly what makes them testable without commits: the
    fixture hands you the transaction and undoes everything afterwards.
    """
    async with db.tx() as session:
        yield session
        await session.rollback()


@pytest_asyncio.fixture
async def ctx(db, tmp_path) -> AppContext:
    """The application context, services and all, over :func:`db`."""
    settings = test_settings(tmp_path)
    context = AppContext(settings=settings, db=db, cache=Cache(prefix="test"), redis=None, bot=None)
    context.caps = type(
        "Caps", (), {"allow": staticmethod(lambda name: False), "rich_messages": False}
    )()  # type: ignore[assignment]
    return build_services(context)


@pytest_asyncio.fixture
async def session(db) -> AsyncIterator:
    async with db.tx() as value:
        yield value


@pytest_asyncio.fixture
async def player(ctx) -> int:
    """A registered player with enough coins to exercise every paid path."""
    user_id = 4242
    async with ctx.db.tx() as session:
        await user_repo.upsert(session, user_id, username="tester", first_name="Test")
        from waifu.db.repo import economy as ledger

        await ledger.credit(session, user_id, 5_000_000, "admin_grant", reference="fixture")
    return user_id


@pytest_asyncio.fixture
async def partner(ctx) -> int:
    """A second player, so theft/trade/gift paths have a real counterparty."""
    user_id = 9999
    async with ctx.db.tx() as session:
        await user_repo.upsert(session, user_id, username="partner", first_name="Part")
        from waifu.db.repo import economy as ledger

        await ledger.credit(session, user_id, 100_000, "admin_grant", reference="fixture")
    return user_id


@pytest_asyncio.fixture
async def any_character(ctx) -> Character:
    async with ctx.db.session() as session:
        return (
            await session.execute(
                select(Character)
                .where(Character.is_active.is_(True))
                .order_by(Character.id)
                .limit(1)
            )
        ).scalar_one()


@pytest.fixture
def count_characters():
    """Helper for tests that need to assert on the catalogue."""

    async def _count(session, *, rarity_id: int | None = None) -> int:
        stmt = select(func.count()).select_from(Character)
        if rarity_id is not None:
            stmt = stmt.where(Character.rarity_id == rarity_id)
        return int((await session.execute(stmt)).scalar_one() or 0)

    return _count
