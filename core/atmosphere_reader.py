"""Group chat atmosphere reader: debounce window, reply-chain untangling, and 5D evaluation."""

import asyncio
from typing import Any, Optional

from .context_budget import SessionBuffer
from .model_hub import ModelHub
from .roguelite_deck import SlotInstance
from .soul_manager import SoulProfile
from .state_machine import BotState, UserTier
from .user_relation import UserProfile
from ..prompts.templates import ATMOSPHERE_EVAL_PROMPT

try:
    from astrbot.api import logger
except ImportError:
    import logging
    logger = logging.getLogger("onlyyou.atmosphere_reader")

try:
    from .character_card import render_card_summary
except ImportError:  # pragma: no cover - character_card is created in parallel
    def render_card_summary(card: Any, max_chars: int = 200) -> str:  # type: ignore[misc]
        return ""


def _soul_summary(soul: Any) -> str:
    """Render the compact `{soul_summary}` snippet of a soul (safe fallback)."""
    try:
        card = getattr(soul, "card", None)
        if not card:
            return ""
        return render_card_summary(card)
    except Exception:
        return ""


class AtmosphereReader:
    """
    群聊主战场：防抖等待窗口、多人对话“解线团”、五维读空气器 (`core/atmosphere_reader.py`)
    """

    def __init__(
        self,
        session_buffer: SessionBuffer,
        model_hub: ModelHub,
        plugin_config: dict[str, Any],
    ):
        self.session_buffer = session_buffer
        self.model_hub = model_hub
        self.plugin_config = plugin_config
        self._latest_seq: dict[str, int] = {}

    @property
    def cfg(self) -> dict[str, Any]:
        return self.plugin_config.get("atmosphere_config", {}) or {}

    @property
    def takeover_group_chat(self) -> bool:
        return bool(self.cfg.get("takeover_group_chat", True))

    @property
    def debounce_seconds(self) -> float:
        return max(0.0, float(self.cfg.get("debounce_seconds", 4.0)))

    @staticmethod
    def extract_reply_topology(event: Any) -> tuple[str, bool]:
        """
        Inspect AstrBot message components (`Reply`, `At`) to untangle who the user is
        talking to (`reply_to_desc`) and whether the bot is directly mentioned (`is_direct_mention`).
        """
        reply_to = ""
        is_direct = bool(getattr(event, "is_at_or_wake_command", False))
        self_id = ""
        if hasattr(event, "get_self_id"):
            try:
                self_id = str(event.get_self_id() or "")
            except Exception:
                self_id = ""

        msg_obj = getattr(event, "message_obj", None)
        chain = getattr(msg_obj, "message", None) if msg_obj else None
        if isinstance(chain, list):
            for seg in chain:
                seg_type = type(seg).__name__.lower()
                if "reply" in seg_type:
                    sender_nick = getattr(seg, "sender_nickname", "") or getattr(
                        seg, "sender_id", ""
                    )
                    snippet = getattr(seg, "message_str", "") or ""
                    if sender_nick:
                        reply_to = f"@{sender_nick}"
                        if snippet:
                            reply_to += f"「{str(snippet)[:18]}」"
                    if self_id and str(getattr(seg, "sender_id", "")) == self_id:
                        is_direct = True
                elif "at" in seg_type:
                    qq = str(getattr(seg, "qq", "") or getattr(seg, "target", ""))
                    name = getattr(seg, "name", "") or qq
                    if self_id and qq == self_id:
                        is_direct = True
                    elif qq and not reply_to:
                        reply_to = f"@{name}"

        return reply_to, is_direct

    @staticmethod
    def is_private_event(event: Any) -> bool:
        """Determine whether `event` is a private chat (or OpenAPI direct session)."""
        if hasattr(event, "is_private_chat"):
            try:
                return bool(event.is_private_chat())
            except Exception:
                pass
        group_id = ""
        if hasattr(event, "get_group_id"):
            try:
                group_id = str(event.get_group_id() or "")
            except Exception:
                group_id = ""
        return not bool(group_id)

    def record_message(
        self,
        event: Any,
        user: UserProfile,
        on_overflow_callback: Any = None,
    ) -> tuple[str, bool]:
        """
        Record incoming `event` into `SessionBuffer` with reply-chain topology and user tier.
        Returns `(reply_to, is_direct_mention)`.
        """
        umo = getattr(event, "unified_msg_origin", "") or "default_umo"
        msg_str = getattr(event, "message_str", "") or ""
        reply_to, is_direct = self.extract_reply_topology(event)

        self.session_buffer.record_user_message(
            umo=umo,
            user=user,
            content=msg_str,
            reply_to=reply_to,
            on_overflow_callback=on_overflow_callback,
        )
        return reply_to, is_direct

    async def evaluate_should_reply(
        self,
        event: Any,
        soul: SoulProfile,
        user: UserProfile,
        current_slot: SlotInstance,
        is_direct_mention: bool = False,
    ) -> bool:
        """
        Evaluate whether the bot should reply to `event` (assuming state gate already passed):
        - In private chat: always reply (`True`) once state gate allows.
        - In group chat:
          * If `takeover_group_chat` is False: follow native `is_direct_mention`.
          * Apply debounce wait window (`debounce_seconds`) if not a direct mention; if a newer
            message arrives in the same group during debounce, let the newer message handle the turn.
          * If directly mentioned (`@` / quote / wake word) or sent by L3 Core User in IDLE: reply.
          * In BUSY state (only L2/L3 reach here): only reply in group if directly mentioned or L3.
          * In IDLE state: run small-LLM 5D atmosphere evaluation (`ATMOSPHERE_EVAL_PROMPT`).
        """
        if self.is_private_event(event):
            return True

        if not self.takeover_group_chat:
            return is_direct_mention

        umo = getattr(event, "unified_msg_origin", "") or "default_umo"
        msg_str = (getattr(event, "message_str", "") or "").strip()
        if not msg_str:
            return False

        # In BUSY state, only reply in group chat if directly mentioned or L3 Core User calls
        if current_slot.state == BotState.BUSY:
            return is_direct_mention or user.tier == UserTier.L3_CORE

        # Fast-path: direct mention in IDLE state
        if is_direct_mention:
            return True

        # Group debounce window for un-mentioned group messages
        seq = self._latest_seq.get(umo, 0) + 1
        self._latest_seq[umo] = seq
        wait_s = min(self.debounce_seconds, 6.0)
        if wait_s > 0:
            await asyncio.sleep(wait_s)
            if self._latest_seq.get(umo, 0) != seq:
                # A newer message arrived in the group during the debounce window
                return False

        # Evaluate group atmosphere via the decision model
        transcript = self.session_buffer.get_window_transcript(umo=umo, max_turns=10)
        prompt = ATMOSPHERE_EVAL_PROMPT.format(
            soul_name=soul.name,
            soul_archetype=soul.archetype,
            current_state=current_slot.state.value,
            current_slot_title=current_slot.title,
            window_transcript=transcript,
            sender_name=user.display_name,
            sender_id=user.global_uid,
            user_tier_label=user.tier.label,
            latest_message=msg_str,
            is_direct_mention="是" if is_direct_mention else "否",
            surfing_preferences="、".join(soul.surfing_preferences[:5]),
            soul_summary=_soul_summary(soul),
        )

        res = await self.model_hub.call_llm_json(
            prompt=prompt, role="decision", umo=umo, default=None
        )
        if isinstance(res, dict):
            decision = str(res.get("decision", "IGNORE")).upper()
            logger.debug(
                f"[OnlyYou] AtmosphereReader decision={decision} ({res.get('reason', '')})"
            )
            return decision == "REPLY"

        # Fallback heuristic if LLM JSON fails: reply if soul's name or surfing preference is mentioned
        if soul.name and soul.name in msg_str:
            return True
        if any(pref in msg_str for pref in soul.surfing_preferences):
            return True
        return False
