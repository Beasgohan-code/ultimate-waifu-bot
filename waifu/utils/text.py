"""Text/HTML helpers (Telegram-flavoured, not generic)."""

from __future__ import annotations

import hashlib
import html
import re
import unicodedata

_ZERO_WIDTH = "\u200b"


def esc(text: str | None) -> str:
    """Escape for Telegram's HTML parse mode."""
    return html.escape(text or "", quote=False)


def bold(text: str) -> str:
    return f"<b>{esc(text)}</b>"


def italic(text: str) -> str:
    return f"<i>{esc(text)}</i>"


def code(text: str) -> str:
    return f"<code>{esc(text)}</code>"


def spoiler(text: str) -> str:
    return f'<span class="tg-spoiler">{esc(text)}</span>'


def link(url: str, text: str) -> str:
    return f'<a href="{html.escape(url, quote=True)}">{esc(text)}</a>'


def user_mention(user_id: int, name: str) -> str:
    return f'<a href="tg://user?id={user_id}">{esc(name or "user")}</a>'


def fmt_num(value: float | int | None) -> str:
    if value is None:
        return "0"
    value = int(value)
    if abs(value) >= 1_000_000_000:
        return f"{value / 1_000_000_000:.2f}B"
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if abs(value) >= 10_000:
        return f"{value / 1000:.1f}K"
    return f"{value:,}"


def clamp_text(text: str, limit: int = 4096) -> str:
    """Telegram's hard message limit is 4096 chars."""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def slugify(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    value = re.sub(r"[^\w\s-]", "", value.lower())
    return re.sub(r"[-\s]+", "-", value).strip("-")


def strip_md(value: str) -> str:
    """Remove markdown/HTML noise from user-provided character names."""
    value = re.sub(r"<[^>]+>", "", value)
    return re.sub(r"[*_`~]", "", value).strip()


def initials(name: str, count: int = 2) -> str:
    parts = [p for p in re.split(r"\s+", (name or "?").strip()) if p]
    if not parts:
        return "?"
    return "".join(p[0].upper() for p in parts[:count])


def short_hash(value: str, length: int = 8) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:length]


# Buttons must be one-line and short; Telegram allows 64 bytes of callback data.
def truncate(value: str, length: int = 24, ellipsis: str = "…") -> str:
    value = value.strip()
    return value if len(value) <= length else value[: length - len(ellipsis)] + ellipsis


def is_nsfw(text: str) -> bool:
    """Cheap pre-filter for the AI persona path. The real guard is the model.

    Deliberately conservative: a hit routes the reply through safe mode.
    """
    lowered = text.lower()
    tokens = re.findall(r"[a-z']+", lowered)
    banned = {"nsfw", "lewd", "sex", "nude", "porn", "rape", "rapey", "gf?m", "18+"}
    return bool(banned.intersection(tokens)) or "nsfw" in lowered


def zerowidth(value: str = "") -> str:
    return _ZERO_WIDTH + value


def chunk_message(text: str, *, limit: int = 1024) -> list[str]:
    """Split long text into Telegram-safe chunks, preferring sentence boundaries.

    ``limit`` is 1024 for captions and 4096 for messages. Splitting on the *last*
    newline/period before the cap keeps a streamed draft readable instead of cutting
    mid-word, and an over-long single line is hard-split rather than dropped.
    """
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        window = rest[:limit]
        cut = max(
            window.rfind("\n"),
            window.rfind(". "),
            window.rfind("! "),
            window.rfind("? "),
            window.rfind(" "),
        )
        if cut <= 0:
            cut = limit
        chunks.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    if rest:
        chunks.append(rest)
    return [chunk for chunk in chunks if chunk]
