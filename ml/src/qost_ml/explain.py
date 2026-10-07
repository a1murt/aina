"""Explanations of a prediction (SPEC §11.1): SHAP TreeExplainer → top-3 features → text.

SHAP values are in the model's raw (log-odds) space. The top factors are the features that push the
risk up the most (positive SHAP, largest first); if fewer than ``k`` push it up, the strongest of
the rest follow. Calendar context (current shift, scheduled hours ahead) is left out: it changes
the probability but is not a cause. Each factor carries its value and the ru/kk text from
``ml/feature_catalog.yaml``.
"""

from __future__ import annotations

import math
import warnings
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import numpy.typing as npt

from qost_ml.catalog import FeatureCatalog
from qost_ml.spec import CONTEXT_FEATURES

TOP_K = 3


@dataclass(frozen=True, slots=True)
class Factor:
    feature: str
    value: float
    shap: float
    """Contribution to the log-odds of failure (> 0 raises the risk)."""
    text_ru: str
    text_kk: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "feature": self.feature,
            "value": None if math.isnan(self.value) else self.value,
            "shap": self.shap,
            "text_ru": self.text_ru,
            "text_kk": self.text_kk,
        }


def top_factors(
    features: Sequence[str],
    values: Sequence[float] | npt.NDArray[np.float64],
    contributions: Sequence[float] | npt.NDArray[np.float64],
    catalog: FeatureCatalog,
    k: int = TOP_K,
    exclude: Sequence[str] = CONTEXT_FEATURES,
) -> list[Factor]:
    """Pick the ``k`` most risk-raising features (outside ``exclude``) and render their texts."""
    order = sorted(
        (i for i in range(len(features)) if features[i] not in exclude),
        key=lambda i: (contributions[i] <= 0, -abs(float(contributions[i])), features[i]),
    )
    out: list[Factor] = []
    for i in order[:k]:
        value = float(values[i])
        texts = catalog.texts(features[i], value)
        out.append(Factor(features[i], value, float(contributions[i]), texts["ru"], texts["kk"]))
    return out


class Explainer:
    """SHAP ``TreeExplainer`` over a LightGBM booster plus the feature catalog."""

    def __init__(self, booster: Any, features: Sequence[str], catalog: FeatureCatalog) -> None:
        self._booster = booster
        self._features = list(features)
        self._catalog = catalog
        self._tree: Any = None

    def shap_values(self, x: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
        """SHAP values (rows × features) in log-odds."""
        if self._tree is None:
            import shap

            self._tree = shap.TreeExplainer(self._booster)
        with warnings.catch_warnings():
            # shap warns that binary LightGBM output "changed to a list"; we normalise both forms
            warnings.simplefilter("ignore", UserWarning)
            values = self._tree.shap_values(x)
        if isinstance(values, list):
            values = values[-1]
        return np.asarray(values, dtype=np.float64)

    def explain(self, row: Mapping[str, float], k: int = TOP_K) -> list[Factor]:
        x = np.array([[row.get(f, math.nan) for f in self._features]], dtype=np.float64)
        contributions = self.shap_values(x)[0]
        return top_factors(self._features, x[0], contributions, self._catalog, k)
