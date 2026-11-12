from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from backend.models.schemas import FigureComparison, GeometryFigureDraft
from backend.services.ai.base import AnalysisContext
from backend.services.figures import crop_figure
from backend.services.geometry_figures import GeometryFigureService
from backend.services.tikz import RenderedFigure, TikzRenderer

BOX = [0, 0, 500, 500]


def _draft(source="initial", *, kind="geometry") -> GeometryFigureDraft:
    return GeometryFigureDraft(
        kind=kind,
        semantic_spec={"points": ["A", "B"], "source": source},
        tikz_source=source,
        assumptions=["示意图不按比例"],
    )


def _page(path: Path) -> Path:
    Image.new("RGB", (100, 80), "white").save(path)
    return path


class FakeAI:
    def __init__(
        self,
        *,
        drafts=(),
        comparisons=(),
        repaired: GeometryFigureDraft | None = None,
        refined_boxes=None,
        locate_error: Exception | None = None,
    ) -> None:
        self.drafts = list(drafts)
        self.comparisons = list(comparisons)
        self.repaired = repaired
        self.refined_boxes = refined_boxes
        self.locate_error = locate_error
        self.redraw_calls = []
        self.compare_calls = []
        self.repair_calls = []
        self.locate_calls = []

    def redraw_geometry(self, image_bytes, **kwargs):
        self.redraw_calls.append((image_bytes, kwargs))
        return list(self.drafts)

    def compare_geometry(self, composite_image_bytes, *, text, draft):
        self.compare_calls.append((composite_image_bytes, text, draft))
        if self.comparisons:
            return self.comparisons.pop(0)
        return FigureComparison(passed=True)

    def repair_geometry(self, composite_image_bytes, *, text, draft, comparison):
        self.repair_calls.append((composite_image_bytes, text, draft, comparison))
        return self.repaired or draft

    def locate_figures(self, image_bytes, items, mime_type="image/png"):
        self.locate_calls.append((image_bytes, items, mime_type))
        if self.locate_error is not None:
            raise self.locate_error
        if self.refined_boxes is None:
            return [[]]
        return self.refined_boxes


class FakeRenderer:
    def __init__(
        self,
        tmp_path: Path,
        *,
        fail_sources=(),
        missing_tex=False,
        wrong_tex=False,
    ) -> None:
        self.tmp_path = tmp_path
        self.fail_sources = set(fail_sources)
        self.missing_tex = missing_tex
        self.wrong_tex = wrong_tex
        self.pdflatex_path = tmp_path / ("missing-pdflatex" if missing_tex else "pdflatex")
        if not missing_tex:
            self.pdflatex_path.touch()
            self.pdflatex_path.chmod(0o755)
        self.check_calls = 0
        self.render_calls = []

    def check_environment(self) -> None:
        self.check_calls += 1
        if self.missing_tex:
            raise RuntimeError("pdflatex is not executable")
        if self.wrong_tex:
            raise RuntimeError("TeX Live 2026 is required, found TeX Live 2025")

    def render(self, source: str) -> RenderedFigure:
        self.render_calls.append(source)
        if source in self.fail_sources:
            raise RuntimeError(f"cannot compile {source}")
        index = len(self.render_calls)
        tex_path = self.tmp_path / f"render-{index}.tex"
        png_path = self.tmp_path / f"render-{index}.png"
        tex_path.write_text(source, encoding="utf-8")
        Image.new("RGB", (20, 10), "black").save(png_path)
        return RenderedFigure(str(tex_path), str(png_path), cache_hit=False)


def _redraw(service, image_path, tmp_path, boxes=None):
    return service.redraw_segment(
        image_path=image_path,
        number="1",
        text="三角形 ABC",
        figure_bboxes=[BOX] if boxes is None else boxes,
        figures_dir=tmp_path / "crops",
        context=AnalysisContext(grade=8),
    )


def test_success_uses_in_memory_comparison_without_refine_or_disk_crop(tmp_path):
    image_path = _page(tmp_path / "page.png")
    ai = FakeAI(drafts=[_draft()])
    renderer = FakeRenderer(tmp_path)
    service = GeometryFigureService(ai, renderer)

    outcome = _redraw(service, image_path, tmp_path)

    assert outcome.needs_review is False
    assert len(outcome.figure_paths) == 1
    assert outcome.figure_paths[0].endswith("render-1.png")
    assert outcome.figure_specs[0]["validation_status"] == "ai_passed"
    assert outcome.figure_specs[0]["crop_path"] is None
    assert outcome.figure_specs[0]["render_path"] == outcome.figure_paths[0]
    assert ai.locate_calls == []
    assert not (tmp_path / "crops").exists()
    with Image.open(io.BytesIO(ai.compare_calls[0][0])) as composite:
        assert composite.size == (70, 40)
    assert ai.redraw_calls[0][1]["figure_bboxes"] == [BOX]
    assert ai.redraw_calls[0][1]["context"] == AnalysisContext(grade=8)


def test_failed_comparison_is_repaired_once_then_recompiled_and_compared(tmp_path):
    image_path = _page(tmp_path / "page.png")
    ai = FakeAI(
        drafts=[_draft("initial")],
        comparisons=[
            FigureComparison(passed=False, issues=["缺少直角"], repair_instruction="补直角"),
            FigureComparison(passed=True),
        ],
        repaired=_draft("fixed"),
    )
    renderer = FakeRenderer(tmp_path)
    outcome = _redraw(GeometryFigureService(ai, renderer), image_path, tmp_path)

    assert renderer.render_calls == ["initial", "fixed"]
    assert len(ai.compare_calls) == 2
    assert len(ai.repair_calls) == 1
    assert outcome.figure_specs[0]["tikz_source"] == "fixed"
    assert outcome.figure_specs[0]["validation_status"] == "ai_passed"
    assert outcome.needs_review is False
    assert ai.locate_calls == []


def test_final_compile_failure_lazily_refines_and_crops(tmp_path):
    image_path = _page(tmp_path / "page.png")
    ai = FakeAI(
        drafts=[_draft("bad")],
        repaired=_draft("bad-repaired"),
        refined_boxes=[[[0, 0, 1000, 500]]],
    )
    renderer = FakeRenderer(tmp_path, fail_sources={"bad", "bad-repaired"})

    outcome = _redraw(GeometryFigureService(ai, renderer), image_path, tmp_path)

    assert renderer.render_calls == ["bad", "bad-repaired"]
    assert len(ai.repair_calls) == 1
    assert len(ai.locate_calls) == 1
    assert outcome.needs_review is True
    assert len(outcome.figure_paths) == 1
    with Image.open(outcome.figure_paths[0]) as crop:
        assert crop.size == (100, 40)
    spec = outcome.figure_specs[0]
    assert spec["validation_status"] == "fallback"
    assert spec["crop_path"] == outcome.figure_paths[0]
    assert spec["render_path"] is None
    assert spec["tikz_source"] == "bad-repaired"


def test_compile_failure_repair_receives_original_reference_png(tmp_path):
    image_path = _page(tmp_path / "page.png")
    ai = FakeAI(drafts=[_draft("bad")], repaired=_draft("fixed"))
    renderer = FakeRenderer(tmp_path, fail_sources={"bad"})

    outcome = _redraw(GeometryFigureService(ai, renderer), image_path, tmp_path)

    repair_image = ai.repair_calls[0][0]
    assert repair_image.startswith(b"\x89PNG\r\n\x1a\n")
    with Image.open(io.BytesIO(repair_image)) as reference:
        assert reference.size == (50, 40)
    assert outcome.figure_specs[0]["validation_status"] == "ai_passed"


def test_refine_failure_uses_first_boxes(tmp_path):
    image_path = _page(tmp_path / "page.png")
    ai = FakeAI(
        drafts=[_draft("bad")],
        repaired=_draft("bad-repaired"),
        locate_error=RuntimeError("locate unavailable"),
    )
    renderer = FakeRenderer(tmp_path, fail_sources={"bad", "bad-repaired"})

    outcome = _redraw(GeometryFigureService(ai, renderer), image_path, tmp_path)

    assert len(ai.locate_calls) == 1
    with Image.open(outcome.figure_paths[0]) as crop:
        assert crop.size == (50, 40)
    assert outcome.figure_specs[0]["validation_status"] == "fallback"


def test_invalid_fallback_box_returns_no_figure_and_needs_review(tmp_path):
    image_path = _page(tmp_path / "page.png")
    ai = FakeAI(drafts=[_draft(kind="statistics")])

    outcome = _redraw(
        GeometryFigureService(ai, FakeRenderer(tmp_path)),
        image_path,
        tmp_path,
        boxes=[[0, 0, 0, 100]],
    )

    assert outcome.figure_paths == []
    assert outcome.needs_review is True
    assert outcome.figure_specs[0]["crop_path"] is None
    assert outcome.figure_specs[0]["validation_status"] == "fallback"


def test_non_geometry_draft_goes_directly_to_fallback(tmp_path):
    image_path = _page(tmp_path / "page.png")
    ai = FakeAI(drafts=[_draft("not-tikz", kind="statistics")])
    renderer = FakeRenderer(tmp_path)

    outcome = _redraw(GeometryFigureService(ai, renderer), image_path, tmp_path)

    assert renderer.render_calls == []
    assert ai.compare_calls == []
    assert ai.repair_calls == []
    assert len(ai.locate_calls) == 1
    assert outcome.figure_specs[0]["kind"] == "statistics"
    assert outcome.figure_specs[0]["validation_status"] == "fallback"
    assert len(outcome.figure_paths) == 1


def test_one_failed_figure_does_not_discard_another_success(tmp_path):
    image_path = _page(tmp_path / "page.png")
    ai = FakeAI(
        drafts=[_draft("good"), _draft("bad")],
        repaired=_draft("bad-repaired"),
    )
    renderer = FakeRenderer(tmp_path, fail_sources={"bad", "bad-repaired"})
    boxes = [BOX, [500, 0, 1000, 500]]

    outcome = _redraw(GeometryFigureService(ai, renderer), image_path, tmp_path, boxes)

    assert len(outcome.figure_paths) == 2
    assert outcome.figure_paths[0].endswith("render-1.png")
    assert outcome.figure_paths[1].endswith(".png")
    assert [spec["validation_status"] for spec in outcome.figure_specs] == [
        "ai_passed",
        "fallback",
    ]
    assert outcome.figure_specs[0]["crop_path"] is None
    assert outcome.figure_specs[1]["crop_path"] == outcome.figure_paths[1]
    assert outcome.needs_review is True
    assert len(ai.locate_calls) == 1


def test_invalid_initial_box_geometry_skips_tikz_and_uses_lazy_fallback(tmp_path):
    image_path = _page(tmp_path / "page.png")
    ai = FakeAI(drafts=[_draft()])
    renderer = FakeRenderer(tmp_path)

    outcome = _redraw(
        GeometryFigureService(ai, renderer),
        image_path,
        tmp_path,
        boxes=[[0, 0, 0, 100]],
    )

    assert renderer.render_calls == []
    assert ai.compare_calls == []
    assert ai.repair_calls == []
    assert len(ai.locate_calls) == 1
    assert outcome.figure_paths == []
    assert outcome.figure_specs[0]["validation_status"] == "fallback"
    assert outcome.figure_specs[0]["crop_path"] is None
    assert outcome.needs_review is True


def test_fallback_crop_exception_preserves_other_figures_and_failed_spec(tmp_path, monkeypatch):
    image_path = _page(tmp_path / "page.png")
    ai = FakeAI(drafts=[_draft("good"), _draft("not-tikz", kind="statistics")])
    renderer = FakeRenderer(tmp_path)

    def fail_crop(*args, **kwargs):
        raise OSError("crop directory unavailable")

    monkeypatch.setattr("backend.services.geometry_figures.crop_figures", fail_crop)
    outcome = _redraw(
        GeometryFigureService(ai, renderer),
        image_path,
        tmp_path,
        boxes=[BOX, [500, 0, 1000, 500]],
    )

    assert len(outcome.figure_paths) == 1
    assert outcome.figure_paths[0].endswith("render-1.png")
    assert [spec["validation_status"] for spec in outcome.figure_specs] == [
        "ai_passed",
        "fallback",
    ]
    assert outcome.figure_specs[1]["crop_path"] is None
    assert outcome.needs_review is True


def test_missing_tex_skips_redraw_ai_and_uses_fallback(tmp_path):
    image_path = _page(tmp_path / "page.png")
    ai = FakeAI(drafts=[_draft()])
    renderer = FakeRenderer(tmp_path, missing_tex=True)

    outcome = _redraw(GeometryFigureService(ai, renderer), image_path, tmp_path)

    assert renderer.check_calls == 1
    assert ai.redraw_calls == []
    assert renderer.render_calls == []
    assert ai.compare_calls == []
    assert ai.repair_calls == []
    assert outcome.needs_review is True
    assert outcome.figure_specs[0]["validation_status"] == "fallback"
    assert len(outcome.figure_paths) == 1


def test_wrong_tex_version_skips_redraw_and_repair_and_uses_fallback(tmp_path):
    image_path = _page(tmp_path / "page.png")
    ai = FakeAI(drafts=[_draft()])
    renderer = FakeRenderer(tmp_path, wrong_tex=True)

    outcome = _redraw(
        GeometryFigureService(ai, renderer),
        image_path,
        tmp_path,
        boxes=[BOX, [500, 0, 1000, 500]],
    )

    assert renderer.check_calls == 1
    assert ai.redraw_calls == []
    assert renderer.render_calls == []
    assert ai.compare_calls == []
    assert ai.repair_calls == []
    assert outcome.needs_review is True
    assert [spec["validation_status"] for spec in outcome.figure_specs] == [
        "fallback",
        "fallback",
    ]
    assert len(outcome.figure_paths) == 2


@pytest.mark.parametrize(
    ("passed", "status"),
    [(True, "ai_passed"), (False, "fallback")],
)
def test_review_tikz_compiles_and_compares_supplied_source_without_redraw_or_repair(
    tmp_path, passed, status
):
    image_path = _page(tmp_path / "page.png")
    ai = FakeAI(comparisons=[FigureComparison(passed=passed, issues=[] if passed else ["不符"])])
    renderer = FakeRenderer(tmp_path)
    service = GeometryFigureService(ai, renderer)

    spec = service.review_tikz(
        image_path=image_path,
        text="三角形 ABC",
        figure_box=BOX,
        draft=_draft("old-source"),
        tikz_source="user-source",
    )

    assert renderer.render_calls == ["user-source"]
    assert ai.redraw_calls == []
    assert ai.repair_calls == []
    assert ai.locate_calls == []
    assert ai.compare_calls[0][2].tikz_source == "user-source"
    assert spec.tikz_source == "user-source"
    assert spec.validation_status == status
    assert spec.validation_issues == ([] if passed else ["不符"])
    assert spec.crop_path is None


def test_from_settings_uses_tikz_renderer_settings(tmp_path):
    settings = SimpleNamespace(data_dir=tmp_path)

    service = GeometryFigureService.from_settings(FakeAI(), settings)

    assert isinstance(service.renderer, TikzRenderer)
    assert service.renderer.artifact_dir == tmp_path / "figures" / "tikz"


def test_crop_figure_returns_in_memory_crop_without_creating_files(tmp_path):
    image_path = _page(tmp_path / "page.png")

    crop = crop_figure(image_path, BOX)

    assert crop is not None
    assert crop.size == (50, 40)
    assert list(tmp_path.iterdir()) == [image_path]
