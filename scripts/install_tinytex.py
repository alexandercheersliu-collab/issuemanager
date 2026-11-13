"""安装或检查 TinyTeX / TeX Live 2026 及 TikZ 所需宏包。

用法::

    python scripts/install_tinytex.py              # 缺失时自动下载并安装
    python scripts/install_tinytex.py --check-only  # 只检查，绝不下载或安装
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend.services.tikz import TikzRenderer  # noqa: E402

MANIFEST_PATH = PROJECT_ROOT / "deploy" / "tinytex-requirements.txt"
DEFAULT_PDFLATEX = Path("/home/anna/.local/bin/pdflatex")
SMOKE_TIKZ_SOURCE = (
    "\\begin{tikzpicture}\n"
    "\\draw (0,0) -- (1,0) -- (0,1) -- cycle;\n"
    "\\node at (0.3,0.2) {$\\alpha$};\n"
    "\\end{tikzpicture}"
)

Runner = Callable[..., subprocess.CompletedProcess[str]]


class MissingPackagesError(RuntimeError):
    def __init__(self, packages: Sequence[str]) -> None:
        self.packages = list(packages)
        super().__init__(f"Missing TeX packages: {', '.join(self.packages)}")


def load_packages(manifest_path: Path = MANIFEST_PATH) -> list[str]:
    return [
        line.strip()
        for line in manifest_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def discover_pdflatex() -> Path | None:
    configured = os.environ.get("PDFLATEX_PATH", "").strip()
    if configured:
        candidate = Path(configured).expanduser()
        return candidate if _is_executable(candidate) else None

    executable_name = "pdflatex.exe" if os.name == "nt" else "pdflatex"
    candidates = [DEFAULT_PDFLATEX]
    on_path = shutil.which(executable_name)
    if on_path:
        candidates.append(Path(on_path))
    home = Path.home()
    roots = [home / ".TinyTeX", home / "Library" / "TinyTeX"]
    for variable in ("APPDATA", "PROGRAMDATA", "LOCALAPPDATA"):
        if os.environ.get(variable):
            roots.append(Path(os.environ[variable]) / "TinyTeX")
    for root in roots:
        candidates.extend(sorted((root / "bin").glob(f"*/{executable_name}")))
    return next((path for path in candidates if _is_executable(path)), None)


def _adjacent_tool(pdflatex_path: Path, name: str) -> Path:
    if os.name == "nt":
        name += ".exe"
    return pdflatex_path.parent / name


def missing_packages(
    pdflatex_path: Path,
    packages: Sequence[str],
    *,
    runner: Runner | None = None,
) -> list[str]:
    run = runner or subprocess.run
    kpsewhich = _adjacent_tool(Path(pdflatex_path), "kpsewhich")
    missing: list[str] = []
    for package in packages:
        command = [str(kpsewhich), f"{package}.sty"]
        try:
            completed = run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
                timeout=20,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"Unable to check TeX package {package}: {exc}") from exc
        if completed.returncode != 0 or not (completed.stdout or "").strip():
            missing.append(package)
    return missing


def install_packages(
    pdflatex_path: Path,
    packages: Sequence[str],
    *,
    runner: Runner | None = None,
) -> None:
    if not packages:
        return
    run = runner or subprocess.run
    tlmgr = _adjacent_tool(Path(pdflatex_path), "tlmgr")
    command = [str(tlmgr), "install", *packages]
    try:
        completed = run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            timeout=600,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Unable to install TeX packages: {exc}") from exc
    if completed.returncode != 0:
        output = (completed.stdout or "")[-4000:].strip()
        raise RuntimeError(f"tlmgr install failed with exit code {completed.returncode}: {output}")


def check_installation(
    pdflatex_path: Path,
    packages: Sequence[str],
    artifact_dir: Path | None = None,
) -> None:
    renderer = TikzRenderer(pdflatex_path, artifact_dir or PROJECT_ROOT / "data" / "figures" / "tikz")
    renderer.check_environment()
    missing = missing_packages(pdflatex_path, packages)
    if missing:
        raise MissingPackagesError(missing)
    if artifact_dir is not None:
        renderer.render(SMOKE_TIKZ_SOURCE)
        return
    with tempfile.TemporaryDirectory(prefix="tinytex-check-") as temp_dir:
        TikzRenderer(pdflatex_path, Path(temp_dir)).render(SMOKE_TIKZ_SOURCE)


def install_tinytex() -> None:
    if os.name == "nt":
        url = "https://yihui.org/tinytex/install-bin-windows.bat"
        suffix = ".bat"
    else:
        url = "https://yihui.org/tinytex/install-bin-unix.sh"
        suffix = ".sh"
    print(f"[install] 下载官方 TinyTeX 安装脚本: {url}")
    with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310
        script = response.read()
    with tempfile.TemporaryDirectory(prefix="tinytex-installer-") as temp_dir:
        script_path = Path(temp_dir) / f"install-tinytex{suffix}"
        script_path.write_bytes(script)
        command = (
            ["cmd.exe", "/c", str(script_path)]
            if os.name == "nt"
            else ["sh", str(script_path)]
        )
        completed = subprocess.run(command, check=False, timeout=1800)
    if completed.returncode != 0:
        raise RuntimeError(f"TinyTeX installer failed with exit code {completed.returncode}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-only", action="store_true", help="只检查，不下载或安装")
    args = parser.parse_args(argv)
    packages = load_packages()

    try:
        pdflatex = discover_pdflatex()
        if pdflatex is None:
            if args.check_only:
                raise RuntimeError("pdflatex is not executable or was not found")
            install_tinytex()
            pdflatex = discover_pdflatex()
            if pdflatex is None:
                raise RuntimeError("TinyTeX installation finished but pdflatex was not found")
        print(f"[cfg] pdflatex: {pdflatex}")
        try:
            check_installation(pdflatex, packages)
        except MissingPackagesError:
            if args.check_only:
                raise
            print(f"[install] tlmgr 安装顶层宏包: {', '.join(packages)}")
            install_packages(pdflatex, packages)
            check_installation(pdflatex, packages)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"[fail] {exc}")
        return 1

    print("[ok] TeX Live 2026")
    print(f"[ok] 宏包可用: {', '.join(packages)}")
    print("[ok] 最小 TikZ 真实编译通过（生产 TikzRenderer / build_document）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
