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
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from sqlalchemy import select

from backend.config import Settings, get_settings
from backend.database import SessionLocal
from backend.models.orm import Job
from backend.services.ai.telemetry import record_reuse_savings
from backend.services.document import (
    IMAGE_SUFFIXES,
    SUPPORTED_SUFFIXES,
    parse_document,
    parse_images,
)
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
        extra_files: list[tuple[bytes, str]] | None = None,
        subject: str = "math",
        grade: int | None = None,
        region: str | None = None,
        textbook_version: str | None = None,
        match_context: bool = False,
    ) -> tuple[str, bool]:
        """接收文档字节，落 DOC_UPLOAD_PATH 并创建异步导入任务。

        extra_files：整卷拍照的其余照片（(字节, 文件名)，按上传顺序，每张 = 一页）；
        仅图片格式支持多文件，PDF/DOCX 恒为单文件。
        返回 (job_id, duplicated)：同一用户重复上传同一文档（SHA-256 相同，
        多文件按序拼接计算）且已有非空任务未失败时，直接返回既有任务，不重复解析；
        match_context 时仅复用 K12 上下文一致的任务。历史空结果任务不复用。
        """
        files = [(data, filename), *(extra_files or [])]
        for file_data, file_name in files:
            file_suffix = Path(file_name).suffix.lower()
            if file_suffix not in SUPPORTED_SUFFIXES:
                raise ValueError(
                    f"不支持的文档格式: {file_suffix}（支持 {sorted(SUPPORTED_SUFFIXES)}）"
                )
            if not file_data:
                raise ValueError("文档内容为空")
        if len(files) > 1 and any(
            Path(name).suffix.lower() not in IMAGE_SUFFIXES for _, name in files
        ):
            raise ValueError("多文件仅支持图片格式（jpg/jpeg/png/webp，每张 = 试卷一页）")

        hasher = hashlib.sha256()
        for file_data, _ in files:
            hasher.update(file_data)
        doc_hash = hasher.hexdigest()
        context = {
            "subject": subject or "math",
            "grade": grade,
            "region": region,
            "textbook_version": textbook_version,
        }
        existing = self._find_active_job(
            user_id, doc_hash, context=context if match_context else None
        )
        if existing is not None:
            logger.info("重复文档 job=%s user=%s，直接复用", existing, user_id)
            return existing, True

        upload_dir = self.settings.doc_upload_path
        assert upload_dir is not None  # model_validator 保证非 None
        upload_dir.mkdir(parents=True, exist_ok=True)
        saved_paths: list[Path] = []
        for index, (file_data, file_name) in enumerate(files):
            file_suffix = Path(file_name).suffix.lower()
            stem = doc_hash if index == 0 else f"{doc_hash}-p{index + 1}"
            saved = upload_dir / f"{stem}{file_suffix}"
            saved.write_bytes(file_data)
            saved_paths.append(saved)
        doc_path = saved_paths[0]
        is_image_job = Path(filename).suffix.lower() in IMAGE_SUFFIXES

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
                        # 图片任务：全部照片路径（含首张），按上传顺序成页
                        "image_paths": [str(p) for p in saved_paths] if is_image_job else None,
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

    def _find_active_job(
        self, user_id: int, doc_hash: str, *, context: dict | None = None
    ) -> str | None:
        """按 SHA-256 找同用户可复用任务；指定上下文时必须完全一致。"""
        with SessionLocal() as session:
            jobs = session.execute(
                select(Job).where(Job.user_id == user_id, Job.type == JOB_TYPE)
            ).scalars()
            for job in jobs:
                payload = job.payload or {}
                if payload.get("doc_hash") != doc_hash or job.status == "failed":
                    continue
                if job.status == "success" and not (job.result or {}).get("total"):
                    continue
                if context is not None and any(
                    payload.get(key) != value for key, value in context.items()
                ):
                    continue
                return job.id
        return None

    # ---------- 异步执行 ----------
    def _run_import(self, job_id: str, user_id: int) -> None:
        """解析与切题后立即匹配；只有新题执行结构化解构和几何重绘。"""
        from backend.services.ai import get_ai_service
        from backend.services.geometry_figures import GeometryFigureService
        from backend.services.question_bank import QuestionBankService
        from backend.services.segment import segment_document

        with SessionLocal() as session:
            job = session.get(Job, job_id)
            if job is None:
                return
            payload = dict(job.payload or {})
            job.status = "running"
            session.commit()

        try:
            bank = QuestionBankService(session_factory=SessionLocal)
            original = None
            if payload.get("doc_hash"):
                try:
                    original = bank.match_original_hash(payload["doc_hash"])
                except Exception as exc:  # noqa: BLE001 - 哈希匹配失败继续正常切题
                    logger.info("题库原图哈希匹配失败 job=%s: %s", job_id, exc)
            if original is not None:
                result = {
                    "pages": 1,
                    "scanned_pages": 0,
                    "page_images": [payload["doc_path"]],
                    "total": 1,
                    "done": 1,
                    "confirmed": False,
                    "segments": [
                        {
                            "number": "1",
                            "text": original.stem_markdown,
                            "page_start": 1,
                            "page_end": 1,
                            "needs_review": False,
                            "continued": False,
                            "source": "original_hash",
                            "figure_paths": [],
                            "figure_specs": [],
                            "figure_bboxes": [],
                            "analysis": None,
                            "error": None,
                            "match_status": "exact",
                            "canonical_id": original.id,
                            "match_candidates": [],
                        }
                    ],
                }
                self._finish(job_id, status="success", result=result)
                return

            work_dir = Path(payload["doc_path"]).parent / "pages" / job_id
            if payload.get("image_paths"):
                pages = parse_images(payload["image_paths"], work_dir)
            else:
                pages = parse_document(payload["doc_path"], work_dir)
            ai = get_ai_service(self.settings)
            context = self._analysis_context(
                payload, bank.list_question_types(payload.get("subject") or "math")
            )
            segments = segment_document(
                pages,
                ai,
                max_workers=self.settings.import_concurrency,
                figures_dir=work_dir / "figures",
                figure_service=None,
                context=context,
            )
            entries: list[dict] = [
                {
                    "number": segment.number,
                    "text": segment.text,
                    "page_start": segment.page_start,
                    "page_end": segment.page_end,
                    "needs_review": segment.needs_review,
                    "continued": segment.continued,
                    "source": segment.source,
                    "figure_paths": list(segment.figure_paths),
                    "figure_specs": list(segment.figure_specs),
                    "figure_bboxes": list(segment.figure_bboxes),
                    "figure_pages": list(segment.figure_pages),
                    "analysis": None,
                    "error": None,
                }
                for segment in segments
            ]
            result: dict = {
                "pages": len(pages),
                "scanned_pages": sum(1 for p in pages if p.is_scanned),
                "page_images": [
                    str(p.image_path) if p.image_path is not None else None for p in pages
                ],
                "total": len(segments),
                "done": 0,
                "segments": entries,
                "confirmed": False,
            }
            self._save_result(job_id, result)

            for entry in entries:
                try:
                    self._refresh_match(entry, context, bank)
                except Exception as exc:  # noqa: BLE001 - 单题匹配失败按新题处理
                    logger.info("整卷题库匹配失败，按新题处理 job=%s: %s", job_id, exc)
                    entry.update(match_status="new", canonical_id=None, match_candidates=[])
            new_indices = [
                index
                for index, entry in enumerate(entries)
                if entry.get("match_status") == "new"
            ]
            result["done"] = len(entries) - len(new_indices)
            self._save_result(job_id, result)
            figure_service = GeometryFigureService.from_settings(ai, self.settings)

            def _analyze(index: int):
                try:
                    analysis, figures = self._analyze_and_redraw(
                        entries[index],
                        ai=ai,
                        figure_service=figure_service,
                        context=context,
                        page_images=result["page_images"],
                        figures_dir=work_dir / "figures",
                    )
                    return index, analysis, figures, None
                except Exception as exc:  # noqa: BLE001 - 单题失败不阻断整卷
                    return index, None, None, exc

            workers = max(1, self.settings.import_concurrency)
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(_analyze, i) for i in new_indices]
                for future in as_completed(futures):
                    index, analysis, figures, exc = future.result()
                    entry = entries[index]
                    if exc is not None:
                        logger.warning("逐题解构失败 job=%s 题%s: %s", job_id, entry["number"], exc)
                        entry["error"] = str(exc)
                        entry["needs_review"] = True
                    else:
                        self._store_analysis(entry, analysis, figures)
                    result["done"] += 1
                    self._save_result(job_id, result)
            self._finish(job_id, status="success", result=result)
        except Exception as exc:  # noqa: BLE001 - 失败落库供轮询方查看
            logger.warning("文档导入失败 job=%s: %s", job_id, exc)
            self._finish(job_id, status="failed", error=str(exc))

    @staticmethod
    def _analyze_and_redraw(entry: dict, *, ai, figure_service, context, page_images, figures_dir):
        analysis = ai.analyze_text(entry["text"][:4000], context=context)
        figures = None
        boxes = list(entry.get("figure_bboxes") or [])
        page_start = entry.get("page_start")
        source_pages = list(entry.get("figure_pages") or [])
        if len(source_pages) < len(boxes):
            source_pages.extend([page_start] * (len(boxes) - len(source_pages)))
        groups: dict[int, list[tuple[int, object]]] = {}
        for index, (box, page) in enumerate(zip(boxes, source_pages, strict=True)):
            groups.setdefault(page, []).append((index, box))
        specs_by_index: dict[int, dict] = {}
        needs_review = False
        if boxes:
            for page, group in groups.items():
                image_path = (
                    page_images[page - 1]
                    if isinstance(page, int) and 0 < page <= len(page_images)
                    else None
                )
                if not image_path:
                    continue
                outcome = figure_service.redraw_segment(
                    image_path=Path(image_path),
                    number=entry.get("number") or "1",
                    text=entry["text"],
                    figure_bboxes=[box for _, box in group],
                    figures_dir=figures_dir / f"page-{page}",
                    context=context,
                )
                needs_review = needs_review or outcome.needs_review
                for (index, _), spec in zip(group, outcome.figure_specs, strict=False):
                    specs_by_index[index] = spec
            if specs_by_index:
                from backend.services.geometry_figures import FigureOutcome

                specs = [specs_by_index[index] for index in sorted(specs_by_index)]
                figures = FigureOutcome(
                    [
                        path
                        for spec in specs
                        if (path := DocumentService._effective_figure_path(spec))
                    ],
                    specs,
                    needs_review,
                )
        return analysis, figures

    @staticmethod
    def _store_analysis(entry: dict, analysis, figures) -> None:
        entry["analysis"] = analysis.model_dump(mode="json")
        entry["error"] = None
        if figures is not None:
            entry["figure_paths"] = list(figures.figure_paths)
            entry["figure_specs"] = list(figures.figure_specs)
            entry["needs_review"] = figures.needs_review

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
        """编辑题干与解构字段；题干变化后要求重新进行图文对比。"""
        result, payload = self._load_editable(job_id, user_id)
        entry = self._segment_at(result, index)
        text_changed = False
        if text is not None and text.strip():
            updated_text = text.strip()
            text_changed = updated_text != entry.get("text")
            entry["text"] = updated_text
        if analysis_updates:
            analysis = dict(entry.get("analysis") or {})
            for key, value in analysis_updates.items():
                if key in self.EDITABLE_ANALYSIS_FIELDS:
                    analysis[key] = value
            entry["analysis"] = analysis
        if text_changed:
            specs = []
            for current in entry.get("figure_specs") or []:
                spec = dict(current)
                if spec.get("validation_status") in {"ai_passed", "manual_passed"}:
                    spec["validation_status"] = "fallback"
                    spec["validation_issues"] = [
                        *(spec.get("validation_issues") or []),
                        "题干已修改，需重新进行图文对比",
                    ]
                specs.append(spec)
            entry["figure_specs"] = specs
            from backend.services.question_bank import QuestionBankService

            bank = QuestionBankService(session_factory=SessionLocal)
            context = self._analysis_context(
                payload, bank.list_question_types(payload.get("subject") or "math")
            )
            try:
                self._refresh_match(entry, context, bank)
            except Exception as exc:  # noqa: BLE001 - 编辑不因匹配失败而回滚
                logger.info("编辑后题库匹配失败，按新题处理: %s", exc)
                entry.update(match_status="new", canonical_id=None, match_candidates=[])
        entry["error"] = None
        entry["needs_review"] = any(
            spec.get("validation_status") == "fallback"
            for spec in entry.get("figure_specs") or []
        )
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
            "figure_paths": [
                *(first.get("figure_paths") or []),
                *(second.get("figure_paths") or []),
            ],
            "figure_specs": [
                *(first.get("figure_specs") or []),
                *(second.get("figure_specs") or []),
            ],
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
        new_match = {"match_status": "new", "canonical_id": None, "match_candidates": []}
        first = {
            **entry,
            **new_match,
            "text": text[:split_at].rstrip(),
            "needs_review": True,
            "error": None,
        }
        second = {
            **entry,
            **new_match,
            "number": f"{entry.get('number')}-拆",
            "text": text[split_at:].lstrip(),
            "analysis": None,
            "error": None,
            "needs_review": True,
            "continued": False,
            "figure_paths": [],
            "figure_specs": [],
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
        """单题重新解构，并为旧任务补齐图形定位与重绘结果。

        失败时错误落回 entry（needs_review 保持）并抛 RuntimeError，供界面提示。
        """
        from backend.services.ai import get_ai_service
        from backend.services.geometry_figures import GeometryFigureService
        from backend.services.question_bank import QuestionBankService

        result, payload = self._load_editable(job_id, user_id)
        entry = self._segment_at(result, index)
        ai = get_ai_service(self.settings)
        bank = QuestionBankService(session_factory=SessionLocal)
        context = self._analysis_context(
            payload, bank.list_question_types(payload.get("subject") or "math")
        )
        figure_needs_review = any(
            spec.get("validation_status") == "fallback"
            for spec in entry.get("figure_specs") or []
        )
        try:
            if "figure_specs" not in entry:
                page_images = result.get("page_images") or []
                page_start = entry.get("page_start")
                if isinstance(page_start, int) and 0 < page_start <= len(page_images):
                    image_path = page_images[page_start - 1]
                    if image_path:
                        located = ai.locate_figures(
                            Path(image_path).read_bytes(),
                            [{"number": entry.get("number"), "text": entry["text"]}],
                            "image/png",
                        )
                        outcome = GeometryFigureService.from_settings(
                            ai, self.settings
                        ).redraw_segment(
                            image_path=Path(image_path),
                            number=entry.get("number"),
                            text=entry["text"],
                            figure_bboxes=located[0] if located else [],
                            figures_dir=Path(image_path).parent / "figures",
                            context=context,
                        )
                        entry["figure_paths"] = list(outcome.figure_paths)
                        entry["figure_specs"] = list(outcome.figure_specs)
                        figure_needs_review = outcome.needs_review
            analysis = ai.analyze_text(entry["text"][:4000], context=context)
        except Exception as exc:  # noqa: BLE001 - 错误落库供界面展示
            entry["error"] = str(exc)
            entry["needs_review"] = True
            self._save_edit(job_id, result, "reanalyze", index, f"重解析失败: {exc}")
            raise RuntimeError(f"重新解析失败: {exc}") from exc
        entry["analysis"] = analysis.model_dump(mode="json")
        try:
            self._refresh_match(entry, context, bank)
        except Exception as exc:  # noqa: BLE001 - 重解析成功不因匹配失败回滚
            logger.info("重解析后题库匹配失败，按新题处理: %s", exc)
            entry.update(match_status="new", canonical_id=None, match_candidates=[])
        entry["error"] = None
        entry["needs_review"] = figure_needs_review
        self._save_edit(job_id, result, "reanalyze", index, f"重解析题 {entry.get('number')}")
        logger.info("校对重解析 job=%s index=%s", job_id, index)
        return entry

    @staticmethod
    def _figure_spec_at(entry: dict, figure_index: int) -> dict:
        specs = entry.get("figure_specs") or []
        if not 0 <= figure_index < len(specs):
            raise IndexError(f"图序号越界: {figure_index}（共 {len(specs)} 图）")
        return specs[figure_index]

    @staticmethod
    def _effective_figure_path(spec: dict) -> str | None:
        if spec.get("validation_status") == "fallback":
            return spec.get("crop_path") or spec.get("render_path")
        return spec.get("render_path") or spec.get("crop_path")

    @classmethod
    def _refresh_figure_paths(
        cls, entry: dict, *, preserved_paths: dict[int, str | None] | None = None
    ) -> None:
        paths = []
        for index, spec in enumerate(entry.get("figure_specs") or []):
            if (
                spec.get("validation_status") == "fallback"
                and not spec.get("crop_path")
                and preserved_paths is not None
                and index in preserved_paths
            ):
                path = preserved_paths[index]
            else:
                path = cls._effective_figure_path(spec)
            if path:
                paths.append(path)
        entry["figure_paths"] = paths
        entry["needs_review"] = any(
            spec.get("validation_status") == "fallback"
            for spec in entry.get("figure_specs") or []
        )

    def recompile_segment_figure(
        self,
        job_id: str,
        user_id: int,
        index: int,
        figure_index: int,
        *,
        tikz_source: str,
    ) -> dict:
        """只重编译用户提供的 TikZ；任何编译或对比异常均不改动旧图。"""
        from backend.models.schemas import GeometryFigureDraft
        from backend.services.ai import get_ai_service
        from backend.services.geometry_figures import GeometryFigureService

        result, _ = self._load_editable(job_id, user_id)
        entry = self._segment_at(result, index)
        spec = self._figure_spec_at(entry, figure_index)
        page_images = result.get("page_images") or []
        page_start = entry.get("page_start")
        if not isinstance(page_start, int) or not 0 < page_start <= len(page_images):
            raise ValueError("缺少原图参考，无法重新校对图形")
        image_path = page_images[page_start - 1]
        if not image_path:
            raise ValueError("缺少原图参考，无法重新校对图形")
        draft = GeometryFigureDraft(
            kind=spec.get("kind") or "geometry",
            semantic_spec=dict(spec.get("semantic_spec") or {}),
            tikz_source=spec.get("tikz_source") or "",
            assumptions=list(spec.get("assumptions") or []),
        )
        figures = GeometryFigureService.from_settings(get_ai_service(self.settings), self.settings)
        reviewed = figures.review_tikz(
            image_path=Path(image_path),
            text=entry.get("text") or "",
            figure_box=None,
            draft=draft,
            tikz_source=tikz_source,
        )
        if not reviewed.render_path:
            raise RuntimeError("TikZ 编译未产生有效图形，已保留原图")
        entry["figure_specs"][figure_index] = reviewed.model_dump(mode="json")
        self._refresh_figure_paths(
            entry,
            preserved_paths={figure_index: self._effective_figure_path(spec)},
        )
        self._save_edit(
            job_id,
            result,
            "recompile_figure",
            index,
            f"重新编译题 {entry.get('number')} 的第 {figure_index + 1} 张图",
        )
        logger.info("校对图形重编译 job=%s index=%s figure=%s", job_id, index, figure_index)
        return entry

    def accept_segment_figure(
        self, job_id: str, user_id: int, index: int, figure_index: int
    ) -> dict:
        """人工确认已有成功渲染图；裁剪兜底图不能冒充编译成功图。"""
        result, _ = self._load_editable(job_id, user_id)
        entry = self._segment_at(result, index)
        current = self._figure_spec_at(entry, figure_index)
        render_path = current.get("render_path")
        if not render_path:
            raise ValueError("仅成功编译并产生渲染图后才能人工确认")
        spec = dict(current)
        spec["validation_status"] = "manual_passed"
        entry["figure_specs"][figure_index] = spec
        self._refresh_figure_paths(entry)
        self._save_edit(
            job_id,
            result,
            "accept_figure",
            index,
            f"人工确认题 {entry.get('number')} 的第 {figure_index + 1} 张图",
        )
        logger.info("人工确认图形 job=%s index=%s figure=%s", job_id, index, figure_index)
        return entry

    # ---------- 确认入库 ----------
    @staticmethod
    def _analysis_context(payload: dict, question_types: list[str] | None = None):
        from backend.services.ai.base import AnalysisContext

        return AnalysisContext(
            subject=payload.get("subject") or "math",
            grade=payload.get("grade"),
            region=payload.get("region"),
            textbook_version=payload.get("textbook_version"),
            question_types=tuple(question_types or []),
        )

    @staticmethod
    def _refresh_match(entry: dict, context, bank) -> None:
        text = str(entry.get("text") or "").strip()
        exact = bank.match_exact(text) if text else None
        if exact is not None:
            entry.update(
                match_status="exact", canonical_id=exact.id, match_candidates=[]
            )
            return
        similar = bank.find_similar(text, context=context) if text else []
        entry.update(
            match_status="similar" if similar else "new",
            canonical_id=None,
            match_candidates=[
                candidate.model_dump(mode="json") for candidate in similar
            ],
        )

    @classmethod
    def _ensure_matches(cls, result: dict, payload: dict) -> None:
        from backend.services.question_bank import QuestionBankService

        entries = result.get("segments") or []
        if all("match_status" in entry for entry in entries):
            return
        bank = QuestionBankService(session_factory=SessionLocal)
        context = cls._analysis_context(
            payload, bank.list_question_types(payload.get("subject") or "math")
        )
        for entry in entries:
            if "match_status" in entry:
                continue
            try:
                cls._refresh_match(entry, context, bank)
            except Exception as exc:  # noqa: BLE001 - 匹配失败按新题隔离处理
                logger.info("整卷题库匹配失败，按新题处理: %s", exc)
                entry.update(match_status="new", canonical_id=None, match_candidates=[])

    @staticmethod
    def _validate_segment_choices(result: dict, match_choices: dict[int, int | None]) -> None:
        segments = result.get("segments") or []
        for index, canonical_id in match_choices.items():
            if type(index) is not int or not 0 <= index < len(segments):
                raise ValueError("匹配选择题序号无效")
            entry = segments[index]
            if entry.get("match_status") != "similar":
                raise ValueError("仅相似题需要匹配选择")
            offered = {
                item.get("question", {}).get("id")
                for item in entry.get("match_candidates") or []
            }
            if canonical_id is not None and (
                type(canonical_id) is not int or canonical_id not in offered
            ):
                raise ValueError("标准题不属于该题的相似候选集合")
        unresolved = [
            index
            for index, entry in enumerate(segments)
            if entry.get("match_status") == "similar" and index not in match_choices
        ]
        if unresolved:
            raise ValueError("相似题必须明确选择引用候选或作为新题")

    @staticmethod
    def _add_publish_assets(
        canonical_id: int,
        image_path: str | None,
        entry: dict,
        *,
        source_path: str | None = None,
        source_hash: str | None = None,
    ) -> None:
        from backend.models.orm import QuestionAsset
        from backend.services.question_bank import QuestionBankService

        QuestionBankService(session_factory=SessionLocal).register_shared_redraws(
            canonical_id, list(entry.get("figure_specs") or [])
        )
        originals: list[tuple[str, str | None]] = []
        if image_path:
            page = Path(image_path)
            originals.append(
                (
                    image_path,
                    hashlib.sha256(page.read_bytes()).hexdigest() if page.is_file() else None,
                )
            )
        if source_path and source_hash and all(
            path != source_path for path, _ in originals
        ):
            originals.append((source_path, source_hash))
        if not originals:
            return
        with SessionLocal() as session:
            for path, content_hash in originals:
                existing = session.execute(
                    select(QuestionAsset).where(
                        QuestionAsset.canonical_id == canonical_id,
                        QuestionAsset.kind == "teacher_original",
                        QuestionAsset.storage_path == path,
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    continue
                session.add(
                    QuestionAsset(
                        canonical_id=canonical_id,
                        kind="teacher_original",
                        visibility="teacher_only",
                        storage_path=path,
                        content_hash=content_hash,
                        is_preferred=False,
                    )
                )
            session.commit()

    def confirm_import(
        self,
        job_id: str,
        user_id: int,
        *,
        context: dict | None = None,
        publish_to_bank: bool = False,
        question_type_overrides: dict[int, str] | None = None,
        match_choices: dict[int, int | None] | None = None,
    ) -> dict:
        """确认整卷：教师可仅发布标准题，学生归档个人错题或引用。"""
        from sqlalchemy import update

        from backend.models.orm import User
        from backend.services.question_service import QuestionService

        overrides = dict(question_type_overrides or {})
        choices = dict(match_choices or {})
        with SessionLocal() as session:
            job = session.execute(
                select(Job).where(Job.id == job_id, Job.user_id == user_id, Job.type == JOB_TYPE)
            ).scalar_one_or_none()
            if job is None:
                raise LookupError("导入任务不存在")
            actor = session.get(User, user_id)
            if actor is None:
                raise LookupError("用户不存在")
            if publish_to_bank and actor.role != "teacher":
                raise PermissionError("仅教师可发布到共享题库")
            result = dict(job.result or {})
            payload = dict(job.payload or {})
            if result.get("confirmed"):
                return {
                    "imported": result.get("imported", 0),
                    "skipped": result.get("skipped", []),
                    "already_confirmed": True,
                }
            if job.status == "confirming":
                raise ValueError("任务正在确认入库，请稍后重试")
            if job.status != "success":
                raise ValueError(f"任务尚未完成（当前状态 {job.status}）")
            if context is not None:
                for key in ("subject", "grade", "region", "textbook_version"):
                    payload[key] = context.get(key)
                payload["subject"] = payload["subject"] or "math"
            self._ensure_matches(result, payload)
            self._validate_segment_choices(result, choices)
            segments = result.get("segments") or []
            for index, question_type in overrides.items():
                if (
                    type(index) is not int
                    or not 0 <= index < len(segments)
                    or not isinstance(question_type, str)
                    or not question_type.strip()
                ):
                    raise ValueError("题型覆盖的题序号或名称无效")
                overrides[index] = question_type.strip()

            session.expunge(actor)
            session.rollback()
            transition = session.execute(
                update(Job)
                .where(
                    Job.id == job_id,
                    Job.user_id == user_id,
                    Job.type == JOB_TYPE,
                    Job.status == "success",
                    Job.result["confirmed"].as_boolean().is_(False),
                )
                .values(status="confirming", payload=payload)
                .execution_options(synchronize_session=False)
            )
            if transition.rowcount != 1:
                session.rollback()
                current = session.execute(
                    select(Job).where(
                        Job.id == job_id, Job.user_id == user_id, Job.type == JOB_TYPE
                    )
                ).scalar_one_or_none()
                if current is None:
                    raise LookupError("导入任务不存在")
                current_result = dict(current.result or {})
                if current_result.get("confirmed"):
                    return {
                        "imported": current_result.get("imported", 0),
                        "skipped": current_result.get("skipped", []),
                        "already_confirmed": True,
                    }
                if current.status == "confirming":
                    raise ValueError("任务正在确认入库，请稍后重试")
                raise ValueError(f"任务尚未完成（当前状态 {current.status}）")
            session.commit()

        from backend.models.schemas import QuestionAnalysis
        from backend.services.ai import get_ai_service
        from backend.services.ai.telemetry import record_bank_decision
        from backend.services.geometry_figures import GeometryFigureService
        from backend.services.question_bank import QuestionBankService

        service = QuestionService(session_factory=SessionLocal)
        bank = QuestionBankService(session_factory=SessionLocal)
        analysis_context = self._analysis_context(payload)
        filename = payload.get("filename") or "document"
        page_images: list[str | None] = result.get("page_images") or []
        segments = result.get("segments", [])
        for index, entry in enumerate(segments):
            if entry.get("match_status") == "similar":
                record_bank_decision("accepted" if choices[index] is not None else "new")

        explicit_new = [
            index
            for index, entry in enumerate(segments)
            if entry.get("match_status") == "similar"
            and choices.get(index) is None
            and not entry.get("analysis")
        ]
        if explicit_new:
            ai = get_ai_service(self.settings)
            figures = GeometryFigureService.from_settings(ai, self.settings)
            work_dir = Path(payload["doc_path"]).parent / "pages" / job_id / "figures"
            for index in explicit_new:
                entry = segments[index]
                try:
                    analysis, redraw = self._analyze_and_redraw(
                        entry,
                        ai=ai,
                        figure_service=figures,
                        context=analysis_context,
                        page_images=page_images,
                        figures_dir=work_dir,
                    )
                    self._store_analysis(entry, analysis, redraw)
                except Exception as exc:  # noqa: BLE001 - 单题失败不阻断其他题
                    entry["error"] = str(exc)
                    entry["needs_review"] = True

        imported = 0
        skipped: list[dict] = []
        document_evidence_path: str | None = None
        source_document = Path(payload.get("doc_path") or "")
        needs_private_source = source_document.suffix.lower() == ".docx" or any(
            entry.get("source") == "original_hash" for entry in segments
        )
        if not publish_to_bank and needs_private_source and segments:
            try:
                evidence_dir = self.settings.data_dir / "images" / f"u{user_id}"
                evidence_dir.mkdir(parents=True, exist_ok=True)
                suffix = source_document.suffix.lower() or ".bin"
                persisted = evidence_dir / f"{uuid.uuid4().hex}{suffix}"
                persisted.write_bytes(source_document.read_bytes())
                document_evidence_path = str(persisted)
            except OSError as exc:
                message = f"私有原始证据保存失败，请重试: {exc}"
                result["confirmed"] = False
                result["evidence_error"] = message
                self._finish(job_id, status="success", result=result, error=message)
                raise ValueError(message) from exc

        for index, entry in enumerate(segments):
            page_start, page_end = entry["page_start"], entry["page_end"]
            page_label = str(page_start) if page_start == page_end else f"{page_start}-{page_end}"
            image_path = (
                page_images[page_start - 1]
                if isinstance(page_start, int) and 0 < page_start <= len(page_images)
                else None
            )
            evidence_path = document_evidence_path or image_path
            source_doc = f"{filename}#{page_label}"
            match_status = entry.get("match_status") or "new"
            selected_id = (
                entry.get("canonical_id")
                if match_status == "exact"
                else choices.get(index) if match_status == "similar" else None
            )
            analysis = entry.get("analysis")
            if not analysis and selected_id is None:
                skipped.append(
                    {
                        "number": entry.get("number"),
                        "reason": entry.get("error") or "未完成结构化解构",
                    }
                )
                continue
            try:
                if publish_to_bank:
                    if analysis:
                        structured = QuestionAnalysis.model_validate(analysis)
                        canonical = bank.create_or_upgrade(
                            entry["text"],
                            structured,
                            actor=actor,
                            context=analysis_context,
                            question_type=overrides.get(index),
                            canonical_id=selected_id,
                        )
                    else:
                        canonical = bank.get_reusable(selected_id)
                        if index in overrides:
                            existing_analysis = QuestionAnalysis.model_construct(
                                knowledge_points=list(canonical.knowledge_points or []),
                                analysis=canonical.analysis_markdown,
                                answer=canonical.answer,
                                difficulty=canonical.difficulty,
                                tags=list(canonical.tags or []),
                                question_type=canonical.question_type or "",
                                chapter=canonical.chapter or "",
                            )
                            canonical = bank.create_or_upgrade(
                                entry["text"],
                                existing_analysis,
                                actor=actor,
                                context=analysis_context,
                                question_type=overrides[index],
                                canonical_id=selected_id,
                            )
                    self._add_publish_assets(
                        canonical.id,
                        image_path,
                        entry,
                        source_path=payload.get("doc_path"),
                        source_hash=payload.get("doc_hash"),
                    )
                elif match_status == "exact" or selected_id is not None:
                    service.reference_canonical(
                        user_id,
                        selected_id,
                        private_image_path=evidence_path or "",
                        user_tags=list((analysis or {}).get("tags") or []),
                        match_method=(
                            "exact" if match_status == "exact" else "confirmed_similar"
                        ),
                        source="document",
                        source_doc=source_doc,
                        image_hash=payload.get("doc_hash"),
                        context=analysis_context,
                    )
                    savings = {"analyze_text": 1}
                    if (
                        entry.get("figure_bboxes")
                        or entry.get("figure_specs")
                        or entry.get("figure_paths")
                    ) and service._geometry_figures()._tex_available():
                        savings["redraw_geometry"] = 1
                    generations, storage_bytes = bank.shared_asset_savings(selected_id)
                    record_reuse_savings(
                        savings,
                        shared_generations=generations,
                        shared_storage_bytes=storage_bytes,
                    )
                else:
                    service.create_ai_reference(
                        user_id,
                        stem=entry["text"],
                        analysis=QuestionAnalysis.model_validate(analysis),
                        private_image_path=evidence_path or "",
                        context=analysis_context,
                        user_tags=list(analysis.get("tags") or []),
                        source="document",
                        source_doc=source_doc,
                        image_hash=payload.get("doc_hash"),
                        figure_paths=list(entry.get("figure_paths") or []),
                        figure_specs=list(entry.get("figure_specs") or []),
                    )
                imported += 1
            except Exception as exc:  # noqa: BLE001 - 单题失败不阻断整卷入库
                logger.warning("确认入库单题失败 job=%s 题%s: %s", job_id, entry.get("number"), exc)
                skipped.append({"number": entry.get("number"), "reason": f"入库失败: {exc}"})

        result["confirmed"] = True
        result["imported"] = imported
        result["skipped"] = skipped
        result["published_to_bank"] = publish_to_bank
        self._finish(job_id, status="success", result=result)
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
