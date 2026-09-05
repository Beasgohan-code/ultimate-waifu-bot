# Waifu

A Telegram gacha-and-economy bot: character collection, free daily claims, a group spawn
feed, an item shop, player auctions and trades, streaks, quests, badges, leaderboards,
moderation with an audit trail, and Telegram Stars payments — on **aiogram 3.31 / Bot API
10.3**, with async SQLAlchemy, versioned migrations, per-chat rate limits and a test suite
that runs the real object graph.

It is a rewrite of [`Beasgohan-code/Summon-bot`](https://github.com/Beasgohan-code/Summon-bot)
built against one promise: **the same commands and the same collection pages, plus new
features on top** — not instead. Every number in [docs/SUMMON_PARITY.md](docs/SUMMON_PARITY.md)
comes from that bot (`config.py`, its 18-tier ladder and its economy constants), so a group
that switches over notices no price, no odds and no missing command; the audit of what it
found is in the same file.

**No placeholder data.** A fresh install seeds the 177-character catalogue and the 18-tier
price/odds ladder from `waifu/data/characters.seed.json` — it is not "add your own
characters", and there is no empty database to fill in. If you are replacing a live
Summon-bot, `python -m waifu import-legacy ./summon.db` migrates its 24 tables (wallets,
collections, favourites, escrow, codes, boosts) idempotently.

## Run it

```bash
cp .env.example .env          # BOT_TOKEN, DATABASE_URL (Postgres), REDIS_URL
docker compose up --build     # bot + postgres + redis
```

Or without Docker:

```bash
make install
python -m waifu doctor        # config + schema + plugin-registration self-check
python -m waifu migrate       # schema, then the shipped catalogue
python -m waifu import-legacy ./summon.db --dry-run   # optional: your old data
make run
```

Postgres and Redis are required at runtime (`WAIFU_TEST_MODE=1` unlocks SQLite for tests
and local poking only — the same code path, no second implementation). The timer loop
(spawn feed, auction settlement, expiries, raffle draws) runs in-process; to drive it from
cron instead, set `NO_JOBS=1` and run `python -m waifu jobs --name autospawn`.

## Layout

```
waifu/
  core/        app assembly, dispatcher + plugin loading, middlewares, access/permissions,
               context (the service registry), jobs (the timer loop), CLI (waifu/__main__.py)
  db/          engine, models, versioned migrations, seed (tier ladders + catalogue),
               repositories/ (all SQL lives here), cache, redis client
  services/    economy, gacha, collection, items, spawn, auction, trading, codes, gifts,
               progress, stats, moderation, premium, ai, cards, hstats — no Telegram types
  tg/          one module per new Bot API surface: rich messages, drafts, ephemerals,
               checklists, guest mode, business connection, paid media/Stars, stories,
               live photos, reactions, capability negotiation
  plugins/     22 routers: one file per feature area, handlers only
  ui/ enums.py errors.py settings.py logging.py utils/
tests/         fixtures wire the real services to in-memory SQLite; no mocked own-layers
docs/          generated command reference + the parity contract
scripts/       build_catalogue.py (roster), import_summon.py (migration), gen_reference_docs.py
```

The dependency rule, enforced by how things are imported: `plugins → services → db`, and
`plugins → tg` for rendering. Services never touch aiogram types; handlers never write SQL
(except the admin roster CRUD, which is documented where it happens); nothing commits
outside the session middleware.

## New Bot API features, and how they degrade

Rich messages (10.1), message drafts (9.5/10.3), ephemeral messages, checklists, live
photos, guest mode (10.0), business connection (10.2), Stars + paid media, boosts, member
tags, `date_time` entities, custom emoji in bot messages (9.4), reaction and poll updates,
prepared inline messages for sharing a harem. `python -m waifu` negotiates them once at
startup (`waifu/tg/caps.py`) and each renderer picks the plain path when the server does
not have them — so an instance on a self-hosted API server of last year loses formatting,
not functionality. Details: [docs/NEW_BOT_API.md](docs/NEW_BOT_API.md).

## Commands

[docs/COMMANDS.md](docs/COMMANDS.md) is generated from the routers, so it cannot drift;
`/help` is curated by topic with a generated `more` page, `/commands` sends the complete
list as a file, and Telegram's ⊞ menu is published at startup from the same registry
(the player scope and the group-admin scope share 100 entries each, deliberately).

## Tests

```bash
make test    # 154 tests: pulls and pity, economy invariants, escrow, paging, migrations
make lint    # ruff + ruff format + "generated docs are current"
make check   # both, which is what CI runs
```

Two of them are worth naming because they are the reason the rest can move fast: the ledger
reconciliation test asserts every coin in the database has a transaction behind it (the
reference bot's balance column drifted, which is how it got farmed), and
`tests/test_summon_parity.py` is the parity contract — every command name Summon-bot
registers must resolve here or be documented as renamed with a reason.
