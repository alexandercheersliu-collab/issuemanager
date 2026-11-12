"""add questions.figure_specs for TikZ geometry figures

Revision ID: b52e0d8a4c21
Revises: a41d9c7e2f10
Create Date: 2026-11-02 00:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b52e0d8a4c21"
down_revision = "a41d9c7e2f10"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("questions", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "figure_specs",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'"),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("questions", schema=None) as batch_op:
        batch_op.drop_column("figure_specs")
