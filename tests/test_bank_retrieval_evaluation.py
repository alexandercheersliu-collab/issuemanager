from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.models.orm import Base
from backend.services.question_identity import normalize_stem
from backend.services.rag import RagHit, SimilarResult
from scripts.eval_retrieval import BANK_MATCH_EVALUATION_CASES, evaluate_bank_matching


class EvaluationVectors:
    def similar_canonical_questions(self, query_text, *, constraints, top_k):
        case = next(
            item
            for item in BANK_MATCH_EVALUATION_CASES
            if normalize_stem(item["query"]) == query_text
        )
        if case["similar_distance"] is None:
            return SimilarResult()
        canonical_id = BANK_MATCH_EVALUATION_CASES.index(case) + 1
        return SimilarResult(
            hits=[
                RagHit(
                    question_id=-1,
                    distance=case["similar_distance"],
                    source="bank",
                    bank_id=str(canonical_id),
                )
            ]
        )

    def upsert_bank_question(self, *args, **kwargs):
        return True


def test_bank_evaluation_covers_math_identity_and_reports_separate_metrics(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'bank-evaluation.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    report = evaluate_bank_matching(factory, EvaluationVectors())

    assert {case["category"] for case in BANK_MATCH_EVALUATION_CASES} == {
        "identical",
        "whitespace_latex",
        "ocr_noise",
        "number_changed",
        "same_knowledge_different_question",
    }
    assert report == {
        "cases": 5,
        "exact_accuracy": 1.0,
        "similar_recall": 1.0,
        "false_match_rate": 0.0,
    }
