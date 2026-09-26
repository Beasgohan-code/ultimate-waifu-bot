"""Application assembly: settings → infrastructure → context → dispatcher → run.

This is the only module allowed to know how everything is wired. ``waifu.cli`` calls
:func:`build_app` (or :func:`run`), tests call :func:`build_app` with an in-memory
database and never start the network, and nothing else constructs a ``Bot``, a
``Database`` or a ``Redis`` client. That single entry point is what makes
``waifu dev`` / ``waifu bot`` / ``waifu job --name …`` / the web panel all run against
*identical* wiring — Summon-bot had four ad-hoc ``asyncio.run`` blocks with slightly
different setups, and "works in dev, broken in prod" lived in those gaps.

Order matters:

1. settings (validated — a bad config fails here, not on the first command);
2. database + migrations check;
3. Redis (FSM, cooldowns, queues);
4. capability negotiation with the API endpoint (**before** services, because
   services branch on :class:`~waifu.tg.caps.Caps` while rendering);
5. services;
6. plugins/routers + middlewares;
7. start polling or webhooks.
"""

from __future__ import annotations

import asyncio
import signal
from dataclasses import dataclass, field
from typing import Any

from aiogram import Bot, Dispatcher

from waifu import __api_version__, __version__
from waifu.core.bot import (
    apply_identity,
    build_bot,
    configure_menu_button,
    delete_webhook_if_polling,
    probe_api_features,
)
from waifu.core.context import AppContext
from waifu.core.dp import RegistrationReport, build_dispatcher, build_storage
from waifu.db import Database
from waifu.db.state import Cache, Redis
from waifu.logging import get_logger, setup_logging
from waifu.services import build as build_services
from waifu.settings import Settings, get_settings
from waifu.tg.caps import Caps

log = get_logger("core.app")


async def _owner_log(app: BuiltApp, text: str, *, rich: Any | None = None) -> None:
    """One line to the owner's log channel; a failed send must not break the lifecycle."""
    try:
        await app.ctx.notify(text, rich=rich)
    except Exception as exc:  # pragma: no cover - by contract notify() does not raise
        log.debug("owner log send failed: %s", exc)


@dataclass(slots=True)
class BuiltApp:
    """Everything a runner needs, so shutdown can release each part in order."""

    ctx: AppContext
    dp: Dispatcher
    bot: Bot
    settings: Settings
    report: RegistrationReport = field(default_factory=RegistrationReport)
    redis: Redis | None = None

    @property
    def summary(self) -> str:
        return (
            f"{self.report.summary()} · caps: {self.ctx.caps.summary()} · "
            f"db: {self.settings.db_driver} · redis: {'on' if self.redis else 'off'}"
        )

    async def startup(self) -> None:
        await self.ctx.startup()

    async def shutdown(self) -> None:
        """Close in reverse order of construction; each step is best-effort."""
        try:
            await self.ctx.shutdown()
        finally:
            try:
                await self.bot.session.close()
            except Exception as exc:  # pragma: no cover - shutdown noise
                log.debug("bot session close: %s", exc)


async def build_app(
    settings: Settings | None = None,
    *,
    token: str | None = None,
    with_bot: bool = True,
    negotiate: bool = True,
    with_plugins: bool = True,
    migrate: bool = True,
) -> BuiltApp:
    """Construct the full application. Never starts it.

    ``with_plugins=False`` keeps a bare dispatcher (no routers attached) and
    reports the plugin status through :func:`waifu.core.dp.plugin_report` —
    for the CLI diagnostics (doctor/jobs/api) that never serve an update and
    must not leave the module-level routers attached for a later dispatcher.
    """
    cfg = settings or get_settings()
    db = Database.from_settings(cfg)
    if migrate:
        # A one-file deploy has no separate deploy step: the process that owns
        # the database applies its pending schema upgrades at startup (idempotent,
        # re-run safe). Without this, a fresh deploy boots into an empty file and
        # every query dies with "no such table" (the Render deploys of 2026-09-24).
        from waifu.db.migrations.runner import apply as apply_migrations

        try:
            applied = await apply_migrations(db.engine)
        except Exception as exc:
            log.error("schema upgrade failed: %s — run `waifu migrate`", exc)
            raise
        if applied:
            log.info("schema: applied %s", ", ".join(applied))
    redis: Redis | None = None
    if cfg.redis_dsn:
        try:
            redis = await Redis.create(cfg)
        except Exception as exc:  # pragma: no cover - depends on the environment
            log.error("redis unavailable (%s) — falling back to in-process caches", exc)
            redis = None
    cache = Cache(redis=redis.client if redis else None, prefix=cfg.redis_key_prefix)
    bot = build_bot(cfg, token=token) if (with_bot and cfg.bot_token) else None

    api_flags: dict[str, bool] = {}
    if bot is not None and negotiate:
        try:
            api_flags = await probe_api_features(bot)
        except Exception as exc:  # pragma: no cover - offline/dev instance
            log.warning("capability probe skipped: %s", exc)

    ctx = AppContext(
        settings=cfg,
        db=db,
        cache=cache,
        redis=redis,
        bot=bot,
        api_flags=api_flags,
        caps=Caps.negotiate(cfg, api_flags),
    )
    ctx = build_services(ctx)
    if with_plugins:
        dp, report = build_dispatcher(cfg, ctx)
    else:
        from waifu.core.dp import plugin_report

        # Bare dispatcher: the diagnostics never serve an update, and attaching
        # the real routers here would make any later build_dispatcher fail.
        dp = Dispatcher(storage=build_storage(cfg), ctx=ctx, settings=cfg)
        report = plugin_report()
    if bot is not None:
        # aiogram injects workflow_data into every handler; ``ctx`` is how handlers
        # reach services without importing a singleton.
        dp.workflow_data.update(
            ctx=ctx, settings=cfg, bot=bot, db=db, redis=redis, cache=cache, caps=ctx.caps
        )
    return BuiltApp(ctx=ctx, dp=dp, bot=bot, settings=cfg, report=report, redis=redis)  # type: ignore[arg-type]


async def run(*, token: str | None = None, settings: Settings | None = None) -> None:
    """Poll or serve webhooks until cancelled, then shut down cleanly."""
    cfg = settings or get_settings()
    setup_logging(cfg.log_level, json_logs=cfg.log_json)
    app = await build_app(cfg, token=token)
    if app.bot is None:
        raise RuntimeError("BOT_TOKEN is required to run the bot (waifu doctor explains)")
    log.info("starting %s", app.summary)
    try:
        await delete_webhook_if_polling(app.bot, cfg)
        await apply_identity(app.bot, cfg)
        if cfg.set_command_menu:
            # The menu is generated from the routers, so it can never list a command that
            # does not exist or omit one that does.
            from waifu.core.bot import set_command_menu
            from waifu.core.dp import command_menu

            await set_command_menu(app.bot, command_menu(), settings=cfg)
        if cfg.set_menu_button:
            await configure_menu_button(app.bot, cfg)
        await app.startup()
        # One resident loop for every timer in the bot (spawns, expiries, settlements).
        # ``python -m waifu jobs --name <pass>`` runs the same code from cron instead, so a
        # deployment can drop this loop entirely by setting ``WAIFU_NO_JOBS=1``.
        runner: Any = None
        if not cfg.no_jobs:
            from waifu.core.jobs import JobRunner

            runner = JobRunner(app.ctx)
            await runner.start()

        # The mini-app JSON API runs in this process (see waifu/api): same engine, same
        # repositories, so the web view and the chat cannot disagree about a price.
        api_runner: Any = None
        if cfg.api_enabled:
            from waifu.api import serve as serve_api

            api_runner = await serve_api(app.ctx)

        # Keep-alive health server (the Videl pattern): free-tier platforms sleep a
        # *web* service that exposes no public endpoint, so polling mode gets a tiny
        # one on $PORT (``/health`` + a deep ``/healthz``). Webhook mode already
        # serves /healthz through its own app, so no second server there — and a
        # taken port degrades to a warning, never to a crashed bot.
        health_runner: Any = None
        if cfg.mode == "polling" and cfg.health_enabled:
            from waifu.core.health import resolve_port
            from waifu.core.health import serve as serve_health

            health_runner = await serve_health(app.ctx, cfg.health_host, resolve_port(cfg))

        await _serve(app, cfg)
    finally:
        if health_runner is not None:
            from waifu.core.health import stop as stop_health

            await stop_health(health_runner)
        if api_runner is not None:
            await api_runner.cleanup()
        if runner is not None:
            await runner.stop()
        await app.shutdown()


async def _serve(app: BuiltApp, cfg: Settings) -> None:
    """The blocking loop (webhooks or long polling) with owner-log bookends.

    The owner's log channel gets a "started" line with the full capability
    summary, and a "stopped" line — or a "crashed" one first — on the way out,
    so a silent deploy or a 3 a.m. death is visible in the channel, not only in
    a web log nobody reads.
    """
    from waifu.tg.rich import rich_log
    from waifu.utils.time import now_utc

    stamp = now_utc().strftime("%Y-%m-%d %H:%M UTC")
    await _owner_log(
        app,
        f"🟢 {cfg.bot_title} started · v{__version__} · Bot API {__api_version__} · "
        f"{stamp} · {app.summary}",
        rich=rich_log(
            f"🟢 {cfg.bot_title} started",
            app.summary,
            detail=f"v{__version__} · Bot API {__api_version__} · {stamp}",
        ),
    )
    crashed = False
    try:
        if cfg.mode == "webhook":
            done = await _run_webhooks(app, cfg)
            await done.wait()
        else:
            # ``start_polling`` with an explicit ``allowed_updates`` instead of
            # run_polling's default list: Summon-bot's allowed_updates omitted
            # chat_member/reactions/poll_answer/pre_checkout, which silently
            # disabled half of its own feature set.
            await app.dp.start_polling(
                app.bot,
                allowed_updates=list(_ALL_UPDATES),
                drop_pending_updates=cfg.drop_pending_updates,
                handle_signals=True,
            )
    except Exception as exc:
        crashed = True
        log.exception("bot run failed: %s", exc)
        await _owner_log(
            app,
            f"💥 {cfg.bot_title} crashed: {type(exc).__name__}: {str(exc)[:300]}",
            rich=rich_log(
                f"💥 {cfg.bot_title} crashed",
                type(exc).__name__,
                detail=str(exc)[:1000],
            ),
        )
        raise
    finally:
        await _owner_log(
            app,
            f"🔴 {cfg.bot_title} {'stopped after a crash' if crashed else 'stopped'} "
            f"after {app.ctx.uptime_text}",
            rich=rich_log(
                f"🔴 {cfg.bot_title} {'crashed — stopped' if crashed else 'stopped'}",
                f"ran for {app.ctx.uptime_text}",
            ),
        )


#: Every update type the bot can act on. Missing one here = a feature silently dead.
_ALL_UPDATES: tuple[str, ...] = (
    "message",
    "edited_message",
    "channel_post",
    "edited_channel_post",
    "business_connection",
    "business_message",
    "edited_business_message",
    "deleted_business_messages",
    "message_reaction",
    "message_reaction_count",
    "inline_query",
    "chosen_inline_result",
    "callback_query",
    "shipping_query",
    "pre_checkout_query",
    "purchased_paid_media",
    "poll",
    "poll_answer",
    "my_chat_member",
    "chat_member",
    "chat_join_request",
    "chat_boost",
    "removed_chat_boost",
    "subscription",
    "guest_message",
)


async def _run_webhooks(app: BuiltApp, cfg: Settings) -> asyncio.Event:
    """aiogram's webhook runner + our health endpoint on the same port."""
    from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
    from aiohttp import web

    done = asyncio.Event()
    # A self-signed local server needs a secret_token; api.telegram.org requires it
    # for HTTPS webhooks since API 6.x, so this is never optional in practice.
    await app.bot.set_webhook(
        url=cfg.webhook_url,
        secret_token=cfg.webhook_secret,
        drop_pending_updates=cfg.webhook_drop_pending_updates,
        allowed_updates=list(_ALL_UPDATES),
    )
    router = web.RouteTableDef()

    @router.get("/healthz")
    async def healthz(_request: web.Request) -> web.Response:
        db_health = await app.ctx.db.healthcheck()
        redis_health = await app.redis.healthcheck() if app.redis else {"redis": "disabled"}
        healthy = bool(db_health.get("ok", True))
        return web.json_response(
            {"ok": healthy, "db": db_health, "redis": redis_health, "uptime": app.ctx.uptime_text},
            status=200 if healthy else 503,
        )

    application = web.Application()
    application.add_routes(router)
    SimpleRequestHandler(dispatcher=app.dp, secret_token=cfg.webhook_secret).register_application(
        application, path="/webhook"
    )
    setup_application(application, dispatcher=app.dp)
    runner = web.AppRunner(application)
    await runner.setup()
    site = web.TCPSite(runner, cfg.webhook_listen_host, cfg.webhook_listen_port)
    await site.start()
    log.info(
        "webhooks on %s:%s → %s", cfg.webhook_listen_host, cfg.webhook_listen_port, cfg.webhook_url
    )

    def _stop(*_a: Any) -> None:
        done.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:  # pragma: no cover - Windows
            pass
    return done


def run_sync(**kwargs: Any) -> None:
    """Synchronous entry point for the console script."""
    asyncio.run(run(**kwargs))


__all__ = ["_ALL_UPDATES", "BuiltApp", "build_app", "build_storage", "run", "run_sync"]
