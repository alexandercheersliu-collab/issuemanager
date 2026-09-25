"""原图路径自愈的回归测试。

背景（2026-09-25 真实事故）：questions.image_path 存的是**绝对路径**，
把 DATA_DIR 迁到别的磁盘后，旧记录全部变成悬空引用——界面静默不显示图片，
而且外部清理脚本会把这些"路径不存在"的文件误判为孤儿。
resolve_image_path() 让读取端按文件名回落到当前数据目录。
"""
from __future__ import annotations

from types import SimpleNamespace

from backend.models.orm import resolve_image_path
from backend.models.schemas import QuestionOut


def test_returns_none_for_empty_or_missing():
    assert resolve_image_path(None, 2) is None
    assert resolve_image_path("", 2) is None
    assert resolve_image_path("D:/nowhere/gone.jpg", 2) is None


def test_existing_absolute_path_is_used_as_is(tmp_path):
    img = tmp_path / "u2" / "a.jpg"
    img.parent.mkdir(parents=True)
    img.write_bytes(b"x")
    assert resolve_image_path(str(img), 2) == img


def test_falls_back_to_current_data_dir(tmp_path, monkeypatch):
    """旧绝对路径失效时，按文件名到当前 DATA_DIR 找回来。"""
    import backend.models.orm as orm
    from backend.config import Settings

    data_dir = tmp_path / "newdata"
    (data_dir / "images" / "u2").mkdir(parents=True)
    moved = data_dir / "images" / "u2" / "20260925_101357_30d68517.jpg"
    moved.write_bytes(b"jpeg-bytes")

    monkeypatch.setattr(
        orm, "get_settings", lambda: Settings(data_dir=data_dir, _env_file=None)
    )
    stale = "D:/workspace/learning-tour/Math_Tutor_RAG/data/images/u2/20260925_101357_30d68517.jpg"

    assert resolve_image_path(stale, 2) == moved
    # 用户隔离：别的用户目录下同名文件不算命中
    assert resolve_image_path(stale, 99) is None


def test_question_out_exposes_resolved_path(tmp_path, monkeypatch):
    """QuestionOut.from_orm_model 必须给出自愈后的路径（界面全部走它）。"""
    import backend.models.orm as orm
    from backend.config import Settings

    data_dir = tmp_path / "newdata"
    (data_dir / "images" / "u2").mkdir(parents=True)
    moved = data_dir / "images" / "u2" / "pic.jpg"
    moved.write_bytes(b"jpeg-bytes")
    monkeypatch.setattr(
        orm, "get_settings", lambda: Settings(data_dir=data_dir, _env_file=None)
    )

    class _Q:
        id = 1
        user_id = 2
        image_path = "D:/old/place/pic.jpg"
        content_markdown = "题面"
        answer = "答案"
        knowledge_points = []
        tags = []
        difficulty = "medium"
        followup_question = None
        source = "ai"
        user_note = None
        ocr_text = None
        reps = 0
        ease = 2.5
        interval_days = 0
        due_at = None
        last_reviewed_at = None
        created_at = None

        def resolved_image_path(self):
            return resolve_image_path(self.image_path, self.user_id)

    out = QuestionOut.from_orm_model(_Q())
    assert out.image_path == str(moved)


def test_question_out_blank_when_image_really_gone(tmp_path, monkeypatch):
    """图真的不在时给 None，而不是把失效路径透给界面。"""
    import backend.models.orm as orm
    from backend.config import Settings

    monkeypatch.setattr(
        orm, "get_settings", lambda: Settings(data_dir=tmp_path, _env_file=None)
    )

    class _Q:
        id = 1
        user_id = 2
        image_path = "D:/old/place/missing.jpg"
        content_markdown = "题面"
        answer = "答案"
        knowledge_points = []
        tags = []
        difficulty = "medium"
        followup_question = None
        source = "ai"
        user_note = None
        ocr_text = None
        reps = 0
        ease = 2.5
        interval_days = 0
        due_at = None
        last_reviewed_at = None
        created_at = None

        def resolved_image_path(self):
            return resolve_image_path(self.image_path, self.user_id)

    assert QuestionOut.from_orm_model(_Q()).image_path is None
    # 记录本身仍然可用（解析内容不受图片缺失影响）
    assert QuestionOut.from_orm_model(_Q()).content_markdown == "题面"


def test_from_orm_model_tolerates_object_without_resolver():
    """兼容没有 resolved_image_path 的轻量对象（老测试替身/第三方模型）。"""
    q = SimpleNamespace(
        id=1, user_id=2, image_path="x.jpg", content_markdown="c", answer="a",
        knowledge_points=[], tags=[], difficulty="medium", followup_question=None,
        source="ai", user_note=None, ocr_text=None, reps=0, ease=2.5,
        interval_days=0, due_at=None, last_reviewed_at=None, created_at=None,
    )
    assert QuestionOut.from_orm_model(q).image_path is None
