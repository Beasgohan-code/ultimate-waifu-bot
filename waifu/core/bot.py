"""Bot construction: defaults, local Bot API server, identity, command menus.

``DefaultBotProperties`` is set once here so no handler has to remember
``parse_mode`` — Summon-bot passed it by hand in ~400 call sites and 3 of them
leaked raw ``<b>`` tags into chats.
"""

from __future__ import annotations

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.methods import DeleteWebhook
from aiogram.types import LinkPreviewOptions

from waifu.logging import get_logger
from waifu.settings import Settings, get_settings

log = get_logger("core.bot")


def build_session(settings: Settings) -> AiohttpSession:
    """Point at a local Bot API server when configured (large files, less latency)."""
    kwargs: dict[str, object] = {"timeout": 60}
    if settings.bot_api_url:
        kwargs["api"] = TelegramAPIServer.from_base(settings.bot_api_url, is_local=True)
        log.info("using local Bot API server: %s", settings.bot_api_url.rstrip("/"))
    return AiohttpSession(**kwargs)  # type: ignore[arg-type]


def build_bot(settings: Settings | None = None, *, token: str | None = None) -> Bot:
    cfg = settings or get_settings()
    bot = Bot(
        token=token or cfg.bot_token,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML,
            # Link previews make spawn cards noisy and leak the art host's URL.
            link_preview=LinkPreviewOptions(is_disabled=True),
        ),
        session=build_session(cfg),
    )
    return bot


async def delete_webhook_if_polling(bot: Bot, settings: Settings) -> None:
    if settings.mode == "polling":
        # A leftover webhook silently starves polling — the classic "bot online
        # but receives nothing" bug.
        await bot(DeleteWebhook(drop_pending_updates=False))


async def apply_identity(bot: Bot, settings: Settings | None = None) -> dict[str, str]:
    """Bot name/description/short description + menu buttons, from config.

    ``setMyProfilePhoto``/``setMyName`` are Bot API 9.4 features, so a server
    owner can rebrand the bot without touching @BotFather.
    """
    cfg = settings or get_settings()
    applied: dict[str, str] = {}
    if cfg.bot_username:
        applied["username"] = cfg.bot_username
    me = await bot.get_me()
    applied["id"] = str(me.id)
    applied["can_join_groups"] = str(bool(me.can_join_groups))
    return applied


async def set_command_menu(
    bot: Bot, commands: list[tuple[str, str]], *, settings: Settings | None = None
) -> None:
    """Publish the command menu for private chats, groups and (if wanted) channels."""
    from aiogram.types import (
        BotCommand,
        BotCommandScopeAllChatAdministrators,
        BotCommandScopeChatAdministrators,
        BotCommandScopeDefault,
    )

    cfg = settings or get_settings()
    cmds = [BotCommand(command=name, description=description) for name, description in commands]
    await bot.set_my_commands(cmds, scope=BotCommandScopeDefault())
    # groups where the bot is admin also get the full menu
    await bot.set_my_commands(cmds, scope=BotCommandScopeAllChatAdministrators())
    if cfg.support_chat_id:
        await bot.set_my_commands(
            [
                c
                for c in cmds
                if c.command
                in {"start", "help", "summon", "spawn", "checkspawn", "changetime", "market"}
            ],
            scope=BotCommandScopeChatAdministrators(chat_id=cfg.support_chat_id),
        )


async def configure_menu_button(bot: Bot, settings: Settings | None = None) -> None:
    """The menu button (Bot API 6.9+, richer in 9.x) → help or the Mini App."""
    from aiogram.types import MenuButtonCommands, MenuButtonWebApp, WebAppInfo

    cfg = settings or get_settings()
    if cfg.webapp_url:
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="Open shop", web_app=WebAppInfo(url=cfg.webapp_url))
        )
    else:
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())


async def probe_api_features(bot: Bot) -> dict[str, bool]:
    """Negotiate the 2026 API surface with the endpoint we are actually talking to.

    aiogram 3.31 *ships* rich messages and drafts, but an older self-hosted Bot
    API server rejects unknown methods. Probing once at startup lets every
    handler branch on a capability flag instead of failing per message — which is
    exactly how Summon-bot's ``style=`` button regression reached production.
    """
    from aiogram.exceptions import TelegramAPIError
    from aiogram.methods import SendMessageDraft, SendRichMessage
    from aiogram.types import InputRichMessage

    flags = {"rich_message": False, "message_draft": False, "reactions": False, "guest_mode": False}
    probe_chat = 1  # never valid, but enough to make the server parse the method name

    # Factories: the payloads must build *inside* the try, so a validation bug
    # here degrades one flag instead of skipping the whole probe (that bug
    # shipped once — and the deploy log only showed a bare pydantic traceback).
    probes = (
        (
            "rich_message",
            lambda: SendRichMessage(chat_id=probe_chat, rich_message=InputRichMessage(blocks=[])),
        ),
        ("message_draft", lambda: SendMessageDraft(chat_id=probe_chat, draft_id=7, text="probe")),
    )
    for key, factory in probes:
        try:
            await bot(factory())
        except TelegramAPIError as exc:
            description = (getattr(exc, "message", "") or "").lower()
            # 'method not found' ⇒ the server is too old for the method. Anything
            # else ⇒ the server knows it — in particular 'chat not found', which
            # is the *expected* answer to the fake probe chat and must not be
            # confused with a missing method (that substring bug would disable
            # rich messages on the official API).
            flags[key] = "method not found" not in description
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("probe %s failed: %s", key, exc)
    try:
        await bot.set_my_commands([])
        flags["reactions"] = True
    except TelegramAPIError:  # pragma: no cover
        pass
    me = await bot.get_me()
    flags["guest_mode"] = bool(getattr(me, "can_manage_guests", False))
    log.info("API capabilities: %s", ", ".join(f"{k}={v}" for k, v in flags.items()))
    return flags
