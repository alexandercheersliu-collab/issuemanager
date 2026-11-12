"""add questions.figure_paths for per-question cropped figures

Revision ID: a41d9c7e2f10
Revises: e7f2c91a3b55
Create Date: 2026-11-01 22:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "a41d9c7e2f10"
down_revision = "e7f2c91a3b55"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("questions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("figure_paths", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("questions", schema=None) as batch_op:
        batch_op.drop_column("figure_paths")
