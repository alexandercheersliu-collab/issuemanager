"""数据迁移脚本测试：计划构造、复制完整性、DB 行数比对、dry-run、失败检出。"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "migrate_data",
    Path(__file__).resolve().parent.parent / "scripts" / "migrate_data.py",
)
migrate = importlib.util.module_from_spec(_SPEC)
sys.modules["migrate_data"] = migrate  # dataclass 装饰器要求模块已注册
_SPEC.loader.exec_module(migrate)


def _make_source(root: Path) -> dict:
    """构造一套假数据：SQLite 库（2 表 3 行）+ uploads + 散落日志 + chroma + 模型。"""
    data_dir = root / "old_data"
    (data_dir / "uploads" / "1").mkdir(parents=True)
    (data_dir / "uploads" / "docs").mkdir(parents=True)
    (data_dir / "uploads" / "1" / "img.jpg").write_bytes(b"\xff\xd8img")
    (data_dir / "uploads" / "docs" / "paper.pdf").write_bytes(b"%PDF-fake")
    (data_dir / "telemetry.jsonl").write_text('{"event":"boot"}\n', encoding="utf-8")

    db_path = data_dir / "math_tutor.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE questions (id INTEGER PRIMARY KEY, content TEXT)")
        conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
        conn.executemany(
            "INSERT INTO questions VALUES (?, ?)", [(1, "题一"), (2, "题二")]
        )
        conn.execute("INSERT INTO users VALUES (1, 'demo')")

    chroma_dir = root / "old_chroma"
    (chroma_dir / "index").mkdir(parents=True)
    (chroma_dir / "index" / "data.bin").write_bytes(b"vector")
    model_dir = root / "old_models"
    (model_dir / "all-MiniLM-L6-v2").mkdir(parents=True)
    (model_dir / "all-MiniLM-L6-v2" / "model.onnx").write_bytes(b"onnx")
    return {
        "data_dir": data_dir,
        "db": db_path,
        "chroma_dir": chroma_dir,
        "model_dir": model_dir,
    }


def _build(src: dict, new_data_dir: Path):
    return migrate.build_plan(
        data_dir=src["data_dir"],
        database_url="",
        chroma_dir=src["chroma_dir"],
        chroma_model_dir=src["model_dir"],
        new_data_dir=new_data_dir,
    )


def test_plan_covers_all_components(tmp_path):
    src = _make_source(tmp_path)
    plan = _build(src, tmp_path / "new_data")
    by_name = {item.component: item for item in plan}
    assert set(by_name) == {"db", "uploads", "data_extra", "chroma", "models"}
    assert not any(item.skipped for item in plan)
    assert by_name["db"].dst == tmp_path / "new_data" / "math_tutor.db"
    assert by_name["db"].src_files == 1
    assert by_name["uploads"].src_files == 2
    assert by_name["data_extra"].src_files == 1  # 只含 telemetry.jsonl，不含 DB


def test_execute_and_verify_roundtrip(tmp_path):
    src = _make_source(tmp_path)
    new_dir = tmp_path / "new_data"
    plan = _build(src, new_dir)

    migrate.execute_plan(plan)
    reports = migrate.verify_plan(plan)
    assert all(r.ok for r in reports), [r.detail for r in reports]

    # DB 行数比对一致
    db_report = next(r for r in reports if r.component == "db")
    assert db_report.extra["src"] == db_report.extra["dst"]
    assert db_report.extra["dst"] == {"questions": 2, "users": 1}
    # 文件落位
    assert (new_dir / "uploads" / "1" / "img.jpg").is_file()
    assert (new_dir / "uploads" / "docs" / "paper.pdf").is_file()
    assert (new_dir / "telemetry.jsonl").is_file()
    assert (new_dir / "chroma" / "index" / "data.bin").is_file()
    assert (new_dir / "models" / "onnx" / "all-MiniLM-L6-v2" / "model.onnx").is_file()


def test_verify_detects_missing_file(tmp_path):
    src = _make_source(tmp_path)
    new_dir = tmp_path / "new_data"
    plan = _build(src, new_dir)
    migrate.execute_plan(plan)

    (new_dir / "uploads" / "docs" / "paper.pdf").unlink()  # 人为制造缺失
    reports = migrate.verify_plan(plan)
    uploads = next(r for r in reports if r.component == "uploads")
    assert uploads.ok is False


def test_dry_run_writes_nothing(tmp_path):
    src = _make_source(tmp_path)
    new_dir = tmp_path / "new_data"
    plan = _build(src, new_dir)
    migrate.execute_plan(plan, dry_run=True)
    assert not new_dir.exists()


def test_mysql_db_is_skipped_with_hint(tmp_path):
    src = _make_source(tmp_path)
    plan = migrate.build_plan(
        data_dir=src["data_dir"],
        database_url="mysql+pymysql://u:p@h/db",
        chroma_dir=src["chroma_dir"],
        chroma_model_dir=None,
        new_data_dir=tmp_path / "new_data",
    )
    db = next(item for item in plan if item.component == "db")
    assert "非 SQLite" in db.skipped
    assert not any(item.component == "models" for item in plan)  # 未配置模型目录则无该组件


def test_missing_sources_are_skipped(tmp_path):
    plan = migrate.build_plan(
        data_dir=tmp_path / "nothing",
        database_url="",
        chroma_dir=tmp_path / "no_chroma",
        chroma_model_dir=None,
        new_data_dir=tmp_path / "new_data",
    )
    assert all(item.skipped for item in plan)
    migrate.execute_plan(plan)  # 不抛异常
    reports = migrate.verify_plan(plan)
    assert all(r.ok for r in reports)  # 跳过视为通过


def test_main_requires_absolute_path(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "d"))
    from backend.config import get_settings

    get_settings.cache_clear()
    try:
        assert migrate.main(["--new-data-dir", "relative/path"]) == 2
    finally:
        get_settings.cache_clear()
    assert "绝对路径" in capsys.readouterr().out
