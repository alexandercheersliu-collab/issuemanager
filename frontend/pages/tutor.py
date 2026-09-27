"""AI 录题页：上传错题图片 → 结构化解析 → 自动入库与向量索引 → 举一反三。"""
from __future__ import annotations

import streamlit as st

from backend.models.schemas import ERROR_CATEGORIES, SUBJECT_NAMES
from backend.services.entry_types import EntryResult
from backend.services.question_service import sanitize_tags
from backend.utils.logging import get_logger
from frontend.common import followup_chat, get_question_service, go_to, page_header
from frontend.components import save_followup_button

logger = get_logger("tutor")

_GRADE_OPTIONS = ["不限"] + [f"{g} 年级" for g in range(1, 13)]


def _k12_context_inputs(key_prefix: str) -> dict:
    """学科/年级/地区/教材版本录入控件（拍照与手动两个 Tab 共用）。

    返回可直接传给服务层的上下文字典；界面层纯控件，服务调用方负责传参。
    """
    subject_labels = list(SUBJECT_NAMES.values())
    col_s, col_g = st.columns(2)
    with col_s:
        subject_label = st.selectbox(
            "学科", subject_labels, index=0, key=f"{key_prefix}_subject"
        )
    with col_g:
        grade_label = st.selectbox("年级", _GRADE_OPTIONS, index=0, key=f"{key_prefix}_grade")
    col_r, col_t = st.columns(2)
    with col_r:
        region = st.text_input(
            "地区（可选）", placeholder="例如：北京", key=f"{key_prefix}_region"
        )
    with col_t:
        textbook = st.text_input(
            "教材版本（可选）", placeholder="例如：人教版", key=f"{key_prefix}_textbook"
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
    page_header("AI 录题", "上传错题照片，自动完成考点分析、详解与归档")

    info = service.ai.provider_info()
    if info.demo_mode:
        st.warning("当前为演示模式：未配置 AI_API_KEY，返回内置示例解析。在 .env 配置后即可调用真实视觉模型。")

    tab_photo, tab_manual = st.tabs(["📸 拍照录题", "⌨️ 手动录入"])

    with tab_photo:
        with st.container(border=True):
            col_upload, col_meta = st.columns([3, 2])
            with col_upload:
                uploads = st.file_uploader(
                    "错题图片（支持多选）",
                    type=["jpg", "jpeg", "png", "webp"],
                    accept_multiple_files=True,
                )
                if uploads:
                    preview_cols = st.columns(min(len(uploads), 4))
                    for i, upload in enumerate(uploads[:4]):
                        with preview_cols[i]:
                            st.image(upload.getvalue(), width="stretch", caption=upload.name)
                    if len(uploads) > 4:
                        st.caption(f"已选择 {len(uploads)} 张图片")
            with col_meta:
                context = _k12_context_inputs("photo")
                tags_input = st.text_input("标签（可选，逗号分隔）", placeholder="例如：期末复习, 几何", key="photo_tags")
                hint = st.text_area(
                    "给老师的话（可选）",
                    placeholder="例如：第二问总是不知道从哪里下手",
                    height=68,
                )

            # 切题计划跨 rerun 存于 session_state；上传集合变化即作废
            plan = st.session_state.get("photo_plan")
            current_names = [u.name for u in uploads] if uploads else []
            if plan is not None and plan.get("upload_names") != current_names:
                plan = None
                st.session_state.pop("photo_plan", None)

            if uploads and st.button("开始 AI 解析", type="primary", width="stretch"):
                with st.spinner("正在从图中找题…"):
                    plan = _plan_photo_entry(
                        service.ai,
                        [(u.name, u.getvalue(), u.type or "image/jpeg") for u in uploads],
                    )
                    plan["upload_names"] = current_names
                st.session_state["photo_plan"] = plan

            if plan is not None:
                if not plan["candidates"]:
                    # 所有图片都只切出单题或切题失败：行为与原整图解析完全一致
                    _process_uploads(
                        service, user, uploads, sanitize_tags(tags_input), hint, context
                    )
                    st.session_state.pop("photo_plan", None)
                else:
                    _render_candidate_review(
                        service, user, plan, tags_input, hint, context
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


# ---------- 拍照切题（从图中找题）----------
# 以下函数均不依赖 Streamlit，可单测：切题判定 / 候选整形 / 默认勾选 / 批量入库。


def should_use_segmentation(segments) -> bool:
    """切出 ≥2 道题才走逐题勾选；单题、空结果或异常（None）回退整图解析。"""
    return isinstance(segments, list) and len(segments) >= 2


def summarize_stem(text: str, limit: int = 50) -> str:
    """题干摘要：压缩空白后截断，供候选清单展示。"""
    clean = " ".join((text or "").split())
    return clean if len(clean) <= limit else clean[:limit] + "…"


def build_candidates(image_name: str, segments: list[dict]) -> list[dict]:
    """把一张图的切题结果整形成候选清单条目（标注来源图片）。"""
    return [
        {
            "image": image_name,
            "number": str(s.get("number", "")).strip() or "?",
            "text": str(s.get("text", "") or "").strip(),
            "summary": summarize_stem(str(s.get("text", "") or "")),
            "likely_wrong": bool(s.get("likely_wrong", False)),
        }
        for s in segments
    ]


def default_checked(candidate: dict) -> bool:
    """勾选框默认值：疑似做错的题（likely_wrong）默认勾选。"""
    return bool(candidate.get("likely_wrong", False))


def _plan_photo_entry(ai, images: list[tuple[str, bytes, str]]) -> dict:
    """逐张切题并合并候选清单；单题/空结果/切题异常的图进 fallbacks 走整图解析。

    images: [(文件名, 字节, MIME), ...]。返回 {"candidates": [...], "fallbacks": [...]}。
    """
    candidates: list[dict] = []
    fallbacks: list[dict] = []
    for name, data, mime in images:
        try:
            segments = ai.segment_page(data, mime)
        except Exception as exc:  # noqa: BLE001 - 切题失败回退整图解析
            logger.info("切题失败，回退整图解析 image=%s: %s", name, exc)
            segments = None
        if should_use_segmentation(segments):
            for cand in build_candidates(name, segments):
                candidates.append({**cand, "image_bytes": data})
        else:
            fallbacks.append({"name": name, "bytes": data, "mime": mime})
    return {"candidates": candidates, "fallbacks": fallbacks}


def _save_checked_candidates(
    service,
    user_id: int,
    candidates: list[dict],
    tags: list[str],
    hint: str,
    context: dict,
    on_progress=None,
) -> list:
    """勾选题逐题解构入库（同一图片的多题共享同一份落盘原图）。

    返回 _collect_entries 兼容的 (名称, EntryResult|None, 错误) 三元组列表。
    """
    results = []
    path_cache: dict[str, str] = {}
    for cand in candidates:
        name = f"{cand['image']} · 第 {cand['number']} 题"
        try:
            if cand["image"] not in path_cache:
                path_cache[cand["image"]] = str(
                    service._persist_image(user_id, cand["image_bytes"])
                )
            out, analysis = service.analyze_text_and_save(
                user_id,
                cand["text"],
                user_tags=tags,
                hint=hint,
                image_path=path_cache[cand["image"]],
                subject=context["subject"],
                grade=context["grade"],
                region=context["region"],
                textbook_version=context["textbook_version"],
            )
            results.append(
                (name, EntryResult(question=out, analysis=analysis, duplicated=False), None)
            )
        except Exception as exc:  # noqa: BLE001 - 单题失败不影响其余
            results.append((name, None, str(exc)))
        if on_progress is not None:
            on_progress(name)
    return results


def _render_candidate_review(service, user, plan: dict, tags_input: str, hint: str, context: dict) -> None:
    """候选清单勾选界面：疑似做错的题默认勾选，确认后批量解构入库。"""
    candidates = plan["candidates"]
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
        if st.checkbox(label, value=default_checked(cand), key=f"photo_seg_{i}"):
            checked.append(cand)
    if plan["fallbacks"]:
        names = "、".join(f["name"] for f in plan["fallbacks"])
        st.caption(f"以下图片未切出多题，将按整图单题解析：{names}")

    if st.button(f"确认录入（{len(checked)} 题）", type="primary", width="stretch"):
        if not checked and not plan["fallbacks"]:
            st.warning("请至少勾选一道题。")
            return
        _process_plan(service, user, plan, checked, sanitize_tags(tags_input), hint, context)
        st.session_state.pop("photo_plan", None)


def _process_plan(service, user, plan: dict, checked: list[dict], tags: list[str], hint: str, context: dict) -> None:
    """批量入库：勾选题逐题解构 + 回退图整图解析，统一渲染结果。"""
    total = len(checked) + len(plan["fallbacks"])
    progress = st.progress(0.0, text="准备解析…")
    state = {"done": 0}

    def _tick(label: str) -> None:
        state["done"] += 1
        progress.progress(state["done"] / total, text=label)

    results = _save_checked_candidates(
        service, user["id"], checked, tags, hint, context,
        on_progress=lambda name: _tick(f"正在解构 {name}"),
    )
    for fb in plan["fallbacks"]:
        try:
            entry = service.analyze_and_save_dedup(
                user["id"],
                fb["bytes"],
                mime_type=fb["mime"],
                user_tags=tags,
                hint=hint,
                subject=context["subject"],
                grade=context["grade"],
                region=context["region"],
                textbook_version=context["textbook_version"],
            )
            results.append((fb["name"], entry, None))
        except Exception as exc:  # noqa: BLE001 - 单张失败不影响其余
            results.append((fb["name"], None, str(exc)))
        _tick(f"正在解析 {fb['name']}")
    progress.progress(1.0, text="解析完成")
    _render_entry_results(service, user, _collect_entries(results), unit="项")


def _process_uploads(service, user, uploads, tags: list[str], hint: str, context: dict) -> None:
    progress = st.progress(0.0, text="准备解析…")
    results = []
    for idx, upload in enumerate(uploads, 1):
        progress.progress(
            (idx - 1) / len(uploads), text=f"正在解析 {upload.name}（{idx}/{len(uploads)}）"
        )
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
    progress.progress(1.0, text="解析完成")
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
        st.markdown(analysis.analysis, unsafe_allow_html=True)
        st.markdown(f"**正确答案**：{analysis.answer}")
        if analysis.mistake_cause:
            st.info(f"常见错因：{analysis.mistake_cause}")
        if analysis.followup_question:
            with st.expander("举一反三 · 变式练习"):
                st.markdown(analysis.followup_question)
                save_followup_button(service, saved, user, analysis.followup_question)

        st.divider()
        st.markdown("**相似错题（向量召回）**")
        similar = service.similar_questions(saved, user_id=user["id"]).items
        if similar:
            for q in similar:
                source_mark = {"bank": "公共题库", "generated": "AI 变式"}.get(q.source)
                prefix = f"[{source_mark}] " if source_mark else ""
                st.markdown(
                    f"- {prefix}🏷️ {'、'.join(q.tags[:3])} · "
                    f"<span class='mm-muted'>{q.content_markdown[:60]}…</span>",
                    unsafe_allow_html=True,
                )
        else:
            st.caption("暂无相似错题。随着错题积累，这里会自动出现同知识点的历史题目。")

    with st.expander("💬 就这道题追问老师"):
        followup_chat(service, saved, user)
