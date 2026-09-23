# Roadmap / suggested features

Ordered by (players who would notice) ÷ (work to build). Everything here assumes the
existing layers: a suggestion that needs a new service, a new table and a new page is two
weeks; a suggestion that only recombines what is already wired is an afternoon. The
"Ammunition" column names the code that already exists, because that is what makes these
cheap rather than speculative.

## Worth doing next

1. **Character voice lines as real audio.** `/char` already has a `voice_line` column and
   the media pipeline re-hosts `file_id`s, so adding `input_audio` (Bot API 9.6's
   `SendAudio` with waveform + title/performer) turns each card into something players
   spam each other with. Ammunition: `tg/media.py`, `cards.CardService`.
2. **Fusion / craft: N dupes → 1 higher tier.** The economy currently *sells* dupes at 40%,
   which is a leak the reference bot never plugged — a whale with 200 Commons would rather
   keep pulling. A 5×Common → 1×Rare bench gives those copies a purpose and makes the dupe
   payout a choice instead of a default. Ammunition: `collection.consume`, `items` shop, the
   ledger (every cost is already a `Transaction`).
3. **Guilds/teams with a shared vault and a weekly guild quest.** `/top` is individual, so a
   group of friends has no reason to keep the bot in their chat once one player is done for
   the day. A team row + a shared counter turns daily engagement into a social obligation,
   which is what retains. Ammunition: `progress.quests`, `stats.leaderboard`, `moderation.group`.
4. **Betting pool on the spawn feed.** `/bet 500 on whoever spawns next` — the spawn row is
   already authoritative and settled atomically, so a pari-mutuel pool is one table and a
   settle hook in the same place claims happen. Ammunition: `spawn.claim`, `economy.ledger`.
5. **Character "requests" board with admin approval and coin sponsorship.** Players pay into
   a bounty for a character they want added; the admin approves and the winner is credited.
   This is how a community-run instance grows its roster without the owner typing JSON.
   Ammunition: `characters.create_or_update`, `codes` (the same payout/idempotency shape).
6. **Duel / challenge: 1v1 stat showdown with a stake.** `/duel @player 5000` uses
   `stat_power` (already on every character) and the escrow machinery trades already prove
   works: propose → both accept → atomic settle. Ammunition: `trading` escrow rows, `items.use`.
7. **Trade-up contract à la CS:GO** (10 of tier X → 1 of tier X+1 with a weighted roll):
   the seed-reveal verification in `gacha.verify` already makes an outcome provable, which is
   the one thing that stops "the bot robbed me" threads.
8. **Season pass with free and paid tracks.** `progress` has streaks, quests and achievements
   — a track is a lookup of thresholds plus a claim flag. The paid track is a Stars invoice
   (`premium.invoice` exists) and `deliver_owned` already makes delivery idempotent.
9. **Webhook-free "open trading lobby" Mini App.** `/webapp` and `web:harem` return the JSON
   the page needs; a real trade picker (tap two harem cards) removes the worst part of
   text trades — typing 12-character codes.
10. **Auto-moderation with a per-group threshold, from the spam counter that already exists.**
    The middleware counts messages per chat already (the spawn feed needs it); warnings at
    N/minute and a mute at M would let a group owner set their own noise rules without editing
    code — the reference bot's global `SPAM_LIMIT = 20` was its second-most-requested change.

## Larger, but the reason to keep going

11. **Multi-bot federation / sharding by chat id.** One process per shard with the same
    Postgres, Redis queues for the spawn feed. `core/dp.py::build_storage` and the
    job passes are already stateless per chat, so this is deployment shape, not new logic.
12. **Local image rendering for cards** (Pillow/Playwright) with the Telegram-hosted photo
    as fallback — the current design deliberately uses `file_id`s because they cost nothing,
    but a rendered name+stats overlay is what makes `/pull` screenshots spread on their own.
13. **A second, non-gacha economy: farming/gacha garden.** A plot that grows a claim over
    hours is the same cooldown machinery as `items.skip`, and it gives players a reason to
    come back twice a day without pulling.
14. **Community events: cross-group raid boss.** A shared HP pool settled by `spawn.claim`
    across every group at once, with per-group credit. Needs one table and one loop; the
    notification path (`ctx.notify`) and the settle-card pattern are already built for it.
15. **Import from other bots.** `scripts/import_summon.py` proved the pattern (map → normalise
    → idempotent upsert → report the rows you could not map). An exporter for the same shape
    is the single best defence against "I will lose my collection if I switch".

## Deliberately not planned

* Voice transcription and AI image generation: both need a model behind them and a cost
  story; `/ai` is already budget-gated per player and that is the pattern to follow if one of
  these lands.
* Any "pay to win" coin shop beyond the cosmetic `premium` perks — the moment coins buy
  power the free players leave, and the group chat is the product.
* Reactions-as-payment (Telegram does not attribute them precisely enough to move coins).
