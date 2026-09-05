"""``/summon``, ``/autospan``, ``/changetime``, ``/hint``, ``/spawn`` (group event spawns).

Auto-spawn is the feature that made the reference bot fun in groups and also the one
that got it muted: every N messages a character appeared, claimed by whoever typed
first, and the counter was a column read-modify-written per message, so a busy chat
double-fired and the "one spawn per 100 messages" promise became "several per minute".

Fixes here: the counter is one atomic ``UPDATE … RETURNING`` in the session middleware
(so 200 simultaneous messages cannot lose or duplicate a count), the spawn row is
claimed by a conditional ``UPDATE`` (one winner, by construction, no lock), and the
admin surface is per-group with a mute switch, because a muted group must be able to
turn *only* the feed off and keep /warn working.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

from waifu.errors import AlreadyClaimed, Locked, NotFound, WaifuError
from waifu.plugins._kit import (
    Args,
    card,
    money,
    note,
    refuse,
    text,
)

if TYPE_CHECKING:  # pragma: no cover
    from waifu.core.access import Access
    from waifu.core.context import AppContext

router = Router(name="spawns")


@router.message(Command("spawn", "current", "checkspawn"))
async def current(message: Message, ctx: AppContext, session: Any, access: Access) -> None:
    """Show the live spawn in this chat (their bot had no way to ask "is one out?")."""
    view = await ctx.spawn.current(session, message.chat.id)
    if view is None:
        await text(
            message,
            ctx,
            "Nothing is out. The feed runs on message count — /autospan shows the settings.",
        )
        return
    builder, buttons = ctx.spawn.card(view)
    await card(
        message,
        ctx,
        builder=builder,
        html=f"⚡ {view.name} is live — /claim {view.expected_name}",
        buttons=[list(row) for row in buttons] if buttons else None,
    )


@router.message(Command("claim", "grab", "catch"))
async def claim(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, access: Access
) -> None:
    """``/claim`` (first responder wins) or ``/claim <name>`` (must match the spawn)."""
    args = Args.of(command)
    result = await ctx.spawn.claim(
        session, access.user_id, chat_id=message.chat.id, typed=args.raw or None
    )
    if not result.won:
        view = await ctx.spawn.current(session, message.chat.id)
        if view is None:
            await text(message, ctx, "Nothing to claim right now.")
            return
        await refuse(message, result.reason or "someone else got there first")
        return
    view = (
        await ctx.spawn.view_by_id(session, int(result.spawn_id or 0)) if result.spawn_id else None
    )
    dupe = (
        f"\n🔁 duplicate — paid {money(result.dupe_payout)} 🪙 instead"
        if result.dupe_payout
        else ""
    )
    await text(
        message,
        ctx,
        f"⚡ <b>{result.name}</b> is yours{dupe}\nnext claim opens after the next spawn.",
    )
    if view is not None:
        await ctx.spawn.settle_card(
            message.chat.id,
            view,
            winner_name=(
                message.from_user.first_name if message.from_user else str(access.user_id)
            ),
            message_id=view.message_id,
        )
    await ctx.react(message.chat.id, message.message_id, "🎉")


@router.callback_query(F.data.startswith("spawn:claim:"))
async def claim_button(
    callback_query: CallbackQuery, ctx: AppContext, session: Any, access: Access
) -> None:
    spawn_id = int((callback_query.data or "").split(":")[-1] or 0)
    result = await ctx.spawn.claim(session, access.user_id, spawn_id=spawn_id)
    view = await ctx.spawn.view_by_id(session, spawn_id)
    if not result.won:
        await note(callback_query, result.reason or "too late — someone claimed it", alert=True)
        if view is not None:
            await ctx.spawn.settle_card(
                view.chat_id, view, winner_name="someone else", message_id=view.message_id
            )
        return
    if view is not None:
        await ctx.spawn.settle_card(
            view.chat_id,
            view,
            winner_name=(callback_query.from_user.first_name if callback_query.from_user else None),
            message_id=view.message_id,
        )
    await note(callback_query, f"claimed {result.name} 🎉", alert=True)


@router.message(Command("hint"))
async def hint(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, access: Access
) -> None:
    """Spend a hint (an item or coins) to narrow the current spawn.

    The round is per-chat, so there is no id to pass — theirs required the spawn id,
    which the bot never printed anywhere, making /hint effectively unusable.
    """
    del command
    view = await ctx.spawn.current(session, message.chat.id)
    if view is None:
        await text(message, ctx, "No spawn is out, so there is nothing to hint about.")
        return
    try:
        text_hint = await ctx.spawn.spend_hint(session, int(view.id))
    except (AlreadyClaimed, Locked, NotFound, WaifuError) as exc:
        await refuse(message, exc.user_message)
        return
    fresh = await ctx.spawn.view_by_id(session, int(view.id))
    if fresh is not None:
        builder, _buttons = ctx.spawn.card(fresh, show_hint=True)
        await card(message, ctx, builder=builder, html=f"💡 {text_hint}")
        return
    await text(message, ctx, f"💡 {text_hint}")


@router.message(Command("summon", "aspawn", "hspawn", "collect"))
async def summon(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, access: Access
) -> None:
    """``/summon <name>`` spawn a specific character · ``/summon`` a random high tier.

    Admin/group-owner tool (their bot let anyone spawn anyone, which is how a
    1M-coin character ends up in one player's harem on day one). A *group* owner may
    spawn only within their own chat and gets the tier cap the settings define.
    """
    staff = access.is_staff
    args = Args.of(command)
    if not staff and not await _is_group_owner(ctx, session, message):
        await refuse(message, "only this group's admins can summon on demand — wait for the feed")
        return
    character = None
    if args.raw:
        try:
            character = await ctx.collection.find(session, args.raw)
        except NotFound:
            await text(message, ctx, f"Nothing in the roster matches “{args.raw[:40]}”.")
            return
    spawn, chosen = await ctx.spawn.open(
        session, message.chat.id, character=character, source="admin" if staff else "group"
    )
    view = await ctx.spawn.view(session, spawn)
    builder, buttons = ctx.spawn.card(view)
    await card(
        message,
        ctx,
        builder=builder,
        html=f"⚡ {chosen.name} is live in this chat",
        buttons=[list(row) for row in buttons] if buttons else None,
    )
    await ctx.notify(
        f"⚡ spawn: {chosen.name} (#{int(view.id)}) in {message.chat.id} by {access.user_id}",
        silent=True,
    )


async def _is_group_owner(ctx: AppContext, session: Any, message: Message) -> bool:
    if message.chat.is_private:
        return False
    return bool(
        message.from_user and message.is_sender_admin
        if hasattr(message, "is_sender_admin")
        else False
    )


@router.message(Command("autospan", "aspawn_time", "changetime", "reggroup", "savegroup"))
async def autospan(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, access: Access
) -> None:
    """/autospan on|off · /autospan limit 250 — this group's feed settings."""
    if message.chat.is_private:
        await text(message, ctx, "The spawn feed is a group feature — run this in the group.")
        return
    if not (access.is_staff or access.is_group_admin):
        await refuse(message, "group admins only.")
        return
    args = Args.of(command)
    flag = args.first.lower()
    if flag in {"on", "off", "enable", "disable"}:
        await ctx.spawn.set_spawn_enabled(session, message.chat.id, flag in {"on", "enable"})
        await text(
            message,
            ctx,
            f"⚡ spawn feed {'on' if flag in {'on', 'enable'} else 'off'} for this group.",
        )
        return
    if flag == "limit" and args.rest.split() and args.rest.split()[0].isdigit():
        await ctx.spawn.save_group(
            session, message.chat.id, spawn_limit=max(10, int(args.rest.split()[0]))
        )
        await text(message, ctx, f"⚡ a spawn every {args.rest.split()[0]} messages.")
        return
    groups = await ctx.spawn.groups(session)
    await text(
        message,
        ctx,
        "Usage: <code>/autospan on|off</code> · <code>/autospan limit 200</code>\n"
        f"This instance: {money(groups.get('enabled', 0))} group(s) feeding, {money(groups.get('total', 0))} registered.",
    )


@router.message(Command("spawnlog", "spawns"))
async def spawnlog(message: Message, ctx: AppContext, session: Any, access: Access) -> None:
    rows = await ctx.spawn.recent(session, message.chat.id, limit=8)
    if not rows:
        await text(message, ctx, "no spawns in this chat yet")
        return
    lines = [
        f"• {getattr(row, 'name', '?')} — {getattr(row, 'status', '')!s} {str(getattr(row, 'expires_at', ''))[5:16]}"
        for row in rows
    ]
    await text(message, ctx, "📜 <b>recent spawns here</b>\n" + "\n".join(lines))


@router.callback_query(F.data.startswith("spawn:noop"))
async def noop(callback_query: CallbackQuery) -> None:
    await callback_query.answer(cache_time=3600)
