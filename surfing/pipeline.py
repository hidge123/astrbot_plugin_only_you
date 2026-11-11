"""Main cyber surfing pipeline (active ONLY when BotState == IDLE)."""

import json
import time
from typing import Any, Optional

from .adapters.base import VideoItem
from .adapters.bilibili import BilibiliAdapter
from .adapters.douyin import DouyinAdapter
from .video_vision import VideoReviewResult, VideoVisionProcessor
from ..core.context_budget import SessionBuffer
from ..core.memory_manager import MemoryManager, MemoryTier
from ..core.model_hub import ModelHub
from ..core.schedule_mutator import BoundedScheduleMutator
from ..core.soul_manager import SoulProfile
from ..core.state_machine import BotState, StateController


class CyberSurfingPipeline:
    """
    📱 赛博冲浪主控流水线 (`surfing/pipeline.py`):
    - 严格受三态控制器约束：仅在 `BotState.IDLE` 下运行，`RESTING` / `BUSY` 直接挂起！
    - 拉取 B站/抖音推荐流 + 热评 -> 多模态看片打分与吐槽 -> 记入短期滑窗与 Tier 1 瞬时记忆
    - 若刷到评分 >= 88 的神作，触发 `BoundedScheduleMutator.on_video_input` 受限微调！
    """

    def __init__(
        self,
        model_hub: ModelHub,
        memory_manager: MemoryManager,
        session_buffer: SessionBuffer,
        schedule_mutator: BoundedScheduleMutator,
        plugin_config: dict[str, Any],
    ):
        self.model_hub = model_hub
        self.memory_manager = memory_manager
        self.session_buffer = session_buffer
        self.schedule_mutator = schedule_mutator
        self.plugin_config = plugin_config
        self.vision_processor = VideoVisionProcessor(model_hub=model_hub)
        self._watched_ids: set[str] = set()

    @property
    def cfg(self) -> dict[str, Any]:
        return self.plugin_config.get("surfing_config", {}) or {}

    def is_enabled(self, soul: Any = None) -> bool:
        """Whether cyber surfing is enabled for ``soul`` (v2.8 §5.2).

        The per-Soul ``settings.surfing_enabled`` value wins; when the Soul does not
        carry that key, fall back to the global ``surfing_config.surfing_enabled``
        (default ``True``).
        """
        try:
            global_enabled = bool(self.cfg.get("surfing_enabled", True))
        except Exception:  # noqa: BLE001 - defensive, never gate on config errors
            global_enabled = True
        if soul is None:
            return global_enabled
        settings = getattr(soul, "settings", None)
        if isinstance(settings, dict):
            if "surfing_enabled" in settings:
                return bool(settings.get("surfing_enabled"))
            return global_enabled
        if isinstance(soul, dict) and "surfing_enabled" in soul:
            return bool(soul.get("surfing_enabled"))
        # Tolerate lightweight duck-typed Souls exposing the flag directly.
        attribute = getattr(soul, "surfing_enabled", None)
        if isinstance(attribute, bool):
            return attribute
        return global_enabled

    async def surf_once(
        self,
        soul: SoulProfile,
        current_state: BotState,
        umo: Optional[str] = None,
        platform_filter: Optional[str] = None,
        ignore_state_gate: bool = False,
    ) -> Optional[VideoReviewResult]:
        """
        Execute one surfing cycle if `current_state == BotState.IDLE` (or `ignore_state_gate=True`).
        """
        # v2.8 §5.2: the per-Soul / global switch gates even manual triggers.
        if not self.is_enabled(soul):
            return None
        if not ignore_state_gate and not StateController.can_surf_video(current_state):
            return None

        p_filter = (platform_filter or "").strip().lower()
        candidates: list[VideoItem] = []

        if p_filter == "bilibili" or (not p_filter and self.cfg.get("enable_bili_surfing", True)):
            bili = BilibiliAdapter(cookie=str(self.cfg.get("bili_cookie") or ""))
            candidates.extend(
                await bili.fetch_recommend_feed(preferences=soul.surfing_preferences, limit=4)
            )

        if p_filter == "douyin" or (
            not candidates and not p_filter and self.cfg.get("enable_douyin_surfing", True)
        ):
            douyin = DouyinAdapter(
                api_base=str(self.cfg.get("douyin_api_base") or "https://api.douyin.wtf")
            )
            candidates.extend(
                await douyin.fetch_recommend_feed(preferences=soul.surfing_preferences, limit=3)
            )

        if not candidates:
            return None

        # v2.8 §5.4: when every candidate has already been watched, return None
        # instead of re-processing / falling back to ``candidates[0]``.
        chosen = next((v for v in candidates if v.video_id not in self._watched_ids), None)
        if chosen is None:
            return None
        self._watched_ids.add(chosen.video_id)

        review = await self.vision_processor.watch_and_evaluate(
            video=chosen,
            soul_name=soul.name,
            soul_archetype=soul.archetype,
            surfing_preferences=soul.surfing_preferences,
            umo=umo,
        )

        # Record into SessionBuffer & Tier 1 Ephemeral Memory
        summary = f"《{chosen.title}》({chosen.share_url}) —— 我的观后感：{review.commentary}"
        self.session_buffer.record_watched_video(summary)

        await self.memory_manager.add_memory(
            soul_id=soul.soul_id,
            content=f"刚刷到视频《{chosen.title[:22]}》：{review.commentary[:24]}",
            keywords=",".join(chosen.tags[:2] + ["刷视频", chosen.platform]),
            importance=3,
            user_id=None,
            user_tier=1,
            memory_tier=MemoryTier.EPHEMERAL,
        )

        # Persist to `surfing_history` table for WebUI Console Module 5
        try:
            await self.memory_manager.db.execute(
                """
                INSERT INTO surfing_history (
                    soul_id, video_id, platform, title, author, cover_url, share_url,
                    tags_json, hot_comments_json, visual_notes, score, commentary,
                    worth_sharing, watched_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    soul.soul_id,
                    chosen.video_id,
                    chosen.platform,
                    chosen.title,
                    chosen.author,
                    chosen.cover_url,
                    chosen.share_url,
                    json.dumps(chosen.tags, ensure_ascii=False),
                    json.dumps(chosen.hot_comments, ensure_ascii=False),
                    review.visual_notes,
                    int(review.score),
                    review.commentary,
                    1 if review.score >= 70 else 0,
                    int(time.time()),
                ),
            )
        except Exception:
            pass

        # Check Scenario 4 (刷片上头 >= 88): trigger bounded schedule mutation
        await self.schedule_mutator.on_video_input(
            soul=soul,
            video_title=chosen.title,
            video_score=review.score,
            umo=umo,
        )

        return review

    async def list_history(
        self, soul_id: Optional[str] = None, limit: int = 30
    ) -> list[dict[str, Any]]:
        """List surfed video records from SQLite `surfing_history` table."""
        if soul_id:
            rows = await self.memory_manager.db.fetchall(
                "SELECT * FROM surfing_history WHERE soul_id = ? ORDER BY watched_at DESC, id DESC LIMIT ?",
                (soul_id, int(limit)),
            )
        else:
            rows = await self.memory_manager.db.fetchall(
                "SELECT * FROM surfing_history ORDER BY watched_at DESC, id DESC LIMIT ?",
                (int(limit),),
            )
        items: list[dict[str, Any]] = []
        for r in rows:
            try:
                tags = json.loads(r.get("tags_json") or "[]")
            except Exception:
                tags = []
            try:
                hot_comments = json.loads(r.get("hot_comments_json") or "[]")
            except Exception:
                hot_comments = []
            items.append(
                {
                    "id": int(r["id"]),
                    "soul_id": str(r["soul_id"]),
                    "video_id": str(r["video_id"]),
                    "platform": str(r["platform"]),
                    "title": str(r["title"]),
                    "author": str(r["author"]),
                    "cover_url": str(r.get("cover_url") or ""),
                    "share_url": str(r.get("share_url") or ""),
                    "tags": tags,
                    "hot_comments": hot_comments,
                    "visual_notes": str(r.get("visual_notes") or ""),
                    "score": int(r.get("score") or 80),
                    "commentary": str(r.get("commentary") or ""),
                    "worth_sharing": bool(r.get("worth_sharing", 1)),
                    "watched_at": int(r.get("watched_at") or 0),
                }
            )
        return items

    async def delete_history_item(self, item_id: Optional[int] = None, video_id: Optional[str] = None) -> None:
        """Delete a video from `surfing_history` by row `id` or `video_id`."""
        if item_id is not None:
            await self.memory_manager.db.execute(
                "DELETE FROM surfing_history WHERE id = ?", (int(item_id),)
            )
        elif video_id:
            await self.memory_manager.db.execute(
                "DELETE FROM surfing_history WHERE video_id = ?", (str(video_id),)
            )
