"""``/code`` (redeem) and the admin code desk (``/addcode``, ``/delcode``, ``/codes``).

Their redeem codes were a ``redeem_codes`` table read with
``SELECT * WHERE code=?`` and no per-user claim record, so the same code could be
redeemed twice in parallel — and codes were never revoked, so a leaked one in a
screenshot stayed live forever. Here a claim is one row keyed
``(code, user)`` with the remaining-uses counter updated in the same statement, and
``/delcode`` revokes instead of deleting (the claim history survives for support).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from waifu.enums import Rarity
from waifu.errors import AlreadyClaimed, NotFound, WaifuError
from waifu.plugins._kit import Args, RichMessageBuilder, card, money, refuse, staff_of, text

if TYPE_CHECKING:  # pragma: no cover
    from waifu.core.access import Access
    from waifu.core.context import AppContext

router = Router(name="codes")


@router.message(Command("code", "redeem", "redeemcode"))
async def redeem(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, access: Access
) -> None:
    args = Args.of(command)
    if not args.raw:
        await text(message, ctx, "Got a code? <code>/code SPRING2026</code>")
        return
    premium = await ctx.premium.is_premium(session, access.user_id)
    try:
        result = await ctx.codes.redeem(session, access.user_id, args.raw, premium=premium)
    except (AlreadyClaimed, NotFound, WaifuError) as exc:
        await refuse(message, exc.user_message)
        return
    lines = [f"🎟️ code <b>{result.code}</b> redeemed"]
    if result.coins:
        lines.append(f"coins: <b>+{money(result.coins)} 🪙</b>")
    if result.character:
        character = result.character
        name = getattr(character, "name", None) or (
            character.get("name") if isinstance(character, dict) else None
        )
        tier = Rarity.from_value(
            int(
                getattr(character, "rarity_id", 1)
                or (character.get("rarity_id", 1) if isinstance(character, dict) else 1)
            )
        )
        lines.append(f"character: <b>{name}</b> {tier.badge}")
    if result.premium_hours:
        lines.append(f"premium: <b>+{result.premium_hours}h</b>")
    lines.append(f"balance: {money(result.balance)} 🪙")
    await text(message, ctx, "\n".join(lines))


@router.message(Command("addcode", "newcode", "gen", "gencode"))
async def addcode(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, access: Access
) -> None:
    """/addcode CODE coins=5000 uses=25 hours=72 premium=48 character=42"""
    staff_of(access)
    args = Args.of(command)
    if not args.words:
        await text(message, ctx, "Usage: <code>/addcode SPRING coins=5000 uses=25 hours=72</code>")
        return
    code = args.first.upper()
    options = {}
    character_id = None
    for token in args.words[1:]:
        if "=" not in token:
            continue
        key, raw = token.split("=", 1)
        value = "".join(ch for ch in raw if ch.isdigit())
        if not value:
            continue
        if key.lower() == "character":
            character_id = int(value)
        else:
            options[key.lower()] = int(value)
    try:
        created = await ctx.codes.create(
            session,
            created_by=access.user_id,
            code=code,
            coins=options.get("coins", 0),
            reward=options.get("reward", 0),
            character_id=character_id,
            premium_hours=options.get("premium", 0),
            uses=options.get("uses", 1),
            hours=options.get("hours", 72),
            note=" ".join(args.raw.split("=")[1:])[:80],
        )
    except WaifuError as exc:
        await refuse(message, exc.user_message)
        return
    await text(
        message,
        ctx,
        f"🎟️ code <b>{created.code}</b> armed: {money(int(created.coins or 0))} 🪙"
        + (f" + {created.premium_hours}h premium" if getattr(created, "premium_hours", 0) else "")
        + f" · {created.max_uses} use(s) · expires in {created.hours_valid if hasattr(created, 'hours_valid') else 72}h",
    )


@router.message(Command("codes", "listcodes"))
async def listcodes(message: Message, ctx: AppContext, session: Any, access: Access) -> None:
    staff_of(access)
    rows = await ctx.codes.list(session, active_only=True, limit=20)
    stats = await ctx.codes.stats(session)
    builder = RichMessageBuilder().heading(f"🎟️ live codes — {len(rows)}", size=2)
    if rows:
        table = [["code", "reward", "uses", "claims", "expires"]]
        for row in rows:
            reward = f"{money(int(row.coins or 0))} 🪙"
            if getattr(row, "character_id", None):
                reward += f" · #{row.character_id}"
            table.append(
                [
                    f"<code>{row.code}</code>",
                    reward,
                    f"{int(getattr(row, 'uses', 0))}/{int(getattr(row, 'max_uses', 1) or 1)}",
                    str(int(getattr(row, "max_uses", 0) or 0)),
                    str(getattr(row, "expires_at", ""))[:16],
                ]
            )
        builder.table(table, compact=True)
    builder.divider()
    builder.line(
        " · ".join(f"{key} {money(value)}" for key, value in stats.items()) or "no codes yet"
    )
    await card(
        message,
        ctx,
        builder=builder,
        html="\n".join(f"{row.code}: {row.coins}🪙" for row in rows) or "no live codes",
    )


@router.message(Command("delcode", "revokecode"))
async def delcode(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, access: Access
) -> None:
    staff_of(access)
    args = Args.of(command)
    if not args.first:
        await text(message, ctx, "Usage: <code>/delcode &lt;CODE&gt;</code>")
        return
    try:
        await ctx.codes.revoke(session, args.first.upper(), actor_id=access.user_id)
    except NotFound as exc:
        await text(message, ctx, exc.user_message)
        return
    await text(
        message,
        ctx,
        f"🚫 <code>{args.first.upper()}</code> revoked — claims stay on record for support.",
    )


@router.message(Command("codeclaims"))
async def codeclaims(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, access: Access
) -> None:
    """Who redeemed what — the answer to 'the code was shared, take it back'."""
    staff_of(access)
    args = Args.of(command)
    if not args.first:
        await text(message, ctx, "Usage: <code>/codeclaims &lt;CODE&gt;</code>")
        return
    rows = await ctx.codes.claims(session, args.first.upper())
    if not rows:
        await text(message, ctx, "No claims for that code.")
        return
    lines = [
        f"• {mention_of(int(getattr(row, 'user_id', 0)))} — {str(getattr(row, 'claimed_at', ''))[:16]}"
        for row in rows[:25]
    ]
    await text(message, ctx, f"🧾 {len(rows)} claim(s):\n" + "\n".join(lines))


def mention_of(user_id: int) -> str:
    from waifu.plugins._kit import mention

    return mention(user_id)
