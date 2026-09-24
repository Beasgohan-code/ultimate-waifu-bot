"""Keep-alive health server — the Videl pattern for free-tier platforms.

Render/Koyeb/Railway free *web* services sleep after idle and uptime monitors
(UptimeRobot & co.) need a cheap URL to poke, so Videl ships a tiny server
(``videl/core/server.py``) answering ``/`` and ``/health`` with status + uptime
on the platform's ``$PORT``. This module is the same shape, adapted to this
codebase:

* ``GET /`` and ``GET /health`` — the uptime-bot answer: JSON, always 200 while
  the process is up, no database access (a wedged DB must not 503 the keep-alive,
  or the platform sleeps the bot and the DB problem becomes "bot is offline").
* ``GET /healthz`` — the *deep* check (database + redis, 503 when unhealthy) —
  the same endpoint the webhook mode already exposes, so one server serves both
  the uptime bot and a human debugging a deployment.

It is aiohttp, not Werkzeug, on purpose: this process already owns an asyncio
loop (bot, jobs, cache). A synchronous WSGI server would mean a worker thread
and an event-loop hop for every health check, and Videl's own implementation is
aiohttp too. The server never takes the bot down: if the port is taken (the
JSON API claims 8080 by default) it logs and continues without it.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from aiohttp import web

from waifu import __api_version__, __version__
from waifu.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from waifu.core.context import AppContext
    from waifu.settings import Settings

log = get_logger("core.health")

DEFAULT_PORT = 8080

_CTX = web.AppKey("ctx", "AppContext")


def resolve_port(settings: Settings) -> int:
    """``HEALTH_PORT`` → platform ``$PORT`` → 8080.

    ``settings.health_port`` already honours ``HEALTH_PORT`` *and* ``PORT`` via
    its validation alias, so this only fills the 0 = "unset" case.
    """
    if settings.health_port:
        return int(settings.health_port)
    raw = (os.environ.get("PORT", "") or "").strip()
    return int(raw) if raw.isdigit() else DEFAULT_PORT


def build_app(ctx: AppContext) -> web.Application:
    application = web.Application()
    application[_CTX] = ctx
    application.router.add_get("/", _simple)
    application.router.add_get("/health", _simple)
    application.router.add_get("/healthz", _detailed)
    return application


async def _simple(request: web.Request) -> web.Response:
    ctx: AppContext = request.app[_CTX]
    return web.json_response(
        {
            "status": "healthy",
            "bot": ctx.settings.bot_title,
            "version": __version__,
            "api_version": __api_version__,
            "uptime_seconds": ctx.uptime_seconds,
        }
    )


async def _detailed(request: web.Request) -> web.Response:
    """DB + redis check; 503 when the data layer is down (the webhook's /healthz)."""
    ctx: AppContext = request.app[_CTX]
    try:
        db_health = await ctx.db.healthcheck()
    except Exception as exc:  # pragma: no cover - an error here *is* the 503
        db_health = {"db": f"error: {exc}"}
    redis_health = await ctx.redis.healthcheck() if ctx.redis else {"redis": "disabled"}
    healthy = bool(db_health.get("db") == "ok" or db_health.get("ok", True))
    return web.json_response(
        {
            "status": "healthy" if healthy else "degraded",
            "db": db_health,
            "redis": redis_health,
            "uptime_seconds": ctx.uptime_seconds,
        },
        status=200 if healthy else 503,
    )


async def serve(ctx: AppContext, host: str, port: int) -> web.AppRunner | None:
    """Start the keep-alive server; ``None`` (with a log line) when it cannot bind."""
    runner = web.AppRunner(build_app(ctx))
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    try:
        await site.start()
    except Exception as exc:
        # The keep-alive is a convenience, not a dependency: a taken port (the
        # JSON API defaults to 8080 too) must not take the bot down with it.
        log.warning(
            "keep-alive server could not bind %s:%s: %s — continuing without it", host, port, exc
        )
        # …on a free-tier *web* service it is not a convenience: the platform
        # scans for the advertised port and sleeps a process that never opens it.
        log.error(
            "no port is open — a web-service platform may sleep this process; "
            "set HEALTH_PORT to a free port (or check what is already using %s)",
            port,
        )
        await runner.cleanup()
        return None
    log.info("keep-alive server on %s:%s (/, /health, /healthz)", host, port)
    return runner


async def stop(runner: web.AppRunner | None) -> None:
    if runner is not None:
        try:
            await runner.cleanup()
        except Exception as exc:  # pragma: no cover - shutdown noise
            log.debug("keep-alive server cleanup: %s", exc)


__all__ = ["build_app", "resolve_port", "serve", "stop"]
