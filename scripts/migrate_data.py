r"""数据整体迁移脚本（P4.2）：把整套运行时数据从旧路径迁移到新 DATA_DIR。

迁移组件：
- SQLite 数据库文件（含行数比对校验；MySQL 等外部库不支持文件级迁移，会提示）
- uploads（错题原图 + 整卷导入文档）
- data_dir 根下的散落文件（遥测日志等）
- ChromaDB 向量库（chroma_dir）
- 嵌入模型缓存（chroma_model_dir）

策略为「复制 + 校验」，不删除旧数据；确认新路径可用后手工清理旧目录。
核心函数（build_plan/execute_plan/verify_plan）不依赖全局 Settings，便于单测。

用法：
    python scripts/migrate_data.py --new-data-dir D:\new\data [--dry-run]
    python scripts/migrate_data.py --new-data-dir D:\new\data \
        --chroma-dir D:\new\data\chroma --model-dir D:\new\data\models\onnx
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@dataclass
class ComponentPlan:
    """一个迁移组件：源 → 目标（文件或目录）。"""

    component: str  # db / uploads / data_extra / chroma / models
    src: Path
    dst: Path
    is_dir: bool = True
    src_files: int = 0
    src_bytes: int = 0
    skipped: str = ""  # 非空 = 跳过原因（源不存在等）


@dataclass
class VerifyReport:
    component: str
    ok: bool
    detail: str
    extra: dict = field(default_factory=dict)


def dir_stats(path: Path) -> tuple[int, int]:
    """目录下文件数与总字节（不存在返回 0,0）。"""
    if not path.is_dir():
        return 0, 0
    files = [p for p in path.rglob("*") if p.is_file()]
    return len(files), sum(p.stat().st_size for p in files)


def resolve_db_path(database_url: str, data_dir: Path) -> Path | None:
    """解析 SQLite 库文件路径；非 SQLite（如 MySQL）返回 None。"""
    if not database_url:
        return data_dir / "math_tutor.db"
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        return None
    return Path(database_url[len(prefix):])


def build_plan(
    *,
    data_dir: Path,
    database_url: str = "",
    chroma_dir: Path,
    chroma_model_dir: Path | None,
    new_data_dir: Path,
    new_chroma_dir: Path | None = None,
    new_model_dir: Path | None = None,
) -> list[ComponentPlan]:
    """根据当前配置与新 DATA_DIR 构造迁移计划（纯函数，不触碰文件系统）。"""
    plan: list[ComponentPlan] = []

    db_path = resolve_db_path(database_url, data_dir)
    if db_path is None:
        plan.append(
            ComponentPlan(
                "db", Path(database_url), Path(),
                is_dir=False, skipped="非 SQLite 数据库，请用 mysqldump 等工具迁移",
            )
        )
    else:
        plan.append(
            ComponentPlan("db", db_path, new_data_dir / db_path.name, is_dir=False)
        )

    uploads = data_dir / "uploads"
    plan.append(ComponentPlan("uploads", uploads, new_data_dir / "uploads"))

    # data_dir 根下的散落文件（遥测日志等），不含子目录与 DB
    plan.append(
        ComponentPlan("data_extra", data_dir, new_data_dir, is_dir=True)
    )

    plan.append(
        ComponentPlan(
            "chroma", chroma_dir, new_chroma_dir or (new_data_dir / "chroma")
        )
    )
    if chroma_model_dir is not None:
        plan.append(
            ComponentPlan(
                "models",
                chroma_model_dir,
                new_model_dir or (new_data_dir / "models" / "onnx"),
            )
        )

    for item in plan:
        if item.skipped:
            continue
        if not item.src.exists():
            item.skipped = "源不存在（全新安装或该组件未启用）"
            continue
        if item.component == "data_extra":
            # 只统计根目录散落文件（子目录由独立组件负责，DB 单独迁移）
            files = [
                p for p in item.src.iterdir() if p.is_file() and p.suffix != ".db"
            ] if item.src.is_dir() else []
            item.src_files = len(files)
            item.src_bytes = sum(p.stat().st_size for p in files)
        elif item.is_dir:
            item.src_files, item.src_bytes = dir_stats(item.src)
        else:
            item.src_files, item.src_bytes = 1, item.src.stat().st_size
    return plan


def _copy_component(item: ComponentPlan) -> None:
    if item.component == "data_extra":
        # 只拷根目录散落文件，跳过子目录（uploads/chroma 等由独立组件负责）与 DB
        item.dst.mkdir(parents=True, exist_ok=True)
        for entry in item.src.iterdir():
            if entry.is_file() and entry.suffix != ".db":
                shutil.copy2(entry, item.dst / entry.name)
        return
    if item.is_dir:
        shutil.copytree(item.src, item.dst, dirs_exist_ok=True)
    else:
        item.dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item.src, item.dst)


def execute_plan(plan: list[ComponentPlan], *, dry_run: bool = False) -> None:
    """执行迁移计划（dry_run 时只打印不落盘）。"""
    for item in plan:
        if item.skipped:
            continue
        if dry_run:
            continue
        _copy_component(item)


def db_table_counts(db_path: Path) -> dict[str, int]:
    """SQLite 库各表行数（校验用；库不存在返回空）。"""
    if not db_path.is_file():
        return {}
    counts: dict[str, int] = {}
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
                " AND name NOT LIKE 'sqlite_%'"
            )
        ]
        for table in tables:
            counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608 - 表名来自 sqlite_master
    return counts


def verify_plan(plan: list[ComponentPlan]) -> list[VerifyReport]:
    """迁移后校验：文件数/字节比对 + DB 行数比对。"""
    reports: list[VerifyReport] = []
    for item in plan:
        if item.skipped:
            reports.append(VerifyReport(item.component, True, f"跳过：{item.skipped}"))
            continue
        if item.component == "data_extra":
            src_files = [
                p for p in item.src.iterdir() if p.is_file() and p.suffix != ".db"
            ] if item.src.is_dir() else []
            dst_files = [
                item.dst / p.name for p in src_files if (item.dst / p.name).is_file()
            ]
            ok = len(dst_files) == len(src_files)
            reports.append(
                VerifyReport(
                    item.component, ok, f"散落文件 {len(dst_files)}/{len(src_files)}"
                )
            )
            continue
        if item.is_dir:
            dst_files, dst_bytes = dir_stats(item.dst)
            ok = dst_files >= item.src_files and dst_bytes >= item.src_bytes
            reports.append(
                VerifyReport(
                    item.component,
                    ok,
                    f"文件 {dst_files}/{item.src_files}，字节 {dst_bytes}/{item.src_bytes}",
                )
            )
            continue
        # DB：文件存在 + 行数一致
        if not item.dst.is_file():
            reports.append(VerifyReport(item.component, False, "目标库文件缺失"))
            continue
        src_counts = db_table_counts(item.src)
        dst_counts = db_table_counts(item.dst)
        ok = src_counts == dst_counts
        detail = "；".join(f"{t}={c}" for t, c in sorted(dst_counts.items())) or "空库"
        reports.append(
            VerifyReport(item.component, ok, detail, extra={"src": src_counts, "dst": dst_counts})
        )
    return reports


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="整体迁移 DB + 向量库 + 上传 + 模型缓存到新 DATA_DIR")
    parser.add_argument("--new-data-dir", required=True, help="新的 DATA_DIR 绝对路径")
    parser.add_argument("--chroma-dir", default="", help="新向量库目录（默认 <new>/chroma）")
    parser.add_argument("--model-dir", default="", help="新模型缓存目录（默认 <new>/models/onnx）")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划与源统计，不复制")
    args = parser.parse_args(argv)

    from backend.config import get_settings

    settings = get_settings()
    new_data_dir = Path(args.new_data_dir)
    if not new_data_dir.is_absolute():
        print("错误：--new-data-dir 必须是绝对路径")
        return 2

    plan = build_plan(
        data_dir=settings.data_dir,
        database_url=settings.database_url,
        chroma_dir=settings.chroma_dir,
        chroma_model_dir=settings.chroma_model_dir,
        new_data_dir=new_data_dir,
        new_chroma_dir=Path(args.chroma_dir) if args.chroma_dir else None,
        new_model_dir=Path(args.model_dir) if args.model_dir else None,
    )

    print("迁移计划（复制+校验，不删除旧数据）：")
    for item in plan:
        if item.skipped:
            print(f"  [{item.component}] 跳过：{item.skipped}")
        else:
            print(
                f"  [{item.component}] {item.src} -> {item.dst}"
                f"（{item.src_files} 个文件，{item.src_bytes / 1024:.1f} KB）"
            )

    if args.dry_run:
        print("dry-run：未复制任何文件")
        return 0

    execute_plan(plan)
    print("复制完成，开始校验…")
    reports = verify_plan(plan)
    all_ok = True
    for report in reports:
        mark = "✅" if report.ok else "❌"
        print(f"  {mark} [{report.component}] {report.detail}")
        all_ok = all_ok and report.ok

    chroma_dst = Path(args.chroma_dir) if args.chroma_dir else new_data_dir / "chroma"
    model_dst = (
        Path(args.model_dir) if args.model_dir else new_data_dir / "models" / "onnx"
    )
    print("\n校验全部通过。请在 .env 中更新以下配置后重启应用：")
    print(f"  DATA_DIR={new_data_dir}")
    print(f"  CHROMA_DIR={chroma_dst}")
    print(f"  CHROMA_MODEL_DIR={model_dst}")
    print("确认应用在新路径运行正常后，可手工清理旧目录。")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
