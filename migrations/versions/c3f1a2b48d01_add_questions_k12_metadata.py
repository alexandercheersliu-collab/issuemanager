"""add questions K12 metadata columns

新增 K12 元数据列：subject / grade / region / textbook_version /
question_type / chapter / error_category / source_doc。
subject 带 server_default='math'，旧数据自动回填为 math。

Revision ID: c3f1a2b48d01
Revises: b98b7ec07b27
Create Date: 2026-09-26 10:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = 'c3f1a2b48d01'
down_revision = 'b98b7ec07b27'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('questions', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('subject', sa.String(length=32), server_default='math', nullable=False)
        )
        batch_op.add_column(sa.Column('grade', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('region', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('textbook_version', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('question_type', sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column('chapter', sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column('error_category', sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column('source_doc', sa.String(length=256), nullable=True))
        batch_op.create_index(batch_op.f('ix_questions_subject'), ['subject'], unique=False)

    # 旧数据回填：server_default 已覆盖绝大多数情形，这里兜底 NULL
    op.execute("UPDATE questions SET subject='math' WHERE subject IS NULL")


def downgrade() -> None:
    with op.batch_alter_table('questions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_questions_subject'))
        batch_op.drop_column('source_doc')
        batch_op.drop_column('error_category')
        batch_op.drop_column('chapter')
        batch_op.drop_column('question_type')
        batch_op.drop_column('textbook_version')
        batch_op.drop_column('region')
        batch_op.drop_column('grade')
        batch_op.drop_column('subject')
