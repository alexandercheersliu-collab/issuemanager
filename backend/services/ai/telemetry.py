"""AI 调用遥测：本地 JSONL 记录每次 LLM 调用（延迟/成功/提供商）。

轻量自包含方案：写 data/telemetry/ai_calls.jsonl，一行一次调用。
需要云端聚合时可将此文件接入 Langfuse 等（记录字段与其语义对齐）。
"""
from __future__ import annotations

import datetime as dt
import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from backend.config import get_settings
from backend.utils.logging import get_logger

logger = get_logger("telemetry")


def _telemetry_path() -> Path:
    settings = get_settings()
    path = settings.data_dir / "telemetry"
    path.mkdir(parents=True, exist_ok=True)
    return path / "ai_calls.jsonl"


@contextmanager
def track_ai_call(operation: str) -> Iterator[dict]:
    """包裹一次 AI 调用：自动记录耗时与结果。

    用法：
        with track_ai_call("analyze_image") as ctx:
            result = provider.analyze_question(...)
            ctx["ok"] = True
    """
    ctx: dict = {"ok": False, "error": None}
    start = time.perf_counter()
    try:
        yield ctx
        ctx["ok"] = True
    except Exception as exc:
        ctx["error"] = str(exc)[:300]
        raise
    finally:
        elapsed_ms = round((time.perf_counter() - start) * 1000)
        _write(
            {
                "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
                "operation": operation,
                "latency_ms": elapsed_ms,
                "ok": ctx["ok"],
                "error": ctx["error"],
            }
        )


def _write(record: dict) -> None:
    """追加一条遥测；失败不阻断主流程，但不允许静默丢失。

    数据盘被占用/权限异常时，旧实现只在 debug 级别留一行日志，
    等于遥测悄悄失效（排查网络问题时会被严重误导）。这里降级为 warning，
    并且重试一次，覆盖 Windows 上偶发的瞬时占用。
    """
    line = json.dumps(record, ensure_ascii=False) + "\n"
    for attempt in (1, 2):
        try:
            with _telemetry_path().open("a", encoding="utf-8") as fh:
                fh.write(line)
            if attempt > 1:
                logger.info("遥测写入在第 %s 次尝试后成功", attempt)
            return
        except OSError as exc:  # noqa: BLE001 - 遥测失败不影响主流程
            if attempt == 2:
                logger.warning(
                    "遥测写入失败（已重试 2 次），该次 AI 调用未被记录: %s | 路径=%s",
                    exc,
                    _telemetry_path(),
                )
            else:
                time.sleep(0.05)


def read_recent(limit: int = 100) -> list[dict]:
    """读取最近 limit 条遥测记录（新→旧）。"""
    path = _telemetry_path()
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    records = []
    for line in reversed(lines[-limit:]):
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def summarize(limit: int = 200) -> dict:
    """汇总最近 limit 次调用：次数/成功率/平均延迟/按操作分组。"""
    records = read_recent(limit)
    if not records:
        return {"calls": 0, "success_rate": None, "avg_latency_ms": None, "by_operation": {}}
    ok = sum(1 for r in records if r.get("ok"))
    latencies = [r.get("latency_ms", 0) for r in records]
    by_op: dict[str, dict] = {}
    for r in records:
        bucket = by_op.setdefault(r.get("operation", "unknown"), {"count": 0, "avg_ms": 0})
        bucket["count"] += 1
    for op, bucket in by_op.items():
        op_latencies = [r["latency_ms"] for r in records if r.get("operation") == op]
        bucket["avg_ms"] = round(sum(op_latencies) / len(op_latencies))
    return {
        "calls": len(records),
        "success_rate": round(ok / len(records) * 100),
        "avg_latency_ms": round(sum(latencies) / len(latencies)),
        "by_operation": by_op,
    }
