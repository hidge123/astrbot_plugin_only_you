"""v2.11 offline tests: per-session "allow proactive messages" switches.

Frozen contract (v2.11):

1. ``UserProfile.proactive_enabled`` (per-Soul ``user_relations`` row; legacy
   ``users.proactive_enabled`` fallback) and ``GroupProfile.proactive_enabled``
   (``plugin_settings["group_profiles"]``) default to **True** (legacy rows keep
   the pre-v2.11 behaviour: proactive messages allowed).
2. Only bot-initiated messages are gated: private unread catch-up, GROUP_LURK
   bubbles, MEMORY_MUSE DMs and surf-video shares. Passive replies (user-initiated
   private messages, @ / wake replies, read-air replies) are never affected.
3. A disabled group is skipped by the GROUP_LURK candidate picker (it must not
   starve other eligible groups); a disabled ★L3 user is skipped by the surf-share
   target picker instead of blocking the whole share.
4. Disabled private users keep their unread backlog (not ACKed) so re-enabling the
   switch can still catch up later.
5. ``users/list`` users + groups expose ``proactive_enabled``; ``users/update``
   main action and ``update_group`` action persist it (both default True).
6. Merging accounts must never silently re-enable a proactive opt-out.

Stdlib ``unittest`` only, fully offline.
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _helpers import (  # noqa: E402
    DatabaseAsyncTestCase,
    StubModelHub,
    build_fake_plugin,
    load_plugin_modules,
    make_plugin_config,
    make_run,
    make_slot,
    make_soul,
    make_user,
)

mods = load_plugin_modules()
UserRelationManager = mods.user_relation.UserRelationManager
GroupProfile = mods.user_relation.GroupProfile
UserProfile = mods.user_relation.UserProfile
DatabaseManager = mods.db.DatabaseManager
BotState = mods.state_machine.BotState
ActionType = mods.state_machine.ActionType
UserTier = mods.state_machine.UserTier
GROUP_SETTING_KEY = mods.user_relation.GROUP_SETTING_KEY

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GROUP_UMO = "aiocqhttp:GroupMessage:g1"
GROUP_UMO_2 = "aiocqhttp:GroupMessage:g2"
PRIVATE_UMO = "aiocqhttp:FriendMessage:f1"
L3_PRIVATE_UMO = "aiocqhttp:FriendMessage:l3"
L3_UID = "core-uid"


class _BridgeStub:
    """Minimal ``PlatformBridge`` stand-in (only ``get_platform_admins`` is used)."""

    def get_platform_admins(self, umo=None):
        return set()


def _make_relation_manager(db, config=None):
    return UserRelationManager(db, _BridgeStub(), config or make_plugin_config())


# ==============================================================================
# 1. Schema + legacy migration
# ==============================================================================
class TestProactiveSchema(DatabaseAsyncTestCase):
    async def test_both_tables_have_proactive_column(self):
        for table in ("users", "user_relations"):
            rows = await self.db.fetchall(f"PRAGMA table_info({table})")
            names = {str(row.get("name") or "") for row in rows or []}
            assert "proactive_enabled" in names, table

    async def test_legacy_database_migration_defaults_to_enabled(self):
        """A pre-v2.11 database is upgraded in place and old rows default to True."""
        path = os.path.join(self.tmpdir, "legacy.db")
        con = sqlite3.connect(path)
        try:
            con.execute(
                """
                CREATE TABLE users (
                    global_uid TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    nickname TEXT NOT NULL DEFAULT '',
                    tier INTEGER NOT NULL DEFAULT 1,
                    interaction_count INTEGER NOT NULL DEFAULT 0,
                    affinity REAL NOT NULL DEFAULT 0.0,
                    platform_ids_json TEXT NOT NULL DEFAULT '[]',
                    attitude TEXT NOT NULL DEFAULT '',
                    updated_at INTEGER NOT NULL
                );
                """
            )
            con.execute(
                """
                CREATE TABLE user_relations (
                    soul_id TEXT NOT NULL,
                    global_uid TEXT NOT NULL,
                    tier INTEGER NOT NULL DEFAULT 1,
                    interaction_count INTEGER NOT NULL DEFAULT 0,
                    affinity REAL NOT NULL DEFAULT 0.0,
                    nickname TEXT NOT NULL DEFAULT '',
                    attitude TEXT NOT NULL DEFAULT '',
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (soul_id, global_uid)
                );
                """
            )
            con.execute(
                "INSERT INTO users (global_uid, display_name, updated_at) "
                "VALUES ('legacy-uid', '旧用户', 1)"
            )
            con.execute(
                "INSERT INTO user_relations (soul_id, global_uid, updated_at) "
                "VALUES ('s1', 'legacy-uid', 1)"
            )
            con.commit()
        finally:
            con.close()

        db = DatabaseManager(path)
        try:
            await db.initialize()
            user = await db.fetchone(
                "SELECT proactive_enabled FROM users WHERE global_uid = 'legacy-uid'"
            )
            relation = await db.fetchone(
                "SELECT proactive_enabled FROM user_relations "
                "WHERE soul_id = 's1' AND global_uid = 'legacy-uid'"
            )
        finally:
            await db.close()
        assert int(user["proactive_enabled"]) == 1
        assert int(relation["proactive_enabled"]) == 1


# ==============================================================================
# 2. UserProfile / UserRelationManager persistence
# ==============================================================================
class TestUserProactiveToggle(DatabaseAsyncTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.config = make_plugin_config()
        self.manager = _make_relation_manager(self.db, self.config)

    async def _seed(self, soul_id, uid, umo=None):
        profile = await self.manager.get_or_create_user(
            user_id=uid,
            display_name="阿泽",
            umo=umo,
            soul_id=soul_id,
        )
        return profile

    async def test_profile_defaults_to_enabled(self):
        profile = UserProfile(global_uid="u-1", display_name="阿一")
        assert profile.proactive_enabled is True
        assert profile.to_dict()["proactive_enabled"] is True

    async def test_scoped_toggle_is_per_soul(self):
        profile = await self._seed("soul_a", "u-1")
        await self.manager.update_user_details(
            global_uid=profile.global_uid,
            soul_id="soul_a",
            proactive_enabled=False,
        )
        a = await self.manager.get_user(profile.global_uid, soul_id="soul_a")
        assert a is not None and a.proactive_enabled is False
        # a second Soul keeps the historical default (proactive still allowed)
        await self._seed("soul_b", "u-1")
        b = await self.manager.get_user(profile.global_uid, soul_id="soul_b")
        assert b is not None and b.proactive_enabled is True

    async def test_scoped_toggle_persists_in_relation_row(self):
        profile = await self._seed("soul_a", "u-2")
        await self.manager.update_user_details(
            global_uid=profile.global_uid,
            soul_id="soul_a",
            proactive_enabled=False,
        )
        row = await self.db.fetchone(
            "SELECT proactive_enabled FROM user_relations "
            "WHERE soul_id = 'soul_a' AND global_uid = ?",
            (profile.global_uid,),
        )
        assert int(row["proactive_enabled"]) == 0
        # cache is invalidated/stored consistently by update_user_details
        again = await self.manager.get_user(profile.global_uid, soul_id="soul_a")
        assert again.proactive_enabled is False

    async def test_legacy_update_without_soul_persists_to_users_table(self):
        profile = await self.manager.get_or_create_user("legacy-uid", "旧用户")
        await self.manager.update_user_details(
            global_uid=profile.global_uid, proactive_enabled=False
        )
        row = await self.db.fetchone(
            "SELECT proactive_enabled FROM users WHERE global_uid = ?",
            (profile.global_uid,),
        )
        assert int(row["proactive_enabled"]) == 0
        legacy = await self.manager.get_user(profile.global_uid)
        assert legacy.proactive_enabled is False

    async def test_list_users_scoped_exposes_flag(self):
        profile = await self._seed("soul_a", "u-3")
        await self.manager.update_user_details(
            global_uid=profile.global_uid,
            soul_id="soul_a",
            proactive_enabled=False,
        )
        users = await self.manager.list_users(soul_id="soul_a")
        match = [user for user in users if user.global_uid == profile.global_uid]
        assert match and match[0].proactive_enabled is False
        assert match[0].to_dict()["proactive_enabled"] is False

    async def test_merge_keeps_opt_out(self):
        primary = await self._seed("soul_a", "u-primary")
        secondary = await self._seed("soul_a", "u-secondary")
        await self.manager.update_user_details(
            global_uid=secondary.global_uid,
            soul_id="soul_a",
            proactive_enabled=False,
        )
        merged = await self.manager.merge_users(
            primary.global_uid, secondary.global_uid, soul_id="soul_a"
        )
        assert merged.proactive_enabled is False
        reloaded = await self.manager.get_user(primary.global_uid, soul_id="soul_a")
        assert reloaded is not None and reloaded.proactive_enabled is False


# ==============================================================================
# 3. GroupProfile persistence
# ==============================================================================
class TestGroupProactiveToggle(DatabaseAsyncTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.config = make_plugin_config()
        self.manager = _make_relation_manager(self.db, self.config)

    async def test_group_profile_defaults_to_enabled(self):
        group = GroupProfile(umo=GROUP_UMO)
        assert group.proactive_enabled is True
        assert group.to_dict()["proactive_enabled"] is True

    async def test_legacy_profile_without_key_defaults_to_enabled(self):
        group = GroupProfile.from_dict(
            {
                "umo": GROUP_UMO,
                "group_id": "g1",
                "platform_id": "aiocqnet",
                "display_name": "旧群",
            }
        )
        assert group is not None and group.proactive_enabled is True

    async def test_touch_group_creates_enabled_and_update_persists(self):
        await self.manager.touch_group(
            GROUP_UMO, group_id="g1", platform_id="aiocqhttp", display_name="测试群"
        )
        group = await self.manager.update_group(GROUP_UMO, proactive_enabled=False)
        assert group is not None and group.proactive_enabled is False
        # reload from plugin_settings (a fresh manager simulates a restart)
        reloaded_manager = _make_relation_manager(self.db, self.config)
        reloaded = await reloaded_manager.get_group(GROUP_UMO)
        assert reloaded is not None and reloaded.proactive_enabled is False
        raw = await self.db.get_setting(GROUP_SETTING_KEY, [])
        assert any(
            item.get("umo") == GROUP_UMO and item.get("proactive_enabled") is False
            for item in raw
        )

    async def test_update_group_none_keeps_current_value(self):
        await self.manager.touch_group(GROUP_UMO, group_id="g1", display_name="测试群")
        await self.manager.update_group(GROUP_UMO, proactive_enabled=False)
        group = await self.manager.update_group(GROUP_UMO, display_name="改名后的群")
        assert group is not None
        assert group.display_name == "改名后的群"
        assert group.proactive_enabled is False


# ==============================================================================
# 4. Web API: users/list + users/update (user & group actions)
# ==============================================================================
class TestWebApiProactiveToggle(DatabaseAsyncTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.plugin, self.api = await build_fake_plugin(self.db, self.tmpdir)
        self.soul = make_soul(soul_id="soul_a", card=None)
        await self.plugin.soul_manager.save_soul(self.soul)
        self.user = await self.plugin.user_relation.get_or_create_user(
            "u-api", "接口用户", soul_id="soul_a"
        )

    async def call(self, handler, query=None, body=None):
        self.api.set_mock_request("POST", query=query or {}, body=body or {})
        return await handler()

    def assert_ok(self, response):
        assert response["status"] == "ok", response
        return response["data"]

    async def test_users_list_exposes_flag(self):
        data = self.assert_ok(
            await self.call(
                self.api.api_users_list, query={"soul_id": "soul_a"}
            )
        )
        match = [
            user for user in data["users"]
            if user["global_uid"] == self.user.global_uid
        ]
        assert match and match[0]["proactive_enabled"] is True

    async def test_users_update_toggles_user_and_keeps_soul_scope(self):
        data = self.assert_ok(
            await self.call(
                self.api.api_users_update,
                body={
                    "global_uid": self.user.global_uid,
                    "soul_id": "soul_a",
                    "proactive_enabled": False,
                },
            )
        )
        assert data["user"]["proactive_enabled"] is False
        row = await self.db.fetchone(
            "SELECT proactive_enabled FROM user_relations "
            "WHERE soul_id = 'soul_a' AND global_uid = ?",
            (self.user.global_uid,),
        )
        assert int(row["proactive_enabled"]) == 0
        # legacy column untouched (the switch is per Soul)
        legacy = await self.db.fetchone(
            "SELECT proactive_enabled FROM users WHERE global_uid = ?",
            (self.user.global_uid,),
        )
        assert int(legacy["proactive_enabled"]) == 1

    async def test_users_update_omitting_flag_keeps_value(self):
        await self.call(
            self.api.api_users_update,
            body={
                "global_uid": self.user.global_uid,
                "soul_id": "soul_a",
                "proactive_enabled": False,
            },
        )
        data = self.assert_ok(
            await self.call(
                self.api.api_users_update,
                body={
                    "global_uid": self.user.global_uid,
                    "soul_id": "soul_a",
                    "display_name": "改名用户",
                },
            )
        )
        assert data["user"]["proactive_enabled"] is False

    async def test_update_group_action_toggles_and_exposes_flag(self):
        await self.plugin.user_relation.touch_group(
            GROUP_UMO,
            group_id="g1",
            platform_id="aiocqhttp",
            display_name="测试群",
            bound_soul_id="soul_a",
        )
        data = self.assert_ok(
            await self.call(
                self.api.api_users_update,
                body={
                    "action": "update_group",
                    "soul_id": "soul_a",
                    "umo": GROUP_UMO,
                    "proactive_enabled": False,
                },
            )
        )
        groups = {item["umo"]: item for item in data.get("groups", [])}
        assert groups[GROUP_UMO]["proactive_enabled"] is False
        list_data = self.assert_ok(
            await self.call(
                self.api.api_users_list, query={"soul_id": "soul_a"}
            )
        )
        listed = {item["umo"]: item for item in list_data["groups"]}
        assert listed[GROUP_UMO]["proactive_enabled"] is False


# ==============================================================================
# 5. main.py enforcement
# ==============================================================================
class TestMainProactiveGating(DatabaseAsyncTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        from test_regressions import (  # noqa: E402
            RecordingProactiveBridge,
            SequencedStubModelHub,
            bind_plugin_methods,
        )

        self.bind = bind_plugin_methods
        self.bridge_cls = RecordingProactiveBridge
        self.hub_cls = SequencedStubModelHub
        self.config = make_plugin_config(
            roguelite_schedule_config={
                "enable_roguelite_schedule": True,
                "max_mutations_per_day": 2,
                "mutation_cooldown_minutes": 120,
                "idle_roam_cooldown_minutes": 0,
                "allow_l3_wake_from_resting": True,
                "unread_private_reply_enabled": True,
                "unread_group_cooldown_minutes": 0,
                "unread_private_cooldown_minutes": 0,
            }
        )
        self.session_buffer = mods.context_budget.SessionBuffer(self.config)
        self.memory_manager = mods.memory_manager.MemoryManager(
            self.db, StubModelHub(), self.config
        )
        self.manager = _make_relation_manager(self.db, self.config)
        self.soul = make_soul("soul-a")
        self.current_slot = make_slot(
            state=BotState.IDLE,
            action_type=ActionType.IDLE_ROAM,
            title="趴在窗边发呆",
        )
        self.run = make_run(self.soul.soul_id)

    # ---- helpers -----------------------------------------------------
    def _fake(self, json_results=None, text_results=None):
        bridge = self.bridge_cls()
        hub = self.hub_cls(
            json_results=list(json_results or []),
            text_results=list(text_results or []),
        )
        fake = SimpleNamespace(
            config=self.config,
            model_hub=hub,
            memory_manager=self.memory_manager,
            session_buffer=self.session_buffer,
            platform_bridge=bridge,
            user_relation=self.manager,
            _known_group_umos=set(),
            _known_l3_umos={L3_UID: L3_PRIVATE_UMO},
            _last_idle_roam_ts={},
            _last_group_lurk_ts={},
            _last_private_catchup_ts={},
        )
        return self.bind(fake), bridge, hub

    async def _seed_user(self, uid, soul_id, enabled):
        profile = await self.manager.get_or_create_user(uid, display_name="阿泽", soul_id=soul_id)
        await self.manager.update_user_details(
            global_uid=profile.global_uid, soul_id=soul_id, proactive_enabled=enabled
        )
        return profile

    def _mark_group_unread(self, umo, content="群里刚聊到一个超大的瓜"):
        user = make_user("u-1", "群友")
        message = self.session_buffer.record_user_message(umo, user, content)
        assert message is not None
        self.session_buffer.mark_unread(umo, message, is_private=False)
        return message

    # ---- unit helpers -------------------------------------------------
    async def test_target_allowed_defaults_true(self):
        fake, _bridge, _hub = self._fake()
        assert await fake._proactive_target_allowed(self.soul, GROUP_UMO) is True
        assert await fake._proactive_target_allowed(self.soul, L3_PRIVATE_UMO) is True
        assert await fake._proactive_target_allowed(self.soul, "") is True

    async def test_target_allowed_group_and_user(self):
        fake, _bridge, _hub = self._fake()
        await self.manager.touch_group(GROUP_UMO, group_id="g1", display_name="测试群")
        await self.manager.update_group(GROUP_UMO, proactive_enabled=False)
        assert await fake._proactive_target_allowed(self.soul, GROUP_UMO) is False

        await self._seed_user(L3_UID, self.soul.soul_id, enabled=False)
        assert await fake._proactive_target_allowed(self.soul, L3_PRIVATE_UMO) is False

    async def test_latest_allowed_l3_umo_skips_disabled(self):
        fake, _bridge, _hub = self._fake()
        fake._known_l3_umos = {
            "uid-old": "aiocqhttp:FriendMessage:old",
            "uid-new": "aiocqhttp:FriendMessage:new",
        }
        await self._seed_user("uid-old", self.soul.soul_id, enabled=True)
        await self._seed_user("uid-new", self.soul.soul_id, enabled=False)
        target = await fake._latest_allowed_l3_umo(self.soul)
        assert target == "aiocqhttp:FriendMessage:old"
        await self.manager.update_user_details(
            global_uid="uid-old", soul_id=self.soul.soul_id, proactive_enabled=False
        )
        assert await fake._latest_allowed_l3_umo(self.soul) == ""

    # ---- private unread catch-up --------------------------------------
    async def test_private_catchup_skips_disabled_user_and_keeps_backlog(self):
        profile = await self._seed_user("u-private", self.soul.soul_id, enabled=False)
        user = make_user(profile.global_uid, "阿泽", tier=UserTier.L3_CORE)
        message = self.session_buffer.record_user_message(
            PRIVATE_UMO, user, "睡了吗？"
        )
        self.session_buffer.mark_unread(PRIVATE_UMO, message, is_private=True)
        fake, bridge, hub = self._fake(text_results=["刚看到你消息"])
        processed = await fake._catch_up_private_unread(
            self.soul, self.run, self.current_slot
        )
        assert processed == 0
        assert bridge.sent == [] and hub.calls == []
        # backlog is kept (not ACKed) so re-enabling the switch can still catch up
        assert self.session_buffer.get_unread_total() == 1

    async def test_private_catchup_works_when_enabled(self):
        profile = await self._seed_user("u-private", self.soul.soul_id, enabled=True)
        user = make_user(profile.global_uid, "阿泽", tier=UserTier.L3_CORE)
        message = self.session_buffer.record_user_message(
            PRIVATE_UMO, user, "睡了吗？"
        )
        self.session_buffer.mark_unread(PRIVATE_UMO, message, is_private=True)
        fake, bridge, _hub = self._fake(text_results=["刚看到你消息，现在才回～"])
        processed = await fake._catch_up_private_unread(
            self.soul, self.run, self.current_slot
        )
        assert processed == 1
        assert bridge.sent and bridge.sent[0][0] == PRIVATE_UMO

    # ---- GROUP_LURK / MEMORY_MUSE -------------------------------------
    async def test_group_lurk_skips_disabled_group(self):
        self._mark_group_unread(GROUP_UMO)
        await self.manager.touch_group(
            GROUP_UMO, group_id="g1", display_name="测试群", bound_soul_id=self.soul.soul_id
        )
        await self.manager.update_group(GROUP_UMO, proactive_enabled=False)
        fake, bridge, hub = self._fake(
            json_results=[
                {"decision": "REPLY", "confidence": 0.9},
                {"target": "GROUP", "message": "这瓜保熟吗"},
            ]
        )
        # v2.12: the probability gate is closed so only branch A runs (the group
        # is skipped, and the tick stops instead of falling through to a persona topic).
        fake.config["roguelite_schedule_config"]["idle_roam_trigger_probability"] = 0
        sent = await fake._run_idle_roam_action(
            self.soul, self.run, self.current_slot
        )
        assert sent is False
        assert bridge.sent == [] and hub.calls == []
        assert self.session_buffer.get_unread_total() == 1  # backlog kept

    async def test_group_lurk_picks_allowed_group_when_top_one_disabled(self):
        self._mark_group_unread(GROUP_UMO)  # more unread -> ranked first
        self._mark_group_unread(GROUP_UMO_2)
        for umo in (GROUP_UMO, GROUP_UMO_2):
            await self.manager.touch_group(
                umo,
                group_id=umo.rsplit(":", 1)[-1],
                display_name="测试群",
                bound_soul_id=self.soul.soul_id,
            )
        await self.manager.update_group(GROUP_UMO, proactive_enabled=False)
        fake, bridge, _hub = self._fake(
            json_results=[
                {"decision": "REPLY", "confidence": 0.9},
                {"target": "GROUP", "message": "这个话题我也来插一句"},
            ]
        )
        sent = await fake._run_idle_roam_action(
            self.soul, self.run, self.current_slot
        )
        assert sent is True
        assert bridge.sent and bridge.sent[0][0] == GROUP_UMO_2

    async def test_memory_muse_skips_disabled_private_target(self):
        await self.memory_manager.add_memory(
            soul_id=self.soul.soul_id,
            content="上次说好周末一起去看展",
            keywords="看展,周末",
            importance=5,
            memory_tier=2,
        )
        await self._seed_user(L3_UID, self.soul.soul_id, enabled=False)
        fake, bridge, hub = self._fake(
            json_results=[{"target": "PRIVATE_L3", "message": "突然想起你"}
            ]
        )
        sent = await fake._run_idle_roam_action(
            self.soul, self.run, self.current_slot
        )
        assert sent is False
        assert bridge.sent == []
        # the decision model still ran (target was resolved), but nothing was sent
        assert len(hub.calls) == 1


# ==============================================================================
# 6. Console page static contract
# ==============================================================================
class TestConsoleProactiveUi(unittest.TestCase):
    def _read(self, name):
        with open(os.path.join(PROJECT_ROOT, "views", "console", name), encoding="utf-8") as handle:
            return handle.read()

    def test_relations_page_has_user_and_group_toggles(self):
        source = self._read("page-relations.js")
        assert 'data-role="proactive-enabled"' in source
        assert 'data-role="group-proactive-enabled"' in source
        assert "proactive_enabled: proactiveEl" in source
        assert "proactiveEnabledOf" in source

    def test_group_normalizer_keeps_proactive_field(self):
        """回归：normalizeGroup 的白名单曾漏掉 proactive_enabled，导致保存后重开弹窗
        又恢复成「开启」（用户实测 bug）。该断言直接冻结 normalizeGroup 保留字段。"""
        source = self._read("page-relations.js")
        assert "proactive_enabled: proactiveEnabledOf(group)" in source
        assert "rawGroupFromPayload" in source

    @unittest.skipUnless(shutil.which("node"), "node 不可用，跳过 JS 行为回归")
    def test_group_normalizer_behaviour_with_node(self):
        """用 node 执行真实 normalizeGroup：false 必须保留，缺失字段默认 true。"""
        harness = """
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
function extract(name) {
  const start = src.indexOf("function " + name + "(");
  if (start < 0) throw new Error("missing " + name);
  let depth = 0;
  for (let i = src.indexOf("{", start); i < src.length; i++) {
    if (src[i] === "{") depth++;
    else if (src[i] === "}") {
      depth--;
      if (depth === 0) return src.slice(start, i + 1);
    }
  }
  throw new Error("unbalanced " + name);
}
function num(value, fallback) {
  const n = Number(value);
  return isFinite(n) ? n : fallback;
}
eval(extract("proactiveEnabledOf"));
eval(extract("normalizeGroup"));
const off = normalizeGroup({ umo: "qq:GroupMessage:1", proactive_enabled: false });
const on = normalizeGroup({ umo: "qq:GroupMessage:2", proactive_enabled: true });
const legacy = normalizeGroup({ umo: "qq:GroupMessage:3" });
const zero = normalizeGroup({ umo: "qq:GroupMessage:4", proactive_enabled: 0 });
if (off.proactive_enabled !== false) throw new Error("false lost: " + JSON.stringify(off));
if (on.proactive_enabled !== true) throw new Error("true lost: " + JSON.stringify(on));
if (legacy.proactive_enabled !== true) throw new Error("legacy default not true");
if (zero.proactive_enabled !== false) throw new Error("integer 0 not treated as false");
process.stdout.write("NORMALIZE_OK");
"""
        fd, path = tempfile.mkstemp(suffix=".js")
        try:
            os.write(fd, harness.encode("utf-8"))
            os.close(fd)
            result = subprocess.run(
                [shutil.which("node"), path, os.path.join(PROJECT_ROOT, "views", "console", "page-relations.js")],
                capture_output=True,
                text=True,
                timeout=30,
            )
        finally:
            os.unlink(path)
        assert result.returncode == 0, result.stderr
        assert "NORMALIZE_OK" in result.stdout, result.stdout

    def test_mock_exposes_switch(self):
        source = self._read("mock.js")
        assert "proactive_enabled" in source
        assert "group.proactive_enabled" in source or "proactive_enabled:" in source


if __name__ == "__main__":
    unittest.main()
