from __future__ import annotations

import math
from pathlib import Path

from PIL import Image

from backend.utils.logging import get_logger

logger = get_logger("figures")


def _pixel_box(box, width: int, height: int) -> tuple[int, int, int, int] | None:
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        return None
    try:
        values = [float(value) for value in box]
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(value) for value in values):
        return None
    x0, y0, x1, y1 = [max(0.0, min(1000.0, value)) for value in values]
    pixels = (
        int(x0 / 1000 * width),
        int(y0 / 1000 * height),
        int(x1 / 1000 * width),
        int(y1 / 1000 * height),
    )
    if pixels[2] <= pixels[0] or pixels[3] <= pixels[1]:
        return None
    return pixels


def crop_figure(image_path, box) -> Image.Image | None:
    try:
        with Image.open(image_path) as image:
            pixels = _pixel_box(box, image.width, image.height)
            if pixels is None:
                return None
            return image.crop(pixels)
    except OSError as exc:
        logger.warning("图形裁剪读取失败 %s: %s", image_path, exc)
        return None


def crop_figures(image_path, bboxes, output_dir, *, prefix: str) -> list[str]:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    paths: list[str] = []
    try:
        with Image.open(image_path) as image:
            for index, box in enumerate(bboxes):
                pixels = _pixel_box(box, image.width, image.height)
                if pixels is None:
                    continue
                target = output / f"{prefix}-figure-{index + 1}.png"
                try:
                    image.crop(pixels).save(target)
                except OSError as exc:
                    logger.warning("图形裁剪保存失败 %s: %s", target, exc)
                    continue
                paths.append(str(target))
    except OSError as exc:
        logger.warning("图形裁剪读取失败 %s: %s", image_path, exc)
    return paths
