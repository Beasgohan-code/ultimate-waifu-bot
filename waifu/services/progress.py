"""Progress service — streaks, achievements and daily quests.

Summon-bot had `/streak` (a number) and `/achievements` (a hand-maintained list of
strings in the DB) and nothing else. Here the same tables do three jobs:

* **streak** — the multiplier curve lives in settings, a freeze is an item, and
  ``bump_streak`` decides "extended / broken / frozen" so the UI can only report the
  truth (``economy.daily`` calls :meth:`ProgressService.mark_daily`);
* **achievements** — definitions in code with a *measurable* target, so progress is
  computed from real data instead of an admin remembering to grant one;
* **quests** — three daily objectives derived from the day's seed and shown as a
  Bot API 10.3 checklist (``/quests``), with claim state in ``daily_claims``.

All reward payouts go through ``claim_bonus``, which is a unique-index insert:
double-tap or replayed webhook = one payout.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from waifu.db.models import User
from waifu.db.repositories import collection as collection_repo
from waifu.db.repositories import economy as ledger
from waifu.db.repositories import items as items_repo
from waifu.db.repositories import metrics as metrics_repo
from waifu.db.repositories import progress as progress_repo
from waifu.db.repositories import users as user_repo
from waifu.enums import LedgerReason, Rarity
from waifu.errors import AlreadyClaimed, NotFound
from waifu.services.base import Service
from waifu.utils.rng import commit
from waifu.utils.time import start_of_month, start_of_week

#: key → (title, description, emoji, target, metric, coin bonus)
ACHIEVEMENTS: tuple[tuple[str, str, str, str, int, str, int], ...] = (
    ("first_claim", "First Summon", "You found your first character.", "✨", 1, "pulls_total", 500),
    ("collector_10", "Shelf Filler", "Own 10 characters.", "📚", 10, "collection_size", 2000),
    ("collector_50", "Curator", "Own 50 characters.", "🖼️", 50, "collection_size", 8000),
    ("collector_150", "Obsessive", "Own 150 characters.", "🧠", 150, "collection_size", 25000),
    ("whale", "High Roller", "Pull 5 rare-or-better characters.", "🐋", 5, "high_pulls", 15000),
    ("legendary", "Legend", "Own one Legendary.", "🌈", 1, "legendary_count", 20000),
    ("streak_7", "Week One", "Keep a 7-day streak.", "🔥", 7, "streak_best", 3000),
    ("streak_30", "Monthly Habit", "Keep a 30-day streak.", "📅", 30, "streak_best", 15000),
    ("rich", "Loaded", "Hold 100,000 coins at once.", "💰", 100000, "balance", 5000),
    ("tycoon", "Tycoon", "Hold 1,000,000 coins at once.", "🏦", 1000000, "balance", 40000),
    ("worker", "Workaholic", "Work 25 times.", "🛠️", 25, "work_count", 6000),
    ("linguist", "Polyglot", "Chat with 3 different characters.", "🗣️", 3, "ai_partners", 4000),
    ("social", "Well Known", "Be top collector in 1 group.", "🤝", 1, "top_collector", 5000),
    ("hunter", "Spawn Hunter", "Claim 20 group spawns.", "🎯", 20, "spawns_claimed", 9000),
    ("gambler", "All In", "Spin 50 times.", "🎡", 50, "spin_count", 7000),
    ("auctioneer", "Sold!", "Win 3 auctions.", "🔨", 3, "auctions_won", 12000),
    ("trader", "Trader", "Complete 5 trades.", "🔁", 5, "trades_done", 8000),
    ("patron", "Patron", "Buy premium once.", "⭐", 1, "paid_count", 10000),
)

#: quest key → (label, emoji, target, reward) — progress comes from today's data.
QUEST_TEMPLATES: tuple[tuple[str, str, str, int, int], ...] = (
    ("daily", "Claim your daily", "📅", 1, 1500),
    ("pulls3", "Do 3 claims", "🎴", 3, 2500),
    ("pulls10", "Do 10 claims", "🎰", 10, 7000),
    ("work2", "Work twice", "🛠️", 2, 2000),
    ("chat", "Chat with a character", "💬", 1, 1800),
    ("spawn", "Claim a group spawn", "🎯", 1, 3000),
    ("sell", "Sell a duplicate", "💸", 1, 1500),
    ("gift", "Gift or trade once", "🎁", 1, 2500),
)
QUESTS_PER_DAY = 3
#: Where a protected collection line comes from (used by /achievements footnote).
PROTECTIVE_ITEMS = ("sshield", "bshield")


@dataclass(slots=True)
class Achievement:
    key: str
    title: str
    description: str
    emoji: str
    target: int
    metric: str
    bonus: int
    progress: int = 0
    unlocked: bool = False
    rarity_percent: float = 0.0

    @property
    def percent(self) -> float:
        return min(100.0, 100.0 * self.progress / max(1, self.target))

    @property
    def line(self) -> str:
        mark = "✅" if self.unlocked else f"{self.progress}/{self.target}"
        return f"{self.emoji} <b>{self.title}</b> — {self.description} <i>{mark}</i>"


@dataclass(slots=True)
class Quest:
    key: str
    label: str
    emoji: str
    target: int
    reward: int
    progress: int = 0
    claimed: bool = False

    @property
    def done(self) -> bool:
        return self.progress >= self.target

    @property
    def id(self) -> str:
        return self.key

    @property
    def checkbox_label(self) -> str:
        return f"{self.emoji} {self.label} ({min(self.progress, self.target)}/{self.target})"

    @property
    def entry_params(self) -> tuple[str, int, bool]:
        """(text, need, done) for ``SendChecklist``."""
        return (self.checkbox_label, self.target, self.done)


@dataclass(slots=True)
class StreakState:
    current: int
    best: int
    multiplier: float
    freezes: int
    last_date: str
    claimed_today: bool
    premium_claimed: bool = False

    @property
    def at_risk(self) -> bool:
        return self.current > 0 and not self.claimed_today

    @property
    def line(self) -> str:
        fire = "🔥" * min(5, max(1, self.current // 7)) if self.current else "—"
        return f"{fire} day {self.current} · best {self.best} · ×{self.multiplier:.2f}"


class ProgressService(Service):
    # ---------------------------------------------------------------- metrics
    async def metrics(self, session: AsyncSession, user: User) -> dict[str, int]:
        """Everything an achievement or quest can measure, in a few COUNTs.

        Counted from the source tables (see :mod:`waifu.db.repositories.metrics`)
        rather than from denormalised columns: an achievement that can silently drift
        is an achievement players will rightly call fake.
        """
        counters = await metrics_repo.many(session, user.id)
        per_rarity = await collection_repo.per_rarity(session, user.id)
        today = self.local_day()
        return {
            "pulls_total": user.pulls_total,
            "high_pulls": user.high_pulls,
            "collection_size": counters["collection_size"],
            "legendary_count": sum(
                c for r, c in per_rarity.items() if int(r) >= int(Rarity.LEGENDARY)
            ),
            "celestial_count": sum(
                c for r, c in per_rarity.items() if int(r) >= int(Rarity.CELESTIAL)
            ),
            "streak_best": user.streak_best,
            "balance": user.balance,
            "work_count": counters["work_count"],
            "ai_partners": len(await self.ctx.ai.summary_for(session, user.id, limit=40))
            if self.ctx.ai
            else 0,
            "spawns_claimed": counters["spawns_claimed"],
            "spin_count": counters["spin_count"],
            "auctions_won": counters["auctions_won"],
            "trades_done": counters["trades_done"],
            "heists_won": counters["heists_won"],
            "gift_count": counters["gifts_sent"],
            "paid_count": 1 if await self._has_paid(session, user.id) else 0,
            "top_collector": await self._is_top_collector(session, user.id),
            # Quest metrics are today-scoped.
            "claims_today": counters["rolls_today"],
            "daily_done": int(
                await progress_repo.already_claimed(session, user.id, "daily", today)
            ),
            "sell_today": counters["sold_today"],
            "chat_today": counters["ai_today"],
        }

    async def _has_paid(self, session: AsyncSession, user_id: int) -> bool:
        from waifu.db.repositories import monetize as monetize_repo

        return await monetize_repo.has_ever_paid(session, user_id)

    async def _is_top_collector(self, session: AsyncSession, user_id: int) -> int:
        board = await collection_repo.top_collectors(session, limit=1)
        return 1 if board and board[0][0].id == user_id else 0

    # ----------------------------------------------------------- achievements
    async def list_for(self, session: AsyncSession, user_id: int) -> list[Achievement]:
        user = await user_repo.get(session, user_id)
        if user is None:
            raise NotFound("player not registered")
        unlocked = await progress_repo.unlocked(session, user_id)
        metrics = await self.metrics(session, user)
        players = max(1, (await user_repo.counts(session)).get("users", 1))
        counts = await progress_repo.unlock_count_map(session)
        out: list[Achievement] = []
        for key, title, description, emoji, target, metric, bonus in ACHIEVEMENTS:
            value = int(metrics.get(metric, 0) or 0)
            holders = counts.get(key, 0)
            out.append(
                Achievement(
                    key=key,
                    title=title,
                    description=description,
                    emoji=emoji,
                    target=target,
                    metric=metric,
                    bonus=bonus,
                    progress=min(value, target),
                    unlocked=key in unlocked,
                    rarity_percent=round(100.0 * holders / players, 1),
                )
            )
        return sorted(out, key=lambda a: (not a.unlocked, -a.percent))

    async def evaluate(self, session: AsyncSession, user_id: int) -> list[Achievement]:
        """Unlock everything earned so far; returns the newly unlocked ones.

        Called after claims, sells, trades, spawns and premium grants — anywhere a
        milestone can pass without the player opening /achievements, so the toast can
        be sent while they are still in the chat that earned it.
        """
        user = await user_repo.get(session, user_id)
        if user is None:
            return []
        metrics = await self.metrics(session, user)
        unlocked_now: list[Achievement] = []
        for key, title, description, emoji, target, metric, bonus in ACHIEVEMENTS:
            value = int(metrics.get(metric, 0) or 0)
            if value < target:
                await progress_repo.set_progress(session, user_id, key, value)
                continue
            # ``unlock`` is a unique-index insert: a milestone passed twice (two pulls
            # in one second, a retried webhook) unlocks once.
            try:
                fresh = await progress_repo.unlock(session, user_id, key, progress=value)
            except AlreadyClaimed:
                continue
            if not fresh:
                continue
            try:
                await progress_repo.claim_bonus(session, user_id, key, bonus)
                await ledger.credit(
                    session,
                    user_id,
                    bonus,
                    LedgerReason.ACHIEVEMENT,
                    reference=f"achievement:{key}",
                    idempotency_key=f"ach:{user_id}:{key}",
                )
            except AlreadyClaimed:  # pragma: no cover - unlocked earlier, bonus paid then
                pass
            unlocked_now.append(
                Achievement(
                    key=key,
                    title=title,
                    description=description,
                    emoji=emoji,
                    target=target,
                    metric=metric,
                    bonus=bonus,
                    progress=value,
                    unlocked=True,
                )
            )
        if unlocked_now:
            await self.log_line(
                f"🏆 {user_id}: " + ", ".join(f"{a.emoji} {a.title}" for a in unlocked_now),
                silent=True,
            )
        return unlocked_now

    async def summary(self, session: AsyncSession, user_id: int) -> dict[str, Any]:
        achievements = await self.list_for(session, user_id)
        return {
            "total": len(achievements),
            "unlocked": sum(1 for a in achievements if a.unlocked),
            "percent": round(
                100.0 * sum(1 for a in achievements if a.unlocked) / max(1, len(achievements)), 1
            ),
            "next": next((a for a in achievements if not a.unlocked), None),
            "rarest": await progress_repo.rarest_unlocks(session, limit=5),
        }

    # ------------------------------------------------------------------ quests
    async def quests(
        self, session: AsyncSession, user_id: int, *, day: str | None = None
    ) -> list[Quest]:
        """Today's three objectives, stable for the day, seeded by the day itself.

        They are *derived*, not stored: the row that used to track quest state could
        drift out of sync with what actually happened, while "did you claim your
        daily today" is always answerable from the ledger.
        """
        today = day or self.local_day()
        user = await user_repo.get(session, user_id)
        if user is None:
            raise NotFound("player not registered")
        metrics = await self.metrics(session, user)
        # Hash of (user, day) → the same three quests all day, different tomorrow,
        # and no state to keep in sync.
        start = int(commit(f"quests:{user_id}:{today}")[:8], 16) % max(1, len(QUEST_TEMPLATES))
        templates = [
            QUEST_TEMPLATES[(start + i) % len(QUEST_TEMPLATES)] for i in range(QUESTS_PER_DAY)
        ]
        out: list[Quest] = []
        for key, label, emoji, target, reward in templates:
            progress = int(metrics.get(_QUEST_METRIC.get(key, key), 0) or 0)
            out.append(
                Quest(
                    key=key,
                    label=label,
                    emoji=emoji,
                    target=target,
                    reward=reward,
                    progress=progress,
                    claimed=await self.quest_claimed(session, user_id, key, day=today),
                )
            )
        return out

    async def claim_quest(
        self, session: AsyncSession, user_id: int, key: str, *, day: str | None = None
    ) -> int:
        """Pay a completed quest; raises :class:`AlreadyClaimed` / :class:`NotFound`."""
        today = day or self.local_day()
        quests = {q.key: q for q in await self.quests(session, user_id, day=today)}
        quest = quests.get(key)
        if quest is None:
            raise NotFound("that quest is not on today's list")
        if not quest.done:
            raise AlreadyClaimed(f"not finished ({quest.progress}/{quest.target})")
        try:
            await progress_repo.claim_bonus(session, user_id, f"quest:{key}:{today}", quest.reward)
        except AlreadyClaimed:
            raise
        await ledger.credit(
            session,
            user_id,
            quest.reward,
            LedgerReason.QUEST,
            reference=f"quest:{key}:{today}",
            idempotency_key=f"quest:{user_id}:{today}:{key}",
        )
        await self.evaluate(session, user_id)
        return quest.reward

    async def quest_claimed(
        self, session: AsyncSession, user_id: int, key: str, *, day: str | None = None
    ) -> bool:
        today = day or self.local_day()
        rows = await ledger.claim_history(session, user_id, f"quest:{key}", limit=1)
        return any(row.local_day == today for row in rows)

    async def checklist_payload(
        self, session: AsyncSession, user_id: int
    ) -> tuple[list[tuple[str, int, bool]], int]:
        """``([(text, need, done)], total_reward)`` — exactly ``SendChecklist`` entries."""
        quests = await self.quests(session, user_id)
        return [q.entry_params for q in quests], sum(q.reward for q in quests if not q.claimed)

    # ----------------------------------------------------------------- streak
    async def streak(self, session: AsyncSession, user_id: int) -> StreakState:
        state = await progress_repo.streak(session, user_id)
        today = self.local_day()
        return StreakState(
            current=state.current,
            best=state.highest,
            multiplier=self.multiplier_for(state.current),
            freezes=state.freezes,
            last_date=state.last_date,
            claimed_today=await progress_repo.already_claimed(session, user_id, "daily", today),
            premium_claimed=await progress_repo.already_claimed(
                session, user_id, "daily_premium", today
            ),
        )

    def multiplier_for(self, day: int) -> float:
        curve = self.settings.streak_multiplier_curve or [1, 1.1, 1.2, 1.3, 1.4, 1.5, 2]
        return float(curve[min(max(day - 1, 0), len(curve) - 1)]) if day > 0 else 1.0

    async def mark_daily(
        self, session: AsyncSession, user_id: int
    ) -> tuple[int, float, bool, bool]:
        """Advance the streak after a successful daily claim (``bump_streak`` owns the rules)."""
        result = await progress_repo.bump_streak(
            session,
            user_id,
            self.local_day(),
            multiplier_curve=self.settings.streak_multiplier_curve,
        )
        return (
            result.current,
            result.multiplier,
            result.broke,
            result.current in {7, 30, 60, 100, 365},
        )

    async def purchase_freeze(self, session: AsyncSession, user_id: int) -> int:
        """Freezes cost coins, cap at 3, and are *not* refundable (spent → granted)."""
        price = 2500
        await ledger.debit(
            session,
            user_id,
            price,
            LedgerReason.BUY,
            reference="streak:freeze",
            idempotency_key=f"freeze:{user_id}:{self.local_day()}",
        )
        return await progress_repo.add_freeze(session, user_id, 1)

    async def protect_streak(self, session: AsyncSession, user_id: int) -> bool:
        """Arm a freeze so the nightly guard skips this player (``/streak protect``).

        The repo's ``bump_streak`` already consumes a freeze when it finds a gap, so
        "protecting" means exactly one thing here: holding a charge. That is why the
        command is honest about what it does instead of writing a second flag.
        """
        streak = await progress_repo.streak(session, user_id)
        today = self.local_day()
        if streak.last_date == today:
            return False
        if streak.freezes > 0:
            return True
        return await progress_repo.spend_freeze(session, user_id)

    async def reset_streaks(self, session: AsyncSession) -> int:
        """Nightly guard: break every streak that missed two days, using the repo rule."""
        return await progress_repo.reset_streaks(session)

    async def weekly_summary(self, session: AsyncSession, user_id: int) -> dict[str, int]:
        since = start_of_week()
        month = start_of_month()
        history = await ledger.history(session, user_id, limit=200)
        return {
            "week_transactions": sum(1 for row in history if row.created_at >= since),
            "month_transactions": sum(1 for row in history if row.created_at >= month),
            "week_net": sum(int(row.delta) for row in history if row.created_at >= since),
        }

    async def protect_collection(self, session: AsyncSession, user_id: int) -> dict[str, Any]:
        """How much of a player's harem is safe from theft — shown in /steal help."""
        locked = await collection_repo.locked_ids(session, user_id)
        shields = {
            kind: await items_repo.count_shields(session, user_id, kind)
            for kind in PROTECTIVE_ITEMS
        }
        return {
            "locked": len(locked),
            "shields": shields,
            "protected_total": len(locked) + sum(shields.values()),
        }


#: quest key → the metrics() entry that measures it
_QUEST_METRIC = {
    "daily": "daily_done",
    "pulls3": "claims_today",
    "pulls10": "claims_today",
    "work2": "work_count",
    "chat": "ai_partners",
    "spawn": "spawns_claimed",
    "sell": "sell_today",
    "gift": "gift_count",
}


def quest_progress(raw: dict[str, int], metric: str) -> int:  # pragma: no cover - helper
    """Boolean metrics count as one completion; numeric ones pass through."""
    value = raw.get(metric, 0)
    return 1 if value is True or value == 1 else int(value or 0)


@dataclass(slots=True)
class AchievementBoard:
    rows: list[Achievement] = field(default_factory=list)
    unlocked: int = 0
    total: int = 0
    rarest: list[tuple[str, int]] = field(default_factory=list)

    def render(self) -> str:
        if not self.rows:
            return "no achievements yet — go pull something"
        lines = [row.line for row in self.rows]
        return f"🏆 <b>{self.unlocked}/{self.total}</b> unlocked\n" + "\n".join(lines)
