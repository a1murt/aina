"""M8b copilot: the request log (SPEC §11.5, kept for 30 days).

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "copilot_log",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("lang", sa.Text(), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("refused", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column(
            "tool_calls",
            postgresql.JSONB(none_as_null=True, astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.CheckConstraint("mode IN ('llm', 'offline')", name=op.f("ck_copilot_log_mode")),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name=op.f("fk_copilot_log_user_id_app_user"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_copilot_log")),
    )
    op.create_index(op.f("ix_copilot_log_ts"), "copilot_log", [sa.text("ts DESC")], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_copilot_log_ts"), table_name="copilot_log")
    op.drop_table("copilot_log")
