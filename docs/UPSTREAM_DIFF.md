# 二次开发改动登记（UPSTREAM_DIFF）

> 基座：`https://github.com/lii-lii321/Math_Tutor_RAG`，基线 HEAD `40a1d12`。
> 二开分支：`feature/k12-errorbook`。
> 本文档登记对基座**已有文件**的所有修改与**新增文件**清单，便于后续合入上游升级。
> 每处改动注明目的；合入上游时按本表逐条核对冲突。

## 修改的基座已有文件

| 文件 | 改动 | 目的 | 引入提交 |
|------|------|------|----------|
| `backend/config.py` | 新增 `data_dir`（DATA_DIR）、`chroma_model_dir`（CHROMA_MODEL_DIR）配置项；空串归一化 validator；`DATABASE_URL` 未配置时自动落 `<DATA_DIR>/math_tutor.db` 的 model_validator；`ensure_dirs()` | 运行时数据（SQLite/向量库/模型缓存/上传原图）可迁到项目外独立数据盘 | `dc76d65`（二开基线） |
| `backend/config.py` | 新增 `share_card_font_path`（SHARE_CARD_FONT_PATH，默认 None = 自动探测） | 去除分享卡片字体的 Windows 路径硬编码 | `beab95a` |
| `backend/config.py` | 新增 `doc_upload_path`（DOC_UPLOAD_PATH，默认 `<DATA_DIR>/uploads/docs`，跟随 DATA_DIR）；`ensure_dirs()` 同步建目录 | P2 整卷导入的文档上传目录可配置化 | `6402917` |
| `backend/services/rag.py` | 接入 `chroma_model_dir`，ChromaDB 内置嵌入模型缓存指向数据盘 | 模型与向量库同盘，避免写用户主目录 | `dc76d65`（二开基线） |
| `backend/services/share_card.py` | `_font()` / `has_cjk_font()` 重构为 `_font_candidates()`：配置项优先 → 按 `sys.platform` 探测（Windows 微软雅黑/黑体、macOS 苹方、Linux 文泉驿/Noto CJK）→ PIL 默认字体 | 修复 `C:\Windows\Fonts\...` 硬编码，跨平台可用 | `beab95a` |
| `frontend/pages/settings.py` | 设置页展示 DATA_DIR / CHROMA_MODEL_DIR 状态 | 部署可观测性 | `dc76d65`（二开基线） |
| `app.py` | 首行（`from __future__` 之后、所有第三方 import 之前）`import bootstrap` | 注入 HOME/HF_HOME 等环境变量，零 C 盘缓存写入 | `d0f667a` |
| `api/main.py` | 同上，首行 `import bootstrap` | 同上 | `d0f667a` |
| `mcp_server.py` | `sys.path` 修正提前，随后 `import bootstrap`（合并原有重复的 path 注入） | 同上 | `d0f667a` |
| `.env.example` | 补充 DATA_DIR / CHROMA_DIR / CHROMA_MODEL_DIR 说明 | 数据盘配置文档化 | `dc76d65`（二开基线） |
| `.env.example` | 补充 DOC_UPLOAD_PATH / SHARE_CARD_FONT_PATH 说明 | 新配置项文档化 | `6402917` |
| `tests/conftest.py` | 测试环境变量隔离（DATABASE_URL/DATA_DIR/CHROMA_DIR 指向临时目录）；ONNX 模型缓存复用逻辑 | 测试不污染真实数据目录 | `dc76d65`（二开基线） |
| `pyproject.toml` | ruff 新增 `per-file-ignores`：三个入口文件豁免 I001（import 排序） | `import bootstrap` 语义上必须置于第三方库之前，与 isort 冲突 | `52cab96` |
| `docs/DEPLOYMENT.md` | 数据盘部署说明 | 部署文档 | `dc76d65`（二开基线） |
| `README.md` | 二开说明 | 项目文档 | `dc76d65`（二开基线） |

## 新增文件（二开引入，上游不存在）

| 文件 | 目的 | 引入提交 |
|------|------|----------|
| `bootstrap.py` | 在任何第三方库 import 前注入 `HOME`/`USERPROFILE`/`HF_HOME`/`TORCH_HOME`/`XDG_CACHE_HOME`，全部指向 `<PROJECT_ROOT>/data/...` 并自动建目录 | `d0f667a` |
| `tests/test_bootstrap.py` | 子进程验证 bootstrap 重定向 `Path.home()`、覆盖外部已设变量、自动建目录 | `d0f667a` |
| `tests/test_config_paths.py` | 验证 `doc_upload_path` 默认跟随 DATA_DIR、显式覆盖、空串回退、`ensure_dirs` 建目录 | `6402917` |
| `scripts/setup_env.ps1` | Windows 一键初始化：venv → 依赖 → .env → 预下载嵌入模型（`-SkipModel` 可跳过） | `c91a740` |
| `scripts/setup_env.sh` | macOS/Linux 同款一键初始化（`--skip-model` 可跳过） | `c91a740` |
| `docs/UPSTREAM_DIFF.md` | 本文档 | 本提交 |

## 合入上游注意事项

1. **入口文件首行顺序敏感**：`app.py` / `api/main.py` / `mcp_server.py` 的
   `import bootstrap` 必须保持在所有第三方库 import 之前；上游若改动这些
   文件的 import 区，合并时勿将其挤到后面。
2. **config.py 的 validator 列表**：`_blank_means_default`（before）与
   `_expand_paths`（after）按字段名登记，新增 Optional[Path] 配置项时两个
   列表都要加，否则空串会被转成 `Path('.')`。
3. **share_card.py 字体回退链**：`has_cjk_font()` 被 `tests/test_share_card.py`
   的 skipif 在收集期调用，改动候选清单逻辑时同步检查测试跳过条件。
4. **已知基线问题（非二开引入）**：当前环境 `import mcp_server` 报
   `AttributeError: 'Server' object has no attribute 'list_tools'`，为基座
   代码与已安装 `mcp` 库版本不匹配所致，基线提交 `dc76d65` 上即可复现，
   待后续升级 mcp 依赖或适配 API 时处理。
