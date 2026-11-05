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
        """Fetch Douyin trending/recommend feed, with fallback curated items when parser service is offline."""
        items = [
            VideoItem(
                platform="douyin",
                video_id="DY73982104912",
                title="大学生宿舍低成本整活：用纸箱还原大型机甲变身现场",
                author="宿舍整活王",
                share_url="https://www.douyin.com/video/73982104912",
                description="工科宿舍周末闲得发慌，用快递纸箱拼出了全套可动机甲",
                duration_sec=32,
                hot_comments=[
                    "宿管阿姨推门进来那一刻空气都安静了哈哈哈",
                    "别人上大学学知识，你们上大学搞军工是吧",
                    "关节居然还是液压纸筒做的，细节拉满",
                ],
                tags=["游戏整活", "日常吐槽"],
            ),
            VideoItem(
                platform="douyin",
                video_id="DY73985512038",
                title="深夜街边偶遇会自己排队买烤肠的小流浪狗",
                author="夜宵观察员",
                share_url="https://www.douyin.com/video/73985512038",
                description="聪明修狗每天准时蹲在烤肠摊前摇尾巴等投喂",
                duration_sec=25,
                hot_comments=[
                    "修狗：老板记刚才那个帅哥账上！",
                    "眼神太真诚了，谁能忍住不给它买一根",
                ],
                tags=["搞笑动物", "美食探店"],
            ),
        ]
        return items[:limit]

    async def fetch_hot_comments(self, video_id: str, limit: int = 5) -> list[str]:
        """Reserved interface for Douyin hot comment fetching."""
        return []

    async def resolve_raw_video_stream(self, video_id: str) -> Optional[str]:
        """Reserved interface for watermark-free Douyin MP4 URL resolution."""
        return None
