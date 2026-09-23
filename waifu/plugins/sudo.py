"""Admin desk: ``/give``, ``/take``, ``/setbal``, ``/sudo``, ``/broadcast``, ``/maint``, ``/doctor``.

The legacy bot's ``sudo_admins`` table made *any* listed id a god: they could mint
coins, change drop rates and unban themselves, with no separation of duties and one
audit line. Here there are two axes — role (owner / admin / moderator) and per-command
permission — resolved in :mod:`waifu.core.access`, and every entry point in this module
writes an audit row with the acting id, because "who set Common to 90%?" needs an answer.

Money commands use the same ledger API as the players do, so an admin grant is a
transaction like any other (``/integrity`` still balances). That is the property the
old bot lacked: ``/gavecoins`` wrote ``balance = balance + n`` directly and left no trace.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from waifu.enums import Role
from waifu.plugins._kit import (
    Args,
    RichMessageBuilder,
    card,
    mention,
    money,
    refuse,
    resolve_user,
    staff_of,
    text,
)

if TYPE_CHECKING:  # pragma: no cover
    from waifu.core.access import Access
    from waifu.core.context import AppContext

router = Router(name="sudo")


@router.message(Command("givecoins", "addcoins", "givemoney", "addmoney"))
async def givecoins(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, access: Access
) -> None:
    staff_of(access)
    args = Args.of(command)
    target = await resolve_user(session, message, args.first)
    amount = Args(raw=" ".join(args.words[1:]), words=tuple(args.words[1:])).amount
    if target is None or not amount:
        await text(message, ctx, "Usage: <code>/givecoins @player 100000</code>")
        return
    result = await ctx.economy.admin_grant(
        session, target, int(amount), actor=access.user_id, reason=f"sudo by {access.user_id}"
    )
    await text(
        message,
        ctx,
        f"💸 {money(amount)} 🪙 → {mention(target)} (new balance {money(result.balance)}).",
    )


@router.message(Command("takecoins", "removecoins", "rmmoney", "delmoney"))
async def takecoins(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, access: Access
) -> None:
    staff_of(access)
    args = Args.of(command)
    target = await resolve_user(session, message, args.first)
    amount = Args(raw=" ".join(args.words[1:]), words=tuple(args.words[1:])).amount
    if target is None or not amount:
        await text(message, ctx, "Usage: <code>/takecoins @player 100000</code>")
        return
    result = await ctx.economy.admin_take(
        session, target, int(amount), actor=access.user_id, reason=f"sudo by {access.user_id}"
    )
    await text(
        message,
        ctx,
        f"➖ {money(amount)} 🪙 from {mention(target)} (balance {money(result.balance)}).",
    )


@router.message(Command("setrole", "sudo", "addsudo", "editsudo", "rmsudo"))
async def setrole(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, access: Access
) -> None:
    """/setrole @player moderator|admin|user — owner only (a role ladder that an admin
    can hand out to itself is not a ladder)."""
    if access.role is not Role.OWNER:
        await refuse(message, "only the bot owner changes roles.")
        return
    args = Args.of(command)
    target = await resolve_user(session, message, args.first)
    wanted = (args.words[1] if len(args.words) > 1 else "").lower()
    if target is None or wanted not in {"user", "moderator", "admin", "owner"}:
        await text(message, ctx, "Usage: <code>/setrole @player admin</code>")
        return
    from waifu.db.repositories import users as user_repo

    await user_repo.set_role(session, target, Role(wanted))
    await ctx.moderation.audit(
        session,
        actor_id=access.user_id,
        action="role.set",
        target=str(target),
        detail=f"{access.role} → {wanted}",
    )
    await text(message, ctx, f"🎭 {mention(target)} is now <b>{wanted}</b> (audit line written).")


@router.message(Command("broadcast", "announce", "bc"))
async def broadcast(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, access: Access
) -> None:
    """``/broadcast <text>`` → every registered group, throttled by the notifier."""
    staff_of(access)
    args = Args.of(command)
    if not args.raw:
        await text(
            message, ctx, "Usage: <code>/broadcast text</code> (attach a photo for a banner)"
        )
        return
    photo = message.photo[-1].file_id if message.photo else None
    result = await ctx.moderation.broadcast(
        session, args.raw, access=access, photo=photo, only_premium=False
    )
    await text(
        message,
        ctx,
        f"📣 sent to {money(result.get('sent', 0))} chat(s), {money(result.get('failed', 0))} failed.",
    )


@router.message(Command("doctor", "status", "diagnose", "panel", "owner"))
async def doctor(message: Message, ctx: AppContext, session: Any, access: Access) -> None:
    """Read-only health page: ledger integrity, queue depth, feature gates."""
    staff_of(access)
    problems = await ctx.economy.integrity(session)
    health = await ctx.db.healthcheck()
    due = await ctx.spawn.due_autospawns(session, limit=50)
    rows = [
        [
            "ledger",
            "✅ clean" if not problems else f"❌ {len(problems)} mismatch(es): {problems[0]}",
        ],
        [
            "db",
            str(health.get("db", "?"))
            + (f" · {health.get('pg_size')}" if health.get("pg_size") else ""),
        ],
        ["players", money(health.get("users", 0))],
        ["spawn queue", f"{len(due)} chat(s) due"],
        [
            "features",
            ", ".join(sorted(name for name, enabled in (ctx.api_flags or {}).items() if enabled))
            or "none",
        ],
    ]
    await card(
        message,
        ctx,
        builder=RichMessageBuilder()
        .heading("🩺 doctor", size=2)
        .table([["check", "result"], *rows], compact=True),
        html="\n".join(f"{row[0]}: {row[1]}" for row in rows),
    )


@router.message(Command("maint", "maintenance"))
async def maint(message: Message, ctx: AppContext, command: CommandObject, access: Access) -> None:
    """``/maint on 30`` — park the bot with a reason; handlers refuse while it is on."""
    staff_of(access)
    args = Args.of(command)
    flag = args.first.lower()
    if flag in {"on", "off"}:
        ctx.extra["maintenance"] = args.rest if flag == "on" else ""
        await text(message, ctx, f"🔧 maintenance {flag}" + (f": {args.rest}" if args.rest else ""))
        await ctx.notify(
            f"🔧 maintenance {flag} by {access.user_id}: {args.rest or '-'}", silent=True
        )
        return
    await text(
        message,
        ctx,
        f"maintenance: {'on — ' + str(ctx.extra.get('maintenance')) if ctx.extra.get('maintenance') is not None and ctx.extra.get('maintenance') != '' else 'off'}",
    )


@router.message(Command("vacuum", "prune"))
async def vacuum(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, access: Access
) -> None:
    """Housekeeping the old bot never had: activity pruning + expired-record sweeps."""
    staff_of(access)
    args = Args.of(command)
    days = args.integer or 14
    pruned = await ctx.stats.prune(session, days=days)
    expired_codes = await ctx.codes.purge_expired(session)
    expired_trades = await ctx.trades.expire_stale(session)
    await text(
        message,
        ctx,
        "🧽 "
        + " · ".join(f"{key} {money(value)}" for key, value in (pruned or {}).items())
        + f" · codes {expired_codes} · trades {expired_trades}",
    )


@router.message(Command("sudolist", "staff"))
async def sudolist(message: Message, ctx: AppContext, session: Any) -> None:
    """Who can run the admin commands — the config layer plus the database layer.

    Two sources because there are two kinds of staff: ids pinned in the environment
    (survive a database reset, which is what you want for the owner) and roles stored per
    player (so a moderator can be added from a phone without a deploy).
    """
    cfg = ctx.settings
    lines = [
        f"👑 owner: {mention(int(cfg.owner_id))}"
        if cfg.owner_id
        else "👑 owner: not configured (OWNER_ID)"
    ]
    if cfg.admin_ids:
        lines.append("🛡️ admins: " + ", ".join(mention(int(item)) for item in cfg.admin_ids[:20]))
    else:
        lines.append("🛡️ admins: none in config (ADMIN_IDS)")
    lines.append(
        "🧩 group moderators are resolved live from Telegram — /warn works for any group admin."
    )
    await text(message, ctx, "\n".join(lines) + "\n\n/setrole @player user|moderator|admin|owner")


@router.message(Command("unpremium", "revokepremium"))
async def unpremium(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, access: Any
) -> None:
    """Take premium back (refund claw-backs; the ledger entry is the audit trail)."""
    from waifu.plugins._kit import Args, resolve_user, staff_of

    staff_of(access)
    args = Args.of(command)
    target = await resolve_user(session, message, args.first)
    if target is None:
        await text(message, ctx, "Usage: <code>/unpremium @player</code>")
        return
    info = await ctx.premium.premium(session, target)
    hours = int(getattr(info, "hours_left", 0) or 0)
    if hours <= 0:
        await text(message, ctx, f"{mention(target)} has no premium left.")
        return
    await ctx.premium.grant_premium(
        session, target, -hours, granted_by=access.user_id, source="admin-revoke"
    )
    await text(message, ctx, f"⏪ removed {hours}h of premium from {mention(target)}.")
