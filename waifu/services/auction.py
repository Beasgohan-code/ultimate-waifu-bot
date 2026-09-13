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

import logging
from dataclasses import dataclass, field
from typing import Any, ClassVar

from sqlalchemy.ext.asyncio import AsyncSession

from waifu.db.models import Auction, Character
from waifu.db.repositories import auctions as auction_repo
from waifu.db.repositories import collection as collection_repo
from waifu.db.repositories import economy as ledger
from waifu.db.repositories import moderation as mod_repo
from waifu.enums import AuctionStatus, ChatMode, LedgerReason, Rarity
from waifu.errors import Locked, NotFound, WaifuError
from waifu.services.base import Service
from waifu.tg.messages import edit_card, send_card
from waifu.tg.rich import RichButton, RichMessageBuilder
from waifu.utils.text import truncate
from waifu.utils.time import human_delta, now_utc, to_naive_utc

log = logging.getLogger(__name__)

log = logging.getLogger(__name__)

log = logging.getLogger(__name__)


def fmt_bid(n: int) -> str:
    """``commands_auction.fmt`` — comma thousands with the separator rendered as a dot.

    Kept because players *type* these numbers back in: the reference's custom-bid parser stripped
    ``.`` and ``,``, so "1.000" meant 1000. A bot that displays one convention and accepts another
    loses bids to arithmetic.
    """
    return f"{int(n):,}".replace(",", ".")


def progress_bar(percent: int) -> str:
    """``commands_auction.progress_bar`` verbatim: ten cells, ``▓`` filled, ``░`` empty."""
    value = max(0, min(100, int(percent)))
    filled = value // 10
    return "▓" * filled + "░" * (10 - filled)


def format_time_left(seconds: float) -> str:
    """``commands_auction.format_time_left`` verbatim: ``1h 02m 05s`` / ``2m 05s``."""
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours > 0:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    return f"{minutes}m {secs:02d}s"


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
    #: The stake the *previous* leader held — the number their refund matches. The plugin used to
    #: print the new high bid here, which told an outbid player they were being paid the winner's
    #: money.
    previous_bid: int = 0
    #: Full span of this listing in seconds, so the progress bar measures the auction the seller
    #: actually started (the reference divided everything by its 1,800-second default, so a 3-hour
    #: auction sat at 100% for the first 25 minutes).
    duration_seconds: int = 0
    live: bool = True
    history: list[dict[str, Any]] = field(default_factory=list)

    @property
    def is_live(self) -> bool:
        return self.live and self.seconds_left > 0 and self.status == str(AuctionStatus.LIVE)

    @property
    def progress(self) -> int:
        span = self.duration_seconds or 1800
        elapsed = max(0, span - self.seconds_left)
        return max(0, min(100, int(elapsed / span * 100)))

    @property
    def bar(self) -> str:
        return progress_bar(self.progress)

    @property
    def clock(self) -> str:
        return format_time_left(self.seconds_left)

    @property
    def ends_in(self) -> str:
        return human_delta(self.seconds_left) if self.seconds_left > 0 else "closed"

    @property
    def meets_reserve(self) -> bool:
        return not self.reserve_price or self.current_bid >= self.reserve_price

    #: ``build_auction_keyboard``'s four quick-bid amounts. They survive verbatim because that is
    #: what players' thumbs expect; a step below this auction's own increment is dropped (it could
    #: not be accepted) and the minimum bid is always offered first, which the reference lacked —
    #: its +500 button silently failed on any auction whose increment was higher.
    BID_STEPS: ClassVar[tuple[int, ...]] = (500, 1000, 5000, 10000)

    def steps(self, min_increment: int = 0) -> list[int]:
        floor = max(1, int(min_increment))
        return [step for step in self.BID_STEPS if step >= floor]


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
        started = getattr(auction, "created_at", None)
        span = 1800
        if started is not None:
            span = max(
                60, int((to_naive_utc(auction.ends_at) - to_naive_utc(started)).total_seconds())
            )
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
            duration_seconds=span,
            live=auction.status == str(AuctionStatus.LIVE),
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
                # ``max(start_price, current + increment)`` — the same floor ``bid`` enforces. It
                # used to read ``a.next_minimum_bid``, a column that has never existed on
                # ``Auction``, so every ``/auctions`` page raised AttributeError.
                max(int(a.start_price or 0), int(a.current_bid or 0) + int(a.min_increment or 0)),
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
        if start_price < self.settings.auction_min_start_price:
            raise WaifuError(
                f"the opening bid has to be at least {self.settings.auction_min_start_price} coins"
            )
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
            minutes=max(
                self.settings.auction_min_minutes,
                min(minutes, self.settings.auction_max_minutes),
            ),
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
        (auction, outbid), extended = await auction_repo.bid(
            session,
            auction_id,
            bidder_id,
            amount,
            snipe_window=int(self.settings.auction_snipe_window),
            snipe_extension=int(self.settings.auction_extend_seconds),
            max_extensions=int(self.settings.auction_max_extensions),
        )
        await ledger.debit(
            session,
            bidder_id,
            amount,
            LedgerReason.BUY,
            reference=f"auction:{auction_id}",
            counterparty=auction.seller_id,
            idempotency_key=f"auc:bid:{auction_id}:{bidder_id}:{amount}",
        )
        # The outbid player's receipt has to quote *their* stake. The reference printed
        # ``highest_bid - add_amount``, which is only right when the previous bid was exactly one
        # button press below the new one; here the number is whatever this call actually released.
        outbid_stake = 0
        for user_id, stake, bid_id in await auction_repo.refund_bids(
            session, auction_id, except_bidder_id=bidder_id
        ):
            outbid_stake = max(outbid_stake, int(stake))
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
        view.previous_bid = int(outbid_stake or 0)
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
    async def settle(self, session: AsyncSession, auction: Auction | int) -> dict[str, Any]:
        """Close one due auction: pay the seller, transfer the character, refund rest.

        Called by the scheduler with one transaction per auction, so a crash settles
        the rest on the next tick instead of half-finishing one. It takes the row *or*
        its id: the job loop reads a candidate (to claim it under ``SKIP LOCKED``) and then
        settles by id, and an earlier version of this signature accepted only the object —
        which meant every scheduled settlement raised ``AttributeError`` on an ``int`` and no
        auction ever closed on its own.

        The returned dict is what the job notifies with, so it carries ``name`` too.
        """
        if not isinstance(auction, Auction):
            row = await session.get(Auction, int(auction))
            if row is None:
                raise NotFound("auction vanished")
            auction = row
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
            await self._retire(auction, sold=False, reason="no bids")
            return {
                "sold": False,
                "winners": [],
                "refunds": len(refunds),
                "name": await self._name(session, auction),
            }
        if auction.reserve_price and auction.current_bid < auction.reserve_price:
            await auction_repo.close(session, auction.id, sold=False)
            await collection_repo.set_flag(
                session, auction.seller_id, auction.character_id, "is_locked", False
            )
            await self._retire(auction, sold=False, reason="reserve not met")
            return {
                "sold": False,
                "reason": "reserve not met",
                "refunds": len(refunds),
                "name": await self._name(session, auction),
            }

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
        await self._retire(auction, sold=True, amount=auction.current_bid)
        return {
            "sold": True,
            "buyer": auction.top_bidder_id,
            "amount": auction.current_bid,
            "fee": fee,
            "refunds": len(refunds),
            "name": await self._name(session, auction),
        }

    async def _name(self, session: AsyncSession, auction: Auction) -> str:
        character = await session.get(Character, auction.character_id)
        return character.name if character else f"#{auction.character_id}"

    async def _retire(
        self, auction: Auction, *, sold: bool, reason: str = "", amount: int = 0
    ) -> None:
        """Unpin the listing and stamp its caption closed.

        The reference left the pinned auction at the top of the chat after it settled, so a busy
        room accumulated ghost listings people kept tapping. Best-effort: an unpin that Telegram
        refuses (no rights in that chat) must not roll back a settled auction.
        """
        if not auction.chat_id or not auction.message_id or self.bot is None:
            return
        chat_id, message_id = int(auction.chat_id), int(auction.message_id)
        line = f"🏆 sold for {fmt_bid(amount)} 🪙" if sold else f"📭 closed — {reason or 'no bids'}"
        try:
            await self.bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id)
        except Exception as exc:
            log.debug("auction %s markup clear failed: %s", auction.id, exc)
        try:
            await self.bot.unpin_chat_message(chat_id=chat_id, message_id=message_id)
        except Exception as exc:
            log.debug("auction %s unpin failed: %s", auction.id, exc)
        log.info("auction %s retired: %s", auction.id, line)

    async def due(self, session: AsyncSession) -> Auction | None:
        return await auction_repo.settle_candidate(session)

    async def stats(self, session: AsyncSession) -> dict[str, int]:
        return await auction_repo.stats(session)

    # ------------------------------------------------------------------- cards
    def card(
        self, view: AuctionView, *, me_id: int | None = None, leader_name: str = ""
    ) -> tuple[RichMessageBuilder, list[RichButton]]:
        """The pinned group post, in the layout ``commands_auction`` trained its players on.

        Same shape as ``build_auction_caption`` — name/anime/rarity block, opening bid, auction id,
        a progress bar with the clock, the high bid, who leads, then the buttons — because a
        re-skinned auction reads as a different game to the people who know where to tap. What is
        added: the reserve line, the fee, the seller's note, the exact minimum next bid, and quick
        steps that respect this auction's own increment.
        """
        live = view.is_live
        builder = RichMessageBuilder().heading("🔨 𝗔 𝗨 𝗖 𝗧 𝗜 𝗢 𝗡", size=1)
        if view.image:
            builder.photo(
                view.image,
                caption=f"🎴 {view.name}\n⛩️ {truncate(view.anime, 40)} · {'⭐' * min(5, view.rarity_id)}",
            )
        builder.line(f"🎴 <b>{view.name}</b> · ⛩️ <i>{truncate(view.anime, 40)}</i>")
        builder.line(
            f"⭐ Rarity: <b>{Rarity.from_value(int(view.rarity_id)).badge}</b> · "
            f"🔖 Auction ID: <code>#{view.id}</code>"
        )
        builder.line(f"💰 Starting bid: <code>{fmt_bid(view.start_price)}</code> 🪙")
        if view.reserve_price:
            builder.line(
                f"🔐 Reserve: <code>{fmt_bid(view.reserve_price)}</code> 🪙 "
                + ("met ✅" if view.meets_reserve else "not met yet")
            )
        if view.note:
            builder.quote(truncate(view.note, 200))
        builder.divider()
        builder.line(f"⏳ <b>Time remaining:</b> {view.bar} {view.clock}")
        builder.line(
            f"🔨 <b>High bid:</b> <code>{fmt_bid(view.current_bid)}</code> 🪙 · "
            f"📈 next: <code>{fmt_bid(view.next_minimum)}</code> 🪙 ({view.bid_count} bids)"
        )
        if view.leader_id:
            builder.line(
                f"👑 Leading: <b>{leader_name or view.leader_id}</b>"
                + (" (that's you)" if view.leader_id == me_id else "")
            )
        else:
            builder.line("👑 Leading: <i>—</i>")
        builder.footer(
            ("seller " + str(view.seller_id) + f" · fee {self.settings.auction_fee_percent}%")
            if live
            else f"status: {view.status}"
        )
        buttons: list[RichButton] = [
            RichButton(
                f"✋ {fmt_bid(view.next_minimum)}",
                callback_data=f"auc:bid:{view.id}" if live else None,
                style="success",
                disabled=not live,
            ),
            RichButton(
                "✏️ Custom bid",
                callback_data=f"auc:custom:{view.id}" if live else None,
                disabled=not live,
            ),
        ]
        steps = view.steps(max(1, int(view.next_minimum - view.current_bid)))
        if live and steps:
            buttons = (
                [
                    RichButton(f"💵 +{fmt_bid(step)}", callback_data=f"auc:bid:{view.id}:{step}")
                    for step in steps[:2]
                ]
                + buttons
                + [
                    RichButton(f"💵 +{fmt_bid(step)}", callback_data=f"auc:bid:{view.id}:{step}")
                    for step in steps[2:4]
                ]
            )
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
            if self.settings.auction_pin_listings and self.bot is not None:
                # ``pin_chat_message(disable_notification=True)`` — the reference pinned every
                # listing so the auction was the room's noticeboard. Silent pin, because a room of
                # 5,000 people does not want a notification for each one.
                try:
                    await self.bot.pin_chat_message(
                        chat_id=chat_id,
                        message_id=result.message.message_id,
                        disable_notification=True,
                    )
                except Exception as exc:
                    log.debug("auction %s pin failed: %s", view.id, exc)
            return result.message.message_id
        return None

    async def refresh_card(
        self,
        chat_id: int,
        view: AuctionView,
        message_id: int,
        *,
        me_id: int | None = None,
        leader_name: str = "",
    ) -> bool:
        builder, buttons = self.card(view, me_id=me_id, leader_name=leader_name)
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
