"""AI 调用遥测测试。"""
from __future__ import annotations

from pathlib import Path

import pytest

import backend.services.ai.telemetry as telemetry


@pytest.fixture(autouse=True)
def isolated_telemetry(tmp_path, monkeypatch):
    monkeypatch.setattr(telemetry, "_telemetry_path", lambda: tmp_path / "ai_calls.jsonl")
    return tmp_path / "ai_calls.jsonl"


def test_track_success_writes_record(isolated_telemetry):
    with telemetry.track_ai_call("analyze_image") as ctx:
        ctx["ok"] = True
    records = telemetry.read_recent()
    assert len(records) == 1
    assert records[0]["operation"] == "analyze_image"
    assert records[0]["ok"] is True
    assert records[0]["latency_ms"] >= 0


def test_track_failure_records_error(isolated_telemetry):
    with pytest.raises(RuntimeError):
        with telemetry.track_ai_call("analyze_text"):
            raise RuntimeError("boom")
    records = telemetry.read_recent()
    assert records[0]["ok"] is False
    assert "boom" in records[0]["error"]


def test_summarize_aggregates(isolated_telemetry):
    with telemetry.track_ai_call("a"):
        pass
    with telemetry.track_ai_call("a"):
        pass
    with pytest.raises(RuntimeError):
        with telemetry.track_ai_call("b"):
            raise RuntimeError("x")

    summary = telemetry.summarize()
    assert summary["calls"] == 3
    assert summary["success_rate"] == round(2 / 3 * 100)
    assert summary["by_operation"]["a"]["count"] == 2
    assert summary["by_operation"]["b"]["count"] == 1


def test_summarize_empty():
    assert telemetry.summarize()["calls"] == 0


def test_write_retries_once_then_succeeds(isolated_telemetry, monkeypatch):
    """瞬时占用（Windows 常见）应当重试一次后成功写入。"""
    real_open = Path.open
    calls = {"n": 0}

    def flaky_open(self, *args, **kwargs):
        if self == isolated_telemetry:
            calls["n"] += 1
            if calls["n"] == 1:
                raise PermissionError(13, "transient lock")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", flaky_open)
    with telemetry.track_ai_call("analyze_text"):
        pass

    assert calls["n"] == 2  # 第一次失败、第二次成功
    records = telemetry.read_recent()
    assert len(records) == 1
    assert records[0]["ok"] is True


def test_write_failure_warns_instead_of_silent_loss(isolated_telemetry, monkeypatch):
    """写不进去时必须留下 warning（旧实现只在 debug 级别，等于静默丢失）。"""
    warnings: list[str] = []
    monkeypatch.setattr(telemetry.logger, "warning", lambda msg, *a: warnings.append(msg % a if a else msg))
    monkeypatch.setattr(
        Path, "open", lambda self, *a, **k: (_ for _ in ()).throw(PermissionError(13, "denied"))
    )

    with telemetry.track_ai_call("analyze_text"):  # 不得抛出
        pass

    assert len(warnings) == 1
    assert "遥测写入失败" in warnings[0]
    assert isolated_telemetry.exists() is False
