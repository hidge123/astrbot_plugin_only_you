"""Douyin video adapter (reserved interface + configurable API base skeleton)."""

from typing import Optional
from .base import BaseVideoAdapter, VideoItem


class DouyinAdapter(BaseVideoAdapter):
    """
    Douyin surfing adapter.
    Note: Deep Douyin scraping and watermark-free stream parsing depend on external parser
    services (`douyin_api_base`); this class provides the standard adapter interface.
    """

    platform_name = "douyin"

    def __init__(self, api_base: str = "https://api.douyin.wtf"):
        self.api_base = (api_base or "https://api.douyin.wtf").rstrip("/")

    async def fetch_recommend_feed(
        self, preferences: Optional[list[str]] = None, limit: int = 5
    ) -> list[VideoItem]:
        """Reserved interface for Douyin trending/feed fetching."""
        return []

    async def fetch_hot_comments(self, video_id: str, limit: int = 5) -> list[str]:
        """Reserved interface for Douyin hot comment fetching."""
        return []

    async def resolve_raw_video_stream(self, video_id: str) -> Optional[str]:
        """Reserved interface for watermark-free Douyin MP4 URL resolution."""
        return None
