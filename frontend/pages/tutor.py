"""AI 录题页：上传错题图片 → 结构化解析 → 自动入库与向量索引 → 举一反三。"""
from __future__ import annotations

import time

import streamlit as st

from backend.models.schemas import (
    ERROR_CATEGORIES,
    SUBJECT_NAMES,
    QuestionAnalysis,
    QuestionOut,
)
from backend.services.ai.base import AnalysisContext
from backend.services.photo_entry import PhotoEntryService, PhotoImage
from backend.services.question_service import sanitize_tags
from frontend.common import (
    followup_chat,
    get_question_service,
    go_to,
    page_header,
    render_markdown,
    safe_truncate_math,
)
from frontend.components import render_figures, save_followup_button

_GRADE_OPTIONS = ["不限"] + [f"{g} 年级" for g in range(1, 13)]


def _k12_context_inputs(key_prefix: str, *, disabled: bool = False) -> dict:
    """学科/年级/地区/教材版本录入控件，默认数学、10 年级、上海、沪教版。

    返回可直接传给服务层的上下文字典；界面层纯控件，服务调用方负责传参。
    """
    subject_labels = list(SUBJECT_NAMES.values())
    col_s, col_g = st.columns(2)
    with col_s:
        subject_label = st.selectbox(
            "学科",
            subject_labels,
            index=subject_labels.index("数学"),
            key=f"{key_prefix}_subject",
            disabled=disabled,
        )
    with col_g:
        grade_label = st.selectbox(
            "年级",
            _GRADE_OPTIONS,
            index=_GRADE_OPTIONS.index("10 年级"),
            key=f"{key_prefix}_grade",
            disabled=disabled,
        )
    col_r, col_t = st.columns(2)
    with col_r:
        region = st.text_input(
            "地区（可选）",
            value="上海",
            placeholder="例如：北京",
            key=f"{key_prefix}_region",
            disabled=disabled,
        )
    with col_t:
        textbook = st.text_input(
            "教材版本（可选）",
            value="沪教版",
            placeholder="例如：人教版",
            key=f"{key_prefix}_textbook",
            disabled=disabled,
        )
    subject = next(k for k, v in SUBJECT_NAMES.items() if v == subject_label)
    grade = int(grade_label.split()[0]) if grade_label != "不限" else None
    return {
        "subject": subject,
        "grade": grade,
        "region": region.strip() or None,
        "textbook_version": textbook.strip() or None,
    }


def render_tutor_page(user: dict) -> None:
    service = get_question_service()
    photo_service = PhotoEntryService()
    page_header("AI 录题", "上传错题照片，自动完成考点分析、详解与归档")

    info = service.ai.provider_info()
    if info.demo_mode:
        st.warning("当前为演示模式：未配置 AI_API_KEY，返回内置示例解析。在 .env 配置后即可调用真实视觉模型。")

    tab_photo, tab_manual = st.tabs(["📸 拍照录题", "⌨️ 手动录入"])

    with tab_photo:
        with st.container(border=True):
            uploads = st.file_uploader(
                "📸 点击或拖拽上传错题图片（支持多选）",
                type=["jpg", "jpeg", "png", "webp"],
                accept_multiple_files=True,
                key="photo_uploads",
                on_change=lambda: st.session_state.pop("photo_job_id", None),
            )
            if uploads:
                preview_cols = st.columns(min(len(uploads), 4))
                for i, upload in enumerate(uploads[:4]):
                    with preview_cols[i]:
                        st.image(upload.getvalue(), width="stretch", caption=upload.name)
                if len(uploads) > 4:
                    st.caption(f"已选择 {len(uploads)} 张图片")

            meta_col1, meta_col2 = st.columns(2)
            with meta_col1:
                tags_input = st.text_input("标签（可选，逗号分隔）", placeholder="例如：期末复习, 几何", key="photo_tags")
            with meta_col2:
                hint = st.text_input(
                    "给老师的话（可选）",
                    placeholder="例如：第二问总是不知道从哪里下手",
                )
            with st.expander("🎓 学科 / 年级 / 教材等补充信息（可选）", expanded=False):
                context = _k12_context_inputs("photo")

            job_id = st.session_state.get("photo_job_id")
            job = photo_service.get_job(job_id, user["id"]) if job_id else None
            if job_id and job is None:
                st.error("拍照录入任务不存在（可能已被清理）")
                st.session_state.pop("photo_job_id", None)

            seg_busy = st.session_state.get("photo_seg_busy", False)
            job_busy = job is not None and job["phase"] in {"planning", "review", "importing"}
            if uploads and st.button(
                "开始 AI 解析",
                type="primary",
                width="stretch",
                disabled=seg_busy or job_busy,
            ):
                st.session_state["photo_seg_busy"] = True
                try:
                    new_job_id = photo_service.create_job(
                        user["id"],
                        [
                            PhotoImage(u.name, u.getvalue(), u.type or "image/jpeg")
                            for u in uploads
                        ],
                        tags=sanitize_tags(tags_input),
                        hint=hint,
                        context=AnalysisContext(**context),
                    )
                except Exception as exc:  # noqa: BLE001 - 创建失败恢复可点击态
                    st.error(f"从图中找题失败：{exc}")
                else:
                    st.session_state["photo_job_id"] = new_job_id
                    st.rerun()
                finally:
                    st.session_state["photo_seg_busy"] = False

            if job is not None:
                phase = job["phase"]
                if phase == "planning":
                    with st.spinner("正在从图中找题…"):
                        time.sleep(0.5)
                    st.rerun()
                elif phase == "review":
                    _render_candidate_review(photo_service, user, job)
                elif phase == "importing":
                    total = job["total"]
                    done = job["done"]
                    with st.status(f"正在录入 {total} 道题…", expanded=True) as status:
                        status.update(label=f"正在录入…（已完成 {done}/{total} 题）")
                        if total:
                            st.progress(done / total)
                    time.sleep(0.5)
                    st.rerun()
                elif phase == "failed":
                    st.error(f"拍照录入失败：{job.get('error') or '未知错误'}")
                elif phase == "done" and job["entries"]:
                    _render_entry_results(
                        service, user, _photo_job_entries(job["entries"]), unit="项"
                    )

    with tab_manual:
        _render_manual_entry(service, user)


def _render_manual_entry(service, user) -> None:
    """手动录入：没有照片时也能把题目文本归档进错题本（含向量索引）。"""
    with st.container(border=True):
        st.caption("适合已经誊抄/打字的题目，直接录入文字版解析，同样参与语义搜索与相似题召回。")
        with st.form("manual_question_form"):
            content = st.text_area(
                "题目与解析（Markdown，支持 LaTeX）",
                placeholder="### 题目\n已知 $x^2-2(k-1)x+k^2=0$ 有两个实数根，求 $k$ 的取值范围。\n\n### 解析\n由判别式…",
                height=200,
            )
            context = _k12_context_inputs("manual")
            col_a, col_t, col_k = st.columns([2, 2, 2])
            with col_a:
                answer = st.text_input("正确答案（可选）")
            with col_t:
                tags = st.text_input("标签（逗号分隔）", placeholder="例如：几何, 相似三角形")
            with col_k:
                points = st.text_input("考点（逗号分隔，可选）", placeholder="例如：相似三角形")
            col_q, col_c, col_e = st.columns([2, 2, 2])
            with col_q:
                question_type = st.text_input(
                    "题型（可选）", placeholder="例如：解答题", key="manual_qtype"
                )
            with col_c:
                chapter = st.text_input(
                    "章节（可选）", placeholder="例如：一元二次方程", key="manual_chapter"
                )
            with col_e:
                error_category = st.selectbox(
                    "错因（可选）", ["未选择", *ERROR_CATEGORIES], index=0, key="manual_errcat"
                )
            ai_enrich = st.toggle(
                "让 AI 解析并补全空缺标注",
                value=False,
                help="开启后 AI 会分析题目文本，补全答案、标签与考点（留空的字段才会被补全）",
            )
            submitted = st.form_submit_button("存入错题本", type="primary", width="stretch")
        if submitted:
            if not content.strip():
                st.error("题目与解析不能为空")
                return
            try:
                saved = service.create_manual_question(
                    user["id"],
                    content_markdown=content.strip(),
                    answer=answer.strip(),
                    tags=sanitize_tags(tags),
                    knowledge_points=sanitize_tags(points),
                    ai_analyze=ai_enrich,
                    subject=context["subject"],
                    grade=context["grade"],
                    region=context["region"],
                    textbook_version=context["textbook_version"],
                    question_type=question_type.strip() or None,
                    chapter=chapter.strip() or None,
                    error_category=None if error_category == "未选择" else error_category,
                )
            except Exception as exc:  # noqa: BLE001 - AI 失败给出明确提示
                st.error(f"保存失败：{exc}")
                return
            st.success(f"已存入错题本（#{saved.id}），向量索引同步更新。")
            st.toast("错题已归档", icon="📒")
            if st.button("📒 去错题本查看", key="manual_view_notebook"):
                go_to("notebook")


def _collect_entries(results: list) -> list[dict]:
    """把 (文件名, EntryResult|None, 错误) 三元组整形成可渲染条目。

    纯函数、不依赖 Streamlit，便于单测：历史 bug 正是这里把 EntryResult
    当成元组解包（TypeError: cannot unpack non-iterable EntryResult object）。
    """
    entries: list[dict] = []
    for name, result, error in results:
        if error is not None or result is None:
            entries.append({"name": name, "error": error or "解析失败", "duplicated": False})
            continue
        entries.append(
            {
                "name": name,
                "error": None,
                "question": result.question,
                "analysis": result.analysis,
                "duplicated": bool(result.duplicated),
            }
        )
    return entries


def _photo_job_entries(entries: list[dict]) -> list[dict]:
    hydrated = []
    for entry in entries:
        item = dict(entry)
        if item.get("question") is not None:
            item["question"] = QuestionOut.model_validate(item["question"])
        if item.get("analysis") is not None:
            item["analysis"] = QuestionAnalysis.model_validate(item["analysis"])
        hydrated.append(item)
    return hydrated


def _render_candidate_review(service, user, job: dict) -> None:
    """候选清单勾选界面：疑似做错的题默认勾选，确认后批量解构入库。"""
    candidates = job["candidates"]
    wrong_count = sum(1 for c in candidates if c["likely_wrong"])
    st.success(
        f"从图中识别出 {len(candidates)} 道题"
        + (f"，其中 {wrong_count} 道疑似做错已默认勾选。" if wrong_count else "。")
        + "请勾选要录入的错题："
    )
    checked: list[dict] = []
    for i, cand in enumerate(candidates):
        mark = " 🟥疑似做错" if cand["likely_wrong"] else ""
        label = f"[{cand['image']}] 第 {cand['number']} 题：{cand['summary']}{mark}"
        with st.container(border=True):
            if st.checkbox(
                label, value=bool(cand.get("likely_wrong", False)), key=f"photo_seg_{i}"
            ):
                checked.append(cand)
    if job["fallback_count"]:
        st.caption(f"另有 {job['fallback_count']} 张图片未切出多题，将按整图单题解析。")

    confirm_busy = st.session_state.get("photo_confirm_busy", False)
    if st.button(
        f"确认录入（{len(checked)} 题）",
        type="primary",
        width="stretch",
        disabled=confirm_busy,
    ):
        if not checked and not job["fallback_count"]:
            st.warning("请至少勾选一道题。")
            return
        st.session_state["photo_confirm_busy"] = True
        try:
            service.confirm(job["job_id"], user["id"], [cand["id"] for cand in checked])
        except Exception as exc:  # noqa: BLE001 - 确认失败保留候选供重试
            st.error(f"确认录入失败：{exc}")
        else:
            st.rerun()
        finally:
            st.session_state["photo_confirm_busy"] = False


def _process_uploads(service, user, uploads, tags: list[str], hint: str, context: dict) -> None:
    results = []
    total = len(uploads)
    with st.status(f"正在录入 {total} 张图片…", expanded=True) as status:
        for idx, upload in enumerate(uploads, 1):
            status.update(label=f"正在录入…（第 {idx}/{total} 张，单张约需 20–60 秒）")
            st.write(f"🔍 正在解析 {upload.name}…")
            try:
                image_bytes = upload.getvalue()
                mime = upload.type or "image/jpeg"
                entry = service.analyze_and_save_dedup(
                    user["id"],
                    image_bytes,
                    mime_type=mime,
                    user_tags=tags,
                    hint=hint,
                    subject=context["subject"],
                    grade=context["grade"],
                    region=context["region"],
                    textbook_version=context["textbook_version"],
                )
                results.append((upload.name, entry, None))
            except Exception as exc:  # noqa: BLE001 - 单张失败不影响其余
                results.append((upload.name, None, str(exc)))
                st.write(f"❌ {upload.name} 解析失败：{exc}")
        failures = sum(1 for _, _, err in results if err is not None)
        if failures:
            status.update(
                label=f"⚠️ 录入完成，{failures}/{total} 张失败（详见下方）",
                state="error",
                expanded=False,
            )
        else:
            status.update(label=f"✅ {total} 张图片全部录入完成", state="complete", expanded=False)
    _render_entry_results(service, user, _collect_entries(results), unit="张")


def _render_entry_results(service, user, entries: list[dict], unit: str = "张") -> None:
    ok_count = sum(1 for e in entries if e["error"] is None)
    st.success(f"完成：成功 {ok_count} / {len(entries)} {unit}，已自动归档入错题本。")

    if ok_count:
        nav_col, _ = st.columns([1, 2])
        with nav_col:
            if st.button("📒 去错题本查看", type="primary"):
                go_to("notebook")

    for i, entry in enumerate(entries):
        with st.expander(
            f"{'✅ ' if entry['error'] is None else '❌ '}{entry['name']}", expanded=(i == 0)
        ):
            if entry["error"]:
                st.error(f"解析失败：{entry['error']}")
                continue
            if entry["duplicated"]:
                st.warning("检测到重复上传：已为你复用既有错题记录。")
            _render_analysis(entry["question"], entry["analysis"], service, user)


def _similar_constraints(question) -> dict:
    return {
        "subject": question.subject,
        "grade": question.grade,
        "region": question.region,
        "difficulty": question.difficulty,
        "knowledge_points": list(question.knowledge_points or []),
        "question_type": question.question_type,
    }


def _render_analysis(saved, analysis, service, user) -> None:
    img_col, content_col = st.columns([2, 3])
    with img_col:
        if getattr(saved, "image_path", None):
            st.image(saved.image_path, width="stretch")
        else:
            st.info("原图已不可用（可能经过数据目录迁移），解析结果不受影响。")
        badges = " ".join(f'<span class="mm-badge">{t}</span>' for t in saved.tags)
        st.markdown(
            f"""<div style="margin-top:0.5rem">
            <span class="mm-badge mm-badge--blue">难度：{saved.difficulty}</span>{badges}
            </div>""",
            unsafe_allow_html=True,
        )
    with content_col:
        meta_bits = []
        if getattr(analysis, "question_type", ""):
            meta_bits.append(f"题型：{analysis.question_type}")
        if getattr(analysis, "chapter", ""):
            meta_bits.append(f"章节：{analysis.chapter}")
        if getattr(analysis, "error_category", None):
            meta_bits.append(f"错因：{analysis.error_category}")
        if meta_bits:
            st.caption(" · ".join(meta_bits))
        st.markdown(f"**考点**：{'、'.join(analysis.knowledge_points)}")
        render_markdown(analysis.analysis, unsafe_allow_html=True)
        render_markdown(f"**正确答案**：{analysis.answer}")
        if analysis.mistake_cause:
            st.info(f"常见错因：{analysis.mistake_cause}")
        if analysis.followup_question:
            with st.expander("举一反三 · 变式练习"):
                render_markdown(analysis.followup_question)
                save_followup_button(service, saved, user, analysis.followup_question)

        st.divider()
        st.markdown("**相似错题（向量召回）**")
        similar = service.similar_questions(
            saved, user_id=user["id"], **_similar_constraints(saved)
        ).items
        if similar:
            for index, q in enumerate(similar):
                source_mark = {"bank": "公共题库", "generated": "AI 变式"}.get(q.source)
                prefix = f"[{source_mark}] " if source_mark else ""
                st.markdown(
                    f"- {prefix}🏷️ {'、'.join(q.tags[:3])} · "
                    f"<span class='mm-muted'>{safe_truncate_math(q.content_markdown, 60)}</span>",
                    unsafe_allow_html=True,
                )
                render_figures(
                    q.figure_paths or [], alt_prefix=f"题 {saved.id} 的同类题图形"
                )
                if q.source == "generated":
                    save_followup_button(
                        service,
                        q,
                        user,
                        q.content_markdown,
                        figure_paths=list(q.figure_paths or []),
                        figure_specs=list(q.figure_specs or []),
                        key_suffix=f"{saved.id}_{index}",
                    )
        else:
            st.caption("暂无相似错题。随着错题积累，这里会自动出现同知识点的历史题目。")

    with st.expander("💬 就这道题追问老师"):
        followup_chat(service, saved, user)
