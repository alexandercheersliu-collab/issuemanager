"""配置中心路径项测试（DATA_DIR 派生默认值与显式覆盖）。"""
from __future__ import annotations

from pathlib import Path

from backend.config import Settings


def test_doc_upload_path_defaults_under_data_dir(tmp_path):
    """未配置 DOC_UPLOAD_PATH 时，默认落在 <DATA_DIR>/uploads/docs。"""
    settings = Settings(data_dir=tmp_path / "data")
    assert settings.doc_upload_path == (tmp_path / "data" / "uploads" / "docs").resolve()


def test_doc_upload_path_explicit_override(tmp_path):
    """显式配置 DOC_UPLOAD_PATH 时生效，并做 expanduser/resolve。"""
    target = tmp_path / "custom" / "docs"
    settings = Settings(data_dir=tmp_path / "data", doc_upload_path=target)
    assert settings.doc_upload_path == target.resolve()
    assert settings.doc_upload_path != settings.data_dir / "uploads" / "docs"


def test_doc_upload_path_blank_env_falls_back(tmp_path, monkeypatch):
    """DOC_UPLOAD_PATH=（空串）等价于未配置，回退到 DATA_DIR 派生默认。"""
    monkeypatch.setenv("DOC_UPLOAD_PATH", "")
    settings = Settings(data_dir=tmp_path / "data")
    assert settings.doc_upload_path == (tmp_path / "data" / "uploads" / "docs").resolve()


def test_ensure_dirs_creates_doc_upload_path(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", rag_enabled=False)
    settings.ensure_dirs()
    assert settings.doc_upload_path is not None
    assert Path(settings.doc_upload_path).is_dir()
