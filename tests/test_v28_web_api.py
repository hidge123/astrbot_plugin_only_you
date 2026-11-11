"""Offline tests for the **v2.8** Web API + group-profile additions (contract §6).

Covers:
- the four new ``sessions/*`` bot-instance binding routes (GET platforms, bind /
  unbind platform with strict mutual exclusion, per-session / per-platform memory
  clearing) and the ``plugin.clear_platform_memory_state`` call on unbind;
- ``users/list`` -> ``groups`` (frozen ``GroupItem`` shape) and the
  ``users/update`` group actions (``update_group`` / ``clear_group_memory`` /
  ``delete_group``) backed by ``UserRelationManager`` group profiles persisted in
  ``plugin_settings["group_profiles"]`` (contract §6.3);
- ``memories/list`` -> ``tier1_limit`` (per-Soul T1 cap from the plugin config);
- ``surfing/list_and_trigger`` GET switch fields + POST ``toggle_surfing`` (per-Soul
  ``settings.surfing_enabled`` persisted into the Soul JSON file);
- ``models/config`` GET/POST ``soul_id`` scoping + per-Soul isolation,
  ``models/test`` optional ``soul_id``, ``init_summary.platform_bindings`` and the
  ``souls/save`` ``model_config`` / ``bound_platforms`` / ``settings`` passthrough.

The fake plugin assembles the **real** business components against a temporary
SQLite database (``tests/_helpers.build_fake_plugin``); ``ModelHub`` is attached to
the ``SoulManager`` in ``asyncSetUp`` exactly like ``main.py`` wires it (contract
§2.1) so per-Soul model configs are exercised end-to-end.  Everything is offline.
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
    make_plugin_config,
    make_soul,
)

mods = load_plugin_modules()
GROUP_SETTING_KEY = mods.user_relation.GROUP_SETTING_KEY
GROUP_UMO = "aiocqhttp:GroupMessage:123456"
PRIVATE_UMO = "aiocqhttp:FriendMessage:654321"
OTHER_GROUP_UMO = "telegram_bot_1:GroupMessage:999"

GROUP_ITEM_KEYS = {
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

PLATFORM_ITEM_KEYS = {
    "platform_id",
    "platform_type",
    "name",
    "enabled",
    "running",
    "source",
    "bound_soul_id",
    "bound_soul_name",
    "session_count",
}

SESSION_ITEM_KEYS = {
    "umo",
    "kind",
    "platform_id",
    "bound_soul_id",
    "window_messages",
    "is_l3_private",
    "last_active_at",
}

CLEARED_KEYS = {"windows", "evicted", "unread", "messages"}


class WebApiV28TestBase(DatabaseAsyncTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        FakeChatProvider.reset()
        FakeTTSProvider.reset()
        FakeTTSProvider.audio_dir = self.tmpdir
        await self._build_plugin()

    async def _build_plugin(self, config=None):
        self.plugin, self.api = await build_fake_plugin(
            self.db, self.tmpdir, config=config
        )
        # main.py wires ModelHub -> SoulManager right after constructing both
        # (contract §2.1); mirror that here so models/* routes are soul-scoped.
        attach = getattr(self.plugin.model_hub, "attach_soul_manager", None)
        if callable(attach):
            attach(self.plugin.soul_manager)
        return self.plugin, self.api

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    async def call(self, method, handler, query=None, body=None):
        self.api.set_mock_request(method, query=query or {}, body=body or {})
        return await handler()

    def assert_ok(self, response):
        assert response["status"] == "ok", response
        assert isinstance(response["message"], str)
        assert isinstance(response["data"], dict)
        return response["data"]

    def assert_error(self, response):
        assert response["status"] == "error", response
        assert isinstance(response["message"], str) and response["message"]
        assert isinstance(response["data"], dict)
        return response

    async def first_soul(self):
        souls = await self.plugin.soul_manager.list_souls()
        return souls[0]

    async def create_soul(self, soul_id, name="第二个角色"):
        soul = make_soul(
            soul_id=soul_id,
            card=card_with(
                identity={"name": name, "tagline": "测试角色"},
                speech={"speaking_style": "简短"},
            ),
        )
        await self.plugin.soul_manager.save_soul(soul)
        return soul

    async def record_window(self, umo, content="测试消息", unread=False, is_private=False):
        """Record one chat message into the real ``SessionBuffer`` sliding window."""
        buffer = self.plugin.session_buffer
        user = await self.plugin.user_relation.get_or_create_user("u-tester", "测试群友")
        message = buffer.record_user_message(umo=umo, user=user, content=content)
        if unread and message is not None:
            buffer.mark_unread(umo, message, is_private=is_private)
        return message

    async def read_soul_file(self, soul_id):
        """Read back the persisted ``souls/<id>.json`` file (contract §1.1)."""
        path_fn = getattr(self.plugin.soul_manager, "soul_file_path", None)
        assert callable(path_fn)
        path = path_fn(soul_id)
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)

    async def bind_platform(self, soul_id, platform_id):
        return await self.plugin.soul_manager.bind_platform(soul_id, platform_id)


# ==============================================================================
# Route registration (v2.8 §6.1: 27 -> 31)
# ==============================================================================
class TestRouteRegistrationV28(WebApiV28TestBase):
    async def test_four_new_session_routes_registered(self):
        self.plugin.context.registered_routes.clear()
        self.api.register_routes()
        routes = self.plugin.context.registered_routes
        assert len(routes) == 31
        mapping = {
            item["route"].replace(mods.web_api.API_PREFIX, ""): item
            for item in routes
        }
        assert mapping["/sessions/platforms"]["methods"] == ["GET"]
        for route in (
            "/sessions/bind_platform",
            "/sessions/unbind_platform",
            "/sessions/clear_memory",
        ):
            assert mapping[route]["methods"] == ["POST"]
            assert mapping[route]["description"]


# ==============================================================================
# sessions/platforms + bind / unbind / clear-memory (contract §6.2)
# ==============================================================================
class TestSessionsPlatforms(WebApiV28TestBase):
    async def test_get_platforms_shape(self):
        soul = await self.first_soul()
        data = self.assert_ok(await self.call("GET", self.api.api_sessions_platforms))
        assert set(data) >= {
            "current_soul_id",
            "default_soul_id",
            "souls",
            "platforms",
            "sessions",
        }
        assert data["current_soul_id"]
        soul_ids = {item["soul_id"] for item in data["souls"]}
        assert data["current_soul_id"] in soul_ids
        soul_item = next(
            item for item in data["souls"] if item["soul_id"] == data["current_soul_id"]
        )
        assert set(soul_item) >= {"soul_id", "name", "bound_platforms"}
        assert isinstance(data["platforms"], list)
        assert isinstance(data["sessions"], list)
        assert soul.soul_id in soul_ids

    async def test_get_platforms_lists_bound_platform_and_sessions(self):
        soul = await self.first_soul()
        await self.bind_platform(soul.soul_id, "aiocqhttp")
        await self.record_window(GROUP_UMO, "群聊消息", unread=True)
        await self.record_window(PRIVATE_UMO, "私聊消息")
        await self.record_window(OTHER_GROUP_UMO, "其他平台消息")

        data = self.assert_ok(
            await self.call(
                "GET",
                self.api.api_sessions_platforms,
                query={"soul_id": soul.soul_id},
            )
        )
        assert data["current_soul_id"] == soul.soul_id
        soul_item = next(item for item in data["souls"] if item["soul_id"] == soul.soul_id)
        assert soul_item["bound_platforms"] == ["aiocqhttp"]

        by_platform = {item["platform_id"]: item for item in data["platforms"]}
        assert "aiocqhttp" in by_platform
        platform_item = by_platform["aiocqhttp"]
        assert set(platform_item) == PLATFORM_ITEM_KEYS
        assert platform_item["bound_soul_id"] == soul.soul_id
        assert platform_item["bound_soul_name"] == soul.name
        assert platform_item["session_count"] == 2
        assert platform_item["source"] in ("runtime", "config", "observed")
        assert isinstance(platform_item["enabled"], bool)
        assert isinstance(platform_item["running"], bool)

        # Sessions only cover the platform actually bound to the current soul.
        sessions = {item["umo"]: item for item in data["sessions"]}
        assert set(sessions) == {GROUP_UMO, PRIVATE_UMO}
        for item in sessions.values():
            assert set(item) == SESSION_ITEM_KEYS
            assert item["platform_id"] == "aiocqhttp"
            assert item["bound_soul_id"] == soul.soul_id
            assert item["window_messages"] == 1
            assert item["is_l3_private"] is False
            assert isinstance(item["last_active_at"], int)
        assert sessions[GROUP_UMO]["kind"] == "group"
        assert sessions[PRIVATE_UMO]["kind"] == "private"

    async def test_bind_platform_success_payload(self):
        soul = await self.first_soul()
        response = await self.call(
            "POST",
            self.api.api_sessions_bind_platform,
            body={"soul_id": soul.soul_id, "platform_id": "aiocqhttp"},
        )
        data = self.assert_ok(response)
        assert data["soul_id"] == soul.soul_id
        assert data["platform_id"] == "aiocqhttp"
        assert isinstance(data["souls"], list) and isinstance(data["platforms"], list)
        bindings = await self.plugin.soul_manager.get_platform_bindings()
        assert bindings == {"aiocqhttp": soul.soul_id}
        soul_item = next(item for item in data["souls"] if item["soul_id"] == soul.soul_id)
        assert soul_item["bound_platforms"] == ["aiocqhttp"]
        platform_item = next(
            item for item in data["platforms"] if item["platform_id"] == "aiocqhttp"
        )
        assert platform_item["bound_soul_id"] == soul.soul_id

    async def test_bind_platform_mutual_exclusion_error_message(self):
        first = await self.first_soul()
        second = await self.create_soul("soul_b", "第二个角色")
        self.assert_ok(
            await self.call(
                "POST",
                self.api.api_sessions_bind_platform,
                body={"soul_id": first.soul_id, "platform_id": "aiocqhttp"},
            )
        )
        response = self.assert_error(
            await self.call(
                "POST",
                self.api.api_sessions_bind_platform,
                body={"soul_id": second.soul_id, "platform_id": "aiocqhttp"},
            )
        )
        expected = (
            f"该机器人接口已被角色『{first.name}』绑定，"
            f"请先在角色 {first.name} 下解绑"
        )
        assert response["message"] == expected, response["message"]
        bindings = await self.plugin.soul_manager.get_platform_bindings()
        assert bindings == {"aiocqhttp": first.soul_id}

    async def test_bind_platform_validation_errors(self):
        soul = await self.first_soul()
        response = self.assert_error(
            await self.call("POST", self.api.api_sessions_bind_platform, body={})
        )
        assert "soul_id" in response["message"]
        response = self.assert_error(
            await self.call(
                "POST",
                self.api.api_sessions_bind_platform,
                body={"soul_id": soul.soul_id},
            )
        )
        assert "platform_id" in response["message"]
        response = self.assert_error(
            await self.call(
                "POST",
                self.api.api_sessions_bind_platform,
                body={"soul_id": "ghost", "platform_id": "aiocqhttp"},
            )
        )
        assert "不存在" in response["message"]
        response = self.assert_error(
            await self.call(
                "POST",
                self.api.api_sessions_bind_platform,
                body={"soul_id": soul.soul_id, "platform_id": "bad:id"},
            )
        )
        assert "冒号" in response["message"] or "platform_id" in response["message"]

    async def test_unbind_platform_clears_platform_memory(self):
        soul = await self.first_soul()
        await self.bind_platform(soul.soul_id, "aiocqhttp")
        await self.record_window(GROUP_UMO, "群聊消息", unread=True)
        await self.record_window(PRIVATE_UMO, "私聊消息")
        await self.record_window(OTHER_GROUP_UMO, "其他平台消息")
        self.plugin._known_group_umos.add(GROUP_UMO)
        self.plugin._known_group_umos.add(OTHER_GROUP_UMO)
        buffer = self.plugin.session_buffer

        response = await self.call(
            "POST",
            self.api.api_sessions_unbind_platform,
            body={"platform_id": "aiocqhttp"},
        )
        data = self.assert_ok(response)
        assert data["platform_id"] == "aiocqhttp"
        assert data["previous_soul_id"] == soul.soul_id
        assert set(data["cleared"]) == CLEARED_KEYS
        assert data["cleared"]["windows"] == 2
        assert data["cleared"]["messages"] >= 2
        assert isinstance(data["souls"], list) and isinstance(data["platforms"], list)

        # platform-scoped in-memory state gone, other platforms untouched
        assert not any(umo.startswith("aiocqhttp:") for umo in buffer._windows)
        assert not any(umo.startswith("aiocqhttp:") for umo in buffer._unread)
        assert OTHER_GROUP_UMO in buffer._windows
        assert GROUP_UMO not in self.plugin._known_group_umos
        # binding removed from the Soul JSON / manager
        assert await self.plugin.soul_manager.get_platform_bindings() == {}
        refreshed = await self.plugin.soul_manager.get_soul_by_id(soul.soul_id)
        assert refreshed.bound_platforms == []

    async def test_unbind_unknown_platform_is_safe(self):
        data = self.assert_ok(
            await self.call(
                "POST",
                self.api.api_sessions_unbind_platform,
                body={"platform_id": "never_bound"},
            )
        )
        assert data["previous_soul_id"] == ""
        assert set(data["cleared"]) == CLEARED_KEYS
        response = self.assert_error(
            await self.call("POST", self.api.api_sessions_unbind_platform, body={})
        )
        assert "platform_id" in response["message"]

    async def test_clear_memory_single_session(self):
        await self.record_window(GROUP_UMO, "群聊消息", unread=True)
        await self.record_window(PRIVATE_UMO, "私聊消息")
        data = self.assert_ok(
            await self.call(
                "POST", self.api.api_sessions_clear_memory, body={"umo": GROUP_UMO}
            )
        )
        assert set(data["cleared"]) == CLEARED_KEYS
        assert data["cleared"]["windows"] == 1
        assert data["cleared"]["unread"] == 1
        buffer = self.plugin.session_buffer
        assert GROUP_UMO not in buffer._windows
        assert GROUP_UMO not in buffer._unread
        assert PRIVATE_UMO in buffer._windows
        session_umos = {item["umo"] for item in data["sessions"]}
        assert GROUP_UMO not in session_umos

    async def test_clear_memory_whole_platform(self):
        await self.record_window(GROUP_UMO, "群聊消息")
        await self.record_window(PRIVATE_UMO, "私聊消息")
        await self.record_window(OTHER_GROUP_UMO, "其他平台")
        data = self.assert_ok(
            await self.call(
                "POST",
                self.api.api_sessions_clear_memory,
                body={"platform_id": "aiocqhttp"},
            )
        )
        assert data["cleared"]["windows"] == 2
        buffer = self.plugin.session_buffer
        assert not any(umo.startswith("aiocqhttp:") for umo in buffer._windows)
        assert OTHER_GROUP_UMO in buffer._windows

    async def test_clear_memory_validation(self):
        response = self.assert_error(
            await self.call("POST", self.api.api_sessions_clear_memory, body={})
        )
        assert "umo" in response["message"] or "platform_id" in response["message"]


# ==============================================================================
# Group profiles: users/list groups[] + users/update group actions (§6.2/§6.3)
# ==============================================================================
class TestUserGroupProfiles(WebApiV28TestBase):
    async def test_group_profile_dataclass_roundtrip(self):
        profile = mods.user_relation.GroupProfile(
            umo=GROUP_UMO,
            group_id="123456",
            platform_id="aiocqhttp",
            display_name="测试群",
            notes="备注",
            bound_soul_id="soul_a",
            last_active_at=1762840000,
        )
        data = profile.to_dict()
        assert set(data) == {
            "umo",
            "group_id",
            "platform_id",
            "display_name",
            "notes",
            "bound_soul_id",
            "last_active_at",
        }
        assert mods.user_relation.GroupProfile.from_dict(data).to_dict() == data
        assert mods.user_relation.GroupProfile.from_dict("bad") is None
        assert mods.user_relation.GroupProfile.from_dict({"group_id": "x"}) is None

    async def test_touch_group_creates_and_persists(self):
        manager = self.plugin.user_relation
        group = await manager.touch_group(
            GROUP_UMO,
            group_id="123456",
            platform_id="aiocqhttp",
            bound_soul_id="soul_a",
        )
        assert group.display_name == "群聊 123456"
        assert group.group_id == "123456"
        assert group.platform_id == "aiocqhttp"
        assert group.bound_soul_id == "soul_a"
        assert group.last_active_at > 0
        stored = await self.db.get_setting(GROUP_SETTING_KEY, [])
        assert len(stored) == 1 and stored[0]["umo"] == GROUP_UMO

    async def test_touch_group_derives_defaults_from_umo(self):
        manager = self.plugin.user_relation
        group = await manager.touch_group(GROUP_UMO)
        assert group.group_id == "123456"
        assert group.platform_id == "aiocqhttp"
        assert group.display_name == "群聊 123456"
        # never raises on a missing UMO either
        empty = await manager.touch_group("")
        assert empty.umo == ""
        assert empty.display_name == "群聊"

    async def test_touch_group_keeps_custom_name_and_throttles_persist(self):
        manager = self.plugin.user_relation
        await manager.touch_group(GROUP_UMO, group_id="123456")
        await manager.update_group(GROUP_UMO, display_name="我的自定义群名")
        await manager.touch_group(GROUP_UMO, display_name="AstrBot 群名")
        group = await manager.get_group(GROUP_UMO)
        assert group.display_name == "我的自定义群名"
        # existing groups refresh last_active_at at most once per 60s
        stored_before = (await self.db.get_setting(GROUP_SETTING_KEY, []))[0]
        updated = await manager.touch_group(GROUP_UMO)
        assert updated.last_active_at >= stored_before["last_active_at"]
        stored_after = (await self.db.get_setting(GROUP_SETTING_KEY, []))[0]
        assert stored_after["last_active_at"] == stored_before["last_active_at"]

    async def test_malformed_group_storage_is_tolerated(self):
        await self.db.set_setting(GROUP_SETTING_KEY, {"bad": 1, "worse": "x"})
        manager = self.plugin.user_relation
        assert await manager.list_groups() == []
        await self.db.set_setting(GROUP_SETTING_KEY, [{"missing_umo": 1}, "x", None])
        manager._groups_cache = None
        assert await manager.list_groups() == []
        group = await manager.touch_group(GROUP_UMO, group_id="123456")
        assert group.display_name == "群聊 123456"
        stored = await self.db.get_setting(GROUP_SETTING_KEY, [])
        assert isinstance(stored, list) and stored[0]["umo"] == GROUP_UMO

    async def test_users_list_includes_group_items(self):
        soul = await self.first_soul()
        await self.record_window(GROUP_UMO, "消息一", unread=True)
        await self.record_window(GROUP_UMO, "消息二")
        await self.plugin.user_relation.touch_group(
            GROUP_UMO,
            group_id="123456",
            platform_id="aiocqhttp",
            bound_soul_id=soul.soul_id,
        )
        data = self.assert_ok(await self.call("GET", self.api.api_users_list))
        assert "groups" in data
        groups = data["groups"]
        assert len(groups) == 1
        item = groups[0]
        assert set(item) == GROUP_ITEM_KEYS
        assert item["umo"] == GROUP_UMO
        assert item["group_id"] == "123456"
        assert item["platform_id"] == "aiocqhttp"
        assert item["display_name"] == "群聊 123456"
        assert item["bound_soul_id"] == soul.soul_id
        assert item["bound_soul_name"] == soul.name
        assert item["window_count"] == 2
        assert item["unread_count"] == 1
        assert item["last_active_at"] > 0
        # legacy shapes unchanged
        assert isinstance(data["users"], list)
        assert set(data["counts"]) == {"l3_count", "l2_count", "l1_count", "total"}

    async def test_users_update_update_group_action(self):
        await self.plugin.user_relation.touch_group(GROUP_UMO, group_id="123456")
        data = self.assert_ok(
            await self.call(
                "POST",
                self.api.api_users_update,
                body={
                    "action": "update_group",
                    "umo": GROUP_UMO,
                    "display_name": "新群名",
                    "notes": "群备注",
                },
            )
        )
        assert set(data) == {"groups", "counts"}
        assert set(data["counts"]) == {"l3_count", "l2_count", "l1_count", "total"}
        item = data["groups"][0]
        assert item["display_name"] == "新群名"
        assert item["notes"] == "群备注"
        group = await self.plugin.user_relation.get_group(GROUP_UMO)
        assert group.display_name == "新群名" and group.notes == "群备注"
        stored = await self.db.get_setting(GROUP_SETTING_KEY, [])
        assert stored[0]["display_name"] == "新群名"

    async def test_users_update_update_group_missing_errors(self):
        response = self.assert_error(
            await self.call(
                "POST",
                self.api.api_users_update,
                body={"action": "update_group", "umo": "aiocqhttp:GroupMessage:nope"},
            )
        )
        assert "未找到群聊档案" in response["message"]
        response = self.assert_error(
            await self.call(
                "POST", self.api.api_users_update, body={"action": "update_group"}
            )
        )
        assert "UMO" in response["message"]

    async def test_users_update_clear_group_memory_action(self):
        await self.record_window(GROUP_UMO, "消息一", unread=True)
        await self.record_window(GROUP_UMO, "消息二")
        await self.plugin.user_relation.touch_group(GROUP_UMO, group_id="123456")
        data = self.assert_ok(
            await self.call(
                "POST",
                self.api.api_users_update,
                body={"action": "clear_group_memory", "umo": GROUP_UMO},
            )
        )
        assert set(data) == {"groups", "counts"}
        # profile is kept, the in-memory window/unread are gone
        item = data["groups"][0]
        assert item["umo"] == GROUP_UMO
        assert item["window_count"] == 0
        assert item["unread_count"] == 0
        buffer = self.plugin.session_buffer
        assert GROUP_UMO not in buffer._windows
        assert GROUP_UMO not in buffer._unread
        assert await self.plugin.user_relation.get_group(GROUP_UMO) is not None

    async def test_users_update_delete_group_action(self):
        await self.plugin.user_relation.touch_group(GROUP_UMO, group_id="123456")
        data = self.assert_ok(
            await self.call(
                "POST",
                self.api.api_users_update,
                body={"action": "delete_group", "umo": GROUP_UMO},
            )
        )
        assert data["groups"] == []
        assert await self.plugin.user_relation.get_group(GROUP_UMO) is None
        assert await self.db.get_setting(GROUP_SETTING_KEY, []) == []
        response = self.assert_error(
            await self.call(
                "POST",
                self.api.api_users_update,
                body={"action": "delete_group", "umo": GROUP_UMO},
            )
        )
        assert "未找到群聊档案" in response["message"]


# ==============================================================================
# Surfing master switch (§5.2 / §6.2)
# ==============================================================================
class TestSurfingToggle(WebApiV28TestBase):
    async def test_get_surfing_includes_switch_and_soul(self):
        soul = await self.first_soul()
        data = self.assert_ok(
            await self.call(
                "GET", self.api.api_surfing_list_and_trigger, query={"soul_id": soul.soul_id}
            )
        )
        assert data["soul_id"] == soul.soul_id
        assert data["surfing_enabled"] is True
        # legacy payload preserved
        assert data["videos"] == []
        assert isinstance(data["known_umos"], list)

    async def test_toggle_surfing_persists_per_soul(self):
        soul = await self.first_soul()
        response = await self.call(
            "POST",
            self.api.api_surfing_list_and_trigger,
            body={"action": "toggle_surfing", "soul_id": soul.soul_id, "enabled": False},
        )
        data = self.assert_ok(response)
        assert data["soul_id"] == soul.soul_id
        assert data["surfing_enabled"] is False
        assert data["soul"]["settings"]["surfing_enabled"] is False
        # persisted into the independent Soul JSON file (contract §1.1)
        stored = await self.read_soul_file(soul.soul_id)
        assert stored["settings"]["surfing_enabled"] is False
        # GET reflects the new switch value
        get_data = self.assert_ok(
            await self.call(
                "GET", self.api.api_surfing_list_and_trigger, query={"soul_id": soul.soul_id}
            )
        )
        assert get_data["surfing_enabled"] is False
        # other Souls keep their own (default true) switch
        second = await self.create_soul("soul_b", "第二个角色")
        second_data = self.assert_ok(
            await self.call(
                "GET",
                self.api.api_surfing_list_and_trigger,
                query={"soul_id": second.soul_id},
            )
        )
        assert second_data["surfing_enabled"] is True
        # and it can be turned back on
        again = self.assert_ok(
            await self.call(
                "POST",
                self.api.api_surfing_list_and_trigger,
                body={"action": "toggle_surfing", "soul_id": soul.soul_id, "enabled": True},
            )
        )
        assert again["surfing_enabled"] is True


# ==============================================================================
# Per-Soul model config: models/config, models/test, init_summary, souls/save
# ==============================================================================
class TestModelsPerSoul(WebApiV28TestBase):
    async def test_models_config_per_soul_isolation(self):
        first = await self.first_soul()
        second = await self.create_soul("soul_b", "第二个角色")
        config_a = {"main": {"source_id": "deepseek", "model": "deepseek-chat"}}
        config_b = {"main": {"source_id": "deepseek", "model": "deepseek-reasoner"}}

        response_a = await self.call(
            "POST",
            self.api.api_models_config,
            body={"soul_id": first.soul_id, "model_config": config_a},
        )
        data_a = self.assert_ok(response_a)
        assert data_a["soul_id"] == first.soul_id
        assert data_a["model_config"]["main"]["model"] == "deepseek-chat"
        assert "热重建" in response_a["message"]

        data_b = self.assert_ok(
            await self.call(
                "POST",
                self.api.api_models_config,
                body={"soul_id": second.soul_id, "model_config": config_b},
            )
        )
        assert data_b["model_config"]["main"]["model"] == "deepseek-reasoner"

        get_a = self.assert_ok(
            await self.call(
                "GET", self.api.api_models_config, query={"soul_id": first.soul_id}
            )
        )
        get_b = self.assert_ok(
            await self.call(
                "GET", self.api.api_models_config, query={"soul_id": second.soul_id}
            )
        )
        assert get_a["soul_id"] == first.soul_id
        assert get_a["model_config"]["main"]["model"] == "deepseek-chat"
        assert get_b["model_config"]["main"]["model"] == "deepseek-reasoner"
        assert set(get_a["status"]) == {"main", "decision", "vision", "tts"}

        # each Soul persisted its own config in its JSON file
        assert (await self.read_soul_file(first.soul_id))["model_config"]["main"][
            "model"
        ] == "deepseek-chat"
        assert (await self.read_soul_file(second.soul_id))["model_config"]["main"][
            "model"
        ] == "deepseek-reasoner"

    async def test_models_config_without_soul_uses_default_soul(self):
        first = await self.first_soul()
        self.assert_ok(
            await self.call(
                "POST",
                self.api.api_sessions_default_soul,
                body={"soul_id": first.soul_id},
            )
        )
        self.assert_ok(
            await self.call(
                "POST",
                self.api.api_models_config,
                body={
                    "model_config": {
                        "main": {"source_id": "deepseek", "model": "deepseek-chat"}
                    }
                },
            )
        )
        data = self.assert_ok(await self.call("GET", self.api.api_models_config))
        assert data["soul_id"] == first.soul_id
        assert data["model_config"]["main"]["model"] == "deepseek-chat"

    async def test_models_test_accepts_soul_id(self):
        soul = await self.first_soul()
        self.assert_ok(
            await self.call(
                "POST",
                self.api.api_models_config,
                body={
                    "soul_id": soul.soul_id,
                    "model_config": {
                        "main": {"source_id": "deepseek", "model": "deepseek-chat"}
                    },
                },
            )
        )
        data = self.assert_ok(
            await self.call(
                "POST",
                self.api.api_models_test,
                body={"role": "main", "soul_id": soul.soul_id},
            )
        )
        assert data["status"] == "ok"
        assert data["role"] == "main"
        assert data["reply"] == "pong"

    async def test_init_summary_platform_bindings_and_soul_fields(self):
        soul = await self.first_soul()
        await self.bind_platform(soul.soul_id, "aiocqhttp")
        data = self.assert_ok(await self.call("GET", self.api.api_init_summary))
        assert data["platform_bindings"] == {"aiocqhttp": soul.soul_id}
        soul_dict = next(
            item for item in data["souls"] if item["soul_id"] == soul.soul_id
        )
        assert set(soul_dict) >= {
            "model_config",
            "bound_platforms",
            "settings",
            "updated_at",
        }
        assert soul_dict["bound_platforms"] == ["aiocqhttp"]
        assert soul_dict["settings"]["surfing_enabled"] is True
        # legacy keys preserved
        assert isinstance(data["umo_bindings"], dict)
        assert isinstance(data["known_umos"], list)

    async def test_souls_detail_data_extension(self):
        soul = await self.first_soul()
        data = self.assert_ok(
            await self.call(
                "GET", self.api.api_souls_detail, query={"soul_id": soul.soul_id}
            )
        )
        assert "platform_bindings" in data
        assert "schedule" in data

    async def test_souls_save_passthrough(self):
        card = card_with(
            identity={"name": "透传角色", "tagline": "测试透传"},
            speech={"speaking_style": "简短"},
        )
        data = self.assert_ok(
            await self.call(
                "POST",
                self.api.api_souls_save,
                body={
                    "soul_id": "soul_p",
                    "card": card,
                    "model_config": {
                        "main": {"source_id": "deepseek", "model": "deepseek-chat"}
                    },
                    "bound_platforms": ["telegram_bot_1", "telegram_bot_1"],
                    "settings": {"surfing_enabled": False},
                },
            )
        )
        soul_dict = data["soul"]
        assert soul_dict["soul_id"] == "soul_p"
        assert soul_dict["model_config"]["main"]["model"] == "deepseek-chat"
        assert soul_dict["bound_platforms"] == ["telegram_bot_1"]
        assert soul_dict["settings"]["surfing_enabled"] is False

        profile = await self.plugin.soul_manager.get_soul_by_id("soul_p")
        assert profile is not None
        assert profile.model_config["main"]["model"] == "deepseek-chat"
        assert profile.bound_platforms == ["telegram_bot_1"]
        assert profile.settings["surfing_enabled"] is False
        stored = await self.read_soul_file("soul_p")
        assert stored["bound_platforms"] == ["telegram_bot_1"]

    async def test_souls_save_passthrough_merges_existing_settings(self):
        soul = await self.first_soul()
        card = card_with(identity={"name": soul.name, "tagline": "x"})
        self.assert_ok(
            await self.call(
                "POST",
                self.api.api_souls_save,
                body={"soul_id": soul.soul_id, "card": card},
            )
        )
        self.assert_ok(
            await self.call(
                "POST",
                self.api.api_souls_save,
                body={"soul_id": soul.soul_id, "settings": {"custom_key": "value"}},
            )
        )
        profile = await self.plugin.soul_manager.get_soul_by_id(soul.soul_id)
        assert profile.settings["surfing_enabled"] is True
        assert profile.settings["custom_key"] == "value"


# ==============================================================================
# memories/list tier1_limit (§5.6 / §6.2)
# ==============================================================================
class TestMemoriesTier1Limit(WebApiV28TestBase):
    async def test_default_tier1_limit_is_30(self):
        data = self.assert_ok(await self.call("GET", self.api.api_memories_list))
        assert data["tier1_limit"] == 30
        assert "stats" in data and "memories" in data

    async def test_tier1_limit_reads_plugin_config(self):
        config = make_plugin_config()
        config["memory_and_context_config"] = dict(
            config["memory_and_context_config"], max_tier1_memories=7
        )
        await self._build_plugin(config=config)
        data = self.assert_ok(await self.call("GET", self.api.api_memories_list))
        assert data["tier1_limit"] == 7


if __name__ == "__main__":
    unittest.main()
