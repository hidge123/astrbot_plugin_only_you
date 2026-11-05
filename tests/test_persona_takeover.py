"""Offline tests for the Persona-section takeover in ``core/context_budget.py`` (§3.3).

Three branches are covered:
1. exact replacement of ``\\n# Persona Instructions\\n\\n{native_prompt}\\n``;
2. regex fallback from the ``# Persona Instructions`` heading to the next heading;
3. prepending the rendered character-card block when no Persona section exists.

It also verifies that inside ``apply_context_and_memory_budget`` the takeover happens
**before** the BUSY/IDLE injection, that ``req.contexts`` is cleared and refilled with
the plugin sliding window, and that the current prompt is de-duplicated.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _helpers import (  # noqa: E402
    FakeEvent,
    FakeRequest,
    FakeSoulManager,
    StubMemoryManager,
    card_with,
    load_plugin_modules,
    make_plugin_config,
    make_run,
    make_slot,
    make_soul,
    make_user,
)

mods = load_plugin_modules()
ContextBudgetManager = mods.context_budget.ContextBudgetManager
SessionBuffer = mods.context_budget.SessionBuffer
BotState = mods.state_machine.BotState
UserTier = mods.state_machine.UserTier

PERSONA_HEADING = "# Persona Instructions"
PERSONA_START = "你正在扮演「"


def card():
    return card_with(
        identity={"name": "星野", "nickname": "小星", "tagline": "爱上网冲浪的邻家女孩"},
        personality={"personality_tags": ["嘴硬心软", "慢热"]},
        speech={"speaking_style": "口语化短句，爱吐槽", "reply_length": "short"},
    )


class PersonaTakeoverTestBase(unittest.IsolatedAsyncioTestCase):
    def build(self, native_prompts=None, config=None, memories=None):
        self.config = config or make_plugin_config()
        self.soul_manager = FakeSoulManager(native_prompts or [])
        self.memory_manager = StubMemoryManager(memories or [])
        self.session_buffer = SessionBuffer(self.config)
        self.budget = ContextBudgetManager(
            memory_manager=self.memory_manager,
            session_buffer=self.session_buffer,
            plugin_config=self.config,
            soul_manager=self.soul_manager,
        )
        self.soul = make_soul(card=card())
        return self.budget


class TestTakeoverBranches(PersonaTakeoverTestBase):
    async def test_exact_native_persona_segment_replaced(self):
        budget = self.build(native_prompts=["原生人设A"])
        system_prompt = (
            "You are AstrBot.\n\n# Persona Instructions\n\n原生人设A\n"
        )
        req = FakeRequest(system_prompt=system_prompt)
        await budget._takeover_persona(req, self.soul)

        assert "原生人设A" not in req.system_prompt
        assert req.system_prompt.startswith("You are AstrBot.")
        assert PERSONA_HEADING in req.system_prompt
        assert PERSONA_START in req.system_prompt
        # rendered card prompt is inside the (single) replaced block
        assert req.system_prompt.count(PERSONA_HEADING) == 1

    async def test_exact_match_prefers_longest_known_prompt(self):
        budget = self.build(native_prompts=["原生人设A", "原生人设A-更长版本"])
        system_prompt = (
            "\n# Persona Instructions\n\n原生人设A-更长版本\n"
        )
        req = FakeRequest(system_prompt=system_prompt)
        await budget._takeover_persona(req, self.soul)
        assert "原生人设A-更长版本" not in req.system_prompt
        assert PERSONA_START in req.system_prompt

    async def test_regex_fallback_replaces_unknown_persona_section(self):
        budget = self.build(native_prompts=["完全对不上的原生人设"])
        system_prompt = (
            "Base Instructions\n\n"
            "# Persona Instructions\n\n"
            "某个未知的原生人设内容\n\n"
            "# Other Section\n"
            "keep me please"
        )
        req = FakeRequest(system_prompt=system_prompt)
        await budget._takeover_persona(req, self.soul)

        assert "某个未知的原生人设内容" not in req.system_prompt
        assert "keep me please" in req.system_prompt
        assert "Base Instructions" in req.system_prompt
        assert PERSONA_START in req.system_prompt

    async def test_regex_fallback_to_end_of_prompt(self):
        budget = self.build(native_prompts=[])
        system_prompt = "Base\n\n# Persona Instructions\n\n未知原生人设（文件末尾）"
        req = FakeRequest(system_prompt=system_prompt)
        await budget._takeover_persona(req, self.soul)
        assert "未知原生人设（文件末尾）" not in req.system_prompt
        assert PERSONA_START in req.system_prompt
        assert "Base" in req.system_prompt

    async def test_prepends_card_when_no_persona_section(self):
        budget = self.build(native_prompts=["原生人设A"])
        original = "You are a helpful assistant."
        req = FakeRequest(system_prompt=original)
        await budget._takeover_persona(req, self.soul)

        assert req.system_prompt.startswith("\n" + PERSONA_HEADING + "\n\n")
        assert req.system_prompt.endswith(original)
        assert PERSONA_START in req.system_prompt

    async def test_empty_system_prompt_gets_card_prepended(self):
        budget = self.build(native_prompts=[])
        req = FakeRequest(system_prompt="")
        await budget._takeover_persona(req, self.soul)
        assert PERSONA_START in req.system_prompt
        assert req.system_prompt.startswith("\n" + PERSONA_HEADING)

    async def test_soul_none_or_invalid_card_is_noop(self):
        budget = self.build(native_prompts=["原生人设A"])
        req = FakeRequest(system_prompt="原始内容")
        await budget._takeover_persona(req, None)
        assert req.system_prompt == "原始内容"

        broken = type("BrokenSoul", (), {"card": None})()
        await budget._takeover_persona(req, broken)
        assert req.system_prompt == "原始内容"

    async def test_list_native_prompts_failure_is_swallowed(self):
        budget = self.build(native_prompts=[])

        async def boom():
            raise RuntimeError("persona manager exploded")

        budget.soul_manager.list_native_prompts = boom
        req = FakeRequest(system_prompt="Base only")
        await budget._takeover_persona(req, self.soul)  # must not raise
        assert PERSONA_START in req.system_prompt


class TestTakeoverOrderingAndContext(PersonaTakeoverTestBase):
    async def test_takeover_happens_before_state_injection_and_contexts_are_replaced(self):
        budget = self.build(native_prompts=["原生人设A"])
        user = make_user("user-1", "阿一", tier=UserTier.L2_IMPORTANT)

        # sliding window: one historical message + the current incoming message
        self.session_buffer.record_user_message("umo-1", user, "第一句历史消息")
        self.session_buffer.record_user_message("umo-1", user, "当前问题")

        current_slot = make_slot(
            slot_key="forenoon",
            label="上午正事",
            start_hour=9,
            end_hour=12,
            state=BotState.BUSY,
            armor=60,
            title="苦逼赶稿",
        )
        run = make_run()

        event = FakeEvent(message_str="当前问题", umo="umo-1")
        req = FakeRequest(
            prompt="当前问题",
            system_prompt="BASE\n\n# Persona Instructions\n\n原生人设A\n",
            contexts=["old-native-1", "old-native-2"],
        )
        result = await budget.apply_context_and_memory_budget(
            event=event,
            req=req,
            soul=self.soul,
            user=user,
            run=run,
            current_slot=current_slot,
        )

        # 1) native Persona replaced by the character card
        assert "原生人设A" not in req.system_prompt
        assert PERSONA_START in req.system_prompt
        # 2) takeover BEFORE the BUSY state instructions
        assert req.system_prompt.index(PERSONA_START) < req.system_prompt.index(
            "繁忙中 (BUSY)"
        )
        # 3) contexts cleared + plugin sliding window injected, current prompt excluded
        assert "old-native-1" not in req.contexts
        assert len(req.contexts) == 1
        assert "第一句历史消息" in req.contexts[0]["content"]
        assert "当前问题" not in req.contexts[0]["content"]
        assert result["window_count"] == 1
        # 4) the prompt itself is annotated with the relationship tier
        assert req.prompt.startswith("[◆L2重要熟人 阿一(ID:user-1)]: 当前问题")

    async def test_latest_window_message_kept_when_not_the_current_prompt(self):
        budget = self.build(native_prompts=[])
        user = make_user("user-1", "阿一")
        self.session_buffer.record_user_message("umo-1", user, "第一句")
        self.session_buffer.record_user_message("umo-1", user, "另一条消息")

        current_slot = make_slot(state=BotState.IDLE)
        req = FakeRequest(prompt="当前问题", system_prompt="BASE", contexts=["old"])
        result = await budget.apply_context_and_memory_budget(
            event=FakeEvent(message_str="当前问题", umo="umo-1"),
            req=req,
            soul=self.soul,
            user=user,
            run=make_run(),
            current_slot=current_slot,
        )
        assert len(req.contexts) == 2
        assert result["window_count"] == 2
        assert "另一条消息" in req.contexts[-1]["content"]

    async def test_idle_takeover_keeps_idle_instructions(self):
        budget = self.build(native_prompts=[])
        user = make_user("user-1", "阿一")
        current_slot = make_slot(state=BotState.IDLE, title="躺沙发刷视频")
        req = FakeRequest(prompt="在吗", system_prompt="BASE")
        await budget.apply_context_and_memory_budget(
            event=FakeEvent(message_str="在吗", umo="umo-1"),
            req=req,
            soul=self.soul,
            user=user,
            run=make_run(),
            current_slot=current_slot,
        )
        assert "空闲中 (IDLE)" in req.system_prompt
        assert "当前三态控制器" in req.system_prompt


if __name__ == "__main__":
    unittest.main()
