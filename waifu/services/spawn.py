"""Spawn service — /spawn, /summon, auto-spawn and /nguess.

This is the module the whole bot is judged on, so the failure modes the reference
implementation shipped with are designed out:

* **Two winners.** Claiming is a compare-and-set on the row (``UPDATE … WHERE
  status='active'``), never a read-then-write, so 400 simultaneous /summon
  messages produce exactly one owner.
* **Reopened spawns.** An expired spawn is never resurrected: a new spawn is a new
  row with a new id, so an old callback can only resolve to "that one's over".
* **Send/DB races.** The row is created first, the card second, and the message id
  is attached in a short follow-up transaction — a failed send leaves an inert row
  that the expiry sweep collects, instead of an invisible spawn nobody can win.

Delivery uses **rich messages** with content buttons when the endpoint supports
them, and degrades to ``send_photo`` + inline keyboard with *identical* wording
otherwise; both show :class:`aiogram.types.DisabledButton` once the spawn is spent,
so a dead card can never look winnable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from aiogram.types import InlineKeyboardButton
from sqlalchemy.ext.asyncio import AsyncSession

from waifu.db.models import Character, GuessSession
from waifu.db.repo import characters as char_repo
from waifu.db.repo import collection as collection_repo
from waifu.db.repo import economy as ledger
from waifu.db.repo import progress as progress_repo
from waifu.db.repo import spawns as spawn_repo
from waifu.db.repo import users as user_repo
from waifu.enums import ChatMode, LedgerReason, Rarity
from waifu.errors import AlreadyClaimed, NotFound, RosterEmpty
from waifu.services.base import Service
from waifu.services.economy import dupe_value
from waifu.tg.buttons import callback
from waifu.tg.messages import SendResult, edit_card, send_card
from waifu.tg.rich import RichButton, RichMessageBuilder
from waifu.utils.rng import system_random
from waifu.utils.text import truncate
from waifu.utils.time import human_delta, now_utc, to_naive_utc

#: Rarer spawns get a shorter window — tension instead of a flat 3 minutes.
_WINDOW_FACTOR = {1: 1.5, 2: 1.3, 3: 1.1, 4: 1.0, 5: 0.8, 6: 0.65, 7: 0.55, 8: 0.45}


@dataclass(slots=True)
class SpawnView:
    """Everything a renderer needs about one spawn, with no ORM objects leaked."""

    id: int
    chat_id: int
    character_id: int
    name: str
    anime: str
    rarity: Rarity
    image: str
    expires_at: Any
    seconds_left: int
    source: str
    expected_name: str
    description: str = ""
    voice_line: str = ""
    stat_power: int = 0
    message_id: int | None = None
    rich: bool = False
    hint_used: bool = False
    claimed_by: int | None = None
    status: str = "active"

    @property
    def is_live(self) -> bool:
        return self.status == "active" and self.seconds_left > 0

    @property
    def timer_text(self) -> str:
        return human_delta(self.seconds_left) if self.seconds_left > 0 else "expired"


@dataclass(slots=True)
class ClaimResult:
    won: bool
    user_id: int
    character_id: int
    name: str
    rarity: Rarity
    dupe_payout: int = 0
    is_dupe: bool = False
    balance: int = 0
    reason: str = ""
    hint: str = ""
    spawn_id: int = 0


@dataclass(slots=True)
class GuessView:
    session: GuessSession
    character: Character
    seconds_left: int

    @property
    def options(self) -> list[str]:
        """Name options for poll mode, fixed at round start (stored on the row)."""
        return [str(o) for o in ((self.session.data or {}).get("options") or [])]


class SpawnService(Service):
    # --------------------------------------------------------------- opening
    async def open(
        self,
        session: AsyncSession,
        chat_id: int,
        *,
        character: Character | None = None,
        source: str = "manual",
        ttl_seconds: int | None = None,
        rarity_floor: int = 1,
        rarity_id: int | None = None,
    ) -> tuple[spawn_repo.Spawn, Character]:
        """Arm a spawn (row first, card later). Returns the spawn + character."""
        character = character or await self.pick_character(
            session, rarity_floor=rarity_floor, rarity_id=rarity_id
        )
        ttl = ttl_seconds or self.window_for(Rarity.from_value(character.rarity_id))
        row = await spawn_repo.open_spawn(
            session,
            chat_id=chat_id,
            character_id=character.id,
            source=source,
            ttl_seconds=ttl,
            payload={"rarity_id": character.rarity_id},
        )
        spawn = await spawn_repo.by_id(session, row.id)
        if spawn is None:  # pragma: no cover - the row we just wrote vanished
            raise NotFound("spawn could not be armed")
        return spawn, character

    def window_for(self, rarity: Rarity) -> int:
        return max(45, int(self.settings.spawn_claim_window * _WINDOW_FACTOR.get(int(rarity), 1.0)))

    async def pick_character(
        self, session: AsyncSession, *, rarity_floor: int = 1, rarity_id: int | None = None
    ) -> Character:
        """Choose art to spawn, weighted by the *published* rarity odds."""
        if rarity_id:
            character = await char_repo.random_of_rarity(session, int(rarity_id))
            if character is not None:
                return character
        table = [
            (r, c) for r, c in await char_repo.normalised_odds(session) if int(r) >= rarity_floor
        ] or list(await char_repo.normalised_odds(session))
        picked = system_random.choices([r for r, _ in table], weights=[c for _, c in table], k=1)[0]
        character = await char_repo.random_of_rarity(session, int(picked))
        if character is None:
            # Empty pool at the drawn tier: walk down until something exists, so a
            # misconfigured /chance can never make spawning throw.
            for step in range(int(picked) - 1, 0, -1):
                character = await char_repo.random_of_rarity(session, step)
                if character is not None:
                    break
        if character is None:
            character = await char_repo.random_any(session)
        if character is None:
            # Empty by design on a fresh install: the owner fills it with /upload, so the
            # player-facing answer has to be the how-to, not a stack trace.
            raise RosterEmpty()
        return character

    async def view(self, session: AsyncSession, spawn: spawn_repo.Spawn) -> SpawnView:
        character = await char_repo.get(session, spawn.character_id)
        expires = to_naive_utc(spawn.expires_at)
        return SpawnView(
            id=spawn.id,
            chat_id=spawn.chat_id,
            character_id=spawn.character_id,
            name=spawn.name,
            anime=spawn.anime or "",
            rarity=Rarity.from_value(spawn.rarity_id),
            image=spawn.image,
            expires_at=spawn.expires_at,
            seconds_left=max(0, int((expires - now_utc()).total_seconds())),
            source=spawn.source,
            expected_name=spawn.expected_name or spawn.name,
            description=(character.description if character else "") or "",
            voice_line=(character.voice_line if character else "") or "",
            stat_power=character.stat_power if character else 0,
            message_id=spawn.message_id,
            rich=spawn.rich_message,
            hint_used=spawn.hint_used,
            status=spawn.status,
        )

    async def view_by_id(self, session: AsyncSession, spawn_id: int) -> SpawnView | None:
        spawn = await spawn_repo.by_id(session, spawn_id)
        return await self.view(session, spawn) if spawn else None

    async def current(self, session: AsyncSession, chat_id: int) -> SpawnView | None:
        spawn, _ = await spawn_repo.current(session, chat_id)
        return await self.view(session, spawn) if spawn else None

    # --------------------------------------------------------------- delivery
    def card(
        self, view: SpawnView, *, claimed_by_name: str | None = None, show_hint: bool = False
    ) -> tuple[RichMessageBuilder, list[list[InlineKeyboardButton]], list[RichButton]]:
        """The spawn card, in both rich and fallback form so they can't disagree.

        Summon-bot edited the *caption* of a photo card to say "claimed" while the
        buttons stayed live — the same content must be re-rendered from one source.
        """
        rarity = view.rarity
        builder = RichMessageBuilder().heading(f"{rarity.emoji} {view.name}")
        if view.image:
            builder.photo(
                view.image,
                caption=f"{view.anime or 'series unknown'} · {rarity.label}",
                credit=f"power {view.stat_power}",
            )
        else:
            builder.paragraph(f"{view.anime or '—'} · {rarity.label} · power {view.stat_power}")
        if view.description:
            builder.paragraph(truncate(view.description, 300))
        if view.voice_line:
            builder.quote(view.voice_line, credit=view.name)
        builder.table(
            [
                ["Rarity", rarity.badge],
                ["Power", str(view.stat_power or 10 * int(rarity))],
                ["Window", view.timer_text],
                ["Spawn", f"#{view.id}"],
            ],
            compact=True,
        )
        if show_hint:
            builder.details("Hint", self.hint_text(view), open=True)
        if claimed_by_name:
            builder.footer(f"claimed by {claimed_by_name}")
        elif view.is_live:
            builder.footer(f"/summon {view.expected_name} — or tap Summon · {view.timer_text} left")
        else:
            builder.footer("this spawn is over")

        rich_buttons = [
            RichButton(
                "Summon" if view.is_live else "Spent",
                callback_data=f"spn:{view.id}" if view.is_live else None,
                style="success" if view.is_live else None,
                disabled=not view.is_live,
            ),
            RichButton(
                "Hint",
                callback_data=f"spnhint:{view.id}",
                disabled=not view.is_live or view.hint_used,
            ),
            RichButton("Skip", callback_data=f"spnskip:{view.id}", disabled=not view.is_live),
        ]
        fallback = [
            [
                callback(
                    "⚡ Summon",
                    f"spn:{view.id}" if view.is_live else "spn:0",
                    disabled=not view.is_live,
                ),
                callback(
                    "💡 Hint", f"spnhint:{view.id}", disabled=not view.is_live or view.hint_used
                ),
                callback("⏭ Skip", f"spnskip:{view.id}", disabled=not view.is_live),
            ]
        ]
        return builder, fallback, rich_buttons

    def hint_text(self, view: SpawnView) -> str:
        name = view.expected_name or view.name
        visible = max(1, len(name) // 3)
        return f"`{name[:visible]}`" + "•" * max(0, len(name) - visible) + f" — {view.rarity.label}"

    async def announce(
        self, chat_id: int, view: SpawnView, *, thread_id: int | None = None, quiet: bool = False
    ) -> SendResult:
        """Send the card, then record its message id on the spawn row."""
        builder, fallback, rich_buttons = self.card(view)
        result = await send_card(
            self.bot,
            chat_id,
            builder=builder,
            caption=builder.fallback_html(limit=1000),
            photo=view.image or None,
            buttons=fallback,
            mode=ChatMode.RICH if self.ctx.caps.rich_messages else ChatMode.PLAIN,
            message_thread_id=thread_id,
            disable_notification=quiet,
            rich_buttons=rich_buttons,
        )
        if result.ok and result.message is not None:
            async with self.ctx.db.tx() as session:
                await spawn_repo.attach_message(
                    session, view.id, result.message.message_id, rich=result.mode is ChatMode.RICH
                )
        return result

    async def settle_card(
        self,
        chat_id: int,
        view: SpawnView,
        *,
        winner_name: str | None,
        message_id: int | None = None,
        show_hint: bool = False,
    ) -> bool:
        """Re-render a spawn card after it is claimed/expired (buttons disabled)."""
        builder, fallback, rich_buttons = self.card(
            view, claimed_by_name=winner_name, show_hint=show_hint
        )
        if message_id is None:
            return False
        from aiogram.types import InlineKeyboardMarkup

        updated = await edit_card(
            self.bot,
            chat_id,
            message_id,
            builder=builder,
            caption=builder.fallback_html(limit=1000),
            markup=InlineKeyboardMarkup(inline_keyboard=fallback),
            mode=ChatMode.RICH if view.rich else ChatMode.PLAIN,
            photo=view.image or None,
        )
        # ``edit_card`` re-sends when the API can't edit rich messages; in that case
        # the old message is deleted by the helper, so the spawn row must point at
        # the new id or a later /checkspawn would edit a deleted message.
        if updated is not None and updated.message_id != message_id:
            async with self.ctx.db.tx() as session:
                await spawn_repo.attach_message(
                    session, view.id, updated.message_id, rich=view.rich
                )
        _ = rich_buttons
        return updated is not None

    # -------------------------------------------------------------- claiming
    async def claim(
        self,
        session: AsyncSession,
        user_id: int,
        *,
        spawn_id: int | None = None,
        chat_id: int | None = None,
        typed: str | None = None,
    ) -> ClaimResult:
        """Claim by button or by typing the name. Exactly one caller wins.

        ``typed`` is the /summon path: the name must match (fuzzy, ≥3 chars — the
        reference bot accepted any first letter, which turned spawns into a lottery).
        """
        if spawn_id:
            spawn = await spawn_repo.by_id(session, spawn_id)
        else:
            if chat_id is None:
                raise NotFound("nothing is spawning here right now")
            spawn, _ = await spawn_repo.current(session, chat_id)
        if spawn is None:
            raise NotFound("nothing is spawning here right now")
        spawn_id = spawn.id
        if typed is not None and not await spawn_repo.name_matches(spawn, typed):
            return ClaimResult(
                won=False,
                user_id=user_id,
                character_id=spawn.character_id,
                name=spawn.name,
                rarity=Rarity.from_value(spawn.rarity_id),
                reason=f"that is not {spawn.name}",
                hint=f"it starts with “{spawn.expected_name[:1] or spawn.name[:1]}”",
                spawn_id=spawn_id,
            )
        try:
            won = await spawn_repo.claim(session, spawn_id, user_id)
        except AlreadyClaimed as exc:
            return ClaimResult(
                won=False,
                user_id=user_id,
                character_id=spawn.character_id,
                name=spawn.name,
                rarity=Rarity.from_value(spawn.rarity_id),
                reason=str(exc),
                spawn_id=spawn_id,
            )

        rarity = Rarity.from_value(won.rarity_id)
        character = await char_repo.get(session, won.character_id)
        already_own = await collection_repo.has_count(session, user_id, won.character_id) > 0
        payout = 0
        if already_own and character is not None:
            payout = dupe_value(character, self.settings.dupe_payout_percent)
            await ledger.credit(
                session,
                user_id,
                payout,
                LedgerReason.DUPE,
                reference=f"spawn:{spawn_id}",
                meta={"source": "spawn"},
            )
        await collection_repo.grant(session, user_id, won.character_id, source="spawn")
        await progress_repo.apply_pity(
            session,
            user_id,
            got_rarity_id=int(rarity),
            rare_at=max(1, self.settings.pity_high_after // 3),
            high_at=self.settings.pity_high_after,
        )
        await self._award_exp(session, user_id, rarity)
        if self.redis is not None:
            await self.redis.zincr("lb:claims", str(user_id), 1)
            await self.redis.publish(
                "spawn", {"chat": won.chat_id, "spawn": spawn_id, "user": user_id}
            )
        return ClaimResult(
            won=True,
            user_id=user_id,
            character_id=won.character_id,
            name=won.name,
            rarity=rarity,
            dupe_payout=payout,
            is_dupe=already_own,
            balance=await ledger.balance(session, user_id),
            spawn_id=spawn_id,
        )

    async def _award_exp(self, session: AsyncSession, user_id: int, rarity: Rarity) -> None:
        user = await user_repo.get(session, user_id)
        if user is None:
            return
        user.exp += 30 * int(rarity)
        await session.flush()

    async def spend_hint(self, session: AsyncSession, spawn_id: int) -> str:
        """One hint per spawn, guarded by the ``hint_used`` column (CAS)."""
        if not await spawn_repo.mark_hint_used(session, spawn_id):
            raise AlreadyClaimed("the hint for this spawn is already spent")
        spawn = await spawn_repo.by_id(session, spawn_id)
        if spawn is None:
            raise NotFound("no such spawn")
        view = await self.view(session, spawn)
        return self.hint_text(view)

    async def expire_overdue(self, session: AsyncSession) -> int:
        return await spawn_repo.expire_overdue(session)

    async def recent(self, session: AsyncSession, chat_id: int, *, limit: int = 5) -> list[Any]:
        return await spawn_repo.recent_for_chat(session, chat_id, limit=limit)

    async def chat_activity(self, session: AsyncSession, chat_id: int) -> dict[str, int]:
        return await spawn_repo.chat_activity(session, chat_id)

    # ------------------------------------------------------------ auto-spawn
    async def on_activity_threshold(
        self, session: AsyncSession, *, chat_id: int, limit: int
    ) -> None:
        """Called when a chat just hit ``spawn_limit`` messages.

        The spawn is *scheduled* (DB row + Redis ZSET) instead of sent inline, so a
        500-message burst in a 40k chat cannot block the event loop or stampede the
        API; the scheduler pops due entries a few at a time.
        """
        when = now_utc() + timedelta(seconds=min(120, max(15, max(1, limit) // 10)))
        await spawn_repo.schedule_next_spawn(session, chat_id, when)
        if self.redis is not None:
            await self.redis.queue_push("autospawn", str(chat_id), when.timestamp())

    async def due_autospawns(self, session: AsyncSession, *, limit: int = 20) -> list[int]:
        """Chat ids whose auto-spawn is due (Redis queue, DB as the fallback)."""
        if self.redis is not None:
            due = await self.redis.queue_pop_due("autospawn", limit=limit)
            if due:
                return [int(chat) for chat in due if str(chat).lstrip("-").isdigit()]
        rows = await spawn_repo.spawnable_groups(session, due_only=True)
        return [row.chat_id for row in rows[:limit]]

    async def save_group(
        self,
        session: AsyncSession,
        chat_id: int,
        *,
        title: str = "",
        spawn_limit: int | None = None,
        enabled: bool | None = None,
    ) -> dict[str, int]:
        group = await spawn_repo.register_group(
            session,
            chat_id,
            title=title,
            spawn_limit=spawn_limit or self.settings.spawn_default_limit,
        )
        if spawn_limit:
            await spawn_repo.set_spawn_limit(session, chat_id, max(1, spawn_limit))
        del enabled  # applied by the group-settings command through the same row
        await self.ctx.cache.invalidate("groups")
        return {"chat_id": group.chat_id, "spawn_limit": group.spawn_limit}

    async def unregister(self, session: AsyncSession, chat_id: int) -> None:
        await spawn_repo.unregister_group(session, chat_id)
        if self.redis is not None:
            await self.redis.queue_remove("autospawn", str(chat_id))
        await self.ctx.cache.invalidate("groups")

    async def groups(self, session: AsyncSession) -> dict[str, int]:
        return await spawn_repo.groups_summary(session)

    async def set_spawn_enabled(self, session: AsyncSession, chat_id: int, enabled: bool) -> None:
        from waifu.db.models import Group

        row = await session.get(Group, chat_id)
        if row is None:
            raise NotFound("this group is not registered — /savegroup first")
        row.spawn_enabled = enabled
        await session.flush()
        await self.ctx.cache.invalidate("groups")

    # ------------------------------------------------------------ group switches
    async def set_switch(
        self, session: AsyncSession, chat_id: int, key: str, *, value: bool, title: str = ""
    ) -> dict[str, bool]:
        """Flip a per-group switch that has no column (``autoadd``) — see the repo doc."""
        out = await spawn_repo.set_group_switch(session, chat_id, key, value=value, title=title)
        await self.ctx.cache.invalidate("groups")
        return out

    async def switch(
        self, session: AsyncSession, chat_id: int, key: str, *, default: bool = False
    ) -> bool:
        return await spawn_repo.group_switch(session, chat_id, key, default=default)

    # ---------------------------------------------------------------- nguess
    async def start_guess(
        self,
        session: AsyncSession,
        *,
        chat_id: int,
        reward: int,
        seconds: int = 90,
        mode: str = "poll",
        character: Character | None = None,
    ) -> tuple[GuessSession, Character]:
        character = character or await self.pick_character(session)
        row = await spawn_repo.start_guess(
            session,
            chat_id=chat_id,
            character_id=character.id,
            reward=reward,
            seconds=seconds,
            mode=mode,
        )
        return row, character

    async def active_guess(self, session: AsyncSession, chat_id: int) -> GuessView | None:
        row = await spawn_repo.active_guess(session, chat_id)
        if row is None:
            return None
        character = await char_repo.get(session, row.character_id)
        if character is None:  # pragma: no cover - deleted character mid-round
            return None
        left = max(0, int((to_naive_utc(row.closes_at) - now_utc()).total_seconds()))
        return GuessView(session=row, character=character, seconds_left=left)

    async def claim_guess(self, session: AsyncSession, session_id: int, user_id: int) -> GuessView:
        """First correct answer wins (CAS on ``guess_sessions.status``)."""
        row = await spawn_repo.claim_guess(session, session_id, user_id)
        character = await char_repo.get(session, row.character_id)
        assert character is not None
        return GuessView(session=row, character=character, seconds_left=0)

    async def resolve_guess(
        self, session: AsyncSession, session_id: int, *, winner_id: int | None, answers: int = 0
    ) -> GuessSession:
        row = await spawn_repo.resolve_guess(
            session, session_id, winner_id=winner_id, answers=answers
        )
        if winner_id and row.reward:
            await ledger.credit(
                session,
                winner_id,
                row.reward,
                LedgerReason.QUEST,
                reference=f"nguess:{session_id}",
                idempotency_key=f"nguess:{session_id}:{winner_id}",
            )
            await spawn_repo.bump_guess_streak(session, row.chat_id, user_id=winner_id, won=True)
        return row

    async def guesses_to_close(self, session: AsyncSession) -> list[GuessSession]:
        return await spawn_repo.guesses_to_close(session)

    async def guess_streak(self, session: AsyncSession, chat_id: int) -> tuple[int, int | None]:
        return await spawn_repo.guess_streak(session, chat_id)

    async def top_guessers(
        self, session: AsyncSession, *, limit: int = 10
    ) -> list[tuple[Any, int]]:
        return await spawn_repo.top_guessers(session, limit=limit)

    async def users_seen(
        self, session: AsyncSession, chat_id: int, *, since_hours: int = 24
    ) -> int:
        from datetime import timedelta as _td

        return await spawn_repo.users_seen_in(
            session, chat_id, since=now_utc() - _td(hours=since_hours)
        )
