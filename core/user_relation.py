"""Three-tier user relationship manager (L1 Normal / L2 Important / L3 Core).

v2.8 additionally keeps a lightweight **group profile** registry (``GroupProfile``)
persisted in ``plugin_settings["group_profiles"]``: it backs the console
关系页 group section and is refreshed by ``main.on_any_message`` through
``touch_group`` (contract §4.2 / §6.3).
"""

import json
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .platform_bridge import PlatformBridge
from .state_machine import UserTier
from ..storage.db import DatabaseManager

try:  # pragma: no cover - AstrBot runtime dependency only
    from astrbot.api import logger
except ImportError:  # pragma: no cover - offline test fallback
    import logging

    logger = logging.getLogger("onlyyou.user_relation")


# plugin_settings KV key holding the persisted group profile list (contract §6.3).
GROUP_SETTING_KEY = "group_profiles"

# Existing group profile: persist the refreshed ``last_active_at`` at most this
# often (contract §6.3: 已有群最多每 60s 持久化一次).
GROUP_PERSIST_THROTTLE_SECONDS = 60.0

# Hard caps guarding malformed client payloads (never part of the frozen contract,
# purely defensive so one bad console edit cannot bloat plugin_settings forever).
_GROUP_NAME_MAX_CHARS = 120
_GROUP_NOTES_MAX_CHARS = 500


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


@dataclass
class GroupProfile:
    """A discovered group chat (contract §6.3).

    ``user_relation`` only records *metadata*; the sliding window / unread backlog
    itself lives in ``SessionBuffer`` and is looked up by ``umo`` on the Web API side.
    """

    umo: str
    group_id: str = ""
    platform_id: str = ""
    display_name: str = ""
    notes: str = ""
    bound_soul_id: str = ""
    last_active_at: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "umo": self.umo,
            "group_id": self.group_id,
            "platform_id": self.platform_id,
            "display_name": self.display_name,
            "notes": self.notes,
            "bound_soul_id": self.bound_soul_id,
            "last_active_at": int(self.last_active_at or 0),
        }

    @classmethod
    def from_dict(cls, data: Any) -> Optional["GroupProfile"]:
        """Best-effort parse of one stored entry (``None`` for malformed rows)."""
        if not isinstance(data, dict):
            return None
        umo = str(data.get("umo") or "").strip()
        if not umo:
            return None
        try:
            last_active_at = int(data.get("last_active_at") or 0)
        except (TypeError, ValueError):
            last_active_at = 0
        return cls(
            umo=umo,
            group_id=str(data.get("group_id") or "").strip(),
            platform_id=str(data.get("platform_id") or "").strip(),
            display_name=str(data.get("display_name") or "").strip(),
            notes=str(data.get("notes") or "").strip(),
            bound_soul_id=str(data.get("bound_soul_id") or "").strip(),
            last_active_at=last_active_at,
        )


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
        # ---- v2.8 group profiles (plugin_settings["group_profiles"]) ----
        self._groups_cache: Optional[dict[str, GroupProfile]] = None
        self._group_persist_ts: dict[str, float] = {}

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

    # ==================================================================
    # v2.8 group chat profiles (contract §4.2 / §6.3)
    # ==================================================================
    @staticmethod
    def _default_group_display_name(group_id: str = "", umo: str = "") -> str:
        """``群聊 {group_id or umo tail}`` (contract §6.3 fallback display name)."""
        name = (group_id or "").strip()
        if not name:
            parts = (umo or "").strip().split(":")
            name = (parts[2].strip() if len(parts) >= 3 else (umo or "").strip()) or ""
        return f"群聊 {name}" if name else "群聊"

    async def _ensure_groups_loaded(self) -> dict[str, GroupProfile]:
        """Load ``plugin_settings["group_profiles"]`` once (never raises)."""
        if self._groups_cache is not None:
            return self._groups_cache
        groups: dict[str, GroupProfile] = {}
        raw: Any = []
        try:
            raw = await self.db.get_setting(GROUP_SETTING_KEY, [])
        except Exception as exc:  # malformed / missing storage must never break chat
            logger.warning(f"[OnlyYou] load group_profiles failed: {exc}")
            raw = []
        # Tolerate an accidental mapping payload as well as a proper list.
        if isinstance(raw, dict):
            raw = list(raw.values())
        if isinstance(raw, (list, tuple)):
            for item in raw:
                profile = GroupProfile.from_dict(item)
                if profile is not None:
                    groups[profile.umo] = profile
        self._groups_cache = groups
        return groups

    async def _persist_groups(self) -> None:
        """Flush the whole group registry to ``plugin_settings`` (never raises)."""
        groups = await self._ensure_groups_loaded()
        try:
            await self.db.set_setting(
                GROUP_SETTING_KEY, [group.to_dict() for group in groups.values()]
            )
        except Exception as exc:
            logger.warning(f"[OnlyYou] persist group_profiles failed: {exc}")

    async def list_groups(self, soul_id: Optional[str] = None) -> list[GroupProfile]:
        """List group profiles (optionally only the ones governed by ``soul_id``)."""
        try:
            groups = list((await self._ensure_groups_loaded()).values())
        except Exception as exc:  # pragma: no cover - purely defensive
            logger.warning(f"[OnlyYou] list_groups failed: {exc}")
            return []
        wanted = (soul_id or "").strip()
        if wanted:
            groups = [group for group in groups if group.bound_soul_id == wanted]
        groups.sort(key=lambda group: (-int(group.last_active_at or 0), group.umo))
        return groups

    async def get_group(self, umo: str) -> Optional[GroupProfile]:
        """Return one group profile by its full UMO (``None`` when unknown)."""
        clean = (umo or "").strip()
        if not clean:
            return None
        try:
            return (await self._ensure_groups_loaded()).get(clean)
        except Exception as exc:  # pragma: no cover - purely defensive
            logger.warning(f"[OnlyYou] get_group failed: {exc}")
            return None

    async def touch_group(
        self,
        umo: str,
        group_id: str = "",
        platform_id: str = "",
        display_name: str = "",
        bound_soul_id: str = "",
    ) -> GroupProfile:
        """Create/refresh a group profile from an incoming group message.

        Called by ``main.on_any_message`` (contract §4.2) on every group message, so
        this method is intentionally cheap and **never raises**: a new group persists
        immediately, an existing group's ``last_active_at`` is persisted at most once
        per :data:`GROUP_PERSIST_THROTTLE_SECONDS` (default 60s, contract §6.3).
        """
        clean_umo = str(umo or "").strip()
        clean_group_id = str(group_id or "").strip()
        clean_platform_id = str(platform_id or "").strip()
        clean_bound = str(bound_soul_id or "").strip()

        try:
            groups = await self._ensure_groups_loaded()
        except Exception:  # pragma: no cover - _ensure_groups_loaded never raises
            groups = {}
        if not clean_umo:
            # No UMO -> nothing addressable/persistable; return a usable stand-in.
            profile = GroupProfile(
                umo="", group_id=clean_group_id, platform_id=clean_platform_id
            )
            profile.display_name = self._default_group_display_name(clean_group_id)
            return profile

        existing = groups.get(clean_umo)
        is_new = existing is None
        profile = existing or GroupProfile(umo=clean_umo)
        now = int(time.time())

        if clean_group_id:
            profile.group_id = clean_group_id
        elif not profile.group_id:
            parts = clean_umo.split(":")
            profile.group_id = parts[2].strip() if len(parts) >= 3 else clean_umo
        if clean_platform_id:
            profile.platform_id = clean_platform_id
        elif not profile.platform_id:
            profile.platform_id = clean_umo.split(":", 1)[0].strip()
        if clean_bound:
            profile.bound_soul_id = clean_bound

        default_name = self._default_group_display_name(profile.group_id, profile.umo)
        provided_name = str(display_name or "").strip()
        if provided_name and (
            not profile.display_name or profile.display_name.startswith("群聊 ")
        ):
            # Fill / upgrade the auto placeholder, but never clobber a custom name.
            profile.display_name = provided_name[:_GROUP_NAME_MAX_CHARS]
        if not profile.display_name:
            profile.display_name = default_name
        profile.last_active_at = now
        groups[clean_umo] = profile
        self._groups_cache = groups

        last_persist = self._group_persist_ts.get(clean_umo, 0.0)
        if is_new or (time.time() - last_persist) >= GROUP_PERSIST_THROTTLE_SECONDS:
            self._group_persist_ts[clean_umo] = time.time()
            await self._persist_groups()
        return profile

    async def update_group(
        self,
        umo: str,
        display_name: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> Optional[GroupProfile]:
        """Edit a group's display name / notes (``None`` when the group is unknown)."""
        clean = (umo or "").strip()
        if not clean:
            return None
        try:
            groups = await self._ensure_groups_loaded()
        except Exception:  # pragma: no cover - purely defensive
            return None
        profile = groups.get(clean)
        if profile is None:
            return None
        if display_name is not None:
            name = str(display_name).strip()
            profile.display_name = (
                name[:_GROUP_NAME_MAX_CHARS]
                if name
                else self._default_group_display_name(profile.group_id, profile.umo)
            )
        if notes is not None:
            profile.notes = str(notes).strip()[:_GROUP_NOTES_MAX_CHARS]
        await self._persist_groups()
        return profile

    async def delete_group(self, umo: str) -> bool:
        """Remove one group profile; returns whether it existed (never raises)."""
        clean = (umo or "").strip()
        if not clean:
            return False
        try:
            groups = await self._ensure_groups_loaded()
        except Exception:  # pragma: no cover - purely defensive
            return False
        existed = groups.pop(clean, None) is not None
        self._groups_cache = groups
        self._group_persist_ts.pop(clean, None)
        if existed:
            await self._persist_groups()
        return existed
