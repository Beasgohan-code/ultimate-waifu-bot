"""Checklists as an interactive menu surface (Bot API 9.3+).

A checklist is a first-class message type: tasks a user ticks off, optionally
editable by others. It is a *much* better daily-quest UI than nine inline buttons
— progress is visible, the ticks persist, and Telegram renders it natively in both
DMs and groups — so this is what /daily and /quests use, falling back to a plain
message with buttons when the endpoint predates it.

``ReplyParameters.checklist_task_id`` additionally lets the bot *reply to a single
task*, which is how /daily explains one objective without reprinting the list.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.methods import SendChecklist
from aiogram.types import Checklist, InputChecklist, InputChecklistTask, Message

from waifu.logging import get_logger
from waifu.utils.text import truncate

log = get_logger("tg.checklist")


@dataclass(slots=True)
class Task:
    id: int
    text: str
    done: bool = False

    def to_aiogram(self) -> InputChecklistTask:
        return InputChecklistTask(id=self.id, text=self.text[:4096])


@dataclass(slots=True)
class ChecklistCard:
    title: str
    tasks: list[Task] = field(default_factory=list)
    others_can_add: bool = False
    others_can_complete: bool = True

    def add(self, text: str, *, task_id: int | None = None, done: bool = False) -> ChecklistCard:
        self.tasks.append(
            Task(id=task_id if task_id is not None else len(self.tasks) + 1, text=text, done=done)
        )
        return self

    @property
    def completed(self) -> int:
        return sum(1 for t in self.tasks if t.done)

    @property
    def progress(self) -> str:
        return f"{self.completed}/{len(self.tasks)}"

    def to_input(self) -> InputChecklist:
        return InputChecklist(
            title=truncate(self.title, 255),
            tasks=[t.to_aiogram() for t in self.tasks],
            others_can_add_tasks=self.others_can_add or None,
            others_can_mark_tasks_as_done=self.others_can_complete or None,
            parse_mode="HTML",
        )

    def fallback_html(self) -> str:
        lines = [f"<b>{self.title}</b>  <i>{self.progress}</i>"]
        lines += [f"{'✅' if t.done else '⬜️'} {t.text}" for t in self.tasks]
        return "\n".join(lines)


def checklist_from_message(message: Message) -> Checklist | None:
    return getattr(message, "checklist", None)


def ticked_task_ids(checklist: Checklist | None) -> set[int]:
    """Task ids someone has completed (used by the quest reward handler)."""
    if checklist is None:
        return set()
    return {
        task.id
        for task in checklist.tasks
        if getattr(task, "completed_by_user", None) is not None
        or getattr(task, "completed_by_chat", None) is not None
    }


async def send_checklist(
    bot: Bot,
    chat_id: int,
    card: ChecklistCard,
    *,
    thread_id: int | None = None,
    reply_to: int | None = None,
    disable_notification: bool = False,
) -> Message | None:
    """Send the checklist, or return ``None`` when unsupported (caller falls back)."""
    try:
        return await bot(
            SendChecklist(
                chat_id=chat_id,
                checklist=card.to_input(),
                message_thread_id=thread_id,
                reply_to_message_id=reply_to,
                disable_notification=disable_notification or None,
            )
        )
    except TelegramAPIError as exc:
        text = str(exc).lower()
        if "not found" in text or ("required parameter" not in text and "checklist" not in text):
            log.info("checklists unavailable here (%s); falling back to a text card", exc)
            return None
        raise


async def send_checklist_or_text(
    bot: Bot,
    chat_id: int,
    card: ChecklistCard,
    *,
    thread_id: int | None = None,
    buttons: Sequence[Sequence[object]] | None = None,
) -> tuple[Message | None, str]:
    """Send a checklist; degrade to the same content as text with buttons.

    Returns ``(message, mode)`` where mode is ``"checklist"`` or ``"text"`` so the
    handler can log which surface the user actually got.
    """
    from aiogram.types import InlineKeyboardMarkup

    sent = await send_checklist(bot, chat_id, card, thread_id=thread_id)
    if sent is not None:
        return sent, "checklist"
    markup = InlineKeyboardMarkup(inline_keyboard=[list(r) for r in buttons]) if buttons else None  # type: ignore[arg-type]
    message = await bot.send_message(
        chat_id, card.fallback_html(), reply_markup=markup, parse_mode="HTML"
    )
    return message, "text"
