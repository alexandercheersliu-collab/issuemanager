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
import time

from backend.models.schemas import QuestionOut
from backend.services.ai.mock import MockProvider
from backend.services.geometry_figures import FigureOutcome, GeometryFigureService
from backend.services.photo_entry import PhotoEntryService
from backend.services.question_mixins import SimilarQuestionsOutcome
from backend.services.question_service import QuestionService

_original_segment_page = MockProvider.segment_page
_original_analyze_text = MockProvider.analyze_text
_original_redraw_segment = GeometryFigureService.redraw_segment
_original_similar_questions = QuestionService.similar_questions
_original_confirm = PhotoEntryService.confirm
_reanalyzed = set()
_reanalyze_attempts = {}


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


def _confirm_to_completion(self, job_id, user_id, candidate_ids):
    _original_confirm(self, job_id, user_id, candidate_ids)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        job = self.get_job(job_id, user_id)
        if job is not None and job["phase"] in {"done", "failed"}:
            return
        time.sleep(0.01)
    raise RuntimeError("controlled photo confirmation did not finish")


MockProvider.segment_page = _controlled_segment_page
MockProvider.analyze_text = _controlled_analyze_text
GeometryFigureService.redraw_segment = _controlled_redraw_segment
QuestionService.similar_questions = _controlled_similar_questions
PhotoEntryService.confirm = _confirm_to_completion

import uvicorn

uvicorn.run("api.main:app", host="127.0.0.1", port=int(__import__("os").environ["PORT"]))
"""


def _photo(name: str, marker: bytes) -> dict:
    return {"name": name, "mimeType": "image/png", "buffer": PNG_BYTES + marker}


@pytest.fixture(scope="module")
def mobile_base_url():
    external = os.getenv("MM_MOBILE_E2E_BASE_URL")
    if external:
        yield external
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
                yield f"http://127.0.0.1:{port}/m/"
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


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
