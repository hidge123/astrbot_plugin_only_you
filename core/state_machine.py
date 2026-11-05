"""Three-state machine (RESTING / BUSY / IDLE) and two-atomic-action controller."""

from enum import Enum
from dataclasses import dataclass


class BotState(str, Enum):
    """Three core states of the anthropomorphic companion."""

    RESTING = "RESTING"  # 💤 休息中：不回任何消息，不刷视频
    BUSY = "BUSY"        # 🔥 繁忙中：只回 L2重要用户 & L3核心用户消息，不主动闲聊，不刷视频
    IDLE = "IDLE"        # ☕ 空闲中：L1/L2/L3 全量读空气回复，可主动搭话，主动刷B站/抖音视频


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
