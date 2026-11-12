"""v2.6 offline tests: ModelHub optional inheritance + four fixed armor tiers.

Covers contract ``.team/V26_IMPLEMENTATION_CONTRACT.md`` §1 and §3.1-3.3:

1. ``normalize_model_config`` ``inherit_from`` branches (explicit / inherit flag /
   legacy default / illegal fallback) for ``decision`` and ``vision``;
2. ``ModelHub.resolve_role`` — disabled inheritance never falls back to ``main``,
   ``vision -> decision -> main`` chain, hop budget / cycle safety;
3. ``ModelHub.get_status`` — ``inherit`` / ``inherit_from`` / ``inherited`` /
   ``configured`` fields and inheritance labels;
4. ``ActionType.fixed_armor`` / ``armor_tier`` / ``to_meta_dict`` and
   ``ACTION_TYPE_DEFINITIONS`` (80 / 40 / 25 / 65 and tiers 4 / 2 / 1 / 3);
5. ``StateEventCard`` default armor -> ``action.fixed_armor`` (explicit legacy
   values still parse), dynamic / static timelines and ``_rebalance_blocks`` all
   pin the fixed tier, ``force_set_current_state`` ignores the caller armor;
6. ``BoundedScheduleMutator`` products: BUSY split front 65 / tail 25, SLEEP wake
   window 25 with remaining SLEEP 80, SURF / IDLE_ROAM extension keeps its own
   fixed tier — timeline invariants preserved.

Everything is stdlib ``unittest`` + ``asyncio`` (``IsolatedAsyncioTestCase``) and
fully offline: no network, no AstrBot install, no real LLM.
"""

import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _helpers import (  # noqa: E402
    DatabaseAsyncTestCase,
    FakeEvent,
    StubModelHub,
    load_plugin_modules,
    make_plugin_config,
    make_run,
    make_soul,
    make_user,
)

mods = load_plugin_modules()

ModelHub = mods.model_hub.ModelHub
normalize_model_config = mods.model_hub.normalize_model_config
DEFAULT_MODEL_CONFIG = mods.model_hub.DEFAULT_MODEL_CONFIG
ActionType = mods.state_machine.ActionType
ACTION_TYPE_DEFINITIONS = mods.state_machine.ACTION_TYPE_DEFINITIONS
BotState = mods.state_machine.BotState
UserTier = mods.state_machine.UserTier
RogueliteDeckEngine = mods.roguelite_deck.RogueliteDeckEngine
StateEventCard = mods.roguelite_deck.StateEventCard
SlotInstance = mods.roguelite_deck.SlotInstance
DEFAULT_STATE_EVENT_CARDS = mods.roguelite_deck.DEFAULT_STATE_EVENT_CARDS
DailyScheduleRun = mods.roguelite_deck.DailyScheduleRun
BoundedScheduleMutator = mods.schedule_mutator.BoundedScheduleMutator

MAIN_CFG = {"main": {"source_id": "deepseek", "model": "deepseek-chat"}}
MAIN_PAIR = {"source_id": "deepseek", "model": "deepseek-chat"}

URGENT_MSG = "醒醒！！快看，出大事了救命！！"


def make_hub(model_config=None):
    """Build a loaded ModelHub with an in-memory normalized config (no provider)."""
    hub = ModelHub(context=None, plugin_config={}, db=None, provider_factory=None)
    hub._config = normalize_model_config(model_config or {})
    hub._loaded = True
    return hub


def core_user():
    return make_user("u-core", "核心用户", tier=UserTier.L3_CORE)


# ===========================================================================
# 1. ModelHub config normalization (§1.1)
# ===========================================================================
class TestNormalizeInheritFrom(unittest.TestCase):
    def test_default_config_declares_inherit_from(self):
        assert DEFAULT_MODEL_CONFIG["decision"] == {
            "inherit": True,
            "inherit_from": "main",
            "source_id": "",
            "model": "",
        }
        assert DEFAULT_MODEL_CONFIG["vision"] == {
            "inherit": True,
            "inherit_from": "main",
            "source_id": "",
            "model": "",
        }
        cfg = normalize_model_config(None)
        for role in ("decision", "vision"):
            assert cfg[role]["inherit"] is True
            assert cfg[role]["inherit_from"] == "main"
            assert cfg[role]["source_id"] == "" and cfg[role]["model"] == ""

    def test_legacy_config_without_inherit_keys_defaults_to_main(self):
        cfg = normalize_model_config(MAIN_CFG)
        assert cfg["decision"]["inherit_from"] == "main"
        assert cfg["decision"]["inherit"] is True
        assert cfg["vision"]["inherit_from"] == "main"
        assert cfg["vision"]["inherit"] is True

    def test_explicit_inherit_false_means_independent(self):
        cfg = normalize_model_config({"decision": {"inherit": False}})
        assert cfg["decision"]["inherit"] is False
        assert cfg["decision"]["inherit_from"] == ""

    def test_explicit_inherit_true_means_inherit_main(self):
        cfg = normalize_model_config({"decision": {"inherit": True}})
        assert cfg["decision"]["inherit"] is True
        assert cfg["decision"]["inherit_from"] == "main"

    def test_explicit_inherit_from_wins_over_inherit_flag(self):
        cfg = normalize_model_config(
            {"decision": {"inherit": False, "inherit_from": "main"}}
        )
        assert cfg["decision"]["inherit"] is True
        assert cfg["decision"]["inherit_from"] == "main"

        cfg = normalize_model_config(
            {"decision": {"inherit": True, "inherit_from": ""}}
        )
        assert cfg["decision"]["inherit"] is False
        assert cfg["decision"]["inherit_from"] == ""

    def test_vision_may_inherit_decision_but_decision_cannot(self):
        cfg = normalize_model_config({"vision": {"inherit_from": "decision"}})
        assert cfg["vision"]["inherit_from"] == "decision"
        assert cfg["vision"]["inherit"] is True
        # ``decision`` can only inherit ``main`` -> illegal value falls back to main
        cfg = normalize_model_config({"decision": {"inherit_from": "vision"}})
        assert cfg["decision"]["inherit_from"] == "main"

    def test_illegal_inherit_from_falls_back_to_main_or_empty(self):
        # default inherit is True -> "main"
        cfg = normalize_model_config({"vision": {"inherit_from": "bogus"}})
        assert cfg["vision"]["inherit_from"] == "main"
        assert cfg["vision"]["inherit"] is True
        # explicit inherit=False + illegal target -> disabled
        cfg = normalize_model_config(
            {"vision": {"inherit_from": "bogus", "inherit": False}}
        )
        assert cfg["vision"]["inherit_from"] == ""
        assert cfg["vision"]["inherit"] is False

    def test_none_inherit_from_falls_through_to_inherit_flag(self):
        cfg = normalize_model_config(
            {"decision": {"inherit_from": None, "inherit": False}}
        )
        assert cfg["decision"]["inherit_from"] == ""
        assert cfg["decision"]["inherit"] is False
        cfg = normalize_model_config({"vision": {"inherit_from": None}})
        assert cfg["vision"]["inherit_from"] == "main"

    def test_source_and_model_are_preserved_even_when_inheriting(self):
        cfg = normalize_model_config(
            {
                "decision": {
                    "inherit_from": "main",
                    "source_id": "  s2  ",
                    "model": "m2",
                }
            }
        )
        assert cfg["decision"]["source_id"] == "s2"
        assert cfg["decision"]["model"] == "m2"
        assert cfg["decision"]["inherit_from"] == "main"


# ===========================================================================
# 2. ModelHub role resolution (§1.2)
# ===========================================================================
class TestResolveRoleInheritance(unittest.IsolatedAsyncioTestCase):
    async def test_main_resolves_own_pair(self):
        hub = make_hub(MAIN_CFG)
        assert await hub.resolve_role("main") == MAIN_PAIR

    async def test_default_decision_and_vision_inherit_main(self):
        hub = make_hub(MAIN_CFG)
        assert await hub.resolve_role("decision") == MAIN_PAIR
        assert await hub.resolve_role("vision") == MAIN_PAIR
        # legacy alias
        assert await hub.resolve_role("small") == MAIN_PAIR

    async def test_disabled_inheritance_without_own_config_returns_none(self):
        hub = make_hub(
            {
                **MAIN_CFG,
                "decision": {"inherit_from": ""},
                "vision": {"inherit": False},
            }
        )
        # main is configured, but disabled roles must NEVER fall back to it.
        assert await hub.resolve_role("main") == MAIN_PAIR
        assert await hub.resolve_role("decision") is None
        assert await hub.resolve_role("vision") is None

    async def test_disabled_inheritance_uses_own_complete_config(self):
        hub = make_hub(
            {
                **MAIN_CFG,
                "decision": {
                    "inherit_from": "",
                    "source_id": "deepseek",
                    "model": "deepseek-reasoner",
                },
            }
        )
        assert await hub.resolve_role("decision") == {
            "source_id": "deepseek",
            "model": "deepseek-reasoner",
        }

    async def test_disabled_inheritance_with_partial_config_returns_none(self):
        hub = make_hub(
            {
                **MAIN_CFG,
                "decision": {"inherit_from": "", "source_id": "deepseek", "model": ""},
            }
        )
        assert await hub.resolve_role("decision") is None

    async def test_vision_inherits_decision_then_main(self):
        hub = make_hub(
            {
                **MAIN_CFG,
                "decision": {"inherit_from": "main"},
                "vision": {"inherit_from": "decision"},
            }
        )
        assert await hub.resolve_role("vision") == MAIN_PAIR

    async def test_vision_inherits_decision_own_config(self):
        hub = make_hub(
            {
                **MAIN_CFG,
                "decision": {
                    "inherit_from": "",
                    "source_id": "deepseek",
                    "model": "deepseek-reasoner",
                },
                "vision": {"inherit_from": "decision"},
            }
        )
        assert await hub.resolve_role("vision") == {
            "source_id": "deepseek",
            "model": "deepseek-reasoner",
        }

    async def test_vision_inheriting_unconfigured_decision_returns_none(self):
        hub = make_hub(
            {
                **MAIN_CFG,
                "decision": {"inherit_from": ""},  # disabled + empty
                "vision": {"inherit_from": "decision"},
            }
        )
        assert await hub.resolve_role("decision") is None
        # main is configured but vision -> decision never falls back to main.
        assert await hub.resolve_role("vision") is None

    async def test_unconfigured_main_returns_none_for_everyone(self):
        hub = make_hub({})
        for role in ("main", "decision", "vision"):
            assert await hub.resolve_role(role) is None, role

    async def test_hop_budget_and_cycle_guard(self):
        hub = make_hub(
            {
                **MAIN_CFG,
                "decision": {"inherit_from": "main"},
                "vision": {"inherit_from": "decision"},
            }
        )
        # exactly 2 hops is the ceiling
        assert hub._resolve_chat_role("vision", hops=2) is None
        assert hub._resolve_chat_role("vision", hops=0) == MAIN_PAIR
        # pathological hand-written cycle: decision -> vision is not a legal
        # parent for decision, so it degrades to "own config only" (None),
        # never recursing forever and never falling back to main.
        hub._config["decision"]["inherit_from"] = "vision"
        hub._config["vision"]["inherit_from"] = "decision"
        assert await hub.resolve_role("vision") is None
        assert await hub.resolve_role("decision") is None

    async def test_unknown_role_returns_none(self):
        hub = make_hub(MAIN_CFG)
        assert await hub.resolve_role("bogus") is None
        assert await hub.resolve_role("") is None


# ===========================================================================
# 3. ModelHub status reporting (§1.3)
# ===========================================================================
class TestModelHubStatus(unittest.IsolatedAsyncioTestCase):
    async def test_default_status_reports_inheritance_fields(self):
        hub = make_hub(MAIN_CFG)
        status = await hub.get_status()
        assert set(status) == {"main", "decision", "vision", "tts"}
        required = {
            "role",
            "configured",
            "inherited",
            "inherit",
            "inherit_from",
            "source_id",
            "model",
            "provider_id",
            "mode",
            "label",
            "ok",
        }
        for role in ("main", "decision", "vision"):
            assert required <= set(status[role]), role
        assert status["main"]["inherit"] is False
        assert status["main"]["inherit_from"] == ""
        assert status["main"]["inherited"] is False
        assert status["decision"]["inherit"] is True
        assert status["decision"]["inherit_from"] == "main"
        assert status["decision"]["inherited"] is True
        assert status["decision"]["configured"] is True
        assert "继承主模型" in status["decision"]["label"]
        assert status["vision"]["inherit_from"] == "main"
        assert status["vision"]["inherited"] is True
        assert "继承主模型" in status["vision"]["label"]

    async def test_status_disabled_unconfigured_role(self):
        hub = make_hub({**MAIN_CFG, "decision": {"inherit_from": ""}})
        status = await hub.get_status()
        decision = status["decision"]
        assert decision["configured"] is False
        assert decision["inherited"] is False
        assert decision["inherit"] is False
        assert decision["inherit_from"] == ""
        assert decision["mode"] == "unconfigured"
        assert decision["label"] == "未配置"
        assert decision["ok"] is False
        assert decision["source_id"] == "" and decision["model"] == ""

    async def test_status_disabled_but_own_config(self):
        hub = make_hub(
            {
                **MAIN_CFG,
                "decision": {
                    "inherit_from": "",
                    "source_id": "deepseek",
                    "model": "deepseek-reasoner",
                },
            }
        )
        decision = (await hub.get_status())["decision"]
        assert decision["configured"] is True
        assert decision["inherited"] is False
        assert decision["inherit"] is False
        assert decision["inherit_from"] == ""
        assert "继承" not in decision["label"]

    async def test_status_vision_inherits_decision_label(self):
        hub = make_hub(
            {
                **MAIN_CFG,
                "decision": {"inherit_from": "main"},
                "vision": {"inherit_from": "decision"},
            }
        )
        vision = (await hub.get_status())["vision"]
        assert vision["inherit_from"] == "decision"
        assert vision["inherited"] is True
        assert "继承轻量决策模型" in vision["label"]

    async def test_status_vision_inheriting_disabled_decision_is_unconfigured(self):
        hub = make_hub(
            {
                **MAIN_CFG,
                "decision": {"inherit_from": ""},
                "vision": {"inherit_from": "decision"},
            }
        )
        vision = (await hub.get_status())["vision"]
        assert vision["configured"] is False
        assert vision["inherited"] is False
        assert vision["mode"] == "unconfigured"

    async def test_status_tts_never_inherits(self):
        hub = make_hub(MAIN_CFG)
        tts = (await hub.get_status())["tts"]
        assert tts["inherit"] is False
        assert tts["inherit_from"] == ""
        assert tts["inherited"] is False
        assert tts["mode"] == "off"


class TestModelConfigPersistence(DatabaseAsyncTestCase):
    async def test_inherit_from_round_trips_through_plugin_settings(self):
        hub = ModelHub(context=None, plugin_config={}, db=self.db)
        await hub.load()
        saved = await hub.save_model_config(
            {
                **MAIN_CFG,
                "decision": {"inherit_from": "main"},
                "vision": {"inherit_from": "decision"},
            }
        )
        assert saved["decision"]["inherit"] is True
        assert saved["decision"]["inherit_from"] == "main"
        assert saved["vision"]["inherit"] is True
        assert saved["vision"]["inherit_from"] == "decision"

        reloaded = ModelHub(context=None, plugin_config={}, db=self.db)
        await reloaded.load()
        cfg = reloaded.get_model_config()
        assert cfg["decision"]["inherit_from"] == "main"
        assert cfg["vision"]["inherit_from"] == "decision"
        assert await reloaded.resolve_role("vision") == MAIN_PAIR

    async def test_closed_inheritance_persists_after_reload(self):
        hub = ModelHub(context=None, plugin_config={}, db=self.db)
        await hub.load()
        await hub.save_model_config(
            {
                **MAIN_CFG,
                "decision": {"inherit_from": ""},
                "vision": {"inherit_from": ""},
            }
        )
        reloaded = ModelHub(context=None, plugin_config={}, db=self.db)
        await reloaded.load()
        assert await reloaded.resolve_role("decision") is None
        assert await reloaded.resolve_role("vision") is None


# ===========================================================================
# 4. ActionType fixed armor tiers (§3.1)
# ===========================================================================
class TestActionTypeArmorTiers(unittest.TestCase):
    EXPECTED = {
        ActionType.SLEEP: (80, 4),
        ActionType.SURF: (40, 2),
        ActionType.IDLE_ROAM: (25, 1),
        ActionType.BUSY_TASK: (65, 3),
    }

    def test_fixed_armor_and_tier_values(self):
        for action, (armor, tier) in self.EXPECTED.items():
            assert action.fixed_armor == armor, action
            assert action.armor_tier == tier, action

    def test_action_type_definitions_have_armor_fields(self):
        assert [item["value"] for item in ACTION_TYPE_DEFINITIONS] == [
            "SLEEP",
            "SURF",
            "IDLE_ROAM",
            "BUSY_TASK",
        ]
        by_value = {item["value"]: item for item in ACTION_TYPE_DEFINITIONS}
        for action in ActionType:
            definition = by_value[action.value]
            assert definition["armor"] == action.fixed_armor
            assert definition["armor_tier"] == action.armor_tier
            # existing keys are preserved
            assert set(definition) >= {
                "value",
                "label",
                "state",
                "min_hours",
                "max_hours",
                "desc",
                "armor",
                "armor_tier",
            }

    def test_to_meta_dict_matches_definition(self):
        for action in ActionType:
            meta = action.to_meta_dict()
            assert meta["value"] == action.value
            assert meta["label"] == action.label
            assert meta["state"] == action.bound_state.value
            assert meta["armor"] == action.fixed_armor
            assert meta["armor_tier"] == action.armor_tier
            assert meta["min_hours"] > 0 and meta["max_hours"] >= meta["min_hours"]


# ===========================================================================
# 5. StateEventCard / deck timeline / force_set fixed armor (§3.2)
# ===========================================================================
class TestStateEventCardArmor(unittest.TestCase):
    def test_default_armor_follows_action_type(self):
        # action_type is the single authority: state / armor are always derived
        # from it (the legacy title-keyword inference was removed in v2.9).
        cases = [
            (ActionType.SLEEP, "呼呼大睡", 80),
            (ActionType.SURF, "窝沙发刷搞笑视频", 40),
            (ActionType.IDLE_ROAM, "趴在窗边发呆", 25),
            (ActionType.BUSY_TASK, "专注处理手头正事", 65),
        ]
        for action, title, armor in cases:
            card = StateEventCard(card_id="c", title=title, action_type=action)
            assert card.action == action
            assert card.state == action.bound_state
            assert card.armor == armor

    def test_explicit_armor_override_is_ignored(self):
        # v2.9: armor is a fixed tier derived from action_type only.
        card = StateEventCard(
            card_id="c",
            title="窝沙发刷搞笑视频",
            action_type=ActionType.SURF,
            armor=33,
        )
        assert card.armor == ActionType.SURF.fixed_armor == 40
        card = StateEventCard(
            card_id="c2",
            title="窝沙发刷搞笑视频",
            action_type=ActionType.SURF,
            armor=999,
        )
        assert card.armor == ActionType.SURF.fixed_armor == 40

    def test_from_dict_missing_armor_uses_fixed_tier(self):
        for action in ActionType:
            payload = {
                "card_id": "a",
                "title": "窝沙发刷搞笑视频",
                "state": "IDLE",
                "action_type": action.value,
            }
            card = StateEventCard.from_dict(payload)
            assert card.action == action, payload
            assert card.armor == action.fixed_armor, payload

    def test_from_dict_none_or_blank_armor_uses_fixed_tier(self):
        for value in (None, "", "  "):
            card = StateEventCard.from_dict(
                {
                    "card_id": "a",
                    "title": "夜间关灯沉睡休息",
                    "action_type": "SLEEP",
                    "armor": value,
                }
            )
            assert card.armor == ActionType.SLEEP.fixed_armor

    def test_from_dict_explicit_armor_is_ignored(self):
        card = StateEventCard.from_dict(
            {
                "card_id": "a",
                "title": "夜间关灯沉睡休息",
                "state": "RESTING",
                "action_type": "SLEEP",
                "armor": "45",
            }
        )
        assert card.armor == ActionType.SLEEP.fixed_armor == 80
        # authoritative action type wins for the action field
        card = StateEventCard.from_dict(
            {"card_id": "b", "title": "x", "state": "IDLE", "action_type": "BUSY_TASK"}
        )
        assert card.action == ActionType.BUSY_TASK
        assert card.armor == ActionType.BUSY_TASK.fixed_armor

    def test_from_dict_without_action_type_defaults_to_idle_roam(self):
        card = StateEventCard.from_dict(
            {"card_id": "legacy", "title": "躺沙发上刷B站视频", "state": "IDLE"}
        )
        assert card.action == ActionType.IDLE_ROAM
        assert card.armor == ActionType.IDLE_ROAM.fixed_armor == 25
        assert "valid_slots" not in card.to_dict()

    def test_default_card_pool_uses_fixed_tiers(self):
        assert DEFAULT_STATE_EVENT_CARDS
        for card in DEFAULT_STATE_EVENT_CARDS:
            assert card.armor == card.action.fixed_armor, card.card_id

    def test_slot_instance_to_dict_keeps_armor(self):
        slot = SlotInstance(
            slot_key="block_0",
            start_hour=0.0,
            end_hour=7.0,
            state=BotState.RESTING,
            action_type=ActionType.SLEEP,
            armor=80,
        )
        payload = slot.to_dict()
        assert payload["armor"] == 80
        assert payload["action_type"] == "SLEEP"
        # default stays 50 for direct test-helper construction
        assert (
            SlotInstance(
                slot_key="block_1",
                start_hour=7.0,
                end_hour=8.0,
                state=BotState.IDLE,
            ).armor
            == 50
        )


class DeckArmorTestBase(DatabaseAsyncTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.config = make_plugin_config()
        self.engine = RogueliteDeckEngine(self.db, self.config)


class TestDeckTimelineArmor(DeckArmorTestBase):
    async def test_dynamic_timeline_blocks_use_fixed_armor(self):
        import random as _random

        for chronotype in ("early", "normal", "night_owl"):
            for seed in range(4):
                _random.seed(seed)
                run = await self.engine.get_or_build_today_run(
                    f"soul-{chronotype}-{seed}",
                    chronotype=chronotype,
                    force_reroll=True,
                )
                assert self.engine.is_valid_timeline(run)
                assert 7 <= len(run.slots) <= 11
                for slot in run.slots:
                    assert slot.action_type is not None
                    assert slot.armor == slot.action.fixed_armor, (
                        slot.action_type,
                        slot.armor,
                    )
                    assert slot.to_dict()["armor"] == slot.action.fixed_armor

    async def test_static_timeline_uses_fixed_armor(self):
        config = make_plugin_config(
            roguelite_schedule_config={
                "enable_roguelite_schedule": False,
                "max_mutations_per_day": 2,
                "mutation_cooldown_minutes": 120,
                "allow_l3_wake_from_resting": True,
            }
        )
        engine = RogueliteDeckEngine(self.db, config)
        run = await engine.get_or_build_today_run("soul-static")
        assert [(s.action_type, s.armor) for s in run.slots] == [
            (ActionType.SLEEP, 80),
            (ActionType.IDLE_ROAM, 25),
            (ActionType.SURF, 40),
        ]
        assert engine.is_valid_timeline(run)

    async def test_rebalance_blocks_pins_fixed_armor(self):
        blocks = [
            SlotInstance(
                slot_key=f"block_{index}",
                start_hour=float(index),
                end_hour=float(index + 1),
                state=BotState.IDLE,
                action_type=ActionType.IDLE_ROAM,
                armor=90,  # stale hand-tuned value must be normalized
            )
            for index in range(24)
        ]
        balanced = RogueliteDeckEngine._rebalance_blocks(blocks, target=9)
        assert 7 <= len(balanced) <= 11
        for slot in balanced:
            assert slot.armor == slot.action.fixed_armor
            assert slot.slot_key.startswith("block_")
        run = DailyScheduleRun(
            soul_id="soul-rb",
            date_str=self.engine.today_str(),
            vigor_id="v",
            vigor_title="v",
            vigor_desc="v",
            slots=balanced,
        )
        assert self.engine.is_valid_timeline(run)

    async def test_force_set_ignores_caller_armor_and_pins_fixed_tier(self):
        now_dt = datetime(2024, 1, 2, 10, 0)
        slot = await self.engine.force_set_current_state(
            "soul-force",
            BotState.BUSY,
            new_title="苦逼赶稿",
            armor=12,
            now_dt=now_dt,
        )
        assert slot.state == BotState.BUSY
        assert slot.action == ActionType.BUSY_TASK
        assert slot.armor == 65

        surf = await self.engine.force_set_current_state(
            "soul-force",
            None,
            action_type=ActionType.SURF,
            armor=100,
            now_dt=now_dt,
        )
        assert surf.action == ActionType.SURF
        assert surf.armor == 40

        sleep = await self.engine.force_set_current_state(
            "soul-force",
            None,
            action_type="SLEEP",
            armor=1,
            now_dt=now_dt,
        )
        assert sleep.action == ActionType.SLEEP
        assert sleep.armor == 80

        roam = await self.engine.force_set_current_state(
            "soul-force",
            BotState.IDLE,
            new_title="发呆放空",
            armor=0,
            now_dt=now_dt,
        )
        assert roam.action == ActionType.IDLE_ROAM
        assert roam.armor == 25


# ===========================================================================
# 6. Mutator products keep the fixed armor tiers (§3.3)
# ===========================================================================
class MutatorArmorTestBase(DatabaseAsyncTestCase):
    NOW = datetime(2024, 1, 2, 10, 0)

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.config = make_plugin_config()
        self.engine = RogueliteDeckEngine(self.db, self.config)
        self.soul = make_soul("soul-mut")
        self.mutator = BoundedScheduleMutator(
            self.engine, StubModelHub(json_result=None), self.config
        )
        self.run = make_run(self.soul.soul_id)
        self.run.date_str = self.engine.today_str(self.NOW)
        # Start from the canonical fixed tiers, like a freshly built v2.6 run.
        for slot in self.run.slots:
            slot.armor = slot.action.fixed_armor
        await self.engine.save_run(self.run)

    def event(self, message=URGENT_MSG):
        return FakeEvent(message_str=message, umo="umo-1")

    def assert_fixed_armor(self, run):
        assert self.engine.is_valid_timeline(run)
        for slot in run.slots:
            assert slot.armor == slot.action.fixed_armor, (
                slot.slot_key,
                slot.action_type,
                slot.armor,
            )

    async def mutate(self, now_dt, *, is_private_chat=True, is_direct_mention=False):
        return await self.mutator.on_message_input(
            soul=self.soul,
            event=self.event(),
            user=core_user(),
            is_private_chat=is_private_chat,
            is_direct_mention=is_direct_mention,
            now_dt=now_dt,
        )


class TestMutatorArmor(MutatorArmorTestBase):
    async def test_busy_split_sets_front_65_and_tail_25(self):
        now_dt = datetime(2024, 1, 2, 10, 0)  # inside the 09:00-12:00 BUSY block
        mutated = await self.mutate(now_dt)
        assert mutated is True
        assert self.run.mutations_used == 1
        self.assert_fixed_armor(self.run)

        busy_slots = [s for s in self.run.slots if s.action == ActionType.BUSY_TASK]
        assert busy_slots, "the front half must stay BUSY_TASK"
        for slot in busy_slots:
            assert slot.armor == 65
        # the front half ends exactly at the interrupted moment
        front = next(s for s in busy_slots if s.busy_settled is False and s.end_hour == 10.0)
        assert front.end_hour == 10.0
        tails = [
            s
            for s in self.run.slots
            if s.action == ActionType.IDLE_ROAM and s.mutated and s.start_hour == 10.0
        ]
        assert tails, "the rest of the interrupted block must become IDLE_ROAM"
        assert tails[0].armor == 25

    async def test_sleep_wake_window_25_remaining_segments_80(self):
        now_dt = datetime(2024, 1, 2, 0, 10)
        mutated = await self.mutate(now_dt)
        assert mutated is True
        self.assert_fixed_armor(self.run)

        awake = [s for s in self.run.slots if s.action == ActionType.IDLE_ROAM and s.mutated]
        assert awake, "an IDLE_ROAM waking window must be carved out"
        assert awake[0].armor == 25
        sleep_slots = [s for s in self.run.slots if s.action == ActionType.SLEEP]
        assert sleep_slots
        for slot in sleep_slots:
            assert slot.armor == 80
        # remaining sleep still covers the rest of the 24h timeline
        assert any(s.end_hour == 7.0 for s in sleep_slots)

    async def test_idle_roam_extension_keeps_own_tier(self):
        now_dt = datetime(2024, 1, 2, 7, 30)  # inside the 07:00-09:00 IDLE_ROAM block
        mutated = await self.mutate(now_dt)
        assert mutated is True
        self.assert_fixed_armor(self.run)

        extended = next(
            s
            for s in self.run.slots
            if s.action == ActionType.IDLE_ROAM and s.mutated and s.start_hour == 7.0
        )
        assert extended.end_hour == 9.5
        assert extended.armor == 25
        after = next(s for s in self.run.slots if s.start_hour == 9.5)
        assert after.action == ActionType.BUSY_TASK
        assert after.armor == 65

    async def test_surf_extension_keeps_surf_tier(self):
        now_dt = datetime(2024, 1, 2, 13, 30)  # inside the 13:00-15:00 SURF block
        mutated = await self.mutate(now_dt)
        assert mutated is True
        self.assert_fixed_armor(self.run)

        extended = next(
            s
            for s in self.run.slots
            if s.action == ActionType.SURF and s.mutated and s.start_hour == 13.0
        )
        assert extended.end_hour == 15.5
        assert extended.armor == 40
        after = next(s for s in self.run.slots if s.start_hour == 15.5)
        assert after.action == ActionType.BUSY_TASK
        assert after.armor == 65

    async def test_stale_armor_values_are_normalized_after_mutation(self):
        # Simulate a legacy v2.5 run loaded from SQLite with hand-tuned armor
        # values (only the current BUSY block must still be breakable by impact 90).
        for slot in self.run.slots:
            slot.armor = 91
        current = next(
            slot
            for slot in self.run.slots
            if slot.action == ActionType.BUSY_TASK and slot.start_hour == 9.0
        )
        current.armor = 40
        now_dt = datetime(2024, 1, 2, 10, 0)
        mutated = await self.mutate(now_dt)
        assert mutated is True
        self.assert_fixed_armor(self.run)


if __name__ == "__main__":
    unittest.main()
