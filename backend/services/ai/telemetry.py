"""AI 调用与题库匹配遥测：本地 JSONL 记录延迟、结果、复用和实际用量。

写 data/telemetry/ai_calls.jsonl。token 仅记录提供商返回的实际值；题库复用
只统计调用点节省，不推算 token。管理写操作的可追踪记录保存在 SQL 审计表。
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
        record = {
            "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
            "event": "ai_call",
            "operation": operation,
            "latency_ms": elapsed_ms,
            "ok": ctx["ok"],
            "error": ctx["error"],
        }
        usage = ctx.get("token_usage")
        if isinstance(usage, dict):
            actual_usage = {
                key: usage[key]
                for key in ("prompt_tokens", "completion_tokens", "total_tokens")
                if type(usage.get(key)) is int and usage[key] >= 0
            }
            if actual_usage:
                record["token_usage"] = actual_usage
        _write(record)


def record_bank_match(match_status: str) -> None:
    """记录一次标准题匹配结果。"""
    if match_status not in {"exact", "similar", "new"}:
        raise ValueError("未知题库匹配状态")
    _write(
        {
            "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
            "event": "bank_match",
            "match_status": match_status,
        }
    )


def record_bank_decision(decision: str) -> None:
    """记录学生对相似候选作出的明确选择。"""
    if decision not in {"accepted", "new"}:
        raise ValueError("未知相似题决定")
    _write(
        {
            "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
            "event": "bank_decision",
            "decision": decision,
        }
    )


def record_reuse_savings(
    skipped_calls: dict[str, int],
    *,
    shared_generations: int = 0,
    shared_storage_bytes: int = 0,
) -> None:
    """记录实际绕过的调用及复用共享图所避免的生成量和存储量。"""
    actual = {
        operation: count
        for operation, count in skipped_calls.items()
        if type(count) is int and count > 0
    }
    generations = max(0, shared_generations)
    storage_bytes = max(0, shared_storage_bytes)
    if actual or generations or storage_bytes:
        record = {
            "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
            "event": "reuse_savings",
            "skipped_calls": actual,
        }
        if generations or storage_bytes:
            record["shared_asset_savings"] = {
                "generations_avoided": generations,
                "storage_bytes_avoided": storage_bytes,
            }
        _write(record)


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
    """汇总 AI 调用、题库匹配和提供商实际返回的 token 用量。"""
    records = read_recent(limit)
    ai_records = [record for record in records if record.get("event", "ai_call") == "ai_call"]
    by_op: dict[str, dict] = {}
    for record in ai_records:
        bucket = by_op.setdefault(
            record.get("operation", "unknown"), {"count": 0, "avg_ms": 0}
        )
        bucket["count"] += 1
    for operation, bucket in by_op.items():
        op_latencies = [
            record["latency_ms"]
            for record in ai_records
            if record.get("operation") == operation
        ]
        bucket["avg_ms"] = round(sum(op_latencies) / len(op_latencies))

    usage_records = [record["token_usage"] for record in ai_records if "token_usage" in record]
    token_usage = None
    if usage_records:
        token_usage = {"calls_with_usage": len(usage_records)}
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            values = [usage[key] for usage in usage_records if key in usage]
            token_usage[key] = sum(values) if values else None

    bank_records = [record for record in records if record.get("event") == "bank_match"]
    matches = {status: 0 for status in ("exact", "similar", "new")}
    for record in bank_records:
        status = record.get("match_status")
        if status in matches:
            matches[status] += 1
    decisions = {"accepted": 0, "new": 0}
    for record in records:
        if record.get("event") == "bank_decision" and record.get("decision") in decisions:
            decisions[record["decision"]] += 1
    skipped_calls: dict[str, int] = {}
    shared_savings = {"generations_avoided": 0, "storage_bytes_avoided": 0}
    for record in records:
        if record.get("event") != "reuse_savings":
            continue
        for operation, count in (record.get("skipped_calls") or {}).items():
            if type(count) is int and count > 0:
                skipped_calls[operation] = skipped_calls.get(operation, 0) + count
        for name in shared_savings:
            count = (record.get("shared_asset_savings") or {}).get(name)
            if type(count) is int and count > 0:
                shared_savings[name] += count

    ok = sum(1 for record in ai_records if record.get("ok"))
    latencies = [record.get("latency_ms", 0) for record in ai_records]
    return {
        "calls": len(ai_records),
        "success_rate": round(ok / len(ai_records) * 100) if ai_records else None,
        "avg_latency_ms": round(sum(latencies) / len(latencies)) if latencies else None,
        "by_operation": by_op,
        "question_bank": {
            "matches": matches,
            "skipped_ai_calls": skipped_calls,
            "similar_decisions": decisions,
            "corrections": {"similar_rejected": decisions["new"]},
            "shared_asset_savings": shared_savings,
        },
        "token_usage": token_usage,
    }
