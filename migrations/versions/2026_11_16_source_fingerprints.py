"""merge format-only canonical duplicates and add source fingerprints

Revision ID: 2026_11_16
Revises: 2026_11_13
Create Date: 2026-11-16 00:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from backend.services.question_identity import normalize_stem, stem_hash

revision = "2026_11_16"
down_revision = "2026_11_13"
branch_labels = None
depends_on = None


def _merge_format_only_duplicates() -> None:
    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            """
            SELECT id, stem_markdown, origin FROM canonical_questions
            ORDER BY CASE WHEN origin = 'teacher' THEN 0 ELSE 1 END, id
            """
        )
    ).mappings().all()
    connection.execute(
        sa.text(
            "UPDATE canonical_questions SET normalized_hash = '__pending__' || id"
        )
    )
    groups: dict[str, list[dict]] = {}
    for row in rows:
        normalized = normalize_stem(row["stem_markdown"])
        groups.setdefault(stem_hash(row["stem_markdown"]), []).append(
            {**row, "normalized_stem": normalized}
        )

    for normalized_hash, group in groups.items():
        survivor = group[0]
        survivor_id = survivor["id"]
        connection.execute(
            sa.text(
                "UPDATE canonical_questions SET normalized_stem = :normalized, "
                "normalized_hash = :hash WHERE id = :id"
            ),
            {
                "normalized": survivor["normalized_stem"],
                "hash": normalized_hash,
                "id": survivor_id,
            },
        )
        for duplicate in group[1:]:
            duplicate_id = duplicate["id"]
            connection.execute(
                sa.text(
                    "DELETE FROM question_source_fingerprints WHERE canonical_id = :duplicate "
                    "AND content_hash IN (SELECT content_hash FROM question_source_fingerprints "
                    "WHERE canonical_id = :survivor)"
                ),
                {"survivor": survivor_id, "duplicate": duplicate_id},
            )
            connection.execute(
                sa.text(
                    "UPDATE question_source_fingerprints SET canonical_id = :survivor "
                    "WHERE canonical_id = :duplicate"
                ),
                {"survivor": survivor_id, "duplicate": duplicate_id},
            )
            connection.execute(
                sa.text("UPDATE questions SET canonical_id = :survivor WHERE canonical_id = :duplicate"),
                {"survivor": survivor_id, "duplicate": duplicate_id},
            )
            connection.execute(
                sa.text("UPDATE question_assets SET canonical_id = :survivor WHERE canonical_id = :duplicate"),
                {"survivor": survivor_id, "duplicate": duplicate_id},
            )
            connection.execute(
                sa.text("DELETE FROM canonical_questions WHERE id = :id"),
                {"id": duplicate_id},
            )


def _backfill_source_fingerprints() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text(
            """
            INSERT INTO question_source_fingerprints (canonical_id, content_hash)
            SELECT DISTINCT canonical_id, image_hash FROM questions
            WHERE canonical_id IS NOT NULL AND image_hash IS NOT NULL AND image_hash <> ''
            """
        )
    )


def upgrade() -> None:
    op.create_table(
        "question_source_fingerprints",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("canonical_id", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["canonical_id"], ["canonical_questions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("canonical_id", "content_hash", name="uq_source_fingerprint_canonical_hash"),
    )
    op.create_index(
        "ix_question_source_fingerprints_canonical_id",
        "question_source_fingerprints",
        ["canonical_id"],
    )
    op.create_index(
        "ix_question_source_fingerprints_content_hash",
        "question_source_fingerprints",
        ["content_hash"],
    )
    _merge_format_only_duplicates()
    _backfill_source_fingerprints()


def downgrade() -> None:
    op.drop_index("ix_question_source_fingerprints_content_hash", table_name="question_source_fingerprints")
    op.drop_index("ix_question_source_fingerprints_canonical_id", table_name="question_source_fingerprints")
    op.drop_table("question_source_fingerprints")
