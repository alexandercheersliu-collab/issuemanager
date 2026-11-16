from __future__ import annotations

import hashlib
import uuid

from backend.database import SessionLocal
from backend.models.orm import QuestionSourceFingerprint, User
from backend.repositories.question_bank import QuestionBankRepository
from backend.services.question_bank import QuestionBankService
from backend.services.question_service import QuestionService
from backend.services.rag import SimilarResult


class _VectorStore:
    def similar_canonical_questions(self, *args, **kwargs):
        return SimilarResult()


def _canonical(session, stem: str) -> int:
    canonical = QuestionBankRepository(session).create(
        stem_markdown=stem,
        analysis_markdown="完整解析",
        answer="完整答案",
    )
    session.commit()
    return canonical.id


def test_reference_registers_pathless_source_fingerprint_for_exact_image_reuse(
    db_session, student_user, tmp_path
):
    canonical_id = _canonical(db_session, f"同图复用-{uuid.uuid4().hex}")
    private_image = tmp_path / "private.jpg"
    private_image.write_bytes(b"student-private-evidence")
    image_hash = hashlib.sha256(private_image.read_bytes()).hexdigest()
    service = QuestionService(session_factory=SessionLocal)

    service.reference_canonical(
        student_user.id,
        canonical_id,
        private_image_path=str(private_image),
        image_hash=image_hash,
    )

    bank = QuestionBankService(
        session_factory=SessionLocal, vector_store=_VectorStore()
    )
    assert bank.match_original_hash(image_hash).id == canonical_id
    fingerprint = db_session.query(QuestionSourceFingerprint).filter_by(
        canonical_id=canonical_id, content_hash=image_hash
    ).one()
    assert fingerprint.canonical_id == canonical_id
    assert fingerprint.content_hash == image_hash
    assert not hasattr(fingerprint, "user_id")
    assert not hasattr(fingerprint, "storage_path")


def test_original_hash_remains_ambiguous_when_it_maps_to_distinct_questions(
    db_session, student_user, tmp_path
):
    first_id = _canonical(db_session, f"第一题-{uuid.uuid4().hex}")
    second_id = _canonical(db_session, f"不同第二题-{uuid.uuid4().hex}")
    other = User(username=f"other-{uuid.uuid4().hex}", password_hash="x", role="student")
    db_session.add(other)
    db_session.commit()
    private_image = tmp_path / "private.jpg"
    private_image.write_bytes(b"shared-image")
    image_hash = hashlib.sha256(private_image.read_bytes()).hexdigest()
    service = QuestionService(session_factory=SessionLocal)
    for user_id, canonical_id in ((student_user.id, first_id), (other.id, second_id)):
        service.reference_canonical(
            user_id,
            canonical_id,
            private_image_path=str(private_image),
            image_hash=image_hash,
        )

    bank = QuestionBankService(
        session_factory=SessionLocal, vector_store=_VectorStore()
    )
    assert bank.match_original_hash(image_hash) is None
