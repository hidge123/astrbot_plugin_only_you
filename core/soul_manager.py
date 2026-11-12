"""Soul (character card) manager: routing, native Persona import & Roguelite deck seeding.

Key concepts after the 2026 refactor:

- A ``SoulProfile`` owns a nested ``card`` dict (see ``core/character_card.py``).
  The four legacy flat fields (``name`` / ``archetype`` / ``speaking_style`` /
  ``surfing_preferences``) plus ``native_persona_prompt`` remain available as
  read/write properties backed by the card, so every existing caller keeps working.
- ``resolve_soul(umo)`` routing is now:
  ``UMO 绑定 -> plugin_settings.default_soul_id -> 最近更新的角色 -> 创建默认角色``.
  It no longer reads ``default_personality`` nor matches ``persona_hash`` and it
  never seeds cards via the LLM on the chat path.
- AstrBot native Personas are only an **import source**: ``list_native_personas``
  reads ``persona_manager.personas_v3`` first, ``list_native_prompts`` exposes the
  raw prompt strings for the Persona-section takeover done by ``ContextBudgetManager``.
- ``generate_card`` asks the **main** model for a whole-card / single-section draft
  (never persisted); ``import_native_persona`` writes the raw native prompt into
  ``card["extra"]["native_persona_prompt"]`` and optionally asks the main model for
  a full AI card, while the four-type action card pool is generated with the
  **decision** model (encounter cards are no longer generated).
"""

import hashlib
import json
import os
import re
import time
import uuid
from typing import TYPE_CHECKING, Any, Optional

from .character_card import (
    CARD_SCHEMA,
    CARD_SECTION_IDS,
    empty_card,
    migrate_legacy,
    normalize_card,
    render_card_summary,
)
from .model_hub import DEFAULT_MODEL_CONFIG, normalize_model_config
from .roguelite_deck import StateEventCard
from .state_machine import ActionType
from ..prompts.templates import (
    ACTION_DECK_GENERATE_PROMPT,
    CARD_AND_DECK_SEED_PROMPT,
    CHARACTER_CARD_GENERATE_PROMPT,
    CHARACTER_CARD_SECTION_PROMPT,
)
from ..storage.db import DatabaseManager

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids a hard runtime dependency
    from .model_hub import ModelHub

try:
    from astrbot.api import logger
except ImportError:
    import logging

    logger = logging.getLogger("onlyyou.soul_manager")


_CARD_SECTION_BY_ID: dict[str, dict[str, Any]] = {s["id"]: s for s in CARD_SCHEMA}

DEFAULT_SOUL_ID = "default_soul"
_SETTING_DEFAULT_SOUL_ID = "default_soul_id"
_SETTING_LEGACY_UMO_MIGRATED = "legacy_umo_bindings_migrated"

#: Default per-Soul plugin settings (v2.8).  ``surfing_enabled`` is the per-Soul
#: master switch; ``surfing_sources`` (v2.8.3) is the per-Soul background video
#: source preference, defaulting to both when the key is absent.
DEFAULT_SOUL_SETTINGS: dict[str, Any] = {"surfing_enabled": True}

#: Four-action card ratio enforced by the AI deck prompt / completion fallback
#: (SLEEP 2 / BUSY_TASK 3 / SURF 2 / IDLE_ROAM 2 = 9 cards).
ACTION_DECK_RATIO: tuple[tuple[ActionType, int], ...] = (
    (ActionType.SLEEP, 2),
    (ActionType.BUSY_TASK, 3),
    (ActionType.SURF, 2),
    (ActionType.IDLE_ROAM, 2),
)

#: Supported background video sources (Pages「冲浪」page owns the per-Soul choice).
SURFING_SOURCE_PLATFORMS: tuple[str, ...] = ("bilibili", "douyin")
DEFAULT_SURFING_SOURCES: list[str] = list(SURFING_SOURCE_PLATFORMS)

#: Characters that are illegal in a cross-platform file name (plus control chars).
_UNSAFE_FILENAME_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_MAX_PLATFORM_ID_LEN = 64

_TAGS_SPLIT_CHARS = ",，、/;；|\n\r"

_DEFAULT_SURFING_PREFERENCES = [
    "搞笑整活",
    "萌宠日常",
    "游戏实况",
    "美食探店",
]

# Valid card['rhythm']['chronotype'] values; anything else normalizes to "normal".
_VALID_CHRONOTYPES = ("early", "normal", "night_owl")

# Best-effort occupation / hobby hints used to tailor the offline fallback card pool.
_PERSONA_KEYWORD_HINTS: tuple[str, ...] = (
    "画师",
    "插画",
    "原画",
    "设计师",
    "程序员",
    "代码",
    "开发",
    "作家",
    "写手",
    "小说",
    "编辑",
    "学生",
    "上课",
    "考研",
    "读研",
    "老师",
    "医生",
    "护士",
    "药师",
    "炼丹",
    "修士",
    "剑客",
    "侠客",
    "魔法",
    "主播",
    "UP主",
    "歌手",
    "音乐",
    "钢琴",
    "吉他",
    "舞者",
    "摄影",
    "咖啡",
    "店主",
    "料理",
    "厨师",
    "烘焙",
    "游戏",
    "电竞",
    "铲屎官",
    "旅行",
    "健身",
    "跑步",
    "追星",
    "二次元",
    "coser",
    "会计",
    "律师",
    "记者",
    "翻译",
)


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _clean_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = [value]
        for ch in _TAGS_SPLIT_CHARS:
            items = [piece for item in items for piece in item.split(ch)]
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = [value]
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = _clean_text(item)
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def normalize_surfing_sources(value: Any) -> list[str]:
    """Normalize a per-Soul background video source list (v2.8.3).

    Accepts a list / tuple / set / comma-separated string, drops unknown values,
    dedupes and returns them in canonical order (``bilibili`` first, then
    ``douyin``).  An explicitly empty list stays empty ("no background source").
    """
    if value is None:
        return []
    if isinstance(value, str):
        items: list[Any] = []
        for piece in re.split(r"[,，、/;；|\s]+", value):
            items.append(piece)
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = [value]
    selected = {_clean_text(item).lower() for item in items}
    return [p for p in SURFING_SOURCE_PLATFORMS if p in selected]


def _normalize_soul_settings(value: Any) -> dict[str, Any]:
    """Normalize ``SoulProfile.settings`` (known keys coerced, unknown keys kept)."""
    settings: dict[str, Any] = dict(DEFAULT_SOUL_SETTINGS)
    if isinstance(value, dict):
        for key, item in value.items():
            text_key = _clean_text(key)
            if text_key:
                settings[text_key] = item
    surfing = settings.get("surfing_enabled", True)
    if isinstance(surfing, str):
        settings["surfing_enabled"] = surfing.strip().lower() not in (
            "0",
            "false",
            "off",
            "no",
        )
    else:
        settings["surfing_enabled"] = bool(surfing)
    # v2.8.3: normalize the per-Soul background video sources only when the key
    # exists, so existing Soul JSON files keep their compact settings shape.
    if "surfing_sources" in settings:
        settings["surfing_sources"] = normalize_surfing_sources(
            settings.get("surfing_sources")
        )
    return settings


def _normalize_bound_platforms(value: Any) -> list[str]:
    """Normalize a Soul's bound bot-instance ids (no UMO / no colon)."""
    if value is None:
        return []
    if isinstance(value, str):
        items: list[Any] = [value]
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = []
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, str):
            continue
        text = _clean_text(item)
        if not text or ":" in text or len(text) > _MAX_PLATFORM_ID_LEN:
            continue
        if text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _coerce_timestamp(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _soul_filename(soul_id: str) -> str:
    """Map a Soul ID onto a safe ``<name>.json`` file name (collision resistant)."""
    sid = _clean_text(soul_id) or DEFAULT_SOUL_ID
    safe = _UNSAFE_FILENAME_RE.sub("_", sid).strip(" .") or "soul"
    if safe != sid:
        digest = hashlib.sha1(sid.encode("utf-8")).hexdigest()[:8]
        safe = f"{safe[:40]}__{digest}"
    return f"{safe}.json"


def _coerce_revision(value: Any) -> int:
    """Coerce a persisted Soul ``revision`` counter into a non-negative int."""
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _soul_content_hash(profile: "SoulProfile") -> str:
    """Hash the *content* of a SoulProfile, ignoring volatile bookkeeping fields.

    Used by the v2.8 dual-write conflict resolver: when the ``souls/*.json`` file
    and the SQLite index share the same ``revision`` but disagree on content, a
    differing hash means the JSON file was edited by hand (the single source of
    truth) and must win instead of being overwritten by a stale index row.
    """
    payload = profile.to_dict()
    payload.pop("updated_at", None)
    payload.pop("revision", None)
    raw = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _is_empty_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, dict, set)):
        return len(value) == 0
    return False


def _compact_title(text: Any, limit: int = 18) -> str:
    """Return a whitespace-trimmed card title capped at ``limit`` characters."""
    clean = _clean_text(text)
    return clean if len(clean) <= limit else clean[:limit]


def _extract_persona_keywords(persona_prompt: Any) -> list[str]:
    """Best-effort extraction of occupation / hobby keywords from raw Persona text."""
    text = _clean_text(persona_prompt)
    if not text:
        return []
    hits: list[tuple[int, str]] = []
    for keyword in _PERSONA_KEYWORD_HINTS:
        index = text.find(keyword)
        if index >= 0:
            hits.append((index, keyword))
    hits.sort(key=lambda item: item[0])
    keywords: list[str] = []
    for _, keyword in hits:
        if keyword not in keywords:
            keywords.append(keyword)
    return keywords[:3]


def _parse_state_event_cards(items: Any) -> list[StateEventCard]:
    """Parse persisted / imported action cards into ``StateEventCard`` objects.

    v2.4 cards had no ``action_type`` and were migrated by guessing from
    ``state + title`` keywords; that compatibility path is gone, so any legacy
    dict without a valid ``action_type`` is dropped instead of being silently
    coerced into a wrong action archetype.
    """
    cards: list[StateEventCard] = []
    for item in items or []:
        try:
            if isinstance(item, StateEventCard):
                cards.append(item)
                continue
            if not isinstance(item, dict):
                continue
            if ActionType.coerce(item.get("action_type"), None) is None:
                logger.warning(
                    "[OnlyYou] Dropped legacy action card without action_type: "
                    f"{item.get('title', '')!r}"
                )
                continue
            cards.append(StateEventCard.from_dict(item))
        except Exception as e:  # noqa: BLE001 - never crash on a single bad card
            logger.warning(f"[OnlyYou] Failed to parse state event card: {e}")
    return cards


class SoulProfile:
    """A character (soul): detailed ``card`` + Roguelite card pool data.

    ``card`` is the single source of truth. The legacy flat attributes are
    compatibility properties so that the existing WebUI / API / main.py code
    does not need to change.
    """

    def __init__(
        self,
        soul_id: str = DEFAULT_SOUL_ID,
        card: Optional[dict[str, Any]] = None,
        state_event_cards: Optional[list[Any]] = None,
        model_config: Optional[dict[str, Any]] = None,
        bound_platforms: Optional[list[str]] = None,
        settings: Optional[dict[str, Any]] = None,
        updated_at: Optional[int] = None,
        revision: Optional[int] = None,
        **legacy: Any,
    ) -> None:
        self.soul_id: str = _clean_text(soul_id) or DEFAULT_SOUL_ID
        # v2.8: each Soul aggregates its own model config / bot bindings / settings
        # (persisted as an independent ``souls/<soul_id>.json`` file).
        self.model_config: dict[str, Any] = normalize_model_config(model_config)
        self.bound_platforms: list[str] = _normalize_bound_platforms(bound_platforms)
        self.settings: dict[str, Any] = _normalize_soul_settings(settings)
        self.updated_at: int = _coerce_timestamp(updated_at)
        # v2.8 hardening: monotonically increasing version counter that survives
        # clock rollbacks; the JSON file and the SQLite index are reconciled by it.
        self.revision: int = _coerce_revision(revision)
        base: dict[str, Any] = normalize_card(card) if isinstance(card, dict) else empty_card()

        # Legacy keyword arguments (name=..., archetype=..., ...) are folded into
        # the card so old call sites such as `SoulProfile(name=..., archetype=...)`
        # keep working during the transition.
        legacy_map = {
            "name": ("identity", "name"),
            "archetype": ("identity", "tagline"),
            "speaking_style": ("speech", "speaking_style"),
            "native_persona_prompt": ("extra", "native_persona_prompt"),
        }
        for legacy_key, (section_id, field_key) in legacy_map.items():
            if legacy.get(legacy_key) is not None:
                base[section_id][field_key] = legacy[legacy_key]
        if legacy.get("surfing_preferences") is not None:
            base["interests"]["surfing_preferences"] = legacy["surfing_preferences"]
        self.card: dict[str, Any] = normalize_card(base)

        self.state_event_cards: list[StateEventCard] = _parse_state_event_cards(
            state_event_cards
        )

    # ---- Legacy flat-field compatibility properties (backed by the card) ----

    @property
    def name(self) -> str:
        return _clean_text((self.card.get("identity") or {}).get("name"))

    @name.setter
    def name(self, value: Any) -> None:
        self.card["identity"]["name"] = value
        self.card = normalize_card(self.card)

    @property
    def archetype(self) -> str:
        return _clean_text((self.card.get("identity") or {}).get("tagline"))

    @archetype.setter
    def archetype(self, value: Any) -> None:
        self.card["identity"]["tagline"] = value
        self.card = normalize_card(self.card)

    @property
    def speaking_style(self) -> str:
        return _clean_text((self.card.get("speech") or {}).get("speaking_style"))

    @speaking_style.setter
    def speaking_style(self, value: Any) -> None:
        self.card["speech"]["speaking_style"] = value
        self.card = normalize_card(self.card)

    @property
    def surfing_preferences(self) -> list[str]:
        return _clean_list((self.card.get("interests") or {}).get("surfing_preferences"))

    @surfing_preferences.setter
    def surfing_preferences(self, value: Any) -> None:
        self.card["interests"]["surfing_preferences"] = value
        self.card = normalize_card(self.card)

    @property
    def chronotype(self) -> str:
        """Rhythm chronotype: ``early`` / ``normal`` / ``night_owl`` (read-only).

        Reads ``card["rhythm"]["chronotype"]``; empty or invalid values fall back
        to ``"normal"`` so schedule generation always has a usable curve.
        """
        raw = _clean_text((self.card.get("rhythm") or {}).get("chronotype")).lower()
        return raw if raw in _VALID_CHRONOTYPES else "normal"

    @property
    def surfing_enabled(self) -> bool:
        """Per-Soul cyber-surfing switch (``settings.surfing_enabled``, default True)."""
        return bool(self.settings.get("surfing_enabled", True))

    @surfing_enabled.setter
    def surfing_enabled(self, value: Any) -> None:
        self.settings = _normalize_soul_settings(
            {**dict(self.settings or {}), "surfing_enabled": value}
        )

    @property
    def surfing_sources(self) -> list[str]:
        """Per-Soul background video sources (``settings.surfing_sources``).

        v2.8.3: the Pages「冲浪」page owns this preference.  When the key is
        absent the Soul keeps both sources enabled (backward compatible with
        v2.8.2 Soul JSON files); an explicitly stored empty list means "no
        background source" (manual surfing still works).
        """
        settings = self.settings if isinstance(self.settings, dict) else {}
        if "surfing_sources" not in settings:
            return list(DEFAULT_SURFING_SOURCES)
        return normalize_surfing_sources(settings.get("surfing_sources"))

    @surfing_sources.setter
    def surfing_sources(self, value: Any) -> None:
        self.settings = _normalize_soul_settings(
            {
                **dict(self.settings or {}),
                "surfing_sources": normalize_surfing_sources(value),
            }
        )

    @property
    def tts_config(self) -> dict[str, Any]:
        """This Soul's normalized TTS config section (copy)."""
        return dict((self.model_config or {}).get("tts") or DEFAULT_MODEL_CONFIG["tts"])

    @property
    def native_persona_prompt(self) -> str:
        return _clean_text((self.card.get("extra") or {}).get("native_persona_prompt"))

    @native_persona_prompt.setter
    def native_persona_prompt(self, value: Any) -> None:
        self.card["extra"]["native_persona_prompt"] = value
        self.card = normalize_card(self.card)

    # ---- Serialization ----

    def to_dict(self) -> dict[str, Any]:
        """Serialize, emitting **both** the new ``card`` and the legacy flat fields.

        v2.8 adds the per-Soul ``model_config`` / ``bound_platforms`` / ``settings``
        / ``updated_at`` keys; the ``souls/<soul_id>.json`` file is this exact dict.
        """
        return {
            "soul_id": self.soul_id,
            "card": normalize_card(self.card),
            "name": self.name,
            "archetype": self.archetype,
            "speaking_style": self.speaking_style,
            "native_persona_prompt": self.native_persona_prompt,
            "surfing_preferences": list(self.surfing_preferences),
            "state_event_cards": [card.to_dict() for card in self.state_event_cards],
            "model_config": normalize_model_config(self.model_config),
            "bound_platforms": _normalize_bound_platforms(self.bound_platforms),
            "settings": _normalize_soul_settings(self.settings),
            "updated_at": _coerce_timestamp(self.updated_at),
            "revision": _coerce_revision(self.revision),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SoulProfile":
        """Deserialize: prefer ``card``; otherwise migrate the old flat profile."""
        data = data if isinstance(data, dict) else {}
        raw_card = data.get("card")
        if isinstance(raw_card, dict) and raw_card:
            card = normalize_card(raw_card)
        elif any(isinstance(data.get(sid), dict) for sid in CARD_SECTION_IDS):
            # Tolerate a bare card dict being passed straight in.
            card = normalize_card(data)
        else:
            card = migrate_legacy(data)

        cards = _parse_state_event_cards(data.get("state_event_cards"))
        # v2.6: armor is a fixed per-action tier now; normalize cards persisted by
        # an older version on load so cards/list & the UI never surface stale values.
        for state_card in cards:
            state_card.armor = state_card.action.fixed_armor

        return cls(
            soul_id=_clean_text(data.get("soul_id")) or DEFAULT_SOUL_ID,
            card=card,
            state_event_cards=cards,
            model_config=data.get("model_config"),
            bound_platforms=data.get("bound_platforms"),
            settings=data.get("settings"),
            updated_at=data.get("updated_at"),
            revision=data.get("revision"),
        )


class SoulManager:
    """Manages SoulProfile routing, persistence, native Persona import and AI cards."""

    def __init__(self, context: Any, db: DatabaseManager, model_hub: "ModelHub"):
        self.context = context
        self.db = db
        self.model_hub = model_hub
        self._cache: dict[str, tuple[str, SoulProfile]] = {}
        self._umo_bindings: dict[str, str] = {}
        self._bindings_loaded: bool = False
        self._default_soul_id_cache: Optional[str] = None
        self._native_prompts_cache: Optional[list[str]] = None
        # v2.8: ``souls/<safe>.json`` is the single source of truth; SQLite ``souls``
        # stays as an index for cascade updates / legacy migration.
        self.souls_dir: str = self._derive_souls_dir()
        self._platform_bindings_cache: Optional[dict[str, str]] = None
        # v2.8 hardening: last JSON-file write error ("" == healthy). Surfaced by the
        # ``souls/save`` Web API so a SQLite-only degradation is never silent again.
        self.last_file_write_error: str = ""
        self._clock_rollback_warned: set[str] = set()

    # ==================================================================
    # v2.8: independent per-Soul JSON files
    # ==================================================================
    def _derive_souls_dir(self) -> str:
        """``<data dir>/souls`` derived from the SQLite path (never the cwd)."""
        db_path = str(getattr(self.db, "db_path", "") or "").strip()
        if db_path:
            return os.path.join(os.path.dirname(os.path.abspath(db_path)), "souls")
        return os.path.join(os.getcwd(), "data", "plugin_data", "astrbot_plugin_only_you", "souls")

    def _ensure_souls_dir(self) -> bool:
        try:
            os.makedirs(self.souls_dir, exist_ok=True)
            return True
        except Exception as exc:  # noqa: BLE001 - degrade to SQLite-only mode
            self.last_file_write_error = f"无法创建 souls 目录 {self.souls_dir}: {exc}"
            logger.error(f"[OnlyYou] create souls dir failed: {exc}")
            return False

    def soul_file_path(self, soul_id: str) -> str:
        """Absolute path of a Soul's JSON file."""
        return os.path.join(self.souls_dir, _soul_filename(soul_id))

    def _write_soul_file(self, profile: SoulProfile) -> bool:
        """Atomically persist one Soul JSON file (unique tmp + fsync + ``os.replace``).

        Returns ``True`` on success and records ``last_file_write_error`` otherwise,
        so callers (``souls/save`` Web API) can surface a degraded persistence state
        instead of silently falling back to SQLite-only.
        """
        path = self.soul_file_path(profile.soul_id)
        if not self._ensure_souls_dir():
            return False
        try:
            payload = json.dumps(profile.to_dict(), ensure_ascii=False, indent=2)
        except Exception as exc:  # noqa: BLE001 - non-serialisable payload
            self.last_file_write_error = f"序列化 Soul JSON 失败: {exc}"
            logger.error(
                f"[OnlyYou] serialize soul file failed ({profile.soul_id}): {exc}"
            )
            return False

        last_error = ""
        for _attempt in range(2):
            tmp_path = (
                f"{path}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
            )
            try:
                with open(tmp_path, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_path, path)
                # Best-effort directory fsync (not supported on every platform).
                try:
                    dir_fd = os.open(self.souls_dir, os.O_RDONLY)
                    try:
                        os.fsync(dir_fd)
                    finally:
                        os.close(dir_fd)
                except OSError:
                    pass
                self.last_file_write_error = ""
                return True
            except Exception as exc:  # noqa: BLE001 - retry once, then degrade loudly
                last_error = str(exc)
                try:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                except OSError:
                    pass
                logger.warning(
                    f"[OnlyYou] write soul file attempt failed ({profile.soul_id}): {exc}"
                )
        self.last_file_write_error = last_error or "未知错误"
        logger.error(
            "[OnlyYou] write soul file failed after retries "
            f"({profile.soul_id}, path={path}): {last_error}; "
            "falling back to the SQLite index (the next read will self-heal)."
        )
        return False

    def _read_soul_file(self, path: str) -> Optional[SoulProfile]:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if not isinstance(data, dict):
                logger.warning(
                    f"[OnlyYou] soul file {os.path.basename(path)} is not a JSON object"
                )
                return None
            profile = SoulProfile.from_dict(data)
            profile.updated_at = _coerce_timestamp(data.get("updated_at"))
            profile.revision = _coerce_revision(data.get("revision"))
            return profile
        except Exception as exc:  # noqa: BLE001 - skip a corrupt file, keep the rest
            logger.warning(
                f"[OnlyYou] read soul file failed ({os.path.basename(path)}): {exc}"
            )
            return None

    @staticmethod
    def _profile_from_index_row(soul_id: str, row: dict[str, Any]) -> Optional[SoulProfile]:
        """Rebuild a SoulProfile from one SQLite index row (tolerant parse)."""
        raw = row.get("profile_json") or "{}"
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            return None
        profile = SoulProfile.from_dict(data if isinstance(data, dict) else {})
        profile.soul_id = soul_id
        profile.updated_at = _coerce_timestamp(row.get("updated_at"))
        profile.revision = _coerce_revision(row.get("revision"))
        return profile

    async def _fetch_soul_index_row(self, soul_id: str) -> Optional[dict[str, Any]]:
        try:
            return await self.db.fetchone(
                "SELECT persona_hash, profile_json, updated_at, revision "
                "FROM souls WHERE soul_id = ?",
                (soul_id,),
            )
        except Exception:
            # A pre-migration database may not have the ``revision`` column yet.
            try:
                return await self.db.fetchone(
                    "SELECT persona_hash, profile_json, updated_at "
                    "FROM souls WHERE soul_id = ?",
                    (soul_id,),
                )
            except Exception as exc:  # noqa: BLE001 - DB may be closed / uninitialized
                logger.debug(f"[OnlyYou] read soul index failed ({soul_id}): {exc}")
                return None

    async def _fetch_all_soul_index_rows(self) -> list[dict[str, Any]]:
        """Return every ``souls`` index row (tolerates a pre-migration schema)."""
        try:
            return await self.db.fetchall(
                "SELECT soul_id, persona_hash, profile_json, updated_at, revision "
                "FROM souls"
            )
        except Exception:
            try:
                return await self.db.fetchall(
                    "SELECT soul_id, persona_hash, profile_json, updated_at FROM souls"
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[OnlyYou] list souls from SQLite failed: {exc}")
                return []

    @staticmethod
    def _resolve_soul_conflict(
        file_profile: SoulProfile, db_profile: SoulProfile
    ) -> tuple[SoulProfile, str]:
        """Return ``(winner, reason)`` when the JSON file and the index disagree.

        v2.8 hardening rules (clock-rollback safe, manual-edit friendly):

        - ``revision`` is the primary key: the higher revision always wins, so a
          wall-clock rollback can never resurrect an older copy;
        - equal revision → compare a content hash (ignores ``updated_at/revision``):
          identical = already synced (JSON wins as the source of truth), differing =
          the JSON file was edited by hand → JSON wins and the index is rebuilt;
        - a higher index revision only happens when the previous JSON write failed →
          the index holds the newer content → index wins and the file is rewritten.
        """
        file_rev = _coerce_revision(getattr(file_profile, "revision", 0))
        db_rev = _coerce_revision(getattr(db_profile, "revision", 0))
        if db_rev > file_rev:
            return db_profile, "index-newer"
        if file_rev > db_rev:
            return file_profile, "file-newer"
        if _soul_content_hash(file_profile) != _soul_content_hash(db_profile):
            return file_profile, "file-edited"
        return file_profile, "synced"

    async def _sync_soul_index_safe(
        self, profile: SoulProfile, persona_hash: str = "file"
    ) -> bool:
        """Best-effort index upsert; never raises (JSON file remains the truth)."""
        try:
            await self._upsert_soul_index(profile, persona_hash=persona_hash)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"[OnlyYou] sync soul index failed ({profile.soul_id}): {exc}"
            )
            return False

    def _invalidate_platform_cache(self) -> None:
        self._platform_bindings_cache = None

    async def _upsert_soul_index(self, profile: SoulProfile, persona_hash: str = "file") -> None:
        """Keep the SQLite ``souls`` index in sync with the JSON file."""
        await self.db.execute(
            """
            INSERT INTO souls (soul_id, persona_hash, profile_json, updated_at, revision)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(soul_id) DO UPDATE SET
                persona_hash = excluded.persona_hash,
                profile_json = excluded.profile_json,
                updated_at = excluded.updated_at,
                revision = excluded.revision
            """,
            (
                profile.soul_id,
                persona_hash or "file",
                json.dumps(profile.to_dict(), ensure_ascii=False),
                _coerce_timestamp(profile.updated_at) or int(time.time()),
                _coerce_revision(profile.revision),
            ),
        )

    def get_cached_soul(self, soul_id: str) -> Optional[SoulProfile]:
        """Return a Soul from the in-memory cache only (sync; used by ModelHub)."""
        sid = _clean_text(soul_id)
        if not sid:
            return None
        item = self._cache.get(sid)
        return item[1] if item else None

    def peek_default_soul(self) -> Optional[SoulProfile]:
        """Return the default Soul from cache synchronously (newest as fallback)."""
        sid = _clean_text(self._default_soul_id_cache)
        if sid:
            soul = self.get_cached_soul(sid)
            if soul is not None:
                return soul
        souls = [profile for (_hash, profile) in self._cache.values() if profile is not None]
        if not souls:
            return None
        return max(souls, key=lambda item: _coerce_timestamp(getattr(item, "updated_at", 0)))

    # ==================================================================
    # UMO routing bindings
    # ==================================================================
    async def _ensure_bindings_loaded(self) -> None:
        if self._bindings_loaded:
            return
        try:
            rows = await self.db.fetchall("SELECT umo, soul_id FROM umo_soul_bindings")
            for r in rows:
                if r.get("umo") and r.get("soul_id"):
                    self._umo_bindings[str(r["umo"])] = str(r["soul_id"])
            self._bindings_loaded = True
        except Exception:
            pass

    async def get_umo_bindings(self) -> dict[str, str]:
        """Return all explicit `umo -> soul_id` routing bindings."""
        await self._ensure_bindings_loaded()
        return dict(self._umo_bindings)

    async def set_umo_binding(self, umo: str, soul_id: Optional[str]) -> None:
        """Bind or unbind a specific session `umo` to a `soul_id`."""
        await self._ensure_bindings_loaded()
        clean_umo = (umo or "").strip()
        if not clean_umo:
            return
        clean_soul = (soul_id or "").strip()
        if not clean_soul:
            self._umo_bindings.pop(clean_umo, None)
            await self.db.execute("DELETE FROM umo_soul_bindings WHERE umo = ?", (clean_umo,))
        else:
            self._umo_bindings[clean_umo] = clean_soul
            await self.db.execute(
                """
                INSERT INTO umo_soul_bindings (umo, soul_id, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(umo) DO UPDATE SET
                    soul_id = excluded.soul_id,
                    updated_at = excluded.updated_at
                """,
                (clean_umo, clean_soul, int(time.time())),
            )

    # ==================================================================
    # Plugin-level default soul (plugin_settings.default_soul_id)
    # ==================================================================
    async def get_default_soul_id(self) -> str:
        """Read the plugin-level default soul id (kept in ``plugin_settings``)."""
        if self._default_soul_id_cache:
            return self._default_soul_id_cache
        try:
            value = await self.db.get_setting(_SETTING_DEFAULT_SOUL_ID, "")
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[OnlyYou] Failed to read default_soul_id: {e}")
            value = ""
        soul_id = _clean_text(value)
        if soul_id:
            self._default_soul_id_cache = soul_id
        return soul_id

    async def set_default_soul_id(self, soul_id: str) -> None:
        """Persist the plugin-level default soul id."""
        clean = _clean_text(soul_id)
        self._default_soul_id_cache = clean
        try:
            await self.db.set_setting(_SETTING_DEFAULT_SOUL_ID, clean)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[OnlyYou] Failed to save default_soul_id: {e}")

    # ==================================================================
    # Native AstrBot Persona discovery (import source only)
    # ==================================================================
    async def _collect_native_personas(self) -> list[dict[str, str]]:
        """Collect native personas as ``[{persona_name, prompt}]``.

        Priority: ``persona_manager.personas_v3`` (AstrBot v4 dict list) ->
        legacy ``persona_manager.personas`` -> ``context.get_config().persona``.
        """
        result: list[dict[str, str]] = []
        seen: set[str] = set()

        def _add(name: Any, prompt: Any) -> None:
            pid = _clean_text(name)
            if not pid or pid in seen:
                return
            seen.add(pid)
            result.append({"persona_name": pid, "prompt": _clean_text(prompt)})

        if self.context is not None:
            pm = getattr(self.context, "persona_manager", None)
            if pm is not None:
                # 1) AstrBot v4: personas_v3 is a list of dicts (name/prompt/begin_dialogs)
                for p in getattr(pm, "personas_v3", None) or []:
                    if isinstance(p, dict):
                        _add(p.get("name"), p.get("prompt"))
                    else:
                        _add(getattr(p, "name", None), getattr(p, "prompt", None))

                # 2) Legacy in-memory list (Persona ORM objects or dicts)
                legacy_personas = getattr(pm, "personas", None) or []
                if not legacy_personas and callable(getattr(pm, "get_all_personas", None)):
                    try:
                        legacy_personas = await pm.get_all_personas() or []
                    except Exception as e:  # noqa: BLE001
                        logger.debug(f"[OnlyYou] get_all_personas failed: {e}")
                        legacy_personas = []
                for p in legacy_personas:
                    if isinstance(p, dict):
                        _add(
                            p.get("name") or p.get("persona_id"),
                            p.get("prompt") or p.get("system_prompt"),
                        )
                    else:
                        _add(
                            getattr(p, "persona_id", None) or getattr(p, "name", None),
                            getattr(p, "system_prompt", None) or getattr(p, "prompt", None),
                        )

            # 3) Config-file personas (old AstrBot layouts)
            try:
                ab_conf = self.context.get_config() or {}
                for p in ab_conf.get("persona", []) or []:
                    if isinstance(p, dict):
                        _add(p.get("name"), p.get("prompt"))
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[OnlyYou] Failed to list config personas: {e}")

        if not result:
            result.append(
                {
                    "persona_name": DEFAULT_SOUL_ID,
                    "prompt": "角色名：星野。性格鲜明、喜欢上网冲浪与朋友聊天的真实网友。",
                }
            )
        return result

    async def list_native_personas(self) -> list[dict[str, str]]:
        """List AstrBot native Personas (import source), personas_v3 first."""
        return await self._collect_native_personas()

    async def list_native_prompts(self) -> list[str]:
        """Return raw native Persona prompt strings (memory-cached).

        Used by ``ContextBudgetManager._takeover_persona`` to locate and replace the
        exact ``\\n# Persona Instructions\\n\\n{prompt}\\n`` segment AstrBot injected.
        """
        if self._native_prompts_cache is not None:
            return list(self._native_prompts_cache)
        prompts: list[str] = []
        seen: set[str] = set()
        for item in await self._collect_native_personas():
            prompt = _clean_text(item.get("prompt"))
            if prompt and prompt not in seen:
                seen.add(prompt)
                prompts.append(prompt)
        if prompts:
            self._native_prompts_cache = prompts
        return list(prompts)

    def invalidate_native_prompt_cache(self) -> None:
        """Drop the cached native prompt list (e.g. after AstrBot Persona edits)."""
        self._native_prompts_cache = None

    # ==================================================================
    # CRUD
    # ==================================================================
    async def list_souls(self) -> list[SoulProfile]:
        """Return all stored SoulProfiles, with ``souls/*.json`` as the source of truth.

        Files are read first; SQLite-only rows (pre-v2.8 databases) are migrated to
        JSON files on the fly. When the JSON file and the SQLite index disagree the
        v2.8 conflict resolver decides (see :meth:`_resolve_soul_conflict`): the
        higher ``revision`` wins, equal revisions keep the JSON file (manual edit)
        and the index is rebuilt, and a newer index row self-heals the JSON file.
        When no Soul exists at all, the built-in default Soul is created.
        """
        souls: dict[str, SoulProfile] = {}
        self._ensure_souls_dir()

        file_names: list[str] = []
        try:
            file_names = sorted(
                name for name in os.listdir(self.souls_dir) if name.endswith(".json")
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[OnlyYou] list soul files failed: {exc}")
            file_names = []

        for name in file_names:
            path = os.path.join(self.souls_dir, name)
            profile = self._read_soul_file(path)
            if profile is None:
                continue
            souls[profile.soul_id] = profile
            cached_hash = self._cache.get(profile.soul_id, ("file",))[0]
            self._cache[profile.soul_id] = (cached_hash, profile)

        db_rows: dict[str, dict[str, Any]] = {}
        for row in await self._fetch_all_soul_index_rows():
            sid = _clean_text(row.get("soul_id"))
            if sid:
                db_rows[sid] = row

        for sid, row in db_rows.items():
            db_profile = self._profile_from_index_row(sid, row)
            if db_profile is None:
                logger.warning(f"[OnlyYou] Failed to load soul row {sid} (corrupt JSON)")
                continue
            persona_hash = str(row.get("persona_hash") or "file")
            file_profile = souls.get(sid)

            if file_profile is None:
                # Legacy SQLite-only row (pre-v2.8) -> migrate to an independent file.
                if _coerce_revision(db_profile.revision) == 0:
                    db_profile.revision = 1
                souls[sid] = db_profile
                self._cache[sid] = (persona_hash, db_profile)
                if self._write_soul_file(db_profile):
                    logger.info(
                        f"[OnlyYou] Migrated legacy SQLite soul '{sid}' to "
                        f"{self.soul_file_path(sid)}"
                    )
                continue

            winner, reason = self._resolve_soul_conflict(file_profile, db_profile)
            if winner is db_profile:
                # The index is newer than the file (the previous JSON write failed,
                # or a wall-clock rollback produced a newer index row) -> self-heal.
                souls[sid] = db_profile
                self._cache[sid] = (persona_hash, db_profile)
                if self._write_soul_file(db_profile):
                    logger.warning(
                        f"[OnlyYou] Restored soul '{sid}' JSON file from the newer "
                        f"SQLite index (revision {_coerce_revision(db_profile.revision)})"
                    )
                continue

            souls[sid] = file_profile
            self._cache[sid] = (persona_hash, file_profile)
            if reason == "file-edited":
                logger.warning(
                    f"[OnlyYou] Soul '{sid}' JSON file differs from the SQLite index "
                    "at the same revision; keeping the JSON file (manual edit) and "
                    "rebuilding the index."
                )
            if reason in ("file-newer", "file-edited"):
                await self._sync_soul_index_safe(file_profile, persona_hash=persona_hash)

        # JSON-only souls (e.g. files copied into ``souls/``) -> rebuild the index.
        for sid, profile in list(souls.items()):
            if sid in db_rows or not os.path.exists(self.soul_file_path(sid)):
                continue
            await self._sync_soul_index_safe(profile)

        orphan_ids = [
            sid
            for sid in souls
            if sid not in db_rows and not os.path.exists(self.soul_file_path(sid))
        ]
        for sid in orphan_ids:
            souls.pop(sid, None)

        if not souls:
            default_soul = await self.resolve_soul(allow_llm_seed=False)
            souls[default_soul.soul_id] = default_soul

        result = sorted(
            souls.values(),
            key=lambda item: (
                -_coerce_timestamp(getattr(item, "updated_at", 0)),
                str(item.soul_id),
            ),
        )
        self._invalidate_platform_cache()
        return result

    async def get_soul_by_id(self, soul_id: str) -> Optional[SoulProfile]:
        """Fetch a specific SoulProfile by ``soul_id`` (cache -> JSON file -> SQLite)."""
        sid = _clean_text(soul_id)
        if not sid:
            return None
        cached = self.get_cached_soul(sid)
        if cached is not None:
            return cached

        file_profile: Optional[SoulProfile] = None
        path = self.soul_file_path(sid)
        if os.path.exists(path):
            file_profile = self._read_soul_file(path)
            if file_profile is not None:
                file_profile.soul_id = file_profile.soul_id or sid

        row = await self._fetch_soul_index_row(sid)
        db_profile: Optional[SoulProfile] = None
        if row and row.get("profile_json"):
            db_profile = self._profile_from_index_row(sid, row)
        persona_hash = str((row or {}).get("persona_hash") or "file")

        if file_profile is not None and db_profile is not None:
            winner, reason = self._resolve_soul_conflict(file_profile, db_profile)
            self._cache[sid] = (persona_hash, winner)
            if winner is db_profile:
                if self._write_soul_file(db_profile):
                    logger.warning(
                        f"[OnlyYou] Restored soul '{sid}' JSON file from the newer "
                        f"SQLite index (revision {_coerce_revision(db_profile.revision)})"
                    )
                return db_profile
            if reason in ("file-newer", "file-edited"):
                await self._sync_soul_index_safe(file_profile, persona_hash=persona_hash)
                if reason == "file-edited":
                    logger.warning(
                        f"[OnlyYou] Soul '{sid}' JSON file differs from the SQLite "
                        "index at the same revision; keeping the JSON file (manual "
                        "edit) and rebuilding the index."
                    )
            return file_profile

        if file_profile is not None:
            self._cache[sid] = (self._cache.get(sid, ("file",))[0], file_profile)
            # A JSON file without an index row still needs a row for cascade updates
            # and the "newest Soul" fallback routing.
            await self._sync_soul_index_safe(file_profile)
            return file_profile

        if db_profile is not None:
            if _coerce_revision(db_profile.revision) == 0:
                db_profile.revision = 1
            self._cache[sid] = (persona_hash, db_profile)
            # Legacy SQLite-only row -> migrate to an independent JSON file.
            if self._write_soul_file(db_profile):
                logger.info(
                    f"[OnlyYou] Migrated legacy SQLite soul '{sid}' to "
                    f"{self.soul_file_path(sid)}"
                )
            return db_profile
        return None

    async def save_soul(self, profile: SoulProfile, persona_hash: Optional[str] = None) -> SoulProfile:
        """Persist a SoulProfile to its own JSON file **and** the SQLite index.

        v2.8 hardening: ``revision`` increases monotonically on every save and
        ``updated_at`` never moves backwards, so a wall-clock rollback can never
        make an older copy win.  The JSON write is atomic and retried; when it
        finally fails the SQLite index still receives the new revision (no data
        loss), ``last_file_write_error`` is set for surfacing, and the next read
        (index revision > file revision) rewrites the JSON file automatically.
        """
        if profile is None:
            raise ValueError("SoulProfile 不能为空")
        profile.card = normalize_card(profile.card)
        profile.model_config = normalize_model_config(profile.model_config)
        profile.bound_platforms = _normalize_bound_platforms(profile.bound_platforms)
        profile.settings = _normalize_soul_settings(profile.settings)
        if not (_clean_text(profile.soul_id)):
            profile.soul_id = DEFAULT_SOUL_ID
        sid = profile.soul_id
        incoming_revision = _coerce_revision(profile.revision)

        # ---- monotonic revision / updated_at (clock-rollback safe) ----
        file_path = self.soul_file_path(sid)
        file_profile = (
            self._read_soul_file(file_path) if os.path.exists(file_path) else None
        )
        row = await self._fetch_soul_index_row(sid)
        db_profile = (
            self._profile_from_index_row(sid, row)
            if row and row.get("profile_json")
            else None
        )
        cached_profile = (self._cache.get(sid) or ("", None))[1]
        previous_revision = max(
            _coerce_revision(getattr(file_profile, "revision", 0)),
            _coerce_revision(getattr(db_profile, "revision", 0)),
            _coerce_revision(getattr(cached_profile, "revision", 0)),
            incoming_revision,
        )
        previous_updated = max(
            _coerce_timestamp(getattr(file_profile, "updated_at", 0)),
            _coerce_timestamp(getattr(db_profile, "updated_at", 0)),
            _coerce_timestamp(getattr(cached_profile, "updated_at", 0)),
        )
        now = int(time.time())
        # A real clock rollback is a jump backwards; a 1s drift is just the
        # same-second save clamp below (monotonic updated_at), not a rollback.
        if (
            previous_updated
            and previous_updated - now > 5
            and sid not in self._clock_rollback_warned
        ):
            self._clock_rollback_warned.add(sid)
            logger.warning(
                f"[OnlyYou] System clock moved backwards while saving soul '{sid}' "
                f"(now={now}, last={previous_updated}); keeping updated_at monotonic."
            )
        if (
            file_profile is not None
            and incoming_revision < _coerce_revision(file_profile.revision)
        ):
            logger.warning(
                f"[OnlyYou] save_soul('{sid}') received a stale copy "
                f"(incoming revision {incoming_revision} < disk revision "
                f"{_coerce_revision(file_profile.revision)}); the disk file may have "
                "been edited externally. Saving with a newer revision will overwrite it."
            )
        profile.revision = previous_revision + 1
        profile.updated_at = max(now, previous_updated + 1)

        old_cached = (self._cache.get(sid) or ("",))[0]
        p_hash = _clean_text(persona_hash) or _clean_text(old_cached) or "webui_custom"

        file_written = self._write_soul_file(profile)
        try:
            await self._upsert_soul_index(profile, persona_hash=p_hash)
        except Exception as exc:  # noqa: BLE001 - JSON file is authoritative
            logger.warning(
                f"[OnlyYou] upsert soul index failed for '{sid}' ({exc}); "
                "the JSON file is kept and the index will be rebuilt on next read."
            )
        self._cache[sid] = (p_hash, profile)
        self._invalidate_platform_cache()
        if not file_written and self.last_file_write_error:
            logger.error(
                f"[OnlyYou] Soul '{sid}' saved to the SQLite index only; "
                f"JSON file degraded: {self.last_file_write_error}"
            )
        return profile

    async def rename_soul(self, old_soul_id: str, new_soul_id: str) -> SoulProfile:
        """
        Rename `old_soul_id` -> `new_soul_id` and cascade update across:
        - `souls` (JSON file renamed) / `daily_schedules` / `memories`
        - `umo_soul_bindings` / `surfing_history` / plugin default soul
        """
        old_id = (old_soul_id or "").strip()
        new_id = (new_soul_id or "").strip()
        if not old_id or not new_id:
            raise ValueError("Soul ID 不能为空")
        if old_id == new_id:
            existing = await self.get_soul_by_id(old_id)
            if existing is None:
                raise ValueError(f"未找到角色 {old_id}")
            return existing

        existing_target = await self.get_soul_by_id(new_id)
        if existing_target is not None:
            raise ValueError(f"目标 Soul ID '{new_id}' 已存在")

        profile = await self.get_soul_by_id(old_id)
        if profile is None:
            raise ValueError(f"未找到原角色 '{old_id}'")

        old_path = self.soul_file_path(old_id)
        profile.soul_id = new_id
        await self.save_soul(profile)

        # Cascade updates in SQLite tables
        await self.db.execute("DELETE FROM souls WHERE soul_id = ?", (old_id,))
        await self.db.execute(
            "UPDATE OR REPLACE daily_schedules SET soul_id = ? WHERE soul_id = ?",
            (new_id, old_id),
        )
        # Also update `soul_id` inside `daily_schedules.run_json`
        sched_rows = await self.db.fetchall(
            "SELECT date_str, run_json FROM daily_schedules WHERE soul_id = ?", (new_id,)
        )
        for sr in sched_rows:
            try:
                rdata = json.loads(sr["run_json"])
                rdata["soul_id"] = new_id
                await self.db.execute(
                    "UPDATE daily_schedules SET run_json = ? WHERE soul_id = ? AND date_str = ?",
                    (json.dumps(rdata, ensure_ascii=False), new_id, sr["date_str"]),
                )
            except Exception:
                pass

        await self.db.execute(
            "UPDATE memories SET soul_id = ? WHERE soul_id = ?", (new_id, old_id)
        )
        await self.db.execute(
            "UPDATE umo_soul_bindings SET soul_id = ? WHERE soul_id = ?", (new_id, old_id)
        )
        await self.db.execute(
            "UPDATE surfing_history SET soul_id = ? WHERE soul_id = ?", (new_id, old_id)
        )

        self._cache.pop(old_id, None)
        self._invalidate_platform_cache()
        try:
            if os.path.exists(old_path):
                os.remove(old_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[OnlyYou] remove old soul file failed: {exc}")
        for k, v in list(self._umo_bindings.items()):
            if v == old_id:
                self._umo_bindings[k] = new_id
        if await self.get_default_soul_id() == old_id:
            await self.set_default_soul_id(new_id)
        return profile

    async def delete_soul(self, soul_id: str) -> None:
        """Delete a SoulProfile (JSON file + its schedules, memories, and bindings)."""
        sid = (soul_id or "").strip()
        if not sid:
            return
        self._cache.pop(sid, None)
        self._invalidate_platform_cache()
        try:
            path = self.soul_file_path(sid)
            if os.path.exists(path):
                os.remove(path)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[OnlyYou] delete soul file failed: {exc}")
        for k, v in list(self._umo_bindings.items()):
            if v == sid:
                self._umo_bindings.pop(k, None)
        # v2.9: purge the per-Soul relation cache held by UserRelationManager.
        purge = getattr(self, "on_soul_deleted", None)
        if callable(purge):
            try:
                purge(sid)
            except Exception as exc:  # noqa: BLE001 - cache purge is best-effort
                logger.warning(f"[OnlyYou] purge soul relation cache failed: {exc}")
        await self.db.execute("DELETE FROM souls WHERE soul_id = ?", (sid,))
        await self.db.execute("DELETE FROM daily_schedules WHERE soul_id = ?", (sid,))
        await self.db.execute("DELETE FROM memories WHERE soul_id = ?", (sid,))
        # v2.9: relationship rows are per Soul -> cascade with the Soul.
        try:
            await self.db.execute(
                "DELETE FROM user_relations WHERE soul_id = ?", (sid,)
            )
        except Exception as exc:  # noqa: BLE001 - older DBs may not have the table yet
            logger.warning(f"[OnlyYou] delete user_relations failed: {exc}")
        await self.db.execute("DELETE FROM umo_soul_bindings WHERE soul_id = ?", (sid,))
        await self.db.execute("DELETE FROM surfing_history WHERE soul_id = ?", (sid,))
        if await self.get_default_soul_id() == sid:
            await self.set_default_soul_id("")

    # ==================================================================
    # v2.8: platform (bot instance) binding -- one-to-many Soul -> platforms,
    # strictly mutually exclusive platform -> Soul.
    # ==================================================================
    async def get_platform_bindings(self) -> dict[str, str]:
        """Return ``{platform_id: soul_id}`` (newest Soul wins on legacy conflicts)."""
        if self._platform_bindings_cache is not None:
            return dict(self._platform_bindings_cache)
        mapping: dict[str, str] = {}
        souls = await self.list_souls()
        for soul in sorted(
            souls,
            key=lambda item: -_coerce_timestamp(getattr(item, "updated_at", 0)),
        ):
            for platform_id in _normalize_bound_platforms(soul.bound_platforms):
                # Strict mutual exclusion: the most recently updated Soul wins.
                mapping.setdefault(platform_id, soul.soul_id)
        self._platform_bindings_cache = mapping
        return dict(mapping)

    async def get_bound_platforms(self, soul_id: str) -> list[str]:
        soul = await self.get_soul_by_id(soul_id)
        return list(soul.bound_platforms) if soul is not None else []

    async def bind_platform(self, soul_id: str, platform_id: str) -> SoulProfile:
        """Bind one bot instance to a Soul (rejects platforms owned by another Soul)."""
        soul = await self.get_soul_by_id(soul_id)
        if soul is None:
            raise ValueError(f"未找到角色 {soul_id}")
        pid = _clean_text(platform_id)
        if not pid:
            raise ValueError("机器人接口 ID (platform_id) 不能为空")
        if ":" in pid:
            raise ValueError("platform_id 必须是不含冒号的机器人接口 ID（如 aiocqhttp）")
        if len(pid) > _MAX_PLATFORM_ID_LEN:
            raise ValueError("platform_id 过长")
        bindings = await self.get_platform_bindings()
        owner = bindings.get(pid, "")
        if owner and owner != soul.soul_id:
            owner_soul = await self.get_soul_by_id(owner)
            owner_name = (
                _clean_text(getattr(owner_soul, "name", "")) or owner_soul.soul_id
                if owner_soul is not None
                else owner
            )
            raise ValueError(
                f"该机器人接口已被角色『{owner_name}』绑定，请先在角色 {owner_name} 下解绑"
            )
        if pid not in soul.bound_platforms:
            soul.bound_platforms = list(soul.bound_platforms) + [pid]
            await self.save_soul(soul)
        return soul

    async def unbind_platform(self, platform_id: str) -> str:
        """Unbind a bot instance from whichever Soul owns it; returns that Soul id."""
        pid = _clean_text(platform_id)
        if not pid:
            return ""
        bindings = await self.get_platform_bindings()
        owner = bindings.get(pid, "")
        if not owner:
            return ""
        soul = await self.get_soul_by_id(owner)
        if soul is not None and pid in soul.bound_platforms:
            soul.bound_platforms = [item for item in soul.bound_platforms if item != pid]
            await self.save_soul(soul)
        return owner

    async def set_soul_setting(self, soul_id: str, key: str, value: Any) -> SoulProfile:
        """Update one key of a Soul's ``settings`` dict and persist the JSON file."""
        soul = await self.get_soul_by_id(soul_id)
        if soul is None:
            raise ValueError(f"未找到角色 {soul_id}")
        text_key = _clean_text(key)
        if not text_key:
            raise ValueError("setting key 不能为空")
        settings = dict(soul.settings or {})
        settings[text_key] = value
        soul.settings = _normalize_soul_settings(settings)
        await self.save_soul(soul)
        return soul

    async def migrate_legacy_umo_bindings(self) -> int:
        """One-shot migration: legacy UMO bindings -> platform (bot instance) bindings.

        v2.8 hardening: the ``legacy_umo_bindings_migrated`` flag is only persisted
        when **every** binding was migrated successfully.  A failed save leaves the
        flag unset so the next startup retries (the migration itself is idempotent:
        already-bound platforms are skipped).
        """
        try:
            migrated = bool(
                await self.db.get_setting(_SETTING_LEGACY_UMO_MIGRATED, False)
            )
        except Exception:
            migrated = False
        if migrated:
            return 0
        await self._ensure_bindings_loaded()
        bindings = await self.get_platform_bindings()
        count = 0
        failed = 0
        for umo, soul_id in list(self._umo_bindings.items()):
            platform_id = _clean_text(umo).split(":", 1)[0].strip() if ":" in _clean_text(umo) else ""
            if not platform_id or platform_id in bindings:
                continue
            soul = await self.get_soul_by_id(soul_id)
            if soul is None or platform_id in soul.bound_platforms:
                continue
            original_platforms = list(soul.bound_platforms)
            try:
                soul.bound_platforms = original_platforms + [platform_id]
                await self.save_soul(soul)
                bindings[platform_id] = soul.soul_id
                count += 1
            except Exception as exc:  # noqa: BLE001
                # Revert the in-memory mutation and drop the cached copy so the
                # next startup retries from the on-disk state (no phantom binding).
                soul.bound_platforms = original_platforms
                self._cache.pop(soul.soul_id, None)
                failed += 1
                logger.warning(f"[OnlyYou] migrate legacy umo binding failed: {exc}")
        if failed:
            logger.warning(
                "[OnlyYou] legacy UMO binding migration incomplete "
                f"({failed} failure(s)); keeping the migration flag unset so the "
                "next startup retries."
            )
            if count:
                logger.info(
                    f"[OnlyYou] Migrated {count} legacy UMO bindings to platform bindings."
                )
            return count
        try:
            await self.db.set_setting(_SETTING_LEGACY_UMO_MIGRATED, True)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[OnlyYou] persist legacy umo migration flag failed: {exc}")
        if count:
            logger.info(f"[OnlyYou] Migrated {count} legacy UMO bindings to platform bindings.")
        return count

    # ==================================================================
    # Default / fallback cards
    # ==================================================================
    @staticmethod
    def _build_default_card() -> dict[str, Any]:
        """Built-in first-run card (no LLM, no AstrBot Persona auto-import)."""
        card = empty_card()
        card["identity"]["name"] = "星野"
        card["identity"]["nickname"] = "小星"
        card["identity"]["tagline"] = "有自己生活作息与喜怒哀乐的真实网友"
        card["personality"]["personality_tags"] = ["自然随性", "有点小幽默", "慢热"]
        card["speech"]["speaking_style"] = "口语化短句，像微信/QQ好友一样自然随性"
        card["speech"]["reply_length"] = "short"
        card["interests"]["surfing_preferences"] = list(_DEFAULT_SURFING_PREFERENCES)
        return normalize_card(card)

    @staticmethod
    def _build_imported_card(
        persona_name: str, native_prompt: str
    ) -> dict[str, Any]:
        """Basic card inferred from the imported native Persona raw text."""
        card = empty_card()
        display_name = persona_name if persona_name and persona_name != DEFAULT_SOUL_ID else "星野"
        card["identity"]["name"] = display_name
        card["identity"]["tagline"] = (
            _clean_text(native_prompt)[:45] if native_prompt else "真实鲜活的数字网友"
        )
        card["speech"]["speaking_style"] = "口语化短句，像熟人好友一样自然随性"
        card["speech"]["reply_length"] = "short"
        card["interests"]["surfing_preferences"] = list(_DEFAULT_SURFING_PREFERENCES)
        card["extra"]["native_persona_prompt"] = _clean_text(native_prompt)
        return normalize_card(card)

    @staticmethod
    def _fill_card_from_seed_data(
        card: dict[str, Any], data: dict[str, Any], persona_name: str
    ) -> dict[str, Any]:
        """Fill still-empty card fields from the legacy deck-seed JSON response."""
        if _is_empty_value(card["identity"].get("name")):
            card["identity"]["name"] = _clean_text(data.get("name")) or persona_name
        if _is_empty_value(card["identity"].get("tagline")):
            card["identity"]["tagline"] = _clean_text(data.get("archetype"))
        if _is_empty_value(card["speech"].get("speaking_style")):
            card["speech"]["speaking_style"] = _clean_text(data.get("speaking_style"))
        if not card["interests"].get("surfing_preferences"):
            card["interests"]["surfing_preferences"] = _clean_list(
                data.get("surfing_preferences")
            ) or list(_DEFAULT_SURFING_PREFERENCES)
        return normalize_card(card)

    # ==================================================================
    # AI card generation (drafts only, never persisted here)
    # ==================================================================
    @staticmethod
    def _extract_card_payload(raw: Any) -> Optional[dict[str, Any]]:
        if not isinstance(raw, dict):
            return None
        if isinstance(raw.get("card"), dict):
            return raw["card"]
        if any(section_id in raw for section_id in CARD_SECTION_IDS):
            return raw
        return None

    @staticmethod
    def _extract_section_payload(
        raw: Any, section_id: str
    ) -> Optional[dict[str, Any]]:
        if not isinstance(raw, dict):
            return None
        section_raw = raw.get(section_id)
        if isinstance(section_raw, dict):
            return section_raw
        if isinstance(raw.get("fields"), dict):
            return raw["fields"]
        field_keys = {
            f["key"] for f in _CARD_SECTION_BY_ID[section_id]["fields"]
        }
        if any(key in field_keys for key in raw):
            return raw
        return None

    async def generate_card(
        self,
        seed_text: str = "",
        base_card: Optional[dict[str, Any]] = None,
        section: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Generate a whole-card or single-section draft with the **main** model.

        The returned card is a draft only — it is never written to the database.
        Returns ``None`` when the model is unavailable / the reply cannot be parsed.
        """
        if self.model_hub is None:
            return None

        base = normalize_card(base_card) if isinstance(base_card, dict) else empty_card()
        if isinstance(base_card, dict) and not any(
            isinstance(base_card.get(sid), dict) for sid in CARD_SECTION_IDS
        ):
            # Tolerate a legacy flat profile dict being passed as `base_card`.
            legacy_keys = (
                "name",
                "archetype",
                "speaking_style",
                "surfing_preferences",
                "native_persona_prompt",
            )
            if any(k in base_card for k in legacy_keys):
                base = migrate_legacy(base_card)
        seed = _clean_text(seed_text)

        try:
            if section is not None and _clean_text(section):
                section_id = _clean_text(section)
                section_def = _CARD_SECTION_BY_ID.get(section_id)
                if section_def is None:
                    logger.warning(f"[OnlyYou] generate_card: unknown section '{section}'")
                    return None
                other_sections = {
                    key: value
                    for key, value in base.items()
                    if key in _CARD_SECTION_BY_ID and key != section_id
                }
                prompt = CHARACTER_CARD_SECTION_PROMPT.format(
                    section_json=json.dumps(section_def, ensure_ascii=False),
                    other_sections_json=json.dumps(other_sections, ensure_ascii=False),
                    seed_text=seed,
                )
                raw = await self.model_hub.call_llm_json(
                    prompt=prompt, role="main", default=None
                )
                generated = self._extract_section_payload(raw, section_id)
                if generated is None:
                    return None
                merged = normalize_card(base)
                for field_def in section_def["fields"]:
                    key = field_def["key"]
                    value = generated.get(key)
                    if not _is_empty_value(value):
                        merged[section_id][key] = value
                # The imported native prompt is system-maintained, never AI-generated.
                merged["extra"]["native_persona_prompt"] = base["extra"][
                    "native_persona_prompt"
                ]
                return normalize_card(merged)

            prompt = CHARACTER_CARD_GENERATE_PROMPT.format(
                schema_json=json.dumps(CARD_SCHEMA, ensure_ascii=False),
                seed_text=seed,
                existing_card_json=json.dumps(base, ensure_ascii=False),
            )
            raw = await self.model_hub.call_llm_json(
                prompt=prompt, role="main", default=None
            )
            generated_card = self._extract_card_payload(raw)
            if generated_card is None:
                return None
            merged = normalize_card(base)
            for section_id in CARD_SECTION_IDS:
                generated_section = generated_card.get(section_id)
                if not isinstance(generated_section, dict):
                    continue
                for field_def in _CARD_SECTION_BY_ID[section_id]["fields"]:
                    key = field_def["key"]
                    value = generated_section.get(key)
                    if not _is_empty_value(value):
                        merged[section_id][key] = value
            # Never let the AI overwrite the imported native Persona raw text.
            merged["extra"]["native_persona_prompt"] = base["extra"][
                "native_persona_prompt"
            ]
            return normalize_card(merged)
        except Exception as e:  # noqa: BLE001 - AI generation must never crash callers
            logger.warning(f"[OnlyYou] generate_card failed: {e}")
            return None

    # ==================================================================
    # Fallback Roguelite deck (LLM offline)
    # ==================================================================
    @staticmethod
    def build_fallback_custom_deck(
        soul_name: str, persona_prompt: str
    ) -> list[StateEventCard]:
        """Synthesize a tailored ~10-card action pool when the LLM is offline.

        Covers all four action types (``BUSY_TASK`` x3 / ``SURF`` x2 /
        ``IDLE_ROAM`` x3 / ``SLEEP`` x2: one long sleep + one nap) and never
        produces encounter cards. Every card's ``state`` is bound to its
        ``action_type.bound_state``, and titles follow the unified 6~10 char
        single-clause rule (no category prefix / role-name prefix / commas).
        """
        del soul_name  # titles must never carry a role-name prefix
        keywords = _extract_persona_keywords(persona_prompt)
        focus = keywords[0] if keywords else ""
        hobby = keywords[1] if len(keywords) > 1 else ""

        busy_title_1 = f"埋头忙{focus}的活" if focus else "埋头赶手头正事"
        busy_title_2 = f"专心搞{focus}相关事" if focus else "专心推进今日任务"
        surf_title_2 = f"窝着刷{hobby}短视频" if hobby else "窝在被窝刷短视频"

        # (title, action_type, min_hours, max_hours, base_weight)
        # 护甲不再由卡池配置：卡片自动取 action_type.fixed_armor（v2.6 四档固定护甲）。
        specs: list[tuple[str, ActionType, float, float, int]] = [
            (busy_title_1, ActionType.BUSY_TASK, 1.0, 3.0, 60),
            (busy_title_2, ActionType.BUSY_TASK, 1.5, 3.0, 55),
            ("关起门来专心赶工", ActionType.BUSY_TASK, 1.0, 2.5, 50),
            ("瘫沙发刷一会儿视频", ActionType.SURF, 0.5, 2.0, 75),
            (surf_title_2, ActionType.SURF, 1.0, 2.0, 70),
            ("趴着发呆放空脑袋", ActionType.IDLE_ROAM, 0.5, 2.0, 70),
            ("翻群记录偷偷吃瓜", ActionType.IDLE_ROAM, 1.0, 2.0, 65),
            ("泡杯茶悠闲摸会儿鱼", ActionType.IDLE_ROAM, 0.5, 1.5, 60),
            ("戴眼罩呼呼大睡", ActionType.SLEEP, 4.0, 7.5, 80),
            ("趴桌上小憩一会儿", ActionType.SLEEP, 1.0, 2.0, 60),
        ]

        cards: list[StateEventCard] = []
        for index, spec in enumerate(specs, start=1):
            title, action_type, min_hours, max_hours, base_weight = spec
            cards.append(
                StateEventCard(
                    card_id=f"evt_fallback_{action_type.value.lower()}_{index}",
                    title=_compact_title(title),
                    state=action_type.bound_state,
                    action_type=action_type,
                    min_hours=min_hours,
                    max_hours=max_hours,
                    base_weight=base_weight,
                )
            )
        return cards

    # ==================================================================
    # AI action-deck generation / re-roll (clear old pool, new cards for tomorrow)
    # ==================================================================
    #: Default number of cards generated by the AI deck prompt (SLEEP×2 /
    #: BUSY_TASK×3 / SURF×2 / IDLE_ROAM×2).
    ACTION_DECK_DEFAULT_COUNT = 9

    async def generate_action_cards(
        self, soul: SoulProfile, count: int = 9
    ) -> tuple[list[StateEventCard], str]:
        """Generate a brand-new four-type action deck for ``soul`` (not persisted).

        Returns ``(cards, source)`` where ``source`` is ``"ai"`` when the decision
        model answered with usable cards, or ``"fallback"`` (the deterministic
        per-character deck) so card re-rolling never fails offline.
        """
        try:
            total = max(1, int(count or self.ACTION_DECK_DEFAULT_COUNT))
        except (TypeError, ValueError):
            total = self.ACTION_DECK_DEFAULT_COUNT

        cards = await self._generate_action_cards_with_llm(soul, total)
        if cards:
            return cards, "ai"

        persona_prompt = (
            getattr(soul, "native_persona_prompt", "")
            or f"{soul.name} {soul.archetype} {soul.speaking_style}".strip()
        )
        fallback = self.build_fallback_custom_deck(soul.name or soul.soul_id, persona_prompt)
        return list(fallback), "fallback"

    @staticmethod
    def _render_action_deck_summary(soul: SoulProfile) -> str:
        """Compact character brief injected into ``ACTION_DECK_GENERATE_PROMPT``.

        Beyond the shared ``render_card_summary`` (定位 / 性格 / 说话方式) this
        adds the fields that matter for choosing daily activities: 职业、居所、
        日常作息与爱好. Never raises — a malformed card just yields a shorter brief.
        """
        try:
            card = getattr(soul, "card", {}) or {}
            identity = card.get("identity") or {}
            rhythm = card.get("rhythm") or {}
            interests = card.get("interests") or {}
            parts = [render_card_summary(card, max_chars=200)]
            extras = (
                ("职业", _clean_text(identity.get("occupation"))),
                ("居所", _clean_text(identity.get("city"))),
                ("日常作息", _clean_text(rhythm.get("daily_routine"))),
                ("爱好", "、".join(_clean_list(interests.get("hobbies")))),
            )
            for label, value in extras:
                if value:
                    parts.append(f"{label}：{value}")
            return "；".join(part for part in parts if part)[:400]
        except Exception:  # noqa: BLE001 - summary is best-effort context only
            return ""

    @staticmethod
    def _action_deck_quotas(total: int) -> dict[ActionType, int]:
        """Split ``total`` cards across the fixed four-action ratio (largest remainder)."""
        try:
            target = max(len(ACTION_DECK_RATIO), int(total or 0))
        except (TypeError, ValueError):
            target = sum(weight for _, weight in ACTION_DECK_RATIO)
        weight_sum = sum(weight for _, weight in ACTION_DECK_RATIO) or 1
        quotas: dict[ActionType, int] = {}
        remainders: list[tuple[float, int, ActionType]] = []
        assigned = 0
        for order, (action, weight) in enumerate(ACTION_DECK_RATIO):
            exact = target * weight / weight_sum
            count = int(exact)
            quotas[action] = count
            assigned += count
            remainders.append((exact - count, order, action))
        for _, _, action in sorted(remainders, key=lambda item: (-item[0], item[1]))[
            : target - assigned
        ]:
            quotas[action] += 1
        return quotas

    def _complete_action_deck(
        self, cards: list[StateEventCard], soul: SoulProfile, count: int
    ) -> list[StateEventCard]:
        """Pad a partial AI deck to the required four-type ratio with fallback cards.

        Real decision models occasionally return fewer than 9 cards or forget a
        whole action type (e.g. every ``SLEEP`` card). Without this, a re-rolled
        pool could silently lose a type and the timeline would fall back to the
        generic built-in cards. Missing / under-represented types are filled from
        ``build_fallback_custom_deck`` for the same Soul.
        """
        quotas = self._action_deck_quotas(count)
        persona_prompt = (
            getattr(soul, "native_persona_prompt", "")
            or f"{soul.name} {soul.archetype} {soul.speaking_style}".strip()
        )
        fallback = self.build_fallback_custom_deck(
            soul.name or soul.soul_id, persona_prompt
        )
        fallback_by_action: dict[ActionType, list[StateEventCard]] = {
            action: [card for card in fallback if card.action == action]
            for action in ActionType
        }
        result: list[StateEventCard] = []
        for action in ActionType:
            wanted = quotas.get(action, 0)
            picked = [card for card in cards if card.action == action][:wanted]
            fillers = fallback_by_action.get(action) or []
            while len(picked) < wanted and fillers:
                picked.append(fillers[len(picked) % len(fillers)])
            result.extend(picked)
        return result

    async def _generate_action_cards_with_llm(
        self, soul: SoulProfile, count: int
    ) -> list[StateEventCard]:
        if self.model_hub is None:
            return []
        try:
            soul_summary = self._render_action_deck_summary(soul)
            prompt = ACTION_DECK_GENERATE_PROMPT.format(
                soul_name=soul.name or soul.soul_id,
                soul_archetype=soul.archetype or "（未填写）",
                speaking_style=soul.speaking_style or "（未填写）",
                chronotype=soul.chronotype,
                surfing_preferences="、".join(soul.surfing_preferences) or "无特别偏好",
                soul_summary=soul_summary or "（无）",
            )
            data = await self.model_hub.call_llm_json(
                prompt=prompt, role="decision", default=None
            )
        except Exception as e:  # noqa: BLE001 - AI generation must never crash callers
            logger.warning(f"[OnlyYou] Action deck generation failed: {e}")
            return []

        if isinstance(data, dict):
            items = data.get("state_event_cards")
            if not isinstance(items, list):
                items = data.get("cards")
        elif isinstance(data, list):
            items = data
        else:
            items = []
        cards = _parse_state_event_cards(items)
        if not cards:
            return []

        # The model may under-deliver (fewer cards / a missing action type): pad to
        # the fixed 2/3/2/2 ratio with deterministic fallback cards before saving.
        cards = self._complete_action_deck(cards, soul, count)

        # Unique ids per batch so re-rolling twice never collides in the UI.
        stamp = int(time.time())
        for index, card in enumerate(cards):
            card.card_id = f"evt_ai_{stamp}_{index + 1}"
        return cards[:count]

    async def regenerate_action_cards(
        self, soul_id: str, count: int = 9
    ) -> tuple[list[StateEventCard], str]:
        """Clear a Soul's action-card pool and replace it with a new generated one.

        Today's already-materialized 24h timeline is untouched (it snapshots the
        cards it drew); the new pool is used from the next day's ``today_str`` on.
        """
        soul = await self.get_soul_by_id(soul_id)
        if soul is None:
            raise ValueError(f"角色 {soul_id} 不存在")
        cards, source = await self.generate_action_cards(soul, count=count)
        soul.state_event_cards = list(cards)
        await self.save_soul(soul)
        logger.info(
            f"[OnlyYou] Regenerated {len(cards)} action cards for soul "
            f"'{soul.soul_id}' (source={source})."
        )
        return soul.state_event_cards, source

    # ==================================================================
    # Native Persona import
    # ==================================================================
    async def import_native_persona(
        self,
        persona_name: str,
        target_soul_id: Optional[str] = None,
        auto_generate_cards: bool = True,
        override_prompt: Optional[str] = None,
        ai_generate_card: bool = False,
        avatar: str = "",
    ) -> SoulProfile:
        """Import an AstrBot native Persona as a SoulProfile.

        - The raw native prompt is always preserved in
          ``card["extra"]["native_persona_prompt"]`` (never rendered).
        - ``ai_generate_card=True``: ask the **main** model for a full card; on
          failure fall back to the basic card inferred from the raw text.
        - ``auto_generate_cards=True``: ask the **decision** model for the four-type
          action card pool (8~12 cards, no encounter cards); on failure or an empty
          result, use ``build_fallback_custom_deck``.
        - ``avatar`` (optional): emoji / short text / image URL / ``data:image/...``
          Base64 avatar; written into ``card["identity"]["avatar"]`` (normalized)
          after the card is inferred or AI-generated, then persisted with the soul.
        - Encounter cards were removed from the system entirely (v2.9 cleanup).
        """
        pname = _clean_text(persona_name) or DEFAULT_SOUL_ID
        soul_id = _clean_text(target_soul_id or pname) or pname

        native_prompt = _clean_text(override_prompt)
        if not native_prompt:
            native_list = await self.list_native_personas()
            for item in native_list:
                if _clean_text(item.get("persona_name")) == pname and _clean_text(
                    item.get("prompt")
                ):
                    native_prompt = _clean_text(item.get("prompt"))
                    break
            if not native_prompt:
                for item in native_list:
                    if _clean_text(item.get("prompt")):
                        native_prompt = _clean_text(item.get("prompt"))
                        break

        card = self._build_imported_card(pname, native_prompt)

        # Optional: AI-generated full card (main model), fall back to the inferred card.
        if ai_generate_card and self.model_hub is not None:
            draft = await self.generate_card(seed_text=native_prompt, base_card=card)
            if isinstance(draft, dict):
                card = normalize_card(draft)
                card["extra"]["native_persona_prompt"] = native_prompt

        # The user-supplied avatar (uploaded image / emoji / URL) wins over the
        # inferred or AI-generated one; normalize_card then validates it (§5.1).
        clean_avatar = _clean_text(avatar)
        if clean_avatar:
            card["identity"]["avatar"] = clean_avatar
            card = normalize_card(card)

        state_cards: list[StateEventCard] = []

        if auto_generate_cards:
            data: Optional[dict[str, Any]] = None
            if self.model_hub is not None:
                try:
                    prompt = CARD_AND_DECK_SEED_PROMPT.format(
                        persona_name=pname,
                        native_persona_prompt=native_prompt[:1500],
                    )
                    data = await self.model_hub.call_llm_json(
                        prompt=prompt, role="decision", default=None
                    )
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[OnlyYou] Deck seeding failed: {e}")
                    data = None

            if isinstance(data, dict):
                state_cards = _parse_state_event_cards(data.get("state_event_cards"))
                if not ai_generate_card:
                    card = self._fill_card_from_seed_data(card, data, pname)

            # LLM offline / failure / empty deck -> deterministic tailored fallback
            # (action cards only; encounter cards are gone from the system).
            if not state_cards:
                state_cards = self.build_fallback_custom_deck(pname, native_prompt)
                logger.info(
                    f"[OnlyYou] Used {len(state_cards)} fallback action cards "
                    f"for imported persona '{pname}'."
                )

        profile = SoulProfile(
            soul_id=soul_id,
            card=card,
            state_event_cards=state_cards,
        )
        return await self.save_soul(profile)

    # ==================================================================
    # Chat-path routing
    # ==================================================================
    async def _get_latest_soul(self) -> Optional[SoulProfile]:
        row = await self.db.fetchone(
            "SELECT soul_id FROM souls ORDER BY updated_at DESC, soul_id ASC LIMIT 1"
        )
        if row and row.get("soul_id"):
            return await self.get_soul_by_id(str(row["soul_id"]))
        return None

    async def resolve_soul(
        self, umo: Optional[str] = None, allow_llm_seed: bool = True
    ) -> SoulProfile:
        """Resolve the SoulProfile for a session.

        v2.8 routing:
        ``机器人接口绑定 (umo 第一段 == platform_id) -> 旧 UMO 绑定兼容 ->
        plugin_settings.default_soul_id -> 最近更新的角色 -> 创建默认角色``.

        ``allow_llm_seed`` is accepted for backward compatibility and ignored — the
        chat path never seeds cards with the LLM anymore (creation is offline/free).
        """
        del allow_llm_seed  # signature compatibility only
        await self._ensure_bindings_loaded()

        clean_umo = _clean_text(umo)
        platform_id = clean_umo.split(":", 1)[0].strip() if ":" in clean_umo else ""
        if platform_id:
            bindings = await self.get_platform_bindings()
            bound_soul_id = bindings.get(platform_id, "")
            if bound_soul_id:
                soul = await self.get_soul_by_id(bound_soul_id)
                if soul is not None:
                    return soul

        if clean_umo and clean_umo in self._umo_bindings:
            soul = await self.get_soul_by_id(self._umo_bindings[clean_umo])
            if soul is not None:
                return soul

        default_soul_id = await self.get_default_soul_id()
        if default_soul_id:
            soul = await self.get_soul_by_id(default_soul_id)
            if soul is not None:
                return soul

        latest = await self._get_latest_soul()
        if latest is not None:
            return latest

        soul = SoulProfile(soul_id=DEFAULT_SOUL_ID, card=self._build_default_card())
        await self.save_soul(soul)
        await self.set_default_soul_id(soul.soul_id)
        logger.info(f"[OnlyYou] Created default soul '{soul.soul_id}' ({soul.name}).")
        return soul
