"""题目、整卷与检测图形资源的鉴权读取契约。"""
from __future__ import annotations

import base64
import datetime as dt
import uuid

import pytest

from backend.database import SessionLocal
from backend.models.orm import Job
from backend.models.schemas import QuestionOut
from backend.services.question_mixins import SimilarQuestionsOutcome
from backend.services.question_service import QuestionService

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


@pytest.fixture
def headers(client):
    response = client.post(
        "/api/auth/login", json={"username": "demo", "password": "demo123"}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture
def other_headers(client):
    response = client.post(
        "/api/auth/login", json={"username": "admin", "password": "admin123"}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _png(path):
    path.write_bytes(_PNG)
    return path


def _question_with_figure(question_service, student_user, figure_path):
    return question_service.create_manual_question(
        student_user.id,
        content_markdown="### 题目\n如图求边长。",
        answer="4",
        figure_paths=[str(figure_path)] if figure_path is not None else [],
    )


def _document_job(student_user, figure_path, page_path):
    job_id = uuid.uuid4().hex
    with SessionLocal() as session:
        session.add(
            Job(
                id=job_id,
                user_id=student_user.id,
                type="document_import",
                status="success",
                payload={"filename": "figures.pdf"},
                result={
                    "confirmed": False,
                    "page_images": [str(page_path) if page_path is not None else None],
                    "segments": [
                        {
                            "number": "1",
                            "text": "如图，求 AB。",
                            "page_start": 1,
                            "page_end": 1,
                            "needs_review": False,
                            "figure_paths": [
                                str(figure_path) if figure_path is not None else None
                            ]
                            if figure_path is not None
                            else [],
                            "figure_specs": [],
                        }
                    ],
                },
            )
        )
        session.commit()
    return job_id


def _assert_png(response):
    assert response.status_code == 200, response.text
    assert response.content == _PNG
    assert response.headers["content-type"].startswith("image/png")


def test_question_list_detail_and_review_share_stem_analysis_and_figure_count(
    client, headers, question_service, student_user, tmp_path
):
    question = question_service.create_manual_question(
        student_user.id,
        content_markdown="### 题目\n文档题干\n\n### 解析\n文档解析",
        answer="4",
        figure_paths=[str(_png(tmp_path / "content.png"))],
    )

    responses = [
        client.get("/api/questions?limit=100", headers=headers),
        client.get(f"/api/questions/{question.id}", headers=headers),
        client.get("/api/review/due", headers=headers),
    ]
    for response in responses:
        assert response.status_code == 200, response.text
        payload = response.json()
        body = (
            next(item for item in payload if item["id"] == question.id)
            if isinstance(payload, list)
            else payload
        )
        assert body["stem"] == "文档题干"
        assert body["analysis"] == "文档解析"
        assert len(body["figure_paths"]) == 1


def test_question_figure_serves_owned_png_and_hides_other_users(
    client, headers, other_headers, question_service, student_user, tmp_path
):
    question = _question_with_figure(
        question_service, student_user, _png(tmp_path / "question.png")
    )
    url = f"/api/questions/{question.id}/figures/0"

    _assert_png(client.get(url, headers=headers))
    assert client.get(url, headers=other_headers).status_code == 404
    assert client.get(f"/api/questions/{question.id}/figures/-1", headers=headers).status_code == 404
    assert client.get(f"/api/questions/{question.id}/figures/1", headers=headers).status_code == 404
    assert client.get("/api/questions/999999999/figures/0", headers=headers).status_code == 404


def test_question_figure_missing_path_and_file_return_404(
    client, headers, question_service, student_user, tmp_path
):
    without_path = _question_with_figure(question_service, student_user, None)
    with_missing_file = _question_with_figure(
        question_service, student_user, tmp_path / "not-created.png"
    )

    assert client.get(
        f"/api/questions/{without_path.id}/figures/0", headers=headers
    ).status_code == 404
    assert client.get(
        f"/api/questions/{with_missing_file.id}/figures/0", headers=headers
    ).status_code == 404


def test_document_segment_figure_and_page_serve_owned_pngs(
    client, headers, other_headers, student_user, tmp_path
):
    job_id = _document_job(
        student_user,
        _png(tmp_path / "segment.png"),
        _png(tmp_path / "page.png"),
    )
    figure_url = f"/api/documents/{job_id}/segments/0/figures/0"
    page_url = f"/api/documents/{job_id}/pages/1"

    _assert_png(client.get(figure_url, headers=headers))
    _assert_png(client.get(page_url, headers=headers))
    assert client.get(figure_url, headers=other_headers).status_code == 404
    assert client.get(page_url, headers=other_headers).status_code == 404


def test_document_resources_reject_bad_indices_and_unknown_jobs(
    client, headers, student_user, tmp_path
):
    job_id = _document_job(
        student_user,
        _png(tmp_path / "indices-figure.png"),
        _png(tmp_path / "indices-page.png"),
    )

    for url in (
        f"/api/documents/{job_id}/segments/-1/figures/0",
        f"/api/documents/{job_id}/segments/1/figures/0",
        f"/api/documents/{job_id}/segments/0/figures/-1",
        f"/api/documents/{job_id}/segments/0/figures/1",
        f"/api/documents/{job_id}/pages/-1",
        f"/api/documents/{job_id}/pages/0",
        f"/api/documents/{job_id}/pages/2",
        "/api/documents/no-such-job/segments/0/figures/0",
        "/api/documents/no-such-job/pages/1",
    ):
        assert client.get(url, headers=headers).status_code == 404, url


def test_document_resources_missing_paths_and_files_return_404(
    client, headers, student_user, tmp_path
):
    missing_paths = _document_job(student_user, None, None)
    missing_files = _document_job(
        student_user, tmp_path / "missing-figure.png", tmp_path / "missing-page.png"
    )

    for job_id in (missing_paths, missing_files):
        assert client.get(
            f"/api/documents/{job_id}/segments/0/figures/0", headers=headers
        ).status_code == 404
        assert client.get(f"/api/documents/{job_id}/pages/1", headers=headers).status_code == 404


def _start_detection_with_figure(
    client,
    headers,
    question_service,
    student_user,
    monkeypatch,
    figure_path,
    source,
):
    original = question_service.create_manual_question(
        student_user.id, content_markdown="检测资源原题", answer="x=1"
    )
    tested = QuestionOut(
        id=-77 if source == "generated" else original.id + 100000,
        user_id=0 if source == "generated" else student_user.id,
        image_path=None,
        content_markdown="### 题目\n检测图形题面\n\n### 解析\n仅题面应下发到 stem",
        answer="" if source == "generated" else "x=1",
        source=source,
        figure_paths=[str(figure_path)],
    )
    monkeypatch.setattr(
        QuestionService,
        "similar_questions",
        lambda self, *args, **kwargs: SimilarQuestionsOutcome(items=[tested]),
    )
    response = client.post(
        f"/api/questions/{original.id}/detection/start", headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.parametrize("source", ["manual", "generated"])
def test_detection_stored_and_generated_figures_have_stem_tokens_and_owner_checked_bytes(
    client,
    headers,
    other_headers,
    question_service,
    student_user,
    monkeypatch,
    tmp_path,
    source,
):
    challenge = _start_detection_with_figure(
        client,
        headers,
        question_service,
        student_user,
        monkeypatch,
        _png(tmp_path / f"{source}.png"),
        source,
    )

    assert challenge["tested"]["source"] == source
    assert challenge["tested"]["stem"] == "检测图形题面"
    assert len(challenge["tested"]["figure_tokens"]) == 1
    url = (
        f"/api/detection/{challenge['log_id']}/figures/0"
        f"?token={challenge['tested']['figure_tokens'][0]}"
    )
    _assert_png(client.get(url, headers=headers))
    assert client.get(url, headers=other_headers).status_code == 404


def test_detection_figure_rejects_expired_tampered_and_mismatched_tokens(
    client,
    headers,
    other_headers,
    question_service,
    student_user,
    monkeypatch,
    tmp_path,
):
    from backend.services.asset_tokens import issue_detection_figure_token

    figure = _png(tmp_path / "signed.png")
    challenge = _start_detection_with_figure(
        client,
        headers,
        question_service,
        student_user,
        monkeypatch,
        figure,
        "generated",
    )
    log_id = challenge["log_id"]
    token = challenge["tested"]["figure_tokens"][0]
    base = f"/api/detection/{log_id}/figures"

    expired = issue_detection_figure_token(
        log_id=log_id,
        user_id=student_user.id,
        figure_index=0,
        path=str(figure),
        now=dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=11),
    )
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")

    assert client.get(f"{base}/0?token={expired}", headers=headers).status_code == 404
    assert client.get(f"{base}/0?token={tampered}", headers=headers).status_code == 404
    assert client.get(f"{base}/1?token={token}", headers=headers).status_code == 404
    assert client.get(f"{base}/-1?token={token}", headers=headers).status_code == 404
    assert client.get(f"{base}/0", headers=headers).status_code == 404
    assert client.get(f"{base}/0?token={token}", headers=other_headers).status_code == 404

    other_log = _start_detection_with_figure(
        client,
        headers,
        question_service,
        student_user,
        monkeypatch,
        figure,
        "generated",
    )
    assert client.get(
        f"/api/detection/{other_log['log_id']}/figures/0?token={token}", headers=headers
    ).status_code == 404
    assert client.get("/api/detection/999999999/figures/0", headers=headers).status_code == 404

    missing = issue_detection_figure_token(
        log_id=log_id,
        user_id=student_user.id,
        figure_index=0,
        path=str(tmp_path / "missing-detection.png"),
    )
    assert client.get(f"{base}/0?token={missing}", headers=headers).status_code == 404
    _assert_png(client.get(f"{base}/0?token={token}&path=/etc/passwd", headers=headers))


def test_detection_figure_token_has_exact_ten_minute_lifetime(tmp_path):
    from backend.services.asset_tokens import (
        issue_detection_figure_token,
        resolve_detection_figure_path,
    )

    figure = _png(tmp_path / "lifetime.png")
    now = dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.timezone.utc)
    token = issue_detection_figure_token(
        log_id=12, user_id=34, figure_index=0, path=str(figure), now=now
    )

    assert resolve_detection_figure_path(
        token,
        log_id=12,
        user_id=34,
        figure_index=0,
        now=now + dt.timedelta(minutes=9, seconds=59),
    ) == figure
    with pytest.raises(ValueError):
        resolve_detection_figure_path(
            token,
            log_id=12,
            user_id=34,
            figure_index=0,
            now=now + dt.timedelta(minutes=10),
        )
