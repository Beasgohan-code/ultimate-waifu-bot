"""Moderation service — warnings, temp bans, cases, group config, broadcasts.

Three things Summon-bot's admin surface got wrong, all fixed here:

1. **no evidence trail** — every action here writes a ``moderation_cases`` row plus
   an ``audit_logs`` line *in the same transaction as the Telegram call's effect*,
   so "the mod banned the wrong person" is answerable with a screenshot-free audit;
2. **mute expiry was in-process** — a restart left people muted forever. Mutes are
   rows with ``expires_at`` and the scheduler unmutes them (and closes the case);
3. **broadcast had no pacing** — see :func:`waifu.tg.notify.broadcast`, which paces
   under Telegram's per-second ceiling, classifies each failure and reports counts.

Group admins can moderate their own chat, but never globally: the permission check
is ``access.can("moderate")`` + ``chat_id`` scoping, enforced in the handlers by
:meth:`Access.require`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from waifu.core.access import Access
from waifu.db.models import ModerationCase
from waifu.db.repo import moderation as mod_repo
from waifu.db.repo import monetize as monetize_repo
from waifu.db.repo import spawns as spawn_repo
from waifu.db.repo import stats as stats_repo
from waifu.db.repo import trades as trade_repo
from waifu.db.repo import users as user_repo
from waifu.errors import NotFound, PermissionDenied, WaifuError
from waifu.services.base import Service
from waifu.tg.notify import broadcast as _broadcast
from waifu.tg.notify import safe_send
from waifu.utils.text import truncate
from waifu.utils.time import now_utc

#: Warning count → consequence. Configurable via settings, mirrored here for defaults.
DEFAULT_LADDER: dict[int, str] = {
    "warn": "warning",
    "mute_1h": "1h mute",
    "mute_24h": "24h mute",
    "ban": "ban",
}


@dataclass(slots=True)
class WarnResult:
    user_id: int
    chat_id: int
    count: int
    action: str
    case_id: int | None = None
    duration: int = 0
    note: str = ""


@dataclass(slots=True)
class BroadcastResult:
    targets: int
    sent: int
    blocked: int
    failed: int
    dry_run: bool = False
    sample: str = ""

    @property
    def summary(self) -> str:
        return f"sent {self.sent}/{self.targets} · {self.blocked} blocked · {self.failed} failed"


class ModerationService(Service):
    # --------------------------------------------------------------- warnings
    async def warn(
        self,
        session: AsyncSession,
        *,
        chat_id: int,
        user_id: int,
        moderator_id: int,
        reason: str = "",
        count: int = 1,
    ) -> WarnResult:
        """Add warning(s), open a case, apply the auto-ladder if configured.

        The ladder (3 warnings → 1h mute, 4 → 24h, 5 → ban) is what makes warnings
        mean something; without it every server's mod queue grows forever.
        """
        _row, total = await mod_repo.add_warning(
            session, chat_id=chat_id, user_id=user_id, moderator_id=moderator_id, reason=reason
        )
        action = "warn"
        duration = 0
        ladder = self.settings.warn_ladder
        for threshold, seconds in sorted(ladder.items()):
            if total >= int(threshold):
                action, duration = ("ban", 0) if int(seconds) < 0 else ("mute", int(seconds))
        case = await mod_repo.open_case(
            session,
            chat_id=chat_id,
            target_user_id=user_id,
            moderator_id=moderator_id,
            action=action,
            reason=reason,
            duration=duration,
        )
        await mod_repo.audit(
            session,
            actor_id=moderator_id,
            action=f"warn:{action}",
            target=str(user_id),
            detail=f"chat={chat_id} total={total} reason={reason!r}",
            chat_id=chat_id,
            scope="chat",
        )
        # The group's own log channel sees moderation too, not only the owner.
        await self.ctx.group_notify(
            session,
            chat_id,
            f"⚠️ warning {total} for {user_id}"
            + (f" → {action}" if action != "warn" else "")
            + (f" ({reason})" if reason else ""),
        )
        return WarnResult(
            user_id=user_id,
            chat_id=chat_id,
            count=total,
            action=action,
            case_id=case.id,
            duration=duration,
        )

    async def remove_warning(
        self,
        session: AsyncSession,
        *,
        chat_id: int,
        user_id: int,
        moderator_id: int,
        count: int = 1,
    ) -> int:
        left = await mod_repo.remove_warning(session, chat_id, user_id, count=count)
        await mod_repo.audit(
            session,
            actor_id=moderator_id,
            action="warn.remove",
            target=str(user_id),
            detail=f"chat={chat_id} left={left}",
            chat_id=chat_id,
            scope="chat",
        )
        return left

    async def warnings(self, session: AsyncSession, chat_id: int, user_id: int) -> list[Any]:
        return await mod_repo.warnings_for(session, chat_id, user_id)

    async def warning_board(
        self, session: AsyncSession, chat_id: int, *, limit: int = 20
    ) -> list[tuple[int, int]]:
        return await mod_repo.warning_counts(session, chat_id, limit=limit)

    # ------------------------------------------------------------------- cases
    async def cases(
        self, session: AsyncSession, chat_id: int, *, user_id: int | None = None, limit: int = 25
    ) -> list[ModerationCase]:
        return await mod_repo.cases(session, chat_id, user_id=user_id, limit=limit)

    async def close_case(self, session: AsyncSession, case_id: int) -> None:
        await mod_repo.close_case(session, case_id)

    async def due_unmutes(self, session: AsyncSession) -> list[ModerationCase]:
        return await mod_repo.due_unmutes(session)

    async def close_active(
        self, session: AsyncSession, chat_id: int, user_id: int, *, action: str | None = None
    ) -> int:
        return await mod_repo.close_active(session, chat_id, user_id, action=action)

    # -------------------------------------------------- Telegram-side actions
    async def restrict(
        self,
        chat_id: int,
        user_id: int,
        *,
        action: str,
        seconds: int = 0,
        reason: str = "",
        delete_last: int = 0,
    ) -> dict[str, Any]:
        """Kick / mute / ban in Telegram. Returns what actually happened.

        ``delete_last`` uses ``deleteMessage`` on tracked ids rather than
        ``deleteChatMessages`` (which needs the newer right) so it works for every
        group the bot admins, and the removed message ids are stored on the case.
        """
        bot: Bot = self.bot
        result: dict[str, Any] = {
            "action": action,
            "ok": False,
            "error": "",
            "until": 0,
            "deleted": 0,
        }
        try:
            if action == "kick":
                await bot.ban_chat_member(chat_id, user_id, until_date=None)
                await bot.unban_chat_member(
                    chat_id, user_id
                )  # lift the ban immediately → a real kick
            elif action == "mute":
                from aiogram.types import ChatPermissions

                until = now_utc() + timedelta(seconds=max(60, seconds)) if seconds else None
                await bot.restrict_chat_member(
                    chat_id, user_id, ChatPermissions(can_send_messages=False), until_date=until
                )
                result["until"] = int(until.timestamp()) if until else 0
            elif action == "unmute":
                from aiogram.types import ChatPermissions

                await bot.restrict_chat_member(
                    chat_id, user_id, ChatPermissions(can_send_messages=True)
                )
            elif action == "ban":
                await bot.ban_chat_member(chat_id, user_id)
            elif action == "unban":
                await bot.unban_chat_member(chat_id, user_id)
            else:
                result["error"] = f"unknown action {action}"
                return result
            result["ok"] = True
        except TelegramAPIError as exc:
            result["error"] = str(exc)[:180]
        if delete_last:
            result["deleted"] = await self.purge_recent(chat_id, user_id, limit=delete_last)
        if reason:
            await safe_send(
                bot,
                chat_id,
                f"{user_id} · {action} · {truncate(reason, 120)}",
                disable_notification=True,
            )
        return result

    async def purge_recent(self, chat_id: int, user_id: int, *, limit: int = 25) -> int:
        """Delete a member's recent messages by tracked id (Bot API has no by-author purge).

        The bot remembers the last :data:`RECENT_MESSAGES` message ids per chat in a
        Redis list, so "remove their spam" is one pass of deletes instead of nothing.
        """
        if self.redis is None or limit <= 0:
            return 0
        pairs = await self.redis.hgetall(f"recent:{chat_id}")
        deleted = 0
        for message_id, author in list(pairs.items())[-200:]:
            if str(author) != str(user_id):
                continue
            try:
                await self.bot.delete_message(chat_id, int(message_id))
                deleted += 1
            except TelegramAPIError:
                pass
            await self.redis.hdel(f"recent:{chat_id}", message_id)
            if deleted >= limit:
                break
        return deleted

    async def remember_message(self, chat_id: int, message_id: int, user_id: int) -> None:
        if self.redis is None:
            return
        await self.redis.hset(f"recent:{chat_id}", {str(message_id): str(user_id)}, ttl=3600)
        size = len(await self.redis.hgetall(f"recent:{chat_id}"))
        if size > 400:  # keep the window bounded per chat
            await self.redis.delete("recent", chat_id)

    async def mark_unavailable(self, chat_id: int) -> None:
        """Called when the bot is kicked/blocked: unregister + drop queued spawns."""
        async with self.ctx.db.tx() as session:
            await spawn_repo.unregister_group(session, chat_id)
            await mod_repo.audit(
                session,
                actor_id=0,
                action="chat.unavailable",
                target=str(chat_id),
                detail="bot removed/blocked",
                chat_id=chat_id,
                scope="chat",
            )
        if self.redis is not None:
            await self.redis.queue_remove("autospawn", str(chat_id))
        await self.ctx.cache.invalidate("groups")

    # ------------------------------------------------------------------ bans
    async def global_ban(
        self,
        session: AsyncSession,
        user_id: int,
        *,
        moderator_id: int,
        reason: str = "",
        username: str = "",
    ) -> None:
        await user_repo.set_banned(session, user_id, banned=True, reason=reason, username=username)
        await mod_repo.audit(
            session,
            actor_id=moderator_id,
            action="ban.global",
            target=str(user_id),
            detail=reason[:200],
        )

    async def global_unban(self, session: AsyncSession, user_id: int, *, moderator_id: int) -> None:
        await user_repo.set_banned(session, user_id, banned=False)
        await mod_repo.audit(
            session, actor_id=moderator_id, action="unban.global", target=str(user_id)
        )

    async def ban_list(self, session: AsyncSession, *, limit: int = 50) -> list[Any]:
        return await mod_repo.ban_list(session, limit=limit)

    # ----------------------------------------------------------- group config
    async def group(
        self, session: AsyncSession, chat_id: int, *, title: str = "", create: bool = True
    ) -> Any:
        return await spawn_repo.group(session, chat_id, create=create, title=title)

    async def set_group_flags(
        self,
        session: AsyncSession,
        chat_id: int,
        *,
        disabled: list[str] | None = None,
        **values: Any,
    ) -> dict[str, Any]:
        """Per-group switches (spawn on/off, nguess, welcome, spam auto-ban).

        Unknown keys are rejected rather than ignored — silent typos in a group
        settings command are indistinguishable from "it didn't work".
        """
        from waifu.db.models import Group

        row = await session.get(Group, chat_id)
        if row is None:
            raise NotFound("this group is not registered — /savegroup first")
        allowed = {
            "spawn_enabled",
            "auto_ban_spam",
            "spam_limit",
            "welcome_enabled",
            "log_channel_id",
        }
        unknown = set(values) - allowed
        if unknown:
            raise WaifuError(f"unknown group setting(s): {', '.join(sorted(unknown))}")
        for key, value in values.items():
            setattr(row, key, value)
        if disabled is not None:
            data = dict(row.data or {})
            data["disabled"] = [str(x) for x in disabled]
            row.data = data
        await session.flush()
        await self.ctx.cache.invalidate("groups")
        return {
            "chat_id": chat_id,
            "settings": {k: getattr(row, k) for k in allowed & set(values)},
            "disabled": list((row.data or {}).get("disabled") or []),
        }

    # -------------------------------------------------------------- broadcasts
    async def broadcast(
        self,
        session: AsyncSession,
        text: str,
        *,
        access: Access,
        photo: str | None = None,
        only_premium: bool = False,
        only_group: int | None = None,
        dry_run: bool = False,
        limit: int = 0,
    ) -> BroadcastResult:
        """Fan out an announcement to registered groups (paced + counted).

        ``dry_run`` sends to the caller only — the difference between a rehearsed
        announcement and an incident in 2,000 chats.
        """
        if only_premium and not access.can("broadcast"):
            raise PermissionDenied("premium-only broadcast needs the broadcast permission")
        groups = await spawn_repo.spawnable_groups(session)
        targets = [g.chat_id for g in groups if not only_group or g.chat_id == only_group]
        if limit:
            targets = targets[:limit]
        if dry_run:
            await safe_send(
                self.bot, access.user_id, f"<b>Dry run</b> → {len(targets)} chats\n\n{text}"
            )
            return BroadcastResult(
                targets=len(targets),
                sent=1,
                blocked=0,
                failed=0,
                dry_run=True,
                sample=truncate(text, 200),
            )
        stats = await _broadcast(self.bot, targets, text, photo=photo)
        await mod_repo.audit(
            session,
            actor_id=access.user_id,
            action="broadcast",
            target=f"{len(targets)} chats",
            detail=truncate(json.dumps(stats, sort_keys=True), 3800),
        )
        return BroadcastResult(
            targets=len(targets),
            sent=int(stats.get("sent", 0)),
            blocked=int(stats.get("blocked", 0)),
            failed=int(stats.get("error", 0)) + int(stats.get("retry", 0)),
        )

    # ------------------------------------------------------- scheduled broadcasts
    async def schedule_broadcast(
        self,
        session: AsyncSession,
        *,
        run_at: datetime,
        text: str,
        actor_id: int,
    ) -> int:
        """Queue an announcement for a future moment (the ``/broadcast at …`` line)."""
        from waifu.db.repo import broadcasts as bc_repo

        row = await bc_repo.schedule(session, run_at=run_at, text=text, created_by=actor_id)
        await self.log_line(
            f"📣 broadcast #{row.id} scheduled by {actor_id} for {run_at:%Y-%m-%d %H:%M UTC}",
            silent=True,
        )
        return row.id

    async def pending_broadcasts(self, session: AsyncSession) -> list[Any]:
        from waifu.db.repo import broadcasts as bc_repo

        return await bc_repo.pending(session)

    async def cancel_broadcasts(self, session: AsyncSession) -> int:
        from waifu.db.repo import broadcasts as bc_repo

        return await bc_repo.cancel_all(session)

    async def fire_due_broadcasts(self, session: AsyncSession) -> dict[str, int]:
        """The ``broadcasts`` job pass: fan out every row whose moment has come.

        Rows are claimed atomically in the repository (``sent_at`` set, one send
        per row), so the resident loop and a cron ``waifu jobs`` cannot double-post.
        The owner's log channel gets the counts, same as an immediate broadcast.
        """
        from waifu.db.repo import broadcasts as bc_repo

        due_rows = await bc_repo.due(session)
        fired = failed = 0
        for row in due_rows:
            groups = await spawn_repo.spawnable_groups(session)
            stats = await _broadcast(self.bot, [g.chat_id for g in groups], row.text)
            sent = int(stats.get("sent", 0))
            if sent:
                fired += 1
            else:
                failed += 1
            await self.log_line(
                f"📣 scheduled broadcast #{row.id} (by {row.created_by}) → "
                f"{sent} sent · {int(stats.get('blocked', 0))} blocked · "
                f"{int(stats.get('error', 0))} failed"
            )
        return {"broadcasts_fired": fired, "broadcasts_failed": failed}

    async def audit(
        self,
        session: AsyncSession,
        *,
        limit: int = 30,
        actor_id: int | None = None,
        chat_id: int | None = None,
        contains: str = "",
    ) -> list[Any]:
        return await mod_repo.audit_rows(
            session, limit=limit, actor_id=actor_id, chat_id=chat_id, contains=contains
        )

    async def summary(self, session: AsyncSession, chat_id: int) -> dict[str, int]:
        return await mod_repo.moderation_summary(session, chat_id)

    async def log(
        self,
        session: AsyncSession,
        *,
        actor_id: int,
        action: str,
        target: str = "",
        detail: str = "",
        chat_id: int | None = None,
        scope: str = "global",
    ) -> None:
        """The single entry point handlers use to record anything privileged."""
        await mod_repo.audit(
            session,
            actor_id=actor_id,
            action=action,
            target=target,
            detail=detail[:4000],
            chat_id=chat_id,
            scope=scope,
        )

    async def owner_snapshot(self, session: AsyncSession) -> dict[str, Any]:
        """The numbers /owner and the web dashboard show, from one round-trip."""
        return {
            "users": await user_repo.counts(session),
            "groups": await spawn_repo.groups_summary(session),
            "auctions": await self.ctx.auctions.stats(session) if self.ctx.auctions else {},
            "codes": await trade_repo.code_stats(session),
            "revenue": await monetize_repo.revenue(session),
            "activity": await stats_repo.global_activity(session, hours=24),
            "database": await self.ctx.db.healthcheck(),
            "redis": await self.ctx.redis.healthcheck()
            if self.ctx.redis
            else {"redis": "disabled"},
        }


def format_case(case: ModerationCase) -> str:
    """One-line case rendering (used by /cases and the audit page)."""
    flags = "🟢" if case.is_active else "⚪"
    duration = f" · {case.duration_seconds // 60}m" if case.duration_seconds else ""
    return f"{flags} #{case.id} {case.action}{duration} → {case.target_user_id} by {case.moderator_id}: {truncate(case.reason or '—', 80)}"
