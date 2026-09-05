"""``/balance``, ``/daily``, ``/work``, ``/spin``, ``/rob``, ``/bomb``, ``/give``.

Money never moves in a handler: every command below calls exactly one
:class:`~waifu.services.economy.EconomyService` method, and that method writes a ledger
row plus the balance in the caller's transaction. So the pattern that made the
reference bot's economy unfixable — ``UPDATE users SET balance = balance - x`` in five
different commands, each with its own rounding, none of them auditable — cannot come
back: ``/integrity`` proves the invariant, and a test enforces it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

from waifu.errors import AlreadyClaimed, CooldownActive, NotEnoughFunds, WaifuError
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
    text,
)

if TYPE_CHECKING:  # pragma: no cover
    from waifu.core.access import Access
    from waifu.core.context import AppContext

router = Router(name="economy")


@router.message(Command("balance", "bal", "wallet", "me"))
async def balance(
    message: Message, ctx: AppContext, session: Any, user: Any, access: Access
) -> None:
    target = access.user_id
    coins = await ctx.economy.balance(session, target)
    summary = await ctx.collection.summary(session, target)
    premium = await ctx.premium.is_premium(session, target)
    stats = await ctx.stats.player(session, target)
    value = int(summary.get("value", 0) or 0)
    builder = (
        RichMessageBuilder()
        .heading(f"💰 {money(coins)} 🪙", size=1)
        .table(
            [
                ["harem value", f"{money(value)} 🪙"],
                ["characters", money(stats.collection)],
                ["level", f"{stats.level} · {money(stats.exp)} xp"],
                ["pulls", f"{money(stats.pulls)} ({money(stats.high_pulls)} high)"],
                ["streak", f"{stats.streak} days (best {stats.best_streak})"],
                [
                    "rank",
                    f"#{money(stats.rank_coins)} of {money(stats.total_players)}"
                    if stats.rank_coins
                    else "unranked",
                ],
            ],
            compact=True,
            bordered=False,
        )
        .footer("👑 premium active" if premium else "premium: off — /premium")
    )
    await card(
        message,
        ctx,
        builder=builder,
        html=f"{money(coins)} 🪙 · harem {money(value)} · level {stats.level}",
        buttons=[
            [
                callback("🎁 daily", cb("eco", "daily")),
                callback("💼 work", cb("eco", "work")),
                callback("🎡 spin", cb("eco", "spin")),
            ],
            [
                callback("🎴 collection", cb("col", "open")),
                callback("🏪 market", cb("mkt", "open")),
            ],
        ],
    )


@router.message(Command("daily", "login"))
async def daily(
    message: Message,
    ctx: AppContext,
    session: Any,
    command: CommandObject,
    user: Any,
    access: Access,
) -> None:
    await do_daily(message, ctx, session, user_id=access.user_id, offset=_offset(command))


def _offset(command: CommandObject | None) -> int:
    """``/daily +5:30`` style offset, so a "daily at midnight" is the *player's* midnight."""
    raw = (command.args or "") if command else ""
    import re

    match = re.search(r"([+-]\d{1,2})(?::(\d{2}))?", raw)
    if not match:
        return 0
    hours = int(match.group(1))
    minutes = int(match.group(2) or 0)
    return max(-12, min(14, hours + (minutes / 60 if minutes else 0)))


async def do_daily(
    event: Message | CallbackQuery, ctx: AppContext, session: Any, *, user_id: int, offset: int = 0
) -> None:
    premium = await ctx.premium.is_premium(session, user_id)
    try:
        result = await ctx.economy.daily(
            session, user_id, utc_offset_hours=int(offset), premium=premium
        )
    except AlreadyClaimed as exc:
        state = await ctx.progress.streak(session, user_id)
        left = ctx.economy.seconds_until_reset(utc_offset_hours=offset)
        await _respond(
            event,
            ctx,
            f"🎁 {exc.user_message} Resets in {left // 3600}h {left % 3600 // 60}m (streak {state.current}).",
        )
        return
    bonus = f"\n🎁 item: <b>{result.bonus_item}</b>" if result.bonus_item else ""
    await _respond(
        event,
        ctx,
        (
            f"🎁 <b>+{money(result.amount)} 🪙</b> · balance {money(result.balance)}\n"
            f"streak <b>{result.streak}</b> days (best {result.best}) × {result.multiplier:g} multiplier"
            + (f" · {result.freezes_used} freeze(s) used" if result.freezes_used else "")
            + bonus
        ),
    )


@router.callback_query(F.data == "eco:daily")
async def daily_button(
    callback_query: CallbackQuery, ctx: AppContext, session: Any, access: Access
) -> None:
    await do_daily(callback_query, ctx, session, user_id=access.user_id)


@router.message(Command("work"))
async def work(message: Message, ctx: AppContext, session: Any, access: Access) -> None:
    await do_work(message, ctx, session, user_id=access.user_id)


@router.callback_query(F.data == "eco:work")
async def work_button(
    callback_query: CallbackQuery, ctx: AppContext, session: Any, access: Access
) -> None:
    await do_work(callback_query, ctx, session, user_id=access.user_id)


async def do_work(
    event: Message | CallbackQuery, ctx: AppContext, session: Any, *, user_id: int
) -> None:
    try:
        result = await ctx.economy.work(
            session, user_id, premium=await ctx.premium.is_premium(session, user_id)
        )
    except CooldownActive as exc:
        await _respond(
            event,
            ctx,
            f"💼 you just worked — back in {exc.retry_after or 0 // 60}m. (their /work had no cooldown at all, which is why coins meant nothing)",
        )
        return
    await _respond(
        event,
        ctx,
        f"💼 <b>{result.get('label', 'a job')}</b> paid <b>+{money(result.get('amount', 0))} 🪙</b> · balance {money(result.get('balance', 0))}",
    )


@router.message(Command("spin", "wheel"))
async def spin(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, access: Access
) -> None:
    """Daily free spin, and ``/spin <amount>`` to gamble coins (their ``/spin``)."""
    args = Args.of(command)
    amount = args.amount
    if amount:
        # ``free=False`` skips the daily claim and stakes the amount instead; the
        # service owns the win/lose math and the payout, the handler only renders it.
        try:
            result = await ctx.economy.spin(session, access.user_id, free=False)
        except (NotEnoughFunds, AlreadyClaimed, WaifuError) as exc:
            await refuse(message, exc.user_message)
            return
    else:
        try:
            result = await ctx.economy.spin(session, access.user_id)
        except AlreadyClaimed:
            await text(
                message,
                ctx,
                f"🎡 already spun today. Bet instead: <code>/spin {money(ctx.settings.spin_min)}</code>–{money(ctx.settings.spin_max)} 🪙.",
            )
            return
    await _respond(event=message, ctx=ctx, html=_spin_text(result, ctx))


def _spin_text(result: dict[str, Any], ctx: AppContext) -> str:
    amount = int(result.get("amount", 0) or 0)
    lucky = " 🍀 lucky day" if result.get("lucky") else ""
    item = f"\n🎁 dropped: <b>{result['item']}</b>" if result.get("item") else ""
    verb = "won" if amount >= 0 else "lost"
    return f"🎡 the wheel lands on <b>{money(abs(amount))} 🪙</b>{lucky}\n{verb} · balance {money(result.get('balance', 0))}{item}"


@router.callback_query(F.data == "eco:spin")
async def spin_button(
    callback_query: CallbackQuery, ctx: AppContext, session: Any, access: Access
) -> None:
    try:
        result = await ctx.economy.spin(session, access.user_id)
    except AlreadyClaimed:
        await note(callback_query, "spun already today — /spin <amount> to gamble", alert=True)
        return
    # The toast carries the result; the card is edited in place so the chat stays clean.
    await edit(
        callback_query,
        ctx,
        builder=RichMessageBuilder()
        .heading("🎡 the wheel", size=2)
        .paragraph(html=_spin_text(result, ctx)),
        html=_spin_text(result, ctx),
        buttons=[[callback("🎡 gamble 100", cb("eco", "spinbet"))]],
    )
    await note(callback_query, f"wheel: {money(result.get('amount', 0))} 🪙")


@router.message(Command("rob", "steal"))
async def rob(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, access: Access
) -> None:
    await do_rob(message, ctx, session, attacker=access.user_id, args=Args.of(command))


async def do_rob(
    message: Message, ctx: AppContext, session: Any, *, attacker: int, args: Args
) -> None:
    target = await resolve_user(session, message, args.first)
    if target is None:
        await text(
            message, ctx, "Rob whom? Reply to their message or use <code>/rob @username</code>."
        )
        return
    if target == attacker:
        await text(message, ctx, "You cannot rob yourself — that is not how theft works.")
        return
    try:
        result = await ctx.economy.steal(
            session,
            attacker,
            target,
            attacker_premium=await ctx.premium.is_premium(session, attacker),
            target_premium=await ctx.premium.is_premium(session, target),
        )
    except (CooldownActive, WaifuError) as exc:
        await refuse(message, exc.user_message)
        return
    if result.get("ok"):
        await text(
            message,
            ctx,
            f"🕵️ you took <b>{money(result.get('amount', 0))} 🪙</b> from {mention(target)}. Balance {money(result.get('balance', 0))}.",
        )
        await ctx.react(message.chat.id, message.message_id, "😱")
        return
    if result.get("shielded"):
        await text(
            message,
            ctx,
            f"🛡️ {mention(target)} had a shield up. Your reputation costs {money(result.get('amount', 0))} 🪙.",
        )
        return
    await text(
        message,
        ctx,
        f"❌ {mention(target)}'s guard caught you: you paid {money(result.get('amount', 0))} 🪙.",
    )


@router.message(Command("bomb", "attack"))
async def bomb(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, access: Access
) -> None:
    args = Args.of(command)
    target = await resolve_user(session, message, args.first)
    if target is None:
        await text(message, ctx, "Bomb whom? Reply to someone or <code>/bomb @username</code>.")
        return
    try:
        result = await ctx.economy.bomb(session, access.user_id, target)
    except (CooldownActive, WaifuError) as exc:
        await refuse(message, exc.user_message)
        return
    if result.get("ok"):
        await text(
            message,
            ctx,
            f"💣 {mention(target)} loses <b>{money(result.get('xp', 0))} xp</b>. Their shields did not save them this time.",
        )
    else:
        await text(
            message, ctx, f"🛡️ {mention(target)} was shielded. Nothing gained, cooldown spent."
        )


@router.message(Command("give", "pay", "send", "transfer"))
async def give(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, access: Access
) -> None:
    args = Args.of(command)
    if len(args.words) < 2:
        await text(
            message, ctx, "Usage: <code>/give @name 5000</code> (or <code>/give @name 5k</code>)."
        )
        return
    target = await resolve_user(session, message, args.first)
    amount = Args(raw=" ".join(args.words[1:]), words=tuple(args.words[1:])).amount
    if target is None or not amount:
        await text(message, ctx, "I need a player and an amount — <code>/give @name 5000</code>.")
        return
    if amount <= 0 or amount > 100_000_000:
        await text(message, ctx, "That amount is out of range.")
        return
    try:
        sent, received = await ctx.economy.transfer(
            session,
            sender_id=access.user_id,
            receiver_id=target,
            amount=int(amount),
            reason="gift",
            reference=f"give:{message.message_id}",
            tax_percent=ctx.settings.gift_tax_percent,
        )
    except NotEnoughFunds as exc:
        await refuse(message, exc.user_message)
        return
    await text(
        message,
        ctx,
        f"🤝 {mention(access.user_id)} → {mention(target)}: <b>{money(amount)} 🪙</b>"
        + (
            f" (recipient received {money(received.balance)})"
            if ctx.settings.gift_tax_percent
            else ""
        )
        + f"\nbalance {money(sent.balance)} 🪙",
    )


@router.message(Command("history", "log", "ledger"))
async def history(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, access: Access
) -> None:
    args = Args.of(command)
    limit = max(5, min(25, args.integer or 12))
    rows = await ctx.economy.history(session, access.user_id, limit=limit)
    if not rows:
        await text(message, ctx, "No transactions yet.")
        return
    table = [["when", "reason", "change", "after"]]
    for row in rows:
        delta = int(getattr(row, "delta", 0))
        sign = "🟢" if delta >= 0 else "🔻"
        table.append(
            [
                str(getattr(row, "created_at", ""))[:16],
                str(getattr(row, "reason", "?")),
                f"{sign} {money(delta)}",
                money(int(getattr(row, "balance_after", 0))),
            ]
        )
    builder = (
        RichMessageBuilder()
        .heading("🧾 wallet history", size=2)
        .table(table, compact=True)
        .footer("Every coin the bot has ever moved for you is in this table.")
    )
    await card(
        message, ctx, builder=builder, html="\n".join(f"{row[1]}: {row[2]}" for row in table[1:])
    )


@router.message(Command("paywall", "boost"))
async def boost(message: Message, ctx: AppContext, session: Any, access: Access) -> None:
    """Premium perks + the boost rebate (``/boost`` parity with their boost rewards)."""
    info = await ctx.premium.premium(session, access.user_id)
    pending = await ctx.premium.pending_boosts(session, access.user_id, message.chat.id)
    rows: list[list[str]] = [["perk", "detail"]]
    for key, value in (info.perks or {}).items():
        rows.append([str(key), str(value)])
    if not info.is_active:
        rows.append(["status", "not active — /premium"])
    builder = (
        RichMessageBuilder()
        .heading("⚡ premium & boosts", size=2)
        .table(rows, compact=True, bordered=False)
        .divider()
        .paragraph(
            html=(
                f"Boosting this chat banks you coins — {money(pending)} boost(s) ready to redeem."
                if pending
                else "No unclaimed boosts here yet: boost the chat from Telegram's menu, then /boost."
            )
        )
    )
    buttons = [[callback("🎁 redeem boosts", cb("prem", "boosts"))]] if pending else None
    await card(message, ctx, builder=builder, html="premium & boosts", buttons=buttons)


@router.callback_query(F.data == "prem:boosts")
async def boost_redeem(
    callback_query: CallbackQuery, ctx: AppContext, session: Any, access: Access
) -> None:
    if callback_query.message is None:
        await callback_query.answer()
        return
    result = await ctx.premium.redeem_boost_rewards(
        session, access.user_id, callback_query.message.chat.id
    )
    await note(
        callback_query,
        f"redeemed {money(result.get('coins', 0))} 🪙 for {result.get('boosts', 0)} boost(s)",
        alert=True,
    )


@router.message(Command("integrity", "audit"))
async def integrity(message: Message, ctx: AppContext, session: Any, access: Access) -> None:
    """``/integrity`` — a player audits their own wallet: the invariant, on demand."""
    problems = await ctx.economy.integrity(session)
    total = sum(int(row.delta) for row in await _all_rows(session, access.user_id))
    balance = await ctx.economy.balance(session, access.user_id)
    lines = [
        f"balance {money(balance)} 🪙",
        f"ledger sum {money(total)} 🪙",
        "✅ they match" if balance == total else "❌ drift — tell the owner",
    ]
    if problems:
        lines.append(
            f"⚠️ {len(problems)} account(s) on this instance disagree (first: {problems[0]})"
        )
    await text(message, ctx, "\n".join(lines))


async def _all_rows(session: Any, user_id: int) -> list[Any]:
    from sqlalchemy import select

    from waifu.db.models import Transaction

    return list(
        (await session.execute(select(Transaction).where(Transaction.user_id == user_id))).scalars()
    )


async def _respond(event: Message | CallbackQuery, ctx: AppContext, html: str) -> None:
    if isinstance(event, CallbackQuery):
        await note(event, html[:200], alert=True)
        return
    await text(event, ctx, html)


# ``/pay`` on the legacy bot paid for premium with coins; keep the alias honest.
@router.message(Command("topup", "deposit"))
async def topup(message: Message, ctx: AppContext) -> None:
    await text(
        message,
        ctx,
        "Coins come from summoning, dailies and the market — or from Telegram Stars in /premium.",
    )
