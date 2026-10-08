"""M7b predictive maintenance: indexes of ``prediction`` and ``work_order``.

* ``ix_prediction_equipment_ts``: the latest prediction of a unit (health endpoint, live list).
* ``ix_work_order_status_created_ts``: the work-order list by status.
* ``uq_work_order_active_alert``: one active work order per alert (a second click on «create
  work order» finds the first one instead of duplicating it).

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_prediction_equipment_ts", "prediction", ["equipment", sa.text("ts DESC")], unique=False
    )
    op.create_index(
        op.f("ix_work_order_status_created_ts"),
        "work_order",
        ["status", sa.text("created_ts DESC")],
        unique=False,
    )
    op.create_index(
        "uq_work_order_active_alert",
        "work_order",
        ["alert_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('open', 'in_progress')"),
    )


def downgrade() -> None:
    op.drop_index("uq_work_order_active_alert", table_name="work_order")
    op.drop_index(op.f("ix_work_order_status_created_ts"), table_name="work_order")
    op.drop_index("ix_prediction_equipment_ts", table_name="prediction")
