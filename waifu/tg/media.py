"""Media pipeline: file_ids, re-hosting, albums, live photos.

Summon-bot stored *external URLs* (uguu.se, catbox.moe) in the DB and re-sent them
on every spawn. Consequences it actually shipped with: hotlink deaths, a third-party
host as an availability dependency, and Telegram re-downloading the same image
hundreds of times a day (which is what exhausted its 30 req/s budget during spawn
storms).

The correct pipeline, implemented here:

1. an admin gives a URL, a file_id, or replies with a photo;
2. the bot re-uploads it into a private asset chat (``MEDIA_ARCHIVE_CHAT_ID``);
3. the returned ``file_id`` is stored on ``characters.photo_file_id``;
4. every later send uses the file_id — instant, free, offline-proof.

``resolve_media`` accepts whatever the DB holds so rows that predate step 3 keep
working during and after the migration, instead of erroring.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    BufferedInputFile,
    FSInputFile,
    InputFile,
    InputMediaAnimation,
    InputMediaLivePhoto,
    InputMediaPhoto,
    URLInputFile,
)

from waifu.logging import get_logger

log = get_logger("tg.media")

#: Bot API limits: a bot may upload 10 MB (2 GB through a local server).
BOT_UPLOAD_LIMIT = 10 * 1024 * 1024
_LOCAL_SERVER_UPLOAD_LIMIT = 2_000_0_000_000
# Telegram file identifiers are base64url blobs without "://" and never contain a
# dot; requiring "long + no dot + no slash" separates them from URLs and paths
# reliably enough for a DB column that only ever holds one of the three.
FILE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,}$")


@dataclass(frozen=True, slots=True)
class MediaRef:
    """A media pointer that knows what kind it is."""

    value: str
    kind: str = "url"  # url | file_id | local

    @classmethod
    def parse(cls, value: str | None) -> MediaRef | None:
        if not value:
            return None
        text = str(value).strip()
        if not text:
            return None
        if text.startswith(("http://", "https://")):
            return cls(text, "url")
        if text.startswith("file://"):
            return cls(text.removeprefix("file://"), "local")
        if Path(text).exists() or (text.startswith(("/", "./", "~/")) and "." in Path(text).suffix):
            return cls(text, "local")
        if FILE_ID_RE.match(text):
            return cls(text, "file_id")
        return cls(text, "url")

    @property
    def needs_rehost(self) -> bool:
        """URLs should be converted to file_ids; anything else can be sent directly."""
        return self.kind == "url"

    def as_input(self) -> str | InputFile:
        if self.kind == "file_id":
            return self.value
        if self.kind == "local":
            return FSInputFile(self.value)
        return URLInputFile(url=self.value)


def resolve_media(value: str | None) -> str | InputFile | None:
    """Turn a stored media string into something Telegram can send."""
    ref = MediaRef.parse(value)
    return ref.as_input() if ref else None


def is_file_id(value: str | None) -> bool:
    ref = MediaRef.parse(value)
    return bool(ref and ref.kind == "file_id")


async def rehost_into_chat(
    bot: Bot,
    source: str,
    chat_id: int,
    *,
    caption: str = "",
    as_live: bool = False,
    spoiler: bool = False,
) -> dict[str, Any]:
    """Download external art and return a permanent Telegram file_id.

    Returns ``{"ok", "file_id", "kind", "error"}`` rather than raising: the caller is
    an admin command that must report *why* an asset failed instead of dying.
    """
    result: dict[str, Any] = {"ok": False, "file_id": "", "kind": "", "error": ""}
    ref = MediaRef.parse(source)
    if ref is None:
        result["error"] = "empty media reference"
        return result
    if ref.kind == "file_id":  # nothing to do — already archived
        result.update({"ok": True, "file_id": ref.value, "kind": "photo"})
        return result
    payload = ref.as_input()
    try:
        if as_live:
            sent = await bot.send_animation(chat_id, animation=payload, caption=caption or None)
            media = sent.animation
            result["kind"] = "animation"
        else:
            sent = await bot.send_photo(
                chat_id, photo=payload, caption=caption or None, has_spoiler=spoiler or None
            )
            media = sent.photo[-1] if sent.photo else None
            result["kind"] = "photo"
    except TelegramAPIError as exc:
        result["error"] = str(exc)[:240]
        log.warning("rehost failed for %s: %s", ref.value[:60], exc)
        return result
    if media is None:  # pragma: no cover - defensive
        result["error"] = "telegram returned no media"
        return result
    result.update(
        {"ok": True, "file_id": media.file_id, "width": media.width, "height": media.height}
    )
    return result


async def download_file(
    bot: Bot, file_id: str, *, destination: str | Path, chunk: int = 65536
) -> Path | None:
    """Persist bot-visible media to disk (``/exportchar``, backups, art mirroring)."""
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = await bot.download(file_id, destination=target, chunk_size=chunk)
    except TelegramAPIError as exc:
        log.warning("download failed for %s: %s", file_id[:24], exc)
        return None
    return target if handle is not None or target.exists() else None


async def bytes_from_file_id(
    bot: Bot, file_id: str, *, limit: int = BOT_UPLOAD_LIMIT
) -> bytes | None:
    """In-memory read for card rendering (the Pillow pipeline wants bytes, not paths)."""
    import io

    try:
        handle = await bot.download(file_id, destination=io.BytesIO(), chunk_size=65536)
    except TelegramAPIError as exc:
        log.debug("bytes download failed: %s", exc)
        return None
    if handle is None:
        return None
    data = handle.getvalue() if hasattr(handle, "getvalue") else b""
    return data[:limit] if data else None


def from_bytes(data: bytes, filename: str) -> BufferedInputFile:
    return BufferedInputFile(file=data, filename=filename)


def photo_item(
    media: str, *, caption: str | None = None, spoiler: bool = False, above: bool = True
) -> InputMediaPhoto:
    return InputMediaPhoto(
        media=resolve_media(media) or media,
        caption=caption,
        parse_mode="HTML" if caption else None,
        has_spoiler=spoiler or None,
        show_caption_above_media=(above and bool(caption)) or None,
    )


def media_album(
    items: Iterable[tuple[str, str | None]],
    *,
    album_caption: str | None = None,
    spoiler: bool = False,
) -> list[InputMediaPhoto]:
    """Build an album; the shared caption goes on the first item only.

    ``show_caption_above_media`` keeps long captions from covering the art — the
    detail that makes a 10-pull album readable on a phone.
    """
    prepared: list[InputMediaPhoto] = []
    for index, (media, caption) in enumerate(items):
        prepared.append(
            photo_item(
                media,
                caption=album_caption if index == 0 and album_caption else caption,
                spoiler=spoiler,
            )
        )
    return prepared


def live_photo(
    video: str, still: str, *, caption: str | None = None, spoiler: bool = False
) -> InputMediaLivePhoto:
    """Live Photo (API 9.1): still cover + motion on long-press, no video UI."""
    return InputMediaLivePhoto(
        media=resolve_media(video) or video,
        photo=resolve_media(still) or still,
        caption=caption,
        parse_mode="HTML" if caption else None,
        has_spoiler=spoiler or None,
        show_caption_above_media=True if caption else None,
    )


def animation(
    media: str, *, cover: str | None = None, caption: str | None = None, duration: int | None = None
) -> InputMediaAnimation:
    return InputMediaAnimation(
        media=resolve_media(media) or media,
        thumbnail=resolve_media(cover) if cover else None,
        caption=caption,
        parse_mode="HTML" if caption else None,
        duration=duration,
        show_caption_above_media=True if caption else None,
    )


def fingerprint(data: bytes) -> str:
    """Content hash used to deduplicate re-uploads (stored on ``characters``)."""
    return hashlib.sha256(data).hexdigest()[:32]


async def collect_media(bot: Bot, *values: str | None) -> dict[str, str | InputFile]:
    """Classify several stored references at once → ``{stored: sendable}``.

    Handlers use this instead of passing raw strings, which is how an expired
    uguu.se link ended up baked into 40 spawn cards in the reference bot.
    """
    out: dict[str, str | InputFile] = {}
    for value in values:
        ref = MediaRef.parse(value)
        if ref is None:
            continue
        out[str(value)] = ref.as_input()
    return out


async def archive_is_usable(bot: Bot, chat_id: int) -> bool:
    """Cheap check that ``MEDIA_ARCHIVE_CHAT_ID`` still accepts messages."""
    if not chat_id:
        return False
    try:
        await bot.get_chat(chat_id)
    except TelegramAPIError:
        return False
    return True
