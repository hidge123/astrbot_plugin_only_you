"""Plugin-autonomous model hub: four model roles backed by ``plugin_settings``.

Roles (CONTRACTS.md §2.2, v2.6 optional inheritance):

* ``main``     — main chat model, required, optionally exposed to AstrBot's native
  pipeline via ``event.set_extra("selected_provider", ...)``.
* ``decision`` — read-the-air / memory extraction / schedule mutation / deck
  generation.  Either configured independently (``inherit_from=""``) or inherited
  from ``main`` (``inherit_from="main"``).
* ``vision``   — multimodal video scoring.  Same as ``decision`` plus the extra
  option to inherit from the lightweight decision model
  (``inherit_from="decision"`` -> ``decision -> main`` at most 2 hops).
* ``tts``      — text-to-speech, never inherited; ``mode="off"`` means disabled.
* ``small``    — legacy alias of ``decision`` kept for old call sites.

Inheritance is **optional** since v2.6: turning it off and leaving ``source_id`` /
``model`` empty simply disables the role (``resolve_role`` returns ``None``) instead
of silently falling back to ``main``.

Everything is stored in ``plugin_settings["model_config"]`` (SQLite KV) so the
plugin never reads AstrBot's own model selection config (``context.get_config`` /
``get_current_chat_provider_id`` / ``default_image_caption_provider_id`` are gone).
All AstrBot provider internals are reached through ``core/provider_factory.py``.
"""

import base64
import copy
import json
import re
import time
from typing import Any, Dict, List, Optional

try:
    from astrbot.api import logger
except ImportError:  # pragma: no cover - local offline tests
    import logging

    logger = logging.getLogger("onlyyou.model_hub")


DEFAULT_MODEL_CONFIG: Dict[str, dict] = {
    "main": {"source_id": "", "model": ""},
    "decision": {
        "inherit": True,
        "inherit_from": "main",
        "source_id": "",
        "model": "",
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
}

TTS_MODES = ("off", "source", "provider")
TTS_SCOPES = ("private", "l3", "all")
CHAT_ROLES = ("main", "decision", "vision")

#: Legal ``inherit_from`` values for the two inheritable chat roles (v2.6).
INHERIT_FROM_OPTIONS: Dict[str, tuple] = {
    "decision": ("", "main"),
    "vision": ("", "main", "decision"),
}

#: Maximum inheritance hops (``vision -> decision -> main`` is exactly 2 hops).
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


def _normalize_inherit_from(role: str, role_raw: dict) -> str:
    """Normalize ``decision`` / ``vision`` inheritance target (v2.6 contract §1.1).

    Rules, in order:

    1. explicit ``inherit_from`` (non-None) wins; illegal values fall back to
       ``"main"`` when ``inherit`` is truthy, otherwise to ``""``;
    2. explicit ``inherit`` booleans map to ``"main"`` / ``""``;
    3. legacy configs (neither key present) keep inheriting ``main``.
    """
    options = INHERIT_FROM_OPTIONS.get(role, ("", "main"))
    if "inherit_from" in role_raw and role_raw.get("inherit_from") is not None:
        candidate = _clean(role_raw.get("inherit_from")).lower()
        if candidate in options:
            return candidate
        return "main" if _to_bool(role_raw.get("inherit", True), default=True) else ""
    if "inherit" in role_raw:
        return "main" if _to_bool(role_raw.get("inherit"), default=True) else ""
    return "main"


def normalize_model_config(raw: Any) -> Dict[str, dict]:
    """Coerce arbitrary stored/requested model config into the canonical shape."""
    raw = raw if isinstance(raw, dict) else {}
    normalized: Dict[str, dict] = {}

    main_raw = raw.get("main") if isinstance(raw.get("main"), dict) else {}
    normalized["main"] = {
        "source_id": _clean(main_raw.get("source_id")),
        "model": _clean(main_raw.get("model")),
    }

    for role in ("decision", "vision"):
        role_raw = raw.get(role) if isinstance(raw.get(role), dict) else {}
        inherit_from = _normalize_inherit_from(role, role_raw)
        normalized[role] = {
            # ``inherit`` and ``inherit_from`` are always kept in sync.
            "inherit": bool(inherit_from),
            "inherit_from": inherit_from,
            "source_id": _clean(role_raw.get("source_id")),
            "model": _clean(role_raw.get("model")),
        }

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
    return normalized


class ModelHub:
    """Four-role model scheduling gateway (main / decision / vision / tts)."""

    ROLES = ("main", "decision", "vision", "tts")
    CONFIG_SETTING_KEY = "model_config"
    MIGRATION_FLAG_KEY = "legacy_model_migrated"
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

    @staticmethod
    def _config_is_meaningful(cfg: Dict[str, dict]) -> bool:
        """Whether a normalized model config carries any explicit configuration."""
        main = cfg.get("main") or {}
        if _clean(main.get("source_id")) and _clean(main.get("model")):
            return True
        for role in ("decision", "vision"):
            entry = cfg.get(role) or {}
            if _clean(entry.get("source_id")) and _clean(entry.get("model")):
                return True
        tts = cfg.get("tts") or {}
        return _clean(tts.get("mode")).lower() not in ("", "off")

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
        main_pair = await self._resolve_legacy_provider_id(
            legacy.get("main_llm_provider_id")
        )
        if main_pair is not None:
            new_config["main"] = {
                "source_id": main_pair[0],
                "model": main_pair[1],
            }
            decision_pair = await self._resolve_legacy_provider_id(
                legacy.get("small_llm_provider_id")
            )
            if decision_pair is not None and decision_pair != main_pair:
                new_config["decision"] = {
                    "inherit": False,
                    "source_id": decision_pair[0],
                    "model": decision_pair[1],
                }
            vision_pair = await self._resolve_legacy_provider_id(
                legacy.get("vision_llm_provider_id")
            )
            if vision_pair is not None and vision_pair != main_pair:
                new_config["vision"] = {
                    "inherit": False,
                    "source_id": vision_pair[0],
                    "model": vision_pair[1],
                }

        current_main = self._config.get("main") or {}
        has_current_main = bool(
            current_main.get("source_id") and current_main.get("model")
        )
        if main_pair is not None and not has_current_main:
            self._config = normalize_model_config(new_config)
            try:
                await self.db.set_setting(self.CONFIG_SETTING_KEY, self._config)
                logger.info(
                    "[OnlyYou] migrated legacy model_hub_config -> model_config "
                    f"({self._config['main'].get('source_id')}/"
                    f"{self._config['main'].get('model')})"
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
        if name in ("small", "aux", "auxiliary"):
            return "decision"
        if name in ModelHub.ROLES:
            return name
        return ""

    def _own_chat_role(
        self, role: str, cfg: Optional[Dict[str, dict]] = None
    ) -> Optional[Dict[str, str]]:
        """Explicitly configured ``(source_id, model)`` of decision/vision, if any."""
        active = cfg if isinstance(cfg, dict) else self._config
        entry = active.get(role) or {}
        source_id = _clean(entry.get("source_id"))
        model = _clean(entry.get("model"))
        if source_id and model:
            return {"source_id": source_id, "model": model}
        return None

    def _main_pair(
        self, cfg: Optional[Dict[str, dict]] = None
    ) -> Optional[Dict[str, str]]:
        active = cfg if isinstance(cfg, dict) else self._config
        entry = active.get("main") or {}
        source_id = _clean(entry.get("source_id"))
        model = _clean(entry.get("model"))
        if source_id and model:
            return {"source_id": source_id, "model": model}
        return None

    def _inherit_from(
        self, role: str, cfg: Optional[Dict[str, dict]] = None
    ) -> str:
        """Normalized inheritance target of ``decision`` / ``vision`` (``""`` == off)."""
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
        """Resolve ``decision`` / ``vision`` through their optional inheritance chain.

        * inheritance disabled (``inherit_from=""``) -> own config only; an
          incomplete own config returns ``None`` (**never** falls back to ``main``);
        * ``inherit_from="main"`` -> the main pair;
        * ``inherit_from="decision"`` (vision only) -> recurse, at most
          ``MAX_INHERIT_HOPS`` hops and cycle-safe (a cycle exhausts the hop budget).
        """
        inherit_from = self._inherit_from(role, cfg=cfg)
        if not inherit_from:
            return self._own_chat_role(role, cfg=cfg)
        if inherit_from == "main":
            return self._main_pair(cfg=cfg)
        if inherit_from in INHERIT_FROM_OPTIONS and hops < MAX_INHERIT_HOPS:
            return self._resolve_chat_role(inherit_from, hops + 1, cfg=cfg)
        # >2 hops / self-cycle / unknown parent role -> disabled (no main fallback).
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

        if name == "main":
            return self._main_pair(cfg=cfg)

        if name in ("decision", "vision"):
            return self._resolve_chat_role(name, cfg=cfg)

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

        * ``""``                  — main model not configured / provider build failed
          (plugin falls back to AstrBot's default model).
        * existing AstrBot id    — an already loaded instance was reused.
        * ``"onlyyou::main"``    — a plugin-owned instance was registered into
          ``provider_manager.inst_map``.
        * ``"onlyyou::main::{soul_id}"`` — v2.8 per-Soul plugin-owned instance.
        """
        try:
            resolved_soul_id = await self._resolve_soul_id(soul_id)
            resolved = await self.resolve_role("main", soul_id=resolved_soul_id or soul_id)
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
        role: str = "decision",
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
        role: str = "decision",
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
    # Diagnostics / testing
    # ------------------------------------------------------------------
    async def test_role(self, role: str, soul_id: Optional[str] = None) -> dict:
        """Send a ping (or a short TTS sentence) and report latency / result."""
        name = self._normalize_role(role)
        if not name:
            return {"status": "error", "role": _clean(role), "error": "未知模型角色"}
        if name == "tts":
            return await self._test_tts(soul_id=soul_id)

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
        if role == "main":
            configured_pair = self._main_pair(cfg=active)
        else:
            inherit_from = self._inherit_from(role, cfg=active)
            if inherit_from:
                configured_pair = self._resolve_chat_role(role, cfg=active)
                # ``inherited`` is True only when an enabled inheritance actually
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
        if not configured:
            return status

        source_id = configured_pair["source_id"]
        model = configured_pair["model"]
        status["source_id"] = source_id
        status["model"] = model

        reused = self._find_reusable(source_id, model)
        if reused is not None:
            provider_id, _model = self._provider_meta(reused)
            status["mode"] = "reuse"
            status["provider_id"] = provider_id
            status["label"] = f"{source_id} / {model}（复用 AstrBot 实例）"
        else:
            status["mode"] = "plugin"
            if role == "main":
                status["provider_id"] = (
                    f"{self.MAIN_PROVIDER_ID}::{soul_id}"
                    if soul_id
                    else self.MAIN_PROVIDER_ID
                )
            else:
                status["provider_id"] = f"onlyyou::chat::{source_id}::{model}"
            status["label"] = f"{source_id} / {model}（插件自建实例）"
        if inherited:
            if inherit_from == "main":
                status["label"] += " · 继承主模型"
            elif inherit_from == "decision":
                status["label"] += " · 继承轻量决策模型"
        status["ok"] = True
        return status

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
