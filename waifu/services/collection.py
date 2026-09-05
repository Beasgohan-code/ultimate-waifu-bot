"""Collection service — /collection, /harem, /check, /fav, /sell, /search.

Rendering-agnostic: the service returns :class:`collection_repo.Owned` rows plus a
few aggregates, and :mod:`waifu.ui` decides whether that becomes a rich collage, an
album or a paged text list.

Selling rules that Summon-bot got wrong and players noticed within a week:

* the **favourite** copy and any **locked** copy (auction/trade escrow) are
  unsellable, enforced here rather than in the UI;
* selling a dupe pays ``dupe_payout_percent`` of the current market price (computed
  from ``characters.price`` + rarity floor), and the payout is a ledger row, not a
  bare ``UPDATE balance``;
* selling the last copy deletes nothing — ``Ownership`` keeps a ``count`` column, so
  a re-pull is a count bump and the history stays intact.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from waifu.db.models import Character
from waifu.db.repositories import characters as char_repo
from waifu.db.repositories import collection as collection_repo
from waifu.db.repositories import economy as ledger
from waifu.db.repositories import items as items_repo
from waifu.db.repositories import users as user_repo
from waifu.enums import LedgerReason
from waifu.errors import Locked, NotFound
from waifu.services.base import Service
from waifu.utils.text import truncate


@dataclass(slots=True)
class CollectionPage:
    items: list[collection_repo.Owned] = field(default_factory=list)
    page: int = 0
    pages: int = 1
    total: int = 0
    value: int = 0
    per_rarity: dict[int, int] = field(default_factory=dict)
    mode: str = "rarity"
    query: str = ""

    @property
    def highest(self) -> collection_repo.Owned | None:
        return max(self.items, key=lambda o: (o.rarity_id, o.stat_power), default=None)


@dataclass(slots=True)
class SellResult:
    character_id: int
    name: str
    count_sold: int
    payout: int
    balance: int
    remaining: int
    price: int


class CollectionService(Service):
    # ------------------------------------------------------------------- lists
    async def page(
        self,
        session: AsyncSession,
        user_id: int,
        *,
        mode: str = "rarity",
        rarity_id: int | None = None,
        query: str = "",
        dupes_only: bool = False,
        page: int = 0,
        page_size: int = 8,
    ) -> CollectionPage:
        items, total = await collection_repo.list_owned(
            session,
            user_id,
            hmode=mode,
            rarity_id=rarity_id,
            query=query,
            dupes_only=dupes_only,
            page_size=page_size,
            page=page,
        )
        return CollectionPage(
            items=items,
            page=page + 1,
            pages=max(1, -(-total // page_size)),
            total=total,
            value=await collection_repo.collection_value(session, user_id),
            per_rarity=await collection_repo.per_rarity(session, user_id),
            mode=mode,
            query=query,
        )

    async def summary(self, session: AsyncSession, user_id: int) -> dict[str, int]:
        return await collection_repo.summary(session, user_id)

    async def per_rarity(self, session: AsyncSession, user_id: int) -> dict[int, int]:
        return await collection_repo.per_rarity(session, user_id)

    async def favourite(self, session: AsyncSession, user_id: int) -> collection_repo.Owned | None:
        return await collection_repo.favourite(session, user_id)

    async def best(self, session: AsyncSession, user_id: int) -> collection_repo.Owned | None:
        return await collection_repo.best(session, user_id)

    async def owned_row(
        self, session: AsyncSession, user_id: int, character_id: int
    ) -> collection_repo.Owned | None:
        return await collection_repo.owned_row(session, user_id, character_id)

    async def sellable(
        self, session: AsyncSession, user_id: int, *, rarity_id: int | None = None, limit: int = 40
    ) -> list[collection_repo.Owned]:
        return await collection_repo.sellable(session, user_id, rarity_id=rarity_id, limit=limit)

    # ------------------------------------------------------------------- flags
    async def set_favourite(
        self, session: AsyncSession, user_id: int, character_id: int, *, value: bool = True
    ) -> str:
        if value and await collection_repo.has_count(session, user_id, character_id) < 1:
            raise NotFound("you do not own that character")
        await collection_repo.set_flag(session, user_id, character_id, "is_favorite", value)
        character = await char_repo.get(session, character_id)
        return character.name if character else f"#{character_id}"

    async def set_lock(
        self, session: AsyncSession, user_id: int, character_id: int, *, value: bool = True
    ) -> str:
        if value and await collection_repo.has_count(session, user_id, character_id) < 1:
            raise NotFound("you do not own that character")
        await collection_repo.set_flag(session, user_id, character_id, "is_locked", value)
        character = await char_repo.get(session, character_id)
        return character.name if character else f"#{character_id}"

    # -------------------------------------------------------------------- sell
    async def sell(
        self, session: AsyncSession, user_id: int, character_id: int, *, count: int = 1
    ) -> SellResult:
        owned = await collection_repo.has_count(session, user_id, character_id)
        if owned < count:
            raise NotFound(f"you have {owned}, not {count}")
        row = await collection_repo.owned_row(session, user_id, character_id)
        if row is None:
            raise NotFound("you do not own that character")
        if row.is_locked:
            raise Locked("that copy is locked (auction or trade)")
        if row.is_favorite and count >= owned:
            raise Locked("that is your favourite — /fav off first")
        character = await char_repo.get(session, character_id)
        if character is None:
            raise NotFound("that character no longer exists")
        price = await char_repo.price_for(session, character)
        # Selling a spare pays the dupe share; selling your *last* copy pays full
        # price, because that is the character leaving the account. Inverting this is
        # what makes dupe-farming profitable (and it was profitable upstream).
        rate = self.settings.dupe_payout_percent / 100 if count < owned else 1.0
        payout = max(1, int(price * rate)) * count
        await collection_repo.consume(session, user_id, character_id, count=count)
        await ledger.credit(
            session, user_id, payout, LedgerReason.SELL, reference=f"sell:{character_id}:{count}"
        )
        await items_repo.log_heist(
            session,
            attacker_id=user_id,
            target_id=user_id,
            kind="sell",
            outcome="ok",
            amount=payout,
            character_id=character_id,
        )
        if self.redis is not None:
            await self.redis.zincr("lb:coins", str(user_id), float(payout))
        return SellResult(
            character_id=character_id,
            name=character.name,
            count_sold=count,
            payout=payout,
            balance=await ledger.balance(session, user_id),
            remaining=max(0, owned - count),
            price=int(price),
        )

    # ------------------------------------------------------------------ search
    async def search(
        self,
        session: AsyncSession,
        query: str,
        *,
        rarity_id: int | None = None,
        anime: str | None = None,
        limit: int = 12,
        page: int = 0,
    ) -> tuple[list[Character], int]:
        return await char_repo.search(
            session, query, rarity_id=rarity_id, anime=anime, limit=limit, offset=page * limit
        )

    async def find(
        self, session: AsyncSession, query: str, *, rarity_id: int | None = None
    ) -> Character:
        return await char_repo.find_one(session, query, rarity_id=rarity_id)

    async def character(self, session: AsyncSession, character_id: int) -> Character | None:
        return await char_repo.get(session, character_id)

    async def holders(
        self, session: AsyncSession, character_id: int, *, limit: int = 25
    ) -> list[tuple[Any, int]]:
        return await collection_repo.holders_of(session, character_id, limit=limit)

    async def top_collectors(
        self, session: AsyncSession, *, limit: int = 15
    ) -> list[tuple[Any, int]]:
        return await collection_repo.top_collectors(session, limit=limit)

    async def price(self, session: AsyncSession, character: Character) -> int:
        return await char_repo.price_for(session, character)

    # --------------------------------------------------------- profile display
    async def profile(self, session: AsyncSession, user_id: int) -> dict[str, Any]:
        """One round-trip bundle for /profile and the Mini App's player sheet."""
        user = await user_repo.get(session, user_id)
        if user is None:
            raise NotFound("player not registered")
        summary = await collection_repo.summary(session, user_id)
        favourite = await collection_repo.favourite(session, user_id)
        prefs = await user_repo.prefs(session, user_id)
        flags = prefs.flags or {}
        return {
            "id": user.id,
            "username": user.username,
            "display": (
                f"@{user.username}" if user.username else (user.first_name or str(user.id))
            ),
            "balance": user.balance,
            "exp": user.exp,
            "level": user.level,
            "role": user.role,
            "premium_hours_left": await ledger.premium_left_hours(session, user_id),
            "streak": user.streak_count,
            "streak_best": user.streak_best,
            "pulls": user.pulls_total,
            "high_pulls": user.high_pulls,
            "warnings": user.warn_count,
            "collection": summary,
            "per_rarity": await collection_repo.per_rarity(session, user_id),
            "favourite": favourite.name if favourite else "",
            "favourite_image": favourite.image if favourite else "",
            "locale": user.locale,
            "mode": prefs.hmode,
            "font": prefs.font,
            "glow": prefs.glow,
            "show_balance": prefs.show_balance,
            "coins_rank": (await user_repo.rank_of(session, user_id, metric="balance"))[0],
            "claim_rank": (await user_repo.rank_of(session, user_id, metric="claims"))[0],
            "seed_commitment": user.seed_commitment,
            "bio": truncate(str(flags.get("bio") or ""), 240),
            "joined": user.created_at,
            "invite_code": user.invite_code,
        }
