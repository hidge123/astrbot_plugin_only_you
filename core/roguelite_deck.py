"""Roguelite daily random schedule engine (DayVigor + 6 Slot StateEventCards + EncounterCards)."""

import json
import random
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Optional

from .state_machine import BotState
from ..storage.db import DatabaseManager


# Six canonical daily time slots
SLOT_DEFINITIONS: list[dict[str, Any]] = [
    {"slot_key": "morning", "label": "清晨早起", "start_hour": 7, "end_hour": 9},
    {"slot_key": "forenoon", "label": "上午正事", "start_hour": 9, "end_hour": 12},
    {"slot_key": "noon", "label": "午休干饭", "start_hour": 12, "end_hour": 14},
    {"slot_key": "afternoon", "label": "下午时光", "start_hour": 14, "end_hour": 18},
    {"slot_key": "evening", "label": "晚间黄金档", "start_hour": 18, "end_hour": 23},
    {"slot_key": "night", "label": "深夜睡眠", "start_hour": 23, "end_hour": 7},
]


@dataclass
class DayVigorCard:
    """① 今日基调卡 (Day Vigor Card)"""

    vigor_id: str
    title: str
    description: str
    tag: str
    state_bias: dict[str, float] = field(default_factory=dict)


@dataclass
class StateEventCard:
    """② 时间槽事件状态卡 (exact definition from docs/ARCHITECTURE.md Section 3.1)"""

    card_id: str
    title: str
    state: BotState
    valid_slots: list[str]
    base_weight: int = 50
    armor: int = 50
    synergy_tags: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "card_id": self.card_id,
            "title": self.title,
            "state": self.state.value if isinstance(self.state, BotState) else str(self.state),
            "valid_slots": list(self.valid_slots),
            "base_weight": self.base_weight,
            "armor": self.armor,
            "synergy_tags": dict(self.synergy_tags),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StateEventCard":
        raw_state = str(data.get("state", "IDLE")).upper()
        state = BotState(raw_state) if raw_state in BotState.__members__ else BotState.IDLE
        return cls(
            card_id=str(data.get("card_id", f"evt_{random.randint(1000, 9999)}")),
            title=str(data.get("title", "日常发呆摸鱼")),
            state=state,
            valid_slots=list(data.get("valid_slots") or ["noon", "afternoon", "evening"]),
            base_weight=int(data.get("base_weight", 50)),
            armor=int(data.get("armor", 45)),
            synergy_tags=dict(data.get("synergy_tags") or {}),
        )


@dataclass
class EncounterCard:
    """③ 奇遇谈资卡（不改变状态，仅作为当天短时记忆提供聊天谈资）"""

    encounter_id: str
    content: str
    used: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EncounterCard":
        return cls(
            encounter_id=str(data.get("encounter_id", "enc_0")),
            content=str(data.get("content", "")),
            used=bool(data.get("used", False)),
        )


@dataclass
class SlotInstance:
    """Concrete slot assignment in today's timeline."""

    slot_key: str
    label: str
    start_hour: int
    end_hour: int
    card_id: str
    title: str
    state: BotState
    armor: int
    mutated: bool = False
    mutation_monologue: str = ""
    delay_minutes: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "slot_key": self.slot_key,
            "label": self.label,
            "start_hour": self.start_hour,
            "end_hour": self.end_hour,
            "card_id": self.card_id,
            "title": self.title,
            "state": self.state.value if isinstance(self.state, BotState) else str(self.state),
            "armor": self.armor,
            "mutated": self.mutated,
            "mutation_monologue": self.mutation_monologue,
            "delay_minutes": self.delay_minutes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SlotInstance":
        raw_state = str(data.get("state", "IDLE")).upper()
        state = BotState(raw_state) if raw_state in BotState.__members__ else BotState.IDLE
        return cls(
            slot_key=str(data["slot_key"]),
            label=str(data.get("label", data["slot_key"])),
            start_hour=int(data.get("start_hour", 0)),
            end_hour=int(data.get("end_hour", 24)),
            card_id=str(data.get("card_id", "evt_default")),
            title=str(data.get("title", "日常摸鱼")),
            state=state,
            armor=int(data.get("armor", 45)),
            mutated=bool(data.get("mutated", False)),
            mutation_monologue=str(data.get("mutation_monologue", "")),
            delay_minutes=int(data.get("delay_minutes", 0)),
        )


@dataclass
class DailyScheduleRun:
    """Today's complete Roguelite schedule run for a specific Soul."""

    soul_id: str
    date_str: str
    vigor_id: str
    vigor_title: str
    vigor_desc: str
    slots: list[SlotInstance]
    encounters: list[EncounterCard]
    mutations_used: int = 0
    last_mutation_ts: float = 0.0
    last_mutation_log: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "soul_id": self.soul_id,
            "date_str": self.date_str,
            "vigor_id": self.vigor_id,
            "vigor_title": self.vigor_title,
            "vigor_desc": self.vigor_desc,
            "slots": [s.to_dict() for s in self.slots],
            "encounters": [e.to_dict() for e in self.encounters],
            "mutations_used": self.mutations_used,
            "last_mutation_ts": self.last_mutation_ts,
            "last_mutation_log": self.last_mutation_log,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DailyScheduleRun":
        return cls(
            soul_id=str(data["soul_id"]),
            date_str=str(data["date_str"]),
            vigor_id=str(data.get("vigor_id", "vigor_normal")),
            vigor_title=str(data.get("vigor_title", "元气满满")),
            vigor_desc=str(data.get("vigor_desc", "作息平稳的普通一天")),
            slots=[SlotInstance.from_dict(s) for s in data.get("slots", [])],
            encounters=[EncounterCard.from_dict(e) for e in data.get("encounters", [])],
            mutations_used=int(data.get("mutations_used", 0)),
            last_mutation_ts=float(data.get("last_mutation_ts", 0.0)),
            last_mutation_log=str(data.get("last_mutation_log", "")),
        )


# Built-in Day Vigor cards
DEFAULT_VIGOR_CARDS: list[DayVigorCard] = [
    DayVigorCard(
        vigor_id="vigor_energetic",
        title="满血冲浪日",
        description="今天精神头超足，遇到什么事都想插两句嘴，闲下来就猛刷手机",
        tag="energetic",
        state_bias={"IDLE": 1.4, "BUSY": 0.9, "RESTING": 0.8},
    ),
    DayVigorCard(
        vigor_id="vigor_deadline",
        title="苦逼赶工日",
        description="今天正事一堆，白天大多在忙，晚上才终于能喘口气摸鱼",
        tag="deadline",
        state_bias={"BUSY": 1.5, "IDLE": 0.85, "RESTING": 1.0},
    ),
    DayVigorCard(
        vigor_id="vigor_slacker",
        title="咸鱼摆烂日",
        description="浑身懒洋洋不想干正事，只想瘫着吃零食刷视频水群",
        tag="slacker",
        state_bias={"IDLE": 1.6, "BUSY": 0.6, "RESTING": 1.2},
    ),
    DayVigorCard(
        vigor_id="vigor_night_owl",
        title="夜猫修仙日",
        description="早上困得睁不开眼，越到晚上越精神",
        tag="night_owl",
        state_bias={"RESTING": 1.2, "IDLE": 1.3, "BUSY": 0.9},
    ),
]

# Built-in general StateEventCards covering all 6 slots & 3 states
DEFAULT_STATE_EVENT_CARDS: list[StateEventCard] = [
    StateEventCard(
        card_id="gen_morning_sleep",
        title="赖床裹紧小被子补觉",
        state=BotState.RESTING,
        valid_slots=["morning"],
        base_weight=55,
        armor=65,
        synergy_tags={"night_owl": 1.8, "slacker": 1.5},
    ),
    StateEventCard(
        card_id="gen_morning_commute",
        title="叼着面包赶早八通勤刷手机",
        state=BotState.IDLE,
        valid_slots=["morning"],
        base_weight=45,
        armor=35,
        synergy_tags={"energetic": 1.5},
    ),
    StateEventCard(
        card_id="gen_forenoon_work",
        title="沉浸式处理手头正事与学习",
        state=BotState.BUSY,
        valid_slots=["forenoon", "afternoon"],
        base_weight=65,
        armor=60,
        synergy_tags={"deadline": 1.7},
    ),
    StateEventCard(
        card_id="gen_forenoon_slack",
        title="表面正经干活实则偷偷刷手机摸鱼",
        state=BotState.IDLE,
        valid_slots=["forenoon", "afternoon"],
        base_weight=45,
        armor=30,
        synergy_tags={"slacker": 1.7, "energetic": 1.3},
    ),
    StateEventCard(
        card_id="gen_noon_eat",
        title="点外卖边干饭边刷下饭视频",
        state=BotState.IDLE,
        valid_slots=["noon"],
        base_weight=75,
        armor=25,
        synergy_tags={"energetic": 1.4, "slacker": 1.4},
    ),
    StateEventCard(
        card_id="gen_noon_nap",
        title="戴上眼罩趴桌上午休小憩",
        state=BotState.RESTING,
        valid_slots=["noon"],
        base_weight=35,
        armor=60,
        synergy_tags={"deadline": 1.3},
    ),
    StateEventCard(
        card_id="gen_afternoon_focus",
        title="跟烦人的任务死磕赶进度",
        state=BotState.BUSY,
        valid_slots=["afternoon"],
        base_weight=55,
        armor=55,
        synergy_tags={"deadline": 1.8},
    ),
    StateEventCard(
        card_id="gen_afternoon_tea",
        title="喝下午茶瘫在椅子上逛B站水群",
        state=BotState.IDLE,
        valid_slots=["afternoon", "evening"],
        base_weight=55,
        armor=30,
        synergy_tags={"slacker": 1.6, "energetic": 1.4},
    ),
    StateEventCard(
        card_id="gen_evening_surf",
        title="躺沙发上快乐刷视频吹水",
        state=BotState.IDLE,
        valid_slots=["evening"],
        base_weight=80,
        armor=25,
        synergy_tags={"energetic": 1.5, "slacker": 1.5, "night_owl": 1.5},
    ),
    StateEventCard(
        card_id="gen_evening_rank",
        title="专注连麦打排位冲分",
        state=BotState.BUSY,
        valid_slots=["evening"],
        base_weight=30,
        armor=50,
    ),
    StateEventCard(
        card_id="gen_night_sleep",
        title="关灯钻进被窝呼呼大睡",
        state=BotState.RESTING,
        valid_slots=["night"],
        base_weight=75,
        armor=70,
    ),
    StateEventCard(
        card_id="gen_night_emo_surf",
        title="躲被窝里熬夜刷手机舍不得睡",
        state=BotState.IDLE,
        valid_slots=["night"],
        base_weight=30,
        armor=35,
        synergy_tags={"night_owl": 2.2},
    ),
]

DEFAULT_ENCOUNTER_POOL: list[str] = [
    "今天中午点外卖，老板居然多送了一根烤肠，运气爆棚！",
    "早上下楼的时候差点被一只胖得像煤气罐的流浪橘猫绊倒",
    "刚才买奶茶店员手抖多加了双倍小料，差点吸不动",
    "今天路过便利店听到店里在放超怀旧的老歌，一下子愣住了",
    "刚才看到窗外有朵云长得特别像一只翻肚皮的修狗",
]


class RogueliteDeckEngine:
    """Builds and manages the daily 6-slot Roguelite schedule and Encounter cards."""

    def __init__(self, db: DatabaseManager, plugin_config: dict[str, Any]):
        self.db = db
        self.plugin_config = plugin_config
        self._cache: dict[tuple[str, str], DailyScheduleRun] = {}

    @property
    def cfg(self) -> dict[str, Any]:
        return self.plugin_config.get("roguelite_schedule_config", {}) or {}

    @staticmethod
    def today_str(now_dt: Optional[datetime] = None) -> str:
        dt = now_dt or datetime.now()
        return dt.strftime("%Y-%m-%d")

    async def get_or_build_today_run(
        self,
        soul_id: str,
        custom_cards: Optional[list[StateEventCard]] = None,
        custom_encounters: Optional[list[str]] = None,
        force_reroll: bool = False,
        now_dt: Optional[datetime] = None,
    ) -> DailyScheduleRun:
        """Load today's schedule from cache/SQLite or draw a new Roguelite run."""
        date_str = self.today_str(now_dt)
        cache_key = (soul_id, date_str)

        if not force_reroll and cache_key in self._cache:
            return self._cache[cache_key]

        if not force_reroll:
            row = await self.db.fetchone(
                "SELECT run_json FROM daily_schedules WHERE soul_id = ? AND date_str = ?",
                (soul_id, date_str),
            )
            if row and row.get("run_json"):
                try:
                    run = DailyScheduleRun.from_dict(json.loads(row["run_json"]))
                    self._cache[cache_key] = run
                    return run
                except Exception:
                    pass

        run = self._draw_new_run(
            soul_id=soul_id,
            date_str=date_str,
            custom_cards=custom_cards,
            custom_encounters=custom_encounters,
        )
        await self.save_run(run)
        return run

    async def save_run(self, run: DailyScheduleRun) -> None:
        """Persist DailyScheduleRun to SQLite and memory cache."""
        cache_key = (run.soul_id, run.date_str)
        self._cache[cache_key] = run
        now_ts = int(time.time())
        await self.db.execute(
            """
            INSERT INTO daily_schedules (soul_id, date_str, run_json, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(soul_id, date_str) DO UPDATE SET
                run_json = excluded.run_json,
                updated_at = excluded.updated_at
            """,
            (run.soul_id, run.date_str, json.dumps(run.to_dict(), ensure_ascii=False), now_ts),
        )

    def _draw_new_run(
        self,
        soul_id: str,
        date_str: str,
        custom_cards: Optional[list[StateEventCard]] = None,
        custom_encounters: Optional[list[str]] = None,
    ) -> DailyScheduleRun:
        """Execute weighted Roguelite draw with DayVigor + Synergy + Encounter cards."""
        enable_roguelite = bool(self.cfg.get("enable_roguelite_schedule", True))
        vigor = random.choice(DEFAULT_VIGOR_CARDS)

        # Merge general pool and persona-specific custom pool
        card_pool: list[StateEventCard] = list(DEFAULT_STATE_EVENT_CARDS)
        if custom_cards:
            card_pool.extend(custom_cards)

        slots: list[SlotInstance] = []
        prev_state: Optional[BotState] = None

        for slot_def in SLOT_DEFINITIONS:
            slot_key = slot_def["slot_key"]
            if not enable_roguelite:
                # Static fallback when Roguelite schedule is disabled
                default_state = BotState.RESTING if slot_key == "night" else BotState.IDLE
                slots.append(
                    SlotInstance(
                        slot_key=slot_key,
                        label=slot_def["label"],
                        start_hour=slot_def["start_hour"],
                        end_hour=slot_def["end_hour"],
                        card_id=f"static_{slot_key}",
                        title="日常在线随时闲聊" if default_state == BotState.IDLE else "夜间休息",
                        state=default_state,
                        armor=50,
                    )
                )
                continue

            candidates = [c for c in card_pool if slot_key in c.valid_slots]
            if not candidates:
                candidates = list(DEFAULT_STATE_EVENT_CARDS)

            weights: list[float] = []
            for card in candidates:
                w = float(max(5, card.base_weight))
                # 1. Apply DayVigor state bias
                w *= vigor.state_bias.get(card.state.value, 1.0)
                # 2. Apply card synergy tags with today's vigor tag
                w *= card.synergy_tags.get(vigor.tag, 1.0)
                # 3. Anti-fatigue synergy: if previous slot was BUSY, boost IDLE weight in next slot
                if prev_state == BotState.BUSY and card.state == BotState.IDLE:
                    w *= 1.45
                # Avoid two consecutive RESTING slots during daytime
                if prev_state == BotState.RESTING and card.state == BotState.RESTING and slot_key != "night":
                    w *= 0.25
                weights.append(max(1.0, w))

            chosen = random.choices(candidates, weights=weights, k=1)[0]
            prev_state = chosen.state

            slots.append(
                SlotInstance(
                    slot_key=slot_key,
                    label=slot_def["label"],
                    start_hour=slot_def["start_hour"],
                    end_hour=slot_def["end_hour"],
                    card_id=chosen.card_id,
                    title=chosen.title,
                    state=chosen.state,
                    armor=chosen.armor,
                )
            )

        # Draw Encounter talk cards
        encounter_count = max(0, int(self.cfg.get("daily_encounter_count", 2)))
        encounter_texts = list(DEFAULT_ENCOUNTER_POOL)
        if custom_encounters:
            for item in custom_encounters:
                if item and item not in encounter_texts:
                    encounter_texts.insert(0, item)

        chosen_encounters = random.sample(
            encounter_texts, k=min(encounter_count, len(encounter_texts))
        )
        encounters = [
            EncounterCard(encounter_id=f"enc_{idx + 1}", content=txt, used=False)
            for idx, txt in enumerate(chosen_encounters)
        ]

        return DailyScheduleRun(
            soul_id=soul_id,
            date_str=date_str,
            vigor_id=vigor.vigor_id,
            vigor_title=vigor.title,
            vigor_desc=vigor.description,
            slots=slots,
            encounters=encounters,
        )

    @staticmethod
    def resolve_slot_index_for_time(
        run: DailyScheduleRun, now_dt: Optional[datetime] = None
    ) -> int:
        """
        Resolve which slot index in `run.slots` applies at `now_dt`,
        accounting for `delay_minutes` on the next slot if a mutation delayed it!
        """
        dt = now_dt or datetime.now()
        hour = dt.hour
        minute = dt.minute

        base_idx = 0
        for idx, slot in enumerate(run.slots):
            if slot.start_hour < slot.end_hour:
                if slot.start_hour <= hour < slot.end_hour:
                    base_idx = idx
                    break
            else:
                # Overnight slot (e.g. 23 -> 7)
                if hour >= slot.start_hour or hour < slot.end_hour:
                    base_idx = idx
                    break

        # Check if current base slot was delayed by previous slot's mutation ("再刷亿会儿 / 热聊熬夜")
        cur_slot = run.slots[base_idx]
        if cur_slot.delay_minutes > 0 and hour == cur_slot.start_hour and minute < cur_slot.delay_minutes:
            prev_idx = (base_idx - 1) % len(run.slots)
            return prev_idx

        return base_idx

    async def get_current_and_next_slot(
        self,
        soul_id: str,
        custom_cards: Optional[list[StateEventCard]] = None,
        custom_encounters: Optional[list[str]] = None,
        now_dt: Optional[datetime] = None,
    ) -> tuple[DailyScheduleRun, SlotInstance, SlotInstance]:
        """Return `(run, current_slot, next_slot)` for `soul_id`."""
        run = await self.get_or_build_today_run(
            soul_id=soul_id,
            custom_cards=custom_cards,
            custom_encounters=custom_encounters,
            now_dt=now_dt,
        )
        idx = self.resolve_slot_index_for_time(run, now_dt=now_dt)
        current_slot = run.slots[idx]
        next_slot = run.slots[(idx + 1) % len(run.slots)]
        return run, current_slot, next_slot

    async def get_current_slot(
        self,
        soul_id: str,
        custom_cards: Optional[list[StateEventCard]] = None,
        custom_encounters: Optional[list[str]] = None,
        now_dt: Optional[datetime] = None,
    ) -> SlotInstance:
        """Convenience method returning `current_slot` for `soul_id`."""
        _, current_slot, _ = await self.get_current_and_next_slot(
            soul_id=soul_id,
            custom_cards=custom_cards,
            custom_encounters=custom_encounters,
            now_dt=now_dt,
        )
        return current_slot

    async def force_set_current_state(
        self,
        soul_id: str,
        new_state: BotState,
        new_title: Optional[str] = None,
        armor: Optional[int] = None,
        now_dt: Optional[datetime] = None,
    ) -> SlotInstance:
        """Manually override the current slot's state & title (for debugging/commands)."""
        run, current_slot, _ = await self.get_current_and_next_slot(
            soul_id=soul_id, now_dt=now_dt
        )
        current_slot.state = new_state
        if new_title:
            current_slot.title = new_title
        if armor is not None:
            current_slot.armor = armor
        current_slot.mutated = False
        current_slot.mutation_monologue = ""
        await self.save_run(run)
        return current_slot
