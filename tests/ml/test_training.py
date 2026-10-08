"""T-ML: time split without leakage, calibration, explanations, Predictor (SPEC §11.1).

Fast versions on one simulated month; the 12-month run with the metric thresholds is
``test_full_training.py`` (marker ``ml``, ``make ml-train``).
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import polars as pl
import pytest
from sklearn.isotonic import IsotonicRegression

from qost_ml.calibration import IsotonicCalibrator
from qost_ml.catalog import FeatureCatalog
from qost_ml.explain import top_factors
from qost_ml.metrics import lead_times, summarize
from qost_ml.predictor import Predictor, latest_version
from qost_ml.rule_based import rule_risk
from qost_ml.spec import CONTEXT_FEATURES, PdmSpec
from qost_ml.train import SplitBounds, TrainResult, add_months, assign_split, save, version_dir
from twin_core.config import TwinConfig

HOUR_US = 3600 * 1_000_000

# --------------------------------------------------------------------------- time split


def test_split_follows_config_months(cfg: TwinConfig, spec: PdmSpec) -> None:
    bounds = SplitBounds.from_config(cfg, spec)
    ds = cfg.simulation.ml_dataset
    assert (ds.split.train_months, ds.split.val_months, ds.split.test_months) == (9, 1, 2)
    local = ds.from_.astimezone(cfg.timezone)
    assert bounds.start_us == int(ds.from_.timestamp()) * 1_000_000
    assert bounds.train_end_us == int(add_months(local, 9).timestamp()) * 1_000_000
    assert bounds.val_end_us == int(add_months(local, 10).timestamp()) * 1_000_000
    assert bounds.test_end_us == int(add_months(local, 12).timestamp()) * 1_000_000
    assert add_months(local, 12).isoformat().startswith("2026-10-01T00:00:00+05:00")


def test_time_split_has_no_leakage(cfg: TwinConfig, spec: PdmSpec) -> None:
    """Every label window [T, T + 8 h) stays inside its split; splits are ordered in time."""
    bounds = SplitBounds.from_config(cfg, spec)
    step = spec.window_s * 1_000_000
    ts = np.arange(bounds.start_us, bounds.test_end_us, step, dtype=np.int64)
    split = assign_split(ts, bounds)
    by_name = {name: ts[split == name] for name in ("train", "val", "test")}
    assert all(len(v) for v in by_name.values())
    for name, (lo, hi) in bounds.ranges().items():
        rows = by_name[name]
        assert rows.min() >= lo
        assert rows.max() + bounds.horizon_us <= hi  # the label never looks into the next split
    assert by_name["train"].max() < by_name["val"].min() < by_name["test"].min()
    gap = ts[split == "gap"]
    # the embargo: windows ending within one horizon before each boundary (and the dataset end)
    assert len(gap) == 3 * (bounds.horizon_us // step - 1)
    assert set(np.unique(split)) == {"train", "val", "test", "gap"}


def test_training_uses_only_past_months(
    robot_model: TrainResult, small_bounds: SplitBounds
) -> None:
    card = robot_model.card
    assert card["split"]["rule"].startswith("row in split iff")
    rows = {k: card["metrics"][k]["rows"] for k in ("train", "val", "test")}
    assert all(n > 0 for n in rows.values())
    assert card["scale_pos_weight"] > 1
    assert card["best_iteration"] >= 1
    assert "degradation" not in json.dumps(card["features"]).lower()
    assert card["not_features"][0].startswith("Degradation")
    del small_bounds


# --------------------------------------------------------------------------- calibration


def test_isotonic_calibrator_is_monotonic_and_matches_sklearn() -> None:
    rng = np.random.default_rng(3)
    raw = rng.uniform(0, 1, 4000)
    y = (rng.uniform(0, 1, 4000) < raw**2).astype(int)
    cal = IsotonicCalibrator.fit(raw, y)
    grid = np.linspace(-0.5, 1.5, 2001)
    out = cal(grid)
    assert np.all(np.diff(out) >= 0)  # T-ML: calibration is monotonic
    assert out.min() >= 0
    assert out.max() <= 1
    ref = IsotonicRegression(y_min=0, y_max=1, out_of_bounds="clip").fit(raw, y)
    np.testing.assert_allclose(out, ref.predict(grid), atol=1e-12)
    again = IsotonicCalibrator.from_dict(json.loads(json.dumps(cal.to_dict())))
    np.testing.assert_array_equal(again(grid), out)
    with pytest.raises(ValueError, match="non-decreasing"):
        IsotonicCalibrator.from_dict({"method": "isotonic", "x": [0, 1], "y": [0.5, 0.1]})
    with pytest.raises(ValueError, match="unsupported"):
        IsotonicCalibrator.from_dict({"method": "platt", "x": [0], "y": [0]})


def test_trained_calibration_is_monotonic(robot_model: TrainResult) -> None:
    ys = np.array(robot_model.calibrator.y)
    assert np.all(np.diff(ys) >= 0)
    assert np.all(np.diff(np.array(robot_model.calibrator.x)) > 0)


# --------------------------------------------------------------------------- metrics


def test_lead_time_of_first_alarm() -> None:
    hours = [7.75, 7.5, 3.0, 2.75, 1.0, 6.0, 5.0]
    rows = pl.DataFrame(
        {
            "ts": pl.Series(
                [
                    0,
                    15 * 60 * 10**6,
                    int(4.75 * HOUR_US),
                    5 * HOUR_US,
                    int(6.75 * HOUR_US),
                    100 * HOUR_US,
                    101 * HOUR_US,
                ],
                dtype=pl.Int64,
            ).cast(pl.Datetime("us", "UTC")),
            "equipment": ["A"] * 5 + ["B"] * 2,
            "y": [1] * 7,
            "hours_to_failure": hours,
        }
    )
    p = np.array([0.1, 0.2, 0.6, 0.9, 0.95, 0.3, 0.4])
    leads, events = lead_times(rows, p, warn_p=0.5)
    assert events == 2
    assert leads == [3.0]  # unit B never alarmed
    stats = summarize(rows, p, warn_p=0.5)
    assert stats["failures_detected"] == 1
    assert stats["lead_time_median_h"] == 3.0
    assert stats["pr_auc"] is None  # one class only


# --------------------------------------------------------------------------- explanations


def test_catalog_has_texts_for_every_feature(spec: PdmSpec) -> None:
    """T-ML: SHAP texts are non-empty for every feature that can appear (ru and kk)."""
    catalog = FeatureCatalog.load(spec)
    assert catalog.problems() == []
    assert (
        catalog.text("vibration_mm_s_slope_4h", 0.523, "ru") == "вибрация растёт: +0,52 мм/с за 4 ч"
    )
    assert catalog.text("vibration_mm_s_slope_4h", -0.2, "ru").startswith("вибрация снижается")
    assert (
        catalog.text("joint_temp_c_slope_24h", 2.0, "ru")
        == "рост температуры оси: +2,00 °C за 24 ч"
    )
    assert catalog.text("shift", 0, "ru") == "сейчас 1 смена"
    assert catalog.text("shift", 2, "kk") == "қазір ауысымнан тыс"
    assert catalog.text("hours_since_pm", math.nan, "ru") == "плановое ТО в истории не найдено"
    for equipment_type in spec.types:
        for feature in spec.feature_names(equipment_type):
            for value in (3.14159, -2.5, 0.0, math.nan):
                texts = catalog.texts(feature, value)
                assert texts["ru"].strip()
                assert texts["kk"].strip()
                assert "{" not in texts["ru"] + texts["kk"]


def test_top_factors_prefer_risk_raising_non_context_features(spec: PdmSpec) -> None:
    catalog = FeatureCatalog.load(spec)
    features = [
        "vibration_mm_s_slope_4h",
        "work_hours_ahead",
        "hours_since_pm",
        "shift",
        "vibration_mm_s_max_1h",
    ]
    factors = top_factors(
        features, [0.6, 8.0, 100.0, 0.0, 3.1], [0.4, 3.0, -0.9, 1.0, 0.1], catalog, k=3
    )
    assert [f.feature for f in factors] == [
        "vibration_mm_s_slope_4h",
        "vibration_mm_s_max_1h",
        "hours_since_pm",
    ]
    assert not {f.feature for f in factors} & set(CONTEXT_FEATURES)
    assert factors[0].text_ru == "вибрация растёт: +0,60 мм/с за 4 ч"


def test_shap_explanation_of_the_trained_model(
    robot_model: TrainResult, small_tables: dict[str, pl.DataFrame], spec: PdmSpec
) -> None:
    catalog = FeatureCatalog.load(spec)
    from qost_ml.explain import Explainer

    explainer = Explainer(robot_model.booster, robot_model.features, catalog)
    rows = small_tables["robot"].filter(pl.col("eligible"), pl.col("y") == 1).head(5)
    x = rows.select(robot_model.features).to_numpy().astype(np.float64)
    values = explainer.shap_values(x)
    contrib = robot_model.booster.predict(x, pred_contrib=True)
    np.testing.assert_allclose(values, contrib[:, :-1], atol=1e-9)  # exact TreeSHAP
    factors = explainer.explain(rows.row(0, named=True))
    assert len(factors) == 3
    for f in factors:
        assert f.text_ru.strip()
        assert f.text_kk.strip()
        assert f.feature in robot_model.features


# --------------------------------------------------------------------------- predictor


def test_predictor_round_trip(
    robot_model: TrainResult,
    small_tables: dict[str, pl.DataFrame],
    cfg: TwinConfig,
    tmp_path: Path,
) -> None:
    target = save(robot_model, tmp_path)
    assert target.name.startswith("v001-")
    assert {p.name for p in target.iterdir()} == {
        "model.txt",
        "calibrator.json",
        "model_card.json",
    }
    assert save(robot_model, tmp_path) == target  # same data → same version directory
    assert version_dir(tmp_path / "robot", "20991231-deadbeef").name.startswith("v002-")
    assert latest_version(tmp_path / "robot") == target
    card = json.loads((target / "model_card.json").read_text("utf-8"))
    assert card["version"] == target.name
    assert "случайные отказы не предсказываются" in card["note_ru"]

    predictor = Predictor.load(cfg, tmp_path)
    assert set(predictor.models) == {"robot"}  # no conveyor model in this directory
    table = small_tables["robot"].filter(pl.col("eligible"))
    row = table.filter(pl.col("y") == 1).row(0, named=True)
    pred = predictor.predict("robot", row)
    assert pred.source == "model"
    assert pred.model_version == target.name
    assert 0.0 <= pred.p_failure <= 1.0
    assert pred.health_index == pytest.approx(100 * (1 - pred.p_failure))
    assert len(pred.top_factors) == 3
    assert pred.as_dict()["top_factors"][0]["text_ru"]
    # batch probabilities equal the single-row path
    x = table.head(50).select(predictor.feature_names("robot")).to_numpy().astype(np.float64)
    batch = predictor.models["robot"].probability(x)
    one = [
        predictor.predict("robot", r, explain=False).p_failure
        for r in table.head(50).iter_rows(named=True)
    ]
    np.testing.assert_allclose(batch, one)

    # conveyor without a model → signal rule (still a prediction with the same interface)
    conveyor = small_tables["conveyor"].filter(pl.col("eligible")).row(0, named=True)
    rule_pred = predictor.predict("conveyor", conveyor)
    assert rule_pred.source == "rule"
    assert rule_pred.model_version is None
    with pytest.raises(KeyError, match="no PdM"):
        predictor.predict("booth", {})


def test_signal_rule(spec: PdmSpec) -> None:
    calm = {"oven_temp_c_mean_1h": 140.0, "oven_temp_c_slope_4h": 0.1}
    assert rule_risk("oven", calm, spec)[0] == 0.0
    drifting = {"oven_temp_c_mean_1h": 143.0, "oven_temp_c_slope_4h": 4.0}
    risk, signals = rule_risk("oven", drifting, spec)
    assert risk == spec.warn_p / 2
    assert signals[0].feature == "oven_temp_c_slope_4h"
    hot = {"oven_temp_c_mean_1h": 150.0}
    risk, signals = rule_risk("oven", hot, spec)
    assert spec.warn_p <= risk < spec.crit_p
    assert signals[0].feature == "oven_temp_c_mean_1h"
    low = {"clamp_pressure_bar_mean_1h": 2.5}
    assert rule_risk("fixture", low, spec)[0] == pytest.approx(
        spec.warn_p + (spec.crit_p - spec.warn_p) * 0.5
    )
    assert rule_risk("fixture", {}, spec) == (0.0, [])
