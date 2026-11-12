"""Offline tests for ``core/web_api.py`` (CONTRACTS.md §4).

Uses the real business components (SoulManager / ModelHub / ProviderFactory /
RogueliteDeckEngine / MemoryManager / UserRelationManager / CyberSurfingPipeline)
assembled against a temporary SQLite database, plus ``set_mock_request`` so the
handlers can be awaited without FastAPI/AstrBot request context.

Covers: the **31** registered routes (v2.8 added the four ``sessions/*`` bot-instance
binding routes, contract §6.1), the ``{status, message, data}`` envelope, the new
``dashboard/overview`` / ``card/*`` / ``models/*`` / ``sessions/*`` routes and the
legacy routes (``souls/detail``, ``cards/list``, ``users/list``,
``memories/list``, ``surfing`` GET).
"""

import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _helpers import (  # noqa: E402
    DEFAULT_FAKE_PROVIDERS,
    DatabaseAsyncTestCase,
    FakeChatProvider,
    FakeTTSProvider,
    build_fake_plugin,
    card_with,
    load_plugin_modules,
    make_slot,
    make_soul,
)

mods = load_plugin_modules()
API_PREFIX = mods.web_api.API_PREFIX
LEGACY_ROUTES = [
    "/init_summary",
    "/souls/detail",
    "/souls/import_persona",
    "/souls/save",
    "/souls/rename",
    "/souls/delete",
    "/schedule/reroll",
    "/schedule/force_state",
    "/cards/list",
    "/cards/save",
    "/cards/delete",
    "/cards/ai_generate",
    "/users/list",
    "/users/update",
    "/memories/list",
    "/memories/mutate",
    "/surfing/list_and_trigger",
]
NEW_ROUTES = [
    "/dashboard/overview",
    "/card/schema",
    "/card/save",
    "/card/ai_generate",
    "/card/preview_prompt",
    "/models/sources",
    "/models/list",
    "/models/config",
    "/models/test",
    "/sessions/default_soul",
]
# v2.8 §6.1: AstrBot bot-instance (platform_id) binding + session memory routes.
V28_ROUTES = [
    "/sessions/platforms",
    "/sessions/bind_platform",
    "/sessions/unbind_platform",
    "/sessions/clear_memory",
]
ALL_ROUTES = LEGACY_ROUTES + NEW_ROUTES + V28_ROUTES


class WebApiTestBase(DatabaseAsyncTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        FakeChatProvider.reset()
        FakeTTSProvider.reset()
        FakeTTSProvider.audio_dir = self.tmpdir
        self.plugin, self.api = await build_fake_plugin(self.db, self.tmpdir)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    async def call(self, method, handler, query=None, body=None):
        self.api.set_mock_request(method, query=query or {}, body=body or {})
        return await handler()

    def assert_ok(self, response):
        assert response["status"] == "ok", response
        assert isinstance(response["message"], str)
        assert isinstance(response["data"], dict)
        return response["data"]

    def assert_error(self, response):
        assert response["status"] == "error", response
        assert isinstance(response["message"], str) and response["message"]
        assert isinstance(response["data"], dict)
        return response

    async def first_soul(self):
        souls = await self.plugin.soul_manager.list_souls()
        return souls[0]

    async def create_soul(self, soul_id, name="第二个角色"):
        soul = make_soul(
            soul_id=soul_id,
            card=card_with(
                identity={"name": name, "tagline": "测试角色"},
                speech={"speaking_style": "简短"},
            ),
        )
        await self.plugin.soul_manager.save_soul(soul)
        return soul


class TestRouteRegistration(WebApiTestBase):
    async def test_registers_31_routes(self):
        self.plugin.context.registered_routes.clear()
        self.api.register_routes()
        routes = self.plugin.context.registered_routes
        assert len(routes) == 31
        paths = [item["route"] for item in routes]
        assert len(paths) == len(set(paths))
        for path in paths:
            assert path.startswith(API_PREFIX + "/")
        for suffix in ALL_ROUTES:
            assert API_PREFIX + suffix in paths, suffix
        for item in routes:
            assert callable(item["handler"])
            assert isinstance(item["methods"], list) and item["methods"]
            assert item["description"]

    async def test_register_routes_without_register_web_api_is_safe(self):
        self.plugin.context = object()  # context without register_web_api
        self.api.register_routes()  # must not raise

    async def test_new_routes_are_bound_to_expected_handlers(self):
        self.plugin.context.registered_routes.clear()
        self.api.register_routes()
        mapping = {
            item["route"].replace(API_PREFIX, ""): item for item in self.plugin.context.registered_routes
        }
        assert mapping["/dashboard/overview"]["methods"] == ["GET"]
        assert mapping["/card/save"]["methods"] == ["POST"]
        assert mapping["/models/config"]["methods"] == ["GET", "POST"]
        assert mapping["/sessions/default_soul"]["methods"] == ["POST"]
        # v2.8 bot-instance binding routes
        assert mapping["/sessions/platforms"]["methods"] == ["GET"]
        for route in (
            "/sessions/bind_platform",
            "/sessions/unbind_platform",
            "/sessions/clear_memory",
        ):
            assert mapping[route]["methods"] == ["POST"]
            assert mapping[route]["description"]


class TestInitSummary(WebApiTestBase):
    async def test_init_summary_structure(self):
        data = self.assert_ok(await self.call("GET", self.api.api_init_summary))
        assert set(data) >= {
            "souls",
            "native_personas",
            "umo_bindings",
            "known_umos",
            "active_soul_id",
            "active_schedule",
            "default_soul_id",
            "model_status",
            "action_types",
        }
        assert data["souls"] and isinstance(data["souls"][0]["card"], dict)
        assert data["active_soul_id"] == data["souls"][0]["soul_id"]
        assert data["default_soul_id"] == data["active_soul_id"]
        assert len(data["action_types"]) == 4
        assert {item["value"] for item in data["action_types"]} == {
            "SLEEP",
            "SURF",
            "IDLE_ROAM",
            "BUSY_TASK",
        }
        assert set(data["model_status"]) == {"main", "decision", "vision", "tts"}
        assert data["model_status"]["main"]["mode"] == "unconfigured"
        assert data["model_status"]["tts"]["mode"] == "off"
        assert isinstance(data["native_personas"], list)
        assert data["native_personas"][0]["persona_name"]
        schedule = data["active_schedule"]
        assert schedule["max_mutations_per_day"] == 2
        assert schedule["mutation_cooldown_minutes"] == 120
        assert 7 <= len(schedule["run"]["slots"]) <= 11


class TestDashboardOverview(WebApiTestBase):
    async def test_dashboard_structure(self):
        data = self.assert_ok(await self.call("GET", self.api.api_dashboard_overview))
        assert set(data) == {
            "soul",
            "vigor",
            "state",
            "schedule",
            "quota",
            "encounters",
            "busy_results",
            "counts",
            "models",
        }
        soul = data["soul"]
        assert set(soul) == {"soul_id", "name", "avatar", "tagline"}
        assert soul["soul_id"]
        state = data["state"]
        assert set(state) >= {
            "state",
            "label",
            "title",
            "armor",
            "slot_key",
            "start_hour",
            "end_hour",
            "remaining_seconds",
            "progress",
            "progress_pct",
            "action_type",
            "action_type_label",
            "completion_result",
            "busy_settled",
            "mutated",
            "next_slot",
        }
        assert state["state"] in ("RESTING", "BUSY", "IDLE")
        assert state["action_type"] in ("SLEEP", "SURF", "IDLE_ROAM", "BUSY_TASK")
        assert isinstance(state["action_type_label"], str) and state["action_type_label"]
        assert 0 <= state["progress_pct"] <= 100
        assert 0.0 <= state["progress"] <= 1.0
        assert state["remaining_seconds"] >= 0
        assert data["vigor"]["chronotype"] in ("early", "normal", "night_owl")
        assert data["schedule"]["max_mutations_per_day"] == 2
        quota = data["quota"]
        assert quota["max"] == 2
        assert quota["used"] == 0
        assert quota["remaining"] == 2
        assert quota["cooldown_remaining_seconds"] == 0
        assert data["encounters"] == []
        assert isinstance(data["busy_results"], list)
        for result in data["busy_results"]:
            assert set(result) >= {
                "slot_key",
                "title",
                "completion_result",
                "progress_pct",
                "start_hour",
                "end_hour",
            }
        slots = data["schedule"]["run"]["slots"]
        assert 7 <= len(slots) <= 11
        assert slots[0]["start_hour"] == 0.0 and slots[-1]["end_hour"] == 24.0
        for slot in slots:
            assert "progress_pct" in slot
        counts = data["counts"]
        assert set(counts) == {"relations", "memories", "videos"}
        assert set(counts["relations"]) == {"l1", "l2", "l3", "total"}
        assert set(counts["memories"]) == {"tier1", "tier2", "tier3", "total"}
        assert isinstance(counts["videos"], int)
        assert set(data["models"]) == {"main", "decision", "vision", "tts"}

    async def test_dashboard_counts_reflect_data(self):
        soul = await self.first_soul()
        # v2.9 §4.3: dashboard relation counters are scoped to the current Soul, so
        # seed the relationships through the per-Soul relation API.
        await self.plugin.user_relation.get_or_create_user(
            "u-l1", "普通群友", soul_id=soul.soul_id
        )
        await self.plugin.user_relation.set_user_tier(
            "u-l3", mods.state_machine.UserTier.L3_CORE, soul_id=soul.soul_id
        )
        await self.plugin.memory_manager.add_memory(
            soul_id=soul.soul_id,
            content="喜欢猫",
            keywords="猫",
            importance=6,
            user_tier=2,
        )
        await self.plugin.memory_manager.add_memory(
            soul_id=soul.soul_id,
            content="海鲜过敏",
            keywords="过敏",
            importance=9,
            user_tier=3,
        )
        await self.db.execute(
            """
            INSERT INTO surfing_history (
                soul_id, video_id, platform, title, author, cover_url, share_url,
                tags_json, hot_comments_json, visual_notes, score, commentary,
                worth_sharing, watched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                soul.soul_id,
                "bv1",
                "bilibili",
                "标题",
                "UP",
                "",
                "",
                "[]",
                "[]",
                "",
                90,
                "好看",
                1,
                1700000000,
            ),
        )
        data = self.assert_ok(
            await self.call(
                "GET", self.api.api_dashboard_overview, query={"soul_id": soul.soul_id}
            )
        )
        assert data["counts"]["relations"] == {"l1": 1, "l2": 0, "l3": 1, "total": 2}
        assert data["counts"]["memories"]["total"] == 2
        assert data["counts"]["memories"]["tier2"] == 1
        assert data["counts"]["memories"]["tier3"] == 1
        assert data["counts"]["videos"] == 1

    async def test_dashboard_query_specific_soul(self):
        second = await self.create_soul("soul_b", name="第二个角色")
        data = self.assert_ok(
            await self.call(
                "GET", self.api.api_dashboard_overview, query={"soul_id": "soul_b"}
            )
        )
        assert data["soul"]["soul_id"] == "soul_b"
        assert data["soul"]["name"] == "第二个角色"
        assert second.soul_id == "soul_b"


class TestNightSlotProgress(WebApiTestBase):
    def test_overnight_night_slot_progress(self):
        # Legacy serialized run payloads may still carry an overnight `end < start`
        # block; the helper keeps its modular fallback for those dict payloads.
        slot = {"start_hour": 23, "end_hour": 7}
        remaining, progress = mods.web_api._slot_remaining_and_progress(
            slot, datetime(2024, 1, 2, 23, 30)
        )
        assert remaining == 27000  # 8h slot, 0.5h elapsed
        assert progress == 0.0625

        remaining, progress = mods.web_api._slot_remaining_and_progress(
            slot, datetime(2024, 1, 2, 1, 0)
        )
        assert remaining == 21600  # 2h elapsed
        assert progress == 0.25

        remaining, progress = mods.web_api._slot_remaining_and_progress(
            slot, datetime(2024, 1, 2, 7, 0)
        )
        assert remaining == 0
        assert progress == 1.0

    def test_normal_daytime_slot_progress(self):
        slot = make_slot("evening", start_hour=18, end_hour=23)
        remaining, progress = mods.web_api._slot_remaining_and_progress(
            slot, datetime(2024, 1, 2, 20, 0)
        )
        assert remaining == 10800
        assert progress == 0.4


class TestCardRoutes(WebApiTestBase):
    async def test_card_schema(self):
        data = self.assert_ok(await self.call("GET", self.api.api_card_schema))
        assert len(data["schema"]) == 10
        assert data["schema"][0]["id"] == "identity"
        assert set(data["card"]) >= {"identity", "speech", "extra"}
        assert data["soul_id"]

    async def test_card_save_then_schema_reflects_it(self):
        soul = await self.first_soul()
        new_card = card_with(
            identity={"name": "新名字", "tagline": "新定位", "avatar": "🌙"},
            speech={"speaking_style": "新的说话方式", "catchphrases": ["好家伙"]},
        )
        data = self.assert_ok(
            await self.call(
                "POST",
                self.api.api_card_save,
                body={"soul_id": soul.soul_id, "card": new_card},
            )
        )
        assert data["soul"]["card"]["identity"]["name"] == "新名字"
        assert len(data["souls"]) >= 1

        schema = self.assert_ok(await self.call("GET", self.api.api_card_schema))
        assert schema["card"]["identity"]["name"] == "新名字"
        assert schema["card"]["speech"]["catchphrases"] == ["好家伙"]

        fetched = await self.plugin.soul_manager.get_soul_by_id(soul.soul_id)
        assert fetched is not None
        assert fetched.card["identity"]["name"] == "新名字"

    async def test_card_save_validation_errors(self):
        response = self.assert_error(
            await self.call("POST", self.api.api_card_save, body={"card": {}})
        )
        assert "soul_id" in response["message"]
        response = self.assert_error(
            await self.call("POST", self.api.api_card_save, body={"soul_id": "x"})
        )
        assert "card" in response["message"]
        response = self.assert_error(
            await self.call(
                "POST",
                self.api.api_card_save,
                body={"soul_id": "does-not-exist", "card": {}},
            )
        )
        assert "不存在" in response["message"] or "角色" in response["message"]

    async def test_card_preview_prompt_from_body(self):
        card = card_with(identity={"name": "星野"}, speech={"speaking_style": "口语化"})
        data = self.assert_ok(
            await self.call(
                "POST", self.api.api_card_preview_prompt, body={"card": card}
            )
        )
        assert data["prompt"].startswith("你正在扮演「星野」。")
        assert data["chars"] == len(data["prompt"])

    async def test_card_preview_prompt_from_saved_soul(self):
        soul = await self.first_soul()
        data = self.assert_ok(
            await self.call(
                "POST",
                self.api.api_card_preview_prompt,
                body={"soul_id": soul.soul_id},
            )
        )
        assert "你正在扮演「" in data["prompt"]
        assert data["chars"] == len(data["prompt"])

    async def test_card_ai_generate_without_model_fails_gracefully(self):
        soul = await self.first_soul()
        response = await self.call(
            "POST",
            self.api.api_card_ai_generate,
            body={"soul_id": soul.soul_id, "seed_text": "一个爱笑的女孩"},
        )
        self.assert_error(response)
        assert "AI 生成角色卡失败" in response["message"]

    async def test_card_ai_generate_with_model_returns_draft(self):
        soul = await self.first_soul()
        await self.call(
            "POST",
            self.api.api_models_config,
            body={
                "model_config": {
                    "main": {"source_id": "deepseek", "model": "deepseek-chat"}
                }
            },
        )
        provider = await self.plugin.model_hub.get_provider("main")
        assert provider is not None
        provider.response_text = (
            '```json\n{"identity": {"name": "AI 草稿角色", '
            '"tagline": "由主模型生成"}, "speech": {"speaking_style": "轻快"}}\n```'
        )
        data = self.assert_ok(
            await self.call(
                "POST",
                self.api.api_card_ai_generate,
                body={"soul_id": soul.soul_id, "seed_text": "爱笑的女孩"},
            )
        )
        assert data["draft_card"]["identity"]["name"] == "AI 草稿角色"
        assert data["section"] is None
        # draft must NOT be persisted
        saved = await self.plugin.soul_manager.get_soul_by_id(soul.soul_id)
        assert saved.card["identity"]["name"] != "AI 草稿角色"

    async def test_card_ai_generate_unknown_section_rejected(self):
        soul = await self.first_soul()
        response = self.assert_error(
            await self.call(
                "POST",
                self.api.api_card_ai_generate,
                body={"soul_id": soul.soul_id, "section": "no_such_section"},
            )
        )
        assert "分区" in response["message"]


class TestModelRoutes(WebApiTestBase):
    async def test_models_sources_never_leaks_key(self):
        data = self.assert_ok(await self.call("GET", self.api.api_models_sources))
        assert data["sources"]
        assert data["tts_providers"]
        for source in data["sources"]:
            assert "key" not in source
            assert "has_key" in source
        import json as _json

        serialized = _json.dumps(data, ensure_ascii=False)
        assert "sk-deepseek-secret" not in serialized
        assert "sk-tts-secret" not in serialized
        assert any(item["id"] == "tts-disabled" for item in data["tts_providers"])

    async def test_models_list_merges_config_and_remote(self):
        FakeChatProvider.class_get_models = ["deepseek-chat", "deepseek-reasoner"]
        data = self.assert_ok(
            await self.call(
                "GET", self.api.api_models_list, query={"source_id": "deepseek"}
            )
        )
        assert data["source_id"] == "deepseek"
        by_model = {item["model"]: item for item in data["models"]}
        assert by_model["deepseek-chat"]["source"] == "config"
        assert by_model["deepseek-reasoner"]["source"] == "remote"

    async def test_models_list_requires_source_id(self):
        response = self.assert_error(
            await self.call("GET", self.api.api_models_list)
        )
        assert "source_id" in response["message"]

    async def test_models_config_get_defaults(self):
        data = self.assert_ok(await self.call("GET", self.api.api_models_config))
        assert set(data["model_config"]) == {"main", "decision", "vision", "tts"}
        assert set(data["status"]) == {"main", "decision", "vision", "tts"}
        assert data["model_config"]["tts"]["mode"] == "off"

    async def test_models_config_post_persists_and_hot_rebuilds(self):
        # make the plugin own a registered main provider first
        await self.call(
            "POST",
            self.api.api_models_config,
            body={
                "model_config": {
                    "main": {"source_id": "deepseek", "model": "deepseek-chat"}
                }
            },
        )
        main_id = await self.plugin.model_hub.get_main_provider_id()
        assert main_id == "onlyyou::main"
        assert "onlyyou::main" in self.plugin.context.provider_manager.inst_map

        response = await self.call(
            "POST",
            self.api.api_models_config,
            body={
                "model_config": {
                    "main": {"source_id": "deepseek", "model": "deepseek-reasoner"},
                    "decision": {"inherit": True},
                }
            },
        )
        data = self.assert_ok(response)
        assert "热重建" in response["message"]
        assert data["model_config"]["main"]["model"] == "deepseek-reasoner"
        assert "onlyyou::main" not in self.plugin.context.provider_manager.inst_map

        stored = await self.db.get_setting("model_config", None)
        assert stored["main"]["model"] == "deepseek-reasoner"

    async def test_models_config_post_rejects_missing_payload(self):
        response = self.assert_error(
            await self.call("POST", self.api.api_models_config, body={"foo": 1})
        )
        assert "model_config" in response["message"]

    async def test_models_test_main_role_ok(self):
        await self.call(
            "POST",
            self.api.api_models_config,
            body={
                "model_config": {
                    "main": {"source_id": "deepseek", "model": "deepseek-chat"}
                }
            },
        )
        data = self.assert_ok(
            await self.call("POST", self.api.api_models_test, body={"role": "main"})
        )
        assert data["status"] == "ok"
        assert data["role"] == "main"
        assert isinstance(data["latency_ms"], int)
        assert data["reply"] == "pong"

    async def test_models_test_tts_disabled_returns_error_payload(self):
        response = await self.call(
            "POST", self.api.api_models_test, body={"role": "tts"}
        )
        data = self.assert_ok(response)
        assert data["status"] == "error"
        assert "TTS 未开启" in data["error"]

    async def test_models_test_invalid_role(self):
        response = self.assert_error(
            await self.call("POST", self.api.api_models_test, body={"role": "bogus"})
        )
        assert "未知模型角色" in response["message"]

    async def test_models_test_unconfigured_is_error_payload(self):
        data = self.assert_ok(
            await self.call("POST", self.api.api_models_test, body={"role": "main"})
        )
        assert data["status"] == "error"


class TestSessionRoutes(WebApiTestBase):
    async def test_set_default_soul(self):
        await self.create_soul("soul_b", name="第二个角色")
        response = await self.call(
            "POST",
            self.api.api_sessions_default_soul,
            body={"soul_id": "soul_b"},
        )
        data = self.assert_ok(response)
        assert data["default_soul_id"] == "soul_b"
        assert [item["soul_id"] for item in data["souls"]] and "soul_b" in [
            item["soul_id"] for item in data["souls"]
        ]
        assert isinstance(data["umo_bindings"], dict)
        # persisted in plugin_settings and reflected by init_summary
        assert await self.db.get_setting("default_soul_id", "") == "soul_b"
        summary = self.assert_ok(await self.call("GET", self.api.api_init_summary))
        assert summary["default_soul_id"] == "soul_b"

    async def test_set_default_soul_validation(self):
        response = self.assert_error(
            await self.call("POST", self.api.api_sessions_default_soul, body={})
        )
        assert "soul_id" in response["message"]
        response = self.assert_error(
            await self.call(
                "POST",
                self.api.api_sessions_default_soul,
                body={"soul_id": "ghost"},
            )
        )
        assert "不存在" in response["message"]


class TestLegacyRoutes(WebApiTestBase):
    async def test_souls_detail(self):
        soul = await self.first_soul()
        data = self.assert_ok(
            await self.call(
                "GET", self.api.api_souls_detail, query={"soul_id": soul.soul_id}
            )
        )
        assert data["soul"]["soul_id"] == soul.soul_id
        assert len(data["card_schema"]) == 10
        assert "schedule" in data
        assert data["schedule"]["run"]["slots"]

    async def test_cards_list(self):
        data = self.assert_ok(await self.call("GET", self.api.api_cards_list))
        assert data["soul_id"]
        assert data["general_cards"] and data["general_cards"][0]["scope"] == "global"
        assert data["custom_cards"] == []
        assert len(data["action_types"]) == 4
        assert {item["value"] for item in data["action_types"]} == {
            "SLEEP",
            "SURF",
            "IDLE_ROAM",
            "BUSY_TASK",
        }

    async def test_users_list_and_update(self):
        await self.plugin.user_relation.get_or_create_user("u-1", "阿一")
        data = self.assert_ok(await self.call("GET", self.api.api_users_list))
        assert len(data["users"]) == 1
        assert data["users"][0]["global_uid"] == "u-1"
        assert data["counts"]["l1_count"] == 1

        updated = self.assert_ok(
            await self.call(
                "POST",
                self.api.api_users_update,
                body={"global_uid": "u-1", "tier": 2, "affinity": 42},
            )
        )
        assert updated["user"]["tier"] == 2
        assert updated["user"]["affinity"] == 42.0

    async def test_memories_list_and_stats(self):
        soul = await self.first_soul()
        await self.plugin.memory_manager.add_memory(
            soul_id=soul.soul_id,
            content="阿泽海鲜过敏",
            keywords="过敏,海鲜",
            importance=9,
            user_tier=3,
        )
        data = self.assert_ok(
            await self.call(
                "GET", self.api.api_memories_list, query={"soul_id": soul.soul_id}
            )
        )
        assert len(data["memories"]) == 1
        assert data["memories"][0]["content"] == "阿泽海鲜过敏"
        stats = data["stats"]
        assert stats["total"] == 1
        assert stats["tier3_count"] == 1
        assert stats["sliding_window_size"] == 5
        assert stats["max_injected_memories"] == 6
        assert stats["max_memory_chars_budget"] == 400

    async def test_surfing_get(self):
        data = self.assert_ok(
            await self.call("GET", self.api.api_surfing_list_and_trigger)
        )
        assert data["videos"] == []
        assert isinstance(data["known_umos"], list)

    async def test_souls_delete_last_soul_returns_ok(self):
        """Deleting the last remaining soul must not raise IndexError."""
        soul = await self.first_soul()
        data = self.assert_ok(
            await self.call(
                "POST", self.api.api_souls_delete, body={"soul_id": soul.soul_id}
            )
        )
        assert data["deleted_soul_id"] == soul.soul_id
        # `list_souls()` recreates a default soul, so the console never ends empty.
        assert data["souls"]
        assert data["active_soul_id"]


if __name__ == "__main__":
    unittest.main()
