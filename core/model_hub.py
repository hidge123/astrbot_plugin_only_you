"""Unified multi-model scheduling gateway (ModelHub) with AstrBot config inheritance."""

import json
import re
from typing import Any, Literal, Optional

try:
    from astrbot.api import logger
except ImportError:
    import logging
    logger = logging.getLogger("onlyyou.model_hub")


class ModelHub:
    """
    插件统一模型调度网关：支持多类别模型配置与平台配置自动继承
    1. main: 主对话模型
    2. small: 小脑轻量模型（读空气/提炼记忆/日程微调）
    3. vision: 多模态视觉模型（看B站/抖音九宫格与视频）
    4. embedding / rerank / tts / t2i: 辅助模型
    """

    def __init__(self, context: Any, plugin_config: dict[str, Any]):
        self.context = context
        self.plugin_config = plugin_config

    @property
    def cfg(self) -> dict[str, Any]:
        return self.plugin_config.get("model_hub_config", {}) or {}

    async def get_chat_provider_id(
        self,
        role: Literal["main", "small", "vision"] = "main",
        umo: Optional[str] = None,
    ) -> str:
        """
        Resolve provider ID with 3-level fallback:
        1. Explicit role provider in plugin config
        2. Fallback to main_llm_provider_id (or platform default_image_caption_provider_id for vision)
        3. Inherit from current UMO's AstrBot configuration profile
        """
        explicit_id = (self.cfg.get(f"{role}_llm_provider_id") or "").strip()
        if explicit_id:
            return explicit_id

        if role == "vision":
            try:
                ab_conf = self.context.get_config(umo=umo) if self.context else {}
                caption_id = (
                    (ab_conf or {})
                    .get("provider_settings", {})
                    .get("default_image_caption_provider_id", "")
                )
                if caption_id:
                    return caption_id
            except Exception:
                pass

        if role in ("small", "vision"):
            main_id = (self.cfg.get("main_llm_provider_id") or "").strip()
            if main_id:
                return main_id

        if self.context is not None:
            try:
                prov_id = await self.context.get_current_chat_provider_id(umo=umo)
                if prov_id:
                    return prov_id
            except Exception as e:
                logger.warning(f"[OnlyYou] get_current_chat_provider_id failed: {e}")

            # Fallback to first available chat provider
            try:
                prov = self.context.get_using_provider(umo=umo)
                if prov and hasattr(prov, "meta"):
                    return prov.meta().id
            except Exception:
                pass

        return ""

    def get_auxiliary_provider(
        self,
        capability: Literal["embedding", "rerank", "tts", "t2i"],
        umo: Optional[str] = None,
    ) -> Any:
        """获取 Embedding / Rerank / TTS / 生图模型实例（插件覆盖 -> 平台配置继承 -> None降级）"""
        if self.context is None:
            return None

        explicit_id = (self.cfg.get(f"{capability}_provider_id") or "").strip()
        if explicit_id:
            try:
                return self.context.get_provider_by_id(explicit_id)
            except Exception:
                pass

        try:
            ab_conf = self.context.get_config(umo=umo) or {}
            ps = ab_conf.get("provider_settings", {}) or {}
            platform_prov_id = ps.get(f"{capability}_provider_id", "")
            if not platform_prov_id and capability == "tts":
                platform_prov_id = (
                    (ab_conf.get("provider_tts_settings") or {}).get("provider_id", "")
                )
            if platform_prov_id:
                return self.context.get_provider_by_id(platform_prov_id)
        except Exception as e:
            logger.debug(f"[OnlyYou] get_auxiliary_provider({capability}) fallback: {e}")
        return None

    async def call_llm_text(
        self,
        prompt: str,
        role: Literal["main", "small", "vision"] = "small",
        umo: Optional[str] = None,
        system_prompt: str = "",
        image_urls: Optional[list[str]] = None,
    ) -> str:
        """Call an AstrBot chat provider and return the raw text response."""
        if self.context is None:
            return ""

        provider_id = await self.get_chat_provider_id(role=role, umo=umo)
        if not provider_id:
            logger.warning(f"[OnlyYou] No available chat provider for role={role}")
            return ""

        try:
            llm_resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                system_prompt=system_prompt or None,
                image_urls=image_urls or None,
            )
            if llm_resp and getattr(llm_resp, "completion_text", None):
                return llm_resp.completion_text.strip()
        except Exception as e:
            logger.warning(
                f"[OnlyYou] llm_generate failed via provider={provider_id}: {e}"
            )
            # Secondary fallback via provider.text_chat
            try:
                provider = self.context.get_provider_by_id(provider_id)
                if provider is not None:
                    resp = await provider.text_chat(
                        prompt=prompt,
                        system_prompt=system_prompt or None,
                        image_urls=image_urls or None,
                    )
                    if resp and getattr(resp, "completion_text", None):
                        return resp.completion_text.strip()
            except Exception as inner_e:
                logger.error(f"[OnlyYou] Fallback text_chat failed: {inner_e}")

        return ""

    async def call_llm_json(
        self,
        prompt: str,
        role: Literal["main", "small", "vision"] = "small",
        umo: Optional[str] = None,
        system_prompt: str = "",
        image_urls: Optional[list[str]] = None,
        default: Any = None,
    ) -> Any:
        """Call LLM and parse JSON object/array from the completion text."""
        raw = await self.call_llm_text(
            prompt=prompt,
            role=role,
            umo=umo,
            system_prompt=system_prompt,
            image_urls=image_urls,
        )
        if not raw:
            return default
        parsed = self.extract_json(raw)
        return parsed if parsed is not None else default

    @staticmethod
    def extract_json(text: str) -> Any:
        """Robustly extract JSON object or array from LLM markdown/text output."""
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
