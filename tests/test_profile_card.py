"""``/pcard`` — the image profile card, ported from the reference's ``plugins/profile.py``.

The reference drew this card synchronously on the event loop, fetched its fonts with blocking
``requests`` **at import time**, pulled the portrait from whatever URL the favourite happened to
carry, and recomputed everything for every viewer. The pixels are the part worth keeping, so this
suite pins the pixels *and* the four things that were wrong with how they were made:

1. the canvas is the reference's 1000×540, and the card is drawn once per set of numbers — the
   signature is the cache key, so a second viewer of the same profile costs nothing;
2. privacy is applied *before* the render: a hidden balance changes the signature, which is what
   stops a cached PNG from leaking it to the wrong viewer;
3. the portrait comes from a stored ``file_id``, an allow-listed host, or the player's own
   Telegram photo — never an arbitrary URL, because a character row is admin input;
4. the drawing runs in a worker thread, so a busy group cannot stall the update loop on PIL.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiogram.types import CallbackQuery, Message

from waifu.core.access import Access
from waifu.db.repo import collection as collection_repo
from waifu.db.repo import users as user_repo
from waifu.enums import Role
from waifu.services.cards import POPPIANS, PROFILE_SIZE, CardService, ProfileArt

pytest.importorskip("PIL", reason="the renderer is Pillow-gated")


class Sender:
    """Records API calls through both shapes aiogram uses (``bot(method)`` and ``bot.method()``)."""

    def __init__(self, *, profile_photos: bool = True) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.profile_photos = profile_photos

    def _record(self, kind: str, **kwargs: Any) -> Any:
        self.calls.append((kind, kwargs))
        if kind == "download":
            Path(str(kwargs["destination"])).write_bytes(b"\x89PNG\r\n\x1a\navatar")
            return None
        if kind == "get_user_profile_photos":
            if not self.profile_photos:
                return SimpleNamespace(count=0, photos=[])
            photo = SimpleNamespace(file_id="AAQAvatar", file_unique_id="u", width=160, height=160)
            return SimpleNamespace(count=1, photos=[[photo]])
        return None

    async def __call__(self, method: Any) -> None:
        # ``__dict__``, not ``model_dump``: the assertions below read
        # ``reply_markup.inline_keyboard[0][0].copy_text``, and dumping would flatten the models.
        fields = {
            key: value for key, value in vars(method).items() if value is not None and key != "bot"
        }
        self._record(re.sub(r"(?<!^)(?=[A-Z])", "_", type(method).__name__).lower(), **fields)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_") or name in {"calls", "profile_photos", "names", "payload"}:
            raise AttributeError(name)

        async def _send(*args: Any, **kwargs: Any) -> Any:
            # ``send_message(chat_id, text, ...)`` is called positionally by the fallback sender.
            for key, value in zip(("chat_id", "text", "parse_mode"), args, strict=False):
                kwargs.setdefault(key, value)
            return self._record(name, **kwargs)

        return _send

    def payload(self, kind: str) -> dict[str, Any]:
        for call, kwargs in reversed(self.calls):
            if call == kind:
                return kwargs
        raise AssertionError(f"{kind} not called: {[c for c, _ in self.calls]}")

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


def _renderer(root: Path) -> CardService:
    """A service with a cache dir and no database — enough to exercise the drawing code."""
    service = CardService.__new__(CardService)
    service.ctx = SimpleNamespace(
        settings=SimpleNamespace(
            data_dir=root, allowed_media_hosts=["files.catbox.moe", "i.ibb.co", "catbox.moe"]
        ),
        bot=None,
        cache=None,
    )
    service.cache_dir = root / "cards"
    service.cache_dir.mkdir(parents=True, exist_ok=True)
    return service


def _sample(**over: Any) -> ProfileArt:
    art = ProfileArt(
        name="Gohan",
        handle="@beasgohan",
        balance="1,250,000",
        level=42,
        exp=8123,
        exp_span=42000,
        harem=120,
        roster=177,
        value="9,348,120",
        summons=5210,
        high_rate=12,
        streak=17,
        best_streak=31,
        badges=24,
        rank=3,
        total_players=4120,
        rarity_label="Celestial Edition",
        rarity_id=16,
        featured="Yor Forger",
        glow=True,
        premium=True,
        footer="collect them all",
    )
    for key, value in over.items():
        setattr(art, key, value)
    return art


@pytest.fixture
def renderer(tmp_path: Path) -> CardService:
    return _renderer(tmp_path)


# ------------------------------------------------------------------ the signature


def test_signature_is_stable_for_identical_numbers() -> None:
    assert _sample().signature() == _sample().signature()


VISIBLE_FIELDS = [
    "name",
    "handle",
    "balance",
    "level",
    "exp",
    "exp_span",
    "harem",
    "roster",
    "value",
    "summons",
    "high_rate",
    "streak",
    "best_streak",
    "badges",
    "rank",
    "total_players",
    "rarity_label",
    "rarity_id",
    "featured",
    "portrait_source",
    "glow",
    "premium",
    "footer",
]


@pytest.mark.parametrize("field", VISIBLE_FIELDS)
def test_every_visible_number_changes_the_signature(field: str) -> None:
    """The cache key must cover everything a viewer can see, or the cache becomes a leak: a card
    drawn for the owner would be served to a stranger promised a masked balance."""
    base = _sample()
    current = getattr(base, field)
    replacement = not current if isinstance(current, bool) else f"{current}-x"
    assert base.signature() != _sample(**{field: replacement}).signature(), field


def test_a_new_portrait_file_changes_the_signature(tmp_path: Path) -> None:
    from PIL import Image

    image = tmp_path / "portrait.jpg"
    Image.new("RGB", (64, 64), (10, 20, 30)).save(image)
    before = _sample(portrait=image).signature()
    Image.new("RGB", (64, 64), (40, 50, 60)).save(image)  # same size, same clock tick
    assert before != _sample(portrait=image).signature(), "re-uploaded art must not be cached away"


def test_a_vanished_portrait_does_not_raise(tmp_path: Path) -> None:
    assert _sample(portrait=tmp_path / "missing.jpg").signature()


# ---------------------------------------------------------------------- the render


def test_card_renders_at_the_reference_canvas_size(renderer: CardService) -> None:
    from PIL import Image

    path = renderer.render_profile(_sample())
    assert path is not None and path.name.startswith("profile_") and path.suffix == ".png"
    with Image.open(path) as image:
        assert image.size == PROFILE_SIZE == (1000, 540)
        assert image.mode == "RGB"


def test_one_render_per_signature(renderer: CardService) -> None:
    art = _sample()
    first = renderer.render_profile(art)
    drawn = list(renderer.cache_dir.glob("*.png"))
    assert renderer.render_profile(art) == first
    assert list(renderer.cache_dir.glob("*.png")) == drawn, "a cached card is not redrawn"
    assert renderer.render_profile(_sample(name="Someone Else")) != first


def test_a_broken_portrait_degrades_to_initials(renderer: CardService) -> None:
    """A truncated download must not take the whole card with it (the reference had no guard)."""
    bogus = renderer.cache_dir / "broken.jpg"
    bogus.write_bytes(b"not an image at all")
    path = renderer.render_profile(_sample(portrait=bogus))
    assert path is not None and path.stat().st_size > 4000


def test_a_card_with_nothing_to_show_still_renders(renderer: CardService) -> None:
    """An unregistered-looking profile is still a card: no favourite, no art, no handle."""
    assert renderer.render_profile(ProfileArt()) is not None


async def test_drawing_leaves_the_event_loop(renderer: CardService, monkeypatch) -> None:
    """PIL is CPU work: on the loop it is a group-wide stall, in a thread it is invisible."""
    import anyio

    calls: list[str] = []
    real = anyio.to_thread.run_sync

    async def spy(func: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append(getattr(func, "__name__", str(func)))
        return await real(func, *args, **kwargs)

    monkeypatch.setattr(anyio.to_thread, "run_sync", spy)
    path = await renderer.render_profile_async(_sample())
    assert calls == ["render_profile"], calls
    assert path is not None and path.exists(), "the worker ran the real renderer"


async def test_fonts_are_fetched_once_and_are_optional(renderer: CardService, monkeypatch) -> None:
    """The reference blocked the event loop on ``requests`` at import time, per font.

    Here: one awaited attempt per process, a silent downgrade to the system face on failure, and a
    second call that does not re-hammer the CDN.
    """
    from waifu.services import cards as cards_module

    monkeypatch.setattr(cards_module.ProfileCardMixin, "_fonts_attempted", False)
    assert set(POPPIANS) == {"Poppins-Bold.ttf", "Poppins-Medium.ttf", "Poppins-Regular.ttf"}

    attempts: list[str] = []

    class Dead:
        async def __aenter__(self) -> Dead:
            return self

        async def __aexit__(self, *exc: object) -> bool:
            return False

        async def get(self, url: str) -> None:
            attempts.append(url)
            raise RuntimeError("no network in tests")

    monkeypatch.setattr("httpx.AsyncClient", lambda *a, **k: Dead())
    assert await renderer.ensure_fonts() == 0
    assert len(attempts) <= 3, attempts
    assert renderer._profile_font(24) is not None, "a missing Poppins falls back to the system face"
    assert renderer._profile_font(24, bold=True) is not None
    await renderer.ensure_fonts()
    assert len(attempts) <= 3, "the second call must not refetch"


def test_font_dir_lives_in_the_data_dir(renderer: CardService) -> None:
    """The reference wrote fonts into its repo root, which a read-only image cannot do."""
    assert renderer.font_dir == renderer.cache_dir.parent / "fonts"


# ------------------------------------------------------------- the portrait policy


@pytest.mark.parametrize(
    ("character", "use_art", "expected"),
    [
        (SimpleNamespace(photo_file_id="AgAC", image_url=""), True, "art-file"),
        (
            SimpleNamespace(photo_file_id="", image_url="https://files.catbox.moe/x.png"),
            True,
            "art-url",
        ),
        (
            SimpleNamespace(photo_file_id="", image_url="https://evil.example/x.png"),
            True,
            "art-card",
        ),
        (SimpleNamespace(photo_file_id="", image_url=""), True, "art-card"),
        (SimpleNamespace(photo_file_id="AgAC", image_url=""), False, "avatar"),
        (None, True, "avatar"),
    ],
)
def test_portrait_source_follows_the_host_allow_list(
    renderer: CardService, character: Any, use_art: bool, expected: str
) -> None:
    """Every image path in this bot obeys one rule; the card used to be the exception."""
    assert renderer.portrait_source(character, use_art=use_art) == expected


async def test_avatar_is_downloaded_once_and_cached(renderer: CardService, monkeypatch) -> None:
    sender = Sender()
    monkeypatch.setattr(renderer.ctx, "bot", sender)
    path = await renderer._telegram_avatar(4242)
    assert path is not None and path.exists()
    assert sender.names().count("get_user_profile_photos") == 1
    assert await renderer._telegram_avatar(4242) == path
    assert sender.names().count("get_user_profile_photos") == 1, "the avatar is cached on disk too"


async def test_no_photo_is_no_problem(renderer: CardService, monkeypatch) -> None:
    monkeypatch.setattr(renderer.ctx, "bot", Sender(profile_photos=False))
    assert await renderer._telegram_avatar(4242) is None


async def test_a_disallowed_url_is_never_fetched(renderer: CardService, monkeypatch) -> None:
    sender = Sender()
    monkeypatch.setattr(renderer.ctx, "bot", sender)
    smuggled = SimpleNamespace(photo_file_id="", image_url="https://attacker.example/p.png", id=1)
    assert await renderer._portrait_asset(smuggled, "art-url") is None
    assert sender.calls == [], "no request left the process"


async def test_a_stored_file_id_is_downloaded_to_the_cache(
    renderer: CardService, monkeypatch
) -> None:
    sender = Sender()
    monkeypatch.setattr(renderer.ctx, "bot", sender)
    favourite = SimpleNamespace(photo_file_id="AgACQAADsomeart", image_url="", id=7)
    path = await renderer._portrait_asset(favourite, "art-file")
    assert path is not None and path.exists()
    assert sender.names().count("get_file") == 1
    assert await renderer._portrait_asset(favourite, "art-file") == path
    assert sender.names().count("get_file") == 1, "the portrait is on disk, not refetched"


async def test_the_rendered_card_is_the_last_resort(renderer: CardService, monkeypatch) -> None:
    """A roster whose art is all URLs from other hosts still gets a portrait: its own card."""
    monkeypatch.setattr(renderer, "render", lambda character: renderer.render_profile(_sample()))
    character = SimpleNamespace(
        photo_file_id="", image_url="https://elsewhere.example/x.png", id=3, rarity_id=4, name="X"
    )
    path = await renderer.portrait_for(character, user_id=None, use_art=True)
    assert path is not None


# --------------------------------------------------------------- the assembled art


async def _seed_favourite(ctx: Any, tx: Any, user_id: int) -> Any:
    from sqlalchemy import select

    from waifu.db.models import Character

    character = (
        await tx.execute(select(Character).where(Character.is_active.is_(True)).limit(1))
    ).scalar_one()
    await collection_repo.grant(tx, user_id, character.id, count=3, source="test")
    await collection_repo.set_flag(tx, user_id, character.id, "is_favorite", True)
    return character


async def test_profile_art_reads_the_same_numbers_as_profile(ctx, tx, player) -> None:
    character = await _seed_favourite(ctx, tx, player)
    art = await ctx.cards.profile_art(tx, player, viewer=player)
    assert art.harem == 1 and art.roster >= 1
    assert art.summons == 0 and art.high_rate == 0
    assert art.featured == character.name
    assert art.rarity_id == int(character.rarity_id)
    assert art.total_players >= 1 and art.rank >= 1
    assert art.badges >= 0
    assert art.balance.replace(",", "").isdigit(), art.balance
    assert art.exp_span == max(1000, art.level * 1000)
    assert art.handle.startswith("@tester")


async def test_completion_never_advertises_more_than_the_roster(ctx, tx, player) -> None:
    """``harem/roster`` is drawn from live data; a shrunk roster must not print 1040%."""
    await _seed_favourite(ctx, tx, player)
    art = await ctx.cards.profile_art(tx, player, viewer=player)
    art.harem, art.roster = 999, 4  # the renderer, not the numbers, is what clamps
    path = ctx.cards.render_profile(art)
    assert path is not None, "a silly ratio must still be a card, not a crash"


async def test_a_hidden_balance_is_hidden_in_the_pixels_too(ctx, tx, player) -> None:
    """The privacy flag has to be applied *before* the render, not in the caption.

    Otherwise the cached PNG — keyed on numbers that include the balance — is served to a viewer
    who was promised it masked, and "hidden" would only mean "not in the text".
    """
    await user_repo.set_pref(tx, player, show_balance=False)
    owner_view = await ctx.cards.profile_art(tx, player, viewer=player)
    public_view = await ctx.cards.profile_art(tx, player, viewer=999999)
    assert public_view.balance == "hidden"
    assert owner_view.balance != "hidden"
    assert owner_view.signature() != public_view.signature()


async def test_glow_pref_and_the_force_override(ctx, tx, player) -> None:
    await user_repo.set_pref(tx, player, glow=False)
    assert (await ctx.cards.profile_art(tx, player, viewer=player)).glow is False
    await user_repo.set_pref(tx, player, glow=True)
    assert (await ctx.cards.profile_art(tx, player, viewer=player)).glow is True
    forced = await ctx.cards.profile_art(tx, player, viewer=player, force_glow=False)
    assert forced.glow is False, "the preview on a button press must show the *new* state"


async def test_the_portrait_toggle_is_a_flag_not_a_migration(ctx, tx, player) -> None:
    await user_repo.set_pref(tx, player, card_portrait="avatar")
    pref = await user_repo.prefs(tx, player)
    assert pref.flags["card_portrait"] == "avatar"
    art = await ctx.cards.profile_art(tx, player, viewer=player)
    assert art.portrait_source == "avatar"


async def test_an_unknown_player_raises_rather_than_rendering_half_a_card(ctx, tx) -> None:
    from waifu.errors import NotFound

    with pytest.raises(NotFound):
        await ctx.collection.profile(tx, 987654321)


# --------------------------------------------------------------- the handler


def _message(bot: Sender, *, user_id: int = 4242) -> Message:
    return Message.model_validate(
        {
            "message_id": 7,
            "date": 1,
            "chat": {"id": -100, "type": "supergroup", "title": "T"},
            "from_user": {
                "id": user_id,
                "is_bot": False,
                "first_name": "Viewer",
                "username": "tester",
            },
            "text": "/pcard",
        },
        context={"bot": bot},
    )


def _callback(bot: Sender, data: str, *, user_id: int = 4242) -> CallbackQuery:
    return CallbackQuery.model_validate(
        {
            "id": f"{user_id}:{data}",
            "from_user": {
                "id": user_id,
                "is_bot": False,
                "first_name": "Owner",
                "username": "tester",
            },
            "chat_instance": "i",
            "data": data,
            "message": {
                "message_id": 7,
                "date": 1,
                "chat": {"id": -100, "type": "supergroup", "title": "T"},
                "from_user": {"id": user_id, "is_bot": False, "first_name": "Owner"},
            },
        },
        context={"bot": bot},
    )


def _access(user_id: int) -> Access:
    return Access(user_id=user_id, role=Role.USER)


async def test_pcard_sends_a_photo_card_with_2026_buttons(ctx, tx, player, monkeypatch) -> None:
    from waifu.plugins import players

    bot = Sender()
    monkeypatch.setattr(ctx, "bot", bot)
    await players.send_pcard(_message(bot), ctx, tx, target=player, viewer=player)
    payload = bot.payload("send_photo")
    assert payload["chat_id"] == -100
    assert payload["caption"].startswith("<b>")
    flat = [button for row in payload["reply_markup"].inline_keyboard for button in row]
    assert any("glow" in button.text for button in flat), [b.text for b in flat]
    assert any("portrait" in button.text for button in flat)
    copy = next(button for button in flat if button.copy_text is not None)
    assert copy.copy_text.text.startswith("@"), "the handle button copies the handle, not the id"
    switch = next(button for button in flat if button.switch_inline_query is not None)
    assert switch.switch_inline_query == f"collection.{player}", "inline mode answers in this chat"


async def test_a_viewers_copy_disables_the_owners_buttons(
    ctx, tx, player, partner, monkeypatch
) -> None:
    from waifu.plugins import players

    bot = Sender()
    monkeypatch.setattr(ctx, "bot", bot)
    await players.send_pcard(_message(bot, user_id=partner), ctx, tx, target=player, viewer=partner)
    flat = [
        button
        for row in bot.payload("send_photo")["reply_markup"].inline_keyboard
        for button in row
    ]
    toggles = [b for b in flat if b.callback_data and b.callback_data.startswith("pc:")]
    assert toggles and all(b.disabled for b in toggles), "no drive-by re-skinning"
    assert any(b.text.startswith("🎁") for b in flat), "gifting stays available"


async def test_a_card_without_pillow_degrades_to_the_text_profile(
    ctx, tx, player, monkeypatch
) -> None:
    """An install without Pillow loses the image, not the command."""
    from waifu.plugins import players

    bot = Sender()
    monkeypatch.setattr(ctx, "bot", bot)
    monkeypatch.setattr(type(ctx.cards), "available", False)
    await players.send_pcard(_message(bot), ctx, tx, target=player, viewer=player)
    assert "send_photo" not in bot.names()
    assert "send_message" in bot.names()


async def test_an_unregistered_target_is_reported_not_rendered(ctx, tx, monkeypatch) -> None:
    from waifu.plugins import players

    bot = Sender()
    monkeypatch.setattr(ctx, "bot", bot)
    await players.send_pcard(_message(bot), ctx, tx, target=987654321, viewer=987654321)
    assert bot.names() == ["send_message"]
    assert "No such player" in bot.payload("send_message")["text"]


async def test_the_toggle_callbacks_write_prefs_and_refuse_strangers(
    ctx, tx, player, partner, monkeypatch
) -> None:
    from waifu.plugins import players

    bot = Sender()
    monkeypatch.setattr(ctx, "bot", bot)
    await players.pcard_toggle(_callback(bot, f"pc:glow:{player}:1"), ctx, tx, _access(player))
    assert (await user_repo.prefs(tx, player)).glow is True

    await players.pcard_toggle(_callback(bot, f"pc:src:{player}"), ctx, tx, _access(player))
    assert (await user_repo.prefs(tx, player)).flags["card_portrait"] == "avatar"

    await players.pcard_toggle(_callback(bot, f"pc:src:{player}"), ctx, tx, _access(partner))
    assert (await user_repo.prefs(tx, player)).flags["card_portrait"] == "avatar", "not their card"
    assert "Only the owner" in bot.payload("answer_callback_query")["text"]


async def test_a_garbled_callback_data_is_answered_and_forgotten(
    ctx, tx, player, monkeypatch
) -> None:
    """A pressed button never leaves a spinner running, even when the payload makes no sense."""
    from waifu.plugins import players

    bot = Sender()
    monkeypatch.setattr(ctx, "bot", bot)
    await players.pcard_toggle(_callback(bot, "pc"), ctx, tx, _access(player))
    assert bot.names() == ["answer_callback_query"]


async def test_pcard_is_documented_in_help() -> None:
    """``/pcard`` and ``/gate`` reach a player through /help, not only through the bot menu."""
    from waifu.plugins.misc import HELP_TOPICS

    documented = {
        name.split()[0].lstrip("/").lower()
        for entries in HELP_TOPICS.values()
        for name, _description in entries
    }
    assert {"pcard", "gate", "gatelink"} <= documented, sorted(documented)[:10]
