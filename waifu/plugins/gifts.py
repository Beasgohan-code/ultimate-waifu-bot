"""``/gift``, ``/sendgift``, ``/anon``, ``/giftlog`` — sending a character or coins.

Their ``/gift`` moved a character with no receipt the sender could read back, and
``/anon`` (anonymous gifting) was a flag on the message text, so the log still named
the sender. Here the log row is written with the *display* choice attached, and
``/giftlog`` shows each side's view of the same event.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

from waifu.enums import Rarity
from waifu.errors import Locked, NotFound, WaifuError
from waifu.plugins._kit import (
    Args,
    RichMessageBuilder,
    callback,
    card,
    cb,
    edit,
    mention,
    money,
    note,
    resolve_user,
    shorten,
    text,
)

if TYPE_CHECKING:  # pragma: no cover
    from waifu.core.access import Access
    from waifu.core.context import AppContext

router = Router(name="gifts")


@router.message(Command("gift", "sendgift", "givechar"))
async def gift(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, access: Access
) -> None:
    """/gift @player <character id|name> [note]  ·  /gift @player 5000 (coins)"""
    args = Args.of(command)
    if len(args.words) < 2:
        await text(
            message,
            ctx,
            "Usage: <code>/gift @name 42 thanks!</code> for a character, <code>/gift @name 5000</code> for coins.",
        )
        return
    receiver = await resolve_user(session, message, args.first)
    if receiver is None or receiver == access.user_id:
        await text(
            message, ctx, "Who? Reply to their message or use their @username (not yourself)."
        )
        return
    rest = " ".join(args.words[1:])
    anonymous = (message.text or "").lower().startswith(
        ("/anon", "/gift --anon")
    ) or "--anon" in rest
    tail = " ".join(rest.split()[1:]) if rest.split() and rest.split()[0].isdigit() else ""
    if rest.isdigit() or (
        rest.split()
        and rest.split()[0].isdigit()
        and len(rest.split()[0]) > 3
        and " " not in rest.strip()
    ):
        pass
    head = rest.split(maxsplit=1)[0]
    if head.isdigit() and len(head) <= 9 and not head.endswith("000"):
        character = None
        try:
            character = await ctx.collection.find(session, head)
        except NotFound:
            character = None
        if character is not None:
            result = await ctx.gifts.character(
                session,
                access.user_id,
                receiver,
                int(character.id),
                note=tail or ("anonymous admirer" if anonymous else ""),
                anonymous=anonymous,
            )
            await _announce(
                message,
                ctx,
                result,
                sender=access.user_id,
                receiver=receiver,
                anonymous=anonymous,
                character=character,
            )
            return
    amount = Args(raw=rest, words=tuple(rest.split())).amount
    if amount and amount > 0:
        result = await ctx.gifts.coins(session, access.user_id, receiver, int(amount))
        await text(
            message,
            ctx,
            f"🎁 {money(result['amount'])} 🪙 → {mention(receiver)}. Balance {money(result['balance'])} 🪙.",
        )
        return
    await text(
        message,
        ctx,
        "I need a character from your collection or a coin amount — <code>/gift @name 42</code> or <code>/gift @name 5000</code>.",
    )


async def _announce(
    message: Message,
    ctx: AppContext,
    result: dict[str, Any],
    *,
    sender: int,
    receiver: int,
    anonymous: bool,
    character: Any,
) -> None:
    rarity = Rarity.from_value(int(character.rarity_id))
    who = (
        "someone anonymously"
        if anonymous
        else mention(sender, (message.from_user.first_name if message.from_user else ""))
    )
    note_text = str(result.get("note") or "").strip()
    builder = (
        RichMessageBuilder()
        .heading(f"🎁 {character.name} changes hands", size=2)
        .photo(
            character.image_url or "", caption=f"{shorten(character.anime, 40)} · {rarity.badge}"
        )
        if (character.image_url and not anonymous)
        else RichMessageBuilder().heading("🎁 a gift arrives", size=2)
    )
    builder.paragraph(
        html=f"{who} → {mention(receiver)}\n<b>{character.name}</b> {rarity.badge}"
        + (f"\n“{shorten(note_text, 120)}”" if note_text else "")
    )
    builder.footer(f"gift #{result.get('id', '?')} recorded in /giftlog")
    await card(message, ctx, builder=builder, html=f"🎁 {character.name} → {mention(receiver)}")
    # A heart on the command message: in a busy group the card scrolls away,
    # the reaction stays with the moment.
    if ctx.caps.allow("reactions"):
        from waifu.tg.interactions import react

        await react(ctx.bot, message, "heart")
    # The owner's log channel and the receiver's private receipt are written by
    # ``GiftService.character`` itself — one record per gift, on every entry
    # point (command, /anon, profile 🎁 button) instead of one per handler.


@router.callback_query(F.data.startswith("gift:to:"))
async def gift_from_profile(
    callback_query: CallbackQuery, ctx: AppContext, session: Any, access: Access
) -> None:
    """The 🎁 button on /profile: pick one of your spares, no typing."""
    receiver = int((callback_query.data or "").split(":")[-1] or 0)
    if not receiver or receiver == access.user_id:
        await note(callback_query, "not yourself 🙂", alert=True)
        return
    sellable = await ctx.collection.sellable(session, access.user_id, limit=6)
    if not sellable:
        await note(
            callback_query, "you have no giftable copy (locked/favourite ones stay)", alert=True
        )
        return
    rows = [
        [
            callback(
                f"{shorten(entry.name, 20)} ×{entry.count}",
                cb("gift", "send", str(receiver), str(entry.character_id)),
            )
        ]
        for entry in sellable
    ]
    rows.append([callback("cancel", cb("gift", "cancel"))])
    builder = (
        RichMessageBuilder()
        .heading(f"🎁 gift to {mention(receiver)}", size=2)
        .paragraph(html="Spares only — your favourite and locked copies cannot be sent.")
    )
    await edit(callback_query, ctx, builder=builder, html="pick a character", buttons=rows)


@router.callback_query(F.data.startswith("gift:send:"))
async def gift_send(
    callback_query: CallbackQuery, ctx: AppContext, session: Any, access: Access
) -> None:
    parts = (callback_query.data or "").split(":")
    receiver, character_id = int(parts[2] or 0), int(parts[3] or 0)
    try:
        result = await ctx.gifts.character(session, access.user_id, receiver, character_id, note="")
    except (NotFound, Locked, WaifuError) as exc:
        await note(callback_query, exc.user_message, alert=True)
        return
    character = await ctx.collection.character(session, character_id)
    await note(callback_query, f"gifted {getattr(character, 'name', character_id)}", alert=True)
    if callback_query.message:
        await _announce(
            callback_query.message,
            ctx,
            result,
            sender=access.user_id,
            receiver=receiver,
            anonymous=False,
            character=character,
        )


@router.callback_query(F.data == "gift:cancel")
async def gift_cancel(callback_query: CallbackQuery) -> None:
    await callback_query.answer("cancelled")


@router.message(Command("giftlog", "gifts"))
async def giftlog(message: Message, ctx: AppContext, session: Any, access: Access) -> None:
    history = await ctx.gifts.history(session, access.user_id)
    sent = history.get("sent") if isinstance(history, dict) else None
    received = history.get("received") if isinstance(history, dict) else None
    builder = RichMessageBuilder().heading("🎁 your gifts", size=2)
    builder.table(
        [["sent", str(len(sent or []))], ["received", str(len(received or []))]],
        compact=True,
        bordered=False,
    )
    for row in (received or [])[:5]:
        builder.line(
            f"⬅️ {money(int(getattr(row, 'coins', 0) or 0))} 🪙 / {shorten(str(getattr(row, 'character_name', '') or ''), 24)}"
        )
    recent = await ctx.gifts.recent(session, access.user_id, limit=6)
    if recent:
        builder.divider().heading("recent in this bot", size=3)
        for row in recent:
            builder.line(
                f"• {shorten(str(getattr(row, 'character_name', '') or getattr(row, 'name', '')), 26)} → {money(int(getattr(row, 'coins', 0) or 0))} 🪙"
            )
    await card(
        message,
        ctx,
        builder=builder,
        html=f"gifts: sent {len(sent or [])}, received {len(received or [])}",
    )


@router.message(Command("anon"))
async def anon(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, access: Access
) -> None:
    """``/anon`` is ``/gift`` with the sender hidden — same code path, one flag."""
    if command and command.args:
        await gift(message, ctx, session, CommandObject(command="/gift", args=command.args), access)
        return
    await text(
        message,
        ctx,
        "Usage: <code>/anon @name 42 a note</code> — sends a spare without your name on it.",
    )
