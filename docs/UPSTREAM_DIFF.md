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

---

## P1 阶段（K12 化改造 + 图片录入增强，2026-09-26）

### 修改的基座已有文件（P1）

| 文件 | 改动 | 目的 | 引入提交 |
|------|------|------|----------|
| `backend/models/orm.py` | `questions` 表新增 8 列：`subject`（默认 math，索引）/`grade`/`region`/`textbook_version`/`question_type`/`chapter`/`error_category`/`source_doc` | K12 元数据落库；source_doc 为 P2 文档输入预留 | `619b6e2` |
| `backend/models/schemas.py` | 新增 `SUBJECT_NAMES`/`ERROR_CATEGORIES`/`ErrorCategory` 常量；`QuestionAnalysis` 增加 `question_type`/`chapter`/`error_category`（枚举约束）；`QuestionOut` 增加 8 个 K12 字段，`from_orm_model` 用 `getattr` 兜底兼容轻量替身对象 | AI 输出与视图契约同步扩展 | `e338350` |
| `backend/repositories/questions.py` | `create`/`update` 接受 K12 字段；`_filtered_stmt` 新增 `subject`/`grade`/`error_category` SQL 过滤（`list_for_user`/`count_for_user` 同步） | CRUD 与筛选下推 | `e338350` |
| `backend/services/question_mixins.py` | `create_manual_question`/`analyze_and_save`/`analyze_and_save_dedup`/`update_question`/`import_user_data` 全链路透传 K12 元数据；`list_questions`/`count_for_user` 支持三维筛选；索引调用收敛到 `_upsert_index`；`dashboard_stats` 新增 `subject` 过滤（复习记录同步过滤）；新增 `subjects_for_user` | 服务层 K12 化 | `e338350` `2f5041a` `3762485` |
| `backend/services/rag.py` | `upsert_question` 新增 `subject`/`grade`/`region`/`knowledge_points`/`difficulty` 可选元数据（非空才写入） | P3 同类题元数据硬过滤基础 | `2f5041a` |
| `backend/services/ai/base.py` | 新增 `AnalysisContext` 与 `build_system_prompt()`（缺省逐字返回基座 `SYSTEM_PROMPT`）；`JSON_INSTRUCTION` 增加 `question_type`/`chapter`/`error_category`；`analyze_question`/`analyze_text` 接受 `context`；`_complete` 抽象签名增加 `system_prompt` 参数 | 提示词按 学科+年级+地区+教材版本 参数化 | `69b13f6` |
| `backend/services/ai/openai_compat.py` | `_complete` 使用传入的 `system_prompt` | Provider 同步 | `69b13f6` |
| `backend/services/ai/gemini.py` | 同上 | Provider 同步 | `69b13f6` |
| `backend/services/ai/mock.py` | `_complete` 签名同步；演示输出补 `question_type`/`chapter`/`error_category`；`analyze_text` 接受 `context` | 无 Key 演示模式跑通新字段 | `69b13f6` |
| `backend/services/job_service.py` | `submit_analyze` 携带 K12 上下文入 payload，`_run_analyze` 透传 | 异步录题上下文不丢失 | `17ca313` |
| `api/routers/questions.py` | `analyze`/`analyze/async` 新增 `subject`/`grade`/`region`/`textbook_version` 表单字段（grade 校验 1-12）；`text` 端点与 `PATCH` 支持完整 K12 元数据；`GET /questions` 新增 `subject`/`grade`/`error_category` 筛选参数 | 录入上下文 API 注入 | `17ca313` |
| `frontend/pages/tutor.py` | 拍照/手动 Tab 增加学科/年级/地区/教材版本控件（共用 `_k12_context_inputs`），手动录入另有题型/章节/错因；解析结果展示新字段 | 录入界面 K12 化 | `17ca313` |
| `frontend/pages/notebook.py` | 新增学科/年级/错因筛选（SQL 下推）；列表标题带学科·年级；清除筛选同步重置 | 错题本筛选 | `0c18332` |
| `frontend/components.py` | 新增 `k12_meta_line()`，详情视图展示 K12 摘要行 | 详情展示 | `0c18332` |
| `frontend/common.py` | `edit_question_form` 增加 K12 元数据编辑控件 | 编辑元数据 | `0c18332` |
| `frontend/pages/dashboard.py` | 多学科时顶部出现「按学科筛选」下拉 | 看板筛选 | `3762485` |
| `.env.example` | RAG 段注释写清嵌入双轨策略（远程 bge-m3 优先、本地回退） | P1.3 配置文档化 | `0198fd4` |
| `tests/test_migrations.py` | 重构出 `_alembic_config` 助手；新增 K12 迁移回填测试 | 迁移回归 | `619b6e2` |
| `tests/test_rag.py` | 新增 2 例：K12 元数据写入与 where 过滤、空值省略 | 向量元数据回归 | `2f5041a` |

### 新增文件（P1）

| 文件 | 目的 | 引入提交 |
|------|------|----------|
| `migrations/versions/c3f1a2b48d01_add_questions_k12_metadata.py` | K12 八列迁移；`subject` server_default + 兜底 UPDATE 回填旧数据 | `619b6e2` |
| `tests/test_k12_metadata.py` | 枚举校验、落库、筛选、编辑、拍照录入上下文（8 例） | `e338350` |
| `tests/test_prompt_context.py` | 提示词组装、上下文透传、非法错因枚举重试、Mock 新字段（10 例） | `69b13f6` |
| `tests/test_k12_api.py` | 同步/异步/文本/PATCH 传参落库与列表筛选（6 例） | `17ca313` |
| `tests/test_k12_dashboard.py` | 学科列表、看板筛选口径、默认行为、`k12_meta_line`（5 例） | `3762485` |

### P1 合入上游注意事项

1. **`_complete` 签名变化**：三个 Provider 的 `_complete` 增加第四个参数
   `system_prompt`（带默认值，向后兼容）；上游若新增 Provider 需遵循同一签名。
2. **`repo.update` 的 None 语义**：`None` = 不修改；`grade`/`error_category`
   目前无法通过编辑表单清空（只能改值），如需清空需引入显式哨兵值。
3. **编辑表单的错因/学科下拉**：选项来自 `schemas.ERROR_CATEGORIES` /
   `SUBJECT_NAMES` 单一事实源，上游若调整枚举需同步检查前端引用。
4. **ChromaDB 元数据写入**：`upsert_question` 对 None 字段省略不写，
   旧向量文档没有 K12 元数据键，P3 过滤实现需兼容「键缺失」。

---

## P2 阶段（文档输入 —— 整卷导入，2026-09-26；前半：解析层 + 切题管线，后半：P2.3 校对界面）

### 修改的基座已有文件（P2）

| 文件 | 改动 | 目的 | 引入提交 |
|------|------|------|----------|
| `requirements.txt` | 新增 `pymupdf>=1.24`（python-docx 基座已声明） | PDF 解析与逐页渲染 | `bbd78a4` |
| `backend/services/ai/base.py` | 新增 `SEGMENT_SYSTEM_PROMPT`/`SEGMENT_INSTRUCTION`、`BaseAIProvider.segment_page()`（视觉切题）、`extract_json_list()` | 策略 A 视觉切题基础设施 | `f2adeb5` |
| `backend/services/ai/mock.py` | `segment_page` 返回固定两题演示 | 无 Key 跑通整卷导入 | `f2adeb5` |
| `backend/services/question_mixins.py` | `create_manual_question` 补 `difficulty`/`followup_question` 透传 | 确认入库保留解构产物 | `6c9f47e` |
| `api/main.py` | 注册 `documents` 路由 | 整卷导入 API | `a48ed6a` |

### 新增文件（P2）

| 文件 | 目的 | 引入提交 |
|------|------|----------|
| `backend/services/document.py` | 文档解析层：`parse_document(path, work_dir) -> list[DocPage]`；PDF 逐页文本层 + 200DPI 渲染（文本 <20 字符判扫描版）；DOCX 段落提取 + 内嵌图落盘留 `[图片:...]` 锚点 | `b4e2285` |
| `backend/services/segment.py` | 切题管线：规则切题（题号正则 + 单调递增防小问误切）、视觉切题、双策略按题号对齐（不一致标 needs_review）、跨页拼接 | `f2adeb5` |
| `backend/services/document_service.py` | 整卷导入编排：SHA-256 去重（重复上传复用既有任务）、文档落 DOC_UPLOAD_PATH、jobs 异步管线（解析→切题→逐题解构，total/done 进度）、`confirm_import` 幂等入库（source=document、source_doc=文档名#页码） | `a48ed6a` `6c9f47e` `77d8d33` |
| `api/routers/documents.py` | `POST /api/documents/import`（202，去重）、`GET /{job_id}/segments`、`POST /{job_id}/confirm` | `a48ed6a` `77d8d33` |
| `tests/doc_samples.py` | 程序化样本：文字版/扫描版 PDF（pymupdf 内置 china-s 字体）、DOCX（含内嵌图） | `b4e2285` |
| `tests/test_document_parse.py` | 三类文档解析 + 不支持格式（5 例） | `b4e2285` |
| `tests/test_document_import.py` | 上传去重、坏输入、导入 API（5 例） | `a48ed6a` |
| `tests/test_segment.py` | 编号风格、锚点保留、双策略对齐/错位、扫描版、跨页拼接、视觉降级（11 例） | `f2adeb5` |
| `tests/test_document_pipeline.py` | 完整管线：解构产物、needs_review 传播、上下文 payload（3 例） | `6c9f47e` |
| `tests/test_document_confirm.py` | 确认入库字段、幂等、segments/confirm 端点（6 例） | `77d8d33` |

### P2 合入上游注意事项

1. **规则切题的递增约束**：题号必须单调递增才切新题（防止把「（1）小问」
   切成新题）；整页用（1）（2）（3）编号时从 1 递增可正常切分。
2. **扫描版判定阈值**：页文本层 <20 字符判扫描版（`_SCANNED_TEXT_THRESHOLD`），
   稀疏文字页（如只有一道短题的页）可能误判，下游以「规则无产出 → 视觉单路
   + needs_review」兜底，不会丢题。
3. **视觉切题成本**：当前每个有页图像的页都调一次视觉模型做校验；
   成本敏感场景可改为仅规则路产出异常时才调（留待 POC 后决定）。
4. **confirm 跳过解构失败题**：`analysis` 为 None 的 segment 不入库，
   由 P2.3 校对界面人工补齐或重解析后再确认（见下节）。
5. **DOCX 无分页**：整篇视为一页，source_doc 恒为 `文档名#1`。

### P2.3 人工校对与批量入库（2026-09-26）

#### 修改的基座已有文件（P2.3）

| 文件 | 改动 | 目的 | 引入提交 |
|------|------|------|----------|
| `backend/repositories/questions.py` | `_filtered_stmt`/`list_for_user`/`count_for_user` 新增 `source`/`source_doc` 过滤（文档名 LIKE 前缀匹配，`%`/`_`/`\` 先转义） | 错题本按来源文档筛选（SQL 下推） | `28f7738` |
| `backend/services/question_mixins.py` | `list_questions`/`count_for_user` 透传 `source`/`source_doc` | 同上 | `28f7738` |
| `frontend/pages/notebook.py` | 新增「按来源文档筛选」下拉（选项取自当前可见题的 source_doc 文档名） | 入库后按文档回看整卷题 | `28f7738` |
| `frontend/i18n.py` | 新增 `nav.import_doc`/`page.import_doc.title` | 导航文案 | `fb386ed` |
| `frontend/common.py` | `_LABEL_TO_KEY` 新增「整卷导入」 | `go_to("import_doc")` 跳转 | `fb386ed` |
| `app.py` | `_PAGES` 与 `main()` 注册整卷导入页（位于 AI 录题之后） | 页面路由 | `fb386ed` |

#### P2 新增文件的本轮扩展

| 文件 | 改动 | 引入提交 |
|------|------|----------|
| `backend/services/document_service.py` | 校对编辑：`update_segment`（白名单字段覆盖，编辑即清除 needs_review）、`merge_segments`/`split_segment`（结构变动标 needs_review）、`delete_segment`、`reanalyze_segment`（单题重走 AI 管线）；全部操作留痕 `result.edit_log`；仅 success 且未 confirm 可编辑；`confirm_import` 返回新增 `skipped`（跳过题号及原因） | `514a486` |
| `api/routers/documents.py` | 校对端点：`PATCH /{job_id}/segments/{index}`、`POST .../merge`、`POST .../split`、`DELETE .../segments/{index}`、`POST .../reanalyze`；错误映射 404/409/502 | `3257960` |

#### 新增文件（P2.3）

| 文件 | 目的 | 引入提交 |
|------|------|----------|
| `frontend/pages/import_doc.py` | 校对界面：上传（SHA-256 去重提示）→ 进度轮询 → 逐题校对（高亮 needs_review、编辑表单、合并/拆分/删除/重解析）→ 确认入库（展示 imported/skipped）；统计与标题逻辑抽为纯函数 `summarize_segments`/`segment_headline` | `fb386ed` |
| `tests/test_document_edit.py` | 服务层 8 例（编辑/合并/拆分/删除/重解析/confirm 后锁定/skipped 明细）+ API 级 4 例 | `514a486` `3257960` |
| `tests/test_source_filter.py` | source/source_doc 筛选与计数（3 例，成员断言防共享库污染） | `28f7738` |
| `tests/test_import_doc_page.py` | 界面纯函数（4 例） | `fb386ed` |

#### P2.3 合入上游注意事项

6. **confirm 返回结构变化**：P2.3 起 `confirm_import` 及 confirm 端点返回
   `{imported, skipped, already_confirmed}`（多了 skipped 明细）；上游若有调用方
   按双字段全等消费需同步调整。
7. **编辑操作的状态锁**：校对编辑仅允许 `status=success` 且未 confirm 的任务
   （409），既防止与后台解析线程的 result 写竞争，也保证已入库结果不被改。

## P3 阶段（同类题检测 + 降级联动，2026-09-26，P3.1 约束检索 + 公共种子题库）

### 修改的基座已有文件（P3.1）

| 文件 | 改动 | 目的 | 引入提交 |
|------|------|------|----------|
| `backend/config.py` | 新增 `rag_leak_threshold`（默认 0.95）/`rag_filter_oversample`（默认 10）/`rag_bank_collection`（默认 question_bank） | 防泄题阈值与超采系数常量化可配；公共库集合名可配 | `6c24407` |
| `backend/services/rag.py` | 新增 `SimilarConstraints`/`SimilarResult`/`matches_constraints`/`difficulty_window`；`constrained_similar`（K12 硬过滤 + 级联放宽 RELAX_LADDER，记录生效层级）；`similar_questions` 加防泄题过滤（semantic_search 不加）；客户端重构支持双 collection；新增 `upsert_bank_question`/`bank_size`/`similar_from_bank`；`RagHit` 加 `source`/`bank_id`/`metadata` | P3.1 全部检索能力 | `6c24407` |
| `backend/services/question_mixins.py` | `similar_questions` 重写为三级回落编排（own→bank→AI 变式），返回 `SimilarQuestionsOutcome`（可迭代/len 兼容旧列表用法）；新增 `_generate_variant`（复用 analyze_text 的 followup_question） | 召回不足兜底链路 | `d8da502` |
| `api/routers/questions.py` | `GET /{id}/similar` 新增可选约束参数 subject/grade/region/difficulty/strict（grade 1-12、difficulty 枚举校验），不传时旧行为不变 | API 约束入口 | `d8da502` |
| `frontend/pages/tutor.py` | 相似错题取 `.items`，公共题库/AI 变式命中加来源前缀标记 | 界面适配新返回结构 | `d8da502` |

### 新增文件（P3.1）

| 文件 | 目的 | 引入提交 |
|------|------|----------|
| `scripts/seed_question_bank.py` | 公共种子题库生成导入：14 类模板 × 参数变体，年级×学科 276 题（数学 1-12 年级全覆盖 + 物理/化学），确定性（seed=42）、幂等 upsert、`--dry-run` | `b23787b` |
| `tests/test_similar_constrained.py` | 约束过滤正确性、键缺失放行、难度 ±1 档、防泄题阈值（默认+可配）、级联放宽层级记录、bank 检索（9 例） | `6c24407` |
| `tests/test_similar_fallback.py` | 三级回落编排（own 足量不回落/bank 补充/generated 兜底/strict 不回落/约束透传）+ API 兼容与参数校验（7 例） | `d8da502` |
| `tests/test_seed_question_bank.py` | 生成器数量/唯一性/元数据完整性/年级覆盖/确定性/模板答案自洽/dry-run（7 例） | `b23787b` |

### P3.1 合入上游注意事项

1. **键缺失放行只能在召回后过滤**：ChromaDB where 不支持 `$exists`，
   「旧向量文档缺 K12 元数据键时该维度不过滤」无法在 where 层表达，
   因此约束过滤在超采召回后于 Python 层执行（`rag_filter_oversample` 控制
   召回池放大系数）；个人库量级下成本可忽略，上游若迁到支持 exists 的
   向量库可下推。
2. **RagHit/返回结构扩展**：`similar_questions` 服务方法返回
   `SimilarQuestionsOutcome`（实现 `__iter__`/`__len__` 兼容旧列表用法，
   但 `== []` 等全等比较会失效）；bank/generated 命中以负数 id 的伪
   `QuestionOut` 返回，`source` 字段标记来源（bank/generated）。
3. **防泄题只作用于推荐路径**：`similar_questions`/`constrained_similar`/
   `similar_from_bank` 过滤 ≥ 阈值近重复；`semantic_search`（用户主动搜索）
   不过滤，避免搜不到原题。
4. **种子题库 region 键不落库**：公共题不带地区元数据，按「键缺失放行」
   语义在地区维度恒通过；若后续引入地方卷种子题需补 region 键。

## P3.2 检测与降级联动 + P3.3 报表（2026-09-26）

### 修改的基座已有文件（P3.2/P3.3）

| 文件 | 改动 | 目的 | 引入提交 |
|------|------|------|----------|
| `backend/config.py` | 新增 `detection_pass_threshold`（默认 2）/`demotion_interval_factor`（默认 1.5） | 降级阈值与间隔系数可配 | `229084c` |
| `backend/models/orm.py` | 新建 `DetectionLog` 模型（tested_ref/tested_source/快照/result/demoted）；`Question` 加 `demotion_state` 列与 `detection_logs` relationship | 检测留痕与降级状态 | `229084c` |
| `backend/services/question_mixins.py` | `grade_review` 加回升钩子（demoted 题评 again → 回 normal）；StatsMixin 加 `detection_overview` | 回升入口①与看板数据通路 | `229084c` `d3952ed` |
| `backend/services/question_service.py` | 组合 `DetectionMixin` | 检测编排并入领域门面 | `bdaa987` |
| `backend/services/stats.py` | 新增 `detection_stats`（通过率 pending 不计；本周降级数周一起算） | P3.3 报表口径 | `d3952ed` |
| `api/main.py` | 注册 `detection` 路由 | 检测 API | `bdaa987` |
| `frontend/pages/notebook.py` | 错题详情加「同类题检测」区块（发起/作答/判分结果/降级状态）；`describe_detection_outcome` 纯函数 | 检测界面入口 | `d3952ed` |
| `frontend/pages/dashboard.py` | 看板新增「同类题通过率」「本周降级错题数」两张统计卡 | P3.3 报表展示 | `d3952ed` |

### 新增文件（P3.2/P3.3）

| 文件 | 目的 | 引入提交 |
|------|------|----------|
| `migrations/versions/e7f2c91a3b55_add_detection_logs.py` | detection_logs 建表 + questions.demotion_state 列（down_revision=c3f1a2b48d01） | `229084c` |
| `backend/services/demotion.py` | 降级规则引擎：begin/record_result（pending→correct/wrong，幂等）+ 状态机（normal→demoted→回升）+ history/logs_for_user；回升=视同 again 复习（SM-2 重置） | `229084c` `bdaa987` |
| `backend/services/detection.py` | 检测编排 Mixin：`start_detection`（按题目自身 K12 元数据约束召回，三级回落）、`submit_detection_answer`（比对快照判分 + 联动）、`answers_match`/`tested_ref_for` 纯函数 | `bdaa987` |
| `api/routers/detection.py` | `POST /questions/{id}/detection/start`（201）、`POST /detection/{log_id}/answer`、`GET /questions/{id}/detection/history`；404/409/422 映射 | `bdaa987` |
| `tests/test_demotion.py` | 状态机全分支 9 例（全流转/streak 中断/阈值与系数可配/归档一致/复习 again 回升/幂等与隔离/历史） | `229084c` |
| `tests/test_detection.py` | 判分纯函数、三来源发题、端到端降级回升、API 协议（8 例） | `bdaa987` |
| `tests/test_detection_stats.py` | 统计口径（pending 不计/周边界）+ 看板通路 delta 断言 + 文案纯函数（5 例） | `d3952ed` |

### P3.2/P3.3 合入上游注意事项

1. **回升语义=一次 again 复习**：检测失败或复习 again 触及已降级题时，
   SM-2 进度重置（reps=0、10 分钟短间隔），掌握归档（reps≥3 且间隔≥21 天）
   自然失效——回升不需要单独的「退出归档」逻辑，双标准不存在。
2. **降级/回升留痕复用 review_logs**：降级写 grade="easy"、回升写
   grade="again" 的复习记录；正确率报表会把降级视为一次「记住」
   （语义上检测通过=掌握证据），上游若细分口径需知悉这一点。
3. **降级事件统计口径在 detection_logs.demoted**：「本周降级错题数」数的是
   触发降级的检测记录（同一题重复降级会各计一次——回升后再降级属于新周期）。
4. **generated 题的参考答案为即时快照**：发题时由 AI 解析生成并落
   detection_logs.tested_answer，判分只认快照；Mock 模式下为固定演示答案。
5. **判分是规范化字符串比对**（`_normalize_answer` + 全等/包含），
   非语义判分；对「解题过程不同但结果等价」的复杂情形会误判为错，
   真实场景可换 AI 判分（answers_match 是纯函数，替换点单一）。

## P4 阶段（收尾：迁移脚本 + 文档 + 验收，2026-09-26）

### 新增文件（P4）

| 文件 | 说明 |
| --- | --- |
| `scripts/migrate_data.py` | 数据整体迁移：SQLite 库 / uploads / data_dir 散落文件 / Chroma 向量库 / 内置模型缓存五组件复制到新数据目录并逐项校验（文件数、字节数、DB 表行数比对）；支持 `--dry-run`；MySQL 部署提示改用 mysqldump；不删旧数据 |
| `tests/test_migrate_data.py` | 7 个用例：计划组件覆盖、复制+校验往返（含 DB 行数断言）、缺失文件检出、dry-run 不落盘、MySQL 跳过提示、源缺失跳过、main 拒绝相对路径 |
| `docs/ACCEPTANCE.md` | 对照 goal.md §4.4 五条验收的逐条核查结论与证据 |

### 修改的基座已有文件（P4）

| 文件 | 说明 |
| --- | --- |
| `README.md` | 增补「K12 二次开发功能」节（元数据/整卷导入/检测降级/约束检索/迁移/新配置项）；「数据落在哪里」补 migrate_data.py 与 seed_question_bank.py 用法；目录结构补新脚本；路线图登记语音延后 |

### 延后登记

- **语音模块（原规划 4.1：语音录入 / 语音播报）已明确延后**，不在本分支
  交付范围；README 路线图与本节均作登记，后续单独立项时再启。

### P4 合入上游注意事项

1. **迁移脚本是运维工具，不接入运行时**：`migrate_data.py` 独立运行，
   仅依赖标准库 + 项目 config 默认值；上游合入无冲突面。
2. **e2e 未跑**：`tests/e2e/` 依赖 Playwright，当前环境未安装，
   全量回归以 `-m "not e2e"` 执行（325 passed）；装 Playwright 后
   `pytest -m e2e` 需自行验证。

## P5 阶段（拍照录入切题优先 + 错题预判，2026-09-27）

### 修改的基座已有文件（P5）

| 文件 | 说明 |
| --- | --- |
| `backend/services/ai/base.py` | `SEGMENT_SYSTEM_PROMPT` / `SEGMENT_INSTRUCTION` 增加 `likely_wrong` 错题预判字段（卷面有作答痕迹且疑似被判错标 true）；`segment_page` 解析容忍字段缺失（默认 False） |
| `backend/services/ai/mock.py` | 演示切题数据带 `likely_wrong`（第 1 题 true、第 2 题 false），演示默认勾选行为 |
| `backend/services/question_mixins.py` | EntryMixin 新增 `analyze_text_and_save`：题干文本走 `analyze_text` 解构入库，一图多题共享调用方落盘的 `image_path`，source 保持 "ai"；content = 题干原文 + 分隔线 + AI 解析 |
| `frontend/pages/tutor.py` | 拍照录入改为切题优先：上传后先逐张 `segment_page`，切出 ≥2 题的图展示候选清单（标注来源图、题干摘要、🟥疑似做错徽标），`likely_wrong` 题默认勾选，确认后逐题解构入库；单题/空结果/切题异常的图自动回退原整图 `analyze_and_save_dedup` 流程；多图合并为统一候选清单；结果渲染抽出 `_render_entry_results` 两路复用 |
| `tests/test_segment.py` | 新增 3 例：`likely_wrong` 解析、字段缺失默认 False、MockProvider 演示数据 |
| `README.md` | 功能导览「AI 录题」补一句多题混拍的切题勾选流程 |

### 新增文件（P5）

| 文件 | 说明 |
| --- | --- |
| `tests/test_photo_segment.py` | 12 例：回退判定（≥2 题才走切题）、候选整形与摘要、默认勾选、多图合并、单题/异常回退、勾选题逐题入库（题目数 / source=ai / 共享原图路径 / K12 元数据）、部分勾选、空题干拒绝 |

### P5 合入上游注意事项

1. **界面层纯函数约定**：`should_use_segmentation` / `build_candidates` /
   `default_checked` / `summarize_stem` / `_plan_photo_entry` /
   `_save_checked_candidates` 均不依赖 Streamlit，测试直接 import；
   上游若改 tutor.py 请保持这一可测性边界。
2. **切题计划存 session_state（`photo_plan`）**：上传集合（文件名列表）
   变化即作废重建；确认入库或全部回退后立即弹出，避免重复入库。
3. **likely_wrong 只是预判**：仅影响勾选框默认值，不入库、不影响解析；
   模型不返回该字段时全链路按 False 处理，无破坏性变更。

## P5.1 复习完成后推荐检测（2026-09-27）

### 修改的基座已有文件（P5.1）

| 文件 | 说明 |
| --- | --- |
| `frontend/pages/review.py` | 复习完成总结新增「🎯 推荐检测」区块：本轮评分逐题记录（`review_session.items`），候选筛选纯函数 `pick_detection_candidates`（good/easy + 未降级 + 未掌握归档，最多 3 题）；候选题复用 notebook 的 `_render_detection` 页内内联完成「发起→作答→判分→降级/回升展示」，不跳页；无候选时显示鼓励文案；完成总结改存 `review_done_summary` 跨 rerun 保留（内联检测触发 rerun 不丢总结），新一轮评分时作废 |
| `backend/models/schemas.py` | `QuestionOut` 新增 `demotion_state` 字段（默认 "normal"），`from_orm_model` 用 getattr 兜底兼容轻量替身对象 |
| `README.md` | 功能导览「今日复习」补一句推荐检测 |

### 新增文件（P5.1）

| 文件 | 说明 |
| --- | --- |
| `tests/test_review_recommend.py` | 6 例：候选筛选（好评过滤 / 已降级与已掌握排除 / 全错空候选 / limit 与顺序）+ 内联检测服务层流程（好评进候选 → 发起 → 作答判分 → 连续 2 次通过触发降级 → 降级后退出候选；答错清零仍可推荐） |

### P5.1 合入上游注意事项

1. **未改降级引擎**：推荐检测只是复用 `start_detection` /
   `submit_detection_answer` 的又一入口，降级/回升语义与 notebook 完全一致。
2. **review.py 依赖 notebook.\_render_detection**：检测交互状态按题隔离
   （`det_challenge_{id}` / `det_result_{id}`），复习页与错题本同时打开同题
   检测会共享 session key——属同一用户的同一会话，语义可接受。
3. **完成总结持久化键**：`review_done_summary` 与 `review_session` 分工
   （前者仅展示、后者累计本轮），改动复习页状态机时注意两处同步。

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

## P5.2 UI 全面美化（2026-09-27）

### 修改的基座已有文件（P5.2）

| 文件 | 说明 |
| --- | --- |
| `frontend/assets/style.css` | 全面重写为 `--mm-*` CSS 变量设计系统（浅色 `:root` + 旧变量别名保留）；组件统一：按钮/tabs/expander/输入框/进度条/dropzone 虚线大卡/徽标/mm-candidate/mm-brand/mm-user-card/mm-feature-card/mm-login\_\_logo；radio 导航菜单化（隐藏圆圈、active 高亮） |
| `frontend/theme.py` | 深色模式改为同名 `--mm-*` 变量覆盖 + 原生组件补丁（header/次按钮/tabs/下拉/toggle/dropzone 按钮与说明文字）；新增 `_DARK_JS`：借 `components.html` 同源脚本把 sac 评分按钮 iframe 背景改透明、文字改浅色 |
| `.streamlit/config.toml` | `backgroundColor` 改 `#f5f7fb`（配合浅色设计系统底色） |
| `app.py` | 侧边栏导航从 `sac.menu` 换成 `st.radio`（sac iframe 深色白底且文本点击不冒泡），`_NAV_ICONS` emoji 图标 map 保持 label 语义，`go_to`/`_pending_nav` 机制不变；品牌区 `.mm-brand` 渐变 logo + 用户卡片 `.mm-user-card`（角色显示中文「教师/学生」） |
| `frontend/pages/auth.py` | 登录页居中卡片化：logo + `.mm-features` 特性卡 + tabs 居中 columns `[1,1.1,1]` |
| `frontend/common.py` | `stat_card()` 新增 `icon`/`variant` 参数（variant: accent/teal/ok/warn），向后兼容 |
| `frontend/pages/dashboard.py` | 统计卡带图标；图表区包 `st.container(border=True)`；plotly 颜色 helper `_chart_text()`/`_chart_grid()` 跟随 dark\_mode |
| `frontend/pages/tutor.py` | dropzone 通栏、标签+hint 两列（hint 改 text\_input）、K12 元数据收 expander「🎓 学科/年级/教材等补充信息（可选）」、切题候选每题包 `st.container(border=True)` |
| `frontend/pages/notebook.py` | 筛选区重排：行1 搜索+排序（+教师学生选择）、行2 三个 toggle、行3 五个筛选下拉 |
| `frontend/pages/review.py` | 闪卡居中（columns `[0.7,2.6,0.7]`）、评分按钮改 `sac.buttons` 四色大按钮（again 红/hard 橙/good 蓝/easy 绿，`variant="filled"`, `return_index=True`）、间隔预览合并一行 caption |

### P5.2 合入上游注意事项

1. **st.radio 导航的 CSS 依赖 Streamlit 1.64 react-aria DOM 结构**
   （`div[data-testid="stRadioGroup"] > div[data-selected] > label[data-testid="stRadioOption"]`）；
   升级 Streamlit 大版本时需复查隐藏圆圈与 active 高亮两条规则。
2. **sac 仅用于复习页评分按钮**：导航已弃用 sac.menu；`_DARK_JS` 的同源
   iframe 补丁只在 `dark_mode` 开启时注入，若 sac 版本更换 iframe title
   命名（`streamlit_antd_components`），补丁选择器需同步。
3. **`stat_card` 新参数向后兼容**：不传 `icon`/`variant` 时渲染与旧版一致。
4. **plotly 深色适配只覆盖 dashboard**：graph 页图表仍用默认配色，深色下
   可读性依赖全局 CSS，未逐图验证。
5. **keyboard\_shortcuts 与 sac 评分按钮**：快捷键按按钮文本点击，sac 渲染
   真实 `<button>` 理论兼容，但未实测逐键触发。

## P5.3 同类题检测：推题去重随机化 + 连续刷题流（2026-09-27）

### 修改的基座已有文件（P5.3）

| 文件 | 说明 |
| --- | --- |
| `backend/services/detection.py` | `start_detection` 候选召回 top_k 1→10（`DETECTION_CANDIDATE_TOP_K`），排除本题 detection_logs 已考过的 tested_ref 后随机抽一；新增纯函数 `pick_tested_candidate`（三级策略见下）；`submit_detection_answer` 返回新增 `threshold` 字段 |
| `backend/services/demotion.py` | `DemotionService` 新增 `tested_refs(question_id, user_id)`：本题检测历史全部 tested_ref 集合（含 pending 与 bank:/gen: 前缀） |
| `frontend/pages/notebook.py` | `_render_detection` 判分结果区：「🔄 再测一题」改为「🔄 再来一道」（直接内联发起新一轮检测，答一道→看结果→下一道连续刷题）+「🏁 结束检测」返回入口视图；新增纯函数 `detection_progress_text` 实时展示连续通过 x/阈值；达阈值降级时 st.success 明确展示；已降级后保留「再来一道」（继续巩固，答错自动回升）；review.py 推荐检测区块复用同一组件自动获得新流程 |
| `tests/test_detection.py` | `test_submit_answer_and_demotion_e2e` 改两道不同 bank 题（同答案），并断言两次推题 tested_ref 不同 |
| `tests/test_review_recommend.py` / `tests/test_detection_stats.py` | 内联检测/统计用例各补一道不同 bank 题，适配去重随机推题（第二次发起不再重推第一题） |

### 新增文件（P5.3）

| 文件 | 说明 |
| --- | --- |
| `tests/test_detection_variety.py` | 8 例：pick 纯函数（库内优先/变式兜底/全考过放宽重考/固定种子随机不总同一道）+ 服务层连续发起 4 次不重复、耗尽后第 5 次放宽有题可推 + 连续作答流（答错清零→两道新题答对→计数累计→达阈值降级）+ 进度文案 |

### P5.3 合入上游注意事项

1. **推题三级策略（pick_tested_candidate 纯函数）**：① 库内/公共题库未考过
   候选随机抽一；② 都考过则优先用本次新生成的 AI 变式（每次生成本身就是
   新题）；③ 变式也考过（内容哈希相同）则放宽从已考过的库内题随机重考——
   检测本质是巩固训练，重考优于无题可推。
2. **库内题优先于变式**：随机池不含 generated（其参考答案为 AI 即时生成，
   可靠性低于库内快照），仅作兜底；上游若想让变式进入随机池，改
   `pick_tested_candidate` 一处即可。
3. **tested_refs 含 pending**：发起即登记（未作答也算已推），放弃检测后
   同一题不会再推，直至候选耗尽走放宽；这是有意为之。
4. **界面纯函数约定**：`detection_progress_text` / `describe_detection_outcome`
   不依赖 Streamlit；「再来一道」的状态迁移（pop result → set challenge →
   rerun）在 `_render_detection` 内，改状态机时注意 challenge_key/result_key
   配对。
