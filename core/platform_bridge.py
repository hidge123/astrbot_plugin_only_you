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
    """

    def __init__(self, context: Any):
        self.context = context

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

