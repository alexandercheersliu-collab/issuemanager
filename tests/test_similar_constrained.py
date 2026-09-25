"""同类题约束检索测试：K12 硬过滤、键缺失放行、防泄题、逐层放宽、公共题库。

全部走 fake chromadb（复用 test_rag_degradation 的夹具），不出网。
"""
from __future__ import annotations

from test_rag_degradation import _rag_settings
from test_rag_degradation import fake_chromadb as fake_chromadb  # noqa: F401 - pytest 夹具注册

from backend.services.rag import (
    QuestionVectorStore,
    SimilarConstraints,
    difficulty_window,
    matches_constraints,
)


def _store(tmp_path, fake_state, query_ids, query_metas, distances=None):
    """构造注入了固定召回结果的 store。"""
    store = QuestionVectorStore(_rag_settings(tmp_path))
    store._ensure_collection()  # 触发假集合创建
    collection = fake_state["collections"]["questions"]
    n = len(query_ids)
    collection.query_result = {
        "ids": [query_ids],
        "distances": [distances or [0.5] * n],
        "documents": [[f"doc-{i}" for i in query_ids]],
        "metadatas": [query_metas],
    }
    return store


def test_difficulty_window():
    assert difficulty_window("easy") == {"easy", "medium"}
    assert difficulty_window("medium") == {"easy", "medium", "hard"}
    assert difficulty_window("hard") == {"medium", "hard"}
    assert difficulty_window(None) is None
    assert difficulty_window("未知难度") is None


def test_matches_constraints_missing_keys_pass():
    """元数据键缺失的旧文档在对应维度放行（不过滤）。"""
    constraints = SimilarConstraints(
        subject="math", grade=9, region="北京", knowledge_points=["判别式"], difficulty="medium"
    )
    assert matches_constraints({}, constraints, level=0) is True  # 全键缺失 → 全放行
    meta = {"subject": "math", "grade": 9}  # region/kp/difficulty 键缺失
    assert matches_constraints(meta, constraints, level=0) is True
    meta_bad_grade = {"subject": "math", "grade": 3}
    assert matches_constraints(meta_bad_grade, constraints, level=0) is False
    meta_bad_kp = {"subject": "math", "grade": 9, "knowledge_points": "圆,切线"}
    assert matches_constraints(meta_bad_kp, constraints, level=0) is False
    meta_ok_kp = {"subject": "math", "grade": 9, "knowledge_points": "判别式,方程"}
    assert matches_constraints(meta_ok_kp, constraints, level=0) is True


def test_constrained_filter_strict(tmp_path, fake_chromadb):
    """strict 模式：全约束层级过滤，不足也不放宽。"""
    store = _store(
        tmp_path,
        fake_chromadb,
        ["1", "2", "3", "4"],
        [
            {"subject": "math", "grade": 9, "region": "北京", "knowledge_points": "判别式",
             "difficulty": "medium"},
            {"subject": "math", "grade": 3, "region": "北京"},  # 年级不符
            {"subject": "math", "grade": 9},  # region/kp/difficulty 键缺失 → 放行
            {"subject": "physics", "grade": 9, "region": "北京"},  # 学科不符
        ],
    )
    result = store.constrained_similar(
        "判别式求参数范围",
        user_ids=[1],
        constraints=SimilarConstraints(subject="math", grade=9, region="北京"),
        strict=True,
        top_k=5,
    )
    assert {h.question_id for h in result.hits} == {1, 3}
    assert result.level == 0
    assert result.relaxed is False


def test_constrained_difficulty_window(tmp_path, fake_chromadb):
    """难度 ±1 档：目标 easy 接受 easy/medium，拒绝 hard，键缺失放行。"""
    store = _store(
        tmp_path,
        fake_chromadb,
        ["1", "2", "3", "4"],
        [
            {"difficulty": "easy"},
            {"difficulty": "medium"},
            {"difficulty": "hard"},
            {},  # 键缺失放行
        ],
    )
    result = store.constrained_similar(
        "基础计算",
        user_ids=[1],
        constraints=SimilarConstraints(difficulty="easy"),
        strict=True,
        top_k=10,
    )
    assert {h.question_id for h in result.hits} == {1, 2, 4}


def test_leak_filter_default_threshold(tmp_path, fake_chromadb):
    """防泄题：cosine 相似度 ≥ 0.95（distance ≤ 0.05）的候选被过滤。"""
    store = _store(
        tmp_path,
        fake_chromadb,
        ["1", "2", "3"],
        [{}, {}, {}],
        distances=[0.02, 0.06, 0.5],  # 相似度 0.98 / 0.94 / 0.5
    )
    result = store.constrained_similar("原题内容", user_ids=[1], top_k=10)
    assert [h.question_id for h in result.hits] == [2, 3]

    # 旧入口 similar_questions 同样带防泄题；semantic_search 不过滤
    similar = store.similar_questions("原题内容", user_ids=[1], top_k=10)
    assert [h.question_id for h in similar] == [2, 3]
    search = store.semantic_search("原题内容", user_ids=[1], top_k=10)
    assert [h.question_id for h in search] == [1, 2, 3]


def test_leak_filter_threshold_configurable(tmp_path, fake_chromadb):
    """阈值常量化可配：调低阈值后更多近重复被过滤。"""
    store = _store(tmp_path, fake_chromadb, ["1", "2"], [{}, {}], distances=[0.3, 0.5])
    store.settings.rag_leak_threshold = 0.6  # 相似度 ≥0.6 即过滤（distance ≤0.4）
    result = store.constrained_similar("原题", user_ids=[1], top_k=10)
    assert [h.question_id for h in result.hits] == [2]


def test_relax_ladder_records_level(tmp_path, fake_chromadb):
    """宽松模式：全约束无召回时级联放宽（先放地区再放年级），记录生效层级。"""
    store = _store(
        tmp_path,
        fake_chromadb,
        ["1", "2"],
        [
            {"subject": "math", "grade": 9, "region": "上海"},  # 地区不符 → L1 放行
            {"subject": "math", "grade": 3, "region": "上海"},  # 年级不符 → L2 放行
        ],
    )
    constraints = SimilarConstraints(subject="math", grade=9, region="北京")
    result = store.constrained_similar(
        "函数题", user_ids=[1], constraints=constraints, top_k=1
    )
    assert [h.question_id for h in result.hits] == [1]
    assert result.level == 1  # 放到「放地区」层补足的
    assert result.relaxed is True

    strict = store.constrained_similar(
        "函数题", user_ids=[1], constraints=constraints, strict=True, top_k=1
    )
    assert strict.hits == []
    assert strict.level == 0


def test_relax_ladder_keeps_strict_hits_first(tmp_path, fake_chromadb):
    """级联补足：严格层命中排前面，宽松层去重后补满 top_k。"""
    store = _store(
        tmp_path,
        fake_chromadb,
        ["1", "2"],
        [
            {"subject": "math", "grade": 9, "region": "北京"},  # L0 命中
            {"subject": "math", "grade": 9, "region": "上海"},  # L1 才命中
        ],
    )
    result = store.constrained_similar(
        "方程",
        user_ids=[1],
        constraints=SimilarConstraints(subject="math", grade=9, region="北京"),
        top_k=2,
    )
    assert [h.question_id for h in result.hits] == [1, 2]
    assert result.level == 1


def test_bank_upsert_and_similar(tmp_path, fake_chromadb):
    """公共题库：独立 collection、约束过滤、source 标记、防泄题。"""
    store = _store(tmp_path, fake_chromadb, [], [])
    assert store.upsert_bank_question(
        "bank-math-9-001",
        "已知判别式大于零，求 k 的范围",
        subject="math",
        grade=9,
        knowledge_points=["判别式"],
        difficulty="medium",
        answer="k≤1/2",
    )
    bank = fake_chromadb["collections"]["question_bank"]
    assert bank.upserts[0]["ids"] == ["bank-math-9-001"]
    assert bank.upserts[0]["metadatas"][0]["grade"] == 9

    bank.query_result = {
        "ids": [["bank-math-9-001", "bank-math-3-001"]],
        "distances": [[0.4, 0.4]],
        "documents": [["判别式题", "小学计算题"]],
        "metadatas": [
            [{"subject": "math", "grade": 9, "answer": "k≤1/2"},
             {"subject": "math", "grade": 3, "answer": "7"}],
        ],
    }
    result = store.similar_from_bank(
        "判别式范围",
        constraints=SimilarConstraints(subject="math", grade=9),
        strict=True,
    )
    assert len(result.hits) == 1
    hit = result.hits[0]
    assert hit.source == "bank"
    assert hit.bank_id == "bank-math-9-001"
    assert hit.question_id == -1
    assert hit.metadata["answer"] == "k≤1/2"


def test_constrained_empty_constraints_backward_compatible(tmp_path, fake_chromadb):
    """无约束调用等价于旧行为（仅 user_id + 防泄题），level=0。"""
    store = _store(tmp_path, fake_chromadb, ["8", "9"], [{}, {}], distances=[0.2, 0.3])
    result = store.constrained_similar("判别式", user_ids=[1], exclude_id=7, top_k=2)
    assert [h.question_id for h in result.hits] == [8, 9]
    assert result.level == 0
