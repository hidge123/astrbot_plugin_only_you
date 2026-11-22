"""Plugin-autonomous model hub: role-based model configuration per Soul.

Model roles (role layout v2):

* ``dialogue`` — 对话模型: **only** produces the actual chat reply. The plugin
  exposes it to AstrBot's native pipeline via
  ``event.set_extra("selected_provider", ...)``; no background task calls it.
* ``main``     — 主模型: background **generation** (character-card AI, memory
  extraction, schedule monologue, busy completion, proactive messages). Defaults
  to inheriting ``dialogue``.
* ``decision`` — 决策模型: read-the-air / reply-or-ignore judgments only. Defaults
  to inheriting ``main``. ``mode="judge"`` switches it from an ordinary chat model
  to OpenRouter's Decisions API (Jev and other System-One judgment models), which
  returns typed answers with probabilities instead of generated text.
* ``vision``   — multimodal video scoring; defaults to inheriting ``main``.
* ``tts``      — text-to-speech, never inherited; ``mode="off"`` means disabled.
* ``embedding`` / ``rerank`` — optional RAG roles (off by default).

Inheritance is optional: turning it off and leaving ``source_id`` / ``model``
empty simply disables the role (``resolve_role`` returns ``None``) instead of
silently falling back to another model.

The pre-split layout (``main`` = dialogue, ``decision`` = mixed generation +
read-the-air) is migrated by :func:`migrate_legacy_model_config`: ``main`` ->
``dialogue``, ``decision`` -> ``main`` + ``decision``.

Everything is stored in the Soul JSON (``model_config``); the legacy global
``plugin_settings["model_config"]`` key is only used before a SoulManager is
attached / for old tests. All AstrBot provider internals are reached through
``core/provider_factory.py``.
"""

import base64
import copy
import inspect
import json
import os
import re
import time
from typing import Any, Dict, List, Optional

try:
    from astrbot.api import logger
except ImportError:  # pragma: no cover - local offline tests
    import logging

    logger = logging.getLogger("onlyyou.model_hub")

from . import decision_model


DEFAULT_MODEL_CONFIG: Dict[str, dict] = {
    "dialogue": {"source_id": "", "model": ""},
    "main": {
        "inherit": True,
        "inherit_from": "dialogue",
        "source_id": "",
        "model": "",
    },
    "decision": {
        "inherit": True,
        "inherit_from": "main",
        "source_id": "",
        "model": "",
        # chat = 通用聊天模型（Prompt + JSON 判定）；judge = Decisions API
        "mode": "chat",
        "threshold": 0.5,
        "endpoint": "",
    },
    "vision": {
        "inherit": True,
        "inherit_from": "main",
        "source_id": "",
        "model": "",
    },
    "tts": {
        "mode": "off",
        "source_id": "",
        "model": "",
        "voice": "",
        "provider_id": "",
        "probability": 1.0,
        "scope": "private",
        "dual_output": False,
    },
    # v2.12: RAG roles (off / provider / source); off = 纯词法降级检索。
    "embedding": {
        "mode": "off",
        "provider_id": "",
        "source_id": "",
        "model": "",
    },
    "rerank": {
        "mode": "off",
        "provider_id": "",
        "source_id": "",
        "model": "",
    },
}

TTS_MODES = ("off", "source", "provider")
TTS_SCOPES = ("private", "l3", "all")
CHAT_ROLES = ("dialogue", "main", "decision", "vision")
#: 支持「独立配置 / 继承父角色」的角色；``dialogue`` 是继承链的根。
INHERITABLE_ROLES = ("main", "decision", "vision")
#: 决策模型的两种工作方式：普通聊天模型 / Decisions API 判断模型。
DECISION_MODES = ("chat", "judge")
DEFAULT_DECISION_THRESHOLD = 0.5
#: OpenRouter Decisions API（Jev 等 System-One 判断模型走这里）。
DEFAULT_DECISIONS_ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
OPENROUTER_KEY_ENV = "OPENROUTER_API_KEY"
#: v2.12: RAG 角色（向量检索 / 重排）——三档来源模式，`off` 表示停用。
VECTOR_ROLES = ("embedding", "rerank")
VECTOR_MODES = ("off", "provider", "source")
VECTOR_ROLE_LABELS = {
    "embedding": "向量模型 (Embedding)",
    "rerank": "重排模型 (Rerank)",
}

#: Legal ``inherit_from`` values per inheritable chat role.
INHERIT_FROM_OPTIONS: Dict[str, tuple] = {
    "main": ("", "dialogue"),
    "decision": ("", "main", "dialogue"),
    # Vision must not inherit the decision role anymore: that role may be a
    # judge/Decisions model which cannot see images.
    "vision": ("", "main", "dialogue"),
}

#: Maximum inheritance hops (``vision -> main -> dialogue`` is exactly 2 hops).
MAX_INHERIT_HOPS = 2


def _clean(value: Any) -> str:
    if value is None:
        return ""
    try:
        return str(value).strip()
    except Exception:
        return ""


def _to_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    try:
        return str(value).strip().lower() in ("1", "true", "yes", "on", "y")
    except Exception:
        return default


def _pair_of(value: Any) -> tuple:
    """Return a ``(source_id, model)`` string pair from a role config entry."""
    raw = value if isinstance(value, dict) else {}
    return _clean(raw.get("source_id")), _clean(raw.get("model"))


def _has_pair(pair: tuple) -> bool:
    return bool(pair and pair[0] and pair[1])


def _looks_like_legacy_model_layout(raw: Any) -> bool:
    """True when ``raw`` predates the dialogue / main / decision split."""
    if not isinstance(raw, dict) or not raw:
        return False
    if "dialogue" in raw:
        return False
    # New-layout markers: ``inherit_from="dialogue"`` or any judge-mode field.
    for role in INHERITABLE_ROLES:
        entry = raw.get(role)
        if not isinstance(entry, dict):
            continue
        if _clean(entry.get("inherit_from")).lower() == "dialogue":
            return False
        if role == "decision" and any(
            key in entry for key in ("mode", "threshold", "endpoint")
        ):
            return False
    return any(key in raw for key in ("main", "decision", "vision"))


def _legacy_inherit_from(entry: Any, options: tuple) -> str:
    """Reproduce the old role-inheritance defaults for one stored entry."""
    raw = entry if isinstance(entry, dict) else {}
    if "inherit_from" in raw and raw.get("inherit_from") is not None:
        candidate = _clean(raw.get("inherit_from")).lower()
        if candidate in options:
            return candidate
        return "main" if _to_bool(raw.get("inherit"), True) else ""
    if "inherit" in raw:
        return "main" if _to_bool(raw.get("inherit"), True) else ""
    return "main"


def migrate_legacy_model_config(raw: Any) -> Dict[str, dict]:
    """Convert the pre-split model config into the new role layout.

    * old ``main`` (dialogue model)          -> ``dialogue``
    * old ``decision`` (mixed generation)    -> ``main``
    * old ``decision`` effective model       -> new ``decision`` (inherits ``main``)

    ``vision`` keeps its old effective parent (old main -> ``dialogue``, old
    decision -> ``main``), so upgrading does not silently change which model sees
    videos. TTS / RAG entries are carried over untouched.
    """
    source = raw if isinstance(raw, dict) else {}
    old_main = source.get("main") if isinstance(source.get("main"), dict) else {}
    old_decision = (
        source.get("decision") if isinstance(source.get("decision"), dict) else {}
    )
    old_vision = source.get("vision") if isinstance(source.get("vision"), dict) else {}

    dialogue_pair = _pair_of(old_main)
    decision_own = _pair_of(old_decision)
    decision_inherit = _legacy_inherit_from(old_decision, ("main",))
    if not decision_inherit and not _has_pair(decision_own):
        decision_inherit = "main"

    if decision_inherit:
        main = {
            "inherit": True,
            "inherit_from": "dialogue",
            "source_id": "",
            "model": "",
        }
    else:
        main = {
            "inherit": False,
            "inherit_from": "",
            "source_id": decision_own[0],
            "model": decision_own[1],
        }

    vision_own = _pair_of(old_vision)
    vision_inherit = _legacy_inherit_from(old_vision, ("main", "decision"))
    if not vision_inherit and not _has_pair(vision_own):
        vision_inherit = "main"
    if vision_inherit:
        # Old default followed the old chat model: keep that via dialogue.
        vision = {
            "inherit": True,
            "inherit_from": "dialogue" if vision_inherit == "main" else "main",
            "source_id": "",
            "model": "",
        }
    else:
        vision = {
            "inherit": False,
            "inherit_from": "",
            "source_id": vision_own[0],
            "model": vision_own[1],
        }

    migrated = {
        "dialogue": {"source_id": dialogue_pair[0], "model": dialogue_pair[1]},
        "main": main,
        "decision": {
            "inherit": True,
            "inherit_from": "main",
            "source_id": "",
            "model": "",
            "mode": "chat",
            "threshold": DEFAULT_DECISION_THRESHOLD,
            "endpoint": "",
        },
        "vision": vision,
        "tts": copy.deepcopy(
            source.get("tts") if isinstance(source.get("tts"), dict) else {}
        ),
        "embedding": copy.deepcopy(
            source.get("embedding") if isinstance(source.get("embedding"), dict) else {}
        ),
        "rerank": copy.deepcopy(
            source.get("rerank") if isinstance(source.get("rerank"), dict) else {}
        ),
    }
    return normalize_model_config(migrated)


def _normalize_inherit_from(role: str, role_raw: dict) -> str:
    """Normalize a role's inheritance target (``""`` == independent config)."""
    options = INHERIT_FROM_OPTIONS.get(role, ("", "dialogue"))
    default_parent = "dialogue" if role == "main" else "main"
    if "inherit_from" in role_raw and role_raw.get("inherit_from") is not None:
        candidate = _clean(role_raw.get("inherit_from")).lower()
        if candidate in options:
            return candidate
        return default_parent if _to_bool(role_raw.get("inherit"), True) else ""
    if "inherit" in role_raw:
        return default_parent if _to_bool(role_raw.get("inherit"), True) else ""
    # Legacy entries without either key keep the documented default.
    return default_parent


def _normalize_decision_fields(role_raw: dict) -> tuple:
    """Normalize ``mode`` / ``threshold`` / ``endpoint`` of the decision role."""
    mode = _clean(role_raw.get("mode")).lower() or "chat"
    if mode not in DECISION_MODES:
        mode = "chat"
    try:
        threshold = float(role_raw.get("threshold", DEFAULT_DECISION_THRESHOLD))
    except (TypeError, ValueError):
        threshold = DEFAULT_DECISION_THRESHOLD
    if threshold != threshold:  # NaN
        threshold = DEFAULT_DECISION_THRESHOLD
    threshold = max(0.0, min(1.0, threshold))
    endpoint = _clean(role_raw.get("endpoint"))
    if endpoint and not re.match(r"^https?://", endpoint, flags=re.IGNORECASE):
        endpoint = ""
    return mode, round(threshold, 4), endpoint


def normalize_model_config(raw: Any) -> Dict[str, dict]:
    """Coerce arbitrary stored/requested model config into the canonical shape.

    Configs stored by an older version (no ``dialogue`` key) are migrated on the
    fly through :func:`migrate_legacy_model_config`.
    """
    if _looks_like_legacy_model_layout(raw):
        raw = migrate_legacy_model_config(raw)
    raw = raw if isinstance(raw, dict) else {}

    dialogue_pair = _pair_of(raw.get("dialogue"))
    normalized: Dict[str, dict] = {
        "dialogue": {"source_id": dialogue_pair[0], "model": dialogue_pair[1]}
    }

    for role in INHERITABLE_ROLES:
        role_raw = raw.get(role) if isinstance(raw.get(role), dict) else {}
        inherit_from = _normalize_inherit_from(role, role_raw)
        source_id, model = _pair_of(role_raw)
        entry: Dict[str, Any] = {
            # ``inherit`` and ``inherit_from`` are always kept in sync.
            "inherit": bool(inherit_from),
            "inherit_from": inherit_from,
            "source_id": source_id,
            "model": model,
        }
        if role == "decision":
            mode, threshold, endpoint = _normalize_decision_fields(role_raw)
            entry["mode"] = mode
            entry["threshold"] = threshold
            entry["endpoint"] = endpoint
        normalized[role] = entry

    tts_raw = raw.get("tts") if isinstance(raw.get("tts"), dict) else {}
    mode = _clean(tts_raw.get("mode")).lower() or "off"
    if mode not in TTS_MODES:
        mode = "off"
    scope = _clean(tts_raw.get("scope")).lower() or "private"
    if scope not in TTS_SCOPES:
        scope = "private"
    try:
        probability = float(tts_raw.get("probability", 1.0))
    except (TypeError, ValueError):
        probability = 1.0
    probability = max(0.0, min(1.0, probability))
    normalized["tts"] = {
        "mode": mode,
        "source_id": _clean(tts_raw.get("source_id")),
        "model": _clean(tts_raw.get("model")),
        "voice": _clean(tts_raw.get("voice")),
        "provider_id": _clean(tts_raw.get("provider_id")),
        "probability": probability,
        "scope": scope,
        "dual_output": _to_bool(tts_raw.get("dual_output"), default=False),
    }
    for role in VECTOR_ROLES:
        role_raw = raw.get(role) if isinstance(raw.get(role), dict) else {}
        role_mode = _clean(role_raw.get("mode")).lower() or "off"
        if role_mode not in VECTOR_MODES:
            role_mode = "off"
        normalized[role] = {
            "mode": role_mode,
            "provider_id": _clean(role_raw.get("provider_id")),
            "source_id": _clean(role_raw.get("source_id")),
            "model": _clean(role_raw.get("model")),
        }
    return normalized


class ModelHub:
    """Model scheduling gateway (dialogue / main / decision / vision / tts / RAG)."""

    ROLES = ("dialogue", "main", "decision", "vision", "tts", "embedding", "rerank")
    CONFIG_SETTING_KEY = "model_config"
    MIGRATION_FLAG_KEY = "legacy_model_migrated"
    #: One-shot migration of the pre-split (main/decision) layout.
    ROLE_LAYOUT_MIGRATION_KEY = "model_role_layout_migrated_v1"
    #: AstrBot ``inst_map`` key of the plugin-owned **dialogue** provider. The id
    #: keeps the historical ``onlyyou::main`` prefix for backward compatibility
    #: (it is an opaque registration key, not a role name).
    MAIN_PROVIDER_ID = "onlyyou::main"

    def __init__(
        self,
        context: Any,
        plugin_config: dict,
        db: Any = None,
        provider_factory: Any = None,
    ) -> None:
        self.context = context
        self.plugin_config = plugin_config if isinstance(plugin_config, dict) else {}
        self.db = db
        self.provider_factory = provider_factory
        self._config: Dict[str, dict] = normalize_model_config({})
        self._loaded = False
        # v2.8: per-Soul model configs live inside each Soul's JSON file.  When a
        # SoulManager is attached, every role resolves against the Soul config;
        # without one (legacy / unit tests) the global plugin_settings config is used.
        self.soul_manager: Any = None
        self.SOUL_MIGRATION_FLAG_KEY = "model_config_migrated_to_souls"

    def attach_soul_manager(self, soul_manager: Any) -> None:
        """Attach the SoulManager that owns per-Soul ``model_config`` (v2.8)."""
        self.soul_manager = soul_manager

    # ------------------------------------------------------------------
    # Per-Soul config resolution helpers (v2.8)
    # ------------------------------------------------------------------
    def _soul_manager_ready(self) -> bool:
        manager = self.soul_manager
        return manager is not None and callable(
            getattr(manager, "get_cached_soul", None)
        )

    def _sync_active_config(self, soul_id: Optional[str] = None) -> Dict[str, dict]:
        """Sync best-effort config: per-Soul cache when available, else global."""
        if self._soul_manager_ready():
            soul = None
            sid = _clean(soul_id)
            try:
                if sid:
                    soul = self.soul_manager.get_cached_soul(sid)
                else:
                    peek = getattr(self.soul_manager, "peek_default_soul", None)
                    soul = peek() if callable(peek) else None
            except Exception:
                soul = None
            if soul is not None:
                dynamic = getattr(soul, "model_config", None)
                return normalize_model_config(
                    dynamic if isinstance(dynamic, dict) else {}
                )
        return self._config

    async def _ensure_soul_cached(self, soul_id: Optional[str] = None) -> Any:
        """Return the Soul object for ``soul_id`` (default Soul when empty)."""
        if not self._soul_manager_ready():
            return None
        manager = self.soul_manager
        sid = _clean(soul_id)
        try:
            if sid:
                soul = manager.get_cached_soul(sid)
                if soul is None:
                    getter = getattr(manager, "get_soul_by_id", None)
                    if callable(getter):
                        soul = await getter(sid)
                return soul
            peek = getattr(manager, "peek_default_soul", None)
            soul = peek() if callable(peek) else None
            if soul is not None:
                return soul
            getter = getattr(manager, "get_default_soul_id", None)
            default_id = _clean(await getter()) if callable(getter) else ""
            if default_id:
                soul = manager.get_cached_soul(default_id)
                if soul is None:
                    lookup = getattr(manager, "get_soul_by_id", None)
                    if callable(lookup):
                        soul = await lookup(default_id)
                if soul is not None:
                    return soul
            lister = getattr(manager, "list_souls", None)
            if callable(lister):
                souls = await lister()
                if souls:
                    return souls[0]
        except Exception as exc:  # noqa: BLE001 - degrade to global config
            logger.debug(f"[OnlyYou] resolve soul model config failed: {exc}")
        return None

    async def _resolve_soul_id(self, soul_id: Optional[str] = None, umo: Optional[str] = None) -> str:
        """Resolve a concrete Soul id from an explicit id / UMO / default Soul."""
        sid = _clean(soul_id)
        if sid or not self._soul_manager_ready():
            return sid
        resolver = getattr(self.soul_manager, "resolve_soul", None)
        if umo and callable(resolver):
            try:
                soul = await resolver(umo=umo, allow_llm_seed=False)
                routed = _clean(getattr(soul, "soul_id", ""))
                if routed:
                    return routed
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[OnlyYou] resolve soul by umo failed: {exc}")
        soul = await self._ensure_soul_cached(None)
        return _clean(getattr(soul, "soul_id", "")) if soul is not None else ""

    async def _active_config(
        self, soul_id: Optional[str] = None, umo: Optional[str] = None
    ) -> Dict[str, dict]:
        """Async config resolution used by every role/provider entry point."""
        if not self._soul_manager_ready():
            if not self._loaded:
                try:
                    await self.load()
                except Exception:
                    pass
            return self._config
        sid = await self._resolve_soul_id(soul_id, umo=umo)
        if sid:
            soul = await self._ensure_soul_cached(sid)
            if soul is not None:
                dynamic = getattr(soul, "model_config", None)
                return normalize_model_config(
                    dynamic if isinstance(dynamic, dict) else {}
                )
        if not self._loaded:
            try:
                await self.load()
            except Exception:
                pass
        return self._config

    # ------------------------------------------------------------------
    # Config persistence
    # ------------------------------------------------------------------
    @property
    def cfg(self) -> Dict[str, dict]:
        """Backwards-compatible alias for the in-memory model config."""
        return self._config

    async def load(self) -> None:
        """Load config from ``plugin_settings`` and run the one-shot legacy migration."""
        raw: Any = None
        if self.db is not None:
            try:
                raw = await self.db.get_setting(self.CONFIG_SETTING_KEY, None)
            except Exception as exc:
                logger.warning(f"[OnlyYou] load model_config failed: {exc}")
        self._config = normalize_model_config(raw if raw is not None else {})
        self._loaded = True
        try:
            await self._migrate_legacy_config_once()
        except Exception as exc:
            logger.warning(f"[OnlyYou] legacy model config migration failed: {exc}")
        try:
            await self._migrate_config_to_souls_once()
        except Exception as exc:
            logger.warning(f"[OnlyYou] per-soul model config migration failed: {exc}")
        try:
            await self._migrate_role_layout_once()
        except Exception as exc:
            logger.warning(f"[OnlyYou] model role layout migration failed: {exc}")

    @staticmethod
    def _config_is_meaningful(cfg: Dict[str, dict]) -> bool:
        """Whether a normalized model config carries any explicit configuration."""
        if _has_pair(_pair_of(cfg.get("dialogue"))):
            return True
        for role in INHERITABLE_ROLES:
            if _has_pair(_pair_of(cfg.get(role))):
                return True
        tts = cfg.get("tts") or {}
        if _clean(tts.get("mode")).lower() not in ("", "off"):
            return True
        for role in VECTOR_ROLES:
            entry = cfg.get(role) or {}
            if _clean(entry.get("mode")).lower() not in ("", "off"):
                return True
        return False

    async def _migrate_config_to_souls_once(self) -> None:
        """One-shot migration of the legacy global config into every existing Soul.

        v2.8 hardening: ``model_config_migrated_to_souls`` is only persisted after
        every Soul was migrated without error.  Partial failures keep the flag unset
        so the next startup retries (idempotent: Souls that already carry a
        meaningful config are skipped).
        """
        if not self._soul_manager_ready() or self.db is None:
            return
        try:
            already = bool(await self.db.get_setting(self.SOUL_MIGRATION_FLAG_KEY, False))
        except Exception:
            already = False
        if already:
            return

        lister = getattr(self.soul_manager, "list_souls", None)
        saver = getattr(self.soul_manager, "save_soul", None)
        if not callable(lister):
            return
        souls = await lister()
        global_cfg = normalize_model_config(self._config)
        failures = 0
        if self._config_is_meaningful(global_cfg):
            migrated = 0
            for soul in souls:
                current = normalize_model_config(
                    getattr(soul, "model_config", None) or {}
                )
                if self._config_is_meaningful(current):
                    continue
                original_config = copy.deepcopy(
                    getattr(soul, "model_config", None) or {}
                )
                try:
                    setattr(soul, "model_config", copy.deepcopy(global_cfg))
                    if callable(saver):
                        await saver(soul)
                        migrated += 1
                except Exception as exc:  # noqa: BLE001
                    # Revert the in-memory mutation + drop the cached copy so the
                    # next startup retries instead of treating the Soul as migrated.
                    try:
                        setattr(soul, "model_config", original_config)
                    except Exception:
                        pass
                    cache = getattr(self.soul_manager, "_cache", None)
                    if isinstance(cache, dict):
                        cache.pop(getattr(soul, "soul_id", ""), None)
                    failures += 1
                    logger.warning(f"[OnlyYou] migrate soul model config failed: {exc}")
            if migrated:
                logger.info(
                    f"[OnlyYou] Migrated legacy global model_config into {migrated} Soul(s)."
                )
        if failures:
            logger.warning(
                "[OnlyYou] per-soul model_config migration incomplete "
                f"({failures} failure(s)); keeping the migration flag unset so the "
                "next startup retries."
            )
            return
        try:
            await self.db.set_setting(self.SOUL_MIGRATION_FLAG_KEY, True)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[OnlyYou] persist soul model migration flag failed: {exc}")

    async def _migrate_role_layout_once(self) -> None:
        """Persist the dialogue / main / decision layout once for old configs.

        ``SoulProfile`` normalizes ``model_config`` while loading, so by the time
        this runs every Soul already carries the migrated layout in memory; saving
        it once makes the JSON files match and removes the migration ambiguity on
        the next start.
        """
        if self.db is None:
            return
        try:
            already = bool(
                await self.db.get_setting(self.ROLE_LAYOUT_MIGRATION_KEY, False)
            )
        except Exception:
            already = False
        if already:
            return

        failures = 0
        raw_global = None
        try:
            raw_global = await self.db.get_setting(self.CONFIG_SETTING_KEY, None)
        except Exception:  # noqa: BLE001 - migration is best effort
            raw_global = None
        if isinstance(raw_global, dict) and _looks_like_legacy_model_layout(raw_global):
            try:
                await self.db.set_setting(
                    self.CONFIG_SETTING_KEY, normalize_model_config(raw_global)
                )
            except Exception as exc:  # noqa: BLE001 - retry next startup
                failures += 1
                logger.warning(
                    f"[OnlyYou] persist migrated global model_config failed: {exc}"
                )

        if self._soul_manager_ready():
            lister = getattr(self.soul_manager, "list_souls", None)
            saver = getattr(self.soul_manager, "save_soul", None)
            if callable(lister):
                try:
                    souls = await lister() or []
                except Exception as exc:  # noqa: BLE001
                    souls = []
                    logger.warning(f"[OnlyYou] list souls for model migration failed: {exc}")
                for soul in souls:
                    raw_soul = self._raw_soul_model_config(soul)
                    if not _looks_like_legacy_model_layout(raw_soul):
                        # Already new layout / unreadable: never rewrite the file.
                        continue
                    try:
                        setattr(
                            soul,
                            "model_config",
                            normalize_model_config(raw_soul),
                        )
                        if callable(saver):
                            await saver(soul)
                    except Exception as exc:  # noqa: BLE001
                        failures += 1
                        logger.warning(
                            f"[OnlyYou] migrate soul model roles failed: {exc}"
                        )
        if failures:
            logger.warning(
                "[OnlyYou] model role layout migration incomplete "
                f"({failures} failure(s)); keeping the flag unset so the next "
                "startup retries."
            )
            return
        try:
            await self.db.set_setting(self.ROLE_LAYOUT_MIGRATION_KEY, True)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[OnlyYou] persist role layout migration flag failed: {exc}")

    def _raw_soul_model_config(self, soul: Any) -> Any:
        """Read a Soul's on-disk ``model_config`` (the in-memory copy is normalized)."""
        getter = getattr(self.soul_manager, "soul_file_path", None)
        soul_id = _clean(getattr(soul, "soul_id", ""))
        if not callable(getter) or not soul_id:
            return None
        try:
            path = getter(soul_id)
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception:  # noqa: BLE001 - migration is best effort
            return None
        if not isinstance(payload, dict):
            return None
        return payload.get("model_config")

    async def _migrate_legacy_config_once(self) -> None:
        """One-shot migration of ``model_hub_config`` provider ids -> (source, model)."""
        if self.db is None:
            return
        try:
            already = bool(await self.db.get_setting(self.MIGRATION_FLAG_KEY, False))
        except Exception:
            already = False
        if already:
            return

        legacy: Dict[str, Any] = {}
        try:
            raw_legacy = self.plugin_config.get("model_hub_config", {})
            if isinstance(raw_legacy, dict):
                legacy = raw_legacy
        except Exception:
            legacy = {}

        new_config = copy.deepcopy(self._config)
        dialogue_pair = await self._resolve_legacy_provider_id(
            legacy.get("main_llm_provider_id")
        )
        if dialogue_pair is not None:
            new_config["dialogue"] = {
                "source_id": dialogue_pair[0],
                "model": dialogue_pair[1],
            }
            decision_pair = await self._resolve_legacy_provider_id(
                legacy.get("small_llm_provider_id")
            )
            if decision_pair is not None:
                # Generation role (主模型) keeps the old lightweight model; the new
                # read-the-air role (决策模型) starts from the same effective model.
                new_config["main"] = {
                    "inherit": False,
                    "inherit_from": "",
                    "source_id": decision_pair[0],
                    "model": decision_pair[1],
                }
                new_config["decision"] = {
                    "inherit": True,
                    "inherit_from": "main",
                    "source_id": "",
                    "model": "",
                    "mode": "chat",
                    "threshold": DEFAULT_DECISION_THRESHOLD,
                    "endpoint": "",
                }
            vision_pair = await self._resolve_legacy_provider_id(
                legacy.get("vision_llm_provider_id")
            )
            if vision_pair is not None and vision_pair != (decision_pair or dialogue_pair):
                new_config["vision"] = {
                    "inherit": False,
                    "inherit_from": "",
                    "source_id": vision_pair[0],
                    "model": vision_pair[1],
                }

        has_current_dialogue = _has_pair(_pair_of(self._config.get("dialogue")))
        if dialogue_pair is not None and not has_current_dialogue:
            self._config = normalize_model_config(new_config)
            try:
                await self.db.set_setting(self.CONFIG_SETTING_KEY, self._config)
                logger.info(
                    "[OnlyYou] migrated legacy model_hub_config -> model_config "
                    f"({self._config['dialogue'].get('source_id')}/"
                    f"{self._config['dialogue'].get('model')})"
                )
            except Exception as exc:
                logger.warning(f"[OnlyYou] persist migrated model_config failed: {exc}")

        # Only ever attempt the migration once, whether it succeeded or not.
        try:
            await self.db.set_setting(self.MIGRATION_FLAG_KEY, True)
        except Exception as exc:
            logger.debug(f"[OnlyYou] persist migration flag failed: {exc}")

    async def _resolve_legacy_provider_id(
        self, provider_id: Any
    ) -> Optional[tuple]:
        provider_id = _clean(provider_id)
        if not provider_id or self.provider_factory is None:
            return None
        finder = getattr(
            self.provider_factory, "find_source_model_by_provider_id", None
        )
        if not callable(finder):
            return None
        try:
            pair = await finder(provider_id)
        except Exception as exc:
            logger.warning(
                f"[OnlyYou] reverse lookup legacy provider {provider_id} failed: {exc}"
            )
            return None
        if not pair or len(pair) < 2:
            return None
        source_id = _clean(pair[0])
        model = _clean(pair[1])
        if not source_id or not model:
            return None
        return (source_id, model)

    def get_model_config(self) -> Dict[str, dict]:
        """Return the (default Soul's) model config; sync, cache-based."""
        return copy.deepcopy(self._sync_active_config())

    def get_config_sync(self, soul_id: Optional[str] = None) -> Dict[str, dict]:
        """Deprecated compatibility alias of :meth:`get_config` (sync path)."""
        return copy.deepcopy(self._sync_active_config(soul_id))

    async def get_config(self, soul_id: Optional[str] = None) -> Dict[str, dict]:
        """Return the four-role model config of one Soul (default Soul when empty)."""
        return copy.deepcopy(await self._active_config(soul_id))

    async def get_model_config_async(self, soul_id: Optional[str] = None) -> Dict[str, dict]:
        if not self._soul_manager_ready() and not self._loaded:
            await self.load()
        return await self.get_config(soul_id)

    async def save_config(self, new_config: dict, soul_id: Optional[str] = None) -> Dict[str, dict]:
        """Persist a Soul's four-role model config (v2.8), rebuilding main provider."""
        return await self.save_model_config(new_config, soul_id=soul_id)

    async def save_model_config(
        self, cfg: dict, soul_id: Optional[str] = None
    ) -> Dict[str, dict]:
        """Normalize + persist model config, then drop that Soul's main provider.

        With a SoulManager attached the config is written into the Soul's own JSON
        file; without one the legacy global ``plugin_settings`` key is used (keeps
        the pre-v2.8 contract and its tests intact).
        """
        normalized = normalize_model_config(cfg)
        sid = await self._resolve_soul_id(soul_id)
        if self._soul_manager_ready() and sid:
            soul = await self._ensure_soul_cached(sid)
            if soul is not None:
                try:
                    setattr(soul, "model_config", copy.deepcopy(normalized))
                    saver = getattr(self.soul_manager, "save_soul", None)
                    if callable(saver):
                        await saver(soul)
                except Exception as exc:
                    logger.warning(f"[OnlyYou] save soul model_config failed: {exc}")
                self._config = normalized
                self._loaded = True
                self._unregister_main_for_soul(sid)
                return copy.deepcopy(normalized)

        self._config = normalized
        self._loaded = True
        if self.db is not None:
            try:
                await self.db.set_setting(self.CONFIG_SETTING_KEY, normalized)
            except Exception as exc:
                logger.warning(f"[OnlyYou] save model_config failed: {exc}")
        self._unregister_main_for_soul(sid)
        return copy.deepcopy(normalized)

    def _unregister_main_for_soul(self, soul_id: str = "") -> None:
        """Drop the plugin-owned main provider registration of one Soul (or all)."""
        if self.provider_factory is None:
            return
        unregister = getattr(self.provider_factory, "unregister_main", None)
        if not callable(unregister):
            return
        try:
            if soul_id:
                unregister(f"{self.MAIN_PROVIDER_ID}::{soul_id}")
            else:
                unregister()
        except TypeError:
            try:
                unregister()
            except Exception as exc:
                logger.warning(f"[OnlyYou] unregister_main failed: {exc}")
        except Exception as exc:
            logger.warning(f"[OnlyYou] unregister_main failed: {exc}")

    # ------------------------------------------------------------------
    # Role resolution
    # ------------------------------------------------------------------
    @staticmethod
    def _normalize_role(role: Any) -> str:
        name = _clean(role).lower()
        # Legacy aliases: ``small`` / ``aux`` used to mean the old decision role,
        # which is the generation role (主模型) in the current layout.
        if name in ("small", "aux", "auxiliary"):
            return "main"
        if name in ("chat", "dialog", "dialogue_model"):
            return "dialogue"
        if name in ("atmosphere", "read_the_air", "judge_model"):
            return "decision"
        if name in ModelHub.ROLES:
            return name
        return ""

    def _own_chat_role(
        self, role: str, cfg: Optional[Dict[str, dict]] = None
    ) -> Optional[Dict[str, str]]:
        """Explicitly configured ``(source_id, model)`` of one chat role, if any."""
        active = cfg if isinstance(cfg, dict) else self._config
        source_id, model = _pair_of(active.get(role))
        if source_id and model:
            return {"source_id": source_id, "model": model}
        return None

    def _dialogue_pair(
        self, cfg: Optional[Dict[str, dict]] = None
    ) -> Optional[Dict[str, str]]:
        """Explicit ``(source_id, model)`` of the dialogue role (inheritance root)."""
        return self._own_chat_role("dialogue", cfg=cfg)

    def _inherit_from(
        self, role: str, cfg: Optional[Dict[str, dict]] = None
    ) -> str:
        """Normalized inheritance target of a chat role (``""`` == independent)."""
        active = cfg if isinstance(cfg, dict) else self._config
        entry = active.get(role) or {}
        value = _clean(entry.get("inherit_from")).lower()
        return value if value in INHERIT_FROM_OPTIONS.get(role, ("",)) else ""

    def _resolve_chat_role(
        self,
        role: str,
        hops: int = 0,
        cfg: Optional[Dict[str, dict]] = None,
    ) -> Optional[Dict[str, str]]:
        """Resolve ``main`` / ``decision`` / ``vision`` through optional inheritance.

        * inheritance disabled (``inherit_from=""``) -> own config only; an
          incomplete own config returns ``None`` (**never** silently falls back);
        * ``inherit_from="dialogue"`` -> the dialogue pair (inheritance root);
        * otherwise recurse, at most ``MAX_INHERIT_HOPS`` hops and cycle-safe
          (``vision -> main -> dialogue`` is exactly 2 hops).
        """
        inherit_from = self._inherit_from(role, cfg=cfg)
        if not inherit_from:
            return self._own_chat_role(role, cfg=cfg)
        if inherit_from == "dialogue":
            return self._dialogue_pair(cfg=cfg)
        if inherit_from in INHERIT_FROM_OPTIONS and hops < MAX_INHERIT_HOPS:
            return self._resolve_chat_role(inherit_from, hops + 1, cfg=cfg)
        # >2 hops / self-cycle / unknown parent role -> disabled (no fallback).
        return None

    def _resolve_vector_role(
        self, role: str, cfg: Optional[Dict[str, dict]] = None
    ) -> Optional[Dict[str, str]]:
        """Resolve ``embedding`` / ``rerank`` into a provider selection dict.

        ``off`` (or an incomplete ``provider`` / ``source`` config) returns ``None``
        — the memory engine then degrades to lexical retrieval, never to ``main``.
        """
        active = cfg if isinstance(cfg, dict) else self._config
        entry = active.get(role) or {}
        mode = _clean(entry.get("mode")).lower() or "off"
        if mode == "provider":
            provider_id = _clean(entry.get("provider_id"))
            if not provider_id:
                return None
            return {"mode": "provider", "provider_id": provider_id}
        if mode == "source":
            source_id = _clean(entry.get("source_id"))
            model = _clean(entry.get("model"))
            if not source_id or not model:
                return None
            return {"mode": "source", "source_id": source_id, "model": model}
        return None

    async def resolve_role(
        self, role: str, soul_id: Optional[str] = None
    ) -> Optional[dict]:
        """Resolve a role into a concrete provider selection dict (or ``None``).

        v2.8: the four-role config is read from the Soul's own JSON file
        (``soul_id=None`` -> default Soul; no SoulManager -> legacy global config).
        """
        name = self._normalize_role(role)
        if not name:
            return None

        cfg = await self._active_config(soul_id)

        if name == "dialogue":
            return self._dialogue_pair(cfg=cfg)

        if name in INHERITABLE_ROLES:
            return self._resolve_chat_role(name, cfg=cfg)

        # v2.12: embedding / rerank never inherit; ``off`` disables the role.
        if name in VECTOR_ROLES:
            return self._resolve_vector_role(name, cfg=cfg)

        # tts never inherits
        tts = cfg.get("tts") or {}
        mode = _clean(tts.get("mode")).lower() or "off"
        if mode == "off":
            return None
        if mode == "provider":
            provider_id = _clean(tts.get("provider_id"))
            if not provider_id:
                return None
            return {"mode": "provider", "provider_id": provider_id}
        if mode == "source":
            source_id = _clean(tts.get("source_id"))
            model = _clean(tts.get("model"))
            if not source_id or not model:
                return None
            return {
                "mode": "source",
                "source_id": source_id,
                "model": model,
                "voice": _clean(tts.get("voice")),
            }
        return None

    # ------------------------------------------------------------------
    # Provider access
    # ------------------------------------------------------------------
    async def get_provider(
        self,
        role: str,
        umo: Optional[str] = None,
        soul_id: Optional[str] = None,
    ) -> Any:
        """Return the provider instance for ``role`` (``None`` when unavailable)."""
        name = self._normalize_role(role)
        if not name:
            return None
        factory = self.provider_factory
        if factory is None:
            logger.warning("[OnlyYou] provider_factory unavailable")
            return None

        resolved_soul_id = await self._resolve_soul_id(soul_id, umo=umo)
        try:
            if name == "decision":
                # 判断模型（Decisions API）没有可复用的聊天 Provider；读空气会走
                # ``judge_choice``，这里返回 None 而不是误建一个聊天实例。
                cfg = await self._active_config(resolved_soul_id or soul_id)
                entry = cfg.get("decision") or {}
                if _clean(entry.get("mode")).lower() == "judge":
                    logger.debug(
                        "[OnlyYou] decision role is in judge mode; no chat provider"
                    )
                    return None
            if name in VECTOR_ROLES:
                resolved = await self.resolve_role(
                    name, soul_id=resolved_soul_id or soul_id
                )
                if resolved is None:
                    return None
                getter = getattr(
                    factory,
                    "get_embedding_provider"
                    if name == "embedding"
                    else "get_rerank_provider",
                    None,
                )
                if not callable(getter):
                    return None
                return await getter(resolved)

            if name == "tts":
                resolved = await self.resolve_role("tts", soul_id=resolved_soul_id or soul_id)
                if resolved is None:
                    return None
                get_tts = getattr(factory, "get_tts_provider", None)
                if not callable(get_tts):
                    return None
                return await get_tts(self.get_tts_config(resolved_soul_id or soul_id))

            resolved = await self.resolve_role(name, soul_id=resolved_soul_id or soul_id)
            if resolved is None:
                return None
            get_chat = getattr(factory, "get_chat_provider", None)
            if not callable(get_chat):
                return None
            return await get_chat(resolved["source_id"], resolved["model"])
        except Exception as exc:
            logger.warning(f"[OnlyYou] get_provider(role={name}) failed: {exc}")
            return None

    # ------------------------------------------------------------------
    # v2.12: Embedding / Rerank calls (RAG memory retrieval)
    # ------------------------------------------------------------------
    def get_embedding_config(self, soul_id: Optional[str] = None) -> dict:
        """Return a copy of a Soul's normalized embedding role config."""
        cfg = self._sync_active_config(soul_id)
        return copy.deepcopy(cfg.get("embedding") or DEFAULT_MODEL_CONFIG["embedding"])

    def get_rerank_config(self, soul_id: Optional[str] = None) -> dict:
        """Return a copy of a Soul's normalized rerank role config."""
        cfg = self._sync_active_config(soul_id)
        return copy.deepcopy(cfg.get("rerank") or DEFAULT_MODEL_CONFIG["rerank"])

    @staticmethod
    def _normalize_vector(raw: Any) -> List[float]:
        """Coerce one embedding payload into a list of finite floats."""
        if isinstance(raw, dict):
            raw = raw.get("embedding") or raw.get("vector") or raw.get("data")
        if not isinstance(raw, (list, tuple)):
            return []
        vector: List[float] = []
        for value in raw:
            if isinstance(value, bool):
                return []
            try:
                number = float(value)
            except (TypeError, ValueError):
                return []
            if number != number or number in (float("inf"), float("-inf")):
                return []
            vector.append(number)
        return vector

    @classmethod
    def _normalize_vectors(cls, raw: Any, expected: int) -> List[List[float]]:
        """Coerce a batch embedding payload into ``expected`` aligned vectors."""
        if raw is None:
            return []
        payload = raw
        if isinstance(payload, dict):
            payload = payload.get("data") or payload.get("embeddings") or payload
        if not isinstance(payload, (list, tuple)):
            return []
        vectors: List[List[float]] = []
        for item in payload:
            vector = cls._normalize_vector(item)
            if not vector:
                return []
            vectors.append(vector)
        if expected > 0 and len(vectors) != expected:
            # Some adapters wrap a single vector in an extra list.
            if expected == 1 and len(vectors) == 1:
                return vectors
            return []
        return vectors

    @staticmethod
    async def _maybe_await(value: Any) -> Any:
        if inspect.isawaitable(value):
            return await value
        return value

    async def get_embeddings(
        self,
        texts: List[str],
        soul_id: Optional[str] = None,
        umo: Optional[str] = None,
    ) -> List[List[float]]:
        """Batch-embed ``texts`` via the configured embedding role.

        Returns ``[]`` whenever the role is off / unconfigured / failed, so every
        caller can degrade to lexical retrieval without exception handling.
        """
        clean_texts = [str(text or "").strip() for text in (texts or [])]
        clean_texts = [text for text in clean_texts if text]
        if not clean_texts:
            return []
        try:
            provider = await self.get_provider(
                "embedding", umo=umo, soul_id=soul_id
            )
        except Exception as exc:  # noqa: BLE001 - degrade to lexical
            logger.debug(f"[OnlyYou] embedding provider unavailable: {exc}")
            return []
        if provider is None:
            return []

        batch_fn = getattr(provider, "get_embeddings", None)
        if callable(batch_fn):
            try:
                raw = await self._maybe_await(batch_fn(clean_texts))
                vectors = self._normalize_vectors(raw, len(clean_texts))
                if vectors:
                    return vectors
            except Exception as exc:  # noqa: BLE001 - fall back to single calls
                logger.debug(f"[OnlyYou] get_embeddings failed, falling back: {exc}")
        single_fn = getattr(provider, "get_embedding", None)
        if not callable(single_fn):
            return []
        vectors: List[List[float]] = []
        for text in clean_texts:
            try:
                raw = await self._maybe_await(single_fn(text))
            except Exception as exc:  # noqa: BLE001 - degrade to lexical
                logger.debug(f"[OnlyYou] get_embedding failed: {exc}")
                return []
            vector = self._normalize_vector(raw)
            if not vector:
                return []
            vectors.append(vector)
        return vectors

    async def get_embedding(
        self,
        text: str,
        soul_id: Optional[str] = None,
        umo: Optional[str] = None,
    ) -> List[float]:
        """Embed one text (``[]`` when the embedding role is disabled/failed)."""
        vectors = await self.get_embeddings([text], soul_id=soul_id, umo=umo)
        return vectors[0] if vectors else []

    async def rerank_documents(
        self,
        query: str,
        documents: List[str],
        top_n: Optional[int] = None,
        soul_id: Optional[str] = None,
        umo: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Rerank ``documents`` against ``query`` via the rerank role.

        Returns ``[{"index": int, "score": float}, ...]`` sorted by score (desc) or
        ``[]`` when the role is off / unconfigured / failed.
        """
        docs = [str(doc or "") for doc in (documents or [])]
        if not docs or not str(query or "").strip():
            return []
        try:
            provider = await self.get_provider("rerank", umo=umo, soul_id=soul_id)
        except Exception as exc:  # noqa: BLE001 - degrade to dense/lexical
            logger.debug(f"[OnlyYou] rerank provider unavailable: {exc}")
            return []
        if provider is None:
            return []
        rerank_fn = getattr(provider, "rerank", None)
        if not callable(rerank_fn):
            return []
        limit = top_n if isinstance(top_n, int) and top_n > 0 else len(docs)
        raw: Any = None
        for kwargs in (
            {"query": query, "documents": docs, "top_n": limit},
            {"query": query, "documents": docs},
        ):
            try:
                raw = await self._maybe_await(rerank_fn(**kwargs))
                break
            except TypeError:
                continue
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[OnlyYou] rerank failed: {exc}")
                return []
        if raw is None:
            try:
                raw = await self._maybe_await(rerank_fn(query, docs, limit))
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[OnlyYou] rerank failed: {exc}")
                return []
        if not isinstance(raw, (list, tuple)):
            return []
        results: List[Dict[str, Any]] = []
        for position, item in enumerate(raw):
            if isinstance(item, dict):
                index = item.get("index", item.get("idx", position))
                score = item.get("relevance_score", item.get("score", item.get("relevance")))
            else:
                index = getattr(item, "index", position)
                score = getattr(
                    item, "relevance_score", getattr(item, "score", None)
                )
            try:
                index_value = int(index)
                score_value = float(score)
            except (TypeError, ValueError):
                continue
            if index_value < 0 or index_value >= len(docs):
                continue
            results.append({"index": index_value, "score": score_value})
        results.sort(key=lambda entry: entry["score"], reverse=True)
        return results[:limit]

    @staticmethod
    def _provider_meta(instance: Any) -> tuple:
        """Return ``(provider_id, model)`` of a provider instance."""
        try:
            meta = instance.meta()
            provider_id = _clean(getattr(meta, "id", ""))
            model = _clean(getattr(meta, "model", ""))
            if provider_id or model:
                return provider_id, model
        except Exception:
            pass
        config = getattr(instance, "provider_config", None)
        if isinstance(config, dict):
            return _clean(config.get("id")), _clean(config.get("model"))
        return "", ""

    async def get_main_provider_id(self, soul_id: Optional[str] = None) -> str:
        """Resolve the ``selected_provider`` id used by AstrBot's native pipeline.

        The native pipeline always uses the **dialogue** role (对话模型); the
        generation role (主模型) is never registered as a chat provider.

        * ``""``                  — dialogue model not configured / build failed
          (plugin falls back to AstrBot's default model).
        * existing AstrBot id    — an already loaded instance was reused.
        * ``"onlyyou::main"``    — a plugin-owned instance was registered into
          ``provider_manager.inst_map``.
        * ``"onlyyou::main::{soul_id}"`` — v2.8 per-Soul plugin-owned instance.
        """
        try:
            resolved_soul_id = await self._resolve_soul_id(soul_id)
            resolved = await self.resolve_role(
                "dialogue", soul_id=resolved_soul_id or soul_id
            )
            if resolved is None:
                return ""
            factory = self.provider_factory
            if factory is None:
                return ""
            get_chat = getattr(factory, "get_chat_provider", None)
            if not callable(get_chat):
                return ""
            instance = await get_chat(resolved["source_id"], resolved["model"])
            if instance is None:
                return ""

            owned: Optional[bool] = None
            checker = getattr(factory, "is_owned_instance", None)
            if callable(checker):
                try:
                    owned = bool(checker(instance))
                except Exception:
                    owned = None
            provider_id, _model = self._provider_meta(instance)
            if owned is None:
                owned = (not provider_id) or provider_id.startswith("onlyyou::")

            if not owned:
                loaded_id = ""
                id_finder = getattr(factory, "find_loaded_provider_id", None)
                if callable(id_finder):
                    try:
                        loaded_id = _clean(id_finder(instance))
                    except Exception:
                        loaded_id = ""
                if not loaded_id:
                    loaded_id = provider_id
                return loaded_id

            register = getattr(factory, "register_main", None)
            if not callable(register):
                return ""
            provider_id = self.MAIN_PROVIDER_ID
            if self._soul_manager_ready() and resolved_soul_id:
                provider_id = f"{self.MAIN_PROVIDER_ID}::{resolved_soul_id}"
            try:
                try:
                    return _clean(register(instance, provider_id))
                except TypeError:
                    return _clean(register(instance))
            except Exception as exc:
                logger.warning(f"[OnlyYou] register_main failed: {exc}")
                return ""
        except Exception as exc:
            logger.warning(f"[OnlyYou] get_main_provider_id failed: {exc}")
            return ""

    # ------------------------------------------------------------------
    # LLM calls
    # ------------------------------------------------------------------
    async def call_llm_text(
        self,
        prompt: str,
        role: str = "main",
        umo: Optional[str] = None,
        system_prompt: str = "",
        image_urls: Optional[List[str]] = None,
        soul_id: Optional[str] = None,
    ) -> str:
        """Call ``inst.text_chat()`` directly and return the completion text."""
        name = self._normalize_role(role)
        if name == "" or name == "tts":
            logger.warning(f"[OnlyYou] call_llm_text: invalid role {role}")
            return ""
        provider = await self.get_provider(name, umo=umo, soul_id=soul_id)
        if provider is None:
            logger.warning(f"[OnlyYou] No provider available for role={name}")
            return ""
        try:
            response = await provider.text_chat(
                prompt=prompt,
                system_prompt=system_prompt or None,
                image_urls=image_urls or None,
            )
        except Exception as exc:
            logger.warning(f"[OnlyYou] text_chat via role={name} failed: {exc}")
            return ""
        text = getattr(response, "completion_text", None)
        if text is None:
            text = response if isinstance(response, str) else ""
        try:
            return str(text).strip()
        except Exception:
            return ""

    async def call_llm_json(
        self,
        prompt: str,
        role: str = "main",
        umo: Optional[str] = None,
        system_prompt: str = "",
        image_urls: Optional[List[str]] = None,
        default: Any = None,
        soul_id: Optional[str] = None,
    ) -> Any:
        """Call the LLM and parse the first JSON object/array from the answer."""
        raw = await self.call_llm_text(
            prompt=prompt,
            role=role,
            umo=umo,
            system_prompt=system_prompt,
            image_urls=image_urls,
            soul_id=soul_id,
        )
        if not raw:
            return default
        parsed = self.extract_json(raw)
        return parsed if parsed is not None else default

    @staticmethod
    def extract_json(text: str) -> Any:
        """Robustly extract a JSON object or array from LLM markdown/text output."""
        if not text:
            return None
        cleaned = text.strip()
        # Strip <think>...</think> blocks if present
        cleaned = re.sub(r"<think>.*?</think>", "", cleaned, flags=re.DOTALL).strip()

        # Try fenced code block first
        fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", cleaned)
        if fence_match:
            candidate = fence_match.group(1).strip()
            try:
                return json.loads(candidate)
            except Exception:
                pass

        # Direct parse
        try:
            return json.loads(cleaned)
        except Exception:
            pass

        # Find outermost { ... } or [ ... ]
        for open_ch, close_ch in (("{", "}"), ("[", "]")):
            start = cleaned.find(open_ch)
            end = cleaned.rfind(close_ch)
            if start != -1 and end != -1 and end > start:
                try:
                    return json.loads(cleaned[start : end + 1])
                except Exception:
                    continue

        return None

    # ------------------------------------------------------------------
    # Decision role (read-the-air) — chat JSON / judge-style Decisions API
    # ------------------------------------------------------------------
    @staticmethod
    def verdict_is_reply(result: Optional[dict]) -> bool:
        """Apply the configured threshold to a normalized judge result."""
        if not isinstance(result, dict):
            return False
        if _clean(result.get("decision")).upper() != "REPLY":
            return False
        threshold = result.get("threshold")
        confidence = result.get("confidence")
        if threshold is None or confidence is None:
            return True
        try:
            return float(confidence) >= float(threshold)
        except (TypeError, ValueError):
            return True

    async def judge_choice(
        self,
        *,
        prompt: str,
        state: Dict[str, Any],
        instructions: str,
        criteria: Dict[str, str],
        umo: Optional[str] = None,
        soul_id: Optional[str] = None,
        question_key: str = "decision",
    ) -> Optional[dict]:
        """Make one read-the-air judgment with the **decision** role.

        ``mode="chat"`` calls the configured chat model with ``prompt`` and parses
        its JSON verdict. ``mode="judge"`` calls the OpenRouter Decisions API with
        ``state`` + one typed ``choice`` question (Jev and other System-One
        models). Returns ``{decision, confidence, probabilities, mode, threshold}``
        or ``None`` when the role is not usable — callers fall back to heuristics.
        """
        try:
            sid = await self._resolve_soul_id(soul_id, umo=umo)
            cfg = await self._active_config(sid or soul_id)
            entry = cfg.get("decision") or {}
            mode, threshold, _endpoint = _normalize_decision_fields(entry)
            if mode == "judge":
                pair = self._resolve_chat_role("decision", cfg=cfg)
                if pair is None:
                    logger.warning(
                        "[OnlyYou] decision judge model 未配置，跳过读空气判断"
                    )
                    return None
                credentials = self._decision_credentials(pair, entry)
                if not credentials.get("api_key"):
                    logger.warning(
                        "[OnlyYou] decision judge model 缺少 API Key"
                        "（提供商源或 %s）",
                        OPENROUTER_KEY_ENV,
                    )
                    return None
                try:
                    result = await decision_model.judge_choice(
                        endpoint=credentials["endpoint"],
                        api_key=credentials["api_key"],
                        model=pair["model"],
                        state=state,
                        instructions=instructions,
                        criteria=criteria,
                        question_key=question_key,
                    )
                except Exception as exc:  # noqa: BLE001 - degrade to heuristic
                    logger.warning(f"[OnlyYou] decision judge call failed: {exc}")
                    return None
                result["threshold"] = threshold
                result["endpoint"] = credentials["endpoint"]
                result["key_source"] = credentials.get("key_source", "")
                return result

            raw = await self.call_llm_json(
                prompt=prompt,
                role="decision",
                umo=umo,
                soul_id=sid or soul_id,
                default=None,
            )
            if not isinstance(raw, dict):
                return None
            decided = _clean(raw.get("decision")).upper()
            if not decided:
                return None
            try:
                confidence = float(raw.get("confidence"))
            except (TypeError, ValueError):
                confidence = None
            return {
                "decision": decided,
                "confidence": confidence,
                "reason": _clean(raw.get("reason")),
                "probabilities": {},
                "mode": "chat",
                # Chat-mode confidence is model self-reporting; the judge threshold
                # only applies to typed probability answers.
                "threshold": None,
            }
        except Exception as exc:  # noqa: BLE001 - never break the message gate
            logger.warning(f"[OnlyYou] judge_choice failed: {exc}")
            return None

    async def _test_decision_judge(self, soul_id: Optional[str] = None) -> dict:
        """Connectivity test for a judge-mode decision role."""
        started = time.time()
        result = await self.judge_choice(
            prompt="",
            state={
                "task": "connectivity test",
                "question": "这是一次连通性测试：是否应该返回 REPLY？",
            },
            instructions="这是一次连通性测试，请选择 REPLY。",
            criteria={
                "REPLY": "连通性测试应选择此项。",
                "IGNORE": "仅在判断模型不可用时选择。",
            },
            soul_id=soul_id,
        )
        if not result:
            return {
                "status": "error",
                "role": "decision",
                "mode": "judge",
                "error": "判断模型调用失败：请检查模型名 / 提供商源 / API Key",
            }
        return {
            "status": "ok",
            "role": "decision",
            "mode": "judge",
            "model": result.get("model", ""),
            "provider": result.get("provider", ""),
            "decision": result.get("decision", ""),
            "confidence": result.get("confidence"),
            "threshold": result.get("threshold"),
            "latency_ms": int((time.time() - started) * 1000),
        }

    # ------------------------------------------------------------------
    # Diagnostics / testing
    # ------------------------------------------------------------------
    async def test_role(self, role: str, soul_id: Optional[str] = None) -> dict:
        """Send a ping (or a short TTS sentence) and report latency / result."""
        name = self._normalize_role(role)
        if not name:
            return {"status": "error", "role": _clean(role), "error": "未知模型角色"}
        if name == "tts":
            return await self._test_tts(soul_id=soul_id)
        if name in VECTOR_ROLES:
            return await self._test_vector_role(name, soul_id=soul_id)
        if name == "decision":
            cfg = await self._active_config(soul_id)
            entry = cfg.get("decision") or {}
            if _clean(entry.get("mode")).lower() == "judge":
                return await self._test_decision_judge(soul_id=soul_id)

        started = time.time()
        try:
            provider = await self.get_provider(name, soul_id=soul_id)
        except Exception as exc:
            return {"status": "error", "role": name, "error": str(exc)}
        if provider is None:
            return {
                "status": "error",
                "role": name,
                "error": "未配置模型或提供商实例创建失败",
            }
        provider_id, model = self._provider_meta(provider)
        try:
            response = await provider.text_chat(
                prompt="ping，请只回复 pong",
                system_prompt=None,
            )
            text = getattr(response, "completion_text", None)
            if text is None and isinstance(response, str):
                text = response
            reply = str(text or "").strip()
            latency_ms = int((time.time() - started) * 1000)
            return {
                "status": "ok",
                "role": name,
                "provider_id": provider_id,
                "model": model,
                "latency_ms": latency_ms,
                "reply": reply[:500],
            }
        except Exception as exc:
            return {"status": "error", "role": name, "error": str(exc)}

    async def _test_vector_role(
        self, role: str, soul_id: Optional[str] = None
    ) -> dict:
        """Connectivity test for ``embedding`` / ``rerank`` (v2.12)."""
        started = time.time()
        config = (
            self.get_embedding_config(soul_id)
            if role == "embedding"
            else self.get_rerank_config(soul_id)
        )
        if _clean(config.get("mode")) in ("", "off"):
            return {
                "status": "error",
                "role": role,
                "error": f"{VECTOR_ROLE_LABELS.get(role, role)}未启用",
            }
        provider = await self.get_provider(role, soul_id=soul_id)
        if provider is None:
            return {
                "status": "error",
                "role": role,
                "error": "未配置模型或提供商实例创建失败",
            }
        provider_id, model = self._provider_meta(provider)

        def _elapsed_ms() -> int:
            return int((time.time() - started) * 1000)
        if role == "embedding":
            vectors = await self.get_embeddings(
                ["OnlyYou 记忆检索连通性测试"], soul_id=soul_id
            )
            if not vectors:
                return {
                    "status": "error",
                    "role": role,
                    "error": "向量模型未返回有效向量",
                }
            return {
                "status": "ok",
                "role": role,
                "provider_id": provider_id,
                "model": model,
                "dimension": len(vectors[0]),
                "latency_ms": _elapsed_ms(),
            }
        results = await self.rerank_documents(
            "火锅", ["阿泽最爱川渝火锅", "今天天气不错"], top_n=2, soul_id=soul_id
        )
        if not results:
            return {
                "status": "error",
                "role": role,
                "error": "重排模型未返回有效结果",
            }
        return {
            "status": "ok",
            "role": role,
            "provider_id": provider_id,
            "model": model,
            "results": [
                {
                    "index": entry["index"],
                    "score": round(float(entry["score"]), 4),
                }
                for entry in results
            ],
            "latency_ms": _elapsed_ms(),
        }

    async def _test_tts(self, soul_id: Optional[str] = None) -> dict:
        started = time.time()
        cfg = self.get_tts_config(soul_id)
        if _clean(cfg.get("mode")) == "off":
            return {"status": "error", "role": "tts", "error": "TTS 未开启"}
        try:
            provider = await self.get_provider("tts", soul_id=soul_id)
        except Exception as exc:
            return {"status": "error", "role": "tts", "error": str(exc)}
        if provider is None:
            return {"status": "error", "role": "tts", "error": "TTS 提供商不可用"}
        try:
            get_audio = getattr(provider, "get_audio", None)
            if not callable(get_audio):
                return {"status": "error", "role": "tts", "error": "提供商不支持语音合成"}
            audio_path = await get_audio("你好呀，我是 OnlyYou，这是一段语音测试。")
        except Exception as exc:
            return {"status": "error", "role": "tts", "error": str(exc)}
        if not audio_path:
            return {"status": "error", "role": "tts", "error": "语音合成未返回音频文件"}
        latency_ms = int((time.time() - started) * 1000)
        audio_base64 = ""
        try:
            with open(str(audio_path), "rb") as file_handle:
                audio_base64 = base64.b64encode(file_handle.read()).decode("ascii")
        except Exception as exc:
            logger.warning(f"[OnlyYou] read TTS audio failed: {exc}")
        return {
            "status": "ok",
            "role": "tts",
            "latency_ms": latency_ms,
            "audio_path": str(audio_path),
            "audio_base64": audio_base64,
        }

    async def synthesize(self, text: str, soul_id: Optional[str] = None) -> Optional[str]:
        """Synthesize ``text`` into an audio file path (``None`` when off/failed)."""
        if not text or not str(text).strip():
            return None
        cfg = self.get_tts_config(soul_id)
        if _clean(cfg.get("mode")) == "off":
            return None
        try:
            provider = await self.get_provider("tts", soul_id=soul_id)
            if provider is None:
                return None
            get_audio = getattr(provider, "get_audio", None)
            if not callable(get_audio):
                return None
            audio_path = await get_audio(str(text).strip())
        except Exception as exc:
            logger.warning(f"[OnlyYou] synthesize failed: {exc}")
            return None
        if not audio_path:
            return None
        return str(audio_path)

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------
    def get_tts_config(self, soul_id: Optional[str] = None) -> dict:
        """Return a copy of a Soul's normalized TTS config (default Soul when empty)."""
        cfg = self._sync_active_config(soul_id)
        return copy.deepcopy(cfg.get("tts") or DEFAULT_MODEL_CONFIG["tts"])

    def _find_reusable(self, source_id: str, model: str) -> Any:
        factory = self.provider_factory
        if factory is None:
            return None
        finder = getattr(factory, "find_reusable_chat_provider", None)
        if not callable(finder):
            return None
        try:
            return finder(source_id, model)
        except Exception:
            return None

    def _chat_role_status(
        self,
        role: str,
        cfg: Optional[Dict[str, dict]] = None,
        soul_id: str = "",
    ) -> dict:
        active = cfg if isinstance(cfg, dict) else self._config
        inherit_from = ""
        inherited = False
        if role == "dialogue":
            configured_pair = self._dialogue_pair(cfg=active)
        else:
            inherit_from = self._inherit_from(role, cfg=active)
            if inherit_from:
                configured_pair = self._resolve_chat_role(role, cfg=active)
                # ``inherited`` is True only when enabled inheritance actually
                # resolved through the parent role (an unconfigured parent means
                # this role is simply disabled, not "inherited").
                inherited = configured_pair is not None
            else:
                configured_pair = self._own_chat_role(role, cfg=active)

        configured = configured_pair is not None
        status: Dict[str, Any] = {
            "role": role,
            "configured": configured,
            "inherited": inherited,
            "inherit": bool(inherit_from),
            "inherit_from": inherit_from,
            "source_id": "",
            "model": "",
            "provider_id": "",
            "mode": "unconfigured",
            "label": "未配置",
            "ok": False,
        }
        if role == "decision":
            entry = active.get("decision") or {}
            decision_mode, threshold, _endpoint = _normalize_decision_fields(entry)
            status["decision_mode"] = decision_mode
            status["threshold"] = threshold
            status["judge_ready"] = False
            status["key_source"] = ""
            status["endpoint"] = ""
        if configured:
            source_id = configured_pair["source_id"]
            model = configured_pair["model"]
            status["source_id"] = source_id
            status["model"] = model
            if status.get("decision_mode") == "judge":
                # 判断模型不需要聊天 Provider；只校验模型 + API Key 是否可用。
                credentials = self._decision_credentials(configured_pair, entry)
                ready = bool(credentials.get("api_key"))
                status["mode"] = "judge"
                status["provider_id"] = ""
                status["judge_ready"] = ready
                status["key_source"] = credentials.get("key_source", "")
                status["endpoint"] = credentials.get("endpoint", "")
                status["ok"] = ready
                status["label"] = (
                    f"判断模型 {model} · {credentials.get('endpoint', '')}"
                    if ready
                    else "判断模型未就绪：缺少 API Key（提供商源或 OPENROUTER_API_KEY）"
                )
            else:
                reused = self._find_reusable(source_id, model)
                if reused is not None:
                    provider_id, _model = self._provider_meta(reused)
                    status["mode"] = "reuse"
                    status["provider_id"] = provider_id
                    status["label"] = f"{source_id} / {model}（复用 AstrBot 实例）"
                else:
                    status["mode"] = "plugin"
                    if role == "dialogue":
                        status["provider_id"] = (
                            f"{self.MAIN_PROVIDER_ID}::{soul_id}"
                            if soul_id
                            else self.MAIN_PROVIDER_ID
                        )
                    else:
                        status["provider_id"] = f"onlyyou::chat::{source_id}::{model}"
                    status["label"] = f"{source_id} / {model}（插件自建实例）"
                status["ok"] = True
        if inherited:
            parent_label = {
                "dialogue": "继承自对话模型",
                "main": "继承自主模型",
                "decision": "继承自决策模型",
            }.get(inherit_from, "")
            if parent_label:
                status["label"] += " · " + parent_label
        return status

    def _decision_credentials(
        self, pair: Optional[Dict[str, str]], entry: Optional[dict] = None
    ) -> Dict[str, str]:
        """Resolve OpenRouter Decisions API credentials without leaking the key.

        Priority: the selected provider source's key when the source points at
        OpenRouter, otherwise ``OPENROUTER_API_KEY`` from the environment. The
        endpoint can be overridden per Soul (custom gateway), defaulting to the
        public OpenRouter Decisions API.
        """
        entry = entry if isinstance(entry, dict) else {}
        source_id = _clean((pair or {}).get("source_id"))
        api_base = ""
        api_key = ""
        factory = self.provider_factory
        if source_id and factory is not None:
            getter = getattr(factory, "get_source_credentials", None)
            if callable(getter):
                try:
                    credentials = getter(source_id) or {}
                except Exception:  # noqa: BLE001 - status must never raise
                    credentials = {}
                api_base = _clean(credentials.get("api_base"))
                api_key = _clean(credentials.get("api_key"))
        is_openrouter = "openrouter.ai" in api_base.lower()
        env_key = _clean(os.environ.get(OPENROUTER_KEY_ENV))
        key_source = ""
        if is_openrouter and api_key:
            key_source = "source"
        elif env_key:
            api_key = env_key
            key_source = "env"
        elif not is_openrouter:
            # A non-OpenRouter source key cannot authenticate the default endpoint.
            api_key = ""
        endpoint = _clean(entry.get("endpoint")) or DEFAULT_DECISIONS_ENDPOINT
        return {
            "endpoint": endpoint,
            "api_key": api_key,
            "key_source": key_source,
            "api_base": api_base,
        }

    def _tts_status(self, cfg: Optional[Dict[str, Any]] = None) -> dict:
        active = cfg if isinstance(cfg, dict) else self._sync_active_config()
        tts_section = active.get("tts") if isinstance(active.get("tts"), dict) else {}
        mode = _clean(tts_section.get("mode")) or "off"
        status: Dict[str, Any] = {
            "role": "tts",
            "configured": mode != "off",
            "inherited": False,
            "inherit": False,
            "inherit_from": "",
            "mode": mode,
            "source_id": _clean(tts_section.get("source_id")),
            "model": _clean(tts_section.get("model")),
            "voice": _clean(tts_section.get("voice")),
            "provider_id": _clean(tts_section.get("provider_id")),
            "probability": tts_section.get("probability", 1.0),
            "scope": _clean(tts_section.get("scope")),
            "dual_output": bool(tts_section.get("dual_output")),
            "label": "已关闭",
            "ok": False,
        }
        if mode == "source":
            configured = bool(status["source_id"] and status["model"])
            status["configured"] = configured
            status["provider_id"] = "onlyyou::tts" if configured else ""
            status["label"] = (
                f"{status['source_id']} / {status['model']}"
                + (f" / {status['voice']}" if status["voice"] else "")
                if configured
                else "提供商源未配置完整"
            )
            status["ok"] = configured
        elif mode == "provider":
            configured = bool(status["provider_id"])
            status["configured"] = configured
            status["label"] = (
                f"AstrBot TTS：{status['provider_id']}" if configured else "未选择 TTS 提供商"
            )
            status["ok"] = configured
        return status

    def _vector_role_status(
        self, role: str, cfg: Optional[Dict[str, Any]] = None
    ) -> dict:
        """Status entry for ``embedding`` / ``rerank`` (v2.12)."""
        active = cfg if isinstance(cfg, dict) else self._sync_active_config()
        section = active.get(role) if isinstance(active.get(role), dict) else {}
        mode = _clean(section.get("mode")).lower() or "off"
        status: Dict[str, Any] = {
            "role": role,
            "name": VECTOR_ROLE_LABELS.get(role, role),
            "configured": False,
            "inherited": False,
            "inherit": False,
            "inherit_from": "",
            "mode": mode,
            "provider_id": _clean(section.get("provider_id")),
            "source_id": _clean(section.get("source_id")),
            "model": _clean(section.get("model")),
            "label": "已关闭（降级为词法检索）",
            "ok": False,
        }
        if mode == "provider":
            configured = bool(status["provider_id"])
            status["configured"] = configured
            status["label"] = (
                f"AstrBot {VECTOR_ROLE_LABELS.get(role, role)}：{status['provider_id']}"
                if configured
                else "未选择 AstrBot 提供商"
            )
            status["ok"] = configured
        elif mode == "source":
            configured = bool(status["source_id"] and status["model"])
            status["configured"] = configured
            status["provider_id"] = (
                f"onlyyou::{role}::{status['source_id']}::{status['model']}"
                if configured
                else ""
            )
            status["label"] = (
                f"{status['source_id']} / {status['model']}"
                if configured
                else "提供商源未配置完整"
            )
            status["ok"] = configured
        return status

    async def get_status(self, soul_id: Optional[str] = None) -> Dict[str, dict]:
        """Return the four-role status of one Soul (default Soul when empty)."""
        if not self._soul_manager_ready() and not self._loaded:
            try:
                await self.load()
            except Exception:
                pass
        resolved_soul_id = await self._resolve_soul_id(soul_id)
        try:
            active_cfg = await self._active_config(resolved_soul_id or soul_id)
        except Exception:
            active_cfg = self._config
        status: Dict[str, dict] = {}
        for role in CHAT_ROLES:
            try:
                status[role] = self._chat_role_status(
                    role, cfg=active_cfg, soul_id=resolved_soul_id
                )
            except Exception as exc:
                status[role] = {
                    "role": role,
                    "configured": False,
                    "inherited": False,
                    "inherit": False,
                    "inherit_from": "",
                    "source_id": "",
                    "model": "",
                    "provider_id": "",
                    "mode": "unconfigured",
                    "label": "未配置",
                    "ok": False,
                    "error": str(exc),
                }
        try:
            status["tts"] = self._tts_status(active_cfg)
        except Exception as exc:
            status["tts"] = {
                "role": "tts",
                "configured": False,
                "inherited": False,
                "inherit": False,
                "inherit_from": "",
                "mode": "off",
                "label": "已关闭",
                "ok": False,
                "error": str(exc),
            }
        for role in VECTOR_ROLES:
            try:
                status[role] = self._vector_role_status(role, active_cfg)
            except Exception as exc:  # noqa: BLE001 - status must never raise
                status[role] = {
                    "role": role,
                    "configured": False,
                    "inherited": False,
                    "inherit": False,
                    "inherit_from": "",
                    "mode": "off",
                    "label": "已关闭（降级为词法检索）",
                    "ok": False,
                    "error": str(exc),
                }
        return status

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def dispose(self) -> None:
        """Terminate plugin-built providers and drop all caches."""
        factory = self.provider_factory
        if factory is None:
            return
        disposer = getattr(factory, "dispose", None)
        if not callable(disposer):
            return
        try:
            await disposer()
        except Exception as exc:
            logger.warning(f"[OnlyYou] provider_factory.dispose failed: {exc}")
