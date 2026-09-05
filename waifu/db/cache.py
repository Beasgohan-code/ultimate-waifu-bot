"""Read-through cache for the hot DB reads that every update touches.

The catalogue (``/chance``, ``/claimlist``, character lookups by id) is written a
handful of times a day and read thousands of times a minute. Those go through
here; everything that involves money, spawns, bids or claims goes straight to
Postgres. Redis optional: with no Redis the cache degrades to a small in-process
TTL dict, so a single-worker instance still runs.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

from waifu.logging import get_logger

log = get_logger("db.cache")

Key = tuple[str | int, ...]


class _LocalTTL:
    """Fallback when Redis is not configured (dev, tests, single process)."""

    def __init__(self, max_items: int = 2048) -> None:
        self._data: dict[str, tuple[float, Any]] = {}
        self._max = max_items
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Any:
        async with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            expires, value = item
            if expires < time.monotonic():
                self._data.pop(key, None)
                return None
            return value

    async def set(self, key: str, value: Any, ttl: int) -> None:
        async with self._lock:
            if len(self._data) >= self._max:  # pragma: no cover - memory guard
                for stale in [k for k, (e, _) in self._data.items() if e < time.monotonic()]:
                    self._data.pop(stale, None)
                if len(self._data) >= self._max:
                    self._data.pop(next(iter(self._data)))
            self._data[key] = (time.monotonic() + ttl, value)

    async def delete(self, *keys: str) -> None:
        async with self._lock:
            for key in keys:
                self._data.pop(key, None)

    async def keys_with_prefix(self, prefix: str) -> list[str]:
        async with self._lock:
            return [k for k in self._data if k.startswith(prefix)]

    async def clear(self) -> None:
        async with self._lock:
            self._data.clear()


class Cache:
    """JSON read-through cache with prefix invalidation.

    Versioned namespaces make invalidation O(1): ``invalidate("chars")`` bumps a
    counter so every previously cached ``chars:*`` key is unreachable instead of
    needing SCAN (which is not allowed on managed Redis in some setups).
    """

    def __init__(self, redis: Any = None, *, prefix: str = "uwb", local_ttl: int = 5) -> None:
        self._redis = redis
        self._prefix = prefix
        self._local = _LocalTTL()
        self._local_ttl = local_ttl
        self._versions: dict[str, int] = {}

    @property
    def enabled(self) -> bool:
        return self._redis is not None

    async def close(self) -> None:
        await self._local.clear()

    # ------------------------------------------------------------------- keys
    def _key(self, namespace: str, *parts: str | int) -> str:
        return f"{self._prefix}:c:{self._version(namespace)}:{namespace}:{':'.join(str(p) for p in parts)}"

    async def _version(self, namespace: str) -> int:
        if namespace in self._versions:
            return self._versions[namespace]
        version = 0
        if self._redis is not None:
            raw = await self._redis.get(f"c:ver:{namespace}")
            version = int(raw) if raw and str(raw).isdigit() else 0
        self._versions[namespace] = version
        return version

    async def get(self, namespace: str, *parts: str | int) -> Any:
        key = self._key(namespace, *parts)
        if self._redis is not None:
            raw = await self._redis.get(key)
            if raw is not None:
                try:
                    return json.loads(raw)
                except (TypeError, json.JSONDecodeError):  # pragma: no cover
                    return None
            return None
        return await self._local.get(key)

    async def set(
        self, namespace: str, key_parts: tuple[str | int, ...], value: Any, ttl: int
    ) -> None:
        full = self._key(namespace, *key_parts)
        if self._redis is not None:
            await self._redis.set(full, json.dumps(value, default=str, separators=(",", ":")), ttl)
            return
        await self._local.set(full, value, ttl or self._local_ttl)

    async def get_or_set(
        self,
        namespace: str,
        key_parts: tuple[str | int, ...],
        loader: Callable[[], Awaitable[Any]],
        *,
        ttl: int = 60,
    ) -> Any:
        """Fetch, or compute-and-cache. Loader exceptions are not cached."""
        cached = await self.get(namespace, *key_parts)
        if cached is not None:
            return cached
        value = await loader()
        if value is not None:
            await self.set(namespace, key_parts, value, ttl)
        return value

    async def delete(self, namespace: str, *parts: str | int) -> None:
        key = self._key(namespace, *parts)
        if self._redis is not None:
            await self._redis.delete(key)
            return
        await self._local.delete(key)

    async def invalidate(self, namespace: str) -> None:
        """Make every cached entry in ``namespace`` unreachable (version bump)."""
        version = await self._version(namespace) + 1
        self._versions[namespace] = version
        if self._redis is not None:
            await self._redis.set(f"c:ver:{namespace}", str(version), None)
        else:
            for key in await self._local.keys_with_prefix(
                f"{self._prefix}:c:{version - 1}:{namespace}"
            ):
                await self._local.delete(key)

    async def stats(self) -> dict[str, Any]:
        return {"backend": "redis" if self.enabled else "local", "namespaces": dict(self._versions)}


#: Namespaces used by the services layer — declared here so a typo fails fast.
NS_CATALOGUE = "catalogue"
NS_ODDS = "odds"
NS_GROUPS = "groups"
NS_LEADERBOARD = "leaderboard"
NS_USER = "user"
CACHE_NAMESPACES = (NS_CATALOGUE, NS_ODDS, NS_GROUPS, NS_LEADERBOARD, NS_USER)
