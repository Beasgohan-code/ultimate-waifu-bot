"""Time helpers.

Everything in the database is stored as *naive UTC*. SQLite cannot round-trip
``timezone=True`` datetimes, while Postgres can, so normalising once here keeps
both backends honest. Rollover boundaries use the server-local timezone from
``Settings.timezone`` so "daily at 4am" means 4am for the community, not UTC.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None  # type: ignore[assignment]

_EPOCH = datetime(1970, 1, 1)


def now_utc() -> datetime:
    """Naive UTC now — the canonical value written to the DB."""
    return datetime.now(UTC).replace(tzinfo=None)


def now_aware() -> datetime:
    return datetime.now(UTC)


def to_naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def to_timestamp(value: datetime) -> int:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int((value - _EPOCH).total_seconds())


def from_timestamp(ts: int | float) -> datetime:
    return datetime.fromtimestamp(ts, tz=UTC).replace(tzinfo=None)


def local_tz(name: str) -> timezone:
    if ZoneInfo is not None:
        try:
            return ZoneInfo(name)  # type: ignore[return-value]
        except Exception:  # pragma: no cover - bad tz string in env
            pass
    return UTC  # pragma: no cover


def local_day_start(offset_hours: int = 0, tz_name: str = "UTC") -> datetime:
    """Start of "today" in the given timezone, shifted by rollover hours.

    ``offset_hours=4`` with ``tz_name='Asia/Kolkata'`` means the daily resets at
    04:00 IST, and is expressed back as naive UTC for storage.
    """
    tz = local_tz(tz_name)
    local_now = datetime.now(tz)
    local_now = local_now - timedelta(hours=offset_hours)
    start_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return (start_local + timedelta(hours=offset_hours)).astimezone(UTC).replace(tzinfo=None)


def local_week_start(offset_hours: int = 0, tz_name: str = "UTC") -> datetime:
    start = local_day_start(offset_hours, tz_name)
    return start - timedelta(days=start.weekday())


def local_date(offset_hours: int = 0, tz_name: str = "UTC") -> str:
    tz = local_tz(tz_name)
    return (datetime.now(tz) - timedelta(hours=offset_hours)).date().isoformat()


def is_same_local_day(a: datetime, b: datetime, *, offset_hours: int, tz_name: str) -> bool:
    tz = local_tz(tz_name)
    la = to_naive_utc(a).replace(tzinfo=UTC).astimezone(tz).date()
    lb = to_naive_utc(b).replace(tzinfo=UTC).astimezone(tz).date()
    la = la - timedelta(hours=0) if offset_hours == 0 else la
    return la == lb


def human_delta(seconds: float | None) -> str:
    """`3h 12m` style remaining-time text used all over the UI."""
    if seconds is None:
        return "now"
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    parts = []
    for label, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            parts.append(f"{seconds // size}{label}")
            seconds %= size
    if not parts:
        return f"{seconds}s"
    return " ".join(parts)


def until(next_reset: datetime | None) -> int:
    if next_reset is None:
        return 0
    delta = (to_naive_utc(next_reset) - now_utc()).total_seconds()
    return max(0, int(delta))


def parse_duration(raw: str) -> int:
    """`90`, `5m`, `2h30m`, `3d` -> seconds. Used by admin time commands."""
    raw = raw.strip().lower()
    if raw.isdigit():
        return int(raw)
    total = 0
    num = ""
    unit_map = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    for char in raw:
        if char.isdigit():
            num += char
        elif char in unit_map:
            if not num:
                raise ValueError(f"bad duration: {raw!r}")
            total += int(num) * unit_map[char]
            num = ""
        elif char in " ,and":
            continue
        else:
            raise ValueError(f"unknown unit {char!r} in {raw!r}")
    if not total:
        raise ValueError(f"bad duration: {raw!r}")
    return total


def start_of_week(value: datetime | None = None) -> datetime:
    """Monday 00:00 of the given (or current) week, in ``value``'s own tz-awareness."""
    moment = value or now_utc()
    return (moment - timedelta(days=moment.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def start_of_month(value: datetime | None = None) -> datetime:
    moment = value or now_utc()
    return moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
