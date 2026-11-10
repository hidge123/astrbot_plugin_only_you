"""Input-driven bounded schedule mutator with 3 hard guardrails (core/schedule_mutator.py).

The mutator is the only component allowed to bend today's 24h dynamic timeline in
reaction to *real world* input (a high-impact user message or a high-score video).
Every mutation still has to pass the three hard guardrails (daily quota / cooldown
lock / ``impact > armor``) and then dispatches on the current block's ``ActionType``:

* ``BUSY_TASK`` -> split the block at the nearest half-hour point, settle the front
  half through the injected ``busy_settler(soul, run, block, interrupted_pct,
  interrupted_by)`` callback (writes ``completion_result`` / ``busy_settled`` / Tier 1
  memory), and turn the remaining part into ``IDLE_ROAM`` (偷偷摸鱼陪聊).
* ``SLEEP`` -> cut a small (0.5h) ``IDLE_ROAM`` waking window out of the sleep block
  (only an ★L3 core user can wake her), keeping the remaining sleep segments intact.
* ``SURF`` / ``IDLE_ROAM`` -> extend the block by 0.5h and push (or swallow) the next
  block — "太上头了再加钟一会儿".

After any mutation :meth:`BoundedScheduleMutator._reindex_slots` re-establishes the
timeline invariants (contiguous ``0.0 -> 24.0``, 0.5h grid, every block >= 0.5h,
``slot_key = block_{i}``, last ``end_hour == 24.0``). All exceptions are contained in
this module so a broken mutation can never crash the message-gate hook.
"""

import inspect
import time
from datetime import datetime
from typing import Any, Optional

from .model_hub import ModelHub
from .roguelite_deck import DailyScheduleRun, RogueliteDeckEngine, SlotInstance
from .soul_manager import SoulProfile
from .state_machine import ActionType, BotState, UserTier
from ..prompts.templates import BOUNDED_STATE_MUTATION_PROMPT

try:
    from astrbot.api import logger
except ImportError:
    import logging

    logger = logging.getLogger("onlyyou.schedule_mutator")


#: Smallest legal dynamic block on the 24h timeline.
MIN_BLOCK_HOURS = 0.5
#: Length of the ``IDLE_ROAM`` window carved out of a sleeping block (0.5h grid).
AWAKE_BLOCK_HOURS = 0.5
#: Every extension of a ``SURF`` / ``IDLE_ROAM`` block (0.5h grid).
EXTEND_HOURS = 0.5

#: Keyword set telling whether a message is an urgent "wake me up" call.
URGENT_KEYWORDS = (
    "急",
    "快看",
    "大瓜",
    "救命",
    "睡了吗",
    "醒醒",
    "在不在",
    "难受",
    "离谱",
    "大事",
    "！！",
    "!!",
)

#: Title keywords used to infer ``SURF`` from legacy (action_type-less) blocks.
_SURF_TITLE_KEYWORDS = ("刷", "B站", "b站", "抖音", "视频", "冲浪", "追剧", "看剧")


def _snap_hour(value: Any, default: float = 0.0) -> float:
    """Snap an hour value onto the 0.5h grid (defensive against legacy data)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return round(float(default) * 2) / 2
    return round(number * 2) / 2


def _clamp(value: float, low: float, high: float) -> float:
    """Clamp ``value`` into ``[low, high]`` (returns ``low`` if the range is empty)."""
    if high < low:
        return low
    return max(low, min(high, value))


class BoundedScheduleMutator:
    """
    输入驱动的受限日程微调器 (docs/ARCHITECTURE.md Section 3.3):
    三重防失控硬性限制：
    1. 每日最大微调次数配额 `run.mutations_used < max_mutations_per_day` (默认 2 次)
    2. 微调冷却时间锁 `time.time() - run.last_mutation_ts >= mutation_cooldown_seconds` (默认 120 分钟)
    3. 定力护甲门槛 `impact > current_slot.armor`（v2.6 起 armor 由动作类型固定四档派生：
       IDLE_ROAM=25 / SURF=40 / BUSY_TASK=65 / SLEEP=80，不再手工设置）

    微调成功后按当前时间块的 ``ActionType`` 分支重排 24h 动态时间轴（见模块 docstring），
    且所有微调产物（切分前半 / 后半 / 唤醒窗 / 加钟块）都回收为各自动作的固定护甲档位。
    """

    def __init__(
        self,
        deck_engine: RogueliteDeckEngine,
        model_hub: ModelHub,
        plugin_config: dict[str, Any],
        busy_settler: Optional[Any] = None,
    ):
        self.deck_engine = deck_engine
        self.model_hub = model_hub
        self.plugin_config = plugin_config
        # Injected by main.py: async callable(soul, run, block, progress_pct:int,
        # interrupted_by:str) -> Any. It settles a BUSY_TASK block (writes
        # completion_result / busy_settled / Tier 1 memory / save_run). Tests may
        # inject a stub; main.py assigns ``plugin.settle_busy_block`` after init.
        self.busy_settler = busy_settler
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

    # ==========================================================================
    # Action-type helpers (prefer action_type, fall back to legacy state)
    # ==========================================================================
    @staticmethod
    def _slot_state(slot: Any) -> BotState:
        """Best-effort ``BotState`` of a block (tolerates legacy / stub objects)."""
        raw = getattr(slot, "state", None)
        if isinstance(raw, BotState):
            return raw
        text = str(raw or "").strip().upper()
        if text in BotState.__members__:
            return BotState(text)
        action = ActionType.coerce(getattr(slot, "action_type", None), None)
        return action.bound_state if action is not None else BotState.IDLE

    @staticmethod
    def _slot_action(slot: Any) -> ActionType:
        """Best-effort ``ActionType`` of a block: action_type first, then state/title."""
        action = ActionType.coerce(getattr(slot, "action_type", None), None)
        if action is not None:
            return action
        state = BoundedScheduleMutator._slot_state(slot)
        if state == BotState.RESTING:
            return ActionType.SLEEP
        if state == BotState.BUSY:
            return ActionType.BUSY_TASK
        title = str(getattr(slot, "title", "") or "")
        if any(keyword in title for keyword in _SURF_TITLE_KEYWORDS):
            return ActionType.SURF
        return ActionType.IDLE_ROAM

    @staticmethod
    def _slot_title(slot: Any) -> str:
        return str(getattr(slot, "title", "") or "")

    @staticmethod
    def _soul_chronotype(soul: Any) -> str:
        """Chronotype of the Soul (``early``/``normal``/``night_owl``, default normal).

        The mutator is often the *first* component to touch today's run in
        ``main.py::on_any_message``, so it must pass the Soul's chronotype when
        building/loading the daily timeline, otherwise an ``early``/``night_owl``
        character would get the default ``normal`` curve for the whole day.
        """
        try:
            value = getattr(soul, "chronotype", None)
            if callable(value):
                value = value()
        except Exception:  # pragma: no cover - defensive
            value = None
        text = str(value or "").strip().lower()
        return text if text in ("early", "normal", "night_owl") else "normal"

    @staticmethod
    def _accepts_keyword(func: Any, keyword: str) -> bool:
        """Whether ``func`` accepts ``keyword`` (or has ``**kwargs``); stub tolerant."""
        try:
            signature = inspect.signature(func)
        except (TypeError, ValueError):  # pragma: no cover - builtins/mocks
            return False
        parameters = signature.parameters
        if keyword in parameters:
            return True
        return any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )

    async def _fetch_current_and_next(
        self, soul: Any, now_dt: Optional[datetime] = None
    ) -> tuple[DailyScheduleRun, SlotInstance, SlotInstance]:
        """Fetch today's ``(run, current_slot, next_slot)`` with the Soul's chronotype."""
        kwargs: dict[str, Any] = {
            "soul_id": getattr(soul, "soul_id", ""),
            "custom_cards": getattr(soul, "state_event_cards", None),
            "custom_encounters": getattr(soul, "encounter_talk_cards", []),
            "now_dt": now_dt,
        }
        fetch = self.deck_engine.get_current_and_next_slot
        if self._accepts_keyword(fetch, "chronotype"):
            kwargs["chronotype"] = self._soul_chronotype(soul)
        return await fetch(**kwargs)

    # ==========================================================================
    # Guardrail 1-3 (unchanged tri-gate)
    # ==========================================================================
    async def can_mutate(
        self, run: DailyScheduleRun, current_slot: SlotInstance, impact: int
    ) -> bool:
        """Exact 3-gate hard constraint check from docs/ARCHITECTURE.md Section 3.3."""
        # 限制 1：每日最大微调次数配额（默认每天最多只允许变卦 2 次）
        if int(getattr(run, "mutations_used", 0) or 0) >= self.max_mutations_per_day:
            return False

        # 限制 2：微调冷却时间锁（每次微调后，至少锁定 120 分钟不允许再次改动规划）
        last_ts = float(getattr(run, "last_mutation_ts", 0.0) or 0.0)
        if last_ts > 0 and (time.time() - last_ts) < self.mutation_cooldown_seconds:
            return False

        # 限制 3：定力护甲门槛（输入冲击力必须严格大于当前卡片的定力护甲）
        armor = int(getattr(current_slot, "armor", 50) or 0)
        if impact <= armor:
            return False

        return True

    # ==========================================================================
    # Impact scoring
    # ==========================================================================
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
        Calculate the impact score (0 ~ 100) of an incoming message across the 3 message scenarios.
        Scene dispatch now prefers ``current_slot.action_type`` (with a legacy
        ``BotState`` fallback when action_type is missing):
        - Scenario 1 (摸鱼突变): BUSY_TASK -> IDLE_ROAM when L3 calls or multiple L2s burst
        - Scenario 2 (热聊熬夜): SURF / IDLE_ROAM before a SLEEP / BUSY_TASK block
        - Scenario 3 (核心唤醒): SLEEP -> IDLE_ROAM ONLY when L3 Core User calls in private chat
        """
        msg = (message_str or "").strip()
        has_urgent_kw = any(keyword in msg for keyword in URGENT_KEYWORDS)

        current_action = self._slot_action(current_slot)
        next_action = self._slot_action(next_slot)

        # Scenario 3: SLEEP (legacy RESTING) — only an L3 core private wake call counts.
        if current_action == ActionType.SLEEP:
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

        # Scenario 1: BUSY_TASK (legacy BUSY)
        if current_action == ActionType.BUSY_TASK:
            if user_tier == UserTier.L1_NORMAL:
                return 10
            if user_tier == UserTier.L3_CORE:
                base = 78 if (is_private_chat or is_direct_mention) else 62
                return min(98, base + (12 if has_urgent_kw or len(msg) >= 15 else 0))
            if user_tier == UserTier.L2_IMPORTANT:
                base = 52 if recent_l2_l3_burst_count >= 2 else 38
                return min(85, base + (15 if has_urgent_kw else 0))

        # Scenario 2: SURF / IDLE_ROAM about to enter a SLEEP / BUSY_TASK block
        if (
            current_action in (ActionType.SURF, ActionType.IDLE_ROAM)
            and next_action in (ActionType.SLEEP, ActionType.BUSY_TASK)
        ):
            if user_tier == UserTier.L3_CORE and (
                has_urgent_kw or recent_l2_l3_burst_count >= 3
            ):
                return 75
            if (
                user_tier == UserTier.L2_IMPORTANT
                and recent_l2_l3_burst_count >= 4
                and has_urgent_kw
            ):
                return 68

        return 0

    # ==========================================================================
    # Public entry points
    # ==========================================================================
    async def on_message_input(
        self,
        soul: SoulProfile,
        event: Any,
        user: Any,
        is_private_chat: bool = False,
        is_direct_mention: bool = False,
        recent_l2_l3_burst_count: int = 0,
        now_dt: Optional[datetime] = None,
    ) -> bool:
        """
        Evaluate whether an incoming user message triggers a bounded schedule mutation.
        Returns True if the schedule was mutated.
        """
        run, current_slot, next_slot = await self._fetch_current_and_next(
            soul, now_dt=now_dt
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
        interrupted_by = (
            str(getattr(user, "display_name", "") or getattr(user, "global_uid", "") or "")
            .strip()
        )
        return await self._execute_mutation(
            soul=soul,
            run=run,
            current_slot=current_slot,
            next_slot=next_slot,
            input_type="USER_MESSAGE",
            input_detail=input_detail,
            umo=umo,
            now_dt=now_dt,
            interrupted_by=interrupted_by,
        )

    async def on_video_input(
        self,
        soul: SoulProfile,
        video_title: str,
        video_score: int,
        umo: Optional[str] = None,
        now_dt: Optional[datetime] = None,
    ) -> bool:
        """
        Scenario 4 (刷片上头): In an IDLE block, watching a high-score video (`>= 88`)
        when the next block is BUSY_TASK or SLEEP extends the current block by 0.5h.
        """
        if video_score < 88:
            return False

        run, current_slot, next_slot = await self._fetch_current_and_next(
            soul, now_dt=now_dt
        )

        current_action = self._slot_action(current_slot)
        next_action = self._slot_action(next_slot)
        if current_action not in (ActionType.SURF, ActionType.IDLE_ROAM):
            return False
        if next_action not in (ActionType.BUSY_TASK, ActionType.SLEEP):
            return False

        impact = video_score
        if not await self.can_mutate(run, current_slot, impact):
            return False

        input_detail = f"刚刷到超上头高分视频《{video_title}》（多模态评分：{video_score}/100）"
        interrupted_by = f"刷到高分视频《{video_title}》"[:40]
        return await self._execute_mutation(
            soul=soul,
            run=run,
            current_slot=current_slot,
            next_slot=next_slot,
            input_type="WATCHED_VIDEO",
            input_detail=input_detail,
            umo=umo,
            now_dt=now_dt,
            interrupted_by=interrupted_by,
        )

    # ==========================================================================
    # Prompt + deterministic fallbacks
    # ==========================================================================
    def _build_mutation_prompt(
        self,
        soul: SoulProfile,
        current_slot: SlotInstance,
        next_slot: SlotInstance,
        input_type: str,
        input_detail: str,
    ) -> str:
        """Format the bounded mutation prompt (new action-type placeholders included)."""
        fields = {
            "soul_name": getattr(soul, "name", "她"),
            "current_slot_title": self._slot_title(current_slot),
            "current_state": self._slot_state(current_slot).value,
            "current_armor": int(getattr(current_slot, "armor", 50) or 0),
            "next_slot_title": self._slot_title(next_slot),
            "next_state": self._slot_state(next_slot).value,
            "input_type": input_type,
            "input_detail": input_detail,
            "current_action_type": self._slot_action(current_slot).value,
            "next_action_type": self._slot_action(next_slot).value,
        }
        try:
            return BOUNDED_STATE_MUTATION_PROMPT.format(**fields)
        except (KeyError, IndexError, ValueError) as exc:
            # A template/contract mismatch must degrade, never crash the pipeline.
            logger.warning(f"[OnlyYou] mutation prompt format failed ({exc!r}), degrading")
            return (
                f"角色【{fields['soul_name']}】正在按今日 24h 时间轴行事，当前时间块："
                f"【{fields['current_slot_title']}】({fields['current_state']}/"
                f"{fields['current_action_type']})，收到外部输入：{input_detail}。"
                "请仅输出 JSON：{\"new_title\": str(15字内), "
                "\"mutation_reason_monologue\": str}"
            )

    @staticmethod
    def _fallback_copy(action: ActionType, current_slot: Any) -> tuple[str, str]:
        """Deterministic per-branch fallback ``(new_title, monologue)`` when the LLM fails."""
        current_title = str(getattr(current_slot, "title", "") or "").strip()
        if action == ActionType.BUSY_TASK:
            return (
                "放下手头正事专心摸鱼陪聊",
                f"本来在忙着{current_title or '手头的正事'}的，看到你的消息，"
                "不管了先摸鱼陪你聊会儿！",
            )
        if action == ActionType.SLEEP:
            return (
                "被吵醒迷迷糊糊看手机",
                "睡得正香被硬生生吵醒，迷迷糊糊抓起手机看看是谁找我……",
            )
        return (
            "太上头了决定再加钟一会儿",
            "聊得/刷得太起劲了，不管了，再玩半小时！",
        )

    def _parse_llm_response(
        self, llm_res: Any, action: ActionType, current_slot: Any
    ) -> tuple[str, str]:
        """Extract ``(new_title, monologue)`` from the decision JSON, with fallbacks."""
        fallback_title, fallback_monologue = self._fallback_copy(action, current_slot)
        raw_title = ""
        monologue = ""
        if isinstance(llm_res, dict):
            raw_title = str(llm_res.get("new_title") or "").strip()
            monologue = str(
                llm_res.get("mutation_reason_monologue")
                or llm_res.get("mutate_reason_monologue")
                or llm_res.get("monologue")
                or ""
            ).strip()
        new_title = (raw_title or fallback_title)[:24]
        return new_title, monologue or fallback_monologue

    # ==========================================================================
    # Mutation execution
    # ==========================================================================
    async def _execute_mutation(
        self,
        soul: SoulProfile,
        run: DailyScheduleRun,
        current_slot: SlotInstance,
        next_slot: SlotInstance,
        input_type: str,
        input_detail: str,
        umo: Optional[str] = None,
        now_dt: Optional[datetime] = None,
        interrupted_by: str = "",
    ) -> bool:
        """Invoke `BOUNDED_STATE_MUTATION_PROMPT` (deterministic fallback) and apply the
        action-type-aware block split / extension, then persist. Never raises."""
        try:
            now = now_dt or datetime.now()
            now_hour = now.hour + now.minute / 60.0 + now.second / 3600.0
            action = self._slot_action(current_slot)

            prompt = self._build_mutation_prompt(
                soul, current_slot, next_slot, input_type, input_detail
            )
            llm_res = await self.model_hub.call_llm_json(
                prompt=prompt, role="decision", umo=umo, default=None
            )
            new_title, monologue = self._parse_llm_response(llm_res, action, current_slot)

            if action == ActionType.BUSY_TASK:
                target = "CURRENT_SLOT"
                await self._apply_busy_task_interrupt(
                    soul=soul,
                    run=run,
                    block=current_slot,
                    now_dt=now,
                    now_hour=now_hour,
                    new_title=new_title,
                    monologue=monologue,
                    interrupted_by=interrupted_by,
                )
            elif action == ActionType.SLEEP:
                target = "CURRENT_SLOT"
                self._apply_sleep_wake(
                    run=run,
                    block=current_slot,
                    now_hour=now_hour,
                    new_title=new_title,
                    monologue=monologue,
                )
            else:
                # SURF / IDLE_ROAM (scenario 2 heat-chat / scenario 4 video binge)
                target = "NEXT_SLOT"
                self._apply_idle_extension(
                    run=run,
                    block=current_slot,
                    new_title=new_title,
                    monologue=monologue,
                )

            self._reindex_slots(run)

            run.mutations_used = int(getattr(run, "mutations_used", 0) or 0) + 1
            run.last_mutation_ts = time.time()
            run.last_mutation_log = (
                f"[{target}] -> {BotState.IDLE.value} ({new_title}): {monologue}"
            )

            await self.deck_engine.save_run(run)
            logger.info(
                f"[OnlyYou] Schedule mutated ({run.mutations_used}/{self.max_mutations_per_day}): "
                f"{run.last_mutation_log}"
            )
            return True
        except Exception as exc:  # pragma: no cover - defensive: never break the hook
            logger.warning(f"[OnlyYou] schedule mutation aborted ({exc!r})")
            return False

    # ------------------------------------------------------------------
    # Branch A: BUSY_TASK interrupted by a high-impact message
    # ------------------------------------------------------------------
    async def _apply_busy_task_interrupt(
        self,
        soul: SoulProfile,
        run: DailyScheduleRun,
        block: SlotInstance,
        now_dt: datetime,
        now_hour: float,
        new_title: str,
        monologue: str,
        interrupted_by: str,
    ) -> None:
        """Split the busy block at the nearest half-hour, settle the front half and
        flip the rest to ``IDLE_ROAM`` (or flip the whole block in place when the
        split points are too close to the block edges / block is not in the timeline).
        """
        interrupted_pct = self._progress_pct(block, now_dt)
        index = self._find_slot_index(run, block)
        start = _snap_hour(getattr(block, "start_hour", 0.0))
        end = _snap_hour(getattr(block, "end_hour", start + MIN_BLOCK_HOURS))
        split_h = _clamp(_snap_hour(now_hour), start, end)

        can_split = (
            index >= 0
            and (split_h - start) >= MIN_BLOCK_HOURS
            and (end - split_h) >= MIN_BLOCK_HOURS
        )

        if can_split:
            # 前半块（已完成部分）保留 BUSY_TASK，护甲回落固定档位 65
            block.end_hour = split_h
            block.armor = ActionType.BUSY_TASK.fixed_armor
            # 后半块（当前生效块）转为 IDLE_ROAM 并插入时间轴，护甲固定档位 25
            tail = SlotInstance(
                slot_key=f"block_{index + 1}",
                start_hour=split_h,
                end_hour=end,
                state=BotState.IDLE,
                card_id=str(getattr(block, "card_id", "evt_default") or "evt_default"),
                title=new_title,
                label=ActionType.IDLE_ROAM.label,
                action_type=ActionType.IDLE_ROAM,
                armor=ActionType.IDLE_ROAM.fixed_armor,
                mutated=True,
                mutation_monologue=monologue,
                delay_minutes=0,
            )
            run.slots.insert(index + 1, tail)

        # 半途打断结算：前半块 / 原块（settler 负责 completion_result + Tier1 记忆）
        await self._settle_busy_block(
            soul=soul,
            run=run,
            block=block,
            progress_pct=interrupted_pct,
            interrupted_by=interrupted_by,
        )

        if not can_split:
            # 刚开头或剩余极短：原地把该块转为 IDLE_ROAM（completion_result 保留为打断记录）
            self._convert_to_idle_roam(block, new_title, monologue, keep_busy_result=True)

    # ------------------------------------------------------------------
    # Branch B: SLEEP core wake-up
    # ------------------------------------------------------------------
    def _apply_sleep_wake(
        self,
        run: DailyScheduleRun,
        block: SlotInstance,
        now_hour: float,
        new_title: str,
        monologue: str,
    ) -> None:
        """Carve a 0.5h ``IDLE_ROAM`` waking window out of the sleeping block.

        Segments that would be shorter than 0.5h are merged into the waking window so
        the ``0.0 -> 24.0`` timeline stays seamless and grid-aligned.
        """
        index = self._find_slot_index(run, block)
        start = _snap_hour(getattr(block, "start_hour", 0.0))
        end = _snap_hour(getattr(block, "end_hour", start + MIN_BLOCK_HOURS))

        awake_start = _clamp(
            _snap_hour(now_hour), start, end - MIN_BLOCK_HOURS
        )
        awake_end = min(end, awake_start + AWAKE_BLOCK_HOURS)

        awake_block_start = (
            start if (awake_start - start) < MIN_BLOCK_HOURS else awake_start
        )
        awake_block_end = (
            end if (end - awake_end) < MIN_BLOCK_HOURS else awake_end
        )

        if index < 0:
            # Not part of the timeline (defensive / legacy stub): mutate in place only.
            self._convert_to_idle_roam(block, new_title, monologue)
            return

        original_title = self._slot_title(block)
        original_card_id = str(getattr(block, "card_id", "evt_default") or "evt_default")
        # v2.6: remaining sleep segments keep the fixed SLEEP tier (80); the carved
        # 0.5h waking window is IDLE_ROAM (25, applied by ``_convert_to_idle_roam``).
        sleep_armor = ActionType.SLEEP.fixed_armor

        replacement: list[SlotInstance] = []
        if awake_block_start > start:
            replacement.append(
                SlotInstance(
                    slot_key=f"block_{index}",
                    start_hour=start,
                    end_hour=awake_block_start,
                    state=BotState.RESTING,
                    card_id=original_card_id,
                    title=original_title,
                    label=ActionType.SLEEP.label,
                    action_type=ActionType.SLEEP,
                    armor=sleep_armor,
                )
            )

        # 被吵醒的 0.5h 小窗口：复用原块对象，改为 IDLE_ROAM
        block.start_hour = awake_block_start
        block.end_hour = awake_block_end
        block.reset_busy()
        self._convert_to_idle_roam(block, new_title, monologue)
        replacement.append(block)

        if awake_block_end < end:
            replacement.append(
                SlotInstance(
                    slot_key=f"block_{index + 1}",
                    start_hour=awake_block_end,
                    end_hour=end,
                    state=BotState.RESTING,
                    card_id=original_card_id,
                    title=original_title,
                    label=ActionType.SLEEP.label,
                    action_type=ActionType.SLEEP,
                    armor=sleep_armor,
                )
            )

        run.slots[index : index + 1] = replacement

    # ------------------------------------------------------------------
    # Branch C: SURF / IDLE_ROAM extension ("再加钟半小时")
    # ------------------------------------------------------------------
    def _apply_idle_extension(
        self,
        run: DailyScheduleRun,
        block: SlotInstance,
        new_title: str,
        monologue: str,
    ) -> None:
        """Extend the idle block by 0.5h and push (or swallow) the following block."""
        block.mutated = True
        if new_title:
            block.title = new_title
        block.mutation_monologue = monologue
        block.delay_minutes = 0
        # v2.6: the extended block keeps the fixed armor tier of its action type.
        block.armor = self._slot_action(block).fixed_armor

        index = self._find_slot_index(run, block)
        if index < 0:
            return  # not part of the timeline (defensive): cosmetic mutation only

        slots = run.slots
        if index >= len(slots) - 1:
            # The last block already ends at 24:00; nothing to extend into.
            return

        start = _snap_hour(getattr(block, "start_hour", 0.0))
        end = _snap_hour(getattr(block, "end_hour", start + MIN_BLOCK_HOURS))
        new_end = _clamp(_snap_hour(end + EXTEND_HOURS), start + MIN_BLOCK_HOURS, 24.0)
        if (new_end - end) < MIN_BLOCK_HOURS:
            return  # already at the 24:00 boundary

        next_block = slots[index + 1]
        next_start = _snap_hour(getattr(next_block, "start_hour", new_end))
        next_end = _snap_hour(getattr(next_block, "end_hour", next_start + MIN_BLOCK_HOURS))
        next_duration = next_end - next_start

        block.end_hour = new_end
        if (next_duration - EXTEND_HOURS) < MIN_BLOCK_HOURS:
            # Next block would become shorter than 0.5h -> swallow it entirely.
            block.end_hour = next_end
            del slots[index + 1]
        else:
            next_block.start_hour = new_end

    # ==========================================================================
    # Shared helpers
    # ==========================================================================
    async def _settle_busy_block(
        self,
        soul: SoulProfile,
        run: DailyScheduleRun,
        block: SlotInstance,
        progress_pct: int,
        interrupted_by: str,
    ) -> None:
        """Invoke the injected ``busy_settler`` for a BUSY_TASK block (best effort).

        The settler (``main.py::settle_busy_block``) writes ``completion_result`` /
        ``busy_settled=True`` / saves the run / stores a Tier 1 memory. A settler
        failure is downgraded to a warning so the timeline mutation still succeeds
        (the lazy ``_settle_finished_busy_blocks`` sweep can settle it later).
        """
        settler = self.busy_settler
        if not callable(settler):
            return
        try:
            result = settler(soul, run, block, int(progress_pct), interrupted_by)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(f"[OnlyYou] busy_settler failed (degraded): {exc!r}")

    @staticmethod
    def _convert_to_idle_roam(
        block: SlotInstance, new_title: str, monologue: str, keep_busy_result: bool = False
    ) -> None:
        """Flip a block into ``IDLE_ROAM`` (state/title/label/armor/mutated bookkeeping)."""
        block.action_type = ActionType.IDLE_ROAM
        block.state = BotState.IDLE
        block.label = ActionType.IDLE_ROAM.label
        # v2.6: the mutated block takes the fixed IDLE_ROAM armor tier (25).
        block.armor = ActionType.IDLE_ROAM.fixed_armor
        if new_title:
            block.title = new_title
        block.mutated = True
        block.mutation_monologue = monologue
        block.delay_minutes = 0
        if not keep_busy_result:
            try:
                block.reset_busy()
            except AttributeError:  # pragma: no cover - stub tolerance
                block.busy_settled = False
                block.completion_result = ""

    @staticmethod
    def _progress_pct(block: Any, now_dt: datetime) -> int:
        """Live progress percentage of ``block`` at ``now_dt`` (0 -> 100)."""
        getter = getattr(block, "get_progress_pct", None)
        if callable(getter):
            try:
                return int(_clamp(int(getter(now_dt)), 0, 100))
            except Exception:  # pragma: no cover - stub tolerance
                pass
        start = _snap_hour(getattr(block, "start_hour", 0.0))
        end = _snap_hour(getattr(block, "end_hour", start + MIN_BLOCK_HOURS))
        now_hour = now_dt.hour + now_dt.minute / 60.0 + now_dt.second / 3600.0
        if now_hour <= start:
            return 0
        if now_hour >= end:
            return 100
        span = max(MIN_BLOCK_HOURS, end - start)
        return int(_clamp(round(100.0 * (now_hour - start) / span), 0, 100))

    @staticmethod
    def _find_slot_index(run: DailyScheduleRun, slot: Any) -> int:
        """Index of ``slot`` inside ``run.slots`` (identity first, then ``slot_key``)."""
        slots = getattr(run, "slots", None) or []
        for position, candidate in enumerate(slots):
            if candidate is slot:
                return position
        slot_key = getattr(slot, "slot_key", None)
        if slot_key:
            for position, candidate in enumerate(slots):
                if getattr(candidate, "slot_key", None) == slot_key:
                    return position
        return -1

    @staticmethod
    def _reindex_slots(run: DailyScheduleRun) -> None:
        """Re-establish the timeline invariants after a mutation.

        Sorts blocks by start hour, repairs float drift into a contiguous 0.5h grid,
        renumbers ``slot_key`` as ``block_{i}`` and pins the last block to ``24.0``.
        """
        slots = [slot for slot in (getattr(run, "slots", None) or []) if slot is not None]
        slots.sort(key=lambda slot: _snap_hour(getattr(slot, "start_hour", 0.0)))

        previous_end = 0.0
        for position, slot in enumerate(slots):
            start = 0.0 if position == 0 else previous_end
            end = _snap_hour(
                getattr(slot, "end_hour", start + MIN_BLOCK_HOURS),
                start + MIN_BLOCK_HOURS,
            )
            if end <= start:
                end = min(24.0, start + MIN_BLOCK_HOURS)
            slot.start_hour = start
            slot.end_hour = end
            slot.slot_key = f"block_{position}"
            # Keep the display label in sync with the (possibly mutated) action type.
            try:
                slot.label = slot.action.label
            except AttributeError:  # pragma: no cover - stub tolerance
                pass
            # v2.6: every block's guard armor follows its action type's fixed tier,
            # so a mutation can never leave a stale hand-tuned armor value behind.
            try:
                slot.armor = BoundedScheduleMutator._slot_action(slot).fixed_armor
            except (AttributeError, TypeError):  # pragma: no cover - stub tolerance
                pass
            previous_end = end

        if slots:
            slots[-1].end_hour = 24.0
        run.slots = slots
