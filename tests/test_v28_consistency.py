"""v2.8 hardening: Soul JSON <-> SQLite dual-write consistency (Lead / T1).

Background
----------
v2.8 made ``souls/<safe>.json`` the single source of truth and kept the SQLite
``souls`` table as a pure index.  The first cut compared ``updated_at`` only, so a
clock rollback or a hand-edited JSON file could make an old copy win, and a failed
JSON write silently degraded to SQLite-only.  This suite freezes the hardened
contract:

1. every save bumps a monotonic ``revision`` (clock-rollback safe) and never moves
   ``updated_at`` backwards; JSON file and index end up with the same revision;
2. conflict resolution prefers the higher revision, and on equal revisions keeps
   the JSON file (manual edit) while rebuilding the index -- a newer index row
   (previous JSON write failure) rewrites the JSON file instead (self-healing);
3. JSON writes are atomic (unique temp + replace), failures are retried, leave no
   ``*.tmp`` garbage, set ``last_file_write_error`` and are surfaced by
   ``souls/save`` (``data.storage.degraded``) instead of silently degrading;
4. corrupt JSON falls back to the index; a legacy database without the ``revision``
   column is migrated idempotently;
5. the one-shot migration flags (``legacy_umo_bindings_migrated`` /
   ``model_config_migrated_to_souls``) are only written after **all** steps succeed
   and therefore retry on the next startup after a partial failure.

Everything is offline (stdlib ``unittest`` + a temporary SQLite DB).
"""

import json
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
    card_with,
    load_plugin_modules,
    make_plugin_config,
    make_soul,
)

mods = load_plugin_modules()
SoulManager = mods.soul_manager.SoulManager
ModelHub = mods.model_hub.ModelHub
ProviderFactory = mods.provider_factory.ProviderFactory

LEGACY_UMO_FLAG = "legacy_umo_bindings_migrated"
SOUL_MODEL_FLAG = "model_config_migrated_to_souls"


def read_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path, payload):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


class DualWriteBase(DatabaseAsyncTestCase):
    """Temporary DB + a real ``ModelHub`` / ``SoulManager`` pair."""

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
        self.hub = ModelHub(
            context=self.context,
            plugin_config=self.config,
            db=self.db,
            provider_factory=self.factory,
        )
        await self.hub.load()
        self.sm = SoulManager(self.context, self.db, self.hub)
        self.souls_dir = os.path.join(self.tmpdir, "souls")

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def soul_path(self, soul_id):
        return self.sm.soul_file_path(soul_id)

    async def save_soul(self, soul_id, display_name="角色"):
        profile = make_soul(soul_id)
        profile.name = display_name
        await self.sm.save_soul(profile)
        return profile

    async def index_row(self, soul_id):
        return await self.db.fetchone(
            "SELECT persona_hash, profile_json, updated_at, revision "
            "FROM souls WHERE soul_id = ?",
            (soul_id,),
        )

    def fresh_manager(self):
        return SoulManager(self.context, self.db, self.hub)


# ===========================================================================
# 1. revision / updated_at monotonicity (clock rollback)
# ===========================================================================
class TestRevisionMonotonicity(DualWriteBase):
    async def test_save_bumps_revision_in_both_json_and_index(self):
        first = await self.save_soul("rev_soul", "第一版")
        first_updated = first.updated_at
        assert first.revision == 1
        file_data = read_json(self.soul_path("rev_soul"))
        assert file_data["revision"] == 1
        assert file_data["updated_at"] == first_updated
        row = await self.index_row("rev_soul")
        assert row["revision"] == 1
        assert row["updated_at"] == first_updated

        second = await self.sm.get_soul_by_id("rev_soul")
        second.name = "第二版"
        await self.sm.save_soul(second)
        assert second.revision == 2
        assert second.updated_at > first_updated
        assert read_json(self.soul_path("rev_soul"))["revision"] == 2
        assert (await self.index_row("rev_soul"))["revision"] == 2

    async def test_clock_rollback_keeps_revision_and_timestamp_monotonic(self):
        first = await self.save_soul("clock_soul", "回拨前")
        first_updated = first.updated_at
        rolled_back = int(time.time()) - 3600
        with mock.patch.object(
            mods.soul_manager.time, "time", return_value=rolled_back
        ):
            profile = await self.sm.get_soul_by_id("clock_soul")
            profile.name = "回拨后"
            await self.sm.save_soul(profile)

        assert profile.revision == 2
        assert profile.updated_at > first_updated
        file_data = read_json(self.soul_path("clock_soul"))
        assert file_data["revision"] == 2
        assert file_data["updated_at"] == profile.updated_at
        row = await self.index_row("clock_soul")
        assert row["revision"] == 2

        # A fresh manager must load the post-rollback content, not the older copy.
        fresh = self.fresh_manager()
        souls = await fresh.list_souls()
        reloaded = next(item for item in souls if item.soul_id == "clock_soul")
        assert reloaded.name == "回拨后"
        assert reloaded.revision == 2

    async def test_repeated_fast_saves_keep_updated_at_non_decreasing(self):
        await self.save_soul("fast_soul", "v1")
        stamps = [read_json(self.soul_path("fast_soul"))["updated_at"]]
        for idx in range(3):
            profile = await self.sm.get_soul_by_id("fast_soul")
            profile.name = f"v{idx + 2}"
            await self.sm.save_soul(profile)
            stamps.append(read_json(self.soul_path("fast_soul"))["updated_at"])
        assert stamps == sorted(stamps)
        assert len(set(stamps)) == len(stamps), "updated_at must strictly increase"


# ===========================================================================
# 2. conflict resolution: JSON file vs SQLite index
# ===========================================================================
class TestConflictResolution(DualWriteBase):
    async def test_manual_json_edit_wins_over_same_revision_index(self):
        await self.save_soul("manual_soul", "原始")
        path = self.soul_path("manual_soul")
        data = read_json(path)
        data["card"]["identity"]["name"] = "手工编辑"
        write_json(path, data)

        fresh = self.fresh_manager()
        souls = await fresh.list_souls()
        winner = next(item for item in souls if item.soul_id == "manual_soul")
        assert winner.name == "手工编辑"

        # The index is rebuilt from the JSON file instead of overwriting it.
        row = await self.index_row("manual_soul")
        index_data = json.loads(row["profile_json"])
        assert index_data["card"]["identity"]["name"] == "手工编辑"
        assert read_json(path)["card"]["identity"]["name"] == "手工编辑"

    async def test_get_soul_by_id_applies_same_conflict_rules(self):
        await self.save_soul("get_soul", "原始")
        path = self.soul_path("get_soul")
        data = read_json(path)
        data["card"]["identity"]["name"] = "手工编辑"
        write_json(path, data)
        fresh = self.fresh_manager()
        soul = await fresh.get_soul_by_id("get_soul")
        assert soul is not None and soul.name == "手工编辑"

    async def test_newer_index_row_self_heals_stale_json_file(self):
        soul = await self.save_soul("heal_soul", "旧文件")
        assert soul.revision == 1
        # Simulate "the last JSON write failed, the index got revision 2".
        index_profile = make_soul("heal_soul")
        index_profile.name = "来自索引"
        index_profile.revision = 2
        index_profile.updated_at = int(time.time()) + 30
        await self.db.execute(
            "UPDATE souls SET profile_json = ?, updated_at = ?, revision = ? "
            "WHERE soul_id = ?",
            (
                json.dumps(index_profile.to_dict(), ensure_ascii=False),
                index_profile.updated_at,
                2,
                "heal_soul",
            ),
        )

        fresh = self.fresh_manager()
        souls = await fresh.list_souls()
        winner = next(item for item in souls if item.soul_id == "heal_soul")
        assert winner.name == "来自索引"
        assert winner.revision == 2
        healed = read_json(self.soul_path("heal_soul"))
        assert healed["card"]["identity"]["name"] == "来自索引"
        assert healed["revision"] == 2

    async def test_corrupt_json_falls_back_to_index_and_is_rewritten(self):
        await self.save_soul("corrupt_soul", "索引里的名字")
        with open(self.soul_path("corrupt_soul"), "w", encoding="utf-8") as handle:
            handle.write("{ this is not valid json ")
        fresh = self.fresh_manager()
        souls = await fresh.list_souls()
        winner = next(item for item in souls if item.soul_id == "corrupt_soul")
        assert winner.name == "索引里的名字"
        repaired = read_json(self.soul_path("corrupt_soul"))
        assert repaired["card"]["identity"]["name"] == "索引里的名字"

    async def test_legacy_sqlite_only_row_gets_revision_on_migration(self):
        profile = make_soul("legacy_row")
        profile.name = "旧库角色"
        await self.db.execute(
            "INSERT INTO souls (soul_id, persona_hash, profile_json, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (
                "legacy_row",
                "native",
                json.dumps(profile.to_dict(), ensure_ascii=False),
                int(time.time()),
            ),
        )
        fresh = self.fresh_manager()
        soul = await fresh.get_soul_by_id("legacy_row")
        assert soul is not None and soul.name == "旧库角色"
        migrated = read_json(self.soul_path("legacy_row"))
        assert migrated["revision"] >= 1
        assert migrated["soul_id"] == "legacy_row"


# ===========================================================================
# 3. atomic write / explicit degradation / self-healing
# ===========================================================================
class TestAtomicWriteAndDegradation(DualWriteBase):
    async def test_write_failure_keeps_index_newer_and_sets_error(self):
        first = await self.save_soul("degraded_soul", "旧版本")
        assert first.revision == 1
        profile = await self.sm.get_soul_by_id("degraded_soul")
        profile.name = "新版本"

        with mock.patch.object(
            mods.soul_manager.os, "replace", side_effect=OSError("disk full")
        ):
            await self.sm.save_soul(profile)

        assert self.sm.last_file_write_error, "degraded write must be recorded"
        assert "disk full" in self.sm.last_file_write_error
        row = await self.index_row("degraded_soul")
        assert row["revision"] == 2
        # The stale JSON file is untouched and no tmp garbage is left behind.
        assert read_json(self.soul_path("degraded_soul"))["revision"] == 1
        assert [
            name for name in os.listdir(self.souls_dir) if name.endswith(".tmp")
        ] == []

        # Next read self-heals the JSON file from the newer index row.
        fresh = self.fresh_manager()
        souls = await fresh.list_souls()
        winner = next(item for item in souls if item.soul_id == "degraded_soul")
        assert winner.name == "新版本"
        healed = read_json(self.soul_path("degraded_soul"))
        assert healed["card"]["identity"]["name"] == "新版本"
        assert healed["revision"] == 2
        assert fresh.last_file_write_error == ""

    async def test_successful_write_clears_previous_error(self):
        self.sm.last_file_write_error = "previous failure"
        await self.save_soul("recovered_soul", "恢复")
        assert self.sm.last_file_write_error == ""

    async def test_stale_incoming_profile_does_not_lower_revision(self):
        current = await self.save_soul("stale_soul", "当前")
        stale = make_soul("stale_soul")  # revision 0
        stale.name = "过期对象"
        await self.sm.save_soul(stale)
        assert stale.revision > current.revision
        row = await self.index_row("stale_soul")
        assert row["revision"] == stale.revision


# ===========================================================================
# 4. souls/save Web API surfaces a degraded JSON write
# ===========================================================================
class TestSoulSaveApiStorage(DatabaseAsyncTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.plugin, self.api = await build_fake_plugin(self.db, self.tmpdir)
        attach = getattr(self.plugin.model_hub, "attach_soul_manager", None)
        if callable(attach):
            attach(self.plugin.soul_manager)

    async def call(self, method, handler, query=None, body=None):
        self.api.set_mock_request(method, query=query or {}, body=body or {})
        return await handler()

    async def test_souls_save_reports_file_written(self):
        resp = await self.call(
            "POST",
            self.api.api_souls_save,
            body={
                "soul_id": "api_ok_soul",
                "card": card_with(identity={"name": "API 正常角色"}),
            },
        )
        assert resp["status"] == "ok", resp
        storage = resp["data"]["storage"]
        assert storage == {"file_written": True, "degraded": False, "error": ""}

    async def test_souls_save_surfaces_degraded_json_write(self):
        manager = self.plugin.soul_manager

        def failing_write(profile):
            manager.last_file_write_error = "permission denied"
            return False

        with mock.patch.object(manager, "_write_soul_file", side_effect=failing_write):
            resp = await self.call(
                "POST",
                self.api.api_souls_save,
                body={
                    "soul_id": "api_degraded_soul",
                    "card": card_with(identity={"name": "降级角色"}),
                },
            )
        assert resp["status"] == "ok", resp
        storage = resp["data"]["storage"]
        assert storage["degraded"] is True
        assert storage["file_written"] is False
        assert "permission denied" in storage["error"]
        assert "Soul JSON 写入失败" in resp["message"]
        # The SQLite index still holds the new data (no data loss).
        row = await self.db.fetchone(
            "SELECT profile_json FROM souls WHERE soul_id = ?", ("api_degraded_soul",)
        )
        assert row is not None
        assert json.loads(row["profile_json"])["card"]["identity"]["name"] == "降级角色"


# ===========================================================================
# 5. legacy schema migration + one-shot migration flags
# ===========================================================================
class TestLegacySchemaAndFlags(DualWriteBase):
    async def test_legacy_database_without_revision_column_is_migrated(self):
        legacy_dir = os.path.join(self.tmpdir, "legacy")
        os.makedirs(legacy_dir, exist_ok=True)
        legacy_path = os.path.join(legacy_dir, "only_you.db")
        legacy_profile = make_soul("legacy_db_soul")
        legacy_profile.name = "旧库无 revision"
        conn = sqlite3.connect(legacy_path)
        try:
            conn.execute(
                """
                CREATE TABLE souls (
                    soul_id TEXT PRIMARY KEY,
                    persona_hash TEXT NOT NULL,
                    profile_json TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                """
            )
            conn.execute(
                "INSERT INTO souls (soul_id, persona_hash, profile_json, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    "legacy_db_soul",
                    "native",
                    json.dumps(legacy_profile.to_dict(), ensure_ascii=False),
                    int(time.time()),
                ),
            )
            conn.commit()
        finally:
            conn.close()

        legacy_db = mods.db.DatabaseManager(legacy_path)
        await legacy_db.initialize()
        try:
            columns = await legacy_db.fetchall("PRAGMA table_info(souls);")
            assert "revision" in {str(row.get("name") or "") for row in columns}
            row = await legacy_db.fetchone(
                "SELECT revision FROM souls WHERE soul_id = ?", ("legacy_db_soul",)
            )
            assert row["revision"] == 0
        finally:
            await legacy_db.close()

    async def test_legacy_umo_migration_flag_retried_after_failure(self):
        await self.save_soul("binding_soul", "绑定角色")
        await self.db.execute(
            "INSERT INTO umo_soul_bindings (umo, soul_id, updated_at) VALUES (?, ?, ?)",
            ("aiocqhttp:FriendMessage:1", "binding_soul", int(time.time())),
        )
        fresh = self.fresh_manager()
        with mock.patch.object(
            fresh, "save_soul", side_effect=RuntimeError("transient save failure")
        ):
            migrated = await fresh.migrate_legacy_umo_bindings()
        assert migrated == 0
        assert await self.db.get_setting(LEGACY_UMO_FLAG, False) is False

        migrated = await fresh.migrate_legacy_umo_bindings()
        assert migrated >= 1
        assert await self.db.get_setting(LEGACY_UMO_FLAG, False) is True
        soul = await fresh.get_soul_by_id("binding_soul")
        assert "aiocqhttp" in soul.bound_platforms

    async def test_soul_model_config_migration_flag_retried_after_failure(self):
        await self.save_soul("model_soul", "模型角色")
        await self.db.set_setting(SOUL_MODEL_FLAG, False)
        self.hub.attach_soul_manager(self.sm)
        self.hub._config = mods.model_hub.normalize_model_config(
            {
                "main": {"source_id": "source_a", "model": "model_a"},
                "decision": {"inherit": True, "inherit_from": "main"},
                "vision": {"inherit": True, "inherit_from": "main"},
                "tts": {"mode": "off"},
            }
        )
        assert self.hub._config_is_meaningful(self.hub._config)

        with mock.patch.object(
            self.sm, "save_soul", side_effect=RuntimeError("transient save failure")
        ):
            await self.hub._migrate_config_to_souls_once()
        assert await self.db.get_setting(SOUL_MODEL_FLAG, False) is False

        await self.hub._migrate_config_to_souls_once()
        assert await self.db.get_setting(SOUL_MODEL_FLAG, False) is True
        fresh = self.fresh_manager()
        soul = await fresh.get_soul_by_id("model_soul")
        assert soul.model_config["main"]["source_id"] == "source_a"
        assert soul.model_config["main"]["model"] == "model_a"
        # The per-Soul config is persisted inside the JSON file, not only in SQLite.
        file_data = read_json(self.soul_path("model_soul"))
        assert file_data["model_config"]["main"]["model"] == "model_a"


if __name__ == "__main__":
    unittest.main()
