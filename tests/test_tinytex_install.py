from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import deploy.launcher as launcher
from backend.config import Settings

PROJECT_ROOT = Path(__file__).resolve().parent.parent
INSTALLER_PATH = PROJECT_ROOT / "scripts" / "install_tinytex.py"
PDFLATEX_PATH = Path("/home/anna/.local/bin/pdflatex")


def _load_installer():
    spec = importlib.util.spec_from_file_location("install_tinytex_under_test", INSTALLER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _fake_compiler(tmp_path: Path, version_output: str) -> Path:
    compiler = tmp_path / "pdflatex"
    compiler.write_text(
        f"#!{sys.executable}\nprint({version_output!r})\n",
        encoding="utf-8",
    )
    compiler.chmod(0o755)
    return compiler


def test_manifest_contains_only_top_level_tikz_packages() -> None:
    installer = _load_installer()

    assert installer.load_packages() == ["standalone", "pgf", "amsmath"]


def test_current_machine_pdflatex_is_discoverable() -> None:
    installer = _load_installer()

    assert installer.discover_pdflatex() == PDFLATEX_PATH


@pytest.mark.parametrize(
    "version_output",
    [
        "pdfTeX 3.141592653-2.6-1.40.28 (TeX Live 2025)",
        "pdfTeX version unknown",
    ],
)
def test_self_check_rejects_wrong_or_unrecognized_tex_live_version(
    tmp_path: Path, version_output: str
) -> None:
    installer = _load_installer()
    compiler = _fake_compiler(tmp_path, version_output)

    with pytest.raises(RuntimeError, match="TeX Live|version"):
        installer.check_installation(compiler, ["standalone", "pgf", "amsmath"], tmp_path / "out")


def test_missing_packages_returns_only_unresolved_manifest_entries(tmp_path: Path) -> None:
    installer = _load_installer()
    commands: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if command[-1] == "pgf.sty":
            return subprocess.CompletedProcess(command, 1, stdout="")
        return subprocess.CompletedProcess(command, 0, stdout=f"/tex/{command[-1]}\n")

    missing = installer.missing_packages(
        tmp_path / "pdflatex", ["standalone", "pgf", "amsmath"], runner=fake_run
    )

    assert missing == ["pgf"]
    assert [command[-1] for command in commands] == ["standalone.sty", "pgf.sty", "amsmath.sty"]


def test_package_install_command_contains_every_manifest_package(tmp_path: Path) -> None:
    installer = _load_installer()
    commands: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="")

    installer.install_packages(
        tmp_path / "pdflatex", ["standalone", "pgf", "amsmath"], runner=fake_run
    )

    assert commands == [
        [str(tmp_path / "tlmgr"), "install", "standalone", "pgf", "amsmath"]
    ]


def test_check_only_never_invokes_installers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    installer = _load_installer()
    monkeypatch.setattr(installer, "discover_pdflatex", lambda: PDFLATEX_PATH)
    monkeypatch.setattr(installer, "check_installation", lambda *args: None)

    def unexpected_install(*args: object, **kwargs: object) -> None:
        raise AssertionError("check-only attempted installation")

    monkeypatch.setattr(installer, "install_tinytex", unexpected_install)
    monkeypatch.setattr(installer, "install_packages", unexpected_install)

    assert installer.main(["--check-only"]) == 0


def test_check_only_missing_packages_never_invokes_package_install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    installer = _load_installer()
    compiler = _fake_compiler(tmp_path, "pdfTeX 3.141592653 (TeX Live 2026)")
    monkeypatch.setattr(installer, "discover_pdflatex", lambda: compiler)

    def missing_packages(*args: object) -> None:
        raise installer.MissingPackagesError(["pgf"])

    def unexpected_install(*args: object, **kwargs: object) -> None:
        raise AssertionError("check-only attempted package installation")

    monkeypatch.setattr(installer, "check_installation", missing_packages)
    monkeypatch.setattr(installer, "install_tinytex", unexpected_install)
    monkeypatch.setattr(installer, "install_packages", unexpected_install)

    assert installer.main(["--check-only"]) == 1


def test_check_only_missing_compiler_does_not_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installer = _load_installer()
    monkeypatch.setattr(installer, "discover_pdflatex", lambda: None)

    def unexpected_install(*args: object, **kwargs: object) -> None:
        raise AssertionError("check-only attempted installation")

    monkeypatch.setattr(installer, "install_tinytex", unexpected_install)
    monkeypatch.setattr(installer, "install_packages", unexpected_install)

    assert installer.main(["--check-only"]) == 1


def test_default_mode_installs_tinytex_only_when_compiler_is_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    installer = _load_installer()
    compiler = _fake_compiler(tmp_path, "pdfTeX 3.141592653 (TeX Live 2026)")
    discoveries = iter([None, compiler])
    installs: list[str] = []
    monkeypatch.setattr(installer, "discover_pdflatex", lambda: next(discoveries))
    monkeypatch.setattr(installer, "install_tinytex", lambda: installs.append("tinytex"))
    monkeypatch.setattr(installer, "check_installation", lambda *args: None)

    assert installer.main([]) == 0
    assert installs == ["tinytex"]


def test_settings_pdflatex_defaults_and_blank_env_use_current_machine_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PDFLATEX_PATH", "")

    settings = Settings(_env_file=None)

    assert settings.pdflatex_path == PDFLATEX_PATH
    assert settings.tikz_timeout_seconds == 20.0


def test_settings_pdflatex_accepts_and_resolves_absolute_path(tmp_path: Path) -> None:
    compiler = tmp_path / "tex" / "pdflatex"

    settings = Settings(_env_file=None, pdflatex_path=compiler)

    assert settings.pdflatex_path == compiler.resolve()


@pytest.mark.parametrize("timeout_seconds", [0, -1, 120.1])
def test_settings_rejects_tikz_timeout_outside_supported_range(
    timeout_seconds: float,
) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, tikz_timeout_seconds=timeout_seconds)


def test_launcher_injects_only_executable_pdflatex_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    missing = tmp_path / "missing"
    directory = tmp_path / "directory"
    directory.mkdir()
    non_executable = tmp_path / "not-executable"
    non_executable.write_text("", encoding="utf-8")
    executable = tmp_path / "pdflatex"
    executable.write_text("", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setenv("PDFLATEX_PATH", "")

    selected = launcher.inject_pdflatex_env([missing, directory, non_executable, executable])

    assert selected == executable
    assert launcher.os.environ["PDFLATEX_PATH"] == str(executable)


def test_launcher_leaves_pdflatex_unset_when_no_candidate_is_executable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    non_executable = tmp_path / "pdflatex"
    non_executable.write_text("", encoding="utf-8")
    monkeypatch.setenv("PDFLATEX_PATH", "")

    selected = launcher.inject_pdflatex_env([non_executable, tmp_path / "missing"])

    assert selected is None
    assert "PDFLATEX_PATH" not in launcher.os.environ


def _make_executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_windows_install_uses_official_batch_url_and_cmd_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installer = _load_installer()
    monkeypatch.setattr(installer, "os", SimpleNamespace(name="nt"))
    requested_urls: list[str] = []
    commands: list[list[str]] = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            return b"@echo off\r\n"

    def fake_urlopen(url: str, *, timeout: int) -> FakeResponse:
        requested_urls.append(url)
        assert timeout == 60
        return FakeResponse()

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(installer.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(installer.subprocess, "run", fake_run)

    installer.install_tinytex()

    assert requested_urls == ["https://yihui.org/tinytex/install-bin-windows.bat"]
    assert len(commands) == 1
    assert commands[0][:2] == ["cmd.exe", "/c"]
    assert commands[0][2].endswith("install-tinytex.bat")


@pytest.mark.parametrize("environment_name", ["APPDATA", "PROGRAMDATA"])
def test_installer_discovers_windows_standard_tex_roots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, environment_name: str
) -> None:
    installer = _load_installer()
    home = tmp_path / "home"
    tex_root = tmp_path / environment_name.lower()
    compiler = _make_executable(
        tex_root / "TinyTeX" / "bin" / "windows" / "pdflatex.exe"
    )
    monkeypatch.setattr(
        installer,
        "os",
        SimpleNamespace(name="nt", environ=os.environ, access=os.access, X_OK=os.X_OK),
    )
    monkeypatch.setattr(installer, "DEFAULT_PDFLATEX", tmp_path / "missing")
    monkeypatch.setattr(installer.shutil, "which", lambda name: None)
    monkeypatch.setattr(installer.Path, "home", lambda: home)
    monkeypatch.setenv("PDFLATEX_PATH", "")
    for name in ("APPDATA", "PROGRAMDATA", "LOCALAPPDATA"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(environment_name, str(tex_root))

    assert installer.discover_pdflatex() == compiler


@pytest.mark.parametrize("environment_name", ["APPDATA", "PROGRAMDATA"])
def test_launcher_candidates_include_windows_standard_tex_roots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, environment_name: str
) -> None:
    home = tmp_path / "home"
    tex_root = tmp_path / environment_name.lower()
    compiler = _make_executable(
        tex_root / "TinyTeX" / "bin" / "windows" / "pdflatex.exe"
    )
    monkeypatch.setattr(launcher, "os", SimpleNamespace(name="nt", environ=os.environ))
    monkeypatch.setattr(launcher, "EXE_DIR", tmp_path / "exe")
    monkeypatch.setattr(launcher, "BUNDLE_DIR", tmp_path / "bundle")
    monkeypatch.setattr(launcher.shutil, "which", lambda name: None)
    monkeypatch.setattr(launcher.Path, "home", lambda: home)
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.delenv("PROGRAMDATA", raising=False)
    monkeypatch.setenv(environment_name, str(tex_root))

    assert compiler in launcher.pdflatex_candidates()


def test_installer_discovers_macos_library_tinytex(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    installer = _load_installer()
    home = tmp_path / "home"
    compiler = _make_executable(
        home / "Library" / "TinyTeX" / "bin" / "universal-darwin" / "pdflatex"
    )
    monkeypatch.setattr(installer, "DEFAULT_PDFLATEX", tmp_path / "missing")
    monkeypatch.setattr(installer.shutil, "which", lambda name: None)
    monkeypatch.setattr(installer.Path, "home", lambda: home)
    monkeypatch.setenv("PDFLATEX_PATH", "")

    assert installer.discover_pdflatex() == compiler


def test_launcher_candidates_include_macos_library_tinytex(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    compiler = _make_executable(
        home / "Library" / "TinyTeX" / "bin" / "universal-darwin" / "pdflatex"
    )
    monkeypatch.setattr(launcher, "EXE_DIR", tmp_path / "exe")
    monkeypatch.setattr(launcher, "BUNDLE_DIR", tmp_path / "bundle")
    monkeypatch.setattr(launcher.shutil, "which", lambda name: None)
    monkeypatch.setattr(launcher.Path, "home", lambda: home)

    assert compiler in launcher.pdflatex_candidates()
