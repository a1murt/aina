"""The engine core (SPEC §9): states, downtime, KPIs, buffers, bottleneck, DQ and alert rules.

Pure and synchronous: :meth:`EngineCore.apply` takes one event, :meth:`EngineCore.advance_to`
fires timers up to a plant time; both append database effects and live messages that a driver
drains (:meth:`EngineCore.drain`). The live driver feeds the Redis stream and the plant clock,
the replay driver feeds stored events — the logic is the same (one code path).
"""

from __future__ import annotations

import math
from collections import OrderedDict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime
from typing import Any, Literal

import structlog

from qost_engine.core import serial
from qost_engine.core.effects import (
    AlertEscalate,
    AlertUpsert,
    AuditRow,
    BottleneckRow,
    DowntimeRow,
    DqUpsert,
    Effect,
    KpiShiftRow,
    LiveMsg,
    PredictionRow,
    ReclassifyRequest,
    StateInterval,
)
from qost_engine.core.pdm import PdmTick
from qost_engine.core.state import (
    BufferSt,
    Cond,
    CoreState,
    Entity,
    LineAcc,
    OpenAlert,
    Point,
    ShiftAcc,
    Span,
    SpcPoint,
    Stop,
)
from twin_core.alert_text import alert_message_ru, alert_title_ru
from twin_core.bottleneck import ShiftingBottleneck, active_periods, shifting_bottleneck
from twin_core.calendar import ShiftInstance
from twin_core.config import FALLBACK_REASON_CODE, Thresholds, TwinConfig
from twin_core.domain import EquipmentState
from twin_core.dq import (
    DqIssue,
    check_downtime_reconciliation,
    check_effectiveness,
    check_flow_balance_live,
)
from twin_core.events import (
    FIRST_EXIT_RESULTS,
    AlarmEvent,
    AnyEvent,
    BufferLevelEvent,
    CkdEvent,
    OperatorEvent,
    StateEvent,
    UnitEvent,
)
from twin_core.kpi import (
    ShiftKpi,
    StateSpan,
    StopSpan,
    aggregate_kpi,
    compute_shift_kpi,
    is_microstop,
    mtbf_h,
    mttr_min,
    pri_seconds,
    rty,
    shift_time_model,
    stop_impact,
)
from twin_core.rules import (
    AL_DOWNTIME_LIMIT,
    AL_LIMIT,
    AL_OEE_BELOW,
    AL_OEE_NEAR,
    AL_PDM,
    Alert,
    AlertEvaluator,
)
from twin_core.spc import Subgroup, p_chart
from twin_core.states import UnitCondition, derive_line_state

log = structlog.get_logger("qost_engine.core")

Mode = Literal["live", "replay"]
DOWN = frozenset({"DOWN_UNPLANNED", "DOWN_PLANNED"})
STOP_STATES = DOWN | {"CHANGEOVER"}
HISTORY_KEEP_S = 36 * 3600.0
"""Recent state intervals kept per entity (current + 2 closed shifts + the bottleneck window)."""
BUFFER_KEEP_S = 2 * 3600.0
CLOSED_SHIFTS_KEPT = 2
DEDUP_IDS = 200_000


def to_dt(sec: float) -> datetime:
    return datetime.fromtimestamp(sec, tz=UTC)


def to_sec(value: datetime) -> float:
    return value.timestamp()


def iso(sec: float) -> str:
    return to_dt(sec).isoformat().replace("+00:00", "Z")


class EngineCore:
    """Stream processor over :mod:`twin_core.events` (see the module docstring)."""

    def __init__(
        self,
        cfg: TwinConfig,
        *,
        mode: Mode = "live",
        thresholds: Thresholds | None = None,
        line_state_source: Literal["events", "derive"] = "events",
        resolve_history_alerts: bool = True,
        state: CoreState | None = None,
    ) -> None:
        self.cfg = cfg
        self.mode: Mode = mode
        self.params = cfg.rules.engine
        self.t = thresholds or cfg.rules.thresholds
        self.evaluator = AlertEvaluator(cfg.rules, self.t)
        self.calendar = cfg.calendar
        self.line_state_source = line_state_source
        self.resolve_history_alerts = resolve_history_alerts
        self.threshold_s = self.t.microstop_threshold_s
        self.close_grace_s = 0.0
        """Plant seconds to wait after a shift end before closing it (live: wall budget x speed)."""
        self.site = cfg.plant.site.code
        self.flow: tuple[str, ...] = cfg.flow_lines
        self.line_of: dict[str, str] = {
            code: cfg.line_of_equipment(code).code for code in cfg.equipment
        }
        self.area_of: dict[str, str] = {line: cfg.area_of_line(line).code for line in cfg.lines}
        self.ict = {line: float(cfg.lines[line].ict_seconds) for line in cfg.lines}
        self.cycle_factor = {p: float(prod.cycle_factor) for p, prod in cfg.products.items()}
        self.buffer_in = {b.to_line: b.code for b in cfg.plant.buffers}
        self.buffer_out = {b.from_line: b.code for b in cfg.plant.buffers}
        self.st = state or CoreState()
        for code in cfg.equipment:
            self.st.entities.setdefault(code, Entity(code, "equipment"))
        for line in cfg.lines:
            self.st.entities.setdefault(line, Entity(line, "line"))
        for b in cfg.plant.buffers:
            self.st.buffers.setdefault(b.code, BufferSt(capacity=b.capacity))
            self.st.buffer_conds.setdefault(b.code, Cond())
        for p in cfg.products:
            self.st.ckd_conds.setdefault(p, Cond())
        self.effects: list[Effect] = []
        self.live: list[LiveMsg] = []
        self.views: dict[str, dict[str, dict[str, Any]]] = {
            "equipment": {},
            "lines": {},
            "buffers": {},
            "areas": {},
        }
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._shift_cache: tuple[float, float, ShiftInstance | None] | None = None
        self.kpi_dirty = True
        self._plan_cache: dict[str, float | None] = {}

    # ================================================================== public API

    def drain(self) -> tuple[list[Effect], list[LiveMsg]]:
        effects, live = self.effects, self.live
        self.effects, self.live = [], []
        return effects, live

    def snapshot(self) -> dict[str, Any]:
        data: dict[str, Any] = serial.dump(self.st)
        return data

    @classmethod
    def restore(cls, cfg: TwinConfig, data: Mapping[str, Any], **kwargs: Any) -> EngineCore:
        state = serial.load(CoreState, dict(data))
        return cls(cfg, state=state, **kwargs)

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "events": self.st.events,
            "late_events": self.st.late_events,
            "dropped_units": self.st.dropped_units,
            "open_alerts": len(self.st.alerts),
            "open_stops": sum(1 for s in self.st.stops.values() if s.end is None),
            "watermark": iso(self.st.watermark) if self.st.watermark else None,
        }

    def apply(self, event: AnyEvent) -> None:
        """Apply one event (in stream order; per entity in time order)."""
        t = to_sec(event.ts)
        self.advance_to(event.ts)
        self.st.events += 1
        if event.quality == "bad":
            return
        if isinstance(event, StateEvent):
            self._on_state(event, t)
        elif isinstance(event, UnitEvent):
            self._on_unit(event, t)
        elif isinstance(event, BufferLevelEvent):
            self._on_buffer(event, t)
        elif isinstance(event, CkdEvent):
            self._on_ckd(event, t)
        elif isinstance(event, AlarmEvent):
            self._on_alarm(event, t)
        elif isinstance(event, OperatorEvent):
            self._on_operator(event, t)
        # telemetry and defect records are facts for the collector; nothing to derive in M3

    def advance_to(self, now: datetime) -> None:
        """Fire everything due up to plant time ``now`` (shift close, timers, escalations)."""
        t = to_sec(now)
        if self.st.watermark is not None and t < self.st.watermark:
            t = self.st.watermark
        self.st.watermark = t
        self._ensure_shifts(t)
        for acc in sorted(self.st.shifts.values(), key=lambda a: a.start):
            if not acc.closed and t >= acc.end + self.close_grace_s:
                self._close_shift(acc, t)
        self._timers(t)

    # ================================================================== helpers

    def _late(self, entity: str, kind: str, t: float) -> bool:
        key = f"{entity}|{kind}"
        last = self.st.last_ts.get(key)
        if last is not None and t < last:
            self.st.late_events += 1
            return True
        self.st.last_ts[key] = t
        return False

    def _dup(self, event_id: str) -> bool:
        if event_id in self._seen:
            return True
        self._seen[event_id] = None
        if len(self._seen) > DEDUP_IDS:
            self._seen.popitem(last=False)
        return False

    def _shift_at(self, t: float) -> ShiftInstance | None:
        cache = self._shift_cache
        if cache is not None and cache[0] <= t < cache[1]:
            return cache[2]
        moment = to_dt(t)
        shift = self.calendar.shift_at(moment, working_only=True)
        if shift is not None:
            self._shift_cache = (to_sec(shift.start), to_sec(shift.end), shift)
        else:
            nxt = self.calendar.next_shift_change(moment)
            hi = to_sec(nxt) if nxt is not None else t + 3600.0
            self._shift_cache = (t, hi, None)
        return shift

    def _ensure_shifts(self, t: float) -> None:
        current = self._shift_at(t)
        if current is None:
            return
        key = f"{current.shift_date.isoformat()}/{current.code}"
        if key in self.st.shifts:
            return
        # also open working shifts skipped since the last one (no events at all in between)
        lo = self.st.last_shift_end
        if lo is not None and lo < to_sec(current.start):
            for sh in self.calendar.shifts_between(to_dt(lo), current.start):
                k = f"{sh.shift_date.isoformat()}/{sh.code}"
                if k not in self.st.shifts and to_sec(sh.start) >= lo:
                    self._open_shift(sh, t)
        self._open_shift(current, t)

    def _open_shift(self, sh: ShiftInstance, t: float) -> ShiftAcc:
        acc = ShiftAcc(
            date=sh.shift_date.isoformat(),
            code=sh.code,
            start=to_sec(sh.start),
            end=to_sec(sh.end),
            lines={line: LineAcc() for line in self.flow},
            buffer_start={
                code: (b.level if b.level is not None else 0) for code, b in self.st.buffers.items()
            },
            # joined more than a minute late without knowing the line states (fresh engine)
            partial=t > to_sec(sh.start) + 60.0
            and any(self.st.entities[line].state is None for line in self.flow),
        )
        self.st.shifts[acc.key] = acc
        self.st.last_shift_end = max(self.st.last_shift_end or acc.end, acc.end)
        self._clock_msg(acc.start, acc, "shift_opened")
        return acc

    def _acc_for(self, t: float) -> ShiftAcc | None:
        shift = self._shift_at(t)
        if shift is None:
            return None
        return self.st.shifts.get(f"{shift.shift_date.isoformat()}/{shift.code}")

    def _current_acc(self, t: float) -> ShiftAcc | None:
        acc = self._acc_for(t)
        return acc if acc is not None and not acc.closed else None

    def _push_history(self, ent: Entity, span: Span, t: float) -> None:
        ent.history.append(span)
        while (
            ent.history
            and ent.history[0].end is not None
            and ent.history[0].end < t - HISTORY_KEEP_S
        ):
            ent.history.popleft()

    def _clock_msg(self, t: float, acc: ShiftAcc, event: str) -> None:
        if self.mode != "live":
            return
        self.live.append(
            LiveMsg(
                "clock",
                to_dt(t),
                {
                    "event": event,
                    "shift": {
                        "date": acc.date,
                        "code": acc.code,
                        "start": iso(acc.start),
                        "end": iso(acc.end),
                    },
                },
            )
        )

    # ================================================================== states (FR-ENG-01)

    def _on_state(self, ev: StateEvent, t: float) -> None:
        ent = self.st.entities.get(ev.entity)
        if ent is None:
            return
        if ent.kind == "line" and self.line_state_source == "derive":
            return
        if self._late(ev.entity, "state", t):
            return
        data = ev.data
        state = str(data.state)
        if ent.kind == "equipment":
            reason = (
                self._equipment_reason(data.reason_code, data.alarm_code, t)
                if state in STOP_STATES
                else None
            )
        else:
            reason = self._line_reason(ev.entity, state, data.reason_code, t)
        if ent.state == state and ent.since is not None:
            if ent.reason == reason:
                return
            if t - ent.since < 1e-6 or (
                data.alarm_code is not None and data.alarm_code == ent.alarm_code
            ):
                # same instant (late AlarmCode join) or a re-statement: reason update in place
                self._update_reason(ent, reason, data.alarm_code, t)
                return
        ent.alarm_code = data.alarm_code
        self._change_state(ent, state, reason, t, source=ev.source)
        if ent.kind == "equipment":
            self._fix_line_reason(ent, t)
            if self.line_state_source == "derive":
                self._derive_line(self.line_of[ent.code], t)

    def _equipment_reason(self, reason_code: str | None, alarm_code: str | None, t: float) -> str:
        reasons = self.cfg.reasons
        if reason_code and reason_code in reasons:
            return reason_code
        if alarm_code:
            code = self.cfg.aliases.reasons.resolve(alarm_code)
            if code is not None:
                return code
            day = self.calendar.local_date(to_dt(t)).isoformat()
            if self.st.unknown_codes.get(alarm_code) != day:
                self.st.unknown_codes[alarm_code] = day
                self._dq(
                    DqIssue(
                        "DQ-07",
                        "warning",
                        "alarm_code",
                        date.fromisoformat(day),
                        {"kind": "alarm_code", "value": alarm_code, "suggestion": None},
                    ),
                    t,
                    f"DQ-07|alarm_code|{alarm_code}|{day}",
                )
        return FALLBACK_REASON_CODE

    def _line_reason(self, line: str, state: str, reason_code: str | None, t: float) -> str | None:
        if state not in STOP_STATES and state not in ("STARVED", "BLOCKED"):
            return None
        if reason_code and reason_code in self.cfg.reasons:
            return reason_code
        if state in DOWN:
            units = [
                self.st.entities[eq.code]
                for eq in self.cfg.equipment_of_line(line)
                if eq.criticality == "A"
            ]
            down = [u for u in units if u.state == state and u.since is not None]
            if down:
                return min(down, key=lambda u: u.since or 0.0).reason or FALLBACK_REASON_CODE
            return FALLBACK_REASON_CODE
        if (
            state == "STARVED"
            and line == self.flow[0]
            and self.st.kits
            and all(k <= 0 for k in self.st.kits.values())
        ):
            return "MAT-SHORTAGE" if "MAT-SHORTAGE" in self.cfg.reasons else None
        return None

    def _fix_line_reason(self, ent: Entity, t: float) -> None:
        """An equipment stop that arrives after its line's stop (same instant) names the reason."""
        if ent.state not in DOWN:
            return
        line = self.st.entities[self.line_of[ent.code]]
        eq = self.cfg.equipment[ent.code]
        if (
            eq.criticality == "A"
            and line.state == ent.state
            and line.reason == FALLBACK_REASON_CODE
            and line.reason_source == "auto"
            and line.since is not None
            and abs(line.since - t) < 1.0
        ):
            self._update_reason(line, ent.reason, None, t)

    def _update_reason(
        self, ent: Entity, reason: str | None, alarm_code: str | None, t: float
    ) -> None:
        ent.reason = reason
        if alarm_code is not None:
            ent.alarm_code = alarm_code
        if ent.history and ent.history[-1].end is None:
            ent.history[-1].reason = reason
        assert ent.since is not None
        self._interval_effect(ent, ent.since, None)
        stop = self.st.stops.get(ent.stop) if ent.stop else None
        if stop is not None and stop.reason_source == "auto" and reason is not None:
            stop.reason = reason
            stop.planned = self._planned(reason, stop.state)
            self._stop_effect(stop)
            if stop.alert and stop.alert in self.st.alerts:
                self._update_s1(stop, t)
        self._state_msg(ent, t)

    def _change_state(
        self, ent: Entity, state: str, reason: str | None, t: float, *, source: str
    ) -> None:
        if ent.state is not None and ent.since is not None:
            if ent.history and ent.history[-1].end is None:
                ent.history[-1].end = t
            self._interval_effect(ent, ent.since, t)
            if ent.stop is not None:
                self._close_stop(ent, t)
        ent.state, ent.since, ent.reason = state, t, reason
        ent.reason_source = "auto"
        ent.source = source
        self._push_history(ent, Span(t, None, state, reason), t)
        self._interval_effect(ent, t, None)
        if state in STOP_STATES:
            self._open_stop(ent, state, reason or FALLBACK_REASON_CODE, t)
        self.kpi_dirty = True
        self._state_msg(ent, t)

    def _interval_effect(self, ent: Entity, start: float, end: float | None) -> None:
        span = next((s for s in reversed(ent.history) if s.start == start), None)
        state = span.state if span else (ent.state or "")
        reason = span.reason if span else ent.reason
        self.effects.append(
            StateInterval(
                entity=ent.code,
                entity_type=ent.kind,
                start=to_dt(start),
                end=to_dt(end) if end is not None else None,
                state=state,
                reason_code=reason,
                source=ent.source,
            )
        )

    def _derive_line(self, line: str, t: float) -> None:
        shift = self._shift_at(t)
        units = [
            UnitCondition(
                criticality=eq.criticality,
                degraded_capacity=eq.degraded_capacity,
                state=EquipmentState(self.st.entities[eq.code].state or "RUNNING"),
                reason=self.st.entities[eq.code].reason,
                since=self.st.entities[eq.code].since or 0.0,
            )
            for eq in self.cfg.equipment_of_line(line)
        ]
        flow = None
        reason = None
        inbuf = self.buffer_in.get(line)
        outbuf = self.buffer_out.get(line)
        if inbuf is not None and (self.st.buffers[inbuf].level or 0) <= 0:
            flow = "STARVED"
        elif inbuf is None and self.st.kits and all(k <= 0 for k in self.st.kits.values()):
            flow, reason = "STARVED", "MAT-SHORTAGE"
        elif outbuf is not None:
            b = self.st.buffers[outbuf]
            if b.level is not None and b.level >= b.capacity:
                flow = "BLOCKED"
        status = derive_line_state(
            in_shift=shift is not None,
            units=units,
            flow=EquipmentState(flow) if flow else None,
            flow_reason=reason,
        )
        ent = self.st.entities[line]
        state = str(status.state)
        if ent.state == state and ent.reason == status.reason:
            return
        self._change_state(ent, state, status.reason, t, source="engine")

    # ================================================================== downtime (FR-ENG-02/03/04)

    def _planned(self, reason: str, state: str) -> bool:
        if reason != FALLBACK_REASON_CODE and reason in self.cfg.reasons:
            return bool(self.cfg.reasons[reason].planned)
        return state == "DOWN_PLANNED"

    def _open_stop(self, ent: Entity, state: str, reason: str, t: float) -> None:
        line = ent.code if ent.kind == "line" else self.line_of[ent.code]
        shift = self._shift_at(t)
        stop = Stop(
            entity=ent.code,
            line=line,
            start=t,
            end=None,
            state=state,
            reason=reason,
            reason_source="auto",
            planned=self._planned(reason, state),
            shift_date=shift.shift_date.isoformat() if shift else None,
            shift_code=shift.code if shift else None,
        )
        self.st.stops[stop.key] = stop
        ent.stop = stop.key
        self._stop_effect(stop)
        if ent.kind == "equipment" and state == "DOWN_UNPLANNED":
            crit = self.cfg.equipment[ent.code].criticality
            if crit == "A":
                self._raise_s1(stop, t)
        self._downtime_msg(stop, t)

    def _close_stop(self, ent: Entity, t: float) -> None:
        stop = self.st.stops.get(ent.stop or "")
        ent.stop = None
        if stop is None:
            return
        stop.end = t
        if (
            stop.reason == FALLBACK_REASON_CODE
            and stop.reason_source == "auto"
            and stop.duration(t) >= self.params.unclassified_after_min * 60.0
        ):
            stop.flagged = True
        self._stop_effect(stop)
        if stop.alert is not None:
            self._update_s1(stop, t, ended=True)
        if ent.kind == "equipment" and self._counts_for_d1(stop):
            self._d1_add(stop)
        if self.mode == "live":
            self.live.append(
                LiveMsg(
                    "state",
                    to_dt(t),
                    {"downtime_closed": self._stop_view(stop, t)},
                    ("downtime_open", stop.entity, None),
                )
            )
        self._prune_stops(t)

    def _counts_for_d1(self, stop: Stop) -> bool:
        if stop.planned or stop.state != "DOWN_UNPLANNED":
            return False
        eq = self.cfg.equipment.get(stop.entity)
        if eq is None or eq.criticality != "A":
            return False
        return stop.end is None or not is_microstop(stop.duration(stop.end), self.threshold_s)

    def _stop_effect(self, stop: Stop) -> None:
        duration = None if stop.end is None else stop.end - stop.start
        microstop = (
            duration is not None
            and stop.state == "DOWN_UNPLANNED"
            and not stop.planned
            and is_microstop(duration, self.threshold_s)
        )
        self.effects.append(
            DowntimeRow(
                entity=stop.entity,
                line=stop.line,
                start=to_dt(stop.start),
                end=to_dt(stop.end) if stop.end is not None else None,
                duration_s=duration,
                planned=stop.planned,
                microstop=microstop,
                reason_code=stop.reason,
                reason_source=stop.reason_source,
                shift_date=date.fromisoformat(stop.shift_date) if stop.shift_date else None,
                shift_code=stop.shift_code,
                comment=stop.comment,
                classified_ts=to_dt(stop.classified_ts) if stop.classified_ts else None,
            )
        )

    def _prune_stops(self, t: float) -> None:
        horizon = t - HISTORY_KEEP_S
        old = [k for k, s in self.st.stops.items() if s.end is not None and s.end < horizon]
        for k in old:
            del self.st.stops[k]

    def _stop_view(self, stop: Stop, t: float) -> dict[str, Any]:
        return {
            "entity": stop.entity,
            "line": stop.line,
            "start_ts": iso(stop.start),
            "end_ts": iso(stop.end) if stop.end is not None else None,
            "duration_min": round(stop.duration(t) / 60.0, 2),
            "state": stop.state,
            "reason_code": stop.reason,
            "reason_source": stop.reason_source,
            "planned": stop.planned,
            "needs_classification": stop.flagged,
        }

    def _downtime_msg(self, stop: Stop, t: float) -> None:
        if self.mode != "live":
            return
        view = self._stop_view(stop, t)
        self.live.append(
            LiveMsg("state", to_dt(t), {"downtime": view}, ("downtime_open", stop.entity, view))
        )

    def _on_operator(self, ev: OperatorEvent, t: float) -> None:
        if ev.data.action != "classify_downtime":
            return
        payload = ev.data.payload
        entity = str(payload.get("entity", ""))
        reason = str(payload.get("reason_code", ""))
        start_raw = payload.get("start_ts")
        if reason not in self.cfg.reasons or not isinstance(start_raw, str):
            log.warning("classify_rejected", payload=payload)
            return
        start = to_sec(datetime.fromisoformat(start_raw.replace("Z", "+00:00")))
        comment = payload.get("comment")
        stop = self._find_stop(entity, start)
        if stop is None:
            # older than the retained shifts: the writer reclassifies the stored rows
            self.effects.append(
                ReclassifyRequest(
                    entity=entity,
                    start=to_dt(start),
                    reason_code=reason,
                    planned=self._planned(reason, "DOWN_UNPLANNED"),
                    comment=comment if isinstance(comment, str) else None,
                    user=ev.data.user,
                    ts=to_dt(t),
                )
            )
            return
        touched = [stop]
        if self.st.entities[entity].kind == "equipment":
            line_stop = self._find_stop(stop.line, stop.start)
            if line_stop is not None and line_stop.reason_source == "auto":
                touched.append(line_stop)
        for s in touched:
            s.reason = reason
            s.reason_source = "operator"
            s.planned = self._planned(reason, s.state)
            s.classified_ts = t
            s.flagged = False
            if isinstance(comment, str):
                s.comment = comment
            self._stop_effect(s)
            ent = self.st.entities[s.entity]
            if ent.stop == s.key:
                ent.reason = reason
                ent.reason_source = "operator"
                if ent.history and ent.history[-1].end is None:
                    ent.history[-1].reason = reason
                self._interval_effect(ent, s.start, None)
                self._state_msg(ent, t)
            if s.end is None:
                self._downtime_msg(s, t)
        # FR-KPI-04: closed shifts touched by the reclassified line stop get a new KPI version
        for acc in list(self.st.shifts.values()):
            if acc.closed and any(
                s.entity in self.cfg.lines and s.start < acc.end and (s.end or t) > acc.start
                for s in touched
            ):
                self._close_shift(acc, t, user=ev.data.user, reason="classify_downtime")
        self.kpi_dirty = True

    def _find_stop(self, entity: str, start: float) -> Stop | None:
        for stop in self.st.stops.values():
            if stop.entity == entity and abs(stop.start - start) < 1e-3:
                return stop
        return None

    # ================================================================== units and counters

    def _on_unit(self, ev: UnitEvent, t: float) -> None:
        if self._dup(ev.event_id):
            return
        data = ev.data
        acc = self._acc_for(t)
        la = acc.lines.get(data.line) if acc is not None else None
        if acc is None or la is None:
            self.st.dropped_units += 1
            return
        result = data.result
        if result in FIRST_EXIT_RESULTS:
            pri = pri_seconds(self.ict[data.line], self.cycle_factor.get(data.product, 1.0))
            la.pq += 1
            la.pri_produced_s += pri
            if result == "pass":
                la.gq += 1
                la.pri_good_s += pri
        if result in ("pass", "rework_pass"):
            la.out_to_next += 1
        if acc.closed:
            self.st.late_events += 1
            self._close_shift(acc, t, reason="late_event")
        self.kpi_dirty = True
        if self.mode == "live":
            self.live.append(
                LiveMsg(
                    "unit",
                    ev.ts,
                    {
                        "line": data.line,
                        "body_id": data.body_id,
                        "product": data.product,
                        "result": result,
                        "defect_code": data.defect_code,
                        "ts": iso(t),
                    },
                )
            )

    # ================================================================== buffers and CKD (§9.4)

    def _on_buffer(self, ev: BufferLevelEvent, t: float) -> None:
        if self._late(ev.entity, "buffer_level", t):
            return
        b = self.st.buffers.get(ev.data.buffer)
        if b is None:
            return
        b.capacity = ev.data.capacity
        b.level, b.ts = ev.data.level, t
        if b.history and b.history[-1].ts == t:
            b.history[-1].level = ev.data.level
        else:
            b.history.append(Point(t, ev.data.level))
        while len(b.history) > 1 and b.history[1].ts < t - BUFFER_KEEP_S:
            b.history.popleft()
        for acc in self.st.shifts.values():
            if not acc.closed and t < acc.start:
                acc.buffer_start[ev.data.buffer] = ev.data.level
        self._check_buffer(ev.data.buffer, t)
        if self.line_state_source == "derive":
            buf = self.cfg.buffers[ev.data.buffer]
            for line in (buf.from_line, buf.to_line):
                self._derive_line(line, t)
        if self.mode == "live":
            view = self._buffer_view(ev.data.buffer, t)
            self.views["buffers"][ev.data.buffer] = view
            self.live.append(LiveMsg("buffer", ev.ts, view, ("buffers", ev.data.buffer, view)))

    def level_at(self, code: str, t: float) -> int | None:
        """Buffer level from events strictly before ``t``."""
        b = self.st.buffers[code]
        level = None
        for p in b.history:
            if p.ts < t:
                level = p.level
            else:
                break
        if level is None and b.history and b.history[0].ts >= t:
            return None
        return level if level is not None else b.level

    def _buffer_view(self, code: str, t: float) -> dict[str, Any]:
        b = self.st.buffers[code]
        window = self.params.buffer_balance_window_min * 60.0
        rate = None
        past = self.level_at(code, t - window)
        if b.level is not None and past is not None and b.history and b.history[0].ts <= t - window:
            rate = (b.level - past) / (window / 60.0)  # units per minute
        to_full = to_empty = None
        if rate is not None and b.level is not None:
            if rate > 1e-9:
                to_full = round((b.capacity - b.level) / rate, 1)
            elif rate < -1e-9:
                to_empty = round(b.level / -rate, 1)
        return {
            "code": code,
            "level": b.level,
            "capacity": b.capacity,
            "rate_per_min": None if rate is None else round(rate, 4),
            "minutes_to_full": to_full,
            "minutes_to_empty": to_empty,
            "ts": iso(b.ts) if b.ts else None,
        }

    def _on_ckd(self, ev: CkdEvent, t: float) -> None:
        if self._late(ev.entity, "ckd", t):
            return
        self.st.kits[ev.data.product] = ev.data.kits
        self._check_ckd(ev.data.product, t)

    def _on_alarm(self, ev: AlarmEvent, t: float) -> None:
        ent = self.st.entities.get(ev.entity)
        if ent is None or self._late(ev.entity, "alarm", t):
            return
        if ent.alarm != ev.data.active:
            ent.alarm = ev.data.active
            self._state_msg(ent, t)

    # ================================================================== shift close (FR-KPI-04)

    def _line_kpis(self, acc: ShiftAcc, hi: float) -> dict[str, ShiftKpi]:
        lo = acc.start
        hi = min(hi, acc.end)
        pot_min = max(hi - lo, 0.0) / 60.0
        out: dict[str, ShiftKpi] = {}
        for line in self.flow:
            ent = self.st.entities[line]
            spans = [
                StateSpan(s.start, s.end if s.end is not None else hi, s.state)
                for s in ent.history
                if s.start < hi and (s.end is None or s.end > lo)
            ]
            stops = [
                s
                for s in self.st.stops.values()
                if s.entity == line and s.state in DOWN and s.start < hi and (s.end or hi) > lo
            ]
            stop_spans = [
                StopSpan(s.start, s.end if s.end is not None else hi, s.planned, s.duration(hi))
                for s in stops
            ]
            out[line] = line_shift_kpi(
                window=(lo, hi),
                pot_min=pot_min,
                spans=spans,
                stops=stop_spans,
                counts=acc.lines[line],
                threshold_s=self.threshold_s,
            )
        return out

    def _area_kpis(self, kpis: Mapping[str, ShiftKpi]) -> dict[str, ShiftKpi]:
        by_area: dict[str, list[ShiftKpi]] = {}
        for line, kpi in kpis.items():
            by_area.setdefault(self.area_of[line], []).append(kpi)
        out: dict[str, ShiftKpi] = {}
        for area, items in by_area.items():
            agg = aggregate_kpi(items)
            if agg is not None:
                out[area] = agg
        return out

    def _bottleneck(self, lo: float, hi: float) -> ShiftingBottleneck:
        periods = {
            line: active_periods(
                (
                    (s.start, s.end if s.end is not None else hi, s.state)
                    for s in self.st.entities[line].history
                ),
                (lo, hi),
            )
            for line in self.flow
        }
        return shifting_bottleneck(periods, (lo, hi), self.flow)

    def _close_shift(
        self, acc: ShiftAcc, t: float, *, user: str | None = None, reason: str | None = None
    ) -> None:
        first = not acc.closed
        kpis = self._line_kpis(acc, acc.end)
        acc.version += 1
        acc.closed = True
        day = date.fromisoformat(acc.date)
        computed = to_dt(t)
        for line, kpi in kpis.items():
            values = kpi_values(kpi)
            self.effects.append(
                KpiShiftRow(line, day, acc.code, acc.version, True, values, computed)
            )
            if self.mode == "live":
                self.live.append(
                    LiveMsg(
                        "kpi",
                        computed,
                        {
                            "level": "line",
                            "code": line,
                            "shift": {"date": acc.date, "code": acc.code},
                            "final": True,
                            "version": acc.version,
                            **values,
                        },
                    )
                )
        areas = self._area_kpis(kpis)
        area_rates = {area: k.defect_rate for area, k in areas.items()}
        if not acc.partial:
            self._spc(acc, areas, t, first=first)
        if not first:
            self.effects.append(
                AuditRow(
                    ts=computed,
                    action="kpi_shift.recompute",
                    entity_type="kpi_shift",
                    entity_id=f"{acc.key}",
                    before={"version": acc.version - 1},
                    after={
                        "version": acc.version,
                        "reason": reason,
                        "oee": {ln: k.oee for ln, k in kpis.items()},
                    },
                    user=user,
                )
            )
        if first:
            bn = self._bottleneck(acc.start, acc.end)
            for line in self.flow:
                self.effects.append(
                    BottleneckRow(
                        self.site,
                        day,
                        acc.code,
                        line,
                        bn.share(bn.sole.get(line, 0.0)),
                        bn.share(bn.shifting.get(line, 0.0)),
                    )
                )
                rates = self.st.bn_rates.setdefault(line, [])
                rates.append(kpis[line].pq)
                del rates[: -self.params.bottleneck_rate_shifts]
            last = self.flow[-1]
            month = acc.date[:7]
            self.st.mtd_good[month] = self.st.mtd_good.get(month, 0) + kpis[last].gq
        if not acc.partial:
            self._shift_dq(acc, kpis, t)
        self._shift_rules(acc, kpis, area_rates, t, first=first)
        acc.area_rates = area_rates
        if first:
            self.st.prev_area_rates = area_rates
            self._clock_msg(t, acc, "shift_closed")
        closed = sorted((a for a in self.st.shifts.values() if a.closed), key=lambda a: a.start)
        for old in closed[:-CLOSED_SHIFTS_KEPT]:
            del self.st.shifts[old.key]

    def _shift_dq(self, acc: ShiftAcc, kpis: Mapping[str, ShiftKpi], t: float) -> None:
        dq = self.cfg.rules.data_quality
        day = date.fromisoformat(acc.date)
        lo, hi = acc.start, acc.end
        for area, lines in self._areas_with_lines().items():
            eqs = {eq.code for line in lines for eq in self.cfg.equipment_of_line(line)}
            logged = 0.0
            for s in self.st.stops.values():
                if s.entity not in eqs or s.state not in DOWN:
                    continue
                if s.start >= hi or (s.end or hi) <= lo:
                    continue
                if not s.planned and is_microstop(s.duration(t), self.threshold_s):
                    continue
                logged += (min(s.end or hi, hi) - max(s.start, lo)) / 60.0
            if self.params.dq_live_lost == "downtime":
                lost = sum(kpis[line].pdot_min + kpis[line].adot_min for line in lines)
            else:
                lost = sum(kpis[line].pbt_min - kpis[line].apt_min for line in lines)
            issue = check_downtime_reconciliation(
                area=area,
                period_date=day,
                logged_min=round(logged, 4),
                lost_min=round(lost, 4),
                thresholds=dq,
            )
            if issue is not None:
                issue = replace(
                    issue, details={**issue.details, "shift": acc.code, "source": "events"}
                )
                self._dq(issue, t, f"DQ-02|{area}|{acc.key}")
        for b in self.cfg.plant.buffers:
            end_level = self.level_at(b.code, acc.end)
            issue = check_flow_balance_live(
                upstream=b.from_line,
                downstream=b.to_line,
                period_date=day,
                shift=acc.code,
                upstream_out=acc.lines[b.from_line].out_to_next,
                downstream_in=acc.lines[b.to_line].pq,
                buffer_start=acc.buffer_start.get(b.code, 0),
                buffer_end=end_level if end_level is not None else acc.buffer_start.get(b.code, 0),
                thresholds=dq,
                wip_tolerance=self.params.flow_wip_tolerance,
            )
            if issue is not None:
                self._dq(issue, t, f"DQ-04|{b.from_line}->{b.to_line}|{acc.key}")
        for line, kpi in kpis.items():
            issue = check_effectiveness(line=line, period_date=day, effectiveness=kpi.effectiveness)
            if issue is not None:
                issue = replace(issue, details={**issue.details, "shift": acc.code})
                self._dq(issue, t, f"DQ-06|{line}|{acc.key}")

    def _areas_with_lines(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for line in self.flow:
            out.setdefault(self.area_of[line], []).append(line)
        return out

    def _dq(self, issue: DqIssue, t: float, key: str) -> None:
        self.effects.append(
            DqUpsert(
                dedup_key=key,
                ts=to_dt(t),
                rule_id=issue.rule_id,
                severity=issue.severity,
                entity=issue.entity,
                period_date=issue.period_date,
                details=dict(issue.details),
            )
        )

    def _shift_rules(
        self,
        acc: ShiftAcc,
        kpis: Mapping[str, ShiftKpi],
        area_rates: Mapping[str, float | None],
        t: float,
        *,
        first: bool,
    ) -> None:
        day = date.fromisoformat(acc.date)
        history = self.mode == "replay" and self.resolve_history_alerts
        confirmed: set[str] = set()
        if not acc.partial:
            for line, kpi in kpis.items():
                alert = self.evaluator.oee(line=line, period_date=day, shift=acc.code, oee=kpi.oee)
                if alert is not None:
                    confirmed.add(alert.dedup_key)
                    self._raise(alert, t, resolve=history)
        for key, open_alert in list(self.st.alerts.items()):
            if (
                open_alert.projection
                and open_alert.period_date == acc.date
                and open_alert.shift == acc.code
                and key not in confirmed
            ):
                self._resolve(key, t)
        for area, rate in area_rates.items():
            alert = self.evaluator.defect_rate(
                area=area, period_date=day, shift=acc.code, defect_rate=rate
            )
            if alert is not None:
                self._raise(alert, t, resolve=history)
        if first and self.st.prev_area_rates:
            alert = self.evaluator.systemic_defects(
                period_date=day,
                previous=self.st.prev_area_rates,
                current=area_rates,
                shift=acc.code,
            )
            if alert is not None:
                self._raise(alert, t, resolve=history)

    # ================================================================== SPC (AL-Q2, §11.3)

    def _spc(self, acc: ShiftAcc, areas: Mapping[str, ShiftKpi], t: float, *, first: bool) -> None:
        """Add the closed shift to each area's p-chart; a Western Electric pattern completed by
        the newest point raises AL-Q2 (replay of history writes it as resolved)."""
        day = date.fromisoformat(acc.date)
        history = self.mode == "replay" and self.resolve_history_alerts
        keep = self.params.spc_history_shifts
        for area, kpi in areas.items():
            if kpi.pq <= 0:
                continue
            points = self.st.spc.setdefault(area, [])
            point = SpcPoint(acc.key, kpi.pq - kpi.gq, kpi.pq)
            for i, old in enumerate(points):
                if old.key == acc.key:
                    point.special = old.special
                    points[i] = point
                    break
            else:
                points.append(point)
            del points[:-keep]
            if not first or points[-1].key != acc.key:
                continue
            chart = p_chart([Subgroup(p.key, p.defects, p.n, p.special) for p in points])
            found = chart.latest_violations
            if chart.p_bar is None or not found:
                continue
            last = chart.points[-1]
            value = {
                "rules": sorted({v.rule for v in found}),
                "side": found[0].side,
                "p": round(last.p, 4),
                "p_bar": round(chart.p_bar, 4),
                "ucl": round(last.ucl, 4),
                "lcl": round(last.lcl, 4),
                "z": round(last.z, 2),
                "n": last.n,
                "defects": last.defects,
                "key": acc.key,
            }
            alert = self.evaluator.spc_violation(
                area=area, period_date=day, shift=acc.code, value=value
            )
            if alert is not None:
                self._raise(alert, t, resolve=history)

    # ================================================================== PdM serving (M7b)

    def pdm_slot(self, now: datetime, settle_s: float = 0.0) -> datetime | None:
        """Plant time of the PdM tick due at ``now`` (every ``pdm_tick_min`` plant minutes,
        aligned to the epoch, ``settle_s`` after the slot); ``None`` if already served."""
        step = self.params.pdm_tick_min * 60.0
        t = to_sec(now)
        slot = math.floor((t - settle_s) / step) * step
        last = self.st.pdm_last
        if last is not None and slot <= last:
            return None
        return to_dt(slot)

    def apply_pdm(self, tick: PdmTick) -> None:
        """Store a PdM tick: ``prediction`` rows, live health, AL-M1 (``p_failure`` thresholds)
        and AL-M2 (a signal reaches its limit within the look-ahead). Units that are down are
        not predicted; their open AL-M1/AL-M2 end (the failure happened or the service started)."""
        t = to_sec(tick.ts)
        self.st.pdm_last = max(self.st.pdm_last or t, t)
        now = max(t, self.st.watermark or t)
        self._pdm_close_for_down_units(now)
        day = self.calendar.local_date(tick.ts)
        thr = self.t
        for unit in tick.units:
            ent = self.st.entities.get(unit.equipment)
            if ent is None or ent.state in DOWN:
                continue
            self.effects.append(
                PredictionRow(
                    unit.equipment,
                    tick.ts,
                    unit.horizon_h,
                    round(unit.p_failure, 4),
                    round(unit.health_index, 1),
                    unit.model_version,
                    [dict(f) for f in unit.factors],
                )
            )
            self.st.health[unit.equipment] = {
                "health_index": round(unit.health_index, 1),
                "p_failure": round(unit.p_failure, 4),
                "ts": t,
            }
            if ent.state is not None:
                self._state_msg(ent, now)
            existing = self._open_for(AL_PDM, unit.equipment)
            if unit.p_failure >= thr.pdm_warn_p:
                value = {
                    "p_failure": round(unit.p_failure, 3),
                    "health_index": round(unit.health_index, 1),
                    "horizon_h": unit.horizon_h,
                    "model_version": unit.model_version,
                    "source": unit.source,
                    "factors_ru": [str(f.get("text_ru", "")) for f in unit.factors],
                    "factors_kk": [str(f.get("text_kk", "")) for f in unit.factors],
                }
                started = existing.period_key if existing and existing.period_key else iso(t)
                alert = self.evaluator.pdm_failure(
                    equipment=unit.equipment,
                    period_date=day,
                    started_key=started,
                    p_failure=unit.p_failure,
                    value=value,
                )
                if alert is not None and (
                    existing is None
                    or existing.severity != alert.severity
                    or abs(_value_p(existing.value) - unit.p_failure) >= 0.05
                ):
                    self._raise(alert, now)
            elif existing is not None and unit.p_failure < (
                self.params.pdm_resolve_ratio * thr.pdm_warn_p
            ):
                self._resolve(existing.key, now)
        for item in tick.limits:
            self._apply_limit(item, tick, day, t, now)

    def _apply_limit(self, item: Any, tick: PdmTick, day: date, t: float, now: float) -> None:
        ent = self.st.entities.get(item.equipment)
        if ent is None or ent.state in DOWN:
            return
        existing = self._open_for(AL_LIMIT, item.equipment, item.signal)
        if item.alert and item.hours_to_limit is not None:
            value = {
                "signal": item.signal,
                "signal_name_ru": item.signal_name_ru,
                "unit": item.unit,
                "limit": item.limit,
                "level_now": round(item.level_now, 2),
                "slope_per_h": round(item.slope_per_h, 3),
                "hours_to_limit": round(item.hours_to_limit, 1),
                "limit_at": item.limit_at.isoformat() if item.limit_at else None,
                "window": item.window.isoformat() if item.window else None,
                "saving_min": round(item.saving_min, 1),
                "saving_cars": round(item.saving_cars, 1),
            }
            if existing is not None and isinstance(existing.value, dict):
                prev = existing.value
                same_window = prev.get("window") == value["window"]
                near = abs(float(prev.get("hours_to_limit", 0.0)) - item.hours_to_limit) < 0.5
                if same_window and near:
                    return
            started = (
                existing.period_key
                if existing is not None and existing.period_key
                else f"{iso(t)}/{item.signal}"
            )
            alert = self.evaluator.limit_reach(
                equipment=item.equipment, period_date=day, started_key=started, value=value
            )
            if alert is not None:
                self._raise(alert, now)
        elif existing is not None and (
            item.hours_to_limit is None
            or item.hours_to_limit > tick.lookahead_h + self.params.pdm_lookahead_clear
        ):
            self._resolve(existing.key, now)

    def _open_for(
        self, rule_id: str, equipment: str, signal: str | None = None
    ) -> OpenAlert | None:
        for a in self.st.alerts.values():
            if a.rule_id != rule_id or a.entity != equipment:
                continue
            if signal is not None and not (
                isinstance(a.value, dict) and a.value.get("signal") == signal
            ):
                continue
            return a
        return None

    def _pdm_close_for_down_units(self, t: float) -> None:
        """AL-M1 / AL-M2 of a unit in planned service or in a real failure are over."""
        for key, a in list(self.st.alerts.items()):
            if a.rule_id not in (AL_PDM, AL_LIMIT):
                continue
            ent = self.st.entities.get(a.entity)
            if ent is None:
                continue
            if ent.state == "DOWN_PLANNED":
                self._resolve(key, t)
            elif ent.state == "DOWN_UNPLANNED":
                stop = self.st.stops.get(ent.stop or "")
                if stop is not None and stop.duration(t) >= self.threshold_s:
                    self._resolve(key, t)

    # ================================================================== alerts (§9.7)

    def _raise(
        self, alert: Alert, t: float, *, resolve: bool = False, projection: bool = False
    ) -> None:
        key = alert.dedup_key
        prev = self.st.alerts.get(key)
        value = alert.value
        if (
            prev is not None
            and prev.severity == alert.severity
            and prev.value == value
            and not resolve
        ):
            return
        ts = prev.ts if prev is not None else t
        if resolve:
            self.st.alerts.pop(key, None)
            self._alert_effect(alert, ts, "resolved", t)
            return
        new = OpenAlert(
            key=key,
            rule_id=alert.rule_id,
            severity=alert.severity,
            ts=ts,
            entity_type=alert.entity_type,
            entity=alert.entity,
            period_date=alert.period_date.isoformat(),
            shift=alert.shift,
            period_key=alert.period_key,
            value=value,
            projection=projection,
        )
        if prev is not None:
            new.escalation_level = prev.escalation_level
            new.escalation_due = prev.escalation_due
        elif self.mode == "live":
            esc = self.cfg.rules.escalation.get(alert.severity)
            if esc is not None and len(esc.chain) > 1:
                new.escalation_due = ts + esc.timeout_min * 60.0
        self.st.alerts[key] = new
        self._alert_effect(alert, ts, "open", None)

    def _resolve(self, key: str, t: float, value: Any = None) -> None:
        open_alert = self.st.alerts.pop(key, None)
        if open_alert is None:
            return
        alert = self._to_alert(open_alert)
        if value is not None:
            alert = replace(alert, value=value)
        self._alert_effect(alert, open_alert.ts, "resolved", t)

    def _to_alert(self, a: OpenAlert) -> Alert:
        return Alert(
            a.rule_id,
            a.severity,  # type: ignore[arg-type]
            a.entity_type,
            a.entity,
            date.fromisoformat(a.period_date),
            a.value,  # type: ignore[arg-type]
            a.shift,
            a.period_key,
        )

    def _alert_effect(self, alert: Alert, ts: float, status: str, resolved: float | None) -> None:
        rule = self.cfg.alert_rules.get(alert.rule_id)
        effect = AlertUpsert(
            dedup_key=alert.dedup_key,
            ts=to_dt(ts),
            rule_id=alert.rule_id,
            severity=alert.severity,
            entity_type=alert.entity_type,
            entity=alert.entity,
            title_ru=alert_title_ru(alert, self.cfg),
            message_ru=alert_message_ru(alert, self.cfg),
            value=alert.value,
            status=status,
            resolved_ts=to_dt(resolved) if resolved is not None else None,
            recipients=tuple(rule.recipients) if rule else (),
            channels=tuple(rule.channels) if rule else (),
        )
        self.effects.append(effect)
        if self.mode == "live":
            self.live.append(LiveMsg("alert", to_dt(resolved or ts), effect.message()))

    # ------------------------------------------------------------------ AL-S1 and impact

    def _s1_value(self, stop: Stop, t: float, *, ended: bool) -> dict[str, Any]:
        eq = self.cfg.equipment[stop.entity]
        elapsed = stop.duration(t)
        microstop = ended and is_microstop(elapsed, self.threshold_s)
        return {
            "started": iso(stop.start),
            "ended": iso(stop.end) if stop.end is not None else None,
            "elapsed_min": round(elapsed / 60.0, 1),
            "reason_code": stop.reason,
            "line": stop.line,
            "criticality": eq.criticality,
            "microstop": microstop,
            "impact": self._impact(stop, t),
        }

    def _raise_s1(self, stop: Stop, t: float) -> None:
        crit = self.cfg.equipment[stop.entity].criticality
        alert = self.evaluator.equipment_stop(
            equipment=stop.entity,
            criticality=crit,
            period_date=self.calendar.local_date(to_dt(stop.start)),
            started_key=iso(stop.start),
            elapsed_s=stop.duration(t),
            value=self._s1_value(stop, t, ended=False),
        )
        if alert is not None:
            stop.alert = alert.dedup_key
            self._raise(alert, t)

    def _update_s1(self, stop: Stop, t: float, *, ended: bool = False) -> None:
        key = stop.alert
        if key is None or key not in self.st.alerts:
            return
        value = self._s1_value(stop, t, ended=ended)
        open_alert = self.st.alerts[key]
        alert = replace(self._to_alert(open_alert), value=value)
        resolve = ended and (
            (value["microstop"] and self.params.s1_resolve_microstops)
            or (self.mode == "replay" and self.resolve_history_alerts)
        )
        if resolve:
            self._resolve(key, t, value)
        else:
            self._raise(alert, t)

    def _impact(self, stop: Stop, t: float) -> dict[str, Any]:
        line = stop.line
        eq = self.cfg.equipment.get(stop.entity)
        degraded = eq.degraded_capacity if eq is not None else 0.0
        acc = self._acc_for(t)
        bn_line = None
        if acc is not None and t > acc.start:
            bn_line = self._bottleneck(acc.start, t).current
        shift_min = (acc.end - acc.start) / 60.0 if acc is not None else self._shift_minutes()
        capacity = shift_min * 60.0 / self.ict[line]
        rates = self.st.bn_rates.get(bn_line or line) or []
        rate = sum(rates) / len(rates) if rates else None
        inbuf = self.buffer_in.get(line)
        free = None
        if inbuf is not None:
            b = self.st.buffers[inbuf]
            free = float(b.capacity - (b.level or 0))
        impact = stop_impact(
            elapsed_min=stop.duration(t) / 60.0,
            degraded_capacity=degraded,
            ict_seconds=self.ict[line],
            is_bottleneck=bn_line == line,
            line_capacity_per_shift=capacity,
            bottleneck_rate_per_shift=rate,
            upstream_free_units=free,
        )
        return {
            "lost_min": round(impact.lost_min, 1),
            "lost_units": round(impact.lost_units, 2),
            "bottleneck": impact.bottleneck,
            "bottleneck_line": bn_line,
            "irrecoverable_units": round(impact.irrecoverable_units, 2),
            "recover_shifts": None
            if impact.recover_shifts is None
            else round(impact.recover_shifts, 2),
        }

    def _shift_minutes(self) -> float:
        shifts = self.cfg.plant.calendar.shifts
        first = shifts[0]
        start = first.start.hour * 60 + first.start.minute
        end = first.end.hour * 60 + first.end.minute
        return float((end - start) % (24 * 60) or 24 * 60)

    # ------------------------------------------------------------------ AL-D1

    def _d1_add(self, stop: Stop) -> None:
        assert stop.end is not None
        for day, minutes in self._split_by_day(stop.start, stop.end):
            key = f"{stop.entity}|{day}"
            self.st.d1_minutes[key] = self.st.d1_minutes.get(key, 0.0) + minutes
            self._d1_eval(stop.entity, day, self.st.d1_minutes[key], stop.end)

    def _split_by_day(self, start: float, end: float) -> Iterable[tuple[str, float]]:
        cursor = start
        while cursor < end:
            day = self.calendar.local_date(to_dt(cursor))
            nxt = (
                datetime.combine(day, datetime.min.time(), tzinfo=self.calendar.tz).timestamp()
                + 86400.0
            )
            stop_at = min(end, nxt)
            yield day.isoformat(), (stop_at - cursor) / 60.0
            cursor = stop_at

    def _d1_eval(self, equipment: str, day: str, minutes: float, t: float) -> None:
        alert = self.evaluator.critical_downtime(
            equipment=equipment,
            period_date=date.fromisoformat(day),
            unplanned_min=round(minutes, 1),
        )
        if alert is not None:
            prev = self.st.alerts.get(alert.dedup_key)
            if (
                prev is None
                or prev.severity != alert.severity
                or abs(_as_float(prev.value) - minutes) >= 1.0
            ):
                self._raise(alert, t)

    # ------------------------------------------------------------------ AL-B1 / AL-L1

    def _in_shift(self, t: float) -> bool:
        return self._shift_at(t) is not None

    def _condition(self, cond: Cond, token: str | None, t: float, make: Any) -> None:
        debounce = self.params.alert_debounce_s
        if cond.active_key is None:
            if token is None:
                cond.pending, cond.since = None, None
                return
            if cond.pending != token:
                cond.pending, cond.since = token, t
            assert cond.since is not None
            if t - cond.since >= debounce:
                alert = make(cond.since)
                if alert is not None:
                    cond.active_key, cond.active_token = alert.dedup_key, token
                    cond.pending = None
                    cond.clear_since = None
                    self._raise(alert, t)
            return
        if token is None or (
            cond.active_token and token.split(":")[-1] != cond.active_token.split(":")[-1]
        ):
            if cond.clear_since is None:
                cond.clear_since = t
            if t - cond.clear_since >= debounce:
                self._resolve(cond.active_key, t)
                cond.active_key = cond.active_token = None
                cond.clear_since = None
                cond.pending, cond.since = None, None
            return
        cond.clear_since = None
        if token != cond.active_token and cond.since is not None:
            alert = make(cond.since)
            if alert is not None and alert.dedup_key == cond.active_key:
                cond.active_token = token
                self._raise(alert, t)

    def _check_buffer(self, code: str, t: float) -> None:
        b = self.st.buffers[code]
        if b.level is None:
            return
        condition = (
            self.evaluator.buffer_condition(level=b.level, capacity=b.capacity)
            if self._in_shift(t)
            else None
        )
        token = f"{condition[0]}:{condition[1]}" if condition else None
        level, capacity = b.level, b.capacity

        def make(since: float) -> Alert | None:
            return self.evaluator.buffer_level(
                buffer=code,
                level=level,
                capacity=capacity,
                period_date=self.calendar.local_date(to_dt(since)),
                started_key=iso(since),
            )

        self._condition(self.st.buffer_conds[code], token, t, make)

    def _daily_plan(self, product: str, t: float) -> float | None:
        day = self.calendar.local_date(to_dt(t))
        month = f"{day.year:04d}-{day.month:02d}"
        key = f"{product}|{month}"
        if key not in self._plan_cache:
            self._plan_cache[key] = self._daily_plan_uncached(product, day, month)
        return self._plan_cache[key]

    def _daily_plan_uncached(self, product: str, day: date, month: str) -> float | None:
        qty = sum(
            p.qty
            for p in self.cfg.plant.plan
            if p.month == month and p.level == "line_model" and p.product == product
        )
        if qty <= 0:
            return None
        days = len(self.calendar.working_days_in_month(day.year, day.month))
        return qty / days if days else None

    def _check_ckd(self, product: str, t: float) -> None:
        daily = self._daily_plan(product, t)
        kits = self.st.kits.get(product)
        if daily is None or kits is None:
            return
        token = (
            "warning:low"
            if kits / daily < self.t.ckd_coverage_min_days and self._in_shift(t)
            else None
        )

        def make(since: float) -> Alert | None:
            return self.evaluator.ckd_coverage(
                product=product,
                kits=kits,
                daily_plan=daily,
                period_date=self.calendar.local_date(to_dt(since)),
                started_key=iso(since),
            )

        self._condition(self.st.ckd_conds[product], token, t, make)

    # ------------------------------------------------------------------ timers

    def _timers(self, t: float) -> None:
        threshold = self.threshold_s
        unclassified = self.params.unclassified_after_min * 60.0
        open_stops = [
            self.st.stops[e.stop] for e in self.st.entities.values() if e.stop in self.st.stops
        ]
        for stop in open_stops:
            ent = self.st.entities[stop.entity]
            if (
                stop.alert is None
                and ent.kind == "equipment"
                and stop.state == "DOWN_UNPLANNED"
                and stop.duration(t) >= threshold
            ):
                self._raise_s1(stop, t)
            elif stop.alert is not None and stop.alert in self.st.alerts:
                shown = self.st.alerts[stop.alert].value
                elapsed = stop.duration(t) / 60.0
                if isinstance(shown, dict) and elapsed - float(shown.get("elapsed_min", 0)) >= 5.0:
                    self._update_s1(stop, t)  # FR-ENG-06: the impact grows with the stop
            if (
                not stop.flagged
                and stop.reason == FALLBACK_REASON_CODE
                and stop.reason_source == "auto"
                and stop.duration(t) >= unclassified
            ):
                stop.flagged = True
                self._downtime_msg(stop, t)
            if stop.end is None and self._counts_for_d1(stop) and stop.duration(t) >= threshold:
                today = self.calendar.local_date(to_dt(t)).isoformat()
                open_today = sum(m for d, m in self._split_by_day(stop.start, t) if d == today)
                closed = self.st.d1_minutes.get(f"{stop.entity}|{today}", 0.0)
                self._d1_eval(stop.entity, today, closed + open_today, t)
        for code in self.st.buffers:
            if self.st.buffer_conds[code].pending or self.st.buffer_conds[code].active_key:
                self._check_buffer(code, t)
        for product in list(self.st.kits):
            cond = self.st.ckd_conds[product]
            if cond.pending or cond.active_key:
                self._check_ckd(product, t)
        if self.mode == "replay" and self.resolve_history_alerts:
            today = self.calendar.local_date(to_dt(t)).isoformat()
            for key, a in list(self.st.alerts.items()):
                if a.rule_id == AL_DOWNTIME_LIMIT and a.period_date < today:
                    self._resolve(key, t)
        for key, a in list(self.st.alerts.items()):
            if a.escalation_due is not None and a.escalation_due <= t:
                esc = self.cfg.rules.escalation.get(a.severity)  # type: ignore[call-overload]
                if esc is None:
                    a.escalation_due = None
                    continue
                a.escalation_level += 1
                level = a.escalation_level
                due = a.escalation_due
                a.escalation_due = (
                    due + esc.timeout_min * 60.0 if level + 1 < len(esc.chain) else None
                )
                self.effects.append(AlertEscalate(key, level, (esc.chain[level],), to_dt(due)))
        horizon = t - HISTORY_KEEP_S - 86400.0
        if len(self.st.d1_minutes) > 64:
            cutoff = self.calendar.local_date(to_dt(horizon)).isoformat()
            for k in [k for k in self.st.d1_minutes if k.split("|")[1] < cutoff]:
                del self.st.d1_minutes[k]

    # ================================================================== live views (§12.3)

    def _state_msg(self, ent: Entity, t: float) -> None:
        if self.mode != "live":
            return
        name = "equipment" if ent.kind == "equipment" else "lines"
        view = self.views[name].setdefault(ent.code, {"code": ent.code})
        view.update(
            {
                "state": ent.state,
                "since": iso(ent.since) if ent.since is not None else None,
                "reason_code": ent.reason,
                "reason_source": ent.reason_source,
            }
        )
        if ent.kind == "equipment":
            eq = self.cfg.equipment[ent.code]
            stop = self.st.stops.get(ent.stop or "")
            view.update(
                {
                    "line": self.line_of[ent.code],
                    "criticality": eq.criticality,
                    "alarm": ent.alarm,
                    "alarm_code": ent.alarm_code,
                    "health_index": (self.st.health.get(ent.code) or {}).get("health_index"),
                    "downtime": self._stop_view(stop, t) if stop else None,
                }
            )
        delta = {"entity_type": ent.kind, **view}
        self.live.append(LiveMsg("state", to_dt(t), delta, (name, ent.code, dict(view))))

    def kpi_due(self, now: datetime) -> bool:
        last = self.st.last_kpi_tick
        return self.kpi_dirty or last is None or to_sec(now) - last >= self.params.kpi_tick_s

    def live_kpi(self, now: datetime) -> list[LiveMsg]:
        """Live KPIs of the running shift, equipment metrics, areas, plant and bottleneck
        (FR-KPI-03, FR-ENG-05); evaluates the projected AL-O1/O2 (live mode)."""
        t = to_sec(now)
        self.st.last_kpi_tick = t
        self.kpi_dirty = False
        msgs: list[LiveMsg] = []
        acc = self._current_acc(t)
        if acc is None:
            return msgs
        hi = min(t, acc.end)
        kpis = self._line_kpis(acc, hi)
        elapsed = max(hi - acc.start, 0.0) / 60.0
        shift = {"date": acc.date, "code": acc.code, "elapsed_min": round(elapsed, 1)}
        day = date.fromisoformat(acc.date)
        for line, kpi in kpis.items():
            plan = (
                self.cfg.lines[line].plan_rate_per_shift
                * elapsed
                / max((acc.end - acc.start) / 60.0, 1.0)
            )
            values = kpi_values(kpi)
            data = {
                "level": "line",
                "code": line,
                "shift": shift,
                "final": False,
                "plan_to_now": round(plan, 1),
                **values,
            }
            view = self.views["lines"].setdefault(line, {"code": line})
            view.update({k: v for k, v in data.items() if k not in ("level",)})
            msgs.append(LiveMsg("kpi", now, data, ("lines", line, dict(view))))
            if not acc.partial and elapsed >= self.params.live_oee_min_elapsed_min:
                alert = self.evaluator.oee(line=line, period_date=day, shift=acc.code, oee=kpi.oee)
                for rule in (AL_OEE_BELOW, AL_OEE_NEAR):
                    key = f"{rule}|{line}|{acc.key}"
                    existing = self.st.alerts.get(key)
                    if (
                        existing is not None
                        and existing.projection
                        and (alert is None or alert.dedup_key != key)
                    ):
                        self._resolve(key, t)
                if alert is not None:
                    prev = self.st.alerts.get(alert.dedup_key)
                    if (
                        prev is None
                        or prev.severity != alert.severity
                        or abs(_as_float(prev.value) - _as_float(alert.value)) >= 0.005
                    ):
                        self._raise(alert, t, projection=True)
        for area, kpi in self._area_kpis(kpis).items():
            data = {
                "level": "area",
                "code": area,
                "shift": shift,
                "final": False,
                **kpi_values(kpi),
            }
            self.views["areas"][area] = data
            msgs.append(LiveMsg("kpi", now, data, ("areas", area, data)))
        month = acc.date[:7]
        plant = {
            "level": "plant",
            "code": self.site,
            "shift": shift,
            "rty": rty(k.quality_ratio for k in kpis.values()),
            "fg_good_shift": kpis[self.flow[-1]].gq,
            "fg_good_mtd": self.st.mtd_good.get(month, 0) + kpis[self.flow[-1]].gq,
        }
        msgs.append(LiveMsg("kpi", now, plant, ("plant", None, plant)))
        for code in self.cfg.equipment:
            metrics = self._equipment_metrics(code, acc.start, hi)
            view = self.views["equipment"].setdefault(code, {"code": code})
            view["shift"] = metrics
            msgs.append(
                LiveMsg(
                    "kpi",
                    now,
                    {"level": "equipment", "code": code, "shift": shift, **metrics},
                    ("equipment", code, dict(view)),
                )
            )
        msgs.append(self._bottleneck_msg(acc, hi, now))
        return msgs

    def _equipment_metrics(self, code: str, lo: float, hi: float) -> dict[str, Any]:
        ent = self.st.entities[code]
        down_u = down_p = 0.0
        for s in ent.history:
            end = s.end if s.end is not None else hi
            sec = max(0.0, min(end, hi) - max(s.start, lo))
            if s.state == "DOWN_UNPLANNED":
                down_u += sec
            elif s.state == "DOWN_PLANNED":
                down_p += sec
        failures = 0
        repair = 0.0
        for stop in self.st.stops.values():
            if stop.entity != code or stop.state != "DOWN_UNPLANNED" or stop.planned:
                continue
            if (
                stop.start >= hi
                or (stop.end or hi) <= lo
                or is_microstop(stop.duration(hi), self.threshold_s)
            ):
                continue
            failures += 1
            repair += (min(stop.end or hi, hi) - max(stop.start, lo)) / 60.0
        window = max(hi - lo, 0.0)
        base = window - down_p
        run_min = max(window - down_u - down_p, 0.0) / 60.0
        return {
            "availability": None if base <= 0 else round((base - down_u) / base, 4),
            "failures": failures,
            "repair_min": round(repair, 1),
            "mtbf_h": None if failures == 0 else round(mtbf_h(run_min, failures) or 0.0, 2),
            "mttr_min": None if failures == 0 else round(mttr_min(repair, failures) or 0.0, 1),
        }

    def _bottleneck_msg(self, acc: ShiftAcc, hi: float, now: datetime) -> LiveMsg:
        shift_bn = self._bottleneck(acc.start, hi)
        window = self.params.bottleneck_window_h * 3600.0
        rolling = self._bottleneck(hi - window, hi)
        since = shift_bn.current_since
        data = {
            "current": shift_bn.current,
            "since": iso(since) if since is not None else None,
            "shift": {"date": acc.date, "code": acc.code},
            "shift_shares": shift_bn.shares(),
            "shift_overall": shift_bn.overall,
            "window_h": self.params.bottleneck_window_h,
            "window_shares": rolling.shares(),
            "window_overall": rolling.overall,
        }
        return LiveMsg("bottleneck", now, data, ("bottleneck", None, data))

    def full_views(self, now: datetime) -> list[LiveMsg]:
        """Every live entry (to rebuild ``live:*`` after a restart or reset)."""
        t = to_sec(now)
        out: list[LiveMsg] = []
        saved, self.live = self.live, []
        for ent in self.st.entities.values():
            if ent.state is not None:
                self._state_msg(ent, t)
        for code, b in self.st.buffers.items():
            if b.level is not None:
                view = self._buffer_view(code, t)
                self.views["buffers"][code] = view
                self.live.append(LiveMsg("buffer", now, view, ("buffers", code, view)))
        for stop in self.st.stops.values():
            if stop.end is None:
                self._downtime_msg(stop, t)
        out, self.live = self.live, saved
        return out + self.live_kpi(now)


def line_shift_kpi(
    *,
    window: tuple[float, float],
    pot_min: float,
    spans: Sequence[StateSpan],
    stops: Sequence[StopSpan],
    counts: LineAcc,
    threshold_s: float,
) -> ShiftKpi:
    """KPIs of one line over a window from its state intervals, its stop records (with the current
    planned flags) and its counters — shared by the live tick, shift close, replay and the
    recomputation from stored rows."""
    lo, hi = window
    time = shift_time_model(
        window=window, pot_min=pot_min, states=spans, stops=stops, microstop_threshold_s=threshold_s
    )
    failures = 0
    repair = 0.0
    for stop in stops:
        if stop.planned or is_microstop(stop.duration_s, threshold_s):
            continue
        failures += 1
        repair += max(0.0, min(stop.end, hi) - max(stop.start, lo)) / 60.0
    return compute_shift_kpi(
        time,
        pq=counts.pq,
        gq=counts.gq,
        pri_produced_s=counts.pri_produced_s,
        pri_good_s=counts.pri_good_s,
        failures=failures,
        repair_min=repair,
    )


def _as_float(value: object) -> float:
    return float(value) if isinstance(value, int | float) else 0.0


def kpi_values(kpi: ShiftKpi) -> dict[str, Any]:
    """KPI fields as stored in ``kpi_shift`` and published live (full precision)."""

    def num(value: float | None) -> float | None:
        return None if value is None or math.isnan(value) else value

    return {
        "pot": kpi.pot_min,
        "pdot": kpi.pdot_min,
        "pbt": kpi.pbt_min,
        "apt": kpi.apt_min,
        "adot": kpi.adot_min,
        "adet": kpi.adet_min,
        "aust": kpi.aust_min,
        "microstop_min": kpi.microstop_min,
        "pq": kpi.pq,
        "gq": kpi.gq,
        "pri_good_s": kpi.pri_good_s,
        "availability": num(kpi.availability),
        "effectiveness": num(kpi.effectiveness),
        "quality_ratio": num(kpi.quality_ratio),
        "oee": num(kpi.oee),
        "fpy": num(kpi.fpy),
        "defect_rate": num(kpi.defect_rate),
        "failures": kpi.failures,
        "repair_min": kpi.repair_min,
        "mtbf_h": kpi.mtbf_h,
        "mttr_min": kpi.mttr_min,
    }


__all__ = ["DOWN", "EngineCore", "Mode", "iso", "kpi_values", "line_shift_kpi", "to_dt", "to_sec"]


def _value_p(value: object) -> float:
    return float(value.get("p_failure", 0.0)) if isinstance(value, dict) else 0.0
