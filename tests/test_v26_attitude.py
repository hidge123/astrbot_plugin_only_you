"""Offline tests for OnlyYou v2.6 Phase 2 + Phase 5 backend data layer.

Covered contract sections:
- §2.1 ``core/character_card.py``: ``social`` section drops ``attitude_l3/l2/l1``
  (10 sections / 36 fields), ``normalize_card`` explicitly drops the retired keys
  of stale cards, ``render_persona_prompt`` only renders ``group_role``.
- §2.2 ``storage/db.py`` + ``core/user_relation.py``: ``users.attitude`` column with
  an idempotent PRAGMA/ALTER migration, ``UserProfile.attitude`` round-trip through
  ``to_dict`` / ``get_or_create_user`` / ``list_users`` / ``save_user`` /
  ``update_user_details`` (max 300 chars) / ``merge_users``.
- §2.3 ``core/context_budget.py``: ``USER_RELATION_CONTEXT_PROMPT`` injection with the
  current speaker profile + the ``window_peers_block`` of other configured speakers
  (max 4) and the "never block the LLM request" guarantee.
- §5.1 ``identity.avatar``: Data URL / http(s) URL / short-text normalization and the
  hard rule that the avatar never enters the persona prompt.
"""

import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _helpers import (  # noqa: E402
    DatabaseAsyncTestCase,
    FakeEvent,
    FakeRequest,
    FakeSoulManager,
    StubMemoryManager,
    load_plugin_modules,
    make_plugin_config,
    make_run,
    make_slot,
    make_soul,
    make_user,
)

mods = load_plugin_modules()
cc = mods.character_card
ContextBudgetManager = mods.context_budget.ContextBudgetManager
SessionBuffer = mods.context_budget.SessionBuffer
BotState = mods.state_machine.BotState
UserTier = mods.state_machine.UserTier
UserRelationManager = mods.user_relation.UserRelationManager

_AVATAR_DATA_URL_PREFIX = "data:image/png;base64,"


def _normalize_avatar(value):
    card = cc.normalize_card({"identity": {"name": "星野", "avatar": value}})
    return card["identity"]["avatar"]


def _data_url(payload="AAAA", image_type="png"):
    return "data:image/%s;base64,%s" % (image_type, payload)


class _StubPlatformBridge:
    """Minimal ``PlatformBridge`` stand-in exposing ``get_platform_admins``."""

    def __init__(self, admins=None):
        self._admins = list(admins or [])

    def get_platform_admins(self, umo=None):
        _ = umo
        return list(self._admins)


# ===========================================================================
# §2.1 Character card: social attitudes removed, stale keys dropped
# ===========================================================================
class TestCardSocialAttitudeRemoval(unittest.TestCase):
    def test_schema_is_ten_sections_and_thirty_six_fields(self):
        assert len(cc.CARD_SCHEMA) == 10
        assert len({section["id"] for section in cc.CARD_SCHEMA}) == 10
        total = sum(len(section["fields"]) for section in cc.CARD_SCHEMA)
        assert total == 36
        social = next(s for s in cc.CARD_SCHEMA if s["id"] == "social")
        assert [field["key"] for field in social["fields"]] == ["group_role"]

    def test_no_section_still_declares_attitude_fields(self):
        keys = [
            field["key"]
            for section in cc.CARD_SCHEMA
            for field in section["fields"]
        ]
        assert "attitude_l3" not in keys
        assert "attitude_l2" not in keys
        assert "attitude_l1" not in keys

    def test_empty_card_social_only_has_group_role(self):
        assert set(cc.empty_card()["social"]) == {"group_role"}
        completion = cc.card_completion(cc.empty_card())
        assert completion["social"] == {"filled": 0, "total": 1}

    def test_normalize_card_drops_retired_social_attitude_keys(self):
        card = cc.normalize_card(
            {
                "social": {
                    "attitude_l3": "会撒娇、直呼昵称，消息秒回",
                    "attitude_l2": "熟络但不越界",
                    "attitude_l1": "礼貌但有距离感",
                    "group_role": "气氛组",
                    "future_social_key": {"kept": True},
                }
            }
        )
        assert card["social"]["group_role"] == "气氛组"
        assert "attitude_l3" not in card["social"]
        assert "attitude_l2" not in card["social"]
        assert "attitude_l1" not in card["social"]
        # Unknown keys inside a known section keep their old "preserve" semantics.
        assert card["social"]["future_social_key"] == {"kept": True}

    def test_stale_card_persona_prompt_only_renders_group_role(self):
        raw = {
            "identity": {"name": "星野"},
            "social": {
                "attitude_l3": "会撒娇、直呼昵称，消息秒回",
                "attitude_l2": "熟络但不越界",
                "attitude_l1": "礼貌但有距离感",
                "group_role": "气氛组",
            },
        }
        prompt = cc.render_persona_prompt(raw)
        assert "## 社交关系" in prompt
        assert "- 群聊中的角色定位：气氛组" in prompt
        assert "会撒娇" not in prompt
        assert "熟络但不越界" not in prompt
        assert "礼貌但有距离感" not in prompt
        assert "attitude_l3" not in prompt

    def test_legacy_wrapped_card_also_drops_retired_keys(self):
        card = cc.migrate_legacy(
            {"card": {"social": {"attitude_l3": "旧卡态度", "group_role": "潜水党"}}}
        )
        assert card["social"] == {"group_role": "潜水党"}


# ===========================================================================
# §5.1 identity.avatar normalization + persona prompt protection
# ===========================================================================
class TestAvatarNormalization(unittest.TestCase):
    def test_emoji_and_short_text_are_kept(self):
        assert _normalize_avatar("🌙") == "🌙"
        assert _normalize_avatar("  星  ") == "星"
        assert _normalize_avatar("星" * 32) == "星" * 32

    def test_long_plain_text_is_truncated_to_32_chars(self):
        assert _normalize_avatar("星" * 40) == "星" * 32
        assert len(_normalize_avatar("a" * 500)) == 32

    def test_none_and_garbage_become_empty_string(self):
        assert _normalize_avatar(None) == ""
        assert cc._coerce_avatar(None) == ""
        assert cc._coerce_avatar({"bad": "type"}) == '{"bad": "type"}'[:32]

    def test_valid_png_data_url_is_kept(self):
        url = _data_url("iVBORw0KGgoAAAANSUhEUg==")
        assert _normalize_avatar(url) == url

    def test_all_supported_data_url_image_types_are_kept(self):
        for image_type in ("png", "jpeg", "jpg", "webp", "gif"):
            url = _data_url("QUJDRA==", image_type=image_type)
            assert _normalize_avatar(url) == url

    def test_data_url_embedded_whitespace_is_stripped(self):
        spaced = "data:image/jpeg;base64,QUJD\nRA==\r\n"
        assert _normalize_avatar(spaced) == "data:image/jpeg;base64,QUJDRA=="

    def test_invalid_data_url_prefix_or_base64_becomes_empty(self):
        assert _normalize_avatar("data:image/svg+xml;base64,AAAA") == ""
        assert _normalize_avatar("data:text/html;base64,AAAA") == ""
        assert _normalize_avatar("data:image/png,AAAA") == ""
        assert _normalize_avatar("data:image/png;base64,") == ""
        assert _normalize_avatar("data:image/png;base64,!!!!") == ""
        assert _normalize_avatar("data:image/png;base64,AAA A BBB") == (
            "data:image/png;base64,AAAABBB"
        )

    def test_oversized_data_url_becomes_empty(self):
        prefix_len = len(_AVATAR_DATA_URL_PREFIX)
        too_long = _AVATAR_DATA_URL_PREFIX + "A" * (120001 - prefix_len)
        assert _normalize_avatar(too_long) == ""
        # Exactly at the 120000-char boundary is still accepted.
        boundary = _AVATAR_DATA_URL_PREFIX + "A" * (120000 - prefix_len)
        assert _normalize_avatar(boundary) == boundary

    def test_http_and_https_urls_are_kept(self):
        assert _normalize_avatar("https://example.com/avatar.png") == (
            "https://example.com/avatar.png"
        )
        assert _normalize_avatar("http://example.com/a.jpg") == "http://example.com/a.jpg"

    def test_local_root_relative_path_is_kept(self):
        assert _normalize_avatar("/static/img/avatar.webp") == "/static/img/avatar.webp"

    def test_oversized_url_becomes_empty(self):
        too_long = "https://example.com/" + "a" * 500
        assert _normalize_avatar(too_long) == ""
        boundary = "https://example.com/" + "a" * (500 - len("https://example.com/"))
        assert _normalize_avatar(boundary) == boundary

    def test_persona_prompt_never_contains_base64_avatar(self):
        big_avatar = _data_url("B" * 4000, image_type="webp")
        card = cc.normalize_card(
            {
                "identity": {"name": "星野", "avatar": big_avatar},
                "social": {"group_role": "气氛组"},
            }
        )
        prompt = cc.render_persona_prompt(card)
        assert "base64" not in prompt
        assert "data:image" not in prompt
        assert big_avatar[:64] not in prompt
        assert "头像" not in prompt
        assert "你正在扮演「星野」。" in prompt

    def test_persona_prompt_skips_emoji_avatar_too(self):
        card = cc.normalize_card({"identity": {"name": "星野", "avatar": "🌙"}})
        prompt = cc.render_persona_prompt(card)
        assert "🌙" not in prompt


# ===========================================================================
# §2.2 users.attitude persistence (DB migration + UserRelationManager)
# ===========================================================================
class AttitudeTestBase(DatabaseAsyncTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.config = make_plugin_config()

    def make_relation(self, db=None):
        return UserRelationManager(
            db or self.db, _StubPlatformBridge(), self.config
        )


class TestAttitudePersistence(AttitudeTestBase):
    async def test_defaults_to_empty_and_roundtrips_through_db(self):
        relation = self.make_relation()
        profile = await relation.get_or_create_user("user-att-1", "阿一")
        assert profile.attitude == ""
        assert profile.to_dict()["attitude"] == ""

        profile.attitude = "把 Ta 当成最信任的损友"
        await relation.save_user(profile)

        fresh = self.make_relation()  # empty memory cache, reads from SQLite
        loaded = await fresh.get_or_create_user("user-att-1")
        assert loaded.attitude == "把 Ta 当成最信任的损友"
        assert loaded.to_dict()["attitude"] == "把 Ta 当成最信任的损友"

        listed = await fresh.list_users()
        assert len(listed) == 1
        assert listed[0].attitude == "把 Ta 当成最信任的损友"

    async def test_update_user_details_caps_attitude_at_300_chars(self):
        relation = self.make_relation()
        profile = await relation.update_user_details(
            "user-att-2", display_name="阿二", nickname="老二", attitude="  毒舌但护短  "
        )
        assert profile.attitude == "毒舌但护短"

        profile = await relation.update_user_details(
            "user-att-2", attitude="态" * 500
        )
        assert profile.attitude == "态" * 300
        assert len(profile.attitude) == 300

        fresh = self.make_relation()
        persisted = await fresh.get_or_create_user("user-att-2")
        assert persisted.attitude == "态" * 300

    async def test_update_user_details_omitted_attitude_keeps_old_value(self):
        relation = self.make_relation()
        await relation.update_user_details("user-att-3", attitude="别丢了我")
        profile = await relation.update_user_details(
            "user-att-3", nickname="新称呼", tier=2
        )
        assert profile.attitude == "别丢了我"
        assert profile.nickname == "新称呼"

    async def test_empty_attitude_can_clear_previous_value(self):
        relation = self.make_relation()
        await relation.update_user_details("user-att-4", attitude="先写点态度")
        profile = await relation.update_user_details("user-att-4", attitude="   ")
        assert profile.attitude == ""

    async def test_merge_users_inherits_secondary_attitude_when_primary_empty(self):
        relation = self.make_relation()
        await relation.update_user_details("primary-1", display_name="主号")
        await relation.update_user_details(
            "secondary-1", display_name="副号", attitude="副号的专属态度"
        )
        merged = await relation.merge_users("primary-1", "secondary-1")
        assert merged.attitude == "副号的专属态度"
        reloaded = await self.make_relation().get_or_create_user("primary-1")
        assert reloaded.attitude == "副号的专属态度"

    async def test_merge_users_keeps_primary_attitude(self):
        relation = self.make_relation()
        await relation.update_user_details("primary-2", attitude="主号自己的态度")
        await relation.update_user_details("secondary-2", attitude="副号态度")
        merged = await relation.merge_users("primary-2", "secondary-2")
        assert merged.attitude == "主号自己的态度"


class TestUsersAttitudeMigration(AttitudeTestBase):
    async def test_initialize_adds_attitude_column_to_legacy_users_table(self):
        legacy_path = os.path.join(self.tmpdir, "legacy-onlyyou.db")
        conn = sqlite3.connect(legacy_path)
        conn.execute(
            """
            CREATE TABLE users (
                global_uid TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                nickname TEXT NOT NULL DEFAULT '',
                tier INTEGER NOT NULL DEFAULT 1,
                interaction_count INTEGER NOT NULL DEFAULT 0,
                affinity REAL NOT NULL DEFAULT 0.0,
                platform_ids_json TEXT NOT NULL DEFAULT '[]',
                updated_at INTEGER NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT INTO users (global_uid, display_name, updated_at) VALUES (?, ?, ?)",
            ("legacy-user-1", "旧用户", 123),
        )
        conn.commit()
        conn.close()

        legacy_db = mods.db.DatabaseManager(legacy_path)
        await legacy_db.initialize()
        try:
            columns = await legacy_db.fetchall("PRAGMA table_info(users);")
            names = {str(row.get("name") or "") for row in columns}
            assert "attitude" in names

            row = await legacy_db.fetchone(
                "SELECT attitude FROM users WHERE global_uid = ?", ("legacy-user-1",)
            )
            assert row is not None and row["attitude"] == ""

            await legacy_db.execute(
                "UPDATE users SET attitude = ? WHERE global_uid = ?",
                ("历史专属态度", "legacy-user-1"),
            )
            row = await legacy_db.fetchone(
                "SELECT attitude FROM users WHERE global_uid = ?", ("legacy-user-1",)
            )
            assert row["attitude"] == "历史专属态度"

            # A second initialize() on the migrated database must be a no-op.
            second_db = mods.db.DatabaseManager(legacy_path)
            await second_db.initialize()
            try:
                again = await second_db.fetchone(
                    "SELECT attitude FROM users WHERE global_uid = ?",
                    ("legacy-user-1",),
                )
                assert again["attitude"] == "历史专属态度"
            finally:
                await second_db.close()
        finally:
            await legacy_db.close()

    async def test_fresh_database_accepts_attitude_without_migration(self):
        await self.db.execute(
            "INSERT INTO users (global_uid, display_name, attitude, updated_at) "
            "VALUES (?, ?, ?, ?)",
            ("fresh-user-1", "新用户", "新建即写态度", 1),
        )
        row = await self.db.fetchone(
            "SELECT attitude FROM users WHERE global_uid = ?", ("fresh-user-1",)
        )
        assert row["attitude"] == "新建即写态度"


# ===========================================================================
# §2.3 USER_RELATION_CONTEXT_PROMPT injection + window peers
# ===========================================================================
class UserRelationInjectionTestBase(AttitudeTestBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.relation = self.make_relation()
        self.session_buffer = SessionBuffer(self.config)
        self.budget = ContextBudgetManager(
            memory_manager=StubMemoryManager([]),
            session_buffer=self.session_buffer,
            plugin_config=self.config,
            soul_manager=FakeSoulManager([]),
            user_relation=self.relation,
        )
        self.soul = make_soul()
        self.user = await self.relation.update_user_details(
            "user-1",
            display_name="阿一",
            nickname="一一",
            attitude="把 Ta 当成最信任的损友",
            affinity=12.34,
        )

    def build_budget(self, user_relation, session_buffer=None):
        return ContextBudgetManager(
            memory_manager=StubMemoryManager([]),
            session_buffer=session_buffer or self.session_buffer,
            plugin_config=self.config,
            soul_manager=FakeSoulManager([]),
            user_relation=user_relation,
        )

    async def apply(self, budget=None, user=None, umo="umo-1", prompt="在吗"):
        req = FakeRequest(prompt=prompt, system_prompt="BASE")
        await (budget or self.budget).apply_context_and_memory_budget(
            event=FakeEvent(message_str=prompt, umo=umo),
            req=req,
            soul=self.soul,
            user=user or self.user,
            run=make_run(),
            current_slot=make_slot(state=BotState.IDLE),
        )
        return req


class TestUserRelationPromptInjection(UserRelationInjectionTestBase):
    async def test_tier_law_and_current_speaker_profile_are_injected(self):
        req = await self.apply()
        sp = req.system_prompt
        assert "BASE" in sp
        assert "[用户关系等级与重要度法则]" in sp
        assert "【★L3 核心用户】" in sp
        assert "【◆L2 重要熟人】" in sp
        assert "【L1 普通群友】" in sp
        assert "[当前对话对象档案]" in sp
        assert "对话人：阿一 (ID: user-1)" in sp
        assert "关系等级：L1普通群友" in sp
        assert "(亲密度 12.3)" in sp
        assert "专属称呼：一一" in sp
        assert "你对 Ta 的专属态度：把 Ta 当成最信任的损友" in sp
        assert "关系备注：无" in sp
        # No other active speakers -> no peers section.
        assert "[当前群聊其他活跃对话者档案]" not in sp

    async def test_relation_notes_getattr_fallback_is_rendered(self):
        # ``UserProfile`` has no ``relation_notes`` field (v2.6 freeze: no new DB
        # column), so the block must read it through ``getattr``.
        assert not hasattr(mods.user_relation.UserProfile("u", "n"), "relation_notes")
        self.user.relation_notes = "大学室友，认识很多年了"
        req = await self.apply()
        assert "关系备注：大学室友，认识很多年了" in req.system_prompt

    async def test_empty_nickname_and_attitude_use_documented_defaults(self):
        plain_user = await self.relation.get_or_create_user("user-plain", "素人")
        req = await self.apply(user=plain_user)
        sp = req.system_prompt
        assert "专属称呼：默认自然称呼" in sp
        assert "你对 Ta 的专属态度：按关系等级与你的人设性格自然相处" in sp
        assert "对话人：素人 (ID: user-plain)" in sp

    async def test_window_peers_are_injected_with_nickname_and_attitude(self):
        peer = await self.relation.update_user_details(
            "user-2",
            display_name="阿二",
            nickname="小二",
            attitude="嘴上嫌弃但每次都帮他兜底",
        )
        plain_peer = await self.relation.get_or_create_user("user-3", "路人丙")
        self.session_buffer.record_user_message("umo-1", peer, "群里的闲聊")
        self.session_buffer.record_user_message("umo-1", plain_peer, "我也说一句")

        req = await self.apply()
        sp = req.system_prompt
        assert "[当前群聊其他活跃对话者档案]" in sp
        assert (
            "- 阿二 (ID: user-2) | 关系等级：L1普通群友"
            " | 专属称呼：小二"
            " | 专属态度：嘴上嫌弃但每次都帮他兜底"
        ) in sp
        # The current speaker must not be duplicated as a peer.
        assert sp.count(" | 专属称呼：") == 1
        # A speaker without nickname / attitude is not a "configured" peer.
        assert "user-3" not in sp

    async def test_window_peers_are_capped_at_four(self):
        for index in range(5):
            uid = "peer-%d" % index
            peer = await self.relation.update_user_details(
                uid, display_name="群友%d" % index, nickname="昵称%d" % index
            )
            self.session_buffer.record_user_message("umo-1", peer, "消息%d" % index)

        req = await self.apply()
        sp = req.system_prompt
        assert "[当前群聊其他活跃对话者档案]" in sp
        assert sp.count(" | 专属称呼：") == 4
        assert sp.count("(ID: peer-") == 4

    async def test_window_sender_ids_returns_distinct_user_ids_newest_first(self):
        first = await self.relation.update_user_details("sender-a", display_name="A")
        self.session_buffer.record_user_message("umo-1", first, "第一句")
        self.session_buffer.record_user_message("umo-1", first, "第二句")
        second = await self.relation.update_user_details("sender-b", display_name="B")
        self.session_buffer.record_user_message("umo-1", second, "第三句")
        assert self.session_buffer.get_window_sender_ids("umo-1") == [
            "sender-b",
            "sender-a",
        ]
        assert self.session_buffer.get_window_sender_ids("unknown-umo") == []

    async def test_no_relation_manager_still_injects_base_block(self):
        budget = self.build_budget(user_relation=None)
        req = await self.apply(budget=budget)
        sp = req.system_prompt
        assert "[用户关系等级与重要度法则]" in sp
        assert "对话人：阿一 (ID: user-1)" in sp
        assert "[当前群聊其他活跃对话者档案]" not in sp

    async def test_peers_lookup_failure_is_silent_and_keeps_base_block(self):
        class _ExplodingRelation:
            async def list_users(self):
                raise RuntimeError("boom")

        peer = await self.relation.update_user_details(
            "user-9", display_name="崩坏测试", nickname="小九"
        )
        self.session_buffer.record_user_message("umo-1", peer, "触发 peers 查询")
        budget = self.build_budget(user_relation=_ExplodingRelation())
        req = await self.apply(budget=budget)
        sp = req.system_prompt
        assert "[用户关系等级与重要度法则]" in sp
        assert "对话人：阿一 (ID: user-1)" in sp
        assert "[当前群聊其他活跃对话者档案]" not in sp

    async def test_block_appears_before_memory_block_and_keeps_state_injections(self):
        memory = mods.memory_manager.MemoryItem(
            id=1,
            soul_id=self.soul.soul_id,
            user_id=self.user.global_uid,
            user_tier=1,
            memory_tier=2,
            content="用户喜欢猫",
            keywords="猫",
            importance=8,
            vitality=100.0,
            access_count=0,
            created_at=int(__import__("time").time()),
            last_accessed_at=None,
        )
        req = FakeRequest(prompt="我家猫", system_prompt="BASE")
        budget = ContextBudgetManager(
            memory_manager=StubMemoryManager([memory]),
            session_buffer=self.session_buffer,
            plugin_config=self.config,
            soul_manager=FakeSoulManager([]),
            user_relation=self.relation,
        )
        await budget.apply_context_and_memory_budget(
            event=FakeEvent(message_str="我家猫", umo="umo-1"),
            req=req,
            soul=self.soul,
            user=self.user,
            run=make_run(),
            current_slot=make_slot(state=BotState.IDLE),
        )
        sp = req.system_prompt
        assert "当前三态控制器" in sp
        assert sp.index("[用户关系等级与重要度法则]") < sp.index("[脑海中的相关记忆")
        assert "- [近期记忆] 用户喜欢猫" in sp


# ===========================================================================
# Constructor backward compatibility
# ===========================================================================
class TestContextBudgetConstructorCompatibility(unittest.TestCase):
    def test_user_relation_defaults_to_none(self):
        config = make_plugin_config()
        budget = ContextBudgetManager(
            memory_manager=StubMemoryManager([]),
            session_buffer=SessionBuffer(config),
            plugin_config=config,
        )
        assert budget.user_relation is None

    def test_user_relation_can_be_injected(self):
        config = make_plugin_config()
        marker = object()
        budget = ContextBudgetManager(
            memory_manager=StubMemoryManager([]),
            session_buffer=SessionBuffer(config),
            plugin_config=config,
            soul_manager=None,
            user_relation=marker,
        )
        assert budget.user_relation is marker


if __name__ == "__main__":
    unittest.main()
