"""
Deterministic random seeding for DEMO_MODE.

When DEMO_MODE is on, every random sampling operation (voterfile slicing,
precinct selection, callable shuffling) needs to produce the same numbers
on every run, otherwise a re-demo shows different counts and the audience
loses trust. This module is the one place that decides whether to seed.

Usage:
    from chat.utils.random_seed import maybe_seed_random

    rng = maybe_seed_random(scope="voterfile")
    sample = rng.sample(rows, k=100)

The `scope` argument lets us derive a per-operation seed off the master
seed, so two unrelated pieces of randomness do not interfere with each
other while still being deterministic individually.
"""
from __future__ import annotations

import hashlib
import random
from typing import Optional

from django.conf import settings


def _scoped_seed(scope: str, master_seed: int) -> int:
    """
    Derive a deterministic per-scope seed from the master seed and a label.

    Example:
        _scoped_seed("voterfile", 2026) -> stable integer
        _scoped_seed("precincts", 2026) -> different stable integer

    A short SHA-256 truncation gives us 64 bits of seed space, plenty for
    Python's random module and well past the variance any demo will hit.
    """
    payload = f"{master_seed}:{scope}".encode("utf-8")
    digest = hashlib.sha256(payload).digest()
    # First 8 bytes -> unsigned 64-bit int
    return int.from_bytes(digest[:8], byteorder="big", signed=False)


def maybe_seed_random(scope: str = "default") -> random.Random:
    """
    Return a `random.Random` instance.

    - When DEMO_MODE is on: seeded with a per-scope deterministic seed.
    - When DEMO_MODE is off: seeded from system entropy (default behaviour).

    Always returns a fresh instance, never touches the global `random` state,
    so callers don't accidentally leak determinism into other code paths.
    """
    if getattr(settings, "DEMO_MODE", False):
        master = getattr(settings, "DEMO_RANDOM_SEED", 2026)
        return random.Random(_scoped_seed(scope, master))
    return random.Random()


def is_demo_mode() -> bool:
    """Convenience boolean for callers that just need to branch on the flag."""
    return bool(getattr(settings, "DEMO_MODE", False))
