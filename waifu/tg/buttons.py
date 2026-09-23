"""Buttons, button styles, and prepared inline buttons (Bot API 9.1+/9.4).

``savePreparedInlineMessage`` lets a bot pre-build an inline message once and
share it as a ``t.me/Bot?startgroup=…`` / ``!`` link — the only clean way to
"send this card into the group you choose", which Summon-bot faked with
``switch_inline_query`` and an inline mode the user had to understand.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from typing import Any

from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    SwitchInlineQueryChosenChat,
)

from waifu.logging import get_logger
from waifu.tg.rich import RichButton

log = get_logger("tg.buttons")

Row = list[InlineKeyboardButton]


def grid(buttons: Sequence[InlineKeyboardButton], *, columns: int = 3) -> list[Row]:
    """Chunk a flat list into rows (used by collection/market pagination grids)."""
    return [list(buttons[i : i + columns]) for i in range(0, len(buttons), columns)]


def callback(
    text: str,
    data: str,
    *,
    style: str | None = None,
    icon_emoji_id: str | None = None,
    disabled: bool = False,
) -> InlineKeyboardButton:
    kwargs: dict[str, Any] = {"text": text[:64], "callback_data": data[:64]}
    if style:
        kwargs["style"] = style
    if icon_emoji_id:
        kwargs["icon_custom_emoji_id"] = icon_emoji_id
    if disabled:
        from aiogram.types import DisabledButton

        kwargs["disabled"] = DisabledButton()
    return InlineKeyboardButton(**kwargs)


def pager(
    *,
    page: int,
    pages: int,
    prefix: str,
    label: str | None = None,
    extra: str = "",
    first_last: bool = True,
) -> Row:
    """A standard ``« ‹ 3/9 › »`` row.

    ``prefix`` is this router's callback namespace (``auc``, ``col``…), which keeps
    the 27 prefixes in the legacy bot from colliding.
    """
    suffix = f":{extra}" if extra else ""
    row: Row = []
    if pages <= 1:
        return row
    if first_last and page > 1:
        row.append(callback("«", f"{prefix}:first{suffix}"))
    row.append(callback("‹", f"{prefix}:prev{suffix}", disabled=page <= 1))
    row.append(callback(label or f"{page}/{pages}", f"{prefix}:noop{suffix}", disabled=True))
    row.append(callback("›", f"{prefix}:next{suffix}", disabled=page >= pages))
    if first_last and page < pages:
        row.append(callback("»", f"{prefix}:last{suffix}"))
    return row


def switch_to_chat(
    text: str = "Share",
    *,
    query: str = "",
    users: bool = True,
    bots: bool = False,
    groups: bool = True,
    channels: bool = True,
    same_group: bool = False,
) -> InlineKeyboardButton:
    """ "Send this card to another chat" — API 7.x ``switchInlineQueryChosenChat``.

    With ``query=""`` the picker opens with the *prepared message* id instead of
    inline mode, which is what /share and /gift use.
    """
    return InlineKeyboardButton(
        text=text[:64],
        switch_inline_query_chosen_chat=SwitchInlineQueryChosenChat(
            query=query,
            allow_user_chats=users,
            allow_bot_chats=bots,
            allow_group_chats=groups,
            allow_channel_chats=channels,
        ),
    )


async def prepare_inline_message(
    bot, *, user_id: int, title: str, text: str, allow: tuple[str, ...] = ("group", "channel")
) -> str | None:
    """Store a card as a *prepared inline message* and return its id (API 9.1).

    The id goes into ``t.me/<bot>?startgroup=<id>``, which opens Telegram's chat
    picker and lets the user drop our card into a chat the bot is **not** in. This
    is how /gift and /share avoid the old "invite the bot first" wall.
    """
    from aiogram.methods import SavePreparedInlineMessage
    from aiogram.types import InlineQueryResultArticle, InputTextMessageContent

    result = InlineQueryResultArticle(
        id=uuid.uuid4().hex[:16],
        title=title[:64],
        description=text[:64],
        input_message_content=InputTextMessageContent(message_text=text[:4000], parse_mode="HTML"),
    )
    kwargs = {f"allow_{kind}_chats": True for kind in allow}
    try:
        prepared = await bot(SavePreparedInlineMessage(user_id=user_id, result=result, **kwargs))  # type: ignore[arg-type]
    except TelegramAPIError as exc:  # pragma: no cover - older server
        log.info("prepared inline message unsupported: %s", exc)
        return None
    return getattr(prepared, "id", None)


def share_link(bot_username: str, prepared_id: str | None = None, *, group: bool = True) -> str:
    """``t.me/bot?startgroup=…`` URL for the button above (or a plain deep link)."""
    if prepared_id:
        return (
            f"https://t.me/{bot_username}?{'startgroup' if group else 'startchannel'}={prepared_id}"
        )
    return f"https://t.me/{bot_username}?{'startgroup' if group else 'start'}=share"


def rich_row(*buttons: RichButton) -> list[RichButton]:
    return list(buttons)


def markup(rows: Iterable[Row]) -> InlineKeyboardMarkup | None:
    materialised = [row for row in rows if row]
    return InlineKeyboardMarkup(inline_keyboard=materialised) if materialised else None


def disable_row(row: Row, *, note: str | None = None) -> Row:
    """Return a dead copy of a row (spawn claimed → buttons must stop lying)."""
    from aiogram.types import DisabledButton

    return [
        InlineKeyboardButton(text=(note or button.text)[:64], disabled=DisabledButton())
        if button.callback_data
        else button
        for button in row
    ]
