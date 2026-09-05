"""``/nguess`` — the timed guessing round, and ``/ngstats``/``/ngtop``/``/ngskip``.

Their version (a 40-line loop in ``plugins/nguess.py``) picked a character, sent a
caption, and then ``await asyncio.sleep(timeout)`` inside the handler while collecting
answers in a module-level dict. Consequences: restarting the bot mid-round lost every
answer, two rounds in one chat overwrote each other, and "first correct answer wins"
was decided by whichever coroutine woke up first rather than the database.

Here the round is a row (``spawn_guesses``) with the answer claim expressed as
``UPDATE … WHERE winner_id IS NULL``, and ``/nguess`` returns immediately: the scheduler
closes expired rounds (:meth:`SpawnService.guesses_to_close`). The poll variant uses a
real Telegram quiz poll (``type="quiz"``, ``is_anonymous=False``), which gives players a
native answer UI and gives the bot a ``poll_answer`` update to read — no text parsing at
all when the chat allows polls.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

from waifu.enums import Rarity
from waifu.errors import AlreadyClaimed, Locked, WaifuError
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
    staff_of,
    text,
)

if TYPE_CHECKING:  # pragma: no cover
    from waifu.core.access import Access
    from waifu.core.context import AppContext

router = Router(name="nguess")


@router.message(Command("nguess", "guess", "guesschar"))
async def nguess(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, access: Access
) -> None:
    """/nguess [seconds] [rarity] — start a round in this group."""
    if message.chat.is_private:
        await text(
            message,
            ctx,
            "Guessing is a group game — /nguess in your group, and members race to type the name.",
        )
        return
    args = Args.of(command)
    numbers = [int(token) for token in args.words if token.isdigit()]
    seconds = max(20, min(600, numbers[0])) if numbers else int(ctx.settings.guess_timeout_seconds)
    rarity_id = None
    wanted = {rarity.label.lower(): int(rarity.value) for rarity in Rarity}
    for token in args.words:
        if token.lower() in wanted:
            rarity_id = wanted[token.lower()]
    view = await ctx.spawn.active_guess(session, message.chat.id)
    if view is not None:
        await text(
            message,
            ctx,
            f"⏳ a round is already running here ({int(view.seconds_left)}s left). /ngskip ends it.",
        )
        return
    try:
        view = await ctx.spawn.start_guess(
            session,
            chat_id=message.chat.id,
            reward=ctx.settings.guess_reward_coins,
            seconds=seconds,
            mode="text",
            rarity_id=rarity_id,
        )
    except (Locked, AlreadyClaimed, WaifuError) as exc:
        await refuse(message, exc.user_message)
        return
    await _announce(message, ctx, view, mode="text")


async def _announce(message: Message, ctx: AppContext, view: Any, *, mode: str) -> None:
    character = view.character
    rarity = Rarity.from_value(int(getattr(character, "rarity_id", 1) or 1))
    builder = (
        RichMessageBuilder()
        .heading("🕵️ who is this?", size=1)
        .photo(getattr(character, "image_url", "") or "", caption="guess the name")
        if getattr(character, "image_url", "")
        else RichMessageBuilder().heading("🕵️ guess the character", size=1)
    )
    builder.table(
        [
            ["series", getattr(character, "anime", "") or "?"],
            ["tier", f"{rarity.badge} {rarity.label}"],
            ["reward", f"{money(ctx.settings.guess_reward_coins)} 🪙"],
            ["timer", f"{int(view.seconds_left)}s"],
        ],
        compact=True,
        bordered=False,
    )
    builder.paragraph(
        html="First exact name wins — typing is the answer (no command needed)."
        if mode == "text"
        else "Vote on the poll — the first correct voter wins."
    )
    buttons = [
        [callback("⏭ skip round", cb("ng", "skip")), callback("💡 reveal hint", cb("ng", "hint"))]
    ]
    await card(
        message,
        ctx,
        builder=builder,
        html=f"🕵️ guess the character ({rarity.badge}, {int(view.seconds_left)}s)",
        buttons=buttons,
    )


@router.message(Command("ngpoll"))
async def ngpoll(message: Message, ctx: AppContext, session: Any, command: CommandObject) -> None:
    """Same round, native poll UI (four candidate names + the answer is a real quiz)."""
    from aiogram.types import InputPollOption

    args = Args.of(command)
    numbers = [int(token) for token in args.words if token.isdigit()]
    seconds = max(20, min(600, numbers[0])) if numbers else int(ctx.settings.guess_timeout_seconds)
    view = await ctx.spawn.start_guess(
        session,
        chat_id=message.chat.id,
        reward=ctx.settings.guess_reward_coins,
        seconds=seconds,
        mode="poll",
    )
    character = view.character
    options = await _option_names(session, ctx, character)
    try:
        poll = await ctx.bot.send_poll(
            message.chat.id,
            question=f"🕵️ which character is this? ({int(view.seconds_left)}s)",
            options=[InputPollOption(text=name) for name in options],
            type="quiz",
            correct_option_id=options.index(getattr(character, "name", ""))
            if getattr(character, "name", "") in options
            else 0,
            is_anonymous=False,
        )
    except Exception:  # pragma: no cover - chats without poll rights
        await _announce(message, ctx, view, mode="text")
        return
    from waifu.db.repositories import spawns as spawn_repo

    await spawn_repo.attach_guess_message(session, int(view.session.id), int(poll.message_id))
    await text(message, ctx, "👆 vote in the poll — the first correct option wins the reward")


async def _option_names(session: Any, ctx: AppContext, character: Any) -> list[str]:
    from waifu.db.repositories import characters as char_repo

    right = getattr(character, "name", "") or ""
    pool = await char_repo.by_rarity(
        session, int(getattr(character, "rarity_id", 1) or 1), limit=12
    )
    others = [item.name for item in pool if item.name != right][:3]
    names = [right, *others]
    import random

    random.SystemRandom().shuffle(names)
    return names or [right]


@router.message(Command("ngskip", "ngend", "nguess_end"))
async def ngskip(message: Message, ctx: AppContext, session: Any, access: Access) -> None:
    """End the running round early (admins only — otherwise anyone cancels a rival's win)."""
    staff_of(access)
    view = await ctx.spawn.active_guess(session, message.chat.id)
    if view is None:
        await text(message, ctx, "No round running.")
        return
    await ctx.spawn.resolve_guess(session, int(view.session.id), winner_id=None, answers=0)
    await text(message, ctx, "🏁 round closed with no winner.")


@router.message(Command("ngstats"))
async def ngstats(message: Message, ctx: AppContext, session: Any, access: Access) -> None:
    streak, best = await ctx.spawn.guess_streak(session, message.chat.id)
    stats = await ctx.stats.player(session, access.user_id)
    await text(
        message,
        ctx,
        f"🧠 this chat's guess streak: {streak} (best {best or 0})\nyour round count lives in /top claims.",
    )
    del stats


@router.message(Command("ngtop", "guys"))
async def ngtop(message: Message, ctx: AppContext, session: Any) -> None:
    rows = await ctx.spawn.top_guessers(session, limit=10)
    if not rows:
        await text(message, ctx, "nobody has won a round yet")
        return
    lines = [
        f"{index}. {mention(int(getattr(user, 'id', 0)), getattr(user, 'first_name', '') or '')} — {money(count)} win(s)"
        for index, (user, count) in enumerate(rows, start=1)
    ]
    await text(message, ctx, "🧠 <b>best guessers</b>\n" + "\n".join(lines))


@router.callback_query(F.data == "ng:skip")
async def ng_skip_button(
    callback_query: CallbackQuery, ctx: AppContext, session: Any, access: Access
) -> None:
    if callback_query.message is None:
        await callback_query.answer()
        return
    await ngskip(callback_query.message, ctx, session, access=access)


@router.callback_query(F.data == "ng:hint")
async def ng_hint_button(
    callback_query: CallbackQuery, ctx: AppContext, session: Any, access: Access
) -> None:
    view = await ctx.spawn.active_guess(
        session, callback_query.message.chat.id if callback_query.message else 0
    )
    if view is None:
        await note(callback_query, "no round running", alert=True)
        return
    name = getattr(view.character, "name", "") or ""
    first = name[:2].title()
    await note(callback_query, f"hint: {first}… ({len(name)} letters)", alert=True)
