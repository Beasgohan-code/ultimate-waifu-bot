"""``/ai``, ``/ask``, ``/charai``, ``/aihistory``, ``/aforget``, ``/setai``.

The reference bot had no AI surface at all, so the design constraints here come from
cost control rather than novelty: a per-user daily character budget
(``AiService.budget``), a persona pulled from the roster row (so the character's
``persona``/``voice_line``/``description`` columns are the prompt, not a copy-pasted
string per deployment), and streaming via **message drafts** (Bot API 9.5) where the
server supports them — a token-by-token fake with ``editMessageText`` is what got other
bots rate-limited, and drafts are the endpoint Telegram built for exactly this.

If the model is not configured the commands say so and cost nothing, which is the
behaviour a group owner needs: /ai failing loudly is better than a half-reply that
burns their API key.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from waifu.errors import AISetupError, NotFound, RateLimited, WaifuError
from waifu.plugins._kit import (
    Args,
    RichMessageBuilder,
    card,
    money,
    refuse,
    staff_of,
    text,
)

if TYPE_CHECKING:  # pragma: no cover
    from waifu.core.access import Access
    from waifu.core.context import AppContext

router = Router(name="ai")


@router.message(Command("ai", "ask", "talk"))
async def ai(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, access: Access
) -> None:
    """/ai <text> — talk to your featured character (or /ai @name <text>)."""
    if not ctx.features.is_enabled("ai"):
        await text(message, ctx, "AI replies are switched off on this instance (FEATURE_AI=0).")
        return
    args = Args.of(command)
    if not args.raw:
        await text(
            message,
            ctx,
            "Ask something: <code>/ai how was your day</code> · <code>/ai Gojo how was your day</code>",
        )
        return
    character, prompt = await _pick_character(session, ctx, message, args)
    if character is None:
        await text(
            message,
            ctx,
            "You need a featured character first — <code>/fav &lt;name&gt;</code>, or name one in the message.",
        )
        return
    try:
        reply = await ctx.ai.chat(
            session,
            user_id=access.user_id,
            character_id=int(character.id),
            text=prompt,
            language=str(getattr(access.user, "locale", "en") or "en"),
        )
    except (AISetupError, RateLimited) as exc:
        await refuse(message, exc.user_message)
        return
    except WaifuError as exc:
        await refuse(message, exc.user_message)
        return
    body = reply if isinstance(reply, str) else str((reply or {}).get("text") or "")
    if not body:
        await text(message, ctx, "the model returned nothing — /aiusage shows the budget")
        return
    streamed = False
    if ctx.wants("drafts") and len(body) > 80:
        # A draft streams the answer in place (no edit-spam, no flood wait), and the
        # stop button is Telegram's own — /ai is the one place the length justifies it.
        from waifu.tg.draft import stream_lines

        try:
            streamed = bool(
                await stream_lines(ctx.bot, message.chat.id, body, reply_to=message.message_id)
            )
        except Exception:  # pragma: no cover - server without draft support
            streamed = False
    if not streamed:
        await text(message, ctx, f"🤖 <b>{character.name}</b>\n{body[:3500]}")


async def _pick_character(
    session: Any, ctx: AppContext, message: Message, args: Args
) -> tuple[Any, str]:
    from waifu.db.repo import users as user_repo

    head = args.first
    character = None
    prompt = args.raw
    player = await user_repo.get(session, message.from_user.id if message.from_user else 0)
    if head and not head.startswith('"'):
        try:
            candidate = await ctx.collection.find(session, head)
        except NotFound:
            candidate = None
        if candidate is not None:
            character, prompt = candidate, " ".join(args.words[1:]) or args.raw
    if character is None and player is not None:
        pref = await user_repo.prefs(session, player.id)
        if getattr(pref, "featured_character_id", None):
            character = await ctx.collection.character(session, int(pref.featured_character_id))
    if character is None:
        favourite = await ctx.collection.favourite(session, player.id) if player else None
        if favourite is not None:
            character = await ctx.collection.character(session, int(favourite.character_id))
    return character, (prompt or args.raw)


@router.message(Command("charai"))
async def charai(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, access: Access
) -> None:
    """Set/reset the persona for a character (admins; the roster's ``persona`` column)."""
    staff_of(access)
    args = Args.of(command)
    if not args.raw:
        await text(
            message,
            ctx,
            "Usage: <code>/charai &lt;name&gt; | &lt;persona text&gt;</code> — the persona is prepended to every /ai reply for that character.",
        )
        return
    name, _, persona = args.raw.partition("|")
    try:
        character = await ctx.collection.find(session, name.strip())
    except NotFound:
        await text(message, ctx, f"No character called “{name.strip()[:40]}”.")
        return
    from waifu.db.repo import characters as char_repo

    await char_repo.create_or_update(
        session, name=character.name, anime=character.anime or "", persona=persona.strip()[:1500]
    )
    await text(
        message,
        ctx,
        f"🧠 persona for <b>{character.name}</b> {'updated' if persona.strip() else 'cleared'}.",
    )


@router.message(Command("aiusage", "aibudget"))
async def aiusage(message: Message, ctx: AppContext, session: Any, access: Access) -> None:
    budget = await ctx.ai.budget(session, access.user_id)
    rows = [
        [key.replace("_", " ").title(), money(value) if isinstance(value, int) else str(value)]
        for key, value in budget.items()
    ]
    if ctx.features.is_enabled("ai"):
        report = await ctx.ai.usage_report(session, days=1)
        rows += [
            [f"instance {key.replace('_', ' ')}", money(value)] for key, value in report.items()
        ]
    await card(
        message,
        ctx,
        builder=RichMessageBuilder()
        .heading("🧠 ai budget", size=2)
        .table(rows, compact=True, bordered=False),
        html="\n".join(f"{row[0]}: {row[1]}" for row in rows),
    )


@router.message(Command("aihistory", "aiexport"))
async def aihistory(message: Message, ctx: AppContext, session: Any, access: Access) -> None:
    """GDPR-shaped: the player can read and delete their own transcripts."""
    export = await ctx.ai.export(session, access.user_id)
    if not export.strip():
        await text(message, ctx, "no stored ai history for you")
        return

    from aiogram.types import BufferedInputFile

    await message.answer_document(
        BufferedInputFile(export.encode("utf-8"), filename="ai-history.txt"),
        caption=f"{len(export):,} characters — /aforget erases it",
    )


@router.message(Command("aforget", "forget"))
async def aforget(message: Message, ctx: AppContext, session: Any, access: Access) -> None:
    removed = await ctx.ai.forget(session, access.user_id)
    await text(message, ctx, f"🗑️ removed {money(removed)} stored ai message(s).")


@router.callback_query(F.data.startswith("ai:noop"))
async def noop(callback_query: Any) -> None:
    await callback_query.answer(cache_time=3600)
