# Changelog

## 2026-09-24 — fresh installs ship playable (roster on by default)

The "why is this bot dead" complaint, root cause #1: a fresh database had
**zero characters**, so `/summon`, `/market` and the spawns were dead on
arrival. The 177-character reference catalogue shipped in
`waifu/data/characters.seed.json` but `SEED_CATALOGUE` defaulted off —
while the reference bot its players gacha'd against was never an empty
shell.

- `seed_catalogue` now defaults **on**: first boot (the auto-migration
  from 0125c4b) loads the catalogue alongside the 18-tier ladder, so a
  redeploy plays out of the box. `waifu migrate` and `waifu seed
  --catalogue` follow the same policy through the same code path
  (`ensure_characters`, deduped on name+series — re-runs are lossless).
- `SEED_CATALOGUE=0` keeps the old empty-roster behaviour (build with
  `/upload` or `waifu import-legacy`); README, `.env.example` and the
  doctor hint now describe the opt-out instead of the opt-in.
- Tests pin both contracts: fresh migrations ship 177 characters,
  `SEED_CATALOGUE=0` leaves the roster empty, and a fresh deploy boot
  lands in a *playable* database. Local proof: fresh file → 8 migrations,
  177 characters, 18 ladders; restart → 0 re-applied, roster intact.

## 2026-09-24 — the deploy now builds its own schema (no migrate step needed)

The deploys of 2026-09-24 finally booted (settings, plugins, polling all
up) and then failed *every* query — jobs, `/start`, `/help` — with
`OperationalError: no such table: auctions / groups / kv / cooldowns`:
the platform never runs `waifu migrate`, so the process started into an
empty SQLite file.

- `build_app` now applies the pending schema upgrades at startup
  (idempotent, re-run safe, logged as `schema: applied …`). Every entry
  point that owns the database — `waifu` (the bot), `waifu api`,
  `waifu jobs` — self-heals; a fresh deploy needs **no deploy step**.
- `waifu doctor` keeps its job: it *reports* pending migrations
  (`migrate=False`) instead of silently doing them.
- `tests/test_deploy_bootstrap.py` pins the failure mode: a fresh
  database boots through `build_app` into a current schema, a reboot is
  a no-op, and the doctor stays read-only.
- A keep-alive bind failure now says so plainly (error level) with the
  fix — a free-tier web service sleeps a process that never opens its
  advertised port.

## 2026-09-24 — deploy-day crash sweep: lazy imports, probe payloads, clean shutdown

The deploy now got past settings and plugin loading and died in the startup
sequence. An audit of *every* aiogram import name against the installed
aiogram 3.31 found two invented type names hiding in lazy imports (the kind
no test ever executed) plus two payload bugs in the capability probe:

- `set_command_menu` imported `BotCommandScopeChatAdmins` — a name aiogram
  does not ship — and crashed every deploy at startup. Now uses the real
  scopes (`BotCommandScopeDefault`, `…AllChatAdministrators`,
  `…ChatAdministrators`).
- `/webapp` imported `WebAppButtonInfo` (invented) for the mini-app button —
  the same crash, one command later. Now `WebAppInfo`.
- The capability probe built its payloads with aiogram-2.x shapes
  (`RichMessage` where `InputRichMessage` is required, a string
  `draft_id`): the ValidationError killed the *whole* probe at every real
  deploy. Payloads are now built inside the try (one bad flag degrades,
  nothing fatal) and use the correct 3.x shapes.
- The probe's old/new-server test matched any "not found" — including
  "chat not found", the *expected* answer to the fake probe chat — which
  would have silently disabled rich messages on the official API. It now
  matches "method not found" specifically.
- `run()` now wraps the entire startup sequence (webhook cleanup, identity,
  command menu, menu button, services, runners) in the same
  try/finally as the serve loop, so a startup failure still closes the bot
  session instead of leaking "Unclosed client session" noise at exit.
- New standing test (`tests/test_aiogram_imports.py`): every aiogram import
  in the codebase is resolved against the installed aiogram, so the next
  invented name fails in CI, not on the platform.

## 2026-09-24 — local Bot API server: one explicit switch, aiogram-3 wiring

- The Render deploy crashed at startup: a generic ``API_BASE`` env var
  (set for another tool) flipped the bot into local-Bot-API-server mode,
  and the session was built with aiogram-2.x parameters
  (``api_url=``/``api_base=``) that aiogram 3.31 no longer accepts —
  ``BaseSession.__init__() got an unexpected keyword argument 'api_url'``.
- Local-server mode is now opt-in through a single explicit variable:
  ``BOT_API_URL`` (the server root, e.g. ``http://apiserver:80``). Left
  empty, the bot uses the official api.telegram.org — and nothing else
  in the environment can change that. ``API_URL``/``API_BASE`` are no
  longer read.
- The wiring is aiogram-3 correct (``TelegramAPIServer`` local mode), so
  a real tg-bot-api deployment now works: the standard ``/bot`` +
  ``/file`` layout is derived from the one URL, and a scheme-less value
  is a clean validation error.

## 2026-09-24 — the whole repository layer in one file

- `waifu/db/repositories/` (15 modules) is now `waifu/db/repo.py`. The database
  story is literally one file pair: `database.py` (the engine — `tx()`/`query()`,
  backup/restore) and `repo.py` (every SQL access function).
- Domains are namespace classes, called exactly as the old modules were:
  `from waifu.db.repo import users as user_repo`. Shared constants
  (`SNIPE_WINDOW`, `ITEMS`, `PERMISSIONS`, …) and the row dataclasses
  (`Owned`, `ItemDef`, `PityState`, …) live at module level; the ones services
  address through a domain (`items_repo.ITEMS`, `collection_repo.Owned`) are
  also exposed on their domain, so no call site had to change shape.
- The merge was mechanical, not hand-rewritten: an AST transform moved each
  module into its class, re-pointed intra-module calls
  (`helper(...)` → `Domain.helper(...)`), and a verification tool re-compared
  all 312 functions, 7 dataclasses and 15 constants against the originals —
  structurally identical, then the full 491-test suite.

## 2026-09-24 — cache and Redis merged into `waifu/db/state.py`

- `waifu/db/cache.py` + `waifu/db/redis_client.py` → one file,
  `waifu/db/state.py` (`Cache` + `Redis`): the hot, non-source-of-truth layer.

## 2026-09-23 — one database, automatic backups, fastest startup

- **One database.** `DATABASE_URL` now defaults to a single SQLite file
  (`data/waifu.db`): no server to install, nothing to connect to, one file is all
  there is to back up. Postgres (`postgresql+asyncpg://…`) remains the scale-up
  path for multi-worker deployments — same code, same migrations (portable to
  both), same backups.
- **Redis is optional.** Leave `REDIS_URL` empty and the bot runs on in-process
  state (MemoryStorage FSM, database-backed cooldowns and queues). The database
  stays the only source of truth, so losing the process loses speed, never data.
- **Backups.** `waifu backup`, `waifu restore <file> [--yes]` (preview without
  `--yes`) and the owner command `/backup`. The jobs loop takes a snapshot of the
  whole database daily and keeps `BACKUP_KEEP` (default 10) of them, reporting
  each one to the owner channel. Restore is all-or-nothing: a bad file leaves
  the database exactly as it was.
- `waifu doctor` now prints the newest backup and its age.
- `deploy.env.example` — the easy deploy env: copy to `.env`, set `BOT_TOKEN`,
  done. The full commented reference stays in `.env.example`.
- `waifu/db/engine.py` became `waifu/db/database.py` — the one database module:
  `tx()` (read/write) and `query()` (read-only) are the two access methods
  everything else builds on, plus `backup()`/`restore()`.

## 2026-09-23 — deploy fixes (Render)

- `NoDecode` on every list-typed settings field: `ADMIN_IDS=1,2,3` (the form
  `.env.example` documents) and JSON arrays both parse; a garbage value is a
  clean validation error, not an opaque env-decode crash.
- FSM storage: `DefaultKeyBuilder(prefix="waifu:fsm")` — aiogram 3.31 has no
  `global_prefix` keyword, which crashed every real deploy at startup.
- When the environment fails to parse, the CLI prints which variable failed,
  what value it had (secrets masked) and the format to use.

## 2026-09-23 — owner feed, rich messages, weekly digest

- `/setlogchannel` (owner, admin-verified, stored in the database so it wins
  over the env across restarts); per-group log channels for joins/warnings/
  raffle draws; `/digest` on demand + the Sunday job pass; rich messages
  (`SendRichMessage`) for every owner-facing send, the gift-receipt DM and the
  in-group raffle results card — plain-text fallback everywhere.

## 2026-09-23 — log self-reporting, reactions

- `/logtest` proves the channel is alive; `/doctor` counts every log-channel
  send since startup (a dead channel shows up, instead of failing silently);
  reactions on a newcomer's first message, a delivered gift and a daily claim.

## 2026-09 — owner log channel + Bot API 10.3 surfaces

- `LOG_CHANNEL_ID` event feed: start/stop/crash, gifts (character + rarity),
  every payment (Stars, paid media, subscriptions), joins, moderation; the
  gift recipient gets a private DM with the character, rarity and art.
- Videl-pattern keep-alive health server (`/`, `/health`, `/healthz`).
- New Bot API surfaces with graceful degradation: rich messages, drafts,
  ephemeral messages, checklists, live photos, member tags, disabled buttons,
  button styles, media polls, guest mode, business connections, topics,
  paid media/Stars.
