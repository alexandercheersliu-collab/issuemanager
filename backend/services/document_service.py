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
        """完整管线：解析 → 双策略切题 → 逐题结构化解构（进度写入 result）。"""
        from backend.services.ai import get_ai_service
        from backend.services.ai.base import AnalysisContext
        from backend.services.segment import segment_document

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
            ai = get_ai_service(self.settings)
            context = AnalysisContext(
                subject=payload.get("subject") or "math",
                grade=payload.get("grade"),
                region=payload.get("region"),
                textbook_version=payload.get("textbook_version"),
            )
            segments = segment_document(pages, ai)

            result: dict = {
                "pages": len(pages),
                "scanned_pages": sum(1 for p in pages if p.is_scanned),
                "total": len(segments),
                "done": 0,
                "segments": [],
                "confirmed": False,
            }
            self._save_result(job_id, result)

            for segment in segments:
                entry: dict = {
                    "number": segment.number,
                    "text": segment.text,
                    "page_start": segment.page_start,
                    "page_end": segment.page_end,
                    "needs_review": segment.needs_review,
                    "continued": segment.continued,
                    "source": segment.source,
                    "analysis": None,
                    "error": None,
                }
                try:
                    analysis = ai.analyze_text(segment.text[:4000], context=context)
                    entry["analysis"] = analysis.model_dump(mode="json")
                except Exception as exc:  # noqa: BLE001 - 单题失败不阻断整卷
                    logger.warning("逐题解构失败 job=%s 题%s: %s", job_id, segment.number, exc)
                    entry["error"] = str(exc)
                    entry["needs_review"] = True
                result["segments"].append(entry)
                result["done"] += 1
                self._save_result(job_id, result)

            self._finish(job_id, status="success", result=result)
        except Exception as exc:  # noqa: BLE001 - 失败落库供轮询方查看
            logger.warning("文档导入失败 job=%s: %s", job_id, exc)
            self._finish(job_id, status="failed", error=str(exc))

    def _save_result(self, job_id: str, result: dict) -> None:
        """任务执行中更新进度（status 保持 running，供轮询方看 total/done）。"""
        with SessionLocal() as session:
            job = session.get(Job, job_id)
            if job is None:
                return
            job.result = result
            session.commit()

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

    # ---------- 校对编辑（P2.3） ----------
    # 允许人工编辑的解构字段白名单（QuestionAnalysis 的子集）
    EDITABLE_ANALYSIS_FIELDS = {
        "analysis",
        "answer",
        "tags",
        "knowledge_points",
        "difficulty",
        "mistake_cause",
        "followup_question",
        "question_type",
        "chapter",
        "error_category",
    }

    def _load_editable(self, job_id: str, user_id: int) -> tuple[dict, dict]:
        """载入可编辑任务：仅 success 且未 confirm 的任务允许校对编辑。

        返回 (result, payload)。限定 success 状态也规避了与后台执行线程的
        result 写竞争。
        """
        with SessionLocal() as session:
            job = session.execute(
                select(Job).where(Job.id == job_id, Job.user_id == user_id, Job.type == JOB_TYPE)
            ).scalar_one_or_none()
            if job is None:
                raise LookupError("导入任务不存在")
            if job.status != "success":
                raise ValueError(f"任务尚未完成（当前状态 {job.status}）")
            result = dict(job.result or {})
            if result.get("confirmed"):
                raise ValueError("任务已确认入库，不能再编辑")
            return result, dict(job.payload or {})

    @staticmethod
    def _segment_at(result: dict, index: int) -> dict:
        segments = result.get("segments", [])
        if not 0 <= index < len(segments):
            raise IndexError(f"题序号越界: {index}（共 {len(segments)} 题）")
        return segments[index]

    def _save_edit(self, job_id: str, result: dict, action: str, index: int, detail: str) -> None:
        """编辑后落库：同步 total/done 并追加操作留痕（edit_log）。"""
        total = len(result.get("segments", []))
        result["total"] = total
        result["done"] = total
        result.setdefault("edit_log", []).append(
            {
                "at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "action": action,
                "index": index,
                "detail": detail,
            }
        )
        self._save_result(job_id, result)

    def update_segment(
        self,
        job_id: str,
        user_id: int,
        index: int,
        *,
        text: str | None = None,
        analysis_updates: dict | None = None,
    ) -> dict:
        """编辑题干与解构字段（白名单内逐字段覆盖，可手工补齐解构失败的题）。

        人工编辑即视为已校对：清除 needs_review 与 error。
        """
        result, _ = self._load_editable(job_id, user_id)
        entry = self._segment_at(result, index)
        if text is not None and text.strip():
            entry["text"] = text.strip()
        if analysis_updates:
            analysis = dict(entry.get("analysis") or {})
            for key, value in analysis_updates.items():
                if key in self.EDITABLE_ANALYSIS_FIELDS:
                    analysis[key] = value
            entry["analysis"] = analysis
        entry["error"] = None
        entry["needs_review"] = False
        self._save_edit(job_id, result, "update", index, f"编辑题 {entry.get('number')}")
        logger.info("校对编辑 job=%s index=%s", job_id, index)
        return entry

    def merge_segments(self, job_id: str, user_id: int, index: int) -> dict:
        """合并第 index 题与下一题（题干拼接，解构结果优先保留前者）。

        结构变动后标 needs_review，建议人工确认或重新解析。
        """
        result, _ = self._load_editable(job_id, user_id)
        segments = result.get("segments", [])
        if not 0 <= index < len(segments) - 1:
            raise IndexError(f"无法合并: {index}（需要存在下一题，共 {len(segments)} 题）")
        first, second = segments[index], segments[index + 1]
        merged = {
            "number": first.get("number"),
            "text": f"{first['text'].rstrip()}\n\n{second['text'].lstrip()}",
            "page_start": first["page_start"],
            "page_end": second["page_end"],
            "needs_review": True,
            "continued": first.get("continued", False),
            "source": first.get("source", "merged"),
            "analysis": first.get("analysis") or second.get("analysis"),
            "error": None,
        }
        segments[index : index + 2] = [merged]
        self._save_edit(
            job_id, result, "merge", index,
            f"合并题 {first.get('number')} 与 {second.get('number')}",
        )
        logger.info("校对合并 job=%s index=%s", job_id, index)
        return merged

    def split_segment(self, job_id: str, user_id: int, index: int, split_at: int) -> list[dict]:
        """按字符偏移把一题拆成两题（后半题需重新解构，两题均标 needs_review）。"""
        result, _ = self._load_editable(job_id, user_id)
        entry = self._segment_at(result, index)
        text = entry["text"]
        if not 0 < split_at < len(text):
            raise ValueError(f"拆分位置必须在 1 到 {len(text) - 1} 之间")
        first = {
            **entry,
            "text": text[:split_at].rstrip(),
            "needs_review": True,
            "error": None,
        }
        second = {
            **entry,
            "number": f"{entry.get('number')}-拆",
            "text": text[split_at:].lstrip(),
            "analysis": None,
            "error": None,
            "needs_review": True,
            "continued": False,
        }
        result["segments"][index : index + 1] = [first, second]
        self._save_edit(
            job_id, result, "split", index, f"拆分题 {entry.get('number')} 于第 {split_at} 字符"
        )
        logger.info("校对拆分 job=%s index=%s at=%s", job_id, index, split_at)
        return [first, second]

    def delete_segment(self, job_id: str, user_id: int, index: int) -> None:
        """删除一题（误切的非题目内容、重复题等）。"""
        result, _ = self._load_editable(job_id, user_id)
        entry = self._segment_at(result, index)
        del result["segments"][index]
        self._save_edit(job_id, result, "delete", index, f"删除题 {entry.get('number')}")
        logger.info("校对删除 job=%s index=%s", job_id, index)

    def reanalyze_segment(self, job_id: str, user_id: int, index: int) -> dict:
        """单题重新解构：调既有 AI 文本解析管线，成功后清除 needs_review。

        失败时错误落回 entry（needs_review 保持）并抛 RuntimeError，供界面提示。
        """
        from backend.services.ai import get_ai_service
        from backend.services.ai.base import AnalysisContext

        result, payload = self._load_editable(job_id, user_id)
        entry = self._segment_at(result, index)
        ai = get_ai_service(self.settings)
        context = AnalysisContext(
            subject=payload.get("subject") or "math",
            grade=payload.get("grade"),
            region=payload.get("region"),
            textbook_version=payload.get("textbook_version"),
        )
        try:
            analysis = ai.analyze_text(entry["text"][:4000], context=context)
        except Exception as exc:  # noqa: BLE001 - 错误落库供界面展示
            entry["error"] = str(exc)
            entry["needs_review"] = True
            self._save_edit(job_id, result, "reanalyze", index, f"重解析失败: {exc}")
            raise RuntimeError(f"重新解析失败: {exc}") from exc
        entry["analysis"] = analysis.model_dump(mode="json")
        entry["error"] = None
        entry["needs_review"] = False
        self._save_edit(job_id, result, "reanalyze", index, f"重解析题 {entry.get('number')}")
        logger.info("校对重解析 job=%s index=%s", job_id, index)
        return entry

    # ---------- 确认入库 ----------
    def confirm_import(self, job_id: str, user_id: int) -> dict:
        """确认入库：把解构成功的 segments 批量写入错题本。

        source="document"、source_doc="文档名#页码"（跨页题为 #起-止），
        K12 上下文取任务 payload，结构化字段取逐题解构结果。
        返回 imported（成功数）与 skipped（跳过题号及原因：未解构/入库失败）。
        幂等：已确认的任务直接返回既有结果，不重复入库。
        """
        from backend.services.question_service import QuestionService

        with SessionLocal() as session:
            job = session.execute(
                select(Job).where(Job.id == job_id, Job.user_id == user_id, Job.type == JOB_TYPE)
            ).scalar_one_or_none()
            if job is None:
                raise LookupError("导入任务不存在")
            if job.status != "success":
                raise ValueError(f"任务尚未完成（当前状态 {job.status}）")
            result = dict(job.result or {})
            payload = dict(job.payload or {})
            if result.get("confirmed"):
                return {
                    "imported": result.get("imported", 0),
                    "skipped": result.get("skipped", []),
                    "already_confirmed": True,
                }

        service = QuestionService(session_factory=SessionLocal)
        filename = payload.get("filename") or "document"
        imported = 0
        skipped: list[dict] = []
        for entry in result.get("segments", []):
            analysis = entry.get("analysis")
            if not analysis:
                skipped.append(
                    {
                        "number": entry.get("number"),
                        "reason": entry.get("error") or "未完成结构化解构",
                    }
                )
                continue
            page_start, page_end = entry["page_start"], entry["page_end"]
            page_label = str(page_start) if page_start == page_end else f"{page_start}-{page_end}"
            try:
                service.create_manual_question(
                    user_id,
                    content_markdown=f"### 题目\n{entry['text']}\n\n### 解析\n{analysis['analysis']}",
                    answer=analysis.get("answer", ""),
                    tags=list(analysis.get("tags") or []),
                    knowledge_points=list(analysis.get("knowledge_points") or []),
                    source="document",
                    difficulty=analysis.get("difficulty") or "medium",
                    followup_question=analysis.get("followup_question") or "",
                    subject=payload.get("subject") or "math",
                    grade=payload.get("grade"),
                    region=payload.get("region"),
                    textbook_version=payload.get("textbook_version"),
                    question_type=analysis.get("question_type") or None,
                    chapter=analysis.get("chapter") or None,
                    error_category=analysis.get("error_category") or None,
                    source_doc=f"{filename}#{page_label}",
                )
                imported += 1
            except Exception as exc:  # noqa: BLE001 - 单题失败不阻断整卷入库
                logger.warning("确认入库单题失败 job=%s 题%s: %s", job_id, entry.get("number"), exc)
                skipped.append({"number": entry.get("number"), "reason": f"入库失败: {exc}"})

        result["confirmed"] = True
        result["imported"] = imported
        result["skipped"] = skipped
        self._save_result(job_id, result)
        logger.info(
            "确认入库完成 job=%s user=%s imported=%s skipped=%s",
            job_id, user_id, imported, len(skipped),
        )
        return {"imported": imported, "skipped": skipped, "already_confirmed": False}

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
