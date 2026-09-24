"""``/webapp``, ``/share``, ``/compact`` — the mini-app surface and share buttons.

Two ways to leave the chat for a better UI, and one way to stay in it:

* ``/webapp`` opens a Telegram Web App (``WebApp.initDataUnsafe`` carries the user, so
  the page needs no login) at ``WEBAPP_URL``; the ``web:data`` callback below is what the
  page fetches through ``sendMessage`` — a bot-to-page channel that survives a reload,
  unlike storing state in the URL;
* ``/share`` uses <b>prepared inline messages</b> (Bot API 9.6): the card is created
  once and any chat can be opened on it, which is the correct primitive for "send my
  harem to my friend" (``switchInlineQueryChosenChat``), instead of the deep-link +
  screenshot flow the old bot offered;
* ``/compact`` is the no-JS fallback: the same data as a text card, for players on
  clients that do not support Web Apps.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message

from waifu.plugins._kit import RichMessageBuilder, callback, card, cb, money, note, text

if TYPE_CHECKING:  # pragma: no cover
    from waifu.core.access import Access
    from waifu.core.context import AppContext

router = Router(name="webapp")


@router.message(Command("webapp", "app", "mini"))
async def webapp(message: Message, ctx: AppContext, session: Any, access: Access) -> None:
    url = str(ctx.settings.webapp_url or "")
    if not url:
        await text(
            message,
            ctx,
            "No mini-app URL is configured (WEBAPP_URL). /share and /collection work everywhere.",
        )
        return
    from aiogram.types import WebAppButtonInfo

    button = InlineKeyboardButton(text="🖼 open collection", web_app=WebAppButtonInfo(url=url))
    await card(
        message,
        ctx,
        builder=RichMessageBuilder()
        .heading("🖼 mini-app", size=2)
        .paragraph(
            html="A paginated, searchable view of your harem and the market, inside Telegram."
        ),
        html="open the mini app",
        buttons=[[button]],
    )


@router.callback_query(F.data == "web:harem")
async def harem_data(
    callback_query: CallbackQuery, ctx: AppContext, session: Any, access: Access
) -> None:
    """The payload a Web App page requests instead of scraping the chat."""
    page = await ctx.collection.page(session, access.user_id, page=0, page_size=12)
    payload = {
        "ok": True,
        "total": page.total,
        "value": page.value,
        "items": [
            {
                "id": int(entry.character_id),
                "name": entry.name,
                "anime": entry.anime,
                "tier": int(entry.rarity_id),
                "count": int(entry.count),
                "fav": bool(entry.is_favorite),
            }
            for entry in page.items
        ],
    }
    if callback_query.message is not None:
        await callback_query.message.answer(
            f"<code>{json.dumps(payload, ensure_ascii=False)[:3000]}</code>"
        )
    await note(callback_query, f"{page.total} characters")


@router.message(Command("share", "sendharem"))
async def share(message: Message, ctx: AppContext, session: Any, access: Access) -> None:
    """Prepare an inline card and hand the player a chat picker (Bot API 9.6)."""
    from waifu.tg.buttons import prepare_inline_message, switch_to_chat

    page = await ctx.collection.page(session, access.user_id, page=0, page_size=6)
    prepared_id = await prepare_inline_message(
        ctx.bot,
        user_id=access.user_id,
        title="my harem",
        text="\n".join(f"{entry.name} ×{entry.count}" for entry in page.items)
        or "empty — /pull first",
    )
    rows = [[switch_to_chat("📤 choose a chat")]] if prepared_id else []
    rows.append([callback("🔗 instead: link", cb("web", "link", prepared_id or ""))])
    await card(
        message,
        ctx,
        builder=RichMessageBuilder()
        .heading("📤 share your harem", size=2)
        .paragraph(
            html=f"{money(page.total)} characters · {money(page.value)} 🪙 value"
            + (
                "\n\nPick a chat, then tap the bot's name in the input box."
                if prepared_id
                else "\n\nInline prep is unavailable here; the link works everywhere."
            )
        ),
        html="share",
        buttons=rows,
    )


@router.callback_query(F.data.startswith("web:link:"))
async def share_link_button(callback_query: CallbackQuery, ctx: AppContext) -> None:
    from waifu.tg.buttons import share_link

    prepared = (callback_query.data or "").split(":", 2)[2]
    link = share_link(ctx.settings.bot_username or "", prepared or None)
    await callback_query.answer(url=link) if link else await note(
        callback_query, "no bot username configured", alert=True
    )


@router.message(Command("compact", "simple"))
async def compact(message: Message, ctx: AppContext, session: Any, access: Access) -> None:
    """Toggle the plain-text card mode (a11y + old clients; stored per player)."""
    from waifu.db.repo import users as user_repo

    pref = await user_repo.prefs(session, access.user_id)
    wanted = not bool((pref.flags or {}).get("compact", False))
    await user_repo.set_pref(session, access.user_id, compact=wanted)
    await text(
        message,
        ctx,
        f"{'🔤 compact cards on — no images, tables as lines' if wanted else '🖼 full cards on'}",
    )
