"""Offline tests for ``core/character_card.py`` and ``SoulProfile`` compatibility (§3)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _helpers import card_with, load_plugin_modules  # noqa: E402

mods = load_plugin_modules()
cc = mods.character_card
SoulProfile = mods.soul_manager.SoulProfile

EXPECTED_SECTIONS = [
    "identity",
    "appearance",
    "personality",
    "background",
    "interests",
    "speech",
    "social",
    "rhythm",
    "examples",
    "extra",
]
EXPECTED_FIELD_COUNTS = {
    "identity": 9,
    "appearance": 2,
    "personality": 6,
    "background": 3,
    "interests": 4,
    "speech": 6,
    # v2.6: ``social`` keeps only ``group_role``; attitude_l3/l2/l1 moved to
    # per-user ``UserProfile.attitude`` (10 sections / 36 fields).
    "social": 1,
    "rhythm": 2,
    "examples": 1,
    "extra": 2,
}


def rich_card():
    return card_with(
        identity={
            "name": "星野",
            "nickname": "小星",
            "tagline": "住在隔壁、爱上网冲浪的邻家女孩",
            "gender": "女",
            "age": "21",
            "birthday": "3 月 14 日",
            "occupation": "独立插画师",
            "city": "杭州",
            "avatar": "🌙",
        },
        appearance={"appearance": "黑色短发，右耳银色小耳钉", "outfit_style": "宽松卫衣"},
        personality={
            "personality_tags": ["嘴硬心软", "慢热", "爱吐槽"],
            "mbti": "INFJ",
            "strengths": "共情力强",
            "weaknesses": "容易拖延",
            "temperament": "平时温和，被放鸽子会生闷气",
            "values": "真诚比体面重要",
        },
        background={
            "growth_story": "在小城长大，大学才来到大城市，一路磕磕绊绊",
            "family": "父母经营一家小面馆",
            "key_experiences": "高中时组过乐队",
        },
        interests={
            "hobbies": ["摄影", "citywalk", "打游戏"],
            "likes": "冰美式、雨天、猫",
            "dislikes": "香菜、被已读不回",
            "surfing_preferences": ["搞笑整活", "萌宠日常"],
        },
        speech={
            "speaking_style": "口语化短句，像熟人聊天，偶尔冷幽默",
            "catchphrases": ["好家伙", "离谱", "笑死"],
            "verbal_tics": "啦、诶嘿",
            "punctuation_habit": "几乎不用句号",
            "reply_length": "short",
            "banned_expressions": ["作为一个AI"],
        },
        social={
            # v2.6 retired per-tier attitudes: normalize_card must silently drop
            # these legacy keys while keeping ``group_role``.
            "attitude_l3": "会撒娇、直呼昵称，消息秒回",
            "attitude_l2": "熟络但不越界",
            "attitude_l1": "礼貌但有距离感",
            "group_role": "气氛组",
        },
        rhythm={"chronotype": "night_owl", "daily_routine": "晚上刷手机到一两点"},
        examples={
            "example_dialogs": [
                {"user": "在干嘛", "character": "刚下班，瘫着"},
                {"user": "吃了吗", "character": "还没呢，你请我？"},
            ]
        },
        extra={
            "extra_notes": "任何上面没覆盖到的设定",
            "native_persona_prompt": "原生人设原文：星野是一个爱笑的女孩",
        },
    )


class TestCardSchema(unittest.TestCase):
    def test_ten_sections_and_thirty_six_fields(self):
        assert len(cc.CARD_SCHEMA) == 10
        assert [section["id"] for section in cc.CARD_SCHEMA] == EXPECTED_SECTIONS
        assert cc.CARD_SECTION_IDS == EXPECTED_SECTIONS
        total_fields = 0
        for section in cc.CARD_SCHEMA:
            assert section["title"]
            assert isinstance(section["fields"], list)
            assert len(section["fields"]) == EXPECTED_FIELD_COUNTS[section["id"]]
            total_fields += len(section["fields"])
            keys = [field["key"] for field in section["fields"]]
            assert len(keys) == len(set(keys))
        # v2.6: ``social.attitude_l3/l2/l1`` were retired (moved to per-user
        # ``UserProfile.attitude``), so the schema is now 10 sections / 36 fields.
        assert total_fields == 36

    def test_field_definitions_are_well_formed(self):
        allowed_types = {"text", "textarea", "tags", "list", "select", "dialogs"}
        for section in cc.CARD_SCHEMA:
            for field in section["fields"]:
                assert set(field) >= {"key", "label", "type"}
                assert field["type"] in allowed_types
                assert field["key"] and field["label"]
                if field["type"] == "select":
                    options = field.get("options")
                    assert isinstance(options, list) and options
                    for option in options:
                        assert set(option) == {"value", "label"}
                        assert option["value"] and option["label"]
        native_field = next(
            f for f in cc.CARD_SCHEMA[9]["fields"] if f["key"] == "native_persona_prompt"
        )
        assert native_field.get("readonly") is True

    def test_empty_card_shape(self):
        card = cc.empty_card()
        assert set(card) == set(cc.CARD_SECTION_IDS)
        for section in cc.CARD_SCHEMA:
            data = card[section["id"]]
            assert set(data) == {field["key"] for field in section["fields"]}
            for field in section["fields"]:
                if field["type"] in ("tags", "list", "dialogs"):
                    assert data[field["key"]] == []
                else:
                    assert data[field["key"]] == ""


class TestNormalizeCard(unittest.TestCase):
    def test_none_and_garbage(self):
        assert cc.normalize_card(None) == cc.empty_card()
        assert cc.normalize_card("not-a-dict") == cc.empty_card()
        assert cc.normalize_card(123) == cc.empty_card()

    def test_tags_and_list_from_string(self):
        card = cc.normalize_card(
            {
                "personality": {"personality_tags": "嘴硬心软，慢热、爱吐槽/嘴硬心软"},
                "speech": {"catchphrases": "好家伙;离谱\n笑死"},
            }
        )
        assert card["personality"]["personality_tags"] == ["嘴硬心软", "慢热", "爱吐槽"]
        assert card["speech"]["catchphrases"] == ["好家伙", "离谱", "笑死"]

    def test_dialogs_three_shapes(self):
        pair_style = cc.normalize_card(
            {"examples": {"example_dialogs": [{"user": "在干嘛", "character": "刚下班"}]}}
        )
        assert pair_style["examples"]["example_dialogs"] == [
            {"user": "在干嘛", "character": "刚下班"}
        ]

        role_style = cc.normalize_card(
            {
                "examples": {
                    "example_dialogs": [
                        {"role": "user", "content": "在干嘛"},
                        {"role": "assistant", "content": "刚下班"},
                    ]
                }
            }
        )
        assert role_style["examples"]["example_dialogs"] == [
            {"user": "在干嘛", "character": "刚下班"}
        ]

        list_style = cc.normalize_card(
            {"examples": {"example_dialogs": [["在干嘛", "刚下班"]]}}
        )
        assert list_style["examples"]["example_dialogs"] == [
            {"user": "在干嘛", "character": "刚下班"}
        ]

        single_dict = cc.normalize_card(
            {"examples": {"example_dialogs": {"user": "吃了吗", "character": "没有"}}}
        )
        assert single_dict["examples"]["example_dialogs"] == [
            {"user": "吃了吗", "character": "没有"}
        ]

    def test_numbers_become_strings(self):
        card = cc.normalize_card({"identity": {"age": 21, "name": "星野"}})
        assert card["identity"]["age"] == "21"
        assert cc.normalize_card({"identity": {"age": 21.0}})["identity"]["age"] == "21"
        assert cc.normalize_card({"identity": {"age": 21.5}})["identity"]["age"] == "21.5"

    def test_unknown_keys_preserved(self):
        raw = {
            "identity": {"name": "星野", "future_field": {"x": 1}},
            "future_section": {"anything": [1, 2, 3]},
        }
        card = cc.normalize_card(raw)
        assert card["identity"]["future_field"] == {"x": 1}
        assert card["future_section"] == {"anything": [1, 2, 3]}

    def test_json_string_and_wrapper(self):
        card = cc.normalize_card('{"identity": {"name": "星野"}}')
        assert card["identity"]["name"] == "星野"
        wrapped = cc.normalize_card({"card": {"identity": {"name": "小星"}}})
        assert wrapped["identity"]["name"] == "小星"

    def test_does_not_mutate_input(self):
        raw = {"identity": {"name": "星野"}}
        cc.normalize_card(raw)
        assert raw == {"identity": {"name": "星野"}}


class TestMigrateLegacy(unittest.TestCase):
    def test_legacy_flat_profile(self):
        card = cc.migrate_legacy(
            {
                "name": "星野",
                "archetype": "爱上网冲浪的邻家女孩",
                "speaking_style": "口语化短句",
                "surfing_preferences": ["搞笑整活", "萌宠日常"],
                "native_persona_prompt": "原生人设原文",
            }
        )
        assert card["identity"]["name"] == "星野"
        assert card["identity"]["tagline"] == "爱上网冲浪的邻家女孩"
        assert card["speech"]["speaking_style"] == "口语化短句"
        assert card["interests"]["surfing_preferences"] == ["搞笑整活", "萌宠日常"]
        assert card["extra"]["native_persona_prompt"] == "原生人设原文"

    def test_card_takes_precedence(self):
        card = cc.migrate_legacy(
            {"name": "旧名", "card": {"identity": {"name": "新名"}}}
        )
        assert card["identity"]["name"] == "新名"

    def test_non_dict_returns_empty_card(self):
        assert cc.migrate_legacy(None) == cc.empty_card()


class TestCardCompletion(unittest.TestCase):
    def test_empty_card(self):
        completion = cc.card_completion(cc.empty_card())
        assert set(completion) == set(EXPECTED_SECTIONS)
        for section_id, info in completion.items():
            assert info["filled"] == 0
            assert info["total"] == EXPECTED_FIELD_COUNTS[section_id]

    def test_partial_card(self):
        completion = cc.card_completion(rich_card())
        assert completion["identity"] == {"filled": 9, "total": 9}
        assert completion["examples"] == {"filled": 1, "total": 1}
        assert cc.card_completion({"identity": {"name": "星野"}})["identity"]["filled"] == 1


class TestRenderPersonaPrompt(unittest.TestCase):
    def test_opens_with_persona_line(self):
        prompt = cc.render_persona_prompt(rich_card())
        assert prompt.startswith("你正在扮演「星野」。")
        assert "## 基础身份" in prompt
        assert "- 名字：星野" in prompt

    def test_skips_empty_fields_and_native_prompt(self):
        card = cc.normalize_card(
            {
                "identity": {"name": "星野"},
                "extra": {"native_persona_prompt": "这段原生人设不应该出现在渲染结果里"},
            }
        )
        prompt = cc.render_persona_prompt(card)
        assert prompt.startswith("你正在扮演「星野」。")
        assert "native_persona_prompt" not in prompt
        assert "这段原生人设不应该出现在渲染结果里" not in prompt
        # no dangling empty bullets
        for line in prompt.splitlines():
            assert not line.strip().endswith("：")

    def test_select_values_render_human_labels(self):
        prompt = cc.render_persona_prompt(rich_card())
        assert "男" not in [line.split("：")[-1] for line in prompt.splitlines()]
        assert "短句（20 字以内）" in prompt
        assert "夜猫子" in prompt

    def test_never_exceeds_budget(self):
        card = rich_card()
        for budget in (25, 50, 100, 250, 500, 1000, 2000):
            prompt = cc.render_persona_prompt(card, max_chars=budget)
            assert len(prompt) <= budget, "budget=%d len=%d" % (budget, len(prompt))

    def test_zero_budget_returns_empty(self):
        assert cc.render_persona_prompt(rich_card(), max_chars=0) == ""
        assert cc.render_persona_prompt(rich_card(), max_chars=-5) == ""

    def test_drops_low_priority_sections_first(self):
        prompt = cc.render_persona_prompt(rich_card(), max_chars=300)
        assert len(prompt) <= 300
        assert prompt.startswith("你正在扮演「星野」。")
        # examples/background/appearance are dropped before identity/speech
        assert "示例对话" not in prompt

    def test_handles_legacy_flat_profile(self):
        prompt = cc.render_persona_prompt(
            {"name": "星野", "archetype": "邻家女孩", "speaking_style": "口语化短句"}
        )
        assert prompt.startswith("你正在扮演「星野」。")

    def test_anonymous_name_fallback(self):
        prompt = cc.render_persona_prompt(cc.empty_card())
        assert prompt.startswith("你正在扮演「一个真实网友」")


class TestRenderCardSummary(unittest.TestCase):
    def test_summary_components(self):
        summary = cc.render_card_summary(rich_card())
        assert len(summary) <= 200
        assert "定位：" in summary
        assert "性格：" in summary
        assert "说话方式：" in summary
        assert "回复长度：" in summary
        assert "短句（20 字以内）" in summary

    def test_summary_budget(self):
        """Every max_chars must return in finite time and honour the hard cap.

        Regression for the 2026 fix: the trim loop used ``max(6, len(text)//2)`` and
        could stop converging at 7 chars (infinite loop) for budgets <= 40.
        """
        card = rich_card()
        for budget in (200, 100, 72, 60, 40, 30, 20, 10):
            summary = cc.render_card_summary(card, max_chars=budget)
            assert len(summary) <= budget, "budget=%d len=%d" % (budget, len(summary))

    def test_summary_tiny_budget_terminates(self):
        # Also covers budgets below the minimum achievable summary length.
        for budget in (10, 5, 1):
            summary = cc.render_card_summary(rich_card(), max_chars=budget)
            assert len(summary) <= budget, "budget=%d len=%d" % (budget, len(summary))

    def test_summary_default_budget(self):
        assert len(cc.render_card_summary(rich_card())) <= 200

    def test_summary_fallback_name(self):
        summary = cc.render_card_summary({"identity": {"name": "星野"}})
        assert "星野" in summary
        assert len(summary) <= 200

    def test_empty_summary(self):
        assert cc.render_card_summary(None) == ""


class TestSoulProfileCompatibility(unittest.TestCase):
    def test_legacy_flat_dict_is_migrated(self):
        soul = SoulProfile.from_dict(
            {
                "soul_id": "legacy_one",
                "name": "旧角色",
                "archetype": "旧定位",
                "speaking_style": "旧风格",
                "surfing_preferences": "搞笑整活，萌宠日常",
                "native_persona_prompt": "旧的原生人设",
            }
        )
        assert soul.soul_id == "legacy_one"
        assert soul.name == "旧角色"
        assert soul.archetype == "旧定位"
        assert soul.speaking_style == "旧风格"
        assert soul.surfing_preferences == ["搞笑整活", "萌宠日常"]
        assert soul.native_persona_prompt == "旧的原生人设"
        assert soul.card["identity"]["name"] == "旧角色"

    def test_flat_property_round_trip(self):
        soul = SoulProfile(soul_id="x", card=cc.empty_card())
        soul.name = "新名字"
        soul.archetype = "新定位"
        soul.speaking_style = "新风格"
        soul.surfing_preferences = ["新偏好"]
        soul.native_persona_prompt = "新原文"
        assert soul.card["identity"]["name"] == "新名字"
        assert soul.card["identity"]["tagline"] == "新定位"
        assert soul.card["speech"]["speaking_style"] == "新风格"
        assert soul.card["interests"]["surfing_preferences"] == ["新偏好"]
        assert soul.card["extra"]["native_persona_prompt"] == "新原文"

    def test_to_dict_emits_both_structures(self):
        soul = SoulProfile(soul_id="x", card=rich_card())
        payload = soul.to_dict()
        assert payload["soul_id"] == "x"
        assert payload["card"]["identity"]["name"] == "星野"
        # legacy flat fields kept for the old WebUI / API
        assert payload["name"] == "星野"
        assert payload["archetype"] == "住在隔壁、爱上网冲浪的邻家女孩"
        assert payload["speaking_style"] == "口语化短句，像熟人聊天，偶尔冷幽默"
        assert payload["surfing_preferences"] == ["搞笑整活", "萌宠日常"]
        assert payload["native_persona_prompt"].startswith("原生人设原文")
        assert payload["state_event_cards"] == []
        assert payload["encounter_talk_cards"] == []

    def test_from_dict_prefers_card(self):
        soul = SoulProfile.from_dict(
            {
                "soul_id": "x",
                "name": "扁平旧名",
                "card": {"identity": {"name": "卡片新名"}},
            }
        )
        assert soul.name == "卡片新名"

    def test_constructor_accepts_legacy_kwargs(self):
        soul = SoulProfile(
            soul_id="x", name="星野", archetype="邻家女孩", speaking_style="口语化"
        )
        assert soul.name == "星野"
        assert soul.archetype == "邻家女孩"
        assert soul.speaking_style == "口语化"


if __name__ == "__main__":
    unittest.main()
