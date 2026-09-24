"""Character ingestion — ``/upload``: the roster is built by hand, not shipped.

A fresh install of this bot has an **empty** ``characters`` table. That is not a gap in the
seed, it is the design of the deployment this bot replaces: Summon-bot's database held
whatever its admins uploaded, one message at a time, and its source tree contained no
roster at all. So the pipeline here is the pipeline they ran, ported to aiogram — with the
quirks kept, because they turned out to be the right ones:

* **Reply to the media, command in the reply.** ``/upload Name Series 3`` sent as a reply to
  a photo/video/GIF. The art arrives first, so adding a character never leaves Telegram, and
  no file touches a third-party host unless the admin asks for it.
* **``file_id`` first, web host second.** The bot stores the ``file_id`` of the media it was
  given (permanent, free to re-send, immune to hotlink rot) and *offers* Catbox/ImgBB as
  buttons for a public URL on top. The reference schema held exactly those three facts in
  ``msg_id`` (``"<type>_<file_id>"``), ``img_url`` and ``img_url2`` — here they sit on
  columns that say what they are.
* **Nothing is invented.** Names, series and tiers are only ever what an admin wrote; the
  tier decides price and power, so a two-word caption is a complete character.

``/upload`` is a port, not a redesign: the guard order, the three-word grammar, the photo →
video → GIF media pick, the zero-padded gap-filled id, the exact reply strings and the
``up_*`` callback data are the reference's, on purpose ("same, no change"). Where the new
API or a safer default genuinely adds something, it is reachable through a *separate* door
(``/autoadd``, ``/addchar``) so that the ported flow stays the flow its admins memorised:

* a **Live Photo** (a photo carrying a paired video, API 9.1) is stored whole on
  ``live_photo_file_id`` by ``/autoadd`` and ``/addchar`` — ``/upload`` flattens it to its
  still, exactly as the reference does;
* **``/autoadd``** turns a group or channel into an ingest feed: media an admin posts with a
  caption becomes a character with no command at all, acknowledged by an **ephemeral**
  receipt (``EphemeralMessageParameters``) so the group sees the result and nobody else's
  chat becomes an admin console;
* ``setMessageReaction`` ticks the ingested message ✅, and the host buttons carry
  ``style`` — set through a helper that drops fields an older server rejects.

State: a pending upload lives in :data:`PENDING_UPLOADS` (process-local, keyed ``u<n>``)
until the admin answers or the table passes :data:`PENDING_MAX`. Nothing ages an in-flight
upload out — the reference bot's entries never expired, and a receipt whose buttons died
while it was still on screen is worse than an entry that waits. ``/uploads`` prunes by age.
The buttons are an *offer*, not a step — the character row is already complete without them,
so losing the state on a restart costs a URL, never a character. Handlers write no SQL:
every column change goes through the ``characters`` repository.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
from aiogram import F, Router
from aiogram.filters import BaseFilter, Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message

from waifu.db.repo import characters as char_repo
from waifu.enums import Rarity
from waifu.errors import RosterEmpty, WaifuError
from waifu.logging import get_logger
from waifu.plugins._kit import note, refuse, staff_of, text
from waifu.tg.media import fingerprint, upload_to_catbox, upload_to_imgbb
from waifu.tg.messages import style_button
from waifu.utils.text import esc, strip_md
from waifu.utils.time import now_utc

if TYPE_CHECKING:  # pragma: no cover
    from waifu.core.access import Access
    from waifu.core.context import AppContext

log = get_logger("plugins.uploads")
router = Router(name="uploads")
#: The watcher that ingests uncaptioned-by-command media in an ``/autoadd`` chat. It is an
#: *aux* router on purpose: aux routers are attached after every plugin, so a filter this
#: broad can never steal a photo from ``/gift``, ``/media`` or the AI chat handler.
autoadd_router = Router(name="autoadd")

#: The reference bot's ``PENDING_UPLOADS`` + ``COUNTER`` pair. Entries expire (see
#: :func:`prune_pending`) because the original grew without bound and only leaked memory
#: until someone rewrote that file.
PENDING_UPLOADS: dict[str, dict[str, Any]] = {}
COUNTER = {"n": 0}
PENDING_TTL = 60 * 60  # only used by /uploads, never to expire a live receipt
PENDING_MAX = 64

#: The media kinds ``/upload`` accepts — the reference's list, in the reference's order.
#: Anything else (document, audio, live photo) is refused with the same line it used.
UPLOAD_KINDS = ("photo", "video", "animation")
#: The reference bot's callback namespace: ``up_cb|u1``, ``up_ib|u1``, ``up_skip|u1``, and a
#: handler whose only filter is "starts with ``up_``". Same strings, so an admin's muscle
#: memory, a forwarded screenshot and any external button builder all still work.
PREFIX = "up_"

USAGE = (
    "❌ <b>Error: Please reply to an image, video, or GIF!</b>\n\n"
    "💡 <b>Usage:</b> Reply to media with:\n"
    "<code>/upload [Char-Name] [Anime-Name] [Rarity_Number]</code>\n"
    "Example: <code>/upload Son-gohan dragon-ball 3</code>"
)
BAD_FORMAT = (
    "❌ <b>Invalid Format!</b>\n\n"
    "💡 <b>Format:</b> <code>/upload [Char-Name] [Anime-Name] [Rarity_Number]</code>\n"
    "Example: <code>/upload Son-gohan dragon-ball 3</code>"
)
BAD_MEDIA = "❌ Unsupported media type! Use Photo, Video, GIF, Live Photo, or a document image."
#: What ``/upload`` answers when the media it was given is not one of the three kinds.
BAD_UPLOAD_MEDIA = "❌ Unsupported media type! Use Photo, Video, or GIF."
#: The reference's refusal, one line, unchanged.
DENIED = "❌ Not allowed"
BAD_RARITY = f"❌ Invalid Rarity ID! Use 1-{len(list(Rarity))}."
EXPIRED = "❌ Session expired. Re-upload with /upload."
#: Suffix per media kind, so the temp file a web host receives keeps a real extension
#: (catbox serves whatever name it is given, and ``.mp4`` is the difference between a
#: playable link and a download prompt).
SUFFIX = {"photo": ".jpg", "live": ".jpg", "video": ".mp4", "animation": ".mp4", "document": ".bin"}


def new_upload_id() -> str:
    """``u1``, ``u2``, … — the reference bot's ``new_upload_id``, counter included."""
    COUNTER["n"] += 1
    return f"u{COUNTER['n']}"


def up_data(action: str, upload_id: str) -> str:
    """``up_cb|u1`` — the reference's callback payload, verbatim in shape."""
    return f"{PREFIX}{action}|{upload_id}"


def parse_up_data(data: str | None) -> tuple[str, str]:
    """Inverse of :func:`up_data`; ``("", "")`` for anything that is not one of ours."""
    raw = data or ""
    if not raw.startswith(PREFIX):
        return "", ""
    head, _, upload_id = raw[len(PREFIX) :].partition("|")
    return head, upload_id


def cap_pending() -> int:
    """Keep :data:`PENDING_UPLOADS` bounded, oldest first — no expiry of live receipts."""
    if len(PENDING_UPLOADS) <= PENDING_MAX:
        return 0
    ordered = sorted(PENDING_UPLOADS, key=lambda key: PENDING_UPLOADS[key].get("at", 0))
    dropped = ordered[: len(PENDING_UPLOADS) - PENDING_MAX]
    for key in dropped:
        entry = PENDING_UPLOADS.pop(key, None) or {}
        path = str(entry.get("local_path") or "")
        if path:
            Path(path).unlink(missing_ok=True)
    return len(dropped)


def prune_pending() -> int:
    """Drop uploads whose buttons no longer lead anywhere; returns how many went away."""
    if len(PENDING_UPLOADS) <= PENDING_MAX:
        cutoff = time.time() - PENDING_TTL
        stale = [key for key, value in PENDING_UPLOADS.items() if value.get("at", 0) < cutoff]
    else:
        ordered = sorted(PENDING_UPLOADS, key=lambda key: PENDING_UPLOADS[key].get("at", 0))
        stale = ordered[: len(PENDING_UPLOADS) - PENDING_MAX]
    for key in stale:
        entry = PENDING_UPLOADS.pop(key, None) or {}
        path = str(entry.get("local_path") or "")
        if path:
            Path(path).unlink(missing_ok=True)
    return len(stale)


# ---------------------------------------------------------------------- parsing
def escape(value: str) -> str:
    """Admin input is interpolated into HTML receipts — so it has to be escaped first.

    The reference bot pasted names in raw, which meant a ``<`` in a character name broke
    every card that showed it (Telegram rejects the message, the admin sees an error and
    the roster entry silently never appears).
    """
    return esc(strip_md(value or ""))


def media_of(message: Message, allow: tuple[str, ...] | None = None) -> tuple[str, str]:
    """``(file_id, kind)`` for the best media on ``message``, in the reference's priority.

    ``allow`` narrows the list: ``/upload`` passes :data:`UPLOAD_KINDS` so that a document
    or a Live Photo is *not* quietly accepted (the reference picks the still of a Live
    Photo because it checks ``photo`` first — the same happens here). ``/autoadd`` passes
    nothing and additionally recognises ``live`` (a photo paired with a video, API 9.1) and
    ``document``, because in a feed chat an admin posts whatever the artist sent.
    """
    photo = message.photo[-1].file_id if message.photo else ""
    video = message.video.file_id if message.video else ""
    pairs: list[tuple[str, str]] = []
    if photo and video:
        pairs.append((video, "live"))
    if photo:
        pairs.append((photo, "photo"))
    if video:
        pairs.append((video, "video"))
    if message.animation:
        pairs.append((message.animation.file_id, "animation"))
    if message.document:
        pairs.append((message.document.file_id, "document"))
    for file_id, kind in pairs:
        if allow is None or kind in allow:
            return file_id, kind
    return "", ""


def columns_for(message: Message, allow: tuple[str, ...] | None = None) -> dict[str, str]:
    """Which ``characters`` columns the media of ``message`` fills."""
    file_id, kind = media_of(message, allow=allow)
    if not file_id:
        return {}
    if kind == "photo":
        return {"photo_file_id": file_id}
    if kind in ("video", "animation"):
        return {"video_file_id": file_id}
    if kind == "live":
        return {
            "photo_file_id": message.photo[-1].file_id if message.photo else "",
            "video_file_id": message.video.file_id if message.video else "",
            "live_photo_file_id": message.video.file_id if message.video else "",
        }
    # A document is stored as the photo: admins upload .webp/.tga art this way, and an
    # ignored file is worse than a slightly mislabelled one.
    return {"photo_file_id": file_id}


def fields_of(raw: str | None) -> list[str]:
    """Split an admin's argument string into fields.

    ``|`` separates fields (so a series may contain spaces: ``Gojo | Jujutsu Kaisen | 4``);
    without any pipe the reference bot's whitespace grammar is kept, where each field is one
    dash-joined word (``Son-gohan dragon-ball 3``).
    """
    text_ = (raw or "").strip()
    if not text_:
        return []
    if "|" in text_:
        return [part.strip() for part in text_.split("|") if part.strip()]
    return text_.split()


def split_entry(fields: list[str]) -> tuple[str, str, str]:
    """``[name, series, tier-token]`` — the reference bot's three fields, verbatim order."""
    name = fields[0].replace("-", " ").title()
    series = fields[1].replace("-", " ").title() if len(fields) > 1 else ""
    tier = " ".join(fields[2:]) if len(fields) > 2 else ""
    return name, series, tier


def parse_tier(raw: str) -> Rarity | None:
    """A tier, strictly: the number 1-18, or a word/emoji that names exactly one tier.

    Summon-bot only took ``args[2] = int``; admins wrote ``Legendary`` and got Common.
    Naming a tier must therefore resolve *or* fail loudly — ``from_label``'s forgiving
    fallback to Common is for reading legacy rows, not for writing new ones.
    """
    text_ = (raw or "").strip()
    if not text_:
        return None
    if text_.isdigit():
        value = int(text_)
        return Rarity(value) if 1 <= value <= len(list(Rarity)) else None
    lowered = text_.lower().strip()
    for member in Rarity:
        if lowered in {member.name.lower(), member.label.lower(), member.emoji.strip()}:
            return member
        bare = member.label.lower().removesuffix(" edition").strip()
        if lowered in {bare, f"{member.emoji} {bare}", f"{member.emoji} {member.label.lower()}"}:
            return member
    return None


def name_from_media(message: Message) -> str:
    """Fallback name for an ``/autoadd`` post: the file name, cleaned (never a guess at
    anything else — an admin can fix a name with a second upload, not a bot's invention)."""
    for attr in ("document", "video", "animation", "audio"):
        row = getattr(message, attr, None)
        raw = getattr(row, "file_name", "") if row is not None else ""
        if raw:
            stem = Path(raw).stem.replace("_", " ").replace("-", " ").strip()
            if stem:
                return stem.title()[:96]
    return ""


def autoadd_fields(message: Message) -> tuple[str, str, Rarity]:
    """Name / series / tier for a feed post, where nobody typed a command.

    Three fields → the ``/upload`` grammar. One or two → name and series, tier Common
    (the tier is what a caption rarely carries and what the ladder defaults cleanly).
    None → the file name, which is the one thing a forwarded photo does carry.
    """
    fields = fields_of(message.caption)
    if len(fields) >= 3:
        name, anime, tier = split_entry(fields)
        return name, anime, parse_tier(tier) or Rarity.COMMON
    if len(fields) == 2:
        return (
            fields[0].replace("-", " ").title(),
            fields[1].replace("-", " ").title(),
            Rarity.COMMON,
        )
    if len(fields) == 1:
        return fields[0].replace("-", " ").title(), "", Rarity.COMMON
    return name_from_media(message) or f"Untitled {message.message_id}", "", Rarity.COMMON


async def is_autoadd_enabled(session: Any, chat_id: int) -> bool:
    from waifu.db.repo import spawns as spawn_repo

    return await spawn_repo.group_switch(session, chat_id, "autoadd")


class AutoAddFeed(BaseFilter):
    """Pass only for media in a chat that switched autoadd on — decided *before* the
    handler is chosen, so every other message in the chat stays available to the plugins
    that were here first."""

    async def __call__(self, message: Message, ctx: AppContext, session: Any = None) -> bool:
        if session is None or message.chat.type not in {"group", "supergroup", "channel"}:
            return False
        if not media_of(message)[0]:
            return False
        # A command's media (``/upload`` with a photo attached) belongs to the command.
        if (message.caption or "").lstrip().startswith("/"):
            return False
        from waifu.db.repo import spawns as spawn_repo

        if not await spawn_repo.group_switch(session, message.chat.id, "autoadd"):
            return False
        return bool(ctx.bot is not None)


# ------------------------------------------------------------------- the ingest
async def ingest(
    ctx: AppContext,
    session: Any,
    *,
    event: Message,
    source: Message,
    name: str,
    anime: str,
    rarity: Rarity,
    auto: bool = False,
    allow: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """File one character from ``source``'s media and return its pending-upload entry.

    Order is the reference bot's and it is not arbitrary:

    1. the row is written first — media in, character exists, even if everything after
       this line fails;
    2. the file is copied to ``upload_dir`` only so a web host can be offered later;
    3. the log channel gets the same post the admin sees, because in the original
       deployment that channel was the archive a database was rebuilt from;
    4. only then the buttons.
    """
    columns = columns_for(source, allow=allow)
    if not columns:
        raise WaifuError(BAD_MEDIA)
    char, created = await char_repo.create_or_update(
        session,
        name=name,
        anime=anime,
        rarity=rarity,
        # The reference bot numbers its roster itself (lowest free id, two digits) and
        # admins quote those numbers everywhere, so a sequence may not pick them.
        assign_id=await char_repo.next_free_id(session),
        photo_file_id=columns.get("photo_file_id", ""),
        video_file_id=columns.get("video_file_id", ""),
        live_photo_file_id=columns.get("live_photo_file_id", ""),
    )
    ref = f"{int(char.id):02d}"
    file_id, kind = media_of(source, allow=allow)
    local_path = await download_media(ctx, source, ref=ref, kind=kind, allow=allow)
    upload_id = new_upload_id()
    entry: dict[str, Any] = {
        "file_id": file_id,
        "file_type": kind,
        "char_id": ref,
        "character_id": int(char.id),
        "char_name": char.name,
        "anime": char.anime,
        "rarity": char.rarity,
        "local_path": str(local_path) if local_path else "",
        "at": time.time(),
        "upload_id": upload_id,
        "created": created,
    }
    PENDING_UPLOADS[upload_id] = entry
    cap_pending()

    # Fingerprint the bytes while they are in hand: "the same character twice under two
    # spellings" is the most common roster disease in a bot run by a committee, and a hash
    # of the media catches it even when the names do not match.
    meta: dict[str, Any] = {
        "by": event.from_user.id if event.from_user else 0,
        "kind": kind,
        "auto": auto,
        "at": now_utc().isoformat(timespec="seconds"),
    }
    if local_path is not None:
        try:
            meta["fingerprint"] = fingerprint(await anyio.Path(local_path).read_bytes())
        except OSError:  # pragma: no cover - disk trouble, not user error
            meta["fingerprint"] = ""
    char.meta = {**(char.meta or {}), "upload": meta}
    await session.flush()
    # ``/chars`` and the pull pools read a 30-second character cache with no invalidation
    # hook on insert, so a roster that was empty when it was first viewed would stay empty
    # for half a minute — long enough for "I added it and it's not there" ticket.
    await ctx.cache.invalidate("catalogue")  # the namespace the reads use

    await post_to_log_channel(ctx, source, char=char, ref=ref, event=event)
    return entry


async def download_media(
    ctx: AppContext,
    message: Message,
    *,
    ref: str,
    kind: str,
    allow: tuple[str, ...] | None = None,
) -> Path | None:
    """Copy the media into ``upload_dir`` so a web host can be offered for it.

    The ``file_id`` is already the durable copy, so a failed download is reported as "no
    URL available" and never as a lost character.
    """
    file_id, _ = media_of(message, allow=allow)
    if not file_id or ctx.bot is None:
        return None
    directory = anyio.Path(ctx.settings.upload_dir)
    await directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"upload_{ref}_{kind}{SUFFIX.get(kind, '.bin')}"
    if await target.exists():  # a stale file would be uploaded under the new character
        await target.unlink(missing_ok=True)
    try:
        handle = await ctx.bot.get_file(file_id)
        await ctx.bot.download(handle, destination=str(target))
    except Exception as exc:  # the URL is optional; the row is not
        log.warning("upload download failed for %s: %s", ref, exc)
        return None
    return Path(str(target))


async def post_to_log_channel(
    ctx: AppContext, source: Message, *, char: Any, ref: str, event: Message
) -> bool:
    """Mirror the new character into ``LOG_CHANNEL_ID`` — the reference bot's habit.

    Failure is logged, not raised: the admin's receipt already has the buttons, and a
    channel the bot was removed from must not undo a valid upload.
    """
    chat_id = int(ctx.settings.log_channel_id or 0)
    if not chat_id or not ctx.settings.upload_to_log_channel or ctx.bot is None:
        return False
    file_id, kind = media_of(source)
    who = event.from_user
    mention = f"<a href='tg://user?id={who.id}'>{escape(who.first_name or '')}</a>" if who else "—"
    caption = (
        "📝 <b>New Character Added!</b>\n\n"
        f"🆔 <b>ID:</b> <code>{ref}</code>\n"
        "<blockquote>"
        f"🎌 <b>Anime:</b> {escape(char.anime) or '—'}\n"
        f"👤 <b>Name:</b> {escape(char.name)}\n"
        f"✨ <b>Rarity:</b> {escape(char.rarity)}\n"
        f"📂 <b>Type:</b> {kind.upper()}"
        "</blockquote>\n"
        f"👑 <b>Uploaded By:</b> {mention}"
    )
    try:
        if kind in ("photo", "live", "document"):
            await ctx.bot.send_photo(chat_id, photo=file_id, caption=caption)
        elif kind == "video":
            await ctx.bot.send_video(chat_id, video=file_id, caption=caption)
        else:
            await ctx.bot.send_animation(chat_id, animation=file_id, caption=caption)
    except Exception as exc:  # audit trail, never a blocker
        log.warning("log channel post failed for %s: %s", ref, exc)
        return False
    return True


def host_buttons(upload_id: str) -> list[list[InlineKeyboardButton]]:
    """The three choices the reference bot offered — same payload, styled (Bot API 10.x)."""
    return [
        [
            style_button("📤 Catbox", callback_data=up_data("cb", upload_id), style="success"),
            style_button("📤 ImgBB", callback_data=up_data("ib", upload_id)),
        ],
        [
            style_button(
                "⏭ Skip (file_id only)",
                callback_data=up_data("skip", upload_id),
                style="danger",
            )
        ],
    ]


def receipt(entry: dict[str, Any]) -> str:
    """The "saved, want a URL too?" line: the reference wording, plus what it could not say."""
    kind = str(entry.get("file_type") or "")
    body = (
        "📝 <b>Character Saved (file_id backup)!</b>\n\n"
        f"🆔 <b>ID:</b> <code>{entry.get('char_id')}</code>\n"
        f"👤 <b>Name:</b> {escape(str(entry.get('char_name') or ''))}\n"
        f"📺 <b>Anime:</b> {escape(str(entry.get('anime') or '')) or '—'}\n"
        f"✨ <b>Rarity:</b> {escape(str(entry.get('rarity') or ''))}\n"
        f"📂 <b>Type:</b> {kind.upper()}\n"
        "📁 <b>Backup:</b> file_id ✅\n\n"
        "<b>Upload to a web host for in-app display?</b>"
    )
    if kind == "live":
        body += "\n\n<i>Live photo stored — the still shows in cards, the video plays on tap.</i>"
    if not entry.get("created", True):
        return f"♻️ <b>That name already existed — updated it.</b>\n\n{body}"
    return body


# ---------------------------------------------------------------------- commands
@router.message(Command("upload", "uploadchar"))
async def upload(message: Message, ctx: AppContext, session: Any, access: Access) -> None:
    """``/upload <name> <series> <1-18>`` as a reply to media — creates the character.

    Every line an admin can see here is the reference bot's, including the unhelpful ones:
    a wrong number of words gets ``❌ Invalid Format!``, a document gets ``❌ Unsupported
    media type! Use Photo, Video, or GIF.``, and an outsider gets ``❌ Not allowed``.

    Gated on ``edit_roster`` (owner, or a sudo admin the owner granted): the roster *is*
    the product, so it is not writable by whoever happens to run a group.
    """
    try:
        access.require("edit_roster")
    except WaifuError:
        # The reference replies with a bare "❌ Not allowed" — one line, no lecture, and no
        # hint about which id *would* be allowed. The reason goes to the log, never the chat.
        log.info("upload denied for %s (no edit_roster)", access.user_id)
        await refuse(message, DENIED)
        return
    # Three words, in this order, and no other spelling of them: the reference bot's
    # grammar, kept exactly (``/autoadd`` and ``/addchar`` are where the tolerance lives).
    args = (message.text or "").split()[1:]
    if len(args) != 3:
        await text(message, ctx, BAD_FORMAT)
        return
    name, anime, tier = split_entry(args)
    # The reference wrote ``int(args[2])``, so ``/upload Yor Anime gold`` raised ValueError
    # inside the handler and the update died half-applied. Refusing costs nothing.
    if not tier.isdigit() or not 1 <= int(tier) <= len(list(Rarity)):
        await refuse(message, BAD_RARITY)
        return
    rarity = Rarity(int(tier))
    source = message.reply_to_message
    if source is None or not media_of(source)[0]:
        await text(message, ctx, USAGE)
        return
    if not media_of(source, allow=UPLOAD_KINDS)[0]:
        await refuse(message, BAD_UPLOAD_MEDIA)
        return
    try:
        entry = await ingest(
            ctx,
            session,
            event=message,
            source=source,
            name=name,
            anime=anime,
            rarity=rarity,
            allow=UPLOAD_KINDS,
        )
    except WaifuError as exc:
        await refuse(message, exc.user_message)
        return
    await ctx.react(message.chat.id, source.message_id, "✅")
    await text(
        message,
        ctx,
        receipt(entry),
        buttons=host_buttons(str(entry["upload_id"])),
    )


@router.message(Command("autoadd"))
async def autoadd(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, access: Access
) -> None:
    """``/autoadd on|off`` — this chat's media feed in, without a command per character."""
    try:
        staff_of(access)
    except WaifuError as exc:
        await refuse(message, exc.user_message)
        return
    if message.chat.type not in {"group", "supergroup", "channel"}:
        await refuse(message, "autoadd is a group or channel switch — it watches a feed.")
        return
    wanted = (command.args or "").strip().lower()
    if wanted not in {"", "on", "off", "status"}:
        await refuse(message, "Usage: <code>/autoadd on</code> · <code>/autoadd off</code>")
        return
    if wanted in {"", "status"}:
        current = await is_autoadd_enabled(session, message.chat.id)
        await text(
            message,
            ctx,
            f"🤖 autoadd here is <b>{'on' if current else 'off'}</b>.\n"
            "On: media an admin posts with a caption — "
            "<code>Name | Series | 3</code>, or just a file name — becomes a character.",
        )
        return
    from waifu.db.repo import spawns as spawn_repo

    await spawn_repo.set_group_switch(session, message.chat.id, "autoadd", value=wanted == "on")
    await ctx.cache.invalidate("groups")
    await text(
        message,
        ctx,
        (
            "🤖 <b>autoadd on.</b> Post the art with a caption and it enters the roster; "
            "reply to my receipt if you also want a web-host URL."
            if wanted == "on"
            else "🤖 <b>autoadd off.</b> Group media is mine to ignore again."
        ),
    )


@router.message(Command("uploads", "pending"))
async def uploads(message: Message, ctx: AppContext, access: Access) -> None:
    """``/uploads`` — which characters are still waiting for a host choice."""
    try:
        staff_of(access)
    except WaifuError as exc:
        await refuse(message, exc.user_message)
        return
    await pending_list(message, ctx)


async def pending_list(message: Message, ctx: AppContext) -> None:
    prune_pending()
    if not PENDING_UPLOADS:
        await text(
            message,
            ctx,
            "📭 Nothing is waiting for a web host. Add something with "
            "<code>/upload</code> (reply to a photo).",
        )
        return
    keys = list(PENDING_UPLOADS)
    lines = [
        f"<code>{key}</code> · <b>{escape(str(value.get('char_name')))}</b> · "
        f"{str(value.get('file_type') or '').upper()} · id {value.get('char_id')}"
        for key, value in ((key, PENDING_UPLOADS[key]) for key in keys[:20])
    ]
    rows = [
        [
            style_button("📤 Catbox", callback_data=up_data("cb", key), style="success"),
            style_button("⏭ Skip", callback_data=up_data("skip", key)),
        ]
        for key in keys[:6]
    ]
    await text(
        message,
        ctx,
        "⏳ <b>Waiting for a host choice</b> (file_id backup is already saved):\n"
        + "\n".join(lines),
        buttons=rows,
    )


@router.message(Command("rosterstats", "ingest"))
async def roster_stats(message: Message, ctx: AppContext, session: Any, access: Access) -> None:
    """``/rosterstats`` — per-tier counts plus what ingestion still has open.

    ``/chars`` is the browser; this is the operator's view (series total, active count,
    uploads awaiting a host) because the roster is only ever as good as the last upload.
    """
    try:
        staff_of(access)
    except WaifuError as exc:
        await refuse(message, exc.user_message)
        return
    totals = await char_repo.totals(session)
    if not totals.get("characters"):
        await text(message, ctx, RosterEmpty().user_message)
        return
    counts = await char_repo.rarity_distribution(session)
    ladder = "\n".join(
        f"{Rarity(tier).display}: {count}" for tier, count in sorted(counts.items()) if count
    )
    await text(
        message,
        ctx,
        f"🗂 <b>Roster</b> — {totals['characters']} characters, {totals.get('series', 0)} series, "
        f"{totals.get('active', 0)} active, {len(PENDING_UPLOADS)} awaiting a host\n{ladder}",
    )


@router.message(Command("archiveart"))
async def archiveart(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, access: Access
) -> None:
    """``/archiveart <id|missing>`` — re-host art into a permanent ``file_id``.

    The unfinished half of ``/upload``: rows whose art is still a hotlinked URL (imported
    from an old deployment, or added before the archive chat existed) 404 the moment the
    host forgets them. This is the reference bot's ``migrate_character_media_to_urls.py``
    run in the opposite, correct direction — the asset ends up owned by the bot.
    """
    try:
        staff_of(access)
    except WaifuError as exc:
        await refuse(message, exc.user_message)
        return
    if ctx.bot is None:  # pragma: no cover - only in unit tests
        await refuse(message, "the bot is not attached, so nothing can be re-hosted")
        return
    if not ctx.settings.media_archive_chat_id:
        await refuse(
            message,
            "set <code>MEDIA_ARCHIVE_CHAT_ID</code> to a private chat the bot can post in "
            "first — that chat is where the permanent file_id is minted",
        )
        return
    wanted = (command.args or "").strip().lower()
    done: list[dict[str, Any]] = []
    if wanted in {"missing", "all", ""}:
        done = await ctx.cards.archive_missing(session, limit=10)
    elif wanted.isdigit():
        done = [await ctx.cards.archive(session, int(wanted))]
    else:
        await refuse(
            message, "Usage: <code>/archiveart 42</code> or <code>/archiveart missing</code>"
        )
        return
    ok = sum(1 for item in done if item.get("ok"))
    if not done:
        await text(message, ctx, "🗃 nothing to archive — every character already has a file_id.")
        return
    problems = [
        f"#{index}: {item.get('error') or 'failed'}"
        for index, item in enumerate(done, start=1)
        if not item.get("ok")
    ][:5]
    body = f"🗃 archived {ok}/{len(done)} character(s) into permanent file_ids."
    if problems:
        body += "\n" + "\n".join(problems)
    if len(done) == 10 and ok:
        body += "\n\n<i>Run it again for the next ten.</i>"
    await text(message, ctx, body)


# ------------------------------------------------------------------ autoadd feed
@autoadd_router.message(AutoAddFeed())
async def autoadd_ingest(message: Message, ctx: AppContext, session: Any) -> None:
    """File one character per media message in an autoadd chat, then say so quietly.

    The caption grammar is the admin's own (``Name | Series | 3``); without one, the file
    name is the character name and the tier is Common — both correctable by posting the
    same file again with a caption, which is what makes a guess cheap here.
    """
    name, anime, rarity = autoadd_fields(message)
    try:
        entry = await ingest(
            ctx,
            session,
            event=message,
            source=message,
            name=name,
            anime=anime,
            rarity=rarity,
            auto=True,
        )
    except WaifuError as exc:
        # Silent by design: in a feed chat a reply on every unsuitable photo is spam, and
        # the admin can read the reason in the log. The ✅ only arrives when it worked.
        log.info("autoadd skipped in %s: %s", message.chat.id, exc)
        return
    await ctx.react(message.chat.id, message.message_id, "✅")
    summary = (
        f"📥 <b>{escape(name)}</b> added"
        + (f" · {escape(anime)}" if anime else "")
        + f" · {rarity.display} · id <code>{entry['char_id']}</code>"
        + ("" if entry["created"] else " (updated)")
    )
    if ctx.caps.allow("ephemeral") and ctx.bot is not None and message.from_user is not None:
        from waifu.tg.ephemeral import send_ephemeral

        sent = await send_ephemeral(
            ctx.bot,
            message.chat.id,
            receiver_user_id=message.from_user.id,
            text=summary + "\n\n" + receipt(entry),
        )
        if sent is not None:
            return
    await note(message, "📥 added to the roster")


# ---------------------------------------------------------------------- callbacks
@router.callback_query(F.data.startswith(PREFIX))
async def upload_callback(callback_query: CallbackQuery, ctx: AppContext, session: Any) -> None:
    """The host choice on a finished upload: Catbox, ImgBB, or keep the file_id only.

    Every branch edits the same receipt — the reference bot's flow, and the right one,
    since the admin is standing at one message rather than scrolling a log — and none of
    them raise: a host that is down costs a URL, so the failure text says in those words
    that the character is saved.
    """
    await callback_query.answer()
    action, upload_id = parse_up_data(callback_query.data)
    entry = PENDING_UPLOADS.get(upload_id)
    message = callback_query.message
    if entry is None or message is None:
        await _edit_plain(message, ctx, EXPIRED)
        return
    if action == "skip":
        await _finish(callback_query, ctx, session, entry, url="", upload_id=upload_id)
        return
    host = "Catbox" if action == "cb" else "ImgBB"
    await _edit_plain(message, ctx, f"⏳ Uploading to <b>{host}</b>...")
    local_path = anyio.Path(str(entry.get("local_path") or ""))
    url = ""
    error = ""
    try:
        if not await local_path.is_file():
            raise WaifuError("the temp file is gone — re-upload to be offered a URL")
        url = await (
            upload_to_catbox(local_path)
            if action == "cb"
            else upload_to_imgbb(local_path, ctx.settings.imgbb_api_key)
        )
    except Exception as exc:  # the admin sees why, and the row survives either way
        error = str(exc)[:180] or exc.__class__.__name__
    finally:
        await local_path.unlink(missing_ok=True)
    if not url:
        await _edit_plain(
            message,
            ctx,
            f"⚠️ <b>{host} upload failed</b>\n\n"
            f"🆔 <code>{entry.get('char_id')}</code> — {escape(str(entry.get('char_name') or ''))}\n"
            "📁 file_id: ✅ (saved as backup)\n"
            f"❌ Error: <code>{escape(error)}</code>\n\n"
            "<i>Character is still saved with file_id only.</i>",
        )
        PENDING_UPLOADS.pop(upload_id, None)
        return
    await _finish(callback_query, ctx, session, entry, url=url, host=host, upload_id=upload_id)


async def _edit_plain(message: Any, ctx: AppContext, html_text: str) -> None:
    """Edit a receipt. Rich mode is skipped on purpose: these are status lines."""
    if message is None or ctx.bot is None:  # pragma: no cover - defensive
        return
    try:
        await message.edit_text(html_text)
    except Exception as exc:
        log.debug("receipt edit skipped: %s", exc)


async def _finish(
    callback_query: CallbackQuery,
    ctx: AppContext,
    session: Any,
    entry: dict[str, Any],
    *,
    url: str,
    host: str = "",
    upload_id: str = "",
) -> None:
    """Store ``url`` on the character (when there is one) and rewrite the receipt."""
    message = callback_query.message
    character_id = int(entry.get("character_id") or 0)
    problem = ""
    if url:
        try:
            await char_repo.attach_media(session, character_id, image_url=url)
            char = await char_repo.get(session, character_id)
            if char is not None:
                upload_meta = {**(char.meta or {}).get("upload", {}), "hosts": {host: url}}
                char.meta = {**(char.meta or {}), "upload": upload_meta}
                await session.flush()
        except WaifuError as exc:
            problem = exc.user_message
    PENDING_UPLOADS.pop(upload_id, None)
    if not url:
        await _edit_plain(
            message,
            ctx,
            f"✅ <b>Done!</b>\n\n🆔 <code>{entry.get('char_id')}</code> — "
            f"{escape(str(entry.get('char_name') or ''))}\n"
            "📁 file_id: ✅\n🌐 URL: <i>skipped (file_id only)</i>",
        )
        await note(callback_query, "kept the file_id backup")
        return
    if problem:
        await _edit_plain(
            message,
            ctx,
            "⚠️ <b>Uploaded but DB save failed</b>\n\n"
            f"🆔 <code>{entry.get('char_id')}</code>\n🌐 {url}\n"
            f"❌ Reason: <code>{escape(problem)}</code>",
        )
        return
    await _edit_plain(
        message,
        ctx,
        f"✅ <b>Character Saved!</b>\n\n🆔 <code>{entry.get('char_id')}</code> — "
        f"{escape(str(entry.get('char_name') or ''))}\n"
        "📁 file_id: ✅\n"
        f"🌐 {host}: {url}",
    )
    await note(callback_query, f"{host} URL stored")


__all__ = [
    "PENDING_UPLOADS",
    "UPLOAD_KINDS",
    "autoadd_fields",
    "autoadd_router",
    "cap_pending",
    "columns_for",
    "fields_of",
    "media_of",
    "new_upload_id",
    "parse_tier",
    "parse_up_data",
    "prune_pending",
    "receipt",
    "router",
    "split_entry",
    "up_data",
]
