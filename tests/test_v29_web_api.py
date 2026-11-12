"""Offline tests for the **v2.9** Web API per-Soul relationship scoping (contract §4).

Covers:
- ``users/list?soul_id=``: users / counts / groups scoped to that Soul, ``data.soul_id``
  echo, per-Soul tier/affinity/nickname/attitude read-back, tier filter, unknown scope,
  and the legacy global view without ``soul_id`` (exact v2.8 behavior);
- ``users/update``: ``update`` writes relation fields only into the current Soul's
  ``user_relations`` row while identity ``display_name`` / ``platform_ids`` stay global;
  ``merge`` validation (both targets must already exist in the current Soul's relation
  list, same user rejected, cross-Soul picks rejected) and the scoped response payload;
  ``delete`` removes only the current role's relation (identity + other role rows +
  memories preserved) with the role-scoped message; ``normalize_identity`` stays
  identity-global and echoes ``soul_id``;
- ``_group_items(soul)``: groups filtered by ``bound_soul_id`` or by one of the Soul's
  bound platforms (covers groups whose ``bound_soul_id`` was never backfilled);
- ``dashboard/overview`` relation counters scoped by the request ``soul_id``;
- v2.8 legacy compatibility: without ``soul_id`` users/counts/groups stay global and
  the old update / merge / delete semantics are unchanged.

The fake plugin assembles the **real** business components against a temporary SQLite
database (``tests/_helpers.build_fake_plugin``); everything is offline (zero network).
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _helpers import (  # noqa: E402
    DatabaseAsyncTestCase,
    FakeChatProvider,
    FakeTTSProvider,
    build_fake_plugin,
    card_with,
    load_plugin_modules,
    make_soul,
)

mods = load_plugin_modules()
MERGE_SCOPE_ERROR = "合并目标必须是当前角色关系列表中的已有用户"

GROUP_A_UMO = "aiocqhttp:GroupMessage:111"
GROUP_B_UMO = "aiocqhttp:GroupMessage:222"
GROUP_PLATFORM_UMO = "telegram_bot_1:GroupMessage:333"
GROUP_UNBOUND_UMO = "aiocqhttp:GroupMessage:444"


class WebApiV29TestBase(DatabaseAsyncTestCase):
    """Fake plugin + Web API assembled from the real v2.9 business components."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        FakeChatProvider.reset()
        FakeTTSProvider.reset()
        FakeTTSProvider.audio_dir = self.tmpdir
        self.plugin, self.api = await build_fake_plugin(self.db, self.tmpdir)
        # main.py wires ModelHub -> SoulManager right after constructing both; mirror
        # it so the soul-scoped helpers see the same state as production.
        attach = getattr(self.plugin.model_hub, "attach_soul_manager", None)
        if callable(attach):
            attach(self.plugin.soul_manager)

    # ------------------------------------------------------------------
    # API helpers
    # ------------------------------------------------------------------
    async def call(self, method, handler, query=None, body=None):
        self.api.set_mock_request(method, query=query or {}, body=body or {})
        return await handler()

    def assert_ok(self, response):
        assert response["status"] == "ok", response
        assert isinstance(response["message"], str)
        assert isinstance(response["data"], dict)
        return response["data"]

    def assert_error(self, response, expected_message=None):
        assert response["status"] == "error", response
        assert isinstance(response["message"], str) and response["message"]
        assert isinstance(response["data"], dict)
        if expected_message is not None:
            assert expected_message in response["message"], response["message"]
        return response

    # ------------------------------------------------------------------
    # Fixture helpers
    # ------------------------------------------------------------------
    async def create_soul(self, soul_id, name="测试角色"):
        soul = make_soul(
            soul_id=soul_id,
            card=card_with(
                identity={"name": name, "tagline": "v2.9 关系隔离测试"},
                speech={"speaking_style": "简短"},
            ),
        )
        await self.plugin.soul_manager.save_soul(soul)
        return soul

    async def seed_relation(
        self,
        soul_id,
        native_uid,
        display_name=None,
        tier=None,
        affinity=None,
        attitude=None,
        nickname=None,
        platform="aiocqhttp",
    ):
        """Create (or reuse) the global identity and seed its per-Soul relation row."""
        relation = self.plugin.user_relation
        profile = await relation.get_or_create_user(
            native_uid,
            display_name or f"用户{native_uid}",
            umo=f"{platform}:FriendMessage:{native_uid}",
            soul_id=soul_id,
        )
        updates = {"soul_id": soul_id}
        if tier is not None:
            updates["tier"] = tier
        if affinity is not None:
            updates["affinity"] = affinity
        if attitude is not None:
            updates["attitude"] = attitude
        if nickname is not None:
            updates["nickname"] = nickname
        if len(updates) > 1:
            profile = await relation.update_user_details(profile.global_uid, **updates)
        return profile

    async def list_scoped(self, soul_id, tier=None):
        query = {"soul_id": soul_id}
        if tier is not None:
            query["tier"] = str(tier)
        return self.assert_ok(
            await self.call("GET", self.api.api_users_list, query=query)
        )

    async def list_legacy(self, tier=None):
        query = {"tier": str(tier)} if tier is not None else {}
        return self.assert_ok(
            await self.call("GET", self.api.api_users_list, query=query)
        )

    async def update_users(self, body):
        return await self.call("POST", self.api.api_users_update, body=body)

    async def fetch_user_row(self, global_uid):
        return await self.db.fetchone(
            "SELECT * FROM users WHERE global_uid = ?", (global_uid,)
        )

    async def relation_rows(self, soul_id):
        return await self.db.fetchall(
            "SELECT * FROM user_relations WHERE soul_id = ? ORDER BY global_uid",
            (soul_id,),
        )

    async def record_window(self, umo, content="测试消息", unread=False):
        buffer = self.plugin.session_buffer
        user = await self.plugin.user_relation.get_or_create_user("u-tester", "测试群友")
        message = buffer.record_user_message(umo=umo, user=user, content=content)
        if unread and message is not None:
            buffer.mark_unread(umo, message)
        return message

    async def bind_platform(self, soul_id, platform_id):
        return await self.plugin.soul_manager.bind_platform(soul_id, platform_id)


class TwoSoulTestBase(WebApiV29TestBase):
    """Two Souls with an overlapping user (u1) plus one Soul-exclusive user each."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.soul_a = await self.create_soul("soul_a", "角色A")
        self.soul_b = await self.create_soul("soul_b", "角色B")
        # Same global identity (aiocqhttp:1001), different relation state per Soul.
        self.u1_a = await self.seed_relation(
            "soul_a",
            "1001",
            "阿一",
            tier=3,
            affinity=30,
            attitude="A态度",
            nickname="A昵称",
        )
        self.u1_b = await self.seed_relation(
            "soul_b", "1001", "阿一", tier=2, affinity=10, attitude="B态度"
        )
        self.u2_a = await self.seed_relation("soul_a", "2002", "阿二")
        self.u3_b = await self.seed_relation("soul_b", "3003", "阿三")

    def uids(self, data):
        return {user["global_uid"] for user in data["users"]}


# ==============================================================================
# 1. users/list — per-Soul scoping (contract §4.1)
# ==============================================================================
class TestUsersListSoulScope(TwoSoulTestBase):
    async def test_scoped_users_counts_and_echo(self):
        assert self.u1_a.global_uid == self.u1_b.global_uid
        data = await self.list_scoped("soul_a")
        assert data["soul_id"] == "soul_a"
        assert self.uids(data) == {self.u1_a.global_uid, self.u2_a.global_uid}
        assert data["counts"] == {
            "l3_count": 1,
            "l2_count": 0,
            "l1_count": 1,
            "total": 2,
        }
        assert data["groups"] == []
        # Every returned profile carries the current scope.
        assert {user["soul_id"] for user in data["users"]} == {"soul_a"}

    async def test_same_user_reads_each_soul_relation(self):
        data_a = await self.list_scoped("soul_a")
        data_b = await self.list_scoped("soul_b")
        by_uid_a = {user["global_uid"]: user for user in data_a["users"]}
        by_uid_b = {user["global_uid"]: user for user in data_b["users"]}
        uid = self.u1_a.global_uid
        assert by_uid_a[uid]["tier"] == 3
        assert by_uid_a[uid]["attitude"] == "A态度"
        assert by_uid_a[uid]["nickname"] == "A昵称"
        assert by_uid_a[uid]["affinity"] == 30.0
        assert by_uid_b[uid]["tier"] == 2
        assert by_uid_b[uid]["attitude"] == "B态度"
        assert by_uid_b[uid]["affinity"] == 10.0
        # soul_b only has u1 + u3 (u2 is exclusive to soul_a).
        assert self.uids(data_b) == {uid, self.u3_b.global_uid}
        assert data_b["counts"] == {
            "l3_count": 0,
            "l2_count": 1,
            "l1_count": 1,
            "total": 2,
        }

    async def test_scoped_tier_filter_keeps_full_counts(self):
        data = await self.list_scoped("soul_a", tier=3)
        assert self.uids(data) == {self.u1_a.global_uid}
        # counts stay derived from the full scoped list (v2.8 behaviour).
        assert data["counts"]["total"] == 2
        assert data["counts"]["l3_count"] == 1
        assert data["counts"]["l1_count"] == 1

    async def test_unknown_soul_id_returns_empty_scope(self):
        data = self.assert_ok(
            await self.call(
                "GET", self.api.api_users_list, query={"soul_id": "ghost_soul"}
            )
        )
        assert data["soul_id"] == "ghost_soul"
        assert data["users"] == []
        assert data["counts"] == {
            "l3_count": 0,
            "l2_count": 0,
            "l1_count": 0,
            "total": 0,
        }
        assert data["groups"] == []

    async def test_legacy_global_view_without_soul_id(self):
        data = await self.list_legacy()
        assert data["soul_id"] == ""
        assert self.uids(data) == {
            self.u1_a.global_uid,
            self.u2_a.global_uid,
            self.u3_b.global_uid,
        }
        # Legacy `users` columns were never touched by the Soul-scoped writes.
        by_uid = {user["global_uid"]: user for user in data["users"]}
        legacy_u1 = by_uid[self.u1_a.global_uid]
        assert legacy_u1["tier"] == 1
        assert legacy_u1["attitude"] == ""
        assert legacy_u1["soul_id"] == ""
        assert data["counts"] == {
            "l3_count": 0,
            "l2_count": 0,
            "l1_count": 3,
            "total": 3,
        }

    async def test_legacy_tier_filter_without_soul_id(self):
        data = await self.list_legacy(tier=3)
        assert data["users"] == []
        assert data["counts"]["total"] == 3


# ==============================================================================
# 2. users/list — group scoping (contract §4.4)
# ==============================================================================
class TestUsersListGroupScope(TwoSoulTestBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        relation = self.plugin.user_relation
        self.group_a = await relation.touch_group(
            GROUP_A_UMO,
            group_id="111",
            platform_id="aiocqhttp",
            bound_soul_id="soul_a",
        )
        self.group_b = await relation.touch_group(
            GROUP_B_UMO,
            group_id="222",
            platform_id="aiocqhttp",
            bound_soul_id="soul_b",
        )
        # Legacy group: no bound_soul_id backfilled, only the platform binding proves
        # which Soul governs it.
        self.group_platform = await relation.touch_group(
            GROUP_PLATFORM_UMO, group_id="333", platform_id="telegram_bot_1"
        )
        # Completely unbound group (visible in the legacy global view only).
        self.group_unbound = await relation.touch_group(
            GROUP_UNBOUND_UMO, group_id="444", platform_id="aiocqhttp"
        )
        await self.bind_platform("soul_a", "telegram_bot_1")

    def group_umos(self, data):
        return {group["umo"] for group in data["groups"]}

    async def test_groups_scoped_by_bound_soul_id(self):
        data = await self.list_scoped("soul_a")
        # bound_soul_id == soul_a plus the legacy platform-bound group.
        assert self.group_umos(data) == {GROUP_A_UMO, GROUP_PLATFORM_UMO}
        for item in data["groups"]:
            assert item["umo"] in {GROUP_A_UMO, GROUP_PLATFORM_UMO}

    async def test_other_soul_sees_only_its_groups(self):
        data = await self.list_scoped("soul_b")
        assert self.group_umos(data) == {GROUP_B_UMO}
        assert data["groups"][0]["bound_soul_id"] == "soul_b"

    async def test_platform_bound_group_visible_without_backfilled_soul_id(self):
        data = await self.list_scoped("soul_a")
        by_umo = {group["umo"]: group for group in data["groups"]}
        item = by_umo[GROUP_PLATFORM_UMO]
        # The console fallback resolves the governing Soul for display purposes.
        assert item["bound_soul_id"] == "soul_a"
        assert item["bound_soul_name"] == "角色A"

    async def test_legacy_global_view_keeps_all_groups(self):
        data = await self.list_legacy()
        assert self.group_umos(data) == {
            GROUP_A_UMO,
            GROUP_B_UMO,
            GROUP_PLATFORM_UMO,
            GROUP_UNBOUND_UMO,
        }
        item = next(
            group for group in data["groups"] if group["umo"] == GROUP_UNBOUND_UMO
        )
        assert item["bound_soul_id"] == ""

    async def test_scoped_group_items_keep_frozen_shape(self):
        data = await self.list_scoped("soul_a")
        expected_keys = {
            "umo",
            "group_id",
            "platform_id",
            "display_name",
            "notes",
            "bound_soul_id",
            "bound_soul_name",
            "window_count",
            "unread_count",
            "last_active_at",
        }
        for item in data["groups"]:
            assert set(item) == expected_keys


# ==============================================================================
# 3. users/update — update / normalize_identity (contract §4.2)
# ==============================================================================
class TestUsersUpdateSoulScope(TwoSoulTestBase):
    async def test_update_writes_relation_fields_only_for_current_soul(self):
        data = self.assert_ok(
            await self.update_users(
                {
                    "soul_id": "soul_a",
                    "global_uid": self.u1_a.global_uid,
                    "tier": 1,
                    "affinity": 55,
                    "attitude": "更新后的A态度",
                    "nickname": "新A昵称",
                }
            )
        )
        assert data["soul_id"] == "soul_a"
        assert data["user"]["tier"] == 1
        assert data["user"]["affinity"] == 55.0
        assert data["user"]["attitude"] == "更新后的A态度"
        assert data["user"]["nickname"] == "新A昵称"
        # Response users reflect the scoped view.
        assert self.uids(data) == {self.u1_a.global_uid, self.u2_a.global_uid}

        # soul_b relation untouched.
        data_b = await self.list_scoped("soul_b")
        user_b = next(
            user
            for user in data_b["users"]
            if user["global_uid"] == self.u1_a.global_uid
        )
        assert user_b["tier"] == 2
        assert user_b["affinity"] == 10.0
        assert user_b["attitude"] == "B态度"

        # Legacy `users` columns untouched by the scoped write.
        row = await self.fetch_user_row(self.u1_a.global_uid)
        assert int(row["tier"]) == 1
        assert row["attitude"] == ""

    async def test_update_display_name_is_identity_global(self):
        self.assert_ok(
            await self.update_users(
                {
                    "soul_id": "soul_a",
                    "global_uid": self.u1_a.global_uid,
                    "display_name": "改名一",
                }
            )
        )
        data_b = await self.list_scoped("soul_b")
        user_b = next(
            user
            for user in data_b["users"]
            if user["global_uid"] == self.u1_a.global_uid
        )
        assert user_b["display_name"] == "改名一"
        legacy = await self.list_legacy()
        legacy_user = next(
            user
            for user in legacy["users"]
            if user["global_uid"] == self.u1_a.global_uid
        )
        assert legacy_user["display_name"] == "改名一"

    async def test_update_platform_ids_is_identity_global(self):
        data = self.assert_ok(
            await self.update_users(
                {
                    "soul_id": "soul_a",
                    "global_uid": self.u1_a.global_uid,
                    "platform_ids": ["aiocqhttp:1001", "telegram:1001"],
                }
            )
        )
        assert data["user"]["platform_ids"] == [
            "aiocqhttp:1001",
            "telegram:1001",
        ]
        data_b = await self.list_scoped("soul_b")
        user_b = next(
            user
            for user in data_b["users"]
            if user["global_uid"] == self.u1_a.global_uid
        )
        assert user_b["platform_ids"] == ["aiocqhttp:1001", "telegram:1001"]

    async def test_update_attitude_truncated_per_soul(self):
        self.assert_ok(
            await self.update_users(
                {
                    "soul_id": "soul_b",
                    "global_uid": self.u1_a.global_uid,
                    "attitude": "毒" * 400,
                }
            )
        )
        data_b = await self.list_scoped("soul_b")
        user_b = next(
            user
            for user in data_b["users"]
            if user["global_uid"] == self.u1_a.global_uid
        )
        assert user_b["attitude"] == "毒" * 300
        data_a = await self.list_scoped("soul_a")
        user_a = next(
            user
            for user in data_a["users"]
            if user["global_uid"] == self.u1_a.global_uid
        )
        assert user_a["attitude"] == "A态度"

    async def test_update_without_soul_id_writes_legacy_columns_only(self):
        data = self.assert_ok(
            await self.update_users(
                {
                    "global_uid": self.u1_a.global_uid,
                    "tier": 2,
                    "affinity": 88,
                    "attitude": "legacy态度",
                }
            )
        )
        assert "soul_id" not in data  # legacy v2.8 shape preserved
        assert data["user"]["tier"] == 2
        assert data["user"]["attitude"] == "legacy态度"
        # Per-Soul relations untouched.
        data_a = await self.list_scoped("soul_a")
        user_a = next(
            user
            for user in data_a["users"]
            if user["global_uid"] == self.u1_a.global_uid
        )
        assert user_a["tier"] == 3
        assert user_a["attitude"] == "A态度"
        row = await self.fetch_user_row(self.u1_a.global_uid)
        assert int(row["tier"]) == 2
        assert row["attitude"] == "legacy态度"

    async def test_update_empty_platform_ids_is_error(self):
        response = await self.update_users(
            {
                "soul_id": "soul_a",
                "global_uid": self.u1_a.global_uid,
                "platform_ids": [],
            }
        )
        self.assert_error(response, "至少保留一个平台账号")

    async def test_normalize_identity_is_identity_global_and_echoes_soul(self):
        # Legacy bare-id profile (pre-v2.8.4) written straight into the users table.
        await self.db.execute(
            """
            INSERT INTO users (
                global_uid, display_name, nickname, tier, interaction_count,
                affinity, platform_ids_json, attitude, updated_at
            ) VALUES (?, ?, '', 1, 0, 0.0, ?, '', ?)
            """,
            ("2975066889", "裸ID用户", '["2975066889"]', 1),
        )
        data = self.assert_ok(
            await self.update_users(
                {
                    "action": "normalize_identity",
                    "soul_id": "soul_a",
                    "global_uid": "2975066889",
                    "platform_id": "aiocqhttp",
                }
            )
        )
        assert data["soul_id"] == "soul_a"
        assert data["user"]["platform_ids"] == ["aiocqhttp:2975066889"]
        # Identity repair is global; scoped users view is echoed as requested.
        assert self.uids(data) == {self.u1_a.global_uid, self.u2_a.global_uid}
        # No relation was invented for the bare-id user in soul_a.
        scoped_uids = {row["global_uid"] for row in await self.relation_rows("soul_a")}
        assert "aiocqhttp:2975066889" not in scoped_uids
        assert "2975066889" not in scoped_uids

        # Without soul_id the legacy global view is returned with an empty echo.
        legacy = self.assert_ok(
            await self.update_users(
                {
                    "action": "normalize_identity",
                    "global_uid": "2975066889",
                }
            )
        )
        assert legacy["soul_id"] == ""


# ==============================================================================
# 4. users/update — merge validation + scoped payload (contract §4.2)
# ==============================================================================
class TestUsersMergeSoulScope(TwoSoulTestBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        # Secondary also has a soul_b relation so cross-Soul merge semantics are
        # exercised through the API response.
        self.u2_b = await self.seed_relation(
            "soul_b", "2002", "阿二", tier=3, attitude="B二态度"
        )

    async def _seed_memory_for(self, uid):
        return await self.plugin.memory_manager.add_memory(
            soul_id="soul_a",
            content="阿二爱吃辣",
            keywords="辣",
            importance=5,
            user_id=uid,
        )

    async def test_merge_within_current_soul(self):
        await self._seed_memory_for(self.u2_a.global_uid)
        data = self.assert_ok(
            await self.update_users(
                {
                    "action": "merge",
                    "soul_id": "soul_a",
                    "primary_uid": self.u1_a.global_uid,
                    "secondary_uid": self.u2_a.global_uid,
                }
            )
        )
        assert data["soul_id"] == "soul_a"
        assert set(data) == {"soul_id", "users", "counts", "merged_user"}
        merged = data["merged_user"]
        assert merged["global_uid"] == self.u1_a.global_uid
        assert merged["tier"] == 3  # max(3, 1)
        assert merged["affinity"] >= 30.0
        # Scoped users: secondary is gone, counts shrink accordingly.
        assert self.uids(data) == {self.u1_a.global_uid}
        assert data["counts"] == {
            "l3_count": 1,
            "l2_count": 0,
            "l1_count": 0,
            "total": 1,
        }
        # Identity merged globally: platforms union, secondary identity row deleted.
        primary_row = await self.fetch_user_row(self.u1_a.global_uid)
        assert "aiocqhttp:2002" in json.loads(primary_row["platform_ids_json"])
        assert await self.fetch_user_row(self.u2_a.global_uid) is None
        # Cross-Soul relation merge: soul_b keeps u1 (tier 3 from secondary) only.
        data_b = await self.list_scoped("soul_b")
        assert self.uids(data_b) == {self.u1_a.global_uid, self.u3_b.global_uid}
        user_b = next(
            user
            for user in data_b["users"]
            if user["global_uid"] == self.u1_a.global_uid
        )
        assert user_b["tier"] == 3
        # memories.user_id re-linked from secondary to primary.
        memories = await self.db.fetchall(
            "SELECT user_id FROM memories WHERE soul_id = ?", ("soul_a",)
        )
        assert memories and all(
            item["user_id"] == self.u1_a.global_uid for item in memories
        )

    async def test_merge_rejects_secondary_outside_current_soul(self):
        before_a = await self.list_scoped("soul_a")
        response = await self.update_users(
            {
                "action": "merge",
                "soul_id": "soul_a",
                "primary_uid": self.u1_a.global_uid,
                "secondary_uid": self.u3_b.global_uid,
            }
        )
        self.assert_error(response, MERGE_SCOPE_ERROR)
        after_a = await self.list_scoped("soul_a")
        assert self.uids(after_a) == self.uids(before_a)
        assert await self.fetch_user_row(self.u3_b.global_uid) is not None

    async def test_merge_rejects_primary_outside_current_soul(self):
        response = await self.update_users(
            {
                "action": "merge",
                "soul_id": "soul_a",
                "primary_uid": self.u3_b.global_uid,
                "secondary_uid": self.u1_a.global_uid,
            }
        )
        self.assert_error(response, MERGE_SCOPE_ERROR)

    async def test_merge_rejects_unknown_uids(self):
        response = await self.update_users(
            {
                "action": "merge",
                "soul_id": "soul_a",
                "primary_uid": "aiocqhttp:nope",
                "secondary_uid": self.u2_a.global_uid,
            }
        )
        self.assert_error(response, MERGE_SCOPE_ERROR)
        response = await self.update_users(
            {
                "action": "merge",
                "soul_id": "soul_a",
                "primary_uid": self.u1_a.global_uid,
                "secondary_uid": "",
            }
        )
        self.assert_error(response, MERGE_SCOPE_ERROR)

    async def test_merge_rejects_same_user(self):
        response = await self.update_users(
            {
                "action": "merge",
                "soul_id": "soul_a",
                "primary_uid": self.u1_a.global_uid,
                "secondary_uid": self.u1_a.global_uid,
            }
        )
        self.assert_error(response, "两个不同的用户")

    async def test_merge_without_soul_id_is_legacy_global(self):
        data = self.assert_ok(
            await self.update_users(
                {
                    "action": "merge",
                    "primary_uid": self.u1_a.global_uid,
                    "secondary_uid": self.u2_a.global_uid,
                }
            )
        )
        assert set(data) == {"users", "merged_user"}  # legacy v2.8 shape
        uids = {user["global_uid"] for user in data["users"]}
        assert self.u1_a.global_uid in uids
        assert self.u2_a.global_uid not in uids
        assert await self.fetch_user_row(self.u2_a.global_uid) is None


# ==============================================================================
# 5. users/update — delete scope (contract §4.2)
# ==============================================================================
class TestUsersDeleteSoulScope(TwoSoulTestBase):
    async def test_delete_removes_only_current_soul_relation(self):
        response = await self.update_users(
            {
                "action": "delete",
                "soul_id": "soul_a",
                "global_uid": self.u1_a.global_uid,
            }
        )
        data = self.assert_ok(response)
        assert data["soul_id"] == "soul_a"
        assert self.uids(data) == {self.u2_a.global_uid}
        assert "其他角色" in response["message"]
        assert "关系档案" in response["message"]
        # Other role relation preserved -> user still listed for soul_b.
        data_b = await self.list_scoped("soul_b")
        assert self.u1_a.global_uid in self.uids(data_b)
        rows_a = {row["global_uid"] for row in await self.relation_rows("soul_a")}
        rows_b = {row["global_uid"] for row in await self.relation_rows("soul_b")}
        assert self.u1_a.global_uid not in rows_a
        assert self.u1_a.global_uid in rows_b
        # Identity row survives because soul_b still keeps a relation.
        assert await self.fetch_user_row(self.u1_a.global_uid) is not None

    async def test_delete_last_relation_removes_identity(self):
        data = self.assert_ok(
            await self.update_users(
                {
                    "action": "delete",
                    "soul_id": "soul_a",
                    "global_uid": self.u2_a.global_uid,
                }
            )
        )
        assert self.u2_a.global_uid not in self.uids(data)
        assert await self.fetch_user_row(self.u2_a.global_uid) is None
        legacy = await self.list_legacy()
        assert self.u2_a.global_uid not in self.uids(legacy)

    async def test_delete_scoped_keeps_memories(self):
        await self.plugin.memory_manager.add_memory(
            soul_id="soul_a",
            content="喜欢猫",
            keywords="猫",
            importance=6,
            user_id=self.u2_a.global_uid,
        )
        self.assert_ok(
            await self.update_users(
                {
                    "action": "delete",
                    "soul_id": "soul_a",
                    "global_uid": self.u2_a.global_uid,
                }
            )
        )
        memories = await self.db.fetchall(
            "SELECT user_id FROM memories WHERE soul_id = ?", ("soul_a",)
        )
        assert len(memories) == 1
        assert memories[0]["user_id"] == self.u2_a.global_uid

    async def test_delete_without_soul_id_is_legacy_global(self):
        data = self.assert_ok(
            await self.update_users(
                {"action": "delete", "global_uid": self.u1_a.global_uid}
            )
        )
        assert set(data) == {"users"}  # legacy v2.8 shape
        assert await self.fetch_user_row(self.u1_a.global_uid) is None
        rows_a = {row["global_uid"] for row in await self.relation_rows("soul_a")}
        rows_b = {row["global_uid"] for row in await self.relation_rows("soul_b")}
        assert self.u1_a.global_uid not in rows_a | rows_b
        assert self.u1_a.global_uid not in self.uids(data)

    async def test_delete_requires_uid(self):
        response = await self.update_users({"action": "delete", "soul_id": "soul_a"})
        self.assert_error(response, "不能为空")


# ==============================================================================
# 6. users/update — group actions stay scoped (contract §4.4)
# ==============================================================================
class TestUsersUpdateGroupActionScope(TwoSoulTestBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        relation = self.plugin.user_relation
        self.group_a = await relation.touch_group(
            GROUP_A_UMO,
            group_id="111",
            platform_id="aiocqhttp",
            bound_soul_id="soul_a",
        )
        self.group_b = await relation.touch_group(
            GROUP_B_UMO,
            group_id="222",
            platform_id="aiocqhttp",
            bound_soul_id="soul_b",
        )

    async def test_update_group_returns_scoped_payload(self):
        data = self.assert_ok(
            await self.update_users(
                {
                    "action": "update_group",
                    "soul_id": "soul_a",
                    "umo": GROUP_A_UMO,
                    "display_name": "A的群",
                    "notes": "A备注",
                }
            )
        )
        assert data["soul_id"] == "soul_a"
        assert {group["umo"] for group in data["groups"]} == {GROUP_A_UMO}
        assert data["groups"][0]["display_name"] == "A的群"
        assert data["groups"][0]["notes"] == "A备注"
        assert data["counts"]["total"] == 2
        group = await self.plugin.user_relation.get_group(GROUP_A_UMO)
        assert group.display_name == "A的群"

    async def test_clear_group_memory_returns_scoped_payload(self):
        await self.record_window(GROUP_A_UMO, "A消息", unread=True)
        await self.record_window(GROUP_B_UMO, "B消息", unread=True)
        data = self.assert_ok(
            await self.update_users(
                {
                    "action": "clear_group_memory",
                    "soul_id": "soul_a",
                    "umo": GROUP_A_UMO,
                }
            )
        )
        assert data["soul_id"] == "soul_a"
        assert {group["umo"] for group in data["groups"]} == {GROUP_A_UMO}
        buffer = self.plugin.session_buffer
        assert GROUP_A_UMO not in buffer._windows
        assert GROUP_B_UMO in buffer._windows
        assert GROUP_A_UMO not in buffer._unread
        assert GROUP_B_UMO in buffer._unread

    async def test_delete_group_returns_scoped_payload(self):
        data = self.assert_ok(
            await self.update_users(
                {
                    "action": "delete_group",
                    "soul_id": "soul_a",
                    "umo": GROUP_A_UMO,
                }
            )
        )
        assert data["soul_id"] == "soul_a"
        assert data["groups"] == []
        assert await self.plugin.user_relation.get_group(GROUP_A_UMO) is None
        # Other Soul's group untouched.
        assert await self.plugin.user_relation.get_group(GROUP_B_UMO) is not None

    async def test_group_action_without_soul_id_keeps_v28_shape(self):
        data = self.assert_ok(
            await self.update_users(
                {
                    "action": "update_group",
                    "umo": GROUP_B_UMO,
                    "display_name": "全局群名",
                }
            )
        )
        # v2.8 frozen payload: exactly groups + counts, no soul_id key.
        assert set(data) == {"groups", "counts"}
        assert {group["umo"] for group in data["groups"]} == {
            GROUP_A_UMO,
            GROUP_B_UMO,
        }

    async def test_group_actions_validate_umo_regardless_of_scope(self):
        response = await self.update_users(
            {
                "action": "update_group",
                "soul_id": "soul_a",
                "umo": "aiocqhttp:GroupMessage:nope",
            }
        )
        self.assert_error(response, "未找到群聊档案")
        response = await self.update_users(
            {"action": "clear_group_memory", "soul_id": "soul_a"}
        )
        self.assert_error(response, "UMO")


# ==============================================================================
# 7. dashboard/overview — scoped relation counters (contract §4.3)
# ==============================================================================
class TestDashboardOverviewSoulScope(TwoSoulTestBase):
    async def test_relation_counts_scoped_per_soul(self):
        data_a = self.assert_ok(
            await self.call(
                "GET",
                self.api.api_dashboard_overview,
                query={"soul_id": "soul_a"},
            )
        )
        assert data_a["soul"]["soul_id"] == "soul_a"
        assert data_a["counts"]["relations"] == {
            "l1": 1,
            "l2": 0,
            "l3": 1,
            "total": 2,
        }
        data_b = self.assert_ok(
            await self.call(
                "GET",
                self.api.api_dashboard_overview,
                query={"soul_id": "soul_b"},
            )
        )
        assert data_b["counts"]["relations"] == {
            "l1": 1,
            "l2": 1,
            "l3": 0,
            "total": 2,
        }
        # Response shape unchanged (memories / videos / models still present).
        assert set(data_b["counts"]) == {"relations", "memories", "videos"}
        assert set(data_b["counts"]["relations"]) == {"l1", "l2", "l3", "total"}

    async def test_relation_counts_follow_relation_updates(self):
        self.assert_ok(
            await self.update_users(
                {
                    "soul_id": "soul_a",
                    "global_uid": self.u2_a.global_uid,
                    "tier": 2,
                }
            )
        )
        data = self.assert_ok(
            await self.call(
                "GET",
                self.api.api_dashboard_overview,
                query={"soul_id": "soul_a"},
            )
        )
        assert data["counts"]["relations"] == {
            "l1": 0,
            "l2": 1,
            "l3": 1,
            "total": 2,
        }


# ==============================================================================
# 8. Legacy (no soul_id) compatibility
# ==============================================================================
class TestLegacyWebApiCompatibility(WebApiV29TestBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        relation = self.plugin.user_relation
        self.legacy_u1 = await relation.get_or_create_user("9001", "旧一")
        self.legacy_u2 = await relation.get_or_create_user("9002", "旧二")

    async def test_list_and_update_keep_legacy_semantics(self):
        listed = await self.list_legacy()
        assert listed["soul_id"] == ""
        assert {user["global_uid"] for user in listed["users"]} == {
            self.legacy_u1.global_uid,
            self.legacy_u2.global_uid,
        }
        assert listed["counts"] == {
            "l3_count": 0,
            "l2_count": 0,
            "l1_count": 2,
            "total": 2,
        }
        updated = self.assert_ok(
            await self.update_users(
                {"global_uid": self.legacy_u1.global_uid, "tier": 2, "affinity": 42}
            )
        )
        assert "soul_id" not in updated  # legacy v2.8 shape preserved
        assert updated["user"]["tier"] == 2
        assert updated["user"]["affinity"] == 42.0
        row = await self.fetch_user_row(self.legacy_u1.global_uid)
        assert int(row["tier"]) == 2

    async def test_legacy_merge_and_delete_still_work(self):
        merged = self.assert_ok(
            await self.update_users(
                {
                    "action": "merge",
                    "primary_uid": self.legacy_u1.global_uid,
                    "secondary_uid": self.legacy_u2.global_uid,
                }
            )
        )
        assert "soul_id" not in merged
        uids = {user["global_uid"] for user in merged["users"]}
        assert uids == {self.legacy_u1.global_uid}
        deleted = self.assert_ok(
            await self.update_users(
                {"action": "delete", "global_uid": self.legacy_u1.global_uid}
            )
        )
        assert set(deleted) == {"users"}
        assert deleted["users"] == []
        assert await self.fetch_user_row(self.legacy_u1.global_uid) is None

    async def test_soul_scoped_and_legacy_views_are_independent(self):
        soul = await self.create_soul("soul_legacy", "旧角色")
        relation = self.plugin.user_relation
        scoped = await relation.get_or_create_user(
            "9001", "旧一", soul_id=soul.soul_id
        )
        await relation.update_user_details(
            scoped.global_uid, tier=3, attitude="Soul态度", soul_id=soul.soul_id
        )
        # Scoped view sees tier=3 / Soul态度; legacy view still sees tier=2 / "".
        data = await self.list_scoped(soul.soul_id)
        scoped_user = next(
            user
            for user in data["users"]
            if user["global_uid"] == scoped.global_uid
        )
        assert scoped_user["tier"] == 3
        assert scoped_user["attitude"] == "Soul态度"
        legacy = await self.list_legacy()
        legacy_user = next(
            user
            for user in legacy["users"]
            if user["global_uid"] == self.legacy_u1.global_uid
        )
        assert legacy_user["tier"] == 1
        assert legacy_user["attitude"] == ""


# ==============================================================================
# 9. Defensive degradation: legacy manager without the ``soul_id`` keyword
# ==============================================================================
class _LegacyUserRelationStub:
    """Minimal pre-v2.9 manager: no ``soul_id`` keyword on ``list_users``."""

    def __init__(self, users, groups=None):
        self._users = list(users)
        self._groups = list(groups or [])

    async def list_users(self, tier_filter=None):
        if tier_filter in (1, 2, 3):
            return [user for user in self._users if int(user.tier.value) == tier_filter]
        return list(self._users)

    async def list_groups(self):
        return list(self._groups)

    async def delete_user(self, global_uid):  # old positional signature
        self._users = [user for user in self._users if user.global_uid != global_uid]


class TestLegacyManagerDegradation(WebApiV29TestBase):
    async def test_users_list_tolerates_manager_without_soul_id(self):
        stub = _LegacyUserRelationStub([mods.user_relation.UserProfile("u-1", "阿一")])
        self.plugin.user_relation = stub
        data = self.assert_ok(
            await self.call(
                "GET", self.api.api_users_list, query={"soul_id": "soul_x"}
            )
        )
        assert data["soul_id"] == "soul_x"
        assert [user["global_uid"] for user in data["users"]] == ["u-1"]
        assert data["counts"]["total"] == 1

    async def test_users_delete_tolerates_manager_without_soul_id(self):
        stub = _LegacyUserRelationStub([mods.user_relation.UserProfile("u-1", "阿一")])
        self.plugin.user_relation = stub
        data = self.assert_ok(
            await self.update_users(
                {"action": "delete", "soul_id": "soul_x", "global_uid": "u-1"}
            )
        )
        assert data["users"] == []


if __name__ == "__main__":
    unittest.main()
