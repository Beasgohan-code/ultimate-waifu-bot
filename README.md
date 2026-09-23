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

**Fastest start — one file, no services:**

```bash
cp deploy.env.example .env    # set BOT_TOKEN — the only required variable
make install
python -m waifu migrate       # schema + tier ladders (writes the one database file)
make run
```

That is the whole setup: the default database is **one file** (`data/waifu.db`, SQLite),
Redis is optional, and `BOT_TOKEN` is the only variable without a safe default. Before you
deploy, point `DATABASE_URL` at a *persistent* volume — on Render the project dir is wiped
on every deploy, so use `sqlite+aiosqlite:////opt/render/project/data/waifu.db` (a backup
taken into the ephemeral dir dies with the box).

**Scale mode — several workers (Docker, Postgres + Redis):**

```bash
cp .env.example .env          # the full reference: every variable, commented
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

The same code runs on the one file or on Postgres (migrations are portable to both; the
tests exercise the SQLite path). The timer loop (spawn feed, auction settlement, expiries,
raffle draws, the daily backup) runs in-process; to drive it from cron instead, set
`NO_JOBS=1` and run `python -m waifu jobs --name autospawn`.

## The database: one file, two methods, automatic backup

Everything the bot remembers — players, coins, collections, characters, auctions, streaks,
log state — lives in exactly **one database**. By default that is a single file;
`postgresql+asyncpg://…` is the scale-up path for multi-worker deployments, with the same
migrations, the same code and the same backups.

Two methods cover all access (every service and command in the codebase builds on them):

- `Database.tx()` — **read/write**: one transaction, commit on success, roll back on error;
- `Database.query(stmt)` — **read-only**: one statement, no commit.

And the guarantee that keeps "data loss" out of the vocabulary:

- `waifu backup` / `waifu restore <file> [--yes]` — the entire database in one JSON file
  (every table, in foreign-key order); restore is all-or-nothing, so a bad file leaves
  the database exactly as it was;
- the jobs loop takes the same snapshot **daily**, keeps `BACKUP_KEEP` (default 10) and
  reports each one to the owner channel;
- `/backup` (owner) — the snapshot from Telegram, answering "where is my data?" with a
  filename you can restore later;
- `waifu doctor` prints the newest backup and its age, so "did it actually back up?" has
  a visible answer.

List-valued env vars (`ADMIN_IDS`, `ALLOWED_MEDIA_HOSTS`) accept the comma-separated form
shown in `.env.example` *and* a JSON array — `ADMIN_IDS=1,2,3`. Both parse on every
platform (Render, Railway, a bare box) because the fields are declared `NoDecode`; a
test pins the env-var path so the deploy never dies on settings parsing again.

## Layout

```
waifu/
  core/        app assembly, dispatcher + plugin loading, middlewares, access/permissions,
               context (the service registry), jobs (the timer loop), CLI (waifu/__main__.py)
  db/          one database: database.py (the engine + tx/query + backup/restore),
               models, versioned migrations, seed (tier ladders + catalogue),
               repositories/ (all SQL lives here), cache, optional redis client
  services/    economy, gacha, collection, items, spawn, auction, trading, codes, gifts,
               progress, stats, moderation, premium, ai, cards, hstats — no Telegram types
  tg/          one module per new Bot API surface: rich messages, drafts, ephemerals,
               checklists, guest mode, business connection, paid media/Stars, stories,
               live photos, reactions, capability negotiation
  plugins/     24 routers: one file per feature area, handlers only
               (uploads.py is the exception the design allows: the admin roster pipeline
               writes through the characters repository because there is no service for
               "ingest this media" — and it is where an empty database stops being one)
  api/         the mini-app JSON API on aiohttp (waifu/api): signed-initData auth, read
               endpoints for harem/market/leaderboard, POST for anything that spends money
  data/        the catalogue build, the custom-emoji map, card fonts
  enums.py errors.py settings.py logging.py utils/
tests/         fixtures wire the real services to in-memory SQLite; no mocked own-layers
docs/          generated command reference, the parity contract, and SUMMON_EXTRACT.md
               (every module and function in the reference repo — source *and* its bytecode —
               mapped to where it went here)
scripts/       build_catalogue.py (roster), import_summon.py (migration),
               gen_reference_docs.py, extract_summon_reference.py (the audit above)
deploy/        supervisor (crash restart + optional recycling) and the systemd unit
```

The dependency rule, enforced by how things are imported: `plugins → services → db`, and
`plugins → tg` for rendering. Services never touch aiogram types; handlers never write SQL
(except the admin roster CRUD, which is documented where it happens); nothing commits
outside the session middleware.

## New Bot API features, and how they degrade

Rich messages (10.1), message drafts (9.5/10.3), ephemeral messages (10.2/10.3 — also how
`/autoadd` acknowledges an ingest without turning the group into a console), checklists,
live photos (9.1 — `/autoadd` and `/addchar` file one with its motion, while `/upload`
flattens it to a still exactly as the reference bot did), guest mode (10.0), business connection (10.2), Stars + paid media, boosts, member
tags, `date_time` entities, custom emoji in bot messages (9.4), reaction and poll updates,
prepared inline messages for sharing a harem, and inline mode for the roster and your own
collection (`@bot <query>`, `@bot collection.<you>`). `python -m waifu` negotiates them once at
startup (`waifu/tg/caps.py`) and each renderer picks the plain path when the server does
not have them — so an instance on a self-hosted API server of last year loses formatting,
not functionality. Reactions are the quietest of them: the bot puts a heart on a
newcomer's first message, on a delivered gift, and a fire on a daily streak claim —
readable in a 4k scrollback without adding a fifth bot message, and gone (gracefully)
on servers without the feature. Details: [docs/NEW_BOT_API.md](docs/NEW_BOT_API.md).

## Owner log channel

Set `LOG_CHANNEL_ID` (a channel id, `-100…` format — forward any channel post to
[@userinfobot](https://t.me/userinfobot) to read it) and add the bot as an **admin** of that
channel. Everything the owner should know about lands there, in-chat, one line per event:

- **lifecycle** — 🟢 started (version, Bot API level, capability summary), 🔴 stopped,
  💥 crashed (with the exception), before each restart the supervisor asks for;
- **players** — 🆕 first seen (name, handle, premium/owner badge), ➕ joined a group;
- **gifts** — 🎁 character gift (giver → receiver, character, rarity) — anonymous gifts
  included, and 🪙 coin gifts; the *receiver* also gets a private DM with the character's
  name, rarity and art (or a text card when there is none to send);
- **money** — 💰 Stars settlements (amount, coins credited), ↩️ refunds, 💳 paid-media
  purchases, 🎉/🔁/🔁 subscription starts, renewals and cancellations;
- **the rest** — 🎟️ raffle results, 🔁 trades, auction settlements, boosts, maintenance
  mode, and ledger-integrity alarms.

The bot never blocks on the channel: every send is fire-and-forget with a bounded
in-process queue, a `try/except` around the whole thing, and a silent drop-and-count once
the channel is unreachable — a misconfigured `LOG_CHANNEL_ID` costs you a channel post,
never a player action.

The channel also verifies itself: `/logtest` sends a test line and reports the outcome
(the "bot is not an admin" case is the common one), and the `/doctor` page counts every
send since startup — a dead channel shows up there instead of failing silently for weeks.

Three owner commands keep the feed in hand without a deploy:

- `/setlogchannel -100…` (owner) repoints the feed — the bot must already be an admin
  of the target channel — and stores the choice in the database, where it wins over
  `LOG_CHANNEL_ID` across restarts;
- `/digest` sends the weekly digest on demand; the Sunday job pass sends it on its own.
  Seven numbers (new players, pulls, gifts, raffles drawn, ⭐ Stars in, orders,
  premium now) as a rich table;
- every line is a **rich message** (Bot API 9.5+ `SendRichMessage`) on servers that
  have the feature — heading + body + a code block for the machine detail — with the
  identical plain line as the fallback on older servers. The same rich layout is used
  for the gift-receipt DM and the in-group raffle results card.

**Per-group log channels.** Every group can point its *own* channel at the feed:
set `log_channel_id` in that group's settings (`/groupsettings`). Group-scoped
events — member joins, warnings/ladder actions, raffle draws — are posted there **in
addition to** the global owner channel, so a group's admins watch their own feed while
the owner keeps the master copy. Unconfigured group → the line simply goes to the
owner channel as before.

## Scheduled broadcasts, character requests, streak warnings

- **`/broadcast at 20:00 <text>`** schedules an announcement (also `20:00 tomorrow`,
  `+2h`, `in 30m`, `2026-12-31 20:00`); the jobs loop fires it at the moment, fans it
  out to every registered group, and logs the result to the owner channel.
  `/broadcast list` shows the queue, `/broadcast cancel` empties it. Rows are claimed
  with an atomic `UPDATE … WHERE sent_at IS NULL`, so the resident loop and a cron
  `waifu jobs` can never double-post.
- **`/request <Name> <Series>`** — the polite door to the admin-curated roster.
  Requests that match an existing character (fuzzy) are answered with a `/check`
  pointer, duplicates collapse to the first ask, and staff see the deduped queue in
  `/requests` with one-tap ✅/❌. Approving flags it for the owner (who still uploads
  the art) and the asker is told the outcome by DM.
- **Streak-break warning** — once a day the jobs loop DMs every player whose streak of
  ≥3 days would break at the nightly reset ("your 5-day streak breaks tonight —
  /daily"), including what freezes they have. A per-cycle marker makes it a single DM,
  not an hourly one, and failed sends retry on the next pass.

## Keep-alive health server

Free-tier platforms (Render's free tier included) sleep a *web* service that exposes no
public endpoint. `HEALTH_ENABLED` (default `1`) makes polling mode run a tiny aiohttp
server — the same pattern Videl ships (`videl/core/server.py`) — alongside the bot:

- `GET /` and `GET /health` — `{status, bot, version, api_version, uptime_seconds}`,
  always 200 while the process is up, no database access (a wedged DB must not 503 the
  keep-alive, or the platform sleeps the bot and the DB problem becomes "bot is offline");
- `GET /healthz` — the deep check (database + redis, 503 when the data layer is down);
- port order `HEALTH_PORT` → the platform's `$PORT` → 8080; `HEALTH_HOST` to override the
  bind address. Webhook mode already serves `/healthz` through the main app, so the second
  server only runs in polling mode. A taken port degrades to a warning, never to a crash.

It is aiohttp rather than Werkzeug because the process already owns the asyncio loop —
Videl's own implementation is aiohttp too, and a WSGI worker would cost a thread and an
event-loop hop per poke.

## Advanced: what the porting turned into

Two things here came from reading the reference deployment rather than its command list —
`docs/SUMMON_EXTRACT.md` records which is which.

**`/pcard` — the drawn profile card.** The reference's `plugins/profile.py` produced the one
image people actually forwarded: a 1000×540 gradient canvas, the favourite's portrait inside a
rarity-coloured ring, a pill badge with a gold star for the tiers that earned one, a glow rect for
premium players. Same visual grammar here, four things fixed: fonts are fetched once on demand
instead of with blocking `requests` at import time, drawing runs in a worker thread instead of on
the event loop, the PNG is cached by a signature derived from *every* field the card prints, and
the portrait only ever comes from a stored `file_id`, an allow-listed host, or the player's own
Telegram photo. `show_balance` masks the number *before* rendering, which is what stops a cached
card from leaking it to the wrong viewer. The card carries its own controls (glow, portrait source,
copy-handle, inline collection switch), and an install without Pillow degrades to the text profile
instead of failing.

**`/gate` — the join-request quiz.** `chat_join_request` was in this bot's subscribed updates from
the first commit and answered by nobody; the reference predates `creates_join_request` entirely.
A group admin runs `/gate on`, mints an invite with `/gatelink` (the flag that generates the update
is set by the bot, since a link without it silently bypasses the gate), and each applicant gets one
question in a DM: pick the right character out of four drawn from *this deployment's* roster. Three
tries, then declined with a reason. The question bank is cached per group for an hour, the correct
index is randomised per pool, and every failure to ask — empty roster, blocked DM, a crash in our
own code — ends in a decision rather than a request hanging forever. State lives in the cache, so
the feature needed no migration.

Both are documented with their reasoning in
[docs/NEW_BOT_API.md](docs/NEW_BOT_API.md), which also lists four bugs this pass exposed in
`/profile`'s own send path (`ChatMode.HTML`, aiogram's removed `Chat.is_private`, a cache key built
from an un-awaited coroutine, a dict passed to `money()`), each now covered by a test.

## Mini-app JSON API

`python -m waifu api` serves the front-end contract the reference deployment's `api.py` had —
`/api/user`, `/api/inventory`, `/api/characters`, `/api/market`, `/api/leaderboard`,
`/api/streak`, `/api/achievements`, `/api/health`, and `POST /api/daily` / `POST /api/summon` —
with Telegram's signed `initData` required on every one of them and the id in the path forced to
match the signature. Details and the auth algorithm: [docs/API.md](docs/API.md).

## Commands

[docs/COMMANDS.md](docs/COMMANDS.md) is generated from the routers, so it cannot drift;
`/help` is curated by topic with a generated `more` page, `/commands` sends the complete
list as a file, and Telegram's ⊞ menu is published at startup from the same registry
(the player scope and the group-admin scope share 100 entries each, deliberately).

## Tests

```bash
make test    # 491 tests: pulls and pity, economy invariants, escrow, paging, ingestion,
             # inline mode, the API's initData signature check, the reference-port map,
             # owner-log coverage (gifts, Stars, subscriptions, the health server, a
             # full /start through the real middleware stack, /logtest, /setlogchannel,
             # the weekly digest, reactions, rich-message fallback, per-group log
             # channels, scheduled broadcasts, character requests, streak warnings),
             # backup/restore (full-DB round trip, all-or-nothing failure, retention,
             # the daily pass, the CLI, /backup), and env-var list parsing — the format
             # a real deploy hands over
make lint    # ruff + ruff format + "generated docs are current"
make check   # both, which is what CI runs
```

Two of them are worth naming because they are the reason the rest can move fast: the ledger
reconciliation test asserts every coin in the database has a transaction behind it (the
reference bot's balance column drifted, which is how it got farmed), and
`tests/test_summon_parity.py` is the parity contract — every command name Summon-bot
registers must resolve here or be documented as renamed with a reason.
