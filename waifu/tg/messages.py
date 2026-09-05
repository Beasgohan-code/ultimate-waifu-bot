"""The unified message sender — one call site, three delivery modes.

Every card the bot posts goes through :func:`send_card`. It decides between

1. **rich message** (Bot API 9.5+) — real blocks, inline content buttons;
2. **photo + inline keyboard** — the classic fallback, identical wording;
3. **text only** — for chats/clients where media is unavailable, and for
   ``ChatMode.OFF`` set per-group by its owner.

Why not "always rich"? Because aiogram 3.31 speaks API 10.3 while a self-hosted
server may speak 8.x; and because a rich message cannot be edited through the
same payload path as a photo. Keeping the decision in one function means the
handlers never contain that complexity — Summon-bot's 40 duplicated
``send_photo(..., reply_markup=...)`` blocks are exactly what this replaces.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.methods import SendPhoto, SendRichMessage
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
)

from waifu.enums import ChatMode
from waifu.logging import get_logger
from waifu.tg.rich import RichButton, RichMessageBuilder
from waifu.utils.text import truncate

log = get_logger("tg.messages")

#: Styles Telegram 9.4+ accepts for inline keyboard buttons.
BUTTON_STYLES = {"default", "success", "danger", "warning", "premium"}


@dataclass(slots=True)
class SendResult:
    message: Message | None
    mode: ChatMode
    rich_message_id: int | None = None
    degraded: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.message is not None


def _markup(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup | None:
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def style_button(
    text: str,
    *,
    callback_data: str | None = None,
    url: str | None = None,
    style: str | None = None,
    icon_emoji_id: str | None = None,
    disabled: bool = False,
    copy_text: str | None = None,
) -> InlineKeyboardButton:
    """Inline keyboard button using the 2026 extras, silently dropping what fails.

    ``style`` was removed once by Telegram and re-added later; a hard dependency
    on it produced Summon-bot's "buttons vanished" incident. Here the field is set
    by attribute assignment inside a try, so an API that rejects it still gets a
    working button.
    """
    kwargs: dict[str, Any] = {"text": text[:64]}
    if callback_data:
        kwargs["callback_data"] = callback_data[:64]
    if url:
        kwargs["url"] = url
    if copy_text:
        from aiogram.types import CopyTextButton

        kwargs["copy_text"] = CopyTextButton(text=copy_text[:1024])
    if disabled:
        from aiogram.types import DisabledButton

        kwargs["disabled"] = DisabledButton()
    button = InlineKeyboardButton(**kwargs)
    if style and style in BUTTON_STYLES and style != "default":
        try:
            object.__setattr__(button, "style", style)
        except Exception:  # pragma: no cover - frozen model variant
            pass
    if icon_emoji_id:
        try:
            object.__setattr__(button, "icon_custom_emoji_id", icon_emoji_id)
        except Exception:  # pragma: no cover
            pass
    return button


async def send_card(
    bot: Bot,
    chat_id: int,
    *,
    builder: RichMessageBuilder,
    caption: str | None = None,
    photo: str | None = None,
    buttons: list[list[InlineKeyboardButton]] | None = None,
    mode: ChatMode = ChatMode.RICH,
    disable_notification: bool = False,
    reply_to: int | None = None,
    protect_content: bool = False,
    message_thread_id: int | None = None,
    allow_paid_broadcast: bool = False,
    rich_buttons: list[RichButton] | None = None,
) -> SendResult:
    """Send a card as a rich message when possible, otherwise as photo+caption."""
    want_rich = mode is ChatMode.RICH and not builder.is_empty
    if want_rich:
        if rich_buttons:
            builder.buttons(rich_buttons)
        try:
            message = await bot(
                SendRichMessage(
                    chat_id=chat_id,
                    rich_message=builder.build(),
                    disable_notification=disable_notification or None,
                    protect_content=protect_content or None,
                    message_thread_id=message_thread_id,
                    reply_to_message_id=reply_to,
                    allow_paid_broadcast=allow_paid_broadcast or None,
                )
            )
            return SendResult(
                message=message, mode=ChatMode.RICH, rich_message_id=message.message_id
            )
        except (TelegramBadRequest, AttributeError) as exc:
            # "method not found" / "rich_message too long" / older server → fallback.
            log.info("rich message rejected for chat %s (%s); falling back to photo", chat_id, exc)
            return await _fallback(
                bot,
                chat_id,
                builder=builder,
                caption=caption,
                photo=photo,
                buttons=buttons,
                disable_notification=disable_notification,
                reply_to=reply_to,
                message_thread_id=message_thread_id,
            )
    return await _fallback(
        bot,
        chat_id,
        builder=builder,
        caption=caption,
        photo=photo,
        buttons=buttons,
        disable_notification=disable_notification,
        reply_to=reply_to,
        message_thread_id=message_thread_id,
    )


async def _fallback(
    bot: Bot,
    chat_id: int,
    *,
    builder: RichMessageBuilder,
    caption: str | None,
    photo: str | None,
    buttons: list[list[InlineKeyboardButton]] | None,
    disable_notification: bool,
    reply_to: int | None,
    message_thread_id: int | None,
) -> SendResult:
    text = caption or builder.fallback_html()
    markup = _markup(buttons or [])
    if photo:
        try:
            message = await bot(
                SendPhoto(
                    chat_id=chat_id,
                    photo=photo,
                    caption=truncate(text, 1024) or None,
                    parse_mode="HTML",
                    reply_markup=markup,
                    disable_notification=disable_notification or None,
                    reply_to_message_id=reply_to,
                    show_caption_above_media=True,
                    link_preview=LinkPreviewOptions(is_disabled=True),
                    message_thread_id=message_thread_id,
                )
            )
            return SendResult(message=message, mode=ChatMode.PLAIN, degraded=True)
        except TelegramAPIError as exc:
            log.warning("photo send failed in chat %s (%s); sending text", chat_id, exc)
    try:
        message = await bot.send_message(
            chat_id,
            truncate(text, 4000),
            parse_mode="HTML",
            reply_markup=markup,
            disable_notification=disable_notification,
            reply_to_message_id=reply_to,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
            message_thread_id=message_thread_id,
        )
    except TelegramRetryAfter as exc:  # pragma: no cover - scheduler handles backoff
        log.warning("flood wait while sending in %s: %ss", chat_id, exc.retry_after)
        return SendResult(
            message=None, mode=ChatMode.PLAIN, degraded=True, error=f"flood:{exc.retry_after}"
        )
    return SendResult(message=message, mode=ChatMode.PLAIN, degraded=True)


async def edit_card(
    bot: Bot,
    chat_id: int,
    message_id: int,
    *,
    builder: RichMessageBuilder,
    caption: str | None = None,
    markup: InlineKeyboardMarkup | None = None,
    mode: ChatMode = ChatMode.RICH,
    photo: str | None = None,
) -> Message | None:
    """Update a card in place (spawn → "claimed", auction → new top bid).

    Bot API 10.3 still has **no** ``editMessageRichMessage``, so a rich card that
    must change is re-sent and the original deleted — that is the only honest
    implementation, and doing it here keeps every plugin from inventing its own
    (Summon-bot edited captions on messages that were photos, so the "claimed"
    state silently never appeared in some groups).
    """
    from aiogram.methods import EditMessageCaption, EditMessageReplyMarkup

    if mode is ChatMode.RICH and not builder.is_empty and photo is None:
        try:
            fresh = await bot(SendRichMessage(chat_id=chat_id, rich_message=builder.build()))
        except TelegramAPIError as exc:
            log.debug("rich re-send failed (%s); editing caption instead", exc)
        else:
            try:
                await bot.delete_message(chat_id, message_id)
            except TelegramAPIError:  # pragma: no cover - already gone
                pass
            return fresh

    text = caption or builder.fallback_html()
    methods = [
        EditMessageCaption(
            chat_id=chat_id,
            message_id=message_id,
            caption=truncate(text, 1024) or None,
            parse_mode="HTML",
        ),
    ]
    if markup is not None:
        methods.append(
            EditMessageReplyMarkup(chat_id=chat_id, message_id=message_id, reply_markup=markup)
        )
    edited: Message | None = None
    for method in methods:
        try:
            result = await bot(method)
            if isinstance(result, Message):
                edited = result
        except TelegramAPIError as exc:
            if "not modified" in str(exc).lower():
                continue
            log.debug("edit failed (%s): %s", method.__class__.__name__, exc)
    return edited


async def send_media_group(
    bot: Bot,
    chat_id: int,
    media: list[Any],
    *,
    caption: str | None = None,
    message_thread_id: int | None = None,
) -> list[Message]:
    """Album send with a shared caption, used by /h-stats and 10-pull grids."""
    from aiogram.types import InputMediaPhoto

    prepared = [
        m if isinstance(m, InputMediaPhoto) else InputMediaPhoto(media=m) for m in media[:10]
    ]
    if caption and prepared:
        prepared[0] = prepared[0].model_copy(
            update={
                "caption": truncate(caption, 1024),
                "parse_mode": "HTML",
                "show_caption_above_media": True,
            }
        )
    sent = await bot.send_media_group(
        chat_id=chat_id, media=prepared, message_thread_id=message_thread_id
    )
    return list(sent)
