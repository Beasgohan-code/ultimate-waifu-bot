"""``/profile``, ``/bio``, ``/setname``, ``/rank``, ``/glow``, ``/privacy``.

Their profile card was a plain caption. Here it is the same
:class:`~waifu.tg.rich.RichMessageBuilder` the rest of the bot uses, so on a modern API
server it gets a photo, a stats table and buttons, and on an old one it degrades to the
identical text (never a broken card). That single-renderer rule is why /profile cannot
drift from /collection the way two hand-written captions do.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

from waifu.enums import Rarity
from waifu.plugins._kit import (
    Args,
    RichMessageBuilder,
    bar,
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
from waifu.utils.font import style

if TYPE_CHECKING:  # pragma: no cover
    from waifu.core.access import Access
    from waifu.core.context import AppContext

router = Router(name="players")


@router.message(Command("profile", "p", "whoami", "pinfo"))
async def profile(
    message: Message,
    ctx: AppContext,
    session: Any,
    command: CommandObject,
    user: Any,
    access: Access,
) -> None:
    args = Args.of(command)
    target = await resolve_user(session, message, args.raw) or access.user_id
    await send_profile(message, ctx, session, target=target, viewer=access.user_id)


async def send_profile(
    event: Message | CallbackQuery, ctx: AppContext, session: Any, *, target: int, viewer: int
) -> None:
    from waifu.db.repositories import users as user_repo

    player = await user_repo.get(session, target)
    if player is None:
        await _respond(event, ctx, "No such player (they have never sent /start here).")
        return
    stats = await ctx.stats.player(session, target)
    profile_data = await ctx.collection.profile(session, target)
    featured = await ctx.collection.favourite(session, target)
    pref = await user_repo.prefs(session, target)
    name = style(player.first_name or player.username or "player", pref.font)
    builder = RichMessageBuilder().heading(f"👤 {name}", size=1)
    if featured is not None:
        card_image = await ctx.cards.image_for(
            session, await ctx.collection.character(session, featured.character_id)
        )
        if card_image.ok and card_image.sendable:
            builder.photo(
                card_image.sendable,
                caption=f"featured: {featured.name} · {shorten(featured.anime, 30)}",
            )
    rows = [
        ["coins", f"{money(stats.balance)} 🪙" if pref.show_balance else "hidden"],
        ["level", f"{stats.level} · {bar(stats.exp % 1000, 1000)} {money(stats.exp)} xp"],
        [
            "harem",
            f"{money(stats.collection)} characters · {money(profile_data.get('value', 0))} 🪙",
        ],
        ["summons", f"{money(stats.pulls)} ({money(stats.high_pulls)} high-tier)"],
        ["streak", f"{stats.streak} 🔥 (best {stats.best_streak})"],
        [
            "badges",
            f"{len(stats.achievements or [])} unlocked"
            if isinstance(stats.achievements, list)
            else f"{money(stats.achievements or 0)} unlocked",
        ],
        ["premium", f"{stats.premium_hours}h left" if stats.premium_hours else "—"],
    ]
    if player.username:
        rows.append(["contact", f"@{player.username}"])
    bio = str(profile_data.get("bio") or "").strip()
    if bio:
        rows.append(["bio", shorten(bio, 220)])
    builder.table(rows, compact=True, bordered=False)
    ladder = " ".join(
        f"{Rarity.from_value(int(rid)).emoji}{money(count)}"
        for rid, count in sorted((stats.per_rarity or {}).items())
    )
    if ladder:
        builder.line(ladder)
    buttons: list[list[Any]] = [[callback("🎴 collection", cb("prof", "col", str(target)))]]
    if viewer != target:
        buttons.append(
            [
                callback("🎁 gift", cb("gift", "to", str(target))),
                callback("🕵️ rob", cb("prof", "rob", str(target))),
            ]
        )
    html = _plain(name, rows)
    if isinstance(event, CallbackQuery):
        await edit(event, ctx, builder=builder, html=html)
    else:
        await card(event, ctx, builder=builder, html=html, buttons=buttons)


def _plain(title: str, rows: list[list[str]]) -> str:
    return f"<b>{title}</b>\n" + "\n".join(f"<b>{row[0]}:</b> {row[1]}" for row in rows)


@router.callback_query(F.data.startswith("prof:col:"))
async def profile_collection(callback_query: CallbackQuery, ctx: AppContext, session: Any) -> None:
    target = int((callback_query.data or "").split(":")[-1] or 0)
    from waifu.plugins.collection import send_page

    await send_page(
        callback_query,
        ctx,
        session,
        owner=target,
        viewer_id=callback_query.from_user.id if callback_query.from_user else 0,
        page=0,
        mode="rarity",
    )


@router.message(Command("bio", "desc", "description"))
async def bio(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, user: Any
) -> None:
    """``/bio <text>`` sets the line under your name (their bot had no bio at all)."""
    if user is None:
        return
    from waifu.db.repositories import users as user_repo

    args = Args.of(command)
    if not args.raw:
        pref = await user_repo.prefs(session, user.id)
        current = str((pref.flags or {}).get("bio") or "")
        await text(
            message, ctx, current or "No bio yet — <code>/bio a line about you</code> (220 chars)."
        )
        return
    clean = " ".join(args.raw.split())[:220]
    if any(token in clean.lower() for token in ("<script", "javascript:")):
        await refuse(message, "no markup in bios, please.")
        return
    await user_repo.set_pref(session, user.id, bio=clean)
    await text(message, ctx, f"Bio saved:\n<i>{clean}</i>")


@router.message(Command("setname", "name", "nickname"))
async def setname(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, user: Any
) -> None:
    """Display name inside the bot (Telegram's own name is not touched)."""
    if user is None:
        return
    args = Args.of(command)
    if not args.raw:
        await text(
            message,
            ctx,
            "Usage: <code>/setname Your Name</code> — only what the bot shows; your Telegram name stays yours.",
        )
        return
    clean = " ".join(args.raw.split())[:48]
    from waifu.db.repositories import users as user_repo

    # ``upsert`` already owns "insert or update the identity fields", so a rename goes
    # through it instead of a second write path that forgets ``updated_at``.
    await user_repo.upsert(session, user.id, first_name=clean)
    await text(
        message,
        ctx,
        f"Name set to <b>{clean}</b>. The bot shows this; your Telegram name is untouched.",
    )


@router.message(Command("lvl", "level", "myrank"))
async def rank(
    message: Message,
    ctx: AppContext,
    session: Any,
    command: CommandObject,
    user: Any,
    access: Access,
) -> None:
    args = Args.of(command)
    target = await resolve_user(session, message, args.raw) or access.user_id
    stats = await ctx.stats.player(session, target)
    kind = args.rest.split()[-1] if len(args.words) > 1 else "coins"
    rank_now, total = await ctx.stats.rank_of(
        session, target, kind=kind if kind in {"coins", "pulls", "value", "claims"} else "coins"
    )
    builder = (
        RichMessageBuilder()
        .heading(f"#{money(rank_now)} of {money(total)}", size=1)
        .table(
            [
                ["level", money(stats.level)],
                ["xp", money(stats.exp)],
                ["to next level", money(max(0, stats.level * 1000 - stats.exp))],
            ],
            compact=True,
            bordered=False,
        )
        .line(bar(stats.exp % 1000, 1000))
    )
    await card(
        message, ctx, builder=builder, html=f"rank #{rank_now}/{total} · level {stats.level}"
    )


@router.message(Command("glow", "profileglow"))
async def glow(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, user: Any
) -> None:
    """Their ``/profileglow``: the name banner on your card. Off by default."""
    if user is None:
        return
    from waifu.db.repositories import users as user_repo

    pref = await user_repo.prefs(session, user.id)
    args = Args.of(command)
    wanted = args.first.lower()
    value = not pref.glow if wanted not in {"on", "off"} else wanted == "on"
    await user_repo.set_pref(session, user.id, glow=value)
    await text(message, ctx, f"profile glow {'✨ on' if value else 'off'}.")


@router.message(Command("privacy", "hide"))
async def privacy(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, user: Any
) -> None:
    if user is None:
        return
    from waifu.db.repositories import users as user_repo

    pref = await user_repo.prefs(session, user.id)
    await user_repo.set_pref(session, user.id, show_balance=not pref.show_balance)
    await text(
        message,
        ctx,
        f"Your balance is {'hidden 🙈' if not pref.show_balance else 'public 👀'} on /profile.",
    )


@router.message(Command("register", "join"))
async def register(
    message: Message, ctx: AppContext, session: Any, user: Any, access: Access
) -> None:
    """/register exists so players can stop asking "how do I start" — it is a no-op if
    the middleware already created the account, which it does on the first message."""
    if user is None:
        await text(
            message,
            ctx,
            "Say anything and you are registered — the bot creates the account on your first message.",
        )
        return
    await text(
        message,
        ctx,
        f"You are registered as {mention(access.user_id, user.first_name)} with <b>{money(await ctx.economy.balance(session, access.user_id))} 🪙</b>.\n"
        "Next: /daily, then /pull.",
    )


async def _respond(event: Message | CallbackQuery, ctx: AppContext, html: str) -> None:
    if isinstance(event, CallbackQuery):
        await note(event, html[:200], alert=True)
        return
    await text(event, ctx, html)


@router.callback_query(F.data.startswith("prof:rob:"))
async def rob_from_profile(
    callback_query: CallbackQuery, ctx: AppContext, session: Any, access: Access
) -> None:
    """The 🕵️ button on someone's card runs the same steal as ``/rob`` — one code path."""
    target = int((callback_query.data or "").split(":")[-1] or 0)
    if not target or target == access.user_id:
        await note(callback_query, "not yourself 🙂", alert=True)
        return
    from waifu.plugins.economy import do_rob

    if callback_query.message is None:
        await callback_query.answer()
        return
    await do_rob(
        callback_query.message,
        ctx,
        session,
        attacker=access.user_id,
        args=Args(raw=str(target), words=(str(target),)),
    )
