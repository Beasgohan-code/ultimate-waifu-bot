"""``/streak``, ``/quests``, ``/achievements``, ``/badges``, ``/freeze``.

The reference bot had a streak counter and nothing else: no visible goal, no way to
protect a run, and a ``last_daily`` column compared with ``datetime.now()`` — so a
player in JST and a player in BRT "shared" a reset time and one of them always lost a
day. Here the day boundary is per player (``utc_offset_hours``, set by /daily +5:30 or
from their Telegram profile), a freeze is a purchasable buffer, and the achievement
definitions live in one table next to the metric they read.

``/checklist`` (Bot API 10.1) renders today's quests as a real Telegram checklist when
the server supports it; everywhere else it degrades to the same list with ▰▰▱ bars,
which is the pattern the whole bot uses for new-API features.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from waifu.errors import AlreadyClaimed, NotFound, WaifuError
from waifu.plugins._kit import (
    RichMessageBuilder,
    bar,
    callback,
    card,
    cb,
    edit,
    money,
    note,
)

if TYPE_CHECKING:  # pragma: no cover
    from waifu.core.access import Access
    from waifu.core.context import AppContext

router = Router(name="progress")


@router.message(Command("streak", "daystreak"))
async def streak(message: Message, ctx: AppContext, session: Any, access: Access) -> None:
    state = await ctx.progress.streak(session, access.user_id)
    multiplier = ctx.progress.multiplier_for(state.current)
    builder = (
        RichMessageBuilder()
        .heading(f"🔥 {state.current} day streak", size=1)
        .table(
            [
                ["best", money(state.best)],
                ["multiplier", f"× {multiplier:g}"],
                ["today", "claimed ✅" if state.claimed_today else "not claimed — /daily"],
                ["freezes", f"{state.freezes} available"],
            ],
            compact=True,
            bordered=False,
        )
        .line(bar(min(state.current, 7), 7))
        .footer(
            "a missed day costs the multiplier, never your characters (theirs could reset to zero)"
        )
    )
    await card(
        message,
        ctx,
        builder=builder,
        html=f"streak {state.current} (best {state.best}) · ×{multiplier:g} · freezes {state.freezes}",
        buttons=[
            [callback("🧊 buy a freeze", cb("prog", "freeze"))],
            [callback("📜 quests", cb("prog", "quests"))],
        ],
    )


@router.callback_query(F.data == "prog:freeze")
async def buy_freeze(
    callback_query: CallbackQuery, ctx: AppContext, session: Any, access: Access
) -> None:
    try:
        total = await ctx.progress.purchase_freeze(session, access.user_id)
    except (AlreadyClaimed, NotFound, WaifuError) as exc:
        await note(callback_query, exc.user_message, alert=True)
        return
    await note(callback_query, f"freeze banked — {total} ready", alert=True)


@router.message(Command("quests", "dailies", "missions"))
async def quests(message: Message, ctx: AppContext, session: Any, access: Access) -> None:
    await send_quests(message, ctx, session, user_id=access.user_id)


@router.callback_query(F.data == "prog:quests")
async def quests_button(
    callback_query: CallbackQuery, ctx: AppContext, session: Any, access: Access
) -> None:
    await send_quests(callback_query, ctx, session, user_id=access.user_id)


async def send_quests(
    event: Message | CallbackQuery, ctx: AppContext, session: Any, *, user_id: int
) -> None:
    rows = await ctx.progress.quests(session, user_id)
    payload, total_reward = await ctx.progress.checklist_payload(session, user_id)
    if ctx.wants("checklist") and isinstance(event, Message):
        # A real checklist (Bot API 10.1): the player ticks it in Telegram and the
        # ``ticked_task`` entity is what /verify-style tooling reads back — no bot-side
        # message to re-render, so it cannot go stale the way their /quests caption did.
        from waifu.tg.checklist import ChecklistCard, send_checklist

        card_obj = ChecklistCard(title=f"Today's quests · {money(total_reward)} 🪙 total")
        for index, (label, need, done_flag) in enumerate(payload, start=1):
            card_obj.add(f"{label} ({need})", task_id=index, done=bool(done_flag))
        sent = await send_checklist(
            ctx.bot, event.chat.id, card_obj, thread_id=event.message_thread_id
        )
        if sent is not None:
            return
    builder = RichMessageBuilder().heading("📜 today's quests", size=1)
    table = [["quest", "progress", "reward", ""]]
    buttons: list[list[Any]] = []
    for quest in rows:
        table.append(
            [
                f"{quest.emoji} <b>{quest.label}</b>\n{quest.target}× {quest.key}",
                f"{bar(min(quest.progress, quest.target), quest.target)} {min(quest.progress, quest.target)}/{quest.target}",
                f"{money(quest.reward)} 🪙",
                "✅" if quest.claimed else "",
            ]
        )
        if quest.progress >= quest.target and not quest.claimed:
            buttons.append(
                [
                    callback(
                        f"claim {quest.label} · {money(quest.reward)}🪙",
                        cb("prog", "claim", quest.key),
                    )
                ]
            )
    builder.table(table, compact=True)
    if not buttons:
        buttons.append([callback("nothing to claim yet", cb("prog", "noop"), disabled=True)])
    html = "\n".join(
        f"{quest.emoji} {quest.label}: {quest.progress}/{quest.target}" for quest in rows
    )
    if isinstance(event, CallbackQuery):
        await edit(event, ctx, builder=builder, html=html, buttons=buttons)
    else:
        await card(event, ctx, builder=builder, html=html, buttons=buttons)


@router.callback_query(F.data.startswith("prog:claim:"))
async def claim_quest(
    callback_query: CallbackQuery, ctx: AppContext, session: Any, access: Access
) -> None:
    key = (callback_query.data or "").split(":")[-1]
    try:
        reward = await ctx.progress.claim_quest(session, access.user_id, key)
    except (AlreadyClaimed, NotFound, WaifuError) as exc:
        await note(callback_query, exc.user_message, alert=True)
        return
    await note(callback_query, f"quest paid: {money(reward)} 🪙", alert=True)
    await send_quests(callback_query, ctx, session, user_id=access.user_id)


@router.message(Command("achievements", "badges", "ach"))
async def achievements(message: Message, ctx: AppContext, session: Any, access: Access) -> None:
    unlocked = await ctx.progress.list_for(session, access.user_id)
    fresh = await ctx.progress.evaluate(session, access.user_id)
    board = await ctx.progress.summary(session, access.user_id)
    builder = RichMessageBuilder().heading(f"🏅 badges — {len(unlocked)} unlocked", size=1)
    rows = [str(item.key) for item in fresh]
    table = [["badge", "progress", "bonus"]]
    for achievement in (board.rows if hasattr(board, "rows") else unlocked)[:14]:
        state = (
            "✅"
            if achievement.unlocked
            else f"{min(int(achievement.progress), int(achievement.target))}/{achievement.target}"
        )
        table.append(
            [
                f"{achievement.emoji} <b>{achievement.title}</b>\n{achievement.description}",
                state,
                money(achievement.bonus) if achievement.bonus else "—",
            ]
        )
    builder.table(table, compact=True)
    if rows:
        builder.footer("🆕 unlocked just now: " + ", ".join(rows[:6]))
    await card(
        message,
        ctx,
        builder=builder,
        html="\n".join(
            f"{a.emoji} {a.title}: {'done' if a.unlocked else f'{a.progress}/{a.target}'}"
            for a in (board.rows if hasattr(board, "rows") else unlocked)[:14]
        ),
    )


@router.message(Command("weekly", "summary"))
async def weekly(message: Message, ctx: AppContext, session: Any, access: Access) -> None:
    data = await ctx.progress.weekly_summary(session, access.user_id)
    table = [
        [key.replace("_", " ").title(), money(value) if isinstance(value, int) else str(value)]
        for key, value in data.items()
    ]
    await card(
        message,
        ctx,
        builder=RichMessageBuilder()
        .heading("📊 your week", size=2)
        .table(table or [["quiet week", "—"]], compact=True, bordered=False),
        html="\n".join(f"{row[0]}: {row[1]}" for row in table),
    )
