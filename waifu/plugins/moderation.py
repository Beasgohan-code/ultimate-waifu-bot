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

import logging
from typing import TYPE_CHECKING, Any

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

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
    split_cb,
    staff_of,
    text,
)
from waifu.utils.chats import is_private

if TYPE_CHECKING:  # pragma: no cover
    from waifu.core.access import Access
    from waifu.core.context import AppContext

log = logging.getLogger(__name__)

router = Router(name="moderation")


@router.message(Command("warn"))
async def warn(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, access: Access
) -> None:
    """/warn @player reason — the ladder decides what happens at 3, 4 and 5."""
    if is_private(message.chat):
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
    if is_private(message.chat):
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


# ------------------------------------------------------------------ join-request gate
#
# ``chat_join_request`` was subscribed by this bot's dispatcher from day one and handled by
# nobody — a group whose owner turned on "request admin approval" simply got no answer at all.
# The reference bot never had this feature (it predates the API method), so there is nothing to
# port and everything to design: the joiner is quizzed in a DM about the *roster the group
# actually plays with*, because a person who has never seen a single character is exactly the
# account that shows up to post ads.
#
# Design notes worth keeping:
#
# * no table, no migration. A pending join is a 15-minute, three-try state, so it lives in
#   :class:`~waifu.db.state.Cache` (Redis when configured, TTL map otherwise) keyed by
#   ``(chat_id, user_id)`` — which is also what Telegram uses to identify a join request.
# * an empty roster must never lock a group: with fewer than four characters the gate answers
#   ``approve`` immediately, so a fresh install can still turn the feature on.
# * the question is only ever asked in a private chat with the joiner; wrong answers are silent
#   in the group, and exhaustion declines with a reason the joiner actually sees.
GATE_TTL = 900
GATE_TRIES = 3
GATE_NS = "gate"
#: The question bank, per group (see :func:`_gate_question`).
GATE_POOL_NS = "gatepool"
GATE_POOL_TTL = 3600


@router.message(Command("gate"))
async def gate(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, access: Access
) -> None:
    """``/gate on|off`` — quiz joiners in a DM before they reach the group."""
    if is_private(message.chat):
        await text(message, ctx, "The gate guards a group. Run this in the group.")
        return
    _require_group_admin(access, message)
    wanted = Args.of(command).first.lower()
    current = await ctx.spawn.switch(session, message.chat.id, "gate")
    enabled = (
        not current if wanted not in {"on", "off", "true", "false"} else wanted in {"on", "true"}
    )
    await ctx.spawn.set_switch(
        session, message.chat.id, "gate", value=enabled, title=message.chat.title or ""
    )
    extra = (
        "\nNothing to quiz yet: the roster has fewer than four characters, so joiners are "
        "approved until you add some."
        if enabled
        else ""
    )
    await text(
        message,
        ctx,
        (
            f"🚪 join gate <b>{'on' if enabled else 'off'}</b> for this group."
            f"{' ' + extra if extra else ''}\nUse /gatelink to make an invite that routes people through it."
        ),
    )


@router.message(Command("gatelink", "joinlink"))
async def gate_link(message: Message, ctx: AppContext, session: Any, access: Access) -> None:
    """``/gatelink`` — a single-use invite with approval requests forced on.

    ``creates_join_request`` is the flag that generates the ``chat_join_request`` update in the
    first place; an admin who forgets it gets a link that bypasses the gate entirely, so the bot
    sets it rather than telling them to.
    """
    if is_private(message.chat):
        await text(message, ctx, "Group command.")
        return
    _require_group_admin(access, message)
    bot = ctx.bot
    if bot is None:  # pragma: no cover - only in unit tests
        await text(message, ctx, "No bot handle available.")
        return
    try:
        link = await bot.create_chat_invite_link(
            chat_id=message.chat.id,
            name=f"waifu gate {message.chat.id}",
            creates_join_request=True,
            member_limit=50,
        )
    except Exception as exc:  # the API error *is* the useful message
        await text(message, ctx, f"Could not create the link: {esc_short(exc)}")
        return
    await text(
        message,
        ctx,
        f"🔗 <code>{link.invite_link}</code>\n"
        "Joiners must answer a question in a DM first (single use, 50 joins, revoke with "
        "/gate off … no: revoke it in the group's invite list).",
    )


def esc_short(value: object) -> str:
    from waifu.utils.text import esc, truncate

    return truncate(esc(str(value)), 160)


@router.chat_join_request()
async def join_gate(event: Any, ctx: AppContext) -> None:
    """The update nobody handled: ask the question, or approve when there is nothing to ask."""
    chat_id = int(getattr(getattr(event, "chat", None), "id", 0) or 0)
    user_id = int(getattr(getattr(event, "from_user", None), "id", 0) or 0) or int(
        getattr(event, "user_id", 0) or 0
    )
    if not chat_id or not user_id:
        return
    async with ctx.db.session() as session:
        if not await ctx.spawn.switch(session, chat_id, "gate"):
            return
        question = await _gate_question(ctx, session, chat_id=chat_id)
    if question is None:
        await _approve(event, ctx, "the gate has nothing to ask yet")
        return
    options = list(question["options"])
    answer = int(question["answer"])
    media = str(question.get("media") or "")
    prompt = _gate_prompt(media)
    state = {"answer": answer, "tries": GATE_TRIES, "options": options}
    outcome = await _send_prompt(
        ctx,
        user_id=user_id,
        chat_id=chat_id,
        prompt=prompt,
        options=options,
        media=media,
    )
    if outcome == "blocked":
        await _decline(ctx, chat_id, user_id, "I could not message you to ask the question.")
    elif outcome == "error":
        await _approve(event, ctx, "the gate could not ask its question")
    elif outcome == "sent" and ctx.cache is not None:
        await ctx.cache.set(GATE_NS, (chat_id, user_id), state, GATE_TTL)


async def _send_prompt(
    ctx: AppContext,
    *,
    user_id: int,
    chat_id: int,
    prompt: str,
    options: list[str],
    media: str,
) -> str:
    """DM the question. Returns ``sent`` / ``blocked`` / ``error`` (``skipped`` without a bot).

    Split out because the three outcomes *are* the policy: blocked → decline with a reason,
    error → approve rather than lock the group behind a bug of ours, sent → arm the answer.
    """
    bot = ctx.bot
    if bot is None:  # pragma: no cover - unit tests
        return "skipped"
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=name[:32],
                    callback_data=cb("gate", str(chat_id), str(user_id), str(index)),
                )
            ]
            for index, name in enumerate(options)
        ]
    )
    try:
        if media:
            await bot.send_photo(chat_id=user_id, photo=media, caption=prompt, reply_markup=markup)
        else:
            await bot.send_message(chat_id=user_id, text=prompt, reply_markup=markup)
    except TelegramForbiddenError:
        return "blocked"
    except Exception as exc:  # never strand a joiner on a rendering bug
        log.warning("gate prompt failed for %s: %s", user_id, exc)
        return "error"
    return "sent"


@router.callback_query(F.data.startswith("gate:"))
async def gate_answer(callback_query: CallbackQuery, ctx: AppContext) -> None:
    """Check the pressed option against the cached answer, then approve or decline."""
    parts = split_cb(callback_query.data)
    if len(parts) < 4:
        await callback_query.answer()
        return
    try:
        chat_id, user_id, picked = int(parts[1]), int(parts[2]), int(parts[3])
    except ValueError:
        await callback_query.answer()
        return
    pressed = callback_query.from_user.id if callback_query.from_user else 0
    if ctx.bot is None or pressed != user_id:
        await note(callback_query, "This question was not addressed to you.", alert=True)
        return
    state = await ctx.cache.get(GATE_NS, chat_id, user_id) if ctx.cache else None
    if not state:
        await note(
            callback_query,
            "That question expired — ask the group admins for a fresh invite.",
            alert=True,
        )
        return
    if picked == int(state.get("answer", -1)):
        await _gate_admit(callback_query, ctx, chat_id=chat_id, user_id=user_id)
    else:
        await _gate_miss(callback_query, ctx, state=state, chat_id=chat_id, user_id=user_id)


async def _gate_admit(
    callback_query: CallbackQuery, ctx: AppContext, *, chat_id: int, user_id: int
) -> None:
    """Approve first, celebrate second: an answer must never be lost to a failed toast."""
    await ctx.cache.delete(GATE_NS, chat_id, user_id)
    try:
        await ctx.bot.approve_chat_join_request(chat_id=chat_id, user_id=user_id)
    except TelegramBadRequest as exc:
        await note(callback_query, f"Could not let you in: {exc}", alert=True)
        return
    try:
        await ctx.bot.send_message(chat_id=user_id, text="You're in. Welcome 🙂")
    except Exception:  # the DM is a courtesy
        pass
    await callback_query.answer("correct — welcome")


async def _gate_miss(
    callback_query: CallbackQuery, ctx: AppContext, *, state: dict, chat_id: int, user_id: int
) -> None:
    """One try left per wrong answer; the third one declines, and the reason is shown."""
    tries = int(state.get("tries", 0)) - 1
    if tries <= 0:
        await ctx.cache.delete(GATE_NS, chat_id, user_id)
        await _decline(ctx, chat_id, user_id, "Wrong character — this group is for fans.")
        await callback_query.answer("wrong — the admins were told", show_alert=True)
        return
    state["tries"] = tries
    if ctx.cache is not None:
        await ctx.cache.set(GATE_NS, (chat_id, user_id), state, GATE_TTL)
    await note(callback_query, f"not quite — {tries} tries left", alert=True)


async def _approve(event: Any, ctx: AppContext, why: str) -> None:
    try:
        await event.approve()
    except Exception as exc:  # already a member, no rights, …
        log.info("gate auto-approve failed (%s): %s", why, exc)


async def _decline(ctx: AppContext, chat_id: int, user_id: int, why: str) -> None:
    """Decline with a reason when the server knows that argument, without when it does not."""
    try:
        await ctx.bot.decline_chat_join_request(chat_id=chat_id, user_id=user_id, reason=why[:160])
    except TypeError:  # pragma: no cover - older aiogram has no reason field
        await ctx.bot.decline_chat_join_request(chat_id=chat_id, user_id=user_id)
    except TelegramBadRequest as exc:  # pragma: no cover - the request is already gone
        log.debug("gate decline skipped: %s", exc)


async def _gate_question(ctx: AppContext, session: Any, *, chat_id: int) -> dict[str, Any] | None:
    """Four names from this deployment's own roster, cached per group for an hour.

    The answer sits at a random index on purpose: a quiz whose correct button is always the first
    one is a formality, and "top left" is exactly what a spam farm learns after one attempt.
    ``None`` means "there is nothing to ask" (a roster under four characters) and every caller
    treats that as an open door, not a locked one.

    Cached per chat because ``ORDER BY random()`` on every join request is how a busy group turns
    a security feature into a DB incident; a stale pool costs nothing, since the options are only
    ever *this* roster's names.
    """
    import secrets

    from waifu.db.repositories import characters as char_repo

    async def _load() -> dict[str, Any] | None:
        totals = await char_repo.totals(session)
        if int(totals.get("characters") or 0) < 4:
            return None
        rows: list[Any] = []
        seen: set[int] = set()
        for _ in range(12):  # up to twelve draws to land four distinct characters
            character = await char_repo.random_any(session)
            if character is None or int(character.id) in seen:
                continue
            seen.add(int(character.id))
            rows.append(character)
            if len(rows) >= 4:
                break
        if len(rows) < 4:
            return None
        answer = secrets.randbelow(len(rows))
        return {
            "options": [str(row.name)[:32] for row in rows],
            "answer": answer,
            # Only ever a stored file_id: a join request is not a reason to fetch a stranger's URL.
            "media": str(rows[answer].photo_file_id or ""),
        }

    if ctx.cache is None:  # pragma: no cover - the context always carries one
        return await _load()
    return await ctx.cache.get_or_set(GATE_POOL_NS, (chat_id,), _load, ttl=GATE_POOL_TTL)


def _gate_prompt(media: str) -> str:
    """The two wordings, picked by whether there is art we are allowed to show."""
    if media:
        return (
            "<b>One question before you join</b> — whose art is this?\n"
            "The admins only approve people who actually play here."
        )
    return (
        "<b>One question before you join</b> — which of these is a real character in this "
        "group's roster?\n(One is; the others are invented.)"
    )
