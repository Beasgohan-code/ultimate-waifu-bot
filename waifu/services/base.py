"""Services: the application layer between handlers and repositories.

Rules that make this codebase reviewable:

* **Handlers never touch the DB directly.** They parse input, call one service
  method, and render the result. That is what makes the same flow reusable from
  a job (auction settlement), the Mini App (``/api``) and the CLI.
* **A service method owns one transaction** (or accepts an existing session when
  it must compose with another service in the same tx). Money moves only through
  :mod:`waifu.services.economy`, which means exactly one place can change a
  balance — the bug class that let Summon-bot's ``/rob`` mint coins.
* **Services return dataclasses, not messages.** Rendering lives in
  :mod:`waifu.ui`, so a card can be re-rendered as a rich message, an album or a
  plain caption without touching business logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from waifu.core.context import AppContext
from waifu.db.redis_client import Redis
from waifu.settings import Settings

if TYPE_CHECKING:  # pragma: no cover
    from waifu.db.models import Character


@dataclass(slots=True)
class Service:
    """Common plumbing: settings, db session factory, redis, bot access."""

    ctx: AppContext

    @property
    def settings(self) -> Settings:
        return self.ctx.settings

    @property
    def redis(self) -> Redis | None:
        return self.ctx.redis

    @property
    def bot(self):
        return self.ctx.bot

    # ------------------------------------------------------------- small shared
    def local_day(self, utc_offset_hours: int = 0) -> str:
        from waifu.utils.time import local_date

        return local_date(offset_hours=utc_offset_hours, tz_name=self.settings.timezone)

    def seconds_until_reset(self, utc_offset_hours: int = 0, *, hour: int | None = None) -> int:
        """Seconds until the next daily reset in *the player's* day boundary.

        A fixed ``24 * 3600`` cooldown is what makes "daily" feel wrong for half
        the playership; resetting at a wall-clock hour per timezone means everyone
        sees the same "resets in 4h 12m".
        """
        from datetime import datetime, timedelta

        from waifu.utils.time import local_tz

        zone = local_tz(self.settings.timezone)
        if utc_offset_hours:
            zone = timedelta(hours=utc_offset_hours)  # type: ignore[assignment]
        now = datetime.now(zone)
        reset_hour = self.settings.daily_reset_hour if hour is None else hour
        midnight = now.replace(hour=reset_hour, minute=0, second=0, microsecond=0)
        if now >= midnight:
            midnight += timedelta(days=1)
        return max(1, int((midnight - now).total_seconds()))

    async def notify(
        self, user_id: int, text: str, *, buttons: Any = None, silent: bool = False
    ) -> bool:
        """DM a user, tolerating "bot blocked". Used by trades, auctions, raffles.

        Returns False rather than raising when the DM fails: a player who blocked
        the bot must not be able to abort a settlement loop halfway through.
        """
        from waifu.tg.notify import safe_send

        result = await safe_send(
            self.bot, user_id, text, reply_markup=buttons, disable_notification=silent
        )
        return result.ok

    async def deliver_character_dm(
        self,
        user_id: int,
        character: Character,
        html: str,
        *,
        silent: bool = True,
        rich: Any | None = None,
    ) -> bool:
        """DM a character to a player *with its art* (gift receipts, paid unlocks).

        The image falls back ``photo_file_id`` → ``image_url``; if Telegram cannot
        send the media (a dead hotlink, a blocked file id) the message is retried
        as text so the player still gets the notification. Returns ``True`` when
        anything reached the player. Never raises: a gift that fails to picture
        is a degraded receipt, not an exception in the middle of a transfer.
        """
        if self.bot is None or character is None:
            return False
        from waifu.tg.media import resolve_media
        from waifu.tg.notify import SendOutcome, safe_send

        photo = resolve_media(character.photo_file_id or character.image_url)
        rich = rich if self.ctx.caps.rich_messages else None
        # ``safe_send`` tries the rich layout first (when given one) and falls
        # back to photo + this HTML caption, so the player always gets the DM.
        result = await safe_send(
            self.bot, user_id, html, photo=photo, rich=rich, disable_notification=silent
        )
        if result.outcome is SendOutcome.ERROR and (photo is not None or rich is not None):
            result = await safe_send(self.bot, user_id, html, disable_notification=silent)
        return result.ok

    async def log_line(
        self, text: str, *, silent: bool = False, as_html: bool = False, rich: Any | None = None
    ) -> None:
        """Copy an event into the log channel — for *staff*, not for players.

        Never fed user-authored text (a group name or a reason string is
        escaped by the caller); that is what keeps the channel unspoofable.
        Delegates to :meth:`AppContext.notify` so every send — service or
        handler — is counted in ``ctx.log_stats``; ``rich`` is an optional
        :class:`InputRichMessage` (see :func:`waifu.tg.rich.rich_log`) that
        renders natively on servers with the feature.
        """
        await self.ctx.notify(text, silent=silent, html=as_html, rich=rich)

    async def broadcast_channel(self, html: str) -> None:
        await self.log_line(html, as_html=True)


__all__ = ["AppContext", "Service"]
