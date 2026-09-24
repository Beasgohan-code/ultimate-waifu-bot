"""The JSON API beside the bot — Summon-bot's ``api.py``, ported to aiohttp.

The reference deployment ran a small Flask service next to the bot for its web front-end.
``api.py`` is gone from the source tree — ``__pycache__/api.cpython-313.pyc`` is what proves it
existed, and its 16 functions (routes, ``get_db``, ``row_to_dict``, ``validate_telegram_data``,
``rarity_emoji``, ``rarity_price``) are the specification this module implements. The endpoint
set is reproduced one for one, in the same shapes, because that is what a front-end speaks::

    GET  /api/health
    GET  /api/user/<telegram_id>            GET /api/inventory/<telegram_id>
    GET  /api/characters                    GET /api/market
    GET  /api/leaderboard                   GET /api/achievements/<telegram_id>
    GET  /api/streak/<telegram_id>          POST /api/daily/<telegram_id>
    POST /api/summon/<telegram_id>

Three differences are deliberate, and all three follow from the service being reachable from a
browser:

* **identity is proven, not typed.** Everything but ``/api/health`` needs a valid
  ``X-Init-Data`` signature (:mod:`waifu.api.auth`), and the ``telegram_id`` in the path has to
  *equal* that identity. The reference read ``?uid=`` from anyone, which turned its
  ``/api/summon`` and ``/api/daily`` into a way to spend another player's coins;
* **money moves on POST.** Both mutating routes were GETs upstream, so a browser prefetch, a
  link scanner or a retry-on-disconnect could charge a summon;
* **status codes mean something** — ``401`` unauthenticated, ``403`` someone else's account,
  ``404`` no such player, ``402`` not enough coins, ``409`` already claimed, ``503`` empty
  roster — while the reference's short error *keys* stay in the body, so a front-end matching
  on ``insufficient_balance`` still works.

It runs in the bot's process on purpose (``python -m waifu api``, or ``API_ENABLED=1`` inside
``waifu bot``): the JSON is a projection of the same tables, and a second service would need its
own migrations — which is how the reference's prices and chances drifted apart in the first
place. Handlers read through the same repositories the chat commands do, so an API response and
a card can never disagree.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aiohttp import web

from waifu.api.auth import (
    API_TOKEN_HEADER,
    INIT_DATA_HEADER,
    InitDataRejected,
    identity_from_headers,
)
from waifu.db.repo import characters as char_repo
from waifu.db.repo import collection as collection_repo
from waifu.db.repo import progress as progress_repo
from waifu.db.repo import users as user_repo
from waifu.enums import Rarity
from waifu.errors import AlreadyClaimed, NotEnoughFunds, WaifuError
from waifu.logging import get_logger
from waifu.utils.text import esc

if TYPE_CHECKING:  # pragma: no cover
    from waifu.core.context import AppContext

log = get_logger("api")

#: The only route reachable without proof of who you are.
PUBLIC_PATHS = frozenset({"/api/health"})
#: aiohttp asks for typed keys for app/request storage (string keys collide with headers).
CTX_KEY: web.AppKey[AppContext] = web.AppKey("ctx")
SESSION_KEY: web.RequestKey[Any] = web.RequestKey("session")
WHO_KEY: web.RequestKey[int] = web.RequestKey("user_id")
#: A page listing 100 characters is a picker, not a scrape; the reference hard-coded 12/25.
MAX_PAGE = 200


def _int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _iso(value: Any) -> str:
    if value is None or value == "":
        return ""
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _character_payload(row: Any, *, count: int | None = None) -> dict[str, Any]:
    """One character, in the reference's field names.

    ``msg_id`` was where the old schema crammed a Telegram file id (``"photo_AgAC…"``); the
    field is kept, filled from the typed columns, so an existing front-end still renders art.
    """
    rarity_id = _int(getattr(row, "rarity_id", 0))
    display = str(getattr(row, "rarity", "") or "") or Rarity.from_value(rarity_id).display
    identifier = _int(getattr(row, "id", 0) or getattr(row, "character_id", 0))
    payload: dict[str, Any] = {
        "id": identifier,
        # The roster's own numbering, which is how admins quote a character (``07``).
        "ref": f"{identifier:02d}",
        "name": row.name,
        "anime": row.anime or "",
        "rarity": display,
        "rarity_id": rarity_id,
        "msg_id": str(getattr(row, "photo_file_id", "") or getattr(row, "video_file_id", "") or ""),
        "image_url": str(getattr(row, "image_url", "") or getattr(row, "image", "") or ""),
        "price": _int(getattr(row, "price", 0)),
        "stat_power": _int(getattr(row, "stat_power", 0)),
    }
    if count is not None:
        payload["count"] = int(count)
    return payload


@web.middleware
async def auth_middleware(request: web.Request, handler: Any) -> web.Response:
    """Prove who is calling before any handler runs, and open one session per request.

    A CORS preflight is exempt (a browser cannot attach custom headers to it), and so is
    ``/api/health`` — everything else needs a signed ``initData`` whose user matches the id in
    the path, which is the whole difference between this and the reference's ``?uid=`` free-for-all.
    """
    ctx: AppContext = request.app[CTX_KEY]
    settings = ctx.settings
    gate = request.method != "OPTIONS" and request.path not in PUBLIC_PATHS
    async with ctx.db.tx() as session:
        request[SESSION_KEY] = session
        if gate:
            try:
                who = identity_from_headers(
                    request.headers,
                    bot_token=settings.bot_token,
                    api_token=str(settings.webapp_secret_key or ""),
                    query_uid=request.query.get("uid", ""),
                    allow_uid_query=bool(settings.api_allow_uid_query),
                    raw_init_data=request.headers.get(INIT_DATA_HEADER)
                    or request.query.get("tgData", ""),
                    max_age=_int(settings.api_init_data_max_age, 86400) or None,
                )
            except InitDataRejected as exc:
                return await _finish(
                    request,
                    web.json_response(
                        {"error": "invalid_init_data", "detail": str(exc)}, status=401
                    ),
                    settings,
                )
            if who is None:
                return await _finish(
                    request,
                    web.json_response(
                        {"error": "authentication_required", "header": INIT_DATA_HEADER}, status=401
                    ),
                    settings,
                )
            path_id = request.match_info.get("telegram_id") if request.match_info else None
            if path_id is not None and _int(path_id) != int(who):
                return await _finish(
                    request, web.json_response({"error": "not_your_account"}, status=403), settings
                )
            request[WHO_KEY] = int(who)
        return await _finish(request, await handler(request), settings)


async def _finish(request: web.Request, response: web.Response, settings: Any) -> web.Response:
    """Headers every response needs, in one place.

    ``no-store`` because a player's balance sitting in a shared proxy cache is somebody
    else's balance; the CORS origin is pinned to ``WEBAPP_URL`` rather than ``*``, for the
    same reason.
    """
    response.headers.setdefault("Cache-Control", "no-store")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    origin = str(request.headers.get("Origin", ""))
    allowed = str(settings.webapp_url or "").rstrip("/")
    if origin and allowed and origin == allowed:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
    return response


async def health(request: web.Request) -> web.Response:
    """``/api/health`` — the reference's ``os.path.exists(DB_PATH)`` plus what the DB says."""
    ctx: AppContext = request.app[CTX_KEY]
    info = await ctx.db.healthcheck()
    return web.json_response({"ok": info.get("db") == "ok", **info, "utc": _utc_iso()})


def _utc_iso() -> str:
    from waifu.utils.time import now_utc

    return now_utc().isoformat(timespec="seconds")


async def get_user(request: web.Request) -> web.Response:
    session = request[SESSION_KEY]
    user_id = _int(request.match_info["telegram_id"])
    user = await user_repo.get(session, user_id)
    if user is None:
        return web.json_response({"error": "user_not_found"}, status=404)
    streak = await progress_repo.streak(session, user_id)
    owned, _total = await collection_repo.list_owned(session, user_id, page_size=MAX_PAGE)
    return web.json_response(
        {
            "user_id": user_id,
            "username": user.username or "",
            "first_name": esc(user.first_name or ""),
            "display_name": user.first_name or (f"@{user.username}" if user.username else "User"),
            "balance": _int(user.balance),
            "level": _int(user.level, 1),
            "last_daily": _iso(user.last_daily),
            # The reference returned its ``user_streaks`` row; the fields keep those names.
            "streak": {
                "streak_count": _int(streak.current),
                "highest_streak": _int(streak.highest),
                "last_streak_date": str(streak.last_date or ""),
                "freezes": _int(streak.freezes),
            },
            "unique_characters": len(owned),
            "total_characters": sum(int(entry.count) for entry in owned),
            "collection_value": sum(int(entry.value) for entry in owned),
        }
    )


async def get_inventory(request: web.Request) -> web.Response:
    """``/api/inventory/<id>`` — the harem, newest first (the reference's ``ORDER BY obtained_at``)."""
    session = request[SESSION_KEY]
    user_id = _int(request.match_info["telegram_id"])
    limit = min(MAX_PAGE, max(1, _int(request.query.get("limit"), 50)))
    page = max(0, _int(request.query.get("page"), 0))
    items, total = await collection_repo.list_owned(
        session, user_id, page=page, page_size=limit, query=request.query.get("q", "") or ""
    )
    return web.json_response(
        {
            "items": [_character_payload(entry, count=entry.count) for entry in items],
            "count": int(total),
            "page": page,
            "value": sum(int(entry.value) for entry in items),
        }
    )


async def get_characters(request: web.Request) -> web.Response:
    """``/api/characters`` — the roster, paged and searchable (empty query lists all)."""
    session = request[SESSION_KEY]
    limit = min(MAX_PAGE, max(1, _int(request.query.get("limit"), 50)))
    page = max(0, _int(request.query.get("page"), 0))
    rows, total = await char_repo.search(
        session,
        request.query.get("q", "") or "",
        rarity_id=_int(request.query.get("rarity"), 0) or None,
        limit=limit,
        offset=page * limit,
    )
    return web.json_response(
        {
            "items": [_character_payload(row) for row in rows],
            "count": int(total),
            "limit": limit,
            "page": page,
        }
    )


async def get_market(request: web.Request) -> web.Response:
    """``/api/market`` — what the shop will sell right now, priced the same way it prices."""
    session = request[SESSION_KEY]
    rows, _total = await char_repo.search(
        session, "", limit=min(MAX_PAGE, max(1, _int(request.query.get("limit"), 12)))
    )
    items = [
        {**_character_payload(row), "price": _int(await char_repo.price_for(session, row))}
        for row in rows
    ]
    return web.json_response({"items": items, "count": len(items)})


async def leaderboard(request: web.Request) -> web.Response:
    """``/api/leaderboard`` — top 25 by balance, the reference's default and its metric."""
    session = request[SESSION_KEY]
    limit = min(MAX_PAGE, max(1, _int(request.query.get("limit"), 25)))
    rows = await user_repo.leaderboard(session, metric="balance", limit=limit)
    return web.json_response(
        {
            "items": [
                {
                    "rank": index,
                    "user_id": int(user.id),
                    "display_name": user.first_name
                    or (f"@{user.username}" if user.username else "User"),
                    "username": user.username or "",
                    "balance": _int(user.balance),
                }
                for index, (user, _score) in enumerate(rows, start=1)
            ],
            "count": len(rows),
        }
    )


async def get_streak(request: web.Request) -> web.Response:
    session = request[SESSION_KEY]
    streak = await progress_repo.streak(session, _int(request.match_info["telegram_id"]))
    return web.json_response(
        {
            "streak_count": _int(streak.current),
            "highest_streak": _int(streak.highest),
            "last_streak_date": str(streak.last_date or ""),
            "freezes": _int(streak.freezes),
        }
    )


async def get_achievements(request: web.Request) -> web.Response:
    """``/api/achievements/<id>`` — unlocked rows, most recent first (the reference's order)."""
    ctx: AppContext = request.app[CTX_KEY]
    user_id = _int(request.match_info["telegram_id"])
    rows = await ctx.progress.list_for(request[SESSION_KEY], user_id)
    return web.json_response(
        {
            "items": [
                {
                    "achievement_id": getattr(row, "achievement_id", ""),
                    "progress": _int(getattr(row, "progress", 0)),
                    "unlocked_at": _iso(getattr(row, "unlocked_at", None)),
                }
                for row in rows
            ],
            "count": len(rows),
        }
    )


async def daily_claim(request: web.Request) -> web.Response:
    """``POST /api/daily/<id>`` — a GET upstream; claiming on a prefetch is not a feature."""
    ctx: AppContext = request.app[CTX_KEY]
    try:
        result = await ctx.economy.daily(
            request[SESSION_KEY], _int(request.match_info["telegram_id"])
        )
    except AlreadyClaimed as exc:
        return web.json_response(
            {"error": "already_claimed", "detail": exc.user_message}, status=409
        )
    return web.json_response(
        {
            "ok": True,
            "reward": int(result.amount),
            "balance": int(result.balance),
            "streak": int(result.streak),
            "highest_streak": int(result.best),
            "multiplier": float(result.multiplier),
            "next_reset": str(result.next_reset),
            "xp": int(result.xp),
            "bonus_item": result.bonus_item,
        }
    )


async def summon(request: web.Request) -> web.Response:
    """``POST /api/summon/<id>`` — one paid pull through the same service ``/pull`` uses.

    ``?ten=1`` for a ten pull; the commitment hash and sequence number come back with the
    rolls, because a web front-end that cannot show ``/verify`` material is decoration.
    """
    ctx: AppContext = request.app[CTX_KEY]
    user_id = _int(request.match_info["telegram_id"])
    batch = 10 if str(request.query.get("ten", "")).lower() in {"1", "true", "yes"} else 1
    totals = await char_repo.totals(request[SESSION_KEY])
    if not totals.get("characters"):
        return web.json_response({"error": "no_characters"}, status=503)
    try:
        result = await ctx.gacha.pull(request[SESSION_KEY], user_id, batch=batch, cooldown_key=None)
    except NotEnoughFunds as exc:
        return web.json_response(
            {"error": "insufficient_balance", "detail": exc.user_message}, status=402
        )
    except WaifuError as exc:
        return web.json_response({"error": exc.user_message}, status=400)
    rolls = [
        {
            "id": int(roll.character_id),
            "name": roll.name,
            "anime": roll.anime,
            "rarity": Rarity.from_value(int(roll.rarity)).display,
            "rarity_id": int(roll.rarity),
            "is_dupe": bool(roll.is_dupe),
            "payout": int(roll.payout),
            "stat_power": int(roll.stat_power),
            "image": roll.image,
        }
        for roll in result.rolls
    ]
    return web.json_response(
        {
            "ok": True,
            "character": rolls[0] if rolls else None,
            "items": rolls,
            "spent": int(result.spent),
            "dupe_payout": int(result.dupe_payout),
            "balance": int(result.balance),
            "new_count": int(result.new_count),
            "commitment": result.commitment,
            "sequence": int(result.sequence),
        }
    )


async def preflight(request: web.Request) -> web.Response:
    """CORS for the configured Mini App origin only — ``*`` would let any page read a harem."""
    ctx: AppContext = request.app[CTX_KEY]
    allowed = str(ctx.settings.webapp_url or "").rstrip("/")
    origin = str(request.headers.get("Origin", ""))
    if not allowed or origin != allowed:
        return web.Response(status=403)
    return web.Response(
        headers={
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Headers": f"{INIT_DATA_HEADER}, {API_TOKEN_HEADER}, Content-Type",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Max-Age": "600",
            "Vary": "Origin",
        }
    )


#: ``path -> handler``, POST for the routes that spend money.
ROUTES: tuple[tuple[str, Any, str], ...] = (
    ("/api/health", health, "GET"),
    ("/api/user/{telegram_id:\\d+}", get_user, "GET"),
    ("/api/inventory/{telegram_id:\\d+}", get_inventory, "GET"),
    ("/api/characters", get_characters, "GET"),
    ("/api/market", get_market, "GET"),
    ("/api/leaderboard", leaderboard, "GET"),
    ("/api/achievements/{telegram_id:\\d+}", get_achievements, "GET"),
    ("/api/streak/{telegram_id:\\d+}", get_streak, "GET"),
    ("/api/daily/{telegram_id:\\d+}", daily_claim, "POST"),
    ("/api/summon/{telegram_id:\\d+}", summon, "POST"),
)


def build_app(ctx: AppContext) -> web.Application:
    """The aiohttp app, sharing the bot's :class:`AppContext` — one process, one schema."""
    app = web.Application(middlewares=[auth_middleware])
    app[CTX_KEY] = ctx
    for path, handler, method in ROUTES:
        app.router.add_route(method, path, handler)
    for path, _handler, _method in ROUTES:
        app.router.add_route("OPTIONS", path, preflight)
    log.info(
        "api: %d routes, auth via %s, ?uid= %s",
        len(ROUTES),
        INIT_DATA_HEADER,
        "allowed (dev)" if getattr(ctx.settings, "api_allow_uid_query", False) else "refused",
    )
    return app


async def serve(ctx: AppContext, *, host: str = "", port: int = 0) -> web.AppRunner:
    """Start the API and hand back its runner — the caller owns shutdown."""
    settings = ctx.settings
    runner = web.AppRunner(build_app(ctx), access_log=None)
    await runner.setup()
    bind_host = host or str(settings.api_host)
    bind_port = _int(port or settings.api_port, 8080)
    site = web.TCPSite(runner, bind_host, bind_port)
    await site.start()
    log.info(
        "api on http://%s:%s (health: /api/health; identity: %s; POST for daily and summon)",
        bind_host,
        bind_port,
        INIT_DATA_HEADER,
    )
    return runner


__all__ = ["ROUTES", "build_app", "serve"]
