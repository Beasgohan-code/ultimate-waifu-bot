"""Auction parity with ``commands_auction.py`` — and the places this port refuses to copy it.

The reference auction is a pinned caption, four bid buttons and a timer that edited the SQLite
row directly. Its money model is what this port cannot reproduce: coins were never moved when a
bid was placed, so settlement ran ``UPDATE users SET balance = balance - ?`` against whatever the
winner happened to have left — a bidder could win five auctions with one wallet, and a winner who
spent their coins in between paid less than they promised (or nothing). Everything below that
involves money is asserted against the escrow, and everything cosmetic is asserted verbatim.
"""

from __future__ import annotations

import pytest

from waifu.db.repo import auctions as auction_repo
from waifu.db.repo import collection as collection_repo
from waifu.db.repo import economy as ledger
from waifu.db.repo import users as users_repo
from waifu.enums import AuctionStatus
from waifu.errors import BidTooLow, Locked, NotFound, WaifuError
from waifu.services.auction import fmt_bid, format_time_left, progress_bar


# --------------------------------------------------------------- presentation
def test_fmt_bid_uses_the_references_dot_separator() -> None:
    """``fmt(n) = f"{n:,}".replace(",", ".")`` — players type these numbers back at us."""
    assert fmt_bid(12500) == "12.500"
    assert fmt_bid(1_000_000) == "1.000.000"
    assert fmt_bid(0) == "0"


def test_progress_bar_is_ten_cells_of_the_reference_glyphs() -> None:
    assert progress_bar(45) == "▓▓▓▓░░░░░░"
    assert progress_bar(0) == "░" * 10
    assert progress_bar(100) == "▓" * 10
    assert progress_bar(-40) == "░" * 10 and progress_bar(400) == "▓" * 10


def test_format_time_left_matches_the_reference_clock() -> None:
    assert format_time_left(3725) == "1h 02m 05s"
    assert format_time_left(125) == "2m 05s"
    assert format_time_left(-10) == "0m 00s"


# ------------------------------------------------------------------- listing
async def test_start_price_floor_is_the_references_100(ctx, tx, player, any_character):
    """``if start_price < 100: "❌ Min starting bid: 100 🪙"``."""
    with pytest.raises(WaifuError) as exc:
        await ctx.auctions.create(
            tx, seller_id=player, character_id=any_character.id, start_price=99
        )
    assert "100" in exc.value.user_message


async def test_duration_is_clamped_to_the_configured_window(ctx, tx, player, any_character):
    """``max(5, min(180, int(args[2])))`` in ``cmd_auction`` — with the bounds in settings."""
    await collection_repo.grant(tx, player, any_character.id, source="test")
    short = await ctx.auctions.create(
        tx, seller_id=player, character_id=any_character.id, start_price=1_000, minutes=1
    )
    assert short.seconds_left <= int(ctx.settings.auction_min_minutes) * 60 + 5
    assert short.duration_seconds >= int(ctx.settings.auction_min_minutes) * 60 - 5
    await ctx.auctions.cancel(tx, short.id, player, force=True)
    await collection_repo.grant(tx, player, any_character.id, source="test")
    long = await ctx.auctions.create(
        tx, seller_id=player, character_id=any_character.id, start_price=1_000, minutes=10_000
    )
    assert long.seconds_left <= int(ctx.settings.auction_max_minutes) * 60 + 5


async def test_progress_bar_measures_this_auction_not_thirty_minutes(
    ctx, tx, player, any_character
):
    """The reference divided every auction by its 1,800-second default, so a 3-hour listing sat at
    100% after 25 minutes. The bar has to come from the row's own span."""
    await collection_repo.grant(tx, player, any_character.id, source="test")
    view = await ctx.auctions.create(
        tx, seller_id=player, character_id=any_character.id, start_price=1_000, minutes=60
    )
    assert view.duration_seconds == pytest.approx(3600, abs=10)
    assert view.progress <= 5, "a listing that just opened cannot be near its end"


async def test_listing_only_moves_one_copy(ctx, tx, player, any_character):
    """``cmd_auction`` ran ``DELETE FROM user_collection``: a seller with two copies listed one and
    lost both. Locking the row keeps the second copy where it belongs."""
    await collection_repo.grant(tx, player, any_character.id, count=2, source="test")
    await ctx.auctions.create(
        tx, seller_id=player, character_id=any_character.id, start_price=1_000
    )
    assert await collection_repo.has_count(tx, player, any_character.id) == 2, "still owned"
    locked = await collection_repo.locked_ids(tx, player)
    assert any_character.id in locked, "and not sellable/giftable while it is on the block"


# ---------------------------------------------------------------------- bids
async def test_seller_cannot_bid_on_their_own_auction(ctx, tx, player, any_character):
    """``🚫 You can't bid on your own auction.``"""
    await collection_repo.grant(tx, player, any_character.id, source="test")
    view = await ctx.auctions.create(
        tx, seller_id=player, character_id=any_character.id, start_price=1_000
    )
    with pytest.raises(Locked) as exc:
        await ctx.auctions.bid(tx, view.id, player, 5_000)
    assert "own auction" in exc.value.user_message


async def test_bid_must_beat_the_minimum_increment(ctx, tx, player, partner, any_character):
    """``MIN_BID_INCREMENT`` exists in the reference but only the custom path enforces anything;
    here every path goes through the same floor."""
    await collection_repo.grant(tx, player, any_character.id, source="test")
    view = await ctx.auctions.create(
        tx,
        seller_id=player,
        character_id=any_character.id,
        start_price=1_000,
        min_increment=5_000,
    )
    with pytest.raises(BidTooLow) as exc:
        await ctx.auctions.bid(tx, view.id, partner, 1_500)
    assert exc.value.minimum == 5_000, "the floor is max(start_price, high bid + increment)"
    with pytest.raises(BidTooLow):
        await ctx.auctions.bid(tx, view.id, partner, 4_999)
    await ctx.auctions.bid(tx, view.id, partner, 5_000)


async def test_quick_bid_steps_respect_the_increment(ctx, tx, player, any_character):
    """``build_auction_keyboard`` offered +500/+1,000/+5,000/+10,000; a step the auction would
    reject is not offered."""
    await collection_repo.grant(tx, player, any_character.id, source="test")
    view = await ctx.auctions.create(
        tx,
        seller_id=player,
        character_id=any_character.id,
        start_price=1_000,
        min_increment=5_000,
    )
    assert view.steps(5_000) == [5_000, 10_000]
    assert view.steps(1) == [500, 1_000, 5_000, 10_000]
    _builder, buttons = ctx.auctions.card(view)
    texts = [button.text for button in buttons]
    assert any("+5.000" in text for text in texts), texts
    assert any("Custom bid" in text for text in texts), texts


async def test_a_bid_escrows_immediately_and_the_losers_stake_comes_back(
    ctx, tx, player, partner, any_character
):
    """The reference moved no money until close. Here the stake leaves at bid time, and every
    superseded stake is refunded in the same transaction — so ``/balance`` always shows what is
    actually spendable, and one wallet can never win two auctions at once."""
    rival = 5150
    await users_repo.upsert(tx, rival, username="rival", first_name="Rival")
    await ledger.credit(tx, rival, 100_000, "admin_grant", reference="test:auction")
    await collection_repo.grant(tx, player, any_character.id, source="test")
    view = await ctx.auctions.create(
        tx,
        seller_id=player,
        character_id=any_character.id,
        start_price=1_000,
        minutes=30,
        min_increment=500,
    )
    partner_before = await ledger.balance(tx, partner)
    _v1, outbid_one, _ = await ctx.auctions.bid(tx, view.id, partner, 2_000)
    assert await ledger.balance(tx, partner) == partner_before - 2_000
    assert outbid_one is None, "the first bid cannot outbid anyone"

    view2, outbid_two, _ = await ctx.auctions.bid(tx, view.id, rival, 2_500)
    assert outbid_two == partner
    assert await ledger.balance(tx, partner) == partner_before, "the loser is made whole at once"
    assert view2.previous_bid == 2_000, (
        "the receipt quotes the stake that was released — not the winner's number minus a step, "
        "which is how the reference got it wrong when two bids arrived together"
    )
    # ``+500`` is ``STARTING_BALANCE``: a user row is funded the first time the ledger sees it.
    assert await ledger.balance(tx, rival) == await ledger.balance(tx, rival)
    assert await ledger.balance(tx, rival) == 100_000 + ctx.settings.starting_balance - 2_500


async def test_seller_cannot_be_the_winner_of_their_own_escrow(
    ctx, tx, player, partner, any_character
):
    await collection_repo.grant(tx, player, any_character.id, source="test")
    view = await ctx.auctions.create(
        tx, seller_id=player, character_id=any_character.id, start_price=1_000
    )
    await ctx.auctions.bid(tx, view.id, partner, 5_000)
    with pytest.raises(Locked):
        await ctx.auctions.bid(tx, view.id, player, 6_000)


# ---------------------------------------------------------------- anti-snipe
async def test_late_bid_extends_the_clock_by_the_settings_not_a_constant(
    ctx, tx, player, partner, any_character, monkeypatch
):
    """``ANTI_SNIPE_EXTEND = 60`` — with the caveat that this port reads its window and extension
    from ``auction_snipe_window`` / ``auction_extend_seconds``. Those knobs were dead before: the
    repository applied hard-coded module constants instead, so changing them changed only the
    message the player was shown. This test moves the settings and asserts the row agrees."""
    monkeypatch.setattr(ctx.settings, "auction_snipe_window", 600)
    monkeypatch.setattr(ctx.settings, "auction_extend_seconds", 90)
    await collection_repo.grant(tx, player, any_character.id, source="test")
    view = await ctx.auctions.create(
        tx, seller_id=player, character_id=any_character.id, start_price=1_000, minutes=5
    )
    # Pull the closing time inside the (widened) window so the guard has to fire.
    from datetime import timedelta

    from waifu.db.models import Auction

    row = await tx.get(Auction, view.id)
    row.ends_at = row.ends_at - timedelta(seconds=180)  # 120s left: inside the widened window
    await tx.flush()
    from waifu.db.models import Auction as AuctionModel
    from waifu.utils.time import to_naive_utc

    before = to_naive_utc(row.ends_at)
    updated, _outbid, extended = await ctx.auctions.bid(tx, view.id, partner, 5_000)
    assert extended is True, "the widened window has to be the one the repository applies"
    after = to_naive_utc((await tx.get(AuctionModel, view.id)).ends_at)
    assert 85 <= (after - before).total_seconds() <= 95, "extended by exactly the knob's 90s"
    assert updated.extended is True


async def test_extensions_are_capped(ctx, tx, player, partner, any_character, monkeypatch):
    """Unlimited extension is a bidding war that never ends; ``auction_max_extensions`` stops it."""
    monkeypatch.setattr(ctx.settings, "auction_snipe_window", 3600)
    monkeypatch.setattr(ctx.settings, "auction_extend_seconds", 60)
    monkeypatch.setattr(ctx.settings, "auction_max_extensions", 1)
    await collection_repo.grant(tx, player, any_character.id, source="test")
    view = await ctx.auctions.create(
        tx, seller_id=player, character_id=any_character.id, start_price=1_000, minutes=10
    )
    _, _o, first = await ctx.auctions.bid(tx, view.id, partner, 2_000)
    assert first is True
    _v, _o, second = await ctx.auctions.bid(tx, view.id, partner, 3_000)
    assert second is False, "the cap is reached: a war cannot extend forever"


# ------------------------------------------------------------------ settling
async def test_settle_accepts_an_id_because_the_job_passes_one(
    ctx, tx, player, partner, any_character
):
    """``core/jobs.py`` claims a candidate row and then calls ``settle(session, auction_id)``.
    The service signature only accepted the ORM object, so every scheduled settlement raised
    ``AttributeError`` on an ``int`` and auctions sat open forever."""
    await collection_repo.grant(tx, player, any_character.id, source="test")
    view = await ctx.auctions.create(
        tx, seller_id=player, character_id=any_character.id, start_price=1_000
    )
    await ctx.auctions.bid(tx, view.id, partner, 4_000)
    outcome = await ctx.auctions.settle(tx, int(view.id))
    assert outcome["sold"] is True
    assert outcome["amount"] == 4_000
    assert outcome["name"] == any_character.name, "the job's notification uses this"
    assert await collection_repo.has_count(tx, partner, any_character.id) == 1
    assert await collection_repo.has_count(tx, player, any_character.id) == 0


async def test_settlement_pays_the_seller_minus_the_fee_and_refunds_the_rest(
    ctx, tx, player, partner, any_character
):
    await collection_repo.grant(tx, player, any_character.id, source="test")
    view = await ctx.auctions.create(
        tx, seller_id=player, character_id=any_character.id, start_price=1_000
    )
    seller_before = await ledger.balance(tx, player)
    bidder_before = await ledger.balance(tx, partner)
    await ctx.auctions.bid(tx, view.id, partner, 10_000)
    outcome = await ctx.auctions.settle(tx, view.id)
    fee = int(10_000 * ctx.settings.auction_fee_percent / 100)
    assert outcome["fee"] == fee
    assert await ledger.balance(tx, player) == seller_before + 10_000 - fee
    assert await ledger.balance(tx, partner) == bidder_before - 10_000, (
        "the winner's stake is spent, not refunded — and it left at bid time, so it could not be "
        "double-committed to a second auction"
    )
    assert await ledger.balance(tx, partner) >= 0


async def test_no_bids_returns_the_character_and_unlocks_it(ctx, tx, player, any_character):
    await collection_repo.grant(tx, player, any_character.id, source="test")
    view = await ctx.auctions.create(
        tx, seller_id=player, character_id=any_character.id, start_price=1_000
    )
    outcome = await ctx.auctions.settle(tx, view.id)
    assert outcome["sold"] is False
    assert outcome["refunds"] == 0
    assert outcome["name"] == any_character.name
    assert await collection_repo.has_count(tx, player, any_character.id) == 1
    assert any_character.id not in await collection_repo.locked_ids(tx, player)


async def test_reserve_not_met_leaves_the_character_with_the_seller(
    ctx, tx, player, partner, any_character
):
    """No reference equivalent — but an auction that sells below the seller's floor is a bug they
    shipped, so the reserve is enforced at settlement, not at listing."""
    await collection_repo.grant(tx, player, any_character.id, source="test")
    view = await ctx.auctions.create(
        tx,
        seller_id=player,
        character_id=any_character.id,
        start_price=1_000,
        reserve_price=50_000,
    )
    await ctx.auctions.bid(tx, view.id, partner, 2_000)
    outcome = await ctx.auctions.settle(tx, view.id)
    assert outcome["sold"] is False and outcome["reason"] == "reserve not met"
    assert await collection_repo.has_count(tx, partner, any_character.id) == 0
    assert await ledger.balance(tx, partner) > 0, "the bid came back"


# -------------------------------------------------------------------- cancel
async def test_cancel_is_refused_once_bids_exist(ctx, tx, player, partner, any_character):
    """``❌ Already has bids — can't cancel.`` — the reference checked ``COUNT(auction_bids)``; this
    checks the row's own counter and lets staff override it."""
    await collection_repo.grant(tx, player, any_character.id, source="test")
    view = await ctx.auctions.create(
        tx, seller_id=player, character_id=any_character.id, start_price=1_000
    )
    cancelled, _refunds = await ctx.auctions.cancel(tx, view.id, player)  # no bids: allowed
    assert cancelled.status == str(AuctionStatus.CANCELLED)
    assert any_character.id not in await collection_repo.locked_ids(tx, player)
    second = await ctx.auctions.create(
        tx, seller_id=player, character_id=any_character.id, start_price=1_000
    )
    await ctx.auctions.bid(tx, second.id, partner, 3_000)
    with pytest.raises(Locked) as exc:
        await ctx.auctions.cancel(tx, second.id, player)
    assert "bids" in exc.value.user_message
    auction, refunds = await ctx.auctions.cancel(tx, second.id, player, force=True)
    assert refunds == 1
    assert auction.status == str(AuctionStatus.CANCELLED)


async def test_cancelling_returns_the_escrowed_stake(ctx, tx, player, partner, any_character):
    await collection_repo.grant(tx, player, any_character.id, source="test")
    view = await ctx.auctions.create(
        tx, seller_id=player, character_id=any_character.id, start_price=1_000
    )
    before = await ledger.balance(tx, partner)
    await ctx.auctions.bid(tx, view.id, partner, 3_000)
    assert await ledger.balance(tx, partner) == before - 3_000
    await ctx.auctions.cancel(tx, view.id, player, force=True)
    assert await ledger.balance(tx, partner) == before, "force-cancel gives the bid back"
    assert await collection_repo.has_count(tx, player, any_character.id) == 1


# ------------------------------------------------------------------ the list
async def test_live_list_reports_next_minimum_and_clock(ctx, tx, player, partner, any_character):
    await collection_repo.grant(tx, player, any_character.id, source="test")
    view = await ctx.auctions.create(
        tx,
        seller_id=player,
        character_id=any_character.id,
        start_price=5_000,
        min_increment=1_000,
    )
    await ctx.auctions.bid(tx, view.id, partner, 6_000)
    views, total = await ctx.auctions.live(tx, limit=10)
    assert total >= 1
    listed = next(item for item in views if item.id == view.id)
    assert listed.current_bid == 6_000
    assert listed.next_minimum == 7_000
    assert listed.clock.endswith("s") and "m" in listed.clock
    assert listed.is_live is True


async def test_missing_auction_raises_not_found(ctx, tx, player):
    with pytest.raises(NotFound):
        await ctx.auctions.view(tx, 999_999)
    assert await auction_repo.stats(tx) is not None
