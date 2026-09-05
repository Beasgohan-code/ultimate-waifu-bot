"""Telegram Stars: paid media, invoices, refunds, subscriptions, gifts.

The money surface, kept in one module because every one of these calls has a
correctness trap:

* ``sendPaidMedia`` — the *paywall*. ``star_count`` is what Telegram charges; the
  bot must not deliver anything until the ``purchased_paid_media`` update with the
  matching ``paid_media_payload`` arrives. Summon-bot delivered first and invoiced
  later, i.e. it could be farmed.
* ``createInvoiceLink`` — Star invoices (``XTR``) with a ``payload`` we use as the
  idempotency key; ``pre_checkout`` may be retried by Telegram.
* ``refundStarPayment`` — only allowed for a limited window; the DB marks the row
  so a second attempt is refused instead of throwing.
* ``editUserStarSubscription`` — Telegram's cancel for recurring Star payments;
  our subscription rows are keyed by ``telegram_payment_charge_id`` so a cancel can
  never demote the wrong user.
* ``getMyStarBalance`` — reported by /revenue and alerted when it can't cover a
  pending refund.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.methods import (
    CreateInvoiceLink,
    EditUserStarSubscription,
    GetMyStarBalance,
    RefundStarPayment,
    SendPaidMedia,
)
from aiogram.types import (
    InputMediaPhoto,
    InputPaidMediaPhoto,
    InputPaidMediaVideo,
    LabeledPrice,
    Message,
    PaidMediaInfo,
    PaidMediaPurchased,
    Update,
)

from waifu.logging import get_logger
from waifu.utils.text import truncate

log = get_logger("tg.paid")

CURRENCY_STARS = "XTR"


@dataclass(slots=True)
class Paywall:
    """A ``sendPaidMedia`` gate we can settle later from the purchase update."""

    chat_id: int
    star_count: int
    payload: str
    media: list[Any]
    caption: str = ""
    thread_id: int | None = None

    async def send(self, bot: Bot) -> Message | None:
        try:
            return await bot(
                SendPaidMedia(
                    chat_id=self.chat_id,
                    star_count=self.star_count,
                    media=self.media,
                    payload=self.payload[:128],
                    caption=truncate(self.caption, 1024) or None,
                    parse_mode="HTML",
                    show_caption_above_media=True,
                    message_thread_id=self.thread_id,
                )
            )
        except TelegramAPIError as exc:
            log.warning("sendPaidMedia failed (%s): %s", self.chat_id, exc)
            return None


def paywall_for(
    chat_id: int,
    *,
    star_count: int,
    payload: str,
    photos: Iterable[str],
    caption: str = "",
    thread_id: int | None = None,
) -> Paywall:
    """Build a photo paywall (art drops, spoiler images). Max 10 items per call."""
    media = [InputPaidMediaPhoto(media=src) for src in list(photos)[:10]]
    return Paywall(
        chat_id=chat_id,
        star_count=max(1, star_count),
        payload=payload,
        media=media,
        caption=caption,
        thread_id=thread_id,
    )


def paywall_video(
    chat_id: int,
    *,
    star_count: int,
    payload: str,
    video: str,
    cover: str | None = None,
    caption: str = "",
) -> Paywall:
    return Paywall(
        chat_id=chat_id,
        star_count=max(1, star_count),
        payload=payload,
        media=[InputPaidMediaVideo(media=video, thumbnail=cover)],
        caption=caption,
    )


def purchased_from(update: Update) -> PaidMediaPurchased | None:
    """The ``purchased_paid_media`` receipt on an update — the ONLY proof of payment."""
    return getattr(update, "purchased_paid_media", None)


def purchased_payload(update: Update) -> str:
    purchase = purchased_from(update)
    return str(getattr(purchase, "paid_media_payload", "") or "")


def paid_media_info(message: Message) -> PaidMediaInfo | None:
    """The paid-media block on the delivered message (``message.paid_media``)."""
    return getattr(message, "paid_media", None)


def paid_invoice(message: Message) -> Any:
    """Invoice metadata attached to a paid post, when the server provides it."""
    info = paid_media_info(message)
    return getattr(info, "invoice", None) if info else None


async def star_balance(bot: Bot) -> int:
    """Bot's own Stars balance (``getMyStarBalance``, API 7.4+)."""
    try:
        amount = await bot(GetMyStarBalance())
    except TelegramAPIError as exc:
        log.warning("getMyStarBalance unsupported/failed: %s", exc)
        return -1
    return int(getattr(amount, "amount", 0) or 0)


async def create_star_invoice(
    bot: Bot,
    *,
    title: str,
    description: str,
    payload: str,
    amount_stars: int,
    provider_token: str = "",
) -> str | None:
    """Create a Stars invoice link. ``XTR`` requires an empty provider token."""
    prices = [LabeledPrice(label=truncate(title, 32), amount=max(1, amount_stars))]
    try:
        return await bot(
            CreateInvoiceLink(
                title=truncate(title, 32),
                description=truncate(description, 256),
                payload=payload[:128],
                currency=CURRENCY_STARS,
                prices=prices,
                provider_token=provider_token or None,
            )
        )
    except TelegramAPIError as exc:
        log.error("createInvoiceLink failed: %s", exc)
        return None


async def refund(bot: Bot, *, user_id: int, charge_id: str) -> bool:
    """Refund a Star payment. Returns False when the API refused (already refunded)."""
    if not charge_id:
        return False
    try:
        await bot(RefundStarPayment(user_id=user_id, telegram_payment_charge_id=charge_id))
    except TelegramAPIError as exc:
        log.warning("refundStarPayment(%s) refused: %s", charge_id, exc)
        return False
    return True


async def cancel_subscription(
    bot: Bot, *, user_id: int, charge_id: str, active: bool = True
) -> bool:
    """Mirror Telegram-side cancellation of a recurring Star subscription."""
    if not charge_id:
        return False
    try:
        await bot(
            EditUserStarSubscription(
                user_id=user_id, telegram_payment_charge_id=charge_id, is_canceled=not active
            )
        )
    except TelegramAPIError as exc:
        log.warning("editUserStarSubscription failed: %s", exc)
        return False
    return True


async def subscription_invoice(
    bot: Bot,
    *,
    title: str,
    description: str,
    payload: str,
    amount_stars: int,
    subscription_period: int = 30 * 24 * 3600,
) -> str | None:
    """Recurring Stars subscription link (``createInvoiceLink`` + ``subscription_period``).

    Telegram then drives the lifecycle and sends ``BotSubscriptionUpdated``; the bot
    only mirrors it, so cancelling on Telegram's side cannot leave perks behind.
    """
    subscription_period = max(subscription_period, 86400)
    try:
        return await bot(
            CreateInvoiceLink(
                title=truncate(title, 32),
                description=truncate(description, 256),
                payload=payload[:128],
                currency=CURRENCY_STARS,
                prices=[LabeledPrice(label=truncate(title, 32), amount=max(1, amount_stars))],
                provider_token=None,
                subscription_period=subscription_period,
            )
        )
    except TelegramAPIError as exc:
        log.warning("subscription invoice unsupported: %s", exc)
        return None


async def transactions_summary(
    bot: Bot, *, offset: int = 0, limit: int = 25, direction: str = ""
) -> dict[str, Any]:
    """Recent Stars income/refunds for the owner dashboard.

    ``direction``: ``""`` all · ``"in"`` income · ``"out"`` refunds/spend. Telegram
    caps a page at 50 rows, so ``limit`` is clamped rather than rejected.
    """
    from aiogram.methods import GetStarTransactions

    try:
        rows = await bot(GetStarTransactions(offset=max(0, offset), limit=max(1, min(50, limit))))
    except TelegramAPIError as exc:
        log.warning("getStarTransactions unsupported: %s", exc)
        return {"count": 0, "stars_in": 0, "stars_out": 0, "rows": [], "error": str(exc)[:120]}
    out: list[dict[str, Any]] = []
    stars_in = stars_out = 0
    for transaction in getattr(rows, "transactions", []) or []:
        amount = int(getattr(transaction, "amount", 0) or 0)
        kind = str(getattr(transaction, "type", "") or "")
        is_out = (
            kind in {"refund", "subscription_expiration_refund", "paid_media_refund"} or amount < 0
        )
        if direction == "in" and is_out:
            continue
        if direction == "out" and not is_out:
            continue
        if is_out:
            stars_out += abs(amount)
        else:
            stars_in += abs(amount)
        source = getattr(transaction, "source", None)
        recipient = getattr(transaction, "recipient", None)
        out.append(
            {
                "id": str(getattr(transaction, "id", "")),
                "type": kind,
                "stars": amount,
                "out": is_out,
                "user_id": int(getattr(source, "id", 0) or getattr(recipient, "id", 0) or 0),
                "name": str(
                    getattr(source, "full_name", "") or getattr(recipient, "full_name", "") or ""
                ),
                "charge_id": str(getattr(transaction, "telegram_payment_charge_id", "") or ""),
                "date": getattr(transaction, "date", None),
            }
        )
    return {"count": len(out), "stars_in": stars_in, "stars_out": stars_out, "rows": out}


async def gift_premium_subscription(
    bot: Bot,
    user_id: int,
    *,
    months: int = 1,
    source: str = "premium_bot",
    message: str | None = None,
) -> dict[str, Any]:
    """Gift a premium subscription to a player (``giftPremiumSubscription``, API 8.0).

    Costs *the bot's* Telegram Premium / Stars, so it is owner-triggered only —
    usually as a raffle prize or an apology. ``user_ids`` is a list in the API, so a
    broadcast gift is one call, not N.
    """
    from aiogram.methods import GiftPremiumSubscription

    months = max(1, min(int(months), 36))
    try:
        result = await bot(
            GiftPremiumSubscription(
                user_ids=[user_id],
                subscription_period_months=months,
                source=source[:128],
                text=message and message[:255],
            )
        )
    except TelegramAPIError as exc:
        return {"ok": False, "error": str(exc)[:180]}
    granted = getattr(result, "premium_subscription_gifting_result", None)
    sent = int(getattr(granted, "or_upgrade_star_count", 0) or 0) if granted else 0
    return {"ok": True, "months": months, "stars_spent": sent}


async def send_gift(
    bot: Bot, user_id: int, *, gift_id: str, message: str | None = None, upgrade: bool = False
) -> bool:
    """Send a collected gift (``sendGift``, API 9.x). Used by /gift premium rewards."""
    from aiogram.methods import SendGift

    if not gift_id:
        return False
    try:
        await bot(
            SendGift(
                gift_id=gift_id, user_id=user_id, pay_for_upgrade=upgrade or None, text=message
            )
        )
    except TelegramAPIError as exc:
        log.warning("sendGift failed: %s", exc)
        return False
    return True


async def paid_media_price(
    bot: Bot, chat_id: int, star_count: int, media: list[Any], *, caption: str = ""
) -> dict[str, Any]:
    """Estimate the delivery payload for /premium "unlock this art for N ⭐"."""
    return {
        "chat_id": chat_id,
        "star_count": star_count,
        "items": len(media),
        "caption": caption,
        "media_types": sorted({getattr(m, "type", "photo") for m in media}),
    }


__all__ = [
    "CURRENCY_STARS",
    "InputMediaPhoto",
    "Paywall",
    "cancel_subscription",
    "create_star_invoice",
    "gift_premium_subscription",
    "paid_invoice",
    "paid_media_info",
    "paid_media_price",
    "paywall_for",
    "paywall_video",
    "purchased_from",
    "refund",
    "send_gift",
    "star_balance",
    "subscription_invoice",
    "transactions_summary",
]
