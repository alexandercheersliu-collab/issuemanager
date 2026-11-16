from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from backend.models.orm import Base, CanonicalQuestion, Question, QuestionAsset, User
from backend.models.schemas import QuestionAnalysis, QuestionBankCandidate
from backend.repositories.question_bank import QuestionBankRepository
from backend.services.ai.base import AnalysisContext
from backend.services.question_bank import QuestionBankService
from backend.services.question_identity import stem_hash
from backend.services.rag import (
    QuestionVectorStore,
    RagHit,
    SimilarConstraints,
    SimilarResult,
)


class FakeVectorStore:
    def __init__(self, hits: list[RagHit] | None = None):
        self.hits = hits or []

    def similar_canonical_questions(
        self, query_text: str, *, constraints, top_k: int
    ) -> SimilarResult:
        return SimilarResult(hits=self.hits[:top_k])

    def upsert_bank_question(self, bank_id: str, text: str, **metadata) -> bool:
        return True


def _analysis(**overrides) -> QuestionAnalysis:
    analysis = QuestionAnalysis(
        knowledge_points=["一元一次方程"],
        analysis="移项后计算未知数的值，得到正确答案。",
        answer="x=1",
        difficulty="medium",
        tags=["方程"],
        question_type="解答题",
        chapter="一元一次方程",
        is_complete=True,
    )
    return analysis.model_copy(update=overrides)


def _service(db_session, vector_store: FakeVectorStore | None = None) -> QuestionBankService:
    return QuestionBankService(
        db_session, vector_store=vector_store or FakeVectorStore()
    )


def _teacher(db_session) -> User:
    teacher = db_session.query(User).filter_by(username="task3-teacher").one_or_none()
    if teacher is None:
        teacher = User(
            username="task3-teacher", password_hash="not-used", role="teacher"
        )
        db_session.add(teacher)
        db_session.flush()
    return teacher


def test_match_exact_uses_normalized_stem_hash(db_session, student_user):
    created = QuestionBankRepository(db_session).create(
        stem_markdown="已知 $x+1=2$，求 $x$。",
        analysis_markdown="移项即可。",
        answer="x=1",
    )

    matched = _service(db_session).match_exact("1.  已知 $x+1=2$，求 $x$。\n")

    assert matched is not None
    assert matched.id == created.id
    assert _service(db_session).match_exact("已知 $x+1=3$，求 $x$。") is None


def test_find_similar_returns_teacher_candidates_before_students(db_session):
    repository = QuestionBankRepository(db_session)
    student = repository.create(
        stem_markdown="学生来源的相似候选题",
        analysis_markdown="学生解析",
        answer="学生答案",
        origin="student",
    )
    teacher = repository.create(
        stem_markdown="教师来源的相似候选题",
        analysis_markdown="教师解析",
        answer="教师答案",
        origin="teacher",
    )
    vector_store = FakeVectorStore(
        [
            RagHit(question_id=-1, distance=0.05, source="bank", bank_id=str(student.id)),
            RagHit(question_id=-1, distance=0.40, source="bank", bank_id=str(teacher.id)),
            RagHit(question_id=-1, distance=0.01, source="bank", bank_id="99999999"),
        ]
    )

    service = _service(db_session, vector_store)
    service.settings = SimpleNamespace(bank_similarity_threshold=0.5)
    candidates = service.find_similar(
        "查询相似题", context=AnalysisContext(subject="math", grade=9)
    )

    assert [candidate.question.id for candidate in candidates] == [teacher.id, student.id]
    assert [candidate.score for candidate in candidates] == [0.60, 0.95]
    assert all(isinstance(candidate, QuestionBankCandidate) for candidate in candidates)


@pytest.mark.parametrize(
    ("stem", "overrides"),
    [
        ("不完整的题目", {"is_complete": False}),
        ("  \n", {}),
        ("答案为空的题目", {"answer": " "}),
        ("解析为空的题目", {"analysis": " "}),
    ],
)
def test_student_creation_requires_structural_completeness(
    db_session, student_user, stem, overrides
):
    before = db_session.query(func.count(CanonicalQuestion.id)).scalar()

    with pytest.raises(ValueError):
        _service(db_session).create_or_upgrade(
            stem,
            _analysis(**overrides),
            actor=student_user,
            context=AnalysisContext(),
        )

    assert db_session.query(func.count(CanonicalQuestion.id)).scalar() == before


def test_student_creation_stores_only_standard_content(db_session, student_user):
    analysis = _analysis(
        private_image_path="/private/student-original.jpg",
        user_note="个人备注",
        reps=8,
    )

    created = _service(db_session).create_or_upgrade(
        "只保存标准内容的题目",
        analysis,
        actor=student_user,
        context=AnalysisContext(subject="math", grade=7, region="北京", textbook_version="人教版"),
    )

    assert created.origin == "student"
    assert created.validation_state == "machine_validated"
    assert created.published_by is None
    assert created.answer == "x=1"
    assert created.analysis_markdown == "移项后计算未知数的值，得到正确答案。"
    assert created.grade == 7
    assert created.region == "北京"
    assert created.textbook_version == "人教版"
    assert db_session.query(QuestionAsset).filter_by(canonical_id=created.id).count() == 0
    assert db_session.query(Question).filter_by(canonical_id=created.id).count() == 0


def test_teacher_same_hash_upgrades_content_and_type_without_duplicate(
    db_session, student_user
):
    original = _service(db_session).create_or_upgrade(
        "已知 $2x=4$，求 $x$。",
        _analysis(answer="学生答案", analysis="学生原始解析内容。", question_type="学生题型"),
        actor=student_user,
        context=AnalysisContext(grade=7),
    )

    upgraded = _service(db_session).create_or_upgrade(
        "1. 已知 $2x=4$，求 $x$。",
        _analysis(
            answer="教师答案",
            analysis="教师修订后的完整解析内容。",
            knowledge_points=["教师知识点"],
            tags=["教师标签"],
            difficulty="hard",
            question_type="AI题型",
            chapter="教师章节",
        ),
        actor=_teacher(db_session),
        context=AnalysisContext(subject="math", grade=9, region="上海"),
        question_type="教师指定题型",
    )

    assert upgraded.id == original.id
    assert db_session.query(func.count(CanonicalQuestion.id)).filter_by(
        normalized_hash=stem_hash("已知 $2x=4$，求 $x$。")
    ).scalar() == 1
    assert upgraded.stem_markdown == "1. 已知 $2x=4$，求 $x$。"
    assert upgraded.answer == "教师答案"
    assert upgraded.analysis_markdown == "教师修订后的完整解析内容。"
    assert upgraded.knowledge_points == ["教师知识点"]
    assert upgraded.tags == ["教师标签"]
    assert upgraded.difficulty == "hard"
    assert upgraded.question_type == "教师指定题型"
    assert upgraded.chapter == "教师章节"
    assert upgraded.grade == 9
    assert upgraded.origin == "teacher"
    assert upgraded.validation_state == "teacher_published"
    assert upgraded.published_by is not None


def test_student_exact_match_does_not_overwrite_teacher_content(db_session, student_user):
    teacher_service = _service(db_session)
    published = teacher_service.create_or_upgrade(
        "教师内容不可被学生覆盖",
        _analysis(answer="教师答案", analysis="教师发布的解析内容。", question_type="教师题型"),
        actor=_teacher(db_session),
        context=AnalysisContext(),
    )

    matched = _service(db_session).create_or_upgrade(
        "教师内容不可被学生覆盖",
        _analysis(answer="学生答案", analysis="学生重新生成的解析。", question_type="学生题型"),
        actor=student_user,
        context=AnalysisContext(),
    )

    assert matched.id == published.id
    assert matched.origin == "teacher"
    assert matched.answer == "教师答案"
    assert matched.analysis_markdown == "教师发布的解析内容。"
    assert matched.question_type == "教师题型"


def test_incomplete_student_analysis_still_reuses_complete_existing_canonical(
    db_session, student_user
):
    published = _service(db_session).create_or_upgrade(
        "完整已有题允许不完整输入复用",
        _analysis(answer="完整答案", analysis="完整已有的解析内容。"),
        actor=_teacher(db_session),
        context=AnalysisContext(),
    )

    matched = _service(db_session).create_or_upgrade(
        "完整已有题允许不完整输入复用",
        _analysis(is_complete=False, answer="", analysis=""),
        actor=student_user,
        context=AnalysisContext(),
    )

    assert matched.id == published.id
    assert matched.answer == "完整答案"
    assert matched.analysis_markdown == "完整已有的解析内容。"


def test_concurrent_creation_keeps_one_canonical_question(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'question-bank-race.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory() as session:
        session.add(
            User(id=1, username="race-student", password_hash="not-used", role="student")
        )
        session.commit()

    gate = threading.Barrier(2)
    original_create = QuestionBankRepository.create

    def concurrent_create(self, **kwargs):
        gate.wait(timeout=5)
        return original_create(self, **kwargs)

    monkeypatch.setattr(QuestionBankRepository, "create", concurrent_create)
    vector_store = FakeVectorStore()
    service = QuestionBankService(
        session_factory=session_factory, vector_store=vector_store
    )
    actor = User(id=1, username="race-student", password_hash="not-used", role="student")
    outcomes: list[int] = []
    errors: list[BaseException] = []
    start = threading.Barrier(3)

    def create() -> None:
        start.wait(timeout=5)
        try:
            question = service.create_or_upgrade(
                "并发创建只能保留一道标准题",
                _analysis(),
                actor=actor,
                context=AnalysisContext(),
            )
            outcomes.append(question.id)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=create) for _ in range(2)]
    for thread in threads:
        thread.start()
    start.wait(timeout=5)
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()

    assert errors == []
    assert len(outcomes) == 2
    assert outcomes[0] == outcomes[1]
    with session_factory() as session:
        rows = session.execute(
            select(CanonicalQuestion).where(
                CanonicalQuestion.normalized_hash == stem_hash("并发创建只能保留一道标准题")
            )
        ).scalars().all()
        assert len(rows) == 1


def test_incomplete_student_submission_reuses_exact_match_without_modification(
    db_session, student_user
):
    service = _service(db_session)
    existing = service.create_or_upgrade(
        "已有标准题允许不完整解析直接复用",
        _analysis(answer="标准答案", analysis="已经校验的标准解析内容。"),
        actor=student_user,
        context=AnalysisContext(grade=8),
    )

    matched = service.create_or_upgrade(
        "1. 已有标准题允许不完整解析直接复用",
        _analysis(is_complete=False, answer="", analysis=""),
        actor=student_user,
        context=AnalysisContext(grade=9),
    )

    assert matched.id == existing.id
    assert matched.answer == "标准答案"
    assert matched.analysis_markdown == "已经校验的标准解析内容。"
    assert matched.grade == 8
    assert db_session.query(func.count(CanonicalQuestion.id)).filter_by(
        normalized_hash=stem_hash("已有标准题允许不完整解析直接复用")
    ).scalar() == 1


def test_similar_canonical_questions_uses_collection_contract(monkeypatch):
    store = QuestionVectorStore()
    store.settings = SimpleNamespace(rag_filter_oversample=4)
    collection = Mock()
    collection.query.return_value = {
        "ids": [["41", "7", "99"]],
        "distances": [[0.0, 0.1, 0.25]],
        "documents": [["教师标准题", "不同学科题", "学生标准题"]],
        "metadatas": [[
            {"subject": "math", "grade": 9, "region": "北京"},
            {"subject": "physics", "grade": 9, "region": "北京"},
            {"subject": "math", "grade": 9, "region": "北京"},
        ]],
    }
    monkeypatch.setattr(store, "_ensure_bank_collection", lambda: collection)

    result = store.similar_canonical_questions(
        "查询题干",
        constraints=SimilarConstraints(subject="math", grade=9, region="北京"),
        top_k=2,
    )

    collection.query.assert_called_once_with(query_texts=["查询题干"], n_results=8)
    assert [hit.bank_id for hit in result.hits] == ["41", "99"]
    assert [hit.distance for hit in result.hits] == [0.0, 0.25]
    assert [hit.snippet for hit in result.hits] == ["教师标准题", "学生标准题"]
    assert all(hit.question_id == -1 and hit.source == "bank" for hit in result.hits)
    assert result.hits[0].metadata["grade"] == 9


@pytest.mark.parametrize(
    "raw_result",
    [
        None,
        {},
        {"ids": None},
        {"ids": [["41"]], "distances": None, "documents": None, "metadatas": None},
    ],
)
def test_similar_canonical_questions_handles_missing_collection_results(
    monkeypatch, raw_result
):
    store = QuestionVectorStore()
    collection = Mock()
    collection.query.return_value = raw_result
    monkeypatch.setattr(store, "_ensure_bank_collection", lambda: collection)

    assert store.similar_canonical_questions("查询题干").hits == []


def _run_teacher_student_race(tmp_path, monkeypatch, winner: str):
    engine = create_engine(
        f"sqlite:///{tmp_path / f'teacher-student-race-{winner}.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory() as session:
        session.add_all(
            [
                User(id=1, username="race-student", password_hash="x", role="student"),
                User(id=2, username="race-teacher", password_hash="x", role="teacher"),
            ]
        )
        session.commit()

    gate = threading.Barrier(2)
    winner_committed = threading.Event()
    original_create = QuestionBankRepository.create

    def ordered_create(self, **kwargs):
        gate.wait(timeout=5)
        if kwargs["origin"] != winner and not winner_committed.wait(timeout=5):
            raise RuntimeError("竞争获胜方未及时提交")
        return original_create(self, **kwargs)

    monkeypatch.setattr(QuestionBankRepository, "create", ordered_create)
    service = QuestionBankService(
        session_factory=session_factory, vector_store=FakeVectorStore()
    )
    actors = {
        "student": User(id=1, username="race-student", password_hash="x", role="student"),
        "teacher": User(id=2, username="race-teacher", password_hash="x", role="teacher"),
    }
    analyses = {
        "student": _analysis(
            answer="学生答案", analysis="学生竞争解析内容。", question_type="学生题型"
        ),
        "teacher": _analysis(
            answer="教师答案", analysis="教师竞争解析内容。", question_type="教师题型"
        ),
    }
    stem = f"师生并发保留教师版本-{winner}"
    outcomes: dict[str, int] = {}
    errors: list[BaseException] = []

    def create(role: str) -> None:
        try:
            question = service.create_or_upgrade(
                stem,
                analyses[role],
                actor=actors[role],
                context=AnalysisContext(),
            )
            outcomes[role] = question.id
        except BaseException as exc:
            errors.append(exc)
        finally:
            if role == winner:
                winner_committed.set()

    threads = [threading.Thread(target=create, args=(role,)) for role in actors]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()

    assert errors == []
    assert set(outcomes) == {"student", "teacher"}
    assert outcomes["student"] == outcomes["teacher"]
    with session_factory() as session:
        rows = session.execute(
            select(CanonicalQuestion).where(
                CanonicalQuestion.normalized_hash == stem_hash(stem)
            )
        ).scalars().all()
        assert len(rows) == 1
        return rows[0]


def test_teacher_losing_create_race_upgrades_after_unique_conflict(
    tmp_path, monkeypatch
):
    stored = _run_teacher_student_race(tmp_path, monkeypatch, winner="student")

    assert stored.origin == "teacher"
    assert stored.validation_state == "teacher_published"
    assert stored.published_by == 2
    assert stored.answer == "教师答案"
    assert stored.analysis_markdown == "教师竞争解析内容。"
    assert stored.question_type == "教师题型"


def test_student_losing_create_race_preserves_teacher_version(tmp_path, monkeypatch):
    stored = _run_teacher_student_race(tmp_path, monkeypatch, winner="teacher")

    assert stored.origin == "teacher"
    assert stored.validation_state == "teacher_published"
    assert stored.published_by == 2
    assert stored.answer == "教师答案"
    assert stored.analysis_markdown == "教师竞争解析内容。"
    assert stored.question_type == "教师题型"
