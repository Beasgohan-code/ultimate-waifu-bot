"""``/hstats``, ``/htop``, ``/hrarity``, ``/hcompletion`` — the group-facing analytics.

Summon-bot's ``hstats.py`` computed a group's stats by importing the ORM inside the
handler and running four ``SELECT count(*)`` queries, one per leaderboard type, on every
call — and its "collection %" divided by the *total rows in the characters table*,
including the ones an admin had deleted, so the percentage drifted downward for
everyone forever.

Here the numbers come from :mod:`waifu.services.hstats`, which uses one grouped query
per panel and defines completion against the <i>active</i> roster.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from waifu.enums import Rarity
from waifu.plugins._kit import (
    RichMessageBuilder,
    bar,
    card,
    mention,
    money,
    text,
)
from waifu.utils.chats import is_private

if TYPE_CHECKING:  # pragma: no cover
    from waifu.core.access import Access
    from waifu.core.context import AppContext

router = Router(name="hstats")


@router.message(Command("hstats", "hs"))
async def hstats(message: Message, ctx: AppContext, session: Any) -> None:
    if is_private(message.chat):
        await text(message, ctx, "h-stats is per-group — run /hstats in a group.")
        return
    stats = await ctx.hstats.group(session, message.chat.id)
    builder = RichMessageBuilder().heading(f"📈 {stats.title or 'this group'}", size=1)
    builder.table(
        [
            ["players seen", money(stats.members_seen)],
            ["spawns", f"{money(stats.spawns_total)} · {money(stats.spawns_claimed)} claimed"],
            ["guess rounds", money(stats.guesses_played)],
            ["best streak", f"{money(stats.guess_streak)} in a row"],
            ["first seen", str(stats.first_seen)[:10] if stats.first_seen else "—"],
        ],
        compact=True,
        bordered=False,
    )
    if stats.rarities:
        builder.divider().heading("tier mix", size=3).line(
            " ".join(
                f"{Rarity.from_value(int(key)).emoji}{money(value)}"
                for key, value in sorted(stats.rarities.items())
            )
        )
    if stats.top_members:
        builder.divider().heading("most active", size=3)
        builder.line(
            " · ".join(
                f"{mention(int(user_id), name)} ×{count}"
                for user_id, name, count in [tuple(item) for item in stats.top_members[:5]]
            )
        )
    await card(
        message,
        ctx,
        builder=builder,
        html=f"spawns {stats.spawns_total}/{stats.spawns_claimed} · members {stats.members_seen}",
    )


@router.message(Command("hrarity", "rarities"))
async def hrarity(message: Message, ctx: AppContext, session: Any) -> None:
    rows = await ctx.hstats.rarity_breakdown(session)
    if not rows:
        await text(message, ctx, "nobody owns anything yet")
        return
    table = [["tier", "owned", "holders", "value"]]
    for stat in rows:
        table.append(
            [
                f"{stat.rarity.emoji} {stat.rarity.label}",
                money(stat.owned),
                money(stat.holders),
                money(stat.value),
            ]
        )
    builder = RichMessageBuilder().heading("💎 who holds what", size=2).table(table, compact=True)
    await card(
        message, ctx, builder=builder, html="\n".join(f"{row[0]}: {row[1]}" for row in table[1:])
    )


@router.message(Command("hcompletion", "completion"))
async def hcompletion(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, access: Access
) -> None:
    from waifu.plugins._kit import Args, resolve_user

    args = Args.of(command)
    target = await resolve_user(session, message, args.raw) or (access.user_id if access else 0)
    data = await ctx.hstats.completion(session, target)
    builder = RichMessageBuilder().heading(f"🧩 completion for {mention(target)}", size=2)
    rows = [
        [
            key.replace("_", " ").title(),
            (f"{value:.1f}%" if isinstance(value, float) else money(value)),
        ]
        for key, value in data.items()
    ]
    builder.table(rows, compact=True, bordered=False)
    overall = float(data.get("overall", 0.0) or 0.0)
    builder.line(bar(overall / 100, 1))
    await card(message, ctx, builder=builder, html="\n".join(f"{row[0]}: {row[1]}" for row in rows))


@router.message(Command("htop", "topcollectors"))
async def htop(message: Message, ctx: AppContext, session: Any, command: CommandObject) -> None:
    from waifu.plugins._kit import Args

    args = Args.of(command)
    kind = (
        args.first.lower()
        if args.first.lower() in {"collection", "value", "high", "claims"}
        else "collection"
    )
    rows = await ctx.hstats.leaderboard(session, kind=kind, limit=10)
    if not rows:
        await text(message, ctx, "nothing to rank yet")
        return
    table = [["#", "player", kind]]
    for index, row in enumerate(rows, start=1):
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(index, str(index))
        table.append(
            [
                medal,
                mention(int(row.get("user_id", 0)), str(row.get("name") or "")),
                money(int(row.get("score", 0) or 0)),
            ]
        )
    await card(
        message,
        ctx,
        builder=RichMessageBuilder()
        .heading(f"🏆 harem {kind} top", size=2)
        .table(table, compact=True),
        html="\n".join(f"{row[0]}. {row[1]} — {row[2]}" for row in table[1:]),
    )


@router.message(Command("hnote", "setnote"))
async def hnote(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, access: Access
) -> None:
    """A pinned one-liner under /hstats (group owners asked for "rules here")."""
    from waifu.plugins._kit import Args

    if is_private(message.chat):
        await text(message, ctx, "notes are per group")
        return
    if not (access.is_group_admin or access.is_staff):
        await text(message, ctx, "group admins only.")
        return
    args = Args.of(command)
    await ctx.hstats.set_chat_note(session, message.chat.id, args.raw[:200])
    await text(
        message, ctx, "📌 note saved" + (f": {args.raw[:120]}" if args.raw else " (cleared)")
    )
