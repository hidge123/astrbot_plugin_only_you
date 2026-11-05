"""Input-driven bounded schedule mutator with 3 hard guardrails (core/schedule_mutator.py)."""

import time
from typing import Any, Optional

from .model_hub import ModelHub
from .roguelite_deck import DailyScheduleRun, RogueliteDeckEngine, SlotInstance
from .soul_manager import SoulProfile
from .state_machine import BotState, UserTier
from ..prompts.templates import BOUNDED_STATE_MUTATION_PROMPT

try:
    from astrbot.api import logger
except ImportError:
    import logging
    logger = logging.getLogger("onlyyou.schedule_mutator")


class BoundedScheduleMutator:
    """
    输入驱动的受限日程微调器 (docs/ARCHITECTURE.md Section 3.3):
    三重防失控硬性限制：
    1. 每日最大微调次数配额 `run.mutations_used < max_mutations_per_day` (默认 2 次)
    2. 微调冷却时间锁 `time.time() - run.last_mutation_ts >= mutation_cooldown_seconds` (默认 120 分钟)
    3. 定力护甲门槛 `impact > current_slot.armor`
    """

    def __init__(
        self,
        deck_engine: RogueliteDeckEngine,
        model_hub: ModelHub,
        plugin_config: dict[str, Any],
    ):
        self.deck_engine = deck_engine
        self.model_hub = model_hub
        self.plugin_config = plugin_config
        # Track recent private call bursts from L3 core users for wake-from-resting
        self._l3_wake_bursts: dict[str, list[float]] = {}

    @property
    def cfg(self) -> dict[str, Any]:
        return self.plugin_config.get("roguelite_schedule_config", {}) or {}

    @property
    def max_mutations_per_day(self) -> int:
        return int(self.cfg.get("max_mutations_per_day", 2))

    @property
    def mutation_cooldown_seconds(self) -> int:
        return int(self.cfg.get("mutation_cooldown_minutes", 120)) * 60

    @property
    def allow_l3_wake_from_resting(self) -> bool:
        return bool(self.cfg.get("allow_l3_wake_from_resting", True))

    async def can_mutate(
        self, run: DailyScheduleRun, current_slot: SlotInstance, impact: int
    ) -> bool:
        """Exact 3-gate hard constraint check from docs/ARCHITECTURE.md Section 3.3."""
        # 限制 1：每日最大微调次数配额（默认每天最多只允许变卦 2 次）
        if run.mutations_used >= self.max_mutations_per_day:
            return False

        # 限制 2：微调冷却时间锁（每次微调后，至少锁定 120 分钟不允许再次改动规划）
        if run.last_mutation_ts > 0 and (time.time() - run.last_mutation_ts) < self.mutation_cooldown_seconds:
            return False

        # 限制 3：定力护甲门槛（输入冲击力必须严格大于当前卡片的定力护甲）
        if impact <= current_slot.armor:
            return False

        return True

    def compute_message_impact(
        self,
        current_slot: SlotInstance,
        next_slot: SlotInstance,
        user_tier: UserTier,
        user_id: str,
        message_str: str,
        is_private_chat: bool,
        is_direct_mention: bool,
        recent_l2_l3_burst_count: int = 0,
    ) -> int:
        """
        Calculate the impact score (0 ~ 100) of an incoming message across the 3 message scenarios:
        - Scenario 1 (摸鱼突变): BUSY -> IDLE when L3 calls or multiple L2s burst
        - Scenario 2 (热聊熬夜): IDLE (with next slot RESTING/BUSY) when L3/L2 hot chat
        - Scenario 3 (核心唤醒): RESTING -> IDLE ONLY when L3 Core User calls in private chat
        """
        msg = (message_str or "").strip()
        urgent_keywords = ("急", "快看", "大瓜", "救命", "睡了吗", "醒醒", "在不在", "难受", "离谱", "大事", "！！", "!!")
        has_urgent_kw = any(k in msg for k in urgent_keywords)

        # Scenario 3: RESTING state
        if current_slot.state == BotState.RESTING:
            if not self.allow_l3_wake_from_resting:
                return 0
            if user_tier != UserTier.L3_CORE or not is_private_chat:
                # L1/L2 or group chat can NEVER wake Bot from RESTING
                return 0

            now = time.time()
            history = [t for t in self._l3_wake_bursts.get(user_id, []) if now - t <= 300]
            history.append(now)
            self._l3_wake_bursts[user_id] = history

            # Consecutive messages from L3 in private chat or urgent wake call
            if len(history) >= 2 or has_urgent_kw:
                return 88
            return 45

        # Scenario 1: BUSY state
        if current_slot.state == BotState.BUSY:
            if user_tier == UserTier.L1_NORMAL:
                return 10
            if user_tier == UserTier.L3_CORE:
                base = 78 if (is_private_chat or is_direct_mention) else 62
                return min(98, base + (12 if has_urgent_kw or len(msg) >= 15 else 0))
            if user_tier == UserTier.L2_IMPORTANT:
                base = 52 if recent_l2_l3_burst_count >= 2 else 38
                return min(85, base + (15 if has_urgent_kw else 0))

        # Scenario 2: IDLE state about to enter RESTING or BUSY
        if current_slot.state == BotState.IDLE and next_slot.state in (BotState.RESTING, BotState.BUSY):
            if user_tier == UserTier.L3_CORE and (has_urgent_kw or recent_l2_l3_burst_count >= 3):
                return 75
            if user_tier == UserTier.L2_IMPORTANT and recent_l2_l3_burst_count >= 4 and has_urgent_kw:
                return 68

        return 0

    async def on_message_input(
        self,
        soul: SoulProfile,
        event: Any,
        user: Any,
        is_private_chat: bool = False,
        is_direct_mention: bool = False,
        recent_l2_l3_burst_count: int = 0,
    ) -> bool:
        """
        Evaluate whether an incoming user message triggers a bounded schedule mutation.
        Returns True if the schedule was mutated.
        """
        run, current_slot, next_slot = await self.deck_engine.get_current_and_next_slot(
            soul_id=soul.soul_id,
            custom_cards=soul.state_event_cards,
            custom_encounters=soul.encounter_talk_cards,
        )

        msg_str = getattr(event, "message_str", "") or ""
        umo = getattr(event, "unified_msg_origin", None)

        impact = self.compute_message_impact(
            current_slot=current_slot,
            next_slot=next_slot,
            user_tier=user.tier,
            user_id=user.global_uid,
            message_str=msg_str,
            is_private_chat=is_private_chat,
            is_direct_mention=is_direct_mention,
            recent_l2_l3_burst_count=recent_l2_l3_burst_count,
        )

        if not await self.can_mutate(run, current_slot, impact):
            return False

        input_detail = (
            f"来自 [{user.tier.label} {user.display_name}] 的消息（冲击力 Impact={impact}）：{msg_str}"
        )
        return await self._execute_mutation(
            soul=soul,
            run=run,
            current_slot=current_slot,
            next_slot=next_slot,
            input_type="USER_MESSAGE",
            input_detail=input_detail,
            umo=umo,
        )

    async def on_video_input(
        self,
        soul: SoulProfile,
        video_title: str,
        video_score: int,
        umo: Optional[str] = None,
    ) -> bool:
        """
        Scenario 4 (刷片上头): In IDLE state, watching a high-score video (`>= 88`) when
        next slot is BUSY or RESTING delays the next slot by 30~45 minutes.
        """
        if video_score < 88:
            return False

        run, current_slot, next_slot = await self.deck_engine.get_current_and_next_slot(
            soul_id=soul.soul_id,
            custom_cards=soul.state_event_cards,
            custom_encounters=soul.encounter_talk_cards,
        )

        if current_slot.state != BotState.IDLE or next_slot.state not in (
            BotState.BUSY,
            BotState.RESTING,
        ):
            return False

        impact = video_score
        if not await self.can_mutate(run, current_slot, impact):
            return False

        input_detail = f"刚刷到超上头高分视频《{video_title}》（多模态评分：{video_score}/100）"
        return await self._execute_mutation(
            soul=soul,
            run=run,
            current_slot=current_slot,
            next_slot=next_slot,
            input_type="WATCHED_VIDEO",
            input_detail=input_detail,
            umo=umo,
        )

    async def _execute_mutation(
        self,
        soul: SoulProfile,
        run: DailyScheduleRun,
        current_slot: SlotInstance,
        next_slot: SlotInstance,
        input_type: str,
        input_detail: str,
        umo: Optional[str] = None,
    ) -> bool:
        """Invoke `BOUNDED_STATE_MUTATION_PROMPT` (with deterministic fallback) and persist."""
        prompt = BOUNDED_STATE_MUTATION_PROMPT.format(
            soul_name=soul.name,
            current_slot_title=current_slot.title,
            current_state=current_slot.state.value,
            current_armor=current_slot.armor,
            next_slot_title=next_slot.title,
            next_state=next_slot.state.value,
            input_type=input_type,
            input_detail=input_detail,
        )

        llm_res = await self.model_hub.call_llm_json(
            prompt=prompt, role="decision", umo=umo, default=None
        )

        # Deterministic fallback if decision LLM fails or returns malformed JSON
        if not isinstance(llm_res, dict):
            if current_slot.state in (BotState.BUSY, BotState.RESTING):
                llm_res = {
                    "mutate_target": "CURRENT_SLOT",
                    "new_state": "IDLE",
                    "new_title": (
                        "被叫醒迷迷糊糊看手机"
                        if current_slot.state == BotState.RESTING
                        else "放下手头正事专心摸鱼陪聊"
                    ),
                    "delay_next_slot_minutes": 0,
                    "mutation_reason_monologue": (
                        "本来在休息/忙正事的，但看到你的消息决定先陪你聊会儿！"
                    ),
                }
            else:
                llm_res = {
                    "mutate_target": "NEXT_SLOT",
                    "new_state": "IDLE",
                    "new_title": "太上头了决定再加钟冲浪一会儿",
                    "delay_next_slot_minutes": 35,
                    "mutation_reason_monologue": "聊得/刷得太起劲了，再玩半小时！",
                }

        target = str(llm_res.get("mutate_target", "CURRENT_SLOT")).upper()
        raw_new_state = str(llm_res.get("new_state", "IDLE")).upper()
        new_state = (
            BotState(raw_new_state)
            if raw_new_state in BotState.__members__
            else BotState.IDLE
        )
        new_title = str(llm_res.get("new_title") or "临时变卦摸鱼水群")[:24]
        delay_mins = int(llm_res.get("delay_next_slot_minutes") or 0)
        monologue = str(
            llm_res.get("mutation_reason_monologue")
            or "本来有别的安排，不管了先摸鱼聊会儿！"
        )

        if target == "NEXT_SLOT" or current_slot.state == BotState.IDLE:
            next_slot.delay_minutes = max(30, min(60, delay_mins or 35))
            current_slot.mutated = True
            current_slot.mutation_monologue = monologue
            if new_title:
                current_slot.title = new_title
        else:
            # CURRENT_SLOT mutation (BUSY -> IDLE or RESTING -> IDLE)
            current_slot.state = new_state
            current_slot.title = new_title
            current_slot.mutated = True
            current_slot.mutation_monologue = monologue

        run.mutations_used += 1
        run.last_mutation_ts = time.time()
        run.last_mutation_log = f"[{target}] -> {new_state.value} ({new_title}): {monologue}"

        await self.deck_engine.save_run(run)
        logger.info(
            f"[OnlyYou] Schedule mutated ({run.mutations_used}/{self.max_mutations_per_day}): "
            f"{run.last_mutation_log}"
        )
        return True
