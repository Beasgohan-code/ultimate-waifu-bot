"""Stats service — /top, /stats, /h-stats, the owner dashboard, the trend job.

Three tiers of "hot", because a bot on a 40k-chat server learns quickly that
per-update aggregates are a trap:

* **Redis ZSETs** back /top (updated on each balance change) → O(log N) ranks with
  zero DB load;
* **counters** in Redis flushed into ``stats_snapshots`` every few minutes by the
  scheduler → the dashboard's trend lines;
* **SQL aggregates** only for pages nobody hammers (``/stats`` per player, audit).

If a leaderboard key is missing (fresh Redis, after a flush) it is *rebuilt* from
Postgres on read, so the worst case is one slow query — never a broken command.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from waifu.db.models import User
from waifu.db.repo import characters as char_repo
from waifu.db.repo import collection as collection_repo
from waifu.db.repo import economy as ledger
from waifu.db.repo import items as items_repo
from waifu.db.repo import moderation as mod_repo
from waifu.db.repo import monetize as monetize_repo
from waifu.db.repo import progress as progress_repo
from waifu.db.repo import spawns as spawn_repo
from waifu.db.repo import stats as stats_repo
from waifu.db.repo import users as user_repo
from waifu.enums import Rarity
from waifu.errors import NotFound
from waifu.services.base import Service
from waifu.utils.misc import bar, pct
from waifu.utils.text import fmt_num
from waifu.utils.time import now_utc


@dataclass(slots=True)
class LeaderRow:
    rank: int
    user_id: int
    name: str
    score: int
    extra: str = ""


@dataclass(slots=True)
class PlayerStats:
    user_id: int
    balance: int
    level: int
    exp: int
    pulls: int
    high_pulls: int
    streak: int
    best_streak: int
    collection: dict[str, int]
    per_rarity: dict[int, int]
    roll_stats: dict[str, float | int]
    heist: dict[str, int]
    achievements: list[str] = field(default_factory=list)
    premium_hours: int = 0
    rank_coins: int = 0
    rank_claims: int = 0
    total_players: int = 0

    @property
    def high_rate(self) -> float:
        return pct(self.high_pulls, max(1, self.pulls))


@dataclass(slots=True)
class GroupStats:
    """``/h-stats``: what this chat has done — scoped so no other group leaks in."""

    chat_id: int
    title: str
    registered_at: Any
    spawns_total: int = 0
    spawns_claimed: int = 0
    messages: int = 0
    unique_members: int = 0
    guesses: int = 0
    streak: int = 0
    top_claimers: list[LeaderRow] = field(default_factory=list)
    rarity_mix: dict[int, int] = field(default_factory=dict)

    @property
    def claim_rate(self) -> float:
        return pct(self.spawns_claimed, max(1, self.spawns_total))

    def bars(self) -> list[str]:
        out = []
        total = sum(self.rarity_mix.values()) or 1
        for rarity_id, count in sorted(self.rarity_mix.items(), key=lambda kv: -kv[1]):
            rarity = Rarity.from_value(rarity_id)
            out.append(f"{rarity.emoji} {bar(count / total, 12)} {count}")
        return out


#: Leaderboard kind → (Redis ZSET name, user_repo metric). The ZSET key is what the
#: writers (economy, gacha, spawn, collection) increment; the DB metric is the
#: fallback and the rebuild source — they are deliberately separate names, because
#: "coins" is what players call it and "balance" is what the column is called.
LEADERBOARDS: dict[str, tuple[str, str]] = {
    "coins": ("lb:coins", "balance"),
    "claims": ("lb:claims", "claims"),
    "high": ("lb:high", "high"),
    "streak": ("lb:streak", "streak"),
    "level": ("", "level"),
    "collection": ("", ""),  # computed from ownership, no ZSET
}


class StatsService(Service):
    # ------------------------------------------------------------- startup work
    async def warm(self) -> None:
        """Rebuild the Redis leaderboards from Postgres when they are missing.

        Called once from :meth:`AppContext.startup`. Without this a Redis flush (or a
        first deploy) leaves ``/top`` empty until people earn their way back in,
        which reads as data loss.
        """
        if self.redis is None:
            return
        async with self.ctx.db.session() as session:
            for zset, metric in LEADERBOARDS.values():
                if not zset or await self.redis.zcard(zset):
                    continue
                for user, score in await user_repo.leaderboard(session, metric=metric, limit=200):
                    await self.redis.zadd(zset, str(user.id), float(score))

    # ------------------------------------------------------------- leaderboards
    async def leaderboard(
        self, session: AsyncSession, kind: str = "coins", *, limit: int = 10, offset: int = 0
    ) -> tuple[list[LeaderRow], int]:
        """ZSET-first, SQL-second. Returns ``(rows, total)``."""
        zset, metric = LEADERBOARDS.get(kind, LEADERBOARDS["coins"])
        if not metric:
            return await self._collectors(session, limit=limit, offset=offset)
        if self.redis is not None and zset:
            cached = await self.redis.ztop(zset, limit=limit + offset)
            if len(cached) >= limit:
                page = cached[offset : offset + limit]
                users = await user_repo.get_many(session, [int(uid) for uid, _ in page])
                rows = [
                    LeaderRow(
                        rank=offset + position + 1,
                        user_id=int(uid),
                        name=self._name(users.get(int(uid))),
                        score=int(score),
                        extra=self._extra(users.get(int(uid))),
                    )
                    for position, (uid, score) in enumerate(page)
                ]
                if any(rows):
                    return rows, len(cached)
        rows = await user_repo.leaderboard(session, metric=metric, limit=limit, offset=offset)
        total = (await user_repo.counts(session)).get("users", len(rows))
        return [
            LeaderRow(
                rank=offset + i + 1,
                user_id=user.id,
                name=self._name(user),
                score=int(score),
                extra=self._extra(user),
            )
            for i, (user, score) in enumerate(rows)
        ], int(total)

    async def _collectors(
        self, session: AsyncSession, *, limit: int, offset: int
    ) -> tuple[list[LeaderRow], int]:
        rows = await collection_repo.top_collectors(session, limit=limit + offset)
        out = [
            LeaderRow(rank=i + 1, user_id=user.id, name=self._name(user), score=count)
            for i, (user, count) in enumerate(rows)
        ][offset : offset + limit]
        return out, len(rows)

    async def rank_of(
        self, session: AsyncSession, user_id: int, *, kind: str = "coins"
    ) -> tuple[int, int]:
        _zset, metric = LEADERBOARDS.get(kind, LEADERBOARDS["coins"])
        if not metric:
            return await self._collector_rank(session, user_id)
        return await user_repo.rank_of(session, user_id, metric=metric)

    async def _collector_rank(self, session: AsyncSession, user_id: int) -> tuple[int, int]:
        board = await collection_repo.top_collectors(session, limit=500)
        rank = next((i for i, (user, _count) in enumerate(board, start=1) if user.id == user_id), 0)
        return rank, len(board)

    @staticmethod
    def _name(user: User | None) -> str:
        if user is None:
            return "unknown"
        return f"@{user.username}" if user.username else (user.full_name or str(user.id))

    @staticmethod
    def _extra(user: User | None) -> str:
        if user is None:
            return ""
        bits = [f"lv{user.level}"]
        if user.premium_until:
            bits.append("⭐")
        return " ".join(bits)

    # ----------------------------------------------------------- player stats
    async def player(self, session: AsyncSession, user_id: int) -> PlayerStats:
        user = await user_repo.get(session, user_id)
        if user is None:
            raise NotFound("player not registered")
        counts = await user_repo.counts(session)
        return PlayerStats(
            user_id=user.id,
            balance=user.balance,
            level=user.level,
            exp=user.exp,
            pulls=user.pulls_total,
            high_pulls=user.high_pulls,
            streak=user.streak_count,
            best_streak=user.streak_best,
            collection=await collection_repo.summary(session, user_id),
            per_rarity=await collection_repo.per_rarity(session, user_id),
            roll_stats=await progress_repo.roll_stats(session, user_id),
            heist=await items_repo.heist_stats(session, user_id),
            achievements=sorted(await progress_repo.unlocked(session, user_id)),
            premium_hours=await ledger.premium_left_hours(session, user_id),
            rank_coins=(await user_repo.rank_of(session, user_id, metric="balance"))[0],
            rank_claims=(await user_repo.rank_of(session, user_id, metric="claims"))[0],
            total_players=int(counts.get("users", 0)),
        )

    async def rarity_share(
        self, session: AsyncSession, user_id: int
    ) -> list[tuple[Rarity, int, float]]:
        per_rarity = await collection_repo.per_rarity(session, user_id)
        total = sum(per_rarity.values()) or 1
        return [
            (Rarity.from_value(rarity_id), count, pct(count, total))
            for rarity_id, count in sorted(per_rarity.items(), key=lambda kv: -kv[0])
        ]

    # ------------------------------------------------------------- global stats
    async def global_(self, session: AsyncSession, *, hours: int = 24) -> dict[str, Any]:
        activity = await stats_repo.global_activity(session, hours=hours)
        counts = await user_repo.counts(session)
        return {
            **counts,
            **activity,
            "characters": await char_repo.totals(session),
            "auctions": await self.ctx.auctions.stats(session) if self.ctx.auctions else {},
            "revenue": await monetize_repo.revenue(session),
            "protective": await items_repo.protective_summary(session),
            "top_commands": await stats_repo.top_commands(session, hours=hours),
            "retention": await stats_repo.retention(session),
            "uptime": self.ctx.uptime_text,
        }

    async def trend(self, session: AsyncSession, *, hours: int = 24) -> list[tuple[str, int, int]]:
        return await stats_repo.trend(session, hours=hours)

    async def snapshots(self, session: AsyncSession, *, limit: int = 48) -> list[Any]:
        return await stats_repo.snapshots(session, limit=limit)

    async def table_sizes(self, session: AsyncSession) -> list[tuple[str, int]]:
        return await stats_repo.table_sizes(session)

    async def vacuum(self, session: AsyncSession, tables: list[str]) -> list[str]:
        return await stats_repo.vacuum_analyze(session, tables)

    async def purge_activity(self, session: AsyncSession, *, older_than_days: int = 14) -> int:
        return await stats_repo.purge_activity(session, older_than_days=older_than_days)

    # --------------------------------------------------------------- counters
    async def bump(self, name: str, amount: int = 1) -> None:
        """Best-effort counter (commands, spawns, claims) — Redis only, never blocks."""
        if self.redis is None:
            return
        await self.redis.incr(f"stat:{name}", ttl=86_400 * 3, amount=amount)

    async def counter(self, name: str) -> int:
        if self.redis is None:
            return 0
        return int(await self.redis.get(f"stat:{name}") or 0)

    async def command_usage(self, *, limit: int = 12) -> list[tuple[str, int]]:
        if self.redis is None:
            return []
        keys = sorted(
            await self.redis.client.keys(f"{self.settings.redis_key_prefix}:cmd:*"), key=lambda k: k
        )  # type: ignore[union-attr]
        out: list[tuple[str, int]] = []
        for key in keys:
            value = await self.redis.client.get(key)  # type: ignore[union-attr]
            out.append((key.rsplit(":", 1)[-1], int(value or 0)))
        out.sort(key=lambda kv: -kv[1])
        return out[:limit]

    async def flush_snapshot(self, session: AsyncSession) -> dict[str, int]:
        """Write one ``stats_snapshots`` row from Redis counters and clear them.

        The scheduler runs this under an advisory lock, so two workers never write
        the same snapshot twice (the reference bot's cron did exactly that, which is
        why its /stats numbers drifted upward).
        """
        counters = {
            "commands_total": await self.counter("commands"),
            "pulls_total": await self.counter("pulls"),
            "spawns_total": await self.counter("spawns"),
            "spawns_claimed": await self.counter("claims"),
        }
        counts = await user_repo.counts(session)
        characters = await char_repo.totals(session)
        fields = {
            "users_total": int(counts.get("users", 0)),
            "users_active_24h": int(counts.get("active_24h", 0)),
            "coins_circulating": await ledger.total_circulating(session),
            "characters_total": int(characters.get("total", 0)),
            "claims_total": int(counts.get("claims", 0)),
            "groups_total": len(await spawn_repo.groups_summary(session)),
            "extra": {
                "counters": counters,
                "new_24h": int(counts.get("new_24h", 0)),
                "reason_totals": [
                    list(row)
                    for row in await ledger.reason_totals(
                        session, since=now_utc() - timedelta(days=7)
                    )
                ],
            },
        }
        await stats_repo.snapshot(session, **fields)
        for name in counters:
            if self.redis is not None:
                await self.redis.delete(f"stat:{name}")
        await self.bump("snapshots_written")
        return counters

    # ---------------------------------------------------------------- h-stats
    async def group(self, session: AsyncSession, chat_id: int) -> GroupStats:
        activity = await spawn_repo.chat_activity(session, chat_id)
        group = await spawn_repo.group(session, chat_id, create=False)
        streak, _ = await spawn_repo.guess_streak(session, chat_id)
        recent = await spawn_repo.recent_for_chat(session, chat_id, limit=200)
        claimed = sum(1 for row in recent if row.status == "claimed")
        return GroupStats(
            chat_id=chat_id,
            title=group.title if group else "",
            registered_at=group.created_at if group else None,
            spawns_total=int(activity.get("total", 0)),
            spawns_claimed=claimed,
            messages=int(group.message_count) if group else 0,
            unique_members=await spawn_repo.users_seen_in(
                session, chat_id, since=now_utc() - timedelta(days=7)
            ),
            guesses=int(activity.get("guesses", 0)),
            streak=streak,
            top_claimers=[
                LeaderRow(rank=i + 1, user_id=int(uid), name=f"user {uid}", score=count)
                for i, (uid, count) in enumerate(await spawn_repo.top_guessers(session, limit=5))
            ],
            rarity_mix=await self._group_rarity_mix(session, recent),
        )

    async def _group_rarity_mix(self, session: AsyncSession, events: list[Any]) -> dict[int, int]:
        ids = [event.character_id for event in events if getattr(event, "status", "") == "claimed"]
        if not ids:
            return {}
        characters = await char_repo.get_many(session, list({*ids}))
        mix: dict[int, int] = {}
        for character_id in ids:
            character = characters.get(character_id)
            if character is not None:
                key = int(character.rarity_id)
                mix[key] = mix.get(key, 0) + 1
        return mix

    async def top_chats(self, session: AsyncSession, *, limit: int = 10) -> list[Any]:
        rows = await spawn_repo.spawnable_groups(session)
        return sorted(rows, key=lambda g: -(g.message_count or 0))[:limit]

    async def leaderboard_markup(self, rows: list[LeaderRow], *, kind: str) -> list[list[str]]:
        """Three-column table payload shared by the text card and the Mini App."""
        header = ["#", kind, ""]
        body = [[str(row.rank), row.name, fmt_num(row.score)] for row in rows]
        return [header, *body]

    async def audit_size(self, session: AsyncSession) -> int:
        rows = await stats_repo.table_sizes(session)
        return sum(size for _name, size in rows)

    async def prune(self, session: AsyncSession, *, days: int = 14) -> dict[str, int]:
        return {
            "activity": await stats_repo.purge_activity(session, older_than_days=days),
            "audit": await mod_repo.audit_purge(session, older_than_days=max(days, 180)),
            "trades": await self._purge_trades(session, days=days),
            "codes": await self._purge_codes(session),
        }

    async def _purge_trades(self, session: AsyncSession, *, days: int) -> int:
        from waifu.db.repo import trades as trade_repo

        return await trade_repo.purge_trade_history(session, older_than_days=days)

    async def _purge_codes(self, session: AsyncSession) -> int:
        from waifu.db.repo import trades as trade_repo

        return await trade_repo.purge_expired(session)

    # ---------------------------------------------------------------- digests
    async def week_summary(self, session: AsyncSession, *, days: int = 7) -> dict[str, int]:
        """The week in seven numbers — the owner digest's raw material.

        Combines the cheap indexed counts (players, gifts, raffles, pulls,
        active subscriptions) with the Stars revenue total, so the digest is
        one method and one ``notify`` call for both the weekly job and
        ``/digest``.
        """
        from datetime import timedelta

        from waifu.db.repo import monetize as monetize_repo
        from waifu.utils.time import now_utc

        since = now_utc() - timedelta(days=days)
        summary = await stats_repo.week_summary(session, since=since)
        revenue = await monetize_repo.revenue(session, since=since)
        summary["stars"] = int(revenue.get("stars", 0))
        summary["orders"] = int(revenue.get("orders", 0))
        summary["days"] = days
        return summary

    @staticmethod
    def digest_rows(summary: dict[str, int]) -> list[list[object]]:
        """``week_summary`` → table rows for the digest card (plain data only,
        so the tg layer owns the rendering)."""
        return [
            ["new players", summary.get("new_players", 0)],
            ["pulls", summary.get("pulls", 0)],
            ["character gifts", summary.get("gifts", 0)],
            ["raffles drawn", summary.get("raffles", 0)],
            ["⭐ Stars in", summary.get("stars", 0)],
            ["Stars orders", summary.get("orders", 0)],
            ["premium now", summary.get("subs_active", 0)],
        ]
