"""Offline tests for ``core/provider_factory.py`` (CONTRACTS.md §2.1).

Covers: ``list_sources`` key redaction, ``list_models`` config+remote merge/dedupe,
reuse of already-enabled AstrBot instances, plugin-built instances through
``dynamic_import_provider`` + ``provider_cls_map[type].cls_type`` + ``initialize``,
``(source, model)`` caching, ``register_main``/``unregister_main``, ``dispose``
cleanup (own instances only) and ``find_source_model_by_provider_id``.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _helpers import (  # noqa: E402
    DEFAULT_FAKE_PROVIDERS,
    DEFAULT_FAKE_SOURCES,
    FakeChatProvider,
    FakeContext,
    FakeProviderManager,
    FakeTTSProvider,
    TempDirAsyncTestCase,
    load_plugin_modules,
)

mods = load_plugin_modules()
ProviderFactory = mods.provider_factory.ProviderFactory


def _source(source_id="deepseek", key="sk-secret", provider_type="chat_completion"):
    return {
        "id": source_id,
        "provider": source_id,
        "type": "openai_chat_completion",
        "provider_type": provider_type,
        "key": key,
        "api_base": "https://%s.example/v1" % source_id,
        "timeout": 120,
        "proxy": "",
        "custom_headers": {},
        "enable": True,
    }


class ProviderFactoryTestBase(TempDirAsyncTestCase):
    def setUp(self):
        super().setUp()
        FakeChatProvider.reset()
        FakeTTSProvider.reset()
        FakeTTSProvider.audio_dir = self.tmpdir

    def make_factory(self, sources=None, providers=None, provider_settings=None):
        self.pm = FakeProviderManager(
            sources=DEFAULT_FAKE_SOURCES if sources is None else sources,
            providers=DEFAULT_FAKE_PROVIDERS if providers is None else providers,
            provider_settings=provider_settings or {"default_provider_id": "x"},
            audio_dir=self.tmpdir,
        )
        self.context = FakeContext(self.pm)
        return ProviderFactory(self.context)


class TestListSources(ProviderFactoryTestBase):
    async def test_list_sources_never_leaks_api_key(self):
        factory = self.make_factory()
        sources = factory.list_sources()
        assert len(sources) == len(DEFAULT_FAKE_SOURCES)
        for item in sources:
            assert set(item) == {
                "id",
                "type",
                "provider_type",
                "provider",
                "api_base",
                "has_key",
                "enable",
            }
            assert "key" not in item
            assert "sk-deepseek-secret" not in str(item)
            assert "sk-tts-secret" not in str(item)
        deepseek = next(item for item in sources if item["id"] == "deepseek")
        assert deepseek["has_key"] is True
        assert deepseek["provider"] == "deepseek"
        assert deepseek["provider_type"] == "chat_completion"
        ailiyun = next(item for item in sources if item["id"] == "ailiyun")
        assert ailiyun["has_key"] is False

    async def test_list_sources_empty_manager_is_safe(self):
        factory = self.make_factory(sources=[])
        assert factory.list_sources() == []

    async def test_list_sources_list_key_container(self):
        source = _source(key=["", "  ", "sk-list"])
        factory = self.make_factory(sources=[source], providers=[])
        info = factory.list_sources()[0]
        assert info["has_key"] is True


class TestListModels(ProviderFactoryTestBase):
    async def test_list_models_merges_config_and_remote_and_dedupes(self):
        FakeChatProvider.class_get_models = ["deepseek-chat", "deepseek-reasoner"]
        factory = self.make_factory()
        models = await factory.list_models("deepseek")
        by_source = {}
        for item in models:
            by_source.setdefault(item["source"], []).append(item["model"])
        config_models = by_source.get("config", [])
        remote_models = by_source.get("remote", [])
        assert config_models == ["deepseek-chat"]
        # remote (already-configured model is de-duplicated):
        assert remote_models == ["deepseek-reasoner"]
        sources_of = {item["model"]: item["source"] for item in models}
        assert sources_of["deepseek-chat"] == "config"
        # temp instance was created through the fake provider manager and terminated
        assert self.pm.dynamic_import_calls == ["openai_chat_completion"]
        assert len(FakeChatProvider.instances) == 1
        assert FakeChatProvider.instances[0].terminated is True
        assert FakeChatProvider.instances[0].initialized is True

    async def test_list_models_config_entry_fields(self):
        factory = self.make_factory()
        models = await factory.list_models("ailiyun")
        kimi = next(item for item in models if item["model"] == "kimi-k2.5")
        assert kimi == {
            "model": "kimi-k2.5",
            "id": "ailiyun/kimi-k2.5",
            "enable": False,
            "modalities": ["text", "image"],
            "source": "config",
        }

    async def test_list_models_unknown_source_returns_empty(self):
        factory = self.make_factory()
        assert await factory.list_models("") == []
        assert await factory.list_models("no-such-source") == []


class TestListTtsProviders(ProviderFactoryTestBase):
    async def test_list_tts_providers_instance_and_disabled_config(self):
        factory = self.make_factory()
        enabled_tts = FakeTTSProvider(
            {"id": "edge-enabled", "type": "openai_tts_api", "model": "edge-tts"}
        )
        self.context.tts_providers = [enabled_tts]
        providers = factory.list_tts_providers()
        ids = [item["id"] for item in providers]
        assert ids[0] == "edge-enabled"
        assert "tts-disabled" in ids
        enabled = next(item for item in providers if item["id"] == "edge-enabled")
        assert enabled["enable"] is True
        assert enabled["model"] == "edge-tts"
        disabled = next(item for item in providers if item["id"] == "tts-disabled")
        assert disabled["enable"] is False


class TestChatProviderReuseAndBuild(ProviderFactoryTestBase):
    async def test_reuses_enabled_astrbot_instance(self):
        factory = self.make_factory()
        loaded = FakeChatProvider(
            {
                "id": "deepseek/deepseek-chat",
                "provider_source_id": "deepseek",
                "model": "deepseek-chat",
                "type": "openai_chat_completion",
                "provider_type": "chat_completion",
            }
        )
        self.pm.add_loaded("deepseek/deepseek-chat", loaded)
        provider = await factory.get_chat_provider("deepseek", "deepseek-chat")
        assert provider is loaded
        assert self.pm.dynamic_import_calls == []
        assert factory.is_owned_instance(provider) is False
        assert factory.find_loaded_provider_id(provider) == "deepseek/deepseek-chat"
        # second call hits the (source, model) cache
        assert await factory.get_chat_provider("deepseek", "deepseek-chat") is loaded

    async def test_disabled_astrbot_instance_is_not_reused(self):
        factory = self.make_factory()
        loaded = FakeChatProvider(
            {
                "id": "ailiyun/kimi-k2.5",
                "provider_source_id": "ailiyun",
                "model": "kimi-k2.5",
                "type": "openai_chat_completion",
                "provider_type": "chat_completion",
            }
        )
        self.pm.add_loaded("ailiyun/kimi-k2.5", loaded)
        provider = await factory.get_chat_provider("ailiyun", "kimi-k2.5")
        assert provider is not loaded
        assert factory.is_owned_instance(provider) is True

    async def test_builds_plugin_instance_with_import_cls_initialize(self):
        factory = self.make_factory()
        provider = await factory.get_chat_provider("deepseek", "deepseek-chat")
        assert provider is not None
        assert self.pm.dynamic_import_calls == ["openai_chat_completion"]
        assert provider.initialized is True
        assert provider.provider_config["id"] == "onlyyou::chat::deepseek::deepseek-chat"
        assert provider.provider_config["provider_source_id"] == "deepseek"
        assert provider.provider_config["model"] == "deepseek-chat"
        assert provider.provider_settings == {"default_provider_id": "x"}
        # API key resolved in the merged config
        assert provider.provider_config["key"] == "sk-deepseek-secret"
        assert factory.is_owned_instance(provider) is True
        # not appended to AstrBot's own provider list
        assert self.pm.provider_insts == []

    async def test_caches_built_instance_by_source_and_model(self):
        factory = self.make_factory()
        first = await factory.get_chat_provider("deepseek", "deepseek-chat")
        second = await factory.get_chat_provider("deepseek", "deepseek-chat")
        assert first is second
        assert len(FakeChatProvider.instances) == 1
        assert len(self.pm.dynamic_import_calls) == 1
        # different model -> different instance
        third = await factory.get_chat_provider("deepseek", "deepseek-reasoner")
        assert third is not first
        assert len(FakeChatProvider.instances) == 2

    async def test_unknown_source_returns_none(self):
        factory = self.make_factory()
        assert await factory.get_chat_provider("no-such-source", "m") is None
        assert await factory.get_chat_provider("", "m") is None
        assert self.pm.dynamic_import_calls == []

    async def test_env_style_key_is_resolved(self):
        source = _source(key="$OY_TEST_PROVIDER_KEY")
        factory = self.make_factory(sources=[source], providers=[])
        with mock.patch.dict(
            os.environ, {"OY_TEST_PROVIDER_KEY": "resolved-secret"}, clear=False
        ):
            provider = await factory.get_chat_provider("deepseek", "deepseek-chat")
        assert provider is not None
        assert provider.provider_config["key"] == "resolved-secret"

    async def test_failed_build_returns_none(self):
        source = _source()
        source["type"] = ""  # no adapter type -> build must fail gracefully
        factory = self.make_factory(sources=[source], providers=[])
        assert await factory.get_chat_provider("deepseek", "deepseek-chat") is None


class TestRegisterMainAndDispose(ProviderFactoryTestBase):
    async def test_register_main_writes_inst_map_without_polluting_provider_list(self):
        factory = self.make_factory()
        provider = await factory.get_chat_provider("deepseek", "deepseek-chat")
        registered = factory.register_main(provider)
        assert registered == "onlyyou::main"
        assert self.pm.inst_map["onlyyou::main"] is provider
        assert self.pm.provider_insts == []
        assert factory.find_loaded_provider_id(provider) == "onlyyou::chat::deepseek::deepseek-chat"
        assert factory.register_main(None) == ""

    async def test_unregister_main_removes_mapping(self):
        factory = self.make_factory()
        provider = await factory.get_chat_provider("deepseek", "deepseek-chat")
        factory.register_main(provider)
        factory.unregister_main()
        assert "onlyyou::main" not in self.pm.inst_map
        # unregistering must not terminate / evict the cached instance
        assert provider.terminated is False

    async def test_dispose_terminates_owned_and_clears_caches_only(self):
        factory = self.make_factory()
        reused = FakeChatProvider(
            {
                "id": "deepseek/deepseek-chat",
                "provider_source_id": "deepseek",
                "model": "deepseek-chat",
                "type": "openai_chat_completion",
                "provider_type": "chat_completion",
            }
        )
        self.pm.add_loaded("deepseek/deepseek-chat", reused)
        reused_provider = await factory.get_chat_provider("deepseek", "deepseek-chat")
        owned = await factory.get_chat_provider("ailiyun", "kimi-k2.5")
        factory.register_main(owned)

        await factory.dispose()

        assert owned.terminated is True
        assert reused_provider is reused
        assert reused.terminated is False
        assert "onlyyou::main" not in self.pm.inst_map
        assert factory._chat_cache == {}
        assert factory._tts_cache == {}
        assert factory._owned_instances == []
        # after dispose a new call rebuilds a fresh instance
        rebuilt = await factory.get_chat_provider("ailiyun", "kimi-k2.5")
        assert rebuilt is not owned


class TestFindSourceModelByProviderId(ProviderFactoryTestBase):
    async def test_reverse_lookup_from_loaded_instance(self):
        factory = self.make_factory()
        loaded = FakeChatProvider(
            {
                "id": "deepseek/deepseek-chat",
                "provider_source_id": "deepseek",
                "model": "deepseek-chat",
                "type": "openai_chat_completion",
                "provider_type": "chat_completion",
            }
        )
        self.pm.add_loaded("deepseek/deepseek-chat", loaded)
        assert await factory.find_source_model_by_provider_id(
            "deepseek/deepseek-chat"
        ) == ("deepseek", "deepseek-chat")

    async def test_reverse_lookup_from_static_provider_entry(self):
        factory = self.make_factory()
        assert await factory.find_source_model_by_provider_id(
            "ailiyun/kimi-k2.5"
        ) == ("ailiyun", "kimi-k2.5")

    async def test_reverse_lookup_unknown_and_plugin_ids(self):
        factory = self.make_factory()
        assert await factory.find_source_model_by_provider_id("") is None
        assert await factory.find_source_model_by_provider_id("unknown/id") is None
        assert await factory.find_source_model_by_provider_id("onlyyou::main") is None


class TestTtsProvider(ProviderFactoryTestBase):
    async def test_mode_off_returns_none(self):
        factory = self.make_factory()
        assert await factory.get_tts_provider({"mode": "off"}) is None

    async def test_mode_source_builds_openai_tts_api(self):
        factory = self.make_factory()
        provider = await factory.get_tts_provider(
            {
                "mode": "source",
                "source_id": "edge-tts-source",
                "model": "tts-1",
                "voice": "alloy",
            }
        )
        assert provider is not None
        assert provider.initialized is True
        assert provider.provider_config["id"] == "onlyyou::tts"
        assert provider.provider_config["type"] == "openai_tts_api"
        assert provider.provider_config["provider_type"] == "text_to_speech"
        assert provider.provider_config["api_key"] == "sk-tts-secret"
        assert provider.provider_config["key"] == "sk-tts-secret"
        assert provider.provider_config["openai-tts-voice"] == "alloy"
        assert self.pm.dynamic_import_calls == ["openai_tts_api"]
        assert factory.is_owned_instance(provider) is True
        # cached by (source, model, voice)
        assert (
            await factory.get_tts_provider(
                {
                    "mode": "source",
                    "source_id": "edge-tts-source",
                    "model": "tts-1",
                    "voice": "alloy",
                }
            )
            is provider
        )

    async def test_mode_source_missing_config_returns_none(self):
        factory = self.make_factory()
        assert (
            await factory.get_tts_provider(
                {"mode": "source", "source_id": "", "model": ""}
            )
            is None
        )
        assert (
            await factory.get_tts_provider(
                {"mode": "source", "source_id": "nope", "model": "tts-1"}
            )
            is None
        )

    async def test_mode_provider_reuses_context_tts_instance(self):
        factory = self.make_factory()
        tts_instance = FakeTTSProvider({"id": "edge-enabled", "model": "edge-tts"})
        self.context.providers_by_id["edge-enabled"] = tts_instance
        provider = await factory.get_tts_provider(
            {"mode": "provider", "provider_id": "edge-enabled"}
        )
        assert provider is tts_instance
        assert factory.is_owned_instance(provider) is False

    async def test_mode_provider_rejects_non_tts_instance(self):
        factory = self.make_factory()
        chat_instance = FakeChatProvider({"id": "plain-chat", "model": "m"})
        self.context.providers_by_id["plain-chat"] = chat_instance
        assert (
            await factory.get_tts_provider(
                {"mode": "provider", "provider_id": "plain-chat"}
            )
            is None
        )

    async def test_tts_provider_audio_file(self):
        factory = self.make_factory()
        provider = await factory.get_tts_provider(
            {
                "mode": "source",
                "source_id": "edge-tts-source",
                "model": "tts-1",
                "voice": "",
            }
        )
        path = await provider.get_audio("你好呀")
        assert os.path.isfile(path)


if __name__ == "__main__":
    unittest.main()
