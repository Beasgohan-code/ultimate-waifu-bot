"""``/chars``, ``/char``, ``/addchar``, ``/delchar``, ``/hidechar``, ``/setchance``, ``/banner``.

The roster is data, not code — the reference bot kept characters in a table but edited
them by hand-running SQL, which is how its catalogue ended up with four rows sharing
one unique ``(name, anime)`` index (and four characters nobody could summon). Everything
here goes through ``characters.create_or_update``, which is the same upsert the seeder
uses, so a duplicated name is a merge instead of a uniqueness violation at 3am.

Admin commands deliberately talk to the repository layer rather than inventing a
``RosterService``: this is thin CRUD with no rules to centralise, and a pass-through
service is how you get the two-places-to-change problem back. The one rule with real
consequences — deleting a character must not orphan a collection — is handled by
``delete_character`` itself.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

from waifu.enums import Rarity
from waifu.errors import NotFound
from waifu.plugins._kit import (
    Args,
    RichMessageBuilder,
    card,
    edit,
    money,
    pager_row,
    refuse,
    shorten,
    staff_of,
    text,
)

if TYPE_CHECKING:  # pragma: no cover
    from waifu.core.access import Access
    from waifu.core.context import AppContext

router = Router(name="characters")
PAGE_SIZE = 12


@router.message(Command("chars", "roster", "characters"))
async def chars(message: Message, ctx: AppContext, session: Any, command: CommandObject) -> None:
    """Browse the roster by tier: ``/chars``, ``/chars 3``, ``/chars gojo``."""
    args = Args.of(command)
    rarity_id: int | None = None
    page = 0
    query = ""
    for word in args.words:
        if word.isdigit() and len(word) <= 2:
            page = max(0, int(word) - 1)
        elif word.isdigit() and len(word) > 2:
            rarity_id = None
            query = word
        else:
            query = " ".join([query, word]).strip()
    if args.first.lower() in {rarity.label.lower() for rarity in Rarity} or args.first.isdigit():
        rarity_id = _rarity_from(args.first)
        if rarity_id is not None:
            query = " ".join(args.words[1:]).strip()
            page = 0
    hits, total = await ctx.collection.search(
        session, query, rarity_id=rarity_id, limit=PAGE_SIZE, page=page
    )
    builder = RichMessageBuilder().heading(f"🎴 roster — {money(total)} characters", size=2)
    if hits:
        rows = [["#", "name", "series", "tier", "price"]]
        for character in hits:
            rarity = Rarity.from_value(int(character.rarity_id))
            rows.append(
                [
                    str(int(character.id)),
                    f"<b>{shorten(character.name, 24)}</b>",
                    shorten(character.anime or "—", 22),
                    f"{rarity.badge}",
                    money(await ctx.collection.price(session, character)),
                ]
            )
        builder.table(rows, compact=True)
    else:
        builder.paragraph(html="<i>nothing matches — try /chars &lt;name&gt;</i>")
    await card(
        message,
        ctx,
        builder=builder,
        html=_plain_chars(hits),
        buttons=[
            pager_row(
                page=page,
                pages=max(1, -(-total // PAGE_SIZE)),
                prefix="chars",
                extra=f"{rarity_id or 0}/{page}",
            )
        ],
    )


def _plain_chars(hits: list[Any]) -> str:
    return "\n".join(f"{character.id}. {character.name} — {character.anime}" for character in hits)


def _rarity_from(token: str) -> int | None:
    token = (token or "").strip().lower()
    if token.isdigit():
        value = int(token)
        return value if 1 <= value <= len(Rarity) else None
    for rarity in Rarity:
        if rarity.label.lower() == token or rarity.name.lower() == token:
            return int(rarity)
    return None


@router.callback_query(F.data.startswith("chars:page:"))
async def chars_page(callback_query: CallbackQuery, ctx: AppContext, session: Any) -> None:
    parts = (callback_query.data or "").split(":")
    direction = parts[2] if len(parts) > 2 else "next"
    rarity_raw, page_raw = ([*parts[3].split("/"), "0", "0"])[:2] if len(parts) > 3 else ("0", "0")
    page = int(page_raw or 0)
    page = (
        max(0, page + 1)
        if direction == "next"
        else max(0, page - 1)
        if direction == "prev"
        else page
    )
    rarity_id = int(rarity_raw) or None
    hits, total = await ctx.collection.search(
        session, "", rarity_id=rarity_id, limit=PAGE_SIZE, page=page
    )
    builder = (
        RichMessageBuilder()
        .heading(f"🎴 roster — page {page + 1}", size=2)
        .table(
            [
                ["#", "name", "tier"],
                *[
                    [
                        str(int(character.id)),
                        shorten(character.name, 28),
                        Rarity.from_value(int(character.rarity_id)).badge,
                    ]
                    for character in hits
                ],
            ],
            compact=True,
        )
    )
    await edit(
        callback_query,
        ctx,
        builder=builder,
        html=_plain_chars(hits),
        buttons=[
            pager_row(
                page=page,
                pages=max(1, -(-total // PAGE_SIZE)),
                prefix="chars",
                extra=f"{rarity_raw}/{page}",
            )
        ],
    )


@router.message(Command("char", "waifu", "info"))
async def char_info(
    message: Message, ctx: AppContext, session: Any, command: CommandObject
) -> None:
    """``/char <name or #id>`` — the character sheet, price, holders and spawn stats."""
    args = Args.of(command)
    if not args.raw:
        await text(message, ctx, "Which one? <code>/char mikasa</code> or <code>/char 42</code>.")
        return
    try:
        character = await ctx.collection.find(session, args.raw)
    except NotFound:
        await text(message, ctx, f"No character matches “{shorten(args.raw, 40)}”.")
        return
    await send_character(message, ctx, session, character)


async def send_character(
    event: Message | CallbackQuery, ctx: AppContext, session: Any, character: Any
) -> None:
    rarity = Rarity.from_value(int(character.rarity_id))
    price = await ctx.collection.price(session, character)
    holders = await ctx.hstats.character_owners(session, character, limit=5)
    spawns: dict[str, int] = {}
    builder = RichMessageBuilder().heading(f"{rarity.badge} {character.name}", size=1)
    if character.image_url or character.photo_file_id:
        card_image = await ctx.cards.image_for(session, character)
        if card_image.ok and card_image.sendable:
            builder.photo(card_image.sendable, caption=shorten(character.anime, 60))
    if character.anime:
        builder.line(f"<i>{character.anime}</i>")
    if character.description:
        builder.quote(shorten(character.description, 300))
    rows = [
        ["tier", f"{rarity.emoji} {rarity.label} (#{int(rarity)})"],
        ["price", f"{money(price)} 🪙"],
        ["power", money(character.stat_power or 0)],
    ]
    if spawns:
        rows.append(
            ["spawned / claimed", f"{spawns.get('spawns', 0)} / {spawns.get('claimed', 0)}"]
        )
    builder.table(rows, compact=True, bordered=False)
    if holders:
        # ``character_owners`` yields player dicts, not ids — the sheet should say who
        # actually has it, which is the first question players ask about a 1M🪙 tier.
        builder.line(
            "held by "
            + ", ".join(
                f"{item.get('name') or item.get('username') or item.get('user_id')} ×{item.get('count', 1)}"
                for item in holders[:5]
            )
        )
    html = f"{rarity.badge} <b>{character.name}</b> · {character.anime or '—'} · {money(price)} 🪙"
    if isinstance(event, CallbackQuery):
        await edit(event, ctx, builder=builder, html=html)
        return
    await card(event, ctx, builder=builder, html=html)


# ------------------------------------------------------------------- roster edits
@router.message(Command("addchar", "newchar", "updatechar"))
async def addchar(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, access: Access
) -> None:
    """``/addchar Name | Series | Rarity | price | tags`` (+ attach a photo or video).

    One message, no FSM: the legacy bot drove this through a seven-step state machine,
    which lost the draft whenever the process restarted and locked two admins out of
    each other's menus in the same chat.
    """
    staff_of(access)
    args = Args.of(command)
    fields = [part.strip() for part in (args.raw or "").split("|")]
    if not fields or not fields[0]:
        await text(
            message,
            ctx,
            "Usage: <code>/addchar Name | Series | Legendary | 150000 | tsundere, sword</code>\nAttach a photo/video to store its file_id too.",
        )
        return
    name = fields[0]
    anime = fields[1] if len(fields) > 1 else ""
    rarity = Rarity.from_label(fields[2]) if len(fields) > 2 and fields[2] else Rarity.COMMON
    try:
        price = (
            int("".join(ch for ch in fields[3] if ch.isdigit()))
            if len(fields) > 3 and fields[3]
            else int(rarity.base_price)
        )
    except ValueError:
        price = int(rarity.base_price)
    tags = fields[4] if len(fields) > 4 else ""
    from waifu.db.repositories import characters as char_repo

    character, created = await char_repo.create_or_update(
        session,
        name=name,
        anime=anime,
        rarity=rarity,
        price=price,
        image_url=_first_url(message),
        photo_file_id=_photo_id(message),
        video_file_id=_video_id(message),
        description=message.caption or "",
        tags=tags,
    )
    await text(
        message,
        ctx,
        f"{'🆕 added' if created else '♻️ updated'} <b>{character.name}</b> ({character.anime or '—'}) {rarity.badge} · {money(int(character.price))} 🪙 · id {character.id}",
    )


def _first_url(message: Message) -> str:
    text_body = message.text or message.caption or ""
    for token in text_body.split():
        if token.startswith(("http://", "https://")) and any(
            token.lower().endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif")
        ):
            return token
    return ""


def _photo_id(message: Message) -> str:
    if message.photo:
        return message.photo[-1].file_id
    return ""


def _video_id(message: Message) -> str:
    return message.video.file_id if message.video else ""


@router.message(Command("delchar", "rmchar", "delete"))
async def delchar(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, access: Access
) -> None:
    staff_of(access)
    args = Args.of(command)
    if not args.raw:
        await text(
            message,
            ctx,
            "Usage: <code>/delchar &lt;#id or name&gt;</code> — say <code>force</code> to skip the confirmation.",
        )
        return
    force = "force" in args.raw.lower()
    query = args.raw.replace("force", "").strip()
    try:
        character = await ctx.collection.find(session, query)
    except NotFound:
        await text(message, ctx, f"Nothing called “{shorten(query, 40)}”.")
        return
    from waifu.db.repositories import characters as char_repo

    holders = await ctx.collection.holders(session, int(character.id), limit=3)
    if not force:
        await text(
            message,
            ctx,
            f"⚠️ <b>{character.name}</b> is held by {money(len(holders))}+ player(s); deleting removes it from every collection.\nConfirm: <code>/delchar {character.id} force</code>",
        )
        return
    removed = await char_repo.delete_character(session, int(character.id))
    await text(message, ctx, f"🗑️ deleted <b>{character.name}</b> (rows affected: {removed}).")


@router.message(Command("hidechar", "showchar"))
async def hidechar(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, access: Access
) -> None:
    """Deactivate instead of delete: keeps history intact, stops future drops."""
    staff_of(access)
    args = Args.of(command)
    name = (message.text or "").split(maxsplit=1)
    enable = len(name) > 0 and name[0].lstrip("/").split("@")[0] == "showchar"
    query = args.raw
    if not query:
        await text(message, ctx, "Usage: <code>/hidechar &lt;name or #id&gt;</code>")
        return
    try:
        character = await ctx.collection.find(session, query)
    except NotFound:
        await text(message, ctx, f"Nothing called “{shorten(query, 40)}”.")
        return
    from waifu.db.repositories import characters as char_repo

    await char_repo.set_active(session, int(character.id), enable)
    await text(message, ctx, f"{'👁️ visible' if enable else '🙈 hidden'}: <b>{character.name}</b>")


@router.message(Command("setchance", "chance", "setrate"))
async def setchance(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, access: Access
) -> None:
    """``/setchance Legendary 6.2`` (and ``--claim`` for the free-claim table)."""
    staff_of(access)
    args = Args.of(command)
    claim = "--claim" in args.raw
    body = args.raw.replace("--claim", "").strip()
    parts = body.split()
    if len(parts) < 2:
        await text(
            message, ctx, "Usage: <code>/setchance &lt;rarity&gt; &lt;percent&gt; [--claim]</code>"
        )
        return
    rarity = Rarity.from_label(" ".join(parts[:-1]))
    try:
        value = float(parts[-1].rstrip("%"))
    except ValueError:
        await refuse(message, "the last argument must be a percentage")
        return
    from waifu.db.repositories import characters as char_repo

    if claim:
        await char_repo.set_claim_chance(session, rarity, value)
    else:
        await char_repo.set_rarity_chance(session, rarity, value)
    await text(
        message,
        ctx,
        f"📊 {rarity.badge} {rarity.label} → {value:.2f}% ({'free claim' if claim else 'summon'} table).",
    )
    await ctx.notify(
        f"⚙️ {rarity.label} odds → {value:.2f}% ({'claim' if claim else 'summon'}) by {message.from_user.id if message.from_user else '?'}",
        silent=True,
    )


@router.message(Command("banner", "feature"))
async def banner(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, access: Access
) -> None:
    """Weight specific characters toward the banner (their bot had no way to promote one)."""
    staff_of(access)
    args = Args.of(command)
    ids = [int(token) for token in args.words if token.isdigit()]
    if not ids:
        await text(
            message,
            ctx,
            "Usage: <code>/banner 12 44 87</code> — ids from /chars. Empty list clears the banner.",
        )
        return
    from waifu.db.repositories import characters as char_repo

    count = await char_repo.set_banner(session, ids)
    await text(message, ctx, f"🎯 banner weight set on {count} character(s).")


@router.message(Command("media", "upload", "attach"))
async def media(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, access: Access
) -> None:
    """``/media <id>`` with an attached photo/video — store the bot's own file_id.

    File ids are per-bot, which is exactly why the legacy ``msg_id`` columns could not
    be reused after a token change; storing them here is the fix, not the bug.
    """
    staff_of(access)
    args = Args.of(command)
    character_id = args.integer
    if not character_id:
        await text(message, ctx, "Usage: <code>/media 42</code> with a photo or video attached.")
        return
    from waifu.db.repositories import characters as char_repo

    await char_repo.attach_media(
        session,
        character_id,
        photo_file_id=_photo_id(message),
        video_file_id=_video_id(message),
        live_photo_file_id=message.video_note.file_id if message.video_note else "",
        image_url=_first_url(message),
    )
    await text(message, ctx, f"🖼️ media stored for #{character_id}.")


@router.message(Command("reseed", "seedcheck"))
async def reseed(message: Message, ctx: AppContext, access: Access) -> None:
    """Re-run the catalogue seed (idempotent) — for a DB that predates the shipped roster."""
    staff_of(access)
    from waifu.db.seed import seed_all

    # The seeder owns its own transaction (it is the same code the migration calls), so
    # this cannot half-write inside the middleware's.
    await seed_all(ctx.db.engine)
    await text(
        message,
        ctx,
        "🌱 catalogue re-seeded (existing rows are kept; see scripts/build_catalogue.py for the source of truth).",
    )


@router.message(Command("chancelist", "clist"))
async def chancelist(message: Message, ctx: AppContext, session: Any) -> None:
    """Both ladders at once: what the gacha pays and what a free claim pays.

    The old bot kept these in two commands (``/chance`` and ``/setclaim``'s table) and
    admins routinely adjusted the wrong one. One table, two columns.
    """
    from waifu.db.repositories import characters as char_repo

    drops = dict(await char_repo.rarity_chances(session, enabled_only=False) or [])
    claims = dict(await char_repo.claim_chances(session, enabled_only=False) or [])
    rows = [["tier", "drop %", "claim %", "base price"]]
    for rarity in Rarity:
        rows.append(
            [
                f"{rarity.emoji} {rarity.label}",
                f"{float(drops.get(rarity, 0.0)):.2f}",
                f"{float(claims.get(rarity, 0.0)):.2f}",
                money(rarity.base_price),
            ]
        )
    builder = RichMessageBuilder().heading("⚖️ live odds", size=2).table(rows, compact=True)
    builder.footer("/setchance <tier> <percent> edits the gacha · /setclaim edits claims")
    await card(
        message,
        ctx,
        builder=builder,
        html="\n".join(" ".join(str(col) for col in row) for row in rows[1:]),
    )


@router.message(Command("setclaim", "claimchance"))
async def setclaim(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, access: Access
) -> None:
    """/setclaim <tier> <percent> — the claim ladder only (gacha rates: /setchance)."""
    staff_of(access)
    from waifu.db.repositories import characters as char_repo

    args = Args.of(command)
    wanted = {rarity.label.lower(): rarity for rarity in Rarity}
    if (
        len(args.words) < 2
        or args.words[0].lower() not in wanted
        or not args.words[1].replace(".", "", 1).isdigit()
    ):
        await text(
            message,
            ctx,
            "Usage: <code>/setclaim limited 2.5</code> — percent of the free-claim pool",
        )
        return
    rarity = wanted[args.words[0].lower()]
    percent = max(0.0, min(100.0, float(args.words[1])))
    await char_repo.set_claim_chance(session, rarity, percent)
    await text(
        message,
        ctx,
        f"🎯 {rarity.badge} {rarity.label} claim weight = {percent:.2f}% — /clist shows the whole ladder",
    )
