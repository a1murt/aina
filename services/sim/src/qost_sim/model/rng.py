"""Named random streams (FR-SIM-01).

Every entity draws from its own ``random.Random`` stream named like ``eq:CONV-03:fail``. String
seeds are hashed with SHA-512 by CPython, so streams do not depend on ``PYTHONHASHSEED`` and two
runs with the same seed are bit-identical. Because streams are never shared between entities, a
scenario that changes one unit does not shift the random numbers of any other (common random
numbers between a baseline and a scenario run).
"""

from __future__ import annotations

import math
import random


class Streams:
    """Factory and cache of named ``random.Random`` streams for one seed."""

    def __init__(self, seed: int) -> None:
        self.seed = seed
        self._streams: dict[str, random.Random] = {}

    def get(self, name: str) -> random.Random:
        stream = self._streams.get(name)
        if stream is None:
            stream = random.Random(f"{self.seed}:{name}")
            self._streams[name] = stream
        return stream


def lognormal(rng: random.Random, median: float, sigma: float) -> float:
    """Lognormal draw given by its median and the sigma of the underlying normal."""
    if sigma <= 0:
        return median
    return median * math.exp(sigma * rng.gauss(0.0, 1.0))


def poisson(rng: random.Random, lam: float) -> int:
    """Poisson draw (Knuth for small lambda, normal approximation for large)."""
    if lam <= 0:
        return 0
    if lam > 30:
        return max(0, round(rng.gauss(lam, math.sqrt(lam))))
    limit = math.exp(-lam)
    k = 0
    p = rng.random()
    while p > limit:
        k += 1
        p *= rng.random()
    return k
