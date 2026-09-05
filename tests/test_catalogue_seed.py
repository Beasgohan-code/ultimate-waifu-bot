"""The default database: 18 tiers and a real roster, not an empty shell.

These assertions exist because the reference bot shipped an empty ``characters``
table and a first run crashed in ``/shop`` (and its rarity table was keyed on display
strings, so odds and prices could drift apart). A fresh install must be *playable*.
"""

from __future__ import annotations

from sqlalchemy import func, select

from waifu.db.models import Character, ClaimChance, RarityChance
from waifu.db.seed import DEFAULT_CLAIM_ODDS, DEFAULT_ODDS, catalogue_rows
from waifu.enums import Rarity


def test_the_ladder_is_theirs():
    """18 tiers, same order, same labels as Summon-bot's live ``rarity_chances``."""
    assert [int(r) for r in Rarity] == list(range(1, 19))
    assert Rarity.COMMON.label == "Common"
    assert Rarity.SPECIAL.label == "Special Edition"
    assert Rarity.LIMITED.label == "Limited Edition"
    assert Rarity.AMV.emoji == "🎥"
    assert Rarity.CELESTIAL.base_price == 1_000_000
    assert Rarity.LIMITED.base_price == 2_000_000


def test_rarity_labels_round_trip_from_legacy_strings():
    """Their rows store "🎥 AMV Edition"; ``from_label`` is the migration door."""
    for member in Rarity:
        assert Rarity.from_label(member.badge) is member
        assert Rarity.from_label(member.label) is member
        assert Rarity.from_label(str(int(member))) is member
    assert Rarity.from_label("🎥 AMV Edition") is Rarity.AMV
    assert Rarity.from_label("AMV edition") is Rarity.AMV
    assert Rarity.from_label("") is Rarity.COMMON


def test_high_tier_boundary_matches_the_reference_bot():
    """Their ``HIGH_TIER`` set starts at Valentine; ours must too (it gates /hclaim,
    /bomb and the premium boost, so a shifted boundary silently rebalances the game)."""
    assert Rarity.VALENTINE.is_high_tier
    assert not Rarity.MYTHIC.is_high_tier
    assert len(Rarity.high_tiers()) == 13


async def test_odds_rows_cover_every_tier(tx):
    pull = list(
        (
            await tx.execute(
                select(RarityChance.rarity_id, RarityChance.chance, RarityChance.is_enabled)
            )
        ).all()
    )
    claim = list((await tx.execute(select(ClaimChance.rarity_id))).scalars())
    assert len(pull) == 18, "one row per tier, or /chance has nothing to display"
    assert {row[0] for row in pull} == set(range(1, 19))
    assert all(row[1] > 0 for row in pull), "a 0% base tier means /pull can never land there"
    assert {int(rid) for rid in claim} == set(range(1, 19))


def test_odds_sum_to_100_percent():
    assert abs(sum(DEFAULT_ODDS.values()) - 100.0) < 0.01
    assert abs(sum(DEFAULT_CLAIM_ODDS.values()) - 100.0) < 0.01


def test_catalogue_file_has_every_tier_and_no_duplicates():
    rows = catalogue_rows()
    assert len(rows) >= 150, "the shipped roster is the default database; it must not be a demo"
    seen = {(row["name"], row.get("anime", "")) for row in rows}
    assert len(seen) == len(rows), "duplicate (name, anime) rows would be silently dropped on seed"
    tiers = {int(row["rarity_id"]) for row in rows}
    assert tiers == set(range(1, 19)), (
        f"tiers with no characters would make /pull crash: {sorted(set(range(1, 19)) - tiers)}"
    )


async def test_seeded_database_is_playable(tx):
    """Counting through the ORM, not the JSON: proves the seed SQL actually ran."""
    total = int((await tx.execute(select(func.count()).select_from(Character))).scalar_one())
    per_tier = dict(
        (
            await tx.execute(
                select(Character.rarity_id, func.count()).group_by(Character.rarity_id)
            )
        ).all()
    )
    priced = int(
        (
            await tx.execute(select(func.count()).select_from(Character).where(Character.price > 0))
        ).scalar_one()
    )
    assert total >= 150
    assert priced == total, "an unpriced character makes /sell and /shop divide-by-zero territory"
    assert all(per_tier.get(tier, 0) > 0 for tier in range(1, 19))
