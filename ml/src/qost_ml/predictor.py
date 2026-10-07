"""Serving interface for the engine (stage M7b): p_failure(8 h), health index, top factors.

    predictor = Predictor.load(cfg)                     # latest model per type, offline
    row = features_at(history, clock.now(), spec=predictor.spec, calendar=cfg.calendar)
    pred = predictor.predict("conveyor", row)
    pred.p_failure, pred.health_index, pred.top_factors[0].text_ru

Types with a LightGBM model (``ml/models/{type}/{version}/``) get the calibrated model probability
and SHAP factors; other PdM types get the signal rule (:mod:`qost_ml.rule_based`). No network
access: models, calibrators and the feature catalog are local files.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import numpy as np

from qost_ml.calibration import IsotonicCalibrator
from qost_ml.catalog import FeatureCatalog, search_upwards
from qost_ml.explain import TOP_K, Explainer, Factor
from qost_ml.rule_based import rule_risk
from qost_ml.spec import PdmSpec, assert_no_oracle
from twin_core.config import TwinConfig

MODEL_FILE = "model.txt"
CALIBRATOR_FILE = "calibrator.json"
CARD_FILE = "model_card.json"


def default_models_dir() -> Path:
    """``ML_MODELS_DIR`` or the repository's ``ml/models``."""
    env = os.environ.get("ML_MODELS_DIR")
    if env:
        return Path(env)
    found = search_upwards(Path("ml") / "models")
    return found if found is not None else Path("ml") / "models"


def latest_version(type_dir: Path) -> Path | None:
    """Newest ``{version}`` directory with a model card (versions sort by plant date first)."""
    if not type_dir.is_dir():
        return None
    versions = sorted(p for p in type_dir.iterdir() if (p / CARD_FILE).is_file())
    return versions[-1] if versions else None


@dataclass(frozen=True, slots=True)
class Prediction:
    equipment_type: str
    p_failure: float
    """Probability of a wear-reason failure within ``horizon_h`` (rule types: rule score)."""
    health_index: float
    """100 × (1 − p_failure)."""
    top_factors: tuple[Factor, ...]
    source: Literal["model", "rule"]
    model_version: str | None
    horizon_h: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "equipment_type": self.equipment_type,
            "p_failure": self.p_failure,
            "health_index": self.health_index,
            "top_factors": [f.as_dict() for f in self.top_factors],
            "source": self.source,
            "model_version": self.model_version,
            "horizon_h": self.horizon_h,
        }


@dataclass(frozen=True)
class LoadedModel:
    equipment_type: str
    version: str
    booster: Any
    calibrator: IsotonicCalibrator
    features: list[str]
    card: dict[str, Any]
    explainer: Explainer
    path: Path

    @classmethod
    def load(cls, path: Path, catalog: FeatureCatalog) -> LoadedModel:
        import lightgbm as lgb

        card: dict[str, Any] = json.loads((path / CARD_FILE).read_text("utf-8"))
        features = [str(f) for f in card["features"]]
        assert_no_oracle(features)
        booster = lgb.Booster(model_file=str(path / MODEL_FILE))
        if booster.feature_name() != features:
            raise ValueError(f"{path}: model features differ from the model card")
        calibrator = IsotonicCalibrator.load(path / CALIBRATOR_FILE)
        explainer = Explainer(booster, features, catalog)
        return cls(
            str(card["type"]),
            str(card["version"]),
            booster,
            calibrator,
            features,
            card,
            explainer,
            path,
        )

    def probability(
        self, rows: np.ndarray[Any, np.dtype[np.float64]]
    ) -> np.ndarray[Any, np.dtype[np.float64]]:
        raw = self.booster.predict(rows)
        return self.calibrator(np.asarray(raw, dtype=np.float64))


class Predictor:
    """Latest model per type + rule fallback, with SHAP/rule factors in ru and kk."""

    def __init__(
        self, spec: PdmSpec, catalog: FeatureCatalog, models: Mapping[str, LoadedModel]
    ) -> None:
        self.spec = spec
        self.catalog = catalog
        self.models = dict(models)

    @classmethod
    def load(
        cls,
        cfg: TwinConfig,
        models_dir: Path | None = None,
        *,
        catalog_path: Path | None = None,
    ) -> Predictor:
        spec = PdmSpec.from_config(cfg)
        catalog = FeatureCatalog.load(spec, catalog_path)
        root = models_dir or default_models_dir()
        models: dict[str, LoadedModel] = {}
        for equipment_type in spec.model_types:
            path = latest_version(root / equipment_type)
            if path is not None:
                models[equipment_type] = LoadedModel.load(path, catalog)
        return cls(spec, catalog, models)

    def feature_names(self, equipment_type: str) -> list[str]:
        model = self.models.get(equipment_type)
        return list(model.features) if model else self.spec.feature_names(equipment_type)

    def predict(
        self,
        equipment_type: str,
        features: Mapping[str, float],
        *,
        explain: bool = True,
        k: int = TOP_K,
    ) -> Prediction:
        """Prediction for one unit from its feature row (:func:`qost_ml.features.features_at`)."""
        if equipment_type not in self.spec.types:
            raise KeyError(f"no PdM for equipment type {equipment_type!r}")
        assert_no_oracle(tuple(features))
        model = self.models.get(equipment_type)
        if model is not None:
            row = np.array([[features.get(f, math.nan) for f in model.features]], dtype=np.float64)
            p = float(model.probability(row)[0])
            factors = tuple(model.explainer.explain(features, k)) if explain else ()
            return self._prediction(equipment_type, p, factors, "model", model.version)
        p, risks = rule_risk(equipment_type, features, self.spec)
        rule_factors: tuple[Factor, ...] = ()
        if explain:
            factors_list: list[Factor] = []
            for r in risks[:k]:
                if r.risk > 0:
                    texts = self.catalog.texts(r.feature, r.value)
                    factors_list.append(
                        Factor(r.feature, r.value, r.risk, texts["ru"], texts["kk"])
                    )
            rule_factors = tuple(factors_list)
        return self._prediction(equipment_type, p, rule_factors, "rule", None)

    def _prediction(
        self,
        equipment_type: str,
        p: float,
        factors: tuple[Factor, ...],
        source: Literal["model", "rule"],
        version: str | None,
    ) -> Prediction:
        p = min(1.0, max(0.0, p))
        return Prediction(
            equipment_type=equipment_type,
            p_failure=p,
            health_index=100.0 * (1.0 - p),
            top_factors=factors,
            source=source,
            model_version=version,
            horizon_h=self.spec.horizon_h,
        )
