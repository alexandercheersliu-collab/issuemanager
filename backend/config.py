"""应用配置中心。

所有配置通过 pydantic-settings 从环境变量 / .env 读取，
类型与取值范围在启动时即完成校验，避免配置错误潜伏到运行期。
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---------- 应用 ----------
    app_name: str = "MathMaster Edu"
    app_version: str = "2.1.0"
    debug: bool = False
    # 运行时数据根目录（SQLite / 上传原图 / 遥测日志）。
    # 默认放在项目内 data/；可通过 DATA_DIR 指向项目外的独立数据盘，
    # 例如 DATA_DIR=D:\workspace\learning-tour\data（此时 DB 仍在 data/ 下）。
    data_dir: Path = PROJECT_ROOT / "data"

    # ---------- 观测（可选）----------
    # 配置 SENTRY_DSN 后自动启用错误上报（需 pip install sentry-sdk）
    sentry_dsn: str = ""
    sentry_traces_sample_rate: float = Field(default=0.0, ge=0.0, le=1.0)

    # ---------- 数据库 ----------
    # 留空 = SQLite 开箱即用，文件为 {DATA_DIR}/math_tutor.db；
    # 也可显式指定绝对路径：DATABASE_URL=sqlite:///D:/workspace/learning-tour/data/math_tutor.db
    # 切换 MySQL 示例：
    # DATABASE_URL=mysql+pymysql://user:password@localhost:3306/math_tutor?charset=utf8mb4
    database_url: str = ""

    # ---------- 认证 ----------
    bcrypt_rounds: int = Field(default=12, ge=4, le=31)
    seed_admin_username: str = "admin"
    seed_admin_password: str = "admin123"
    seed_demo_username: str = "demo"
    seed_demo_password: str = "demo123"

    # ---------- API 网关 (JWT) ----------
    # 生产环境务必通过 .env 设置强随机密钥（>= 32 字节）
    auth_secret: str = "dev-only-secret-change-me-0123456789abcdef"
    access_token_expire_minutes: int = Field(default=7 * 24 * 60, ge=1)
    api_prefix: str = "/api"

    # ---------- AI 提供商 ----------
    # openai_compatible: 任何兼容 OpenAI Chat Completions 的服务
    #   (SiliconFlow / Qwen / GLM / DeepSeek / OpenAI / Ollama ...)
    # gemini: Google Gemini（需要 google-genai 包）
    # mock:   无 Key 演示模式，返回固定结构化结果
    ai_provider: Literal["openai_compatible", "gemini", "mock"] = "mock"
    ai_base_url: str = "https://api.siliconflow.cn/v1"
    ai_api_key: str = ""
    ai_model: str = "Qwen/Qwen2.5-VL-32B-Instruct"
    ai_temperature: float = Field(default=0.3, ge=0.0, le=2.0)
    ai_max_retries: int = Field(default=3, ge=1, le=10)
    ai_timeout_seconds: float = Field(default=90.0, gt=0)

    # ---------- RAG / 向量库 ----------
    chroma_dir: Path = PROJECT_ROOT / "data" / "chroma"
    # ChromaDB 内置嵌入模型（all-MiniLM-L6-v2）的 ONNX 缓存父目录；留空 = 沿用其默认
    # （用户主目录 ~/.cache/chroma/onnx_models）。指向数据盘可避免模型与向量库分居两处：
    # CHROMA_MODEL_DIR=D:\workspace\learning-tour\data\models\onnx
    chroma_model_dir: Path | None = None
    rag_enabled: bool = True
    embedding_model: str = "BAAI/bge-m3"
    embedding_base_url: str = ""  # 留空则使用 ChromaDB 内置本地嵌入模型
    embedding_api_key: str = ""
    rag_top_k: int = Field(default=3, ge=1, le=20)

    # ---------- 重排（可选，混合检索精排）----------
    # SiliconFlow: https://api.siliconflow.cn/v1 + BAAI/bge-reranker-v2-m3
    rerank_base_url: str = ""
    rerank_api_key: str = ""
    rerank_model: str = "BAAI/bge-reranker-v2-m3"

    # ---------- OCR（可选）----------
    # 开启后录题时对原图做文字识别，识别文本参与语义/关键词搜索。
    # 需要：pip install rapidocr-onnxruntime
    ocr_enabled: bool = False

    # ---------- 分享卡片 ----------
    # 渲染分享卡片使用的中文字体文件（.ttf/.ttc/.otf）绝对路径；
    # 留空 = 按平台自动探测常见字体（Windows 微软雅黑/黑体、Linux 文泉驿、
    # macOS 苹方），都找不到则回退 PIL 默认字体（可能不支持中文）。
    # SHARE_CARD_FONT_PATH=/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc
    share_card_font_path: Path | None = None

    # ---------- 文档上传（整卷导入）----------
    # 上传文档（PDF/DOCX 等）的存储目录；留空 = <DATA_DIR>/uploads/docs，
    # 自定义 DATA_DIR 时自动跟随。显式指定示例：
    # DOC_UPLOAD_PATH=D:\workspace\learning-tour\data\uploads\docs
    doc_upload_path: Path | None = None

    # ---------- 复习算法 (SM-2) ----------
    review_default_ease: float = Field(default=2.5, ge=1.3)
    review_again_minutes: int = Field(default=10, ge=1)

    @field_validator("chroma_model_dir", "share_card_font_path", "doc_upload_path", mode="before")
    @classmethod
    def _blank_means_default(cls, value: object) -> object:
        """空字符串（如 CHROMA_MODEL_DIR=）等价于「未配置」。

        必须在 before 阶段拦截：pydantic-settings 会把空字符串先转成 Path('.')，
        到 after 阶段就再也分不清「用户留空」和「真的想用当前目录」。
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator(
        "data_dir", "chroma_dir", "chroma_model_dir", "share_card_font_path", "doc_upload_path",
        mode="after",
    )
    @classmethod
    def _expand_paths(cls, value: Path | None) -> Path | None:
        if value is None:
            return None
        return value.expanduser().resolve()

    @model_validator(mode="after")
    def _resolve_database_url(self) -> Settings:
        """未显式配置 DATABASE_URL 时，SQLite 落在 DATA_DIR 下。"""
        if not self.database_url.strip():
            db_file = (self.data_dir / "math_tutor.db").as_posix()
            self.database_url = f"sqlite:///{db_file}"
        return self

    @model_validator(mode="after")
    def _resolve_doc_upload_path(self) -> Settings:
        """未显式配置 DOC_UPLOAD_PATH 时，落在 <DATA_DIR>/uploads/docs。"""
        if self.doc_upload_path is None:
            self.doc_upload_path = self.data_dir / "uploads" / "docs"
        return self

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if self.doc_upload_path is not None:
            self.doc_upload_path.mkdir(parents=True, exist_ok=True)
        if self.rag_enabled:
            self.chroma_dir.mkdir(parents=True, exist_ok=True)
            if self.chroma_model_dir is not None:
                self.chroma_model_dir.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    """进程级单例配置。"""
    return Settings()
