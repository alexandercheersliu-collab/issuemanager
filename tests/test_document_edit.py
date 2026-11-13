"""校对编辑操作测试：合并/拆分/删除/重解析/字段编辑、留痕、confirm 跳过明细。"""
from __future__ import annotations

import time
import uuid

import pytest
from doc_samples import make_text_pdf
from PIL import Image

from backend.database import SessionLocal
from backend.models.orm import Job
from backend.models.schemas import FigureSpec
from backend.services.document_service import DocumentService
from backend.services.geometry_figures import FigureOutcome, GeometryFigureService
from backend.services.tikz import TikzRenderer


def _wait_job(service: DocumentService, job_id: str, user_id: int, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = service.get_job(job_id, user_id)
        assert job is not None
        if job["status"] in ("success", "failed"):
            return job
        time.sleep(0.1)
    raise TimeoutError(f"任务 {job_id} 在 {timeout}s 内未完成")


def _import_and_wait(student_user, tmp_path, name="edit.pdf"):
    pdf = make_text_pdf(
        tmp_path / name,
        ["1. 校对题一：已知 x^2=9，求 x 的值。\n2. 校对题二：计算 1+1 的结果。"],
    )
    service = DocumentService()
    job_id, _ = service.import_document(
        student_user.id, pdf.read_bytes(), name, subject="math", grade=9
    )
    job = _wait_job(service, job_id, student_user.id)
    assert job["status"] == "success", job["error"]
    assert job["result"]["total"] == 2
    return service, job_id


def _segments(service: DocumentService, job_id: str, user_id: int) -> list[dict]:
    job = service.get_job(job_id, user_id)
    assert job is not None
    return job["result"]["segments"]


def test_update_segment_fields_and_clears_review(student_user, tmp_path):
    service, job_id = _import_and_wait(student_user, tmp_path)

    entry = service.update_segment(
        job_id,
        student_user.id,
        0,
        text="1. 校对题一（已修订）：求 x。",
        analysis_updates={"answer": "x=±3", "非白名单字段": "应被忽略"},
    )
    assert entry["text"] == "1. 校对题一（已修订）：求 x。"
    assert entry["analysis"]["answer"] == "x=±3"
    assert "非白名单字段" not in entry["analysis"]
    assert entry["needs_review"] is False
    assert entry["error"] is None

    job = service.get_job(job_id, student_user.id)
    log = job["result"]["edit_log"]
    assert any(item["action"] == "update" and item["index"] == 0 for item in log)


def test_update_segment_rejects_invalid_job_and_index(student_user, tmp_path):
    service, job_id = _import_and_wait(student_user, tmp_path, "invalid.pdf")
    with pytest.raises(LookupError):
        service.update_segment("no-such-job", student_user.id, 0, text="x")
    with pytest.raises(IndexError):
        service.update_segment(job_id, student_user.id, 99, text="x")


def test_merge_segments(student_user, tmp_path):
    service, job_id = _import_and_wait(student_user, tmp_path, "merge.pdf")
    first_spec = {
        "kind": "geometry",
        "semantic_spec": {"part": 1},
        "tikz_source": "first",
        "render_path": "first.png",
        "crop_path": None,
        "validation_status": "ai_passed",
        "validation_issues": [],
        "assumptions": [],
    }
    second_spec = {
        "kind": "geometry",
        "semantic_spec": {"part": 2},
        "tikz_source": "second",
        "render_path": None,
        "crop_path": "second.png",
        "validation_status": "fallback",
        "validation_issues": [],
        "assumptions": [],
    }
    job = service.get_job(job_id, student_user.id)
    result = job["result"]
    result["segments"][0]["figure_paths"] = ["first.png"]
    result["segments"][0]["figure_specs"] = [first_spec]
    result["segments"][1]["figure_paths"] = ["second.png"]
    result["segments"][1]["figure_specs"] = [second_spec]
    service._save_result(job_id, result)

    merged = service.merge_segments(job_id, student_user.id, 0)
    assert "校对题一" in merged["text"] and "校对题二" in merged["text"]
    assert merged["needs_review"] is True  # 结构变动需人工确认
    assert merged["analysis"] is not None  # 解构结果优先保留前者
    assert merged["figure_paths"] == ["first.png", "second.png"]
    assert merged["figure_specs"] == [first_spec, second_spec]

    segments = _segments(service, job_id, student_user.id)
    assert len(segments) == 1
    job = service.get_job(job_id, student_user.id)
    assert job["result"]["total"] == 1  # total/done 同步
    assert job["result"]["done"] == 1

    with pytest.raises(IndexError):
        service.merge_segments(job_id, student_user.id, 0)  # 已无下一题


def test_split_segment(student_user, tmp_path):
    service, job_id = _import_and_wait(student_user, tmp_path, "split.pdf")
    spec = {
        "kind": "geometry",
        "semantic_spec": {"part": 1},
        "tikz_source": "first",
        "render_path": "first.png",
        "crop_path": None,
        "validation_status": "ai_passed",
        "validation_issues": [],
        "assumptions": [],
    }
    job = service.get_job(job_id, student_user.id)
    result = job["result"]
    result["segments"][0]["figure_paths"] = ["first.png"]
    result["segments"][0]["figure_specs"] = [spec]
    service._save_result(job_id, result)
    original = _segments(service, job_id, student_user.id)[0]
    split_at = len(original["text"]) // 2

    first, second = service.split_segment(job_id, student_user.id, 0, split_at)
    assert first["figure_paths"] == ["first.png"]
    assert second["figure_paths"] == []
    assert first["figure_specs"] == [spec]
    assert second["figure_specs"] == []
    assert first["text"] + "" != ""
    assert first["text"] == original["text"][:split_at].rstrip()
    assert second["text"] == original["text"][split_at:].lstrip()
    assert second["number"].startswith(original["number"])
    assert second["analysis"] is None  # 后半题需重新解构
    assert first["needs_review"] is True and second["needs_review"] is True

    segments = _segments(service, job_id, student_user.id)
    assert len(segments) == 3

    with pytest.raises(ValueError):
        service.split_segment(job_id, student_user.id, 0, 0)  # 位置越界


def test_delete_segment(student_user, tmp_path):
    service, job_id = _import_and_wait(student_user, tmp_path, "delete.pdf")

    service.delete_segment(job_id, student_user.id, 0)
    segments = _segments(service, job_id, student_user.id)
    assert len(segments) == 1
    assert "校对题二" in segments[0]["text"]

    job = service.get_job(job_id, student_user.id)
    assert job["result"]["total"] == 1
    assert any(item["action"] == "delete" for item in job["result"]["edit_log"])


def test_reanalyze_segment(student_user, tmp_path):
    service, job_id = _import_and_wait(student_user, tmp_path, "reanalyze.pdf")
    original = _segments(service, job_id, student_user.id)[0]
    split_at = len(original["text"]) // 2
    service.split_segment(job_id, student_user.id, 0, split_at)

    entry = service.reanalyze_segment(job_id, student_user.id, 1)
    assert entry["analysis"] is not None
    assert entry["analysis"]["answer"]
    assert entry["needs_review"] is False
    assert entry["error"] is None


def test_edit_rejected_after_confirm(question_service, student_user, tmp_path):
    service, job_id = _import_and_wait(student_user, tmp_path, "locked.pdf")
    service.confirm_import(job_id, student_user.id)

    with pytest.raises(ValueError, match="已确认入库"):
        service.update_segment(job_id, student_user.id, 0, text="x")
    with pytest.raises(ValueError, match="已确认入库"):
        service.delete_segment(job_id, student_user.id, 0)


def test_confirm_reports_skipped(question_service, student_user, tmp_path):
    """拆分产生的未解构题在 confirm 时进入 skipped 明细，其余正常入库。"""
    service, job_id = _import_and_wait(student_user, tmp_path, "skipped.pdf")
    original = _segments(service, job_id, student_user.id)[0]
    service.split_segment(job_id, student_user.id, 0, len(original["text"]) // 2)

    outcome = service.confirm_import(job_id, student_user.id)
    assert outcome["already_confirmed"] is False
    assert outcome["imported"] == 2  # 原第 1 题前半 + 原第 2 题
    assert len(outcome["skipped"]) == 1
    assert outcome["skipped"][0]["reason"] == "未完成结构化解构"

    docs = [
        q
        for q in question_service.list_questions(student_user.id, semantic=False)
        if q.source == "document" and (q.source_doc or "").startswith("skipped.pdf#")
    ]
    assert len(docs) == 2

    again = service.confirm_import(job_id, student_user.id)
    assert again["already_confirmed"] is True
    assert again["skipped"] == outcome["skipped"]  # 幂等返回既有跳过明细


# ---------- TikZ 图形校对 ----------


def _figure_job(student_user, tmp_path):
    page = tmp_path / "page.png"
    Image.new("RGB", (40, 30), "white").save(page)
    old_spec = FigureSpec(
        kind="geometry",
        semantic_spec={"points": ["A", "B"]},
        tikz_source="old tikz",
        render_path="old-render.png",
        crop_path=None,
        validation_status="ai_passed",
        validation_issues=[],
        assumptions=["旧图假设"],
    ).model_dump()
    job_id = uuid.uuid4().hex
    with SessionLocal() as session:
        session.add(
            Job(
                id=job_id,
                user_id=student_user.id,
                type="document_import",
                status="success",
                payload={"filename": "figure.pdf"},
                result={
                    "confirmed": False,
                    "page_images": [str(page)],
                    "segments": [
                        {
                            "number": "1",
                            "text": "如图，求 AB。",
                            "page_start": 1,
                            "page_end": 1,
                            "needs_review": False,
                            "figure_paths": ["old-render.png"],
                            "figure_specs": [old_spec],
                        }
                    ],
                },
            )
        )
        session.commit()
    return DocumentService(), job_id, old_spec


def test_answer_only_edit_preserves_fallback_review_requirement(student_user, tmp_path):
    service, job_id, old_spec = _figure_job(student_user, tmp_path)
    fallback = dict(
        old_spec,
        render_path=None,
        crop_path="fallback-crop.png",
        validation_status="fallback",
        validation_issues=["对比未通过"],
    )
    job = service.get_job(job_id, student_user.id)
    result = job["result"]
    result["segments"][0]["figure_specs"] = [fallback]
    result["segments"][0]["figure_paths"] = ["fallback-crop.png"]
    service._save_result(job_id, result)

    entry = service.update_segment(
        job_id,
        student_user.id,
        0,
        analysis_updates={"answer": "AB=2"},
    )

    assert entry["analysis"]["answer"] == "AB=2"
    assert entry["needs_review"] is True
    assert entry["figure_specs"] == [fallback]
    assert entry["figure_paths"] == ["fallback-crop.png"]


def test_stem_edit_invalidates_passed_comparison_but_preserves_current_figure(
    student_user, tmp_path
):
    service, job_id, _ = _figure_job(student_user, tmp_path)

    entry = service.update_segment(
        job_id,
        student_user.id,
        0,
        text="如图，AB 已改为 4，求 BC。",
    )

    spec = entry["figure_specs"][0]
    assert spec["validation_status"] == "fallback"
    assert any("题干" in issue and "对比" in issue for issue in spec["validation_issues"])
    assert spec["render_path"] == "old-render.png"
    assert entry["figure_paths"] == ["old-render.png"]
    assert entry["needs_review"] is True


class _ReviewService:
    def __init__(self, *, spec=None, error=None):
        self.spec = spec
        self.error = error
        self.calls = []

    def review_tikz(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.spec


class _RedrawService:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []

    def redraw_segment(self, **kwargs):
        self.calls.append(kwargs)
        return self.outcome


def test_reanalyze_legacy_segment_rebuilds_missing_figure_specs(
    student_user, tmp_path, monkeypatch
):
    service, job_id, old_spec = _figure_job(student_user, tmp_path)
    job = service.get_job(job_id, student_user.id)
    result = job["result"]
    result["segments"][0].pop("figure_paths")
    result["segments"][0].pop("figure_specs")
    service._save_result(job_id, result)

    import backend.services.ai as ai_services

    provider = ai_services.get_ai_service(service.settings)
    monkeypatch.setattr(
        provider,
        "locate_figures",
        lambda image_bytes, items, mime_type: [[[0, 0, 1000, 1000]]],
    )
    monkeypatch.setattr(ai_services, "get_ai_service", lambda settings: provider)
    outcome = FigureOutcome(
        figure_paths=["restored-render.png"],
        figure_specs=[old_spec],
        needs_review=False,
    )
    redraw = _RedrawService(outcome)
    monkeypatch.setattr(GeometryFigureService, "from_settings", lambda ai, settings: redraw)

    entry = service.reanalyze_segment(job_id, student_user.id, 0)

    assert entry["figure_paths"] == ["restored-render.png"]
    assert entry["figure_specs"] == [old_spec]
    assert entry["needs_review"] is False
    persisted = _segments(service, job_id, student_user.id)[0]
    assert persisted["figure_specs"] == [old_spec]


def _reject_figure_rebuild(monkeypatch, service):
    import backend.services.ai as ai_services

    provider = ai_services.get_ai_service(service.settings)

    def unexpected_rebuild(*args, **kwargs):
        raise AssertionError("existing figure specs must not be rebuilt")

    monkeypatch.setattr(provider, "locate_figures", unexpected_rebuild)
    monkeypatch.setattr(ai_services, "get_ai_service", lambda settings: provider)
    monkeypatch.setattr(GeometryFigureService, "from_settings", unexpected_rebuild)


def test_reanalyze_preserves_existing_figure_specs_without_rebuild(
    student_user, tmp_path, monkeypatch
):
    service, job_id, old_spec = _figure_job(student_user, tmp_path)
    _reject_figure_rebuild(monkeypatch, service)

    entry = service.reanalyze_segment(job_id, student_user.id, 0)

    assert entry["figure_paths"] == ["old-render.png"]
    assert entry["figure_specs"] == [old_spec]
    assert entry["needs_review"] is False
    persisted = _segments(service, job_id, student_user.id)[0]
    assert persisted["figure_specs"] == [old_spec]


def test_reanalyze_preserves_fallback_review_requirement(
    student_user, tmp_path, monkeypatch
):
    service, job_id, old_spec = _figure_job(student_user, tmp_path)
    fallback = dict(
        old_spec,
        render_path=None,
        crop_path="fallback-crop.png",
        validation_status="fallback",
    )
    job = service.get_job(job_id, student_user.id)
    result = job["result"]
    result["segments"][0]["figure_paths"] = ["fallback-crop.png"]
    result["segments"][0]["figure_specs"] = [fallback]
    service._save_result(job_id, result)
    _reject_figure_rebuild(monkeypatch, service)

    entry = service.reanalyze_segment(job_id, student_user.id, 0)

    assert entry["figure_paths"] == ["fallback-crop.png"]
    assert entry["figure_specs"] == [fallback]
    assert entry["needs_review"] is True
    persisted = _segments(service, job_id, student_user.id)[0]
    assert persisted["figure_specs"] == [fallback]
    assert persisted["needs_review"] is True


def test_recompile_figure_rejects_other_users_and_out_of_range_indices(
    student_user, tmp_path, monkeypatch
):
    service, job_id, _ = _figure_job(student_user, tmp_path)
    reviewer = _ReviewService()
    monkeypatch.setattr(GeometryFigureService, "from_settings", lambda ai, settings: reviewer)

    with pytest.raises(LookupError):
        service.recompile_segment_figure(
            job_id, student_user.id + 999, 0, 0, tikz_source="user tikz"
        )
    with pytest.raises(IndexError):
        service.recompile_segment_figure(
            job_id, student_user.id, 4, 0, tikz_source="user tikz"
        )
    with pytest.raises(IndexError):
        service.recompile_segment_figure(
            job_id, student_user.id, 0, 3, tikz_source="user tikz"
        )
    assert reviewer.calls == []


def test_recompile_figure_rejects_dangerous_tikz_and_preserves_valid_figure(
    student_user, tmp_path, monkeypatch
):
    service, job_id, old_spec = _figure_job(student_user, tmp_path)
    geometry = GeometryFigureService(
        object(),
        TikzRenderer(tmp_path / "missing-pdflatex", tmp_path / "artifacts"),
    )
    monkeypatch.setattr(GeometryFigureService, "from_settings", lambda ai, settings: geometry)
    before = _segments(service, job_id, student_user.id)[0]

    with pytest.raises(ValueError, match="Dangerous LaTeX command"):
        service.recompile_segment_figure(
            job_id,
            student_user.id,
            0,
            0,
            tikz_source=r"\begin{tikzpicture}\immediate\write18{echo bad}\end{tikzpicture}",
        )

    after = _segments(service, job_id, student_user.id)[0]
    assert after["figure_specs"] == [old_spec]
    assert after == before


def test_recompile_figure_compile_failure_preserves_valid_figure(
    student_user, tmp_path, monkeypatch
):
    service, job_id, old_spec = _figure_job(student_user, tmp_path)
    reviewer = _ReviewService(error=RuntimeError("compile failed"))
    monkeypatch.setattr(GeometryFigureService, "from_settings", lambda ai, settings: reviewer)
    before = _segments(service, job_id, student_user.id)[0]

    with pytest.raises(RuntimeError, match="compile failed"):
        service.recompile_segment_figure(
            job_id, student_user.id, 0, 0, tikz_source="broken tikz"
        )

    after = _segments(service, job_id, student_user.id)[0]
    assert after["figure_specs"] == [old_spec]
    assert after == before


def test_recompile_failed_comparison_preserves_trusted_path_until_accepted(
    student_user, tmp_path, monkeypatch
):
    service, job_id, _ = _figure_job(student_user, tmp_path)
    reviewed = FigureSpec(
        kind="geometry",
        semantic_spec={"points": ["C", "D"]},
        tikz_source="user tikz",
        render_path="new-render.png",
        crop_path=None,
        validation_status="fallback",
        validation_issues=["请人工核对"],
        assumptions=["新图假设"],
    )
    reviewer = _ReviewService(spec=reviewed)
    monkeypatch.setattr(GeometryFigureService, "from_settings", lambda ai, settings: reviewer)

    entry = service.recompile_segment_figure(
        job_id, student_user.id, 0, 0, tikz_source="user tikz"
    )

    assert entry["figure_paths"] == ["old-render.png"]
    assert entry["figure_specs"] == [reviewed.model_dump()]
    assert entry["needs_review"] is True
    assert reviewer.calls[0]["tikz_source"] == "user tikz"
    assert reviewer.calls[0]["draft"].tikz_source == "old tikz"
    assert reviewer.calls[0]["figure_box"] is None
    persisted = _segments(service, job_id, student_user.id)[0]
    assert persisted["figure_specs"][0]["validation_status"] == "fallback"
    assert persisted["figure_paths"] == ["old-render.png"]

    accepted = service.accept_segment_figure(job_id, student_user.id, 0, 0)

    assert accepted["figure_paths"] == ["new-render.png"]
    assert accepted["figure_specs"][0]["validation_status"] == "manual_passed"
    assert accepted["needs_review"] is False


def test_recompile_failed_comparison_preserves_other_figure_paths(
    student_user, tmp_path, monkeypatch
):
    service, job_id, old_spec = _figure_job(student_user, tmp_path)
    second = FigureSpec(
        kind="geometry",
        semantic_spec={"figure": 2},
        tikz_source="second old",
        render_path="second-render.png",
        crop_path=None,
        validation_status="ai_passed",
        validation_issues=[],
        assumptions=[],
    ).model_dump()
    job = service.get_job(job_id, student_user.id)
    result = job["result"]
    result["segments"][0]["figure_specs"] = [old_spec, second]
    result["segments"][0]["figure_paths"] = ["old-render.png", "second-render.png"]
    service._save_result(job_id, result)
    reviewed = FigureSpec(
        kind="geometry",
        semantic_spec={"figure": 1},
        tikz_source="first reviewed",
        render_path="first-candidate.png",
        crop_path=None,
        validation_status="fallback",
        validation_issues=["题干与新图不符"],
        assumptions=[],
    )
    monkeypatch.setattr(
        GeometryFigureService,
        "from_settings",
        lambda ai, settings: _ReviewService(spec=reviewed),
    )

    entry = service.recompile_segment_figure(
        job_id, student_user.id, 0, 0, tikz_source="first reviewed"
    )

    assert entry["figure_paths"] == ["old-render.png", "second-render.png"]
    assert entry["figure_specs"][0]["render_path"] == "first-candidate.png"
    assert entry["figure_specs"][1] == second


def _set_two_figures_with_first_path_missing(service, job_id, user_id):
    first = FigureSpec(
        kind="geometry",
        semantic_spec={"figure": 1},
        tikz_source="first old",
        render_path=None,
        crop_path=None,
        validation_status="fallback",
        validation_issues=["裁剪失败"],
        assumptions=[],
    ).model_dump()
    second = FigureSpec(
        kind="geometry",
        semantic_spec={"figure": 2},
        tikz_source="second old",
        render_path="second-render.png",
        crop_path=None,
        validation_status="ai_passed",
        validation_issues=[],
        assumptions=[],
    ).model_dump()
    job = service.get_job(job_id, user_id)
    result = job["result"]
    result["segments"][0]["figure_paths"] = ["second-render.png"]
    result["segments"][0]["figure_specs"] = [first, second]
    service._save_result(job_id, result)


def test_recompile_first_figure_rebuilds_paths_when_earlier_path_was_missing(
    student_user, tmp_path, monkeypatch
):
    service, job_id, _ = _figure_job(student_user, tmp_path)
    _set_two_figures_with_first_path_missing(service, job_id, student_user.id)
    reviewed = FigureSpec(
        kind="geometry",
        semantic_spec={"figure": 1},
        tikz_source="first reviewed",
        render_path="first-reviewed.png",
        crop_path=None,
        validation_status="ai_passed",
        validation_issues=[],
        assumptions=[],
    )
    monkeypatch.setattr(
        GeometryFigureService,
        "from_settings",
        lambda ai, settings: _ReviewService(spec=reviewed),
    )

    entry = service.recompile_segment_figure(
        job_id, student_user.id, 0, 0, tikz_source="first reviewed"
    )

    assert entry["figure_paths"] == ["first-reviewed.png", "second-render.png"]


def test_accept_second_figure_does_not_duplicate_its_compacted_path(student_user, tmp_path):
    service, job_id, _ = _figure_job(student_user, tmp_path)
    _set_two_figures_with_first_path_missing(service, job_id, student_user.id)

    entry = service.accept_segment_figure(job_id, student_user.id, 0, 1)

    assert entry["figure_paths"] == ["second-render.png"]
    assert entry["figure_specs"][1]["validation_status"] == "manual_passed"


def test_accept_figure_preserves_other_fallback_crop_path(student_user, tmp_path):
    service, job_id, _ = _figure_job(student_user, tmp_path)
    job = service.get_job(job_id, student_user.id)
    result = job["result"]
    first = result["segments"][0]["figure_specs"][0]
    fallback = FigureSpec(
        kind="geometry",
        semantic_spec={"figure": 2},
        tikz_source="fallback tikz",
        render_path="fallback-redraw.png",
        crop_path="fallback-crop.png",
        validation_status="fallback",
        validation_issues=["对比未通过"],
        assumptions=[],
    ).model_dump()
    result["segments"][0]["figure_specs"] = [first, fallback]
    result["segments"][0]["figure_paths"] = ["old-render.png", "fallback-crop.png"]
    service._save_result(job_id, result)

    entry = service.accept_segment_figure(job_id, student_user.id, 0, 0)

    assert entry["figure_paths"] == ["old-render.png", "fallback-crop.png"]
    assert entry["figure_specs"][1] == fallback


def test_accept_figure_requires_a_compiled_render(student_user, tmp_path):
    service, job_id, _ = _figure_job(student_user, tmp_path)

    entry = service.accept_segment_figure(job_id, student_user.id, 0, 0)
    assert entry["figure_specs"][0]["validation_status"] == "manual_passed"

    job = service.get_job(job_id, student_user.id)
    result = job["result"]
    result["segments"][0]["figure_specs"][0].update(
        {"render_path": None, "crop_path": "fallback.png", "validation_status": "fallback"}
    )
    service._save_result(job_id, result)
    with pytest.raises(ValueError, match="成功编译"):
        service.accept_segment_figure(job_id, student_user.id, 0, 0)


# ---------- API 级：校对编辑端点 ----------


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    from api.main import create_app

    return TestClient(create_app())


@pytest.fixture(scope="module")
def headers(client):
    resp = client.post(
        "/api/auth/login", json={"username": "demo", "password": "demo123"}
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _api_import_and_wait(client, headers, tmp_path, name="api_edit.pdf") -> str:
    pdf = make_text_pdf(
        tmp_path / name,
        ["1. 接口校对题一：求 x 的值。\n2. 接口校对题二：求 y 的值。"],
    )
    resp = client.post(
        "/api/documents/import",
        headers=headers,
        files={"document": (name, pdf.read_bytes(), "application/pdf")},
        data={"subject": "math"},
    )
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]

    deadline = time.time() + 30
    while time.time() < deadline:
        body = client.get(f"/api/documents/{job_id}/segments", headers=headers).json()
        if body["status"] in ("success", "failed"):
            break
        time.sleep(0.1)
    assert body["status"] == "success", body["error"]
    assert body["total"] == 2
    return job_id


def test_update_segment_endpoint(client, headers, tmp_path):
    job_id = _api_import_and_wait(client, headers, tmp_path)

    resp = client.patch(
        f"/api/documents/{job_id}/segments/0",
        headers=headers,
        json={"text": "1. 接口校对题一（已修订）", "analysis": {"answer": "x=3"}},
    )
    assert resp.status_code == 200, resp.text
    segment = resp.json()["segment"]
    assert segment["text"] == "1. 接口校对题一（已修订）"
    assert segment["analysis"]["answer"] == "x=3"
    assert segment["needs_review"] is False

    missing = client.patch(
        f"/api/documents/{job_id}/segments/99",
        headers=headers,
        json={"text": "x"},
    )
    assert missing.status_code == 404
    unknown_job = client.patch(
        "/api/documents/no-such-job/segments/0",
        headers=headers,
        json={"text": "x"},
    )
    assert unknown_job.status_code == 404


def test_merge_split_delete_endpoints(client, headers, tmp_path):
    job_id = _api_import_and_wait(client, headers, tmp_path, "api_ops.pdf")

    # 拆分第 1 题 → 3 题
    body = client.get(f"/api/documents/{job_id}/segments", headers=headers).json()
    split_at = len(body["segments"][0]["text"]) // 2
    split = client.post(
        f"/api/documents/{job_id}/segments/0/split",
        headers=headers,
        json={"split_at": split_at},
    )
    assert split.status_code == 200, split.text
    assert len(split.json()["segments"]) == 2

    # 合并刚拆出的两题 → 回到 2 题（拆分点可能落在词中间，按两段分别断言）
    merge = client.post(f"/api/documents/{job_id}/segments/0/merge", headers=headers)
    assert merge.status_code == 200, merge.text
    merged_text = merge.json()["segment"]["text"]
    assert body["segments"][0]["text"][:split_at].rstrip() in merged_text
    assert body["segments"][0]["text"][split_at:].lstrip() in merged_text

    # 删除第 2 题 → 剩 1 题
    delete = client.delete(f"/api/documents/{job_id}/segments/1", headers=headers)
    assert delete.status_code == 200
    body = client.get(f"/api/documents/{job_id}/segments", headers=headers).json()
    assert body["total"] == 1

    # 拆分位置非法 → 409
    bad_split = client.post(
        f"/api/documents/{job_id}/segments/0/split",
        headers=headers,
        json={"split_at": 0},
    )
    assert bad_split.status_code == 409


def test_reanalyze_endpoint(client, headers, tmp_path):
    job_id = _api_import_and_wait(client, headers, tmp_path, "api_reanalyze.pdf")

    resp = client.post(f"/api/documents/{job_id}/segments/1/reanalyze", headers=headers)
    assert resp.status_code == 200, resp.text
    segment = resp.json()["segment"]
    assert segment["analysis"] is not None
    assert segment["needs_review"] is False

    persisted = client.get(f"/api/documents/{job_id}/segments", headers=headers)
    assert persisted.status_code == 200
    assert persisted.json()["segments"][1]["analysis"] is not None
    confirm = client.post(f"/api/documents/{job_id}/confirm", headers=headers)
    assert confirm.status_code == 200, confirm.text
    assert confirm.json() == {"imported": 2, "skipped": [], "already_confirmed": False}


def test_edit_endpoints_reject_after_confirm(client, headers, tmp_path):
    job_id = _api_import_and_wait(client, headers, tmp_path, "api_locked.pdf")
    confirm = client.post(f"/api/documents/{job_id}/confirm", headers=headers)
    assert confirm.status_code == 200

    resp = client.delete(f"/api/documents/{job_id}/segments/0", headers=headers)
    assert resp.status_code == 409
    resp = client.post(f"/api/documents/{job_id}/segments/0/merge", headers=headers)
    assert resp.status_code == 409
