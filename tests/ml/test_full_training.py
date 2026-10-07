"""T-ML on the full dataset (marker ``ml``; ``make ml-train``): metrics ≥ SPEC §11.1 thresholds.

Needs ``make ml-dataset`` (12 months, seed 7); skipped without it. Retrains both models into a
temporary directory, checks the thresholds and that the committed models in ``ml/models`` were
trained on exactly this dataset (same hash, same metrics) from the current config.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from qost_ml.predictor import CARD_FILE, latest_version
from qost_ml.spec import PdmSpec
from qost_ml.train import ACCEPTANCE, TrainResult, train_all
from qost_sim.ml_dataset import config_hash
from support import REPO_ROOT
from twin_core.config import TwinConfig

pytestmark = pytest.mark.ml

FEATURES_DIR = REPO_ROOT / "ml" / "data" / "features"
MODELS_DIR = REPO_ROOT / "ml" / "models"


@pytest.fixture(scope="module")
def full_results(
    cfg: TwinConfig, tmp_path_factory: pytest.TempPathFactory
) -> dict[str, TrainResult]:
    if not (FEATURES_DIR / "meta.json").is_file():
        pytest.skip("no full dataset: run `make ml-dataset`")
    return train_all(cfg, FEATURES_DIR, tmp_path_factory.mktemp("models"))


def test_dataset_is_current(cfg: TwinConfig, spec: PdmSpec) -> None:
    if not (FEATURES_DIR / "meta.json").is_file():
        pytest.skip("no full dataset: run `make ml-dataset`")
    meta = json.loads((FEATURES_DIR / "meta.json").read_text("utf-8"))
    ds = cfg.simulation.ml_dataset
    assert meta["raw"]["months"] == ds.months == 12
    assert meta["raw"]["seed"] == ds.random_seed == 7
    assert meta["raw"]["telemetry_period_s"] == ds.telemetry_period_s == 300
    assert meta["raw"]["config_hash"] == config_hash(cfg), "config changed: rerun make ml-dataset"
    for equipment_type in spec.model_types:
        assert meta["tables"][equipment_type]["features"] == spec.feature_names(equipment_type)


@pytest.mark.parametrize("equipment_type", ["conveyor", "robot"])
def test_metrics_meet_spec_thresholds(
    full_results: dict[str, TrainResult], equipment_type: str
) -> None:
    card = full_results[equipment_type].card
    test = card["metrics"]["test"]
    for key, threshold in ACCEPTANCE[equipment_type].items():
        assert test[key] is not None
        assert test[key] >= threshold, f"{equipment_type} {key} = {test[key]} < {threshold}"
    assert all(card["thresholds"]["passed"].values())
    assert test["failures"] > 0
    assert test["failures_detected"] / test["failures"] >= 0.5


@pytest.mark.parametrize("equipment_type", ["conveyor", "robot"])
def test_committed_model_matches_the_dataset(
    full_results: dict[str, TrainResult], equipment_type: str
) -> None:
    committed = latest_version(MODELS_DIR / equipment_type)
    assert committed is not None, f"no committed {equipment_type} model: run make ml-train"
    card = json.loads((committed / CARD_FILE).read_text("utf-8"))
    fresh = full_results[equipment_type].card
    assert card["dataset_hash"] == fresh["dataset_hash"], "retrain: make ml-train"
    assert card["metrics"] == fresh["metrics"]
    assert card["features"] == fresh["features"]
    assert Path(committed / "model.txt").is_file()
