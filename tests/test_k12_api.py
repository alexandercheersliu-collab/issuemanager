"""K12 录入上下文的 API 级测试：传参 → 解析管线 → 落库 → 筛选。"""
from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from api.main import create_app


@pytest.fixture(scope="module")
def client():
    return TestClient(create_app())


@pytest.fixture(scope="module")
def headers(client):
    resp = client.post(
        "/api/auth/login", json={"username": "demo", "password": "demo123"}
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _tiny_jpeg() -> bytes:
    image = Image.new("RGB", (10, 10), (200, 120, 60))
    stream = io.BytesIO()
    image.save(stream, format="JPEG")
    return stream.getvalue()


def test_analyze_endpoint_accepts_k12_context(client, headers):
    """POST /api/questions/analyze 携带学科/年级/地区/教材版本并落库。"""
    resp = client.post(
        "/api/questions/analyze",
        headers=headers,
        files={"image": ("q.jpg", _tiny_jpeg(), "image/jpeg")},
        data={
            "subject": "physics",
            "grade": "9",
            "region": "北京",
            "textbook_version": "人教版",
        },
    )
    assert resp.status_code == 201, resp.text
    question = resp.json()["question"]
    assert question["subject"] == "physics"
    assert question["grade"] == 9
    assert question["region"] == "北京"
    assert question["textbook_version"] == "人教版"
    # Mock 演示模式：AI 结构化字段随解析落库
    assert question["question_type"] == "解答题"
    assert question["chapter"] == "一元二次方程"
    assert question["error_category"] == "概念不清"


def test_analyze_endpoint_rejects_invalid_grade(client, headers):
    resp = client.post(
        "/api/questions/analyze",
        headers=headers,
        files={"image": ("q.jpg", _tiny_jpeg(), "image/jpeg")},
        data={"grade": "13"},
    )
    assert resp.status_code == 422


def test_analyze_async_endpoint_accepts_k12_context(client, headers):
    resp = client.post(
        "/api/questions/analyze/async",
        headers=headers,
        files={"image": ("q.jpg", _tiny_jpeg(), "image/jpeg")},
        data={"subject": "chemistry", "grade": "11", "region": "上海"},
    )
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["job_id"]

    import time

    for _ in range(50):
        status_resp = client.get(f"/api/jobs/{job_id}", headers=headers)
        assert status_resp.status_code == 200
        body = status_resp.json()
        if body["status"] in ("success", "failed"):
            break
        time.sleep(0.1)
    assert body["status"] == "success", body

    question_id = body["result"]["question_id"]
    question = client.get(f"/api/questions/{question_id}", headers=headers).json()
    assert question["subject"] == "chemistry"
    assert question["grade"] == 11
    assert question["region"] == "上海"


def test_text_endpoint_accepts_k12_metadata(client, headers):
    resp = client.post(
        "/api/questions/text",
        headers=headers,
        json={
            "content_markdown": "API 文本录入：求 x^2=4 的解。",
            "answer": "x=±2",
            "subject": "math",
            "grade": 7,
            "region": "江苏",
            "textbook_version": "苏科版",
            "question_type": "计算题",
            "chapter": "平方根",
            "error_category": "粗心其他",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["grade"] == 7
    assert body["textbook_version"] == "苏科版"
    assert body["question_type"] == "计算题"
    assert body["error_category"] == "粗心其他"


def test_list_endpoint_k12_filters(client, headers):
    by_subject = client.get(
        "/api/questions", headers=headers, params={"subject": "physics", "limit": 100}
    )
    assert by_subject.status_code == 200
    items = by_subject.json()
    assert items and all(q["subject"] == "physics" for q in items)

    by_grade = client.get(
        "/api/questions", headers=headers, params={"grade": 7, "limit": 100}
    )
    assert by_grade.status_code == 200
    assert all(q["grade"] == 7 for q in by_grade.json())

    by_category = client.get(
        "/api/questions", headers=headers, params={"error_category": "粗心其他", "limit": 100}
    )
    assert by_category.status_code == 200
    assert all(q["error_category"] == "粗心其他" for q in by_category.json())


def test_patch_endpoint_updates_k12_metadata(client, headers):
    created = client.post(
        "/api/questions/text",
        headers=headers,
        json={"content_markdown": "待 PATCH 的题"},
    )
    assert created.status_code == 201
    qid = created.json()["id"]

    patched = client.patch(
        f"/api/questions/{qid}",
        headers=headers,
        json={"subject": "biology", "grade": 10, "error_category": "审题偏差"},
    )
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["subject"] == "biology"
    assert body["grade"] == 10
    assert body["error_category"] == "审题偏差"
