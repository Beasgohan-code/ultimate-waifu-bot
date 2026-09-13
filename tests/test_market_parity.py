"""Market parity: the numbers and words ``plugins/market.py`` of Summon-bot uses.

Each assertion names the reference behaviour it pins, so a future "improvement" has to be argued
against a citation rather than against memory. Where this port deliberately differs (no free
``/bomb``, no purchase without an expiry, no penalty on a shielded raid), the divergence is
asserted here too — a difference nobody wrote a test for is a difference that silently regresses.
"""

from __future__ import annotations

import pytest
from sqlalchemy import update

from waifu.db.models import InventoryItem
from waifu.db.repositories import characters as char_repo
from waifu.db.repositories import collection as collection_repo
from waifu.db.repositories import economy as ledger
from waifu.db.repositories import items as items_repo
from waifu.errors import AlreadyClaimed, Locked, NotFound, WaifuError
from waifu.services.economy import steal_slice
from waifu.services.gacha import GachaService
from waifu.utils.time import now_utc


class _Rng:
    """A stand-in for ``random`` that always returns the same draw."""

    def __init__(self, value: float) -> None:
        self.value = value

    def random(self) -> float:
        return self.value


# --------------------------------------------------------------------- /steal
# ``steal_cmd`` slices the *target's* balance by tier: 20-70% under 1,000, 10-30% under
# 100,000, 5-20% under 1,000,000, and a flat 1,000-50,000 above that.
@pytest.mark.parametrize(
    ("balance", "draw", "expected"),
    [
        (500, 0.0, 100),  # 20% of 500
        (500, 1.0, 350),  # 70% of 500
        (999, 0.0, 199),  # bottom tier ends at 999
        (1_000, 0.0, 100),  # 10% of 1,000
        (50_000, 1.0, 15_000),  # 30% of 50,000
        (100_000, 0.0, 5_000),  # 5% of 100,000
        (999_999, 1.0, 199_999),  # 20% of just under a million
        (1_000_000, 0.0, 1_000),  # flat band starts here
        (9_999_999, 1.0, 50_000),  # …and never takes more than 50,000
    ],
)
def test_steal_slice_matches_the_reference_tiers(balance: int, draw: float, expected: int) -> None:
    assert steal_slice(balance, rng=_Rng(draw)) == expected


def test_steal_slice_never_drains_a_whale_in_one_click() -> None:
    """Above a million the reference stops percentage-draining: a 500M player loses at most 50k."""
    assert max(steal_slice(500_000_000, rng=_Rng(v)) for v in (0.0, 0.5, 0.999)) <= 50_000


async def test_steal_takes_the_tier_slice_not_a_stake(ctx, tx, player, partner) -> None:
    """``/rob`` in the reference takes a slice of the victim's purse; the caller's money is at risk
    only when the optional failure dice is switched on."""
    before_target = await ledger.balance(tx, partner)
    result = await ctx.economy.steal(tx, player, partner)
    assert result["ok"] is True, result
    taken = int(result["amount"])
    assert 0 < taken <= before_target
    assert before_target - taken == await ledger.balance(tx, partner)


async def test_steal_refuses_a_target_below_the_floor(ctx, tx, player, partner) -> None:
    """``min_target = 100`` in ``steal_cmd``."""
    held = await ledger.balance(tx, partner)
    await ledger.debit(
        tx, partner, held - 50, "admin_take", reference="parity:poor", idempotency_key="p1"
    )
    attacker_before = await ledger.balance(tx, player)
    result = await ctx.economy.steal(tx, player, partner)
    assert result["ok"] is False
    assert "broke" in str(result.get("reason", "")).lower(), (
        "the reference answered 'Too poor!' — this port says the same thing in its own words"
    )
    assert await ledger.balance(tx, player) == attacker_before, "no cooldown penalty for a no-op"


async def test_steal_dice_only_fires_when_steal_risk_is_on(ctx, tx, player, partner, monkeypatch):
    """The reference has no failure roll. Ours is opt-in, and the opt-in has to actually work."""
    monkeypatch.setattr(ctx.settings, "steal_risk", True)
    monkeypatch.setattr("waifu.services.economy.STEAL_RISK", 1.0)
    result = await ctx.economy.steal(tx, player, partner)
    assert result["ok"] is False, "a 100% risk setting must be able to lose the raid"


# ----------------------------------------------------------------------- /bomb
async def test_bomb_needs_a_bomb_and_hijacks_a_character(ctx, tx, player, partner, any_character):
    """``bomb_cmd`` deletes one ``bomb`` row and then takes one random owned character."""
    await collection_repo.grant(tx, partner, any_character.id, source="test")
    with pytest.raises(NotFound):
        await ctx.economy.bomb(tx, player, partner)
    await ctx.items.buy(tx, player, "bomb", quantity=1)
    result = await ctx.economy.bomb(tx, player, partner)
    assert result["ok"] is True
    assert result["character"] == any_character.name
    assert await items_repo.stacks(tx, player, "bomb") == 0, "the bomb is consumed, not reused"
    assert await collection_repo.has_count(tx, partner, any_character.id) == 0
    assert await collection_repo.has_count(tx, player, any_character.id) == 1


async def test_bomb_cannot_take_a_locked_copy(ctx, tx, player, partner, any_character):
    """The reference looted ``user_collection`` with no lock check; a pinned favourite is safe here."""
    from waifu.db.models import Ownership

    await collection_repo.grant(tx, partner, any_character.id, source="test")
    await tx.execute(
        update(Ownership)
        .where(Ownership.user_id == partner, Ownership.character_id == any_character.id)
        .values(is_locked=True)
    )
    await ctx.items.buy(tx, player, "bomb", quantity=1)
    result = await ctx.economy.bomb(tx, player, partner)
    assert result["ok"] is True
    assert result["character"] is None, "a locked collection is not loot"
    assert await collection_repo.has_count(tx, partner, any_character.id) == 1


async def test_bomb_blocked_still_burns_the_bomb(ctx, tx, player, partner):
    """On a block the reference consumed the attacker's bomb *and* the defender's shield."""
    await ctx.items.buy(tx, player, "bomb", quantity=1)
    await ctx.items.buy(tx, partner, "bshield", quantity=1)
    await ctx.items.use(tx, partner, "bshield")
    result = await ctx.economy.bomb(tx, player, partner)
    assert result["ok"] is False and result["shielded"] is True
    assert await items_repo.stacks(tx, player, "bomb") == 0
    assert await items_repo.count_shields(tx, partner, "bshield") == 0


# ------------------------------------------------------------- inventory expiry
async def test_bought_items_carry_the_reference_expiry(ctx, tx, player):
    """``buy_item`` stamped ``expires_at = now + 24h`` on every purchase."""
    await ctx.items.buy(tx, player, "bomb", quantity=2)
    hours = await items_repo.expiry_hours(tx, player)
    assert 1 <= hours["bomb"] <= 24, hours


async def test_expired_stacks_vanish_from_every_view(ctx, tx, player):
    """Expired rows are excluded from ``stacks``, ``inventory`` and ``spend`` alike."""
    await ctx.items.buy(tx, player, "bomb", quantity=1)
    await tx.execute(
        update(InventoryItem)
        .where(InventoryItem.user_id == player, InventoryItem.item_id == "bomb")
        .values(expires_at=now_utc().replace(year=2020))
    )
    assert await items_repo.stacks(tx, player, "bomb") == 0
    assert [entry.key for entry in await ctx.items.inventory(tx, player)] == []
    with pytest.raises((NotFound, Locked, WaifuError)):
        await items_repo.spend(tx, player, "bomb")


async def test_spend_eats_the_stack_that_rots_first(ctx, tx, player):
    """``buy_item`` picked ``ORDER BY expires_at ASC LIMIT 1``: a player holding a stale stack and a
    fresh one loses the stale one, which is the only fair reading of an expiring currency. Two rows
    are inserted directly because ``bomb`` is capped at one per player — the cap is an anti-hoarding
    rule, and this is about which row a use takes."""
    from datetime import timedelta

    stale = InventoryItem(
        user_id=player,
        item_id="bomb",
        uses_remaining=1,
        expires_at=now_utc() + timedelta(hours=1),
    )
    fresh = InventoryItem(user_id=player, item_id="bomb", uses_remaining=1, expires_at=None)
    tx.add_all([stale, fresh])
    await tx.flush()
    assert await items_repo.stacks(tx, player, "bomb") == 2
    await items_repo.spend(tx, player, "bomb")
    assert stale.uses_remaining == 0, "the row closest to rotting paid for the use"
    assert fresh.uses_remaining == 1
    assert await items_repo.stacks(tx, player, "bomb") == 1


async def test_premium_players_pay_nothing_and_are_told(ctx, tx, player):
    """``market_cmd``: "👑 Premium Mode Active: All items are FREE!" and price 0 in the queries."""
    shop = await ctx.items.shop(tx, player, premium=True)
    assert shop.premium_free is True
    assert all(entry.cost == 0 and entry.free for entry in shop.items), shop.items
    plain = await ctx.items.shop(tx, player, premium=False)
    assert plain.premium_free is False
    assert all(entry.cost > 0 and not entry.free for entry in plain.items)


async def test_price_of_follows_the_knob(ctx, tx, player, monkeypatch):
    definition = items_repo.item("bomb")
    assert ctx.items.price_of(definition, premium=True) == 0
    monkeypatch.setattr(ctx.settings, "premium_free_items", False)
    assert ctx.items.price_of(definition, premium=True) == int(definition.cost)
    assert ctx.items.price_of(definition, premium=False) == int(definition.cost)


# ------------------------------------------------------------------- /skip 1-3
async def test_skip_modes_match_skip_cmd(ctx, tx, player, monkeypatch):
    """``/skip 2`` loads a 🛡️ bomb shield, ``/skip 3`` a 🔒 steal shield, ``/skip 1`` resets
    ``/daily`` — and each mode burns exactly one ticket."""
    await ctx.items.buy(tx, player, "skip", quantity=3)
    bomb = await ctx.items.skip_mode(tx, player, "2")
    assert "🛡️" in bomb.effect and await items_repo.count_shields(tx, player, "bshield") == 1
    steal = await ctx.items.skip_mode(tx, player, "3")
    assert "🔒" in steal.effect and await items_repo.count_shields(tx, player, "sshield") == 1
    assert bomb.stacks_left == 2 and steal.stacks_left == 1
    await items_repo.set_cooldown(tx, player, "daily")  # the row /skip 1 exists to delete
    skipped = await ctx.items.skip_mode(tx, player, "1")
    assert "daily" in skipped.effect
    assert await items_repo.cooldown_left(tx, player, "daily", 86_400) == 0
    assert skipped.stacks_left == 0


async def test_skip_with_an_unknown_mode_is_free(ctx, tx, player):
    """The reference decremented the ticket on paths that then did nothing. Ours refuses first."""
    await ctx.items.buy(tx, player, "skip", quantity=1)
    with pytest.raises(Locked):
        await ctx.items.skip_mode(tx, player, "9")
    assert await items_repo.stacks(tx, player, "skip") == 1
    with pytest.raises(AlreadyClaimed):
        await ctx.items.skip_mode(tx, player, "1")  # nothing on cooldown
    assert await items_repo.stacks(tx, player, "skip") == 1, "a no-op must not cost 25,000 coins"
    await tx.execute(InventoryItem.__table__.delete().where(InventoryItem.user_id == player))
    with pytest.raises(NotFound):
        await ctx.items.skip_mode(tx, player, "2")


def test_skip_usage_lists_the_three_modes() -> None:
    from waifu.services.items import ItemService

    usage = ItemService.SKIP_USAGE
    for needle in (
        "Multi-Purpose Skip Cooldown Usage",
        "/skip 1",
        "/daily",
        "/skip 2",
        "/skip 3",
    ):
        assert needle in usage, needle


# ------------------------------------------------------------------ /hclaim
async def test_hclaim_rolls_the_claim_ladder_not_the_paid_one(ctx, tx, player, monkeypatch):
    """The bug this closes: ``pull()`` asked for ``odds(premium=…)`` and never passed ``claim``,
    so ``/hclaim`` secretly used the paid weights while ``/chances`` advertised the claim table."""
    from waifu.enums import PullKind

    seen: list[bool] = []
    real = GachaService.odds

    async def spy(self, session, *, claim: bool = False, premium: bool = False):
        seen.append(claim)
        return await real(self, session, claim=claim, premium=premium)

    monkeypatch.setattr(GachaService, "odds", spy)
    await ctx.gacha.pull(
        tx, player, batch=1, kind=PullKind.FREE_DAILY, free=True, cooldown_key=None
    )
    await ctx.gacha.pull(tx, player, batch=1, cooldown_key=None)
    assert seen == [True, False], seen


async def test_hclaim_quota_is_one_and_premium_is_two(ctx, tx, player, monkeypatch):
    """``hclaim_command``: ``max_claims = 2 if premium else 1`` per day."""
    monkeypatch.setattr(ctx.settings, "hclaim_daily_limit", 1)
    monkeypatch.setattr(ctx.settings, "hclaim_premium_daily_limit", 2)
    first = await ctx.gacha.free_claim(tx, player, premium=True)
    assert (first.claims_today, first.claims_limit) == (1, 2)
    second = await ctx.gacha.free_claim(tx, player, premium=True)
    assert (second.claims_today, second.claims_limit) == (2, 2)
    assert second.premium_claim is True
    with pytest.raises(AlreadyClaimed) as exc:
        await ctx.gacha.free_claim(tx, player, premium=True)
    assert "2 random characters" in exc.value.user_message


async def test_premium_claim_multiplier_is_applied_to_the_claim_table(ctx, tx, player, monkeypatch):
    """``final_chance = chance * 3.0`` for high rarities when premium claims."""
    monkeypatch.setattr(ctx.settings, "premium_claim_boost_percent", 0)
    monkeypatch.setattr(ctx.settings, "hclaim_premium_multiplier", 3.0)
    base = dict(await ctx.gacha.odds(tx, claim=True, premium=False))
    boosted = dict(await ctx.gacha.odds(tx, claim=True, premium=True))
    from waifu.services.gacha import HIGH_TIER_FLOOR

    highs = [rarity for rarity in base if int(rarity) >= HIGH_TIER_FLOOR]
    assert highs, "the seeded roster has high tiers to boost"
    for rarity in highs:
        assert boosted[rarity] > base[rarity], rarity
    for rarity, chance in base.items():
        if int(rarity) < HIGH_TIER_FLOOR:
            assert boosted[rarity] < chance, "the table is renormalised to 100%"


async def test_hclaim_refuses_when_no_edition_is_claimable(ctx, tx, player, monkeypatch):
    """ "❌ All claimable editions are currently turned OFF!" — refused, not silently rerolled."""

    async def empty(session, **kwargs):  # mirrors ``SELECT … FROM claim_list WHERE chance > 0``
        return []

    monkeypatch.setattr(char_repo, "claim_chances", empty)
    with pytest.raises(WaifuError) as exc:
        await ctx.gacha.free_claim(tx, player)
    assert "turned OFF" in exc.value.user_message


async def test_hclaim_counts_reset_per_local_day(ctx, tx, player):
    """``claims_today`` reads the same slots ``free_claim`` writes, so the two cannot disagree."""
    used, limit = await ctx.gacha.claims_today(tx, player)
    assert (used, limit) == (0, max(1, int(ctx.settings.hclaim_daily_limit)))
    await ctx.gacha.free_claim(tx, player)
    assert await ctx.gacha.claims_today(tx, player) == (1, limit)
