"""E2E：教师标准题库搜索、编辑、未引用删除与学生导航隔离。"""
from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from playwright.sync_api import expect

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(os.getenv("MM_E2E", "") != "1", reason="需要 MM_E2E=1 与 Playwright"),
]

_CONTROLLED_WEB = r"""
import base64
import os
from pathlib import Path

from backend.database import SessionLocal, init_db
from backend.models.orm import Question, QuestionAsset, User
from backend.repositories.question_bank import QuestionBankRepository
from backend.services.rag import QuestionVectorStore, RagHit, SimilarResult
from backend.utils.security import hash_password

EDIT_TARGET = "Web 受控题库编辑目标"
REFERENCED = "Web 受控题库已引用题"
MATH_TARGET = r"已知函数 $f(x)=x^2$，求 $f(2)$。"

init_db(seed_users=True)
with SessionLocal() as session:
    repository = QuestionBankRepository(session)
    if repository.find_by_stem(MATH_TARGET) is None:
        repository.create(
            stem_markdown=MATH_TARGET,
            analysis_markdown="代入 $x=2$ 计算。",
            answer="$4$",
            question_type="解答题",
            origin="teacher",
        )
    if session.query(User).filter_by(username="teacher").one_or_none() is None:
        session.add(User(
            username="teacher",
            password_hash=hash_password("teacher123", 4),
            role="teacher",
        ))
    if repository.find_by_stem("Web 分页 00") is None:
        target = repository.create(
            stem_markdown=EDIT_TARGET,
            analysis_markdown="受控标准解析",
            answer="受控旧答案",
            knowledge_points=["方程"],
            question_type="解答题",
            subject="math",
            grade=9,
            textbook_version="人教版",
            origin="teacher",
        )
        image = Path(os.environ["DATA_DIR"]) / "question-bank" / "web-teacher-original.png"
        image.parent.mkdir(parents=True, exist_ok=True)
        image.write_bytes(base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        ))
        session.add(QuestionAsset(
            canonical_id=target.id,
            kind="teacher_original",
            visibility="teacher_only",
            storage_path=str(image),
            mime_type="image/png",
        ))
        for index in range(21):
            repository.create(
                stem_markdown=f"Web 分页 {index:02d}",
                analysis_markdown="分页解析",
                answer="分页答案",
                question_type="解答题",
                subject="math",
                grade=9,
                textbook_version="人教版",
                origin="teacher",
            )
    if repository.find_by_stem(REFERENCED) is None:
        referenced = repository.create(
            stem_markdown=REFERENCED,
            analysis_markdown="仍被引用的解析",
            answer="引用答案",
            question_type="解答题",
            origin="teacher",
        )
        student = session.query(User).filter_by(username="demo").one()
        student_image = (
            Path(os.environ["DATA_DIR"])
            / "images"
            / f"u{student.id}"
            / "controlled-student-original.png"
        )
        student_image.parent.mkdir(parents=True, exist_ok=True)
        student_image.write_bytes(base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        ))
        session.add(Question(
            user_id=student.id,
            canonical_id=referenced.id,
            private_image_path=str(student_image),
            content_markdown="",
            answer="",
            user_note="Web 删除后仍保留的个人备注",
        ))
        cleanup_image = (
            Path(os.environ["DATA_DIR"])
            / "question-bank"
            / "web-cleanup-failure.png"
        )
        cleanup_image.parent.mkdir(parents=True, exist_ok=True)
        cleanup_image.write_bytes(base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        ))
        session.add(QuestionAsset(
            canonical_id=referenced.id,
            kind="ai_redraw",
            visibility="shared",
            storage_path=str(cleanup_image),
        ))
    session.commit()


class FailingCollection:
    def upsert(self, **kwargs):
        raise RuntimeError("controlled vector upsert failure")

    def delete(self, **kwargs):
        return None


def controlled_similar(self, query_text, *, constraints=None, top_k=3):
    if query_text not in {EDIT_TARGET, "Web 语义独立查询"}:
        return SimilarResult(hits=[])
    with SessionLocal() as session:
        question = QuestionBankRepository(session).find_by_stem(EDIT_TARGET)
        if question is None:
            return SimilarResult(hits=[])
        return SimilarResult(hits=[RagHit(
            question_id=-1, distance=0.0, source="bank", bank_id=str(question.id)
        )])


QuestionVectorStore._ensure_bank_collection = lambda self: FailingCollection()
QuestionVectorStore.similar_canonical_questions = controlled_similar

if not hasattr(Path, "_controlled_original_unlink"):
    Path._controlled_original_unlink = Path.unlink

def controlled_unlink(path, *args, **kwargs):
    if path.name == "web-cleanup-failure.png":
        raise OSError("controlled Web asset cleanup failure")
    return Path._controlled_original_unlink(path, *args, **kwargs)

Path.unlink = controlled_unlink

import app

app.main()
"""


@pytest.fixture(scope="module")
def controlled_web_url():
    root = Path(__file__).resolve().parents[2]
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix="mathmaster_question_bank_e2e_") as data_dir:
        script = Path(data_dir) / "controlled_app.py"
        script.write_text(_CONTROLLED_WEB, encoding="utf-8")
        env = os.environ.copy()
        env.update(
            {
                "DATA_DIR": data_dir,
                "DATABASE_URL": f"sqlite:///{Path(data_dir) / 'math_tutor.db'}",
                "CHROMA_DIR": str(Path(data_dir) / "chroma"),
                "CHROMA_MODEL_DIR": str(Path(data_dir) / "models"),
                "AI_PROVIDER": "mock",
                "RAG_ENABLED": "false",
                "API_RATE_LIMIT_ENABLED": "false",
                "BCRYPT_ROUNDS": "4",
                "PYTHONPATH": str(root),
            }
        )
        log_path = Path(data_dir) / "streamlit.log"
        with log_path.open("w", encoding="utf-8") as log:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "streamlit",
                    "run",
                    str(script),
                    "--server.port",
                    str(port),
                    "--server.headless",
                    "true",
                ],
                cwd=root,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            try:
                base_url = f"http://127.0.0.1:{port}"
                deadline = time.monotonic() + 45
                while True:
                    if process.poll() is not None:
                        raise RuntimeError(
                            f"Controlled Web exited early:\n{log_path.read_text()}"
                        )
                    try:
                        with urllib.request.urlopen(
                            f"{base_url}/_stcore/health", timeout=0.5
                        ) as response:
                            if response.status == 200:
                                break
                    except (urllib.error.URLError, TimeoutError):
                        pass
                    if time.monotonic() >= deadline:
                        raise RuntimeError(
                            f"Controlled Web health check timed out:\n{log_path.read_text()}"
                        )
                    time.sleep(0.1)
                yield base_url
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def _login(page, username: str, password: str, base_url: str) -> None:
    page.goto(base_url)
    page.wait_for_selector("input", timeout=30000)
    inputs = page.locator('input[type="text"], input[type="password"]')
    inputs.nth(0).fill(username)
    inputs.nth(1).fill(password)
    page.get_by_role("button", name="登录", exact=True).first.click(force=True)
    page.wait_for_selector("text=累计错题", timeout=30000)


def _goto_question_bank(page, base_url: str, username: str="admin", password: str="admin123") -> None:
    _login(page, username, password, base_url)
    page.wait_for_timeout(1000)
    page.locator("section[data-testid='stSidebar'] button", has_text="标准题库").first.click()
    expect(page.get_by_text("搜索并维护全系统共享标准题", exact=False)).to_be_visible(timeout=30000)


def _search(page, keyword: str) -> None:
    page.get_by_label("关键词", exact=True).fill(keyword)
    page.get_by_role("button", name="搜索标准题", exact=True).first.click()


def _select_option(page, label: str, option: str) -> None:
    page.get_by_role("combobox", name=label, exact=True).click()
    page.get_by_role("option", name=option, exact=True).click()


def test_logged_out_and_student_users_have_no_question_bank_navigation(page, controlled_web_url):
    page.goto(controlled_web_url)
    page.wait_for_selector("input", timeout=30000)
    assert page.get_by_role("button", name="标准题库", exact=True).count() == 0

    _login(page, "demo", "demo123", controlled_web_url)
    page.wait_for_timeout(1000)
    assert page.get_by_role("button", name="标准题库", exact=True).count() == 0


def test_results_render_math_and_detail_selector_hides_latex_source(page, controlled_web_url):
    _goto_question_bank(page, controlled_web_url, "teacher", "teacher123")
    _search(page, "已知函数")

    expect(page.locator('[data-testid="stTable"] .katex').first).to_be_visible()
    detail_selector = page.get_by_role("combobox", name="选择标准题查看详情", exact=True)
    expect(detail_selector).to_have_value(re.compile(r"^第 \d+ 题$"))


def test_teacher_searches_edits_and_deletes_unreferenced_question(page, controlled_web_url):
    _goto_question_bank(page, controlled_web_url, "teacher", "teacher123")
    expect(page.get_by_role("button", name="后台管理", exact=True)).to_have_count(0)

    _search(page, "Web 语义独立查询")
    expect(page.get_by_text("没有符合条件的标准题。", exact=True)).to_be_visible()
    page.get_by_role("checkbox", name="使用语义搜索").check(force=True)
    _search(page, "Web 语义独立查询")
    expect(
        page.locator("p").filter(has_text="Web 受控题库编辑目标").last
    ).to_be_visible()

    page.get_by_role("checkbox", name="使用语义搜索").uncheck(force=True)
    _select_option(page, "学科筛选", "数学")
    page.get_by_label("年级筛选（可留空）", exact=True).fill("9")
    page.get_by_label("题型筛选（可留空）", exact=True).fill("解答题")
    page.get_by_label("教材版本筛选（可留空）", exact=True).fill("人教版")
    _select_option(page, "来源筛选", "教师")
    page.get_by_label("引用数筛选（精确，可留空）", exact=True).fill("0")
    _search(page, "Web 受控题库编辑目标")

    table = page.get_by_role(
        "table", name="标准题搜索结果，包含题干、题型、来源、更新时间和引用数"
    )
    expect(table).to_be_visible()
    cells = table.locator("tbody tr").first.locator("td")
    for column, value in ((1, "解答题"), (2, "教师"), (4, "0")):
        expect(cells.nth(column)).to_have_text(value)
    expect(cells.nth(3)).to_have_text(re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}"))

    for label, wrong, restored in (
        ("年级筛选（可留空）", "8", "9"),
        ("题型筛选（可留空）", "选择题", "解答题"),
        ("教材版本筛选（可留空）", "北师大版", "人教版"),
        ("引用数筛选（精确，可留空）", "1", "0"),
    ):
        page.get_by_label(label, exact=True).fill(wrong)
        _search(page, "Web 受控题库编辑目标")
        expect(page.get_by_text("没有符合条件的标准题。", exact=True)).to_be_visible()
        page.get_by_label(label, exact=True).fill(restored)

    for label, wrong, restored in (
        ("学科筛选", "物理", "数学"),
        ("来源筛选", "学生", "教师"),
    ):
        _select_option(page, label, wrong)
        _search(page, "Web 受控题库编辑目标")
        expect(page.get_by_text("没有符合条件的标准题。", exact=True)).to_be_visible()
        _select_option(page, label, restored)

    _search(page, "Web 受控题库编辑目标")
    expect(
        page.locator("p").filter(has_text="Web 受控题库编辑目标").last
    ).to_be_visible()
    expect(
        page.get_by_role("img", name=re.compile(r"标准题 \d+ 的教师原始图片"))
    ).to_be_visible()
    stem_input = page.get_by_label("题干（Markdown）", exact=True)
    expect(stem_input).not_to_be_visible()
    page.locator("summary", has_text="编辑标准内容").click()
    expect(stem_input).to_be_visible()
    stem_input.fill("Web 受控题库已修订题干")
    page.get_by_label("答案", exact=True).fill("Web 受控新答案")
    page.get_by_role("button", name="保存标准题", exact=True).click()
    expect(page.get_by_text("标准题已保存，但向量同步待重试", exact=False)).to_be_visible(
        timeout=30000
    )

    _search(page, "Web 受控题库已修订题干")
    expect(page.get_by_text("Web 受控新答案", exact=False)).to_be_visible()
    page.get_by_role("button", name="删除当前标准题", exact=True).first.click()
    page.get_by_role("button", name="确认删除", exact=True).click()
    expect(page.get_by_text("标准题已删除，资源和向量索引已同步清理", exact=False)).to_be_visible(
        timeout=30000
    )
    _search(page, "Web 受控题库已修订题干")
    expect(page.get_by_text("没有符合条件的标准题。", exact=True)).to_be_visible()

    _search(page, "Web 分页")
    expect(page.get_by_role("button", name="下一页", exact=True)).to_be_enabled()
    page.get_by_role("button", name="下一页", exact=True).click()
    expect(page.get_by_text("第 2 页", exact=True)).to_be_visible()
    expect(page.locator("p").filter(has_text="Web 分页 00").last).to_be_visible()


def test_referenced_delete_shows_409_for_ordinary_teacher(page, controlled_web_url):
    _goto_question_bank(page, controlled_web_url, "teacher", "teacher123")
    _search(page, "Web 受控题库已引用题")

    page.get_by_role("button", name="删除当前标准题", exact=True).first.click()
    page.get_by_role("button", name="确认删除", exact=True).click()
    expect(page.get_by_text("409", exact=False)).to_be_visible(timeout=30000)


def test_admin_previews_confirms_snapshot_delete_and_student_keeps_evidence(
    page, controlled_web_url
):
    _goto_question_bank(page, controlled_web_url)
    _search(page, "Web 受控题库已引用题")
    page.get_by_role("button", name="删除当前标准题", exact=True).first.click()

    dialog = page.get_by_role("dialog")
    expect(dialog.get_by_text("受影响学生：1 人 · 个人错题：1 道", exact=True)).to_be_visible()
    expect(dialog.get_by_text("demo", exact=False)).to_be_visible()
    expect(dialog.get_by_text("个人错题 ID", exact=False)).to_be_visible()
    canonical_id = re.search(r"ID (\d+) · 引用数", page.locator("main").inner_text()).group(1)
    confirmation = f"删除标准题 {canonical_id}"
    confirm_button = dialog.get_by_role("button", name="确认删除并保留个人快照", exact=True)
    expect(confirm_button).to_be_disabled()
    dialog.get_by_label("输入确认短语", exact=True).fill(confirmation)
    dialog.get_by_label("输入确认短语", exact=True).press("Tab")
    expect(confirm_button).to_be_enabled()
    confirm_button.click()
    expect(page.get_by_text("个人快照已保留", exact=False)).to_be_visible(timeout=30000)
    expect(page.get_by_text("资源文件清理待重试", exact=False)).to_be_visible()
    expect(page.get_by_text("资源和向量索引已同步清理", exact=False)).to_have_count(0)

    page.get_by_role("button", name="退出登录", exact=True).click()
    _login(page, "demo", "demo123", controlled_web_url)
    page.wait_for_timeout(1000)
    page.locator("section[data-testid='stSidebar'] button", has_text="错题本").first.click()
    page.get_by_label("搜索", exact=True).fill("Web 受控题库已引用题")
    page.get_by_label("搜索", exact=True).press("Enter")
    page.locator("summary", has_text="未分类").last.click()
    expect(
        page.get_by_role("paragraph").filter(
            has_text=re.compile(r"^Web 受控题库已引用题$")
        )
    ).to_be_visible()
    expect(page.get_by_text("仍被引用的解析", exact=True)).to_be_visible()
    expect(page.get_by_text("引用答案", exact=False)).to_be_visible()
    expect(
        page.locator("p").filter(has_text="我的笔记：Web 删除后仍保留的个人备注")
    ).to_be_visible()
    expect(page.get_by_role("img", name=re.compile(r"题 \d+ 的原图"))).to_be_visible()
