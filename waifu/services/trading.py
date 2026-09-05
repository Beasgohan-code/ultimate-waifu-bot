"""Trades, gifts and redeem codes — the player-to-player surface.

All three share one requirement: **the item must be unavailable to its owner the
instant the deal is offered, and restored the instant it fails.** The reference bot
did not escrow, so a player could list the same character in a trade, an auction and
the market simultaneously and walk away with three payouts.

Here:

* :meth:`TradeService.propose` locks both sides' ownership rows
  (``Ownership.is_locked``) and stores the offer with a short code;
* acceptance is confirmed through **ephemeral messages** (Bot API 10.2) so the
  group doesn't see the terms and, more importantly, so neither side can be
  pressured into a public "just tap yes";
* execution is a compare-and-set on ``trade_offers.status`` — the job and the
  button cannot both complete a trade;
* gifts and code redemptions use unique idempotency keys, so a retried webhook
  cannot double-pay.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from waifu.db.models import Character, RedeemCode, TradeOffer
from waifu.db.repositories import characters as char_repo
from waifu.db.repositories import collection as collection_repo
from waifu.db.repositories import economy as ledger
from waifu.db.repositories import moderation as mod_repo
from waifu.db.repositories import trades as trade_repo
from waifu.db.repositories import users as user_repo
from waifu.enums import LedgerReason, Rarity
from waifu.errors import Locked, NotFound, WaifuError
from waifu.services.base import Service
from waifu.tg.ephemeral import send_ephemeral
from waifu.utils.text import truncate
from waifu.utils.time import human_delta, now_utc, to_naive_utc


@dataclass(slots=True)
class TradeView:
    id: int
    code: str
    initiator_id: int
    partner_id: int
    status: str
    giving: list[tuple[int, str, int]] = field(default_factory=list)
    receiving: list[tuple[int, str, int]] = field(default_factory=list)
    cash: int = 0
    expires_in: int = 0
    initiator_accepted: bool = False
    partner_accepted: bool = False

    @property
    def summary(self) -> str:
        def _fmt(items: list[tuple[int, str, int]]) -> str:
            return ", ".join(f"{name}×{count}" for _id, name, count in items) or "nothing"

        left = f"{_fmt(self.giving)}" + (
            f" + {self.cash:,} 🪙".replace(",", "\u2009") if self.cash else ""
        )
        right = f"{_fmt(self.receiving)}" + (
            f" + {abs(self.cash):,} 🪙".replace(",", "\u2009") if self.cash < 0 else ""
        )
        return f"{left} ⇄ {right}"

    @property
    def ready(self) -> bool:
        return self.initiator_accepted and self.partner_accepted and self.status == "accepted"


@dataclass(slots=True)
class RedeemResult:
    code: str
    coins: int = 0
    character: Character | None = None
    premium_hours: int = 0
    balance: int = 0

    @property
    def lines(self) -> list[str]:
        out: list[str] = []
        if self.coins:
            out.append(f"🪙 {self.coins:,} coins".replace(",", "\u2009"))
        if self.character is not None:
            out.append(
                f"🎁 {self.character.name} ({Rarity.from_value(self.character.rarity_id).label})"
            )
        if self.premium_hours:
            out.append(f"⭐ {self.premium_hours}h premium")
        return out or ["🎟️ a very sincere thank-you"]


class TradeService(Service):
    # ------------------------------------------------------------------ offer
    async def propose(
        self,
        session: AsyncSession,
        *,
        initiator_id: int,
        partner_id: int,
        give: dict[int, int],
        receive: dict[int, int],
        cash: int = 0,
    ) -> TradeView:
        """Lock both sides and open an offer.

        Every character id is validated against the owner's *unlocked* collection
        before anything is written, so a failed proposal never leaves rows locked.
        """
        if initiator_id == partner_id:
            raise Locked("you cannot trade with yourself")
        for owner, items in ((initiator_id, give), (partner_id, receive)):
            if not items and cash == 0:
                raise WaifuError("an offer needs at least one character or some cash")
            locked = await collection_repo.locked_ids(session, owner)
            for character_id, count in items.items():
                if count < 1:
                    raise WaifuError("copy counts must be positive")
                if await collection_repo.has_count(session, owner, character_id) < count:
                    raise NotFound(f"you do not own {count} of character {character_id}")
                if character_id in locked:
                    raise Locked("one of those characters is already locked in another deal")
        if cash and await ledger.balance(session, initiator_id if cash > 0 else partner_id) < abs(
            cash
        ):
            raise Locked("not enough coins for that cash side of the deal")

        offer = await trade_repo.propose(
            session,
            initiator_id=initiator_id,
            partner_id=partner_id,
            initiator_offer=give,
            partner_offer=receive,
            cash=cash,
        )
        for owner, items in ((initiator_id, give), (partner_id, receive)):
            for character_id in items:
                await collection_repo.set_flag(session, owner, character_id, "is_locked", True)
        await mod_repo.audit(
            session,
            actor_id=initiator_id,
            action="trade.propose",
            target=str(offer.id),
            detail=f"partner={partner_id} give={give} get={receive} cash={cash}",
        )
        return await self.view(session, offer)

    async def view(self, session: AsyncSession, offer: TradeOffer) -> TradeView:
        names = await char_repo.get_many(
            session, list({*offer.initiator_offer, *offer.partner_offer})
        )
        giving = [
            (int(cid), names.get(int(cid)).name if int(cid) in names else f"#{cid}", count)
            for cid, count in (offer.initiator_offer or {}).items()
        ]
        receiving = [
            (int(cid), names.get(int(cid)).name if int(cid) in names else f"#{cid}", count)
            for cid, count in (offer.partner_offer or {}).items()
        ]
        left = (
            max(0, int((to_naive_utc(offer.expires_at) - now_utc()).total_seconds()))
            if offer.expires_at
            else 0
        )
        return TradeView(
            id=offer.id,
            code=offer.code or "",
            initiator_id=offer.initiator_id,
            partner_id=offer.partner_id,
            status=offer.status,
            giving=giving,
            receiving=receiving,
            cash=offer.cash,
            expires_in=left,
            initiator_accepted=bool(offer.initiator_accepted),
            partner_accepted=bool(offer.partner_accepted),
        )

    async def get(self, session: AsyncSession, trade_id: int) -> TradeView | None:
        offer = await trade_repo.get(session, trade_id)
        return await self.view(session, offer) if offer else None

    async def by_code(self, session: AsyncSession, code: str) -> TradeView | None:
        offer = await trade_repo.by_code(session, code)
        return await self.view(session, offer) if offer else None

    async def open_for(self, session: AsyncSession, user_id: int) -> list[TradeView]:
        return [
            await self.view(session, offer) for offer in await trade_repo.open_for(session, user_id)
        ]

    # -------------------------------------------------------------- acceptance
    async def set_accept(
        self, session: AsyncSession, trade_id: int, user_id: int, *, accept: bool = True
    ) -> TradeView:
        offer = await trade_repo.set_accept(session, trade_id, user_id, accept)
        return await self.view(session, offer)

    async def confirm_cards(
        self, view: TradeView, *, actor_side: str = "partner"
    ) -> tuple[str, str]:
        """Text for the two ephemeral confirmations (each side sees their own side)."""
        mine = view.giving if actor_side == "initiator" else view.receiving
        theirs = view.receiving if actor_side == "initiator" else view.giving

        def _fmt(items: list[tuple[int, str, int]]) -> str:
            return "\n".join(f"  • {name} ×{count}" for _id, name, count in items) or "  • nothing"

        header = f"<b>Trade #{view.id}</b> · expires in {human_delta(view.expires_in)}\n"
        return (
            header + f"You give:\n{_fmt(mine)}\nYou receive:\n{_fmt(theirs)}",
            f"Terms:\n{truncate(view.summary, 600)}",
        )

    async def send_confirmation(
        self,
        chat_id: int,
        user_id: int,
        text: str,
        *,
        buttons: Any = None,
        callback_query_id: str | None = None,
    ) -> int | None:
        """Private confirmation inside the group (Bot API 10.2 ephemeral message)."""
        message = await send_ephemeral(
            self.bot,
            chat_id,
            receiver_user_id=user_id,
            text=text,
            callback_query_id=callback_query_id,
        )
        if message is None:
            return None
        return message.message_id

    # ----------------------------------------------------------------- execute
    async def execute(self, session: AsyncSession, trade_id: int) -> dict[str, Any]:
        """Complete a fully-accepted trade. Runs once, whoever triggers it.

        ``ready_to_execute`` is the CAS: it flips ACCEPTED→COMPLETED and returns the
        row only for the winner, so a job and a button press cannot both move the
        same characters.
        """
        offer = await trade_repo.ready_to_execute(session, trade_id)
        if offer is None:
            raise Locked("that trade is not ready to complete")
        mover: list[str] = []
        for giver, taker, items in (
            (offer.initiator_id, offer.partner_id, offer.initiator_offer or {}),
            (offer.partner_id, offer.initiator_id, offer.partner_offer or {}),
        ):
            for character_id, count in items.items():
                await collection_repo.consume(
                    session, giver, int(character_id), count=int(count), releasing=True
                )
                await collection_repo.grant(
                    session, taker, int(character_id), count=int(count), source="trade"
                )
                await collection_repo.set_flag(
                    session, giver, int(character_id), "is_locked", False
                )
                mover.append(f"{giver}->{taker}:{character_id}x{count}")
        if offer.cash:
            sender, receiver = (
                (offer.initiator_id, offer.partner_id)
                if offer.cash > 0
                else (offer.partner_id, offer.initiator_id)
            )
            tax = int(abs(offer.cash) * self.settings.gift_tax_percent / 100)
            await ledger.transfer(
                session,
                sender_id=sender,
                receiver_id=receiver,
                amount=abs(offer.cash),
                reason=LedgerReason.TRADE,
                reference=f"trade:{trade_id}",
            )
            if tax:
                await ledger.debit(
                    session, receiver, tax, LedgerReason.TRADE_TAX, reference=f"trade:{trade_id}"
                )
        await mod_repo.audit(
            session,
            actor_id=0,
            action="trade.execute",
            target=str(trade_id),
            detail=f"moves={';'.join(mover)} cash={offer.cash}",
        )
        if self.redis is not None:
            for user_id in (offer.initiator_id, offer.partner_id):
                await self.redis.delete("trade", user_id)
        return {"trade_id": trade_id, "moves": mover, "cash": offer.cash}

    async def cancel(self, session: AsyncSession, trade_id: int, actor_id: int) -> TradeView:
        offer = await trade_repo.cancel(session, trade_id, actor_id)
        for owner, items in (
            (offer.initiator_id, offer.initiator_offer),
            (offer.partner_id, offer.partner_offer),
        ):
            for character_id in items or {}:
                await collection_repo.set_flag(
                    session, owner, int(character_id), "is_locked", False
                )
        await mod_repo.audit(
            session, actor_id=actor_id, action="trade.cancel", target=str(trade_id)
        )
        return await self.view(session, offer)

    async def expire_stale(self, session: AsyncSession) -> int:
        """Release locks on dead offers (the scheduler calls this every minute)."""
        stale = await trade_repo.expire_stale(session)
        for offer in stale:
            for owner, items in (
                (offer.initiator_id, offer.initiator_offer),
                (offer.partner_id, offer.partner_offer),
            ):
                for character_id in items or {}:
                    await collection_repo.set_flag(
                        session, owner, int(character_id), "is_locked", False
                    )
        return len(stale)


class GiftService(Service):
    """Coins and characters sent to another player, with an optional house tax."""

    async def coins(
        self, session: AsyncSession, sender_id: int, receiver_id: int, amount: int
    ) -> dict[str, int]:
        if amount <= 0:
            raise WaifuError("that amount is not spendable")
        tax = int(amount * self.settings.gift_tax_percent / 100)
        await ledger.transfer(
            session,
            sender_id=sender_id,
            receiver_id=receiver_id,
            amount=amount,
            reason=LedgerReason.GIFT,
            reference=f"gift:{sender_id}->{receiver_id}",
            meta={"tax": tax},
        )
        if tax:
            await ledger.debit(
                session,
                receiver_id,
                tax,
                LedgerReason.TRADE_TAX,
                reference=f"gift:tax:{sender_id}->{receiver_id}",
            )
        await trade_repo.log_gift(
            session,
            sender_id=sender_id,
            receiver_id=receiver_id,
            character_id=0,
            note=f"coins:{amount}",
        )
        return {"amount": amount, "tax": tax, "balance": await ledger.balance(session, sender_id)}

    async def character(
        self,
        session: AsyncSession,
        sender_id: int,
        receiver_id: int,
        character_id: int,
        *,
        note: str = "",
    ) -> dict[str, Any]:
        if await collection_repo.has_count(session, sender_id, character_id) < 1:
            raise NotFound("you do not own that character")
        if character_id in await collection_repo.locked_ids(session, sender_id):
            raise Locked("that copy is locked in an auction or trade")
        await collection_repo.consume(session, sender_id, character_id)
        await collection_repo.grant(session, receiver_id, character_id, source="gift")
        await trade_repo.log_gift(
            session,
            sender_id=sender_id,
            receiver_id=receiver_id,
            character_id=character_id,
            note=note,
        )
        character = await char_repo.get(session, character_id)
        return {
            "name": character.name if character else f"#{character_id}",
            "receiver": receiver_id,
        }

    async def history(self, session: AsyncSession, user_id: int) -> dict[str, Any]:
        return await trade_repo.gift_summary(session, user_id)

    async def recent(self, session: AsyncSession, user_id: int, *, limit: int = 8) -> list[Any]:
        return await trade_repo.recent_gifts(session, user_id, limit=limit)


class CodeService(Service):
    """Redeem codes: created by staff, consumed exactly once per player."""

    async def create(
        self,
        session: AsyncSession,
        *,
        created_by: int,
        code: str | None = None,
        coins: int = 0,
        reward: int = 0,
        character_id: int | None = None,
        premium_hours: int = 0,
        uses: int = 1,
        hours: int = 72,
        note: str = "",
    ) -> RedeemCode:
        row = await trade_repo.create(
            session,
            created_by=created_by,
            code=code,
            coins=coins,
            reward=reward,
            character_id=character_id,
            premium_hours=premium_hours,
            uses=uses,
            hours=hours,
            note=note,
        )
        await mod_repo.audit(
            session,
            actor_id=created_by,
            action="code.create",
            target=row.code,
            detail="'coins': coins, 'uses': uses, 'hours': hours",
        )
        return row

    async def list(
        self, session: AsyncSession, *, active_only: bool = True, limit: int = 30
    ) -> list[RedeemCode]:
        return await trade_repo.list_codes(session, active_only=active_only, limit=limit)

    async def revoke(self, session: AsyncSession, code: str, *, actor_id: int) -> None:
        await trade_repo.disable(session, code)
        await mod_repo.audit(session, actor_id=actor_id, action="code.revoke", target=code)

    async def stats(self, session: AsyncSession) -> dict[str, int]:
        return await trade_repo.code_stats(session)

    async def redeem(
        self, session: AsyncSession, user_id: int, raw_code: str, *, premium: bool = False
    ) -> RedeemResult:
        """Consume a code and pay out. One winner per use, guaranteed.

        ``consume_use`` burns the count with ``used_count < uses`` in the WHERE
        clause *and* claims the unique ``code_claims`` row: 500 players redeeming a
        one-use code produce exactly one payout, and a retry by the winner gets
        "already used" instead of a second payout.
        """
        code = await trade_repo.validate(session, raw_code)
        await trade_repo.consume_use(session, code, user_id)
        result = RedeemResult(code=code.code)
        amount = int(code.coins or 0) + int(code.reward or 0)
        if amount:
            await ledger.credit(
                session,
                user_id,
                amount,
                LedgerReason.REDEEM,
                reference=f"code:{code.code}",
                idempotency_key=f"code:{code.code}:{user_id}",
            )
            result.coins = amount
        if code.character_id:
            await collection_repo.grant(session, user_id, code.character_id, source="code")
            result.character = await char_repo.get(session, code.character_id)
        if code.premium_hours:
            await ledger.grant_premium(
                session,
                user_id,
                code.premium_hours,
                granted_by=code.created_by,
                source=f"code:{code.code}",
            )
            result.premium_hours = code.premium_hours
        result.balance = await ledger.balance(session, user_id)
        await mod_repo.audit(
            session,
            actor_id=user_id,
            action="code.redeem",
            target=code.code,
            detail="'coins': amount",
        )
        del premium
        return result

    async def claims(self, session: AsyncSession, code: str) -> list[Any]:
        return await trade_repo.claims_of(session, code)

    async def purge_expired(self, session: AsyncSession) -> int:
        return await trade_repo.purge_expired(session)

    async def user_exists(self, session: AsyncSession, user_id: int) -> bool:
        return await user_repo.get(session, user_id) is not None
