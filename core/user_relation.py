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

#: One-shot migration flag for legacy bare ``global_uid`` platform ids (v2.8.4).
LEGACY_USER_IDENTITY_MIGRATED_KEY = "legacy_user_identity_migrated_v1"


def split_platform_account(value: Any) -> Optional[tuple[str, str]]:
    """Parse ``"platform_id:native_uid"`` -> ``(platform_id, native_uid)``.

    Legacy rows only stored the bare ``global_uid`` (e.g. a QQ number) without a
    platform prefix; those return ``None`` and are surfaced as legacy entries.
    """
    text = str(value or "").strip()
    if not text or ":" not in text:
        return None
    platform_id, native_uid = text.split(":", 1)
    platform_id = platform_id.strip()
    native_uid = native_uid.strip()
    if not platform_id or not native_uid or ":" in platform_id:
        return None
    return platform_id, native_uid


def make_platform_account(platform_id: Any, native_uid: Any) -> str:
    """Build the canonical ``platform_id:native_uid`` platform account string."""
    pid = str(platform_id or "").strip()
    uid = str(native_uid or "").strip()
    if not pid or not uid or ":" in pid:
        return ""
    return f"{pid}:{uid}"


def _platform_id_from_umo(umo: Any) -> str:
    """``"<platform_id>:<msg_type>:<session>"`` -> platform id (``""`` safe)."""
    text = str(umo or "").strip()
    if not text or ":" not in text:
        return ""
    return text.split(":", 1)[0].strip()


def _is_qualified_account(value: Any) -> bool:
    return split_platform_account(value) is not None


def _normalize_platform_ids(values: Any) -> list[str]:
    """Normalize console/user payloads into canonical platform account strings.

    Accepts ``["platform:uid", {"platform_id": ..., "native_uid": ...}, "platform:uid"]``
    and legacy bare ids. Qualified accounts win over a bare id with the same native
    uid (the bare legacy entry is dropped as a "shadow" duplicate).
    """
    if values is None:
        return []
    if isinstance(values, str):
        items: list[Any] = [values]
    elif isinstance(values, (list, tuple, set)):
        items = list(values)
    else:
        items = []
    accounts: list[str] = []
    seen: set[str] = set()
    for item in items:
        candidates: list[str] = []
        if isinstance(item, dict):
            platform_id = item.get("platform_id") or item.get("platform") or ""
            native_uid = (
                item.get("native_uid")
                or item.get("uid")
                or item.get("user_id")
                or item.get("global_uid")
                or ""
            )
            if platform_id and native_uid:
                candidates.append(make_platform_account(platform_id, native_uid))
            account = str(item.get("account") or "").strip()
            if account:
                candidates.append(account)
        else:
            text = str(item or "").strip()
            if text:
                candidates.append(text)
        for text in candidates:
            if not text:
                continue
            parsed = split_platform_account(text)
            canonical = f"{parsed[0]}:{parsed[1]}" if parsed else text
            if canonical in seen:
                continue
            seen.add(canonical)
            accounts.append(canonical)
    # Drop legacy bare ids that are already represented by a qualified account of
    # the same native uid (e.g. ["2975066889", "qq bot:2975066889"] -> the latter).
    qualified_uids = {
        parsed[1]
        for parsed in (split_platform_account(account) for account in accounts)
        if parsed is not None
    }
    return [
        account
        for account in accounts
        if _is_qualified_account(account) or account not in qualified_uids
    ]


def _dedupe_keep_order(values: Any) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in list(values or []):
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


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
        accounts: list[dict[str, Any]] = []
        legacy_ids: list[str] = []
        for raw in list(self.platform_ids):
            text = str(raw or "").strip()
            if not text:
                continue
            parsed = split_platform_account(text)
            if parsed is None:
                legacy_ids.append(text)
                accounts.append(
                    {
                        "account": text,
                        "platform_id": "",
                        "uid": text,
                        "legacy": True,
                    }
                )
            else:
                accounts.append(
                    {
                        "account": f"{parsed[0]}:{parsed[1]}",
                        "platform_id": parsed[0],
                        "uid": parsed[1],
                        "legacy": False,
                    }
                )
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
            # v2.8.4: parsed platform accounts + legacy (bare-id) diagnostics.
            "platform_accounts": accounts,
            "legacy_platform_ids": legacy_ids,
            "has_legacy_platform_ids": bool(legacy_ids),
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
    - ★ L3_CORE (3): 管理员 / OnlyYou（由平台 admins_id 自动确定，或 Pages「关系与群聊」页显式设置）
    - ◆ L2_IMPORTANT (2): 重要熟人（Pages 显式设置，或通过良性互动从 L1 自动晋升）
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
        """Resolve the auto-determined L3 tier (Pages / SQLite owns the rest).

        v2.8.2: the plugin's general config no longer carries per-user ID lists
        (``core_user_ids`` / ``important_user_ids``); user tiers are managed in the
        Pages「关系与群聊」page (``users`` table) and L3 is auto-determined only by
        the platform admin identity (``admins_id`` / event admin flag).
        """
        sid = str(sender_id).strip()
        if not sid:
            return None
        platform_admins = set(self.platform_bridge.get_platform_admins(umo=umo))
        account = make_platform_account(_platform_id_from_umo(umo), sid)
        if (
            sid in platform_admins
            or (account and account in platform_admins)
            or is_event_admin
        ):
            return UserTier.L3_CORE
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
            platform_id=_platform_id_from_umo(umo),
        )

    # ------------------------------------------------------------------
    # v2.8.4: canonical platform-account identity helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _pids_from_row(row: dict[str, Any]) -> list[str]:
        try:
            pids = json.loads(row.get("platform_ids_json") or "[]")
        except Exception:
            pids = []
        if not isinstance(pids, list):
            return []
        cleaned: list[str] = []
        for item in pids:
            text = str(item or "").strip()
            if text and text not in cleaned:
                cleaned.append(text)
        return cleaned

    def _profile_from_row(self, row: dict[str, Any]) -> UserProfile:
        raw_tier = int(row.get("tier", 1))
        tier = UserTier(raw_tier) if raw_tier in (1, 2, 3) else UserTier.L1_NORMAL
        global_uid = str(row.get("global_uid") or "").strip()
        pids = _normalize_platform_ids(self._pids_from_row(row))
        if not pids:
            pids = [global_uid]
        return UserProfile(
            global_uid=global_uid,
            display_name=str(row.get("display_name") or global_uid),
            tier=tier,
            interaction_count=int(row.get("interaction_count", 0)),
            affinity=float(row.get("affinity", 0.0)),
            platform_ids=pids,
            nickname=str(row.get("nickname") or ""),
            attitude=str(row.get("attitude") or ""),
        )

    @staticmethod
    def _heal_profile_identity(
        profile: UserProfile, native_uid: str, account: str
    ) -> bool:
        """Upgrade a legacy bare platform id to ``platform:native_uid`` in place.

        Called on every incoming message (the platform is known from the UMO), so
        historical users are repaired lazily without a destructive big-bang rewrite.
        """
        if profile is None or not native_uid or not account:
            return False
        before = list(profile.platform_ids)
        healed: list[str] = []
        for value in before:
            if split_platform_account(value) is None and str(value).strip() == native_uid:
                healed.append(account)
            else:
                healed.append(value)
        if account not in healed:
            healed.append(account)
        normalized = _normalize_platform_ids(healed)
        if normalized and normalized != before:
            profile.platform_ids = normalized
            return True
        return False

    async def _lookup_profile_by_identity(
        self, account: str, bare_uid: str
    ) -> Optional[UserProfile]:
        """Locate an existing profile by canonical account / legacy bare id."""
        for key in (account, bare_uid):
            if not key:
                continue
            cached = self._cache.get(key)
            if cached is None:
                continue
            # Never trust an unrelated cache entry that only shares a bare id.
            if key == bare_uid and (
                cached.global_uid != bare_uid and bare_uid not in cached.platform_ids
            ):
                continue
            return cached

        for key in (account, bare_uid):
            if not key:
                continue
            row = await self.db.fetchone(
                "SELECT * FROM users WHERE global_uid = ?", (key,)
            )
            if row:
                return self._profile_from_row(row)

        if not account and not bare_uid:
            return None
        rows = await self.db.fetchall("SELECT * FROM users")
        account_matches: list[dict[str, Any]] = []
        bare_matches: list[dict[str, Any]] = []
        for row in rows:
            pids = self._pids_from_row(row)
            if account and account in pids:
                account_matches.append(row)
            if bare_uid and bare_uid in pids:
                bare_matches.append(row)
        if account_matches:
            return self._profile_from_row(account_matches[0])
        if len(bare_matches) == 1:
            # Unique legacy owner -> adopt it and upgrade its id to qualified form.
            return self._profile_from_row(bare_matches[0])
        if len(bare_matches) > 1:
            exact = [
                row
                for row in bare_matches
                if str(row.get("global_uid") or "").strip() == bare_uid
            ]
            if len(exact) == 1:
                return self._profile_from_row(exact[0])
            logger.warning(
                "[OnlyYou] Ambiguous legacy user id '%s' (matches %d profiles); "
                "creating a platform-qualified profile instead of auto-merging",
                bare_uid,
                len(bare_matches),
            )
        return None

    async def get_or_create_user(
        self,
        user_id: str,
        display_name: str = "",
        umo: Optional[str] = None,
        is_event_admin: bool = False,
        bump_interaction: bool = False,
        platform_id: Optional[str] = None,
    ) -> UserProfile:
        """Get or create a UserProfile by platform identity.

        v2.8.4: the identity is the canonical ``platform_id:native_uid`` account
        (e.g. ``qq bot:2975066889``) built from the event's UMO prefix + sender id.
        Legacy profiles that only stored the bare sender id are healed lazily: the
        bare id is upgraded to the qualified account on the next message instead of
        being kept as an undeletable ghost entry.
        """
        uid = str(user_id).strip() or "anonymous"
        name = (display_name or uid).strip()
        pid = str(platform_id or "").strip() or _platform_id_from_umo(umo)
        account = make_platform_account(pid, uid) if pid else ""
        cfg_tier = self._get_configured_tier(uid, umo=umo, is_event_admin=is_event_admin)

        profile = await self._lookup_profile_by_identity(account, uid)
        if profile is not None:
            self._heal_profile_identity(profile, uid, account)
        else:
            new_uid = account or uid
            profile = UserProfile(
                global_uid=new_uid,
                display_name=name,
                tier=cfg_tier or UserTier.L1_NORMAL,
                interaction_count=0,
                affinity=0.0,
                platform_ids=[new_uid],
                nickname="",
                attitude="",
            )

        if name and name != profile.global_uid:
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

    async def normalize_user_identity(
        self, user_id: str, platform_id: str = ""
    ) -> UserProfile:
        """One-click repair of legacy bare platform ids (Pages「补全平台」).

        ``platform_id`` is the platform to use when the legacy entry has no prefix.
        """
        target = str(user_id or "").strip()
        if not target:
            raise ValueError("用户 ID 不能为空")
        pid_hint = str(platform_id or "").strip()
        profile = await self._lookup_profile_by_identity(target, target)
        if profile is None:
            profile = await self.get_or_create_user(target)
        repaired: list[str] = []
        for value in list(profile.platform_ids):
            parsed = split_platform_account(value)
            if parsed is not None:
                repaired.append(value)
                continue
            if not pid_hint:
                repaired.append(value)
                continue
            account = make_platform_account(pid_hint, value)
            repaired.append(account or value)
        if not pid_hint:
            # No explicit platform: infer from any single known platform evidence.
            evidence = await self._collect_platform_evidence()
            inferred: list[str] = []
            for value in list(profile.platform_ids):
                parsed = split_platform_account(value)
                if parsed is not None:
                    inferred.append(value)
                    continue
                candidates = evidence.get(value, set())
                if len(candidates) == 1:
                    account = make_platform_account(next(iter(candidates)), value)
                    inferred.append(account or value)
                else:
                    inferred.append(value)
            repaired = inferred
        normalized = _normalize_platform_ids(repaired)
        if normalized:
            profile.platform_ids = normalized
        await self.save_user(profile)
        return profile

    # ------------------------------------------------------------------
    # v2.8.4: legacy bare platform id migration (evidence based, non-destructive)
    # ------------------------------------------------------------------
    async def _collect_platform_evidence(self) -> dict[str, set[str]]:
        """Infer ``{native_uid: {platform_id}}`` from persisted UMO evidence.

        Sources: legacy ``umo_soul_bindings`` rows and the persisted runtime-state
        ``known_l3_umos`` UMOs — only **private-chat** UMOs whose session id is the
        user's native id can prove a platform. Group UMOs are ignored (their session
        id is a group id, never a user id).
        """
        evidence: dict[str, set[str]] = {}

        def _add_umo(umo: Any) -> None:
            text = str(umo or "").strip()
            parts = text.split(":")
            if len(parts) < 3:
                return
            platform_id = parts[0].strip()
            msg_type = parts[1].strip().lower()
            session_id = parts[2].strip()
            if not platform_id or not session_id:
                return
            is_private = ("friend" in msg_type) or ("private" in msg_type)
            if not is_private:
                return
            evidence.setdefault(session_id, set()).add(platform_id)

        try:
            rows = await self.db.fetchall(
                "SELECT umo FROM umo_soul_bindings WHERE umo IS NOT NULL"
            )
            for row in rows:
                _add_umo(row.get("umo"))
        except Exception as exc:  # noqa: BLE001 - migration must not crash startup
            logger.debug(f"[OnlyYou] read umo bindings for identity migration failed: {exc}")
        try:
            snapshot = await self.db.get_setting("runtime_state_v1", {}) or {}
            known = (
                snapshot.get("known_l3_umos")
                if isinstance(snapshot, dict)
                else None
            )
            if isinstance(known, dict):
                for umo in known.values():
                    _add_umo(umo)
            elif isinstance(known, list):
                for umo in known:
                    _add_umo(umo)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[OnlyYou] read runtime_state for identity migration failed: {exc}")
        return evidence

    async def migrate_legacy_user_identities(self) -> dict[str, int]:
        """Upgrade legacy bare ``platform_ids`` to ``platform_id:native_uid`` form.

        Only entries with **unambiguous** evidence (exactly one private-chat
        platform) are rewritten; ambiguous / unknown ones are kept for manual
        completion in the Pages console (never guessed destructively).
        Idempotent via ``plugin_settings["legacy_user_identity_migrated_v1"]``.
        """
        stats = {"users_updated": 0, "accounts_converted": 0, "ambiguous": 0}
        try:
            already = bool(
                await self.db.get_setting(LEGACY_USER_IDENTITY_MIGRATED_KEY, False)
            )
        except Exception:
            already = False
        if already:
            return stats

        evidence = await self._collect_platform_evidence()
        try:
            rows = await self.db.fetchall("SELECT * FROM users")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[OnlyYou] identity migration read users failed: {exc}")
            return stats

        for row in rows:
            profile = self._profile_from_row(row)
            rewritten: list[str] = []
            changed = False
            for value in list(profile.platform_ids):
                parsed = split_platform_account(value)
                if parsed is not None:
                    rewritten.append(value)
                    continue
                candidates = evidence.get(value, set())
                if len(candidates) == 1:
                    account = make_platform_account(next(iter(candidates)), value)
                    if account:
                        rewritten.append(account)
                        stats["accounts_converted"] += 1
                        changed = True
                        continue
                stats["ambiguous"] += 1
                rewritten.append(value)
            if not changed:
                continue
            normalized = _normalize_platform_ids(rewritten)
            if not normalized:
                continue
            profile.platform_ids = normalized
            await self.save_user(profile)
            stats["users_updated"] += 1
        try:
            await self.db.set_setting(LEGACY_USER_IDENTITY_MIGRATED_KEY, True)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[OnlyYou] persist identity migration flag failed: {exc}")
        if stats["accounts_converted"] or stats["ambiguous"]:
            logger.info(
                "[OnlyYou] Legacy user identity migration: %s",
                stats,
            )
        return stats

    async def set_user_tier(self, user_id: str, new_tier: UserTier, display_name: str = "") -> UserProfile:
        """Manually set a user's relationship tier (e.g. via `/onlyyou tier`)."""
        profile = await self.get_or_create_user(user_id=user_id, display_name=display_name)
        profile.tier = new_tier
        await self.save_user(profile)
        return profile

    async def save_user(self, profile: UserProfile) -> None:
        """Persist UserProfile to SQLite and memory cache.

        v2.8.4: platform ids are normalized to ``platform:native_uid`` (bare legacy
        ids shadowed by a qualified account are dropped) and the cache never lets a
        shared bare id silently overwrite another profile (cross-platform collision).
        """
        normalized = _normalize_platform_ids(profile.platform_ids)
        if normalized:
            profile.platform_ids = normalized
        elif profile.global_uid:
            profile.platform_ids = [profile.global_uid]
        self._cache[profile.global_uid] = profile
        for pid in profile.platform_ids:
            existing = self._cache.get(pid)
            if (
                existing is not None
                and existing is not profile
                and existing.global_uid != profile.global_uid
                and not _is_qualified_account(pid)
            ):
                logger.warning(
                    "[OnlyYou] legacy platform id '%s' already cached for user %s; "
                    "skip cache overwrite for user %s",
                    pid,
                    existing.global_uid,
                    profile.global_uid,
                )
                continue
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
        """List all known users (tiers live in SQLite and are managed by Pages)."""
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
            try:
                u = self._profile_from_row(r)
            except Exception as exc:  # noqa: BLE001 - one bad row must not break the list
                logger.warning(f"[OnlyYou] Failed to parse user row: {exc}")
                continue
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
            cleaned = _normalize_platform_ids(platform_ids)
            if not cleaned:
                raise ValueError(
                    "至少保留一个平台账号（格式 platform_id:native_uid，例如 qq bot:123456）"
                )
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

        merged_pids = _normalize_platform_ids(
            list(primary.platform_ids) + [s_id] + list(secondary.platform_ids)
        )

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
        for key, value in list(self._cache.items()):
            if value is not None and value.global_uid == uid:
                self._cache.pop(key, None)
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
