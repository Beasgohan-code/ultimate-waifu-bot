"""Collection-page parity with Summon-bot, plus the paging contract our UI depends on.

Their ``/collection`` is the single most-used screen in the whole bot, and the two things
people rely on it for are (a) ``/collection 3`` jumping to page three and (b)
``/collection Naruto`` narrowing to one series — the *same argument slot* meaning two
different things, which is why :func:`entry_args` exists here and is tested first.

Everything below deliberately avoids fake Telegram objects: the renderer helpers
(:func:`_page_rows`, :func:`_pager_extra`) are pure, and the filtering is asserted against
the service. A handler test that monkeypatches ``Message`` proves nothing about what a
player sees and breaks on every aiogram upgrade.
"""

from __future__ import annotations

import pytest

from waifu.db.repositories import characters as char_repo
from waifu.db.repositories import collection as collection_repo
from waifu.plugins._kit import Args
from waifu.plugins.collection import _page_rows, _pager_extra, _parse_extra, entry_args


def _args(text: str) -> Args:
    return (
        Args.of_text(text)
        if hasattr(Args, "of_text")
        else Args(raw=text, words=tuple(text.split()))
    )


def test_collection_argument_slot_matches_their_behaviour() -> None:
    """/collection 3 → page three · /collection Naruto → that series · /collection 2 dupes."""
    assert entry_args(_args("3"), default_mode="rarity") == (2, "rarity", "")
    assert entry_args(_args("Naruto"), default_mode="rarity") == (0, "anime", "Naruto")
    assert entry_args(_args("2 power"), default_mode="rarity") == (1, "power", "")
    assert entry_args(_args(""), default_mode="rarity") == (0, "rarity", "")
    # A page number out of range is clamped by the service, never an error page.
    assert entry_args(_args("0"), default_mode="rarity")[0] == 0
    assert entry_args(_args("One Piece"), default_mode="power") == (0, "anime", "One Piece")


async def test_pager_state_travels_in_the_callback_data(ctx, tx, player) -> None:
    """Two players paging at once must not share a cursor.

    Summon-bot kept ``page`` in ``context.user_data`` — one dict per user, so opening a
    second collection page (or running two bots on the same session) reset the first. The
    view state here is encoded in the button and decoded on click.
    """
    extra = _pager_extra(mode="anime", query="Naruto", rarity_id=3)
    assert extra.count("/") == 2
    assert _parse_extra(extra) == ("anime", 3, "Naruto")
    # Callback data has a 64-byte budget; a long filter is truncated, not dropped.
    long = _pager_extra(mode="rarity", query="x" * 40, rarity_id=None)
    assert len(f"col:next:{long}".encode()) <= 64
    assert _parse_extra(long)[2] == "x" * 14


async def test_pages_and_buttons(ctx, tx, player, any_character) -> None:
    from waifu.db.repositories import characters as chars

    roster, _found = await chars.search(tx, "", limit=6)
    assert len(roster) >= 4, "the shipped catalogue must be big enough to page through"
    for character in roster[:4]:
        await collection_repo.grant(tx, player, int(character.id), source="test")

    page = await ctx.collection.page(tx, player, page_size=2)
    assert page.total == 4 and page.pages == 2, (page.total, page.pages)
    assert len(page.items) == 2

    rows = _page_rows(page, mode="rarity", query="", rarity_id=None, self_view=True)
    flat = [button.callback_data for row in rows for button in row if button.callback_data]
    assert any(data.startswith("col:next:") for data in flat), flat
    # With only two pages the pager drops ⏭ (first/prev/next/« » are enough) — the same
    # compaction the reference bot's 5-button row did when it had nothing to skip to.
    assert not any(data.startswith("col:last:") for data in flat)
    assert any(data.startswith("col:mode:") for data in flat), (
        "the mode row is what replaces /hmode"
    )

    # Another player's view of the same harem must not offer the owner-only controls.
    theirs = _page_rows(page, mode="rarity", query="", rarity_id=None, self_view=False)
    assert not any((b.callback_data or "").startswith("col:mode:") for row in theirs for b in row)


async def test_series_filter_and_grouping(ctx, tx, player) -> None:
    """``/collection <series>`` narrows the page *and* switches to the series layout."""
    roster, _found = await char_repo.search(tx, "", limit=40)
    by_series: dict[str, list] = {}
    for character in roster:
        by_series.setdefault(character.anime or "?", []).append(character)
    first_series, first_group = next(
        (key, rows) for key, rows in by_series.items() if len(rows) >= 2
    )
    other = next(rows for key, rows in by_series.items() if key != first_series)[0]

    for character in [*first_group[:2], other]:
        await collection_repo.grant(tx, player, int(character.id), source="test")

    narrowed = await ctx.collection.page(tx, player, mode="anime", query=first_series, page_size=10)
    assert narrowed.items, "the series filter must find its own rows"
    assert {item.anime for item in narrowed.items} == {first_series}
    assert narrowed.total == 2, "the other series must be filtered out"

    grouped = await ctx.collection.page(tx, player, mode="anime", page_size=10)
    series = [item.anime for item in grouped.items]
    adjacent = [
        name for index, name in enumerate(series) if index == 0 or name != series[index - 1]
    ]
    assert len(adjacent) == len(set(adjacent)), f"grouped view interleaved a series: {series}"


async def test_rarity_and_dupe_filters(ctx, tx, player, any_character) -> None:
    roster, _found = await char_repo.search(tx, "", limit=30)
    high = max(roster, key=lambda item: int(item.rarity_id))
    await collection_repo.grant(tx, player, int(high.id), source="test")
    await collection_repo.grant(tx, player, int(high.id), source="test")

    filtered = await ctx.collection.page(tx, player, rarity_id=int(high.rarity_id), page_size=10)
    assert filtered.items and all(item.rarity_id == int(high.rarity_id) for item in filtered.items)
    # ``per_rarity`` counts *characters* per tier (that is what the tier row shows), so a
    # single character with two copies is 1 there and 2 in the row's own count.
    assert filtered.per_rarity.get(int(high.rarity_id)) == 1, "per-rarity counts drive the tier row"
    assert filtered.items[0].count == 2

    dupes = await ctx.collection.page(tx, player, mode="dupes", page_size=10)
    assert dupes.total == 1 and dupes.items[0].count == 2, (
        "dupes mode is how you find what is worth selling"
    )

    missing = await ctx.collection.page(
        tx, player, rarity_id=int(high.rarity_id) + 90, page_size=10
    )
    assert not missing.items and missing.total == 0, "an empty tier is an empty page, not an error"


async def test_page_value_matches_the_price_table(ctx, tx, player) -> None:
    roster, _found = await char_repo.search(tx, "", limit=3)
    expected = 0
    for character in roster:
        await collection_repo.grant(tx, player, int(character.id), source="test")
        expected += await char_repo.price_for(tx, character)
    page = await ctx.collection.page(tx, player, page_size=10)
    assert page.value == expected, (page.value, expected)


@pytest.mark.parametrize("page_index", [0, 1, 2])
async def test_every_page_of_a_big_harem_renders(ctx, tx, player, page_index) -> None:
    """Paging must be total: the union of the pages is the whole collection."""
    roster, _found = await char_repo.search(tx, "", limit=9)
    for character in roster:
        await collection_repo.grant(tx, player, int(character.id), source="test")
    page = await ctx.collection.page(tx, player, page_size=3, page=page_index)
    assert len(page.items) == 3
    # The service takes a 0-based page and returns the 1-based number to print (and to put
    # in the "2/3" label), which is why /collection 2 subtracts one on the way in.
    assert page.page == page_index + 1
    assert page.pages == 3
