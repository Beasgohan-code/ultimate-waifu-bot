"""Repository layer — the only modules that talk SQL.

Layout mirrors Summon-bot's file split (users / characters / collection / market
items / auctions / admin), so an admin reading this codebase recognises the
domain immediately. Handlers never import these; services do. That gives one
auditable chokepoint for "who changed money or a collection".
"""

from __future__ import annotations

from waifu.db.repositories import (
    auctions,
    characters,
    collection,
    economy,
    items,
    moderation,
    monetize,
    progress,
    spawns,
    stats,
    trades,
    users,
)

__all__ = [
    "auctions",
    "characters",
    "collection",
    "economy",
    "items",
    "moderation",
    "monetize",
    "progress",
    "spawns",
    "stats",
    "trades",
    "users",
]
