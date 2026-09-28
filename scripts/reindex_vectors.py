"""一次性重建错题向量索引：把 question_type 等 K12 元数据补写进既有向量文档。

背景：P3 同类题召回新增「题型硬过滤」，但既有向量文档缺 question_type 键时
按「键缺失放行」语义不会被过滤，导致旧数据仍可能跨题型误配（应用题/计算题）。
本脚本对全部错题重新 upsert（幂等），补全元数据。

用法（项目根目录）：
    .venv/Scripts/python.exe scripts/reindex_vectors.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.database import SessionLocal, init_db  # noqa: E402
from backend.models.orm import Question  # noqa: E402
from backend.models.schemas import QuestionOut  # noqa: E402
from backend.services.question_service import QuestionService  # noqa: E402


def main() -> None:
    init_db()
    service = QuestionService()
    with SessionLocal() as session:
        questions = list(session.query(Question).all())
    total = len(questions)
    ok = 0
    for index, question in enumerate(questions, 1):
        out = QuestionOut.from_orm_model(question)
        text = " ".join(
            [*(out.knowledge_points or []), out.content_markdown, out.answer or ""]
        )
        success = service.vector_store.upsert_question(
            out.id,
            text,
            user_id=out.user_id,
            tags=out.tags,
            subject=out.subject,
            grade=out.grade,
            region=out.region,
            knowledge_points=out.knowledge_points,
            difficulty=out.difficulty,
            question_type=out.question_type,
        )
        ok += int(success)
        if index % 20 == 0 or index == total:
            print(f"进度 {index}/{total}（成功 {ok}）", flush=True)
    print(f"完成：共 {total} 条，成功 {ok}，失败 {total - ok}")
    if ok < total:
        print("提示：有写入失败（常见于服务运行时 SQLite 锁），请停止服务后重跑本脚本")


if __name__ == "__main__":
    main()
