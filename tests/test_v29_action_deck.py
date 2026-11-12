"""v2.9 regression tests: unified action-card titles + "re-roll card pool" semantics.

Covers the three v2.9 changes (see ``docs/IMPLEMENTATION_PLAN.md`` companion plan):

1. **统一行动卡标题格式** — ``ACTION_DECK_GENERATE_PROMPT`` + the code-side
   ``normalize_action_card_title`` sanitizer, so every generated / imported /
   fallback card title is a reusable 5~10-char single clause without a category
   colon prefix or sentence punctuation.
2. **重抽日程 = 清空并重新生成行动卡池（今日 24h 时间轴保持不变）** — both the
   ``schedule/reroll`` Web API and ``/onlyyou reroll`` lock today's already-drawn
   timeline first, then replace the Soul's ``state_event_cards`` for tomorrow on.
3. **旧版行动卡迁移清理** — legacy cards without ``action_type`` are dropped, and
   ``valid_slots`` / encounter-talk-card / ``custom_encounters`` are gone.

Everything is offline (stdlib unittest, no network, no AstrBot runtime).
"""

import os
import string
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _helpers import load_plugin_modules  # noqa: E402
from test_web_api import WebApiTestBase  # noqa: E402

mods = load_plugin_modules()
templates = mods.templates
deck = mods.roguelite_deck
soul_manager_mod = mods.soul_manager
ActionType = mods.state_machine.ActionType
StateEventCard = deck.StateEventCard

FIXED_ARMOR = {"SLEEP": 80, "SURF": 40, "BUSY_TASK": 65, "IDLE_ROAM": 25}
BAD_TITLE_PUNCTUATION = ("：", ":", "，", ",", "；", ";", "。", "！", "!", "？", "?")


def _assert_clean_title(testcase, title):
    testcase.assertTrue(title, "title must not be empty")
    testcase.assertGreaterEqual(len(title), 4, title)
    testcase.assertLessEqual(len(title), deck.MAX_ACTION_TITLE_CHARS, title)
    for punct in BAD_TITLE_PUNCTUATION:
        testcase.assertNotIn(punct, title, title)


# ==============================================================================
# 1. Prompt format constraints
# ==============================================================================
class TestActionDeckPrompt(unittest.TestCase):
    def test_generate_prompt_placeholders(self):
        prompt = templates.ACTION_DECK_GENERATE_PROMPT
        placeholders = {
            field
            for _, field, _, _ in string.Formatter().parse(prompt)
            if field
        }
        assert placeholders == {
            "soul_name",
            "soul_archetype",
            "speaking_style",
            "chronotype",
            "surfing_preferences",
            "soul_summary",
        }, placeholders

    def test_generate_prompt_enforces_9_cards_and_title_rules(self):
        prompt = templates.ACTION_DECK_GENERATE_PROMPT
        # Exactly 9 cards with the fixed four-type ratio.
        assert "9 张" in prompt
        assert "`SLEEP`（绑定 `RESTING`）**2 张**" in prompt
        assert "`BUSY_TASK`（绑定 `BUSY`）**3 张**" in prompt
        assert "`SURF`（绑定 `IDLE`）**2 张**" in prompt
        assert "`IDLE_ROAM`（绑定 `IDLE`）**2 张**" in prompt
        # Title hard constraints.
        assert "5 ~ 10 个汉字" in prompt
        assert "分类冒号前缀" in prompt
        assert "逗号 / 分号 / 句号 / 引号" in prompt
        # No armor / encounter-card leftovers, but the numeric fields stay.
        assert "armor" not in prompt.lower()
        assert "护甲" not in prompt
        assert "encounter" not in prompt.lower()
        assert "base_weight" in prompt and "min_hours" in prompt

    def test_seed_prompt_title_rules_are_aligned(self):
        prompt = templates.CARD_AND_DECK_SEED_PROMPT
        assert "5~10 个汉字" in prompt
        assert "分类冒号前缀" in prompt
        assert "角色名前缀" in prompt
        # Existing JSON contract (4 examples) must stay intact.
        assert prompt.count('"card_id"') == 4
        assert prompt.count('"base_weight"') == 4
        assert "armor" not in prompt.lower()
        assert "护甲" not in prompt


# ==============================================================================
# 2. Fallback deck titles (offline generation)
# ==============================================================================
class TestFallbackDeckTitles(unittest.TestCase):
    def test_fallback_titles_are_clean_reusable_phrases(self):
        cards = soul_manager_mod.SoulManager.build_fallback_custom_deck(
            "星野", "一个喜欢画插画、手冲咖啡与B站冲浪的画师"
        )
        assert len(cards) == 10
        assert {card.action for card in cards} == set(ActionType)
        for card in cards:
            _assert_clean_title(self, card.title)
            assert not card.title.startswith("星野")
            assert card.armor == FIXED_ARMOR[card.action.value], card.title
            assert "valid_slots" not in card.to_dict()

    def test_fallback_without_persona_keywords_uses_generic_clean_titles(self):
        cards = soul_manager_mod.SoulManager.build_fallback_custom_deck("小星", "")
        assert len(cards) == 10
        for card in cards:
            _assert_clean_title(self, card.title)


# ==============================================================================
# 3. SoulManager: AI generation / full-pool regeneration
# ==============================================================================
class TestGenerateActionCards(WebApiTestBase):
    async def test_llm_cards_are_sanitized_and_pinned_to_fixed_armor(self):
        soul = await self.first_soul()
        captured = {}

        async def fake_call_llm_json(**kwargs):
            captured.update(kwargs)
            return {
                "state_event_cards": [
                    {
                        "card_id": "x1",
                        "title": "正事：守着限定联名外卖开抢",
                        "action_type": "BUSY_TASK",
                        "min_hours": 1.0,
                        "max_hours": 3.0,
                        "base_weight": 60,
                        "armor": 3,
                    },
                    {
                        "card_id": "x2",
                        "title": "帮群友代肝活动，边打边发乐",
                        "action_type": "SURF",
                        "min_hours": 0.5,
                        "max_hours": 2.0,
                        "base_weight": 70,
                    },
                    # Legacy payload without action_type must be dropped.
                    {"card_id": "x3", "title": "旧版无行动类型卡", "state": "IDLE"},
                ]
            }

        with patch.object(self.plugin.model_hub, "call_llm_json", new=fake_call_llm_json):
            cards, source = await self.plugin.soul_manager.generate_action_cards(soul)

        assert source == "ai"
        # The model returned only 2 usable cards -> padded to the fixed 2/3/2/2
        # ratio with deterministic fallback cards (9 total, all four types).
        assert len(cards) == 9
        actions = [card.action for card in cards]
        assert set(actions) == set(ActionType)
        assert actions.count(ActionType.SLEEP) == 2
        assert actions.count(ActionType.BUSY_TASK) == 3
        assert actions.count(ActionType.SURF) == 2
        assert actions.count(ActionType.IDLE_ROAM) == 2
        titles = {card.title for card in cards}
        assert "守着限定联名外卖开抢" in titles
        assert "帮群友代肝活动" in titles
        assert len({card.card_id for card in cards}) == 9
        for card in cards:
            assert card.armor == FIXED_ARMOR[card.action.value]
            assert "valid_slots" not in card.to_dict()
            _assert_clean_title(self, card.title)

        prompt = str(captured.get("prompt") or "")
        assert captured.get("role") == "decision"
        assert soul.name in prompt
        assert "normal" in prompt  # chronotype injected
        assert "base_weight" in prompt
        assert "armor" not in prompt.lower()

    async def test_prompt_injects_occupation_routine_hobbies_and_city(self):
        soul = await self.first_soul()
        soul.card["identity"]["occupation"] = "自由插画师"
        soul.card["identity"]["city"] = "杭州"
        soul.card["interests"]["hobbies"] = ["手冲咖啡", "摄影"]
        soul.card["rhythm"]["daily_routine"] = "白天赶稿，晚上刷视频"
        captured = {}

        async def fake_call_llm_json(**kwargs):
            captured.update(kwargs)
            return {"state_event_cards": []}

        with patch.object(self.plugin.model_hub, "call_llm_json", new=fake_call_llm_json):
            await self.plugin.soul_manager.generate_action_cards(soul)

        prompt = str(captured.get("prompt") or "")
        for snippet in ("自由插画师", "杭州", "手冲咖啡", "白天赶稿"):
            assert snippet in prompt, snippet

    async def test_offline_generation_falls_back_to_clean_ten_cards(self):
        soul = await self.first_soul()
        cards, source = await self.plugin.soul_manager.generate_action_cards(soul)
        assert source == "fallback"
        assert len(cards) == 10
        for card in cards:
            _assert_clean_title(self, card.title)

    async def test_regenerate_replaces_old_pool_and_persists(self):
        soul = await self.first_soul()
        old = StateEventCard(
            card_id="evt_old_custom",
            title="旧的自定义卡",
            action_type=ActionType.IDLE_ROAM,
            min_hours=0.5,
            max_hours=1.5,
        )
        soul.state_event_cards = [old]
        await self.plugin.soul_manager.save_soul(soul)

        cards, source = await self.plugin.soul_manager.regenerate_action_cards(
            soul.soul_id
        )
        assert source == "fallback"
        assert cards

        reloaded = await self.plugin.soul_manager.get_soul_by_id(soul.soul_id)
        assert [card.card_id for card in reloaded.state_event_cards] == [
            card.card_id for card in cards
        ]
        assert all(card.card_id != "evt_old_custom" for card in reloaded.state_event_cards)


# ==============================================================================
# 4. Web API: schedule/reroll keeps today's timeline, replaces the card pool
# ==============================================================================
class TestScheduleReroll(WebApiTestBase):
    async def test_reroll_keeps_today_timeline_and_replaces_cards(self):
        soul = await self.first_soul()
        old = StateEventCard(
            card_id="evt_old_custom",
            title="旧的自定义卡",
            action_type=ActionType.BUSY_TASK,
            min_hours=1.0,
            max_hours=2.0,
        )
        soul.state_event_cards = list(soul.state_event_cards) + [old]
        await self.plugin.soul_manager.save_soul(soul)

        # Page load materializes today's 24h timeline before the re-roll click.
        detail = self.assert_ok(
            await self.call(
                "GET", self.api.api_souls_detail, query={"soul_id": soul.soul_id}
            )
        )
        before_slots = detail["schedule"]["run"]["slots"]
        assert len(before_slots) >= 7

        response = await self.call(
            "POST", self.api.api_schedule_reroll, body={"soul_id": soul.soul_id}
        )
        data = self.assert_ok(response)
        assert "今日时间轴保持不变" in response["message"]
        assert data["card_source"] == "fallback"
        assert data["cards"] == [
            card.to_dict()
            for card in (
                await self.plugin.soul_manager.get_soul_by_id(soul.soul_id)
            ).state_event_cards
        ]
        titles = [card["title"] for card in data["cards"]]
        assert "旧的自定义卡" not in titles
        for card in data["cards"]:
            assert "valid_slots" not in card
            _assert_clean_title(self, card["title"])

        after_slots = data["schedule"]["run"]["slots"]
        assert [
            (slot["slot_key"], slot["start_hour"], slot["end_hour"], slot["title"])
            for slot in before_slots
        ] == [
            (slot["slot_key"], slot["start_hour"], slot["end_hour"], slot["title"])
            for slot in after_slots
        ]

    async def test_reroll_materializes_timeline_before_replacing_pool(self):
        soul = await self.first_soul()
        # First action ever: reroll -> must materialize today's timeline itself.
        data = self.assert_ok(
            await self.call(
                "POST", self.api.api_schedule_reroll, body={"soul_id": soul.soul_id}
            )
        )
        slots = data["schedule"]["run"]["slots"]
        assert 7 <= len(slots) <= 11
        assert slots[0]["start_hour"] == 0.0 and slots[-1]["end_hour"] == 24.0

        detail = self.assert_ok(
            await self.call(
                "GET", self.api.api_souls_detail, query={"soul_id": soul.soul_id}
            )
        )
        after = detail["schedule"]["run"]["slots"]
        assert [
            (slot["slot_key"], slot["title"], slot["action_type"]) for slot in slots
        ] == [
            (slot["slot_key"], slot["title"], slot["action_type"]) for slot in after
        ]


# ==============================================================================
# 5. Web API: cards/ai_generate replaces the pool instead of stacking cards
# ==============================================================================
class TestCardsAiGenerateReplace(WebApiTestBase):
    async def test_auto_save_replaces_pool_instead_of_appending(self):
        soul = await self.first_soul()
        old = StateEventCard(
            card_id="evt_old_custom",
            title="旧卡保留测试",
            action_type=ActionType.IDLE_ROAM,
            min_hours=0.5,
            max_hours=1.5,
        )
        soul.state_event_cards = [old]
        await self.plugin.soul_manager.save_soul(soul)

        async def fake_call_llm_json(**kwargs):
            return {
                "state_event_cards": [
                    {
                        "card_id": "ai_1",
                        "title": "窝在沙发刷搞笑视频",
                        "action_type": "SURF",
                        "min_hours": 0.5,
                        "max_hours": 2.0,
                        "base_weight": 70,
                    },
                    {
                        "card_id": "ai_2",
                        "title": "专注处理手头正事",
                        "action_type": "BUSY_TASK",
                        "min_hours": 1.0,
                        "max_hours": 3.0,
                        "base_weight": 60,
                    },
                    {
                        "card_id": "ai_3",
                        "title": "夜间关灯沉睡休息",
                        "action_type": "SLEEP",
                        "min_hours": 4.5,
                        "max_hours": 7.5,
                        "base_weight": 80,
                    },
                ]
            }

        with patch.object(self.plugin.model_hub, "call_llm_json", new=fake_call_llm_json):
            data = self.assert_ok(
                await self.call(
                    "POST",
                    self.api.api_cards_ai_generate,
                    body={"soul_id": soul.soul_id, "auto_save": True},
                )
            )

        assert data["replaced"] is True
        assert data["card_source"] == "ai"
        # 3 AI cards were padded to the fixed 9-card four-type ratio.
        assert len(data["generated_cards"]) == 9
        assert {card["action_type"] for card in data["generated_cards"]} == set(FIXED_ARMOR)
        assert [card["card_id"] for card in data["custom_cards"]] == [
            card["card_id"] for card in data["generated_cards"]
        ]
        assert all(
            card["title"] != "旧卡保留测试" for card in data["custom_cards"]
        )
        for card in data["custom_cards"]:
            assert card["armor"] == FIXED_ARMOR[card["action_type"]]
            assert "valid_slots" not in card

        reloaded = await self.plugin.soul_manager.get_soul_by_id(soul.soul_id)
        assert len(reloaded.state_event_cards) == 9
        assert all(
            card.card_id != "evt_old_custom" for card in reloaded.state_event_cards
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
