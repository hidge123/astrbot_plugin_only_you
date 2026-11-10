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
    nickname: str = ""
    # v2.6: the character's custom attitude / tone reserved for this exact user.
    attitude: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "global_uid": self.global_uid,
            "display_name": self.display_name,
            "nickname": self.nickname,
            "attitude": self.attitude,
            "tier": int(self.tier.value),
            "tier_label": self.tier.label,
            "interaction_count": self.interaction_count,
            "affinity": round(float(self.affinity), 1),
            "platform_ids": list(self.platform_ids),
        }


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
            if not row:
                # Check if `uid` was merged into another `global_uid` via `platform_ids_json`
                all_rows = await self.db.fetchall("SELECT * FROM users")
                for r in all_rows:
                    try:
                        pids = json.loads(r.get("platform_ids_json") or "[]")
                    except Exception:
                        pids = []
                    if uid in pids:
                        row = r
                        uid = str(r["global_uid"])
                        break
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
                    nickname=str(row.get("nickname") or ""),
                    attitude=str(row.get("attitude") or ""),
                )
            else:
                profile = UserProfile(
                    global_uid=uid,
                    display_name=name,
                    tier=cfg_tier or UserTier.L1_NORMAL,
                    interaction_count=0,
                    affinity=0.0,
                    platform_ids=[uid],
                    nickname="",
                    attitude="",
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
        for pid in profile.platform_ids:
            self._cache[pid] = profile
        await self.db.execute(
            """
            INSERT INTO users (
                global_uid, display_name, nickname, tier, interaction_count, affinity, platform_ids_json, attitude, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(global_uid) DO UPDATE SET
                display_name = excluded.display_name,
                nickname = excluded.nickname,
                tier = excluded.tier,
                interaction_count = excluded.interaction_count,
                affinity = excluded.affinity,
                platform_ids_json = excluded.platform_ids_json,
                attitude = excluded.attitude,
                updated_at = excluded.updated_at
            """,
            (
                profile.global_uid,
                profile.display_name,
                profile.nickname,
                int(profile.tier.value),
                profile.interaction_count,
                profile.affinity,
                json.dumps(profile.platform_ids, ensure_ascii=False),
                profile.attitude,
                int(time.time()),
            ),
        )

    async def list_users(self, tier_filter: Optional[int] = None) -> list[UserProfile]:
        """List all known users (seeding configured L3/L2 IDs if not yet in SQLite)."""
        for cid in self.cfg.get("core_user_ids") or []:
            if str(cid).strip():
                await self.get_or_create_user(str(cid).strip())
        for iid in self.cfg.get("important_user_ids") or []:
            if str(iid).strip():
                await self.get_or_create_user(str(iid).strip())

        if tier_filter in (1, 2, 3):
            rows = await self.db.fetchall(
                "SELECT * FROM users WHERE tier = ? ORDER BY tier DESC, affinity DESC, interaction_count DESC",
                (int(tier_filter),),
            )
        else:
            rows = await self.db.fetchall(
                "SELECT * FROM users ORDER BY tier DESC, affinity DESC, interaction_count DESC"
            )

        users: list[UserProfile] = []
        for r in rows:
            raw_tier = int(r.get("tier", 1))
            t = UserTier(raw_tier) if raw_tier in (1, 2, 3) else UserTier.L1_NORMAL
            try:
                pids = json.loads(r.get("platform_ids_json") or "[]")
            except Exception:
                pids = [str(r["global_uid"])]
            u = UserProfile(
                global_uid=str(r["global_uid"]),
                display_name=str(r.get("display_name") or r["global_uid"]),
                tier=t,
                interaction_count=int(r.get("interaction_count", 0)),
                affinity=float(r.get("affinity", 0.0)),
                platform_ids=pids,
                nickname=str(r.get("nickname") or ""),
                attitude=str(r.get("attitude") or ""),
            )
            self._cache[u.global_uid] = u
            users.append(u)
        return users

    async def update_user_details(
        self,
        global_uid: str,
        display_name: Optional[str] = None,
        nickname: Optional[str] = None,
        tier: Optional[int] = None,
        affinity: Optional[float] = None,
        platform_ids: Optional[list[str]] = None,
        attitude: Optional[str] = None,
    ) -> UserProfile:
        """Create or update a user's tier, affinity, nickname, display_name, platform_ids, attitude."""
        profile = await self.get_or_create_user(
            user_id=global_uid, display_name=display_name or ""
        )
        if display_name is not None and display_name.strip():
            profile.display_name = display_name.strip()
        if nickname is not None:
            profile.nickname = nickname.strip()
        if attitude is not None:
            # v2.6: per-user custom attitude, hard-capped at 300 chars.
            profile.attitude = attitude.strip()[:300]
        if tier in (1, 2, 3):
            profile.tier = UserTier(int(tier))
        if affinity is not None:
            profile.affinity = max(0.0, min(100.0, float(affinity)))
        if platform_ids is not None:
            cleaned = [str(p).strip() for p in platform_ids if str(p).strip()]
            if profile.global_uid not in cleaned:
                cleaned.insert(0, profile.global_uid)
            profile.platform_ids = cleaned
        await self.save_user(profile)
        return profile

    async def merge_users(self, primary_uid: str, secondary_uid: str) -> UserProfile:
        """
        Merge `secondary_uid` into `primary_uid` (`GlobalUser` unification across platforms):
        - Combine `platform_ids`, sum `interaction_count`, take max `tier` & `affinity`
        - Re-link `memories.user_id` from `secondary_uid` to `primary_uid`
        - Delete `secondary_uid` row from `users`
        """
        p_id = (primary_uid or "").strip()
        s_id = (secondary_uid or "").strip()
        if not p_id or not s_id or p_id == s_id:
            raise ValueError("请选择两个不同的账号进行跨平台合并")

        primary = await self.get_or_create_user(p_id)
        secondary = await self.get_or_create_user(s_id)

        merged_pids = list(primary.platform_ids)
        for pid in [s_id] + list(secondary.platform_ids):
            if pid and pid not in merged_pids:
                merged_pids.append(pid)

        primary.platform_ids = merged_pids
        if secondary.tier.value > primary.tier.value:
            primary.tier = secondary.tier
        primary.interaction_count += secondary.interaction_count
        primary.affinity = min(100.0, max(primary.affinity, secondary.affinity) + secondary.affinity * 0.5)
        if not primary.nickname and secondary.nickname:
            primary.nickname = secondary.nickname
        if not primary.attitude and secondary.attitude:
            primary.attitude = secondary.attitude

        await self.save_user(primary)
        await self.db.execute(
            "UPDATE memories SET user_id = ? WHERE user_id = ?", (p_id, s_id)
        )
        await self.db.execute("DELETE FROM users WHERE global_uid = ?", (s_id,))
        self._cache[s_id] = primary
        return primary

    async def delete_user(self, global_uid: str) -> None:
        """Delete a user record by `global_uid`."""
        uid = (global_uid or "").strip()
        if not uid:
            return
        self._cache.pop(uid, None)
        await self.db.execute("DELETE FROM users WHERE global_uid = ?", (uid,))
