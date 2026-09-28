"""RAG 服务：基于 ChromaDB 的错题向量库。

能力：
1. 错题解析文本入库（含知识点/标签元数据）
2. 「举一反三」相似题召回
3. 错题本语义搜索（自然语言找题，不依赖标签完全匹配）

嵌入模型策略：
- 配置了 EMBEDDING_BASE_URL/KEY → 使用 OpenAI 兼容嵌入接口（如 BGE-M3）
- 未配置 → 使用 ChromaDB 内置本地嵌入模型，零外部依赖

任何环节故障均自动降级为关键词检索，保证主流程不被向量库阻断。
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from backend.config import Settings, get_settings
from backend.utils.logging import get_logger

logger = get_logger("rag")


@dataclass
class RagHit:
    question_id: int  # 用户错题 SQL id；公共题库命中为 -1（bank_id 取真实 id）
    distance: float
    tags: list[str] = field(default_factory=list)
    snippet: str = ""
    source: str = "own"  # own（用户错题库）/ bank（公共种子题库）
    bank_id: str | None = None
    metadata: dict = field(default_factory=dict)  # 透传元数据，bank 命中展示用


# ---------- 同类题约束检索（P3.1） ----------

# 难度序：±1 档过滤的基准
DIFFICULTY_ORDER = ("easy", "medium", "hard")


def difficulty_window(target: str | None) -> frozenset[str] | None:
    """目标难度的 ±1 档集合；未指定或非法值返回 None（该维度不过滤）。"""
    if not target or target not in DIFFICULTY_ORDER:
        return None
    i = DIFFICULTY_ORDER.index(target)
    return frozenset(DIFFICULTY_ORDER[max(0, i - 1) : i + 2])


@dataclass
class SimilarConstraints:
    """同类题检索的 K12 硬过滤约束；全部为 None/空 时等价于不过滤。"""

    subject: str | None = None
    grade: int | None = None
    region: str | None = None
    knowledge_points: list[str] = field(default_factory=list)
    difficulty: str | None = None
    question_type: str | None = None  # 题型（应用题/计算题…）：同知识点不同题型不算同类

    def is_empty(self) -> bool:
        return not (
            self.subject
            or self.grade is not None
            or self.region
            or self.knowledge_points
            or self.difficulty
            or self.question_type
        )


@dataclass
class SimilarResult:
    """约束检索结果：hits + 实际生效的过滤层级（供调用方记录/展示放宽情况）。"""

    hits: list[RagHit] = field(default_factory=list)
    level: int = 0  # 0 全约束；1 放地区；2 再放年级；3 仅学科；4 无约束

    @property
    def relaxed(self) -> bool:
        return self.level > 0


def _dim_match(meta_value, target) -> bool:
    """单维度匹配：未约束（target 为 None）或元数据键缺失（meta_value 为 None）均放行。"""
    return target is None or meta_value is None or meta_value == target


# 题型相容组：同组内互为同类。应用题本质上是带情境的解答题，AI 标注常在两者间摇摆；
# 但计算题与应用题/解答题绝不互为同类（纯运算 vs 情境建模，考查能力不同）。
_TYPE_COMPAT_GROUPS = ({"应用题", "解答题"},)


def _type_match(meta_value, target) -> bool:
    """题型匹配：键缺失/未约束放行；相等或同属相容组放行。"""
    if target is None or meta_value is None or meta_value == target:
        return True
    return any(target in group and meta_value in group for group in _TYPE_COMPAT_GROUPS)


def matches_constraints(meta: dict, constraints: SimilarConstraints, level: int) -> bool:
    """元数据是否满足指定层级的约束（键缺失放行，保证 P1 前的旧向量文档不被误排）。

    层级：0=学科+年级+地区+知识点+题型+难度；1=放地区；2=再放年级；
    3=仅学科+题型；4=仅学科+题型（其余全放）。约束为空时任意层级都通过。
    题型与学科为全程硬过滤（所有层级生效）：应用题/计算题不互为同类，
    即使其余维度全放宽也绝不跨题型召回；库内无同题型候选时宁可返回空，
    由上层回落公共题库或 AI 生成（生成提示词同样锁题型）。
    """
    if constraints.is_empty():
        return True
    if not _dim_match(meta.get("subject"), constraints.subject):
        return False
    if not _type_match(meta.get("question_type"), constraints.question_type):
        return False
    if level >= 4:
        return True
    if level <= 2:
        if not _dim_match(meta.get("grade"), constraints.grade):
            return False
        window = difficulty_window(constraints.difficulty)
        difficulty = meta.get("difficulty")
        if window is not None and difficulty is not None and difficulty not in window:
            return False
        if constraints.knowledge_points and "knowledge_points" in meta:
            meta_points = {p for p in str(meta.get("knowledge_points") or "").split(",") if p}
            if meta_points and not (meta_points & set(constraints.knowledge_points)):
                return False
    if level <= 0 and not _dim_match(meta.get("region"), constraints.region):
        return False
    return True


# 宽松模式下的逐层放宽顺序（先放地区、再放年级）
RELAX_LADDER = (0, 1, 2, 3, 4)


class QuestionVectorStore:
    """ChromaDB 持久化向量库的轻量封装。"""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self._client = None
        self._collection = None
        self._bank_collection = None
        self._available: bool | None = None  # None = 未探测

    # ---------- 惰性初始化 ----------
    def _ensure_client(self):
        """创建（并缓存）ChromaDB 客户端；失败时置 _available=False 并返回 None。"""
        if self._client is not None:
            return self._client
        if self._available is False:
            return None
        if not self.settings.rag_enabled:
            self._available = False
            return None
        try:
            import chromadb

            self._client = chromadb.PersistentClient(path=str(self.settings.chroma_dir))
            self._available = True
        except Exception as exc:  # noqa: BLE001 - 向量库故障不阻断主流程
            logger.warning("向量库初始化失败，已降级为关键词检索: %s", exc)
            self._client = None
            self._available = False
        return self._client

    def _ensure_collection(self):
        if self._collection is not None:
            return self._collection
        client = self._ensure_client()
        if client is None:
            return None
        try:
            from chromadb.utils import embedding_functions

            embed_fn = self._build_embedding_fn(embedding_functions)
            self._collection = client.get_or_create_collection(
                name="questions",
                embedding_function=embed_fn,
                metadata={"hnsw:space": "cosine"},
            )
        except Exception as exc:  # noqa: BLE001 - 向量库故障不阻断主流程
            logger.warning("向量集合初始化失败，已降级为关键词检索: %s", exc)
            self._collection = None
            self._available = False
        return self._collection

    def _ensure_bank_collection(self):
        """公共种子题库集合：跨用户共享，不按 user_id 隔离（P3.1）。"""
        if self._bank_collection is not None:
            return self._bank_collection
        client = self._ensure_client()
        if client is None:
            return None
        try:
            from chromadb.utils import embedding_functions

            embed_fn = self._build_embedding_fn(embedding_functions)
            self._bank_collection = client.get_or_create_collection(
                name=self.settings.rag_bank_collection,
                embedding_function=embed_fn,
                metadata={"hnsw:space": "cosine"},
            )
        except Exception as exc:  # noqa: BLE001 - 公共库故障不影响用户库检索
            logger.warning("公共题库集合初始化失败: %s", exc)
            self._bank_collection = None
        return self._bank_collection

    def _build_embedding_fn(self, embedding_functions):
        if self.settings.embedding_base_url and self.settings.embedding_api_key:
            logger.info("使用远程嵌入模型: %s", self.settings.embedding_model)
            return embedding_functions.OpenAIEmbeddingFunction(
                api_key=self.settings.embedding_api_key,
                api_base=self.settings.embedding_base_url,
                model_name=self.settings.embedding_model,
            )
        model_dir = self.settings.chroma_model_dir
        if model_dir is not None:
            # ChromaDB 把 ONNX 模型缓存的父目录写死为「用户主目录/.cache/chroma/onnx_models」，
            # 且不读取任何环境变量；这里显式改写类属性，让模型落到配置的数据盘。
            # 最终路径 = {chroma_model_dir}/all-MiniLM-L6-v2/onnx/…
            # 不直接取 embedding_functions.ONNXMiniLM_L6_V2：该名字未必由包入口重导出，
            # 取不到时退回子模块导入，避免整个向量库因此降级为关键词检索。
            onnx_cls = getattr(embedding_functions, "ONNXMiniLM_L6_V2", None)
            if onnx_cls is None:
                try:
                    from chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import (  # noqa: PLC0415
                        ONNXMiniLM_L6_V2 as onnx_cls,
                    )
                except ImportError:
                    onnx_cls = None
            if onnx_cls is not None:
                onnx_cls.DOWNLOAD_PATH = model_dir / "all-MiniLM-L6-v2"
            logger.info("内置嵌入模型缓存目录: %s", model_dir)
        logger.info("使用 ChromaDB 内置本地嵌入模型")
        return embedding_functions.DefaultEmbeddingFunction()

    def is_available(self) -> bool:
        return bool(self._ensure_collection() is not None)

    # ---------- 写入 / 删除 ----------
    def upsert_question(
        self,
        question_id: int,
        text: str,
        *,
        user_id: int,
        tags: list[str],
        created_at: dt.datetime | None = None,
        subject: str | None = None,
        grade: int | None = None,
        region: str | None = None,
        knowledge_points: list[str] | None = None,
        difficulty: str | None = None,
        question_type: str | None = None,
    ) -> bool:
        """错题向量入库；K12 元数据（学科/年级/地区/知识点/难度/题型）随文档写入，
        供 P3 同类题检索做元数据硬过滤。ChromaDB 元数据值不接受 None，
        未提供的字段直接省略。"""
        collection = self._ensure_collection()
        if collection is None or not text.strip():
            return False
        metadata: dict = {
            "user_id": user_id,
            "tags": ",".join(tags),
            "created": (created_at or dt.datetime.now(dt.timezone.utc)).strftime("%Y-%m-%d"),
        }
        if subject:
            metadata["subject"] = subject
        if grade is not None:
            metadata["grade"] = grade
        if region:
            metadata["region"] = region
        if knowledge_points:
            metadata["knowledge_points"] = ",".join(knowledge_points)
        if difficulty:
            metadata["difficulty"] = difficulty
        if question_type:
            metadata["question_type"] = question_type
        try:
            collection.upsert(
                ids=[str(question_id)],
                documents=[text[:4000]],
                metadatas=[metadata],
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("向量入库失败 (question=%s): %s", question_id, exc)
            return False

    def delete_questions(self, question_ids: list[int]) -> None:
        collection = self._ensure_collection()
        if collection is None or not question_ids:
            return
        try:
            collection.delete(ids=[str(qid) for qid in question_ids])
        except Exception as exc:  # noqa: BLE001
            logger.warning("向量删除失败: %s", exc)

    # ---------- 检索 ----------
    def _is_leak(self, distance: float) -> bool:
        """防泄题：cosine 空间 distance = 1 - 相似度，相似度 ≥ 阈值视为泄题候选。"""
        return (1.0 - float(distance)) >= self.settings.rag_leak_threshold

    def similar_questions(
        self,
        query_text: str,
        *,
        user_ids: list[int],
        exclude_id: int | None = None,
        top_k: int | None = None,
    ) -> list[RagHit]:
        return self._query(
            query_text, user_ids=user_ids, exclude_id=exclude_id, top_k=top_k, leak_filter=True
        )

    def semantic_search(
        self, query: str, *, user_ids: list[int], top_k: int | None = None
    ) -> list[RagHit]:
        # 语义搜索不做防泄题过滤（搜索场景本来就想找原文/近重复）
        return self._query(query, user_ids=user_ids, exclude_id=None, top_k=top_k)

    def constrained_similar(
        self,
        query_text: str,
        *,
        user_ids: list[int],
        exclude_id: int | None = None,
        constraints: SimilarConstraints | None = None,
        strict: bool = False,
        top_k: int | None = None,
    ) -> SimilarResult:
        """同类题约束召回：K12 硬过滤 + 防泄题，宽松模式按 RELAX_LADDER 逐层放宽。

        约束按「元数据键缺失放行」语义在召回后过滤（ChromaDB where 不支持
        $exists，无法在 where 层表达键缺失），召回池按超采系数放大。
        strict=True 时只用全约束层级，不足也不放宽。
        """
        collection = self._ensure_collection()
        if collection is None or not query_text.strip():
            return SimilarResult()
        constraints = constraints or SimilarConstraints()
        top_k = top_k or self.settings.rag_top_k
        where = {"user_id": {"$in": user_ids}} if user_ids else None
        ladder = (0,) if strict else RELAX_LADDER
        hits: list[RagHit] = []
        seen: set = set()
        applied_level = ladder[0]
        for level in ladder:  # 级联放宽：严格层结果保留，不足从下一层补齐
            applied_level = level
            for hit in self._fetch_filtered(
                collection,
                query_text,
                where=where,
                exclude_id=exclude_id,
                constraints=constraints,
                level=level,
                top_k=top_k,
                source="own",
            ):
                if hit.question_id in seen:
                    continue
                seen.add(hit.question_id)
                hits.append(hit)
                if len(hits) >= top_k:
                    break
            if len(hits) >= top_k:
                break
        return SimilarResult(hits=hits, level=applied_level)

    def _fetch_filtered(
        self,
        collection,
        query_text: str,
        *,
        where: dict | None,
        exclude_id: int | None,
        constraints: SimilarConstraints,
        level: int,
        top_k: int,
        source: str,
    ) -> list[RagHit]:
        """超采召回 →（键缺失放行）约束过滤 → 防泄题 → 截断 top_k。"""
        oversample = max(1, self.settings.rag_filter_oversample)
        n_results = top_k * oversample + (1 if exclude_id else 0)
        try:
            result = collection.query(
                query_texts=[query_text],
                n_results=n_results,
                where=where,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("向量检索失败: %s", exc)
            return []

        hits: list[RagHit] = []
        ids = (result.get("ids") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]
        documents = (result.get("documents") or [[]])[0]
        metadatas = (result.get("metadatas") or [[]])[0]
        for raw_id, dist, doc, meta in zip(ids, distances, documents, metadatas, strict=False):
            meta = dict(meta or {})
            if source == "own":
                numeric_id = int(raw_id)
                if exclude_id is not None and numeric_id == exclude_id:
                    continue
                bank_id = None
            else:
                numeric_id, bank_id = -1, str(raw_id)
            if not matches_constraints(meta, constraints, level):
                continue
            if self._is_leak(dist):
                continue  # 防泄题：与源题过近的近重复
            hits.append(
                RagHit(
                    question_id=numeric_id,
                    distance=float(dist),
                    tags=[t for t in str(meta.get("tags", "")).split(",") if t],
                    snippet=(doc or "")[:200],
                    source=source,
                    bank_id=bank_id,
                    metadata=meta,
                )
            )
            if len(hits) >= top_k:
                break
        return hits

    def _query(
        self,
        query_text: str,
        *,
        user_ids: list[int],
        exclude_id: int | None,
        top_k: int | None,
        leak_filter: bool = False,
    ) -> list[RagHit]:
        collection = self._ensure_collection()
        if collection is None or not query_text.strip():
            return []
        top_k = top_k or self.settings.rag_top_k
        where = {"user_id": {"$in": user_ids}} if user_ids else None
        try:
            # where 过滤由 chromadb 在查询时执行，无需先 count
            result = collection.query(
                query_texts=[query_text],
                n_results=top_k + (1 if exclude_id else 0),
                where=where,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("向量检索失败: %s", exc)
            return []

        hits: list[RagHit] = []
        ids = (result.get("ids") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]
        documents = (result.get("documents") or [[]])[0]
        metadatas = (result.get("metadatas") or [[]])[0]
        for qid, dist, doc, meta in zip(ids, distances, documents, metadatas, strict=False):
            numeric_id = int(qid)
            if exclude_id is not None and numeric_id == exclude_id:
                continue
            if leak_filter and self._is_leak(dist):
                continue
            hits.append(
                RagHit(
                    question_id=numeric_id,
                    distance=float(dist),
                    tags=[t for t in str(meta.get("tags", "")).split(",") if t],
                    snippet=(doc or "")[:200],
                    metadata=dict(meta or {}),
                )
            )
            if len(hits) >= top_k:
                break
        return hits

    # ---------- 公共种子题库（P3.1，跨用户共享） ----------
    def upsert_bank_question(
        self,
        bank_id: str,
        text: str,
        *,
        subject: str = "math",
        grade: int | None = None,
        region: str | None = None,
        knowledge_points: list[str] | None = None,
        difficulty: str | None = None,
        answer: str = "",
        question_type: str = "",
        chapter: str = "",
    ) -> bool:
        """种子题入库（幂等 upsert，bank_id 确定性生成即可反复执行）。"""
        collection = self._ensure_bank_collection()
        if collection is None or not text.strip():
            return False
        metadata: dict = {"subject": subject, "answer": answer[:500]}
        if grade is not None:
            metadata["grade"] = grade
        if region:
            metadata["region"] = region
        if knowledge_points:
            metadata["knowledge_points"] = ",".join(knowledge_points)
        if difficulty:
            metadata["difficulty"] = difficulty
        if question_type:
            metadata["question_type"] = question_type
        if chapter:
            metadata["chapter"] = chapter
        try:
            collection.upsert(
                ids=[bank_id],
                documents=[text[:4000]],
                metadatas=[metadata],
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("公共题库入库失败 (bank_id=%s): %s", bank_id, exc)
            return False

    def bank_size(self) -> int:
        collection = self._ensure_bank_collection()
        if collection is None:
            return 0
        try:
            return int(collection.count())
        except Exception:  # noqa: BLE001
            return 0

    def similar_from_bank(
        self,
        query_text: str,
        *,
        constraints: SimilarConstraints | None = None,
        strict: bool = False,
        top_k: int | None = None,
    ) -> SimilarResult:
        """公共题库约束召回（同样防泄题：与源题过近的种子题不推荐）。"""
        collection = self._ensure_bank_collection()
        if collection is None or not query_text.strip():
            return SimilarResult()
        constraints = constraints or SimilarConstraints()
        top_k = top_k or self.settings.rag_top_k
        ladder = (0,) if strict else RELAX_LADDER
        hits: list[RagHit] = []
        seen: set = set()
        applied_level = ladder[0]
        for level in ladder:  # 级联放宽：严格层结果保留，不足从下一层补齐
            applied_level = level
            for hit in self._fetch_filtered(
                collection,
                query_text,
                where=None,
                exclude_id=None,
                constraints=constraints,
                level=level,
                top_k=top_k,
                source="bank",
            ):
                if hit.bank_id in seen:
                    continue
                seen.add(hit.bank_id)
                hits.append(hit)
                if len(hits) >= top_k:
                    break
            if len(hits) >= top_k:
                break
        return SimilarResult(hits=hits, level=applied_level)
