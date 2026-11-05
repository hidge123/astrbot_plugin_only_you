"""Bilibili video adapter (popular feed + hot comments + reserved stream interface)."""

from typing import Optional

try:
    import httpx
except ImportError:
    httpx = None

from .base import BaseVideoAdapter, VideoItem


class BilibiliAdapter(BaseVideoAdapter):
    """Bilibili surfing adapter."""

    platform_name = "bilibili"

    def __init__(self, cookie: str = ""):
        self.cookie = (cookie or "").strip()

    def _headers(self) -> dict[str, str]:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Referer": "https://www.bilibili.com/",
        }
        if self.cookie:
            headers["Cookie"] = self.cookie
        return headers

    async def fetch_recommend_feed(
        self, preferences: Optional[list[str]] = None, limit: int = 5
    ) -> list[VideoItem]:
        """Fetch Bilibili popular videos, with fallback sample items if network is restricted."""
        items: list[VideoItem] = []
        try:
            async with httpx.AsyncClient(timeout=8.0, headers=self._headers()) as client:
                resp = await client.get(
                    "https://api.bilibili.com/x/web-interface/popular",
                    params={"ps": max(5, limit * 2), "pn": 1},
                )
                data = resp.json()
                raw_list = (data.get("data") or {}).get("list") or []
                for entry in raw_list[:limit]:
                    bvid = str(entry.get("bvid") or "")
                    aid = str(entry.get("aid") or "")
                    title = str(entry.get("title") or "B站热门视频")
                    owner = (entry.get("owner") or {}).get("name") or "UP主"
                    pic = str(entry.get("pic") or "")
                    desc = str(entry.get("desc") or "")
                    duration = int(entry.get("duration") or 0)
                    rcmd_reason = (
                        (entry.get("rcmd_reason") or {}).get("content") or ""
                    )
                    comments = [rcmd_reason] if rcmd_reason else []
                    if aid and not comments:
                        comments = await self.fetch_hot_comments(aid, limit=3)
                    items.append(
                        VideoItem(
                            platform="bilibili",
                            video_id=bvid or aid,
                            title=title,
                            author=owner,
                            share_url=f"https://www.bilibili.com/video/{bvid}" if bvid else "https://www.bilibili.com",
                            cover_url=pic,
                            description=desc[:120],
                            duration_sec=duration,
                            hot_comments=comments,
                            tags=[ str(entry.get("tname") or "热门") ],
                        )
                    )
        except Exception:
            pass

        if not items:
            # Fallback curated items when offline or rate-limited
            items = [
                VideoItem(
                    platform="bilibili",
                    video_id="BV1xx411c7mD",
                    title="当小猫咪发现自己假装晕倒就能骗到罐头后，演技彻底刹不住了",
                    author="喵星观察局",
                    share_url="https://www.bilibili.com/video/BV1xx411c7mD",
                    description="橘猫为了骗零食上演奥斯卡级碰瓷",
                    duration_sec=48,
                    hot_comments=[
                        "奥斯卡欠它一座小金人哈哈哈哈",
                        "最后那个偷瞄铲屎官的小眼神绝了",
                        "猫：这罐头我吃得凭本事！",
                    ],
                    tags=["搞笑动物", "萌宠日常"],
                )
            ]
        return items[:limit]

    async def fetch_hot_comments(self, video_id: str, limit: int = 5) -> list[str]:
        """Fetch top replies for an aid on Bilibili."""
        if not str(video_id).isdigit():
            return []
        try:
            async with httpx.AsyncClient(timeout=5.0, headers=self._headers()) as client:
                resp = await client.get(
                    "https://api.bilibili.com/x/v2/reply/main",
                    params={"type": 1, "oid": video_id, "mode": 3, "ps": limit},
                )
                data = resp.json()
                replies = (data.get("data") or {}).get("replies") or []
                return [
                    str((r.get("content") or {}).get("message") or "").strip()[:60]
                    for r in replies[:limit]
                    if (r.get("content") or {}).get("message")
                ]
        except Exception:
            return []
