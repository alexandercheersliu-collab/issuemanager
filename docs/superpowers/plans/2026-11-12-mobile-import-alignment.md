# Mobile Import Alignment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Align the backend processing and rendered results of the two existing Mobile import flows with Web without adding entry points or student-facing advanced editing.

**Architecture:** Extract photo planning/confirmation into a persistent backend `PhotoEntryService` used directly by Web and through REST by Mobile. Reuse the existing documents pipeline, expose canonical stem/analysis fields, and serve figures through owner-checked endpoints; generated detection figures use short-lived signed tokens because they have no database record.

**Tech Stack:** Python 3.10+, FastAPI, SQLAlchemy, Streamlit >=1.65, React 19, TypeScript, Vite, existing pytest and Playwright; no new runtime or test dependencies.

**Spec:** `docs/superpowers/specs/2026-11-12-mobile-import-alignment-design.md`

## Global Constraints

- Keep only the existing Mobile “拍照录题” and bottom “整卷导入” entries; add no manual entry, navigation tab, K12 form, text editor, merge/split UI, or TikZ editor.
- Default Mobile import context is exactly `subject="math"`, `grade=10`, `region="上海"`, `textbook_version="沪教版"`.
- Preserve current Web marker text and interaction order: “从图中识别出” → “确认录入” → “已自动归档入错题本”.
- Use `AnalysisContext`, `QuestionService`, `DocumentService`, jobs, JWT and existing UI components; do not add a package or test framework.
- Do not change database tables, AI prompts, TikZ policy, or review scheduling.
- Preserve unrelated working-tree changes. Do not run `git commit`, `git merge`, or `git push` unless the user explicitly requests it.

## Review Focus

- Empty, oversized, unsupported, or mixed document selections must fail before creating a job; pin with API validation tests in Tasks 3 and 6.
- Zero selected candidates, segmentation failure, and single-question images must not dead-end or duplicate work; pin with service tests in Task 2.
- Repeated upload/confirmation and per-item AI failure must be idempotent and isolated; pin with service/API tests in Tasks 2, 3, and 6.
- Cross-user job/question/detection IDs, bad indices, expired tokens, and missing files must return 404 without exposing paths; pin with resource tests in Task 7.
- Legacy content, document content, missing figures, and fallback figures must remain readable and must not crash detail/review/detection; pin with contract tests in Tasks 1, 7, and 8.

---

### Task 1: Canonical Question Content Contract

**Files:**
- Create: `backend/services/question_content.py`
- Modify: `backend/models/schemas.py:115-200`
- Modify: `frontend/pages/review.py:31-50`
- Test: `tests/test_review_flashcard.py`
- Create: `tests/test_question_content_api.py`

**Interfaces:**
- Produces: `split_stem_and_analysis(content_markdown: str) -> tuple[str, str | None]`.
- Produces read-only `QuestionOut.stem: str` and `QuestionOut.analysis: str | None`, computed from `content_markdown` without constructor changes.

- [ ] **Step 1: Write failing parser and API tests**

Cover exact inputs: `题干\n\n---\n\n解析`, `### 题目\n题干\n\n### 解析\n解析`, empty analysis, repeated separators in analysis, and legacy plain text. Add an API assertion that a created document-format question returns separate `stem == "题干"` and `analysis == "解析"` while retaining `content_markdown`.

- [ ] **Step 2: Run tests to verify RED**

Run: `.venv/bin/python -m pytest tests/test_review_flashcard.py tests/test_question_content_api.py -q`
Expected: FAIL because the backend module/response fields do not exist.

- [ ] **Step 3: Implement the shared parser and response fields**

Move the two-format logic into `question_content.py`; make the Streamlit helper delegate to it. Add Pydantic computed fields on `QuestionOut` using the shared parser, preserving the existing `mastered` computed field.

- [ ] **Step 4: Run focused tests to verify GREEN**

Run: `.venv/bin/python -m pytest tests/test_review_flashcard.py tests/test_question_content_api.py tests/test_k12_api.py -q`
Expected: PASS.

### Task 2: Shared Default Context and Persistent Photo Entry Service

**Files:**
- Modify: `backend/services/entry_types.py`
- Create: `backend/services/photo_entry.py`
- Create: `tests/test_photo_entry_service.py`
- Reuse: `backend/services/question_mixins.py:324-554`, `backend/models/orm.py:207-222`

**Interfaces:**
- Produces: `DEFAULT_IMPORT_CONTEXT = AnalysisContext(subject="math", grade=10, region="上海", textbook_version="沪教版")`.
- Produces: `PhotoImage(name: str, data: bytes, mime_type: str)` dataclass.
- Produces: `PhotoEntryService.create_job(user_id: int, images: list[PhotoImage], *, tags: list[str] | None = None, hint: str = "", context: AnalysisContext | None = None) -> str`.
- Produces: `PhotoEntryService.get_job(job_id: str, user_id: int) -> dict | None`.
- Produces: `PhotoEntryService.confirm(job_id: str, user_id: int, candidate_ids: list[str]) -> None`.
- Job responses contain `job_id`, `phase`, `candidates`, `fallback_count`, `total`, `done`, `entries`, `error`; candidates have stable `id`, `image`, `number`, `text`, `summary`, `likely_wrong`, `figure_bboxes`.

- [ ] **Step 1: Write failing service tests**

Use MockProvider and temporary data. Test: two images produce stable candidates; `likely_wrong` defaults are observable; a single/empty/failing segmentation falls back without a second segmentation call; no candidates auto-import; confirming selected IDs imports only those plus fallbacks; context defaults are exact; duplicate images reuse records; one failing item does not stop others; empty selection without fallbacks raises `ValueError`; repeated confirmation returns the original entries; another user gets `None`/`LookupError`.

- [ ] **Step 2: Run service tests to verify RED**

Run: `.venv/bin/python -m pytest tests/test_photo_entry_service.py -q`
Expected: FAIL because `PhotoEntryService` does not exist.

- [ ] **Step 3: Implement the service**

Extract the logic currently in `frontend/pages/tutor.py:257-449` into framework-independent functions. Store original inputs under the job directory, persist candidates/results in `jobs.payload/result`, and run planning/import using the existing daemon-thread pattern. Phases are `planning`, `review`, `importing`, `done`, `failed`; planning failure removes no other jobs. Never trust client text or paths during confirmation.

- [ ] **Step 4: Run service and existing backend photo tests**

Run: `.venv/bin/python -m pytest tests/test_photo_entry_service.py tests/test_photo_segment.py -q`
Expected: PASS. UI delegation tests remain unchanged and green until Task 4 writes their new failing assertions.

### Task 3: Photo Entry REST API

**Files:**
- Create: `api/routers/photo_entry.py`
- Modify: `api/main.py:18-30,81-91`
- Create: `tests/test_photo_entry_api.py`

**Interfaces:**
- Consumes: Task 2 `PhotoEntryService` and `PhotoImage`.
- Produces: `POST /api/photo-entry/jobs`, `GET /api/photo-entry/jobs/{job_id}`, `POST /api/photo-entry/jobs/{job_id}/confirm`; confirmation JSON is `{"candidate_ids": list[str]}`.

- [ ] **Step 1: Write failing API tests**

Test JWT protection, multi-image 202 response, polling to `review`, selected confirmation to `done`, notebook visibility through `/api/questions`, invalid candidate IDs as 409, unknown/cross-user jobs as 404, and empty/oversized/non-image upload rejection before job creation. Assert omitted context resolves to Task 2 defaults.

- [ ] **Step 2: Run API tests to verify RED**

Run: `.venv/bin/python -m pytest tests/test_photo_entry_api.py -q`
Expected: FAIL with 404 for the new routes.

- [ ] **Step 3: Implement routes and validation**

Mirror `questions.py` upload constants and `documents.py` error mapping. Read each upload once, enforce per-file 10MB and JPEG/PNG/WEBP, create `PhotoImage` objects, and register the router under the existing API prefix. Apply a user-scoped photo-entry rate limit.

- [ ] **Step 4: Run API and regression tests**

Run: `.venv/bin/python -m pytest tests/test_photo_entry_api.py tests/test_k12_api.py tests/test_api.py -q`
Expected: PASS.

### Task 4: Migrate Web Photo Entry to the Shared Service

**Files:**
- Modify: `frontend/pages/tutor.py:1-449`
- Modify: `tests/test_tutor_uploads.py`
- Modify: `tests/test_photo_segment.py`
- Test: `tests/e2e/test_tutor_flow.py`

**Interfaces:**
- Consumes: Task 2 service methods and job response shape.
- Produces: no new public interface; Web marker text and candidate behavior remain unchanged.

- [ ] **Step 1: Update tests to assert shared-service delegation**

Replace tests that expect Streamlit-local `_plan_photo_entry`/`_save_checked_candidates` internals with tests that patch `PhotoEntryService` and assert create/get/confirm calls. Keep pure behavior coverage in Task 2 rather than duplicating it in the UI.

- [ ] **Step 2: Run changed tests to verify RED**

Run: `.venv/bin/python -m pytest tests/test_tutor_uploads.py tests/test_photo_segment.py -q`
Expected: FAIL because the page still uses local orchestration.

- [ ] **Step 3: Replace local orchestration with task polling**

Keep existing uploader, tags, hint, K12 controls, candidate cards and result rendering. Store only `job_id` plus UI busy state in Streamlit session state; map service candidates to existing checkbox defaults and entries to `_render_entry_results`. Remove duplicated local planning/confirm implementations after their callers are gone.

- [ ] **Step 4: Verify Web unit and real E2E flow**

Run: `.venv/bin/python -m pytest tests/test_tutor_uploads.py tests/test_photo_segment.py -q`
Expected: PASS.
Run the existing app on 8601 with mock AI, then: `MM_E2E=1 MM_E2E_BASE_URL=http://localhost:8601 .venv/bin/python -m pytest tests/e2e/test_tutor_flow.py -q`
Expected: PASS, including the exact marker sequence in Global Constraints.

### Task 5: Align the Existing Mobile Photo Flow

**Files:**
- Modify: `mobile/src/lib/api.ts`
- Modify: `mobile/src/types/index.ts`
- Modify: `mobile/src/pages/Capture.tsx`
- Create: `tests/e2e/test_mobile_import.py`

**Interfaces:**
- Consumes: Task 3 endpoints and response shape.
- Produces: `api.createPhotoJob(files: File[], extra: {tags?: string; hint?: string})`, `api.getPhotoJob(jobId: string)`, `api.confirmPhotoJob(jobId: string, candidateIds: string[])`.

- [ ] **Step 1: Write failing Mobile photo Playwright scenarios**

Use a 390×844 viewport, real login and MockProvider. Cover multi-photo candidate confirmation, single-photo automatic fallback, duplicate result, refresh/resume, and notebook visibility. Assert that no “文字录入”, K12 form, TikZ editor, merge/split control, or new navigation entry appears. Gate these tests with `MM_MOBILE_E2E=1` and use `MM_MOBILE_E2E_BASE_URL`, defaulting to `http://localhost:8000/m/`.

- [ ] **Step 2: Run Mobile photo E2E to verify RED**

Build Mobile, start API on 8000, then run: `MM_MOBILE_E2E=1 .venv/bin/python -m pytest tests/e2e/test_mobile_import.py -q`
Expected: FAIL because Capture still submits one image directly to `/questions/analyze`.

- [ ] **Step 3: Implement the contract and existing-page flow**

Add TypeScript interfaces matching Task 2. Allow multiple JPEG/PNG/WEBP files in the current picker, preview them, and replace direct analysis with photo tasks. Support `planning`, candidate `review`, `importing`, `done`, and recoverable `failed`; default-check `likely_wrong`, require a selection only when needed, poll with cleanup, persist/restore the active job ID, revoke object URLs, and render duplicate/failure summaries. Add no entry or K12 control.

- [ ] **Step 4: Run Mobile photo E2E and static checks**

Run the Step 2 E2E command, then `npm run lint && npm run build` in `mobile/`.
Expected: all PASS with no TypeScript or hook dependency errors.

### Task 6: Align the Existing Mobile Document Flow and Defaults

**Files:**
- Modify: `api/routers/documents.py:24-71`
- Modify: `mobile/src/lib/api.ts`
- Modify: `mobile/src/types/index.ts`
- Modify: `mobile/src/pages/BatchImport.tsx`
- Modify: `tests/test_document_import.py`
- Modify: `tests/test_document_edit.py`
- Modify: `tests/e2e/test_mobile_import.py`

**Interfaces:**
- Produces optional form flag `use_default_context: bool = Form(default=False)` on document import; when true, absent fields come from `DEFAULT_IMPORT_CONTEXT`, while omitted flag preserves legacy API behavior.
- Produces: `api.importDocument(files: File[], useDefaultContext = true)` sending repeated `document` parts.
- Consumes: existing delete/reanalyze/confirm endpoints.

- [ ] **Step 1: Write failing backend and Mobile document tests**

Backend: `use_default_context=true` yields math/10/上海/沪教版 after confirm, omitted flag keeps legacy `grade=None`, direct PDF/DOCX works, multiple images preserve order, mixed multi-file document/image input returns 400, and invalid/empty files create no job. Mobile E2E: existing batch entry uploads PDF and multiple images, deletes/reanalyzes a segment, confirms, displays skipped reasons, and restores an unfinished job.

- [ ] **Step 2: Run document tests to verify RED**

Run: `.venv/bin/python -m pytest tests/test_document_import.py tests/test_document_edit.py -q`
Run only the new document E2E scenarios using the Task 5 environment.
Expected: backend FAIL for the new flag; Mobile FAIL for direct document upload and missing reanalyze behavior.

- [ ] **Step 3: Implement backend defaults and the existing Mobile batch flow**

Resolve defaults in the API boundary. In `BatchImport`, keep the current entry and restoration, accept one PDF/DOCX or multiple JPEG/PNG/WEBP images, upload images directly in page order instead of calling `imagesToPdf`, and add “重新解析” beside delete. Show page/source, fallback warning, confirm counts and expandable skipped reasons; add no other editing controls.

- [ ] **Step 4: Run document backend, E2E, and Mobile checks**

Run: `.venv/bin/python -m pytest tests/test_document_import.py tests/test_document_edit.py tests/test_document_confirm.py -q`
Run the document E2E scenarios, then `npm run lint && npm run build` in `mobile/`.
Expected: all PASS.

### Task 7: Owner-Checked Question, Document, and Detection Figures

**Files:**
- Modify: `api/routers/questions.py`
- Modify: `api/routers/documents.py`
- Modify: `api/routers/detection.py`
- Modify: `backend/services/detection.py:138-213`
- Create: `backend/services/asset_tokens.py`
- Create: `tests/test_figure_assets_api.py`

**Interfaces:**
- Produces question/document/page GET routes listed in the spec.
- Produces: `issue_detection_figure_token(*, log_id: int, user_id: int, figure_index: int, path: str, now: dt.datetime | None = None) -> str`.
- Produces: `resolve_detection_figure_path(token: str, *, log_id: int, user_id: int, figure_index: int, now: dt.datetime | None = None) -> Path`.
- Detection `tested` adds `stem: str` and `figure_tokens: list[str]`; token lifetime is 10 minutes and uses the existing auth secret through PyJWT.

- [ ] **Step 1: Write failing resource tests**

Create real tiny PNGs in question/document/detection-owned locations. Assert authorized bytes, content type, and 404 for another user, negative/out-of-range index, missing path/file, expired token, tampered token, and mismatched log/user/index. Test both stored and generated detection figures and the new `tested.stem` field.

- [ ] **Step 2: Run resource tests to verify RED**

Run: `.venv/bin/python -m pytest tests/test_figure_assets_api.py -q`
Expected: FAIL because routes/tokens do not exist.

- [ ] **Step 3: Implement resource resolution and signed detection tokens**

Resolve only paths already present in `QuestionOut`, document job results, or verified token claims. Use `FileResponse`; never accept a bare client path. Verify the detection log belongs to the current user before serving even a valid token. Use HMAC-signed JWT claims for kind, user, log, index, path, issue/expiry times.

- [ ] **Step 4: Run resource and security regressions**

Run: `.venv/bin/python -m pytest tests/test_figure_assets_api.py tests/test_detection.py tests/test_security.py tests/test_document_edit.py -q`
Expected: PASS.

### Task 8: Mobile Content and Figure Rendering

**Files:**
- Create: `mobile/src/components/AuthImage.tsx`
- Modify: `mobile/src/lib/api.ts`
- Modify: `mobile/src/types/index.ts`
- Modify: `mobile/src/pages/Notebook.tsx`
- Modify: `mobile/src/pages/Review.tsx`
- Modify: `mobile/src/components/DetectionPanel.tsx`
- Modify: `mobile/src/pages/BatchImport.tsx`
- Modify: `tests/test_figure_assets_api.py`
- Modify: `tests/e2e/test_mobile_import.py`

**Interfaces:**
- Consumes: Task 1 `Question.stem/analysis`, Task 6 document segments, Task 7 resource routes and `DetectionStart.tested.figure_tokens/stem`.
- Produces: `AuthImage({src, alt, fallback?: ReactNode})` where `src` is an API-relative path; it fetches with JWT, creates/revokes a blob URL, and renders fallback on 404/error.

- [ ] **Step 1: Write failing content and figure scenarios**

Extend API tests for list/detail/review stem/analysis and detection figure tokens. Extend Mobile E2E with a controlled figure fixture: verify document content is split correctly, figures appear in detail/review/detection, legacy/no-figure content remains readable, and fallback shows only a short warning with no TikZ source.

- [ ] **Step 2: Run contract and Mobile E2E tests to verify RED**

Run: `.venv/bin/python -m pytest tests/test_figure_assets_api.py -q`
Run the new content/figure E2E scenarios using Task 5 environment.
Expected: FAIL before Mobile consumes the new contract.

- [ ] **Step 3: Implement rendering without new UI entries**

Replace Mobile stem guessing with server fields. Display figures before/alongside stem in Notebook, Review, DetectionPanel, and BatchImport; use original image/page only as the defined fallback. Keep answer, grading, detection, deletion and confirmation behavior unchanged.

- [ ] **Step 4: Run content/figure and static checks**

Run: `.venv/bin/python -m pytest tests/test_question_content_api.py tests/test_figure_assets_api.py -q`
Run the content/figure E2E scenarios, then `npm run lint && npm run build` in `mobile/`.
Expected: PASS.

### Task 9: End-to-End Alignment and Full Regression

**Files:**
- Modify: `tests/e2e/test_mobile_import.py`
- Modify: `.github/workflows/ci.yml`
- Modify: `mobile/README.md`
- Modify: `docs/ARCHITECTURE.md:129-150`
- Modify: `docs/USER_MANUAL.md:78-101`

**Interfaces:**
- Consumes: all previous tasks.
- Produces: no runtime interface; documents the same two Mobile entries and simplified student behavior.

- [ ] **Step 1: Complete E2E assertions and CI startup**

Ensure the scenarios from Tasks 5, 6, and 8 run together without shared-state assumptions. Add a CI Mobile E2E step that builds Mobile, starts API on 8000, health-checks it, sets `MM_MOBILE_E2E=1`, runs the file, and uploads failure screenshots with the existing action versions. Do not change the existing Web E2E base URL.

- [ ] **Step 2: Run the complete Mobile E2E file**

Build Mobile and start API on 8000, then run: `MM_MOBILE_E2E=1 .venv/bin/python -m pytest tests/e2e/test_mobile_import.py -q`
Expected: all real Mobile scenarios PASS without arbitrary sleeps where condition-based waits are available.

- [ ] **Step 3: Update documentation**

Document only implemented behavior in Mobile README, architecture, and user manual. Keep registration-preference capture explicitly marked as future work; do not claim new entries or editing features exist.

- [ ] **Step 4: Run complete verification**

Run: `.venv/bin/python -m ruff check .`
Run: `AI_PROVIDER=mock RAG_ENABLED=false .venv/bin/python -m pytest -q`
Run all existing Web E2E on 8601 and new Mobile E2E on 8000.
Run: `npm run lint && npm run build` in `mobile/`.
Expected: all commands exit 0; report real-model geometry/TikZ semantic acceptance separately because MockProvider cannot prove it.

- [ ] **Step 5: Inspect the final change set**

Run: `git status --short && git -c core.whitespace=cr-at-eol diff --check && git diff --stat`
Expected: only planned files plus user-approved pre-existing changes; no generated uploads, logs, coverage output, or build artifacts accidentally added. Do not commit without explicit user authorization.
