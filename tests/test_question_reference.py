from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from backend.models.orm import (
    CanonicalQuestion,
    Job,
    Question,
    QuestionAsset,
    QuestionBankAudit,
    User,
)
from backend.repositories.question_bank import QuestionBankRepository


class RecordingVectorStore:
    def __init__(self):
        self.deleted_questions: list[list[int]] = []
        self.deleted_bank_questions: list[list[str]] = []

    def delete_questions(self, question_ids: list[int]) -> None:
        self.deleted_questions.append(list(question_ids))

    def delete_bank_questions(self, canonical_ids: list[str]) -> None:
        self.deleted_bank_questions.append(list(canonical_ids))

    def upsert_question(self, *args, **kwargs) -> bool:
        return True


def _students(db_session, count: int = 2) -> list[User]:
    suffix = uuid.uuid4().hex[:10]
    users = [
        User(
            username=f"reference-{suffix}-{index}",
            password_hash="not-used",
            role="student",
        )
        for index in range(count)
    ]
    db_session.add_all(users)
    db_session.commit()
    return users


def _canonical(db_session, stem: str, *, origin: str = "student") -> CanonicalQuestion:
    canonical = QuestionBankRepository(db_session).create(
        stem_markdown=stem,
        analysis_markdown="标准解析：移项并计算。",
        answer="标准答案",
        knowledge_points=["标准知识点"],
        tags=["标准标签"],
        difficulty="hard",
        question_type="解答题",
        chapter="方程",
        grade=9,
        origin=origin,
    )
    db_session.commit()
    return canonical


def _private_file(question_service, user_id: int, name: str) -> Path:
    path = question_service.settings.data_dir / "images" / f"u{user_id}" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"private")
    return path


def _shared_asset(db_session, canonical: CanonicalQuestion, root: Path) -> tuple[Path, Path]:
    shared = root / "question-bank" / f"{uuid.uuid4().hex}-shared.png"
    teacher_only = root / "question-bank" / f"{uuid.uuid4().hex}-teacher.png"
    shared.parent.mkdir(parents=True, exist_ok=True)
    shared.write_bytes(b"independent-ai-redraw")
    teacher_only.write_bytes(b"teacher-original")
    db_session.add_all(
        [
            QuestionAsset(
                canonical_id=canonical.id,
                kind="ai_redraw",
                visibility="shared",
                storage_path=str(shared),
            ),
            QuestionAsset(
                canonical_id=canonical.id,
                kind="teacher_original",
                visibility="teacher_only",
                storage_path=str(teacher_only),
            ),
        ]
    )
    db_session.commit()
    return shared, teacher_only


def _attach_crop(db_session, question_id: int, crop: Path) -> None:
    question = db_session.get(Question, question_id)
    question.figure_specs = [
        {
            "kind": "geometry",
            "semantic_spec": {},
            "tikz_source": "",
            "render_path": None,
            "crop_path": str(crop),
            "validation_status": "fallback",
            "validation_issues": [],
            "assumptions": [],
        }
    ]
    db_session.commit()


def test_reference_requires_a_private_original(question_service, db_session, student_user):
    canonical = _canonical(db_session, "引用必须有个人原图")

    with pytest.raises(ValueError, match="原图"):
        question_service.reference_canonical(
            student_user.id, canonical.id, private_image_path="  "
        )

    assert QuestionBankRepository(db_session).count_references(canonical.id) == 0


def test_references_keep_private_originals_review_state_and_only_shared_assets(
    question_service, db_session
):
    first_user, second_user = _students(db_session)
    canonical = _canonical(db_session, "两个学生引用同一道标准题")
    shared, teacher_only = _shared_asset(
        db_session, canonical, question_service.settings.data_dir
    )
    first_image = _private_file(question_service, first_user.id, "first.jpg")
    second_image = _private_file(question_service, second_user.id, "second.jpg")

    first = question_service.reference_canonical(
        first_user.id,
        canonical.id,
        private_image_path=str(first_image),
        user_tags=["A 的个人标签"],
        source_doc="A 的试卷.pdf#1",
    )
    second = question_service.reference_canonical(
        second_user.id,
        canonical.id,
        private_image_path=str(second_image),
        user_tags=["B 的个人标签"],
        match_method="confirmed_similar",
        source="manual",
    )

    assert first.id != second.id
    assert first.canonical_id == second.canonical_id == canonical.id
    assert first.match_method == "exact"
    assert second.match_method == "confirmed_similar"
    assert first.image_path == str(first_image)
    assert second.image_path == str(second_image)
    assert first.content_markdown == "两个学生引用同一道标准题\n\n---\n\n标准解析：移项并计算。"
    assert first.answer == second.answer == "标准答案"
    assert first.tags == ["A 的个人标签"]
    assert second.tags == ["B 的个人标签"]
    assert first.source_doc == "A 的试卷.pdf#1"
    assert second.source == "manual"
    assert str(shared) in first.figure_paths and str(shared) in second.figure_paths
    assert str(teacher_only) not in first.figure_paths + second.figure_paths
    assert canonical.tags == ["标准标签"]

    graded = question_service.grade_review(first.id, first_user.id, "good")
    assert graded is not None and graded.reps == 1
    assert question_service.get_question(second.id, second_user.id).reps == 0
    assert QuestionBankRepository(db_session).count_references(canonical.id) == 2


def test_keyword_and_metadata_filters_read_canonical_content(question_service, db_session):
    user = _students(db_session, 1)[0]
    canonical = _canonical(db_session, "合并后检索的标准题干")
    personal = question_service.reference_canonical(
        user.id,
        canonical.id,
        private_image_path=str(_private_file(question_service, user.id, "search.jpg")),
    )

    by_analysis = question_service.list_questions(
        user.id, keyword="标准解析", semantic=False
    )
    by_stem_and_metadata = question_service.list_questions(
        user.id,
        keyword="合并后检索",
        subject="math",
        grade=9,
        semantic=False,
    )

    assert [question.id for question in by_analysis] == [personal.id]
    assert [question.id for question in by_stem_and_metadata] == [personal.id]
    assert question_service.count_for_user(user.id, keyword="标准解析") == 1


def test_teacher_revision_flows_to_plain_reference_but_not_personal_overrides(
    question_service, db_session
):
    first_user, second_user = _students(db_session)
    canonical = _canonical(db_session, "教师修订前的题干")
    first = question_service.reference_canonical(
        first_user.id,
        canonical.id,
        private_image_path=str(_private_file(question_service, first_user.id, "a.jpg")),
    )
    second = question_service.reference_canonical(
        second_user.id,
        canonical.id,
        private_image_path=str(_private_file(question_service, second_user.id, "b.jpg")),
    )

    overridden = question_service.update_question(
        first.id,
        first_user.id,
        content_markdown="个人题干\n\n---\n\n个人解析",
        answer="个人答案",
        user_note="只属于自己的备注",
    )
    assert overridden is not None
    assert overridden.content_markdown == "个人题干\n\n---\n\n个人解析"
    assert overridden.answer == "个人答案"

    stored_canonical = db_session.get(CanonicalQuestion, canonical.id)
    stored_canonical.stem_markdown = "教师修订后的题干"
    stored_canonical.analysis_markdown = "教师修订后的解析"
    stored_canonical.answer = "教师修订后的答案"
    db_session.commit()

    first_after = question_service.get_question(first.id, first_user.id)
    second_after = question_service.get_question(second.id, second_user.id)
    assert first_after.content_markdown == "个人题干\n\n---\n\n个人解析"
    assert first_after.answer == "个人答案"
    assert first_after.user_note == "只属于自己的备注"
    assert second_after.content_markdown == "教师修订后的题干\n\n---\n\n教师修订后的解析"
    assert second_after.answer == "教师修订后的答案"


def test_detached_reference_uses_snapshots_below_personal_overrides(
    question_service, db_session
):
    user = _students(db_session, 1)[0]
    canonical = _canonical(db_session, "即将解除引用的标准题")
    image = _private_file(question_service, user.id, "detached.jpg")
    personal = question_service.reference_canonical(
        user.id, canonical.id, private_image_path=str(image)
    )
    question_service.update_question(
        personal.id, user.id, answer="个人保留答案"
    )

    stored = db_session.get(Question, personal.id)
    stored.canonical = None
    stored.detached_reason = "admin_delete"
    db_session.commit()

    detached = question_service.get_question(personal.id, user.id)
    assert detached.canonical_id is None
    assert detached.content_markdown == "即将解除引用的标准题\n\n---\n\n标准解析：移项并计算。"
    assert detached.answer == "个人保留答案"
    assert detached.image_path == str(image)


def test_student_delete_preserves_other_reference_then_cleans_student_orphan(
    question_service, db_session
):
    first_user, second_user = _students(db_session)
    canonical = _canonical(db_session, "A-B 生命周期题")
    shared, _ = _shared_asset(db_session, canonical, question_service.settings.data_dir)
    first_image = _private_file(question_service, first_user.id, "a-original.jpg")
    second_image = _private_file(question_service, second_user.id, "b-original.jpg")
    first_crop = _private_file(question_service, first_user.id, "a-crop.png")
    second_crop = _private_file(question_service, second_user.id, "b-crop.png")
    vectors = RecordingVectorStore()
    question_service.vector_store = vectors
    first = question_service.reference_canonical(
        first_user.id, canonical.id, private_image_path=str(first_image)
    )
    second = question_service.reference_canonical(
        second_user.id, canonical.id, private_image_path=str(second_image)
    )
    _attach_crop(db_session, first.id, first_crop)
    _attach_crop(db_session, second.id, second_crop)
    canonical_id = canonical.id

    assert question_service.delete_questions([second.id], first_user.id) == 0
    assert question_service.get_question(second.id, second_user.id) is not None
    assert second_image.exists()
    assert vectors.deleted_questions == []

    assert question_service.delete_questions([first.id], first_user.id) == 1

    assert not first_image.exists()
    assert not first_crop.exists()
    assert second_image.exists() and second_crop.exists()
    assert shared.exists()
    assert db_session.get(CanonicalQuestion, canonical_id) is not None
    remaining = question_service.get_question(second.id, second_user.id)
    assert remaining is not None and str(shared) in remaining.figure_paths
    assert vectors.deleted_questions == [[first.id]]
    assert vectors.deleted_bank_questions == []

    assert question_service.delete_questions([second.id], second_user.id) == 1
    db_session.expire_all()

    assert not second_image.exists()
    assert not second_crop.exists()
    assert not shared.exists()
    assert db_session.get(CanonicalQuestion, canonical_id) is None
    assert vectors.deleted_questions == [[first.id], [second.id]]
    assert vectors.deleted_bank_questions == [[str(canonical_id)]]
    audit = db_session.execute(
        select(QuestionBankAudit).where(
            QuestionBankAudit.canonical_id == canonical_id,
            QuestionBankAudit.action == "student_orphan_cleanup",
        )
    ).scalar_one()
    assert audit.actor == "system"
    assert audit.affected_student_count == 0


@pytest.mark.parametrize("origin", ["teacher", "seed"])
def test_last_student_delete_never_cleans_teacher_or_seed_canonical(
    question_service, db_session, origin
):
    user = _students(db_session, 1)[0]
    canonical = _canonical(db_session, f"保留 {origin} 标准题", origin=origin)
    shared, _ = _shared_asset(db_session, canonical, question_service.settings.data_dir)
    image = _private_file(question_service, user.id, f"{origin}.jpg")
    vectors = RecordingVectorStore()
    question_service.vector_store = vectors
    personal = question_service.reference_canonical(
        user.id, canonical.id, private_image_path=str(image)
    )

    assert question_service.delete_questions([personal.id], user.id) == 1

    assert db_session.get(CanonicalQuestion, canonical.id) is not None
    assert shared.exists()
    assert not image.exists()
    assert vectors.deleted_bank_questions == []


def test_delete_rejects_outside_private_path_without_touching_file(
    question_service, db_session, tmp_path
):
    user = _students(db_session, 1)[0]
    canonical = _canonical(db_session, "删除路径边界题")
    outside = tmp_path / "outside-private.jpg"
    outside.write_bytes(b"must-survive")
    personal = question_service.reference_canonical(
        user.id, canonical.id, private_image_path=str(outside)
    )

    assert question_service.delete_questions([personal.id], user.id) == 1

    assert outside.read_bytes() == b"must-survive"
    assert question_service.get_question(personal.id, user.id) is None


def test_post_commit_file_failure_records_retryable_cleanup(
    question_service, db_session, monkeypatch
):
    user = _students(db_session, 1)[0]
    canonical = _canonical(db_session, "文件失败可重试清理题")
    image = _private_file(question_service, user.id, "unlink-fails.jpg")
    personal = question_service.reference_canonical(
        user.id, canonical.id, private_image_path=str(image)
    )
    original_unlink = Path.unlink

    def fail_for_private_file(path, *args, **kwargs):
        if path == image:
            raise OSError("simulated filesystem failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_for_private_file)

    assert question_service.delete_questions([personal.id], user.id) == 1

    assert image.exists()
    assert db_session.get(Question, personal.id) is None
    cleanup = db_session.execute(
        select(Job).where(Job.type == "asset_cleanup", Job.user_id == user.id)
    ).scalar_one()
    assert cleanup.status == "pending"
    assert str(image) in cleanup.payload["paths"]
    assert "simulated filesystem failure" in cleanup.error


def _document_job(
    db_session, user_id: int, doc_path: Path, *, page_path: Path, crop_path: Path
) -> Job:
    job = Job(
        id=uuid.uuid4().hex,
        user_id=user_id,
        type="document_import",
        status="success",
        payload={"doc_path": str(doc_path), "image_paths": None},
        result={
            "page_images": [str(page_path)],
            "segments": [
                {
                    "figure_specs": [
                        {"crop_path": str(crop_path), "render_path": None}
                    ]
                }
            ],
        },
    )
    db_session.add(job)
    db_session.commit()
    return job


def test_delete_rejects_document_path_owned_by_another_user(
    question_service, db_session
):
    attacker, victim = _students(db_session)
    document_root = question_service.settings.doc_upload_path
    victim_page = document_root / "pages" / "victim-job" / "page.png"
    victim_crop = document_root / "pages" / "victim-job" / "figures" / "crop.png"
    victim_page.parent.mkdir(parents=True, exist_ok=True)
    victim_crop.parent.mkdir(parents=True, exist_ok=True)
    victim_page.write_bytes(b"victim original")
    victim_crop.write_bytes(b"victim crop")
    victim_document = document_root / "victim.pdf"
    victim_document.write_bytes(b"victim document")
    _document_job(
        db_session,
        victim.id,
        victim_document,
        page_path=victim_page,
        crop_path=victim_crop,
    )
    canonical = _canonical(db_session, "跨用户文档路径攻击")
    personal = question_service.reference_canonical(
        attacker.id, canonical.id, private_image_path=str(victim_page)
    )
    _attach_crop(db_session, personal.id, victim_crop)

    assert question_service.delete_questions([personal.id], attacker.id) == 1

    assert victim_page.read_bytes() == b"victim original"
    assert victim_crop.read_bytes() == b"victim crop"


def test_delete_cleans_document_assets_owned_by_current_user_job(
    question_service, db_session
):
    user = _students(db_session, 1)[0]
    document_root = question_service.settings.doc_upload_path
    page = document_root / "pages" / "owned-job" / "page.png"
    crop = document_root / "pages" / "owned-job" / "figures" / "crop.png"
    page.parent.mkdir(parents=True, exist_ok=True)
    crop.parent.mkdir(parents=True, exist_ok=True)
    page.write_bytes(b"owned original")
    crop.write_bytes(b"owned crop")
    document = document_root / "owned.pdf"
    document.write_bytes(b"owned document")
    _document_job(db_session, user.id, document, page_path=page, crop_path=crop)
    canonical = _canonical(db_session, "本人文档资源正常清理")
    personal = question_service.reference_canonical(
        user.id, canonical.id, private_image_path=str(page)
    )
    _attach_crop(db_session, personal.id, crop)

    assert question_service.delete_questions([personal.id], user.id) == 1

    assert not page.exists()
    assert not crop.exists()


def test_keyword_filters_do_not_match_content_hidden_by_personal_override(
    question_service, db_session
):
    user = _students(db_session, 1)[0]
    canonical = _canonical(db_session, "被覆盖的标准题干")
    personal = question_service.reference_canonical(
        user.id,
        canonical.id,
        private_image_path=str(_private_file(question_service, user.id, "override-search.jpg")),
    )
    question_service.update_question(
        personal.id,
        user.id,
        content_markdown="个人可见题干\n\n---\n\n个人可见解析",
        answer="个人可见答案",
    )

    for hidden in ("被覆盖的标准题干", "标准解析", "标准答案"):
        assert question_service.list_questions(
            user.id, keyword=hidden, semantic=False
        ) == []
    assert [q.id for q in question_service.list_questions(
        user.id, keyword="个人可见解析", semantic=False
    )] == [personal.id]


def test_keyword_filters_ignore_stale_snapshot_while_canonical_exists(
    question_service, db_session
):
    user = _students(db_session, 1)[0]
    canonical = _canonical(db_session, "快照里的旧题干")
    personal = question_service.reference_canonical(
        user.id,
        canonical.id,
        private_image_path=str(_private_file(question_service, user.id, "snapshot-search.jpg")),
    )
    stored_canonical = db_session.get(CanonicalQuestion, canonical.id)
    stored_canonical.stem_markdown = "当前标准题干"
    stored_canonical.analysis_markdown = "当前标准解析"
    stored_canonical.answer = "现行答案"
    db_session.commit()

    for stale in ("快照里的旧题干", "标准解析：移项并计算。", "标准答案"):
        assert question_service.list_questions(
            user.id, keyword=stale, semantic=False
        ) == []
    assert [q.id for q in question_service.list_questions(
        user.id, keyword="当前标准题干", semantic=False
    )] == [personal.id]


def test_detached_record_filters_use_snapshot_metadata_but_not_hidden_keywords(
    question_service, db_session
):
    user = _students(db_session, 1)[0]
    canonical = _canonical(db_session, "解除后应隐藏的快照题干")
    stored_canonical = db_session.get(CanonicalQuestion, canonical.id)
    stored_canonical.subject = "physics"
    stored_canonical.grade = 11
    stored_canonical.region = "上海"
    stored_canonical.textbook_version = "沪教版"
    db_session.commit()
    personal = question_service.reference_canonical(
        user.id,
        canonical.id,
        private_image_path=str(_private_file(question_service, user.id, "detached-filter.jpg")),
    )
    stored = db_session.get(Question, personal.id)
    stored.canonical = None
    stored.detached_reason = "admin_delete"
    db_session.commit()
    question_service.update_question(
        personal.id, user.id, content_markdown="解除后个人可见题干"
    )

    matches = question_service.list_questions(
        user.id,
        subject="physics",
        grade=11,
        region="上海",
        textbook_version="沪教版",
        semantic=False,
    )
    assert [question.id for question in matches] == [personal.id]
    assert question_service.count_for_user(
        user.id, subject="physics", grade=11, region="上海", textbook_version="沪教版"
    ) == 1
    assert question_service.list_questions(
        user.id, keyword="解除后应隐藏的快照题干", semantic=False
    ) == []
    assert [q.id for q in question_service.list_questions(
        user.id, keyword="解除后个人可见题干", semantic=False
    )] == [personal.id]
