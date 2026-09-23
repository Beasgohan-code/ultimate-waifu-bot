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

import re
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


def _schedule_broadcast_args(rest: str) -> tuple[Any, Any, str]:
    """Split ``broadcast at 20:00 the text`` into (run_at, text, problem).

    The time is the leading token(s) — ``20:00``, ``20:00 tomorrow``, ``+2h``,
    ``in 30m``, ``2026-12-31 20:00`` — and the announcement is the remainder.
    Two-token times are tried only when the first token alone does not parse,
    so ``20:00 Meet at 21:00`` keeps "Meet at 21:00" as the text.
    """
    from waifu.utils.schedule import ScheduleError, parse_when

    usage = "Usage: <code>/broadcast at 20:00 your text</code>"
    tokens = rest.split(" ", 2)
    if len(tokens) < 2:
        return None, None, usage
    if tokens[1].lower() == "tomorrow":
        when, body = " ".join(tokens[:2]), (tokens[2] if len(tokens) > 2 else "")
    elif (
        len(tokens) >= 3
        and re.match(r"^\d{4}-\d{2}-\d{2}$", tokens[0])
        and re.match(r"^\d{1,2}:\d{2}$", tokens[1])
    ):
        # "2026-12-31 20:00 …" — a full date plus time is two tokens
        when, body = " ".join(tokens[:2]), tokens[2]
    else:
        when, body = tokens[0], (tokens[2] if len(tokens) > 2 else tokens[1])
    try:
        return parse_when(when), body, ""
    except ScheduleError:
        try:
            return parse_when(" ".join(tokens[:2])), (tokens[2] if len(tokens) > 2 else ""), ""
        except ScheduleError as exc:
            return None, None, exc.explain()


@router.message(Command("broadcast", "announce", "bc"))
async def broadcast(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, access: Access
) -> None:
    """``/broadcast <text>`` → every registered group, throttled by the notifier.

    ``/broadcast at 20:00 <text>`` schedules it instead (the jobs loop fires it,
    and the owner's log channel gets the result); ``/broadcast list`` shows the
    queue; ``/broadcast cancel`` empties it.
    """
    staff_of(access)
    args = Args.of(command)
    raw = args.raw
    if not raw:
        await text(
            message,
            ctx,
            "Usage: <code>/broadcast text</code> (attach a photo for a banner)\n"
            "<code>/broadcast at 20:00 text</code> · <code>/broadcast list</code> · "
            "<code>/broadcast cancel</code>",
        )
        return
    if raw.lower() in {"list", "ls", "queue"}:
        from waifu.utils.text import esc

        rows = await ctx.moderation.pending_broadcasts(session)
        if not rows:
            await text(message, ctx, "No scheduled broadcasts.")
            return
        lines = [
            f"<code>#{row.id}</code> {row.run_at:%Y-%m-%d %H:%M UTC} — "
            f"{esc(row.text[:80])} (by {row.created_by})"
            for row in rows
        ]
        await text(message, ctx, "📣 scheduled:\n" + "\n".join(lines))
        return
    if raw.lower() in {"cancel", "clear", "cancel all"}:
        n = await ctx.moderation.cancel_broadcasts(session)
        await text(message, ctx, f"🗑️ cancelled {n} scheduled broadcast(s).")
        return
    if raw.lower().startswith("at "):
        run_at, body, problem = _schedule_broadcast_args(raw[3:].strip())
        if problem:
            await text(message, ctx, problem)
            return
        assert run_at is not None and body is not None
        broadcast_id = await ctx.moderation.schedule_broadcast(
            session, run_at=run_at, text=body, actor_id=access.user_id
        )
        await text(
            message,
            ctx,
            f"⏰ broadcast <code>#{broadcast_id}</code> will fire at "
            f"<b>{run_at:%Y-%m-%d %H:%M UTC}</b>. <code>/broadcast list</code> to see it.",
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
        ["log channel", _log_channel_row(ctx)],
    ]
    await card(
        message,
        ctx,
        builder=RichMessageBuilder()
        .heading("🩺 doctor", size=2)
        .table([["check", "result"], *rows], compact=True),
        html="\n".join(f"{row[0]}: {row[1]}" for row in rows),
    )


def _log_channel_row(ctx: AppContext) -> str:
    """One line answering "is the log channel alive?" from /doctor."""
    if not ctx.settings.log_channel_id:
        return "⚠️ not configured (LOG_CHANNEL_ID)"
    stats = ctx.log_stats
    if stats.sent == 0 and stats.failed == 0:
        return "configured, no sends yet — /logtest to check it"
    row = f"{stats.sent} sent · {stats.failed} failed"
    if stats.failed and stats.last_error:
        row += f" · last: {stats.last_error}"
    return ("❌ " if stats.failed > stats.sent else "✅ ") + row


@router.message(Command("digest", "report"))
async def digest(message: Message, ctx: AppContext, access: Access) -> None:
    """Send the weekly owner digest right now (the Sunday pass does it on its own).

    Seven numbers — new players, pulls, gifts, raffles drawn, Stars in, orders,
    premium now — as a rich table on new-API servers, a plain list on old ones.
    (``/weekly`` is the *player's* personal summary, owned by the progress router.)
    """
    staff_of(access)
    from waifu.core.jobs import send_digest

    sent = await send_digest(ctx)
    if sent:
        await text(message, ctx, "📊 digest sent to the log channel.")
    else:
        await text(
            message,
            ctx,
            "⚠️ the digest has nowhere to go — the log channel is not configured "
            "(``/setlogchannel`` or LOG_CHANNEL_ID in the env).",
        )


async def _verify_log_channel(bot: Any, chat_id: int) -> tuple[Any, str]:
    """(chat, "") when the bot may post to this channel, else (None, reason).

    Two checks, because a log channel that can post today but not tomorrow is
    worse than none: the chat must exist and be a *channel* (a group's members
    could read the owner's feed), and the bot must already hold admin rights
    there — the "not enough rights" case is what silently kills feeds.
    """
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.types import Chat, ChatMemberAdministrator, ChatMemberOwner

    from waifu.utils.text import esc

    try:
        chat = await bot.get_chat(chat_id)
    except TelegramBadRequest as exc:
        return (
            None,
            f"❌ the bot cannot see that chat ({str(exc)[:120]}). Add the bot to the channel first.",
        )
    if not isinstance(chat, Chat) or chat.type != "channel":
        return (
            None,
            f"❌ that chat is a <b>{getattr(chat, 'type', '?')}</b> — the log feed goes to a "
            "channel, so nobody can see it except members of it.",
        )
    try:
        member = await bot.get_chat_member(chat_id, int(bot.id))
    except TelegramBadRequest as exc:
        return None, f"❌ rights check failed ({str(exc)[:120]})."
    if not isinstance(member, (ChatMemberAdministrator, ChatMemberOwner)):
        return (
            None,
            f"❌ the bot is not an admin of <b>{esc(chat.title or 'that channel')}</b> — it could "
            "lose posting rights at any moment. Make it admin (post messages), then try again.",
        )
    return chat, ""


@router.message(Command("setlogchannel", "setlog", "logchannel"))
async def setlogchannel(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, access: Access
) -> None:
    """Point the owner's log channel at a channel the bot administers.

    The env value stays the default; what is written here is stored in the
    database (``kv_state``) and wins across restarts — so the owner can move
    the feed without a deploy. The bot must already be an admin of the target
    channel: a channel that can post there but not see its own rights would be
    a log channel that dies the day the admin rights get trimmed.
    """
    if access.role is not Role.OWNER:
        await refuse(message, "only the bot owner moves the log channel.")
        return
    from waifu.db.repositories.stats import kv_get, kv_set
    from waifu.utils.text import esc

    args = Args.of(command)
    raw = (args.words[0] if args.words else "").strip()
    if not raw:
        current = ctx.settings.log_channel_id
        await text(
            message,
            ctx,
            (f"Log channel: <code>{current}</code>\n\n" if current else "No log channel set.\n\n")
            + "Usage: <code>/setlogchannel -100…</code> — add the bot to the channel as an "
            "<b>admin</b> first, then set it here. It persists in the database and wins "
            "over LOG_CHANNEL_ID across restarts. Check it with <code>/logtest</code>.",
        )
        return
    if not raw.lstrip("-").isdigit():
        await text(
            message, ctx, "That is not a chat id — channel ids look like <code>-100123…</code>."
        )
        return
    chat_id = int(raw)

    chat, problem = await _verify_log_channel(ctx.bot, chat_id)
    if problem:
        await text(message, ctx, problem)
        return

    overrides = await kv_get(session, "runtime_overrides")
    overrides["log_channel_id"] = chat_id
    await kv_set(session, "runtime_overrides", overrides)
    ctx.settings = ctx.settings.model_copy(update={"log_channel_id": chat_id})
    await session.commit()

    from waifu.utils.time import now_utc

    stamp = now_utc().strftime("%Y-%m-%d %H:%M UTC")
    ok = await ctx.notify(
        f"📡 log channel moved to {chat.title or chat_id} by {access.user_id} at {stamp}",
        silent=True,
    )
    await text(
        message,
        ctx,
        (
            f"✅ log channel is now <b>{esc(chat.title or str(chat_id))}</b>"
            if ok
            else "⚠️ channel stored, but the test line failed — check admin rights"
        )
        + " — it will apply on the next restart too.",
    )


@router.message(Command("logtest", "testlog"))
async def logtest(message: Message, ctx: AppContext, access: Access) -> None:
    """Send a test line to the log channel and report the outcome.

    The five-second setup check for ``LOG_CHANNEL_ID``: the owner points the
    channel at the bot, runs ``/logtest``, and knows immediately whether the
    bot can actually post there (the "not enough rights" case is the common
    one — the bot was added but not made admin).
    """
    staff_of(access)
    if not ctx.settings.log_channel_id:
        await text(
            message,
            ctx,
            "No log channel configured — set LOG_CHANNEL_ID in the env (forward any channel "
            "post to @userinfobot to read the id) and make the bot an admin of that channel.",
        )
        return
    from waifu.utils.time import now_utc

    stamp = now_utc().strftime("%Y-%m-%d %H:%M UTC")
    ok = await ctx.notify(f"📡 log channel test from {access.user_id} at {stamp}", silent=True)
    if ok:
        await text(message, ctx, "✅ test line delivered — the channel is live.")
    else:
        last = ctx.log_stats.last_error or "unknown error"
        await text(
            message,
            ctx,
            f"❌ the test line did not land ({last}). Most cause: the bot is not an admin "
            "of the channel, or the id is wrong.",
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
