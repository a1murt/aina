"""Keyed random streams for common random numbers (SPEC §10.2).

Every draw comes from a generator keyed by ``(seed, stream, slot)`` where ``slot`` is a calendar
position (hour of the month, shift instance, CKD lot, ...). A baseline and a scenario run with
the same seed therefore see identical numbers wherever the calendar is the same, even when a
scenario adds shifts or changes cycle times, buffers or failure rates.
"""

from __future__ import annotations

from enum import IntEnum

import numpy as np


class Stream(IntEnum):
    FAILURES = 1
    LAMBDA = 2
    DEFECTS = 3
    EFFICIENCY = 4
    FILTER_RATE = 5
    FILTER_SWAP = 6
    FILTER_START = 7
    OPEN_REPAIR = 8
    CKD_LOT = 9
    CKD_TRANSIT = 10


_MASK = (1 << 63) - 1


def generator(seed: int, stream: Stream, slot: int = 0) -> np.random.Generator:
    key = [int(seed) & _MASK, int(stream), int(slot) & _MASK]
    return np.random.Generator(np.random.PCG64(np.random.SeedSequence(key)))
