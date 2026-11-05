"""Three-tier user relationship manager (L1 Normal / L2 Important / L3 Core)."""

import json
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .platform_bridge import PlatformBridge
from .state_machine import UserTier
from ..storage.db import DatabaseManager


@dataclass
class UserProfile:
    """User relationship profile across platforms."""

    global_uid: str
    display_name: str
    tier: UserTier = UserTier.L1_NORMAL
    interaction_count: int = 0
    affinity: float = 0.0
    platform_ids: list[str] = field(default_factory=list)


class UserRelationManager:
    """
    三级用户关系管理器:
    - ★ L3_CORE (3): 管理员 / OnlyYou (配置在 core_user_ids 或平台 admins_id 中)
    - ◆ L2_IMPORTANT (2): 重要熟人 (配置在 important_user_ids 或通过良性互动从 L1 自动晋升)
    - ○ L1_NORMAL (1): 普通用户（群友）
    """

    AUTO_PROMOTE_INTERACTION_THRESHOLD = 12
    AUTO_PROMOTE_AFFINITY_THRESHOLD = 15.0

    def __init__(
        self,
        db: DatabaseManager,
        platform_bridge: PlatformBridge,
        plugin_config: dict[str, Any],
    ):
        self.db = db
        self.platform_bridge = platform_bridge
        self.plugin_config = plugin_config
        self._cache: dict[str, UserProfile] = {}

    @property
    def cfg(self) -> dict[str, Any]:
        return self.plugin_config.get("relationship_config", {}) or {}

    def _get_configured_tier(
        self, sender_id: str, umo: Optional[str] = None, is_event_admin: bool = False
    ) -> Optional[UserTier]:
        """Check if `sender_id` is explicitly configured as L3_CORE or L2_IMPORTANT."""
        sid = str(sender_id).strip()
        if not sid:
            return None

        core_ids = {str(x).strip() for x in (self.cfg.get("core_user_ids") or []) if str(x).strip()}
        platform_admins = set(self.platform_bridge.get_platform_admins(umo=umo))
        if sid in core_ids or sid in platform_admins or is_event_admin:
            return UserTier.L3_CORE

        important_ids = {
            str(x).strip() for x in (self.cfg.get("important_user_ids") or []) if str(x).strip()
        }
        if sid in important_ids:
            return UserTier.L2_IMPORTANT

        return None

    async def resolve_user(self, event: Any) -> UserProfile:
        """Resolve UserProfile from an incoming AstrMessageEvent and record interaction."""
        sender_id = ""
        sender_name = "群友"
        umo = getattr(event, "unified_msg_origin", None)
        is_admin = False

        if hasattr(event, "get_sender_id"):
            sender_id = str(event.get_sender_id() or "").strip()
        if not sender_id:
            sender_id = str(getattr(event, "sender_id", "") or "anonymous").strip()

        if hasattr(event, "get_sender_name"):
            sender_name = str(event.get_sender_name() or sender_id).strip()
        if not sender_name:
            sender_name = sender_id

        if hasattr(event, "is_admin"):
            try:
                is_admin = bool(event.is_admin())
            except Exception:
                is_admin = False

        return await self.get_or_create_user(
            user_id=sender_id,
            display_name=sender_name,
            umo=umo,
            is_event_admin=is_admin,
            bump_interaction=True,
        )

    async def get_or_create_user(
        self,
        user_id: str,
        display_name: str = "",
        umo: Optional[str] = None,
        is_event_admin: bool = False,
        bump_interaction: bool = False,
    ) -> UserProfile:
        """Get or create a UserProfile by `user_id`, applying tier rules & auto-promotion."""
        uid = str(user_id).strip() or "anonymous"
        name = (display_name or uid).strip()
        cfg_tier = self._get_configured_tier(uid, umo=umo, is_event_admin=is_event_admin)

        profile = self._cache.get(uid)
        if profile is None:
            row = await self.db.fetchone(
                "SELECT * FROM users WHERE global_uid = ?", (uid,)
            )
            if row:
                raw_tier = int(row.get("tier", 1))
                tier = (
                    UserTier(raw_tier)
                    if raw_tier in (1, 2, 3)
                    else UserTier.L1_NORMAL
                )
                try:
                    pids = json.loads(row.get("platform_ids_json") or "[]")
                except Exception:
                    pids = [uid]
                profile = UserProfile(
                    global_uid=uid,
                    display_name=str(row.get("display_name") or name),
                    tier=tier,
                    interaction_count=int(row.get("interaction_count", 0)),
                    affinity=float(row.get("affinity", 0.0)),
                    platform_ids=pids,
                )
            else:
                profile = UserProfile(
                    global_uid=uid,
                    display_name=name,
                    tier=cfg_tier or UserTier.L1_NORMAL,
                    interaction_count=0,
                    affinity=0.0,
                    platform_ids=[uid],
                )

        if name and name != uid:
            profile.display_name = name

        # Config tier always upgrades if higher
        if cfg_tier is not None and cfg_tier.value > profile.tier.value:
            profile.tier = cfg_tier

        if bump_interaction:
            profile.interaction_count += 1
            profile.affinity += 1.5
            # Check automatic L1 -> L2 promotion (never auto-promotes to L3_CORE!)
            enable_auto = bool(self.cfg.get("enable_auto_promotion_l2", True))
            if (
                enable_auto
                and profile.tier == UserTier.L1_NORMAL
                and (
                    profile.interaction_count >= self.AUTO_PROMOTE_INTERACTION_THRESHOLD
                    or profile.affinity >= self.AUTO_PROMOTE_AFFINITY_THRESHOLD
                )
            ):
                profile.tier = UserTier.L2_IMPORTANT

        await self.save_user(profile)
        return profile

    async def set_user_tier(self, user_id: str, new_tier: UserTier, display_name: str = "") -> UserProfile:
        """Manually set a user's relationship tier (e.g. via `/onlyyou tier`)."""
        profile = await self.get_or_create_user(user_id=user_id, display_name=display_name)
        profile.tier = new_tier
        await self.save_user(profile)
        return profile

    async def save_user(self, profile: UserProfile) -> None:
        """Persist UserProfile to SQLite and memory cache."""
        self._cache[profile.global_uid] = profile
        await self.db.execute(
            """
            INSERT INTO users (
                global_uid, display_name, tier, interaction_count, affinity, platform_ids_json, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(global_uid) DO UPDATE SET
                display_name = excluded.display_name,
                tier = excluded.tier,
                interaction_count = excluded.interaction_count,
                affinity = excluded.affinity,
                platform_ids_json = excluded.platform_ids_json,
                updated_at = excluded.updated_at
            """,
            (
                profile.global_uid,
                profile.display_name,
                int(profile.tier.value),
                profile.interaction_count,
                profile.affinity,
                json.dumps(profile.platform_ids, ensure_ascii=False),
                int(time.time()),
            ),
        )
