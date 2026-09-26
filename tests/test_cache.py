"""The cache layer itself: hits, TTLs and versioned invalidation.

These tests exist because a broken cache is *silent* — a read that always misses looks exactly like
a cold start, and the services above it (group settings, ``/hstats``, the join gate) simply get
slower while every number stays correct. The bug this file caught is documented on
:meth:`Cache._key`: the key builder interpolated an un-awaited coroutine, so no key ever repeated.
"""

from __future__ import annotations

from waifu.db.state import Cache


async def test_a_second_read_hits_without_calling_the_loader() -> None:
    calls = 0

    async def loader() -> dict[str, int]:
        nonlocal calls
        calls += 1
        return {"n": calls}

    cache = Cache(prefix="t")
    first = await cache.get_or_set("groups", (1,), loader)
    second = await cache.get_or_set("groups", (1,), loader)
    assert first == second == {"n": 1}, "a read-through cache that reloads is just a slower cache"


async def test_distinct_parts_are_distinct_entries() -> None:
    cache = Cache(prefix="t")
    await cache.set("groups", (1,), {"chat": 1}, 60)
    await cache.set("groups", (2,), {"chat": 2}, 60)
    assert await cache.get("groups", 1) == {"chat": 1}
    assert await cache.get("groups", 2) == {"chat": 2}
    assert await cache.get("groups", 3) is None


async def test_invalidate_orphans_the_namespace_and_leaves_the_neighbours() -> None:
    cache = Cache(prefix="t")
    await cache.set("groups", (1,), {"a": 1}, 60)
    await cache.set("hstats", (1,), {"b": 2}, 60)
    await cache.invalidate("groups")
    assert await cache.get("groups", 1) is None, "the version bump must be consulted on read"
    assert await cache.get("hstats", 1) == {"b": 2}, "invalidation is per namespace, not global"


async def test_delete_removes_one_entry() -> None:
    cache = Cache(prefix="t")
    await cache.set("gate", (1, 2), {"tries": 3}, 60)
    await cache.delete("gate", 1, 2)
    assert await cache.get("gate", 1, 2) is None


async def test_the_backend_is_reported_for_the_health_page() -> None:
    cache = Cache(prefix="t")
    stats = await cache.stats()
    assert stats["backend"] == "local" and cache.enabled is False
    await cache.set("groups", (9,), {"x": 1}, 60)
    assert (await cache.stats())["namespaces"]["groups"] == 0, "reading does not bump versions"


async def test_a_zero_ttl_falls_back_to_the_local_default() -> None:
    cache = Cache(prefix="t", local_ttl=7)
    await cache.set("groups", (1,), {"x": 1}, 0)
    assert await cache.get("groups", 1) == {"x": 1}
    assert cache._local._data["t:c:0:groups:1"][0] > 0
