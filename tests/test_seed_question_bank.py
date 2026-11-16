"""公共种子题库生成器测试：数量/覆盖/字段/确定性/模板答案正确性。"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker

from backend.models.orm import Base, CanonicalQuestion, Question
from backend.models.schemas import SUBJECT_NAMES
from backend.repositories.question_bank import QuestionBankRepository
from backend.services.ai import telemetry
from backend.services.ai.base import AnalysisContext
from backend.services.question_bank import QuestionBankService
from backend.services.question_service import QuestionService
from backend.services.rag import DIFFICULTY_ORDER, SimilarResult

_SPEC = importlib.util.spec_from_file_location(
    "seed_question_bank",
    Path(__file__).resolve().parent.parent / "scripts" / "seed_question_bank.py",
)
seed = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(seed)


@pytest.fixture(scope="module")
def items():
    return seed.generate_bank_items()


def test_template_answers_correct():
    """模板生成函数的答案与题干自洽（抽查各类模板）。"""
    assert seed.add_item(2, 3) == ("计算 2 + 3 的结果。", "5")
    assert seed.mul_item(4, 5)[1] == "20"
    assert seed.div_item(6, 7) == ("计算 42 ÷ 7 的结果。", "6")
    assert seed.sub_item(3, 10)[1] == "7"  # 自动交换保证非负
    assert seed.linear_eq_item(2, 3, 1) == ("解方程：2x + 1 = 7。", "x = 3")
    assert seed.sqrt_item(4) == ("求 16 的平方根。", "±4")
    text, answer = seed.quadratic_item(1, 2)
    assert "x² - 3x + 2 = 0" in text and "x₁ = 1" in answer and "x₂ = 2" in answer
    assert seed.linear_func_item(2, 1, 3)[1] == "7"
    assert seed.derivative_item(3, 2)[1] == "7"  # f'(x)=2x+3, x=2
    assert seed.arithmetic_seq_item(1, 2, 5)[1] == "9"  # 1+(5-1)*2
    assert seed.speed_item(5, 4) == ("物体在 4 s 内匀速通过 20 m，求平均速度（m/s）。", "5")
    assert seed.newton_item(2, 3)[1] == "3"
    assert seed.mole_mass_item(2)[1] == "36 g"
    assert seed.concentration_item(2, 2)[1] == "1 mol/L"


def test_generate_at_least_200(items):
    assert len(items) >= 200


def test_bank_ids_unique(items):
    ids = [item["bank_id"] for item in items]
    assert len(ids) == len(set(ids))


def test_items_have_complete_k12_metadata(items):
    for item in items:
        assert item["content"].strip() and item["answer"].strip()
        assert item["subject"] in SUBJECT_NAMES
        assert 1 <= item["grade"] <= 12
        assert item["difficulty"] in DIFFICULTY_ORDER
        assert item["knowledge_points"]
        assert item["question_type"] and item["chapter"]


def test_grade_coverage_for_math(items):
    math_grades = {item["grade"] for item in items if item["subject"] == "math"}
    assert math_grades == set(range(1, 13))


def test_generation_is_deterministic():
    assert seed.generate_bank_items() == seed.generate_bank_items()


def test_main_dry_run(capsys):
    assert seed.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "共生成" in out and "dry-run" in out


class FakeVectorStore:
    def __init__(self):
        self.upserted = []
        self.hits = []

    def upsert_bank_question(self, bank_id, text, **metadata):
        self.upserted.append((bank_id, text, metadata))
        return True

    def reconcile_bank_questions(self, canonical_ids):
        return True

    def similar_canonical_questions(self, query_text, *, constraints, top_k):
        return SimilarResult(hits=self.hits[:top_k])


@pytest.fixture
def bank_session_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'seed-bank.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    engine.dispose()


def _seed_item(**overrides):
    item = {
        "bank_id": "math-g7-test",
        "content": "解方程：2x + 1 = 7。",
        "analysis": "两边同时减去 1，再除以 2，得到 x = 3。",
        "answer": "x = 3",
        "subject": "math",
        "grade": 7,
        "knowledge_points": ["一元一次方程"],
        "difficulty": "medium",
        "question_type": "计算题",
        "chapter": "一元一次方程",
    }
    item.update(overrides)
    return item


def test_seed_import_persists_complete_sql_question_for_exact_reuse(bank_session_factory):
    vectors = FakeVectorStore()

    result = seed.import_bank_items(
        [_seed_item()], session_factory=bank_session_factory, vector_store=vectors
    )

    assert result == {"created": 1, "reused": 0, "skipped_incomplete": 0, "indexed": 1}
    with bank_session_factory() as session:
        canonical = QuestionBankService(
            session, vector_store=vectors
        ).match_exact("1. 解方程：2x + 1 = 7。\n")
        assert canonical is not None
        assert canonical.origin == "seed"
        assert canonical.analysis_markdown == "两边同时减去 1，再除以 2，得到 x = 3。"
        assert vectors.upserted[0][0] == str(canonical.id)

        from backend.models.orm import User

        student = User(username="seed-student", password_hash="not-used", role="student")
        session.add(student)
        session.commit()
        student_id = student.id

    personal = QuestionService(session_factory=bank_session_factory).reference_canonical(
        student_id, canonical.id, private_image_path="/private/student-original.png"
    )
    assert personal.answer == "x = 3"
    assert "两边同时减去 1" in personal.content_markdown


def test_incomplete_canonical_cannot_be_exact_reused_or_referenced(bank_session_factory):
    vectors = FakeVectorStore()
    with bank_session_factory() as session:
        from backend.models.orm import User

        canonical = QuestionBankRepository(session).create(
            stem_markdown="既有不完整标准题", analysis_markdown="", answer=""
        )
        student = User(username="incomplete-student", password_hash="not-used", role="student")
        session.add(student)
        session.commit()
        canonical_id = canonical.id
        student_id = student.id
        service = QuestionBankService(session, vector_store=vectors)

        assert service.match_exact("既有不完整标准题") is None

    with pytest.raises(ValueError, match="不完整"):
        QuestionService(session_factory=bank_session_factory).reference_canonical(
            student_id, canonical_id, private_image_path="/private/incomplete.png"
        )
    with bank_session_factory() as session:
        assert session.query(func.count(Question.id)).scalar() == 0


@pytest.mark.parametrize("blank", ["", " "])
def test_complete_seed_backfills_same_hash_incomplete_canonical(bank_session_factory, blank):
    vectors = FakeVectorStore()
    with bank_session_factory() as session:
        existing = QuestionBankRepository(session).create(
            stem_markdown="解方程：2x + 1 = 7。", analysis_markdown=blank, answer=blank
        )
        session.commit()
        existing_id = existing.id

    result = seed.import_bank_items(
        [_seed_item()], session_factory=bank_session_factory, vector_store=vectors
    )

    assert result == {"created": 0, "reused": 1, "skipped_incomplete": 0, "indexed": 1}
    with bank_session_factory() as session:
        assert session.query(func.count(CanonicalQuestion.id)).scalar() == 1
        canonical = session.get(CanonicalQuestion, existing_id)
        assert canonical.analysis_markdown == "两边同时减去 1，再除以 2，得到 x = 3。"
        assert canonical.answer == "x = 3"
        assert QuestionBankService(
            session, vector_store=vectors
        ).match_exact("解方程：2x + 1 = 7。").id == existing_id


def test_seed_import_skips_incomplete_and_is_idempotent(bank_session_factory):
    vectors = FakeVectorStore()
    items = [_seed_item(), _seed_item(bank_id="incomplete", content="不完整题", analysis="")]

    first = seed.import_bank_items(
        items, session_factory=bank_session_factory, vector_store=vectors
    )
    second = seed.import_bank_items(
        items, session_factory=bank_session_factory, vector_store=vectors
    )

    assert first == {"created": 1, "reused": 0, "skipped_incomplete": 1, "indexed": 1}
    assert second == {"created": 0, "reused": 1, "skipped_incomplete": 1, "indexed": 1}
    with bank_session_factory() as session:
        assert session.query(func.count(CanonicalQuestion.id)).scalar() == 1
        assert session.query(func.count(Question.id)).scalar() == 0


def test_match_telemetry_counts_outcomes_without_assuming_savings(
    bank_session_factory, tmp_path, monkeypatch
):
    monkeypatch.setattr(telemetry, "_telemetry_path", lambda: tmp_path / "telemetry.jsonl")
    vectors = FakeVectorStore()
    with bank_session_factory() as session:
        canonical = QuestionBankRepository(session).create(
            stem_markdown="可复用题",
            analysis_markdown="完整解析内容",
            answer="答案",
        )
        session.commit()
        service = QuestionBankService(session, vector_store=vectors)
        from types import SimpleNamespace

        service.settings = SimpleNamespace(bank_similarity_threshold=0.7)
        assert service.match_exact("可复用题") is not None
        assert service.match_exact("新题") is None
        assert service.find_similar("新题", context=AnalysisContext()) == []

        from backend.services.rag import RagHit

        vectors.hits = [
            RagHit(question_id=-1, distance=0.2, source="bank", bank_id=str(canonical.id))
        ]
        assert len(service.find_similar("相似题", context=AnalysisContext())) == 1

    summary = telemetry.summarize()
    assert summary["question_bank"]["matches"] == {"exact": 1, "similar": 1, "new": 1}
    assert summary["question_bank"]["skipped_ai_calls"] == {}
    assert summary["token_usage"] is None


def test_telemetry_records_only_provider_supplied_token_usage(tmp_path, monkeypatch):
    monkeypatch.setattr(telemetry, "_telemetry_path", lambda: tmp_path / "telemetry.jsonl")
    with telemetry.track_ai_call("analyze_text"):
        pass

    assert telemetry.summarize()["token_usage"] is None
    assert "token_usage" not in telemetry.read_recent()[0]

    with telemetry.track_ai_call("analyze_text") as ctx:
        ctx["token_usage"] = {
            "prompt_tokens": 3,
            "completion_tokens": 5,
            "total_tokens": 8,
        }

    assert telemetry.summarize()["token_usage"] == {
        "calls_with_usage": 1,
        "prompt_tokens": 3,
        "completion_tokens": 5,
        "total_tokens": 8,
    }


def test_canonical_reindex_uses_authoritative_sql_ids(bank_session_factory):
    with bank_session_factory() as session:
        canonical = QuestionBankRepository(session).create(
            stem_markdown="待重建的标准题",
            analysis_markdown="完整解析",
            answer="答案",
            origin="seed",
        )
        session.commit()
        canonical_id = canonical.id

    spec = importlib.util.spec_from_file_location(
        "reindex_vectors",
        Path(__file__).resolve().parent.parent / "scripts" / "reindex_vectors.py",
    )
    reindex = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reindex)
    vectors = FakeVectorStore()

    assert reindex.reindex_canonical_questions(bank_session_factory, vectors) == (1, 1)
    assert vectors.upserted[0][0] == str(canonical_id)
    assert vectors.upserted[0][1] == "待重建的标准题"


def test_canonical_reindex_removes_stale_bank_ids_and_preserves_personal_vectors(
    bank_session_factory, monkeypatch
):
    from backend.services.rag import QuestionVectorStore

    class FakeBankCollection:
        def __init__(self):
            self.rows = {
                "math-g7-001": {"document": "legacy", "metadata": {"subject": "math"}},
                "999999": {"document": "orphan", "metadata": {"subject": "math"}},
                "77": {"document": "personal", "metadata": {"user_id": 8}},
            }

        def upsert(self, *, ids, documents, metadatas):
            for bank_id, document, metadata in zip(ids, documents, metadatas, strict=True):
                self.rows[bank_id] = {"document": document, "metadata": metadata}

        def get(self, *, include):
            assert include == ["metadatas"]
            return {
                "ids": list(self.rows),
                "metadatas": [row["metadata"] for row in self.rows.values()],
            }

        def delete(self, *, ids):
            for bank_id in ids:
                self.rows.pop(bank_id, None)

    with bank_session_factory() as session:
        canonical = QuestionBankRepository(session).create(
            stem_markdown="仍存在SQL标准题",
            analysis_markdown="完整解析",
            answer="答案",
            origin="seed",
        )
        session.commit()
        canonical_id = canonical.id

    spec = importlib.util.spec_from_file_location(
        "reindex_vectors_reconcile",
        Path(__file__).resolve().parent.parent / "scripts" / "reindex_vectors.py",
    )
    reindex = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reindex)
    collection = FakeBankCollection()
    vectors = QuestionVectorStore()
    monkeypatch.setattr(vectors, "_ensure_bank_collection", lambda: collection)

    assert reindex.reindex_canonical_questions(bank_session_factory, vectors) == (1, 1)
    assert set(collection.rows) == {str(canonical_id), "77"}
    assert collection.rows[str(canonical_id)]["document"] == "仍存在SQL标准题"


def test_personal_reindex_serializes_canonical_references_inside_session(
    bank_session_factory,
):
    with bank_session_factory() as session:
        canonical = QuestionBankRepository(session).create(
            stem_markdown="个人重建引用",
            analysis_markdown="完整解析",
            answer="答案",
        )
        session.add(
            Question(
                user_id=1,
                canonical_id=canonical.id,
                private_image_path="/private/evidence.jpg",
                content_markdown="",
                answer="",
                knowledge_points=[],
                tags=[],
            )
        )
        session.commit()

    spec = importlib.util.spec_from_file_location(
        "reindex_vectors_personal",
        Path(__file__).resolve().parent.parent / "scripts" / "reindex_vectors.py",
    )
    reindex = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reindex)

    class Vectors:
        def __init__(self):
            self.upserted = []

        def upsert_question(self, question_id, text, **metadata):
            self.upserted.append((question_id, text, metadata))
            return True

    vectors = Vectors()
    assert reindex.reindex_personal_questions(bank_session_factory, vectors) == (1, 1)
    assert vectors.upserted[0][1] == "个人重建引用\n\n---\n\n完整解析 答案"
