# JSON API (the mini app's back end)

The reference deployment ran a Flask service next to its bot — `api.py`, deleted from the source
tree and preserved only in `__pycache__/api.cpython-313.pyc`. Its sixteen functions were a
health probe, seven read endpoints for the web front-end, two *mutating* endpoints, a SQLite
helper, and Telegram's `initData` signature check. This bot ports the whole set to aiohttp,
in the bot's own process, on the same tables.

```bash
python -m waifu api --port 8080     # standalone
API_ENABLED=1 python -m waifu bot   # alongside polling
```

`waifu doctor` prints whether it is enabled; nothing else about the bot changes if you never
turn it on.

## Routes

| method | path | what it returns |
| --- | --- | --- |
| GET | `/api/health` | `{ok, db, users, characters, tiers, utc}` — the only public route |
| GET | `/api/user/<telegram_id>` | balance, level, `last_daily`, the streak row, unique/total counts, collection value |
| GET | `/api/inventory/<telegram_id>` | `?q=&limit=&page=` — the harem, newest first, `count` per character |
| GET | `/api/characters` | `?q=&rarity=&limit=&page=` — the roster, `ref` = the padded id admins quote |
| GET | `/api/market` | what the shop is offering right now, priced by the same `price_for()` the chat uses |
| GET | `/api/leaderboard` | `?limit=25` — by balance, like the reference's default |
| GET | `/api/streak/<telegram_id>` | `streak_count`, `highest_streak`, `last_streak_date`, `freezes` |
| GET | `/api/achievements/<telegram_id>` | unlocked rows, most recent first |
| POST | `/api/daily/<telegram_id>` | the daily claim: reward, new balance, streak, `next_reset`, xp, bonus item |
| POST | `/api/summon/<telegram_id>` | `?ten=1` for a ten pull; the rolls **plus `commitment` and `sequence`**, so a browser can show `/verify` material |

Field names follow the reference where the concept survived (`msg_id` still carries the
character's Telegram file id, `rarity` still the decorated display string), and status codes
were added: `401` unauthenticated, `403` another account, `404` no such player, `402` not
enough coins, `409` already claimed, `503` empty roster. The short error *keys*
(`insufficient_balance`, `user_not_found`, `no_characters`, `already_claimed`) are the
reference's, so an existing front-end keeps matching on them.

## Authentication

Every non-public route needs **signed** `initData`, in the `X-Init-Data` header (what the
reference used) or a `?tgData=` value:

```
data_check_string = "\n".join(f"{k}={unquote(v)}" for k, v in sorted(params.items()))
secret_key        = HMAC_SHA256(key="WebAppData", msg=BOT_TOKEN)
valid             = HMAC_SHA256(key=secret_key, msg=data_check_string) == params["hash"]
```

— i.e. [Telegram's documented check](https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app),
implemented in `waifu/api/auth.py` with three additions:

* `hmac.compare_digest`, so the comparison is not a timing oracle;
* `auth_date` freshness (`API_INIT_DATA_MAX_AGE`, default one day) — without it, a link pasted
  in a chat is a permanent key to someone's account;
* the id in the path **must equal** the id in the signature. `GET /api/user/7` with your own
  valid `initData` is a `403`, not somebody else's balance.

The reference additionally accepted `?uid=42` from anybody. Here that is
`API_ALLOW_UID_QUERY=1`, refused unless the operator sets it, and `waifu api --insecure-uid-query`
prints a warning when it is on. A front-end that is not a Mini App (an operator dashboard, a
monitoring probe) authenticates with `X-API-Token: <WEBAPP_SECRET_KEY>`; leaving that empty
disables the header entirely.

CORS is pinned to `WEBAPP_URL`: a preflight from any other origin is refused, and every response
carries `Cache-Control: no-store` — a cached balance in a shared proxy is somebody else's
balance.

## Why it is not a separate service

A second deployable would need its own migrations, and then the web view and the bot disagree
about a price — which is precisely how the reference's `config.py` dict of prices and its
`rarity_chance` table drifted apart. So `waifu/api` imports the same repositories the handlers
do, `build_app(ctx)` receives the bot's `AppContext`, and the money-moving routes call
`ctx.economy.daily()` / `ctx.gacha.pull()`: the API cannot cheat at pulling, because it *is*
pulling.
