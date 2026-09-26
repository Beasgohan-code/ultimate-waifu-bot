"""Monetisation service — Stars, premium, boosts, paid media, raffles.

Every item here is an idempotency problem first and a feature second, so the rule is
uniform: **record the intent before Telegram is called, settle on the update, never
grant twice.**

* :meth:`invoice` writes the ``star_purchases`` row *before* handing out a link, so a
  player who pays twice, pays-then-disconnects, or pays-then-refunds can be answered
  from the database alone.
* :meth:`settle` is safe to call twice — delivery is a conditional ``paid → delivered``
  flip in the repository, not a read-modify-write.
* Paid media follows Telegram's own semantics: ``sendPaidMedia`` **is** the paywall
  (there is no separate invoice step), and the ``purchased_paid_media`` update with the
  matching payload is the only proof of payment. Summon-bot delivered first and
  invoiced later, i.e. it could be farmed.
* Boosts are counted, not trusted: the repository de-duplicates the at-least-once
  ``ChatBoostUpdated`` stream and redemption flips ``reward_state`` first.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from waifu.db.repo import economy as ledger
from waifu.db.repo import moderation as mod_repo
from waifu.db.repo import monetize as monetize_repo
from waifu.db.repo import users as user_repo
from waifu.enums import LedgerReason
from waifu.errors import NotFound, PaywallRequired
from waifu.services.base import Service
from waifu.settings import CoinPack as SettingsCoinPack
from waifu.tg import paid
from waifu.utils.text import esc, fmt_num, truncate
from waifu.utils.time import now_utc

#: A boost reward stays claimable for a day after the boost itself expires.
BOOST_GRACE = timedelta(days=1)
RAFFLE_ENTRY_KEY = "raffle:entry:{raffle_id}"


# The pack model itself lives in settings (``CoinPack``) — one definition, parsed and
# validated once, so the shop the bot advertises and the shop it settles cannot
# disagree. ``pack.id`` is the key players pick in /buy.
CoinPack = SettingsCoinPack


def _description(pack: CoinPack) -> str:
    bits = [f"{pack.coins:,} coins"] if pack.coins else []
    if pack.premium_hours:
        bits.append(f"{pack.premium_hours // 24}d premium")
    if pack.item_id:
        bits.append(f"1× {pack.item_id}")
    return " · ".join(bits) or "in-game coins"


@dataclass(slots=True)
class PremiumInfo:
    user_id: int
    is_active: bool
    hours_left: int
    subscribed: bool
    period_end: datetime | None
    perks: list[str]

    @property
    def status_line(self) -> str:
        return f"⭐ premium · {self.hours_left}h left" if self.is_active else "🆓 free tier"


@dataclass(slots=True)
class PaidPreview:
    """A ``sendPaidMedia`` gate plus the DB row that lets us settle it later."""

    chat_id: int
    character_id: int
    user_id: int
    stars: int
    payload: str
    message_id: int = 0
    owned: bool = False

    @property
    def note(self) -> str:
        return f"⭐{self.stars} · payload {self.payload} · character {self.character_id}"


class PremiumService(Service):
    # -------------------------------------------------------------- star shop
    async def coin_packs(self) -> list[CoinPack]:
        return self.settings.coin_packs

    async def invoice(
        self, session: AsyncSession, user_id: int, pack: str
    ) -> tuple[dict[str, Any], str | None]:
        """Order row first, then the Stars invoice link.

        The payload is random per order: Telegram echoes it back on
        ``successful_payment``, and matching on ``user_id`` alone would let a player
        replay somebody else's receipt.
        """
        packs = {p.id: p for p in await self.coin_packs()}
        if pack not in packs:
            raise NotFound(f"unknown pack “{pack}” — try one of {', '.join(sorted(packs))}")
        chosen = packs[pack]
        payload = f"coins:{user_id}:{chosen.id}:{secrets.token_hex(6)}"
        order = await monetize_repo.create_order(
            session,
            user_id=user_id,
            invoice_payload=payload,
            product="coins",
            product_ref=chosen.id,
            star_count=chosen.stars,
            coins_granted=chosen.coins,
            premium_hours=chosen.premium_hours,
        )
        link = await paid.create_star_invoice(
            self.bot,
            title=chosen.title,
            description=_description(chosen),
            payload=payload,
            amount_stars=chosen.stars,
        )
        return {
            "payload": payload,
            "coins": chosen.coins,
            "stars": chosen.stars,
            "premium_hours": chosen.premium_hours,
            "order_id": order.id,
        }, link

    async def premium_invoice(
        self, session: AsyncSession, user_id: int, *, months: int = 1
    ) -> tuple[dict[str, Any], str | None]:
        """One-off premium purchase (recurring is :meth:`subscribe`)."""
        days = self.settings.premium_sub_days * max(1, months)
        stars = self.settings.premium_sub_stars * max(1, months)
        payload = f"prem:{user_id}:{secrets.token_hex(6)}"
        await monetize_repo.create_order(
            session,
            user_id=user_id,
            invoice_payload=payload,
            product="premium",
            product_ref=f"{days}d",
            star_count=stars,
            premium_hours=days * 24,
        )
        link = await paid.create_star_invoice(
            self.bot,
            title=f"{self.settings.bot_name} premium · {days}d",
            description="Boosted claims, private drops, priority art, 2× /ai budget.",
            payload=payload,
            amount_stars=stars,
        )
        return {"payload": payload, "stars": stars, "days": days}, link

    async def subscribe(self, user_id: int, *, months: int = 1) -> str | None:
        """Recurring link (``createInvoiceLink.subscription_period``, API 7.4+).

        Telegram then owns the billing cycle and sends ``BotSubscriptionUpdated``,
        which :meth:`sync_subscription` mirrors — so a cancellation at Telegram
        cannot leave premium perks behind, which is the classic Stars support debt.
        """
        return await paid.subscription_invoice(
            self.bot,
            title=f"{self.settings.bot_name} premium",
            description="Boosted claims, private drops, priority art.",
            payload=f"sub:{user_id}:{secrets.token_hex(4)}",
            amount_stars=self.settings.premium_sub_stars,
            subscription_period=30 * 24 * 3600 * max(1, months),
        )

    # ------------------------------------------------------------------ settle
    async def settle(
        self, session: AsyncSession, payload: str, *, charge_id: str = "", star_count: int = 0
    ) -> dict[str, Any]:
        """Apply a successful payment. Safe to call twice, safe to call never."""
        order = await monetize_repo.by_payload(session, payload)
        if order is None:
            return {"ok": False, "reason": "unknown payload"}
        user_id = order.user_id
        await monetize_repo.mark_paid(
            session, payload, charge_id=charge_id, star_count=star_count or order.star_count
        )
        if not await monetize_repo.mark_delivered(session, payload):
            return {"ok": True, "duplicate": True, "user_id": order.user_id}
        granted: dict[str, Any] = {"coins": 0, "premium_hours": 0, "character_id": None}
        if order.coins_granted:
            await ledger.credit(
                session,
                order.user_id,
                int(order.coins_granted),
                LedgerReason.STARS,
                reference=f"stars:{payload}",
                idempotency_key=f"stars:{payload}",
            )
            granted["coins"] = int(order.coins_granted)
        if order.premium_hours:
            await ledger.grant_premium(
                session, order.user_id, hours=int(order.premium_hours), granted_by=0, source="stars"
            )
            await monetize_repo.record_premium_granted(session, payload, int(order.premium_hours))
            granted["premium_hours"] = int(order.premium_hours)
        if order.character_id:
            from waifu.db.repo import collection as collection_repo

            await collection_repo.grant(
                session, order.user_id, int(order.character_id), source="stars"
            )
            granted["character_id"] = int(order.character_id)
        await mod_repo.audit(
            session,
            actor_id=order.user_id,
            action="stars.purchase",
            target=payload,
            detail=f"charge={charge_id} granted={granted}",
        )
        # The owner's log channel is the money trail: every delivered payment,
        # with what was paid and what was granted, in one line.
        granted_bits = [
            bit
            for bit in (
                f"{int(granted.get('coins') or 0):,} 🪙" if granted.get("coins") else "",
                f"{int(granted.get('premium_hours') or 0) // 24}d premium"
                if granted.get("premium_hours")
                else "",
                f"#{granted['character_id']}" if granted.get("character_id") else "",
            )
            if bit
        ]
        from waifu.tg.rich import rich_log

        deliverables = " + ".join(granted_bits) or "no deliverables"
        if charge_id:
            deliverables += f"\ncharge {charge_id}"
        await self.log_line(
            f"💰 payment: {user_id} paid {int(order.star_count)} ⭐ for "
            f"{order.product_ref or order.product} → "
            + deliverables.replace("\n", " · ")
            + (f" (charge {charge_id})" if charge_id else ""),
            rich=rich_log(
                "💰 Stars payment",
                f"{user_id} paid {int(order.star_count)} ⭐ for {order.product_ref or order.product}",
                detail=deliverables,
            ),
        )
        return {"ok": True, "user_id": user_id, "granted": granted, "stars": int(order.star_count)}

    async def refund(
        self, session: AsyncSession, user_id: int, charge_id: str, *, reason: str = "requested"
    ) -> dict[str, Any]:
        """Refund at Telegram *and* claw the grant back.

        Claw-back is not optional: leaving it out is how "refund, keep the coins"
        becomes a farming loop. The debit may push the balance negative — that is
        exactly what a refunded purchase should look like in a ledger.
        """
        if not await paid.refund(self.bot, user_id=user_id, charge_id=charge_id):
            return {
                "ok": False,
                "error": "telegram refused the refund (outside the refund window, or already refunded)",
            }
        payload = await monetize_repo.payload_for_charge(session, charge_id)
        clawed = 0
        if payload:
            order = await monetize_repo.mark_refunded(session, payload)
            if order and order.coins_granted:
                clawed = int(order.coins_granted)
                await ledger.debit(
                    session,
                    user_id,
                    clawed,
                    LedgerReason.STARS,
                    reference=f"refund:{payload}",
                    idempotency_key=f"refund:{payload}",
                )
            if order and order.premium_hours:
                await ledger.grant_premium(
                    session, user_id, hours=-int(order.premium_hours), granted_by=0, source="refund"
                )
            if order and order.character_id:
                from waifu.db.repo import collection as collection_repo

                await collection_repo.consume(
                    session, user_id, int(order.character_id), 1, releasing=True
                )
        await mod_repo.audit(
            session,
            actor_id=0,
            action="stars.refund",
            target=str(user_id),
            detail=f"charge={charge_id} clawed={clawed} reason={truncate(reason, 120)}",
        )
        await self.log_line(
            f"↩️ refund: {user_id} · {charge_id} · clawed back {clawed:,} 🪙 ({truncate(reason, 60)})",
            silent=True,
        )
        return {"ok": True, "clawed_back": clawed, "user_id": user_id}

    # ------------------------------------------------------------ subscriptions
    async def sync_subscription(self, session: AsyncSession, subscription: Any) -> dict[str, Any]:
        """Consume a ``subscription`` update (Bot API 10.1, aiogram 3.31).

        The update carries exactly three fields — ``user``, ``invoice_payload``
        and ``state`` (``active`` | ``canceled`` | ``failed``). There is no price
        and no renewal flag: the price is whatever the link we created charged,
        and **renewals are silent**, which is why :meth:`renew_due_subscriptions`
        runs nightly instead of trusting an update that never comes.

        (The previous version read ``subscriber`` / ``is_canceled`` /
        ``is_renewal`` — fields that do not exist on the 10.1 object — so every
        subscription update was a silent no-op and cancellations leaked premium.)
        """
        user = getattr(subscription, "user", None)
        user_id = int(getattr(user, "id", 0) or 0)
        if not user_id:
            return {"ok": False, "reason": "no user on subscription update"}
        # A subscriber update does not materialise the player row the way a chat
        # does (the middleware reads ``from_user``, which this update lacks) —
        # and the premium/subscription tables have a FK to ``users``. Without
        # this, a first-time subscriber's payment update dies on the FK.
        await user_repo.upsert(
            session,
            user_id,
            first_name=str(getattr(user, "first_name", "") or ""),
            username=getattr(user, "username", None),
            settings=self.settings,
        )
        payload = str(getattr(subscription, "invoice_payload", "") or "")
        state = str(getattr(subscription, "state", "") or "").lower()

        if state == "canceled":
            closed = await monetize_repo.close_subscription(session, user_id)
            await mod_repo.audit(
                session,
                actor_id=user_id,
                action="subscription.cancel",
                target=payload or f"sub-{user_id}",
                detail="telegram update",
            )
            if closed:
                from waifu.tg.rich import rich_log

                await self.log_line(
                    f"💔 subscription cancelled by {user_id}",
                    silent=True,
                    rich=rich_log("💔 subscription cancelled", f"by {user_id}"),
                )
            return {"ok": True, "state": "cancelled", "user_id": user_id, "closed": closed}

        if state == "failed":
            # Telegram's dunning: it keeps trying the card, the perks stay as
            # they are — but the owner must see it, or "why is he still premium
            # after canceling?" becomes a support ticket.
            await mod_repo.audit(
                session,
                actor_id=user_id,
                action="subscription.failed",
                target=payload or f"sub-{user_id}",
                detail="telegram update",
            )
            from waifu.tg.rich import rich_log

            await self.log_line(
                f"❌ subscription payment failed for {user_id} — Telegram will keep retrying",
                silent=True,
                rich=rich_log(
                    "❌ subscription payment failed",
                    f"for {user_id}",
                    detail="Telegram will keep retrying the charge",
                ),
            )
            return {"ok": True, "state": "failed", "user_id": user_id}

        # "active" — a fresh subscription or a re-enabled one. An *already
        # active* subscription means a duplicated at-least-once update: the
        # period and the perks stay exactly as they are (closing + re-granting
        # here would flicker the user's premium to zero for a moment).
        if await monetize_repo.has_subscription(session, user_id):
            await mod_repo.audit(
                session,
                actor_id=user_id,
                action="subscription.active",
                target=payload or f"sub-{user_id}",
                detail="duplicate or no-op",
            )
            return {"ok": True, "state": "active", "user_id": user_id, "already_active": True}
        await monetize_repo.upsert_subscription(
            session,
            user_id=user_id,
            subscription_id=payload or f"sub-{user_id}",
            chat_id=None,
            tier="supporter",
            status="active",
            amount=self.settings.premium_sub_stars,
            currency="XTR",
            current_period_end=now_utc() + timedelta(days=self.settings.premium_sub_days),
        )
        await ledger.grant_premium(
            session,
            user_id,
            hours=self.settings.premium_sub_days * 24,
            granted_by=0,
            source="stars",
        )
        await mod_repo.audit(
            session,
            actor_id=user_id,
            action="subscription.active",
            target=payload or f"sub-{user_id}",
            detail="telegram update",
        )
        from waifu.tg.rich import rich_log

        await self.log_line(
            f"🎉 {user_id} started a premium subscription · "
            f"{self.settings.premium_sub_stars} ⭐ every {self.settings.premium_sub_days}d",
            rich=rich_log(
                "🎉 premium subscription started",
                f"{user_id} · {self.settings.premium_sub_stars} ⭐ every "
                f"{self.settings.premium_sub_days}d",
            ),
        )
        return {"ok": True, "state": "active", "user_id": user_id}

    async def renew_due_subscriptions(self, session: AsyncSession) -> int:
        """Extend premium on subscriptions whose period lapsed.

        Telegram renews silently (no ``subscription`` update on renewal), so
        without this pass a subscriber's premium would quietly expire while the
        Stars keep leaving their account — the exact moment users open disputes.
        The nightly job calls it; each renewal is a payment the owner gets told
        about, same as a fresh one.
        """
        days = self.settings.premium_sub_days
        renewed = 0
        for user_id, amount in await monetize_repo.due_renewals(session):
            await monetize_repo.extend_subscription(session, user_id, days=days)
            await ledger.grant_premium(
                session,
                user_id,
                hours=days * 24,
                granted_by=0,
                source="subscription",
            )
            renewed += 1
            from waifu.tg.rich import rich_log

            await self.log_line(
                f"🔁 {user_id} renewed premium — {amount} ⭐ · {days}d (silent renewal)",
                rich=rich_log(
                    "🔁 premium renewed",
                    f"{user_id} · {amount} ⭐ · {days}d",
                    detail="silent renewal (Telegram renews without an update)",
                ),
            )
        return renewed

    async def cancel_subscription(
        self, session: AsyncSession, user_id: int, *, reason: str = "user request"
    ) -> dict[str, Any]:
        """Cancel at Telegram too (``editUserStarSubscription``), not only in our DB."""
        charge_id = await monetize_repo.active_subscription_charge(session, user_id)
        if not charge_id:
            raise NotFound("no active subscription")
        ok = await paid.cancel_subscription(
            self.bot, user_id=user_id, charge_id=charge_id, active=False
        )
        await monetize_repo.close_subscription(session, user_id)
        await mod_repo.audit(
            session, actor_id=user_id, action="subscription.cancel", target=charge_id, detail=reason
        )
        return {"ok": ok, "charge_id": charge_id, "mirrored": True}

    async def subscription(self, session: AsyncSession, user_id: int) -> dict[str, Any]:
        return {
            "rows": await monetize_repo.subscription_rows(session, user_id),
            "period_end": await monetize_repo.subscription_period_end(session, user_id),
            "premium": await self.premium(session, user_id),
        }

    # ------------------------------------------------------------------ premium
    async def premium(self, session: AsyncSession, user_id: int) -> PremiumInfo:
        hours = await ledger.premium_left_hours(session, user_id)
        subscribed = await monetize_repo.has_subscription(session, user_id)
        perks = [
            f"+{self.settings.premium_claim_boost_percent}% claim odds on high tiers",
            "double /daily claim (a premium slot after the free one)",
            "extra /reroll and free /shop refresh",
            "paid previews at half price",
            "2× /ai character budget",
        ]
        return PremiumInfo(
            user_id=user_id,
            is_active=hours > 0,
            hours_left=hours,
            subscribed=subscribed,
            period_end=await monetize_repo.subscription_period_end(session, user_id),
            perks=perks,
        )

    async def grant_premium(
        self,
        session: AsyncSession,
        user_id: int,
        hours: int,
        *,
        granted_by: int = 0,
        source: str = "admin",
    ) -> int:
        """Admin/boost/gift grant. Negative hours revoke (used by refund claw-backs)."""
        await ledger.grant_premium(
            session, user_id, hours=hours, granted_by=granted_by, source=source
        )
        left = await ledger.premium_left_hours(session, user_id)
        await mod_repo.audit(
            session,
            actor_id=granted_by,
            action="premium.grant",
            target=str(user_id),
            detail=f"{hours}h ({source}) → {left}h left",
        )
        await self.log_line(
            f"{'⭐ premium +' if hours > 0 else '↩️ premium −'}{abs(hours)}h → {user_id}"
            + (f" by {granted_by}" if granted_by else "")
            + f" ({source}) · {left}h left",
            silent=True,
        )
        return left

    async def consume_premium_claim(
        self, session: AsyncSession, user_id: int, day: str, *, premium: bool
    ) -> bool:
        """True when this claim may proceed (``claim_once`` is atomic, so a double
        tap or a duplicate webhook pays once)."""
        if await ledger.claim_once(session, user_id, "daily", day):
            return True
        return bool(premium) and await ledger.claim_once(session, user_id, "daily_premium", day)

    async def is_premium(self, session: AsyncSession, user_id: int) -> bool:
        return (await ledger.premium_left_hours(session, user_id)) > 0

    async def gift_premium(
        self, user_id: int, *, months: int = 1, message: str = "for being here"
    ) -> dict[str, Any]:
        """Send a real Telegram Premium gift (Bot API 8.0) — raffle prizes, apologies."""
        result = await paid.gift_premium_subscription(
            self.bot, user_id, months=months, message=message or None
        )
        if result.get("ok"):
            await self.log_line(f"🎁 premium gift → {user_id} ({months}mo)")
        return result

    # --------------------------------------------------------------- paid media
    async def preview_price(self, *, premium: bool = False) -> int:
        price = max(1, self.settings.paid_media_star_price)
        return max(1, price // 2) if premium else price

    async def owns_preview(self, session: AsyncSession, user_id: int, character_id: int) -> bool:
        return await monetize_repo.owns_paid_media(session, user_id, character_id)

    async def post_preview(
        self,
        session: AsyncSession,
        *,
        chat_id: int,
        character_id: int,
        photo: str,
        caption: str = "",
        price: int | None = None,
        buyer_id: int | None = None,
        thread_id: int | None = None,
    ) -> PaidPreview:
        """Publish art *as the paywall* (``sendPaidMedia``).

        The order row is created first with a random payload; Telegram refunds
        automatically if nobody pays, and the only way to settle it is the
        ``purchased_paid_media`` update carrying that payload.
        """
        stars = int(price) if price else await self.preview_price()
        payload = f"pm:{character_id}:{secrets.token_hex(8)}"
        await monetize_repo.create_order(
            session,
            user_id=buyer_id or chat_id,
            invoice_payload=payload,
            product="paid_media",
            product_ref=str(character_id),
            star_count=stars,
            character_id=character_id,
            source="paid_media",
        )
        wall = paid.paywall_for(
            chat_id,
            star_count=stars,
            payload=payload,
            photos=[photo],
            caption=caption,
            thread_id=thread_id,
        )
        message = await wall.send(self.bot)
        return PaidPreview(
            chat_id=chat_id,
            character_id=character_id,
            user_id=buyer_id or 0,
            stars=stars,
            payload=payload,
            message_id=int(getattr(message, "message_id", 0) or 0),
        )

    async def settle_purchase(self, session: AsyncSession, update: Any) -> dict[str, Any]:
        """Handle ``purchased_paid_media``: match payload → mark paid → grant.

        Ownership is a real collection grant, so an unlocked preview survives the
        original message being deleted (Summon-bot's buyers lost the art when the
        post got cleaned up).
        """
        payload = paid.purchased_payload(update)
        if not payload:
            return {"ok": False, "reason": "no payload on update"}
        buyer = getattr(getattr(update, "purchased_paid_media", None), "from_user", None)
        user_id = int(getattr(buyer, "id", 0) or 0)
        order = await monetize_repo.by_payload(session, payload)
        if order is None:
            await mod_repo.audit(
                session,
                actor_id=user_id,
                action="media.orphan",
                target=payload,
                detail="paid for an order we do not have",
            )
            return {"ok": False, "reason": "unknown payload", "user_id": user_id}
        await monetize_repo.mark_paid(
            session,
            payload,
            charge_id=str(
                getattr(update.purchased_paid_media, "telegram_payment_charge_id", "") or ""
            ),
            star_count=int(getattr(update.purchased_paid_media, "star_count", 0) or 0),
        )
        await monetize_repo.mark_delivered(session, payload)
        if order.character_id and user_id:
            from waifu.db.repo import characters as char_repo
            from waifu.db.repo import collection as collection_repo
            from waifu.enums import Rarity

            await collection_repo.grant(session, user_id, int(order.character_id), source="stars")
            character = await char_repo.get(session, int(order.character_id))
            if character is not None:
                # The buyer's proof of purchase — the paywall post can be deleted
                # and the character is theirs either way, but the receipt is what
                # makes "I paid, where is it?" a non-question.
                await self.deliver_character_dm(
                    user_id,
                    character,
                    (
                        "🔓 <b>Unlocked!</b> You paid "
                        f"{int(order.star_count)} ⭐ and this is yours now.\n\n"
                        f"<b>{esc(character.name)}</b>"
                        + (f" — {esc(character.anime)}" if character.anime else "")
                        + f"\n{Rarity.from_value(int(character.rarity_id)).badge}"
                        + "\n\nRe-send it any time from your collection."
                    ),
                )
        await mod_repo.audit(
            session,
            actor_id=user_id,
            action="media.unlock",
            target=str(order.character_id or 0),
            detail=f"payload={payload}",
        )
        await self.log_line(
            f"💳 paid media: {user_id or order.user_id} unlocked #{order.character_id} "
            f"for {int(order.star_count)} ⭐"
        )
        return {
            "ok": True,
            "user_id": user_id,
            "character_id": int(order.character_id or 0),
            "stars": int(order.star_count),
        }

    async def require_preview(
        self, session: AsyncSession, user_id: int, character_id: int, *, premium: bool = False
    ) -> int:
        """Return the price, or raise :class:`PaywallRequired` for the unlock button."""
        if await self.owns_preview(session, user_id, character_id):
            return 0
        return await self.preview_price(premium=premium)

    async def deliver_owned(
        self,
        session: AsyncSession,
        user_id: int,
        character_id: int,
        photo: str,
        *,
        caption: str = "",
    ) -> dict[str, Any]:
        """Re-send something a player already unlocked — free, plain ``sendPhoto``."""
        if not await self.owns_preview(session, user_id, character_id):
            raise PaywallRequired(f"🔒 not unlocked yet · {await self.preview_price()} ⭐")
        from waifu.tg.media import send_media

        return await send_media(self.bot, user_id, photo, caption=caption or "from your unlocks")

    async def previews_bought(self, session: AsyncSession, user_id: int) -> list[dict[str, Any]]:
        return await monetize_repo.paid_media_history(session, user_id)

    async def preview_buyers(self, session: AsyncSession, character_id: int) -> list[int]:
        return await monetize_repo.paid_media_buyers(session, character_id)

    # ------------------------------------------------------------------- boosts
    async def record_boost(
        self,
        session: AsyncSession,
        *,
        user_id: int,
        chat_id: int,
        boost_id: str,
        count: int = 1,
        source: str = "premium",
    ) -> bool:
        expires = now_utc() + timedelta(days=30)
        created = await monetize_repo.record_boost(
            session,
            user_id=user_id,
            chat_id=chat_id,
            boost_id=boost_id,
            boost_count=count,
            source=source,
            expires_at=expires,
        )
        if created:
            await self.log_line(f"🚀 {user_id} boosted this chat ×{count}")
        return created

    async def pending_boosts(self, session: AsyncSession, user_id: int, chat_id: int) -> int:
        return await monetize_repo.pending_boost_count(session, user_id, chat_id)

    async def redeem_boost_rewards(
        self, session: AsyncSession, user_id: int, chat_id: int, *, per_boost: int = 5000
    ) -> dict[str, int]:
        """Exchange pending boosts for coins + premium hours.

        Marked consumed *before* paying: losing a reward to a crash is recoverable by
        an admin, paying twice on a retry is not.
        """
        pending = await monetize_repo.pending_boost_count(session, user_id, chat_id)
        if pending <= 0:
            return {"boosts": 0, "coins": 0, "premium_hours": 0}
        rows = await monetize_repo.consume_boost_rewards(session, user_id, chat_id)
        coins = pending * per_boost
        hours = min(pending, 10) * 24
        await ledger.credit(
            session,
            user_id,
            coins,
            LedgerReason.STARS,
            reference=f"boost:{chat_id}:{rows}",
            idempotency_key=f"boost:{chat_id}:{user_id}:{rows}",
        )
        if hours:
            await ledger.grant_premium(session, user_id, hours=hours, granted_by=0, source="boost")
        await mod_repo.audit(
            session,
            actor_id=user_id,
            action="boost.redeem",
            target=str(chat_id),
            detail=f"{pending} boosts → {fmt_num(coins)} coins, {hours}h",
        )
        return {"boosts": pending, "coins": coins, "premium_hours": hours}

    async def boosters(
        self, session: AsyncSession, chat_id: int, *, limit: int = 25
    ) -> list[tuple[int, int]]:
        return await monetize_repo.boosters_in(session, chat_id, limit=limit)

    # ------------------------------------------------------------------ raffles
    async def open_raffle(
        self,
        session: AsyncSession,
        *,
        chat_id: int,
        message_id: int | None,
        emoji: str = "🎉",
        reward: int = 0,
        seconds: int = 600,
        max_winners: int = 3,
        theme: str = "",
    ) -> Any:
        row = await monetize_repo.open_raffle(
            session,
            chat_id=chat_id,
            message_id=message_id,
            emoji=emoji,
            reward=reward,
            seconds=seconds,
            max_winners=max_winners,
            theme=theme,
        )
        if self.redis is not None:
            # Fresh key per round; the TTL outlives the round so a late reaction is
            # simply ignored instead of leaking into the next raffle.
            await self.redis.client.expire(
                self.redis.k(RAFFLE_ENTRY_KEY.format(raffle_id=row.id)), max(120, seconds + 3600)
            )  # type: ignore[union-attr]
        return row

    async def add_entrant(self, raffle_id: int, user_id: int) -> bool:
        """React-to-enter. One entry per user — ``SADD`` returns 0 for a duplicate."""
        if self.redis is None:
            return False
        return (
            await self.redis.sadd(RAFFLE_ENTRY_KEY.format(raffle_id=raffle_id), str(user_id))
        ) > 0

    async def entrants(self, raffle_id: int) -> list[int]:
        if self.redis is None:
            return []
        return sorted(
            int(member)
            for member in await self.redis.smembers(RAFFLE_ENTRY_KEY.format(raffle_id=raffle_id))
        )

    async def current_raffle(self, session: AsyncSession, chat_id: int) -> Any:
        return await monetize_repo.current_raffle(session, chat_id)

    async def raffle_by_message(self, session: AsyncSession, chat_id: int, message_id: int) -> Any:
        return await monetize_repo.raffle_by_message(session, chat_id, message_id)

    async def draw_raffles(self, session: AsyncSession) -> list[dict[str, Any]]:
        """Close every expired raffle: pick winners uniformly, pay, react to their post."""
        out: list[dict[str, Any]] = []
        for row in await monetize_repo.raffles_to_draw(session):
            pool = await self.entrants(row.id)
            winners = (
                sorted(secrets.SystemRandom().sample(pool, k=min(row.max_winners, len(pool))))
                if pool
                else []
            )
            for user_id in winners:
                if row.reward:
                    await ledger.credit(
                        session,
                        user_id,
                        int(row.reward),
                        LedgerReason.STARS,
                        reference=f"raffle:{row.id}",
                        idempotency_key=f"raffle:{row.id}:{user_id}",
                    )
            await monetize_repo.finish_raffle(
                session, row.id, winners=dict.fromkeys(winners, int(row.reward))
            )
            if winners:
                # one reaction on the announcement, not one per winner: the emoji is the
                # "it happened" marker for the whole chat, and N calls would be N flood-wait
                # risks in the busiest chats. The group also gets the result as a message —
                # a reaction says "something happened", the card says who won.
                await self._react_ids(row.chat_id, int(row.message_id or 0), row.emoji or "🎉")
                await self._raffle_results_post(session, row, pool, winners)
            if row.reward and winners:
                # Coins leaving the house are a payment event: the owner sees who won.
                await self.log_line(
                    f"🎟️ raffle #{row.id} in {row.chat_id}: {len(pool)} entrants → "
                    + ", ".join(str(w) for w in winners)
                    + f" · {int(row.reward):,} 🪙 each",
                    silent=True,
                )
            if self.redis is not None:
                await self.redis.delete(RAFFLE_ENTRY_KEY.format(raffle_id=row.id))
            out.append(
                {
                    "raffle_id": row.id,
                    "chat_id": row.chat_id,
                    "entrants": len(pool),
                    "winners": winners,
                }
            )
        return out

    async def _react_ids(self, chat_id: int, message_id: int, emoji: str) -> None:
        """``setMessageReaction`` on the raffle post — a public announcement of the draw."""
        if not message_id or not self.ctx.caps.allow("reactions"):
            return
        from aiogram.exceptions import TelegramAPIError

        from waifu.tg.interactions import emoji_reaction

        try:
            await self.bot.set_message_reaction(
                chat_id, message_id, reaction=[emoji_reaction(emoji)], is_big=True
            )
        except TelegramAPIError:  # pragma: no cover - bot may lack the right in some chats
            pass

    async def _raffle_results_post(
        self, session: Any, row: Any, pool: list[int], winners: list[int]
    ) -> None:
        """The draw's result as a card in the group (rich when the server has it).

        The reaction marks the moment; this names the winners. Kept in the
        service (not the handler) because the draw runs from the scheduler, and
        a blocked group (bot kicked) degrades to a logged skip, not a crash.
        Groups with their own log channel get the line there as well.
        """
        if self.bot is None:
            return
        from waifu.tg.notify import safe_send
        from waifu.tg.rich import RichMessageBuilder, rich_log

        reward = int(row.reward or 0)
        names = ", ".join(str(w) for w in winners)
        body = (
            f"🎟️ <b>Raffle #{row.id} results</b>\n"
            f"{len(pool)} entrants · {len(winners)} winner(s)\n"
            f"winners: <code>{names}</code>"
            + (f"\nreward: <b>{reward:,} 🪙 each</b>" if reward else "")
        )
        rich = None
        if self.ctx.caps.rich_messages:
            builder = RichMessageBuilder().heading(f"🎟️ raffle #{row.id} results")
            for winner in winners:
                builder.line(f"🏆 {winner}" + (f" — {reward:,} 🪙" if reward else ""))
            builder.footer(f"{len(pool)} entrants · drawn now")
            rich = builder.build()
        await safe_send(self.bot, row.chat_id, body, rich=rich, disable_notification=False)
        # The group's own log channel, when the admins set one up.
        await self.ctx.group_notify(
            session,
            row.chat_id,
            f"🎟️ raffle #{row.id} drawn: {len(pool)} entrants → "
            + ", ".join(str(w) for w in winners)
            + (f" · {reward:,} 🪙 each" if reward else ""),
            rich=rich_log(
                f"🎟️ raffle #{row.id} drawn",
                f"{len(pool)} entrants → " + ", ".join(str(w) for w in winners),
                detail=f"{reward:,} 🪙 each" if reward else "no coin reward",
            ),
        )

    async def raffle_history(
        self, session: AsyncSession, chat_id: int, *, limit: int = 10
    ) -> list[Any]:
        return await monetize_repo.raffle_history(session, chat_id, limit=limit)

    # ---------------------------------------------------------------- revenue
    async def revenue(self, session: AsyncSession, *, days: int = 30) -> dict[str, Any]:
        since = now_utc() - timedelta(days=days)
        totals = await monetize_repo.revenue(session, since=since)
        totals["balance_stars"] = await paid.star_balance(self.bot)
        totals.update(await paid.transactions_summary(self.bot, limit=25))
        totals["pending"] = len(await monetize_repo.pending_orders(session, older_than_minutes=120))
        totals["supporters"] = await monetize_repo.supporter_count(session)
        totals["since_days"] = days
        totals["top_supporters"] = [
            {"user_id": user.id, "name": user.full_name, "stars": stars}
            for user, stars in await monetize_repo.top_supporters(session, limit=10)
        ]
        return totals

    async def my_orders(
        self, session: AsyncSession, user_id: int, *, limit: int = 10
    ) -> dict[str, Any]:
        """What a player paid us — they can see it without asking.

        The refund button is here for the same reason: Telegram allows a Stars refund
        on request within its window, and a bot that hides that is one complaint away
        from a payment-provider dispute.
        """
        rows = await monetize_repo.orders_for(session, user_id, limit=limit)
        return {
            "orders": [
                {
                    "payload": row.invoice_payload,
                    "product": row.product,
                    "stars": int(row.star_count),
                    "status": row.status,
                    "at": row.created_at,
                    "refundable": bool(row.telegram_payment_charge_id)
                    and row.status != "refunded"
                    and (now_utc() - row.created_at) < timedelta(days=3),
                }
                for row in rows
            ],
            "ever_paid": await monetize_repo.has_ever_paid(session, user_id),
            "total_stars": sum(
                int(row.star_count or 0) for row in rows if row.status in {"paid", "delivered"}
            ),
        }

    async def pending_report(
        self, session: AsyncSession, *, minutes: int = 120
    ) -> list[dict[str, Any]]:
        """Orders paid-but-undelivered or abandoned mid-sheet (the support queue)."""
        rows = await monetize_repo.pending_orders(session, older_than_minutes=minutes)
        return [
            {
                "payload": row.invoice_payload,
                "user_id": row.user_id,
                "product": row.product,
                "stars": int(row.star_count),
                "at": row.created_at,
            }
            for row in rows
        ]

    async def user_name(self, session: AsyncSession, user_id: int) -> str:
        user = await user_repo.get(session, user_id)
        return user.full_name if user else str(user_id)
