"""v2.10 T2 offline tests: probabilistic / budget reply gate for group read-air.

Frozen contract (``docs/TODO.md`` T2):

1. The gate sits **after** the direct-mention + BUSY fast paths and **before** the
   debounce window / 5D atmosphere decision model.
2. It only affects ordinary (non-@, non-wake, non-quote) group messages: private
   chats, @ / wake / quote messages and the BUSY special case return earlier.
3. ``reply_gate_enabled=False`` or ``reply_gate_probability=100`` keeps the legacy
   behaviour; ``0`` never reaches the decision model.
4. An optional rolling hourly budget (``reply_gate_hourly_limit``, ``0`` =
   unlimited) caps how many ordinary messages per UMO enter read-air per hour.
5. Rejected messages never call the decision model and never enter debounce.
6. Defaults: enabled, 80% pass rate, no hourly limit; invalid configs are clamped.

Stdlib ``unittest`` only, fully offline.
"""

import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _helpers import (  # noqa: E402
    StubModelHub,
    load_plugin_modules,
    make_plugin_config,
    make_slot,
    make_soul,
    make_user,
)

mods = load_plugin_modules()
SessionBuffer = mods.context_budget.SessionBuffer
AtmosphereReader = mods.atmosphere_reader.AtmosphereReader
BotState = mods.state_machine.BotState
ActionType = mods.state_machine.ActionType
UserTier = mods.state_machine.UserTier

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GROUP_UMO = "aiocqhttp:GroupMessage:g1"


class _GroupEvent:
    """Minimal AstrBot group event (ordinary message, no @ / wake / quote)."""

    def __init__(
        self,
        message_str="今天这瓜也太大了",
        umo=GROUP_UMO,
        sender_id="u-1",
        sender_name="阿一",
        at_bot=False,
    ):
        self.message_str = message_str
        self.unified_msg_origin = umo
        self.is_at_or_wake_command = bool(at_bot)
        self.is_wake = bool(at_bot)
        self.call_llm = False
        self.message_obj = SimpleNamespace(
            message=[], raw_message={"post_type": "message"}
        )
        self._sender_id = sender_id
        self._sender_name = sender_name

    def get_sender_id(self):
        return self._sender_id

    def get_sender_name(self):
        return self._sender_name

    def get_group_id(self):
        return "g1"

    def get_self_id(self):
        return "bot-1"

    def is_admin(self):
        return False

    def is_private_chat(self):
        return False

    def should_call_llm(self, value):
        self.call_llm = bool(value)

    def set_extra(self, key, value):
        self._extra = getattr(self, "_extra", {})
        self._extra[key] = value


class _PrivateEvent(_GroupEvent):
    """Minimal AstrBot private-chat event."""

    def __init__(self, message_str="在吗", umo="aiocqhttp:FriendMessage:u-1"):
        super().__init__(message_str=message_str, umo=umo)

    def get_group_id(self):
        return None

    def is_private_chat(self):
        return True


class GateTestBase(unittest.IsolatedAsyncioTestCase):
    def build_reader(self, hub=None, **atmosphere_cfg):
        config = make_plugin_config()
        atmosphere = {
            "takeover_group_chat": True,
            "debounce_seconds": 0.0,
            "reply_gate_enabled": True,
            "reply_gate_probability": 100,
            "reply_gate_hourly_limit": 0,
        }
        atmosphere.update(atmosphere_cfg)
        config["atmosphere_config"] = atmosphere
        self.config = config
        self.buffer = SessionBuffer(config)
        self.hub = (
            hub
            if hub is not None
            else StubModelHub(json_result={"decision": "REPLY", "confidence": 0.9})
        )
        self.reader = AtmosphereReader(self.buffer, self.hub, config)
        self.soul = make_soul("soul-gate")
        self.idle_slot = make_slot(state=BotState.IDLE, action_type=ActionType.IDLE_ROAM)
        self.busy_slot = make_slot(state=BotState.BUSY, action_type=ActionType.BUSY_TASK)
        self.user = make_user("u-1", "阿一")
        self.l2_user = make_user("u-2", "老王", tier=UserTier.L2_IMPORTANT)
        self.l3_user = make_user("u-3", "阿泽", tier=UserTier.L3_CORE)
        return self.reader

    async def evaluate(self, event=None, is_direct=False, slot=None, user=None):
        return await self.reader.evaluate_should_reply(
            event=event or _GroupEvent(),
            soul=self.soul,
            user=user or self.user,
            current_slot=slot or self.idle_slot,
            is_direct_mention=is_direct,
        )


# ===========================================================================
# 1. Defaults / clamping
# ===========================================================================
class TestGateConfigDefaults(GateTestBase):
    async def test_missing_config_defaults_to_enabled_80_unlimited(self):
        config = make_plugin_config()
        config["atmosphere_config"] = {}
        reader = AtmosphereReader(SessionBuffer(config), StubModelHub(), config)
        assert reader.reply_gate_enabled is True
        assert reader.reply_gate_probability == 80.0
        assert reader.reply_gate_hourly_limit == 0

    async def test_invalid_values_are_clamped(self):
        self.build_reader(reply_gate_probability=250)
        assert self.reader.reply_gate_probability == 100.0
        self.build_reader(reply_gate_probability=-5)
        assert self.reader.reply_gate_probability == 0.0
        self.build_reader(reply_gate_probability="abc")
        assert self.reader.reply_gate_probability == 80.0
        self.build_reader(reply_gate_hourly_limit=-3)
        assert self.reader.reply_gate_hourly_limit == 0
        self.build_reader(reply_gate_hourly_limit="not-a-number")
        assert self.reader.reply_gate_hourly_limit == 0
        self.build_reader(reply_gate_enabled="false")
        assert self.reader.reply_gate_enabled is False
        self.build_reader(reply_gate_enabled="true")
        assert self.reader.reply_gate_enabled is True


# ===========================================================================
# 2. Probability gate
# ===========================================================================
class TestProbabilityGate(GateTestBase):
    async def test_disabled_gate_keeps_legacy_behaviour(self):
        """Gate off + 0% must behave exactly like the pre-v2.10 code path."""
        self.build_reader(reply_gate_enabled=False, reply_gate_probability=0)
        assert await self.evaluate() is True
        assert len(self.hub.calls) == 1

    async def test_probability_100_keeps_legacy_behaviour(self):
        self.build_reader(reply_gate_probability=100)
        assert await self.evaluate() is True
        assert len(self.hub.calls) == 1

    async def test_probability_0_never_calls_decision_model(self):
        self.build_reader(reply_gate_probability=0)
        assert await self.evaluate() is False
        assert self.hub.calls == []

    async def test_probability_sampling_drops_and_passes(self):
        self.build_reader(reply_gate_probability=50)
        with mock.patch(
            "OnlyYou.core.atmosphere_reader.random.random", return_value=0.9
        ):
            assert await self.evaluate() is False
        assert self.hub.calls == []
        with mock.patch(
            "OnlyYou.core.atmosphere_reader.random.random", return_value=0.1
        ):
            assert await self.evaluate() is True
        assert len(self.hub.calls) == 1

    async def test_gate_rejection_skips_debounce_window(self):
        self.build_reader(reply_gate_probability=0, debounce_seconds=2.0)
        with mock.patch(
            "OnlyYou.core.atmosphere_reader.asyncio.sleep",
            new=mock.AsyncMock(),
        ) as sleep_mock:
            assert await self.evaluate() is False
        sleep_mock.assert_not_awaited()


# ===========================================================================
# 3. Unaffected paths: @ / BUSY / private / takeover off
# ===========================================================================
class TestGateScopeBypasses(GateTestBase):
    async def test_direct_mention_bypasses_gate(self):
        self.build_reader(reply_gate_probability=0)
        event = _GroupEvent(at_bot=True)
        assert await self.evaluate(event=event, is_direct=True) is True
        assert self.hub.calls == []

    async def test_busy_l3_bypasses_gate(self):
        self.build_reader(reply_gate_probability=0)
        assert (
            await self.evaluate(slot=self.busy_slot, user=self.l3_user) is True
        )
        assert self.hub.calls == []

    async def test_busy_l1_l2_are_ignored_without_model_call(self):
        self.build_reader(reply_gate_probability=0)
        assert await self.evaluate(slot=self.busy_slot, user=self.user) is False
        assert await self.evaluate(slot=self.busy_slot, user=self.l2_user) is False
        assert self.hub.calls == []

    async def test_private_chat_bypasses_gate(self):
        self.build_reader(reply_gate_probability=0)
        assert await self.evaluate(event=_PrivateEvent()) is True
        assert self.hub.calls == []

    async def test_takeover_off_ignores_gate(self):
        self.build_reader(takeover_group_chat=False, reply_gate_probability=0)
        assert await self.evaluate() is False
        assert await self.evaluate(is_direct=True) is True
        assert self.hub.calls == []


# ===========================================================================
# 4. Rolling hourly budget
# ===========================================================================
class TestHourlyBudget(GateTestBase):
    async def test_hourly_limit_caps_passes_per_umo(self):
        self.build_reader(reply_gate_probability=100, reply_gate_hourly_limit=1)
        start = 1_800_000_000.0
        with mock.patch(
            "OnlyYou.core.atmosphere_reader.time.time", return_value=start
        ):
            assert await self.evaluate() is True
            assert await self.evaluate() is False
        assert len(self.hub.calls) == 1

        # A different group has its own budget.
        with mock.patch(
            "OnlyYou.core.atmosphere_reader.time.time", return_value=start
        ):
            assert (
                await self.evaluate(
                    event=_GroupEvent(umo="aiocqhttp:GroupMessage:g2")
                )
                is True
            )
        assert len(self.hub.calls) == 2

        # After one rolling hour the budget is reset.
        with mock.patch(
            "OnlyYou.core.atmosphere_reader.time.time", return_value=start + 3601
        ):
            assert await self.evaluate() is True
        assert len(self.hub.calls) == 3

    async def test_clear_gate_budget_reopens_slot(self):
        self.build_reader(reply_gate_probability=100, reply_gate_hourly_limit=1)
        start = 1_800_000_000.0
        with mock.patch(
            "OnlyYou.core.atmosphere_reader.time.time", return_value=start
        ):
            assert await self.evaluate() is True
            assert await self.evaluate() is False
            assert self.reader.clear_gate_budget(GROUP_UMO) == 1
            assert await self.evaluate() is True
        assert len(self.hub.calls) == 2

    async def test_clear_gate_budget_is_platform_scoped(self):
        """Soul unbind clears only that bot instance's rolling gate budgets."""
        self.build_reader(reply_gate_probability=100, reply_gate_hourly_limit=1)
        start = 1_800_000_000.0
        with mock.patch(
            "OnlyYou.core.atmosphere_reader.time.time", return_value=start
        ):
            assert await self.evaluate() is True
            assert (
                await self.evaluate(
                    event=_GroupEvent(umo="aiocqhttp:GroupMessage:g2")
                )
                is True
            )
            # Another platform's rows are untouched by the unbind cleanup.
            assert self.reader.clear_gate_budget(platform_id="telegram") == 0
            assert await self.evaluate() is False
            # Both aiocqhttp groups had a recorded pass -> both are cleared.
            assert self.reader.clear_gate_budget(platform_id="aiocqhttp") == 2
            assert await self.evaluate() is True
        assert len(self.hub.calls) == 3


# ===========================================================================
# 5. Plugin config schema contract
# ===========================================================================
class TestReplyGateSchema(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(
            os.path.join(PROJECT_ROOT, "_conf_schema.json"), "r", encoding="utf-8"
        ) as handle:
            cls.schema = json.load(handle)
        cls.items = cls.schema["atmosphere_config"]["items"]

    def test_gate_items_present_with_expected_defaults(self):
        assert self.items["reply_gate_enabled"]["type"] == "bool"
        assert self.items["reply_gate_enabled"]["default"] is True
        assert self.items["reply_gate_probability"]["type"] == "int"
        assert self.items["reply_gate_probability"]["default"] == 80
        assert self.items["reply_gate_hourly_limit"]["type"] == "int"
        assert self.items["reply_gate_hourly_limit"]["default"] == 0

    def test_probability_default_is_a_valid_percentage(self):
        default = self.items["reply_gate_probability"]["default"]
        assert 0 <= int(default) <= 100

    def test_every_gate_item_has_short_description_and_hint(self):
        for key in (
            "reply_gate_enabled",
            "reply_gate_probability",
            "reply_gate_hourly_limit",
        ):
            item = self.items[key]
            description = str(item.get("description") or "").strip()
            hint = str(item.get("hint") or "").strip()
            assert description, f"{key} 缺少 description"
            assert len(description) <= 24, f"{key} 标题过长"
            assert hint, f"{key} 缺少 hint"


if __name__ == "__main__":
    unittest.main()
