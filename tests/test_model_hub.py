"""Offline tests for ``core/model_hub.py`` (CONTRACTS.md §2.2).

Covers: ``normalize_model_config``, decision/vision inheritance, TTS disabled by
default, ``get_main_provider_id`` three branches (unconfigured / reuse / self-built
``onlyyou::main``), persistence in ``plugin_settings.model_config`` with hot rebuild,
``call_llm_text`` going straight through ``inst.text_chat``, the one-shot legacy
``model_hub_config`` migration, and the strict "never call ``context.get_config`` /
``get_current_chat_provider_id``" rule.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _helpers import (  # noqa: E402
    DatabaseAsyncTestCase,
    FakeChatProvider,
    FakeContext,
    FakeProviderManager,
    FakeTTSProvider,
    StrictContext,
    load_plugin_modules,
)

mods = load_plugin_modules()
ModelHub = mods.model_hub.ModelHub
ProviderFactory = mods.provider_factory.ProviderFactory
normalize_model_config = mods.model_hub.normalize_model_config

SOURCE = {
    "id": "deepseek",
    "provider": "deepseek",
    "type": "openai_chat_completion",
    "provider_type": "chat_completion",
    "key": "sk-secret",
    "api_base": "https://api.deepseek.example/v1",
    "timeout": 120,
    "proxy": "",
    "enable": True,
}
TTS_SOURCE = {
    "id": "edge-tts-source",
    "provider": "edge-tts",
    "type": "openai_tts_api",
    "provider_type": "text_to_speech",
    "key": "sk-tts",
    "api_base": "https://tts.example/v1",
    "timeout": 60,
    "proxy": "",
    "enable": True,
}
PROVIDERS = [
    {
        "id": "deepseek/deepseek-chat",
        "enable": True,
        "provider_source_id": "deepseek",
        "model": "deepseek-chat",
        "modalities": ["text"],
    },
    {
        "id": "deepseek/deepseek-reasoner",
        "enable": True,
        "provider_source_id": "deepseek",
        "model": "deepseek-reasoner",
        "modalities": ["text"],
    },
]
MAIN_CONFIG = {"main": {"source_id": "deepseek", "model": "deepseek-chat"}}


class ModelHubTestBase(DatabaseAsyncTestCase):
    def setUp(self):
        super().setUp()
        FakeChatProvider.reset()
        FakeTTSProvider.reset()
        FakeTTSProvider.audio_dir = self.tmpdir

    def make_context(self, context_cls=FakeContext):
        manager = FakeProviderManager(
            sources=[SOURCE, TTS_SOURCE],
            providers=PROVIDERS,
            provider_settings={"default_provider_id": "x"},
            audio_dir=self.tmpdir,
        )
        context = context_cls(manager)
        return context, ProviderFactory(context)

    async def make_hub(self, plugin_config=None, model_config=None, context_cls=FakeContext):
        context, factory = self.make_context(context_cls)
        hub = ModelHub(
            context=context,
            plugin_config=plugin_config if plugin_config is not None else {},
            db=self.db,
            provider_factory=factory,
        )
        await hub.load()
        if model_config is not None:
            await hub.save_model_config(model_config)
        return hub, context, factory


class TestNormalizeModelConfig(unittest.TestCase):
    def test_defaults_are_complete(self):
        cfg = normalize_model_config(None)
        assert set(cfg) == {"main", "decision", "vision", "tts"}
        assert cfg["main"] == {"source_id": "", "model": ""}
        assert cfg["decision"]["inherit"] is True
        assert cfg["vision"]["inherit"] is True
        assert cfg["tts"]["mode"] == "off"
        assert cfg["tts"]["scope"] == "private"
        assert cfg["tts"]["probability"] == 1.0
        assert cfg["tts"]["dual_output"] is False

    def test_coerces_bad_values(self):
        cfg = normalize_model_config(
            {
                "main": {"source_id": "  deepseek  ", "model": 123},
                "decision": {"inherit": "false", "source_id": "s", "model": "m"},
                "tts": {
                    "mode": "BOGUS",
                    "scope": "EVERYONE",
                    "probability": "7",
                    "dual_output": "yes",
                },
            }
        )
        assert cfg["main"] == {"source_id": "deepseek", "model": "123"}
        assert cfg["decision"]["inherit"] is False
        assert cfg["tts"]["mode"] == "off"
        assert cfg["tts"]["scope"] == "private"
        assert cfg["tts"]["probability"] == 1.0
        assert cfg["tts"]["dual_output"] is True

    def test_probability_clamped(self):
        assert normalize_model_config({"tts": {"probability": -3}})["tts"]["probability"] == 0.0
        assert normalize_model_config({"tts": {"probability": 3}})["tts"]["probability"] == 1.0


class TestRoleInheritance(ModelHubTestBase):
    async def test_decision_and_vision_inherit_main_by_default(self):
        hub, _ctx, _factory = await self.make_hub(model_config=MAIN_CONFIG)
        assert await hub.resolve_role("main") == {
            "source_id": "deepseek",
            "model": "deepseek-chat",
        }
        assert await hub.resolve_role("decision") == await hub.resolve_role("main")
        assert await hub.resolve_role("vision") == await hub.resolve_role("main")
        # legacy alias
        assert await hub.resolve_role("small") == await hub.resolve_role("main")

    async def test_decision_explicit_override(self):
        hub, _ctx, _factory = await self.make_hub(
            model_config={
                "main": {"source_id": "deepseek", "model": "deepseek-chat"},
                "decision": {
                    "inherit": False,
                    "source_id": "deepseek",
                    "model": "deepseek-reasoner",
                },
            }
        )
        assert await hub.resolve_role("decision") == {
            "source_id": "deepseek",
            "model": "deepseek-reasoner",
        }
        # explicit override makes the status non-inherited
        status = await hub.get_status()
        assert status["decision"]["mode"] == "plugin"
        assert status["decision"]["inherited"] is False
        assert status["main"]["inherited"] is False

    async def test_inherit_true_wins_over_own_values(self):
        hub, _ctx, _factory = await self.make_hub(
            model_config={
                "main": {"source_id": "deepseek", "model": "deepseek-chat"},
                "vision": {
                    "inherit": True,
                    "source_id": "deepseek",
                    "model": "deepseek-reasoner",
                },
            }
        )
        assert await hub.resolve_role("vision") == {
            "source_id": "deepseek",
            "model": "deepseek-chat",
        }

    async def test_tts_never_inherits_and_off_by_default(self):
        hub, _ctx, _factory = await self.make_hub(
            model_config={"main": {"source_id": "deepseek", "model": "deepseek-chat"}}
        )
        assert await hub.resolve_role("tts") is None
        assert await hub.get_provider("tts") is None
        assert await hub.synthesize("你好") is None
        status = await hub.get_status()
        assert status["tts"]["mode"] == "off"
        assert status["tts"]["configured"] is False
        assert status["tts"]["ok"] is False
        assert status["tts"]["label"] == "已关闭"

    async def test_tts_provider_mode_resolution(self):
        hub, _ctx, _factory = await self.make_hub(
            model_config={
                "tts": {"mode": "provider", "provider_id": "edge-enabled"}
            }
        )
        assert await hub.resolve_role("tts") == {
            "mode": "provider",
            "provider_id": "edge-enabled",
        }

    async def test_invalid_role_returns_none(self):
        hub, _ctx, _factory = await self.make_hub(model_config=MAIN_CONFIG)
        assert await hub.resolve_role("bogus") is None
        assert await hub.resolve_role("") is None


class TestMainProviderId(ModelHubTestBase):
    async def test_unconfigured_returns_empty_string(self):
        hub, _ctx, _factory = await self.make_hub()
        assert await hub.get_main_provider_id() == ""

    async def test_reuse_existing_astrbot_instance_id(self):
        hub, _ctx, factory = await self.make_hub(model_config=MAIN_CONFIG)
        loaded = FakeChatProvider(
            {
                "id": "deepseek/deepseek-chat",
                "provider_source_id": "deepseek",
                "model": "deepseek-chat",
                "type": "openai_chat_completion",
                "provider_type": "chat_completion",
            }
        )
        factory.provider_manager.add_loaded("deepseek/deepseek-chat", loaded)
        assert await hub.get_main_provider_id() == "deepseek/deepseek-chat"
        assert factory.provider_manager.inst_map.get("onlyyou::main") is None

    async def test_self_built_instance_registers_onlyyou_main(self):
        hub, _ctx, factory = await self.make_hub(model_config=MAIN_CONFIG)
        main_id = await hub.get_main_provider_id()
        assert main_id == "onlyyou::main"
        instance = factory.provider_manager.inst_map["onlyyou::main"]
        assert isinstance(instance, FakeChatProvider)
        assert factory.provider_manager.provider_insts == []

    async def test_build_failure_returns_empty_string(self):
        hub, _ctx, factory = await self.make_hub(
            model_config={"main": {"source_id": "no-such-source", "model": "m"}}
        )
        assert await hub.get_main_provider_id() == ""


class TestNeverCallsPlatformModelSelection(ModelHubTestBase):
    async def test_model_hub_never_reads_context_model_selection(self):
        hub, context, _factory = await self.make_hub(
            plugin_config={"model_hub_config": {"main_llm_provider_id": "x"}},
            model_config={
                **MAIN_CONFIG,
                "tts": {
                    "mode": "source",
                    "source_id": "edge-tts-source",
                    "model": "tts-1",
                    "voice": "alloy",
                },
            },
            context_cls=StrictContext,
        )
        # every public entry point is exercised; any forbidden access would raise
        await hub.load()
        await hub.get_model_config_async()
        await hub.get_status()
        await hub.resolve_role("main")
        await hub.resolve_role("decision")
        await hub.resolve_role("vision")
        await hub.resolve_role("tts")
        await hub.get_main_provider_id()
        await hub.call_llm_text("hi", role="main")
        await hub.call_llm_json("hi", role="decision", default={})
        await hub.test_role("main")
        await hub.synthesize("你好")
        await hub.save_model_config(MAIN_CONFIG)
        assert context.forbidden_calls == []


class TestPersistenceAndHotRebuild(ModelHubTestBase):
    async def test_save_model_config_persists_and_unregisters_main(self):
        hub, _ctx, factory = await self.make_hub(model_config=MAIN_CONFIG)
        first_id = await hub.get_main_provider_id()
        assert first_id == "onlyyou::main"
        assert "onlyyou::main" in factory.provider_manager.inst_map

        saved = await hub.save_model_config(
            {"main": {"source_id": "deepseek", "model": "deepseek-reasoner"}}
        )
        assert saved["main"]["model"] == "deepseek-reasoner"
        # unregister_main executed -> next AstrBot request goes through a fresh instance
        assert "onlyyou::main" not in factory.provider_manager.inst_map

        stored = await self.db.get_setting("model_config", None)
        assert stored is not None
        assert stored["main"] == {"source_id": "deepseek", "model": "deepseek-reasoner"}
        assert set(stored) == {"main", "decision", "vision", "tts"}

        # a brand-new hub instance reading the same DB sees the persisted config
        hub2 = ModelHub(
            context=_ctx, plugin_config={}, db=self.db, provider_factory=factory
        )
        await hub2.load()
        assert hub2.get_model_config()["main"]["model"] == "deepseek-reasoner"

    async def test_get_main_provider_id_hot_rebuilds_after_save(self):
        hub, _ctx, factory = await self.make_hub(model_config=MAIN_CONFIG)
        await hub.get_main_provider_id()
        old_instance = factory.provider_manager.inst_map["onlyyou::main"]
        await hub.save_model_config(
            {"main": {"source_id": "deepseek", "model": "deepseek-reasoner"}}
        )
        main_id = await hub.get_main_provider_id()
        assert main_id == "onlyyou::main"
        new_instance = factory.provider_manager.inst_map["onlyyou::main"]
        assert new_instance is not old_instance
        assert new_instance.provider_config["model"] == "deepseek-reasoner"


class TestLlmCalls(ModelHubTestBase):
    async def test_call_llm_text_uses_text_chat(self):
        hub, _ctx, factory = await self.make_hub(model_config=MAIN_CONFIG)
        provider = await hub.get_provider("main")
        assert isinstance(provider, FakeChatProvider)
        provider.response_text = "  你好呀  "
        text = await hub.call_llm_text(
            "在干嘛", role="decision", system_prompt="sys", image_urls=["http://img"]
        )
        assert text == "你好呀"
        call = provider.text_chat_calls[-1]
        assert call["prompt"] == "在干嘛"
        assert call["system_prompt"] == "sys"
        assert call["image_urls"] == ["http://img"]

    async def test_call_llm_text_empty_system_prompt_becomes_none(self):
        hub, _ctx, _factory = await self.make_hub(model_config=MAIN_CONFIG)
        provider = await hub.get_provider("main")
        await hub.call_llm_text("hi", role="main")
        assert provider.text_chat_calls[-1]["system_prompt"] is None
        assert provider.text_chat_calls[-1]["image_urls"] is None

    async def test_call_llm_text_unconfigured_returns_empty(self):
        hub, _ctx, _factory = await self.make_hub()
        assert await hub.call_llm_text("hi") == ""

    async def test_call_llm_json_parses_fenced_json(self):
        hub, _ctx, _factory = await self.make_hub(model_config=MAIN_CONFIG)
        provider = await hub.get_provider("main")
        provider.response_text = '```json\n{"a": 1}\n```'
        assert await hub.call_llm_json("hi", role="main") == {"a": 1}

    async def test_call_llm_json_returns_default_on_garbage(self):
        hub, _ctx, _factory = await self.make_hub(model_config=MAIN_CONFIG)
        provider = await hub.get_provider("main")
        provider.response_text = "not json at all"
        assert await hub.call_llm_json("hi", role="main", default={"fallback": True}) == {
            "fallback": True
        }

    def test_extract_json_variants(self):
        assert ModelHub.extract_json('{"a": 1}') == {"a": 1}
        assert ModelHub.extract_json("prefix {\"a\": 2} suffix") == {"a": 2}
        assert ModelHub.extract_json("<think>x</think>[1, 2]") == [1, 2]
        assert ModelHub.extract_json("") is None
        assert ModelHub.extract_json("nope") is None


class TestStatusAndDiagnostics(ModelHubTestBase):
    async def test_get_status_four_roles(self):
        hub, _ctx, _factory = await self.make_hub(model_config=MAIN_CONFIG)
        status = await hub.get_status()
        assert set(status) == {"main", "decision", "vision", "tts"}
        assert status["main"]["configured"] is True
        assert status["main"]["mode"] in ("reuse", "plugin")
        assert status["main"]["ok"] is True
        assert status["main"]["source_id"] == "deepseek"
        assert status["main"]["model"] == "deepseek-chat"
        assert status["decision"]["inherited"] is True
        assert status["vision"]["inherited"] is True
        assert status["tts"]["role"] == "tts"

    async def test_test_role_main_success(self):
        hub, _ctx, _factory = await self.make_hub(model_config=MAIN_CONFIG)
        result = await hub.test_role("main")
        assert result["status"] == "ok"
        assert result["role"] == "main"
        assert result["model"] == "deepseek-chat"
        assert isinstance(result["latency_ms"], int)
        assert result["reply"] == "pong"

    async def test_test_role_unconfigured_error(self):
        hub, _ctx, _factory = await self.make_hub()
        result = await hub.test_role("main")
        assert result["status"] == "error"
        assert result["role"] == "main"
        assert "error" in result

    async def test_test_tts_disabled_error(self):
        hub, _ctx, _factory = await self.make_hub()
        result = await hub.test_role("tts")
        assert result["status"] == "error"
        assert "TTS 未开启" in result["error"]

    async def test_tts_synthesize_and_test_role(self):
        hub, _ctx, factory = await self.make_hub(
            model_config={
                "tts": {
                    "mode": "source",
                    "source_id": "edge-tts-source",
                    "model": "tts-1",
                    "voice": "alloy",
                }
            }
        )
        path = await hub.synthesize("你好呀")
        assert path is not None and os.path.isfile(path)
        result = await hub.test_role("tts")
        assert result["status"] == "ok"
        assert result["role"] == "tts"
        assert os.path.isfile(result["audio_path"])
        assert result["audio_base64"]

    async def test_dispose_delegates_to_provider_factory(self):
        hub, _ctx, factory = await self.make_hub(model_config=MAIN_CONFIG)
        await hub.get_main_provider_id()
        assert "onlyyou::main" in factory.provider_manager.inst_map
        await hub.dispose()
        assert "onlyyou::main" not in factory.provider_manager.inst_map


class TestLegacyMigration(ModelHubTestBase):
    async def test_legacy_model_hub_config_is_migrated_once(self):
        legacy_config = {
            "model_hub_config": {
                "main_llm_provider_id": "deepseek/deepseek-chat",
                "small_llm_provider_id": "deepseek/deepseek-reasoner",
                "vision_llm_provider_id": "deepseek/deepseek-chat",
            }
        }
        context, factory = self.make_context()
        hub = ModelHub(
            context=context, plugin_config=legacy_config, db=self.db, provider_factory=factory
        )
        await hub.load()

        cfg = hub.get_model_config()
        assert cfg["main"] == {"source_id": "deepseek", "model": "deepseek-chat"}
        assert cfg["decision"] == {
            "inherit": False,
            "source_id": "deepseek",
            "model": "deepseek-reasoner",
        }
        assert cfg["vision"]["inherit"] is True
        assert await self.db.get_setting("legacy_model_migrated", False) is True
        assert await self.db.get_setting("model_config", None) is not None

    async def test_migration_flag_prevents_second_migration(self):
        first_legacy = {
            "model_hub_config": {"main_llm_provider_id": "deepseek/deepseek-chat"}
        }
        context, factory = self.make_context()
        hub = ModelHub(
            context=context, plugin_config=first_legacy, db=self.db, provider_factory=factory
        )
        await hub.load()
        migrated = hub.get_model_config()
        assert migrated["main"]["model"] == "deepseek-chat"

        # even if the old config changes, the one-shot flag stops re-migration
        second_legacy = {
            "model_hub_config": {"main_llm_provider_id": "deepseek/deepseek-reasoner"}
        }
        hub2 = ModelHub(
            context=context, plugin_config=second_legacy, db=self.db, provider_factory=factory
        )
        await hub2.load()
        assert hub2.get_model_config()["main"]["model"] == "deepseek-chat"

    async def test_migration_unknown_provider_still_sets_flag(self):
        context, factory = self.make_context()
        hub = ModelHub(
            context=context,
            plugin_config={"model_hub_config": {"main_llm_provider_id": "unknown-id"}},
            db=self.db,
            provider_factory=factory,
        )
        await hub.load()
        assert hub.get_model_config()["main"]["model"] == ""
        assert await self.db.get_setting("legacy_model_migrated", False) is True

    async def test_migration_skipped_when_main_already_configured(self):
        context, factory = self.make_context()
        await self.db.set_setting(
            "model_config",
            {"main": {"source_id": "deepseek", "model": "deepseek-reasoner"}},
        )
        hub = ModelHub(
            context=context,
            plugin_config={
                "model_hub_config": {"main_llm_provider_id": "deepseek/deepseek-chat"}
            },
            db=self.db,
            provider_factory=factory,
        )
        await hub.load()
        assert hub.get_model_config()["main"]["model"] == "deepseek-reasoner"
        assert await self.db.get_setting("legacy_model_migrated", False) is True


if __name__ == "__main__":
    unittest.main()
