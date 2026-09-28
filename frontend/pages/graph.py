"""知识图谱页：错题标签共现的力导向图，洞察知识点关联结构。"""
from __future__ import annotations

from itertools import combinations

import networkx as nx
import plotly.graph_objects as go
import streamlit as st

from frontend.common import get_question_service, page_header

_PALETTE = [
    "#2563eb", "#0d9488", "#d97706", "#dc2626", "#7c3aed",
    "#059669", "#db2777", "#0891b2", "#65a30d", "#c2410c",
]

_TOP_TAGS = 15
_TOP_PAIRS = 8


def _ink() -> str:
    """图表文字色：跟随深色模式开关（plotly 不吃 CSS 变量，需手动适配）。"""
    return "#e2e8f0" if st.session_state.get("dark_mode") else "#334155"


def _edge_color() -> str:
    return "rgba(148,163,184,0.55)" if st.session_state.get("dark_mode") else "rgba(100,116,139,0.45)"


def _build_figure(
    top_tags: list[str],
    tag_count: dict[str, int],
    edge_count: dict[tuple[str, str], int],
) -> go.Figure:
    """用 networkx spring 布局 + plotly 散点画力导向共现图（固定随机种子保证稳定）。"""
    top_set = set(top_tags)
    edges = [(a, b, w) for (a, b), w in edge_count.items() if a in top_set and b in top_set]

    graph = nx.Graph()
    graph.add_nodes_from(top_tags)
    for a, b, w in edges:
        graph.add_edge(a, b, weight=w)
    pos = nx.spring_layout(graph, weight="weight", seed=42, k=1.6, iterations=80)

    max_count = max(tag_count[t] for t in top_tags)
    max_weight = max(w for _, _, w in edges)

    # 边：按共现频率分组画线段，线粗=频率
    edge_traces = []
    for a, b, w in edges:
        x0, y0 = pos[a]
        x1, y1 = pos[b]
        edge_traces.append(go.Scatter(
            x=[x0, x1], y=[y0, y1], mode="lines",
            line={"width": 1 + 2.4 * w / max_weight, "color": _edge_color()},
            hoverinfo="text",
            hovertext=f"{a} × {b}<br>共现 <b>{w}</b> 次",
            showlegend=False,
        ))

    # 节点：大小=错题数；hover 显示错题数与最强关联
    neighbors: dict[str, list[tuple[str, int]]] = {t: [] for t in top_tags}
    for a, b, w in edges:
        neighbors[a].append((b, w))
        neighbors[b].append((a, w))

    node_x, node_y, node_size, node_color, node_text, node_hover = [], [], [], [], [], []
    for i, tag in enumerate(top_tags):
        x, y = pos[tag]
        node_x.append(x)
        node_y.append(y)
        node_size.append(24 + 30 * tag_count[tag] / max_count)
        node_color.append(_PALETTE[i % len(_PALETTE)])
        node_text.append(tag)
        top_links = sorted(neighbors[tag], key=lambda kv: kv[1], reverse=True)[:3]
        links_html = "<br>".join(f"· {name}（{w} 次）" for name, w in top_links) or "· 暂无共现"
        node_hover.append(
            f"<b>{tag}</b><br>错题 {tag_count[tag]} 道<br>最强关联：<br>{links_html}"
        )

    node_trace = go.Scatter(
        x=node_x, y=node_y, mode="markers+text",
        marker={
            "size": node_size, "color": node_color,
            "line": {"width": 2, "color": "rgba(255,255,255,0.85)"},
            "opacity": 0.92,
        },
        text=node_text, textposition="top center",
        textfont={"size": 11, "color": _ink()},
        hoverinfo="text", hovertext=node_hover,
        showlegend=False,
    )

    fig = go.Figure(data=[*edge_traces, node_trace])
    fig.update_layout(
        height=560,
        margin={"l": 10, "r": 10, "t": 10, "b": 10},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        xaxis={"visible": False, "showgrid": False, "zeroline": False},
        yaxis={"visible": False, "showgrid": False, "zeroline": False, "scaleanchor": "x"},
        hovermode="closest",
        font={"color": _ink()},
    )
    return fig


def _render_pair_list(edge_count: dict[tuple[str, str], int]) -> None:
    """右侧「关联最强的知识点对」：一行一对紧凑卡片，成对 chips 对齐。"""
    strongest = sorted(edge_count.items(), key=lambda kv: kv[1], reverse=True)[:_TOP_PAIRS]
    rows = "".join(
        "<div class='mm-pair'>"
        f"<span class='mm-pair__rank'>{i + 1}</span>"
        f"<span class='mm-pair__tags'>"
        f"<span class='mm-badge'>{a}</span>"
        f"<span class='mm-pair__x'>×</span>"
        f"<span class='mm-badge'>{b}</span>"
        f"</span>"
        f"<span class='mm-badge mm-badge--blue mm-pair__count'>{weight} 次</span>"
        "</div>"
        for i, ((a, b), weight) in enumerate(strongest)
    )
    st.markdown(f"<div class='mm-pair-list'>{rows}</div>", unsafe_allow_html=True)


def render_graph_page(user: dict) -> None:
    service = get_question_service()
    page_header("知识图谱", "标签共现网络 · 节点大小=错题数，连线的粗细=两种知识点同时出现的频率")

    questions = service.list_questions(user["id"], include_others=user["role"] == "teacher")
    if not questions:
        st.info("还没有错题，先去「AI 录题」上传几张错题照片，图谱会随错题积累自动生长。")
        return

    tag_count: dict[str, int] = {}
    edge_count: dict[tuple[str, str], int] = {}
    for q in questions:
        tags = sorted({t for t in (q.tags or []) if t})
        for tag in tags:
            tag_count[tag] = tag_count.get(tag, 0) + 1
        for a, b in combinations(tags, 2):
            edge_count[(a, b)] = edge_count.get((a, b), 0) + 1

    top_tags = sorted(tag_count, key=tag_count.get, reverse=True)[:_TOP_TAGS]
    top_set = set(top_tags)
    visible_edges = {k: v for k, v in edge_count.items() if k[0] in top_set and k[1] in top_set}
    if not visible_edges:
        st.warning("错题数量还太少，标签之间尚未形成共现关系。多积累几道错题后再来看图谱。")
        return

    c_graph, c_insight = st.columns([3, 1])
    with c_graph:
        fig = _build_figure(top_tags, tag_count, visible_edges)
        st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})
    with c_insight:
        st.markdown("#### 关联最强的知识点对")
        _render_pair_list(visible_edges)
        st.caption("同时出现在同一道错题中的知识点，往往需要一起复习。")
