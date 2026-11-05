"""Multimodal video preprocessor interface (3x3 keyframe grid & vision scoring)."""

from dataclasses import dataclass
from typing import Any, Optional

from .adapters.base import VideoItem
from ..core.model_hub import ModelHub


@dataclass
class VideoReviewResult:
    """Result of watching and scoring a video via Vision / Fast LLM."""

    video: VideoItem
    score: int  # 0 ~ 100 (>=88 triggers bounded schedule mutation "刷片上头")
    commentary: str  # First-person reaction/commentary


class VideoVisionProcessor:
    """
    多模态视频预处理器 (`surfing/video_vision.py`):
    - 预留 `build_3x3_keyframe_grid` 接口（用于 FFmpeg 抽帧拼接 3x3 时间戳九宫格）
    - 提供基于封面图 (`cover_url`) + 标题 + 热评的多模态看片评分与第一人称吐槽生成
    """

    def __init__(self, model_hub: ModelHub):
        self.model_hub = model_hub

    async def build_3x3_keyframe_grid(
        self, video_stream_url: str, duration_sec: int = 60
    ) -> Optional[str]:
        """
        Reserved interface: Extract 9 evenly-spaced keyframes via FFmpeg and stitch them
        into a single 3x3 timestamped grid image path for Vision LLM inspection.
        """
        return None

    async def watch_and_evaluate(
        self,
        video: VideoItem,
        soul_name: str,
        soul_archetype: str,
        surfing_preferences: list[str],
        umo: Optional[str] = None,
    ) -> VideoReviewResult:
        """Evaluate a video using Vision/Small LLM and generate a first-person commentary."""
        prompt = (
            f"你是【{soul_name}】（设定：{soul_archetype}，冲浪偏好：{', '.join(surfing_preferences[:5])}）。\n"
            f"你刚才在空闲时间刷到了这个视频：\n"
            f"- 标题：《{video.title}》 (作者：{video.author})\n"
            f"- 简介：{video.description}\n"
            f"- 网友热评：{' | '.join(video.hot_comments[:3])}\n\n"
            "请以第一人称给这个视频打一个感兴趣/好笑程度分（`score`: 0~100，若超级搞笑上头可打 88~95 分），"
            "并写一句你想发给好友分享或吐槽的话（`commentary`，35字以内，口语自然）。\n"
            '请仅输出合法 JSON：{"score": 85, "commentary": "..."}'
        )

        image_urls = [video.cover_url] if video.cover_url.startswith("http") else None
        res: Any = await self.model_hub.call_llm_json(
            prompt=prompt,
            role="vision" if image_urls else "small",
            umo=umo,
            image_urls=image_urls,
            default=None,
        )

        if isinstance(res, dict) and "commentary" in res:
            score = max(0, min(100, int(res.get("score") or 78)))
            commentary = str(res.get("commentary") or "").strip()
            if commentary:
                return VideoReviewResult(video=video, score=score, commentary=commentary)

        return VideoReviewResult(
            video=video,
            score=80,
            commentary=f"刚刷到《{video.title}》，评论区笑死我了hhh",
        )
