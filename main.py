"""Main entry point for astrbot_plugin_only_you (OnlyYou / 只有你)."""

import asyncio
import os
from typing import Any, Optional

try:
    from astrbot.api import AstrBotConfig, logger
    from astrbot.api.event import AstrMessageEvent, filter
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

    class _FilterStub:
        class EventMessageType:
            ALL = "ALL"

        @staticmethod
        def event_message_type(*args, **kwargs):
            def _dec(fn):
                return fn
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
        def command(*args, **kwargs):
            def _dec(fn):
                return fn
            return _dec

    filter = _FilterStub()

from .core.atmosphere_reader import AtmosphereReader
from .core.context_budget import ContextBudgetManager, SessionBuffer
from .core.memory_manager import MemoryManager, MemoryTier
from .core.model_hub import ModelHub
from .core.platform_bridge import PlatformBridge
from .core.roguelite_deck import RogueliteDeckEngine
from .core.schedule_mutator import BoundedScheduleMutator
from .core.soul_manager import SoulManager
from .core.state_machine import BotState, StateController, UserTier
from .core.user_relation import UserRelationManager
from .core.web_api import OnlyYouWebAPI
from .prompts.templates import PROACTIVE_CHAT_PROMPT
from .storage.db import DatabaseManager
from .surfing.pipeline import CyberSurfingPipeline


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
    - Roguelite 每日随机日程引擎与奇遇谈资池
    - 输入驱动的受限日程状态微调器 (三重硬性门禁)
    - 三级用户关系体系 (L1普通 / ◆L2重要熟人 / ★L3核心用户)
    - 精简版三级记忆 + 定时自动遗忘清理 (Forgetting GC) + 上下文预算管理器
    """

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
        self.model_hub = ModelHub(context=self.context, plugin_config=self.config)
        self.platform_bridge = PlatformBridge(context=self.context)
        self.soul_manager = SoulManager(
            context=self.context, db=self.db, model_hub=self.model_hub
        )
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

    async def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        async with self._init_lock:
            if not self._initialized:
                await self.db.initialize()
                self._initialized = True

    async def initialize(self) -> None:
        """AstrBot Star lifecycle initialization hook."""
        await self._ensure_initialized()
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
        await self.db.close()
        logger.info("[OnlyYou] Plugin terminated cleanly.")

    # ==========================================================================
    # Background Loops: Action 2 (Cyber Surfing & Proactive Chat) + Forgetting GC
    # ==========================================================================
    async def _heartbeat_loop(self) -> None:
        """Every 15 minutes, check if current state is IDLE to surf videos or proactively chat."""
        while True:
            try:
                await asyncio.sleep(900)
                await self._ensure_initialized()
                soul = await self.soul_manager.resolve_soul(allow_llm_seed=False)
                run, current_slot, _ = await self.deck_engine.get_current_and_next_slot(
                    soul_id=soul.soul_id,
                    custom_cards=soul.state_event_cards,
                    custom_encounters=soul.encounter_talk_cards,
                )

                # Strictly respect 3-state gate: never surf or initiate chat in RESTING or BUSY
                if not StateController.can_surf_video(current_slot.state):
                    continue

                # Branch A: Surf video in IDLE state
                review = await self.surfing_pipeline.surf_once(
                    soul=soul, current_state=current_slot.state
                )

                # Branch B: Proactive chat with known L3 Core User UMO if unused encounter or video exists
                if self._known_l3_umos and StateController.can_initiate_proactive_chat(
                    current_slot.state
                ):
                    unused_enc = next((e for e in run.encounters if not e.used), None)
                    if unused_enc or review:
                        target_umo = next(iter(self._known_l3_umos.values()))
                        enc_text = unused_enc.content if unused_enc else "无"
                        vid_text = (
                            f"《{review.video.title}》({review.video.share_url}) - {review.commentary}"
                            if review
                            else "无"
                        )
                        prompt = PROACTIVE_CHAT_PROMPT.format(
                            soul_name=soul.name,
                            soul_archetype=soul.archetype,
                            speaking_style=soul.speaking_style,
                            current_slot_title=current_slot.title,
                            encounter_text=enc_text,
                            video_text=vid_text,
                        )
                        msg = await self.model_hub.call_llm_text(
                            prompt=prompt, role="small", umo=target_umo
                        )
                        if msg:
                            sent = await self.platform_bridge.send_proactive_message(
                                umo=target_umo,
                                text=msg,
                                session_buffer=self.session_buffer,
                                soul_name=soul.name,
                            )
                            if sent and unused_enc:
                                unused_enc.used = True
                                await self.deck_engine.save_run(run)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"[OnlyYou] Heartbeat loop error: {e}")

    async def _daily_gc_loop(self) -> None:
        """Run Forgetting GC every 24 hours."""
        while True:
            try:
                await asyncio.sleep(86400)
                await self._ensure_initialized()
                await self.memory_manager.run_forgetting_gc()
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
            return

        umo = getattr(event, "unified_msg_origin", "") or "default_umo"
        soul = await self.soul_manager.resolve_soul(umo=umo)
        user = await self.user_relation.resolve_user(event)

        if user.tier == UserTier.L3_CORE and self.atmosphere_reader.is_private_event(event):
            self._known_l3_umos[user.global_uid] = umo

        # 1. Record into sliding window & extract reply topology
        _, is_direct_mention = self.atmosphere_reader.record_message(
            event=event,
            user=user,
            on_overflow_callback=self._on_window_overflow,
        )

        is_private = self.atmosphere_reader.is_private_event(event)
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
    # 3. Response Recording & Encounter Consumption Hook (`on_llm_response`)
    # ==========================================================================
    @filter.on_llm_response()
    async def on_llm_response_hook(self, event: AstrMessageEvent, resp: LLMResponse):
        """Record bot's reply into `SessionBuffer` and mark used `EncounterCard`s."""
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

        # Check if any unused encounter card was naturally talked about in this reply,
        # and consume one-shot mutation_monologue after the first post-mutation reply.
        run, current_slot, _ = await self.deck_engine.get_current_and_next_slot(
            soul_id=soul.soul_id,
            custom_cards=soul.state_event_cards,
            custom_encounters=soul.encounter_talk_cards,
        )
        changed = False
        if current_slot.mutation_monologue:
            current_slot.mutation_monologue = ""
            changed = True
        if current_slot.state == BotState.IDLE:
            for enc in run.encounters:
                if not enc.used and len(enc.content) >= 4:
                    # Match 3+ char key phrase overlap
                    for i in range(0, len(enc.content) - 2, 2):
                        sub = enc.content[i : i + 3]
                        if sub in reply_text:
                            enc.used = True
                            changed = True
                            break
        if changed:
            await self.deck_engine.save_run(run)

    # ==========================================================================
    # 4. Diagnostic & Management Command (`/onlyyou`)
    # ==========================================================================
    @filter.command("onlyyou", alias={"oy"})
    async def cmd_onlyyou(self, event: AstrMessageEvent):
        """
        Unified `/onlyyou` (`/oy`) command for inspecting and controlling OnlyYou systems:
        - `/onlyyou status`
        - `/onlyyou state <RESTING|BUSY|IDLE> [title] [armor]`
        - `/onlyyou reroll`
        - `/onlyyou memory [list|gc|add|extract]`
        - `/onlyyou tier [user_id] [1|2|3]`
        - `/onlyyou surf`
        """
        await self._ensure_initialized()
        umo = getattr(event, "unified_msg_origin", "") or "default_umo"
        raw_msg = (getattr(event, "message_str", "") or "").strip()
        tokens = raw_msg.split()
        # Strip command token ("/onlyyou", "onlyyou", "/oy", "oy")
        if tokens and tokens[0].lstrip("/").lower() in ("onlyyou", "oy"):
            tokens = tokens[1:]

        sub = tokens[0].lower() if tokens else "status"
        soul = await self.soul_manager.resolve_soul(umo=umo)
        user = await self.user_relation.resolve_user(event)

        if sub == "status":
            run, cur_slot, next_slot = await self.deck_engine.get_current_and_next_slot(
                soul_id=soul.soul_id,
                custom_cards=soul.state_event_cards,
                custom_encounters=soul.encounter_talk_cards,
            )
            slot_lines = []
            for s in run.slots:
                marker = "👉 " if s.slot_key == cur_slot.slot_key else "   "
                mut_tag = " [⚡已微调]" if s.mutated else ""
                delay_tag = f" [顺延+{s.delay_minutes}m]" if s.delay_minutes else ""
                slot_lines.append(
                    f"{marker}[{s.start_hour:02d}:00-{s.end_hour:02d}:00 {s.label}] "
                    f"{s.state.value} | {s.title} (护甲:{s.armor}){mut_tag}{delay_tag}"
                )
            enc_lines = [
                f"- [{'已聊' if e.used else '未聊'}] {e.content}" for e in run.encounters
            ] or ["- （无）"]
            text = (
                f"🌟 【OnlyYou 状态面板】\n"
                f"• 当前角色卡：{soul.name} ({soul.soul_id}) —— {soul.archetype}\n"
                f"• 你的关系等级：{user.tier.label} (ID: {user.global_uid}, 互动:{user.interaction_count}次)\n"
                f"• 今日基调卡：【{run.vigor_title}】{run.vigor_desc}\n"
                f"• 当前时段状态：【{cur_slot.state.value}】{cur_slot.title} (定力护甲: {cur_slot.armor})\n"
                f"• 今日微调配额：{run.mutations_used}/{self.schedule_mutator.max_mutations_per_day} 次\n"
                f"\n📅 【今日 6 时段日程轴 ({run.date_str})】\n"
                + "\n".join(slot_lines)
                + "\n\n🃏 【今日奇遇谈资卡】\n"
                + "\n".join(enc_lines)
            )
            yield event.plain_result(text)
            return

        if sub == "state":
            if len(tokens) < 2:
                yield event.plain_result("用法：/onlyyou state <RESTING|BUSY|IDLE> [事件标题] [护甲值]")
                return
            state_name = tokens[1].upper()
            if state_name not in BotState.__members__:
                yield event.plain_result("状态必须是 RESTING、BUSY 或 IDLE 之一。")
                return
            new_state = BotState(state_name)
            new_title = tokens[2] if len(tokens) >= 3 else None
            new_armor = int(tokens[3]) if len(tokens) >= 4 and tokens[3].isdigit() else None
            slot = await self.deck_engine.force_set_current_state(
                soul_id=soul.soul_id,
                new_state=new_state,
                new_title=new_title,
                armor=new_armor,
            )
            yield event.plain_result(
                f"✅ 已将当前时段【{slot.label}】强制切换为：{slot.state.value} —— {slot.title} (护甲: {slot.armor})"
            )
            return

        if sub == "reroll":
            run = await self.deck_engine.get_or_build_today_run(
                soul_id=soul.soul_id,
                custom_cards=soul.state_event_cards,
                custom_encounters=soul.encounter_talk_cards,
                force_reroll=True,
            )
            _, cur_slot, _ = await self.deck_engine.get_current_and_next_slot(soul_id=soul.soul_id)
            yield event.plain_result(
                f"🎲 已重新抽取今日 Roguelite 日程！\n"
                f"• 今日基调：【{run.vigor_title}】\n"
                f"• 当前时段：【{cur_slot.state.value}】{cur_slot.title} (护甲:{cur_slot.armor})\n"
                f"• 奇遇谈资：{' / '.join(e.content for e in run.encounters)}"
            )
            return

        if sub == "tier":
            if len(tokens) == 1:
                yield event.plain_result(
                    f"💖 你的当前关系等级：{user.tier.label} (ID: {user.global_uid}, 互动:{user.interaction_count}次, 好感:{user.affinity:.1f})"
                )
                return
            if len(tokens) >= 3 and tokens[2] in ("1", "2", "3"):
                target_uid = tokens[1]
                new_tier = UserTier(int(tokens[2]))
                updated = await self.user_relation.set_user_tier(target_uid, new_tier)
                yield event.plain_result(
                    f"✅ 已将用户 `{updated.global_uid}` 的关系等级设置为：{updated.tier.label}"
                )
                return
            yield event.plain_result("用法：/onlyyou tier [user_id] [1|2|3]")
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
            cur_slot = await self.deck_engine.get_current_slot(
                soul_id=soul.soul_id,
                custom_cards=soul.state_event_cards,
                custom_encounters=soul.encounter_talk_cards,
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

        yield event.plain_result(
            "OnlyYou 指令列表：\n"
            "• /onlyyou status - 查看今日 Roguelite 日程与三态面板\n"
            "• /onlyyou state <RESTING|BUSY|IDLE> [标题] [护甲] - 切换当前状态\n"
            "• /onlyyou reroll - 重新抽取今日日程与奇遇卡\n"
            "• /onlyyou tier [user_id] [1|2|3] - 查看或修改用户关系等级\n"
            "• /onlyyou memory [list|gc|extract|add] - 管理三级记忆与遗忘GC\n"
            "• /onlyyou surf - 在 IDLE 状态下触发一次刷视频"
        )
