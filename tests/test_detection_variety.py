"""推题去重随机化与连续刷题流测试（缺陷修复：top-k 去重随机 + 再来一道）。

全部走 MockProvider / 向量库替身，不调真实 API；随机断言用固定种子的
random.Random，避免 flaky。
"""
from __future__ import annotations

import random

from backend.database import SessionLocal
from backend.models.orm import DetectionLog
from backend.models.schemas import QuestionOut
from backend.services.detection import pick_tested_candidate
from backend.services.detection import tested_ref_for as _tested_ref_for
from backend.services.rag import RagHit, SimilarResult
from frontend.pages.notebook import detection_progress_text


class _StubStore:
    def __init__(self, own_hits=None, bank_hits=None):
        self.own_hits = own_hits or []
        self.bank_hits = bank_hits or []

    def constrained_similar(self, query_text, **kwargs) -> SimilarResult:
        return SimilarResult(hits=self.own_hits[: kwargs.get("top_k") or 3], level=0)

    def similar_from_bank(self, query_text, **kwargs) -> SimilarResult:
        return SimilarResult(hits=self.bank_hits[: kwargs.get("top_k") or 3], level=0)


def _own(qid: int, text: str = "题") -> QuestionOut:
    return QuestionOut(id=qid, user_id=1, image_path=None, content_markdown=text, answer="1")


def _gen(text: str = "变式题") -> QuestionOut:
    return QuestionOut(
        id=-9999, user_id=0, image_path=None,
        content_markdown=text, answer="", source="generated",
    )


def _bank_hit(bank_id: str, snippet: str, answer: str = "x=1 或 x=2") -> RagHit:
    return RagHit(
        question_id=-1,
        distance=0.4,
        snippet=snippet,
        source="bank",
        bank_id=bank_id,
        metadata={"subject": "math", "grade": 9, "answer": answer, "difficulty": "medium"},
    )


def _make_question(question_service, student_user, text="去重随机·源题", answer="x=1"):
    return question_service.create_manual_question(
        student_user.id,
        content_markdown=text,
        answer=answer,
        knowledge_points=["一元二次方程"],
        subject="math",
        grade=9,
    )


def _ref(log_id: int) -> str:
    with SessionLocal() as session:
        log = session.get(DetectionLog, log_id)
        return log.tested_ref


# ---------- pick_tested_candidate 纯函数 ----------


def test_pick_prefers_fresh_library_candidates():
    """库内未考过候选优先于 generated 变式，且已考过的被排除。"""
    items = [_own(1), _own(2), _gen()]
    tested = {_tested_ref_for(_own(1))}
    for seed in range(20):
        chosen, retest = pick_tested_candidate(items, tested, rng=random.Random(seed))
        assert chosen.id == 2  # 1 已考、变式让位于库内未考题
        assert retest is False


def test_pick_falls_back_to_fresh_variant():
    """库内候选都考过时，用本次新生成的变式（每次生成本身就是新题）。"""
    items = [_own(1), _own(2), _gen()]
    tested = {_tested_ref_for(_own(1)), _tested_ref_for(_own(2))}
    chosen, retest = pick_tested_candidate(items, tested, rng=random.Random(42))
    assert chosen.source == "generated"
    assert retest is False


def test_pick_retest_when_all_tested():
    """候选全部考过：放宽为从已考过的库内题里随机重考，不无题可推。"""
    items = [_own(1), _own(2), _gen()]
    tested = {_tested_ref_for(q) for q in items}
    chosen, retest = pick_tested_candidate(items, tested, rng=random.Random(42))
    assert chosen.id in (1, 2)
    assert retest is True


def test_pick_random_varies_across_draws():
    """随机性：固定种子多次抽取不总是同一道（确定性断言，不 flaky）。"""
    items = [_own(i) for i in range(1, 6)]
    rng = random.Random(42)
    picked = {pick_tested_candidate(items, set(), rng=rng)[0].id for _ in range(20)}
    assert len(picked) > 1


# ---------- 服务层：连续发起去重 + 耗尽放宽 ----------


def test_start_detection_no_repeat_until_exhausted(
    question_service, student_user, monkeypatch
):
    """同一原错题连续发起：候选充足时推题不重复；耗尽后放宽重考不报错。"""
    source = _make_question(question_service, student_user)
    others = [
        _make_question(question_service, student_user, f"去重随机·同类题{i}")
        for i in range(3)
    ]
    monkeypatch.setattr(
        question_service,
        "vector_store",
        _StubStore(own_hits=[RagHit(q.id, 0.4) for q in others]),
    )

    refs = []
    for _ in range(4):  # 3 道库内 + 1 道 generated 变式
        challenge = question_service.start_detection(source.id, student_user.id)
        refs.append(_ref(challenge["log_id"]))
    assert len(set(refs)) == 4  # 候选充足：4 次推题互不重复

    # 第 5 次：候选全部考过 → 放宽重考，仍有题可推
    challenge = question_service.start_detection(source.id, student_user.id)
    assert _ref(challenge["log_id"]) in set(refs)


# ---------- 服务层：连续作答流（答→再发起→新题→再答→计数→降级） ----------


def test_continuous_answer_flow_to_demotion(question_service, student_user, monkeypatch):
    """先答错清零，再连续答对两道不同题 → 计数累计 → 达阈值降级。"""
    source = _make_question(question_service, student_user, "连续刷题·源题")
    hits = [
        _bank_hit("b1", "公共题库：解方程 x² - 3x + 2 = 0"),
        _bank_hit("b2", "公共题库：解方程 x² - 5x + 6 = 0"),
        _bank_hit("b3", "公共题库：解方程 x² - 7x + 12 = 0"),
    ]
    monkeypatch.setattr(
        question_service, "vector_store", _StubStore(bank_hits=hits)
    )

    # 第 1 轮：答错，连续通过清零
    c1 = question_service.start_detection(source.id, student_user.id)
    refs = {_ref(c1["log_id"])}
    out = question_service.submit_detection_answer(c1["log_id"], student_user.id, "x=9")
    assert out["result"] == "wrong"
    assert out["streak"] == 0

    # 第 2/3 轮：每轮推新题（tested_ref 不同），答对后计数累计
    outcome = None
    for expected_streak in (1, 2):
        challenge = question_service.start_detection(source.id, student_user.id)
        ref = _ref(challenge["log_id"])
        assert ref not in refs  # 再来一道不推旧题
        refs.add(ref)
        outcome = question_service.submit_detection_answer(
            challenge["log_id"], student_user.id, "x=1 或 x=2"
        )
        assert outcome["result"] == "correct"
        assert outcome["streak"] == expected_streak
        assert outcome["threshold"] == 2
    assert outcome["demoted"] is True
    assert outcome["state"] == "demoted"


# ---------- 进度文案纯函数 ----------


def test_detection_progress_text():
    assert detection_progress_text(0, 2, "normal") == "连续通过 0/2 次"
    assert detection_progress_text(1, 2, "normal") == "连续通过 1/2 次"
    assert "已掌握降级" in detection_progress_text(2, 2, "demoted")
    # 降级后继续巩固 streak 超过阈值时按阈值封顶展示
    assert detection_progress_text(3, 2, "demoted").startswith("连续通过 2/2 次")
