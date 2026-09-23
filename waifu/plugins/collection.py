"""``/collection``, ``/harem``, ``/check``, ``/fav``, ``/lock``, ``/sell``, ``/search``.

This is the page players spend most of their time on, so it reproduces the reference
bot's layout — a rarity-grouped list with a ladder header and a numbered pager — and
fixes the three things that made that page painful there:

* **state**: it kept the current page in a dict keyed by user, so two open pages in two
  chats fought each other and a restart reset everyone to page 1. Here the page number
  lives in the callback data (stateless, survives restarts, works on every worker).
* **sorting**: "by rarity" and "by newest" were separate queries with different
  pagination maths; they share one service call here (``CollectionService.page``).
* **filtering**: a text search re-fetched the whole collection per keystroke; the
  service paginates in SQL with an index.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message

from waifu.enums import Rarity
from waifu.errors import WaifuError
from waifu.plugins._kit import (
    Args,
    RichMessageBuilder,
    bar,
    callback,
    card,
    cb,
    edit,
    mention,
    money,
    note,
    page_of,
    pager_row,
    refuse,
    resolve_user,
    shorten,
    text,
)

if TYPE_CHECKING:  # pragma: no cover
    from waifu.core.access import Access
    from waifu.core.context import AppContext
    from waifu.db.repositories import collection as collection_repo
    from waifu.services.collection import CollectionPage

router = Router(name="collection")

MODES = [
    ("rarity", "🏆 Rarity"),
    ("recent", "🕒 Recent"),
    ("power", "⚔️ Power"),
    ("dupes", "📚 Dupes"),
    ("favourites", "⭐ Fav"),
]
PAGE_SIZE = 8


def entry_args(args: Args, *, default_mode: str) -> tuple[int, str, str]:
    """Parse ``/collection`` arguments the way the reference bot's users learned them.

    Summon-bot accepted <b>a page number</b> and <b>a series name</b> in the same slot
    (``/collection 3`` vs ``/collection Naruto``, the latter forcing the series view), and
    its admins wrote both into the group rules. So: digits are the page, a mode word is the
    mode, and anything else becomes the filter — with the anime grouping implied by a
    series filter exactly as their ``anime_filter`` did.
    """
    page = 0
    mode = ""
    words: list[str] = []
    for word in args.words:
        if word.isdigit():
            page = max(0, int(word) - 1)
        elif word.lower() in {key for key, _ in MODES}:
            mode = word.lower()
        else:
            words.append(word)
    query = " ".join(words).strip()
    if query and not mode:
        mode = "anime"
    return page, (mode or default_mode), query


# ------------------------------------------------------------------- entry points
@router.message(Command("collection", "col", "mywaifus", "waifus", "harem2"))
async def collection(
    message: Message,
    ctx: AppContext,
    session: Any,
    command: CommandObject,
    user: Any,
    access: Access,
) -> None:
    args = Args.of(command)
    if not user:
        await text(message, ctx, "Send /start once and this works forever after.")
        return
    page, mode, query = entry_args(args, default_mode="rarity")
    await send_page(
        message,
        ctx,
        session,
        owner=user.id,
        viewer_id=access.user_id,
        page=page,
        mode=mode,
        query=query,
    )


@router.message(Command("harem"))
async def harem(
    message: Message,
    ctx: AppContext,
    session: Any,
    command: CommandObject,
    user: Any,
    access: Access,
) -> None:
    """``/harem`` is their power-sorted collection page — same renderer, one default."""
    args = Args.of(command)
    if not user:
        return
    page, _mode, query = entry_args(args, default_mode="power")
    await send_page(
        message,
        ctx,
        session,
        owner=user.id,
        viewer_id=access.user_id,
        page=page,
        mode="power",
        query=query,
    )


@router.message(Command("check", "view", "peek"))
async def check(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, access: Access
) -> None:
    args = Args.of(command)
    # A bare number is a *page* (their habit); it is still worth resolving as a player,
    # because "/check 123456789" is how someone pastes a user id.
    target = await resolve_user(session, message, args.first)
    if target is None:
        # ``/check 3`` used to mean "page 3 of my own collection" upstream; keep that.
        if args.first.isdigit() and access is not None:
            await send_page(
                message,
                ctx,
                session,
                owner=access.user_id,
                viewer_id=access.user_id,
                page=max(0, int(args.first) - 1),
                mode="rarity",
            )
        else:
            await text(message, ctx, "Who? Reply to someone, or use their @username.")
        return
    await send_page(
        message,
        ctx,
        session,
        owner=target,
        viewer_id=access.user_id if access else 0,
        page=0,
        mode="rarity",
    )


def user_pref(args: Args, default: str) -> str:
    """``/collection 3 power`` — an explicit mode argument beats the saved preference."""
    for word in args.words:
        if word.lower() in {key for key, _ in MODES}:
            return word.lower()
    return default


# ----------------------------------------------------------------------- rendering
async def send_page(
    event: Message | CallbackQuery,
    ctx: AppContext,
    session: Any,
    *,
    owner: int,
    viewer_id: int,
    page: int,
    mode: str,
    query: str = "",
    rarity_id: int | None = None,
) -> None:
    data: CollectionPage = await ctx.collection.page(
        session,
        owner,
        mode=mode,
        rarity_id=rarity_id,
        query=query,
        dupes_only=mode == "dupes",
        page=page,
        page_size=PAGE_SIZE,
    )
    roster = await _roster_size(session)
    builder = _page_builder(
        ctx,
        data,
        owner=owner,
        viewer_id=viewer_id,
        mode=mode,
        query=query,
        rarity_id=rarity_id,
        roster=roster,
    )
    rows = _page_rows(
        data, mode=mode, query=query, rarity_id=rarity_id, self_view=viewer_id == owner
    )
    html = _page_plain(ctx, data, owner=owner, mode=mode, roster=roster)
    if isinstance(event, CallbackQuery):
        await edit(event, ctx, builder=builder, html=html, buttons=rows)
    else:
        await card(event, ctx, builder=builder, html=html, buttons=rows)


async def _roster_size(session: Any) -> int:
    from waifu.db.repositories import characters as char_repo

    totals = await char_repo.totals(session)
    return int(totals.get("characters", 0) or 0)


def _page_builder(
    ctx: AppContext,
    data: CollectionPage,
    *,
    owner: int,
    viewer_id: int,
    mode: str,
    query: str,
    rarity_id: int | None,
    roster: int,
) -> RichMessageBuilder:
    from waifu.db.repositories import users as user_repo  # local: keeps import cost off startup

    del user_repo  # (name resolution happens in _page_plain's caller via mention())
    ladder = ladder_line(data.per_rarity)
    builder = RichMessageBuilder()
    title = "My collection" if viewer_id == owner else f"{mention(owner)}'s collection"
    builder.heading(title, size=1)
    if query:
        builder.paragraph(
            html=f"🔎 matches for <b>{query}</b>"
            + (f" in {Rarity.from_value(rarity_id).badge}" if rarity_id else "")
        )
    if mode == "dupes":
        builder.paragraph(
            html="📚 spares only — everything here can be sold without losing the character"
        )
    builder.line(ladder)
    owned = sum(data.per_rarity.values())
    completion = (owned / roster * 100) if roster else 0.0
    builder.table(
        [
            ["characters", f"{money(owned)} / {money(roster)}  ({completion:.1f}%)"],
            ["value", f"{money(data.value)} 🪙"],
            ["this page", f"page {data.page + 1} of {max(1, data.pages)}"],
        ],
        compact=True,
        bordered=False,
    )
    builder.line(bar(owned, roster, width=12))
    if data.items:
        rows = [["#", "character", "rarity", "×", "value"]]
        for index, entry in enumerate(data.items, start=1):
            number = f"{'★' if entry.is_favorite else ''}{'🔒' if entry.is_locked else ''}{index}"
            rows.append(
                [
                    number,
                    f"<b>{shorten(entry.name, 26)}</b>"
                    + (f"\n<i>{shorten(entry.anime, 24)}</i>" if entry.anime else ""),
                    entry.rarity if isinstance(entry.rarity, str) else str(entry.rarity),
                    f"×{entry.count}" if entry.count > 1 else "1",
                    money(entry.price),
                ]
            )
        builder.table(rows, compact=True)
    else:
        builder.paragraph(
            html="<i>Nothing here yet — /pull, /hclaim, or wait for a spawn in this group.</i>"
        )
    return builder


def ladder_line(per_rarity: dict[int, int]) -> str:
    """``⚪️12/18 🔵4/9 …`` — the one-glance summary their page had and mine keeps."""
    parts: list[str] = []
    for rarity in Rarity:
        owned = int(per_rarity.get(int(rarity), 0))
        if owned:
            parts.append(f"{rarity.emoji}{money(owned)}")
    return " ".join(parts) if parts else "no characters yet"


def _page_plain(
    ctx: AppContext, data: CollectionPage, *, owner: int, mode: str, roster: int
) -> str:
    """The HTML fallback (old API servers get this instead of a rich card)."""
    lines = [f"<b>{mention(owner)} — collection</b>", ladder_line(data.per_rarity)]
    for index, entry in enumerate(data.items, start=1):
        star = "⭐" if entry.is_favorite else "  "
        lines.append(
            f"{star}{index}. {entry.name} ({entry.anime or '—'}) ×{entry.count} — {money(entry.price)}🪙"
        )
    lines.append(
        f"<i>page {data.page + 1}/{max(1, data.pages)} · value {money(data.value)}🪙 · mode {mode}</i>"
    )
    return "\n".join(lines)


def _page_rows(
    data: CollectionPage, *, mode: str, query: str, rarity_id: int | None, self_view: bool
) -> list[list[InlineKeyboardButton]]:
    extra = _pager_extra(mode=mode, query=query, rarity_id=rarity_id)
    rows: list[list[InlineKeyboardButton]] = [
        pager_row(page=data.page, pages=max(1, data.pages), prefix="col", extra=extra)
    ]
    if self_view:
        rows.append(
            [
                callback("🏷️ " + label, cb("col", "mode", key, extra), disabled=key == mode)
                for key, label in MODES
            ]
        )
        rows.append(
            [
                callback("🔎 search", cb("col", "ask", "search", extra)),
                callback("⭐ fav this page", cb("col", "favpage", str(data.page), extra)),
                callback("💰 sell dupes", cb("col", "sellall", extra)),
            ]
        )
    else:
        rows.append(
            [
                callback(
                    "📊 their stats",
                    cb("col", "stats", str(data.total)),
                ),
                callback(
                    "🎴 gift",
                    cb("gift", "open", str(int(data.items[0].character_id) if data.items else 0)),
                ),
            ]
        )
    # Tier filter row: 18 tiers do not fit in one line, so pages of 6 with the active one first.
    tiers = [
        callback(
            rarity.emoji,
            cb("col", "tier", str(int(rarity)), extra),
            disabled=rarity_id == int(rarity),
        )
        for rarity in list(Rarity)[:9]
    ]
    rows.append(tiers)
    rest = [
        callback(
            rarity.emoji,
            cb("col", "tier", str(int(rarity)), extra),
            disabled=rarity_id == int(rarity),
        )
        for rarity in list(Rarity)[9:]
    ]
    if rarity_id:
        rows.append(
            [
                callback(
                    f"✖ clear tier ({Rarity.from_value(rarity_id).badge})",
                    cb("col", "tier", "0", extra),
                ),
                *rest[:6],
            ]
        )
    return rows


def _pager_extra(*, mode: str, query: str, rarity_id: int | None) -> str:
    """Pager callbacks must carry the view state — Telegram gives us 64 bytes, so keys
    are short and the query is truncated rather than stored in FSM."""
    bits = [mode or "rarity", str(int(rarity_id or 0)), (query or "").replace(":", " ")[:14]]
    return "/".join(bits)


def _parse_extra(extra: str) -> tuple[str, int | None, str]:
    mode, raw_rarity, query = ([*list(extra.split("/")), "", ""])[:3]
    return (
        mode or "rarity",
        int(raw_rarity) if raw_rarity.isdigit() and int(raw_rarity) else None,
        query or "",
    )


# ------------------------------------------------------------------------ actions
@router.callback_query(F.data.regexp(r"^col:(first|prev|next|last):"))
async def col_page(
    callback_query: CallbackQuery, ctx: AppContext, session: Any, access: Access
) -> None:
    """Pager buttons: ``col:next:<mode>/<rarity>/<query>``.

    The view state rides in the callback data instead of FSM, so two players paging at
    once cannot move each other's page and a restart costs nobody their place.
    """
    parts = (callback_query.data or "").split(":", 2)
    direction = parts[1]
    extra = parts[2] if len(parts) > 2 else ""
    data: CollectionPage = await ctx.collection.page(
        session, access.user_id, page=0, page_size=PAGE_SIZE
    )
    current = data.page
    mode, rarity_id, query = _parse_extra(extra) if extra else ("rarity", None, "")
    target = await page_of(callback_query, max(1, data.pages), direction, current)
    await send_page(
        callback_query,
        ctx,
        session,
        owner=access.user_id,
        viewer_id=access.user_id,
        page=target,
        mode=mode,
        query=query,
        rarity_id=rarity_id,
    )


@router.callback_query(F.data.startswith("col:mode:"))
async def col_mode(
    callback_query: CallbackQuery, ctx: AppContext, session: Any, access: Access
) -> None:
    parts = (callback_query.data or "").split(":")
    mode = parts[2] if len(parts) > 2 else "rarity"
    extra = ":".join(parts[3:])
    _, rarity_id, query = _parse_extra(extra) if extra else ("rarity", None, "")
    from waifu.db.repositories import users as user_repo

    await user_repo.set_pref(session, access.user_id, hmode=mode)
    await send_page(
        callback_query,
        ctx,
        session,
        owner=access.user_id,
        viewer_id=access.user_id,
        page=0,
        mode=mode,
        query=query,
        rarity_id=rarity_id,
    )
    await note(callback_query, f"view: {mode}")


@router.callback_query(F.data.startswith("col:tier:"))
async def col_tier(
    callback_query: CallbackQuery, ctx: AppContext, session: Any, access: Access
) -> None:
    parts = (callback_query.data or "").split(":")
    rarity = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
    extra = ":".join(parts[3:])
    mode, _rarity, query = _parse_extra(extra) if extra else ("rarity", None, "")
    await send_page(
        callback_query,
        ctx,
        session,
        owner=access.user_id,
        viewer_id=access.user_id,
        page=0,
        mode=mode,
        query=query,
        rarity_id=rarity or None,
    )


@router.callback_query(F.data == "col:open")
async def col_open(
    callback_query: CallbackQuery, ctx: AppContext, session: Any, access: Access
) -> None:
    await send_page(
        callback_query,
        ctx,
        session,
        owner=access.user_id,
        viewer_id=access.user_id,
        page=0,
        mode="rarity",
    )


@router.callback_query(F.data.startswith("col:fav:"))
async def col_fav(
    callback_query: CallbackQuery, ctx: AppContext, session: Any, access: Access
) -> None:
    """``⭐`` on a page row: index → character id, resolved from the same page."""
    parts = (callback_query.data or "").split(":")
    page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
    index = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
    if not index:
        await note(callback_query, "tap a numbered button", alert=True)
        return
    data: CollectionPage = await ctx.collection.page(
        session, access.user_id, page=page, page_size=PAGE_SIZE
    )
    if index > len(data.items):
        await note(callback_query, "that page changed — try again", alert=True)
        return
    entry = data.items[index - 1]
    message = await ctx.collection.set_favourite(
        session, access.user_id, entry.character_id, value=not entry.is_favorite
    )
    await send_page(
        callback_query,
        ctx,
        session,
        owner=access.user_id,
        viewer_id=access.user_id,
        page=page,
        mode=data.mode,
    )
    await note(callback_query, message)


@router.callback_query(F.data.startswith("col:favpage:"))
async def col_fav_page(
    callback_query: CallbackQuery, ctx: AppContext, session: Any, access: Access
) -> None:
    parts = (callback_query.data or "").split(":")
    page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
    extra = ":".join(parts[3:])
    mode, rarity_id, query = _parse_extra(extra) if extra else ("rarity", None, "")
    data: CollectionPage = await ctx.collection.page(
        session,
        access.user_id,
        mode=mode,
        rarity_id=rarity_id,
        query=query,
        page=page,
        page_size=PAGE_SIZE,
    )
    toggled = 0
    for entry in data.items:
        if not entry.is_favorite:
            await ctx.collection.set_favourite(
                session, access.user_id, entry.character_id, value=True
            )
            toggled += 1
    await send_page(
        callback_query,
        ctx,
        session,
        owner=access.user_id,
        viewer_id=access.user_id,
        page=page,
        mode=mode,
        query=query,
        rarity_id=rarity_id,
    )
    await note(callback_query, f"starred {toggled}")


@router.callback_query(F.data.startswith("col:sellall"))
async def col_sell_dupes(
    callback_query: CallbackQuery, ctx: AppContext, session: Any, access: Access
) -> None:
    """Sell every spare in one tap — the single most requested feature on the old bot."""
    sellable = await ctx.collection.sellable(session, access.user_id, limit=200)
    if not sellable:
        await note(callback_query, "no dupes to sell", alert=True)
        return
    total = 0
    sold = 0
    for entry in sellable:
        spare = max(0, int(entry.count) - 1)
        if spare <= 0:
            continue
        result = await ctx.collection.sell(session, access.user_id, entry.character_id, count=spare)
        total += int(result.payout)
        sold += int(result.count_sold)
    await note(callback_query, f"sold {sold} spares for {money(total)} 🪙", alert=sold == 0)
    await send_page(
        callback_query,
        ctx,
        session,
        owner=access.user_id,
        viewer_id=access.user_id,
        page=0,
        mode="dupes",
    )


@router.message(Command("sell", "sellchar"))
async def sell(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, user: Any
) -> None:
    """/sell <id|name> [×n] — by collection-page number, id, or name."""
    args = Args.of(command)
    if not args.raw or not user:
        await text(
            message,
            ctx,
            "Usage: <code>/sell 3</code> (page position), <code>/sell 12x2</code> (by id) or <code>/sell gojo</code>.",
        )
        return
    character_id = await _resolve_character(session, ctx, user.id, args)
    try:
        result = await ctx.collection.sell(session, user.id, character_id, count=args.count)
    except WaifuError as exc:
        await refuse(message, exc.user_message)
        return
    await text(
        message,
        ctx,
        f"💵 sold <b>{result.count_sold}× {result.name}</b> for <b>{money(result.payout)} 🪙</b> — balance {money(result.balance)} 🪙, {result.remaining} left.",
    )


async def _resolve_character(session: Any, ctx: AppContext, user_id: int, args: Args) -> int:
    """Position on the current page → id → fuzzy name, in that order (their ``/sell`` only did ids)."""
    token = args.first
    if token.isdigit() and len(token) <= 3:
        index = int(token)
        if index <= 20:  # a page position, not a character id
            data: CollectionPage = await ctx.collection.page(
                session, user_id, page=0, page_size=PAGE_SIZE
            )
            if 1 <= index <= len(data.items):
                return int(data.items[index - 1].character_id)
    if token.isdigit():
        return int(token)
    found = await ctx.collection.find(session, args.raw.split("×")[0].split("x")[0].strip())
    return int(found.id)


@router.message(Command("fav", "favorite", "favourite"))
async def fav(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, user: Any
) -> None:
    args = Args.of(command)
    if not user:
        return
    if not args.raw:
        current = await ctx.collection.favourite(session, user.id)
        await text(
            message,
            ctx,
            (
                f"Your featured character is <b>{current.name}</b>. /fav &lt;name&gt; to change it."
                if current
                else "No favourite set — <code>/fav &lt;name or #id&gt;</code>."
            ),
        )
        return
    character_id = await _resolve_character(session, ctx, user.id, args)
    message_text = await ctx.collection.set_favourite(session, user.id, character_id)
    await text(message, ctx, message_text)


@router.message(Command("lock", "unlock"))
async def lock(
    message: Message, ctx: AppContext, command: CommandObject, user: Any, session: Any
) -> None:
    args = Args.of(command)
    if not user or not args.raw:
        await text(
            message,
            ctx,
            "Usage: <code>/lock &lt;name or #id&gt;</code> — a locked copy can't be sold, gifted or traded.",
        )
        return
    command_name = (message.text or "/lock").split()[0].lstrip("/").split("@")[0]
    character_id = await _resolve_character(session, ctx, user.id, args)
    result = await ctx.collection.set_lock(
        session, user.id, character_id, value=command_name == "lock"
    )
    await text(message, ctx, result)


@router.message(Command("search", "find"))
async def search(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, access: Access
) -> None:
    args = Args.of(command)
    if not args.raw:
        await text(
            message,
            ctx,
            "Search the roster: <code>/search mikasa</code>, <code>/search naruto --anime</code>, or use 🔎 on your collection page.",
        )
        return
    hits, total = await ctx.collection.search(session, args.raw, limit=8, page=0)
    if not hits:
        await text(message, ctx, f"No character matches “{args.raw}”.")
        return
    builder = RichMessageBuilder().heading(f"🔎 {money(total)} matches", size=2)
    rows = [["character", "rarity", "price", "in your harem"]]
    from waifu.db.repositories import collection as collection_repo

    owned: dict[int, collection_repo.Owned] = {}
    for character in hits:
        row = await collection_repo.owned_row(session, access.user_id, int(character.id))
        if row is not None:
            owned[int(character.id)] = row
        rarity = Rarity.from_value(int(character.rarity_id))
        rows.append(
            [
                f"<b>{shorten(character.name, 24)}</b>\n<i>{shorten(character.anime, 22)}</i>",
                f"{rarity.badge}",
                money(await ctx.collection.price(session, character)),
                f"×{owned[int(character.id)].count}" if int(character.id) in owned else "—",
            ]
        )
    builder.table(rows, compact=True)
    await card(
        message, ctx, builder=builder, html="\n".join(f"{hit.name} — {hit.anime}" for hit in hits)
    )


@router.message(Command("mystats", "cstats", "pstats"))
async def stats(message: Message, ctx: AppContext, session: Any, user: Any, access: Access) -> None:
    if not user:
        return
    await send_stats(message, ctx, session, target=access.user_id)


async def send_stats(
    event: Message | CallbackQuery, ctx: AppContext, session: Any, *, target: int
) -> None:
    profile = await ctx.collection.profile(session, target)
    builder = RichMessageBuilder().heading(f"📊 {mention(target)}", size=1)
    rows = [
        [key, money(value) if isinstance(value, int) else str(value)]
        for key, value in profile.items()
        if value not in (None, "", {}, [])
    ]
    builder.table(rows, compact=True, bordered=False)
    await (
        edit(event, ctx, builder=builder, html=_flat(profile))
        if isinstance(event, CallbackQuery)
        else card(event, ctx, builder=builder, html=_flat(profile))
    )


def _flat(profile: dict[str, Any]) -> str:
    return "\n".join(f"<b>{key}:</b> {value}" for key, value in profile.items())


@router.callback_query(F.data.startswith("col:stats:"))
async def col_stats(
    callback_query: CallbackQuery, ctx: AppContext, session: Any, access: Access
) -> None:
    await send_stats(callback_query, ctx, session, target=access.user_id)


@router.callback_query(F.data.startswith("col:ask:"))
async def col_ask(callback_query: CallbackQuery, ctx: AppContext) -> None:
    await note(
        callback_query,
        "type /search <text> in the chat — Telegram gives inline search only to bots in a topic.",
        alert=True,
    )


@router.callback_query(F.data.startswith("col:noop"))
async def col_noop(callback_query: CallbackQuery) -> None:
    await callback_query.answer(cache_time=3600)
