"""Labels of the PdM dataset (SPEC §11.1) — offline only.

y = 1 if within ``[T, T + horizon)`` the unit starts an unplanned failure of at least
``microstop_threshold_s`` with a wear reason of its type. Random failures are not labelled: they
have no precursors by design and the model does not promise to predict them.

Rows where the unit is already down at ``T`` (any ``DOWN_*``) are not eligible for training or
evaluation: the failure has already happened or the unit is being serviced.
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt

from qost_ml.features import F64, I64, US, B

I8 = npt.NDArray[np.int8]

_HOUR_US = 3600 * US


def label_rows(at_us: I64, failure_start_us: I64, horizon_h: float) -> tuple[I8, F64]:
    """(y, hours to the next labelled failure — inf if none) for each evaluation time."""
    starts = np.sort(failure_start_us)
    idx = np.searchsorted(starts, at_us, side="left")
    found = idx < len(starts)
    nxt = starts[np.minimum(idx, max(len(starts) - 1, 0))] if len(starts) else np.zeros_like(at_us)
    hours = np.where(found, (nxt - at_us) / _HOUR_US, np.inf)
    y = (hours < horizon_h).astype(np.int8)
    return y, hours


def down_at(at_us: I64, down_start_us: I64, down_end_us: I64) -> B:
    """True where ``T`` falls inside one of the (non-overlapping) down intervals."""
    if len(down_start_us) == 0:
        return np.zeros(len(at_us), dtype=np.bool_)
    order = np.argsort(down_start_us, kind="stable")
    starts, ends = down_start_us[order], down_end_us[order]
    idx = np.searchsorted(starts, at_us, side="right") - 1
    safe = np.maximum(idx, 0)
    return (idx >= 0) & (at_us < ends[safe])
