"""Offline tests for regenerating an existing Soul's character card from a Persona.

Covers the import mode introduced for the character page:

- ``SoulProfile``-level: ``import_native_persona(..., regenerate_existing=True)``
  rewrites only ``card`` while keeping ``model_config`` / ``bound_platforms`` /
  ``settings`` / ``state_event_cards`` and the avatar untouched;
- failure handling: an AI card-generation failure keeps the original card and
  raises instead of silently downgrading it;
- Web API: ``souls/import_persona`` accepts ``mode="regenerate"`` (defaults
  ``auto_generate_cards`` to false, keeps today's timeline) while the legacy
  ``mode="new"`` path still creates a brand-new Soul.

Run with: ``python3 -m unittest tests.test_persona_regenerate -v`` (stdlib only).
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _helpers import card_with, load_plugin_modules, make_soul  # noqa: E402
from test_web_api import WebApiTestBase  # noqa: E402

mods = load_plugin_modules()
ActionType = mods.state_machine.ActionType

PERSONA_NAME = "酒狐"
PERSONA_TEXT = "你叫酒狐，一位慢热但嘴硬心软的插画师，喜欢深夜刷手机、聊猫和画画。"


def _configured_soul():
    """Old card + custom Soul-level config that regeneration must preserve."""
    soul = make_soul(
        soul_id="soul_keep",
        card=card_with(
            identity={
                "name": "旧角色",
                "nickname": "旧小名",
                "tagline": "旧的定位",
                "avatar": "🌙",
            },
            speech={"speaking_style": "旧的口头禅"},
        ),
        model_config={
            "main": {"source_id": "deepseek", "model": "deepseek-reasoner"},
        },
        bound_platforms=["qq"],
        settings={"surfing_enabled": False, "surfing_sources": ["bilibili"]},
    )
    soul.state_event_cards = [
        mods.roguelite_deck.StateEventCard(
            card_id="evt_keep_1",
            title="专注赶稿",
            action_type=ActionType.BUSY_TASK,
            min_hours=1.0,
            max_hours=2.0,
        )
    ]
    return soul


class PersonaImportTestBase(WebApiTestBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        # The Web API / manager reads native Personas through context.
        self.plugin.context.persona_manager = SimpleNamespace(
            personas_v3=[{"name": PERSONA_NAME, "prompt": PERSONA_TEXT}],
            personas=[],
        )

    async def _save_configured_soul(self):
        await self.plugin.soul_manager.save_soul(_configured_soul())
        soul = await self.plugin.soul_manager.get_soul_by_id("soul_keep")
        assert soul is not None
        return soul

    @staticmethod
    def _snapshot(soul):
        data = soul.to_dict()
        return {
            "model_config": data["model_config"],
            "bound_platforms": data["bound_platforms"],
            "settings": data["settings"],
            "cards": [card["card_id"] for card in data["state_event_cards"]],
            "name": data["card"]["identity"]["name"],
            "avatar": data["card"]["identity"]["avatar"],
        }


# ==============================================================================
# 1. SoulManager: card-only regeneration
# ==============================================================================
class TestRegenerateSoulCard(PersonaImportTestBase):
    async def test_regenerate_replaces_only_the_character_card(self):
        soul = await self._save_configured_soul()
        before = self._snapshot(soul)

        updated = await self.plugin.soul_manager.import_native_persona(
            persona_name=PERSONA_NAME,
            target_soul_id="soul_keep",
            auto_generate_cards=False,
            regenerate_existing=True,
        )

        assert updated.soul_id == "soul_keep"
        assert updated.card["identity"]["name"] == PERSONA_NAME
        assert updated.card["extra"]["native_persona_prompt"] == PERSONA_TEXT
        # The existing avatar is kept when the import does not upload a new one.
        assert updated.card["identity"]["avatar"] == "🌙"
        after = self._snapshot(updated)
        assert after["model_config"] == before["model_config"]
        assert after["bound_platforms"] == ["qq"]
        assert after["settings"] == before["settings"]
        assert after["cards"] == ["evt_keep_1"]

        # Persisted JSON/SQLite state matches the returned profile.
        fetched = await self.plugin.soul_manager.get_soul_by_id("soul_keep")
        assert self._snapshot(fetched) == after

    async def test_regenerate_keeps_the_deck_when_seeding_fails(self):
        await self._save_configured_soul()

        async def fake_call_llm_json(**kwargs):
            return None

        with patch.object(
            self.plugin.model_hub, "call_llm_json", new=fake_call_llm_json
        ):
            updated = await self.plugin.soul_manager.import_native_persona(
                persona_name=PERSONA_NAME,
                target_soul_id="soul_keep",
                auto_generate_cards=True,
                regenerate_existing=True,
            )

        assert [card.card_id for card in updated.state_event_cards] == ["evt_keep_1"]

    async def test_regenerate_ai_failure_keeps_the_original_card(self):
        await self._save_configured_soul()

        async def fake_generate_card(seed_text="", base_card=None, section=None):
            return None

        with patch.object(
            self.plugin.soul_manager, "generate_card", new=fake_generate_card
        ):
            with self.assertRaises(ValueError):
                await self.plugin.soul_manager.import_native_persona(
                    persona_name=PERSONA_NAME,
                    target_soul_id="soul_keep",
                    auto_generate_cards=False,
                    ai_generate_card=True,
                    regenerate_existing=True,
                )

        fetched = await self.plugin.soul_manager.get_soul_by_id("soul_keep")
        assert fetched.card["identity"]["name"] == "旧角色"

    async def test_regenerate_requires_an_existing_soul(self):
        with self.assertRaises(ValueError):
            await self.plugin.soul_manager.import_native_persona(
                persona_name=PERSONA_NAME,
                target_soul_id="missing_soul",
                auto_generate_cards=False,
                regenerate_existing=True,
            )

    async def test_new_import_with_existing_id_keeps_soul_config(self):
        soul = await self._save_configured_soul()
        before = self._snapshot(soul)

        updated = await self.plugin.soul_manager.import_native_persona(
            persona_name=PERSONA_NAME,
            target_soul_id="soul_keep",
            auto_generate_cards=False,
        )

        assert updated.card["identity"]["name"] == PERSONA_NAME
        after = self._snapshot(updated)
        assert after["model_config"] == before["model_config"]
        assert after["bound_platforms"] == before["bound_platforms"]
        assert after["settings"] == before["settings"]
        assert after["cards"] == before["cards"]

    async def test_explicit_avatar_wins_over_the_old_one(self):
        await self._save_configured_soul()
        updated = await self.plugin.soul_manager.import_native_persona(
            persona_name=PERSONA_NAME,
            target_soul_id="soul_keep",
            auto_generate_cards=False,
            regenerate_existing=True,
            avatar="https://cdn.example.com/new-avatar.png",
        )
        assert updated.card["identity"]["avatar"] == "https://cdn.example.com/new-avatar.png"


# ==============================================================================
# 2. Web API: mode = new / regenerate
# ==============================================================================
class TestImportPersonaApiModes(PersonaImportTestBase):
    async def test_regenerate_updates_current_soul_only(self):
        await self._save_configured_soul()
        souls_before = await self.plugin.soul_manager.list_souls()
        soul_before = next(s for s in souls_before if s.soul_id == "soul_keep")
        before = self._snapshot(soul_before)

        detail = self.assert_ok(
            await self.call(
                "GET",
                self.api.api_souls_detail,
                query={"soul_id": "soul_keep"},
            )
        )
        slots_before = [
            (slot["start_hour"], slot["end_hour"], slot["title"])
            for slot in detail["schedule"]["run"]["slots"]
        ]

        response = await self.call(
            "POST",
            self.api.api_souls_import_persona,
            body={
                "mode": "regenerate",
                "persona_name": PERSONA_NAME,
                "target_soul_id": "soul_keep",
                "auto_generate_cards": False,
                "ai_generate_card": False,
            },
        )
        data = self.assert_ok(response)
        assert "其余配置保持不变" in response["message"]
        assert data["mode"] == "regenerate"

        soul = data["soul"]
        assert soul["soul_id"] == "soul_keep"
        assert soul["card"]["identity"]["name"] == PERSONA_NAME
        assert soul["card"]["extra"]["native_persona_prompt"] == PERSONA_TEXT
        after = self._snapshot_from_dict(soul)
        assert after["model_config"] == before["model_config"]
        assert after["bound_platforms"] == before["bound_platforms"]
        assert after["settings"] == before["settings"]
        assert after["cards"] == before["cards"]
        assert after["avatar"] == before["avatar"]

        # No new Soul was added and today's timeline is untouched.
        souls_after = await self.plugin.soul_manager.list_souls()
        assert len(souls_after) == len(souls_before)
        slots_after = [
            (slot["start_hour"], slot["end_hour"], slot["title"])
            for slot in data["schedule"]["run"]["slots"]
        ]
        assert slots_after == slots_before

    async def test_regenerate_defaults_to_keeping_the_deck(self):
        await self._save_configured_soul()

        async def fake_call_llm_json(**kwargs):
            return {
                "state_event_cards": [
                    {
                        "card_id": "evt_should_not_apply",
                        "title": "不该出现的新卡",
                        "action_type": "BUSY_TASK",
                        "min_hours": 1.0,
                        "max_hours": 2.0,
                    }
                ]
            }

        with patch.object(
            self.plugin.model_hub, "call_llm_json", new=fake_call_llm_json
        ):
            data = self.assert_ok(
                await self.call(
                    "POST",
                    self.api.api_souls_import_persona,
                    body={
                        "mode": "regenerate",
                        "persona_name": PERSONA_NAME,
                        "target_soul_id": "soul_keep",
                        # auto_generate_cards intentionally omitted: default false
                    },
                )
            )

        assert data["mode"] == "regenerate"
        assert [card["card_id"] for card in data["soul"]["state_event_cards"]] == [
            "evt_keep_1"
        ]

    async def test_regenerate_can_opt_into_a_new_deck(self):
        await self._save_configured_soul()

        async def fake_call_llm_json(**kwargs):
            return {
                "state_event_cards": [
                    {
                        "card_id": "evt_new_1",
                        "title": "新的忙碌",
                        "action_type": "BUSY_TASK",
                        "min_hours": 1.0,
                        "max_hours": 2.0,
                    }
                ]
            }

        with patch.object(
            self.plugin.model_hub, "call_llm_json", new=fake_call_llm_json
        ):
            data = self.assert_ok(
                await self.call(
                    "POST",
                    self.api.api_souls_import_persona,
                    body={
                        "mode": "regenerate",
                        "persona_name": PERSONA_NAME,
                        "target_soul_id": "soul_keep",
                        "auto_generate_cards": True,
                    },
                )
            )

        assert [card["card_id"] for card in data["soul"]["state_event_cards"]] == [
            "evt_new_1"
        ]

    async def test_new_mode_still_creates_a_new_soul(self):
        souls_before = await self.plugin.soul_manager.list_souls()
        data = self.assert_ok(
            await self.call(
                "POST",
                self.api.api_souls_import_persona,
                body={
                    "persona_name": PERSONA_NAME,
                    "target_soul_id": "soul_brand_new",
                    "auto_generate_cards": False,
                },
            )
        )
        assert data["mode"] == "new"
        assert data["soul"]["soul_id"] == "soul_brand_new"
        souls_after = await self.plugin.soul_manager.list_souls()
        assert len(souls_after) == len(souls_before) + 1

    async def test_regenerate_unknown_soul_returns_error(self):
        response = await self.call(
            "POST",
            self.api.api_souls_import_persona,
            body={
                "mode": "regenerate",
                "persona_name": PERSONA_NAME,
                "target_soul_id": "missing_soul",
            },
        )
        self.assert_error(response)
        assert "不存在" in response["message"]

    # ------------------------------------------------------------------
    async def test_api_error_keeps_the_original_card(self):
        await self._save_configured_soul()

        async def fake_generate_card(seed_text="", base_card=None, section=None):
            return None

        with patch.object(
            self.plugin.soul_manager, "generate_card", new=fake_generate_card
        ):
            response = await self.call(
                "POST",
                self.api.api_souls_import_persona,
                body={
                    "mode": "regenerate",
                    "persona_name": PERSONA_NAME,
                    "target_soul_id": "soul_keep",
                    "auto_generate_cards": False,
                    "ai_generate_card": True,
                },
            )
        self.assert_error(response)
        fetched = await self.plugin.soul_manager.get_soul_by_id("soul_keep")
        assert fetched.card["identity"]["name"] == "旧角色"

    @staticmethod
    def _snapshot_from_dict(soul):
        return {
            "model_config": soul["model_config"],
            "bound_platforms": soul["bound_platforms"],
            "settings": soul["settings"],
            "cards": [card["card_id"] for card in soul["state_event_cards"]],
            "name": soul["card"]["identity"]["name"],
            "avatar": soul["card"]["identity"]["avatar"],
        }


if __name__ == "__main__":
    unittest.main()
