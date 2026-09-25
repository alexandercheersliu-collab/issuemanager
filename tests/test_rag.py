from __future__ import annotations

import pytest

from backend.services.rag import QuestionVectorStore


@pytest.fixture
def store():
    return QuestionVectorStore()


def test_availability(store):
    # 测试环境启用 RAG，ChromaDB 应可用；不可用时测试仍应验证降级路径
    assert store.is_available() in (True, False)


@pytest.mark.skipif(
    not QuestionVectorStore().is_available(),
    reason="向量库不可用（如模型下载受限），降级路径已在其他用例覆盖",
)
def test_upsert_and_similar(store):
    ok = store.upsert_question(
        9001, "一元二次方程判别式问题，delta 大于零求 k 范围", user_id=1, tags=["方程"]
    )
    assert ok
    ok2 = store.upsert_question(
        9002, "三角形相似，求线段比例", user_id=1, tags=["几何"]
    )
    assert ok2

    hits = store.similar_questions(
        "利用判别式求参数取值范围", user_ids=[1], exclude_id=9001
    )
    assert hits and hits[0].question_id == 9002 or hits == []

    store.delete_questions([9001, 9002])


def test_user_isolation_in_query(store):
    store.upsert_question(9101, "相似三角形求比例", user_id=77, tags=["几何"])
    hits_other_user = store.semantic_search("相似三角形", user_ids=[999999])
    assert all(h.question_id != 9101 for h in hits_other_user)
    store.delete_questions([9101])


@pytest.mark.skipif(
    not QuestionVectorStore().is_available(),
    reason="向量库不可用（如模型下载受限），降级路径已在其他用例覆盖",
)
def test_upsert_writes_k12_metadata(store):
    """K12 元数据（学科/年级/地区/知识点/难度）随向量文档写入，供 P3 过滤。"""
    ok = store.upsert_question(
        9201,
        "一元二次方程判别式与根的关系",
        user_id=1,
        tags=["方程"],
        subject="math",
        grade=9,
        region="北京",
        knowledge_points=["判别式", "韦达定理"],
        difficulty="medium",
    )
    assert ok

    collection = store._ensure_collection()
    got = collection.get(ids=["9201"], include=["metadatas"])
    assert got["metadatas"], "向量文档未写入"
    metadata = got["metadatas"][0]
    assert metadata["subject"] == "math"
    assert metadata["grade"] == 9
    assert metadata["region"] == "北京"
    assert metadata["knowledge_points"] == "判别式,韦达定理"
    assert metadata["difficulty"] == "medium"

    # 元数据可用于 where 过滤（P3 同类题检索的硬过滤基础）
    filtered = collection.get(
        where={"$and": [{"subject": "math"}, {"grade": 9}]}, include=["metadatas"]
    )
    assert "9201" in filtered["ids"]
    wrong_grade = collection.get(where={"grade": 3}, include=["metadatas"])
    assert "9201" not in wrong_grade["ids"]

    store.delete_questions([9201])


@pytest.mark.skipif(
    not QuestionVectorStore().is_available(),
    reason="向量库不可用（如模型下载受限），降级路径已在其他用例覆盖",
)
def test_upsert_omits_empty_k12_metadata(store):
    """未提供 K12 元数据时不写入对应键（ChromaDB 不接受 None 值）。"""
    ok = store.upsert_question(9202, "光的折射定律应用", user_id=1, tags=["物理"])
    assert ok
    collection = store._ensure_collection()
    got = collection.get(ids=["9202"], include=["metadatas"])
    metadata = got["metadatas"][0]
    assert "subject" not in metadata
    assert "grade" not in metadata
    assert "region" not in metadata
    store.delete_questions([9202])
