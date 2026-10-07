"""Isotonic probability calibration (SPEC §11.1), stored as plain JSON breakpoints.

Fitted with scikit-learn's ``IsotonicRegression`` on the validation month; applied as piecewise
linear interpolation between the breakpoints, clipped at both ends — exactly what
``IsotonicRegression.predict`` does, without pickling a scikit-learn object. The map is
non-decreasing by construction (T-ML checks it).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt

F64 = npt.NDArray[np.float64]


@dataclass(frozen=True, slots=True)
class IsotonicCalibrator:
    x: tuple[float, ...]
    """Raw model scores (increasing)."""
    y: tuple[float, ...]
    """Calibrated probabilities (non-decreasing)."""

    @classmethod
    def fit(cls, raw: F64, labels: npt.NDArray[Any]) -> IsotonicCalibrator:
        from sklearn.isotonic import IsotonicRegression

        iso = IsotonicRegression(y_min=0.0, y_max=1.0, increasing=True, out_of_bounds="clip")
        iso.fit(np.asarray(raw, dtype=np.float64), np.asarray(labels, dtype=np.float64))
        xs = tuple(float(v) for v in iso.X_thresholds_)
        ys = tuple(float(v) for v in iso.y_thresholds_)
        return cls(xs, ys)

    def __call__(self, raw: F64 | float) -> F64:
        return np.interp(np.asarray(raw, dtype=np.float64), self.x, self.y)

    def to_dict(self) -> dict[str, Any]:
        return {"method": "isotonic", "x": list(self.x), "y": list(self.y)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> IsotonicCalibrator:
        if data.get("method") != "isotonic":
            raise ValueError(f"unsupported calibration method {data.get('method')!r}")
        xs = tuple(float(v) for v in data["x"])
        ys = tuple(float(v) for v in data["y"])
        if len(xs) != len(ys) or not xs:
            raise ValueError("calibrator needs equally long non-empty x and y")
        if any(b < a for a, b in pairwise(ys)):
            raise ValueError("calibrator y must be non-decreasing")
        return cls(xs, ys)

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(self.to_dict(), indent=1), "utf-8")

    @classmethod
    def load(cls, path: Path) -> IsotonicCalibrator:
        data: dict[str, Any] = json.loads(path.read_text("utf-8"))
        return cls.from_dict(data)
