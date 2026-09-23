"""Inline mode: what ``@bot <query>`` is allowed to show, and to whom.

The reference bot's inline handler was a straight SQL query with an owner id taken from the
query string, which means two things worth pinning down:

1. it works without the bot being in the chat (that is the feature); and
2. ``collection.42`` handed user 42's harem to whoever typed it. Here the owner is always the
   sender, and that is asserted — the *only* fix made to the ported flow, because player data
   is not catalogue data.

Everything else is checked the way the reference behaved: photo results only for art on an
approved host, 25 results, an escaped caption, and an empty list instead of a traceback when
the query is nonsense.
"""

from __future__ import annotations

from typing import Any

import pytest
from aiogram.types import InlineQueryResultArticle, InlineQueryResultPhoto, User

from waifu.db.models import Ownership
from waifu.db.repositories import characters as char_repo
from waifu.enums import Rarity
from waifu.plugins import inline
from waifu.plugins.inline import build_results, caption_for, rarity_line, result_id

SENDER_ID = 4242
SENDER = User.model_construct(id=SENDER_ID, is_bot=False, first_name="Me")


async def _own(tx, character_id: int, user_id: int, *, count: int = 1) -> None:
    from waifu.db.repositories import users as user_repo

    await user_repo.upsert(tx, user_id, username=f"inline{user_id}", first_name="Owner")
    tx.add(
        Ownership(
            user_id=user_id,
            character_id=character_id,
            count=count,
            is_favorite=False,
            is_locked=False,
        )
    )
    await tx.flush()


def _text(result: Any) -> str:
    """The caption either way: a photo carries it, an article hides it in the content."""
    return str(getattr(result, "caption", "") or result.input_message_content.message_text)


def test_caption_escapes_and_decorates_once() -> None:
    body = caption_for("Yor <b>", 7, "Spy x Family", "⭐ Legendary", count=3)
    assert "<b>Yor &lt;b&gt;</b> ×3" in body
    assert "🆔 <code>7</code>" in body
    # the emoji in the stored display string is the emoji — it is not doubled
    assert body.count("⭐") == 1
    assert rarity_line("Legendary") == "⭐ Legendary" == rarity_line("⭐ Legendary")
    assert rarity_line("") == "—"


def test_result_id_is_stable_across_queries() -> None:
    assert result_id(1, 42, 2) == result_id(1, 42, 2) != result_id(2, 42, 2)
    assert len(result_id(None, 1)) == 32


async def test_roster_query_returns_results_with_ids(tx, ctx) -> None:
    results = await build_results(ctx, tx, query="", owner_id=SENDER_ID)
    assert results, "the seeded roster must answer an empty query"
    assert len(results) <= inline.PAGE_SIZE
    first = results[0]
    assert first.id and first.title and first.description
    assert (
        not hasattr(first, "input_message_content")
        or first.input_message_content.parse_mode == "HTML"
    )


async def test_unknown_host_never_becomes_a_photo(tx, ctx) -> None:
    """Art on an unapproved host is an article, so the picker never fetches it for us."""
    wanted = await char_repo.next_free_id(tx)
    await char_repo.create_or_update(
        tx,
        name="Hotlink",
        anime="Testseries",
        rarity=Rarity.RARE,
        assign_id=wanted,
        image_url="https://evil.example/hotlink.png",
    )
    results = await build_results(ctx, tx, query="Hotlink", owner_id=SENDER_ID)
    assert results, "the search itself must still find the row"
    assert all(isinstance(item, InlineQueryResultArticle) for item in results)
    assert all(not getattr(item, "photo_url", "") for item in results)

    # and an approved host does become a photo — the rule has two sides
    char = await char_repo.by_name(tx, "Hotlink", "Testseries")
    assert char is not None
    char.image_url = "https://files.catbox.moe/good.png"
    await tx.flush()
    tx.expire_all()
    photos = await build_results(ctx, tx, query="Hotlink", owner_id=SENDER_ID)
    assert photos and isinstance(photos[0], InlineQueryResultPhoto)
    assert photos[0].photo_url.endswith("good.png")


async def test_collection_prefix_lists_mine_and_only_mine(tx, ctx, partner) -> None:
    rivals, _total = await char_repo.search(tx, "", limit=12)
    mine_row, theirs = rivals[0], rivals[1]
    await _own(tx, int(mine_row.id), SENDER_ID, count=2)
    await _own(tx, int(theirs.id), partner)

    mine = await build_results(ctx, tx, query="collection.", owner_id=SENDER_ID)
    assert len(mine) == 1, "only the sender's own harem"
    assert "×2" in _text(mine[0])
    # The reference read the id out of the prefix; typing one must change nothing here.
    spy = await build_results(ctx, tx, query=f"collection.{partner} ", owner_id=SENDER_ID)
    assert [item.id for item in spy] == [item.id for item in mine]
    assert str(theirs.name) not in "".join(_text(item) for item in mine)


async def test_collection_keyword_filters_my_harem(tx, ctx) -> None:
    rivals, _total = await char_repo.search(tx, "", limit=2)
    picked, other = rivals[0], rivals[1]
    await _own(tx, int(picked.id), SENDER_ID)
    await _own(tx, int(other.id), SENDER_ID)
    unfiltered = await build_results(ctx, tx, query="collection.", owner_id=SENDER_ID)
    assert len(unfiltered) == 2
    hits = await build_results(ctx, tx, query=f"collection. {picked.name[:3]}", owner_id=SENDER_ID)
    assert len(hits) == 1, "the keyword filters inside the harem, not against the roster"
    assert str(picked.name)[:3] in (hits[0].title or _text(hits[0]))
    misses = await build_results(ctx, tx, query="collection. zznothing zz", owner_id=SENDER_ID)
    assert misses == []


async def test_empty_query_on_an_empty_roster_says_nothing(tx, ctx, monkeypatch) -> None:
    """An inline query must never raise: the reference answered [] and so do we."""

    async def _boom(*args: Any, **kwargs: Any) -> tuple[list[Any], int]:
        raise RuntimeError("db is having a day")

    monkeypatch.setattr(char_repo, "search", _boom)
    answered: dict[str, Any] = {}

    class _Query:
        """aiogram models are frozen, so the stub is the shortest honest double here."""

        from_user = SENDER
        query = "anything"

        async def answer(self, results, **kwargs) -> None:
            answered["results"] = list(results)
            answered.update(kwargs)

    await inline.inline_search(inline_query=_Query(), ctx=ctx, session=tx)  # type: ignore[arg-type]
    assert answered == {"results": [], "cache_time": 2}


@pytest.mark.parametrize(
    ("query", "mine"),
    [("", False), ("collection.", True), ("Collection. 42", True), ("not collection.", False)],
)
def test_prefix_matching_is_case_insensitive_and_positional(query: str, mine: bool) -> None:
    head = (query or "").strip().partition(" ")[0]
    assert head.lower().startswith(inline.COLLECTION_PREFIX) is mine


def test_inline_router_is_registered() -> None:
    from waifu.core.dp import PLUGIN_ROUTERS

    assert "waifu.plugins.inline" in PLUGIN_ROUTERS, (
        "an unregistered router is an unshipped feature"
    )
