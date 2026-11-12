from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

from backend.models.schemas import (
    FigureComparison,
    FigureSpec,
    GeometryFigureDraft,
    GeometryVariant,
    QuestionOut,
)
from backend.services.ai.base import AnalysisContext
from backend.services.figures import crop_figure, crop_figures
from backend.services.tikz import TikzRenderer
from backend.utils.logging import get_logger

logger = get_logger("geometry_figures")


@dataclass(frozen=True)
class FigureOutcome:
    figure_paths: list[str]
    figure_specs: list[dict]
    needs_review: bool


@dataclass(frozen=True)
class GeneratedVariant:
    content: str
    figure_paths: list[str]
    figure_specs: list[dict]


@dataclass
class _FigureState:
    draft: GeometryFigureDraft
    render_path: str | None = None
    issues: list[str] = field(default_factory=list)
    passed: bool = False


class _VariantValidationError(ValueError):
    def __init__(self, comparison: FigureComparison) -> None:
        self.comparison = comparison
        super().__init__("; ".join(comparison.issues) or "新图与新题不一致")


class GeometryFigureService:
    def __init__(self, ai, renderer) -> None:
        self.ai = ai
        self.renderer = renderer

    @classmethod
    def from_settings(cls, ai, settings) -> GeometryFigureService:
        return cls(ai, TikzRenderer.from_settings(settings))

    def render_variant(
        self, variant: GeometryVariant, *, source: QuestionOut
    ) -> GeneratedVariant:
        current = variant
        for attempt in range(2):
            try:
                return self._render_variant_once(current, source=source)
            except Exception as exc:  # noqa: BLE001 - 编译或校验失败后允许整体重生成一次
                if attempt == 1:
                    raise
                repair_instruction = str(exc)
                if isinstance(exc, _VariantValidationError):
                    repair_instruction = exc.comparison.repair_instruction or repair_instruction
                current = self.ai.generate_geometry_variant(
                    source.content_markdown[:4000],
                    knowledge_points=list(source.knowledge_points or []),
                    error_category=source.error_category or "",
                    question_type=source.question_type or "",
                    difficulty=source.difficulty or "medium",
                    figure_specs=list(source.figure_specs or []),
                    context=AnalysisContext(
                        subject=source.subject or "math",
                        grade=source.grade,
                        region=source.region,
                        textbook_version=source.textbook_version,
                    ),
                    repair_instruction=repair_instruction,
                )
        raise RuntimeError("几何变式生成未完成")

    def _render_variant_once(
        self, variant: GeometryVariant, *, source: QuestionOut
    ) -> GeneratedVariant:
        content = variant.question.strip()
        if not content:
            raise ValueError("几何变式题干为空")
        if not variant.figures and (source.figure_paths or source.figure_specs):
            raise ValueError("源题包含图形，变式必须至少生成一幅新图，并与题干一致")
        figure_paths: list[str] = []
        figure_specs: list[dict] = []
        for draft in variant.figures:
            rendered = self.renderer.render(draft.tikz_source)
            comparison = self.ai.validate_geometry_variant(
                Path(rendered.png_path).read_bytes(),
                content=content,
                draft=draft,
                original_content=source.content_markdown,
                knowledge_points=list(source.knowledge_points or []),
                error_category=source.error_category or "",
            )
            if not comparison.passed:
                raise _VariantValidationError(comparison)
            figure_paths.append(rendered.png_path)
            figure_specs.append(
                FigureSpec(
                    kind=draft.kind,
                    semantic_spec=draft.semantic_spec,
                    tikz_source=draft.tikz_source,
                    render_path=rendered.png_path,
                    crop_path=None,
                    validation_status="ai_passed",
                    validation_issues=comparison.issues,
                    assumptions=draft.assumptions,
                ).model_dump()
            )
        return GeneratedVariant(content, figure_paths, figure_specs)

    def redraw_segment(
        self,
        *,
        image_path: Path,
        number: str,
        text: str,
        figure_bboxes: list,
        figures_dir: Path,
        context: AnalysisContext,
    ) -> FigureOutcome:
        boxes = list(figure_bboxes or [])
        if not boxes:
            return FigureOutcome([], [], False)

        image_bytes = Path(image_path).read_bytes()
        tex_available = self._tex_available()
        drafts = [_empty_draft() for _ in boxes]
        if tex_available:
            try:
                generated = self.ai.redraw_geometry(
                    image_bytes,
                    text=text,
                    figure_bboxes=boxes,
                    mime_type="image/png",
                    context=context,
                )
                for index, draft in enumerate(generated[: len(boxes)]):
                    drafts[index] = draft
            except Exception as exc:  # noqa: BLE001 - 单题解析失败后仍可裁剪兜底
                logger.warning("几何图形解析失败 number=%s: %s", number, exc)

        states = [_FigureState(draft=draft) for draft in drafts]
        failed_indices: list[int] = []
        for index, state in enumerate(states):
            if not tex_available or state.draft.kind != "geometry":
                failed_indices.append(index)
                continue
            try:
                reference = crop_figure(image_path, boxes[index])
                if reference is None:
                    failed_indices.append(index)
                    continue
                reference.close()
                state.passed = self._compile_compare_and_repair(
                    state,
                    image_path=image_path,
                    figure_box=boxes[index],
                    text=text,
                )
            except Exception as exc:  # noqa: BLE001 - 任一图异常不影响其他图
                state.issues = [str(exc)]
                state.passed = False
            if not state.passed:
                failed_indices.append(index)

        fallback_boxes = boxes
        if failed_indices:
            fallback_boxes = self._refine_boxes(image_bytes, number, text, boxes)

        paths_by_index: list[str | None] = [None] * len(boxes)
        specs: list[FigureSpec] = []
        for index, state in enumerate(states):
            crop_path = None
            if state.passed:
                paths_by_index[index] = state.render_path
            else:
                try:
                    crop_path = self._crop_fallback(
                        image_path=image_path,
                        number=number,
                        index=index,
                        box=_box_at(fallback_boxes, index, boxes[index]),
                        original_box=boxes[index],
                        figures_dir=figures_dir,
                    )
                except Exception as exc:  # noqa: BLE001 - 裁剪异常仅影响当前图
                    logger.warning("图形兜底裁剪失败 number=%s index=%s: %s", number, index, exc)
                    state.issues = [*state.issues, str(exc)]
                paths_by_index[index] = crop_path
            specs.append(
                FigureSpec(
                    kind=state.draft.kind,
                    semantic_spec=state.draft.semantic_spec,
                    tikz_source=state.draft.tikz_source,
                    render_path=state.render_path,
                    crop_path=crop_path,
                    validation_status="ai_passed" if state.passed else "fallback",
                    validation_issues=state.issues,
                    assumptions=state.draft.assumptions,
                )
            )

        return FigureOutcome(
            figure_paths=[path for path in paths_by_index if path is not None],
            figure_specs=[spec.model_dump() for spec in specs],
            needs_review=bool(failed_indices),
        )

    def review_tikz(
        self,
        *,
        image_path: Path,
        text: str,
        figure_box: list | None,
        draft: GeometryFigureDraft,
        tikz_source: str,
    ) -> FigureSpec:
        reviewed_draft = draft.model_copy(update={"tikz_source": tikz_source})
        rendered = self.renderer.render(tikz_source)
        composite = _composite_image(image_path, figure_box, rendered.png_path)
        comparison = self.ai.compare_geometry(
            composite,
            text=text,
            draft=reviewed_draft,
        )
        return FigureSpec(
            kind=reviewed_draft.kind,
            semantic_spec=reviewed_draft.semantic_spec,
            tikz_source=tikz_source,
            render_path=rendered.png_path,
            crop_path=None,
            validation_status="ai_passed" if comparison.passed else "fallback",
            validation_issues=comparison.issues,
            assumptions=reviewed_draft.assumptions,
        )

    def _tex_available(self) -> bool:
        try:
            self.renderer.check_environment()
        except Exception as exc:  # noqa: BLE001 - TeX 不可用时整题走延迟裁剪兜底
            logger.warning("TikZ 环境不可用: %s", exc)
            return False
        return True

    def _compile_compare_and_repair(
        self,
        state: _FigureState,
        *,
        image_path: Path,
        figure_box: list,
        text: str,
    ) -> bool:
        composite = _reference_png(image_path, figure_box)
        for attempt in range(2):
            try:
                state.render_path = None
                rendered = self.renderer.render(state.draft.tikz_source)
                state.render_path = rendered.png_path
                composite = _composite_image(image_path, figure_box, rendered.png_path)
                comparison = self.ai.compare_geometry(
                    composite,
                    text=text,
                    draft=state.draft,
                )
                state.issues = comparison.issues
                if comparison.passed:
                    return True
            except Exception as exc:  # noqa: BLE001 - 编译和对比异常均进入一次修复
                state.issues = [str(exc)]
                comparison = FigureComparison(
                    passed=False,
                    issues=[str(exc)],
                    repair_instruction=str(exc),
                )

            if attempt == 1:
                break
            try:
                state.draft = self.ai.repair_geometry(
                    composite,
                    text=text,
                    draft=state.draft,
                    comparison=comparison,
                )
            except Exception as exc:  # noqa: BLE001 - 修复失败直接进入延迟兜底
                state.issues = [*state.issues, str(exc)]
                break
        return False

    def _refine_boxes(
        self,
        image_bytes: bytes,
        number: str,
        text: str,
        boxes: list,
    ) -> list:
        try:
            refined = self.ai.locate_figures(
                image_bytes,
                [{"number": number, "text": text, "figure_bboxes": boxes}],
                "image/png",
            )
            if refined and refined[0]:
                return refined[0]
        except Exception as exc:  # noqa: BLE001 - 二次定位失败保留首次坐标
            logger.warning("二次图形定位失败 number=%s，使用首次坐标: %s", number, exc)
        return boxes

    @staticmethod
    def _crop_fallback(
        *,
        image_path: Path,
        number: str,
        index: int,
        box: list,
        original_box: list,
        figures_dir: Path,
    ) -> str | None:
        safe_number = re.sub(r"[^A-Za-z0-9_.-]+", "-", number).strip("-.") or "unknown"
        prefix = f"segment-{safe_number}-{index + 1}"
        paths = crop_figures(image_path, [box], figures_dir, prefix=prefix)
        if not paths and box != original_box:
            paths = crop_figures(image_path, [original_box], figures_dir, prefix=prefix)
        return paths[0] if paths else None


def _empty_draft() -> GeometryFigureDraft:
    return GeometryFigureDraft(kind="geometry", semantic_spec={}, tikz_source="")


def _box_at(boxes: list, index: int, default: list) -> list:
    return boxes[index] if index < len(boxes) else default


def _reference_png(image_path: Path, figure_box: list | None) -> bytes:
    reference = crop_figure(image_path, figure_box) if figure_box is not None else None
    if reference is None:
        with Image.open(image_path) as original:
            reference = original.convert("RGB")
    else:
        converted = reference.convert("RGB")
        reference.close()
        reference = converted
    output = io.BytesIO()
    reference.save(output, format="PNG")
    reference.close()
    return output.getvalue()


def _composite_image(image_path: Path, figure_box: list | None, png_path: str) -> bytes:
    reference = crop_figure(image_path, figure_box) if figure_box is not None else None
    if reference is None:
        with Image.open(image_path) as original:
            reference = original.convert("RGB")
    else:
        converted = reference.convert("RGB")
        reference.close()
        reference = converted

    with Image.open(png_path) as rendered:
        rendered_rgb = rendered.convert("RGB")
    composite = Image.new(
        "RGB",
        (reference.width + rendered_rgb.width, max(reference.height, rendered_rgb.height)),
        "white",
    )
    composite.paste(reference, (0, 0))
    composite.paste(rendered_rgb, (reference.width, 0))
    output = io.BytesIO()
    composite.save(output, format="PNG")
    reference.close()
    rendered_rgb.close()
    composite.close()
    return output.getvalue()
