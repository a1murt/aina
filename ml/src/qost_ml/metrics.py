"""Evaluation of PdM predictions (SPEC §11.1): PR-AUC, ROC-AUC, Brier, lead time of the first alarm.

Lead time: for every labelled failure, the first row in its horizon (``[start − 8 h, start)``, unit
not down) with p ≥ ``pdm_warn_p`` raises the alarm; lead = failure start − that row's ``T``.
The median is over detected failures; the share of detected failures is reported next to it.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import numpy.typing as npt
import polars as pl

from qost_ml.features import US

F64 = npt.NDArray[np.float64]


def pr_auc(y: npt.NDArray[Any], p: F64) -> float | None:
    from sklearn.metrics import average_precision_score

    if len(np.unique(y)) < 2:
        return None
    return float(average_precision_score(y, p))


def roc_auc(y: npt.NDArray[Any], p: F64) -> float | None:
    from sklearn.metrics import roc_auc_score

    if len(np.unique(y)) < 2:
        return None
    return float(roc_auc_score(y, p))


def lead_times(rows: pl.DataFrame, p: F64, warn_p: float) -> tuple[list[float], int]:
    """(lead hours of detected failures, number of failures) from labelled rows.

    ``rows`` needs ``ts``, ``equipment``, ``y``, ``hours_to_failure`` (eligible rows only).
    """
    frame = rows.select("ts", "equipment", "y", "hours_to_failure").with_columns(
        pl.Series("p", p),
    )
    positives = frame.filter(pl.col("y") == 1).with_columns(
        (
            pl.col("ts").dt.epoch("us")
            + (pl.col("hours_to_failure") * 3600 * US).round(0).cast(pl.Int64)
        ).alias("failure_us")
    )
    if positives.is_empty():
        return [], 0
    # failures are identified to the second: float hours lose sub-microsecond precision
    positives = positives.with_columns((pl.col("failure_us") // US).alias("failure_s"))
    alarms = (
        positives.sort("ts")
        .group_by("equipment", "failure_s", maintain_order=True)
        .agg(
            pl.col("hours_to_failure").filter(pl.col("p") >= warn_p).first().alias("lead_h"),
        )
    )
    leads = [float(v) for v in alarms["lead_h"].to_list() if v is not None]
    return leads, len(alarms)


def summarize(rows: pl.DataFrame, p: F64, *, warn_p: float) -> dict[str, float | int | None]:
    """All metrics for one split (``rows``: eligible rows with ``y``)."""
    y = rows["y"].to_numpy()
    leads, events = lead_times(rows, p, warn_p)
    alarm = p >= warn_p
    tp = int(np.sum(alarm & (y == 1)))
    median_lead = float(np.median(leads)) if leads else None
    return {
        "rows": len(rows),
        "positive_rows": int(y.sum()),
        "base_rate": float(y.mean()) if len(y) else None,
        "pr_auc": pr_auc(y, p),
        "roc_auc": roc_auc(y, p),
        "brier": float(np.mean((p - y) ** 2)) if len(y) else None,
        "precision_at_warn": tp / int(alarm.sum()) if alarm.any() else None,
        "recall_at_warn": tp / int(y.sum()) if y.any() else None,
        "failures": events,
        "failures_detected": len(leads),
        "lead_time_median_h": median_lead,
        "lead_time_p25_h": float(np.percentile(leads, 25)) if leads else None,
    }


def rounded(metrics: dict[str, Any], digits: int = 4) -> dict[str, Any]:
    return {
        k: (round(v, digits) if isinstance(v, float) and math.isfinite(v) else v)
        for k, v in metrics.items()
    }
