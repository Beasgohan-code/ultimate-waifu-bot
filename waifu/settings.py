"""Typed configuration — the only place environment variables are read.

Two deliberate, opinionated constraints (per the "full PSQL + Redis" brief):

* ``DATABASE_URL`` **must** be ``postgresql+asyncpg://…`` — no SQLite runtime path.
* ``REDIS_URL`` **must** be set and reachable — FSM, cooldowns, queues, rate
  limits and leaderboards live there.

Both are validated at startup with an actionable message, and the only escape
hatch is ``WAIFU_TEST_MODE=1`` (used by the unit-test harness, never by a bot).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent

TEST_ENV_FLAG = "WAIFU_TEST_MODE"


def in_test_mode() -> bool:
    return os.environ.get(TEST_ENV_FLAG, "").strip().lower() in {"1", "true", "yes"}


class CoinPack(BaseModel):
    """A Telegram Stars purchase: N stars -> coins (+ optional premium hours)."""

    id: str
    label: str = ""
    stars: int = Field(ge=1)
    coins: int = Field(default=0, ge=0)
    premium_hours: int = Field(default=0, ge=0)
    item_id: str = ""

    @property
    def title(self) -> str:
        return self.label or self.id.replace("_", " ").title()

    @property
    def value_ratio(self) -> float:
        return self.coins / self.stars if self.stars else 0.0


class AISettings(BaseModel):
    enabled: bool = True
    api_base: str = "https://api.openai.com/v1"
    api_key: str = ""
    model: str = "gpt-4o-mini"
    max_tokens: int = 400
    daily_char_budget: int = 6000
    moderation: bool = True

    @property
    def configured(self) -> bool:
        return self.enabled and bool(self.api_key)


class FeatureFlags(BaseModel):
    """Plugins are gated by flags so an owner can enable features progressively.

    Env form is ``FEATURE_<NAME>=true|false``.
    """

    ai: bool = True
    items: bool = True  # /market shop, /steal, /bomb, /skip, shields
    auction: bool = True
    trade: bool = True  # escrow offers driven from /gift buttons
    hstats: bool = True
    streak: bool = True
    achievements: bool = True
    premium: bool = True
    stars: bool = True  # Telegram Stars invoices
    paid_media: bool = True  # sendPaidMedia motion cards
    boosts: bool = True  # chat-boost perks
    reactions: bool = True  # react-to-enter raffles + setMessageReaction
    games: bool = True  # /nguess
    inline: bool = True
    guest_mode: bool = True
    business_mode: bool = True
    topics: bool = True  # forum topics in private chats
    cards: bool = True  # Pillow-rendered cards when no art exists
    stickers: bool = False
    autoban: bool = True
    autospawn: bool = True
    fair_mode: bool = True  # commit/reveal verification buttons
    # --- Brand new Bot API surfaces -----------------------------------------
    rich_messages: bool = True  # sendRichMessage + InputRichMessage w/ media
    draft_stream: bool = True  # sendMessageDraft (typewriter) for AI/broadcast
    ephemeral: bool = True  # EphemeralMessageParameters (private, in-place)
    checklist: bool = True  # sendChecklist for the daily task board
    media_polls: bool = True  # sendPoll with per-option media
    live_photos: bool = True  # sendLivePhoto character "motion" cards
    member_tags: bool = True  # setChatMemberTag for roles/whales in groups
    disabled_buttons: bool = True  # DisabledButton on spent/sold/claimed buttons
    button_styles: bool = True  # InlineKeyboardButton.style + icon_custom_emoji_id

    def is_enabled(self, name: str) -> bool:
        return bool(getattr(self, name, False))

    def enabled_list(self) -> list[str]:
        return sorted(k for k, v in self.model_dump().items() if v)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    # --- Telegram -------------------------------------------------------------
    bot_token: str = ""
    owner_id: int = 0
    admin_ids: list[int] = Field(default_factory=list)
    bot_username: str = "UltimateWaifuBot"
    #: Display name in headers and /start. Not the Telegram-side name (that is set with
    #: ``setMyName`` in :func:`waifu.core.bot.apply_identity`); this is what *we* print.
    bot_title: str = "Ultimate Waifu Bot"
    #: ``/update`` shows the tail of this file (the reference bot pasted a hard-coded
    #: changelog string that went stale within a release).
    changelog_path: str = "docs/CHANGELOG.md"
    support_url: str = ""
    channel_url: str = ""
    group_url: str = ""

    mode: Literal["polling", "webhook"] = "polling"
    webhook_secret: str = ""
    webhook_url: str = ""
    webhook_listen_host: str = "0.0.0.0"
    webhook_listen_port: int = 8081
    webhook_certificate: str = ""
    webhook_max_connections: int = 40
    webhook_drop_pending_updates: bool = False
    #: Drop updates queued while the bot was offline (polling). ``False`` is the
    #: default because replaying an hour of /daily presses is worse than missing them.
    drop_pending_updates: bool = False
    #: Push name/description/menu button from config on startup.
    set_menu_button: bool = True
    #: Publish the command menu (⊞) at startup, generated from the wired routers.
    set_command_menu: bool = True
    #: Run the timer loop in-process (spawns, settlements, expiries). Set when a cron
    #: job runs ``waifu jobs --name <pass>`` instead, so only one driver exists.
    no_jobs: bool = False
    allowed_updates: list[str] = Field(default_factory=list)  # empty -> derived from routers

    # Local Bot API server: required for >5MB sends/receives and heavy traffic.
    api_url: str = ""
    api_base: str = ""

    # --- Postgres (required) --------------------------------------------------
    database_url: str = ""
    db_echo: bool = False
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_statement_timeout_ms: int = 8000
    legacy_db_path: str = ""

    # --- Redis (required) -----------------------------------------------------
    redis_url: str = ""
    redis_max_connections: int = 64
    redis_key_prefix: str = "uwb"
    redis_fsm_ttl_days: int = 14

    # --- Economy --------------------------------------------------------------
    # Defaults mirror the reference bot's ``config.py`` so a migrated server keeps the
    # economy its players learned: STARTING_BALANCE=500, DAILY_REWARD=5000.
    starting_balance: int = 500
    daily_reward_min: int = 5000
    daily_reward_max: int = 5000
    daily_jackpot_chance: float = 0.05
    daily_jackpot_max: int = 15000
    spin_min: int = 100
    spin_max: int = 1000
    spin_bonus_min: int = 500
    spin_bonus_max: int = 2000
    spin_lucky_chance: float = 0.1
    dupe_payout_percent: int = 40  # dupe of a collected char pays 40% of its price
    shop_refresh_cost: int = 10000
    auction_fee_percent: int = 7
    gift_tax_percent: int = 0
    steal_min_target: int = 100
    steal_max_percent: int = 15
    bomb_min_rarity_id: int = 1
    cooldown_hours_daily: int = 24
    cooldown_hours_spin: int = 24
    cooldown_hours_hclaim: int = 24
    cooldown_seconds_bomb: int = 86400
    cooldown_seconds_steal: int = 3600
    pull_cooldown_seconds: int = 8
    pull_cost: int = 1000
    ten_pull_cost: int = 9000
    ten_pull_guarantee_rarity_id: int = 3  # a 10-pull never misses RARE and above
    spawn_min_interval: int = 1800
    spawn_max_interval: int = 5400
    spawn_claim_window: int = 180
    spawn_default_limit: int = 100
    # warnings count → punishment seconds (-1 = ban). Editable per deployment.
    warn_ladder_json: str = '{"3": 3600, "4": 86400, "5": -1}'
    spawn_high_tier_ceiling: int = 5  # /hclaim can never roll above this rarity id w/o premium
    #: ``GUESS_TIMEOUT`` / ``REWARD_COINS`` from the reference bot's /nguess.
    guess_timeout_seconds: int = 30
    guess_reward_coins: int = 20
    #: ``REACTIONS`` — the vote pool for /nguess and raffles.
    guess_reactions: list[str] = Field(
        default_factory=lambda: ["🔥", "🎉", "👍", "💯", "⚡", "🥳", "👀", "✨"]
    )
    #: ``SPAM_LIMIT``: messages per minute before the group autoban fires.
    spam_limit_per_minute: int = 20
    #: Auto-ban groups whose owner enabled it, after N strikes in the window.
    spam_auto_ban_strikes: int = 3
    auction_extend_seconds: int = 120
    auction_snipe_window: int = 180
    auction_max_extensions: int = 6
    trade_escrow_minutes: int = 30
    #: Coins for the inviter on a friend's first /start (``/invite``).
    referral_bonus: int = 1000

    #: Daily streak multiplier per consecutive day (last value repeats). Env form is
    #: JSON: ``ECONOMY_STREAK_MULTIPLIER_CURVE=[1,1.1,1.2,1.3,1.4,1.5,2]``.
    streak_multiplier_curve: list[float] = Field(
        default_factory=lambda: [1.0, 1.1, 1.2, 1.3, 1.4, 1.5, 2.0]
    )

    # --- Pity (addition to Summon-bot: bad luck streaks are compensated) -----
    pity_high_after: int = 25
    pity_celestial_after: int = 120
    premium_claim_boost_percent: int = 15

    # --- Monetisation ---------------------------------------------------------
    star_supports: bool = True
    #: Stars → coins (+ optional premium hours / item). Shipped non-empty on purpose:
    #: an empty shop with ``FEATURE_STARS=1`` is a "premium is broken" support ticket,
    #: which is why ``Settings.validate_runtime`` flags that combination.
    #: Env form is the same JSON string: COIN_PACK_JSON='[{"id":"mini",...}]'.
    coin_pack_json: str = (
        '[{"id":"starter","label":"5,000 coins","stars":50,"coins":5000},'
        '{"id":"value","label":"12,000 coins","stars":100,"coins":12000},'
        '{"id":"whale","label":"40,000 coins + 7d premium","stars":300,"coins":40000,"premium_hours":168}]'
    )
    paid_media_star_price: int = 45
    premium_sub_stars: int = 750
    premium_sub_days: int = 30
    premium_prices_json: str = "{}"

    # --- Media / rendering ----------------------------------------------------
    card_width: int = 900
    media_base_url: str = ""
    font_path: str = ""
    allowed_media_hosts: list[str] = Field(
        default_factory=lambda: ["api.telegram.org", "files.catbox.moe", "i.ibb.co", "catbox.moe"]
    )
    enable_outbound_media: bool = True
    # Rich messages need the *file_id* of the photo; this controls whether the bot
    # caches file_ids for URL-hosted art on first send (recommended: yes).
    cache_media_file_ids: bool = True

    # --- Behaviour ------------------------------------------------------------
    ai: AISettings = Field(default_factory=AISettings)
    features: FeatureFlags = Field(default_factory=FeatureFlags)
    default_locale: str = "en"
    timezone: str = "Asia/Kolkata"
    daily_reset_hour: int = 0
    log_level: str = "INFO"
    log_json: bool = False
    data_dir: Path = Path("./data")
    support_chat_id: int = 0
    #: Private chat where generated art is uploaded so it becomes a permanent
    #: file_id (``/archiveart`` uses this; leave 0 to disable archiving).
    # ---- mini app ----------------------------------------------------------------------
    #: The page a ``/webapp`` button opens, and the only origin the JSON API answers CORS for.
    #: Empty means "no front-end": ``/webapp`` explains itself instead of sending a button to a
    #: URL nobody configured, and the API takes no ``X-API-Token`` (see ``webapp_secret_key``).
    webapp_url: str = ""
    #: Shared secret for a front-end that is *not* a Telegram Mini App (an operator dashboard, a
    #: monitoring probe). Empty means the header is not accepted at all — signed ``initData``
    #: is the normal path and never needs this.
    webapp_secret_key: str = ""

    # ---- JSON API (the reference bot's Flask ``api.py``; see docs/API.md) ----------------
    # On by default *off*: the routes are a projection of tables a browser can reach, so they
    # only exist if the operator points a Mini App at them.
    api_enabled: bool = False
    # 0.0.0.0 because a container has no other address worth binding; the port is only open at
    # all when API_ENABLED is set, and every route but /api/health needs a signature.
    api_host: str = "0.0.0.0"
    api_port: int = 8080
    #: ``?uid=`` without a signature: development only, and refused when the bot is not in
    #: test mode. The reference accepted it always, which is the hole in that design.
    api_allow_uid_query: bool = False
    #: How old a signed ``initData`` may be before it is treated as a leaked credential.
    api_init_data_max_age: int = 24 * 60 * 60

    media_archive_chat_id: int = 0
    log_channel_id: int = 0

    # --- Roster ingestion -----------------------------------------------------
    #: A fresh install ships an **empty** character database, exactly like the
    #: reference deployment: its ``summon.db`` contained one row because *admins added
    #: characters at runtime*, not because the code shipped a list. ``waifu migrate``
    #: therefore writes the tier ladder and prices (configuration) and nothing else.
    #: Set ``SEED_CATALOGUE=1`` to also load ``waifu/data/characters.seed.json`` — the
    #: optional 177-entry catalogue — on migrate/seed, or use ``/upload``/``waifu seed
    #: --catalogue``/``waifu import-legacy`` when you want the roster built your way.
    seed_catalogue: bool = False
    #: Where ``/upload`` parks the downloaded media until the admin chooses a web host
    #: (the reference bot hard-coded ``~/summon-bot/uploads``).
    upload_dir: Path = Path("./data/uploads")
    #: ImgBB needs an API key; Catbox is anonymous, so it is always offered. Both are
    #: only reachable by the owner through the ``/upload`` receipt buttons.
    imgbb_api_key: str = ""
    #: Post every ingested character (media + caption) to ``LOG_CHANNEL_ID`` so the
    #: channel stays the archive the DB was rebuilt from, as in the reference bot.
    upload_to_log_channel: bool = True

    # -------------------------------------------------------------- validators
    @field_validator("bot_token")
    @classmethod
    def _validate_token(cls, raw: str) -> str:
        raw = (raw or "").strip()
        if raw and raw != "PUT_YOUR_BOT_TOKEN_HERE" and ":" not in raw:
            raise ValueError(
                "BOT_TOKEN must look like '<digits>:<secret>' (get one from @BotFather)"
            )
        return raw

    @field_validator("database_url")
    @classmethod
    def _validate_db(cls, raw: str) -> str:
        raw = (raw or "").strip()
        if raw.startswith("sqlite") and not in_test_mode():
            raise ValueError(
                "SQLite is not a supported runtime database. Set DATABASE_URL to "
                "postgresql+asyncpg://user:pass@host:5432/waifu (see docker-compose.yml)."
            )
        if raw and not raw.startswith(("postgresql+asyncpg://", "sqlite+aiosqlite://")):
            raise ValueError("DATABASE_URL must use the postgresql+asyncpg:// scheme.")
        return raw

    @field_validator("redis_url")
    @classmethod
    def _validate_redis(cls, raw: str) -> str:
        raw = (raw or "").strip()
        if not raw and not in_test_mode():
            raise ValueError(
                "REDIS_URL is required (FSM storage, cooldowns, spawn queue, rate limits). "
                "Example: redis://localhost:6379/0"
            )
        if raw and not raw.startswith(("redis://", "rediss://", "unix://")):
            raise ValueError("REDIS_URL must start with redis://, rediss:// or unix://")
        return raw

    @field_validator("admin_ids", "allowed_media_hosts", mode="before")
    @classmethod
    def _parse_lists(cls, raw: object) -> object:
        if raw in (None, ""):
            return []
        if isinstance(raw, str):
            return [p for part in (raw.split(","),) for p in [x.strip() for x in part] if p]
        return raw

    @field_validator("admin_ids", mode="after")
    @classmethod
    def _coerce_admin_ids(cls, raw: list) -> list[int]:
        return [int(x) for x in raw]

    @model_validator(mode="after")
    def _post(self) -> Self:
        if self.mode == "webhook":
            if not self.webhook_secret:
                raise ValueError("MODE=webhook requires WEBHOOK_SECRET (blocks forged updates).")
            if not self.webhook_url.startswith("https://"):
                raise ValueError("WEBHOOK_URL must be an https:// URL.")
        if self.sqlite_path is not None and not in_test_mode():  # defensive
            raise ValueError("SQLite runtime is not supported.")
        self.data_dir = Path(self.data_dir)
        if not self.data_dir.is_absolute():
            self.data_dir = PROJECT_ROOT / self.data_dir
        return self

    # --------------------------------------------------------------- accessors
    @property
    def warn_ladder(self) -> dict[int, int]:
        """Warning count → punishment length in seconds (-1 = permanent ban)."""
        try:
            raw = json.loads(self.warn_ladder_json or "{}")
        except (TypeError, ValueError):
            return {3: 3600, 4: 86400, 5: -1}
        return {int(k): int(v) for k, v in raw.items()} or {3: 3600, 4: 86400, 5: -1}

    @property
    def redis_dsn(self) -> str:
        """Redis URL when it is usable (empty string = run without Redis)."""
        return (
            self.redis_url
            if self.redis_url and not self.redis_url.startswith(("dummy://", "memory://"))
            else ""
        )

    @property
    def db_driver(self) -> str:
        """``postgres`` / ``sqlite`` — for logs and the doctor, never for SQL."""
        if self.is_postgres:
            return "postgres"
        if self.database_url.startswith("sqlite"):
            return "sqlite"
        return "none"

    @property
    def is_postgres(self) -> bool:
        return self.database_url.startswith("postgresql")

    @property
    def sqlite_path(self) -> Path | None:
        prefix = "sqlite+aiosqlite:///"
        if not self.database_url.startswith(prefix):
            return None
        path = Path(self.database_url[len(prefix) :])
        return path if (path.is_absolute() or ":memory:" in str(path)) else PROJECT_ROOT / path

    @property
    def coin_packs(self) -> list[CoinPack]:
        """The Stars shop, validated (a malformed COIN_PACK_JSON is a config error)."""
        raw = self.coin_pack_json
        if isinstance(raw, str):
            raw = raw.strip()
            if not raw:
                return []
            payload = json.loads(raw)
        else:  # tolerate a pre-parsed list from a .env loader or a test
            payload = list(raw or [])
        if isinstance(payload, dict):
            payload = [payload]
        return [CoinPack(**item) for item in payload]

    @property
    def owner_ids(self) -> set[int]:
        return ({self.owner_id} | set(self.admin_ids)) - {0}

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.owner_ids

    def key(self, *parts: str | int) -> str:
        """Namespaced Redis key: ``uwb:spawn:queue`` etc."""
        return ":".join((self.redis_key_prefix, *(str(p) for p in parts)))

    def validate_runtime(self) -> list[str]:
        """Startup checklist; each entry is a human-readable problem. Empty = good."""
        problems: list[str] = []
        if not self.bot_token or self.bot_token == "PUT_YOUR_BOT_TOKEN_HERE":
            problems.append("BOT_TOKEN is missing — create one with @BotFather.")
        if self.owner_id <= 0:
            problems.append("OWNER_ID must be your numeric Telegram id (use @userinfobot).")
        if not self.database_url:
            problems.append("DATABASE_URL is missing (postgresql+asyncpg://…).")
        elif not self.is_postgres:
            problems.append("DATABASE_URL must be postgresql+asyncpg://…; SQLite is test-only.")
        if not self.redis_url:
            problems.append("REDIS_URL is missing (redis://localhost:6379/0).")
        if self.mode == "webhook" and not self.webhook_secret:
            problems.append("WEBHOOK_SECRET is required in webhook mode.")
        if not self.coin_packs and self.features.stars:
            problems.append(
                "FEATURE_STARS is on but COIN_PACK_JSON is empty (the Stars shop would be blank)."
            )
        return problems


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()  # type: ignore[call-arg]
    return _settings


def reload_settings() -> Settings:  # pragma: no cover - test helper
    global _settings
    _settings = None
    return get_settings()
