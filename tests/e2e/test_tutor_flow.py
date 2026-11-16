"""E2E：AI 录题全流程与追问对话（mock 提供商，无需真实 API Key）。"""
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
from pathlib import Path

import pytest
from playwright.sync_api import expect

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(os.getenv("MM_E2E", "") != "1", reason="需要 MM_E2E=1 与运行中的应用"),
]

BASE_URL = os.getenv("MM_E2E_BASE_URL", "http://localhost:8601")
_CONTROLLED_WEB = """
from backend.database import SessionLocal, init_db
from backend.models.orm import QuestionAsset
from backend.models.schemas import CanonicalQuestionOut, QuestionBankCandidate
from backend.repositories.question_bank import QuestionBankRepository
from backend.services.ai.mock import MockProvider
from backend.services.question_bank import QuestionBankService

EXACT = "【Web 受控精确题】直接复用标准解析。"
SIMILAR_REUSE = "【Web 受控相似引用】确认后引用候选。"
SIMILAR_NEW = "【Web 受控相似新题】明确作为新题解析。"
NEW = "【Web 受控新题】没有匹配，执行新题解析。"
CANDIDATE = "【Web 受控共享候选】候选标准题干。"
original_segment = MockProvider.segment_page
original_similar = QuestionBankService.find_similar


def segment_page(self, image_bytes, mime_type="image/png"):
    if b"web-match-statuses" not in image_bytes:
        return original_segment(self, image_bytes, mime_type)
    return [
        {"number": "1", "text": EXACT, "likely_wrong": True},
        {"number": "2", "text": SIMILAR_REUSE, "likely_wrong": True},
        {"number": "3", "text": SIMILAR_NEW, "likely_wrong": True},
        {"number": "4", "text": NEW, "likely_wrong": True},
    ]


def find_similar(self, stem, *, context, limit=3):
    if stem not in {SIMILAR_REUSE, SIMILAR_NEW}:
        return original_similar(self, stem, context=context, limit=limit)
    with SessionLocal() as session:
        question = QuestionBankRepository(session).find_by_stem(CANDIDATE)
        return [QuestionBankCandidate(
            score=0.87,
            question=CanonicalQuestionOut.from_orm_model(question),
        )]


MockProvider.segment_page = segment_page
QuestionBankService.find_similar = find_similar
init_db(seed_users=True)
with SessionLocal() as session:
    repository = QuestionBankRepository(session)
    for stem in (EXACT, CANDIDATE):
        if repository.find_by_stem(stem) is not None:
            continue
        question = repository.create(
            stem_markdown=stem,
            analysis_markdown="Web 受控标准解析摘要。",
            answer="Web 受控答案",
            origin="teacher",
        )
        session.flush()
        shared = __import__("pathlib").Path(__import__("os").environ["DATA_DIR"]) / f"web-{question.id}.png"
        shared.write_bytes(__import__("base64").b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        ))
        session.add(QuestionAsset(
            canonical_id=question.id,
            kind="ai_redraw",
            visibility="shared",
            storage_path=str(shared),
            is_preferred=True,
        ))
    session.commit()

import app

app.main()
"""


@pytest.fixture(scope="module")
def controlled_web_url():
    root = Path(__file__).resolve().parents[2]
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix="mathmaster_web_match_e2e_") as data_dir:
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


def _login(page, username: str, password: str, base_url: str = BASE_URL) -> None:
    page.goto(base_url)
    page.wait_for_selector("input", timeout=30000)
    inputs = page.locator('input[type="text"], input[type="password"]')
    inputs.nth(0).fill(username)
    inputs.nth(1).fill(password)
    page.get_by_role("button", name="登录", exact=True).first.click(force=True)
    page.wait_for_selector("text=累计错题", timeout=30000)


def _goto(page, label: str, marker: str | None = None, timeout: int = 30000) -> None:
    """点击侧边栏 Material 图标按钮，并等待目标页标志出现。"""
    page.wait_for_timeout(1200)
    page.locator("section[data-testid='stSidebar'] button", has_text=label).first.click()
    if marker:
        deadline = time.monotonic() + timeout / 1000
        while time.monotonic() < deadline:
            if page.locator(f"text={marker}").first.is_visible():
                return
            page.wait_for_timeout(500)
        page.screenshot(path=f"e2e_failure_{label}.png")
        raise AssertionError(f"{label} 页面未出现标志：{marker}")


@pytest.fixture(scope="module")
def _sample_jpeg(tmp_path_factory):
    from PIL import Image

    image = Image.new("RGB", (40, 30), (170, 200, 240))
    path = tmp_path_factory.mktemp("upload") / "sample.jpg"
    image.save(path, format="JPEG")
    return str(path)


def test_full_tutor_flow(page, _sample_jpeg):
    """上传 → AI 解析（mock）→ 归档 → 错题本可见。"""
    _login(page, "demo", "demo123")
    _goto(page, "AI 录题", "拍照录题")

    file_input = page.locator('input[type="file"]').first
    file_input.set_input_files(_sample_jpeg)
    page.get_by_role("button", name="开始 AI 解析").first.click()

    # 切题候选确认（mock 固定切出多题）→ 确认录入后逐题解构归档
    page.wait_for_selector("text=从图中识别出", timeout=30000)
    page.get_by_role("button", name="确认录入").first.click()
    # mock 提供商固定返回含这些字段的解析
    page.wait_for_selector("text=已自动归档入错题本", timeout=30000)
    page.wait_for_selector("text=去错题本查看", timeout=15000)

    _goto(page, "错题本", "语义搜索")
    # 折叠面板采用懒渲染，展开后才会加载 mock 解析内容
    page.locator("summary", has_text="⏰").first.click()
    page.wait_for_selector("text=演示模式", timeout=15000, state="visible")


def test_followup_chat_on_question(page, _sample_jpeg):
    """先确保有一道题（录题流程），再在错题本里发一条追问。"""
    _login(page, "demo", "demo123")
    _goto(page, "AI 录题", "拍照录题")
    file_input = page.locator('input[type="file"]').first
    file_input.set_input_files(_sample_jpeg)
    page.get_by_role("button", name="开始 AI 解析").first.click()
    page.wait_for_function(
        """() => document.body.innerText.includes('从图中识别出')
          || document.body.innerText.includes('已自动归档入错题本')""",
        timeout=30000,
    )
    if page.locator("text=从图中识别出").first.is_visible():
        page.get_by_role("button", name="确认录入").first.click()
    page.wait_for_selector("text=已自动归档入错题本", timeout=30000)

    _goto(page, "错题本", "语义搜索")
    page.wait_for_timeout(1500)
    # 展开第一题并切到追问讲题（Streamlit DOM 上用 JS 触发最可靠）
    page.evaluate(
        """(() => {
          const s = [...document.querySelectorAll('summary')].find(x => x.innerText.includes('⏰'));
          if (s) s.click();
        })()"""
    )
    page.wait_for_timeout(3000)
    page.evaluate(
        """(() => {
          const t = [...document.querySelectorAll('button[data-baseweb="tab"]')]
            .find(x => x.innerText.includes('追问讲题'));
          if (t) t.click();
        })()"""
    )
    page.wait_for_timeout(2500)

    page.evaluate(
        """(() => {
          const ta = document.querySelector('div[data-testid="stChatInput"] textarea');
          if (!ta) return;
          const setter = Object.getOwnPropertyDescriptor(
            window.HTMLTextAreaElement.prototype, 'value').set;
          setter.call(ta, '为什么判别式要大于等于零？');
          ta.dispatchEvent(new Event('input', { bubbles: true }));
          ta.dispatchEvent(new KeyboardEvent('keydown', {
            key: 'Enter', code: 'Enter', keyCode: 13, bubbles: true }));
        })()"""
    )
    # 发送后 st.rerun 会重新折叠面板 → 回复在 DOM 中但隐藏，attached 即可
    page.wait_for_selector("text=已收到你的追问", timeout=30000, state="attached")


def test_controlled_match_decisions_and_reference_images(
    page, controlled_web_url, tmp_path
):
    """Web 不得自动接受相似题；两种明确选择与双图展示必须在真实浏览器生效。"""
    _login(page, "demo", "demo123", controlled_web_url)
    _goto(page, "AI 录题", "拍照录题")
    upload = tmp_path / "web-match.png"
    upload.write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
        + b"web-match-statuses"
    )
    page.locator('input[type="file"]').first.set_input_files(upload)
    page.get_by_role("button", name="开始 AI 解析").click()

    page.wait_for_selector("text=从图中识别出 4 道题", timeout=30000)
    expect(page.get_by_text("精确复用", exact=True)).to_have_count(1)
    expect(page.get_by_text("相似待确认", exact=True)).to_have_count(2)
    expect(page.get_by_text("新题解析", exact=True)).to_have_count(1)
    confirm = page.get_by_role("button", name="确认录入（4 题）", exact=True)
    expect(confirm).to_be_disabled()
    selectors = page.locator('[data-testid="stSelectbox"]').filter(
        has_text="相似题处理（必须明确选择）"
    )
    expect(selectors).to_have_count(2)
    reuse_select = selectors.nth(0).get_by_role("combobox")
    reuse_select.fill("引用此题")
    reuse_select.press("ArrowDown")
    reuse_select.press("Enter")
    expect(confirm).to_be_disabled()
    new_select = selectors.nth(1).get_by_role("combobox")
    new_select.fill("作为新题解析")
    new_select.press("ArrowDown")
    new_select.press("Enter")
    expect(confirm).to_be_enabled()
    confirm.click()

    page.wait_for_selector("text=已自动归档入错题本", timeout=30000)
    expect(page.get_by_role("img", name="本次私有原图", exact=False)).to_have_count(1)
    expect(page.get_by_role("img", name="共享 AI 重绘图", exact=False)).to_have_count(1)
    page.locator("summary", has_text="web-match.png · 第 2 题").click()
    expect(page.get_by_role("img", name="本次私有原图", exact=False)).to_have_count(2)
    expect(page.get_by_role("img", name="共享 AI 重绘图", exact=False)).to_have_count(2)
