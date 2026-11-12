from __future__ import annotations

import hashlib
import math
import os
import re
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

import pymupdf

_TEMPLATE_VERSION = "standalone-tikz-v1"
_DEFAULT_PDFLATEX = Path("/home/anna/.local/bin/pdflatex")
_MAX_SOURCE_BYTES = 256 * 1024
_MAX_LOG_BYTES = 64 * 1024
_MAX_OUTPUT_BYTES = 25 * 1024 * 1024
_MAX_PAGE_SIDE_POINTS = 2048
_MAX_RASTER_PIXELS = 16_000_000
_RASTER_SCALE = 2

_DOCUMENT_TEMPLATE = """\\documentclass[border=2pt]{standalone}
\\newcommand{\\DoNotLoadEpstopdf}{}
\\usepackage{tikz}
\\usepackage{amsmath}
\\usetikzlibrary{angles,quotes,calc,intersections,arrows.meta,bending}
\\begin{document}
{source}
\\end{document}
"""

_COMMAND_RE = re.compile(r"\\([A-Za-z@]+|.)", re.DOTALL)
_TIKZPICTURE_RE = re.compile(
    r"\s*\\begin\{tikzpicture\}(.*)\\end\{tikzpicture\}\s*", re.DOTALL
)
_ENVIRONMENT_RE = re.compile(r"\\(?:begin|end)\s*\{([^{}]+)\}")
_TIKZPICTURE_BEGIN_RE = re.compile(r"\\begin\s*\{\s*tikzpicture\s*\}")
_TIKZPICTURE_END_RE = re.compile(r"\\end\s*\{\s*tikzpicture\s*\}")
_TEX_VERSION_RE = re.compile(
    r"^pdfTeX\s+([^\s]+).*?\(TeX Live\s+(\d{4})\)", re.MULTILINE
)

_DANGEROUS_COMMANDS = frozenset(
    "documentclass usepackage input include includeonly openin openout read write "
    "write18 immediate closeout closein catcode csname expandafter def gdef edef xdef "
    "let futurelet newcommand renewcommand providecommand RequirePackage special "
    "pdffiledump pdf shellescape".split()
)

_ALLOWED_COMMANDS = frozenset(
    (
        "begin end draw fill filldraw path node coordinate clip useasboundingbox pic "
        "foreach matrix x y i n "
        "alpha beta gamma delta epsilon varepsilon zeta eta theta vartheta iota kappa "
        "lambda mu nu xi pi varpi rho varrho sigma varsigma tau upsilon phi varphi chi "
        "psi omega Gamma Delta Theta Lambda Xi Pi Sigma Upsilon Phi Psi Omega "
        "sin cos tan arcsin arccos arctan sinh cosh tanh log ln exp min max gcd deg det lim "
        "frac dfrac tfrac sqrt overline underline vec overrightarrow overleftarrow hat widehat tilde widetilde bar dot "
        "ddot mathrm mathbf mathit mathsf mathtt mathcal text textbf textit left right "
        "cdot times div pm mp angle triangle perp parallel in notin subset subseteq "
        "supset supseteq cup cap setminus emptyset varnothing forall exists neg land lor "
        "le leq ge geq ne neq approx sim simeq cong equiv propto infty to mapsto circ "
        "bullet ldots cdots vdots ddots sum prod int quad qquad hspace vspace "
        "displaystyle textstyle scriptstyle scriptscriptstyle scriptsize"
    ).split()
) | frozenset({"\\", ",", ";", ":", "!", "%", "$", "&", "#", "_", "{", "}"})

_ALLOWED_ENVIRONMENTS = frozenset(
    "tikzpicture scope aligned alignat cases matrix pmatrix bmatrix Bmatrix vmatrix Vmatrix".split()
)


@dataclass(frozen=True)
class RenderedFigure:
    tex_path: str
    png_path: str
    cache_hit: bool


def validate_tikz_source(source: str) -> None:
    if not isinstance(source, str) or not source.strip():
        raise ValueError("TikZ source must be a non-empty string")
    if "^^" in source:
        raise ValueError("TeX ^^ escape sequences are not allowed")
    if len(source.encode("utf-8")) > _MAX_SOURCE_BYTES:
        raise ValueError("TikZ source exceeds the 256 KiB limit")
    if "\x00" in source:
        raise ValueError("TikZ source contains a NUL byte")
    if _TIKZPICTURE_RE.fullmatch(source) is None:
        raise ValueError("TikZ source must contain exactly one complete tikzpicture")
    if len(_TIKZPICTURE_BEGIN_RE.findall(source)) != 1 or len(
        _TIKZPICTURE_END_RE.findall(source)
    ) != 1:
        raise ValueError("Nested or multiple tikzpicture environments are not allowed")

    for environment in _ENVIRONMENT_RE.findall(source):
        if environment not in _ALLOWED_ENVIRONMENTS:
            raise ValueError(f"Unsupported LaTeX environment: {environment}")

    for match in _COMMAND_RE.finditer(source):
        command = match.group(1)
        if command in _DANGEROUS_COMMANDS:
            raise ValueError(f"Dangerous LaTeX command is not allowed: \\{command}")
        if command not in _ALLOWED_COMMANDS:
            raise ValueError(f"Unknown LaTeX command is not allowed: \\{command}")


def build_document(source: str) -> str:
    validate_tikz_source(source)
    return _DOCUMENT_TEMPLATE.replace("{source}", source.strip())


class TikzRenderer:
    def __init__(
        self,
        pdflatex_path: Path,
        artifact_dir: Path,
        timeout_seconds: float = 20.0,
    ) -> None:
        self.pdflatex_path = Path(pdflatex_path).expanduser()
        self.artifact_dir = Path(artifact_dir).expanduser()
        self.timeout_seconds = float(timeout_seconds)
        self._compiler_version: str | None = None

    @classmethod
    def from_settings(cls, settings) -> TikzRenderer:
        project_data = Path(__file__).resolve().parents[2] / "data"
        data_dir = Path(getattr(settings, "data_dir", project_data))
        return cls(
            pdflatex_path=Path(getattr(settings, "pdflatex_path", _DEFAULT_PDFLATEX)),
            artifact_dir=data_dir / "figures" / "tikz",
            timeout_seconds=float(getattr(settings, "tikz_timeout_seconds", 20.0)),
        )

    def check_environment(self) -> None:
        if not self.pdflatex_path.is_file() or not os.access(self.pdflatex_path, os.X_OK):
            raise RuntimeError(f"pdflatex is not executable: {self.pdflatex_path}")
        try:
            completed = subprocess.run(
                [str(self.pdflatex_path), "--version"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                shell=False,
                timeout=min(self.timeout_seconds, 10.0),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"Unable to run pdflatex --version: {exc}") from exc
        output = completed.stdout.decode("utf-8", errors="replace")
        if completed.returncode != 0:
            raise RuntimeError(
                f"pdflatex --version failed with exit code {completed.returncode}: "
                f"{_log_summary(output)}"
            )
        match = _TEX_VERSION_RE.search(output)
        if match is None:
            raise RuntimeError("Unable to identify the pdflatex / TeX Live version")
        if match.group(2) != "2026":
            raise RuntimeError(f"TeX Live 2026 is required, found TeX Live {match.group(2)}")
        self._compiler_version = f"{match.group(1)}-TeX-Live-{match.group(2)}"

    def render(self, tikz_source: str) -> RenderedFigure:
        validate_tikz_source(tikz_source)
        if self._compiler_version is None:
            self.check_environment()
        compiler_version = self._compiler_version
        if compiler_version is None:
            raise RuntimeError("pdflatex version was not initialized")

        digest = hashlib.sha256()
        for value in (_TEMPLATE_VERSION, compiler_version, tikz_source):
            encoded = value.encode("utf-8")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
        key = digest.hexdigest()

        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        tex_path = self.artifact_dir / f"{key}.tex"
        png_path = self.artifact_dir / f"{key}.png"
        if _is_nonempty_file(tex_path) and _is_nonempty_file(png_path):
            return RenderedFigure(str(tex_path), str(png_path), cache_hit=True)

        with tempfile.TemporaryDirectory(prefix=".compile-", dir=self.artifact_dir) as temp_dir:
            work_dir = Path(temp_dir)
            work_tex = work_dir / "figure.tex"
            work_pdf = work_dir / "figure.pdf"
            work_png = work_dir / "figure.png"
            work_tex.write_text(build_document(tikz_source), encoding="utf-8")

            log_output = self._compile(work_dir)
            if not _is_nonempty_file(work_pdf):
                raise RuntimeError(
                    "pdflatex exited successfully without producing a PDF. "
                    f"Log tail:\n{_log_summary(log_output)}"
                )
            png_bytes = self._rasterize(work_pdf)
            work_png.write_bytes(png_bytes)
            os.replace(work_tex, tex_path)
            os.replace(work_png, png_path)

        return RenderedFigure(str(tex_path), str(png_path), cache_hit=False)

    def _compile(self, work_dir: Path) -> bytes:
        command = [
            str(self.pdflatex_path),
            "-no-shell-escape",
            "-interaction=nonstopmode",
            "-halt-on-error",
            "-file-line-error",
            "-output-directory=.",
            "figure.tex",
        ]
        environment = os.environ.copy()
        environment.update(
            {"openin_any": "p", "openout_any": "p", "TEXMFOUTPUT": str(work_dir)}
        )
        try:
            process = subprocess.Popen(
                command,
                cwd=work_dir,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                shell=False,
                bufsize=0,
            )
        except OSError as exc:
            raise RuntimeError(f"Unable to start pdflatex: {exc}") from exc

        output = bytearray()

        def consume_output() -> None:
            if process.stdout is None:
                return
            while True:
                chunk = process.stdout.read(8192)
                if not chunk:
                    return
                output.extend(chunk)
                if len(output) > _MAX_LOG_BYTES:
                    del output[: len(output) - _MAX_LOG_BYTES]

        reader = threading.Thread(target=consume_output, daemon=True)
        reader.start()
        try:
            returncode = process.wait(timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            process.wait()
            reader.join(timeout=2.0)
            raise RuntimeError(
                f"pdflatex timed out after {self.timeout_seconds:g} seconds. "
                f"Log tail:\n{_log_summary(bytes(output))}"
            ) from exc
        reader.join(timeout=2.0)
        if returncode != 0:
            raise RuntimeError(
                f"pdflatex failed with exit code {returncode}. "
                f"Log tail:\n{_log_summary(bytes(output))}"
            )
        return bytes(output)

    def _rasterize(self, pdf_path: Path) -> bytes:
        if pdf_path.stat().st_size > _MAX_OUTPUT_BYTES:
            raise RuntimeError("Compiled PDF exceeds the 25 MiB output limit")
        try:
            with pymupdf.open(pdf_path) as document:
                if document.page_count == 0:
                    raise RuntimeError("Compiled PDF has no pages")
                page = document[0]
                width = float(page.rect.width)
                height = float(page.rect.height)
                if not math.isfinite(width) or not math.isfinite(height):
                    raise RuntimeError("Compiled PDF has invalid page dimensions")
                if width <= 0 or height <= 0:
                    raise RuntimeError("Compiled PDF has an empty page")
                if width > _MAX_PAGE_SIDE_POINTS or height > _MAX_PAGE_SIDE_POINTS:
                    raise RuntimeError("Compiled PDF exceeds the page dimension limit")
                if width * height * _RASTER_SCALE * _RASTER_SCALE > _MAX_RASTER_PIXELS:
                    raise RuntimeError("Compiled PDF exceeds the raster size limit")
                pixmap = page.get_pixmap(matrix=pymupdf.Matrix(_RASTER_SCALE, _RASTER_SCALE), alpha=False)
                png_bytes = pixmap.tobytes("png")
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(f"Unable to rasterize compiled PDF: {exc}") from exc
        if len(png_bytes) > _MAX_OUTPUT_BYTES:
            raise RuntimeError("Rendered PNG exceeds the 25 MiB output limit")
        return png_bytes


def _is_nonempty_file(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0


def _log_summary(output: bytes | str) -> str:
    if isinstance(output, bytes):
        text = output.decode("utf-8", errors="replace")
    else:
        text = output
    summary = text[-_MAX_LOG_BYTES:].strip()
    return summary or "(no compiler output)"
