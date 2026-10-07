"""ml-dataset mode (SPEC §6.1, §11.1): raw PdM data as parquet, no database.

Writes ``ml_dataset.months`` months from ``ml_dataset.from`` with seed ``ml_dataset.random_seed``
and telemetry every ``ml_dataset.telemetry_period_s`` into ``OUT``:

* ``telemetry/part-YYYY-MM.parquet`` — ts, equipment, type, signal, value
* ``states.parquet`` — state intervals of equipment and lines (start, end, state, reason)
* ``downtime.parquet`` — equipment stops with planned / microstop / wear flags (wear = the
  reason is one of the type's ``wear_reasons`` incl. ``chain_break``: the label of §11.1)
* ``units.parquet`` — line exits (cycles since maintenance, defects)
* ``oracle_degradation.parquet`` — hidden wear at each telemetry sample. NEVER a feature
  (FR-SIM-02); only for checking how strongly signals follow wear.
* ``meta.json`` — period, seed, config hash, row counts.

Windows, features and labels are built in ``ml/`` (stage M7).
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from qost_sim.model import PlantModel, Rec
from qost_sim.model.records import ORACLE
from twin_core.clock import format_utc
from twin_core.config import TwinConfig

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def add_months(instant: datetime, months: int) -> datetime:
    month0 = instant.month - 1 + months
    year = instant.year + month0 // 12
    return instant.replace(year=year, month=month0 % 12 + 1)


@dataclass
class _Columns:
    data: dict[str, list[Any]] = field(default_factory=dict)

    def add(self, **values: Any) -> None:
        for key, value in values.items():
            self.data.setdefault(key, []).append(value)

    def __len__(self) -> int:
        return len(next(iter(self.data.values()), []))


def _ts(series: list[int]) -> pl.Series:
    return pl.Series(series, dtype=pl.Int64).cast(pl.Datetime("us", "UTC"))


def _frame(cols: _Columns, ts_columns: Iterable[str]) -> pl.DataFrame:
    ts_set = set(ts_columns)
    series = [
        _ts(values).alias(name) if name in ts_set else pl.Series(name, values)
        for name, values in cols.data.items()
    ]
    return pl.DataFrame(series)


def config_hash(cfg: TwinConfig) -> str:
    digest = hashlib.sha256()
    for name in ("plant.yaml", "simulation.yaml", "reason_codes.yaml", "defect_codes.yaml"):
        digest.update((cfg.config_dir / name).read_bytes())
    return digest.hexdigest()[:16]


def generate(
    cfg: TwinConfig,
    out_dir: Path,
    *,
    months: int | None = None,
    seed: int | None = None,
    start: datetime | None = None,
) -> dict[str, Any]:
    spec = cfg.simulation.ml_dataset
    start = start or spec.from_
    months = months or spec.months
    seed = spec.random_seed if seed is None else seed
    end = add_months(start, months)
    began = time.perf_counter()
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "telemetry").mkdir(exist_ok=True)
    for old in (out_dir / "telemetry").glob("part-*.parquet"):
        old.unlink()

    model = PlantModel(cfg, start=start, seed=seed, telemetry_period_s=spec.telemetry_period_s)
    t0_us = (model.t0 - _EPOCH) // timedelta(microseconds=1)
    unit_type = {code: u.type for code, u in model.units.items()}
    wear_reasons = {code: u.wear_reasons for code, u in model.units.items()}
    threshold = cfg.rules.thresholds.microstop_threshold_s
    planned = {code: r.planned for code, r in cfg.reasons.items()}

    def us(t: float) -> int:
        return t0_us + round(t * 1e6)

    states = _Columns()
    downtime = _Columns()
    units = _Columns()
    oracle = _Columns()
    rows = {"telemetry": 0}
    open_state: dict[str, tuple[float, str, str, str | None]] = {}

    def close(entity: str, t_end: float) -> None:
        prev = open_state.get(entity)
        if prev is None:
            return
        t_start, etype, state, reason = prev
        states.add(
            start=us(t_start),
            end=us(t_end),
            entity_type=etype,
            entity=entity,
            state=state,
            reason=reason,
        )
        if etype == "equipment" and state.startswith("DOWN_"):
            duration = t_end - t_start
            downtime.add(
                start=us(t_start),
                end=us(t_end),
                equipment=entity,
                type=unit_type[entity],
                state=state,
                reason=reason,
                duration_s=duration,
                planned=bool(reason and planned.get(reason, False)),
                microstop=state == "DOWN_UNPLANNED" and duration < threshold,
                wear=bool(reason and reason in wear_reasons[entity]),
            )

    def flush_month(tele: _Columns, month: date) -> None:
        if len(tele):
            frame = _frame(tele, ["ts"]).with_columns(
                pl.col("equipment").cast(pl.Categorical),
                pl.col("type").cast(pl.Categorical),
                pl.col("signal").cast(pl.Categorical),
            )
            frame.write_parquet(out_dir / "telemetry" / f"part-{month:%Y-%m}.parquet")
            rows["telemetry"] += len(tele)

    tele = _Columns()
    month = model.at(0).astimezone(model.tz).date().replace(day=1)
    cursor = start
    while cursor < end:
        cursor = min(cursor + timedelta(days=1), end)
        model.run_until_time(cursor)
        recs: list[Rec] = model.drain()
        for rec in recs:
            kind = rec.kind
            if kind == "telemetry":
                tele.add(
                    ts=us(rec.t),
                    equipment=rec.entity,
                    type=unit_type[rec.entity],
                    signal=rec.data["signal"],
                    value=float(rec.data["value"]),
                )
            elif kind == "state":
                close(rec.entity, rec.t)
                open_state[rec.entity] = (
                    rec.t,
                    rec.entity_type,
                    rec.data["state"],
                    rec.data.get("reason_code"),
                )
            elif kind == "unit":
                units.add(
                    ts=us(rec.t),
                    line=rec.entity,
                    body_id=rec.data["body_id"],
                    product=rec.data["product"],
                    result=rec.data["result"],
                    defect_code=rec.data.get("defect_code"),
                )
            elif kind == ORACLE:
                oracle.add(ts=us(rec.t), equipment=rec.entity, degradation=rec.data["degradation"])
        local_month = model.now.astimezone(model.tz).date().replace(day=1)
        if local_month != month:
            flush_month(tele, month)
            tele = _Columns()
            month = local_month
    flush_month(tele, month)
    for entity in list(open_state):
        close(entity, model.env.now)

    _frame(states, ["start", "end"]).write_parquet(out_dir / "states.parquet")
    _frame(downtime, ["start", "end"]).write_parquet(out_dir / "downtime.parquet")
    _frame(units, ["ts"]).write_parquet(out_dir / "units.parquet")
    _frame(oracle, ["ts"]).write_parquet(out_dir / "oracle_degradation.parquet")
    meta = {
        "from": format_utc(start),
        "to": format_utc(end),
        "months": months,
        "seed": seed,
        "telemetry_period_s": spec.telemetry_period_s,
        "config_hash": config_hash(cfg),
        "rows": {
            "telemetry": rows["telemetry"],
            "states": len(states),
            "downtime": len(downtime),
            "units": len(units),
            "oracle_degradation": len(oracle),
        },
        "wear_reasons": {t: sorted(r) for t, r in _type_wear(model).items()},
        "oracle_note": "oracle_degradation is the simulator's hidden state: never a feature",
        "seconds": round(time.perf_counter() - began, 1),
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), "utf-8")
    return meta


def _type_wear(model: PlantModel) -> dict[str, frozenset[str]]:
    result: dict[str, frozenset[str]] = {}
    for unit in model.units.values():
        if unit.wear_reasons:
            result[unit.type] = unit.wear_reasons
    return result
