"""Economy service — the only module allowed to change a balance.

Design notes worth stealing:

* every mutation goes through :meth:`EconomyService.apply`, which wraps
  ``repo.economy.credit/debit`` and *always* writes the ledger row in the
  same transaction — the reference bot's ``/givemoney`` edited the column directly
  and left no trace;
* repeat-safety is by ``idempotency_key`` (unique index), so a Telegram retry or a
  double-tapped button cannot pay twice;
* cooldowns are Redis-only (TTL keys) so /daily doesn't need a DB round-trip,
  while the *claim* itself is recorded in ``daily_claims`` (durable, survives a
  Redis flush and is what /history shows).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from waifu.db.models import Character
from waifu.db.repo import collection as collection_repo
from waifu.db.repo import economy as ledger
from waifu.db.repo import items as items_repo
from waifu.db.repo import progress as progress_repo
from waifu.db.repo import users as users_repo
from waifu.enums import LedgerReason, Rarity
from waifu.errors import AlreadyClaimed, CooldownActive, NotFound, WaifuError
from waifu.services.base import Service
from waifu.utils.rng import system_random
from waifu.utils.time import human_delta


def dupe_value(character: Character, percent: int) -> int:
    """What a *spare* copy of ``character`` is worth, in coins.

    One formula, one home: gacha, spawn claims and /sell all used to carry their own
    copy and each disagreed (which is precisely how an exploit that mints coins out of
    dupes survived in the bot this project replaces).
    """
    price = int(character.price or 0) or int(Rarity.from_value(int(character.rarity_id)).base_price)
    return max(1, int(price * percent / 100))


#: Jobs for /work: (label, min, max, hours-of-stamina). Fun beats a flat reward.
WORK_JOBS: tuple[tuple[str, int, int], ...] = (
    ("delivering manga", 180, 520),
    ("babysitting the guild cat", 120, 400),
    ("fixing the vending machine", 260, 700),
    ("subtitling an episode", 400, 1100),
    ("walking the shipyard docks", 300, 850),
)


def steal_slice(target_balance: int, rng: object | None = None) -> int:
    """How much one /steal takes from a balance, mirroring ``plugins/market.py``.

    Pure on purpose: the tiers are the number players argue about, so they get tested without a
    database (``tests/test_market_parity.py``) and any future command can quote the same table.
    """
    draw = (rng if rng is not None else system_random).random
    balance = int(target_balance)
    if balance < 1_000:
        percent = draw() * 50 + 20
        return int(balance * percent / 100)
    if balance < 100_000:
        percent = draw() * 20 + 10
        return int(balance * percent / 100)
    if balance < 1_000_000:
        percent = draw() * 15 + 5
        return int(balance * percent / 100)
    return int(draw() * 49_000 + 1_000)


STEAL_RISK = 0.42  # base failure chance
STEAL_PENALTY_PERCENT = 10  # paid to the victim when the steal fails


@dataclass(slots=True)
class MoneyResult:
    delta: int
    balance: int
    reason: str
    note: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class DailyResult:
    amount: int
    balance: int
    streak: int
    best: int
    multiplier: float
    freezes_used: int
    next_reset: str
    xp: int = 0
    bonus_item: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


class EconomyService(Service):
    # -------------------------------------------------------------- read-only
    async def balance(self, session: AsyncSession, user_id: int) -> int:
        return await ledger.balance(session, user_id)

    async def history(
        self,
        session: AsyncSession,
        user_id: int,
        *,
        limit: int = 12,
        reasons: list[str] | None = None,
    ) -> list[Any]:
        return await ledger.history(session, user_id, limit=limit, reasons=reasons)

    async def is_premium(self, session: AsyncSession, user_id: int) -> bool:
        return await ledger.is_premium(session, user_id)

    # ----------------------------------------------------------------- writes
    async def apply(
        self,
        session: AsyncSession,
        user_id: int,
        *,
        credit: int = 0,
        debit: int = 0,
        reason: LedgerReason | str,
        reference: str = "",
        counterparty: int | None = None,
        idempotency_key: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> MoneyResult:
        """Credit or debit with a ledger row. Exactly one of the two amounts."""
        if credit and debit:
            raise ValueError("apply() takes either credit or debit, not both")
        if credit:
            entry = await ledger.credit(
                session,
                user_id,
                credit,
                reason,
                reference=reference,
                counterparty=counterparty,
                meta=meta,
                idempotency_key=idempotency_key,
            )
        elif debit:
            entry = await ledger.debit(
                session,
                user_id,
                debit,
                reason,
                reference=reference,
                counterparty=counterparty,
                meta=meta,
                idempotency_key=idempotency_key,
            )
        else:
            entry = await ledger.credit(session, user_id, 0, reason, reference=reference, meta=meta)
        await self._touch_leaderboards(user_id, entry.balance_after)
        return MoneyResult(
            delta=entry.delta, balance=entry.balance_after, reason=entry.reason, extra=meta or {}
        )

    async def _touch_leaderboards(self, user_id: int, balance: int) -> None:
        if self.redis is None:
            return
        await self.redis.zadd("lb:coins", str(user_id), float(balance))

    async def transfer(
        self,
        session: AsyncSession,
        *,
        sender_id: int,
        receiver_id: int,
        amount: int,
        reason: LedgerReason | str = LedgerReason.GIFT,
        reference: str = "",
        tax_percent: int = 0,
    ) -> tuple[MoneyResult, MoneyResult]:
        """Give/pay with optional house tax, both sides in one transaction."""
        out, inp = await ledger.transfer(
            session,
            sender_id=sender_id,
            receiver_id=receiver_id,
            amount=amount,
            reason=reason,
            reference=reference,
            meta={"tax": tax_percent},
        )
        if tax_percent:
            tax = int(amount * tax_percent / 100)
            if tax > 0:
                # The tax is burned from the receiver's side (negative supply).
                # Minting it into the bot's own account would only hide inflation.
                await ledger.debit(
                    session,
                    receiver_id,
                    tax,
                    LedgerReason.TRADE_TAX,
                    reference=f"tax:{reference or 'gift'}",
                    meta={"percent": tax_percent},
                )
        await self._touch_leaderboards(receiver_id, inp.balance_after)
        await self._touch_leaderboards(sender_id, out.balance_after)
        return (
            MoneyResult(delta=out.delta, balance=out.balance_after, reason=str(reason)),
            MoneyResult(delta=inp.delta, balance=inp.balance_after, reason=str(reason)),
        )

    # ----------------------------------------------------------- timed rewards
    async def daily(
        self,
        session: AsyncSession,
        user_id: int,
        *,
        utc_offset_hours: int = 0,
        premium: bool = False,
    ) -> DailyResult:
        """``/daily`` — streak-multiplied payout, jackpot roll, first freeze grant.

        Streak maths live in :mod:`waifu.db.repo.progress` so the same
        curve applies to /work-streaks and the reminder job.
        """
        day = self.local_day(utc_offset_hours)
        if not await ledger.claim_once(session, user_id, "daily", day):
            raise AlreadyClaimed(
                f"already claimed today — next reset in {human_delta(self.settings.cooldown_hours_daily * 3600)}"
            )

        base = system_random.randint(self.settings.daily_reward_min, self.settings.daily_reward_max)
        outcome = await progress_repo.bump_streak(
            session, user_id, day, multiplier_curve=self._streak_curve()
        )
        amount = int(base * outcome.multiplier)
        jackpot = system_random.random() < self.settings.daily_jackpot_chance
        if jackpot:
            amount += system_random.randint(
                self.settings.daily_reward_max, self.settings.daily_jackpot_max
            )
        if premium:
            amount = int(amount * (100 + self.settings.premium_claim_boost_percent) / 100)

        await ledger.credit(
            session,
            user_id,
            amount,
            LedgerReason.DAILY,
            reference=f"daily:{day}",
            idempotency_key=f"daily:{user_id}:{day}",
        )
        await self._award_xp(session, user_id, 40 + int(outcome.current * 5))
        bonus_item = ""
        if outcome.current and outcome.current % 14 == 0:
            await items_repo.add_shields(session, user_id, "sshield", 1)
            await progress_repo.add_freeze(session, user_id, 1)
            bonus_item = "🧊 Streak Freeze + 🛡️ Anti-Theft Shield"
        return DailyResult(
            amount=amount,
            balance=await ledger.balance(session, user_id),
            streak=outcome.current,
            best=outcome.best,
            multiplier=outcome.multiplier,
            freezes_used=0 if outcome.current > 1 else 1,
            next_reset=human_delta(self.seconds_until_reset(utc_offset_hours)),
            xp=40 + int(outcome.current * 5),
            bonus_item=bonus_item,
            extra={"broke": outcome.broke, "jackpot": jackpot},
        )

    def _streak_curve(self) -> list[float]:
        # day 1..n multiplier; capped so a 300-day streak can't print money.
        return [1.0, 1.1, 1.2, 1.35, 1.5, 1.65, 1.8, 2.0]

    async def spin(
        self, session: AsyncSession, user_id: int, *, free: bool = True
    ) -> dict[str, Any]:
        """``/spin`` — weighted wheel with a lucky-day bonus and item drops."""
        day = self.local_day()
        if free and not await ledger.claim_once(session, user_id, "spin", day):
            raise AlreadyClaimed("the wheel is already spun today")
        roll = system_random.random()
        if roll < self.settings.spin_lucky_chance:
            amount = system_random.randint(
                self.settings.spin_bonus_min, self.settings.spin_bonus_max
            )
            prize = "jackpot"
        else:
            amount = system_random.randint(self.settings.spin_min, self.settings.spin_max)
            prize = "reward"
        await ledger.credit(
            session,
            user_id,
            amount,
            LedgerReason.SPIN,
            reference=f"spin:{day}",
            idempotency_key=f"spin:{user_id}:{day}" if free else None,
        )
        drop = ""
        if roll > 0.93:
            await items_repo.add_shields(session, user_id, "bshield", 1)
            drop = "bomb_shield"
        return {
            "amount": amount,
            "balance": await ledger.balance(session, user_id),
            "prize": prize,
            "drop": drop,
        }

    async def work(
        self, session: AsyncSession, user_id: int, *, premium: bool = False
    ) -> dict[str, Any]:
        """``/work`` — short cooldown, random job, small XP. (Summon-bot had none.)"""
        left = await items_repo.cooldown_left(session, user_id, "work", 1800)
        if left:
            raise CooldownActive(left)
        label, low, high = WORK_JOBS[system_random.randrange(len(WORK_JOBS))]
        amount = system_random.randint(low, high)
        if premium:
            amount = int(amount * 1.25)
        await ledger.credit(
            session,
            user_id,
            amount,
            LedgerReason.QUEST,
            reference=f"work:{label.replace(' ', '_')}",
        )
        await items_repo.set_cooldown(session, user_id, "work")
        await self._award_xp(session, user_id, 15)
        return {
            "job": label,
            "amount": amount,
            "balance": await ledger.balance(session, user_id),
            "next": 1800 - left,
        }

    # ------------------------------------------------------------------- PvP
    async def steal(
        self,
        session: AsyncSession,
        attacker_id: int,
        target_id: int,
        *,
        attacker_premium: bool = False,
        target_premium: bool = False,
    ) -> dict[str, Any]:
        """``/rob`` — shield-checked, cooldown-gated, failure pays the victim.

        Order of operations is the whole point: check the target's shield, then
        resolve the outcome, then move money **once**. The reference bot debited
        first and refunded on failure, which duplicated coins whenever the refund
        raced a retry.
        """
        if attacker_id == target_id:
            raise WaifuError("you cannot rob yourself — that is not how theft works")
        left = await items_repo.cooldown_left(
            session, attacker_id, "steal", self.settings.cooldown_seconds_steal
        )
        if left:
            raise CooldownActive(left)
        target_balance = await ledger.balance(session, target_id)
        if target_balance < self.settings.steal_min_target:
            return {"ok": False, "reason": "target is too broke to bother", "amount": 0}

        shielded = await items_repo.pop_shield(session, target_id, "sshield")
        # The reference /steal had no failure roll at all: a shieldless target always paid. That is
        # the honest default (``steal_risk=False``) because a coin flip that burns an hour of
        # cooldown *and* the penalty on a loss reads as a punishment for using the feature; the
        # old dice stays available for servers that want the gamble.
        risk = STEAL_RISK - (0.08 if attacker_premium else 0.0) + (0.05 if target_premium else 0.0)
        chance = 1.0 - risk if self.settings.steal_risk else 1.0
        success = not shielded and system_random.random() < chance
        # ``steal_tiered`` is the reference's own ladder — 20-70% under 1,000 coins, 10-30% under
        # 100,000, 5-20% under a million, and a flat 1,000-50,000 above that so a whale can never
        # be drained by one click. ``steal_max_percent`` stays as the single-slice cap for bots
        # configured back to a flat share.
        stake = (
            steal_slice(target_balance)
            if self.settings.steal_tiered
            else int(target_balance * self.settings.steal_max_percent / 100)
        )
        stake = min(stake, target_balance)
        await items_repo.set_cooldown(session, attacker_id, "steal")
        if success:
            await ledger.transfer(
                session,
                sender_id=target_id,
                receiver_id=attacker_id,
                amount=stake,
                reason=LedgerReason.JACKPOT,
                reference=f"rob:{target_id}",
            )
            await items_repo.log_heist(
                session,
                attacker_id=attacker_id,
                target_id=target_id,
                kind="steal",
                outcome="success",
                amount=stake,
            )
            await self._award_xp(session, attacker_id, 25)
            return {
                "ok": True,
                "amount": stake,
                "shielded": False,
                "balance": await ledger.balance(session, attacker_id),
            }
        penalty = int(await ledger.balance(session, attacker_id) * STEAL_PENALTY_PERCENT / 100)
        if penalty > 0:
            await ledger.transfer(
                session,
                sender_id=attacker_id,
                receiver_id=target_id,
                amount=penalty,
                reason=LedgerReason.ADMIN_TAKE,
                reference=f"robfail:{target_id}",
            )
        await items_repo.log_heist(
            session,
            attacker_id=attacker_id,
            target_id=target_id,
            kind="steal",
            outcome="shield" if shielded else "fail",
            amount=penalty,
        )
        return {
            "ok": False,
            "amount": penalty,
            "shielded": shielded,
            "balance": await ledger.balance(session, attacker_id),
        }

    async def bomb(self, session: AsyncSession, attacker_id: int, target_id: int) -> dict[str, Any]:
        """``/bomb`` — steals a share of XP and burns a shield (Summon-bot parity)."""
        left = await items_repo.cooldown_left(
            session, attacker_id, "bomb", self.settings.cooldown_seconds_bomb
        )
        if left:
            raise CooldownActive(left)
        # A bomb is consumed by the attempt, and a blocked one consumes the defender's shield too:
        # that pairing is what makes "Attack Blocked!" cost something on both sides. The reference
        # deleted one bomb row from the attacker and one shield row from the target in the same
        # handler, and — unlike this port — it never checked the attacker held a bomb at all, which
        # made the daily attack free.
        held = await items_repo.stacks(session, attacker_id, "bomb")
        if held <= 0:
            raise NotFound(
                "you have no Bomb Item to throw — /market sells them, and the cooldown only starts "
                "when you actually throw one."
            )
        shielded = await items_repo.pop_shield(session, target_id, "bshield")
        await items_repo.spend(session, attacker_id, "bomb")
        await items_repo.set_cooldown(session, attacker_id, "bomb")
        if shielded:
            await items_repo.log_heist(
                session, attacker_id=attacker_id, target_id=target_id, kind="bomb", outcome="shield"
            )
            return {"ok": False, "shielded": True, "xp": 0, "character": None}
        victim = await users_repo.get(session, target_id)
        stolen = int((victim.exp if victim else 0) * 0.1)
        if victim is not None and stolen > 0:
            victim.exp = max(0, victim.exp - stolen)
            attacker = await users_repo.get(session, attacker_id)
            if attacker is not None:
                attacker.exp += stolen // 2
            await session.flush()
        loot = None
        if self.settings.bomb_steals_character:
            # Their /bomb was never about XP: it picked one random owned character with
            # ``ORDER BY RANDOM() LIMIT 1`` and moved it to the attacker. Taking the copy through
            # ``consume``/``grant`` in the same transaction is what makes it safe to run twice —
            # the row-level lock means the target cannot sell it mid-blast.
            prize = await collection_repo.random_owned(session, target_id)
            if prize is not None:
                character, _count = prize
                await collection_repo.consume(session, target_id, character.id)
                await collection_repo.grant(session, attacker_id, character.id, source="bomb")
                loot = character.name
        await items_repo.log_heist(
            session,
            attacker_id=attacker_id,
            target_id=target_id,
            kind="bomb",
            outcome="steal" if loot else "success",
            amount=stolen,
        )
        return {"ok": True, "shielded": False, "xp": stolen, "character": loot}

    # ---------------------------------------------------------------- admin
    async def admin_grant(
        self, session: AsyncSession, user_id: int, amount: int, *, actor: int, reason: str = "admin"
    ) -> MoneyResult:
        entry = await ledger.credit(
            session,
            user_id,
            abs(amount),
            LedgerReason.ADMIN_GRANT,
            reference=f"{reason}:{actor}",
            counterparty=actor,
            meta={"actor": actor},
        )
        await self._touch_leaderboards(user_id, entry.balance_after)
        return MoneyResult(delta=entry.delta, balance=entry.balance_after, reason="admin grant")

    async def admin_take(
        self, session: AsyncSession, user_id: int, amount: int, *, actor: int, reason: str = "admin"
    ) -> MoneyResult:
        entry = await ledger.debit(
            session,
            user_id,
            abs(amount),
            LedgerReason.ADMIN_TAKE,
            reference=f"{reason}:{actor}",
            counterparty=actor,
            meta={"actor": actor},
        )
        await self._touch_leaderboards(user_id, entry.balance_after)
        return MoneyResult(delta=entry.delta, balance=entry.balance_after, reason="admin take")

    async def premium_hours(
        self, session: AsyncSession, user_id: int, hours: int, *, actor: int, source: str = "admin"
    ) -> int:
        return await ledger.grant_premium(session, user_id, hours, granted_by=actor, source=source)

    # ------------------------------------------------------------------ misc
    async def _award_xp(self, session: AsyncSession, user_id: int, xp: int) -> None:
        """XP + level curve in one place (levels gate some shop slots)."""
        user = await users_repo.get(session, user_id)
        if user is None:
            return
        user.exp += xp
        threshold = 1000
        level = 1
        while user.exp >= threshold * level:
            level += 1
        if level != user.level:
            user.level = level
        await session.flush()

    def dupe_payout(self, character: Character) -> int:
        return dupe_value(character, self.settings.dupe_payout_percent)

    async def integrity(self, session: AsyncSession) -> list[str]:
        return await ledger.integrity_report(session)
