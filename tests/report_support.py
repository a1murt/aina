"""Shift facts for report tests (shaped like the engine's rows for shift 15.10.2026 A)."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from twin_core.calendar import ShiftInstance
from twin_core.config import TwinConfig
from twin_core.forecast.result import (
    ForecastResult,
    Histogram,
    HorizonInfo,
    Quantiles,
    StateDigest,
)
from twin_core.report import AlertFact, BottleneckFact, DefectFact, KpiFact, ShiftFacts, StopFact

DAY = date(2026, 10, 15)


def kpi(
    line: str, oee: float, pq: int, gq: int, *, version: int = 1, source: str = "events"
) -> KpiFact:
    apt = 420.0
    return KpiFact(
        line=line,
        source=source,
        version=version,
        final=True,
        pot=480.0,
        pdot=10.0,
        pbt=470.0,
        apt=apt,
        adot=40.0,
        adet=10.0,
        aust=0.0,
        microstop_min=6.0,
        pq=pq,
        gq=gq,
        pri_good_s=gq * 233.0,
        availability=apt / 470.0,
        effectiveness=pq * 233.0 / (apt * 60.0),
        quality_ratio=gq / pq,
        oee=oee,
        defect_rate=(pq - gq) / pq,
        failures=2,
        repair_min=40.0,
    )


def sample_facts(cfg: TwinConfig, shift: ShiftInstance | None = None) -> ShiftFacts:
    sh = shift or cfg.calendar.shift(DAY, "A")
    t0 = sh.start
    return ShiftFacts(
        shift=sh,
        now=sh.end + timedelta(minutes=5),
        kpis=[
            kpi("WELD-1", 0.83, 108, 106),
            kpi("PAINT-1", 0.79, 106, 101),
            kpi("PAINT-1", 0.70, 106, 100, version=0),  # an older version must be ignored
            kpi("ASSY-1", 0.86, 105, 104),
            kpi("QC-1", 0.90, 104, 103),
        ],
        stops=[
            StopFact(
                "CONV-03",
                "ASSY-1",
                t0 + timedelta(hours=2, minutes=31),
                t0 + timedelta(hours=3, minutes=26),
                3300.0,
                False,
                False,
                "ME-CHAIN",
            ),
            StopFact(
                "BOOTH-02",
                "PAINT-1",
                t0 + timedelta(hours=5),
                t0 + timedelta(hours=5, minutes=20),
                1200.0,
                False,
                False,
                "MT-FILTER",
            ),
            StopFact(  # started before the shift: only the part inside counts
                "ABB-04",
                "WELD-1",
                t0 - timedelta(minutes=30),
                t0 + timedelta(minutes=12),
                2520.0,
                True,
                False,
                "PM-SCHEDULED",
            ),
            StopFact(
                "ASSY-1",
                "ASSY-1",
                t0 + timedelta(hours=1),
                t0 + timedelta(hours=1, minutes=2),
                120.0,
                False,
                True,
                "UNK",
            ),
        ],
        defects=[
            DefectFact("PAINT", "P-RUN", 3),
            DefectFact("PAINT", "P-DUST", 2),
            DefectFact("WELD", "W-SPOT", 2),
        ],
        alerts=[
            AlertFact(
                "AL-S1",
                "critical",
                "equipment",
                "CONV-03",
                t0 + timedelta(hours=2, minutes=31),
                "Внеплановая остановка оборудования",
                "Конвейер-03 (финальная): обрыв цепи, остановка с 15.10.2026 09:31, 55 мин; "
                "потеря ≈ 14,2 авто; отыгрывается за ≈ 0,5 смены",
                "resolved",
            ),
            AlertFact(
                "AL-Q1",
                "warning",
                "area",
                "PAINT",
                sh.end,
                "Брак выше нормы",
                "Окраска: брак 4,72% (15.10.2026, смена A), норма 2%",
                "open",
            ),
        ],
        bottleneck=[BottleneckFact("PAINT-1", 0.58, 0.1), BottleneckFact("WELD-1", 0.3, 0.02)],
    )


def sample_forecast(as_of: datetime, *, p_plan: float = 0.22) -> ForecastResult:
    """A month forecast as the M6 service returns it (only the fields reports read matter)."""
    q = Quantiles(p10=4757.0, p50=4787.4, p90=4807.0, mean=4786.0, sd=19.0)
    return ForecastResult(
        month="2026-10",
        as_of=as_of,
        seed=1,
        n_runs=5000,
        mtd=2511,
        targets={"plant_target": 5500, "line_plan": 4800},
        required_rate={"plant_target": 149.45, "line_plan": 114.45},
        horizon=HorizonInfo(
            steps=240, hours=240.0, working_shifts=20.0, month_shifts=42, extra_shifts=[]
        ),
        summary=q,
        p_reach={"plant_target": 0.0, "line_plan": p_plan},
        expected_shortfall={"plant_target": 713.0, "line_plan": 14.0},
        histogram=Histogram(edges=[4700.0, 4800.0], counts=[5000]),
        fan=[],
        rework_expected={},
        state=StateDigest(
            as_of=as_of,
            mtd=2511,
            buffers={},
            open_downs=[],
            filter_dp={},
            source="test",
            digest="x",
        ),
    )
