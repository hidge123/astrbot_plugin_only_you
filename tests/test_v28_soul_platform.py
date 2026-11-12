"""Offline unit tests for the v2.8 Soul/platform/model refactor (Lead scope).

Covers:
- ``SoulProfile`` v2.8 fields (model_config / bound_platforms / settings / updated_at)
  and ``souls/<soul_id>.json`` file lifecycle (save/list/get/rename/delete + legacy
  SQLite-only migration);
- bot-instance (``platform_id``) bindings: one-to-many Soul -> platforms, strict
  mutual exclusion, unbind, legacy UMO-binding migration, platform-first routing;
- ``ModelHub`` per-Soul config (attach_soul_manager, isolation, per-Soul main
  provider ids ``onlyyou::main::<soul_id>``, legacy global migration, unattached
  legacy path compatibility);
- ``PlatformBridge.list_platform_instances`` / ``extract_platform_id`` merge;
- ``SessionBuffer.clear_platform_memory`` / ``clear_session_memory``;
- ``ProviderFactory`` multi-main registration;
- ``main.py`` v2.8 runtime helpers: ``clear_platform_memory_state``,
  ``_maybe_run_scheduled_gc`` (persisted date) and ``_maybe_share_surfed_video`` gates.
"""

import json
import os
import sys
import time
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _helpers import (  # noqa: E402
    DEFAULT_FAKE_PROVIDERS,
    DEFAULT_FAKE_SOURCES,
    DatabaseAsyncTestCase,
    FakeContext,
    FakeProviderManager,
    build_fake_plugin,
    card_with,
    load_plugin_modules,
    make_plugin_config,
    make_soul,
    make_user,
    temp_db,
)

mods = load_plugin_modules()

import OnlyYou.core.atmosphere_reader as _atmosphere_reader  # noqa: E402

mods.atmosphere_reader = _atmosphere_reader  # not preloaded by load_plugin_modules()

SoulProfile = mods.soul_manager.SoulProfile
SoulManager = mods.soul_manager.SoulManager
DEFAULT_SOUL_ID = mods.soul_manager.DEFAULT_SOUL_ID


def _ctx_with_providers(audio_dir):
    manager = FakeProviderManager(
        sources=DEFAULT_FAKE_SOURCES,
        providers=DEFAULT_FAKE_PROVIDERS,
        audio_dir=audio_dir,
    )
    return FakeContext(manager), manager


class TestSoulProfileV28(unittest.TestCase):
    def test_new_fields_and_defaults(self):
        soul = SoulProfile(soul_id="s1", card=card_with(identity={"name": "星野"}))
        assert soul.bound_platforms == []
        assert soul.settings == {"surfing_enabled": True}
        assert soul.surfing_enabled is True
        assert soul.model_config["main"] == {"source_id": "", "model": ""}
        assert soul.model_config["tts"]["mode"] == "off"
        assert soul.updated_at == 0
        assert soul.tts_config["mode"] == "off"

    def test_bound_platforms_normalization(self):
        soul = SoulProfile(
            soul_id="s1",
            bound_platforms=[
                "aiocqhttp",
                " aiocqhttp ",
                "",
                "telegram:GroupMessage:1",  # UMO is illegal here
                "x" * 100,  # too long
                123,
            ],
        )
        assert soul.bound_platforms == ["aiocqhttp"]

    def test_settings_normalization(self):
        soul = SoulProfile(soul_id="s1", settings={"surfing_enabled": "false", "extra": 7})
        assert soul.surfing_enabled is False
        assert soul.settings["extra"] == 7
        soul.surfing_enabled = "true"
        assert soul.surfing_enabled is True
        # unknown keys survive the round trip
        assert soul.settings["extra"] == 7

    def test_surfing_sources_default_normalize_and_setter(self):
        # v2.8.3: absent key keeps both sources enabled (old Soul JSON compatible)
        soul = SoulProfile(soul_id="s1")
        assert soul.surfing_sources == ["bilibili", "douyin"]
        assert "surfing_sources" not in soul.settings
        # stored value is normalized (dedupe / unknown drop / canonical order)
        soul.settings = {"surfing_sources": ["douyin", "bilibili", "x"]}
        assert soul.surfing_sources == ["bilibili", "douyin"]
        # setter persists a normalized list and keeps other settings
        soul.surfing_sources = [" douyin ", "douyin"]
        assert soul.surfing_sources == ["douyin"]
        assert soul.settings["surfing_sources"] == ["douyin"]
        # explicit empty list means "no background source" (manual trigger still works)
        soul.surfing_sources = []
        assert soul.surfing_sources == []

    def test_model_config_normalization(self):
        soul = SoulProfile(
            soul_id="s1",
            model_config={
                "main": {"source_id": "deepseek", "model": "deepseek-chat"},
                "decision": {"inherit": False, "source_id": "", "model": ""},
            },
        )
        assert soul.model_config["main"]["model"] == "deepseek-chat"
        assert soul.model_config["decision"]["inherit"] is False
        assert soul.model_config["decision"]["inherit_from"] == ""

    def test_to_dict_from_dict_roundtrip(self):
        soul = SoulProfile(
            soul_id="酒狐",
            card=card_with(identity={"name": "酒狐"}),
            model_config={"main": {"source_id": "deepseek", "model": "deepseek-chat"}},
            bound_platforms=["aiocqhttp", "telegram_bot_1"],
            settings={"surfing_enabled": False},
            updated_at=1762840000,
        )
        data = soul.to_dict()
        for key in ("model_config", "bound_platforms", "settings", "updated_at"):
            assert key in data, key
        restored = SoulProfile.from_dict(data)
        assert restored.soul_id == "酒狐"
        assert restored.bound_platforms == ["aiocqhttp", "telegram_bot_1"]
        assert restored.surfing_enabled is False
        assert restored.updated_at == 1762840000
        assert restored.model_config["main"]["model"] == "deepseek-chat"

    def test_old_soul_dict_without_new_keys_is_tolerated(self):
        old_payload = {
            "soul_id": "old",
            "card": card_with(identity={"name": "旧角色"}),
            "state_event_cards": [],
        }
        restored = SoulProfile.from_dict(old_payload)
        assert restored.bound_platforms == []
        assert restored.surfing_enabled is True
        assert restored.model_config["main"]["source_id"] == ""

    def test_soul_filename_is_safe(self):
        assert SoulManager._derive_souls_dir is not None
        # path separators / illegal chars are sanitized but still deterministic
        fname = mods.soul_manager._soul_filename("a/b:c*?")
        assert "/" not in fname and ":" not in fname and fname.endswith(".json")
        assert mods.soul_manager._soul_filename("酒狐") == "酒狐.json"
        assert mods.soul_manager._soul_filename("soul_a") == "soul_a.json"


class SoulStoreTestBase(DatabaseAsyncTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        context, self.provider_manager = _ctx_with_providers(self.tmpdir)
        self.context = context
        config = make_plugin_config()
        self.factory = mods.provider_factory.ProviderFactory(context)
        self.hub = mods.model_hub.ModelHub(
            context=context, plugin_config=config, db=self.db, provider_factory=self.factory
        )
        await self.hub.load()
        self.soul_manager = SoulManager(context, self.db, self.hub)
        self.hub.attach_soul_manager(self.soul_manager)

    def json_path(self, soul_id, manager=None):
        return (manager or self.soul_manager).soul_file_path(soul_id)


class TestSoulJsonStore(SoulStoreTestBase):
    async def test_save_soul_writes_independent_json_file(self):
        soul = make_soul("酒狐", card=card_with(identity={"name": "酒狐"}))
        soul.bound_platforms = ["aiocqhttp"]
        soul.settings = {"surfing_enabled": False}
        soul.model_config = {
            "main": {"source_id": "deepseek", "model": "deepseek-chat"}
        }
        await self.soul_manager.save_soul(soul)
        path = self.json_path("酒狐")
        assert path == os.path.join(self.soul_manager.souls_dir, "酒狐.json")
        assert os.path.exists(path)
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
        assert raw["soul_id"] == "酒狐"
        assert raw["bound_platforms"] == ["aiocqhttp"]
        assert raw["settings"]["surfing_enabled"] is False
        assert raw["model_config"]["main"]["model"] == "deepseek-chat"
        assert isinstance(raw["updated_at"], int) and raw["updated_at"] > 0
        assert raw["card"]["identity"]["name"] == "酒狐"
        assert "state_event_cards" in raw

    async def test_get_soul_by_id_from_json_after_cache_reset(self):
        soul = make_soul("s2", card=card_with(identity={"name": "二狐"}))
        soul.settings = {"surfing_enabled": False}
        await self.soul_manager.save_soul(soul)
        # a brand-new manager must load the Soul from its JSON file
        fresh = SoulManager(self.context, self.db, self.hub)
        loaded = await fresh.get_soul_by_id("s2")
        assert loaded is not None
        assert loaded.name == "二狐"
        assert loaded.surfing_enabled is False

    async def test_list_souls_reads_json_files(self):
        await self.soul_manager.save_soul(make_soul("a1", card=card_with(identity={"name": "A"})))
        await self.soul_manager.save_soul(make_soul("b1", card=card_with(identity={"name": "B"})))
        fresh = SoulManager(self.context, self.db, self.hub)
        souls = await fresh.list_souls()
        ids = {soul.soul_id for soul in souls}
        assert {"a1", "b1"}.issubset(ids)

    async def test_legacy_sqlite_only_row_is_migrated_to_json(self):
        card = card_with(identity={"name": "旧库角色"}, speech={"speaking_style": "短句"})
        payload = {
            "soul_id": "legacy_soul",
            "card": card,
            "state_event_cards": [],
        }
        await self.db.execute(
            "INSERT INTO souls (soul_id, persona_hash, profile_json, updated_at) VALUES (?,?,?,?)",
            ("legacy_soul", "legacy_hash", json.dumps(payload, ensure_ascii=False), 12345),
        )
        fresh = SoulManager(self.context, self.db, self.hub)
        souls = await fresh.list_souls()
        legacy = next((soul for soul in souls if soul.soul_id == "legacy_soul"), None)
        assert legacy is not None
        assert os.path.exists(fresh.soul_file_path("legacy_soul"))
        assert legacy.settings == {"surfing_enabled": True}
        assert legacy.bound_platforms == []
        assert legacy.model_config["main"]["source_id"] == ""

    async def test_rename_soul_renames_file_and_keeps_data(self):
        soul = make_soul("old_id", card=card_with(identity={"name": "旧名"}))
        soul.bound_platforms = ["aiocqhttp"]
        await self.soul_manager.save_soul(soul)
        await self.soul_manager.set_default_soul_id("old_id")
        renamed = await self.soul_manager.rename_soul("old_id", "new_id")
        assert renamed.soul_id == "new_id"
        assert renamed.bound_platforms == ["aiocqhttp"]
        assert not os.path.exists(self.json_path("old_id"))
        assert os.path.exists(self.json_path("new_id"))
        assert await self.soul_manager.get_default_soul_id() == "new_id"
        assert await self.soul_manager.get_soul_by_id("old_id") is None

    async def test_delete_soul_removes_file_and_cascades(self):
        await self.soul_manager.resolve_soul(allow_llm_seed=False)  # ensure default soul
        soul = make_soul("doomed", card=card_with(identity={"name": "删除我"}))
        await self.soul_manager.save_soul(soul)
        await self.soul_manager.set_default_soul_id("doomed")
        await self.soul_manager.delete_soul("doomed")
        assert not os.path.exists(self.json_path("doomed"))
        assert await self.soul_manager.get_soul_by_id("doomed") is None
        assert await self.soul_manager.get_default_soul_id() == ""

    async def test_default_soul_materialized_on_empty_store(self):
        soul = await self.soul_manager.resolve_soul(allow_llm_seed=False)
        assert soul.soul_id == DEFAULT_SOUL_ID
        assert os.path.exists(self.json_path(DEFAULT_SOUL_ID))
        assert await self.soul_manager.get_default_soul_id() == DEFAULT_SOUL_ID


class TestPlatformBindings(SoulStoreTestBase):
    async def test_bind_unbind_roundtrip(self):
        soul = make_soul("soul_p", card=card_with(identity={"name": "绑定狐"}))
        await self.soul_manager.save_soul(soul)
        await self.soul_manager.bind_platform("soul_p", "aiocqhttp")
        await self.soul_manager.bind_platform("soul_p", "telegram_bot_1")
        assert await self.soul_manager.get_bound_platforms("soul_p") == [
            "aiocqhttp",
            "telegram_bot_1",
        ]
        bindings = await self.soul_manager.get_platform_bindings()
        assert bindings == {"aiocqhttp": "soul_p", "telegram_bot_1": "soul_p"}
        owner = await self.soul_manager.unbind_platform("telegram_bot_1")
        assert owner == "soul_p"
        assert await self.soul_manager.get_platform_bindings() == {"aiocqhttp": "soul_p"}
        assert await self.soul_manager.unbind_platform("unknown") == ""

    async def test_mutual_exclusion_blocks_second_soul(self):
        await self.soul_manager.save_soul(
            make_soul("soul_a", card=card_with(identity={"name": "A 狐"}))
        )
        await self.soul_manager.save_soul(
            make_soul("soul_b", card=card_with(identity={"name": "B 狐"}))
        )
        await self.soul_manager.bind_platform("soul_a", "aiocqhttp")
        with self.assertRaises(ValueError) as ctx:
            await self.soul_manager.bind_platform("soul_b", "aiocqhttp")
        message = str(ctx.exception)
        assert "该机器人接口已被角色『A 狐』绑定" in message
        assert "请先在角色 A 狐 下解绑" in message
        # the failed bind left both Souls untouched
        assert await self.soul_manager.get_bound_platforms("soul_b") == []

    async def test_bind_validates_platform_id(self):
        await self.soul_manager.save_soul(make_soul("soul_v"))
        with self.assertRaises(ValueError):
            await self.soul_manager.bind_platform("soul_v", "")
        with self.assertRaises(ValueError):
            await self.soul_manager.bind_platform("soul_v", "aiocqhttp:GroupMessage:1")
        with self.assertRaises(ValueError):
            await self.soul_manager.bind_platform("soul_v", "x" * 100)
        with self.assertRaises(ValueError):
            await self.soul_manager.bind_platform("no-such-soul", "aiocqhttp")

    async def test_platform_first_routing(self):
        default_soul = await self.soul_manager.resolve_soul(allow_llm_seed=False)
        bound = make_soul("bound_soul", card=card_with(identity={"name": "绑定狐"}))
        await self.soul_manager.save_soul(bound)
        await self.soul_manager.bind_platform("bound_soul", "aiocqhttp")
        routed = await self.soul_manager.resolve_soul(umo="aiocqhttp:GroupMessage:123")
        assert routed.soul_id == "bound_soul"
        # unknown platform falls back to the plugin default Soul
        other = await self.soul_manager.resolve_soul(umo="telegram:GroupMessage:1")
        assert other.soul_id == default_soul.soul_id

    async def test_legacy_umo_bindings_migrated_once(self):
        soul = make_soul("legacy_target", card=card_with(identity={"name": "旧绑定"}))
        await self.soul_manager.save_soul(soul)
        await self.soul_manager.set_umo_binding(
            "qq:GroupMessage:9", "legacy_target"
        )
        migrated = await self.soul_manager.migrate_legacy_umo_bindings()
        assert migrated == 1
        assert (await self.soul_manager.get_platform_bindings())["qq"] == "legacy_target"
        # idempotent (one-shot flag persisted in plugin_settings)
        assert await self.soul_manager.migrate_legacy_umo_bindings() == 0

    async def test_set_soul_setting_persists_json(self):
        soul = make_soul("setting_soul")
        await self.soul_manager.save_soul(soul)
        updated = await self.soul_manager.set_soul_setting(
            "setting_soul", "surfing_enabled", False
        )
        assert updated.surfing_enabled is False
        fresh = SoulManager(self.context, self.db, self.hub)
        loaded = await fresh.get_soul_by_id("setting_soul")
        assert loaded.surfing_enabled is False


class TestModelHubPerSoul(SoulStoreTestBase):
    async def test_per_soul_config_isolation(self):
        soul_a = await self.soul_manager.resolve_soul(allow_llm_seed=False)
        await self.soul_manager.save_soul(make_soul("soul_b"))
        cfg_a = {
            "main": {"source_id": "deepseek", "model": "deepseek-chat"},
            "tts": {"mode": "off"},
        }
        cfg_b = {
            "main": {"source_id": "ailiyun", "model": "kimi-k2.5"},
            "tts": {"mode": "provider", "provider_id": "edge-tts"},
        }
        await self.hub.save_config(cfg_a, soul_id=soul_a.soul_id)
        await self.hub.save_config(cfg_b, soul_id="soul_b")
        config_a = await self.hub.get_config(soul_a.soul_id)
        config_b = await self.hub.get_config("soul_b")
        assert config_a["main"]["model"] == "deepseek-chat"
        assert config_b["main"]["model"] == "kimi-k2.5"
        assert config_a["tts"]["mode"] == "off"
        assert config_b["tts"]["mode"] == "provider"
        assert await self.hub.resolve_role("main", soul_id="soul_b") == {
            "source_id": "ailiyun",
            "model": "kimi-k2.5",
        }
        # persisted inside the Soul's own JSON file
        fresh = SoulManager(self.context, self.db, self.hub)
        loaded = await fresh.get_soul_by_id("soul_b")
        assert loaded.model_config["main"]["model"] == "kimi-k2.5"
        # legacy global key is untouched when a SoulManager is attached
        assert await self.db.get_setting("model_config", None) in (None, {})

    async def test_get_tts_config_per_soul(self):
        await self.soul_manager.resolve_soul(allow_llm_seed=False)
        await self.soul_manager.save_soul(make_soul("tts_a"))
        await self.soul_manager.save_soul(make_soul("tts_b"))
        await self.hub.save_config(
            {"tts": {"mode": "provider", "provider_id": "edge-tts"}}, soul_id="tts_a"
        )
        await self.hub.save_config({"tts": {"mode": "off"}}, soul_id="tts_b")
        assert self.hub.get_tts_config("tts_a")["mode"] == "provider"
        assert self.hub.get_tts_config("tts_b")["mode"] == "off"

    async def test_main_provider_id_registered_per_soul(self):
        soul_a = await self.soul_manager.resolve_soul(allow_llm_seed=False)
        await self.soul_manager.save_soul(make_soul("soul_b"))
        await self.hub.save_config(
            {"main": {"source_id": "deepseek", "model": "deepseek-chat"}},
            soul_id=soul_a.soul_id,
        )
        await self.hub.save_config(
            {"main": {"source_id": "ailiyun", "model": "kimi-k2.5"}},
            soul_id="soul_b",
        )
        pid_a = await self.hub.get_main_provider_id(soul_a.soul_id)
        pid_b = await self.hub.get_main_provider_id("soul_b")
        assert pid_a == f"onlyyou::main::{soul_a.soul_id}"
        assert pid_b == "onlyyou::main::soul_b"
        assert pid_a in self.provider_manager.inst_map
        assert pid_b in self.provider_manager.inst_map
        # saving one Soul's config only unregisters that Soul's provider id
        await self.hub.save_config(
            {"main": {"source_id": "ailiyun", "model": "kimi-k2.5"}},
            soul_id="soul_b",
        )
        assert pid_a in self.provider_manager.inst_map
        assert pid_b not in self.provider_manager.inst_map

    async def test_status_is_per_soul_and_keeps_four_roles(self):
        soul_a = await self.soul_manager.resolve_soul(allow_llm_seed=False)
        await self.soul_manager.save_soul(make_soul("soul_b"))
        await self.hub.save_config(
            {"main": {"source_id": "deepseek", "model": "deepseek-chat"}},
            soul_id=soul_a.soul_id,
        )
        status_a = await self.hub.get_status(soul_a.soul_id)
        status_b = await self.hub.get_status("soul_b")
        assert set(status_a) == {"main", "decision", "vision", "tts"}
        assert status_a["main"]["configured"] is True
        assert status_a["main"]["provider_id"] == f"onlyyou::main::{soul_a.soul_id}"
        assert status_b["main"]["configured"] is False

    async def test_legacy_global_config_migrated_into_empty_souls(self):
        await self.soul_manager.resolve_soul(allow_llm_seed=False)
        await self.soul_manager.save_soul(make_soul("soul_b"))
        global_cfg = {"main": {"source_id": "deepseek", "model": "deepseek-chat"}}
        await self.db.set_setting("model_config", global_cfg)
        fresh_hub = mods.model_hub.ModelHub(
            context=self.context,
            plugin_config=make_plugin_config(),
            db=self.db,
            provider_factory=self.factory,
        )
        fresh_hub.attach_soul_manager(self.soul_manager)
        await fresh_hub.load()
        config = await fresh_hub.get_config(DEFAULT_SOUL_ID)
        assert config["main"]["model"] == "deepseek-chat"
        migrated = await self.soul_manager.get_soul_by_id(DEFAULT_SOUL_ID)
        assert migrated.model_config["main"]["model"] == "deepseek-chat"
        # a non-empty Soul config is never overwritten by the migration
        await self.soul_manager.save_soul(
            make_soul("own_config"), persona_hash="webui_custom"
        )
        soul = await self.soul_manager.get_soul_by_id("own_config")
        soul.model_config = {"main": {"source_id": "ailiyun", "model": "kimi-k2.5"}}
        await self.soul_manager.save_soul(soul)
        await self.db.set_setting("model_config_migrated_to_souls", False)
        await fresh_hub.load()
        kept = await self.soul_manager.get_soul_by_id("own_config")
        assert kept.model_config["main"]["model"] == "kimi-k2.5"

    async def test_unattached_hub_keeps_legacy_global_behaviour(self):
        legacy_hub = mods.model_hub.ModelHub(
            context=self.context,
            plugin_config=make_plugin_config(),
            db=self.db,
            provider_factory=self.factory,
        )
        await legacy_hub.load()
        await legacy_hub.save_model_config(
            {"main": {"source_id": "deepseek", "model": "deepseek-chat"}}
        )
        stored = await self.db.get_setting("model_config", None)
        assert stored["main"]["model"] == "deepseek-chat"
        assert legacy_hub.get_model_config()["main"]["model"] == "deepseek-chat"


class TestPlatformBridgeDiscovery(unittest.TestCase):
    class _Meta:
        """Mirror AstrBot ``PlatformMetadata``: name=adapter type, id=instance id."""

        def __init__(self, pid, adapter_type="aiocqhttp", display_name=""):
            self.id = pid
            self.name = adapter_type
            self.adapter_display_name = display_name or None

    class _Inst:
        def __init__(self, meta):
            self._meta = meta

        def meta(self):
            return self._meta

    def _bridge(self, context):
        return mods.platform_bridge.PlatformBridge(context)

    def test_extract_platform_id(self):
        extract = mods.platform_bridge.PlatformBridge.extract_platform_id
        assert extract("aiocqhttp:GroupMessage:123") == "aiocqhttp"
        assert extract("telegram") == "telegram"
        assert extract("") == ""
        assert extract(None) == ""

    def test_list_merges_runtime_config_and_observed(self):
        context = FakeContext()
        context.config = {
            "platform": [
                {"id": "tg_bot", "type": "telegram", "enable": True},
                {"id": "disabled_bot", "type": "aiocqhttp", "enable": False},
            ]
        }
        context.platform_manager = SimpleNamespace(
            get_insts=lambda: [
                self._Inst(self._Meta("aiocqhttp")),
                self._Inst(self._Meta("tg_bot", "telegram", "TG 机器人")),
            ]
        )
        items = self._bridge(context).list_platform_instances(
            observed_umos=["aiocqhttp:GroupMessage:1", "lark:GroupMessage:2"]
        )
        by_id = {item["platform_id"]: item for item in items}
        assert set(by_id) == {"aiocqhttp", "tg_bot", "disabled_bot", "lark"}
        assert by_id["aiocqhttp"]["running"] is True
        assert by_id["aiocqhttp"]["platform_type"] == "aiocqhttp"
        assert by_id["aiocqhttp"]["name"] == "aiocqhttp"  # fallback: platform id
        assert by_id["disabled_bot"]["enabled"] is False
        assert by_id["lark"]["source"] == "observed"
        # AstrBot PlatformMetadata.name is the adapter type, adapter_display_name
        # is the UI label (id "tg_bot" -> type telegram, display "TG 机器人").
        assert by_id["tg_bot"]["platform_type"] == "telegram"
        assert by_id["tg_bot"]["name"] == "TG 机器人"

    def test_list_never_raises_with_broken_context(self):
        class Broken:
            @property
            def platform_manager(self):
                raise RuntimeError("boom")

            def get_config(self):
                raise RuntimeError("boom")

        items = self._bridge(Broken()).list_platform_instances(
            observed_umos=["aiocqhttp:GroupMessage:1"]
        )
        assert [item["platform_id"] for item in items] == ["aiocqhttp"]


class TestSessionBufferPlatformMemory(unittest.TestCase):
    def _buffer(self):
        config = make_plugin_config()
        buffer = mods.context_budget.SessionBuffer(config)
        user = make_user("u-1", "阿一")
        for umo in (
            "aiocqhttp:GroupMessage:1",
            "aiocqhttp:FriendMessage:2",
            "telegram:GroupMessage:3",
        ):
            buffer.record_user_message(umo, user, "消息")
            buffer.record_assistant_message(umo, "回复")
            message = buffer.record_user_message(umo, user, "未读")
            buffer.mark_unread(umo, message, is_private="Friend" in umo)
        return buffer

    def test_clear_platform_memory_only_touches_that_platform(self):
        buffer = self._buffer()
        stats = buffer.clear_platform_memory("aiocqhttp")
        assert stats["windows"] == 2
        assert stats["unread"] == 2
        assert set(buffer.list_window_umos()) == {"telegram:GroupMessage:3"}
        assert buffer.get_unread_count() == 1
        # clearing is idempotent
        again = buffer.clear_platform_memory("aiocqhttp")
        assert again == {"windows": 0, "evicted": 0, "unread": 0, "messages": 0}
        assert buffer.clear_platform_memory("") == {
            "windows": 0,
            "evicted": 0,
            "unread": 0,
            "messages": 0,
        }

    def test_clear_session_memory(self):
        buffer = self._buffer()
        stats = buffer.clear_session_memory("aiocqhttp:GroupMessage:1")
        assert stats["window"] >= 2
        assert stats["unread"] == 1
        assert "aiocqhttp:GroupMessage:1" not in buffer.list_window_umos()
        assert "aiocqhttp:FriendMessage:2" in buffer.list_window_umos()


class TestProviderFactoryMultiMain(DatabaseAsyncTestCase):
    async def test_register_and_unregister_multiple_main_ids(self):
        context, manager = _ctx_with_providers(self.tmpdir)
        factory = mods.provider_factory.ProviderFactory(context)

        class Instance:
            pass

        first = Instance()
        second = Instance()
        pid_first = factory.register_main(first, "onlyyou::main::soul_a")
        pid_second = factory.register_main(second, "onlyyou::main::soul_b")
        assert pid_first == "onlyyou::main::soul_a"
        assert pid_second == "onlyyou::main::soul_b"
        assert set(manager.inst_map) == {pid_first, pid_second}
        factory.unregister_main("onlyyou::main::soul_a")
        assert pid_first not in manager.inst_map
        assert pid_second in manager.inst_map
        factory.unregister_main()
        assert not manager.inst_map
        # short ids are namespaced automatically
        assert factory.register_main(first, "soul_x") == "onlyyou::main::soul_x"
        await factory.dispose()


class TestMainRuntimeHelpers(DatabaseAsyncTestCase):
    """Unbound ``OnlyYouPlugin`` methods against a lightweight fake ``self``."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.config = make_plugin_config(
            surfing_config={
                "surf_share_min_score": 80,
                "surf_share_cooldown_minutes": 90,
            },
            roguelite_schedule_config={
                "enable_roguelite_schedule": True,
                "max_mutations_per_day": 2,
                "mutation_cooldown_minutes": 120,
                "idle_roam_cooldown_minutes": 45,
                "allow_l3_wake_from_resting": True,
            },
        )
        self.memory_manager = mods.memory_manager.MemoryManager(
            self.db, mods.model_hub.ModelHub(context=FakeContext(), plugin_config=self.config), self.config
        )
        self.session_buffer = mods.context_budget.SessionBuffer(self.config)
        self.bridge = _RecordingBridge()
        self.sent_reviews = []

    def _bind(self, fake):
        from OnlyYou.main import OnlyYouPlugin

        fake._idle_roam_cooldown_seconds = OnlyYouPlugin._idle_roam_cooldown_seconds.__get__(fake)
        fake._latest_l3_umo = OnlyYouPlugin._latest_l3_umo.__get__(fake)
        # staticmethod: keep it a plain function (no `self` binding)
        fake._render_soul_summary = OnlyYouPlugin._render_soul_summary
        fake._maybe_synthesize_proactive = (
            OnlyYouPlugin._maybe_synthesize_proactive.__get__(fake)
        )
        return fake

    async def test_clear_platform_memory_state(self):
        user = make_user("u-1", "阿一")
        for umo in ("aiocqhttp:GroupMessage:1", "telegram:GroupMessage:2"):
            message = self.session_buffer.record_user_message(umo, user, "在吗")
            self.session_buffer.mark_unread(umo, message, is_private=False)
        fake = self._bind(
            SimpleNamespace(
                session_buffer=self.session_buffer,
                _known_group_umos={"aiocqhttp:GroupMessage:1", "telegram:GroupMessage:2"},
                _known_l3_umos={
                    "uid-1": "aiocqhttp:FriendMessage:9",
                    "uid-2": "telegram:FriendMessage:8",
                },
                _last_group_lurk_ts={"aiocqhttp:GroupMessage:1": 1.0, "telegram:GroupMessage:2": 2.0},
                _last_private_catchup_ts={"aiocqhttp:FriendMessage:9": 1.0},
            )
        )
        from OnlyYou.main import OnlyYouPlugin

        stats = await OnlyYouPlugin.clear_platform_memory_state(fake, "aiocqhttp")
        assert stats["windows"] == 1
        assert stats["unread"] == 1
        assert stats["groups"] == 1
        assert stats["l3"] == 1
        assert fake._known_group_umos == {"telegram:GroupMessage:2"}
        assert fake._known_l3_umos == {"uid-2": "telegram:FriendMessage:8"}
        assert fake._last_group_lurk_ts == {"telegram:GroupMessage:2": 2.0}
        assert fake._last_private_catchup_ts == {}
        assert await OnlyYouPlugin.clear_platform_memory_state(fake, "") == {
            "windows": 0,
            "evicted": 0,
            "unread": 0,
            "messages": 0,
            "groups": 0,
            "l3": 0,
            "caches": 0,
        }

    async def test_maybe_run_scheduled_gc_persists_date(self):
        await self.memory_manager.add_memory(
            soul_id="s1", content="48 小时前的瞬时记忆", memory_tier=1
        )
        now = int(time.time())
        await self.db.execute(
            "UPDATE memories SET created_at = ? WHERE content = ?",
            (now - 172801, "48 小时前的瞬时记忆"),
        )
        fake = SimpleNamespace(db=self.db, memory_manager=self.memory_manager)
        from OnlyYou.main import OnlyYouPlugin

        stats = await OnlyYouPlugin._maybe_run_scheduled_gc(fake)
        assert stats["gc_ran"] == 1
        # realtime T1 trimming deletes the expired item before the daily GC sweep
        assert stats["deleted_expired"] == 1
        assert stats["deleted_tier1"] == 0
        today = time.strftime("%Y-%m-%d")
        assert await self.db.get_setting("last_forgetting_gc_date") == today
        # second call the same day does not run the heavy GC again
        assert (await OnlyYouPlugin._maybe_run_scheduled_gc(fake))["gc_ran"] == 0

    def _surf_fake(self, maps=None):
        maps = maps or {}
        messages = []

        async def call_text(prompt="", role="decision", umo=None):
            messages.append((umo, prompt))
            return "这个视频太搞笑了，必须分享给你哈哈哈"

        hub = SimpleNamespace(call_llm_text=call_text)
        fake = SimpleNamespace(
            config=self.config,
            model_hub=hub,
            memory_manager=self.memory_manager,
            session_buffer=self.session_buffer,
            platform_bridge=self.bridge,
            _known_l3_umos={"core": "aiocqhttp:FriendMessage:core"},
            _shared_video_ids=maps.get("_shared_video_ids", {}),
            _last_surf_share_ts=maps.get("_last_surf_share_ts", {}),
            _last_idle_roam_ts=maps.get("_last_idle_roam_ts", {}),
            _idle_roam_cooldown_seconds=lambda: 45 * 60,
        )
        return self._bind(fake), messages

    @staticmethod
    def _review(video_id="v1", score=90, is_fallback=False):
        video = SimpleNamespace(
            video_id=video_id,
            title="测试视频",
            share_url="https://b23.tv/v1",
            platform="bilibili",
        )
        return SimpleNamespace(
            video=video,
            score=score,
            commentary="太搞笑了",
            is_fallback=is_fallback,
        )

    async def test_share_gates(self):
        from OnlyYou.main import OnlyYouPlugin

        soul = make_soul("soul_surf")
        run = SimpleNamespace()
        current_slot = SimpleNamespace(title="刷视频")
        # 1) happy path
        fake, messages = self._surf_fake()
        assert await OnlyYouPlugin._maybe_share_surfed_video(
            fake, soul, run, current_slot, self._review()
        ) is True
        assert messages and messages[0][0] == "aiocqhttp:FriendMessage:core"
        assert fake._shared_video_ids["soul_surf"] == {"v1"}
        # 2) same video never shared twice
        assert await OnlyYouPlugin._maybe_share_surfed_video(
            fake, soul, run, current_slot, self._review()
        ) is False
        # 3) vision fallback is never shared
        fake2, _ = self._surf_fake()
        assert await OnlyYouPlugin._maybe_share_surfed_video(
            fake2, soul, run, current_slot, self._review("v2", score=95, is_fallback=True)
        ) is False
        # 4) score below surf_share_min_score
        fake3, _ = self._surf_fake()
        assert await OnlyYouPlugin._maybe_share_surfed_video(
            fake3, soul, run, current_slot, self._review("v3", score=70)
        ) is False
        # 5) surf-share cooldown
        fake4, _ = self._surf_fake(
            {"_last_surf_share_ts": {"soul_surf": time.time() - 60}}
        )
        assert await OnlyYouPlugin._maybe_share_surfed_video(
            fake4, soul, run, current_slot, self._review("v4")
        ) is False
        # 6) IDLE_ROAM cooldown recently talked
        fake5, _ = self._surf_fake(
            {"_last_idle_roam_ts": {"soul_surf": time.time() - 60}}
        )
        assert await OnlyYouPlugin._maybe_share_surfed_video(
            fake5, soul, run, current_slot, self._review("v5")
        ) is False
        # 7) no ★L3 target
        fake6, _ = self._surf_fake()
        fake6._known_l3_umos = {}
        assert await OnlyYouPlugin._maybe_share_surfed_video(
            fake6, soul, run, current_slot, self._review("v6")
        ) is False
        # 8) no review
        assert await OnlyYouPlugin._maybe_share_surfed_video(
            fake6, soul, run, current_slot, None
        ) is False


class _RecordingBridge:
    """Minimal proactive-send bridge used by the main.py runtime helper tests."""

    def __init__(self):
        self.sent = []
        self.ok = True

    async def send_proactive_message(
        self, umo="", text="", session_buffer=None, soul_name="Bot", audio_path=None
    ):
        if not self.ok:
            return False
        self.sent.append((umo, text))
        return True


if __name__ == "__main__":
    unittest.main()


# ==========================================================================
# v2.8.1 hotfix regression: "@bot only" group pings have an empty message_str
# (AstrBot folds At components out of it) and used to be dropped by
# ``on_any_message`` / ``AtmosphereReader.evaluate_should_reply``.
# ==========================================================================
class _AtOnlyGroupEvent:
    """Minimal AstrBot group event mirroring an "@bot" ping with no extra text."""

    def __init__(
        self,
        message_str="",
        umo="qq bot:GroupMessage:1039802291",
        at_bot=True,
        sender_id="2975066889",
        sender_name="hidge-1",
    ):
        self.message_str = message_str
        self.unified_msg_origin = umo
        self.is_at_or_wake_command = bool(at_bot)
        self.is_wake = bool(at_bot)
        self.call_llm = False
        self.message_obj = SimpleNamespace(message=[])
        self._sender_id = sender_id
        self._sender_name = sender_name

    def get_sender_id(self):
        return self._sender_id

    def get_sender_name(self):
        return self._sender_name

    def get_group_id(self):
        return "1039802291"

    def get_self_id(self):
        return "3297648235"

    def is_admin(self):
        return True

    def is_private_chat(self):
        return False

    def should_call_llm(self, value):
        self.call_llm = bool(value)

    def set_extra(self, key, value):
        self._extra = getattr(self, "_extra", {})
        self._extra[key] = value


class _StubHubWithProviderId(SimpleNamespace):
    async def get_main_provider_id(self, soul_id=None):
        return ""

    async def call_llm_json(
        self, prompt="", role="decision", umo=None, system_prompt="", image_urls=None, default=None
    ):
        return {"decision": "REPLY", "confidence": 0.9}


class _StubSoulManager:
    def __init__(self, soul):
        self._soul = soul

    async def resolve_soul(self, umo=None, allow_llm_seed=True):
        return self._soul


class _StubScheduleMutator:
    async def on_message_input(self, **kwargs):
        return False


class _StubDeckEngine:
    def __init__(self, slot):
        self._slot = slot

    async def get_current_slot(self, **kwargs):
        return self._slot


class TestAtOnlyGroupMessage(DatabaseAsyncTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        from test_regressions import bind_plugin_methods

        self._bind_plugin_methods = bind_plugin_methods
        self.config = make_plugin_config()
        self.soul = make_soul("酒狐", card=card_with(identity={"name": "酒狐"}))
        self.soul.bound_platforms = ["qq bot"]
        self.slot = mods.roguelite_deck.SlotInstance(
            slot_key="block_4",
            start_hour=18.0,
            end_hour=21.0,
            card_id="evt_idle",
            title="瘫着刷B站水群",
            state=mods.state_machine.BotState.IDLE,
            action_type=mods.state_machine.ActionType.IDLE_ROAM,
            armor=25,
        )
        self.session_buffer = mods.context_budget.SessionBuffer(self.config)
        self.platform_bridge = mods.platform_bridge.PlatformBridge(FakeContext())
        self.user_relation = mods.user_relation.UserRelationManager(
            self.db, self.platform_bridge, self.config
        )
        self.atmosphere = mods.atmosphere_reader.AtmosphereReader(
            session_buffer=self.session_buffer,
            model_hub=_StubHubWithProviderId(),
            plugin_config=self.config,
        )
        self.fake = SimpleNamespace(
            config=self.config,
            session_buffer=self.session_buffer,
            platform_bridge=self.platform_bridge,
            soul_manager=_StubSoulManager(self.soul),
            user_relation=self.user_relation,
            atmosphere_reader=self.atmosphere,
            schedule_mutator=_StubScheduleMutator(),
            deck_engine=_StubDeckEngine(self.slot),
            model_hub=_StubHubWithProviderId(),
            _last_message_tier={},
            _known_l3_umos={},
            _known_group_umos=set(),
            _ensure_initialized=self._noop_ensure_initialized,
            _on_window_overflow=None,
        )
        self.fake = self._bind_plugin_methods(self.fake)

    async def _noop_ensure_initialized(self):
        return None

    async def test_at_only_group_ping_is_recorded_and_replied(self):
        event = _AtOnlyGroupEvent(message_str="", at_bot=True)
        await self.fake.on_any_message(event)
        umo = "qq bot:GroupMessage:1039802291"
        window = self.session_buffer._windows.get(umo) or []
        assert window, "the @-only ping must be recorded in the sliding window"
        assert "@" in window[0].content
        # direct mention in IDLE -> the native pipeline is allowed to answer
        assert event.is_at_or_wake_command is True
        assert event.call_llm is False
        # group card / relations data is persisted (v2.8 group profiles)
        group = await self.user_relation.get_group(umo)
        assert group is not None
        assert group.group_id == "1039802291"
        assert group.platform_id == "qq bot"
        assert group.bound_soul_id == "酒狐"

    async def test_empty_non_mention_message_is_still_ignored(self):
        event = _AtOnlyGroupEvent(message_str="", at_bot=False)
        await self.fake.on_any_message(event)
        assert not self.session_buffer._windows.get(event.unified_msg_origin)
        assert await self.user_relation.get_group(event.unified_msg_origin) is None

    async def test_evaluate_should_reply_answers_at_only_mention(self):
        event = _AtOnlyGroupEvent(message_str="")
        assert (
            await self.atmosphere.evaluate_should_reply(
                event=event,
                soul=self.soul,
                user=make_user("2975066889", "hidge-1"),
                current_slot=self.slot,
                is_direct_mention=True,
            )
            is True
        )
        assert (
            await self.atmosphere.evaluate_should_reply(
                event=event,
                soul=self.soul,
                user=make_user("2975066889", "hidge-1"),
                current_slot=self.slot,
                is_direct_mention=False,
            )
            is False
        )
