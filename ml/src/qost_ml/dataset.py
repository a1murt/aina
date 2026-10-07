"""Windowed feature table from the raw ml-dataset (SPEC §11.1; ``make ml-dataset``, step 2).

Input — the parquet files of ``python -m qost_sim ml-dataset`` (``telemetry/``, ``downtime``,
``units``, ``meta.json``). ``oracle_degradation.parquet`` (the simulator's hidden wear) is never
opened here (FR-SIM-02, project rule 5).

Output — ``{type}.parquet`` per PdM equipment type: one row per unit every ``ml_dataset.window_min``
(15 min) from ``from + 24 h`` (full feature windows) to ``to − horizon`` (full label horizon), with
``ts`` (= T, window end), ``equipment``, ``line``, the features of :mod:`qost_ml.features`, the
label ``y``, ``hours_to_failure`` (to the next labelled failure, for lead times) and ``eligible``
(unit not down at T). Plus ``meta.json`` with row counts and a content hash per table.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from qost_ml.features import (
    US,
    ShiftTimeline,
    UnitHistory,
    classify_stops,
    compute_features,
    to_grid,
    to_us,
)
from qost_ml.labels import down_at, label_rows
from qost_ml.spec import PdmSpec, assert_no_oracle
from twin_core.calendar import PlantCalendar
from twin_core.config import TwinConfig
from twin_core.events import FIRST_EXIT_RESULTS

ORACLE_FILE = "oracle_degradation.parquet"
META_COLUMNS = ("ts", "equipment", "type", "line")
LABEL_COLUMNS = ("y", "hours_to_failure", "eligible")
_HOUR_US = 3600 * US


@dataclass(frozen=True)
class RawDataset:
    start_us: int
    end_us: int
    meta: dict[str, Any]
    telemetry: pl.DataFrame
    downtime: pl.DataFrame
    units: pl.DataFrame


def load_raw(raw_dir: Path, spec: PdmSpec) -> RawDataset:
    """Read the raw ml-dataset (only the units and signals of PdM types)."""
    meta: dict[str, Any] = json.loads((raw_dir / "meta.json").read_text("utf-8"))
    start = datetime.fromisoformat(meta["from"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(meta["to"].replace("Z", "+00:00"))
    codes = list(spec.equipment)
    signals = sorted({s for t in spec.types.values() for s in t.signal_codes})
    assert_no_oracle(tuple(signals))
    telemetry = (
        pl.scan_parquet(raw_dir / "telemetry" / "*.parquet")
        .select(
            pl.col("ts").dt.epoch("us").alias("ts_us"),
            pl.col("equipment").cast(pl.String),
            pl.col("signal").cast(pl.String),
            pl.col("value"),
        )
        .filter(pl.col("equipment").is_in(codes), pl.col("signal").is_in(signals))
        .collect()
    )
    downtime = pl.read_parquet(raw_dir / "downtime.parquet").select(
        pl.col("start").dt.epoch("us").alias("start_us"),
        pl.col("end").dt.epoch("us").alias("end_us"),
        pl.col("equipment"),
        pl.col("state"),
        pl.col("reason"),
    )
    units = (
        pl.read_parquet(raw_dir / "units.parquet")
        .filter(pl.col("result").is_in(sorted(FIRST_EXIT_RESULTS)))
        .select(pl.col("ts").dt.epoch("us").alias("ts_us"), pl.col("line"))
    )
    return RawDataset(to_us(start), to_us(end), meta, telemetry, downtime, units)


def unit_histories(
    raw: RawDataset, spec: PdmSpec, units: Sequence[str] | None = None
) -> dict[str, UnitHistory]:
    """Full history of PdM units (default: all) on the feature grid (offline path)."""
    step = spec.grid_s * US
    grid0 = (raw.start_us // step) * step
    n_slots = (raw.end_us - grid0) // step + 1
    tele = raw.telemetry.partition_by(["equipment", "signal"], as_dict=True)
    stops = raw.downtime.partition_by("equipment", as_dict=True)
    exits = raw.units.partition_by("line", as_dict=True)
    out: dict[str, UnitHistory] = {}
    for code, (equipment_type, line) in spec.equipment.items():
        if units is not None and code not in units:
            continue
        values = {}
        for sig in spec.types[equipment_type].signals:
            frame = tele.get((code, sig.code))
            if frame is None:
                continue
            values[sig.code] = to_grid(
                frame["ts_us"].to_numpy(),
                frame["value"].to_numpy(),
                grid0_us=grid0,
                n_slots=n_slots,
                grid_s=spec.grid_s,
            )
        st = stops.get((code,))
        classified = classify_stops(
            st["start_us"].to_numpy() if st is not None else [],
            st["end_us"].to_numpy() if st is not None else [],
            st["state"].to_list() if st is not None else [],
            st["reason"].to_list() if st is not None else [],
            spec=spec,
            equipment_type=equipment_type,
        )
        ex = exits.get((line,))
        exits_us = np.sort(ex["ts_us"].to_numpy()) if ex is not None else np.zeros(0, np.int64)
        out[code] = UnitHistory(code, equipment_type, grid0, values, classified, exits_us)
    return out


def evaluation_times(raw: RawDataset, spec: PdmSpec) -> np.ndarray[Any, np.dtype[np.int64]]:
    """Window ends T: full feature windows behind, full label horizon ahead."""
    window = spec.window_s * US
    first = raw.start_us + spec.max_window_h * _HOUR_US
    first = -(-first // window) * window
    last = raw.end_us - round(spec.horizon_h * _HOUR_US)
    return np.arange(first, last + 1, window, dtype=np.int64)


def build_tables(
    raw: RawDataset,
    spec: PdmSpec,
    calendar: PlantCalendar,
    units: Sequence[str] | None = None,
) -> dict[str, pl.DataFrame]:
    """One feature/label table per PdM equipment type (``units``: only these units)."""
    at = evaluation_times(raw, spec)
    timeline = ShiftTimeline.build(
        calendar, spec.shift_codes, int(at[0]), int(at[-1]) + round(spec.horizon_h * _HOUR_US)
    )
    histories = unit_histories(raw, spec, units)
    parts: dict[str, list[pl.DataFrame]] = {}
    for code, hist in histories.items():
        cols = compute_features(hist, at, spec=spec, calendar=calendar, timeline=timeline)
        wear_start = hist.stops.start_us[hist.stops.wear_repair]
        y, hours = label_rows(at, wear_start, spec.horizon_h)
        eligible = ~down_at(at, hist.stops.start_us, hist.stops.end_us)
        frame = pl.DataFrame(
            {
                "ts": pl.Series(at, dtype=pl.Int64).cast(pl.Datetime("us", "UTC")),
                "equipment": [code] * len(at),
                "type": [hist.type] * len(at),
                "line": [spec.equipment[code][1]] * len(at),
                **{name: cols[name] for name in spec.feature_names(hist.type)},
                "y": y,
                "hours_to_failure": hours,
                "eligible": eligible,
            }
        )
        parts.setdefault(hist.type, []).append(frame)
    return {t: pl.concat(frames) for t, frames in parts.items()}


def frame_hash(frame: pl.DataFrame) -> str:
    """Content hash of a table (column names, order and values), independent of parquet bytes."""
    digest = hashlib.sha256()
    for name in frame.columns:
        col = frame[name]
        digest.update(name.encode())
        if col.dtype == pl.String:
            digest.update("\x00".join(col.to_list()).encode())
        elif col.dtype == pl.Boolean:
            digest.update(col.cast(pl.Int8).to_numpy().tobytes())
        elif isinstance(col.dtype, pl.Datetime):
            digest.update(col.dt.epoch("us").to_numpy().tobytes())
        else:
            digest.update(np.ascontiguousarray(col.to_numpy()).tobytes())
    return digest.hexdigest()


def build_dataset(
    cfg: TwinConfig, raw_dir: Path, out_dir: Path, units: Sequence[str] | None = None
) -> dict[str, Any]:
    """``make ml-dataset`` step 2: raw parquet → ``{type}.parquet`` + ``meta.json``."""
    spec = PdmSpec.from_config(cfg)
    raw = load_raw(raw_dir, spec)
    tables = build_tables(raw, spec, cfg.calendar, units)
    out_dir.mkdir(parents=True, exist_ok=True)
    meta: dict[str, Any] = {
        "raw": raw.meta,
        "grid_s": spec.grid_s,
        "window_s": spec.window_s,
        "horizon_h": spec.horizon_h,
        "tables": {},
    }
    for equipment_type, frame in tables.items():
        frame.write_parquet(out_dir / f"{equipment_type}.parquet")
        eligible = frame.filter(pl.col("eligible"))
        meta["tables"][equipment_type] = {
            "rows": len(frame),
            "eligible_rows": len(eligible),
            "positive_rows": int(eligible["y"].sum()),
            "units": sorted(set(frame["equipment"].to_list())),
            "features": spec.feature_names(equipment_type),
            "wear_reasons": sorted(spec.types[equipment_type].wear_reasons),
            "hash": frame_hash(frame),
        }
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), "utf-8")
    return meta
