"""Session sliding window buffer and Context & Token Budget Manager (core/context_budget.py)."""

import asyncio
import inspect
import math
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Coroutine, Optional

from .character_card import (
    DEFAULT_PERSONA_PROMPT_MAX_CHARS,
    PERSONA_PROMPT_BUDGET_MAX,
    PERSONA_PROMPT_BUDGET_MIN,
    render_persona_prompt,
)
from .memory_manager import (
    MemoryManager,
    MemoryTier,
    cosine_similarity,
    normalize_memory_type,
    retrieval_similarity,
    MEMORY_TYPE_LABELS,
)
from .roguelite_deck import DailyScheduleRun, SlotInstance
from .soul_manager import SoulProfile
from .state_machine import ActionType, BotState
from .time_utils import (
    current_datetime_text,
    describe_time_anchor,
    describe_time_span,
    format_transcript_stamp,
)
from .user_relation import UserProfile
from ..prompts.templates import (
    BUSY_STATE_INJECTION_PROMPT,
    IDLE_STATE_INJECTION_PROMPT,
    MEMORY_TIME_CONTEXT_PROMPT,
    MUTATION_MONOLOGUE_INJECTION_PROMPT,
    USER_RELATION_CONTEXT_PROMPT,
)

try:
    from astrbot.api import logger
except ImportError:
    import logging
    logger = logging.getLogger("onlyyou.context_budget")

# ==============================================================================
# v2.12: RAG 混合召回排序权重与最低相关性门闸（contract §2.4）
# ==============================================================================
#: 综合得分 = 0.55 * 语义相关 + 0.25 * 时间新鲜度 + 0.20 * 重要度
SEMANTIC_WEIGHT = 0.55
RECENCY_WEIGHT = 0.25
IMPORTANCE_WEIGHT = 0.20
#: 低于相关性门槛的记忆绝不注入（0 分硬凑是旧版的病根）。
MIN_RECALL_SCORE = 0.38
MIN_SEMANTIC_SCORE = 0.25
#: 刚提炼且内容仍在当前滑窗里的记忆，避免与滑窗重复注入。
RECENT_DUPLICATE_WINDOW_SECONDS = 10 * 60
#: 上下文感知 Query 取该用户最近几条发言（解决“那它呢/然后呢”无关键词）。
QUERY_CONTEXT_USER_MESSAGES = 2


def _clean_text(value: Any) -> str:
    """Trim any scalar into a compact string (``None`` -> ``""``)."""
    if value is None:
        return ""
    return str(value).strip()


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
    # Monotonic per-SessionBuffer sequence number (used by the unread backlog to
    # avoid same-second timestamp collisions).
    seq: int = 0
    # Whether this message arrived in a private chat (used by the unread backlog).
    is_private: bool = False

    def format_for_llm(self) -> dict[str, str]:
        """Format into an OpenAI-compatible `{role, content}` context entry."""
        if self.role == "assistant":
            prefix = "[主动发起话题] " if self.is_proactive else ""
            return {"role": "assistant", "content": f"{prefix}{self.content}"}

        reply_tag = f" (回复 {self.reply_to})" if self.reply_to else ""
        header = f"[{self.user_tier_label} {self.sender_name}(ID:{self.sender_id}){reply_tag}]"
        return {"role": "user", "content": f"{header}: {self.content}"}

    def format_for_transcript(self, with_time: bool = False) -> str:
        """Format as a single line for memory extraction / atmosphere reading.

        v2.10: ``with_time=True`` prefixes the message's real local send time
        (``[2026-11-15 21:04]``) so a cold window spanning several days can be
        located by the extraction model. The default stays ``False`` to keep
        every pre-v2.10 prompt (atmosphere / unread batch) byte-identical.
        """
        stamp = f"[{format_transcript_stamp(self.timestamp)}] " if with_time else ""
        if self.role == "assistant":
            return f"{stamp}[{self.sender_name}(Bot)]: {self.content}"
        reply_tag = f" -> 回复 {self.reply_to}" if self.reply_to else ""
        return (
            f"{stamp}[{self.user_tier_label} {self.sender_name}"
            f"(ID:{self.sender_id}){reply_tag}]: {self.content}"
        )


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
        # ---- unread backlog (independent of the 15-message sliding window) ----
        # "Unread" == messages the bot could not handle because the three-state
        # gate blocked them (RESTING: everything / BUSY: L1). Messages that passed
        # the gate but were judged "ignore" by the atmosphere model are read.
        self._unread: dict[str, list[BufferedMessage]] = {}
        self._unread_kind: dict[str, bool] = {}
        self._seq_counter: int = 0
        # v2.12: strong references to the background extraction tasks (a bare
        # ``loop.create_task`` result may be garbage-collected) + one retry batch
        # per session kept when the extraction LLM call / JSON parse failed.
        self._overflow_tasks: set = set()
        self._retry_batches: dict[str, list[BufferedMessage]] = {}

    @property
    def cfg(self) -> dict[str, Any]:
        return self.plugin_config.get("memory_and_context_config", {}) or {}

    @property
    def roguelite_cfg(self) -> dict[str, Any]:
        return self.plugin_config.get("roguelite_schedule_config", {}) or {}

    @property
    def unread_group_ttl_seconds(self) -> float:
        """Unread group messages older than this are dropped (default 2h)."""
        try:
            minutes = int(self.roguelite_cfg.get("unread_group_ttl_minutes", 120))
        except (TypeError, ValueError):
            minutes = 120
        return max(60, minutes) * 60.0

    @property
    def unread_private_ttl_seconds(self) -> float:
        """Unread private messages older than this are dropped (default 12h)."""
        try:
            minutes = int(self.roguelite_cfg.get("unread_private_ttl_minutes", 720))
        except (TypeError, ValueError):
            minutes = 720
        return max(60, minutes) * 60.0

    @property
    def unread_max_per_chat(self) -> int:
        """Per-session unread backlog cap (oldest dropped when exceeded)."""
        try:
            count = int(self.roguelite_cfg.get("unread_max_per_chat", 20))
        except (TypeError, ValueError):
            count = 20
        return max(1, count)

    @property
    def unread_batch_max_chars(self) -> int:
        """Max characters of one unread-batch transcript (`unread_batch_max_chars`)."""
        try:
            value = int(self.roguelite_cfg.get("unread_batch_max_chars", 800))
        except (TypeError, ValueError):
            value = 800
        return max(200, min(6000, value))

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
    ) -> Optional[BufferedMessage]:
        """Record an incoming user message into the sliding window.

        Returns the created :class:`BufferedMessage` so callers (e.g. the gate)
        can mark that exact message as unread later.
        """
        if not umo or not (content or "").strip():
            return None
        msg = BufferedMessage(
            role="user",
            sender_id=user.global_uid,
            # v2.12: use the stable canonical name (nickname -> display name) so one
            # user cannot appear under several nicknames inside the transcript.
            sender_name=str(
                getattr(user, "canonical_name", "") or user.display_name
            ),
            user_tier=int(user.tier.value),
            user_tier_label=user.tier.label,
            content=content.strip(),
            reply_to=reply_to,
            is_proactive=False,
            timestamp=time.time(),
            seq=self._next_seq(),
        )
        self._append_and_trim(umo, msg, on_overflow_callback)
        return msg

    def _next_seq(self) -> int:
        self._seq_counter += 1
        return self._seq_counter

    # ==================================================================
    # Unread backlog (gate-blocked messages)
    # ==================================================================
    def mark_unread(
        self, umo: str, message: Optional[BufferedMessage], is_private: bool = False
    ) -> None:
        """Add a gate-blocked user message to the unread backlog of ``umo``."""
        if not umo or message is None or message.role != "user":
            return
        if not message.seq:
            message.seq = self._next_seq()
        message.is_private = bool(is_private)
        self._unread_kind[umo] = bool(is_private)
        bucket = self._unread.setdefault(umo, [])
        if any(item.seq == message.seq for item in bucket):
            return
        bucket.append(message)
        cap = self.unread_max_per_chat
        if len(bucket) > cap:
            del bucket[: len(bucket) - cap]

    def _prune_unread(self, now: Optional[float] = None) -> None:
        """Drop expired unread messages (TTL is per group / private)."""
        now = time.time() if now is None else now
        for umo in list(self._unread.keys()):
            is_private = self._unread_kind.get(umo, False)
            ttl = (
                self.unread_private_ttl_seconds
                if is_private
                else self.unread_group_ttl_seconds
            )
            fresh = [
                message
                for message in self._unread.get(umo, [])
                if (now - float(message.timestamp or 0.0)) <= ttl
            ]
            if fresh:
                self._unread[umo] = fresh
            else:
                self._unread.pop(umo, None)
                self._unread_kind.pop(umo, None)

    def get_unread_batch(
        self, umo: str, limit: Optional[int] = None
    ) -> list[BufferedMessage]:
        """Return a copy of the unread backlog for ``umo`` (oldest first)."""
        self._prune_unread()
        batch = list(self._unread.get(umo, []))
        if limit is not None and limit > 0:
            batch = batch[-limit:]
        return batch

    def get_unread_total(self) -> int:
        self._prune_unread()
        return sum(len(batch) for batch in self._unread.values())

    def get_unread_count(self, is_private: Optional[bool] = None) -> int:
        """Count unread messages, optionally filtered by group/private kind."""
        self._prune_unread()
        if is_private is None:
            return sum(len(batch) for batch in self._unread.values())
        return sum(
            len(batch)
            for umo, batch in self._unread.items()
            if bool(self._unread_kind.get(umo, False)) == bool(is_private)
        )

    def list_unread_chats(
        self, *, private: bool, limit: int = 20
    ) -> list[tuple[str, list[BufferedMessage]]]:
        """List sessions of one kind with a non-empty unread backlog.

        Sorted by unread count (desc) then latest message (desc), so the busiest /
        freshest conversations are consumed first.
        """
        self._prune_unread()
        now = time.time()
        result: list[tuple[str, list[BufferedMessage]]] = []
        for umo, batch in self._unread.items():
            if bool(self._unread_kind.get(umo, False)) != bool(private):
                continue
            if not batch:
                continue
            result.append((umo, list(batch)))
        result.sort(
            key=lambda item: (
                -len(item[1]),
                -max(float(m.timestamp or 0.0) for m in item[1]),
            )
        )
        _ = now
        return result[: max(1, int(limit))]

    def ack_unread(self, umo: str, seqs: Optional[list[int]] = None) -> int:
        """Mark unread messages of ``umo`` as consumed (remove them from the backlog)."""
        bucket = self._unread.get(umo)
        if not bucket:
            return 0
        if seqs is None:
            removed = len(bucket)
            self._unread.pop(umo, None)
            self._unread_kind.pop(umo, None)
            return removed
        seq_set = {int(seq) for seq in seqs}
        remaining = [message for message in bucket if message.seq not in seq_set]
        removed = len(bucket) - len(remaining)
        if remaining:
            self._unread[umo] = remaining
        else:
            self._unread.pop(umo, None)
            self._unread_kind.pop(umo, None)
        return removed

    def clear_unread(self, umo: Optional[str] = None) -> int:
        """Clear the unread backlog for one session (or all sessions)."""
        if umo:
            return self.ack_unread(umo)
        removed = sum(len(batch) for batch in self._unread.values())
        self._unread.clear()
        self._unread_kind.clear()
        return removed

    def list_window_umos(self) -> list[str]:
        """Return every UMO that currently owns an in-memory sliding window."""
        try:
            return [umo for umo in self._windows.keys() if umo]
        except Exception:
            return []

    def clear_session_memory(self, umo: str) -> dict[str, int]:
        """Drop one session's sliding window / evicted buffer / unread backlog.

        Returns ``{"window": n, "evicted": n, "unread": n}`` message counts.
        """
        clean = (umo or "").strip()
        stats = {"window": 0, "evicted": 0, "unread": 0}
        if not clean:
            return stats
        stats["window"] = len(self._windows.pop(clean, None) or [])
        stats["evicted"] = len(self._evicted_buffers.pop(clean, None) or [])
        stats["unread"] = self.ack_unread(clean)
        return stats

    def clear_platform_memory(self, platform_id: str) -> dict[str, int]:
        """Drop **in-memory** chat state for every session of one bot platform.

        Called when a Soul unbinds a bot instance (``platform_id``): all UMOs whose
        ``<platform_id>:`` prefix matches lose their sliding window, evicted buffer
        and unread backlog.  Durable SQLite data (memories / users / video history)
        is intentionally preserved.
        """
        clean = (platform_id or "").strip()
        stats = {"windows": 0, "evicted": 0, "unread": 0, "messages": 0}
        if not clean:
            return stats
        prefix = f"{clean}:"
        for container_key in ("_windows", "_evicted_buffers", "_unread"):
            container = getattr(self, container_key, None)
            if not isinstance(container, dict):
                continue
            doomed = [umo for umo in list(container.keys()) if str(umo).startswith(prefix)]
            for umo in doomed:
                removed = container.pop(umo, None) or []
                if container_key == "_windows":
                    stats["windows"] += 1
                    stats["messages"] += len(removed)
                elif container_key == "_evicted_buffers":
                    stats["evicted"] += 1
                    stats["messages"] += len(removed)
                else:
                    stats["unread"] += 1
                    stats["messages"] += len(removed)
        for umo in [umo for umo in list(self._unread_kind.keys()) if str(umo).startswith(prefix)]:
            self._unread_kind.pop(umo, None)
        return stats

    def format_unread_batch(
        self, batch: list[BufferedMessage], max_chars: Optional[int] = None
    ) -> str:
        """Render an unread batch as a compact transcript snippet."""
        budget = int(max_chars) if max_chars else self.unread_batch_max_chars
        lines: list[str] = []
        used = 0
        for message in batch:
            line = message.format_for_transcript()
            if used + len(line) > budget:
                lines.append("……（更早的未读消息已省略）")
                break
            lines.append(line)
            used += len(line)
        return "\n".join(lines) if lines else "（无未读消息）"

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
            # v2.12: prepend a previously failed batch so its messages are retried
            # (at most one retained batch per session).
            retry_batch = self._retry_batches.pop(umo, None)
            if retry_batch:
                batch = list(retry_batch) + batch
            transcript = self.format_extraction_transcript(batch)
            if on_overflow_callback is not None:
                try:
                    loop = asyncio.get_running_loop()
                    task = loop.create_task(
                        self._run_overflow_extraction(
                            on_overflow_callback, umo, batch, transcript
                        )
                    )
                    self._overflow_tasks.add(task)
                    task.add_done_callback(self._overflow_tasks.discard)
                except RuntimeError:
                    pass

    async def _run_overflow_extraction(
        self,
        callback: Callable[[str, str], Coroutine[Any, Any, Any]],
        umo: str,
        batch: list[BufferedMessage],
        transcript: str,
    ) -> None:
        """Run one extraction callback; retain the batch once when it fails."""
        try:
            result = await callback(umo, transcript)
            failed = result is False
        except Exception as exc:  # noqa: BLE001 - a background task must not raise
            logger.warning(f"[OnlyYou] memory extraction task failed: {exc}")
            failed = True
        if failed and batch:
            # Keep only the latest failed batch (bounded retry memory).
            self._retry_batches[umo] = list(batch)

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

    # ------------------------------------------------------------------
    # v2.10: time-stamped transcript handed to the memory-extraction model
    # ------------------------------------------------------------------
    def format_extraction_transcript(self, messages: list[BufferedMessage]) -> str:
        """Render evicted/window messages with per-message real send times.

        Layout (TODO T1 acceptance #2: the extraction model must be able to tell
        which day the first / last message belongs to and what "today" is)::

            【窗口时间范围】2026-11-10 09:12 ~ 2026-11-15 21:04（跨度 6 天），共 15 条消息
            [2026-11-10 09:12] [L1 阿一(ID:u-1)]: 这周末去考试吗
            ...

        The current real time is injected separately by
        ``MemoryManager.extract_memories_from_transcript`` via
        ``MEMORY_EXTRACT_AND_GRADE_PROMPT.current_datetime``.
        """
        batch = [m for m in (messages or []) if m is not None]
        if not batch:
            return "（暂无近期对话）"
        stamps = [float(getattr(m, "timestamp", 0.0) or 0.0) for m in batch]
        span = describe_time_span(min(stamps), max(stamps))
        lines = [f"【窗口时间范围】{span}，共 {len(batch)} 条消息"]
        lines.extend(m.format_for_transcript(with_time=True) for m in batch)
        return "\n".join(lines)

    def get_extraction_transcript(self, umo: str, max_turns: int = 15) -> str:
        """Time-stamped transcript of ``umo``'s last ``max_turns`` messages.

        Used by the sliding-window overflow callback and by the manual
        ``/onlyyou memory extract`` / Pages「记忆 → 提炼」entries.
        """
        win = self._windows.get(umo, [])[-max_turns:]
        return self.format_extraction_transcript(win)

    def get_recent_user_texts(
        self, umo: str, sender_id: str = "", limit: int = 2
    ) -> list[str]:
        """Return the last ``limit`` user message texts of ``umo`` (oldest first).

        v2.12: used to build the context-aware recall query
        (``当前消息 + 该用户最近 2 条发言``) so follow-up questions such as
        "那它呢 / 然后呢 / 你还记得吗" still retrieve the right memories.
        """
        try:
            count = max(0, int(limit))
        except (TypeError, ValueError):
            count = 2
        if count <= 0:
            return []
        wanted = str(sender_id or "").strip()
        texts: list[str] = []
        for message in reversed(self._windows.get(umo, []) or []):
            if message.role != "user":
                continue
            if wanted and str(message.sender_id or "").strip() != wanted:
                continue
            content = str(message.content or "").strip()
            if content:
                texts.append(content)
            if len(texts) >= count:
                break
        texts.reverse()
        return texts

    def get_window_contents(self, umo: str) -> list[str]:
        """All non-empty message contents of ``umo``'s window (for dedupe checks)."""
        return [
            str(message.content or "").strip()
            for message in self._windows.get(umo, []) or []
            if str(message.content or "").strip()
        ]

    def get_window_sender_ids(self, umo: str) -> list[str]:
        """Return distinct user ``sender_id`` values of ``umo``'s window (newest first).

        Used by the v2.6 user-relation context block to resolve the *other* active
        speakers of the current group conversation into their configured
        nickname / attitude.
        """
        ids: list[str] = []
        for message in reversed(self._windows.get(umo, []) or []):
            if message.role != "user":
                continue
            sender_id = str(message.sender_id or "").strip()
            if sender_id and sender_id not in ids:
                ids.append(sender_id)
        return ids

    def get_recent_l2_l3_burst_count(self, umo: str, window_seconds: float = 180.0) -> int:
        """Count how many messages from L2/L3 users arrived in `umo` within `window_seconds`."""
        now = time.time()
        win = self._windows.get(umo, [])
        return sum(
            1
            for m in win
            if m.role == "user" and m.user_tier >= 2 and (now - m.timestamp) <= window_seconds
        )

    def get_recent_user_message_ts(
        self, umo: str, window_seconds: float = 7200.0
    ) -> float:
        """Return the timestamp of the latest user message in `umo`.

        Returns ``0.0`` when the window has no user message or the latest one is
        older than ``window_seconds`` (default 2h). Used by the IDLE_ROAM
        ``GROUP_LURK`` branch to decide whether a known group is worth lurking.
        """
        if not umo:
            return 0.0
        now = time.time()
        win = self._windows.get(umo, [])
        for message in reversed(win):
            if message.role != "user":
                continue
            if (now - float(message.timestamp or 0.0)) > window_seconds:
                return 0.0
            return float(message.timestamp or 0.0)
        return 0.0

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
    简易上下文与预算管理器 (docs/ARCHITECTURE.md Section 6.2):
    0. 用角色卡渲染出的人格 Prompt 替换 AstrBot 原生 `# Persona Instructions` 段
    1. 完全清空平台原生历史 `req.contexts.clear()`，注入插件维护的高信噪比会话滑窗
    2. 按 `[0.40 * 关键词匹配 + 0.35 * 时间新鲜度 + 0.25 * 重要度]` 对候选记忆打分排序
    3. 硬性预算裁剪 (`max_injected_memories=6`, `max_memory_chars_budget=400`)
    4. 注入当前行动卡控制指令（BUSY_TASK 实时进度 / IDLE 今日繁忙成果与视频分享）与记忆块
    """

    def __init__(
        self,
        memory_manager: MemoryManager,
        session_buffer: SessionBuffer,
        plugin_config: dict[str, Any],
        soul_manager: Any = None,
        user_relation: Any = None,
    ):
        self.memory_manager = memory_manager
        self.session_buffer = session_buffer
        self.plugin_config = plugin_config
        self.soul_manager = soul_manager
        # v2.6 Phase 2: optional relationship manager used to look up the current
        # speaker's per-user attitude / nickname and the other active speakers of
        # the current window. ``None`` keeps the legacy constructor working.
        self.user_relation = user_relation

    @property
    def cfg(self) -> dict[str, Any]:
        return self.plugin_config.get("memory_and_context_config", {}) or {}

    def _cfg_float(self, key: str, default: float, low: float, high: float) -> float:
        try:
            value = float(self.cfg.get(key, default))
        except (TypeError, ValueError):
            value = default
        return max(low, min(high, value))

    def _cfg_int(self, key: str, default: int, low: int, high: int) -> int:
        try:
            value = int(self.cfg.get(key, default))
        except (TypeError, ValueError):
            value = default
        return max(low, min(high, value))

    @property
    def max_injected_memories(self) -> int:
        return max(1, int(self.cfg.get("max_injected_memories", 6)))

    @property
    def recall_semantic_weight(self) -> float:
        return self._cfg_float("recall_semantic_weight", SEMANTIC_WEIGHT, 0.0, 1.0)

    @property
    def recall_recency_weight(self) -> float:
        return self._cfg_float("recall_recency_weight", RECENCY_WEIGHT, 0.0, 1.0)

    @property
    def recall_importance_weight(self) -> float:
        return self._cfg_float("recall_importance_weight", IMPORTANCE_WEIGHT, 0.0, 1.0)

    @property
    def recent_duplicate_window_seconds(self) -> int:
        return (
            self._cfg_int("recent_duplicate_window_minutes", 10, 0, 10080) * 60
        )

    @property
    def recall_query_context_turns(self) -> int:
        return self._cfg_int("recall_query_context_turns", QUERY_CONTEXT_USER_MESSAGES, 0, 50)

    @property
    def max_memory_chars_budget(self) -> int:
        return max(50, int(self.cfg.get("max_memory_chars_budget", 400)))

    @property
    def persona_prompt_max_chars(self) -> int:
        """Rendered persona prompt budget (Pages「设置 → 记忆与上下文」, default 4000)."""
        return self._cfg_int(
            "persona_prompt_max_chars",
            DEFAULT_PERSONA_PROMPT_MAX_CHARS,
            PERSONA_PROMPT_BUDGET_MIN,
            PERSONA_PROMPT_BUDGET_MAX,
        )

    @property
    def max_recall_candidates(self) -> int:
        """Coarse-recall pool size entering the rerank stage (default 24)."""
        try:
            return max(4, int(self.cfg.get("max_recall_candidates", 24)))
        except (TypeError, ValueError):
            return 24

    @property
    def min_recall_score(self) -> float:
        """Minimum fused relevance for a memory to be injected (default 0.38)."""
        try:
            return max(
                0.0, min(1.0, float(self.cfg.get("min_recall_score", MIN_RECALL_SCORE)))
            )
        except (TypeError, ValueError):
            return MIN_RECALL_SCORE

    @property
    def min_semantic_score(self) -> float:
        """Minimum semantic relevance for a memory to be injected (default 0.25)."""
        try:
            return max(
                0.0,
                min(1.0, float(self.cfg.get("min_semantic_score", MIN_SEMANTIC_SCORE))),
            )
        except (TypeError, ValueError):
            return MIN_SEMANTIC_SCORE

    # ------------------------------------------------------------------
    # v2.12: RAG recall helpers (context-aware query / hybrid pool / rerank)
    # ------------------------------------------------------------------
    def _build_recall_query(
        self, umo: str, user: UserProfile, query_text: str
    ) -> str:
        """Build the context-aware query: current message + recent same-user turns."""
        current = str(query_text or "").strip()
        recent: list[str] = []
        getter = getattr(self.session_buffer, "get_recent_user_texts", None)
        if callable(getter):
            try:
                recent = getter(
                    umo,
                    sender_id=str(getattr(user, "global_uid", "") or ""),
                    limit=self.recall_query_context_turns,
                )
            except Exception as exc:  # noqa: BLE001 - query enrichment is best effort
                logger.debug(f"[OnlyYou] build recall query failed: {exc}")
                recent = []
        parts = [text for text in recent if text and text != current]
        if current:
            parts.append(current)
        return " ".join(parts).strip() or current

    async def _embed_query(self, query_text: str, soul: SoulProfile) -> list[float]:
        """Embed the recall query (``[]`` when the embedding role is off)."""
        hub = getattr(self.memory_manager, "model_hub", None)
        getter = getattr(hub, "get_embedding", None) if hub is not None else None
        if not callable(getter):
            return []
        soul_id = str(getattr(soul, "soul_id", "") or "")
        try:
            vector = getter(query_text, soul_id=soul_id) if soul_id else getter(query_text)
            if inspect.isawaitable(vector):
                vector = await vector
        except TypeError:
            try:
                vector = getter(query_text)
                if inspect.isawaitable(vector):
                    vector = await vector
            except Exception:  # noqa: BLE001 - degrade to lexical
                return []
        except Exception as exc:  # noqa: BLE001 - degrade to lexical
            logger.debug(f"[OnlyYou] query embedding failed: {exc}")
            return []
        if not isinstance(vector, (list, tuple)):
            return []
        return [float(value) for value in vector if isinstance(value, (int, float))]

    async def _retrieve_candidates(
        self,
        soul: SoulProfile,
        user: UserProfile,
        query_text: str,
        query_vector: list[float],
    ) -> list[Any]:
        """Three-route hybrid pool when available, legacy SQL list otherwise."""
        manager = self.memory_manager
        retriever = getattr(manager, "retrieve_hybrid_candidates", None)
        if callable(retriever):
            try:
                return await retriever(
                    soul_id=soul.soul_id,
                    user_id=user.global_uid,
                    query_text=query_text,
                    query_embedding=query_vector or None,
                    limit=self.max_recall_candidates,
                )
            except Exception as exc:  # noqa: BLE001 - fall through to the legacy pool
                logger.warning(f"[OnlyYou] hybrid recall failed: {exc}")
        fallback = getattr(manager, "retrieve_candidates", None)
        if not callable(fallback):
            return []
        try:
            return await fallback(soul_id=soul.soul_id, user_id=user.global_uid)
        except Exception as exc:  # noqa: BLE001 - memory is best effort
            logger.warning(f"[OnlyYou] retrieve_candidates failed: {exc}")
            return []

    async def _semantic_scores(
        self,
        query_text: str,
        candidates: list[Any],
        query_vector: list[float],
        soul: SoulProfile,
    ) -> dict[int, float]:
        """Semantic relevance of every candidate (rerank > dense > lexical)."""
        scores: dict[int, float] = {}
        if not candidates:
            return scores
        hub = getattr(self.memory_manager, "model_hub", None)
        rerank = getattr(hub, "rerank_documents", None) if hub is not None else None
        if callable(rerank):
            documents = [
                f"{getattr(item, 'content', '')} {getattr(item, 'keywords', '')}".strip()
                for item in candidates
            ]
            rows: list[Any] = []
            soul_id = str(getattr(soul, "soul_id", "") or "")
            try:
                kwargs: dict[str, Any] = {"top_n": len(documents)}
                if soul_id:
                    kwargs["soul_id"] = soul_id
                rows = await rerank(query_text, documents, **kwargs)
            except TypeError:
                rows = []
            except Exception as exc:  # noqa: BLE001 - degrade to dense / lexical
                logger.debug(f"[OnlyYou] rerank failed: {exc}")
                rows = []
            for row in rows or []:
                if isinstance(row, dict):
                    raw_index = row.get("index")
                    raw_score = row.get("score")
                else:
                    raw_index = getattr(row, "index", None)
                    raw_score = getattr(
                        row, "score", getattr(row, "relevance_score", None)
                    )
                try:
                    index = int(raw_index)
                    score = float(raw_score)
                except (TypeError, ValueError):
                    continue
                if index < 0 or index >= len(candidates):
                    continue
                scores[candidates[index].id] = max(0.0, min(1.0, score))
            if scores:
                # A provider that only returns its top-N must not silently gate out
                # the remaining candidates: fill them with the local fallback.
                for mem in candidates:
                    if mem.id in scores:
                        continue
                    scores[mem.id] = self._local_semantic_score(
                        query_text, mem, query_vector
                    )
                return scores

        for mem in candidates:
            scores[mem.id] = self._local_semantic_score(query_text, mem, query_vector)
        return scores

    @staticmethod
    def _local_semantic_score(
        query_text: str, mem: Any, query_vector: list[float]
    ) -> float:
        """Dense cosine when both vectors exist, else keyword-aware lexical score."""
        embedding = getattr(mem, "embedding", None) or []
        if query_vector and embedding:
            return max(0.0, min(1.0, cosine_similarity(query_vector, embedding)))
        return max(
            0.0,
            min(
                1.0,
                retrieval_similarity(
                    query_text,
                    getattr(mem, "content", ""),
                    getattr(mem, "keywords", ""),
                ),
            ),
        )

    def _is_recent_window_duplicate(self, umo: str, mem: Any, now: float) -> bool:
        """Whether a just-extracted memory is still fully present in the window."""
        try:
            created_at = int(getattr(mem, "created_at", 0) or 0)
        except (TypeError, ValueError):
            created_at = 0
        if not created_at or (now - created_at) > self.recent_duplicate_window_seconds:
            return False
        content = str(getattr(mem, "content", "") or "").strip()
        if not content:
            return False
        getter = getattr(self.session_buffer, "get_window_contents", None)
        contents = getter(umo) if callable(getter) else []
        return any(content in text for text in contents or [])

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
            + render_persona_prompt(
                soul.card, max_chars=self.persona_prompt_max_chars
            )
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

    @staticmethod
    def _render_short_term_talk_text(run: DailyScheduleRun) -> str:
        """Render today's settled BUSY_TASK completion results for the IDLE prompt.

        Most recent 3 results, one per line as ``- {title}：{completion_result}``;
        falls back to ``（今天还没忙完什么事）`` when nothing has been settled yet.
        """
        lines: list[str] = []
        try:
            slots = list(getattr(run, "slots", None) or [])
            for slot in reversed(slots):
                action = ActionType.coerce(
                    getattr(slot, "action_type", None), None
                )
                if action != ActionType.BUSY_TASK:
                    continue
                if not getattr(slot, "busy_settled", False):
                    continue
                completion = str(getattr(slot, "completion_result", "") or "").strip()
                if not completion:
                    continue
                title = str(getattr(slot, "title", "") or "").strip() or "繁忙事项"
                lines.append(f"- {title}：{completion}")
                if len(lines) >= 3:
                    break
        except Exception:  # noqa: BLE001 - a malformed run must never break the LLM hook
            return "（今天还没忙完什么事）"
        if not lines:
            return "（今天还没忙完什么事）"
        return "\n   ".join(lines)

    async def _render_window_peers_block(
        self, umo: str, user: UserProfile, soul_id: str = ""
    ) -> str:
        """Render the other configured active speakers of the current window.

        v2.6 (contract §2.3): only speakers of ``umo``'s sliding window that have a
        per-user ``nickname`` or ``attitude`` configured are listed (max 4, current
        speaker excluded). Returns ``""`` when there is nothing to show; every
        failure degrades to an empty block instead of raising.
        """
        if self.user_relation is None:
            return ""
        try:
            sender_ids = self.session_buffer.get_window_sender_ids(umo)
        except Exception:  # noqa: BLE001 - best-effort only
            sender_ids = []
        if not sender_ids:
            return ""
        current_uid = _clean_text(getattr(user, "global_uid", ""))
        # v2.9: peer nickname/attitude must come from the current Soul's relations.
        # Transitional fallback: if this Soul has no relation rows yet (pre-migration
        # / unit tests), fall back to the legacy global view instead of dropping peers.
        profiles = []
        if soul_id:
            try:
                profiles = await self.user_relation.list_users(soul_id=soul_id)
            except Exception:  # noqa: BLE001 - never break the LLM hook
                profiles = []
        if not profiles:
            profiles = await self.user_relation.list_users()
        by_uid: dict[str, Any] = {}
        for profile in profiles or []:
            uid = _clean_text(getattr(profile, "global_uid", ""))
            if uid:
                by_uid.setdefault(uid, profile)
            for platform_id in getattr(profile, "platform_ids", None) or []:
                pid = _clean_text(platform_id)
                if pid:
                    by_uid.setdefault(pid, profile)

        lines: list[str] = []
        seen: set[str] = set()
        for sender_id in sender_ids:
            profile = by_uid.get(_clean_text(sender_id))
            if profile is None:
                continue
            uid = _clean_text(getattr(profile, "global_uid", ""))
            if not uid or uid == current_uid or uid in seen:
                continue
            nickname = _clean_text(getattr(profile, "nickname", ""))
            attitude = _clean_text(getattr(profile, "attitude", ""))
            if not nickname and not attitude:
                continue
            seen.add(uid)
            name = _clean_text(getattr(profile, "display_name", "")) or uid
            tier = getattr(profile, "tier", None)
            tier_label = _clean_text(getattr(tier, "label", ""))
            lines.append(
                f"- {name} (ID: {uid}) | 关系等级：{tier_label}"
                f" | 专属称呼：{nickname or '默认自然称呼'}"
                f" | 专属态度：{attitude or '按关系等级与你的人设性格自然相处'}"
            )
            if len(lines) >= 4:
                break
        if not lines:
            return ""
        return "\n\n[当前群聊其他活跃对话者档案]:\n" + "\n".join(lines)

    async def _render_user_relation_context(
        self, umo: str, user: UserProfile, soul_id: str = ""
    ) -> str:
        """Render ``USER_RELATION_CONTEXT_PROMPT`` for the current speaker (v2.6).

        The block documents the three-tier importance law plus the current user's
        profile (tier / intimacy / nickname / per-user attitude / notes) and the
        other configured speakers of the window. It never raises: any failure
        simply yields an empty block so the LLM request is never blocked.
        """
        display_name = _clean_text(getattr(user, "display_name", ""))
        global_uid = _clean_text(getattr(user, "global_uid", ""))
        display_name = display_name or global_uid
        tier = getattr(user, "tier", None)
        tier_label = _clean_text(getattr(tier, "label", ""))
        try:
            intimacy = round(float(getattr(user, "affinity", 0.0) or 0.0), 1)
        except (TypeError, ValueError):
            intimacy = 0.0
        nickname_text = _clean_text(getattr(user, "nickname", "")) or "默认自然称呼"
        attitude_text = (
            _clean_text(getattr(user, "attitude", ""))
            or "按关系等级与你的人设性格自然相处"
        )
        # ``relation_notes`` is not a DB column yet (v2.6 freeze): getattr fallback.
        notes_text = _clean_text(getattr(user, "relation_notes", "")) or "无"
        # v2.12: 身份归一 —— 标准称呼 + 曾用名 + 绑定账号，避免模型把同一人当两个人。
        canonical_name = (
            _clean_text(getattr(user, "canonical_name", "")) or display_name or global_uid
        )
        aliases = [
            _clean_text(alias)
            for alias in (getattr(user, "aliases", None) or [])
            if _clean_text(alias)
        ]
        platforms = [
            _clean_text(account)
            for account in (getattr(user, "platform_ids", None) or [])
            if _clean_text(account)
        ]
        aliases_text = "、".join(aliases) or "无"
        platforms_text = "、".join(platforms) or "无"
        try:
            window_peers_block = await self._render_window_peers_block(
                umo, user, soul_id=soul_id
            )
        except Exception as e:  # noqa: BLE001 - peers are best-effort only
            logger.warning(f"[OnlyYou] window peers context failed: {e}")
            window_peers_block = ""
        return USER_RELATION_CONTEXT_PROMPT.format(
            display_name=display_name,
            global_uid=global_uid,
            tier_label=tier_label,
            intimacy=intimacy,
            nickname_text=nickname_text,
            attitude_text=attitude_text,
            notes_text=notes_text,
            canonical_name=canonical_name,
            aliases_text=aliases_text,
            platforms_text=platforms_text,
            window_peers_block=window_peers_block,
        )

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
            # v2.12: always use the stable canonical name (identity normalisation).
            speaker_name = (
                _clean_text(getattr(user, "canonical_name", ""))
                or user.display_name
                or user.global_uid
            )
            req.prompt = (
                f"[{user.tier.label} {speaker_name}(ID:{user.global_uid})]: {req.prompt}"
            )

        # =====================================================================
        # 第二步：上下文感知 Query + 三路混合召回 (Top-24) + Rerank 精排 + 门闸
        # =====================================================================
        now = time.time()
        recall_query = self._build_recall_query(umo, user, query_text)
        query_vector = await self._embed_query(recall_query, soul)
        candidates = await self._retrieve_candidates(
            soul=soul,
            user=user,
            query_text=recall_query,
            query_vector=query_vector,
        )
        candidates = [
            mem
            for mem in candidates
            if not self._is_recent_window_duplicate(umo, mem, now)
        ]
        sem_scores = await self._semantic_scores(
            recall_query, candidates, query_vector, soul
        )

        scored_memories: list[tuple[float, float, Any]] = []
        for mem in candidates:
            sem_score = float(sem_scores.get(mem.id, 0.0))
            event_ts = int(
                getattr(mem, "effective_event_at", 0) or mem.created_at or now
            )
            days_old = max(0.0, (now - max(0, event_ts)) / 86400.0)
            recency_score = math.exp(-0.15 * days_old)
            imp_score = max(1, min(10, int(mem.importance))) / 10.0
            weight_sum = (
                self.recall_semantic_weight
                + self.recall_recency_weight
                + self.recall_importance_weight
            )
            if weight_sum <= 0:
                weight_sum = (
                    SEMANTIC_WEIGHT + RECENCY_WEIGHT + IMPORTANCE_WEIGHT
                )
            total_score = (
                self.recall_semantic_weight * sem_score
                + self.recall_recency_weight * recency_score
                + self.recall_importance_weight * imp_score
            ) / weight_sum
            # v2.12 最低相关性门闸：不相关的记忆绝不硬凑注入（旧版 0 分也会塞 6 条）。
            if total_score < self.min_recall_score:
                continue
            if sem_score < self.min_semantic_score:
                continue
            scored_memories.append((total_score, sem_score, mem))

        scored_memories.sort(key=lambda item: item[0], reverse=True)

        # =====================================================================
        # 第三步：硬性预算裁剪 (预算计入时间标签开销，超长项 continue 而不是 break)
        # =====================================================================
        selected_lines: list[str] = []
        current_chars = 0
        recalled_ids: list[int] = []
        recall_sem_scores: dict[int, float] = {}

        for _total_score, sem_score, mem in scored_memories:
            if len(selected_lines) >= self.max_injected_memories:
                break

            if mem.memory_tier == MemoryTier.CORE:
                tag = "核心记忆"
            elif mem.memory_tier == MemoryTier.RECENT:
                tag = "近期记忆"
            else:
                tag = "瞬时记忆"
            type_label = MEMORY_TYPE_LABELS.get(
                normalize_memory_type(getattr(mem, "memory_type", "episode")), ""
            )
            header = f"[{tag}"
            if type_label:
                header += f" · {type_label}"

            # v2.10: every recalled memory carries its event-time anchor so the model
            # can resolve relative expressions ("这周末 / 明天"). Content stays raw.
            anchor = describe_time_anchor(
                int(getattr(mem, "effective_event_at", 0) or mem.created_at), now
            )
            if anchor:
                header += f" · 形成于 {anchor}"
            header += "] "
            line = f"- {header}{mem.content}"
            # v2.12: the time label counts against the character budget too.
            cost = len(line) + 1
            if current_chars + cost > self.max_memory_chars_budget:
                # Skip this too-long memory but keep scanning shorter relevant ones.
                continue
            selected_lines.append(line)
            current_chars += cost
            recalled_ids.append(mem.id)
            recall_sem_scores[mem.id] = sem_score

        if recalled_ids:
            reinforce = getattr(self.memory_manager, "reinforce_memories", None)
            if callable(reinforce):
                try:
                    result = reinforce(recalled_ids, sem_scores=recall_sem_scores)
                    if inspect.isawaitable(result):
                        await result
                except TypeError:
                    result = reinforce(recalled_ids)
                    if inspect.isawaitable(result):
                        await result
                except Exception as exc:  # noqa: BLE001 - reinforcement is best effort
                    logger.debug(f"[OnlyYou] reinforce_memories failed: {exc}")

        # =====================================================================
        # 第四步：注入当前行动卡约束（BUSY_TASK 实时进度 / IDLE 繁忙成果与视频）
        #        + 变卦内心独白 + 精简记忆块到 System Prompt
        # =====================================================================
        additions: list[str] = []

        action_type = ActionType.coerce(
            getattr(current_slot, "action_type", None), None
        )
        if action_type is None:
            # Legacy slot data (no action_type): derive from the bound state.
            if current_slot.state == BotState.BUSY:
                action_type = ActionType.BUSY_TASK
            elif current_slot.state == BotState.RESTING:
                action_type = ActionType.SLEEP
            else:
                action_type = ActionType.IDLE_ROAM
        if action_type is not None:
            action_label = action_type.label
        else:  # pragma: no cover - defensive
            action_label = current_slot.state.value

        try:
            progress_pct = int(current_slot.get_progress_pct())
        except Exception:  # noqa: BLE001 - legacy slot without the helper
            progress_pct = 0
        try:
            start_time, end_time = current_slot.format_time_range().split("-")
        except Exception:  # noqa: BLE001 - legacy slot without the helper
            start_time, end_time = "", ""

        if current_slot.state == BotState.BUSY or action_type == ActionType.BUSY_TASK:
            additions.append(
                BUSY_STATE_INJECTION_PROMPT.format(
                    current_slot_title=current_slot.title,
                    progress_pct=progress_pct,
                    start_time=start_time,
                    end_time=end_time,
                    user_tier_name=user.tier.label,
                    sender_name=user.display_name,
                )
            )
        elif current_slot.state == BotState.IDLE:
            additions.append(
                IDLE_STATE_INJECTION_PROMPT.format(
                    action_type=action_label,
                    current_slot_title=current_slot.title,
                    progress_pct=progress_pct,
                    short_term_talk_text=self._render_short_term_talk_text(run),
                    recent_watched_video_text=self.session_buffer.get_recent_watched_video_text(),
                )
            )

        if current_slot.mutated and current_slot.mutation_monologue:
            additions.append(
                MUTATION_MONOLOGUE_INJECTION_PROMPT.format(
                    mutation_monologue=current_slot.mutation_monologue
                )
            )

        # =====================================================================
        # 第四步补充：注入用户关系等级重要度法则 + 当前对话对象专属档案（v2.6）
        # 整段 try/except 兜底，任何异常都静默降级为空 block，绝不阻塞 LLM 请求。
        # =====================================================================
        try:
            relation_block = await self._render_user_relation_context(
                umo, user, soul_id=getattr(soul, "soul_id", "")
            )
        except Exception as e:  # noqa: BLE001 - never break the LLM request hook
            logger.warning(f"[OnlyYou] user relation context injection failed: {e}")
            relation_block = ""
        if relation_block:
            additions.append(relation_block)

        if selected_lines:
            # v2.10: the instruction block must precede the memory lines so the model
            # treats each "形成于 …" label as the anchor for relative expressions.
            additions.append(
                MEMORY_TIME_CONTEXT_PROMPT.format(
                    current_datetime=current_datetime_text(now)
                )
            )
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
            "progress_pct": progress_pct,
            "action_type": action_type.value if action_type is not None else "",
            # v2.12 diagnostics: context-aware query / coarse pool / relevance scores.
            "recall_query": recall_query,
            "candidate_count": len(candidates),
            "semantic_scores": dict(recall_sem_scores),
        }
