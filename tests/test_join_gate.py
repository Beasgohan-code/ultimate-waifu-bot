"""The join-request gate: ``/gate``, ``/gatelink`` and the ``chat_join_request`` handler.

This is the one feature here with no counterpart in Summon-bot — that deployment predates
``createChatInviteLink(creates_join_request=True)``, so there was nothing to port and only a gap to
close: :mod:`waifu.core.app` has subscribed ``chat_join_request`` from the start and nothing
handled it, which meant a group that turned on approval requests simply never answered.

The contract these tests pin:

* the switch is **per group**, like every other moderation rule in this bot;
* a joiner is approved only after naming the right character, with three attempts;
* fewer than four characters in the roster means **auto-approve** — a security feature must not
  become a way to lock a group — and neither must a failure to ask;
* a blocked DM declines with a reason instead of leaving the request pending forever;
* state lives in :class:`~waifu.db.cache.Cache`, so nothing is written to the database for a
  request that goes cold and no migration was needed to add the feature.

Events are real aiogram models bound to a recording bot via ``context={"bot": ...}``. That path
makes ``await event.approve()`` verifiable without a network *and* fails loudly if a field or
method name is invented (``ChatJoinRequest.from_user``, not ``user``).
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

import pytest
from aiogram.exceptions import TelegramForbiddenError
from aiogram.filters import CommandObject
from aiogram.methods import CreateChatInviteLink
from aiogram.types import CallbackQuery, ChatJoinRequest, Message
from sqlalchemy import delete, select

from waifu.core.access import Access
from waifu.db.models import Character
from waifu.enums import Role
from waifu.plugins import moderation

GATE_CHAT = -100
RECORDED = {"calls", "send_error", "link", "names", "payload", "count"}


def _snake(name: str) -> str:
    """``ApproveChatJoinRequest`` → ``approve_chat_join_request``: one vocabulary, both paths."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


class Recorder:
    """A bot that is nothing but a ledger.

    Two call shapes have to be recorded: this bot's own ``await bot.send_message(...)`` and
    aiogram's event helpers (``message.answer()``, ``request.approve()``), which build a typed
    method and ``await bot(method)``. Both land in one list under the API method name, so the
    assertions below double as a check that the handler uses method names Telegram actually has.
    """

    def __init__(
        self,
        *,
        send_error: Exception | None = None,
        link: str = "https://t.me/+abc123",
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.send_error = send_error
        self.link = link

    def _record(self, kind: str, **kwargs: Any) -> Any:
        self.calls.append((kind, kwargs))
        if kind in {"send_message", "send_photo"} and self.send_error is not None:
            raise self.send_error
        if kind == "create_chat_invite_link":
            return type("Link", (), {"invite_link": self.link})()
        return None

    async def __call__(self, method: Any) -> Any:
        return self._record(
            _snake(type(method).__name__),
            **method.model_dump(exclude_none=True, exclude={"bot"}),
        )

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_") or name in RECORDED:
            raise AttributeError(name)

        async def _send(*args: Any, **kwargs: Any) -> Any:  # `name` may legitimately be a kwarg
            # ``send_message(chat_id, text, ...)`` is called positionally by the fallback sender,
            # and a recorder that drops positionals loses the very text an assertion needs.
            for key, value in zip(("chat_id", "text", "parse_mode"), args, strict=False):
                kwargs.setdefault(key, value)
            return self._record(name, **kwargs)

        return _send

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def payload(self, name: str) -> dict[str, Any]:
        for call_name, kwargs in reversed(self.calls):
            if call_name == name:
                return kwargs
        raise AssertionError(f"{name} was never called (calls: {self.names()})")

    def count(self, name: str) -> int:
        return self.names().count(name)


def _now() -> int:
    return int(dt.datetime.now(dt.UTC).timestamp())


def _user(bot: Recorder, *, user_id: int, username: str = "someone") -> dict:
    return {"id": user_id, "is_bot": False, "first_name": username.title(), "username": username}


def _chat(bot: Recorder, *, chat_id: int = GATE_CHAT, private: bool = False) -> dict:
    """A plain dict, on purpose: aiogram must choose the model (``Chat``) and its ``type`` field
    is what :func:`waifu.utils.chats.is_private` reads. Handing back a pre-built ``Chat`` would
    let the fixture disagree with what a real update looks like."""
    return {
        "id": chat_id,
        "type": "private" if private else "supergroup",
        "title": "Waifu HQ",
        "is_channel": False,
    }


def _request(bot: Recorder, *, chat_id: int = GATE_CHAT, user_id: int = 7) -> ChatJoinRequest:
    return ChatJoinRequest.model_validate(
        {
            "chat": _chat(bot, chat_id=chat_id),
            "from_user": _user(bot, user_id=user_id),
            "date": _now(),
            "user_chat_id": user_id,
        },
        context={"bot": bot},
    )


def _command(
    bot: Recorder, text: str, *, chat_id: int = GATE_CHAT, private: bool = False
) -> Message:
    return Message.model_validate(
        {
            "message_id": 1,
            "date": _now(),
            "chat": _chat(bot, chat_id=chat_id, private=private),
            "from_user": _user(bot, user_id=5, username="admin"),
            "text": text,
        },
        context={"bot": bot},
    )


def _args(text: str) -> CommandObject:
    name, _, rest = text[1:].partition(" ")
    return CommandObject(command=name, args=rest or None)


def _callback(bot: Recorder, data: str, *, user_id: int = 7) -> CallbackQuery:
    return CallbackQuery.model_validate(
        {
            "id": f"{user_id}:{data}",
            "from_user": _user(bot, user_id=user_id),
            "chat_instance": "instance",
            "data": data,
        },
        context={"bot": bot},
    )


def _admin(bot: Recorder) -> Access:
    return Access(
        user_id=5,
        role=Role.MODERATOR,
        is_group_admin=True,
        user=_user(bot, user_id=5, username="admin"),
    )


@pytest.fixture
def bot(ctx, monkeypatch) -> Recorder:
    """The recorder, attached: every handler here ends in a send, so there is no offline path."""
    recorder = Recorder()
    monkeypatch.setattr(ctx, "bot", recorder)
    return recorder


async def _arm(ctx, *, chat_id: int = GATE_CHAT, enabled: bool = True) -> None:
    async with ctx.db.tx() as session:
        await ctx.spawn.set_switch(session, chat_id, "gate", value=enabled)


# --------------------------------------------------------------------- the switch


async def test_gate_command_turns_the_switch_on_and_off(bot, ctx) -> None:
    async with ctx.db.tx() as session:
        await moderation.gate(
            _command(bot, "/gate on"), ctx, session, _args("/gate on"), _admin(bot)
        )
        assert await ctx.spawn.switch(session, GATE_CHAT, "gate") is True
    async with ctx.db.session() as session:
        assert await ctx.spawn.switch(session, -200, "gate") is False, (
            "each group keeps its own rule"
        )
    async with ctx.db.tx() as session:
        await moderation.gate(
            _command(bot, "/gate off"), ctx, session, _args("/gate off"), _admin(bot)
        )
        assert await ctx.spawn.switch(session, GATE_CHAT, "gate") is False
    assert bot.count("send_message") == 2, "one receipt per command, posted to the group that asked"
    assert "join gate" in bot.payload("send_message")["text"]


async def test_bare_gate_toggles_from_the_current_state(bot, ctx) -> None:
    for expected in (True, False):
        async with ctx.db.tx() as session:
            await moderation.gate(_command(bot, "/gate"), ctx, session, _args("/gate"), _admin(bot))
            assert await ctx.spawn.switch(session, GATE_CHAT, "gate") is expected


async def test_gate_needs_group_admin_rights(bot, ctx) -> None:
    from waifu.errors import PermissionDenied

    plain = Access(user_id=9, role=Role.USER, is_group_admin=False, user=_user(bot, user_id=9))
    async with ctx.db.tx() as session:
        with pytest.raises(PermissionDenied):
            await moderation.gate(_command(bot, "/gate on"), ctx, session, _args("/gate on"), plain)
    assert bot.calls == []
    async with ctx.db.session() as session:
        assert await ctx.spawn.switch(session, GATE_CHAT, "gate") is False


async def test_gate_in_a_private_chat_is_refused(bot, ctx) -> None:
    """Warnings are per-group and so is this: a DM has no group to configure."""
    async with ctx.db.tx() as session:
        await moderation.gate(
            _command(bot, "/gate on", private=True), ctx, session, _args("/gate on"), _admin(bot)
        )
    text = bot.payload("send_message")["text"]
    assert text.startswith("The gate guards a group")
    assert "join gate" not in text


# --------------------------------------------------------------------- the link


async def test_gatelink_forces_join_requests(bot, ctx) -> None:
    """The flag that generates the update is set by the bot: a link without it walks straight past
    the gate, and an admin who forgets it never finds out."""
    assert "creates_join_request" in CreateChatInviteLink.model_fields
    async with ctx.db.tx() as session:
        await moderation.gate_link(_command(bot, "/gatelink"), ctx, session, _admin(bot))
    payload = bot.payload("create_chat_invite_link")
    assert payload["creates_join_request"] is True
    assert payload["member_limit"] == 50
    assert payload["chat_id"] == GATE_CHAT
    assert "+abc123" in bot.payload("send_message")["text"]


# --------------------------------------------------------------------- the quiz


async def test_join_request_update_is_subscribed() -> None:
    """The wiring claim, checked not assumed — an update nobody handles is a silent gap."""
    from waifu.core import app as app_module

    assert "chat_join_request" in app_module._ALL_UPDATES


async def test_empty_roster_approves_instead_of_locking_the_group(bot, ctx) -> None:
    """A fresh install has no characters, and an empty roster must never mean an empty group."""
    await _arm(ctx)
    async with ctx.db.tx() as session:
        await session.execute(delete(Character))
    await moderation.join_gate(_request(bot), ctx)
    assert bot.names() == ["approve_chat_join_request"]


async def test_switch_off_leaves_the_request_alone(bot, ctx) -> None:
    await moderation.join_gate(_request(bot), ctx)
    assert bot.calls == [], "a group that did not ask for a gate must not be answered by us"


async def test_quiz_arms_state_and_asks_in_a_dm(bot, ctx) -> None:
    await _arm(ctx)
    await moderation.join_gate(_request(bot), ctx)
    ask = bot.payload("send_message")
    assert ask["chat_id"] == 7, "the question is asked privately, so the group sees no spam"
    assert "One question before you join" in ask["text"]
    rows = ask["reply_markup"].inline_keyboard
    assert len(rows) == 4 and all(len(row) == 1 for row in rows)
    assert [row[0].callback_data for row in rows] == [f"gate:{GATE_CHAT}:7:{i}" for i in range(4)]
    state = await ctx.cache.get(moderation.GATE_NS, GATE_CHAT, 7)
    assert state["tries"] == moderation.GATE_TRIES
    assert len(state["options"]) == 4
    assert 0 <= int(state["answer"]) < 4


async def test_question_pool_is_reused_within_the_ttl(bot, ctx) -> None:
    """Four ``ORDER BY random()`` scans per join request is a self-inflicted incident."""
    await _arm(ctx)
    await moderation.join_gate(_request(bot, user_id=7), ctx)
    await moderation.join_gate(_request(bot, user_id=8), ctx)
    first = await ctx.cache.get(moderation.GATE_NS, GATE_CHAT, 7)
    second = await ctx.cache.get(moderation.GATE_NS, GATE_CHAT, 8)
    assert first["options"] == second["options"]
    assert first["answer"] == second["answer"]


async def test_correct_answer_admits_and_clears_state(bot, ctx) -> None:
    await _arm(ctx)
    await moderation.join_gate(_request(bot), ctx)
    state = await ctx.cache.get(moderation.GATE_NS, GATE_CHAT, 7)
    await moderation.gate_answer(_callback(bot, f"gate:{GATE_CHAT}:7:{state['answer']}"), ctx)
    assert bot.payload("approve_chat_join_request") == {"chat_id": GATE_CHAT, "user_id": 7}
    assert await ctx.cache.get(moderation.GATE_NS, GATE_CHAT, 7) is None
    assert "You're in" in bot.payload("send_message")["text"]


async def test_wrong_answers_are_counted_and_the_third_declines(bot, ctx) -> None:
    await _arm(ctx)
    await moderation.join_gate(_request(bot), ctx)
    state = await ctx.cache.get(moderation.GATE_NS, GATE_CHAT, 7)
    wrong = next(i for i in range(4) if i != int(state["answer"]))
    for expected in (moderation.GATE_TRIES - 1, moderation.GATE_TRIES - 2):
        await moderation.gate_answer(_callback(bot, f"gate:{GATE_CHAT}:7:{wrong}"), ctx)
        state = await ctx.cache.get(moderation.GATE_NS, GATE_CHAT, 7)
        assert state["tries"] == expected
    await moderation.gate_answer(_callback(bot, f"gate:{GATE_CHAT}:7:{wrong}"), ctx)
    assert bot.count("approve_chat_join_request") == 0
    assert bot.payload("decline_chat_join_request")["user_id"] == 7
    assert await ctx.cache.get(moderation.GATE_NS, GATE_CHAT, 7) is None


async def test_only_the_person_asked_may_answer(bot, ctx) -> None:
    """Otherwise anyone forwarded the button could wave a stranger in — or lock a fan out."""
    await _arm(ctx)
    await moderation.join_gate(_request(bot, user_id=7), ctx)
    await moderation.gate_answer(_callback(bot, f"gate:{GATE_CHAT}:7:0", user_id=8), ctx)
    assert bot.count("approve_chat_join_request") == 0
    assert "not addressed to you" in bot.payload("answer_callback_query")["text"]


async def test_the_answer_is_not_always_the_first_button(bot, ctx) -> None:
    """A quiz with a fixed position is a formality — the index is drawn per pool, not per joiner."""
    await _arm(ctx)
    seen: set[int] = set()
    async with ctx.db.session() as session:
        for probe in range(40):
            question = await moderation._gate_question(ctx, session, chat_id=-(1000 + probe))
            assert question is not None
            seen.add(int(question["answer"]))
    assert len(seen) > 1, f"the correct option always sat at {sorted(seen)}"


async def test_expired_question_says_so(bot, ctx) -> None:
    await moderation.gate_answer(_callback(bot, f"gate:{GATE_CHAT}:7:0"), ctx)
    assert bot.names() == ["answer_callback_query"]
    assert "expired" in bot.payload("answer_callback_query")["text"]


async def test_malformed_callback_data_is_ignored(bot, ctx) -> None:
    await moderation.gate_answer(_callback(bot, "gate:-100"), ctx)
    await moderation.gate_answer(_callback(bot, "gate:oops:7:0"), ctx)
    assert bot.count("approve_chat_join_request") == 0
    assert bot.count("decline_chat_join_request") == 0


async def test_blocked_dm_declines_with_a_reason(bot, ctx, monkeypatch) -> None:
    """A joiner who cannot be asked must not sit in limbo; the admin gets an answer either way."""
    blocked = Recorder(send_error=TelegramForbiddenError(message="blocked", method="SendMessage"))
    monkeypatch.setattr(ctx, "bot", blocked)
    await _arm(ctx)
    await moderation.join_gate(_request(blocked), ctx)
    assert blocked.names() == ["send_message", "decline_chat_join_request"]
    assert "could not message you" in blocked.payload("decline_chat_join_request")["reason"]
    assert await ctx.cache.get(moderation.GATE_NS, GATE_CHAT, 7) is None


async def test_a_broken_prompt_falls_open(bot, ctx, monkeypatch) -> None:
    """Our bug is not the joiner's fault: let them in, and log it for the owner."""
    await _arm(ctx)

    async def _error(*args: Any, **kwargs: Any) -> str:
        return "error"

    monkeypatch.setattr(moderation, "_send_prompt", _error)
    await moderation.join_gate(_request(bot), ctx)
    assert bot.names() == ["approve_chat_join_request"]


async def test_a_gateless_group_is_untouched_while_another_is_quizzed(bot, ctx) -> None:
    """Two groups, one rule each — the per-group switch is the point of the whole design."""
    await _arm(ctx, chat_id=GATE_CHAT)
    await moderation.join_gate(_request(bot, chat_id=-300), ctx)
    assert bot.calls == []
    await moderation.join_gate(_request(bot, chat_id=GATE_CHAT), ctx)
    assert bot.count("send_message") == 1


async def test_three_characters_is_not_a_quiz(bot, ctx) -> None:
    """With four options a guesser is 25% likely to get in; the gate would rather not ask."""
    await _arm(ctx)
    async with ctx.db.tx() as session:
        keep = list((await session.execute(select(Character.id).limit(3))).scalars().all())
        await session.execute(delete(Character).where(Character.id.not_in(keep)))
    async with ctx.db.session() as session:
        assert await moderation._gate_question(ctx, session, chat_id=GATE_CHAT) is None
    await moderation.join_gate(_request(bot), ctx)
    assert bot.names() == ["approve_chat_join_request"], "nothing to ask → open the door"


async def test_stored_art_makes_it_an_image_question(bot, ctx) -> None:
    """``photo_file_id`` art turns it into "whose art is this?" — and a URL is never fetched here."""
    await _arm(ctx)
    async with ctx.db.tx() as session:
        rows = list((await session.execute(select(Character))).scalars())
        for row in rows:
            row.photo_file_id = ""
            row.image_url = "https://catbox.example.org/nope.png"
    async with ctx.db.session() as session:
        question = await moderation._gate_question(ctx, session, chat_id=GATE_CHAT)
    assert question is not None and question["media"] == ""
    await moderation.join_gate(_request(bot), ctx)
    assert bot.count("send_message") == 1, "text question, no media fetch at join time"
    assert bot.count("send_photo") == 0
