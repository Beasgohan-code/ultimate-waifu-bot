"""The owner's log channel and the recipient gift DMs.

This pins down the two things a bot owner checks first after a deploy:

* **the log channel** — every start/stop, gift, payment, trade, raffle and
  membership event must land in ``LOG_CHANNEL_ID`` as a readable, escaped line.
  The old ``AppContext.notify`` delegated to a *DM* method (``user_id`` first),
  so every one of those sends raised ``TypeError`` and the channel stayed
  silent; a gift was the only money event with no receipt on either side.
* **the gift receipt** — when a character is gifted, the *receiver* gets a
  private message naming the character, its rarity and its art (file_id or
  URL, falling back to text), and the sender is hidden for ``/anon`` gifts.

Also covered: the two Bot API 10.x payment updates that used to arrive and
vanish (``purchased_paid_media``, ``subscription``), the silent-renewal pass,
the keep-alive health server, and an end-to-end update through the real
dispatcher (middlewares included) — the gap that let ``session``/``access``
stop being injected without a single test noticing.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from aiogram.types import (
    BotSubscriptionUpdated,
    Message,
    Update,
)
from sqlalchemy import select

from waifu.core.health import build_app as build_health_app
from waifu.core.health import resolve_port
from waifu.db.models import Character, SubscriptionAccess
from waifu.db.repo import characters as char_repo
from waifu.db.repo import collection as collection_repo
from waifu.db.repo import economy as ledger
from waifu.db.repo import monetize as monetize_repo
from waifu.enums import Rarity
from waifu.utils.time import now_utc

LOG_CHAT = 777001


async def _character_in(tx, any_character: Character) -> Character:
    """The fixture's session is closed; mutate the row through *this* one."""
    character = await char_repo.get(tx, any_character.id)
    assert character is not None
    return character


class Recorder:
    """Records API calls through both shapes aiogram uses (see test_send_paths)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail_with: Exception | None = None
        self.fail_remaining: int | None = None  # None = every call, 1 = the first only
        self.fail_methods: set[str] = set()  # restrict failures to these kinds

    def _record(self, kind: str, **kwargs: Any) -> None:
        self.calls.append((kind, kwargs))

    def _maybe_fail(self, kind: str = "") -> None:
        if self.fail_with is None:
            return
        if self.fail_methods and kind not in self.fail_methods:
            return
        if self.fail_remaining is not None:
            if self.fail_remaining == 0:
                return
            self.fail_remaining -= 1
        raise self.fail_with

    async def __call__(self, method: Any) -> Any:
        self._record(type(method).__name__, **method.model_dump(exclude_none=True, exclude={"bot"}))
        self._maybe_fail(type(method).__name__)  # after the record: failed attempts are visible
        return None

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_") or name in {"calls"}:
            raise AttributeError(name)

        async def _send(*args: Any, **kwargs: Any) -> Any:
            for key, value in zip(("chat_id", "text", "parse_mode"), args, strict=False):
                kwargs.setdefault(key, value)
            self._record(name, **kwargs)
            self._maybe_fail(name)  # after the record: failed attempts are visible
            return None

        return _send

    def by(self, name: str) -> list[dict[str, Any]]:
        return [kw for kind, kw in self.calls if kind == name]

    def to(self, name: str, chat_id: int) -> list[dict[str, Any]]:
        return [kw for kw in self.by(name) if kw.get("chat_id") == chat_id]


@pytest.fixture
def bot(ctx) -> Recorder:
    """Attach a recording bot to the shared context."""
    recorder = Recorder()
    ctx.bot = recorder
    return recorder


@pytest.fixture
def log_on(ctx) -> None:
    """Point the owner's log channel at a known chat."""
    ctx.settings.log_channel_id = LOG_CHAT


# ------------------------------------------------------------------ ctx.notify
async def test_notify_without_channel_is_a_noop(ctx) -> None:
    """No channel configured (or no bot) → False, and crucially no exception."""
    ctx.settings.log_channel_id = 0
    assert await ctx.notify("anything at all") is False
    assert await ctx.notify("anything at all", html=True) is False


async def test_notify_sends_escaped_line(bot: Recorder, ctx, log_on) -> None:
    """Plain text is HTML-escaped: a group name cannot markup the owner feed."""
    ok = await ctx.notify("➕ added to <b>evil</b> group — id 1")
    assert ok is True
    (sent,) = bot.to("send_message", LOG_CHAT)
    assert "<b>evil</b>" not in sent["text"]
    assert "&lt;b&gt;evil&lt;/b&gt;" in sent["text"]


async def test_notify_html_mode_passes_through(bot: Recorder, ctx, log_on) -> None:
    ok = await ctx.notify("<b>real</b> markup", html=True)
    assert ok is True
    (sent,) = bot.to("send_message", LOG_CHAT)
    assert sent["text"] == "<b>real</b> markup"


# ---------------------------------------------------------------------- gifts
async def test_character_gift_dm_and_owner_log(
    bot: Recorder, ctx, tx, player, partner, any_character, log_on
) -> None:
    """The receiver DMs the character (name + rarity + art), the owner gets a line."""
    character = await _character_in(tx, any_character)
    character.photo_file_id = "AgACAgUAAxyzFileId001"
    character.image_url = ""
    await tx.flush()
    await collection_repo.grant(tx, player, character.id, source="fixture")

    result = await ctx.gifts.character(
        tx, player, partner, character.id, note="for you", anonymous=False
    )
    assert result["receiver"] == partner

    # 1. The receiver's DM: a photo with the character's file_id and a caption
    #    carrying the name, the rarity and a tappable sender.
    (photo,) = bot.to("send_photo", partner)
    assert photo["photo"] == "AgACAgUAAxyzFileId001"
    caption = photo["caption"]
    assert character.name in caption
    assert Rarity.from_value(int(character.rarity_id)).label in caption
    assert f"tg://user?id={player}" in caption
    assert "for you" in caption

    # 2. The owner's log channel: one line, sender and receiver named.
    (line,) = bot.to("send_message", LOG_CHAT)
    assert "character gift" in line["text"]
    assert str(player) in line["text"] and str(partner) in line["text"]
    assert character.name in line["text"]

    # 3. The transfer itself still happened exactly once.
    assert await collection_repo.has_count(tx, partner, character.id) == 1
    assert await collection_repo.has_count(tx, player, character.id) == 0


async def test_character_gift_anonymous_hides_sender(
    bot: Recorder, ctx, tx, player, partner, any_character, log_on
) -> None:
    character = await _character_in(tx, any_character)
    character.photo_file_id = "AgACAgUAAxyzFileId002"
    character.image_url = ""
    await tx.flush()
    await collection_repo.grant(tx, player, character.id, source="fixture")

    await ctx.gifts.character(tx, player, partner, character.id, note="", anonymous=True)

    (photo,) = bot.to("send_photo", partner)
    caption = photo["caption"]
    assert "anonymous" in caption.lower()
    assert f"tg://user?id={player}" not in caption

    (line,) = bot.to("send_message", LOG_CHAT)
    assert "anonymous" in line["text"]
    assert f"tg://user?id={player}" not in line["text"]


async def test_character_gift_without_art_falls_back_to_text(
    bot: Recorder, ctx, tx, player, partner, any_character, log_on
) -> None:
    """No file_id, no URL: the receipt is still delivered, as text."""
    character = await _character_in(tx, any_character)
    character.photo_file_id = ""
    character.image_url = ""
    await tx.flush()
    await collection_repo.grant(tx, player, character.id, source="fixture")

    await ctx.gifts.character(tx, player, partner, character.id)

    assert bot.to("send_photo", partner) == []
    (dm,) = bot.to("send_message", partner)
    assert character.name in dm["text"]


async def test_coin_gift_dm_and_owner_log(bot: Recorder, ctx, tx, player, partner, log_on) -> None:
    before = await ledger.balance(tx, partner)
    await ctx.gifts.coins(tx, player, partner, 500)

    (dm,) = bot.to("send_message", partner)
    assert "500" in dm["text"]
    assert await ledger.balance(tx, partner) == before + 500

    (line,) = bot.to("send_message", LOG_CHAT)
    assert "coin gift" in line["text"]
    assert str(player) in line["text"] and str(partner) in line["text"]


# ------------------------------------------------------------------- payments
async def test_stars_settle_logs_the_payment(bot: Recorder, ctx, tx, partner, log_on) -> None:
    await monetize_repo.create_order(
        tx,
        user_id=partner,
        invoice_payload="coins:9999:starter:abcd1234",
        product="coins",
        product_ref="starter",
        star_count=50,
        coins_granted=5000,
    )
    before = await ledger.balance(tx, partner)
    result = await ctx.premium.settle(
        tx, "coins:9999:starter:abcd1234", charge_id="XTR123", star_count=50
    )
    assert result["ok"] is True and result["granted"]["coins"] == 5000
    assert await ledger.balance(tx, partner) == before + 5000

    (line,) = bot.to("send_message", LOG_CHAT)
    assert "payment" in line["text"]
    assert "50 ⭐" in line["text"]
    assert "5,000 🪙" in line["text"]
    assert "XTR123" in line["text"]


async def test_paid_media_purchase_delivers_and_logs(
    bot: Recorder, ctx, tx, partner, any_character, log_on
) -> None:
    """The purchased_paid_media update: the buyer is granted *and* receipted."""
    payload = "pm:1234:deadbeef"
    await monetize_repo.create_order(
        tx,
        user_id=partner,
        invoice_payload=payload,
        product="paid_media",
        product_ref=str(any_character.id),
        star_count=45,
        character_id=any_character.id,
        source="paid_media",
    )
    character = await _character_in(tx, any_character)
    character.photo_file_id = "AgACAgUAAxyzFileId003"
    await tx.flush()

    update = Update.model_validate(
        {
            "update_id": 9001,
            "purchased_paid_media": {
                "from_user": {"id": partner, "is_bot": False, "first_name": "P"},
                "paid_media_payload": payload,
            },
        }
    )
    result = await ctx.premium.settle_purchase(tx, update)
    assert result["ok"] is True

    assert await collection_repo.has_count(tx, partner, any_character.id) == 1
    (photo,) = bot.to("send_photo", partner)
    assert "Unlocked" in photo["caption"]
    assert any_character.name in photo["caption"]
    (line,) = bot.to("send_message", LOG_CHAT)
    assert "paid media" in line["text"]
    assert str(partner) in line["text"]


async def test_subscription_active_canceled_duplicate(bot: Recorder, ctx, tx, log_on) -> None:
    """The Bot API 10.1 subscription object: active → perks, canceled → none,
    a duplicated active update must not grant twice."""
    payload = "sub:4242:abcd12"
    active = BotSubscriptionUpdated.model_validate(
        {
            "user": {"id": 4242, "is_bot": False, "first_name": "T"},
            "invoice_payload": payload,
            "state": "active",
        }
    )

    first = await ctx.premium.sync_subscription(tx, active)
    assert first["ok"] and first["state"] == "active"
    assert await monetize_repo.has_subscription(tx, 4242) is True
    hours_one = await ledger.premium_left_hours(tx, 4242)
    assert hours_one > 0

    # Duplicated at-least-once delivery: a no-op that neither stacks a period
    # nor flickers the user's premium to zero.
    second = await ctx.premium.sync_subscription(tx, active)
    assert second["state"] == "active" and second["already_active"] is True
    assert await ledger.premium_left_hours(tx, 4242) == hours_one

    canceled = BotSubscriptionUpdated.model_validate(
        {
            "user": {"id": 4242, "is_bot": False, "first_name": "T"},
            "invoice_payload": payload,
            "state": "canceled",
        }
    )
    third = await ctx.premium.sync_subscription(tx, canceled)
    assert third["state"] == "cancelled"
    assert await monetize_repo.has_subscription(tx, 4242) is False
    # The already-paid period survives a cancel (standard subscription semantics)
    # — what must stop is the *renewal*: age the period out and the nightly pass
    # must not find anything to extend.
    assert await ledger.premium_left_hours(tx, 4242) > 0
    row = (
        await tx.execute(select(SubscriptionAccess).where(SubscriptionAccess.user_id == 4242))
    ).scalar_one()
    row.current_period_end = now_utc() - timedelta(days=1)
    await tx.flush()
    assert await ctx.premium.renew_due_subscriptions(tx) == 0

    # The owner saw the subscription lifecycle in the log channel.
    lines = [kw["text"] for kw in bot.to("send_message", LOG_CHAT)]
    assert any("started a premium subscription" in t for t in lines)
    assert any("cancelled" in t for t in lines)


async def test_silent_renewal_extends_premium_and_logs(bot: Recorder, ctx, tx, log_on) -> None:
    """Telegram renews without an update — the nightly pass pays the period out."""
    payload = "sub:4242:renew99"
    await ctx.premium.sync_subscription(
        tx,
        BotSubscriptionUpdated.model_validate(
            {
                "user": {"id": 4242, "is_bot": False, "first_name": "T"},
                "invoice_payload": payload,
                "state": "active",
            }
        ),
    )
    # Age the period until it is due.
    row = (
        await tx.execute(select(SubscriptionAccess).where(SubscriptionAccess.user_id == 4242))
    ).scalar_one()
    row.current_period_end = now_utc() - timedelta(days=1)
    await tx.flush()

    renewed = await ctx.premium.renew_due_subscriptions(tx)
    assert renewed == 1
    row = (
        await tx.execute(select(SubscriptionAccess).where(SubscriptionAccess.user_id == 4242))
    ).scalar_one()
    assert row.current_period_end > now_utc()
    assert await ledger.premium_left_hours(tx, 4242) > 0
    (line,) = [kw for kw in bot.to("send_message", LOG_CHAT) if "renewed premium" in kw["text"]]
    assert "4242" in line["text"]


# ----------------------------------------------------------------- /premium UI
async def test_premium_menu_survives_the_perks_list(bot: Recorder, ctx, tx, player) -> None:
    """Regression: /premium used to call ``.items()`` on the perks *list*."""
    import waifu.plugins.premium as premium_plugin

    message = Message.model_validate(
        {
            "message_id": 7,
            "date": 1,
            "chat": {"id": player, "type": "private"},
            "from_user": {"id": player, "is_bot": False, "first_name": "P"},
            "text": "/premium",
        },
    )
    await premium_plugin.send_menu(message, ctx, tx, user_id=player)
    assert bot.to("send_message", player) or bot.to("send_photo", player)


# ------------------------------------------------------------- keep-alive http
async def test_health_endpoints(ctx) -> None:
    """/ and /health answer the uptime bot; /healthz checks the database."""
    from aiohttp.test_utils import TestClient, TestServer

    client = TestClient(TestServer(build_health_app(ctx)))
    await client.start_server()
    try:
        simple = await client.get("/health")
        assert simple.status == 200
        body = await simple.json()
        assert body["status"] == "healthy"
        assert body["version"] and body["api_version"]
        assert body["uptime_seconds"] >= 0

        deep = await client.get("/healthz")
        assert deep.status == 200
        deep_body = await deep.json()
        assert deep_body["db"]["db"] == "ok"
    finally:
        await client.close()


def test_health_port_falls_back_to_port_env(ctx, monkeypatch) -> None:
    ctx.settings.health_port = 0
    monkeypatch.setenv("PORT", "10000")
    assert resolve_port(ctx.settings) == 10000
    monkeypatch.delenv("PORT")
    assert resolve_port(ctx.settings) == 8080
    ctx.settings.health_port = 9999
    assert resolve_port(ctx.settings) == 9999


# ------------------------------------------------------- dispatcher end-to-end
async def test_dispatcher_feeds_a_real_update(ctx) -> None:
    """An update through the real middleware stack: ``ContextMiddleware`` must
    inject ``session`` and ``access`` into the handler. Before the fix
    ``install_middlewares`` was never called, so every command died on its
    first update with "missing argument" — a gap no service-level test saw.

    A local probe router is used on purpose: aiogram refuses to attach the
    module-level plugin routers to a second dispatcher (test_plugin_registry
    owns those), and the middleware layer is what this test is about anyway.
    """
    from aiogram import Dispatcher, Router
    from aiogram.filters import Command
    from aiogram.fsm.storage.memory import MemoryStorage

    from waifu.core.middlewares import install_middlewares

    recorder = Recorder()
    ctx.bot = recorder

    probe = Router(name="probe")
    seen: dict[str, Any] = {}

    @probe.message(Command("start"))
    async def start_probe(message: Message, ctx, session, access) -> None:
        seen["session"] = session
        seen["access"] = access
        await ctx.bot.send_message(message.chat.id, "start ok")

    dp = Dispatcher(storage=MemoryStorage())
    install_middlewares(dp, ctx, ctx.settings)
    dp.include_router(probe)
    dp.workflow_data.update(
        ctx=ctx,
        settings=ctx.settings,
        bot=recorder,
        db=ctx.db,
        redis=None,
        cache=ctx.cache,
        caps=ctx.caps,
    )
    update = Update.model_validate(
        {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": 1,
                "chat": {"id": 4242, "type": "private"},
                "from_user": {"id": 4242, "is_bot": False, "first_name": "P"},
                "text": "/start",
            },
        }
    )
    await dp.feed_update(bot=recorder, update=update)

    # The middleware injected both, and the handler answered in the same DM.
    assert seen.get("session") is not None
    assert seen.get("access") is not None and seen["access"].user_id == 4242
    assert recorder.to("send_message", 4242)


# ------------------------------------------- the log channel reports on itself
def _message(user_id: int, text: str) -> Message:
    return Message.model_validate(
        {
            "message_id": 1,
            "date": 1,
            "chat": {"id": user_id, "type": "private"},
            "from_user": {"id": user_id, "is_bot": False, "first_name": "P"},
            "text": text,
        }
    )


def _caps(*, reactions: bool = False, rich: bool = False) -> Any:
    """The conftest stub says no to everything; this grants only what is asked."""
    return type(
        "Caps",
        (),
        {
            "allow": staticmethod(lambda name: name == "reactions" and reactions),
            "rich_messages": rich,
        },
    )()


async def test_notify_counts_sends(bot: Recorder, ctx, log_on) -> None:
    assert await ctx.notify("one") is True
    assert await ctx.notify("two", html=True) is True
    assert (ctx.log_stats.sent, ctx.log_stats.failed) == (2, 0)


async def test_notify_failure_is_counted_and_explained(bot: Recorder, ctx, log_on) -> None:
    """A dead channel (the bot is not its admin) must be *visible*, not silent.

    Before LogChannelStats, a wrong LOG_CHANNEL_ID swallowed every event in the
    bot's life and the owner found out weeks later, if at all.
    """
    from aiogram.exceptions import TelegramForbiddenError

    bot.fail_with = TelegramForbiddenError(
        message="Forbidden: bot is not a member of the chat", method="SendMessage"
    )
    assert await ctx.notify("into the void") is False
    stats = ctx.log_stats
    assert (stats.sent, stats.failed) == (0, 1)
    assert "Forbidden" in stats.last_error
    assert stats.last_error_at > 0


async def test_service_log_line_shares_the_counter(bot: Recorder, ctx, log_on) -> None:
    """Service.log_line goes through ctx.notify — one counter, two entry points."""
    await ctx.economy.log_line("➕ service line")
    assert ctx.log_stats.sent == 1
    (sent,) = bot.to("send_message", LOG_CHAT)
    assert "service line" in sent["text"]


# ------------------------------------------------------------ /logtest + /doctor
async def test_logtest_reports_a_live_channel(bot: Recorder, ctx, player, log_on) -> None:
    from waifu.core.access import Access
    from waifu.enums import Role
    from waifu.plugins.sudo import logtest

    await logtest(_message(player, "/logtest"), ctx, Access(user_id=player, role=Role.OWNER))
    (test,) = bot.to("send_message", LOG_CHAT)
    assert "log channel test" in test["text"]
    (reply,) = bot.to("send_message", player)
    assert "live" in reply["text"]


async def test_logtest_explains_a_dead_channel(bot: Recorder, ctx, player, log_on) -> None:
    from aiogram.exceptions import TelegramForbiddenError

    from waifu.core.access import Access
    from waifu.enums import Role
    from waifu.plugins.sudo import logtest

    bot.fail_with = TelegramForbiddenError(
        message="Forbidden: not enough rights", method="SendMessage"
    )
    bot.fail_remaining = 1  # the test line fails; the reply to the owner must still land
    await logtest(_message(player, "/logtest"), ctx, Access(user_id=player, role=Role.OWNER))
    (reply,) = bot.to("send_message", player)
    assert "not an admin" in reply["text"]
    assert "not enough rights" in reply["text"]


async def test_logtest_names_the_missing_setting(bot: Recorder, ctx, player) -> None:
    from waifu.core.access import Access
    from waifu.enums import Role
    from waifu.plugins.sudo import logtest

    await logtest(_message(player, "/logtest"), ctx, Access(user_id=player, role=Role.OWNER))
    assert not bot.to("send_message", LOG_CHAT)
    (reply,) = bot.to("send_message", player)
    assert "LOG_CHANNEL_ID" in reply["text"]


async def test_doctor_reports_the_log_channel(bot: Recorder, ctx, tx, player, log_on) -> None:
    from waifu.core.access import Access
    from waifu.enums import Role
    from waifu.plugins.sudo import doctor

    await ctx.notify("warm-up", silent=True)
    await doctor(_message(player, "/doctor"), ctx, tx, Access(user_id=player, role=Role.OWNER))
    (reply,) = bot.to("send_message", player)
    assert "log channel" in reply["text"]
    assert "1 sent" in reply["text"]


# -------------------------------------------------------- reactions on big moments
async def test_react_helper_records_and_never_raises(bot: Recorder) -> None:
    from aiogram.exceptions import TelegramAPIError

    from waifu.tg.interactions import react

    message = _message(4242, "hi")
    assert await react(bot, message, "heart") is True
    (call,) = bot.by("SetMessageReaction")
    assert call["chat_id"] == 4242
    assert call["reaction"][0]["emoji"] == "❤️"

    # An API refusal (old server, no rights) degrades to False, never an exception.
    bot.fail_with = TelegramAPIError(
        message="reactions not available here", method="SetMessageReaction"
    )
    assert await react(bot, message, "fire") is False


async def test_daily_claim_reacts_when_caps_allow(bot: Recorder, ctx, tx, player) -> None:
    from waifu.plugins.economy import do_daily

    ctx.caps = _caps(reactions=True)
    await do_daily(_message(player, "/daily"), ctx, tx, user_id=player, offset=0)
    calls = bot.by("SetMessageReaction")
    assert len(calls) == 1
    assert calls[0]["reaction"][0]["emoji"] == "🔥"


async def test_daily_claim_stays_silent_without_the_capability(
    bot: Recorder, ctx, tx, player
) -> None:
    """The conftest caps stub grants nothing — no reaction, no error."""
    from waifu.plugins.economy import do_daily

    await do_daily(_message(player, "/daily"), ctx, tx, user_id=player, offset=0)
    assert not bot.by("SetMessageReaction")


async def test_character_gift_reacts_to_the_command(
    bot: Recorder, ctx, tx, player, partner, any_character
) -> None:
    from waifu.plugins.gifts import _announce

    character = await _character_in(tx, any_character)
    character.image_url = "https://example.com/art.jpg"
    await tx.flush()
    ctx.caps = _caps(reactions=True)
    await _announce(
        _message(player, "/gift"),
        ctx,
        {"id": 1, "note": ""},
        sender=player,
        receiver=partner,
        anonymous=False,
        character=character,
    )
    calls = bot.by("SetMessageReaction")
    assert len(calls) == 1
    assert calls[0]["reaction"][0]["emoji"] == "❤️"


async def test_first_contact_reacts_and_logs(bot: Recorder, ctx, tx, log_on) -> None:
    """First contact: the owner's channel gets the line, the newcomer's own message
    gets a heart — the welcome, without a fifth bot message in the group."""
    from waifu.core.access import Access
    from waifu.core.middlewares import ContextMiddleware
    from waifu.enums import Role

    class _FreshRedis:
        """claim_once always says 'first hour' — the fresh-eyes path."""

        async def claim_once(self, key, ttl, value="1"):
            return True

    ctx.redis = _FreshRedis()
    ctx.caps = _caps(reactions=True)
    middleware = ContextMiddleware(ctx, ctx.settings)
    event = _message(918273645, "hello world")
    await middleware._ensure_player(tx, event, Access(user_id=918273645, role=Role.USER))

    (line,) = bot.to("send_message", LOG_CHAT)
    assert "new player" in line["text"]
    (call,) = bot.by("SetMessageReaction")
    assert call["chat_id"] == 918273645
    assert call["reaction"][0]["emoji"] == "❤️"


# ---------------------------------------------------------------- rich messages
async def test_safe_send_falls_back_to_plain_when_rich_is_unsupported(
    bot: Recorder,
) -> None:
    """Old server → SendRichMessage is refused → the same content goes out plain."""
    from aiogram.exceptions import TelegramBadRequest

    from waifu.tg.notify import safe_send
    from waifu.tg.rich import rich_log

    bot.fail_with = TelegramBadRequest(message="method not found", method="SendRichMessage")
    bot.fail_methods = {"SendRichMessage"}
    result = await safe_send(bot, 4242, "plain line", rich=rich_log("title", "body"))
    assert result.ok
    assert bot.by("SendRichMessage"), "the rich send must have been attempted"
    (plain,) = bot.to("send_message", 4242)
    assert plain["text"] == "plain line"


async def test_safe_send_sends_rich_when_available(bot: Recorder) -> None:
    from waifu.tg.notify import safe_send
    from waifu.tg.rich import rich_log

    result = await safe_send(bot, 4242, "plain line", rich=rich_log("title", "body"))
    assert result.ok
    (rich,) = bot.by("SendRichMessage")
    assert rich["chat_id"] == 4242
    assert not bot.to("send_message", 4242), "no plain send when the rich one landed"


async def test_notify_renders_rich_events(bot: Recorder, ctx, log_on) -> None:
    from waifu.tg.rich import rich_log

    ctx.caps = _caps(rich=True)
    assert await ctx.notify("a gift happened", rich=rich_log("🎁 gift", "a gift happened"))
    (rich,) = bot.by("SendRichMessage")
    assert rich["chat_id"] == LOG_CHAT
    # The plain fallback line must NOT be sent as a second message.
    assert len(bot.to("send_message", LOG_CHAT)) == 0


# ------------------------------------------------------------ /setlogchannel
def _channel_chat(chat_id: int, *, type_: str = "channel", title: str = "Owner Feed") -> Any:
    from aiogram.types import Chat

    return Chat(id=chat_id, type=type_, title=title)


def _admin_member(bot_id: int = 55) -> Any:
    from aiogram.types import ChatMemberAdministrator

    return ChatMemberAdministrator.model_validate(
        {
            "user": {"id": bot_id, "is_bot": True, "first_name": "B"},
            "can_be_edited": True,
            "is_anonymous": False,
            "can_manage_chat": True,
            "can_delete_messages": True,
            "can_manage_video_chats": True,
            "can_restrict_members": True,
            "can_promote_members": True,
            "can_change_info": True,
            "can_invite_users": True,
            "can_post_stories": True,
            "can_edit_stories": True,
            "can_delete_stories": True,
            "can_send_welcome_messages": True,
        }
    )


def _plain_member(bot_id: int = 55) -> Any:
    from aiogram.types import ChatMemberMember

    return ChatMemberMember.model_validate(
        {"user": {"id": bot_id, "is_bot": True, "first_name": "B"}}
    )


class ChannelBot(Recorder):
    """A Recorder that also answers the two lookups /setlogchannel performs."""

    id = 55  # the bot's own id, for get_chat_member(chat_id, bot.id)

    def __init__(self, chat: Any, member: Any) -> None:
        super().__init__()
        self._chat = chat
        self._member = member

    async def get_chat(self, chat_id: Any, *args: Any, **kwargs: Any) -> Any:
        self._record("get_chat", chat_id=chat_id)
        return self._chat

    async def get_chat_member(self, chat_id: Any, user_id: Any, *args: Any, **kwargs: Any) -> Any:
        self._record("get_chat_member", chat_id=chat_id, user_id=user_id)
        return self._member


def _setlog_command(args: str) -> Any:
    from aiogram.filters.command import CommandObject

    return CommandObject(command="setlogchannel", args=args)


async def test_setlogchannel_moves_the_feed_and_persists_it(ctx, tx, player) -> None:
    from waifu.core.access import Access
    from waifu.db.repo import stats
    from waifu.enums import Role
    from waifu.plugins.sudo import setlogchannel

    new_chat = -100999001
    ctx.bot = ChannelBot(_channel_chat(new_chat), _admin_member())
    await setlogchannel(
        _message(player, "/setlogchannel"),
        ctx,
        tx,
        _setlog_command(str(new_chat)),
        Access(user_id=player, role=Role.OWNER),
    )
    # Runtime change, database persistence, and the test line in the new channel.
    assert ctx.settings.log_channel_id == new_chat
    assert (await stats.kv_get(tx, "runtime_overrides"))["log_channel_id"] == new_chat
    (test,) = ctx.bot.to("send_message", new_chat)
    assert "log channel moved" in test["text"]


async def test_setlogchannel_rejects_a_non_admin_bot(ctx, tx, player) -> None:
    from waifu.core.access import Access
    from waifu.enums import Role
    from waifu.plugins.sudo import setlogchannel

    new_chat = -100999002
    ctx.bot = ChannelBot(_channel_chat(new_chat), _plain_member())
    await setlogchannel(
        _message(player, "/setlogchannel"),
        ctx,
        tx,
        _setlog_command(str(new_chat)),
        Access(user_id=player, role=Role.OWNER),
    )
    assert ctx.settings.log_channel_id == 0, "nothing moves until the rights check passes"
    assert not ctx.bot.to("send_message", new_chat)
    (reply,) = ctx.bot.to("send_message", player)
    assert "not an admin" in reply["text"]


async def test_setlogchannel_rejects_a_group(ctx, tx, player) -> None:
    from waifu.core.access import Access
    from waifu.enums import Role
    from waifu.plugins.sudo import setlogchannel

    ctx.bot = ChannelBot(_channel_chat(-100999003, type_="supergroup"), _admin_member())
    await setlogchannel(
        _message(player, "/setlogchannel"),
        ctx,
        tx,
        _setlog_command("-100999003"),
        Access(user_id=player, role=Role.OWNER),
    )
    assert ctx.settings.log_channel_id == 0
    (reply,) = ctx.bot.to("send_message", player)
    assert "channel" in reply["text"]


async def test_startup_applies_the_stored_channel_override(ctx, tx) -> None:
    """What /setlogchannel wrote survives a restart (this is the whole point)."""
    from waifu.db.repo import stats

    assert ctx.settings.log_channel_id == 0
    await stats.kv_set(tx, "runtime_overrides", {"log_channel_id": 555777})
    await tx.commit()
    await ctx._apply_runtime_overrides()
    assert ctx.settings.log_channel_id == 555777


# ----------------------------------------------------------------- the digest
async def test_week_summary_counts_the_weeks_events(
    ctx, tx, player, partner, any_character
) -> None:
    from datetime import timedelta

    from waifu.db.models import FairRoll, RaffleRound, StarPurchase, User
    from waifu.utils.time import now_utc

    character = await _character_in(tx, any_character)
    await collection_repo.grant(tx, player, character.id, source="fixture")
    await ctx.gifts.character(tx, player, partner, character.id)
    tx.add(FairRoll(user_id=player, sequence=1, commitment="abc123"))
    tx.add(RaffleRound(id=1, chat_id=1, ends_at=now_utc(), status="drawn"))
    tx.add(
        StarPurchase(
            user_id=player,
            invoice_payload="test-inv-1",
            star_count=100,
            coins_granted=5000,
            status="paid",
            paid_at=now_utc(),
        )
    )
    user = await tx.get(User, player)
    user.premium_until = now_utc() + timedelta(days=1)
    await tx.flush()

    summary = await ctx.stats.week_summary(tx, days=7)
    assert summary["new_players"] >= 2  # player + partner, materialised by the fixtures
    assert summary["gifts"] == 1
    assert summary["pulls"] == 1
    assert summary["raffles"] == 1
    assert summary["subs_active"] >= 1
    assert summary["stars"] == 100
    assert summary["orders"] == 1


async def test_send_digest_posts_the_rich_table(bot: Recorder, ctx, tx, log_on) -> None:
    from waifu.core.jobs import send_digest

    ctx.caps = _caps(rich=True)
    assert await send_digest(ctx) is True
    (rich,) = bot.by("SendRichMessage")
    assert rich["chat_id"] == LOG_CHAT
    assert not bot.to("send_message", LOG_CHAT)


async def test_send_digest_without_a_channel_is_a_noop(ctx, tx) -> None:
    from waifu.core.jobs import send_digest

    assert ctx.settings.log_channel_id == 0
    assert await send_digest(ctx) is False


# ------------------------------------------------------- raffle results in group
async def test_raffle_results_post(bot: Recorder, ctx, tx) -> None:
    from waifu.db.models import RaffleRound
    from waifu.utils.time import now_utc

    row = RaffleRound(
        id=9, chat_id=424200, emoji="🔥", reward=500, status="drawn", ends_at=now_utc()
    )
    # Old server: the plain HTML card goes out.
    await ctx.premium._raffle_results_post(tx, row, [1, 2, 3], [1])
    (plain,) = bot.to("send_message", 424200)
    assert "Raffle #9" in plain["text"]
    assert "500" in plain["text"]
    # New server: the rich card instead.
    ctx.caps = _caps(rich=True)
    await ctx.premium._raffle_results_post(tx, row, [1, 2, 3], [1])
    assert bot.by("SendRichMessage")


# ---------------------------------------------------- per-group log channels
async def test_group_notify_uses_the_groups_own_channel(bot: Recorder, ctx, tx, log_on) -> None:
    """Group.log_channel_id finally does something: a second copy, group-scoped."""
    from waifu.db.models import Group

    tx.add(Group(chat_id=424200, log_channel_id=555001))
    await tx.flush()

    ok = await ctx.group_notify(tx, 424200, "➕ alice joined · id 7")
    assert ok is True
    (line,) = bot.to("send_message", 555001)
    assert "alice joined" in line["text"]
    # The global owner channel (log_on) did NOT get the group copy.
    assert not bot.to("send_message", LOG_CHAT)

    # A group without a configured channel: quiet no-op.
    tx.add(Group(chat_id=424201))
    await tx.flush()
    assert await ctx.group_notify(tx, 424201, "nothing to see") is False
    assert len(bot.calls) == 1  # still only the one send from above


# ------------------------------------------------------- scheduled broadcasts
def test_parse_when_formats() -> None:
    from waifu.utils.schedule import ScheduleError, parse_when

    for arg in ("20:00", "20:00 tomorrow", "+2h", "in 30m", "+2d", "2026-12-31 20:00"):
        parse_when(arg)  # must parse
    for arg in ("whenever", "25:00", "2026-13-01", ""):
        with pytest.raises(ScheduleError):
            parse_when(arg)


async def test_broadcast_schedule_fire_and_log(bot: Recorder, ctx, tx, player) -> None:
    from datetime import timedelta

    from waifu.db.models import Group, ScheduledBroadcast
    from waifu.utils.schedule import parse_when
    from waifu.utils.time import now_utc

    tx.add(Group(chat_id=424210))  # a spawnable group for the fan-out
    run_at = parse_when("+1h")
    bc_id = await ctx.moderation.schedule_broadcast(
        tx, run_at=run_at, text="meet us", actor_id=player
    )
    assert bc_id
    pending = await ctx.moderation.pending_broadcasts(tx)
    assert len(pending) == 1 and pending[0].text == "meet us"
    await tx.flush()

    # Not due yet: nothing fires, the queue is untouched.
    assert await ctx.moderation.fire_due_broadcasts(tx) == {
        "broadcasts_fired": 0,
        "broadcasts_failed": 0,
    }
    assert len(await ctx.moderation.pending_broadcasts(tx)) == 1

    # Make it due: claimed atomically, fanned out, queue empty.
    row = await tx.get(ScheduledBroadcast, bc_id)
    row.run_at = now_utc() - timedelta(minutes=1)
    await tx.flush()
    result = await ctx.moderation.fire_due_broadcasts(tx)
    assert result == {"broadcasts_fired": 1, "broadcasts_failed": 0}
    assert bot.to("send_message", 424210)
    assert not await ctx.moderation.pending_broadcasts(tx)

    # A second pass finds nothing (the claim is permanent).
    assert await ctx.moderation.fire_due_broadcasts(tx) == {
        "broadcasts_fired": 0,
        "broadcasts_failed": 0,
    }
    # Cancel drops unsent rows only.
    await ctx.moderation.schedule_broadcast(
        tx, run_at=now_utc() + timedelta(hours=2), text="later", actor_id=player
    )
    await tx.flush()
    assert await ctx.moderation.cancel_broadcasts(tx) == 1
    assert (await tx.get(ScheduledBroadcast, bc_id)).sent_at is not None  # fired rows stay


# ------------------------------------------------------- character requests
async def test_request_queue_approve_flow(bot: Recorder, ctx, tx, player, partner) -> None:
    from waifu.db.repo import characters as char_repo
    from waifu.errors import NotFound

    row = await char_repo.submit_request(
        tx, name="Kiyomi", series="Fate/GO", requester_id=player, note="seen in the anime"
    )
    # Duplicate detection is case-insensitive (one ask per character).
    dup = await char_repo.find_pending_request(tx, "  kiyomi  ", "fate/go")
    assert dup is not None and dup.id == row.id
    # Approve: the row flips, the queue drains, and re-deciding is refused.
    approved = await char_repo.decide_request(tx, row.id, decision="approved", decided_by=partner)
    assert approved.status == "approved" and approved.decided_by == partner
    assert approved.decided_at is not None
    with pytest.raises(NotFound):
        await char_repo.decide_request(tx, row.id, decision="declined", decided_by=partner)
    assert await char_repo.pending_requests(tx) == []


async def test_request_command_dedupes_and_reports(
    bot: Recorder, ctx, tx, player, any_character
) -> None:
    from aiogram.filters.command import CommandObject

    from waifu.core.access import Access
    from waifu.enums import Role
    from waifu.plugins.requests import request_character

    access = Access(user_id=player, role=Role.USER)
    # First ask lands in the queue.
    await request_character(
        _message(player, "/request"),
        ctx,
        tx,
        CommandObject(command="request", args="Kiyomi Fate/GO"),
        access,
    )
    # Asking again is refused with the existing id.
    await request_character(
        _message(player, "/request"),
        ctx,
        tx,
        CommandObject(command="request", args="Kiyomi Fate/GO"),
        access,
    )
    replies = bot.to("send_message", player)
    assert any("already requested" in r["text"] for r in replies)
    # A character already in the roster is answered with a /check pointer.
    character = await _character_in(tx, any_character)
    await request_character(
        _message(player, "/request"),
        ctx,
        tx,
        CommandObject(command="request", args=f"{character.name} {character.anime}"),
        access,
    )
    (reply,) = [r for r in bot.to("send_message", player) if "/check" in r["text"]]
    assert character.name in reply["text"]


# ----------------------------------------------------- streak-break warnings
async def test_streak_warning_pass_warns_once(bot: Recorder, ctx, tx, player) -> None:
    from datetime import timedelta

    from waifu.core.jobs import _streaks
    from waifu.db.models import Streak
    from waifu.utils.time import now_utc

    yesterday = (now_utc().date() - timedelta(days=1)).isoformat()
    tx.add(Streak(user_id=player, current=5, highest=5, last_date=yesterday, freezes=0))
    await tx.commit()  # the pass reads through its own session

    first = await _streaks(ctx, limit=20)
    assert first["streak_warnings"] == 1
    (dm,) = bot.to("send_message", player)
    assert "5-day streak" in dm["text"]

    # Same cycle again: the kv marker makes it a one-shot, not an hourly DM.
    second = await _streaks(ctx, limit=20)
    assert second["streak_warnings"] == 0
    assert len(bot.to("send_message", player)) == 1
