"""Bot session wiring: the official API by default, a local server only when
``BOT_API_URL`` says so.

Regression pinned here: the deploy crashed with
``BaseSession.__init__() got an unexpected keyword argument 'api_url'`` —
aiogram-2.x parameters passed to aiogram 3.x — and a generic ``API_BASE``
env var (set for another tool) flipped the bot into local-server mode.
"""

from __future__ import annotations

from waifu.core.bot import build_session
from waifu.settings import Settings


def _settings(**env: object) -> Settings:
    return Settings(_env_file=None, bot_token="123:abc", owner_id=1, **env)


def test_session_defaults_to_the_official_api() -> None:
    session = build_session(_settings())
    assert not session.api.is_local
    assert session.api.api_url("T", "getMe") == "https://api.telegram.org/botT/getMe"


def test_session_uses_the_local_server_only_when_configured() -> None:
    session = build_session(_settings(bot_api_url="http://apiserver:80"))
    assert session.api.is_local
    # the standard tg-bot-api layout, derived from the single configured URL
    assert session.api.api_url("T", "getMe") == "http://apiserver:80/botT/getMe"
    assert session.api.file_url("T", "photos/a.png") == "http://apiserver:80/file/botT/photos/a.png"


def test_the_generic_api_env_names_are_gone() -> None:
    """API_URL / API_BASE were too generic: any other tool that sets them on the
    platform hijacked the bot. Only the explicit BOT_API_URL switches local mode."""
    settings = _settings()
    assert not hasattr(settings, "api_url")
    assert not hasattr(settings, "api_base")
