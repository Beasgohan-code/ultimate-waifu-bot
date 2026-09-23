"""``/trade``, ``/taccept``, ``/tdecline``, ``/tcancel``, ``/trades``, ``/tradeid``.

Their trade was: A types ``/trade @B give:id want:id``, B types ``/accept``, and the bot
moves the rows. Three things went wrong in production: both sides could accept twice
(no state machine), a character listed in two open trades could leave twice (no lock),
and a trade interrupted by a restart simply vanished.

Here an offer is a row with a status machine (``proposed → awaiting_partner → ready →
executed``), each side's characters are locked the moment the offer is created
(``Ownership.is_locked``, which is why /sell refuses), and execution is one transaction —
so a crash either completes both sides or neither.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

from waifu.errors import Locked, NotFound, PermissionDenied, WaifuError
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
    refuse,
    resolve_user,
    shorten,
    text,
)

if TYPE_CHECKING:  # pragma: no cover
    from waifu.core.access import Access
    from waifu.core.context import AppContext

router = Router(name="trades")


@router.message(Command("trade", "offer"))
async def trade(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, access: Access
) -> None:
    """/trade <player> give:<ids> want:<ids> [cash:<amount>]"""
    args = Args.of(command)
    if not args.raw:
        await send_open(message, ctx, session, user_id=access.user_id)
        return
    partner = await resolve_user(session, message, args.first)
    if partner is None or partner == access.user_id:
        await text(
            message, ctx, "Trade with whom? <code>/trade @name give:12,44 want:87 cash:5000</code>"
        )
        return
    rest = " ".join(args.words[1:])
    give = _ids(rest, "give")
    receive = _ids(rest, "want") or _ids(rest, "get") or _ids(rest, "receive")
    cash = _cash(rest)
    if not give and not receive and not cash:
        await text(
            message,
            ctx,
            "Say what is on each side: <code>give:12 want:44</code> (ids from /collection), plus optional <code>cash:5000</code>.",
        )
        return
    try:
        view = await ctx.trades.propose(
            session,
            initiator_id=access.user_id,
            partner_id=partner,
            give=_mapping(give),
            receive=_mapping(receive),
            cash=cash,
        )
    except (NotFound, Locked, WaifuError) as exc:
        await refuse(message, exc.user_message)
        return
    builder = await _offer_card(ctx, session, view)
    await card(message, ctx, builder=builder, html=f"offer #{view.id} → {mention(partner)}")
    await ctx.trades.send_confirmation(
        message.chat.id,
        partner,
        f"🤝 {mention(access.user_id)} offers a trade (#{view.id}). Tap below to accept.",
        buttons=[
            [
                callback("✅ accept", cb("tr", "accept", str(view.id))),
                callback("❌ decline", cb("tr", "decline", str(view.id))),
            ]
        ],
    )


def _ids(text_body: str, key: str) -> list[int]:
    for token in text_body.replace(",", " ").split():
        if token.lower().startswith(f"{key}:"):
            payload = token.split(":", 1)[1]
            return [int(part) for part in payload.split(",") if part.isdigit()]
    return []


def _cash(text_body: str) -> int:
    for token in text_body.split():
        if token.lower().startswith("cash:"):
            digits = "".join(ch for ch in token.split(":", 1)[1] if ch.isdigit())
            return int(digits or 0)
    return 0


def _mapping(ids: list[int]) -> dict[int, int]:
    """One copy each; ``give:12x2`` asks for two (a count the owner can actually meet)."""
    out: dict[int, int] = {}
    for character_id in ids:
        out[character_id] = out.get(character_id, 0) + 1
    return out


async def _offer_card(ctx: AppContext, session: Any, view: Any) -> RichMessageBuilder:
    builder = RichMessageBuilder().heading(f"🤝 offer #{view.id}", size=2)
    rows = [["side", "characters", "coins"]]
    for side, items, who in (
        ("gives", view.giving, view.initiator_id),
        ("wants", view.receiving, view.partner_id),
    ):
        names = (
            ", ".join(shorten(await _name(ctx, session, int(cid)), 22) for cid in (items or {}))
            or "—"
        )
        rows.append(
            [
                f"{side} ({mention(int(who))})",
                names,
                money(int((items or {}).get("cash", 0) or 0)) if "cash" in (items or {}) else "",
            ]
        )
    if view.cash:
        rows.append(
            [
                "cash",
                f"from {'initiator' if view.cash > 0 else 'partner'}",
                money(abs(int(view.cash))),
            ]
        )
    builder.table(rows, compact=True)
    state = {
        "proposed": "⏳ waiting for the other player",
        "awaiting_partner": "⏳ partner has not accepted",
        "ready": "✅ both accepted — executing",
        "executed": "🎉 completed",
        "cancelled": "🚫 cancelled",
        "expired": "⌛ expired",
    }.get(str(view.status), str(view.status))
    builder.line(state)
    builder.footer(f"expires in {int(view.expires_in)}s; both sides' copies are locked until then")
    return builder


async def _name(ctx: AppContext, session: Any, character_id: int) -> str:
    character = await ctx.collection.character(session, character_id)
    return character.name if character else f"#{character_id}"


@router.message(Command("taccept", "accept", "traccept"))
async def accept(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, access: Access
) -> None:
    args = Args.of(command)
    trade_id = args.integer
    if not trade_id:
        await send_open(message, ctx, session, user_id=access.user_id)
        return
    await do_accept(message, ctx, session, trade_id=trade_id, user_id=access.user_id)


async def do_accept(
    event: Message | CallbackQuery, ctx: AppContext, session: Any, *, trade_id: int, user_id: int
) -> None:
    try:
        view = await ctx.trades.set_accept(session, trade_id, user_id, accept=True)
    except (PermissionDenied, NotFound, Locked, WaifuError) as exc:
        await refuse(event, exc.user_message)
        return
    if view.status in {"ready", "executed"}:
        result = await ctx.trades.execute(session, trade_id)
        await _respond(
            event,
            ctx,
            f"🎉 trade #{trade_id} executed — {len(result.get('moves') or [])} character move(s).",
        )
        return
    await _respond(event, ctx, f"✅ accepted. Waiting for the other side (offer #{trade_id}).")


@router.callback_query(F.data.startswith("tr:accept:"))
async def accept_button(
    callback_query: CallbackQuery, ctx: AppContext, session: Any, access: Access
) -> None:
    await do_accept(
        callback_query,
        ctx,
        session,
        trade_id=int((callback_query.data or "").split(":")[-1] or 0),
        user_id=access.user_id,
    )


@router.callback_query(F.data.startswith("tr:decline:"))
async def decline_button(
    callback_query: CallbackQuery, ctx: AppContext, session: Any, access: Access
) -> None:
    trade_id = int((callback_query.data or "").split(":")[-1] or 0)
    try:
        await ctx.trades.cancel(session, trade_id, access.user_id)
    except (NotFound, Locked, WaifuError) as exc:
        await note(callback_query, exc.user_message, alert=True)
        return
    await note(callback_query, "declined — their copies are unlocked again")


@router.message(Command("tcancel", "decline"))
async def cancel(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, access: Access
) -> None:
    args = Args.of(command)
    trade_id = args.integer
    if not trade_id:
        await text(message, ctx, "Usage: <code>/tcancel &lt;offer id&gt;</code>")
        return
    try:
        view = await ctx.trades.cancel(session, trade_id, access.user_id)
    except (NotFound, Locked, PermissionDenied, WaifuError) as exc:
        await refuse(message, exc.user_message)
        return
    await text(message, ctx, f"🚫 offer #{view.id} cancelled; everything unlocked.")


@router.message(Command("trades", "mytrade", "opentrades"))
async def trades_list(message: Message, ctx: AppContext, session: Any, access: Access) -> None:
    await send_open(message, ctx, session, user_id=access.user_id)


async def send_open(
    event: Message | CallbackQuery, ctx: AppContext, session: Any, *, user_id: int
) -> None:
    rows = await ctx.trades.open_for(session, user_id)
    builder = RichMessageBuilder().heading(f"🤝 your open offers — {len(rows)}", size=2)
    buttons: list[list[Any]] = []
    if rows:
        table = [["id", "with", "status", "actions"]]
        for view in rows[:8]:
            other = view.partner_id if view.initiator_id == user_id else view.initiator_id
            table.append([str(view.id), mention(int(other)), str(view.status), ""])
            buttons.append(
                [
                    callback(f"#{view.id} accept", cb("tr", "accept", str(view.id))),
                    callback(f"#{view.id} cancel", cb("tr", "decline", str(view.id))),
                ]
            )
        builder.table(table, compact=True)
    else:
        builder.paragraph(html="<i>nothing open — start one with /trade @player give:… want:…</i>")
    html = f"{len(rows)} open offer(s)"
    if isinstance(event, CallbackQuery):
        await edit(event, ctx, builder=builder, html=html, buttons=buttons)
    else:
        await card(event, ctx, builder=builder, html=html, buttons=buttons)


@router.message(Command("tradeid", "offercode"))
async def by_code(message: Message, ctx: AppContext, session: Any, command: CommandObject) -> None:
    """``/tradeid ABC123`` — offers can be shared as a code, for cross-chat trades."""
    args = Args.of(command)
    if not args.first:
        await text(message, ctx, "Usage: <code>/tradeid &lt;code&gt;</code>")
        return
    view = await ctx.trades.by_code(session, args.first.upper())
    if view is None:
        await text(message, ctx, "No offer with that code (they expire).")
        return
    await card(
        message, ctx, builder=await _offer_card(ctx, session, view), html=f"offer #{view.id}"
    )


async def _respond(event: Message | CallbackQuery, ctx: AppContext, html: str) -> None:
    if isinstance(event, CallbackQuery):
        await note(event, html[:200], alert=True)
        return
    await text(event, ctx, html)
