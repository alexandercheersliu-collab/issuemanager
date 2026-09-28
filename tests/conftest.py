"""pytest 全局夹具。

在导入任何 backend 模块之前设置测试环境变量，
确保 Settings / engine 单例在测试配置下构建。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="mathmaster_test_"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'test.db').as_posix()}"
os.environ["DATA_DIR"] = str(_TMP / "data")
os.environ["CHROMA_DIR"] = str(_TMP / "chroma")
os.environ["RAG_ENABLED"] = "true"  # 允许向量库参与集成测试
os.environ["AI_PROVIDER"] = "mock"
os.environ["BCRYPT_ROUNDS"] = "4"  # 加速测试
os.environ["API_RATE_LIMIT_ENABLED"] = "false"  # 测试大量复用同 IP 登录，关闭限流


def _reusable_model_cache() -> Path | None:
    """复用已安装好的 ONNX 嵌入模型，避免测试期间现下 83MB（弱网下等于卡死）。

    查找顺序：进程环境变量 CHROMA_MODEL_DIR → 项目 .env 里的同名项 →
    DATA_DIR/models/onnx → 项目内 data/models/onnx。
    只有模型确实解压完整才复用，否则交回 ChromaDB 默认位置（自行下载）。
    """
    onnx_files = (
        "config.json",
        "model.onnx",
        "special_tokens_map.json",
        "tokenizer_config.json",
        "tokenizer.json",
        "vocab.txt",
    )
    root = Path(__file__).resolve().parent.parent
    settings: dict[str, str] = {}
    if os.environ.get("CHROMA_MODEL_DIR"):
        settings["CHROMA_MODEL_DIR"] = os.environ["CHROMA_MODEL_DIR"]
    env_file = root / ".env"
    if env_file.is_file():
        try:
            from dotenv import dotenv_values

            settings.update({k: v for k, v in dotenv_values(env_file).items() if v})
        except Exception:  # noqa: BLE001 - .env 不可读时退回默认位置
            pass

    candidates: list[Path] = []
    if settings.get("CHROMA_MODEL_DIR"):
        candidates.append(Path(settings["CHROMA_MODEL_DIR"]))
    if settings.get("DATA_DIR"):
        candidates.append(Path(settings["DATA_DIR"]) / "models" / "onnx")
    candidates.append(root / "data" / "models" / "onnx")

    for parent in candidates:
        extracted = parent.expanduser() / "all-MiniLM-L6-v2" / "onnx"
        if all((extracted / name).is_file() for name in onnx_files):
            return parent.expanduser()
    return None


_model_cache = _reusable_model_cache()
if _model_cache is not None:
    # 隔离出测试专用目录，避免污染真实缓存
    os.environ["CHROMA_MODEL_DIR"] = str(_model_cache)
else:  # 未预装模型：仍指向临时目录，行为与真实运行时一致
    os.environ["CHROMA_MODEL_DIR"] = str(_TMP / "models" / "onnx")

import pytest  # noqa: E402

from backend.database import SessionLocal, init_db  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _database() -> None:
    init_db(seed_users=True)


@pytest.fixture(scope="session")
def api_client():
    from fastapi.testclient import TestClient

    from api.main import create_app

    return TestClient(create_app())


@pytest.fixture
def client(api_client):
    return api_client


@pytest.fixture
def db_session():
    session = SessionLocal()
    try:
        yield session
        session.rollback()
    finally:
        session.close()


@pytest.fixture
def student_user(db_session):
    from backend.models.orm import User

    user = db_session.query(User).filter_by(username="demo").one()
    return user


@pytest.fixture
def question_service():
    from backend.services.question_service import QuestionService

    return QuestionService(session_factory=SessionLocal)
