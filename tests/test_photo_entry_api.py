"""拍照录入 REST API：鉴权、上传校验、候选确认与错题本可见性。"""
from __future__ import annotations

import time

import pytest

from backend.models.orm import Job


def _login(client, username: str, password: str) -> dict[str, str]:
    response = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture(scope="module")
def headers(api_client):
    return _login(api_client, "demo", "demo123")


@pytest.fixture(scope="module")
def other_headers(api_client):
    return _login(api_client, "admin", "admin123")


def _create_job(client, headers, *images: tuple[str, bytes, str]) -> str:
    response = client.post(
        "/api/photo-entry/jobs",
        headers=headers,
        files=[("images", image) for image in images],
    )
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "pending"
    return response.json()["job_id"]


def _wait_for_phase(client, headers, job_id: str, phase: str) -> dict:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        response = client.get(f"/api/photo-entry/jobs/{job_id}", headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
        if body["phase"] == phase:
            return body
        if body["phase"] == "failed":
            pytest.fail(f"拍照任务失败：{body['error']}")
        time.sleep(0.02)
    raise TimeoutError(f"任务 {job_id} 未进入 {phase}")


def test_photo_entry_endpoints_require_jwt(client):
    create = client.post(
        "/api/photo-entry/jobs",
        files={"images": ("photo.jpg", b"photo", "image/jpeg")},
    )
    poll = client.get("/api/photo-entry/jobs/unknown")
    confirm = client.post(
        "/api/photo-entry/jobs/unknown/confirm", json={"candidate_ids": []}
    )

    assert create.status_code == 401
    assert poll.status_code == 401
    assert confirm.status_code == 401


def test_multi_image_review_confirm_and_notebook_flow(client, headers):
    job_id = _create_job(
        client,
        headers,
        ("first.jpg", b"first-photo", "image/jpeg"),
        ("second.png", b"second-photo", "image/png"),
    )

    review = _wait_for_phase(client, headers, job_id, "review")
    assert len(review["candidates"]) == 4
    assert [candidate["image"] for candidate in review["candidates"]] == [
        "first.jpg",
        "first.jpg",
        "second.png",
        "second.png",
    ]
    selected = [review["candidates"][1], review["candidates"][2]]
    selected_texts = {candidate["text"] for candidate in selected}

    confirmed = client.post(
        f"/api/photo-entry/jobs/{job_id}/confirm",
        headers=headers,
        json={"candidate_ids": [candidate["id"] for candidate in selected]},
    )
    assert confirmed.status_code == 200, confirmed.text
    done = _wait_for_phase(client, headers, job_id, "done")
    assert done["total"] == 2
    assert done["done"] == 2
    assert len(done["entries"]) == 2
    assert {entry["question"]["stem"] for entry in done["entries"]} == selected_texts

    notebook = client.get(
        "/api/questions", headers=headers, params={"semantic": False, "limit": 100}
    )
    assert notebook.status_code == 200, notebook.text
    saved = {
        question["id"]: question
        for question in notebook.json()
        if question["id"] in {entry["question"]["id"] for entry in done["entries"]}
    }
    assert len(saved) == 2
    assert {question["stem"] for question in saved.values()} == selected_texts
    assert all(question["subject"] == "math" for question in saved.values())
    assert all(question["grade"] == 10 for question in saved.values())
    assert all(question["region"] == "上海" for question in saved.values())
    assert all(question["textbook_version"] == "沪教版" for question in saved.values())


def test_invalid_candidate_id_returns_409(client, headers):
    job_id = _create_job(client, headers, ("invalid.jpg", b"invalid-candidate", "image/jpeg"))
    _wait_for_phase(client, headers, job_id, "review")

    response = client.post(
        f"/api/photo-entry/jobs/{job_id}/confirm",
        headers=headers,
        json={"candidate_ids": ["not-a-candidate"]},
    )

    assert response.status_code == 409, response.text
    assert client.get(f"/api/photo-entry/jobs/{job_id}", headers=headers).json()["phase"] == "review"


def test_unknown_and_cross_user_jobs_return_404(client, headers, other_headers):
    unknown_get = client.get("/api/photo-entry/jobs/not-found", headers=headers)
    unknown_confirm = client.post(
        "/api/photo-entry/jobs/not-found/confirm",
        headers=headers,
        json={"candidate_ids": []},
    )
    job_id = _create_job(client, headers, ("owner.jpg", b"owner-photo", "image/jpeg"))
    cross_user_get = client.get(f"/api/photo-entry/jobs/{job_id}", headers=other_headers)
    cross_user_confirm = client.post(
        f"/api/photo-entry/jobs/{job_id}/confirm",
        headers=other_headers,
        json={"candidate_ids": []},
    )

    assert unknown_get.status_code == 404
    assert unknown_confirm.status_code == 404
    assert cross_user_get.status_code == 404
    assert cross_user_confirm.status_code == 404


@pytest.mark.parametrize(
    ("image", "expected_status"),
    [
        (("empty.jpg", b"", "image/jpeg"), 400),
        (("large.png", b"x" * (10 * 1024 * 1024 + 1), "image/png"), 413),
        (("notes.txt", b"not an image", "text/plain"), 415),
    ],
)
def test_invalid_uploads_are_rejected_before_job_creation(
    client, headers, db_session, image, expected_status
):
    before = db_session.query(Job).filter_by(type="photo_entry").count()

    response = client.post(
        "/api/photo-entry/jobs", headers=headers, files={"images": image}
    )

    assert response.status_code == expected_status, response.text
    assert db_session.query(Job).filter_by(type="photo_entry").count() == before


def test_invalid_grade_is_rejected_before_job_creation(client, headers, db_session):
    before = db_session.query(Job).filter_by(type="photo_entry").count()

    response = client.post(
        "/api/photo-entry/jobs",
        headers=headers,
        files={"images": ("grade.jpg", b"grade-photo", "image/jpeg")},
        data={"grade": "13"},
    )

    assert response.status_code == 422, response.text
    assert db_session.query(Job).filter_by(type="photo_entry").count() == before


def test_similar_confirmation_requires_an_offered_canonical_or_explicit_new_choice(
    client, headers, db_session, monkeypatch
):
    import uuid

    from backend.repositories.question_bank import QuestionBankRepository
    from backend.services.ai.mock import MockProvider
    from backend.services.question_identity import normalize_stem
    from backend.services.rag import RagHit, SimilarResult

    stem = f"API 未精确存在的相似题干-{uuid.uuid4().hex}"
    monkeypatch.setattr(
        MockProvider,
        "segment_page",
        lambda self, *args, **kwargs: [
            {"number": "1", "text": stem},
            {"number": "2", "text": f"另一道题-{uuid.uuid4().hex}"},
        ],
    )
    canonical = QuestionBankRepository(db_session).create(
        stem_markdown=f"API 相似候选标准题-{uuid.uuid4().hex}",
        analysis_markdown="标准解析：计算判别式并检查参数范围。",
        answer="标准答案",
        origin="teacher",
    )
    db_session.commit()

    class Vectors:
        def similar_canonical_questions(self, query_text, *, constraints, top_k):
            hits = []
            if query_text == normalize_stem(stem):
                hits = [
                    RagHit(
                        question_id=-1,
                        distance=0.0,
                        source="bank",
                        bank_id=str(canonical.id),
                    )
                ]
            return SimilarResult(hits=hits)

    monkeypatch.setattr("backend.services.question_bank.QuestionVectorStore", Vectors)
    job_id = _create_job(client, headers, ("similar.jpg", b"api-similar", "image/jpeg"))
    review = _wait_for_phase(client, headers, job_id, "review")
    similar = review["candidates"][0]
    assert similar["match_status"] == "similar"
    assert similar["canonical_id"] is None
    assert [item["question"]["id"] for item in similar["match_candidates"]] == [canonical.id]

    unresolved = client.post(
        f"/api/photo-entry/jobs/{job_id}/confirm",
        headers=headers,
        json={"candidate_ids": [similar["id"]]},
    )
    forged = client.post(
        f"/api/photo-entry/jobs/{job_id}/confirm",
        headers=headers,
        json={
            "candidate_ids": [similar["id"]],
            "match_choices": {similar["id"]: 99999999},
        },
    )
    assert unresolved.status_code == 409, unresolved.text
    assert forged.status_code == 409, forged.text
    assert client.get(f"/api/photo-entry/jobs/{job_id}", headers=headers).json()["phase"] == "review"

    accepted = client.post(
        f"/api/photo-entry/jobs/{job_id}/confirm",
        headers=headers,
        json={
            "candidate_ids": [similar["id"]],
            "match_choices": {similar["id"]: None},
        },
    )
    assert accepted.status_code == 200, accepted.text
    done = _wait_for_phase(client, headers, job_id, "done")
    assert done["entries"][0]["question"]["canonical_id"] != canonical.id
    assert done["entries"][0]["question"]["match_method"] == "new"
