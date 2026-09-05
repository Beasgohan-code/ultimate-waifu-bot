"""Auction service — escrowed bids, anti-snipe extension, atomic settlement.

Money flow, which is where auction bots break:

* listing locks the ownership row (``Ownership.is_locked``) — the seller can't sell
  or gift an item that is on auction;
* a bid moves the stake out of the bidder's balance immediately and refunds the
  previous leader in the **same transaction**, so no one is ever double-paid and no
  balance is ever negative (the DB has ``CHECK (balance >= 0)`` as the backstop);
* the *last* bid's stake stays in the ledger as a debit until settlement either pays
  the seller (minus fee) or refunds everybody;
* settlement runs under ``FOR UPDATE SKIP LOCKED`` so two workers can never settle
  the same auction (Summon-bot settled twice during a restart and paid two winners).

Anti-snipe: a bid inside ``auction_snipe_window`` seconds of close extends
``ends_at`` by ``auction_extend_seconds``, up to ``auction_max_extensions`` times —
without it, bot-scripted snipers win every auction on a 5k-member server.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from waifu.db.models import Auction, Character
from waifu.db.repositories import auctions as auction_repo
from waifu.db.repositories import collection as collection_repo
from waifu.db.repositories import economy as ledger
from waifu.db.repositories import moderation as mod_repo
from waifu.enums import ChatMode, LedgerReason
from waifu.errors import Locked, NotFound, WaifuError
from waifu.services.base import Service
from waifu.tg.messages import edit_card, send_card
from waifu.tg.rich import RichButton, RichMessageBuilder
from waifu.utils.text import truncate
from waifu.utils.time import human_delta, now_utc, to_naive_utc


@dataclass(slots=True)
class AuctionView:
    id: int
    seller_id: int
    character_id: int
    name: str
    anime: str
    rarity_id: int
    image: str
    start_price: int
    current_bid: int
    bid_count: int
    leader_id: int | None
    next_minimum: int
    ends_at: Any
    seconds_left: int
    status: str
    reserve_price: int = 0
    note: str = ""
    message_id: int | None = None
    chat_id: int | None = None
    extended: bool = False
    history: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ends_in(self) -> str:
        return human_delta(self.seconds_left) if self.seconds_left > 0 else "closed"

    @property
    def meets_reserve(self) -> bool:
        return not self.reserve_price or self.current_bid >= self.reserve_price


class AuctionService(Service):
    # ------------------------------------------------------------------ views
    async def view(self, session: AsyncSession, auction_id: int) -> AuctionView:
        triple = await auction_repo.view(session, auction_id)
        if triple is None:
            raise NotFound("no such auction")
        auction, character, current_bid, bid_count = triple
        return self._view(
            auction,
            character,
            current_bid,
            bid_count,
            await auction_repo.next_minimum(session, auction_id),
        )

    def _view(
        self,
        auction: Auction,
        character: Character | None,
        current_bid: int,
        bid_count: int,
        next_minimum: int,
    ) -> AuctionView:
        left = max(0, int((to_naive_utc(auction.ends_at) - now_utc()).total_seconds()))
        return AuctionView(
            id=auction.id,
            seller_id=auction.seller_id,
            character_id=auction.character_id,
            name=character.name if character else "unknown",
            anime=(character.anime or "") if character else "",
            rarity_id=int(character.rarity_id) if character else 1,
            image=character.image_ref() if character else "",
            start_price=auction.start_price,
            current_bid=current_bid,
            bid_count=bid_count,
            leader_id=auction.top_bidder_id,
            next_minimum=next_minimum,
            ends_at=auction.ends_at,
            seconds_left=left,
            status=auction.status,
            reserve_price=auction.reserve_price,
            note=auction.note or "",
            message_id=auction.message_id,
            chat_id=auction.chat_id,
        )

    async def live(
        self, session: AsyncSession, *, limit: int = 10, offset: int = 0, sort: str = "ends"
    ) -> tuple[list[AuctionView], int]:
        rows, total, chars = await auction_repo.live(session, limit=limit, offset=offset, sort=sort)
        views = [
            self._view(
                a,
                chars.get(a.character_id),
                a.current_bid,
                a.bids_count,
                a.next_minimum_bid or a.current_bid,
            )
            for a in rows
        ]
        return views, total

    async def mine(
        self, session: AsyncSession, user_id: int, *, as_seller: bool = True, limit: int = 10
    ) -> list[Auction]:
        return await auction_repo.mine(session, user_id, as_seller=as_seller, limit=limit)

    async def leading(self, session: AsyncSession, user_id: int) -> list[Auction]:
        return await auction_repo.leading(session, user_id)

    async def history(
        self, session: AsyncSession, user_id: int, *, limit: int = 10
    ) -> list[Auction]:
        return await auction_repo.history_of(session, user_id, limit=limit)

    async def bid_log(
        self, session: AsyncSession, auction_id: int, *, limit: int = 12
    ) -> list[Any]:
        return await auction_repo.bid_log(session, auction_id, limit=limit)

    # ---------------------------------------------------------------- listing
    async def create(
        self,
        session: AsyncSession,
        *,
        seller_id: int,
        character_id: int,
        start_price: int,
        minutes: int = 60,
        reserve_price: int = 0,
        min_increment: int = 1000,
        note: str = "",
        chat_id: int | None = None,
    ) -> AuctionView:
        if start_price < 1:
            raise WaifuError("an auction needs a start price above zero")
        owned = await collection_repo.has_count(session, seller_id, character_id)
        if owned < 1:
            raise NotFound("you do not own that character")
        locked = await collection_repo.locked_ids(session, seller_id)
        if character_id in locked:
            raise Locked("that copy is already locked (in an auction or trade)")
        auction = await auction_repo.create(
            session,
            seller_id=seller_id,
            character_id=character_id,
            start_price=start_price,
            minutes=max(5, min(minutes, 7 * 24 * 60)),
            reserve_price=reserve_price,
            min_increment=min_increment,
            note=note,
        )
        await collection_repo.set_flag(session, seller_id, character_id, "is_locked", True)
        await mod_repo.audit(
            session,
            actor_id=seller_id,
            action="auction.create",
            target=str(auction.id),
            detail=f"character={character_id} start={start_price} reserve={reserve_price} minutes={minutes}",
        )
        character = await session.get(Character, character_id)
        view = self._view(auction, character, start_price, 0, start_price + min_increment)
        view.chat_id = chat_id
        return view

    async def cancel(
        self, session: AsyncSession, auction_id: int, actor_id: int, *, force: bool = False
    ) -> tuple[Auction, int]:
        """Cancel a listing and return every stake. ``(auction, refund_count)``."""
        auction = await auction_repo.cancel(session, auction_id, actor_id, force=force)
        refunds = await auction_repo.refund_bids(session, auction_id)
        for user_id, amount, bid_id in refunds:
            await ledger.credit(
                session,
                user_id,
                amount,
                LedgerReason.REFUND,
                reference=f"auction:{auction_id}",
                idempotency_key=f"auc:refund:{bid_id}",
                meta={"reason": "cancelled"},
            )
        await collection_repo.set_flag(
            session, auction.seller_id, auction.character_id, "is_locked", False
        )
        await mod_repo.audit(
            session,
            actor_id=actor_id,
            action="auction.cancel",
            target=str(auction_id),
            detail=f"refunds={len(refunds)}",
        )
        return auction, len(refunds)

    # ------------------------------------------------------------------- bids
    async def bid(
        self, session: AsyncSession, auction_id: int, bidder_id: int, amount: int
    ) -> tuple[AuctionView, int | None, bool]:
        """Place a bid: debit the stake, refund every superseded one, extend the clock.

        The debit carries an idempotency key built from the auction, bidder and
        amount, so a re-sent button press cannot bid twice; refunds are keyed by the
        *bid row* so no stake can be paid back twice either.
        """
        (auction, outbid), extended = await auction_repo.bid(session, auction_id, bidder_id, amount)
        await ledger.debit(
            session,
            bidder_id,
            amount,
            LedgerReason.BUY,
            reference=f"auction:{auction_id}",
            counterparty=auction.seller_id,
            idempotency_key=f"auc:bid:{auction_id}:{bidder_id}:{amount}",
        )
        for user_id, stake, bid_id in await auction_repo.refund_bids(
            session, auction_id, except_bidder_id=bidder_id
        ):
            await ledger.credit(
                session,
                user_id,
                stake,
                LedgerReason.REFUND,
                reference=f"auction:{auction_id}",
                idempotency_key=f"auc:refund:{bid_id}",
                meta={"reason": "outbid"},
            )
        character = await session.get(Character, auction.character_id)
        # ``bids_count`` moved inside a Core UPDATE, so the ORM copy is one behind.
        await session.refresh(auction)
        view = self._view(
            auction,
            character,
            auction.current_bid,
            auction.bids_count,
            await auction_repo.next_minimum(session, auction_id),
        )
        view.extended = extended
        return view, outbid, extended

    async def refund_outbid(
        self, session: AsyncSession, auction_id: int, user_id: int, amount: int
    ) -> None:
        await ledger.credit(
            session,
            user_id,
            amount,
            LedgerReason.REFUND,
            reference=f"auction:{auction_id}:outbid:{user_id}",
            idempotency_key=f"auc:outbid:{auction_id}:{user_id}:{amount}",
        )

    # -------------------------------------------------------------- settlement
    async def settle(self, session: AsyncSession, auction: Auction) -> dict[str, Any]:
        """Close one due auction: pay the seller, transfer the character, refund rest.

        Called by the scheduler with one transaction per auction, so a crash settles
        the rest on the next tick instead of half-finishing one.
        """
        refunds = await auction_repo.refund_bids(
            session, auction.id, except_bidder_id=auction.top_bidder_id
        )
        for user_id, amount, bid_id in refunds:
            await ledger.credit(
                session,
                user_id,
                amount,
                LedgerReason.REFUND,
                reference=f"auction:{auction.id}",
                idempotency_key=f"auc:refund:{bid_id}",
            )
        if auction.top_bidder_id is None or auction.current_bid < auction.start_price:
            await auction_repo.close(session, auction.id, sold=False)
            await collection_repo.set_flag(
                session, auction.seller_id, auction.character_id, "is_locked", False
            )
            return {"sold": False, "winners": [], "refunds": len(refunds)}
        if auction.reserve_price and auction.current_bid < auction.reserve_price:
            await auction_repo.close(session, auction.id, sold=False)
            await collection_repo.set_flag(
                session, auction.seller_id, auction.character_id, "is_locked", False
            )
            return {"sold": False, "reason": "reserve not met", "refunds": len(refunds)}

        fee = int(auction.current_bid * self.settings.auction_fee_percent / 100)
        await ledger.credit(
            session,
            auction.seller_id,
            auction.current_bid - fee,
            LedgerReason.SELL,
            reference=f"auction:{auction.id}",
            counterparty=auction.top_bidder_id,
        )
        # releasing=True: the escrow itself put the lock on this row.
        await collection_repo.consume(
            session, auction.seller_id, auction.character_id, count=1, releasing=True
        )
        await collection_repo.grant(
            session, auction.top_bidder_id, auction.character_id, source="auction"
        )
        await auction_repo.close(session, auction.id, sold=True, fee=fee)
        await mod_repo.audit(
            session,
            actor_id=0,
            action="auction.settle",
            target=str(auction.id),
            detail=f"buyer={auction.top_bidder_id} amount={auction.current_bid} fee={fee}",
        )
        return {
            "sold": True,
            "buyer": auction.top_bidder_id,
            "amount": auction.current_bid,
            "fee": fee,
            "refunds": len(refunds),
        }

    async def due(self, session: AsyncSession) -> Auction | None:
        return await auction_repo.settle_candidate(session)

    async def stats(self, session: AsyncSession) -> dict[str, int]:
        return await auction_repo.stats(session)

    # ------------------------------------------------------------------- cards
    def card(
        self, view: AuctionView, *, me_id: int | None = None
    ) -> tuple[RichMessageBuilder, list[RichButton]]:
        builder = RichMessageBuilder().heading(f"🔨 {view.name}")
        if view.image:
            builder.photo(view.image, caption=f"{view.anime} · {'⭐' * min(5, view.rarity_id)}")
        builder.table(
            [
                ["Now", f"{view.current_bid:,} 🪙".replace(",", "\u2009")],
                ["Min next", f"{view.next_minimum:,} 🪙".replace(",", "\u2009")],
                ["Bids", str(view.bid_count)],
                ["Ends in", view.ends_in],
            ],
            compact=True,
        )
        if view.note:
            builder.paragraph(truncate(view.note, 200))
        if view.leader_id == me_id:
            builder.footer("you are currently winning")
        elif view.seconds_left:
            builder.footer(f"seller {view.seller_id} · fee {self.settings.auction_fee_percent}%")
        else:
            builder.footer(view.status)
        live = view.status == "live" and view.seconds_left > 0
        buttons = [
            RichButton(
                "Bid minimum",
                callback_data=f"aucb:{view.id}:{view.next_minimum}" if live else None,
                style="success",
                disabled=not live,
            ),
            RichButton(
                "+10%", callback_data=f"aucp:{view.id}" if live else None, disabled=not live
            ),
            RichButton(
                "Watch", callback_data=f"aucw:{view.id}" if live else None, disabled=not live
            ),
        ]
        return builder, buttons

    async def publish(
        self, chat_id: int, view: AuctionView, *, thread_id: int | None = None
    ) -> int | None:
        builder, buttons = self.card(view)
        result = await send_card(
            self.bot,
            chat_id,
            builder=builder,
            caption=builder.fallback_html(limit=1000),
            photo=view.image or None,
            buttons=[[callback_button(b) for b in buttons]],
            mode=ChatMode.RICH if self.ctx.caps.rich_messages else ChatMode.PLAIN,
            message_thread_id=thread_id,
            rich_buttons=buttons,
        )
        if result.ok and result.message is not None:
            async with self.ctx.db.tx() as session:
                await auction_repo.attach_message(
                    session, view.id, result.message.message_id, chat_id
                )
            return result.message.message_id
        return None

    async def refresh_card(
        self, chat_id: int, view: AuctionView, message_id: int, *, me_id: int | None = None
    ) -> bool:
        builder, buttons = self.card(view, me_id=me_id)
        from waifu.tg.buttons import markup

        rows = [[callback_button(b) for b in buttons]]
        updated = await edit_card(
            self.bot,
            chat_id,
            message_id,
            builder=builder,
            caption=builder.fallback_html(limit=1000),
            markup=markup(rows),
            mode=ChatMode.RICH if self.ctx.caps.rich_messages else ChatMode.PLAIN,
            photo=view.image or None,
        )
        return updated is not None


def callback_button(button: RichButton) -> Any:
    """Mirror a rich button into an inline keyboard (the two must never diverge)."""
    from waifu.tg.buttons import callback

    return callback(
        button.text, button.callback_data or "noop", style=button.style, disabled=button.disabled
    )
