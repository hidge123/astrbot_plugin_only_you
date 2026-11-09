"""Three-state machine (RESTING / BUSY / IDLE) and four-action-type controller."""

from enum import Enum
from dataclasses import dataclass
from typing import Any, Optional


class BotState(str, Enum):
    """Three core states of the anthropomorphic companion."""

    RESTING = "RESTING"  # 💤 休息中：不回任何消息，不刷视频
    BUSY = "BUSY"        # 🔥 繁忙中：只回 L2重要用户 & L3核心用户消息，不主动闲聊，不刷视频
    IDLE = "IDLE"        # ☕ 空闲中：L1/L2/L3 全量读空气回复，可主动搭话，主动刷B站/抖音视频


class ActionType(str, Enum):
    """Four concrete executable action archetypes bound to a :class:`BotState`.

    - ``SLEEP`` (RESTING): do absolutely nothing, fully silent.
    - ``SURF`` (IDLE): run the real Bilibili/Douyin surfing pipeline.
    - ``IDLE_ROAM`` (IDLE): lurk group chats for gossip, or muse over old memories.
    - ``BUSY_TASK`` (BUSY): current-activity memory + live progress bar + one random
      daily completion result written into Tier 1 memory.
    """

    SLEEP = "SLEEP"          # ① 睡觉卡 (RESTING)
    SURF = "SURF"            # ② 冲浪卡 (IDLE)
    IDLE_ROAM = "IDLE_ROAM"  # ③ 空闲游荡卡 (IDLE)
    BUSY_TASK = "BUSY_TASK"  # ④ 角色繁忙卡 (BUSY)

    @property
    def label(self) -> str:
        return {
            ActionType.SLEEP: "睡觉 / 休眠",
            ActionType.SURF: "冲浪刷片",
            ActionType.IDLE_ROAM: "空闲闲逛",
            ActionType.BUSY_TASK: "繁忙主业",
        }[self]

    @property
    def bound_state(self) -> "BotState":
        return {
            ActionType.SLEEP: BotState.RESTING,
            ActionType.SURF: BotState.IDLE,
            ActionType.IDLE_ROAM: BotState.IDLE,
            ActionType.BUSY_TASK: BotState.BUSY,
        }[self]

    @classmethod
    def coerce(
        cls, value: Any, default: Optional["ActionType"] = None
    ) -> Optional["ActionType"]:
        """Best-effort parse of an ``ActionType`` from a str/Enum value."""
        if isinstance(value, cls):
            return value
        raw = str(value or "").strip().upper()
        if raw in cls.__members__:
            return cls(raw)
        return default


# Four action archetypes used by the Web UI editor / badges / charts.
ACTION_TYPE_DEFINITIONS: list[dict[str, Any]] = [
    {
        "value": "SLEEP",
        "label": "睡觉 / 休眠",
        "state": "RESTING",
        "min_hours": 1.0,
        "max_hours": 7.5,
        "desc": "后台完全静默，什么都不做",
    },
    {
        "value": "SURF",
        "label": "冲浪刷片",
        "state": "IDLE",
        "min_hours": 0.5,
        "max_hours": 2.0,
        "desc": "真实刷 B站/抖音，看片打分并分享",
    },
    {
        "value": "IDLE_ROAM",
        "label": "空闲闲逛",
        "state": "IDLE",
        "min_hours": 0.5,
        "max_hours": 2.0,
        "desc": "翻群记录吃瓜，或发呆记忆联想",
    },
    {
        "value": "BUSY_TASK",
        "label": "繁忙主业",
        "state": "BUSY",
        "min_hours": 1.0,
        "max_hours": 3.0,
        "desc": "当前行为记忆 + 进度条 + 每日随机完成结果",
    },
]


class UserTier(int, Enum):
    """Three-tier user relationship hierarchy."""

    L1_NORMAL = 1        # 普通用户（群友）
    L2_IMPORTANT = 2     # ◆ 重要用户（混熟的老水友/熟人）
    L3_CORE = 3          # ★ 核心用户（管理员 / OnlyYou）

    @property
    def label(self) -> str:
        return {
            UserTier.L1_NORMAL: "L1普通群友",
            UserTier.L2_IMPORTANT: "◆L2重要熟人",
            UserTier.L3_CORE: "★L3核心用户",
        }[self]


class AtomicAction(str, Enum):
    """Two concrete executable actions of the bot."""

    MESSAGE = "MESSAGE"        # 行为一：回/发消息管线
    SURF_VIDEO = "SURF_VIDEO"  # 行为二：刷视频管线


@dataclass
class GateDecision:
    """Result of the three-state message gate evaluation."""

    allowed: bool
    state: BotState
    user_tier: UserTier
    reason: str
    requires_atmosphere_check: bool = True


class StateController:
    """Stateless controller enforcing the 3-state x 2-action rules."""

    @staticmethod
    def check_message_gate(
        state: BotState,
        user_tier: UserTier,
        is_private_chat: bool = False,
    ) -> GateDecision:
        """
        Evaluate Action 1 (MESSAGE pipeline) against current BotState and UserTier.
        Zero-token pre-filtering before any LLM or atmosphere check.
        """
        if state == BotState.RESTING:
            return GateDecision(
                allowed=False,
                state=state,
                user_tier=user_tier,
                reason="处于【💤 休息中 (RESTING)】，拦截所有消息",
                requires_atmosphere_check=False,
            )

        if state == BotState.BUSY:
            if user_tier == UserTier.L1_NORMAL:
                return GateDecision(
                    allowed=False,
                    state=state,
                    user_tier=user_tier,
                    reason="处于【🔥 繁忙中 (BUSY)】，过滤 L1 普通用户消息",
                    requires_atmosphere_check=False,
                )
            return GateDecision(
                allowed=True,
                state=state,
                user_tier=user_tier,
                reason=f"处于【🔥 繁忙中 (BUSY)】，放行 {user_tier.label}",
                requires_atmosphere_check=not is_private_chat,
            )

        # BotState.IDLE
        return GateDecision(
            allowed=True,
            state=state,
            user_tier=user_tier,
            reason=f"处于【☕ 空闲中 (IDLE)】，全量放行 {user_tier.label}",
            requires_atmosphere_check=not is_private_chat,
        )

    @staticmethod
    def can_surf_video(state: BotState) -> bool:
        """Evaluate Action 2 (SURF_VIDEO pipeline): only active in IDLE state."""
        return state == BotState.IDLE

    @staticmethod
    def can_initiate_proactive_chat(state: BotState) -> bool:
        """Proactive chat (encounters / video sharing) is only allowed in IDLE state."""
        return state == BotState.IDLE
