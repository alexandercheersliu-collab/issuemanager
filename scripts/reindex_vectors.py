"""从权威 SQL 重建标准题与个人错题向量索引（幂等）。

标准题向量使用 canonical_questions.id，不再使用旧种子 bank_id；个人错题同时补写
question_type 等 K12 元数据。ChromaDB 只保存可重建的召回索引。

用法（项目根目录）：
    .venv/bin/python scripts/reindex_vectors.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.database import SessionLocal, init_db  # noqa: E402
from backend.models.orm import CanonicalQuestion, Question  # noqa: E402
from backend.models.schemas import QuestionOut  # noqa: E402
from backend.services.question_service import QuestionService  # noqa: E402


def reindex_canonical_questions(session_factory, vector_store) -> tuple[int, int]:
    """从权威 SQL 重建全部标准题向量，ID 始终使用 canonical_questions.id。"""
    with session_factory() as session:
        questions = list(session.query(CanonicalQuestion).all())
        rows = [
            {
                "id": question.id,
                "stem": question.stem_markdown,
                "subject": question.subject,
                "grade": question.grade,
                "region": question.region,
                "knowledge_points": list(question.knowledge_points or []),
                "difficulty": question.difficulty,
                "answer": question.answer,
                "question_type": question.question_type or "",
                "chapter": question.chapter or "",
            }
            for question in questions
        ]
    ok = 0
    for question in rows:
        ok += int(
            vector_store.upsert_bank_question(
                str(question["id"]),
                question["stem"],
                subject=question["subject"],
                grade=question["grade"],
                region=question["region"],
                knowledge_points=question["knowledge_points"],
                difficulty=question["difficulty"],
                answer=question["answer"],
                question_type=question["question_type"],
                chapter=question["chapter"],
            )
        )
    if not vector_store.reconcile_bank_questions([str(question["id"]) for question in rows]):
        raise RuntimeError("标准题向量对账失败，请修复 ChromaDB 后重跑")
    return len(rows), ok


def reindex_personal_questions(session_factory, vector_store) -> tuple[int, int]:
    with session_factory() as session:
        questions = list(session.query(Question).all())
        total = len(questions)
        ok = 0
        for index, question in enumerate(questions, 1):
            out = QuestionOut.from_orm_model(question)
            text = " ".join(
                [*(out.knowledge_points or []), out.content_markdown, out.answer or ""]
            )
            success = vector_store.upsert_question(
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
                print(f"个人错题进度 {index}/{total}（成功 {ok}）", flush=True)
    return total, ok


def main() -> None:
    init_db()
    service = QuestionService()
    canonical_total, canonical_ok = reindex_canonical_questions(
        SessionLocal, service.vector_store
    )
    print(f"标准题：共 {canonical_total} 条，成功 {canonical_ok}，失败 {canonical_total - canonical_ok}")
    total, ok = reindex_personal_questions(SessionLocal, service.vector_store)
    print(f"个人错题：共 {total} 条，成功 {ok}，失败 {total - ok}")
    if ok < total or canonical_ok < canonical_total:
        print("提示：有写入失败（常见于服务运行时 SQLite 锁），请停止服务后重跑本脚本")


if __name__ == "__main__":
    main()
