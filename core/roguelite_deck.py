"""Roguelite 24h dynamic action-card timeline engine (core/roguelite_deck.py).

A ``DailyScheduleRun`` is now built by stitching the Soul's *four action-type card
pool* (``SLEEP`` / ``SURF`` / ``IDLE_ROAM`` / ``BUSY_TASK``) into a contiguous
``0.0 -> 24.0`` hour timeline of elastic ``SlotInstance`` blocks (0.5h grid), driven
by the character's ``rhythm.chronotype`` curve, today's DayVigor card and adjacent
card synergy. The legacy encounter-talk-card system is removed: real talk material
now comes from BUSY_TASK completion results / watched videos / idle-roam memories.
"""

import json
import random
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Optional

from .state_machine import ActionType, BotState
from ..storage.db import DatabaseManager

HOUR_GRID = 0.5
MIN_BLOCK_HOURS = 0.5
MAX_ROLL_HOURS = 8.0
MIN_TIME_BLOCKS = 7
MAX_TIME_BLOCKS = 11
CHRONOTYPES = ("early", "normal", "night_owl")
DEFAULT_CHRONOTYPE = "normal"

# Legacy fixed 6-slot definitions. Kept only for backward-compatible imports
# (old WebUI payloads / third-party integrations); the timeline engine no longer
# uses them.
SLOT_DEFINITIONS: list[dict[str, Any]] = [
    {"slot_key": "morning", "label": "清晨早起", "start_hour": 7, "end_hour": 9},
    {"slot_key": "forenoon", "label": "上午正事", "start_hour": 9, "end_hour": 12},
    {"slot_key": "noon", "label": "午休干饭", "start_hour": 12, "end_hour": 14},
    {"slot_key": "afternoon", "label": "下午时光", "start_hour": 14, "end_hour": 18},
    {"slot_key": "evening", "label": "晚间黄金档", "start_hour": 18, "end_hour": 23},
    {"slot_key": "night", "label": "深夜睡眠", "start_hour": 23, "end_hour": 7},
]


# ===========================================================================
# Helpers
# ===========================================================================
def snap_hour(value: Any, default: float = 0.0) -> float:
    """Snap an hour value onto the 0.5h grid."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    return round(number * 2) / 2


def clamp_hours(value: Any, default: float = 1.0) -> float:
    """Clamp a duration onto the [0.5, 8.0] hours 0.5h grid."""
    snapped = snap_hour(value, default)
    return max(MIN_BLOCK_HOURS, min(MAX_ROLL_HOURS, snapped))


def clamp_hour_of_day(value: Any, default: float = 0.0) -> float:
    """Clamp an absolute hour-of-day onto the [0.0, 24.0] 0.5h grid."""
    snapped = snap_hour(value, default)
    return max(0.0, min(24.0, snapped))


def normalize_chronotype(value: Any) -> str:
    """Return one of ``early`` / ``normal`` / ``night_owl`` (default ``normal``)."""
    raw = str(value or "").strip().lower()
    if raw in CHRONOTYPES:
        return raw
    if raw in ("morning", "lark", "early_bird"):
        return "early"
    if raw in ("night", "owl", "nightowl", "night-owl"):
        return "night_owl"
    return DEFAULT_CHRONOTYPE


def format_hour(hour: Any) -> str:
    """Format a float hour-of-day as ``HH:MM`` (``13.5 -> "13:30"``, ``24 -> "24:00"``)."""
    total_minutes = int(round(float(hour or 0) * 60))
    total_minutes = max(0, min(24 * 60, total_minutes))
    return f"{total_minutes // 60:02d}:{total_minutes % 60:02d}"


_SURF_TITLE_KEYWORDS = ("刷", "B站", "b站", "抖音", "视频", "冲浪", "追剧", "看剧")
_NAP_TITLE_KEYWORDS = ("午", "小憩", "打个盹", "趴", "nap", "眯")


def _derive_action_type(state: BotState, title: str = "") -> ActionType:
    """Infer an ``ActionType`` from a legacy card's state + title (migration rule)."""
    if state == BotState.RESTING:
        return ActionType.SLEEP
    if state == BotState.BUSY:
        return ActionType.BUSY_TASK
    text = str(title or "")
    if any(keyword in text for keyword in _SURF_TITLE_KEYWORDS):
        return ActionType.SURF
    return ActionType.IDLE_ROAM


def default_hour_range(action_type: ActionType, title: str = "") -> tuple[float, float]:
    """Default ``[min_hours, max_hours]`` range for a migrated action card."""
    if action_type == ActionType.SLEEP:
        if any(keyword in str(title or "") for keyword in _NAP_TITLE_KEYWORDS):
            return 1.0, 2.0
        return 4.0, 7.5
    if action_type == ActionType.BUSY_TASK:
        return 1.0, 3.0
    return 0.5, 2.0


def _coerce_state(value: Any, default: BotState = BotState.IDLE) -> BotState:
    if isinstance(value, BotState):
        return value
    raw = str(value or "").strip().upper()
    if raw.startswith("BOTSTATE."):
        raw = raw.split(".", 1)[1]
    if raw in BotState.__members__:
        return BotState(raw)
    return default


# ===========================================================================
# Data model
# ===========================================================================
@dataclass
class DayVigorCard:
    """① 今日基调卡 (Day Vigor Card)"""

    vigor_id: str
    title: str
    description: str
    tag: str
    state_bias: dict[str, float] = field(default_factory=dict)
    # Deprecated since v2.6: armor is a fixed tier per ``ActionType``, so the
    # vigor offset is kept only for old serialized data and no longer applied.
    armor_offset: int = 0


@dataclass
class StateEventCard:
    """② 四类行动卡定义（兼容旧名 ``StateEventCard``）。

    New fields ``action_type`` / ``min_hours`` / ``max_hours`` drive the dynamic
    stitched timeline; ``valid_slots`` is kept only so legacy serialized decks keep
    deserializing (the engine no longer reads it).
    """

    card_id: str
    title: str
    state: BotState = BotState.IDLE
    valid_slots: list[str] = field(default_factory=list)
    base_weight: int = 50
    # v2.6: ``None`` means "use the fixed armor tier of ``action_type``".  An
    # explicit value is still honored here for old persisted data / test helpers,
    # but every business entry point (deck draw / force_state / cards save)
    # overwrites it with ``action.fixed_armor``.
    armor: Optional[int] = None
    synergy_tags: dict[str, float] = field(default_factory=dict)
    action_type: Optional[ActionType] = None
    min_hours: float = 1.0
    max_hours: float = 2.5

    def __post_init__(self) -> None:
        self.state = _coerce_state(self.state, BotState.IDLE)
        derived = ActionType.coerce(self.action_type, None)
        if derived is None:
            derived = _derive_action_type(self.state, self.title)
        self.action_type = derived
        self.state = derived.bound_state
        self.min_hours = clamp_hours(self.min_hours, 1.0)
        self.max_hours = clamp_hours(self.max_hours, self.min_hours)
        if self.max_hours < self.min_hours:
            self.max_hours = self.min_hours
        self.base_weight = max(1, min(100, int(self.base_weight or 50)))
        if self.armor is None:
            self.armor = derived.fixed_armor
        else:
            self.armor = max(0, min(100, int(self.armor)))
        self.valid_slots = [str(item) for item in (self.valid_slots or []) if str(item).strip()]
        self.synergy_tags = {
            str(key): float(value) for key, value in (self.synergy_tags or {}).items()
        }

    @property
    def action(self) -> ActionType:
        return self.action_type or ActionType.IDLE_ROAM

    def to_dict(self) -> dict[str, Any]:
        return {
            "card_id": self.card_id,
            "title": self.title,
            "state": self.state.value,
            "action_type": self.action.value,
            "min_hours": float(self.min_hours),
            "max_hours": float(self.max_hours),
            "valid_slots": list(self.valid_slots),
            "base_weight": int(self.base_weight),
            "armor": int(self.armor),
            "synergy_tags": dict(self.synergy_tags),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StateEventCard":
        data = data if isinstance(data, dict) else {}
        state = _coerce_state(data.get("state"), BotState.IDLE)
        title = str(data.get("title", "日常发呆摸鱼"))
        raw_action = ActionType.coerce(data.get("action_type"), None)
        action = raw_action if raw_action is not None else _derive_action_type(state, title)
        default_min, default_max = default_hour_range(action, title)
        try:
            min_hours = clamp_hours(data.get("min_hours", default_min), default_min)
        except (TypeError, ValueError):
            min_hours = default_min
        try:
            max_hours = clamp_hours(data.get("max_hours", default_max), default_max)
        except (TypeError, ValueError):
            max_hours = default_max
        if max_hours < min_hours:
            max_hours = min_hours
        kwargs: dict[str, Any] = {
            "card_id": str(data.get("card_id", f"evt_{random.randint(1000, 9999)}")),
            "title": title,
            "state": action.bound_state,
            "valid_slots": list(data.get("valid_slots") or []),
            "base_weight": int(data.get("base_weight", 50) or 50),
            "synergy_tags": dict(data.get("synergy_tags") or {}),
            "action_type": action,
            "min_hours": min_hours,
            "max_hours": max_hours,
        }
        # v2.6: only keep an explicitly provided armor value (legacy cards / tests);
        # missing / None / blank -> leave it to ``__post_init__`` (fixed tier).
        raw_armor = data.get("armor")
        if raw_armor is not None and str(raw_armor).strip() != "":
            try:
                kwargs["armor"] = int(float(raw_armor))
            except (TypeError, ValueError):
                pass
        return cls(**kwargs)


@dataclass
class EncounterCard:
    """③ 奇遇谈资卡 —— 已从产品逻辑移除，仅保留类定义兼容旧反序列化。"""

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
    """24h 动态时间轴上的单个弹性时间块（兼容旧名 ``SlotInstance``）。"""

    slot_key: str
    start_hour: float
    end_hour: float
    state: BotState
    card_id: str = "evt_default"
    title: str = "日常摸鱼"
    label: str = ""
    action_type: Optional[ActionType] = None
    armor: int = 50
    mutated: bool = False
    mutation_monologue: str = ""
    delay_minutes: int = 0
    busy_settled: bool = False
    completion_result: str = ""

    def __post_init__(self) -> None:
        self.start_hour = clamp_hour_of_day(self.start_hour, 0.0)
        self.end_hour = clamp_hour_of_day(self.end_hour, 24.0)
        if self.end_hour <= self.start_hour:
            self.end_hour = min(24.0, round((self.start_hour + MIN_BLOCK_HOURS) * 2) / 2)
        self.state = _coerce_state(self.state, BotState.IDLE)
        derived = ActionType.coerce(self.action_type, None)
        if derived is None:
            derived = _derive_action_type(self.state, self.title)
        self.action_type = derived
        self.state = derived.bound_state
        self.label = str(self.label or derived.label)
        self.armor = max(0, min(100, int(self.armor if self.armor is not None else 50)))
        self.completion_result = str(self.completion_result or "")
        self.busy_settled = bool(self.busy_settled)

    @property
    def action(self) -> ActionType:
        return self.action_type or ActionType.IDLE_ROAM

    @property
    def duration_hours(self) -> float:
        return max(HOUR_GRID, round(self.end_hour - self.start_hour, 1))

    def get_progress_pct(self, now_dt: Optional[datetime] = None) -> int:
        """Live progress percentage (0 -> 100) of this time block at ``now_dt``."""
        dt = now_dt or datetime.now()
        now_hour = dt.hour + dt.minute / 60.0 + dt.second / 3600.0
        if now_hour <= self.start_hour:
            return 0
        if now_hour >= self.end_hour:
            return 100
        span = max(HOUR_GRID, self.end_hour - self.start_hour)
        progress = round(100.0 * (now_hour - self.start_hour) / span)
        return max(0, min(100, int(progress)))

    def format_time_range(self) -> str:
        return f"{format_hour(self.start_hour)}-{format_hour(self.end_hour)}"

    def reset_busy(self) -> None:
        self.busy_settled = False
        self.completion_result = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "slot_key": self.slot_key,
            "label": self.label,
            "start_hour": float(self.start_hour),
            "end_hour": float(self.end_hour),
            "card_id": self.card_id,
            "title": self.title,
            "state": self.state.value,
            "action_type": self.action.value,
            "armor": int(self.armor),
            "mutated": bool(self.mutated),
            "mutation_monologue": self.mutation_monologue,
            "delay_minutes": int(self.delay_minutes),
            "busy_settled": bool(self.busy_settled),
            "completion_result": self.completion_result,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SlotInstance":
        data = data if isinstance(data, dict) else {}
        state = _coerce_state(data.get("state"), BotState.IDLE)
        title = str(data.get("title", "日常摸鱼"))
        raw_action = ActionType.coerce(data.get("action_type"), None)
        action = raw_action if raw_action is not None else None
        if action is not None:
            state = action.bound_state
        try:
            start_hour = clamp_hour_of_day(data.get("start_hour", 0.0), 0.0)
        except (TypeError, ValueError):
            start_hour = 0.0
        try:
            end_hour = clamp_hour_of_day(data.get("end_hour", 24.0), 24.0)
        except (TypeError, ValueError):
            end_hour = 24.0
        if end_hour <= start_hour:
            end_hour = min(24.0, round((start_hour + MIN_BLOCK_HOURS) * 2) / 2)
        return cls(
            slot_key=str(data.get("slot_key") or "block_0"),
            start_hour=start_hour,
            end_hour=end_hour,
            state=state,
            card_id=str(data.get("card_id", "evt_default")),
            title=title,
            label=str(data.get("label") or ""),
            action_type=action,
            armor=int(data.get("armor", 45) or 45),
            mutated=bool(data.get("mutated", False)),
            mutation_monologue=str(data.get("mutation_monologue", "") or ""),
            delay_minutes=int(data.get("delay_minutes", 0) or 0),
            busy_settled=bool(data.get("busy_settled", False)),
            completion_result=str(data.get("completion_result", "") or ""),
        )


@dataclass
class DailyScheduleRun:
    """Today's complete 24h dynamic timeline for a specific Soul."""

    soul_id: str
    date_str: str
    vigor_id: str
    vigor_title: str
    vigor_desc: str
    slots: list[SlotInstance]
    encounters: list[EncounterCard] = field(default_factory=list)
    mutations_used: int = 0
    last_mutation_ts: float = 0.0
    last_mutation_log: str = ""
    chronotype: str = DEFAULT_CHRONOTYPE

    def to_dict(self) -> dict[str, Any]:
        return {
            "soul_id": self.soul_id,
            "date_str": self.date_str,
            "vigor_id": self.vigor_id,
            "vigor_title": self.vigor_title,
            "vigor_desc": self.vigor_desc,
            "slots": [s.to_dict() for s in self.slots],
            # Deprecated: encounter cards were removed from the product; the empty
            # list is kept so old consumers can still deserialize the payload.
            "encounters": [],
            "mutations_used": self.mutations_used,
            "last_mutation_ts": self.last_mutation_ts,
            "last_mutation_log": self.last_mutation_log,
            "chronotype": self.chronotype,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DailyScheduleRun":
        data = data if isinstance(data, dict) else {}
        slots = [SlotInstance.from_dict(s) for s in data.get("slots", [])]
        # v2.6 integration fix: armor is a fixed per-action tier now, so a run
        # persisted by an older version is normalized on load instead of keeping
        # stale 0~100 values that would desync the mutation impact gate.
        for slot in slots:
            slot.armor = slot.action.fixed_armor
        return cls(
            soul_id=str(data["soul_id"]),
            date_str=str(data["date_str"]),
            vigor_id=str(data.get("vigor_id", "vigor_normal")),
            vigor_title=str(data.get("vigor_title", "元气满满")),
            vigor_desc=str(data.get("vigor_desc", "作息平稳的普通一天")),
            slots=slots,
            encounters=[],  # legacy encounter cards are intentionally dropped
            mutations_used=int(data.get("mutations_used", 0) or 0),
            last_mutation_ts=float(data.get("last_mutation_ts", 0.0) or 0.0),
            last_mutation_log=str(data.get("last_mutation_log", "") or ""),
            chronotype=normalize_chronotype(data.get("chronotype")),
        )

    # ---- Convenience helpers used by Web API / mutator ----
    def is_busy_task(self, slot: SlotInstance) -> bool:
        return slot.action == ActionType.BUSY_TASK

    def settled_busy_results(self) -> list[SlotInstance]:
        """All BUSY_TASK blocks of today that already produced a completion result."""
        return [
            slot
            for slot in self.slots
            if slot.action == ActionType.BUSY_TASK and slot.busy_settled
        ]


# Built-in Day Vigor cards
DEFAULT_VIGOR_CARDS: list[DayVigorCard] = [
    DayVigorCard(
        vigor_id="vigor_energetic",
        title="满血冲浪日",
        description="今天精神头超足，遇到什么事都想插两句嘴，闲下来就猛刷手机",
        tag="energetic",
        state_bias={"IDLE": 1.4, "BUSY": 0.9, "RESTING": 0.8},
        armor_offset=-5,
    ),
    DayVigorCard(
        vigor_id="vigor_deadline",
        title="苦逼赶工日",
        description="今天正事一堆，白天大多在忙，晚上才终于能喘口气摸鱼",
        tag="deadline",
        state_bias={"BUSY": 1.5, "IDLE": 0.85, "RESTING": 1.0},
        armor_offset=8,
    ),
    DayVigorCard(
        vigor_id="vigor_slacker",
        title="咸鱼摆烂日",
        description="浑身懒洋洋不想干正事，只想瘫着吃零食刷视频水群",
        tag="slacker",
        state_bias={"IDLE": 1.6, "BUSY": 0.6, "RESTING": 1.2},
        armor_offset=-8,
    ),
    DayVigorCard(
        vigor_id="vigor_night_owl",
        title="夜猫修仙日",
        description="早上困得睁不开眼，越到晚上越精神",
        tag="night_owl",
        state_bias={"RESTING": 1.2, "IDLE": 1.3, "BUSY": 0.9},
        armor_offset=0,
    ),
]

# Built-in general four-action-type card pool (used when a Soul has no custom pool).
DEFAULT_STATE_EVENT_CARDS: list[StateEventCard] = [
    # ---- SLEEP ----
    StateEventCard(
        card_id="gen_sleep_night",
        title="关灯钻进被窝呼呼大睡",
        state=BotState.RESTING,
        action_type=ActionType.SLEEP,
        min_hours=4.0,
        max_hours=7.5,
        base_weight=75,
    ),
    StateEventCard(
        card_id="gen_sleep_nap",
        title="戴上眼罩趴桌上午休小憩",
        state=BotState.RESTING,
        action_type=ActionType.SLEEP,
        min_hours=1.0,
        max_hours=2.0,
        base_weight=35,
        synergy_tags={"deadline": 1.3},
    ),
    # ---- BUSY_TASK ----
    StateEventCard(
        card_id="gen_busy_focus",
        title="沉浸式处理手头正事与学习",
        state=BotState.BUSY,
        action_type=ActionType.BUSY_TASK,
        min_hours=1.5,
        max_hours=3.0,
        base_weight=65,
        synergy_tags={"deadline": 1.7},
    ),
    StateEventCard(
        card_id="gen_busy_afternoon",
        title="跟烦人的任务死磕赶进度",
        state=BotState.BUSY,
        action_type=ActionType.BUSY_TASK,
        min_hours=1.0,
        max_hours=2.5,
        base_weight=55,
        synergy_tags={"deadline": 1.8},
    ),
    StateEventCard(
        card_id="gen_busy_rank",
        title="专注连麦打排位冲分",
        state=BotState.BUSY,
        action_type=ActionType.BUSY_TASK,
        min_hours=1.0,
        max_hours=2.0,
        base_weight=30,
    ),
    # ---- SURF ----
    StateEventCard(
        card_id="gen_surf_evening",
        title="躺沙发上快乐刷视频吹水",
        state=BotState.IDLE,
        action_type=ActionType.SURF,
        min_hours=0.5,
        max_hours=2.0,
        base_weight=80,
        synergy_tags={"energetic": 1.5, "slacker": 1.5, "night_owl": 1.5},
    ),
    StateEventCard(
        card_id="gen_surf_meal",
        title="点外卖边干饭边刷下饭视频",
        state=BotState.IDLE,
        action_type=ActionType.SURF,
        min_hours=0.5,
        max_hours=1.5,
        base_weight=55,
        synergy_tags={"energetic": 1.4, "slacker": 1.4},
    ),
    StateEventCard(
        card_id="gen_surf_night",
        title="躲被窝里熬夜刷手机舍不得睡",
        state=BotState.IDLE,
        action_type=ActionType.SURF,
        min_hours=0.5,
        max_hours=1.5,
        base_weight=30,
        synergy_tags={"night_owl": 2.2},
    ),
    # ---- IDLE_ROAM ----
    StateEventCard(
        card_id="gen_roam_commute",
        title="叼着面包赶早八通勤刷手机",
        state=BotState.IDLE,
        action_type=ActionType.IDLE_ROAM,
        min_hours=0.5,
        max_hours=1.5,
        base_weight=45,
        synergy_tags={"energetic": 1.5},
    ),
    StateEventCard(
        card_id="gen_roam_slack",
        title="表面正经干活实则偷偷摸鱼",
        state=BotState.IDLE,
        action_type=ActionType.IDLE_ROAM,
        min_hours=0.5,
        max_hours=1.5,
        base_weight=45,
        synergy_tags={"slacker": 1.7, "energetic": 1.3},
    ),
    StateEventCard(
        card_id="gen_roam_tea",
        title="喝下午茶瘫在椅子上逛B站水群",
        state=BotState.IDLE,
        action_type=ActionType.IDLE_ROAM,
        min_hours=0.5,
        max_hours=2.0,
        base_weight=55,
        synergy_tags={"slacker": 1.6, "energetic": 1.4},
    ),
    StateEventCard(
        card_id="gen_roam_emo",
        title="趴在窗边发呆看云放空脑袋",
        state=BotState.IDLE,
        action_type=ActionType.IDLE_ROAM,
        min_hours=0.5,
        max_hours=1.5,
        base_weight=35,
    ),
]

# Deprecated: encounter cards were removed from the product logic.
DEFAULT_ENCOUNTER_POOL: list[str] = []


class RogueliteDeckEngine:
    """Builds and manages the daily 24h dynamic action-card timeline."""

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

    # ------------------------------------------------------------------
    # Chronotype weight curve
    # ------------------------------------------------------------------
    @staticmethod
    def get_chronotype_weights(chronotype: str, hour_t: float) -> dict[ActionType, float]:
        """Return the four action-type draw multipliers at hour ``hour_t`` (0.0~24.0).

        The curve is intentionally coarse and piecewise-linear-free: it only needs to
        make "when does she sleep / work / surf" feel human for each chronotype.
        """
        chrono = normalize_chronotype(chronotype)
        try:
            hour = float(hour_t) % 24.0
        except (TypeError, ValueError):
            hour = 0.0

        # baseline: every action type is drawable
        weights: dict[ActionType, float] = {
            ActionType.SLEEP: 1.0,
            ActionType.SURF: 1.0,
            ActionType.IDLE_ROAM: 1.0,
            ActionType.BUSY_TASK: 1.0,
        }

        if chrono == "early":
            if hour >= 22.0 or hour < 6.0:
                weights.update(
                    {
                        ActionType.SLEEP: 8.0,
                        ActionType.SURF: 0.15,
                        ActionType.IDLE_ROAM: 0.15,
                        ActionType.BUSY_TASK: 0.1,
                    }
                )
            elif hour < 7.0:
                weights.update(
                    {
                        ActionType.SLEEP: 3.0,
                        ActionType.IDLE_ROAM: 1.0,
                        ActionType.SURF: 0.6,
                        ActionType.BUSY_TASK: 0.8,
                    }
                )
            elif hour < 17.0:
                weights.update(
                    {
                        ActionType.SLEEP: 0.12,
                        ActionType.BUSY_TASK: 2.6,
                        ActionType.IDLE_ROAM: 1.8,
                        ActionType.SURF: 0.9,
                    }
                )
            else:  # 17:00 ~ 22:00
                weights.update(
                    {
                        ActionType.SLEEP: 0.35,
                        ActionType.SURF: 2.4,
                        ActionType.IDLE_ROAM: 2.0,
                        ActionType.BUSY_TASK: 0.8,
                    }
                )
            return weights

        if chrono == "night_owl":
            if hour < 3.5:
                weights.update(
                    {
                        ActionType.SURF: 3.0,
                        ActionType.IDLE_ROAM: 2.6,
                        ActionType.BUSY_TASK: 1.8,
                        ActionType.SLEEP: 0.2,
                    }
                )
            elif hour < 11.5:
                weights.update(
                    {
                        ActionType.SLEEP: 7.0,
                        ActionType.SURF: 0.15,
                        ActionType.IDLE_ROAM: 0.2,
                        ActionType.BUSY_TASK: 0.12,
                    }
                )
            elif hour < 18.0:
                weights.update(
                    {
                        ActionType.BUSY_TASK: 2.2,
                        ActionType.IDLE_ROAM: 2.0,
                        ActionType.SURF: 1.6,
                        ActionType.SLEEP: 0.2,
                    }
                )
            else:  # 18:00 ~ 24:00
                weights.update(
                    {
                        ActionType.SURF: 3.0,
                        ActionType.IDLE_ROAM: 2.4,
                        ActionType.BUSY_TASK: 1.6,
                        ActionType.SLEEP: 0.2,
                    }
                )
            return weights

        # normal
        if hour < 7.5:
            weights.update(
                {
                    ActionType.SLEEP: 6.0 if hour < 6.0 else 4.0,
                    ActionType.SURF: 0.2,
                    ActionType.IDLE_ROAM: 0.2,
                    ActionType.BUSY_TASK: 0.12,
                }
            )
        elif hour < 9.0:
            weights.update(
                {
                    ActionType.SLEEP: 1.0,
                    ActionType.BUSY_TASK: 1.8,
                    ActionType.IDLE_ROAM: 1.5,
                    ActionType.SURF: 1.0,
                }
            )
        elif hour < 12.5:
            weights.update(
                {
                    ActionType.BUSY_TASK: 2.8,
                    ActionType.IDLE_ROAM: 1.5,
                    ActionType.SURF: 0.8,
                    ActionType.SLEEP: 0.1,
                }
            )
        elif hour < 14.0:
            weights.update(
                {
                    ActionType.SLEEP: 1.3,
                    ActionType.IDLE_ROAM: 2.2,
                    ActionType.SURF: 1.8,
                    ActionType.BUSY_TASK: 0.9,
                }
            )
        elif hour < 18.0:
            weights.update(
                {
                    ActionType.BUSY_TASK: 2.6,
                    ActionType.IDLE_ROAM: 1.6,
                    ActionType.SURF: 1.0,
                    ActionType.SLEEP: 0.1,
                }
            )
        elif hour < 23.5:
            weights.update(
                {
                    ActionType.SURF: 2.8,
                    ActionType.IDLE_ROAM: 2.2,
                    ActionType.BUSY_TASK: 0.5,
                    ActionType.SLEEP: 0.1,
                }
            )
        else:  # 23:30 ~ 24:00
            weights.update(
                {
                    ActionType.SLEEP: 3.5,
                    ActionType.SURF: 1.6,
                    ActionType.IDLE_ROAM: 1.2,
                    ActionType.BUSY_TASK: 0.2,
                }
            )
        return weights

    # ------------------------------------------------------------------
    # Card pool + duration helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _normalize_pool(custom_cards: Optional[list[Any]]) -> list[StateEventCard]:
        pool: list[StateEventCard] = []
        for item in custom_cards or []:
            try:
                if isinstance(item, StateEventCard):
                    pool.append(item)
                elif isinstance(item, dict):
                    pool.append(StateEventCard.from_dict(item))
            except Exception:
                continue
        if not pool:
            return list(DEFAULT_STATE_EVENT_CARDS)
        # Guarantee that all four action types stay drawable even if the Soul's
        # custom pool happens to miss one.
        available = {card.action for card in pool}
        for default_card in DEFAULT_STATE_EVENT_CARDS:
            if default_card.action not in available:
                pool.append(default_card)
                available.add(default_card.action)
        return pool

    @staticmethod
    def _is_long_sleep(card: StateEventCard) -> bool:
        return card.action == ActionType.SLEEP and card.max_hours >= 3.0

    @staticmethod
    def _card_weight(
        card: StateEventCard,
        state_weights: dict[ActionType, float],
        vigor: DayVigorCard,
        prev_block: Optional[SlotInstance],
        consecutive_busy_hours: float,
        hours_since_long_sleep: float,
    ) -> float:
        weight = float(max(5, card.base_weight))
        weight *= float(state_weights.get(card.action, 1.0))
        weight *= float(vigor.state_bias.get(card.state.value, 1.0))
        weight *= float(card.synergy_tags.get(vigor.tag, 1.0))

        if prev_block is not None:
            # Anti-fatigue / anti-repeat synergy: never schedule the exact same
            # action archetype twice in a row unless weights really force it.
            if card.action == prev_block.action:
                weight *= 0.55
            # Just came out of a BUSY_TASK -> lean into a break.
            if prev_block.action == ActionType.BUSY_TASK and card.action in (
                ActionType.SURF,
                ActionType.IDLE_ROAM,
            ):
                weight *= 1.45

        # Worked too long -> double the chance of taking a break.
        if consecutive_busy_hours >= 3.5:
            if card.action in (ActionType.SURF, ActionType.IDLE_ROAM):
                weight *= 2.0
            elif card.action == ActionType.BUSY_TASK:
                weight *= 0.4

        # No second long sleep within 6h after waking up from a long sleep.
        if (
            hours_since_long_sleep < 6.0
            and card.action == ActionType.SLEEP
            and card.max_hours >= 3.0
        ):
            weight *= 0.06

        return max(0.05, weight)

    def _weighted_draw_card(
        self,
        pool: list[StateEventCard],
        remaining_hours: float,
        state_weights: dict[ActionType, float],
        vigor: DayVigorCard,
        prev_block: Optional[SlotInstance],
        consecutive_busy_hours: float,
        hours_since_long_sleep: float,
        rng: Any = None,
    ) -> StateEventCard:
        rng = rng or random
        candidates: list[StateEventCard] = []
        weights: list[float] = []
        for card in pool:
            # A card whose minimum duration cannot fit the remaining time is not a
            # candidate (unless it is the only thing left).
            if card.min_hours > remaining_hours + 1e-6:
                continue
            candidates.append(card)
            weights.append(
                self._card_weight(
                    card,
                    state_weights,
                    vigor,
                    prev_block,
                    consecutive_busy_hours,
                    hours_since_long_sleep,
                )
            )
        if not candidates:
            return min(
                pool,
                key=lambda item: abs(item.min_hours - remaining_hours),
            ) if pool else DEFAULT_STATE_EVENT_CARDS[0]
        return rng.choices(candidates, weights=weights, k=1)[0]

    def _roll_duration(
        self, min_hours: float, max_hours: float, remaining: float, rng: Any = None
    ) -> float:
        """Roll a 0.5h-grid duration within ``[min_hours, max_hours]`` and ``remaining``."""
        rng = rng or random
        remaining = round(max(0.0, float(remaining)) * 2) / 2
        if remaining <= MIN_BLOCK_HOURS:
            return max(remaining, MIN_BLOCK_HOURS)
        low = clamp_hours(min_hours, 1.0)
        high = clamp_hours(max_hours, low)
        high = min(high, remaining)
        if high < low:
            low = high
        steps = int(round((high - low) / HOUR_GRID))
        if steps <= 0:
            return round(max(MIN_BLOCK_HOURS, min(low, remaining)), 1)
        return round(low + HOUR_GRID * rng.randint(0, steps), 1)

    # ------------------------------------------------------------------
    # 24h dynamic timeline builder
    # ------------------------------------------------------------------
    def _build_dynamic_slots(
        self,
        chronotype: str,
        vigor: DayVigorCard,
        pool: list[StateEventCard],
        rng: Any = None,
    ) -> list[SlotInstance]:
        """Stitch the Soul's card pool into a contiguous 0.0 -> 24.0 timeline."""
        rng = rng or random
        if not pool:
            pool = list(DEFAULT_STATE_EVENT_CARDS)

        blocks: list[SlotInstance] = []
        t = 0.0
        prev_block: Optional[SlotInstance] = None
        consecutive_busy_hours = 0.0
        hours_since_long_sleep = 99.0

        while t < 24.0 - 1e-6:
            remaining = round(24.0 - t, 1)
            state_weights = self.get_chronotype_weights(chronotype, t)
            card = self._weighted_draw_card(
                pool,
                remaining,
                state_weights,
                vigor,
                prev_block,
                consecutive_busy_hours,
                hours_since_long_sleep,
                rng=rng,
            )
            duration = self._roll_duration(
                card.min_hours, card.max_hours, remaining, rng=rng
            )
            duration = round(max(HOUR_GRID, min(duration, remaining)) * 2) / 2

            action = card.action
            block = SlotInstance(
                slot_key=f"block_{len(blocks)}",
                start_hour=round(t, 1),
                end_hour=round(t + duration, 1),
                state=action.bound_state,
                card_id=card.card_id,
                title=card.title,
                label=action.label,
                action_type=action,
                # v2.6: armor is a fixed tier of the action type; the DayVigor
                # card's ``armor_offset`` no longer participates in scheduling.
                armor=action.fixed_armor,
            )
            blocks.append(block)

            if action == ActionType.SLEEP and duration >= 3.0:
                hours_since_long_sleep = 0.0
            else:
                hours_since_long_sleep += duration
            consecutive_busy_hours = (
                consecutive_busy_hours + duration
                if action == ActionType.BUSY_TASK
                else 0.0
            )
            prev_block = block
            t = round(block.end_hour, 1)

        # Defensive float snap: guarantee exactly 24.0 at the end.
        if blocks:
            blocks[-1].end_hour = 24.0
        target = int(rng.choice([8, 9, 10, 11]))
        return self._rebalance_blocks(blocks, target=target)

    @staticmethod
    def _rebalance_blocks(
        blocks: list[SlotInstance], target: int = MAX_TIME_BLOCKS
    ) -> list[SlotInstance]:
        """Merge/split blocks so a day stays within the 7~11 elastic blocks range.

        Merging always prefers adjacent blocks of the same action type, then the
        smallest combined duration, so the timeline keeps its natural shape instead
        of collapsing into one giant tail block.
        """
        result = list(blocks)
        if not result:
            return result

        target = max(MIN_TIME_BLOCKS, min(MAX_TIME_BLOCKS, int(target or MAX_TIME_BLOCKS)))
        # -- too many blocks: merge adjacent blocks (same action first) --
        guard = 0
        while len(result) > target and len(result) > 1 and guard < 64:
            guard += 1
            best_index = 0
            best_cost = float("inf")
            for index in range(len(result) - 1):
                first, second = result[index], result[index + 1]
                combined = first.duration_hours + second.duration_hours
                cost = combined
                if first.action == second.action:
                    cost -= 4.0
                if combined > MAX_ROLL_HOURS:
                    cost += 100.0
                # Never swallow a BUSY_TASK completion result during rebalancing.
                if second.action == ActionType.BUSY_TASK and second.completion_result:
                    cost += 1000.0
                if cost < best_cost:
                    best_cost = cost
                    best_index = index
            first, second = result[best_index], result[best_index + 1]
            first.end_hour = second.end_hour
            if first.action != second.action and second.action == ActionType.BUSY_TASK:
                first.action_type = second.action_type
                first.state = second.state
                first.title = second.title
                first.label = second.label
                first.card_id = second.card_id
                first.busy_settled = second.busy_settled
                first.completion_result = second.completion_result
            del result[best_index + 1]

        # -- too few blocks: split the longest one in half --
        guard = 0
        while len(result) < MIN_TIME_BLOCKS and guard < 16:
            guard += 1
            index = max(
                range(len(result)), key=lambda i: result[i].duration_hours
            )
            block = result[index]
            if block.duration_hours < 1.5:
                break
            midpoint = round((block.start_hour + block.duration_hours / 2.0) * 2) / 2
            if midpoint <= block.start_hour or midpoint >= block.end_hour:
                break
            clone = SlotInstance(
                slot_key=block.slot_key,
                start_hour=midpoint,
                end_hour=block.end_hour,
                state=block.state,
                card_id=block.card_id,
                title=block.title,
                label=block.label,
                action_type=block.action,
                armor=block.armor,
                mutated=block.mutated,
                mutation_monologue=block.mutation_monologue,
                delay_minutes=block.delay_minutes,
                busy_settled=block.busy_settled,
                completion_result=block.completion_result,
            )
            block.end_hour = midpoint
            result.insert(index + 1, clone)

        # Re-index the canonical slot keys after merging/splitting and pin every
        # block to the fixed v2.6 armor tier of its (possibly merged) action type.
        for index, block in enumerate(result):
            block.slot_key = f"block_{index}"
            block.armor = block.action.fixed_armor
        if result:
            result[-1].end_hour = 24.0
        return result

    def _build_static_slots(self) -> list[SlotInstance]:
        """Static fallback timeline when ``enable_roguelite_schedule`` is disabled."""
        return [
            SlotInstance(
                slot_key="block_0",
                start_hour=0.0,
                end_hour=7.0,
                state=BotState.RESTING,
                card_id="static_sleep",
                title="夜间休息",
                action_type=ActionType.SLEEP,
                armor=ActionType.SLEEP.fixed_armor,
            ),
            SlotInstance(
                slot_key="block_1",
                start_hour=7.0,
                end_hour=19.0,
                state=BotState.IDLE,
                card_id="static_roam",
                title="日常在线随时闲聊",
                action_type=ActionType.IDLE_ROAM,
                armor=ActionType.IDLE_ROAM.fixed_armor,
            ),
            SlotInstance(
                slot_key="block_2",
                start_hour=19.0,
                end_hour=24.0,
                state=BotState.IDLE,
                card_id="static_surf",
                title="晚上躺沙发刷视频",
                action_type=ActionType.SURF,
                armor=ActionType.SURF.fixed_armor,
            ),
        ]

    # ------------------------------------------------------------------
    # Persistence + public API
    # ------------------------------------------------------------------
    @staticmethod
    def is_valid_timeline(run: DailyScheduleRun) -> bool:
        """Whether ``run.slots`` forms a contiguous 0.0 -> 24.0 0.5h-grid timeline."""
        slots = getattr(run, "slots", None) or []
        if len(slots) < 3:
            return False
        if abs(float(slots[0].start_hour) - 0.0) > 1e-6:
            return False
        prev_end = 0.0
        for slot in slots:
            start = float(slot.start_hour)
            end = float(slot.end_hour)
            if end <= start:
                return False
            if abs(start - prev_end) > 1e-6:
                return False
            prev_end = end
        return abs(prev_end - 24.0) < 1e-6

    async def get_or_build_today_run(
        self,
        soul_id: str,
        custom_cards: Optional[list[Any]] = None,
        custom_encounters: Optional[list[Any]] = None,  # deprecated, ignored
        force_reroll: bool = False,
        now_dt: Optional[datetime] = None,
        chronotype: str = DEFAULT_CHRONOTYPE,
    ) -> DailyScheduleRun:
        """Load today's timeline from cache/SQLite or build a new dynamic run."""
        del custom_encounters  # encounter cards removed from the product logic
        date_str = self.today_str(now_dt)
        cache_key = (soul_id, date_str)

        if not force_reroll and cache_key in self._cache:
            cached = self._cache[cache_key]
            if self.is_valid_timeline(cached):
                return cached

        if not force_reroll:
            row = await self.db.fetchone(
                "SELECT run_json FROM daily_schedules WHERE soul_id = ? AND date_str = ?",
                (soul_id, date_str),
            )
            if row and row.get("run_json"):
                try:
                    run = DailyScheduleRun.from_dict(json.loads(row["run_json"]))
                    if self.is_valid_timeline(run):
                        self._cache[cache_key] = run
                        return run
                except Exception:
                    pass

        run = self._draw_new_run(
            soul_id=soul_id,
            date_str=date_str,
            custom_cards=custom_cards,
            chronotype=chronotype,
        )
        await self.save_run(run)
        return run

    async def save_run(self, run: DailyScheduleRun) -> None:
        """Persist DailyScheduleRun to SQLite and the memory cache."""
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
        custom_cards: Optional[list[Any]] = None,
        chronotype: str = DEFAULT_CHRONOTYPE,
    ) -> DailyScheduleRun:
        """Build a brand new 24h dynamic timeline (no LLM involved)."""
        run_chronotype = normalize_chronotype(chronotype)
        enable_roguelite = bool(self.cfg.get("enable_roguelite_schedule", True))
        vigor = random.choice(DEFAULT_VIGOR_CARDS)

        if not enable_roguelite:
            slots = self._build_static_slots()
        else:
            pool = self._normalize_pool(custom_cards)
            slots = self._build_dynamic_slots(run_chronotype, vigor, pool)

        return DailyScheduleRun(
            soul_id=soul_id,
            date_str=date_str,
            vigor_id=vigor.vigor_id,
            vigor_title=vigor.title,
            vigor_desc=vigor.description,
            slots=slots,
            encounters=[],
            chronotype=run_chronotype,
        )

    @staticmethod
    def resolve_slot_index_for_time(
        run: DailyScheduleRun, now_dt: Optional[datetime] = None
    ) -> int:
        """Resolve which dynamic block in ``run.slots`` applies at ``now_dt``."""
        dt = now_dt or datetime.now()
        now_hour = dt.hour + dt.minute / 60.0 + dt.second / 3600.0
        slots = getattr(run, "slots", None) or []
        for idx, slot in enumerate(slots):
            try:
                start = float(slot.start_hour)
                end = float(slot.end_hour)
            except (TypeError, ValueError):
                continue
            if start <= now_hour < end:
                return idx
        return 0

    async def get_current_and_next_slot(
        self,
        soul_id: str,
        custom_cards: Optional[list[Any]] = None,
        custom_encounters: Optional[list[Any]] = None,
        now_dt: Optional[datetime] = None,
        chronotype: str = DEFAULT_CHRONOTYPE,
    ) -> tuple[DailyScheduleRun, SlotInstance, SlotInstance]:
        """Return ``(run, current_slot, next_slot)`` for ``soul_id``."""
        run = await self.get_or_build_today_run(
            soul_id=soul_id,
            custom_cards=custom_cards,
            custom_encounters=custom_encounters,
            now_dt=now_dt,
            chronotype=chronotype,
        )
        idx = self.resolve_slot_index_for_time(run, now_dt=now_dt)
        current_slot = run.slots[idx]
        next_slot = run.slots[(idx + 1) % len(run.slots)]
        return run, current_slot, next_slot

    async def get_current_slot(
        self,
        soul_id: str,
        custom_cards: Optional[list[Any]] = None,
        custom_encounters: Optional[list[Any]] = None,
        now_dt: Optional[datetime] = None,
        chronotype: str = DEFAULT_CHRONOTYPE,
    ) -> SlotInstance:
        """Convenience method returning the current ``SlotInstance`` for ``soul_id``."""
        _, current_slot, _ = await self.get_current_and_next_slot(
            soul_id=soul_id,
            custom_cards=custom_cards,
            custom_encounters=custom_encounters,
            now_dt=now_dt,
            chronotype=chronotype,
        )
        return current_slot

    async def force_set_current_state(
        self,
        soul_id: str,
        new_state: Optional[BotState] = None,
        new_title: Optional[str] = None,
        armor: Optional[int] = None,
        slot_key: Optional[str] = None,
        now_dt: Optional[datetime] = None,
        action_type: Optional[Any] = None,
    ) -> SlotInstance:
        """Manually override the current (or specified ``slot_key``) block.

        ``armor`` is deprecated since v2.6: every block's guard armor is a fixed
        tier derived from its ``action_type`` (``SLEEP=80`` / ``BUSY_TASK=65`` /
        ``SURF=40`` / ``IDLE_ROAM=25``), so the parameter is kept only for call-site
        signature compatibility and is ignored.
        """
        run, current_slot, _ = await self.get_current_and_next_slot(
            soul_id=soul_id, now_dt=now_dt
        )
        target_slot = current_slot
        if slot_key:
            found = next((s for s in run.slots if s.slot_key == slot_key), None)
            if found is not None:
                target_slot = found

        parsed_action = ActionType.coerce(action_type, None)
        if parsed_action is not None:
            target_slot.action_type = parsed_action
            target_slot.state = parsed_action.bound_state
        elif new_state is not None:
            coerced_state = _coerce_state(
                new_state.value if isinstance(new_state, BotState) else new_state,
                BotState.IDLE,
            )
            target_slot.state = coerced_state
            derived_action = _derive_action_type(coerced_state, new_title or target_slot.title)
            # Keep the existing SURF action only when the caller did NOT give a new
            # title (bare ``state IDLE``); an explicit non-surf title must win so
            # `/onlyyou state IDLE 发呆` can turn the block into IDLE_ROAM.
            if (
                coerced_state == BotState.IDLE
                and target_slot.action == ActionType.SURF
                and not new_title
            ):
                derived_action = ActionType.SURF
            target_slot.action_type = derived_action
        target_slot.label = target_slot.action.label

        if new_title:
            target_slot.title = str(new_title)
        # v2.6: ignore the caller-provided armor, always pin the fixed tier.
        target_slot.armor = target_slot.action.fixed_armor
        target_slot.mutated = False
        target_slot.mutation_monologue = ""
        target_slot.delay_minutes = 0
        target_slot.reset_busy()
        await self.save_run(run)
        return target_slot
