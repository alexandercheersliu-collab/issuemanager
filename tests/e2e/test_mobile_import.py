"""Mobile 拍照录入：真实登录、多图候选、自动回退、去重与任务恢复。"""
from __future__ import annotations

import base64
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from functools import wraps
from pathlib import Path

import pytest
from playwright.sync_api import expect

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        os.getenv("MM_MOBILE_E2E", "") != "1",
        reason="需要 MM_MOBILE_E2E=1；未指定 BASE_URL 时自动启动隔离服务",
    ),
]

PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
_CONTROLLED_SERVER = """
import base64
import time

from backend.config import get_settings
from backend.database import SessionLocal
from backend.models.orm import Question, QuestionAsset, User
from backend.models.schemas import CanonicalQuestionOut, QuestionBankCandidate, QuestionOut
from backend.repositories.question_bank import QuestionBankRepository
from backend.services.ai.mock import MockProvider
from backend.services.geometry_figures import FigureOutcome, GeometryFigureService
from backend.services.photo_entry import PhotoEntryService
from backend.services.question_bank import QuestionBankService
from backend.services.question_mixins import SimilarQuestionsOutcome
from backend.services.question_service import QuestionService
from backend.services.rag import QuestionVectorStore

_original_segment_page = MockProvider.segment_page
_original_analyze_text = MockProvider.analyze_text
_original_redraw_segment = GeometryFigureService.redraw_segment
_original_similar_questions = QuestionService.similar_questions
_original_find_similar = QuestionBankService.find_similar
_original_delete_bank_questions = QuestionVectorStore.delete_bank_questions
_original_confirm = PhotoEntryService.confirm
_reanalyzed = set()
_reanalyze_attempts = {}
_EXACT_STEM = "【受控精确题】直接复用标准解析。"
_SIMILAR_REUSE_STEM = "【受控相似引用】确认后引用候选。"
_SIMILAR_NEW_STEM = "【受控相似新题】明确作为新题解析。"
_NEW_STEM = "【受控新题】没有匹配，执行新题解析。"
_CANDIDATE_STEM = "【受控共享候选】候选标准题干。"
_AB_STEM = "【受控 AB 生命周期题】两个学生依次引用和删除。"
_CONTROLLED_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _item(number, text):
    return {
        "number": number,
        "text": text,
        "continued_from_prev": False,
        "continues_to_next": False,
        "likely_wrong": False,
        "figure_bboxes": [],
    }


def _controlled_segment_page(self, image_bytes, mime_type="image/png"):
    if b"match-statuses" in image_bytes:
        return [
            dict(_item("1", _EXACT_STEM), likely_wrong=True),
            dict(_item("2", _SIMILAR_REUSE_STEM), likely_wrong=True),
            dict(_item("3", _SIMILAR_NEW_STEM), likely_wrong=True),
            dict(_item("4", _NEW_STEM), likely_wrong=True),
        ]
    if b"batch-match" in image_bytes:
        return [
            _item("1", _EXACT_STEM),
            _item("2", _SIMILAR_REUSE_STEM),
            _item("3", "【受控整卷新题】只在本场景执行新题解析。"),
        ]
    if b"ab-exact" in image_bytes:
        return [_item("1", _AB_STEM)]
    if b"single-fallback" in image_bytes or b"duplicate-fallback" in image_bytes:
        return [_item("1", "【演示单题】自动回退整图解析。")]
    if b"batch-first" in image_bytes:
        return [
            _item("1", "【整卷第一页】直接上传的第一页。"),
            _item("2", "【整卷跳过题】此题用于跳过原因。"),
        ]
    if b"batch-second" in image_bytes:
        return [_item("3", "【整卷第二页】直接上传的第二页。")]
    if b"figure-content" in image_bytes:
        figure_item = _item("1", "【整卷图形题】如图，求 AB。")
        figure_item["figure_bboxes"] = [[0, 0, 1, 1]]
        return [figure_item, _item("2", "【旧题无图】直接保留这段题干。")]
    if b"figure-spec-only" in image_bytes:
        item = _item("1", "【无有效图】渲染与裁剪均失败时使用原图。")
        item["figure_bboxes"] = [[0, 0, 1, 1]]
        return [item]
    return _original_segment_page(self, image_bytes, mime_type)


def _controlled_analyze_text(self, text, hint="", context=None):
    if "【整卷跳过题】" in text:
        raise RuntimeError("演示解构失败")
    if "【整卷重解析】" in text and text not in _reanalyzed:
        _reanalyzed.add(text)
        raise RuntimeError("首次解析失败，请重新解析")
    if "【整卷重解析后失败】" in text:
        _reanalyze_attempts[text] = _reanalyze_attempts.get(text, 0) + 1
        if _reanalyze_attempts[text] > 1:
            raise RuntimeError("演示重解析暂时失败")
    return _original_analyze_text(self, text, hint=hint, context=context)


def _controlled_redraw_segment(self, *, image_path, number, text, figure_bboxes, figures_dir, context):
    if "【无有效图】" in text:
        return FigureOutcome(
            figure_paths=[],
            figure_specs=[{
                "kind": "geometry",
                "semantic_spec": {"controlled": True},
                "validation_status": "fallback",
                "tikz_source": "CONTROLLED_TIKZ_SOURCE_NEVER_RENDER",
                "crop_path": None,
            }],
            needs_review=True,
        )
    if "【整卷图形题】" not in text:
        return _original_redraw_segment(
            self,
            image_path=image_path,
            number=number,
            text=text,
            figure_bboxes=figure_bboxes,
            figures_dir=figures_dir,
            context=context,
        )
    figures_dir.mkdir(parents=True, exist_ok=True)
    figure = figures_dir / "controlled-fallback.png"
    figure.write_bytes(image_path.read_bytes())
    return FigureOutcome(
        figure_paths=[str(figure)],
        figure_specs=[{
            "kind": "geometry",
            "semantic_spec": {"controlled": True},
            "validation_status": "fallback",
            "tikz_source": "CONTROLLED_TIKZ_SOURCE_NEVER_RENDER",
            "crop_path": str(figure),
        }],
        needs_review=True,
    )


def _controlled_similar_questions(self, question, *args, **kwargs):
    if not question.figure_paths:
        return _original_similar_questions(self, question, *args, **kwargs)
    tested = QuestionOut(
        id=-8801,
        user_id=0,
        image_path=None,
        content_markdown="### 题目\\n【检测图形题】根据新图求边长。\\n\\n### 解析\\n检测解析不属于题面。",
        answer="4",
        source="generated",
        figure_paths=list(question.figure_paths),
        figure_specs=list(question.figure_specs),
    )
    return SimilarQuestionsOutcome(items=[tested])


def _controlled_find_similar(self, stem, *, context, limit=3):
    if stem not in {_SIMILAR_REUSE_STEM, _SIMILAR_NEW_STEM}:
        return _original_find_similar(self, stem, context=context, limit=limit)
    with SessionLocal() as session:
        question = QuestionBankRepository(session).find_by_stem(_CANDIDATE_STEM)
        return [
            QuestionBankCandidate(
                score=0.87, question=CanonicalQuestionOut.from_orm_model(question)
            )
        ]


def _record_bank_vector_deletion(self, canonical_ids):
    _original_delete_bank_questions(self, canonical_ids)
    log_path = get_settings().data_dir / "bank-vector-deletions.log"
    with log_path.open("a", encoding="utf-8") as log:
        log.write(",".join(canonical_ids) + "\\n")


def _confirm_to_completion(self, job_id, user_id, candidate_ids, match_choices=None):
    _original_confirm(
        self, job_id, user_id, candidate_ids, match_choices=match_choices
    )
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        job = self.get_job(job_id, user_id)
        if job is not None and job["phase"] in {"done", "failed"}:
            return
        time.sleep(0.01)
    raise RuntimeError("controlled photo confirmation did not finish")


def _seed_match_fixtures():
    settings = get_settings()
    shared_dir = settings.data_dir / "question-bank"
    shared_dir.mkdir(parents=True, exist_ok=True)
    with SessionLocal() as session:
        repository = QuestionBankRepository(session)
        for stem, origin in (
            (_EXACT_STEM, "teacher"),
            (_CANDIDATE_STEM, "teacher"),
            (_AB_STEM, "student"),
        ):
            question = repository.create(
                stem_markdown=stem,
                analysis_markdown="受控标准解析摘要：按步骤计算并验证。",
                answer="受控答案",
                knowledge_points=["受控知识点"],
                origin=origin,
            )
            session.flush()
            shared = shared_dir / f"canonical-{question.id}-ai.png"
            private = shared_dir / f"canonical-{question.id}-teacher.png"
            shared.write_bytes(_CONTROLLED_PNG)
            private.write_bytes(_CONTROLLED_PNG)
            session.add_all([
                QuestionAsset(
                    canonical_id=question.id,
                    kind="ai_redraw",
                    visibility="shared",
                    storage_path=str(shared),
                    is_preferred=True,
                ),
                QuestionAsset(
                    canonical_id=question.id,
                    kind="teacher_original",
                    visibility="teacher_only",
                    storage_path=str(private),
                ),
            ])
        docx_stem = "【受控 DOCX 带图】仍须下载原始文档。"
        docx_canonical = repository.create(
            stem_markdown=docx_stem,
            analysis_markdown="受控 DOCX 带图解析。",
            answer="受控答案",
            origin="teacher",
        )
        session.flush()
        student = session.query(User).filter_by(username="demo").one()
        private_dir = settings.data_dir / "images" / f"u{student.id}"
        private_dir.mkdir(parents=True, exist_ok=True)
        evidence = private_dir / "figure-evidence.docx"
        crop = private_dir / "figure-crop.png"
        evidence.write_bytes(b"controlled-docx-with-figure")
        crop.write_bytes(_CONTROLLED_PNG)
        session.add(
            Question(
                user_id=student.id,
                canonical_id=docx_canonical.id,
                private_image_path=str(evidence),
                image_path=str(evidence),
                figure_paths=[],
                figure_specs=[{
                    "kind": "geometry",
                    "semantic_spec": {},
                    "tikz_source": "",
                    "render_path": None,
                    "crop_path": str(crop),
                    "validation_status": "fallback",
                    "validation_issues": [],
                    "assumptions": [],
                }],
                content_markdown="",
                answer="",
                match_method="new",
            )
        )
        session.commit()


MockProvider.segment_page = _controlled_segment_page
MockProvider.analyze_text = _controlled_analyze_text
GeometryFigureService.redraw_segment = _controlled_redraw_segment
QuestionService.similar_questions = _controlled_similar_questions
QuestionBankService.find_similar = _controlled_find_similar
QuestionVectorStore.delete_bank_questions = _record_bank_vector_deletion
PhotoEntryService.confirm = _confirm_to_completion

import api.main as api_main
import uvicorn

_seed_match_fixtures()
uvicorn.run(api_main.app, host="127.0.0.1", port=int(__import__("os").environ["PORT"]))
"""


def _photo(name: str, marker: bytes) -> dict:
    return {"name": name, "mimeType": "image/png", "buffer": PNG_BYTES + marker}


@pytest.fixture(scope="module")
def mobile_server():
    external = os.getenv("MM_MOBILE_E2E_BASE_URL")
    if external:
        yield {"base_url": external, "database_path": None}
        return

    root = Path(__file__).resolve().parents[2]
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    with tempfile.TemporaryDirectory(prefix="mathmaster_mobile_e2e_") as data_dir:
        env = os.environ.copy()
        env.update(
            {
                "PORT": str(port),
                "DATA_DIR": data_dir,
                "DATABASE_URL": f"sqlite:///{Path(data_dir) / 'math_tutor.db'}",
                "CHROMA_DIR": str(Path(data_dir) / "chroma"),
                "CHROMA_MODEL_DIR": str(Path(data_dir) / "models"),
                "DOC_UPLOAD_PATH": str(Path(data_dir) / "uploads" / "docs"),
                "AI_PROVIDER": "mock",
                "RAG_ENABLED": "false",
                "API_RATE_LIMIT_ENABLED": "false",
                "BCRYPT_ROUNDS": "4",
            }
        )
        log_path = Path(data_dir) / "api.log"
        with log_path.open("w", encoding="utf-8") as log:
            process = subprocess.Popen(
                [sys.executable, "-c", _CONTROLLED_SERVER],
                cwd=root,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            try:
                health_url = f"http://127.0.0.1:{port}/health"
                deadline = time.monotonic() + 30
                while True:
                    if process.poll() is not None:
                        raise RuntimeError(
                            f"Controlled Mobile API exited early:\n{log_path.read_text()}"
                        )
                    try:
                        with urllib.request.urlopen(health_url, timeout=0.5) as response:
                            if response.status == 200:
                                break
                    except (urllib.error.URLError, TimeoutError):
                        pass
                    if time.monotonic() >= deadline:
                        raise RuntimeError(
                            f"Controlled Mobile API health check timed out:\n{log_path.read_text()}"
                        )
                    time.sleep(0.1)
                yield {
                    "base_url": f"http://127.0.0.1:{port}/m/",
                    "database_path": Path(data_dir) / "math_tutor.db",
                }
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


@pytest.fixture(scope="module")
def mobile_base_url(mobile_server):
    return mobile_server["base_url"]


def _screenshot_on_failure(test):
    @wraps(test)
    def run(*args, **kwargs):
        try:
            return test(*args, **kwargs)
        except Exception:
            page = kwargs.get("page")
            if page is not None:
                try:
                    page.screenshot(
                        path=f"e2e_failure_mobile_{test.__name__}.png",
                        full_page=True,
                    )
                except Exception:
                    pass
            raise

    return run


@pytest.fixture
def page():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 390, "height": 844})
        page = context.new_page()
        yield page
        context.close()
        browser.close()


def _open_capture(page, base_url: str) -> None:
    page.goto(base_url)
    page.get_by_label("用户名").fill("demo")
    page.get_by_label("密码").fill("demo123")
    page.get_by_role("button", name="登录", exact=True).click()
    page.get_by_role("button", name="录题", exact=True).click()
    expect(page.get_by_role("heading", name="录错题", exact=True)).to_be_visible()


def _assert_no_extra_entry_or_editing_controls(page) -> None:
    nav = page.locator("nav")
    expect(nav.get_by_role("button")).to_have_count(5)
    expect(nav.get_by_role("button")).to_have_text(["复习", "录题", "助手", "错题本", "我的"])
    for text in ("文字录入", "学科", "年级", "地区", "教材版本", "TikZ"):
        expect(page.get_by_text(text, exact=True)).to_have_count(0)
    for name in ("合并", "拆分"):
        expect(page.get_by_role("button", name=name, exact=True)).to_have_count(0)


def _submit_and_wait_for_done(page) -> None:
    page.get_by_role("button", name="开始解析", exact=True).click()
    expect(page.get_by_role("heading", name="录入完成", exact=True)).to_be_visible(timeout=30000)


@_screenshot_on_failure
def test_multi_photo_confirmation_refresh_and_notebook_visibility(page, mobile_base_url):
    """直接整图解析、未恢复任务或完成后残留任务 ID 时，本场景失败。"""
    _open_capture(page, mobile_base_url)
    _assert_no_extra_entry_or_editing_controls(page)
    page.locator('input[type="file"]').set_input_files(
        [_photo("first.png", b"multi-first"), _photo("second.png", b"multi-second")]
    )
    expect(page.locator('img[alt^="错题预览"]')).to_have_count(2)
    page.get_by_role("button", name="开始解析", exact=True).click()

    expect(page.get_by_text("从照片中识别出 4 道题", exact=False)).to_be_visible(timeout=30000)
    candidates = page.get_by_role("checkbox")
    expect(candidates).to_have_count(4)
    expect(candidates.nth(0)).to_be_checked()
    expect(candidates.nth(1)).not_to_be_checked()
    expect(candidates.nth(2)).to_be_checked()
    expect(candidates.nth(3)).not_to_be_checked()
    _assert_no_extra_entry_or_editing_controls(page)

    page.reload()
    page.get_by_role("button", name="录题", exact=True).click()
    expect(page.get_by_text("从照片中识别出 4 道题", exact=False)).to_be_visible(timeout=30000)
    expect(page.get_by_role("checkbox")).to_have_count(4)
    page.get_by_role("button", name="确认录入", exact=True).click()
    expect(page.get_by_role("heading", name="录入完成", exact=True)).to_be_visible(timeout=30000)
    expect(page.get_by_text("已入库 2 道", exact=False)).to_be_visible()
    assert page.evaluate("() => localStorage.getItem('mm_photo_job')") is None

    page.get_by_role("button", name="去错题本看看", exact=True).click()
    expect(page.get_by_role("heading", name="错题本", exact=True)).to_be_visible()
    expect(page.get_by_text("【演示切题】已知关于 x 的一元二次方程", exact=False).first).to_be_visible()
    page.get_by_role("button", name="录题", exact=True).click()
    expect(page.get_by_role("heading", name="录错题", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="拍照 / 从相册选择", exact=False)).to_be_visible()


@_screenshot_on_failure
def test_single_photo_automatically_falls_back_without_candidate_review(page, mobile_base_url):
    """若单题照片仍要求候选确认，或仍走旧同步分析入口，本场景失败。"""
    _open_capture(page, mobile_base_url)
    page.locator('input[type="file"]').set_input_files(
        _photo("single.png", b"single-fallback")
    )
    _submit_and_wait_for_done(page)

    expect(page.get_by_text("已入库 1 道", exact=False)).to_be_visible()
    expect(page.get_by_role("button", name="确认录入", exact=True)).to_have_count(0)
    expect(page.get_by_role("checkbox")).to_have_count(0)


@_screenshot_on_failure
def test_duplicate_photo_is_reported_in_existing_result_view(page, mobile_base_url):
    """若重复照片再次调用分析或不展示去重结果，本场景失败。"""
    _open_capture(page, mobile_base_url)
    duplicate = _photo("duplicate.png", b"duplicate-fallback")
    page.locator('input[type="file"]').set_input_files(duplicate)
    _submit_and_wait_for_done(page)
    expect(page.get_by_text("已入库 1 道", exact=False)).to_be_visible()

    page.get_by_role("button", name="再录一道", exact=True).click()
    page.locator('input[type="file"]').set_input_files(duplicate)
    _submit_and_wait_for_done(page)
    expect(page.get_by_text("重复 1 道", exact=False)).to_be_visible()
    expect(page.get_by_text("已入库 0 道", exact=False)).to_be_visible()


def _open_batch(page, base_url: str) -> None:
    _open_capture(page, base_url)
    page.get_by_role("button", name="拍的是一整张试卷？试试整卷导入 →", exact=True).click()
    expect(page.get_by_role("heading", name="整卷导入", exact=True)).to_be_visible()


@_screenshot_on_failure
def test_batch_pdf_reanalyze_delete_confirm_and_restore(page, mobile_base_url, tmp_path):
    """直接 PDF、失败题重解析、删题、确认及未确认任务恢复必须在原入口完成。"""
    from doc_samples import make_text_pdf

    pdf = make_text_pdf(
        tmp_path / "mobile-batch.pdf",
        ["1. 【整卷重解析】求 x 的值。\n2. 【整卷删除题】求 y 的值。"],
    )
    _open_batch(page, mobile_base_url)
    page.locator('input[type="file"]').set_input_files(pdf)
    expect(page.get_by_text("mobile-batch.pdf", exact=True)).to_be_visible()
    page.get_by_role("button", name="开始识别", exact=False).click()

    expect(page.get_by_role("heading", name="确认识别结果", exact=True)).to_be_visible(timeout=30000)
    expect(page.get_by_text("首次解析失败，请重新解析", exact=False)).to_be_visible()
    expect(page.get_by_text("第 1 页", exact=True).first).to_be_visible()
    expect(page.get_by_text("规则切题", exact=True).first).to_be_visible()
    _assert_no_extra_entry_or_editing_controls(page)

    page.reload()
    page.get_by_role("button", name="录题", exact=True).click()
    expect(page.get_by_role("heading", name="确认识别结果", exact=True)).to_be_visible(timeout=30000)
    expect(page.get_by_text("【整卷重解析】", exact=False)).to_be_visible()

    page.get_by_role("button", name="重新解析", exact=True).first.click()
    expect(page.get_by_text("首次解析失败，请重新解析", exact=False)).to_have_count(0)
    expect(page.get_by_role("button", name="确认入库（2 题）", exact=True)).to_be_enabled()
    page.get_by_role("button", name="删除这题", exact=True).nth(1).click()
    expect(page.get_by_text("【整卷删除题】", exact=False)).to_have_count(0)
    page.get_by_role("button", name="确认入库（1 题）", exact=True).click()
    expect(page.get_by_text("已入库 1 道题", exact=False)).to_be_visible(timeout=30000)
    assert page.evaluate("() => localStorage.getItem('mm_batch_job')") is None


@_screenshot_on_failure
def test_batch_multiple_images_keep_order_and_show_skipped_reasons(
    page, mobile_base_url
):
    """多图必须按页序直接上传；确认后显示数量并可展开真实跳过原因。"""
    _open_batch(page, mobile_base_url)
    page.locator('input[type="file"]').set_input_files(
        [_photo("batch-1.png", b"batch-first"), _photo("batch-2.png", b"batch-second")]
    )
    expect(page.locator('img[alt="第 1 页"]')).to_be_visible()
    expect(page.locator('img[alt="第 2 页"]')).to_be_visible()
    page.get_by_role("button", name="开始识别", exact=False).click()

    expect(page.get_by_role("heading", name="确认识别结果", exact=True)).to_be_visible(timeout=30000)
    expect(page.get_by_text("【整卷第一页】", exact=False)).to_be_visible()
    expect(page.get_by_text("【整卷第二页】", exact=False)).to_be_visible()
    review_text = page.locator("body").inner_text()
    assert review_text.index("【整卷第一页】") < review_text.index("【整卷第二页】")
    expect(page.get_by_text("视觉切题", exact=True).first).to_be_visible()
    expect(page.get_by_text("建议复核", exact=True).first).to_be_visible()
    expect(page.get_by_text("共切出 3 题，2 题可入库", exact=False)).to_be_visible()
    _assert_no_extra_entry_or_editing_controls(page)

    page.get_by_role("button", name="确认入库（2 题）", exact=True).click()
    expect(page.get_by_text("已入库 2 道题", exact=False)).to_be_visible(timeout=30000)
    expect(page.get_by_text("跳过 1 道题", exact=False)).to_be_visible()
    page.get_by_text("查看跳过原因（1）", exact=True).click()
    expect(page.get_by_text("第 2 题", exact=True)).to_be_visible()
    expect(page.get_by_text("演示解构失败", exact=False)).to_be_visible()


@_screenshot_on_failure
def test_batch_failed_reanalysis_keeps_previous_result_and_count(
    page, mobile_base_url, tmp_path
):
    """重解析失败仍按后端语义入库上一次成功分析，并明确提示。"""
    from doc_samples import make_text_pdf

    pdf = make_text_pdf(
        tmp_path / "mobile-reanalyze-failed.pdf",
        ["1. 【整卷重解析后失败】求 x 的值。"],
    )
    _open_batch(page, mobile_base_url)
    page.locator('input[type="file"]').set_input_files(pdf)
    page.get_by_role("button", name="开始识别", exact=False).click()
    expect(page.get_by_role("heading", name="确认识别结果", exact=True)).to_be_visible(timeout=30000)
    expect(page.get_by_role("button", name="确认入库（1 题）", exact=True)).to_be_enabled()

    page.get_by_role("button", name="重新解析", exact=True).click()
    expect(page.get_by_text("重新解析失败，将使用上次成功结果", exact=True)).to_be_visible()
    expect(page.get_by_text("演示重解析暂时失败", exact=True)).to_be_visible()
    expect(page.get_by_text("共切出 1 题，1 题可入库", exact=False)).to_be_visible()
    page.get_by_role("button", name="确认入库（1 题）", exact=True).click()

    expect(page.get_by_text("已入库 1 道题", exact=False)).to_be_visible(timeout=30000)
    expect(page.get_by_text("跳过", exact=False)).to_have_count(0)
    page.get_by_role("button", name="去错题本看看", exact=True).click()
    expect(page.get_by_text("【整卷重解析后失败】", exact=False)).to_be_visible()


@_screenshot_on_failure
def test_batch_restore_retains_job_and_retries_after_network_failure(
    page, mobile_base_url, tmp_path
):
    """恢复请求瞬时失败不能清除任务号，学生可重试并回到校对页。"""
    from doc_samples import make_text_pdf

    pdf = make_text_pdf(
        tmp_path / "mobile-restore-retry.pdf",
        ["1. 【整卷恢复重试】求 x 的值。"],
    )
    _open_batch(page, mobile_base_url)
    page.locator('input[type="file"]').set_input_files(pdf)
    page.get_by_role("button", name="开始识别", exact=False).click()
    expect(page.get_by_role("heading", name="确认识别结果", exact=True)).to_be_visible(timeout=30000)
    job_id = page.evaluate("() => localStorage.getItem('mm_batch_job')")
    assert job_id

    attempts = {"count": 0}

    def fail_first_restore(route):
        attempts["count"] += 1
        if attempts["count"] == 1:
            route.abort("failed")
        else:
            route.continue_()

    page.route(f"**/api/documents/{job_id}/segments", fail_first_restore)
    page.reload()
    page.get_by_role("button", name="录题", exact=True).click()

    assert page.evaluate("() => localStorage.getItem('mm_batch_job')") == job_id
    page.get_by_role("button", name="重试恢复任务", exact=True).click()
    expect(page.get_by_role("heading", name="确认识别结果", exact=True)).to_be_visible(timeout=30000)
    assert page.evaluate("() => localStorage.getItem('mm_batch_job')") == job_id
    assert attempts["count"] == 2


@_screenshot_on_failure
def test_document_content_and_figures_render_in_existing_mobile_flows(page, mobile_base_url):
    """旧拆分逻辑或不展示鉴权图形时，本场景在原有入口中失败。"""
    _open_batch(page, mobile_base_url)
    page.locator('input[type="file"]').set_input_files(
        _photo("figure-content.png", b"figure-content")
    )
    page.get_by_role("button", name="开始识别", exact=False).click()

    expect(page.get_by_role("heading", name="确认识别结果", exact=True)).to_be_visible(timeout=30000)
    expect(page.get_by_role("img", name="整卷题图 1", exact=True)).to_be_visible()
    expect(page.get_by_text("图形为原图裁剪，请核对准确性", exact=True)).to_be_visible()
    expect(page.get_by_text("【旧题无图】直接保留这段题干。", exact=True)).to_be_visible()
    expect(page.get_by_text("CONTROLLED_TIKZ_SOURCE_NEVER_RENDER", exact=False)).to_have_count(0)
    _assert_no_extra_entry_or_editing_controls(page)

    page.get_by_role("button", name="确认入库（2 题）", exact=True).click()
    expect(page.get_by_text("已入库 2 道题", exact=False)).to_be_visible(timeout=30000)
    page.get_by_role("button", name="去错题本看看", exact=True).click()

    page.get_by_text("【整卷图形题】", exact=False).first.click()
    dialog = page.get_by_role("dialog")
    expect(dialog.get_by_role("img", name="题图 1", exact=True)).to_be_visible()
    expect(dialog.get_by_text("【整卷图形题】如图，求 AB。", exact=True)).to_be_visible()
    expect(dialog.get_by_text("【演示模式】", exact=False)).to_be_visible()
    expect(dialog.get_by_role("heading", name="题目", exact=True)).to_have_count(0)
    expect(dialog.get_by_role("heading", name="解析", exact=True)).to_have_count(0)

    dialog.get_by_role("button", name="同类题检测", exact=True).click()
    expect(dialog.get_by_text("【检测图形题】根据新图求边长。", exact=True)).to_be_visible(timeout=30000)
    expect(dialog.get_by_role("img", name="检测题图 1", exact=True)).to_be_visible()
    expect(dialog.get_by_role("heading", name="题目", exact=True)).to_have_count(0)
    expect(dialog.get_by_text("CONTROLLED_TIKZ_SOURCE_NEVER_RENDER", exact=False)).to_have_count(0)
    page.keyboard.press("Escape")
    expect(page.get_by_role("dialog")).to_have_count(0)

    page.get_by_text("【旧题无图】", exact=False).first.click()
    legacy_dialog = page.get_by_role("dialog")
    expect(legacy_dialog.get_by_text("【旧题无图】直接保留这段题干。", exact=True)).to_be_visible()
    expect(legacy_dialog.get_by_role("img", name="题图 1", exact=True)).to_have_count(0)
    page.keyboard.press("Escape")

    page.get_by_role("button", name="复习", exact=True).click()
    _skip_to_review_question(
        page,
        "【整卷图形题】",
        "Imported figure question did not appear in the existing review queue",
    )

    expect(page.get_by_role("img", name="题图 1", exact=True)).to_be_visible()
    expect(page.get_by_text("图形为原图裁剪，请核对准确性", exact=True)).to_be_visible()
    page.get_by_role("button", name="显示解析", exact=True).click()
    expect(page.get_by_text("【演示模式】", exact=False)).to_be_visible()
    expect(page.get_by_role("heading", name="题目", exact=True)).to_have_count(0)
    expect(page.get_by_role("heading", name="解析", exact=True)).to_have_count(0)
    expect(page.get_by_text("CONTROLLED_TIKZ_SOURCE_NEVER_RENDER", exact=False)).to_have_count(0)


def _skip_to_review_question(page, marker: str, failure_message: str) -> None:
    expect(page.get_by_role("heading", name="今日复习", exact=True)).to_be_visible()
    for _ in range(30):
        if page.get_by_text(marker, exact=False).count():
            return
        progress = page.locator("header p")
        previous = progress.inner_text()
        page.get_by_role("button", name="先跳过", exact=False).click()
        expect(progress).not_to_have_text(previous)
    pytest.fail(failure_message)


def _start_spec_only_import(page, base_url: str) -> None:
    _open_batch(page, base_url)
    page.locator('input[type="file"]').set_input_files(
        _photo("figure-spec-only.png", b"figure-spec-only")
    )
    page.get_by_role("button", name="开始识别", exact=False).click()
    expect(page.get_by_role("heading", name="确认识别结果", exact=True)).to_be_visible(timeout=30000)


@_screenshot_on_failure
def test_spec_only_batch_fallback_shows_original_page_and_accuracy_warning(
    page, mobile_base_url
):
    """无有效图路径时必须显示原页和兜底准确性提示。"""
    _start_spec_only_import(page, mobile_base_url)

    expect(page.get_by_role("img", name="第 1 页原图", exact=True)).to_be_visible()
    expect(page.get_by_text("图形为原图裁剪，请核对准确性", exact=True)).to_be_visible()
    expect(page.get_by_text("CONTROLLED_TIKZ_SOURCE_NEVER_RENDER", exact=False)).to_have_count(0)


@_screenshot_on_failure
def test_spec_only_question_uses_original_image_in_notebook_and_review(
    page, mobile_base_url
):
    """只有图形 spec 时，详情和复习都必须显示完整原图。"""
    _start_spec_only_import(page, mobile_base_url)
    page.get_by_role("button", name="确认入库（1 题）", exact=True).click()
    expect(page.get_by_text("已入库 1 道题", exact=False)).to_be_visible(timeout=30000)
    page.get_by_role("button", name="去错题本看看", exact=True).click()

    page.get_by_text("【无有效图】", exact=False).first.click()
    dialog = page.get_by_role("dialog")
    expect(dialog.get_by_role("img", name="错题原图", exact=True)).to_be_visible()
    expect(dialog.get_by_text("图形为原图裁剪，请核对准确性", exact=True)).to_be_visible()
    expect(dialog.get_by_role("img", name="题图 1", exact=True)).to_have_count(0)
    page.keyboard.press("Escape")

    page.get_by_role("button", name="复习", exact=True).click()
    _skip_to_review_question(
        page,
        "【无有效图】",
        "Spec-only figure question did not appear in the existing review queue",
    )

    expect(page.get_by_role("img", name="错题原图", exact=True)).to_be_visible()
    expect(page.get_by_text("图形为原图裁剪，请核对准确性", exact=True)).to_be_visible()
    expect(page.get_by_role("img", name="题图 1", exact=True)).to_have_count(0)


@_screenshot_on_failure
def test_match_statuses_require_explicit_similar_choices_and_keep_private_images(
    page, mobile_base_url
):
    """相似题自动接受、漏传选择或复用结果不显示双图时，本场景失败。"""
    _open_capture(page, mobile_base_url)
    page.locator('input[type="file"]').set_input_files(
        _photo("match-statuses.png", b"match-statuses")
    )
    page.get_by_role("button", name="开始解析", exact=True).click()

    expect(page.get_by_role("heading", name="确认识别结果", exact=True)).to_be_visible(timeout=30000)
    expect(page.get_by_text("精确复用", exact=True)).to_have_count(1)
    expect(page.get_by_text("相似待确认", exact=True)).to_have_count(2)
    expect(page.get_by_text("新题解析", exact=True)).to_have_count(1)
    confirm = page.get_by_role("button", name="确认录入", exact=True)
    expect(confirm).to_be_disabled()
    reuse_card = page.locator('[data-slot="card"]').filter(has_text="【受控相似引用】")
    new_card = page.locator('[data-slot="card"]').filter(has_text="【受控相似新题】")
    expect(reuse_card.get_by_text("【受控共享候选】", exact=False)).to_be_visible()
    expect(reuse_card.get_by_text("受控标准解析摘要", exact=False)).to_be_visible()
    expect(reuse_card.get_by_text("教师发布", exact=False)).to_be_visible()
    reuse_card.get_by_role("button", name="引用此题", exact=True).click()
    new_card.get_by_role("button", name="作为新题解析", exact=True).click()
    expect(reuse_card.get_by_role("button", name="引用此题", exact=True)).to_have_attribute(
        "aria-pressed", "true"
    )
    expect(confirm).to_be_enabled()

    page.set_viewport_size({"width": 320, "height": 700})
    expect(reuse_card.get_by_role("button", name="引用此题", exact=True)).to_have_attribute(
        "aria-pressed", "true"
    )
    page.reload()
    page.get_by_role("button", name="录题", exact=True).click()
    expect(page.get_by_role("heading", name="确认识别结果", exact=True)).to_be_visible(timeout=30000)
    expect(page.get_by_role("button", name="确认录入", exact=True)).to_be_disabled()
    reuse_card = page.locator('[data-slot="card"]').filter(has_text="【受控相似引用】")
    new_card = page.locator('[data-slot="card"]').filter(has_text="【受控相似新题】")
    expect(reuse_card.get_by_role("button", name="引用此题", exact=True)).to_have_attribute(
        "aria-pressed", "false"
    )
    reuse_card.get_by_role("button", name="引用此题", exact=True).click()
    new_card.get_by_role("button", name="作为新题解析", exact=True).click()
    page.get_by_role("button", name="确认录入", exact=True).click()

    expect(page.get_by_role("heading", name="录入完成", exact=True)).to_be_visible(timeout=30000)
    expect(page.get_by_text("已入库 4 道", exact=False)).to_be_visible()
    expect(page.get_by_role("img", name="本次私有原图", exact=False)).to_have_count(4)
    expect(page.get_by_role("img", name="共享 AI 重绘图", exact=False)).to_have_count(2)
    expect(page.get_by_role("img", name="教师原图", exact=False)).to_have_count(0)
    _assert_no_extra_entry_or_editing_controls(page)
    expect(page.get_by_text("题库管理", exact=True)).to_have_count(0)


@_screenshot_on_failure
def test_batch_match_statuses_submit_segment_index_choice(page, mobile_base_url):
    """整卷校对若遗漏服务端状态或错误使用候选 ID 作为键，本场景失败。"""
    _open_batch(page, mobile_base_url)
    page.locator('input[type="file"]').set_input_files(
        _photo("batch-match.png", b"batch-match")
    )
    page.get_by_role("button", name="开始识别", exact=False).click()

    expect(page.get_by_role("heading", name="确认识别结果", exact=True)).to_be_visible(timeout=30000)
    expect(page.get_by_text("精确复用", exact=True)).to_have_count(1)
    expect(page.get_by_text("相似待确认", exact=True)).to_have_count(1)
    expect(page.get_by_text("新题解析", exact=True)).to_have_count(1)
    confirm = page.get_by_role("button", name="确认入库（3 题）", exact=True)
    expect(confirm).to_be_disabled()
    similar = page.locator('[data-slot="card"]').filter(has_text="【受控相似引用】")
    similar.get_by_role("button", name="作为新题解析", exact=True).click()
    expect(confirm).to_be_enabled()
    confirm.click()
    expect(page.get_by_text("已入库 3 道题", exact=False)).to_be_visible(timeout=30000)


def _register_student(page, api_base: str, username: str) -> str:
    response = page.request.post(
        f"{api_base}/auth/register",
        data={"username": username, "password": "controlled-pass", "role": "student"},
    )
    assert response.ok, response.text()
    return response.json()["access_token"]


def _create_exact_reference(page, api_base: str, token: str) -> dict:
    response = page.request.post(
        f"{api_base}/photo-entry/jobs",
        headers={"Authorization": f"Bearer {token}"},
        multipart={"images": _photo("ab.png", b"ab-exact")},
    )
    assert response.status == 202, response.text()
    job_id = response.json()["job_id"]
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        current = page.request.get(
            f"{api_base}/photo-entry/jobs/{job_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert current.ok, current.text()
        job = current.json()
        if job["phase"] == "done":
            assert job["entries"][0]["error"] is None
            return job["entries"][0]["question"]
        assert job["phase"] != "failed", job["error"]
        time.sleep(0.05)
    raise TimeoutError("controlled exact reference did not finish")


def test_ab_sequential_delete_cleans_only_private_then_student_orphan(
    page, mobile_server
):
    """A 删除不能动 B 的原图/共享图；B 最后删除才清理学生来源标准题。"""
    import sqlite3

    database_path = mobile_server["database_path"]
    if database_path is None:
        pytest.skip("外部 Mobile E2E 服务不提供隔离数据库路径")
    api_base = mobile_server["base_url"].removesuffix("/m/") + "/api"
    token_a = _register_student(page, api_base, "controlled-student-a")
    token_b = _register_student(page, api_base, "controlled-student-b")
    question_a = _create_exact_reference(page, api_base, token_a)
    question_b = _create_exact_reference(page, api_base, token_b)
    canonical_id = question_a["canonical_id"]
    assert question_b["canonical_id"] == canonical_id
    vector_log = database_path.parent / "bank-vector-deletions.log"

    def vector_deletions() -> list[str]:
        return vector_log.read_text(encoding="utf-8").splitlines() if vector_log.exists() else []

    vectors_before = vector_deletions()
    with sqlite3.connect(database_path) as connection:
        private_a = connection.execute(
            "SELECT private_image_path FROM questions WHERE id = ?", (question_a["id"],)
        ).fetchone()[0]
        private_b = connection.execute(
            "SELECT private_image_path FROM questions WHERE id = ?", (question_b["id"],)
        ).fetchone()[0]
        shared = connection.execute(
            "SELECT storage_path FROM question_assets WHERE canonical_id = ? AND visibility = 'shared'",
            (canonical_id,),
        ).fetchone()[0]

    deleted = page.request.delete(
        f"{api_base}/questions/{question_a['id']}",
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert deleted.status == 204, deleted.text()
    assert not Path(private_a).exists()
    assert Path(private_b).exists() and Path(shared).exists()
    assert vector_deletions() == vectors_before
    image_a = page.request.get(
        f"{api_base}/questions/{question_a['id']}/image",
        headers={"Authorization": f"Bearer {token_a}"},
    )
    shared_b = page.request.get(
        f"{api_base}/questions/{question_b['id']}/figures/0",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert image_a.status == 404
    assert shared_b.status == 200
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT 1 FROM canonical_questions WHERE id = ?", (canonical_id,)
        ).fetchone() == (1,)

    deleted = page.request.delete(
        f"{api_base}/questions/{question_b['id']}",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert deleted.status == 204, deleted.text()
    assert not Path(private_b).exists() and not Path(shared).exists()
    assert vector_deletions() == [*vectors_before, str(canonical_id)]
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT 1 FROM canonical_questions WHERE id = ?", (canonical_id,)
        ).fetchone() is None
        assert connection.execute(
            "SELECT COUNT(*) FROM question_bank_audit WHERE canonical_id = ? AND action = 'student_orphan_cleanup'",
            (canonical_id,),
        ).fetchone() == (1,)


@_screenshot_on_failure
def test_docx_evidence_has_authenticated_download_in_notebook_and_review(
    page, mobile_base_url, tmp_path
):
    """DOCX 原证据若仍走图片渲染或无鉴权下载，本场景失败。"""
    from doc_samples import make_docx

    marker = "【DOCX 私有证据】"
    document = make_docx(
        tmp_path / "mobile-evidence.docx",
        [f"1. {marker}下载原始文档并保留内容。"],
        with_image=False,
    )
    _open_batch(page, mobile_base_url)
    page.locator('input[type="file"]').set_input_files(document)
    page.get_by_role("button", name="开始识别", exact=False).click()
    expect(page.get_by_role("heading", name="确认识别结果", exact=True)).to_be_visible(timeout=30000)
    page.get_by_role("button", name="确认入库（1 题）", exact=True).click()
    expect(page.get_by_text("已入库 1 道题", exact=False)).to_be_visible(timeout=30000)
    page.get_by_role("button", name="去错题本看看", exact=True).click()

    page.get_by_text(marker, exact=False).first.click()
    dialog = page.get_by_role("dialog")
    expect(dialog.get_by_text("原始文档", exact=True)).to_be_visible()
    expect(dialog.get_by_text("DOCX", exact=True)).to_be_visible()
    expect(dialog.get_by_role("img", name="错题原图", exact=True)).to_have_count(0)
    with page.expect_download() as notebook_download:
        dialog.get_by_role("button", name="下载原始文档", exact=True).click()
    assert notebook_download.value.suggested_filename.endswith(".docx")
    page.keyboard.press("Escape")

    page.get_by_role("button", name="复习", exact=True).click()
    _skip_to_review_question(page, marker, "DOCX question did not appear in review")
    expect(page.get_by_text("原始文档", exact=True)).to_be_visible()
    expect(page.get_by_role("img", name="错题原图", exact=True)).to_have_count(0)
    with page.expect_download() as review_download:
        page.get_by_role("button", name="下载原始文档", exact=True).click()
    assert review_download.value.suggested_filename.endswith(".docx")


@_screenshot_on_failure
def test_docx_with_figure_spec_keeps_notebook_document_download(page, mobile_base_url):
    """有图 DOCX 若隐藏文档入口或把 DOCX 送进 AuthImage，本场景失败。"""
    _open_capture(page, mobile_base_url)
    page.get_by_role("button", name="错题本", exact=True).click()
    page.get_by_placeholder("搜索错题…").fill("【受控 DOCX 带图】")
    expect(page.get_by_text("【受控 DOCX 带图】", exact=False).first).to_be_visible()
    page.get_by_text("【受控 DOCX 带图】", exact=False).first.click()

    dialog = page.get_by_role("dialog")
    expect(dialog.get_by_text("原始文档", exact=True)).to_be_visible()
    expect(dialog.get_by_text("DOCX", exact=True)).to_be_visible()
    expect(dialog.get_by_text("图形为原图裁剪，请核对准确性", exact=True)).to_be_visible()
    expect(dialog.get_by_role("img", name="错题原图", exact=True)).to_have_count(0)
    with page.expect_download() as download:
        dialog.get_by_role("button", name="下载原始文档", exact=True).click()
    assert download.value.suggested_filename.endswith(".docx")
