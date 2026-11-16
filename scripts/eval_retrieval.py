"""检索评测：量化 RRF 混合检索相对单路检索的召回提升（Recall@K）。

方法：以错题自身解析中的关键词为查询，检验目标题是否出现在 top-K。
对比三路：仅关键词（SQL LIKE）/ 仅向量 / RRF 融合。

运行（需先 init_db + 有若干错题）：
    python -m scripts.eval_retrieval
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


BANK_MATCH_EVALUATION_CASES = [
    {
        "category": "identical",
        "canonical_stem": "已知 $x+1=2$，求 $x$。",
        "query": "已知 $x+1=2$，求 $x$。",
        "exact_expected": True,
        "equivalent": True,
        "similar_distance": None,
    },
    {
        "category": "whitespace_latex",
        "canonical_stem": "已知 \\(2x=4\\)，求 \\(x\\)。",
        "query": "1.  已知 $2x=4$，求 $x$。\n",
        "exact_expected": True,
        "equivalent": True,
        "similar_distance": None,
    },
    {
        "category": "ocr_noise",
        "canonical_stem": "解方程：2x + 1 = 7。",
        "query": "解方程：2x + l = 7。",
        "exact_expected": False,
        "equivalent": True,
        "similar_distance": 0.1,
    },
    {
        "category": "number_changed",
        "canonical_stem": "计算 2 + 3。",
        "query": "计算 2 + 4。",
        "exact_expected": False,
        "equivalent": False,
        "similar_distance": 0.5,
    },
    {
        "category": "same_knowledge_different_question",
        "canonical_stem": "已知 x+1=2，求 x。",
        "query": "已知 3x-1=5，求 x。",
        "exact_expected": False,
        "equivalent": False,
        "similar_distance": 0.6,
    },
]


def evaluate_bank_matching(session_factory, vector_store) -> dict:
    """用受控向量结果评测匹配规则；结果不代表真实嵌入模型质量。"""
    from backend.repositories.question_bank import QuestionBankRepository
    from backend.services.ai.base import AnalysisContext
    from backend.services.question_bank import QuestionBankService

    service = QuestionBankService(
        session_factory=session_factory, vector_store=vector_store
    )
    cases = [dict(case) for case in BANK_MATCH_EVALUATION_CASES]
    with session_factory() as session:
        repository = QuestionBankRepository(session)
        for case in cases:
            canonical = repository.create(
                stem_markdown=case["canonical_stem"],
                analysis_markdown="完整解析：按数学关系计算并检验。",
                answer="评测答案",
                origin="teacher",
            )
            session.flush()
            case["canonical_id"] = canonical.id
        session.commit()

    exact_correct = 0
    similar_found = 0
    similar_total = 0
    false_matches = 0
    negative_total = 0
    for case in cases:
        exact = service.match_exact(case["query"])
        expected_id = case["canonical_id"] if case["exact_expected"] else None
        exact_correct += (exact.id if exact is not None else None) == expected_id
        if case["exact_expected"]:
            continue
        candidates = service.find_similar(case["query"], context=AnalysisContext())
        matched = any(
            candidate.question.id == case["canonical_id"] for candidate in candidates
        )
        if case["equivalent"]:
            similar_total += 1
            similar_found += matched
        else:
            negative_total += 1
            false_matches += matched
    return {
        "cases": len(cases),
        "exact_accuracy": exact_correct / len(cases),
        "similar_recall": similar_found / similar_total if similar_total else 0.0,
        "false_match_rate": false_matches / negative_total if negative_total else 0.0,
    }


def main() -> None:
    os.environ.setdefault("AI_PROVIDER", "mock")
    from backend.config import get_settings
    from backend.database import SessionLocal, init_db
    from backend.models.orm import User
    from backend.repositories.questions import QuestionRepository
    from backend.services.fusion import rrf_fuse
    from backend.services.rag import QuestionVectorStore

    init_db(seed_users=True)
    settings = get_settings()
    store = QuestionVectorStore(settings)

    with SessionLocal() as session:
        user = session.query(User).filter_by(username="demo").first()
        if user is None:
            print("无 demo 用户，请先运行应用初始化")
            return
        repo = QuestionRepository(session)
        questions = repo.list_for_user(user.id)
        if len(questions) < 3:
            print(f"错题太少（{len(questions)}），先录入更多数据再评测")
            return

    def keyword_rank(query: str) -> list[int]:
        kw = query.lower()
        hits = [
            q
            for q in questions
            if kw in (q.content_markdown or "").lower()
            or kw in (q.answer or "").lower()
            or kw in (q.ocr_text or "").lower()
            or any(kw in str(t).lower() for t in (q.tags or []))
        ]
        return [q.id for q in hits]

    def vector_rank(query: str) -> list[int]:
        hits = store.semantic_search(query, user_ids=[user.id], top_k=10)
        return [h.question_id for h in hits]

    k_values = (1, 3, 5)
    recall = {"keyword": {k: 0 for k in k_values}, "vector": {k: 0 for k in k_values}, "rrf": {k: 0 for k in k_values}}
    total = 0

    for q in questions:
        # 用该题标签构造查询（模拟真实用户检索词）
        query = (q.tags or [""])[0] if q.tags else None
        if not query:
            continue
        total += 1
        kw_ids = keyword_rank(query)
        vec_ids = vector_rank(query)
        fused = rrf_fuse(vec_ids, kw_ids, top_k=10)

        for k in k_values:
            if q.id in kw_ids[:k]:
                recall["keyword"][k] += 1
            if q.id in vec_ids[:k]:
                recall["vector"][k] += 1
            if q.id in fused[:k]:
                recall["rrf"][k] += 1

    if total == 0:
        print("无可评测样本（错题均无标签）")
        return

    print(f"评测样本：{total} 条查询 · Recall@K（越高越好）")
    print(f"{'K':>3} | {'关键词':>8} | {'向量':>8} | {'RRF融合':>8}")
    print("-" * 40)
    for k in k_values:
        kw_pct = round(recall["keyword"][k] / total * 100)
        vec_pct = round(recall["vector"][k] / total * 100)
        rrf_pct = round(recall["rrf"][k] / total * 100)
        print(f"{k:>3} | {kw_pct:>7}% | {vec_pct:>7}% | {rrf_pct:>7}%")


if __name__ == "__main__":
    main()
