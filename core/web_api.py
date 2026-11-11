"""Backend Web API routes (`context.register_web_api`) for OnlyYou Plugin Pages (`views/console`)."""

import inspect
import random
import re
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional

from .memory_manager import MemoryTier
from .roguelite_deck import (
    DEFAULT_STATE_EVENT_CARDS,
    StateEventCard,
)
from .soul_manager import SoulProfile
from .state_machine import ACTION_TYPE_DEFINITIONS, ActionType, BotState, UserTier

try:
    from astrbot.api import logger
except ImportError:
    import logging
    logger = logging.getLogger("onlyyou.web_api")

try:
    from astrbot.api.web import request as astrbot_web_request
except ImportError:
    astrbot_web_request = None

if TYPE_CHECKING:
    from ..main import OnlyYouPlugin


API_PREFIX = "/astrbot_plugin_only_you"


def ok_response(data: Any = None, message: str = "ok") -> dict[str, Any]:
    """Standard AstrBot Web API success envelope."""
    return {
        "status": "ok",
        "message": message,
        "data": {} if data is None else data,
    }


def err_response(message: str, code: str = "bad_request", data: Any = None) -> dict[str, Any]:
    """Standard AstrBot Web API error envelope."""
    return {
        "status": "error",
        "message": message,
        "data": {"code": code} if data is None else data,
    }


# ==============================================================================
# Module-level helpers shared by the new Plugin Pages routes.
# The parallel-implemented modules (`character_card`, rewritten `model_hub`, ...)
# are imported lazily so that a missing/partial implementation degrades into an
# `err_response` instead of breaking the whole plugin import chain.
# ==============================================================================
def _character_card_module(log_missing: bool = False) -> Any:
    """Lazily import the contract `core.character_card` module (implemented in parallel)."""
    try:
        from . import character_card

        return character_card
    except Exception as exc:  # pragma: no cover - only before character_card.py lands
        if log_missing:
            logger.warning(f"[OnlyYou] character_card module unavailable: {exc}")
        return None


def _as_str_list(value: Any) -> list[str]:
    """Normalize a legacy string (comma/、/slash separated) or array into a clean str list."""
    if isinstance(value, str):
        parts = re.split(r"[,，、/\n]+", value)
        return [part.strip() for part in parts if part.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _as_bool(value: Any, default: bool = False) -> bool:
    """Coerce a JSON payload value (bool/int/str) into a bool with a safe default."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("1", "true", "yes", "on", "enable", "enabled"):
            return True
        if text in ("0", "false", "no", "off", "disable", "disabled", ""):
            return False
        return default
    return default


def _umo_kind(umo: Any) -> str:
    """``"group"`` / ``"private"`` from an AstrBot UMO (contract §6.2 session item)."""
    parts = str(umo or "").split(":")
    if len(parts) >= 2 and "group" in parts[1].strip().lower():
        return "group"
    return "private"


# In-memory chat-memory cleanup statistics (contract §6.2 / §4.1).
CLEAR_STAT_KEYS = ("windows", "evicted", "unread", "messages")


def _normalize_clear_stats(value: Any) -> dict[str, int]:
    """Normalize a ``SessionBuffer`` cleanup result into ``{windows, evicted, unread, messages}``."""
    stats: dict[str, int] = {key: 0 for key in CLEAR_STAT_KEYS}
    if isinstance(value, dict):
        for key in CLEAR_STAT_KEYS:
            try:
                stats[key] = int(value.get(key) or 0)
            except (TypeError, ValueError):
                stats[key] = 0
    return stats


def _ensure_section(card: dict[str, Any], key: str) -> dict[str, Any]:
    """Return `card[key]` as a dict, replacing invalid values with a fresh dict."""
    section = card.get(key)
    if not isinstance(section, dict):
        section = {}
        card[key] = section
    return section


CHRONOTYPES = ("early", "normal", "night_owl")


def _coerce_hour(value: Any, default: float = 0.0) -> float:
    """Best-effort float hour (dynamic timeline uses the 0.5h grid)."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _slot_attr(slot: Any, key: str, default: Any = None) -> Any:
    """Read a slot field from either a `SlotInstance` or a plain dict payload."""
    if isinstance(slot, dict):
        value = slot.get(key, default)
    else:
        value = getattr(slot, key, default)
    return default if value is None else value


def _slot_action_type(slot: Any) -> Optional[ActionType]:
    """Best-effort `ActionType` of a slot/card (legacy dict payloads tolerated)."""
    action = getattr(slot, "action_type", None)
    if action is None and isinstance(slot, dict):
        action = slot.get("action_type")
    if isinstance(action, ActionType):
        return action
    return ActionType.coerce(action, None)


def _slot_remaining_and_progress(
    slot: Any, now_dt: Optional[datetime] = None
) -> tuple[int, float]:
    """
    Compute `(remaining_seconds, progress)` for a dynamic timeline block.

    Both values derive from the block's **float** `start_hour` / `end_hour`
    (0.5h grid, contiguous `0.0 -> 24.0`). Legacy overnight blocks
    (`end_hour < start_hour`, e.g. `23 -> 7`) still fall back to modular
    arithmetic so old serialized runs never break the UI.
    """
    now_dt = now_dt or datetime.now()
    now_hour = now_dt.hour + now_dt.minute / 60.0 + now_dt.second / 3600.0
    start_hour = _coerce_hour(_slot_attr(slot, "start_hour", 0.0), 0.0)
    end_hour = _coerce_hour(_slot_attr(slot, "end_hour", 24.0), 24.0)

    if end_hour <= start_hour:
        # Legacy `end < start` overnight slot: wrap around midnight.
        total_hours = (end_hour - start_hour) % 24.0 or 24.0
        elapsed_hours = (now_hour - start_hour) % 24.0
    else:
        total_hours = max(0.0, end_hour - start_hour)
        elapsed_hours = now_hour - start_hour
    elapsed_hours = max(0.0, min(total_hours, elapsed_hours))

    remaining_seconds = int(round(max(0.0, total_hours - elapsed_hours) * 3600))
    progress = elapsed_hours / total_hours if total_hours > 0 else 1.0
    return remaining_seconds, round(max(0.0, min(1.0, progress)), 4)


def _format_slot_time_range(slot: Any) -> tuple[str, str]:
    """Return `(start_time, end_time)` as `HH:MM` strings for a timeline block."""
    formatter = getattr(slot, "format_time_range", None)
    if callable(formatter):
        try:
            text = str(formatter() or "")
            if "-" in text:
                start_time, end_time = text.split("-", 1)
                return start_time.strip(), end_time.strip()
        except Exception:  # pragma: no cover - defensive only
            pass
    start_hour = _coerce_hour(_slot_attr(slot, "start_hour", 0.0), 0.0)
    end_hour = _coerce_hour(_slot_attr(slot, "end_hour", 24.0), 24.0)

    def _fmt(hour: float) -> str:
        minutes = max(0, min(24 * 60, int(round(hour * 60))))
        return f"{minutes // 60:02d}:{minutes % 60:02d}"

    return _fmt(start_hour), _fmt(end_hour)


def _enrich_slot(slot: Any, now_dt: Optional[datetime] = None) -> dict[str, Any]:
    """`slot.to_dict()` plus the live `progress_pct` int (contract §6.1)."""
    to_dict = getattr(slot, "to_dict", None)
    if callable(to_dict):
        data = dict(to_dict() or {})
    elif isinstance(slot, dict):
        data = dict(slot)
    else:
        data = {}
    progress_pct: Optional[int] = None
    getter = getattr(slot, "get_progress_pct", None)
    if callable(getter):
        try:
            progress_pct = int(getter(now_dt))
        except Exception:  # pragma: no cover - defensive only
            progress_pct = None
    if progress_pct is None:
        _, progress = _slot_remaining_and_progress(slot, now_dt)
        progress_pct = int(round(progress * 100))
    data["progress_pct"] = max(0, min(100, int(progress_pct)))
    return data


_CARD_PAYLOAD_FIELDS = (
    "card_id",
    "title",
    "state",
    "action_type",
    "min_hours",
    "max_hours",
    "valid_slots",
    "base_weight",
    "armor",
    "synergy_tags",
)


def _card_payload_from_body(body: dict[str, Any]) -> dict[str, Any]:
    """Merge the nested `card` object with flat top-level card fields.

    The WebUI sends `{"card": {...}, "scope": ..., "soul_id": ...}`, while legacy
    callers send the card fields flat in the body and may put `action_type` /
    `min_hours` / `max_hours` next to (instead of inside) the `card` object.
    """
    body = body if isinstance(body, dict) else {}
    nested = body.get("card")
    if isinstance(nested, dict):
        card_data = dict(nested)
        for key in _CARD_PAYLOAD_FIELDS:
            if key not in card_data and body.get(key) is not None:
                card_data[key] = body[key]
        return card_data
    return {
        key: value
        for key, value in body.items()
        if key not in ("card_type", "scope", "soul_id")
    }


def _busy_results_payload(run: Any, now_dt: Optional[datetime] = None) -> list[dict[str, Any]]:
    """Settled (or already finished) `BUSY_TASK` blocks -> today's Tier-1 results."""
    now_dt = now_dt or datetime.now()
    now_hour = now_dt.hour + now_dt.minute / 60.0 + now_dt.second / 3600.0
    results: list[dict[str, Any]] = []
    for slot in _slot_attr(run, "slots", None) or []:
        if _slot_action_type(slot) != ActionType.BUSY_TASK:
            continue
        end_hour = _coerce_hour(_slot_attr(slot, "end_hour", 24.0), 24.0)
        settled = bool(_slot_attr(slot, "busy_settled", False))
        if not settled and end_hour > now_hour:
            continue
        start_time, end_time = _format_slot_time_range(slot)
        results.append(
            {
                "slot_key": str(_slot_attr(slot, "slot_key", "") or ""),
                "title": str(_slot_attr(slot, "title", "") or ""),
                "completion_result": str(_slot_attr(slot, "completion_result", "") or ""),
                "progress_pct": int(_enrich_slot(slot, now_dt).get("progress_pct", 100)),
                "start_hour": _coerce_hour(_slot_attr(slot, "start_hour", 0.0), 0.0),
                "end_hour": end_hour,
                "start_time": start_time,
                "end_time": end_time,
            }
        )
    return results


class OnlyYouWebAPI:
    """
    🖥️ Registers and handles all 31 RESTful Web API routes for the revamped
    `views/console` AstrBot Plugin Pages (17 legacy routes kept for compatibility
    plus 10 new dashboard / character-card / model / session routes and the four
    v2.8 bot-platform binding routes in `register_routes`).
    """

    def __init__(self, plugin: "OnlyYouPlugin"):
        self.plugin = plugin
        # Optional test override for offline unit tests
        self._mock_query: dict[str, Any] = {}
        self._mock_body: dict[str, Any] = {}
        self._mock_method: str = "GET"

    def set_mock_request(
        self,
        method: str = "GET",
        query: Optional[dict[str, Any]] = None,
        body: Optional[dict[str, Any]] = None,
    ) -> None:
        """Helper for local unit testing outside FastAPI/AstrBot request context."""
        self._mock_method = method.upper()
        self._mock_query = dict(query or {})
        self._mock_body = dict(body or {})

    def _get_method(self) -> str:
        if astrbot_web_request is not None:
            try:
                return str(astrbot_web_request.method or "GET").upper()
            except Exception:
                pass
        return self._mock_method

    def _get_query_param(self, key: str, default: str = "") -> str:
        if astrbot_web_request is not None:
            try:
                val = astrbot_web_request.query.get(key)
                if val is not None:
                    return str(val).strip()
            except Exception:
                pass
        val = self._mock_query.get(key)
        return str(val).strip() if val is not None else default

    async def _get_json_body(self) -> dict[str, Any]:
        if astrbot_web_request is not None:
            try:
                data = await astrbot_web_request.json(default={})
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
        return dict(self._mock_body)

    def register_routes(self) -> None:
        """Register all 31 WebUI Console routes with `self.plugin.context.register_web_api`."""
        ctx = getattr(self.plugin, "context", None)
        register_fn = getattr(ctx, "register_web_api", None) if ctx is not None else None
        if not callable(register_fn):
            logger.debug("[OnlyYou] Context does not support register_web_api in current environment.")
            return

        routes: list[tuple[str, Any, list[str], str]] = [
            (
                f"{API_PREFIX}/init_summary",
                self.api_init_summary,
                ["GET"],
                "OnlyYou: 获取角色列表、AstrBot原生Persona列表与全局模型状态",
            ),
            (
                f"{API_PREFIX}/souls/detail",
                self.api_souls_detail,
                ["GET"],
                "OnlyYou: 获取指定 Soul 完整角色卡、今日三态日程与会话路由绑定",
            ),
            (
                f"{API_PREFIX}/souls/import_persona",
                self.api_souls_import_persona,
                ["POST"],
                "OnlyYou: 从 AstrBot 原生 Persona 导入人格并可选 AI 扩写行动卡池",
            ),
            (
                f"{API_PREFIX}/souls/save",
                self.api_souls_save,
                ["POST"],
                "OnlyYou: 新建或保存指定 Soul 的基础设定、冲浪标签与会话绑定",
            ),
            (
                f"{API_PREFIX}/souls/rename",
                self.api_souls_rename,
                ["POST"],
                "OnlyYou: 修改 Soul ID 并事务级联更新关联表",
            ),
            (
                f"{API_PREFIX}/souls/delete",
                self.api_souls_delete,
                ["POST"],
                "OnlyYou: 删除指定 Soul 及其关联数据",
            ),
            (
                f"{API_PREFIX}/schedule/reroll",
                self.api_schedule_reroll,
                ["POST"],
                "OnlyYou: 重新抽取今日 24h 动态日程（四类行动卡）",
            ),
            (
                f"{API_PREFIX}/schedule/force_state",
                self.api_schedule_force_state,
                ["POST"],
                "OnlyYou: 手动强制切换当前或指定槽位的四类行动类型与标题（护甲按行动固定档位）",
            ),
            (
                f"{API_PREFIX}/cards/list",
                self.api_cards_list,
                ["GET"],
                "OnlyYou: 获取指定 Soul 的专属行动卡池、全局默认卡池与四类行动定义",
            ),
            (
                f"{API_PREFIX}/cards/save",
                self.api_cards_save,
                ["POST"],
                "OnlyYou: 新增或保存单张四类行动卡片（SLEEP/SURF/IDLE_ROAM/BUSY_TASK）",
            ),
            (
                f"{API_PREFIX}/cards/delete",
                self.api_cards_delete,
                ["POST"],
                "OnlyYou: 删除指定行动卡片",
            ),
            (
                f"{API_PREFIX}/cards/ai_generate",
                self.api_cards_ai_generate,
                ["POST"],
                "OnlyYou: 调用小脑模型根据角色设定生成 8~12 张专属四类行动卡",
            ),
            (
                f"{API_PREFIX}/users/list",
                self.api_users_list,
                ["GET"],
                "OnlyYou: 获取 L1/L2/L3 三级用户关系列表与好感度",
            ),
            (
                f"{API_PREFIX}/users/update",
                self.api_users_update,
                ["POST"],
                "OnlyYou: 修改用户等级、好感度、专属称呼或合并跨平台账号",
            ),
            (
                f"{API_PREFIX}/memories/list",
                self.api_memories_list,
                ["GET"],
                "OnlyYou: 按分级 (Tier 1/2/3) 与用户筛选查询记忆列表及活力值",
            ),
            (
                f"{API_PREFIX}/memories/mutate",
                self.api_memories_mutate,
                ["POST"],
                "OnlyYou: 新增、编辑、删除记忆，钉选核心记忆或手动触发遗忘清理 GC",
            ),
            (
                f"{API_PREFIX}/surfing/list_and_trigger",
                self.api_surfing_list_and_trigger,
                ["GET", "POST"],
                "OnlyYou: 查看已刷视频卡片墙，或手动触发B站/抖音冲浪与分享",
            ),
            # ------------------------------------------------------------------
            # New Plugin Pages routes (2026 revamp): overview / character card /
            # plugin-autonomous four-role models / default session routing.
            # ------------------------------------------------------------------
            (
                f"{API_PREFIX}/dashboard/overview",
                self.api_dashboard_overview,
                ["GET"],
                "OnlyYou: 概览页一次拉齐当前时间块进度、24h 日程、繁忙成果、微调配额、关系/记忆/视频统计与模型健康",
            ),
            (
                f"{API_PREFIX}/card/schema",
                self.api_card_schema,
                ["GET"],
                "OnlyYou: 获取角色卡 10 分区字段定义(CARD_SCHEMA)与指定角色的当前卡数据",
            ),
            (
                f"{API_PREFIX}/card/save",
                self.api_card_save,
                ["POST"],
                "OnlyYou: 保存完整角色卡（normalize_card 归一化后持久化）",
            ),
            (
                f"{API_PREFIX}/card/ai_generate",
                self.api_card_ai_generate,
                ["POST"],
                "OnlyYou: 调用主模型生成整张角色卡或单个分区的草稿（不落库）",
            ),
            (
                f"{API_PREFIX}/card/preview_prompt",
                self.api_card_preview_prompt,
                ["POST"],
                "OnlyYou: 预览角色卡渲染后的 Persona Prompt 与字符数",
            ),
            (
                f"{API_PREFIX}/models/sources",
                self.api_models_sources,
                ["GET"],
                "OnlyYou: 获取 AstrBot 提供商源列表与可用 TTS 提供商列表（不含密钥）",
            ),
            (
                f"{API_PREFIX}/models/list",
                self.api_models_list,
                ["GET"],
                "OnlyYou: 按提供商源 ID 拉取已配置 + 远程可用模型列表",
            ),
            (
                f"{API_PREFIX}/models/config",
                self.api_models_config,
                ["GET", "POST"],
                "OnlyYou: 读取或保存四角色模型配置（保存后热重建插件模型实例）",
            ),
            (
                f"{API_PREFIX}/models/test",
                self.api_models_test,
                ["POST"],
                "OnlyYou: 测试指定模型角色连通性（TTS 额外返回 base64 试听音频）",
            ),
            (
                f"{API_PREFIX}/sessions/default_soul",
                self.api_sessions_default_soul,
                ["POST"],
                "OnlyYou: 设置插件默认角色（用于未绑定 UMO 的会话路由）",
            ),
            # ------------------------------------------------------------------
            # v2.8 bot-platform binding routes (contract §6.1): one AstrBot bot
            # instance (platform_id) belongs to exactly one Soul; unbinding also
            # clears that platform's in-memory chat state.
            # ------------------------------------------------------------------
            (
                f"{API_PREFIX}/sessions/platforms",
                self.api_sessions_platforms,
                ["GET"],
                "OnlyYou: 机器人接口列表 / 角色绑定关系 / 当前角色管辖的活跃会话内存概览",
            ),
            (
                f"{API_PREFIX}/sessions/bind_platform",
                self.api_sessions_bind_platform,
                ["POST"],
                "OnlyYou: 将 AstrBot 机器人接口（platform_id）绑定到指定角色（平台互斥）",
            ),
            (
                f"{API_PREFIX}/sessions/unbind_platform",
                self.api_sessions_unbind_platform,
                ["POST"],
                "OnlyYou: 解绑机器人接口并清理该平台的内存滑窗与未读",
            ),
            (
                f"{API_PREFIX}/sessions/clear_memory",
                self.api_sessions_clear_memory,
                ["POST"],
                "OnlyYou: 清空单个会话或整个机器人接口平台的内存滑窗与未读队列",
            ),
        ]

        for route_path, handler, methods, desc in routes:
            register_fn(route_path, handler, methods, desc)
        logger.info(f"[OnlyYou] Registered {len(routes)} Web API routes for Plugin Pages.")

    # ==========================================================================
    # 24h dynamic timeline payload helpers (contract §6.1 / §6.2)
    # ==========================================================================
    @staticmethod
    def _soul_chronotype(soul: Any) -> str:
        """Best-effort normalized chronotype of a SoulProfile (``normal`` fallback)."""
        raw = str(getattr(soul, "chronotype", "") or "").strip().lower()
        return raw if raw in CHRONOTYPES else "normal"

    async def _settle_finished_busy_blocks_safe(
        self, soul: Any, run: Any, now_dt: Optional[datetime] = None
    ) -> list[str]:
        """Lazily settle finished `BUSY_TASK` blocks through `main.py` (never raises).

        `main.py::_settle_finished_busy_blocks` is owned by the runtime teammate;
        when it is absent / still being written the page must keep working, so a
        missing method simply means "nothing to settle".
        """
        settler = getattr(self.plugin, "_settle_finished_busy_blocks", None)
        if not callable(settler):
            return []
        try:
            try:
                value = settler(soul, run, now_dt)
            except TypeError:
                # Older/partial implementation without the optional `now_dt` arg.
                value = settler(soul, run)
            if inspect.isawaitable(value):
                value = await value
        except Exception as exc:
            logger.warning(f"[OnlyYou] lazy busy-task settlement failed: {exc}")
            return []
        if not isinstance(value, (list, tuple, set)):
            return []
        return [str(item) for item in value if str(item).strip()]

    async def _load_current_schedule(self, soul: Any) -> tuple[Any, Any, Any, datetime, str]:
        """Load the current timeline, lazily settle finished busy blocks, reload it."""
        now_dt = datetime.now()
        chronotype = self._soul_chronotype(soul)
        fetch_kwargs: dict[str, Any] = {
            "soul_id": soul.soul_id,
            "custom_cards": getattr(soul, "state_event_cards", None),
            "now_dt": now_dt,
            "chronotype": chronotype,
        }
        run, cur_slot, next_slot = await self.plugin.deck_engine.get_current_and_next_slot(
            **fetch_kwargs
        )
        # Lazy completion settlement can rewrite `completion_result` / `busy_settled`,
        # so always re-read the latest run afterwards (contract §6.2).
        await self._settle_finished_busy_blocks_safe(soul, run, now_dt)
        run, cur_slot, next_slot = await self.plugin.deck_engine.get_current_and_next_slot(
            **fetch_kwargs
        )
        return run, cur_slot, next_slot, now_dt, chronotype

    def _schedule_payload_from(
        self,
        run: Any,
        cur_slot: Any,
        next_slot: Any,
        now_dt: datetime,
        chronotype: str,
    ) -> dict[str, Any]:
        """Assemble the frozen `_build_schedule_payload` shape (contract §6.2)."""
        run_data = dict(run.to_dict() or {}) if callable(getattr(run, "to_dict", None)) else {}
        run_data["slots"] = [
            _enrich_slot(slot, now_dt) for slot in (_slot_attr(run, "slots", None) or [])
        ]
        mutator = getattr(self.plugin, "schedule_mutator", None)
        return {
            "run": run_data,
            "current_slot": _enrich_slot(cur_slot, now_dt),
            "next_slot": _enrich_slot(next_slot, now_dt),
            "max_mutations_per_day": int(getattr(mutator, "max_mutations_per_day", 2) or 2),
            "mutation_cooldown_minutes": int(
                (getattr(mutator, "mutation_cooldown_seconds", 0) or 0) // 60
            ),
            "chronotype": chronotype,
            "busy_results": _busy_results_payload(run, now_dt),
        }

    async def _build_schedule_payload(self, soul: SoulProfile) -> dict[str, Any]:
        run, cur_slot, next_slot, now_dt, chronotype = await self._load_current_schedule(soul)
        return self._schedule_payload_from(run, cur_slot, next_slot, now_dt, chronotype)

    def _collect_known_umos(self, bindings: dict[str, str]) -> list[dict[str, Any]]:
        seen: set[str] = set()
        items: list[dict[str, Any]] = []

        for umo in list(self.plugin.session_buffer._windows.keys()) + list(
            self.plugin._known_l3_umos.values()
        ) + list(bindings.keys()):
            clean = (umo or "").strip()
            if not clean or clean in seen:
                continue
            seen.add(clean)
            msg_count = len(self.plugin.session_buffer._windows.get(clean, []))
            items.append(
                {
                    "umo": clean,
                    "bound_soul_id": bindings.get(clean, ""),
                    "window_messages": msg_count,
                    "is_l3_private": clean in self.plugin._known_l3_umos.values(),
                }
            )
        return items

    # ==========================================================================
    # v2.8 platform binding / session memory / group profile helpers (§6.2/§6.3)
    # ==========================================================================
    def _session_buffer(self) -> Any:
        return getattr(self.plugin, "session_buffer", None)

    def _window_map(self) -> dict[str, list[Any]]:
        windows = getattr(self._session_buffer(), "_windows", None)
        return windows if isinstance(windows, dict) else {}

    def _unread_map(self) -> dict[str, list[Any]]:
        unread = getattr(self._session_buffer(), "_unread", None)
        return unread if isinstance(unread, dict) else {}

    def _platform_id_of(self, umo: Any) -> str:
        """``<platform_id>:...`` first segment via the contract PlatformBridge helper."""
        bridge = getattr(self.plugin, "platform_bridge", None)
        extract = getattr(bridge, "extract_platform_id", None)
        if callable(extract):
            try:
                return str(extract(umo) or "").strip()
            except Exception:  # pragma: no cover - defensive only
                pass
        text = str(umo or "").strip()
        return text.split(":", 1)[0].strip() if text else ""

    def _known_umos_for_ui(self) -> list[str]:
        """Every UMO the console may need to list (windows + known group/L3 sessions)."""
        umos: list[str] = []
        buffer = self._session_buffer()
        lister = getattr(buffer, "list_window_umos", None)
        if callable(lister):
            try:
                umos.extend(list(lister() or []))
            except Exception:  # pragma: no cover - defensive only
                pass
        if not umos:
            umos.extend(list(self._window_map().keys()))
        known_group = getattr(self.plugin, "_known_group_umos", None)
        if isinstance(known_group, (set, list, tuple)):
            umos.extend([str(item) for item in known_group])
        known_l3 = getattr(self.plugin, "_known_l3_umos", None)
        if isinstance(known_l3, dict):
            umos.extend([str(item) for item in known_l3.values()])
        elif isinstance(known_l3, (set, list, tuple)):
            umos.extend([str(item) for item in known_l3])
        seen: set[str] = set()
        result: list[str] = []
        for umo in umos:
            clean = str(umo or "").strip()
            if clean and clean not in seen:
                seen.add(clean)
                result.append(clean)
        return result

    async def _safe_list_souls(self) -> list[Any]:
        manager = getattr(self.plugin, "soul_manager", None)
        lister = getattr(manager, "list_souls", None) if manager is not None else None
        if not callable(lister):
            return []
        try:
            value = lister()
            if inspect.isawaitable(value):
                value = await value
            return list(value or [])
        except Exception as exc:
            logger.warning(f"[OnlyYou] list_souls failed: {exc}")
            return []

    async def _platform_bindings(self) -> dict[str, str]:
        """``{platform_id: soul_id}`` via the contract SoulManager API (defensive)."""
        manager = getattr(self.plugin, "soul_manager", None)
        getter = (
            getattr(manager, "get_platform_bindings", None)
            if manager is not None
            else None
        )
        if callable(getter):
            try:
                value = getter()
                if inspect.isawaitable(value):
                    value = await value
                if isinstance(value, dict):
                    bindings = {
                        str(platform_id).strip(): str(soul_id).strip()
                        for platform_id, soul_id in value.items()
                        if str(platform_id).strip()
                    }
                    return bindings
            except Exception as exc:
                logger.warning(f"[OnlyYou] get_platform_bindings failed: {exc}")
        # Fallback: rebuild from the SoulProfile `bound_platforms` passthrough field
        # (covers a partially-landed SoulManager implementation).
        bindings = {}
        for soul in await self._safe_list_souls():
            soul_id = str(getattr(soul, "soul_id", "") or "").strip()
            for platform_id in _as_str_list(getattr(soul, "bound_platforms", None)):
                bindings.setdefault(platform_id, soul_id)
        return bindings

    async def _bound_platforms_of(self, soul_id: str) -> list[str]:
        """Platform ids owned by one Soul (contract §1.3 ``get_bound_platforms``)."""
        clean = (soul_id or "").strip()
        if not clean:
            return []
        manager = getattr(self.plugin, "soul_manager", None)
        getter = (
            getattr(manager, "get_bound_platforms", None)
            if manager is not None
            else None
        )
        if callable(getter):
            try:
                value = getter(clean)
                if inspect.isawaitable(value):
                    value = await value
                return list(dict.fromkeys(_as_str_list(value)))
            except Exception as exc:
                logger.warning(f"[OnlyYou] get_bound_platforms failed: {exc}")
        bindings = await self._platform_bindings()
        return [platform_id for platform_id, owner in bindings.items() if owner == clean]

    async def _soul_name_map(self) -> dict[str, str]:
        names: dict[str, str] = {}
        for soul in await self._safe_list_souls():
            soul_id = str(getattr(soul, "soul_id", "") or "").strip()
            if soul_id:
                names[soul_id] = str(
                    getattr(soul, "name", "") or soul_id
                ).strip() or soul_id
        return names

    async def _soul_summaries(self) -> list[dict[str, Any]]:
        """``[{soul_id, name, bound_platforms}]`` (contract §6.2 sessions/platforms)."""
        summaries: list[dict[str, Any]] = []
        for soul in await self._safe_list_souls():
            soul_id = str(getattr(soul, "soul_id", "") or "").strip()
            summaries.append(
                {
                    "soul_id": soul_id,
                    "name": str(getattr(soul, "name", "") or soul_id),
                    "bound_platforms": await self._bound_platforms_of(soul_id),
                }
            )
        return summaries

    def _session_window_count(self, umo: str) -> int:
        return len(self._window_map().get(umo) or [])

    def _session_unread_count(self, umo: str) -> int:
        buffer = self._session_buffer()
        getter = getattr(buffer, "get_unread_batch", None)
        if callable(getter):
            try:
                return len(getter(umo) or [])
            except Exception:  # pragma: no cover - defensive only
                pass
        return len(self._unread_map().get(umo) or [])

    def _session_last_active_at(self, umo: str) -> int:
        latest = 0.0
        for msg in list(self._window_map().get(umo) or []) + list(
            self._unread_map().get(umo) or []
        ):
            try:
                latest = max(latest, float(getattr(msg, "timestamp", 0.0) or 0.0))
            except (TypeError, ValueError):
                continue
        return int(latest)

    def _session_item(
        self, umo: str, bindings: dict[str, str], l3_umos: set[str]
    ) -> dict[str, Any]:
        platform_id = self._platform_id_of(umo)
        return {
            "umo": umo,
            "kind": _umo_kind(umo),
            "platform_id": platform_id,
            "bound_soul_id": bindings.get(platform_id, ""),
            "window_messages": self._session_window_count(umo),
            "is_l3_private": umo in l3_umos,
            "last_active_at": self._session_last_active_at(umo),
        }

    def _l3_umos(self) -> set[str]:
        known_l3 = getattr(self.plugin, "_known_l3_umos", None)
        if isinstance(known_l3, dict):
            return {str(umo) for umo in known_l3.values() if str(umo).strip()}
        if isinstance(known_l3, (set, list, tuple)):
            return {str(umo) for umo in known_l3 if str(umo).strip()}
        return set()

    async def _session_entries(self, soul: Any) -> list[dict[str, Any]]:
        """In-memory session overview for the platforms governed by ``soul``."""
        soul_id = str(getattr(soul, "soul_id", "") or "").strip()
        bindings = await self._platform_bindings()
        bound_platforms = await self._bound_platforms_of(soul_id)
        l3_umos = self._l3_umos()
        entries: list[dict[str, Any]] = []
        for umo in self._known_umos_for_ui():
            platform_id = self._platform_id_of(umo)
            if bound_platforms:
                if platform_id not in bound_platforms:
                    continue
            else:
                # Unbound fallback Soul: only sessions whose platform is not owned by
                # another Soul are actually governed by it (contract §6.2).
                owner = bindings.get(platform_id, "")
                if owner and owner != soul_id:
                    continue
            entries.append(self._session_item(umo, bindings, l3_umos))
        entries.sort(key=lambda item: (-int(item["last_active_at"] or 0), item["umo"]))
        return entries

    async def _platforms_payload(self) -> list[dict[str, Any]]:
        """Bot instances (contract §3) merged with Soul bindings + session counters."""
        observed_umos = self._known_umos_for_ui()
        bridge = getattr(self.plugin, "platform_bridge", None)
        lister = getattr(bridge, "list_platform_instances", None)
        raw_instances: list[dict[str, Any]] = []
        if callable(lister):
            try:
                try:
                    value = lister(observed_umos)
                except TypeError:  # legacy / partial helper without the argument
                    value = lister()
                if inspect.isawaitable(value):
                    value = await value
                if isinstance(value, list):
                    raw_instances = [item for item in value if isinstance(item, dict)]
            except Exception as exc:
                logger.warning(f"[OnlyYou] list_platform_instances failed: {exc}")
        if not raw_instances:
            # Defensive fallback: infer platforms from known UMO observations.
            seen: set[str] = set()
            for umo in observed_umos:
                platform_id = self._platform_id_of(umo)
                if platform_id and platform_id not in seen:
                    seen.add(platform_id)
                    raw_instances.append(
                        {
                            "platform_id": platform_id,
                            "platform_type": platform_id,
                            "name": platform_id,
                            "enabled": True,
                            "running": False,
                            "source": "observed",
                        }
                    )

        bindings = await self._platform_bindings()
        name_map = await self._soul_name_map()
        session_counts: dict[str, int] = {}
        for umo in observed_umos:
            platform_id = self._platform_id_of(umo)
            if platform_id:
                session_counts[platform_id] = session_counts.get(platform_id, 0) + 1

        def _item(platform_id: str, raw: dict[str, Any], source: str = "") -> dict[str, Any]:
            bound_soul_id = bindings.get(platform_id, "")
            return {
                "platform_id": platform_id,
                "platform_type": str(raw.get("platform_type") or platform_id),
                "name": str(raw.get("name") or platform_id),
                "enabled": bool(raw.get("enabled", True)),
                "running": bool(raw.get("running", False)),
                "source": source or str(raw.get("source") or "observed"),
                "bound_soul_id": bound_soul_id,
                "bound_soul_name": name_map.get(bound_soul_id, ""),
                "session_count": int(session_counts.get(platform_id, 0) or 0),
            }

        result: list[dict[str, Any]] = []
        seen_pids: set[str] = set()
        for raw in raw_instances:
            platform_id = str(raw.get("platform_id") or "").strip()
            if not platform_id or platform_id in seen_pids:
                continue
            seen_pids.add(platform_id)
            result.append(_item(platform_id, raw))
        # A manually pre-bound platform id may not be reported by AstrBot yet, but the
        # sessions page must still show the binding it just created.
        for platform_id in bindings:
            if platform_id in seen_pids:
                continue
            seen_pids.add(platform_id)
            result.append(
                _item(
                    platform_id,
                    {"platform_id": platform_id, "enabled": True, "running": False},
                    source="observed",
                )
            )
        result.sort(key=lambda item: (not bool(item["running"]), item["platform_id"]))
        return result

    async def _sessions_payload(self, soul: Any) -> dict[str, Any]:
        """Frozen GET ``sessions/platforms`` data payload (contract §6.2)."""
        return {
            "current_soul_id": str(getattr(soul, "soul_id", "") or ""),
            "default_soul_id": await self._safe_default_soul_id(),
            "souls": await self._soul_summaries(),
            "platforms": await self._platforms_payload(),
            "sessions": await self._session_entries(soul),
        }

    async def _group_items(self) -> list[dict[str, Any]]:
        """Frozen ``GroupItem`` list for ``users/list`` (contract §6.2 / §6.3)."""
        manager = getattr(self.plugin, "user_relation", None)
        lister = getattr(manager, "list_groups", None) if manager is not None else None
        if not callable(lister):
            return []
        try:
            groups = lister()
            if inspect.isawaitable(groups):
                groups = await groups
            groups = list(groups or [])
        except Exception as exc:
            logger.warning(f"[OnlyYou] list_groups failed: {exc}")
            return []
        bindings = await self._platform_bindings()
        name_map = await self._soul_name_map()
        items: list[dict[str, Any]] = []
        for group in groups:
            to_dict = getattr(group, "to_dict", None)
            data = dict(to_dict()) if callable(to_dict) else {}
            umo = str(data.get("umo") or getattr(group, "umo", "") or "").strip()
            if not umo:
                continue
            platform_id = str(data.get("platform_id") or "").strip() or self._platform_id_of(umo)
            bound_soul_id = str(data.get("bound_soul_id") or "").strip() or bindings.get(
                platform_id, ""
            )
            items.append(
                {
                    "umo": umo,
                    "group_id": str(data.get("group_id") or "").strip(),
                    "platform_id": platform_id,
                    "display_name": str(data.get("display_name") or "").strip(),
                    "notes": str(data.get("notes") or "").strip(),
                    "bound_soul_id": bound_soul_id,
                    "bound_soul_name": name_map.get(bound_soul_id, ""),
                    "window_count": self._session_window_count(umo),
                    "unread_count": self._session_unread_count(umo),
                    "last_active_at": int(data.get("last_active_at") or 0),
                }
            )
        items.sort(key=lambda item: (-int(item["last_active_at"] or 0), item["umo"]))
        return items

    @staticmethod
    def _counts_from_users(users: list[Any]) -> dict[str, int]:
        return {
            "l3_count": sum(1 for user in users if getattr(user, "tier", None) == UserTier.L3_CORE),
            "l2_count": sum(
                1 for user in users if getattr(user, "tier", None) == UserTier.L2_IMPORTANT
            ),
            "l1_count": sum(
                1 for user in users if getattr(user, "tier", None) == UserTier.L1_NORMAL
            ),
            "total": len(users),
        }

    async def _user_counts(self) -> dict[str, int]:
        manager = getattr(self.plugin, "user_relation", None)
        lister = getattr(manager, "list_users", None) if manager is not None else None
        if not callable(lister):
            return self._counts_from_users([])
        try:
            value = lister(tier_filter=None)
            if inspect.isawaitable(value):
                value = await value
            return self._counts_from_users(list(value or []))
        except Exception as exc:
            logger.warning(f"[OnlyYou] list_users for counts failed: {exc}")
            return self._counts_from_users([])

    def _tier1_limit(self) -> int:
        """``memory_and_context_config.max_tier1_memories`` (default 30, contract §5.6)."""
        config = getattr(self.plugin, "config", None)
        try:
            mem_cfg = config.get("memory_and_context_config") if isinstance(config, dict) else None
        except Exception:  # pragma: no cover - exotic config mapping
            mem_cfg = None
        if not isinstance(mem_cfg, dict):
            return 30
        try:
            return max(1, int(mem_cfg.get("max_tier1_memories", 30)))
        except (TypeError, ValueError):
            return 30

    def _soul_surfing_enabled(self, soul: Any) -> bool:
        """Effective ``surfing_enabled``: Soul setting -> global config -> True."""
        value = getattr(soul, "surfing_enabled", None)
        if isinstance(value, bool):
            return value
        settings = getattr(soul, "settings", None)
        if isinstance(settings, dict) and "surfing_enabled" in settings:
            return _as_bool(settings.get("surfing_enabled"), default=True)
        config = getattr(self.plugin, "config", None)
        try:
            surf_cfg = config.get("surfing_config") if isinstance(config, dict) else None
        except Exception:  # pragma: no cover - exotic config mapping
            surf_cfg = None
        if isinstance(surf_cfg, dict) and "surfing_enabled" in surf_cfg:
            return _as_bool(surf_cfg.get("surfing_enabled"), default=True)
        return True

    async def _clear_umo_memory(self, umo: str) -> dict[str, int]:
        """Drop one session's sliding window / evicted buffer / unread backlog."""
        stats: dict[str, int] = {key: 0 for key in CLEAR_STAT_KEYS}
        buffer = self._session_buffer()
        if buffer is None or not umo:
            return stats
        clear_session = getattr(buffer, "clear_session_memory", None)
        if callable(clear_session):
            try:
                value = clear_session(umo)
                if inspect.isawaitable(value):
                    value = await value
                if isinstance(value, dict):
                    window_count = int(value.get("window") or 0)
                    stats["windows"] = 1 if window_count > 0 else 0
                    stats["evicted"] = int(value.get("evicted") or 0)
                    stats["unread"] = int(value.get("unread") or 0)
                    stats["messages"] = (
                        window_count
                        + int(value.get("evicted") or 0)
                        + int(value.get("unread") or 0)
                    )
                    return stats
            except Exception as exc:
                logger.warning(f"[OnlyYou] clear_session_memory failed: {exc}")
        windows = getattr(buffer, "_windows", None)
        if isinstance(windows, dict):
            removed = windows.pop(umo, None) or []
            stats["windows"] = 1 if removed else 0
            stats["messages"] += len(removed)
        evicted_map = getattr(buffer, "_evicted_buffers", None)
        if isinstance(evicted_map, dict):
            removed = evicted_map.pop(umo, None) or []
            stats["evicted"] = 1 if removed else 0
            stats["messages"] += len(removed)
        clear_unread = getattr(buffer, "clear_unread", None)
        if callable(clear_unread):
            try:
                removed_unread = clear_unread(umo)
                if inspect.isawaitable(removed_unread):
                    removed_unread = await removed_unread
                stats["unread"] = int(removed_unread or 0)
                stats["messages"] += stats["unread"]
            except Exception as exc:
                logger.warning(f"[OnlyYou] clear_unread failed: {exc}")
        return stats

    async def _clear_platform_memory(self, platform_id: str) -> dict[str, int]:
        """Clear all in-memory chat state of one bot platform (contract §4.1)."""
        clean = (platform_id or "").strip()
        if not clean:
            return _normalize_clear_stats(None)
        clearer = getattr(self.plugin, "clear_platform_memory_state", None)
        if callable(clearer):
            try:
                value = clearer(clean)
                if inspect.isawaitable(value):
                    value = await value
                return _normalize_clear_stats(value)
            except Exception as exc:
                logger.warning(f"[OnlyYou] clear_platform_memory_state failed: {exc}")
        # Defensive fallback for a runtime still being written by the Lead.
        buffer = self._session_buffer()
        clearer = getattr(buffer, "clear_platform_memory", None) if buffer is not None else None
        if callable(clearer):
            try:
                value = clearer(clean)
                if inspect.isawaitable(value):
                    value = await value
                return _normalize_clear_stats(value)
            except Exception as exc:
                logger.warning(f"[OnlyYou] session_buffer.clear_platform_memory failed: {exc}")
        return _normalize_clear_stats(None)

    # ==========================================================================
    # Shared helpers for the new Plugin Pages routes: character cards, soul
    # resolution, four-role model status and dashboard counters.
    # ==========================================================================
    @staticmethod
    def _card_schema() -> list[dict[str, Any]]:
        """Return `CARD_SCHEMA` of the contract `core.character_card` module (or [])."""
        module = _character_card_module()
        schema = getattr(module, "CARD_SCHEMA", None) if module is not None else None
        return schema if isinstance(schema, list) else []

    @staticmethod
    def _normalize_card(raw: Any) -> dict[str, Any]:
        """Best-effort `character_card.normalize_card` with a plain-dict fallback."""
        if not isinstance(raw, dict):
            return {}
        module = _character_card_module()
        normalize = getattr(module, "normalize_card", None) if module is not None else None
        if callable(normalize):
            try:
                normalized = normalize(raw)
                if isinstance(normalized, dict):
                    return normalized
            except Exception as exc:
                logger.warning(f"[OnlyYou] normalize_card failed: {exc}")
        return dict(raw)

    @staticmethod
    def _legacy_to_card(profile: Any) -> dict[str, Any]:
        """Map the legacy 4-flat-field SoulProfile payload onto the 10-section card shape."""
        data = profile if isinstance(profile, dict) else {}
        return {
            "identity": {
                "name": str(data.get("name") or ""),
                "tagline": str(data.get("archetype") or ""),
                "avatar": "",
            },
            "appearance": {},
            "personality": {},
            "background": {},
            "interests": {
                "surfing_preferences": _as_str_list(data.get("surfing_preferences")),
            },
            "speech": {"speaking_style": str(data.get("speaking_style") or "")},
            "social": {},
            "rhythm": {},
            "examples": {},
            "extra": {"native_persona_prompt": str(data.get("native_persona_prompt") or "")},
        }

    def _soul_card(self, soul: Any) -> dict[str, Any]:
        """Return the normalized character card of a SoulProfile (legacy migration included)."""
        if soul is None:
            return self._normalize_card({})
        card = getattr(soul, "card", None)
        if isinstance(card, dict) and card:
            return self._normalize_card(dict(card))
        legacy: Any = {}
        to_dict = getattr(soul, "to_dict", None)
        if callable(to_dict):
            try:
                legacy = to_dict()
            except Exception:
                legacy = {}
        module = _character_card_module()
        migrate = getattr(module, "migrate_legacy", None) if module is not None else None
        if callable(migrate):
            try:
                migrated = migrate(legacy if isinstance(legacy, dict) else {})
                if isinstance(migrated, dict):
                    return migrated
            except Exception as exc:
                logger.warning(f"[OnlyYou] migrate_legacy failed: {exc}")
        return self._normalize_card(self._legacy_to_card(legacy))

    @staticmethod
    def _apply_legacy_flat_fields(card: dict[str, Any], body: dict[str, Any]) -> None:
        """Overlay legacy flat body fields (`name/archetype/...`) onto a card structure."""
        identity = _ensure_section(card, "identity")
        interests = _ensure_section(card, "interests")
        speech = _ensure_section(card, "speech")
        extra = _ensure_section(card, "extra")
        if "name" in body:
            identity["name"] = str(body.get("name") or "").strip()
        if "archetype" in body:
            identity["tagline"] = str(body.get("archetype") or "").strip()
        if "speaking_style" in body:
            speech["speaking_style"] = str(body.get("speaking_style") or "").strip()
        if "native_persona_prompt" in body:
            extra["native_persona_prompt"] = str(body.get("native_persona_prompt") or "").strip()
        if "surfing_preferences" in body:
            interests["surfing_preferences"] = _as_str_list(body.get("surfing_preferences"))

    @staticmethod
    def _fill_new_soul_defaults(card: dict[str, Any], soul_id: str) -> None:
        """Keep the legacy `souls/save` defaults when creating a brand-new soul."""
        identity = _ensure_section(card, "identity")
        interests = _ensure_section(card, "interests")
        speech = _ensure_section(card, "speech")
        if not str(identity.get("name") or "").strip():
            identity["name"] = soul_id
        if not str(identity.get("tagline") or "").strip():
            identity["tagline"] = "有自己生活节律与喜怒哀乐的数字网友"
        if not str(speech.get("speaking_style") or "").strip():
            speech["speaking_style"] = "口语化短句，自然随性"
        if not _as_str_list(interests.get("surfing_preferences")):
            interests["surfing_preferences"] = ["搞笑整活", "萌宠日常", "美食探店"]

    def _card_flat_fields(
        self, card: dict[str, Any], existing: Any = None, soul_id: str = ""
    ) -> dict[str, Any]:
        """Derive legacy flat SoulProfile attributes from a character card (compat shim)."""
        identity = card.get("identity") if isinstance(card.get("identity"), dict) else {}
        speech = card.get("speech") if isinstance(card.get("speech"), dict) else {}
        interests = card.get("interests") if isinstance(card.get("interests"), dict) else {}
        extra = card.get("extra") if isinstance(card.get("extra"), dict) else {}

        def _pick(new_value: Any, attr: str, default: str) -> str:
            text = str(new_value or "").strip()
            if text:
                return text
            old = getattr(existing, attr, None) if existing is not None else None
            if isinstance(old, str) and old.strip():
                return old.strip()
            return default

        prefs = _as_str_list(interests.get("surfing_preferences"))
        if not prefs and existing is not None:
            old_prefs = getattr(existing, "surfing_preferences", None)
            if isinstance(old_prefs, (list, tuple)):
                prefs = [str(item).strip() for item in old_prefs if str(item).strip()]

        return {
            "name": _pick(identity.get("name"), "name", soul_id or "小星"),
            "archetype": _pick(
                identity.get("tagline"), "archetype", "有自己生活节律与喜怒哀乐的数字网友"
            ),
            "speaking_style": _pick(
                speech.get("speaking_style"), "speaking_style", "口语化短句，自然随性"
            ),
            "native_persona_prompt": _pick(
                extra.get("native_persona_prompt"), "native_persona_prompt", ""
            ),
            "surfing_preferences": prefs,
        }

    def _apply_card_to_profile(self, profile: Any, card: dict[str, Any]) -> Any:
        """Write a normalized card onto a SoulProfile (legacy flat attrs synced when needed)."""
        normalized = self._normalize_card(card)
        had_card_dict = isinstance(getattr(profile, "card", None), dict)
        try:
            setattr(profile, "card", normalized)
        except Exception as exc:
            logger.warning(f"[OnlyYou] Failed to assign card to SoulProfile: {exc}")

        if not had_card_dict:
            # Pre-refactor SoulProfile (flat dataclass): keep flat attrs in sync so that
            # the old implementation can still persist/render the profile.
            flat = self._card_flat_fields(
                normalized, profile, str(getattr(profile, "soul_id", "") or "")
            )
            for attr in ("name", "archetype", "speaking_style", "native_persona_prompt"):
                try:
                    setattr(profile, attr, flat[attr])
                except Exception:
                    pass
            try:
                setattr(profile, "surfing_preferences", list(flat["surfing_preferences"]))
            except Exception:
                pass
        return profile

    async def _resolve_soul(self, soul_id: str = "") -> Any:
        """Resolve the requested / default / most-recent soul (never returns None)."""
        manager = getattr(self.plugin, "soul_manager", None)
        if manager is None:
            raise RuntimeError("SoulManager 未初始化")
        sid = (soul_id or "").strip()
        soul = None
        if sid:
            soul = await manager.get_soul_by_id(sid)
        if soul is None:
            getter = getattr(manager, "get_default_soul_id", None)
            if callable(getter):
                try:
                    default_id = str(await getter() or "").strip()
                except Exception as exc:
                    logger.warning(f"[OnlyYou] get_default_soul_id failed: {exc}")
                    default_id = ""
                if default_id:
                    soul = await manager.get_soul_by_id(default_id)
        if soul is None:
            souls = await manager.list_souls()
            soul = souls[0] if souls else None
        if soul is None:
            resolver = getattr(manager, "resolve_soul", None)
            if callable(resolver):
                try:
                    soul = await resolver(allow_llm_seed=False)
                except TypeError:
                    soul = await resolver()
                except Exception as exc:
                    logger.warning(f"[OnlyYou] resolve_soul fallback failed: {exc}")
        if soul is None:
            raise RuntimeError("尚未创建任何角色")
        return soul

    def _provider_factory(self) -> Any:
        """Return the contract `ProviderFactory` instance created by main.py."""
        factory = getattr(self.plugin, "provider_factory", None)
        if factory is None:
            raise RuntimeError("ProviderFactory 未初始化（请检查 main.py 接线）")
        return factory

    async def _safe_model_status(self, soul_id: Optional[str] = None) -> dict[str, Any]:
        """``await model_hub.get_status(soul_id)`` with graceful degradation to ``{}``."""
        hub = getattr(self.plugin, "model_hub", None)
        if hub is None:
            return {}
        getter = getattr(hub, "get_status", None)
        if not callable(getter):
            return {}
        try:
            try:
                value = getter(soul_id=soul_id) if soul_id else getter()
            except TypeError:
                # Legacy/partial signature: fall back to the positional/no-arg call.
                value = getter(soul_id) if soul_id else getter()
            if inspect.isawaitable(value):
                value = await value
        except Exception as exc:
            logger.warning(f"[OnlyYou] model_hub.get_status failed: {exc}")
            return {}
        return value if isinstance(value, dict) else {}

    async def _safe_default_soul_id(self) -> str:
        """Read `plugin_settings.default_soul_id` through the contract SoulManager API."""
        manager = getattr(self.plugin, "soul_manager", None)
        getter = getattr(manager, "get_default_soul_id", None) if manager is not None else None
        if not callable(getter):
            return ""
        try:
            return str(await getter() or "").strip()
        except Exception as exc:
            logger.warning(f"[OnlyYou] get_default_soul_id failed: {exc}")
            return ""

    async def _read_model_config(
        self, hub: Any, soul_id: Optional[str] = None
    ) -> dict[str, Any]:
        """Read the four-role model config (soul-scoped when ``soul_id`` is given)."""
        for attr in (
            "get_config",
            "get_config_sync",
            "get_model_config_async",
            "get_model_config",
        ):
            getter = getattr(hub, attr, None)
            if not callable(getter):
                continue
            try:
                try:
                    value = getter(soul_id=soul_id) if soul_id else getter()
                except TypeError:
                    # Legacy / partial signature without the `soul_id` keyword.
                    value = getter()
                if inspect.isawaitable(value):
                    value = await value
                if isinstance(value, dict):
                    return value
            except Exception as exc:
                logger.warning(f"[OnlyYou] model_hub.{attr} failed: {exc}")
        return {}

    async def _save_hub_model_config(
        self, hub: Any, cfg: dict[str, Any], soul_id: Optional[str] = None
    ) -> dict[str, Any]:
        """Save the four-role model config through ModelHub (contract §2.2, soul-aware)."""
        for attr in ("save_config", "save_model_config"):
            saver = getattr(hub, attr, None)
            if not callable(saver):
                continue
            try:
                try:
                    value = saver(cfg, soul_id=soul_id) if soul_id else saver(cfg)
                except TypeError:
                    # Legacy / partial signature without the `soul_id` keyword.
                    value = saver(cfg)
                if inspect.isawaitable(value):
                    value = await value
                return value if isinstance(value, dict) else {}
            except Exception as exc:
                raise RuntimeError(f"保存模型配置失败: {exc}") from exc
        raise RuntimeError("ModelHub 未实现 save_model_config")

    @staticmethod
    def _apply_soul_passthrough(profile: Any, body: dict[str, Any]) -> None:
        """v2.8 ``souls/save`` compatibility: optional model/binding/settings passthrough."""
        if not isinstance(body, dict):
            return
        raw_model = body.get("model_config")
        if isinstance(raw_model, dict) and raw_model:
            normalized: dict[str, Any] = dict(raw_model)
            try:
                from .model_hub import normalize_model_config

                normalized = normalize_model_config(raw_model)
            except Exception:  # pragma: no cover - partial model_hub during refactor
                normalized = dict(raw_model)
            try:
                profile.model_config = normalized
            except Exception:  # pragma: no cover - defensive only
                pass
        raw_platforms = body.get("bound_platforms")
        if raw_platforms is not None:
            platforms = list(dict.fromkeys(_as_str_list(raw_platforms)))
            try:
                profile.bound_platforms = platforms
            except Exception:  # pragma: no cover - defensive only
                pass
        raw_settings = body.get("settings")
        if isinstance(raw_settings, dict):
            try:
                settings = dict(getattr(profile, "settings", None) or {})
            except Exception:  # pragma: no cover - defensive only
                settings = {}
            settings.update(raw_settings)
            try:
                profile.settings = settings
            except Exception:  # pragma: no cover - defensive only
                pass

    async def _relation_counts(self) -> dict[str, int]:
        """Dashboard relation counters derived from `user_relation.list_users()`."""
        counts = {"l1": 0, "l2": 0, "l3": 0, "total": 0}
        manager = getattr(self.plugin, "user_relation", None)
        if manager is None:
            return counts
        try:
            users = list(await manager.list_users() or [])
        except Exception as exc:
            logger.warning(f"[OnlyYou] dashboard relation counts failed: {exc}")
            return counts
        counts["total"] = len(users)
        for user in users:
            try:
                tier = int(getattr(user, "tier", 0) or 0)
            except (TypeError, ValueError):
                continue
            if tier == int(UserTier.L3_CORE):
                counts["l3"] += 1
            elif tier == int(UserTier.L2_IMPORTANT):
                counts["l2"] += 1
            elif tier == int(UserTier.L1_NORMAL):
                counts["l1"] += 1
        return counts

    async def _memory_counts(self, soul_id: str) -> dict[str, int]:
        """Dashboard tiered-memory counters derived from `memory_manager.list_memories()`."""
        counts = {"tier1": 0, "tier2": 0, "tier3": 0, "total": 0}
        manager = getattr(self.plugin, "memory_manager", None)
        if manager is None:
            return counts
        try:
            memories = list(await manager.list_memories(soul_id=soul_id, limit=1000) or [])
        except Exception as exc:
            logger.warning(f"[OnlyYou] dashboard memory counts failed: {exc}")
            return counts
        counts["total"] = len(memories)
        for item in memories:
            try:
                tier = int(getattr(item, "memory_tier", 0) or 0)
            except (TypeError, ValueError):
                continue
            if tier == int(MemoryTier.CORE):
                counts["tier3"] += 1
            elif tier == int(MemoryTier.RECENT):
                counts["tier2"] += 1
            elif tier == int(MemoryTier.EPHEMERAL):
                counts["tier1"] += 1
        return counts

    async def _video_count(self, soul_id: str) -> int:
        """Dashboard surfed-video counter derived from `surfing_pipeline.list_history()`."""
        pipeline = getattr(self.plugin, "surfing_pipeline", None)
        if pipeline is None:
            return 0
        try:
            videos = await pipeline.list_history(soul_id=soul_id, limit=1000)
        except Exception as exc:
            logger.warning(f"[OnlyYou] dashboard video count failed: {exc}")
            return 0
        return len(videos or [])

    # ==========================================================================
    # 1. Init Summary & Soul Studio (`init_summary`, `souls/*`)
    # ==========================================================================
    async def api_init_summary(self) -> dict[str, Any]:
        try:
            await self.plugin._ensure_initialized()
            souls = await self.plugin.soul_manager.list_souls()
            if not souls:
                # Fresh install: lazily materialize the default soul so Pages never
                # boot into an empty/500 state.
                resolver = getattr(self.plugin.soul_manager, "resolve_soul", None)
                if callable(resolver):
                    try:
                        await resolver(allow_llm_seed=False)
                    except TypeError:
                        await resolver()
                    except Exception as exc:
                        logger.warning(f"[OnlyYou] api_init_summary resolve_soul failed: {exc}")
                    souls = await self.plugin.soul_manager.list_souls()
            if not souls:
                return err_response("尚未创建任何角色，请先在角色卡页新建角色")

            native_personas = await self.plugin.soul_manager.list_native_personas()
            bindings = await self.plugin.soul_manager.get_umo_bindings()
            active_soul = souls[0]
            sched_payload = await self._build_schedule_payload(active_soul)
            model_status = await self._safe_model_status()
            default_soul_id = await self._safe_default_soul_id()
            platform_bindings = await self._platform_bindings()

            return ok_response(
                {
                    "souls": [s.to_dict() for s in souls],
                    "native_personas": native_personas,
                    "umo_bindings": bindings,
                    # v2.8: {platform_id: soul_id} bot-instance ownership (contract §6.2).
                    "platform_bindings": platform_bindings,
                    "known_umos": self._collect_known_umos(bindings),
                    "active_soul_id": active_soul.soul_id,
                    "active_schedule": sched_payload,
                    "default_soul_id": default_soul_id,
                    "model_status": model_status,
                    "action_types": ACTION_TYPE_DEFINITIONS,
                }
            )
        except Exception as e:
            logger.warning(f"[OnlyYou] api_init_summary error: {e}")
            return err_response(f"初始化摘要加载失败: {e}")

    async def api_souls_detail(self) -> dict[str, Any]:
        try:
            await self.plugin._ensure_initialized()
            soul_id = self._get_query_param("soul_id")
            soul = (
                await self.plugin.soul_manager.get_soul_by_id(soul_id)
                if soul_id
                else None
            )
            if soul is None:
                souls = await self.plugin.soul_manager.list_souls()
                soul = souls[0]

            sched_payload = await self._build_schedule_payload(soul)
            bindings = await self.plugin.soul_manager.get_umo_bindings()
            return ok_response(
                {
                    "soul": soul.to_dict(),
                    "card_schema": self._card_schema(),
                    "schedule": sched_payload,
                    "umo_bindings": bindings,
                    # v2.8 data extension (contract §6.1): platform ownership map.
                    "platform_bindings": await self._platform_bindings(),
                    "known_umos": self._collect_known_umos(bindings),
                }
            )
        except Exception as e:
            logger.warning(f"[OnlyYou] api_souls_detail error: {e}")
            return err_response(f"加载角色详情失败: {e}")

    async def api_souls_import_persona(self) -> dict[str, Any]:
        try:
            await self.plugin._ensure_initialized()
            body = await self._get_json_body()
            persona_name = str(body.get("persona_name") or "default_soul").strip()
            target_soul_id = str(body.get("target_soul_id") or persona_name).strip()
            auto_gen = bool(body.get("auto_generate_cards", True))
            ai_generate_card = bool(body.get("ai_generate_card", False))
            override_prompt = str(body.get("native_prompt") or "").strip() or None
            # v2.6 §5.1: optional character avatar (emoji / image URL / data URL).
            avatar = str(body.get("avatar") or "").strip()

            importer = self.plugin.soul_manager.import_native_persona
            try:
                importer_params = inspect.signature(importer).parameters
            except (TypeError, ValueError):
                importer_params = {}
            import_kwargs: dict[str, Any] = {
                "persona_name": persona_name,
                "target_soul_id": target_soul_id,
                "auto_generate_cards": auto_gen,
                "override_prompt": override_prompt,
            }
            # The rewritten SoulManager supports optional AI card elaboration; keep
            # the legacy call signature working if the parameter is absent.
            if "ai_generate_card" in importer_params:
                import_kwargs["ai_generate_card"] = ai_generate_card
            # Same tolerant pass-through for the v2.6 `avatar` parameter.
            if "avatar" in importer_params:
                import_kwargs["avatar"] = avatar
            profile = await importer(**import_kwargs)
            # Build fresh schedule run with the newly imported deck
            await self.plugin.deck_engine.get_or_build_today_run(
                soul_id=profile.soul_id,
                custom_cards=profile.state_event_cards,
                force_reroll=True,
                chronotype=self._soul_chronotype(profile),
            )
            sched_payload = await self._build_schedule_payload(profile)
            souls = await self.plugin.soul_manager.list_souls()
            return ok_response(
                {
                    "soul": profile.to_dict(),
                    "schedule": sched_payload,
                    "souls": [s.to_dict() for s in souls],
                },
                message=f"已成功导入角色 {profile.name} ({profile.soul_id})",
            )
        except Exception as e:
            logger.warning(f"[OnlyYou] api_souls_import_persona error: {e}")
            return err_response(f"导入人格失败: {e}")

    async def api_souls_save(self) -> dict[str, Any]:
        try:
            await self.plugin._ensure_initialized()
            body = await self._get_json_body()
            soul_id = str(body.get("soul_id") or "").strip()
            if not soul_id:
                return err_response("Soul ID 不能为空")

            manager = getattr(self.plugin, "soul_manager", None)
            if manager is None:
                return err_response("SoulManager 未初始化")
            existing = await manager.get_soul_by_id(soul_id)
            body_card = body.get("card")
            module = _character_card_module(log_missing=isinstance(body_card, dict))

            if isinstance(body_card, dict):
                # New Plugin Pages payload: the full character card takes precedence
                # over the legacy flat fields.
                card = self._normalize_card(body_card)
            else:
                # Legacy payload (flat `name/archetype/...`): keep the existing card
                # intact and overlay only the provided legacy fields.
                card: dict[str, Any] = {}
                if existing is not None:
                    card = dict(self._soul_card(existing))
                elif module is not None:
                    empty = getattr(module, "empty_card", None)
                    if callable(empty):
                        try:
                            empty_card = empty()
                            if isinstance(empty_card, dict):
                                card = empty_card
                        except Exception as exc:
                            logger.warning(f"[OnlyYou] empty_card failed: {exc}")
                self._apply_legacy_flat_fields(card, body)
                card = self._normalize_card(card)
                if existing is None:
                    self._fill_new_soul_defaults(card, soul_id)

            if existing is not None:
                profile = self._apply_card_to_profile(existing, card)
            else:
                payload: dict[str, Any] = {"soul_id": soul_id, "card": card}
                payload.update(self._card_flat_fields(card, existing=None, soul_id=soul_id))
                payload["state_event_cards"] = []
                profile = SoulProfile.from_dict(payload)
                profile = self._apply_card_to_profile(profile, card)

            # v2.8 §6.2: tolerate optional soul-scoped config passthrough fields.
            self._apply_soul_passthrough(profile, body)
            await manager.save_soul(profile)

            # Optional UMO routing binding updates (legacy console contract)
            if isinstance(body.get("umo_binding"), dict):
                ub = body["umo_binding"]
                await manager.set_umo_binding(
                    str(ub.get("umo") or ""), str(ub.get("soul_id") or "")
                )

            souls = await manager.list_souls()
            bindings = await manager.get_umo_bindings()
            to_dict = getattr(profile, "to_dict", None)
            profile_dict = to_dict() if callable(to_dict) else {}
            profile_name = str(getattr(profile, "name", "") or soul_id)
            # v2.8 hardening: surface a degraded JSON write instead of silently
            # reporting success when only the SQLite index could be updated.
            write_error = str(
                getattr(manager, "last_file_write_error", "") or ""
            ).strip()
            storage = {
                "file_written": not bool(write_error),
                "degraded": bool(write_error),
                "error": write_error,
            }
            message = f"已保存角色 {profile_name} ({soul_id})"
            if write_error:
                message += (
                    "（⚠️ Soul JSON 写入失败，已降级写入 SQLite 索引，"
                    f"读取/重启时会自动重试落盘：{write_error}）"
                )
            return ok_response(
                {
                    "soul": profile_dict,
                    "souls": [s.to_dict() for s in souls],
                    "umo_bindings": bindings,
                    "known_umos": self._collect_known_umos(bindings),
                    "storage": storage,
                },
                message=message,
            )
        except Exception as e:
            logger.warning(f"[OnlyYou] api_souls_save error: {e}")
            return err_response(f"保存角色失败: {e}")

    async def api_souls_rename(self) -> dict[str, Any]:
        try:
            await self.plugin._ensure_initialized()
            body = await self._get_json_body()
            old_id = str(body.get("old_soul_id") or "").strip()
            new_id = str(body.get("new_soul_id") or "").strip()
            profile = await self.plugin.soul_manager.rename_soul(old_id, new_id)
            # Clear schedule cache for old_id
            for k in list(self.plugin.deck_engine._cache.keys()):
                if k[0] == old_id:
                    self.plugin.deck_engine._cache.pop(k, None)

            souls = await self.plugin.soul_manager.list_souls()
            sched_payload = await self._build_schedule_payload(profile)
            return ok_response(
                {
                    "soul": profile.to_dict(),
                    "souls": [s.to_dict() for s in souls],
                    "schedule": sched_payload,
                },
                message=f"已将 Soul ID 从 {old_id} 修改为 {new_id}",
            )
        except Exception as e:
            return err_response(str(e))

    async def api_souls_delete(self) -> dict[str, Any]:
        try:
            await self.plugin._ensure_initialized()
            body = await self._get_json_body()
            soul_id = str(body.get("soul_id") or "").strip()
            if not soul_id:
                return err_response("缺少 soul_id")
            await self.plugin.soul_manager.delete_soul(soul_id)
            for k in list(self.plugin.deck_engine._cache.keys()):
                if k[0] == soul_id:
                    self.plugin.deck_engine._cache.pop(k, None)
            souls = await self.plugin.soul_manager.list_souls()
            return ok_response(
                {
                    "deleted_soul_id": soul_id,
                    "souls": [s.to_dict() for s in souls],
                    # `list_souls()` recreates a default soul when the last one is
                    # deleted; stay defensive anyway and never index an empty list.
                    "active_soul_id": souls[0].soul_id if souls else "",
                },
                message=f"已删除角色 {soul_id}",
            )
        except Exception as e:
            return err_response(f"删除角色失败: {e}")

    # ==========================================================================
    # 2. Schedule Control & Action Cards (`schedule/*`, `cards/*`)
    # ==========================================================================
    async def api_schedule_reroll(self) -> dict[str, Any]:
        try:
            await self.plugin._ensure_initialized()
            body = await self._get_json_body()
            soul_id = str(body.get("soul_id") or "default_soul").strip()
            soul = await self.plugin.soul_manager.get_soul_by_id(soul_id)
            if soul is None:
                soul = (await self.plugin.soul_manager.list_souls())[0]

            await self.plugin.deck_engine.get_or_build_today_run(
                soul_id=soul.soul_id,
                custom_cards=soul.state_event_cards,
                force_reroll=True,
                chronotype=self._soul_chronotype(soul),
            )
            sched_payload = await self._build_schedule_payload(soul)
            return ok_response(
                {"schedule": sched_payload},
                message="已重新抽取今日 24h 动态日程",
            )
        except Exception as e:
            return err_response(f"重抽日程失败: {e}")

    async def api_schedule_force_state(self) -> dict[str, Any]:
        try:
            await self.plugin._ensure_initialized()
            body = await self._get_json_body()
            soul_id = str(body.get("soul_id") or "default_soul").strip()
            soul = await self.plugin.soul_manager.get_soul_by_id(soul_id)
            if soul is None:
                soul = (await self.plugin.soul_manager.list_souls())[0]

            # Optional four-action override (contract §6.4): when present it wins
            # over the legacy `new_state` payload and the bound state is derived.
            raw_action = str(body.get("action_type") or "").strip().upper()
            action_type: Optional[ActionType] = None
            if raw_action:
                action_type = ActionType.coerce(raw_action, None)
                if action_type is None:
                    return err_response(
                        "行动类型必须是 SLEEP、SURF、IDLE_ROAM 或 BUSY_TASK 之一"
                    )

            new_state: Optional[BotState] = None
            if action_type is None:
                raw_state = str(body.get("new_state") or "IDLE").strip().upper()
                if raw_state not in BotState.__members__:
                    return err_response("状态必须是 RESTING、BUSY 或 IDLE 之一")
                new_state = BotState(raw_state)

            new_title = str(body.get("new_title") or "").strip() or None
            # v2.6 §3.4: armor is a fixed per-action tier now (ActionType.fixed_armor),
            # so the legacy `armor` request field is intentionally ignored / not passed.
            slot_key = str(body.get("slot_key") or "").strip() or None

            updated_slot = await self.plugin.deck_engine.force_set_current_state(
                soul_id=soul.soul_id,
                new_state=new_state,
                new_title=new_title,
                slot_key=slot_key,
                action_type=action_type,
            )
            sched_payload = await self._build_schedule_payload(soul)
            updated_action = _slot_action_type(updated_slot) or ActionType.IDLE_ROAM
            updated_state = getattr(updated_slot, "state", None)
            state_value = (
                updated_state.value
                if isinstance(updated_state, BotState)
                else str(updated_state or updated_action.bound_state.value)
            )
            return ok_response(
                {
                    "updated_slot": _enrich_slot(updated_slot),
                    "schedule": sched_payload,
                },
                message=(
                    f"已将时段【{getattr(updated_slot, 'label', '')}】切换为 "
                    f"{updated_action.label} {state_value} ({getattr(updated_slot, 'title', '')})"
                ),
            )
        except Exception as e:
            return err_response(f"切换状态失败: {e}")

    async def api_cards_list(self) -> dict[str, Any]:
        try:
            await self.plugin._ensure_initialized()
            soul_id = self._get_query_param("soul_id")
            soul = (
                await self.plugin.soul_manager.get_soul_by_id(soul_id)
                if soul_id
                else None
            )
            if soul is None:
                soul = (await self.plugin.soul_manager.list_souls())[0]
            return await self.api_cards_list_for_soul(soul)
        except Exception as e:
            return err_response(f"获取卡片池失败: {e}")

    async def api_cards_save(self) -> dict[str, Any]:
        try:
            await self.plugin._ensure_initialized()
            body = await self._get_json_body()
            soul_id = str(body.get("soul_id") or "default_soul").strip()
            soul = await self.plugin.soul_manager.get_soul_by_id(soul_id)
            if soul is None:
                soul = (await self.plugin.soul_manager.list_souls())[0]

            card_type = str(body.get("card_type") or "action").strip().lower()
            scope = str(body.get("scope") or "soul").strip().lower()

            if card_type == "encounter":
                # Encounter-talk cards were removed from the product (contract §6.4).
                return err_response("奇遇谈资卡已从系统中移除")

            card_data = _card_payload_from_body(body)
            card = StateEventCard.from_dict(card_data)
            if not card.card_id:
                card.card_id = f"evt_{int(time.time())}_{random.randint(100, 999)}"
            # v2.6 §3.4: armor is a fixed per-action tier; any client-supplied legacy
            # `armor` value is overwritten (never clamped/trusted).
            card.armor = card.action.fixed_armor
            card.base_weight = max(1, min(100, int(card.base_weight)))
            # `from_dict` already derives the bound `BotState`; keep it explicit so a
            # legacy `state` field can never desync from the four action types.
            card.state = card.action.bound_state

            target_deck = (
                DEFAULT_STATE_EVENT_CARDS
                if scope == "global"
                else soul.state_event_cards
            )
            replaced = False
            for i, existing in enumerate(target_deck):
                if existing.card_id == card.card_id:
                    target_deck[i] = card
                    replaced = True
                    break
            if not replaced:
                target_deck.insert(0, card)
            await self.plugin.soul_manager.save_soul(soul)

            return await self.api_cards_list_for_soul(soul)
        except Exception as e:
            return err_response(f"保存卡片失败: {e}")

    async def api_cards_delete(self) -> dict[str, Any]:
        try:
            await self.plugin._ensure_initialized()
            body = await self._get_json_body()
            soul_id = str(body.get("soul_id") or "default_soul").strip()
            soul = await self.plugin.soul_manager.get_soul_by_id(soul_id)
            if soul is None:
                soul = (await self.plugin.soul_manager.list_souls())[0]

            card_type = str(body.get("card_type") or "action").strip().lower()
            scope = str(body.get("scope") or "soul").strip().lower()

            if card_type == "encounter":
                return err_response("奇遇谈资卡已从系统中移除")

            card_id = str(body.get("card_id") or "").strip()
            if not card_id:
                return err_response("缺少 card_id")
            target_deck = (
                DEFAULT_STATE_EVENT_CARDS
                if scope == "global"
                else soul.state_event_cards
            )
            target_deck[:] = [c for c in target_deck if c.card_id != card_id]
            await self.plugin.soul_manager.save_soul(soul)

            return await self.api_cards_list_for_soul(soul)
        except Exception as e:
            return err_response(f"删除卡片失败: {e}")

    async def api_cards_ai_generate(self) -> dict[str, Any]:
        try:
            await self.plugin._ensure_initialized()
            body = await self._get_json_body()
            soul_id = str(body.get("soul_id") or "default_soul").strip()
            auto_save = bool(body.get("auto_save", True))
            soul = await self.plugin.soul_manager.get_soul_by_id(soul_id)
            if soul is None:
                soul = (await self.plugin.soul_manager.list_souls())[0]

            prompt = (
                f"请为角色【{soul.name}】（人设定位：{soul.archetype}；口癖：{soul.speaking_style}；"
                f"冲浪喜好：{', '.join(soul.surfing_preferences) or '无特别偏好'}）"
                f"生成 8~12 张贴合该角色身份与爱好的专属行动卡，必须覆盖四类行动：\n"
                f"- SLEEP（睡觉 / 休眠，绑定 RESTING）：长睡 4.0~7.5 小时，或小憩 1.0~2.0 小时\n"
                f"- SURF（冲浪刷片，绑定 IDLE）：0.5~2.0 小时\n"
                f"- IDLE_ROAM（空闲闲逛，绑定 IDLE）：0.5~2.0 小时\n"
                f"- BUSY_TASK（繁忙主业，绑定 BUSY）：1.0~3.0 小时\n"
                f"每张卡需要：title（贴合角色的活动标题）、action_type、min_hours、max_hours"
                f"（0.5 步长，min_hours <= max_hours）、base_weight（1~100）。\n"
                '仅输出合法 JSON：{"state_event_cards": [{"card_id": "evt_ai_1", "title": "...", '
                '"action_type": "BUSY_TASK", "min_hours": 1.5, "max_hours": 3.0, '
                '"base_weight": 55}]}'
            )
            data = await self.plugin.model_hub.call_llm_json(
                prompt=prompt, role="decision", default=None
            )
            gen_cards: list[StateEventCard] = []
            if isinstance(data, dict) and data.get("state_event_cards"):
                for raw_c in data.get("state_event_cards") or []:
                    if not isinstance(raw_c, dict):
                        continue
                    try:
                        gen_cards.append(StateEventCard.from_dict(raw_c))
                    except Exception as exc:  # never let one bad card break the batch
                        logger.warning(f"[OnlyYou] AI card parse skipped: {exc}")

            persona_prompt = str(getattr(soul, "native_persona_prompt", "") or "")
            if not persona_prompt:
                persona_prompt = f"{soul.name} {soul.archetype} {soul.speaking_style}".strip()
            if not gen_cards:
                fallback = self.plugin.soul_manager.build_fallback_custom_deck(
                    soul.name, persona_prompt
                )
                # New signature returns `list[StateEventCard]`; tolerate a legacy tuple.
                if isinstance(fallback, tuple):
                    fallback = fallback[0] if fallback else []
                gen_cards = [c for c in (fallback or []) if isinstance(c, StateEventCard)]
            gen_cards = gen_cards[:12]
            # v2.6 §3.2/§3.4: armor is derived from the action type, never from the
            # LLM reply nor the fallback spec list.
            for card in gen_cards:
                card.armor = card.action.fixed_armor
            # Rewrite card ids so every generated card is unique per batch.
            stamp = int(time.time())
            for idx, card in enumerate(gen_cards):
                card.card_id = f"evt_ai_{stamp}_{idx + 1}"

            if auto_save:
                existing_titles = {c.title for c in soul.state_event_cards}
                for card in gen_cards:
                    if card.title not in existing_titles:
                        soul.state_event_cards.insert(0, card)
                        existing_titles.add(card.title)
                await self.plugin.soul_manager.save_soul(soul)

            payload = await self.api_cards_list_for_soul(soul)
            payload["data"]["generated_cards"] = [c.to_dict() for c in gen_cards]
            payload["message"] = f"已为 {soul.name} 生成 {len(gen_cards)} 张专属行动卡"
            return payload
        except Exception as e:
            return err_response(f"AI 生成卡片失败: {e}")

    async def api_cards_list_for_soul(self, soul: SoulProfile) -> dict[str, Any]:
        return ok_response(
            {
                "soul_id": soul.soul_id,
                "custom_cards": [
                    {**c.to_dict(), "scope": "soul"} for c in soul.state_event_cards
                ],
                "general_cards": [
                    {**c.to_dict(), "scope": "global"} for c in DEFAULT_STATE_EVENT_CARDS
                ],
                "action_types": ACTION_TYPE_DEFINITIONS,
                "chronotype": self._soul_chronotype(soul),
            }
        )

    # ==========================================================================
    # 3. Three-Tier Relationships (`users/*`)
    # ==========================================================================
    async def api_users_list(self) -> dict[str, Any]:
        try:
            await self.plugin._ensure_initialized()
            tier_str = self._get_query_param("tier")
            tier_filter = int(tier_str) if tier_str in ("1", "2", "3") else None
            users = await self.plugin.user_relation.list_users(tier_filter=tier_filter)
            all_users = (
                users
                if tier_filter is None
                else await self.plugin.user_relation.list_users(tier_filter=None)
            )
            return ok_response(
                {
                    "users": [u.to_dict() for u in users],
                    "counts": self._counts_from_users(list(all_users)),
                    # v2.8: discovered group chats (contract §6.2 GroupItem).
                    "groups": await self._group_items(),
                }
            )
        except Exception as e:
            return err_response(f"获取用户关系列表失败: {e}")

    async def _handle_group_action(self, action: str, body: dict[str, Any]) -> dict[str, Any]:
        """v2.8 ``users/update`` group actions (contract §6.2)."""
        manager = getattr(self.plugin, "user_relation", None)
        if manager is None:
            return err_response("UserRelationManager 未初始化")
        umo = str(body.get("umo") or "").strip()
        if not umo:
            return err_response("群聊 UMO 不能为空")

        if action == "update_group":
            updater = getattr(manager, "update_group", None)
            if not callable(updater):
                return err_response("UserRelationManager 未实现 update_group")
            group = updater(
                umo,
                display_name=body.get("display_name"),
                notes=body.get("notes"),
            )
            if inspect.isawaitable(group):
                group = await group
            if group is None:
                return err_response(f"未找到群聊档案: {umo}")
            message = f"已更新群聊 {getattr(group, 'display_name', '') or umo}"
        elif action == "clear_group_memory":
            cleared = await self._clear_umo_memory(umo)
            message = (
                f"已清空群聊 {umo} 的内存滑窗与未读"
                f"（{cleared['messages']} 条消息）"
            )
        else:  # delete_group
            deleter = getattr(manager, "delete_group", None)
            if not callable(deleter):
                return err_response("UserRelationManager 未实现 delete_group")
            deleted = deleter(umo)
            if inspect.isawaitable(deleted):
                deleted = await deleted
            if not deleted:
                return err_response(f"未找到群聊档案: {umo}")
            message = f"已移除群聊档案 {umo}"

        return ok_response(
            {
                "groups": await self._group_items(),
                "counts": await self._user_counts(),
            },
            message=message,
        )

    async def api_users_update(self) -> dict[str, Any]:
        try:
            await self.plugin._ensure_initialized()
            body = await self._get_json_body()
            action = str(body.get("action") or "update").strip().lower()

            # v2.8 group profile actions (contract §6.2).
            if action in ("update_group", "clear_group_memory", "delete_group"):
                return await self._handle_group_action(action, body)

            if action == "merge":
                primary_uid = str(body.get("primary_uid") or "").strip()
                secondary_uid = str(body.get("secondary_uid") or "").strip()
                merged = await self.plugin.user_relation.merge_users(primary_uid, secondary_uid)
                users = await self.plugin.user_relation.list_users()
                return ok_response(
                    {
                        "merged_user": merged.to_dict(),
                        "users": [u.to_dict() for u in users],
                    },
                    message=f"已将账号 {secondary_uid} 合并至 {primary_uid}",
                )

            if action == "delete":
                uid = str(body.get("global_uid") or "").strip()
                await self.plugin.user_relation.delete_user(uid)
                users = await self.plugin.user_relation.list_users()
                return ok_response(
                    {"users": [u.to_dict() for u in users]},
                    message=f"已删除用户 {uid}",
                )

            uid = str(body.get("global_uid") or "").strip()
            if not uid:
                return err_response("用户账号 ID (global_uid) 不能为空")

            tier_val = body.get("tier")
            aff_val = body.get("affinity")
            pids_val = body.get("platform_ids")
            # v2.7: ★L3 = 管理员级。它**不会**通过好感度/自动晋升产生（自动晋升只到 L2），
            # 但允许管理员从控制台关系页「⋯ → 设为 L3」等显式入口手动指定 / 取消。
            requested_tier = (
                int(tier_val) if tier_val in (1, 2, 3, "1", "2", "3") else None
            )
            if isinstance(pids_val, str):
                pids_list = [x.strip() for x in pids_val.split(",") if x.strip()]
            elif isinstance(pids_val, list):
                pids_list = [str(x).strip() for x in pids_val if str(x).strip()]
            else:
                pids_list = None

            # v2.6 §2.2: per-user 专属态度 (max 300 chars) lives on UserProfile/DB.
            update_kwargs: dict[str, Any] = {
                "global_uid": uid,
                "display_name": body.get("display_name"),
                "nickname": body.get("nickname"),
                "tier": requested_tier,
                "affinity": float(aff_val) if aff_val is not None and str(aff_val) != "" else None,
                "platform_ids": pids_list,
            }
            if "attitude" in body:
                attitude_raw = body.get("attitude")
                update_kwargs["attitude"] = (
                    None if attitude_raw is None else str(attitude_raw).strip()[:300]
                )

            updated = await self.plugin.user_relation.update_user_details(**update_kwargs)
            users = await self.plugin.user_relation.list_users()
            return ok_response(
                {
                    "user": updated.to_dict(),
                    "users": [u.to_dict() for u in users],
                },
                message=f"已更新用户 {updated.display_name} ({updated.tier.label})",
            )
        except Exception as e:
            return err_response(f"更新用户关系失败: {e}")

    # ==========================================================================
    # 4. Tiered Memory Vault (`memories/*`)
    # ==========================================================================
    async def api_memories_list(self) -> dict[str, Any]:
        try:
            await self.plugin._ensure_initialized()
            soul_id = self._get_query_param("soul_id") or None
            tier_str = self._get_query_param("memory_tier")
            memory_tier = int(tier_str) if tier_str in ("1", "2", "3") else None
            user_id = self._get_query_param("user_id") or None
            keyword = self._get_query_param("keyword") or None

            items = await self.plugin.memory_manager.list_memories(
                soul_id=soul_id,
                memory_tier=memory_tier,
                user_id=user_id,
                keyword=keyword,
                limit=120,
            )
            all_for_soul = await self.plugin.memory_manager.list_memories(
                soul_id=soul_id, limit=500
            )
            stats = {
                "tier3_count": sum(1 for m in all_for_soul if m.memory_tier == MemoryTier.CORE),
                "tier2_count": sum(1 for m in all_for_soul if m.memory_tier == MemoryTier.RECENT),
                "tier1_count": sum(
                    1 for m in all_for_soul if m.memory_tier == MemoryTier.EPHEMERAL
                ),
                "total": len(all_for_soul),
                "sliding_window_size": self.plugin.session_buffer.sliding_window_size,
                "max_injected_memories": self.plugin.context_budget.max_injected_memories,
                "max_memory_chars_budget": self.plugin.context_budget.max_memory_chars_budget,
            }
            return ok_response(
                {
                    "memories": [m.to_dict() for m in items],
                    "stats": stats,
                    # v2.8 §5.6: T1 capacity shown by the Pages memory panel.
                    "tier1_limit": self._tier1_limit(),
                }
            )
        except Exception as e:
            return err_response(f"查询分级记忆失败: {e}")

    async def api_memories_mutate(self) -> dict[str, Any]:
        try:
            await self.plugin._ensure_initialized()
            body = await self._get_json_body()
            action = str(body.get("action") or "add").strip().lower()
            soul_id = str(body.get("soul_id") or "default_soul").strip()

            if action == "gc":
                gc_stats = await self.plugin.memory_manager.run_forgetting_gc()
                mems = await self.plugin.memory_manager.list_memories(soul_id=soul_id)
                return ok_response(
                    {
                        "gc_stats": gc_stats,
                        "memories": [m.to_dict() for m in mems],
                    },
                    message=(
                        f"遗忘清理完成：清理T1({gc_stats['deleted_tier1']})、"
                        f"物理删除T2({gc_stats['deleted_tier2']})、"
                        f"晋升T3({gc_stats['promoted_to_tier3']})"
                    ),
                )

            if action == "pin_core":
                mid = int(body.get("id") or 0)
                pinned = await self.plugin.memory_manager.pin_to_core(mid)
                if not pinned:
                    return err_response("未找到指定的记忆条目")
                mems = await self.plugin.memory_manager.list_memories(soul_id=pinned.soul_id)
                return ok_response(
                    {
                        "memory": pinned.to_dict(),
                        "memories": [m.to_dict() for m in mems],
                    },
                    message=f"已将记忆 #{mid} 锁定为永久核心记忆 (Tier 3)",
                )

            if action == "delete":
                mid = int(body.get("id") or 0)
                await self.plugin.memory_manager.delete_memory(mid)
                mems = await self.plugin.memory_manager.list_memories(soul_id=soul_id)
                return ok_response(
                    {"memories": [m.to_dict() for m in mems]},
                    message=f"已物理删除记忆 #{mid}",
                )

            if action == "edit":
                mid = int(body.get("id") or 0)
                updated = await self.plugin.memory_manager.update_memory(
                    memory_id=mid,
                    content=body.get("content"),
                    keywords=body.get("keywords"),
                    importance=body.get("importance"),
                    memory_tier=body.get("memory_tier"),
                    vitality=body.get("vitality"),
                    user_id=body.get("user_id"),
                )
                if not updated:
                    return err_response("未找到要修改的记忆条目")
                mems = await self.plugin.memory_manager.list_memories(soul_id=updated.soul_id)
                return ok_response(
                    {
                        "memory": updated.to_dict(),
                        "memories": [m.to_dict() for m in mems],
                    },
                    message=f"已保存记忆 #{mid}",
                )

            if action == "extract":
                umo = str(body.get("umo") or "").strip()
                if not umo and self.plugin.session_buffer._windows:
                    umo = next(iter(self.plugin.session_buffer._windows.keys()))
                soul = await self.plugin.soul_manager.get_soul_by_id(soul_id)
                if soul is None:
                    soul = (await self.plugin.soul_manager.list_souls())[0]
                transcript = (
                    self.plugin.session_buffer.get_window_transcript(umo=umo, max_turns=15)
                    if umo
                    else ""
                )
                count = await self.plugin.memory_manager.extract_memories_from_transcript(
                    soul_id=soul.soul_id,
                    soul_name=soul.name,
                    transcript_text=transcript,
                    umo=umo or None,
                )
                mems = await self.plugin.memory_manager.list_memories(soul_id=soul.soul_id)
                return ok_response(
                    {
                        "extracted_count": count,
                        "memories": [m.to_dict() for m in mems],
                    },
                    message=f"已从会话滑窗提炼 {count} 条记忆",
                )

            # Default: `action == "add"`
            content = str(body.get("content") or "").strip()
            if not content:
                return err_response("记忆内容不能为空")
            keywords = str(body.get("keywords") or "").strip()
            importance = int(body.get("importance") or 8)
            mtier_raw = body.get("memory_tier")
            mtier = int(mtier_raw) if mtier_raw in (1, 2, 3, "1", "2", "3") else 3
            user_id = str(body.get("user_id") or "").strip() or None
            user_tier = int(body.get("user_tier") or (3 if mtier == 3 else 2))

            mid = await self.plugin.memory_manager.add_memory(
                soul_id=soul_id,
                content=content,
                keywords=keywords,
                importance=importance,
                user_id=user_id,
                user_tier=user_tier,
                memory_tier=mtier,
            )
            mems = await self.plugin.memory_manager.list_memories(soul_id=soul_id)
            return ok_response(
                {
                    "new_id": mid,
                    "memories": [m.to_dict() for m in mems],
                },
                message=f"已新增记忆 #{mid}",
            )
        except Exception as e:
            return err_response(f"操作记忆库失败: {e}")

    # ==========================================================================
    # 5. Cyber Surfing Pool (`surfing/list_and_trigger`)
    # ==========================================================================
    async def api_surfing_list_and_trigger(self) -> dict[str, Any]:
        try:
            await self.plugin._ensure_initialized()
            method = self._get_method()

            if method == "GET":
                soul = await self._resolve_soul(self._get_query_param("soul_id"))
                soul_id = self._get_query_param("soul_id") or None
                items = await self.plugin.surfing_pipeline.list_history(soul_id=soul_id, limit=30)
                bindings = await self.plugin.soul_manager.get_umo_bindings()
                return ok_response(
                    {
                        "videos": items,
                        "known_umos": self._collect_known_umos(bindings),
                        # v2.8 §5.2: per-soul cyber-surfing master switch.
                        "surfing_enabled": self._soul_surfing_enabled(soul),
                        "soul_id": str(getattr(soul, "soul_id", "") or ""),
                    }
                )

            body = await self._get_json_body()
            action = str(body.get("action") or "surf").strip().lower()
            soul_id = str(body.get("soul_id") or "default_soul").strip()
            soul = await self.plugin.soul_manager.get_soul_by_id(soul_id)
            if soul is None:
                soul = (await self.plugin.soul_manager.list_souls())[0]

            if action == "toggle_surfing":
                enabled = _as_bool(body.get("enabled", True), default=True)
                setter = getattr(self.plugin.soul_manager, "set_soul_setting", None)
                if callable(setter):
                    try:
                        updated = setter(soul.soul_id, "surfing_enabled", enabled)
                        if inspect.isawaitable(updated):
                            updated = await updated
                        if updated is not None:
                            soul = updated
                    except Exception as exc:
                        return err_response(f"保存冲浪总开关失败: {exc}")
                else:
                    # Defensive fallback for a partially-landed SoulManager.
                    settings = getattr(soul, "settings", None)
                    settings = dict(settings) if isinstance(settings, dict) else {}
                    settings["surfing_enabled"] = enabled
                    try:
                        soul.settings = settings
                        await self.plugin.soul_manager.save_soul(soul)
                    except Exception as exc:  # pragma: no cover - defensive only
                        return err_response(f"保存冲浪总开关失败: {exc}")
                effective = self._soul_surfing_enabled(soul)
                to_dict = getattr(soul, "to_dict", None)
                return ok_response(
                    {
                        "soul_id": str(getattr(soul, "soul_id", "") or soul_id),
                        "surfing_enabled": effective,
                        "soul": to_dict() if callable(to_dict) else {},
                    },
                    message=("已开启" if effective else "已关闭") + "赛博冲浪总开关",
                )

            if action == "delete":
                vid_id = str(body.get("video_id") or "").strip() or None
                row_id = body.get("id")
                await self.plugin.surfing_pipeline.delete_history_item(
                    item_id=int(row_id) if row_id is not None else None,
                    video_id=vid_id,
                )
                items = await self.plugin.surfing_pipeline.list_history(soul_id=soul.soul_id)
                return ok_response({"videos": items}, message="已从冲浪池移除该视频")

            if action == "share":
                video_id = str(body.get("video_id") or "").strip()
                target_umo = str(body.get("target_umo") or "").strip()
                if not target_umo and self.plugin._known_l3_umos:
                    target_umo = next(iter(self.plugin._known_l3_umos.values()))
                if not target_umo and self.plugin.session_buffer._windows:
                    target_umo = next(iter(self.plugin.session_buffer._windows.keys()))

                items = await self.plugin.surfing_pipeline.list_history(soul_id=soul.soul_id)
                target_vid = next(
                    (v for v in items if v["video_id"] == video_id or str(v["id"]) == video_id),
                    items[0] if items else None,
                )
                if not target_vid:
                    return err_response("未找到可分享的视频")

                share_text = (
                    f"{target_vid['commentary']}\n"
                    f"📺 《{target_vid['title']}》(UP: {target_vid['author']})\n"
                    f"🔗 {target_vid['share_url']}"
                )
                sent = False
                if target_umo:
                    sent = await self.plugin.platform_bridge.send_proactive_message(
                        umo=target_umo,
                        text=share_text,
                        session_buffer=self.plugin.session_buffer,
                        soul_name=soul.name,
                    )
                else:
                    # Record into default session buffer window even when no live platform UMO is active yet
                    self.plugin.session_buffer.record_assistant_message(
                        umo="default_umo",
                        content=share_text,
                        is_proactive=True,
                        soul_name=soul.name,
                    )
                    sent = True

                return ok_response(
                    {
                        "sent": sent,
                        "target_umo": target_umo or "default_umo",
                        "share_text": share_text,
                    },
                    message=f"已将《{target_vid['title']}》分享至会话 {target_umo or '当前默认窗口'}",
                )

            # Default action: `surf` (trigger Bilibili or Douyin surfing)
            platform = str(body.get("platform") or "").strip().lower() or None
            ignore_gate = bool(body.get("ignore_state_gate", True))
            cur_slot = await self.plugin.deck_engine.get_current_slot(
                soul_id=soul.soul_id,
                custom_cards=soul.state_event_cards,
                chronotype=self._soul_chronotype(soul),
            )
            review = await self.plugin.surfing_pipeline.surf_once(
                soul=soul,
                current_state=cur_slot.state,
                platform_filter=platform,
                ignore_state_gate=ignore_gate,
            )
            items = await self.plugin.surfing_pipeline.list_history(soul_id=soul.soul_id)
            sched_payload = await self._build_schedule_payload(soul)
            if review is None:
                return err_response("本次冲浪未能拉取到新视频，请检查配置或状态门禁")
            return ok_response(
                {
                    "review": review.to_dict(),
                    "videos": items,
                    "schedule": sched_payload,
                },
                message=f"冲浪完成：《{review.video.title}》(评分 {review.score}/100)",
            )
        except Exception as e:
            logger.warning(f"[OnlyYou] api_surfing_list_and_trigger error: {e}")
            return err_response(f"冲浪操作失败: {e}")

    # ==========================================================================
    # 6. Dashboard Overview (`dashboard/overview`)
    # ==========================================================================
    async def api_dashboard_overview(self) -> dict[str, Any]:
        """One-shot payload for the new Overview page (state / schedule / quota / counters)."""
        try:
            await self.plugin._ensure_initialized()
            soul = await self._resolve_soul(self._get_query_param("soul_id"))

            run, cur_slot, next_slot, now_dt, chronotype = await self._load_current_schedule(soul)
            schedule_payload = self._schedule_payload_from(
                run, cur_slot, next_slot, now_dt, chronotype
            )
            mutator = getattr(self.plugin, "schedule_mutator", None)
            max_mutations = int(getattr(mutator, "max_mutations_per_day", 2) or 2)
            cooldown_seconds = int(getattr(mutator, "mutation_cooldown_seconds", 0) or 0)

            remaining_seconds, progress = _slot_remaining_and_progress(cur_slot, now_dt)
            current_action = _slot_action_type(cur_slot) or ActionType.IDLE_ROAM
            raw_state = getattr(cur_slot, "state", None)
            state_value = (
                raw_state.value
                if isinstance(raw_state, BotState)
                else str(raw_state or current_action.bound_state.value)
            )
            enriched_current = schedule_payload["current_slot"]
            progress_pct = int(enriched_current.get("progress_pct", round(progress * 100)))
            state_payload = {
                "state": state_value,
                "action_type": current_action.value,
                "action_type_label": current_action.label,
                "label": str(getattr(cur_slot, "label", "") or current_action.label),
                "title": str(getattr(cur_slot, "title", "") or ""),
                "armor": int(getattr(cur_slot, "armor", 50) or 0),
                "slot_key": str(getattr(cur_slot, "slot_key", "") or ""),
                "start_hour": _coerce_hour(getattr(cur_slot, "start_hour", 0.0), 0.0),
                "end_hour": _coerce_hour(getattr(cur_slot, "end_hour", 24.0), 24.0),
                "remaining_seconds": remaining_seconds,
                "progress": progress,
                "progress_pct": max(0, min(100, progress_pct)),
                "completion_result": str(getattr(cur_slot, "completion_result", "") or ""),
                "busy_settled": bool(getattr(cur_slot, "busy_settled", False)),
                "mutated": bool(getattr(cur_slot, "mutated", False)),
                "next_slot": schedule_payload["next_slot"],
            }

            used = int(getattr(run, "mutations_used", 0) or 0)
            last_mutation_ts = float(getattr(run, "last_mutation_ts", 0.0) or 0.0)
            if last_mutation_ts > 0:
                cooldown_remaining_seconds = int(
                    max(0.0, last_mutation_ts + cooldown_seconds - time.time())
                )
            else:
                cooldown_remaining_seconds = 0
            quota_payload = {
                "used": used,
                "max": max_mutations,
                "remaining": max(0, max_mutations - used),
                "cooldown_remaining_seconds": cooldown_remaining_seconds,
            }

            card = self._soul_card(soul)
            identity = card.get("identity") if isinstance(card.get("identity"), dict) else {}
            soul_payload = {
                "soul_id": soul.soul_id,
                "name": str(identity.get("name") or getattr(soul, "name", "") or soul.soul_id),
                "avatar": str(identity.get("avatar") or ""),
                "tagline": str(identity.get("tagline") or getattr(soul, "archetype", "") or ""),
            }

            return ok_response(
                {
                    "soul": soul_payload,
                    "vigor": {
                        "title": str(getattr(run, "vigor_title", "") or ""),
                        "desc": str(getattr(run, "vigor_desc", "") or ""),
                        "chronotype": chronotype,
                    },
                    "state": state_payload,
                    "schedule": schedule_payload,
                    "quota": quota_payload,
                    # Encounter-talk cards were removed from the product; the empty
                    # list is kept so old frontend bundles can still deserialize.
                    "encounters": [],
                    "busy_results": schedule_payload["busy_results"],
                    "counts": {
                        "relations": await self._relation_counts(),
                        "memories": await self._memory_counts(soul.soul_id),
                        "videos": await self._video_count(soul.soul_id),
                    },
                    "models": await self._safe_model_status(),
                }
            )
        except Exception as e:
            logger.warning(f"[OnlyYou] api_dashboard_overview error: {e}")
            return err_response(f"加载概览数据失败: {e}")

    # ==========================================================================
    # 7. Character Card Studio (`card/*`)
    # ==========================================================================
    async def api_card_schema(self) -> dict[str, Any]:
        """Return the character-card form schema plus the current soul card data."""
        try:
            await self.plugin._ensure_initialized()
            soul = await self._resolve_soul(self._get_query_param("soul_id"))
            module = _character_card_module(log_missing=True)
            schema = getattr(module, "CARD_SCHEMA", None) if module is not None else None
            if not isinstance(schema, list):
                return err_response("角色卡 schema 不可用：core/character_card.py 尚未就绪")
            return ok_response(
                {
                    "schema": schema,
                    "card": self._soul_card(soul),
                    "soul_id": soul.soul_id,
                }
            )
        except Exception as e:
            logger.warning(f"[OnlyYou] api_card_schema error: {e}")
            return err_response(f"加载角色卡 schema 失败: {e}")

    async def api_card_save(self) -> dict[str, Any]:
        """Persist a complete character card (`normalize_card` applied) for one soul."""
        try:
            await self.plugin._ensure_initialized()
            body = await self._get_json_body()
            soul_id = str(body.get("soul_id") or "").strip()
            card_raw = body.get("card")
            if not soul_id:
                return err_response("缺少 soul_id")
            if not isinstance(card_raw, dict):
                return err_response("缺少 card 对象")
            module = _character_card_module(log_missing=True)
            if module is None:
                return err_response("角色卡模块不可用：core/character_card.py 尚未就绪")
            manager = getattr(self.plugin, "soul_manager", None)
            if manager is None:
                return err_response("SoulManager 未初始化")
            soul = await manager.get_soul_by_id(soul_id)
            if soul is None:
                return err_response(f"角色不存在: {soul_id}")

            profile = self._apply_card_to_profile(soul, self._normalize_card(card_raw))
            await manager.save_soul(profile)

            souls = await manager.list_souls()
            to_dict = getattr(profile, "to_dict", None)
            return ok_response(
                {
                    "soul": to_dict() if callable(to_dict) else {},
                    "souls": [s.to_dict() for s in souls],
                },
                message=f"已保存角色卡 {soul_id}",
            )
        except Exception as e:
            logger.warning(f"[OnlyYou] api_card_save error: {e}")
            return err_response(f"保存角色卡失败: {e}")

    async def api_card_ai_generate(self) -> dict[str, Any]:
        """Generate a whole-card or single-section character-card draft (not persisted)."""
        try:
            await self.plugin._ensure_initialized()
            body = await self._get_json_body()
            soul = await self._resolve_soul(str(body.get("soul_id") or "").strip())
            seed_text = str(body.get("seed_text") or "").strip()
            section = str(body.get("section") or "").strip() or None

            module = _character_card_module()
            if section:
                section_ids = getattr(module, "CARD_SECTION_IDS", None) if module else None
                if isinstance(section_ids, list) and section not in section_ids:
                    return err_response(f"未知的角色卡分区: {section}")

            manager = getattr(self.plugin, "soul_manager", None)
            generator = getattr(manager, "generate_card", None) if manager is not None else None
            if not callable(generator):
                return err_response("SoulManager 未实现 generate_card")

            base_card = self._soul_card(soul)
            draft = await generator(seed_text=seed_text, base_card=base_card, section=section)
            if not isinstance(draft, dict):
                return err_response("AI 生成角色卡失败，请检查主模型配置与运行日志")
            draft = self._normalize_card(draft)
            message = f"已为 {soul.soul_id} 生成角色卡草稿"
            if section:
                message += f"（分区 {section}）"
            return ok_response({"draft_card": draft, "section": section}, message=message)
        except Exception as e:
            logger.warning(f"[OnlyYou] api_card_ai_generate error: {e}")
            return err_response(f"AI 生成角色卡失败: {e}")

    async def api_card_preview_prompt(self) -> dict[str, Any]:
        """Render the persona prompt of a card (either the body draft or the saved card)."""
        try:
            await self.plugin._ensure_initialized()
            body = await self._get_json_body()
            module = _character_card_module(log_missing=True)
            render = getattr(module, "render_persona_prompt", None) if module is not None else None
            if not callable(render):
                return err_response("角色卡渲染不可用：core/character_card.py 尚未就绪")

            body_card = body.get("card")
            if isinstance(body_card, dict):
                card = self._normalize_card(body_card)
            else:
                soul = await self._resolve_soul(str(body.get("soul_id") or "").strip())
                card = self._soul_card(soul)

            try:
                max_chars = int(body.get("max_chars") or 2000)
            except (TypeError, ValueError):
                max_chars = 2000
            max_chars = max(200, min(20000, max_chars))
            try:
                prompt = render(card, max_chars=max_chars)
            except TypeError:
                prompt = render(card)
            prompt_text = str(prompt or "")
            return ok_response({"prompt": prompt_text, "chars": len(prompt_text)})
        except Exception as e:
            logger.warning(f"[OnlyYou] api_card_preview_prompt error: {e}")
            return err_response(f"预览角色卡 Prompt 失败: {e}")

    # ==========================================================================
    # 8. Plugin-autonomous Four-Role Models (`models/*`)
    # ==========================================================================
    async def api_models_sources(self) -> dict[str, Any]:
        """List AstrBot provider sources + TTS providers (never exposes API keys)."""
        try:
            await self.plugin._ensure_initialized()
            factory = self._provider_factory()
            sources = factory.list_sources()
            tts_providers = factory.list_tts_providers()
            if inspect.isawaitable(sources):
                sources = await sources
            if inspect.isawaitable(tts_providers):
                tts_providers = await tts_providers
            return ok_response(
                {
                    "sources": list(sources or []),
                    "tts_providers": list(tts_providers or []),
                }
            )
        except Exception as e:
            logger.warning(f"[OnlyYou] api_models_sources error: {e}")
            return err_response(f"获取提供商源列表失败: {e}")

    async def api_models_list(self) -> dict[str, Any]:
        """List models available under one provider source (configured + remote)."""
        try:
            await self.plugin._ensure_initialized()
            source_id = self._get_query_param("source_id")
            if not source_id:
                return err_response("缺少提供商源 ID (source_id)")
            factory = self._provider_factory()
            models = await factory.list_models(source_id)
            return ok_response({"source_id": source_id, "models": list(models or [])})
        except Exception as e:
            logger.warning(f"[OnlyYou] api_models_list error: {e}")
            return err_response(f"获取模型列表失败: {e}")

    async def api_models_config(self) -> dict[str, Any]:
        """GET: read the four-role model config; POST: save it and hot-rebuild instances.

        v2.8 (contract §6.2): soul-aware — ``GET ?soul_id=`` / ``POST {soul_id, model_config}``.
        Without ``soul_id`` ModelHub resolves the default soul (or keeps the legacy
        global config when ``attach_soul_manager`` was never called).
        """
        try:
            await self.plugin._ensure_initialized()
            hub = getattr(self.plugin, "model_hub", None)
            if hub is None:
                return err_response("ModelHub 未初始化")
            method = self._get_method()
            requested_soul_id = ""
            saved: dict[str, Any] = {}

            if method == "POST":
                body = await self._get_json_body()
                requested_soul_id = str(body.get("soul_id") or "").strip()
                raw_cfg = body.get("model_config")
                if not isinstance(raw_cfg, dict):
                    # Tolerate raw config bodies: {"main": {...}, "decision": {...}, ...}
                    if any(key in body for key in ("main", "decision", "vision", "tts")):
                        raw_cfg = {
                            key: body.get(key)
                            for key in ("main", "decision", "vision", "tts")
                            if key in body
                        }
                if not isinstance(raw_cfg, dict):
                    return err_response("请求体缺少 model_config 对象")
                try:
                    saved = await self._save_hub_model_config(
                        hub, raw_cfg, requested_soul_id or None
                    )
                except RuntimeError as exc:
                    return err_response(str(exc))
            else:
                requested_soul_id = self._get_query_param("soul_id")

            model_config = (
                saved
                if saved
                else await self._read_model_config(hub, requested_soul_id or None)
            )
            status = await self._safe_model_status(requested_soul_id or None)
            soul_id = requested_soul_id or await self._safe_default_soul_id()
            message = "模型配置已保存并热重建实例" if method == "POST" else "ok"
            return ok_response(
                {"soul_id": soul_id, "model_config": model_config, "status": status},
                message=message,
            )
        except Exception as e:
            logger.warning(f"[OnlyYou] api_models_config error: {e}")
            return err_response(f"读取/保存模型配置失败: {e}")

    async def api_models_test(self) -> dict[str, Any]:
        """Connectivity test for one model role; TTS additionally returns base64 audio."""
        try:
            await self.plugin._ensure_initialized()
            body = await self._get_json_body()
            role = str(body.get("role") or "main").strip().lower()
            soul_id = str(body.get("soul_id") or "").strip() or None
            hub = getattr(self.plugin, "model_hub", None)
            if hub is None:
                return err_response("ModelHub 未初始化")

            roles = getattr(hub, "ROLES", ("main", "decision", "vision", "tts"))
            allowed = {str(item).strip().lower() for item in roles}
            allowed.add("small")  # legacy alias of `decision`
            if role not in allowed:
                return err_response(f"未知模型角色: {role}（可选 main/decision/vision/tts）")

            tester = getattr(hub, "test_role", None)
            if not callable(tester):
                return err_response("ModelHub 未实现 test_role")
            try:
                try:
                    value = tester(role, soul_id=soul_id) if soul_id else tester(role)
                except TypeError:
                    # Legacy / partial signature without the `soul_id` keyword.
                    value = tester(role)
                if inspect.isawaitable(value):
                    value = await value
            except Exception as exc:
                return err_response(f"模型测试失败: {exc}")
            result = value
            if result is None:
                return err_response("模型测试未返回结果")
            if isinstance(result, dict):
                return ok_response(result, message="模型测试完成")
            return ok_response({"result": result}, message="模型测试完成")
        except Exception as e:
            logger.warning(f"[OnlyYou] api_models_test error: {e}")
            return err_response(f"模型测试失败: {e}")

    # ==========================================================================
    # 9. Session Routing (`sessions/*`)
    # ==========================================================================
    async def api_sessions_default_soul(self) -> dict[str, Any]:
        """Set the plugin default soul used when a session has no explicit UMO binding."""
        try:
            await self.plugin._ensure_initialized()
            body = await self._get_json_body()
            soul_id = str(body.get("soul_id") or "").strip()
            if not soul_id:
                return err_response("缺少 soul_id")
            manager = getattr(self.plugin, "soul_manager", None)
            if manager is None:
                return err_response("SoulManager 未初始化")
            soul = await manager.get_soul_by_id(soul_id)
            if soul is None:
                return err_response(f"角色不存在: {soul_id}")

            setter = getattr(manager, "set_default_soul_id", None)
            if not callable(setter):
                return err_response("SoulManager 未实现 set_default_soul_id")
            await setter(soul_id)

            default_soul_id = await self._safe_default_soul_id() or soul_id
            souls = await manager.list_souls()
            bindings = await manager.get_umo_bindings()
            return ok_response(
                {
                    "default_soul_id": default_soul_id,
                    "souls": [s.to_dict() for s in souls],
                    "umo_bindings": bindings,
                },
                message=f"已将默认角色设置为 {soul_id}",
            )
        except Exception as e:
            logger.warning(f"[OnlyYou] api_sessions_default_soul error: {e}")
            return err_response(f"设置默认角色失败: {e}")

    # ==========================================================================
    # 10. Bot-instance (platform) binding & session memory (v2.8, contract §6.2)
    # ==========================================================================
    async def api_sessions_platforms(self) -> dict[str, Any]:
        """GET ``sessions/platforms?soul_id=`` — bot instances, bindings & sessions."""
        try:
            await self.plugin._ensure_initialized()
            soul = await self._resolve_soul(self._get_query_param("soul_id"))
            return ok_response(await self._sessions_payload(soul))
        except Exception as e:
            logger.warning(f"[OnlyYou] api_sessions_platforms error: {e}")
            return err_response(f"加载机器人接口与会话内存失败: {e}")

    async def api_sessions_bind_platform(self) -> dict[str, Any]:
        """POST ``sessions/bind_platform`` ``{soul_id, platform_id}`` (exclusive binding)."""
        try:
            await self.plugin._ensure_initialized()
            body = await self._get_json_body()
            soul_id = str(body.get("soul_id") or "").strip()
            platform_id = str(body.get("platform_id") or "").strip()
            if not soul_id:
                return err_response("缺少 soul_id")
            if not platform_id:
                return err_response("缺少 platform_id")
            manager = getattr(self.plugin, "soul_manager", None)
            if manager is None:
                return err_response("SoulManager 未初始化")
            soul = await manager.get_soul_by_id(soul_id)
            if soul is None:
                return err_response(f"角色不存在: {soul_id}")
            binder = getattr(manager, "bind_platform", None)
            if not callable(binder):
                return err_response("SoulManager 未实现 bind_platform（机器人接口绑定不可用）")
            try:
                bound = binder(soul_id, platform_id)
                if inspect.isawaitable(bound):
                    bound = await bound
                if bound is not None:
                    soul = bound
            except ValueError as exc:
                # Exact mutual-exclusion message required by contract §6.2.
                return err_response(str(exc))
            payload = {
                "soul_id": soul_id,
                "platform_id": platform_id,
                "souls": await self._soul_summaries(),
                "platforms": await self._platforms_payload(),
            }
            soul_name = str(getattr(soul, "name", "") or soul_id)
            return ok_response(
                payload, message=f"已将机器人接口 {platform_id} 绑定到角色 {soul_name}"
            )
        except Exception as e:
            logger.warning(f"[OnlyYou] api_sessions_bind_platform error: {e}")
            return err_response(f"绑定机器人接口失败: {e}")

    async def api_sessions_unbind_platform(self) -> dict[str, Any]:
        """POST ``sessions/unbind_platform`` ``{platform_id}`` + memory cleanup."""
        try:
            await self.plugin._ensure_initialized()
            body = await self._get_json_body()
            platform_id = str(body.get("platform_id") or "").strip()
            if not platform_id:
                return err_response("缺少 platform_id")
            manager = getattr(self.plugin, "soul_manager", None)
            if manager is None:
                return err_response("SoulManager 未初始化")
            unbinder = getattr(manager, "unbind_platform", None)
            if not callable(unbinder):
                return err_response("SoulManager 未实现 unbind_platform（机器人接口解绑不可用）")
            try:
                previous_soul_id = unbinder(platform_id)
                if inspect.isawaitable(previous_soul_id):
                    previous_soul_id = await previous_soul_id
                previous_soul_id = str(previous_soul_id or "").strip()
            except ValueError as exc:
                return err_response(str(exc))
            # Always drop the platform's in-memory chat state (contract §4.1/§6.2).
            cleared = await self._clear_platform_memory(platform_id)
            soul = await self._resolve_soul(str(body.get("soul_id") or ""))
            payload = {
                "platform_id": platform_id,
                "previous_soul_id": previous_soul_id,
                "cleared": cleared,
                "souls": await self._soul_summaries(),
                "platforms": await self._platforms_payload(),
            }
            return ok_response(
                payload,
                message=(
                    f"已解绑机器人接口 {platform_id}"
                    f"（清理 {cleared['messages']} 条内存消息；仅清理内存滑窗/未读，"
                    "数据库 memories / users / surfing_history 与群档案保留）"
                ),
            )
        except Exception as e:
            logger.warning(f"[OnlyYou] api_sessions_unbind_platform error: {e}")
            return err_response(f"解绑机器人接口失败: {e}")

    async def api_sessions_clear_memory(self) -> dict[str, Any]:
        """POST ``sessions/clear_memory`` ``{umo?}`` / ``{platform_id?}``."""
        try:
            await self.plugin._ensure_initialized()
            body = await self._get_json_body()
            umo = str(body.get("umo") or "").strip()
            platform_id = str(body.get("platform_id") or "").strip()
            if not umo and not platform_id:
                return err_response("请提供 umo 或 platform_id")
            if umo:
                cleared = await self._clear_umo_memory(umo)
                target = f"会话 {umo}"
            else:
                cleared = await self._clear_platform_memory(platform_id)
                target = f"平台 {platform_id}"
            soul = await self._resolve_soul(str(body.get("soul_id") or ""))
            return ok_response(
                {"cleared": cleared, "sessions": await self._session_entries(soul)},
                message=(
                    f"已清空{target}的内存滑窗与未读"
                    f"（{cleared['messages']} 条消息；仅清理内存态，"
                    "数据库 memories / users / surfing_history 与群档案保留）"
                ),
            )
        except Exception as e:
            logger.warning(f"[OnlyYou] api_sessions_clear_memory error: {e}")
            return err_response(f"清空会话内存失败: {e}")

