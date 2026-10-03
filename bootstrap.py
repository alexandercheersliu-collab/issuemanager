"""进程引导：在任何第三方库 import 之前注入本地化环境变量。

chromadb / huggingface_hub / torch 等库会把模型与缓存写进用户主目录
（Windows 上即 C:\\Users\\<name>\\.cache 等）。为了让应用「零 C 盘写入」，
本模块在入口文件所有其他 import 之前，把下列变量全部指向
``<PROJECT_ROOT>/data/`` 下的子目录（不存在则创建）：

- ``HOME`` / ``USERPROFILE`` → ``data/home``（``Path.home()`` 随之改变）
- ``HF_HOME``                → ``data/models/huggingface``
- ``TORCH_HOME``             → ``data/models/torch``
- ``XDG_CACHE_HOME``         → ``data/cache``

使用方式（必须在所有第三方库 import 之前）::

    import bootstrap  # noqa: F401  注入本地化环境变量，必须在其他 import 之前

注意：本模块无条件覆盖上述变量；如需自定义，请改用 ``DATA_DIR`` /
``CHROMA_MODEL_DIR`` 等应用级配置项（见 backend/config.py）。
"""
from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
DATA_ROOT = PROJECT_ROOT / "data"

ENV_TARGETS: dict[str, Path] = {
    "HOME": DATA_ROOT / "home",
    "USERPROFILE": DATA_ROOT / "home",  # Windows 上 Path.home() 以此为准
    "HF_HOME": DATA_ROOT / "models" / "huggingface",
    "TORCH_HOME": DATA_ROOT / "models" / "torch",
    "XDG_CACHE_HOME": DATA_ROOT / "cache",
}


def normalize_proxy_scheme() -> None:
    """把 curl 风格的 ``socks://`` 代理改写为 httpx/httpcore 支持的 ``socks5://``。"""
    for var in ("ALL_PROXY", "all_proxy"):
        value = os.environ.get(var)
        if value and value.startswith("socks://"):
            os.environ[var] = "socks5://" + value.removeprefix("socks://")


def apply() -> None:
    """创建目标目录并注入环境变量（幂等，可重复调用）。"""
    for name, path in ENV_TARGETS.items():
        path.mkdir(parents=True, exist_ok=True)
        os.environ[name] = str(path)
    normalize_proxy_scheme()


apply()
