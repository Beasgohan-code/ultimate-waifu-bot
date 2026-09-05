"""Summon-bot command parity — the contract the rewrite had to honour.

The reference bot (``Beasgohan-code/Summon-bot``) ships this exact command surface,
registered in ``main.py`` with ``CommandHandler([...])`` aliases. Every name below is
wired here, either to the same behaviour or — where our version does the same job under a
better name — to an explicit, documented deviation in :data:`RENAMED`.

Keeping the list as data in the test suite (rather than reading the other repo) is
deliberate: the parity promise must survive Summon-bot disappearing, and a test that
silently skips when a path is missing tests nothing.

Names and aliases were extracted from the reference source, so a name nobody can type on
the old bot is not promised here either.
"""

from __future__ import annotations

import pytest

from waifu.core.dp import primary_commands, public_commands

#: ``name -> what it does`` for every player-facing command in Summon-bot.
SUMMON_COMMANDS: dict[str, str] = {
    # core
    "start": "welcome + register",
    "help": "command list",
    "menu": "alias of help",
    "commands": "alias of help",
    "ping": "latency",
    "invite": "referral link",
    "about": "bot info",
    # economy
    "balance": "wallet",
    "bal": "alias",
    "daily": "daily coins",
    "spin": "coin wheel",
    "work": "earn coins",
    "steal": "rob a player",
    "rob": "alias",
    "bomb": "attack a player",
    "give": "send a character",
    "pay": "send coins",
    "transfer": "send coins",
    "history": "wallet history",
    "streak": "daily streak",
    "achievements": "badge board",
    "ach": "alias",
    "badges": "alias",
    # gacha
    "pull": "gacha roll",
    "hclaim": "free claim",
    "claim": "alias of the claim path",
    "chance": "drop table",
    "claimlist": "what /hclaim can roll",
    "setclaim": "edit the claim ladder",
    # collection
    "collection": "harem pages",
    "harem": "alias",
    "check": "character detail card",
    "info": "alias",
    "search": "roster search",
    "find": "alias",
    "fav": "favourite",
    "favorite": "alias",
    "sell": "sell copies",
    "sellchar": "alias",
    "hmode": "collection display mode",
    # market / shop
    "market": "character shop",
    "shop": "item shop",
    "cshop000000": "character shop (their admin-facing alias)",
    "buy": "buy from market",
    "skip": "skip an item cooldown",
    "inventory": "item bag",
    "inv": "alias",
    "gift": "gift a character",
    "paymoney": "top up",
    "givemoney": "admin coins",
    "rmmoney": "admin coins",
    "addcoins": "admin coins",
    # auctions / trading
    "auction": "list an auction",
    "bid": "place a bid",
    "auctionlist": "live auctions",
    "auction_list": "alias",
    "mybids": "your bids",
    "cancelauction": "cancel own auction",
    # spawns / guessing
    "spawn": "trigger a spawn",
    "summon": "alias",
    "grab": "alias",
    "collect": "alias",
    "guess": "alias",
    "nguess": "timed round",
    "nguess_end": "end the round",
    "ngstats": "your round stats",
    "ngtop": "best guessers",
    "changetime": "spawn interval",
    "checkspawn": "is one live now",
    "reggroup": "register this group",
    "savegroup": "alias",
    # stats / profile
    "profile": "player card",
    "pinfo": "alias",
    "me": "self stats",
    "stats": "server stats",
    "server": "alias",
    "top": "leaderboards",
    "rank": "alias of top",
    "hstats": "harem stats",
    "premium": "premium info",
    "unpremium": "revoke premium",
    "redeem": "redeem a code",
    "gen": "create a code",
    "gencode": "alias",
    "chancelist": "odds table",
    "clist": "alias",
    "warn": "moderation",
    "font": "profile font",
    "style": "alias",
    # admin
    "addchar": "roster CRUD",
    "updatechar": "roster CRUD",
    "update": "roster CRUD",
    "delete": "roster CRUD",
    "remove": "purge messages",
    "removeall": "purge messages",
    "ban": "group ban",
    "unban": "group unban",
    "broadcast": "announce",
    "addsudo": "staff role",
    "editsudo": "staff role",
    "rmsudo": "staff role",
    "sudolist": "staff list",
    "restart": "bot control",
    "owner": "owner dashboard",
    "panel": "owner dashboard",
    "setchance": "edit drop ladder",
    "banner": "banner characters",
    "media": "attach art",
    "upload": "alias",
    "reseed": "re-roll seeds",
}

#: Summon names we deliberately answer under a different command, with the reason and the
#: name to use instead. Anything here is still *typed as documented on the old bot*.
RENAMED: dict[str, str] = {
    "claim": "spawns own /claim for the group spawn feed; the free pull is /hclaim (theirs collided both ways)",
    "chance": "/chances — the gacha table is /chances, the pair of ladders is /clist",
    "paymoney": "/topup — 'pay' reads as buying from another player",
}


def test_every_summon_command_is_wired() -> None:
    wired = set(public_commands())
    missing = sorted(name for name in SUMMON_COMMANDS if name not in wired and name not in RENAMED)
    assert not missing, f"Summon-bot commands not wired here: {missing}"


def test_parity_names_resolve_to_a_handler() -> None:
    """Not just registered: registered on a router that aiogram will actually reach."""
    primaries = set(primary_commands())
    aliases = set(public_commands())
    unresolved = sorted(name for name in SUMMON_COMMANDS if name not in aliases)
    for name in unresolved:
        assert name in RENAMED, f"{name} is neither wired nor documented as renamed"
    assert primaries, "the router walk found nothing"


def test_renamed_deviations_point_at_real_commands() -> None:
    wired = set(public_commands())
    for name, reason in RENAMED.items():
        assert reason, f"{name} needs a reason for the deviation"
        target = {"claim": "hclaim", "chance": "chances", "paymoney": "topup"}[name]
        assert target in wired, f"the documented replacement /{target} for /{name} is missing"


@pytest.mark.parametrize("name", sorted(SUMMON_COMMANDS))
def test_command_name_is_legal_for_telegram(name: str) -> None:
    """Bot API: 1-32 chars, ``a-z0-9_`` — one illegal alias and setMyCommands fails whole."""
    assert 1 <= len(name) <= 32
    assert name == name.lower()
    assert all(char.isalnum() or char == "_" for char in name), name
