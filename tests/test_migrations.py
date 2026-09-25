"""Alembic 迁移冒烟测试：baseline 能在空库上建出完整 schema。"""
from __future__ import annotations

import configparser
import sqlite3
from pathlib import Path

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"


def test_alembic_ini_is_ascii_readable():
    """alembic.ini 必须能按系统 locale 解码。

    Alembic 用 encoding="locale" 读取该文件（中文 Windows 即 cp936），
    一旦写入 UTF-8 中文注释，真实 `alembic upgrade head` 会直接报
    UnicodeDecodeError；这里用 locale 编码预检，防止回归。
    """
    import locale

    ALEMBIC_INI.read_bytes().decode(locale.getpreferredencoding(False))


def test_baseline_migration_creates_schema(tmp_path, monkeypatch):
    from alembic import command
    from alembic.config import Config

    db = tmp_path / "migration_test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db.as_posix()}")

    # alembic.ini 以 UTF-8 保存（含中文注释）；Alembic 默认按系统 locale 读取
    # （中文 Windows 是 cp936）会抛 UnicodeDecodeError/ParsingError，
    # 因此这里自行以 UTF-8 解析后注入 Config，绕开按 locale 的重复读取。
    parser = configparser.ConfigParser(interpolation=None)
    with open(ALEMBIC_INI, encoding="utf-8") as fh:
        parser.read_file(fh)
    cfg = Config()
    cfg.config_file_name = "alembic.ini"
    cfg.__dict__["file_config"] = parser
    for section in parser.sections():
        for key, value in parser.items(section):
            cfg.set_section_option(section, key, value)
    cfg.set_main_option("script_location", "migrations")
    command.upgrade(cfg, "head")

    conn = sqlite3.connect(db)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"users", "questions", "review_logs", "alembic_version"} <= tables

    # 版本表已标记到 head
    (version,) = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    assert version
