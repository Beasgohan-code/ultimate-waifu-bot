"""Text/formatting helpers — HTML built once, correctly, everywhere.

Custom emoji (``<tg-emoji emoji-id="…">``, API 9.0+) and ``date_time`` entities
(9.1) are the two 2025-era formats worth adopting in a game bot: the first gives
animated rarity badges, the second makes "expires in" text localise per viewer
instead of showing everyone UTC.

Everything here returns plain strings/``MessageEntity`` lists so it composes with
aiogram's ``parse_mode=HTML`` default and with rich-message blocks.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from html import escape
from typing import Any

from aiogram.types import MessageEntity

_TAG_RE = re.compile(r"<(/?)(b|i|u|s|code|pre|blockquote|tg-emoji|span)([^>]*)>", re.IGNORECASE)
_USER_MENTION_RE = re.compile(r"\{\{(\w+)\}\}")
_CUSTOM_EMOJI_RE = re.compile(r":([a-z0-9_]{2,32}):")


def bold(text: str) -> str:
    return f"<b>{escape(text)}</b>"


def italic(text: str) -> str:
    return f"<i>{escape(text)}</i>"


def underline(text: str) -> str:
    return f"<u>{escape(text)}</u>"


def strike(text: str) -> str:
    return f"<s>{escape(text)}</s>"


def code(text: str) -> str:
    return f"<code>{escape(text)}</code>"


def spoiler(text: str) -> str:
    return f'<span class="tg-spoiler">{escape(text)}</span>'


def hidden(text: str) -> str:
    """Spoiler-tagged text — used for hints so the answer isn't free."""
    return spoiler(text)


def link(text: str, url: str) -> str:
    safe = escape(url, quote=True)
    return f'<a href="{safe}">{escape(text)}</a>'


def mention(user_id: int, name: str | None = None) -> str:
    """Mention by id (works even when the user has no username)."""
    label = escape(name or f"user {user_id}")
    return f'<a href="tg://user?id={int(user_id)}">{label}</a>'


def user_tag(raw: str, names: dict[str, str] | None = None) -> str:
    """Replace ``{{username}}`` placeholders with mentions. Used by templates."""
    mapping = names or {}

    def _sub(match: re.Match[str]) -> str:
        key = match.group(1)
        return mapping.get(key, f"@{key}")

    return _USER_MENTION_RE.sub(_sub, raw)


def custom_emoji(text: str, emoji_id: str) -> str:
    if not emoji_id:
        return escape(text)
    return f'<tg-emoji emoji-id="{escape(emoji_id, quote=True)}">{escape(text)}</tg-emoji>'


def expand_custom_emojis(text: str, map_: dict[str, str]) -> str:
    """``:fire:`` → animated emoji when the server supports custom emoji (9.0+).

    Falls back to the literal token so a disabled feature never shows broken tags.
    """
    if not map_:
        return text

    def _sub(match: re.Match[str]) -> str:
        token = match.group(1)
        emoji_id = map_.get(token)
        if not emoji_id:
            return match.group(0)
        return f'<tg-emoji emoji-id="{escape(emoji_id, quote=True)}">{escape(token)}</tg-emoji>'

    return _CUSTOM_EMOJI_RE.sub(_sub, text)


def date_time(
    unix: int | datetime, *, fmt: str = "dd MMM yyyy, HH:mm", label: str | None = None
) -> tuple[str, list[MessageEntity]]:
    """Localised timestamp as (text, entities) for ``parse_mode`` calls.

    Bot API 9.1's ``date_time`` entity renders the moment in each viewer's
    timezone and locale — so /daily "resets at" and auction deadlines stop being
    wrong for anyone outside the server's timezone.
    """
    if isinstance(unix, datetime):
        moment = int((unix if unix.tzinfo else unix.replace(tzinfo=UTC)).timestamp())
    else:
        moment = int(unix)
    rendered = label or datetime.fromtimestamp(moment, tz=UTC).strftime("%d %b %Y, %H:%M UTC")
    entity = MessageEntity(
        type="date_time", offset=0, length=len(rendered), unix_time=moment, date_time_format=fmt
    )
    return rendered, [entity]


def with_date_times(
    *segments: tuple[str, list[MessageEntity]] | str,
) -> tuple[str, list[MessageEntity]]:
    """Concatenate (text, entities) parts, rebasing entity offsets.

    Manual offset arithmetic is the #1 source of "TelegramBadRequest: entity
    offset out of range" in hand-rolled bot code; doing it once here removes the
    class of bug.
    """
    text_parts: list[str] = []
    entities: list[MessageEntity] = []
    offset = 0
    for segment in segments:
        if isinstance(segment, str):
            text_parts.append(segment)
            offset += len(segment)
            continue
        chunk, chunk_entities = segment
        text_parts.append(chunk)
        for entity in chunk_entities:
            entities.append(entity.model_copy(update={"offset": entity.offset + offset}))
        offset += len(chunk)
    return "".join(text_parts), entities


def strip_html(text: str) -> str:
    """Remove Telegram HTML tags (for log lines, file names and plain fallbacks)."""
    cleaned = _TAG_RE.sub("", text)
    return (
        re.sub(r"<[^>]{0,80}>", "", cleaned)
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
    )


def readable_int(value: int | float | None) -> str:
    """1234567 → ``1 234 567`` (thin spaces so it survives copy/paste)."""
    if value is None:
        return "0"
    return f"{int(value):,}".replace(",", "\u2009")


def percent(value: float, *, digits: int = 1) -> str:
    return f"{value:.{digits}f}%"


def compact_number(value: int) -> str:
    """For stat tables: 12.4k, 3.1M."""
    number = float(value)
    for unit, threshold in (("B", 1_000_000_000), ("M", 1_000_000), ("k", 1_000)):
        if abs(number) >= threshold:
            return f"{number / threshold:.1f}{unit}"
    return str(int(number))


def safe(text: Any, *, limit: int = 4000) -> str:
    """Escape + truncate on a *character* boundary that never splits a tag."""
    rendered = str(text or "")
    if len(rendered) <= limit:
        return rendered
    cut = rendered[:limit]
    # Drop a trailing half-written tag.
    if "<" in cut.rsplit(">", 1)[-1]:
        cut = cut[: cut.rfind("<")]
    return cut + "…"
