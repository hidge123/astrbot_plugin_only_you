"""v2.10 T1 offline tests: memory time annotations & time-stamped extraction windows.

Frozen contract (``docs/ARCHITECTURE.md`` §6.3):

1. ``core/time_utils.py`` renders local time / weekday / age / spans defensively
   (invalid or missing timestamps degrade to ``""`` and never raise).
2. The memory-extraction transcript handed to the decision model carries one
   ``[YYYY-MM-DD HH:MM]`` stamp per message plus a ``【窗口时间范围】`` header,
   and ``MEMORY_EXTRACT_AND_GRADE_PROMPT`` receives the current local time
   (weekday included) — so a cold window spanning several days can be located.
3. Memory content is stored **verbatim** (no forced absolute-date rewriting);
   each extracted memory keeps its formation anchor in ``created_at``.
4. Every recalled memory is injected as ``- [层级 · 形成于 2026-11-05（星期四，10 天前）]``
   followed by ``MEMORY_TIME_CONTEXT_PROMPT`` explaining how to un-freeze relative
   expressions and judge expiry; ``MemoryItem.to_dict()`` exposes readable times.
5. MEMORY_MUSE material also carries the memory formation time.

Stdlib ``unittest`` only, fully offline, temp DBs.
"""

import os
import sys
import time
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _helpers import (  # noqa: E402
    DatabaseAsyncTestCase,
    FakeEvent,
    FakeRequest,
    FakeSoulManager,
    StubMemoryManager,
    StubModelHub,
    load_plugin_modules,
    make_memory,
    make_plugin_config,
    make_run,
    make_slot,
    make_soul,
    make_user,
)
from test_regressions import (  # noqa: E402
    RecordingProactiveBridge,
    SequencedStubModelHub,
    bind_plugin_methods,
)

mods = load_plugin_modules()
time_utils = mods.time_utils
MemoryManager = mods.memory_manager.MemoryManager
MemoryTier = mods.memory_manager.MemoryTier
SessionBuffer = mods.context_budget.SessionBuffer
ContextBudgetManager = mods.context_budget.ContextBudgetManager
BotState = mods.state_machine.BotState
ActionType = mods.state_machine.ActionType

#: Fixed "now" used by the deterministic tests: 2026-11-15 21:30 (a Sunday).
NOW_DT = datetime(2026, 11, 15, 21, 30, 0)
NOW_TS = int(NOW_DT.timestamp())


def _ts(**delta):
    """Epoch seconds of ``NOW_DT`` shifted by ``timedelta`` kwargs."""
    return int((NOW_DT + timedelta(**delta)).timestamp())


# ===========================================================================
# 1. core/time_utils.py
# ===========================================================================
class TestTimeUtils(unittest.TestCase):
    def test_format_local_datetime_and_date(self):
        assert time_utils.format_local_datetime(NOW_TS) == "2026-11-15 21:30"
        assert time_utils.format_local_date(NOW_TS) == "2026-11-15"
        assert time_utils.weekday_label(NOW_TS) == "星期日"
        assert time_utils.format_transcript_stamp(_ts(days=-3)) == "2026-11-12 21:30"

    def test_invalid_timestamps_degrade_to_empty_strings(self):
        for value in (None, 0, -1, "", "abc", [], {}):
            assert time_utils.format_local_datetime(value) == ""
            assert time_utils.format_local_date(value) == ""
            assert time_utils.weekday_label(value) == ""
            assert time_utils.format_transcript_stamp(value) == ""
            assert time_utils.describe_time_anchor(value, NOW_TS) == ""

    def test_describe_time_anchor_ages(self):
        assert (
            time_utils.describe_time_anchor(NOW_TS, NOW_TS)
            == "2026-11-15（星期日，今天）"
        )
        assert (
            time_utils.describe_time_anchor(_ts(days=-1), NOW_TS)
            == "2026-11-14（星期六，昨天）"
        )
        assert (
            time_utils.describe_time_anchor(_ts(days=-10), NOW_TS)
            == "2026-11-05（星期四，10 天前）"
        )
        # Clock skew / future timestamps must not fabricate a negative age.
        assert "未来" in time_utils.describe_time_anchor(_ts(days=2), NOW_TS)
        # ``now_ts`` omitted -> real clock, but the date part is still rendered.
        assert time_utils.describe_time_anchor(NOW_TS).startswith("2026-11-15（星期日")

    def test_current_datetime_text_includes_weekday(self):
        assert time_utils.current_datetime_text(NOW_TS) == "2026-11-15（星期日）21:30"
        # No now_ts -> real clock, but the shape is stable.
        assert "（星期" in time_utils.current_datetime_text()

    def test_describe_time_span(self):
        same_day = time_utils.describe_time_span(_ts(hours=-3), NOW_TS)
        assert "2026-11-15 18:30 ~ 2026-11-15 21:30" in same_day
        assert "（同日）" in same_day

        multi_day = time_utils.describe_time_span(_ts(days=-6), NOW_TS)
        assert "2026-11-09 21:30 ~ 2026-11-15 21:30" in multi_day
        assert "跨度 6 天" in multi_day

        # Reversed arguments are normalised instead of rendering a negative span.
        reversed_span = time_utils.describe_time_span(NOW_TS, _ts(days=-6))
        assert "跨度 6 天" in reversed_span
        assert time_utils.describe_time_span(None, NOW_TS) == "时间未知"


# ===========================================================================
# 2. SessionBuffer time-stamped transcripts
# ===========================================================================
class TestTranscriptTimeStamps(unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.config = make_plugin_config()
        self.buffer = SessionBuffer(self.config)
        self.user = make_user("u-1", "阿一")

    def test_format_for_transcript_keeps_default_shape(self):
        """Backward compatibility: every pre-v2.10 prompt must stay unchanged."""
        message = self.buffer.record_user_message("umo-1", self.user, "第一句")
        assert message is not None
        assert message.format_for_transcript() == "[L1普通群友 阿一(ID:u-1)]: 第一句"
        timed = message.format_for_transcript(with_time=True)
        assert timed.startswith("[")
        assert "]: 第一句" in timed
        assert time_utils.format_transcript_stamp(message.timestamp) in timed

    def test_extraction_transcript_carries_window_header_and_per_line_stamps(self):
        first = self.buffer.record_user_message("umo-1", self.user, "这周末去考试吗")
        second = self.buffer.record_user_message("umo-1", self.user, "去的")
        assert first is not None and second is not None
        first.timestamp = _ts(days=-6)
        second.timestamp = NOW_TS

        text = self.buffer.get_extraction_transcript("umo-1")
        assert text.startswith("【窗口时间范围】")
        assert "跨度 6 天" in text
        assert "共 2 条消息" in text
        assert f"[{time_utils.format_transcript_stamp(first.timestamp)}]" in text
        assert f"[{time_utils.format_transcript_stamp(second.timestamp)}]" in text
        assert "这周末去考试吗" in text

    def test_assistant_messages_are_stamped_too(self):
        self.buffer.record_assistant_message("umo-1", "我也想去", soul_name="星野")
        message = self.buffer._windows["umo-1"][-1]
        message.timestamp = NOW_TS
        live_text = self.buffer.get_extraction_transcript("umo-1")
        assert f"[2026-11-15 21:30] [星野(Bot)]: 我也想去" in live_text
        # The legacy (untimed) atmosphere transcript remains unchanged.
        assert self.buffer.get_window_transcript("umo-1") == "[星野(Bot)]: 我也想去"

    def test_empty_window_returns_placeholder(self):
        assert self.buffer.get_extraction_transcript("umo-none") == "（暂无近期对话）"
        assert self.buffer.format_extraction_transcript([]) == "（暂无近期对话）"


# ===========================================================================
# 3. Memory extraction prompt + verbatim storage
# ===========================================================================
class TestExtractionPromptTimeBase(DatabaseAsyncTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.config = make_plugin_config()
        self.buffer = SessionBuffer(self.config)
        self.user = make_user("u-1", "阿一")

    def _transcript(self):
        first = self.buffer.record_user_message("umo-1", self.user, "阿泽说这周末要去考科目二")
        second = self.buffer.record_user_message("umo-1", self.user, "他有点紧张")
        assert first is not None and second is not None
        first.timestamp = _ts(days=-2)
        second.timestamp = NOW_TS
        return self.buffer.get_extraction_transcript("umo-1")

    async def test_prompt_carries_current_time_and_per_message_dates(self):
        transcript = self._transcript()
        hub = StubModelHub(
            json_result=[
                {
                    "user_id": "u-1",
                    "user_tier": 2,
                    "memory_tier": 2,
                    "importance": 6,
                    "content": "阿泽说这周末要去考科目二",
                    "keywords": "科目二,周末,考试",
                }
            ]
        )
        manager = MemoryManager(self.db, hub, self.config)

        count = await manager.extract_memories_from_transcript(
            soul_id="soul-a",
            soul_name="星野",
            transcript_text=transcript,
            now_ts=NOW_TS,
        )
        assert count == 1
        prompt = hub.calls[0]["prompt"]
        # TODO T1 acceptance #2: current date/time + window date range + each message day.
        assert f"当前真实时间：{time_utils.current_datetime_text(NOW_TS)}" in prompt
        assert "【窗口时间范围】" in prompt
        assert "跨度 2 天" in prompt
        assert time_utils.format_transcript_stamp(_ts(days=-2)) in prompt
        # The prompt explains what the timestamps are for, without forcing a rewrite.
        assert "真实发送时间" in prompt
        assert "50 字以内" in prompt

    async def test_memory_content_is_stored_verbatim_and_anchored(self):
        transcript = self._transcript()
        relative_content = "阿泽说这周末要去考科目二"
        hub = StubModelHub(
            json_result=[
                {
                    "user_id": "u-1",
                    "user_tier": 2,
                    "memory_tier": 2,
                    "importance": 6,
                    "content": relative_content,
                    "keywords": "科目二",
                }
            ]
        )
        manager = MemoryManager(self.db, hub, self.config)

        await manager.extract_memories_from_transcript(
            soul_id="soul-a",
            soul_name="星野",
            transcript_text=transcript,
            now_ts=NOW_TS,
        )
        row = await self.db.fetchone("SELECT * FROM memories")
        # No forced absolute-date rewriting: the model's own wording is preserved.
        assert row["content"] == relative_content
        # The formation anchor is the injected extraction time.
        assert int(row["created_at"]) == NOW_TS
        assert int(row["last_accessed_at"]) == NOW_TS

    async def test_long_content_is_truncated_to_fifty_chars(self):
        manager = MemoryManager(
            self.db,
            StubModelHub(
                json_result=[
                    {
                        "user_tier": 3,
                        "memory_tier": 3,
                        "importance": 9,
                        "content": "超" * 80,
                        "keywords": "长",
                    }
                ]
            ),
            self.config,
        )
        count = await manager.extract_memories_from_transcript(
            soul_id="soul-a",
            soul_name="星野",
            transcript_text="（暂无近期对话）",
            now_ts=NOW_TS,
        )
        assert count == 1
        row = await self.db.fetchone("SELECT * FROM memories")
        assert len(row["content"]) == 50

    async def test_repeated_extraction_still_dedupes_tier1(self):
        hub = StubModelHub(
            json_result=[
                {
                    "user_tier": 2,
                    "memory_tier": 1,
                    "importance": 4,
                    "content": "重复的短时动态",
                    "keywords": "动态",
                }
            ]
        )
        manager = MemoryManager(self.db, hub, self.config)
        for _ in range(2):
            count = await manager.extract_memories_from_transcript(
                soul_id="soul-a",
                soul_name="星野",
                transcript_text="（暂无近期对话）",
                now_ts=NOW_TS,
            )
            assert count == 1
        rows = await self.db.fetchall("SELECT * FROM memories WHERE memory_tier = 1")
        assert len(rows) == 1


# ===========================================================================
# 4. MemoryItem.to_dict() time text
# ===========================================================================
class TestMemoryItemTimeText(unittest.TestCase):
    def test_to_dict_exposes_readable_times(self):
        item = make_memory(id=7, created_at=NOW_TS)
        payload = item.to_dict()
        assert payload["created_at"] == NOW_TS
        assert payload["created_at_text"] == "2026-11-15 21:30"
        assert payload["last_accessed_at"] is None
        assert payload["last_accessed_at_text"] == ""

    def test_invalid_created_at_degrades_to_empty_text(self):
        item = make_memory(id=8, created_at=0)
        payload = item.to_dict()
        assert payload["created_at_text"] == ""


# ===========================================================================
# 5. ContextBudgetManager memory injection time block
# ===========================================================================
class TestMemoryInjectionTimeAnchor(DatabaseAsyncTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.config = make_plugin_config()
        self.soul = make_soul("soul-a")
        self.user = make_user("user-1", "阿一")

    def _build_budget(self, memories=None):
        manager = StubMemoryManager(memories or [])
        buffer = SessionBuffer(self.config)
        budget = ContextBudgetManager(
            memory_manager=manager,
            session_buffer=buffer,
            plugin_config=self.config,
            soul_manager=FakeSoulManager([]),
        )
        return budget, buffer, manager

    async def _apply(self, budget):
        req = FakeRequest(prompt="还记得周末的事吗", system_prompt="BASE")
        await budget.apply_context_and_memory_budget(
            event=FakeEvent(message_str="还记得周末的事吗", umo="umo-1"),
            req=req,
            soul=self.soul,
            user=self.user,
            run=make_run(),
            current_slot=make_slot(state=BotState.IDLE),
        )
        return req

    async def test_recalled_memory_is_annotated_with_formation_time(self):
        created_ts = _ts(days=-10)
        memory = make_memory(
            id=1,
            soul_id=self.soul.soul_id,
            content="阿泽说这周末要去考科目二",
            keywords="科目二",
            importance=9,
            memory_tier=MemoryTier.CORE,
            user_tier=3,
            created_at=created_ts,
        )
        budget, _buffer, _manager = self._build_budget([memory])

        with mock.patch(
            "OnlyYou.core.context_budget.time.time", return_value=float(NOW_TS)
        ):
            req = await self._apply(budget)

        prompt = req.system_prompt
        anchor = time_utils.describe_time_anchor(created_ts, NOW_TS)
        assert "[记忆时间基准]" in prompt
        assert f"今天是 {time_utils.current_datetime_text(NOW_TS)}" in prompt
        assert f"- [核心记忆 · 形成于 {anchor}] 阿泽说这周末要去考科目二" in prompt
        # Relative wording is preserved; the model resolves it from the anchor.
        assert "这周末" in prompt
        assert "历史信息" in prompt and "未来安排" in prompt
        # The hard char budget keeps counting memory content only.
        assert req.system_prompt.count("[记忆时间基准]") == 1

    async def test_budget_character_count_ignores_time_labels(self):
        memory = make_memory(
            id=2,
            soul_id=self.soul.soul_id,
            content="一二三四五六七八九十",
            importance=8,
            memory_tier=MemoryTier.RECENT,
            created_at=_ts(days=-3),
        )
        budget, _buffer, _manager = self._build_budget([memory])
        with mock.patch(
            "OnlyYou.core.context_budget.time.time", return_value=float(NOW_TS)
        ):
            result = await budget.apply_context_and_memory_budget(
                event=FakeEvent(message_str="随便聊聊", umo="umo-1"),
                req=FakeRequest(prompt="随便聊聊", system_prompt="BASE"),
                soul=self.soul,
                user=self.user,
                run=make_run(),
                current_slot=make_slot(state=BotState.IDLE),
            )
        assert result["injected_chars"] == len("一二三四五六七八九十")

    async def test_no_memory_means_no_time_block(self):
        budget, _buffer, _manager = self._build_budget([])
        req = await self._apply(budget)
        assert "[记忆时间基准]" not in req.system_prompt
        assert "脑海中的相关记忆" not in req.system_prompt


# ===========================================================================
# 6. MEMORY_MUSE material carries the memory formation time
# ===========================================================================
class TestMuseTimeLabel(DatabaseAsyncTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.config = make_plugin_config()
        self.soul = make_soul("soul-muse")
        self.memory_manager = MemoryManager(self.db, StubModelHub(), self.config)
        self.session_buffer = SessionBuffer(self.config)
        self.current_slot = make_slot(
            state=BotState.IDLE,
            action_type=ActionType.IDLE_ROAM,
            title="趴在窗边发呆",
        )
        self.run = make_run(self.soul.soul_id)

    def _make_fake(self, json_results):
        bridge = RecordingProactiveBridge()
        fake = SimpleNamespace(
            config=self.config,
            model_hub=SequencedStubModelHub(json_results=json_results),
            memory_manager=self.memory_manager,
            session_buffer=self.session_buffer,
            platform_bridge=bridge,
            _known_group_umos=set(),
            _known_l3_umos={"core-uid": "aiocqhttp:FriendMessage:l3"},
            _last_idle_roam_ts={},
            _last_group_lurk_ts={},
            _last_private_catchup_ts={},
        )
        return bind_plugin_methods(fake), bridge

    async def test_muse_prompt_contains_formation_anchor(self):
        created_ts = _ts(days=-3)
        await self.memory_manager.add_memory(
            soul_id=self.soul.soul_id,
            content="老王答应周五带大家开黑",
            keywords="开黑",
            importance=6,
            memory_tier=MemoryTier.RECENT,
            created_at=created_ts,
        )
        fake, bridge = self._make_fake(
            [{"target": "PRIVATE_L3", "message": "突然想起老王说要开黑"}]
        )
        sent = await fake._run_idle_roam_action(
            self.soul, self.run, self.current_slot
        )
        assert sent is True
        assert bridge.sent
        prompt = fake.model_hub.calls[0]["prompt"]
        # The MUSE material annotates the formation time as the relative-time anchor.
        assert "【记忆 · " in prompt
        assert time_utils.format_local_date(created_ts) in prompt
        assert "3 天前" in prompt
        assert "老王答应周五带大家开黑" in prompt


if __name__ == "__main__":
    unittest.main()
