"""Service registry: one place that builds the application layer.

The services are constructed with the context (not the other way round) so they can
hold a back-reference for cross-service calls — :meth:`build` runs *after* the
context exists. Nothing here touches Telegram or SQL: services own the business
rules, the repositories own the SQL, ``waifu/tg`` owns the Bot API, and the plugins
own the routing. That separation is the whole reason this codebase can be tested
without a bot token.

Adding a feature = one file in this package + one line in :data:`ORDER` + a router in
``waifu/plugins/``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from waifu.services.ai import AiService
from waifu.services.auction import AuctionService
from waifu.services.cards import CardService
from waifu.services.collection import CollectionService
from waifu.services.economy import EconomyService
from waifu.services.gacha import GachaService
from waifu.services.hstats import HStatsService
from waifu.services.items import ItemService
from waifu.services.moderation import ModerationService
from waifu.services.premium import PremiumService
from waifu.services.progress import ProgressService
from waifu.services.spawn import SpawnService
from waifu.services.stats import StatsService
from waifu.services.trading import CodeService, GiftService, TradeService

if TYPE_CHECKING:  # pragma: no cover
    from waifu.core.context import AppContext

#: Attribute name → service class, in dependency order.
#:
#: ``economy`` first (everyone debits through it), then ``collection``/``items``
#: (ownership + consumables), then the features that compose them. The order matters
#: only for readability — nothing in here talks during ``__init__``.
ORDER: tuple[tuple[str, type], ...] = (
    ("economy", EconomyService),
    ("collection", CollectionService),
    ("items", ItemService),
    ("progress", ProgressService),
    ("gacha", GachaService),
    ("spawn", SpawnService),
    ("auctions", AuctionService),
    ("trades", TradeService),
    ("gifts", GiftService),
    ("codes", CodeService),
    ("premium", PremiumService),
    ("stats", StatsService),
    ("hstats", HStatsService),
    ("moderation", ModerationService),
    ("cards", CardService),
    ("ai", AiService),
)


def build(ctx: AppContext) -> AppContext:
    """Attach every service to the context and return it."""
    for name, service in ORDER:
        setattr(ctx, name, service(ctx))
    return ctx


def services(ctx: AppContext) -> dict[str, Any]:
    """The live services as a mapping (used by the web panel and /diagnostics)."""
    return {name: getattr(ctx, name) for name, _ in ORDER if getattr(ctx, name, None) is not None}


__all__ = ["ORDER", "AiService", "build", "services"]
