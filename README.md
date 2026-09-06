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

**Nothing is invented for you.** A fresh install seeds the *configuration* — the 18-tier
ladder with its published odds, claim rates and prices — and an **empty character table**,
because that is how the reference deployment actually worked: its roster was not source
code, it was what its admins uploaded, one message at a time. So characters come in through
the bot itself:

```
/upload Gojo jujutsu-kaisen 4      ← sent as a reply to a photo, video, GIF or live photo
```

The media is stored as a permanent `file_id` (never a hotlinked URL), price and power are
taken from the tier, the same name again updates the row instead of duplicating it, and the
receipt offers Catbox/ImgBB if you also want a public URL — the reference bot's
`msg_id`/`img_url`/`img_url2` triple, on columns that say what they are. `/autoadd on` in a
group or channel turns it into a feed: any captioned media an admin posts is filed, with an
ephemeral receipt only the uploader sees.

Two ways in that do not need a phone: `python -m waifu seed --catalogue` loads the optional
177-entry reference catalogue (`waifu/data/characters.seed.json`, `SEED_CATALOGUE=1` to make
it automatic), and `python -m waifu import-legacy ./summon.db` migrates a live Summon-bot's
24 tables (characters, wallets, collections, favourites, escrow, codes, boosts) idempotently.

## Run it

```bash
cp .env.example .env          # BOT_TOKEN, DATABASE_URL (Postgres), REDIS_URL
docker compose up --build     # bot + postgres + redis
```

Or without Docker:

```bash
make install
python -m waifu doctor        # config + schema + plugin-registration self-check
python -m waifu migrate       # schema + tier ladders; the roster is yours to upload
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
  plugins/     23 routers: one file per feature area, handlers only
               (uploads.py is the exception the design allows: the admin roster pipeline
               writes through the characters repository because there is no service for
               "ingest this media" — and it is where an empty database stops being one)
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

Rich messages (10.1), message drafts (9.5/10.3), ephemeral messages (10.2/10.3 — also how
`/autoadd` acknowledges an ingest without turning the group into a console), checklists,
live photos (9.1 — `/upload` files a live photo with its motion instead of flattening it to
a still), guest mode (10.0), business connection (10.2), Stars + paid media, boosts, member
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
make test    # 190 tests: pulls and pity, economy invariants, escrow, paging, ingestion
make lint    # ruff + ruff format + "generated docs are current"
make check   # both, which is what CI runs
```

Two of them are worth naming because they are the reason the rest can move fast: the ledger
reconciliation test asserts every coin in the database has a transaction behind it (the
reference bot's balance column drifted, which is how it got farmed), and
`tests/test_summon_parity.py` is the parity contract — every command name Summon-bot
registers must resolve here or be documented as renamed with a reason.
