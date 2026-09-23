"""Console entry point: ``python -m waifu <command>`` (``waifu-bot`` is the same thing).

Subcommands are thin wrappers around the library — ``bot`` calls
:func:`waifu.core.app.run`, ``doctor`` calls :meth:`Database.healthcheck`, and
``import-legacy`` runs the Summon-bot migration script — so there is exactly one way to
start the bot whether you are on Docker, systemd, or a laptop. Summon-bot shipped four
different ``asyncio.run`` blocks (``main.py``, ``bot.py``, ``run.sh``, the Procfile) and
each had a slightly different startup, which is how a fix that worked in production kept
breaking in dev.

Nothing here touches the network except ``bot``, so ``doctor``/``migrate``/``import-legacy``
work on a machine with no token — useful the moment you point the bot at a copied
``summon.db``.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Awaitable, Sequence

__all__ = ["cli", "main"]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="waifu", description="Waifu — gacha, economy, spawns and analytics for Telegram."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    bot = sub.add_parser("bot", help="run the bot (long polling, or webhooks with MODE=webhook)")
    bot.add_argument("--token", help="override BOT_TOKEN")
    bot.add_argument("--dev", action="store_true", help="dev console errors + verbose logging")
    bot.add_argument("--drop-pending", action="store_true", help="discard queued updates on start")

    doctor = sub.add_parser("doctor", help="config + database + capability self-check")
    doctor.add_argument("--json", action="store_true", help="machine-readable output")

    sub.add_parser("migrate", help="create/upgrade the schema (idempotent)")

    seedp = sub.add_parser(
        "seed", help="load the rarity ladders (the roster is empty on purpose; see --catalogue)"
    )
    seedp.add_argument(
        "--catalogue",
        action="store_true",
        help="also insert the shipped catalogue (waifu/data/characters.seed.json)",
    )
    seedp.add_argument(
        "--force", action="store_true", help="re-apply catalogue rows by name+series"
    )

    legacy = sub.add_parser(
        "import-legacy", help="migrate a Summon-bot sqlite file into this schema"
    )
    legacy.add_argument("source", help="path to summon.db (or a postgresql:// URL)")
    legacy.add_argument(
        "--target", default="", help="target DATABASE_URL (defaults to $DATABASE_URL)"
    )
    legacy.add_argument(
        "--dry-run", action="store_true", help="report what would change, write nothing"
    )
    legacy.add_argument("--limit", type=int, default=None, help="cap rows per table (smoke test)")
    legacy.add_argument(
        "--reset-escrow", action="store_true", help="close legacy market escrow before starting"
    )

    api = sub.add_parser(
        "api",
        help="serve the mini-app JSON API (the reference bot's api.py, ported and authenticated)",
    )
    api.add_argument("--host", default="", help="bind address (default API_HOST)")
    api.add_argument("--port", type=int, default=0, help="bind port (default API_PORT)")
    api.add_argument(
        "--insecure-uid-query",
        action="store_true",
        help="accept ?uid= with no signature (development only)",
    )

    jobs = sub.add_parser("jobs", help="run one scheduler pass (cron instead of polling)")
    jobs.add_argument(
        "--name", default="all", help="autospawn | settle | quests | premium | cleanup | all"
    )

    sub.add_parser("version", help="print the package version")
    return parser


def main(argv: Sequence[str] | None = None) -> int:  # noqa: PLR0911 - a subcommand dispatcher is a wall of returns
    args = _parser().parse_args(argv)
    if args.command == "bot":
        return _run_bot(args)
    if args.command == "doctor":
        return _run(_doctor(json_output=args.json))
    if args.command == "migrate":
        return _run(_migrate())
    if args.command == "seed":
        return _run(_seed(catalogue=args.catalogue, force=args.force))
    if args.command == "import-legacy":
        return _run(_import_legacy(args))
    if args.command == "api":
        return _run(_api(host=args.host, port=args.port, insecure_uid=args.insecure_uid_query))
    if args.command == "jobs":
        return _run(_jobs(args.name))
    if args.command == "version":
        from waifu import __version__

        print(__version__)
        return 0
    return 2


def cli() -> None:  # pragma: no cover - console-script shim
    raise SystemExit(main())


def _run(coroutine: Awaitable[int]) -> int:
    try:
        return asyncio.run(coroutine)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _run_bot(args: argparse.Namespace) -> int:
    from waifu.core.app import run

    if args.dev:
        import os

        os.environ.setdefault("WAIFU_TEST_MODE", "1")
        os.environ.setdefault("LOG_LEVEL", "DEBUG")
    return _run(run(token=args.token))


async def _doctor(*, json_output: bool = False) -> int:
    import json as _json

    from waifu.core.app import build_app

    app = await build_app(with_bot=False, negotiate=False)
    pending = ""
    try:
        from waifu.db.migrations.runner import plan

        pending = ", ".join(step.name for step in (await plan(app.ctx.db.engine)).pending)
    except Exception as exc:
        pending = f"unavailable ({exc})"
    try:
        health = await app.ctx.db.healthcheck()
    except Exception as exc:
        health = {"db": f"error: {exc} — run `python -m waifu migrate`"}
    report = {
        "health": health,
        "plugins": app.report.summary(),
        "skipped": [f"{path}: {reason}" for path, reason in app.report.skipped],
        "pending_migrations": pending,
        "redis": bool(app.redis),
        # The owner's event feed: a deploy without it runs "fine" but records
        # nothing — worth a line here, because /logtest only exists once the bot
        # is up.
        "log_channel": app.ctx.settings.log_channel_id,
        "tables": [{"name": name, "bytes": size} for name, size in await app.ctx.db.table_sizes()][
            :40
        ],
    }
    if json_output:
        print(_json.dumps(report, indent=2, default=str))
    else:
        print(f"plugins : {report['plugins']}")
        print(f"schema  : {'pending: ' + pending if pending else 'current'}")
        for line in report["skipped"]:
            print(f"  skipped  {line}")
        print(f"redis   : {'connected' if report['redis'] else 'not configured'}")
        log_channel = report["log_channel"]
        print(
            f"logchan : {'channel ' + str(log_channel) + ' — run /logtest once the bot is up' if log_channel else 'not configured (set LOG_CHANNEL_ID)'}"
        )
        for key, value in report["health"].items():
            print(f"{key:<8}: {value}")
        if report["health"].get("characters") == 0:
            print(ROSTER_EMPTY_HINT)
        print("tables  :")
        for entry in report["tables"]:
            print(f"  {entry['name']:<26}{entry['bytes']:>12,}")
    await app.ctx.db.dispose()
    return 0 if "ok" in str(health.get("db", "")) else 1


async def _api(*, host: str = "", port: int = 0, insecure_uid: bool = False) -> int:
    """Serve the mini-app API on its own, for a front-end that scales apart from the bot.

    Same process, same engine, same repositories as the chat — a second service would need its
    own migrations, which is how the reference deployment's web view drifted from its bot.
    """
    from waifu.api import serve
    from waifu.core.app import build_app

    app = await build_app(with_bot=False, negotiate=False)
    if insecure_uid:
        app.ctx.settings = app.ctx.settings.model_copy(update={"api_allow_uid_query": True})
        print("warning: ?uid= accepted without a signature — development only")
    runner = await serve(app.ctx, host=host, port=port)
    settings = app.ctx.settings
    print(
        f"api: http://{settings.api_host}:{settings.api_port}/api/health — "
        "identity via X-Init-Data (signed initData); POST /api/daily, POST /api/summon"
    )
    try:
        await asyncio.Event().wait()  # pragma: no cover - until Ctrl-C
    except (KeyboardInterrupt, asyncio.CancelledError):  # pragma: no cover
        pass
    finally:
        await runner.cleanup()
        await app.ctx.db.dispose()
    return 0


async def _migrate(*, seed: bool = True, catalogue: bool | None = None, force: bool = False) -> int:
    """Migrations first, then the tier ladders (and only on request, a roster).

    Order matters and is not obvious: :func:`waifu.db.seed.seed_all` writes into
    ``rarity_chances``/``characters``, so running it against an uncreated schema is the
    ``no such table`` that this command exists to prevent. Re-running is safe — the
    migration list is recorded in ``schema_version`` and the seed is an upsert.

    ``catalogue=None`` follows ``SEED_CATALOGUE`` (off). A bot that invents characters for
    its players shows them a demo; the reference deployment had an empty ``characters``
    table and filled it through ``/upload``, so that is the shipped behaviour here.
    """
    from waifu.db.engine import Database
    from waifu.db.migrations.runner import apply as apply_migrations
    from waifu.db.seed import seed_all

    db = Database.from_settings(_settings())
    try:
        applied = await apply_migrations(db.engine)
        print(f"migrations: {', '.join(applied) if applied else 'schema already current'}")
        if seed:
            result = await seed_all(db.engine, characters=catalogue, force=force)
            print("seed: " + ", ".join(f"{key}={_flat(value)}" for key, value in result.items()))
        await _roster_notice(db)
    finally:
        await db.dispose()
    return 0


#: What a fresh install says about its own empty ``characters`` table. The bot shows the
#: same text to the owner via ``/rosterstats`` and ``RosterEmpty``; three audiences, one
#: list of doors, because "where do characters come from?" is the first question here.
ROSTER_EMPTY_HINT = (
    "roster  : empty (by design — this bot does not invent characters)\n"
    "          add them from Telegram: reply to a photo/video/GIF with\n"
    "            /upload <Name> <Series> <1-18>\n"
    "          turn a group into a feed: /autoadd on\n"
    "          or load the optional catalogue: python -m waifu seed --catalogue\n"
    "          or import your old database: python -m waifu import-legacy summon.db"
)


async def _roster_notice(db: object) -> None:
    """Tell the operator, on the terminal, what an empty roster means and how to fill it."""
    from sqlalchemy import func, select

    from waifu.db.models import Character, ClaimChance, RarityChance

    async with db.engine.connect() as conn:
        count = int((await conn.execute(select(func.count()).select_from(Character))).scalar() or 0)
        pulls = int(
            (await conn.execute(select(func.count()).select_from(RarityChance))).scalar() or 0
        )
        claims = int(
            (await conn.execute(select(func.count()).select_from(ClaimChance))).scalar() or 0
        )
    # Stated every run, because "seed: odds={rarity_chances:0}" on a first migrate means
    # *the migration already wrote them* — a count of what is there beats a count of what
    # this command happened to insert.
    print(f"ladders : {pulls} pull tiers, {claims} claim tiers (18 and 18 is a complete set)")
    if count:
        print(f"roster  : {count} character(s) available")
        return
    print(ROSTER_EMPTY_HINT)


def _flat(value: object) -> str:
    """Render a nested seed report on one line."""
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k}:{v}" for k, v in value.items()) + "}"
    return str(value)


async def _seed(*, catalogue: bool = False, force: bool = False) -> int:
    """``waifu seed`` *is* ``waifu migrate``: a seed without the schema is an error and a
    schema without the ladder is a bot that cannot roll, so there is one correct command."""
    return await _migrate(catalogue=True if catalogue else None, force=force)


async def _import_legacy(args: argparse.Namespace) -> int:
    import sys

    sys.path.insert(0, ".")
    from scripts.import_summon import Importer, Report, read_source

    from waifu.db.engine import Database
    from waifu.db.seed import seed_all

    settings = _settings()
    if getattr(args, "target", None):
        settings = settings.model_copy(update={"database_url": args.target})
    db = Database.from_settings(settings)
    try:
        from waifu.db.migrations.runner import apply as apply_migrations

        await apply_migrations(db.engine)
        await seed_all(db.engine)
        data = read_source(args.source)
        async with db.tx() as session:
            importer = Importer(session, data, dry=bool(args.dry_run), report=Report())
            report = await importer.run()
        print(f"import-legacy ({'dry run' if args.dry_run else 'applied'}):\n{report.render()}")
    finally:
        await db.dispose()
    return 0


async def _jobs(name: str) -> int:
    from waifu.core.app import build_app
    from waifu.core.jobs import run_pass

    app = await build_app(with_bot=False, negotiate=False)
    try:
        await app.startup()
        results = await run_pass(app.ctx, name)
        for result in results:
            print(str(result))
        return 1 if any(result.error for result in results) else 0
    finally:
        await app.shutdown()


def _settings():
    from waifu.logging import setup_logging
    from waifu.settings import get_settings

    settings = get_settings()
    setup_logging(settings.log_level, json_logs=settings.log_json)
    return settings


if __name__ == "__main__":  # pragma: no cover
    cli()
