"""``/start``, ``/help``, ``/ping``, ``/settings``, ``/font``, ``/about``, ``/update``
and the update-type routers that have no command of their own (group joins, reactions,
raffaft polls, business messages, guest-mode answers).

Two deliberate differences from the reference bot:

* ``/start`` in a group answers **ephemerally** (Bot API 10.3). The old bot posted a
  40-line tutorial into the chat for every joiner, which is the most common reason a
  group muted it.
* ``/help`` is generated from :data:`HELP_TOPICS`, and a test asserts every command it
  lists is actually registered. Summon-bot kept a hand-written help string *and* a
  ``setBotCommands`` list; they disagreed for months (``/hclaim`` was advertised but
  filtered out of groups, so players reported "the bot ignores me").
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    Message,
    PollAnswer,
)

from waifu import __api_version__, __version__
from waifu.enums import ChatMode
from waifu.plugins._kit import (
    Args,
    RichMessageBuilder,
    callback,
    card,
    cb,
    edit,
    mention,
    mode_of,
    money,
    note,
    tabs,
    text,
)
from waifu.utils.font import FONTS, style

if TYPE_CHECKING:  # pragma: no cover
    from waifu.core.access import Access
    from waifu.core.context import AppContext
    from waifu.db.models import User

router = Router(name="misc")
member_bonus_router = Router(name="member_bonus")
reaction_router = Router(name="reactions")
poll_router = Router(name="polls")
business_router = Router(name="business")
chat_member_router = Router(name="chat_member")
guest_router = Router(name="guest")

#: ``/help <topic>`` sections: ``(command, one-line description)``.
HELP_TOPICS: dict[str, tuple[tuple[str, str], ...]] = {
    "gacha": (
        ("/pull [n]", "summon one or many characters"),
        ("/hclaim", "free daily claim, event tiers included"),
        ("/guarantee", "pity counters and what they promise"),
        ("/chances", "the live drop table"),
        ("/rolls", "your recent pulls, with verifiable roll seeds"),
        ("/verify <n>", "replay one of your own rolls against the seed"),
    ),
    "collection": (
        ("/collection [page]", "your characters, grouped by rarity"),
        ("/harem", "the same list, sorted by power"),
        ("/check <player>", "look at someone else's collection"),
        ("/fav <character>", "pin a character to your profile"),
        ("/lock <character>", "protect a copy from /sell"),
        ("/sell <id> [×n]", "turn a spare into coins"),
        ("/search <text>", "find a character in the roster"),
    ),
    "economy": (
        ("/balance", "coins, value and rank"),
        ("/daily", "the daily reward, multiplied by your streak"),
        ("/work", "a quick coin job"),
        ("/spin [amount]", "gamble coins for a multiplier"),
        ("/rob <player>", "steal a share of someone's coins"),
        ("/bomb <player>", "burn their XP unless they shielded"),
        ("/give <player> <amount>", "send coins"),
    ),
    "market": (
        ("/market", "the item shop and today's featured characters"),
        ("/shop", "your inventory and what each item does"),
        ("/buy <id>", "purchase a featured character"),
        ("/auction <id> <price>", "list a character for coins"),
        ("/bid <auction> <amount>", "bid — funds are escrowed"),
        ("/trade <player>", "escrowed character-for-character swap"),
        ("/gift <player> <character>", "send a character"),
        ("/code <redeem code>", "redeem a code"),
    ),
    "social": (
        ("/nguess", "start a guess-the-character round here"),
        ("/summon <name>", "admin: spawn a specific character"),
        ("/autospan on|off", "admin: message-count spawns"),
        ("/top [coins|pulls|value]", "leaderboards"),
        ("/streak", "your daily streak and its multiplier"),
        ("/achievements", "badges and progress"),
        ("/profile [player]", "someone's card, as others see it"),
    ),
    "settings": (
        ("/settings", "collection mode, glow, privacy"),
        ("/font [style]", "decorative text for your display name"),
        ("/mute", "stop the spawn feed in this group"),
        ("/language [code]", "interface language"),
        ("/premium", "what premium does and what it costs"),
    ),
    # The staff page exists because the roster story is unusual here: a fresh install has no
    # characters, and the *only* way in is Telegram. A generated "more" page is where the
    # leftovers used to land, which made "/upload" impossible to find for exactly the
    # person who had to run it first.
    "admin": (
        ("/upload <name> <series> <1-18>", "reply to a photo/video/GIF — adds the character"),
        ("/autoadd <on|off>", "ingest this group's captioned media without a command"),
        ("/uploads", "uploads still waiting for a web host"),
        ("/archiveart <id|missing>", "re-host URL art into a permanent file_id"),
        ("/rosterstats", "per-tier counts and what ingestion has open"),
        ("/chars [page|tier|text]", "browse the roster"),
        ("/addchar <name> | <series> | <tier>", "add by hand, no media"),
        ("/delchar <id>", "delete a character"),
        ("/media <id>", "attach art to an existing character"),
        ("/setchance <tier> <pct>", "edit the pull ladder"),
        ("/chancelist", "both ladders, with prices"),
        ("/reseed", "load the optional reference catalogue"),
    ),
}
#: "more" is generated at render time from the wired routers (see :func:`send_help`).
HELP_ORDER = (*tuple(HELP_TOPICS), "more")


def _start_buttons(settings: Any) -> list[list[InlineKeyboardButton]]:
    rows = [
        [
            callback("🎴 Collection", cb("col", "open")),
            callback("🎰 Pull ×1", cb("gacha", "pull", 1)),
            callback("🎟️ Pull ×10", cb("gacha", "pull", 10)),
        ],
        [
            callback("🏪 Market", cb("mkt", "open")),
            callback("🎁 Daily", cb("eco", "daily")),
            callback("📖 Help", cb("help", "index")),
        ],
    ]
    if settings.support_chat_id:
        rows.append([callback("💬 Support group", cb("misc", "support"))])
    if settings.bot_username:
        rows.insert(
            0,
            [
                callback(
                    "➕ Add me to a group", f"https://t.me/{settings.bot_username}?startgroup=true"
                )
            ],
        )
    return rows


@router.message(CommandStart())
async def start(
    message: Message,
    ctx: AppContext,
    command: CommandObject | None = None,
    access: Access | None = None,
) -> None:
    """/start — with an optional ``ref-<id>`` deep link argument (``/invite``)."""
    settings = ctx.settings
    roster: dict[str, int] = {}
    try:
        async with ctx.db.session() as session:
            from waifu.db.repositories import characters as char_repo

            roster = await char_repo.totals(session)
    except Exception as exc:  # pragma: no cover - the DB is up or we would not be here
        await text(
            message,
            ctx,
            f"⚠️ the catalogue is unreadable ({type(exc).__name__}) — try again in a moment.",
        )
        return
    if access is not None and command and (command.args or "").startswith("ref-"):
        await _apply_referral(message, ctx, access, command.args)
    builder = (
        RichMessageBuilder()
        .heading(
            f"{settings.bot_title} — {money(roster.get('characters', 0))} characters, 18 rarity tiers",
            size=1,
        )
        .paragraph(
            html=(
                "Summon, collect, trade and defend a harem.\n"
                f"• <b>/pull</b> — {money(settings.pull_cost)}🪙 each, {money(settings.ten_pull_cost)}🪙 for ten with a guaranteed high-rarity\n"
                f"• <b>/hclaim</b> — one free claim a day, event tiers included\n"
                f"• <b>/daily</b> — {money(settings.daily_reward_min)}🪙, multiplied by your streak\n"
                "• <b>/collection</b> — your pages, grouped by rarity like the original"
            )
        )
        .divider()
        .footer(
            f"up {ctx.uptime_text} · Bot API {__api_version__} · cards {'rich' if mode_of(ctx) is ChatMode.RICH else 'html'}"
        )
    )
    await card(
        message,
        ctx,
        builder=builder,
        html="Send /help for every command.",
        buttons=_start_buttons(settings),
    )
    if access is not None and message.chat.id != access.user_id:
        from waifu.tg.ephemeral import ephemeral_note

        await ephemeral_note(
            ctx.bot,
            message.chat.id,
            receiver_user_id=access.user_id,
            text="Tap /start in my DMs for the menu — I keep group chats quiet on purpose.",
        )


async def _apply_referral(message: Message, ctx: AppContext, access: Access, argument: str) -> None:
    """Credit an inviter once, on first /start only (``/invite``)."""
    from waifu.db.repositories import economy as ledger
    from waifu.db.repositories import users as user_repo

    try:
        inviter_id = int(argument.split("-", 1)[1])
    except ValueError:
        return
    if inviter_id <= 0 or inviter_id == access.user_id:
        return
    settings = ctx.settings
    async with ctx.db.tx() as session:
        inviter = await user_repo.get(session, inviter_id)
        if inviter is None:
            return
        result = await ledger.credit(
            session,
            inviter_id,
            int(settings.referral_bonus),
            "admin_grant",
            reference=f"referral:{access.user_id}",
            idempotency_key=f"referral:{inviter_id}:{access.user_id}",
            counterparty=access.user_id,
        )
        if getattr(result, "duplicate", False):
            return
        await user_repo.upsert(
            session,
            access.user_id,
            username=(message.from_user.username if message.from_user else None),
        )
    await text(
        message,
        ctx,
        f"Thanks for coming from {mention(inviter_id)} — they got {money(settings.referral_bonus)}🪙 for referring you.",
    )


@router.message(Command("invite", "ref", "refer"))
async def invite(message: Message, ctx: AppContext) -> None:
    settings = ctx.settings
    code = f"ref-{message.from_user.id if message.from_user else 0}"
    link = f"https://t.me/{settings.bot_username}?start={code}" if settings.bot_username else ""
    await text(
        message,
        ctx,
        f"Send this link — you get <b>{money(settings.referral_bonus)}🪙</b> for each friend who starts the bot.\n{link}",
        buttons=[[callback("📋 copy link", cb("misc", "noop"))]] if link else None,
    )


@router.message(Command("commands"))
async def commands_list(message: Message, ctx: AppContext) -> None:
    """The complete command list, generated from the routers and sent as a file.

    ``/help`` stays curated (readable, one topic at a time) while ``/commands`` is the
    machine-written superset: 130 commands fit neither in a chat message nor in
    Telegram's 100-entry menu, so the residual lives where it can actually be read — and
    because it is generated, it can never drift from what is wired.
    """
    from html import escape

    from waifu.core.dp import primary_commands

    grouped: dict[str, list[tuple[str, str]]] = {}
    for name, (owner, doc) in sorted(primary_commands().items()):
        grouped.setdefault(owner or "other", []).append((name, doc))
    body: list[str] = ["Waifu — every wired command", ""]
    for owner, rows in grouped.items():
        body.append(f"[{owner.replace('_', ' ').title()}] {len(rows)} command(s)")
        body += [f"  /{name}" + (f"  - {doc}" if doc else "") for name, doc in rows]
        body.append("")
    plain = "\n".join(body)
    html_body = "\n".join(
        f"<b>{owner.replace('_', ' ').title()}</b> — {len(rows)}\n"
        + "\n".join(
            f"• <code>/{name}</code>" + (f" — {escape(doc)}" if doc else "") for name, doc in rows
        )
        for owner, rows in grouped.items()
    )
    if len(html_body) <= 3900:
        await text(message, ctx, html_body)
        return

    from aiogram.types import BufferedInputFile

    await message.answer_document(
        BufferedInputFile(plain.encode("utf-8"), filename="commands.txt"),
        caption=f"<b>{sum(len(rows) for rows in grouped.values())} commands</b> across {len(grouped)} plugins — everything listed here is wired and answering.",
    )


@router.message(Command("help", "h", "menu", "ahelp"))
async def help_command(message: Message, ctx: AppContext, command: CommandObject) -> None:
    args = Args.of(command)
    topic = args.first.lower()
    if topic and topic not in HELP_TOPICS and topic != "more":
        await text(
            message,
            ctx,
            f"No topic called <b>{topic}</b>. Try: {', '.join(f'/help {name}' for name in HELP_ORDER)}.",
        )
        return
    await send_help(message, ctx, topic=topic or HELP_ORDER[0])


async def send_help(event: Message | CallbackQuery, ctx: AppContext, *, topic: str) -> None:
    """Curated topics, plus a generated ``more`` page for everything else.

    The curated list is what makes /help readable; the generated page is what keeps it
    honest — a command that ships but is not in :data:`HELP_TOPICS` shows up there
    instead of vanishing (the legacy bot's help drifted into fiction this way).
    """
    documented = {
        name.split()[0].lstrip("/").lower() for entry in HELP_TOPICS.values() for name, _ in entry
    }
    if topic == "more":
        from waifu.core.dp import public_commands

        residual = [
            (name, owner, description)
            for name, (owner, description) in sorted(public_commands().items())
            if name not in documented
        ]
        commands = tuple(
            (f"/{name}", (description or f"({owner})")[:90])
            for name, owner, description in residual[:40]
        )
        if len(residual) > 40:
            commands = (
                *commands,
                (
                    f"… {len(residual) - 40} more",
                    "they are all in the ⊞ command menu (and /commands)",
                ),
            )
    else:
        commands = HELP_TOPICS.get(topic) or ()
    builder = RichMessageBuilder().heading(f"Help · {topic.title()}", size=2)
    builder.table(
        [
            ["command", "what it does"],
            *[[f"<code>{name}</code>", description] for name, description in commands],
        ],
        compact=True,
    )
    builder.divider()
    rows = tabs(topic, [(key, key.title()) for key in (*HELP_ORDER, "more")], prefix="help")
    await edit_or_send(event, ctx, builder=builder, html=_help_plain(commands), rows=rows)


async def edit_or_send(
    event: Message | CallbackQuery,
    ctx: AppContext,
    *,
    builder: RichMessageBuilder,
    html: str,
    rows: list[list[InlineKeyboardButton]],
) -> None:
    """Edit in place when it is a callback (no message spam), send when it is a command."""
    if isinstance(event, CallbackQuery):
        await edit(event, ctx, builder=builder, html=html, buttons=rows)
        return
    await card(event, ctx, builder=builder, html=html, buttons=rows)


def _help_plain(commands: tuple[tuple[str, str], ...]) -> str:
    return "\n".join(f"<b>{name}</b> — {description}" for name, description in commands)


@router.callback_query(F.data.startswith("help:"))
async def help_callback(callback_query: CallbackQuery, ctx: AppContext) -> None:
    parts = (callback_query.data or "").split(":")
    topic = parts[1] if len(parts) > 1 else ""
    if topic not in HELP_TOPICS:
        builder = (
            RichMessageBuilder()
            .heading("Help", size=1)
            .list(
                *[f"/help {key} — {len(HELP_TOPICS[key])} commands" for key in HELP_ORDER],
                numbered=True,
            )
            .footer("The menu button (bottom-left on mobile) lists everything too.")
        )
        rows = [[callback(f"📂 {key.title()}", cb("help", key))] for key in HELP_ORDER]
        await edit_or_send(
            callback_query, ctx, builder=builder, html="Use /help <topic>.", rows=rows
        )
        return
    await send_help(callback_query, ctx, topic=topic)


@router.message(Command("ping", "health"))
async def ping(message: Message, ctx: AppContext) -> None:
    started = time.perf_counter()
    await ctx.bot.get_me()
    api_ms = int((time.perf_counter() - started) * 1000)
    health: dict[str, Any] = {}
    sizes: list[tuple[str, int]] = []
    try:
        mark = time.perf_counter()
        health = await ctx.db.healthcheck()
        sizes = await ctx.db.table_sizes()
        db_ms = int((time.perf_counter() - mark) * 1000)
    except Exception as exc:  # pragma: no cover - only when the DB is down
        await text(message, ctx, f"⚠️ database unavailable: <code>{type(exc).__name__}</code>")
        return
    features = (
        ", ".join(sorted(name for name, enabled in (ctx.api_flags or {}).items() if enabled))
        or "none detected"
    )
    biggest = " · ".join(f"{name} {size // 1024} KB" for name, size in sizes[:3]) or "—"
    builder = (
        RichMessageBuilder()
        .heading("🏓 Pong", size=2)
        .table(
            [
                ["Telegram API", f"{api_ms} ms"],
                [
                    "Database",
                    f"{db_ms} ms · {health.get('db', '?')}"
                    + (f" · {health.get('pg_size')}" if health.get("pg_size") else ""),
                ],
                ["Players", money(health.get("users", 0))],
                ["Uptime", ctx.uptime_text],
                ["Largest tables", biggest],
                ["New API features", features],
            ],
            compact=True,
        )
        .footer(f"waifu {__version__} · mode {ctx.settings.mode}")
    )
    await card(
        message,
        ctx,
        builder=builder,
        html=f"API {api_ms} ms · DB {db_ms} ms · up {ctx.uptime_text}",
    )


@router.message(Command("about", "version"))
async def about(message: Message, ctx: AppContext) -> None:
    settings = ctx.settings
    flags = settings.features
    enabled = [name for name in flags.model_dump() if flags.is_enabled(name)]
    builder = (
        RichMessageBuilder()
        .heading(f"{settings.bot_title} {__version__}", size=1)
        .paragraph(
            html=(
                f"Collection gacha on aiogram 3 targeting Bot API {__api_version__}: rich message cards, "
                "ephemeral replies, streamed drafts, reactions, guest-mode inline answers, Telegram Stars.\n"
                "Every coin moves through an append-only ledger, and every roll is reproducible from a committed seed "
                "(<code>/verify</code>)."
            )
        )
        .table(
            [
                ["enabled features", ", ".join(enabled) or "none"],
                ["roster", "18 tiers"],
                ["mode", settings.mode],
            ],
            compact=True,
        )
    )
    await card(message, ctx, builder=builder, html=f"{settings.bot_title} {__version__}")


@router.message(Command("update", "restart", "changelog", "whatsnew"))
async def changelog(message: Message, ctx: AppContext) -> None:
    """Show the shipped changelog (the reference bot pasted a hard-coded string)."""
    from pathlib import Path

    body = ""
    for candidate in (Path(ctx.settings.changelog_path), Path("docs/CHANGELOG.md")):
        try:
            body = candidate.read_text(encoding="utf-8")
            break
        except OSError:
            continue
    if not body:
        await text(
            message, ctx, f"Version <b>{__version__}</b> — no changelog shipped with this build."
        )
        return
    section = _latest_section(body)
    await text(message, ctx, f"<b>What's new</b>\n{section[:3200]}")


def _latest_section(markdown: str) -> str:
    lines = []
    seen = False
    for line in markdown.splitlines():
        if line.startswith("## "):
            if seen:
                break
            seen = True
            continue
        if seen:
            lines.append(line)
    return "\n".join(lines).strip() or markdown[:2000]


# ------------------------------------------------------------------- preferences
@router.message(Command("settings", "config", "hmode"))
async def settings_command(
    message: Message, ctx: AppContext, session: Any, user: User | None
) -> None:
    if user is None:
        await text(message, ctx, "Send /start once, then /settings works.")
        return
    await send_settings(message, ctx, session, user.id)


async def send_settings(
    event: Message | CallbackQuery, ctx: AppContext, session: Any, user_id: int
) -> None:
    from waifu.db.repositories import users as user_repo

    pref = await user_repo.prefs(session, user_id)
    modes = [
        ("rarity", "Rarity"),
        ("recent", "Recent"),
        ("power", "Power"),
        ("dupes", "Dupes"),
        ("favourites", "Fav"),
    ]
    builder = (
        RichMessageBuilder()
        .heading("Your settings", size=2)
        .table(
            [
                ["collection view", pref.hmode],
                ["profile glow", "on" if pref.glow else "off"],
                ["show balance", "on" if pref.show_balance else "off"],
                ["name style", f"{pref.font} → {style('Naruto', pref.font)}"],
            ],
            compact=True,
            bordered=False,
        )
        .paragraph(html="Tapping a button saves immediately.")
    )
    rows = tabs(pref.hmode, modes, prefix="set:hmode")
    rows.append(
        [
            callback("✨ glow off" if pref.glow else "✨ glow on", cb("set", "glow")),
            callback(
                "💰 balance on" if pref.show_balance else "💰 balance off", cb("set", "balance")
            ),
        ]
    )
    rows.append([callback(font.key, cb("set", "font", font.key)) for font in FONTS])
    await edit_or_send(event, ctx, builder=builder, html="Settings.", rows=rows)


@router.callback_query(F.data.startswith("set:"))
async def settings_callback(
    callback_query: CallbackQuery, ctx: AppContext, session: Any, user: User | None
) -> None:
    if user is None:
        await callback_query.answer()
        return
    from waifu.db.repositories import users as user_repo

    parts = (callback_query.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    value = parts[2] if len(parts) > 2 else ""
    pref = await user_repo.prefs(session, user.id)
    if action == "hmode":
        await user_repo.set_pref(session, user.id, hmode=value or "rarity")
    elif action == "glow":
        await user_repo.set_pref(session, user.id, glow=not pref.glow)
    elif action == "balance":
        await user_repo.set_pref(session, user.id, show_balance=not pref.show_balance)
    elif action == "font":
        await user_repo.set_pref(session, user.id, font=value or "default")
    await send_settings(callback_query, ctx, session, user.id)
    await note(callback_query, "Saved.")


@router.message(Command("font", "fontstyle", "style"))
async def font_command(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, user: User | None
) -> None:
    """``/font`` lists the styles, ``/font gothic`` picks one (parity with the old bot)."""
    if user is None:
        return
    from waifu.db.repositories import users as user_repo

    args = Args.of(command)
    if not args.words:
        sample = message.from_user.first_name if message.from_user else "Naruto"
        lines = "\n".join(f"<b>{font.label}:</b> {style(sample, font.key)}" for font in FONTS)
        await text(
            message,
            ctx,
            f"Pick one — <code>/font &lt;name&gt;</code>, or tap:\n{lines}",
            buttons=[[callback(font.key, cb("set", "font", font.key)) for font in FONTS]],
        )
        return
    wanted = args.first.lower()
    match = next(
        (font for font in FONTS if font.key == wanted or font.label.lower() == wanted), None
    )
    if match is None:
        await text(
            message,
            ctx,
            f"No style called “{wanted}”. Options: {', '.join(font.key for font in FONTS)}.",
        )
        return
    await user_repo.set_pref(session, user.id, font=match.key)
    await text(
        message,
        ctx,
        f"Name style → <b>{match.label}</b>: {style(user.first_name or 'you', match.key)}",
    )


@router.message(Command("language", "lang"))
async def language(
    message: Message, ctx: AppContext, session: Any, command: CommandObject, user: User | None
) -> None:
    """``/language`` shows what is translated; ``/language ja`` sets it."""
    if user is None:
        return
    args = Args.of(command)
    locales = discover_locales(ctx)
    if not args.words:
        listing = (
            "\n".join(f"• <code>{code}</code> — {label}" for code, label in sorted(locales.items()))
            or "• <code>en</code> — English (built in)"
        )
        await text(
            message, ctx, f"Interface language:\n{listing}\nSet yours with /language &lt;code&gt;."
        )
        return
    code = args.first.lower()
    if code not in locales:
        await text(
            message, ctx, f"No catalogue for “{code}”. Available: {', '.join(sorted(locales))}."
        )
        return
    from waifu.db.repositories import users as user_repo

    # ``locale`` is a column on the player row (upsert owns the write), not a pref flag.
    await user_repo.upsert(session, user.id, locale=code)
    await text(
        message,
        ctx,
        f"Interface language → {locales[code]}. Bot messages follow it where a catalogue exists.",
    )


# ------------------------------------------------------------------ AUX routers
@member_bonus_router.my_chat_member(
    F.my_chat_member.new_status.in_({"member", "administrator", "creator"})
)
async def added_to_chat(update: ChatMemberUpdated, ctx: AppContext, session: Any) -> None:
    """Getting added registers the chat (spawn feed off until an owner turns it on)."""
    from waifu.db.repositories import spawns as spawn_repo

    await spawn_repo.register_group(session, chat_id=update.chat.id, title=update.chat.title or "")
    await ctx.notify(
        f"➕ added to <b>{update.chat.title or update.chat.id}</b> — /summon to start the feed",
        silent=True,
    )


@member_bonus_router.my_chat_member(F.my_chat_member.new_status == "kicked")
async def removed_from_chat(update: ChatMemberUpdated, ctx: AppContext, session: Any) -> None:
    from waifu.db.repositories import spawns as spawn_repo

    await spawn_repo.unregister_group(session, update.chat.id)


@chat_member_router.chat_member(F.chat_member.new_status == "member")
async def new_member(update: ChatMemberUpdated, ctx: AppContext, session: Any) -> None:
    """Welcome text, delivered ephemerally to the joiner only."""
    member = update.chat_member.new_chat_member.user
    if member.is_bot or member.id <= 0:
        return
    group = await ctx.moderation.group(
        session, update.chat.id, title=update.chat.title or "", create=False
    )
    welcome = (
        str((getattr(group, "data", None) or {}).get("welcome_text") or "")
        if group is not None
        else ""
    )
    if not welcome:
        return
    from waifu.tg.ephemeral import ephemeral_note

    await ephemeral_note(
        ctx.bot,
        update.chat.id,
        receiver_user_id=member.id,
        text=welcome.format(
            name=member.full_name,
            mention=f"@{member.username}" if member.username else member.full_name,
            id=member.id,
        )[:4000],
    )


@reaction_router.message_reaction()
async def on_reaction(update: Any, ctx: AppContext, session: Any) -> None:
    """React with 🔥 on a live spawn to claim it (Bot API 9.3 reactions).

    Same transaction as the rest of the update, so a claim that wins races the /claim
    button rather than duplicating it: ``spawns.claim`` is a conditional UPDATE.
    """
    if not ctx.features.is_enabled("reactions"):
        return
    chat_id, user_id, message_id = (
        getattr(update, "chat", None),
        None,
        getattr(update, "message_id", None),
    )
    if chat_id is None or message_id is None:
        return
    reaction = _reaction_emoji(update)
    if reaction not in _CLAIM_EMOJIS:
        return
    view = await ctx.spawn.current(session, int(chat_id.id))
    if view is None or (view.message_id and int(view.message_id) != int(message_id)):
        return
    from waifu.db.models import User as UserModel

    user_id = int(getattr(getattr(update, "user", None), "id", 0) or 0)
    if not user_id:
        return
    result = await ctx.spawn.claim(session, user_id, spawn_id=view.id)
    if not result.won:
        await ctx.react(int(chat_id.id), int(message_id), "🚫")
        return
    player = await session.get(UserModel, user_id)
    await ctx.spawn.settle_card(
        int(chat_id.id),
        view,
        winner_name=(player.first_name if player else str(user_id)),
        message_id=view.message_id,
    )


_CLAIM_EMOJIS = frozenset({"🔥", "🎉", "✨", "⚡"})


def _reaction_emoji(update: Any) -> str:
    for reaction in getattr(update, "new_reaction", None) or []:
        emoji = getattr(reaction, "emoji", None)
        if emoji:
            return str(emoji)
    return ""


@poll_router.poll_answer()
async def on_poll_answer(answer: PollAnswer, ctx: AppContext, session: Any) -> None:
    """Poll answers are raffle entries (``/raffle`` posts a 👍/🎉 poll)."""
    # A ``PollAnswer`` carries the *poll* id, not the message id, so the raffle is
    # resolved per chat (``current_raffle``) instead of per message: one open raffle
    # per chat is the invariant ``/raffle`` already enforces.
    if not answer.option_ids:
        return
    raffle = await ctx.premium.current_raffle(session, answer.chat_id)
    if raffle is None or int(getattr(raffle, "poll_id", 0) or 0) != int(answer.poll_id or 0):
        return
    if int(answer.option_ids[0]) != 0:  # option 0 is the "enter" cell of the poll
        return
    await ctx.premium.add_entrant(int(raffle.id), int(answer.user.id))


@business_router.business_message()
async def on_business_message(message: Message, ctx: AppContext, session: Any) -> None:
    """A customer message that arrived through business mode: answer, then log it.

    Telegram routes a business account's chats to the bot; without this handler the
    owner simply stops receiving support messages the moment they connect a business.
    """
    from waifu.tg.business import BusinessTicket, mirror_to_support, reply

    ticket = BusinessTicket.from_message(message)
    if ticket is None or not ticket.text:
        return
    if ctx.ai is not None and ctx.features.is_enabled("ai"):
        answer = await ctx.ai.summary_for(session, ticket.user_id)
        if answer:
            await reply(ctx.bot, ticket, answer[0])
    support = ctx.settings.support_chat_id
    if support:
        await mirror_to_support(ctx.bot, support, ticket)


@guest_router.guest_message()
async def on_guest_message(message: Message, ctx: AppContext, session: Any) -> None:
    """Guest mode (Bot API 10.0): answer inside a channel post without being a member.

    This is how “reply to me in the announcement channel” works now; the reference bot
    needed to be added to the chat, which channel owners refuse to do.
    """
    from waifu.tg.guest import answer, refuse

    if not ctx.caps.allow("guest_mode"):
        return
    topic = (message.text or "").strip().lstrip("/").lower()
    if not topic:
        return
    if topic in {"help", "start"}:
        await answer(
            ctx.bot,
            message,
            f"I run {ctx.settings.bot_title}: /pull, /collection, /market. Add me to a group to play.",
            title="Waifu bot",
        )
        return
    try:
        async with ctx.db.session() as session2:
            page = await ctx.collection.search(session2, topic, limit=1)
    except Exception:  # pragma: no cover - defensive: guest answers must never raise
        page = None
    hits = (page or ([], 0))[0]
    if not hits:
        await refuse(ctx.bot, message, f"No character called “{topic[:40]}” in my roster.")
        return
    character = hits[0]
    from waifu.enums import Rarity

    rarity = Rarity.from_value(int(character.rarity_id))
    await answer(
        ctx.bot,
        message,
        f"{rarity.badge} <b>{character.name}</b> · {character.anime}\nprice {money(character.price)}🪙 · power {character.stat_power}",
        title=f"{character.name} — {ctx.settings.bot_title}",
    )


@router.callback_query(F.data == "misc:support")
async def support_button(callback_query: CallbackQuery, ctx: AppContext) -> None:
    """Deep-link into the support group via a fresh invite link (the bot may not be an
    admin there, so a hard-coded ``t.me/`` link is the fallback, not an error)."""
    chat = ctx.settings.support_chat_id
    if not chat:
        await callback_query.answer(
            "No support group is configured on this instance.", show_alert=True
        )
        return
    try:
        invite = await ctx.bot.create_chat_invite_link(chat, name="waifu-bot-support")
        await callback_query.answer(url=invite.invite_link, cache_time=3600)
    except Exception:  # pragma: no cover - depends on the chat's rights
        await callback_query.answer(
            url=f"https://t.me/c/{str(-abs(int(chat)))[4:]}", cache_time=3600
        )


def discover_locales(ctx: AppContext) -> dict[str, str]:
    """``waifu/data/locales/*.json`` → ``{"en": "English", ...}``.

    A deployment adds a language by dropping a file in, no code change; the keys it
    skips fall back to English at render time (see :mod:`waifu.settings`).
    """
    import json
    from pathlib import Path

    found = {"en": "English"}
    root = Path(__file__).resolve().parents[1] / "data" / "locales"
    for candidate in sorted(root.glob("*.json")) if root.is_dir() else []:
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        found[candidate.stem] = str(payload.get("language") or candidate.stem.upper())
    del ctx
    return found


@router.callback_query(F.data == "misc:noop")
async def noop(callback_query: CallbackQuery) -> None:
    await callback_query.answer(cache_time=3600)
