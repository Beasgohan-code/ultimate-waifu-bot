"""The mini-app JSON API: signature-gated, own-account-only, and POST for anything that spends.

``api.py`` in the reference deployment was a Flask service with a real ``initData`` HMAC check
and an unauthenticated ``?uid=`` back door next to it — you could read anyone's balance and
summon with anyone's coins. These tests pin both halves: the crypto is the documented one, and
the back door is closed by default.
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from waifu.api import sign_init_data, validate_init_data
from waifu.api.auth import InitDataRejected
from waifu.api.server import ROUTES, build_app
from waifu.db.repositories import economy as ledger
from waifu.db.repositories import users as user_repo

TOKEN = "123456:TEST-token-not-a-real-bot"  # noqa: S105 - not a secret, the fixture token


# ------------------------------------------------------------------ initData crypto
def _signed(payload: dict[str, Any] | None = None, token: str = TOKEN) -> str:
    """A correctly signed ``initData`` string, the way Telegram's own client builds one."""
    body: dict[str, str] = {
        "auth_date": str(int(time.time())),
        "user": json.dumps({"id": 4242, "first_name": "Test", "username": "tester"}),
    }
    body.update(
        {
            key: value if isinstance(value, str) else json.dumps(value, separators=(",", ":"))
            for key, value in (payload or {}).items()
        }
    )
    body["user"] = json.dumps(
        json.loads(body["user"]) if body["user"].startswith("{") else body["user"],
        separators=(",", ":"),
    )
    query = "&".join(f"{key}={value}" for key, value in body.items())
    return f"{query}&hash={sign_init_data(body, token)}"


def test_signed_init_data_round_trips() -> None:
    data = _signed({"start_param": "harem"})
    payload = validate_init_data(data, TOKEN)
    assert payload["user"]["id"] == 4242
    assert payload["start_param"] == "harem"


def test_tampering_with_any_field_invalidates_the_signature() -> None:
    data = _signed()
    swapped = data.replace('"id":4242', '"id":9999')
    assert swapped != data
    with pytest.raises(InitDataRejected):
        validate_init_data(swapped, TOKEN)


@pytest.mark.parametrize(
    "data",
    [
        "",
        "user=%7B%22id%22%3A1%7D",  # no hash at all
        "hash=&user=%7B%22id%22%3A1%7D",
        f"user=%7B%22id%22%3A1%7D&auth_date=1&hash={'0' * 64}",
    ],
)
def test_garbage_is_rejected_not_crashed(data: str) -> None:
    with pytest.raises(InitDataRejected):
        validate_init_data(data, TOKEN)


def test_stale_init_data_is_rejected() -> None:
    old = int(time.time()) - 48 * 3600
    body = {"user": json.dumps({"id": 4242}), "auth_date": str(old)}
    data = f"user={body['user']}&auth_date={old}&hash={sign_init_data(body, TOKEN)}"
    with pytest.raises(InitDataRejected, match="stale"):
        validate_init_data(data, TOKEN)
    # and an operator who knows what they are doing can allow it
    assert validate_init_data(data, TOKEN, max_age=None)["user"]["id"] == 4242


# ------------------------------------------------------------------------- the routes
@pytest.fixture
async def client(ctx, monkeypatch):
    """A live HTTP client over the same fixture database the chat tests use."""
    app = build_app(ctx)
    instance = TestClient(TestServer(app))
    await instance.start_server()
    yield instance
    await instance.close()


def _headers(payload: dict[str, Any] | None = None) -> dict[str, str]:
    return {"X-Init-Data": _signed(payload)}


async def test_health_is_public_and_reports_the_roster(client) -> None:
    response = await client.get("/api/health")
    assert response.status == 200
    body = await response.json()
    assert body["ok"] is True and body["characters"] >= 0 and body["tiers"] == 18


async def test_tgdata_query_param_is_accepted_like_the_header(client, player) -> None:
    """A Mini App opens with ``?tgData=`` when the page cannot set headers yet (first paint)."""
    from urllib.parse import quote

    signed = quote(_signed(), safe="")
    ok = await client.get(f"/api/user/{player}?tgData={signed}")
    assert ok.status == 200, await ok.text()
    forged = await client.get(
        f"/api/user/{player}?tgData={quote(_signed()[:-4] + '0000', safe='')}"
    )
    assert forged.status == 401
    assert (await forged.json())["error"] == "invalid_init_data"


async def test_everything_else_needs_proof(client) -> None:
    for path in ["/api/user/4242", "/api/inventory/4242", "/api/streak/4242"]:
        response = await client.get(path)
        assert response.status == 401, path
        assert (await response.json())["error"] == "authentication_required"


async def test_signature_opens_the_door_and_the_path_must_match(client, player) -> None:
    ok = await client.get(f"/api/user/{player}", headers=_headers())
    assert ok.status == 200
    body = await ok.json()
    assert body["user_id"] == 4242 and "streak" in body and "balance" in body
    # a valid signature for *me* does not read someone else
    other = await client.get("/api/user/7", headers=_headers())
    assert other.status == 403
    assert await other.json() == {"error": "not_your_account"}


async def test_uid_query_is_refused_until_the_operator_allows_it(ctx, monkeypatch, player) -> None:
    """The reference's back door, behind a flag, off by default, loud when on."""
    from aiohttp.test_utils import TestClient as _TestClient

    monkeypatch.setattr(
        ctx, "settings", ctx.settings.model_copy(update={"api_allow_uid_query": True})
    )
    client = _TestClient(TestServer(build_app(ctx)))
    await client.start_server()
    try:
        assert (await client.get(f"/api/user/{player}?uid={player}")).status == 200
        assert (await client.get(f"/api/user/{player}")).status == 401
    finally:
        await client.close()

    monkeypatch.setattr(
        ctx, "settings", ctx.settings.model_copy(update={"api_allow_uid_query": False})
    )
    locked = _TestClient(TestServer(build_app(ctx)))
    await locked.start_server()
    try:
        assert (await locked.get(f"/api/user/{player}?uid={player}")).status == 401
    finally:
        await locked.close()


async def test_inventory_and_leaderboard_are_pages_of_counts(client) -> None:
    headers = _headers()
    inventory = await client.get("/api/inventory/4242", headers=headers)
    body = await inventory.json()
    assert body == {"items": [], "count": 0, "page": 0, "value": 0}

    top = await client.get("/api/leaderboard", headers=headers)
    rows = (await top.json())["items"]
    assert isinstance(rows, list) and all(
        {"rank", "user_id", "balance"} <= set(row) for row in rows
    )


async def test_characters_lists_the_roster_with_the_reference_fields(client) -> None:
    headers = _headers()
    response = await client.get("/api/characters?limit=3", headers=headers)
    body = await response.json()
    assert body["limit"] == 3 and len(body["items"]) <= 3
    first = body["items"][0]
    assert {"id", "ref", "name", "anime", "rarity", "msg_id", "image_url", "price"} <= set(first)
    assert first["ref"] == f"{first['id']:02d}"


async def test_mutating_routes_are_post_only(client) -> None:
    headers = _headers()
    for path in ("/api/daily/4242", "/api/summon/4242"):
        assert (await client.get(path, headers=headers)).status == 405, path


async def test_daily_and_summon_move_the_same_money_the_chat_does(client, ctx, tx, player) -> None:
    headers = _headers()
    async with ctx.db.tx() as session:
        before = await ledger.balance(session, player)
    daily = await client.post(f"/api/daily/{player}", headers=headers)
    assert daily.status == 200
    payload = await daily.json()
    assert payload["ok"] and payload["reward"] > 0 and payload["streak"] >= 1
    assert payload["balance"] == before + payload["reward"]
    # claiming twice in a day is a 409 with the reference's error key
    again = await client.post(f"/api/daily/{player}", headers=headers)
    assert again.status == 409
    assert (await again.json())["error"] == "already_claimed"

    pulled = await client.post(f"/api/summon/{player}", headers=headers)
    assert pulled.status == 200
    body = await pulled.json()
    assert body["character"]["name"] and body["commitment"]
    assert body["balance"] == body["spent"] + (await _balance(ctx, player)) - ctx.settings.pull_cost


async def _balance(ctx, user_id: int) -> int:
    async with ctx.db.tx() as session:
        return await ledger.balance(session, user_id)


async def test_broke_player_gets_the_reference_error_key(client, ctx, player) -> None:
    async with ctx.db.tx() as session:
        await ledger.debit(
            session, player, await ledger.balance(session, player) - 1, "test", reference="drain"
        )
    headers = _headers()
    response = await client.post(f"/api/summon/{player}", headers=headers)
    assert response.status == 402
    assert (await response.json())["error"] == "insufficient_balance"


async def test_new_player_row_is_404_not_an_invention(client) -> None:
    headers = _headers({"user": {"id": 123456}})
    response = await client.get("/api/user/123456", headers=headers)
    assert response.status == 404
    assert await response.json() == {"error": "user_not_found"}


async def test_preflight_is_origin_pinned(ctx, monkeypatch) -> None:
    monkeypatch.setattr(
        ctx, "settings", ctx.settings.model_copy(update={"webapp_url": "https://example.com/app"})
    )
    client = TestClient(TestServer(build_app(ctx)))
    await client.start_server()
    try:
        allowed = await client.options(
            "/api/characters", headers={"Origin": "https://example.com/app"}
        )
        refused = await client.options(
            "/api/characters", headers={"Origin": "https://evil.example"}
        )
        assert allowed.status == 200
        assert allowed.headers["Access-Control-Allow-Origin"] == "https://example.com/app"
        assert refused.status == 403
    finally:
        await client.close()


def test_route_table_matches_the_reference_surface() -> None:
    paths = {path for path, _handler, _method in ROUTES}
    assert {
        "/api/health",
        "/api/characters",
        "/api/market",
        "/api/leaderboard",
    } <= paths
    for named in ("user", "inventory", "achievements", "streak", "daily", "summon"):
        assert any(f"/api/{named}/" in path for path in paths), named
    assert user_repo  # imported for the shape of User; keeps the dependency explicit


async def test_nothing_here_is_cachable(client) -> None:
    """A player's balance behind a shared cache is someone else's balance."""
    health = await client.get("/api/health")
    assert health.headers.get("Cache-Control") == "no-store"
    signed = await client.get("/api/characters", headers=_headers())
    assert signed.headers.get("Cache-Control") == "no-store"
