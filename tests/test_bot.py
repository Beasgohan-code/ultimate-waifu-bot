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


class _MenuBot:
    """Records every set_my_commands call with its scope."""

    def __init__(self) -> None:
        self.scopes: list[object] = []

    async def set_my_commands(self, commands: object, scope: object | None = None) -> object:
        self.scopes.append(scope)
        return True


async def test_set_command_menu_uses_scopes_aiogram_actually_ships() -> None:
    """Regression: the deploy crashed with
    ``cannot import name 'BotCommandScopeChatAdmins' from 'aiogram.types'`` —
    an invented type name in a lazy import that no test ever executed."""
    from aiogram.types import (
        BotCommandScopeAllChatAdministrators,
        BotCommandScopeChatAdministrators,
        BotCommandScopeDefault,
    )

    from waifu.core.bot import set_command_menu

    bot = _MenuBot()
    await set_command_menu(
        bot, [("start", "Start"), ("help", "Help")], settings=_settings(support_chat_id=1234)
    )
    assert [type(scope) for scope in bot.scopes] == [
        BotCommandScopeDefault,
        BotCommandScopeAllChatAdministrators,
        BotCommandScopeChatAdministrators,
    ]
    assert bot.scopes[2].chat_id == 1234

    bare = _MenuBot()
    await set_command_menu(bare, [("start", "Start")], settings=_settings())
    assert [type(scope) for scope in bare.scopes] == [
        BotCommandScopeDefault,
        BotCommandScopeAllChatAdministrators,
    ]


class _ProbeBot:
    """Answers every probe; records which methods were built and sent."""

    def __init__(self) -> None:
        self.methods: list[str] = []

    async def __call__(self, method: object) -> object:
        """The real server answers the probe sends with 'chat not found' — the
        bot's chat 1 is never valid. Anything else (a crash) would mask a
        payload regression, so mimic the real error exactly."""
        from aiogram.exceptions import TelegramAPIError

        self.methods.append(type(method).__name__)
        raise TelegramAPIError(type(method).__name__, "Bad Request: chat not found")

    async def set_my_commands(self, commands: object, scope: object | None = None) -> object:
        self.methods.append("set_my_commands")
        return True

    async def get_me(self) -> object:
        class _Me:
            id = 42
            can_manage_guests = False

        return _Me()


async def test_probe_payloads_build_and_flags_are_read() -> None:
    """Regression: the probe payloads were built with aiogram-2.x shapes
    (``RichMessage`` where ``InputRichMessage`` is required, a string
    ``draft_id``) — the ValidationError killed the whole capability probe
    at every real deploy, silently degrading the bot to defaults."""
    from waifu.core.bot import probe_api_features

    bot = _ProbeBot()
    flags = await probe_api_features(bot)
    assert flags == {
        "rich_message": True,
        "message_draft": True,
        "reactions": True,
        "guest_mode": False,
    }
    assert "SendRichMessage" in bot.methods
    assert "SendMessageDraft" in bot.methods
