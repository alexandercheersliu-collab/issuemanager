"""整卷导入服务：文档上传去重、异步解析任务编排、确认入库。

复用 jobs 表与「提交即返回 job_id + 后台线程执行」的既有异步模式
（与 JobService 一致，未来可平滑替换为 Celery worker）。

任务负载（payload）：文件名 / SHA-256 / 文档路径 / K12 录入上下文。
任务结果（result）：页数、切题与逐题解构进度（total/done）、segments 明细。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import threading
import uuid
from pathlib import Path

from sqlalchemy import select

from backend.config import Settings, get_settings
from backend.database import SessionLocal
from backend.models.orm import Job
from backend.services.document import SUPPORTED_SUFFIXES, parse_document
from backend.utils.logging import get_logger

logger = get_logger("document_service")

JOB_TYPE = "document_import"


class DocumentService:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    # ---------- 上传与去重 ----------
    def import_document(
        self,
        user_id: int,
        data: bytes,
        filename: str,
        *,
        subject: str = "math",
        grade: int | None = None,
        region: str | None = None,
        textbook_version: str | None = None,
    ) -> tuple[str, bool]:
        """接收文档字节，落 DOC_UPLOAD_PATH 并创建异步导入任务。

        返回 (job_id, duplicated)：同一用户重复上传同一文档（SHA-256 相同）
        且已有任务未失败时，直接返回既有任务，不重复解析。
        """
        suffix = Path(filename).suffix.lower()
        if suffix not in SUPPORTED_SUFFIXES:
            raise ValueError(f"不支持的文档格式: {suffix}（支持 {sorted(SUPPORTED_SUFFIXES)}）")
        if not data:
            raise ValueError("文档内容为空")

        doc_hash = hashlib.sha256(data).hexdigest()
        existing = self._find_active_job(user_id, doc_hash)
        if existing is not None:
            logger.info("重复文档 job=%s user=%s，直接复用", existing, user_id)
            return existing, True

        upload_dir = self.settings.doc_upload_path
        assert upload_dir is not None  # model_validator 保证非 None
        upload_dir.mkdir(parents=True, exist_ok=True)
        doc_path = upload_dir / f"{doc_hash}{suffix}"
        doc_path.write_bytes(data)

        job_id = uuid.uuid4().hex
        with SessionLocal() as session:
            session.add(
                Job(
                    id=job_id,
                    user_id=user_id,
                    type=JOB_TYPE,
                    status="pending",
                    payload={
                        "filename": filename,
                        "doc_hash": doc_hash,
                        "doc_path": str(doc_path),
                        "subject": subject or "math",
                        "grade": grade,
                        "region": region,
                        "textbook_version": textbook_version,
                    },
                )
            )
            session.commit()

        thread = threading.Thread(
            target=self._run_import, args=(job_id, user_id), daemon=True
        )
        thread.start()
        logger.info("文档导入任务已提交 job=%s user=%s file=%s", job_id, user_id, filename)
        return job_id, False

    def _find_active_job(self, user_id: int, doc_hash: str) -> str | None:
        """按 SHA-256 找同用户未失败的既有导入任务。"""
        with SessionLocal() as session:
            jobs = session.execute(
                select(Job).where(Job.user_id == user_id, Job.type == JOB_TYPE)
            ).scalars()
            for job in jobs:
                if (job.payload or {}).get("doc_hash") == doc_hash and job.status != "failed":
                    return job.id
        return None

    # ---------- 异步执行 ----------
    def _run_import(self, job_id: str, user_id: int) -> None:
        with SessionLocal() as session:
            job = session.get(Job, job_id)
            if job is None:
                return
            payload = dict(job.payload or {})
            job.status = "running"
            session.commit()

        try:
            pages = parse_document(
                payload["doc_path"],
                Path(payload["doc_path"]).parent / "pages" / job_id,
            )
            result = {
                "pages": len(pages),
                "scanned_pages": sum(1 for p in pages if p.is_scanned),
                "total": 0,
                "done": 0,
                "segments": [],
                "confirmed": False,
            }
            self._finish(job_id, status="success", result=result)
        except Exception as exc:  # noqa: BLE001 - 失败落库供轮询方查看
            logger.warning("文档导入失败 job=%s: %s", job_id, exc)
            self._finish(job_id, status="failed", error=str(exc))

    def _finish(
        self,
        job_id: str,
        *,
        status: str,
        result: dict | None = None,
        error: str | None = None,
    ) -> None:
        with SessionLocal() as session:
            job = session.get(Job, job_id)
            if job is None:
                return
            job.status = status
            if result is not None:
                job.result = result
            job.error = error
            job.finished_at = dt.datetime.now(dt.timezone.utc)
            session.commit()

    # ---------- 查询 ----------
    def get_job(self, job_id: str, user_id: int) -> dict | None:
        """查询导入任务状态（按用户隔离）。"""
        with SessionLocal() as session:
            job = session.execute(
                select(Job).where(Job.id == job_id, Job.user_id == user_id, Job.type == JOB_TYPE)
            ).scalar_one_or_none()
            if job is None:
                return None
            return {
                "job_id": job.id,
                "status": job.status,
                "type": job.type,
                "payload": job.payload,
                "result": job.result,
                "error": job.error,
                "created_at": job.created_at,
                "finished_at": job.finished_at,
            }
