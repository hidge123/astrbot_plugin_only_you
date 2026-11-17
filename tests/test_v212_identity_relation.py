"""v2.12 身份归一与关系晋升曲线回归。

冻结契约：

1. ``users.aliases_json`` 幂等迁移；改名时旧名字自动收入 ``aliases``，
   ``canonical_name`` = 角色专属称呼 -> 主 display_name -> uid；
2. 绑定多平台账号 / 合并用户时，孤立记忆自动归并到主 ``global_uid``；
3. 只有 Bot **真正回复过**的回合才结算好感：同用户 180s 冷却、+0.4 群聊 /
   +0.6 直接或私聊、>=60 好感增速减半、每日 +5.0 上限；
4. L1 -> ◆L2 必须同时满足互动次数与好感度（AND），阈值可配；
5. 用户关系上下文注入【身份归一】说明。
"""

import unittest

from _helpers import (
    DatabaseAsyncTestCase,
    FakeEvent,
    FakeRequest,
    StubModelHub,
    load_plugin_modules,
    make_plugin_config,
    make_run,
    make_slot,
    make_soul,
    make_user,
)

mods = load_plugin_modules()
UserRelationManager = mods.user_relation.UserRelationManager
UserTier = mods.state_machine.UserTier
ContextBudgetManager = mods.context_budget.ContextBudgetManager
SessionBuffer = mods.context_budget.SessionBuffer
MemoryManager = mods.memory_manager.MemoryManager


class _Bridge:
    def __init__(self, admins=None):
        self.admins = list(admins or [])

    def get_platform_admins(self, umo=None):
        return list(self.admins)


class RelationTestBase(DatabaseAsyncTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.config = make_plugin_config()
        self.relation = UserRelationManager(self.db, _Bridge(), self.config)

    async def _make(self, uid, name="阿一", soul_id=None, **kwargs):
        return await self.relation.get_or_create_user(
            uid, name, soul_id=soul_id, **kwargs
        )


# ---------------------------------------------------------------------------
# 1. aliases / canonical name
# ---------------------------------------------------------------------------
class TestAliases(RelationTestBase):
    async def test_rename_accumulates_aliases_and_canonical_name(self):
        profile = await self._make("u-1", "胡翔宇", soul_id="soul-a")
        profile = await self._make("u-1", "胡翔宇（考研版）", soul_id="soul-a")
        assert profile.display_name == "胡翔宇（考研版）"
        assert "胡翔宇" in profile.aliases
        assert profile.canonical_name == "胡翔宇（考研版）"
        profile.nickname = "hidge"
        assert profile.canonical_name == "hidge"
        payload = profile.to_dict()
        assert payload["canonical_name"] == "hidge"
        assert "胡翔宇" in payload["aliases"]

    async def test_aliases_persist_across_manager_instances(self):
        profile = await self._make("u-1", "胡翔宇", soul_id="soul-a")
        await self._make("u-1", "hidge（考研版）", soul_id="soul-a")
        fresh = UserRelationManager(self.db, _Bridge(), self.config)
        loaded = await fresh.get_user("u-1", soul_id="soul-a")
        assert loaded is not None
        assert "胡翔宇" in loaded.aliases
        assert loaded.display_name == "hidge（考研版）"
        row = await self.db.fetchone("SELECT aliases_json FROM users WHERE global_uid = ?", ("u-1",))
        assert "胡翔宇" in row["aliases_json"]

    async def test_aliases_are_capped_and_deduped(self):
        await self._make("u-1", "名字0", soul_id="soul-a")
        for index in range(1, 20):
            await self._make("u-1", f"名字{index}", soul_id="soul-a")
        profile = await self.relation.get_user("u-1", soul_id="soul-a")
        assert len(profile.aliases) <= mods.user_relation.MAX_ALIASES
        assert len(set(profile.aliases)) == len(profile.aliases)

    async def test_platform_accounts_are_not_treated_as_aliases(self):
        profile = await self._make("u-1", "qq bot:12345", soul_id="soul-a")
        assert all(":" not in alias for alias in profile.aliases)


# ---------------------------------------------------------------------------
# 2. orphan memory reconciliation
# ---------------------------------------------------------------------------
class TestMemoryReconciliation(RelationTestBase):
    async def _seed_memory(self, user_id, content):
        manager = MemoryManager(self.db, StubModelHub(), self.config)
        return await manager.add_memory(
            soul_id="soul-a", content=content, user_id=user_id, importance=5
        )

    async def test_binding_platform_account_folds_orphan_memories(self):
        await self._make("qq bot:42", "阿一")
        orphan = await self._seed_memory("qq bot:42", "阿一喜欢喝冰美式")
        assert orphan > 0
        await self.relation.update_user_details(
            "qq bot:42", platform_ids=["qq bot:42", "telegram:42"]
        )
        # simulate a memory stored under the newly bound account
        await self._seed_memory("telegram:42", "阿一在学数电")
        await self.relation.update_user_details(
            "qq bot:42", platform_ids=["qq bot:42", "telegram:42"]
        )
        rows = await self.db.fetchall("SELECT user_id FROM memories")
        assert {row["user_id"] for row in rows} == {"qq bot:42"}

    async def test_legacy_bare_and_alias_memory_user_ids_are_folded(self):
        profile = await self._make("webchat:42", "胡翔宇")
        profile.platform_ids = ["webchat:42"]
        profile.aliases = ["hidge"]
        await self.relation.save_user(profile)
        # two identities share native uid "7" -> ambiguous, never auto-merged
        await self._make("webchat:7", "甲")
        await self._make("qq bot:7", "乙")

        for index, user_id in enumerate(("42", "hidge", "7", "unknown")):
            await self.db.execute(
                "INSERT INTO memories (soul_id, user_id, memory_tier, content, keywords,"
                " importance, created_at) VALUES ('soul-a', ?, 2, ?, 'k', 5, 1)",
                (user_id, f"历史记忆{index}"),
            )

        stats = await self.relation.migrate_legacy_memory_user_ids()
        assert stats["updated"] == 2
        assert stats["ambiguous"] >= 1
        rows = {
            row["content"]: row["user_id"]
            for row in await self.db.fetchall("SELECT content, user_id FROM memories")
        }
        assert rows["历史记忆0"] == "webchat:42"   # bare native uid folded
        assert rows["历史记忆1"] == "webchat:42"   # old alias folded
        assert rows["历史记忆2"] == "7"            # ambiguous -> untouched
        assert rows["历史记忆3"] == "unknown"      # no owner -> untouched

        # One-shot: the flag prevents a second pass.
        assert await self.relation.migrate_legacy_memory_user_ids() == {
            "updated": 0,
            "ambiguous": 0,
        }

    async def test_merge_users_folds_memories_and_aliases(self):
        primary = await self._make("qq bot:42", "胡翔宇", soul_id="soul-a")
        secondary = await self._make("telegram:42", "hidge", soul_id="soul-a")
        await self._seed_memory("telegram:42", "hidge 爱吃火锅")
        merged = await self.relation.merge_users(
            "qq bot:42", "telegram:42", soul_id="soul-a"
        )
        assert merged.global_uid == primary.global_uid
        assert "hidge" in merged.aliases
        rows = await self.db.fetchall("SELECT user_id FROM memories")
        assert {row["user_id"] for row in rows} == {primary.global_uid}
        assert await self.db.fetchone(
            "SELECT global_uid FROM users WHERE global_uid = ?", ("telegram:42",)
        ) is None
        _ = secondary


# ---------------------------------------------------------------------------
# 3. effective-reply-only affinity curve
# ---------------------------------------------------------------------------
class TestAffinityCurve(RelationTestBase):
    async def _user(self, uid="u-1", affinity=0.0, interactions=0, tier=None):
        profile = await self._make(uid, "阿一", soul_id="soul-a")
        profile.affinity = float(affinity)
        profile.interaction_count = int(interactions)
        if tier is not None:
            profile.tier = tier
        await self.relation.save_user(profile)
        return profile

    async def test_group_and_direct_gains(self):
        user = await self._user("u-1")
        now = 100000.0
        await self.relation.record_effective_interaction(
            "soul-a", user, is_private=False, now=now
        )
        assert round(user.affinity, 2) == 0.4
        other = await self._user("u-2")
        await self.relation.record_effective_interaction(
            "soul-a", other, is_direct_mention=True, now=now
        )
        assert round(other.affinity, 2) == 0.6

    async def test_cooldown_settles_one_burst(self):
        user = await self._user("u-1")
        now = 200000.0
        for offset in (0, 10, 60):
            await self.relation.record_effective_interaction(
                "soul-a", user, now=now + offset
            )
        assert user.interaction_count == 1
        await self.relation.record_effective_interaction(
            "soul-a", user, now=now + 181
        )
        assert user.interaction_count == 2

    async def test_daily_cap_limits_affinity(self):
        self.config["relationship_config"]["affinity_daily_cap"] = 1.0
        relation = UserRelationManager(self.db, _Bridge(), self.config)
        user = await relation.get_or_create_user("u-9", "阿九", soul_id="soul-a")
        now = 300000.0
        for index in range(10):
            await relation.record_effective_interaction(
                "soul-a", user, is_direct_mention=True, now=now + index * 200
            )
        assert round(user.affinity, 2) == 1.0

    async def test_affinity_above_sixty_grows_slower(self):
        user = await self._user("u-1", affinity=70.0)
        await self.relation.record_effective_interaction(
            "soul-a", user, is_direct_mention=True, now=400000.0
        )
        assert round(user.affinity, 2) == 70.3

    async def test_auto_promotion_requires_both_thresholds(self):
        low_affinity = await self._user("u-1", affinity=5.0, interactions=39)
        await self.relation.record_effective_interaction(
            "soul-a", low_affinity, now=500000.0
        )
        assert low_affinity.interaction_count == 40
        assert low_affinity.tier == UserTier.L1_NORMAL  # affinity 5.4 < 30

        low_interactions = await self._user("u-2", affinity=35.0, interactions=5)
        await self.relation.record_effective_interaction(
            "soul-a", low_interactions, now=500000.0
        )
        assert low_interactions.tier == UserTier.L1_NORMAL  # 6 < 40

        both = await self._user("u-3", affinity=30.0, interactions=39)
        await self.relation.record_effective_interaction(
            "soul-a", both, now=500000.0
        )
        assert both.tier == UserTier.L2_IMPORTANT

    async def test_thresholds_are_configurable(self):
        self.config["relationship_config"]["auto_promote_l2_min_interactions"] = 3
        self.config["relationship_config"]["auto_promote_l2_min_affinity"] = 0.5
        relation = UserRelationManager(self.db, _Bridge(), self.config)
        user = await relation.get_or_create_user("u-8", "阿八", soul_id="soul-a")
        now = 600000.0
        for index in range(3):
            await relation.record_effective_interaction(
                "soul-a", user, now=now + index * 200
            )
        assert user.interaction_count == 3
        assert user.tier == UserTier.L2_IMPORTANT

    async def test_resolve_user_does_not_bump_by_default(self):
        event = FakeEvent(message_str="你好", umo="qq bot:FriendMessage:42")
        user = await self.relation.resolve_user(event, soul_id="soul-a")
        assert user.interaction_count == 0
        assert user.affinity == 0.0
        relation_row = await self.db.fetchone(
            "SELECT interaction_count, affinity FROM user_relations"
            " WHERE soul_id = ? AND global_uid = ?",
            ("soul-a", user.global_uid),
        )
        assert int(relation_row["interaction_count"]) == 0
        assert float(relation_row["affinity"]) == 0.0


# ---------------------------------------------------------------------------
# 4. identity block in the LLM prompt
# ---------------------------------------------------------------------------
class TestIdentityPrompt(DatabaseAsyncTestCase):
    async def test_relation_block_includes_aliases_and_platforms(self):
        config = make_plugin_config()
        relation = UserRelationManager(self.db, _Bridge(), config)
        profile = await relation.get_or_create_user("qq bot:42", "胡翔宇", soul_id="soul-a")
        await relation.update_user_details(
            "qq bot:42",
            display_name="hidge（考研版）",
            nickname="hidge",
            platform_ids=["qq bot:42", "telegram:42"],
            soul_id="soul-a",
        )
        user = await relation.get_user("qq bot:42", soul_id="soul-a")
        _ = profile

        budget = ContextBudgetManager(
            memory_manager=MemoryManager(self.db, StubModelHub(), config),
            session_buffer=SessionBuffer(config),
            plugin_config=config,
            soul_manager=None,
            user_relation=relation,
        )
        req = FakeRequest(prompt="在吗", system_prompt="BASE")
        await budget.apply_context_and_memory_budget(
            event=FakeEvent(message_str="在吗", umo="qq bot:FriendMessage:42"),
            req=req,
            soul=make_soul("soul-a"),
            user=user,
            run=make_run(),
            current_slot=make_slot(state=mods.state_machine.BotState.IDLE),
        )
        prompt = req.system_prompt
        assert "身份归一：标准称呼「hidge」" in prompt
        assert "胡翔宇" in prompt  # previous display name kept as alias
        assert "telegram:42" in prompt
        assert "切勿拆分为两个人" in prompt


if __name__ == "__main__":
    unittest.main()
