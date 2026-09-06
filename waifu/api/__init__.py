"""The Mini-App / operator JSON API — Summon-bot's ``api.py``, ported and locked.

``build_app(ctx)`` returns an aiohttp application that shares the bot's engine, cache and
services; ``serve(ctx)`` binds it. Identity comes from Telegram's signed ``initData`` — see
:mod:`waifu.api.auth` — and every mutating route is a POST.
"""

from __future__ import annotations

from waifu.api.auth import (
    API_TOKEN_HEADER,
    INIT_DATA_HEADER,
    InitDataRejected,
    identity_from_headers,
    sign_init_data,
    validate_init_data,
)
from waifu.api.server import ROUTES, build_app, serve

__all__ = [
    "API_TOKEN_HEADER",
    "INIT_DATA_HEADER",
    "ROUTES",
    "InitDataRejected",
    "build_app",
    "identity_from_headers",
    "serve",
    "sign_init_data",
    "validate_init_data",
]
