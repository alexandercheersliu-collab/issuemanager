"""错题路由：列表 / AI 录题 / 文本录题 / 详情 / 编辑 / 删除 / 相似题。"""
from __future__ import annotations

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from api.deps import get_current_user, rate_limit
from backend.models.orm import User
from backend.models.schemas import QuestionAnalysis, QuestionOut
from backend.services.export import generate_word_exam
from backend.services.question_service import QuestionService, sanitize_tags

router = APIRouter(prefix="/questions", tags=["questions"])

_ALLOWED_MIME = {"image/jpeg", "image/png", "image/webp"}
_MAX_IMAGE_BYTES = 10 * 1024 * 1024


class QuestionUpdate(BaseModel):
    content_markdown: str | None = None
    answer: str | None = None
    tags: list[str] | None = None
    user_note: str | None = None
    # K12 元数据（None = 不修改）
    subject: str | None = None
    grade: int | None = Field(default=None, ge=1, le=12)
    region: str | None = None
    textbook_version: str | None = None
    question_type: str | None = None
    chapter: str | None = None
    error_category: str | None = None


class TextQuestionInput(BaseModel):
    content_markdown: str
    answer: str = ""
    tags: list[str] = Field(default_factory=list, max_length=8)
    knowledge_points: list[str] = Field(default_factory=list, max_length=8)
    # K12 元数据（可选）
    subject: str = "math"
    grade: int | None = Field(default=None, ge=1, le=12)
    region: str | None = None
    textbook_version: str | None = None
    question_type: str | None = None
    chapter: str | None = None
    error_category: str | None = None


class AnalyzeResult(BaseModel):
    question: QuestionOut
    analysis: QuestionAnalysis


class ImportPayload(BaseModel):
    """备份恢复请求体：format 标识 + questions 列表（逐条字段由服务层校验）。"""

    format: str = Field(min_length=1)
    questions: list[dict] = Field(min_length=1)


class ImportResult(BaseModel):
    imported: int


def _service() -> QuestionService:
    return QuestionService()


@router.get("", response_model=list[QuestionOut])
def list_questions(
    response: Response,
    tag: str | None = None,
    keyword: str | None = None,
    subject: str | None = None,
    grade: int | None = Query(default=None, ge=1, le=12),
    error_category: str | None = None,
    semantic: bool = True,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    user: User = Depends(get_current_user),
) -> list[QuestionOut]:
    """分页列出错题（按创建时间倒序）；X-Total-Count 为过滤后的总数。

    支持 K12 元数据过滤：subject（学科代码）/ grade（1-12）/ error_category（错因枚举）。
    注意：keyword + semantic=true 时走向量召回，结果数与 SQL 计数口径不一致，
    此时不返回 X-Total-Count，客户端应改用「返回数 < limit 即到底」判断分页终止。
    """
    service = _service()
    items = service.list_questions(
        user.id,
        include_others=user.role == "teacher",
        tag=tag,
        keyword=keyword,
        subject=subject,
        grade=grade,
        error_category=error_category,
        semantic=semantic,
        offset=offset,
        limit=limit,
    )
    total = len(items)
    if not (semantic and keyword):
        # 非语义检索时计数口径一致，返回精确总数
        if offset or total == limit:
            total = service.count_for_user(
                user.id,
                include_others=user.role == "teacher",
                tag=tag,
                keyword=keyword,
                subject=subject,
                grade=grade,
                error_category=error_category,
            )
        response.headers["X-Total-Count"] = str(total)
    return items


@router.post(
    "/analyze",
    response_model=AnalyzeResult,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rate_limit("questions:analyze", 20))],
)
async def analyze_question(
    image: UploadFile = File(...),
    tags: str = Form(default=""),
    hint: str = Form(default=""),
    subject: str = Form(default="math"),
    grade: int | None = Form(default=None),
    region: str = Form(default=""),
    textbook_version: str = Form(default=""),
    user: User = Depends(get_current_user),
) -> AnalyzeResult:
    """上传错题图片，返回结构化解析并自动归档（含向量索引）。

    subject/grade/region/textbook_version：K12 录入上下文，参与提示词组装并落库。
    """
    if image.content_type not in _ALLOWED_MIME:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            f"仅支持 {', '.join(sorted(_ALLOWED_MIME))}",
        )
    data = await image.read()
    if not data:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "图片内容为空")
    if len(data) > _MAX_IMAGE_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "图片不能超过 10MB")
    if grade is not None and not 1 <= grade <= 12:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "年级必须在 1-12 之间")

    try:
        saved, analysis = _service().analyze_and_save(
            user.id,
            data,
            mime_type=image.content_type,
            user_tags=sanitize_tags(tags),
            hint=hint,
            subject=subject or "math",
            grade=grade,
            region=region or None,
            textbook_version=textbook_version or None,
        )
    except Exception as exc:  # noqa: BLE001 - 统一转为 502
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"AI 解析失败: {exc}") from exc
    return AnalyzeResult(question=saved, analysis=analysis)


@router.post("/text", response_model=QuestionOut, status_code=status.HTTP_201_CREATED)
def create_text_question(
    payload: TextQuestionInput, user: User = Depends(get_current_user)
) -> QuestionOut:
    """手动录入文本错题（跳过视觉模型，直接归档 + 向量索引）。"""
    if not payload.content_markdown.strip():
        raise HTTPException(422, "题目内容不能为空")
    return _service().create_manual_question(
        user.id,
        content_markdown=payload.content_markdown,
        answer=payload.answer,
        tags=payload.tags,
        knowledge_points=payload.knowledge_points,
        subject=payload.subject,
        grade=payload.grade,
        region=payload.region,
        textbook_version=payload.textbook_version,
        question_type=payload.question_type,
        chapter=payload.chapter,
        error_category=payload.error_category,
    )


@router.post("/import", response_model=ImportResult)
def import_questions(
    payload: ImportPayload, user: User = Depends(get_current_user)
) -> ImportResult:
    """从备份 JSON 恢复错题（按手动错题处理）；缺 format/questions 字段直接 422。"""
    try:
        imported = _service().import_user_data(user.id, payload.model_dump())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return ImportResult(imported=imported)


@router.post(
    "/analyze/async",
    status_code=202,
    dependencies=[Depends(rate_limit("questions:analyze", 20))],
)
async def analyze_question_async(
    image: UploadFile = File(...),
    tags: str = Form(default=""),
    hint: str = Form(default=""),
    subject: str = Form(default="math"),
    grade: int | None = Form(default=None),
    region: str = Form(default=""),
    textbook_version: str = Form(default=""),
    user: User = Depends(get_current_user),
) -> dict:
    """提交异步解析任务，返回 job_id；用 GET /api/jobs/{job_id} 轮询结果。"""
    if image.content_type not in _ALLOWED_MIME:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            f"仅支持 {', '.join(sorted(_ALLOWED_MIME))}",
        )
    data = await image.read()
    if not data:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "图片内容为空")
    if len(data) > _MAX_IMAGE_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "图片不能超过 10MB")
    if grade is not None and not 1 <= grade <= 12:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "年级必须在 1-12 之间")

    from backend.services.job_service import JobService

    job_id = JobService().submit_analyze(
        user.id,
        data,
        filename=image.filename or "upload.jpg",
        mime_type=image.content_type,
        tags=sanitize_tags(tags),
        hint=hint,
        subject=subject or "math",
        grade=grade,
        region=region or None,
        textbook_version=textbook_version or None,
    )
    return {"job_id": job_id, "status": "pending"}


@router.get("/export")
def export_questions(user: User = Depends(get_current_user)) -> dict:
    """导出当前用户全部错题的 JSON 备份。"""
    return _service().export_user_data(user.id)


@router.get("/export/csv")
def export_questions_csv(user: User = Depends(get_current_user)) -> Response:
    """导出当前用户全部错题为 CSV（Excel 友好，UTF-8 BOM）。"""
    csv_text = _service().export_user_csv(user.id)
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="mathmaster_questions.csv"'},
    )


@router.get("/export/docx")
def export_word_exam(
    tag: str | None = None,
    keyword: str | None = None,
    user: User = Depends(get_current_user),
) -> Response:
    """按当前筛选（可选 tag/keyword）导出可打印的 Word 复习卷。"""
    questions = _service().list_questions(
        user.id, include_others=user.role == "teacher", tag=tag, keyword=keyword, semantic=False
    )
    if not questions:
        raise HTTPException(404, "没有可导出的错题")
    stream = generate_word_exam(questions, "错题复习卷")
    return Response(
        content=stream.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": 'attachment; filename="mathmaster_exam.docx"'},
    )


@router.get("/{question_id}/image")
def question_image(question_id: int, user: User = Depends(get_current_user)) -> FileResponse:
    """错题原图（移动端/第三方客户端按 id 拉取，鉴权后不落公开目录）。"""
    import os

    question = _service().get_question(question_id, user.id)
    if (
        question is None
        or not question.image_path
        or not os.path.exists(question.image_path)
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "图片不存在")
    return FileResponse(question.image_path)


@router.get("/{question_id}", response_model=QuestionOut)
def get_question(question_id: int, user: User = Depends(get_current_user)) -> QuestionOut:
    question = _service().get_question(question_id, user.id)
    if question is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "错题不存在")
    return question


@router.get("/{question_id}/similar", response_model=list[QuestionOut])
def similar_questions(
    question_id: int,
    subject: str | None = Query(default=None, description="学科硬过滤（如 math）"),
    grade: int | None = Query(default=None, ge=1, le=12, description="年级硬过滤"),
    region: str | None = Query(default=None, description="地区硬过滤"),
    difficulty: str | None = Query(
        default=None, pattern="^(easy|medium|hard)$", description="难度（±1 档）"
    ),
    strict: bool = Query(default=False, description="严格模式：召回不足也不放宽/回落"),
    user: User = Depends(get_current_user),
) -> list[QuestionOut]:
    """同类题召回（P3.1）：约束参数均可选，不传时与旧行为一致。

    宽松模式下召回不足会级联放宽（先放地区、再放年级），并回落公共题库
    （source="bank"）与 AI 变式生成（source="generated"）。
    """
    question = _service().get_question(question_id, user.id)
    if question is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "错题不存在")
    outcome = _service().similar_questions(
        question,
        user_id=user.id,
        subject=subject or None,
        grade=grade,
        region=region or None,
        difficulty=difficulty,
        strict=strict,
    )
    return outcome.items


@router.patch("/{question_id}", response_model=QuestionOut)
def update_question(
    question_id: int,
    payload: QuestionUpdate,
    user: User = Depends(get_current_user),
) -> QuestionOut:
    updated = _service().update_question(
        question_id,
        user.id,
        content_markdown=payload.content_markdown,
        answer=payload.answer,
        tags=payload.tags,
        user_note=payload.user_note,
        subject=payload.subject,
        grade=payload.grade,
        region=payload.region,
        textbook_version=payload.textbook_version,
        question_type=payload.question_type,
        chapter=payload.chapter,
        error_category=payload.error_category,
    )
    if updated is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "错题不存在")
    return updated


@router.delete("/{question_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_question(question_id: int, user: User = Depends(get_current_user)) -> None:
    deleted = _service().delete_questions([question_id], user.id)
    if deleted == 0:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "错题不存在")
