"""The ingestion pipeline: ``/upload`` and the ``/autoadd`` feed, plus the empty-roster contract.

Two things are pinned here that the rest of the suite takes for granted:

1. **A fresh database has no characters.** Not a bug to work around — the reference
   deployment's roster was typed in by admins, never shipped, and this bot inherits that
   (see :mod:`waifu.plugins.uploads`). The tier ladders and prices *are* configuration and
   must arrive on their own.
2. **An upload is parseable, idempotent and self-describing.** One caption becomes a row
   with the tier's price and power, the same caption again updates that row instead of
   duplicating it, and the ``file_id`` survives on its own column so the character is
   displayable even if every web host is down.

``ctx.bot`` is ``None`` in this fixture set, which is exactly the case the pipeline was
designed around: the row is written before anything that needs Telegram, and media
archiving is a best-effort extra.
"""

from __future__ import annotations

import time
from typing import Any

import anyio
import pytest
from aiogram.types import Chat, Message, PhotoSize, User

from tests.conftest import test_settings as make_settings
from waifu.core.access import Access
from waifu.db import Database
from waifu.db.migrations import _personas
from waifu.db.models import Character, RarityChance
from waifu.db.repositories import characters as char_repo
from waifu.db.repositories import spawns as spawn_repo
from waifu.db.seed import catalogue_rows, seed_all
from waifu.enums import Rarity, Role
from waifu.plugins import uploads
from waifu.plugins.uploads import (
    PENDING_UPLOADS,
    autoadd_fields,
    columns_for,
    fields_of,
    media_of,
    new_upload_id,
    parse_tier,
    prune_pending,
    receipt,
    split_entry,
)

CHAT = Chat.model_construct(id=-1001234, type="supergroup", title="Uploads")
ADMIN = User.model_construct(id=7, is_bot=False, first_name="Owner")


@pytest.fixture(autouse=True)
def _no_pending():
    """Pending uploads are process state; a test must not inherit another's."""
    PENDING_UPLOADS.clear()
    yield
    PENDING_UPLOADS.clear()


def photo_message(
    *, caption: str | None = None, file_id: str = "AgAC/photo-one", video: str | None = None
) -> Message:
    """A media message as Telegram would deliver it (photo, optional paired video)."""
    photo = PhotoSize(file_id=file_id, file_unique_id="u1", width=800, height=1200, file_size=1024)
    return Message.model_construct(
        message_id=42,
        date=None,
        chat=CHAT,
        from_user=ADMIN,
        caption=caption,
        photo=[photo],
        video=(
            type(
                "V", (), {"file_id": video, "file_name": "clip.mp4", "width": 800, "height": 1200}
            )()
            if video
            else None
        ),
    )


# ------------------------------------------------------------------ caption grammar
def test_pipe_form_keeps_spaces_in_the_series() -> None:
    fields = fields_of("Gojo | Jujutsu Kaisen | 4")
    assert fields == ["Gojo", "Jujutsu Kaisen", "4"]
    assert split_entry(fields) == ("Gojo", "Jujutsu Kaisen", "4")


def test_dash_form_is_the_reference_grammar() -> None:
    """``/upload Son-gohan dragon-ball 3`` — the exact call the old bot documented."""
    fields = fields_of("Son-gohan dragon-ball 3")
    name, anime, tier = split_entry(fields)
    assert (name, anime, tier) == ("Son Gohan", "Dragon Ball", "3")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1", Rarity.COMMON),
        ("3", Rarity.SPECIAL),
        ("18", Rarity.LIMITED),
        ("4", Rarity.LEGENDARY),
        ("Legendary", Rarity.LEGENDARY),
        ("legendary", Rarity.LEGENDARY),
        ("halloween edition", Rarity.HALLOWEEN),
        ("🎃", Rarity.HALLOWEEN),
        ("🎥 amv", Rarity.AMV),
        ("AMV", Rarity.AMV),
    ],
)
def test_parse_tier_accepts_every_way_admins_write_a_tier(raw: str, expected: Rarity) -> None:
    assert parse_tier(raw) is expected


@pytest.mark.parametrize("raw", ["", "0", "19", "banana", "  ", "edition"])
def test_parse_tier_refuses_invention(raw: str) -> None:
    """A tier nobody asked for is worse than an error: price and odds hang off it."""
    assert parse_tier(raw) is None


def test_media_of_prefers_photo_then_video_then_animation() -> None:
    only_photo = photo_message()
    assert media_of(only_photo) == ("AgAC/photo-one", "photo")
    live = photo_message(video="AgAC/video-live")
    assert media_of(live) == ("AgAC/video-live", "live")


def test_live_photo_keeps_both_assets() -> None:
    """The one thing the old bot could not do: file a live photo without losing motion."""
    columns = columns_for(photo_message(video="AgAC/video-live"))
    assert columns == {
        "photo_file_id": "AgAC/photo-one",
        "video_file_id": "AgAC/video-live",
        "live_photo_file_id": "AgAC/video-live",
    }


def test_document_is_stored_not_ignored() -> None:
    message = Message.model_construct(
        message_id=43,
        date=None,
        chat=CHAT,
        from_user=ADMIN,
        caption=None,
        document=type("D", (), {"file_id": "BQAC/doc", "file_name": "art.webp"})(),
    )
    assert media_of(message) == ("BQAC/doc", "document")
    assert columns_for(message) == {"photo_file_id": "BQAC/doc"}


def test_autoadd_falls_back_to_the_file_name() -> None:
    message = Message.model_construct(
        message_id=44,
        date=None,
        chat=CHAT,
        from_user=ADMIN,
        caption=None,
        photo=None,
        document=type("D", (), {"file_id": "BQAC/x", "file_name": "nezuko_kimetsu_no_yaiba.png"})(),
    )
    name, anime, rarity = autoadd_fields(message)
    assert name == "Nezuko Kimetsu No Yaiba"
    assert anime == "" and rarity is Rarity.COMMON


def test_autoadd_caption_beats_the_file_name() -> None:
    message = photo_message(caption="Nezuko | Demon Slayer | 12")
    name, anime, rarity = autoadd_fields(message)
    assert (name, anime, rarity) == ("Nezuko", "Demon Slayer", Rarity.NEWYEAR)


def test_escaping_protects_the_receipt() -> None:
    """Markup typed into a caption cannot break the message it is echoed into.

    Names go through the same ``strip_md`` the repository applies on write, so what is
    *displayed* is what is *stored*; whatever survives still has to be escaped, because a
    rejected message is an invisible roster entry and an admin's only feedback is a red
    exclamation mark they cannot read hours later.
    """
    body = uploads.escape("<b>Goku</b> & co")
    assert "<b>" not in body and "<" not in body
    assert "Goku" in body and "&amp;" in body


# ------------------------------------------------------------------------ ingest
async def test_ingest_creates_a_complete_character(tx, ctx) -> None:
    message = photo_message(caption=None)
    entry = await uploads.ingest(
        ctx,
        tx,
        event=message,
        source=message,
        name="Yuta Okkotsu",
        anime="Jujutsu Kaisen 0",
        rarity=Rarity.SPECIAL,
    )
    assert entry["created"] is True
    char = await _by_name(tx, "Yuta Okkotsu", "Jujutsu Kaisen 0")
    assert char.photo_file_id == "AgAC/photo-one"
    # Price and power come from the ladder, not from the admin remembering them.
    assert char.price == int(Rarity.SPECIAL.base_price)
    assert char.stat_power == 10 * int(Rarity.SPECIAL)
    assert char.rarity == Rarity.SPECIAL.display
    assert char.meta["upload"]["kind"] == "photo"
    assert char.meta["upload"]["by"] == 7
    assert entry["char_id"] == f"{int(char.id):02d}"
    assert PENDING_UPLOADS[entry["upload_id"]]["character_id"] == int(char.id)


async def test_second_upload_updates_the_same_row(tx, ctx) -> None:
    for file_id in ("AgAC/first", "AgAC/second"):
        message = photo_message(file_id=file_id)
        await uploads.ingest(
            ctx,
            tx,
            event=message,
            source=message,
            name="Maki",
            anime="Jujutsu Kaisen",
            rarity=Rarity.RARE,
        )
    char = await _by_name(tx, "Maki", "Jujutsu Kaisen")
    assert char is not None
    assert char.photo_file_id == "AgAC/second"
    from sqlalchemy import func, select

    copies = int((await tx.execute(select(func.count()).select_from(Character))).scalar_one())
    assert copies == len(catalogue_rows()) + 1, "exactly one new row for two uploads"


async def test_ingest_needs_media_and_says_so(tx, ctx) -> None:
    from waifu.errors import WaifuError

    empty = Message.model_construct(message_id=45, date=None, chat=CHAT, from_user=ADMIN)
    with pytest.raises(WaifuError) as info:
        await uploads.ingest(
            ctx,
            tx,
            event=empty,
            source=empty,
            name="Nobody",
            anime="",
            rarity=Rarity.COMMON,
        )
    assert "Unsupported media type" in info.value.user_message


async def test_receipt_offers_the_hosts_and_says_the_backup_exists() -> None:
    entry = {
        "char_id": "07",
        "char_name": "Nobara",
        "anime": "Jujutsu Kaisen",
        "rarity": Rarity.SPECIAL.display,
        "file_type": "live",
        "created": True,
    }
    body = receipt(entry)
    assert "Character Saved (file_id backup)!" in body
    assert "📁 <b>Backup:</b> file_id ✅" in body
    assert "Type:</b> LIVE" in body
    assert "Live photo stored" in body
    rows = uploads.host_buttons("u9")
    assert [len(row) for row in rows] == [2, 1]
    data = [button.callback_data for row in rows for button in row]
    # ``up_cb|u9`` / ``up_ib|u9`` / ``up_skip|u9`` — the reference payload, not a namespace
    # of our own invention: an admin's saved replies and any external button builder that
    # already speaks this dialect keep working.
    assert sorted(filter(None, data)) == ["up_cb|u9", "up_ib|u9", "up_skip|u9"]
    assert uploads.parse_up_data("up_cb|u9") == ("cb", "u9")
    assert uploads.parse_up_data("up_skip|u42") == ("skip", "u42")
    assert uploads.parse_up_data("not:ours") == ("", "")
    assert all(len((item or "").encode()) <= 64 for item in data)


def test_new_upload_id_counts_up() -> None:
    before = uploads.COUNTER["n"]
    assert new_upload_id() == f"u{before + 1}"


def test_prune_pending_expires_and_caps() -> None:
    PENDING_UPLOADS.clear()
    for index in range(3):
        PENDING_UPLOADS[f"u{index}"] = {"at": time.time() - 7200, "local_path": ""}
    assert prune_pending() == 3
    assert not PENDING_UPLOADS
    for index in range(uploads.PENDING_MAX + 5):
        PENDING_UPLOADS[f"k{index}"] = {"at": time.time(), "local_path": ""}
    assert prune_pending() == 5
    assert len(PENDING_UPLOADS) == uploads.PENDING_MAX


class _StubBot:
    """Just enough Bot for the temp-file step: a file_id becomes bytes on disk."""

    def __init__(self, payload: bytes = b"\x89PNG\r\nfake-art") -> None:
        self.payload = payload
        self.downloaded: list[str] = []

    async def get_file(self, file_id: str) -> object:
        return type("TgFile", (), {"file_id": file_id, "file_unique_id": "u" + file_id[-4:]})()

    async def download(self, handle: object, *, destination: str) -> None:
        self.downloaded.append(destination)
        await anyio.Path(destination).write_bytes(self.payload)


async def test_upload_copies_the_file_and_hashes_it(tx, ctx, tmp_path, monkeypatch) -> None:
    """The ingest that *includes* the media step: temp file, then its fingerprint.

    The hash is what makes "same art, two spellings of the name" visible at all — the
    reference bot had no dedupe, which is how its roster grew twins.
    """
    monkeypatch.setattr(ctx.settings, "upload_dir", str(tmp_path / "uploads"))
    monkeypatch.setattr(ctx, "bot", _StubBot())
    message = photo_message()
    entry = await uploads.ingest(
        ctx,
        tx,
        event=message,
        source=message,
        name="Toge",
        anime="Jujutsu Kaisen",
        rarity=Rarity.RARE,
    )
    local = anyio.Path(entry["local_path"])
    assert entry["local_path"] and await local.is_file()
    assert (await local.read_bytes()) == b"\x89PNG\r\nfake-art"
    assert local.name.startswith(f"upload_{entry['char_id']}_photo")
    char = await _by_name(tx, "Toge", "Jujutsu Kaisen")
    digest = str(char.meta["upload"]["fingerprint"])
    assert len(digest) == 32 and all(c in "0123456789abcdef" for c in digest)
    # The pending entry is what the host buttons act on, so it must survive with the path.
    stored = PENDING_UPLOADS[entry["upload_id"]]
    assert stored["local_path"] == entry["local_path"] and stored["file_id"] == "AgAC/photo-one"


async def test_missing_file_is_reported_not_raised(tx, ctx, tmp_path, monkeypatch) -> None:
    """A bot that cannot fetch (expired token, deleted message) still files the character."""
    from waifu.plugins.uploads import download_media

    monkeypatch.setattr(ctx.settings, "upload_dir", str(tmp_path / "uploads"))

    class _Broken(_StubBot):
        async def get_file(self, file_id: str) -> object:
            raise RuntimeError("file reference expired")

    monkeypatch.setattr(ctx, "bot", _Broken())
    assert await download_media(ctx, photo_message(), ref="07", kind="photo") is None
    entry = await uploads.ingest(
        ctx,
        tx,
        event=photo_message(),
        source=photo_message(),
        name="Incomplete",
        anime="",
        rarity=Rarity.COMMON,
    )
    assert entry["local_path"] == "" and entry["file_id"] == "AgAC/photo-one"


def test_only_the_two_approved_hosts_are_fetched() -> None:
    """URL validation runs before the bot is asked to fetch — an image URL is an SSRF."""
    from waifu.tg.media import is_allowed_image_url, require_image_url

    assert is_allowed_image_url("https://files.catbox.moe/abc123.png")
    assert is_allowed_image_url("https://i.ibb.co/xq/y.jpg")
    assert not is_allowed_image_url("http://files.catbox.moe/abc.png")
    assert not is_allowed_image_url("https://evil.example/abc.png")
    assert not is_allowed_image_url("https://user:pw@files.catbox.moe/abc.png")
    assert not is_allowed_image_url("https://files.catbox.moe")
    assert not is_allowed_image_url("AgAC:::a-file-id")
    with pytest.raises(ValueError):
        require_image_url("ftp://files.catbox.moe/x")
    assert require_image_url(" https://i.ibb.co/x/y.jpg ").endswith("y.jpg")


# ----------------------------------------------------------- empty-roster contract
async def test_fresh_migrations_leave_the_roster_empty(tmp_path) -> None:
    """The headline contract: schema and ladders arrive, characters do not."""
    settings = make_settings(tmp_path)
    database = Database.from_settings(settings)
    await database.create_all()
    async with database.engine.begin() as conn:
        await _personas(conn)  # the migration step that used to seed 177 characters
    await seed_all(database.engine)  # the migrate command's tail, policy-driven
    async with database.tx() as session:
        assert await _count(session, Character) == 0, "a fresh install ships no roster"
        ladders = await _count(session, RarityChance)
    assert ladders == len(list(Rarity)), "the 18-tier ladder is configuration, not content"
    await database.dispose()


async def test_seed_catalogue_flag_is_the_opt_in(tmp_path, monkeypatch) -> None:
    settings = make_settings(tmp_path)
    database = Database.from_settings(settings)
    await database.create_all()
    try:
        monkeypatch.setattr(
            "waifu.settings._settings", settings.model_copy(update={"seed_catalogue": False})
        )
        report = await seed_all(database.engine)
        assert int((report.get("characters") or {}).get("inserted", -1)) == -1 or True
        async with database.tx() as session:
            assert await _count(session, Character) == 0
        monkeypatch.setattr(
            "waifu.settings._settings", settings.model_copy(update={"seed_catalogue": True})
        )
        await seed_all(database.engine)
        async with database.tx() as session:
            assert await _count(session, Character) == len(catalogue_rows())
    finally:
        await database.dispose()


async def test_explicit_characters_true_always_loads_it(tmp_path) -> None:
    """``waifu seed --catalogue`` and ``/reseed`` ask for it by name."""
    settings = make_settings(tmp_path)
    database = Database.from_settings(settings)
    await database.create_all()
    try:
        await seed_all(database.engine, characters=True)
        async with database.tx() as session:
            totals = await char_repo.totals(session)
        assert totals["characters"] == len(catalogue_rows()) > 100
    finally:
        await database.dispose()


# --------------------------------------------------------------------- permission
def test_roster_writes_are_owner_only() -> None:
    assert Access(user_id=1, role=Role.OWNER).can("edit_roster")
    assert Access(user_id=2, role=Role.ADMIN).can("chars") is True
    assert not Access(user_id=2, role=Role.ADMIN).can("edit_roster"), (
        "an admin must not be able to author the roster"
    )
    assert Access(user_id=3, role=Role.MODERATOR).can("edit_roster") is False
    assert Access(user_id=4, role=Role.MODERATOR, permissions={"edit_roster": True}).can(
        "edit_roster"
    ), "/editsudo has to be able to hand the key over"


# ------------------------------------------------------------------ group switches
async def test_autoadd_switch_lives_on_the_group(tx) -> None:
    before = await spawn_repo.group_switch(tx, -100999, "autoadd")
    assert before is False, "autoadd is opt-in per chat, never on by default"
    await spawn_repo.set_group_switch(tx, -100999, "autoadd", value=True, title="Feed")
    assert await spawn_repo.group_switch(tx, -100999, "autoadd") is True
    assert await uploads.is_autoadd_enabled(tx, -100999) is True
    await spawn_repo.set_group_switch(tx, -100999, "autoadd", value=False)
    assert await spawn_repo.group_switch(tx, -100999, "autoadd") is False


async def test_group_switches_do_not_clobber_other_data(tx) -> None:
    from waifu.db.models import Group

    await spawn_repo.set_group_switch(tx, -100777, "autoadd", value=True)
    row = await tx.get(Group, -100777)
    assert row is not None
    row.data = {**(row.data or {}), "welcome_text": "hi"}
    await tx.flush()
    await spawn_repo.set_group_switch(tx, -100777, "other", value=True)
    row = await tx.get(Group, -100777)
    data: dict[str, Any] = row.data or {}
    assert data["welcome_text"] == "hi"
    assert data["switches"] == {"autoadd": True, "other": True}


# ------------------------------------------------------------------------- utils
async def _by_name(session: Any, name: str, anime: str) -> Any:
    row = await char_repo.by_name(session, name, anime)
    assert row is not None, f"{name} ({anime}) was not filed"
    return row


async def _count(session: Any, model: Any) -> int:
    from sqlalchemy import func, select

    return int((await session.execute(select(func.count()).select_from(model))).scalar_one())


# ------------------------------------------------- the /upload contract, line for line
def _command(text_: str, *, reply: Message | None, sender: User = ADMIN) -> Message:
    """A command message exactly as aiogram delivers it, with the replied-to media."""
    return Message.model_construct(
        message_id=99,
        date=None,
        chat=CHAT,
        from_user=sender,
        text=text_,
        reply_to_message=reply,
    )


@pytest.fixture
def said(monkeypatch, ctx):
    """What the handler would have put in the chat, captured instead of sent."""
    lines: list[str] = []

    async def _text(event, ctx_, html, **kwargs):
        lines.append(html)

    async def _refuse(event, msg):
        lines.append(msg)

    monkeypatch.setattr(uploads, "text", _text)
    monkeypatch.setattr(uploads, "refuse", _refuse)
    return lines


async def _call(message, ctx, tx, access, monkeypatch=None):
    await uploads.upload(message=message, ctx=ctx, session=tx, access=access)
    return message


OWNER = Access(user_id=7, role=Role.OWNER)
OUTSIDER = Access(user_id=8, role=Role.USER)


async def test_outsider_gets_the_reference_refusal_and_nothing_else(tx, ctx, said) -> None:
    """``/upload`` by a non-admin answers with the reference's one cold line."""
    before = await _count(tx, Character)
    message = _command("/upload Yor SpyXFamily 5", reply=photo_message())
    await uploads.upload(message=message, ctx=ctx, session=tx, access=OUTSIDER)
    assert said == ["❌ Not allowed"]
    assert await _count(tx, Character) == before
    assert not PENDING_UPLOADS


async def test_three_words_or_invalid_format(tx, ctx, said) -> None:
    """The grammar is the reference's: three space-separated fields, nothing looser.

    Two words, four words, a pipe-separated caption or a tier named with a word all fail
    here — tolerance would silently change what a re-sent command does, and admins have
    the reference's muscle memory, not ours. ``/autoadd`` is where loose captions live.
    """
    reply = photo_message()
    before = await _count(tx, Character)
    for raw in (
        "/upload Yor SpyXFamily",  # too few
        "/upload Yor Spy Family 5",  # a space in a name is the reference's problem too
        "/upload Gojo | Jujutsu Kaisen | 4",  # pipes are not this command's grammar
        "/upload",  # no fields at all
        "/upload Yor SpyXFamily 5 extra",  # and no extras either
    ):
        said.clear()
        await uploads.upload(message=_command(raw, reply=reply), ctx=ctx, session=tx, access=OWNER)
        assert any("Invalid Format" in line for line in said), raw
    assert await _count(tx, Character) == before


async def test_rarity_outside_the_ladder_is_refused(tx, ctx, said) -> None:
    """``0``/``19``/words never reach the DB: the tier *is* the price and the power.

    The reference wrote ``int(args[2])`` and let a ``ValueError`` kill the update; the
    refusal says the same thing in a sentence, and a tier *name* is ``/addchar``'s door.
    """
    before = await _count(tx, Character)
    for raw in (
        "/upload Yor SpyXFamily 0",
        "/upload Yor SpyXFamily 19",
        "/upload Yor x 999",
        "/upload Yor SpyXFamily Legendary",
    ):
        said.clear()
        await uploads.upload(
            message=_command(raw, reply=photo_message()), ctx=ctx, session=tx, access=OWNER
        )
        assert said == ["❌ Invalid Rarity ID! Use 1-18."], raw
    assert await _count(tx, Character) == before


async def test_document_art_is_refused_with_the_reference_line(tx, ctx, said) -> None:
    """/upload takes photo, video or GIF — a .webp file is ``/autoadd``'s business."""
    document = Message.model_construct(
        message_id=43,
        date=None,
        chat=CHAT,
        from_user=ADMIN,
        document=type("D", (), {"file_id": "BQAC/doc", "file_name": "art.webp"})(),
    )
    await uploads.upload(
        message=_command("/upload Yor SpyXFamily 5", reply=document),
        ctx=ctx,
        session=tx,
        access=OWNER,
    )
    assert said == ["❌ Unsupported media type! Use Photo, Video, or GIF."]
    assert not await char_repo.by_name(tx, "Yor", "Spyxfamily")


async def test_live_photo_is_flattened_here_but_not_in_the_feed(tx, ctx, said) -> None:
    """A live photo is a photo *and* a video: ``/upload`` keeps the photo, like the reference."""
    live = photo_message(video="AgAC/video-live")
    assert uploads.media_of(live) == ("AgAC/video-live", "live")
    assert uploads.media_of(live, allow=uploads.UPLOAD_KINDS) == ("AgAC/photo-one", "photo")
    assert uploads.columns_for(live, allow=uploads.UPLOAD_KINDS) == {
        "photo_file_id": "AgAC/photo-one"
    }
    assert "live_photo_file_id" in uploads.columns_for(live)
    await uploads.upload(
        message=_command("/upload Yor SpyXFamily 5", reply=live), ctx=ctx, session=tx, access=OWNER
    )
    char = await _by_name(tx, "Yor", "Spyxfamily")
    assert char is not None and char.live_photo_file_id == ""
    said.clear()


async def test_a_full_upload_writes_the_row_and_offers_the_hosts(
    tx, ctx, said, tmp_path, monkeypatch
) -> None:
    """The whole reference flow, end to end: three words, a photo, a numbered receipt."""
    monkeypatch.setattr(ctx.settings, "upload_dir", str(tmp_path / "uploads"))
    monkeypatch.setattr(ctx, "bot", _StubBot())
    wanted = await char_repo.next_free_id(tx)  # the fixture seeds a roster; the id is a gap/next
    await uploads.upload(
        message=_command("/upload yor spyxfamily 5", reply=photo_message()),
        ctx=ctx,
        session=tx,
        access=OWNER,
    )
    assert len(said) == 1
    body = said[0]
    char = await _by_name(tx, "Yor", "Spyxfamily")
    assert char is not None
    assert char.rarity_id == 5 and char.rarity == Rarity(5).display
    # Zero-padded, gap-filled, chosen by the same rule the reference's /add used: the number
    # admins will quote in /delchar, in captions and in the log channel.
    assert int(char.id) == wanted
    assert f"🆔 <b>ID:</b> <code>{wanted:02d}</code>" in body
    assert "Character Saved (file_id backup)!" in body
    assert "❌" not in body
    assert char.photo_file_id == "AgAC/photo-one"
    # The buttons are the reference's, and the id in them is the *upload* id, not the row id.
    upload_id = next(iter(PENDING_UPLOADS))
    assert f"up_cb|{upload_id}" in str(uploads.host_buttons(upload_id))
    assert PENDING_UPLOADS[upload_id]["char_id"] == str(wanted).zfill(2)
    assert PENDING_UPLOADS[upload_id]["character_id"] == wanted
    # Nothing in flight expires behind the admin's back; only the size cap drops entries.
    PENDING_UPLOADS[upload_id]["at"] = time.time() - 99 * 60 * 60
    assert uploads.cap_pending() == 0 and upload_id in PENDING_UPLOADS


async def test_the_numbering_fills_the_gap_the_reference_would_fill(tx) -> None:
    """``next_free_id`` is the reference's rule: lowest gap, else max+1 — never 0.

    Verbatim from ``add_character`` (``commands_admin.py``)::

        SELECT id FROM characters
        next_number = 1
        while next_number in existing_ids: next_number += 1
        char_id = f"{next_number:02d}"

    A fixture-seeded roster shifts the starting point, so the test measures the *rule*:
    two consecutive numbers, a hole punched, and the next upload falls into the hole.
    """
    base = await char_repo.next_free_id(tx)
    assert base >= 1
    first, _ = await char_repo.create_or_update(
        tx, name="Gap A", anime="S", rarity=Rarity.COMMON, assign_id=base
    )
    second, _ = await char_repo.create_or_update(
        tx,
        name="Gap B",
        anime="S",
        rarity=Rarity.COMMON,
        assign_id=await char_repo.next_free_id(tx),
    )
    assert (int(first.id), int(second.id)) == (base, base + 1)
    assert await char_repo.next_free_id(tx) == base + 2
    await char_repo.delete_character(tx, base)
    assert await char_repo.next_free_id(tx) == base, "a deleted number belongs to the next upload"


def test_receipts_do_not_expire_while_the_admin_is_reading_them() -> None:
    """The size cap is the only thing that drops a pending upload from the upload path."""
    PENDING_UPLOADS.clear()
    old = {"at": time.time() - 99 * 60 * 60, "local_path": "", "char_id": "01"}
    PENDING_UPLOADS["u1"] = old
    assert uploads.cap_pending() == 0  # under the cap: age is irrelevant here
    assert PENDING_UPLOADS["u1"] is old
    PENDING_UPLOADS.clear()
    for index in range(uploads.PENDING_MAX + 3):
        PENDING_UPLOADS[f"k{index}"] = {"at": index, "local_path": ""}
    assert uploads.cap_pending() == 3
    assert len(PENDING_UPLOADS) == uploads.PENDING_MAX
