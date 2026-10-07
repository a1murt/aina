"""Training of the PdM models (SPEC §11.1; ``make ml-train``).

Per equipment type in :data:`qost_ml.spec.MODEL_TYPES` (robot, conveyor):

* time split by ``simulation.yaml: ml_dataset.split`` (9/1/2 months from ``ml_dataset.from``, plant
  calendar months) with an embargo of one label horizon before each boundary: a row belongs to a
  split only if its whole label window ``[T, T + 8 h)`` lies inside it — no label leaks across;
* LightGBM binary with ``scale_pos_weight`` = negatives / positives of the training rows, early
  stopping on validation PR-AUC (``average_precision``);
* isotonic calibration on the validation month (:mod:`qost_ml.calibration`);
* metrics on the untouched test months, :data:`ACCEPTANCE` thresholds, ``model_card.json``.

Artifacts: ``ml/models/{type}/{version}/`` — ``model.txt`` (LightGBM text format),
``calibrator.json``, ``model_card.json``. ``version`` = ``vNNN-<dataset end, plant date>-<dataset
hash>``: NNN grows with every new dataset, so the latest model sorts last; retraining on the same
data reuses its directory (deterministic LightGBM, fixed seed — the same files).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import numpy.typing as npt
import polars as pl

from qost_ml.calibration import IsotonicCalibrator
from qost_ml.dataset import frame_hash
from qost_ml.features import US, from_us, to_us
from qost_ml.metrics import rounded, summarize
from qost_ml.spec import CATEGORICAL, PdmSpec, assert_no_oracle
from twin_core.config import TwinConfig

F64 = npt.NDArray[np.float64]

ACCEPTANCE: dict[str, dict[str, float]] = {
    "conveyor": {"pr_auc": 0.60, "roc_auc": 0.80, "lead_time_median_h": 2.0},
    "robot": {"pr_auc": 0.40, "roc_auc": 0.75},
}
"""SPEC §11.1 thresholds on the test months (synthetic data)."""

NOTE_RU = (
    "Метрики получены на синтетике (виртуальный завод). Модель предсказывает только "
    "отказы по износовым причинам; случайные отказы не предсказываются. Перед пилотом — "
    "дообучение на данных завода."
)
NOTE_EN = (
    "Metrics on synthetic data (virtual plant). The model predicts wear-reason failures "
    "only; random failures are not predicted. Retrain on plant data before the pilot."
)

BASE_PARAMS: dict[str, Any] = {
    "objective": "binary",
    "metric": "average_precision",
    "learning_rate": 0.03,
    "num_leaves": 15,
    "min_data_in_leaf": 200,
    "feature_fraction": 0.8,
    "bagging_fraction": 0.8,
    "bagging_freq": 1,
    "lambda_l2": 1.0,
    "max_bin": 255,
    "seed": 7,
    "deterministic": True,
    "force_col_wise": True,
    "num_threads": 4,
    "verbosity": -1,
}
NUM_BOOST_ROUND = 2000
EARLY_STOPPING_ROUNDS = 100
SPLITS = ("train", "val", "test")


def add_months(instant: datetime, months: int) -> datetime:
    month0 = instant.month - 1 + months
    return instant.replace(year=instant.year + month0 // 12, month=month0 % 12 + 1)


@dataclass(frozen=True, slots=True)
class SplitBounds:
    """Split boundaries (UTC µs) and the label horizon used as an embargo."""

    start_us: int
    train_end_us: int
    val_end_us: int
    test_end_us: int
    horizon_us: int
    timezone: str
    """Plant time zone: months are plant calendar months, versions use plant dates."""

    @classmethod
    def from_config(cls, cfg: TwinConfig, spec: PdmSpec) -> SplitBounds:
        ds = cfg.simulation.ml_dataset
        start = ds.from_.astimezone(cfg.timezone)  # calendar months of the plant
        train_end = add_months(start, ds.split.train_months)
        val_end = add_months(train_end, ds.split.val_months)
        test_end = add_months(val_end, ds.split.test_months)
        return cls(
            to_us(start),
            to_us(train_end),
            to_us(val_end),
            to_us(test_end),
            round(spec.horizon_h * 3600 * US),
            cfg.plant.site.timezone,
        )

    def ranges(self) -> dict[str, tuple[int, int]]:
        return {
            "train": (self.start_us, self.train_end_us),
            "val": (self.train_end_us, self.val_end_us),
            "test": (self.val_end_us, self.test_end_us),
        }


def assign_split(ts_us: npt.NDArray[np.int64], bounds: SplitBounds) -> npt.NDArray[np.str_]:
    """``train`` / ``val`` / ``test`` if ``[T, T + horizon)`` is inside the split, else ``gap``."""
    out = np.full(len(ts_us), "gap", dtype="<U5")
    for name, (lo, hi) in bounds.ranges().items():
        inside = (ts_us >= lo) & (ts_us + bounds.horizon_us <= hi)
        out[inside] = name
    return out


@dataclass(frozen=True)
class TrainResult:
    equipment_type: str
    booster: Any
    """``lightgbm.Booster``."""
    calibrator: IsotonicCalibrator
    features: list[str]
    card: dict[str, Any]


def _matrix(frame: pl.DataFrame, features: list[str]) -> F64:
    return frame.select(features).to_numpy().astype(np.float64)


def train_type(
    table: pl.DataFrame,
    *,
    spec: PdmSpec,
    bounds: SplitBounds,
    equipment_type: str,
    dataset_meta: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    num_boost_round: int = NUM_BOOST_ROUND,
) -> TrainResult:
    """Train, calibrate and evaluate one type's model on its feature table."""
    import lightgbm as lgb

    features = spec.feature_names(equipment_type)
    assert_no_oracle(features)
    assert_no_oracle(tuple(table.columns))
    rows = table.filter(pl.col("eligible")).sort("ts", "equipment")
    split = assign_split(rows["ts"].dt.epoch("us").to_numpy(), bounds)
    parts = {name: rows.filter(pl.Series(split == name)) for name in SPLITS}
    for name, part in parts.items():
        if part.is_empty() or part["y"].sum() == 0:
            raise ValueError(f"{equipment_type}: split {name!r} has no positive rows")

    x_train, y_train = _matrix(parts["train"], features), parts["train"]["y"].to_numpy()
    x_val, y_val = _matrix(parts["val"], features), parts["val"]["y"].to_numpy()
    pos = int(y_train.sum())
    scale_pos_weight = (len(y_train) - pos) / pos
    run_params = {**BASE_PARAMS, **(params or {}), "scale_pos_weight": scale_pos_weight}
    categorical = [f for f in features if f in CATEGORICAL]
    dtrain = lgb.Dataset(
        x_train,
        label=y_train,
        feature_name=features,
        categorical_feature=categorical,
        free_raw_data=False,
    )
    dval = lgb.Dataset(x_val, label=y_val, reference=dtrain, free_raw_data=False)
    booster = lgb.train(
        run_params,
        dtrain,
        num_boost_round=num_boost_round,
        valid_sets=[dval],
        valid_names=["val"],
        callbacks=[
            lgb.early_stopping(EARLY_STOPPING_ROUNDS, first_metric_only=True, verbose=False)
        ],
    )
    best = booster.best_iteration or booster.current_iteration()

    raw_val = np.asarray(booster.predict(x_val, num_iteration=best), dtype=np.float64)
    calibrator = IsotonicCalibrator.fit(raw_val, y_val)

    metrics: dict[str, Any] = {}
    for name, part in parts.items():
        raw = np.asarray(booster.predict(_matrix(part, features), num_iteration=best), np.float64)
        metrics[name] = rounded(summarize(part, calibrator(raw), warn_p=spec.warn_p))
        if name == "test":
            metrics["test_uncalibrated"] = rounded(
                {k: v for k, v in summarize(part, raw, warn_p=0.5).items() if "auc" in k}
            )
    acceptance = ACCEPTANCE.get(equipment_type, {})
    checks = {
        key: (metrics["test"].get(key) is not None and metrics["test"][key] >= threshold)
        for key, threshold in acceptance.items()
    }
    gain = booster.feature_importance(importance_type="gain", iteration=best)
    importance = sorted(
        ((f, float(g)) for f, g in zip(features, gain, strict=True)), key=lambda kv: -kv[1]
    )
    table_hash = frame_hash(table)
    raw_meta = (dataset_meta or {}).get("raw", {})
    data_to = (
        to_us(datetime.fromisoformat(raw_meta["to"])) if "to" in raw_meta else bounds.test_end_us
    )
    plant_date = from_us(data_to).astimezone(ZoneInfo(bounds.timezone)).date()
    card: dict[str, Any] = {
        "type": equipment_type,
        "version": None,  # vNNN-<data_id>, assigned by save()
        "data_id": f"{plant_date:%Y%m%d}-{table_hash[:8]}",
        "model": "lightgbm binary",
        "lightgbm_version": lgb.__version__,
        "target": {
            "text_ru": (
                f"внеплановый отказ ≥ {spec.min_failure_s / 60:.0f} мин по износовой причине "
                f"в ближайшие {spec.horizon_h:.0f} ч"
            ),
            "horizon_h": spec.horizon_h,
            "wear_reasons": sorted(spec.types[equipment_type].wear_reasons),
            "min_duration_s": spec.min_failure_s,
        },
        "trained_on": {
            "dataset_from": raw_meta.get("from"),
            "dataset_to": raw_meta.get("to"),
            "seed": raw_meta.get("seed"),
            "config_hash": raw_meta.get("config_hash"),
            "telemetry_period_s": raw_meta.get("telemetry_period_s"),
            "training_data_until": _iso(bounds.val_end_us),
        },
        "dataset_hash": table_hash,
        "split": {
            "train": [_iso(bounds.start_us), _iso(bounds.train_end_us)],
            "val": [_iso(bounds.train_end_us), _iso(bounds.val_end_us)],
            "test": [_iso(bounds.val_end_us), _iso(bounds.test_end_us)],
            "embargo_h": spec.horizon_h,
            "rule": "row in split iff [T, T + horizon) lies inside it",
        },
        "features": features,
        "categorical_features": categorical,
        "not_features": [
            "Degradation (hidden simulator state, FR-SIM-02)",
            "type (one model per type)",
        ],
        "params": {k: v for k, v in run_params.items() if k != "num_threads"},
        "best_iteration": best,
        "scale_pos_weight": round(scale_pos_weight, 4),
        "calibration": "isotonic on the validation month",
        "metrics": metrics,
        "thresholds": {
            "acceptance": acceptance,
            "passed": checks,
            "alert": {"pdm_warn_p": spec.warn_p, "pdm_crit_p": spec.crit_p},
        },
        "feature_importance_gain_top10": [[f, round(g, 1)] for f, g in importance[:10]],
        "note_ru": NOTE_RU,
        "note_en": NOTE_EN,
    }
    return TrainResult(equipment_type, booster, calibrator, features, card)


def _iso(us: int) -> str:
    return from_us(us).isoformat().replace("+00:00", "Z")


_VERSION = re.compile(r"^v(?P<n>\d{3})-(?P<data>\d{8}-[0-9a-f]{8})$")


def version_dir(type_dir: Path, data_id: str) -> Path:
    """``vNNN-<data_id>``: the existing directory for the same data, else the next number."""
    numbers = [0]
    if type_dir.is_dir():
        for path in sorted(type_dir.iterdir()):
            match = _VERSION.match(path.name)
            if match is None:
                continue
            if match["data"] == data_id:
                return path
            numbers.append(int(match["n"]))
    return type_dir / f"v{max(numbers) + 1:03d}-{data_id}"


def save(result: TrainResult, models_dir: Path) -> Path:
    """Write ``model.txt``, ``calibrator.json``, ``model_card.json`` into ``{type}/{version}``."""
    target = version_dir(models_dir / result.equipment_type, str(result.card["data_id"]))
    result.card["version"] = target.name
    target.mkdir(parents=True, exist_ok=True)
    result.booster.save_model(
        str(target / "model.txt"), num_iteration=result.card["best_iteration"]
    )
    result.calibrator.save(target / "calibrator.json")
    (target / "model_card.json").write_text(
        json.dumps(result.card, indent=2, ensure_ascii=False) + "\n", "utf-8"
    )
    return target


def train_all(
    cfg: TwinConfig,
    features_dir: Path,
    models_dir: Path,
    *,
    types: tuple[str, ...] | None = None,
) -> dict[str, TrainResult]:
    """``make ml-train``: train every model type from ``features_dir`` into ``models_dir``."""
    spec = PdmSpec.from_config(cfg)
    bounds = SplitBounds.from_config(cfg, spec)
    meta: dict[str, Any] = json.loads((features_dir / "meta.json").read_text("utf-8"))
    results: dict[str, TrainResult] = {}
    for equipment_type in types or spec.model_types:
        table = pl.read_parquet(features_dir / f"{equipment_type}.parquet")
        result = train_type(
            table, spec=spec, bounds=bounds, equipment_type=equipment_type, dataset_meta=meta
        )
        save(result, models_dir)
        results[equipment_type] = result
    return results
