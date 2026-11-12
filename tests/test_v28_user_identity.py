"""v2.8.4 user platform-identity regression tests.

Covers the legacy bare-``global_uid`` problem introduced by the v2.8 platform
binding refactor:

- canonical identity is ``platform_id:native_uid`` (e.g. ``qq bot:2975066889``),
  derived from the event UMO prefix + sender id;
- legacy profiles storing only the bare sender id are healed lazily on the next
  message (and via a non-destructive startup migration using persisted UMO evidence);
- the console can delete / complete legacy platform ids (they are no longer force
  re-inserted by ``update_user_details``);
- cross-platform native-id collisions never overwrite another profile's cache;
- merge / delete / normalize APIs keep memories and aliases consistent.
"""

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
)

mods = load_plugin_modules()
UserProfile = mods.user_relation.UserProfile
UserRelationManager = mods.user_relation.UserRelationManager
UserTier = mods.state_machine.UserTier


class _Bridge:
    """PlatformBridge stand-in with configurable platform admins."""

    def __init__(self, admins=None, per_platform=None):
        self.admins = list(admins or [])
        self.per_platform = dict(per_platform or {})

    def get_platform_admins(self, umo=None):
        if umo is not None:
            platform_id = str(umo).split(":", 1)[0].strip()
            return list(self.per_platform.get(platform_id, self.admins))
        return list(self.admins)


class _Event:
    """Minimal AstrMessageEvent stand-in for ``resolve_user``."""

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


class UserIdentityTestBase(DatabaseAsyncTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.config = make_plugin_config()
        self.bridge = _Bridge()
        self.relation = UserRelationManager(self.db, self.bridge, self.config)

    async def seed_user(self, global_uid, platform_ids, display_name="", tier=1):
        await self.db.execute(
            """
            INSERT INTO users (
                global_uid, display_name, nickname, tier, interaction_count,
                affinity, platform_ids_json, attitude, updated_at
            ) VALUES (?, ?, '', ?, 0, 0.0, ?, '', ?)
            """,
            (
                global_uid,
                display_name or global_uid,
                int(tier),
                json.dumps(platform_ids, ensure_ascii=False),
                1,
            ),
        )

    async def fetch_user_row(self, global_uid):
        return await self.db.fetchone(
            "SELECT * FROM users WHERE global_uid = ?", (global_uid,)
        )


class TestCanonicalIdentity(UserIdentityTestBase):
    async def test_new_user_gets_platform_qualified_account(self):
        user = await self.relation.get_or_create_user(
            "2975066889",
            "hidge-1",
            umo="qq bot:FriendMessage:2975066889",
            bump_interaction=True,
        )
        assert user.global_uid == "qq bot:2975066889"
        assert user.platform_ids == ["qq bot:2975066889"]
        assert user.to_dict()["platform_accounts"] == [
            {
                "account": "qq bot:2975066889",
                "platform_id": "qq bot",
                "uid": "2975066889",
                "legacy": False,
            }
        ]

    async def test_explicit_platform_id_argument_wins(self):
        user = await self.relation.get_or_create_user(
            "42", "阿二", platform_id="telegram"
        )
        assert user.global_uid == "telegram:42"
        assert user.platform_ids == ["telegram:42"]

    async def test_same_native_id_on_two_platforms_are_distinct_users(self):
        qq_user = await self.relation.get_or_create_user(
            "123", "QQ 阿三", umo="qq bot:FriendMessage:123"
        )
        tg_user = await self.relation.get_or_create_user(
            "123", "TG 阿三", umo="telegram:FriendMessage:123"
        )
        assert qq_user.global_uid == "qq bot:123"
        assert tg_user.global_uid == "telegram:123"
        assert qq_user.global_uid != tg_user.global_uid
        users = await self.relation.list_users()
        assert {user.global_uid for user in users} == {
            "qq bot:123",
            "telegram:123",
        }

    async def test_resolve_user_uses_umo_platform(self):
        user = await self.relation.resolve_user(
            _Event("qq bot:GroupMessage:1039802291", "2975066889", "hidge-1")
        )
        assert user.global_uid == "qq bot:2975066889"
        assert user.platform_ids == ["qq bot:2975066889"]

    async def test_platform_admin_with_bare_id_is_l3(self):
        bridge = _Bridge(per_platform={"qq bot": ["2975066889"]})
        relation = UserRelationManager(self.db, bridge, self.config)
        user = await relation.resolve_user(
            _Event("qq bot:FriendMessage:2975066889", "2975066889")
        )
        assert user.tier == UserTier.L3_CORE


class TestLegacyHealing(UserIdentityTestBase):
    async def test_legacy_bare_id_upgraded_on_next_message(self):
        await self.seed_user("2975066889", ["2975066889"])
        user = await self.relation.get_or_create_user(
            "2975066889",
            "hidge-1",
            umo="qq bot:FriendMessage:2975066889",
            bump_interaction=True,
        )
        # global_uid stays the legacy primary key (memories keep pointing at it) but
        # the platform account list is upgraded and the ghost bare id removed.
        assert user.global_uid == "2975066889"
        assert user.platform_ids == ["qq bot:2975066889"]
        row = await self.fetch_user_row("2975066889")
        assert json.loads(row["platform_ids_json"]) == ["qq bot:2975066889"]

    async def test_healing_keeps_other_platform_accounts(self):
        await self.seed_user(
            "2975066889", ["2975066889", "telegram:2975066889"]
        )
        user = await self.relation.get_or_create_user(
            "2975066889", umo="qq bot:FriendMessage:2975066889"
        )
        # The bare legacy entry was shadowed/dropped by the qualified telegram one;
        # the freshly proven QQ platform account is added.
        assert set(user.platform_ids) == {
            "qq bot:2975066889",
            "telegram:2975066889",
        }

    async def test_ambiguous_bare_id_prefers_exact_primary_key_profile(self):
        await self.seed_user("123", ["123"], "主档案")
        await self.seed_user("another", ["123"], "另一个档案")
        user = await self.relation.get_or_create_user(
            "123", umo="qq bot:FriendMessage:123"
        )
        assert user.global_uid == "123"
        assert "qq bot:123" in user.platform_ids

    async def test_legacy_profile_without_umo_is_untouched(self):
        await self.seed_user("legacy-user", ["legacy-user"])
        user = await self.relation.get_or_create_user("legacy-user", "旧用户")
        assert user.global_uid == "legacy-user"
        assert user.platform_ids == ["legacy-user"]


class TestPlatformIdEditing(UserIdentityTestBase):
    async def test_update_can_remove_legacy_id(self):
        await self.seed_user(
            "2975066889", ["2975066889", "qq bot:2975066889"]
        )
        updated = await self.relation.update_user_details(
            "2975066889", platform_ids=["qq bot:2975066889"]
        )
        assert updated.platform_ids == ["qq bot:2975066889"]
        row = await self.fetch_user_row("2975066889")
        assert json.loads(row["platform_ids_json"]) == ["qq bot:2975066889"]

    async def test_update_drops_shadowed_bare_id_automatically(self):
        await self.seed_user("42", ["42"])
        updated = await self.relation.update_user_details(
            "42", platform_ids=["42", "telegram:42"]
        )
        assert updated.platform_ids == ["telegram:42"]

    async def test_update_accepts_account_objects(self):
        await self.seed_user("42", ["qq bot:42"])
        updated = await self.relation.update_user_details(
            "42",
            platform_ids=[
                {"platform_id": "qq bot", "native_uid": "42"},
                {"platform_id": "telegram", "native_uid": "42"},
            ],
        )
        assert updated.platform_ids == ["qq bot:42", "telegram:42"]

    async def test_empty_platform_ids_rejected(self):
        await self.seed_user("42", ["qq bot:42"])
        with self.assertRaises(ValueError):
            await self.relation.update_user_details("42", platform_ids=[])

    async def test_normalize_identity_with_explicit_platform(self):
        await self.seed_user("2975066889", ["2975066889"])
        user = await self.relation.normalize_user_identity(
            "2975066889", platform_id="qq bot"
        )
        assert user.platform_ids == ["qq bot:2975066889"]

    async def test_normalize_identity_infers_from_umo_evidence(self):
        await self.seed_user("2975066889", ["2975066889"])
        await self.db.execute(
            "INSERT INTO umo_soul_bindings (umo, soul_id, updated_at) VALUES (?,?,?)",
            ("qq bot:FriendMessage:2975066889", "soul_a", 1),
        )
        user = await self.relation.normalize_user_identity("2975066889")
        assert user.platform_ids == ["qq bot:2975066889"]

    async def test_deleted_user_cache_aliases_removed(self):
        user = await self.relation.get_or_create_user(
            "2975066889", umo="qq bot:FriendMessage:2975066889"
        )
        assert self.relation._cache.get("qq bot:2975066889") is user
        await self.relation.delete_user(user.global_uid)
        assert self.relation._cache.get("qq bot:2975066889") is None
        assert await self.fetch_user_row(user.global_uid) is None


class TestCacheCollisionGuard(UserIdentityTestBase):
    async def test_shared_bare_id_does_not_overwrite_other_profile_cache(self):
        first = UserProfile(
            global_uid="first", display_name="A", platform_ids=["123"]
        )
        second = UserProfile(
            global_uid="second", display_name="B", platform_ids=["123"]
        )
        await self.relation.save_user(first)
        await self.relation.save_user(second)
        assert self.relation._cache["123"] is first


class TestLegacyIdentityMigration(UserIdentityTestBase):
    async def test_migration_uses_private_umo_evidence(self):
        await self.seed_user("2975066889", ["2975066889"])
        await self.seed_user("no-evidence", ["no-evidence"])
        await self.db.execute(
            "INSERT INTO umo_soul_bindings (umo, soul_id, updated_at) VALUES (?,?,?)",
            ("qq bot:FriendMessage:2975066889", "soul_a", 1),
        )
        stats = await self.relation.migrate_legacy_user_identities()
        assert stats["accounts_converted"] == 1
        assert stats["users_updated"] == 1
        assert stats["ambiguous"] == 1  # the row without evidence
        migrated = await self.fetch_user_row("2975066889")
        assert json.loads(migrated["platform_ids_json"]) == [
            "qq bot:2975066889"
        ]
        untouched = await self.fetch_user_row("no-evidence")
        assert json.loads(untouched["platform_ids_json"]) == ["no-evidence"]
        assert await self.db.get_setting(
            mods.user_relation.LEGACY_USER_IDENTITY_MIGRATED_KEY, False
        ) is True

    async def test_migration_reads_runtime_state_known_l3_umos(self):
        await self.seed_user("555", ["555"])
        await self.db.set_setting(
            "runtime_state_v1",
            {"known_l3_umos": {"555": "webchat:FriendMessage:555"}},
        )
        stats = await self.relation.migrate_legacy_user_identities()
        assert stats["accounts_converted"] == 1
        row = await self.fetch_user_row("555")
        assert json.loads(row["platform_ids_json"]) == ["webchat:555"]

    async def test_migration_is_idempotent(self):
        await self.seed_user("2975066889", ["2975066889"])
        await self.db.execute(
            "INSERT INTO umo_soul_bindings (umo, soul_id, updated_at) VALUES (?,?,?)",
            ("qq bot:FriendMessage:2975066889", "soul_a", 1),
        )
        first = await self.relation.migrate_legacy_user_identities()
        second = await self.relation.migrate_legacy_user_identities()
        assert first["accounts_converted"] == 1
        assert second == {
            "users_updated": 0,
            "accounts_converted": 0,
            "ambiguous": 0,
        }

    async def test_group_umo_is_not_used_as_evidence(self):
        await self.seed_user("123", ["123"])
        await self.db.execute(
            "INSERT INTO umo_soul_bindings (umo, soul_id, updated_at) VALUES (?,?,?)",
            ("qq bot:GroupMessage:123", "soul_a", 1),  # 123 is a group id here
        )
        stats = await self.relation.migrate_legacy_user_identities()
        assert stats["accounts_converted"] == 0
        row = await self.fetch_user_row("123")
        assert json.loads(row["platform_ids_json"]) == ["123"]


class TestMergeWithPlatformAccounts(UserIdentityTestBase):
    async def test_merge_combines_qualified_accounts_and_memories(self):
        primary = await self.relation.get_or_create_user(
            "2975066889", "主档案", umo="qq bot:FriendMessage:2975066889"
        )
        secondary = await self.relation.get_or_create_user(
            "999", "TG 档案", umo="telegram:FriendMessage:999"
        )
        await self.db.execute(
            "INSERT INTO memories (soul_id, user_id, user_tier, memory_tier, content, keywords, importance, vitality, access_count, created_at, last_accessed_at) "
            "VALUES ('s', ?, 1, 1, '测试记忆', '', 4, 100, 0, 1, 1)",
            (secondary.global_uid,),
        )
        merged = await self.relation.merge_users(
            primary.global_uid, secondary.global_uid
        )
        assert merged.global_uid == primary.global_uid
        assert set(merged.platform_ids) == {
            "qq bot:2975066889",
            "telegram:999",
        }
        row = await self.db.fetchone(
            "SELECT user_id FROM memories WHERE content = '测试记忆'"
        )
        assert row["user_id"] == primary.global_uid
        assert await self.fetch_user_row(secondary.global_uid) is None


class TestUserIdentityWebApi(DatabaseAsyncTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.plugin, self.api = await build_fake_plugin(self.db, self.tmpdir)

    async def call(self, method, handler, query=None, body=None):
        self.api.set_mock_request(method, query=query or {}, body=body or {})
        return await handler()

    async def seed_user(self, global_uid, platform_ids, display_name=""):
        await self.db.execute(
            """
            INSERT INTO users (
                global_uid, display_name, nickname, tier, interaction_count,
                affinity, platform_ids_json, attitude, updated_at
            ) VALUES (?, ?, '', 1, 1, 0.0, ?, '', 1)
            """,
            (
                global_uid,
                display_name or global_uid,
                json.dumps(platform_ids, ensure_ascii=False),
            ),
        )

    async def test_users_list_exposes_platform_accounts_and_legacy_flags(self):
        await self.seed_user("legacy-user", ["legacy-user"])
        response = await self.call("GET", self.api.api_users_list)
        assert response["status"] == "ok"
        user = next(
            item
            for item in response["data"]["users"]
            if item["global_uid"] == "legacy-user"
        )
        assert user["has_legacy_platform_ids"] is True
        assert user["legacy_platform_ids"] == ["legacy-user"]
        assert user["platform_accounts"] == [
            {
                "account": "legacy-user",
                "platform_id": "",
                "uid": "legacy-user",
                "legacy": True,
            }
        ]

    async def test_users_list_drops_bare_id_shadowed_by_qualified_account(self):
        await self.seed_user(
            "2975066889", ["2975066889", "qq bot:2975066889"]
        )
        response = await self.call("GET", self.api.api_users_list)
        user = next(
            item
            for item in response["data"]["users"]
            if item["global_uid"] == "2975066889"
        )
        # The bare duplicate is shadowed by the qualified account and hidden.
        assert user["platform_ids"] == ["qq bot:2975066889"]
        assert user["has_legacy_platform_ids"] is False

    async def test_users_update_removes_legacy_id(self):
        await self.seed_user(
            "2975066889", ["2975066889", "qq bot:2975066889"]
        )
        response = await self.call(
            "POST",
            self.api.api_users_update,
            body={
                "global_uid": "2975066889",
                "platform_ids": ["qq bot:2975066889"],
            },
        )
        assert response["status"] == "ok"
        assert response["data"]["user"]["platform_ids"] == [
            "qq bot:2975066889"
        ]
        assert response["data"]["user"]["has_legacy_platform_ids"] is False

    async def test_users_update_accepts_newline_text(self):
        await self.seed_user("42", ["qq bot:42"])
        response = await self.call(
            "POST",
            self.api.api_users_update,
            body={"global_uid": "42", "platform_ids": "qq bot:42\ntelegram:42"},
        )
        assert response["status"] == "ok"
        assert response["data"]["user"]["platform_ids"] == [
            "qq bot:42",
            "telegram:42",
        ]

    async def test_users_update_empty_platform_ids_is_error(self):
        await self.seed_user("42", ["qq bot:42"])
        response = await self.call(
            "POST",
            self.api.api_users_update,
            body={"global_uid": "42", "platform_ids": []},
        )
        assert response["status"] == "error"
        assert "至少保留一个平台账号" in response["message"]

    async def test_normalize_identity_action(self):
        await self.seed_user("2975066889", ["2975066889"])
        response = await self.call(
            "POST",
            self.api.api_users_update,
            body={
                "action": "normalize_identity",
                "global_uid": "2975066889",
                "platform_id": "qq bot",
            },
        )
        assert response["status"] == "ok"
        assert response["data"]["user"]["platform_ids"] == [
            "qq bot:2975066889"
        ]


if __name__ == "__main__":
    unittest.main()
