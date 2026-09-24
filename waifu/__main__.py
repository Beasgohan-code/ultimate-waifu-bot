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
import os
import sys
import time
from collections.abc import Awaitable, Sequence
from pathlib import Path
from typing import Any

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
        "--name", default="all", help="any pass name (autospawn, auctions, backup, …) or all"
    )

    backupp = sub.add_parser(
        "backup", help="snapshot the whole database to one JSON file (default BACKUP_DIR)"
    )
    backupp.add_argument("--dir", default="", help="output directory (default BACKUP_DIR)")

    restorep = sub.add_parser(
        "restore", help="replace the database with a backup file (all-or-nothing)"
    )
    restorep.add_argument("file", help="path to a waifu-backup-*.json file")
    restorep.add_argument(
        "--yes",
        action="store_true",
        help="apply it (without --yes only a preview is printed, nothing is written)",
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
    if args.command == "backup":
        return _run(_backup_file(dir=args.dir))
    if args.command == "restore":
        return _run(_restore_file(args.file, apply=args.yes))
    if args.command == "version":
        from waifu import __version__

        print(__version__)
        return 0
    return 2


def cli() -> None:  # pragma: no cover - console-script shim
    raise SystemExit(main())


def _env_name_for_field(field: str) -> str:
    """The env var name behind a Settings field (aliases included)."""
    from waifu.settings import Settings

    info = Settings.model_fields.get(field)
    if info is not None:
        alias = info.validation_alias or info.alias
        if isinstance(alias, str):
            return alias.upper()
        choices = getattr(alias, "choices", None)
        if choices:
            return str(choices[0]).upper()
    return field.upper()


def _explain_settings_error(exc: Exception) -> None:
    """Turn pydantic-settings' 'error parsing value for field …' into an answer.

    A deploy that dies on env parsing must say *which variable*, *what value it
    had*, and *what format works* — that triple is what takes a 1 a.m. incident
    from an hour to five minutes.
    """
    import re

    m = re.search(r'field "(\w+)"', str(exc))
    field = m.group(1) if m else "?"
    env_name = _env_name_for_field(field)
    value = os.environ.get(env_name)
    if value is None:
        detail = f"({env_name} is not set — the default was rejected, which means the "
        "failure is in a computed field)"
    elif any(word in field.lower() for word in ("token", "secret", "password")):
        detail = f"({env_name} is set, {len(value)} chars — secret values are not printed)"
    else:
        detail = f"({env_name}={value!r})"
    hint = ""
    if field in ("admin_ids", "allowed_media_hosts", "guess_reactions", "streak_multiplier_curve"):
        hint = "\n  list fields accept a comma-separated value (a,b,c) or a JSON array"
    print(
        f"error: the environment does not parse — field '{field}' failed to load {detail}\n"
        f"  fix {env_name} in the deployment's environment variables and redeploy.{hint}",
        file=sys.stderr,
    )


def _run(coroutine: Awaitable[int]) -> int:
    try:
        return asyncio.run(coroutine)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        from pydantic_settings import SettingsError

        if isinstance(exc, SettingsError):
            _explain_settings_error(exc)
        else:
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

    # migrate=False: the doctor reports pending schema work, it does not do it
    app = await build_app(with_bot=False, negotiate=False, with_plugins=False, migrate=False)
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
    backup_info = _latest_backup_line(Path(app.ctx.settings.backup_dir))
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
        "backup": backup_info,
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
        print(f"backup  : {report['backup']}")
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

    app = await build_app(with_bot=False, negotiate=False, with_plugins=False)
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
    from waifu.db import Database
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

    from waifu.db import Database
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

    app = await build_app(with_bot=False, negotiate=False, with_plugins=False)
    try:
        await app.startup()
        results = await run_pass(app.ctx, name)
        for result in results:
            print(str(result))
        return 1 if any(result.error for result in results) else 0
    finally:
        await app.shutdown()


async def _backup_file(*, dir: str = "") -> int:
    """``waifu backup`` — the database becomes one JSON file under BACKUP_DIR."""
    from waifu.db import Database

    settings = _settings()
    db = Database.from_settings(settings)
    try:
        path, counts = await db.backup(dir or str(settings.backup_dir))
    finally:
        await db.dispose()
    total = sum(counts.values())
    print(f"backup: {path} ({total:,} rows across {len(counts)} tables)")
    for name, count in sorted(counts.items(), key=lambda item: -item[1])[:15]:
        print(f"  {name:<28}{count:>10,}")
    print("restore any time with: python -m waifu restore " + path.name)
    return 0


def _backup_preview(file: str) -> dict[str, Any] | None:
    """Read and validate a backup file's header (synchronous file work).

    Returns the ``meta`` dict, or prints the reason and returns ``None`` when
    the file is not a waifu backup — checked *before* anything may be written.
    """
    import json as _json

    from waifu.db import BACKUP_MAGIC

    try:
        payload = _json.loads(Path(file).read_text(encoding="utf-8"))
        meta = payload.get("meta") if isinstance(payload, dict) else None
    except Exception as exc:
        print(f"error: {file} is not readable as a waifu backup ({exc})", file=sys.stderr)
        return None
    if (
        not isinstance(meta, dict)
        or meta.get("app") != BACKUP_MAGIC["app"]
        or meta.get("backup") != BACKUP_MAGIC["backup"]
        or not isinstance(meta.get("tables"), dict)
    ):
        print(f"error: {file} is not a waifu backup file (no matching header)", file=sys.stderr)
        return None
    return meta


def _latest_backup_line(backup_dir: Path) -> str:
    """The doctor's ``backup:`` line (synchronous file work)."""
    try:
        backup_files = sorted(backup_dir.glob("waifu-backup-*.json"), key=lambda p: p.name)
        if backup_files:
            latest = backup_files[-1]
            age_h = (time.time() - latest.stat().st_mtime) / 3600
            return f"{latest.name} ({age_h:.0f} h ago, {latest.stat().st_size:,} bytes)"
    except OSError:  # pragma: no cover - unreadable backup dir
        pass
    return "none yet (one is taken daily; /backup or `waifu backup` on demand)"


async def _restore_file(file: str, *, apply: bool) -> int:
    """``waifu restore FILE`` — without ``--yes`` this previews and writes nothing."""
    from waifu.db import BackupError, Database

    meta = _backup_preview(file)
    if meta is None:
        return 1
    tables = meta.get("tables", {})
    print(
        f"backup file : {file}\n"
        f"  created   : {meta.get('created_at')}\n"
        f"  version   : {meta.get('version')}\n"
        f"  rows      : {meta.get('rows', 0):,} across {len(tables)} tables"
    )
    if not apply:
        print("\nthis is a preview — run again with --yes to replace the database with it.")
        return 0
    db = Database.from_settings(_settings())
    try:
        result = await db.restore(file)
    except BackupError as exc:
        print(f"error: restore aborted, the database is unchanged — {exc}", file=sys.stderr)
        return 1
    finally:
        await db.dispose()
    print(f"restored: {result['rows']:,} rows — the database now holds this backup.")
    return 0


def _settings():
    from waifu.logging import setup_logging
    from waifu.settings import get_settings

    settings = get_settings()
    setup_logging(settings.log_level, json_logs=settings.log_json)
    return settings


if __name__ == "__main__":  # pragma: no cover
    cli()
