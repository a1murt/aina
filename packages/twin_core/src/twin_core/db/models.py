"""SQLAlchemy 2 models of the whole storage schema (SPEC §8), shared by api, engine, collector.

Conventions:

* Reference keys are the text codes from ``config/*.yaml``; fact tables store codes without
  foreign keys to the reference tables (codes are validated against the config on write; the
  ``asset_*`` / ``product`` / ``*_code`` tables are a SQL mirror of the YAML, synced by ``api``).
* Every timestamp is ``timestamptz`` in UTC and is set by the application from
  :class:`twin_core.clock.Clock` — no ``now()`` server defaults (plant time may differ from the
  wall clock in ``CLOCK_MODE=sim``).
* Hypertables (TimescaleDB) have primary keys that include their time column (a TimescaleDB
  requirement for unique indexes); hypertable conversion, compression, retention and continuous
  aggregates are created by the Alembic migration ``0002``.
* Enumerations that are part of the contract (severities, statuses, sources) are ``text`` with
  ``CHECK`` constraints; plant-specific vocabularies (roles, codes) are not constrained here.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Double,
    ForeignKey,
    Identity,
    Index,
    Integer,
    MetaData,
    SmallInteger,
    Text,
    UniqueConstraint,
    false,
    text,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from twin_core.domain import EquipmentState

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

Tz = DateTime(timezone=True)
Json = JSONB(none_as_null=True)


def _in(column: str, *values: str) -> str:
    listed = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({listed})"


SEVERITIES = ("info", "warning", "critical")
STATES = tuple(state.value for state in EquipmentState)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _pk_id() -> Mapped[int]:
    return mapped_column(BigInteger, Identity(), primary_key=True)


def _user_fk() -> Mapped[int | None]:
    return mapped_column(BigInteger, ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True)


# =========================================================================== reference (YAML)


class AssetArea(Base):
    __tablename__ = "asset_area"

    code: Mapped[str] = mapped_column(Text, primary_key=True)
    kind: Mapped[str] = mapped_column(Text)
    name_ru: Mapped[str] = mapped_column(Text)
    name_kk: Mapped[str | None] = mapped_column(Text)
    seq: Mapped[int] = mapped_column(Integer)


class AssetLine(Base):
    __tablename__ = "asset_line"

    code: Mapped[str] = mapped_column(Text, primary_key=True)
    area: Mapped[str] = mapped_column(Text, ForeignKey("asset_area.code"))
    name_ru: Mapped[str] = mapped_column(Text)
    ict_seconds: Mapped[float] = mapped_column(Double)
    plan_rate_per_shift: Mapped[int] = mapped_column(Integer)


class AssetEquipment(Base):
    __tablename__ = "asset_equipment"
    __table_args__ = (CheckConstraint(_in("criticality", "A", "B", "C"), name="criticality"),)

    code: Mapped[str] = mapped_column(Text, primary_key=True)
    line: Mapped[str] = mapped_column(Text, ForeignKey("asset_line.code"))
    type: Mapped[str] = mapped_column(Text)
    criticality: Mapped[str] = mapped_column(Text)
    degraded_capacity: Mapped[float] = mapped_column(Double)
    name_ru: Mapped[str] = mapped_column(Text)


class AssetBuffer(Base):
    __tablename__ = "asset_buffer"

    code: Mapped[str] = mapped_column(Text, primary_key=True)
    from_line: Mapped[str] = mapped_column(Text, ForeignKey("asset_line.code"))
    to_line: Mapped[str] = mapped_column(Text, ForeignKey("asset_line.code"))
    capacity: Mapped[int] = mapped_column(Integer)


class Product(Base):
    __tablename__ = "product"

    code: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    cycle_factor: Mapped[float] = mapped_column(Double)


class ReasonCode(Base):
    __tablename__ = "reason_code"

    code: Mapped[str] = mapped_column(Text, primary_key=True)
    category: Mapped[str] = mapped_column(Text)
    name_ru: Mapped[str] = mapped_column(Text)
    name_kk: Mapped[str | None] = mapped_column(Text)
    planned: Mapped[bool] = mapped_column(Boolean)
    bucket: Mapped[str] = mapped_column(Text)


class DefectCode(Base):
    __tablename__ = "defect_code"

    code: Mapped[str] = mapped_column(Text, primary_key=True)
    area: Mapped[str] = mapped_column(Text)
    """Area code or ``ANY``."""
    name_ru: Mapped[str] = mapped_column(Text)
    name_kk: Mapped[str | None] = mapped_column(Text)
    disposition: Mapped[str] = mapped_column(Text)
    rework_min: Mapped[float] = mapped_column(Double)
    repaint: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())


# =========================================================================== calendar and plan


class Shift(Base):
    __tablename__ = "shift"

    shift_date: Mapped[date] = mapped_column(Date, primary_key=True)
    shift_code: Mapped[str] = mapped_column(Text, primary_key=True)
    start_ts: Mapped[datetime] = mapped_column(Tz)
    end_ts: Mapped[datetime] = mapped_column(Tz)
    working: Mapped[bool] = mapped_column(Boolean)


class ProductionPlan(Base):
    __tablename__ = "production_plan"
    __table_args__ = (
        UniqueConstraint("month", "level", "line", "product", postgresql_nulls_not_distinct=True),
        CheckConstraint(_in("level", "plant_target", "line_model"), name="level"),
    )

    id: Mapped[int] = _pk_id()
    month: Mapped[str] = mapped_column(Text)
    """``YYYY-MM``."""
    level: Mapped[str] = mapped_column(Text)
    line: Mapped[str | None] = mapped_column(Text)
    product: Mapped[str | None] = mapped_column(Text)
    qty: Mapped[int] = mapped_column(Integer)
    import_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("import_job.id", ondelete="SET NULL")
    )


# =========================================================================== hypertables


class EventRaw(Base):
    """Raw event journal (``twin_core.events`` schema); idempotent by ``event_id``."""

    __tablename__ = "event_raw"
    __table_args__ = (Index("ix_event_raw_entity_ts", "entity", text("ts DESC")),)

    event_id: Mapped[str] = mapped_column(Text, primary_key=True)
    ts: Mapped[datetime] = mapped_column(Tz, primary_key=True)
    received_ts: Mapped[datetime | None] = mapped_column(Tz)
    source: Mapped[str] = mapped_column(Text)
    entity_type: Mapped[str] = mapped_column(Text)
    entity: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    data: Mapped[dict[str, Any]] = mapped_column(Json)
    quality: Mapped[str] = mapped_column(Text, default="good", server_default="good")


class EquipmentStateInterval(Base):
    """State intervals of equipment and lines (``end_ts`` NULL = still open)."""

    __tablename__ = "equipment_state"
    __table_args__ = (CheckConstraint(_in("state", *STATES), name="state"),)

    entity: Mapped[str] = mapped_column(Text, primary_key=True)
    start_ts: Mapped[datetime] = mapped_column(Tz, primary_key=True)
    end_ts: Mapped[datetime | None] = mapped_column(Tz)
    entity_type: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text)
    reason_code: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)


class UnitEvent(Base):
    """A unit (body) leaving a line."""

    __tablename__ = "unit_event"
    __table_args__ = (
        CheckConstraint(_in("result", "pass", "defect", "rework_pass", "scrap"), name="result"),
    )

    line: Mapped[str] = mapped_column(Text, primary_key=True)
    body_id: Mapped[str] = mapped_column(Text, primary_key=True)
    ts: Mapped[datetime] = mapped_column(Tz, primary_key=True)
    product: Mapped[str] = mapped_column(Text)
    result: Mapped[str] = mapped_column(Text)
    defect_code: Mapped[str | None] = mapped_column(Text)


class Telemetry(Base):
    __tablename__ = "telemetry"

    equipment: Mapped[str] = mapped_column(Text, primary_key=True)
    signal: Mapped[str] = mapped_column(Text, primary_key=True)
    ts: Mapped[datetime] = mapped_column(Tz, primary_key=True)
    value: Mapped[float] = mapped_column(Double)
    quality: Mapped[str] = mapped_column(Text, default="good", server_default="good")


class BufferLevel(Base):
    __tablename__ = "buffer_level"

    buffer: Mapped[str] = mapped_column(Text, primary_key=True)
    ts: Mapped[datetime] = mapped_column(Tz, primary_key=True)
    level: Mapped[int] = mapped_column(Integer)


class CkdStock(Base):
    __tablename__ = "ckd_stock"

    product: Mapped[str] = mapped_column(Text, primary_key=True)
    ts: Mapped[datetime] = mapped_column(Tz, primary_key=True)
    kits: Mapped[int] = mapped_column(Integer)


class Prediction(Base):
    __tablename__ = "prediction"
    __table_args__ = (Index("ix_prediction_equipment_ts", "equipment", text("ts DESC")),)

    equipment: Mapped[str] = mapped_column(Text, primary_key=True)
    horizon_h: Mapped[float] = mapped_column(Double, primary_key=True)
    ts: Mapped[datetime] = mapped_column(Tz, primary_key=True)
    p_failure: Mapped[float] = mapped_column(Double)
    health_index: Mapped[float] = mapped_column(Double)
    model_version: Mapped[str] = mapped_column(Text)
    top_factors: Mapped[list[dict[str, Any]] | None] = mapped_column(Json)


HYPERTABLES: dict[str, tuple[str, str]] = {
    "event_raw": ("ts", "1 day"),
    "telemetry": ("ts", "1 day"),
    "equipment_state": ("start_ts", "7 days"),
    "unit_event": ("ts", "7 days"),
    "buffer_level": ("ts", "1 day"),
    "ckd_stock": ("ts", "30 days"),
    "prediction": ("ts", "7 days"),
}
"""Hypertable -> (time column, chunk interval); used by the migration."""


# =========================================================================== regular tables


class AppUser(Base):
    __tablename__ = "app_user"

    id: Mapped[int] = _pk_id()
    username: Mapped[str] = mapped_column(Text, unique=True)
    display_name: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text)
    lang: Mapped[str] = mapped_column(Text, default="ru", server_default="ru")
    password_hash: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())


class ImportJob(Base):
    __tablename__ = "import_job"
    __table_args__ = (
        CheckConstraint(_in("kind", "docx", "xlsx", "csv"), name="kind"),
        CheckConstraint(_in("status", "processing", "done", "failed"), name="status"),
    )

    id: Mapped[int] = _pk_id()
    filename: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(Text, unique=True)
    kind: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    created_by: Mapped[int | None] = _user_fk()
    created_ts: Mapped[datetime] = mapped_column(Tz)
    result: Mapped[dict[str, Any] | None] = mapped_column(Json)


class Downtime(Base):
    __tablename__ = "downtime"
    __table_args__ = (
        CheckConstraint(_in("reason_source", "auto", "operator", "import"), name="reason_source"),
        Index(None, "line", "start_ts"),
        Index(None, "entity", "start_ts"),
        Index(None, "shift_date", "line"),
        Index(
            "ux_downtime_engine_key",
            "entity",
            "start_ts",
            unique=True,
            postgresql_where=text("import_id IS NULL AND start_ts IS NOT NULL"),
        ),
    )

    id: Mapped[int] = _pk_id()
    entity: Mapped[str] = mapped_column(Text)
    line: Mapped[str] = mapped_column(Text)
    start_ts: Mapped[datetime | None] = mapped_column(Tz)
    """NULL for imported journal entries (only the day is known, D1)."""
    end_ts: Mapped[datetime | None] = mapped_column(Tz)
    duration_s: Mapped[float | None] = mapped_column(Double)
    planned: Mapped[bool] = mapped_column(Boolean)
    microstop: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    reason_code: Mapped[str] = mapped_column(Text)
    reason_source: Mapped[str] = mapped_column(Text)
    shift_date: Mapped[date | None] = mapped_column(Date)
    shift_code: Mapped[str | None] = mapped_column(Text)
    comment: Mapped[str | None] = mapped_column(Text)
    classified_by: Mapped[int | None] = _user_fk()
    classified_ts: Mapped[datetime | None] = mapped_column(Tz)
    import_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("import_job.id", ondelete="CASCADE")
    )


class Defect(Base):
    __tablename__ = "defect"
    __table_args__ = (Index(None, "line", "ts"),)

    id: Mapped[int] = _pk_id()
    ts: Mapped[datetime] = mapped_column(Tz)
    line: Mapped[str] = mapped_column(Text)
    area: Mapped[str] = mapped_column(Text)
    equipment: Mapped[str | None] = mapped_column(Text)
    body_id: Mapped[str | None] = mapped_column(Text)
    defect_code: Mapped[str] = mapped_column(Text)
    qty: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    disposition: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)
    created_by: Mapped[int | None] = _user_fk()


class ShiftReport(Base):
    __tablename__ = "shift_report"
    __table_args__ = (
        UniqueConstraint("line", "shift_date", "shift_code"),
        CheckConstraint(_in("source", "import", "manual"), name="source"),
    )

    id: Mapped[int] = _pk_id()
    import_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("import_job.id", ondelete="SET NULL")
    )
    line: Mapped[str] = mapped_column(Text)
    shift_date: Mapped[date] = mapped_column(Date)
    shift_code: Mapped[str] = mapped_column(Text)
    plan_qty: Mapped[int | None] = mapped_column(Integer)
    produced_qty: Mapped[int] = mapped_column(Integer)
    defect_qty: Mapped[int] = mapped_column(Integer)
    worked_min: Mapped[float] = mapped_column(Double)
    reported_load_pct: Mapped[float | None] = mapped_column(Double)
    reported_defect_pct: Mapped[float | None] = mapped_column(Double)
    source: Mapped[str] = mapped_column(Text)


class KpiShift(Base):
    """KPIs of a line for a shift; recalculation adds a new ``version`` (FR-KPI-04)."""

    __tablename__ = "kpi_shift"
    __table_args__ = (CheckConstraint(_in("source", "events", "import"), name="source"),)

    line: Mapped[str] = mapped_column(Text, primary_key=True)
    shift_date: Mapped[date] = mapped_column(Date, primary_key=True)
    shift_code: Mapped[str] = mapped_column(Text, primary_key=True)
    source: Mapped[str] = mapped_column(Text, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    final: Mapped[bool] = mapped_column(Boolean)
    pot: Mapped[float] = mapped_column(Double)
    pdot: Mapped[float] = mapped_column(Double)
    pbt: Mapped[float] = mapped_column(Double)
    apt: Mapped[float] = mapped_column(Double)
    adot: Mapped[float] = mapped_column(Double)
    adet: Mapped[float] = mapped_column(Double)
    aust: Mapped[float] = mapped_column(Double)
    microstop_min: Mapped[float] = mapped_column(Double)
    pq: Mapped[int] = mapped_column(Integer)
    gq: Mapped[int] = mapped_column(Integer)
    pri_good_s: Mapped[float] = mapped_column(Double)
    availability: Mapped[float | None] = mapped_column(Double)
    effectiveness: Mapped[float | None] = mapped_column(Double)
    quality_ratio: Mapped[float | None] = mapped_column(Double)
    oee: Mapped[float | None] = mapped_column(Double)
    fpy: Mapped[float | None] = mapped_column(Double)
    defect_rate: Mapped[float | None] = mapped_column(Double)
    failures: Mapped[int | None] = mapped_column(Integer)
    repair_min: Mapped[float | None] = mapped_column(Double)
    computed_ts: Mapped[datetime] = mapped_column(Tz)


class BottleneckShift(Base):
    __tablename__ = "bottleneck_shift"

    line_group: Mapped[str] = mapped_column(Text, primary_key=True)
    shift_date: Mapped[date] = mapped_column(Date, primary_key=True)
    shift_code: Mapped[str] = mapped_column(Text, primary_key=True)
    line: Mapped[str] = mapped_column(Text, primary_key=True)
    sole_share: Mapped[float] = mapped_column(Double)
    shifting_share: Mapped[float] = mapped_column(Double)


class AlertRow(Base):
    __tablename__ = "alert"
    __table_args__ = (
        CheckConstraint(_in("severity", *SEVERITIES), name="severity"),
        CheckConstraint(_in("status", "open", "ack", "resolved"), name="status"),
        Index(None, "status", "ts"),
        Index(None, "entity", "ts"),
    )

    id: Mapped[int] = _pk_id()
    ts: Mapped[datetime] = mapped_column(Tz)
    rule_id: Mapped[str] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(Text)
    entity_type: Mapped[str] = mapped_column(Text)
    entity: Mapped[str] = mapped_column(Text)
    title_ru: Mapped[str] = mapped_column(Text)
    title_kk: Mapped[str | None] = mapped_column(Text)
    message_ru: Mapped[str] = mapped_column(Text)
    message_kk: Mapped[str | None] = mapped_column(Text)
    value: Mapped[Any] = mapped_column(Json)
    status: Mapped[str] = mapped_column(Text, default="open", server_default="open")
    ack_by: Mapped[int | None] = _user_fk()
    ack_ts: Mapped[datetime | None] = mapped_column(Tz)
    resolved_ts: Mapped[datetime | None] = mapped_column(Tz)
    escalation_level: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0")
    dedup_key: Mapped[str] = mapped_column(Text, unique=True)


class AlertNotification(Base):
    __tablename__ = "alert_notification"

    id: Mapped[int] = _pk_id()
    alert_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("alert.id", ondelete="CASCADE"), index=True
    )
    channel: Mapped[str] = mapped_column(Text)
    recipient: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    sent_ts: Mapped[datetime | None] = mapped_column(Tz)
    error: Mapped[str | None] = mapped_column(Text)


class DqIssueRow(Base):
    __tablename__ = "dq_issue"
    __table_args__ = (
        CheckConstraint(_in("severity", *SEVERITIES), name="severity"),
        CheckConstraint(_in("status", "open", "resolved"), name="status"),
        Index(None, "status", "ts"),
    )

    id: Mapped[int] = _pk_id()
    ts: Mapped[datetime] = mapped_column(Tz)
    rule_id: Mapped[str] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(Text)
    entity: Mapped[str] = mapped_column(Text)
    period_date: Mapped[date | None] = mapped_column(Date)
    details: Mapped[dict[str, Any]] = mapped_column(Json)
    status: Mapped[str] = mapped_column(Text, default="open", server_default="open")
    resolved_by: Mapped[int | None] = _user_fk()
    comment: Mapped[str | None] = mapped_column(Text)
    import_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("import_job.id", ondelete="CASCADE"), index=True
    )
    dedup_key: Mapped[str | None] = mapped_column(Text, unique=True)
    """Live findings (engine, collector DQ-07) are upserted by key; imports keep NULL."""


class EngineCheckpoint(Base):
    """Restart point of the engine: stream position, last event time, state snapshot (NFR-03)."""

    __tablename__ = "engine_checkpoint"

    name: Mapped[str] = mapped_column(Text, primary_key=True)
    stream_id: Mapped[str | None] = mapped_column(Text)
    event_ts: Mapped[datetime | None] = mapped_column(Tz)
    state: Mapped[dict[str, Any] | None] = mapped_column(Json)
    updated_ts: Mapped[datetime] = mapped_column(Tz)


class ForecastRun(Base):
    __tablename__ = "forecast_run"
    __table_args__ = (CheckConstraint(_in("mode", "fast", "des"), name="mode"),)

    id: Mapped[int] = _pk_id()
    created_ts: Mapped[datetime] = mapped_column(Tz)
    created_by: Mapped[int | None] = _user_fk()
    mode: Mapped[str] = mapped_column(Text)
    month: Mapped[str] = mapped_column(Text)
    overrides: Mapped[dict[str, Any]] = mapped_column(Json)
    n_runs: Mapped[int] = mapped_column(Integer)
    seed: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(Text)
    progress: Mapped[float] = mapped_column(Double, default=0.0, server_default="0")
    result: Mapped[dict[str, Any] | None] = mapped_column(Json)
    duration_ms: Mapped[int | None] = mapped_column(Integer)


class CalibrationSnapshot(Base):
    __tablename__ = "calibration_snapshot"

    id: Mapped[int] = _pk_id()
    ts: Mapped[datetime] = mapped_column(Tz)
    window_days: Mapped[int] = mapped_column(Integer)
    params: Mapped[dict[str, Any]] = mapped_column(Json)


class WorkOrder(Base):
    __tablename__ = "work_order"
    __table_args__ = (
        CheckConstraint(_in("kind", "corrective", "preventive", "predictive"), name="kind"),
        CheckConstraint(_in("status", "open", "in_progress", "done", "cancelled"), name="status"),
        Index("ix_work_order_status_created_ts", "status", text("created_ts DESC")),
        Index(
            "uq_work_order_active_alert",
            "alert_id",
            unique=True,
            postgresql_where=text("status IN ('open', 'in_progress')"),
        ),
    )

    id: Mapped[int] = _pk_id()
    equipment: Mapped[str] = mapped_column(Text, index=True)
    alert_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("alert.id", ondelete="SET NULL")
    )
    kind: Mapped[str] = mapped_column(Text)
    priority: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="open", server_default="open")
    title: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    assignee: Mapped[int | None] = _user_fk()
    created_by: Mapped[int | None] = _user_fk()
    created_ts: Mapped[datetime] = mapped_column(Tz)
    due_ts: Mapped[datetime | None] = mapped_column(Tz)
    closed_ts: Mapped[datetime | None] = mapped_column(Tz)


class Report(Base):
    __tablename__ = "report"
    __table_args__ = (
        CheckConstraint(_in("generated_by", "llm", "template"), name="generated_by"),
        Index(None, "shift_date", "shift_code"),
    )

    id: Mapped[int] = _pk_id()
    kind: Mapped[str] = mapped_column(Text)
    shift_date: Mapped[date | None] = mapped_column(Date)
    shift_code: Mapped[str | None] = mapped_column(Text)
    lang: Mapped[str] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    generated_by: Mapped[str] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    numbers_verified: Mapped[bool] = mapped_column(Boolean)
    input: Mapped[dict[str, Any]] = mapped_column(Json)
    created_ts: Mapped[datetime] = mapped_column(Tz)


class TelegramSubscription(Base):
    __tablename__ = "telegram_subscription"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    role: Mapped[str] = mapped_column(Text)
    created_ts: Mapped[datetime] = mapped_column(Tz)
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[Any] = mapped_column(Json)
    updated_by: Mapped[int | None] = _user_fk()
    updated_ts: Mapped[datetime] = mapped_column(Tz)


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (Index(None, "entity_type", "entity_id"), Index(None, "ts"))

    id: Mapped[int] = _pk_id()
    ts: Mapped[datetime] = mapped_column(Tz)
    user_id: Mapped[int | None] = _user_fk()
    action: Mapped[str] = mapped_column(Text)
    entity_type: Mapped[str] = mapped_column(Text)
    entity_id: Mapped[str | None] = mapped_column(Text)
    before: Mapped[dict[str, Any] | None] = mapped_column(Json)
    after: Mapped[dict[str, Any] | None] = mapped_column(Json)


class CopilotLog(Base):
    """Copilot requests and answers (SPEC §11.5: kept for 30 days)."""

    __tablename__ = "copilot_log"
    __table_args__ = (
        CheckConstraint(_in("mode", "llm", "offline"), name="mode"),
        Index("ix_copilot_log_ts", text("ts DESC")),
    )

    id: Mapped[int] = _pk_id()
    ts: Mapped[datetime] = mapped_column(Tz)
    user_id: Mapped[int | None] = _user_fk()
    username: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text)
    lang: Mapped[str] = mapped_column(Text)
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    refused: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    tool_calls: Mapped[list[dict[str, Any]]] = mapped_column(Json)
    error: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int] = mapped_column(Integer)
