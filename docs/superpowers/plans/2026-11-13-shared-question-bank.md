# Shared Question Bank Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立全系统标准题库，让学生录入复用同题题干、解析和合规图形，并在 Web 提供受权限控制的题库搜索、编辑和安全删除。

**Architecture:** SQL 保存标准题、个人引用、私有原图和快照，ChromaDB 只做相似候选召回。`QuestionBankService` 统一匹配与管理规则，拍照/整卷、Web/Mobile 都复用该服务；个人错题通过标准题引用、个人覆盖和删除快照组合展示。

**Tech Stack:** Python 3.10+、SQLAlchemy 2、Alembic、Pydantic、ChromaDB、Streamlit、FastAPI、React/TypeScript、pytest、Playwright。

**Spec:** `docs/superpowers/specs/2026-11-13-shared-question-bank-design.md`

## Global Constraints

- 学生每次拍照/整卷录入都保存自己的原图作为错因证据；命中任何标准题也不能省略。
- 学生原图、手写裁剪图、备注、错因和复习状态不得跨学生共享；标准题库原始图片不向学生展示或引用。
- 不依赖私有原图的 AI 重绘图必须共享；仍有学生引用时不得因另一学生删除错题而移除。
- 学生可删除自己的错题并同时删除私有原图/裁剪图；学生来源标准题在剩余引用数为零时连同共享 AI 图和向量清理，教师/种子来源不自动删除。
- 规范化题干精确匹配自动复用；相似匹配无论置信度多高都必须由学生明确确认。
- 教师可搜索、编辑标准题，只能删除引用数为零的标准题；服务层必须再次校验权限和引用数。
- 只有现有 `is_admin_username` 认定的 admin 可删除被引用标准题；必须预览受影响学生并二次确认。
- admin 删除被引用题时保留学生题干/解析快照、私有原图、复习记录、评论和检测记录。
- 标准题库搜索、编辑、删除仅在 Web 提供；Mobile 不增加管理导航。
- 教师明确指定的题型优先；未指定时 AI 优先选择同学科下教师已发布的适用题型。
- 不接入第三方题库，不新增班级/师生绑定，不保留旧表双读或旧内部格式兼容层。
- 沿用现有依赖，不新增外部服务；Git add/commit 仅在未来获得用户明确授权后执行，否则记录为跳过。

## Review Focus

- 数字、变量或运算符不同的数学题必须保持不同身份；Task 1 用语义差异测试固定。
- 删除预览后新增引用会使确认过期；Task 9 用引用数变化测试固定。
- 存续期间不能丢学生原图，学生主动删除又必须物理清理；孤儿标准题不得误删其他学生仍使用的共享 AI 图；Task 4、Task 5、Task 9 分别验证。
- 伪造候选 ID、标准题资源 ID 或任务 ID 不得越权；Task 5、Task 8、Task 9 覆盖。
- SQL 删除成功而向量删除失败必须可对账，不能恢复半成品标准题；Task 9 验证降级与重试。

---

### Task 1: 题干规范化与稳定身份

**Files:**
- Create: `backend/services/question_identity.py`
- Test: `tests/test_question_identity.py`

**Interfaces:**
- Produces: `normalize_stem(text: str) -> str` 和 `stem_hash(text: str) -> str`；后续所有精确匹配、唯一约束和迁移只使用这两个函数。

- [ ] **Step 1: 写失败的规范化测试**

覆盖 NFC、LaTeX 定界符、外层题号、换行与连续空白；同时断言 `x=1`/`x=2`、`A`/`a`、`x+y`/`x-y` 的哈希不同，空题干抛 `ValueError`。

- [ ] **Step 2: 验证测试因函数不存在而失败**

Run: `.venv/bin/python -m pytest tests/test_question_identity.py -q`
Expected: FAIL，缺少 `backend.services.question_identity`。

- [ ] **Step 3: 实现最小规范化**

在 `question_identity.py` 中复用 `normalize_math_delimiters`，执行 NFC、外层题号移除和空白折叠；`stem_hash` 使用 UTF-8 SHA-256。不做小写转换、数字归一或数学符号改写。

- [ ] **Step 4: 验证测试通过**

Run: `.venv/bin/python -m pytest tests/test_question_identity.py -q`
Expected: PASS。

- [ ] **Step 5: 经授权后提交**

`git add backend/services/question_identity.py tests/test_question_identity.py && git commit -m "feat: add stable question identity"`

### Task 2: 标准题、个人引用和资源权限数据模型

**Files:**
- Modify: `backend/models/orm.py`
- Modify: `backend/models/schemas.py`
- Create: `backend/repositories/question_bank.py`
- Create: `migrations/versions/2026_11_13_shared_question_bank.py`
- Test: `tests/test_question_bank_schema.py`

**Interfaces:**
- Produces ORM：`CanonicalQuestion`、`QuestionAsset`、`QuestionTypeDefinition`、`QuestionBankAudit`。
- `CanonicalQuestion.normalized_hash` 唯一；`QuestionAsset.visibility` 为 `teacher_only`/`shared`。
- `Question.canonical_id` 可空；新增私有原图、个人覆盖、`stem_snapshot`/`analysis_snapshot`/`answer_snapshot`/`metadata_snapshot`/`detached_reason`。
- Produces schemas：`CanonicalQuestionOut`、`QuestionBankCandidate(score: float, question: CanonicalQuestionOut)`、`CanonicalQuestionUpdate`、`DeleteImpactOut`。

- [ ] **Step 1: 写失败的 schema 与关系测试**

断言唯一哈希、`(subject, name)` 题型唯一、个人题必须能保存私有原图、快照字段可读写、共享/教师资源可见性可区分。

- [ ] **Step 2: 运行并确认 RED**

Run: `.venv/bin/python -m pytest tests/test_question_bank_schema.py -q`
Expected: FAIL，ORM/字段尚不存在。

- [ ] **Step 3: 实现 ORM、仓储和 Alembic 重构**

按 spec 第 4 节建表并改造 `questions`；迁移按 `stem_hash` 合并开发数据，教师来源优先，私有原图仍绑定个人记录。不增加旧表双读。

- [ ] **Step 4: 验证迁移和模型**

Run: `.venv/bin/python -m pytest tests/test_question_bank_schema.py -q && .venv/bin/python -m alembic upgrade head`
Expected: 测试 PASS，迁移成功。

- [ ] **Step 5: 经授权后提交**

`git add backend/models backend/repositories/question_bank.py migrations tests/test_question_bank_schema.py && git commit -m "feat: add shared question bank schema"`

### Task 3: 本地精确/相似匹配与标准题创建升级

**Files:**
- Create: `backend/services/question_bank.py`
- Modify: `backend/services/rag.py`
- Modify: `backend/repositories/question_bank.py`
- Test: `tests/test_question_bank_matching.py`

**Interfaces:**
- Consumes: Task 1 的 `normalize_stem`/`stem_hash` 和 Task 2 模型。
- Produces: `QuestionBankService.match_exact(stem: str) -> CanonicalQuestion | None`。
- Produces: `QuestionBankService.find_similar(stem: str, *, context: AnalysisContext, limit: int = 3) -> list[QuestionBankCandidate]`。
- Produces: `QuestionBankService.create_or_upgrade(stem: str, analysis: QuestionAnalysis, *, actor: User, context: AnalysisContext, question_type: str | None = None) -> CanonicalQuestion`。

- [ ] **Step 1: 写失败的匹配与并发测试**

验证精确命中、教师候选排在学生候选前、相似结果只返回候选；学生题须 `is_complete=True` 且题干/答案/解析非空；教师同哈希升级而非重复；两个并发创建只留一个标准题。

- [ ] **Step 2: 运行并确认 RED**

Run: `.venv/bin/python -m pytest tests/test_question_bank_matching.py -q`
Expected: FAIL，`QuestionBankService` 不存在。

- [ ] **Step 3: 实现匹配与创建/升级**

SQL 精确查询优先；ChromaDB 用标准题 SQL ID 召回，按教师优先级和相似度排序。创建/升级用唯一约束处理竞争；教师字段优先，学生创建只保存标准内容，不接收私有原图。

- [ ] **Step 4: 验证匹配测试**

Run: `.venv/bin/python -m pytest tests/test_question_bank_matching.py tests/test_question_identity.py -q`
Expected: PASS。

- [ ] **Step 5: 经授权后提交**

`git add backend/services/question_bank.py backend/services/rag.py backend/repositories/question_bank.py tests/test_question_bank_matching.py && git commit -m "feat: match and reuse canonical questions"`

### Task 4: 个人引用、展示合并与学生删除清理

**Files:**
- Modify: `backend/services/question_mixins.py`
- Modify: `backend/services/question_bank.py`
- Modify: `backend/repositories/questions.py`
- Modify: `backend/repositories/question_bank.py`
- Modify: `backend/models/schemas.py`
- Modify: `backend/services/rag.py`
- Test: `tests/test_question_reference.py`

**Interfaces:**
- Produces: `QuestionService.reference_canonical(user_id: int, canonical_id: int, *, private_image_path: str, user_tags: list[str] | None = None, match_method: str = "exact", source: str = "ai", source_doc: str | None = None) -> QuestionOut`。
- Extends: 既有 `QuestionService.delete_questions(question_ids: list[int], user_id: int) -> int`，只删除本人记录，同时清理私有文件并执行学生来源孤儿标准题清理。
- `QuestionOut` 增加 `canonical_id`、`match_method`；展示内容按“个人覆盖 > 标准题 > 删除快照”合并，原图始终取个人记录。

- [ ] **Step 1: 写失败的引用、覆盖和删除测试**

验证两个学生引用同一标准题但保留各自原图和复习状态；教师修订更新未覆盖引用；个人覆盖不变；私有原图路径不能变成共享资源。A 删除后其私有原图/裁剪图物理删除，但 B 的标准题和共享 AI 图仍可用；B 也删除后学生来源标准题、共享 AI 图和向量清理；教师/种子来源不因最后一名学生删除而自动清理。

- [ ] **Step 2: 运行并确认 RED**

Run: `.venv/bin/python -m pytest tests/test_question_reference.py -q`
Expected: FAIL，引用接口、合并规则或孤儿清理不存在。

- [ ] **Step 3: 实现引用归档、展示合并和删除生命周期**

创建个人记录时要求私有原图非空；个人标签/备注/错因不写标准题。标准题向量与个人复习数据分离，既有查询、复习和导出改用合并后的内容。删除事务锁定本人记录和相关标准题，在删除后重查引用数：仅清理 `origin=student` 且零引用的标准题；SQL 提交后删除对应私有文件和孤儿资产，越界路径拒绝，文件失败进入可重试清理。

- [ ] **Step 4: 运行引用及既有错题回归**

Run: `.venv/bin/python -m pytest tests/test_question_reference.py tests/test_question_service.py tests/test_api.py -q`
Expected: PASS。

- [ ] **Step 5: 经授权后提交**

`git add backend/services/question_mixins.py backend/services/question_bank.py backend/repositories backend/models/schemas.py backend/services/rag.py tests/test_question_reference.py && git commit -m "feat: manage canonical references and private asset lifecycle"`

### Task 5: 拍照录入复用与相似候选确认

**Files:**
- Modify: `backend/services/photo_entry.py`
- Modify: `api/routers/photo_entry.py`
- Modify: `backend/models/schemas.py`
- Test: `tests/test_photo_entry_reuse.py`
- Modify: `tests/test_photo_entry_api.py`

**Interfaces:**
- Consumes: Task 3 匹配服务和 Task 4 `reference_canonical`。
- 任务候选增加 `match_status: exact/similar/new`、`canonical_id`、`match_candidates`。
- Extends: `PhotoEntryService.confirm(job_id: str, user_id: int, candidate_ids: list[str], match_choices: dict[str, int | None] | None = None) -> None`；`match_choices` 中值 `None` 表示明确选择新题，缺少相似题键表示尚未决定。

- [ ] **Step 1: 写失败的零解析与原图证据测试**

精确命中时 `analyze_question`、`analyze_text`、`redraw_geometry` 均为零调用；学生题和教师题命中都保存本次私有原图。相似题未确认不归档，选择新题才调用 AI；伪造候选/任务/标准题 ID 返回 404 或 409。

- [ ] **Step 2: 运行并确认 RED**

Run: `.venv/bin/python -m pytest tests/test_photo_entry_reuse.py -q`
Expected: FAIL，任务没有匹配状态或复用路径。

- [ ] **Step 3: 接入匹配、复用和确认**

切题后先精确、再相似、最后新题；只对未命中及明确选择新题的条目调用 AI。复用时调用 `reference_canonical`，保留现有有限并发、稳定顺序、失败隔离和重复确认幂等。题库原图哈希只用于匹配，不向学生返回资源。

- [ ] **Step 4: 运行拍照与 API 回归**

Run: `.venv/bin/python -m pytest tests/test_photo_entry_reuse.py tests/test_photo_entry_service.py tests/test_photo_entry_api.py -q`
Expected: PASS。

- [ ] **Step 5: 经授权后提交**

`git add backend/services/photo_entry.py api/routers/photo_entry.py backend/models/schemas.py tests/test_photo_entry_reuse.py tests/test_photo_entry_api.py && git commit -m "feat: reuse bank matches during photo entry"`

### Task 6: 教师题型与整卷预发布

**Files:**
- Modify: `backend/services/question_bank.py`
- Modify: `backend/services/document_service.py`
- Modify: `backend/services/ai/base.py`
- Modify: `api/routers/documents.py`
- Modify: `frontend/pages/import_doc.py`
- Test: `tests/test_question_bank_publish.py`

**Interfaces:**
- Produces: `list_question_types(subject: str) -> list[str]` 和 `create_question_type(actor_id: int, subject: str, name: str) -> QuestionTypeDefinition`。
- Extends: `DocumentService.confirm_import(..., publish_to_bank: bool = False, question_type_overrides: dict[int, str] | None = None, match_choices: dict[int, int | None] | None = None) -> dict`；下标是 segment index，`None` 表示明确选择新题，缺少相似题键表示尚未决定。
- 教师发布只创建/升级标准题；学生确认只创建个人引用并保存私有原图。

- [ ] **Step 1: 写失败的发布和题型优先级测试**

验证学生发布抛 `PermissionError`；教师指定题型覆盖 AI；未指定时提示词包含同学科教师题型；教师同哈希题升级且学生原图不变；相似题缺少明确选择时返回 409；重复发布幂等。

- [ ] **Step 2: 运行并确认 RED**

Run: `.venv/bin/python -m pytest tests/test_question_bank_publish.py -q`
Expected: FAIL，发布和题型接口不存在。

- [ ] **Step 3: 实现教师发布、题型管理与 Web 发布控件**

在整卷校对页增加“发布到共享题库”和逐题题型选择；服务层校验教师角色。文档条目也走 Task 3 精确/候选匹配，不能把教师原图资源暴露给学生。

- [ ] **Step 4: 运行整卷回归**

Run: `.venv/bin/python -m pytest tests/test_question_bank_publish.py tests/test_document_confirm.py tests/test_document_import.py tests/test_import_concurrency.py -q`
Expected: PASS。

- [ ] **Step 5: 经授权后提交**

`git add backend/services/question_bank.py backend/services/document_service.py backend/services/ai/base.py api/routers/documents.py frontend/pages/import_doc.py tests/test_question_bank_publish.py && git commit -m "feat: let teachers publish question banks"`

### Task 7: Web/Mobile 匹配状态和相似确认界面

**Files:**
- Modify: `frontend/pages/tutor.py`
- Modify: `mobile/src/pages/Capture.tsx`
- Modify: `mobile/src/pages/BatchImport.tsx`
- Modify: `mobile/src/lib/api.ts`
- Modify: `mobile/src/types/index.ts`
- Test: `tests/test_photo_segment.py`
- Modify: `tests/e2e/test_mobile_import.py`
- Modify: `tests/e2e/test_tutor_flow.py`

**Interfaces:**
- Consumes: Task 5 的 `match_status`/`match_candidates`/`match_choices` 契约和 Task 6 文档任务契约。
- Produces: 两端一致的 `精确复用`、`相似待确认`、`新题解析` 展示及“引用此题/作为新题解析”操作。

- [ ] **Step 1: 写失败的 UI 契约与 E2E 场景**

覆盖精确复用标识、共享 AI 重绘图、相似题必须选择、明确新题、私有原图展示；Mobile 不出现题库管理入口。增加 A/B 学生依次删除：A 的私有图删除且 B 仍可看共享 AI 图，B 删除后学生来源孤儿标准题清理。先通过受控 provider 构造 exact/similar/new 三种条目。

- [ ] **Step 2: 运行并确认 RED**

Run: `.venv/bin/python -m pytest tests/test_photo_segment.py -q && MM_MOBILE_E2E=1 .venv/bin/python -m pytest tests/e2e/test_mobile_import.py -q`
Expected: 新增场景 FAIL，界面尚未实现。

- [ ] **Step 3: 实现双端界面**

Web 使用现有候选确认容器；Mobile 在现有拍照/整卷校对页内展示选择，不增加文字录入或新导航。所有选择通过共享 API 提交，不在客户端重复匹配。

- [ ] **Step 4: 验证 UI 和 Mobile 构建**

Run: `.venv/bin/python -m pytest tests/test_photo_segment.py -q && MM_MOBILE_E2E=1 .venv/bin/python -m pytest tests/e2e/test_mobile_import.py -q && MM_E2E=1 .venv/bin/python -m pytest tests/e2e/test_tutor_flow.py -q && cd mobile && npm run lint && npm run build`
Expected: 测试 PASS，共享 AI 图与学生删除场景正确，lint 0 错误，build 成功。

- [ ] **Step 5: 经授权后提交**

`git add frontend/pages/tutor.py mobile/src tests/test_photo_segment.py tests/e2e/test_mobile_import.py tests/e2e/test_tutor_flow.py && git commit -m "feat: expose question bank match decisions"`

### Task 8: Web 标准题库搜索、编辑和未引用删除

**Files:**
- Create: `frontend/pages/question_bank.py`
- Modify: `app.py`
- Modify: `frontend/i18n.py`
- Modify: `frontend/common.py`
- Modify: `backend/services/question_bank.py`
- Modify: `backend/repositories/question_bank.py`
- Test: `tests/test_question_bank_management.py`
- Create: `tests/e2e/test_question_bank.py`

**Interfaces:**
- Produces: `search_canonical(actor_id: int, *, keyword: str = "", semantic: bool = False, subject: str | None = None, grade: int | None = None, question_type: str | None = None, origin: str | None = None, limit: int = 50, offset: int = 0) -> list[CanonicalQuestionOut]`。
- Produces: `update_canonical(actor_id: int, canonical_id: int, updates: CanonicalQuestionUpdate) -> CanonicalQuestionOut`。
- Produces: `delete_unreferenced(actor_id: int, canonical_id: int) -> None`。

- [ ] **Step 1: 写失败的权限、搜索、编辑和删除测试**

验证学生/未登录访问被拒绝；教师可按筛选条件搜索并编辑；哈希冲突拒绝；被引用删除返回 409；未引用删除清理 SQL、资源和向量；伪造资源 ID 不暴露教师原图。

- [ ] **Step 2: 运行并确认 RED**

Run: `.venv/bin/python -m pytest tests/test_question_bank_management.py -q`
Expected: FAIL，管理服务与页面不存在。

- [ ] **Step 3: 实现服务和 Web 管理页**

新增教师/admin 可见导航；列表展示题干、题型、来源、更新时间和引用数，详情支持编辑与删除。教师原图只在管理详情读取，编辑审计和向量同步使用同一服务。

- [ ] **Step 4: 运行服务及 Web E2E**

Run: `.venv/bin/python -m pytest tests/test_question_bank_management.py -q && MM_E2E=1 .venv/bin/python -m pytest tests/e2e/test_question_bank.py -q`
Expected: PASS；教师完成搜索、编辑、未引用删除，学生看不到管理页。

- [ ] **Step 5: 经授权后提交**

`git add app.py frontend backend/services/question_bank.py backend/repositories/question_bank.py tests/test_question_bank_management.py tests/e2e/test_question_bank.py && git commit -m "feat: add web question bank management"`

### Task 9: admin 引用影响预览、二次确认和个人快照

**Files:**
- Modify: `backend/services/question_bank.py`
- Modify: `backend/repositories/question_bank.py`
- Modify: `frontend/pages/question_bank.py`
- Modify: `backend/services/rag.py`
- Test: `tests/test_question_bank_admin_delete.py`
- Modify: `tests/e2e/test_question_bank.py`

**Interfaces:**
- Produces: `preview_canonical_delete(actor_id: int, canonical_id: int) -> DeleteImpactOut`，返回受影响学生用户名、个人错题 ID 和引用数。
- Produces: `delete_canonical_with_snapshots(actor_id: int, canonical_id: int, *, expected_reference_count: int, confirmation: str) -> DeleteImpactOut`；确认短语固定为 `删除标准题 {canonical_id}`。

- [ ] **Step 1: 写失败的删除影响与快照测试**

验证普通教师无权限；admin 预览名单准确；错误短语和引用数变化均拒绝；删除后个人题干/解析/原图/复习/评论/检测保留且 `canonical_id=None`、`detached_reason=admin_delete`；审计完整；向量删除失败可对账重试。

- [ ] **Step 2: 运行并确认 RED**

Run: `.venv/bin/python -m pytest tests/test_question_bank_admin_delete.py -q`
Expected: FAIL，影响预览和快照删除不存在。

- [ ] **Step 3: 实现事务性快照删除与 Web 二次确认**

在 SQL 事务内重查引用、保存快照、解除引用、写审计并删除标准题；Web 对话框显示学生清单并要求输入固定短语。SQL 为权威，向量失败记录待重试状态，不回滚成半份 SQL 数据。

- [ ] **Step 4: 运行删除和 Web 场景**

Run: `.venv/bin/python -m pytest tests/test_question_bank_admin_delete.py -q && MM_E2E=1 .venv/bin/python -m pytest tests/e2e/test_question_bank.py -q`
Expected: PASS；admin 看到学生列表并完成确认，学生错题及原图随后仍可用。

- [ ] **Step 5: 经授权后提交**

`git add backend/services/question_bank.py backend/repositories/question_bank.py backend/services/rag.py frontend/pages/question_bank.py tests/test_question_bank_admin_delete.py tests/e2e/test_question_bank.py && git commit -m "feat: safely delete referenced bank questions"`

### Task 10: 种子迁移、观测、文档和整分支验收

**Files:**
- Modify: `scripts/seed_question_bank.py`
- Modify: `scripts/reindex_vectors.py`
- Modify: `scripts/migrate_data.py`
- Modify: `backend/services/ai/telemetry.py`
- Modify: `docs/ARCHITECTURE.md`
- Modify: `docs/USER_MANUAL.md`
- Modify: `docs/PERFORMANCE.md`
- Modify: `mobile/README.md`
- Test: `tests/test_seed_question_bank.py`

**Interfaces:**
- Consumes: 前九任务的标准题、匹配、管理和删除接口。
- Produces: SQL 权威种子题库、可重建向量索引、匹配/复用/管理审计指标和最终使用文档。

- [ ] **Step 1: 写失败的种子与观测测试**

验证完整种子可复用、不完整种子不会创建空解析个人题；重复种子幂等；统计精确/相似/新建及跳过的 AI 调用，不在没有真实用量时伪造 token 数。

- [ ] **Step 2: 运行并确认 RED**

Run: `.venv/bin/python -m pytest tests/test_seed_question_bank.py -q`
Expected: FAIL，种子仍只写 ChromaDB 或观测字段缺失。

- [ ] **Step 3: 更新种子、迁移、观测和文档**

种子同时落 SQL 与向量；文档明确学生原图在个人错题存续期间保存、主动删除时清理、题库原图不给学生、共享 AI 图复用、学生来源孤儿题清理、Web 管理权限和 admin 快照删除。更新截图只通过真实 Web 场景生成，不伪造运行结果。

- [ ] **Step 4: 运行完整验证**

Run:
```bash
.venv/bin/python -m ruff check backend api frontend scripts tests
.venv/bin/python -m pytest -q
MM_MOBILE_E2E=1 .venv/bin/python -m pytest tests/e2e/test_mobile_import.py -q
MM_E2E=1 .venv/bin/python -m pytest tests/e2e/test_app_e2e.py tests/e2e/test_tutor_flow.py tests/e2e/test_question_bank.py -q
cd mobile && npm run lint && npm run build
```
Expected: Ruff 0 错误，所有测试 PASS，Mobile lint/build 成功。真实模型识别质量另行人工验收，MockProvider 不作为质量证明。

- [ ] **Step 5: 检查工作区并经授权后提交**

Run: `git status --short && git -c core.whitespace=cr-at-eol diff --check`
Expected: 无测试截图、临时数据库或运行产物；只保留计划内文件。授权后提交 `docs: document shared question bank`。

## Completion Evidence

最终报告必须列出：精确命中时各类 AI/几何调用计数、相似确认行为、共享 AI 图复用、学生原图存续期保留与主动删除证明、学生来源孤儿标准题清理、教师/admin 权限矩阵、被引用删除后的学生快照与审计、全量测试和双端 E2E 结果。不得仅因 MockProvider 流程通过就声称真实模型准确率或实际 token 数量已验证。
