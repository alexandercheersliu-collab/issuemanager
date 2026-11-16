"""add shared canonical questions and personal references

Revision ID: 2026_11_13
Revises: b52e0d8a4c21
Create Date: 2026-11-13 00:00:00.000000
"""
from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

from backend.services.question_content import split_stem_and_analysis
from backend.services.question_identity import normalize_stem, stem_hash

revision = "2026_11_13"
down_revision = "b52e0d8a4c21"
branch_labels = None
depends_on = None


def _json_value(value):
    if value is None or isinstance(value, (list, dict)):
        return value
    return json.loads(value)


def _merge_existing_questions() -> None:
    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            """
            SELECT q.*, u.role AS user_role
            FROM questions AS q
            JOIN users AS u ON u.id = q.user_id
            ORDER BY CASE WHEN u.role IN ('teacher', 'admin') THEN 0 ELSE 1 END, q.id
            """
        )
    ).mappings().all()
    canonical_ids: dict[str, int] = {}

    for row in rows:
        private_image_path = row["image_path"]
        try:
            normalized = normalize_stem(split_stem_and_analysis(row["content_markdown"])[0])
            normalized_hash = stem_hash(split_stem_and_analysis(row["content_markdown"])[0])
        except ValueError:
            connection.execute(
                sa.text("UPDATE questions SET private_image_path = :path WHERE id = :id"),
                {"path": private_image_path, "id": row["id"]},
            )
            continue

        if normalized_hash not in canonical_ids:
            stem, analysis = split_stem_and_analysis(row["content_markdown"])
            is_teacher = row["user_role"] in ("teacher", "admin")
            connection.execute(
                sa.text(
                    """
                    INSERT INTO canonical_questions (
                        stem_markdown, normalized_stem, normalized_hash,
                        analysis_markdown, answer, knowledge_points, tags, difficulty,
                        question_type, chapter, subject, grade, region, textbook_version,
                        origin, validation_state, published_by
                    ) VALUES (
                        :stem_markdown, :normalized_stem, :normalized_hash,
                        :analysis_markdown, :answer, :knowledge_points, :tags, :difficulty,
                        :question_type, :chapter, :subject, :grade, :region, :textbook_version,
                        :origin, :validation_state, :published_by
                    )
                    """
                ),
                {
                    "stem_markdown": stem,
                    "normalized_stem": normalized,
                    "normalized_hash": normalized_hash,
                    "analysis_markdown": analysis or "",
                    "answer": row["answer"] or "",
                    "knowledge_points": json.dumps(
                        _json_value(row["knowledge_points"]) or [], ensure_ascii=False
                    ),
                    "tags": json.dumps(_json_value(row["tags"]) or [], ensure_ascii=False),
                    "difficulty": row["difficulty"] or "medium",
                    "question_type": row["question_type"],
                    "chapter": row["chapter"],
                    "subject": row["subject"] or "math",
                    "grade": row["grade"],
                    "region": row["region"],
                    "textbook_version": row["textbook_version"],
                    "origin": "teacher" if is_teacher else "student",
                    "validation_state": (
                        "teacher_published" if is_teacher else "machine_validated"
                    ),
                    "published_by": row["user_id"] if is_teacher else None,
                },
            )
            canonical_ids[normalized_hash] = connection.execute(
                sa.text(
                    "SELECT id FROM canonical_questions WHERE normalized_hash = :hash"
                ),
                {"hash": normalized_hash},
            ).scalar_one()

        canonical = connection.execute(
            sa.text(
                """
                SELECT stem_markdown, analysis_markdown, answer, knowledge_points, tags,
                       difficulty, question_type, chapter, subject, grade, region,
                       textbook_version
                FROM canonical_questions WHERE id = :id
                """
            ),
            {"id": canonical_ids[normalized_hash]},
        ).mappings().one()
        metadata_snapshot = {
            key: (
                _json_value(canonical[key])
                if key in ("knowledge_points", "tags")
                else canonical[key]
            )
            for key in (
                "knowledge_points", "tags", "difficulty", "question_type", "chapter",
                "subject", "grade", "region", "textbook_version",
            )
        }
        connection.execute(
            sa.text(
                """
                UPDATE questions
                SET canonical_id = :canonical_id,
                    private_image_path = :private_image_path,
                    stem_snapshot = :stem_snapshot,
                    analysis_snapshot = :analysis_snapshot,
                    answer_snapshot = :answer_snapshot,
                    metadata_snapshot = :metadata_snapshot
                WHERE id = :id
                """
            ),
            {
                "canonical_id": canonical_ids[normalized_hash],
                "private_image_path": private_image_path,
                "stem_snapshot": canonical["stem_markdown"],
                "analysis_snapshot": canonical["analysis_markdown"],
                "answer_snapshot": canonical["answer"],
                "metadata_snapshot": json.dumps(metadata_snapshot, ensure_ascii=False),
                "id": row["id"],
            },
        )


def upgrade() -> None:
    op.create_table(
        "canonical_questions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("stem_markdown", sa.Text(), nullable=False),
        sa.Column("normalized_stem", sa.Text(), nullable=False),
        sa.Column("normalized_hash", sa.String(length=64), nullable=False),
        sa.Column("analysis_markdown", sa.Text(), server_default="", nullable=False),
        sa.Column("answer", sa.Text(), server_default="", nullable=False),
        sa.Column("knowledge_points", sa.JSON(), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("difficulty", sa.String(length=16), server_default="medium", nullable=False),
        sa.Column("question_type", sa.String(length=32), nullable=True),
        sa.Column("chapter", sa.String(length=128), nullable=True),
        sa.Column("subject", sa.String(length=32), server_default="math", nullable=False),
        sa.Column("grade", sa.Integer(), nullable=True),
        sa.Column("region", sa.String(length=64), nullable=True),
        sa.Column("textbook_version", sa.String(length=64), nullable=True),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column(
            "validation_state",
            sa.String(length=32),
            server_default="machine_validated",
            nullable=False,
        ),
        sa.Column("published_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "origin IN ('teacher', 'student', 'seed')",
            name="ck_canonical_questions_origin",
        ),
        sa.ForeignKeyConstraint(["published_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("canonical_questions", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_canonical_questions_normalized_hash"),
            ["normalized_hash"],
            unique=True,
        )
        batch_op.create_index(batch_op.f("ix_canonical_questions_subject"), ["subject"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_canonical_questions_published_by"), ["published_by"], unique=False
        )

    op.create_table(
        "question_type_definitions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("subject", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("subject", "name", name="uq_question_type_definitions_subject_name"),
    )
    with op.batch_alter_table("question_type_definitions", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_question_type_definitions_subject"), ["subject"], unique=False
        )

    op.create_table(
        "question_assets",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("canonical_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("visibility", sa.String(length=16), nullable=False),
        sa.Column("storage_path", sa.String(length=512), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("mime_type", sa.String(length=128), nullable=True),
        sa.Column("is_preferred", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "visibility IN ('teacher_only', 'shared')",
            name="ck_question_assets_visibility",
        ),
        sa.ForeignKeyConstraint(["canonical_id"], ["canonical_questions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("question_assets", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_question_assets_canonical_id"), ["canonical_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_question_assets_content_hash"), ["content_hash"], unique=False)

    op.create_table(
        "question_bank_audit",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("actor", sa.String(length=64), server_default="system", nullable=False),
        sa.Column("actor_id", sa.Integer(), nullable=True),
        sa.Column("canonical_id", sa.Integer(), nullable=True),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("change_summary", sa.JSON(), nullable=False),
        sa.Column("affected_student_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("affected_question_ids", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("question_bank_audit", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_question_bank_audit_canonical_id"), ["canonical_id"], unique=False
        )

    with op.batch_alter_table("questions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("canonical_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("private_image_path", sa.String(length=512), nullable=True))
        batch_op.add_column(sa.Column("stem_override", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("analysis_override", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("answer_override", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("stem_snapshot", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("analysis_snapshot", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("answer_snapshot", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("metadata_snapshot", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("detached_reason", sa.String(length=64), nullable=True))
        batch_op.add_column(
            sa.Column("match_method", sa.String(length=32), server_default="new", nullable=False)
        )
        batch_op.create_foreign_key(
            "fk_questions_canonical_id_canonical_questions",
            "canonical_questions",
            ["canonical_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index(batch_op.f("ix_questions_canonical_id"), ["canonical_id"], unique=False)

    _merge_existing_questions()


def downgrade() -> None:
    with op.batch_alter_table("questions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_questions_canonical_id"))
        batch_op.drop_constraint(
            "fk_questions_canonical_id_canonical_questions", type_="foreignkey"
        )
        batch_op.drop_column("match_method")
        batch_op.drop_column("detached_reason")
        batch_op.drop_column("metadata_snapshot")
        batch_op.drop_column("answer_snapshot")
        batch_op.drop_column("analysis_snapshot")
        batch_op.drop_column("stem_snapshot")
        batch_op.drop_column("answer_override")
        batch_op.drop_column("analysis_override")
        batch_op.drop_column("stem_override")
        batch_op.drop_column("private_image_path")
        batch_op.drop_column("canonical_id")

    op.drop_table("question_bank_audit")
    op.drop_table("question_assets")
    op.drop_table("question_type_definitions")
    op.drop_table("canonical_questions")
