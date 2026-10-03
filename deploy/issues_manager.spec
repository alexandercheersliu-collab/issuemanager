# -*- mode: python ; coding: utf-8 -*-
"""Issues Manager Windows 部署包 PyInstaller spec（onedir）。

构建（在项目根目录）：
    pyinstaller deploy/issues_manager.spec --noconfirm

产物：dist/IssuesManager/（含 IssuesManager.exe 与 _internal/）。
注意：PyInstaller 以 spec 所在目录为工作目录，本文件内所有路径基于 SPECPATH 换算。
打包前准备：
  - mobile/dist 已构建（仓库内现有，或先 npm run build）
  - 嵌入模型已下载到 deploy/bundle/models/onnx（scripts/install_onnx_model.py --dir，
    CI 自动完成；无该目录时跳过，便于快速烟测）
  - 用户手册 docs/USER_MANUAL.md；PDF 由 scripts/build_user_manual_pdf.py 生成
"""
import os

from PyInstaller.utils.hooks import collect_all

block_cipher = None
ROOT = os.path.dirname(os.path.abspath(SPECPATH))  # 项目根（spec 在 <root>/deploy/ 下）


def rel(*parts: str) -> str:
    return os.path.join(ROOT, *parts)


# 需要连数据文件一起收的包（streamlit 的静态资源、chromadb 的迁移/sql 文件等）
COLLECT_PACKAGES = [
    "streamlit",
    "streamlit_antd_components",
    "chromadb",
    "plotly",
    "onnxruntime",
    "tokenizers",
    "pymupdf",
    "google.genai",
    "mcp",
    "reportlab",
    "networkx",
    "uvicorn",
    "altair",
    "pydeck",
]

datas = [
    (rel("app.py"), "."),
    (rel("bootstrap.py"), "."),
    (rel("backend"), "backend"),
    (rel("api"), "api"),
    (rel("frontend"), "frontend"),
    (rel("migrations"), "migrations"),
    (rel("alembic.ini"), "."),
    (rel(".streamlit"), ".streamlit"),
    (rel("static"), "static"),
    (rel("mobile", "dist"), "mobile/dist"),
    (rel(".env.example"), "."),
    (rel("docs", "USER_MANUAL.md"), "."),
]
binaries = []
hiddenimports = [
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
]

for package in COLLECT_PACKAGES:
    pkg_datas, pkg_binaries, pkg_hiddenimports = collect_all(package)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hiddenimports

# 预置嵌入模型与用户手册 PDF（存在才收，便于无模型烟测）
if os.path.isdir(rel("deploy", "bundle", "models", "onnx")):
    datas.append((rel("deploy", "bundle", "models", "onnx"), "models/onnx"))
if os.path.isfile(rel("dist_assets", "USER_MANUAL.pdf")):
    datas.append((rel("dist_assets", "USER_MANUAL.pdf"), "."))

a = Analysis(
    [rel("deploy", "launcher.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tests", "pytest", "pytest_cov", "locust", "playwright", "ruff", "matplotlib"],
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="IssuesManager",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,  # 配置向导与运行日志走控制台
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="IssuesManager",
)
