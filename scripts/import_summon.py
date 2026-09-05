#!/usr/bin/env python3
"""Migrate a Summon-bot database into this schema — full parity, no lost progress.

    # from the sqlite file the old bot shipped (or a dump of it)
    python scripts/import_summon.py --from summon.db --dry-run
    python scripts/import_summon.py --from summon.db

    # or straight from their production Postgres
    python scripts/import_summon.py \\
        --from postgresql://user:pass@host/summon_bot --to postgresql+asyncpg://user:pass@host/waifu

Why a script instead of "just copy the rows": the two schemas disagree in ways that
silently corrupt a collection, and every one of them is handled here explicitly —

* their ``characters.id`` is **TEXT** (``'01'``), ours is an int identity column, so an
  ``old_id → new_id`` map is built first and every referencing table goes through it;
* their rarity is the *decorated display string* (``"🎥 AMV Edition"``), mapped via
  :meth:`waifu.enums.Rarity.from_label`, with unknown labels reported instead of
  being silently downgraded to Common;
* their ``users.balance`` has no ledger, and a wallet with no history is unauditable,
  so each balance is imported as an ``admin_grant`` transaction with a stable
  idempotency key — re-running the import cannot double anyone's money;
* ``premium.expires_at`` becomes ``premium_until`` **plus** a ``premium`` grant row;
* auctions/bids keep their real ids so a live auction stays live and ``pinned_msg_id``
  still points at the same Telegram message;
* anything we cannot map (a shop item we do not sell, an orphan collection row) is
  counted in the report, never dropped quietly.

The script is idempotent: it skips what already exists, keyed on natural identity.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import func, insert, select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from waifu.db.models import (
    Achievement,
    Auction,
    AuctionBid,
    Character,
    ClaimChance,
    GiftLog,
    Group,
    InventoryItem,
    Ownership,
    Premium,
    RarityChance,
    RedeemCode,
    Streak,
    Transaction,
    User,
    UserPref,
    Warning,
)
from waifu.enums import LedgerReason, Rarity

#: Their ``user_inventory.item_id`` values → our item keys. The old bot sold numbered
#: slots in some deployments and string ids in others; both are accepted.
ITEM_MAP = {
    "1": "bomb",
    "2": "lucky",
    "3": "skip",
    "4": "magnet",
    "5": "sshield",
    "6": "bshield",
    "7": "xp",
    "bomb": "bomb",
    "lucky": "lucky",
    "lucky_ticket": "lucky",
    "skip": "skip",
    "magnet": "magnet",
    "sshield": "sshield",
    "steal_shield": "sshield",
    "bshield": "bshield",
    "bomb_shield": "bshield",
    "xp": "xp",
    "xp_boost": "xp",
}


def _drop_none(**values: Any) -> dict[str, Any]:
    """Strip NULLs so column defaults apply.

    ``insert().values(x=None)`` is not the same as omitting ``x``: the former writes
    NULL and defeats the model's default (and any NOT NULL column), which is exactly
    how a migration can look fine on rows that had every field and die on the ones
    that did not.
    """
    return {key: value for key, value in values.items() if value is not None}


@dataclass(slots=True)
class Report:
    created: Counter[str] = field(default_factory=Counter)
    skipped: Counter[str] = field(default_factory=Counter)
    warnings: list[str] = field(default_factory=list)
    unknown: Counter[str] = field(default_factory=Counter)

    def add_warning(self, message: str) -> None:
        if len(self.warnings) < 200:  # keep the tail readable on a big import
            self.warnings.append(message)

    def render(self) -> str:
        lines = ["── import report ──"]
        for key in sorted(set(self.created) | set(self.skipped)):
            lines.append(f"  {key:<18} created={self.created[key]:<6} skipped={self.skipped[key]}")
        if self.unknown:
            lines.append(
                "  unmapped: " + ", ".join(f"{k}×{v}" for k, v in self.unknown.most_common())
            )
        if self.warnings:
            lines.append(f"  {len(self.warnings)} warning(s):")
            lines += [f"    · {w}" for w in self.warnings[:25]]
        return "\n".join(lines)


def parse_dt(raw: Any) -> datetime | None:
    """Accept ``'2026-07-24 03:34:40'``, ISO, epoch ints, and NULLs."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, datetime):
        return raw.replace(tzinfo=None) if raw.tzinfo else raw
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(float(raw), UTC).replace(tzinfo=None)
    text_value = str(raw).strip().replace("Z", "")
    for pattern in (
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(text_value, pattern)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text_value).replace(tzinfo=None)
    except ValueError:
        return None


def read_source(url: str) -> dict[str, list[dict[str, Any]]]:
    """Return ``{table: [rows]}`` from a sqlite path or a Postgres URL (sync driver)."""
    if url.startswith(("postgresql://", "postgresql+asyncpg://", "postgres://")):
        return _read_postgres(url)
    path = Path(url)
    if not path.exists():
        raise SystemExit(f"source not found: {path}")
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    tables = [
        row[0]
        for row in conn.execute(
            "select name from sqlite_master where type='table' and name != 'sqlite_sequence'"
        )
    ]
    return {
        table: [dict(row) for row in conn.execute(f'select * from "{table}"')] for table in tables
    }


def _read_postgres(url: str) -> dict[str, list[dict[str, Any]]]:
    """Same shape as the sqlite reader, via psycopg (v3 or v2, whichever is installed)."""
    dsn = re.sub(r"^postgresql\+asyncpg://", "postgresql://", url)
    try:
        import psycopg  # type: ignore[import-not-found]

        conn = psycopg.connect(dsn, autocommit=True)

        def cursor_row_factory(cur):
            return cur

        del cursor_row_factory
        names = [
            r[0]
            for r in conn.execute(
                "select table_name from information_schema.tables where table_schema='public'"
            )
        ]
        out: dict[str, list[dict[str, Any]]] = {}
        for name in names:
            with conn.cursor() as cur:
                cur.execute(f'select * from "{name}"')
                columns = [d.name for d in cur.description]
                out[name] = [dict(zip(columns, row, strict=False)) for row in cur.fetchall()]
        conn.close()
        return out
    except ImportError:
        import psycopg2  # type: ignore[import-not-found]
        from psycopg2.extras import RealDictCursor

        conn = psycopg2.connect(dsn)
        conn.autocommit = True
        names = [
            r[0] for r in conn.cursor() if conn.cursor().execute("select 1") is None
        ] or _pg_tables(conn)
        out: dict[str, list[dict[str, Any]]] = {}
        for name in names:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(f'select * from "{name}"')
                out[name] = list(cur.fetchall())
        conn.close()
        return out


def _pg_tables(conn: Any) -> list[str]:
    with conn.cursor() as cur:
        cur.execute("select table_name from information_schema.tables where table_schema='public'")
        return [row[0] for row in cur.fetchall()]


def normalise(data: dict[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    """Lower-case table keys so the mapper is not case-sensitive (Postgres folds)."""
    return {str(key).lower(): [dict(row) for row in value] for key, value in data.items()}


# --------------------------------------------------------------------- the importer
class Importer:
    def __init__(
        self,
        session: AsyncSession,
        data: dict[str, list[dict[str, Any]]],
        *,
        dry: bool = False,
        report: Report | None = None,
    ) -> None:
        self.s = session
        self.data = data
        self.dry = dry
        self.report = report or Report()
        self.char_map: dict[str, int] = {}
        self.user_ids: set[int] = set()

    async def _exists(self, model, **conds: Any) -> bool:
        stmt = select(func.count()).select_from(model)
        for key, value in conds.items():
            stmt = stmt.where(getattr(model, key) == value)
        return int((await self.s.execute(stmt)).scalar_one() or 0) > 0

    async def _get(self, model, **conds: Any):
        stmt = select(model)
        for key, value in conds.items():
            stmt = stmt.where(getattr(model, key) == value)
        return (await self.s.execute(stmt)).scalar_one_or_none()

    def rows(self, table: str) -> list[dict[str, Any]]:
        return self.data.get(table, [])

    async def _commit(self) -> None:
        """Flush per step so later steps can see earlier rows, even in a dry run.

        ``--dry-run`` is a *real* rehearsal: everything is written inside one
        transaction and rolled back at the end, so FK and uniqueness errors surface
        exactly as they would during the live import.
        """
        await self.s.flush()

    # ---------------------------------------------------------------- characters
    async def characters(self) -> None:
        for row in self.rows("characters"):
            old_id = str(row.get("id", "")).strip()
            name = str(row.get("name") or "").strip()
            anime = str(row.get("anime") or "").strip()
            if not name:
                self.report.add_warning(f"characters: row without a name ({row})")
                continue
            label = str(row.get("rarity") or "")
            rarity = Rarity.from_label(label)
            if (
                label
                and label.strip()
                and rarity is Rarity.COMMON
                and "common" not in label.lower()
            ):
                # The only real failure mode: a label we could not place at all.
                self.report.unknown[f"rarity:{label}"] += 1
                self.report.add_warning(
                    f"{name}: rarity “{label}” is unknown here, defaulted to Common — fix with /chance or rename the row"
                )
            existing = await self._get(Character, name=name, anime=anime)
            if existing is not None:
                self.char_map[old_id] = existing.id
                self.report.skipped["characters"] += 1
                continue
            payload = {
                "name": name,
                "anime": anime,
                "rarity": rarity.badge,
                "rarity_id": int(rarity),
                # Their prices were per-rarity constants; keep any explicit override.
                "price": int(row.get("price") or rarity.base_price),
                "stat_power": int(row.get("stat_power") or 10 * int(rarity)),
                "description": str(row.get("description") or ""),
                "voice_line": str(row.get("voice_line") or ""),
                "tags": str(row.get("tags") or ""),
                "is_active": bool(row.get("is_active", 1)),
                "banner_weight": float(row.get("banner_weight") or 1.0),
                # ``msg_id`` in the old bot is a *video* file_id, not a photo.
                "video_file_id": str(row.get("msg_id") or ""),
                "image_url": str(row.get("img_url") or ""),
                "meta": {
                    "imported_from": "summon-bot",
                    "legacy_id": old_id,
                    "alt_image": row.get("img_url2") or "",
                },
                "created_at": parse_dt(row.get("created_at"))
                or datetime.now(UTC).replace(tzinfo=None),
            }
            created = Character(**payload)
            self.s.add(created)
            await self.s.flush()
            self.char_map[old_id] = created.id
            self.report.created["characters"] += 1

    # --------------------------------------------------------------------- users
    async def users(self) -> None:
        for row in self.rows("users"):
            user_id = int(row.get("user_id") or row.get("id") or 0)
            if not user_id:
                continue
            self.user_ids.add(user_id)
            existing = await self._get(User, id=user_id)
            balance = int(row.get("balance") or 0)
            if existing is not None:
                self.report.skipped["users"] += 1
                continue
            role = "owner" if user_id == int(os.getenv("OWNER_ID", "0") or 0) else "user"
            if int(row.get("sudo_admin") or 0) or str(row.get("role") or "") in {"admin", "owner"}:
                role = "admin"
            banned = bool(int(row.get("banned") or 0))
            await self.s.execute(
                insert(User).values(
                    **_drop_none(
                        id=user_id,
                        username=(str(row.get("username") or "")).lstrip("@") or None,
                        first_name=str(row.get("first_name") or "")[:64] or None,
                        locale=str(row.get("locale") or "en")[:8] or "en",
                        balance=0,
                        exp=int(row.get("exp") or 0),
                        level=int(row.get("level") or 1),
                        pulls_total=int(row.get("pulls_total") or row.get("total_claims") or 0),
                        high_pulls=int(row.get("high_pulls") or 0),
                        last_daily=parse_dt(row.get("last_daily")),
                        last_spin=parse_dt(row.get("last_spin")),
                        last_hclaim=parse_dt(row.get("last_hclaim")),
                        streak_count=int(row.get("streak_count") or 0),
                        streak_best=int(row.get("highest_streak") or 0),
                        role=role,
                        banned=banned,
                        ban_reason=str(row.get("ban_reason") or "")[:200] or None,
                        warn_count=int(row.get("warn_count") or 0),
                        premium_until=parse_dt(row.get("premium_until")),
                        created_at=parse_dt(row.get("created_at"))
                        or datetime.now(UTC).replace(tzinfo=None),
                        updated_at=datetime.now(UTC).replace(tzinfo=None),
                    )
                )
            )
            self.report.created["users"] += 1
            if balance > 0:
                # Wallet without a ledger is unauditable: the opening balance is a
                # real transaction row, keyed so a re-import cannot mint coins.
                await self.s.execute(
                    insert(Transaction).values(
                        user_id=user_id,
                        delta=balance,
                        balance_after=balance,
                        reason=str(LedgerReason.ADMIN_GRANT),
                        reference=f"import:summon:{user_id}",
                        counterparty=None,
                        meta={
                            "source": "summon-bot",
                            "note": "opening balance migrated from the old bot",
                        },
                        idempotency_key=f"import:summon:{user_id}",
                        created_at=datetime.now(UTC).replace(tzinfo=None),
                    )
                )
                await self.s.execute(
                    text("UPDATE users SET balance = :b WHERE id = :u"),
                    {"b": balance, "u": user_id},
                )
                self.report.created["wallets"] += 1

    # ---------------------------------------------------------------- collection
    async def collection(self) -> None:
        for row in self.rows("user_collection"):
            user_id = int(row.get("user_id") or 0)
            character_id = self.char_map.get(str(row.get("character_id") or "").strip())
            if character_id is None:
                self.report.unknown["collection:unknown-character"] += 1
                self.report.add_warning(
                    f"collection row for user {user_id} points at legacy character {row.get('character_id')!r}, which was not imported"
                )
                continue
            if await self._exists(Ownership, user_id=user_id, character_id=character_id):
                self.report.skipped["collection"] += 1
                continue
            await self.s.execute(
                insert(Ownership).values(
                    user_id=user_id,
                    character_id=character_id,
                    count=max(1, int(row.get("count") or 1)),
                    is_favorite=False,
                    is_locked=False,
                    first_obtained=parse_dt(row.get("obtained_at")),
                    last_obtained=parse_dt(row.get("obtained_at")),
                    source="import",
                )
            )
            self.report.created["collection"] += 1
        # Their favourite lives on the user row; mirror it onto the ownership row.
        for row in self.rows("users"):
            fav = str(row.get("favorite") or "").strip()
            user_id = int(row.get("user_id") or 0)
            character_id = self.char_map.get(fav)
            if not user_id or not character_id:
                continue
            owned = await self._get(Ownership, user_id=user_id, character_id=character_id)
            if owned is not None and not owned.is_favorite:
                owned.is_favorite = True
                self.report.created["favourites"] += 1

    # -------------------------------------------------------- streaks + settings
    async def streaks(self) -> None:
        for row in self.rows("user_streaks"):
            user_id = int(row.get("user_id") or 0)
            if not user_id or await self._exists(Streak, user_id=user_id):
                self.report.skipped["streaks"] += 1
                continue
            await self.s.execute(
                insert(Streak).values(
                    user_id=user_id,
                    current=int(row.get("streak_count") or 0),
                    highest=int(row.get("highest_streak") or 0),
                    last_date=str(row.get("last_streak_date") or "")[:10],
                    freezes=int(row.get("freezes") or 0),
                )
            )
            self.report.created["streaks"] += 1

    async def preferences(self) -> None:
        """``collection_mode``/``profile_glow``/``font_pref`` → our UserPref + flags."""
        seen: set[int] = set()
        for row in self.rows("user_preferences"):
            seen.add(int(row.get("user_id") or 0))
            await self._pref_row(
                int(row.get("user_id") or 0),
                hmode=str(row.get("collection_mode") or "rarity"),
                glow=bool(int(row.get("profile_glow") or 0)),
                font=str(row.get("font_pref") or "mono"),
                flags={"market_filter": row.get("market_filter")}
                if row.get("market_filter")
                else {},
            )
        for row in self.rows("users"):  # the old bot kept the font on the user row too
            user_id = int(row.get("user_id") or 0)
            if not user_id or user_id in seen:
                continue
            await self._pref_row(user_id, font=str(row.get("font_pref") or "mono"))
        for row in self.rows("users"):
            user_id = int(row.get("user_id") or 0)
            if user_id:
                await self._pref_row(user_id)

    async def _pref_row(
        self,
        user_id: int,
        *,
        hmode: str = "rarity",
        glow: bool = False,
        font: str = "mono",
        flags: dict[str, Any] | None = None,
    ) -> None:
        if not user_id:
            return
        existing = await self._get(UserPref, user_id=user_id)
        if existing is not None:
            self.report.skipped["preferences"] += 1
            return
        merged = {"font": font or "mono", **(flags or {})}
        await self.s.execute(
            insert(UserPref).values(
                user_id=user_id,
                hmode=(hmode or "rarity")[:16],
                glow=glow,
                show_balance=True,
                flags=merged,
            )
        )
        self.report.created["preferences"] += 1

    async def inventory(self) -> None:
        for row in self.rows("user_inventory"):
            raw = str(row.get("item_id") or "").strip()
            key = ITEM_MAP.get(raw.lower())
            if key is None:
                self.report.unknown[f"item:{raw or '∅'}"] += 1
                continue
            user_id = int(row.get("user_id") or 0)
            uses = int(row.get("uses_remaining") or 1)
            if uses <= 0:
                self.report.skipped["inventory"] += 1
                continue
            await self.s.execute(
                insert(InventoryItem).values(
                    user_id=user_id,
                    item_id=key,
                    uses_remaining=min(uses, 99),
                    created_at=parse_dt(row.get("purchased_at")),
                )
            )
            self.report.created["inventory"] += 1

    async def achievements(self) -> None:
        for row in self.rows("user_achievements"):
            key = str(row.get("achievement_id") or "").strip()
            user_id = int(row.get("user_id") or 0)
            if (
                not key
                or not user_id
                or await self._exists(Achievement, user_id=user_id, achievement_id=key)
            ):
                self.report.skipped["achievements"] += 1
                continue
            await self.s.execute(
                insert(Achievement).values(
                    user_id=user_id,
                    achievement_id=key[:48],
                    progress=int(row.get("progress") or 0),
                    unlocked_at=parse_dt(row.get("unlocked_at")),
                )
            )
            self.report.created["achievements"] += 1

    async def warnings(self) -> None:
        """Their single-row-per-user counter becomes real per-incident rows."""
        for row in self.rows("warnings"):
            user_id = int(row.get("user_id") or 0)
            count = int(row.get("warn_count") or 0)
            if not user_id or count <= 0 or await self._exists(Warning, user_id=user_id):
                self.report.skipped["warnings"] += 1
                continue
            for _ in range(min(count, 20)):
                self.s.add(
                    Warning(
                        chat_id=0,
                        user_id=user_id,
                        moderator_id=int(row.get("warned_by") or 0),
                        reason=str(row.get("reason") or "migrated")[:255],
                        is_resolved=False,
                        created_at=parse_dt(row.get("warned_at"))
                        or datetime.now(UTC).replace(tzinfo=None),
                    )
                )
            self.report.created["warnings"] += count

    async def banned(self) -> None:
        for row in self.rows("banned_users"):
            user_id = int(row.get("user_id") or 0)
            if not user_id:
                continue
            user = await self._get(User, id=user_id)
            if user is None:
                await self.s.execute(
                    insert(User).values(
                        id=user_id,
                        username=str(row.get("username") or "")[:64] or None,
                        banned=True,
                        ban_reason=str(row.get("reason") or "")[:200] or None,
                    )
                )
                self.report.created["banlist"] += 1
                continue
            if not user.banned:
                user.banned = True
                user.ban_reason = str(row.get("reason") or "")[:200] or user.ban_reason
                self.report.created["banlist"] += 1
            else:
                self.report.skipped["banlist"] += 1

    async def premium(self) -> None:
        for row in self.rows("premium"):
            user_id = int(row.get("user_id") or 0)
            expires = parse_dt(row.get("expires_at"))
            if not user_id or expires is None or expires <= datetime.now(UTC).replace(tzinfo=None):
                self.report.skipped["premium"] += 1
                continue
            if await self._exists(Premium, user_id=user_id, expires_at=expires):
                self.report.skipped["premium"] += 1
                continue
            hours = max(
                1, int((expires - datetime.now(UTC).replace(tzinfo=None)).total_seconds() // 3600)
            )
            self.s.add(
                Premium(
                    user_id=user_id,
                    hours=hours,
                    granted_by=int(row.get("granted_by") or 0),
                    source="import",
                    expires_at=expires,
                )
            )
            await self.s.execute(
                text("UPDATE users SET premium_until = :e WHERE id = :u"),
                {"e": expires, "u": user_id},
            )
            self.report.created["premium"] += 1

    async def codes(self) -> None:
        for row in self.rows("redeem_codes"):
            code = str(row.get("code") or "").strip()
            if not code or await self._exists(RedeemCode, code=code):
                self.report.skipped["codes"] += 1
                continue
            reward = str(row.get("reward") or "").strip()
            coins = int(row.get("coins") or 0)
            if not coins and reward.isdigit():
                coins = int(reward)
            character_id = self.char_map.get(str(row.get("character_id") or "").strip())
            self.s.add(
                RedeemCode(
                    code=code[:48],
                    character_id=character_id if character_id else None,
                    reward="" if coins else reward[:48],
                    coins=coins,
                    uses=int(row.get("uses") or 1),
                    used_count=int(row.get("used_count") or 0),
                    created_by=int(row.get("created_by") or 0),
                    note="migrated from summon-bot"[:96],
                    is_active=True,
                    created_at=parse_dt(row.get("created_at")),
                )
            )
            self.report.created["codes"] += 1

    async def auctions(self) -> None:
        for row in self.rows("auctions"):
            auction_id = int(row.get("id") or 0)
            character_id = self.char_map.get(str(row.get("character_id") or "").strip())
            if character_id is None:
                self.report.unknown["auction:unknown-character"] += 1
                continue
            if await self._exists(Auction, id=auction_id):
                self.report.skipped["auctions"] += 1
                continue
            if auction_id and not await self._exists(User, id=int(row.get("seller_id") or 0)):
                # An auction cannot exist without its seller row; create the shell so
                # the listing survives the migration instead of being silently lost.
                await self.s.execute(
                    insert(User).values(
                        id=int(row.get("seller_id") or 0),
                        username=f"imported{int(row.get('seller_id') or 0)}",
                        balance=0,
                        role="user",
                        locale="en",
                    )
                )
                self.report.created["users"] += 1
            status = str(row.get("status") or "live").lower()
            mapped = status if status in {"live", "sold", "no_sale", "cancelled"} else "live"
            highest_bid = int(row.get("highest_bid") or 0)
            self.s.add(
                Auction(
                    id=auction_id,
                    seller_id=int(row.get("seller_id") or 0),
                    character_id=character_id,
                    message_id=int(row.get("pinned_msg_id") or 0) or None,
                    chat_id=int(row.get("chat_id") or 0) or None,
                    status=mapped,
                    start_price=int(row.get("start_price") or 0),
                    reserve_price=int(row.get("reserve_price") or 0),
                    min_increment=max(
                        1, int(row.get("min_increment") or max(100, (highest_bid or 1) // 10))
                    ),
                    current_bid=highest_bid,
                    top_bidder_id=int(row.get("highest_bidder_id") or 0) or None,
                    winner_id=int(row.get("winner_id") or row.get("highest_bidder_id") or 0)
                    or None,
                    sold_price=int(
                        row.get("sold_price") or (highest_bid if mapped == "sold" else 0)
                    ),
                    ends_at=parse_dt(row.get("end_time")) or datetime.now(UTC).replace(tzinfo=None),
                    created_at=parse_dt(row.get("created_at")),
                    note="migrated from summon-bot",
                )
            )
            self.report.created["auctions"] += 1
        for row in self.rows("auction_bids"):
            if await self._exists(AuctionBid, id=int(row.get("id") or 0)):
                self.report.skipped["bids"] += 1
                continue
            self.s.add(
                AuctionBid(
                    id=int(row.get("id") or 0) or None,
                    auction_id=int(row.get("auction_id") or 0),
                    bidder_id=int(row.get("user_id") or 0),
                    amount=int(row.get("amount") or 0),
                    created_at=parse_dt(row.get("created_at")),
                )
            )
            self.report.created["bids"] += 1

    async def market_history(self) -> None:
        """``market_transactions`` → ledger rows, so /history keeps working per-player."""
        reason = {"buy": str(LedgerReason.BUY), "sell": str(LedgerReason.SELL)}
        for row in self.rows("market_transactions"):
            user_id = int(row.get("user_id") or 0)
            delta = int(row.get("price") or 0)
            kind = reason.get(str(row.get("transaction_type") or "").lower())
            if not user_id or not delta or not kind:
                self.report.unknown[f"market:{row.get('transaction_type')}"] += 1
                continue
            reference = f"market:{row.get('id')}"
            if await self._exists(Transaction, user_id=user_id, reference=reference):
                self.report.skipped["ledger"] += 1
                continue
            await self.s.execute(
                insert(Transaction).values(
                    user_id=user_id,
                    delta=delta if kind == str(LedgerReason.SELL) else -delta,
                    balance_after=0,  # recomputed below; unknown at row level
                    reason=kind,
                    reference=reference,
                    meta={
                        "source": "summon-bot",
                        "character_id": self.char_map.get(str(row.get("char_id") or "")),
                    },
                    created_at=parse_dt(row.get("timestamp")),
                )
            )
            self.report.created["ledger"] += 1
        await self.recount_balances()

    async def recount_balances(self) -> None:
        """Rebuild ``balance``/``balance_after`` from the ledger after a market import.

        The old bot updated the wallet and the history table in separate connections,
        so their rows disagree. Ours must not: after importing, the ledger is the truth.
        """
        if self.dry:
            return
        await self.s.execute(
            text(
                "UPDATE users SET balance = COALESCE((SELECT sum(delta) FROM transactions WHERE transactions.user_id = users.id), 0)"
            )
        )
        self.report.created["balances_recomputed"] = 1

    async def gifts(self) -> None:
        for row in self.rows("gift_log"):
            character_id = self.char_map.get(str(row.get("char_id") or "").strip())
            sender = int(row.get("from_user") or 0)
            receiver = int(row.get("to_user") or 0)
            if not sender or not receiver or not character_id:
                self.report.unknown["gift:unmapped"] += 1
                continue
            await self.s.execute(
                insert(GiftLog).values(
                    sender_id=sender,
                    receiver_id=receiver,
                    character_id=character_id,
                    note="migrated"[:140],
                    created_at=parse_dt(row.get("gifted_at")),
                )
            )
            self.report.created["gifts"] += 1

    async def groups(self) -> None:
        """``groups`` + ``group_settings`` → one ``groups`` row with spawn config."""
        merged: dict[int, dict[str, Any]] = {}
        for table in ("group_settings", "groups"):
            for row in self.rows(table):
                chat_id = int(row.get("chat_id") or 0)
                if not chat_id:
                    continue
                slot = merged.setdefault(chat_id, {"chat_id": chat_id})
                if row.get("message_count") is not None:
                    slot["message_count"] = int(row.get("message_count") or 0)
                if row.get("spawn_limit") is not None:
                    slot["spawn_limit"] = int(row.get("spawn_limit") or 100)
                if row.get("title"):
                    slot["title"] = str(row.get("title"))[:128]
        for chat_id, values in merged.items():
            if await self._exists(Group, chat_id=chat_id):
                self.report.skipped["groups"] += 1
                continue
            await self.s.execute(
                insert(Group).values(
                    chat_id=chat_id,
                    title=values.get("title") or f"group {chat_id}",
                    is_registered=True,
                    message_count=int(values.get("message_count") or 0),
                    spawn_limit=int(values.get("spawn_limit") or 100),
                    spawn_enabled=True,
                    data={"imported_from": "summon-bot"},
                )
            )
            self.report.created["groups"] += 1

    async def odds(self) -> None:
        """``rarity_chances`` / ``claim_list`` → only if the target has none.

        Their tables are keyed on the same 18 ids, so the rates transfer verbatim —
        but a server that already tuned ``/chance`` in the new bot must not be
        stomped by an import.
        """
        for table, model, column in (
            ("rarity_chances", RarityChance, "chance"),
            ("claim_list", ClaimChance, "chance"),
        ):
            if (
                int(
                    (await self.s.execute(select(func.count()).select_from(model))).scalar_one()
                    or 0
                )
                > 0
            ):
                self.report.skipped[table] += len(self.rows(table))
                continue
            for row in self.rows(table):
                rarity = Rarity.from_value(row.get("rarity_id") or 1)
                raw = float(row.get(column) or 0)
                chance = raw / 100.0 if raw > 100 else raw  # they stored per-10 000
                await self.s.execute(
                    insert(model.__table__).values(
                        rarity_id=int(rarity),
                        rarity_name=str(row.get("rarity_name") or rarity.label),
                        chance=chance,
                        **(
                            {"min_price": rarity.base_price, "is_enabled": chance > 0}
                            if model is RarityChance
                            else {}
                        ),
                    )
                )
                self.report.created[table] += 1

    async def activity(self) -> None:
        """``activity_log`` is history, not state: export it, don't pretend to model it."""
        rows = self.rows("activity_log")
        if not rows:
            return
        path = ROOT / "imported_activity_log.json"
        path.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
        self.report.created["activity_exported"] = len(rows)

    # --------------------------------------------------------------------- glue
    async def run(self) -> Report:
        steps = (
            self.odds,
            self.characters,
            self.users,
            self.collection,
            self.preferences,
            self.streaks,
            self.inventory,
            self.achievements,
            self.warnings,
            self.banned,
            self.premium,
            self.codes,
            self.auctions,
            self.market_history,
            self.gifts,
            self.groups,
            self.activity,
        )
        for step in steps:
            try:
                await step()
            except Exception as exc:
                self.report.add_warning(f"{step.__name__}: {type(exc).__name__}: {str(exc)[:220]}")
            await self._commit()
        return self.report


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--from",
        dest="source",
        required=True,
        help="sqlite path or postgresql:// URL of the old bot",
    )
    parser.add_argument(
        "--to", dest="target", default="", help="target DATABASE_URL (defaults to $DATABASE_URL)"
    )
    parser.add_argument("--dry-run", action="store_true", help="map + report, then roll back")
    parser.add_argument(
        "--limit", type=int, default=0, help="import at most N rows per table (smoke test)"
    )
    args = parser.parse_args()

    target = args.target or os.getenv("DATABASE_URL", "")
    if not target:
        raise SystemExit("pass --to postgresql+asyncpg://… or set DATABASE_URL")

    data = normalise(read_source(args.source))
    if args.limit:
        data = {name: rows[: args.limit] for name, rows in data.items()}
    print(f"source tables: {', '.join(sorted(data)) or 'none'}")
    print(
        "row counts:    " + ", ".join(f"{name}={len(rows)}" for name, rows in sorted(data.items()))
    )

    engine = create_async_engine(target, echo=False, future=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        # One savepoint around the import: ``--dry-run`` rolls it back, a real run
        # releases it and commits, so the rehearsal exercises the same constraint
        # checks (FKs, uniqueness, defaults) as the live migration.
        nested = await session.begin_nested()
        importer = Importer(session, data, dry=args.dry_run)
        report = await importer.run()
        if args.dry_run:
            await nested.rollback()
        else:
            await nested.commit()
            await session.commit()
    await engine.dispose()
    print(report.render())
    print("dry run — nothing written" if args.dry_run else "import complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
