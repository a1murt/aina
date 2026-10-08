"""M3 collector and engine: engine checkpoint, DQ dedup key, idempotent engine downtime.

* ``engine_checkpoint``: the engine's restart point (stream position, last event time and the
  JSON snapshot of its state), written in the same transaction as the derived rows (NFR-03);
  ``baseline`` holds the state at ``demo_start`` for the demo reset.
* ``dq_issue.dedup_key``: live DQ findings (engine, collector DQ-07) are upserted by key; imported
  findings keep NULL (NULLs are distinct).
* unique ``downtime(entity, start_ts)`` for engine rows (``import_id`` NULL): replays and restarts
  upsert instead of duplicating.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "engine_checkpoint",
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("stream_id", sa.Text(), nullable=True),
        sa.Column("event_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "state", postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True
        ),
        sa.Column("updated_ts", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("name", name=op.f("pk_engine_checkpoint")),
    )
    op.add_column("dq_issue", sa.Column("dedup_key", sa.Text(), nullable=True))
    op.create_unique_constraint(op.f("uq_dq_issue_dedup_key"), "dq_issue", ["dedup_key"])
    op.create_index(
        "ux_downtime_engine_key",
        "downtime",
        ["entity", "start_ts"],
        unique=True,
        postgresql_where=sa.text("import_id IS NULL AND start_ts IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ux_downtime_engine_key", table_name="downtime")
    op.drop_constraint(op.f("uq_dq_issue_dedup_key"), "dq_issue", type_="unique")
    op.drop_column("dq_issue", "dedup_key")
    op.drop_table("engine_checkpoint")
