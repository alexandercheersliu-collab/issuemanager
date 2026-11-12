# 几何图形 TikZ 重绘与同类题配图 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将几何题配图改为“语义解析 → TikZ → pdflatex 编译 → 原图对比”，仅在失败时延迟裁剪，并为同类题生成与新题干一致的新图。

**Architecture:** `GeometryFigureService` 负责按题编排 AI、渲染、对比和兜底，`TikzRenderer` 负责安全编译与内容寻址产物。现有 `figure_paths` 继续表示有效展示图，新增 `figure_specs` 传递语义、源码和校验状态；同类题使用独立结构化生成，不再复制原图。

**Tech Stack:** Python 3.10+、Pydantic、SQLAlchemy/Alembic、Pillow、PyMuPDF、TinyTeX / TeX Live 2026、TikZ、Streamlit、pytest。

**Spec:** `docs/superpowers/specs/2026-11-02-tikz-geometry-figures-design.md`

## Global Constraints

- 当前编译器为 `/home/anna/.local/bin/pdflatex`，发行版必须是 TeX Live 2026；当前宏包已安装，不重复安装。
- 成功路径不调用 `locate_figures`，不保存裁剪图；只有重绘、编译或对比失败后才延迟定位和裁剪。
- 原题图形必须保持解题所需语义；同类题只保留考点/错点，不要求外观相同，且不得复制原 `figure_paths`。
- AI TikZ 是不可信输入：固定模板、命令白名单、`-no-shell-escape`、独立目录、超时和输出限制缺一不可。
- 缺少 TeX 或单图失败不得阻断整卷导入或应用启动；未通过校验的重绘图不得静默替代原图。
- 安装交付采用首启/安装时自动配置 TinyTeX，不离线内置 TeX；TeX Live 不写成 pip 依赖。
- 保留当前工作区已有修改；不执行 `git commit`、`git merge`、`git push` 或其他 Git 写操作。
- 每个任务先写失败测试，再写最小实现；不增加规格外功能。

## Review Focus

- TeX 缺失、版本错误或编译超时：当前图应快速兜底，其他导入和录题功能继续工作；由 Task 4、8 的测试固定。
- 非几何图、多图、空坐标或部分坐标无效：不得强行 TikZ 化，且每张图独立选择有效展示结果；由 Task 4 的测试固定。
- AI 返回文件读写、shell escape 或未知宏：必须在启动进程前拒绝；由 Task 2 的测试固定。
- 变式图与新题干矛盾，或参考答案生成丢失图形语义：应重试/跳过变式，不得误答带图题；由 Task 6 的测试固定。
- 旧题没有 `figure_specs`、数据目录迁移或导入任务合拆题：旧图仍可用，新规格不丢失、不错误复制；由 Task 1、5 的测试固定。

---

### Task 1: 图形数据契约与数据库兼容

**Files:**
- Modify: `backend/models/schemas.py`
- Modify: `backend/models/orm.py`
- Modify: `backend/repositories/questions.py`
- Create: `migrations/versions/b52e0d8a4c21_add_questions_figure_specs.py`
- Test: `tests/test_migrations.py`
- Test: `tests/test_question_service.py`

**Interfaces:**
- Produces: `GeometryFigureDraft(kind, semantic_spec, tikz_source, assumptions)`、`FigureComparison(passed, issues, repair_instruction)`、`GeometryVariant(question, figures)`、`FigureSpec(kind, semantic_spec, tikz_source, render_path, crop_path, validation_status, validation_issues, assumptions)`。
- Produces: `QuestionOut.figure_specs: list[FigureSpec]`、`Question.figure_specs` JSON、`QuestionRepository.create(..., figure_specs: list[dict] | None = None)`。

- [ ] **Step 1: 写失败测试**

在 `test_question_service.py` 增加旧 ORM 对象无 `figure_specs` 时 DTO 返回 `[]`、带规格创建/读取完整往返的测试；在 `test_migrations.py` 断言新迁移 `down_revision == "a41d9c7e2f10"`，升级后 `questions.figure_specs` 存在且旧行读出空列表。另用 `GeometryVariant(question="新题", figures=[])` 固定 AI 变式契约。

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_migrations.py tests/test_question_service.py -q`
Expected: 因缺少模型字段、ORM 列或迁移而 FAIL。

- [ ] **Step 3: 实现数据模型与迁移**

在 `schemas.py` 定义上述 Pydantic 模型；`validation_status` 仅允许 `ai_passed | manual_passed | fallback`。ORM 和仓库按现有 `figure_paths` 模式保存 JSON，路径自愈逻辑同时处理 `render_path`/`crop_path`。新迁移只增加 `figure_specs`，默认空列表，不重写既有数据。

- [ ] **Step 4: 运行聚焦测试**

Run: `.venv/bin/python -m pytest tests/test_migrations.py tests/test_question_service.py -q`
Expected: PASS，旧记录与路径迁移测试均保持通过。

### Task 2: 安全 TikZ 编译器与持久产物

**Files:**
- Create: `backend/services/tikz.py`
- Create: `tests/test_tikz.py`

**Interfaces:**
- Consumes: `backend.config.Settings`。
- Produces: `RenderedFigure(tex_path: str, png_path: str, cache_hit: bool)`。
- Produces: `TikzRenderer(pdflatex_path: Path, artifact_dir: Path, timeout_seconds: float = 20.0)`、`.from_settings(settings)`、`.check_environment() -> None`、`.render(tikz_source: str) -> RenderedFigure`。
- Produces: `validate_tikz_source(source: str) -> None`、`build_document(source: str) -> str`。

- [ ] **Step 1: 写失败测试**

覆盖：完整 `tikzpicture` 可接受；`\input`、`\openout`、`\write18`、`\documentclass` 和未知宏在 subprocess 前拒绝；超时/非零退出抛带日志摘要的 `RuntimeError`；同一源码第二次 `cache_hit=True`，源码变化生成新路径。真实编译测试画带 $\alpha$、直角标记的三角形，断言 PDF 转出的 PNG 非空且尺寸大于零。

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_tikz.py -q`
Expected: 因 `backend.services.tikz` 不存在而 FAIL。

- [ ] **Step 3: 实现安全渲染**

固定 `standalone + tikz + amsmath` 模板和 `angles,quotes,calc,intersections,arrows.meta,bending` 库；只允许绘图、标注和数学所需的明确命令集合。使用参数数组启动 `pdflatex`，启用 `-no-shell-escape -interaction=nonstopmode -halt-on-error` 和 paranoid 文件访问模式。产物写入 `data/figures/tikz/<sha256>.tex/.png`，键包含模板版本和编译器主版本；临时目录编译后用 PyMuPDF 渲染并原子发布。Task 8 尚未加入配置字段前，`from_settings` 用 `getattr` 读取可选字段并回退到指定编译器、`data/figures/tikz` 和 20 秒，保证任务可独立验收。

- [ ] **Step 4: 运行真实及模拟编译测试**

Run: `.venv/bin/python -m pytest tests/test_tikz.py -q`
Expected: PASS；本机测试使用 `/home/anna/.local/bin/pdflatex`，没有伪造真实编译结果。

### Task 3: 原图解析、对比与变式 AI 契约

**Files:**
- Modify: `backend/services/ai/base.py`
- Test: `tests/test_ai_parsing.py`

**Interfaces:**
- Consumes: Task 1 的 `GeometryFigureDraft`、`FigureComparison`、`GeometryVariant`、`AnalysisContext`。
- Produces: `BaseAIProvider.redraw_geometry(image_bytes, *, text, figure_bboxes, mime_type="image/png", context=None) -> list[GeometryFigureDraft]`。
- Produces: `.compare_geometry(composite_image_bytes, *, text, draft) -> FigureComparison`、`.repair_geometry(composite_image_bytes, *, text, draft, comparison) -> GeometryFigureDraft`。
- Produces: `.generate_geometry_variant(content, *, knowledge_points, error_category, question_type, difficulty, figure_specs, context=None, repair_instruction="") -> GeometryVariant`。
- Produces: `.validate_geometry_variant(rendered_png_bytes, *, content, draft, original_content, knowledge_points, error_category) -> FigureComparison`。

- [ ] **Step 1: 写失败测试**

用假提供商返回围栏 JSON，断言各方法解析为 Task 1 模型；检查原图提示包含坐标和 K12 上下文，变式提示明确“保留考点/错点但可改变图形结构”，校验提示包含新题干与图形语义。非法 `kind`、空 TikZ、非布尔 `passed` 必须抛 `AIMessageError`。

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_ai_parsing.py -q`
Expected: 因新接口不存在而 FAIL。

- [ ] **Step 3: 实现结构化提示与解析**

各方法沿用现有重试、`track_ai_call`、JSON 提取和数学定界符归一模式。对比图由调用方合成，因此不扩展底层提供商的多图接口；变式重试只新增 `repair_instruction`，不引入另一套生成接口。

- [ ] **Step 4: 运行 AI 解析测试**

Run: `.venv/bin/python -m pytest tests/test_ai_parsing.py -q`
Expected: PASS，既有解析行为不变。

### Task 4: 几何图编排与延迟裁剪兜底

**Files:**
- Create: `backend/services/geometry_figures.py`
- Modify: `backend/services/figures.py`
- Create: `tests/test_geometry_figures.py`

**Interfaces:**
- Consumes: Task 2 渲染器、Task 3 AI 接口、`AnalysisContext`。
- Produces: `FigureOutcome(figure_paths: list[str], figure_specs: list[dict], needs_review: bool)`。
- Produces: `GeometryFigureService(ai, renderer)`、`.from_settings(ai, settings)`、`.redraw_segment(*, image_path: Path, number: str, text: str, figure_bboxes: list, figures_dir: Path, context: AnalysisContext) -> FigureOutcome`。
- Produces: `.review_tikz(*, image_path: Path, text: str, figure_box: list | None, draft: GeometryFigureDraft, tikz_source: str) -> FigureSpec`，供 Task 7 只编译/对比用户提供的源码，不重新生成图形。
- Produces: `figures.crop_figure(image_path, box) -> PIL.Image.Image | None`，既有 `crop_figures` 继续兼容。

- [ ] **Step 1: 写失败测试**

用假 AI/渲染器固定：成功时二次定位和磁盘裁剪均为零调用；编译失败后才定位并裁剪；二次定位失败使用首次坐标；无效坐标返回空图且 `needs_review=True`；非几何类型直接兜底；两图中一图失败不影响另一张有效图；TeX 缺失时不发起重绘 AI 调用。对比合成只使用内存图片，成功路径 `crop_path is None`。另测 `review_tikz` 不调用 `redraw_geometry`，只编译指定源码并返回对比状态。

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_geometry_figures.py -q`
Expected: 因编排服务不存在而 FAIL。

- [ ] **Step 3: 实现按图状态机**

每图执行“编译 → 对比 → 一次修复 → 再编译/对比”。只有最终失败才按需定位/裁剪；成功规格状态为 `ai_passed`，兜底规格为 `fallback`。用 Pillow 将原图参考区域与渲染 PNG 合成对比输入，参考区域不落盘；任一异常只影响当前图。`review_tikz` 复用编译/对比原语，但不执行自动修复或重新解析。

- [ ] **Step 4: 运行编排与旧裁剪回归测试**

Run: `.venv/bin/python -m pytest tests/test_geometry_figures.py tests/test_figures.py -q`
Expected: PASS；本任务完成时不运行尚未迁移到 Task 5 新接口的 `test_segment.py`。

### Task 5: 整卷切题、任务编辑与确认入库传递

**Files:**
- Modify: `backend/services/segment.py`
- Modify: `backend/services/document_service.py`
- Test: `tests/test_segment.py`
- Test: `tests/test_document_import.py`
- Test: `tests/test_document_edit.py`
- Test: `tests/test_document_confirm.py`

**Interfaces:**
- Consumes: Task 4 `GeometryFigureService`/`FigureOutcome`，Task 1 数据字段。
- Produces: `Segment.figure_specs: list[dict]`；`visual_segment_page(..., figure_service=None, context=None)`；`segment_document(..., figure_service=None, context=None)`。
- Produces: 导入任务 `segments[*].figure_specs`，合并时双列表同时拼接，拆分时规格跟随原图归属，确认入库调用 `repo.create(..., figure_specs=...)`。

- [ ] **Step 1: 写失败测试**

更新视觉切题测试，断言生产路径把坐标交给 `GeometryFigureService`，成功时不调用旧定位/裁剪。任务测试断言规格随条目保存；合并题拼接规格；拆分后第二题不获得第一张图；确认入库后 `QuestionOut.figure_specs` 与任务一致，旧任务缺少该键仍兼容。

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_segment.py tests/test_document_import.py tests/test_document_edit.py tests/test_document_confirm.py -q`
Expected: 因缺少参数或传递字段而 FAIL。

- [ ] **Step 3: 接入整卷管线**

`DocumentService._run_import` 在已有 `AnalysisContext` 创建后构造一次 `GeometryFigureService.from_settings` 并传入 `segment_document`。移除成功路径的立即二次定位/裁剪；`figures_dir` 仅供失败兜底。所有任务编辑和确认分支同步传递 `figure_specs`，不改变现有题号、跨页和去重逻辑。

- [ ] **Step 4: 运行整卷相关测试**

Run: `.venv/bin/python -m pytest tests/test_segment.py tests/test_document_import.py tests/test_document_edit.py tests/test_document_confirm.py tests/test_geometry_figures.py -q`
Expected: PASS。

### Task 6: 同类题新图与检测参考答案

**Files:**
- Modify: `backend/services/geometry_figures.py`
- Modify: `backend/services/question_mixins.py`
- Modify: `backend/services/detection.py`
- Test: `tests/test_similar_fallback.py`
- Test: `tests/test_detection.py`

**Interfaces:**
- Consumes: Task 3 变式接口、Task 4 服务、Task 1 `FigureSpec`。
- Produces: `GeneratedVariant(content: str, figure_paths: list[str], figure_specs: list[dict])`。
- Produces: `GeometryFigureService.render_variant(variant: GeometryVariant, *, source: QuestionOut) -> GeneratedVariant`，执行编译和新题一致性校验，失败后按规格重试一次。
- Changes: `_generate_variant(question) -> GeneratedVariant | None`，`_generated_as_question(question, variant: GeneratedVariant)` 使用变式自己的图，不再复制 `question.figure_paths`。

- [ ] **Step 1: 写失败测试**

覆盖：带图题调用结构化变式并返回新路径/规格；无图非几何题保留原 `followup_question` 行为；新图校验失败重试一次后返回 `None`，库内候选不受影响；生成题路径与原题不同；检测 `tested` 携带新规格；`_generate_reference_answer` 的输入包含 `semantic_spec`，避免只凭文字判答。

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_similar_fallback.py tests/test_detection.py -q`
Expected: 因旧字符串变式契约或缺少图形语义而 FAIL。

- [ ] **Step 3: 实现变式渲染与服务替换**

`render_variant` 对每张新图编译并调用 `validate_geometry_variant`，重试后仍失败则抛出让 `_generate_variant` 返回 `None`。`CoreMixin` 延迟构造几何服务，避免增加无图题启动成本；非几何题继续走现有文本分析。参考答案提示加入新图语义和假设，不传递原题图片路径冒充新图。

- [ ] **Step 4: 运行同类题与检测测试**

Run: `.venv/bin/python -m pytest tests/test_similar_fallback.py tests/test_detection.py tests/test_question_service.py -q`
Expected: PASS。

### Task 7: 校对、展示与变式入库界面

**Files:**
- Modify: `backend/services/document_service.py`
- Modify: `frontend/components.py`
- Modify: `frontend/pages/import_doc.py`
- Modify: `frontend/pages/tutor.py`
- Test: `tests/test_document_edit.py`
- Test: `tests/test_question_figures_ui.py`

**Interfaces:**
- Consumes: Task 4 `GeometryFigureService.review_tikz`；校对重编译不得调用 `redraw_geometry`。
- Produces: `DocumentService.recompile_segment_figure(job_id, user_id, index, figure_index, *, tikz_source: str) -> dict`，重新安全编译和对比并更新当前条目。
- Produces: `DocumentService.accept_segment_figure(job_id, user_id, index, figure_index) -> dict`，仅允许把已成功编译的图设为 `manual_passed`。
- Changes: `save_followup_button(..., figure_paths=None, figure_specs=None)` 保存变式自己的图。

- [ ] **Step 1: 写失败测试**

服务测试覆盖越权/越界、危险 TikZ、编译失败保留旧有效图、重编译成功更新规格、人工确认状态。界面测试断言校对页显示原图参考/重绘图/状态及源码编辑入口，错题和复习仍读取有效 `figure_paths`，AI 变式“存入错题本”传递新图而非原题图。

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_document_edit.py tests/test_question_figures_ui.py -q`
Expected: 因缺少校对接口或参数而 FAIL。

- [ ] **Step 3: 实现校对和展示**

校对页用原生 `st.image` 展示对比并提供 TikZ 表单；重编译和人工确认分别调用上述接口，失败用页面错误提示，不写死成功。变式保存按钮同步保存图路径、规格及既有 K12 元数据。页面刷新不调用 AI/TeX，不新增自定义 HTML 图片查看器。

- [ ] **Step 4: 运行界面与编辑测试**

Run: `.venv/bin/python -m pytest tests/test_document_edit.py tests/test_question_figures_ui.py tests/test_document_confirm.py -q`
Expected: PASS。

### Task 8: TinyTeX 依赖清单、安装自检与部署配置

**Files:**
- Create: `deploy/tinytex-requirements.txt`
- Create: `scripts/install_tinytex.py`
- Create: `tests/test_tinytex_install.py`
- Modify: `backend/config.py`
- Modify: `deploy/launcher.py`
- Modify: `requirements.txt`
- Modify: `.env.example`
- Modify: `docs/DEPLOYMENT.md`

**Interfaces:**
- Produces: `Settings.pdflatex_path: Path`、`Settings.tikz_timeout_seconds: float = 20.0`。
- Produces: `scripts/install_tinytex.py --check-only` 和默认自动安装模式；自检必须验证 TeX Live 2026、宏包和一次真实最小编译。

- [ ] **Step 1: 写失败测试**

对安装脚本纯函数/subprocess 替身测试：当前机器路径可发现；2025 或无法识别版本判失败；缺少宏包返回缺失列表；自动安装命令包含清单包；`--check-only` 不执行安装；启动器只注入可执行文件路径。配置测试覆盖空值、绝对路径和超时范围。

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_tinytex_install.py -q`
Expected: 因脚本或配置不存在而 FAIL。

- [ ] **Step 3: 实现幂等安装与配置**

清单列出 `standalone`、`pgf`、`amsmath`，由 `tlmgr` 解决依赖；脚本只在默认安装模式下载/安装。当前默认配置使用指定路径，部署启动器可覆盖为实际安装路径。`requirements.txt` 仅说明外部依赖并链接清单，文档写明首次配置需要网络及缺 TeX 时的裁剪降级。

- [ ] **Step 4: 运行测试和真实环境自检**

Run: `.venv/bin/python -m pytest tests/test_tinytex_install.py tests/test_tikz.py -q`
Run: `.venv/bin/python scripts/install_tinytex.py --check-only`
Expected: 测试 PASS；自检报告 TeX Live 2026、指定编译器和全部宏包可用，最小编译成功，不执行安装。

### Task 9: 真实几何样例与全量验收

**Files:**
- Modify only if acceptance exposes a defect in the scoped implementation; add regression coverage to the owning test file first.

**Interfaces:**
- Consumes: Tasks 1–8 的完整链路。

- [ ] **Step 1: 执行 `几何2.jpg` 真实导入**

通过实际 Streamlit 导入流程处理 `/home/anna/workspace/learning-tour/高中数学/几何2.jpg`。确认成功路径没有新裁剪文件，校对页能看到 TikZ 重绘和原图对比；人工核对 $P,Q,A,B,C$、$\alpha,\beta,\gamma$、$a=200$ 及垂直/仰角关系。若命中既有去重任务，不删除用户数据绕过；使用既有未确认任务的重解析/校对能力，或先征得用户确认。

- [ ] **Step 2: 验证入库、复习和同类题**

确认入库后检查错题本与今日复习展示同一有效图。生成同类题，确认仍训练仰角测量/正弦定理等目标考点，使用自己的 TikZ 图且题干、图和参考答案一致；保存变式后图规格仍存在。再导入或重解析 `几何1.jpg`，检查立体几何关系或明确兜底。

- [ ] **Step 3: 验证故障降级**

用测试替身或临时无效 `PDFLATEX_PATH` 运行单题导入，确认只在失败阶段生成裁剪图、题目标记校对且其余题成功；恢复配置后用 `--check-only` 确认真实环境未改变。

- [ ] **Step 4: 运行完整质量检查**

Run: `TZ=UTC .venv/bin/python -m pytest -q`
Run: `.venv/bin/python -m ruff check .`
Expected: 全量测试 PASS、Ruff 无错误。检查 `git status --short`，删除本次测试产生且不需要的临时日志/构建物，不清理或提交用户已有修改。

- [ ] **Step 5: 验证用户实际访问入口**

检查既有 `http://localhost:8501` 应用；需要重启时先征得用户同意。最终在浏览器实际打开校对、错题、复习和同类题页面，不仅以导入或编译成功代替端到端验收。
