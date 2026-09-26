"""Parsing the ``at …`` argument of ``/broadcast at 20:00 …``.

Deliberately small — the formats a human actually types into a chat box:

* ``20:00`` — today if still ahead, otherwise tomorrow
* ``20:00 tomorrow`` — explicitly the next day
* ``+2h`` / ``+30m`` / ``in 2h 30m`` — relative to now
* ``2026-09-30 20:00`` — a full date (date-only means 00:00 UTC)

Anything else is rejected with a message that shows the working formats,
because a scheduled broadcast that silently lands three hours early is a
support ticket, and one that never lands is worse.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from waifu.utils.time import now_utc

#: How far a future time must be in the future (fights typing a broadcast for
#: "in 5 minutes" during the last minute before the server is restarted).
MIN_LEAD = timedelta(minutes=1)

_CLOCK = re.compile(r"^(\d{1,2}):(\d{2})$")
_RELATIVE = re.compile(
    r"^\s*\+?(?:(?:in)\s+)?(\d{1,3})\s*(h|hr|hrs|hour|hours|m|min|mins|minute|minutes|d|day|days)\s*$",
    re.I,
)
_FULL = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{1,2}):(\d{2}))?$")


class ScheduleError(ValueError):
    """The argument is not a time this bot understands (message is user-facing)."""

    def explain(self) -> str:
        return (
            "I don't read that as a time. Formats: <code>20:00</code>, "
            "<code>20:00 tomorrow</code>, <code>+2h</code>, <code>in 30m</code>, "
            "<code>2026-12-31 20:00</code>."
        )


def parse_when(raw: str) -> datetime:
    """``at`` argument → naive UTC datetime in the future (the DB convention).

    Raises :class:`ScheduleError` whose message is user-facing.
    """
    text = raw.strip().lower()
    now = now_utc()

    if text.endswith(" tomorrow"):
        clock = _CLOCK.match(text[: -len(" tomorrow")].strip())
        if not clock:
            raise ScheduleError()
        hour, minute = int(clock.group(1)), int(clock.group(2))
        candidate = _at_wall_clock(now, hour, minute, force_next_day=True)
    elif clock := _CLOCK.match(text):
        hour, minute = int(clock.group(1)), int(clock.group(2))
        candidate = _at_wall_clock(now, hour, minute, force_next_day=False)
    elif rel := _RELATIVE.match(text):
        amount, unit = int(rel.group(1)), rel.group(2).lower()
        if unit.startswith("h"):
            candidate = now + timedelta(hours=amount)
        elif unit.startswith("m"):
            candidate = now + timedelta(minutes=amount)
        else:
            candidate = now + timedelta(days=amount)
    elif full := _FULL.match(text):
        year, month, day = int(full.group(1)), int(full.group(2)), int(full.group(3))
        hour = int(full.group(4) or 0)
        minute = int(full.group(5) or 0)
        try:
            candidate = datetime(year, month, day, hour, minute)
        except ValueError:
            raise ScheduleError() from None
    else:
        raise ScheduleError()

    if candidate <= now + MIN_LEAD:
        raise ScheduleError(f"that moment already passed — {candidate:%Y-%m-%d %H:%M UTC}")
    return candidate


def _at_wall_clock(now: datetime, hour: int, minute: int, *, force_next_day: bool) -> datetime:
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ScheduleError()
    today = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if today <= now or force_next_day:
        today += timedelta(days=1)
    return today
