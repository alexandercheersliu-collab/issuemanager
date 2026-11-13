# 整卷逐题图形与同类题图形 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为整卷题目自动裁剪并持久化逐题图形，在校对、复习和同类题中展示，且 AI 变式沿用原图。

**Architecture:** 视觉切题响应增加归一化图形坐标，本地 Pillow 裁剪并把路径随 Segment、导入任务和 Question 传递。完整原页继续由 `image_path` 保存，新增 `figure_paths` 专用于逐题展示；变式只复制图形并用提示词锁定图形结构和标注。

**Tech Stack:** Python 3.12、SQLAlchemy 2、Alembic、Pillow、Streamlit、pytest、现有 OpenAI 兼容视觉服务。

**Spec:** `docs/superpowers/specs/2026-11-01-question-figures-design.md`

## Global Constraints

- 不引入新依赖，不生成新图形；变式仅沿用原图并修改数值、条件或设问。
- 坐标统一为相对页图宽高的 0–1000 `[x0, y0, x1, y1]`。
- 无图、非法坐标和裁剪失败不得阻断整卷导入；老数据回退原有原页展示。
- 保留工作区已有未提交修改；不执行 git commit，除非用户另行明确要求。
- 所有新行为先写失败测试并确认 RED，再最小实现至 GREEN。

## Review Focus

- 非法、越界、零面积或非列表坐标：跳过对应图形且不中断导入，由 Task 1/2 测试。
- 多图与跨页题：路径顺序保持页序和图序，由 Task 2/3 测试。
- 数据目录迁移：裁剪图与完整原页都能解析有效路径，由 Task 4 测试。
- 有图变式：返回相同图形路径且提示词禁止修改结构和标注，由 Task 5 测试。
- 老任务或旧错题没有 `figure_paths`：仍显示完整原页，由 Task 4/6 测试。

---

### Task 1: 视觉切题图形坐标契约

**Files:**
- Modify: `backend/services/ai/base.py`
- Test: `tests/test_ai_parsing.py`

**Interfaces:**
- Consumes: 现有 `BaseAIProvider.segment_page(image_bytes, mime_type)`。
- Produces: 返回项增加 `figure_bboxes: list`，缺省为 `[]`；其他字段保持不变。

- [ ] **Step 1: 写失败测试**
  - 构造含 `figure_bboxes: [[530, 110, 950, 490]]` 的视觉切题响应，断言解析后坐标原样保留。
  - 构造不含该字段的响应，断言返回 `figure_bboxes == []`。

- [ ] **Step 2: 验证 RED**
  - Run: `.venv/bin/python -m pytest tests/test_ai_parsing.py -k figure_bboxes -q`
  - Expected: 新响应缺少 `figure_bboxes` 或缺省断言失败。

- [ ] **Step 3: 最小实现**
  - 在 `SEGMENT_INSTRUCTION` 定义可选 `figure_bboxes`，要求只框图形及必要标注，使用 0–1000 归一化坐标。
  - 在 `segment_page` 返回字典中传递列表值；缺失或非列表值归一为 `[]`，不在此层做像素裁剪。

- [ ] **Step 4: 验证 GREEN**
  - Run: `.venv/bin/python -m pytest tests/test_ai_parsing.py -q`
  - Expected: 全部通过。

### Task 2: 本地裁剪与 Segment 图形传递

**Files:**
- Create: `backend/services/figures.py`
- Modify: `backend/services/segment.py`
- Create: `tests/test_figures.py`
- Modify: `tests/test_segment.py`

**Interfaces:**
- Consumes: Task 1 的 `figure_bboxes`。
- Produces: `crop_figures(image_path, bboxes, output_dir, *, prefix: str) -> list[str]`。
- Produces: `Segment.figure_paths: list[str]`。
- Produces: `visual_segment_page(provider, image_path, page_no, figures_dir=None)` 和 `segment_document(..., figures_dir=None)`；不传目录时不裁剪，兼容旧调用。

- [ ] **Step 1: 写裁剪失败测试**
  - 用临时 100×200 图片和 `[[0, 0, 500, 500], [500, 500, 1000, 1000]]`，断言生成两张图、尺寸分别为 50×100 和 50×100、顺序不变。
  - 用越界坐标断言裁剪被限制在图内；用零面积、非数字和畸形项断言跳过且不抛异常。

- [ ] **Step 2: 验证裁剪测试 RED**
  - Run: `.venv/bin/python -m pytest tests/test_figures.py -q`
  - Expected: 模块或函数不存在导致失败。

- [ ] **Step 3: 实现 `crop_figures` 并验证 GREEN**
  - 使用现有 Pillow，逐框校验、钳制到 0–1000、换算并裁剪；输出 PNG，目录自动创建。
  - Run: `.venv/bin/python -m pytest tests/test_figures.py -q`
  - Expected: 全部通过。

- [ ] **Step 4: 写 Segment 失败测试**
  - 视觉结果带坐标和 `figures_dir` 时，断言 `Segment.figure_paths` 指向真实裁剪图。
  - 规则/视觉对齐、跨页合并时，断言路径按原顺序保留；无坐标时为空列表。

- [ ] **Step 5: 验证 Segment RED 后实现并转 GREEN**
  - 给 `Segment` 增加默认空列表字段；视觉切题按 `页码-题序` 前缀裁剪；合并与跨页拼接传递路径。
  - Run: `.venv/bin/python -m pytest tests/test_segment.py tests/test_figures.py -q`
  - Expected: 全部通过。

### Task 3: 整卷任务与人工校对的图形归属

**Files:**
- Modify: `backend/services/document_service.py`
- Modify: `tests/test_document_import.py`
- Modify: `tests/test_document_confirm.py`

**Interfaces:**
- Consumes: Task 2 的 `Segment.figure_paths` 和 `segment_document(..., figures_dir=...)`。
- Produces: `result.segments[*]["figure_paths"] = list[str]`，供 Task 4 确认入库。

- [ ] **Step 1: 写失败测试**
  - 视觉导入完成后断言任务页图存在且对应 segment 含裁剪 `figure_paths`。
  - `merge_segments` 断言两题图形按题序合并；`split_segment` 断言起始段保留图形、后半段图形为空。

- [ ] **Step 2: 验证 RED**
  - Run: `.venv/bin/python -m pytest tests/test_document_import.py tests/test_document_confirm.py -k figure -q`
  - Expected: 新字段缺失或归属不符合断言。

- [ ] **Step 3: 最小实现**
  - `_run_import` 传 `figures_dir=work_dir / "figures"`，逐题 entry 保存路径。
  - 合并时拼接，拆分时仅首段保留；本任务不改变确认入库接口。

- [ ] **Step 4: 验证 GREEN**
  - Run: `.venv/bin/python -m pytest tests/test_document_import.py tests/test_document_confirm.py -q`
  - Expected: 全部通过。

### Task 4: 错题图形持久化与路径兼容

**Files:**
- Create: `migrations/versions/a41d9c7e2f10_add_questions_figure_paths.py`（`down_revision = "e7f2c91a3b55"`）
- Modify: `backend/models/orm.py`
- Modify: `backend/models/schemas.py`
- Modify: `backend/repositories/questions.py`
- Modify: `backend/services/question_mixins.py`
- Modify: `tests/test_migrations.py`
- Modify: `tests/test_question_service.py`
- Modify: `tests/test_document_confirm.py`

**Interfaces:**
- Consumes: Task 3 的逐题 `figure_paths`。
- Produces: `Question.figure_paths` JSON 列、`QuestionOut.figure_paths: list[str]`、`Question.resolved_figure_paths() -> list[Path]`。
- Produces: `create_manual_question(..., figure_paths: list[str] | None = None)` 和 `QuestionRepository.create(..., figure_paths=None)`。

- [ ] **Step 1: 写迁移与持久化失败测试**
  - 升级到 head 后断言 `questions.figure_paths` 存在，旧行读取为空或 `NULL` 后由模型归一为 `[]`。
  - 创建带两条图形路径的错题，断言 ORM、`QuestionOut` 和再次查询均保留顺序。
  - 模拟数据目录迁移，断言 `resolved_figure_paths` 只返回存在的有效路径。
  - 确认整卷入库存档后，断言题目保留对应 segment 的图形路径。

- [ ] **Step 2: 验证 RED**
  - Run: `.venv/bin/python -m pytest tests/test_migrations.py tests/test_question_service.py -k figure -q`
  - Expected: 列、字段或参数不存在。

- [ ] **Step 3: 最小实现**
  - 添加 Alembic JSON 列迁移和 ORM/Schema 字段；旧值用 `getattr` 与 `or []` 兼容。
  - 创建与确认入库路径传递列表；不改变 `image_path` 和更新接口的既有语义。

- [ ] **Step 4: 验证 GREEN**
  - Run: `.venv/bin/python -m pytest tests/test_migrations.py tests/test_question_service.py tests/test_document_confirm.py -q`
  - Expected: 全部通过。

### Task 5: 变式与检测后端图形契约

**Files:**
- Modify: `backend/services/question_mixins.py`
- Modify: `backend/services/detection.py`
- Modify: `tests/test_similar_fallback.py`
- Modify: `tests/test_detection.py`

**Interfaces:**
- Consumes: Task 4 的 `QuestionOut.figure_paths`。
- Produces: 生成题复制原图路径；`start_detection(...)["tested"]["figure_paths"] = list[str]`。

- [ ] **Step 1: 写失败测试**
  - 原题 `figure_paths=["figure.png"]` 时，断言生成题保留同一路径，无图题为空。
  - 拦截真实 `analyze_text` 调用，断言有图变式的 `hint` 明确要求结构、位置和字母标注不变，仅修改数值、条件或设问。
  - 同类题检测返回的 `tested` 断言含相同路径。

- [ ] **Step 2: 验证 RED**
  - Run: `.venv/bin/python -m pytest tests/test_similar_fallback.py tests/test_detection.py -k figure -q`
  - Expected: 路径缺失或生成约束未传递。

- [ ] **Step 3: 最小实现**
  - `_generated_as_question` 复制列表；`_generate_variant` 仅在有图时附加约束；bank 命中默认空列表。
  - `start_detection` 在 `tested` 中返回路径，不改检测日志或判分。

- [ ] **Step 4: 验证 GREEN**
  - Run: `.venv/bin/python -m pytest tests/test_similar_fallback.py tests/test_detection.py tests/test_detection_variety.py -q`
  - Expected: 全部通过。

### Task 6: 校对、错题本和复习的逐题图形展示

**Files:**
- Modify: `frontend/components.py`
- Modify: `frontend/pages/import_doc.py`
- Modify: `frontend/pages/review.py`
- Modify: `tests/test_import_doc_page.py`
- Create: `tests/test_question_figures_ui.py`

**Interfaces:**
- Consumes: Task 3/4 的任务及 `QuestionOut.figure_paths`。
- Produces: `render_figures(paths: list[str], *, alt_prefix: str, width: str | int = "stretch") -> None`，过滤不存在文件并逐图调用 `st.image(..., alt=...)`。

- [ ] **Step 1: 写 UI 失败测试**
  - AppTest 渲染一张临时图，断言 `render_figures` 产生一个 image 元素；空列表和不存在路径产生零个。
  - 有裁剪图的 segment 默认显示裁剪图，完整原页只在展开区；无裁剪图的老 segment 保持原页预览。
  - 复习正面显示题干和当前题图形，解析不提前展示。

- [ ] **Step 2: 验证 RED**
  - Run: `.venv/bin/python -m pytest tests/test_question_figures_ui.py tests/test_import_doc_page.py -k figure -q`
  - Expected: 辅助函数不存在或图片数量不符合断言。

- [ ] **Step 3: 最小实现**
  - 在共享组件实现 `render_figures` 并复用，不新增 HTML/CSS。
  - 校对、错题详情和复习有裁剪图时优先展示；完整原页改为 expander，无图时保留现有回退。

- [ ] **Step 4: 验证 GREEN**
  - Run: `.venv/bin/python -m pytest tests/test_question_figures_ui.py tests/test_import_doc_page.py tests/test_review_flashcard.py -q`
  - Expected: 全部通过。

### Task 7: 同类题页面图形展示

**Files:**
- Modify: `frontend/pages/notebook.py`
- Modify: `frontend/pages/tutor.py`
- Modify: `tests/test_question_figures_ui.py`

**Interfaces:**
- Consumes: Task 5 的 `tested.figure_paths` 和 Task 6 的 `render_figures`。

- [ ] **Step 1: 写失败测试**
  - AppTest 分别渲染带图检测挑战和带图相似题候选，断言题干前/附近出现对应 image 元素，无图候选不显示占位图。

- [ ] **Step 2: 验证 RED 后实现并转 GREEN**
  - 检测页在 `tested["content"]` 前渲染图形；相似题列表对每个候选渲染其 `figure_paths`。
  - Run: `.venv/bin/python -m pytest tests/test_question_figures_ui.py -q`
  - Expected: 全部通过。

### Task 8: 真实几何样例与最终验收

**Files:**
- Modify only runtime data for current `几何1.jpg` job/questions as needed; no new production files.

**Interfaces:**
- Consumes: Tasks 1–7 的完整图形链路。

- [ ] **Step 1: 应用真实数据库迁移**
  - Run: `.venv/bin/alembic upgrade head`
  - 用 SQL 检查 `questions.figure_paths` 存在。

- [ ] **Step 2: 为当前几何任务补图**
  - 排除 shell 的 `socks://` 代理后重新执行视觉切题，保留既有题干和分析，仅补充当前任务/题目的 `figure_paths`；若任务已确认，则同步更新对应错题。
  - 使用 `ReadMediaFile` 查看裁剪结果，确认包含平行六面体及标注且不包含答案解析或其他题。

- [ ] **Step 3: 验证真实页面和变式**
  - AppTest 打开当前任务，断言展示裁剪图且完整原页位于展开区。
  - 生成一道变式，检查返回图形路径相同，题干未改变图形结构或字母标注；检测返回也包含该路径。

- [ ] **Step 4: 全量验证与工作区检查**
  - Run: `.venv/bin/python -m pytest -q`
  - Run: `.venv/bin/python -m ruff check .`
  - Run: `curl -fsS -o /dev/null -w 'HTTP %{http_code}\n' http://localhost:8501`
  - Run: `git status --short`，删除本轮测试产生且不需要的文件，不触碰既有修改。
