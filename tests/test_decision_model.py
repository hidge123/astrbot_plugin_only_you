"""Decision-role split tests: dialogue / main / decision + judge (Jev) mode.

Covers the new role layout:

* legacy ``main`` (chat) -> ``dialogue``; legacy ``decision`` -> ``main`` +
  ``decision``;
* ``get_main_provider_id`` only uses the dialogue role;
* the decision role supports both ``mode="chat"`` (JSON verdict) and
  ``mode="judge"`` (OpenRouter Decisions API, Jev-style typed probabilities);
* credentials come from the selected OpenRouter provider source or the
  ``OPENROUTER_API_KEY`` environment variable (never leaked by status);
* the atmosphere reader routes through ``judge_choice`` when available.
"""

import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _helpers import (  # noqa: E402
    DatabaseAsyncTestCase,
    FakeChatProvider,
    FakeContext,
    FakeProviderManager,
    StubModelHub,
    load_plugin_modules,
    make_plugin_config,
    make_slot,
    make_soul,
    make_user,
)

mods = load_plugin_modules()
ModelHub = mods.model_hub.ModelHub
ProviderFactory = mods.provider_factory.ProviderFactory
normalize_model_config = mods.model_hub.normalize_model_config
decision_model = mods.decision_model
AtmosphereReader = mods.atmosphere_reader.AtmosphereReader
SessionBuffer = mods.context_budget.SessionBuffer
BotState = mods.state_machine.BotState
ActionType = mods.state_machine.ActionType
UserTier = mods.state_machine.UserTier

OPENROUTER_SOURCE = {
    "id": "openrouter",
    "provider": "openrouter",
    "type": "openai_chat_completion",
    "provider_type": "chat_completion",
    "key": "sk-or-source",
    "api_base": "https://openrouter.ai/api/v1",
    "timeout": 120,
    "proxy": "",
    "enable": True,
}
DEEPSEEK_SOURCE = {
    "id": "deepseek",
    "provider": "deepseek",
    "type": "openai_chat_completion",
    "provider_type": "chat_completion",
    "key": "sk-deepseek",
    "api_base": "https://api.deepseek.com/v1",
    "timeout": 120,
    "proxy": "",
    "enable": True,
}
GROUP_UMO = "aiocqhttp:GroupMessage:g1"


def _judge_payload(choice="REPLY", reply_probability=0.82, confidence=0.64):
    return {
        "id": "gen-dec-test",
        "model": "typesafe/jev-1.14-20261014",
        "provider": "TypeSafe",
        "answers": {
            "decision": {
                "type": "choice",
                "choice": choice,
                "probabilities": {
                    "REPLY": reply_probability,
                    "IGNORE": round(1.0 - reply_probability, 4),
                },
                "confidence": confidence,
            }
        },
        "usage": {"input_tokens": 500, "output_tokens": 10, "cost": 0.00002},
    }


class TestRoleLayoutMigration(unittest.TestCase):
    def test_legacy_main_becomes_dialogue_and_decision_becomes_main(self):
        cfg = normalize_model_config(
            {
                "main": {"source_id": "s1", "model": "chat"},
                "decision": {
                    "inherit": False,
                    "source_id": "s2",
                    "model": "reasoner",
                },
            }
        )
        assert cfg["main"] == {
            "inherit": False,
            "inherit_from": "",
            "source_id": "s2",
            "model": "reasoner",
        }
        # old main chat model is preserved as an independent dialogue config
        assert cfg["dialogue"]["inherit"] is False
        assert cfg["dialogue"]["source_id"] == "s1"
        assert cfg["dialogue"]["model"] == "chat"
        # read-the-air starts from the old decision's effective model
        assert cfg["decision"]["inherit_from"] == "main"
        assert cfg["decision"]["mode"] == "chat"
        assert cfg["decision"]["threshold"] == 0.5
        # old vision followed the old chat model, which differs from the base
        assert cfg["vision"]["inherit"] is False
        assert cfg["vision"]["model"] == "chat"

    def test_legacy_inherited_decision_keeps_following_dialogue(self):
        cfg = normalize_model_config(
            {"main": {"source_id": "s1", "model": "chat"}}
        )
        assert cfg["main"]["source_id"] == "s1"
        assert cfg["main"]["model"] == "chat"
        assert cfg["dialogue"]["inherit_from"] == "main"
        assert cfg["decision"]["inherit_from"] == "main"

    def test_dialogue_root_layout_migrates_to_main_base(self):
        cfg = normalize_model_config(
            {
                # interim layout: main inherited dialogue, dialogue was the root
                "dialogue": {"source_id": "chat_src", "model": "chat_model"},
                "main": {
                    "inherit": True,
                    "inherit_from": "dialogue",
                    "source_id": "",
                    "model": "",
                },
                "decision": {"inherit": True, "inherit_from": "main"},
                "vision": {"inherit": True, "inherit_from": "dialogue"},
            }
        )
        # effective models are preserved; main becomes the base.
        assert cfg["main"]["source_id"] == "chat_src"
        assert cfg["main"]["model"] == "chat_model"
        assert cfg["main"]["inherit_from"] == ""
        assert cfg["dialogue"]["inherit_from"] == "main"
        assert cfg["dialogue"]["model"] == ""
        assert cfg["decision"]["inherit_from"] == "main"
        assert cfg["vision"]["inherit_from"] == "main"

    def test_interim_layout_with_different_pairs_keeps_independent_roles(self):
        cfg = normalize_model_config(
            {
                "dialogue": {"source_id": "chat_src", "model": "chat_model"},
                "main": {
                    "inherit": False,
                    "inherit_from": "",
                    "source_id": "gen_src",
                    "model": "gen_model",
                },
                "decision": {"inherit": True, "inherit_from": "main"},
                "vision": {"inherit": True, "inherit_from": "dialogue"},
            }
        )
        assert cfg["main"]["model"] == "gen_model"
        # dialogue / vision point at a different model, so they stay independent
        assert cfg["dialogue"]["inherit"] is False
        assert cfg["dialogue"]["model"] == "chat_model"
        assert cfg["decision"]["inherit_from"] == "main"
        assert cfg["vision"]["inherit"] is False
        assert cfg["vision"]["model"] == "chat_model"

    def test_new_layout_is_not_migrated(self):
        raw = {
            "dialogue": {"inherit": False, "source_id": "s1", "model": "chat"},
            "main": {"inherit_from": "", "source_id": "s2", "model": "gen"},
            "decision": {
                "inherit_from": "",
                "source_id": "s3",
                "model": "typesafe/jev-1.14",
                "mode": "judge",
                "threshold": 0.7,
            },
        }
        cfg = normalize_model_config(raw)
        assert cfg["dialogue"]["model"] == "chat"
        assert cfg["main"]["model"] == "gen"
        assert cfg["decision"]["model"] == "typesafe/jev-1.14"
        assert cfg["decision"]["mode"] == "judge"
        assert cfg["decision"]["threshold"] == 0.7

    def test_partial_new_layout_with_judge_fields_is_not_migrated(self):
        cfg = normalize_model_config(
            {"decision": {"mode": "judge", "model": "~typesafe/jev-latest"}}
        )
        # no dialogue key, but the judge marker prevents legacy migration
        assert cfg["dialogue"]["inherit_from"] == "main"
        assert cfg["dialogue"]["source_id"] == ""
        assert cfg["dialogue"]["model"] == ""
        assert cfg["decision"]["model"] == "~typesafe/jev-latest"
        assert cfg["decision"]["mode"] == "judge"


class JudgeTestBase(DatabaseAsyncTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        manager = FakeProviderManager(
            sources=[OPENROUTER_SOURCE, DEEPSEEK_SOURCE],
            providers=[],
            provider_settings={},
            audio_dir=self.tmpdir,
        )
        self.context = FakeContext(manager)
        self.factory = ProviderFactory(self.context)
        self.hub = ModelHub(
            context=self.context,
            plugin_config={},
            db=self.db,
            provider_factory=self.factory,
        )
        await self.hub.load()

    async def save_judge_config(self, **decision_overrides):
        decision = {
            "inherit": False,
            "inherit_from": "",
            "source_id": "openrouter",
            "model": "typesafe/jev-1.14",
            "mode": "judge",
            "threshold": 0.5,
            "endpoint": "",
        }
        decision.update(decision_overrides)
        return await self.hub.save_model_config(
            {
                "dialogue": {"source_id": "deepseek", "model": "deepseek-chat"},
                "decision": decision,
            }
        )


class TestDecisionCredentials(JudgeTestBase):
    def test_source_key_and_endpoint(self):
        credentials = self.hub._decision_credentials(
            {"source_id": "openrouter", "model": "typesafe/jev-1.14"}
        )
        assert credentials["api_key"] == "sk-or-source"
        assert credentials["key_source"] == "source"
        assert credentials["endpoint"] == mods.model_hub.DEFAULT_DECISIONS_ENDPOINT

    def test_env_fallback_when_no_openrouter_source(self):
        credentials = self.hub._decision_credentials(
            {"source_id": "", "model": "~typesafe/jev-latest"}
        )
        assert credentials["api_key"] == ""
        with mock.patch.dict(
            os.environ, {"OPENROUTER_API_KEY": "sk-or-env"}, clear=False
        ):
            credentials = self.hub._decision_credentials(
                {"source_id": "", "model": "~typesafe/jev-latest"}
            )
        assert credentials["api_key"] == "sk-or-env"
        assert credentials["key_source"] == "env"

    def test_non_openrouter_source_falls_back_to_env(self):
        with mock.patch.dict(
            os.environ, {"OPENROUTER_API_KEY": "sk-or-env"}, clear=False
        ):
            credentials = self.hub._decision_credentials(
                {"source_id": "deepseek", "model": "typesafe/jev-1.14"}
            )
        # A DeepSeek key cannot authenticate the OpenRouter Decisions endpoint.
        assert credentials["api_key"] == "sk-or-env"
        assert credentials["key_source"] == "env"


class TestDecisionJudge(JudgeTestBase):
    async def test_judge_calls_decisions_api_and_normalizes_choice(self):
        await self.save_judge_config()
        captured = {}

        async def fake_post(url, headers, payload, timeout=45.0):
            captured.update(
                {"url": url, "headers": headers, "payload": payload, "timeout": timeout}
            )
            return _judge_payload()

        with mock.patch.object(decision_model, "post_json", fake_post):
            result = await self.hub.judge_choice(
                prompt="chat prompt (must not be used in judge mode)",
                state={"latest": "在吗", "context": "群聊"},
                instructions="判断是否接话",
                criteria={"REPLY": "适合接话", "IGNORE": "不适合"},
            )
        assert result is not None
        assert result["decision"] == "REPLY"
        assert result["confidence"] == 0.82       # probability of the chosen option
        assert result["threshold"] == 0.5
        assert result["mode"] == "judge"
        assert self.hub.verdict_is_reply(result) is True

        assert captured["url"] == mods.model_hub.DEFAULT_DECISIONS_ENDPOINT
        assert captured["headers"]["Authorization"].startswith("Bearer ")
        payload = captured["payload"]
        assert payload["model"] == "typesafe/jev-1.14"
        assert payload["state"]["latest"] == "在吗"
        question = payload["questions"]["decision"]
        assert question["type"] == "choice"
        assert set(question["criteria"]) == {"REPLY", "IGNORE"}

    async def test_threshold_blocks_low_probability_reply(self):
        await self.save_judge_config(threshold=0.6)
        async def fake_post(url, headers, payload, timeout=45.0):
            return _judge_payload(reply_probability=0.55)

        with mock.patch.object(decision_model, "post_json", fake_post):
            result = await self.hub.judge_choice(
                prompt="",
                state={},
                instructions="",
                criteria={"REPLY": "yes", "IGNORE": "no"},
            )
        assert result["decision"] == "REPLY"
        assert result["threshold"] == 0.6
        assert self.hub.verdict_is_reply(result) is False

    async def test_judge_env_only_without_provider_source(self):
        await self.save_judge_config(source_id="", model="~typesafe/jev-latest")

        async def fake_post(url, headers, payload, timeout=45.0):
            assert headers["Authorization"] == "Bearer sk-or-env"
            return _judge_payload(reply_probability=0.7)

        with mock.patch.dict(
            os.environ, {"OPENROUTER_API_KEY": "sk-or-env"}, clear=False
        ):
            status = (await self.hub.get_status())["decision"]
            with mock.patch.object(decision_model, "post_json", fake_post):
                result = await self.hub.judge_choice(
                    prompt="",
                    state={},
                    instructions="",
                    criteria={"REPLY": "yes", "IGNORE": "no"},
                )
        assert status["configured"] is True
        assert status["judge_ready"] is True
        assert status["key_source"] == "env"
        assert result["decision"] == "REPLY"

    async def test_judge_mode_missing_key_returns_none(self):
        await self.save_judge_config(source_id="", model="~typesafe/jev-latest")
        env = {key: value for key, value in os.environ.items() if key != "OPENROUTER_API_KEY"}
        with mock.patch.dict(os.environ, env, clear=True):
            result = await self.hub.judge_choice(
                prompt="",
                state={},
                instructions="",
                criteria={"REPLY": "yes", "IGNORE": "no"},
            )
        assert result is None

    async def test_chat_mode_still_uses_json_prompt(self):
        await self.save_judge_config(mode="chat", inherit=True, inherit_from="dialogue")
        calls = []

        async def fake_chat_json(**kwargs):
            calls.append(kwargs)
            return {"decision": "IGNORE", "confidence": 0.2, "reason": "复读"}

        self.hub.call_llm_json = fake_chat_json
        with mock.patch.object(
            decision_model, "post_json", side_effect=AssertionError("must not call")
        ):
            result = await self.hub.judge_choice(
                prompt="prompt-body",
                state={"x": 1},
                instructions="",
                criteria={"REPLY": "yes", "IGNORE": "no"},
            )
        assert result["mode"] == "chat"
        assert result["decision"] == "IGNORE"
        assert result["threshold"] is None
        assert calls and calls[0]["prompt"] == "prompt-body"
        assert calls[0]["role"] == "decision"


class TestDecisionStatusAndTest(JudgeTestBase):
    async def test_status_reports_judge_readiness_without_leaking_key(self):
        await self.save_judge_config(threshold=0.65)
        status = (await self.hub.get_status())["decision"]
        assert status["decision_mode"] == "judge"
        assert status["judge_ready"] is True
        assert status["threshold"] == 0.65
        assert status["mode"] == "judge"
        assert status["ok"] is True
        assert status["key_source"] == "source"
        assert "sk-or-source" not in json.dumps(status, ensure_ascii=False)

    async def test_status_judge_unready_without_credentials(self):
        await self.save_judge_config(source_id="", model="~typesafe/jev-latest")
        env = {key: value for key, value in os.environ.items() if key != "OPENROUTER_API_KEY"}
        with mock.patch.dict(os.environ, env, clear=True):
            status = (await self.hub.get_status())["decision"]
        assert status["decision_mode"] == "judge"
        assert status["judge_ready"] is False
        assert status["ok"] is False

    async def test_test_role_routes_judge_mode_to_decisions_api(self):
        await self.save_judge_config()

        async def fake_post(url, headers, payload, timeout=45.0):
            return _judge_payload()

        with mock.patch.object(decision_model, "post_json", fake_post):
            result = await self.hub.test_role("decision")
        assert result["status"] == "ok"
        assert result["role"] == "decision"
        assert result["mode"] == "judge"
        assert result["decision"] == "REPLY"


class TestDialogueRoleSeparation(JudgeTestBase):
    async def test_dialogue_inherits_main_but_can_override_or_disable(self):
        main_cfg = {
            "main": {
                "inherit": False,
                "inherit_from": "",
                "source_id": "deepseek",
                "model": "deepseek-chat",
            },
            "decision": {"mode": "chat"},
        }
        # Default: dialogue inherits the base model.
        await self.hub.save_model_config(main_cfg)
        assert await self.hub.get_main_provider_id() == "onlyyou::main"
        assert await self.hub.resolve_role("dialogue") == {
            "source_id": "deepseek",
            "model": "deepseek-chat",
        }

        # Independently configured dialogue overrides the base.
        await self.hub.save_model_config(
            {
                **main_cfg,
                "dialogue": {
                    "inherit": False,
                    "source_id": "deepseek",
                    "model": "deepseek-reasoner",
                },
            }
        )
        assert await self.hub.resolve_role("dialogue") == {
            "source_id": "deepseek",
            "model": "deepseek-reasoner",
        }
        assert await self.hub.get_main_provider_id() == "onlyyou::main"

        # Explicitly disabled dialogue never falls back to main.
        await self.hub.save_model_config(
            {**main_cfg, "dialogue": {"inherit": False}}
        )
        assert await self.hub.get_main_provider_id() == ""


class _GroupEvent:
    def __init__(self, message_str="今天这瓜也太大了", umo=GROUP_UMO):
        self.message_str = message_str
        self.unified_msg_origin = umo
        self.is_at_or_wake_command = False
        self.is_wake = False
        self.call_llm = False
        self.message_obj = SimpleNamespace(
            message=[], raw_message={"post_type": "message"}
        )

    def get_sender_id(self):
        return "u-1"

    def get_sender_name(self):
        return "阿一"

    def get_group_id(self):
        return "g1"

    def is_admin(self):
        return False

    def is_private_chat(self):
        return False

    def should_call_llm(self, value):
        self.call_llm = bool(value)


class _JudgeStubHub(StubModelHub):
    def __init__(self):
        super().__init__()
        self.judge_calls = []

    async def judge_choice(self, **kwargs):
        self.judge_calls.append(kwargs)
        return {
            "decision": "REPLY",
            "confidence": 0.9,
            "threshold": 0.5,
            "mode": "judge",
        }

    @staticmethod
    def verdict_is_reply(result):
        return str(result.get("decision") or "").upper() == "REPLY"


class TestAtmosphereUsesJudgeChoice(DatabaseAsyncTestCase):
    async def test_evaluate_should_reply_uses_typed_judge_payload(self):
        config = make_plugin_config()
        config["atmosphere_config"] = {
            "takeover_group_chat": True,
            "debounce_seconds": 0.0,
            "reply_gate_enabled": False,
        }
        buffer = SessionBuffer(config)
        hub = _JudgeStubHub()
        reader = AtmosphereReader(buffer, hub, config)
        soul = make_soul("soul-judge")
        slot = make_slot(state=BotState.IDLE, action_type=ActionType.IDLE_ROAM)
        user = make_user("u-1", "阿一")

        decision = await reader.evaluate_should_reply(
            event=_GroupEvent(),
            soul=soul,
            user=user,
            current_slot=slot,
            is_direct_mention=False,
        )
        assert decision is True
        assert hub.judge_calls, "judge_choice must be used instead of call_llm_json"
        call = hub.judge_calls[0]
        assert call["state"]["latest_message"]["content"] == "今天这瓜也太大了"
        assert call["state"]["latest_message"]["user_tier"] == user.tier.label
        assert "REPLY" in call["criteria"] and "IGNORE" in call["criteria"]


class TestGenerationRoleRemap(DatabaseAsyncTestCase):
    async def test_memory_extraction_uses_main_role(self):
        from _helpers import StubModelHub as _StubHub

        hub = _StubHub(json_result=[])
        manager = mods.memory_manager.MemoryManager(
            self.db, hub, make_plugin_config()
        )
        await manager.extract_memories_from_transcript(
            soul_id="soul-main",
            soul_name="星野",
            transcript_text="[2026-11-16 21:10] [L1 阿一(ID:u-1)]: 我最近在学做饭",
        )
        assert hub.calls, "extraction must call the model hub"
        assert hub.calls[0]["role"] == "main"


if __name__ == "__main__":
    unittest.main()
