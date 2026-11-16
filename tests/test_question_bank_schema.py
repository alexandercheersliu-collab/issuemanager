from __future__ import annotations

import configparser
import hashlib
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

from backend.models.orm import (
    CanonicalQuestion,
    Question,
    QuestionAsset,
    QuestionBankAudit,
    QuestionTypeDefinition,
)
from backend.models.schemas import (
    CanonicalQuestionOut,
    CanonicalQuestionUpdate,
    DeleteImpactOut,
    QuestionBankCandidate,
)
from backend.repositories.question_bank import QuestionBankRepository

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"


def _canonical(**overrides) -> CanonicalQuestion:
    values = {
        "stem_markdown": "计算 $x+1$",
        "normalized_stem": "计算 $x+1$",
        "normalized_hash": "a" * 64,
        "analysis_markdown": "合并同类项。",
        "answer": "x+1",
        "origin": "student",
        "validation_state": "machine_validated",
    }
    values.update(overrides)
    return CanonicalQuestion(**values)


def test_canonical_question_normalized_hash_is_unique(db_session):
    db_session.add(_canonical())
    db_session.flush()
    db_session.add(_canonical(stem_markdown="另一道题", normalized_stem="另一道题"))

    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_question_type_is_unique_within_subject(db_session, student_user):
    db_session.add(
        QuestionTypeDefinition(
            subject="math", name="解答题", created_by=student_user.id
        )
    )
    db_session.flush()
    db_session.add(
        QuestionTypeDefinition(
            subject="math", name="解答题", created_by=student_user.id
        )
    )

    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_personal_question_roundtrips_reference_overrides_and_snapshots(
    db_session, student_user
):
    canonical = _canonical()
    personal = Question(
        user_id=student_user.id,
        canonical=canonical,
        private_image_path="/data/images/u1/private.jpg",
        content_markdown="个人旧内容",
        stem_override="个人题干",
        analysis_override="个人解析",
        answer_override="个人答案",
        stem_snapshot="标准题干快照",
        analysis_snapshot="标准解析快照",
        answer_snapshot="标准答案快照",
        metadata_snapshot={"question_type": "解答题", "grade": 9},
        detached_reason="admin_delete",
        match_method="confirmed_similar",
    )
    db_session.add(personal)
    db_session.flush()
    db_session.expire_all()

    stored = db_session.get(Question, personal.id)
    assert stored is not None
    assert stored.canonical_id == canonical.id
    assert stored.private_image_path == "/data/images/u1/private.jpg"
    assert stored.stem_override == "个人题干"
    assert stored.analysis_override == "个人解析"
    assert stored.answer_override == "个人答案"
    assert stored.stem_snapshot == "标准题干快照"
    assert stored.analysis_snapshot == "标准解析快照"
    assert stored.answer_snapshot == "标准答案快照"
    assert stored.metadata_snapshot == {"question_type": "解答题", "grade": 9}
    assert stored.detached_reason == "admin_delete"
    assert stored.match_method == "confirmed_similar"
    assert stored.canonical.id == canonical.id


def test_question_assets_distinguish_shared_and_teacher_only(db_session):
    canonical = _canonical()
    canonical.assets = [
        QuestionAsset(
            kind="teacher_original",
            visibility="teacher_only",
            storage_path="/data/question-bank/original.png",
            mime_type="image/png",
        ),
        QuestionAsset(
            kind="ai_render",
            visibility="shared",
            storage_path="/data/question-bank/render.png",
            mime_type="image/png",
            is_preferred=True,
        ),
    ]
    db_session.add(canonical)
    db_session.flush()
    db_session.expire_all()

    stored = db_session.get(CanonicalQuestion, canonical.id)
    assert stored is not None
    assert {(asset.kind, asset.visibility) for asset in stored.assets} == {
        ("teacher_original", "teacher_only"),
        ("ai_render", "shared"),
    }


def test_question_asset_rejects_unknown_visibility(db_session):
    db_session.add(
        QuestionAsset(
            canonical=_canonical(),
            kind="ai_render",
            visibility="public",
            storage_path="/data/question-bank/render.png",
        )
    )

    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_question_bank_audit_survives_canonical_delete(db_session, student_user):
    canonical = _canonical()
    db_session.add(canonical)
    db_session.flush()
    audit = QuestionBankAudit(
        actor="system",
        actor_id=student_user.id,
        canonical_id=canonical.id,
        action="admin_delete",
        change_summary={"reason": "test"},
        affected_student_count=1,
        affected_question_ids=[11, 12],
    )
    db_session.add(audit)
    db_session.flush()
    canonical_id = canonical.id
    db_session.delete(canonical)
    db_session.flush()
    db_session.expire_all()

    stored = db_session.get(QuestionBankAudit, audit.id)
    assert stored is not None
    assert stored.canonical_id == canonical_id
    assert stored.affected_question_ids == [11, 12]


def test_repository_uses_stable_stem_identity(db_session):
    repository = QuestionBankRepository(db_session)
    created = repository.create(
        stem_markdown="1.  x+1",
        analysis_markdown="直接计算。",
        answer="2",
    )

    assert created.normalized_stem == "x+1"
    assert created.normalized_hash == hashlib.sha256(b"x+1").hexdigest()
    assert repository.find_by_stem("  x+1\n") is created


def test_question_bank_pydantic_contracts(db_session):
    canonical = QuestionBankRepository(db_session).create(
        stem_markdown="x+1=2", analysis_markdown="移项。", answer="x=1"
    )
    output = CanonicalQuestionOut.from_orm_model(canonical)
    candidate = QuestionBankCandidate(score=0.875, question=output)
    update = CanonicalQuestionUpdate(stem_markdown="x+2=3", answer="x=1")
    impact = DeleteImpactOut(
        canonical_id=canonical.id,
        reference_count=2,
        affected_students=[{"username": "demo", "question_ids": [3, 4]}],
        affected_question_ids=[3, 4],
    )

    assert candidate.score == 0.875
    assert candidate.question.normalized_hash == canonical.normalized_hash
    assert update.model_dump(exclude_unset=True) == {
        "stem_markdown": "x+2=3",
        "answer": "x=1",
    }
    assert impact.affected_students[0]["question_ids"] == [3, 4]


def _alembic_config(db: Path, monkeypatch):  # noqa: ANN001,ANN202
    from alembic.config import Config

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db.as_posix()}")
    parser = configparser.ConfigParser(interpolation=None)
    with open(ALEMBIC_INI, encoding="utf-8") as handle:
        parser.read_file(handle)
    config = Config()
    config.config_file_name = "alembic.ini"
    config.__dict__["file_config"] = parser
    for section in parser.sections():
        for key, value in parser.items(section):
            config.set_section_option(section, key, value)
    config.set_main_option("script_location", "migrations")
    return config


def test_migration_merges_by_stem_hash_with_teacher_priority(tmp_path, monkeypatch):
    from alembic import command

    database = tmp_path / "shared_question_bank.db"
    config = _alembic_config(database, monkeypatch)
    command.upgrade(config, "b52e0d8a4c21")

    connection = sqlite3.connect(database)
    connection.executemany(
        "INSERT INTO users (username, password_hash, role) VALUES (?, 'x', ?)",
        [("student", "student"), ("teacher", "teacher")],
    )
    insert_question = """
        INSERT INTO questions (
            user_id, image_path, content_markdown, answer, knowledge_points, tags,
            difficulty, source, reps, ease, interval_days, subject, grade
        ) VALUES (?, ?, ?, ?, '[]', '[]', 'medium', 'ai', 0, 2.5, 0, 'math', 9)
    """
    connection.execute(
        insert_question,
        (1, "/private/student.jpg", "$x+1$\n\n---\n\n学生解析", "学生答案"),
    )
    connection.execute(
        insert_question,
        (2, "/private/teacher.jpg", r"1. \(x+1\)" + "\n\n---\n\n教师解析", "教师答案"),
    )
    connection.commit()
    connection.close()

    command.upgrade(config, "head")

    connection = sqlite3.connect(database)
    canonical_rows = connection.execute(
        "SELECT id, normalized_stem, normalized_hash, analysis_markdown, answer, "
        "origin, validation_state, published_by FROM canonical_questions"
    ).fetchall()
    assert len(canonical_rows) == 1
    canonical_id = canonical_rows[0][0]
    assert canonical_rows[0][1:] == (
        "$x+1$",
        hashlib.sha256(b"$x+1$").hexdigest(),
        "教师解析",
        "教师答案",
        "teacher",
        "teacher_published",
        2,
    )
    personal_rows = connection.execute(
        "SELECT image_path, private_image_path, canonical_id, stem_snapshot, "
        "analysis_snapshot, answer_snapshot FROM questions ORDER BY id"
    ).fetchall()
    assert personal_rows == [
        ("/private/student.jpg", "/private/student.jpg", canonical_id, r"1. \(x+1\)", "教师解析", "教师答案"),
        ("/private/teacher.jpg", "/private/teacher.jpg", canonical_id, r"1. \(x+1\)", "教师解析", "教师答案"),
    ]
    assert connection.execute("SELECT COUNT(*) FROM question_assets").fetchone()[0] == 0
    columns = {row[1] for row in connection.execute("PRAGMA table_info(questions)")}
    assert {
        "stem_override",
        "analysis_override",
        "answer_override",
        "metadata_snapshot",
        "detached_reason",
        "match_method",
    } <= columns
    connection.close()
