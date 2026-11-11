"""Production upgrade path v2.7 -> v2.8 (Lead / T5).

Verifies the migration checklist documented in ``docs/UPGRADE.md``:

1. an old database (SQLite-only ``souls`` rows, legacy ``umo_soul_bindings``, the
   v2.7 global ``model_config`` in ``plugin_settings``, no ``revision`` column) is
   upgraded in-place: old Souls are written to independent ``souls/*.json`` files,
   the global model config is migrated into the Souls and the two one-shot flags
   (``legacy_umo_bindings_migrated`` / ``model_config_migrated_to_souls``) are set;
2. the upgrade is idempotent (a second pass changes nothing and duplicates no
   platform binding) and never needs a manual SQL step;
3. the plugin startup logs advertise ``Registered 31 Web API routes`` (the v2.8
   route count) and the whole upgrade emits no Traceback;
4. a partially failed migration leaves the flag unset so the next startup retries
   (already covered deeply in ``test_v28_consistency.py``; asserted here for the
   upgrade flow itself).

Offline only: temp SQLite DBs, fake providers, no network.
"""

import json
import logging
import os
import sqlite3
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _helpers import (  # noqa: E402
    DEFAULT_FAKE_PROVIDERS,
    DEFAULT_FAKE_SOURCES,
    DatabaseAsyncTestCase,
    FakeContext,
    FakeProviderManager,
    build_fake_plugin,
    load_plugin_modules,
    make_plugin_config,
    make_soul,
)

mods = load_plugin_modules()
SoulManager = mods.soul_manager.SoulManager
ModelHub = mods.model_hub.ModelHub
ProviderFactory = mods.provider_factory.ProviderFactory
OnlyYouWebAPI = mods.web_api.OnlyYouWebAPI

LEGACY_UMO_FLAG = "legacy_umo_bindings_migrated"
SOUL_MODEL_FLAG = "model_config_migrated_to_souls"
EXPECTED_WEB_API_ROUTES = 31

OLD_SOULS_TABLE = """
    CREATE TABLE souls (
        soul_id TEXT PRIMARY KEY,
        persona_hash TEXT NOT NULL,
        profile_json TEXT NOT NULL,
        updated_at INTEGER NOT NULL
    );
"""
OLD_PLUGIN_SETTINGS_TABLE = """
    CREATE TABLE plugin_settings (
        key TEXT PRIMARY KEY,
        value_json TEXT NOT NULL,
        updated_at INTEGER NOT NULL
    );
"""
OLD_UMO_BINDINGS_TABLE = """
    CREATE TABLE umo_soul_bindings (
        umo TEXT PRIMARY KEY,
        soul_id TEXT NOT NULL,
        updated_at INTEGER NOT NULL
    );
"""


class V27ToV28UpgradeBase(DatabaseAsyncTestCase):
    """Build a v2.7-shaped database, then run the v2.8 startup upgrade steps."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.config = make_plugin_config()
        self.provider_manager = FakeProviderManager(
            sources=DEFAULT_FAKE_SOURCES,
            providers=DEFAULT_FAKE_PROVIDERS,
            audio_dir=self.tmpdir,
        )
        self.context = FakeContext(self.provider_manager)
        self.factory = ProviderFactory(self.context)
        self.legacy_dir = os.path.join(self.tmpdir, "legacy")
        self.legacy_db_path = os.path.join(self.legacy_dir, "only_you.db")
        self.souls_dir = os.path.join(self.legacy_dir, "souls")
        self.log_records: list[logging.LogRecord] = []

    # ------------------------------------------------------------------
    # v2.7 fixture
    # ------------------------------------------------------------------
    def seed_v27_database(self, db_path):
        """Create/seed a v2.7 database (old ``souls`` schema, no ``revision``)."""
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        legacy_soul = make_soul("legacy_soul")
        legacy_soul.name = "旧版角色"
        legacy_soul.speaking_style = "旧版口头禅"
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(OLD_SOULS_TABLE)
            conn.execute(OLD_PLUGIN_SETTINGS_TABLE)
            conn.execute(OLD_UMO_BINDINGS_TABLE)
            conn.execute(
                "INSERT INTO souls (soul_id, persona_hash, profile_json, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    "legacy_soul",
                    "native_persona_hash",
                    json.dumps(legacy_soul.to_dict(), ensure_ascii=False),
                    int(time.time()),
                ),
            )
            conn.execute(
                "INSERT INTO umo_soul_bindings (umo, soul_id, updated_at) "
                "VALUES (?, ?, ?)",
                ("aiocqhttp:FriendMessage:1234", "legacy_soul", int(time.time())),
            )
            legacy_global_model_config = {
                "main": {
                    "source_id": "source_a",
                    "model": "model_a",
                    "inherit": False,
                    "inherit_from": "",
                },
                "decision": {"inherit": True, "inherit_from": "main"},
                "vision": {"inherit": True, "inherit_from": "main"},
                "tts": {"mode": "off"},
            }
            conn.execute(
                "INSERT INTO plugin_settings (key, value_json, updated_at) "
                "VALUES (?, ?, ?)",
                (
                    "model_config",
                    json.dumps(legacy_global_model_config, ensure_ascii=False),
                    int(time.time()),
                ),
            )
            conn.execute(
                "INSERT INTO plugin_settings (key, value_json, updated_at) "
                "VALUES (?, ?, ?)",
                (
                    "default_soul_id",
                    json.dumps("legacy_soul", ensure_ascii=False),
                    int(time.time()),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    async def run_v28_startup_upgrade(self):
        """Replicate ``OnlyYouPlugin.initialize()``'s migration steps in order."""
        db = mods.db.DatabaseManager(self.legacy_db_path)
        await db.initialize()
        hub = ModelHub(
            context=self.context,
            plugin_config=self.config,
            db=db,
            provider_factory=self.factory,
        )
        soul_manager = SoulManager(self.context, db, hub)
        hub.attach_soul_manager(soul_manager)
        await hub.load()  # legacy global model config -> per-Soul (flag) + TTS etc.
        migrated_bindings = await soul_manager.migrate_legacy_umo_bindings()
        souls = await soul_manager.list_souls()  # SQLite-only rows -> souls/*.json
        return db, hub, soul_manager, migrated_bindings, souls


class TestV27ToV28Upgrade(V27ToV28UpgradeBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.seed_v27_database(self.legacy_db_path)

    async def asyncTearDown(self):
        db = getattr(self, "_upgrade_db", None)
        if db is not None:
            await db.close()
        await super().asyncTearDown()

    async def _upgrade(self):
        db, hub, sm, migrated, souls = await self.run_v28_startup_upgrade()
        self._upgrade_db = db
        return hub, sm, migrated, souls

    async def test_legacy_souls_become_independent_json_files(self):
        hub, sm, _migrated, souls = await self._upgrade()
        assert any(soul.soul_id == "legacy_soul" for soul in souls)
        path = os.path.join(self.souls_dir, "legacy_soul.json")
        assert os.path.isfile(path), "old SQLite-only Soul must be written to souls/*.json"
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        for key in (
            "soul_id",
            "card",
            "model_config",
            "bound_platforms",
            "settings",
            "updated_at",
            "revision",
        ):
            assert key in data, f"upgraded Soul JSON is missing '{key}'"
        assert data["card"]["identity"]["name"] == "旧版角色"
        assert data["revision"] >= 1

    async def test_global_model_config_migrates_into_soul_and_sets_flag(self):
        hub, sm, _migrated, _souls = await self._upgrade()
        assert await hub.db.get_setting(SOUL_MODEL_FLAG, False) is True
        soul = await sm.get_soul_by_id("legacy_soul")
        assert soul is not None
        assert soul.model_config["main"]["source_id"] == "source_a"
        assert soul.model_config["main"]["model"] == "model_a"
        # The global key is intentionally kept for rollback purposes.
        assert await hub.db.get_setting("model_config", None) is not None

    async def test_legacy_umo_binding_becomes_platform_binding(self):
        _hub, sm, migrated, _souls = await self._upgrade()
        assert migrated >= 1
        assert await sm.db.get_setting(LEGACY_UMO_FLAG, False) is True
        bindings = await sm.get_platform_bindings()
        assert bindings.get("aiocqhttp") == "legacy_soul"
        soul = await sm.get_soul_by_id("legacy_soul")
        assert soul.bound_platforms == ["aiocqhttp"]

    async def test_upgrade_flow_is_idempotent(self):
        hub, sm, _migrated, _souls = await self._upgrade()
        first_soul = await sm.get_soul_by_id("legacy_soul")
        first_platforms = list(first_soul.bound_platforms)
        first_revision = first_soul.revision

        # Second startup pass: flags already set -> migrations become no-ops.
        await hub.load()
        again = await sm.migrate_legacy_umo_bindings()
        assert again == 0
        souls = await sm.list_souls()
        second_soul = next(item for item in souls if item.soul_id == "legacy_soul")
        assert second_soul.bound_platforms == first_platforms
        assert second_soul.revision == first_revision
        assert await sm.db.get_setting(LEGACY_UMO_FLAG, False) is True
        assert await sm.db.get_setting(SOUL_MODEL_FLAG, False) is True

    async def test_upgrade_emits_no_traceback(self):
        records = []

        class _Collector(logging.Handler):
            def emit(self, record):
                records.append(record)

        collector = _Collector()
        root = logging.getLogger()
        root.addHandler(collector)
        old_level = root.level
        root.setLevel(logging.DEBUG)
        try:
            await self._upgrade()
        finally:
            root.removeHandler(collector)
            root.setLevel(old_level)
        formatted = "\n".join(record.getMessage() for record in records)
        for record in records:
            assert record.exc_info is None, (
                f"upgrade unexpectedly raised inside {record.name}: {record.getMessage()}"
            )
        assert "Traceback" not in formatted

    async def test_web_api_registers_31_routes_with_expected_log(self):
        plugin, api = await build_fake_plugin(self.db, self.tmpdir)
        plugin.context.registered_routes.clear()
        with mock.patch.object(mods.web_api.logger, "info") as fake_info:
            api.register_routes()
        assert len(plugin.context.registered_routes) == EXPECTED_WEB_API_ROUTES
        messages = [
            str(call.args[0]) if call.args else ""
            for call in fake_info.call_args_list
            if call.args
        ]
        assert any(
            f"Registered {EXPECTED_WEB_API_ROUTES} Web API routes" in message
            for message in messages
        ), messages

    async def test_upgrade_flag_not_set_when_migration_partially_fails(self):
        # Reset the flag and force one Soul save to fail during the retry pass.
        _hub, sm, _migrated, _souls = await self._upgrade()
        await sm.db.set_setting(LEGACY_UMO_FLAG, False)
        # Re-create a legacy binding pointing at a platform not yet migrated.
        await sm.db.execute(
            "INSERT OR REPLACE INTO umo_soul_bindings (umo, soul_id, updated_at) "
            "VALUES (?, ?, ?)",
            ("telegram:FriendMessage:99", "legacy_soul", int(time.time())),
        )
        fresh_sm = SoulManager(self.context, sm.db, sm.model_hub)
        with mock.patch.object(
            fresh_sm, "save_soul", side_effect=RuntimeError("disk hiccup")
        ):
            migrated = await fresh_sm.migrate_legacy_umo_bindings()
        assert migrated == 0
        assert await sm.db.get_setting(LEGACY_UMO_FLAG, False) is False
        # Next startup retries and succeeds.
        retried = await fresh_sm.migrate_legacy_umo_bindings()
        assert retried >= 1
        assert await sm.db.get_setting(LEGACY_UMO_FLAG, False) is True


class TestOldSchemaAutoMigration(V27ToV28UpgradeBase):
    async def test_old_schema_without_revision_column_is_upgraded_in_place(self):
        db_path = os.path.join(self.tmpdir, "legacy_schema", "only_you.db")
        self.seed_v27_database(db_path)
        db = mods.db.DatabaseManager(db_path)
        await db.initialize()
        try:
            columns = await db.fetchall("PRAGMA table_info(souls);")
            names = {str(row.get("name") or "") for row in columns}
            assert "revision" in names
            row = await db.fetchone(
                "SELECT revision FROM souls WHERE soul_id = ?", ("legacy_soul",)
            )
            assert row["revision"] == 0
            # A pre-existing v2.7 row is readable after the ALTER TABLE migration.
            legacy_row = await db.fetchone(
                "SELECT profile_json FROM souls WHERE soul_id = ?", ("legacy_soul",)
            )
            assert json.loads(legacy_row["profile_json"])["soul_id"] == "legacy_soul"
        finally:
            await db.close()


if __name__ == "__main__":
    unittest.main()
