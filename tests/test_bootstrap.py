"""bootstrap.py 环境变量注入测试。

用子进程验证：导入 bootstrap 后 HOME/USERPROFILE/HF_HOME/TORCH_HOME/
XDG_CACHE_HOME 全部指向 <PROJECT_ROOT>/data/...，且 Path.home() 跟随变化。
子进程方式可避免污染当前 pytest 进程的真实环境。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = PROJECT_ROOT / "data"

_PROBE = """
import os
from pathlib import Path
import bootstrap  # noqa: F401
print(Path.home())
for name in ("HOME", "USERPROFILE", "HF_HOME", "TORCH_HOME", "XDG_CACHE_HOME"):
    print(f"{name}={os.environ[name]}")
"""


def _run_bootstrap_probe(extra_env: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"子进程失败:\n{result.stderr}"
    lines = result.stdout.strip().splitlines()
    out = {"home": lines[0]}
    for line in lines[1:]:
        key, _, value = line.partition("=")
        out[key] = value
    return out


def test_bootstrap_redirects_home_and_caches():
    out = _run_bootstrap_probe()
    assert Path(out["home"]) == DATA_ROOT / "home"
    assert Path(out["HOME"]) == DATA_ROOT / "home"
    assert Path(out["USERPROFILE"]) == DATA_ROOT / "home"
    assert Path(out["HF_HOME"]) == DATA_ROOT / "models" / "huggingface"
    assert Path(out["TORCH_HOME"]) == DATA_ROOT / "models" / "torch"
    assert Path(out["XDG_CACHE_HOME"]) == DATA_ROOT / "cache"


def test_bootstrap_overrides_preexisting_env():
    """即使外部环境已设置这些变量（指向 C 盘），bootstrap 也必须覆盖。"""
    fake = "C:\\Users\\somebody" if os.name == "nt" else "/home/somebody"
    out = _run_bootstrap_probe(
        {
            "HOME": fake,
            "USERPROFILE": fake,
            "HF_HOME": str(Path(fake) / ".cache" / "huggingface"),
            "TORCH_HOME": str(Path(fake) / ".cache" / "torch"),
            "XDG_CACHE_HOME": str(Path(fake) / ".cache"),
        }
    )
    assert Path(out["home"]) == DATA_ROOT / "home"
    assert Path(out["HF_HOME"]) == DATA_ROOT / "models" / "huggingface"


def test_bootstrap_creates_directories():
    _run_bootstrap_probe()
    for sub in ("home", "models/huggingface", "models/torch", "cache"):
        assert (DATA_ROOT / sub).is_dir(), f"目录未创建: data/{sub}"
