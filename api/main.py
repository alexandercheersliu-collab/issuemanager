"""FastAPI 网关：把 backend 服务暴露为 REST API，供多端复用。

启动：uvicorn api.main:app --port 8000
文档：http://localhost:8000/docs
"""
from __future__ import annotations

from pathlib import Path

import bootstrap  # noqa: F401  注入 HOME/HF_HOME 等本地化环境变量，必须在其他 import 之前

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from api.routers import (
    admin,
    agent,
    auth,
    comments,
    detection,
    documents,
    jobs,
    questions,
    review,
    stats,
    tags,
)
from backend.config import Settings, get_settings
from backend.utils.logging import get_logger

logger = get_logger("api")


def _init_sentry(settings: Settings) -> None:
    """可选的错误上报：配置 SENTRY_DSN 后启用（依赖缺失时仅告警）。"""
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration

        sentry_sdk.init(
            dsn=settings.sentry_dsn,
            traces_sample_rate=settings.sentry_traces_sample_rate,
            release=f"mathmaster@{settings.app_version}",
            integrations=[FastApiIntegration()],
        )
        logger.info("Sentry 已启用")
    except ImportError:
        logger.warning("SENTRY_DSN 已配置但 sentry-sdk 未安装：pip install sentry-sdk[fastapi]")


def create_app() -> FastAPI:
    settings = get_settings()
    if settings.sentry_dsn:
        _init_sentry(settings)

    app = FastAPI(
        title=f"{settings.app_name} API",
        version=settings.app_version,
        description=(
            "智能错题本 REST API：认证 / 错题管理 / AI 录题 / 复习调度 / 学情统计。"
            "所有受保护端点使用 Bearer JWT。"
        ),
    )
    cors_origins = [o.strip() for o in settings.api_cors_origins.split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins or ["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(auth.router, prefix=settings.api_prefix)
    app.include_router(questions.router, prefix=settings.api_prefix)
    app.include_router(review.router, prefix=settings.api_prefix)
    app.include_router(stats.router, prefix=settings.api_prefix)
    app.include_router(tags.router, prefix=settings.api_prefix)
    app.include_router(comments.router, prefix=settings.api_prefix)
    app.include_router(agent.router, prefix=settings.api_prefix)
    app.include_router(jobs.router, prefix=settings.api_prefix)
    app.include_router(documents.router, prefix=settings.api_prefix)
    app.include_router(detection.router, prefix=settings.api_prefix)
    app.include_router(admin.router, prefix=settings.api_prefix)

    @app.get("/health", tags=["meta"])
    def health() -> dict:
        return {"status": "ok", "version": settings.app_version}

    # 移动端静态站点：mobile/dist 存在时挂到 /m，与 API 同源（免 CORS、单一端口）。
    # 应用无客户端路由（Tab 是状态而非路由），html=True 的目录回退足够。
    mobile_dist = Path(__file__).resolve().parent.parent / "mobile" / "dist"
    if mobile_dist.is_dir():
        app.mount("/m", StaticFiles(directory=mobile_dist, html=True), name="mobile")
        logger.info("移动端静态站点已挂载: /m -> %s", mobile_dist)

        @app.get("/", include_in_schema=False)
        def index() -> RedirectResponse:
            return RedirectResponse("/m/")
    else:
        logger.info("mobile/dist 不存在，跳过移动端静态托管（先执行 npm run build）")

    return app


app = create_app()
