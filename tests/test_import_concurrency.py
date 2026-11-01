"""整卷导入有限并发：逐题解构的并发度、结果顺序、错误隔离与进度推进。

并发证明不打墙钟：替身 Provider 用线程安全计数器记录 analyze_text 的
实时并发峰值（进场 +1 / 出场 -1），多题导入时峰值 > 1 即证明并发生效。
"""
from __future__ import annotations

import threading
import time

import pytest
from doc_samples import make_docx

from backend.config import Settings
from backend.services.ai.mock import MockProvider
from backend.services.document_service import DocumentService


def _wait_job(service: DocumentService, job_id: str, user_id: int, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = service.get_job(job_id, user_id)
        assert job is not None
        if job["status"] in ("success", "failed"):
            return job
        time.sleep(0.05)
    raise TimeoutError(f"任务 {job_id} 在 {timeout}s 内未完成")


class _TrackingProvider(MockProvider):
    """记录 analyze_text 实时并发峰值的测试替身。

    delay_by_text：按题干文本决定睡眠时长（用于构造「后题先完成」的乱序完成场景）；
    fail_on：题干包含该子串时抛错（用于单题失败隔离场景）。
    """

    def __init__(self, *, delay: float = 0.05, fail_on: str = "", delay_by_text=None):
        super().__init__()
        self.delay = delay
        self.fail_on = fail_on
        self.delay_by_text = delay_by_text
        self._lock = threading.Lock()
        self._current = 0
        self.max_concurrent = 0

    def analyze_text(self, text, hint="", context=None):
        with self._lock:
            self._current += 1
            self.max_concurrent = max(self.max_concurrent, self._current)
        try:
            time.sleep(self.delay_by_text(text) if self.delay_by_text else self.delay)
            if self.fail_on and self.fail_on in text:
                raise RuntimeError("模拟单题解构失败")
            analysis = super().analyze_text(text, hint, context)
            # 把题干首行写进 answer，便于校验「结果与题目的对应关系」
            analysis.answer = f"答案:{text.splitlines()[0]}"
            return analysis
        finally:
            with self._lock:
                self._current -= 1


def _run_docx_import(
    service: DocumentService,
    provider: _TrackingProvider,
    user_id: int,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    tag: str,
    question_count: int = 4,
) -> dict:
    """用 DOCX（无页图，不触发视觉路）跑完整导入并等待完成。"""
    lines = [f"{i}. {tag}第{i}题 计算 {i}+{i}" for i in range(1, question_count + 1)]
    docx = make_docx(tmp_path / f"{tag}.docx", lines, with_image=False)
    monkeypatch.setattr(
        "backend.services.ai.get_ai_service", lambda settings=None: provider
    )
    job_id, dup = service.import_document(user_id, docx.read_bytes(), f"{tag}.docx")
    assert not dup
    return _wait_job(service, job_id, user_id)


def test_settings_import_concurrency_knob(monkeypatch):
    """配置项 import_concurrency：默认 3，可用 IMPORT_CONCURRENCY 环境变量覆盖。"""
    assert Settings().import_concurrency == 3
    monkeypatch.setenv("IMPORT_CONCURRENCY", "5")
    assert Settings().import_concurrency == 5


def test_analyze_runs_concurrently(question_service, student_user, tmp_path, monkeypatch):
    """多题导入并发解构：并发峰值 > 1，进度推满，结果按题号顺序且与题目一一对应。"""
    provider = _TrackingProvider()
    service = DocumentService(Settings(import_concurrency=3))
    job = _run_docx_import(service, provider, student_user.id, tmp_path, monkeypatch, "并发")

    assert job["status"] == "success", job["error"]
    assert provider.max_concurrent > 1, "逐题解构应并发执行"
    result = job["result"]
    assert result["done"] == result["total"] == 4
    assert [s["number"] for s in result["segments"]] == ["1", "2", "3", "4"]
    for entry in result["segments"]:
        assert entry["error"] is None
        assert entry["analysis"]["answer"] == f"答案:{entry['text'].splitlines()[0]}"


def test_analyze_error_isolation(question_service, student_user, tmp_path, monkeypatch):
    """单题解构失败：该题记 error + needs_review，其余题正常，整卷任务仍成功。"""
    provider = _TrackingProvider(fail_on="隔离第2题")
    service = DocumentService(Settings(import_concurrency=3))
    job = _run_docx_import(service, provider, student_user.id, tmp_path, monkeypatch, "隔离")

    assert job["status"] == "success", job["error"]
    result = job["result"]
    assert result["done"] == result["total"] == 4
    entries = result["segments"]
    failed = entries[1]
    assert failed["number"] == "2"
    assert "模拟单题解构失败" in failed["error"]
    assert failed["analysis"] is None
    assert failed["needs_review"] is True
    for entry in (entries[0], *entries[2:]):
        assert entry["error"] is None
        assert entry["analysis"] is not None


def test_analyze_order_preserved_when_later_questions_finish_first(
    question_service, student_user, tmp_path, monkeypatch
):
    """后题先完成（睡眠随题号递减）时，result["segments"] 仍保持原始题号顺序。"""

    def delay_by_text(text: str) -> float:
        number = int(text.split(".")[0])
        return (5 - number) * 0.03  # 题号越大睡得越短 → 完成顺序与题号相反

    provider = _TrackingProvider(delay_by_text=delay_by_text)
    service = DocumentService(Settings(import_concurrency=4))
    job = _run_docx_import(service, provider, student_user.id, tmp_path, monkeypatch, "顺序")

    assert job["status"] == "success", job["error"]
    result = job["result"]
    assert result["done"] == result["total"] == 4
    assert [s["number"] for s in result["segments"]] == ["1", "2", "3", "4"]
    for entry in result["segments"]:
        assert entry["analysis"]["answer"] == f"答案:{entry['text'].splitlines()[0]}"


def test_analyze_sequential_when_concurrency_one(
    question_service, student_user, tmp_path, monkeypatch
):
    """import_concurrency=1：保持顺序执行（并发峰值恒为 1），结果不变。"""
    provider = _TrackingProvider()
    service = DocumentService(Settings(import_concurrency=1))
    job = _run_docx_import(service, provider, student_user.id, tmp_path, monkeypatch, "串行")

    assert job["status"] == "success", job["error"]
    assert provider.max_concurrent == 1, "并发度 1 时应逐题顺序解构"
    result = job["result"]
    assert result["done"] == result["total"] == 4
    assert [s["number"] for s in result["segments"]] == ["1", "2", "3", "4"]
