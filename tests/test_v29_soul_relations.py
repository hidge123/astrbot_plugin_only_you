"""v2.9 user identity (global) vs relationship state (per Soul) regression tests.

Covers the schema/CRUD/migration split:
- ``users`` = global identity (global_uid / display_name / platform_ids);
- ``user_relations`` = per-``(soul_id, global_uid)`` tier/affinity/interactions/
  nickname/attitude;
- legacy no-``soul_id`` callers keep the old ``users``-column behavior;
- one-shot migration splits legacy rows by platform -> Soul bindings;
- ``delete_soul`` cascades relationship rows.
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _helpers import (  # noqa: E402
    DatabaseAsyncTestCase,
    build_fake_plugin,
    load_plugin_modules,
    make_plugin_config,
    make_soul,
)

mods = load_plugin_modules()
UserProfile = mods.user_relation.UserProfile
UserRelationManager = mods.user_relation.UserRelationManager
UserTier = mods.state_machine.UserTier


class _Bridge:
    """PlatformBridge stand-in with configurable platform admins."""

    def __init__(self, per_platform=None):
        self.per_platform = dict(per_platform or {})

    def get_platform_admins(self, umo=None):
        platform_id = str(umo or "").split(":", 1)[0].strip()
        return list(self.per_platform.get(platform_id, []))


class _Event:
    def __init__(self, umo, sender_id, sender_name="阿一", admin=False):
        self.unified_msg_origin = umo
        self._sender_id = sender_id
        self._sender_name = sender_name
        self._admin = admin

    def get_sender_id(self):
        return self._sender_id

    def get_sender_name(self):
        return self._sender_name

    def is_admin(self):
        return self._admin


class SoulRelationsTestBase(DatabaseAsyncTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.config = make_plugin_config()
        self.bridge = _Bridge()
        self.relation = UserRelationManager(self.db, self.bridge, self.config)

    async def make_user(self, uid="u1", soul_id="soul-a", **kwargs):
        return await self.relation.get_or_create_user(
            uid, kwargs.pop("display_name", "阿一"), soul_id=soul_id, **kwargs
        )

    async def relation_row(self, soul_id, uid):
        return await self.db.fetchone(
            "SELECT * FROM user_relations WHERE soul_id = ? AND global_uid = ?",
            (soul_id, uid),
        )

    async def user_row(self, uid):
        return await self.db.fetchone(
            "SELECT * FROM users WHERE global_uid = ?", (uid,)
        )


class TestUserRelationsSchema(SoulRelationsTestBase):
    async def test_relation_table_exists(self):
        rows = await self.db.fetchall("PRAGMA table_info(user_relations)")
        names = {str(row.get("name") or "") for row in rows or []}
        assert names == {
            "soul_id",
            "global_uid",
            "tier",
            "interaction_count",
            "affinity",
            "nickname",
            "attitude",
            "updated_at",
        }

    async def test_soul_scoped_user_creates_identity_and_relation(self):
        user = await self.make_user("u1", "soul-a", display_name="阿一")
        assert user.global_uid == "u1"
        assert user.soul_id == "soul-a"
        identity = await self.user_row("u1")
        assert identity is not None
        relation = await self.relation_row("soul-a", "u1")
        assert relation is not None
        assert user.to_dict()["soul_id"] == "soul-a"

    async def test_soul_relation_fields_are_independent(self):
        a = await self.relation.update_user_details(
            "u1",
            display_name="阿一",
            nickname="小一",
            tier=3,
            affinity=42,
            attitude="对 A 角色很亲近",
            soul_id="soul-a",
        )
        b = await self.relation.update_user_details(
            "u1",
            nickname="一号",
            tier=1,
            affinity=3,
            attitude="对 B 角色很客气",
            soul_id="soul-b",
        )
        assert a.tier == UserTier.L3_CORE
        assert b.tier == UserTier.L1_NORMAL
        assert (a.nickname, b.nickname) == ("小一", "一号")
        assert (a.attitude, b.attitude) == ("对 A 角色很亲近", "对 B 角色很客气")
        assert (a.affinity, b.affinity) == (42.0, 3.0)
        # display_name is a global identity field
        for soul in ("soul-a", "soul-b"):
            loaded = await self.relation.get_user("u1", soul_id=soul)
            assert loaded.display_name == "阿一"

    async def test_bump_interaction_only_current_soul(self):
        await self.make_user("u1", "soul-a")
        await self.make_user("u1", "soul-b")
        await self.relation.get_or_create_user(
            "u1", soul_id="soul-a", bump_interaction=True
        )
        rel_a = await self.relation.get_user("u1", soul_id="soul-a")
        rel_b = await self.relation.get_user("u1", soul_id="soul-b")
        assert rel_a.interaction_count == 1
        assert rel_b.interaction_count == 0
        assert rel_a.soul_id == "soul-a"

    async def test_auto_promotion_is_per_soul(self):
        await self.make_user("u1", "soul-a")
        await self.make_user("u1", "soul-b")
        for _ in range(self.relation.AUTO_PROMOTE_INTERACTION_THRESHOLD):
            await self.relation.get_or_create_user(
                "u1", soul_id="soul-a", bump_interaction=True
            )
        rel_a = await self.relation.get_user("u1", soul_id="soul-a")
        rel_b = await self.relation.get_user("u1", soul_id="soul-b")
        assert rel_a.tier == UserTier.L2_IMPORTANT
        assert rel_b.tier == UserTier.L1_NORMAL


class TestResolveUserSoulScope(SoulRelationsTestBase):
    async def test_resolve_user_passes_soul_scope(self):
        user = await self.relation.resolve_user(
            _Event("qq bot:FriendMessage:42", "42"), soul_id="酒狐"
        )
        assert user.soul_id == "酒狐"
        assert user.global_uid == "qq bot:42"
        relation = await self.relation_row("酒狐", "qq bot:42")
        assert relation is not None
        # Legacy users columns stay untouched by Soul-scoped writes.
        identity = await self.user_row("qq bot:42")
        assert int(identity["interaction_count"]) == 0

    async def test_tier_change_is_isolated_per_soul(self):
        bridge = _Bridge(per_platform={"qq bot": ["42"]})
        relation = UserRelationManager(self.db, bridge, self.config)
        event = _Event("qq bot:FriendMessage:42", "42")
        a = await relation.resolve_user(event, soul_id="soul-a")
        assert a.tier == UserTier.L3_CORE
        # A manual tier change in another Soul must not leak into soul-a's row.
        await relation.update_user_details("qq bot:42", tier=1, soul_id="soul-b")
        assert (await relation.get_user("qq bot:42", soul_id="soul-a")).tier == UserTier.L3_CORE
        assert (await relation.get_user("qq bot:42", soul_id="soul-b")).tier == UserTier.L1_NORMAL

    async def test_legacy_callers_still_use_users_columns(self):
        user = await self.relation.get_or_create_user(
            "legacy", "旧用户", bump_interaction=True
        )
        assert user.soul_id == ""
        row = await self.user_row("legacy")
        assert int(row["interaction_count"]) == 1
        count = await self.db.fetchone("SELECT COUNT(*) AS c FROM user_relations")
        assert int(count["c"]) == 0


class TestListAndGetUsers(SoulRelationsTestBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.relation.update_user_details(
            "u1", display_name="阿一", tier=3, soul_id="soul-a"
        )
        await self.relation.update_user_details(
            "u2", display_name="阿二", tier=2, soul_id="soul-a"
        )
        await self.relation.update_user_details(
            "u3", display_name="阿三", tier=1, soul_id="soul-b"
        )

    async def test_list_users_scoped_to_soul(self):
        users_a = await self.relation.list_users(soul_id="soul-a")
        users_b = await self.relation.list_users(soul_id="soul-b")
        assert {u.global_uid for u in users_a} == {"u1", "u2"}
        assert {u.global_uid for u in users_b} == {"u3"}
        assert [u.tier for u in users_a] == [
            UserTier.L3_CORE,
            UserTier.L2_IMPORTANT,
        ]

    async def test_list_users_tier_filter_scoped(self):
        users = await self.relation.list_users(tier_filter=2, soul_id="soul-a")
        assert [u.global_uid for u in users] == ["u2"]

    async def test_legacy_list_users_still_returns_identity_rows(self):
        users = await self.relation.list_users()
        assert {u.global_uid for u in users} == {"u1", "u2", "u3"}
        # Legacy view reads the legacy users columns (all still L1 defaults here).
        assert all(u.soul_id == "" for u in users)

    async def test_get_user_without_relation_returns_none(self):
        assert await self.relation.get_user("u1", soul_id="soul-none") is None
        loaded = await self.relation.get_user("u1", soul_id="soul-a")
        assert loaded.tier == UserTier.L3_CORE


class TestUpdateAndDeleteScoping(SoulRelationsTestBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.relation.update_user_details(
            "u1", display_name="阿一", tier=3, soul_id="soul-a"
        )
        await self.relation.update_user_details(
            "u1", display_name="阿一", tier=1, soul_id="soul-b"
        )

    async def test_update_does_not_touch_other_soul(self):
        await self.relation.update_user_details(
            "u1", tier=1, nickname="普通朋友", soul_id="soul-b"
        )
        rel_a = await self.relation_row("soul-a", "u1")
        rel_b = await self.relation_row("soul-b", "u1")
        assert int(rel_a["tier"]) == 3
        assert int(rel_b["tier"]) == 1
        assert rel_b["nickname"] == "普通朋友"

    async def test_delete_removes_only_current_soul_relation(self):
        await self.relation.delete_user("u1", soul_id="soul-a")
        assert await self.relation_row("soul-a", "u1") is None
        assert await self.relation_row("soul-b", "u1") is not None
        assert await self.user_row("u1") is not None
        assert await self.relation.get_user("u1", soul_id="soul-b") is not None

    async def test_delete_last_relation_removes_identity(self):
        await self.relation.delete_user("u1", soul_id="soul-a")
        await self.relation.delete_user("u1", soul_id="soul-b")
        assert await self.relation_row("soul-b", "u1") is None
        assert await self.user_row("u1") is None

    async def test_legacy_delete_removes_identity_and_relations(self):
        await self.relation.delete_user("u1")
        assert await self.user_row("u1") is None
        rows = await self.db.fetchall(
            "SELECT * FROM user_relations WHERE global_uid = ?", ("u1",)
        )
        assert not rows


class TestMergeScoping(SoulRelationsTestBase):
    async def test_merge_requires_existing_relation_in_current_soul(self):
        await self.relation.update_user_details(
            "p", display_name="主号", soul_id="soul-a"
        )
        await self.relation.update_user_details(
            "s", display_name="副号", soul_id="soul-b"
        )
        with self.assertRaises(ValueError):
            await self.relation.merge_users("p", "s", soul_id="soul-a")

    async def test_merge_same_user_rejected(self):
        await self.relation.update_user_details(
            "p", display_name="主号", soul_id="soul-a"
        )
        with self.assertRaises(ValueError):
            await self.relation.merge_users("p", "p", soul_id="soul-a")

    async def test_merge_merges_relations_per_soul_and_memories(self):
        await self.relation.update_user_details(
            "p", display_name="主号", tier=2, affinity=10, soul_id="soul-a"
        )
        await self.relation.update_user_details(
            "p", display_name="主号", tier=1, affinity=2, soul_id="soul-b"
        )
        await self.relation.update_user_details(
            "s", display_name="副号", tier=3, affinity=30, soul_id="soul-a"
        )
        await self.relation.update_user_details(
            "s", display_name="副号", tier=2, affinity=8, soul_id="soul-b"
        )
        await self.db.execute(
            "INSERT INTO memories (soul_id, user_id, user_tier, memory_tier, content, "
            "keywords, importance, vitality, access_count, created_at, last_accessed_at) "
            "VALUES ('soul-a', 's', 1, 1, '副号的记忆', '', 4, 100, 0, 1, 1)"
        )
        merged = await self.relation.merge_users("p", "s", soul_id="soul-a")
        assert merged.global_uid == "p"
        assert merged.tier == UserTier.L3_CORE
        rel_a = await self.relation_row("soul-a", "p")
        rel_b = await self.relation_row("soul-b", "p")
        assert int(rel_a["tier"]) == 3
        assert int(rel_b["tier"]) == 2
        assert await self.relation_row("soul-a", "s") is None
        assert await self.relation_row("soul-b", "s") is None
        assert await self.user_row("s") is None
        memory = await self.db.fetchone(
            "SELECT user_id FROM memories WHERE content = '副号的记忆'"
        )
        assert memory["user_id"] == "p"


class TestMigration(SoulRelationsTestBase):
    async def seed_legacy_user(self, uid, pids, tier=1, affinity=0.0):
        await self.db.execute(
            """
            INSERT INTO users (
                global_uid, display_name, nickname, tier, interaction_count,
                affinity, platform_ids_json, attitude, updated_at
            ) VALUES (?, ?, '旧称呼', ?, 5, ?, ?, '旧态度', 1)
            """,
            (
                uid,
                uid,
                int(tier),
                affinity,
                json.dumps(pids, ensure_ascii=False),
            ),
        )

    async def test_migration_splits_by_platform_binding(self):
        await self.seed_legacy_user("qq bot:42", ["qq bot:42"], tier=3, affinity=12)
        await self.seed_legacy_user(
            "multi", ["qq bot:42", "telegram:42"], tier=2, affinity=5
        )
        stats = await self.relation.migrate_user_relations(
            {"qq bot": "soul-a", "telegram": "soul-b"}, "soul-a", ["soul-a", "soul-b"]
        )
        assert stats["users_processed"] == 2
        assert stats["relations_created"] == 3
        qq_rel = await self.relation_row("soul-a", "qq bot:42")
        assert int(qq_rel["tier"]) == 3
        assert qq_rel["nickname"] == "旧称呼"
        multi_a = await self.relation_row("soul-a", "multi")
        multi_b = await self.relation_row("soul-b", "multi")
        assert int(multi_a["tier"]) == 2 and int(multi_b["tier"]) == 2
        assert await self.db.get_setting("user_relations_migrated_v1", False) is True

    async def test_migration_is_idempotent(self):
        await self.seed_legacy_user("u1", ["qq bot:42"])
        first = await self.relation.migrate_user_relations(
            {"qq bot": "soul-a"}, "soul-a", ["soul-a"]
        )
        second = await self.relation.migrate_user_relations(
            {"qq bot": "soul-a"}, "soul-a", ["soul-a"]
        )
        assert first["relations_created"] == 1
        assert second == {"users_processed": 0, "relations_created": 0, "skipped": 0}

    async def test_migration_without_souls_retries_later(self):
        await self.seed_legacy_user("u1", ["qq bot:42"])
        stats = await self.relation.migrate_user_relations({}, "", [])
        assert stats == {"users_processed": 0, "relations_created": 0, "skipped": 0}
        assert await self.db.get_setting("user_relations_migrated_v1", False) is False

    async def test_bare_legacy_user_uses_persisted_umo_evidence(self):
        await self.seed_legacy_user("2975066889", ["2975066889"])
        await self.db.execute(
            "INSERT INTO umo_soul_bindings (umo, soul_id, updated_at) VALUES (?,?,?)",
            ("qq bot:FriendMessage:2975066889", "soul-qq", 1),
        )
        stats = await self.relation.migrate_user_relations(
            {"qq bot": "soul-qq"}, "soul-default", ["soul-qq", "soul-default"]
        )
        assert stats["relations_created"] == 1
        assert await self.relation_row("soul-qq", "2975066889") is not None
        assert await self.relation_row("soul-default", "2975066889") is None

    async def test_unbound_legacy_user_falls_back_to_default_soul(self):
        await self.seed_legacy_user("legacy-user", ["legacy-user"])
        stats = await self.relation.migrate_user_relations(
            {}, "soul-default", ["soul-default", "soul-other"]
        )
        assert stats["relations_created"] == 1
        assert await self.relation_row("soul-default", "legacy-user") is not None


class TestSoulDeletionCascade(DatabaseAsyncTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.plugin, self.api = await build_fake_plugin(self.db, self.tmpdir)

    async def test_delete_soul_cascades_user_relations(self):
        soul = make_soul("soul-del")
        await self.plugin.soul_manager.save_soul(soul)
        manager = self.plugin.user_relation
        await manager.update_user_details(
            "u1", display_name="阿一", tier=2, soul_id="soul-del"
        )
        assert await manager.get_user("u1", soul_id="soul-del") is not None
        # main.initialize() wires this callback; the FakePlugin does not run it.
        self.plugin.soul_manager.on_soul_deleted = manager.purge_soul_cache
        await self.plugin.soul_manager.delete_soul("soul-del")
        assert await manager.get_user("u1", soul_id="soul-del") is None
        # Identity row survives (other Souls may still know this person).
        assert await self.db.fetchone(
            "SELECT * FROM users WHERE global_uid = ?", ("u1",)
        ) is not None


if __name__ == "__main__":
    unittest.main()
