"""Backend Web API routes (`context.register_web_api`) for OnlyYou Plugin Pages (`views/console`)."""

import random
import time
from typing import TYPE_CHECKING, Any, Optional

from .memory_manager import MemoryTier
from .roguelite_deck import (
    DEFAULT_ENCOUNTER_POOL,
    DEFAULT_STATE_EVENT_CARDS,
    SLOT_DEFINITIONS,
    StateEventCard,
)
from .soul_manager import SoulProfile
from .state_machine import BotState, UserTier

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


class OnlyYouWebAPI:
    """
    🖥️ Registers and handles all 17 RESTful Web API routes defined in `docs/WEBUI_DESIGN.md`
    Section 3.3 for the `views/console` AstrBot Plugin Page.
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
        """Register all 17 WebUI Console routes with `self.plugin.context.register_web_api`."""
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
                "OnlyYou: 重新抽取今日 Roguelite 三态日程与奇遇谈资卡",
            ),
            (
                f"{API_PREFIX}/schedule/force_state",
                self.api_schedule_force_state,
                ["POST"],
                "OnlyYou: 手动强制切换当前或指定槽位的三态状态与护甲",
            ),
            (
                f"{API_PREFIX}/cards/list",
                self.api_cards_list,
                ["GET"],
                "OnlyYou: 获取指定 Soul 可用的三态行动卡片池与奇遇谈资卡池",
            ),
            (
                f"{API_PREFIX}/cards/save",
                self.api_cards_save,
                ["POST"],
                "OnlyYou: 新增或保存单张三态行动卡片或奇遇谈资卡",
            ),
            (
                f"{API_PREFIX}/cards/delete",
                self.api_cards_delete,
                ["POST"],
                "OnlyYou: 删除指定三态行动卡片或奇遇谈资卡",
            ),
            (
                f"{API_PREFIX}/cards/ai_generate",
                self.api_cards_ai_generate,
                ["POST"],
                "OnlyYou: 调用小脑模型根据角色设定生成新的三态行动卡与奇遇卡",
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
        ]

        for route_path, handler, methods, desc in routes:
            register_fn(route_path, handler, methods, desc)
        logger.info(f"[OnlyYou] Registered {len(routes)} Web API routes for Plugin Pages.")

    async def _build_schedule_payload(self, soul: SoulProfile) -> dict[str, Any]:
        run, cur_slot, next_slot = await self.plugin.deck_engine.get_current_and_next_slot(
            soul_id=soul.soul_id,
            custom_cards=soul.state_event_cards,
            custom_encounters=soul.encounter_talk_cards,
        )
        return {
            "run": run.to_dict(),
            "current_slot": cur_slot.to_dict(),
            "next_slot": next_slot.to_dict(),
            "max_mutations_per_day": self.plugin.schedule_mutator.max_mutations_per_day,
            "mutation_cooldown_minutes": int(
                self.plugin.schedule_mutator.mutation_cooldown_seconds // 60
            ),
        }

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
    # 1. Init Summary & Soul Studio (`init_summary`, `souls/*`)
    # ==========================================================================
    async def api_init_summary(self) -> dict[str, Any]:
        try:
            await self.plugin._ensure_initialized()
            souls = await self.plugin.soul_manager.list_souls()
            native_personas = await self.plugin.soul_manager.list_native_personas()
            bindings = await self.plugin.soul_manager.get_umo_bindings()
            active_soul = souls[0]
            sched_payload = await self._build_schedule_payload(active_soul)

            m_cfg = self.plugin.config.get("model_hub_config", {}) or {}
            model_status = {
                "main_llm": m_cfg.get("main_llm_provider_id") or "继承平台当前会话主模型",
                "small_llm": m_cfg.get("small_llm_provider_id") or "自动回退至主模型",
                "vision_llm": m_cfg.get("vision_llm_provider_id") or "继承平台默认识图/主模型",
                "embedding": m_cfg.get("embedding_provider_id") or "SQLite 关键词+时间新鲜度混合检索",
                "rerank": m_cfg.get("rerank_provider_id") or "内置权重精排",
            }

            return ok_response(
                {
                    "souls": [s.to_dict() for s in souls],
                    "native_personas": native_personas,
                    "umo_bindings": bindings,
                    "known_umos": self._collect_known_umos(bindings),
                    "active_soul_id": active_soul.soul_id,
                    "active_schedule": sched_payload,
                    "model_status": model_status,
                    "slot_definitions": SLOT_DEFINITIONS,
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
                    "schedule": sched_payload,
                    "umo_bindings": bindings,
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
            override_prompt = str(body.get("native_prompt") or "").strip() or None

            profile = await self.plugin.soul_manager.import_native_persona(
                persona_name=persona_name,
                target_soul_id=target_soul_id,
                auto_generate_cards=auto_gen,
                override_prompt=override_prompt,
            )
            # Build fresh schedule run with the newly imported deck
            await self.plugin.deck_engine.get_or_build_today_run(
                soul_id=profile.soul_id,
                custom_cards=profile.state_event_cards,
                custom_encounters=profile.encounter_talk_cards,
                force_reroll=True,
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

            existing = await self.plugin.soul_manager.get_soul_by_id(soul_id)
            raw_prefs = body.get("surfing_preferences")
            if isinstance(raw_prefs, str):
                prefs = [x.strip() for x in raw_prefs.replace("，", ",").split(",") if x.strip()]
            elif isinstance(raw_prefs, list):
                prefs = [str(x).strip() for x in raw_prefs if str(x).strip()]
            else:
                prefs = existing.surfing_preferences if existing else ["搞笑整活", "萌宠日常", "美食探店"]

            profile = SoulProfile(
                soul_id=soul_id,
                name=str(body.get("name") or (existing.name if existing else soul_id)).strip(),
                archetype=str(
                    body.get("archetype")
                    or (existing.archetype if existing else "有自己生活作息的真实网友")
                ).strip(),
                speaking_style=str(
                    body.get("speaking_style")
                    or (existing.speaking_style if existing else "口语化短句，自然随性")
                ).strip(),
                native_persona_prompt=str(
                    body.get("native_persona_prompt")
                    if "native_persona_prompt" in body
                    else (existing.native_persona_prompt if existing else "")
                ).strip(),
                surfing_preferences=prefs,
                state_event_cards=existing.state_event_cards if existing else [],
                encounter_talk_cards=existing.encounter_talk_cards if existing else [],
            )
            await self.plugin.soul_manager.save_soul(profile)

            # Optional UMO routing binding updates
            if isinstance(body.get("umo_binding"), dict):
                ub = body["umo_binding"]
                await self.plugin.soul_manager.set_umo_binding(
                    str(ub.get("umo") or ""), str(ub.get("soul_id") or "")
                )

            souls = await self.plugin.soul_manager.list_souls()
            bindings = await self.plugin.soul_manager.get_umo_bindings()
            return ok_response(
                {
                    "soul": profile.to_dict(),
                    "souls": [s.to_dict() for s in souls],
                    "umo_bindings": bindings,
                    "known_umos": self._collect_known_umos(bindings),
                },
                message=f"已保存角色 {profile.name} ({profile.soul_id})",
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
                    "active_soul_id": souls[0].soul_id,
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
                custom_encounters=soul.encounter_talk_cards,
                force_reroll=True,
            )
            sched_payload = await self._build_schedule_payload(soul)
            return ok_response(
                {"schedule": sched_payload},
                message="已重新抽取今日 Roguelite 日程与奇遇谈资卡",
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

            raw_state = str(body.get("new_state") or "IDLE").strip().upper()
            if raw_state not in BotState.__members__:
                return err_response("状态必须是 RESTING、BUSY 或 IDLE 之一")

            new_state = BotState(raw_state)
            new_title = str(body.get("new_title") or "").strip() or None
            armor_raw = body.get("armor")
            new_armor = int(armor_raw) if armor_raw is not None and str(armor_raw) != "" else None
            slot_key = str(body.get("slot_key") or "").strip() or None

            updated_slot = await self.plugin.deck_engine.force_set_current_state(
                soul_id=soul.soul_id,
                new_state=new_state,
                new_title=new_title,
                armor=new_armor,
                slot_key=slot_key,
            )
            sched_payload = await self._build_schedule_payload(soul)
            return ok_response(
                {
                    "updated_slot": updated_slot.to_dict(),
                    "schedule": sched_payload,
                },
                message=f"已将时段【{updated_slot.label}】切换为 {updated_slot.state.value} ({updated_slot.title})",
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

            custom_cards = [{**c.to_dict(), "scope": "soul"} for c in soul.state_event_cards]
            general_cards = [
                {**c.to_dict(), "scope": "global"} for c in DEFAULT_STATE_EVENT_CARDS
            ]
            custom_encounters = [
                {"content": txt, "scope": "soul"} for txt in soul.encounter_talk_cards
            ]
            general_encounters = [
                {"content": txt, "scope": "global"} for txt in DEFAULT_ENCOUNTER_POOL
            ]
            return ok_response(
                {
                    "soul_id": soul.soul_id,
                    "custom_cards": custom_cards,
                    "general_cards": general_cards,
                    "custom_encounters": custom_encounters,
                    "general_encounters": general_encounters,
                    "slot_definitions": SLOT_DEFINITIONS,
                }
            )
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
                content = str(body.get("content") or "").strip()
                old_content = str(body.get("old_content") or "").strip()
                if not content:
                    return err_response("奇遇谈资卡内容不能为空")
                target_list = (
                    DEFAULT_ENCOUNTER_POOL
                    if scope == "global"
                    else soul.encounter_talk_cards
                )
                if old_content and old_content in target_list:
                    idx = target_list.index(old_content)
                    target_list[idx] = content
                elif content not in target_list:
                    target_list.insert(0, content)
                await self.plugin.soul_manager.save_soul(soul)
            else:
                card_data = body.get("card") if isinstance(body.get("card"), dict) else body
                card = StateEventCard.from_dict(card_data)
                if not card.card_id:
                    card.card_id = f"evt_{int(time.time())}_{random.randint(100, 999)}"
                card.armor = max(0, min(100, int(card.armor)))
                card.base_weight = max(1, min(100, int(card.base_weight)))

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
                content = str(body.get("content") or "").strip()
                target_list = (
                    DEFAULT_ENCOUNTER_POOL
                    if scope == "global"
                    else soul.encounter_talk_cards
                )
                if content in target_list:
                    target_list.remove(content)
                await self.plugin.soul_manager.save_soul(soul)
            else:
                card_id = str(body.get("card_id") or "").strip()
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
                f"请为角色【{soul.name}】（人设定位：{soul.archetype}，口癖：{soul.speaking_style}，"
                f"冲浪喜好：{', '.join(soul.surfing_preferences)}）生成 5 张全新的三态行动卡片（覆盖 RESTING/BUSY/IDLE）"
                f"和 3 张今日奇遇谈资卡。\n"
                '仅输出合法 JSON：{"state_event_cards": [{"card_id": "evt_ai_1", "title": "...", "state": "IDLE|BUSY|RESTING", '
                '"valid_slots": ["morning","forenoon","noon","afternoon","evening","night"], "armor": 45, "base_weight": 60}], '
                '"encounter_talk_cards": ["奇遇1", "奇遇2", "奇遇3"]}'
            )
            data = await self.plugin.model_hub.call_llm_json(
                prompt=prompt, role="small", default=None
            )
            gen_cards: list[StateEventCard] = []
            gen_encounters: list[str] = []

            if isinstance(data, dict) and data.get("state_event_cards"):
                for idx, raw_c in enumerate(data.get("state_event_cards") or []):
                    if isinstance(raw_c, dict):
                        c = StateEventCard.from_dict(raw_c)
                        c.card_id = f"evt_ai_{int(time.time())}_{idx + 1}"
                        gen_cards.append(c)
                gen_encounters = [
                    str(x).strip()
                    for x in (data.get("encounter_talk_cards") or [])
                    if str(x).strip()
                ]

            if not gen_cards:
                fallback_cards, fallback_enc = self.plugin.soul_manager.build_fallback_custom_deck(
                    soul.name, soul.native_persona_prompt
                )
                gen_cards = fallback_cards[:5]
                gen_encounters = fallback_enc[:3]

            if auto_save:
                existing_titles = {c.title for c in soul.state_event_cards}
                for c in gen_cards:
                    if c.title not in existing_titles:
                        soul.state_event_cards.insert(0, c)
                for e in gen_encounters:
                    if e not in soul.encounter_talk_cards:
                        soul.encounter_talk_cards.insert(0, e)
                await self.plugin.soul_manager.save_soul(soul)

            payload = await self.api_cards_list_for_soul(soul)
            payload["data"]["generated_cards"] = [c.to_dict() for c in gen_cards]
            payload["data"]["generated_encounters"] = gen_encounters
            payload["message"] = f"已为 {soul.name} 生成 {len(gen_cards)} 张行动卡与 {len(gen_encounters)} 张奇遇卡"
            return payload
        except Exception as e:
            return err_response(f"AI 生成卡片失败: {e}")

    async def api_cards_list_for_soul(self, soul: SoulProfile) -> dict[str, Any]:
        return ok_response(
            {
                "soul_id": soul.soul_id,
                "custom_cards": [{**c.to_dict(), "scope": "soul"} for c in soul.state_event_cards],
                "general_cards": [
                    {**c.to_dict(), "scope": "global"} for c in DEFAULT_STATE_EVENT_CARDS
                ],
                "custom_encounters": [
                    {"content": txt, "scope": "soul"} for txt in soul.encounter_talk_cards
                ],
                "general_encounters": [
                    {"content": txt, "scope": "global"} for txt in DEFAULT_ENCOUNTER_POOL
                ],
                "slot_definitions": SLOT_DEFINITIONS,
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
            counts = {
                "l3_count": sum(1 for u in all_users if u.tier == UserTier.L3_CORE),
                "l2_count": sum(1 for u in all_users if u.tier == UserTier.L2_IMPORTANT),
                "l1_count": sum(1 for u in all_users if u.tier == UserTier.L1_NORMAL),
                "total": len(all_users),
            }
            return ok_response(
                {
                    "users": [u.to_dict() for u in users],
                    "counts": counts,
                }
            )
        except Exception as e:
            return err_response(f"获取用户关系列表失败: {e}")

    async def api_users_update(self) -> dict[str, Any]:
        try:
            await self.plugin._ensure_initialized()
            body = await self._get_json_body()
            action = str(body.get("action") or "update").strip().lower()

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
            if isinstance(pids_val, str):
                pids_list = [x.strip() for x in pids_val.split(",") if x.strip()]
            elif isinstance(pids_val, list):
                pids_list = [str(x).strip() for x in pids_val if str(x).strip()]
            else:
                pids_list = None

            updated = await self.plugin.user_relation.update_user_details(
                global_uid=uid,
                display_name=body.get("display_name"),
                nickname=body.get("nickname"),
                tier=int(tier_val) if tier_val in (1, 2, 3, "1", "2", "3") else None,
                affinity=float(aff_val) if aff_val is not None and str(aff_val) != "" else None,
                platform_ids=pids_list,
            )
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
                soul_id = self._get_query_param("soul_id") or None
                items = await self.plugin.surfing_pipeline.list_history(soul_id=soul_id, limit=30)
                bindings = await self.plugin.soul_manager.get_umo_bindings()
                return ok_response(
                    {
                        "videos": items,
                        "known_umos": self._collect_known_umos(bindings),
                    }
                )

            body = await self._get_json_body()
            action = str(body.get("action") or "surf").strip().lower()
            soul_id = str(body.get("soul_id") or "default_soul").strip()
            soul = await self.plugin.soul_manager.get_soul_by_id(soul_id)
            if soul is None:
                soul = (await self.plugin.soul_manager.list_souls())[0]

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
                custom_encounters=soul.encounter_talk_cards,
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
