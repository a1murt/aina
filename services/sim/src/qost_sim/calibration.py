"""Calibration report of the virtual plant against ``simulation.yaml: calibration_targets``.

FR-SIM-03: N working days with a fixed seed after a warm-up -> steady throughput (QC exit),
defect rates per area, unplanned downtime per area and working day, and a bottleneck proxy
(the line that is least often starved or blocked inside working shifts).

``python -m qost_sim calibrate`` prints the report; ``tests/sim/test_calibration.py`` checks it.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from qost_sim.model import PlantModel, Rec
from twin_core.config import TwinConfig
from twin_core.domain import EquipmentState
from twin_core.events import FINISHED_RESULTS

FIRST_EXITS = frozenset({"pass", "defect", "scrap"})
REJECTS = frozenset({"defect", "scrap"})
FG_RESULTS = FINISHED_RESULTS


@dataclass
class CalibrationReport:
    seed: int
    window: tuple[datetime, datetime]
    working_days: int
    working_shifts: int
    throughput_by_shift: list[int]
    defect_rate: dict[str, float]
    unplanned_min_per_day: dict[str, float]
    flow_loss_share: dict[str, float]
    bottleneck: str
    problems: list[str] = field(default_factory=list)

    @property
    def throughput_per_shift(self) -> float:
        return sum(self.throughput_by_shift) / max(1, self.working_shifts)

    def render(self) -> str:
        lines = [
            f"seed {self.seed}, window {self.window[0]:%Y-%m-%d} .. {self.window[1]:%Y-%m-%d} "
            f"({self.working_days} working days, {self.working_shifts} shifts)",
            f"throughput per shift (QC exit): {self.throughput_per_shift:.1f} "
            f"(min {min(self.throughput_by_shift, default=0)}, "
            f"max {max(self.throughput_by_shift, default=0)})",
            "defect rate: " + ", ".join(f"{a} {r * 100:.2f}%" for a, r in self.defect_rate.items()),
            "unplanned downtime, min/area/day: "
            + ", ".join(f"{a} {m:.1f}" for a, m in self.unplanned_min_per_day.items()),
            "starved+blocked share: "
            + ", ".join(f"{ln} {s * 100:.1f}%" for ln, s in self.flow_loss_share.items()),
            f"bottleneck (least starved/blocked): {self.bottleneck}",
        ]
        lines.extend(f"OUT OF TARGET: {p}" for p in self.problems)
        return "\n".join(lines)


def _overlap(a0: float, a1: float, spans: Sequence[tuple[float, float]]) -> float:
    total = 0.0
    for s0, s1 in spans:
        lo, hi = max(a0, s0), min(a1, s1)
        if hi > lo:
            total += hi - lo
    return total


def _intervals(
    records: Iterable[Rec], entity_type: str, end: float
) -> dict[str, list[tuple[float, float, str, str | None]]]:
    """State intervals (start, end, state, reason) per entity from state records."""
    open_: dict[str, tuple[float, str, str | None]] = {}
    out: dict[str, list[tuple[float, float, str, str | None]]] = defaultdict(list)
    for rec in records:
        if rec.kind != "state" or rec.entity_type != entity_type:
            continue
        prev = open_.get(rec.entity)
        if prev is not None:
            out[rec.entity].append((prev[0], rec.t, prev[1], prev[2]))
        open_[rec.entity] = (rec.t, rec.data["state"], rec.data.get("reason_code"))
    for entity, (start, state, reason) in open_.items():
        out[entity].append((start, end, state, reason))
    return out


def analyze(
    model: PlantModel, records: Sequence[Rec], window: tuple[datetime, datetime]
) -> CalibrationReport:
    cfg = model.cfg
    w0, w1 = model.sec(window[0]), model.sec(window[1])
    shifts = cfg.calendar.shifts_between(window[0], window[1], working_only=True)
    spans = [(model.sec(s.start), model.sec(s.end)) for s in shifts]
    days = len({s.shift_date for s in shifts})
    last_line = cfg.flow_lines[-1]

    per_shift = [0] * len(spans)
    exits: dict[str, int] = defaultdict(int)
    rejects: dict[str, int] = defaultdict(int)
    for rec in records:
        if rec.kind != "unit" or not w0 <= rec.t < w1:
            continue
        result = rec.data["result"]
        line = rec.data["line"]
        if result in FIRST_EXITS:
            exits[line] += 1
            if result in REJECTS:
                rejects[line] += 1
        if line == last_line and result in FG_RESULTS:
            for i, (s0, s1) in enumerate(spans):
                if s0 <= rec.t < s1:
                    per_shift[i] += 1
                    break

    defect_rate = {
        cfg.area_of_line(line).code: rejects[line] / exits[line]
        for line in cfg.flow_lines
        if exits[line]
    }

    threshold = cfg.rules.thresholds.microstop_threshold_s
    unplanned: dict[str, float] = {cfg.area_of_line(ln).code: 0.0 for ln in cfg.flow_lines}
    for code, items in _intervals(records, "equipment", model.env.now).items():
        area = model.area_of_unit(code)
        for start, end, state, _reason in items:
            if state == EquipmentState.DOWN_UNPLANNED.value and end - start >= threshold:
                unplanned[area] += _overlap(start, end, spans) / 60.0
    unplanned_per_day = {a: m / max(1, days) for a, m in unplanned.items()}

    shift_time = sum(s1 - s0 for s0, s1 in spans)
    flow_loss: dict[str, float] = {}
    line_intervals = _intervals(records, "line", model.env.now)
    flow_states = {EquipmentState.STARVED.value, EquipmentState.BLOCKED.value}
    for line in cfg.flow_lines:
        lost = sum(
            _overlap(s, e, spans)
            for s, e, st, _ in line_intervals.get(line, [])
            if st in flow_states
        )
        flow_loss[line] = lost / shift_time if shift_time else 0.0
    bottleneck = min(cfg.flow_lines, key=lambda ln: flow_loss[ln])

    report = CalibrationReport(
        seed=model.seed,
        window=window,
        working_days=days,
        working_shifts=len(spans),
        throughput_by_shift=per_shift,
        defect_rate=defect_rate,
        unplanned_min_per_day=unplanned_per_day,
        flow_loss_share=flow_loss,
        bottleneck=bottleneck,
    )
    report.problems = check_targets(cfg, report)
    return report


def check_targets(cfg: TwinConfig, report: CalibrationReport) -> list[str]:
    targets = cfg.simulation.calibration_targets
    problems: list[str] = []
    tp = report.throughput_per_shift
    if not targets.throughput_per_shift.min <= tp <= targets.throughput_per_shift.max:
        problems.append(
            f"throughput {tp:.1f}/shift not in "
            f"[{targets.throughput_per_shift.min}, {targets.throughput_per_shift.max}]"
        )
    for area, (low, high) in targets.defect_rate.items():
        rate = report.defect_rate.get(area, 0.0)
        if not low <= rate <= high:
            problems.append(f"defect rate {area} {rate:.4f} not in [{low}, {high}]")
    rng = targets.unplanned_downtime_min_per_area_day
    for area in targets.defect_rate:
        minutes = report.unplanned_min_per_day.get(area, 0.0)
        if not rng.min <= minutes <= rng.max:
            problems.append(
                f"unplanned downtime {area} {minutes:.1f} min/day not in [{rng.min}, {rng.max}]"
            )
    for area, minutes in report.unplanned_min_per_day.items():
        if area not in targets.defect_rate and minutes > rng.max:
            problems.append(f"unplanned downtime {area} {minutes:.1f} min/day above {rng.max}")
    if report.bottleneck != targets.expected_bottleneck:
        problems.append(f"bottleneck {report.bottleneck}, expected {targets.expected_bottleneck}")
    return problems


def calibration_window(
    cfg: TwinConfig, *, warmup_days: int, days: int
) -> tuple[datetime, datetime, datetime]:
    """(model start, window start, window end): local midnights, counted in working days."""
    tz = cfg.timezone
    start_day = cfg.simulation.clock.backfill_from.astimezone(tz).date()
    day = start_day
    working: list[datetime] = []
    while len(working) < warmup_days + days:
        if cfg.calendar.is_working_day(day):
            working.append(datetime.combine(day, time(0), tzinfo=tz))
        day += timedelta(days=1)
    start = datetime.combine(start_day, time(0), tzinfo=tz)
    w0 = working[warmup_days]
    w1 = working[-1] + timedelta(days=1)
    return start, w0, w1


def run_calibration(
    cfg: TwinConfig, *, seed: int | None = None, warmup_days: int = 5, days: int = 20
) -> tuple[CalibrationReport, PlantModel, list[Rec]]:
    start, w0, w1 = calibration_window(cfg, warmup_days=warmup_days, days=days)
    model = PlantModel(cfg, start=start, seed=seed)
    model.run_until_time(w1)
    records = model.drain()
    return analyze(model, records, (w0, w1)), model, records
