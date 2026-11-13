from __future__ import annotations

from pathlib import Path

from PIL import Image

from backend.services.figures import crop_figures


def _image(path: Path, size=(100, 200)) -> Path:
    Image.new("RGB", size, "white").save(path)
    return path


def test_crop_figures_preserves_multiple_figure_order(tmp_path):
    image = _image(tmp_path / "page.png")

    paths = crop_figures(
        image,
        [[0, 0, 500, 500], [500, 500, 1000, 1000]],
        tmp_path / "figures",
        prefix="page-1-segment-1",
    )

    assert len(paths) == 2
    assert [Image.open(path).size for path in paths] == [(50, 100), (50, 100)]
    assert paths[0] != paths[1]


def test_crop_figures_clamps_coordinates_to_image(tmp_path):
    image = _image(tmp_path / "page.png")

    paths = crop_figures(
        image,
        [[-100, -100, 2000, 2000]],
        tmp_path / "figures",
        prefix="page-1-segment-1",
    )

    assert len(paths) == 1
    assert Image.open(paths[0]).size == (100, 200)


def test_crop_figures_skips_invalid_boxes(tmp_path):
    image = _image(tmp_path / "page.png")

    paths = crop_figures(
        image,
        [None, [0, 0, 0, 10], ["x", 0, 100, 100], [0, 0], [0, 0, 100, 100]],
        tmp_path / "figures",
        prefix="page-1-segment-1",
    )

    assert len(paths) == 1
    assert Image.open(paths[0]).size == (10, 20)
