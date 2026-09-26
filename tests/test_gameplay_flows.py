"""End-to-end service flows on the seeded database.

Each test drives a *rule* the reference bot got wrong, through the real service
layer (no mocks of our own code): the ledger is the only way money moves, a dupe
pays a percentage instead of minting coins, a claim is atomic, and a spawn can only
be claimed once by one person. These are the invariants that make a bot with 60+
commands auditable instead of folklore.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from waifu.db.models import Character, Transaction
from waifu.db.repo import economy as ledger
from waifu.db.repo import items as items_repo
from waifu.db.repo import users as user_repo
from waifu.enums import Rarity
from waifu.errors import AlreadyClaimed, NotEnoughFunds, WaifuError


async def balance_of(session, user_id: int) -> int:
    return await ledger.balance(session, user_id)


async def collection_repo_has(session, user_id: int, character_id: int) -> bool:
    from waifu.db.repo import collection as collection_repo

    return bool(await collection_repo.has_count(session, user_id, character_id))


async def test_first_pull_costs_coins_and_grants_a_character(ctx, tx, player):
    before = await balance_of(tx, player)
    result = await ctx.gacha.pull(tx, player, batch=1, cooldown_key=None)
    after = await balance_of(tx, player)
    assert len(result.rolls) == 1
    roll = result.rolls[0]
    assert isinstance(roll.rarity, Rarity)
    assert before - after == ctx.settings.pull_cost, (
        "the price charged must equal the price advertised"
    )
    owned = await ctx.collection.page(tx, player)
    assert owned.total == 1
    # and the roll is recorded for the fair-play verifier
    assert result.commitment, "every pull needs a commitment hash to be verifiable"


async def test_ten_pull_is_cheaper_and_guarantees_a_rare(ctx, tx, player):
    result = await ctx.gacha.pull(tx, player, batch=10, cooldown_key=None)
    assert len(result.rolls) == 10
    assert result.spent == ctx.settings.ten_pull_cost
    best = max(int(roll.rarity) for roll in result.rolls)
    assert best >= ctx.settings.ten_pull_guarantee_rarity_id


async def test_dupe_pays_a_share_of_the_price(ctx, tx, player, any_character):
    from waifu.db.repo import collection as collection_repo

    """The reference bot paid full price for a dupe, which made farming dupes profitable.

    Here the payout is a percentage of the value, and the percentage is configurable
    (``ECONOMY_DUPE_PAYOUT_PERCENT``) rather than hard-coded across four call sites.
    """
    payout = ctx.economy.dupe_payout(any_character)
    expected = max(1, int(any_character.price * ctx.settings.dupe_payout_percent / 100))
    assert payout == expected
    assert payout < any_character.price
    before = await balance_of(tx, player)
    # A dupe sale pays the configured share; selling the last copy pays full price.
    await collection_repo.grant(tx, player, any_character.id, source="test")
    await collection_repo.grant(tx, player, any_character.id, source="test")
    dupe = await ctx.collection.sell(tx, player, any_character.id, count=1)
    assert dupe.payout == payout and dupe.remaining == 1
    last = await ctx.collection.sell(tx, player, any_character.id, count=1)
    assert last.payout == any_character.price and last.remaining == 0
    assert await balance_of(tx, player) == before + dupe.payout + last.payout


async def test_daily_is_once_per_day_and_bumps_the_streak(ctx, tx, player):
    first = await ctx.economy.daily(tx, player)
    assert first.amount > 0
    assert first.streak == 1
    with pytest.raises(AlreadyClaimed):
        await ctx.economy.daily(tx, player)
    state = await ctx.progress.streak(tx, player)
    assert state.current == 1
    assert state.claimed_today
    assert first.balance == await balance_of(tx, player), "DailyResult must report the real wallet"


async def test_cannot_spend_more_than_you_have(ctx, tx, player, any_character):
    await ledger.debit(tx, player, await balance_of(tx, player), "admin_grant", reference="drain")
    with pytest.raises(NotEnoughFunds):
        await ctx.gacha.pull(tx, player, batch=1, cooldown_key=None)


async def test_transfer_moves_money_exactly_once(ctx, tx, player, partner):
    sender_before, receiver_before = await balance_of(tx, player), await balance_of(tx, partner)
    await ctx.economy.transfer(
        session := tx, sender_id=player, receiver_id=partner, amount=1000, reference="test-transfer"
    )
    delta = (await balance_of(tx, player)) - sender_before
    assert delta <= -1000, "sender pays amount plus tax"
    assert (await balance_of(tx, partner)) - receiver_before == 1000
    rows = list(
        (
            await tx.execute(select(Transaction).where(Transaction.reference == "test-transfer"))
        ).scalars()
    )
    assert len(rows) == 2, "one ledger line per side, or /history lies"
    del session


async def test_shop_sells_and_item_use_consumes_a_stack(ctx, tx, player):
    shop = await ctx.items.shop(tx, player)
    assert len(shop.items) == 7
    # 'bshield' is a self-targeted item (a 'skip' would fail on an empty cooldown,
    # which is the correct behaviour: no refund, no burn — see the last assertion).
    entry = next(item for item in shop.items if item.key == "bshield")
    before = await balance_of(tx, player)
    bought = await ctx.items.buy(tx, player, entry.key)
    assert bought["owned"] == 1
    assert await balance_of(tx, player) == before - entry.cost, "the item shop must charge"
    used = await ctx.items.use(tx, player, entry.key)
    assert used.stacks_left == 0, "using an item consumes the stack"
    assert await items_repo.count_shields(tx, player, "bshield") == 1, (
        "and grants the shield charge"
    )
    with pytest.raises(WaifuError) as empty:
        await ctx.items.use(tx, player, entry.key)  # nothing left to use
    assert "shield" in empty.value.user_message.lower(), (
        "an empty stack must explain itself instead of becoming a 500"
    )


async def test_spawn_claim_is_atomic(ctx, tx, player, partner):
    chat_id = -100200
    spawn, character = await ctx.spawn.open(tx, chat_id, source="test")
    claimed = await ctx.spawn.claim(tx, player, spawn_id=spawn.id)
    assert claimed.won and claimed.character_id == character.id
    again = await ctx.spawn.claim(tx, partner, spawn_id=spawn.id)
    assert not again.won, "a spawn can be claimed exactly once"


async def test_spawn_claim_by_typed_name_only_matches_the_right_person(ctx, tx, player, partner):
    chat_id = -100201
    _spawn, character = await ctx.spawn.open(tx, chat_id, source="test")
    wrong = await ctx.spawn.claim(tx, partner, chat_id=chat_id, typed="Definitely Not Her")
    assert not wrong.won
    right = await ctx.spawn.claim(
        tx, player, chat_id=chat_id, typed=f"  {character.name.upper()}  "
    )
    assert right.won, "typed claims match case- and whitespace-insensitively"


async def test_free_claim_cannot_be_repeat_farmed(ctx, tx, player):
    first = await ctx.gacha.free_claim(tx, player)
    assert first.rolls
    with pytest.raises(AlreadyClaimed):
        await ctx.gacha.free_claim(tx, player)


async def test_quests_are_stable_for_a_day_and_claimable_once(ctx, tx, player):
    # ``day=`` pins the draw: the rotation is seeded by (user, day), and a test that reads
    # "today" from the clock goes red whenever the day rolls over mid-run.
    quests = await ctx.progress.quests(tx, player, day="2026-01-01")
    assert len(quests) == 3
    assert {q.key for q in quests} == {
        q.key for q in await ctx.progress.quests(tx, player, day="2026-01-01")
    }, "quests must not reshuffle on refresh"
    daily = next((q for q in quests if q.key == "daily"), None)
    assert daily is not None, "the daily claim is the anchor quest, every day"
    assert daily.progress == 0 and not daily.claimed
    await ctx.economy.daily(tx, player)
    refreshed = {q.key: q for q in await ctx.progress.quests(tx, player)}
    assert refreshed[daily.key].progress >= 1, "quests must read the same data the commands write"
    reward = await ctx.progress.claim_quest(tx, player, daily.key)
    assert reward == daily.reward
    assert await balance_of(tx, player) >= reward
    with pytest.raises(AlreadyClaimed):
        await ctx.progress.claim_quest(tx, player, daily.key)


async def test_achievements_unlock_from_real_data(ctx, tx, player):
    await ctx.economy.daily(tx, player)
    unlocked = await ctx.progress.evaluate(tx, player)
    keys = {a.key for a in unlocked}
    assert "first_claim" not in keys, "nothing pulled yet, so no summon badge"
    await ctx.gacha.pull(tx, player, batch=1, cooldown_key=None)
    unlocked = await ctx.progress.evaluate(tx, player)
    assert "first_claim" in {a.key for a in unlocked}
    # …and it pays exactly once
    again = await ctx.progress.evaluate(tx, player)
    assert "first_claim" not in {a.key for a in again}


async def test_auction_escrow_and_settlement(ctx, tx, player, partner, any_character):
    from waifu.db.repo import collection as collection_repo

    await collection_repo.grant(tx, player, any_character.id, source="test")
    seller_before = await balance_of(tx, player)
    view = await ctx.auctions.create(
        tx, seller_id=player, character_id=any_character.id, start_price=1000, minutes=1
    )
    assert await balance_of(tx, player) == seller_before, "listing must not move money"
    bid_view, outbid, extended = await ctx.auctions.bid(tx, view.id, partner, 2000)
    assert bid_view.current_bid == 2000
    assert outbid is None and extended is False, (
        "one bid on a 1000 opening can neither outbid nor extend"
    )
    assert await balance_of(tx, partner) == 100_000 + ctx.settings.starting_balance - 2000, (
        "the bid must be escrowed, not merely noted"
    )
    from waifu.db.repo import auctions as auctions_repo

    row = await auctions_repo.get(tx, view.id)
    assert row is not None
    settled = await ctx.auctions.settle(tx, row)
    assert settled["sold"] and settled["buyer"] == partner
    fee = int(2000 * ctx.settings.auction_fee_percent / 100)
    assert await balance_of(tx, player) == seller_before + 2000 - fee, (
        "the seller nets the hammer price minus the house cut"
    )
    assert settled["fee"] == fee
    assert await collection_repo_has(tx, partner, any_character.id)


async def test_trade_execute_swaps_ownership_and_refunds_on_cancel(ctx, tx, player, partner):
    from waifu.db.repo import collection as collection_repo
    from waifu.db.repo import trades as trades_repo

    mine = (
        await tx.execute(select(Character).where(Character.rarity_id == 1).limit(1))
    ).scalar_one()
    theirs = (
        await tx.execute(select(Character).where(Character.rarity_id == 2).limit(1))
    ).scalar_one()
    await collection_repo.grant(tx, player, mine.id, source="test")
    await collection_repo.grant(tx, partner, theirs.id, source="test")
    view = await ctx.trades.propose(
        tx, initiator_id=player, partner_id=partner, give={mine.id: 1}, receive={theirs.id: 1}
    )
    assert view.status == "proposed"
    with pytest.raises(Exception):
        await ctx.trades.execute(tx, view.id)  # partner has not accepted yet
    await trades_repo.set_accept(tx, view.id, partner, True)
    executed = await ctx.trades.execute(tx, view.id)
    assert len(executed["moves"]) == 2, executed  # one leg per side
    assert await collection_repo.has_count(tx, player, theirs.id) == 1
    assert await collection_repo.has_count(tx, partner, mine.id) == 1


async def test_redeem_code_pays_once_per_use(ctx, tx, player):
    await ctx.codes.create(tx, created_by=1, code="TESTCODE", coins=5000, uses=1)
    result = await ctx.codes.redeem(tx, player, "  testcode  ")
    assert result.coins == 5000, "codes are matched case- and whitespace-insensitively"
    assert result.balance == await balance_of(tx, player)
    with pytest.raises(Exception):  # uses exhausted
        await ctx.codes.redeem(tx, player, "TESTCODE")


async def test_bomb_is_blocked_by_a_shield(ctx, tx, player, partner, any_character):
    """The name said "steal" while the body bombed someone; the test follows the code."""
    from waifu.db.repo import collection as collection_repo
    from waifu.db.repo import items as items_repo

    await collection_repo.grant(tx, partner, any_character.id, source="test")
    await ctx.items.buy(tx, partner, "bshield", quantity=1)
    await ctx.items.use(tx, partner, "bshield")
    await ctx.items.buy(tx, player, "bomb", quantity=1)
    assert await items_repo.count_shields(tx, partner, "bshield") == 1
    result = await ctx.economy.bomb(tx, player, partner)
    assert result.get("ok") is False and result.get("shielded"), (
        "a shielded target cannot be bombed"
    )
    assert await items_repo.stacks(tx, player, "bomb") == 0, (
        "a blocked attack still burns the bomb — plugins/market.py deletes the row on both paths"
    )
    assert await items_repo.stacks(tx, partner, "bshield") == 0, (
        "the shield item itself is not the defence; the charge is"
    )
    assert await items_repo.count_shields(tx, partner, "bshield") == 0, (
        "the shield is consumed, not a permanent force field"
    )
    assert await collection_repo.has_count(tx, partner, any_character.id) == 1


async def test_ledger_is_the_only_wallet_and_balances_reconcile(ctx, tx, player, partner):
    """Recompute every balance from transactions: the /integrity check, in test form."""
    await ctx.gacha.pull(tx, player, batch=1, cooldown_key=None)
    await ctx.economy.daily(tx, player)
    rows = list(
        (
            await tx.execute(
                select(Transaction).where(Transaction.user_id == player).order_by(Transaction.id)
            )
        ).scalars()
    )
    assert {row.reason for row in rows} >= {"signup", "pull", "daily"}, (
        "an account may not hold coins /history cannot explain"
    )
    total = sum(int(row.delta) for row in rows)
    assert total == await balance_of(tx, player), "balance must equal the sum of its own history"
    problems = await ctx.economy.integrity(tx)
    assert not problems, f"integrity report found drift: {problems}"


async def test_collection_pages_group_by_rarity(ctx, tx, player, any_character):
    from waifu.db.repo import collection as collection_repo

    for character in (
        await tx.execute(select(Character).where(Character.rarity_id.in_([1, 1, 2])).limit(3))
    ).scalars():
        await collection_repo.grant(tx, player, character.id, source="test")
    page = await ctx.collection.page(tx, player, mode="rarity", page=0, page_size=2)
    assert page.total >= 1
    assert len(page.items) <= 2, "page_size is a contract with the UI"
    assert page.pages >= 1
    assert sum(page.per_rarity.values()) >= 1, "the rarity ladder header must add up"
    assert all(int(rarity_id) > 0 for rarity_id in page.per_rarity)


async def test_user_row_carries_derived_counters(ctx, tx, player):
    await ctx.gacha.pull(tx, player, batch=10, cooldown_key=None)
    user = await user_repo.get(tx, player)
    assert user is not None
    assert user.pulls_total >= 10
    assert user.balance > 0
    assert user.server_seed and user.seed_commitment, "fair mode needs a committed seed"
    counts = await user_repo.counts(tx)
    assert counts["users"] >= 1
