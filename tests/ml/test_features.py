"""T-ML: PdM features and labels (SPEC §11.1) — offline/online parity, no Degradation, labels."""

from __future__ import annotations

import json
import math
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from qost_ml.dataset import RawDataset, build_dataset, evaluation_times, load_raw, unit_histories
from qost_ml.features import (
    US,
    ShiftTimeline,
    StopRecord,
    compute_features,
    features_at,
    from_us,
    history_from_records,
    theil_sen_rows,
    to_grid,
    to_us,
)
from qost_ml.labels import down_at, label_rows
from qost_ml.spec import (
    EVENT_FEATURES,
    MODEL_TYPES,
    OracleLeakError,
    PdmSpec,
    assert_no_oracle,
)
from support import mutate
from twin_core.config import TwinConfig, load_config
from twin_core.stats import theil_sen

HOUR_US = 3600 * US

# --------------------------------------------------------------------------- spec from config


def test_spec_comes_from_config(spec: PdmSpec, cfg: TwinConfig) -> None:
    assert spec.model_types == MODEL_TYPES == ("robot", "conveyor")
    assert spec.types["robot"].wear_reasons == {"RB-TOOL", "EL-SENSOR"}
    assert spec.types["conveyor"].wear_reasons == {"ME-CHAIN"}  # chain_break reason
    assert spec.types["fixture"].wear_reasons == {"ME-JAM"}
    assert "booth" not in spec.types  # no wear reasons: filters are AL-M2's job
    assert "test_stand" not in spec.types  # no signals
    assert spec.horizon_h == cfg.rules.thresholds.pdm_horizon_h == 8
    assert spec.grid_s == 300
    assert spec.window_s == cfg.simulation.ml_dataset.window_min * 60
    assert spec.min_failure_s == cfg.rules.thresholds.microstop_threshold_s
    assert spec.equipment["CONV-03"] == ("conveyor", "ASSY-1")


def test_feature_list_per_spec(spec: PdmSpec) -> None:
    names = spec.feature_names("robot")
    for signal in ("motor_current_a", "joint_temp_c", "sensor_errors_h"):
        for w in (1, 4, 24):
            for stat in ("mean", "std", "slope", "max"):
                assert f"{signal}_{stat}_{w}h" in names
    assert "sensor_errors_h_count_4h" in names  # the only rate signal (1/ч)
    assert "motor_current_a_count_4h" not in names
    assert set(EVENT_FEATURES) <= set(names)
    assert len(names) == len(set(names))


# --------------------------------------------------------------------------- no Degradation


def test_degradation_is_never_a_feature(
    spec: PdmSpec, small_tables: dict[str, pl.DataFrame]
) -> None:
    """Rule 5 / FR-SIM-02: the hidden oracle never reaches features or tables."""
    for equipment_type in spec.types:
        assert not [n for n in spec.feature_names(equipment_type) if "degradation" in n.lower()]
    for table in small_tables.values():
        assert not [c for c in table.columns if "degradation" in c.lower() or "oracle" in c]
    with pytest.raises(OracleLeakError, match="Degradation"):
        assert_no_oracle(["vibration_mm_s_mean_1h", "degradation_mean_1h"])


def test_dataset_does_not_read_the_oracle_file(
    cfg: TwinConfig, ml_raw_dir: Path, tmp_path: Path
) -> None:
    raw_copy = tmp_path / "raw"
    shutil.copytree(ml_raw_dir, raw_copy)
    (raw_copy / "oracle_degradation.parquet").unlink()
    spec = PdmSpec.from_config(cfg)
    raw = load_raw(raw_copy, spec)
    assert raw.telemetry["signal"].unique().to_list() != []
    assert "degradation" not in set(raw.telemetry["signal"].to_list())


def test_oracle_signal_in_config_is_rejected(config_copy: Path) -> None:
    mutate(
        config_copy / "plant.yaml",
        '- { code: chain_elongation_pct, name_ru: "Вытяжка цепи",',
        '- { code: degradation_pct, name_ru: "Износ (оракул)", unit: "%", lo: 0, hi: 1 }\n'
        '      - { code: chain_elongation_pct, name_ru: "Вытяжка цепи",',
    )
    mutate(
        config_copy / "simulation.yaml",
        '    chain_elongation_pct: "0.4 + 2.8*d"',
        '    chain_elongation_pct: "0.4 + 2.8*d"\n    degradation_pct: "d"',
    )
    cfg = load_config(config_copy, tag_map=False)
    with pytest.raises(OracleLeakError):
        PdmSpec.from_config(cfg)


# --------------------------------------------------------------------------- offline = online


def _records_before(raw: RawDataset, code: str, line: str, t_us: int) -> dict[str, object]:
    tele = raw.telemetry.filter(
        pl.col("equipment") == code,
        pl.col("ts_us") < t_us,
        pl.col("ts_us") >= t_us - 25 * HOUR_US,
    )
    telemetry = {
        signal: ([from_us(v) for v in frame["ts_us"].to_list()], frame["value"].to_list())
        for (signal,), frame in tele.partition_by("signal", as_dict=True).items()
    }
    stops = [
        StopRecord(from_us(r["start_us"]), from_us(r["end_us"]), r["state"], r["reason"])
        for r in raw.downtime.filter(
            pl.col("equipment") == code, pl.col("end_us") < t_us
        ).iter_rows(named=True)
    ]
    exits = [
        from_us(v)
        for v in raw.units.filter(pl.col("line") == line, pl.col("ts_us") < t_us)["ts_us"].to_list()
    ]
    return {"telemetry": telemetry, "stops": stops, "exits": exits}


@pytest.mark.parametrize("code", ["ABB-04", "CONV-03", "OVEN-01", "JIG-01"])
def test_offline_and_online_features_are_identical(
    code: str, raw: RawDataset, spec: PdmSpec, cfg: TwinConfig
) -> None:
    """The engine's online path (records → features_at) equals the batch table at the same T."""
    hist = unit_histories(raw, spec, units=[code])[code]
    at = evaluation_times(raw, spec)
    offline = compute_features(hist, at, spec=spec, calendar=cfg.calendar)
    rng = np.random.default_rng(len(code))
    picks = sorted(rng.choice(len(at), size=6, replace=False).tolist())
    _type, line = spec.equipment[code]
    for i in picks:
        t_us = int(at[i])
        records = _records_before(raw, code, line, t_us)
        online_hist = history_from_records(
            code,
            spec=spec,
            until=from_us(t_us) + timedelta(seconds=37),  # floored to the grid
            telemetry=records["telemetry"],  # type: ignore[arg-type]
            stops=records["stops"],  # type: ignore[arg-type]
            exits=records["exits"],  # type: ignore[arg-type]
        )
        online = features_at(online_hist, from_us(t_us), spec=spec, calendar=cfg.calendar)
        assert list(online) == spec.feature_names(_type)
        for name, value in online.items():
            expected = float(offline[name][i])
            if math.isnan(expected):
                assert math.isnan(value), f"{code} {name} at {from_us(t_us)}"
            else:
                assert value == pytest.approx(expected, rel=1e-12, abs=1e-12), (
                    f"{code} {name} at {from_us(t_us)}: online {value} != offline {expected}"
                )


def test_future_data_never_changes_features(
    raw: RawDataset, spec: PdmSpec, cfg: TwinConfig
) -> None:
    """Truncating the history at T (stops ending at/after T, later telemetry) changes nothing."""
    hist = unit_histories(raw, spec, units=["CONV-03"])["CONV-03"]
    at = evaluation_times(raw, spec)[::40]
    full = compute_features(hist, at, spec=spec, calendar=cfg.calendar)
    for k, t_us in enumerate(at):
        slots = (int(t_us) - hist.grid0_us) // (spec.grid_s * US)
        keep = hist.stops.end_us < t_us
        cut = type(hist)(
            hist.equipment,
            hist.type,
            hist.grid0_us,
            {s: v[:slots] for s, v in hist.values.items()},
            type(hist.stops)(
                hist.stops.start_us[keep],
                hist.stops.end_us[keep],
                hist.stops.pm[keep],
                hist.stops.repair[keep],
                hist.stops.wear_repair[keep],
                hist.stops.microstop[keep],
            ),
            hist.exits_us[hist.exits_us < t_us],
        )
        row = compute_features(cut, [int(t_us)], spec=spec, calendar=cfg.calendar)
        for name, col in full.items():
            np.testing.assert_array_equal(row[name], col[k : k + 1], err_msg=name)


# --------------------------------------------------------------------------- building blocks


def test_vectorised_theil_sen_matches_twin_core() -> None:
    rng = np.random.default_rng(5)
    win = rng.normal(0, 1, (50, 12)) + np.arange(12) * 0.3
    win[3, [0, 5, 7]] = np.nan
    win[4, :11] = np.nan  # one point only → NaN
    step_h = 5 / 60
    got = theil_sen_rows(win, step_h)
    for r in range(win.shape[0]):
        line = theil_sen((np.arange(12) * step_h).astype(np.float64), win[r])
        if line is None:
            assert math.isnan(got[r])
        else:
            assert got[r] == pytest.approx(line.slope, rel=1e-12)
    # bucketed (24 h at 5 min → 24 hourly means): a clean line keeps its slope
    long = np.tile(np.arange(288) * 0.01, (2, 1))
    assert theil_sen_rows(long, step_h) == pytest.approx([0.01 / step_h] * 2)


def test_to_grid_resamples_live_samples() -> None:
    grid0 = to_us(datetime(2026, 10, 16, 2, 0, tzinfo=UTC))
    ts = [grid0 + k * 60 * US for k in range(30)]  # 60 s samples for 30 min
    values = [float(k) for k in range(30)]
    grid = to_grid(ts, values, grid0_us=grid0, n_slots=8, grid_s=300)
    assert grid[:6].tolist() == [0.0, 5.0, 10.0, 15.0, 20.0, 25.0]  # sample at each 5-min mark
    assert grid[6] == 29.0  # latest within the previous 5 min
    assert math.isnan(grid[7])
    jitter = to_grid([grid0 + 300 * US + 3], [1.0], grid0_us=grid0, n_slots=3, grid_s=300)
    assert jitter[1] == 1.0  # microsecond rounding of simulator time stays in its slot


def test_labels_and_eligibility() -> None:
    at = np.array([0, 1, 2, 9, 10], dtype=np.int64) * HOUR_US
    y, hours = label_rows(at, np.array([9 * HOUR_US + 1], dtype=np.int64), horizon_h=8)
    assert y.tolist() == [0, 0, 1, 1, 0]
    assert hours[2] == pytest.approx(7.0, abs=1e-6)
    assert math.isinf(hours[4])
    none_y, none_h = label_rows(at, np.zeros(0, dtype=np.int64), horizon_h=8)
    assert none_y.sum() == 0
    assert np.isinf(none_h).all()
    down = down_at(at, np.array([HOUR_US], dtype=np.int64), np.array([2 * HOUR_US], dtype=np.int64))
    assert down.tolist() == [False, True, False, False, False]


def test_calendar_features(cfg: TwinConfig, spec: PdmSpec) -> None:
    tz = cfg.timezone
    times = [
        datetime(2026, 10, 16, 8, 0, tzinfo=tz),  # Friday shift A
        datetime(2026, 10, 16, 16, 0, tzinfo=tz),  # shift B
        datetime(2026, 10, 16, 22, 0, tzinfo=tz),  # still B: 1 h of work left, then the weekend
        datetime(2026, 10, 18, 23, 30, tzinfo=tz),  # Sunday night: 0.5 h before Monday 07:00
        datetime(2026, 10, 25, 12, 0, tzinfo=tz),  # Sunday before the 26.10 holiday
    ]
    at = np.array([to_us(t) for t in times], dtype=np.int64)
    timeline = ShiftTimeline.build(
        cfg.calendar, spec.shift_codes, int(at.min()), int(at.max()) + 9 * HOUR_US
    )
    assert timeline.shift_index(at, 2).tolist() == [0, 1, 1, 2, 2]
    hours = timeline.working_us(at, at + 8 * HOUR_US) / HOUR_US
    assert hours.tolist() == pytest.approx([8.0, 7.0, 1.0, 0.5, 0.0])


def test_build_dataset_writes_tables_and_meta(
    cfg: TwinConfig, ml_raw_dir: Path, tmp_path: Path, spec: PdmSpec
) -> None:
    meta = build_dataset(cfg, ml_raw_dir, tmp_path, units=["ABB-01", "OVEN-01"])
    assert set(meta["tables"]) == {"robot", "oven"}
    on_disk = json.loads((tmp_path / "meta.json").read_text("utf-8"))
    assert on_disk["tables"]["robot"]["hash"] == meta["tables"]["robot"]["hash"]
    robot = pl.read_parquet(tmp_path / "robot.parquet")
    assert robot.columns[:4] == ["ts", "equipment", "type", "line"]
    assert robot.columns[-3:] == ["y", "hours_to_failure", "eligible"]
    assert robot.columns[4:-3] == spec.feature_names("robot")
    step = robot.filter(pl.col("equipment") == "ABB-01")["ts"].diff().drop_nulls().unique()
    assert step.to_list() == [timedelta(minutes=15)]
    first = robot["ts"].min()
    assert isinstance(first, datetime)
    assert first - datetime.fromisoformat(meta["raw"]["from"]) >= timedelta(hours=24)
    eligible = robot.filter(pl.col("eligible"))
    assert 0 < eligible["y"].mean() < 0.2  # type: ignore[operator]
    assert meta["tables"]["robot"]["positive_rows"] == int(eligible["y"].sum())
