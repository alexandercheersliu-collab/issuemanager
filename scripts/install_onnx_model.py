"""把 ChromaDB 内置嵌入模型 (all-MiniLM-L6-v2 ONNX) 安装到指定缓存目录。

ChromaDB 官方实现把模型路径写死为 ``~/.cache/chroma/onnx_models``，且下载实现
不带断点续传；国内网络下 79MB 的包经常半路超时，留下一个「大小像模像样、
哈希对不上」的坏包，导致每次启动都重新下载。

本脚本替代那一步：

1. 校验已有包，命中官方 SHA256 直接跳过；
2. 未命中则带 ``Range`` 断点续传下载（可反复执行，接着上次的进度下）；
3. 下载中先写 ``*.part``，校验通过才原子改名成 ``onnx.tar.gz``；
4. 解压出 ``onnx/`` 目录，最后做一次真实嵌入自检。

用法::

    python scripts/install_onnx_model.py                     # 使用 CHROMA_MODEL_DIR / 内置默认
    python scripts/install_onnx_model.py --dir D:\\path\\onnx  # 显式指定缓存父目录
    python scripts/install_onnx_model.py --check-only         # 只校验，不下载
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
import tarfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# httpx/httpcore 不认 curl 风格的 socks:// 代理，统一改写为 socks5://
#（与 bootstrap.normalize_proxy_scheme 相同；本脚本不 import bootstrap，
#  避免其 apply() 改写 HOME 导致模型落到 data/home 下的默认缓存）
for _var in ("ALL_PROXY", "all_proxy"):
    _value = os.environ.get(_var)
    if _value and _value.startswith("socks://"):
        os.environ[_var] = "socks5://" + _value.removeprefix("socks://")

MODEL_NAME = "all-MiniLM-L6-v2"
ARCHIVE_FILENAME = "onnx.tar.gz"
EXTRACTED_FOLDER_NAME = "onnx"
MODEL_DOWNLOAD_URL = (
    "https://chroma-onnx-models.s3.amazonaws.com/all-MiniLM-L6-v2/onnx.tar.gz"
)
MODEL_SHA256 = "913d7300ceae3b2dbc2c50d1de4baacab4be7b9380491c27fab7418616a16ec3"

ONNX_FILES = (
    "config.json",
    "model.onnx",
    "special_tokens_map.json",
    "tokenizer_config.json",
    "tokenizer.json",
    "vocab.txt",
)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_target(explicit: str | None) -> Path:
    """返回模型目录（…/all-MiniLM-L6-v2）。"""
    if explicit:
        parent = Path(explicit)
    else:
        import chromadb.utils.embedding_functions as ef  # noqa: PLC0415

        try:
            from backend.config import get_settings  # noqa: PLC0415

            settings = get_settings()
            parent = settings.chroma_model_dir or ef.ONNXMiniLM_L6_V2.DOWNLOAD_PATH.parent
        except Exception:  # noqa: BLE001 - 配置不可用时退回 ChromaDB 默认位置
            parent = ef.ONNXMiniLM_L6_V2.DOWNLOAD_PATH.parent
    parent = Path(parent).expanduser()
    # 允许直接传 …/all-MiniLM-L6-v2 这一层
    return parent if parent.name == MODEL_NAME else parent / MODEL_NAME


def is_installed(model_dir: Path) -> bool:
    extracted = model_dir / EXTRACTED_FOLDER_NAME
    return all((extracted / name).is_file() for name in ONNX_FILES)


def verify_archive(path: Path) -> bool:
    return path.is_file() and sha256_of(path) == MODEL_SHA256


def download(url: str, dest: Path, *, attempts: int = 200, timeout: float = 180.0) -> None:
    """带断点续传的下载；已存在的 ``.part`` 会被接着写。

    反复执行本函数是安全的：断线、Ctrl-C 甚至被杀进程，下次都会从 ``.part``
    已有字节继续，直到整包 SHA256 命中官方值才原子改名。
    """
    import httpx  # noqa: PLC0415

    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    if dest.exists() and not verify_archive(dest):
        print(f"[warn] 已有包哈希不符，先移走: {dest}")
        dest.replace(dest.with_suffix(dest.suffix + ".bad"))

    for attempt in range(1, attempts + 1):
        offset = part.stat().st_size if part.exists() else 0
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        try:
            with httpx.stream("GET", url, headers=headers, timeout=timeout) as resp:
                supports_resume = resp.status_code == 206
                if offset and not supports_resume:
                    print("[info] 服务端不支持断点续传，重新开始")
                    offset = 0
                resp.raise_for_status()
                declared = int(resp.headers.get("content-length") or 0)
                total = offset + declared
                if supports_resume and "bytes" in (resp.headers.get("content-range") or ""):
                    total = int(resp.headers["content-range"].split("/")[-1] or 0) or total
                mode = "ab" if offset else "wb"
                started, last_report = time.monotonic(), 0.0
                with part.open(mode) as fh:
                    written = offset
                    for chunk in resp.iter_bytes(1 << 16):
                        fh.write(chunk)
                        written += len(chunk)
                        now = time.monotonic()
                        if now - last_report >= 10:
                            last_report = now
                            pct = written / total * 100 if total else 0.0
                            rate = (written - offset) / max(now - started, 1e-6) / 1024
                            print(
                                f"[dl] {written / 1e6:.1f}/{total / 1e6:.1f} MB "
                                f"({pct:.1f}%) {rate:.0f} KB/s",
                                flush=True,
                            )
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # noqa: BLE001 - 网络抖动继续重试
            print(f"[retry {attempt}/{attempts}] {type(exc).__name__}: {exc}", flush=True)
            time.sleep(min(5 * attempt, 30))
            continue

        if verify_archive(part):
            part.replace(dest)
            print(f"[ok] 下载并校验通过: {dest}")
            return
        print(f"[retry {attempt}/{attempts}] 本段结束但整包哈希仍不符，继续续传", flush=True)
    raise SystemExit("下载重试次数用尽，请稍后重跑本脚本（会自动续传）")


def extract(archive: Path, model_dir: Path) -> None:
    with tarfile.open(archive, "r:gz") as tar:
        if sys.version_info >= (3, 12):
            tar.extractall(path=model_dir, filter="data")
        else:
            tar.extractall(path=model_dir)
    print(f"[ok] 已解压到: {model_dir / EXTRACTED_FOLDER_NAME}")


def self_check() -> None:
    from backend.config import get_settings  # noqa: PLC0415
    from backend.services.rag import QuestionVectorStore  # noqa: PLC0415

    settings = get_settings()
    fn = QuestionVectorStore(settings)._build_embedding_fn(  # noqa: SLF001
        __import__("chromadb.utils.embedding_functions", fromlist=["x"])
    )
    vectors = fn(["一元二次方程的求根公式是什么"])
    print(f"[ok] 嵌入自检通过，维度 = {len(vectors[0])}")


def main() -> int:
    # Windows en-US 控制台（cp1252）打印中文会 UnicodeEncodeError，降级替换保证不崩
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001 - 非文本流时忽略
            pass

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", default=None, help="模型缓存父目录（默认取 CHROMA_MODEL_DIR）")
    parser.add_argument("--check-only", action="store_true", help="只校验/解压，不下载")
    parser.add_argument("--skip-self-check", action="store_true")
    args = parser.parse_args()

    model_dir = resolve_target(args.dir)
    archive = model_dir / ARCHIVE_FILENAME
    print(f"[cfg] 模型目录: {model_dir}")

    if is_installed(model_dir):
        print("[ok] 模型已就绪，无需处理")
        return 0

    if not verify_archive(archive):
        if args.check_only:
            print(f"[fail] 缺少可用模型包: {archive}")
            return 1
        download(MODEL_DOWNLOAD_URL, archive)
    else:
        print("[ok] 已有模型包校验通过")

    extract(archive, model_dir)
    if not is_installed(model_dir):
        print("[fail] 解压后仍缺少模型文件")
        return 1
    if not args.skip_self_check:
        self_check()
    print("[done] ONNX 嵌入模型安装完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
