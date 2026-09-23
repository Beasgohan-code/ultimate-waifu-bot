"""Mini-App request authentication — the part of the reference's ``api.py`` worth copying exactly.

The reference bot ran a Flask service next to the bot (deleted from the source tree, still
present in the deployment's compiled cache) that served JSON for its web front-end: user,
inventory, market, leaderboard, and two mutating routes. Its one piece of real security was
``validate_telegram_data`` — Telegram's ``initData`` HMAC check — and the algorithm is short
enough to reproduce faithfully::

    data_check_string = "\\n".join(f"{k}={unquote(v)}" for k, v in sorted(params.items()))
    secret_key = HMAC_SHA256(key="WebAppData", msg=BOT_TOKEN)
    valid = HMAC_SHA256(key=secret_key, msg=data_check_string).hexdigest() == params["hash"]

That is <https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app>.

What is added here is not decoration, it is the two ways this check gets bypassed in the wild:

* ``hmac.compare_digest`` instead of ``==`` (a timing oracle on a public port is a real one);
* an ``auth_date`` staleness window, because a captured ``initData`` string is valid forever
  otherwise — a link in a chat log is then a permanent key to someone's account;
* the ``?uid=`` fallback the reference accepted *unconditionally* (any client could read or
  spend anyone's balance) is here behind ``API_ALLOW_UID_QUERY``, and refused outright unless
  ``WAIFU_TEST_MODE`` is set or a shared ``API_TOKEN`` header matches.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any
from urllib.parse import parse_qsl, unquote

__all__ = ["InitDataRejected", "identity_from_headers", "sign_init_data", "validate_init_data"]

#: The reference read the signed payload from this header; the webapp front-end sends it.
INIT_DATA_HEADER = "X-Init-Data"
#: Header for the optional shared secret (deployment behind a proxy, or a dev with no Mini App).
API_TOKEN_HEADER = "X-API-Token"  # noqa: S105 - a header name, not a secret

#: One day: long enough for a slow phone, short enough that a leaked link expires.
DEFAULT_MAX_AGE = 24 * 60 * 60


class InitDataRejected(ValueError):
    """The payload is not a signature this bot's token could have produced."""


def _data_check_string(params: dict[str, str]) -> str:
    return "\n".join(f"{key}={unquote(value)}" for key, value in sorted(params.items()))


def _secret(bot_token: str) -> bytes:
    return hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()


def sign_init_data(params: dict[str, str], bot_token: str) -> str:
    """Produce the ``hash=…`` for ``params`` — the inverse of the check, for tests and tooling."""
    return hmac.new(
        _secret(bot_token), _data_check_string(params).encode(), hashlib.sha256
    ).hexdigest()


def validate_init_data(
    init_data: str,
    bot_token: str,
    *,
    max_age: int | None = DEFAULT_MAX_AGE,
) -> dict[str, Any]:
    """Parse and verify a Mini App ``initData`` string; return its payload.

    Raises :class:`InitDataRejected` for anything that is not provably signed by ``bot_token``
    — including a payload whose ``auth_date`` is older than ``max_age`` seconds.
    """
    if not init_data or not bot_token:
        raise InitDataRejected("no init data")
    params = dict(parse_qsl(init_data, keep_blank_values=True))
    received = params.pop("hash", "")
    if not received:
        raise InitDataRejected("missing hash")
    expected = sign_init_data(params, bot_token)
    if not hmac.compare_digest(expected, received):
        raise InitDataRejected("bad signature")
    if max_age is not None:
        raw_auth_date = params.get("auth_date", "")
        try:
            auth_date = int(float(raw_auth_date))
        except (TypeError, ValueError):
            raise InitDataRejected("unreadable auth_date") from None
        if time.time() - auth_date > max_age:
            raise InitDataRejected("stale init data")
    payload: dict[str, Any] = dict(params)
    # ``user``/``chat``/``start_param`` arrive as JSON strings inside the query.
    for key in ("user", "chat", "receiver_chat"):
        raw = params.get(key)
        if raw:
            try:
                payload[key] = json.loads(raw)
            except ValueError:
                raise InitDataRejected(f"malformed {key} json") from None
    return payload


def identity_from_headers(
    headers: Any,
    *,
    bot_token: str,
    api_token: str = "",
    query_uid: str = "",
    allow_uid_query: bool = False,
    raw_init_data: str = "",
    max_age: int | None = DEFAULT_MAX_AGE,
) -> int | None:
    """Whose account is this request about? ``None`` means "not proven".

    Order matters: a signed ``X-Init-Data`` (or its ``?tgData=`` twin) always wins; the
    shared-secret header is the documented escape hatch for a front-end that is not a Mini
    App (it authenticates the *caller*, so it still needs an id); the bare ``?uid=`` is a
    development convenience and only honoured when the operator asked for it.
    """
    get = headers.get if hasattr(headers, "get") else {}.get
    # A Mini App sends the header; a page opened as a plain link can only pass a query value,
    # and both are useless to an attacker without the bot token, so either is accepted.
    raw = raw_init_data or str(get(INIT_DATA_HEADER) or "")
    if raw:
        payload = validate_init_data(str(raw), bot_token, max_age=max_age)
        user = payload.get("user")
        if isinstance(user, dict) and str(user.get("id", "")).isdigit():
            return int(user["id"])
        raise InitDataRejected("signed payload carries no user id")
    if api_token and hmac.compare_digest(str(get(API_TOKEN_HEADER) or ""), api_token):
        return int(query_uid) if str(query_uid).isdigit() else None
    if allow_uid_query and str(query_uid).isdigit():
        return int(query_uid)
    return None
