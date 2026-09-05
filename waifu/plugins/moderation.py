"""``/warn``, ``/warnings``, ``/case``, ``/banlist``, ``/mute``, ``/kick``, ``/settings``.

Their moderation was a ``warnings`` table plus an auto-ban flag on ``users``, and the
bot banned people for spam in *every* group because the threshold was a global constant
(``SPAM_LIMIT = 20``). Two owners who wanted different rules had to fork the code.

Here: every punishment is a case row with an actor, a reason and an expiry, so
``/case <user>`` reconstructs what happened and when; the warn ladder is per-group
(``warn_ladder_json`` in settings, overridable with ``/group warn_ladder``); and the
spam threshold belongs to the group, not the deployment.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, ChatPermissions, Message

from waifu.errors import NotFound, PermissionDenied, WaifuError
from waifu.plugins._kit import (
    Args,
    RichMessageBuilder,
    callback,
    card,
    cb,
    mention,
    money,
    note,
    refuse,
    resolve_user,
    shorten,
    staff_of,
    text,
)

if TYPE_CHECKING:  # pragma: no cover
    from waifu.core.access import Access
    from waifu.core.context import AppContext

router = Router(name="moderation")


@router.message(Command("warn"))
async def warn(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, access: Access
) -> None:
    """/warn @player reason — the ladder decides what happens at 3, 4 and 5."""
    if message.chat.is_private:
        await text(message, ctx, "Warnings are per-group; run this in the group.")
        return
    _require_group_admin(access, message)
    args = Args.of(command)
    target = await resolve_user(session, message, args.first)
    if target is None:
        await text(
            message, ctx, "Whom? <code>/warn @player being rude</code> (or reply to their message)."
        )
        return
    reason = args.rest or "(no reason given)"
    result = await ctx.moderation.warn(
        session,
        chat_id=message.chat.id,
        user_id=target,
        moderator_id=access.user_id,
        reason=reason,
        count=args.count,
    )
    extra = ""
    if result.action and result.action != "warn":
        extra = f"\n⛔ {result.action}" + (f" for {result.duration}s" if result.duration else "")
    await text(
        message,
        ctx,
        f"⚠️ {mention(target)} has <b>{result.count}</b> warning(s) — {shorten(reason, 120)}{extra}",
    )
    await ctx.notify(
        f"⚠️ warn {target} in {message.chat.id} by {access.user_id}: {shorten(reason, 60)}",
        silent=True,
    )


def _require_group_admin(access: Access, message: Message) -> None:
    """Staff, or a chat admin whose rights the access middleware just read.

    Group owners must be able to moderate their own chat without a sudo entry — that was
    the single most requested change in the legacy support group, and it is why the
    warn ladder is per-group rather than a constant in ``config.py``.
    """
    if access is None or not (access.is_staff or access.is_group_admin):
        raise PermissionDenied("this command needs group-admin rights (or bot staff).")


@router.message(Command("warnings", "warns"))
async def warnings(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, access: Access
) -> None:
    args = Args.of(command)
    target = await resolve_user(session, message, args.raw) or (access.user_id if access else 0)
    rows = await ctx.moderation.warnings(session, message.chat.id, target)
    if not rows:
        await text(message, ctx, f"{mention(target)} has no warnings here.")
        return
    lines = [
        f"{index}. {str(getattr(row, 'created_at', ''))[:16]} — {shorten(str(getattr(row, 'reason', '') or ''), 60)} (by {getattr(row, 'moderator_id', '?')})"
        for index, row in enumerate(rows, start=1)
    ]
    board = await ctx.moderation.warning_board(session, message.chat.id, limit=10)
    top = (
        " · ".join(f"{mention(user_id)} ×{count}" for user_id, count in board[:5]) if board else ""
    )
    await text(
        message,
        ctx,
        f"⚠️ {mention(target)}: {len(rows)} warning(s)\n"
        + "\n".join(lines)
        + (f"\n\n📋 most warned: {top}" if top else ""),
    )


@router.message(Command("clearwarns", "unwarn"))
async def clear_warns(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, access: Access
) -> None:
    _require_group_admin(access, message)
    args = Args.of(command)
    target = await resolve_user(session, message, args.first)
    if target is None:
        await text(message, ctx, "Usage: <code>/clearwarns @player [count]</code>")
        return
    removed = await ctx.moderation.remove_warning(
        session,
        chat_id=message.chat.id,
        user_id=target,
        moderator_id=access.user_id,
        count=args.count,
    )
    await text(message, ctx, f"🧹 removed {removed} warning(s) from {mention(target)}.")


@router.message(Command("case", "cases"))
async def cases(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, access: Access
) -> None:
    args = Args.of(command)
    target = await resolve_user(session, message, args.first) if args.first else None
    rows = await ctx.moderation.cases(session, message.chat.id, user_id=target, limit=15)
    if not rows:
        await text(message, ctx, "no moderation cases recorded here")
        return
    table = [["when", "action", "target", "moderator", "note"]]
    for row in rows:
        table.append(
            [
                str(getattr(row, "created_at", ""))[:16],
                str(getattr(row, "action", "?")),
                str(getattr(row, "user_id", "?")),
                str(getattr(row, "moderator_id", "?")),
                shorten(str(getattr(row, "detail", "") or getattr(row, "reason", "") or ""), 40),
            ]
        )
    builder = (
        RichMessageBuilder().heading(f"📋 cases — {len(rows)}", size=2).table(table, compact=True)
    )
    buttons = (
        [[callback("close newest", cb("mod", "close", str(getattr(rows[0], "id", 0))))]]
        if any(getattr(row, "id", None) for row in rows)
        else None
    )
    await card(
        message,
        ctx,
        builder=builder,
        html="\n".join(f"{row[0]} {row[1]} → {row[2]}" for row in table[1:]),
        buttons=buttons,
    )


@router.callback_query(F.data.startswith("mod:close:"))
async def close_case(
    callback_query: CallbackQuery, ctx: AppContext, session: Any, access: Access
) -> None:
    staff_of(access)
    case_id = int((callback_query.data or "").split(":")[-1] or 0)
    try:
        await ctx.moderation.close_case(session, case_id)
    except NotFound as exc:
        await note(callback_query, exc.user_message, alert=True)
        return
    await note(callback_query, "case closed")


@router.message(Command("banlist", "bans"))
async def banlist(message: Message, ctx: AppContext, session: Any) -> None:
    rows = await ctx.moderation.ban_list(session, limit=25)
    if not rows:
        await text(message, ctx, "no global bans on this instance")
        return
    lines = [
        f"• {money(int(getattr(row, 'user_id', 0)))} — {shorten(str(getattr(row, 'reason', '') or ''), 50)} ({str(getattr(row, 'created_at', ''))[:10]})"
        for row in rows
    ]
    await text(message, ctx, "⛔ <b>global bans</b>\n" + "\n".join(lines))


@router.message(Command("gban", "globalban"))
async def gban(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, access: Access
) -> None:
    staff_of(access)
    args = Args.of(command)
    target = await resolve_user(session, message, args.first)
    if target is None:
        await text(message, ctx, "Usage: <code>/gban @player reason</code>")
        return
    await ctx.moderation.global_ban(
        session, target, moderator_id=access.user_id, reason=args.rest or "spam"
    )
    await text(message, ctx, f"⛔ {mention(target)} banned across every chat this bot serves.")


@router.message(Command("ungban", "globalunban"))
async def ungban(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, access: Access
) -> None:
    staff_of(access)
    args = Args.of(command)
    target = await resolve_user(session, message, args.first)
    if target is None:
        await text(message, ctx, "Usage: <code>/ungban @player</code>")
        return
    await ctx.moderation.global_unban(session, target, moderator_id=access.user_id)
    await text(message, ctx, f"✅ {mention(target)} is no longer globally banned.")


@router.message(Command("mute", "unmute", "tempmute"))
async def mute(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, access: Access
) -> None:
    _require_group_admin(access, message)
    args = Args.of(command)
    target = await resolve_user(session, message, args.first)
    if target is None:
        await text(message, ctx, "Usage: <code>/mute @player 600 reason</code>")
        return
    verb = (message.text or "/mute").split()[0].lstrip("/").split("@")[0]
    seconds = 0
    for token in args.words[1:]:
        if token.isdigit():
            seconds = max(0, min(365 * 86400, int(token)))
            break
    if verb == "unmute":
        await ctx.bot.restrict_chat_member(
            message.chat.id, target, ChatPermissions(can_send_messages=True)
        )
        await ctx.moderation.close_active(session, message.chat.id, target, action="unmute")
        await text(message, ctx, f"🔊 {mention(target)} can talk again.")
        return
    result = await ctx.moderation.restrict(
        message.chat.id,
        target,
        action="mute",
        seconds=seconds or 3600,
        reason=args.rest or "moderated",
    )
    if not result.get("ok", True):
        await refuse(message, str(result.get("error", "the bot cannot restrict members here")))
        return
    await text(message, ctx, f"🔇 {mention(target)} muted for {money(seconds or 3600)}s.")


@router.message(Command("purge", "deletelast", "remove", "removeall"))
async def purge(message: Message, ctx: AppContext, access: Access, command: CommandObject) -> None:
    """Delete the bot's own recent spam in this chat — the fix for a bad config."""
    _require_group_admin(access, message)
    count = await ctx.moderation.purge_recent(message.chat.id, access.user_id, limit=25)
    await text(message, ctx, f"🧹 removed {count} of the bot's recent messages here.", silent=True)


@router.message(Command("groupsettings", "gset"))
async def group_settings(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, access: Access
) -> None:
    """/gset spam_limit=30 welcome_text=hi {name} — per-group knobs."""
    if message.chat.is_private:
        await text(message, ctx, "Group settings apply to a group; run this there.")
        return
    _require_group_admin(access, message)
    args = Args.of(command)
    values: dict[str, Any] = {}
    for token in (args.raw or "").split():
        if "=" not in token:
            continue
        key, raw = token.split("=", 1)
        key = key.lower().replace("-", "_")
        if raw.lower() in {"on", "true"}:
            values[key] = True
        elif raw.lower() in {"off", "false"}:
            values[key] = False
        elif raw.isdigit():
            values[key] = int(raw)
        else:
            values[key] = raw.replace("_", " ")
    if not values:
        group = await ctx.moderation.group(
            session, message.chat.id, title=message.chat.title or "", create=True
        )
        snapshot = dict((getattr(group, "data", None) or {}).items())
        snapshot.update(
            {
                "spawn_enabled": getattr(group, "spawn_enabled", None),
                "spawn_limit": getattr(group, "spawn_limit", None),
                "spam_limit": getattr(group, "spam_limit", None),
            }
        )
        rows = [[key, str(value)] for key, value in snapshot.items() if value is not None]
        await card(
            message,
            ctx,
            builder=RichMessageBuilder()
            .heading("⚙️ this group", size=2)
            .table(rows, compact=True, bordered=False),
            html="\n".join(f"{row[0]}: {row[1]}" for row in rows),
        )
        return
    try:
        applied = await ctx.moderation.set_group_flags(session, message.chat.id, **values)
    except WaifuError as exc:
        await refuse(message, exc.user_message)
        return
    await text(
        message,
        ctx,
        "⚙️ set: " + ", ".join(f"{key}={value}" for key, value in applied.items() if key in values),
    )


@router.message(Command("ban", "unban"))
async def ban(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, access: Access
) -> None:
    """/ban @player reason · /unban @player — Telegram action *and* a case row.

    The old bot called ``ban_chat_member`` straight from the handler: no record, no
    reason, nothing for ``/case`` to show, so a wrongful ban was unarguable. Everything
    here goes through the moderation service, which writes the case and returns whether
    Telegram actually allowed it (the bot is often not an admin with ban rights).
    """
    _require_group_admin(access, message)
    args = Args.of(command)
    target = await resolve_user(session, message, args.first)
    if target is None:
        await text(
            message, ctx, "Usage: <code>/ban @player reason</code> · <code>/unban @player</code>"
        )
        return
    verb = (message.text or "/ban").split()[0].lstrip("/").split("@")[0]
    action = "unban" if verb == "unban" else "ban"
    result = await ctx.moderation.restrict(
        message.chat.id,
        target,
        action=action,
        reason=args.rest[:120] or ("unbanned" if action == "unban" else "banned"),
    )
    if not result.get("ok", False):
        await refuse(
            message,
            f"Telegram refused: {result.get('error') or 'the bot needs ban rights in this group'}",
        )
        return
    if action == "unban":
        await ctx.moderation.close_active(session, message.chat.id, target, action="unban")
    await text(
        message,
        ctx,
        f"{'⛔ ' + mention(target) + ' banned from this group (permanent — use /mute for timed)' if action == 'ban' else '✅ ' + mention(target) + ' is unbanned.'}",
    )
