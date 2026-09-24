"""Character requests: the polite door to the admin-curated roster.

The roster is content the owner curates (media first, via ``/upload``) — but
"please add <character>" arrives anyway, in DMs and comments, where it gets
lost. ``/request <Name> <Series>`` gives it a queue: players ask, the owner
sees the deduped list in ``/requests`` with one-tap approve/decline, and the
asker is told the outcome. Approving does not add the character — it flags it
for the owner (who still uploads the art); the queue is a promise with a name,
not a backdoor into the catalogue.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message

from waifu.db.repo import characters as char_repo
from waifu.plugins._kit import Args, cb, mention, note, staff_of, text

if TYPE_CHECKING:  # pragma: no cover
    from waifu.core.access import Access
    from waifu.core.context import AppContext

router = Router(name="requests")


@router.message(Command("request", "suggest", "requestchar"))
async def request_character(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, access: Access
) -> None:
    """/request <Name> <Series> [note] — ask the owner to add a character."""
    args = Args.of(command)
    words = args.words
    if len(words) < 2:
        await text(
            message,
            ctx,
            "Usage: <code>/request Kiyomi Fate/Grand Order</code>\n"
            "First word = the character's name, the rest = series (and an optional note "
            "in “quotes”). The owner sees every request in <code>/requests</code>.",
        )
        return
    name = words[0]
    series_words = list(words[1:])
    note = ""
    # An optional trailing quoted note: /request Kiyomi FGO "seen in the anime"
    if series_words and series_words[0].startswith(("“", '"')):
        note = series_words[0].strip('“”"')
        series_words = series_words[1:]
    series = " ".join(series_words)[:96]

    # Already in the roster? Say so — the most common "request" is a search miss.
    # Series-qualified first (exact ask), then the fuzzy resolver (a "Naruto"
    # request points at "Naruto Uzumaki" instead of queuing a near-duplicate).
    from waifu.errors import NotFound as _NotFound

    existing = await char_repo.by_name(session, name, series) or await char_repo.by_name(
        session, name
    )
    if existing is None:
        try:
            existing = await char_repo.find_one(session, name)
        except _NotFound:
            existing = None
    if existing is not None:
        await text(
            message,
            ctx,
            f"🔎 <b>{existing.name}</b> is already in the roster — <code>/check {existing.name}</code>.",
        )
        return
    # Someone else already asked for this one? One request per character, not a queue of clones.
    duplicate = await char_repo.find_pending_request(session, name, series)
    if duplicate is not None:
        await text(
            message,
            ctx,
            f"📋 already requested as <code>#{duplicate.id}</code> — the owner will see it. "
            "No need to ask twice.",
        )
        return
    row = await char_repo.submit_request(
        session, name=name, series=series, requester_id=access.user_id, note=note
    )
    await text(
        message,
        ctx,
        f"📋 request <code>#{row.id}</code> sent — <b>{row.name}</b>"
        + (f" ({row.series})" if row.series else "")
        + ". The owner sees it in <code>/requests</code> and you'll be told the outcome.",
    )


@router.message(Command("requests", "requestlist"))
async def requests_queue(message: Message, ctx: AppContext, session: Any, access: Access) -> None:
    """/requests — the pending queue, with one-tap approve/decline for staff."""
    staff_of(access)
    rows = await char_repo.pending_requests(session)
    if not rows:
        await text(message, ctx, "📋 no pending character requests.")
        return
    lines = [
        f"<code>#{row.id}</code> <b>{row.name}</b>"
        + (f" — {row.series}" if row.series else "")
        + f" · by {mention(row.requester_id)}"
        + (f" · “{row.note[:60]}”" if row.note else "")
        for row in rows
    ]
    buttons = [
        [
            InlineKeyboardButton(text=f"✅ #{row.id}", callback_data=cb("req", "ok", row.id)),
            InlineKeyboardButton(text=f"❌ #{row.id}", callback_data=cb("req", "no", row.id)),
        ]
        for row in rows
    ]
    await text(
        message,
        ctx,
        f"📋 {len(rows)} pending request(s) — approve, then <code>/upload</code> the art:\n"
        + "\n".join(lines),
        buttons=buttons,
    )


@router.callback_query(F.data.startswith("req:"))
async def request_decide(
    callback_query: CallbackQuery, ctx: AppContext, session: Any, access: Access
) -> None:
    """The ✅/❌ buttons on /requests — decide one request, tell the asker."""
    staff_of(access)
    _, decision, raw_id = (callback_query.data or "").split(":", 2)
    request_id = int(raw_id or 0)
    if not request_id:
        await note(callback_query, "expired — /requests for the current queue", alert=True)
        return
    if decision == "ok":
        from waifu.utils.text import esc

        row = await char_repo.decide_request(
            session, request_id, decision="approved", decided_by=access.user_id
        )
        await note(callback_query, f"#{row.id} approved — /upload the art when ready")
        await ctx.notify(
            f"📋 request #{row.id} approved by {access.user_id}: {row.name} "
            f"({row.series}) — requested by {row.requester_id}",
            silent=True,
        )
        await _tell_asker(
            ctx,
            row.requester_id,
            (
                f"🎉 your request for <b>{esc(row.name)}</b>"
                + (f" — {esc(row.series)}" if row.series else "")
                + " was <b>approved</b>. The owner will add it when the art is ready."
            ),
        )
    else:
        row = await char_repo.decide_request(
            session, request_id, decision="declined", decided_by=access.user_id
        )
        await note(callback_query, f"#{row.id} declined")
        await ctx.notify(
            f"📋 request #{row.id} declined by {access.user_id}: {row.name} ({row.series})",
            silent=True,
        )
        await _tell_asker(
            ctx,
            row.requester_id,
            f"😔 your request for <b>{row.name}</b>"
            + (f" — {row.series}" if row.series else "")
            + " was declined this time. You can ask again later.",
        )


async def _tell_asker(ctx: AppContext, user_id: int, html: str) -> None:
    """The outcome DM — a blocked player simply never hears back (no error)."""
    if ctx.bot is None:
        return
    from waifu.tg.notify import safe_send

    await safe_send(ctx.bot, user_id, html, parse_mode="HTML")
