"""Card rendering — generated art so a character is never a blank caption.

Half of Summon-bot's catalogue had no image at all, and its handler sent
``send_photo(None)`` → the bot threw. Here, when a character has no art, the bot
composes a card: rarity-tinted gradient, name, series, stat bars, an optional
voice line — 900×1200 PNG, cached on disk and (when an archive chat is configured)
uploaded once to get a permanent file_id.

Pillow is an optional dependency at runtime: if it is missing, the service reports
``available = False`` and the renderer falls back to text blocks.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from waifu.db.models import Character
from waifu.db.repositories import characters as char_repo
from waifu.enums import Rarity
from waifu.services.base import Service
from waifu.tg.media import rehost_into_chat
from waifu.utils.text import truncate

try:  # pragma: no cover - exercised by the no-Pillow path
    from PIL import Image, ImageDraw, ImageFont

    HAVE_PIL = True
except Exception:  # pragma: no cover
    Image = ImageDraw = ImageFont = None  # type: ignore[assignment]
    HAVE_PIL = False

CARD_SIZE = (900, 1200)
FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
)


@dataclass(slots=True)
class CardImage:
    path: Path | None
    file_id: str = ""
    generated: bool = False
    reason: str = ""

    @property
    def sendable(self) -> str:
        """What to hand to ``sendPhoto``: file_id > local path > ''."""
        return self.file_id or (str(self.path) if self.path else "")

    @property
    def ok(self) -> bool:
        return bool(self.sendable)


class CardService(Service):
    available: bool = HAVE_PIL

    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)
        self.cache_dir = Path(self.settings.data_dir) / "cards"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ lookup
    async def image_for(
        self, session: AsyncSession, character: Character | None, *, force_render: bool = False
    ) -> CardImage:
        """Prefer stored art; render only when there is genuinely nothing to show."""
        if character is None:
            return CardImage(path=None, reason="no character")
        if character.photo_file_id and not force_render:
            return CardImage(path=None, file_id=character.photo_file_id)
        if character.image_url and not force_render:
            return CardImage(path=None, file_id="", reason="url")
        if not HAVE_PIL:
            return CardImage(path=None, reason="pillow unavailable")
        path = self.render(character)
        if path is None:
            return CardImage(path=None, reason="render failed")
        return CardImage(path=path, generated=True)

    def signature(self, character: Character) -> str:
        raw = f"{character.id}:{character.name}:{character.rarity_id}:{character.stat_power}:{character.description[:64]}"
        return hashlib.sha256(raw.encode()).hexdigest()[:24]

    def render(self, character: Character) -> Path | None:  # pragma: no cover - needs fonts
        """Compose the PNG. Synchronous and CPU-light (~15 ms), so no thread needed."""
        if not HAVE_PIL:
            return None
        path = self.cache_dir / f"{self.signature(character)}.png"
        if path.exists():
            return path
        rarity = Rarity.from_value(character.rarity_id)
        tint = rarity.color
        width, height = CARD_SIZE
        try:
            image = Image.new("RGB", (width, height), (18, 18, 24))
            draw = ImageDraw.Draw(image)
            # Vertical gradient from the rarity colour into near-black.
            for y in range(height):
                ratio = y / height
                colour = tuple(int(c * (1 - ratio) * 0.85 + 12) for c in tint)
                draw.line([(0, y), (width, y)], fill=colour)  # type: ignore[arg-type]
            font_title = self._font(64)
            font_body = self._font(34)
            font_small = self._font(26)
            # Soft translucent band behind the name (RGBA composite, not a paste:
            # pasting a composite of mismatched sizes is how this used to crash).
            glow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            ImageDraw.Draw(glow).rectangle(
                (0, 40, width, 300), fill=(tint[0], tint[1], tint[2], 70)
            )
            image = Image.alpha_composite(image.convert("RGBA"), glow).convert("RGB")
            draw = ImageDraw.Draw(image)
            draw.text((48, 90), truncate(character.name, 22), font=font_title, fill=(250, 250, 252))
            draw.text(
                (48, 178),
                truncate(character.anime or "original", 34),
                font=font_small,
                fill=(210, 210, 220),
            )
            draw.text((48, 250), rarity.badge, font=font_body, fill=(255, 255, 255))
            if character.description:
                self._wrap(
                    draw, truncate(character.description, 240), (48, 340), font_body, width - 96
                )
            self._stats(draw, character, (48, height - 360), width - 96, font_small)
            if character.voice_line:
                draw.text(
                    (48, height - 150),
                    f"“{truncate(character.voice_line, 70)}”",
                    font=font_small,
                    fill=(235, 235, 245),
                )
            draw.text(
                (48, height - 84),
                f"#{character.id} · ultimate waifu bot",
                font=font_small,
                fill=(170, 170, 185),
            )
            image.save(path, "PNG", optimize=True)
        except Exception as exc:
            from waifu.logging import get_logger

            get_logger("cards").warning("card render failed for %s: %s", character.name, exc)
            return None
        return path

    def _font(self, size: int):
        for candidate in FONT_CANDIDATES:
            if Path(candidate).exists():
                return ImageFont.truetype(candidate, size)
        return ImageFont.load_default()

    @staticmethod
    def _wrap(draw, text: str, origin: tuple[int, int], font, width: int) -> None:
        words, line, x, y = text.split(), "", origin[0], origin[1]
        del x
        for word in words:
            trial = f"{line} {word}".strip()
            if draw.textlength(trial, font=font) > width and line:
                draw.text((origin[0], y), line, font=font, fill=(225, 225, 235))
                y += int(font.size * 1.35)
                line = word
            else:
                line = trial
        if line:
            draw.text((origin[0], y), line, font=font, fill=(225, 225, 235))

    @staticmethod
    def _stats(draw, character: Character, origin: tuple[int, int], width: int, font) -> None:
        rows = [
            ("Power", min(100, character.stat_power)),
            ("Rarity", int(Rarity.from_value(character.rarity_id)) * 12),
            ("Voice", 55 if character.voice_line else 20),
            ("Art", 75 if character.image_ref() else 15),
        ]
        y = origin[1]
        for label, value in rows:
            draw.text((origin[0], y), label, font=font, fill=(220, 220, 232))
            draw.rounded_rectangle(
                (origin[0] + 150, y + 6, origin[0] + width, y + 20),
                radius=7,
                fill=(255, 255, 255, 30),
            )
            filled = int((origin[0] + width - (origin[0] + 150)) * max(0.05, min(1.0, value / 100)))
            draw.rounded_rectangle(
                (origin[0] + 150, y + 6, origin[0] + 150 + filled, y + 20),
                radius=7,
                fill=(255, 255, 255),
            )
            y += 46

    # ------------------------------------------------------------------ upload
    async def archive(
        self, session: AsyncSession, character_id: int, *, chat_id: int | None = None
    ) -> dict[str, Any]:
        """Turn a rendered/generated card into a permanent Telegram file_id.

        This is the migration path for the URL-only rows the reference bot shipped:
        render → upload to the archive chat → store the file_id → future sends are
        free and can never 404.
        """
        target = chat_id or self.settings.media_archive_chat_id
        character = await char_repo.get(session, character_id)
        if character is None:
            return {"ok": False, "error": "no such character"}
        if not target:
            return {"ok": False, "error": "MEDIA_ARCHIVE_CHAT_ID is not set"}
        card = await self.image_for(session, character, force_render=True)
        source = str(card.path) if card.path else character.image_url
        if not source:
            return {"ok": False, "error": "no art to archive"}
        result = await rehost_into_chat(
            self.bot, source, target, caption=f"{character.name} #{character.id}"
        )
        if result.get("ok"):
            await char_repo.attach_media(
                session, character.id, photo_file_id=str(result["file_id"])
            )
        return result

    async def archive_missing(
        self, session: AsyncSession, *, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Batch job for the owner panel: archive art for characters lacking a file_id."""
        from sqlalchemy import select

        from waifu.db.models import Character as Model

        rows = list(
            (
                await session.execute(
                    select(Model).where(Model.photo_file_id == "").order_by(Model.id).limit(limit)
                )
            ).scalars()
        )
        out = []
        for character in rows:
            out.append(await self.archive(session, character.id))
        return out

    async def preview(
        self, character: Character
    ) -> bytes | None:  # pragma: no cover - needs Pillow
        path = self.render(character)
        return path.read_bytes() if path and path.exists() else None

    def purge_cache(self, *, older_than_days: int = 30) -> int:
        """Drop rendered cards nobody has requested in a month."""
        import time

        cutoff = time.time() - older_than_days * 86400
        removed = 0
        for path in self.cache_dir.glob("*.png"):
            try:
                if path.stat().st_atime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:  # pragma: no cover
                continue
        return removed
