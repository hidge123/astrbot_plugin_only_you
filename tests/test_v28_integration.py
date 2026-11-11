"""Offline cross-module integration tests for OnlyYou v2.8 (QA / T5).

Frozen contract: ``.team/V28_IMPLEMENTATION_CONTRACT.md``. This file deliberately
covers **cross-module** behaviour only (the per-module unit suites live in
``test_v28_soul_platform.py`` / ``test_v28_memory_surf.py`` / ``test_v28_web_api.py``):

1. Soul independent JSON files: lifecycle (save/list/get/rename/delete), atomic
   filename sanitisation, legacy SQLite-only row migration (§1).
2. Platform (bot instance) binding routing: umo platform prefix, strict mutual
   exclusion, unbind, legacy UMO binding one-shot migration (§1.3).
3. ModelHub per-Soul model config isolation and ``onlyyou::main::<soul_id>``
   ``inst_map`` registration, plus the untouched legacy unattached global path (§2).
4. ``main.py`` unbound methods via the ``bind_plugin_methods`` pattern:
   ``clear_platform_memory_state``, ``_maybe_run_scheduled_gc`` and the SURF share
   gates (§4.1 / §4.4 / §5.1).
5. Web API ``sessions/*`` cross-module wiring through ``FakePlugin`` (bind / unbind
   / clear memory, mutual-exclusion error text) (§6.2).

Everything is stdlib ``unittest`` only: temporary SQLite DBs, no network, no sleep.
"""

import inspect
import json
import os
import sys
import time
import unittest
from types import MethodType, SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _helpers import (  # noqa: E402
    DEFAULT_FAKE_PROVIDERS,
    DEFAULT_FAKE_SOURCES,
    DatabaseAsyncTestCase,
    FakeChatProvider,
    FakeContext,
    FakePlugin,
    FakeProviderManager,
    FakeTTSProvider,
    build_fake_plugin,
    card_with,
    load_plugin_modules,
    make_plugin_config,
    make_run,
    make_slot,
    make_soul,
    make_user,
    StubModelHub,
)

mods = load_plugin_modules()
SoulManager = mods.soul_manager.SoulManager
ModelHub = mods.model_hub.ModelHub
ProviderFactory = mods.provider_factory.ProviderFactory
normalize_model_config = mods.model_hub.normalize_model_config

TODAY = time.strftime("%Y-%m-%d")


def bind_methods(fake, names):
    """Bind *real* unbound ``OnlyYouPlugin`` methods onto a fake ``self``.

    Same pattern as ``tests/test_regressions.py::bind_plugin_methods`` but with an
    explicit name list so this integration file does not depend on another test
    module's private registry.
    """
    from OnlyYou.main import OnlyYouPlugin

    for name in names:
        raw = inspect.getattr_static(OnlyYouPlugin, name)
        if isinstance(raw, staticmethod):
            setattr(fake, name, raw.__func__)
        else:
            setattr(fake, name, MethodType(raw, fake))
    return fake


def read_text(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def read_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


class V28IntegrationBase(DatabaseAsyncTestCase):
    """Temporary DB + real components (ModelHub / ProviderFactory / SoulManager)."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        FakeChatProvider.reset()
        FakeTTSProvider.reset()
        FakeTTSProvider.audio_dir = self.tmpdir
        self.config = make_plugin_config()
        self.provider_manager = FakeProviderManager(
            sources=DEFAULT_FAKE_SOURCES,
            providers=DEFAULT_FAKE_PROVIDERS,
            audio_dir=self.tmpdir,
        )
        self.context = FakeContext(self.provider_manager)
        self.factory = ProviderFactory(self.context)
        self.hub = ModelHub(
            context=self.context,
            plugin_config=self.config,
            db=self.db,
            provider_factory=self.factory,
        )
        await self.hub.load()
        self.sm = SoulManager(self.context, self.db, self.hub)
        self.expected_souls_dir = os.path.join(self.tmpdir, "souls")

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    async def save_soul(self, soul_id, display_name="", **overrides):
        card = card_with(
            identity={"name": display_name or str(soul_id), "tagline": "集成测试角色"},
            speech={"speaking_style": "简短口语化"},
        )
        soul = make_soul(soul_id=soul_id, card=card)
        for key, value in overrides.items():
            setattr(soul, key, value)
        return await self.sm.save_soul(soul)

    def soul_path(self, soul_id):
        return self.sm.soul_file_path(soul_id)

    async def soul_rows(self):
        return await self.db.fetchall("SELECT * FROM souls ORDER BY soul_id")

    async def insert_legacy_soul_row(self, soul_id, name="旧版角色", updated_at=None):
        """Insert a pre-v2.8 SQLite-only ``souls`` row (no JSON file)."""
        profile = make_soul(
            soul_id=soul_id,
            card=card_with(identity={"name": name, "tagline": "旧数据"}),
        )
        payload = profile.to_dict()
        for key in ("model_config", "bound_platforms", "settings", "updated_at"):
            payload.pop(key, None)
        await self.db.execute(
            "INSERT INTO souls (soul_id, persona_hash, profile_json, updated_at)"
            " VALUES (?, ?, ?, ?)",
            (
                soul_id,
                "legacy_hash",
                json.dumps(payload, ensure_ascii=False),
                int(updated_at if updated_at is not None else time.time()) - 120,
            ),
        )
        return payload


# ===========================================================================
# 1. Soul independent JSON files (contract §1)
# ===========================================================================
class TestSoulJsonFileLifecycle(V28IntegrationBase):
    async def test_souls_dir_is_derived_from_db_path(self):
        assert self.sm.souls_dir == self.expected_souls_dir
        assert self.expected_souls_dir.startswith(self.tmpdir)
        await self.sm.list_souls()
        assert os.path.isdir(self.expected_souls_dir)

    async def test_save_soul_writes_contract_json_file(self):
        soul = await self.save_soul("酒狐", display_name="酒狐", bound_platforms=["aiocqhttp"])
        path = self.soul_path("酒狐")
        assert path == os.path.join(self.expected_souls_dir, "酒狐.json")
        assert os.path.isfile(path)

        raw = read_text(path)
        # UTF-8 + ensure_ascii=False + indent=2 (contract §1.1)
        assert "酒狐" in raw and "\\u9152" not in raw
        assert raw.startswith('{\n  "soul_id"')
        data = json.loads(raw)
        for key in (
            "soul_id",
            "card",
            "state_event_cards",
            "model_config",
            "bound_platforms",
            "settings",
            "updated_at",
        ):
            assert key in data, f"missing frozen key {key!r}"
        assert data["soul_id"] == "酒狐"
        assert data["bound_platforms"] == ["aiocqhttp"]
        assert data["settings"]["surfing_enabled"] is True
        assert isinstance(data["updated_at"], int) and data["updated_at"] > 0
        assert data["card"]["identity"]["name"] == "酒狐"
        # the model config is always stored in its normalized four-role shape
        assert set(data["model_config"]) == {"main", "decision", "vision", "tts"}

        # SQLite keeps the index row in sync
        rows = await self.soul_rows()
        assert [row["soul_id"] for row in rows] == ["酒狐"]
        assert json.loads(rows[0]["profile_json"])["soul_id"] == "酒狐"
        assert soul.updated_at == data["updated_at"]

    async def test_save_and_reload_roundtrips_v28_fields(self):
        model_config = {
            "main": {"source_id": "deepseek", "model": "deepseek-chat"},
            "decision": {
                "inherit": False,
                "inherit_from": "",
                "source_id": "ailiyun",
                "model": "kimi-k2.5",
            },
        }
        await self.save_soul(
            "roundtrip",
            display_name="回环",
            model_config=model_config,
            bound_platforms=["aiocqhttp", "telegram_bot_1"],
            settings={"surfing_enabled": False, "custom_key": {"nested": [1, 2]}},
        )

        fresh = SoulManager(self.context, self.db, self.hub)
        loaded = await fresh.get_soul_by_id("roundtrip")
        assert loaded is not None
        assert loaded.name == "回环"
        assert loaded.surfing_enabled is False
        assert loaded.bound_platforms == ["aiocqhttp", "telegram_bot_1"]
        assert loaded.settings["custom_key"] == {"nested": [1, 2]}
        expected = normalize_model_config(model_config)
        assert loaded.model_config == expected
        assert loaded.model_config["decision"]["inherit"] is False
        assert loaded.updated_at > 0

        listed = await fresh.list_souls()
        assert [soul.soul_id for soul in listed] == ["roundtrip"]
        assert listed[0].model_config == expected

    async def test_empty_library_creates_default_soul_json(self):
        souls = await self.sm.list_souls()
        assert len(souls) == 1
        default = souls[0]
        assert default.soul_id == mods.soul_manager.DEFAULT_SOUL_ID
        assert os.path.isfile(self.soul_path(default.soul_id))
        assert await self.sm.get_default_soul_id() == default.soul_id
        listed_files = [n for n in os.listdir(self.expected_souls_dir) if n.endswith(".json")]
        assert listed_files == [f"{default.soul_id}.json"]

    async def test_rename_moves_json_file_and_cascades_routing(self):
        await self.save_soul("old_bot", display_name="旧名", bound_platforms=["aiocqhttp"])
        await self.save_soul("other_bot", display_name="另一个")
        await self.sm.set_default_soul_id("old_bot")
        await self.sm.set_umo_binding("legacy:FriendMessage:1", "old_bot")
        await self.db.execute(
            "INSERT INTO memories (soul_id, user_id, user_tier, memory_tier, content,"
            " keywords, importance, vitality, access_count, created_at)"
            " VALUES ('old_bot', 'u1', 1, 2, '旧角色记忆', 'k', 5, 100.0, 0, ?)",
            (int(time.time()),),
        )

        old_path = self.soul_path("old_bot")
        assert os.path.isfile(old_path)
        renamed = await self.sm.rename_soul("old_bot", "new_bot")

        assert renamed.soul_id == "new_bot"
        renamed_path = self.soul_path("new_bot")
        assert os.path.isfile(renamed_path)
        new_raw = read_json(renamed_path)
        assert new_raw["soul_id"] == "new_bot"
        assert new_raw["bound_platforms"] == ["aiocqhttp"]
        assert not os.path.exists(old_path), f"stale soul file {base} still present"

        fresh = SoulManager(self.context, self.db, self.hub)
        assert await fresh.get_soul_by_id("old_bot") is None
        reloaded = await fresh.get_soul_by_id("new_bot")
        assert reloaded is not None and reloaded.name == "旧名"
        assert await fresh.get_default_soul_id() == "new_bot"
        assert (await fresh.get_umo_bindings()).get("legacy:FriendMessage:1") == "new_bot"
        memory_rows = await self.db.fetchall("SELECT soul_id FROM memories")
        assert [row["soul_id"] for row in memory_rows] == ["new_bot"]

    async def test_delete_soul_removes_json_and_index(self):
        await self.save_soul("to_delete", display_name="待删")
        path = self.soul_path("to_delete")
        assert os.path.isfile(path)
        await self.sm.delete_soul("to_delete")
        assert not os.path.exists(path)
        assert await self.sm.get_soul_by_id("to_delete") is None
        assert await self.db.fetchone("SELECT * FROM souls WHERE soul_id = ?", ("to_delete",)) is None

    async def test_unsafe_soul_id_gets_safe_filename(self):
        soul = await self.save_soul("a/b:c*?", display_name="危险 ID")
        path = self.soul_path(soul.soul_id)
        assert os.path.dirname(path) == self.expected_souls_dir
        filename = os.path.basename(path)
        for char in '\\/:*?"<>|':
            assert char not in filename, f"unsafe char {char!r} in {filename!r}"
        assert filename.endswith(".json")
        assert "__" in filename  # sha1 suffix added after sanitising
        # the soul id itself is preserved verbatim inside the JSON payload
        assert read_json(path)["soul_id"] == "a/b:c*?"

    async def test_v28_settings_property_and_setting_update(self):
        soul = await self.save_soul("toggle_bot", display_name="开关")
        assert soul.surfing_enabled is True
        updated = await self.sm.set_soul_setting("toggle_bot", "surfing_enabled", False)
        assert updated.surfing_enabled is False
        assert read_json(self.soul_path("toggle_bot"))["settings"]["surfing_enabled"] is False

        fresh = SoulManager(self.context, self.db, self.hub)
        reloaded = await fresh.get_soul_by_id("toggle_bot")
        assert reloaded is not None and reloaded.surfing_enabled is False
        with self.assertRaises(ValueError):
            await fresh.set_soul_setting("missing_bot", "surfing_enabled", False)


class TestSoulLegacyMigration(V28IntegrationBase):
    async def test_sqlite_only_row_migrates_to_json_on_list(self):
        payload = await self.insert_legacy_soul_row("legacy_soul", name="旧数据角色")
        assert not os.path.exists(self.soul_path("legacy_soul"))

        souls = await self.sm.list_souls()
        migrated = next(soul for soul in souls if soul.soul_id == "legacy_soul")
        assert migrated.name == "旧数据角色"
        # missing v2.8 keys fall back to defaults
        assert migrated.model_config == normalize_model_config(None)
        assert migrated.bound_platforms == []
        assert migrated.settings == {"surfing_enabled": True}

        path = self.soul_path("legacy_soul")
        assert os.path.isfile(path)
        data = read_json(path)
        assert data["soul_id"] == "legacy_soul"
        assert data["card"]["identity"]["name"] == "旧数据角色"
        assert "model_config" in data and "settings" in data
        assert payload["soul_id"] == "legacy_soul"

    async def test_sqlite_only_row_migrates_to_json_on_get(self):
        await self.insert_legacy_soul_row("get_migrated", name="按需迁移")
        assert not os.path.exists(self.soul_path("get_migrated"))

        fresh = SoulManager(self.context, self.db, self.hub)
        soul = await fresh.get_soul_by_id("get_migrated")
        assert soul is not None and soul.name == "按需迁移"
        assert os.path.isfile(self.soul_path("get_migrated"))
        assert read_json(self.soul_path("get_migrated"))["soul_id"] == "get_migrated"

    def test_from_dict_tolerates_missing_v28_keys(self):
        legacy = make_soul("no_v28").to_dict()
        for key in ("model_config", "bound_platforms", "settings", "updated_at"):
            legacy.pop(key, None)
        profile = mods.soul_manager.SoulProfile.from_dict(legacy)
        assert profile.soul_id == "no_v28"
        assert profile.model_config == normalize_model_config(None)
        assert profile.bound_platforms == []
        assert profile.settings == {"surfing_enabled": True}
        assert profile.updated_at == 0

    async def test_json_file_wins_over_stale_sqlite_index(self):
        soul = await self.save_soul("winner", display_name="JSON 优先")
        # Manually rewrite the JSON file with a newer timestamp + different name.
        path = self.soul_path("winner")
        data = read_json(path)
        data["card"]["identity"]["name"] = "文件赢"
        data["updated_at"] = int(time.time()) + 60
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        fresh = SoulManager(self.context, self.db, self.hub)
        souls = await fresh.list_souls()
        winner = next(item for item in souls if item.soul_id == "winner")
        assert winner.name == "文件赢"
        assert soul is not None


# ===========================================================================
# 2. Platform (bot instance) binding routing (contract §1.3)
# ===========================================================================
class TestPlatformBindingRouting(V28IntegrationBase):
    async def test_bind_platform_and_query_bindings(self):
        await self.save_soul("soul_a", display_name="甲")
        await self.save_soul("soul_b", display_name="乙")

        soul = await self.sm.bind_platform("soul_a", "aiocqhttp")
        assert soul.bound_platforms == ["aiocqhttp"]
        assert await self.sm.get_platform_bindings() == {"aiocqhttp": "soul_a"}
        assert await self.sm.get_bound_platforms("soul_a") == ["aiocqhttp"]
        assert await self.sm.get_bound_platforms("soul_b") == []
        assert read_json(self.soul_path("soul_a"))["bound_platforms"] == ["aiocqhttp"]

        # binding the same platform to the same soul again is idempotent
        again = await self.sm.bind_platform("soul_a", "aiocqhttp")
        assert again.bound_platforms == ["aiocqhttp"]

    async def test_platform_mutual_exclusion_error_text(self):
        await self.save_soul("soul_a", display_name="甲")
        await self.save_soul("soul_b", display_name="乙")
        await self.sm.bind_platform("soul_a", "aiocqhttp")
        with self.assertRaises(ValueError) as ctx:
            await self.sm.bind_platform("soul_b", "aiocqhttp")
        assert str(ctx.exception) == (
            "该机器人接口已被角色『甲』绑定，请先在角色 甲 下解绑"
        )
        # the failed bind leaves both souls untouched
        assert await self.sm.get_platform_bindings() == {"aiocqhttp": "soul_a"}
        assert (await self.sm.get_soul_by_id("soul_b")).bound_platforms == []

    async def test_bind_platform_validates_input(self):
        await self.save_soul("soul_a", display_name="甲")
        for bad in ("", "   ", "aiocqhttp:GroupMessage:1"):
            with self.assertRaises(ValueError):
                await self.sm.bind_platform("soul_a", bad)
        with self.assertRaises(ValueError):
            await self.sm.bind_platform("soul_a", "x" * 65)
        with self.assertRaises(ValueError):
            await self.sm.bind_platform("missing_soul", "aiocqhttp")

    async def test_unbind_platform_returns_owner_and_clears_binding(self):
        await self.save_soul("soul_a", display_name="甲")
        await self.save_soul("soul_b", display_name="乙")
        await self.sm.bind_platform("soul_a", "aiocqhttp")
        await self.sm.bind_platform("soul_b", "telegram")

        assert await self.sm.unbind_platform("aiocqhttp") == "soul_a"
        assert await self.sm.get_platform_bindings() == {"telegram": "soul_b"}
        assert read_json(self.soul_path("soul_a"))["bound_platforms"] == []
        # unknown / already unbound platform is a no-op
        assert await self.sm.unbind_platform("aiocqhttp") == ""
        assert await self.sm.unbind_platform("") == ""

    async def test_resolve_soul_routes_by_platform_prefix(self):
        await self.save_soul("soul_a", display_name="甲")
        await self.save_soul("soul_b", display_name="乙")
        await self.sm.bind_platform("soul_a", "aiocqhttp")
        await self.sm.set_default_soul_id("soul_b")

        routed = await self.sm.resolve_soul("aiocqhttp:GroupMessage:123456")
        assert routed.soul_id == "soul_a"
        # an unbound platform falls back to the plugin default soul
        fallback = await self.sm.resolve_soul("telegram:FriendMessage:9")
        assert fallback.soul_id == "soul_b"
        # private messages of a bound platform route to that Soul too
        private = await self.sm.resolve_soul("aiocqhttp:FriendMessage:987")
        assert private.soul_id == "soul_a"

    async def test_platform_binding_wins_over_legacy_umo_binding(self):
        await self.save_soul("soul_a", display_name="甲")
        await self.save_soul("soul_b", display_name="乙")
        await self.sm.set_umo_binding("qq:FriendMessage:1", "soul_b")
        await self.sm.bind_platform("soul_a", "qq")

        resolved = await self.sm.resolve_soul("qq:FriendMessage:1")
        assert resolved.soul_id == "soul_a", "platform binding must win (contract §1.3)"

    async def test_legacy_umo_bindings_migrate_once(self):
        await self.save_soul("soul_a", display_name="甲")
        await self.save_soul("soul_b", display_name="乙")
        await self.sm.set_umo_binding("qq:FriendMessage:1", "soul_b")
        await self.sm.set_umo_binding("telegram:FriendMessage:2", "soul_b")
        assert await self.sm.get_platform_bindings() == {}

        # simulate a restart: fresh manager re-reads UMO bindings from SQLite
        fresh = SoulManager(self.context, self.db, self.hub)
        migrated = await fresh.migrate_legacy_umo_bindings()
        assert migrated == 2
        assert await fresh.get_platform_bindings() == {
            "qq": "soul_b",
            "telegram": "soul_b",
        }
        assert await fresh.get_bound_platforms("soul_b") == ["qq", "telegram"]
        assert read_json(self.soul_path("soul_b"))["bound_platforms"] == [
            "qq",
            "telegram",
        ]
        # one-shot flag: the second run is a no-op
        assert await fresh.migrate_legacy_umo_bindings() == 0
        assert await self.db.get_setting("legacy_umo_bindings_migrated", False) is True
        # routing now works through the platform prefix even without exact UMO hit
        resolved = await fresh.resolve_soul("qq:GroupMessage:whatever")
        assert resolved.soul_id == "soul_b"


if __name__ == "__main__":
    unittest.main()


# ===========================================================================
# 3. ModelHub per-Soul model config + provider registration (contract §2)
# ===========================================================================
MAIN_A = {"main": {"source_id": "deepseek", "model": "deepseek-chat"}}
MAIN_B = {"main": {"source_id": "deepseek", "model": "deepseek-reasoner"}}
DECISION_ONLY = {
    "main": {"source_id": "deepseek", "model": "deepseek-chat"},
    "decision": {
        "inherit": False,
        "inherit_from": "",
        "source_id": "deepseek",
        "model": "deepseek-reasoner",
    },
}


class TestModelHubPerSoul(V28IntegrationBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        # main.py wires ModelHub -> SoulManager right after constructing both.
        self.hub.attach_soul_manager(self.sm)

    async def make_pair(self):
        await self.save_soul("soul_a", display_name="甲")
        await self.save_soul("soul_b", display_name="乙")

    async def test_save_config_writes_soul_json_and_isolates(self):
        await self.make_pair()
        saved = await self.hub.save_config(MAIN_A, soul_id="soul_a")
        assert saved["main"] == {"source_id": "deepseek", "model": "deepseek-chat"}
        assert self.hub.get_config_sync("soul_a")["main"] == saved["main"]
        assert (await self.hub.get_config("soul_a"))["main"] == saved["main"]
        # per-Soul persistence goes to the Soul JSON, never the legacy global KV
        assert read_json(self.soul_path("soul_a"))["model_config"]["main"] == saved["main"]
        assert read_json(self.soul_path("soul_b"))["model_config"]["main"] == {
            "source_id": "",
            "model": "",
        }
        assert await self.db.get_setting("model_config", None) is None
        # role resolution is isolated per Soul
        resolved_a = await self.hub.resolve_role("main", soul_id="soul_a")
        assert resolved_a == {"source_id": "deepseek", "model": "deepseek-chat"}
        assert await self.hub.resolve_role("main", soul_id="soul_b") is None
        assert await self.hub.resolve_role("decision", soul_id="soul_a") == resolved_a

    async def test_default_soul_config_resolution(self):
        await self.make_pair()
        await self.hub.save_config(DECISION_ONLY, soul_id="soul_b")
        await self.sm.set_default_soul_id("soul_b")

        default_cfg = await self.hub.get_config()
        assert default_cfg["main"] == DECISION_ONLY["main"]
        assert default_cfg["decision"]["model"] == "deepseek-reasoner"
        assert (
            await self.hub.get_config("soul_a")
        )["main"] == {"source_id": "", "model": ""}
        # an explicit soul_id overrides the default routing
        assert (await self.hub.get_config("soul_a"))["main"]["model"] == ""

    async def test_provider_routing_by_umo_uses_bound_soul(self):
        await self.make_pair()
        await self.hub.save_config(MAIN_A, soul_id="soul_a")
        await self.hub.save_config(MAIN_B, soul_id="soul_b")
        await self.sm.bind_platform("soul_a", "qq")
        await self.sm.bind_platform("soul_b", "telegram")

        provider_a = await self.hub.get_provider("main", umo="qq:GroupMessage:1")
        provider_b = await self.hub.get_provider("main", umo="telegram:GroupMessage:2")
        assert isinstance(provider_a, FakeChatProvider)
        assert isinstance(provider_b, FakeChatProvider)
        assert provider_a.provider_config["model"] == "deepseek-chat"
        assert provider_b.provider_config["model"] == "deepseek-reasoner"

    async def test_get_main_provider_id_registers_onlyyou_main_per_soul(self):
        await self.make_pair()
        await self.hub.save_config(MAIN_A, soul_id="soul_a")
        await self.hub.save_config(MAIN_B, soul_id="soul_b")

        id_a = await self.hub.get_main_provider_id("soul_a")
        id_b = await self.hub.get_main_provider_id("soul_b")
        assert id_a == "onlyyou::main::soul_a"
        assert id_b == "onlyyou::main::soul_b"
        inst_map = self.provider_manager.inst_map
        assert isinstance(inst_map[id_a], FakeChatProvider)
        assert isinstance(inst_map[id_b], FakeChatProvider)
        assert inst_map[id_a] is not inst_map[id_b]
        assert inst_map[id_a].provider_config["model"] == "deepseek-chat"
        assert inst_map[id_b].provider_config["model"] == "deepseek-reasoner"
        # plugin-built instances never pollute AstrBot's own provider list
        assert self.provider_manager.provider_insts == []

    async def test_save_config_unregisters_only_that_soul(self):
        await self.make_pair()
        await self.hub.save_config(MAIN_A, soul_id="soul_a")
        await self.hub.save_config(MAIN_B, soul_id="soul_b")
        await self.hub.get_main_provider_id("soul_a")
        await self.hub.get_main_provider_id("soul_b")
        inst_map = self.provider_manager.inst_map
        assert "onlyyou::main::soul_a" in inst_map
        assert "onlyyou::main::soul_b" in inst_map

        await self.hub.save_config(MAIN_B, soul_id="soul_a")
        assert "onlyyou::main::soul_a" not in inst_map
        assert "onlyyou::main::soul_b" in inst_map, "other Souls must stay registered"

    async def test_reuse_loaded_astrbot_instance_returns_native_id(self):
        await self.make_pair()
        await self.hub.save_config(MAIN_A, soul_id="soul_a")
        loaded = FakeChatProvider(
            {
                "id": "deepseek/deepseek-chat",
                "provider_source_id": "deepseek",
                "model": "deepseek-chat",
                "type": "openai_chat_completion",
                "provider_type": "chat_completion",
            }
        )
        self.provider_manager.add_loaded("deepseek/deepseek-chat", loaded)

        main_id = await self.hub.get_main_provider_id("soul_a")
        assert main_id == "deepseek/deepseek-chat"
        assert "onlyyou::main::soul_a" not in self.provider_manager.inst_map

    async def test_tts_and_status_read_per_soul(self):
        await self.make_pair()
        tts_cfg = {
            "main": {"source_id": "deepseek", "model": "deepseek-chat"},
            "tts": {
                "mode": "source",
                "source_id": "edge-tts-source",
                "model": "tts-1",
                "voice": "alloy",
            },
        }
        await self.hub.save_config(tts_cfg, soul_id="soul_a")
        assert self.hub.get_tts_config("soul_a")["mode"] == "source"
        assert self.hub.get_tts_config("soul_b")["mode"] == "off"
        resolved_tts = await self.hub.resolve_role("tts", soul_id="soul_a")
        assert resolved_tts is not None and resolved_tts["source_id"] == "edge-tts-source"
        assert await self.hub.resolve_role("tts", soul_id="soul_b") is None

        status_a = await self.hub.get_status("soul_a")
        status_b = await self.hub.get_status("soul_b")
        assert set(status_a) >= {"main", "decision", "vision", "tts"}
        assert status_a["main"]["configured"] is True
        assert status_b["main"]["configured"] is False

    async def test_legacy_unattached_global_path_preserved(self):
        """No SoulManager attached -> pre-v2.8 plugin_settings persistence."""
        legacy_hub = ModelHub(
            context=self.context,
            plugin_config=self.config,
            db=self.db,
            provider_factory=self.factory,
        )
        # a bare hub must not touch the Soul JSON files at all
        legacy_hub.soul_manager = None
        await legacy_hub.load()
        await legacy_hub.save_model_config(MAIN_A)
        stored = await self.db.get_setting("model_config", None)
        assert stored is not None and stored["main"] == MAIN_A["main"]
        assert set(stored) == {"main", "decision", "vision", "tts"}

        main_id = await legacy_hub.get_main_provider_id()
        assert main_id == "onlyyou::main"
        assert "onlyyou::main" in self.provider_manager.inst_map
        assert not os.path.isdir(self.expected_souls_dir) or os.listdir(
            self.expected_souls_dir
        ) == []

    async def test_global_config_migrates_into_existing_souls_once(self):
        await self.make_pair()
        # pre-v2.8 global config written by an unattached hub
        legacy_hub = ModelHub(
            context=self.context,
            plugin_config=self.config,
            db=self.db,
            provider_factory=self.factory,
        )
        await legacy_hub.load()
        await legacy_hub.save_model_config(DECISION_ONLY)

        fresh_hub = ModelHub(
            context=self.context,
            plugin_config=self.config,
            db=self.db,
            provider_factory=self.factory,
        )
        fresh_hub.attach_soul_manager(self.sm)
        await fresh_hub.load()

        for soul_id in ("soul_a", "soul_b"):
            data = read_json(self.soul_path(soul_id))
            assert data["model_config"]["main"] == DECISION_ONLY["main"]
            assert data["model_config"]["decision"]["model"] == "deepseek-reasoner"
        # legacy global key survives, and the one-shot flag is persisted
        assert (await self.db.get_setting("model_config", None))["main"] == DECISION_ONLY["main"]
        assert (
            await self.db.get_setting("model_config_migrated_to_souls", False)
        ) is True
        # a meaningful per-Soul config is never overwritten by a later load
        await self.hub.save_config(MAIN_B, soul_id="soul_a")
        await fresh_hub.load()
        assert read_json(self.soul_path("soul_a"))["model_config"]["main"] == MAIN_B["main"]


# ===========================================================================
# 5. Web API sessions/* cross-module wiring (contract §6.2)
# ===========================================================================
class FakePlatformInstance:
    """Minimal AstrBot platform instance exposing ``meta()`` for the bridge."""

    def __init__(self, platform_id, platform_type="telegram", name="TG Bot"):
        self._meta = SimpleNamespace(
            id=platform_id, type=platform_type, name=name
        )

    def meta(self):
        return self._meta


class TestWebApiSessionsCrossModule(DatabaseAsyncTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        FakeChatProvider.reset()
        FakeTTSProvider.reset()
        FakeTTSProvider.audio_dir = self.tmpdir
        self.plugin, self.api = await build_fake_plugin(self.db, self.tmpdir)
        self.sm = self.plugin.soul_manager
        self.buffer = self.plugin.session_buffer
        await self.create_soul("soul_a", "甲")
        await self.create_soul("soul_b", "乙")

    # ------------------------------------------------------------------
    async def create_soul(self, soul_id, name):
        soul = make_soul(
            soul_id=soul_id,
            card=card_with(identity={"name": name, "tagline": "接口集成角色"}),
        )
        return await self.sm.save_soul(soul)

    async def call(self, method, handler, query=None, body=None):
        self.api.set_mock_request(method, query=query or {}, body=body or {})
        return await handler()

    def assert_ok(self, response):
        assert response["status"] == "ok", response
        assert isinstance(response["message"], str)
        return response["data"]

    def assert_error(self, response):
        assert response["status"] == "error", response
        return response

    def add_window(self, umo, count=2, unread=0):
        for index in range(count):
            msg = self.buffer.record_user_message(
                umo, make_user(uid="user-1", name="阿一"), f"消息 {index}"
            )
            assert msg is not None
            if index < unread:
                self.buffer.mark_unread(umo, msg, is_private="FriendMessage" in umo)

    def mark_known(self, umo):
        self.plugin._known_group_umos.add(umo)
        self.plugin._last_group_lurk_ts[umo] = 1.0
        self.plugin._last_private_catchup_ts[umo] = 1.0

    # ------------------------------------------------------------------
    async def test_platforms_payload_merges_bridge_souls_and_sessions(self):
        # one configured platform + one runtime platform (bridge merge)
        self.plugin.context.config = {
            "platform": [
                {"id": "aiocqhttp", "type": "aiocqhttp", "enable": True, "name": "QQ"}
            ]
        }
        self.plugin.context.platform_manager = SimpleNamespace(
            get_insts=lambda: [FakePlatformInstance("telegram")]
        )
        await self.sm.bind_platform("soul_a", "aiocqhttp")
        self.add_window("aiocqhttp:GroupMessage:1", count=3)
        self.add_window("telegram:GroupMessage:2", count=1)
        self.mark_known("aiocqhttp:GroupMessage:1")

        data = self.assert_ok(
            await self.call(
                "GET", self.api.api_sessions_platforms, query={"soul_id": "soul_a"}
            )
        )
        assert data["current_soul_id"] == "soul_a"
        souls = {item["soul_id"]: item for item in data["souls"]}
        assert souls["soul_a"]["bound_platforms"] == ["aiocqhttp"]
        assert souls["soul_b"]["bound_platforms"] == []

        platforms = {item["platform_id"]: item for item in data["platforms"]}
        assert set(platforms) == {"aiocqhttp", "telegram"}
        assert platforms["aiocqhttp"]["bound_soul_id"] == "soul_a"
        assert platforms["aiocqhttp"]["bound_soul_name"] == "甲"
        assert platforms["aiocqhttp"]["source"] == "config"
        assert platforms["aiocqhttp"]["session_count"] == 1
        assert platforms["telegram"]["running"] is True
        assert platforms["telegram"]["source"] == "runtime"
        for item in data["platforms"]:
            for key in (
                "platform_id",
                "platform_type",
                "name",
                "enabled",
                "running",
                "source",
                "bound_soul_id",
                "bound_soul_name",
                "session_count",
            ):
                assert key in item, f"platform item missing {key!r}"

        sessions = {item["umo"]: item for item in data["sessions"]}
        assert "aiocqhttp:GroupMessage:1" in sessions
        assert "telegram:GroupMessage:2" not in sessions, "other Soul's platform leaks in"
        entry = sessions["aiocqhttp:GroupMessage:1"]
        assert entry["kind"] == "group"
        assert entry["platform_id"] == "aiocqhttp"
        assert entry["bound_soul_id"] == "soul_a"
        assert entry["window_messages"] == 3
        assert entry["is_l3_private"] is False

    async def test_bind_platform_api_is_exclusive_and_updates_routing(self):
        data = self.assert_ok(
            await self.call(
                "POST",
                self.api.api_sessions_bind_platform,
                body={"soul_id": "soul_a", "platform_id": "aiocqhttp"},
            )
        )
        assert data["soul_id"] == "soul_a" and data["platform_id"] == "aiocqhttp"
        assert await self.sm.get_platform_bindings() == {"aiocqhttp": "soul_a"}
        resolved = await self.sm.resolve_soul("aiocqhttp:GroupMessage:99")
        assert resolved.soul_id == "soul_a"
        platforms = {item["platform_id"]: item for item in data["platforms"]}
        assert platforms["aiocqhttp"]["bound_soul_id"] == "soul_a"

        conflict = self.assert_error(
            await self.call(
                "POST",
                self.api.api_sessions_bind_platform,
                body={"soul_id": "soul_b", "platform_id": "aiocqhttp"},
            )
        )
        assert conflict["message"] == "该机器人接口已被角色『甲』绑定，请先在角色 甲 下解绑"
        assert await self.sm.get_platform_bindings() == {"aiocqhttp": "soul_a"}
        assert (await self.sm.get_soul_by_id("soul_b")).bound_platforms == []

    async def test_unbind_platform_api_clears_platform_memory_across_modules(self):
        await self.sm.bind_platform("soul_a", "aiocqhttp")
        self.add_window("aiocqhttp:GroupMessage:1", count=3, unread=2)
        self.add_window("telegram:GroupMessage:2", count=2)
        self.mark_known("aiocqhttp:GroupMessage:1")
        self.mark_known("telegram:GroupMessage:2")
        self.plugin._known_l3_umos["user-9"] = "aiocqhttp:FriendMessage:9"
        memory_id = await self.plugin.memory_manager.add_memory(
            soul_id="soul_a",
            content="解绑不应该删除的持久记忆",
            keywords="记忆",
            importance=6,
            memory_tier=2,
        )
        assert memory_id

        data = self.assert_ok(
            await self.call(
                "POST",
                self.api.api_sessions_unbind_platform,
                body={"platform_id": "aiocqhttp"},
            )
        )
        assert data["previous_soul_id"] == "soul_a"
        assert data["platform_id"] == "aiocqhttp"
        cleared = data["cleared"]
        assert set(cleared) == {"windows", "evicted", "unread", "messages"}
        assert cleared["windows"] >= 1
        assert cleared["unread"] >= 1
        assert cleared["messages"] > 0

        # cross-module: SoulManager binding gone, SessionBuffer + runtime maps cleaned
        assert await self.sm.get_platform_bindings() == {}
        remaining = self.buffer.list_window_umos()
        assert "aiocqhttp:GroupMessage:1" not in remaining
        assert "telegram:GroupMessage:2" in remaining, "other platform must survive"
        assert "aiocqhttp:GroupMessage:1" not in self.plugin._known_group_umos
        assert "aiocqhttp:FriendMessage:9" not in self.plugin._known_l3_umos.values()
        assert "user-9" not in self.plugin._known_l3_umos
        assert "aiocqhttp:GroupMessage:1" not in self.plugin._last_group_lurk_ts
        assert "aiocqhttp:GroupMessage:1" not in self.plugin._last_private_catchup_ts
        # persistent memory is never deleted by an unbind
        memories = await self.plugin.memory_manager.list_memories(soul_id="soul_a")
        assert any(item.id == memory_id for item in memories)

        again = self.assert_ok(
            await self.call(
                "POST",
                self.api.api_sessions_unbind_platform,
                body={"platform_id": "aiocqhttp"},
            )
        )
        assert again["previous_soul_id"] == ""
        assert again["cleared"] == {
            "windows": 0,
            "evicted": 0,
            "unread": 0,
            "messages": 0,
        }

    async def test_clear_memory_single_session_keeps_siblings(self):
        await self.sm.bind_platform("soul_a", "aiocqhttp")
        self.add_window("aiocqhttp:GroupMessage:1", count=2)
        self.add_window("aiocqhttp:GroupMessage:2", count=2)

        data = self.assert_ok(
            await self.call(
                "POST",
                self.api.api_sessions_clear_memory,
                body={"umo": "aiocqhttp:GroupMessage:1"},
            )
        )
        assert data["cleared"]["messages"] >= 2
        remaining = self.buffer.list_window_umos()
        assert "aiocqhttp:GroupMessage:1" not in remaining
        assert "aiocqhttp:GroupMessage:2" in remaining
        assert [item["umo"] for item in data["sessions"]] == ["aiocqhttp:GroupMessage:2"]

    async def test_clear_memory_platform_scopes_and_errors(self):
        await self.sm.bind_platform("soul_a", "aiocqhttp")
        self.add_window("aiocqhttp:GroupMessage:1", count=2, unread=1)
        self.add_window("telegram:GroupMessage:2", count=1)
        self.plugin._known_group_umos.add("aiocqhttp:GroupMessage:1")

        data = self.assert_ok(
            await self.call(
                "POST",
                self.api.api_sessions_clear_memory,
                body={"platform_id": "aiocqhttp"},
            )
        )
        assert data["cleared"]["messages"] >= 3
        remaining = self.buffer.list_window_umos()
        assert "telegram:GroupMessage:2" in remaining
        assert "aiocqhttp:GroupMessage:1" not in remaining
        assert "aiocqhttp:GroupMessage:1" not in self.plugin._known_group_umos

        missing = self.assert_error(
            await self.call("POST", self.api.api_sessions_clear_memory, body={})
        )
        assert "umo" in missing["message"] or "platform_id" in missing["message"]


# ===========================================================================
# 4. main.py unbound methods (contract §4.1 / §4.4 / §5.1 / §4.3)
# ===========================================================================
class RecordingProactiveBridge:
    """Minimal ``PlatformBridge`` stand-in recording proactive sends."""

    def __init__(self):
        self.sent = []
        self.ok = True

    async def send_proactive_message(
        self, umo, text, session_buffer=None, soul_name="Bot", audio_path=None
    ):
        self.sent.append(
            {
                "umo": umo,
                "text": text,
                "soul_name": soul_name,
                "audio_path": audio_path,
                "session_buffer": session_buffer,
            }
        )
        return self.ok


class RecordingTextHub:
    """ModelHub stub recording per-Soul ``call_llm_text`` calls."""

    def __init__(self, text="刚刷到一个巨好笑的视频，快来看！"):
        self.text = text
        self.calls = []

    async def call_llm_text(
        self, prompt="", role="decision", umo=None, system_prompt="",
        image_urls=None, soul_id=None,
    ):
        self.calls.append({"prompt": prompt, "role": role, "umo": umo, "soul_id": soul_id})
        return self.text

    def get_tts_config(self, soul_id=None):
        return {"mode": "off"}


def make_review(video_id="video-1", score=90, is_fallback=False):
    return SimpleNamespace(
        video=SimpleNamespace(
            video_id=video_id,
            title="猫猫翻车名场面",
            share_url=f"https://www.bilibili.com/video/{video_id}",
        ),
        score=score,
        commentary="笑死我了哈哈哈哈",
        visual_notes="",
        is_fallback=is_fallback,
    )


class TestClearPlatformMemoryState(DatabaseAsyncTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.config = make_plugin_config()
        self.buffer = mods.context_budget.SessionBuffer(self.config)
        self.memory = mods.memory_manager.MemoryManager(
            self.db, StubModelHub(), self.config
        )
        self.fake = FakePlugin(
            db=self.db,
            config=self.config,
            session_buffer=self.buffer,
            memory_manager=self.memory,
            context=FakeContext(),
        )
        self.fake._known_group_umos = {
            "aiocqhttp:GroupMessage:1",
            "telegram:GroupMessage:2",
        }
        self.fake._known_l3_umos = {
            "user-1": "aiocqhttp:FriendMessage:1",
            "user-2": "telegram:FriendMessage:2",
        }
        self.fake._last_group_lurk_ts = {
            "aiocqhttp:GroupMessage:1": 1.0,
            "telegram:GroupMessage:2": 2.0,
        }
        self.fake._last_private_catchup_ts = {
            "aiocqhttp:FriendMessage:1": 3.0,
            "telegram:FriendMessage:2": 4.0,
        }
        bind_methods(self.fake, ["clear_platform_memory_state"])

    def add_window(self, umo, count=2, unread=0):
        for index in range(count):
            msg = self.buffer.record_user_message(
                umo, make_user(uid="user-1", name="阿一"), f"消息 {index}"
            )
            if index < unread:
                self.buffer.mark_unread(umo, msg, is_private="FriendMessage" in umo)

    async def test_clears_one_platform_and_keeps_other_platform(self):
        self.add_window("aiocqhttp:GroupMessage:1", count=3, unread=2)
        self.add_window("aiocqhttp:FriendMessage:1", count=1)
        self.add_window("telegram:GroupMessage:2", count=2)
        memory_id = await self.memory.add_memory(
            soul_id="soul_a",
            content="解绑平台后仍然保留的持久记忆",
            keywords="记忆",
            memory_tier=2,
        )

        stats = await self.fake.clear_platform_memory_state("aiocqhttp")
        assert set(stats) >= {"windows", "evicted", "unread", "messages"}
        assert stats["windows"] >= 1
        assert stats["unread"] >= 1
        assert stats["messages"] >= 3
        assert stats["groups"] == 1
        assert stats["l3"] == 1
        assert stats["caches"] == 2

        remaining = self.buffer.list_window_umos()
        assert "aiocqhttp:GroupMessage:1" not in remaining
        assert "aiocqhttp:FriendMessage:1" not in remaining
        assert "telegram:GroupMessage:2" in remaining
        assert self.fake._known_group_umos == {"telegram:GroupMessage:2"}
        assert self.fake._known_l3_umos == {"user-2": "telegram:FriendMessage:2"}
        assert self.fake._last_group_lurk_ts == {"telegram:GroupMessage:2": 2.0}
        assert self.fake._last_private_catchup_ts == {"telegram:FriendMessage:2": 4.0}
        memories = await self.memory.list_memories(soul_id="soul_a")
        assert any(item.id == memory_id for item in memories)

    async def test_empty_platform_is_a_noop(self):
        self.add_window("aiocqhttp:GroupMessage:1", count=1)
        stats = await self.fake.clear_platform_memory_state("")
        assert stats["windows"] == 0 and stats["messages"] == 0
        assert "aiocqhttp:GroupMessage:1" in self.buffer.list_window_umos()


class TestScheduledGc(DatabaseAsyncTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.config = make_plugin_config()
        self.memory = mods.memory_manager.MemoryManager(
            self.db, StubModelHub(), self.config
        )
        self.fake = FakePlugin(
            db=self.db, config=self.config, memory_manager=self.memory
        )
        bind_methods(self.fake, ["_maybe_run_scheduled_gc"])

        self.calls = {"cleanup": 0, "gc": 0}
        original_cleanup = self.memory.cleanup_tier1
        original_gc = self.memory.run_forgetting_gc

        async def counted_cleanup(now_override=None):
            self.calls["cleanup"] += 1
            return await original_cleanup(now_override)

        async def counted_gc(now_override=None):
            self.calls["gc"] += 1
            return await original_gc(now_override)

        self.memory.cleanup_tier1 = counted_cleanup
        self.memory.run_forgetting_gc = counted_gc
        self.addCleanup(setattr, self.memory, "cleanup_tier1", original_cleanup)
        self.addCleanup(setattr, self.memory, "run_forgetting_gc", original_gc)

    async def test_runs_full_gc_once_per_day_and_persists_date(self):
        expired_id = await self.memory.add_memory(
            soul_id="soul_a",
            content="一条超过48小时的T1记忆",
            keywords="过期",
            memory_tier=1,
            created_at=int(time.time()) - 49 * 3600,
        )
        fresh_id = await self.memory.add_memory(
            soul_id="soul_a",
            content="一条新鲜的T1记忆",
            keywords="新鲜",
            memory_tier=1,
        )

        stats = await self.fake._maybe_run_scheduled_gc()
        assert stats["gc_ran"] == 1
        assert stats["deleted_expired"] >= 1
        assert self.calls == {"cleanup": 1, "gc": 1}
        assert await self.db.get_setting("last_forgetting_gc_date", "") == TODAY
        rows = await self.db.fetchall("SELECT id FROM memories WHERE memory_tier = 1")
        remaining = {row["id"] for row in rows}
        assert expired_id not in remaining
        assert fresh_id in remaining

        # second heartbeat on the same day: only realtime T1 trimming
        again = await self.fake._maybe_run_scheduled_gc()
        assert again["gc_ran"] == 0
        assert self.calls == {"cleanup": 2, "gc": 1}

        # restart safety: a fresh object on the same DB does not re-run the daily GC
        restarted = FakePlugin(
            db=self.db, config=self.config, memory_manager=self.memory
        )
        bind_methods(restarted, ["_maybe_run_scheduled_gc"])
        assert (await restarted._maybe_run_scheduled_gc())["gc_ran"] == 0
        assert self.calls["gc"] == 1

    async def test_new_day_runs_full_gc_again(self):
        await self.db.set_setting("last_forgetting_gc_date", "2000-01-01")
        stats = await self.fake._maybe_run_scheduled_gc()
        assert stats["gc_ran"] == 1
        assert self.calls["gc"] == 1
        assert await self.db.get_setting("last_forgetting_gc_date", "") == TODAY


class TestSurfShareGates(DatabaseAsyncTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.config = make_plugin_config(
            surfing_config={
                "enable_bilibili": True,
                "surf_share_min_score": 80,
                "surf_share_cooldown_minutes": 90,
            }
        )
        self.buffer = mods.context_budget.SessionBuffer(self.config)
        self.bridge = RecordingProactiveBridge()
        self.hub = RecordingTextHub()
        self.soul = make_soul("soul_a")
        self.run = make_run(soul_id="soul_a")
        self.surf_slot = next(
            slot for slot in self.run.slots if slot.action_type == mods.roguelite_deck.ActionType.SURF
        )
        self.fake = FakePlugin(
            config=self.config,
            session_buffer=self.buffer,
            platform_bridge=self.bridge,
            model_hub=self.hub,
            context=FakeContext(),
        )
        self.fake._known_l3_umos = {"user-1": "aiocqhttp:FriendMessage:1"}
        bind_methods(
            self.fake,
            [
                "_maybe_share_surfed_video",
                "_idle_roam_cooldown_seconds",
                "_latest_l3_umo",
                "_render_soul_summary",
                "_maybe_synthesize_proactive",
            ],
        )

    async def share(self, review):
        return await self.fake._maybe_share_surfed_video(
            self.soul, self.run, self.surf_slot, review
        )

    async def test_share_success_marks_video_and_timestamp(self):
        sent = await self.share(make_review("v-1"))
        assert sent is True
        assert len(self.bridge.sent) == 1
        record = self.bridge.sent[0]
        assert record["umo"] == "aiocqhttp:FriendMessage:1"
        assert record["soul_name"] == self.soul.name
        assert self.hub.calls and self.hub.calls[0]["soul_id"] == "soul_a"
        assert self.fake._shared_video_ids["soul_a"] == {"v-1"}
        assert self.fake._last_surf_share_ts["soul_a"] > 0

        # the same video is never shared twice by the same Soul
        assert await self.share(make_review("v-1")) is False
        assert len(self.bridge.sent) == 1

    async def test_vision_fallback_is_never_shared(self):
        assert await self.share(make_review("v-fb", score=60, is_fallback=True)) is False
        assert self.bridge.sent == []
        assert self.fake._shared_video_ids.get("soul_a", set()) == set()

    async def test_low_score_is_not_shared(self):
        assert await self.share(make_review("v-low", score=79)) is False
        assert self.bridge.sent == []

    async def test_surf_share_cooldown_gate(self):
        self.fake._last_surf_share_ts["soul_a"] = time.time() - 10 * 60
        assert await self.share(make_review("v-cd")) is False
        assert self.bridge.sent == []
        # older than the 90-minute cooldown passes again
        self.fake._last_surf_share_ts["soul_a"] = time.time() - 91 * 60
        assert await self.share(make_review("v-cd")) is True

    async def test_idle_roam_cooldown_gate(self):
        self.fake._last_idle_roam_ts["soul_a"] = time.time() - 10 * 60
        assert await self.share(make_review("v-roam")) is False
        assert self.bridge.sent == []
        self.fake._last_idle_roam_ts["soul_a"] = time.time() - 46 * 60
        assert await self.share(make_review("v-roam")) is True

    async def test_no_l3_target_skips_share(self):
        self.fake._known_l3_umos = {}
        assert await self.share(make_review("v-no-l3")) is False
        assert self.bridge.sent == []
        assert "v-no-l3" not in self.fake._shared_video_ids.get("soul_a", set())

    async def test_failed_send_keeps_retry_chance(self):
        self.bridge.ok = False
        assert await self.share(make_review("v-retry")) is False
        assert "v-retry" not in self.fake._shared_video_ids.get("soul_a", set())
        assert "soul_a" not in self.fake._last_surf_share_ts
        # next pass retries and succeeds
        self.bridge.ok = True
        assert await self.share(make_review("v-retry")) is True
        assert len(self.bridge.sent) == 2

    async def test_empty_generation_skips_send(self):
        self.hub.text = ""
        assert await self.share(make_review("v-empty")) is False
        assert self.bridge.sent == []


class TestHeartbeatSoulSelection(DatabaseAsyncTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.context = FakeContext()
        self.factory = ProviderFactory(self.context)
        self.hub = ModelHub(
            context=self.context, plugin_config={}, db=self.db,
            provider_factory=self.factory,
        )
        await self.hub.load()
        self.sm = SoulManager(self.context, self.db, self.hub)
        self.fake = FakePlugin(soul_manager=self.sm)
        bind_methods(self.fake, ["_souls_for_heartbeat"])

    async def create_soul(self, soul_id):
        await self.sm.save_soul(make_soul(soul_id=soul_id))

    async def test_no_bindings_returns_single_default_soul(self):
        await self.create_soul("soul_a")
        await self.create_soul("soul_b")
        await self.sm.set_default_soul_id("soul_b")
        souls = await self.fake._souls_for_heartbeat()
        assert [soul.soul_id for soul in souls] == ["soul_b"]

    async def test_bound_souls_plus_default_fallback(self):
        await self.create_soul("soul_a")
        await self.create_soul("soul_b")
        await self.create_soul("soul_c")
        await self.sm.bind_platform("soul_a", "aiocqhttp")
        await self.sm.set_default_soul_id("soul_b")
        souls = await self.fake._souls_for_heartbeat()
        assert [soul.soul_id for soul in souls] == ["soul_a", "soul_b"]


class TestHeartbeatDispatch(DatabaseAsyncTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.config = make_plugin_config()
        self.calls = []
        self.run = make_run(soul_id="soul_a")

        async def get_slot(soul_id="", **kwargs):
            return self.run, self.current_slot, None

        class DeckStub:
            get_current_and_next_slot = staticmethod(get_slot)

        class PipelineStub:
            def __init__(self, calls):
                self.calls = calls

            async def surf_once(self, soul=None, current_state=None):
                self.calls.append(("surf_once", soul.soul_id))
                return make_review("v-heartbeat")

        self.sm_stub = SimpleNamespace(
            get_default_soul_id=self._default_id,
            get_platform_bindings=self._empty_bindings,
        )
        self.fake = FakePlugin(
            config=self.config,
            soul_manager=self.sm_stub,
            deck_engine=DeckStub(),
            surfing_pipeline=PipelineStub(self.calls),
        )

        async def settle(*args, **kwargs):
            self.calls.append(("settle",))

        async def catch_up(soul, run=None, current_slot=None, **kwargs):
            self.calls.append(("catch_up", tuple(sorted(kwargs.get("allowed_platforms") or ()))))

        async def idle_roam(soul, run, current_slot, **kwargs):
            self.calls.append(("idle_roam", tuple(sorted(kwargs.get("allowed_platforms") or ()))))
            return True

        async def share(soul, run, current_slot, review):
            self.calls.append(("share", getattr(review.video, "video_id", "")))
            return True

        self.fake._settle_finished_busy_blocks = settle
        self.fake._catch_up_private_unread = catch_up
        self.fake._run_idle_roam_action = idle_roam
        self.fake._maybe_share_surfed_video = share
        bind_methods(self.fake, ["_dispatch_heartbeat_for_soul", "_surfing_enabled"])

    async def _default_id(self):
        return "soul_a"

    async def _empty_bindings(self):
        return {}

    def soul(self, bound_platforms=None, surfing_enabled=True):
        return make_soul(
            "soul_a",
            settings={"surfing_enabled": surfing_enabled},
            bound_platforms=bound_platforms or [],
        )

    async def test_surf_enabled_goes_through_pipeline_and_share(self):
        self.current_slot = make_slot(
            action_type=mods.roguelite_deck.ActionType.SURF,
            start_hour=13.0, end_hour=15.0, state=mods.state_machine.BotState.IDLE,
            title="刷视频",
        )
        await self.fake._dispatch_heartbeat_for_soul(
            self.soul(bound_platforms=["aiocqhttp"])
        )
        assert ("settle",) in self.calls
        assert ("surf_once", "soul_a") in self.calls
        assert ("share", "v-heartbeat") in self.calls
        assert not any(call[0] == "idle_roam" for call in self.calls)
        catch_up = next(call for call in self.calls if call[0] == "catch_up")
        assert catch_up[1] == ("aiocqhttp",)

    async def test_surf_disabled_degrades_to_idle_roam(self):
        self.current_slot = make_slot(
            action_type=mods.roguelite_deck.ActionType.SURF,
            start_hour=13.0, end_hour=15.0, state=mods.state_machine.BotState.IDLE,
            title="刷视频",
        )
        await self.fake._dispatch_heartbeat_for_soul(
            self.soul(surfing_enabled=False)
        )
        assert not any(call[0] == "surf_once" for call in self.calls)
        assert not any(call[0] == "share" for call in self.calls)
        assert any(call[0] == "idle_roam" for call in self.calls)

    async def test_sleep_block_skips_all_actions(self):
        self.current_slot = make_slot(
            action_type=mods.roguelite_deck.ActionType.SLEEP,
            start_hour=0.0, end_hour=7.0, state=mods.state_machine.BotState.RESTING,
            title="睡觉",
        )
        await self.fake._dispatch_heartbeat_for_soul(self.soul())
        assert ("settle",) in self.calls
        for kind in ("surf_once", "share", "idle_roam", "catch_up"):
            assert not any(call[0] == kind for call in self.calls)

    async def test_idle_roam_block_dispatches_idle_roam(self):
        self.current_slot = make_slot(
            action_type=mods.roguelite_deck.ActionType.IDLE_ROAM,
            start_hour=18.0, end_hour=23.0, state=mods.state_machine.BotState.IDLE,
            title="发呆",
        )
        await self.fake._dispatch_heartbeat_for_soul(self.soul())
        assert any(call[0] == "idle_roam" for call in self.calls)
        assert not any(call[0] == "surf_once" for call in self.calls)


class TestHubCallSoulFallback(unittest.IsolatedAsyncioTestCase):
    """Contract §10.3: per-Soul calls must fall back to the legacy stub signature."""

    async def test_hub_call_json_and_text_soul_id_fallback(self):
        import OnlyYou.main as main_module

        class LegacyStub:
            def __init__(self):
                self.calls = []

            async def call_llm_json(self, prompt="", role="decision", umo=None,
                                    system_prompt="", image_urls=None, default=None):
                self.calls.append(("json", umo))
                return default

            async def call_llm_text(self, prompt="", role="decision", umo=None,
                                    system_prompt="", image_urls=None):
                self.calls.append(("text", umo))
                return "legacy"

        class SoulAwareStub(LegacyStub):
            async def call_llm_json(self, prompt="", role="decision", umo=None,
                                    system_prompt="", image_urls=None, default=None,
                                    soul_id=None):
                self.calls.append(("json", umo, soul_id))
                return {"ok": True}

            async def call_llm_text(self, prompt="", role="decision", umo=None,
                                    system_prompt="", image_urls=None, soul_id=None):
                self.calls.append(("text", umo, soul_id))
                return "soul"

        soul = make_soul("soul_a")
        legacy = LegacyStub()
        assert await main_module._hub_call_json(
            legacy, "p", umo="u", default={"d": 1}, soul=soul
        ) == {"d": 1}
        assert await main_module._hub_call_text(legacy, "p", umo="u", soul=soul) == "legacy"

        aware = SoulAwareStub()
        assert await main_module._hub_call_json(
            aware, "p", umo="u", default=None, soul=soul
        ) == {"ok": True}
        assert await main_module._hub_call_text(aware, "p", umo="u", soul=soul) == "soul"
        assert aware.calls == [("json", "u", "soul_a"), ("text", "u", "soul_a")]
