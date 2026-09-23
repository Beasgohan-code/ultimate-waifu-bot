"""Randomness.

Two different needs, deliberately separated:

* ``system_rand`` — real RNG for anything with value (Stars grants, dice).
* ``derive_roll`` — a *deterministic* function of (server seed, player, sequence).
  This is what makes gacha provably fair: the seed commitment is published before
  the pull, the seed itself is revealed with the result, and anyone can recompute
  the outcome with ``waifu verify``.
"""

from __future__ import annotations

import hashlib
import hmac
import random
import secrets
import struct
from dataclasses import dataclass

_ALPHA = "abcdefghijkmnopqrstuvwxyz23456789"


def new_seed(nbytes: int = 16) -> str:
    return secrets.token_hex(nbytes)


def commit(seed: str) -> str:
    """The public promise shown to the player *before* the roll happens."""
    return hashlib.sha256(seed.encode()).hexdigest()


def redeem_code(prefix: str = "WTF", length: int = 10) -> str:
    body = "".join(secrets.choice(_ALPHA) for _ in range(length))
    return f"{prefix}-{body}".upper()


def weighted_choice(weights: list[int], draw: float) -> int:
    """Map a float in [0,1) onto an index using normalised cumulative weights.

    ``draw`` comes from the deterministic stream, never from ``random``.
    """
    if not weights:
        raise ValueError("empty weights")
    total = float(sum(weights))
    if total <= 0:
        raise ValueError("weights must sum to a positive number")
    target = draw * total
    acc = 0.0
    for index, weight in enumerate(weights):
        acc += weight
        if target < acc:
            return index
    return len(weights) - 1


def uniform_from(*, seed: str, player_id: int, sequence: int, salt: str = "") -> float:
    digest = hmac.new(
        seed.encode(), f"{player_id}:{sequence}:{salt}".encode(), hashlib.sha256
    ).digest()
    (as_int,) = struct.unpack(">Q", digest[:8])
    return (as_int % (2**53)) / float(2**53)


@dataclass(frozen=True, slots=True)
class FairRoll:
    """A single reproducible roll, and everything needed to verify it."""

    seed: str
    player_id: int
    sequence: int
    salt: str
    value: float

    @property
    def reveal(self) -> str:
        return f"{self.seed}|{self.player_id}|{self.sequence}|{self.salt}"

    @property
    def commitment(self) -> str:
        return commit(self.seed)

    def verify(self) -> bool:
        return (
            abs(
                self.value
                - uniform_from(
                    seed=self.seed, player_id=self.player_id, sequence=self.sequence, salt=self.salt
                )
            )
            < 1e-12
        )


def make_roll(seed: str, player_id: int, sequence: int, salt: str = "") -> FairRoll:
    return FairRoll(
        seed=seed,
        player_id=player_id,
        sequence=sequence,
        salt=salt,
        value=uniform_from(seed=seed, player_id=player_id, sequence=sequence, salt=salt),
    )


def system_rand() -> secrets.SystemRandom:
    """Use for: who wins a raffle from reactions, daily jackpot dice, spin wheels."""
    return secrets.SystemRandom()


#: Shared, thread-safe generator for non-money picks (banner weighting).
#: `secrets.SystemRandom` is used where value is involved; see :func:`system_rand`.
system_random = random.SystemRandom()


def derive_roll(*, seed: str, player_id: int, sequence: int, salt: str = "") -> float:
    """Alias used by the repositories: deterministic (seed, player, seq) → [0,1)."""
    return uniform_from(seed=seed, player_id=player_id, sequence=sequence, salt=salt)
