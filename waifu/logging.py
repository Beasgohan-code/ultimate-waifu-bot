"""Logging setup.

Summon-bot mixed ``print()`` with ``logging`` and committed ``bot.log`` (which
leaked the live bot token 81 times). Two rules fix that class of problem:

1. Only ``logging`` is used, and it is configured in exactly one place.
2. A redaction filter scrubs token-shaped strings from every record.
"""

from __future__ import annotations

import logging
import re
import sys
from datetime import UTC, datetime

# "<bot id>:<35-char secret>" and typical URL/path encodings of it.
_TOKEN_RE = re.compile(r"\b(\d{8,12}):([A-Za-z0-9_-]{20,})\b")
# Any long query-string secret.
_SECRET_KW_RE = re.compile(
    r"(?i)\b(token|secret|password|api[_-]?key|authorization)=([^&\s\"']+)",
)


class RedactFilter(logging.Filter):
    """Never let credentials reach a log handler, whatever the caller logged."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # pragma: no cover - defensive
            return True
        safe = _TOKEN_RE.sub(r"\1:***redacted***", msg)
        safe = _SECRET_KW_RE.sub(lambda m: f"{m.group(1)}=***", safe)
        if safe != msg:
            # Replace both msg and args so downstream formatters stay consistent.
            record.msg = safe
            record.args = ()
        return True


class LevelFormatFormatter(logging.Formatter):
    """Human-readable in a terminal, single-line-safe, UTC timestamps."""

    def format(self, record: logging.LogRecord) -> str:
        record.created_utc = datetime.fromtimestamp(record.created, tz=UTC).strftime(
            "%Y-%m-%d %H:%M:%SZ"
        )
        record.level = record.levelname[0]
        if record.exc_info:
            record.exc_text = self.formatException(record.exc_info)
        name = record.name
        if name.startswith("waifu."):
            name = name[len("waifu.") :]
        return (
            f"{record.created_utc} {record.level:<1} [{name}:{record.lineno}] {record.getMessage()}"
        )


_NOISY = {
    "aiogram.event": logging.WARNING,
    "aiogram.dispatcher": logging.INFO,
    "aiogram.middlewares": logging.WARNING,
    "aiohttp.access": logging.WARNING,
    "httpx": logging.WARNING,
    "httpcore": logging.WARNING,
    "asyncio": logging.WARNING,
    "PIL": logging.WARNING,
}


def setup_logging(level: str = "INFO", *, json_logs: bool = False) -> logging.Logger:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(RedactFilter())
    if json_logs:
        handler.setFormatter(
            logging.Formatter(
                '{"t":"%(asctime)s","lvl":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}'
            )
        )
    else:
        handler.setFormatter(LevelFormatFormatter())
    root.addHandler(handler)

    for name, lvl in _NOISY.items():
        logging.getLogger(name).setLevel(lvl)
    return root


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name if name.startswith("waifu") else f"waifu.{name}")
