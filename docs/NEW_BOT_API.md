# New Bot API features, and how each one degrades

Every 2025–2026 addition this bot uses, where it lives, and what happens on a server that
does not have it. The rule the whole tree follows: **a missing capability costs formatting,
never function** — so one codebase runs against api.telegram.org and against a self-hosted
server that is a year behind.

The gate is negotiated once at startup. `waifu/core/bot.py::probe_api_features` asks the
endpoint which methods it answers, `waifu/tg/caps.py` turns that into flags, and handlers
ask `ctx.caps.allow("rich")` / `ctx.wants("checklist")` (the second also respects the
player's own preference). A typo in a capability name therefore falls back to the old
behaviour instead of raising inside a command — which is the difference between a bot that
degrades and a bot that 500s in every group.

| feature | module | used by | fallback |
| --- | --- | --- | --- |
| Rich messages (`SendRichMessage`, blocks + `RichToolbarButton`) | `waifu/tg/rich.py` | spawn cards, pull results, receipts, market/auction listings | `RichMessageBuilder.fallback_html()` — the same content as an HTML caption with an image |
| Message drafts (`SendMessageDraft`, `can_stop`, `keep_on_stop`) | `waifu/tg/draft.py` | `/ai` answers, long `/market` listings, `/history` exports | one `send_message` with the finished text |
| Ephemeral messages (`EphemeralMessageParameters`) | `waifu/tg/ephemeral.py` | per-player answers in busy groups (spoilers, hint reveals, "you lost" receipts), the `/autoadd` ingest receipt | a reply in-thread, deleted after the claim window |
| Checklists | `waifu/tg/checklist.py` | `/quests` (ticking a quest is the claim gesture; `ticked_task` entities are read back) | a table with ▰▰▱ progress bars |
| Live photos, `sendPaidMedia`, albums | `waifu/tg/media.py`, `waifu/tg/paid.py` | character art (`/char`, pulls); `/upload` **files** a live photo with its motion instead of flattening it to a still | static photo, then the URL as caption |
| Reactions (`setMessageReaction`, `message_reaction`) | `waifu/tg/interactions.py`, `waifu/plugins/misc.py` | high-tier pulls get 🔥🎉 instead of text spam; raffle entry counting | votes-only raffles |
| Polls / quiz mode | `waifu/plugins/nguess.py` | `/ngpoll` — the guessing round in the native UI | `/nguess` typed-answer round |
| Guest mode (`AnswerGuestQuery`) | `waifu/tg/guest.py`, `waifu/plugins/misc.py` | a chat the bot is not a member of can still `/pull` a preview | "add me to use this" (the invite CTA is real, not a deflection) |
| Business connection | `waifu/tg/business.py` | a Premium user's private chat through their own bot: their balance, /daily and collection commands work there | — (no business account, no behaviour change) |
| Stars: invoices, paid media, subscriptions, refunds, gifts | `waifu/tg/paid.py`, `waifu/plugins/premium.py` | `/premium`, coin packs, `/topup`, raffles and boosts | the free paths stay free; the buttons explain the payment is unavailable |
| Member tags, emoji status, verification | `waifu/tg/interactions.py` | moderators tag the player they warned; `/glow` uses emoji status | tags as text in the case row |
| Prepared inline messages + `switchInlineQueryChosenChat` | `waifu/tg/buttons.py`, `waifu/plugins/webapp.py` | `/share` — send your harem card to any chat without a bot round-trip | a `t.me/` link that re-renders the same page |
| Per-scope command menu (`setMyCommands`, `BotCommandScopeChatAdmins`) | `waifu/core/bot.py`, `waifu/core/dp.py::command_menu` | the ⊞ menu, generated from the routers at startup | the menu is simply what BotFather set |
| Mini App entry point (`setChatMenuButton`, `WebAppInfo`) | `waifu/tg/stories.py`, `waifu/plugins/webapp.py` | `/webapp` for searchable roster browsing | `/chars` + `/collection` paging |
| `date_time` entities, custom emoji in bot messages | `waifu/tg/text.py` | timers, claim windows, cooldowns render in the reader's own timezone | absolute UTC + a "your time" line |

Two things that are **not** here, deliberately: reactions as a payment signal (Telegram
does not attribute them precisely enough to move coins) and voice transcription (an
inbound-audio feature that needs a second model behind it — see the roadmap in
`docs/ROADMAP.md` if you want to add it).

## Why not python-telegram-bot

PTB 22.8 has no typed support for the rich-message, draft or ephemeral surface, so every
one of those would have been a raw `call_api` with a hand-rolled schema — which is exactly
the shape of bug that made the reference bot's new features quietly stop working when the
API renamed a field. aiogram ships them as models, so a missing field is a type error in
review instead of a runtime surprise in a group of four thousand people.
