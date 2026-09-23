"""Stories, menu button, Mini App entry points and per-chat command scopes.

``postStory`` (API 7.x) is the only way a bot can appear in a *story feed*, and
``setChatMenuButton`` (6.9) is how a Mini App becomes one tap inside a group. The
bot uses both during event weeks (double-rarity weekend posts a story; group owners
get an "Open shop" button that deep-links their chat into the Mini App), and
per-chat ``setMyCommands`` so an event group only shows the commands that make
sense there — the config knob Summon-bot's README promised but never had.

Every function here returns a bool/dict instead of raising: these are decorations,
and a missing capability must never break a game loop.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.methods import PostStory, RepostStory, SetChatMenuButton
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    FSInputFile,
    InlineKeyboardButton,
    InputStoryContentPhoto,
    InputStoryContentVideo,
    MenuButtonCommands,
    MenuButtonDefault,
    MenuButtonWebApp,
    WebAppInfo,
)

from waifu.logging import get_logger
from waifu.utils.text import truncate

log = get_logger("tg.stories")

#: Telegram's own bounds for ``active_period`` (hours).
STORY_MIN_HOURS = 1
STORY_MAX_HOURS = 168


async def post_photo_story(
    bot: Bot, path: str | Path, *, caption: str = "", hours: int = 24
) -> dict[str, Any]:
    """Publish a story from a local file. Stories do not accept remote URLs."""
    file = Path(path)
    if not file.exists():
        return {"ok": False, "error": f"no such file: {file}"}
    try:
        story = await bot(
            PostStory(
                content=InputStoryContentPhoto(photo=FSInputFile(str(file))),
                caption=truncate(caption, 200) or None,
                parse_mode="HTML" if caption else None,
                active_period=max(STORY_MIN_HOURS, min(hours, STORY_MAX_HOURS)),
                post_to_chat_page=True,
            )
        )
    except TelegramAPIError as exc:
        log.info("postStory unavailable (%s): %s", file.name, exc)
        return {"ok": False, "error": str(exc)[:200]}
    return {"ok": True, "story_id": getattr(story, "id", None)}


async def post_video_story(
    bot: Bot, path: str | Path, *, caption: str = "", hours: int = 24, is_animation: bool = False
) -> dict[str, Any]:
    file = Path(path)
    if not file.exists():
        return {"ok": False, "error": f"no such file: {file}"}
    kwargs: dict[str, Any] = {"video": FSInputFile(str(file))}
    if is_animation:
        kwargs["is_animation"] = True
    try:
        story = await bot(
            PostStory(
                content=InputStoryContentVideo(**kwargs),
                caption=truncate(caption, 200) or None,
                parse_mode="HTML" if caption else None,
                active_period=max(STORY_MIN_HOURS, min(hours, STORY_MAX_HOURS)),
            )
        )
    except TelegramAPIError as exc:
        log.info("postStory(video) unavailable: %s", exc)
        return {"ok": False, "error": str(exc)[:200]}
    return {"ok": True, "story_id": getattr(story, "id", None)}


async def repost(bot: Bot, from_chat_id: int, story_id: int, *, to_chat_page: bool = True) -> bool:
    try:
        await bot(
            RepostStory(
                from_chat_id=from_chat_id,
                from_story_id=story_id,
                post_to_chat_page=to_chat_page or None,
            )
        )
    except TelegramAPIError as exc:
        log.debug("repostStory failed: %s", exc)
        return False
    return True


# ------------------------------------------------------------------ menu button
async def set_web_app_button(
    bot: Bot, url: str, *, text: str = "Open shop", chat_id: int | None = None
) -> bool:
    """Attach the Mini App to the menu button (global, or for one chat)."""
    try:
        await bot(
            SetChatMenuButton(
                chat_id=chat_id,
                menu_button=MenuButtonWebApp(text=text[:64], web_app=WebAppInfo(url=url)),
            )
        )
    except TelegramAPIError as exc:
        log.warning("setChatMenuButton(web_app) failed: %s", exc)
        return False
    return True


async def set_commands_button(bot: Bot, *, chat_id: int | None = None) -> bool:
    try:
        await bot(SetChatMenuButton(chat_id=chat_id, menu_button=MenuButtonCommands()))
    except TelegramAPIError:
        return False
    return True


async def reset_menu_button(bot: Bot, chat_id: int) -> bool:
    try:
        await bot(SetChatMenuButton(chat_id=chat_id, menu_button=MenuButtonDefault()))
    except TelegramAPIError:
        return False
    return True


# ------------------------------------------------------------------- command scopes
def commands_from(pairs: list[tuple[str, str]]) -> list[BotCommand]:
    return [
        BotCommand(command=name.lstrip("/"), description=description[:256])
        for name, description in pairs
    ]


async def scope_commands_to_chat(bot: Bot, chat_id: int, pairs: list[tuple[str, str]]) -> bool:
    """Give one supergroup its own command list.

    ``BotCommandScopeChat`` means ``/help`` in the event channel shows event
    commands — the least-effort fix for "why is /addchar in my casual chat".
    """
    try:
        await bot.set_my_commands(commands_from(pairs), scope=BotCommandScopeChat(chat_id=chat_id))
    except TelegramAPIError as exc:
        log.debug("scoped commands refused for %s: %s", chat_id, exc)
        return False
    return True


async def clear_chat_command_scope(bot: Bot, chat_id: int) -> bool:
    try:
        await bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=chat_id))
    except TelegramAPIError:
        return False
    return True


# ------------------------------------------------------------------------ helpers
def web_app_button(text: str, url: str, *, style: str | None = None) -> InlineKeyboardButton:
    """A Mini App button — the button type Summon-bot never used.

    ``style`` (9.4) makes "Open shop" read differently from navigation buttons.
    """
    kwargs: dict[str, Any] = {"text": truncate(text, 64), "web_app": WebAppInfo(url=url)}
    if style:
        kwargs["style"] = style
    return InlineKeyboardButton(**kwargs)


def app_link(bot_username: str, *, startapp: str = "") -> str:
    """``https://t.me/<bot>/<app>?startapp=…`` — the shareable Mini App deep link."""
    base = f"https://t.me/{bot_username}"
    return f"{base}?startapp={startapp}" if startapp else base


def expiry_label(moment: datetime | None) -> str:
    if moment is None:
        return "never"
    reference = moment if moment.tzinfo else moment.replace(tzinfo=UTC)
    remaining = reference - datetime.now(UTC)
    if remaining <= timedelta(0):
        return "expired"
    days, rest = divmod(int(remaining.total_seconds()), 86400)
    hours, minutes = divmod(rest // 60, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"
