"""Group chat atmosphere reader: debounce window, reply-chain untangling, and 5D evaluation."""

import asyncio
import random
import time
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


def _as_bool(value: Any, default: bool = True) -> bool:
    """Coerce a config value into bool (handles ``"false"`` / ``0`` / ``"off"``)."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() not in ("", "false", "0", "no", "off", "none")


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

    v2.10 adds a probability / budget reply gate **between** the direct-mention +
    BUSY fast paths and the debounce window: ordinary (non-@) group messages are
    sampled by ``reply_gate_probability`` and optionally capped by
    ``reply_gate_hourly_limit`` before any decision-model call happens. @ / wake /
    quote messages, BUSY special cases and private chats return earlier and are
    never affected by the gate.
    """

    #: Rolling gate budget window (one hour, in seconds).
    GATE_BUDGET_WINDOW_SECONDS = 3600.0

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
        # v2.10: per-UMO timestamps of ordinary group messages that passed the
        # probabilistic gate (used only when ``reply_gate_hourly_limit`` > 0).
        self._gate_pass_ts: dict[str, list[float]] = {}

    @property
    def cfg(self) -> dict[str, Any]:
        return self.plugin_config.get("atmosphere_config", {}) or {}

    @property
    def takeover_group_chat(self) -> bool:
        return bool(self.cfg.get("takeover_group_chat", True))

    @property
    def debounce_seconds(self) -> float:
        try:
            return max(0.0, float(self.cfg.get("debounce_seconds", 4.0)))
        except (TypeError, ValueError):
            return 4.0

    @property
    def atmosphere_transcript_turns(self) -> int:
        """Group transcript turns read by the 5D atmosphere model (default 12)."""
        try:
            value = int(self.cfg.get("atmosphere_transcript_turns", 12))
        except (TypeError, ValueError):
            value = 12
        return max(1, min(100, value))

    # ------------------------------------------------------------------
    # v2.10: probability / budget reply gate (non-@ ordinary group messages only)
    # ------------------------------------------------------------------
    @property
    def reply_gate_enabled(self) -> bool:
        """Whether the probabilistic reply gate is active (default: on)."""
        return _as_bool(self.cfg.get("reply_gate_enabled", True), True)

    @property
    def reply_gate_probability(self) -> float:
        """Percentage (0~100) of ordinary group messages admitted to read-air."""
        try:
            value = float(self.cfg.get("reply_gate_probability", 80))
        except (TypeError, ValueError):
            return 80.0
        return max(0.0, min(100.0, value))

    @property
    def reply_gate_hourly_limit(self) -> int:
        """Max gate passes per UMO per rolling hour (``0`` = unlimited)."""
        try:
            value = int(float(self.cfg.get("reply_gate_hourly_limit", 0)))
        except (TypeError, ValueError):
            return 0
        return max(0, value)

    def _passes_reply_gate(self, umo: str) -> bool:
        """Decide whether one non-@ ordinary group message may enter read-air.

        Order of checks: probability first, then the optional rolling hourly
        budget. Rejected messages are logged once and never reach the decision
        model or the debounce window; they are **not** unread (they already
        passed the three-state gate), so the unread backlog is untouched.
        """
        if not self.reply_gate_enabled:
            return True

        probability = self.reply_gate_probability
        if probability < 100.0:
            if random.random() * 100.0 >= probability:
                logger.info(
                    "[OnlyYou] Reply gate dropped non-@ group message "
                    f"(probability={probability:.0f}%, umo={umo})"
                )
                return False

        limit = self.reply_gate_hourly_limit
        if limit <= 0:
            return True

        now = time.time()
        cutoff = now - self.GATE_BUDGET_WINDOW_SECONDS
        stamps = [
            stamp
            for stamp in self._gate_pass_ts.get(umo, [])
            if float(stamp) >= cutoff
        ]
        if len(stamps) >= limit:
            self._gate_pass_ts[umo] = stamps
            logger.info(
                "[OnlyYou] Reply gate dropped non-@ group message "
                f"(hourly limit={limit}, umo={umo})"
            )
            return False
        stamps.append(now)
        self._gate_pass_ts[umo] = stamps
        return True

    def clear_gate_budget(
        self, umo: Optional[str] = None, platform_id: str = ""
    ) -> int:
        """Drop rolling gate-budget bookkeeping and return the removed UMO count.

        - ``umo`` given: clear that one session only;
        - ``platform_id`` given: clear every session of that bot instance
          (used by ``main.clear_platform_memory_state`` on Soul unbind);
        - neither: clear everything.
        """
        if umo:
            return 1 if self._gate_pass_ts.pop(umo, None) is not None else 0
        clean = str(platform_id or "").strip()
        if not clean:
            removed = len(self._gate_pass_ts)
            self._gate_pass_ts.clear()
            return removed
        prefix = f"{clean}:"
        doomed = [
            key for key in list(self._gate_pass_ts.keys()) if str(key).startswith(prefix)
        ]
        for key in doomed:
            self._gate_pass_ts.pop(key, None)
        return len(doomed)

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
    ) -> tuple[str, bool, Any]:
        """
        Record incoming `event` into `SessionBuffer` with reply-chain topology and user tier.
        Returns `(reply_to, is_direct_mention, buffered_message)`; the buffered message
        reference lets the three-state gate mark this exact message as unread.
        """
        umo = getattr(event, "unified_msg_origin", "") or "default_umo"
        msg_str = getattr(event, "message_str", "") or ""
        reply_to, is_direct = self.extract_reply_topology(event)

        buffered = self.session_buffer.record_user_message(
            umo=umo,
            user=user,
            content=msg_str,
            reply_to=reply_to,
            on_overflow_callback=on_overflow_callback,
        )
        return reply_to, is_direct, buffered

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
          * If directly mentioned (`@` / quote / wake word) or sent by L3 Core User in IDLE: reply.
          * In BUSY state (only L2/L3 reach here): only reply in group if directly mentioned or L3.
          * v2.10 probability / budget gate: ordinary (non-@) group messages are
            sampled by `reply_gate_probability` (+ optional `reply_gate_hourly_limit`)
            before the debounce window and before any decision-model call.
          * Apply debounce wait window (`debounce_seconds`) if not a direct mention; if a newer
            message arrives in the same group during debounce, let the newer message handle the turn.
          * In IDLE state: run small-LLM 5D atmosphere evaluation (`ATMOSPHERE_EVAL_PROMPT`).
        """
        if self.is_private_event(event):
            return True

        if not self.takeover_group_chat:
            return is_direct_mention

        umo = getattr(event, "unified_msg_origin", "") or "default_umo"
        msg_str = (getattr(event, "message_str", "") or "").strip()
        if not msg_str:
            # An @-only ping has no text (AstrBot folds At out of message_str); a
            # direct mention must still be answered instead of returning False.
            return bool(is_direct_mention)

        # In BUSY state, only reply in group chat if directly mentioned or L3 Core User calls
        if current_slot.state == BotState.BUSY:
            return is_direct_mention or user.tier == UserTier.L3_CORE

        # Fast-path: direct mention in IDLE state
        if is_direct_mention:
            return True

        # v2.10: probability / budget gate — only non-@ ordinary group messages reach
        # here (private / BUSY / direct-mention paths returned above). A rejected
        # message is dropped before debounce and before any decision-model call.
        if not self._passes_reply_gate(umo):
            return False

        # Group debounce window for un-mentioned group messages
        seq = self._latest_seq.get(umo, 0) + 1
        self._latest_seq[umo] = seq
        wait_s = min(self.debounce_seconds, 30.0)
        if wait_s > 0:
            await asyncio.sleep(wait_s)
            if self._latest_seq.get(umo, 0) != seq:
                # A newer message arrived in the group during the debounce window
                return False

        # Evaluate group atmosphere via the decision model
        transcript = self.session_buffer.get_window_transcript(
            umo=umo, max_turns=self.atmosphere_transcript_turns
        )
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
