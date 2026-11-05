"""Abstract base interface and data models for video surfing adapters."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class VideoItem:
    """Standardized video metadata across Bilibili / Douyin."""

    platform: str  # "bilibili" | "douyin"
    video_id: str
    title: str
    author: str
    share_url: str
    cover_url: str = ""
    description: str = ""
    duration_sec: int = 0
    hot_comments: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    raw_stream_url: Optional[str] = None

    def format_brief(self) -> str:
        comments_str = " / ".join(self.hot_comments[:3]) if self.hot_comments else "暂无热评"
        return (
            f"[{self.platform.upper()}] 《{self.title}》(UP/作者: {self.author}) "
            f"| 链接: {self.share_url} | 热评: {comments_str}"
        )


class BaseVideoAdapter(ABC):
    """Abstract interface for platform video fetching & comment scraping."""

    platform_name: str = "base"

    @abstractmethod
    async def fetch_recommend_feed(
        self, preferences: Optional[list[str]] = None, limit: int = 5
    ) -> list[VideoItem]:
        """Fetch recommended or trending videos matching preferences."""
        raise NotImplementedError

    @abstractmethod
    async def fetch_hot_comments(self, video_id: str, limit: int = 5) -> list[str]:
        """Fetch top/hot comments for a video."""
        raise NotImplementedError

    async def resolve_raw_video_stream(self, video_id: str) -> Optional[str]:
        """
        Reserved interface: Resolve direct watermark-free MP4 stream URL for downloading
        or sending raw video files.
        """
        return None
