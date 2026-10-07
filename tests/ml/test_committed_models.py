"""The committed PdM models (``ml/models``, used offline by ``make demo``) fit the current code.

If a feature, the catalog or the config changes, these fail until ``make ml-dataset ml-train``.
"""

from __future__ import annotations

import re

import polars as pl
import pytest

from qost_ml.predictor import Predictor
from qost_ml.spec import PdmSpec
from qost_ml.train import ACCEPTANCE
from twin_core.config import TwinConfig


@pytest.fixture(scope="module")
def predictor(cfg: TwinConfig) -> Predictor:
    return Predictor.load(cfg)


def test_latest_model_per_type_is_committed(predictor: Predictor, spec: PdmSpec) -> None:
    assert set(predictor.models) == set(spec.model_types)
    for equipment_type, model in predictor.models.items():
        card = model.card
        assert re.fullmatch(r"v\d{3}-\d{8}-[0-9a-f]{8}", model.version)
        assert card["type"] == equipment_type
        assert card["features"] == spec.feature_names(equipment_type)
        assert card["thresholds"]["acceptance"] == ACCEPTANCE[equipment_type]
        assert all(card["thresholds"]["passed"].values())
        assert "случайные отказы не предсказываются" in card["note_ru"]
        assert card["trained_on"]["seed"] == 7
        assert card["metrics"]["test"]["pr_auc"] >= ACCEPTANCE[equipment_type]["pr_auc"]
        assert "degradation" not in " ".join(card["features"]).lower()


def test_committed_models_predict_offline(
    predictor: Predictor, small_tables: dict[str, pl.DataFrame]
) -> None:
    for equipment_type in ("robot", "conveyor"):
        rows = small_tables[equipment_type].filter(pl.col("eligible"))
        hot = rows.filter(pl.col("y") == 1).row(-1, named=True)
        pred = predictor.predict(equipment_type, hot)
        assert pred.source == "model"
        assert 0 <= pred.p_failure <= 1
        assert len(pred.top_factors) == 3
        assert all(f.text_ru and f.text_kk for f in pred.top_factors)
    oven = small_tables["oven"].filter(pl.col("eligible")).row(0, named=True)
    assert predictor.predict("oven", oven).source == "rule"
