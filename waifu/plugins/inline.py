"""Inline mode — ``@bot <query>`` in any chat, without the bot being a member of it.

The reference bot grew this as a workaround: its characters were URLs on Catbox, so a
player could paste a card into a group by picking it from the inline list
(``inline_search.py``), and ``collection_inline`` (an alias of the same handler in the
source, a real one in the deployment's bytecode) let a player paste *their own* harem into
anywhere. It is still the only way to put a character into a chat the bot has never seen,
which is why it is ported rather than invented over:

* the query grammar is the reference's — ``collection.<id> [keyword]`` lists a harem, anything
  else searches the roster by name or series, 25 results, ``cache_time=2``;
* a result with an approved image URL becomes an ``InlineQueryResultPhoto`` (the picker shows
  the art), anything else an article with the same caption; nothing is fetched from a host the
  bot does not allow (``waifu/tg/media.py``);
* the caption is the reference's: bold name, ``×count`` when it is a dupe, then
  ``🆔 id / 🎌 series / {rarity emoji} {rarity}`` in a quote block.

One thing is *not* ported, on purpose. ``collection.42`` in the reference means "show me
user 42's collection", so any Telegram user could enumerate any other user's harem — which is
player data, not catalogue data. Here the owner is always the person typing, and the number in
the prefix is parsed and ignored, so an old macro still works instead of leaking.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

from aiogram import Router
from aiogram.types import (
    InlineQuery,
    InlineQueryResultArticle,
    InlineQueryResultPhoto,
    InputTextMessageContent,
)

from waifu.db.repositories import characters as char_repo
from waifu.enums import Rarity
from waifu.logging import get_logger
from waifu.tg.media import is_allowed_image_url

if TYPE_CHECKING:  # pragma: no cover
    from waifu.core.context import AppContext

log = get_logger("plugins.inline")
router = Router(name="inline")

#: Results per query — the reference's ``LIMIT 25``; Telegram allows 50, and 25 is what a
#: thumb-through of a phone screen actually is.
PAGE_SIZE = 25
#: ``collection.`` in the reference's grammar. Kept as the trigger word, dropped as a target.
COLLECTION_PREFIX = "collection."


def rarity_line(rarity: object) -> str:
    """The rarity as one decorated string — no doubled emoji, whichever way it is spelled.

    The reference stored bare names (``Legendary``) and prefixed the emoji at display time;
    this bot stores ``Rarity.display`` (``⭐ Legendary``) so cards and DB agree. Both arrive
    here — legacy rows through ``import-legacy``, new ones from ``/upload`` — and both must
    read the same in the picker.
    """
    label = str(rarity or "").strip()
    if not label:
        return "—"
    if not label[0].isascii():  # already decorated with its badge emoji
        return label
    return f"{rarity_emoji(label)} {label}"


def rarity_emoji(rarity: object) -> str:
    """The emoji a rarity string deserves — the reference's lookup, on our enum.

    ``inline_search.py`` carried its own ``label → id`` map (only 15 of the 18 tiers were
    listed, so two tiers rendered with the fallback star). ``Rarity.from_label`` knows all
    18 and is the same resolver the rest of the bot uses, so an inline caption and a card
    can never disagree about what ``Luxury`` looks like.
    """
    return Rarity.from_label(str(rarity or "")).emoji.strip() or "⭐"


def caption_for(
    name: str, char_id: object, anime: str, rarity: object, *, count: int | None = None
) -> str:
    """``<b>Name ×2</b>`` + the quote block, exactly as the reference formatted it.

    Names come out of the admin's own typing, so they are escaped: an unescaped ``<`` made
    Telegram reject the message and the picker show nothing.
    """
    from waifu.utils.text import esc

    title = f"<b>{esc(name)}</b>" + (f" ×{count}" if count and count > 1 else "")
    return (
        f"{title}\n<blockquote>🆔 <code>{char_id}</code>\n"
        f"🎌 {esc(anime) or '—'}\n{esc(rarity_line(rarity))}</blockquote>"
    )


def result_id(owner_id: int | None, char_id: object, count: int | None = None) -> str:
    """Stable 32-char result id (the reference hashed ``owner:id:count`` into sha256).

    Stability matters more than it looks: Telegram uses the id to decide whether a result is
    the same one the user already saw, so a per-query random id makes the list flicker while
    the user types.
    """
    digest = hashlib.sha256(f"{owner_id or 'catalog'}:{char_id}:{count or 0}".encode())
    return digest.hexdigest()[:32]


def _title(name: str, rarity: object) -> str:
    from waifu.utils.text import esc

    return f"{esc(str(rarity or ''))} — {esc(name)}"


def _hosts(ctx: AppContext) -> list[str]:
    return list(ctx.settings.allowed_media_hosts or [])


async def build_results(
    ctx: AppContext, session: Any, *, query: str, owner_id: int
) -> list[InlineQueryResultArticle | InlineQueryResultPhoto]:
    """The picker's rows: a harem for ``collection.`` queries, the roster for everything else."""
    raw = (query or "").strip()
    mine = False
    if raw.lower().startswith(COLLECTION_PREFIX):
        mine = True
        _head, _, keyword = raw.partition(" ")
        # head is ``collection.<id>`` in the reference; the id is dropped — see the module
        # docstring. Whatever follows is the search term.
        raw = keyword.strip()
    results: list[InlineQueryResultArticle | InlineQueryResultPhoto] = []
    if mine:
        page = await ctx.collection.page(session, owner_id, page=0, page_size=PAGE_SIZE, query=raw)
        for entry in page.items:
            results.append(
                _result(
                    ctx,
                    char_id=int(entry.character_id),
                    name=entry.name,
                    anime=entry.anime,
                    rarity=entry.rarity or Rarity.from_value(entry.rarity_id).display,
                    image_url=str(entry.image or ""),
                    count=int(entry.count),
                    owner_id=owner_id,
                )
            )
        return results
    # ``search`` treats an empty query as "no filter" (the reference's empty query listed
    # the first 25 by name), and it adds the ``%`` wildcards itself.
    found, _total = await char_repo.search(session, raw, limit=PAGE_SIZE)
    for character in found:
        results.append(
            _result(
                ctx,
                char_id=int(character.id),
                name=character.name,
                anime=character.anime,
                rarity=character.rarity,
                image_url=str(character.image_url or ""),
                owner_id=None,
            )
        )
    return results


def _result(
    ctx: AppContext,
    *,
    char_id: object,
    name: str,
    anime: str,
    rarity: object,
    image_url: str,
    owner_id: int | None,
    count: int | None = None,
) -> InlineQueryResultArticle | InlineQueryResultPhoto:
    """A photo when the art is on a host we trust, an article otherwise (the reference's rule)."""
    text = caption_for(name, char_id, anime, rarity, count=count)
    identifier = result_id(owner_id, char_id, count)
    if image_url and is_allowed_image_url(image_url, hosts=_hosts(ctx)):
        return InlineQueryResultPhoto(
            id=identifier,
            photo_url=image_url,
            thumbnail_url=image_url,
            title=_title(name, rarity),
            description=anime or "—",
            caption=text,
        )
    return InlineQueryResultArticle(
        id=identifier,
        title=_title(name, rarity),
        description=anime or "—",
        input_message_content=InputTextMessageContent(
            message_text=text, parse_mode="HTML", disable_web_page_preview=True
        ),
    )


@router.inline_query()
async def inline_search(inline_query: InlineQuery, ctx: AppContext, session: Any = None) -> None:
    """``@bot <query>`` — roster search; ``@bot collection. [query]`` — my own harem.

    Never raises: the reference wrapped the whole thing in a try/except that answered with an
    empty list, and that is right here — a broken inline query should show "no results" in the
    picker, not an error toast in the middle of typing in someone else's chat.
    """
    user = inline_query.from_user
    if user is None:  # pragma: no cover - Telegram always fills this
        await inline_query.answer([], cache_time=2)
        return
    results: list[InlineQueryResultArticle | InlineQueryResultPhoto] = []
    try:
        if session is None:  # pragma: no cover - the middleware always supplies one
            async with ctx.db.session() as own:
                results = await build_results(ctx, own, query=inline_query.query, owner_id=user.id)
        else:
            results = await build_results(ctx, session, query=inline_query.query, owner_id=user.id)
    except Exception as exc:  # an empty picker beats an error toast in someone else's chat
        log.warning("inline query failed for %s: %s", user.id, exc)
    await inline_query.answer(results, cache_time=2)


__all__ = [
    "build_results",
    "caption_for",
    "rarity_emoji",
    "rarity_line",
    "result_id",
    "router",
]
