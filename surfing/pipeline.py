"""Main cyber surfing pipeline (active ONLY when BotState == IDLE)."""

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

    async def surf_once(
        self,
        soul: SoulProfile,
        current_state: BotState,
        umo: Optional[str] = None,
    ) -> Optional[VideoReviewResult]:
        """
        Execute one surfing cycle if and only if `current_state == BotState.IDLE`.
        """
        if not StateController.can_surf_video(current_state):
            return None

        candidates: list[VideoItem] = []
        if self.cfg.get("enable_bili_surfing", True):
            bili = BilibiliAdapter(cookie=str(self.cfg.get("bili_cookie") or ""))
            candidates.extend(
                await bili.fetch_recommend_feed(preferences=soul.surfing_preferences, limit=4)
            )

        if not candidates and self.cfg.get("enable_douyin_surfing", True):
            douyin = DouyinAdapter(
                api_base=str(self.cfg.get("douyin_api_base") or "https://api.douyin.wtf")
            )
            candidates.extend(
                await douyin.fetch_recommend_feed(preferences=soul.surfing_preferences, limit=3)
            )

        if not candidates:
            return None

        chosen = next((v for v in candidates if v.video_id not in self._watched_ids), candidates[0])
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

        # Check Scenario 4 (刷片上头 >= 88): trigger bounded schedule mutation
        await self.schedule_mutator.on_video_input(
            soul=soul,
            video_title=chosen.title,
            video_score=review.score,
            umo=umo,
        )

        return review
