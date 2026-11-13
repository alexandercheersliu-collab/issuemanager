"""题目内容契约的 API 测试。"""
from __future__ import annotations


def test_document_question_returns_stem_and_analysis(client):
    login = client.post(
        "/api/auth/login", json={"username": "demo", "password": "demo123"}
    )
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    content = "### 题目\n题干\n\n### 解析\n解析"

    response = client.post(
        "/api/questions/text",
        headers=headers,
        json={"content_markdown": content},
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["stem"] == "题干"
    assert body["analysis"] == "解析"
    assert body["content_markdown"] == content
