"""Wiring tests: the registry, the command surface, and the timer loop.

These are the parts of a bot this size that break *silently*:

* a plugin that fails to import is only a warning in the log (by design — a broken
  optional module must not stop the bot), which makes "half the commands are gone" a
  silent production failure. Here it is a test failure instead;
* two routers claiming the same command name makes the second one dead code, because
  aiogram stops at the first match;
* every timed behaviour (spawn feed, auction settlement, expired trades, raffle draws)
  lives in :mod:`waifu.core.jobs`, and a typo there shows up as "my group's spawns just
  stopped one day" — so the loop is actually executed here against the test database.

The dispatcher is built once per module and reused: :func:`build_dispatcher` attaches the
*module-level* router objects, and aiogram refuses to attach a router twice — which is
correct in production (one dispatcher per process) and only needs respecting in tests.
"""

from __future__ import annotations

from aiogram import Router

from waifu.core.dp import AUX_ROUTERS, PLUGIN_ROUTERS, RegistrationReport, build_dispatcher
from waifu.plugins.misc import HELP_ORDER, HELP_TOPICS

_BUILT: tuple[Router, RegistrationReport] | None = None


def _handler_commands(handler) -> list[str]:
    """Command names behind a handler's filters (aiogram keeps them on the callback)."""
    names: list[str] = []
    for filter_obj in handler.filters:
        for target in (filter_obj, getattr(filter_obj, "callback", None)):
            for command in getattr(target, "commands", None) or []:
                text = str(getattr(command, "command", command)).lstrip("/").lower()
                if text:
                    names.append(text)
    return names


async def _dispatcher(ctx) -> tuple[Router, RegistrationReport]:
    global _BUILT
    if _BUILT is None:
        _BUILT = build_dispatcher(ctx.settings, ctx)
    return _BUILT


def _routers(dp: Router) -> list[Router]:
    return list(dp.sub_routers)


def _walk(dp: Router):
    """Every router in the tree, depth-first (aux routers are children of misc)."""
    for router in dp.sub_routers:
        yield router
        yield from _walk(router)


def _commands(dp: Router) -> dict[str, str]:
    """Each ``/command`` the wired routers listen for → owning router name."""
    found: dict[str, str] = {}

    def visit(router: Router) -> None:
        for handler in router.message.handlers:
            for command in _handler_commands(handler):
                found.setdefault(command, str(getattr(router, "name", "?")))
        for child in router.sub_routers:
            visit(child)

    visit(dp)
    return found


async def test_every_plugin_router_registers(ctx) -> None:
    dp, report = await _dispatcher(ctx)
    assert not report.skipped, f"plugins failed to load: {report.skipped}"
    assert len(report.loaded) == len(PLUGIN_ROUTERS), report.summary()
    routers = _routers(dp)
    assert routers, "dispatcher has no routers"
    assert len({router.name for router in routers}) == len(routers), "two routers share a name"
    for router in routers:
        assert isinstance(router, Router)
        # ``observers`` is the complete set (message, callback_query, message_reaction,
        # poll_answer, pre_checkout_query, …) so a router that only listens for
        # reactions still counts as wired.
        observed = sum(len(observer.handlers) for observer in router.observers.values())
        assert observed > 0, f"{router.name} registers nothing"


async def test_aux_routers_are_mounted(ctx) -> None:
    _dp, report = await _dispatcher(ctx)
    assert not report.skipped
    # Every aux router lives in a plugin module already counted above.
    assert set(AUX_ROUTERS.values()) <= set(PLUGIN_ROUTERS)


async def test_no_two_routers_claim_the_same_command(ctx) -> None:
    dp, _report = await _dispatcher(ctx)
    from waifu.core.dp import command_names

    owners: dict[str, list[str]] = {}
    for router in _walk(dp):
        for handler in router.message.handlers:
            for name in command_names(handler):
                owners.setdefault(name, []).append(str(getattr(router, "name", "?")))
    clashes = {
        name: sorted(set(routers)) for name, routers in owners.items() if len(set(routers)) > 1
    }
    assert not clashes, f"command claimed by two routers (the second is unreachable): {clashes}"
    # Summon-bot ships ~40 player commands and ~15 admin ones; far below that means a
    # module quietly stopped registering.
    assert len(owners) >= 60, f"only {len(owners)} commands wired"


async def test_help_pages_reach_every_wired_command(ctx) -> None:
    """Curated ``/help`` topics, a generated ``more`` page, and ``/commands`` as superset.

    Three discovery surfaces have to agree with the code, or one of them becomes a lie:
    the ⊞ menu cannot hold more than 100 entries, so the residual must be reachable
    through help/commands, and nothing may advertise a command that stopped existing.
    """
    from waifu.core.dp import admin_command_menu, command_menu, primary_commands, public_commands

    primaries = set(primary_commands())
    assert len(primaries) >= 100, f"only {len(primaries)} primary commands wired"
    assert len(public_commands()) > len(primaries), "aliases are expected (Summon parity names)"
    documented: set[str] = set()
    for topic in HELP_ORDER:
        for command, _description in HELP_TOPICS.get(topic, ()):
            documented |= {part.lstrip("/").lower() for part in command.split()[0].split(",")}
    assert "more" in HELP_ORDER, "the generated /help page must be reachable"
    residual = sorted(primaries - documented)
    # ``/commands`` renders exactly this residual, so nothing is undiscoverable by
    # construction; the bound keeps that list from quietly becoming a novel.
    assert len(residual) <= 100, (
        f"{len(residual)} commands are missing from the curated pages: {residual[:8]}"
    )
    player_menu = {name for name, _ in command_menu()}
    admin_menu = {name for name, _ in admin_command_menu()}
    assert player_menu | admin_menu == primaries, "the two scopes together must reach every command"
    assert len(player_menu) <= 100 and len(admin_menu) <= 100, (
        "Telegram caps a scope at 100 entries"
    )
    ghost = sorted(documented - set(public_commands()) - {"more"})
    assert not ghost, f"/help advertises commands nothing handles: {ghost}"


async def test_job_passes_run_clean(ctx) -> None:
    from waifu.core.jobs import PASSES, run_pass

    results = await run_pass(ctx, "all", limit=2)
    assert len(results) == len(PASSES)
    failed = {result.name: result.error for result in results if result.error}
    assert not failed, f"job pass raised: {failed}"
    # A pass with no counters at all usually means it returned before doing any work.
    assert all(result.counters for result in results), [
        result.name for result in results if not result.counters
    ]


async def test_doctor_reports_a_healthy_database(ctx) -> None:
    health = await ctx.db.healthcheck()
    assert health["db"] == "ok"
    assert "users" in health
    from waifu.db.repositories import characters as char_repo

    async with ctx.db.tx() as session:
        totals = await char_repo.totals(session)
    assert totals["characters"] > 100, "the shipped catalogue must be present, not a stub roster"
    assert totals["active"] > 100
    assert totals["series"] > 20
    assert totals["copies"] == 0, "a fresh database has no copies yet"
    # ``table_sizes`` is a Postgres-only view (``pg_total_relation_size``); on the SQLite
    # test database it is documented to come back empty.
    sizes = await ctx.db.table_sizes()
    assert isinstance(sizes, list)
    if not ctx.db.is_sqlite:
        assert sizes, "postgres must report table sizes for /ping"
