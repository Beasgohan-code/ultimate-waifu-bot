"""Domain exceptions — mapped to user-facing messages by the error handler."""

from __future__ import annotations


class WaifuError(Exception):
    """Base class. ``user_message`` is safe to send to a player."""

    retry_after: int | None = None
    user_message: str = "Something went wrong on our side. Please try again."

    def __init__(self, message: str | None = None, *, retry_after: int | None = None) -> None:
        self.detail = message or ""
        if message:
            self.user_message = message
        if retry_after is not None:
            self.retry_after = retry_after
        super().__init__(message or self.user_message)


class NotEnoughFunds(WaifuError):
    user_message = "You don't have enough coins for that."

    def __init__(self, needed: int = 0, have: int = 0) -> None:
        self.needed, self.have = needed, have
        super().__init__(f"Needs {needed:,} coins, you have {have:,}.")


class NotEnoughShards(WaifuError):
    user_message = "Not enough shards."


class CooldownActive(WaifuError):
    user_message = "Calm down — you just did that."

    def __init__(self, retry_after: int) -> None:
        super().__init__(f"Try again in {retry_after}s.", retry_after=max(1, int(retry_after)))


class AlreadyClaimed(WaifuError):
    user_message = "Already claimed."


class MultipleMatches(WaifuError):
    """A name matched more than one player — ask for an exact handle, do not guess."""

    def __init__(self, query: str = "", count: int = 0) -> None:
        self.query, self.count = query, count
        hint = f"{count} players match “{query}”" if count else f"“{query}” is ambiguous"
        super().__init__(f"{hint}. Use @username or the numeric id instead.")


class NotFound(WaifuError):
    user_message = "I couldn't find that."


class PermissionDenied(WaifuError):
    user_message = "You don't have permission to do that."


class NotInChat(WaifuError):
    user_message = "The bot needs to be a member of this chat to run that here."


class BidTooLow(WaifuError):
    """Raised by the auction layer when a bid misses the minimum increment."""

    def __init__(self, minimum: int = 0) -> None:
        self.minimum = int(minimum)
        super().__init__(f"the minimum bid is {minimum:,} coins".replace(",", "\u2009"))


class Locked(WaifuError):
    """Raised when an optimistic concurrency guard fails (someone beat you to it)."""

    user_message = "Someone got there first — the offer is gone."


class RateLimited(WaifuError):
    user_message = "Too many requests. Pausing for a moment."

    def __init__(self, retry_after: int = 5) -> None:
        super().__init__(retry_after=int(retry_after))


class PaywallRequired(WaifuError):
    user_message = "This is a supporter feature."


class AISetupError(WaifuError):
    def __init__(self, message: str) -> None:
        super().__init__(f"AI backend problem: {message}")


class ConfigError(RuntimeError):
    """Startup validation failure — the bot refuses to boot with a bad config."""
