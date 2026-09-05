"""The timer loop: auto-spawn, expiries, auction settlement, nightly resets.

Summon-bot split these across three places: ``auto_spawn.py`` was a second process
started by a shell script (and silently stopped whenever it crashed, so groups simply
stopped getting spawns and the owner found out from complaints days later), auction
settlement ran in whoever's handler happened to fire first, and ``/nguess`` held the
round open inside its own ``asyncio.sleep``.

Here every timed behaviour is a *pass* — one short function that takes the due rows,
does the work, and returns counters — driven from a single loop with per-pass
intervals. Two consequences that matter:

* a pass is also runnable by hand (``python -m waifu jobs --name autospawn``), so cron
  or Kubernetes CronJob can drive the whole bot without any resident process, which is
  how a free-tier deployment keeps working when the web process restarts;
* passes are idempotent and bounded: they take at most ``limit`` rows and each claim is
  an atomic ``UPDATE … WHERE`` — so two instances running the same loop cannot
  double-settle an auction or double-open a spawn (the legacy bot's worst incident).

``ctx.bot is None`` (doctor/tests) makes every pass a no-op read, so nothing here
requires a live Telegram connection.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from waifu.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from waifu.core.context import AppContext

log = get_logger("core.jobs")

#: seconds between passes — short enough that a 60 s spawn timer feels live, long
#: enough that an idle instance does zero work (each pass is one indexed read).
INTERVALS: dict[str, int] = {
    "autospawn": 5,
    "spawns": 15,
    "auctions": 20,
    "trades": 60,
    "codes": 3600,
    "raffles": 30,
    "nightly": 3600,
    "integrity": 900,
}


@dataclass(slots=True)
class PassResult:
    name: str
    counters: dict[str, int] = field(default_factory=dict)
    seconds: float = 0.0
    error: str = ""

    def __str__(self) -> str:
        base = f"{self.name}: " + (
            ", ".join(f"{k}={v}" for k, v in self.counters.items()) or "idle"
        )
        return (
            base
            + (f" ({self.seconds:.2f}s)" if self.seconds >= 0.5 else "")
            + (f" ⚠ {self.error}" if self.error else "")
        )


async def _autospawn(ctx: AppContext, *, limit: int) -> dict[str, int]:
    """Open and post the spawn for every chat whose message counter reached its limit."""
    opened = sent = 0
    async with ctx.db.tx() as session:
        due = await ctx.spawn.due_autospawns(session, limit=limit)
        chats = list(due)
    for chat_id in chats:
        try:
            async with ctx.db.tx() as session:
                spawn, _character = await ctx.spawn.open(session, chat_id, source="auto")
                view = await ctx.spawn.view(session, spawn)
            if ctx.bot is None:
                opened += 1
                continue
            result = await ctx.spawn.announce(chat_id, view)
            opened += 1
            sent += 1 if getattr(result, "ok", False) else 0
        except Exception as exc:
            log.warning("autospawn in %s failed: %s", chat_id, exc)
    return {"chats": len(chats), "opened": opened, "sent": sent}


async def _spawns(ctx: AppContext, *, limit: int) -> dict[str, int]:
    """Expire unclaimed spawns and close guessing rounds whose clock ran out."""
    expired = rounds = 0
    async with ctx.db.tx() as session:
        expired = await ctx.spawn.expire_overdue(session)
        pending = list(await ctx.spawn.guesses_to_close(session))
        for guess in pending[:limit]:
            await ctx.spawn.resolve_guess(
                session,
                int(getattr(guess, "id", 0)),
                winner_id=None,
                answers=int(getattr(guess, "answer_count", 0) or 0),
            )
            rounds += 1
    return {"expired": expired, "rounds_closed": rounds}


async def _auctions(ctx: AppContext, *, limit: int) -> dict[str, int]:
    """Settle every auction past its end time (one candidate per read, bounded loop)."""
    settled = 0
    for _ in range(limit):
        async with ctx.db.tx() as session:
            auction = await ctx.auctions.due(session)
            if auction is None:
                break
            auction_id = int(getattr(auction, "id", 0))
            outcome = await ctx.auctions.settle(session, auction_id)
        settled += 1
        sold = bool(outcome.get("sold"))
        await ctx.notify(
            (
                "🔨 sold: "
                + str(outcome.get("name", ""))
                + f" for {int(outcome.get('amount', 0) or 0):,} 🪙"
            )
            if sold
            else f"🔨 {outcome.get('name', 'auction')} ended with no bids — refunds issued"
        )
        log.info("auction %s settled (%s)", auction_id, "sold" if sold else "no sale")
    return {"settled": settled}


async def _trades(ctx: AppContext, *, limit: int) -> dict[str, int]:
    async with ctx.db.tx() as session:
        stale = await ctx.trades.expire_stale(session)
        purged = await ctx.codes.purge_expired(session)
    return {"trades_expired": int(stale or 0), "codes_purged": int(purged or 0)}


async def _codes(ctx: AppContext, *, limit: int) -> dict[str, int]:
    """Codes service lives in :mod:`waifu.services.trading`; sweeps expired redeem codes."""
    async with ctx.db.tx() as session:
        revoked = await ctx.codes.purge_expired(session)
    return {"codes_expired": int(revoked or 0)}


async def _raffles(ctx: AppContext, *, limit: int) -> dict[str, int]:
    """Draw every expired premium raffle (reactions + votes both counted)."""
    draws = 0
    if ctx.premium is not None:
        async with ctx.db.tx() as session:
            draws = len(list(await ctx.premium.draw_raffles(session)))
    return {"raffles_drawn": draws}


async def _nightly(ctx: AppContext, *, limit: int) -> dict[str, int]:
    async with ctx.db.tx() as session:
        broken = await ctx.progress.reset_streaks(session)
    return {"streaks_reset": int(broken or 0)}


async def _integrity(ctx: AppContext, *, limit: int) -> dict[str, int]:
    problems: list[str] = []
    async with ctx.db.tx() as session:
        problems = list(await ctx.economy.integrity(session))
        archived = await ctx.cards.archive_missing(session, limit=min(5, limit))
    if problems:
        # Loud in the log, quiet in the chat: a mismatch is an operator's bug (or a
        # player's exploit) and both want the detail, not a public accusation.
        log.error("ledger integrity: %d mismatch(es) — %s", len(problems), problems[0])
        await ctx.notify("🛡️ ledger mismatch detected — /integrity has the detail", silent=True)
    return {"mismatches": len(problems), "art_archived": len(archived)}


PASSES: dict[str, Callable[..., Awaitable[dict[str, int]]]] = {
    "autospawn": _autospawn,
    "spawns": _spawns,
    "auctions": _auctions,
    "trades": _trades,
    "codes": _codes,
    "raffles": _raffles,
    "nightly": _nightly,
    "integrity": _integrity,
}


async def run_pass(ctx: AppContext, name: str = "all", *, limit: int = 20) -> list[PassResult]:
    """Run one pass (or every pass) once. Used by ``python -m waifu jobs`` and by cron."""
    wanted = list(PASSES) if name in {"all", "", "*"} else [name]
    results: list[PassResult] = []
    for pass_name in wanted:
        func = PASSES.get(pass_name)
        if func is None:
            results.append(PassResult(name=pass_name, error="unknown pass"))
            continue
        started = time.perf_counter()
        try:
            counters = await func(ctx, limit=limit)
            results.append(
                PassResult(name=pass_name, counters=counters, seconds=time.perf_counter() - started)
            )
        except Exception as exc:
            log.exception("job %s failed", pass_name)
            results.append(
                PassResult(
                    name=pass_name,
                    counters={},
                    seconds=time.perf_counter() - started,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
    return results


class JobRunner:
    """The resident loop. ``await runner.stop()`` shuts it down cleanly."""

    def __init__(
        self, ctx: AppContext, *, intervals: dict[str, int] | None = None, limit: int = 20
    ) -> None:
        self.ctx = ctx
        self.intervals = dict(intervals or INTERVALS)
        self.limit = limit
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self.last: dict[str, float] = {}
        self.history: list[PassResult] = []

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.running:
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._loop(), name="waifu-jobs")
        log.info(
            "job loop started (%d passes, %s)",
            len(self.intervals),
            " ".join(f"{k}/{v}s" for k, v in self.intervals.items()),
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except (TimeoutError, asyncio.CancelledError):  # pragma: no cover
                self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        # One loop, checked against each pass's own interval — a task-per-pass would
        # multiply DB connections and drift apart after any restart.
        while not self._stop.is_set():
            now = time.monotonic()
            for name, every in self.intervals.items():
                if now - self.last.get(name, 0.0) < every:
                    continue
                self.last[name] = now
                results = await run_pass(self.ctx, name, limit=self.limit)
                for result in results:
                    self.history.append(result)
                    if result.counters and any(result.counters.values()):
                        log.info("%s", result)
            self.history = self.history[-200:]
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=1.0)
            except TimeoutError:
                continue


__all__ = ["INTERVALS", "PASSES", "JobRunner", "PassResult", "run_pass"]
