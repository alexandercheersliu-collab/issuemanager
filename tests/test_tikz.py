from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from backend.config import Settings
from backend.services.tikz import TikzRenderer, build_document, validate_tikz_source

PDFLATEX_PATH = Path("/home/anna/.local/bin/pdflatex")
TRIANGLE_SOURCE = r"""
\begin{tikzpicture}[scale=1.5]
  \coordinate (A) at (0,0);
  \coordinate (B) at (3,0);
  \coordinate (C) at (3,2);
  \draw[thick] (A) -- (B) -- (C) -- cycle;
  \node[below left] at (A) {$A$};
  \node[below right] at (B) {$B$};
  \node[above right] at (C) {$C$};
  \pic[draw, angle radius=9mm, angle eccentricity=1.4, "$\alpha$"] {angle = B--A--C};
  \pic[draw, angle radius=4mm] {right angle = A--B--C};
\end{tikzpicture}
"""


def _fake_compiler(tmp_path: Path, body: str) -> Path:
    compiler = tmp_path / "pdflatex"
    compiler.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "if '--version' in sys.argv:\n"
        "    print('pdfTeX 3.141592653-2.6-1.40.29 (TeX Live 2026)')\n"
        "    raise SystemExit(0)\n"
        + body,
        encoding="utf-8",
    )
    compiler.chmod(0o755)
    return compiler


def test_complete_tikzpicture_uses_fixed_document_template() -> None:
    validate_tikz_source(TRIANGLE_SOURCE)

    document = build_document(TRIANGLE_SOURCE)

    assert document.startswith("\\documentclass[border=2pt]{standalone}\n")
    assert "\\usepackage{tikz}\n" in document
    assert "\\usepackage{amsmath}\n" in document
    assert (
        "\\usetikzlibrary{angles,quotes,calc,intersections,arrows.meta,bending}\n"
        in document
    )
    assert TRIANGLE_SOURCE.strip() in document
    assert document.rstrip().endswith("\\end{document}")


def test_scriptsize_text_labels_are_allowed() -> None:
    source = (
        "\\begin{tikzpicture}"
        "\\node[font=\\scriptsize] at (0,0) {$A$};"
        "\\end{tikzpicture}"
    )

    validate_tikz_source(source)


@pytest.mark.parametrize(
    "command",
    [
        r"\input{secret}",
        r"\openout1=secret",
        r"\write18{evil}",
        r"\documentclass{article}",
        r"\unknownmacro",
        r"^^5cinput{secret}",
        r"^^5copenout1=secret",
        r"^^5cwrite18{evil}",
        r"^^5cunknownmacro",
    ],
)
def test_dangerous_and_unknown_commands_are_rejected_before_subprocess(
    tmp_path: Path, command: str
) -> None:
    marker = tmp_path / "compiler-invoked"
    compiler = _fake_compiler(
        tmp_path,
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n",
    )
    renderer = TikzRenderer(compiler, tmp_path / "artifacts")
    source = "\\begin{tikzpicture}\n\\draw (0,0) -- (1,1);\n" + command + "\n\\end{tikzpicture}"

    with pytest.raises(ValueError):
        renderer.render(source)

    assert not marker.exists()


def test_nonzero_compiler_exit_contains_bounded_log_summary_and_uses_safe_mode(
    tmp_path: Path,
) -> None:
    invocation_path = tmp_path / "invocation.json"
    compiler = _fake_compiler(
        tmp_path,
        "import json\n"
        "import os\n"
        "from pathlib import Path\n"
        f"Path({str(invocation_path)!r}).write_text(json.dumps({{'argv': sys.argv, "
        "'openin_any': os.environ.get('openin_any'), "
        "'openout_any': os.environ.get('openout_any')}))\n"
        "print('x' * 200000)\n"
        "print('deliberate compiler failure')\n"
        "raise SystemExit(7)\n",
    )
    renderer = TikzRenderer(compiler, tmp_path / "artifacts")

    with pytest.raises(RuntimeError) as exc_info:
        renderer.render(TRIANGLE_SOURCE)

    message = str(exc_info.value)
    assert "deliberate compiler failure" in message
    assert len(message) < 70000
    invocation = json.loads(invocation_path.read_text(encoding="utf-8"))
    assert "-no-shell-escape" in invocation["argv"]
    assert "-interaction=nonstopmode" in invocation["argv"]
    assert "-halt-on-error" in invocation["argv"]
    assert invocation["openin_any"] == "p"
    assert invocation["openout_any"] == "p"


def test_compiler_timeout_contains_log_summary(tmp_path: Path) -> None:
    compiler = _fake_compiler(
        tmp_path,
        "import time\n"
        "print('timeout marker', flush=True)\n"
        "time.sleep(5)\n",
    )
    renderer = TikzRenderer(compiler, tmp_path / "artifacts", timeout_seconds=0.1)

    with pytest.raises(RuntimeError) as exc_info:
        renderer.render(TRIANGLE_SOURCE)

    assert "timed out" in str(exc_info.value).lower()
    assert "timeout marker" in str(exc_info.value)


def test_from_settings_uses_task_8_fallbacks_and_optional_overrides(tmp_path: Path) -> None:
    fallback = TikzRenderer.from_settings(Settings(data_dir=tmp_path))
    assert fallback.pdflatex_path == PDFLATEX_PATH
    assert fallback.artifact_dir == tmp_path / "figures" / "tikz"
    assert fallback.timeout_seconds == 20.0

    compiler = tmp_path / "custom-pdflatex"
    configured = TikzRenderer.from_settings(
        SimpleNamespace(
            data_dir=tmp_path,
            pdflatex_path=compiler,
            tikz_timeout_seconds=3.5,
        )
    )
    assert configured.pdflatex_path == compiler
    assert configured.timeout_seconds == 3.5


@pytest.mark.skipif(not PDFLATEX_PATH.is_file(), reason="TeX Live is not installed")
def test_real_tex_compiles_alpha_and_right_angle_triangle_to_png(tmp_path: Path) -> None:
    renderer = TikzRenderer(PDFLATEX_PATH, tmp_path / "artifacts")

    rendered = renderer.render(TRIANGLE_SOURCE)

    tex_path = Path(rendered.tex_path)
    png_path = Path(rendered.png_path)
    assert rendered.cache_hit is False
    assert tex_path.is_file()
    assert png_path.stat().st_size > 0
    assert tex_path.parent == tmp_path / "artifacts"
    assert png_path.parent == tmp_path / "artifacts"
    with Image.open(png_path) as image:
        assert image.width > 0
        assert image.height > 0


@pytest.mark.skipif(not PDFLATEX_PATH.is_file(), reason="TeX Live is not installed")
def test_real_tex_cache_hit_and_source_change_create_new_paths(tmp_path: Path) -> None:
    renderer = TikzRenderer(PDFLATEX_PATH, tmp_path / "artifacts")
    source = "\\begin{tikzpicture}\n\\draw (0,0) -- (1,1);\n\\end{tikzpicture}"

    first = renderer.render(source)
    second = renderer.render(source)
    changed = renderer.render(source.replace("(1,1)", "(2,1)"))

    assert first.cache_hit is False
    assert second.cache_hit is True
    assert second.tex_path == first.tex_path
    assert second.png_path == first.png_path
    assert changed.cache_hit is False
    assert changed.tex_path != first.tex_path
    assert changed.png_path != first.png_path
    assert Path(changed.png_path).stat().st_size > 0


@pytest.mark.parametrize("changed_component", ["template", "compiler"])
def test_cache_key_changes_with_template_or_compiler_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed_component: str
) -> None:
    import pymupdf

    renderer = TikzRenderer(tmp_path / "unused-pdflatex", tmp_path / "artifacts")
    renderer._compiler_version = "compiler-a"

    def fake_compile(work_dir: Path) -> bytes:
        with pymupdf.open() as document:
            document.new_page(width=20, height=20)
            document.save(work_dir / "figure.pdf")
        return b""

    monkeypatch.setattr(renderer, "_compile", fake_compile)
    source = "\\begin{tikzpicture}\n\\draw (0,0) -- (1,1);\n\\end{tikzpicture}"
    first = renderer.render(source)
    if changed_component == "template":
        monkeypatch.setattr("backend.services.tikz._TEMPLATE_VERSION", "standalone-tikz-v2")
    else:
        renderer._compiler_version = "compiler-b"

    changed = renderer.render(source)

    assert first.cache_hit is False
    assert changed.cache_hit is False
    assert changed.tex_path != first.tex_path
    assert changed.png_path != first.png_path
    assert Path(changed.png_path).stat().st_size > 0
