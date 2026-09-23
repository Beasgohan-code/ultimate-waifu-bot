"""Hot state — the fast layer that is **never the source of truth**.

Two objects, one rule: everything in this module is a cache or a coordination
helper with TTLs. Money and collections live in the database
(:mod:`waifu.db.database`), so losing this layer degrades speed, never
correctness, and a backup of the database restores everything that matters.

* :class:`Cache` — read-through cache for the hot catalogue reads
  (``/chance``, ``/claimlist``, character lookups). Written a handful of times
  a day, read thousands of times a minute; everything involving money, spawns,
  bids or claims goes straight to the database.
* :class:`Redis` — optional: shared FSM state, cooldowns, leaderboards, the
  spawn queue, work streams, locks and pub/sub. Configured with ``REDIS_URL``;
  without it the bot runs on in-process state (single worker) and the cache
  degrades to a small in-process TTL dict.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

from waifu.logging import get_logger
from waifu.settings import Settings, get_settings

log = get_logger("db.state")

Key = tuple[str | int, ...]


# ----------------------------------------------------------------------- cache
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
    def _ver_key(self, namespace: str) -> str:
        return f"{self._prefix}:c:ver:{namespace}"

    async def _key(self, namespace: str, *parts: str | int) -> str:
        """The full key, *awaiting* the namespace version.

        This used to be a synchronous ``f-string`` that interpolated ``self._version(...)`` —
        a coroutine object — so every generated key was unique per call: reads never hit,
        ``invalidate`` bumped a version nothing consulted, and the whole cache degraded to a
        write-only store. The async signature is the fix; :mod:`tests.test_cache` is the tripwire.
        """
        version = await self._version(namespace)
        return f"{self._prefix}:c:{version}:{namespace}:{':'.join(str(p) for p in parts)}"

    async def _version(self, namespace: str) -> int:
        if namespace in self._versions:
            return self._versions[namespace]
        version = 0
        if self._redis is not None:
            raw = await self._redis.get(self._ver_key(namespace))
            version = int(raw) if raw and str(raw).isdigit() else 0
        self._versions[namespace] = version
        return version

    async def get(self, namespace: str, *parts: str | int) -> Any:
        key = await self._key(namespace, *parts)
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
        full = await self._key(namespace, *key_parts)
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
        key = await self._key(namespace, *parts)
        if self._redis is not None:
            await self._redis.delete(key)
            return
        await self._local.delete(key)

    async def invalidate(self, namespace: str) -> None:
        """Make every cached entry in ``namespace`` unreachable (version bump)."""
        version = await self._version(namespace) + 1
        self._versions[namespace] = version
        if self._redis is not None:
            await self._redis.set(self._ver_key(namespace), str(version), None)
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


# ----------------------------------------------------------------------- redis
# Lua: "decrement if positive", atomic. Used for item/shield/charge spends.
_DECREMENT_IF_POSITIVE = """
local v = redis.call('GET', KEYS[1])
if v and tonumber(v) > 0 then
  local left = tonumber(v) - 1
  redis.call('SET', KEYS[1], left)
  if tonumber(ARGV[1]) > 0 and left == tonumber(ARGV[1]) then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
  return left
end
return -1
"""

_CONSUME_TTL = """
if redis.call(' EXISTS ', KEYS[1]) == 1 then
  local ttl = redis.call('TTL', KEYS[1])
  redis.call('DEL', KEYS[1])
  return ttl
end
return -2
"""


class Redis:
    """Thin, typed-ish facade over redis.asyncio with namespaced keys.

    Only built when ``REDIS_URL`` is set (see :meth:`Redis.create`); the
    application runs fine without it (in-process state, single worker).
    """

    def __init__(self, client: Any, prefix: str) -> None:
        self._client = client
        self._prefix = prefix
        self._scripts: dict[str, Any] = {}

    # ------------------------------------------------------------- lifecycle
    @classmethod
    async def create(cls, settings: Settings | None = None) -> Redis:
        cfg = settings or get_settings()
        import redis.asyncio as aioredis

        client = aioredis.from_url(
            cfg.redis_url,
            encoding="utf-8",
            decode_responses=True,
            max_connections=cfg.redis_max_connections,
            health_check_interval=30,
        )
        await client.ping()
        redis = cls(client, cfg.redis_key_prefix)
        redis._register_scripts()
        log.info("redis connected (%s connections allowed)", cfg.redis_max_connections)
        return redis

    @classmethod
    def from_client(cls, client: Any, prefix: str = "uwb") -> Redis:
        redis = cls(client, prefix)
        redis._register_scripts()
        return redis

    def _register_scripts(self) -> None:
        for name, body in (("decr_if_positive", _DECREMENT_IF_POSITIVE), ("consume", _CONSUME_TTL)):
            try:
                self._scripts[name] = self._client.register_script(body)
            except Exception:  # pragma: no cover - pipeline-less registration rarely fails
                self._scripts[name] = None

    @property
    def client(self) -> Any:
        return self._client

    async def close(self) -> None:
        with_suppress = getattr(self._client, "aclose", None) or self._client.close
        await with_suppress()

    def k(self, *parts: str | int) -> str:
        return ":".join((self._prefix, *(str(p) for p in parts)))

    # ---------------------------------------------------------------- generic
    async def get(self, *key: str | int) -> str | None:
        return await self._client.get(self.k(*key))

    async def get_json(self, *key: str | int) -> Any:
        raw = await self.get(*key)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return raw

    async def set(
        self, key: tuple[str | int, ...] | str, value: Any, ttl: int | None = None
    ) -> None:
        if isinstance(value, (dict, list)):
            value = json.dumps(value, separators=(",", ":"), default=str)
        name = self.k(*(key if isinstance(key, tuple) else (key,)))
        await self._client.set(name, value, ex=ttl)

    async def setex(self, *key: str | int, ttl: int, value: Any) -> None:
        await self.set(tuple(key[:-1]) if len(key) > 1 else key[0], value, ttl)

    async def delete(self, *key: str | int) -> int:
        return int(await self._client.delete(self.k(*key)))

    async def exists(self, *key: str | int) -> bool:
        return bool(await self._client.exists(self.k(*key)))

    async def incr(self, *key: str | int, ttl: int | None = None, amount: int = 1) -> int:
        name = self.k(*key)
        value = int(await self._client.incrby(name, amount))
        if ttl and value == amount:
            await self._client.expire(name, ttl)
        return value

    async def decr_if_positive(self, *key: str | int, rearm_ttl: int = 0) -> int:
        """Atomically spend one charge; returns remaining, or -1 when none left."""
        script = self._scripts.get("decr_if_positive")
        name = self.k(*key)
        if script is None:  # pragma: no cover - fallback for exotic redis setups
            current = await self._client.get(name)
            if not current or int(current) <= 0:
                return -1
            return int(await self._client.decr(name))
        return int(await script(keys=[name], args=[rearm_ttl]))

    async def consume(self, *key: str | int) -> int:
        """Delete a one-shot marker, returning its previous TTL (-2 if absent)."""
        script = self._scripts.get("consume")
        name = self.k(*key)
        if script is None:
            existed = await self._client.delete(name)
            return 0 if existed else -2
        return int(await script(keys=[name], args=[]))

    # ------------------------------------------------------------------ locks
    async def lock(self, name: str, ttl: int = 20) -> str | None:
        """Acquire a lock, returning its token (pass to :meth:`unlock`)."""
        token = f"{int(time.time() * 1000):x}{id(object())}"
        ok = await self._client.set(self.k("lock", name), token, nx=True, px=ttl * 1000)
        return token if ok else None

    async def unlock(self, name: str, token: str) -> None:
        # Only delete if we still own it (Lua would be stricter; this is enough
        # for TTL'd idempotency guards).
        key = self.k("lock", name)
        if await self._client.get(key) == token:
            await self._client.delete(key)

    async def with_lock(self, name: str, ttl: int = 20):
        import contextlib

        @contextlib.asynccontextmanager
        async def ctx() -> Any:
            token = await self.lock(name, ttl)
            try:
                yield token is not None
            finally:
                if token:
                    await self.unlock(name, token)

        return ctx()

    # -------------------------------------------------------------- cooldowns
    async def cooldown_left(self, *key: str | int) -> int:
        ttl = await self._client.ttl(self.k("cd", *key))
        return max(0, int(ttl)) if ttl and ttl > 0 else 0

    async def start_cooldown(
        self, key: tuple[str | int, ...], seconds: int, value: Any = 1
    ) -> None:
        await self.set(("cd", *key), value, seconds)

    async def claim_once(self, key: tuple[str | int, ...], ttl: int, value: Any = 1) -> bool:
        """Set-if-absent: idempotency guard for /daily-style one-shots."""
        return bool(
            await self._client.set(
                self.k(*key),
                value if not isinstance(value, (dict, list)) else json.dumps(value),
                nx=True,
                ex=ttl,
            )
        )

    # ------------------------------------------------------------- rate limits
    async def sliding_hit(self, key: str, limit: int, window: int) -> tuple[bool, int, int]:
        """Returns ``(allowed, remaining, retry_after)`` using a fixed window."""
        name = self.k("rl", key, str(int(time.time() // window)))
        count = int(await self._client.incr(name))
        if count == 1:
            await self._client.expire(name, window)
        if count <= limit:
            return True, limit - count, 0
        retry = await self._client.ttl(name)
        return False, 0, max(1, int(retry or window))

    # --------------------------------------------------------- leaderboards
    async def zadd(self, name: str, member: str, score: float) -> None:
        await self._client.zadd(self.k("lb", name), {member: score})

    async def zincr(self, name: str, member: str, delta: float) -> float:
        return float(await self._client.zincrby(self.k("lb", name), delta, member))

    async def ztop(self, name: str, limit: int = 25, offset: int = 0) -> list[tuple[str, float]]:
        rows = await self._client.zrevrange(
            self.k("lb", name), offset, offset + limit - 1, withscores=True
        )
        return [(str(m), float(s)) for m, s in rows]

    async def zrank_desc(self, name: str, member: str) -> int | None:
        rank = await self._client.zrevrank(self.k("lb", name), member)
        return None if rank is None else int(rank) + 1

    async def zcard(self, name: str) -> int:
        return int(await self._client.zcard(self.k("lb", name)))

    async def zscore(self, name: str, member: str) -> float | None:
        value = await self._client.zscore(self.k("lb", name), member)
        return None if value is None else float(value)

    # ----------------------------------------------------- timed queues (ZSET)
    async def queue_push(self, name: str, member: str, when: float) -> None:
        await self._client.zadd(self.k("q", name), {member: when})

    async def queue_pop_due(
        self, name: str, up_to: float | None = None, *, limit: int = 50
    ) -> list[str]:
        """Pop members whose due time has passed (claim-ish, not exactly-once)."""
        up_to = up_to if up_to is not None else time.time()
        key = self.k("q", name)
        members = await self._client.zrangebyscore(key, "-inf", up_to, start=0, num=limit)
        if members:
            await self._client.zrem(key, *members)
        return [str(m) for m in members]

    async def queue_peek_due(
        self, name: str, up_to: float | None = None, *, limit: int = 50
    ) -> list[tuple[str, float]]:
        up_to = up_to if up_to is not None else time.time()
        rows = await self._client.zrangebyscore(
            self.k("q", name), "-inf", up_to, start=0, num=limit, withscores=True
        )
        return [(str(m), float(s)) for m, s in rows]

    async def queue_remove(self, name: str, *members: str) -> None:
        if members:
            await self._client.zrem(self.k("q", name), *members)

    # ------------------------------------------------------------ streams (jobs)
    async def stream_add(self, name: str, payload: dict[str, Any], *, maxlen: int = 10000) -> str:
        return str(
            await self._client.xadd(
                self.k("s", name),
                {"data": json.dumps(payload, default=str)},
                maxlen=maxlen,
                approximate=True,
            )
        )

    async def stream_group_setup(self, name: str, group: str) -> None:
        try:
            await self._client.xgroup_create(self.k("s", name), group, id="$", mkstream=True)
        except Exception as exc:  # BUSYGROUP means it already exists
            if "BUSYGROUP" not in str(exc):
                raise

    async def stream_read(
        self, name: str, group: str, consumer: str, *, count: int = 25, block: int = 2000
    ) -> list[tuple[str, dict]]:
        rows = await self._client.xreadgroup(
            group, consumer, {self.k("s", name): ">"}, count=count, block=block
        )
        out: list[tuple[str, dict]] = []
        for _stream, entries in rows or []:
            for entry_id, fields in entries:
                try:
                    out.append((entry_id, json.loads(fields.get("data", "{}"))))
                except (TypeError, json.JSONDecodeError):  # pragma: no cover
                    out.append((entry_id, dict(fields)))
        return out

    async def stream_ack(self, name: str, group: str, *ids: str) -> None:
        if ids:
            await self._client.xack(self.k("s", name), group, *ids)

    # -------------------------------------------------------------- pub/sub
    async def publish(self, channel: str, payload: dict[str, Any]) -> int:
        return int(
            await self._client.publish(self.k("ch", channel), json.dumps(payload, default=str))
        )

    async def subscribe(self, channel: str) -> Any:
        pubsub = self._client.pubsub()
        await pubsub.subscribe(self.k("ch", channel))
        return pubsub

    # -------------------------------------------------------- hash-based state
    async def hset(self, name: str, mapping: dict[str, Any], *, ttl: int | None = None) -> None:
        key = self.k("h", name)
        flat = {
            k: (json.dumps(v, default=str) if isinstance(v, (dict, list)) else str(v))
            for k, v in mapping.items()
        }
        await self._client.hset(key, mapping=flat)
        if ttl:
            await self._client.expire(key, ttl)

    async def hgetall(self, name: str) -> dict[str, str]:
        return dict(await self._client.hgetall(self.k("h", name)) or {})

    async def hdel(self, name: str, *fields: str) -> None:
        if fields:
            await self._client.hdel(self.k("h", name), *fields)

    async def sadd(self, name: str, *members: str) -> int:
        return int(await self._client.sadd(self.k("set", name), *members)) if members else 0

    async def smembers(self, name: str) -> set[str]:
        return set(await self._client.smembers(self.k("set", name)))

    async def sismember(self, name: str, member: str) -> bool:
        return bool(await self._client.sismember(self.k("set", name), member))

    async def scard(self, name: str) -> int:
        return int(await self._client.scard(self.k("set", name)))

    # ------------------------------------------------------------ info/health
    async def healthcheck(self) -> dict[str, Any]:
        info = await self._client.info(section="server")
        mem = await self._client.info(section="memory")
        return {
            "redis": "ok",
            "version": info.get("redis_version"),
            "uptime_s": info.get("uptime_in_seconds"),
            "used_memory_human": mem.get("used_memory_human"),
            "keyspace": await self._client.dbsize(),
        }
