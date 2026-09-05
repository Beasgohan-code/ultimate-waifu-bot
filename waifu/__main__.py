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
    sub.add_parser("seed", help="load the Summon-parity catalogue + rarity ladders")

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
        return _run(_seed())
    if args.command == "import-legacy":
        return _run(_import_legacy(args))
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
        for key, value in report["health"].items():
            print(f"{key:<8}: {value}")
        print("tables  :")
        for entry in report["tables"]:
            print(f"  {entry['name']:<26}{entry['bytes']:>12,}")
    await app.ctx.db.dispose()
    return 0 if "ok" in str(health.get("db", "")) else 1


async def _migrate(*, seed: bool = True) -> int:
    """Migrations first, then the shipped catalogue.

    Order matters and is not obvious: :func:`waifu.db.seed.seed_all` writes into
    ``rarity_chances``/``characters``, so running it against an uncreated schema is the
    ``no such table`` that this command exists to prevent. Re-running is safe — the
    migration list is recorded in ``schema_version`` and the seed is an upsert.
    """
    from waifu.db.engine import Database
    from waifu.db.migrations.runner import apply as apply_migrations
    from waifu.db.seed import seed_all

    db = Database.from_settings(_settings())
    try:
        applied = await apply_migrations(db.engine)
        print(f"migrations: {', '.join(applied) if applied else 'schema already current'}")
        if seed:
            result = await seed_all(db.engine)
            print("seed: " + ", ".join(f"{key}={_flat(value)}" for key, value in result.items()))
    finally:
        await db.dispose()
    return 0


def _flat(value: object) -> str:
    """Render a nested seed report on one line."""
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k}:{v}" for k, v in value.items()) + "}"
    return str(value)


async def _seed() -> int:
    """``waifu seed`` *is* ``waifu migrate``: a seed without the schema is an error and a
    schema without the seed is an empty bot, so there is only one correct command."""
    return await _migrate()


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
