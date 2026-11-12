"""v2.9 cross-module integration tests: global identity + per-Soul relationships.

Complements ``test_v29_soul_relations.py`` (Lead unit) and
``test_v29_web_api.py`` (Web API): wires ``main.initialize``'s per-Soul migration,
the context-budget window peers, and the Web API/FakePlugin path end-to-end.
"""

import asyncio
import json
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _helpers import (  # noqa: E402
    DatabaseAsyncTestCase,
    FakeContext,
    build_fake_plugin,
    load_plugin_modules,
    make_plugin_config,
    make_soul,
)

import OnlyYou.main as _onlyyou_main  # noqa: E402

mods = load_plugin_modules()
UserRelationManager = mods.user_relation.UserRelationManager
ContextBudgetManager = mods.context_budget.ContextBudgetManager
SessionBuffer = mods.context_budget.SessionBuffer
OnlyYouPlugin = _onlyyou_main.OnlyYouPlugin


class _Bridge:
    def get_platform_admins(self, umo=None):
        return []


class _SoulManagerStub:
    """Minimal SoulManager stand-in for ``OnlyYouPlugin.initialize`` wiring."""

    def __init__(self, bindings=None, default_soul_id="soul-b"):
        self.bindings = dict(bindings or {"qq bot": "soul-a"})
        self.default_soul_id = default_soul_id
        self.on_soul_deleted = None
        self.soul_ids = ["soul-a", "soul-b"]

    async def migrate_legacy_umo_bindings(self):
        return 0

    async def get_platform_bindings(self):
        return dict(self.bindings)

    async def list_souls(self):
        return [SimpleNamespace(soul_id=soul_id) for soul_id in self.soul_ids]

    async def get_default_soul_id(self):
        return self.default_soul_id

    async def resolve_soul(self, umo=None):
        return SimpleNamespace(soul_id=self.default_soul_id)


class _InitHost:
    """Host object exposing exactly what ``OnlyYouPlugin.initialize`` touches."""

    def __init__(self, db, user_relation, soul_manager):
        self.db = db
        self.user_relation = user_relation
        self.soul_manager = soul_manager
        self.web_api = SimpleNamespace(register_routes=lambda: None)
        self._bg_tasks = []
        self.migration_ran = False

    async def _ensure_initialized(self):
        return None

    async def _load_runtime_state(self):
        return None

    async def _maybe_run_scheduled_gc(self):
        return None

    async def _heartbeat_loop(self):
        await asyncio.sleep(0)

    async def _daily_gc_loop(self):
        await asyncio.sleep(0)


class TestInitializeMigrationWiring(DatabaseAsyncTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.config = make_plugin_config()
        self.relation = UserRelationManager(self.db, _Bridge(), self.config)

    async def seed_user(self, uid, pids):
        await self.db.execute(
            """
            INSERT INTO users (
                global_uid, display_name, nickname, tier, interaction_count,
                affinity, platform_ids_json, attitude, updated_at
            ) VALUES (?, ?, '', 2, 4, 0.0, ?, '', 1)
            """,
            (uid, uid, json.dumps(pids, ensure_ascii=False)),
        )

    async def test_initialize_splits_legacy_users_per_soul(self):
        await self.seed_user("qq bot:42", ["qq bot:42"])
        await self.seed_user("legacy-only", ["legacy-only"])
        soul_manager = _SoulManagerStub(default_soul_id="soul-b")
        host = _InitHost(self.db, self.relation, soul_manager)
        await OnlyYouPlugin.initialize(host)  # unbound: uses our host attrs
        for task in host._bg_tasks:
            task.cancel()
        assert soul_manager.on_soul_deleted == self.relation.purge_soul_cache
        assert await self.db.get_setting("user_relations_migrated_v1", False) is True
        assert await self.relation.get_user("qq bot:42", soul_id="soul-a") is not None
        assert await self.relation.get_user("legacy-only", soul_id="soul-b") is not None
        # The default Soul (soul-b) also knows the QQ account? No — qq bot maps to
        # soul-a only; legacy-only has no platform -> default soul-b only.
        assert await self.relation.get_user("qq bot:42", soul_id="soul-b") is None

    async def test_initialize_migration_is_idempotent(self):
        await self.seed_user("qq bot:42", ["qq bot:42"])
        soul_manager = _SoulManagerStub()
        host = _InitHost(self.db, self.relation, soul_manager)
        await OnlyYouPlugin.initialize(host)
        for task in host._bg_tasks:
            task.cancel()
        first = await self.db.fetchall("SELECT * FROM user_relations")
        host2 = _InitHost(self.db, self.relation, soul_manager)
        await OnlyYouPlugin.initialize(host2)
        for task in host2._bg_tasks:
            task.cancel()
        second = await self.db.fetchall("SELECT * FROM user_relations")
        assert len(first) == 1
        assert len(second) == 1

    async def test_initialize_does_not_fail_without_souls(self):
        await self.seed_user("qq bot:42", ["qq bot:42"])
        soul_manager = _SoulManagerStub()
        soul_manager.soul_ids = []
        host = _InitHost(self.db, self.relation, soul_manager)
        await OnlyYouPlugin.initialize(host)
        for task in host._bg_tasks:
            task.cancel()
        assert await self.db.get_setting("user_relations_migrated_v1", False) is False
        assert not await self.db.fetchall("SELECT * FROM user_relations")


class TestContextPeersPerSoul(DatabaseAsyncTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.config = make_plugin_config()
        self.relation = UserRelationManager(self.db, _Bridge(), self.config)
        self.buffer = SessionBuffer(self.config)
        self.umo = "qq bot:GroupMessage:999"
        # Current speaker u1 + peer u2, different per-Soul nickname/attitude.
        await self.relation.update_user_details(
            "u1", display_name="阿一", nickname="A-一", attitude="A 态度", soul_id="soul-a"
        )
        await self.relation.update_user_details(
            "u1", display_name="阿一", nickname="B-一", attitude="B 态度", soul_id="soul-b"
        )
        await self.relation.update_user_details(
            "u2", display_name="阿二", nickname="A-二", attitude="A 二态度", soul_id="soul-a"
        )
        await self.relation.update_user_details(
            "u2", display_name="阿二", nickname="B-二", attitude="B 二态度", soul_id="soul-b"
        )
        u1_a = await self.relation.get_user("u1", soul_id="soul-a")
        u1_b = await self.relation.get_user("u1", soul_id="soul-b")
        self.buffer.record_user_message(self.umo, u1_b, "我先说一句")
        self.buffer.record_user_message(self.umo, u1_a, "换角色也说一句")
        self.buffer.record_user_message(
            self.umo, await self.relation.get_user("u2", soul_id="soul-a"), "阿二插话"
        )
        budget = ContextBudgetManager(
            memory_manager=None,
            session_buffer=self.buffer,
            plugin_config=self.config,
            user_relation=self.relation,
        )
        self.current_a = u1_a
        self.current_b = u1_b
        self.budget = budget

    async def test_peers_use_current_soul_relations(self):
        block_a = await self.budget._render_user_relation_context(
            self.umo, self.current_a, soul_id="soul-a"
        )
        block_b = await self.budget._render_user_relation_context(
            self.umo, self.current_b, soul_id="soul-b"
        )
        assert "A-一" in block_a and "A-二" in block_a
        assert "B-一" in block_b and "B-二" in block_b
        assert "B-二" not in block_a
        assert "A-二" not in block_b

    async def test_current_user_relation_fields_are_used(self):
        block_a = await self.budget._render_user_relation_context(
            self.umo, self.current_a, soul_id="soul-a"
        )
        block_b = await self.budget._render_user_relation_context(
            self.umo, self.current_b, soul_id="soul-b"
        )
        assert "A 态度" in block_a
        assert "B 态度" in block_b


class TestWebApiSoulScoping(DatabaseAsyncTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.plugin, self.api = await build_fake_plugin(self.db, self.tmpdir)
        for soul_id in ("soul-a", "soul-b"):
            await self.plugin.soul_manager.save_soul(make_soul(soul_id))
        self.relation = self.plugin.user_relation
        await self.relation.update_user_details(
            "u1", display_name="A-阿一", tier=3, soul_id="soul-a"
        )
        await self.relation.update_user_details(
            "u2", display_name="A-阿二", tier=2, soul_id="soul-a"
        )
        await self.relation.update_user_details(
            "u1", display_name="A-阿一", tier=1, soul_id="soul-b"
        )

    async def call(self, method, handler, query=None, body=None):
        self.api.set_mock_request(method, query=query or {}, body=body or {})
        return await handler()

    async def test_users_list_is_soul_scoped(self):
        resp = await self.call(
            "GET", self.api.api_users_list, query={"soul_id": "soul-a"}
        )
        assert resp["status"] == "ok"
        assert resp["data"]["soul_id"] == "soul-a"
        assert {u["global_uid"] for u in resp["data"]["users"]} == {"u1", "u2"}
        scoped = {u["global_uid"]: u for u in resp["data"]["users"]}
        assert scoped["u1"]["tier"] == 3
        assert resp["data"]["counts"]["l3_count"] == 1

        resp_b = await self.call(
            "GET", self.api.api_users_list, query={"soul_id": "soul-b"}
        )
        assert [u["global_uid"] for u in resp_b["data"]["users"]] == ["u1"]
        assert resp_b["data"]["users"][0]["tier"] == 1

    async def test_users_list_unknown_soul_is_empty(self):
        resp = await self.call(
            "GET", self.api.api_users_list, query={"soul_id": "no-such-soul"}
        )
        assert resp["status"] == "ok"
        assert resp["data"]["users"] == []

    async def test_users_list_legacy_view_without_soul_id(self):
        resp = await self.call("GET", self.api.api_users_list)
        assert resp["status"] == "ok"
        assert resp["data"]["soul_id"] == ""
        assert {u["global_uid"] for u in resp["data"]["users"]} >= {"u1", "u2"}

    async def test_scoped_update_isolated(self):
        resp = await self.call(
            "POST",
            self.api.api_users_update,
            body={
                "action": "update",
                "soul_id": "soul-b",
                "global_uid": "u1",
                "tier": 2,
                "nickname": "B-专属",
            },
        )
        assert resp["status"] == "ok"
        rel_a = await self.db.fetchone(
            "SELECT * FROM user_relations WHERE soul_id='soul-a' AND global_uid='u1'"
        )
        rel_b = await self.db.fetchone(
            "SELECT * FROM user_relations WHERE soul_id='soul-b' AND global_uid='u1'"
        )
        assert int(rel_a["tier"]) == 3
        assert int(rel_b["tier"]) == 2
        assert rel_b["nickname"] == "B-专属"

    async def test_merge_rejects_user_not_in_current_soul(self):
        await self.relation.update_user_details(
            "other", display_name="Bonly", soul_id="soul-b"
        )
        resp = await self.call(
            "POST",
            self.api.api_users_update,
            body={
                "action": "merge",
                "soul_id": "soul-a",
                "primary_uid": "u1",
                "secondary_uid": "other",
            },
        )
        assert resp["status"] == "error"
        assert "当前角色关系列表" in resp["message"]
        assert await self.relation.get_user("u1", soul_id="soul-a") is not None
        assert await self.relation.get_user("other", soul_id="soul-b") is not None

    async def test_merge_rejects_same_user(self):
        resp = await self.call(
            "POST",
            self.api.api_users_update,
            body={
                "action": "merge",
                "soul_id": "soul-a",
                "primary_uid": "u1",
                "secondary_uid": "u1",
            },
        )
        assert resp["status"] == "error"

    async def test_merge_merges_only_selected_soul_and_keeps_other_identity_relations(self):
        # u2 participates in soul-a; soul-b only knows u1.
        await self.relation.update_user_details(
            "u2", display_name="A-阿二", tier=2, soul_id="soul-b"
        )
        resp = await self.call(
            "POST",
            self.api.api_users_update,
            body={
                "action": "merge",
                "soul_id": "soul-a",
                "primary_uid": "u1",
                "secondary_uid": "u2",
            },
        )
        assert resp["status"] == "ok"
        # ``u2`` is now an alias of ``u1``: looking it up resolves to the merged
        # identity, but no separate relation row exists for it in any Soul.
        alias = await self.relation.get_user("u2", soul_id="soul-a")
        assert alias is not None and alias.global_uid == "u1"
        for soul_id in ("soul-a", "soul-b"):
            rows = await self.db.fetchall(
                "SELECT * FROM user_relations WHERE soul_id = ? AND global_uid = ?",
                (soul_id, "u2"),
            )
            assert not rows
        merged_a = await self.relation.get_user("u1", soul_id="soul-a")
        assert merged_a is not None
        # The global identity row of the secondary account is gone.
        assert await self.db.fetchone(
            "SELECT * FROM users WHERE global_uid = ?", ("u2",)
        ) is None

    async def test_delete_scoped_to_current_soul(self):
        # Give u2 a relation in soul-b first, so deleting from soul-a must keep the
        # global identity row (only the last relation removal deletes it).
        await self.relation.update_user_details(
            "u2", display_name="A-阿二", soul_id="soul-b"
        )
        resp = await self.call(
            "POST",
            self.api.api_users_update,
            body={"action": "delete", "soul_id": "soul-a", "global_uid": "u2"},
        )
        assert resp["status"] == "ok"
        assert "当前角色" in resp["message"]
        assert await self.relation.get_user("u2", soul_id="soul-a") is None
        assert await self.db.fetchone(
            "SELECT * FROM users WHERE global_uid = ?", ("u2",)
        ) is not None
        # u1 still exists in both Souls.
        assert await self.relation.get_user("u1", soul_id="soul-b") is not None


if __name__ == "__main__":
    unittest.main()
