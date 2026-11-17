"""Tiered memory engine (Tier 1 Ephemeral / Tier 2 Recent / Tier 3 Core) + Forgetting GC."""

import asyncio
import inspect
import json
import math
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .model_hub import ModelHub
from .time_utils import current_datetime_text, format_local_datetime
from ..prompts.templates import (
    MEMORY_EXTRACT_AND_GRADE_PROMPT,
    USER_IDENTITY_NORMALIZATION_PROMPT,
)
from ..storage.db import DatabaseManager

try:
    from astrbot.api import logger
except ImportError:
    import logging
    logger = logging.getLogger("onlyyou.memory_manager")


class MemoryTier:
    EPHEMERAL = 1  # 瞬时短时记忆 (48h 物理清理)
    RECENT = 2     # 近期情节记忆 (随时间衰减，访问强化可晋升为 CORE)
    CORE = 3       # 核心永久记忆 (免疫日常遗忘)


# ==============================================================================
# v2.12: 四类记忆分类 + 本地语义去重 / 混合召回所需的纯 Python 工具
# ==============================================================================
#: profile 人物画像 / relationship 关系共识 / state 近期状态 / episode 短期情节
MEMORY_TYPES = ("profile", "relationship", "state", "episode")
MEMORY_TYPE_LABELS = {
    "profile": "人物画像",
    "relationship": "关系共识",
    "state": "近期状态",
    "episode": "短期情节",
}
#: 只有这两类「长期人设 / 关系」允许在严苛条件下晋升 T3。
PROMOTABLE_MEMORY_TYPES = ("profile", "relationship")

#: 写路径语义去重阈值（余弦相似度 / 字符 Bigram Jaccard）。
DEDUPE_COSINE_THRESHOLD = 0.85
DEDUPE_JACCARD_THRESHOLD = 0.65
#: 状态类记忆的「版本替代（Supersede）」相似度区间（同类型 state）。
SUPERSEDE_STATE_COSINE_MIN = 0.72
#: 无向量时 state 的版本替代阈值（低于去重阈值，允许状态演进）。
SUPERSEDE_STATE_JACCARD_MIN = 0.50

#: 读路径：有效召回强化最小语义相关性 + 强化冷却（6 小时）。
REINFORCE_MIN_SEMANTIC_SCORE = 0.55
REINFORCE_COOLDOWN_SECONDS = 6 * 60 * 60
#: T2 -> T3 晋升硬条件：跨时段有效命中次数。
PROMOTE_TO_T3_MIN_ACCESS = 8
PROMOTE_TO_T3_MIN_IMPORTANCE = 8
PROMOTE_TO_T3_MIN_AGE_SECONDS = 24 * 60 * 60

#: 候选池分桶保底比例（T3 / T2 / T1），防止高 tier 吃满候选名额。
CANDIDATE_BUCKET_RATIOS = {3: 0.30, 2: 0.40, 1: 0.30}

#: Tier 1 存活时长：创建为 T1 或从 T2/T3 降级为 T1 时重新计算。
TIER1_TTL_SECONDS = 48 * 60 * 60


def normalize_memory_type(value: Any, default: str = "episode") -> str:
    """Normalize a memory classification into the frozen four-value enum."""
    text = str(value or "").strip().lower()
    return text if text in MEMORY_TYPES else default


def _normalize_similarity_text(text: Any) -> str:
    """Lowercase + strip whitespace/punctuation for n-gram comparison."""
    return re.sub(r"[\s\W_]+", "", str(text or "").lower())


def char_bigrams(text: Any) -> set[str]:
    """Character bigrams of ``text`` (single char text keeps one unigram)."""
    cleaned = _normalize_similarity_text(text)
    if not cleaned:
        return set()
    if len(cleaned) == 1:
        return {cleaned}
    return {cleaned[index : index + 2] for index in range(len(cleaned) - 1)}


def jaccard_similarity(left: Any, right: Any) -> float:
    """Character-bigram Jaccard overlap in ``[0, 1]``."""
    left_set = char_bigrams(left)
    right_set = char_bigrams(right)
    if not left_set or not right_set:
        return 0.0
    return len(left_set & right_set) / float(len(left_set | right_set))


def cosine_similarity(left: Any, right: Any) -> float:
    """Cosine similarity of two numeric vectors, clamped to ``[0, 1]``."""
    if not isinstance(left, (list, tuple)) or not isinstance(right, (list, tuple)):
        return 0.0
    size = min(len(left), len(right))
    if size <= 0:
        return 0.0
    dot = 0.0
    left_norm = 0.0
    right_norm = 0.0
    try:
        for index in range(size):
            a = float(left[index])
            b = float(right[index])
            dot += a * b
            left_norm += a * a
            right_norm += b * b
    except (TypeError, ValueError):
        return 0.0
    if left_norm <= 0.0 or right_norm <= 0.0:
        return 0.0
    return max(0.0, min(1.0, dot / math.sqrt(left_norm * right_norm)))


def tokenize_text(text: Any) -> list[str]:
    """Tokenize Chinese / latin text for BM25 (CJK bigrams + ascii words)."""
    cleaned = str(text or "").lower()
    tokens: list[str] = re.findall(r"[a-z0-9_]+", cleaned)
    for chunk in re.findall(r"[\u4e00-\u9fff]+", cleaned):
        if len(chunk) <= 1:
            tokens.append(chunk)
        else:
            tokens.extend(chunk[index : index + 2] for index in range(len(chunk) - 1))
    return tokens


def retrieval_similarity(query: Any, content: Any, keywords: Any = "") -> float:
    """Lexical relevance used by the read path (content n-grams + curated keywords).

    Curated ``keywords`` are the author's retrieval handles, so an exact keyword
    substring hit in the query counts as evidence even when the surrounding
    sentence shares no n-gram (e.g. query "我家猫拆家了" vs keyword "猫").
    """
    score = lexical_similarity(query, content, keywords)
    query_text = str(query or "")
    keyword_list = [k.strip() for k in str(keywords or "").split(",") if k.strip()]
    if keyword_list and query_text:
        hits = sum(1 for keyword in keyword_list if keyword in query_text)
        if hits:
            score = max(score, min(1.0, 0.5 * hits))
    return max(0.0, min(1.0, score))


def lexical_similarity(query: Any, content: Any, keywords: Any = "") -> float:
    """Normalized lexical relevance of one memory against ``query`` (0~1)."""
    query_tokens = set(tokenize_text(query))
    doc_tokens = set(tokenize_text(f"{content or ''} {keywords or ''}"))
    if not query_tokens or not doc_tokens:
        return 0.0
    overlap = len(query_tokens & doc_tokens)
    if overlap <= 0:
        return 0.0
    coverage = overlap / float(len(query_tokens))
    dice = 2.0 * overlap / float(len(query_tokens) + len(doc_tokens))
    return max(0.0, min(1.0, 0.6 * coverage + 0.4 * dice))


def bm25_scores(query: Any, documents: list[str]) -> list[float]:
    """BM25 scores of ``documents`` against ``query`` in plain Python."""
    query_tokens = tokenize_text(query)
    if not query_tokens or not documents:
        return [0.0 for _ in documents]
    doc_tokens = [tokenize_text(doc) for doc in documents]
    doc_count = len(doc_tokens)
    avg_len = sum(len(tokens) for tokens in doc_tokens) / float(max(1, doc_count))
    k1 = 1.5
    b = 0.75
    scores: list[float] = []
    unique_query = list(dict.fromkeys(query_tokens))
    for tokens in doc_tokens:
        length = len(tokens)
        score = 0.0
        for token in unique_query:
            tf = tokens.count(token)
            if tf <= 0:
                continue
            df = sum(1 for other in doc_tokens if token in other)
            idf = math.log(1.0 + (doc_count - df + 0.5) / (df + 0.5))
            denominator = tf + k1 * (1.0 - b + b * (length / max(1e-9, avg_len)))
            score += idf * (tf * (k1 + 1.0)) / denominator
        scores.append(score)
    return scores


def _load_vector(raw: Any) -> list[float]:
    """Parse a stored ``embedding_json`` payload (``[]`` on any failure)."""
    if isinstance(raw, (list, tuple)):
        vector: list[float] = []
        for value in raw:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return []
            vector.append(float(value))
        return vector
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    vector = []
    for value in parsed:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return []
        vector.append(float(value))
    return vector


def _dump_vector(vector: Any) -> str:
    """Serialize a numeric vector for SQLite storage (``""`` when invalid)."""
    if not isinstance(vector, (list, tuple)) or not vector:
        return ""
    cleaned: list[float] = []
    for value in vector:
        if isinstance(value, bool):
            return ""
        try:
            number = float(value)
        except (TypeError, ValueError):
            return ""
        cleaned.append(round(number, 6))
    return json.dumps(cleaned, separators=(",", ":"))


@dataclass
class MemoryItem:
    """Single row representation of the `memories` table."""

    id: int
    soul_id: str
    user_id: Optional[str]
    user_tier: int
    memory_tier: int
    content: str
    keywords: str
    importance: int
    vitality: float
    access_count: int
    created_at: int
    last_accessed_at: Optional[int]
    # ---- v2.12: RAG / 动态演进字段（旧行 / 手工构造默认值兼容）----
    memory_type: str = "episode"
    event_at: int = 0
    embedding: list[float] = field(default_factory=list)
    superseded_by: Optional[int] = None
    last_reinforced_at: int = 0
    # v2.12: T1 retention start (0 = legacy row -> fall back to created_at)
    tier1_since: int = 0

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "MemoryItem":
        superseded_raw = row.get("superseded_by")
        try:
            superseded_by = int(superseded_raw) if superseded_raw not in (None, "") else None
        except (TypeError, ValueError):
            superseded_by = None
        return cls(
            id=int(row["id"]),
            soul_id=str(row["soul_id"]),
            user_id=row.get("user_id"),
            user_tier=int(row.get("user_tier", 1)),
            memory_tier=int(row.get("memory_tier", 2)),
            content=str(row["content"]),
            keywords=str(row.get("keywords", "")),
            importance=int(row.get("importance", 5)),
            vitality=float(row.get("vitality", 100.0)),
            access_count=int(row.get("access_count", 0)),
            created_at=int(row.get("created_at", 0)),
            last_accessed_at=row.get("last_accessed_at"),
            memory_type=normalize_memory_type(row.get("memory_type")),
            event_at=int(row.get("event_at") or 0),
            embedding=_load_vector(row.get("embedding_json")),
            superseded_by=superseded_by,
            last_reinforced_at=int(row.get("last_reinforced_at") or 0),
            tier1_since=int(row.get("tier1_since") or 0),
        )

    @property
    def effective_event_at(self) -> int:
        """Event time when known, else the formation time (``created_at``)."""
        return int(self.event_at or self.created_at or 0)

    @property
    def is_superseded(self) -> bool:
        return self.superseded_by is not None

    @property
    def tier1_started_at(self) -> int:
        """When the current T1 retention window started (downgrade-aware)."""
        return int(self.tier1_since or self.created_at or 0)

    @property
    def tier1_expires_at(self) -> int:
        """Epoch when this T1 memory expires (0 for non-T1 rows)."""
        if int(self.memory_tier or 0) != MemoryTier.EPHEMERAL:
            return 0
        return self.tier1_started_at + TIER1_TTL_SECONDS

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "soul_id": self.soul_id,
            "user_id": self.user_id,
            "user_tier": self.user_tier,
            "memory_tier": self.memory_tier,
            "content": self.content,
            "keywords": self.keywords,
            "importance": self.importance,
            "vitality": round(float(self.vitality), 1),
            "access_count": self.access_count,
            "created_at": self.created_at,
            "last_accessed_at": self.last_accessed_at,
            # v2.10: human readable local time (the epoch fields stay authoritative).
            "created_at_text": format_local_datetime(self.created_at),
            "last_accessed_at_text": format_local_datetime(self.last_accessed_at),
            # ---- v2.12: 记忆分类 / 事件时间 / 版本替代状态 ----
            "memory_type": self.memory_type,
            "memory_type_label": MEMORY_TYPE_LABELS.get(self.memory_type, "短期情节"),
            "event_at": int(self.event_at or 0),
            "event_at_text": format_local_datetime(self.event_at) if self.event_at else "",
            "effective_event_at": self.effective_event_at,
            "has_embedding": bool(self.embedding),
            "superseded_by": self.superseded_by,
            "superseded": self.is_superseded,
            "last_reinforced_at": int(self.last_reinforced_at or 0),
            "last_reinforced_at_text": (
                format_local_datetime(self.last_reinforced_at)
                if self.last_reinforced_at
                else ""
            ),
            # v2.12: T1 countdown is anchored to tier1_since (reset on downgrade).
            "tier1_since": int(self.tier1_since or 0),
            "tier1_since_text": (
                format_local_datetime(self.tier1_since) if self.tier1_since else ""
            ),
            "tier1_expires_at": self.tier1_expires_at,
            "tier1_remaining_seconds": (
                max(0, self.tier1_expires_at - int(time.time()))
                if self.tier1_expires_at
                else 0
            ),
            "tier1_expired": bool(
                self.tier1_expires_at and self.tier1_expires_at <= int(time.time())
            ),
        }


class MemoryManager:
    """
    精简版分级记忆与定时遗忘管理器 (docs/ARCHITECTURE.md Section 6.1):
    - 单张轻量 SQLite `memories` 表
    - 自动按重要度划分为 Tier 1 (EPHEMERAL) / Tier 2 (RECENT) / Tier 3 (CORE)
    - 每日 `run_forgetting_gc()` 物理清理过期或活力归零记忆，并自动晋升高频召回记忆
    """

    def __init__(
        self,
        db: DatabaseManager,
        model_hub: ModelHub,
        plugin_config: dict[str, Any],
    ):
        self.db = db
        self.model_hub = model_hub
        self.plugin_config = plugin_config
        # v2.12: optional per-user identity resolver (aliases / canonical name) used
        # to build the 【身份归一表】 injected into the extraction prompt.
        self.user_relation: Any = None
        # v2.12: extraction failure flag read by SessionBuffer to keep ONE retry batch.
        self.last_extraction_failed: bool = False
        # v2.12: background backfill of legacy memories recalled without a vector.
        self._backfill_tasks: set = set()

    def attach_user_relation(self, user_relation: Any) -> None:
        """Attach the :class:`UserRelationManager` used for identity normalisation."""
        self.user_relation = user_relation

    @property
    def cfg(self) -> dict[str, Any]:
        return self.plugin_config.get("memory_and_context_config", {}) or {}

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enable_tiered_memory", True))

    @staticmethod
    def resolve_tier_by_importance(importance: int, user_tier: int = 1) -> int:
        """
        Enforce automatic tier grading rules (docs/ARCHITECTURE.md Section 6.1):
        - importance >= 8 (and L2/L3 user) -> Tier 3 (CORE)
        - 5 <= importance <= 7 -> Tier 2 (RECENT)
        - importance <= 4 -> Tier 1 (EPHEMERAL)
        """
        if importance >= 8 and user_tier >= 2:
            return MemoryTier.CORE
        if importance >= 5:
            return MemoryTier.RECENT
        return MemoryTier.EPHEMERAL

    async def add_memory(
        self,
        soul_id: str,
        content: str,
        keywords: str = "",
        importance: int = 5,
        user_id: Optional[str] = None,
        user_tier: int = 1,
        memory_tier: Optional[int] = None,
        vitality: float = 100.0,
        created_at: Optional[int] = None,
        memory_type: Optional[str] = None,
        event_at: Optional[int] = None,
        embedding: Any = None,
        supersedes_id: Optional[int] = None,
    ) -> int:
        """Insert a memory item (truncated to <=60 chars per design spec).

        v2.8.2: honours the global ``enable_tiered_memory`` switch — when tiered
        memory is disabled this is a no-op returning ``0`` (all other write paths
        such as command / Web API add, BUSY_TASK completion and extraction already
        go through this method, so the switch is now enforced consistently).

        v2.12 (write path, contract §2.3):
        1. compute (or reuse) the content embedding and persist it locally;
        2. **semantic dedupe for every tier** — cosine ``>= 0.85`` (or bigram
           Jaccard ``>= 0.65`` when no vector is available) refreshes the existing
           row's timestamp/keywords instead of inserting a duplicate;
        3. **supersede** — an explicit ``supersedes_id`` (or a same-``state``
           memory with cosine in ``[0.72, 0.85)``) archives the old row by
           pointing ``superseded_by`` at the new row;
        4. ``memory_type`` / ``event_at`` are stored so the read path can rank by
           the real event time and only promote ``profile`` / ``relationship``
           memories to Tier 3.
        """
        if not self.enabled:
            return 0
        clean_content = (content or "").strip()[:60]
        if not clean_content:
            return 0

        imp = max(1, min(10, int(importance)))
        utier = max(1, min(3, int(user_tier)))
        mtype = normalize_memory_type(memory_type)
        mtier = (
            int(memory_tier)
            if memory_tier in (1, 2, 3)
            else self.resolve_tier_by_importance(imp, utier)
        )
        # Enforce rule: L1 normal user cannot directly create Tier 3 CORE memory.
        if mtier == MemoryTier.CORE and utier == 1:
            mtier = MemoryTier.RECENT

        now_ts = int(created_at if created_at is not None else time.time())
        event_ts = int(event_at) if event_at else 0
        kw_str = (keywords or "").strip()

        # ---- 1) embedding (best effort; failure degrades to lexical similarity) ----
        vector = _load_vector(embedding) if embedding is not None else []
        if not vector:
            vector = await self._embed_content(clean_content, soul_id=soul_id)

        # ---- 2/3) 语义去重 + 版本替代裁决 ----
        duplicate_id, supersede_target = await self._resolve_write_decision(
            soul_id=soul_id,
            user_id=user_id,
            content=clean_content,
            keywords=kw_str,
            vector=vector,
            memory_type=mtype,
            supersedes_id=supersedes_id,
            now_ts=now_ts,
        )
        if duplicate_id:
            existing_row = await self.db.fetchone(
                "SELECT keywords FROM memories WHERE id = ?", (int(duplicate_id),)
            )
            merged_keywords = self._merge_keywords(
                (existing_row or {}).get("keywords"), kw_str
            )
            await self.db.execute(
                """
                UPDATE memories
                SET last_accessed_at = ?,
                    keywords = ?,
                    importance = MAX(importance, ?)
                WHERE id = ?
                """,
                (now_ts, merged_keywords, imp, duplicate_id),
            )
            logger.debug(
                "[OnlyYou] Duplicate memory skipped (refreshed #%s) for soul=%s",
                duplicate_id,
                soul_id,
            )
            return duplicate_id

        row_id = await self.db.execute(
            """
            INSERT INTO memories (
                soul_id, user_id, user_tier, memory_tier, content, keywords,
                importance, vitality, access_count, created_at, last_accessed_at,
                memory_type, event_at, embedding_json, superseded_by,
                last_reinforced_at, tier1_since
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, NULL, 0, ?)
            """,
            (
                soul_id,
                user_id,
                utier,
                mtier,
                clean_content,
                kw_str,
                imp,
                float(vitality),
                now_ts,
                now_ts,
                mtype,
                event_ts,
                _dump_vector(vector),
                now_ts if mtier == MemoryTier.EPHEMERAL else 0,
            ),
        )
        if supersede_target and row_id > 0:
            await self.mark_superseded(supersede_target, row_id)
        # v2.8 §5.4: FIFO cap for Tier 1 rows (oldest by created_at / id first).
        if mtier == MemoryTier.EPHEMERAL and row_id > 0:
            await self._enforce_tier1_cap(soul_id)
        return row_id

    @staticmethod
    def _merge_keywords(existing: Any, incoming: Any) -> str:
        """Union two comma-separated keyword strings (existing order first)."""
        merged: list[str] = []
        for source in (existing, incoming):
            for keyword in str(source or "").split(","):
                text = keyword.strip()
                if text and text not in merged:
                    merged.append(text)
        return ",".join(merged)

    async def _embed_content(self, content: str, soul_id: str = "") -> list[float]:
        """Embed one memory content via the hub (``[]`` when RAG is disabled)."""
        hub = self.model_hub
        getter = getattr(hub, "get_embedding", None) if hub is not None else None
        if not callable(getter):
            return []
        try:
            vector = getter(content, soul_id=soul_id) if soul_id else getter(content)
            if inspect.isawaitable(vector):
                vector = await vector
        except Exception as exc:  # noqa: BLE001 - degrade to lexical similarity
            logger.debug(f"[OnlyYou] memory embedding failed: {exc}")
            return []
        return _load_vector(vector) if vector else []

    async def _load_dedupe_pool(
        self, soul_id: str, user_id: Optional[str], limit: int = 400
    ) -> list[MemoryItem]:
        """Active memories of one soul that may duplicate / supersede a new row."""
        uid = str(user_id or "").strip()
        if uid:
            rows = await self.db.fetchall(
                """
                SELECT * FROM memories
                WHERE soul_id = ? AND superseded_by IS NULL
                  AND (user_id = ? OR user_id IS NULL OR user_id = '')
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (soul_id, uid, int(limit)),
            )
        else:
            # A public memory is only compared with other public memories: a private
            # row of another user must never swallow (or be "refreshed" by) it.
            rows = await self.db.fetchall(
                """
                SELECT * FROM memories
                WHERE soul_id = ? AND superseded_by IS NULL
                  AND (user_id IS NULL OR user_id = '')
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (soul_id, int(limit)),
            )
        return [MemoryItem.from_row(row) for row in rows]

    async def _resolve_write_decision(
        self,
        *,
        soul_id: str,
        user_id: Optional[str],
        content: str,
        keywords: str,
        vector: list[float],
        memory_type: str,
        supersedes_id: Optional[int],
        now_ts: int,
    ) -> tuple[Optional[int], Optional[int]]:
        """Return ``(duplicate_id, supersede_target_id)`` for a new memory row.

        ``duplicate_id`` means the content is already stored semantics-wise and the
        caller must refresh the existing row. ``supersede_target_id`` means a new
        row should be inserted and the old one archived afterwards.
        """
        try:
            pool = await self._load_dedupe_pool(soul_id, user_id)
        except Exception as exc:  # noqa: BLE001 - dedupe is best effort
            logger.debug(f"[OnlyYou] load dedupe pool failed: {exc}")
            return None, None

        explicit_target: Optional[int] = None
        if supersedes_id:
            try:
                explicit_target = int(supersedes_id)
            except (TypeError, ValueError):
                explicit_target = None
            if explicit_target and explicit_target not in {item.id for item in pool}:
                explicit_target = None

        best: Optional[MemoryItem] = None
        best_score = 0.0
        best_is_vector = False
        for item in pool:
            if vector and item.embedding:
                score = cosine_similarity(vector, item.embedding)
                is_vector = True
            else:
                score = jaccard_similarity(content, f"{item.content} {item.keywords}")
                is_vector = False
            if score > best_score:
                best = item
                best_score = score
                best_is_vector = is_vector

        if best is None:
            return None, explicit_target

        threshold = (
            DEDUPE_COSINE_THRESHOLD if best_is_vector else DEDUPE_JACCARD_THRESHOLD
        )
        if best_score >= threshold:
            return best.id, None

        if explicit_target:
            return None, explicit_target
        # 同类型 state 语义接近 -> 新状态取代旧状态（归档旧行，保留备查）。
        if (
            memory_type == "state"
            and best.memory_type == "state"
            and best_is_vector
            and best_score >= SUPERSEDE_STATE_COSINE_MIN
        ):
            return None, best.id
        if (
            memory_type == "state"
            and best.memory_type == "state"
            and not best_is_vector
            and best_score >= SUPERSEDE_STATE_JACCARD_MIN
        ):
            return None, best.id
        _ = now_ts
        return None, None

    async def mark_superseded(self, old_id: int, new_id: int) -> bool:
        """Archive ``old_id`` by pointing ``superseded_by`` at ``new_id``."""
        try:
            old_value = int(old_id)
            new_value = int(new_id)
        except (TypeError, ValueError):
            return False
        if old_value <= 0 or new_value <= 0 or old_value == new_value:
            return False
        try:
            await self.db.execute(
                "UPDATE memories SET superseded_by = ? WHERE id = ? AND superseded_by IS NULL",
                (new_value, old_value),
            )
        except Exception as exc:  # noqa: BLE001 - supersede must not break the write path
            logger.warning(f"[OnlyYou] mark_superseded failed: {exc}")
            return False
        return True

    async def retrieve_candidates(
        self,
        soul_id: str,
        user_id: Optional[str] = None,
        limit: int = 40,
        include_superseded: bool = False,
    ) -> list[MemoryItem]:
        """
        Retrieve candidate memories for `soul_id` that belong to `user_id` or are shared/public
        (`user_id IS NULL`), ordered by tier, importance, and recency.

        v2.12: archived rows (``superseded_by IS NOT NULL``) are excluded by default.
        """
        if not self.enabled:
            return []

        archived_clause = "" if include_superseded else "AND superseded_by IS NULL"
        if user_id:
            rows = await self.db.fetchall(
                f"""
                SELECT * FROM memories
                WHERE soul_id = ? AND (user_id = ? OR user_id IS NULL OR user_id = '')
                  {archived_clause}
                ORDER BY memory_tier DESC, importance DESC, created_at DESC
                LIMIT ?
                """,
                (soul_id, user_id, limit),
            )
        else:
            rows = await self.db.fetchall(
                f"""
                SELECT * FROM memories
                WHERE soul_id = ? {archived_clause}
                ORDER BY memory_tier DESC, importance DESC, created_at DESC
                LIMIT ?
                """,
                (soul_id, limit),
            )
        return [MemoryItem.from_row(r) for r in rows]

    async def retrieve_hybrid_candidates(
        self,
        soul_id: str,
        user_id: Optional[str] = None,
        query_text: str = "",
        query_embedding: Any = None,
        limit: int = 24,
        pool_limit: int = 400,
    ) -> list[MemoryItem]:
        """v2.12 三路混合粗排候选池（Dense / Sparse / Tier 分桶保底）。

        * only **active** memories (``superseded_by IS NULL``) are considered;
        * dense = query embedding vs. stored embedding cosine;
        * sparse = BM25 over ``content + keywords``;
        * each tier gets a guaranteed bucket quota so a pile of T3 memories can
          never squeeze relevant T1 rows out of the pool (contract §2.4).
        """
        if not self.enabled:
            return []
        try:
            desired = max(1, int(limit))
        except (TypeError, ValueError):
            desired = 24
        uid = str(user_id or "").strip()
        if uid:
            rows = await self.db.fetchall(
                """
                SELECT * FROM memories
                WHERE soul_id = ? AND superseded_by IS NULL
                  AND (user_id = ? OR user_id IS NULL OR user_id = '')
                ORDER BY COALESCE(NULLIF(event_at, 0), created_at) DESC, id DESC
                LIMIT ?
                """,
                (soul_id, uid, int(max(desired, pool_limit))),
            )
        else:
            rows = await self.db.fetchall(
                """
                SELECT * FROM memories
                WHERE soul_id = ? AND superseded_by IS NULL
                ORDER BY COALESCE(NULLIF(event_at, 0), created_at) DESC, id DESC
                LIMIT ?
                """,
                (soul_id, int(max(desired, pool_limit))),
            )
        items = [MemoryItem.from_row(row) for row in rows]
        if not items:
            return []

        query_vector = _load_vector(query_embedding) if query_embedding else []
        documents = [f"{item.content} {item.keywords}" for item in items]
        bm25 = bm25_scores(query_text, documents)
        max_bm25 = max(bm25) if bm25 else 0.0

        scored: list[tuple[float, MemoryItem]] = []
        for index, item in enumerate(items):
            sparse = (bm25[index] / max_bm25) if max_bm25 > 0 else 0.0
            if query_vector and item.embedding:
                dense = cosine_similarity(query_vector, item.embedding)
                coarse = 0.6 * dense + 0.4 * sparse
            else:
                dense = 0.0
                coarse = sparse
            # 重要度 / 新鲜度只作为极小的平手加权，避免无关记忆靠重要性挤进池子。
            importance_hint = max(1, min(10, int(item.importance))) / 100.0
            scored.append((coarse + importance_hint, item))
        scored.sort(key=lambda pair: pair[0], reverse=True)

        by_tier: dict[int, list[MemoryItem]] = {1: [], 2: [], 3: []}
        for _score, item in scored:
            tier = int(item.memory_tier or 2)
            by_tier.setdefault(tier, []).append(item)

        selected: list[MemoryItem] = []
        selected_ids: set[int] = set()
        for tier in (3, 2, 1):
            quota = max(1, int(round(desired * CANDIDATE_BUCKET_RATIOS.get(tier, 0.2))))
            for item in by_tier.get(tier, [])[:quota]:
                if item.id in selected_ids:
                    continue
                selected.append(item)
                selected_ids.add(item.id)
        if len(selected) < desired:
            for _score, item in scored:
                if len(selected) >= desired:
                    break
                if item.id in selected_ids:
                    continue
                selected.append(item)
                selected_ids.add(item.id)
        return selected[:desired]

    # ------------------------------------------------------------------
    # v2.8 §5.3 / §5.4: MEMORY_MUSE bucket rotation, T1 cap & real-time cleanup
    # ------------------------------------------------------------------

    def _tier1_limit(self) -> int:
        """Configured ``memory_and_context_config.max_tier1_memories`` (default 30)."""
        raw = self.cfg.get("max_tier1_memories", 30)
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return 30

    async def _enforce_tier1_cap(self, soul_id: str) -> int:
        """FIFO-trim one soul's Tier 1 rows down to ``max_tier1_memories``.

        Oldest rows (``created_at ASC, id ASC``) are deleted first. Returns the
        number of deleted rows. Tier 2 / Tier 3 rows are never touched.
        """
        limit = self._tier1_limit()
        rows = await self.db.fetchall(
            """
            SELECT id FROM memories
            WHERE soul_id = ? AND memory_tier = 1
            ORDER BY COALESCE(NULLIF(tier1_since, 0), created_at) ASC, id ASC
            """,
            (soul_id,),
        )
        overflow = len(rows) - limit
        if overflow <= 0:
            return 0
        for row in rows[:overflow]:
            await self.db.execute("DELETE FROM memories WHERE id = ?", (int(row["id"]),))
        return overflow

    @staticmethod
    def _muse_weight(item: MemoryItem) -> float:
        """Weight for the MEMORY_MUSE weighted draw: importance x tier bias."""
        try:
            importance = max(1, int(item.importance))
        except (TypeError, ValueError):
            importance = 1
        tier_weight = {1: 1.0, 2: 1.5, 3: 2.0}.get(int(item.memory_tier or 1), 1.0)
        return float(importance) * tier_weight

    async def sample_muse_memories(
        self,
        soul_id: str,
        exclude_ids: Optional[list[int]] = None,
        per_bucket_limit: int = 15,
        pick: int = 1,
        max_age_seconds: Optional[int] = None,
    ) -> list[MemoryItem]:
        """Sample 1~``pick`` memories for an IDLE_ROAM MEMORY_MUSE prompt (v2.8 §5.3).

        Tier 1 / Tier 2 / Tier 3 are queried **independently** (oldest-accessed
        first, ``per_bucket_limit`` each), ``exclude_ids`` are filtered out, then a
        weighted draw biased by importance / tier picks at most ``pick`` items.

        v2.12: archived (``superseded_by``) rows never surface again; the default
        draw is a **single** memory (no forced 2-memory collage) and an optional
        ``max_age_seconds`` keeps the muse to recent material (72h in production).
        """
        if not self.enabled:
            return []
        try:
            bucket_limit = max(1, int(per_bucket_limit))
        except (TypeError, ValueError):
            bucket_limit = 15
        try:
            pick_count = max(0, int(pick))
        except (TypeError, ValueError):
            pick_count = 1
        if pick_count <= 0:
            return []

        excluded: set[int] = set()
        for value in exclude_ids or []:
            try:
                excluded.add(int(value))
            except (TypeError, ValueError):
                continue

        age_clause = ""
        age_params: tuple = ()
        if max_age_seconds is not None:
            try:
                age_limit = max(60, int(max_age_seconds))
            except (TypeError, ValueError):
                age_limit = 0
            if age_limit:
                age_clause = (
                    " AND COALESCE(NULLIF(event_at, 0), created_at) >= ?"
                )
                age_params = (int(time.time()) - age_limit,)

        pool: list[MemoryItem] = []
        for tier in (MemoryTier.EPHEMERAL, MemoryTier.RECENT, MemoryTier.CORE):
            rows = await self.db.fetchall(
                f"""
                SELECT * FROM memories
                WHERE soul_id = ? AND memory_tier = ? AND superseded_by IS NULL
                  {age_clause}
                ORDER BY COALESCE(last_accessed_at, 0) ASC, id ASC
                LIMIT ?
                """,
                (soul_id, int(tier)) + age_params + (bucket_limit,),
            )
            for row in rows:
                item = MemoryItem.from_row(row)
                if item.id not in excluded:
                    pool.append(item)
        if not pool:
            return []

        sampled: list[MemoryItem] = []
        remaining = list(pool)
        for _ in range(min(pick_count, len(remaining))):
            weights = [self._muse_weight(item) for item in remaining]
            chosen = random.choices(remaining, weights=weights, k=1)[0]
            sampled.append(chosen)
            remaining.remove(chosen)
        return sampled

    async def touch_memories_for_muse(self, memory_ids: list[int]) -> None:
        """Refresh ``last_accessed_at`` only — no access_count / vitality / tier change."""
        if not memory_ids:
            return
        now = int(time.time())
        seen: set[int] = set()
        for value in memory_ids:
            try:
                memory_id = int(value)
            except (TypeError, ValueError):
                continue
            if memory_id in seen:
                continue
            seen.add(memory_id)
            await self.db.execute(
                "UPDATE memories SET last_accessed_at = ? WHERE id = ?",
                (now, memory_id),
            )

    async def cleanup_tier1(self, now_override: Optional[int] = None) -> dict[str, int]:
        """Real-time Tier 1 cleanup (v2.8 §5.4).

        Deletes Tier 1 rows past ``tier1_since + 48h`` (``deleted_expired``) and FIFO-trims
        each soul's Tier 1 rows above ``max_tier1_memories`` (``deleted_overflow``).
        """
        now = int(now_override if now_override is not None else time.time())
        stats = {"deleted_expired": 0, "deleted_overflow": 0}

        expired = await self.db.fetchall(
            "SELECT id FROM memories WHERE memory_tier = 1 AND "
            "(? - COALESCE(NULLIF(tier1_since, 0), created_at)) > ?",
            (now, TIER1_TTL_SECONDS),
        )
        if expired:
            stats["deleted_expired"] = len(expired)
            await self.db.execute(
                "DELETE FROM memories WHERE memory_tier = 1 AND "
                "(? - COALESCE(NULLIF(tier1_since, 0), created_at)) > ?",
                (now, TIER1_TTL_SECONDS),
            )

        souls = await self.db.fetchall(
            "SELECT DISTINCT soul_id FROM memories WHERE memory_tier = 1"
        )
        for row in souls:
            stats["deleted_overflow"] += await self._enforce_tier1_cap(
                str(row["soul_id"])
            )
        if stats["deleted_expired"] or stats["deleted_overflow"]:
            logger.info(f"[OnlyYou] Tier1 cleanup completed: {stats}")
        return stats

    async def reinforce_memories(
        self,
        memory_ids: list[int],
        sem_scores: Optional[dict[int, float]] = None,
        now_override: Optional[int] = None,
    ) -> int:
        """True-Hit Reinforcement (v2.12 contract §2.4).

        Only memories that were **really relevant** are reinforced:

        * ``sem_scores`` maps ``memory_id -> semantic relevance``; a memory below
          :data:`REINFORCE_MIN_SEMANTIC_SCORE` (0.55) never counts as a recall;
        * a memory already reinforced less than
          :data:`REINFORCE_COOLDOWN_SECONDS` (6h) ago is skipped (cross-session
          repetition is what should promote a memory, not the same burst);
        * Tier 2 -> Tier 3 promotion is now a **hard gate**: ``memory_type`` must
          be ``profile`` / ``relationship``, ``importance >= 8``, at least
          :data:`PROMOTE_TO_T3_MIN_ACCESS` effective hits and the memory must be
          at least one day old. Everyday ``episode`` / ``state`` rows stay T2.

        Returns the number of reinforced rows.
        """
        if not memory_ids:
            return 0
        now = int(now_override if now_override is not None else time.time())
        scores = sem_scores if isinstance(sem_scores, dict) else {}
        reinforced = 0
        pending: list[tuple[int, str, str]] = []
        for raw_id in memory_ids:
            try:
                mid = int(raw_id)
            except (TypeError, ValueError):
                continue
            row = await self.db.fetchone(
                """
                SELECT id, soul_id, content, embedding_json, memory_tier,
                       memory_type, importance, access_count, created_at,
                       last_reinforced_at
                FROM memories WHERE id = ?
                """,
                (mid,),
            )
            if not row:
                continue
            score = scores.get(mid)
            if score is not None:
                try:
                    if float(score) < REINFORCE_MIN_SEMANTIC_SCORE:
                        continue
                except (TypeError, ValueError):
                    continue
            last_reinforced = int(row.get("last_reinforced_at") or 0)
            if last_reinforced and (now - last_reinforced) < REINFORCE_COOLDOWN_SECONDS:
                continue

            access_count = int(row.get("access_count") or 0) + 1
            tier = int(row.get("memory_tier") or 1)
            memory_type = normalize_memory_type(row.get("memory_type"))
            importance = int(row.get("importance") or 5)
            created_at = int(row.get("created_at") or 0)
            promote = (
                tier == MemoryTier.RECENT
                and memory_type in PROMOTABLE_MEMORY_TYPES
                and importance >= PROMOTE_TO_T3_MIN_IMPORTANCE
                and access_count >= PROMOTE_TO_T3_MIN_ACCESS
                and created_at > 0
                and (now - created_at) >= PROMOTE_TO_T3_MIN_AGE_SECONDS
            )
            await self.db.execute(
                """
                UPDATE memories
                SET access_count = ?,
                    vitality = MIN(100.0, vitality + 8.0),
                    last_accessed_at = ?,
                    last_reinforced_at = ?,
                    memory_tier = ?
                WHERE id = ?
                """,
                (
                    access_count,
                    now,
                    now,
                    MemoryTier.CORE if promote else tier,
                    mid,
                ),
            )
            reinforced += 1
            # Legacy rows recalled without a vector: backfill lazily (never blocks
            # the reply) so the next recall can use dense similarity.
            if not row.get("embedding_json"):
                pending.append(
                    (mid, str(row.get("content") or ""), str(row.get("soul_id") or ""))
                )
        if pending:
            try:
                loop = asyncio.get_running_loop()
                task = loop.create_task(self._backfill_embeddings(pending))
                self._backfill_tasks.add(task)
                task.add_done_callback(self._backfill_tasks.discard)
            except RuntimeError:
                # No running loop (sync unit context): best-effort inline is skipped.
                pass
        return reinforced

    async def _backfill_embeddings(
        self, items: list[tuple[int, str, str]]
    ) -> None:
        """Best-effort embedding backfill for legacy memories (v2.12)."""
        for memory_id, content, soul_id in items:
            if not content:
                continue
            vector = await self._embed_content(content, soul_id=soul_id)
            if not vector:
                continue
            try:
                await self.db.execute(
                    "UPDATE memories SET embedding_json = ? "
                    "WHERE id = ? AND (embedding_json IS NULL OR embedding_json = '')",
                    (_dump_vector(vector), int(memory_id)),
                )
            except Exception as exc:  # noqa: BLE001 - backfill is best effort
                logger.debug(f"[OnlyYou] backfill embedding #{memory_id} failed: {exc}")

    async def run_forgetting_gc(self, now_override: Optional[int] = None) -> dict[str, int]:
        """
        每日定时遗忘清理程序 (exact implementation from docs/ARCHITECTURE.md Section 6.1):
        1. 清理超过 48 小时 (172800s，含降级重算) 的 Tier 1 (瞬时短时记忆)
        2. 结算所有 Tier 2 (近期记忆) 的每日活力衰减:
           - 用户等级保护系数: L1=0.6, L2=1.2, L3=2.0
           - 访问强化系数: 1.0 + 0.5 * ln(1 + access_count)
           - daily_decay = 35.0 / (max(1, importance) * tier_factor * reinforce_factor)
           - new_vitality <= 0 -> 物理删除
           - v2.12 晋升门闸: 仅 profile / relationship 且 importance >= 8 且
             access_count >= 8 才晋升 Tier 3（episode / state 留在 T2 自然代谢）
        """
        now = int(now_override if now_override is not None else time.time())
        stats = {"deleted_tier1": 0, "deleted_tier2": 0, "promoted_to_tier3": 0, "decayed_tier2": 0}

        # Count & delete expired Tier 1 (> 48 hours)
        expired_t1 = await self.db.fetchall(
            "SELECT id FROM memories WHERE memory_tier = 1 AND "
            "(? - COALESCE(NULLIF(tier1_since, 0), created_at)) > ?",
            (now, TIER1_TTL_SECONDS),
        )
        stats["deleted_tier1"] = len(expired_t1)
        if expired_t1:
            await self.db.execute(
                "DELETE FROM memories WHERE memory_tier = 1 AND "
                "(? - COALESCE(NULLIF(tier1_since, 0), created_at)) > ?",
                (now, TIER1_TTL_SECONDS),
            )

        # Evaluate Tier 2 decay & promotion
        rows = await self.db.fetchall(
            "SELECT id, importance, user_tier, vitality, access_count, memory_type, "
            "created_at FROM memories WHERE memory_tier = 2"
        )
        for row in rows:
            tier_factor = {1: 0.6, 2: 1.2, 3: 2.0}.get(int(row["user_tier"]), 1.0)
            reinforce_factor = 1.0 + 0.5 * math.log(1 + int(row["access_count"]))

            daily_decay = 35.0 / (max(1, int(row["importance"])) * tier_factor * reinforce_factor)
            new_vitality = float(row["vitality"]) - daily_decay

            promotable = (
                normalize_memory_type(row.get("memory_type")) in PROMOTABLE_MEMORY_TYPES
                and int(row["importance"]) >= PROMOTE_TO_T3_MIN_IMPORTANCE
                and int(row["access_count"]) >= PROMOTE_TO_T3_MIN_ACCESS
                and int(row.get("created_at") or 0) > 0
                and (now - int(row.get("created_at") or 0))
                >= PROMOTE_TO_T3_MIN_AGE_SECONDS
            )
            if new_vitality <= 0:
                await self.db.execute("DELETE FROM memories WHERE id = ?", (row["id"],))
                stats["deleted_tier2"] += 1
            elif promotable:
                await self.db.execute(
                    "UPDATE memories SET memory_tier = 3, vitality = 100.0 WHERE id = ?",
                    (row["id"],),
                )
                stats["promoted_to_tier3"] += 1
            else:
                await self.db.execute(
                    "UPDATE memories SET vitality = ? WHERE id = ?",
                    (round(new_vitality, 2), row["id"]),
                )
                stats["decayed_tier2"] += 1

        logger.info(f"[OnlyYou] Forgetting GC completed: {stats}")
        return stats

    async def _build_identity_block(
        self, transcript_text: str, soul_id: str = ""
    ) -> str:
        """Build the 【参与者身份归一表】 from the transcript's ``(ID:...)`` stamps.

        v2.12 (contract §3.1): the extraction model must know that several display
        names / platform accounts belong to the same person, otherwise it splits one
        user into several memory subjects. Every lookup is best effort.
        """
        if self.user_relation is None:
            return ""
        getter = getattr(self.user_relation, "get_user", None)
        if not callable(getter):
            return ""
        uids: list[str] = []
        for match in re.finditer(r"\(ID:\s*([^)]+)\)", transcript_text or ""):
            uid = str(match.group(1) or "").strip()
            if uid and uid not in uids:
                uids.append(uid)
            if len(uids) >= 5:
                break
        lines: list[str] = []
        for uid in uids:
            try:
                # Prefer the per-Soul relation so the character's nickname (the most
                # stable "canonical name") is available; fall back to the identity row.
                profile = (
                    await getter(uid, soul_id=soul_id) if soul_id else await getter(uid)
                )
                if profile is None and soul_id:
                    # No relation row for this Soul yet -> fall back to the identity.
                    profile = await getter(uid)
            except TypeError:
                try:
                    profile = await getter(uid)
                except Exception:  # noqa: BLE001
                    continue
            except Exception:  # noqa: BLE001 - identity block is best effort
                continue
            if profile is None:
                continue
            canonical = (
                str(getattr(profile, "canonical_name", "") or "").strip()
                or str(getattr(profile, "display_name", "") or "").strip()
                or uid
            )
            aliases = [
                str(alias or "").strip()
                for alias in (getattr(profile, "aliases", None) or [])
                if str(alias or "").strip()
            ]
            platforms = [
                str(account or "").strip()
                for account in (getattr(profile, "platform_ids", None) or [])
                if str(account or "").strip()
            ]
            lines.append(
                f"- 唯一主 ID：{uid} | 标准称呼：{canonical}"
                f" | 曾用名/多端昵称：{'、'.join(aliases) or '无'}"
                f" | 绑定账号：{'、'.join(platforms) or '无'}"
            )
        if not lines:
            return ""
        return USER_IDENTITY_NORMALIZATION_PROMPT.format(lines="\n".join(lines))

    @staticmethod
    def _transcript_participant_ids(transcript_text: str) -> list[str]:
        """Distinct ``(ID:...)`` participants of a transcript (max 8)."""
        uids: list[str] = []
        for match in re.finditer(r"\(ID:\s*([^)]+)\)", transcript_text or ""):
            uid = str(match.group(1) or "").strip()
            if uid and uid not in uids:
                uids.append(uid)
            if len(uids) >= 8:
                break
        return uids

    async def _render_existing_extraction_memories(
        self, soul_id: str, participant_ids: Optional[list[str]] = None
    ) -> str:
        """Render existing T2/T3 memories of this conversation's participants."""
        try:
            candidates = await self.retrieve_candidates(soul_id=soul_id, limit=120)
        except Exception as exc:  # noqa: BLE001 - prompt enrichment is best effort
            logger.debug(f"[OnlyYou] load extraction context failed: {exc}")
            return "（暂无既有长期记忆）"
        wanted = {str(uid).strip() for uid in (participant_ids or []) if str(uid).strip()}
        lines: list[str] = []
        for item in candidates:
            if int(item.memory_tier) < 2:
                continue
            if wanted and item.user_id and str(item.user_id) not in wanted:
                continue
            label = MEMORY_TYPE_LABELS.get(item.memory_type, "短期情节")
            lines.append(
                f"- [ID:{item.id} · {label} · T{item.memory_tier}] {item.content}"
            )
            if len(lines) >= 10:
                break
        return "\n".join(lines) if lines else "（暂无既有长期记忆）"

    @staticmethod
    def _parse_event_timestamp(item: dict[str, Any], fallback: int) -> int:
        """Resolve a memory's real event time from the LLM payload / transcript."""
        raw = item.get("event_at")
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            value = int(raw)
            # Tolerate millisecond timestamps emitted by some models.
            if value > 10_000_000_000:
                value //= 1000
            if value > 0:
                return value
        if isinstance(raw, str):
            text_raw = raw.strip()
            if text_raw.isdigit():
                value = int(text_raw)
                if value > 10_000_000_000:
                    value //= 1000
                return value
        text = str(
            item.get("event_time") or (raw if isinstance(raw, str) else "") or ""
        ).strip()
        if text:
            from datetime import datetime

            for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d %H:%M", "%Y/%m/%d"):
                try:
                    return int(datetime.strptime(text, fmt).timestamp())
                except ValueError:
                    continue
        return int(fallback)

    @staticmethod
    def _infer_transcript_event_times(
        transcript_text: str, fallback: int
    ) -> tuple[int, dict[str, int]]:
        """Infer the real event time of each transcript participant.

        Returns ``(latest_global_ts, {participant_uid: latest_ts})`` from the
        ``[YYYY-MM-DD HH:MM] ... (ID:uid)`` lines produced by
        ``SessionBuffer.format_extraction_transcript``. When a memory names a
        ``user_id`` its own latest message time becomes ``event_at``; otherwise the
        batch-wide latest time is used (never ``null`` / the extraction moment).
        """
        from datetime import datetime

        line_pattern = re.compile(
            r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\].*?\(ID:\s*([^)]+)\)"
        )
        latest_global = 0
        per_user: dict[str, int] = {}
        for match in line_pattern.finditer(transcript_text or ""):
            try:
                stamp = int(
                    datetime.strptime(match.group(1), "%Y-%m-%d %H:%M").timestamp()
                )
            except ValueError:
                continue
            latest_global = max(latest_global, stamp)
            uid = str(match.group(2) or "").strip()
            if uid:
                per_user[uid] = max(per_user.get(uid, 0), stamp)
        return (latest_global or int(fallback)), per_user

    async def extract_memories_from_transcript(
        self,
        soul_id: str,
        soul_name: str,
        transcript_text: str,
        umo: Optional[str] = None,
        now_ts: Optional[int] = None,
        identity_block: str = "",
        max_extract_per_batch: int = 3,
    ) -> int:
        """
        Invoke the decision model with `MEMORY_EXTRACT_AND_GRADE_PROMPT` on evicted sliding-window
        messages and store extracted memories into SQLite.

        v2.10: the prompt receives the **current local time** (weekday included); the
        transcript itself is expected to carry one ``[YYYY-MM-DD HH:MM]`` stamp per
        message (``SessionBuffer.format_extraction_transcript``). Memory content is
        stored verbatim (no forced date rewriting) — relative expressions are
        resolved later when the memory is injected with its formation-time anchor.
        All memories extracted in one batch share ``now_ts`` as their ``created_at``
        anchor so the injected time label is consistent.

        v2.12 (contract §2.3): the prompt receives the Soul's existing T2/T3 memories
        (so the model can answer "already known" / emit ``supersedes_id``) and an
        optional participant identity block; at most ``max_extract_per_batch`` (3)
        memories are stored per batch, each carrying ``memory_type`` and a real
        ``event_at``. Extraction may only *directly* produce T3 for
        ``profile`` / ``relationship`` memories with ``importance >= 9``.
        """
        self.last_extraction_failed = False
        if not self.enabled or not (transcript_text or "").strip():
            return 0

        anchor_ts = int(now_ts if now_ts is not None else time.time())
        participant_ids = self._transcript_participant_ids(transcript_text)
        existing_memories_text = await self._render_existing_extraction_memories(
            soul_id, participant_ids=participant_ids
        )
        identity_text = (identity_block or "").strip()
        if not identity_text:
            identity_text = await self._build_identity_block(
                transcript_text, soul_id=soul_id
            )
        prompt = MEMORY_EXTRACT_AND_GRADE_PROMPT.format(
            soul_name=soul_name,
            recent_chat_transcript=transcript_text,
            current_datetime=current_datetime_text(anchor_ts),
            identity_block=identity_text or "（无额外身份映射）",
            existing_memories_text=existing_memories_text,
        )
        kwargs: dict[str, Any] = {
            "prompt": prompt,
            "role": "decision",
            "umo": umo,
            "default": None,
        }
        try:
            items = await self.model_hub.call_llm_json(soul_id=soul_id, **kwargs)
        except TypeError:
            items = await self.model_hub.call_llm_json(**kwargs)
        if not isinstance(items, list):
            # ``None`` means the decision model / JSON parse failed; the caller keeps
            # this batch for one retry instead of dropping it forever.
            self.last_extraction_failed = True
            return 0

        event_ts, per_user_event_ts = self._infer_transcript_event_times(
            transcript_text, anchor_ts
        )
        try:
            limit = max(1, int(max_extract_per_batch))
        except (TypeError, ValueError):
            limit = 3

        inserted = 0
        for item in items:
            if inserted >= limit:
                break
            if not isinstance(item, dict):
                continue
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            user_id = item.get("user_id")
            if user_id in ("null", "None", ""):
                user_id = None
            try:
                user_tier = max(1, min(3, int(item.get("user_tier") or 1)))
            except (TypeError, ValueError):
                user_tier = 1
            try:
                importance = max(1, min(10, int(item.get("importance") or 5)))
            except (TypeError, ValueError):
                importance = 5
            memory_type = normalize_memory_type(item.get("memory_type"))
            keywords_raw = item.get("keywords")
            if isinstance(keywords_raw, (list, tuple)):
                keywords = ",".join(str(k).strip() for k in keywords_raw if str(k).strip())
            else:
                keywords = str(keywords_raw or "")

            raw_tier = item.get("memory_tier")
            mtier: Optional[int] = None
            try:
                if str(raw_tier).strip() in ("1", "2", "3"):
                    mtier = int(raw_tier)
            except (TypeError, ValueError):
                mtier = None
            if mtier is None:
                mtier = MemoryTier.RECENT if importance >= 5 else MemoryTier.EPHEMERAL
            # 严控直出 T3：仅长期人设 / 关系类且重要度 >= 9 才允许。
            if mtier == MemoryTier.CORE and not (
                memory_type in PROMOTABLE_MEMORY_TYPES and importance >= 9
            ):
                mtier = MemoryTier.RECENT

            supersedes_raw = item.get("supersedes_id")
            supersedes_id: Optional[int] = None
            try:
                if supersedes_raw not in (None, "", "null", "None"):
                    candidate = int(supersedes_raw)
                    if candidate > 0:
                        supersedes_id = candidate
            except (TypeError, ValueError):
                supersedes_id = None

            row_id = await self.add_memory(
                soul_id=soul_id,
                content=content[:50],
                keywords=keywords,
                importance=importance,
                user_id=str(user_id) if user_id else None,
                user_tier=user_tier,
                memory_tier=mtier,
                created_at=anchor_ts,
                memory_type=memory_type,
                event_at=self._parse_event_timestamp(
                    item,
                    per_user_event_ts.get(str(user_id), event_ts)
                    if user_id
                    else event_ts,
                ),
                supersedes_id=supersedes_id,
            )
            if row_id > 0:
                inserted += 1

        if inserted > 0:
            logger.info(
                f"[OnlyYou] Extracted and saved {inserted} tiered memories for soul={soul_id}"
            )
        return inserted

    async def list_memories(
        self,
        soul_id: Optional[str] = None,
        memory_tier: Optional[int] = None,
        user_id: Optional[str] = None,
        keyword: Optional[str] = None,
        limit: int = 100,
        memory_type: Optional[str] = None,
        include_superseded: bool = True,
    ) -> list[MemoryItem]:
        """List memories with optional filtering by `soul_id`, `memory_tier`, `user_id`, and `keyword`.

        v2.12: ``memory_type`` filters the four-value classification and
        ``include_superseded=False`` hides archived (``superseded_by``) rows. The
        console keeps the default ``True`` so archived rows stay inspectable.
        """
        clauses: list[str] = []
        params: list[Any] = []
        if soul_id:
            clauses.append("soul_id = ?")
            params.append(soul_id)
        if memory_tier in (1, 2, 3):
            clauses.append("memory_tier = ?")
            params.append(int(memory_tier))
        if user_id:
            clauses.append("user_id = ?")
            params.append(str(user_id))
        if memory_type:
            clauses.append("memory_type = ?")
            params.append(normalize_memory_type(memory_type))
        if not include_superseded:
            clauses.append("superseded_by IS NULL")
        if keyword:
            clauses.append("(content LIKE ? OR keywords LIKE ?)")
            kw_pat = f"%{keyword.strip()}%"
            params.extend([kw_pat, kw_pat])

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"""
            SELECT * FROM memories
            {where_sql}
            ORDER BY memory_tier DESC, importance DESC, vitality DESC, created_at DESC
            LIMIT ?
        """
        params.append(int(limit))
        rows = await self.db.fetchall(sql, tuple(params))
        return [MemoryItem.from_row(r) for r in rows]

    async def update_memory(
        self,
        memory_id: int,
        content: Optional[str] = None,
        keywords: Optional[str] = None,
        importance: Optional[int] = None,
        memory_tier: Optional[int] = None,
        vitality: Optional[float] = None,
        user_id: Optional[str] = None,
        memory_type: Optional[str] = None,
        event_at: Optional[int] = None,
        now_override: Optional[int] = None,
    ) -> Optional[MemoryItem]:
        """Update fields of an existing memory item."""
        row = await self.db.fetchone("SELECT * FROM memories WHERE id = ?", (int(memory_id),))
        if not row:
            return None
        item = MemoryItem.from_row(row)
        new_content = (content.strip()[:60] if content is not None else item.content) or item.content
        new_kw = keywords.strip() if keywords is not None else item.keywords
        new_imp = max(1, min(10, int(importance))) if importance is not None else item.importance
        new_tier = item.memory_tier
        if memory_tier is not None:
            try:
                candidate_tier = int(memory_tier)
            except (TypeError, ValueError):
                candidate_tier = item.memory_tier
            if candidate_tier in (1, 2, 3):
                new_tier = candidate_tier
        new_vit = max(0.0, min(100.0, float(vitality))) if vitality is not None else item.vitality
        new_uid = (user_id.strip() or None) if user_id is not None else item.user_id
        new_type = (
            normalize_memory_type(memory_type)
            if memory_type is not None
            else item.memory_type
        )
        try:
            new_event = int(event_at) if event_at is not None else int(item.event_at or 0)
        except (TypeError, ValueError):
            new_event = int(item.event_at or 0)
        # v2.12: entering T1 starts a fresh 48h retention window; leaving T1
        # clears it. Editing a row that is already T1 does not restart the clock.
        if new_tier == MemoryTier.EPHEMERAL:
            if item.memory_tier != MemoryTier.EPHEMERAL or not item.tier1_since:
                new_tier1_since = int(now_override if now_override is not None else time.time())
            else:
                new_tier1_since = int(item.tier1_since)
        else:
            new_tier1_since = 0

        embedding_json = _dump_vector(item.embedding)
        if new_content != item.content:
            # Keep the local vector in sync with an edited content (best effort).
            embedding_json = _dump_vector(
                await self._embed_content(new_content, soul_id=item.soul_id)
            )

        await self.db.execute(
            """
            UPDATE memories
            SET content = ?, keywords = ?, importance = ?, memory_tier = ?,
                vitality = ?, user_id = ?, memory_type = ?, event_at = ?,
                embedding_json = ?, tier1_since = ?
            WHERE id = ?
            """,
            (
                new_content,
                new_kw,
                new_imp,
                new_tier,
                new_vit,
                new_uid,
                new_type,
                new_event,
                embedding_json,
                new_tier1_since,
                int(memory_id),
            ),
        )
        updated_row = await self.db.fetchone("SELECT * FROM memories WHERE id = ?", (int(memory_id),))
        return MemoryItem.from_row(updated_row) if updated_row else None

    async def pin_to_core(self, memory_id: int) -> Optional[MemoryItem]:
        """One-click pin a Tier 1/2 memory into a permanent Tier 3 (CORE) memory."""
        row = await self.db.fetchone("SELECT * FROM memories WHERE id = ?", (int(memory_id),))
        if not row:
            return None
        new_imp = max(8, int(row.get("importance", 8)))
        await self.db.execute(
            "UPDATE memories SET memory_tier = 3, importance = ?, vitality = 100.0, "
            "tier1_since = 0 WHERE id = ?",
            (new_imp, int(memory_id)),
        )
        updated = await self.db.fetchone("SELECT * FROM memories WHERE id = ?", (int(memory_id),))
        return MemoryItem.from_row(updated) if updated else None

    async def delete_memory(self, memory_id: int) -> None:
        """Delete a single memory item by `id`."""
        await self.db.execute("DELETE FROM memories WHERE id = ?", (int(memory_id),))
