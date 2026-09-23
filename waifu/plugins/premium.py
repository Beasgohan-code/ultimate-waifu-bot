"""``/premium``, ``/buy stars``, ``/raffle``, ``/boost``, ``/preview`` — Telegram Stars.

Stars (Bot API 7+) rather than an external gateway: no business account, no webhook
signature to get wrong, no payment that a player has to leave Telegram for, and refunds
are an API call (``refundStarPayment``) instead of a support-ticket spreadsheet.

The order lifecycle is deliberately boring: an invoice is created with a payload that
encodes the user and the pack, ``pre_checkout_query`` answers immediately (so a buyer is
never held at the payment sheet while the bot thinks), and delivery is
``settle_purchase`` — idempotent on the charge id, which is the only thing that keeps a
double notification from minting two subscriptions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message, PreCheckoutQuery, Update

from waifu.errors import NotFound, WaifuError
from waifu.logging import get_logger
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
    text,
)

if TYPE_CHECKING:  # pragma: no cover
    from waifu.core.access import Access
    from waifu.core.context import AppContext

router = Router(name="premium")
log = get_logger("plugins.premium")


@router.message(Command("premium", "vip", "stars"))
async def premium(message: Message, ctx: AppContext, session: Any, access: Access) -> None:
    await send_menu(message, ctx, session, user_id=access.user_id)


async def send_menu(
    event: Message | CallbackQuery, ctx: AppContext, session: Any, *, user_id: int
) -> None:
    info = await ctx.premium.premium(session, user_id)
    packs = await ctx.premium.coin_packs()
    builder = RichMessageBuilder().heading("👑 premium", size=1)
    builder.paragraph(
        html=(
            f"<b>{'active' if info.is_active else 'not active'}</b>"
            + (f" · {info.hours_left}h left" if info.hours_left else "")
            + (" · subscription" if info.subscribed else "")
        )
    )
    rows = [["perk", "what it gives you"]]
    # ``perks`` is a list of sentences — the old code called ``.items()`` on it
    # and /premium raised AttributeError on the first user who opened it.
    for perk in info.perks or []:
        rows.append(["⭐", str(perk)])
    rows += [
        ["double /hclaim", "two free claims a day"],
        ["+claim odds", f"+{ctx.settings.premium_claim_boost_percent}% weight toward high tiers"],
        ["no spawn cap", f"/hclaim can roll past tier {ctx.settings.spawn_high_tier_ceiling}"],
        ["profile badge", "shown on /profile and every card"],
    ]
    builder.table(rows, compact=True, bordered=False)
    buttons: list[list[Any]] = []
    if ctx.settings.features.stars:
        table = [["pack", "stars", "you get"]]
        for pack in packs:
            table.append(
                [
                    pack.title,
                    f"⭐ {money(pack.stars)}",
                    f"{money(pack.coins)} 🪙"
                    + (f" + {pack.premium_hours // 24}d premium" if pack.premium_hours else ""),
                ]
            )
            buttons.append([callback(f"{pack.title} · ⭐{pack.stars}", cb("prem", "buy", pack.id))])
        builder.divider().heading("coins with Stars", size=3).table(table, compact=True)
    if ctx.settings.features.premium and ctx.settings.premium_sub_stars:
        buttons.append(
            [
                callback(
                    f"👑 subscribe · ⭐{money(ctx.settings.premium_sub_stars)}/mo",
                    cb("prem", "sub"),
                )
            ]
        )
    buttons.append([callback("🧾 my orders", cb("prem", "orders"))])
    html = "premium menu — /premium"
    if isinstance(event, CallbackQuery):
        await edit(event, ctx, builder=builder, html=html, buttons=buttons)
    else:
        await card(event, ctx, builder=builder, html=html, buttons=buttons)


@router.callback_query(F.data.startswith("prem:buy:"))
async def buy_pack(
    callback_query: CallbackQuery, ctx: AppContext, session: Any, access: Access
) -> None:
    pack = (callback_query.data or "").split(":")[-1]
    try:
        order, link = await ctx.premium.invoice(session, access.user_id, pack)
    except (NotFound, WaifuError) as exc:
        await note(callback_query, exc.user_message, alert=True)
        return
    if not link:
        await note(
            callback_query,
            "Star payments are not configured on this instance (FEATURE_STARS / bot payments settings).",
            alert=True,
        )
        return
    await callback_query.answer(url=link)
    await note(
        callback_query,
        f"invoice ready: {money(order.get('coins', 0))} 🪙 for ⭐{money(order.get('stars', 0))}",
    )


@router.callback_query(F.data == "prem:sub")
async def subscribe_button(
    callback_query: CallbackQuery, ctx: AppContext, session: Any, access: Access
) -> None:
    link = await ctx.premium.subscribe(access.user_id, months=1)
    if not link:
        await note(
            callback_query,
            "subscriptions need a Star-subscription configured on the bot (Bot API 9.x). Coins packs work meanwhile.",
            alert=True,
        )
        return
    await callback_query.answer(url=link)


@router.callback_query(F.data == "prem:orders")
async def orders_button(
    callback_query: CallbackQuery, ctx: AppContext, session: Any, access: Access
) -> None:
    orders = await ctx.premium.my_orders(session, access.user_id, limit=10)
    rows = orders.get("rows") if isinstance(orders, dict) else None
    lines = [
        f"• {str(getattr(row, 'created_at', ''))[:16]} {getattr(row, 'product_ref', '?')} ⭐{getattr(row, 'star_count', 0)} → {'delivered' if getattr(row, 'delivered_at', None) else 'pending'}"
        for row in (rows or [])[:8]
    ]
    await note(callback_query, "\n".join(lines) or "no orders yet", alert=True)


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery, ctx: AppContext) -> None:
    """Approve at the payment sheet; delivery happens on the successful-payment update."""
    try:
        await query.answer(ok=True)
    except Exception as exc:  # pragma: no cover - invoice may already be expired
        ctx.extra.setdefault("pre_checkout_errors", []).append(str(exc))


@router.message(F.successful_payment)
async def delivered(message: Message, ctx: AppContext, session: Any) -> None:
    payload = getattr(message.successful_payment, "invoice_payload", "") or ""
    charge = getattr(message.successful_payment, "telegram_payment_charge_id", "") or ""
    stars = int(getattr(message.successful_payment, "total_amount", 0) or 0)
    if message.from_user is None:
        return
    result = await ctx.premium.settle(session, payload, charge_id=charge, star_count=stars)
    lines = [f"🎉 {key}: {value}" for key, value in result.items() if value not in (None, "", 0)]
    await text(message, ctx, "\n".join(lines) or "payment received — thanks!")


@router.purchased_paid_media()
async def on_purchased_paid_media(update: Update, ctx: AppContext, session: Any) -> None:
    """A paid-media purchase (Bot API 7.2) — the *only* proof of payment.

    Without this handler the money leaves the buyer's account and the
    ``purchased_paid_media`` update dies unhandled: no grant, no receipt, and
    the order row stuck in ``pending`` until an admin notices it in
    ``/revenue``. Settlement is idempotent on the payload, so a Telegram retry
    cannot deliver twice.
    """
    result = await ctx.premium.settle_purchase(session, update)
    if not result.get("ok"):
        log.warning("paid media purchase not settled: %s", result.get("reason"))


@router.subscription()
async def on_subscription(update: Update, ctx: AppContext, session: Any) -> None:
    """A Stars subscription changed (Bot API 10.1) — mirror it.

    Telegram owns the billing cycle; this is where a cancel at Telegram's side
    stops the perks, a failed charge is reported to the owner, and a (silent)
    renewal keeps premium honest. See :meth:`PremiumService.sync_subscription`.
    """
    await ctx.premium.sync_subscription(session, update.subscription)


@router.message(Command("raffle"))
async def raffle(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, access: Access
) -> None:
    """``/raffle 5000 600`` — coins to a random reactor (uses the poll router in misc)."""
    from waifu.plugins._kit import staff_of

    staff_of(access)
    args = Args.of(command)
    reward = args.integer or 5000
    seconds = 600
    numbers = [int(token) for token in args.words if token.isdigit()]
    if len(numbers) > 1:
        seconds = max(60, min(86400, numbers[1]))
    poll = None
    if ctx.caps.allow("reactions"):
        from aiogram.types import InputPollOption

        try:
            poll = await ctx.bot.send_poll(
                message.chat.id,
                question=f"🎉 react or vote to enter — {money(reward)} 🪙 in {seconds // 60}m",
                options=[InputPollOption(text="🎟️ enter"), InputPollOption(text="🚫 pass")],
                type="quiz",
                is_anonymous=False,
            )
        except Exception:  # pragma: no cover - chats with poll rights disabled
            poll = None
    view = await ctx.premium.open_raffle(
        session,
        chat_id=message.chat.id,
        message_id=poll.message_id if poll else message.message_id,
        reward=reward,
        seconds=seconds,
    )
    await text(
        message,
        ctx,
        f"🎟️ raffle open for {seconds // 60} minutes · {money(reward)} 🪙 · id {getattr(view, 'id', '?')}",
    )


@router.message(Command("boosters", "boostboard"))
async def boosters(message: Message, ctx: AppContext, session: Any, access: Access) -> None:
    rows = await ctx.premium.boosters(session, message.chat.id, limit=15)
    if not rows:
        await text(
            message, ctx, "Nobody has boosted this chat yet — boosting unlocks the raffle perks."
        )
        return
    lines = [
        f"{index}. {mention(user_id)} — {count} boost(s)"
        for index, (user_id, count) in enumerate(rows, start=1)
    ]
    await text(message, ctx, "🚀 <b>boosters</b>\n" + "\n".join(lines))


@router.callback_query(F.data.startswith("prem:noop"))
async def noop(callback_query: CallbackQuery) -> None:
    await callback_query.answer(cache_time=3600)
