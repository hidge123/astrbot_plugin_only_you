"""Provider factory: the only module allowed to touch AstrBot provider internals.

This module centralises every access to AstrBot 4.x private provider structures so
that the rest of the plugin (``ModelHub``, ``web_api``) never has to poke around
inside ``context.provider_manager`` directly.  It exposes:

* read-only queries used by the Pages console:
  ``list_sources`` / ``list_models`` / ``list_tts_providers`` /
  ``find_source_model_by_provider_id``
* provider construction / reuse:
  ``get_chat_provider`` / ``register_main`` / ``unregister_main`` /
  ``get_tts_provider``
* lifecycle: ``dispose``

Design rules (CONTRACTS.md §2.1):

1. Every AstrBot attribute access is wrapped in ``getattr`` + ``try/except``.
   Failures only log a warning and return ``None`` / ``[]`` — the plugin lifecycle
   must never be broken by an AstrBot internal change.
2. API keys are never returned to callers that face the WebUI layer
   (``list_sources`` only reports ``has_key``).
3. Plugin-built chat providers are cached by ``(source_id, model)``; the main one
   is also registered into ``provider_manager.inst_map["onlyyou::main"]`` so that
   AstrBot's native pipeline (``_select_provider`` + ``event.get_extra``
   ``"selected_provider"``) can use it without the plugin owning the reply.
   Plugin-built instances are **not** appended to ``provider_insts`` so they never
   show up in AstrBot's own model list.
"""

import inspect
import os
from typing import Any, Dict, List, Optional, Tuple

try:
    from astrbot.api import logger
except ImportError:  # pragma: no cover - local offline tests
    import logging

    logger = logging.getLogger("onlyyou.provider_factory")


MAIN_PROVIDER_ID = "onlyyou::main"
TTS_PROVIDER_ID = "onlyyou::tts"
PLUGIN_ID_PREFIX = "onlyyou::"


def _getattr_safe(obj: Any, name: str, default: Any = None) -> Any:
    """``getattr`` that never raises (properties may explode)."""
    if obj is None:
        return default
    try:
        return getattr(obj, name, default)
    except Exception:
        return default


def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list:
    return list(value) if isinstance(value, (list, tuple)) else []


def _clean(value: Any) -> str:
    if value is None:
        return ""
    try:
        return str(value).strip()
    except Exception:
        return ""


class ProviderFactory:
    """Encapsulates all AstrBot provider internals used by OnlyYou."""

    MAIN_PROVIDER_ID = MAIN_PROVIDER_ID
    TTS_PROVIDER_ID = TTS_PROVIDER_ID
    CHAT_ID_PREFIX = "onlyyou::chat::"

    def __init__(self, context: Any) -> None:
        self.context = context
        # (source_id, model) -> provider instance (either reused AstrBot instance or
        # plugin-built one).
        self._chat_cache: Dict[Tuple[str, str], Any] = {}
        # misc key -> TTS provider instance
        self._tts_cache: Dict[str, Any] = {}
        # Strong references to plugin-built instances (so ``id()`` bookkeeping stays
        # valid and dispose can terminate them deterministically).
        self._owned_instances: List[Any] = []
        self._owned_ids: set = set()
        self._main_instance: Any = None

    # ------------------------------------------------------------------
    # Low level helpers
    # ------------------------------------------------------------------
    @property
    def provider_manager(self) -> Any:
        return _getattr_safe(self.context, "provider_manager", None)

    def _inst_map(self) -> dict:
        inst_map = _getattr_safe(self.provider_manager, "inst_map", None)
        return inst_map if isinstance(inst_map, dict) else {}

    def _provider_settings(self) -> dict:
        return _as_dict(_getattr_safe(self.provider_manager, "provider_settings", {}))

    def _provider_sources_config(self) -> list:
        return _as_list(
            _getattr_safe(self.provider_manager, "provider_sources_config", [])
        )

    def _providers_config(self) -> list:
        return _as_list(_getattr_safe(self.provider_manager, "providers_config", []))

    def _find_source(self, source_id: str) -> Optional[dict]:
        """Return the raw provider source config (``None`` when unknown)."""
        source_id = _clean(source_id)
        if not source_id:
            return None
        for source in self._provider_sources_config():
            if isinstance(source, dict) and _clean(source.get("id")) == source_id:
                return source
        return None

    def _find_provider_entry(self, provider_id: str) -> Optional[dict]:
        """Return the raw ``providers_config`` entry with the given id."""
        provider_id = _clean(provider_id)
        if not provider_id:
            return None
        for entry in self._providers_config():
            if isinstance(entry, dict) and _clean(entry.get("id")) == provider_id:
                return entry
        return None

    def _provider_cls_map(self) -> dict:
        """Resolve AstrBot's module level ``provider_cls_map`` without hard imports."""
        for attr_owner in (self.provider_manager, self.context):
            candidate = _getattr_safe(attr_owner, "provider_cls_map", None)
            if isinstance(candidate, dict) and candidate:
                return candidate

        candidates = (
            ("astrbot.core.provider.register", "provider_cls_map"),
            ("astrbot.core.provider.manager", "provider_cls_map"),
            ("astrbot.core.provider", "provider_cls_map"),
        )
        for module_path, attr_name in candidates:
            try:
                module = __import__(module_path, fromlist=[attr_name])
                candidate = getattr(module, attr_name, None)
                if isinstance(candidate, dict) and candidate:
                    return candidate
            except Exception:
                continue
        return {}

    @staticmethod
    def _safe_meta(instance: Any) -> dict:
        """Extract ``{id, model, type, provider_type}`` from a provider instance."""
        try:
            meta = instance.meta()
            provider_type = getattr(meta, "provider_type", "")
            provider_type = getattr(provider_type, "value", provider_type)
            return {
                "id": _clean(getattr(meta, "id", "")),
                "model": _clean(getattr(meta, "model", "")),
                "type": _clean(getattr(meta, "type", "")),
                "provider_type": _clean(provider_type),
            }
        except Exception:
            pass
        # Fallback: read the raw config the provider was built from.
        config = _as_dict(_getattr_safe(instance, "provider_config", {}))
        model = _clean(config.get("model"))
        if not model:
            try:
                model = _clean(instance.get_model())
            except Exception:
                model = ""
        return {
            "id": _clean(config.get("id")),
            "model": model,
            "type": _clean(config.get("type")),
            "provider_type": _clean(config.get("provider_type")),
        }

    @staticmethod
    def _read_env_key(raw: Any) -> Any:
        """Resolve a single ``key`` entry: ``$ENV`` / ``${ENV}`` -> ``os.environ``."""
        if not isinstance(raw, str):
            return raw
        value = raw.strip()
        if not value.startswith("$"):
            return value
        env_name = value[1:]
        if env_name.startswith("{") and env_name.endswith("}"):
            env_name = env_name[1:-1]
        if not env_name:
            return ""
        return os.environ.get(env_name, "") or ""

    @classmethod
    def _resolve_env_keys(cls, config: dict) -> dict:
        """Resolve ``$ENV`` style key(s) in a merged provider config (in place)."""
        try:
            raw_key = config.get("key")
            if isinstance(raw_key, str):
                config["key"] = cls._read_env_key(raw_key)
            elif isinstance(raw_key, (list, tuple)):
                config["key"] = [cls._read_env_key(item) for item in raw_key]
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(f"[OnlyYou] resolve provider key failed: {exc}")
        return config

    @classmethod
    def _resolve_single_key(cls, raw_key: Any) -> str:
        """Resolve a key container (str / list) down to one non-empty string."""
        try:
            if isinstance(raw_key, str):
                resolved = cls._read_env_key(raw_key)
                return resolved if isinstance(resolved, str) else _clean(resolved)
            if isinstance(raw_key, (list, tuple)):
                for item in raw_key:
                    resolved = cls._read_env_key(item)
                    text = _clean(resolved)
                    if text:
                        return text
        except Exception:
            pass
        return ""

    def _merge_provider_config(self, entry: dict) -> dict:
        """Merge a provider entry with its source (provider entry wins)."""
        try:
            merge = getattr(self.provider_manager, "get_merged_provider_config", None)
            if callable(merge):
                merged = merge(entry)
                if isinstance(merged, dict):
                    return merged
        except Exception as exc:
            logger.warning(f"[OnlyYou] get_merged_provider_config failed: {exc}")

        # Manual fallback identical to AstrBot's merge rule.
        merged = {}
        source_id = _clean(entry.get("provider_source_id"))
        source = self._find_source(source_id)
        if source:
            merged.update(source)
        merged.update(entry)
        merged["id"] = entry.get("id") or merged.get("id") or ""
        return merged

    def _is_loaded_instance(self, instance: Any) -> bool:
        """True when ``instance`` is plugin-owned or still known to AstrBot."""
        if instance is None:
            return False
        if id(instance) in self._owned_ids:
            return True
        try:
            pm = self.provider_manager
            for attr in ("provider_insts", "tts_provider_insts", "inst_map"):
                container = _getattr_safe(pm, attr, None)
                if isinstance(container, dict):
                    if any(value is instance for value in container.values()):
                        return True
                elif isinstance(container, (list, tuple)):
                    if any(value is instance for value in container):
                        return True
        except Exception:
            pass
        return False

    def is_owned_instance(self, instance: Any) -> bool:
        """Whether ``instance`` was built by this plugin (must be terminated by us)."""
        try:
            return id(instance) in self._owned_ids
        except Exception:
            return False

    def find_loaded_provider_id(self, instance: Any) -> str:
        """Return the AstrBot provider id of an already loaded instance."""
        if instance is None:
            return ""
        try:
            for provider_id, loaded in self._inst_map().items():
                if loaded is instance:
                    pid = _clean(provider_id)
                    if pid and not pid.startswith(PLUGIN_ID_PREFIX):
                        return pid
        except Exception:
            pass
        return self._safe_meta(instance).get("id", "")

    def _track_owned(self, instance: Any) -> Any:
        if instance is not None and id(instance) not in self._owned_ids:
            self._owned_instances.append(instance)
            self._owned_ids.add(id(instance))
        return instance

    # ------------------------------------------------------------------
    # Read-only queries (WebUI)
    # ------------------------------------------------------------------
    def list_sources(self) -> List[dict]:
        """List provider sources for the model page. Never exposes API keys."""
        sources: List[dict] = []
        try:
            for source in self._provider_sources_config():
                if not isinstance(source, dict):
                    continue
                source_id = _clean(source.get("id"))
                if not source_id:
                    continue
                raw_key = source.get("key")
                if isinstance(raw_key, (list, tuple)):
                    has_key = any(_clean(item) for item in raw_key)
                else:
                    has_key = bool(_clean(raw_key))
                enable = source.get("enable", True)
                sources.append(
                    {
                        "id": source_id,
                        "type": _clean(source.get("type")),
                        "provider_type": _clean(source.get("provider_type")),
                        "provider": _clean(source.get("provider")) or source_id,
                        "api_base": _clean(source.get("api_base")),
                        "has_key": bool(has_key),
                        "enable": True if enable is None else bool(enable),
                    }
                )
        except Exception as exc:
            logger.warning(f"[OnlyYou] list_sources failed: {exc}")
            return []
        return sources

    def _is_chat_source(self, source: dict) -> bool:
        provider_type = _clean(source.get("provider_type"))
        return provider_type in ("", "chat_completion")

    async def list_models(self, source_id: str) -> List[dict]:
        """List models of ``source_id``: local ``providers_config`` + remote ``get_models``."""
        source_id = _clean(source_id)
        if not source_id:
            return []

        models: List[dict] = []
        seen: set = set()
        try:
            for entry in self._providers_config():
                if not isinstance(entry, dict):
                    continue
                if _clean(entry.get("provider_source_id")) != source_id:
                    continue
                model = _clean(entry.get("model"))
                if not model or model in seen:
                    continue
                seen.add(model)
                modalities = entry.get("modalities")
                models.append(
                    {
                        "model": model,
                        "id": _clean(entry.get("id")),
                        "enable": bool(entry.get("enable", False)),
                        "modalities": [
                            _clean(item) for item in _as_list(modalities) if _clean(item)
                        ],
                        "source": "config",
                    }
                )
        except Exception as exc:
            logger.warning(f"[OnlyYou] list_models(config) failed: {exc}")

        source = self._find_source(source_id)
        if source is None or not self._is_chat_source(source):
            return models

        # Remote probing with a temporary instance; ignore every failure silently.
        temp_instance = None
        try:
            temp_model = models[0]["model"] if models else ""
            temp_instance = await self._instantiate_chat_provider(source_id, temp_model)
            if temp_instance is None:
                return models
            get_models = getattr(temp_instance, "get_models", None)
            if not callable(get_models):
                return models
            remote_models = await get_models() or []
            for raw in _as_list(remote_models):
                name = _clean(raw)
                if not name or name in seen:
                    continue
                seen.add(name)
                models.append(
                    {
                        "model": name,
                        "id": "",
                        "enable": False,
                        "modalities": [],
                        "source": "remote",
                    }
                )
        except Exception as exc:
            logger.debug(f"[OnlyYou] list_models(remote) skipped: {exc}")
        finally:
            if temp_instance is not None:
                try:
                    terminate = getattr(temp_instance, "terminate", None)
                    if callable(terminate):
                        result = terminate()
                        if hasattr(result, "__await__"):
                            await result
                except Exception:
                    pass
        return models

    def list_tts_providers(self) -> List[dict]:
        """List enabled AstrBot TTS instances + configured-but-disabled TTS entries."""
        providers: List[dict] = []
        seen: set = set()
        try:
            get_all_tts = _getattr_safe(self.context, "get_all_tts_providers", None)
            if callable(get_all_tts):
                for instance in _as_list(get_all_tts()):
                    meta = self._safe_meta(instance)
                    pid = meta.get("id") or _clean(
                        _as_dict(_getattr_safe(instance, "provider_config", {})).get("id")
                    )
                    if not pid or pid in seen:
                        continue
                    seen.add(pid)
                    providers.append(
                        {
                            "id": pid,
                            "model": meta.get("model", ""),
                            "type": meta.get("type", ""),
                            "enable": True,
                        }
                    )
        except Exception as exc:
            logger.warning(f"[OnlyYou] list_tts_providers(instances) failed: {exc}")

        try:
            for entry in self._providers_config():
                if not isinstance(entry, dict):
                    continue
                if _clean(entry.get("provider_type")) != "text_to_speech":
                    continue
                pid = _clean(entry.get("id"))
                if not pid or pid in seen:
                    continue
                seen.add(pid)
                providers.append(
                    {
                        "id": pid,
                        "model": _clean(entry.get("model")),
                        "type": _clean(entry.get("type")),
                        "enable": bool(entry.get("enable", False)),
                    }
                )
        except Exception as exc:
            logger.warning(f"[OnlyYou] list_tts_providers(config) failed: {exc}")
        return providers

    async def find_source_model_by_provider_id(
        self, provider_id: str
    ) -> Optional[Tuple[str, str]]:
        """Reverse lookup an AstrBot provider id into ``(source_id, model)``."""
        provider_id = _clean(provider_id)
        if not provider_id or provider_id.startswith(PLUGIN_ID_PREFIX):
            return None

        # 1) Live instance metadata (``provider_config`` is the merged config).
        try:
            instance = self._inst_map().get(provider_id)
            if instance is not None:
                config = _as_dict(_getattr_safe(instance, "provider_config", {}))
                meta = self._safe_meta(instance)
                source_id = _clean(config.get("provider_source_id"))
                model = _clean(config.get("model")) or meta.get("model", "")
                if not source_id and self._find_source(provider_id):
                    source_id = provider_id
                if source_id and model:
                    return (source_id, model)
        except Exception as exc:
            logger.debug(f"[OnlyYou] find_source_model_by_provider_id(instance): {exc}")

        # 2) Static provider entry (handles disabled providers too).
        try:
            entry = self._find_provider_entry(provider_id)
            if entry is not None:
                source_id = _clean(entry.get("provider_source_id"))
                model = _clean(entry.get("model"))
                if not source_id and self._find_source(provider_id):
                    source_id = provider_id
                if source_id and model:
                    return (source_id, model)
        except Exception as exc:
            logger.debug(f"[OnlyYou] find_source_model_by_provider_id(entry): {exc}")

        return None

    # ------------------------------------------------------------------
    # Chat provider construction / reuse
    # ------------------------------------------------------------------
    def _instance_matches(self, instance: Any, source_id: str, model: str) -> bool:
        """Whether ``instance`` is a chat provider for exactly ``(source_id, model)``."""
        try:
            config = _as_dict(_getattr_safe(instance, "provider_config", {}))
            if _clean(config.get("provider_source_id")) != source_id:
                return False
            meta = self._safe_meta(instance)
            provider_type = meta.get("provider_type", "")
            if provider_type and provider_type != "chat_completion":
                return False
            config_model = _clean(config.get("model"))
            meta_model = meta.get("model", "")
            return bool(model) and model in (config_model, meta_model)
        except Exception:
            return False

    def _instance_is_enabled(self, provider_id: str) -> bool:
        """Check the static ``providers_config`` ``enable`` flag (loaded => enabled)."""
        entry = self._find_provider_entry(provider_id)
        if entry is not None and entry.get("enable") is False:
            return False
        return True

    def find_reusable_chat_provider(self, source_id: str, model: str) -> Any:
        """Return an already loaded AstrBot chat instance for ``(source, model)``."""
        source_id = _clean(source_id)
        model = _clean(model)
        if not source_id or not model:
            return None
        try:
            inst_map = self._inst_map()
            for provider_id, instance in list(inst_map.items()):
                pid = _clean(provider_id)
                # never "reuse" our own plugin-built instances
                if not pid or pid.startswith(PLUGIN_ID_PREFIX):
                    continue
                if not self._instance_is_enabled(pid):
                    continue
                if self._instance_matches(instance, source_id, model):
                    return instance
        except Exception as exc:
            logger.warning(f"[OnlyYou] find_reusable_chat_provider failed: {exc}")
        return None

    async def _instantiate_chat_provider(self, source_id: str, model: str) -> Any:
        """Build a brand new chat provider instance from a provider source."""
        source_id = _clean(source_id)
        model = _clean(model)
        if not source_id:
            logger.warning("[OnlyYou] get_chat_provider: empty source_id")
            return None
        source = self._find_source(source_id)
        if source is None:
            logger.warning(
                f"[OnlyYou] provider source not found: {source_id}"
            )
            return None

        try:
            entry = {
                "id": f"{self.CHAT_ID_PREFIX}{source_id}::{model}",
                "enable": True,
                "provider_source_id": source_id,
                "model": model,
                "modalities": [],
                "custom_extra_body": {},
                "max_context_tokens": 0,
            }
            config = self._merge_provider_config(entry)
            config = self._resolve_env_keys(config)
            config["model"] = model
            provider_type = _clean(config.get("type"))
            if not provider_type:
                logger.warning(
                    f"[OnlyYou] provider source {source_id} has no adapter type"
                )
                return None

            pm = self.provider_manager
            if pm is None:
                logger.warning("[OnlyYou] provider_manager unavailable")
                return None

            dynamic_import = getattr(pm, "dynamic_import_provider", None)
            if callable(dynamic_import):
                dynamic_import(provider_type)
            else:
                self._fallback_import_provider(provider_type)

            meta_data = self._provider_cls_map().get(provider_type)
            provider_cls = _getattr_safe(meta_data, "cls_type", None)
            if provider_cls is None:
                logger.warning(
                    f"[OnlyYou] provider adapter not registered: {provider_type}"
                )
                return None

            instance = provider_cls(config, self._provider_settings())
            initialize = getattr(instance, "initialize", None)
            if callable(initialize):
                result = initialize()
                if hasattr(result, "__await__"):
                    await result
            return instance
        except Exception as exc:
            logger.warning(
                f"[OnlyYou] build chat provider {source_id}/{model} failed: {exc}"
            )
            return None

    @staticmethod
    def _fallback_import_provider(provider_type: str) -> None:
        """Best-effort import when ``provider_manager.dynamic_import_provider`` is absent."""
        if provider_type == "openai_chat_completion":
            try:
                import astrbot.core.provider.sources.openai_source  # noqa: F401
            except Exception:
                pass
        elif provider_type == "openai_tts_api":
            try:
                import astrbot.core.provider.sources.openai_tts_api_source  # noqa: F401
            except Exception:
                pass

    async def get_chat_provider(self, source_id: str, model: str) -> Any:
        """Return a chat provider instance for ``(source_id, model)``.

        Order: memory cache -> reuse an already loaded AstrBot instance ->
        build a plugin-owned instance. Returns ``None`` on any failure.
        """
        source_id = _clean(source_id)
        model = _clean(model)
        if not source_id or not model:
            return None

        cache_key = (source_id, model)
        cached = self._chat_cache.get(cache_key)
        if cached is not None:
            if self._is_loaded_instance(cached):
                # AstrBot's ``ProviderManager.reload()`` terminates every inst_map key
                # that is not part of the user config — including our ``onlyyou::main``.
                # Detect that case so the next message rebuilds a healthy instance.
                if (
                    id(cached) in self._owned_ids
                    and cached is self._main_instance
                    and self.MAIN_PROVIDER_ID not in self._inst_map()
                ):
                    self._owned_ids.discard(id(cached))
                    try:
                        self._owned_instances.remove(cached)
                    except ValueError:
                        pass
                    self._main_instance = None
                    self._chat_cache.pop(cache_key, None)
                else:
                    return cached
            else:
                # The AstrBot instance was reloaded/terminated; drop the stale cache.
                self._chat_cache.pop(cache_key, None)

        reused = self.find_reusable_chat_provider(source_id, model)
        if reused is not None:
            self._chat_cache[cache_key] = reused
            return reused

        instance = await self._instantiate_chat_provider(source_id, model)
        if instance is None:
            return None
        self._track_owned(instance)
        self._chat_cache[cache_key] = instance
        return instance

    def register_main(self, instance: Any) -> str:
        """Expose a plugin-built main provider to AstrBot's native pipeline."""
        if instance is None:
            return ""
        try:
            inst_map = self._inst_map()
            if not isinstance(inst_map, dict):
                logger.warning("[OnlyYou] provider_manager.inst_map unavailable")
                return ""
            inst_map[self.MAIN_PROVIDER_ID] = instance
            self._main_instance = instance
            meta = self._safe_meta(instance)
            logger.info(
                "[OnlyYou] Registered plugin-built main provider as %s "
                "(type=%s, model=%s)",
                self.MAIN_PROVIDER_ID,
                meta.get("type") or "?",
                meta.get("model") or getattr(instance, "model_name", "") or "?",
            )
            return self.MAIN_PROVIDER_ID
        except Exception as exc:
            logger.warning(f"[OnlyYou] register_main failed: {exc}")
            return ""

    def unregister_main(self) -> None:
        """Remove the ``onlyyou::main`` mapping (instance itself is kept for caching)."""
        try:
            inst_map = self._inst_map()
            if isinstance(inst_map, dict):
                inst_map.pop(self.MAIN_PROVIDER_ID, None)
        except Exception as exc:
            logger.warning(f"[OnlyYou] unregister_main failed: {exc}")
        finally:
            self._main_instance = None

    # ------------------------------------------------------------------
    # TTS provider construction / reuse
    # ------------------------------------------------------------------
    @staticmethod
    def _looks_like_tts(instance: Any) -> bool:
        try:
            from astrbot.core.provider.provider import TTSProvider

            if isinstance(instance, TTSProvider):
                return True
        except Exception:
            pass
        return callable(getattr(instance, "get_audio", None))

    async def get_tts_provider(self, tts_cfg: dict) -> Any:
        """Build / reuse a TTS provider according to the plugin TTS config."""
        cfg = _as_dict(tts_cfg)
        mode = _clean(cfg.get("mode")).lower() or "off"
        if mode == "off":
            return None

        if mode == "provider":
            provider_id = _clean(cfg.get("provider_id"))
            if not provider_id:
                return None
            cache_key = "provider::" + provider_id
            cached = self._tts_cache.get(cache_key)
            if cached is not None and self._is_loaded_instance(cached):
                return cached
            instance = None
            try:
                get_by_id = _getattr_safe(self.context, "get_provider_by_id", None)
                if callable(get_by_id):
                    instance = get_by_id(provider_id)
                    if inspect.isawaitable(instance):
                        instance = await instance
            except Exception as exc:
                logger.warning(
                    f"[OnlyYou] get_provider_by_id({provider_id}) failed: {exc}"
                )
            if instance is None:
                logger.warning(
                    f"[OnlyYou] TTS provider not found: {provider_id}"
                )
                return None
            if not self._looks_like_tts(instance):
                logger.warning(
                    f"[OnlyYou] provider {provider_id} is not a TTS provider"
                )
                return None
            self._tts_cache[cache_key] = instance
            return instance

        if mode == "source":
            source_id = _clean(cfg.get("source_id"))
            model = _clean(cfg.get("model"))
            voice = _clean(cfg.get("voice")) or "alloy"
            if not source_id or not model:
                return None
            cache_key = f"source::{source_id}::{model}::{voice}"
            cached = self._tts_cache.get(cache_key)
            if cached is not None and self._is_loaded_instance(cached):
                return cached

            source = self._find_source(source_id)
            if source is None:
                logger.warning(
                    f"[OnlyYou] TTS provider source not found: {source_id}"
                )
                return None
            try:
                api_key = self._resolve_single_key(source.get("key"))
                entry = {
                    "id": self.TTS_PROVIDER_ID,
                    "type": "openai_tts_api",
                    "provider_type": "text_to_speech",
                    "provider_source_id": source_id,
                    "api_base": _clean(source.get("api_base")),
                    "api_key": api_key,
                    "key": api_key,
                    "model": model,
                    "openai-tts-voice": voice,
                    "timeout": source.get("timeout") or 60,
                    "proxy": _clean(source.get("proxy")),
                    "enable": True,
                }
                instance = await self._instantiate_tts_provider(entry)
            except Exception as exc:
                logger.warning(f"[OnlyYou] build TTS provider failed: {exc}")
                return None
            if instance is None:
                return None
            self._track_owned(instance)
            self._tts_cache[cache_key] = instance
            return instance

        logger.warning(f"[OnlyYou] unknown TTS mode: {mode}")
        return None

    async def _instantiate_tts_provider(self, entry: dict) -> Any:
        """Instantiate ``openai_tts_api`` from a fully merged config dict."""
        try:
            pm = self.provider_manager
            if pm is None:
                return None
            provider_type = _clean(entry.get("type")) or "openai_tts_api"

            dynamic_import = getattr(pm, "dynamic_import_provider", None)
            if callable(dynamic_import):
                dynamic_import(provider_type)
            else:
                self._fallback_import_provider(provider_type)

            meta_data = self._provider_cls_map().get(provider_type)
            provider_cls = _getattr_safe(meta_data, "cls_type", None)
            if provider_cls is None:
                logger.warning(
                    f"[OnlyYou] TTS adapter not registered: {provider_type}"
                )
                return None

            instance = provider_cls(entry, self._provider_settings())
            initialize = getattr(instance, "initialize", None)
            if callable(initialize):
                result = initialize()
                if hasattr(result, "__await__"):
                    await result
            return instance
        except Exception as exc:
            logger.warning(f"[OnlyYou] instantiate TTS provider failed: {exc}")
            return None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def dispose(self) -> None:
        """Remove the registered main provider and terminate every plugin-built instance."""
        self.unregister_main()

        seen: set = set()
        owned = list(self._owned_instances)
        for instance in owned:
            if instance is None or id(instance) in seen:
                continue
            seen.add(id(instance))
            try:
                terminate = getattr(instance, "terminate", None)
                if callable(terminate):
                    result = terminate()
                    if hasattr(result, "__await__"):
                        await result
            except Exception as exc:
                logger.warning(f"[OnlyYou] terminate provider instance failed: {exc}")

        self._owned_instances.clear()
        self._owned_ids.clear()
        self._chat_cache.clear()
        self._tts_cache.clear()
        self._main_instance = None
