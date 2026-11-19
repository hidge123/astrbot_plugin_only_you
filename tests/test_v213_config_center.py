"""v2.13 配置中心回归：``PluginConfigManager`` 热更新 / 迁移 / settings API。

覆盖实现计划 §一 的核心契约：

1. 旧 AstrBotConfig 中的非默认值在首次启动时一次性迁入
   ``plugin_settings["runtime_config"]``，之后不再覆盖 Pages 保存的新值；
2. Pages 保存后**原地**更新共享配置字典（所有子模块立即读到新值），并触发
   ``reload_config`` / 心跳唤醒钩子；
3. 后端 Schema 做类型归一化与范围钳制（Clamp），未知键被忽略；
4. ``settings/get|save|reset`` 三条 Web API 返回标准信封；
5. 原硬编码参数（心跳周期 / GC 周期 / 记忆字数 / RAG 权重 / 好感增益 /
   冲击力表 / 冲浪看片分 / 运行时队列容量）都从配置中心读取。
"""

import os
import sys
import unittest
from types import MethodType

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _helpers import (  # noqa: E402
    DatabaseAsyncTestCase,
    build_fake_plugin,
    load_plugin_modules,
    make_plugin_config,
    make_slot,
    make_soul,
)

mods = load_plugin_modules()
config_manager = mods.config_manager


class ConfigManagerTestBase(DatabaseAsyncTestCase):
    async def build(self, config=None):
        return await build_fake_plugin(self.db, self.tmpdir, config=config)


class TestMigrationAndHotUpdate(ConfigManagerTestBase):
    async def test_legacy_values_are_migrated_once(self):
        config = make_plugin_config()
        config["roguelite_schedule_config"].update(
            {"heartbeat_interval_minutes": 25, "idle_roam_cooldown_minutes": 30}
        )
        config["memory_and_context_config"]["memory_content_max_chars"] = 120
        plugin, _api = await self.build(config=config)

        assert await self.db.get_setting(config_manager.LEGACY_MIGRATED_KEY) is True
        assert plugin.config["roguelite_schedule_config"]["heartbeat_interval_minutes"] == 25
        assert plugin.config["memory_and_context_config"]["memory_content_max_chars"] == 120
        stored = await self.db.get_setting(config_manager.RUNTIME_CONFIG_KEY)
        assert stored["groups"]["roguelite_schedule_config"]["heartbeat_interval_minutes"] == 25

        # Pages 改值后再次启动：旧 AstrBotConfig 不得覆盖已保存的新值。
        await plugin.config_manager.save(
            {"section": "schedule", "values": {"heartbeat_interval_minutes": 45}}
        )
        restart_cfg = make_plugin_config()
        restart_cfg["roguelite_schedule_config"]["heartbeat_interval_minutes"] = 10
        plugin2, _ = await build_fake_plugin(self.db, self.tmpdir, config=restart_cfg)
        assert plugin2.config_manager.get("heartbeat_interval_minutes") == 45
        assert plugin2.config["roguelite_schedule_config"]["heartbeat_interval_minutes"] == 45

    async def test_save_updates_shared_dict_in_place_and_notifies_hooks(self):
        plugin, _api = await self.build()
        memory_group = plugin.config["memory_and_context_config"]
        calls = []

        async def hook(changed):
            calls.append(list(changed))

        plugin.config_manager.bind_change_hook(hook)
        result = await plugin.config_manager.save(
            {
                "section": "memory",
                "values": {
                    "memory_content_max_chars": 180,
                    "sliding_window_size": 999,
                    "recall_semantic_weight": 0.9,
                    "unknown_key": 1,
                },
            }
        )
        # 同一 dict / 同一分组对象：子模块无需重建即可看到新值
        assert plugin.config["memory_and_context_config"] is memory_group
        assert memory_group["memory_content_max_chars"] == 180
        assert memory_group["sliding_window_size"] == 60  # clamped to max
        assert "unknown_key" not in memory_group
        assert memory_group["recall_semantic_weight"] == 0.9
        assert sorted(result["changed"]) == [
            "memory_content_max_chars",
            "recall_semantic_weight",
            "sliding_window_size",
        ]
        assert calls and calls[0] == result["changed"]

        # 各模块属性即时读到新值（无需 reload）
        assert plugin.memory_manager.memory_content_max_chars == 180
        assert plugin.session_buffer.sliding_window_size == 60
        assert plugin.context_budget.recall_semantic_weight == 0.9
        await plugin.config_manager.reset({"section": "memory"})
        assert plugin.memory_manager.memory_content_max_chars == 60
        assert plugin.session_buffer.sliding_window_size == 15

    async def test_type_normalisation_and_clamping(self):
        plugin, _api = await self.build()
        manager = plugin.config_manager
        await manager.save(
            {
                "schedule": {
                    "heartbeat_interval_minutes": "0",
                    "urgent_keywords": "急, 救命，大瓜",
                    "mutation_impact_table": {"sleep_wake_urgent": 500, "busy_l1": -3},
                },
                "atmosphere": {"debounce_seconds": 999, "takeover_group_chat": "off"},
                "surfing": {"send_raw_video": 0, "douyin_api_base": "  https://x.test  "},
            }
        )
        assert manager.get("heartbeat_interval_minutes") == 5
        assert manager.get("urgent_keywords") == ["急", "救命", "大瓜"]
        assert manager.get("mutation_impact_table")["sleep_wake_urgent"] == 100
        assert manager.get("mutation_impact_table")["busy_l1"] == 0
        assert manager.get("debounce_seconds") == 30.0
        assert manager.get("takeover_group_chat") is False
        assert manager.get("send_raw_video") is False
        assert manager.get("douyin_api_base") == "https://x.test"

    async def test_reset_all_restores_defaults(self):
        plugin, _api = await self.build()
        await plugin.config_manager.save(
            {"relationship": {"affinity_gain_group": 2.0}, "surfing": {"surf_share_min_score": 60}}
        )
        await plugin.config_manager.reset({})
        assert plugin.config_manager.get("affinity_gain_group") == 0.4
        assert plugin.config_manager.get("surf_share_min_score") == 80


class TestSettingsApi(ConfigManagerTestBase):
    async def call(self, method, handler, body=None):
        self.api.set_mock_request(method, query={}, body=body or {})
        return await handler()

    async def test_settings_get_save_reset_envelope(self):
        plugin, self.api = await self.build()
        data = self.assert_ok(await self.call("GET", self.api.api_settings_get))
        assert len(data["sections"]) == 6
        schedule = next(item for item in data["sections"] if item["id"] == "schedule")
        assert any(field["key"] == "heartbeat_interval_minutes" for field in schedule["fields"])
        assert all("advanced" in field for field in schedule["fields"])

        saved = self.assert_ok(
            await self.call(
                "POST",
                self.api.api_settings_save,
                {"section": "schedule", "values": {"heartbeat_interval_minutes": 20}},
            )
        )
        assert "heartbeat_interval_minutes" in saved["changed"]
        assert plugin.config["roguelite_schedule_config"]["heartbeat_interval_minutes"] == 20

        reset = self.assert_ok(
            await self.call("POST", self.api.api_settings_reset, {"section": "schedule"})
        )
        assert "heartbeat_interval_minutes" in reset["changed"]
        assert plugin.config_manager.get("heartbeat_interval_minutes") == 15

    async def test_init_summary_exposes_settings_summary(self):
        plugin, self.api = await self.build()
        data = self.assert_ok(await self.call("GET", self.api.api_init_summary))
        summary = data.get("settings")
        assert summary and summary["version"] == 1
        assert [item["id"] for item in summary["sections"]] == [
            "schedule",
            "proactive",
            "atmosphere",
            "memory",
            "relationship",
            "surfing",
        ]

    def assert_ok(self, response):
        assert response["status"] == "ok", response
        assert isinstance(response["message"], str)
        assert isinstance(response["data"], dict)
        return response["data"]


class TestBusySettlementPreservedOnTitleEdit(ConfigManagerTestBase):
    """#6：只改标题不得清空已结算繁忙成果；行动类型变化才清空。"""

    async def test_title_only_edit_keeps_settled_result(self):
        from OnlyYou.core.state_machine import ActionType

        plugin, _api = await self.build()
        soul_id = "soul-busy-edit"
        run = await plugin.deck_engine.get_or_build_today_run(soul_id=soul_id)
        slot = run.slots[0]
        await plugin.deck_engine.force_set_current_state(
            soul_id=soul_id, action_type=ActionType.BUSY_TASK, slot_key=slot.slot_key
        )
        slot.busy_settled = True
        slot.completion_result = "把稿子收尾了"
        await plugin.deck_engine.save_run(run)

        await plugin.deck_engine.force_set_current_state(
            soul_id=soul_id,
            action_type=ActionType.BUSY_TASK,
            new_title="继续赶稿",
            slot_key=slot.slot_key,
        )
        refreshed = await plugin.deck_engine.get_or_build_today_run(soul_id=soul_id)
        target = next(item for item in refreshed.slots if item.slot_key == slot.slot_key)
        assert target.busy_settled is True
        assert target.completion_result == "把稿子收尾了"

        await plugin.deck_engine.force_set_current_state(
            soul_id=soul_id,
            action_type=ActionType.IDLE_ROAM,
            slot_key=slot.slot_key,
        )
        refreshed = await plugin.deck_engine.get_or_build_today_run(soul_id=soul_id)
        target = next(item for item in refreshed.slots if item.slot_key == slot.slot_key)
        assert target.busy_settled is False
        assert target.completion_result == ""

    async def test_legacy_state_change_clears_settled_result(self):
        from OnlyYou.core.state_machine import ActionType, BotState

        plugin, _api = await self.build()
        soul_id = "soul-busy-legacy"
        run = await plugin.deck_engine.get_or_build_today_run(soul_id=soul_id)
        slot = run.slots[0]
        await plugin.deck_engine.force_set_current_state(
            soul_id=soul_id, action_type=ActionType.BUSY_TASK, slot_key=slot.slot_key
        )
        slot.busy_settled = True
        slot.completion_result = "旧版状态切换也要清空"
        await plugin.deck_engine.save_run(run)
        await plugin.deck_engine.force_set_current_state(
            soul_id=soul_id,
            new_state=BotState.IDLE,
            slot_key=slot.slot_key,
        )
        refreshed = await plugin.deck_engine.get_or_build_today_run(soul_id=soul_id)
        target = next(item for item in refreshed.slots if item.slot_key == slot.slot_key)
        assert target.busy_settled is False

    async def test_explicit_reset_settled_clears_even_for_same_action(self):
        from OnlyYou.core.state_machine import ActionType

        plugin, _api = await self.build()
        soul_id = "soul-busy-reset"
        run = await plugin.deck_engine.get_or_build_today_run(soul_id=soul_id)
        slot = run.slots[0]
        await plugin.deck_engine.force_set_current_state(
            soul_id=soul_id, action_type=ActionType.BUSY_TASK, slot_key=slot.slot_key
        )
        slot.busy_settled = True
        slot.completion_result = "临时成果"
        await plugin.deck_engine.save_run(run)
        await plugin.deck_engine.force_set_current_state(
            soul_id=soul_id,
            action_type=ActionType.BUSY_TASK,
            slot_key=slot.slot_key,
            reset_settled=True,
        )
        refreshed = await plugin.deck_engine.get_or_build_today_run(soul_id=soul_id)
        target = next(item for item in refreshed.slots if item.slot_key == slot.slot_key)
        assert target.busy_settled is False


class TestSurfSharedFlag(ConfigManagerTestBase):
    async def test_history_rows_are_marked_with_real_shared_flag(self):
        plugin, api = await self.build()
        soul = (await plugin.soul_manager.list_souls())[0]
        for index, video_id in enumerate(("bv-shared", "bv-new")):
            await self.db.execute(
                """
                INSERT INTO surfing_history (
                    soul_id, video_id, platform, title, author, watched_at
                ) VALUES (?, ?, 'bilibili', ?, 'up', ?)
                """,
                (soul.soul_id, video_id, f"视频{index}", 2000 + index),
            )
        plugin._shared_video_ids = {soul.soul_id: {"bv-shared"}}
        api.set_mock_request("GET", query={"soul_id": soul.soul_id}, body={})
        response = await api.api_surfing_list_and_trigger()
        assert response["status"] == "ok", response
        flags = {item["video_id"]: item["shared"] for item in response["data"]["videos"]}
        assert flags == {"bv-shared": True, "bv-new": False}


class TestHardcodedParametersAreConfigurable(ConfigManagerTestBase):
    async def test_main_intervals_follow_config(self):
        config = make_plugin_config()
        config["roguelite_schedule_config"].update(
            {"heartbeat_interval_minutes": 7, "gc_check_interval_hours": 3}
        )
        from OnlyYou.main import OnlyYouPlugin

        plugin, _api = await self.build(config=config)
        plugin._heartbeat_interval_seconds = MethodType(
            OnlyYouPlugin._heartbeat_interval_seconds, plugin
        )
        plugin._gc_interval_seconds = MethodType(
            OnlyYouPlugin._gc_interval_seconds, plugin
        )
        assert plugin._heartbeat_interval_seconds() == 7 * 60
        assert plugin._gc_interval_seconds() == 3 * 3600
        await plugin.config_manager.save(
            {"schedule": {"heartbeat_interval_minutes": 30, "gc_check_interval_hours": 12}}
        )
        assert plugin._heartbeat_interval_seconds() == 30 * 60
        assert plugin._gc_interval_seconds() == 12 * 3600

    async def test_mutator_uses_configurable_keywords_and_impact_table(self):
        plugin, _api = await self.build()
        mutator = plugin.schedule_mutator
        mutator.config_manager = plugin.config_manager
        soul = make_soul("soul-mut")
        soul.settings = {
            "urgent_keywords": ["暗号"],
            "mutation_impact": {"busy_l3_direct": 90},
        }
        assert mutator.urgent_keywords(soul) == ["暗号"]
        assert mutator.impact_table(soul)["busy_l3_direct"] == 90
        # 全局默认值仍在未覆盖项上生效
        assert mutator.impact_table(soul)["prelude_l3"] == 75
        assert "急" in mutator.urgent_keywords(None)

    async def test_runtime_queue_caps_follow_config(self):
        config = make_plugin_config()
        config["roguelite_schedule_config"].update(
            {"mused_memory_ring_size": 12, "proactive_anti_repeat_count": 3}
        )
        plugin, _api = await self.build(config=config)
        store = mods.runtime_state.RuntimeStateStore(
            self.db, plugin_config=plugin.config
        )
        assert store.mused_memory_limit == 12
        assert store.recent_proactive_limit == 3
        await plugin.config_manager.save(
            {"proactive": {"mused_memory_ring_size": 50, "proactive_anti_repeat_count": 8}}
        )
        assert store.mused_memory_limit == 50
        assert store.recent_proactive_limit == 8

    async def test_surf_history_trim_respects_cap(self):
        plugin, _api = await self.build()
        for index in range(6):
            await self.db.execute(
                """
                INSERT INTO surfing_history (
                    soul_id, video_id, platform, title, author, watched_at
                ) VALUES (?, ?, 'bilibili', ?, 'up', ?)
                """,
                ("soul-trim", f"bv{index}", f"视频{index}", 1000 + index),
            )
        deleted = await plugin.surfing_pipeline.trim_history(max_per_soul=3)
        assert deleted == 3
        rows = await self.db.fetchall(
            "SELECT video_id FROM surfing_history WHERE soul_id = ?", ("soul-trim",)
        )
        assert len(rows) == 3

    async def test_vision_scores_and_memory_content_limit(self):
        config = make_plugin_config()
        config["surfing_config"] = {
            "vision_fallback_score": 42,
            "vision_default_score": 81,
            "surf_bili_candidates": 2,
            "surf_douyin_candidates": 1,
        }
        plugin, _api = await self.build(config=config)
        vision = plugin.surfing_pipeline.vision_processor
        assert vision.fallback_score == 42
        assert vision.default_score == 81
        assert plugin.config_manager.get("surf_bili_candidates") == 2
        assert plugin.memory_manager.memory_content_max_chars == 60


if __name__ == "__main__":
    unittest.main()
