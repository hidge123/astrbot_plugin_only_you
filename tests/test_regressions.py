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
import random
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime
from types import MethodType, SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _helpers import (  # noqa: E402
    FakeEvent,
    FakeRequest,
    FakeSoulManager,
    StubModelHub,
    card_with,
    load_plugin_modules,
    make_default_slots,
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
ActionType = mods.state_machine.ActionType
SlotInstance = mods.roguelite_deck.SlotInstance
StateEventCard = mods.roguelite_deck.StateEventCard
DailyScheduleRun = mods.roguelite_deck.DailyScheduleRun
normalize_chronotype = mods.roguelite_deck.normalize_chronotype

_PLUGIN_METHOD_NAMES = (
    "settle_busy_block",
    "_settle_finished_busy_blocks",
    "_run_idle_roam_action",
    "_idle_roam_cooldown_seconds",
    "_roguelite_setting",
    "_group_lurk_cooldown_seconds",
    "_private_catchup_cooldown_seconds",
    "_unread_private_reply_enabled",
    "_pick_group_unread_candidate",
    "_decide_group_unread_reply",
    "_catch_up_private_unread",
    "_pick_memory_muse_material",
    "_resolve_idle_roam_target",
    "_extract_idle_roam_message",
    "_latest_l3_umo",
    "_fallback_busy_completion",
    "_fallback_private_catchup",
    "_maybe_synthesize_proactive",
    "_render_soul_summary",
    "_is_non_chat_platform_event",
    "on_any_message",
)


def bind_plugin_methods(fake):
    """Bind the *real* ``OnlyYouPlugin`` methods onto a lightweight fake ``self``.

    ``OnlyYouPlugin`` itself is never instantiated by the test suite (its StarTools
    stub would create an SQLite database inside the repository), so unit tests call
    the unbound methods against a ``SimpleNamespace`` carrying the real components.
    """
    from OnlyYou.main import OnlyYouPlugin

    for name in _PLUGIN_METHOD_NAMES:
        raw = inspect.getattr_static(OnlyYouPlugin, name)
        if isinstance(raw, staticmethod):
            setattr(fake, name, raw.__func__)
        else:
            setattr(fake, name, MethodType(raw, fake))
    return fake


class RecordingProactiveBridge:
    """Minimal ``PlatformBridge`` stand-in recording proactive sends."""

    def __init__(self):
        self.sent = []
        self.ok = True
        self.decisions = []

    def apply_reply_decision(self, event, should_reply=False):
        self.decisions.append(bool(should_reply))

    async def send_proactive_message(
        self, umo, text, session_buffer=None, soul_name="Bot", audio_path=None
    ):
        self.sent.append((umo, text, audio_path))
        return self.ok


class SequencedStubModelHub(StubModelHub):
    """StubModelHub returning queued JSON/text results (decision then generation)."""

    def __init__(self, json_results=None, text_results=None):
        super().__init__()
        self.json_results = list(json_results or [])
        self.text_results = list(text_results or [])

    async def call_llm_json(
        self,
        prompt="",
        role="decision",
        umo=None,
        system_prompt="",
        image_urls=None,
        default=None,
    ):
        self.calls.append(
            {"prompt": prompt, "role": role, "umo": umo, "default": default}
        )
        if self.json_results:
            return self.json_results.pop(0)
        return default

    async def call_llm_text(
        self,
        prompt="",
        role="decision",
        umo=None,
        system_prompt="",
        image_urls=None,
    ):
        self.calls.append({"prompt": prompt, "role": role, "umo": umo})
        if self.text_results:
            return self.text_results.pop(0)
        return self.text_result


class _StubAtmosphereForGate:
    """AtmosphereReader stand-in for the three-state gate unit test."""

    def __init__(self, session_buffer):
        self.session_buffer = session_buffer

    def record_message(self, event, user, on_overflow_callback=None):
        message = self.session_buffer.record_user_message(
            event.unified_msg_origin,
            user,
            event.message_str,
            on_overflow_callback=on_overflow_callback,
        )
        return "", False, message

    @staticmethod
    def is_private_event(event):
        return True

    async def evaluate_should_reply(self, **kwargs):
        return False


# ===========================================================================
# 1. Roguelite 24h dynamic action-card timeline
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

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def assert_timeline(run, engine, chronotype=None):
        slots = run.slots
        assert engine.is_valid_timeline(run)
        assert 7 <= len(slots) <= 11, len(slots)
        assert slots[0].start_hour == 0.0
        assert slots[-1].end_hour == 24.0
        prev = 0.0
        for slot in slots:
            assert abs(slot.start_hour - prev) < 1e-9, (
                slot.slot_key,
                slot.start_hour,
                prev,
            )
            duration = slot.end_hour - slot.start_hour
            assert duration >= 0.5, duration
            assert abs(duration * 2 - round(duration * 2)) < 1e-9, duration
            assert isinstance(slot.state, BotState)
            assert isinstance(slot.action_type, ActionType)
            assert slot.action_type.bound_state == slot.state
            assert slot.title and slot.label
            assert 0 <= slot.armor <= 100
            prev = slot.end_hour
        assert run.encounters == []
        if chronotype is not None:
            assert run.chronotype == chronotype

    async def _build(self, soul_id, chronotype="normal", seed=0):
        random.seed(seed)
        return await self.engine.get_or_build_today_run(
            soul_id, chronotype=chronotype, force_reroll=True
        )

    # -- timeline invariants --------------------------------------------
    async def test_dynamic_timeline_covers_full_day_for_all_chronotypes(self):
        for chronotype in ("early", "normal", "night_owl"):
            for seed in range(5):
                run = await self._build(
                    f"{chronotype}-{seed}", chronotype=chronotype, seed=seed
                )
                self.assert_timeline(run, self.engine, chronotype=chronotype)

    async def test_same_day_run_is_stable_and_persisted(self):
        first = await self._build("soul-stable", seed=3)
        second = await self.engine.get_or_build_today_run("soul-stable")
        assert second is first  # in-memory cache

        # a brand-new engine (empty cache) must load the persisted run from SQLite
        other_engine = RogueliteDeckEngine(self.db, self.config)
        reloaded = await other_engine.get_or_build_today_run("soul-stable")
        assert reloaded.to_dict() == first.to_dict()
        self.assert_timeline(reloaded, other_engine)

        # a different soul gets an independent run
        other_soul_run = await other_engine.get_or_build_today_run(
            "soul-b", chronotype="early"
        )
        assert other_soul_run.soul_id == "soul-b"
        self.assert_timeline(other_soul_run, other_engine)

    async def test_reroll_regenerates_a_valid_timeline(self):
        original = await self._build("soul-reroll", seed=11)
        rerolled = original
        changed = False
        for _ in range(4):
            rerolled = await self.engine.get_or_build_today_run(
                "soul-reroll", force_reroll=True
            )
            self.assert_timeline(rerolled, self.engine)
            if rerolled.to_dict() != original.to_dict():
                changed = True
                break
        assert changed, "reroll should (almost surely) produce a different schedule"
        fresh_engine = RogueliteDeckEngine(self.db, self.config)
        persisted = await fresh_engine.get_or_build_today_run("soul-reroll")
        assert persisted.to_dict() == rerolled.to_dict()

    async def test_custom_card_pool_is_used_and_completed_to_four_actions(self):
        custom = [
            StateEventCard(
                card_id="custom_busy",
                title="在丹房炼制回春丹",
                state=BotState.BUSY,
                action_type=ActionType.BUSY_TASK,
                min_hours=1.0,
                max_hours=2.0,
                armor=60,
            ),
            StateEventCard(
                card_id="custom_surf",
                title="瘫在懒人沙发上刷B站",
                state=BotState.IDLE,
                action_type=ActionType.SURF,
                min_hours=0.5,
                max_hours=1.5,
                armor=30,
            ),
        ]
        pool = self.engine._normalize_pool(custom)
        assert {card.action for card in pool} == set(ActionType)
        assert any(card.card_id == "custom_busy" for card in pool)
        # The Soul's own cards must be drawable while all four types stay covered.
        drawn_custom = False
        for seed in range(10):
            random.seed(seed)
            run = await self.engine.get_or_build_today_run(
                f"soul-custom-{seed}",
                custom_cards=custom,
                chronotype="night_owl",
                force_reroll=True,
            )
            self.assert_timeline(run, self.engine)
            if any(slot.card_id.startswith("custom_") for slot in run.slots):
                drawn_custom = True
        assert drawn_custom

    async def test_chronotype_weight_curves_differ(self):
        early_1am = self.engine.get_chronotype_weights("early", 1.0)
        owl_1am = self.engine.get_chronotype_weights("night_owl", 1.0)
        early_9am = self.engine.get_chronotype_weights("early", 9.0)
        owl_9am = self.engine.get_chronotype_weights("night_owl", 9.0)
        # 01:00 — early bird is fast asleep, night owl is wide awake.
        assert early_1am[ActionType.SLEEP] > 3.0
        assert owl_1am[ActionType.SLEEP] < 1.0
        assert owl_1am[ActionType.SURF] > 1.0
        assert early_1am[ActionType.BUSY_TASK] < 1.0
        # 09:00 — reversed: night owl is still asleep, early bird already busy.
        assert owl_9am[ActionType.SLEEP] > 3.0
        assert early_9am[ActionType.SLEEP] < 1.0
        assert early_9am[ActionType.BUSY_TASK] > 1.0
        for action in ActionType:
            for weights in (early_1am, owl_1am, early_9am, owl_9am):
                assert weights[action] > 0

    def test_legacy_card_dict_migrates_action_type_and_hours(self):
        surf = StateEventCard.from_dict(
            {"card_id": "a", "title": "躺沙发上刷B站", "state": "IDLE", "armor": 20}
        )
        assert surf.action_type == ActionType.SURF
        assert surf.state == BotState.IDLE
        assert (surf.min_hours, surf.max_hours) == (0.5, 2.0)

        sleep = StateEventCard.from_dict(
            {"card_id": "b", "title": "关灯钻进被窝呼呼大睡", "state": "RESTING"}
        )
        assert sleep.action_type == ActionType.SLEEP
        assert (sleep.min_hours, sleep.max_hours) == (4.0, 7.5)

        nap = StateEventCard.from_dict(
            {"card_id": "c", "title": "戴眼罩午休眯一会儿", "state": "RESTING"}
        )
        assert nap.action_type == ActionType.SLEEP
        assert (nap.min_hours, nap.max_hours) == (1.0, 2.0)

        busy = StateEventCard.from_dict(
            {"card_id": "d", "title": "沉浸式处理手头正事", "state": "BUSY"}
        )
        assert busy.action_type == ActionType.BUSY_TASK
        assert (busy.min_hours, busy.max_hours) == (1.0, 3.0)

        roam = StateEventCard.from_dict(
            {"card_id": "e", "title": "趴在窗边发呆", "state": "IDLE"}
        )
        assert roam.action_type == ActionType.IDLE_ROAM
        assert (roam.min_hours, roam.max_hours) == (0.5, 2.0)

        # explicit action_type wins over the (legacy) state field
        explicit = StateEventCard.from_dict(
            {
                "card_id": "f",
                "title": "冲浪刷片",
                "state": "BUSY",
                "action_type": "SURF",
                "min_hours": 0.2,
                "max_hours": 12.0,
            }
        )
        assert explicit.action_type == ActionType.SURF
        assert explicit.state == BotState.IDLE
        assert explicit.min_hours == 0.5
        assert explicit.max_hours == 8.0

    async def test_legacy_six_slot_run_is_redrawn(self):
        date_str = self.engine.today_str()
        legacy = {
            "soul_id": "soul-legacy",
            "date_str": date_str,
            "vigor_id": "vigor_normal",
            "vigor_title": "普通一天",
            "vigor_desc": "普通一天",
            "slots": [
                {
                    "slot_key": "morning",
                    "label": "清晨早起",
                    "start_hour": 7,
                    "end_hour": 9,
                    "title": "早起",
                    "state": "IDLE",
                    "armor": 50,
                },
                {
                    "slot_key": "night",
                    "label": "深夜睡眠",
                    "start_hour": 23,
                    "end_hour": 7,
                    "title": "睡觉",
                    "state": "RESTING",
                    "armor": 60,
                },
            ],
        }
        await self.db.execute(
            "INSERT INTO daily_schedules (soul_id, date_str, run_json, updated_at)"
            " VALUES (?, ?, ?, ?)",
            ("soul-legacy", date_str, json.dumps(legacy), 0),
        )
        run = await self.engine.get_or_build_today_run("soul-legacy")
        self.assert_timeline(run, self.engine)

    async def test_resolve_slot_index_for_time(self):
        run = await self._build("soul-resolve", seed=5)
        for index, slot in enumerate(run.slots):
            midpoint = slot.start_hour + (slot.end_hour - slot.start_hour) / 2.0
            hour = int(midpoint)
            minute = int(round((midpoint - hour) * 60))
            now_dt = datetime(2024, 1, 2, hour, minute)
            assert self.engine.resolve_slot_index_for_time(run, now_dt) == index, (
                now_dt,
                slot,
            )
        first = self.engine.resolve_slot_index_for_time(
            run, datetime(2024, 1, 2, 0, 0)
        )
        assert run.slots[first].start_hour == 0.0
        last = self.engine.resolve_slot_index_for_time(
            run, datetime(2024, 1, 2, 23, 59)
        )
        assert run.slots[last].end_hour == 24.0

    async def test_force_set_current_state(self):
        now_dt = datetime(2024, 1, 2, 10, 0)
        slot = await self.engine.force_set_current_state(
            "soul-force",
            BotState.BUSY,
            new_title="苦逼赶稿",
            armor=80,
            now_dt=now_dt,
        )
        assert slot.state == BotState.BUSY
        assert slot.action_type == ActionType.BUSY_TASK
        assert slot.title == "苦逼赶稿"
        # v2.6: the ``armor=`` argument is deprecated/ignored — every block is
        # pinned to the fixed tier bound to its action type (BUSY_TASK=65).
        assert slot.armor == ActionType.BUSY_TASK.fixed_armor == 65
        assert slot.mutated is False
        current = await self.engine.get_current_slot("soul-force", now_dt=now_dt)
        assert current.state == BotState.BUSY

        surf = await self.engine.force_set_current_state(
            "soul-force", None, action_type=ActionType.SURF, now_dt=now_dt
        )
        assert surf.action_type == ActionType.SURF
        assert surf.state == BotState.IDLE
        assert surf.armor == ActionType.SURF.fixed_armor == 40
        assert surf.busy_settled is False
        assert surf.completion_result == ""

        # bare ``state IDLE`` keeps the current SURF action ...
        bare_idle = await self.engine.force_set_current_state(
            "soul-force", BotState.IDLE, now_dt=now_dt
        )
        assert bare_idle.action_type == ActionType.SURF
        assert bare_idle.armor == ActionType.SURF.fixed_armor == 40
        # ... while an explicit non-surf title derives IDLE_ROAM again.
        roam = await self.engine.force_set_current_state(
            "soul-force",
            BotState.IDLE,
            new_title="趴在窗边发呆看云",
            now_dt=now_dt,
        )
        assert roam.action_type == ActionType.IDLE_ROAM
        assert roam.state == BotState.IDLE
        assert roam.armor == ActionType.IDLE_ROAM.fixed_armor == 25
        assert roam.label == ActionType.IDLE_ROAM.label

    def test_progress_and_time_range_formatting(self):
        block = SlotInstance(
            slot_key="block_x",
            start_hour=8.0,
            end_hour=10.0,
            state=BotState.BUSY,
            action_type=ActionType.BUSY_TASK,
            title="炼制回春丹",
        )
        assert block.get_progress_pct(datetime(2024, 1, 1, 8, 0)) == 0
        assert block.get_progress_pct(datetime(2024, 1, 1, 9, 0)) == 50
        assert block.get_progress_pct(datetime(2024, 1, 1, 10, 0)) == 100
        assert block.format_time_range() == "08:00-10:00"

        half = SlotInstance(
            slot_key="block_y",
            start_hour=13.5,
            end_hour=15.0,
            state=BotState.IDLE,
            action_type=ActionType.SURF,
        )
        assert half.format_time_range() == "13:30-15:00"
        late = SlotInstance(
            slot_key="block_z",
            start_hour=23.5,
            end_hour=24.0,
            state=BotState.RESTING,
            action_type=ActionType.SLEEP,
        )
        assert late.format_time_range() == "23:30-24:00"


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
            req,
            FakeEvent(message_str="在吗", umo="umo-1"),
            make_slot(state=BotState.BUSY, action_type=ActionType.BUSY_TASK),
        )
        assert "BUSY_TASK" in req.system_prompt
        assert "当前完成进度" in req.system_prompt
        assert "当前三态控制器" in req.system_prompt

    async def test_idle_instructions_include_settled_busy_results(self):
        config = make_plugin_config()
        budget, _manager, _buffer = self.build_budget(config)
        slots = make_default_slots()
        busy = [s for s in slots if s.action_type == ActionType.BUSY_TASK][0]
        busy.busy_settled = True
        busy.completion_result = "终于把回春丹炼出来了，差点炸炉"
        req = FakeRequest(prompt="在吗", system_prompt="BASE")
        await self.apply(
            req,
            FakeEvent(message_str="在吗", umo="umo-1"),
            make_slot(state=BotState.IDLE, action_type=ActionType.IDLE_ROAM),
            run=make_run(slots=slots),
        )
        assert "空闲中 (" in req.system_prompt
        assert "终于把回春丹炼出来了" in req.system_prompt

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
# 6. BUSY_TASK daily completion settlement + IDLE_ROAM proactive action
# ===========================================================================
class DynamicTimelineTestBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="onlyyou-tests-")
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self._temp_db = mods.db.DatabaseManager(os.path.join(self.tmpdir, "only.you.db"))
        self.db = self._temp_db
        await self.db.initialize()
        self.config = make_plugin_config()

    async def asyncTearDown(self):
        await self._temp_db.close()


class TestBusyTaskSettlement(DynamicTimelineTestBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.engine = RogueliteDeckEngine(self.db, self.config)
        self.memory_manager = MemoryManager(self.db, StubModelHub(), self.config)
        self.soul = make_soul("soul-busy")

    def fake_plugin(self, json_result=None):
        fake = SimpleNamespace(
            model_hub=StubModelHub(json_result=json_result),
            deck_engine=self.engine,
            memory_manager=self.memory_manager,
            _busy_settling=set(),
        )
        return bind_plugin_methods(fake)

    async def _tier1_rows(self):
        return await self.db.fetchall(
            "SELECT content, importance FROM memories"
            " WHERE soul_id = ? AND memory_tier = 1",
            (self.soul.soul_id,),
        )

    async def test_settle_busy_block_writes_tier1_once(self):
        run = make_run(self.soul.soul_id)
        block = next(s for s in run.slots if s.action_type == ActionType.BUSY_TASK)
        fake = self.fake_plugin(
            {
                "completion_memory": "花了两个多小时终于把回春丹炼出来了，最后一炉差点炸炉",
                "keywords": "炼丹,回春丹",
            }
        )
        text = await fake.settle_busy_block(self.soul, run, block, progress_pct=100)
        assert text == "花了两个多小时终于把回春丹炼出来了，最后一炉差点炸炉"
        assert block.busy_settled is True
        assert block.completion_result == text
        rows = await self._tier1_rows()
        assert len(rows) == 1
        assert rows[0]["content"] == text
        assert rows[0]["importance"] == 4

        # second settlement attempt must not duplicate the Tier 1 memory
        again = await fake.settle_busy_block(self.soul, run, block, progress_pct=100)
        assert again == text
        assert len(await self._tier1_rows()) == 1

    async def test_settle_busy_block_falls_back_when_llm_missing(self):
        run = make_run(self.soul.soul_id)
        block = next(s for s in run.slots if s.action_type == ActionType.BUSY_TASK)
        fake = self.fake_plugin(None)
        text = await fake.settle_busy_block(
            self.soul, run, block, progress_pct=40, interrupted_by="阿泽"
        )
        assert text
        assert block.busy_settled is True
        assert block.completion_result == text
        assert len(await self._tier1_rows()) == 1

    async def test_settle_finished_busy_blocks_only_finished_ones(self):
        run = make_run(self.soul.soul_id)
        busies = [s for s in run.slots if s.action_type == ActionType.BUSY_TASK]
        assert len(busies) >= 2
        fake = self.fake_plugin(
            {"completion_memory": "上午的正事顺利收尾，顺手把桌面也收拾了"}
        )
        texts = await fake._settle_finished_busy_blocks(
            self.soul, run, now_dt=datetime(2024, 1, 2, 13, 0)
        )
        assert len(texts) == 1
        assert busies[0].busy_settled is True
        assert busies[1].busy_settled is False
        assert len(await self._tier1_rows()) == 1


class TestUnreadBacklog(unittest.TestCase):
    """The unread backlog is independent of the 15-message sliding window."""

    def _buffer(self, **roguelite_overrides):
        config = make_plugin_config(
            roguelite_schedule_config={
                "enable_roguelite_schedule": True,
                "max_mutations_per_day": 2,
                "mutation_cooldown_minutes": 120,
                "allow_l3_wake_from_resting": True,
                **roguelite_overrides,
            }
        )
        return SessionBuffer(config)

    def test_record_returns_sequential_messages(self):
        buffer = self._buffer()
        user = make_user("u-1", "阿一")
        first = buffer.record_user_message("umo-1", user, "第一句")
        second = buffer.record_user_message("umo-1", user, "第二句")
        assert first is not None and second is not None
        assert second.seq > first.seq
        assert first.role == "user" and first.sender_name == "阿一"

    def test_mark_unread_partitions_group_and_private(self):
        buffer = self._buffer()
        user = make_user("u-1", "阿一")
        group_msg = buffer.record_user_message("aiocqhttp:GroupMessage:g1", user, "群消息")
        private_msg = buffer.record_user_message(
            "aiocqhttp:FriendMessage:f1", user, "私聊消息"
        )
        buffer.mark_unread("aiocqhttp:GroupMessage:g1", group_msg, is_private=False)
        buffer.mark_unread("aiocqhttp:FriendMessage:f1", private_msg, is_private=True)
        assert buffer.get_unread_count(is_private=False) == 1
        assert buffer.get_unread_count(is_private=True) == 1
        assert buffer.get_unread_total() == 2
        group_chats = buffer.list_unread_chats(private=False)
        private_chats = buffer.list_unread_chats(private=True)
        assert [umo for umo, _ in group_chats] == ["aiocqhttp:GroupMessage:g1"]
        assert [umo for umo, _ in private_chats] == ["aiocqhttp:FriendMessage:f1"]

    def test_ack_unread_removes_only_consumed_messages(self):
        buffer = self._buffer()
        user = make_user("u-1", "阿一")
        first = buffer.record_user_message("umo-1", user, "第一条")
        second = buffer.record_user_message("umo-1", user, "第二条")
        buffer.mark_unread("umo-1", first, is_private=True)
        buffer.mark_unread("umo-1", second, is_private=True)
        removed = buffer.ack_unread("umo-1", [first.seq])
        assert removed == 1
        remaining = buffer.get_unread_batch("umo-1")
        assert [m.seq for m in remaining] == [second.seq]
        assert buffer.ack_unread("umo-1") == 1
        assert buffer.get_unread_total() == 0

    def test_unread_survives_sliding_window_eviction(self):
        buffer = self._buffer()
        user = make_user("u-1", "阿一")
        first = buffer.record_user_message("umo-1", user, "最早的消息")
        buffer.mark_unread("umo-1", first, is_private=True)
        for index in range(buffer.sliding_window_size + 5):
            buffer.record_user_message("umo-1", user, f"后续消息{index}")
        # the message was pushed out of the 15-message window but stays unread
        assert first not in buffer._windows.get("umo-1", [])
        assert [m.seq for m in buffer.get_unread_batch("umo-1")] == [first.seq]

    def test_unread_cap_keeps_newest_messages(self):
        buffer = self._buffer(unread_max_per_chat=2)
        user = make_user("u-1", "阿一")
        messages = [
            buffer.record_user_message("umo-1", user, f"消息{index}")
            for index in range(3)
        ]
        for message in messages:
            buffer.mark_unread("umo-1", message, is_private=True)
        batch = buffer.get_unread_batch("umo-1")
        assert [m.content for m in batch] == ["消息1", "消息2"]

    def test_expired_group_unread_is_pruned(self):
        buffer = self._buffer()
        user = make_user("u-1", "阿一")
        message = buffer.record_user_message("umo-1", user, "很久以前的群消息")
        buffer.mark_unread("umo-1", message, is_private=False)
        message.timestamp = time.time() - 10000  # group TTL default 2h
        assert buffer.get_unread_batch("umo-1") == []
        assert buffer.get_unread_total() == 0

    def test_format_unread_batch_renders_transcript(self):
        buffer = self._buffer()
        user = make_user("u-1", "阿一", tier=UserTier.L2_IMPORTANT)
        message = buffer.record_user_message("umo-1", user, "周末去漫展吗")
        text = buffer.format_unread_batch([message])
        assert "阿一" in text and "漫展" in text


class TestGateUnreadMarking(unittest.IsolatedAsyncioTestCase):
    """Only gate-blocked messages become unread; IGNORE after the gate stays read."""

    async def _run_gate(self, slot_state, tier, action_type):
        config = make_plugin_config()
        buffer = SessionBuffer(config)
        bridge = RecordingProactiveBridge()
        soul = make_soul("gate-soul")
        slot = make_slot(state=slot_state, action_type=action_type, armor=50)
        user = make_user("u-1", "阿一", tier=tier)

        async def resolve_soul(umo=None, allow_llm_seed=True):
            return soul

        async def resolve_user(event, soul_id=None):
            return user

        async def noop_mutator(**kwargs):
            return False

        async def get_current_slot(**kwargs):
            return slot

        async def noop_init():
            return None

        fake = SimpleNamespace(
            config=config,
            session_buffer=buffer,
            platform_bridge=bridge,
            soul_manager=SimpleNamespace(resolve_soul=resolve_soul),
            user_relation=SimpleNamespace(resolve_user=resolve_user),
            atmosphere_reader=_StubAtmosphereForGate(buffer),
            schedule_mutator=SimpleNamespace(on_message_input=noop_mutator),
            deck_engine=SimpleNamespace(get_current_slot=get_current_slot),
            _last_message_tier={},
            _known_l3_umos={},
            _known_group_umos=set(),
            _ensure_initialized=noop_init,
            _on_window_overflow=None,
        )
        fake = bind_plugin_methods(fake)
        await fake.on_any_message(FakeEvent(message_str="在吗", umo="umo-1"))
        return buffer, bridge

    async def test_resting_blocked_message_is_unread(self):
        buffer, bridge = await self._run_gate(
            BotState.RESTING, UserTier.L1_NORMAL, ActionType.SLEEP
        )
        assert buffer.get_unread_total() == 1
        assert buffer.get_unread_count(is_private=True) == 1
        assert bridge.sent == []

    async def test_busy_l1_blocked_message_is_unread(self):
        buffer, _bridge = await self._run_gate(
            BotState.BUSY, UserTier.L1_NORMAL, ActionType.BUSY_TASK
        )
        assert buffer.get_unread_total() == 1

    async def test_busy_l2_allowed_but_ignored_is_read(self):
        buffer, _bridge = await self._run_gate(
            BotState.BUSY, UserTier.L2_IMPORTANT, ActionType.BUSY_TASK
        )
        # gate allowed it; the atmosphere stub then says IGNORE -> read, not unread
        assert buffer.get_unread_total() == 0

    async def test_idle_ignored_message_is_read(self):
        buffer, _bridge = await self._run_gate(
            BotState.IDLE, UserTier.L1_NORMAL, ActionType.IDLE_ROAM
        )
        assert buffer.get_unread_total() == 0


class TestIdleRoamAction(DynamicTimelineTestBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.engine = RogueliteDeckEngine(self.db, self.config)
        self.memory_manager = MemoryManager(self.db, StubModelHub(), self.config)
        self.session_buffer = SessionBuffer(self.config)
        self.soul = make_soul("soul-roam")
        self.current_slot = make_slot(
            state=BotState.IDLE,
            action_type=ActionType.IDLE_ROAM,
            title="趴在窗边发呆",
        )
        self.run = make_run(self.soul.soul_id)

    def _make_fake(self, json_results, groups=None, cooldown=0):
        config = make_plugin_config(
            roguelite_schedule_config={
                "enable_roguelite_schedule": True,
                "max_mutations_per_day": 2,
                "mutation_cooldown_minutes": 120,
                "idle_roam_cooldown_minutes": cooldown,
                "allow_l3_wake_from_resting": True,
                "unread_group_cooldown_minutes": 60,
                "unread_private_cooldown_minutes": 60,
            }
        )
        self.config = config
        bridge = RecordingProactiveBridge()
        fake = SimpleNamespace(
            config=config,
            model_hub=SequencedStubModelHub(json_results=json_results),
            memory_manager=self.memory_manager,
            session_buffer=self.session_buffer,
            platform_bridge=bridge,
            _known_group_umos=set(groups or []),
            _known_l3_umos={"core-uid": "aiocqhttp:FriendMessage:l3"},
            _last_idle_roam_ts={},
            _last_group_lurk_ts={},
            _last_private_catchup_ts={},
        )
        return bind_plugin_methods(fake), bridge

    def _mark_group_unread(self, umo="aiocqhttp:GroupMessage:g1", content="群里刚聊到一个超大的瓜"):
        user = make_user("u-1", "群友")
        message = self.session_buffer.record_user_message(umo, user, content)
        assert message is not None
        self.session_buffer.mark_unread(umo, message, is_private=False)
        return message

    async def test_group_lurk_decides_reply_then_bubbles_up(self):
        umo = "aiocqhttp:GroupMessage:g1"
        self._mark_group_unread(umo)
        fake, bridge = self._make_fake(
            [
                {"decision": "REPLY", "confidence": 0.9, "reason": "大瓜可接"},
                {"target": "GROUP", "message": "哈哈哈这个瓜我也刚看到，太离谱了"},
            ]
        )
        sent = await fake._run_idle_roam_action(
            self.soul, self.run, self.current_slot
        )
        assert sent is True
        assert bridge.sent and bridge.sent[0][0] == umo
        # decision prompt first, generation prompt second
        assert "未读消息" in fake.model_hub.calls[0]["prompt"]
        assert "GROUP_LURK" in fake.model_hub.calls[1]["prompt"]
        # consumed once, never replayed
        assert self.session_buffer.get_unread_total() == 0

    async def test_group_lurk_ignore_consumes_without_sending(self):
        umo = "aiocqhttp:GroupMessage:g1"
        self._mark_group_unread(umo)
        fake, bridge = self._make_fake([{"decision": "IGNORE", "confidence": 0.8}])
        sent = await fake._run_idle_roam_action(
            self.soul, self.run, self.current_slot
        )
        # no memories -> MEMORY_MUSE also sends nothing
        assert sent is False
        assert bridge.sent == []
        assert self.session_buffer.get_unread_total() == 0

    async def test_group_lurk_respects_per_group_cooldown(self):
        umo = "aiocqhttp:GroupMessage:g1"
        self._mark_group_unread(umo)
        fake, bridge = self._make_fake(
            [
                {"decision": "REPLY", "confidence": 0.9},
                {"target": "GROUP", "message": "这瓜保熟吗"},
            ]
        )
        assert (
            await fake._run_idle_roam_action(self.soul, self.run, self.current_slot)
            is True
        )
        # a new unread message arrives, but the group is cooling down
        self._mark_group_unread(umo, "群里又聊起来了")
        assert (
            await fake._run_idle_roam_action(self.soul, self.run, self.current_slot)
            is False
        )
        assert len(bridge.sent) == 1
        assert self.session_buffer.get_unread_total() == 1

    async def test_memory_muse_dms_l3_with_busy_result(self):
        await self.memory_manager.add_memory(
            soul_id=self.soul.soul_id,
            content="今天把回春丹炼成了，差点炸炉",
            keywords="炼丹,繁忙结果",
            importance=4,
            memory_tier=1,
        )
        fake, bridge = self._make_fake(
            [{"target": "PRIVATE_L3", "message": "突然想起今天炼丹差点炸炉，笑死"}]
        )
        sent = await fake._run_idle_roam_action(
            self.soul, self.run, self.current_slot
        )
        assert sent is True
        assert bridge.sent[0][0] == "aiocqhttp:FriendMessage:l3"
        assert "MEMORY_MUSE" in fake.model_hub.calls[0]["prompt"]
        assert "回春丹" in fake.model_hub.calls[0]["prompt"]

    async def test_idle_roam_cooldown_blocks_second_call(self):
        self._mark_group_unread("aiocqhttp:GroupMessage:g1")
        fake, _bridge = self._make_fake(
            [
                {"decision": "REPLY", "confidence": 0.9},
                {"target": "GROUP", "message": "这瓜保熟吗"},
            ],
            cooldown=45,
        )
        assert (
            await fake._run_idle_roam_action(self.soul, self.run, self.current_slot)
            is True
        )
        calls_before = len(fake.model_hub.calls)
        assert (
            await fake._run_idle_roam_action(self.soul, self.run, self.current_slot)
            is False
        )
        assert len(fake.model_hub.calls) == calls_before


class TestPrivateUnreadCatchUp(DynamicTimelineTestBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.session_buffer = SessionBuffer(self.config)
        self.soul = make_soul("soul-private")
        self.current_slot = make_slot(
            state=BotState.IDLE,
            action_type=ActionType.IDLE_ROAM,
            title="趴在窗边发呆",
        )
        self.run = make_run(self.soul.soul_id)

    def _make_fake(self, text_result="", ok=True, enabled=True, cooldown=60):
        config = make_plugin_config(
            roguelite_schedule_config={
                "enable_roguelite_schedule": True,
                "max_mutations_per_day": 2,
                "mutation_cooldown_minutes": 120,
                "idle_roam_cooldown_minutes": 45,
                "allow_l3_wake_from_resting": True,
                "unread_private_reply_enabled": enabled,
                "unread_private_cooldown_minutes": cooldown,
                "unread_private_ttl_minutes": 720,
            }
        )
        self.config = config
        self.session_buffer.plugin_config = config
        bridge = RecordingProactiveBridge()
        bridge.ok = ok
        hub = SequencedStubModelHub(text_results=[text_result])
        fake = SimpleNamespace(
            config=config,
            model_hub=hub,
            memory_manager=MemoryManager(self.db, StubModelHub(), config),
            session_buffer=self.session_buffer,
            platform_bridge=bridge,
            _last_idle_roam_ts={},
            _last_group_lurk_ts={},
            _last_private_catchup_ts={},
            _known_group_umos=set(),
            _known_l3_umos={},
        )
        return bind_plugin_methods(fake), bridge, hub

    def _mark_private_unread(self, umo="aiocqhttp:FriendMessage:f1", content="睡了吗？"):
        user = make_user("u-l3", "阿泽", tier=UserTier.L3_CORE)
        message = self.session_buffer.record_user_message(umo, user, content)
        assert message is not None
        self.session_buffer.mark_unread(umo, message, is_private=True)
        return message

    async def test_catches_up_private_unread_and_acks(self):
        umo = "aiocqhttp:FriendMessage:f1"
        self._mark_private_unread(umo)
        fake, bridge, hub = self._make_fake(text_result="刚看到你消息，之前睡着了，现在才回你～")
        processed = await fake._catch_up_private_unread(
            self.soul, self.run, self.current_slot
        )
        assert processed == 1
        assert bridge.sent and bridge.sent[0][0] == umo
        assert "刚看到你消息" in bridge.sent[0][1]
        assert "睡了吗" in hub.calls[0]["prompt"]
        assert self.session_buffer.get_unread_total() == 0

    async def test_second_call_is_gated_by_cooldown(self):
        umo = "aiocqhttp:FriendMessage:f1"
        self._mark_private_unread(umo)
        fake, bridge, _hub = self._make_fake(text_result="好")
        assert (
            await fake._catch_up_private_unread(self.soul, self.run, self.current_slot)
            == 1
        )
        self._mark_private_unread(umo, "又发了一条")
        assert (
            await fake._catch_up_private_unread(self.soul, self.run, self.current_slot)
            == 0
        )
        assert len(bridge.sent) == 1
        assert self.session_buffer.get_unread_total() == 1

    async def test_disabled_switch_does_nothing(self):
        self._mark_private_unread()
        fake, bridge, hub = self._make_fake(enabled=False)
        assert (
            await fake._catch_up_private_unread(self.soul, self.run, self.current_slot)
            == 0
        )
        assert bridge.sent == [] and hub.calls == []
        assert self.session_buffer.get_unread_total() == 1

    async def test_send_failure_keeps_backlog(self):
        self._mark_private_unread()
        fake, bridge, _hub = self._make_fake(text_result="好", ok=False)
        assert (
            await fake._catch_up_private_unread(self.soul, self.run, self.current_slot)
            == 0
        )
        assert bridge.sent  # attempted
        assert self.session_buffer.get_unread_total() == 1

    async def test_empty_llm_reply_uses_fallback(self):
        self._mark_private_unread()
        fake, bridge, _hub = self._make_fake(text_result="")
        assert (
            await fake._catch_up_private_unread(self.soul, self.run, self.current_slot)
            == 1
        )
        assert bridge.sent and bridge.sent[0][1].strip()
        assert self.session_buffer.get_unread_total() == 0


class TestBoundedScheduleMutatorDynamicBlocks(DynamicTimelineTestBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.engine = RogueliteDeckEngine(self.db, self.config)
        self.soul = make_soul("soul-mutate")
        self.settled = []

    def _build_mutator(self):
        async def busy_settler(soul, run, block, progress_pct, interrupted_by):
            self.settled.append((block, int(progress_pct), interrupted_by))
            block.completion_result = "本来在忙这件事，刚做到一半就被拉去聊天了"
            block.busy_settled = True
            await self.engine.save_run(run)

        return BoundedScheduleMutator(
            self.engine,
            StubModelHub(json_result=None),
            self.config,
            busy_settler=busy_settler,
        )

    # Fixed calendar date used both to build the run and to resolve "now" inside
    # the mutator (``get_or_build_today_run`` keys its cache by ``today_str(now_dt)``,
    # so the build and the mutation must share the same date).
    BASE_DT = datetime(2024, 1, 2, 12, 0)

    async def _find_run_with(self, predicate):
        for seed in range(30):
            random.seed(seed)
            run = await self.engine.get_or_build_today_run(
                self.soul.soul_id,
                chronotype="night_owl",
                force_reroll=True,
                now_dt=self.BASE_DT,
            )
            for index, slot in enumerate(run.slots):
                if predicate(run, index, slot):
                    return run, index, slot
        return None, None, None

    @staticmethod
    def _dt_for_hour(hour):
        hour_int = int(hour)
        minute = int(round((hour - hour_int) * 60))
        return datetime(2024, 1, 2, hour_int, minute)

    def _event(self):
        return FakeEvent(
            message_str="醒醒！！快看，出大事了救命！！", umo="umo-1"
        )

    def _core_user(self):
        return make_user("u-core", "核心用户", tier=UserTier.L3_CORE)

    async def test_busy_task_interruption_splits_and_settles(self):
        run, index, busy = await self._find_run_with(
            lambda run, index, slot: slot.action_type == ActionType.BUSY_TASK
            and (slot.end_hour - slot.start_hour) >= 1.5
        )
        assert run is not None, "no suitable BUSY_TASK block found"
        busy.armor = 40
        now_hour = busy.start_hour + 0.5
        mutator = self._build_mutator()
        mutated = await mutator.on_message_input(
            soul=self.soul,
            event=self._event(),
            user=self._core_user(),
            is_private_chat=True,
            now_dt=self._dt_for_hour(now_hour),
        )
        assert mutated is True
        assert run.mutations_used == 1
        assert self.settled
        settled_block, progress_pct, interrupted_by = self.settled[0]
        assert 0 < progress_pct < 100
        assert interrupted_by == "核心用户"
        assert settled_block.completion_result
        assert settled_block.busy_settled is True
        assert self.engine.is_valid_timeline(run)
        assert any(
            slot.action_type == ActionType.IDLE_ROAM and slot.mutated
            for slot in run.slots
        )
        assert any(
            slot.action_type == ActionType.BUSY_TASK and slot.busy_settled
            for slot in run.slots
        )

    async def test_idle_block_extension_pushes_next_block(self):
        def predicate(run, index, slot):
            if index + 1 >= len(run.slots):
                return False
            nxt = run.slots[index + 1]
            return (
                slot.action_type in (ActionType.SURF, ActionType.IDLE_ROAM)
                and (slot.end_hour - slot.start_hour) >= 1.0
                and nxt.action_type in (ActionType.SLEEP, ActionType.BUSY_TASK)
                and (nxt.end_hour - nxt.start_hour) >= 1.0
            )

        run, index, block = await self._find_run_with(predicate)
        assert run is not None, "no suitable IDLE block found"
        block.armor = 20
        next_block = run.slots[index + 1]
        old_end = block.end_hour
        old_next_start = next_block.start_hour
        mutator = self._build_mutator()
        mutated = await mutator.on_message_input(
            soul=self.soul,
            event=self._event(),
            user=self._core_user(),
            is_private_chat=False,
            is_direct_mention=True,
            now_dt=self._dt_for_hour(block.start_hour + 0.5),
        )
        assert mutated is True
        assert block.end_hour >= old_end + 0.5 - 1e-9
        assert next_block.start_hour >= old_next_start + 0.5 - 1e-9
        assert self.engine.is_valid_timeline(run)

    async def test_sleep_wake_carves_idle_roam_window(self):
        def predicate(run, index, slot):
            return (
                slot.action_type == ActionType.SLEEP
                and (slot.end_hour - slot.start_hour) >= 1.5
            )

        run, index, sleep_block = await self._find_run_with(predicate)
        assert run is not None, "no suitable SLEEP block found"
        sleep_block.armor = 50
        mutator = self._build_mutator()
        mutated = await mutator.on_message_input(
            soul=self.soul,
            event=self._event(),
            user=self._core_user(),
            is_private_chat=True,
            now_dt=self._dt_for_hour(sleep_block.start_hour + 0.5),
        )
        assert mutated is True
        assert self.engine.is_valid_timeline(run)
        assert any(
            slot.action_type == ActionType.IDLE_ROAM and slot.mutated
            for slot in run.slots
        )


# ===========================================================================
# 7. Project-level sanity: offline import + compileall
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
            "settle_busy_block",
            "_settle_finished_busy_blocks",
            "_run_idle_roam_action",
        ):
            assert hasattr(main_module.OnlyYouPlugin, method), method
        source = inspect.getsource(main_module.OnlyYouPlugin.on_any_message)
        assert "selected_provider" in source
        assert "get_main_provider_id" in source

    def test_onlyyou_command_group_exposes_all_subcommands(self):
        """The dashboard plugin panel lists commands from AstrBot's
        CommandFilter/CommandGroupFilter registry, so `/onlyyou` must be a
        command group with one real handler per sub-command."""
        import OnlyYou.main as main_module

        plugin_cls = main_module.OnlyYouPlugin
        for handler in (
            "onlyyou_status",
            "onlyyou_state",
            "onlyyou_reroll",
            "onlyyou_tier",
            "onlyyou_memory",
            "onlyyou_surf",
            "onlyyou_unread",
            "onlyyou_help",
        ):
            assert hasattr(plugin_cls, handler), handler
            assert inspect.getdoc(getattr(plugin_cls, handler)), handler
        assert hasattr(plugin_cls, "_dispatch_onlyyou")
        dispatch_src = inspect.getsource(plugin_cls._dispatch_onlyyou)
        assert "default_sub" in dispatch_src
        # Every sub-command handler must delegate to the shared dispatcher
        # (guards against a stale standalone `cmd_onlyyou` implementation).
        for handler in (
            "onlyyou_status",
            "onlyyou_state",
            "onlyyou_reroll",
            "onlyyou_tier",
            "onlyyou_memory",
            "onlyyou_surf",
            "onlyyou_unread",
            "onlyyou_help",
        ):
            src = inspect.getsource(getattr(plugin_cls, handler))
            assert "_dispatch_onlyyou" in src, handler
        assert not hasattr(plugin_cls, "cmd_onlyyou")

    def test_onlyyou_help_text_covers_every_subcommand(self):
        import OnlyYou.main as main_module

        plugin_cls = main_module.OnlyYouPlugin
        text = plugin_cls._onlyyou_help_text(object())
        for sub in plugin_cls.ONLYYOU_SUBCOMMANDS:
            assert f"/onlyyou {sub}" in text, sub
        registered = {
            name[len("onlyyou_"):]
            for name in dir(plugin_cls)
            if name.startswith("onlyyou_")
        }
        assert registered == set(plugin_cls.ONLYYOU_SUBCOMMANDS)
        # Unknown sub-commands must be intercepted before falling through to
        # the native LLM pipeline.
        listener_src = inspect.getsource(plugin_cls.on_any_message)
        assert "ONLYYOU_SUBCOMMANDS" in listener_src
        assert "_onlyyou_help_text" in listener_src

    def test_onlyyou_commands_are_admin_only_by_default(self):
        """Every sub-command must carry AstrBot's PermissionTypeFilter(ADMIN) so
        the dashboard command panel lists them as admin-only; the dashboard
        "指令管理" page can still override this per command."""
        import OnlyYou.main as main_module

        plugin_cls = main_module.OnlyYouPlugin
        assert plugin_cls._ADMIN_ONLY_HINT
        for sub in sorted(plugin_cls.ONLYYOU_SUBCOMMANDS):
            source = inspect.getsource(getattr(plugin_cls, f"onlyyou_{sub}"))
            assert "permission_type" in source, sub
            assert "PermissionType.ADMIN" in source, sub
        listener_src = inspect.getsource(plugin_cls.on_any_message)
        assert "_ADMIN_ONLY_HINT" in listener_src
        assert "is_admin" in listener_src
        # Default (non-admin) event must be reported as non-admin.
        from tests._helpers import FakeEvent  # type: ignore

        assert FakeEvent().is_admin() is False
        assert FakeEvent(role="admin").is_admin() is True

    def test_metadata_help_lists_unread_and_has_no_encounters(self):
        with open(os.path.join(PROJECT_ROOT, "metadata.yaml"), "r", encoding="utf-8") as handle:
            metadata = handle.read()
        assert "/onlyyou unread" in metadata
        assert "/onlyyou help" in metadata
        assert "仅管理员" in metadata
        assert "奇遇" not in metadata

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
        # 24h dynamic timeline config: encounter count removed, roam cooldown added.
        roguelite = schema.get("roguelite_schedule_config", {}).get("items", {})
        assert "daily_encounter_count" not in roguelite
        assert "idle_roam_cooldown_minutes" in roguelite
        assert roguelite["idle_roam_cooldown_minutes"]["default"] == 45

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
