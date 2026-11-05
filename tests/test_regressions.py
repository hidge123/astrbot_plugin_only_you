"""AGENTS.md Step-2 regression suite (offline, no network, no AstrBot install).

Five groups:
1. ``RogueliteDeckEngine`` — 6 time slots, same-day stability, reroll, persistence,
   current-slot resolution (including the overnight ``night`` slot).
2. ``StateController`` — RESTING/BUSY/IDLE x L1/L2/L3 message gate matrix plus the
   surf-video / proactive-chat gates.
3. ``BoundedScheduleMutator`` — the triple guardrail (daily quota / cooldown /
   ``Impact > Armor``) and the deterministic mutation fallback.
4. ``MemoryManager.run_forgetting_gc`` — Tier 1 expiry, Tier 2 vitality decay and
   deletion, Tier 3 promotion, access reinforcement.
5. ``ContextBudgetManager`` — ``req.contexts.clear()`` + sliding-window injection
   with de-duplication and the memory count/char hard budgets.

Also runs ``compileall`` (to ``/tmp`` pycache) and imports ``OnlyYou.main`` offline.
"""

import inspect
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _helpers import (  # noqa: E402
    FakeEvent,
    FakeRequest,
    FakeSoulManager,
    StubModelHub,
    card_with,
    load_plugin_modules,
    make_plugin_config,
    make_run,
    make_slot,
    make_soul,
    make_user,
)

mods = load_plugin_modules()
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

RogueliteDeckEngine = mods.roguelite_deck.RogueliteDeckEngine
SLOT_DEFINITIONS = mods.roguelite_deck.SLOT_DEFINITIONS
StateController = mods.state_machine.StateController
BotState = mods.state_machine.BotState
UserTier = mods.state_machine.UserTier
BoundedScheduleMutator = mods.schedule_mutator.BoundedScheduleMutator
MemoryManager = mods.memory_manager.MemoryManager
MemoryTier = mods.memory_manager.MemoryTier
ContextBudgetManager = mods.context_budget.ContextBudgetManager
SessionBuffer = mods.context_budget.SessionBuffer


# ===========================================================================
# 1. Roguelite 6-slot schedule
# ===========================================================================
class TestRogueliteDeck(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="onlyyou-tests-")
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self._temp_db = mods.db.DatabaseManager(os.path.join(self.tmpdir, "only.you.db"))
        self.db = self._temp_db
        await self.db.initialize()
        self.config = make_plugin_config()
        self.engine = RogueliteDeckEngine(self.db, self.config)

    async def asyncTearDown(self):
        await self._temp_db.close()

    async def test_draws_all_six_slots(self):
        run = await self.engine.get_or_build_today_run("soul-a")
        assert len(run.slots) == 6
        assert [slot.slot_key for slot in run.slots] == [
            item["slot_key"] for item in SLOT_DEFINITIONS
        ]
        for slot, definition in zip(run.slots, SLOT_DEFINITIONS):
            assert slot.label == definition["label"]
            assert slot.start_hour == definition["start_hour"]
            assert slot.end_hour == definition["end_hour"]
            assert isinstance(slot.state, BotState)
            assert 0 <= slot.armor <= 100
            assert slot.title
        assert len(run.encounters) == 2
        assert all(encounter.content for encounter in run.encounters)
        assert run.date_str

    async def test_same_day_run_is_stable_and_persisted(self):
        first = await self.engine.get_or_build_today_run("soul-a")
        second = await self.engine.get_or_build_today_run("soul-a")
        assert second is first  # in-memory cache

        # a brand-new engine (empty cache) must load the persisted run from SQLite
        other_engine = RogueliteDeckEngine(self.db, self.config)
        reloaded = await other_engine.get_or_build_today_run("soul-a")
        assert reloaded.to_dict() == first.to_dict()

        # a different soul gets an independent run
        other_soul_run = await other_engine.get_or_build_today_run("soul-b")
        assert other_soul_run.soul_id == "soul-b"

    async def test_reroll_changes_schedule_and_keeps_six_slots(self):
        original = await self.engine.get_or_build_today_run("soul-a")
        rerolled = None
        changed = False
        for _ in range(3):
            rerolled = await self.engine.get_or_build_today_run(
                "soul-a", force_reroll=True
            )
            assert len(rerolled.slots) == 6
            if rerolled.to_dict() != original.to_dict():
                changed = True
                break
        assert changed, "reroll should (almost surely) produce a different schedule"
        # reroll result replaces the persisted row
        fresh_engine = RogueliteDeckEngine(self.db, self.config)
        persisted = await fresh_engine.get_or_build_today_run("soul-a")
        assert persisted.to_dict() == rerolled.to_dict()

    async def test_custom_cards_and_encounters_enter_the_pool(self):
        soul = make_soul("soul-c")
        assert soul.state_event_cards == []
        # Ask for the whole encounter pool so the custom entry is guaranteed to be drawn.
        config = make_plugin_config(
            roguelite_schedule_config={
                "enable_roguelite_schedule": True,
                "daily_encounter_count": 100,
                "max_mutations_per_day": 2,
                "mutation_cooldown_minutes": 120,
            }
        )
        engine = RogueliteDeckEngine(self.db, config)
        run = await engine.get_or_build_today_run(
            "soul-c",
            custom_encounters=["独家奇遇：今天捡到一只猫"],
            force_reroll=True,
        )
        assert len(run.slots) == 6
        assert any("独家奇遇" in encounter.content for encounter in run.encounters)

    async def test_resolve_slot_index_for_time(self):
        run = await self.engine.get_or_build_today_run("soul-a")
        from datetime import datetime

        cases = [
            (datetime(2024, 1, 2, 8, 0), "morning"),
            (datetime(2024, 1, 2, 10, 0), "forenoon"),
            (datetime(2024, 1, 2, 13, 0), "noon"),
            (datetime(2024, 1, 2, 15, 0), "afternoon"),
            (datetime(2024, 1, 2, 20, 0), "evening"),
            (datetime(2024, 1, 2, 2, 0), "night"),
        ]
        for now_dt, expected_key in cases:
            index = self.engine.resolve_slot_index_for_time(run, now_dt)
            assert run.slots[index].slot_key == expected_key, (now_dt, expected_key)

    async def test_force_set_current_state(self):
        from datetime import datetime

        now_dt = datetime(2024, 1, 2, 10, 0)
        slot = await self.engine.force_set_current_state(
            "soul-a",
            BotState.BUSY,
            new_title="苦逼赶稿",
            armor=80,
            now_dt=now_dt,
        )
        assert slot.state == BotState.BUSY
        assert slot.title == "苦逼赶稿"
        assert slot.armor == 80
        assert slot.mutated is False
        current = await self.engine.get_current_slot("soul-a", now_dt=now_dt)
        assert current.state == BotState.BUSY


# ===========================================================================
# 2. Three-state x three-tier gate matrix
# ===========================================================================
class TestStateGateMatrix(unittest.TestCase):
    def test_full_message_gate_matrix(self):
        expected = {
            (BotState.RESTING, UserTier.L1_NORMAL): False,
            (BotState.RESTING, UserTier.L2_IMPORTANT): False,
            (BotState.RESTING, UserTier.L3_CORE): False,
            (BotState.BUSY, UserTier.L1_NORMAL): False,
            (BotState.BUSY, UserTier.L2_IMPORTANT): True,
            (BotState.BUSY, UserTier.L3_CORE): True,
            (BotState.IDLE, UserTier.L1_NORMAL): True,
            (BotState.IDLE, UserTier.L2_IMPORTANT): True,
            (BotState.IDLE, UserTier.L3_CORE): True,
        }
        for (state, tier), allowed in expected.items():
            for is_private in (True, False):
                decision = StateController.check_message_gate(state, tier, is_private)
                assert decision.allowed is allowed, (state, tier)
                assert decision.state == state
                assert decision.user_tier == tier
                assert isinstance(decision.reason, str) and decision.reason
                if not allowed:
                    assert decision.requires_atmosphere_check is False
                else:
                    assert decision.requires_atmosphere_check is (not is_private)

    def test_resting_blocks_everything(self):
        for tier in UserTier:
            for is_private in (True, False):
                decision = StateController.check_message_gate(
                    BotState.RESTING, tier, is_private
                )
                assert decision.allowed is False
                assert decision.requires_atmosphere_check is False

    def test_surf_and_proactive_chat_only_in_idle(self):
        for state in BotState:
            assert StateController.can_surf_video(state) is (state == BotState.IDLE)
            assert StateController.can_initiate_proactive_chat(state) is (
                state == BotState.IDLE
            )


# ===========================================================================
# 3. BoundedScheduleMutator triple guardrail
# ===========================================================================
class TestBoundedScheduleMutator(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="onlyyou-tests-")
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self._temp_db = mods.db.DatabaseManager(os.path.join(self.tmpdir, "only.you.db"))
        await self._temp_db.initialize()
        self.db = self._temp_db
        self.config = make_plugin_config()
        self.engine = RogueliteDeckEngine(self.db, self.config)
        self.model_hub = StubModelHub(json_result=None)
        self.mutator = BoundedScheduleMutator(self.engine, self.model_hub, self.config)
        self.soul = make_soul("soul-a")

    async def asyncTearDown(self):
        await self._temp_db.close()

    async def test_gate1_impact_must_strictly_exceed_armor(self):
        run = make_run()
        slot = make_slot(state=BotState.BUSY, armor=60)
        assert await self.mutator.can_mutate(run, slot, 61) is True
        assert await self.mutator.can_mutate(run, slot, 60) is False
        assert await self.mutator.can_mutate(run, slot, 10) is False

    async def test_gate2_daily_quota(self):
        slot = make_slot(state=BotState.BUSY, armor=10)
        run = make_run(mutations_used=0)
        assert await self.mutator.can_mutate(run, slot, 100) is True
        run = make_run(mutations_used=1)
        assert await self.mutator.can_mutate(run, slot, 100) is True
        run = make_run(mutations_used=2)  # max_mutations_per_day default is 2
        assert await self.mutator.can_mutate(run, slot, 100) is False

    async def test_gate3_cooldown_lock(self):
        slot = make_slot(state=BotState.BUSY, armor=10)
        run = make_run(last_mutation_ts=time.time())
        assert await self.mutator.can_mutate(run, slot, 100) is False
        run = make_run(last_mutation_ts=time.time() - 121 * 60)
        assert await self.mutator.can_mutate(run, slot, 100) is True

    async def test_custom_quota_and_cooldown_config(self):
        config = make_plugin_config(
            roguelite_schedule_config={
                "enable_roguelite_schedule": True,
                "daily_encounter_count": 2,
                "max_mutations_per_day": 1,
                "mutation_cooldown_minutes": 0,
            }
        )
        mutator = BoundedScheduleMutator(self.engine, self.model_hub, config)
        slot = make_slot(state=BotState.BUSY, armor=10)
        run = make_run(mutations_used=1, last_mutation_ts=time.time())
        assert await mutator.can_mutate(run, slot, 100) is False  # quota exhausted
        run = make_run(mutations_used=0, last_mutation_ts=time.time())
        assert await mutator.can_mutate(run, slot, 100) is True  # no cooldown lock

    async def test_compute_message_impact_scenarios(self):
        busy = make_slot(state=BotState.BUSY, armor=60)
        next_idle = make_slot(state=BotState.IDLE)
        resting = make_slot(state=BotState.RESTING, armor=65)
        idle = make_slot(state=BotState.IDLE)
        next_resting = make_slot(state=BotState.RESTING)

        # RESTING can only be woken by an L3 private urgent call
        assert (
            self.mutator.compute_message_impact(
                resting, next_idle, UserTier.L3_CORE, "u3", "醒醒！！快看，出大事了", True, False
            )
            == 88
        )
        assert (
            self.mutator.compute_message_impact(
                resting, next_idle, UserTier.L2_IMPORTANT, "u2", "醒醒", True, False
            )
            == 0
        )
        assert (
            self.mutator.compute_message_impact(
                resting, next_idle, UserTier.L3_CORE, "u3", "醒醒", False, False
            )
            == 0
        )
        # BUSY
        assert (
            self.mutator.compute_message_impact(
                busy, next_idle, UserTier.L1_NORMAL, "u1", "在吗", False, False
            )
            == 10
        )
        assert (
            self.mutator.compute_message_impact(
                busy, next_idle, UserTier.L3_CORE, "u3", "急", True, False
            )
            == 90  # base 78 + urgent keyword bonus 12
        )
        # IDLE -> next RESTING: an ordinary chat cannot delay the schedule
        assert (
            self.mutator.compute_message_impact(
                idle, next_resting, UserTier.L3_CORE, "u3", "再聊会儿嘛", False, False
            )
            == 0
        )
        # IDLE -> next RESTING with an urgent L3 message -> delay next slot
        assert (
            self.mutator.compute_message_impact(
                idle, next_resting, UserTier.L3_CORE, "u3", "救命啊快看", False, False
            )
            == 75
        )

    async def test_on_message_input_mutates_then_hits_quota_and_cooldown(self):
        run = make_run()
        current_slot = make_slot(state=BotState.RESTING, armor=60, title="呼呼大睡")
        # next slot is RESTING so that the post-mutation IDLE slot can still be
        # "delayed" by an urgent L3 message (impact 75 > armor 60).
        next_slot = make_slot(slot_key="night", state=BotState.RESTING, armor=70)

        async def fake_current_and_next(**kwargs):
            return run, current_slot, next_slot

        self.engine.get_current_and_next_slot = fake_current_and_next
        event = FakeEvent(message_str="醒醒！！快看，出大事了救命！！", umo="umo-1")
        user = make_user("u3", "核心用户", tier=UserTier.L3_CORE)

        mutated = await self.mutator.on_message_input(
            soul=self.soul,
            event=event,
            user=user,
            is_private_chat=True,
        )
        assert mutated is True
        assert current_slot.state == BotState.IDLE
        assert current_slot.mutated is True
        assert current_slot.mutation_monologue
        assert run.mutations_used == 1
        assert run.last_mutation_ts > 0
        # the deterministic fallback was used (StubModelHub returned None) and the
        # decision prompt carried the USER_MESSAGE input type
        assert self.model_hub.calls
        assert "USER_MESSAGE" in self.model_hub.calls[0]["prompt"]

        # cooldown lock blocks the immediate second mutation
        assert (
            await self.mutator.on_message_input(
                soul=self.soul, event=event, user=user, is_private_chat=True
            )
            is False
        )
        # cooldown passed -> second mutation allowed, then the quota blocks the third
        run.last_mutation_ts = 0.0
        assert (
            await self.mutator.on_message_input(
                soul=self.soul, event=event, user=user, is_private_chat=True
            )
            is True
        )
        assert run.mutations_used == 2
        run.last_mutation_ts = 0.0
        assert (
            await self.mutator.on_message_input(
                soul=self.soul, event=event, user=user, is_private_chat=True
            )
            is False
        )
        assert run.mutations_used == 2


# ===========================================================================
# 4. MemoryManager.run_forgetting_gc
# ===========================================================================
class TestMemoryForgettingGC(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="onlyyou-tests-")
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self._temp_db = mods.db.DatabaseManager(os.path.join(self.tmpdir, "only.you.db"))
        await self._temp_db.initialize()
        self.db = self._temp_db
        self.config = make_plugin_config()
        self.manager = MemoryManager(self.db, StubModelHub(), self.config)
        self.now = int(time.time())

    async def asyncTearDown(self):
        await self._temp_db.close()

    def test_importance_to_tier_grading(self):
        assert MemoryManager.resolve_tier_by_importance(9, 3) == MemoryTier.CORE
        assert MemoryManager.resolve_tier_by_importance(8, 2) == MemoryTier.CORE
        assert MemoryManager.resolve_tier_by_importance(7, 1) == MemoryTier.RECENT
        assert MemoryManager.resolve_tier_by_importance(5, 1) == MemoryTier.RECENT
        assert MemoryManager.resolve_tier_by_importance(4, 1) == MemoryTier.EPHEMERAL
        assert MemoryManager.resolve_tier_by_importance(1, 1) == MemoryTier.EPHEMERAL

    async def test_l1_user_cannot_create_tier3(self):
        memory_id = await self.manager.add_memory(
            soul_id="s1",
            content="很高的重要度",
            importance=10,
            user_tier=1,
            memory_tier=3,
        )
        row = await self.db.fetchone("SELECT memory_tier FROM memories WHERE id = ?", (memory_id,))
        assert row["memory_tier"] == MemoryTier.RECENT

    async def test_tier1_expired_is_deleted_fresh_is_kept(self):
        expired = await self.manager.add_memory(
            soul_id="s1",
            content="两天半前的瞬时记忆",
            importance=2,
            created_at=self.now - 200000,
        )
        fresh = await self.manager.add_memory(
            soul_id="s1",
            content="刚刚的瞬时记忆",
            importance=2,
            created_at=self.now - 100,
        )
        stats = await self.manager.run_forgetting_gc(now_override=self.now)
        assert stats["deleted_tier1"] == 1
        assert await self.db.fetchone("SELECT id FROM memories WHERE id = ?", (expired,)) is None
        assert await self.db.fetchone("SELECT id FROM memories WHERE id = ?", (fresh,)) is not None

    async def test_tier2_daily_vitality_decay(self):
        memory_id = await self.manager.add_memory(
            soul_id="s1",
            content="近期情节记忆",
            importance=5,
            user_tier=1,
            memory_tier=MemoryTier.RECENT,
            vitality=100.0,
        )
        stats = await self.manager.run_forgetting_gc(now_override=self.now)
        assert stats["decayed_tier2"] == 1
        # decay = 35 / (importance 5 * tier_factor 0.6 * reinforce 1.0) = 11.6667
        row = await self.db.fetchone("SELECT vitality FROM memories WHERE id = ?", (memory_id,))
        assert abs(row["vitality"] - (100.0 - 35.0 / (5 * 0.6))) < 0.02

    async def test_tier2_zero_vitality_is_deleted(self):
        memory_id = await self.manager.add_memory(
            soul_id="s1",
            content="快要遗忘的记忆",
            importance=10,
            user_tier=1,
            memory_tier=MemoryTier.RECENT,
            vitality=1.0,
        )
        stats = await self.manager.run_forgetting_gc(now_override=self.now)
        assert stats["deleted_tier2"] == 1
        assert await self.db.fetchone("SELECT id FROM memories WHERE id = ?", (memory_id,)) is None

    async def test_tier2_promoted_to_tier3_after_frequent_access(self):
        memory_id = await self.manager.add_memory(
            soul_id="s1",
            content="被反复提起的记忆",
            importance=6,
            user_tier=2,
            memory_tier=MemoryTier.RECENT,
        )
        await self.db.execute(
            "UPDATE memories SET access_count = 4 WHERE id = ?", (memory_id,)
        )
        stats = await self.manager.run_forgetting_gc(now_override=self.now)
        assert stats["promoted_to_tier3"] == 1
        row = await self.db.fetchone(
            "SELECT memory_tier, vitality FROM memories WHERE id = ?", (memory_id,)
        )
        assert row["memory_tier"] == MemoryTier.CORE
        assert row["vitality"] == 100.0

    async def test_tier3_is_immune_to_gc(self):
        memory_id = await self.manager.add_memory(
            soul_id="s1",
            content="核心永久记忆",
            importance=9,
            user_tier=3,
            memory_tier=MemoryTier.CORE,
        )
        stats = await self.manager.run_forgetting_gc(now_override=self.now)
        assert stats["deleted_tier1"] == 0
        assert stats["deleted_tier2"] == 0
        assert stats["promoted_to_tier3"] == 0
        row = await self.db.fetchone(
            "SELECT memory_tier, vitality FROM memories WHERE id = ?", (memory_id,)
        )
        assert row["memory_tier"] == MemoryTier.CORE
        assert row["vitality"] == 100.0

    async def test_reinforce_memories_promotes_and_caps_vitality(self):
        memory_id = await self.manager.add_memory(
            soul_id="s1",
            content="高频召回记忆",
            importance=6,
            user_tier=2,
            memory_tier=MemoryTier.RECENT,
            vitality=100.0,
        )
        await self.db.execute(
            "UPDATE memories SET access_count = 3 WHERE id = ?", (memory_id,)
        )
        await self.manager.reinforce_memories([memory_id])
        row = await self.db.fetchone(
            "SELECT memory_tier, access_count, vitality FROM memories WHERE id = ?",
            (memory_id,),
        )
        assert row["access_count"] == 4
        assert row["memory_tier"] == MemoryTier.CORE
        assert row["vitality"] == 100.0


# ===========================================================================
# 5. ContextBudgetManager
# ===========================================================================
class ContextBudgetTestBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="onlyyou-tests-")
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self._temp_db = mods.db.DatabaseManager(os.path.join(self.tmpdir, "only.you.db"))
        await self._temp_db.initialize()
        self.db = self._temp_db
        self.soul = make_soul("soul-a")
        self.user = make_user("user-1", "阿一")

    async def asyncTearDown(self):
        await self._temp_db.close()

    def build_budget(self, config, memories=None):
        self.memory_manager = MemoryManager(self.db, StubModelHub(), config)
        self.session_buffer = SessionBuffer(config)
        self.budget = ContextBudgetManager(
            memory_manager=self.memory_manager,
            session_buffer=self.session_buffer,
            plugin_config=config,
            soul_manager=FakeSoulManager([]),
        )
        return self.budget, self.memory_manager, self.session_buffer

    async def apply(self, req, event, slot, run=None):
        return await self.budget.apply_context_and_memory_budget(
            event=event,
            req=req,
            soul=self.soul,
            user=self.user,
            run=run or make_run(),
            current_slot=slot,
        )


class TestContextBudget(ContextBudgetTestBase):
    async def test_contexts_are_cleared_and_window_injected_without_duplicate(self):
        config = make_plugin_config(
            memory_and_context_config={
                "enable_tiered_memory": True,
                "sliding_window_size": 5,
                "extract_every_n_messages": 5,
                "max_injected_memories": 6,
                "max_memory_chars_budget": 400,
            }
        )
        budget, _manager, session_buffer = self.build_budget(config)
        session_buffer.record_user_message("umo-1", self.user, "第一句历史消息")
        session_buffer.record_user_message("umo-1", self.user, "当前问题")

        req = FakeRequest(
            prompt="当前问题",
            system_prompt="BASE",
            contexts=["native-old-1", "native-old-2", "native-old-3"],
        )
        result = await self.apply(
            req, FakeEvent(message_str="当前问题", umo="umo-1"), make_slot(state=BotState.IDLE)
        )
        assert "native-old-1" not in req.contexts
        assert len(req.contexts) == 1
        assert "第一句历史消息" in req.contexts[0]["content"]
        assert "当前问题" not in req.contexts[0]["content"]
        assert result["window_count"] == 1

    async def test_window_is_capped_at_sliding_window_size(self):
        config = make_plugin_config(
            memory_and_context_config={
                "enable_tiered_memory": True,
                "sliding_window_size": 3,
                "extract_every_n_messages": 10,
                "max_injected_memories": 6,
                "max_memory_chars_budget": 400,
            }
        )
        budget, _manager, session_buffer = self.build_budget(config)
        for index in range(6):
            session_buffer.record_user_message("umo-1", self.user, "历史消息%d" % index)
        req = FakeRequest(prompt="新的问题", system_prompt="BASE", contexts=["old"])
        result = await self.apply(
            req, FakeEvent(message_str="新的问题", umo="umo-1"), make_slot(state=BotState.BUSY)
        )
        assert len(req.contexts) == 3
        assert result["window_count"] == 3
        assert "历史消息5" in req.contexts[-1]["content"]

    async def test_max_injected_memories_budget(self):
        config = make_plugin_config(
            memory_and_context_config={
                "enable_tiered_memory": True,
                "sliding_window_size": 5,
                "extract_every_n_messages": 5,
                "max_injected_memories": 2,
                "max_memory_chars_budget": 400,
            }
        )
        budget, manager, _buffer = self.build_budget(config)
        for index in range(6):
            await manager.add_memory(
                soul_id=self.soul.soul_id,
                content="记忆内容%d" % index,
                keywords="记忆",
                importance=9 - index,
            )
        req = FakeRequest(prompt="还记得吗", system_prompt="BASE")
        result = await self.apply(
            req, FakeEvent(message_str="还记得吗", umo="umo-1"), make_slot(state=BotState.IDLE)
        )
        assert len(result["recalled_ids"]) == 2
        assert req.system_prompt.count("- [") == 2
        assert "脑海中的相关记忆" in req.system_prompt

    async def test_max_memory_chars_budget_is_hard(self):
        config = make_plugin_config(
            memory_and_context_config={
                "enable_tiered_memory": True,
                "sliding_window_size": 5,
                "extract_every_n_messages": 5,
                "max_injected_memories": 6,
                "max_memory_chars_budget": 50,  # property floor is max(50, cfg)
            }
        )
        budget, manager, _buffer = self.build_budget(config)
        long_content = "很长" * 20  # 40 chars, still under the 60-char storage cap
        long_content = long_content + "很长的记忆内容超过五十字符预算"  # 60 chars
        await manager.add_memory(
            soul_id=self.soul.soul_id,
            content=long_content,
            keywords="长记忆",
            importance=10,
        )
        req = FakeRequest(prompt="长记忆", system_prompt="BASE")
        result = await self.apply(
            req, FakeEvent(message_str="长记忆", umo="umo-1"), make_slot(state=BotState.IDLE)
        )
        assert result["recalled_ids"] == []
        assert result["injected_chars"] == 0
        assert "脑海中的相关记忆" not in req.system_prompt

    async def test_memory_chars_budget_stops_when_next_does_not_fit(self):
        config = make_plugin_config(
            memory_and_context_config={
                "enable_tiered_memory": True,
                "sliding_window_size": 5,
                "extract_every_n_messages": 5,
                "max_injected_memories": 6,
                "max_memory_chars_budget": 50,  # property floor is max(50, cfg)
            }
        )
        budget, manager, _buffer = self.build_budget(config)
        for index in range(4):
            await manager.add_memory(
                soul_id=self.soul.soul_id,
                content="一二三四五六七八九十一二三四五六七八九十",  # 20 chars each
                keywords="测试",
                importance=8 - index,
            )
        req = FakeRequest(prompt="测试", system_prompt="BASE")
        result = await self.apply(
            req, FakeEvent(message_str="测试", umo="umo-1"), make_slot(state=BotState.IDLE)
        )
        assert len(result["recalled_ids"]) == 2
        assert result["injected_chars"] == 40
        assert result["injected_chars"] <= budget.max_memory_chars_budget

    async def test_keyword_match_boosts_ranking(self):
        config = make_plugin_config(
            memory_and_context_config={
                "enable_tiered_memory": True,
                "sliding_window_size": 5,
                "extract_every_n_messages": 5,
                "max_injected_memories": 6,
                "max_memory_chars_budget": 400,
            }
        )
        budget, manager, _buffer = self.build_budget(config)
        matched = await manager.add_memory(
            soul_id=self.soul.soul_id,
            content="用户喜欢猫",
            keywords="猫,宠物",
            importance=5,
        )
        await manager.add_memory(
            soul_id=self.soul.soul_id,
            content="完全不相关的内容",
            keywords="天气",
            importance=10,
        )
        req = FakeRequest(prompt="我家猫今天又拆家了", system_prompt="BASE")
        result = await self.apply(
            req,
            FakeEvent(message_str="我家猫今天又拆家了", umo="umo-1"),
            make_slot(state=BotState.IDLE),
        )
        assert result["recalled_ids"][0] == matched

    async def test_state_instructions_injected(self):
        config = make_plugin_config()
        budget, _manager, _buffer = self.build_budget(config)
        req = FakeRequest(prompt="在吗", system_prompt="BASE", contexts=["old"])
        await self.apply(
            req, FakeEvent(message_str="在吗", umo="umo-1"), make_slot(state=BotState.BUSY)
        )
        assert "繁忙中 (BUSY)" in req.system_prompt
        assert "当前三态控制器" in req.system_prompt

    async def test_mutated_slot_injects_monologue(self):
        config = make_plugin_config()
        budget, _manager, _buffer = self.build_budget(config)
        slot = make_slot(
            state=BotState.IDLE,
            mutated=True,
            mutation_monologue="本来在忙的，算了先摸鱼",
        )
        req = FakeRequest(prompt="在吗", system_prompt="BASE")
        await self.apply(req, FakeEvent(message_str="在吗", umo="umo-1"), slot)
        assert "本来在忙的，算了先摸鱼" in req.system_prompt
        assert "刚刚触发了日程变卦" in req.system_prompt


# ===========================================================================
# 6. Project-level sanity: offline import + compileall
# ===========================================================================
class TestProjectSanity(unittest.TestCase):
    def test_main_module_imports_offline_without_instantiation(self):
        import OnlyYou.main as main_module

        assert hasattr(main_module, "OnlyYouPlugin")
        for method in (
            "on_any_message",
            "on_llm_request_hook",
            "on_llm_response_hook",
            "on_decorating_result_hook",
            "terminate",
            "initialize",
        ):
            assert hasattr(main_module.OnlyYouPlugin, method), method
        source = inspect.getsource(main_module.OnlyYouPlugin.on_any_message)
        assert "selected_provider" in source
        assert "get_main_provider_id" in source

    def test_conf_schema_is_valid_and_has_no_model_config(self):
        path = os.path.join(PROJECT_ROOT, "_conf_schema.json")
        with open(path, "r", encoding="utf-8") as handle:
            schema = json.load(handle)
        assert isinstance(schema, dict) and schema
        assert "model_hub_config" not in schema
        for removed in (
            "small_llm_provider_id",
            "embedding_provider_id",
            "rerank_provider_id",
        ):
            assert removed not in json.dumps(schema)

    def test_compileall_passes_without_repo_artifacts(self):
        pycache_prefix = tempfile.mkdtemp(prefix="onlyyou-pyc-")
        self.addCleanup(shutil.rmtree, pycache_prefix, ignore_errors=True)
        env = dict(os.environ, PYTHONPYCACHEPREFIX=pycache_prefix)
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "compileall",
                "-q",
                "core",
                "storage",
                "surfing",
                "prompts",
                "main.py",
            ],
            cwd=PROJECT_ROOT,
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr

    def test_no_sqlite_database_written_into_repository(self):
        # The suite must never write a database into the repository (OnlyYouPlugin
        # is never instantiated; all DBs live under tempfile.mkdtemp()).
        repo_db_files = []
        for root, _dirs, files in os.walk(PROJECT_ROOT):
            if os.sep + "tests" in root or os.sep + ".git" in root or os.sep + "data" in root:
                continue
            for name in files:
                if name.endswith((".db", ".db-wal", ".db-shm", ".sqlite")):
                    repo_db_files.append(os.path.join(root, name))
        assert repo_db_files == [], repo_db_files


if __name__ == "__main__":
    unittest.main()
