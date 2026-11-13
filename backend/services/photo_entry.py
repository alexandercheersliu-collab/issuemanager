"""共享拍照录入：持久化切题任务、候选确认与后台入库。"""
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
from backend.services.ai.base import AnalysisContext
from backend.services.entry_types import DEFAULT_IMPORT_CONTEXT, EntryResult, PhotoImage
from backend.utils.logging import get_logger

logger = get_logger("photo_entry")

JOB_TYPE = "photo_entry"
_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


def _initial_result() -> dict:
    return {
        "phase": "planning",
        "candidates": [],
        "fallback_count": 0,
        "total": 0,
        "done": 0,
        "entries": [],
    }


def _summary(text: str, limit: int = 50) -> str:
    clean = " ".join((text or "").split())
    return clean if len(clean) <= limit else clean[:limit] + "…"


def _entry(name: str, result: EntryResult | None, error: str | None) -> dict:
    return {
        "name": name,
        "error": error,
        "question": result.question.model_dump(mode="json") if result else None,
        "analysis": result.analysis.model_dump(mode="json") if result else None,
        "duplicated": bool(result and result.duplicated),
    }


class PhotoEntryService:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    def create_job(
        self,
        user_id: int,
        images: list[PhotoImage],
        *,
        tags: list[str] | None = None,
        hint: str = "",
        context: AnalysisContext | None = None,
    ) -> str:
        if not images:
            raise ValueError("请至少上传一张图片")
        if any(not image.data for image in images):
            raise ValueError("图片内容为空")

        job_id = uuid.uuid4().hex
        job_dir = self.settings.data_dir / "jobs" / JOB_TYPE / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        stored_images = []
        for index, image in enumerate(images):
            suffix = Path(image.name).suffix.lower()
            if suffix not in _IMAGE_SUFFIXES:
                suffix = ".bin"
            path = job_dir / f"{index:03d}{suffix}"
            path.write_bytes(image.data)
            stored_images.append(
                {
                    "name": image.name,
                    "mime_type": image.mime_type or "image/jpeg",
                    "path": str(path),
                }
            )

        selected_context = context or DEFAULT_IMPORT_CONTEXT
        payload = {
            "images": stored_images,
            "tags": list(tags or []),
            "hint": hint,
            "subject": selected_context.subject or "math",
            "grade": selected_context.grade,
            "region": selected_context.region,
            "textbook_version": selected_context.textbook_version,
            "candidates": [],
            "fallbacks": [],
            "selected_candidate_ids": [],
        }
        with SessionLocal() as session:
            session.add(
                Job(
                    id=job_id,
                    user_id=user_id,
                    type=JOB_TYPE,
                    status="pending",
                    payload=payload,
                    result=_initial_result(),
                )
            )
            session.commit()

        threading.Thread(
            target=self._run_planning, args=(job_id, user_id), daemon=True
        ).start()
        logger.info("拍照录入任务已提交 job=%s user=%s", job_id, user_id)
        return job_id

    @staticmethod
    def _question_service():
        from backend.services.question_service import QuestionService

        return QuestionService(session_factory=SessionLocal)

    def _run_planning(self, job_id: str, user_id: int) -> None:
        result = _initial_result()
        try:
            with SessionLocal() as session:
                job = session.get(Job, job_id)
                if job is None or job.user_id != user_id or job.type != JOB_TYPE:
                    return
                payload = dict(job.payload or {})
                job.status = "running"
                session.commit()

            service = self._question_service()

            def _segment(image_index: int):
                image = payload["images"][image_index]
                data = Path(image["path"]).read_bytes()
                try:
                    return image_index, service.ai.segment_page(data, image["mime_type"])
                except Exception as exc:  # noqa: BLE001 - 单图切题失败回退整图
                    logger.info("切题失败，回退整图 job=%s image=%s: %s", job_id, image["name"], exc)
                    return image_index, None

            segment_results: list[list[dict] | None] = [None] * len(payload["images"])
            workers = max(1, self.settings.import_concurrency)
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [
                    pool.submit(_segment, index) for index in range(len(payload["images"]))
                ]
                for future in as_completed(futures):
                    image_index, segments = future.result()
                    segment_results[image_index] = segments

            candidates: list[dict] = []
            fallbacks: list[dict] = []
            for image_index, (image, segments) in enumerate(
                zip(payload["images"], segment_results, strict=True)
            ):
                if isinstance(segments, list) and len(segments) >= 2:
                    for segment_index, segment in enumerate(segments):
                        text = str(segment.get("text", "") or "").strip()
                        candidate = {
                            "id": f"{image_index}:{segment_index}",
                            "image": image["name"],
                            "number": str(segment.get("number", "")).strip() or "?",
                            "text": text,
                            "summary": _summary(text),
                            "likely_wrong": bool(segment.get("likely_wrong", False)),
                            "figure_bboxes": list(segment.get("figure_bboxes") or []),
                        }
                        candidates.append({**candidate, "image_path": image["path"]})
                else:
                    fallbacks.append(
                        {
                            **image,
                            "image_hash": hashlib.sha256(
                                Path(image["path"]).read_bytes()
                            ).hexdigest(),
                            "segments": segments if isinstance(segments, list) else [],
                        }
                    )

            payload["candidates"] = candidates
            payload["fallbacks"] = fallbacks
            result.update(
                candidates=[
                    {key: value for key, value in candidate.items() if key != "image_path"}
                    for candidate in candidates
                ],
                fallback_count=len(fallbacks),
            )
            if candidates:
                result["phase"] = "review"
                self._save_state(job_id, status="success", payload=payload, result=result)
                return

            result["phase"] = "importing"
            result["total"] = len(fallbacks)
            self._save_state(job_id, status="running", payload=payload, result=result)
            self._run_import(job_id, user_id)
        except Exception as exc:  # noqa: BLE001 - 规划失败仅终止当前任务
            logger.warning("拍照切题规划失败 job=%s: %s", job_id, exc)
            self._fail(job_id, result, exc)

    def confirm(self, job_id: str, user_id: int, candidate_ids: list[str]) -> None:
        from sqlalchemy import update

        with SessionLocal() as session:
            job = session.execute(
                select(Job).where(Job.id == job_id, Job.user_id == user_id, Job.type == JOB_TYPE)
            ).scalar_one_or_none()
            if job is None:
                raise LookupError("拍照录入任务不存在")
            payload = dict(job.payload or {})
            result = dict(job.result or {})
            phase = result.get("phase")
            if phase in {"importing", "done"}:
                return
            if phase != "review":
                raise ValueError(f"任务当前不可确认（{phase}）")

            candidates = list(payload.get("candidates") or [])
            fallbacks = list(payload.get("fallbacks") or [])
            by_id = {candidate["id"]: candidate for candidate in candidates}
            if any(not isinstance(item, str) or item not in by_id for item in candidate_ids):
                raise ValueError("候选 ID 无效")
            selected_ids = set(candidate_ids)
            selected = [candidate for candidate in candidates if candidate["id"] in selected_ids]
            if not selected and not fallbacks:
                raise ValueError("请至少勾选一道题")

            payload["selected_candidate_ids"] = [candidate["id"] for candidate in selected]
            result.update(
                phase="importing",
                total=len(selected) + len(fallbacks),
                done=0,
                entries=[],
            )
            session.rollback()
            transition = session.execute(
                update(Job)
                .where(
                    Job.id == job_id,
                    Job.user_id == user_id,
                    Job.type == JOB_TYPE,
                    Job.status == "success",
                    Job.result["phase"].as_string() == "review",
                )
                .values(status="running", payload=payload, result=result, error=None)
                .execution_options(synchronize_session=False)
            )
            if transition.rowcount != 1:
                session.rollback()
                return
            session.commit()

        threading.Thread(
            target=self._run_import, args=(job_id, user_id), daemon=True
        ).start()

    def _run_import(self, job_id: str, user_id: int) -> None:
        try:
            with SessionLocal() as session:
                job = session.get(Job, job_id)
                if job is None or job.user_id != user_id or job.type != JOB_TYPE:
                    return
                payload = dict(job.payload or {})
                result = dict(job.result or {})
                if result.get("phase") != "importing":
                    return
                job.status = "running"
                session.commit()

            service = self._question_service()
            selected_ids = set(payload.get("selected_candidate_ids") or [])
            selected = [
                candidate
                for candidate in payload.get("candidates") or []
                if candidate["id"] in selected_ids
            ]
            items = [
                *[("candidate", candidate) for candidate in selected],
                *[("fallback", fallback) for fallback in payload.get("fallbacks") or []],
            ]
            path_cache: dict[str, str] = {}
            path_lock = threading.Lock()
            fallback_groups: dict[str, list[int]] = {}
            for index, (kind, item) in enumerate(items):
                if kind == "fallback":
                    fallback_groups.setdefault(item["image_hash"], []).append(index)
            fallback_positions = {
                index: position
                for group in fallback_groups.values()
                for position, index in enumerate(group)
            }
            fallback_conditions = {
                image_hash: threading.Condition() for image_hash in fallback_groups
            }
            fallback_turns = {image_hash: 0 for image_hash in fallback_groups}

            def _import(index: int):
                kind, item = items[index]
                if kind == "candidate":
                    name = f"{item['image']} · 第 {item['number']} 题"
                    try:
                        source_path = item["image_path"]
                        with path_lock:
                            if source_path not in path_cache:
                                path_cache[source_path] = str(
                                    service._persist_image(
                                        user_id, Path(source_path).read_bytes()
                                    )
                                )
                        out, analysis = service.analyze_text_and_save(
                            user_id,
                            item["text"],
                            user_tags=list(payload.get("tags") or []),
                            hint=payload.get("hint", ""),
                            image_path=path_cache[source_path],
                            segment=item,
                            subject=payload.get("subject") or "math",
                            grade=payload.get("grade"),
                            region=payload.get("region"),
                            textbook_version=payload.get("textbook_version"),
                        )
                        return index, _entry(
                            name, EntryResult(out, analysis, duplicated=False), None
                        )
                    except Exception as exc:  # noqa: BLE001 - 单题失败不影响其余
                        return index, _entry(name, None, str(exc))

                name = item["name"]
                image_hash = item["image_hash"]
                condition = fallback_conditions[image_hash]
                try:
                    with condition:
                        condition.wait_for(
                            lambda: fallback_turns[image_hash]
                            == fallback_positions[index]
                        )
                        try:
                            entry = service.analyze_and_save_dedup(
                                user_id,
                                Path(item["path"]).read_bytes(),
                                mime_type=item.get("mime_type") or "image/jpeg",
                                user_tags=list(payload.get("tags") or []),
                                hint=payload.get("hint", ""),
                                segments=list(item.get("segments") or []),
                                subject=payload.get("subject") or "math",
                                grade=payload.get("grade"),
                                region=payload.get("region"),
                                textbook_version=payload.get("textbook_version"),
                            )
                        finally:
                            fallback_turns[image_hash] += 1
                            condition.notify_all()
                    return index, _entry(name, entry, None)
                except Exception as exc:  # noqa: BLE001 - 单图失败不影响其余
                    return index, _entry(name, None, str(exc))

            entries: list[dict] = [{} for _ in items]
            workers = max(1, self.settings.import_concurrency)
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(_import, index) for index in range(len(items))]
                for future in as_completed(futures):
                    index, entry = future.result()
                    entries[index] = entry
                    result["done"] += 1
                    self._save_result(job_id, result)

            result["entries"] = entries
            result["phase"] = "done"
            self._save_state(job_id, status="success", result=result, finished=True)
        except Exception as exc:  # noqa: BLE001 - 任务级失败持久化供轮询
            logger.warning("拍照入库失败 job=%s: %s", job_id, exc)
            self._fail(job_id, result, exc)

    def get_job(self, job_id: str, user_id: int) -> dict | None:
        with SessionLocal() as session:
            job = session.execute(
                select(Job).where(Job.id == job_id, Job.user_id == user_id, Job.type == JOB_TYPE)
            ).scalar_one_or_none()
            if job is None:
                return None
            result = dict(job.result or {})
            phase = result.get("phase") or {
                "pending": "planning",
                "running": "planning",
                "success": "done",
                "failed": "failed",
            }.get(job.status, "failed")
            return {
                "job_id": job.id,
                "phase": phase,
                "candidates": list(result.get("candidates") or []),
                "fallback_count": int(result.get("fallback_count") or 0),
                "total": int(result.get("total") or 0),
                "done": int(result.get("done") or 0),
                "entries": list(result.get("entries") or []),
                "error": job.error or result.get("error"),
            }

    @staticmethod
    def _save_result(job_id: str, result: dict) -> None:
        with SessionLocal() as session:
            job = session.get(Job, job_id)
            if job is None:
                return
            job.result = result
            session.commit()

    @staticmethod
    def _save_state(
        job_id: str,
        *,
        status: str,
        result: dict,
        payload: dict | None = None,
        finished: bool = False,
    ) -> None:
        with SessionLocal() as session:
            job = session.get(Job, job_id)
            if job is None:
                return
            job.status = status
            job.result = result
            if payload is not None:
                job.payload = payload
            job.error = None
            if finished:
                job.finished_at = dt.datetime.now(dt.timezone.utc)
            session.commit()

    @staticmethod
    def _fail(job_id: str, result: dict, exc: Exception) -> None:
        result["phase"] = "failed"
        result["error"] = str(exc)
        with SessionLocal() as session:
            job = session.get(Job, job_id)
            if job is None:
                return
            job.status = "failed"
            job.result = result
            job.error = str(exc)
            job.finished_at = dt.datetime.now(dt.timezone.utc)
            session.commit()


__all__ = ["DEFAULT_IMPORT_CONTEXT", "PhotoEntryService", "PhotoImage"]
