"""QQ「戳一戳」/ OneBot notice 伪消息回归测试（v2.8.2）。

背景
----
AstrBot 的 aiocqhttp(OneBot v11) 适配器会把**所有** ``post_type=notice`` 事件
（戳一戳 ``notify/poke``、撤回、群文件、好友请求、输入状态等）转换为
``AstrBotMessage``：``message_str == ""``，poke 事件最多带一个 ``Poke`` 组件。
私聊消息在 AstrBot 中默认唤醒，于是这些伪消息会进入插件门禁；v2.8.1 的
「纯 @ 空文本补占位文案」逻辑又把它们当成 @ 唤醒，导致机器人对空 prompt 回复
（表现为「戳一戳」自动回复）。本文件冻结修复契约：

1. ``_is_non_chat_platform_event`` 识别 notice/request/meta_event 与纯 Poke 链；
2. ``on_any_message`` 对这类事件直接跳过：不写滑窗、不建群档案、不走门禁、
   不产生回复决策，并显式关闭原生 LLM 管线；
3. 真正的消息（含 @-only 空文本、普通文本）继续走原有流程（回归 2d119a2 修复）。
"""

import os
import sys
import time
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _helpers import (  # noqa: E402
    DatabaseAsyncTestCase,
    FakeContext,
    card_with,
    load_plugin_modules,
    make_plugin_config,
    make_soul,
)
from test_regressions import bind_plugin_methods  # noqa: E402

mods = load_plugin_modules()

import OnlyYou.core.atmosphere_reader as _atmosphere_reader  # noqa: E402

mods.atmosphere_reader = _atmosphere_reader  # not preloaded by load_plugin_modules()

PRIVATE_UMO = "qq bot:FriendMessage:2975066889"
GROUP_UMO = "qq bot:GroupMessage:1039802291"
BOT_QQ = "3297648235"
USER_QQ = "2975066889"


class _StubHubWithProviderId(SimpleNamespace):
    async def get_main_provider_id(self, soul_id=None):
        return ""

    async def call_llm_json(
        self,
        prompt="",
        role="decision",
        umo=None,
        system_prompt="",
        image_urls=None,
        default=None,
    ):
        return {"decision": "REPLY", "confidence": 0.9}

    async def call_llm_text(
        self,
        prompt="",
        role="decision",
        umo=None,
        system_prompt="",
        image_urls=None,
    ):
        return "嗯嗯"


class _StubSoulManager:
    def __init__(self, soul):
        self._soul = soul

    async def resolve_soul(self, umo=None, allow_llm_seed=True):
        return self._soul


class _StubScheduleMutator:
    async def on_message_input(self, **kwargs):
        return False


class _StubDeckEngine:
    def __init__(self, slot):
        self._slot = slot

    async def get_current_slot(self, **kwargs):
        return self._slot


class _PlatformEvent:
    """Minimal ``AstrMessageEvent`` stand-in with a raw OneBot payload."""

    def __init__(
        self,
        umo=PRIVATE_UMO,
        post_type="notice",
        sub_type="poke",
        notice_type="notify",
        message_str="",
        at_bot=True,
        group_id=None,
        components=None,
        raw_message=None,
    ):
        self.message_str = message_str
        self.unified_msg_origin = umo
        self.is_at_or_wake_command = bool(at_bot)
        self.is_wake = bool(at_bot)
        self.call_llm = False
        self._sender_id = USER_QQ
        self._sender_name = "hidge-1"
        self._group_id = str(group_id or "")
        self._extra = {}

        if raw_message is not None:
            self.message_obj = SimpleNamespace(
                message=list(components or []), raw_message=raw_message
            )
            return

        raw = {
            "post_type": post_type,
            "time": int(time.time()),
            "self_id": BOT_QQ,
            "user_id": USER_QQ,
        }
        if post_type == "notice":
            raw["notice_type"] = notice_type
            raw["sub_type"] = sub_type
            if sub_type == "poke":
                raw["target_id"] = BOT_QQ
        if group_id:
            raw["group_id"] = str(group_id)
        if components is None:
            components = [SimpleNamespace(type="poke")] if sub_type == "poke" else []
        self.message_obj = SimpleNamespace(
            message=list(components), raw_message=raw
        )

    # --- AstrMessageEvent-ish API used by the gate / atmosphere reader ---
    def get_sender_id(self):
        return self._sender_id

    def get_sender_name(self):
        return self._sender_name

    def get_group_id(self):
        return self._group_id or None

    def get_self_id(self):
        return BOT_QQ

    def is_admin(self):
        return True

    def is_private_chat(self):
        return not self._group_id

    def should_call_llm(self, value):
        self.call_llm = bool(value)

    def set_extra(self, key, value):
        self._extra[key] = value


class NoticeEventTestBase(DatabaseAsyncTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.config = make_plugin_config()
        self.soul = make_soul("酒狐", card=card_with(identity={"name": "酒狐"}))
        self.soul.bound_platforms = ["qq bot"]
        self.slot = mods.roguelite_deck.SlotInstance(
            slot_key="block_4",
            start_hour=18.0,
            end_hour=21.0,
            card_id="evt_idle",
            title="瘫着刷B站水群",
            state=mods.state_machine.BotState.IDLE,
            action_type=mods.state_machine.ActionType.IDLE_ROAM,
            armor=25,
        )
        self.session_buffer = mods.context_budget.SessionBuffer(self.config)
        self.platform_bridge = mods.platform_bridge.PlatformBridge(FakeContext())
        self.user_relation = mods.user_relation.UserRelationManager(
            self.db, self.platform_bridge, self.config
        )
        self.atmosphere = mods.atmosphere_reader.AtmosphereReader(
            session_buffer=self.session_buffer,
            model_hub=_StubHubWithProviderId(),
            plugin_config=self.config,
        )
        self.fake = SimpleNamespace(
            config=self.config,
            session_buffer=self.session_buffer,
            platform_bridge=self.platform_bridge,
            soul_manager=_StubSoulManager(self.soul),
            user_relation=self.user_relation,
            atmosphere_reader=self.atmosphere,
            schedule_mutator=_StubScheduleMutator(),
            deck_engine=_StubDeckEngine(self.slot),
            model_hub=_StubHubWithProviderId(),
            _last_message_tier={},
            _known_l3_umos={},
            _known_group_umos=set(),
            _ensure_initialized=self._noop_ensure_initialized,
            _on_window_overflow=None,
        )
        self.fake = bind_plugin_methods(self.fake)

    async def _noop_ensure_initialized(self):
        return None


class TestNonChatEventDetection(NoticeEventTestBase):
    def test_private_poke_notice_is_detected(self):
        event = _PlatformEvent(umo=PRIVATE_UMO)
        assert self.fake._is_non_chat_platform_event(event) is True

    def test_group_poke_notice_is_detected(self):
        event = _PlatformEvent(umo=GROUP_UMO, group_id="1039802291")
        assert self.fake._is_non_chat_platform_event(event) is True

    def test_request_and_other_notices_are_detected(self):
        for post_type, sub_type in (
            ("notice", "group_upload"),
            ("notice", "friend_recall"),
            ("notice", "input_status"),
            ("request", "friend"),
            ("meta_event", "heartbeat"),
            ("message_sent", ""),
        ):
            event = _PlatformEvent(
                post_type=post_type, sub_type=sub_type, components=[]
            )
            assert self.fake._is_non_chat_platform_event(event) is True, (
                f"{post_type}/{sub_type} must be treated as non-chat"
            )

    def test_regular_message_event_is_not_detected(self):
        event = _PlatformEvent(
            umo=GROUP_UMO,
            post_type="message",
            sub_type="",
            group_id="1039802291",
            message_str="你好呀",
            components=[SimpleNamespace(type="plain")],
        )
        assert self.fake._is_non_chat_platform_event(event) is False

    def test_at_only_message_event_is_not_detected(self):
        event = _PlatformEvent(
            umo=GROUP_UMO,
            post_type="message",
            sub_type="",
            group_id="1039802291",
            message_str="",
            at_bot=True,
            components=[SimpleNamespace(type="at")],
        )
        assert self.fake._is_non_chat_platform_event(event) is False

    def test_poke_component_without_raw_post_type_is_detected(self):
        event = _PlatformEvent(
            umo=PRIVATE_UMO,
            raw_message={},
            components=[SimpleNamespace(type="poke")],
        )
        assert self.fake._is_non_chat_platform_event(event) is True


class TestPokeGateBehaviour(NoticeEventTestBase):
    async def test_private_poke_is_ignored_and_never_replies(self):
        event = _PlatformEvent(umo=PRIVATE_UMO, at_bot=True)
        await self.fake.on_any_message(event)

        # No sliding window / no relation bookkeeping.
        assert not self.session_buffer._windows.get(PRIVATE_UMO)
        assert PRIVATE_UMO not in self.fake._last_message_tier
        # Native LLM pipeline explicitly suppressed (private chat is wake-by-default).
        assert event.is_at_or_wake_command is False
        assert event.call_llm is True
        # No group card side effects for a private poke.
        assert not self.fake._known_group_umos

    async def test_group_poke_is_ignored_and_creates_no_group_profile(self):
        event = _PlatformEvent(
            umo=GROUP_UMO, group_id="1039802291", at_bot=True
        )
        await self.fake.on_any_message(event)

        assert not self.session_buffer._windows.get(GROUP_UMO)
        assert GROUP_UMO not in self.fake._known_group_umos
        assert await self.user_relation.get_group(GROUP_UMO) is None
        assert event.is_at_or_wake_command is False
        assert event.call_llm is True

    async def test_all_notice_variants_are_ignored(self):
        for idx, (post_type, sub_type, components) in enumerate(
            (
                ("notice", "group_upload", []),
                ("notice", "friend_recall", []),
                ("notice", "input_status", []),
                ("notice", "group_admin", []),
                ("request", "group", []),
                ("meta_event", "", []),
            )
        ):
            event = _PlatformEvent(
                umo=f"{GROUP_UMO}:{idx}",
                post_type=post_type,
                sub_type=sub_type,
                components=components,
                group_id="1039802291",
                at_bot=True,
            )
            await self.fake.on_any_message(event)
            assert not self.session_buffer._windows.get(event.unified_msg_origin)
            assert event.call_llm is True


class TestRegularMessagesStillWork(NoticeEventTestBase):
    async def test_regular_text_message_is_processed(self):
        event = _PlatformEvent(
            umo=GROUP_UMO,
            post_type="message",
            sub_type="",
            group_id="1039802291",
            message_str="狐狸今天干嘛呢",
            at_bot=False,
            components=[SimpleNamespace(type="plain")],
        )
        await self.fake.on_any_message(event)
        window = self.session_buffer._windows.get(GROUP_UMO) or []
        assert window, "regular messages must still enter the sliding window"
        assert any("狐狸今天干嘛呢" in item.content for item in window)
        assert GROUP_UMO in self.fake._known_group_umos

    async def test_at_only_message_still_processed_after_notice_filter(self):
        """Regression for 2d119a2: @-only empty text is a real message, not a notice."""
        event = _PlatformEvent(
            umo=GROUP_UMO,
            post_type="message",
            sub_type="",
            group_id="1039802291",
            message_str="",
            at_bot=True,
            components=[SimpleNamespace(type="at")],
        )
        await self.fake.on_any_message(event)
        window = self.session_buffer._windows.get(GROUP_UMO) or []
        assert window, "@-only messages must still be recorded"
        assert "@" in window[0].content
        group = await self.user_relation.get_group(GROUP_UMO)
        assert group is not None


if __name__ == "__main__":
    unittest.main()
