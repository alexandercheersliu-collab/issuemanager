"""复习完成后的「推荐检测」测试：候选筛选纯函数 + 内联检测服务层流程。

全部走 MockProvider / 向量库替身，不调真实 API。
"""
from __future__ import annotations

from backend.services.rag import RagHit, SimilarResult
from frontend.pages.review import pick_detection_candidates

# ---------- 候选筛选纯函数 ----------

def _item(qid: int, grade: str, *, demotion_state: str = "normal", mastered: bool = False):
    return {"id": qid, "grade": grade, "demotion_state": demotion_state, "mastered": mastered}


def test_pick_only_good_grades():
    items = [
        _item(1, "again"),
        _item(2, "hard"),
        _item(3, "good"),
        _item(4, "easy"),
    ]
    picked = pick_detection_candidates(items)
    assert [c["id"] for c in picked] == [3, 4]


def test_pick_excludes_demoted_and_mastered():
    """已降级（demotion_state 终态）与已掌握归档的题不再推荐。"""
    items = [
        _item(1, "easy", demotion_state="demoted"),
        _item(2, "good", mastered=True),
        _item(3, "good"),
    ]
    picked = pick_detection_candidates(items)
    assert [c["id"] for c in picked] == [3]


def test_pick_empty_when_all_wrong():
    """全部答错（again/hard）→ 空候选，界面走鼓励文案分支。"""
    items = [_item(1, "again"), _item(2, "hard"), _item(3, "again")]
    assert pick_detection_candidates(items) == []
    assert pick_detection_candidates([]) == []


def test_pick_respects_limit_and_order():
    items = [_item(i, "good") for i in range(1, 6)]
    picked = pick_detection_candidates(items, limit=3)
    assert [c["id"] for c in picked] == [1, 2, 3]


# ---------- 内联检测流程（服务层，与复习页调用路径一致） ----------

class _StubStore:
    def __init__(self, bank_hits=None):
        self.bank_hits = bank_hits or []

    def constrained_similar(self, query_text, **kwargs) -> SimilarResult:
        return SimilarResult(hits=[], level=0)

    def similar_from_bank(self, query_text, **kwargs) -> SimilarResult:
        return SimilarResult(hits=self.bank_hits[: kwargs.get("top_k") or 3], level=0)


def _bank_hit() -> RagHit:
    return RagHit(
        question_id=-1,
        distance=0.4,
        snippet="公共题库：解方程 x² - 3x + 2 = 0",
        source="bank",
        bank_id="bank-math-9-001",
        metadata={"subject": "math", "grade": 9, "answer": "x=1 或 x=2", "difficulty": "medium"},
    )


def _enrich(service, user_id: int, graded_items: list[dict]) -> list[dict]:
    """模拟复习页完成总结的候选装配：按 id 取最新题目状态再筛选。"""
    enriched = []
    for item in graded_items:
        q = service.get_question(item["id"], user_id)
        if q is None:
            continue
        enriched.append(
            {
                "id": q.id,
                "grade": item["grade"],
                "demotion_state": q.demotion_state,
                "mastered": q.mastered,
            }
        )
    return enriched


def test_inline_detection_flow_and_demotion(question_service, student_user, monkeypatch):
    """复习评 good → 进入候选 → 发起检测 → 作答判分 → 连续 2 次通过触发降级 → 降级后退出候选。"""
    source = question_service.create_manual_question(
        student_user.id,
        content_markdown="复习推荐检测·源题",
        answer="x=1",
        knowledge_points=["一元二次方程"],
        subject="math",
        grade=9,
    )
    monkeypatch.setattr(
        question_service, "vector_store", _StubStore(bank_hits=[_bank_hit()])
    )

    # 1) 复习评 good → 候选筛选命中
    graded = [{"id": source.id, "grade": "good"}]
    picked = pick_detection_candidates(_enrich(question_service, student_user.id, graded))
    assert [c["id"] for c in picked] == [source.id]

    # 2) 内联检测：发起 → 作答 → 判分（连续 2 次正确 → 自动降级）
    for expected_streak in (1, 2):
        challenge = question_service.start_detection(source.id, student_user.id)
        assert challenge["tested"]["source"] == "bank"
        assert "answer" not in challenge["tested"]  # 答案不下发
        outcome = question_service.submit_detection_answer(
            challenge["log_id"], student_user.id, "x=1 或 x=2"
        )
        assert outcome["result"] == "correct"
        assert outcome["streak"] == expected_streak
    assert outcome["demoted"] is True
    assert outcome["state"] == "demoted"

    # 3) 降级后（终态）→ 不再进入推荐候选
    picked_after = pick_detection_candidates(
        _enrich(question_service, student_user.id, graded)
    )
    assert picked_after == []


def test_inline_detection_wrong_answer_keeps_candidate(question_service, student_user, monkeypatch):
    """答错：连续计数清零、状态保持 normal，题仍可被再次推荐。"""
    source = question_service.create_manual_question(
        student_user.id,
        content_markdown="复习推荐检测·答错题",
        answer="x=1",
        knowledge_points=["一元二次方程"],
        subject="math",
        grade=9,
    )
    monkeypatch.setattr(
        question_service, "vector_store", _StubStore(bank_hits=[_bank_hit()])
    )

    challenge = question_service.start_detection(source.id, student_user.id)
    outcome = question_service.submit_detection_answer(
        challenge["log_id"], student_user.id, "x=9"
    )
    assert outcome["result"] == "wrong"
    assert outcome["streak"] == 0
    assert outcome["state"] == "normal"
    assert outcome["demoted"] is False

    graded = [{"id": source.id, "grade": "easy"}]
    picked = pick_detection_candidates(_enrich(question_service, student_user.id, graded))
    assert [c["id"] for c in picked] == [source.id]
