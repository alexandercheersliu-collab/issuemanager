"""同类题检测路由（P3.2）：发起检测 → 作答判分 → 降级联动 → 历史查询。

路径跨 /questions 与 /detection 两个前缀，故 router 不设公共前缀。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from api.deps import get_current_user, rate_limit
from backend.models.orm import User
from backend.services.question_service import QuestionService

router = APIRouter(tags=["detection"])


def _service() -> QuestionService:
    return QuestionService()


class DetectionAnswer(BaseModel):
    answer: str = Field(min_length=1, max_length=2000)


@router.post(
    "/questions/{question_id}/detection/start",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rate_limit("detection:start", 30))],
)
def start_detection(question_id: int, user: User = Depends(get_current_user)) -> dict:
    """发起同类题检测：约束召回一道同类题（宽松模式三级回落），返回 log_id 与题干。"""
    try:
        return _service().start_detection(question_id, user.id)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.post("/detection/{log_id}/answer")
def submit_detection_answer(
    log_id: int, body: DetectionAnswer, user: User = Depends(get_current_user)
) -> dict:
    """作答判分：返回对错 + 降级/回升联动结果（streak/state/demoted/promoted/mastered）。"""
    try:
        return _service().submit_detection_answer(log_id, user.id, body.answer)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.get("/questions/{question_id}/detection/history")
def detection_history(question_id: int, user: User = Depends(get_current_user)) -> dict:
    """某题检测历史（新→旧）+ 连续通过进度与降级状态。"""
    try:
        logs = _service().detection_history(question_id, user.id)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return {"logs": logs, "count": len(logs)}
