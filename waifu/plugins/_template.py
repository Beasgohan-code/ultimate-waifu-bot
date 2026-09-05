"""Copy this file to start a new plugin — and read the rules before you delete the comments.

A plugin is a module in :mod:`waifu.plugins` that exposes ``router`` and is listed in
:data:`waifu.core.dp.PLUGIN_ROUTERS`. That is the whole contract; everything below is how
a plugin stays maintainable once there are twenty of them.

1. **One session per handler.** The ``session`` kwarg comes from
   :class:`~waifu.core.middlewares.SessionMiddleware`, which opens it, hands it over and
   commits (or rolls back) when the handler returns. Handlers never call ``commit`` and
   services never open a session, so a bug can only live in one place.
2. **Reach features through ``ctx``** (``ctx.economy``, ``ctx.collection`` …). Never
   import another plugin, and never instantiate a service: ``AppContext`` is built once at
   startup, and cross-plugin imports are how import cycles and "works until the scheduler
   runs it" bugs appear.
3. **Name your callback namespace** (``mything:verb:arg``) and keep every ``callback_data``
   under 64 bytes. Put view state in the data, not in FSM: two players tapping the same
   menu must not share a cursor.
4. **Raise for expected failures** (:class:`waifu.errors.WaifuError` and friends) and let
   :func:`waifu.plugins._kit.refuse` format them; unexpected exceptions belong to
   ``on_error``, which logs with the chat/user and replies with one generic line.
5. **New Bot API features degrade, never fail.** Ask ``ctx.caps.allow("…")`` (or
   ``ctx.wants("…")`` for the per-player preference) and keep the plain-text path working
   — the same code has to run against a self-hosted server from last year.
6. **Register the commands** in ``HELP_TOPICS`` (misc) or accept them landing on the
   generated ``/help more`` page, and run ``make docs`` so ``docs/COMMANDS.md`` stays true.
7. Tests go in ``tests/`` using the fixtures in ``conftest.py`` (``ctx``, ``tx``,
   ``player``, ``partner``, ``any_character``) — service-level, no mocked Telegram.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from waifu.errors import NotFound, WaifuError
from waifu.plugins._kit import note, refuse, text

if TYPE_CHECKING:  # pragma: no cover
    from waifu.core.access import Access
    from waifu.core.context import AppContext

router = Router(name="template")  # this module is not in PLUGIN_ROUTERS; a copy is


@router.message(Command("example"))
async def example(message: Message, ctx: AppContext, session: Any, access: Access) -> None:
    """The only shape a command handler needs: read, call a service, render, done."""
    try:
        value = await ctx.stats.summary(session)
    except (NotFound, WaifuError) as exc:
        await refuse(message, exc.user_message)
        return
    await text(message, ctx, f"{value}")


@router.callback_query(F.data.startswith("tpl:"))
async def tpl_button(callback_query: CallbackQuery, ctx: AppContext) -> None:
    await note(callback_query, "handled by the edit-in-place pattern", alert=True)
