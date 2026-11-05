"""Session sliding window buffer and Context & Token Budget Manager (core/context_budget.py)."""

import asyncio
import math
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Coroutine, Optional

from .character_card import render_persona_prompt
from .memory_manager import MemoryManager, MemoryTier
from .roguelite_deck import DailyScheduleRun, SlotInstance
from .soul_manager import SoulProfile
from .state_machine import BotState
from .user_relation import UserProfile
from ..prompts.templates import (
    BUSY_STATE_INJECTION_PROMPT,
    IDLE_STATE_INJECTION_PROMPT,
    MUTATION_MONOLOGUE_INJECTION_PROMPT,
)

try:
    from astrbot.api import logger
except ImportError:
    import logging
    logger = logging.getLogger("onlyyou.context_budget")


@dataclass
class BufferedMessage:
    """Single message entry in the plugin-managed sliding window."""

    role: str  # "user" | "assistant"
    sender_id: str
    sender_name: str
    user_tier: int
    user_tier_label: str
    content: str
    reply_to: str = ""
    is_proactive: bool = False
    timestamp: float = 0.0

    def format_for_llm(self) -> dict[str, str]:
        """Format into an OpenAI-compatible `{role, content}` context entry."""
        if self.role == "assistant":
            prefix = "[主动发起话题] " if self.is_proactive else ""
            return {"role": "assistant", "content": f"{prefix}{self.content}"}

        reply_tag = f" (回复 {self.reply_to})" if self.reply_to else ""
        header = f"[{self.user_tier_label} {self.sender_name}(ID:{self.sender_id}){reply_tag}]"
        return {"role": "user", "content": f"{header}: {self.content}"}

    def format_for_transcript(self) -> str:
        """Format as a single line for memory extraction / atmosphere reading."""
        if self.role == "assistant":
            return f"[{self.sender_name}(Bot)]: {self.content}"
        reply_tag = f" -> 回复 {self.reply_to}" if self.reply_to else ""
        return f"[{self.user_tier_label} {self.sender_name}(ID:{self.sender_id}){reply_tag}]: {self.content}"


class SessionBuffer:
    """
    Plugin-managed sliding window per UMO (`sliding_window_size=15`).
    Replaces AstrBot's native `req.contexts` so proactive messages, reply topology,
    and user relationship tiers are preserved, and evicted messages are sent to
    `MemoryManager.extract_memories_from_transcript` before destruction.
    """

    def __init__(self, plugin_config: dict[str, Any]):
        self.plugin_config = plugin_config
        self._windows: dict[str, list[BufferedMessage]] = {}
        self._evicted_buffers: dict[str, list[BufferedMessage]] = {}
        self._watched_videos: list[str] = []

    @property
    def cfg(self) -> dict[str, Any]:
        return self.plugin_config.get("memory_and_context_config", {}) or {}

    @property
    def sliding_window_size(self) -> int:
        return max(3, int(self.cfg.get("sliding_window_size", 15)))

    @property
    def extract_every_n_messages(self) -> int:
        return max(3, int(self.cfg.get("extract_every_n_messages", 15)))

    def record_user_message(
        self,
        umo: str,
        user: UserProfile,
        content: str,
        reply_to: str = "",
        on_overflow_callback: Optional[
            Callable[[str, str], Coroutine[Any, Any, Any]]
        ] = None,
    ) -> None:
        """Record an incoming user message into the sliding window."""
        if not umo or not (content or "").strip():
            return
        msg = BufferedMessage(
            role="user",
            sender_id=user.global_uid,
            sender_name=user.display_name,
            user_tier=int(user.tier.value),
            user_tier_label=user.tier.label,
            content=content.strip(),
            reply_to=reply_to,
            is_proactive=False,
            timestamp=time.time(),
        )
        self._append_and_trim(umo, msg, on_overflow_callback)

    def record_assistant_message(
        self,
        umo: str,
        content: str,
        is_proactive: bool = False,
        soul_name: str = "Bot",
        on_overflow_callback: Optional[
            Callable[[str, str], Coroutine[Any, Any, Any]]
        ] = None,
    ) -> None:
        """Record a passive or proactive assistant message into the sliding window."""
        if not umo or not (content or "").strip():
            return
        msg = BufferedMessage(
            role="assistant",
            sender_id="bot",
            sender_name=soul_name,
            user_tier=3,
            user_tier_label="Bot",
            content=content.strip(),
            is_proactive=is_proactive,
            timestamp=time.time(),
        )
        self._append_and_trim(umo, msg, on_overflow_callback)

    def _append_and_trim(
        self,
        umo: str,
        msg: BufferedMessage,
        on_overflow_callback: Optional[
            Callable[[str, str], Coroutine[Any, Any, Any]]
        ] = None,
    ) -> None:
        win = self._windows.setdefault(umo, [])
        win.append(msg)

        max_size = self.sliding_window_size
        evicted = self._evicted_buffers.setdefault(umo, [])
        while len(win) > max_size:
            popped = win.pop(0)
            evicted.append(popped)

        if len(evicted) >= self.extract_every_n_messages:
            batch = list(evicted)
            evicted.clear()
            transcript = "\n".join(m.format_for_transcript() for m in batch)
            if on_overflow_callback is not None:
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(on_overflow_callback(umo, transcript))
                except RuntimeError:
                    pass

    def get_formatted_window(
        self,
        umo: str,
        max_turns: Optional[int] = None,
        exclude_latest_user_text: Optional[str] = None,
    ) -> list[dict[str, str]]:
        """
        Return formatted `{role, content}` messages for `req.contexts`.
        If the last entry in the window is the current incoming message (which AstrBot
        already places in `req.prompt`), we exclude that final entry to avoid duplication.
        """
        win = list(self._windows.get(umo, []))
        if (
            exclude_latest_user_text
            and win
            and win[-1].role == "user"
            and win[-1].content == exclude_latest_user_text.strip()
        ):
            win = win[:-1]

        limit = max_turns or self.sliding_window_size
        sliced = win[-limit:]
        return [m.format_for_llm() for m in sliced]

    def get_window_transcript(self, umo: str, max_turns: int = 12) -> str:
        """Return human-readable transcript of recent messages in `umo`."""
        win = self._windows.get(umo, [])[-max_turns:]
        if not win:
            return "（暂无近期对话）"
        return "\n".join(m.format_for_transcript() for m in win)

    def get_recent_l2_l3_burst_count(self, umo: str, window_seconds: float = 180.0) -> int:
        """Count how many messages from L2/L3 users arrived in `umo` within `window_seconds`."""
        now = time.time()
        win = self._windows.get(umo, [])
        return sum(
            1
            for m in win
            if m.role == "user" and m.user_tier >= 2 and (now - m.timestamp) <= window_seconds
        )

    def record_watched_video(self, summary: str) -> None:
        """Record a recently watched Bilibili/Douyin video for IDLE prompt injection."""
        if summary:
            self._watched_videos.append(summary)
            if len(self._watched_videos) > 5:
                self._watched_videos = self._watched_videos[-5:]

    def get_recent_watched_video_text(self) -> str:
        if not self._watched_videos:
            return "（今天还没刷到特别想分享的视频）"
        return "\n   ".join(f"- {v}" for v in self._watched_videos[-2:])


class ContextBudgetManager:
    """
    简易上下文与预算管理器 (docs/ARCHITECTURE.md Section 4.3):
    0. 用角色卡渲染出的人格 Prompt 替换 AstrBot 原生 `# Persona Instructions` 段
    1. 完全清空平台原生历史 `req.contexts.clear()`，注入插件维护的高信噪比会话滑窗
    2. 按 `[0.40 * 关键词匹配 + 0.35 * 时间新鲜度 + 0.25 * 重要度]` 对候选记忆打分排序
    3. 硬性预算裁剪 (`max_injected_memories=6`, `max_memory_chars_budget=400`)
    4. 注入当前三态控制指令（BUSY 简短抽空回复 / IDLE 奇遇谈资与视频分享）与记忆块
    """

    def __init__(
        self,
        memory_manager: MemoryManager,
        session_buffer: SessionBuffer,
        plugin_config: dict[str, Any],
        soul_manager: Any = None,
    ):
        self.memory_manager = memory_manager
        self.session_buffer = session_buffer
        self.plugin_config = plugin_config
        self.soul_manager = soul_manager

    @property
    def cfg(self) -> dict[str, Any]:
        return self.plugin_config.get("memory_and_context_config", {}) or {}

    @property
    def max_injected_memories(self) -> int:
        return max(1, int(self.cfg.get("max_injected_memories", 6)))

    @property
    def max_memory_chars_budget(self) -> int:
        return max(50, int(self.cfg.get("max_memory_chars_budget", 400)))

    async def _takeover_persona(self, req: Any, soul: SoulProfile) -> None:
        """Replace AstrBot's native Persona block with the rendered character card.

        Three branches (CONTRACTS.md §3.3):
        1. Exact match: any native Persona raw prompt was injected verbatim as
           ``\\n# Persona Instructions\\n\\n{prompt}\\n`` -> replace that whole segment.
        2. Regex fallback: replace from the ``# Persona Instructions`` heading up to
           the next top-level heading (or end of the system prompt).
        3. Not found: prepend the rendered card block to the system prompt.
        """
        if soul is None or not isinstance(getattr(soul, "card", None), dict):
            return

        system_prompt = getattr(req, "system_prompt", None) or ""
        card_block = (
            "\n# Persona Instructions\n\n"
            + render_persona_prompt(soul.card)
            + "\n"
        )

        # ① Exact replacement of a known native Persona segment.
        native_prompts: list[str] = []
        if self.soul_manager is not None:
            try:
                native_prompts = await self.soul_manager.list_native_prompts()
            except Exception as e:  # noqa: BLE001 - must never break the LLM hook
                logger.warning(f"[OnlyYou] list_native_prompts failed: {e}")
                native_prompts = []
        for native_prompt in native_prompts or []:
            prompt_text = str(native_prompt or "").strip()
            if not prompt_text:
                continue
            segment = f"\n# Persona Instructions\n\n{prompt_text}\n"
            if segment in system_prompt:
                req.system_prompt = system_prompt.replace(segment, card_block, 1)
                return

        # ② Regex fallback: consume until the next top-level heading / end.
        new_prompt, replaced_count = re.subn(
            r"\n# Persona Instructions\n\n.*?(?=\n# |\Z)",
            lambda _match: card_block,
            system_prompt,
            count=1,
            flags=re.S,
        )
        if replaced_count:
            req.system_prompt = new_prompt
            return

        # ③ No native Persona block found -> prepend the character card.
        req.system_prompt = card_block + system_prompt

    async def apply_context_and_memory_budget(
        self,
        event: Any,
        req: Any,
        soul: SoulProfile,
        user: UserProfile,
        run: DailyScheduleRun,
        current_slot: SlotInstance,
    ) -> dict[str, Any]:
        """
        Execute full context takeover, memory scoring + hard budget gate, and state prompt injection.
        Returns diagnostic metadata (`recalled_ids`, `injected_chars`, `window_count`).
        """
        umo = getattr(event, "unified_msg_origin", "") or "default_umo"
        query_text = getattr(event, "message_str", "") or getattr(req, "prompt", "") or ""

        # =====================================================================
        # 第零步：角色卡接管 AstrBot 原生 Persona 段（必须先于三态/记忆注入）
        # =====================================================================
        try:
            await self._takeover_persona(req, soul)
        except Exception as e:  # noqa: BLE001 - never break the LLM request hook
            logger.warning(f"[OnlyYou] Persona takeover failed: {e}")

        # =====================================================================
        # 第一步：完全清空平台原生历史 (req.contexts.clear())，由插件全权接管上下文！
        # =====================================================================
        if hasattr(req, "contexts") and isinstance(req.contexts, list):
            req.contexts.clear()
            session_window_msgs = self.session_buffer.get_formatted_window(
                umo=umo,
                max_turns=self.session_buffer.sliding_window_size,
                exclude_latest_user_text=query_text,
            )
            req.contexts.extend(session_window_msgs)
        else:
            session_window_msgs = []

        # Annotate current user prompt with relationship tier & sender identity
        if hasattr(req, "prompt") and req.prompt and not req.prompt.startswith("["):
            req.prompt = f"[{user.tier.label} {user.display_name}(ID:{user.global_uid})]: {req.prompt}"

        # =====================================================================
        # 第二步：按 [关键词匹配 + 时间新鲜度 + 重要度] 检索候选记忆
        # =====================================================================
        candidates = await self.memory_manager.retrieve_candidates(
            soul_id=soul.soul_id, user_id=user.global_uid
        )

        now = time.time()
        scored_memories = []
        for mem in candidates:
            # 1. 关键词命中分 (0.0 ~ 1.0)
            kw_list = [k.strip() for k in mem.keywords.split(",") if k.strip()]
            kw_hits = sum(1 for k in kw_list if k in query_text)
            # Also check substring overlap with content if no keyword hit directly
            if kw_hits == 0 and any( tok in query_text for tok in kw_list ):
                kw_hits = 1
            kw_score = min(1.0, kw_hits * 0.5) if kw_list else 0.0

            # 2. 时间新鲜度分 Recency (越近越容易被想起，指数衰减)
            days_old = max(0.0, (now - mem.created_at) / 86400.0)
            recency_score = math.exp(-0.15 * days_old)

            # 3. 重要度分 Importance (0.1 ~ 1.0)
            imp_score = mem.importance / 10.0

            # 综合得分
            total_score = 0.40 * kw_score + 0.35 * recency_score + 0.25 * imp_score
            scored_memories.append((total_score, mem))

        scored_memories.sort(key=lambda x: x[0], reverse=True)

        # =====================================================================
        # 第三步：硬性预算裁剪 (Hard Budget Gate：限制最大条数与最大字符数)
        # =====================================================================
        selected_lines: list[str] = []
        current_chars = 0
        recalled_ids: list[int] = []

        for score, mem in scored_memories:
            if len(selected_lines) >= self.max_injected_memories:
                break
            if current_chars + len(mem.content) > self.max_memory_chars_budget:
                break

            if mem.memory_tier == MemoryTier.CORE:
                tag = "核心记忆"
            elif mem.memory_tier == MemoryTier.RECENT:
                tag = "近期记忆"
            else:
                tag = "瞬时记忆"

            selected_lines.append(f"- [{tag}] {mem.content}")
            current_chars += len(mem.content)
            recalled_ids.append(mem.id)

        if recalled_ids:
            await self.memory_manager.reinforce_memories(recalled_ids)

        # =====================================================================
        # 第四步：注入当前三态行为约束 + 变卦内心独白 + 精简记忆块到 System Prompt
        # =====================================================================
        additions: list[str] = []

        if current_slot.state == BotState.BUSY:
            additions.append(
                BUSY_STATE_INJECTION_PROMPT.format(
                    current_slot_title=current_slot.title,
                    user_tier_name=user.tier.label,
                    sender_name=user.display_name,
                )
            )
        elif current_slot.state == BotState.IDLE:
            unused_encounters = [e for e in run.encounters if not e.used]
            if unused_encounters:
                enc_text = "\n   ".join(f"- {e.content}" for e in unused_encounters)
            else:
                enc_text = "（今日奇遇话题已聊完）"

            additions.append(
                IDLE_STATE_INJECTION_PROMPT.format(
                    current_slot_title=current_slot.title,
                    unused_encounter_cards_text=enc_text,
                    recent_watched_video_text=self.session_buffer.get_recent_watched_video_text(),
                )
            )

        if current_slot.mutated and current_slot.mutation_monologue:
            additions.append(
                MUTATION_MONOLOGUE_INJECTION_PROMPT.format(
                    mutation_monologue=current_slot.mutation_monologue
                )
            )

        if selected_lines:
            additions.append(
                "[脑海中的相关记忆 (精简注入)]:\n" + "\n".join(selected_lines)
            )

        sys_prompt = getattr(req, "system_prompt", "") or ""
        if additions:
            req.system_prompt = (sys_prompt + "\n\n" + "\n\n".join(additions)).strip()

        return {
            "window_count": len(session_window_msgs),
            "recalled_ids": recalled_ids,
            "injected_chars": current_chars,
        }
