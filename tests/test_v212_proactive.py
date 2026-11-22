"""v2.12 主动对话（IDLE_ROAM）节奏与反反复读回归。

冻结契约：

1. 冷却（默认 120min）+ 概率门闸（默认 35%）+ 每日上限（默认 3）；
2. 话题模式按权重选择：默认 70% PERSONA_TOPIC / 30% MEMORY_MUSE；
3. MEMORY_MUSE 抽不到未冷却记忆时**不得**回退 ``list_memories()`` 全量硬抽，
   必须无缝切到 PERSONA_TOPIC；
4. 最近 5 条主动消息进入反反复读负面约束（话题 + 句首 / 句尾用字）；
5. ``recent_proactive_messages`` / ``idle_roam_daily_counts`` 随 runtime_state
   持久化，重启不重置；
6. 提炼失败时 SessionBuffer 只保留 1 批重试，且后台任务有强引用。
"""

import unittest
from types import SimpleNamespace

from _helpers import (
    DatabaseAsyncTestCase,
    StubModelHub,
    load_plugin_modules,
    make_plugin_config,
    make_run,
    make_slot,
    make_soul,
    make_user,
)

mods = load_plugin_modules()
ActionType = mods.state_machine.ActionType
BotState = mods.state_machine.BotState
SessionBuffer = mods.context_budget.SessionBuffer
MemoryManager = mods.memory_manager.MemoryManager
RuntimeStateStore = mods.runtime_state.RuntimeStateStore
RUNTIME_STATE_SETTING_KEY = mods.runtime_state.RUNTIME_STATE_SETTING_KEY


class RecordingProactiveBridge:
    def __init__(self, ok=True):
        self.sent = []
        self.ok = ok

    def apply_reply_decision(self, event, should_reply=False):
        pass

    async def send_proactive_message(
        self, umo, text, session_buffer=None, soul_name="Bot", audio_path=None,
        on_overflow_callback=None,
    ):
        if not self.ok:
            return False
        self.sent.append((umo, text))
        return True


class QueuedHub(StubModelHub):
    def __init__(self, json_results=None):
        super().__init__()
        self.json_results = list(json_results or [])

    async def call_llm_json(
        self, prompt="", role="main", umo=None, default=None, soul_id=None, **kwargs
    ):
        self.calls.append({"prompt": prompt, "role": role, "umo": umo})
        if self.json_results:
            return self.json_results.pop(0)
        return default


def bind_plugin_methods(fake, names):
    from OnlyYou.main import OnlyYouPlugin
    import inspect
    from types import MethodType

    for name in names:
        raw = inspect.getattr_static(OnlyYouPlugin, name)
        if isinstance(raw, staticmethod):
            setattr(fake, name, raw.__func__)
        else:
            setattr(fake, name, MethodType(raw, fake))
    return fake


_BOUND = (
    "_run_idle_roam_action",
    "_idle_roam_cooldown_seconds",
    "_idle_roam_trigger_probability",
    "_idle_roam_daily_max",
    "_idle_roam_persona_ratio",
    "_idle_roam_day_key",
    "_idle_roam_daily_count",
    "_record_idle_roam_sent",
    "_anti_repeat_block",
    "_unused_busy_results",
    "_sample_muse_memory",
    "_roguelite_setting",
    "_group_lurk_cooldown_seconds",
    "_private_catchup_cooldown_seconds",
    "_unread_private_reply_enabled",
    "_pick_group_unread_candidate",
    "_decide_group_unread_reply",
    "_resolve_idle_roam_target",
    "_extract_idle_roam_message",
    "_latest_l3_umo",
    "_latest_allowed_l3_umo",
    "_umo_is_group",
    "_uid_for_private_umo",
    "_group_proactive_enabled",
    "_user_proactive_enabled",
    "_proactive_target_allowed",
    "_maybe_synthesize_proactive",
    "_render_soul_summary",
)


class ProactiveTestBase(DatabaseAsyncTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.soul = make_soul("soul-proactive")
        self.current_slot = make_slot(
            state=BotState.IDLE,
            action_type=ActionType.IDLE_ROAM,
            title="趴在窗边发呆",
        )
        self.run = make_run(self.soul.soul_id)
        self.hub = QueuedHub()
        self.bridge = RecordingProactiveBridge()
        self.memory_manager = MemoryManager(self.db, StubModelHub(), make_plugin_config())
        self.session_buffer = SessionBuffer(make_plugin_config())

    def make_fake(self, **roguelite):
        config = make_plugin_config(roguelite_schedule_config=roguelite)
        self.memory_manager.plugin_config = config
        self.session_buffer.plugin_config = config
        fake = SimpleNamespace(
            config=config,
            model_hub=self.hub,
            memory_manager=self.memory_manager,
            session_buffer=self.session_buffer,
            platform_bridge=self.bridge,
            _known_group_umos=set(),
            _known_l3_umos={"core-uid": "aiocqhttp:FriendMessage:l3"},
            _last_idle_roam_ts={},
            _last_group_lurk_ts={},
            _last_private_catchup_ts={},
            _recent_proactive_messages={},
            _idle_roam_daily_counts={},
            _mused_busy_keys={},
            _mused_memory_ids={},
        )
        return bind_plugin_methods(fake, _BOUND)


class TestProactivePace(ProactiveTestBase):
    async def test_probability_zero_never_calls_llm_or_sends(self):
        fake = self.make_fake(
            idle_roam_cooldown_minutes=0,
            idle_roam_trigger_probability=0,
            idle_roam_persona_topic_ratio=100,
        )
        sent = await fake._run_idle_roam_action(self.soul, self.run, self.current_slot)
        assert sent is False
        assert self.hub.calls == []
        assert self.bridge.sent == []

    async def test_persona_topic_sends_and_records_daily_budget(self):
        self.hub.json_results = [{"target": "PRIVATE_L3", "message": "刚泡了杯乌龙茶"}]
        fake = self.make_fake(
            idle_roam_cooldown_minutes=0,
            idle_roam_trigger_probability=100,
            idle_roam_persona_topic_ratio=100,
            idle_roam_daily_max_count=0,
        )
        sent = await fake._run_idle_roam_action(self.soul, self.run, self.current_slot)
        assert sent is True
        assert self.bridge.sent[0][0] == "aiocqhttp:FriendMessage:l3"
        assert "本次只从下面这一个灵感切面出发" in self.hub.calls[0]["prompt"]
        assert fake._idle_roam_daily_count(self.soul.soul_id) == 1
        assert fake._recent_proactive_messages[self.soul.soul_id] == ["刚泡了杯乌龙茶"]

    async def test_daily_cap_blocks_further_messages(self):
        self.hub.json_results = [
            {"target": "PRIVATE_L3", "message": "第一条主动消息"},
            {"target": "PRIVATE_L3", "message": "第二条不应发出"},
        ]
        fake = self.make_fake(
            idle_roam_cooldown_minutes=0,
            idle_roam_trigger_probability=100,
            idle_roam_persona_topic_ratio=100,
            idle_roam_daily_max_count=1,
        )
        assert (
            await fake._run_idle_roam_action(self.soul, self.run, self.current_slot)
            is True
        )
        calls_before = len(self.hub.calls)
        assert (
            await fake._run_idle_roam_action(self.soul, self.run, self.current_slot)
            is False
        )
        assert len(self.hub.calls) == calls_before
        assert len(self.bridge.sent) == 1

    async def test_cooldown_blocks_second_call(self):
        self.hub.json_results = [
            {"target": "PRIVATE_L3", "message": "第一条"},
            {"target": "PRIVATE_L3", "message": "第二条"},
        ]
        fake = self.make_fake(
            idle_roam_cooldown_minutes=45,
            idle_roam_trigger_probability=100,
            idle_roam_persona_topic_ratio=100,
            idle_roam_daily_max_count=0,
        )
        assert await fake._run_idle_roam_action(self.soul, self.run, self.current_slot) is True
        calls_before = len(self.hub.calls)
        assert await fake._run_idle_roam_action(self.soul, self.run, self.current_slot) is False
        assert len(self.hub.calls) == calls_before


class TestPersonaTopic(ProactiveTestBase):
    async def test_busy_result_is_used_at_most_once(self):
        busy_slot = self.run.slots[2]
        assert busy_slot.action == ActionType.BUSY_TASK
        busy_slot.busy_settled = True
        busy_slot.completion_result = "今天把回春丹炼成了，差点炸炉"
        self.hub.json_results = [
            {"target": "PRIVATE_L3", "message": "今天炼丹差点炸炉笑死"},
            {"target": "PRIVATE_L3", "message": "晚上想吃点热的"},
        ]
        fake = self.make_fake(
            idle_roam_cooldown_minutes=0,
            idle_roam_trigger_probability=100,
            idle_roam_persona_topic_ratio=100,
            idle_roam_daily_max_count=0,
        )
        assert await fake._run_idle_roam_action(self.soul, self.run, self.current_slot) is True
        assert "回春丹" in self.hub.calls[0]["prompt"]
        assert busy_slot.slot_key in fake._mused_busy_keys[self.soul.soul_id]
        assert await fake._run_idle_roam_action(self.soul, self.run, self.current_slot) is True
        assert "回春丹" not in self.hub.calls[1]["prompt"]

    async def test_anti_repeat_block_lists_recent_messages_and_bans_chars(self):
        fake = self.make_fake(
            idle_roam_cooldown_minutes=0,
            idle_roam_trigger_probability=100,
            idle_roam_persona_topic_ratio=100,
            idle_roam_daily_max_count=0,
        )
        fake._recent_proactive_messages[self.soul.soul_id] = [
            "草，又想起你那天聊骚",
            "两天前发的那个太乐了",
            "笑死，BLG 夺冠",
            "你欠我一顿火锅",
            "晚上别熬太晚",
        ]
        expected_messages = list(fake._recent_proactive_messages[self.soul.soul_id])
        self.hub.json_results = [{"target": "PRIVATE_L3", "message": "窗外下雨了"}]
        assert await fake._run_idle_roam_action(self.soul, self.run, self.current_slot) is True
        prompt = self.hub.calls[0]["prompt"]
        assert "你最近 5 条主动消息" in prompt
        assert "严禁" in prompt
        for message in expected_messages:
            assert message in prompt
        assert "用这些字开头" in prompt
        # the brand-new message joins the ring (and the oldest one is evicted)
        assert len(fake._recent_proactive_messages[self.soul.soul_id]) == 5
        assert fake._recent_proactive_messages[self.soul.soul_id][-1] == "窗外下雨了"

    async def test_ring_keeps_only_five(self):
        fake = self.make_fake()
        for index in range(7):
            fake._record_idle_roam_sent(self.soul.soul_id, f"主动消息{index}")
        assert len(fake._recent_proactive_messages[self.soul.soul_id]) == 5
        assert fake._recent_proactive_messages[self.soul.soul_id][0] == "主动消息2"


class TestMemoryMuseFallback(ProactiveTestBase):
    async def test_no_memory_falls_back_to_persona_without_full_list_sweep(self):
        fake = self.make_fake(
            idle_roam_cooldown_minutes=0,
            idle_roam_trigger_probability=100,
            idle_roam_persona_topic_ratio=0,  # force MEMORY_MUSE first
            idle_roam_daily_max_count=0,
        )

        async def _explode(*args, **kwargs):
            raise AssertionError("list_memories fallback must never be used")

        self.memory_manager.list_memories = _explode  # type: ignore[assignment]
        self.hub.json_results = [{"target": "PRIVATE_L3", "message": "发会儿呆"}]
        sent = await fake._run_idle_roam_action(self.soul, self.run, self.current_slot)
        assert sent is True
        assert "本次只从下面这一个灵感切面出发" in self.hub.calls[0]["prompt"]
        assert "MEMORY_MUSE" not in self.hub.calls[0]["prompt"]

    async def test_cooled_down_memory_is_used_once(self):
        await self.memory_manager.add_memory(
            soul_id=self.soul.soul_id,
            content="阿泽上周去考了科目二",
            keywords="科目二",
            importance=6,
            memory_tier=2,
        )
        fake = self.make_fake(
            idle_roam_cooldown_minutes=0,
            idle_roam_trigger_probability=100,
            idle_roam_persona_topic_ratio=0,
            idle_roam_daily_max_count=0,
        )
        self.hub.json_results = [
            {"target": "PRIVATE_L3", "message": "突然想起你考科目二的事"},
            {"target": "PRIVATE_L3", "message": "换个人设话题"},
        ]
        assert await fake._run_idle_roam_action(self.soul, self.run, self.current_slot) is True
        assert "MEMORY_MUSE" in self.hub.calls[0]["prompt"]
        assert "科目二" in self.hub.calls[0]["prompt"]
        assert fake._mused_memory_ids[self.soul.soul_id]
        # second tick: the only memory is in the rotation ring -> PERSONA_TOPIC
        assert await fake._run_idle_roam_action(self.soul, self.run, self.current_slot) is True
        assert "MEMORY_MUSE" not in self.hub.calls[1]["prompt"]


class TestRuntimeStateMaps(DatabaseAsyncTestCase):
    async def test_new_maps_roundtrip_and_are_trimmed(self):
        store = RuntimeStateStore(self.db)
        await store.save(
            {
                "recent_proactive_messages": {
                    "soul-a": [f"消息{index}" for index in range(8)],
                },
                "idle_roam_daily_counts": {
                    "soul-a": {"date": "2026-11-17", "count": 2},
                    "soul-b": {"date": "", "count": 5},
                },
            }
        )
        snapshot = await store.load()
        assert snapshot["recent_proactive_messages"]["soul-a"] == [
            f"消息{index}" for index in range(3, 8)
        ]
        assert snapshot["idle_roam_daily_counts"] == {
            "soul-a": {"date": "2026-11-17", "count": 2}
        }


class TestExtractionRetryBuffer(unittest.IsolatedAsyncioTestCase):
    async def test_failed_batch_is_kept_once_and_retried(self):
        config = make_plugin_config(
            memory_and_context_config={
                "sliding_window_size": 3,
                "extract_every_n_messages": 3,
            }
        )
        buffer = SessionBuffer(config)
        user = make_user("u-1", "阿一")
        transcripts = []

        async def failing(umo, transcript):
            transcripts.append(("F", transcript))
            return False

        for index in range(6):
            buffer.record_user_message(
                "umo-1", user, f"失败批次{index}", on_overflow_callback=failing
            )
        # let the background task run
        for _ in range(5):
            await _drain()
        assert transcripts, "extraction callback should have been scheduled"
        assert "失败批次0" in transcripts[0][1]
        assert buffer._retry_batches["umo-1"]

        async def succeeding(umo, transcript):
            transcripts.append(("S", transcript))
            return True

        for index in range(9):
            buffer.record_user_message(
                "umo-1", user, f"新批次{index}", on_overflow_callback=succeeding
            )
        for _ in range(5):
            await _drain()
        # The failed batch is prepended to the next extraction round and re-sent once.
        retried = [text for kind, text in transcripts if kind == "S"]
        assert retried, transcripts
        assert any("失败批次0" in text for text in retried), transcripts
        assert any("新批次5" in text for text in retried), transcripts
        assert not buffer._retry_batches
        assert buffer._overflow_tasks in (set(),) or all(
            task.done() for task in buffer._overflow_tasks
        )


async def _drain():
    import asyncio

    await asyncio.sleep(0.02)


if __name__ == "__main__":
    unittest.main()
