"""AstrBot platform configuration bridge (segmented reply, wake control, streaming, TTS)."""

import asyncio
import random
import re
from typing import Any, Optional

try:
    from astrbot.api import logger
    from astrbot.api.event import MessageChain
except ImportError:
    import logging
    logger = logging.getLogger("onlyyou.platform_bridge")
    MessageChain = None

try:
    from astrbot.api.message_components import Record
except ImportError:  # pragma: no cover - local offline tests
    class Record:  # type: ignore[no-redef]
        def __init__(
            self,
            file: Optional[str] = None,
            url: Optional[str] = None,
            text: Optional[str] = None,
            **kwargs: Any,
        ):
            self.file = file
            self.url = url
            self.text = text


class PlatformBridge:
    """
    AstrBot 平台配置协同桥：
    1. 协同控制群聊/私聊唤醒与拦截（适配 AstrBot v4 ProcessStage & should_call_llm）
    2. 后台主动发消息时复用平台 segmented_reply 配置与打字延迟
    3. 感知平台 streaming_response 开关
    4. 枚举 AstrBot 机器人接口（platform_id），供 Soul 绑定与路由
    """

    def __init__(self, context: Any):
        self.context = context

    # ==================================================================
    # Bot platform instances (v2.8: Soul -> platform_id binding)
    # ==================================================================
    @staticmethod
    def extract_platform_id(umo: Any) -> str:
        """``"<platform_id>:<msg_type>:<session_id>"`` -> first segment (``""`` safe)."""
        if not umo:
            return ""
        text = str(umo).strip()
        if not text:
            return ""
        return text.split(":", 1)[0].strip()

    @staticmethod
    def _meta_attr(meta: Any, key: str) -> str:
        try:
            value = getattr(meta, key, "")
            if value is None and isinstance(meta, dict):
                value = meta.get(key)
            return str(value or "").strip()
        except Exception:
            return ""

    def _runtime_instances(self) -> list[dict[str, Any]]:
        """AstrBot currently loaded bot instances (``platform_manager.get_insts()``)."""
        items: list[dict[str, Any]] = []
        try:
            pm = getattr(self.context, "platform_manager", None) if self.context is not None else None
        except Exception as exc:  # noqa: BLE001 - broken contexts must not crash discovery
            logger.debug(f"[OnlyYou] platform_manager unavailable: {exc}")
            pm = None
        if pm is None:
            return items
        getter = getattr(pm, "get_insts", None)
        insts: Any = []
        try:
            if callable(getter):
                insts = getter()
            else:
                insts = getattr(pm, "platform_insts", None) or getattr(pm, "insts", None) or []
        except Exception:
            insts = []
        try:
            for inst in list(insts or []):
                meta = None
                try:
                    meta_getter = getattr(inst, "meta", None)
                    if callable(meta_getter):
                        meta = meta_getter()
                except Exception:
                    meta = None
                platform_id = self._meta_attr(meta, "id") or str(
                    getattr(inst, "platform_id", "") or ""
                ).strip()
                if not platform_id:
                    continue
                name = self._meta_attr(meta, "name") or platform_id
                type_name = self._meta_attr(meta, "type") or str(
                    getattr(inst, "platform", "") or ""
                ).strip()
                items.append(
                    {
                        "platform_id": platform_id,
                        "platform_type": type_name or platform_id,
                        "name": name,
                        "enabled": True,
                        "running": True,
                        "source": "runtime",
                    }
                )
        except Exception as exc:  # noqa: BLE001 - discovery must never crash callers
            logger.debug(f"[OnlyYou] runtime platform discovery failed: {exc}")
        return items

    def _config_instances(self) -> list[dict[str, Any]]:
        """AstrBot configured bot instances (``context.get_config()["platform"]``)."""
        items: list[dict[str, Any]] = []
        if self.context is None:
            return items
        try:
            ab_conf = self.context.get_config() or {}
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[OnlyYou] platform config discovery failed: {exc}")
            return items
        try:
            for entry in ab_conf.get("platform", []) or []:
                if not isinstance(entry, dict):
                    continue
                platform_id = str(entry.get("id") or "").strip()
                if not platform_id:
                    continue
                enable = entry.get("enable", True)
                items.append(
                    {
                        "platform_id": platform_id,
                        "platform_type": str(entry.get("type") or platform_id).strip(),
                        "name": str(entry.get("name") or platform_id).strip(),
                        "enabled": True if enable is None else bool(enable),
                        "running": False,
                        "source": "config",
                    }
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[OnlyYou] platform config parse failed: {exc}")
        return items

    @staticmethod
    def _observed_instances(observed_umos: Optional[list[str]]) -> list[dict[str, Any]]:
        """Fallback entries inferred from historical UMOs (``platform:type:session``)."""
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        for umo in list(observed_umos or []):
            platform_id = PlatformBridge.extract_platform_id(umo)
            if not platform_id or platform_id in seen:
                continue
            seen.add(platform_id)
            items.append(
                {
                    "platform_id": platform_id,
                    "platform_type": platform_id,
                    "name": platform_id,
                    "enabled": True,
                    "running": False,
                    "source": "observed",
                }
            )
        return items

    def list_platform_instances(
        self, observed_umos: Optional[list[str]] = None
    ) -> list[dict[str, Any]]:
        """Enumerate AstrBot bot instances for the Soul binding UI (v2.8).

        Sources merged by ``platform_id`` (first wins for descriptive fields, flags
        are OR-ed): runtime ``platform_manager.get_insts()`` -> configured
        ``platform`` entries -> historical UMO observations.  Never raises.
        """
        merged: dict[str, dict[str, Any]] = {}

        def _merge(items: list[dict[str, Any]]) -> None:
            for item in items:
                pid = str(item.get("platform_id") or "").strip()
                if not pid:
                    continue
                current = merged.get(pid)
                if current is None:
                    merged[pid] = dict(item)
                    continue
                # Running/enabled are sticky; keep the most informative name/type.
                current["enabled"] = bool(current.get("enabled")) or bool(
                    item.get("enabled")
                )
                current["running"] = bool(current.get("running")) or bool(
                    item.get("running")
                )
                for key in ("platform_type", "name"):
                    if not str(current.get(key) or "").strip() and str(
                        item.get(key) or ""
                    ).strip():
                        current[key] = item[key]

        _merge(self._runtime_instances())
        _merge(self._config_instances())
        _merge(self._observed_instances(observed_umos))
        result = list(merged.values())
        result.sort(key=lambda item: (not bool(item.get("running")), str(item.get("platform_id"))))
        return result

    @staticmethod
    def apply_reply_decision(event: Any, should_reply: bool) -> None:
        """
        Coordinate with AstrBot's native pipeline whether to invoke the main LLM.

        Note on AstrBot v4 internals:
        - In AstrBot's `AstrMessageEvent`, `event.call_llm` means "whether to DISABLE
          the default LLM request in this event" (`False` by default), and
          `event.should_call_llm(val)` sets `self.call_llm = val`.
        - `ProcessStage.process` runs `agent_sub_stage` iff:
          `not event._has_send_oper and event.is_at_or_wake_command and not event.call_llm`.
        - Therefore:
          * When `should_reply=True`: we set `event.is_at_or_wake_command = True` and
            `event.should_call_llm(False)` (`event.call_llm = False`) so the native LLM
            pipeline executes even for un-mentioned group messages that passed AtmosphereReader.
          * When `should_reply=False`: we set `event.is_at_or_wake_command = False` and
            `event.should_call_llm(True)` (`event.call_llm = True`) so the native LLM
            pipeline is silently skipped without breaking event post-processing.
        """
        if should_reply:
            event.is_at_or_wake_command = True
            event.is_wake = True
            if hasattr(event, "should_call_llm"):
                event.should_call_llm(False)
            else:
                event.call_llm = False
        else:
            event.is_at_or_wake_command = False
            if hasattr(event, "should_call_llm"):
                event.should_call_llm(True)
            else:
                event.call_llm = True

    def is_streaming_enabled(self, umo: Optional[str] = None) -> bool:
        """Check if streaming response is enabled in the UMO's platform config."""
        if self.context is None:
            return False
        try:
            ab_conf = self.context.get_config(umo=umo) or {}
            return bool(
                (ab_conf.get("provider_settings") or {}).get("streaming_response", False)
            )
        except Exception:
            return False

    def get_platform_admins(self, umo: Optional[str] = None) -> list[str]:
        """Read admins_id from AstrBot platform config."""
        if self.context is None:
            return []
        try:
            ab_conf = self.context.get_config(umo=umo) or {}
            admins = ab_conf.get("admins_id", []) or []
            return [str(a).strip() for a in admins if str(a).strip()]
        except Exception:
            return []

    def split_by_platform_segmented_config(
        self, text: str, umo: Optional[str] = None
    ) -> tuple[list[str], tuple[float, float]]:
        """
        Split proactive message text using AstrBot's native `platform_settings.segmented_reply`
        rules if enabled, returning `(segments, (min_delay, max_delay))`.
        """
        if not text:
            return [], (0.5, 1.5)

        if self.context is None:
            return [text], (0.5, 1.5)

        try:
            ab_conf = self.context.get_config(umo=umo) or {}
            ps = ab_conf.get("platform_settings", {}) or {}
            enabled = bool(ps.get("enable_segmented_reply", False))
            if not enabled:
                return [text], (0.5, 1.5)

            words_threshold = int(ps.get("words_count_threshold", 150) or 150)
            if len(text) > words_threshold:
                return [text], (0.5, 1.5)

            regex_str = ps.get("split_mode", "") or r".*?[。？！~…]+|.+$"
            interval_str = str(ps.get("interval", "1.5,3.5") or "1.5,3.5")
            parts = [float(x.strip()) for x in interval_str.split(",") if x.strip()]
            delay_range = (
                (parts[0], parts[1]) if len(parts) >= 2 else (1.0, 2.5)
            )

            segments = [s.strip() for s in re.findall(regex_str, text) if s.strip()]
            return (segments or [text]), delay_range
        except Exception as e:
            logger.debug(f"[OnlyYou] split_by_platform_segmented_config fallback: {e}")
            return [text], (0.5, 1.5)

    async def send_proactive_message(
        self,
        umo: str,
        text: str,
        session_buffer: Any = None,
        soul_name: str = "Bot",
        audio_path: Optional[str] = None,
    ) -> bool:
        """
        Send a proactive message to `umo` reusing AstrBot's `segmented_reply` config,
        and record the proactive message into `session_buffer` so the bot never forgets
        what it proactively said!

        When `audio_path` is given (plugin TTS), a `Record` voice message is sent after
        the text segments.
        """
        if not umo or not text or self.context is None:
            return False

        segments, (min_d, max_d) = self.split_by_platform_segmented_config(text, umo=umo)
        sent_any = False

        for idx, seg in enumerate(segments):
            try:
                if MessageChain is not None:
                    chain = MessageChain().message(seg)
                    await self.context.send_message(umo, chain)
                else:
                    await self.context.send_message(umo, seg)
                sent_any = True
            except Exception as e:
                logger.warning(f"[OnlyYou] send_proactive_message to {umo} failed: {e}")
                break

            if idx < len(segments) - 1:
                await asyncio.sleep(random.uniform(min_d, max_d))

        if audio_path:
            sent_audio = await self.send_voice_message(
                umo=umo, audio_path=str(audio_path), text=text
            )
            sent_any = sent_any or sent_audio

        if session_buffer is not None:
            session_buffer.record_assistant_message(
                umo=umo,
                content=text,
                is_proactive=True,
                soul_name=soul_name,
            )

        return sent_any

    async def send_voice_message(
        self, umo: str, audio_path: str, text: str = ""
    ) -> bool:
        """Send a single `Record` voice message to `umo` (best effort)."""
        if not umo or not audio_path or self.context is None:
            return False
        try:
            if MessageChain is not None:
                chain = MessageChain()
                try:
                    component = Record(file=audio_path, url=audio_path, text=text)
                except TypeError:
                    component = Record(file=audio_path)
                chain_attr = getattr(chain, "chain", None)
                if isinstance(chain_attr, list):
                    chain_attr.append(component)
                await self.context.send_message(umo, chain)
            else:
                await self.context.send_message(umo, audio_path)
            return True
        except Exception as e:
            logger.warning(f"[OnlyYou] send_voice_message to {umo} failed: {e}")
            return False

