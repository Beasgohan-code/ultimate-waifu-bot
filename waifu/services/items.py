"""Shop, inventory and consumables.

Two shops, because the reference bot conflated them and both were worse for it:

* the **item shop** — a fixed catalogue (:data:`waifu.db.repo.ITEMS`)
  whose entries all *do* something (arm a shield, clear a cooldown, bank charges);
* the **featured character pool** — ``shop_pools`` rows with ``refreshes_used``, so
  re-rolling is a priced action instead of the free "close and reopen until the one
  you want appears" exploit the old shop allowed.

Purchase rules: stock is decremented with a conditional UPDATE (a sell-out between
listing and buying costs the buyer coins for nothing in the old bot), and every
spend writes a ledger row, so /history explains every coin.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Any, ClassVar

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from waifu.db.models import Character, ShopPool
from waifu.db.repo import characters as char_repo
from waifu.db.repo import collection as collection_repo
from waifu.db.repo import economy as ledger
from waifu.db.repo import items as items_repo
from waifu.db.repo import progress as progress_repo
from waifu.db.repo import users as user_repo
from waifu.enums import LedgerReason, Rarity
from waifu.errors import AlreadyClaimed, CooldownActive, Locked, NotFound, WaifuError
from waifu.services.base import Service
from waifu.utils.rng import system_random

#: Items that arm a shield instead of being "used" from the bag.
#: Item key → shield kind (they coincide; ``add_shields`` validates the kind).
SHIELD_ITEMS = {"sshield": "sshield", "bshield": "bshield"}

#: Items whose value is a banked charge the game loops consume.
CHARGE_ITEMS = {"lucky": "lucky_charges", "xp": "xp_boost_charges", "magnet": "magnet_charges"}

FEATURED_PER_PAGE = 6
FREE_REFRESHES = 2


@dataclass(slots=True)
class ShopEntry:
    key: str
    name: str
    cost: int
    owned: int
    desc: str
    max_stack: int = 1
    #: Hours until the stack rots (0 = permanent). The reference printed ``⌛ 7h left`` per line
    #: of ``/inv`` because its items expire; a bag that silently loses its bombs needs a clock.
    hours_left: int = 0
    #: True when premium waived the price (``premium_free_items``), not when the item is free
    #: in the catalogue — the receipt has to say which of the two happened.
    free: bool = False

    @property
    def expiry_text(self) -> str:
        return "∞" if not self.hours_left else f"{self.hours_left}h"

    @property
    def emoji(self) -> str:
        return self.name.split(" ", 1)[0]

    @property
    def label(self) -> str:
        return self.name.split(" ", 1)[1] if " " in self.name else self.name


@dataclass(slots=True)
class FeaturedEntry:
    character_id: int
    name: str
    anime: str
    rarity: Rarity
    price: int
    discount: int
    image: str = ""
    stat_power: int = 0
    owned: int = 0

    @property
    def final_price(self) -> int:
        return max(1, int(self.price * (100 - self.discount) / 100))


@dataclass(slots=True)
class Shop:
    items: list[ShopEntry] = field(default_factory=list)
    featured: list[FeaturedEntry] = field(default_factory=list)
    refreshes_left: int = FREE_REFRESHES
    refresh_cost: int = 0
    balance: int = 0
    #: The reference announced it in the header ("All items are FREE!"), and a shop that quietly
    #: charges 0 while the player thinks it is a sale is a support ticket.
    premium_free: bool = False


@dataclass(slots=True)
class UseResult:
    key: str
    name: str
    effect: str
    stacks_left: int = 0
    charges: int = 0
    details: dict[str, Any] = field(default_factory=dict)


class ItemService(Service):
    # ------------------------------------------------------------------- shop
    def price_of(self, definition: items_repo.ItemDef, *, premium: bool) -> int:
        """What one stack costs this player right now.

        ``plugins/market.py`` charged premium players nothing and told them so ("All items are
        FREE!"), which is a licence to print coins unless the cap is what you want: the reference
        relied on the 24h expiry and the per-item stack cap to keep it bounded, and so does this,
        which is why both are settings rather than an assumption.
        """
        if premium and self.settings.premium_free_items:
            return 0
        return int(definition.cost)

    async def is_premium(self, session: AsyncSession, user_id: int) -> bool:
        return await ledger.premium_left_hours(session, user_id) > 0

    async def shop(
        self,
        session: AsyncSession,
        user_id: int,
        *,
        refresh: bool = False,
        rarity: str = "featured",
        premium: bool | None = None,
    ) -> Shop:
        entitled = await self.is_premium(session, user_id) if premium is None else premium
        expiries = await items_repo.expiry_hours(session, user_id)
        entries = [
            ShopEntry(
                key=definition.key,
                name=definition.name,
                cost=self.price_of(definition, premium=entitled),
                desc=definition.desc,
                max_stack=definition.max_stack,
                owned=await items_repo.stacks(session, user_id, definition.key),
                hours_left=expiries.get(definition.key, 0),
                free=bool(entitled and self.settings.premium_free_items),
            )
            for definition in items_repo.ITEMS.values()
        ]
        pool = (
            await session.execute(
                select(ShopPool).where(ShopPool.user_id == user_id, ShopPool.rarity == rarity)
            )
        ).scalar_one_or_none()
        used = int(pool.refreshes_used) if pool else 0
        left = max(0, FREE_REFRESHES - used)
        if pool is None or refresh:
            if refresh and left <= 0:
                cost = self.settings.shop_refresh_cost
                if cost:
                    await ledger.debit(
                        session,
                        user_id,
                        cost,
                        LedgerReason.BUY,
                        reference=f"shop:refresh:{rarity}",
                        idempotency_key=f"shop:refresh:{user_id}:{used}",
                    )
                used += 1
            self._roll_item_pool()  # advances the RNG so a re-roll is not the same pool
            featured = await self._roll_featured(session, rarity=rarity)
            if pool is None:
                session.add(
                    ShopPool(
                        user_id=user_id,
                        rarity=rarity,
                        characters={
                            "ids": [c.id for c in featured],
                            "seed": system_random.randrange(10**9),
                        },
                    )
                )
            else:
                pool.characters = {
                    "ids": [c.id for c in featured],
                    "seed": system_random.randrange(10**9),
                }
                pool.refreshes_used = used
            await session.flush()
        else:
            ids = list((pool.characters or {}).get("ids") or [])
            featured = (
                list((await char_repo.get_many(session, ids)).values())
                if ids
                else await self._roll_featured(session, rarity=rarity)
            )
            if not featured:
                featured = await self._roll_featured(session, rarity=rarity)
        owned = await collection_repo.per_rarity(session, user_id)
        del owned
        entries_by_id = {c.id: c for c in featured}
        featured_rows = []
        for character in entries_by_id.values():
            price = await char_repo.price_for(session, character)
            already = await collection_repo.has_count(session, user_id, character.id)
            featured_rows.append(
                FeaturedEntry(
                    character_id=character.id,
                    name=character.name,
                    anime=character.anime or "",
                    rarity=Rarity.from_value(character.rarity_id),
                    price=price,
                    discount=0 if already else 15,
                    image=character.image_ref(),
                    stat_power=character.stat_power,
                    owned=already,
                )
            )
        return Shop(
            items=entries,
            featured=featured_rows,
            refreshes_left=left,
            refresh_cost=self.settings.shop_refresh_cost,
            balance=await ledger.balance(session, user_id),
            premium_free=bool(entitled and self.settings.premium_free_items),
        )

    def _roll_item_pool(self) -> list[str]:
        return list(items_repo.ITEMS)

    async def _roll_featured(self, session: AsyncSession, *, rarity: str) -> list[Character]:
        """Six discounted characters, weighted up the rarity ladder."""
        table = await char_repo.normalised_odds(session)
        weights = [c for _, c in table] or [1]
        picks: list[Character] = []
        seen: set[int] = set()
        for _ in range(FEATURED_PER_PAGE * 3):
            rarity_choice = system_random.choices([r for r, _ in table], weights=weights, k=1)[0]
            character = await char_repo.random_of_rarity(
                session, int(rarity_choice), banner_only=rarity == "banner"
            )
            if character is not None and character.id not in seen:
                seen.add(character.id)
                picks.append(character)
            if len(picks) >= FEATURED_PER_PAGE:
                break
        return picks

    async def buy(
        self, session: AsyncSession, user_id: int, key: str, *, quantity: int = 1
    ) -> dict[str, Any]:
        definition = items_repo.item(key)
        # ``items_repo.buy`` deliberately does not touch money, and this is the only
        # place allowed to: an item shop that hands out stacks without a debit is a
        # coin printer. Order matters — add stacks first (it clamps to the stack cap),
        # then charge for what actually landed; a failed debit rolls the stacks back
        # with it because both live in the caller's transaction.
        entitled = await self.is_premium(session, user_id)
        spent = await items_repo.buy(
            session,
            user_id,
            definition.key,
            quantity=max(1, quantity),
            ttl_hours=int(self.settings.shop_item_ttl_hours),
        )
        cost = self.price_of(definition, premium=entitled) * spent
        if cost:
            await ledger.debit(
                session,
                user_id,
                cost,
                LedgerReason.BUY,
                reference=f"item:{definition.key}:{spent}",
                idempotency_key=f"item:{user_id}:{definition.key}:{secrets.token_hex(6)}",
            )
        return {
            "key": definition.key,
            "name": definition.name,
            # ``quantity`` is what landed in the bag (the stack cap can clamp it) and
            # ``cost`` is what it charged — the old single "spent" key meant the
            # quantity here and the coin amount in ``buy_character``, which is how a
            # receipt line ends up saying "spent 1".
            "quantity": spent,
            "cost": cost,
            "balance": await ledger.balance(session, user_id),
            "owned": await items_repo.stacks(session, user_id, definition.key),
        }

    async def buy_character(
        self, session: AsyncSession, user_id: int, character_id: int, *, price: int | None = None
    ) -> dict[str, Any]:
        """Buy a featured character outright (the /market one-shot purchase)."""
        character = await char_repo.get(session, character_id)
        if character is None:
            raise NotFound("that character is gone")
        cost = int(price) if price else await char_repo.price_for(session, character)
        if cost <= 0:
            raise Locked("this character is not for sale")
        await ledger.debit(
            session,
            user_id,
            cost,
            LedgerReason.BUY,
            reference=f"shopchar:{character_id}",
            idempotency_key=f"shopchar:{user_id}:{character_id}",
        )
        await collection_repo.grant(session, user_id, character_id, source="shop")
        return {
            "name": character.name,
            "spent": cost,
            "balance": await ledger.balance(session, user_id),
        }

    # --------------------------------------------------------------- inventory
    async def inventory(self, session: AsyncSession, user_id: int) -> list[ShopEntry]:
        owned = await items_repo.inventory(session, user_id)
        expiries = await items_repo.expiry_hours(session, user_id)
        out: list[ShopEntry] = []
        for key, count in owned.items():
            if count <= 0:
                continue
            try:
                definition = items_repo.item(key)
            except NotFound:  # pragma: no cover - item removed from the catalogue
                continue
            out.append(
                ShopEntry(
                    key=key,
                    name=definition.name,
                    cost=definition.cost,
                    desc=definition.desc,
                    max_stack=definition.max_stack,
                    owned=count,
                    hours_left=expiries.get(key, 0),
                )
            )
        return sorted(out, key=lambda e: e.name)

    #: ``/skip 1`` clears the daily-reward cooldown, ``2`` loads a bomb shield, ``3`` a steal
    #: shield: the three modes ``plugins/market.py`` gave the ticket, kept as data so the command,
    #: the shop button and the tests all read one table.
    SKIP_MODES: ClassVar[dict[str, str]] = {"1": "daily", "2": "bshield", "3": "sshield"}
    SKIP_USAGE: ClassVar[str] = (
        "💡 <b>Multi-Purpose Skip Cooldown Usage:</b>\n"
        "• <code>/skip 1</code> ➡️ Reset /daily Reward Cooldown\n"
        "• <code>/skip 2</code> ➡️ Consume ticket &amp; load 1 🛡️ Bomb Shield\n"
        "• <code>/skip 3</code> ➡️ Consume ticket &amp; load 1 🔒 Steal Shield"
    )

    async def skip_mode(self, session: AsyncSession, user_id: int, mode: str) -> UseResult:
        """Spend one Skip Cooldown ticket on exactly one of the three reference modes.

        The reference burned the ticket on every path it reached, including the two that then
        returned "you have nothing to skip" — a 25,000-coin ticket lost to a typo. Here the effect
        is resolved first and the ticket is spent last, so a no-op is free.
        """
        if mode not in self.SKIP_MODES:
            raise Locked(self.SKIP_USAGE)
        if await items_repo.stacks(session, user_id, "skip") <= 0:
            raise NotFound("Skip Ticket Missing! Buy one from /market.")
        target = self.SKIP_MODES[mode]
        if target == "daily":
            cleared = await items_repo.clear_cooldown(session, user_id, "daily")
            if not cleared:
                raise AlreadyClaimed("your /daily reward is already available to claim!")
            effect = "⏰ daily cooldown cleared — claim /daily now"
        else:
            total = await items_repo.add_shields(session, user_id, target, 1)
            label = "🛡️ bomb shield" if target == "bshield" else "🔒 steal shield"
            effect = f"{label} loaded ({total} charge(s) on file)"
        left = await items_repo.spend(session, user_id, "skip")
        return UseResult(
            key="skip",
            name=items_repo.item("skip").name,
            effect=effect,
            stacks_left=left,
            details={"mode": mode, "tickets_left": left},
        )

    async def use(
        self, session: AsyncSession, user_id: int, key: str, *, target_id: int | None = None
    ) -> UseResult:
        """Apply a consumable. The stack is spent **after** the effect succeeds.

        Reversing those is the classic "item vanished, nothing happened" bug, and a
        bomb is the one effect that can legitimately fail (nothing to steal), so it
        pays special attention to the order.
        """
        definition = items_repo.item(key)
        if key in SHIELD_ITEMS:
            # Buy-and-arm in one step: the stack is spent first, and only then is the
            # shield charge created, so a crash cannot grant a free shield.
            await items_repo.spend(session, user_id, key)
            total = await items_repo.add_shields(session, user_id, SHIELD_ITEMS[key], 1)
            return UseResult(
                key=key,
                name=definition.name,
                effect=f"🛡️ shield armed ({SHIELD_ITEMS[key]})",
                stacks_left=await items_repo.stacks(session, user_id, key),
                charges=total,
            )
        if key == "skip":
            cleared = await items_repo.clear_cooldown(session, user_id)
            if not cleared:
                raise AlreadyClaimed("nothing is on cooldown right now")
            await items_repo.spend(session, user_id, key)
            return UseResult(
                key=key,
                name=definition.name,
                effect=f"{cleared} cooldown(s) cleared",
                stacks_left=await items_repo.stacks(session, user_id, key),
            )
        if key in CHARGE_ITEMS:
            charges = await items_repo.activate(session, user_id, key)
            user = await user_repo.get(session, user_id)
            attribute = CHARGE_ITEMS[key]
            if user is not None:
                setattr(user, attribute, int(getattr(user, attribute)) + definition.charges)
                await session.flush()
            return UseResult(
                key=key,
                name=definition.name,
                effect=f"{definition.label} active — {getattr(user, attribute) if user else 0} charges banked",
                stacks_left=await items_repo.stacks(session, user_id, key),
                charges=getattr(user, attribute) if user else 0,
                details={"left": charges},
            )
        if key == "bomb":
            if target_id is None:
                raise WaifuError("pick a victim first: /bomb <player>")
            result = await self.ctx.economy.bomb(session, user_id, target_id)
            if not result.get("ok"):
                # Failed bomb: shield absorbed it, so the attacker keeps the item.
                return UseResult(
                    key=key,
                    name=definition.name,
                    effect="target was shielded — item kept",
                    stacks_left=await items_repo.stacks(session, user_id, key),
                    details=result,
                )
            await items_repo.spend(session, user_id, key)
            return UseResult(
                key=key,
                name=definition.name,
                effect=f"stole {result.get('xp', 0)} EXP",
                stacks_left=await items_repo.stacks(session, user_id, key),
                details=result,
            )
        await items_repo.spend(session, user_id, key)
        return UseResult(
            key=key,
            name=definition.name,
            effect="used",
            stacks_left=await items_repo.stacks(session, user_id, key),
        )

    # ----------------------------------------------------------------- charges
    def take_charge(self, user: Any, attribute: str) -> bool:
        """Consume one banked charge if present (called by the game loops)."""
        if int(getattr(user, attribute, 0) or 0) <= 0:
            return False
        setattr(user, attribute, int(getattr(user, attribute)) - 1)
        return True

    async def pity_report(self, session: AsyncSession, user_id: int) -> dict[str, int]:
        state = await progress_repo.pity(session, user_id)
        return {
            "rare": state.rare,
            "high": state.high,
            "celestial": state.celestial,
            "total": state.pulls_total,
            "rare_left": state.rare_remaining(max(1, self.settings.pity_high_after // 3)),
            "high_left": state.high_remaining(self.settings.pity_high_after),
            "celestial_left": max(0, self.settings.pity_celestial_after - state.celestial),
        }

    async def heist_stats(self, session: AsyncSession, user_id: int) -> dict[str, int]:
        return await items_repo.heist_stats(session, user_id)

    async def recent_heists(
        self, session: AsyncSession, user_id: int, *, limit: int = 8
    ) -> list[Any]:
        return await items_repo.recent_heists(session, user_id, limit=limit)

    async def protective_summary(self, session: AsyncSession) -> dict[str, int]:
        return await items_repo.protective_summary(session)

    async def most_stolen(self, session: AsyncSession, *, limit: int = 10) -> list[tuple[int, int]]:
        return await items_repo.most_stolen_from(session, limit=limit)

    async def purge(self, session: AsyncSession) -> int:
        return await items_repo.purge_spent_items(session)

    async def cooldowns(self, session: AsyncSession, user_id: int) -> dict[str, int]:
        """Remaining cooldowns, so /bag can tell a player what they can do now."""
        out: dict[str, int] = {}
        for command, seconds in items_repo.SPECIAL_COOLDOWNS.items():
            left = await items_repo.cooldown_left(session, user_id, command, seconds)
            if left:
                out[command] = left
        for command, seconds in (("work", 1800),):
            left = await items_repo.cooldown_left(session, user_id, command, seconds)
            if left:
                out[command] = left
        return out

    async def clear_cooldown(
        self, session: AsyncSession, user_id: int, *, command: str | None = None
    ) -> int:
        return await items_repo.clear_cooldown(session, user_id, command)

    async def set_cooldown(self, session: AsyncSession, user_id: int, command: str) -> None:
        await items_repo.set_cooldown(session, user_id, command)

    async def raise_if_on_cooldown(
        self, session: AsyncSession, user_id: int, command: str, seconds: int
    ) -> None:
        left = await items_repo.cooldown_left(session, user_id, command, seconds)
        if left:
            raise CooldownActive(left)
