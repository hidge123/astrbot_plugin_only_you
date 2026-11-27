"""Offline tests for the configurable persona-prompt budget.

Covers ``render_persona_prompt`` / ``measure_persona_prompt``, the Pages card
APIs' budget / full / over-budget stats, and hot-applying
``memory_and_context_config.persona_prompt_max_chars`` from the settings page.

Run with: ``python3 -m unittest tests.test_persona_prompt_budget -v``.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _helpers import (  # noqa: E402
    card_with,
    load_plugin_modules,
    make_plugin_config,
)
from test_web_api import WebApiTestBase  # noqa: E402

mods = load_plugin_modules()
character_card = mods.character_card
config_manager = mods.config_manager

HUGE_STORY = "这是一段很长的背景故事。" * 420  # ~5040 chars


class TestPersonaPromptBudget(WebApiTestBase):
    @staticmethod
    def _huge_card():
        return card_with(
            identity={"name": "长卡测试", "tagline": "超长背景"},
            background={"growth_story": HUGE_STORY},
        )

    async def test_measure_reports_full_length_and_render_stays_within_budget(self):
        card = self._huge_card()
        full = character_card.measure_persona_prompt(card)
        assert full > character_card.DEFAULT_PERSONA_PROMPT_MAX_CHARS
        prompt = character_card.render_persona_prompt(card)
        assert len(prompt) <= character_card.DEFAULT_PERSONA_PROMPT_MAX_CHARS
        assert full > len(prompt)

    async def test_config_field_exists_with_expected_default_and_range(self):
        field = config_manager.SECTION_MAP["memory"].field_map()[
            "persona_prompt_max_chars"
        ]
        assert field.default == character_card.DEFAULT_PERSONA_PROMPT_MAX_CHARS
        assert field.minimum == character_card.PERSONA_PROMPT_BUDGET_MIN
        assert field.maximum == character_card.PERSONA_PROMPT_BUDGET_MAX
        assert field.unit == "字"

    async def test_card_schema_reports_budget_stats(self):
        data = self.assert_ok(await self.call("GET", self.api.api_card_schema))
        stats = data["persona_prompt"]
        assert stats["budget"] == character_card.DEFAULT_PERSONA_PROMPT_MAX_CHARS
        assert stats["chars"] <= stats["budget"]
        assert stats["full_chars"] >= stats["chars"]
        assert isinstance(stats["over_budget"], bool)
        assert stats["dropped_chars"] == max(0, stats["full_chars"] - stats["chars"])

    async def test_save_and_preview_report_over_budget(self):
        soul = await self.first_soul()
        card = self._huge_card()

        saved = self.assert_ok(
            await self.call(
                "POST",
                self.api.api_card_save,
                body={"soul_id": soul.soul_id, "card": card},
            )
        )
        stats = saved["persona_prompt"]
        assert stats["over_budget"] is True
        assert stats["full_chars"] > stats["budget"]
        assert stats["chars"] <= stats["budget"]

        preview = self.assert_ok(
            await self.call(
                "POST",
                self.api.api_card_preview_prompt,
                body={"soul_id": soul.soul_id},
            )
        )
        assert preview["over_budget"] is True
        assert preview["full_chars"] == stats["full_chars"]
        assert preview["chars"] == len(preview["prompt"])

    async def test_budget_is_adjustable_and_hot_applies(self):
        soul = await self.first_soul()
        card = self._huge_card()
        await self.call(
            "POST",
            self.api.api_card_save,
            body={"soul_id": soul.soul_id, "card": card},
        )

        before = self.assert_ok(
            await self.call(
                "GET", self.api.api_card_schema, query={"soul_id": soul.soul_id}
            )
        )["persona_prompt"]
        assert before["budget"] == 4000
        assert before["over_budget"] is True

        result = await self.plugin.config_manager.save(
            {
                "section": "memory",
                "values": {"persona_prompt_max_chars": 6000},
            }
        )
        assert "persona_prompt_max_chars" in result["changed"]
        assert self.plugin.config["memory_and_context_config"][
            "persona_prompt_max_chars"
        ] == 6000
        # Sub-modules read the shared dict in place: no rebuild required.
        assert self.plugin.context_budget.persona_prompt_max_chars == 6000

        after = self.assert_ok(
            await self.call(
                "GET", self.api.api_card_schema, query={"soul_id": soul.soul_id}
            )
        )["persona_prompt"]
        assert after["budget"] == 6000
        assert after["over_budget"] is False
        assert after["chars"] == after["full_chars"]

        # Clamped to the schema range.
        await self.plugin.config_manager.save(
            {
                "section": "memory",
                "values": {"persona_prompt_max_chars": 99999},
            }
        )
        assert self.plugin.context_budget.persona_prompt_max_chars == 20000


if __name__ == "__main__":
    unittest.main()
