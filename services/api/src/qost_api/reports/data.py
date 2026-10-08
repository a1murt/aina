"""Database access of shift reports: the facts of one shift (SPEC §8, as the engine and the
import write them) and the ``report`` table.

Facts of shift (date, code) with boundaries [start, end):

* ``kpi_shift`` — every row of the shift (the assembly picks the latest version, events first);
* ``downtime`` — rows overlapping the shift, plus imported journal rows of that date/shift;
* ``defect`` — ``ts`` within the shift, grouped by area and code;
* ``alert`` — raised within the shift or keyed to it (``…|date/shift``);
* ``bottleneck_shift`` — shares of the shift.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Protocol

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from qost_api.audit import audit
from qost_api.auth import Principal
from twin_core.calendar import ShiftInstance
from twin_core.config import TwinConfig
from twin_core.db import Report
from twin_core.report import AlertFact, BottleneckFact, DefectFact, KpiFact, ShiftFacts, StopFact


@dataclass(frozen=True, slots=True)
class ReportRecord:
    kind: str
    shift_date: date
    shift_code: str
    lang: str
    text: str
    generated_by: str
    model: str | None
    numbers_verified: bool
    input: dict[str, Any]
    created_ts: datetime


@dataclass(frozen=True, slots=True)
class StoredReport:
    id: int
    record: ReportRecord


class ReportBackend(Protocol):
    async def shift_facts(
        self, cfg: TwinConfig, shift: ShiftInstance, *, now: datetime
    ) -> ShiftFacts: ...

    async def save(self, record: ReportRecord, *, principal: Principal) -> int: ...

    async def find(
        self, kind: str, shift_date: date, shift_code: str, lang: str | None
    ) -> list[StoredReport]: ...


_KPI_SQL = text(
    """
    SELECT line, source, version, final, pot, pdot, pbt, apt, adot, adet, aust, microstop_min,
           pq, gq, pri_good_s, availability, effectiveness, quality_ratio, oee, defect_rate,
           failures, repair_min
    FROM kpi_shift WHERE shift_date = :d AND shift_code = :c
    """
)
_STOPS_SQL = text(
    """
    SELECT entity, line, start_ts, end_ts, duration_s, planned, microstop, reason_code
    FROM downtime
    WHERE (start_ts IS NOT NULL AND start_ts < :end AND COALESCE(end_ts, :now) > :start)
       OR (start_ts IS NULL AND shift_date = :d AND shift_code = :c)
    """
)
_DEFECTS_SQL = text(
    """
    SELECT area, defect_code, SUM(qty)::int AS qty FROM defect
    WHERE ts >= :start AND ts < :end
    GROUP BY area, defect_code
    """
)
_ALERTS_SQL = text(
    """
    SELECT rule_id, severity, entity_type, entity, ts, title_ru, title_kk, message_ru, message_kk,
           status
    FROM alert
    WHERE (ts >= :start AND ts < :end) OR dedup_key LIKE :key
    ORDER BY ts
    """
)
_BOTTLENECK_SQL = text(
    """
    SELECT line, sole_share, shifting_share FROM bottleneck_shift
    WHERE shift_date = :d AND shift_code = :c
    """
)


async def load_shift_facts(
    session: AsyncSession, cfg: TwinConfig, shift: ShiftInstance, *, now: datetime
) -> ShiftFacts:
    window = {"start": shift.start, "end": shift.end}
    key = {"d": shift.shift_date, "c": shift.code}
    kpis = [KpiFact(**dict(r._mapping)) for r in (await session.execute(_KPI_SQL, key)).all()]
    stops = [
        StopFact(
            entity=r.entity,
            line=r.line,
            start=r.start_ts,
            end=r.end_ts,
            duration_s=r.duration_s,
            planned=r.planned,
            microstop=r.microstop,
            reason_code=r.reason_code,
        )
        for r in (await session.execute(_STOPS_SQL, {**window, **key, "now": now})).all()
    ]
    defects = [
        DefectFact(r.area, r.defect_code, int(r.qty))
        for r in (await session.execute(_DEFECTS_SQL, window)).all()
    ]
    suffix = f"%|{shift.shift_date.isoformat()}/{shift.code}"
    alerts = [
        AlertFact(
            rule_id=r.rule_id,
            severity=r.severity,
            entity_type=r.entity_type,
            entity=r.entity,
            ts=r.ts,
            title_ru=r.title_ru,
            message_ru=r.message_ru,
            status=r.status,
            title_kk=r.title_kk,
            message_kk=r.message_kk,
        )
        for r in (await session.execute(_ALERTS_SQL, {**window, "key": suffix})).all()
    ]
    bottleneck = [
        BottleneckFact(r.line, float(r.sole_share), float(r.shifting_share))
        for r in (await session.execute(_BOTTLENECK_SQL, key)).all()
    ]
    return ShiftFacts(
        shift=shift,
        now=now,
        kpis=kpis,
        stops=stops,
        defects=defects,
        alerts=alerts,
        bottleneck=bottleneck,
    )


def _record(row: Report) -> StoredReport:
    return StoredReport(
        id=row.id,
        record=ReportRecord(
            kind=row.kind,
            shift_date=row.shift_date or date.min,
            shift_code=row.shift_code or "",
            lang=row.lang,
            text=row.text,
            generated_by=row.generated_by,
            model=row.model,
            numbers_verified=row.numbers_verified,
            input=row.input,
            created_ts=row.created_ts,
        ),
    )


class DbReportBackend:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self.sessionmaker = sessionmaker

    async def shift_facts(
        self, cfg: TwinConfig, shift: ShiftInstance, *, now: datetime
    ) -> ShiftFacts:
        async with self.sessionmaker() as session:
            return await load_shift_facts(session, cfg, shift, now=now)

    async def save(self, record: ReportRecord, *, principal: Principal) -> int:
        async with self.sessionmaker() as session, session.begin():
            row = Report(
                kind=record.kind,
                shift_date=record.shift_date,
                shift_code=record.shift_code,
                lang=record.lang,
                text=record.text,
                generated_by=record.generated_by,
                model=record.model,
                numbers_verified=record.numbers_verified,
                input=record.input,
                created_ts=record.created_ts,
            )
            session.add(row)
            await session.flush()
            audit(
                session,
                ts=record.created_ts,
                principal=principal,
                action="report.create",
                entity_type="report",
                entity_id=str(row.id),
                after={
                    "kind": record.kind,
                    "shift": f"{record.shift_date.isoformat()}/{record.shift_code}",
                    "lang": record.lang,
                    "generated_by": record.generated_by,
                    "model": record.model,
                    "numbers_verified": record.numbers_verified,
                },
            )
            return row.id

    async def find(
        self, kind: str, shift_date: date, shift_code: str, lang: str | None
    ) -> list[StoredReport]:
        query = select(Report).where(
            Report.kind == kind, Report.shift_date == shift_date, Report.shift_code == shift_code
        )
        if lang is not None:
            query = query.where(Report.lang == lang)
        async with self.sessionmaker() as session:
            rows = (await session.execute(query.order_by(Report.id.desc()))).scalars().all()
        return [_record(r) for r in rows]


__all__ = [
    "DbReportBackend",
    "ReportBackend",
    "ReportRecord",
    "StoredReport",
    "load_shift_facts",
]
