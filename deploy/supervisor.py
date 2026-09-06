#!/usr/bin/env python3
"""Run ``python -m waifu bot`` with crash recovery and optional scheduled recycling.

Ported from the reference deployment's ``supervisor.py``, with the same knobs and the same
default: ``AUTO_RESTART_MINUTES=0`` means "restart only when it died", never "recycle on a
timer" — a periodic restart is only ever the answer to a provider with a known leak.

The one thing added is a floor under the loop. The reference restarted *forever* after a
10-second delay, which on a fatal configuration error (bad ``DATABASE_URL``, expired token)
turns into an endless chain of identical tracebacks in the log and a bot that never came up.
Here ``MAX_QUICK_FAILURES`` consecutive exits inside ``QUICK_FAILURE_SECONDS`` stop the
supervisor with the child's exit status, so a process manager above (systemd's ``Restart=``,
Docker's ``restart:`` policy) either handles it or the container visibly exits.

    python deploy/supervisor.py                       # with the repo's .env
    AUTO_RESTART_MINUTES=15 python deploy/supervisor.py

Ctrl-C and SIGTERM stop the child cleanly (``terminate`` then ``kill`` after 30s), which is
what a systemd ``TimeoutStopSec=45`` unit expects.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODULE = ["-m", "waifu", "bot"]
#: A dict rather than a module global: signal handlers can only mutate what they already
#: reference, and ``STOP["requested"]`` survives ruff's (correct) distrust of ``global``.
STOP: dict[str, bool] = {"requested": False}


def _stop(_signum: int, _frame: object) -> None:
    STOP["requested"] = True


def _interval_minutes() -> int:
    raw = os.getenv("AUTO_RESTART_MINUTES", "0").strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise SystemExit(
            "AUTO_RESTART_MINUTES must be 0, 10, 15, or another positive integer"
        ) from exc
    if value < 0:
        raise SystemExit("AUTO_RESTART_MINUTES cannot be negative")
    return value


def _env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


def run() -> int:
    interval_seconds = _interval_minutes() * 60
    restart_delay = max(5, _env("RESTART_DELAY_SECONDS", 10))
    quick_window = max(15, _env("QUICK_FAILURE_SECONDS", 60))
    max_quick = max(1, _env("MAX_QUICK_FAILURES", 3))

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    quick_failures = 0
    while not STOP["requested"]:
        started = time.monotonic()
        child = subprocess.Popen(  # noqa: S603 - this file's own module, no shell, fixed argv
            [sys.executable, *MODULE], cwd=ROOT
        )
        scheduled_restart = False
        try:
            while child.poll() is None and not STOP["requested"]:
                if interval_seconds and time.monotonic() - started >= interval_seconds:
                    scheduled_restart = True
                    child.terminate()
                    break
                time.sleep(1)
        finally:
            if STOP["requested"] and child.poll() is None:
                child.terminate()
            try:
                child.wait(timeout=30)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()

        if STOP["requested"]:
            return 0
        stop_now, quick_failures, exit_code = _settle(
            child,
            started=started,
            scheduled_restart=scheduled_restart,
            quick_failures=quick_failures,
            quick_window=quick_window,
            max_quick=max_quick,
        )
        if stop_now:
            return exit_code
        time.sleep(restart_delay)

    return 0


def _settle(
    child: subprocess.Popen[int],
    *,
    started: float,
    scheduled_restart: bool,
    quick_failures: int,
    quick_window: int,
    max_quick: int,
) -> tuple[bool, int, int]:
    """Decide what a dead child means: ``(stop_now, quick_failures, exit_code)``.

    Split out because the whole judgment fits in five lines and the loop around it should not
    have to carry it — and because the *stop* case is the reason this supervisor exists at all:
    an endless restart of a bot that cannot read its own config looks like resilience in a log
    and is an outage.
    """
    alive_for = time.monotonic() - started
    status = int(child.returncode or 0)
    if scheduled_restart:
        # A recycling restart is not a failure; the clock below starts over.
        print("scheduled restart (AUTO_RESTART_MINUTES)", flush=True)
        return False, 0, status
    if not status:
        print("bot stopped cleanly; nothing to restart", flush=True)
        return True, 0, 0
    print(f"bot exited with status {status}; restarting", flush=True)
    if alive_for < quick_window:
        quick_failures += 1
        if quick_failures >= max_quick:
            print(
                f"bot died {quick_failures} times within {int(alive_for)}s each — that is a "
                "configuration or dependency failure, not a crash to ride out. Run "
                "`python -m waifu doctor`; supervisor exiting with the child's status.",
                flush=True,
            )
            return True, quick_failures, status
    return False, quick_failures, status
