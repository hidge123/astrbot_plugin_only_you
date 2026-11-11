"""Restart-safe runtime-state snapshot store for astrbot_plugin_only_you (v2.8).

The plugin keeps a handful of **proactive-message bookkeeping** maps in memory:

- ``_shared_video_ids`` / ``_last_surf_share_ts`` — SURF sharing dedupe + cooldown;
- ``_mused_busy_keys`` / ``_mused_memory_ids`` — MEMORY_MUSE rotation (each settled
  BUSY_TASK result and each memory is proactively chatted about at most once);
- ``_last_idle_roam_ts`` / ``_last_group_lurk_ts`` / ``_last_private_catchup_ts`` —
  proactive-action cool-downs;
- ``_known_l3_umos`` — most-recently-seen ★L3 private-chat targets.

Before v2.8 these were pure memory state, so a plugin reload/restart could make the
bot re-share the same video, re-chat the same busy result and reset every cooldown.
:class:`RuntimeStateStore` snapshots them into the generic ``plugin_settings`` KV
table (``runtime_state_v1``) and restores them on startup.

Boundary (product contract, ``.team/V28_HARDENING_BRIEF.md`` §3.3): the **unread
backlog itself is intentionally not persisted** — a restart clears it. Only the
proactive-message bookkeeping above survives, so "重启后重复分享/重复搭话" is a bug
while "重启后未读队列清空" is the documented behaviour.

Everything here is defensive by contract: ``load()`` / ``save()`` never raise into
the caller and dirty / oversized / stale data silently degrades to the defaults.
"""

from __future__ import annotations

import math
import time
from typing import Any, Optional

try:
    from astrbot.api import logger
except ImportError:  # pragma: no cover - offline unit tests / no AstrBot container
    import logging

    logger = logging.getLogger("onlyyou.runtime_state")


#: ``plugin_settings`` key holding the whole snapshot (versioned, one row).
RUNTIME_STATE_SETTING_KEY = "runtime_state_v1"

#: Snapshot schema version.
RUNTIME_STATE_VERSION = 1

#: Per-Soul hard caps (FIFO: stale entries are evicted from the head).
MAX_SHARED_VIDEO_IDS_PER_SOUL = 200
MAX_MUSED_BUSY_KEYS_PER_SOUL = 100
MAX_MUSED_MEMORY_IDS_PER_SOUL = 30

#: ``known_l3_umos`` is capped MRU-style (oldest insertion evicted first).
MAX_KNOWN_L3_UMOS = 100

#: Cooldown timestamps older than this are dropped on load/save (7 days).
COOLDOWN_RETENTION_SECONDS = 7 * 24 * 60 * 60

#: Keys of the snapshot that hold ``{key: cooldown_timestamp}`` maps.
COOLDOWN_MAP_KEYS = (
    "last_surf_share_ts",
    "last_idle_roam_ts",
    "last_group_lurk_ts",
    "last_private_catchup_ts",
)

#: Keys of the snapshot that hold ``{soul_id: [str, ...]}`` maps.
STRING_LIST_MAP_KEYS = ("shared_video_ids", "mused_busy_keys")


def default_snapshot() -> dict[str, Any]:
    """Return a fresh, empty, correctly-typed snapshot (version 1)."""
    return {
        "version": RUNTIME_STATE_VERSION,
        "shared_video_ids": {},
        "last_surf_share_ts": {},
        "mused_busy_keys": {},
        "mused_memory_ids": {},
        "last_idle_roam_ts": {},
        "last_group_lurk_ts": {},
        "last_private_catchup_ts": {},
        "known_l3_umos": {},
    }


def _clean_key(value: Any) -> str:
    """Validate a dict key (soul ids / UMOs / platform user ids are strings)."""
    if not isinstance(value, str):
        return ""
    return value.strip()


def _trim_tail(values: list[Any], limit: int) -> list[Any]:
    """Keep the most recent *limit* entries of an append-ordered list."""
    if limit <= 0:
        return []
    if len(values) > limit:
        return values[-limit:]
    return values


def _normalize_string_list(raw: Any, limit: int) -> list[str]:
    """Defensive ``list[str]`` normalisation (FIFO trim, dedupe preserving order)."""
    if not isinstance(raw, (list, tuple, set)):
        # A bare string / number / None is dirty data -> the whole value is dropped.
        return []
    values = list(raw)
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in values:
        if not isinstance(item, str):
            continue
        value = item.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        cleaned.append(value)
    return _trim_tail(cleaned, limit)


def _normalize_memory_ids(raw: Any, limit: int) -> list[int]:
    """Defensive ``list[int]`` normalisation (drops bools / junk, FIFO trim)."""
    if not isinstance(raw, (list, tuple)):
        return []
    cleaned: list[int] = []
    for item in raw:
        if isinstance(item, bool):
            continue
        if isinstance(item, int):
            memory_id = item
        elif isinstance(item, float) and math.isfinite(item) and item.is_integer():
            memory_id = int(item)
        else:
            continue
        if memory_id > 0:
            cleaned.append(memory_id)
    return _trim_tail(cleaned, limit)


def _normalize_string_map(raw: Any, limit: int) -> dict[str, str]:
    """Defensive ``dict[str, str]`` normalisation (MRU cap, insertion order kept)."""
    if not isinstance(raw, dict):
        return {}
    cleaned: dict[str, str] = {}
    for key, value in raw.items():
        clean_key = _clean_key(key)
        if not clean_key or not isinstance(value, str):
            continue
        clean_value = value.strip()
        if not clean_value:
            continue
        # Re-insert so insertion order really reflects the latest update.
        cleaned.pop(clean_key, None)
        cleaned[clean_key] = clean_value
    if limit > 0 and len(cleaned) > limit:
        overflow = len(cleaned) - limit
        for stale_key in list(cleaned.keys())[:overflow]:
            cleaned.pop(stale_key, None)
    return cleaned


def _normalize_timestamp_map(
    raw: Any, now_ts: float, retention_seconds: float = COOLDOWN_RETENTION_SECONDS
) -> dict[str, float]:
    """Defensive ``dict[str, float]`` cooldown map: str keys, finite floats, 7d TTL.

    Values in the future are clamped to ``now`` (a clock skew must never freeze a
    cooldown forever), non-positive / malformed / stale entries are dropped.
    """
    if not isinstance(raw, dict):
        return {}
    cleaned: dict[str, float] = {}
    for key, value in raw.items():
        clean_key = _clean_key(key)
        if not clean_key:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        try:
            timestamp = float(value)
        except (TypeError, ValueError):  # pragma: no cover - both are numeric already
            continue
        if not math.isfinite(timestamp) or timestamp <= 0:
            continue
        if timestamp > now_ts:
            timestamp = now_ts
        if now_ts - timestamp > retention_seconds:
            continue
        cleaned[clean_key] = timestamp
    return cleaned


def normalize_snapshot(
    snapshot: Any, now: Optional[float] = None
) -> dict[str, Any]:
    """Return a defensive, fully-typed snapshot (never raises).

    Non-dict input, wrong value types, unknown keys and dirty entries all degrade
    to the documented defaults; every per-Soul collection is trimmed to its cap
    and stale cooldown timestamps (> 7 days) are dropped.
    """
    result = default_snapshot()
    if not isinstance(snapshot, dict):
        return result
    try:
        now_ts = float(now) if now is not None else time.time()
    except (TypeError, ValueError):
        now_ts = time.time()
    if not math.isfinite(now_ts) or now_ts <= 0:
        now_ts = time.time()

    # ---- SURF share / MEMORY_MUSE rotation: {soul_id: [str, ...]} ----
    for key in STRING_LIST_MAP_KEYS:
        raw = snapshot.get(key)
        if not isinstance(raw, dict):
            continue
        limit = (
            MAX_SHARED_VIDEO_IDS_PER_SOUL
            if key == "shared_video_ids"
            else MAX_MUSED_BUSY_KEYS_PER_SOUL
        )
        cleaned: dict[str, list[str]] = {}
        for soul_id, values in raw.items():
            clean_soul = _clean_key(soul_id)
            if not clean_soul:
                continue
            items = _normalize_string_list(values, limit)
            if items:
                cleaned[clean_soul] = items
        result[key] = cleaned

    # ---- MEMORY_MUSE memory ids: {soul_id: [int, ...]} (ring buffer) ----
    raw_ids = snapshot.get("mused_memory_ids")
    if isinstance(raw_ids, dict):
        cleaned_ids: dict[str, list[int]] = {}
        for soul_id, values in raw_ids.items():
            clean_soul = _clean_key(soul_id)
            if not clean_soul:
                continue
            items = _normalize_memory_ids(values, MAX_MUSED_MEMORY_IDS_PER_SOUL)
            if items:
                cleaned_ids[clean_soul] = items
        result["mused_memory_ids"] = cleaned_ids

    # ---- proactive-action cooldown timestamps: {key: float} ----
    for key in COOLDOWN_MAP_KEYS:
        result[key] = _normalize_timestamp_map(snapshot.get(key), now_ts)

    # ---- ★L3 private-chat targets: {global_uid: umo} (MRU capped) ----
    result["known_l3_umos"] = _normalize_string_map(
        snapshot.get("known_l3_umos"), MAX_KNOWN_L3_UMOS
    )

    # Version is owned by the schema, not by the stored payload.
    result["version"] = RUNTIME_STATE_VERSION
    return result


class RuntimeStateStore:
    """Snapshot :class:`~storage.db.DatabaseManager` ``plugin_settings`` persistence.

    ``db`` may be ``None`` (offline / degraded start): :meth:`load` then returns the
    default snapshot and :meth:`save` is a safe no-op returning ``False``. Both
    methods swallow their own exceptions by contract, so a broken DB can never
    break message handling / the 15-minute heartbeat.
    """

    def __init__(
        self,
        db: Any = None,
        setting_key: str = RUNTIME_STATE_SETTING_KEY,
    ) -> None:
        self.db = db
        self.setting_key = str(setting_key or RUNTIME_STATE_SETTING_KEY)

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    @staticmethod
    def normalize(snapshot: Any, now: Optional[float] = None) -> dict[str, Any]:
        """Static alias of :func:`normalize_snapshot` (test / caller convenience)."""
        return normalize_snapshot(snapshot, now=now)

    async def load(self) -> dict[str, Any]:
        """Load + normalize the persisted snapshot (defaults on any failure)."""
        if self.db is None:
            return default_snapshot()
        try:
            raw = await self.db.get_setting(self.setting_key, None)
        except Exception as exc:  # noqa: BLE001 - startup must never crash
            logger.warning(f"[OnlyYou] runtime state load failed: {exc}")
            return default_snapshot()
        try:
            return normalize_snapshot(raw)
        except Exception as exc:  # noqa: BLE001 - normalize is defensive but belt+suspenders
            logger.warning(f"[OnlyYou] runtime state normalize failed: {exc}")
            return default_snapshot()

    async def save(self, snapshot: Any) -> bool:
        """Persist a normalized snapshot; returns ``True`` only when written."""
        if self.db is None:
            return False
        try:
            payload = normalize_snapshot(snapshot)
            await self.db.set_setting(self.setting_key, payload)
            return True
        except Exception as exc:  # noqa: BLE001 - persistence is best-effort
            logger.warning(f"[OnlyYou] runtime state save failed: {exc}")
            return False
