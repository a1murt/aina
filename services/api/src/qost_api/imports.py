"""Import service: run the import pipeline and persist its result (FR-IMP-04/06).

Writes, in one transaction: ``import_job`` (report in ``result``), ``shift_report`` (shift of D1,
upsert per line and shift), ``kpi_shift`` (``source=import``, a new version per import),
``downtime`` (``reason_source=import``, ``shift_code`` NULL; the latest import replaces earlier
imported entries of the same days), ``production_plan`` (upsert), ``dq_issue``, ``alert`` (upsert
by ``dedup_key``) and an ``audit_log`` entry.

Idempotency (FR-IMP-06): the upload digest is the sha256 of the file; for several files, the
sha256 of their sorted per-file digests (names do not matter). A repeated digest returns the
stored job without writing anything.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from qost_api.audit import audit
from qost_api.auth import Principal
from twin_core.alert_text import alert_message_ru
from twin_core.clock import Clock
from twin_core.config import TwinConfig
from twin_core.db import (
    AlertRow,
    Downtime,
    DqIssueRow,
    ImportJob,
    KpiShift,
    ProductionPlan,
    ShiftReport,
)
from twin_core.importer import ImportReport, UploadedFile, run_import

IMPORT_SOURCE = "import"


def upload_digest(files: Sequence[UploadedFile]) -> str:
    digests = sorted(hashlib.sha256(f.content).hexdigest() for f in files)
    if len(digests) == 1:
        return digests[0]
    return hashlib.sha256("\n".join(digests).encode()).hexdigest()


def upload_name(files: Sequence[UploadedFile]) -> str:
    return ", ".join(sorted(f.name for f in files))


@dataclass(frozen=True, slots=True)
class ImportOutcome:
    job: ImportJob
    created: bool


def job_view(job: ImportJob) -> dict[str, Any]:
    """The API body of an import: the stored report plus the job description."""
    body = dict(job.result or {})
    body["job"] = {
        "id": job.id,
        "filename": job.filename,
        "sha256": job.sha256,
        "kind": job.kind,
        "status": job.status,
        "created_ts": job.created_ts.isoformat(),
    }
    return body


# --------------------------------------------------------------------------- persistence


async def _persist(
    session: AsyncSession,
    report: ImportReport,
    job: ImportJob,
    cfg: TwinConfig,
    now: datetime,
) -> dict[str, int]:
    counts = {"shift_report": 0, "kpi_shift": 0, "downtime": 0, "production_plan": 0}
    for r in report.shift_reports:
        k = r.kpi
        values = {
            "import_id": job.id,
            "plan_qty": r.plan_qty,
            "produced_qty": k.pq,
            "defect_qty": k.defects,
            "worked_min": k.apt_min,
            "reported_load_pct": r.reported_load_pct,
            "reported_defect_pct": r.reported_defect_pct,
            "source": IMPORT_SOURCE,
        }
        stmt = insert(ShiftReport).values(
            line=r.line, shift_date=r.day, shift_code=r.shift, **values
        )
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=["line", "shift_date", "shift_code"], set_=values
            )
        )
        counts["shift_report"] += 1
        version = await session.scalar(
            select(func.coalesce(func.max(KpiShift.version), 0)).where(
                KpiShift.line == r.line,
                KpiShift.shift_date == r.day,
                KpiShift.shift_code == r.shift,
                KpiShift.source == IMPORT_SOURCE,
            )
        )
        session.add(
            KpiShift(
                line=r.line,
                shift_date=r.day,
                shift_code=r.shift,
                source=IMPORT_SOURCE,
                version=(version or 0) + 1,
                final=True,
                pot=k.pot_min,
                pdot=k.pdot_min,
                pbt=k.pbt_min,
                apt=k.apt_min,
                adot=k.adot_min,
                adet=k.adet_min,
                aust=k.aust_min,
                microstop_min=k.microstop_min,
                pq=k.pq,
                gq=k.gq,
                pri_good_s=k.pri_good_s,
                availability=k.availability,
                effectiveness=k.effectiveness,
                quality_ratio=k.quality_ratio,
                oee=k.oee,
                fpy=k.fpy,
                defect_rate=k.defect_rate,
                failures=k.failures,
                repair_min=k.repair_min,
                computed_ts=now,
            )
        )
        counts["kpi_shift"] += 1

    days = {d.day for d in report.downtime} | {r.day for r in report.shift_reports}
    replaced = await session.execute(
        delete(Downtime).where(
            Downtime.reason_source == IMPORT_SOURCE, Downtime.shift_date.in_(days)
        )
    )
    counts["downtime_replaced"] = int(getattr(replaced, "rowcount", 0) or 0)
    threshold_s = cfg.rules.thresholds.microstop_threshold_s
    for d in report.downtime:
        duration_s = d.duration_min * 60
        session.add(
            Downtime(
                entity=d.equipment,
                line=d.line,
                start_ts=None,
                end_ts=None,
                duration_s=duration_s,
                planned=d.planned,
                microstop=not d.planned and duration_s < threshold_s,
                reason_code=d.reason_code,
                reason_source=IMPORT_SOURCE,
                shift_date=d.day,
                shift_code=d.shift,
                comment=d.reason_text_src,
                import_id=job.id,
            )
        )
        counts["downtime"] += 1

    plan_rows: list[dict[str, Any]] = [
        {
            "month": report.plan.month,
            "level": "line_model",
            "line": report.plan.line,
            "product": row.model,
            "qty": row.qty,
        }
        for row in report.plan.rows
    ]
    if any(
        c.key == "plant_target_per_month" and c.source == "text" for c in report.constraint_checks
    ):
        plan_rows.append(
            {
                "month": report.plan.month,
                "level": "plant_target",
                "line": None,
                "product": None,
                "qty": report.plan.plant_target,
            }
        )
    for values in plan_rows:
        stmt = insert(ProductionPlan).values(import_id=job.id, **values)
        await session.execute(
            stmt.on_conflict_do_update(
                constraint="uq_production_plan_month_level_line_product",
                set_={"qty": stmt.excluded.qty, "import_id": stmt.excluded.import_id},
            )
        )
        counts["production_plan"] += 1

    for issue in report.dq_issues:
        session.add(
            DqIssueRow(
                ts=now,
                rule_id=issue.rule_id,
                severity=issue.severity,
                entity=issue.entity,
                period_date=issue.period_date,
                details=dict(issue.details),
                status="open",
                import_id=job.id,
            )
        )
    counts["dq_issue"] = len(report.dq_issues)

    for alert in report.alerts:
        rule = cfg.alert_rules[alert.rule_id]
        message = alert_message_ru(alert, cfg)
        stmt = insert(AlertRow).values(
            ts=now,
            rule_id=alert.rule_id,
            severity=alert.severity,
            entity_type=alert.entity_type,
            entity=alert.entity,
            title_ru=rule.name_ru,
            title_kk=rule.name_kk,
            message_ru=message,
            message_kk=None,
            value=alert.value,
            status="open",
            escalation_level=0,
            dedup_key=alert.dedup_key,
        )
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=["dedup_key"],
                set_={
                    "value": stmt.excluded.value,
                    "severity": stmt.excluded.severity,
                    "message_ru": stmt.excluded.message_ru,
                },
            )
        )
    counts["alert"] = len(report.alerts)
    return counts


async def find_job(session: AsyncSession, digest: str) -> ImportJob | None:
    job: ImportJob | None = await session.scalar(
        select(ImportJob).where(ImportJob.sha256 == digest)
    )
    return job


async def create_import(
    session: AsyncSession,
    files: Sequence[UploadedFile],
    *,
    cfg: TwinConfig,
    clock: Clock,
    principal: Principal,
) -> ImportOutcome:
    """Import an upload or return the existing job for the same content (FR-IMP-06).

    Raises :class:`twin_core.importer.ImportFormatError` for files that cannot be imported.
    """
    digest = upload_digest(files)
    existing = await find_job(session, digest)
    if existing is not None:
        return ImportOutcome(existing, created=False)
    name = upload_name(files)
    report = run_import(files, cfg, source=name)
    now = clock.now()
    job = ImportJob(
        filename=name,
        sha256=digest,
        kind=report.meta["kind"],
        status="done",
        created_by=principal.user_id,
        created_ts=now,
        result=report.to_json(),
    )
    session.add(job)
    try:
        await session.flush()
        counts = await _persist(session, report, job, cfg, now)
        audit(
            session,
            ts=now,
            principal=principal,
            action="import.create",
            entity_type="import_job",
            entity_id=str(job.id),
            after={
                "filename": name,
                "sha256": digest,
                "kind": job.kind,
                "period": report.meta["period"],
                "rows": counts,
            },
        )
        await session.commit()
    except IntegrityError:
        # A concurrent upload of the same content won the race: return its job.
        await session.rollback()
        winner = await find_job(session, digest)
        if winner is None:
            raise
        return ImportOutcome(winner, created=False)
    return ImportOutcome(job, created=True)


async def get_job(session: AsyncSession, import_id: int) -> ImportJob | None:
    return await session.get(ImportJob, import_id)
