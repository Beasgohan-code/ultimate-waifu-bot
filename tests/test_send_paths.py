"""The two helpers every plugin ends in: ``mode_of`` and the ``card``/``text``/``note`` trio.

These are the last lines of a handler, which is exactly why they went untested for so long: unit
tests stop at the service layer and integration tests need a real API server. That gap let
``mode_of`` return ``ChatMode.HTML`` — a member the enum does not have — so *every* send to an
older server raised ``AttributeError`` instead of degrading to a caption, and it let
:meth:`waifu.tg.caps.Capabilities.card_mode` name a second member that did not exist either.

So this file drives the helpers directly, against a recording bot, and pins four things: the enum's
real membership, the mode resolution (including that a typo in a setting is never a crash), the
photo/caption fallback — a locally rendered card must be uploaded, not sent as a path string — and
that a callback is always answered, because an unanswered callback leaves the user's spinner
turning for a minute and players read that as the bot being down.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from aiogram.types import CallbackQuery, Chat, FSInputFile, Message

from waifu.enums import ChatMode
from waifu.plugins._kit import card, mode_of, note, text

ROOT = Path(__file__).resolve().parents[1]
_CODE_ONLY = re.compile(r'""".*?"""|\'\'\'.*?\'\'\'', re.S)


class Sender:
    """Records API calls through both shapes aiogram uses: ``bot(method)`` and ``bot.method(...)``."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _record(self, kind: str, **kwargs: Any) -> Any:
        self.calls.append((kind, kwargs))

    async def __call__(self, method: Any) -> None:
        self._record(
            re.sub(r"(?<!^)(?=[A-Z])", "_", type(method).__name__).lower(),
            **method.model_dump(exclude_none=True, exclude={"bot"}),
        )

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_") or name in {"calls", "names", "payload"}:
            raise AttributeError(name)

        async def _send(*args: Any, **kwargs: Any) -> None:  # `name` may legitimately be a kwarg
            # ``send_message(chat_id, text, ...)`` is called positionally in places, and a recorder
            # that drops positionals would "lose" the text it is supposed to assert on.
            for key, value in zip(("chat_id", "text", "parse_mode"), args, strict=False):
                kwargs.setdefault(key, value)
            self._record(name, **kwargs)

        return _send

    def payload(self, name: str) -> dict[str, Any]:
        for call, kwargs in reversed(self.calls):
            if call == name:
                return kwargs
        raise AssertionError(f"{name} not called: {[c for c, _ in self.calls]}")

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


def _message(bot: Sender, *, chat_id: int = -100, message_id: int = 4) -> Message:
    return Message.model_validate(
        {
            "message_id": message_id,
            "date": 1,
            "chat": {"id": chat_id, "type": "supergroup", "title": "T"},
            "from_user": {"id": 5, "is_bot": False, "first_name": "P"},
            "text": "hi",
        },
        context={"bot": bot},
    )


def _callback(bot: Sender, data: str = "x:y") -> CallbackQuery:
    return CallbackQuery.model_validate(
        {
            "id": "1",
            "from_user": {"id": 5, "is_bot": False, "first_name": "P"},
            "chat_instance": "i",
            "data": data,
        },
        context={"bot": bot},
    )


# ------------------------------------------------------------------ the enum itself


def test_chat_mode_members_are_the_four_the_code_names() -> None:
    """``HTML`` never existed; ``AUTO`` had to be added. This is the tripwire for both."""
    assert {member.name for member in ChatMode} == {"AUTO", "OFF", "RICH", "PLAIN"}
    assert "HTML" not in ChatMode.__members__, "the fallback mode is called PLAIN"


def test_mode_parsing_never_raises() -> None:
    """A typo in a setting is a config bug, not a crash at startup."""
    assert ChatMode.from_value("auto") is ChatMode.AUTO
    assert ChatMode.from_value("") is ChatMode.AUTO
    assert ChatMode.from_value(None) is ChatMode.AUTO
    assert ChatMode.from_value("gibberish") is ChatMode.AUTO
    assert ChatMode.from_value("OFF") is ChatMode.OFF
    assert ChatMode.from_value("html") is ChatMode.PLAIN
    assert ChatMode.from_value(ChatMode.RICH) is ChatMode.RICH


def test_card_mode_asks_the_server_before_promising_rich_blocks(ctx, monkeypatch) -> None:
    """The whole point of AUTO: identical behaviour on api.telegram.org and on a five-year-old
    self-hosted server — and an explicit ``off`` is honoured either way."""
    from waifu.tg.caps import Caps

    rich = Caps(rich_messages=True)
    plain = Caps(rich_messages=False)
    assert rich.card_mode("auto", settings=ctx.settings) is ChatMode.RICH
    assert rich.card_mode(ChatMode.OFF, settings=ctx.settings) is ChatMode.OFF
    assert rich.card_mode("nonsense", settings=ctx.settings) is ChatMode.RICH
    assert plain.card_mode("auto", settings=ctx.settings) is ChatMode.PLAIN
    assert plain.card_mode("rich", settings=ctx.settings) is ChatMode.PLAIN


@pytest.mark.parametrize(
    ("wants", "expected"),
    [(True, ChatMode.RICH), (False, ChatMode.PLAIN)],
)
def test_mode_of_falls_back_to_the_capability_probe(
    ctx, monkeypatch, wants: bool, expected: ChatMode
) -> None:
    """The fixture's ``caps`` stub has no ``card_mode`` — that fallback branch must still answer
    with a real mode instead of assuming the full context."""
    monkeypatch.setattr(type(ctx), "wants", lambda self, name: wants)
    assert mode_of(ctx) is expected


def test_no_invented_enum_member_or_removed_aiogram_helper() -> None:
    """A grep guard over the source (docstrings excluded): the two names that looked right and
    were not — ``ChatMode.HTML`` and aiogram's removed ``Chat.is_private`` — stay out."""
    offenders: list[str] = []
    for path in sorted((ROOT / "waifu").rglob("*.py")):
        code = _CODE_ONLY.sub("", path.read_text(encoding="utf-8"))
        if "ChatMode.HTML" in code:
            offenders.append(f"{path.name}: ChatMode.HTML")
        if ".is_private" in code and path.name != "chats.py":
            offenders.append(f"{path.name}: chat.is_private (removed in aiogram 3.7)")
    assert not offenders, f"dead attribute access is back: {offenders}"


# --------------------------------------------------------------------- the sends


async def test_text_sends_html_caption_to_the_right_chat(ctx, monkeypatch) -> None:
    bot = Sender()
    monkeypatch.setattr(ctx, "bot", bot)
    await text(_message(bot), ctx, "<b>hi</b>")
    payload = bot.payload("send_message")
    assert payload["chat_id"] == -100
    assert payload["reply_to_message_id"] == 4, "a card belongs to the message that asked for it"
    assert payload["parse_mode"] == "HTML"
    assert payload["text"] == "<b>hi</b>"


async def test_card_without_a_builder_uses_the_photo_fallback(ctx, monkeypatch, tmp_path) -> None:
    """A rendered card is a local file: it must be uploaded, never sent as a path string."""
    bot = Sender()
    monkeypatch.setattr(ctx, "bot", bot)
    png = tmp_path / "card.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    result = await card(_message(bot), ctx, html="caption", photo=str(png))
    payload = bot.payload("send_photo")
    assert isinstance(payload["photo"], FSInputFile), "Telegram cannot read our filesystem"
    assert payload["caption"] == "caption"
    assert result is not None and result.mode is not ChatMode.RICH


async def test_buttons_travel_with_the_card(ctx, monkeypatch) -> None:
    from aiogram.types import InlineKeyboardButton

    bot = Sender()
    monkeypatch.setattr(ctx, "bot", bot)
    buttons = [[InlineKeyboardButton(text="go", callback_data="a:b")]]
    await card(_message(bot), ctx, html="hi", buttons=buttons)
    markup = bot.payload("send_message")["reply_markup"]
    assert markup.inline_keyboard[0][0].callback_data == "a:b"


async def test_note_answers_a_callback_and_replies_to_a_message(ctx, monkeypatch) -> None:
    """The spinner rule: a button press always gets an answer, even when the reply goes elsewhere."""
    bot = Sender()
    monkeypatch.setattr(ctx, "bot", bot)
    await note(_callback(bot), "toast")
    assert bot.payload("answer_callback_query")["text"] == "toast"
    await note(_message(bot), "replied")
    assert bot.payload("send_message")["text"] == "replied"


async def test_note_truncates_a_toast_to_telegrams_limit(ctx, monkeypatch) -> None:
    bot = Sender()
    monkeypatch.setattr(ctx, "bot", bot)
    await note(_callback(bot), "x" * 900)
    assert len(bot.payload("answer_callback_query")["text"]) == 200


async def test_a_url_answer_opens_the_button(ctx, monkeypatch) -> None:
    bot = Sender()
    monkeypatch.setattr(ctx, "bot", bot)
    await note(_callback(bot), "", url="https://example.com/x")
    assert bot.payload("answer_callback_query")["url"] == "https://example.com/x"


async def test_a_callback_with_no_visible_message_still_answers(ctx, monkeypatch) -> None:
    bot = Sender()
    monkeypatch.setattr(ctx, "bot", bot)
    await card(_callback(bot), ctx, html="nothing to edit here")
    assert bot.names() == ["answer_callback_query"], "a card that cannot be sent must not be silent"


def test_chat_type_predicates_read_the_field_aiogram_still_has() -> None:
    from waifu.utils.chats import is_group, is_private

    for chat_type, private in [
        ("private", True),
        ("group", False),
        ("supergroup", False),
        ("channel", False),
    ]:
        chat = Chat.model_validate({"id": 1, "type": chat_type})
        assert is_private(chat) is private, chat_type
        assert is_group(chat) is (not private), chat_type
    assert is_private(None) is True, "a detached callback is never a group"
