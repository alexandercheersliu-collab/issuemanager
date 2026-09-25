# 验收核查（对照 goal.md §4.4）

核查日期：2026-09-26　分支：`feature/k12-errorbook`

自动化证据：`pytest tests/test_bootstrap.py tests/test_config_paths.py tests/test_share_card.py tests/test_migrate_data.py` → **18 passed**。

## 逐条结论

### ① 全新环境部署不向 C 盘写入任何模型 / 缓存 / 数据 —— ✅ 通过

- 所有运行时路径默认落在项目内：`DATA_DIR` / `CHROMA_DIR` 默认 `PROJECT_ROOT/data`，
  SQLite 默认 `{DATA_DIR}/math_tutor.db`，上传文件落 `{DATA_DIR}/uploads`。
- 启动时 `bootstrap` 预创建目录并输出实际生效路径，便于审计。
- 证据：`tests/test_bootstrap.py`、`tests/test_config_paths.py`（断言默认路径不含用户目录 / 系统盘，全部在项目内或配置的绝对路径下）。

### ② 修改 .env DATA_DIR 后所有数据落新路径 —— ✅ 通过

- `DATA_DIR` / `CHROMA_DIR` / `CHROMA_MODEL_DIR` / `DOC_UPLOAD_PATH` 均为绝对路径配置项，
  经 `_expand_paths` validator 统一展开，DB、uploads、Chroma、模型缓存随之整体迁移。
- 已有部署的数据搬迁用 `scripts/migrate_data.py --new-data-dir <绝对路径>`：
  复制 + 逐项校验（文件数 / 字节数 / DB 表行数），不删旧数据，`--dry-run` 可预演。
- 证据：`tests/test_config_paths.py`（改 DATA_DIR 后断言落点）、`tests/test_migrate_data.py`（复制+校验往返，含 DB 行数 `{"questions": 2, "users": 1}` 比对）。

### ③ ChromaDB 模型下载到 CHROMA_MODEL_DIR —— ✅ 通过

- 未配置远程 `EMBEDDING_*` 时内置 `all-MiniLM-L6-v2` 落 `CHROMA_MODEL_DIR`
  （默认 `{DATA_DIR}/models/onnx`），而非 Chroma 官方默认的 `~/.cache/chroma`。
- `scripts/install_onnx_model.py` 支持断点续传 + SHA256 校验，弱网友好。
- 证据：`tests/test_config_paths.py`、`tests/test_bootstrap.py`。

### ④ 分享卡片字体路径可配置，无 C:\Windows\Fonts 硬编码 —— ✅ 通过（含说明）

- `SHARE_CARD_FONT_PATH` 配置项**优先级最高**（`backend/services/share_card.py` `_font_candidates`）。
- 说明（如实登记）：代码中仍保留 `C:\Windows\Fonts\msyh*.ttc` 等字符串，
  但仅作为 `sys.platform` 探测后的**平台回退候选**（Windows / macOS / Linux 各一组），
  全部不可用时最终回退 PIL 默认字体——不构成对 C 盘路径的依赖，满足「可配置、无硬编码依赖」的验收意图。
- 证据：`tests/test_share_card.py`（字体回退链用例）。

### ⑤ 提供 setup_env.ps1 / setup_env.sh 一键脚本 —— ✅ 通过

- `scripts/setup_env.ps1`（3509 B）与 `scripts/setup_env.sh`（2684 B）均存在，
  覆盖全新环境的依赖安装、`.env` 初始化与数据目录预建。

## 补充说明

- 全量回归：`pytest -m "not e2e"` → **325 passed, 5 deselected**；`ruff check .` 全绿。
- e2e（`tests/e2e/`，Playwright）因当前环境未安装 `playwright` 未执行；
  安装后需自行运行 `pytest -m e2e` 验证。
- 语音模块（原规划 4.1）已登记延后，不在本次验收范围，见 `docs/UPSTREAM_DIFF.md` P4 节。
