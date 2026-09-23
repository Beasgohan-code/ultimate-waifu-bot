"""Misc small utilities."""

from __future__ import annotations

import fnmatch
import re
from urllib.parse import urlparse

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def chunked(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def strip_html(text: str) -> str:
    return _HTML_TAG_RE.sub("", text)


def safe_url(url: str, allowed_hosts: list[str]) -> bool:
    """Guard against SSRF/redirect-to-evil-host when admins add character art.

    Only http(s) is accepted, credentials in the URL are rejected, and the host
    must match one of the ``ALLOWED_MEDIA_HOSTS`` globs.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    if parsed.username or parsed.password:
        return False
    if not parsed.netloc:
        return False
    if not allowed_hosts:
        return True  # no policy configured -> host opt-in disabled
    host = parsed.hostname or ""
    return any(fnmatch.fnmatch(host, pattern) for pattern in allowed_hosts)


def pct(part: float, whole: float) -> float:
    return (part / whole * 100.0) if whole else 0.0


def parse_id(raw: str | int | None) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, int):
        return raw
    raw = raw.strip().lstrip("@").strip()
    try:
        return int(raw)
    except ValueError:
        return None


def ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def bar(ratio: float, width: int = 10, filled: str = "█", empty: str = "░") -> str:
    ratio = max(0.0, min(1.0, ratio))
    count = round(ratio * width)
    return filled * count + empty * (width - count)


def humanize(key: str) -> str:
    return key.replace("_", " ").strip().title()
