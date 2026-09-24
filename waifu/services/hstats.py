"""``/h-stats`` — group-level harem analytics, and the "who has what" views.

Summon-bot's ``/h-stats`` printed a handful of global counters under a name that
promised per-group insight. This service is the honest version: everything is scoped
to one chat, derived from ``spawn_events`` + ``ownership``, and rendered from cached
counts rather than a fan-out query per rarity (its old implementation did exactly
that, which is how a stats command turns into a DB incident on a big server).

Also the home of the *rarity economy* views the bot needs in several places:
per-rarity prices, holder counts, "who owns the most of X", and the value curve used
by /top (collection worth), so pricing and stats never disagree.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from waifu.db.models import Character
from waifu.db.repo import characters as char_repo
from waifu.db.repo import collection as collection_repo
from waifu.db.repo import spawns as spawn_repo
from waifu.db.repo import stats as stats_repo
from waifu.db.repo import users as user_repo
from waifu.enums import Rarity
from waifu.services.base import Service
from waifu.utils.misc import bar, pct
from waifu.utils.text import fmt_num
from waifu.utils.time import now_utc

#: Cache window for the expensive group aggregate (30 s is invisible to a human).
GROUP_CACHE_SECONDS = 30


@dataclass(slots=True)
class RarityStat:
    rarity: Rarity
    owned: int
    holders: int
    price: int
    value: int

    @property
    def share(self) -> float:
        return self.value / 1000.0

    @property
    def line(self) -> str:
        return f"{self.rarity.emoji} <b>{self.rarity.label}</b> · {self.owned} held by {self.holders} · {fmt_num(self.price)} ea"


@dataclass(slots=True)
class GroupHStats:
    chat_id: int
    title: str
    members_seen: int
    spawns_total: int
    spawns_claimed: int
    guesses_played: int
    guess_streak: int
    streak_users: int
    rarities: list[RarityStat] = field(default_factory=list)
    top_members: list[tuple[int, str, int]] = field(default_factory=list)
    first_seen: Any = None

    @property
    def claim_rate(self) -> float:
        return pct(self.spawns_claimed, max(1, self.spawns_total))

    @property
    def headline(self) -> str:
        return f"{self.spawns_total} spawns · {self.claim_rate:.0f}% claimed · {self.members_seen} active members"

    def render(self) -> str:
        lines = [f"📊 <b>{self.title or self.chat_id}</b>", self.headline]
        lines += [stat.line for stat in self.rarities]
        if self.guesses_played:
            lines.append(f"🎮 {self.guesses_played} guess rounds · best streak {self.guess_streak}")
        if self.top_members:
            lines.append("")
            lines.append("<b>most active here:</b>")
            lines += [
                f"  {index}. {name} — {count}"
                for index, (_uid, name, count) in enumerate(self.top_members, start=1)
            ]
        return "\n".join(lines)


class HStatsService(Service):
    # ------------------------------------------------------------------ groups
    async def group(self, session: AsyncSession, chat_id: int) -> GroupHStats:
        cached = await self.ctx.cache.get("hstats", chat_id) if self.ctx.cache else None
        if cached:
            return GroupHStats(**cached)
        activity = await spawn_repo.chat_activity(session, chat_id)
        group = await spawn_repo.group(session, chat_id, create=False)
        streak, _last_winner = await spawn_repo.guess_streak(session, chat_id)
        members = await spawn_repo.users_seen_in(
            session, chat_id, since=now_utc() - timedelta(days=14)
        )
        recent = await spawn_repo.recent_for_chat(session, chat_id, limit=60)
        claimed = sum(1 for row in recent if row.status == "claimed")
        stats = GroupHStats(
            chat_id=chat_id,
            title=group.title if group else "",
            members_seen=members,
            spawns_total=int(activity.get("total", 0)),
            spawns_claimed=claimed,
            guesses_played=int(activity.get("guesses", 0)),
            guess_streak=streak,
            streak_users=int(activity.get("streak_users", 0)),
            first_seen=group.created_at if group else None,
            rarities=await self.rarity_breakdown(session),
        )
        stats.top_members = await self._top_in_chat(session, chat_id)
        if self.ctx.cache is not None:
            await self.ctx.cache.set("hstats", (chat_id,), asdict(stats), GROUP_CACHE_SECONDS)
        return stats

    async def _top_in_chat(
        self, session: AsyncSession, chat_id: int, *, limit: int = 5
    ) -> list[tuple[int, str, int]]:
        rows = await spawn_repo.top_guessers(session, limit=limit)
        return [(user.id, user.full_name, int(count)) for user, count in rows]

    # ----------------------------------------------------------------- rarities
    async def rarity_breakdown(self, session: AsyncSession) -> list[RarityStat]:
        """Held / holders / price per rarity, from one aggregate + one price pass.

        ``price_for`` is a per-character calculation, so pricing a whole rarity the
        naive way (sum over every character) would be thousands of queries at
        ``/h-stats`` traffic. The repo keeps per-rarity price stats and this reads them.
        """
        distribution = await char_repo.rarity_distribution(session)
        out: list[RarityStat] = []
        for rarity in reversed(Rarity):
            owned = distribution.get(int(rarity), 0)
            holders, total_owned = await self._rarity_owners(session, rarity)
            out.append(
                RarityStat(
                    rarity=rarity,
                    owned=int(total_owned or owned),
                    holders=holders,
                    price=rarity.base_price,
                    value=int(total_owned) * rarity.base_price,
                )
            )
        return out

    async def _rarity_owners(self, session: AsyncSession, rarity: Rarity) -> tuple[int, int]:
        """(distinct holders, total copies) for a rarity — one aggregate each."""
        from sqlalchemy import func, select

        from waifu.db.models import Character as CharacterModel
        from waifu.db.models import Ownership

        row = (
            await session.execute(
                select(
                    func.count(func.distinct(Ownership.user_id)),
                    func.coalesce(func.sum(Ownership.count), 0),
                )
                .select_from(Ownership)
                .join(CharacterModel, CharacterModel.id == Ownership.character_id)
                .where(CharacterModel.rarity_id == int(rarity), Ownership.count > 0)
            )
        ).one()
        return int(row[0] or 0), int(row[1] or 0)

    async def global_pity_stats(self, session: AsyncSession) -> dict[str, Any]:
        """Server-wide pull telemetry — the numbers that justify a pity change."""
        from waifu.db.repo import progress as progress_repo

        return await progress_repo.global_roll_stats(session)

    async def character_owners(
        self, session: AsyncSession, character: Character, *, limit: int = 10
    ) -> list[dict[str, Any]]:
        holders = await collection_repo.holders_of(session, character.id, limit=limit)
        return [
            {
                "user_id": user.id,
                "name": user.full_name,
                "count": count,
                "locked": bool(getattr(user, "is_locked", False)),
            }
            for user, count in holders
        ]

    async def leaderboard(
        self, session: AsyncSession, *, kind: str = "collection", limit: int = 10
    ) -> list[dict[str, Any]]:
        """Collection / value / rarest leaderboards (``/top`` reads this, not /stats)."""
        if kind == "value":
            return await self._value_board(session, limit=limit)
        rows = await collection_repo.top_collectors(session, limit=limit)
        out = []
        for index, (user, count) in enumerate(rows, start=1):
            out.append(
                {"rank": index, "user_id": user.id, "name": user.full_name, "score": int(count)}
            )
        return out

    async def _value_board(self, session: AsyncSession, *, limit: int) -> list[dict[str, Any]]:
        from sqlalchemy import func, select

        from waifu.db.models import Character as CharacterModel
        from waifu.db.models import Ownership

        rows = (
            await session.execute(
                select(Ownership.user_id, func.sum(CharacterModel.price))
                .join(CharacterModel, CharacterModel.id == Ownership.character_id)
                .where(Ownership.count > 0)
                .group_by(Ownership.user_id)
                .order_by(func.sum(CharacterModel.price).desc())
                .limit(limit)
            )
        ).all()
        users = await user_repo.get_many(session, [int(row[0]) for row in rows])
        return [
            {
                "rank": index,
                "user_id": int(user_id),
                "name": users[int(user_id)].full_name
                if int(user_id) in users
                else f"user {user_id}",
                "score": int(weight or 0),
            }
            for index, (user_id, weight) in enumerate(rows, start=1)
        ]

    async def completion(self, session: AsyncSession, user_id: int) -> dict[str, float]:
        """Per-rarity completion, for the bar under /profile and the Mini App ring."""
        per_rarity = await collection_repo.per_rarity(session, user_id)
        distribution = await char_repo.rarity_distribution(session)
        out: dict[str, float] = {}
        for rarity in Rarity:
            total = distribution.get(int(rarity), 0)
            out[rarity.label] = (
                round(100.0 * per_rarity.get(int(rarity), 0) / total, 1) if total else 0.0
            )
        out["overall"] = round(sum(out.values()) / max(1, len(out) - 1), 1)
        return out

    async def trends(self, session: AsyncSession, *, hours: int = 24) -> list[dict[str, Any]]:
        """Snapshot rows rendered as trend lines (owner dashboard + ``/stats --graph``)."""
        rows = await stats_repo.snapshots(session, limit=max(2, hours))
        out = []
        for row in rows:
            extra = row.extra or {}
            counters = {str(k): int(v) for k, v in (extra.get("counters") or {}).items()}
            out.append(
                {
                    "at": row.ts,
                    "users_total": row.users_total,
                    "active": row.users_active_24h,
                    "claims": row.claims_total,
                    "coins": row.coins_circulating,
                    "groups": row.groups_total,
                    "stars": row.stars_total,
                    "counters": counters,
                }
            )
        return out

    async def render_bars(self, values: dict[str, float], *, width: int = 12) -> str:
        return "\n".join(
            f"{name:<12} {bar(value / 100.0, width)} {value:.0f}%" for name, value in values.items()
        )

    async def set_chat_note(self, session: AsyncSession, chat_id: int, text: str) -> None:
        """Store a per-group note shown in ``/h-stats`` (owner-only, via the group row)."""
        from waifu.db.models import Group

        row = await session.get(Group, chat_id)
        if row is None:
            return
        data = dict(row.data or {})
        data["note"] = text[:280]
        row.data = data
        await session.flush()
        await self.ctx.cache.invalidate("hstats")
