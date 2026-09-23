"""``waifu.tg`` — Telegram-transport adapters, one module per Bot API capability.

Nothing in here knows about waifus, coins or Postgres. It converts intent
("show this card privately in the group", "stream this long answer", "charge 25 ⭐
before revealing") into the newest Bot API calls the endpoint actually supports,
and it is the only layer allowed to touch ``aiogram.methods`` directly.

Every module follows the same three rules:

1. **feature-detect, never assume** — capabilities come from
   :mod:`waifu.tg.caps`, negotiated once at startup;
2. **degrade, don't crash** — an unsupported call returns a falsy result (or
   :class:`~waifu.tg.messages.SendResult` with ``degraded=True``) and the caller's
   fallback path renders the same content;
3. **one place per feature** — so a Telegram schema change is a one-file diff.

===============================  ==================================================
Module                           Bot API feature
===============================  ==================================================
:mod:`~waifu.tg.caps`            capability negotiation for the endpoint in use
:mod:`~waifu.tg.rich`            rich messages (9.5+) — blocks, tables, collages
:mod:`~waifu.tg.messages`        unified send/edit with rich → photo → text fallback
:mod:`~waifu.tg.buttons`         button ``style``/``icon_custom_emoji_id``/``copy_text``,
                                 prepared inline messages (9.1) & share links
:mod:`~waifu.tg.ephemeral`       ephemeral messages (10.2) — private replies in groups
:mod:`~waifu.tg.draft`           message drafts (9.5+) — streaming long answers
:mod:`~waifu.tg.checklist`       checklists (9.3+) as interactive quest menus
:mod:`~waifu.tg.guest`           guest mode (10.0) — commands from chats without rights
:mod:`~waifu.tg.paid`            Stars: ``sendPaidMedia``, invoices, refunds, gifts
:mod:`~waifu.tg.media`           file_id caching, re-hosting, albums, Live Photo
:mod:`~waifu.tg.interactions`    reactions, member tags, emoji status, verification
:mod:`~waifu.tg.stories`         stories, menu button, Mini App links, command scopes
:mod:`~waifu.tg.business`        business messages (7.2) as a support inbox
:mod:`~waifu.tg.notify`          ``sendChatAction`` pacing + flood-aware broadcast
:mod:`~waifu.tg.text`            entity builders (custom emoji, ``date_time`` 9.1)
===============================  ==================================================
"""

from __future__ import annotations

from waifu.tg.buttons import callback, grid, pager, share_link
from waifu.tg.caps import Caps
from waifu.tg.ephemeral import ephemeral_note, send_ephemeral
from waifu.tg.media import collect_media, download_file, resolve_media
from waifu.tg.messages import SendResult, edit_card, send_card, style_button
from waifu.tg.notify import SendOutcome, TypingIndicator, broadcast, safe_send
from waifu.tg.rich import RichButton, RichMessageBuilder
from waifu.tg.text import bold, code, date_time, hidden, italic, link, mention, spoiler

__all__ = [
    "Caps",
    "RichButton",
    "RichMessageBuilder",
    "SendOutcome",
    "SendResult",
    "TypingIndicator",
    "bold",
    "broadcast",
    "callback",
    "code",
    "collect_media",
    "date_time",
    "download_file",
    "edit_card",
    "ephemeral_note",
    "grid",
    "hidden",
    "italic",
    "link",
    "mention",
    "pager",
    "resolve_media",
    "safe_send",
    "send_card",
    "send_ephemeral",
    "share_link",
    "spoiler",
    "style_button",
]
