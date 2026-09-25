"""add detection_logs table and questions.demotion_state

P3.2 同类题检测与降级联动：
- 新建 detection_logs 表（原错题 → 同类题标识/来源/快照 → 作答结果 + 降级标记）
- questions 新增 demotion_state 列（normal/demoted，server_default='normal'）

Revision ID: e7f2c91a3b55
Revises: c3f1a2b48d01
Create Date: 2026-09-26 14:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = 'e7f2c91a3b55'
down_revision = 'c3f1a2b48d01'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'detection_logs',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('question_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('tested_ref', sa.String(length=128), nullable=False),
        sa.Column('tested_source', sa.String(length=16), nullable=False),
        sa.Column('tested_content', sa.Text(), nullable=True),
        sa.Column('tested_answer', sa.Text(), nullable=True),
        sa.Column('result', sa.String(length=8), nullable=False, server_default='pending'),
        sa.Column('student_answer', sa.Text(), nullable=True),
        sa.Column('demoted', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['question_id'], ['questions.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('detection_logs', schema=None) as batch_op:
        batch_op.create_index('ix_detection_logs_question_id', ['question_id'], unique=False)
        batch_op.create_index('ix_detection_logs_user_id', ['user_id'], unique=False)
        batch_op.create_index(
            'ix_detection_logs_user_created', ['user_id', 'created_at'], unique=False
        )

    with op.batch_alter_table('questions', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'demotion_state', sa.String(length=16),
                server_default='normal', nullable=False,
            )
        )
    op.execute("UPDATE questions SET demotion_state='normal' WHERE demotion_state IS NULL")


def downgrade() -> None:
    with op.batch_alter_table('questions', schema=None) as batch_op:
        batch_op.drop_column('demotion_state')
    with op.batch_alter_table('detection_logs', schema=None) as batch_op:
        batch_op.drop_index('ix_detection_logs_user_created')
        batch_op.drop_index('ix_detection_logs_user_id')
        batch_op.drop_index('ix_detection_logs_question_id')
    op.drop_table('detection_logs')
