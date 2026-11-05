"""Tiered memory engine (Tier 1 Ephemeral / Tier 2 Recent / Tier 3 Core) + Forgetting GC."""

import math
import time
from dataclasses import dataclass
from typing import Any, Optional

from .model_hub import ModelHub
from ..prompts.templates import MEMORY_EXTRACT_AND_GRADE_PROMPT
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

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "MemoryItem":
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
        )

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
        }


class MemoryManager:
    """
    精简版分级记忆与定时遗忘管理器 (docs/ARCHITECTURE.md Section 4.1 & 4.2):
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

    @property
    def cfg(self) -> dict[str, Any]:
        return self.plugin_config.get("memory_and_context_config", {}) or {}

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enable_tiered_memory", True))

    @staticmethod
    def resolve_tier_by_importance(importance: int, user_tier: int = 1) -> int:
        """
        Enforce automatic tier grading rules (docs/ARCHITECTURE.md Section 4.1):
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
    ) -> int:
        """Insert a memory item (truncated to <=60 chars per design spec)."""
        clean_content = (content or "").strip()[:60]
        if not clean_content:
            return 0

        imp = max(1, min(10, int(importance)))
        utier = max(1, min(3, int(user_tier)))
        mtier = (
            int(memory_tier)
            if memory_tier in (1, 2, 3)
            else self.resolve_tier_by_importance(imp, utier)
        )
        # Enforce rule: L1 normal user cannot directly create Tier 3 CORE memory
        if mtier == MemoryTier.CORE and utier == 1:
            mtier = MemoryTier.RECENT

        now_ts = int(created_at if created_at is not None else time.time())
        kw_str = (keywords or "").strip()

        return await self.db.execute(
            """
            INSERT INTO memories (
                soul_id, user_id, user_tier, memory_tier, content, keywords,
                importance, vitality, access_count, created_at, last_accessed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
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
            ),
        )

    async def retrieve_candidates(
        self, soul_id: str, user_id: Optional[str] = None, limit: int = 40
    ) -> list[MemoryItem]:
        """
        Retrieve candidate memories for `soul_id` that belong to `user_id` or are shared/public
        (`user_id IS NULL`), ordered by tier, importance, and recency.
        """
        if not self.enabled:
            return []

        if user_id:
            rows = await self.db.fetchall(
                """
                SELECT * FROM memories
                WHERE soul_id = ? AND (user_id = ? OR user_id IS NULL OR user_id = '')
                ORDER BY memory_tier DESC, importance DESC, created_at DESC
                LIMIT ?
                """,
                (soul_id, user_id, limit),
            )
        else:
            rows = await self.db.fetchall(
                """
                SELECT * FROM memories
                WHERE soul_id = ?
                ORDER BY memory_tier DESC, importance DESC, created_at DESC
                LIMIT ?
                """,
                (soul_id, limit),
            )
        return [MemoryItem.from_row(r) for r in rows]

    async def reinforce_memories(self, memory_ids: list[int]) -> None:
        """
        Increment `access_count`, refresh `last_accessed_at`, and auto-promote Tier 2 -> Tier 3
        if `access_count >= 4` and `importance >= 6`.
        """
        if not memory_ids:
            return
        now = int(time.time())
        for mid in memory_ids:
            await self.db.execute(
                """
                UPDATE memories
                SET access_count = access_count + 1,
                    vitality = MIN(100.0, vitality + 8.0),
                    last_accessed_at = ?,
                    memory_tier = CASE
                        WHEN memory_tier = 2 AND (access_count + 1) >= 4 AND importance >= 6 THEN 3
                        ELSE memory_tier
                    END
                WHERE id = ?
                """,
                (now, mid),
            )

    async def run_forgetting_gc(self, now_override: Optional[int] = None) -> dict[str, int]:
        """
        每日定时遗忘清理程序 (exact implementation from docs/ARCHITECTURE.md Section 4.2):
        1. 清理超过 48 小时 (172800s) 的 Tier 1 (瞬时短时记忆)
        2. 结算所有 Tier 2 (近期记忆) 的每日活力衰减:
           - 用户等级保护系数: L1=0.6, L2=1.2, L3=2.0
           - 访问强化系数: 1.0 + 0.5 * ln(1 + access_count)
           - daily_decay = 35.0 / (max(1, importance) * tier_factor * reinforce_factor)
           - new_vitality <= 0 -> 物理删除
           - access_count >= 4 and importance >= 6 -> 自动晋升为 Tier 3 (核心永久记忆)
        """
        now = int(now_override if now_override is not None else time.time())
        stats = {"deleted_tier1": 0, "deleted_tier2": 0, "promoted_to_tier3": 0, "decayed_tier2": 0}

        # Count & delete expired Tier 1 (> 48 hours)
        expired_t1 = await self.db.fetchall(
            "SELECT id FROM memories WHERE memory_tier = 1 AND (? - created_at) > 172800",
            (now,),
        )
        stats["deleted_tier1"] = len(expired_t1)
        if expired_t1:
            await self.db.execute(
                "DELETE FROM memories WHERE memory_tier = 1 AND (? - created_at) > 172800",
                (now,),
            )

        # Evaluate Tier 2 decay & promotion
        rows = await self.db.fetchall(
            "SELECT id, importance, user_tier, vitality, access_count FROM memories WHERE memory_tier = 2"
        )
        for row in rows:
            tier_factor = {1: 0.6, 2: 1.2, 3: 2.0}.get(int(row["user_tier"]), 1.0)
            reinforce_factor = 1.0 + 0.5 * math.log(1 + int(row["access_count"]))

            daily_decay = 35.0 / (max(1, int(row["importance"])) * tier_factor * reinforce_factor)
            new_vitality = float(row["vitality"]) - daily_decay

            if new_vitality <= 0:
                await self.db.execute("DELETE FROM memories WHERE id = ?", (row["id"],))
                stats["deleted_tier2"] += 1
            elif int(row["access_count"]) >= 4 and int(row["importance"]) >= 6:
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

    async def extract_memories_from_transcript(
        self,
        soul_id: str,
        soul_name: str,
        transcript_text: str,
        umo: Optional[str] = None,
    ) -> int:
        """
        Invoke small fast LLM with `MEMORY_EXTRACT_AND_GRADE_PROMPT` on evicted sliding-window
        messages and store extracted memories into SQLite.
        """
        if not self.enabled or not (transcript_text or "").strip():
            return 0

        prompt = MEMORY_EXTRACT_AND_GRADE_PROMPT.format(
            soul_name=soul_name,
            recent_chat_transcript=transcript_text,
        )
        items = await self.model_hub.call_llm_json(
            prompt=prompt, role="small", umo=umo, default=[]
        )
        if not isinstance(items, list):
            return 0

        inserted = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            user_id = item.get("user_id")
            if user_id in ("null", "None", ""):
                user_id = None
            user_tier = int(item.get("user_tier") or 1)
            importance = int(item.get("importance") or 5)
            memory_tier = item.get("memory_tier")
            keywords = str(item.get("keywords") or "")

            row_id = await self.add_memory(
                soul_id=soul_id,
                content=content[:50],
                keywords=keywords,
                importance=importance,
                user_id=str(user_id) if user_id else None,
                user_tier=user_tier,
                memory_tier=int(memory_tier) if memory_tier in (1, 2, 3) else None,
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
    ) -> list[MemoryItem]:
        """List memories with optional filtering by `soul_id`, `memory_tier`, `user_id`, and `keyword`."""
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
    ) -> Optional[MemoryItem]:
        """Update fields of an existing memory item."""
        row = await self.db.fetchone("SELECT * FROM memories WHERE id = ?", (int(memory_id),))
        if not row:
            return None
        item = MemoryItem.from_row(row)
        new_content = (content.strip()[:60] if content is not None else item.content) or item.content
        new_kw = keywords.strip() if keywords is not None else item.keywords
        new_imp = max(1, min(10, int(importance))) if importance is not None else item.importance
        new_tier = int(memory_tier) if memory_tier in (1, 2, 3) else item.memory_tier
        new_vit = max(0.0, min(100.0, float(vitality))) if vitality is not None else item.vitality
        new_uid = (user_id.strip() or None) if user_id is not None else item.user_id

        await self.db.execute(
            """
            UPDATE memories
            SET content = ?, keywords = ?, importance = ?, memory_tier = ?, vitality = ?, user_id = ?
            WHERE id = ?
            """,
            (new_content, new_kw, new_imp, new_tier, new_vit, new_uid, int(memory_id)),
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
            "UPDATE memories SET memory_tier = 3, importance = ?, vitality = 100.0 WHERE id = ?",
            (new_imp, int(memory_id)),
        )
        updated = await self.db.fetchone("SELECT * FROM memories WHERE id = ?", (int(memory_id),))
        return MemoryItem.from_row(updated) if updated else None

    async def delete_memory(self, memory_id: int) -> None:
        """Delete a single memory item by `id`."""
        await self.db.execute("DELETE FROM memories WHERE id = ?", (int(memory_id),))
