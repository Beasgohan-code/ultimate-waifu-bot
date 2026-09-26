"""``/market``, ``/buy``, ``/price``, ``/sell`` shortcuts and the item storefront.

The reference bot's ``/market`` had three problems this module fixes:

* it listed every character on one page (hundreds of rows, past Telegram's message
  limit, so the bot truncated it mid-tier);
* its "prices" were the raw catalogue column — no supply signal, so a 1M-coin
  character and a 1M-coin character were equally worth buying;
* the buy path checked ``balance >= price`` and then subtracted in two statements,
  which let two simultaneous purchases both succeed.

Here the storefront is paged per tier, prices come from
:meth:`waifu.db.repo.characters.price_for` (which is where a supply factor
would go — one place), and purchase is one service call inside the update's
transaction.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

from waifu.enums import Rarity
from waifu.errors import Locked, NotEnoughFunds, NotFound, WaifuError
from waifu.plugins._kit import (
    Args,
    RichMessageBuilder,
    callback,
    card,
    cb,
    edit,
    money,
    note,
    pager_row,
    refuse,
    shorten,
    text,
)

if TYPE_CHECKING:  # pragma: no cover
    from waifu.core.access import Access
    from waifu.core.context import AppContext

router = Router(name="market")
PAGE_SIZE = 8


@router.message(Command("market", "store", "shop000000", "cshop000000"))
async def market(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, access: Access
) -> None:
    await send_market(
        message,
        ctx,
        session,
        user_id=access.user_id,
        page=max(0, Args.of(command).paged(1) - 1),
        rarity_id=None,
    )


@router.callback_query(F.data == "mkt:open")
async def market_open(
    callback_query: CallbackQuery, ctx: AppContext, session: Any, access: Access
) -> None:
    await send_market(callback_query, ctx, session, user_id=access.user_id, page=0, rarity_id=None)


async def send_market(
    event: Message | CallbackQuery,
    ctx: AppContext,
    session: Any,
    *,
    user_id: int,
    page: int,
    rarity_id: int | None,
    refresh: bool = False,
) -> None:
    shop = await ctx.items.shop(session, user_id, refresh=refresh)
    hits, total = await ctx.collection.search(
        session, "", rarity_id=rarity_id, limit=PAGE_SIZE, page=page
    )
    builder = RichMessageBuilder().heading("🏪 market", size=1)
    builder.paragraph(
        html=f"<b>{money(total)}</b> characters listed · your balance <b>{money(shop.balance)} 🪙</b>"
    )
    if shop.premium_free:
        # Their premium shop said so out loud ("All items are FREE!") — a waiver the player has to
        # discover by trial is a waiver that gets argued about in the group chat.
        builder.paragraph(html="👑 <b>Premium Mode Active:</b> All items are FREE!")
    if shop.items:
        rows = [["item", "cost", "you have", "what it does"]]
        for entry in shop.items:
            rows.append(
                [
                    entry.name,
                    "Free ✨" if entry.free else f"{money(entry.cost)} 🪙",
                    (
                        f"{entry.owned}/{entry.max_stack}"
                        + (f" · ⌛ {entry.hours_left}h" if entry.hours_left else "")
                    ),
                    shorten(entry.desc, 54),
                ]
            )
        builder.divider()
        builder.heading("consumables", size=3)
        builder.table(rows, compact=True)
    if hits:
        builder.divider()
        builder.heading("characters", size=3)
        rows = [["#", "character", "tier", "price", "own"]]
        for character in hits:
            rarity = Rarity.from_value(int(character.rarity_id))
            owned = await _owned_count(session, user_id, int(character.id))
            rows.append(
                [
                    str(int(character.id)),
                    f"<b>{shorten(character.name, 22)}</b>\n<i>{shorten(character.anime, 20)}</i>",
                    rarity.badge,
                    money(await ctx.collection.price(session, character)),
                    f"×{owned}" if owned else "—",
                ]
            )
        builder.table(rows, compact=True)
    await edit_or_send(
        event,
        ctx,
        builder=builder,
        html=_plain(shop, hits),
        rows=_rows(shop, page=page, pages=max(1, -(-total // PAGE_SIZE)), rarity_id=rarity_id),
    )


async def _owned_count(session: Any, user_id: int, character_id: int) -> int:
    from waifu.db.repo import collection as collection_repo

    return int(await collection_repo.has_count(session, user_id, character_id))


def _plain(shop: Any, hits: list[Any]) -> str:
    items = "\n".join(
        f"• {entry.name} — {'Free ✨' if entry.free else f'{money(entry.cost)}🪙'}"
        for entry in shop.items
    )
    chars = "\n".join(f"{character.id}. {character.name}" for character in hits)
    return f"balance {money(shop.balance)}🪙\n{items}\n{chars}"


def _rows(shop: Any, *, page: int, pages: int, rarity_id: int | None) -> list[list[Any]]:
    extra = f"{rarity_id or 0}/{page}"
    rows: list[list[Any]] = [pager_row(page=page, pages=pages, prefix="mkt", extra=extra)]
    rows.append(
        [
            callback(
                "🔁 refresh "
                + (
                    f"({money(shop.refresh_cost)}🪙)"
                    if shop.refreshes_left <= 0
                    else f"({shop.refreshes_left} free)"
                ),
                cb("mkt", "refresh"),
            ),
        ]
    )
    tiers = [
        callback(
            rarity.emoji,
            cb("mkt", "tier", str(int(rarity)), "0/0"),
            disabled=rarity_id == int(rarity),
        )
        for rarity in list(Rarity)[:9]
    ]
    rows.append(tiers)
    rows.append(
        [
            callback(
                rarity.emoji,
                cb("mkt", "tier", str(int(rarity)), "0/0"),
                disabled=rarity_id == int(rarity),
            )
            for rarity in list(Rarity)[9:]
        ]
    )
    rows.append(
        [callback("🛒 my bag", cb("shop", "open")), callback("🎴 collection", cb("col", "open"))]
    )
    return rows


async def edit_or_send(
    event: Message | CallbackQuery,
    ctx: AppContext,
    *,
    builder: RichMessageBuilder,
    html: str,
    rows: list[list[Any]],
) -> None:
    if isinstance(event, CallbackQuery):
        await edit(event, ctx, builder=builder, html=html, buttons=rows)
    else:
        await card(event, ctx, builder=builder, html=html, buttons=rows)


@router.callback_query(F.data.startswith("mkt:tier:"))
async def market_tier(
    callback_query: CallbackQuery, ctx: AppContext, session: Any, access: Access
) -> None:
    parts = (callback_query.data or "").split(":")
    rarity_id = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
    await send_market(
        callback_query, ctx, session, user_id=access.user_id, page=0, rarity_id=rarity_id or None
    )


@router.callback_query(F.data.regexp(r"^mkt:(first|prev|next|last)(:|$)"))
async def market_page(
    callback_query: CallbackQuery, ctx: AppContext, session: Any, access: Access
) -> None:
    """The pager row emits ``mkt:next:<rarity>/<page>`` (see ``buttons.pager``)."""
    parts = (callback_query.data or "").split(":")
    direction = parts[1] if len(parts) > 1 else "next"
    rarity_raw, page_raw = ([*(parts[2] if len(parts) > 2 else "0/0").split("/"), "0", "0"])[:2]
    page = int(page_raw or 0)
    pages = 1
    if direction == "next":
        page += 1
    elif direction == "prev":
        page = max(0, page - 1)
    await send_market(
        callback_query,
        ctx,
        session,
        user_id=access.user_id,
        page=max(0, page),
        rarity_id=int(rarity_raw) or None,
    )
    del pages


@router.callback_query(F.data == "mkt:refresh")
async def market_refresh(
    callback_query: CallbackQuery, ctx: AppContext, session: Any, access: Access
) -> None:
    try:
        await send_market(
            callback_query,
            ctx,
            session,
            user_id=access.user_id,
            page=0,
            rarity_id=None,
            refresh=True,
        )
    except (NotEnoughFunds, Locked) as exc:
        await note(callback_query, exc.user_message, alert=True)
        return
    await note(callback_query, "shop refreshed")


@router.message(Command("buy"))
async def buy(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, access: Access
) -> None:
    """``/buy <character id>`` — one copy at the listed price (their ``/buy``)."""
    args = Args.of(command)
    character_id = args.integer
    if not character_id:
        await text(
            message,
            ctx,
            "Usage: <code>/buy 42</code> — ids come from /market. For items use <code>/buyitem bomb</code>.",
        )
        return
    try:
        result = await ctx.items.buy_character(session, access.user_id, character_id)
    except (NotEnoughFunds, NotFound, Locked, WaifuError) as exc:
        await refuse(message, exc.user_message)
        return
    await text(
        message,
        ctx,
        f"🛒 bought <b>{result['name']}</b> for <b>{money(result['spent'])} 🪙</b> · balance {money(result['balance'])} 🪙",
    )


@router.callback_query(F.data.startswith("mkt:buychar:"))
async def buy_from_button(
    callback_query: CallbackQuery, ctx: AppContext, session: Any, access: Access
) -> None:
    character_id = int((callback_query.data or "").split(":")[-1] or 0)
    try:
        result = await ctx.items.buy_character(session, access.user_id, character_id)
    except (NotEnoughFunds, NotFound, Locked, WaifuError) as exc:
        await note(callback_query, exc.user_message, alert=True)
        return
    await note(callback_query, f"bought {result['name']} for {money(result['spent'])} 🪙")
    await send_market(callback_query, ctx, session, user_id=access.user_id, page=0, rarity_id=None)


@router.message(Command("buyitem", "item"))
async def buyitem(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, access: Access
) -> None:
    args = Args.of(command)
    if not args.first:
        await text(message, ctx, f"Which item? {', '.join(spec.key for spec in _item_defs())}")
        return
    key = args.first.lower()
    try:
        result = await ctx.items.buy(session, access.user_id, key, quantity=args.count)
    except (NotEnoughFunds, Locked, NotFound, WaifuError) as exc:
        await refuse(message, exc.user_message)
        return
    await text(
        message,
        ctx,
        f"🛒 <b>{result['name']}</b> ×{result['quantity']} for <b>{money(result['cost'])} 🪙</b> · bag {result['owned']} · balance {money(result['balance'])} 🪙",
    )


def _item_defs() -> list[Any]:
    from waifu.db.repo import items as items_repo

    return list(items_repo.ITEMS.values())


@router.message(Command("price", "worth"))
async def price(message: Message, ctx: AppContext, session: Any, command: CommandObject) -> None:
    args = Args.of(command)
    if not args.raw:
        await text(message, ctx, "Price of what? <code>/price gojo</code>")
        return
    try:
        character = await ctx.collection.find(session, args.raw)
    except NotFound:
        await text(message, ctx, f"Nothing matches “{shorten(args.raw, 40)}”.")
        return
    rarity = Rarity.from_value(int(character.rarity_id))
    listed = await ctx.collection.price(session, character)
    spare = ctx.gacha.dupe_payout(character)
    await text(
        message,
        ctx,
        (
            f"💵 <b>{character.name}</b> {rarity.badge}\n"
            f"buy at market: <b>{money(listed)} 🪙</b>\n"
            f"selling a spare pays: <b>{money(spare)} 🪙</b> ({ctx.settings.dupe_payout_percent}% of value)\n"
            f"base tier price: {money(rarity.base_price)} 🪙"
        ),
    )
