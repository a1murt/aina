"""Whole storage schema of SPEC §8: reference, calendar/plan, hypertables, regular tables.

Tables mirror ``twin_core.db.models``. The TimescaleDB part is hand-written: hypertables (time
column in every unique key), compression after 7 days and retention of 30 days for ``event_raw``
and ``telemetry`` (NFR-09), real-time continuous aggregates ``telemetry_15m`` / ``telemetry_1h``
(avg, min, max, last). Other tables keep their data (downtime/defects/KPI: 2 years — pruning is
an ops job, not a TimescaleDB policy, since they are regular tables).

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Frozen copy (a migration must not change when the models do): table -> (time column, chunk).
HYPERTABLES = {
    "event_raw": ("ts", "1 day"),
    "telemetry": ("ts", "1 day"),
    "equipment_state": ("start_ts", "7 days"),
    "unit_event": ("ts", "7 days"),
    "buffer_level": ("ts", "1 day"),
    "ckd_stock": ("ts", "30 days"),
    "prediction": ("ts", "7 days"),
}
COMPRESSED = {
    # table: (segment by, order by)
    "event_raw": ("entity", "ts DESC, event_id"),
    "telemetry": ("equipment, signal", "ts DESC"),
}
COMPRESS_AFTER = "7 days"
RETAIN = "30 days"
CONTINUOUS_AGGREGATES = (
    # view, bucket, refresh start offset, end offset, schedule
    ("telemetry_15m", "15 minutes", "3 days", "15 minutes", "15 minutes"),
    ("telemetry_1h", "1 hour", "7 days", "1 hour", "1 hour"),
)


def _timescale_upgrade() -> None:
    for table, (column, chunk) in HYPERTABLES.items():
        op.execute(
            f"SELECT create_hypertable('{table}', by_range('{column}', INTERVAL '{chunk}'), "
            "create_default_indexes => false)"
        )
    for table, (segment_by, order_by) in COMPRESSED.items():
        op.execute(
            f"ALTER TABLE {table} SET (timescaledb.compress, "
            f"timescaledb.compress_segmentby = '{segment_by}', "
            f"timescaledb.compress_orderby = '{order_by}')"
        )
        op.execute(f"SELECT add_compression_policy('{table}', INTERVAL '{COMPRESS_AFTER}')")
        op.execute(f"SELECT add_retention_policy('{table}', INTERVAL '{RETAIN}')")
    for view, bucket, start, end, schedule in CONTINUOUS_AGGREGATES:
        # Real-time aggregates (materialized_only = false): plant time in sim mode can run ahead
        # of the wall clock the refresh policy is based on; the tail is read from raw data.
        op.execute(
            f"CREATE MATERIALIZED VIEW {view} "
            "WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS "
            f"SELECT time_bucket(INTERVAL '{bucket}', ts) AS bucket, equipment, signal, "
            "avg(value) AS avg, min(value) AS min, max(value) AS max, "
            "last(value, ts) AS last, count(*) AS n "
            "FROM telemetry GROUP BY bucket, equipment, signal WITH NO DATA"
        )
        op.execute(
            f"SELECT add_continuous_aggregate_policy('{view}', "
            f"start_offset => INTERVAL '{start}', end_offset => INTERVAL '{end}', "
            f"schedule_interval => INTERVAL '{schedule}')"
        )


def upgrade() -> None:
    op.create_table(
        "app_user",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("lang", sa.Text(), server_default="ru", nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_app_user")),
        sa.UniqueConstraint("username", name=op.f("uq_app_user_username")),
    )
    op.create_table(
        "asset_area",
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("name_ru", sa.Text(), nullable=False),
        sa.Column("name_kk", sa.Text(), nullable=True),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("code", name=op.f("pk_asset_area")),
    )
    op.create_table(
        "bottleneck_shift",
        sa.Column("line_group", sa.Text(), nullable=False),
        sa.Column("shift_date", sa.Date(), nullable=False),
        sa.Column("shift_code", sa.Text(), nullable=False),
        sa.Column("line", sa.Text(), nullable=False),
        sa.Column("sole_share", sa.Double(), nullable=False),
        sa.Column("shifting_share", sa.Double(), nullable=False),
        sa.PrimaryKeyConstraint(
            "line_group", "shift_date", "shift_code", "line", name=op.f("pk_bottleneck_shift")
        ),
    )
    op.create_table(
        "buffer_level",
        sa.Column("buffer", sa.Text(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("buffer", "ts", name=op.f("pk_buffer_level")),
    )
    op.create_table(
        "calibration_snapshot",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_days", sa.Integer(), nullable=False),
        sa.Column(
            "params", postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_calibration_snapshot")),
    )
    op.create_table(
        "ckd_stock",
        sa.Column("product", sa.Text(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("kits", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("product", "ts", name=op.f("pk_ckd_stock")),
    )
    op.create_table(
        "defect_code",
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("area", sa.Text(), nullable=False),
        sa.Column("name_ru", sa.Text(), nullable=False),
        sa.Column("name_kk", sa.Text(), nullable=True),
        sa.Column("disposition", sa.Text(), nullable=False),
        sa.Column("rework_min", sa.Double(), nullable=False),
        sa.Column("repaint", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.PrimaryKeyConstraint("code", name=op.f("pk_defect_code")),
    )
    op.create_table(
        "equipment_state",
        sa.Column("entity", sa.Text(), nullable=False),
        sa.Column("start_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("entity_type", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("reason_code", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "state IN ('RUNNING', 'DEGRADED', 'STARVED', 'BLOCKED', 'DOWN_UNPLANNED', "
            "'DOWN_PLANNED', 'CHANGEOVER', 'IDLE_NO_PLAN')",
            name=op.f("ck_equipment_state_state"),
        ),
        sa.PrimaryKeyConstraint("entity", "start_ts", name=op.f("pk_equipment_state")),
    )
    op.create_table(
        "event_raw",
        sa.Column("event_id", sa.Text(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("entity_type", sa.Text(), nullable=False),
        sa.Column("entity", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column(
            "data", postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=False
        ),
        sa.Column("quality", sa.Text(), server_default="good", nullable=False),
        sa.PrimaryKeyConstraint("event_id", "ts", name=op.f("pk_event_raw")),
    )
    op.create_index(
        "ix_event_raw_entity_ts",
        "event_raw",
        ["entity", sa.literal_column("ts DESC")],
        unique=False,
    )
    op.create_table(
        "kpi_shift",
        sa.Column("line", sa.Text(), nullable=False),
        sa.Column("shift_date", sa.Date(), nullable=False),
        sa.Column("shift_code", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("final", sa.Boolean(), nullable=False),
        sa.Column("pot", sa.Double(), nullable=False),
        sa.Column("pdot", sa.Double(), nullable=False),
        sa.Column("pbt", sa.Double(), nullable=False),
        sa.Column("apt", sa.Double(), nullable=False),
        sa.Column("adot", sa.Double(), nullable=False),
        sa.Column("adet", sa.Double(), nullable=False),
        sa.Column("aust", sa.Double(), nullable=False),
        sa.Column("microstop_min", sa.Double(), nullable=False),
        sa.Column("pq", sa.Integer(), nullable=False),
        sa.Column("gq", sa.Integer(), nullable=False),
        sa.Column("pri_good_s", sa.Double(), nullable=False),
        sa.Column("availability", sa.Double(), nullable=True),
        sa.Column("effectiveness", sa.Double(), nullable=True),
        sa.Column("quality_ratio", sa.Double(), nullable=True),
        sa.Column("oee", sa.Double(), nullable=True),
        sa.Column("fpy", sa.Double(), nullable=True),
        sa.Column("defect_rate", sa.Double(), nullable=True),
        sa.Column("failures", sa.Integer(), nullable=True),
        sa.Column("repair_min", sa.Double(), nullable=True),
        sa.Column("computed_ts", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("source IN ('events', 'import')", name=op.f("ck_kpi_shift_source")),
        sa.PrimaryKeyConstraint(
            "line", "shift_date", "shift_code", "source", "version", name=op.f("pk_kpi_shift")
        ),
    )
    op.create_table(
        "prediction",
        sa.Column("equipment", sa.Text(), nullable=False),
        sa.Column("horizon_h", sa.Double(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("p_failure", sa.Double(), nullable=False),
        sa.Column("health_index", sa.Double(), nullable=False),
        sa.Column("model_version", sa.Text(), nullable=False),
        sa.Column(
            "top_factors", postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True
        ),
        sa.PrimaryKeyConstraint("equipment", "horizon_h", "ts", name=op.f("pk_prediction")),
    )
    op.create_table(
        "product",
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("cycle_factor", sa.Double(), nullable=False),
        sa.PrimaryKeyConstraint("code", name=op.f("pk_product")),
    )
    op.create_table(
        "reason_code",
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("name_ru", sa.Text(), nullable=False),
        sa.Column("name_kk", sa.Text(), nullable=True),
        sa.Column("planned", sa.Boolean(), nullable=False),
        sa.Column("bucket", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("code", name=op.f("pk_reason_code")),
    )
    op.create_table(
        "report",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("shift_date", sa.Date(), nullable=True),
        sa.Column("shift_code", sa.Text(), nullable=True),
        sa.Column("lang", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("generated_by", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("numbers_verified", sa.Boolean(), nullable=False),
        sa.Column(
            "input", postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=False
        ),
        sa.Column("created_ts", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "generated_by IN ('llm', 'template')", name=op.f("ck_report_generated_by")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_report")),
    )
    op.create_index(
        op.f("ix_report_shift_date_shift_code"),
        "report",
        ["shift_date", "shift_code"],
        unique=False,
    )
    op.create_table(
        "shift",
        sa.Column("shift_date", sa.Date(), nullable=False),
        sa.Column("shift_code", sa.Text(), nullable=False),
        sa.Column("start_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("working", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("shift_date", "shift_code", name=op.f("pk_shift")),
    )
    op.create_table(
        "telegram_subscription",
        sa.Column("chat_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("created_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.PrimaryKeyConstraint("chat_id", name=op.f("pk_telegram_subscription")),
    )
    op.create_table(
        "telemetry",
        sa.Column("equipment", sa.Text(), nullable=False),
        sa.Column("signal", sa.Text(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("value", sa.Double(), nullable=False),
        sa.Column("quality", sa.Text(), server_default="good", nullable=False),
        sa.PrimaryKeyConstraint("equipment", "signal", "ts", name=op.f("pk_telemetry")),
    )
    op.create_table(
        "unit_event",
        sa.Column("line", sa.Text(), nullable=False),
        sa.Column("body_id", sa.Text(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("product", sa.Text(), nullable=False),
        sa.Column("result", sa.Text(), nullable=False),
        sa.Column("defect_code", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "result IN ('pass', 'defect', 'rework_pass', 'scrap')",
            name=op.f("ck_unit_event_result"),
        ),
        sa.PrimaryKeyConstraint("line", "body_id", "ts", name=op.f("pk_unit_event")),
    )
    op.create_table(
        "alert",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rule_id", sa.Text(), nullable=False),
        sa.Column("severity", sa.Text(), nullable=False),
        sa.Column("entity_type", sa.Text(), nullable=False),
        sa.Column("entity", sa.Text(), nullable=False),
        sa.Column("title_ru", sa.Text(), nullable=False),
        sa.Column("title_kk", sa.Text(), nullable=True),
        sa.Column("message_ru", sa.Text(), nullable=False),
        sa.Column("message_kk", sa.Text(), nullable=True),
        sa.Column(
            "value", postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=False
        ),
        sa.Column("status", sa.Text(), server_default="open", nullable=False),
        sa.Column("ack_by", sa.BigInteger(), nullable=True),
        sa.Column("ack_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("escalation_level", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("dedup_key", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "severity IN ('info', 'warning', 'critical')", name=op.f("ck_alert_severity")
        ),
        sa.CheckConstraint("status IN ('open', 'ack', 'resolved')", name=op.f("ck_alert_status")),
        sa.ForeignKeyConstraint(
            ["ack_by"], ["app_user.id"], name=op.f("fk_alert_ack_by_app_user"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_alert")),
        sa.UniqueConstraint("dedup_key", name=op.f("uq_alert_dedup_key")),
    )
    op.create_index(op.f("ix_alert_entity_ts"), "alert", ["entity", "ts"], unique=False)
    op.create_index(op.f("ix_alert_status_ts"), "alert", ["status", "ts"], unique=False)
    op.create_table(
        "asset_line",
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("area", sa.Text(), nullable=False),
        sa.Column("name_ru", sa.Text(), nullable=False),
        sa.Column("ict_seconds", sa.Double(), nullable=False),
        sa.Column("plan_rate_per_shift", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["area"], ["asset_area.code"], name=op.f("fk_asset_line_area_asset_area")
        ),
        sa.PrimaryKeyConstraint("code", name=op.f("pk_asset_line")),
    )
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("entity_type", sa.Text(), nullable=False),
        sa.Column("entity_id", sa.Text(), nullable=True),
        sa.Column(
            "before", postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "after", postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name=op.f("fk_audit_log_user_id_app_user"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
    )
    op.create_index(
        op.f("ix_audit_log_entity_type_entity_id"),
        "audit_log",
        ["entity_type", "entity_id"],
        unique=False,
    )
    op.create_index(op.f("ix_audit_log_ts"), "audit_log", ["ts"], unique=False)
    op.create_table(
        "defect",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("line", sa.Text(), nullable=False),
        sa.Column("area", sa.Text(), nullable=False),
        sa.Column("equipment", sa.Text(), nullable=True),
        sa.Column("body_id", sa.Text(), nullable=True),
        sa.Column("defect_code", sa.Text(), nullable=False),
        sa.Column("qty", sa.Integer(), server_default="1", nullable=False),
        sa.Column("disposition", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["app_user.id"],
            name=op.f("fk_defect_created_by_app_user"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_defect")),
    )
    op.create_index(op.f("ix_defect_line_ts"), "defect", ["line", "ts"], unique=False)
    op.create_table(
        "forecast_run",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("created_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("month", sa.Text(), nullable=False),
        sa.Column(
            "overrides", postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=False
        ),
        sa.Column("n_runs", sa.Integer(), nullable=False),
        sa.Column("seed", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("progress", sa.Double(), server_default="0", nullable=False),
        sa.Column(
            "result", postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True
        ),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.CheckConstraint("mode IN ('fast', 'des')", name=op.f("ck_forecast_run_mode")),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["app_user.id"],
            name=op.f("fk_forecast_run_created_by_app_user"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_forecast_run")),
    )
    op.create_table(
        "import_job",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("created_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "result", postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True
        ),
        sa.CheckConstraint("kind IN ('docx', 'xlsx', 'csv')", name=op.f("ck_import_job_kind")),
        sa.CheckConstraint(
            "status IN ('processing', 'done', 'failed')", name=op.f("ck_import_job_status")
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["app_user.id"],
            name=op.f("fk_import_job_created_by_app_user"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_import_job")),
        sa.UniqueConstraint("sha256", name=op.f("uq_import_job_sha256")),
    )
    op.create_table(
        "settings",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column(
            "value", postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=False
        ),
        sa.Column("updated_by", sa.BigInteger(), nullable=True),
        sa.Column("updated_ts", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["updated_by"],
            ["app_user.id"],
            name=op.f("fk_settings_updated_by_app_user"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_settings")),
    )
    op.create_table(
        "alert_notification",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("alert_id", sa.BigInteger(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("recipient", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("sent_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["alert_id"],
            ["alert.id"],
            name=op.f("fk_alert_notification_alert_id_alert"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_alert_notification")),
    )
    op.create_index(
        op.f("ix_alert_notification_alert_id"), "alert_notification", ["alert_id"], unique=False
    )
    op.create_table(
        "asset_buffer",
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("from_line", sa.Text(), nullable=False),
        sa.Column("to_line", sa.Text(), nullable=False),
        sa.Column("capacity", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["from_line"], ["asset_line.code"], name=op.f("fk_asset_buffer_from_line_asset_line")
        ),
        sa.ForeignKeyConstraint(
            ["to_line"], ["asset_line.code"], name=op.f("fk_asset_buffer_to_line_asset_line")
        ),
        sa.PrimaryKeyConstraint("code", name=op.f("pk_asset_buffer")),
    )
    op.create_table(
        "asset_equipment",
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("line", sa.Text(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("criticality", sa.Text(), nullable=False),
        sa.Column("degraded_capacity", sa.Double(), nullable=False),
        sa.Column("name_ru", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "criticality IN ('A', 'B', 'C')", name=op.f("ck_asset_equipment_criticality")
        ),
        sa.ForeignKeyConstraint(
            ["line"], ["asset_line.code"], name=op.f("fk_asset_equipment_line_asset_line")
        ),
        sa.PrimaryKeyConstraint("code", name=op.f("pk_asset_equipment")),
    )
    op.create_table(
        "downtime",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("entity", sa.Text(), nullable=False),
        sa.Column("line", sa.Text(), nullable=False),
        sa.Column("start_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_s", sa.Double(), nullable=True),
        sa.Column("planned", sa.Boolean(), nullable=False),
        sa.Column("microstop", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("reason_code", sa.Text(), nullable=False),
        sa.Column("reason_source", sa.Text(), nullable=False),
        sa.Column("shift_date", sa.Date(), nullable=True),
        sa.Column("shift_code", sa.Text(), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("classified_by", sa.BigInteger(), nullable=True),
        sa.Column("classified_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("import_id", sa.BigInteger(), nullable=True),
        sa.CheckConstraint(
            "reason_source IN ('auto', 'operator', 'import')",
            name=op.f("ck_downtime_reason_source"),
        ),
        sa.ForeignKeyConstraint(
            ["classified_by"],
            ["app_user.id"],
            name=op.f("fk_downtime_classified_by_app_user"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["import_id"],
            ["import_job.id"],
            name=op.f("fk_downtime_import_id_import_job"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_downtime")),
    )
    op.create_index(
        op.f("ix_downtime_entity_start_ts"), "downtime", ["entity", "start_ts"], unique=False
    )
    op.create_index(
        op.f("ix_downtime_line_start_ts"), "downtime", ["line", "start_ts"], unique=False
    )
    op.create_index(
        op.f("ix_downtime_shift_date_line"), "downtime", ["shift_date", "line"], unique=False
    )
    op.create_table(
        "dq_issue",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rule_id", sa.Text(), nullable=False),
        sa.Column("severity", sa.Text(), nullable=False),
        sa.Column("entity", sa.Text(), nullable=False),
        sa.Column("period_date", sa.Date(), nullable=True),
        sa.Column(
            "details", postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=False
        ),
        sa.Column("status", sa.Text(), server_default="open", nullable=False),
        sa.Column("resolved_by", sa.BigInteger(), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("import_id", sa.BigInteger(), nullable=True),
        sa.CheckConstraint(
            "severity IN ('info', 'warning', 'critical')", name=op.f("ck_dq_issue_severity")
        ),
        sa.CheckConstraint("status IN ('open', 'resolved')", name=op.f("ck_dq_issue_status")),
        sa.ForeignKeyConstraint(
            ["import_id"],
            ["import_job.id"],
            name=op.f("fk_dq_issue_import_id_import_job"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["resolved_by"],
            ["app_user.id"],
            name=op.f("fk_dq_issue_resolved_by_app_user"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dq_issue")),
    )
    op.create_index(op.f("ix_dq_issue_import_id"), "dq_issue", ["import_id"], unique=False)
    op.create_index(op.f("ix_dq_issue_status_ts"), "dq_issue", ["status", "ts"], unique=False)
    op.create_table(
        "production_plan",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("month", sa.Text(), nullable=False),
        sa.Column("level", sa.Text(), nullable=False),
        sa.Column("line", sa.Text(), nullable=True),
        sa.Column("product", sa.Text(), nullable=True),
        sa.Column("qty", sa.Integer(), nullable=False),
        sa.Column("import_id", sa.BigInteger(), nullable=True),
        sa.CheckConstraint(
            "level IN ('plant_target', 'line_model')", name=op.f("ck_production_plan_level")
        ),
        sa.ForeignKeyConstraint(
            ["import_id"],
            ["import_job.id"],
            name=op.f("fk_production_plan_import_id_import_job"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_production_plan")),
        sa.UniqueConstraint(
            "month",
            "level",
            "line",
            "product",
            name=op.f("uq_production_plan_month_level_line_product"),
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_table(
        "shift_report",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("import_id", sa.BigInteger(), nullable=True),
        sa.Column("line", sa.Text(), nullable=False),
        sa.Column("shift_date", sa.Date(), nullable=False),
        sa.Column("shift_code", sa.Text(), nullable=False),
        sa.Column("plan_qty", sa.Integer(), nullable=True),
        sa.Column("produced_qty", sa.Integer(), nullable=False),
        sa.Column("defect_qty", sa.Integer(), nullable=False),
        sa.Column("worked_min", sa.Double(), nullable=False),
        sa.Column("reported_load_pct", sa.Double(), nullable=True),
        sa.Column("reported_defect_pct", sa.Double(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.CheckConstraint("source IN ('import', 'manual')", name=op.f("ck_shift_report_source")),
        sa.ForeignKeyConstraint(
            ["import_id"],
            ["import_job.id"],
            name=op.f("fk_shift_report_import_id_import_job"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_shift_report")),
        sa.UniqueConstraint(
            "line",
            "shift_date",
            "shift_code",
            name=op.f("uq_shift_report_line_shift_date_shift_code"),
        ),
    )
    op.create_table(
        "work_order",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("equipment", sa.Text(), nullable=False),
        sa.Column("alert_id", sa.BigInteger(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("priority", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="open", nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("assignee", sa.BigInteger(), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("created_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("due_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_ts", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "kind IN ('corrective', 'preventive', 'predictive')", name=op.f("ck_work_order_kind")
        ),
        sa.CheckConstraint(
            "status IN ('open', 'in_progress', 'done', 'cancelled')",
            name=op.f("ck_work_order_status"),
        ),
        sa.ForeignKeyConstraint(
            ["alert_id"],
            ["alert.id"],
            name=op.f("fk_work_order_alert_id_alert"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["assignee"],
            ["app_user.id"],
            name=op.f("fk_work_order_assignee_app_user"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["app_user.id"],
            name=op.f("fk_work_order_created_by_app_user"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_work_order")),
    )
    op.create_index(op.f("ix_work_order_equipment"), "work_order", ["equipment"], unique=False)
    _timescale_upgrade()


def downgrade() -> None:
    for view, *_ in reversed(CONTINUOUS_AGGREGATES):
        op.execute(f"DROP MATERIALIZED VIEW IF EXISTS {view}")
    op.drop_index(op.f("ix_work_order_equipment"), table_name="work_order")
    op.drop_table("work_order")
    op.drop_table("shift_report")
    op.drop_table("production_plan")
    op.drop_index(op.f("ix_dq_issue_status_ts"), table_name="dq_issue")
    op.drop_index(op.f("ix_dq_issue_import_id"), table_name="dq_issue")
    op.drop_table("dq_issue")
    op.drop_index(op.f("ix_downtime_shift_date_line"), table_name="downtime")
    op.drop_index(op.f("ix_downtime_line_start_ts"), table_name="downtime")
    op.drop_index(op.f("ix_downtime_entity_start_ts"), table_name="downtime")
    op.drop_table("downtime")
    op.drop_table("asset_equipment")
    op.drop_table("asset_buffer")
    op.drop_index(op.f("ix_alert_notification_alert_id"), table_name="alert_notification")
    op.drop_table("alert_notification")
    op.drop_table("settings")
    op.drop_table("import_job")
    op.drop_table("forecast_run")
    op.drop_index(op.f("ix_defect_line_ts"), table_name="defect")
    op.drop_table("defect")
    op.drop_index(op.f("ix_audit_log_ts"), table_name="audit_log")
    op.drop_index(op.f("ix_audit_log_entity_type_entity_id"), table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_table("asset_line")
    op.drop_index(op.f("ix_alert_status_ts"), table_name="alert")
    op.drop_index(op.f("ix_alert_entity_ts"), table_name="alert")
    op.drop_table("alert")
    op.drop_table("unit_event")
    op.drop_table("telemetry")
    op.drop_table("telegram_subscription")
    op.drop_table("shift")
    op.drop_index(op.f("ix_report_shift_date_shift_code"), table_name="report")
    op.drop_table("report")
    op.drop_table("reason_code")
    op.drop_table("product")
    op.drop_table("prediction")
    op.drop_table("kpi_shift")
    op.drop_index("ix_event_raw_entity_ts", table_name="event_raw")
    op.drop_table("event_raw")
    op.drop_table("equipment_state")
    op.drop_table("defect_code")
    op.drop_table("ckd_stock")
    op.drop_table("calibration_snapshot")
    op.drop_table("buffer_level")
    op.drop_table("bottleneck_shift")
    op.drop_table("asset_area")
    op.drop_table("app_user")
