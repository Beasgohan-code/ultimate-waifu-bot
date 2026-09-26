"""``/pull``, ``/hclaim``, ``/guarantee``, ``/chances``, ``/history``, ``/verify``.

The summon itself lives in :class:`waifu.services.gacha.GachaService`; this module only
decides how a result looks and which buttons follow it.

Two things the reference bot could not do and these commands exist to fix:

* **provability.** Their RNG was ``random.choice`` with no record, so "the bot rigged it
  for me" was unanswerable. Every roll here is drawn from a per-server seed that was
  committed *before* the pull, and :meth:`GachaService.verify` replays a roll from that
  commitment — which is what ``/verify`` shows.
* **cooldowns.** They slept on a ``asyncio.sleep`` per user, so a restart dropped every
  timer. The service takes a cooldown key and the DB owns the timestamp; nothing to lose.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

from waifu.enums import Rarity
from waifu.errors import AlreadyClaimed, CooldownActive, NotEnoughFunds, WaifuError
from waifu.plugins._kit import (
    Args,
    RichMessageBuilder,
    bar,
    callback,
    card,
    cb,
    edit,
    money,
    note,
    refuse,
    text,
)

if TYPE_CHECKING:  # pragma: no cover
    from waifu.core.access import Access
    from waifu.core.context import AppContext

router = Router(name="gacha")

BATCHES = (1, 3, 5, 10)


@router.message(Command("pull", "summonwaifu", "gacha"))
async def pull(
    message: Message,
    ctx: AppContext,
    session: Any,
    command: CommandObject,
    user: Any,
    access: Access,
) -> None:
    args = Args.of(command)
    if not user:
        return
    batch = args.integer or 1
    if batch not in BATCHES and batch > 10:
        await text(message, ctx, f"Pick 1, 3, 5 or 10 — not {batch}.")
        return
    await do_pull(message, ctx, session, user_id=access.user_id, batch=max(1, min(10, batch)))


async def do_pull(
    event: Message | CallbackQuery,
    ctx: AppContext,
    session: Any,
    *,
    user_id: int,
    batch: int,
    premium: bool = False,
) -> None:
    try:
        result = await ctx.gacha.pull(session, user_id, batch=batch, premium=premium)
    except (NotEnoughFunds, AlreadyClaimed, CooldownActive, WaifuError) as exc:
        await _failure(event, ctx, exc)
        return
    await render_pull(event, ctx, session, result=result, user_id=user_id)


def _claim_footer(result: Any) -> str:
    """The counters ``hclaim_command`` printed under the card (premium tag + Claims Today)."""
    bits = []
    if getattr(result, "premium_claim", False):
        bits.append("👑 <b>Premium Claim Active</b> (high-edition chance boosted)")
    if getattr(result, "claims_limit", 0):
        bits.append(f"📊 <b>Claims Today:</b> {result.claims_today}/{result.claims_limit}")
    return " · ".join(bits)


async def render_pull(
    event: Message | CallbackQuery,
    ctx: AppContext,
    session: Any,
    *,
    result: Any,
    user_id: int,
    footer: str = "",
) -> None:
    rolls = list(result.rolls)
    best = (
        max(rolls, key=lambda roll: (int(roll.rarity), int(roll.roll_value or 0)))
        if rolls
        else None
    )
    builder = RichMessageBuilder()
    builder.heading(_headline(rolls, best), size=1)
    if footer:
        builder.paragraph(html=footer)
    if best is not None and getattr(best, "image", ""):
        builder.photo(
            str(best.image),
            caption=f"{best.name} · {best.rarity.emoji if hasattr(best.rarity, 'emoji') else ''}",
        )
    rows = [["character", "rarity", "result", "power"]]
    for roll in rolls:
        badge = roll.rarity.badge if hasattr(roll.rarity, "badge") else str(roll.rarity)
        outcome = "NEW" if not roll.is_dupe else f"+{money(roll.payout)} 🪙 dupe"
        rows.append(
            [
                f"<b>{roll.name}</b>",
                badge,
                ("✨ " if not roll.is_dupe else "🔁 ") + outcome,
                str(roll.stat_power or ""),
            ]
        )
    builder.table(rows, compact=True)
    summary = [
        ["spent", f"{money(result.spent)} 🪙"],
        ["new characters", str(result.new_count)],
        ["dupe payout", f"{money(result.dupe_payout)} 🪙"],
        ["balance", f"{money(result.balance)} 🪙"],
    ]
    if result.commitment:
        summary.append(["roll #", f"{result.sequence} · <code>{result.commitment[:16]}…</code>"])
    builder.divider()
    builder.table(summary, compact=True, bordered=False)
    if result.pity is not None:
        builder.line(
            f"pity: rare {bar(result.pity.rare, 5)} · high {bar(result.pity.high, ctx.settings.pity_high_after)} · celestial {bar(result.pity.celestial, ctx.settings.pity_celestial_after)}"
        )
    buttons = [
        [
            callback("🎰 again", cb("gacha", "pull", len(rolls))),
            callback("🎟️ 10", cb("gacha", "pull", 10)),
            callback("🎴 collection", cb("col", "open")),
        ]
    ]
    html = (
        "\n".join(
            f"• {roll.name} — {roll.rarity.badge if hasattr(roll.rarity, 'badge') else roll.rarity} {'NEW' if not roll.is_dupe else f'dupe +{money(roll.payout)}'}"
            for roll in rolls
        )
        or "no results"
    )
    if isinstance(event, CallbackQuery):
        await edit(
            event,
            ctx,
            builder=builder,
            html=html,
            buttons=buttons,
            photo=(str(best.image) if best is not None and getattr(best, "image", "") else None),
        )
    else:
        await card(
            event,
            ctx,
            builder=builder,
            html=html,
            buttons=buttons,
            photo=(str(best.image) if best is not None and getattr(best, "image", "") else None),
        )
    if best is not None and int(best.rarity) >= int(Rarity.VALENTINE):
        # A high-tier pull gets a reaction on the message (Bot API 9.3) instead of the
        # old bot's CAPS-lock celebration; ctx.react no-ops when the chat has no
        # reaction rights or the API server predates 9.3, so this needs no branching.
        message = event.message if isinstance(event, CallbackQuery) else event
        if message is not None:
            await ctx.react(message.chat.id, message.message_id, "🎉", big=True)


def _headline(rolls: list[Any], best: Any) -> str:
    if not rolls:
        return "Nothing came back"
    if best is None:
        return f"{len(rolls)} summons"
    if int(best.rarity) >= int(Rarity.CELESTIAL):
        return f"🌌 {best.name} — {money(len(rolls))}-pull jackpot"
    if len(rolls) == 1:
        return f"{best.rarity.emoji if hasattr(best.rarity, 'emoji') else ''} {best.name}"
    news = sum(1 for roll in rolls if not roll.is_dupe)
    return f"{len(rolls)} summons · {news} new · best {best.name}"


async def _failure(event: Message | CallbackQuery, ctx: AppContext, exc: Exception) -> None:
    """Map service errors to a toast/message instead of an internal-error apology."""
    if isinstance(exc, CooldownActive):
        await refuse(event, f"⏳ {exc.user_message}")
        return
    if isinstance(exc, (AlreadyClaimed, WaifuError)):
        await refuse(event, exc.user_message)
        return
    raise exc


@router.callback_query(F.data.startswith("gacha:pull:"))
async def pull_button(
    callback_query: CallbackQuery, ctx: AppContext, session: Any, access: Access
) -> None:
    parts = (callback_query.data or "").split(":")
    batch = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
    try:
        result = await ctx.gacha.pull(session, access.user_id, batch=max(1, min(10, batch)))
    except (NotEnoughFunds, AlreadyClaimed, CooldownActive, WaifuError) as exc:
        await _failure(callback_query, ctx, exc)
        return
    await render_pull(callback_query, ctx, session, result=result, user_id=access.user_id)


@router.message(Command("hclaim", "freepull", "dailyclaim"))
async def hclaim(
    message: Message, ctx: AppContext, session: Any, user: Any, access: Access
) -> None:
    """The free daily claim (their ``/hclaim``), with the premium double-claim bonus."""
    if not user:
        return
    premium = await ctx.premium.is_premium(session, access.user_id)
    try:
        result = await ctx.gacha.free_claim(session, access.user_id, premium=premium)
    except AlreadyClaimed as exc:
        await text(message, ctx, f"🎟️ {exc.user_message} Next one in <b>{_until_reset(ctx)}</b>.")
        return
    except WaifuError as exc:
        await text(message, ctx, f"❌ {exc.user_message}")
        return
    await render_pull(
        message,
        ctx,
        session,
        result=result,
        user_id=access.user_id,
        footer=_claim_footer(result),
    )


@router.callback_query(F.data == "gacha:claim")
async def claim_button(
    callback_query: CallbackQuery, ctx: AppContext, session: Any, access: Access
) -> None:
    try:
        result = await ctx.gacha.free_claim(
            session, access.user_id, premium=await ctx.premium.is_premium(session, access.user_id)
        )
    except (NotEnoughFunds, AlreadyClaimed, CooldownActive, WaifuError) as exc:
        await _failure(callback_query, ctx, exc)
        return
    await render_pull(
        callback_query,
        ctx,
        session,
        result=result,
        user_id=access.user_id,
        footer=_claim_footer(result),
    )


def _until_reset(ctx: AppContext) -> str:
    from waifu.utils.time import human_delta

    seconds = ctx.gacha.seconds_until_reset(utc_offset_hours=0)
    return human_delta(int(seconds))


@router.message(Command("guarantee", "pity"))
async def guarantee(message: Message, ctx: AppContext, session: Any, access: Access) -> None:
    state = await ctx.gacha.pity(session, access.user_id)
    settings = ctx.settings
    builder = (
        RichMessageBuilder()
        .heading("🛡️ Guarantees", size=2)
        .table(
            [
                ["rare or better", f"{bar(state.rare, 5)} {state.rare}/5"],
                [
                    "high tier (💝+)",
                    f"{bar(state.high, settings.pity_high_after)} {state.high}/{settings.pity_high_after}",
                ],
                [
                    "🌌 celestial",
                    f"{bar(state.celestial, settings.pity_celestial_after)} {state.celestial}/{settings.pity_celestial_after}",
                ],
                ["pulls", money(state.pulls_total)],
            ],
            compact=True,
            bordered=False,
        )
        .paragraph(
            html=(
                "Counters reset on the payout, not the pull, and they are per account — "
                "not per session, so a restart cannot lose your progress (theirs could)."
            )
        )
    )
    await card(
        message,
        ctx,
        builder=builder,
        html=f"rare {state.rare}/5 · high {state.high}/{settings.pity_high_after} · celestial {state.celestial}/{settings.pity_celestial_after}",
    )


@router.message(Command("chances", "rates", "odds"))
async def chances(message: Message, ctx: AppContext, session: Any, access: Access) -> None:
    """Both tables (summon and free claim), because hiding one is how you get banned."""
    premium = await ctx.premium.is_premium(session, access.user_id)
    pull_odds = await ctx.gacha.odds(session, claim=False, premium=premium)
    claim_odds = await ctx.gacha.odds(session, claim=True, premium=premium)
    by_claim = {int(rarity): value for rarity, value in claim_odds}
    rows = [["tier", "summon", "free claim", "price"]]
    for rarity, value in pull_odds:
        rows.append(
            [
                f"{rarity.badge} {rarity.label}",
                f"{value:.2f}%",
                f"{by_claim.get(int(rarity), 0.0):.2f}%",
                money(rarity.base_price),
            ]
        )
    builder = (
        RichMessageBuilder()
        .heading("📊 Drop rates", size=2)
        .table(rows, compact=True)
        .divider()
        .paragraph(
            html=(
                f"Premium: +{ctx.settings.premium_claim_boost_percent}% weight toward high tiers, double /hclaim, no spawn cap.\n"
                "Odds are read live from the database — <code>/setchance</code> changes them for everyone at once."
            )
        )
    )
    await card(
        message,
        ctx,
        builder=builder,
        html="\n".join(
            f"{rarity.badge} {rarity.label}: {value:.2f}%" for rarity, value in pull_odds
        ),
        buttons=[
            [
                callback("🎴 my pity", cb("gacha", "pity")),
                callback("🎰 pull 1", cb("gacha", "pull", 1)),
            ]
        ],
    )


@router.callback_query(F.data == "gacha:pity")
async def pity_button(
    callback_query: CallbackQuery, ctx: AppContext, session: Any, access: Access
) -> None:
    state = await ctx.gacha.pity(session, access.user_id)
    await note(
        callback_query,
        f"rare {state.rare}/5 · high {state.high}/{ctx.settings.pity_high_after} · celestial {state.celestial}/{ctx.settings.pity_celestial_after}",
        alert=True,
    )


@router.message(Command("rolls", "pullhistory"))
async def history(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, access: Access
) -> None:
    args = Args.of(command)
    rows = await ctx.gacha.history(
        session, access.user_id, limit=min(20, max(5, args.integer or 10))
    )
    if not rows:
        await text(message, ctx, "No summons yet — <code>/pull</code> fixes that.")
        return
    table = [["#", "character", "rarity", "roll", "when"]]
    for row in rows:
        rarity = Rarity.from_value(int(getattr(row, "rarity_id", 1) or 1))
        table.append(
            [
                str(getattr(row, "sequence", getattr(row, "id", "?"))),
                f"<b>{getattr(row, 'name', '?')}</b>",
                rarity.emoji,
                f"<code>{str(getattr(row, 'roll_value', ''))[:8]}</code>",
                str(getattr(row, "created_at", ""))[:16],
            ]
        )
    builder = (
        RichMessageBuilder()
        .heading("🧾 Your recent summons", size=2)
        .table(table, compact=True)
        .footer("/verify <roll #> replays one of these against the committed seed.")
    )
    await card(
        message, ctx, builder=builder, html="\n".join(f"{row[0]}. {row[1]}" for row in table[1:])
    )


@router.message(Command("verify", "checkroll", "proof"))
async def verify(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, access: Access
) -> None:
    args = Args.of(command)
    sequence = args.integer
    if not sequence:
        await text(message, ctx, "Usage: <code>/verify &lt;roll number&gt;</code> (see /history).")
        return
    try:
        proof = await ctx.gacha.verify(session, access.user_id, sequence)
    except WaifuError as exc:
        await refuse(message, exc.user_message)
        return
    builder = (
        RichMessageBuilder()
        .heading("🔍 Roll verification", size=2)
        .table(
            [
                ["roll", str(proof.get("sequence"))],
                ["server seed", f"<code>{str(proof.get('seed', ''))[:24]}…</code>"],
                ["commitment", f"<code>{str(proof.get('commitment', ''))[:24]}…</code>"],
                ["draw", f"<code>{proof.get('draw')}</code>"],
                ["tier", str(proof.get("rarity", ""))],
                ["character", str(proof.get("name", ""))],
            ],
            compact=True,
            bordered=False,
        )
        .paragraph(
            html=(
                "The commitment is ``sha256(seed)`` and was stored <i>before</i> the roll; the draw is "
                "``sha256(seed:sequence)`` mapped onto the tier table. Recompute it yourself — if the "
                "tier matches, the pull could not have been changed after the fact."
            )
        )
    )
    await card(
        message,
        ctx,
        builder=builder,
        html=f"roll {proof.get('sequence')} → {proof.get('name')} (draw {proof.get('draw')})",
    )


@router.message(Command("pulls", "mypulls"))
async def pulls_stat(message: Message, ctx: AppContext, session: Any, access: Access) -> None:
    stats = await ctx.gacha.player_stats(session, access.user_id)
    table = [
        [key.replace("_", " ").title(), money(value) if isinstance(value, int) else f"{value:.2f}"]
        for key, value in stats.items()
    ]
    await card(
        message,
        ctx,
        builder=RichMessageBuilder()
        .heading("📈 your summon stats", size=2)
        .table(table, compact=True, bordered=False),
        html="\n".join(f"{row[0]}: {row[1]}" for row in table),
    )


@router.callback_query(F.data.startswith("gacha:noop"))
async def noop(callback_query: CallbackQuery) -> None:
    await callback_query.answer(cache_time=3600)


@router.message(Command("claimlist", "claimable"))
async def claimlist(message: Message, ctx: AppContext, session: Any) -> None:
    """Which tiers /hclaim can roll right now — their bot called this ``/claimlist``.

    Worth its own command because the two ladders differ: a tier can be rare in the gacha
    and still be the best free claim today, which is the trade players actually plan around.
    """
    from waifu.db.repo import characters as char_repo

    claims = dict(await char_repo.claim_chances(session) or [])
    rows = [["tier", "claim weight", "base price"]]
    for rarity, percent in sorted(claims.items(), key=lambda item: int(item[0].value)):
        rows.append(
            [f"{rarity.badge} {rarity.label}", f"{float(percent):.2f}%", money(rarity.base_price)]
        )
    builder = RichMessageBuilder().heading("🎯 claimable tiers", size=2)
    builder.table(rows, compact=True) if len(rows) > 1 else builder.paragraph(
        html="<i>no tier is enabled for /hclaim — an admin sets that with /setclaim</i>"
    )
    builder.divider()
    odds = await ctx.gacha.odds(session, claim=True)
    builder.line(
        "your claim odds: " + ", ".join(f"{rarity.label} {pct:.1f}%" for rarity, pct in odds[:5])
    )
    await card(
        message,
        ctx,
        builder=builder,
        html="\n".join(" ".join(str(col) for col in row) for row in rows[1:]),
    )
