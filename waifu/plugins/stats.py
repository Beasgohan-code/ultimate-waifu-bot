"""``/top``, ``/lb``, ``/server``, ``/trend``, ``/usage`` — the numbers people argue about.

Their leaderboards were rebuilt by ``SELECT``-ing every user into Python, sorting in
memory and caching in a dict, so at ~50k players /top took seconds and timed out under
load. Here the ranking is a single indexed SQL read with a Redis sorted-set fast path
(:mod:`waifu.db.repositories.users`) and offsets, and the player's own rank is fetched
with ``rank_of`` instead of "find yourself in the top 10".

``/server`` answers the question group owners actually ask ("is this bot alive in my
chat?") from the same tables, so no separate telemetry stack is required.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

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
    pager_row,
    text,
)
from waifu.utils.chats import is_private

if TYPE_CHECKING:  # pragma: no cover
    from waifu.core.access import Access
    from waifu.core.context import AppContext

router = Router(name="stats")
KINDS = {
    "coins": "coins",
    "pulls": "summons",
    "value": "harem value",
    "claims": "claims",
    "xp": "xp",
    "streak": "streak",
}


@router.message(Command("top", "lb", "leaderboard", "rank"))
async def top(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, access: Access
) -> None:
    args = Args.of(command)
    kind = next((token.lower() for token in args.words if token.lower() in KINDS), "coins")
    page = max(0, args.paged(1) - 1)
    await send_top(message, ctx, session, kind=kind, page=page, viewer=access.user_id)


async def send_top(
    event: Message | CallbackQuery,
    ctx: AppContext,
    session: Any,
    *,
    kind: str,
    page: int,
    viewer: int,
) -> None:
    limit = 10
    rows, total = await ctx.stats.leaderboard(session, kind, limit=limit, offset=page * limit)
    mine, of_total = await ctx.stats.rank_of(session, viewer, kind=kind)
    builder = RichMessageBuilder().heading(f"🏆 top {KINDS.get(kind, kind)}", size=1)
    if rows:
        table = [["#", "player", KINDS.get(kind, kind), "extra"]]
        for row in rows:
            medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(int(row.rank), str(row.rank))
            table.append(
                [
                    medal,
                    mention(int(row.user_id), row.name or "")
                    + (" ← you" if int(row.user_id) == viewer else ""),
                    money(row.score),
                    str(row.extra or ""),
                ]
            )
        builder.table(table, compact=True)
    else:
        builder.paragraph(html="<i>no data yet — the first /daily or /pull puts someone here</i>")
    builder.divider()
    builder.line(f"your rank: #{money(mine)} of {money(of_total or 0)}")
    pages = max(1, -(-total // limit)) if total else 1
    buttons = [pager_row(page=page, pages=pages, prefix="top", extra=f"{kind}/{page}")]
    buttons.append(
        [
            callback(f"📊 {label}", cb("top", "kind", key, str(page)), disabled=key == kind)
            for key, label in list(KINDS.items())[:4]
        ]
    )
    html = "\n".join(f"{row.rank}. {row.name or row.user_id} — {money(row.score)}" for row in rows)
    if isinstance(event, CallbackQuery):
        await edit(event, ctx, builder=builder, html=html, buttons=buttons)
    else:
        await card(event, ctx, builder=builder, html=html, buttons=buttons)


@router.callback_query(F.data.regexp(r"^top:(first|prev|next|last):"))
async def top_page(
    callback_query: CallbackQuery, ctx: AppContext, session: Any, access: Access
) -> None:
    parts = (callback_query.data or "").split(":")
    direction = parts[1]
    kind, page_raw = ([*(parts[2] if len(parts) > 2 else "coins/0").split("/"), "0"])[:2]
    page = int(page_raw or 0)
    page = (
        page + 1
        if direction == "next"
        else max(0, page - 1)
        if direction == "prev"
        else (0 if direction == "first" else page)
    )
    await send_top(callback_query, ctx, session, kind=kind, page=page, viewer=access.user_id)


@router.callback_query(F.data.startswith("top:kind:"))
async def top_kind(
    callback_query: CallbackQuery, ctx: AppContext, session: Any, access: Access
) -> None:
    parts = (callback_query.data or "").split(":")
    kind = parts[2] if len(parts) > 2 and parts[2] in KINDS else "coins"
    await send_top(callback_query, ctx, session, kind=kind, page=0, viewer=access.user_id)


@router.message(Command("server", "stats", "groupstats", "chatstats"))
async def server(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, access: Access
) -> None:
    """``/server`` — this chat's numbers; ``/server all`` — the whole instance."""
    args = Args.of(command)
    if args.first.lower() in {"all", "global", "bot"}:
        data = await ctx.stats.global_(session, hours=24)
        table = [
            [
                key.replace("_", " ").title(),
                money(value)
                if isinstance(value, int)
                else (f"{value:.1f}" if isinstance(value, float) else str(value)),
            ]
            for key, value in data.items()
        ]
        await card(
            message,
            ctx,
            builder=RichMessageBuilder()
            .heading("🌐 instance (24h)", size=2)
            .table(table, compact=True, bordered=False),
            html="\n".join(f"{row[0]}: {row[1]}" for row in table),
        )
        return
    if is_private(message.chat):
        await text(
            message,
            ctx,
            "Run /server in a group to see that group's numbers, or <code>/server all</code> for the whole bot.",
        )
        return
    stats = await ctx.stats.group(session, message.chat.id)
    builder = (
        RichMessageBuilder()
        .heading(f"📈 {stats.title or 'this group'}", size=2)
        .table(
            [
                ["messages counted", money(stats.messages)],
                ["spawns", f"{money(stats.spawns_total)} · {money(stats.spawns_claimed)} claimed"],
                ["guess rounds", money(stats.guesses)],
                ["players seen", money(stats.unique_members)],
                ["registered", str(stats.registered_at)[:10]],
            ],
            compact=True,
            bordered=False,
        )
    )
    if stats.top_claimers:
        builder.divider().heading("most claims", size=3).line(
            " · ".join(
                f"{mention(int(user_id), name)} ×{count}"
                for user_id, name, count in [tuple(item) for item in stats.top_claimers[:5]]
            )
        )
    await card(
        message,
        ctx,
        builder=builder,
        html=f"messages {stats.messages} · spawns {stats.spawns_total}/{stats.spawns_claimed} claimed",
    )


@router.message(Command("trend", "activity"))
async def trend(message: Message, ctx: AppContext, session: Any) -> None:
    rows = await ctx.stats.trend(session, hours=24)
    if not rows:
        await text(message, ctx, "no hourly activity recorded yet")
        return
    peak = max((int(row[1]) for row in rows), default=1) or 1
    builder = RichMessageBuilder().heading("📊 24h activity", size=2)
    table = [["hour", "pulls", "claims"]]
    for label, pulls, claims in rows[-12:]:
        table.append(
            [
                str(label)[-5:],
                f"{bar(pulls, peak, width=8)} {money(pulls)}",
                f"{bar(claims, peak, width=8)} {money(claims)}",
            ]
        )
    builder.table(table, compact=True)
    await card(
        message,
        ctx,
        builder=builder,
        html="\n".join(f"{row[0]}: {row[1]} pulls" for row in table[1:]),
    )


@router.message(Command("usage"))
async def usage(message: Message, ctx: AppContext, session: Any) -> None:
    rows = await ctx.stats.command_usage(limit=12)
    if not rows:
        await text(message, ctx, "no commands recorded since boot")
        return
    total = sum(count for _, count in rows) or 1
    table = [["command", "uses", "share"]]
    for name, count in rows:
        table.append([f"<code>/{name}</code>", money(count), f"{count / total * 100:.0f}%"])
    await card(
        message,
        ctx,
        builder=RichMessageBuilder().heading("⌨️ command usage", size=2).table(table, compact=True),
        html="\n".join(f"/{name}: {count}" for name, count in rows),
    )
