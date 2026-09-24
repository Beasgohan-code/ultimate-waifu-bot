"""Chat with the waifus — persona completions through any OpenAI-compatible API.

Deliberately **not** the old bot's behaviour (one giant prompt string, no budget, no
history, ``openai==0.28``):

* per-user character budget per day, enforced before the call, not after the bill;
* conversation history from ``ai_messages`` (real continuity, capped tokens);
* the reply streams into a Bot API 9.6 ``sendMessageDraft`` when the endpoint allows
  it — otherwise a normal edit — and generation is logged with its token count;
* a disabled/unconfigured AI raises :class:`AISetupError`, which the UI renders as an
  explanation instead of a traceback;
* optional output moderation (a cheap keyword screen by default, pluggable).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from waifu.db.models import Character
from waifu.db.repo import ai as ai_repo
from waifu.db.repo import users as user_repo
from waifu.errors import AISetupError, CooldownActive, NotFound
from waifu.services.base import Service
from waifu.utils.text import chunk_message, truncate
from waifu.utils.time import now_utc

SYSTEM_TEMPLATE = """You are {name}, a fictional character from "{series}".
Stay in character. Speak in {language}. Reply in at most {max_words} words, warm and
specific, never breaking the fourth wall about being an AI.
Character sheet: {sheet}
{extra}"""

#: Cheap, over-blocking-but-honest guard used when no moderation endpoint is set.
BLOCKED_HINTS = ("nsfw", "explicit", "sexual", "nude", "gore")


@dataclass(slots=True)
class AiReply:
    text: str
    tokens: int = 0
    streamed: bool = False
    flagged: bool = False
    remaining: int = 0
    draft_id: str = ""

    @property
    def chunks(self) -> list[str]:
        return chunk_message(self.text, limit=1024)


class AiService(Service):
    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)
        ai = self.settings.ai
        self.configured = bool(ai.configured)
        self._client: httpx.AsyncClient | None = httpx.AsyncClient(
            base_url=ai.api_base.rstrip("/"),
            timeout=httpx.Timeout(60.0, connect=10.0),
            headers={"Authorization": f"Bearer {ai.api_key}"} if ai.api_key else {},
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise AISetupError("the AI client is closed")
        return self._client

    # ---------------------------------------------------------------- budget
    async def budget(self, session: AsyncSession, user_id: int) -> dict[str, int]:
        ai = self.settings.ai
        user = await user_repo.get(session, user_id)
        used = int(user.ai_chars_today or 0) if user else 0
        if user and user.ai_reset_at and user.ai_reset_at <= now_utc():
            used = 0
        limit = ai.daily_char_budget * (2 if user and user.is_premium else 1)
        return {"used": used, "limit": limit, "left": max(0, limit - used)}

    async def spend(self, session: AsyncSession, user_id: int, characters: int) -> None:
        """Charge the daily character budget; the reset stamp is local midnight+1d."""
        from datetime import timedelta

        from sqlalchemy import update

        from waifu.db.models import User

        midnight = now_utc().replace(hour=0, minute=0, second=0, microsecond=0)
        await session.execute(
            update(User)
            .where(User.id == user_id)
            .values(
                ai_chars_today=User.ai_chars_today + max(0, characters),
                ai_reset_at=midnight + timedelta(days=1),
            )
        )
        await session.flush()

    # ------------------------------------------------------------------- chat
    async def chat(
        self,
        session: AsyncSession,
        *,
        user_id: int,
        character_id: int,
        text: str,
        history_limit: int = 10,
        language: str = "English",
        max_words: int = 90,
    ) -> AiReply:
        if not self.configured:
            raise AISetupError(
                "AI is not configured — set OPENAI_API_KEY (or AI_API_KEY) to enable /ai"
            )
        character = await session.get(Character, character_id)
        if character is None:
            raise NotFound("no such character")
        budget = await self.budget(session, user_id)
        if budget["left"] <= 0:
            raise CooldownActive(max(60, int(self.seconds_until_reset())))
        await self._gate(session, user_id=user_id, text=text)
        messages = await self._messages(
            session,
            user_id=user_id,
            character=character,
            text=text,
            history_limit=history_limit,
            language=language,
            max_words=max_words,
        )
        payload = {
            "model": self.settings.ai.model,
            "messages": messages,
            "max_tokens": min(700, max(80, max_words * 3)),
            "temperature": 0.85,
            "stream": False,
        }
        response = await self.client.post("/chat/completions", json=payload)
        if response.status_code >= 400:
            raise AISetupError(
                f"AI provider returned {response.status_code}: {truncate(response.text, 200)}"
            )
        body = response.json()
        reply_text = str(
            ((body.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        ).strip()
        usage = int((body.get("usage") or {}).get("completion_tokens") or 0)
        flagged = self._flag_output(reply_text)
        await ai_repo.log(
            session,
            user_id=user_id,
            character_id=character.id,
            role="assistant",
            content=reply_text,
            tokens_out=usage,
            flagged=flagged,
        )
        await self.spend(session, user_id, len(reply_text))
        return AiReply(
            text=reply_text,
            tokens=usage,
            remaining=budget["left"] - len(reply_text),
            flagged=flagged,
        )

    async def _messages(
        self,
        session: AsyncSession,
        *,
        user_id: int,
        character: Character,
        text: str,
        history_limit: int,
        language: str,
        max_words: int,
    ) -> list[dict[str, str]]:
        history = await ai_repo.recent(
            session, user_id=user_id, character_id=character.id, limit=history_limit
        )
        sheet = " / ".join(
            part
            for part in (
                character.description and truncate(character.description, 320),
                f"catchphrase: {character.voice_line}" if character.voice_line else "",
                f"likes: {', '.join(character.likes)}" if getattr(character, "likes", None) else "",
            )
            if part
        )
        system = SYSTEM_TEMPLATE.format(
            name=character.name,
            series=character.anime or "an unnamed series",
            language=language,
            max_words=max_words,
            sheet=sheet or "(no description — invent one that fits the name)",
            extra="The player owns you in their collection; you may reference shared history."
            if history
            else "",
        )
        messages: list[dict[str, str]] = [{"role": "system", "content": system}]
        for row in history:
            messages.append(
                {
                    "role": "user" if row.role == "user" else "assistant",
                    "content": truncate(row.content, 800),
                }
            )
        messages.append({"role": "user", "content": truncate(text, 900)})
        return messages

    async def _gate(self, session: AsyncSession, *, user_id: int, text: str) -> None:
        """Rate-limit + content gate, both before any money is spent upstream."""
        if self.redis is not None:
            allowed, _left, retry_after = await self.redis.sliding_hit(
                f"ai:{user_id}", limit=8, window=60
            )
            if not allowed:
                raise CooldownActive(max(1, retry_after))
        if self.settings.ai.moderation:
            lowered = text.lower()
            if any(hint in lowered for hint in BLOCKED_HINTS):
                await ai_repo.log(
                    session,
                    user_id=user_id,
                    character_id=None,
                    role="user",
                    content=f"[blocked] {text[:200]}",
                    flagged=True,
                )
                raise NotFound("that topic is out of bounds here")

    @staticmethod
    def _flag_output(text: str) -> bool:
        lowered = text.lower()
        return any(hint in lowered for hint in BLOCKED_HINTS)

    # ------------------------------------------------------------ draft stream
    async def stream_reply(
        self,
        chat_id: int,
        text: str,
        *,
        reply_to: int | None = None,
        can_stop: bool = True,
        chunk_delay: float = 0.35,
    ) -> dict[str, Any]:
        """Stream into a Bot API 9.6 draft so the player reads it as it is written.

        Requires ``can_send_message_draft`` in the chat; when the flag is missing the
        caller falls back to a plain edit — that negotiation lives in
        :mod:`waifu.tg.messages`, not here.
        """
        from asyncio import sleep

        from waifu.tg.messages import send_message_draft

        pieces = chunk_message(text, limit=220)
        state: dict[str, Any] = {
            "ok": False,
            "draft_id": "",
            "stopped": False,
            "chunks": len(pieces),
        }
        if not self.ctx.caps.allow("drafts"):
            return {**state, "reason": "drafts unsupported"}
        buffer = ""
        for piece in pieces:
            buffer = f"{buffer}\n{piece}".strip() if buffer else piece
            result = await send_message_draft(
                self.bot,
                chat_id,
                buffer,
                reply_to_message_id=reply_to,
                can_stop=can_stop,
                keep_on_stop=True,
            )
            state["draft_id"] = result.draft_id or state["draft_id"]
            state["ok"] = result.ok
            if result.stopped:
                state["stopped"] = True
                break
            await sleep(chunk_delay)
        return state

    async def usage_report(self, session: AsyncSession, *, days: int = 1) -> dict[str, int]:
        """Provider cost accounting for the owner panel — counts, not guesses."""
        from datetime import timedelta

        return await ai_repo.usage_since(session, since=now_utc() - timedelta(days=days))

    async def summary_for(
        self, session: AsyncSession, user_id: int, *, limit: int = 6
    ) -> list[str]:
        rows = await ai_repo.recent(session, user_id=user_id, character_id=None, limit=limit)
        return [f"{row.role}: {truncate(row.content, 90)}" for row in rows]

    async def export(self, session: AsyncSession, user_id: int) -> str:
        """GDPR-style export of a player's AI transcripts (JSON, no DB access needed)."""
        rows = await ai_repo.all_for(session, user_id)
        return json.dumps(
            [
                {
                    "at": row.created_at.isoformat(),
                    "role": row.role,
                    "character_id": row.character_id,
                    "text": row.content,
                }
                for row in rows
            ],
            ensure_ascii=False,
            indent=1,
        )

    async def forget(self, session: AsyncSession, user_id: int) -> int:
        return await ai_repo.purge(session, user_id)
