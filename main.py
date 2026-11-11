"""Main entry point for astrbot_plugin_only_you (OnlyYou / 只有你)."""

import asyncio
import os
import random
import time
from datetime import datetime
from typing import Any, Optional

try:
    from astrbot.api import AstrBotConfig, logger
    from astrbot.api.event import AstrMessageEvent, MessageChain, filter
    from astrbot.api.provider import LLMResponse, ProviderRequest
    from astrbot.api.star import Context, Star, StarTools, register
except ImportError:
    # Fallback stubs for local unit testing outside AstrBot container
    import logging

    logger = logging.getLogger("onlyyou.main")
    AstrBotConfig = dict
    AstrMessageEvent = Any
    LLMResponse = Any
    ProviderRequest = Any
    Context = Any

    class Star:
        def __init__(self, context: Any = None, config: Any = None):
            self.context = context
            self.config = config or {}

    class StarTools:
        @staticmethod
        def get_data_dir(plugin_name: str = "astrbot_plugin_only_you") -> str:
            return os.path.join(os.getcwd(), "data", plugin_name)

    def register(*args, **kwargs):
        def _decorator(cls):
            return cls
        return _decorator

    class _CommandGroupStub:
        """Minimal stand-in for AstrBot's ``RegisteringCommandable``.

        Only used when AstrBot is not importable (offline unit tests); real
        sub-command handlers are still plain functions so tests can call them.
        """

        def __init__(self, fn):
            self._fn = fn

        def command(self, *args, **kwargs):
            def _dec(fn):
                return fn
            return _dec

    class _FilterStub:
        class EventMessageType:
            ALL = "ALL"

        @staticmethod
        def event_message_type(*args, **kwargs):
            def _dec(fn):
                return fn
            return _dec

        class PermissionType:
            ADMIN = "admin"
            MEMBER = "member"
            GROUP_ADMIN = "group_admin"
            SHARED_GROUP_ADMIN = "shared_group_admin"

        @staticmethod
        def permission_type(*args, **kwargs):
            def _dec(fn):
                return fn
            return _dec

        @staticmethod
        def command_group(*args, **kwargs):
            def _dec(fn):
                return _CommandGroupStub(fn)
            return _dec

        @staticmethod
        def on_llm_request(*args, **kwargs):
            def _dec(fn):
                return fn
            return _dec

        @staticmethod
        def on_llm_response(*args, **kwargs):
            def _dec(fn):
                return fn
            return _dec

        @staticmethod
        def on_decorating_result(*args, **kwargs):
            def _dec(fn):
                return fn
            return _dec

        @staticmethod
        def command(*args, **kwargs):
            def _dec(fn):
                return fn
            return _dec

    filter = _FilterStub()

try:
    from astrbot.api.message_components import Plain, Record
except ImportError:
    # Minimal message component stubs so the plugin stays importable offline.
    class Plain:  # type: ignore[no-redef]
        def __init__(self, text: str = "", **kwargs: Any):
            self.text = text

        def __repr__(self) -> str:
            return f"Plain({self.text!r})"

    class Record:  # type: ignore[no-redef]
        def __init__(
            self,
            file: Optional[str] = None,
            url: Optional[str] = None,
            text: Optional[str] = None,
            **kwargs: Any,
        ):
            self.file = file
            self.url = url
            self.text = text

        def __repr__(self) -> str:
            return f"Record({self.file!r})"

    class MessageChain:  # type: ignore[no-redef]
        """Minimal MessageChain stub used by off-line unit tests."""

        def __init__(self, chain: Optional[list] = None, **kwargs: Any):
            self.chain = list(chain or [])
            self.type = kwargs.get("type")

        def message(self, text: str):
            self.chain.append(Plain(text))
            return self

from .core.atmosphere_reader import AtmosphereReader
from .core.context_budget import ContextBudgetManager, SessionBuffer
from .core.memory_manager import MemoryManager, MemoryTier
from .core.model_hub import ModelHub
from .core.platform_bridge import PlatformBridge
from .core.provider_factory import ProviderFactory
from .core.roguelite_deck import RogueliteDeckEngine, SlotInstance
from .core.schedule_mutator import BoundedScheduleMutator
from .core.soul_manager import SoulManager
from .core.state_machine import ActionType, BotState, StateController, UserTier
from .core.user_relation import UserRelationManager
from .core.web_api import OnlyYouWebAPI
from .prompts.templates import (
    ATMOSPHERE_EVAL_PROMPT,
    BUSY_TASK_COMPLETION_PROMPT,
    IDLE_ROAM_PROACTIVE_PROMPT,
    PROACTIVE_CHAT_PROMPT,
    UNREAD_PRIVATE_CATCHUP_PROMPT,
)
from .storage.db import DatabaseManager
from .surfing.pipeline import CyberSurfingPipeline

try:
    from .core.character_card import render_card_summary
except ImportError:  # pragma: no cover - character_card is created in parallel
    def render_card_summary(card: Any, max_chars: int = 200) -> str:
        """Fallback summary renderer (kept importable until character_card lands)."""
        try:
            if not isinstance(card, dict):
                return ""
            identity = card.get("identity") or {}
            personality = card.get("personality") or {}
            speech = card.get("speech") or {}
            parts = []
            name = str(identity.get("name") or "").strip()
            tagline = str(identity.get("tagline") or "").strip()
            if name:
                parts.append(f"「{name}」")
            if tagline:
                parts.append(tagline)
            tags = personality.get("personality_tags") or []
            if isinstance(tags, list) and tags:
                parts.append("性格：" + "、".join(str(t) for t in tags[:4]))
            style = str(speech.get("speaking_style") or "").strip()
            if style:
                parts.append("说话方式：" + style)
            text = "；".join(part for part in parts if part)
            return text[:max_chars]
        except Exception:
            return ""



# ==========================================================================
# v2.8 module-level helpers (usable from ``OnlyYouPlugin`` unbound methods that
# offline tests bind onto lightweight SimpleNamespace fakes).
# ==========================================================================
async def _hub_call_json(
    model_hub: Any,
    prompt: str,
    role: str = "decision",
    umo: Optional[str] = None,
    default: Any = None,
    soul: Any = None,
) -> Any:
    """``model_hub.call_llm_json`` with an optional per-Soul config (TypeError safe)."""
    soul_id = str(getattr(soul, "soul_id", "") or "").strip()
    kwargs: dict[str, Any] = {"prompt": prompt, "role": role, "umo": umo, "default": default}
    if soul_id:
        try:
            return await model_hub.call_llm_json(soul_id=soul_id, **kwargs)
        except TypeError:
            pass
    return await model_hub.call_llm_json(**kwargs)


async def _hub_call_text(
    model_hub: Any,
    prompt: str,
    role: str = "decision",
    umo: Optional[str] = None,
    soul: Any = None,
) -> str:
    """``model_hub.call_llm_text`` with an optional per-Soul config (TypeError safe)."""
    soul_id = str(getattr(soul, "soul_id", "") or "").strip()
    kwargs: dict[str, Any] = {"prompt": prompt, "role": role, "umo": umo}
    if soul_id:
        try:
            return await model_hub.call_llm_text(soul_id=soul_id, **kwargs)
        except TypeError:
            pass
    return await model_hub.call_llm_text(**kwargs)


def _hub_tts_config(model_hub: Any, soul: Any = None) -> dict:
    """Read the TTS config of one Soul (falls back to the old no-arg signature)."""
    soul_id = str(getattr(soul, "soul_id", "") or "").strip()
    if soul_id:
        try:
            return dict(model_hub.get_tts_config(soul_id=soul_id) or {})
        except TypeError:
            pass
    try:
        return dict(model_hub.get_tts_config() or {})
    except Exception:
        return {}


async def _hub_synthesize(model_hub: Any, text: str, soul: Any = None) -> Optional[str]:
    """Synthesize proactive speech with the Soul's TTS config (TypeError safe)."""
    soul_id = str(getattr(soul, "soul_id", "") or "").strip()
    if soul_id:
        try:
            return await model_hub.synthesize(text, soul_id=soul_id)
        except TypeError:
            pass
    return await model_hub.synthesize(text)


async def _hub_main_provider_id(model_hub: Any, soul: Any = None) -> str:
    """Resolve the native-pipeline ``selected_provider`` id for one Soul."""
    soul_id = str(getattr(soul, "soul_id", "") or "").strip()
    if soul_id:
        try:
            return str(await model_hub.get_main_provider_id(soul_id=soul_id) or "")
        except TypeError:
            pass
    return str(await model_hub.get_main_provider_id() or "")


def _extract_group_meta(event: Any, umo: str) -> tuple[str, str]:
    """Best-effort ``(group_id, group_name)`` of a group event / UMO."""
    group_id = ""
    group_name = ""
    for attr in ("get_group_id",):
        getter = getattr(event, attr, None)
        if callable(getter):
            try:
                group_id = str(getter() or "").strip()
            except Exception:
                group_id = ""
            if group_id:
                break
    if not group_id:
        try:
            parts = str(umo or "").split(":")
            if len(parts) >= 3 and "group" in parts[1].lower():
                group_id = parts[2].split("!")[0].strip()
        except Exception:
            group_id = ""
    for attr in ("get_group_name",):
        getter = getattr(event, attr, None)
        if callable(getter):
            try:
                group_name = str(getter() or "").strip()
            except Exception:
                group_name = ""
            if group_name:
                break
    if not group_name:
        group_name = str(getattr(event, "group_name", "") or "").strip()
    return group_id, group_name


def _umo_in_platform_scope(
    umo: str, allowed_platforms: Any = None, blocked_platforms: Any = None
) -> bool:
    """Whether a UMO belongs to a Soul's bot-platform scope (None = no filter)."""
    platform_id = PlatformBridge.extract_platform_id(umo)
    if allowed_platforms is not None:
        try:
            if platform_id not in set(allowed_platforms):
                return False
        except Exception:
            return False
    if blocked_platforms is not None:
        try:
            if platform_id in set(blocked_platforms):
                return False
        except Exception:
            pass
    return True


@register(
    "astrbot_plugin_only_you",
    "hidge123",
    "OnlyYou (只有你) —— 具备生命节律、三级记忆与赛博冲浪能力的高自治 AI 伴侣与数字网友插件",
    "0.1.0",
    "https://github.com/hidge123/astrbot_plugin_only_you",
)
class OnlyYouPlugin(Star):
    """
    OnlyYou (只有你) 核心插件主类:
    - 三态模型 (RESTING / BUSY / IDLE) × 两大原子行为 (MESSAGE / SURF_VIDEO)
    - Roguelite 24h 动态弹性时间轴引擎（SLEEP / SURF / IDLE_ROAM / BUSY_TASK 四类行动卡）
    - 输入驱动的受限日程状态微调器 (三重硬性门禁)
    - 三级用户关系体系 (L1普通 / ◆L2重要熟人 / ★L3核心用户)
    - 精简版三级记忆 + 定时自动遗忘清理 (Forgetting GC) + 上下文预算管理器
    """

    #: All valid `/onlyyou` (`/oy`) sub-commands. Must stay in sync with the
    #: ``@onlyyou.command(...)`` registrations below: AstrBot's dashboard
    #: command list is rendered from those CommandFilters, and the listener
    #: below uses this set to catch typos before they fall through to the LLM.
    ONLYYOU_SUBCOMMANDS = frozenset(
        {"status", "state", "reroll", "tier", "memory", "surf", "unread", "help"}
    )

    #: Reply sent when a non-admin tries the bare group name or an unknown
    #: sub-command (both bypass AstrBot's PermissionTypeFilter).
    _ADMIN_ONLY_HINT = "⛔ /onlyyou（/oy）指令仅管理员可用。"

    def __init__(self, context: Context, config: Optional[AstrBotConfig] = None):
        super().__init__(context)
        self.context = context
        self.config: dict[str, Any] = config if config is not None else {}

        try:
            data_dir = str(StarTools.get_data_dir("astrbot_plugin_only_you"))
        except Exception:
            data_dir = "/AstrBot/data/plugin_data/astrbot_plugin_only_you"
        os.makedirs(data_dir, exist_ok=True)
        self.db_path = os.path.join(data_dir, "only_you.db")

        # Initialize core components
        self.db = DatabaseManager(self.db_path)
        self.provider_factory = ProviderFactory(self.context)
        self.model_hub = ModelHub(
            context=self.context,
            plugin_config=self.config,
            db=self.db,
            provider_factory=self.provider_factory,
        )
        self.platform_bridge = PlatformBridge(context=self.context)
        self.soul_manager = SoulManager(
            context=self.context, db=self.db, model_hub=self.model_hub
        )
        # v2.8: four-role model configs live in each Soul's own JSON file.
        self.model_hub.attach_soul_manager(self.soul_manager)
        self.deck_engine = RogueliteDeckEngine(db=self.db, plugin_config=self.config)
        self.schedule_mutator = BoundedScheduleMutator(
            deck_engine=self.deck_engine,
            model_hub=self.model_hub,
            plugin_config=self.config,
        )
        self.user_relation = UserRelationManager(
            db=self.db,
            platform_bridge=self.platform_bridge,
            plugin_config=self.config,
        )
        self.memory_manager = MemoryManager(
            db=self.db, model_hub=self.model_hub, plugin_config=self.config
        )
        self.session_buffer = SessionBuffer(plugin_config=self.config)
        self.context_budget = ContextBudgetManager(
            memory_manager=self.memory_manager,
            session_buffer=self.session_buffer,
            plugin_config=self.config,
            soul_manager=self.soul_manager,
            # v2.6: per-user 专属称呼/专属态度 need the relation profiles for the
            # system-prompt block (window peers included).
            user_relation=self.user_relation,
        )
        self.atmosphere_reader = AtmosphereReader(
            session_buffer=self.session_buffer,
            model_hub=self.model_hub,
            plugin_config=self.config,
        )
        self.surfing_pipeline = CyberSurfingPipeline(
            model_hub=self.model_hub,
            memory_manager=self.memory_manager,
            session_buffer=self.session_buffer,
            schedule_mutator=self.schedule_mutator,
            plugin_config=self.config,
        )
        self.web_api = OnlyYouWebAPI(plugin=self)
        self.web_api.register_routes()

        self._initialized = False
        self._init_lock = asyncio.Lock()
        self._bg_tasks: list[asyncio.Task] = []
        self._known_l3_umos: dict[str, str] = {}
        # umo -> tier of the last user message; used by the TTS scope check without
        # touching the DB (avoids double interaction counting).
        self._last_message_tier: dict[str, UserTier] = {}

        # 24h dynamic timeline runtime state (Phase 3):
        # - group UMOs observed from non-private chats (IDLE_ROAM GROUP_LURK);
        # - last IDLE_ROAM proactive action timestamp per soul (cooldown gate);
        # - BUSY_TASK completion settler injected into the schedule mutator.
        self._known_group_umos: set[str] = set()
        self._last_idle_roam_ts: dict[str, float] = {}
        # Unread backlog consumption gates:
        # - per-group GROUP_LURK cooldown (avoid re-reading the same group);
        # - per-private-chat catch-up cooldown (avoid double-messaging).
        self._last_group_lurk_ts: dict[str, float] = {}
        self._last_private_catchup_ts: dict[str, float] = {}
        # (soul_id, slot_key) currently being settled; guards the heartbeat / Web API
        # / mutator call sites against double-settling the same block across awaits.
        self._busy_settling: set[tuple[str, str]] = set()
        self.schedule_mutator.busy_settler = self.settle_busy_block

        # ---- v2.8 proactive-message / memory runtime state ----
        # SURF sharing gates: shared video ids + last share timestamp per Soul.
        self._shared_video_ids: dict[str, set[str]] = {}
        self._last_surf_share_ts: dict[str, float] = {}
        # MEMORY_MUSE rotation: busy results already mused (slot keys) and the
        # recently mused memory ids per Soul (ring buffer, keeps the buckets rotating).
        self._mused_busy_keys: dict[str, set[str]] = {}
        self._mused_memory_ids: dict[str, list[int]] = {}

    async def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        async with self._init_lock:
            if not self._initialized:
                await self.db.initialize()
                await self.model_hub.load()
                self._initialized = True

    async def initialize(self) -> None:
        """AstrBot Star lifecycle initialization hook."""
        await self._ensure_initialized()
        # v2.8: migrate legacy per-UMO bindings into bot-instance (platform) bindings
        # and catch up a missed daily Forgetting GC after a restart.
        try:
            await self.soul_manager.migrate_legacy_umo_bindings()
        except Exception as exc:  # noqa: BLE001 - migration must never block startup
            logger.warning(f"[OnlyYou] legacy umo binding migration failed: {exc}")
        try:
            await self._maybe_run_scheduled_gc()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[OnlyYou] startup Forgetting GC failed: {exc}")
        self.web_api.register_routes()
        self._bg_tasks.append(asyncio.create_task(self._heartbeat_loop()))
        self._bg_tasks.append(asyncio.create_task(self._daily_gc_loop()))
        logger.info("[OnlyYou] Plugin initialized successfully.")

    async def terminate(self) -> None:
        """AstrBot Star lifecycle termination hook."""
        for task in self._bg_tasks:
            if not task.done():
                task.cancel()
        self._bg_tasks.clear()
        try:
            await self.model_hub.dispose()
        except Exception as e:
            logger.warning(f"[OnlyYou] model_hub dispose failed: {e}")
        await self.db.close()
        logger.info("[OnlyYou] Plugin terminated cleanly.")

    # ==========================================================================
    # 0. BUSY_TASK 每日随机完成结果结算 + IDLE_ROAM 空闲游荡主动行为 (Phase 3)
    # ==========================================================================
    def _latest_l3_umo(self, allowed_platforms: Any = None) -> str:
        """Return the most recently seen ★L3 private-chat UMO (empty if none)."""
        try:
            values = list(self._known_l3_umos.values())
        except Exception:
            return ""
        for umo in reversed(values):
            if _umo_in_platform_scope(str(umo or ""), allowed_platforms=allowed_platforms):
                return str(umo or "")
        return ""

    def _surfing_enabled(self, soul: Any) -> bool:
        """Per-Soul cyber-surfing switch (``settings.surfing_enabled``, default True)."""
        try:
            return bool(getattr(soul, "surfing_enabled", True))
        except Exception:
            return True

    async def clear_platform_memory_state(self, platform_id: str) -> dict[str, int]:
        """Drop all **in-memory** chat state of one bot instance (Soul unbind).

        Persisted data (SQLite memories / users / surfing_history) is untouched.
        """
        clean = str(platform_id or "").strip()
        stats: dict[str, int] = {
            "windows": 0,
            "evicted": 0,
            "unread": 0,
            "messages": 0,
            "groups": 0,
            "l3": 0,
            "caches": 0,
        }
        if not clean:
            return stats
        prefix = f"{clean}:"

        clearer = getattr(self.session_buffer, "clear_platform_memory", None)
        if callable(clearer):
            try:
                raw = clearer(clean) or {}
                for key in ("windows", "evicted", "unread", "messages"):
                    stats[key] = int(raw.get(key, 0) or 0)
            except Exception as exc:  # noqa: BLE001 - unbind must never crash
                logger.warning(f"[OnlyYou] clear_platform_memory failed: {exc}")

        for attr in ("_last_group_lurk_ts", "_last_private_catchup_ts"):
            mapping = getattr(self, attr, None)
            if isinstance(mapping, dict):
                doomed = [key for key in mapping.keys() if str(key).startswith(prefix)]
                for key in doomed:
                    mapping.pop(key, None)
                stats["caches"] += len(doomed)

        l3 = getattr(self, "_known_l3_umos", None)
        if isinstance(l3, dict):
            doomed_l3 = [key for key, value in l3.items() if str(value).startswith(prefix)]
            for key in doomed_l3:
                l3.pop(key, None)
            stats["l3"] = len(doomed_l3)

        groups = getattr(self, "_known_group_umos", None)
        if isinstance(groups, set):
            doomed_groups = [umo for umo in list(groups) if str(umo).startswith(prefix)]
            for umo in doomed_groups:
                groups.discard(umo)
            stats["groups"] = len(doomed_groups)

        if stats["windows"] or stats["unread"] or stats["groups"]:
            logger.info(f"[OnlyYou] Cleared platform memory of {clean}: {stats}")
        return stats

    async def _maybe_run_scheduled_gc(self) -> dict[str, int]:
        """Persisted daily Forgetting GC + realtime T1 trimming (restart safe)."""
        stats: dict[str, int] = {
            "gc_ran": 0,
            "deleted_expired": 0,
            "deleted_overflow": 0,
            "deleted_tier1": 0,
            "deleted_tier2": 0,
            "decayed_tier2": 0,
            "promoted_to_tier3": 0,
        }
        cleaner = getattr(self.memory_manager, "cleanup_tier1", None)
        if callable(cleaner):
            try:
                trimmed = await cleaner()
                if isinstance(trimmed, dict):
                    for key in ("deleted_expired", "deleted_overflow"):
                        stats[key] = int(trimmed.get(key, 0) or 0)
            except Exception as exc:  # noqa: BLE001 - never break the heartbeat
                logger.warning(f"[OnlyYou] realtime T1 cleanup failed: {exc}")

        today = datetime.now().strftime("%Y-%m-%d")
        last_date = ""
        try:
            last_date = str(await self.db.get_setting("last_forgetting_gc_date", "") or "")
        except Exception:
            last_date = ""
        if last_date == today:
            return stats

        runner = getattr(self.memory_manager, "run_forgetting_gc", None)
        if callable(runner):
            try:
                full = await runner()
                if isinstance(full, dict):
                    for key in (
                        "deleted_tier1",
                        "deleted_tier2",
                        "decayed_tier2",
                        "promoted_to_tier3",
                    ):
                        stats[key] = int(full.get(key, 0) or 0)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[OnlyYou] daily Forgetting GC failed: {exc}")
        stats["gc_ran"] = 1
        try:
            await self.db.set_setting("last_forgetting_gc_date", today)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[OnlyYou] persist forgetting gc date failed: {exc}")
        return stats

    def _idle_roam_cooldown_seconds(self) -> int:
        """IDLE_ROAM proactive-action cooldown in seconds (`idle_roam_cooldown_minutes`)."""
        try:
            minutes = int(
                (self.config.get("roguelite_schedule_config") or {}).get(
                    "idle_roam_cooldown_minutes", 45
                )
            )
        except (TypeError, ValueError, AttributeError):
            minutes = 45
        return max(0, minutes) * 60

    def _roguelite_setting(self, key: str, default: Any) -> Any:
        try:
            return (self.config.get("roguelite_schedule_config") or {}).get(key, default)
        except (AttributeError, TypeError):
            return default

    def _group_lurk_cooldown_seconds(self) -> int:
        """Per-group GROUP_LURK cooldown (`unread_group_cooldown_minutes`, default 60)."""
        try:
            minutes = int(self._roguelite_setting("unread_group_cooldown_minutes", 60))
        except (TypeError, ValueError):
            minutes = 60
        return max(0, minutes) * 60

    def _private_catchup_cooldown_seconds(self) -> int:
        """Per-private-chat catch-up cooldown (`unread_private_cooldown_minutes`, default 60)."""
        try:
            minutes = int(
                self._roguelite_setting("unread_private_cooldown_minutes", 60)
            )
        except (TypeError, ValueError):
            minutes = 60
        return max(0, minutes) * 60

    def _unread_private_reply_enabled(self) -> bool:
        value = self._roguelite_setting("unread_private_reply_enabled", True)
        if isinstance(value, str):
            return value.strip().lower() not in ("0", "false", "off", "no")
        return bool(value)

    @staticmethod
    def _fallback_busy_completion(
        task_title: str, progress: int, interrupted_by: str = ""
    ) -> str:
        """Random 4-flavor fallback used when the decision LLM is unavailable."""
        title = (task_title or "手头的事").strip() or "手头的事"
        if progress < 100:
            who = (interrupted_by or "临时打断").strip() or "临时打断"
            templates = [
                f"{title}刚做到一半就被{who}打断了，心里有点痒痒的，回头还得接着弄",
                f"本来在{title}，结果被{who}叫走，进度停在{progress}%有点不甘心",
                f"{title}做到{progress}%只好先放下，杯子里的水都还没喝完，{who}就来了",
                f"差一点点就能把{title}收尾了，被{who}一打岔全乱套，回头再收拾吧",
            ]
        else:
            templates = [
                f"总算把{title}弄完了，中途差点把水杯碰翻，好险",
                f"{title}磕磕绊绊搞定了，窗外刚好下过一阵雨，回过神来天都暗了",
                f"花了小半天把{title}做完，最后发现有个小地方要返工，气死",
                f"{title}终于收尾，顺手奖励自己一颗糖，甜得值了",
            ]
        return random.choice(templates)

    async def settle_busy_block(
        self,
        soul: Any,
        run: Any,
        block: Any,
        progress_pct: int = 100,
        interrupted_by: str = "",
    ) -> str:
        """Settle one ``BUSY_TASK`` block: today's random one-line completion result.

        Called from the heartbeat / Web API lazy settlement and from
        ``BoundedScheduleMutator._execute_mutation`` (``busy_settler`` injection) when a
        busy block is interrupted halfway. The result is persisted on the block
        (``completion_result`` + ``busy_settled``) and written into Tier 1 memory.
        """
        settle_key: tuple[str, str] = ("", "")
        inflight: Optional[set[tuple[str, str]]] = None
        try:
            if block is None or run is None:
                return ""
            if getattr(block, "busy_settled", False):
                return str(getattr(block, "completion_result", "") or "")

            # Guard against double settlement: the LLM await below is an interleaving
            # point for the heartbeat sweep / Web API lazy settle / mutator settler.
            settle_key = (
                str(getattr(soul, "soul_id", "") or ""),
                str(getattr(block, "slot_key", "") or ""),
            )
            inflight = getattr(self, "_busy_settling", None)
            if inflight is not None:
                if settle_key in inflight:
                    return ""
                inflight.add(settle_key)

            try:
                progress = max(0, min(100, int(progress_pct)))
            except (TypeError, ValueError):
                progress = 100
            interrupted_by = (interrupted_by or "").strip()
            status_desc = (
                "顺利100%完成"
                if progress >= 100
                else f"做到{progress}%被{interrupted_by or '临时打断'}打断"
            )
            block_title = str(getattr(block, "title", "") or "").strip() or "手头的事"

            prompt = BUSY_TASK_COMPLETION_PROMPT.format(
                soul_name=getattr(soul, "name", "") or "她",
                archetype=getattr(soul, "archetype", "") or "普通网友",
                task_title=block_title,
                progress_pct=progress,
                status_desc=status_desc,
                today_date=datetime.now().strftime("%Y-%m-%d"),
            )
            try:
                data = await self.model_hub.call_llm_json(
                    prompt=prompt, role="decision", default=None
                )
            except Exception as exc:  # noqa: BLE001 - fall back to templates
                logger.warning(f"[OnlyYou] busy completion LLM failed: {exc}")
                data = None

            completion_memory = ""
            keywords = ""
            if isinstance(data, dict):
                completion_memory = str(data.get("completion_memory") or "").strip()
                keywords = str(data.get("keywords") or "").strip()
            if not completion_memory:
                completion_memory = self._fallback_busy_completion(
                    block_title, progress, interrupted_by
                )
            completion_memory = completion_memory[:60]
            if not keywords:
                keywords = f"繁忙,{block_title}"

            block.completion_result = completion_memory
            block.busy_settled = True
            await self.deck_engine.save_run(run)

            try:
                await self.memory_manager.add_memory(
                    soul_id=soul.soul_id,
                    content=completion_memory,
                    keywords=keywords,
                    importance=4,
                    memory_tier=1,
                )
            except Exception as exc:  # noqa: BLE001 - never break the mutator/heartbeat
                logger.warning(f"[OnlyYou] busy completion memory write failed: {exc}")
            if inflight is not None:
                inflight.discard(settle_key)
            return completion_memory
        except Exception as exc:  # noqa: BLE001 - must never break mutator/heartbeat
            if inflight is not None:
                try:
                    inflight.discard(settle_key)
                except Exception:  # pragma: no cover - defensive
                    pass
            logger.warning(f"[OnlyYou] settle_busy_block failed: {exc}")
            return ""

    async def _settle_finished_busy_blocks(
        self, soul: Any, run: Any, now_dt: Optional[datetime] = None
    ) -> list[str]:
        """Settle every finished-but-unsettled ``BUSY_TASK`` block of today's run."""
        results: list[str] = []
        try:
            now = now_dt or datetime.now()
            now_hour = now.hour + now.minute / 60.0 + now.second / 3600.0
            for slot in list(getattr(run, "slots", None) or []):
                try:
                    action = ActionType.coerce(getattr(slot, "action_type", None), None)
                    state = getattr(slot, "state", None)
                    if action != ActionType.BUSY_TASK and state != BotState.BUSY:
                        continue
                    if getattr(slot, "busy_settled", False):
                        continue
                    end_hour = float(getattr(slot, "end_hour", 24.0))
                    if end_hour > now_hour:
                        continue
                    text = await self.settle_busy_block(
                        soul, run, slot, progress_pct=100
                    )
                    if text:
                        results.append(text)
                except Exception as exc:  # noqa: BLE001 - one bad block must not stop the loop
                    logger.warning(
                        f"[OnlyYou] settle finished busy block failed: {exc}"
                    )
        except Exception as exc:  # noqa: BLE001 - never break the heartbeat/web API
            logger.warning(f"[OnlyYou] _settle_finished_busy_blocks failed: {exc}")
        return results

    def _pick_group_unread_candidate(
        self,
        allowed_platforms: Any = None,
        blocked_platforms: Any = None,
        soul_id: str = "",
    ) -> Optional[tuple[str, list[Any], str]]:
        """Pick the group with the most / freshest **unread backlog**.

        "Unread" = user messages blocked by the three-state gate while the bot was
        unavailable (RESTING: all tiers / BUSY: L1). Messages the atmosphere model
        judged as IGNORE are read and never enter the backlog.
        """
        now = time.time()
        cooldown = self._group_lurk_cooldown_seconds()
        try:
            chats = self.session_buffer.list_unread_chats(private=False, limit=10)
        except Exception as exc:  # noqa: BLE001 - never break the heartbeat
            logger.warning(f"[OnlyYou] list_unread_chats failed: {exc}")
            chats = []
        candidates: list[tuple[str, list[Any], str]] = []
        for umo, batch in chats:
            if not umo or umo == "default_umo" or not batch:
                continue
            if not _umo_in_platform_scope(
                umo,
                allowed_platforms=allowed_platforms,
                blocked_platforms=blocked_platforms,
            ):
                continue
            if cooldown > 0 and (now - self._last_group_lurk_ts.get(umo, 0.0)) < cooldown:
                continue
            try:
                transcript = self.session_buffer.get_window_transcript(
                    umo, max_turns=12
                )
            except Exception:
                transcript = "（暂无近期对话）"
            candidates.append((umo, batch, transcript))
        if not candidates:
            return None
        candidates.sort(
            key=lambda item: (
                -len(item[1]),
                -float(getattr(item[1][-1], "timestamp", 0.0) or 0.0),
            )
        )
        return candidates[0]

    async def _decide_group_unread_reply(
        self,
        soul: Any,
        run: Any,
        current_slot: Any,
        umo: str,
        batch: list[Any],
        transcript: str,
    ) -> bool:
        """Run the atmosphere/decision model over a group's unread backlog.

        This keeps the "先决策后回复" contract: the bot only chimes in when the
        decision model says REPLY. On LLM failure / malformed output we degrade
        to IGNORE (consume silently) so a broken decision model can never spam.
        """
        if not batch:
            return False
        last = batch[-1]
        try:
            unread_text = self.session_buffer.format_unread_batch(batch)
            window_text = (
                "【未读消息（Bot 当时在休息/忙，没有处理）】\n"
                + unread_text
                + "\n\n【最近群聊窗口】\n"
                + str(transcript or "（暂无近期对话）")
            )
            state_value = getattr(current_slot, "state", BotState.IDLE)
            state_text = (
                state_value.value if isinstance(state_value, BotState) else str(state_value)
            )
            prompt = ATMOSPHERE_EVAL_PROMPT.format(
                soul_name=getattr(soul, "name", "") or "小星",
                soul_archetype=getattr(soul, "archetype", "") or "普通网友",
                soul_summary=self._render_soul_summary(soul),
                current_state=state_text,
                current_slot_title=str(getattr(current_slot, "title", "") or "发呆闲逛"),
                window_transcript=window_text,
                sender_name=str(getattr(last, "sender_name", "") or "群友"),
                sender_id=str(getattr(last, "sender_id", "") or "unknown"),
                user_tier_label=str(
                    getattr(last, "user_tier_label", "") or "L1普通群友"
                ),
                latest_message=str(getattr(last, "content", "") or ""),
                is_direct_mention="否",
                surfing_preferences="、".join(getattr(soul, "surfing_preferences", []) or [])
                or "（无）",
            )
            data = await _hub_call_json(
                self.model_hub, prompt, role="decision", umo=umo, default=None, soul=soul
            )
        except Exception as exc:  # noqa: BLE001 - never break the heartbeat
            logger.warning(f"[OnlyYou] group unread decision failed: {exc}")
            return False
        if not isinstance(data, dict):
            logger.info("[OnlyYou] group unread decision: no JSON -> degrade to IGNORE")
            return False
        decision = str(data.get("decision") or "").strip().upper()
        return decision == "REPLY"

    @staticmethod
    def _extract_idle_roam_message(data: Any) -> str:
        """Extract the 30-char-ish proactive message from the roam LLM JSON."""
        if not isinstance(data, dict):
            return ""
        message = str(data.get("message") or "").strip()
        message = message.strip("\"'“”「」")
        return message[:60]

    def _resolve_idle_roam_target(
        self, data: Any, group_hint: str = "", allowed_platforms: Any = None
    ) -> str:
        """Resolve the LLM ``target`` (``GROUP`` / ``PRIVATE_L3``) into a concrete UMO."""
        if not isinstance(data, dict):
            return ""
        target = str(data.get("target") or "").strip().upper()
        if target in ("GROUP", "GROUP_LURK"):
            if group_hint:
                return group_hint
            try:
                groups = [
                    umo
                    for umo in self._known_group_umos
                    if umo
                    and umo != "default_umo"
                    and _umo_in_platform_scope(str(umo), allowed_platforms=allowed_platforms)
                ]
            except Exception:
                groups = []
            return random.choice(groups) if groups else ""
        if target in ("PRIVATE_L3", "PRIVATE"):
            return self._latest_l3_umo(allowed_platforms=allowed_platforms)
        if not target:
            return group_hint or self._latest_l3_umo(allowed_platforms=allowed_platforms)
        return ""

    @staticmethod
    def _pick_memory_muse_material(run: Any, memories: list[Any]) -> list[Any]:
        """Pick 1~2 memories, preferring today's settled BUSY_TASK result memories."""
        settled_texts: list[str] = []
        try:
            for slot in run.settled_busy_results():
                text = str(getattr(slot, "completion_result", "") or "").strip()
                if text:
                    settled_texts.append(text)
        except Exception:
            settled_texts = []
        priority: list[Any] = []
        for memory in memories:
            try:
                is_tier1 = int(getattr(memory, "memory_tier", 0) or 0) == 1
                content = str(getattr(memory, "content", "") or "")
            except Exception:
                continue
            if is_tier1 and any(
                text in content or content in text for text in settled_texts
            ):
                priority.append(memory)
        pool = priority or list(memories or [])
        if not pool:
            return []
        count = 1 if len(pool) <= 1 else 2
        return random.sample(pool, k=count)

    async def _run_idle_roam_action(
        self,
        soul: Any,
        run: Any,
        current_slot: Any,
        allowed_platforms: Any = None,
        blocked_platforms: Any = None,
    ) -> bool:
        """Execute one ``IDLE_ROAM`` proactive action (GROUP_LURK or MEMORY_MUSE).

        Branch A lurk: pick a known group with recent user messages, feed its sliding
        window to ``IDLE_ROAM_PROACTIVE_PROMPT`` and bubble up there or DM ★L3.
        Branch B muse (v2.8): consume today's settled BUSY_TASK results **once**; when
        exhausted, rotate memories sampled independently across the T1/T2/T3 buckets
        and refresh only their ``last_accessed_at`` after a successful send.
        Returns True only when a proactive message was actually sent.
        """
        try:
            now = time.time()
            cooldown = self._idle_roam_cooldown_seconds()
            if (
                cooldown > 0
                and (now - self._last_idle_roam_ts.get(soul.soul_id, 0.0)) < cooldown
            ):
                return False

            soul_id = str(getattr(soul, "soul_id", "") or "")
            slot_title = str(getattr(current_slot, "title", "") or "发呆闲逛")
            current_time = datetime.now().strftime("%H:%M")

            # ---- 分支 A：翻群记录吃瓜 (GROUP_LURK，先决策后回复) ----
            lurked = self._pick_group_unread_candidate(
                allowed_platforms=allowed_platforms,
                blocked_platforms=blocked_platforms,
                soul_id=soul_id,
            )
            if lurked is not None:
                umo, batch, transcript = lurked
                seqs = [int(getattr(message, "seq", 0) or 0) for message in batch]
                decision_reply = await self._decide_group_unread_reply(
                    soul, run, current_slot, umo, batch, transcript
                )
                if decision_reply:
                    unread_text = self.session_buffer.format_unread_batch(batch)
                    prompt = IDLE_ROAM_PROACTIVE_PROMPT.format(
                        soul_name=soul.name,
                        speaking_style=soul.speaking_style or "自然口语短句",
                        current_slot_title=slot_title,
                        roam_mode="GROUP_LURK",
                        context_material=(
                            f"群〖{umo}〗里你休息/忙时错过的未读消息：\n{unread_text}\n\n"
                            f"最近群聊上下文：\n{transcript}"
                        ),
                        current_time=current_time,
                    )
                    data = await _hub_call_json(
                        self.model_hub,
                        prompt,
                        role="decision",
                        umo=umo,
                        default=None,
                        soul=soul,
                    )
                    target_umo = self._resolve_idle_roam_target(
                        data, group_hint=umo, allowed_platforms=allowed_platforms
                    )
                    message = self._extract_idle_roam_message(data)
                    if target_umo and message:
                        audio_path = await self._maybe_synthesize_proactive(message, soul)
                        sent = await self.platform_bridge.send_proactive_message(
                            umo=target_umo,
                            text=message,
                            session_buffer=self.session_buffer,
                            soul_name=soul.name,
                            audio_path=audio_path,
                        )
                        if sent:
                            self._last_idle_roam_ts[soul.soul_id] = now
                            self._last_group_lurk_ts[umo] = now
                            self.session_buffer.ack_unread(umo, seqs)
                            logger.info(
                                "[OnlyYou] IDLE_ROAM(GROUP_LURK) proactive message sent "
                                f"to {target_umo} (consumed {len(batch)} unread)"
                            )
                            return True
                    # Decision said REPLY but generation/send failed: keep the
                    # backlog for a later retry, but cool this group down.
                    self._last_group_lurk_ts[umo] = now
                    logger.info(
                        "[OnlyYou] IDLE_ROAM(GROUP_LURK) decision=REPLY but send "
                        "failed; unread kept for retry"
                    )
                else:
                    # Decision model judged IGNORE -> these messages are consumed
                    # silently (read but not worth replying), never re-evaluated.
                    self.session_buffer.ack_unread(umo, seqs)
                    self._last_group_lurk_ts[umo] = now
                    logger.info(
                        "[OnlyYou] IDLE_ROAM(GROUP_LURK) decided IGNORE, consumed "
                        f"{len(batch)} unread from {umo}"
                    )

            # ---- 分支 B：发呆记忆联想 (MEMORY_MUSE) ----
            # v2.8: each settled BUSY_TASK result is mused at most once per Soul.
            mused_busy = getattr(self, "_mused_busy_keys", None)
            if not isinstance(mused_busy, dict):
                mused_busy = {}
                try:
                    self._mused_busy_keys = mused_busy
                except Exception:
                    pass
            used_keys = mused_busy.setdefault(soul.soul_id, set())
            busy_picks: list[tuple[str, str]] = []
            try:
                for settled_slot in run.settled_busy_results():
                    text = str(getattr(settled_slot, "completion_result", "") or "").strip()
                    slot_key = str(getattr(settled_slot, "slot_key", "") or "")
                    if not text or not slot_key or slot_key in used_keys:
                        continue
                    busy_picks.append((slot_key, text))
            except Exception:
                busy_picks = []
            busy_picks = busy_picks[:2]

            picked_memories: list[Any] = []
            use_busy = bool(busy_picks)
            if use_busy:
                context_material = "\n".join(
                    f"- 【今日繁忙成果】{text}" for _key, text in busy_picks
                )
            else:
                sampler = getattr(self.memory_manager, "sample_muse_memories", None)
                if callable(sampler):
                    recent_ids = list(
                        (getattr(self, "_mused_memory_ids", None) or {}).get(soul.soul_id, [])
                    )
                    try:
                        picked_memories = await sampler(
                            soul_id=soul.soul_id, exclude_ids=recent_ids, pick=2
                        )
                    except TypeError:
                        picked_memories = await sampler(soul.soul_id)
                    except Exception as exc:  # noqa: BLE001 - never break the heartbeat
                        logger.warning(f"[OnlyYou] sample_muse_memories failed: {exc}")
                        picked_memories = []
                if not picked_memories:
                    try:
                        fallback_pool = await self.memory_manager.list_memories(
                            soul_id=soul.soul_id, limit=10
                        )
                    except Exception:
                        fallback_pool = []
                    picked_memories = self._pick_memory_muse_material(run, fallback_pool)
                if not picked_memories:
                    logger.info(
                        "[OnlyYou] IDLE_ROAM skipped: no active group, no busy result "
                        "and no memory"
                    )
                    return False
                context_material = "\n".join(
                    f"- {getattr(item, 'content', '')}" for item in picked_memories
                )

            prompt = IDLE_ROAM_PROACTIVE_PROMPT.format(
                soul_name=soul.name,
                speaking_style=soul.speaking_style or "自然口语短句",
                current_slot_title=slot_title,
                roam_mode="MEMORY_MUSE",
                context_material=context_material,
                current_time=current_time,
            )
            data = await _hub_call_json(
                self.model_hub, prompt, role="decision", default=None, soul=soul
            )
            target_umo = self._resolve_idle_roam_target(
                data, group_hint="", allowed_platforms=allowed_platforms
            )
            message = self._extract_idle_roam_message(data)
            if target_umo and message:
                audio_path = await self._maybe_synthesize_proactive(message, soul)
                sent = await self.platform_bridge.send_proactive_message(
                    umo=target_umo,
                    text=message,
                    session_buffer=self.session_buffer,
                    soul_name=soul.name,
                    audio_path=audio_path,
                )
                if sent:
                    self._last_idle_roam_ts[soul.soul_id] = now
                    if use_busy:
                        for slot_key, _text in busy_picks:
                            used_keys.add(slot_key)
                    else:
                        memory_ids = [
                            int(getattr(item, "id", 0) or 0) for item in picked_memories
                        ]
                        memory_ids = [mid for mid in memory_ids if mid > 0]
                        toucher = getattr(
                            self.memory_manager, "touch_memories_for_muse", None
                        )
                        if memory_ids and callable(toucher):
                            try:
                                await toucher(memory_ids)
                            except Exception as exc:  # noqa: BLE001
                                logger.debug(f"[OnlyYou] touch muse memories failed: {exc}")
                        muse_ids = getattr(self, "_mused_memory_ids", None)
                        if not isinstance(muse_ids, dict):
                            muse_ids = {}
                            try:
                                self._mused_memory_ids = muse_ids
                            except Exception:
                                pass
                        ring = list(muse_ids.get(soul.soul_id, [])) + memory_ids
                        muse_ids[soul.soul_id] = ring[-30:]
                    logger.info(
                        "[OnlyYou] IDLE_ROAM(MEMORY_MUSE) proactive message sent "
                        f"to {target_umo} (source={'BUSY_RESULT' if use_busy else 'MEMORY'})"
                    )
                    return True
            logger.info(
                "[OnlyYou] IDLE_ROAM(MEMORY_MUSE) skipped: no target or empty message"
            )
            return False
        except Exception as exc:  # noqa: BLE001 - never break the heartbeat loop
            logger.warning(f"[OnlyYou] _run_idle_roam_action failed: {exc}")
            return False

    @staticmethod
    def _fallback_private_catchup(sender_name: str, count: int) -> str:
        """Deterministic fallback when the catch-up LLM is unavailable."""
        name = (sender_name or "").strip()
        templates = [
            "刚看到你发的消息，之前睡着了/在忙没顾上，现在才回你～",
            f"刚忙完才翻到手机，{name or '你'}发的消息我都看到啦，别急～",
            "刚才没看手机，看到消息啦，这就回你！",
            f"抱歉抱歉，之前没顾上回，攒了{count}条我这就看~",
        ]
        return random.choice(templates)

    async def _catch_up_private_unread(
        self,
        soul: Any,
        run: Any = None,
        current_slot: Any = None,
        allowed_platforms: Any = None,
        blocked_platforms: Any = None,
    ) -> int:
        """Proactively catch up private chats whose messages were missed.

        Only messages blocked by the three-state gate (RESTING: all / BUSY: L1)
        live in the unread backlog; when the bot is IDLE it sends one natural
        catch-up message per private chat (bounded per heartbeat, per-chat
        cooldown), then ACKs the consumed backlog.

        Returns the number of private chats caught up.
        """
        if not self._unread_private_reply_enabled():
            return 0
        try:
            candidates = self.session_buffer.list_unread_chats(
                private=True, limit=5
            )
        except Exception as exc:  # noqa: BLE001 - never break the heartbeat
            logger.warning(f"[OnlyYou] private unread listing failed: {exc}")
            return 0
        if not candidates:
            return 0

        now = time.time()
        cooldown = self._private_catchup_cooldown_seconds()
        slot_title = str(getattr(current_slot, "title", "") or "休息/忙档")
        current_time = datetime.now().strftime("%H:%M")
        processed = 0
        for umo, batch in candidates:
            if processed >= 2:
                break
            if not umo or not batch:
                continue
            if not _umo_in_platform_scope(
                umo,
                allowed_platforms=allowed_platforms,
                blocked_platforms=blocked_platforms,
            ):
                continue
            if cooldown > 0 and (now - self._last_private_catchup_ts.get(umo, 0.0)) < cooldown:
                continue
            # cool down even if the send fails, so we never tight-loop a broken chat
            self._last_private_catchup_ts[umo] = now
            last = batch[-1]
            sender_name = str(getattr(last, "sender_name", "") or "")
            try:
                recent_text = self.session_buffer.get_window_transcript(
                    umo, max_turns=8
                )
                prompt = UNREAD_PRIVATE_CATCHUP_PROMPT.format(
                    soul_name=getattr(soul, "name", "") or "小星",
                    soul_archetype=getattr(soul, "archetype", "") or "普通网友",
                    speaking_style=getattr(soul, "speaking_style", "") or "自然口语短句",
                    current_slot_title=slot_title,
                    current_time=current_time,
                    user_tier_label=str(
                        getattr(last, "user_tier_label", "") or "L1普通群友"
                    ),
                    user_name=sender_name or "对方",
                    unread_messages_text=self.session_buffer.format_unread_batch(batch),
                    recent_window_text=recent_text,
                )
                msg = await _hub_call_text(
                    self.model_hub, prompt, role="decision", umo=umo, soul=soul
                )
            except Exception as exc:  # noqa: BLE001 - fall back instead of crashing
                logger.warning(f"[OnlyYou] private unread catch-up LLM failed: {exc}")
                msg = ""
            text = str(msg or "").strip().strip("\"'“”「」")
            if not text:
                text = self._fallback_private_catchup(sender_name, len(batch))
            text = text[:120]
            try:
                audio_path = await self._maybe_synthesize_proactive(text, soul)
                sent = await self.platform_bridge.send_proactive_message(
                    umo=umo,
                    text=text,
                    session_buffer=self.session_buffer,
                    soul_name=getattr(soul, "name", "Bot") or "Bot",
                    audio_path=audio_path,
                )
            except Exception as exc:  # noqa: BLE001 - keep unread for a later retry
                logger.warning(f"[OnlyYou] private unread catch-up send failed: {exc}")
                sent = False
            if not sent:
                logger.info(
                    f"[OnlyYou] UNREAD_PRIVATE_CATCHUP send failed for {umo}; backlog kept"
                )
                continue
            self.session_buffer.ack_unread(
                umo, [int(getattr(message, "seq", 0) or 0) for message in batch]
            )
            processed += 1
            logger.info(
                "[OnlyYou] UNREAD_PRIVATE_CATCHUP sent to %s (consumed %d unread)",
                umo,
                len(batch),
            )
        return processed

    # ==========================================================================
    # Background Loops: 24h dynamic timeline dispatch + Forgetting GC
    # ==========================================================================
    async def _souls_for_heartbeat(self) -> list[Any]:
        """Souls driven by the 15-minute heartbeat (bound platforms + default Soul)."""
        try:
            souls = await self.soul_manager.list_souls()
        except Exception as exc:  # noqa: BLE001 - never break the heartbeat
            logger.warning(f"[OnlyYou] heartbeat list_souls failed: {exc}")
            return []
        if not souls:
            return []
        try:
            bindings = await self.soul_manager.get_platform_bindings()
        except Exception:
            bindings = {}
        if not bindings:
            # Backward compatible: no bot instance bound yet -> single default Soul.
            default_id = ""
            try:
                default_id = str(await self.soul_manager.get_default_soul_id() or "")
            except Exception:
                default_id = ""
            for soul in souls:
                if soul.soul_id == default_id:
                    return [soul]
            return [souls[0]]
        selected = [
            soul
            for soul in souls
            if list(getattr(soul, "bound_platforms", None) or [])
        ]
        default_id = ""
        try:
            default_id = str(await self.soul_manager.get_default_soul_id() or "")
        except Exception:
            default_id = ""
        if default_id:
            for soul in souls:
                if soul.soul_id == default_id and soul not in selected:
                    selected.append(soul)
        return selected or [souls[0]]

    async def _dispatch_heartbeat_for_soul(
        self, soul: Any, bindings: Optional[dict[str, str]] = None, sole_soul: bool = False
    ) -> None:
        """Run one Soul's 15-minute dispatch on its own timeline / model config."""
        bindings = dict(bindings or {})
        default_id = ""
        try:
            default_id = str(await self.soul_manager.get_default_soul_id() or "")
        except Exception:
            default_id = ""
        bound = set(getattr(soul, "bound_platforms", None) or [])
        if sole_soul:
            allowed_platforms: Any = None
            blocked_platforms: Any = None
        elif bound:
            allowed_platforms = bound
            blocked_platforms = None
        elif default_id and soul.soul_id == default_id:
            # Default Soul is the fallback for bot instances nobody bound.
            allowed_platforms = None
            blocked_platforms = set(bindings.keys())
        else:
            allowed_platforms = set()
            blocked_platforms = None

        run, current_slot, _ = await self.deck_engine.get_current_and_next_slot(
            soul_id=soul.soul_id,
            custom_cards=soul.state_event_cards,
            custom_encounters=soul.encounter_talk_cards,
            chronotype=soul.chronotype,
        )

        # 1) Lazily settle every BUSY_TASK block that already reached 100%.
        await self._settle_finished_busy_blocks(soul, run)

        # 2) Dispatch on the current block's action type (state fallback).
        action = ActionType.coerce(getattr(current_slot, "action_type", None), None)
        if action is None:
            if current_slot.state == BotState.RESTING:
                action = ActionType.SLEEP
            elif current_slot.state == BotState.BUSY:
                action = ActionType.BUSY_TASK
            else:
                action = ActionType.SURF

        if action in (ActionType.SLEEP, ActionType.BUSY_TASK):
            return

        # Any IDLE block: first catch up private chats blocked while resting/busy.
        await self._catch_up_private_unread(
            soul,
            run,
            current_slot,
            allowed_platforms=allowed_platforms,
            blocked_platforms=blocked_platforms,
        )

        if action == ActionType.SURF:
            if not self._surfing_enabled(soul):
                # v2.8: cyber-surfing switched off -> degrade to idle roaming.
                logger.info(
                    f"[OnlyYou] SURF disabled for soul={soul.soul_id}; "
                    "degrading to IDLE_ROAM."
                )
                await self._run_idle_roam_action(
                    soul,
                    run,
                    current_slot,
                    allowed_platforms=allowed_platforms,
                    blocked_platforms=blocked_platforms,
                )
                return
            review = await self.surfing_pipeline.surf_once(
                soul=soul, current_state=BotState.IDLE
            )
            await self._maybe_share_surfed_video(soul, run, current_slot, review)
            return

        if action == ActionType.IDLE_ROAM:
            await self._run_idle_roam_action(
                soul,
                run,
                current_slot,
                allowed_platforms=allowed_platforms,
                blocked_platforms=blocked_platforms,
            )

    async def _maybe_share_surfed_video(
        self, soul: Any, run: Any, current_slot: Any, review: Any
    ) -> bool:
        """Share one surfed video to the latest ★L3 only when all v2.8 gates pass.

        Gates: not a degraded vision fallback + video never shared before +
        ``score >= surf_share_min_score`` + surf-share cooldown + IDLE_ROAM cooldown.
        """
        try:
            if review is None:
                return False
            video = getattr(review, "video", None)
            video_id = str(getattr(video, "video_id", "") or "").strip()
            if not video:
                return False
            if bool(getattr(review, "is_fallback", False)):
                logger.info("[OnlyYou] SURF share skipped: vision fallback result")
                return False
            if not video_id:
                return False
            soul_id = str(getattr(soul, "soul_id", "") or "")

            shared_map = getattr(self, "_shared_video_ids", None)
            if not isinstance(shared_map, dict):
                shared_map = {}
                try:
                    self._shared_video_ids = shared_map
                except Exception:
                    pass
            shared_ids = shared_map.setdefault(soul_id, set())
            if video_id in shared_ids:
                logger.info("[OnlyYou] SURF share skipped: video already shared")
                return False

            surf_cfg = {}
            try:
                surf_cfg = dict(self.config.get("surfing_config") or {})
            except Exception:
                surf_cfg = {}
            try:
                min_score = int(surf_cfg.get("surf_share_min_score", 80) or 80)
            except (TypeError, ValueError):
                min_score = 80
            try:
                score = int(getattr(review, "score", 0) or 0)
            except (TypeError, ValueError):
                score = 0
            if score < min_score:
                logger.info(
                    f"[OnlyYou] SURF share skipped: score {score} < min {min_score}"
                )
                return False

            now = time.time()
            try:
                cooldown_minutes = int(
                    surf_cfg.get("surf_share_cooldown_minutes", 90) or 90
                )
            except (TypeError, ValueError):
                cooldown_minutes = 90
            last_share_map = getattr(self, "_last_surf_share_ts", None)
            if not isinstance(last_share_map, dict):
                last_share_map = {}
                try:
                    self._last_surf_share_ts = last_share_map
                except Exception:
                    pass
            last_share = float(last_share_map.get(soul_id, 0.0) or 0.0)
            if cooldown_minutes > 0 and (now - last_share) < cooldown_minutes * 60:
                logger.info(
                    "[OnlyYou] SURF share skipped: surf-share cooldown "
                    f"({cooldown_minutes}min)"
                )
                return False

            idle_cooldown = self._idle_roam_cooldown_seconds()
            try:
                last_roam = float(
                    (getattr(self, "_last_idle_roam_ts", None) or {}).get(soul_id, 0.0)
                    or 0.0
                )
            except Exception:
                last_roam = 0.0
            if idle_cooldown > 0 and (now - last_roam) < idle_cooldown:
                logger.info(
                    "[OnlyYou] SURF share skipped: IDLE_ROAM cooldown "
                    f"({idle_cooldown // 60}min)"
                )
                return False

            target_umo = self._latest_l3_umo()
            if not target_umo:
                logger.info("[OnlyYou] SURF share skipped: no ★L3 private session")
                return False

            video_text = (
                f"《{getattr(video, 'title', '')}》"
                f"({getattr(video, 'share_url', '')}) - {getattr(review, 'commentary', '')}"
            )
            prompt = PROACTIVE_CHAT_PROMPT.format(
                soul_name=soul.name,
                soul_archetype=soul.archetype,
                speaking_style=soul.speaking_style,
                soul_summary=self._render_soul_summary(soul),
                current_slot_title=str(getattr(current_slot, "title", "") or "刷视频"),
                video_text=video_text,
            )
            msg = await _hub_call_text(
                self.model_hub, prompt, role="decision", umo=target_umo, soul=soul
            )
            if not msg:
                return False
            audio_path = await self._maybe_synthesize_proactive(msg, soul)
            sent = await self.platform_bridge.send_proactive_message(
                umo=target_umo,
                text=msg,
                session_buffer=self.session_buffer,
                soul_name=soul.name,
                audio_path=audio_path,
            )
            if sent:
                shared_ids.add(video_id)
                last_share_map[soul_id] = now
                logger.info(
                    f"[OnlyYou] SURF share sent to {target_umo} "
                    f"(video={video_id}, score={score})"
                )
            return bool(sent)
        except Exception as exc:  # noqa: BLE001 - never break the heartbeat
            logger.warning(f"[OnlyYou] _maybe_share_surfed_video failed: {exc}")
            return False

    async def _heartbeat_loop(self) -> None:
        """Every 15 minutes: per-Soul busy settlement + action dispatch.

        v2.8 drives **every Soul that owns bot instances** (plus the default Soul as
        the fallback for unbound platforms), each with its own timeline, model config
        and platform-scoped unread backlog.  Also runs the restart-safe daily GC.
        """
        while True:
            try:
                await asyncio.sleep(900)
                await self._ensure_initialized()
                await self._maybe_run_scheduled_gc()
                souls = await self._souls_for_heartbeat()
                try:
                    bindings = await self.soul_manager.get_platform_bindings()
                except Exception:
                    bindings = {}
                sole_soul = len(souls) <= 1 and not bindings
                for soul in souls:
                    try:
                        await self._dispatch_heartbeat_for_soul(
                            soul, bindings=bindings, sole_soul=sole_soul
                        )
                    except Exception as exc:  # noqa: BLE001 - one Soul must not stop others
                        logger.warning(
                            f"[OnlyYou] Heartbeat dispatch failed for soul="
                            f"{getattr(soul, 'soul_id', '?')}: {exc}"
                        )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"[OnlyYou] Heartbeat loop error: {e}")

    async def _daily_gc_loop(self) -> None:
        """Fallback daily GC ticker (the persisted date keeps it idempotent)."""
        while True:
            try:
                await asyncio.sleep(86400)
                await self._ensure_initialized()
                await self._maybe_run_scheduled_gc()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"[OnlyYou] Daily Forgetting GC error: {e}")

    async def _on_window_overflow(self, umo: str, transcript: str) -> None:
        """Callback triggered when `SessionBuffer` evicts `extract_every_n_messages` messages."""
        soul = await self.soul_manager.resolve_soul(umo=umo, allow_llm_seed=False)
        await self.memory_manager.extract_memories_from_transcript(
            soul_id=soul.soul_id,
            soul_name=soul.name,
            transcript_text=transcript,
            umo=umo,
        )

    @staticmethod
    def _render_soul_summary(soul: Any) -> str:
        """Build the compact `{soul_summary}` snippet for prompts (safe fallback)."""
        try:
            card = getattr(soul, "card", None)
            if not card:
                return ""
            return render_card_summary(card)
        except Exception as exc:
            logger.debug(f"[OnlyYou] render_card_summary failed: {exc}")
            return ""

    async def _maybe_synthesize_proactive(
        self, text: str, soul: Any = None
    ) -> Optional[str]:
        """Synthesize a proactive message with the Soul's TTS config (best effort)."""
        try:
            tts_cfg = _hub_tts_config(self.model_hub, soul)
            if (tts_cfg.get("mode") or "off") == "off" or not text:
                return None
            try:
                probability = float(tts_cfg.get("probability", 1.0))
            except (TypeError, ValueError):
                probability = 1.0
            if random.random() > probability:
                return None
            return await _hub_synthesize(self.model_hub, text, soul)
        except Exception as exc:
            logger.debug(f"[OnlyYou] proactive TTS skipped: {exc}")
            return None

    # ==========================================================================
    # 1. Three-State Message Gate (`priority=999`)
    # ==========================================================================
    @filter.event_message_type(filter.EventMessageType.ALL, priority=999)
    async def on_any_message(self, event: AstrMessageEvent):
        """
        First-stage gate for every incoming group/private message:
        1. Pass through system/plugin commands (`/onlyyou`, `/oy`, `/...`)
        2. Silently record message into sliding window (`SessionBuffer`)
        3. Check input-driven bounded schedule mutation (`BoundedScheduleMutator`)
        4. Enforce 3-state gate (`RESTING` blocks all, `BUSY` blocks L1, `IDLE` allows all)
        5. Run group `AtmosphereReader` and coordinate with native pipeline via `PlatformBridge`
        """
        await self._ensure_initialized()

        msg_str = (getattr(event, "message_str", "") or "").strip()
        if not msg_str:
            return

        # Allow commands through without interference
        lower_msg = msg_str.lower()
        if (
            msg_str.startswith("/")
            or lower_msg == "onlyyou"
            or lower_msg.startswith("onlyyou ")
            or lower_msg == "oy"
            or lower_msg.startswith("oy ")
        ):
            # ``/onlyyou`` is admin-only by default: every sub-command handler
            # carries AstrBot's PermissionTypeFilter(ADMIN), which the dashboard
            # "指令管理" panel can override per command. Two cases bypass that
            # filter and are handled here instead:
            #   1. AstrBot's command-group matcher only *wakes* the event for an
            #      unknown sub-command (no CommandFilter matches), so the typo
            #      would fall through to the main LLM;
            #   2. the bare group name (`/onlyyou`) makes AstrBot raise the
            #      "参数不足" command tree before permission filters run.
            cmd_tokens = msg_str.split()
            cmd_name = cmd_tokens[0].lstrip("/").lower() if cmd_tokens else ""
            if cmd_name in ("onlyyou", "oy"):
                sub = cmd_tokens[1].lower() if len(cmd_tokens) > 1 else ""
                is_admin = bool(getattr(event, "is_admin", None) and event.is_admin())
                if not is_admin:
                    event.stop_event()
                    await event.send(
                        MessageChain().message(self._ADMIN_ONLY_HINT)
                    )
                elif sub and sub not in self.ONLYYOU_SUBCOMMANDS:
                    event.stop_event()
                    await event.send(MessageChain().message(self._onlyyou_help_text()))
            return

        umo = getattr(event, "unified_msg_origin", "") or "default_umo"
        soul = await self.soul_manager.resolve_soul(umo=umo)
        user = await self.user_relation.resolve_user(event)
        self._last_message_tier[umo] = user.tier

        if user.tier == UserTier.L3_CORE and self.atmosphere_reader.is_private_event(event):
            # Refresh insertion order so `_latest_l3_umo()` really returns the MRU chat.
            self._known_l3_umos.pop(user.global_uid, None)
            self._known_l3_umos[user.global_uid] = umo

        # 1. Record into sliding window & extract reply topology
        _, is_direct_mention, buffered_message = self.atmosphere_reader.record_message(
            event=event,
            user=user,
            on_overflow_callback=self._on_window_overflow,
        )

        is_private = self.atmosphere_reader.is_private_event(event)
        if not is_private and umo and umo != "default_umo":
            # Remember active groups for IDLE_ROAM(GROUP_LURK) proactive actions.
            self._known_group_umos.add(umo)
            # v2.8: maintain the persisted group card / last-active profile.
            touch_group = getattr(self.user_relation, "touch_group", None)
            if callable(touch_group):
                try:
                    group_id, group_name = _extract_group_meta(event, umo)
                    await touch_group(
                        umo=umo,
                        group_id=group_id,
                        platform_id=PlatformBridge.extract_platform_id(umo),
                        display_name=group_name,
                        bound_soul_id=str(getattr(soul, "soul_id", "") or ""),
                    )
                except Exception as exc:  # noqa: BLE001 - never block message handling
                    logger.debug(f"[OnlyYou] touch_group failed: {exc}")
        recent_burst = self.session_buffer.get_recent_l2_l3_burst_count(umo=umo)

        # 2. Check if high-impact message mutates the current/next slot state
        await self.schedule_mutator.on_message_input(
            soul=soul,
            event=event,
            user=user,
            is_private_chat=is_private,
            is_direct_mention=is_direct_mention,
            recent_l2_l3_burst_count=recent_burst,
        )

        current_slot = await self.deck_engine.get_current_slot(
            soul_id=soul.soul_id,
            custom_cards=soul.state_event_cards,
            custom_encounters=soul.encounter_talk_cards,
            chronotype=soul.chronotype,
        )

        # 3. Three-state message gate (zero-token pre-filter)
        gate = StateController.check_message_gate(
            state=current_slot.state,
            user_tier=user.tier,
            is_private_chat=is_private,
        )
        if not gate.allowed:
            logger.info(
                f"[OnlyYou] Gate intercepted message from {user.display_name}({user.tier.label}): {gate.reason}"
            )
            # Unread definition: messages that the state gate blocked (RESTING: all
            # tiers / BUSY: L1) were "missed while unavailable" -> backlog.
            # Messages that pass the gate but are judged IGNORE by the atmosphere
            # model are considered read and never enter this backlog.
            self.session_buffer.mark_unread(
                umo, buffered_message, is_private=is_private
            )
            self.platform_bridge.apply_reply_decision(event, should_reply=False)
            return

        # 4. Atmosphere evaluation for group chats (or direct pass for private chats)
        should_reply = await self.atmosphere_reader.evaluate_should_reply(
            event=event,
            soul=soul,
            user=user,
            current_slot=current_slot,
            is_direct_mention=is_direct_mention,
        )
        # Let AstrBot's native pipeline (`_select_provider`) use our main model.
        if should_reply:
            try:
                # per-Soul model: get_main_provider_id(soul_id=...) via the
                # TypeError-safe helper (legacy hubs without soul_id still work)
                main_provider_id = await _hub_main_provider_id(self.model_hub, soul)
                if main_provider_id:
                    event.set_extra("selected_provider", main_provider_id)
            except Exception as e:
                logger.warning(f"[OnlyYou] resolve main provider failed: {e}")
        self.platform_bridge.apply_reply_decision(event, should_reply=should_reply)

    # ==========================================================================
    # 2. Context & Token Budget Hook (`on_llm_request`)
    # ==========================================================================
    @filter.on_llm_request()
    async def on_llm_request_hook(self, event: AstrMessageEvent, req: ProviderRequest):
        """
        Take over `req.contexts` (`req.contexts.clear()`), inject sliding window,
        recall & score memories within hard char/count budget, and inject 3-state prompt.
        """
        await self._ensure_initialized()
        umo = getattr(event, "unified_msg_origin", "") or "default_umo"
        soul = await self.soul_manager.resolve_soul(umo=umo, allow_llm_seed=False)
        user = await self.user_relation.resolve_user(event)
        run, current_slot, _ = await self.deck_engine.get_current_and_next_slot(
            soul_id=soul.soul_id,
            custom_cards=soul.state_event_cards,
            custom_encounters=soul.encounter_talk_cards,
            chronotype=soul.chronotype,
        )

        await self.context_budget.apply_context_and_memory_budget(
            event=event,
            req=req,
            soul=soul,
            user=user,
            run=run,
            current_slot=current_slot,
        )

    # ==========================================================================
    # 3. Response Recording & Mutation Monologue Consumption (`on_llm_response`)
    # ==========================================================================
    @filter.on_llm_response()
    async def on_llm_response_hook(self, event: AstrMessageEvent, resp: LLMResponse):
        """Record the bot's reply into ``SessionBuffer`` and consume the one-shot mutation monologue."""
        await self._ensure_initialized()
        reply_text = (getattr(resp, "completion_text", "") or "").strip()
        if not reply_text:
            return

        umo = getattr(event, "unified_msg_origin", "") or "default_umo"
        soul = await self.soul_manager.resolve_soul(umo=umo, allow_llm_seed=False)

        self.session_buffer.record_assistant_message(
            umo=umo,
            content=reply_text,
            is_proactive=False,
            soul_name=soul.name,
            on_overflow_callback=self._on_window_overflow,
        )

        # Consume the one-shot mutation monologue after the first post-mutation reply.
        run, current_slot, _ = await self.deck_engine.get_current_and_next_slot(
            soul_id=soul.soul_id,
            custom_cards=soul.state_event_cards,
            custom_encounters=soul.encounter_talk_cards,
            chronotype=soul.chronotype,
        )
        if current_slot.mutation_monologue:
            current_slot.mutation_monologue = ""
            await self.deck_engine.save_run(run)

    # ==========================================================================
    # 4. Plugin-autonomous TTS (`on_decorating_result`, runs before native TTS)
    # ==========================================================================
    async def _tts_scope_matches(self, event: AstrMessageEvent, scope: str) -> bool:
        """Whether the current session falls into the configured TTS scope."""
        try:
            if scope == "all":
                return True
            is_private = self.atmosphere_reader.is_private_event(event)
            if scope == "private":
                return is_private
            if scope == "l3":
                umo = getattr(event, "unified_msg_origin", "") or "default_umo"
                tier = self._last_message_tier.get(umo)
                if tier is not None:
                    return tier == UserTier.L3_CORE
                # L3 private sessions are recorded by the message gate
                return umo in self._known_l3_umos.values()
        except Exception as exc:
            logger.debug(f"[OnlyYou] TTS scope check failed: {exc}")
        return False

    @filter.on_decorating_result()
    async def on_decorating_result_hook(self, event: AstrMessageEvent):
        """Replace LLM text replies with a synthesized voice message (best effort).

        AstrBot's ``ResultDecorateStage`` runs ``on_decorating_result`` hooks *before*
        segmented reply and native TTS, so converting the ``Plain`` segments into a
        ``Record`` here guarantees the native TTS cannot synthesize the same text
        again (unless ``dual_output`` is enabled by the user).
        """
        try:
            umo = getattr(event, "unified_msg_origin", "") or "default_umo"
            try:
                soul = await self.soul_manager.resolve_soul(
                    umo=umo, allow_llm_seed=False
                )
            except Exception:
                soul = None
            tts_cfg = _hub_tts_config(self.model_hub, soul)
            if (tts_cfg.get("mode") or "off") == "off":
                return
            try:
                probability = float(tts_cfg.get("probability", 1.0))
            except (TypeError, ValueError):
                probability = 1.0
            if random.random() > probability:
                return
            scope = str(tts_cfg.get("scope") or "private")
            if not await self._tts_scope_matches(event, scope):
                return

            result = event.get_result()
            if result is None:
                return
            chain = getattr(result, "chain", None)
            if not chain:
                return
            is_llm_result = getattr(result, "is_llm_result", None)
            if callable(is_llm_result):
                try:
                    if not is_llm_result():
                        return
                except Exception:
                    pass

            plain_texts = []
            for comp in chain:
                if isinstance(comp, Plain):
                    plain_texts.append(str(getattr(comp, "text", "") or ""))
            text = "".join(plain_texts).strip()
            if not text:
                return

            audio_path = await _hub_synthesize(self.model_hub, text, soul)
            if not audio_path:
                return

            dual_output = bool(tts_cfg.get("dual_output"))
            new_chain = []
            replaced = False
            for comp in chain:
                if isinstance(comp, Plain):
                    if replaced:
                        continue
                    if dual_output:
                        new_chain.append(Plain(text))
                    try:
                        new_chain.append(
                            Record(file=audio_path, url=audio_path, text=text)
                        )
                    except TypeError:  # older AstrBot Record signature
                        new_chain.append(Record(file=audio_path))
                    replaced = True
                else:
                    new_chain.append(comp)
            if replaced:
                result.chain = new_chain
        except Exception as exc:
            logger.warning(f"[OnlyYou] TTS decorating hook failed: {exc}")

    # ==========================================================================
    # 5. Diagnostic & Management Command (`/onlyyou`, alias `/oy`)
    # ==========================================================================
    # Registered as an AstrBot **command group** so that the AstrBot dashboard
    # plugin panel renders the complete sub-command list (status / state /
    # reroll / tier / memory / surf / unread / help) instead of a single
    # opaque `/onlyyou` entry. Each sub-command handler below is a thin
    # delegate; all real logic lives in `_dispatch_onlyyou`.
    #
    # NOTE: the group function itself is never executed by AstrBot (the wake
    # stage only uses its CommandGroupFilter for matching and for the
    # "参数不足" command tree); keep it as a no-op.
    @filter.command_group("onlyyou", alias={"oy"})
    def onlyyou(self):
        """OnlyYou 调试与管理指令组（状态 / 日程 / 关系 / 记忆 / 冲浪 / 未读）"""
        pass

    @onlyyou.command("status")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def onlyyou_status(self, event: AstrMessageEvent):
        """查看角色卡、24h 动态时间轴、固定护甲档位与进度、微调配额、今日繁忙成果"""
        async for result in self._dispatch_onlyyou(event, "status"):
            yield result

    @onlyyou.command("state")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def onlyyou_state(self, event: AstrMessageEvent):
        """强制切换当前时间块状态（RESTING|BUSY|IDLE [标题]；护甲按行动类型自动取固定档位）"""
        async for result in self._dispatch_onlyyou(event, "state"):
            yield result

    @onlyyou.command("reroll")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def onlyyou_reroll(self, event: AstrMessageEvent):
        """按专属行动卡池与作息曲线重新抽取今日 24h 动态时间轴"""
        async for result in self._dispatch_onlyyou(event, "reroll"):
            yield result

    @onlyyou.command("tier")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def onlyyou_tier(self, event: AstrMessageEvent):
        """查看或修改用户关系等级（1=L1普通 / 2=L2重要 / 3=★L3核心管理员级）"""
        async for result in self._dispatch_onlyyou(event, "tier"):
            yield result

    @onlyyou.command("memory")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def onlyyou_memory(self, event: AstrMessageEvent):
        """管理分级记忆与遗忘 GC（list / add / extract / gc）"""
        async for result in self._dispatch_onlyyou(event, "memory"):
            yield result

    @onlyyou.command("surf")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def onlyyou_surf(self, event: AstrMessageEvent):
        """在 IDLE 状态下立即触发一次赛博冲浪看片与吐槽分享"""
        async for result in self._dispatch_onlyyou(event, "surf"):
            yield result

    @onlyyou.command("unread")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def onlyyou_unread(self, event: AstrMessageEvent):
        """查看未读消息队列 / 立即补回 / 清空（list / flush / clear）"""
        async for result in self._dispatch_onlyyou(event, "unread"):
            yield result

    @onlyyou.command("help")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def onlyyou_help(self, event: AstrMessageEvent):
        """显示 OnlyYou 完整指令列表"""
        async for result in self._dispatch_onlyyou(event, "help"):
            yield result

    def _onlyyou_help_text(self) -> str:
        """Complete `/onlyyou` command list (shared by `help` and typo fallback)."""
        return (
            "OnlyYou 指令列表（别名 /oy，仅管理员可用；直接发送 /onlyyou 可查看指令树）：\n"
            "• /onlyyou status - 查看 24h 动态时间轴、繁忙进度与今日繁忙成果\n"
            "• /onlyyou state <RESTING|BUSY|IDLE> [标题] - 切换当前时间块状态（护甲按行动类型固定档位）\n"
            "• /onlyyou reroll - 重新抽取今日动态时间轴与专属行动卡\n"
            "• /onlyyou tier [user_id] [1|2|3] - 查看或修改用户关系等级（★L3=管理员级，无好感度）\n"
            "• /onlyyou memory [list|gc|extract|add] - 管理三级记忆与遗忘GC\n"
            "• /onlyyou surf - 在 IDLE 状态下触发一次刷视频\n"
            "• /onlyyou unread [list|flush|clear] - 查看/立即补回/清空因休息或繁忙错过的未读消息\n"
            "• /onlyyou help - 显示本指令列表"
        )

    async def _dispatch_onlyyou(self, event: AstrMessageEvent, default_sub: str = "status"):
        """Shared implementation for every `/onlyyou` sub-command."""
        await self._ensure_initialized()
        umo = getattr(event, "unified_msg_origin", "") or "default_umo"
        raw_msg = (getattr(event, "message_str", "") or "").strip()
        tokens = raw_msg.split()
        # Strip command token ("/onlyyou", "onlyyou", "/oy", "oy")
        if tokens and tokens[0].lstrip("/").lower() in ("onlyyou", "oy"):
            tokens = tokens[1:]

        sub = tokens[0].lower() if tokens else default_sub
        soul = await self.soul_manager.resolve_soul(umo=umo)
        user = await self.user_relation.resolve_user(event)

        if sub == "status":
            run, cur_slot, next_slot = await self.deck_engine.get_current_and_next_slot(
                soul_id=soul.soul_id,
                custom_cards=soul.state_event_cards,
                custom_encounters=soul.encounter_talk_cards,
                chronotype=soul.chronotype,
            )
            # Lazily settle finished BUSY_TASK blocks so "今日繁忙成果" is fresh.
            await self._settle_finished_busy_blocks(soul, run)

            chrono_label = {
                "early": "早起型",
                "normal": "规律型",
                "night_owl": "夜猫子",
            }.get(soul.chronotype, "规律型")
            progress_pct = cur_slot.get_progress_pct()
            start_time, end_time = cur_slot.format_time_range().split("-")
            mut_tag = " [⚡已微调]" if cur_slot.mutated else ""
            slot_lines = []
            for s in run.slots:
                marker = "👉 " if s.slot_key == cur_slot.slot_key else "   "
                s_mut_tag = " [⚡已微调]" if s.mutated else ""
                slot_lines.append(
                    f"{marker}{s.format_time_range()} [{s.action.label}] "
                    f"{s.state.value} | {s.title} (护甲:Lv.{s.action.armor_tier}·{s.armor}){s_mut_tag}"
                )
            busy_results = [
                s for s in run.settled_busy_results() if s.completion_result
            ]
            if busy_results:
                busy_lines = [
                    f"- 【{s.title}】{s.completion_result}" for s in busy_results
                ]
            else:
                busy_lines = ["- （还没有忙完什么事）"]
            text = (
                f"🌟 【OnlyYou 状态面板】\n"
                f"• 当前角色卡：{soul.name} ({soul.soul_id}) —— {soul.archetype} (作息:{chrono_label})\n"
                f"• 你的关系等级：{user.tier.label} (ID: {user.global_uid}, 互动:{user.interaction_count}次)\n"
                f"• 今日基调卡：【{run.vigor_title}】{run.vigor_desc}\n"
                f"• 当前时间块：【{cur_slot.action.label}】{cur_slot.state.value} | "
                f"{cur_slot.title} (护甲:Lv.{cur_slot.action.armor_tier}·{cur_slot.armor}){mut_tag}\n"
                f"• 当前进度：{progress_pct}% ({start_time}-{end_time})\n"
                f"• 今日微调配额：{run.mutations_used}/{self.schedule_mutator.max_mutations_per_day} 次\n"
                f"• 未读消息：群聊 {self.session_buffer.get_unread_count(is_private=False)} 条 / "
                f"私聊 {self.session_buffer.get_unread_count(is_private=True)} 条"
                f"（/onlyyou unread 查看 · flush 立即补回）\n"
                f"\n📅 【今日动态时间轴 ({run.date_str})】\n"
                + "\n".join(slot_lines)
                + "\n\n🏆 【今日繁忙成果】\n"
                + "\n".join(busy_lines)
            )
            yield event.plain_result(text)
            return

        if sub == "unread":
            action = tokens[1].lower() if len(tokens) >= 2 else "list"
            if action == "clear":
                removed = self.session_buffer.clear_unread()
                yield event.plain_result(f"🧹 已清空未读消息队列（{removed} 条）。")
                return
            if action == "flush":
                run, cur_slot, _ = await self.deck_engine.get_current_and_next_slot(
                    soul_id=soul.soul_id,
                    custom_cards=soul.state_event_cards,
                    custom_encounters=soul.encounter_talk_cards,
                    chronotype=soul.chronotype,
                )
                await self._settle_finished_busy_blocks(soul, run)
                if cur_slot.state != BotState.IDLE:
                    yield event.plain_result(
                        f"⛔ 当前处于【{cur_slot.state.value} ({cur_slot.title})】，"
                        "未读补回仅在 IDLE 空闲状态下执行（休息/繁忙时不打扰）。"
                    )
                    return
                private_done = await self._catch_up_private_unread(
                    soul, run, cur_slot
                )
                group_sent = await self._run_idle_roam_action(soul, run, cur_slot)
                yield event.plain_result(
                    "📨 未读补回执行完毕：\n"
                    f"• 私聊主动补回：{private_done} 个会话\n"
                    f"• 群聊翻群/记忆联想：{'已发送' if group_sent else '本次未发送'}\n"
                    f"• 剩余未读：群 {self.session_buffer.get_unread_count(is_private=False)} 条 / "
                    f"私聊 {self.session_buffer.get_unread_count(is_private=True)} 条"
                )
                return

            group_chats = self.session_buffer.list_unread_chats(private=False, limit=20)
            private_chats = self.session_buffer.list_unread_chats(private=True, limit=20)
            lines = [
                "📬 【OnlyYou 未读消息队列】"
                f"共 {self.session_buffer.get_unread_total()} 条"
            ]
            if group_chats:
                lines.append("👥 群聊：")
                for umo, batch in group_chats:
                    latest = batch[-1]
                    lines.append(
                        f"- {umo}: {len(batch)} 条 | 最近 "
                        f"{str(getattr(latest, 'sender_name', '') or '')}: "
                        f"{str(getattr(latest, 'content', '') or '')[:28]}"
                    )
            if private_chats:
                lines.append("💬 私聊：")
                for umo, batch in private_chats:
                    latest = batch[-1]
                    lines.append(
                        f"- {umo}: {len(batch)} 条 | 来自 "
                        f"{str(getattr(latest, 'sender_name', '') or '')} "
                        f"({str(getattr(latest, 'user_tier_label', '') or '')})"
                    )
            if not group_chats and not private_chats:
                lines.append("- （当前没有未读消息）")
            lines.append("提示：/onlyyou unread flush 在 IDLE 下立即补回；clear 清空。")
            yield event.plain_result("\n".join(lines))
            return

        if sub == "state":
            if len(tokens) < 2:
                yield event.plain_result("用法：/onlyyou state <RESTING|BUSY|IDLE> [事件标题]（护甲按行动类型自动取固定档位）")
                return
            state_name = tokens[1].upper()
            if state_name not in BotState.__members__:
                yield event.plain_result("状态必须是 RESTING、BUSY 或 IDLE 之一。")
                return
            new_state = BotState(state_name)
            new_title = tokens[2] if len(tokens) >= 3 else None
            # v2.6: armor is a fixed per-action tier now (ActionType.fixed_armor);
            # the legacy 4th token is intentionally ignored.
            slot = await self.deck_engine.force_set_current_state(
                soul_id=soul.soul_id,
                new_state=new_state,
                new_title=new_title,
            )
            yield event.plain_result(
                f"✅ 已将当前时段【{slot.label}】强制切换为：{slot.state.value} —— {slot.title} "
                f"(护甲档位: Lv.{slot.action.armor_tier}·{slot.armor})"
            )
            return

        if sub == "reroll":
            run = await self.deck_engine.get_or_build_today_run(
                soul_id=soul.soul_id,
                custom_cards=soul.state_event_cards,
                custom_encounters=soul.encounter_talk_cards,
                force_reroll=True,
                chronotype=soul.chronotype,
            )
            _, cur_slot, _ = await self.deck_engine.get_current_and_next_slot(
                soul_id=soul.soul_id,
                custom_cards=soul.state_event_cards,
                custom_encounters=soul.encounter_talk_cards,
                chronotype=soul.chronotype,
            )
            yield event.plain_result(
                f"🎲 已重新抽取今日 Roguelite 动态时间轴！\n"
                f"• 今日基调：【{run.vigor_title}】\n"
                f"• 新时间块数量：{len(run.slots)} 块\n"
                f"• 当前时间块：【{cur_slot.action.label}】{cur_slot.state.value} | "
                f"{cur_slot.title} (护甲:Lv.{cur_slot.action.armor_tier}·{cur_slot.armor})"
            )
            return

        if sub == "tier":
            if len(tokens) == 1:
                if user.tier == UserTier.L3_CORE:
                    yield event.plain_result(
                        f"💖 你的当前关系等级：{user.tier.label} (ID: {user.global_uid}, "
                        f"互动:{user.interaction_count}次) —— ★L3 管理员级（不需要好感度；"
                        f"可在关系页「⋯」菜单或 /onlyyou tier <uid> 3 调整）"
                    )
                else:
                    yield event.plain_result(
                        f"💖 你的当前关系等级：{user.tier.label} (ID: {user.global_uid}, "
                        f"互动:{user.interaction_count}次, 好感:{user.affinity:.1f})"
                    )
                return
            if len(tokens) >= 3 and tokens[2] in ("1", "2", "3"):
                target_uid = tokens[1]
                new_tier = UserTier(int(tokens[2]))
                updated = await self.user_relation.set_user_tier(target_uid, new_tier)
                extra = ""
                if new_tier == UserTier.L3_CORE:
                    extra = (
                        " —— ★L3 = 管理员级：不参与好感度、不会被自动晋升，"
                        "请在控制台关系页「⋯」菜单管理"
                    )
                yield event.plain_result(
                    f"✅ 已将用户 `{updated.global_uid}` 的关系等级设置为：{updated.tier.label}{extra}"
                )
                return
            yield event.plain_result(
                "用法：/onlyyou tier [user_id] [1|2|3]（1=L1普通 / 2=L2重要 / 3=★L3管理员级）"
            )
            return

        if sub == "memory":
            action = tokens[1].lower() if len(tokens) >= 2 else "list"
            if action == "gc":
                stats = await self.memory_manager.run_forgetting_gc()
                yield event.plain_result(
                    f"🗑️ 定时遗忘清理 (Forgetting GC) 执行完毕：\n"
                    f"- 清理过期 Tier1 瞬时记忆：{stats['deleted_tier1']} 条\n"
                    f"- 活力归零物理删除 Tier2 记忆：{stats['deleted_tier2']} 条\n"
                    f"- 衰减结算 Tier2 记忆：{stats['decayed_tier2']} 条\n"
                    f"- 访问强化晋升 Tier3 核心记忆：{stats['promoted_to_tier3']} 条"
                )
                return
            if action == "extract":
                transcript = self.session_buffer.get_window_transcript(umo=umo, max_turns=15)
                count = await self.memory_manager.extract_memories_from_transcript(
                    soul_id=soul.soul_id,
                    soul_name=soul.name,
                    transcript_text=transcript,
                    umo=umo,
                )
                yield event.plain_result(f"🧠 已从当前会话滑窗提炼并入库 {count} 条分级记忆。")
                return
            if action == "add" and len(tokens) >= 6:
                mtier = int(tokens[2]) if tokens[2] in ("1", "2", "3") else 2
                imp = int(tokens[3]) if tokens[3].isdigit() else 6
                kws = tokens[4]
                content = " ".join(tokens[5:])
                mid = await self.memory_manager.add_memory(
                    soul_id=soul.soul_id,
                    content=content,
                    keywords=kws,
                    importance=imp,
                    user_id=user.global_uid,
                    user_tier=int(user.tier.value),
                    memory_tier=mtier,
                )
                yield event.plain_result(f"✅ 已写入记忆 ID={mid} (Tier={mtier}, 重要度={imp})：{content[:50]}")
                return

            mems = await self.memory_manager.retrieve_candidates(
                soul_id=soul.soul_id, user_id=user.global_uid, limit=15
            )
            if not mems:
                yield event.plain_result("🧠 当前暂无存储的分级记忆。")
                return
            lines = [f"🧠 【{soul.name} 的分级记忆库 (Top {len(mems)})】"]
            for m in mems:
                t_label = {1: "T1瞬时", 2: "T2近期", 3: "T3核心"}[m.memory_tier]
                lines.append(
                    f"- #{m.id} [{t_label}|重要度:{m.importance}|活力:{m.vitality:.0f}|召回:{m.access_count}] "
                    f"{m.content} (关键词:{m.keywords})"
                )
            yield event.plain_result("\n".join(lines))
            return

        if sub == "surf":
            if not self._surfing_enabled(soul):
                yield event.plain_result(
                    "📴 赛博冲浪总开关已关闭：当前角色不再主动刷视频，"
                    "SURF 时段会自动降级为空闲闲逛。请先在 Pages「冲浪」页开启总开关。"
                )
                return
            cur_slot = await self.deck_engine.get_current_slot(
                soul_id=soul.soul_id,
                custom_cards=soul.state_event_cards,
                custom_encounters=soul.encounter_talk_cards,
                chronotype=soul.chronotype,
            )
            if not StateController.can_surf_video(cur_slot.state):
                yield event.plain_result(
                    f"⛔ 当前处于【{cur_slot.state.value} ({cur_slot.title})】，冲浪管线已挂起（仅在 IDLE 空闲状态下刷视频）。"
                )
                return
            review = await self.surfing_pipeline.surf_once(
                soul=soul, current_state=cur_slot.state, umo=umo
            )
            if not review:
                yield event.plain_result("📱 本次冲浪未获取到新视频。")
                return
            yield event.plain_result(
                f"📱 【赛博冲浪完成】\n"
                f"• 视频：《{review.video.title}》(UP: {review.video.author})\n"
                f"• 链接：{review.video.share_url}\n"
                f"• 多模态评分：{review.score}/100\n"
                f"• {soul.name}的观后吐槽：{review.commentary}"
            )
            return

        yield event.plain_result(self._onlyyou_help_text())
