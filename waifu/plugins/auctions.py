"""``/auction``, ``/bid``, ``/auctions``, ``/myauctions``, ``/cancelauction``.

The reference bot's auction was a pinned message plus a dict of bids in the database, and
settlement ran from a timer that could fire *while* a bid was being written — the loser
kept their coins and the winner got nothing. Here the money movement is the same
escrow the trade flow uses:

* a bid debits the stake up front (idempotency key per auction/bidder/amount, so a
  double-tap cannot bid twice);
* the previous leader is refunded inside the same transaction, keyed by the bid row;
* ``settle`` (scheduler-owned, one transaction per auction) moves the character and the
  coins together, and the row's ``status`` is what makes it idempotent.

The snipe window and extensions are in :class:`~waifu.services.auction.AuctionService`;
this module only draws them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

from waifu.enums import Rarity
from waifu.errors import BidTooLow, Locked, NotEnoughFunds, NotFound, PermissionDenied, WaifuError
from waifu.plugins._kit import (
    Args,
    RichMessageBuilder,
    callback,
    card,
    cb,
    edit,
    mention,
    money,
    note,
    pager_row,
    refuse,
    shorten,
    split_cb,
    text,
)
from waifu.services.auction import fmt_bid
from waifu.utils.chats import is_group
from waifu.utils.time import human_delta

if TYPE_CHECKING:  # pragma: no cover
    from waifu.core.access import Access
    from waifu.core.context import AppContext

router = Router(name="auctions")

#: ``auction_bid_input`` in the reference: one pending custom bid per user, keyed by user id.
#: In the cache rather than a table, so it expires by itself instead of leaving rows behind for a
#: cleanup job that the reference never wrote.
CUSTOM_NS = "aucbid"
#: Debounce for the pinned live edit (see :func:`do_bid`).
REFRESH_NS = "aucrefresh"


@router.message(Command("auction", "sell_auction", "newauction"))
async def auction(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, access: Access
) -> None:
    """/auction <character> <start> [minutes] [reserve=…] — list one of your copies."""
    args = Args.of(command)
    if not args.raw:
        await send_live(message, ctx, session, page=0, actor=access.user_id)
        return
    tokens = args.raw.split()
    character_id = await _character_id(session, ctx, access.user_id, args)
    if character_id is None:
        await text(
            message,
            ctx,
            "❌ <b>Usage:</b> <code>/auction &lt;char_id&gt; &lt;start_price&gt; [minutes]</code>\n"
            "Example: <code>/auction 05 5000 30</code> · list it in a group to pin it for everyone.",
        )
        return
    numbers = [
        int(token.replace("_", "")) for token in tokens[1:] if token.replace("_", "").isdigit()
    ]
    start = numbers[0] if numbers else 0
    # A bare second number is minutes, not a second price — that is how the old bot's
    # players used it, and guessing wrong here costs somebody a listing. The service clamps to
    # ``auction_min_minutes``/``auction_max_minutes`` (the reference's 5-180 window) and refuses an
    # opening bid under ``auction_min_start_price``, which is its own "Min starting bid: 100" rule.
    minutes = int(numbers[1]) if len(numbers) > 1 else int(ctx.settings.auction_default_minutes)
    if start < int(ctx.settings.auction_min_start_price):
        await text(
            message,
            ctx,
            f"❌ Min starting bid: <b>{ctx.settings.auction_min_start_price}</b> 🪙",
        )
        return
    reserve = 0
    for token in tokens[1:]:
        if token.lower().startswith("reserve="):
            reserve = max(0, int("".join(ch for ch in token.split("=", 1)[1] if ch.isdigit()) or 0))
    try:
        view = await ctx.auctions.create(
            session,
            seller_id=access.user_id,
            character_id=character_id,
            start_price=max(1, int(start)),
            minutes=minutes,
            reserve_price=reserve,
            chat_id=message.chat.id if is_group(message.chat) else None,
        )
    except (NotFound, Locked, WaifuError) as exc:
        await refuse(message, exc.user_message)
        return
    card_builder = await _listing_card(ctx, view)
    await card(
        message,
        ctx,
        builder=card_builder,
        html=f"🔨 auction #{view.id}: {view.name} from {money(view.start_price)} 🪙, ends in {minutes}m",
    )
    if view.chat_id and view.message_id is None:
        # ``publish`` posts the card, stores the message id on the row and pins it — the pin is
        # what made the reference's auction room legible, and doing all three in the service keeps
        # the id and the pin from disagreeing (this handler used to attach the id a second time).
        await ctx.auctions.publish(int(view.chat_id), view)
    if ctx.bot is not None and message.chat.type != "private":
        # Their seller receipt, in the same shape: id, character, duration.
        try:
            await ctx.bot.send_message(
                chat_id=access.user_id,
                text=(
                    f"✅ <b>Auction #{view.id} listed!</b>\n"
                    f"🎴 <b>{view.name}</b> ({Rarity.from_value(int(view.rarity_id)).badge})\n"
                    f"⏳ Duration: {minutes} min · 💰 opens at {fmt_bid(view.start_price)} 🪙\n"
                    f"/cancelauction {view.id} cancels it while nobody has bid."
                ),
                parse_mode="HTML",
            )
        except Exception:
            pass


async def _listing_card(ctx: AppContext, view: Any) -> RichMessageBuilder:
    builder = RichMessageBuilder().heading(f"🔨 {view.name}", size=2)
    builder.paragraph(
        html=f"{shorten(view.anime, 40)} · {Rarity.from_value(int(view.rarity_id)).badge}"
    )
    builder.table(
        [
            ["opening", money(view.start_price)],
            ["current", money(view.current_bid)],
            ["next bid", money(view.next_minimum)],
            ["bids", str(view.bid_count)],
            ["ends in", human_delta(max(0, int(view.seconds_left)))],
        ],
        compact=True,
        bordered=False,
    )
    if view.note:
        builder.quote(shorten(view.note, 160))
    if view.reserve_price:
        builder.line(f"reserve {money(view.reserve_price)} 🪙")
    return builder


async def _character_id(session: Any, ctx: AppContext, user_id: int, args: Args) -> int | None:
    from waifu.db.repo import collection as collection_repo

    token = args.first
    if not token:
        return None
    if token.isdigit() and len(token) > 3:
        return int(token)
    if token.isdigit():
        data = await ctx.collection.page(session, user_id, page=0, page_size=8)
        index = int(token)
        if 1 <= index <= len(data.items):
            return int(data.items[index - 1].character_id)
    try:
        character = await ctx.collection.find(session, args.raw.split(maxsplit=1)[0])
        return int(character.id)
    except (NotFound, IndexError):
        pass
    owned = await collection_repo.list_owned(session, user_id, limit=200)
    needle = token.lower()
    for entry in owned:
        if needle in entry.name.lower():
            return int(entry.character_id)
    return None


@router.message(Command("bid", "auctionbid"))
async def bid(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, access: Access
) -> None:
    args = Args.of(command)
    if len(args.words) < 2:
        await text(
            message,
            ctx,
            "Usage: <code>/bid &lt;auction id&gt; &lt;amount&gt;</code> — or tap ✋ under the listing.",
        )
        return
    auction_id = int(args.words[0]) if args.words[0].isdigit() else 0
    amount = args.count or 0
    if not amount:
        tail = " ".join(args.words[1:])
        from waifu.plugins._kit import Args as _A

        amount = _A(raw=tail, words=(tail,)).amount or 0
    if not auction_id or not amount:
        await text(message, ctx, "I need the auction id and a number: <code>/bid 12 60000</code>.")
        return
    await do_bid(
        message, ctx, session, auction_id=auction_id, bidder=access.user_id, amount=int(amount)
    )


async def do_bid(
    event: Message | CallbackQuery,
    ctx: AppContext,
    session: Any,
    *,
    auction_id: int,
    bidder: int,
    amount: int,
) -> None:
    leader = getattr(getattr(event, "from_user", None), "username", None) or ""
    try:
        view, outbid, extended = await ctx.auctions.bid(session, auction_id, bidder, amount)
    except BidTooLow as exc:
        # ``❌ Must beat current bid …`` — the reference's wording, because "the minimum bid is
        # 12 000 coins" reads like a rule about the *character*, not about the number to type.
        await refuse(event, f"❌ Must beat current bid {fmt_bid(max(0, exc.minimum))} 🪙")
        return
    except (NotEnoughFunds, NotFound, Locked, PermissionDenied, WaifuError) as exc:
        if isinstance(event, Message) and is_group(event.chat) and ctx.caps.allow("ephemeral"):
            # Bot API 10.3 ephemerals: in a 5,000-member auction room, "you cannot cover 1.2M 🪙"
            # is private information, and the reference pasted it in public for everyone to read.
            await _private_refusal(ctx, event, bidder, f"⚠️ {exc.user_message}")
            return
        await refuse(event, exc.user_message)
        return
    lines = [f"✅ you lead <b>#{view.id}</b> with <b>{money(view.current_bid)} 🪙</b>"]
    if view.previous_bid:
        lines.append(
            f"⏳ {view.bar} {view.clock} · 🔒 {money(view.previous_bid)} 🪙 escrowed from you"
        )
    if outbid:
        lines.append(
            f"💸 {mention(outbid)} has their {money(view.previous_bid)} 🪙 back — refunded the moment "
            "you took the lead"
        )
    if extended:
        lines.append(
            f"⚡ Anti-snipe: +{ctx.settings.auction_extend_seconds}s added! ⏳ {view.clock} left"
        )
    await _respond(event, ctx, "\n".join(lines))
    if outbid:
        await _outbid_notice(ctx, view, outbid)
    if view.message_id and view.chat_id:
        # One edit per ``auction_refresh_seconds`` per auction: ``_update_pinned_message`` edited
        # the caption on every bid, which is exactly the request pattern Telegram's per-message
        # flood limit punishes during a war — the losers' room then shows a stale price for minutes.
        fresh = await ctx.cache.get(REFRESH_NS, view.id) if ctx.cache is not None else None
        if fresh is None:
            if ctx.cache is not None:
                await ctx.cache.set(
                    REFRESH_NS,
                    (view.id,),
                    1,
                    max(1, int(ctx.settings.auction_refresh_seconds)),
                )
            await ctx.auctions.refresh_card(
                int(view.chat_id),
                view,
                int(view.message_id),
                me_id=bidder,
                leader_name=leader,
            )


async def _private_refusal(ctx: AppContext, message: Message, user_id: int, text_: str) -> None:
    """A refusal only the bidder can see (or a toast, when ephemerals are unavailable)."""
    if ctx.bot is not None and ctx.caps.allow("ephemeral"):
        from waifu.tg.ephemeral import send_ephemeral

        try:
            await send_ephemeral(
                ctx.bot, int(message.chat.id), receiver_user_id=user_id, text=text_
            )
            return
        except Exception:
            pass
    await note(message, text_)


async def _outbid_notice(ctx: AppContext, view: Any, user_id: int) -> None:
    """``⚡ 𝗬𝗢𝗨 𝗪𝗘𝗥𝗘 𝗢𝗨𝗧𝗕𝗜𝗗!`` — the reference's DM, plus the refund it never sent."""
    if ctx.bot is None or not user_id:
        return
    import html as _html

    try:
        await ctx.bot.send_message(
            chat_id=int(user_id),
            text=(
                f"⚡ <b>𝗬𝗢𝗨 𝗪𝗘𝗥𝗘 𝗢𝗨𝗧𝗕𝗜𝗗!</b>\n\n"
                f"🎴 <b>{_html.escape(str(view.name))}</b> "
                f"({Rarity.from_value(int(view.rarity_id)).badge})\n"
                f"💰 Your bid: <code>{fmt_bid(view.previous_bid)}</code> 🪙 — returned to you\n"
                f"🔥 New high: <code>{fmt_bid(view.current_bid)}</code> 🪙\n"
                f"⏳ Time left: <b>{view.clock}</b>\n"
                "<i>tap the listing to bid again — your stake is already back in your balance.</i>"
            ),
            parse_mode="HTML",
        )
    except Exception:
        pass


async def _respond(event: Message | CallbackQuery, ctx: AppContext, html: str) -> None:
    if isinstance(event, CallbackQuery):
        await note(event, html[:200], alert=True)
        return
    await text(event, ctx, html)


@router.callback_query(F.data.startswith("auc:bid:"))
async def bid_button(
    callback_query: CallbackQuery, ctx: AppContext, session: Any, access: Access
) -> None:
    """✋ bids the minimum; ``+500 / +1,000 / +5,000 / +10,000`` are ``build_auction_keyboard``'s
    quick steps, added to the current high bid exactly as ``bid_add_<id>_<amount>`` did. Steps
    below this auction's own increment are not offered at all (the reference offered them and then
    failed the press with no explanation).
    """
    parts = split_cb(callback_query.data)
    auction_id = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
    delta = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
    try:
        view = await ctx.auctions.view(session, auction_id)
    except (NotFound, Locked) as exc:
        await refuse(callback_query, f"⏰ {exc.user_message}")
        return
    amount = int(view.current_bid) + delta if delta else int(view.next_minimum)
    await do_bid(
        callback_query,
        ctx,
        session,
        auction_id=auction_id,
        bidder=access.user_id,
        amount=amount,
    )
    await send_listing(callback_query, ctx, session, auction_id=auction_id, actor=access.user_id)


@router.callback_query(F.data.startswith("auc:custom:"))
async def custom_bid_ask(
    callback_query: CallbackQuery, ctx: AppContext, session: Any, access: Access
) -> None:
    """Arm the custom-bid window. The prompt goes to the bidder's DM in a group: the reference
    answered ``✏️ Enter your bid`` *in the channel*, which told every rival exactly how much room
    was left under their ceiling.
    """
    parts = split_cb(callback_query.data)
    auction_id = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
    try:
        view = await ctx.auctions.view(session, auction_id)
    except (NotFound, Locked) as exc:
        await refuse(callback_query, f"⏰ {exc.user_message}")
        return
    ttl = max(30, int(ctx.settings.auction_custom_bid_seconds))
    if ctx.cache is not None:
        await ctx.cache.set(CUSTOM_NS, (access.user_id,), int(auction_id), ttl)
    prompt = (
        f"✏️ <b>Enter your bid for auction #{auction_id}:</b>\n"
        f"Reply with just the number (e.g. <code>{fmt_bid(view.next_minimum)}</code>), or use "
        f"<code>/bid {auction_id} {view.next_minimum}</code>. Valid for {ttl // 60 or 1} min — "
        f"the high bid is {fmt_bid(view.current_bid)} 🪙."
    )
    if callback_query.message and is_group(callback_query.message.chat) and ctx.bot is not None:
        try:
            await ctx.bot.send_message(chat_id=access.user_id, text=prompt, parse_mode="HTML")
        except Exception:
            await note(callback_query, "✏️ I cannot DM you — use /bid directly.")
        return
    await text(callback_query, ctx, prompt)


#: ``auction_bid_input`` in the reference accepted any private non-command text, which meant a
#: stray word inside the 120s window was silently eaten by the auction. The filter now matches a
#: number and nothing else, so this handler cannot swallow another private conversation (an
#: upload reply, a code redemption) that happens to land in the same two minutes.
NUMBER_ONLY = F.text.regexp(r"^[\d.,\u2009 ]{1,24}$")


@router.message(F.chat.type == "private", NUMBER_ONLY)
async def custom_bid_reply(message: Message, ctx: AppContext, session: Any, access: Access) -> None:
    """The other half of ✏️: one bare number, dot-or-comma grouped, inside the window."""
    auction_id = await ctx.cache.get(CUSTOM_NS, access.user_id) if ctx.cache is not None else None
    if not auction_id:
        return
    if ctx.cache is not None:
        # Consumed either way: a stale window must not hijack the next private message the player
        # sends for an unrelated reason (the reference left rows behind until they timed out).
        await ctx.cache.delete(CUSTOM_NS, access.user_id)
    raw = (message.text or "").strip().replace(".", "").replace(",", "").replace("\u2009", "")
    if not raw.isdigit():
        await text(message, ctx, "❌ Enter a valid number (digits only).")
        return
    try:
        view = await ctx.auctions.view(session, int(auction_id))
    except (NotFound, Locked) as exc:
        await text(message, ctx, f"⏰ {exc.user_message}")
        return
    if int(raw) <= int(view.current_bid):
        await text(
            message,
            ctx,
            f"❌ Must beat current bid <code>{fmt_bid(view.current_bid)}</code> 🪙.",
        )
        return
    await do_bid(
        message,
        ctx,
        session,
        auction_id=int(auction_id),
        bidder=access.user_id,
        amount=int(raw),
    )
    await text(
        message,
        ctx,
        f"✅ <b>Bid placed!</b>\n🎴 <b>{view.name}</b>\n💰 Your bid: <code>{fmt_bid(int(raw))}</code> 🪙",
    )


@router.callback_query(F.data.startswith("auc:view:"))
async def view_button(
    callback_query: CallbackQuery, ctx: AppContext, session: Any, access: Access
) -> None:
    auction_id = int((callback_query.data or "").split(":")[-1] or 0)
    await send_listing(callback_query, ctx, session, auction_id=auction_id, actor=access.user_id)


async def send_listing(
    event: Message | CallbackQuery, ctx: AppContext, session: Any, *, auction_id: int, actor: int
) -> None:
    view = await ctx.auctions.view(session, auction_id)
    builder = await _listing_card(ctx, view)
    rows = [
        [
            callback("✋ bid minimum", cb("auc", "bid", str(auction_id))),
            callback("✏️ custom bid", cb("auc", "custom", str(auction_id))),
        ]
    ]
    steps = view.steps(max(1, int(view.next_minimum) - int(view.current_bid)))
    for pair in (steps[:2], steps[2:4]):
        if pair:
            rows.append(
                [
                    callback(
                        f"💰 +{fmt_bid(step)}",
                        cb("auc", "bid", str(auction_id), str(step)),
                        disabled=not view.is_live,
                    )
                    for step in pair
                ]
            )
    rows.append([callback("📜 history", cb("auc", "log", str(auction_id)))])
    rows.append([callback(f"page {view.id}", cb("auc", "noop"))])
    if view.seller_id == actor or (await _is_staff(session, actor)):
        rows.append([callback("🚫 cancel", cb("auc", "cancel", str(auction_id)))])
    html = (
        f"#{view.id} {view.name}: current {money(view.current_bid)}, "
        f"next {money(view.next_minimum)}, {view.bid_count} bids, "
        f"💡 Tap a button below to bid! (⏳ {view.clock})"
    )
    if isinstance(event, CallbackQuery):
        await edit(event, ctx, builder=builder, html=html, buttons=rows)
    else:
        await card(event, ctx, builder=builder, html=html, buttons=rows)


async def _is_staff(session: Any, user_id: int) -> bool:
    from waifu.db.repo import users as user_repo

    player = await user_repo.get(session, user_id)
    return bool(player and str(getattr(player, "role", "user")) in {"owner", "admin", "moderator"})


@router.callback_query(F.data.startswith("auc:log:"))
async def bid_log(callback_query: CallbackQuery, ctx: AppContext, session: Any) -> None:
    auction_id = int((callback_query.data or "").split(":")[-1] or 0)
    rows = await ctx.auctions.bid_log(session, auction_id, limit=12)
    lines = ["📜 bid history"]
    for row in rows:
        lines.append(
            f"• {mention(int(getattr(row, 'bidder_id', 0)))} — {money(int(getattr(row, 'amount', 0)))} 🪙 {str(getattr(row, 'created_at', ''))[:16]}"
        )
    await note(callback_query, "\n".join(lines)[:190] or "no bids yet", alert=True)


@router.callback_query(F.data.startswith("auc:cancel:"))
async def cancel_auction(
    callback_query: CallbackQuery, ctx: AppContext, session: Any, access: Access
) -> None:
    auction_id = int((callback_query.data or "").split(":")[-1] or 0)
    try:
        _auction, refunds = await ctx.auctions.cancel(session, auction_id, access.user_id)
    except (PermissionDenied, NotFound, Locked, WaifuError) as exc:
        await note(callback_query, exc.user_message, alert=True)
        return
    await note(callback_query, f"cancelled — {refunds} stake(s) refunded", alert=True)


@router.message(Command("cancelauction", "cancelauc"))
async def cancel_command(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, access: Access
) -> None:
    args = Args.of(command)
    auction_id = args.integer
    if not auction_id:
        await text(
            message,
            ctx,
            "Usage: <code>/cancelauction &lt;id&gt;</code> (only while there are no bids)",
        )
        return
    try:
        _auction, refunds = await ctx.auctions.cancel(session, auction_id, access.user_id)
    except (PermissionDenied, NotFound, Locked, WaifuError) as exc:
        await refuse(message, exc.user_message)
        return
    await text(message, ctx, f"🚫 auction #{auction_id} cancelled — {refunds} stake(s) refunded.")


@router.message(Command("auctions", "auct", "auctionlist", "auction_list"))
async def auctions_list(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, access: Access
) -> None:
    await send_live(
        message, ctx, session, page=max(0, Args.of(command).paged(1) - 1), actor=access.user_id
    )


async def send_live(
    event: Message | CallbackQuery,
    ctx: AppContext,
    session: Any,
    *,
    page: int,
    actor: int,
    sort: str = "ends",
) -> None:
    views, total = await ctx.auctions.live(session, limit=6, offset=page * 6, sort=sort)
    builder = RichMessageBuilder().heading(f"🔨 live auctions — {money(total)}", size=1)
    if views:
        rows = [["#", "character", "current", "next", "bids", "ends"]]
        for view in views:
            rows.append(
                [
                    str(view.id),
                    f"<b>{shorten(view.name, 22)}</b> {Rarity.from_value(int(view.rarity_id)).emoji}",
                    money(view.current_bid),
                    money(view.next_minimum),
                    str(view.bid_count),
                    human_delta(max(0, int(view.seconds_left))),
                ]
            )
        builder.table(rows, compact=True)
    else:
        builder.paragraph(
            html="<i>nothing listed — /auction &lt;character&gt; &lt;price&gt; to open one</i>"
        )
    buttons = [
        pager_row(
            page=page, pages=max(1, -(-total // 6)), prefix="auc", extra=f"live/{page}/{sort}"
        )
    ]
    buttons.append(
        [
            callback("🧾 mine", cb("auc", "mine", str(page))),
            callback("📊 market stats", cb("auc", "stats")),
        ]
    )
    html = (
        "\n".join(f"#{view.id} {view.name} → {money(view.current_bid)} 🪙" for view in views)
        or "no live auctions"
    )
    if isinstance(event, CallbackQuery):
        await edit(event, ctx, builder=builder, html=html, buttons=buttons)
    else:
        await card(event, ctx, builder=builder, html=html, buttons=buttons)


@router.callback_query(F.data.regexp(r"^auc:(first|prev|next|last):"))
async def live_page(
    callback_query: CallbackQuery, ctx: AppContext, session: Any, access: Access
) -> None:
    """Pager buttons carry ``live/<page>/<sort>`` — see :func:`send_live`."""
    parts = (callback_query.data or "").split(":")
    direction = parts[1]
    raw = (parts[2] if len(parts) > 2 else "live/0/ends").split("/")
    page = int(raw[1]) if len(raw) > 1 and raw[1].isdigit() else 0
    sort = raw[2] if len(raw) > 2 and raw[2] in {"ends", "value", "bids"} else "ends"
    if direction == "next":
        page += 1
    elif direction == "prev":
        page = max(0, page - 1)
    elif direction == "first":
        page = 0
    await send_live(callback_query, ctx, session, page=page, actor=access.user_id, sort=sort)


@router.callback_query(F.data.startswith("auc:mine"))
async def my_auctions(
    callback_query: CallbackQuery, ctx: AppContext, session: Any, access: Access
) -> None:
    await send_mine(callback_query, ctx, session, user_id=access.user_id)


@router.message(Command("myauctions", "myauction", "mybids"))
async def my_auctions_command(
    message: Message, ctx: AppContext, session: Any, access: Access
) -> None:
    await send_mine(message, ctx, session, user_id=access.user_id)


async def send_mine(
    event: Message | CallbackQuery, ctx: AppContext, session: Any, *, user_id: int
) -> None:
    rows = await ctx.auctions.mine(session, user_id, as_seller=True, limit=10)
    won = await ctx.auctions.history(session, user_id, limit=10)
    builder = RichMessageBuilder().heading("🧾 your auctions", size=2)
    table = [["id", "character", "status", "current"]]
    for auction in rows:
        table.append(
            [
                str(auction.id),
                shorten(str(getattr(auction, "character_id", "?")), 18),
                str(auction.status),
                money(int(auction.current_bid or 0)),
            ]
        )
    if len(table) > 1:
        builder.table(table, compact=True)
    else:
        builder.paragraph(html="<i>you have not listed anything</i>")
    if won:
        builder.divider().heading("history", size=3).table(
            [
                ["id", "status", "price"],
                *[
                    [str(a.id), str(a.status), money(int(a.sold_price or a.current_bid or 0))]
                    for a in won[:6]
                ],
            ],
            compact=True,
        )
    html = f"{len(rows)} live · {len(won)} in history"
    if isinstance(event, CallbackQuery):
        await edit(event, ctx, builder=builder, html=html)
    else:
        await card(event, ctx, builder=builder, html=html)


@router.callback_query(F.data.startswith("auc:stats"))
async def auction_stats(callback_query: CallbackQuery, ctx: AppContext, session: Any) -> None:
    stats = await ctx.auctions.stats(session)
    rows = [[key.replace("_", " ").title(), money(value)] for key, value in stats.items()]
    await note(
        callback_query,
        " · ".join(f"{row[0]}: {row[1]}" for row in rows)[:190] or "no auctions yet",
        alert=True,
    )


@router.callback_query(F.data.startswith("auc:noop"))
async def noop(callback_query: CallbackQuery) -> None:
    await callback_query.answer(cache_time=60)
