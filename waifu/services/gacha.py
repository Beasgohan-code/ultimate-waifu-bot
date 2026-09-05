"""Gacha service — provably fair pulls with pity and premium weighting.

Why this is not ``random.choice``:

Summon-bot rolled with ``random.randint`` inside the handler. Nobody could check
the odds, and "the bot is rigging it" was the single most common complaint in its
support chat. Here every roll is derived from a **committed server seed**:

* ``users.server_seed`` is random per player; ``sha256(seed)`` (the commitment) is
  published on /profile and in every pull receipt *before* the roll is revealed;
* ``roll = HMAC-SHA256(seed, "player:sequence:salt") → [0,1)`` — deterministic,
  so /verify recomputes the exact value, and the seed can be rotated on demand
  (a player who suspects a rigged seed can demand a reveal + rotation);
* every roll is stored in ``fair_rolls`` with its sequence index, so a batch of ten
  is auditable as one unit.

Pity and the ten-pull guarantee are applied *after* the roll value is fixed but are
also recorded (``was_pity``), so the audit trail explains every upgrade instead of
hiding it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from waifu.db.models import Character
from waifu.db.repositories import characters as char_repo
from waifu.db.repositories import collection as collection_repo
from waifu.db.repositories import economy as ledger
from waifu.db.repositories import progress as progress_repo
from waifu.db.repositories import users as user_repo
from waifu.enums import LedgerReason, PullKind, Rarity
from waifu.errors import CooldownActive
from waifu.services.base import Service
from waifu.services.economy import dupe_value
from waifu.utils.rng import commit, uniform_from, weighted_choice

#: Rarity id at which the "high tier" pity counter resets (SPECIAL+).
HIGH_TIER_FLOOR = int(Rarity.SPECIAL)


@dataclass(slots=True)
class RollResult:
    index: int
    character_id: int
    name: str
    anime: str
    rarity: Rarity
    is_dupe: bool
    payout: int
    roll_value: float
    was_pity: bool
    image: str = ""
    stat_power: int = 0

    @property
    def badge(self) -> str:
        return self.rarity.badge

    @property
    def headline(self) -> str:
        return f"{self.name}" + (f" — {self.anime}" if self.anime else "")


@dataclass(slots=True)
class PullResult:
    kind: str
    sequence: int
    commitment: str
    rolls: list[RollResult] = field(default_factory=list)
    spent: int = 0
    dupe_payout: int = 0
    new_count: int = 0
    balance: int = 0
    pity: progress_repo.PityState | None = None
    seed_reveal: str = ""

    @property
    def best(self) -> RollResult:
        return max(self.rolls, key=lambda r: (int(r.rarity), r.stat_power))

    @property
    def net(self) -> int:
        return self.dupe_payout - self.spent

    @property
    def new_ones(self) -> list[RollResult]:
        return [r for r in self.rolls if not r.is_dupe]


class GachaService(Service):
    # ------------------------------------------------------------------ odds
    async def odds(
        self, session: AsyncSession, *, claim: bool = False, premium: bool = False
    ) -> list[tuple[Rarity, float]]:
        """Published odds, with the premium boost already applied.

        Honest display matters more than marketing: /chance shows exactly the
        weights used to roll, because the roll itself is verifiable.
        """
        table = await char_repo.normalised_odds(session, claim=claim)
        if not premium:
            return table
        boost = 1.0 + self.settings.premium_claim_boost_percent / 100
        weighted = [(r, c * boost if int(r) >= HIGH_TIER_FLOOR else c) for r, c in table]
        total = sum(c for _, c in weighted) or 1.0
        return [(r, c / total * 100.0) for r, c in weighted]

    async def pity(self, session: AsyncSession, user_id: int) -> progress_repo.PityState:
        return await progress_repo.pity(session, user_id)

    # ------------------------------------------------------------------ rolls
    async def pull(
        self,
        session: AsyncSession,
        user_id: int,
        *,
        batch: int = 1,
        kind: PullKind = PullKind.SINGLE,
        premium: bool = False,
        free: bool = False,
        cooldown_key: str | None = "pull",
    ) -> PullResult:
        """Run ``batch`` rolls, pay for them, apply dupe payouts and pity.

        The whole batch is one transaction: a crash halfway through cannot leave a
        player charged for five of ten rolls.
        """
        if cooldown_key and self.redis is not None:
            left = await self.redis.cooldown_left(cooldown_key, user_id)
            if left:
                raise CooldownActive(left)

        user = await user_repo.get(session, user_id)
        if user is None:
            raise LookupError("player not registered")
        cost = 0 if free else self.cost_for(batch)
        if cost:
            await ledger.debit(
                session,
                user_id,
                cost,
                LedgerReason.PULL,
                reference=f"{kind}:{batch}",
                idempotency_key=None,
            )

        sequence = await progress_repo.next_sequence(session, user_id)
        seed = user.server_seed or ""
        commitment = user.seed_commitment or commit(seed)
        table = await self.odds(session, premium=premium)
        rarities = [r for r, _ in table]
        weights = [c for _, c in table]
        pity = await progress_repo.pity(session, user_id)
        guarantee = self.settings.ten_pull_guarantee_rarity_id if batch >= 10 else 0

        rolls: list[RollResult] = []
        payout_total = 0
        new_count = 0
        for index in range(batch):
            forced, pity_hit = self._pity_override(pity, index, batch_total=batch)
            value = uniform_from(
                seed=seed, player_id=user_id, sequence=sequence, salt=f"{kind}:{index}"
            )
            rarity = forced if forced is not None else rarities[weighted_choice(weights, value)]
            if guarantee and index == batch - 1 and all(int(r.rarity) < guarantee for r in rolls):
                rarity = max(rarity, Rarity.from_value(guarantee))
                pity_hit = pity_hit or True
            character = await self._pick_character(session, rarity)
            if character is None:  # empty pool → pay the player instead of failing
                payout_total += rarity.base_price
                continue
            owned = await collection_repo.has_count(session, user_id, character.id)
            is_dupe = owned > 0
            payout = 0
            if is_dupe:
                payout = self.dupe_payout(character)
                payout_total += payout
            else:
                new_count += 1
            await collection_repo.grant(session, user_id, character.id, source=str(kind))
            await progress_repo.record_roll(
                session,
                user_id=user_id,
                sequence=sequence,
                index_in_batch=index,
                kind=str(kind),
                commitment=commitment,
                seed=seed,
                roll_value=value,
                rarity_id=int(rarity),
                character_id=character.id,
                was_dupe=is_dupe,
                was_pity=pity_hit,
                payout=payout,
            )
            await progress_repo.apply_pity(
                session,
                user_id,
                got_rarity_id=int(rarity),
                rare_at=self.settings.pity_high_after // 3 or 8,
                high_at=self.settings.pity_high_after,
            )
            pity = await progress_repo.pity(session, user_id)
            rolls.append(
                RollResult(
                    index=index,
                    character_id=character.id,
                    name=character.name,
                    anime=character.anime or "",
                    rarity=rarity,
                    is_dupe=is_dupe,
                    payout=payout,
                    roll_value=value,
                    was_pity=pity_hit,
                    image=character.image_ref(),
                    stat_power=character.stat_power,
                )
            )

        if payout_total:
            await ledger.credit(
                session, user_id, payout_total, LedgerReason.DUPE, reference=f"dupe:{sequence}"
            )
        if self.redis is not None and cooldown_key and not free:
            await self.redis.start_cooldown(
                (cooldown_key, user_id), self.settings.pull_cooldown_seconds
            )

        if self.redis is not None:
            # The "high" board has to be fed here: /top reads Redis, and a board that
            # only SQL could answer would make /top a per-view table scan.
            if any(
                int(roll.rarity) >= self.settings.ten_pull_guarantee_rarity_id for roll in rolls
            ):
                await self.redis.zincr("lb:high", str(user_id), 1)
            await self.redis.incr("stat:pulls", ttl=86400 * 3, amount=len(rolls))

        return PullResult(
            kind=str(kind),
            sequence=sequence,
            commitment=commitment,
            rolls=rolls,
            spent=cost,
            dupe_payout=payout_total,
            new_count=new_count,
            balance=await ledger.balance(session, user_id),
            pity=await progress_repo.pity(session, user_id),
            seed_reveal=seed,
        )

    def _pity_override(
        self, pity: progress_repo.PityState, index: int, *, batch_total: int
    ) -> tuple[Rarity | None, bool]:
        """Pity is checked per roll against the counters as they stand."""
        if pity.celestial >= self.settings.pity_celestial_after:
            return Rarity.CELESTIAL, True
        if pity.high >= self.settings.pity_high_after:
            return Rarity.LEGENDARY, True
        if pity.rare >= max(1, self.settings.pity_high_after // 3):
            return Rarity.RARE, True
        return None, False

    async def _pick_character(self, session: AsyncSession, rarity: Rarity) -> Character | None:
        character = await char_repo.random_of_rarity(session, int(rarity), banner_only=False)
        if character is None:
            # Tier disabled or empty pool: fall back to the nearest tier below so a
            # pull always gives *something* (Summon-bot crashed the handler here).
            for step in range(int(rarity) - 1, 0, -1):
                character = await char_repo.random_of_rarity(session, step)
                if character is not None:
                    break
        return character

    def dupe_payout(self, character: Character) -> int:
        # Shared with the spawn claim and /sell so the three cannot drift apart.
        return dupe_value(character, self.settings.dupe_payout_percent)

    def cost_for(self, batch: int) -> int:
        if batch >= 10:
            return self.settings.ten_pull_cost
        return self.settings.pull_cost * max(1, batch)

    # -------------------------------------------------------- /hclaim (free)
    async def free_claim(
        self, session: AsyncSession, user_id: int, *, premium: bool = False
    ) -> PullResult:
        """``/hclaim`` — the daily free roll against the *claim* table.

        Capped at ``spawn_high_tier_ceiling`` unless premium: a free daily roll that
        can hit Celestial destroys the paid loop (and the reference bot's economy
        within a week).
        """
        day = self.local_day()
        claimed = await ledger.claimed(session, user_id, "hclaim", day)
        if claimed:
            from waifu.errors import AlreadyClaimed

            raise AlreadyClaimed("no free claim left today")
        result = await self.pull(
            session,
            user_id,
            batch=1,
            kind=PullKind.FREE_DAILY,
            premium=premium,
            free=True,
            cooldown_key=None,
        )
        # Cap the outcome: rewrite the award if the claim table exceeded the ceiling.
        ceiling = Rarity.from_value(self.settings.spawn_high_tier_ceiling)
        if not premium and any(int(r.rarity) > int(ceiling) for r in result.rolls):
            for roll in result.rolls:
                if int(roll.rarity) > int(ceiling):
                    downgrade = await char_repo.random_of_rarity(session, int(ceiling))
                    if downgrade is not None:
                        await collection_repo.consume(session, user_id, roll.character_id, count=1)
                        await collection_repo.grant(
                            session, user_id, downgrade.id, source="hclaim_cap"
                        )
                        roll.character_id = downgrade.id
                        roll.name = downgrade.name
                        roll.anime = downgrade.anime or ""
                        roll.rarity = ceiling
                        roll.image = downgrade.image_ref()
        await ledger.claim_once(session, user_id, "hclaim", day)
        return result

    # ------------------------------------------------------------- auditing
    async def history(self, session: AsyncSession, user_id: int, *, limit: int = 12) -> list[Any]:
        return await progress_repo.roll_rows(session, user_id, limit=limit)

    async def verify(self, session: AsyncSession, user_id: int, sequence: int) -> dict[str, Any]:
        """Recompute a batch from its stored seed — the player's proof, in one call.

        Also usable by /verify <seq> and by the Mini App's "check this pull" button,
        which is what makes the claim list credible.
        """
        rows = await progress_repo.roll_detail(session, user_id, sequence)
        table = await char_repo.normalised_odds(session)
        rarities = [r for r, _ in table]
        weights = [c for _, c in table]
        out = []
        for row in rows:
            recomputed = uniform_from(
                seed=row.seed_reveal,
                player_id=user_id,
                sequence=row.sequence,
                salt=f"{row.kind}:{row.index_in_batch}",
            )
            expected = rarities[weighted_choice(weights, recomputed)] if weights else None
            out.append(
                {
                    "index": row.index_in_batch,
                    "roll": row.roll_value,
                    "recomputed": recomputed,
                    "matches": abs(recomputed - row.roll_value) < 1e-12,
                    "rarity": int(row.rarity_id),
                    "expected_rarity": int(expected) if expected else None,
                    "character_id": row.character_id,
                    "was_pity": row.was_pity,
                    "commitment": row.commitment,
                    "commitment_ok": hashlib.sha256(row.seed_reveal.encode()).hexdigest()
                    == row.commitment,
                }
            )
        return {
            "sequence": sequence,
            "rolls": out,
            "all_match": bool(out) and all(r["matches"] and r["commitment_ok"] for r in out),
        }

    async def reveal_and_rotate(self, session: AsyncSession, user_id: int) -> dict[str, str]:
        """Give the player their seed, publish the commitment of the next one."""
        user = await user_repo.get(session, user_id)
        old = user.server_seed if user else ""
        new_seed = await progress_repo.rotate_seed(session, user_id)
        return {"revealed": old, "commitment": commit(new_seed), "next_seed_prefix": new_seed[:8]}

    async def global_stats(self, session: AsyncSession, *, since: Any = None) -> dict[str, float]:
        return await progress_repo.global_roll_stats(session, since=since)

    async def player_stats(self, session: AsyncSession, user_id: int) -> dict[str, float | int]:
        return await progress_repo.roll_stats(session, user_id)
