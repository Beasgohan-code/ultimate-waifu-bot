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
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
from sqlalchemy.ext.asyncio import AsyncSession

from waifu.db.models import Character
from waifu.db.repo import characters as char_repo
from waifu.enums import Rarity
from waifu.services.base import Service
from waifu.tg.media import rehost_into_chat
from waifu.utils.text import truncate

log = logging.getLogger(__name__)

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


# ================================================================ profile card
#
# Ported from the reference deployment's ``plugins/profile.py`` — the module that lived in a
# ``plugins/`` package (``market``, ``nguess``, ``profile``: the newest things that bot ever
# shipped, and the only reason it had a folder structure at all). The visual grammar is kept
# because it is good: a 1000x540 canvas, a vertical gradient, a rounded portrait with a soft
# rarity *ring*, a pill badge with a gold star for the tiers that earned one, and a glow behind
# the whole card for premium players.
#
# Three things are different, all of them bugs in the original rather than choices:
#
# * the reference called ``download_fonts()`` **at import time** with blocking ``requests``, so
#   importing the module could hang the event loop for ten seconds per font; here the fetch is
#   awaited once, on demand, best-effort, and a missing font degrades to the system face;
# * drawing happened on the event loop (30-60 ms of PIL per card, per viewer); here the CPU
#   work runs in a worker thread and the result is cached by a signature of the numbers, so the
#   second player to ask for the same card costs nothing;
# * the portrait came from ``requests.get(fav_char[6])`` — an arbitrary URL the bot fetched with
#   no host check. Only allow-listed hosts (the same rule every other image in this bot obeys)
#   or the player's own Telegram photo are used.
PROFILE_SIZE = (1000, 540)
POPPIANS = {
    "Poppins-Bold.ttf": "https://github.com/google/fonts/raw/main/ofl/poppins/Poppins-Bold.ttf",
    "Poppins-Medium.ttf": "https://github.com/google/fonts/raw/main/ofl/poppins/Poppins-Medium.ttf",
    "Poppins-Regular.ttf": "https://github.com/google/fonts/raw/main/ofl/poppins/Poppins-Regular.ttf",
}
# The reference's palette, transcribed (it is the reason those cards looked like something).
BG_TOP = (20, 12, 35)
BG_BOTTOM = (8, 6, 18)
WHITE = (255, 255, 255)
DIM = (180, 170, 200)
MUTE = (130, 120, 155)
GOLD = (250, 204, 21)


@dataclass(slots=True)
class ProfileArt:
    """Everything a profile card shows, assembled by the service — the renderer does no SQL."""

    name: str = "player"
    handle: str = ""
    balance: str = "—"
    level: int = 1
    exp: int = 0
    exp_span: int = 1000
    harem: int = 0
    roster: int = 0
    value: str = "—"
    summons: int = 0
    high_rate: int = 0
    streak: int = 0
    best_streak: int = 0
    badges: int = 0
    rank: int = 0
    total_players: int = 0
    rarity_label: str = ""
    rarity_id: int = 0
    featured: str = ""
    portrait: Path | None = None
    portrait_source: str = "avatar"
    glow: bool = False
    premium: bool = False
    footer: str = ""

    def signature(self) -> str:
        """The cache key: *every* field the renderer can draw, derived rather than listed.

        Hand-writing this list is how a card cache starts leaking: the day somebody adds a field
        and the card prints it, the old PNGs are still served under the new numbers — a balance
        that was promised hidden, a completion bar from last week. ``asdict`` makes that
        impossible to forget. The portrait is hashed by *content*, because re-saving the same size
        inside one clock tick is exactly what "owner re-uploaded the art" looks like, and an
        mtime-based key serves yesterday's face under today's card.
        """
        from dataclasses import asdict

        visible = sorted(
            (key, str(value)) for key, value in asdict(self).items() if key != "portrait"
        )
        stamp = ""
        if self.portrait is not None:
            try:
                stamp = hashlib.sha256(Path(self.portrait).read_bytes()).hexdigest()[:12]
            except OSError:  # pragma: no cover - the cache tolerates a vanished file
                stamp = ""
        return hashlib.sha256(f"{visible}|{stamp}".encode()).hexdigest()[:24]


def _lerp(
    start: tuple[int, int, int], stop: tuple[int, int, int], t: float
) -> tuple[int, int, int]:
    return tuple(int(start[i] + (stop[i] - start[i]) * t) for i in range(3))  # type: ignore[return-value]


def _rounded(mask_size: tuple[int, int], radius: int):
    from PIL import Image, ImageDraw

    mask = Image.new("L", mask_size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, mask_size[0] - 1, mask_size[1] - 1), radius=radius, fill=255
    )
    return mask


class ProfileCardMixin:
    """Profile-card methods, mixed into :class:`CardService` (one cache dir, one font policy)."""

    #: The reference downloaded fonts into its repo root on import; this keeps them in the
    #: data dir and treats a failure as "use the system face", never as a crash.
    _fonts_attempted: bool = False

    @property
    def font_dir(self) -> Path:
        return Path(self.settings.data_dir) / "fonts"

    async def ensure_fonts(self) -> int:
        """Fetch the Poppins trio once if it is missing; returns how many arrived."""
        if ProfileCardMixin._fonts_attempted:
            return 0
        ProfileCardMixin._fonts_attempted = True
        directory = anyio.Path(str(self.font_dir))
        await directory.mkdir(parents=True, exist_ok=True)
        fetched = 0
        try:
            import httpx

            async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
                for name, url in POPPIANS.items():
                    target = directory / name
                    if await target.exists():
                        continue
                    try:
                        response = await client.get(url)
                        response.raise_for_status()
                        await target.write_bytes(response.content)
                        fetched += 1
                    except Exception as exc:  # a font is never worth a failed command
                        log.debug("font %s unavailable: %s", name, exc)
        except Exception as exc:  # pragma: no cover - no httpx, no fonts
            log.debug("font download skipped: %s", exc)
        return fetched

    def _profile_font(self, size: int, *, bold: bool = False):
        names = ("Poppins-Bold.ttf", "Poppins-Medium.ttf") if bold else ("Poppins-Regular.ttf",)
        for name in names:
            candidate = self.font_dir / name
            if candidate.is_file():
                try:
                    return ImageFont.truetype(str(candidate), size)  # type: ignore[arg-type]
                except OSError:  # pragma: no cover - a corrupt download
                    continue
        return self._font(size)

    def portrait_source(self, character: Character | None, *, use_art: bool) -> str:
        """Which image the portrait box will show — reported so the receipt can say why not."""
        if not use_art or character is None:
            return "avatar"
        if character.photo_file_id:
            return "art-file"
        from waifu.tg.media import is_allowed_image_url

        hosts = list(self.settings.allowed_media_hosts or [])
        if is_allowed_image_url(character.image_url, hosts=hosts):
            return "art-url"
        return "art-card"

    async def portrait_for(
        self, character: Character | None, *, user_id: int | None, use_art: bool = True
    ) -> Path | None:
        """The portrait image, in the reference's priority order but with the safety it lacked.

        Order: the favourite's stored ``file_id`` (free, permanent) → its art URL (only from an
        allow-listed host) → the player's Telegram profile photo → the *rendered* character card
        (so a roster with no web URLs still gets a portrait, which the reference could not do at
        all: it only ever read ``img_url``).
        """
        wanted = self.portrait_source(character, use_art=use_art)
        if wanted in ("art-file", "art-url") and character is not None:
            path = await self._portrait_asset(character, wanted)
            if path is not None:
                return path
        if user_id is not None and self.ctx.bot is not None:
            path = await self._telegram_avatar(int(user_id))
            if path is not None:
                return path
        if wanted == "art-card" and character is not None:
            rendered = self.render(character)
            if rendered is not None:
                return rendered
        return None

    async def _portrait_asset(self, character: Character, which: str) -> Path | None:
        directory = self.cache_dir / "portraits"
        directory.mkdir(parents=True, exist_ok=True)
        if which == "art-file":
            if self.ctx.bot is None:  # pragma: no cover - only in unit tests
                return None
            target = directory / f"tg_{character.photo_file_id[:24]}.jpg"
            if target.is_file():
                return target
            try:
                handle = await self.ctx.bot.get_file(character.photo_file_id)
                await self.ctx.bot.download(handle, destination=str(target))
            except Exception as exc:
                log.debug("portrait download failed: %s", exc)
                return None
            return target if target.is_file() else None
        from waifu.tg.media import is_allowed_image_url

        hosts = list(self.settings.allowed_media_hosts or [])
        url = str(character.image_url or "")
        if not is_allowed_image_url(url, hosts=hosts):  # pragma: no cover - guarded upstream
            return None
        target = directory / f"{hashlib.sha256(url.encode()).hexdigest()[:20]}.jpg"
        if target.is_file():
            return target
        try:
            import httpx

            async with httpx.AsyncClient(timeout=8, follow_redirects=False) as client:
                response = await client.get(url)
                response.raise_for_status()
                await anyio.Path(str(target)).write_bytes(response.content)
        except Exception as exc:
            log.debug("portrait fetch failed for %s: %s", url, exc)
            return None
        return target if target.is_file() else None

    async def _telegram_avatar(self, user_id: int) -> Path | None:
        """The player's own photo, which is what the reference fell back to as well."""
        bot = self.ctx.bot
        if bot is None:  # pragma: no cover - only in unit tests
            return None
        directory = self.cache_dir / "portraits"
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"avatar_{user_id}.jpg"
        if target.is_file():
            return target
        try:
            photos = await bot.get_user_profile_photos(user_id, limit=1)
            if not photos.photos:
                return None
            handle = await bot.get_file(photos.photos[0][-1].file_id)
            await bot.download(handle, destination=str(target))
        except Exception as exc:
            log.debug("avatar fetch failed for %s: %s", user_id, exc)
            return None
        return target if target.is_file() else None

    def render_profile(self, art: ProfileArt) -> Path | None:
        """Draw the card (synchronous: call through ``anyio.to_thread`` from a handler)."""
        if not HAVE_PIL:  # pragma: no cover - Pillow is a hard dependency, guarded anyway
            return None
        path = self.cache_dir / f"profile_{art.signature()}.png"
        if path.exists():
            return path
        try:
            image = self._draw_profile(art)
            image.save(path, "PNG", optimize=True)
        except Exception as exc:
            log.warning("profile card render failed for %s: %s", art.name, exc)
            return None
        return path if path.is_file() else None

    def _draw_profile(self, art: ProfileArt):
        from PIL import Image, ImageDraw

        width, height = PROFILE_SIZE
        base = Image.new("RGBA", (width, height), (*BG_BOTTOM, 255))
        layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
        layer_draw = ImageDraw.Draw(layer)
        for y in range(height):  # the reference's vertical gradient, one line at a time
            layer_draw.line([(0, y), (width, y)], fill=(*_lerp(BG_TOP, BG_BOTTOM, y / height), 255))
        base = Image.alpha_composite(base, layer)
        draw = ImageDraw.Draw(base)

        tint = Rarity.from_value(art.rarity_id).color if art.rarity_id else MUTE
        tint = tuple(int(c) for c in tint[:3])  # type: ignore[assignment]
        if art.glow or art.premium:
            self._glow_rect(base, (10, 10, width - 11, height - 11), tint if art.glow else GOLD)
            draw = ImageDraw.Draw(base)

        # ---- portrait, ringed by the featured character's rarity (the reference's layout)
        box = (56, 96, 356, 396)
        self._rarity_ring(base, box, tint)
        draw = ImageDraw.Draw(base)
        pasted = False
        if art.portrait is not None and art.portrait.is_file():
            try:
                with Image.open(art.portrait) as raw:
                    piece = raw.convert("RGBA").resize(
                        (box[2] - box[0], box[3] - box[1]), Image.LANCZOS
                    )
                piece.putalpha(_rounded(piece.size, 32))
                base.alpha_composite(piece, (box[0], box[1]))
                draw = ImageDraw.Draw(base)
                pasted = True
            except Exception as exc:
                log.debug("portrait paste failed: %s", exc)
        if not pasted:
            draw.rounded_rectangle(box, radius=32, fill=(30, 24, 46, 255), outline=(*tint, 160))
            initials = "".join(part[:1] for part in art.name.split()[:2]) or "?"
            draw.text(
                (box[0] + 96, box[1] + 96),
                initials.upper()[:2],
                font=self._profile_font(96, bold=True),
                fill=(*WHITE, 220),
            )
        self._rarity_badge(
            draw,
            box,
            art.rarity_label or (Rarity.from_value(art.rarity_id).name if art.rarity_id else ""),
        )

        # ---- identity. Both the name and the premium pill are measured and *right*-aligned
        # into the margin, which is why neither can be pushed off the canvas by a long name.
        premium_font = self._profile_font(20, bold=True)
        premium_w = draw.textlength("PREMIUM", font=premium_font) + 24 if art.premium else 0
        draw.text(
            (400, 52),
            self._fit(draw, art.name, self._profile_font(56, bold=True), 940 - 400 - premium_w),
            font=self._profile_font(56, bold=True),
            fill=(*WHITE, 255),
        )
        if art.handle:
            draw.text(
                (400, 122),
                self._fit(draw, art.handle, self._profile_font(28), 460),
                font=self._profile_font(28),
                fill=(*DIM, 255),
            )
        if art.premium:
            x1 = 950 - premium_w
            draw.rounded_rectangle((x1, 60, 950, 96), radius=14, fill=(*GOLD, 255))
            draw.text((x1 + 12, 68), "PREMIUM", font=premium_font, fill=(15, 10, 25, 255))

        # ---- level bar (the reference drew its progress with ▰▱; a real bar reads better at 1000px)
        bar_box = (400, 160, 950, 188)
        draw.rounded_rectangle(bar_box, radius=12, fill=(255, 255, 255, 26))
        span = max(1, int(art.exp_span))
        filled = bar_box[0] + int((bar_box[2] - bar_box[0]) * max(0.0, min(1.0, art.exp / span)))
        draw.rounded_rectangle(
            (bar_box[0], bar_box[1], max(bar_box[0] + 12, filled), bar_box[3]),
            radius=12,
            fill=(*tint, 255),
        )
        draw.text((400, 196), f"level {art.level}", font=self._profile_font(22), fill=(*DIM, 255))
        xp = f"{art.exp}/{span} xp"
        draw.text(
            (950 - draw.textlength(xp, font=self._profile_font(22)), 196),
            xp,
            font=self._profile_font(22),
            fill=(*MUTE, 255),
        )

        # ---- the numbers, in two columns like the original
        rows_left = [
            ("harem", f"{art.harem}" + (f"/{art.roster}" if art.roster else "")),
            ("value", art.value),
            ("summons", f"{art.summons} ({art.high_rate}% high)"),
        ]
        rows_right = [
            ("balance", art.balance),
            ("streak", f"{art.streak}d (best {art.best_streak})"),
            ("badges", str(art.badges)),
        ]
        for index, (label, value) in enumerate(rows_left):
            self._row(draw, 400, 250 + index * 58, label, value, width=270)
        for index, (label, value) in enumerate(rows_right):
            self._row(draw, 700, 250 + index * 58, label, value, width=250)

        if art.featured:
            draw.text(
                (56, 424),
                self._fit(draw, f"featured · {art.featured}", self._profile_font(24), 640),
                font=self._profile_font(24),
                fill=(*DIM, 255),
            )
        # Clamped: a roster that *shrinks* (an owner deleting characters) would otherwise
        # advertise 1040% completion, which reads as a bug in the card, not in the data.
        completion = min(100.0, art.harem / art.roster * 100) if art.roster else 0.0
        line = f"{completion:.1f}% of the roster"
        if art.total_players:
            line += f" · rank #{art.rank} of {art.total_players} by coins"
        draw.text(
            (56, 468),
            line,
            font=self._profile_font(24, bold=True),
            fill=(*(GOLD if completion >= 25 else WHITE), 240),
        )
        if art.footer:
            draw.text((56, 505), art.footer, font=self._profile_font(18), fill=(*MUTE, 255))
        draw.text(
            (width - 250, 505),
            self._fit(draw, "ultimate waifu bot", self._profile_font(18), 220),
            font=self._profile_font(18),
            fill=(*MUTE, 255),
        )
        return base.convert("RGB")

    async def profile_art(
        self,
        session: AsyncSession,
        user_id: int,
        *,
        viewer: int | None = None,
        force_glow: bool | None = None,
    ) -> ProfileArt:
        """Assemble a card's numbers from the same bundles ``/profile`` renders.

        The privacy flag is honoured here rather than in the renderer, because a viewer
        who should not see a balance must not get it back from the Mini App or a cached
        PNG either: the masked value is what is hashed into the signature.
        """
        from waifu.db.repo import characters as char_repo
        from waifu.db.repo import progress as progress_repo
        from waifu.db.repo import users as user_repo

        bundle = await self.ctx.collection.profile(session, user_id)
        totals = await char_repo.totals(session)
        counts = await user_repo.counts(session)
        badges = len(await progress_repo.unlocked(session, user_id))
        prefs = await user_repo.prefs(session, user_id)
        flags = prefs.flags or {}

        show_balance = bool(bundle.get("show_balance")) if viewer != user_id else True
        balance = f"{bundle['balance']:,}" if show_balance else "hidden"
        pulls = int(bundle.get("pulls") or 0)
        high = int(bundle.get("high_pulls") or 0)

        character = None
        favourite = bundle.get("favourite") or ""
        owned = await self.ctx.collection.favourite(session, user_id)
        if owned is not None:
            character = await char_repo.get(session, owned.character_id)
        use_art = str(flags.get("card_portrait") or "art") == "art"
        portrait = await self.portrait_for(character, user_id=user_id, use_art=use_art)

        span = max(1000, int(bundle["level"]) * 1000)
        return ProfileArt(
            name=str(bundle.get("display") or user_id).lstrip("@"),
            handle=f"@{bundle['username']}" if bundle.get("username") else f"id {user_id}",
            balance=balance,
            level=int(bundle["level"]),
            exp=int(bundle["exp"]),
            exp_span=span,
            harem=int((bundle.get("collection") or {}).get("unique") or 0),
            roster=int(totals.get("characters") or 0),
            value=f"{(bundle.get('collection') or {}).get('value') or 0:,}",
            summons=pulls,
            high_rate=int(high / pulls * 100) if pulls else 0,
            streak=int(bundle.get("streak") or 0),
            best_streak=int(bundle.get("streak_best") or 0),
            badges=badges,
            rank=int(bundle.get("coins_rank") or 0),
            total_players=int(counts.get("users") or 0),
            rarity_label=str(
                (character.rarity if character else "") or (owned.rarity if owned else "")
            ),
            rarity_id=int(character.rarity_id if character else (owned.rarity_id if owned else 0)),
            featured=favourite,
            portrait=portrait,
            portrait_source=self.portrait_source(character, use_art=use_art),
            glow=bool(bundle.get("glow")) if force_glow is None else bool(force_glow),
            premium=bool(bundle.get("premium_hours_left")),
            footer=str(bundle.get("bio") or ""),
        )

    async def render_profile_async(self, art: ProfileArt) -> Path | None:
        """``render_profile`` off the loop: PIL is CPU work and belongs in a worker thread."""
        return await anyio.to_thread.run_sync(self.render_profile, art)

    def _fit(self, draw, text: str, font, max_width: int) -> str:
        """``smart_truncate`` from the reference, verbatim behaviour (shrink until it fits)."""
        if not text:
            return ""
        while len(text) > 1 and draw.textlength(text + "…", font=font) > max_width:
            text = text[:-1]
        return text + "…" if draw.textlength(text, font=font) > max_width else text

    def _row(self, draw, x: int, y: int, label: str, value: str, *, width: int) -> None:
        draw.text((x, y), label, font=self._profile_font(20), fill=(*MUTE, 255))
        draw.text(
            (x, y + 22),
            self._fit(draw, str(value), self._profile_font(26, bold=True), width),
            font=self._profile_font(26, bold=True),
            fill=(*WHITE, 250),
        )

    def _glow_rect(self, image, xy, color, *, radius: int = 18, size: int = 8) -> None:
        """``draw_glow_rect`` ported: expanding translucent outlines, blurred, then a sharp line."""
        from PIL import Image, ImageDraw, ImageFilter

        overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        for step in range(size, 0, -1):
            alpha = int(90 * (1 - step / size))
            expand = (size - step) * 2
            draw.rounded_rectangle(
                (xy[0] - expand, xy[1] - expand, xy[2] + expand, xy[3] + expand),
                radius=radius + expand,
                outline=(*color[:3], alpha),
                width=2,
            )
        image.alpha_composite(overlay.filter(ImageFilter.GaussianBlur(radius=3)))
        ImageDraw.Draw(image).rounded_rectangle(
            xy, radius=radius, outline=(*color[:3], 255), width=3
        )

    def _rarity_ring(self, image, box, color) -> None:
        """``draw_rarity_ring`` ported: soft outer glow, hard ring, faint inner highlight."""
        from PIL import Image, ImageDraw, ImageFilter

        pad = 6
        glow_size = 10
        overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        for step in range(glow_size, 0, -1):
            alpha = int(85 * (1 - step / glow_size))
            expand = (glow_size - step) * 2
            draw.rounded_rectangle(
                (
                    box[0] - pad - expand,
                    box[1] - pad - expand,
                    box[2] + pad + expand,
                    box[3] + pad + expand,
                ),
                radius=32 + pad + expand,
                outline=(*color[:3], alpha),
                width=3,
            )
        image.alpha_composite(overlay.filter(ImageFilter.GaussianBlur(radius=4)))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle(
            (box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad),
            radius=32 + pad,
            outline=(*color[:3], 255),
            width=3,
        )
        draw.rounded_rectangle(
            (box[0] + 5, box[1] + 5, box[2] - 5, box[3] - 5),
            radius=27,
            outline=(*WHITE, 40),
            width=1,
        )

    def _rarity_badge(self, draw, box, label: str) -> None:
        """Pill badge in the corner of the portrait + the gold star for tiers that earn one."""
        if not label:
            return
        tier = Rarity.from_label(label)
        color = tuple(int(c) for c in tier.color[:3])
        font = self._profile_font(20, bold=True)
        text = label.upper()
        pad_x, pad_y = 12, 8
        x1, y1 = box[0] + 12, box[1] + 12
        text = self._fit(draw, text, font, box[2] - x1 - 48)
        x2 = x1 + draw.textlength(text, font=font) + pad_x * 2
        y2 = y1 + 20 + pad_y * 2
        draw.rounded_rectangle((x1, y1, x2, y2), radius=14, fill=(*color, 255))
        draw.text((x1 + pad_x, y1 + pad_y - 2), text, font=font, fill=(15, 10, 25, 255))
        if tier.is_high_tier:
            self._star(draw, (x2 + 6, y1 + 4))

    @staticmethod
    def _star(draw, origin) -> None:
        """The reference's nine-point gold star polygon, for the tiers worth bragging about."""
        x, y = origin
        points = [
            (x + 6, y),
            (x + 7, y + 5),
            (x + 12, y + 5),
            (x + 8, y + 8),
            (x + 10, y + 14),
            (x + 6, y + 10),
            (x + 2, y + 14),
            (x + 4, y + 8),
            (x, y + 5),
            (x + 5, y + 5),
        ]
        draw.polygon(points, fill=(*GOLD, 255))


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


class CardService(ProfileCardMixin, Service):
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
