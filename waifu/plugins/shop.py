"""``/shop``, ``/bag``, ``/use``, ``/skip``, ``/cooldowns`` — the consumables surface.

Their item shop (/cshop000000) sold a "Skip Cooldown" item that, when used while
nothing was on cooldown, deleted the item and did nothing — and bought items were never
charged at all in one of the three code paths. Both are handled here by the service:
``ItemService.use`` refuses before consuming, and the debit lives in one place.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

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
    resolve_user,
    text,
)

if TYPE_CHECKING:  # pragma: no cover
    from waifu.core.access import Access
    from waifu.core.context import AppContext

router = Router(name="shop")


@router.message(Command("shop", "bag", "items", "inventory", "inv"))
async def shop(message: Message, ctx: AppContext, session: Any, access: Access) -> None:
    await send_bag(message, ctx, session, user_id=access.user_id)


async def send_bag(
    event: Message | CallbackQuery, ctx: AppContext, session: Any, *, user_id: int
) -> None:
    inventory = await ctx.items.inventory(session, user_id)
    cooldowns = await ctx.items.cooldowns(session, user_id)
    builder = RichMessageBuilder().heading("🎒 your bag", size=1)
    if inventory:
        rows = [["item", "charges", "use"]]
        for entry in inventory:
            clock = f" · ⌛ {entry.hours_left}h left" if entry.hours_left else ""
            rows.append([entry.name, f"×{entry.owned}{clock}", f"/use {entry.key}"])
        builder.table(rows, compact=True)
    else:
        builder.paragraph(html="<i>empty — /market has consumables</i>")
    builder.divider()
    builder.paragraph(
        html="💡 <code>/skip 1</code> resets /daily · <code>/skip 2</code> loads a 🛡️ bomb shield · "
        "<code>/skip 3</code> loads a 🔒 steal shield"
    )
    if cooldowns:
        builder.divider()
        builder.heading("⏳ on cooldown", size=3)
        builder.line(
            " · ".join(
                f"{name} {seconds}s" for name, seconds in sorted(cooldowns.items()) if seconds
            )
        )
        builder.paragraph(html="A <b>Skip Cooldown</b> clears one of these — <code>/skip</code>.")
    html = "\n".join(f"{entry.name} ×{entry.owned}" for entry in inventory) or "bag is empty"
    buttons = [[callback(f"🧹 skip ({len(cooldowns)})", cb("shop", "skip"))]] if cooldowns else None
    if isinstance(event, CallbackQuery):
        await edit(event, ctx, builder=builder, html=html, buttons=buttons or [])
    else:
        await card(event, ctx, builder=builder, html=html, buttons=buttons)


@router.callback_query(F.data == "shop:open")
async def shop_open(
    callback_query: CallbackQuery, ctx: AppContext, session: Any, access: Access
) -> None:
    await send_bag(callback_query, ctx, session, user_id=access.user_id)


@router.message(Command("use"))
async def use(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, access: Access
) -> None:
    await do_use(message, ctx, session, user_id=access.user_id, args=Args.of(command))


async def do_use(
    event: Message | CallbackQuery, ctx: AppContext, session: Any, *, user_id: int, args: Args
) -> None:
    key = args.first.lower()
    if not key:
        inventory = await ctx.items.inventory(session, user_id)
        await _respond(
            event,
            ctx,
            "Use what? "
            + (
                ", ".join(f"<code>/use {entry.key}</code>" for entry in inventory)
                or "your bag is empty."
            ),
        )
        return
    target = await resolve_user(session, event, args.rest) if args.rest else None
    # ``use`` never charges before the effect lands, so a failed use leaves the item
    # in the bag — the reference bot's "item vanished, nothing happened" bug.
    try:
        result = await ctx.items.use(session, user_id, key, target_id=target)
    except (NotFound, Locked, NotEnoughFunds, WaifuError) as exc:
        await _respond(event, ctx, f"❌ {exc.user_message}")
        return
    except ValueError:  # a target-requiring item used without one
        await _respond(event, ctx, f"That item needs a player: <code>/use {key} @username</code>")
        return
    details = ", ".join(f"{k} {v}" for k, v in (result.details or {}).items())
    await _respond(
        event,
        ctx,
        f"✅ <b>{result.name}</b> — {result.effect}"
        + (f" ({details})" if details else "")
        + (f" · {result.stacks_left} left" if result.stacks_left else ""),
    )


@router.callback_query(F.data.startswith("shop:use:"))
async def use_button(
    callback_query: CallbackQuery, ctx: AppContext, session: Any, access: Access
) -> None:
    key = (callback_query.data or "").split(":")[-1]
    await do_use(
        callback_query, ctx, session, user_id=access.user_id, args=Args(raw=key, words=(key,))
    )


@router.message(Command("skip"))
async def skip(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, access: Access
) -> None:
    """``/skip 1|2|3`` spends one Skip Cooldown ticket, exactly as ``skip_cmd`` did: ``1`` resets
    ``/daily``, ``2`` loads a 🛡️ bomb shield, ``3`` a 🔒 steal shield. A bare ``/skip`` keeps this
    port's older behaviour (clear whatever is cooling) because the shop button calls that.
    """
    mode = (Args.of(command).first or "").strip()
    if mode and mode not in ctx.items.SKIP_MODES:
        await text(message, ctx, ctx.items.SKIP_USAGE)
        return
    try:
        if mode:
            ticket = await ctx.items.skip_mode(session, access.user_id, mode)
            await text(message, ctx, f"{ticket.effect} · {ticket.stacks_left} ticket(s) left")
            return
        result = await ctx.items.use(session, access.user_id, "skip")
    except WaifuError as exc:
        # No item: offer the paid alternative instead of a dead end (their /skip said
        # "nothing on cooldown" even when the player simply did not own the item).
        await text(
            message,
            ctx,
            f"⏭️ {exc.user_message}\nBuy a <b>Skip Cooldown</b> in /market, or wait it out.",
        )
        return
    await text(message, ctx, f"⏭️ {result.effect} · {result.stacks_left} left")


@router.callback_query(F.data == "shop:skip")
async def skip_button(
    callback_query: CallbackQuery, ctx: AppContext, session: Any, access: Access
) -> None:
    try:
        result = await ctx.items.use(session, access.user_id, "skip")
    except WaifuError as exc:
        await note(callback_query, exc.user_message, alert=True)
        return
    await send_bag(callback_query, ctx, session, user_id=access.user_id)
    await note(callback_query, result.effect)


@router.message(Command("heists", "robstats"))
async def heists(message: Message, ctx: AppContext, session: Any, access: Access) -> None:
    stats = await ctx.items.heist_stats(session, access.user_id)
    rows = [[key.replace("_", " ").title(), money(value)] for key, value in stats.items()]
    builder = (
        RichMessageBuilder()
        .heading("🕵️ robbery record", size=2)
        .table(rows or [["no attempts yet", "—"]], compact=True, bordered=False)
    )
    await card(message, ctx, builder=builder, html="\n".join(f"{row[0]}: {row[1]}" for row in rows))


async def _respond(event: Message | CallbackQuery, ctx: AppContext, html: str) -> None:
    if isinstance(event, CallbackQuery):
        await note(event, html[:200], alert=True)
        return
    await text(event, ctx, html)


@router.callback_query(F.data.regexp(r"^mkt:noop|^shop:noop"))
async def noop(callback_query: CallbackQuery) -> None:
    await callback_query.answer(cache_time=3600)
