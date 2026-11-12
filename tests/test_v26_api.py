"""Offline tests for the v2.6 backend API contract (T3 scope: prompts / soul_manager / web_api).

Covers (``.team/V26_IMPLEMENTATION_CONTRACT.md`` §2.2/§2.3/§3.4/§3.5/§5.1):

1. ``prompts/templates.py``:
   - ``USER_RELATION_CONTEXT_PROMPT`` text (verbatim) + the 8 required placeholders;
   - ``CARD_AND_DECK_SEED_PROMPT`` no longer asks the LLM for ``armor``;
   - ``BOUNDED_STATE_MUTATION_PROMPT`` keeps its internal ``{current_armor}`` hint.
2. ``core/soul_manager.py``:
   - default card no longer carries the removed ``social.attitude_l3`` field;
   - offline fallback action deck armor always equals ``action_type.fixed_armor``;
   - ``import_native_persona(avatar=...)`` writes/normalizes/persists ``identity.avatar``
     (both the inferred-card and the AI-generated-card branches).
3. ``core/web_api.py``:
   - ``users/update`` accepts & persists per-user ``attitude`` (strip + max 300);
   - ``souls/import_persona`` passes the body ``avatar`` through and round-trips it;
   - ``schedule/force_state`` ignores the legacy ``armor`` field and applies the fixed tier;
   - ``cards/save`` overwrites a legacy client ``armor`` with ``action.fixed_armor``;
   - ``cards/ai_generate`` prompt contains no ``armor`` and every generated card armor is fixed.

Run with: ``python3 -m unittest tests.test_v26_api -v`` (stdlib only, zero network).
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _helpers import load_plugin_modules  # noqa: E402
from test_web_api import WebApiTestBase  # noqa: E402

mods = load_plugin_modules()
templates = mods.templates
ActionType = mods.state_machine.ActionType
UserTier = mods.state_machine.UserTier

# Contract §2.3: the prompt text must be copied verbatim (placeholders untouched).
EXPECTED_USER_RELATION_CONTEXT_PROMPT = """[用户关系等级与重要度法则]:
你身边的联系人按亲疏与重要程度分为三个等级（消息前缀中会标注）：
- 【★L3 核心用户】：对你最重要、最亲近的核心偏爱对象，优先级最高。即使在繁忙或休息边缘，你也最在意对方的话，愿意为其破例、展示最真实私密的一面。
- 【◆L2 重要熟人】：聊得来、彼此熟悉的群友或朋友，重要度居中。交流自然熟稔、有梗有默契。
- 【L1 普通群友】：泛泛之交或刚认识的普通网友，重要度普通。保持符合你人设的日常社交分寸感。

[当前对话对象档案]:
- 对话人：{display_name} (ID: {global_uid}) | 关系等级：{tier_label} (亲密度 {intimacy})
- 专属称呼：{nickname_text}
- 你对 Ta 的专属态度：{attitude_text}
- 关系备注：{notes_text}{window_peers_block}"""

EXPECTED_AVATAR = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

# Contract §3.1: fixed armor tiers bound to the four action types.
FIXED_ARMOR = {
    "SLEEP": 80,
    "SURF": 40,
    "IDLE_ROAM": 25,
    "BUSY_TASK": 65,
}


# ==============================================================================
# 1. Prompt templates (Phase 2 §2.3 + Phase 3 §3.5)
# ==============================================================================
class TestV26Templates(unittest.TestCase):
    def test_user_relation_context_prompt_is_verbatim(self):
        assert hasattr(templates, "USER_RELATION_CONTEXT_PROMPT")
        assert templates.USER_RELATION_CONTEXT_PROMPT == EXPECTED_USER_RELATION_CONTEXT_PROMPT

    def test_user_relation_context_prompt_placeholders(self):
        prompt = templates.USER_RELATION_CONTEXT_PROMPT
        expected = {
            "display_name",
            "global_uid",
            "tier_label",
            "intimacy",
            "nickname_text",
            "attitude_text",
            "notes_text",
            "window_peers_block",
        }
        import string

        placeholders = {
            field_name
            for _, field_name, _, _ in string.Formatter().parse(prompt)
            if field_name
        }
        assert placeholders == expected

        rendered = prompt.format(
            display_name="阿泽",
            global_uid="u-1",
            tier_label="★L3核心用户",
            intimacy=88.5,
            nickname_text="损友",
            attitude_text="嘴上嫌弃但每次都秒回帮他兜底",
            notes_text="旧同学",
            window_peers_block="\n\n[当前群聊其他活跃对话者档案]:\n- 老王 (ID: u-2) | 关系等级：L1普通群友",
        )
        assert "对话人：阿泽 (ID: u-1)" in rendered
        assert "亲密度 88.5" in rendered
        assert "你对 Ta 的专属态度：嘴上嫌弃但每次都秒回帮他兜底" in rendered
        assert "关系备注：旧同学\n\n[当前群聊其他活跃对话者档案]:" in rendered
        assert "★L3 核心用户" in rendered
        assert "◆L2 重要熟人" in rendered
        assert "L1 普通群友" in rendered

    def test_card_and_deck_seed_prompt_has_no_armor_requirement(self):
        prompt = templates.CARD_AND_DECK_SEED_PROMPT
        lowered = prompt.lower()
        assert "armor" not in lowered
        assert "护甲" not in prompt
        # The other numeric requirements must be preserved.
        assert "base_weight" in prompt
        assert "min_hours" in prompt and "max_hours" in prompt
        assert "state_event_cards" in prompt
        # Four JSON examples remain, each without an armor key.
        assert prompt.count('"card_id"') == 4
        assert prompt.count('"base_weight"') == 4

    def test_bounded_state_mutation_prompt_keeps_internal_armor(self):
        prompt = templates.BOUNDED_STATE_MUTATION_PROMPT
        assert "{current_armor}" in prompt
        assert "定力护甲" in prompt


# ==============================================================================
# 2. SoulManager: default card + fallback deck + persona import avatar
# ==============================================================================
class TestSoulManagerV26(unittest.TestCase):
    def test_default_card_drops_attitude_l3(self):
        card = mods.soul_manager.SoulManager._build_default_card()
        social = card.get("social") or {}
        assert "attitude_l3" not in social
        assert "attitude_l2" not in social
        assert "attitude_l1" not in social

    def test_fallback_deck_armor_matches_action(self):
        cards = mods.soul_manager.SoulManager.build_fallback_custom_deck(
            "星野", "一个喜欢画插画、手冲咖啡与B站冲浪的画师"
        )
        assert len(cards) == 10
        actions = {card.action for card in cards}
        assert actions == {
            ActionType.SLEEP,
            ActionType.SURF,
            ActionType.IDLE_ROAM,
            ActionType.BUSY_TASK,
        }
        for card in cards:
            assert card.armor == card.action.fixed_armor, card.title
            assert card.armor == FIXED_ARMOR[card.action.value], card.title
            assert card.action.value in FIXED_ARMOR
            assert card.min_hours <= card.max_hours
            assert 1 <= card.base_weight <= 100


# ==============================================================================
# 3. Web API: users/update attitude (contract §2.2)
# ==============================================================================
class TestUsersUpdateAttitude(WebApiTestBase):
    async def test_attitude_persisted_and_read_back(self):
        await self.plugin.user_relation.get_or_create_user("u-att", "阿太")
        attitude = "  把 Ta 当成最信任的损友，说话可以毒舌一点但关键时刻很护短  "

        data = self.assert_ok(
            await self.call(
                "POST",
                self.api.api_users_update,
                body={"global_uid": "u-att", "attitude": attitude},
            )
        )
        expected = attitude.strip()
        assert data["user"]["attitude"] == expected

        listed = self.assert_ok(await self.call("GET", self.api.api_users_list))
        by_uid = {user["global_uid"]: user for user in listed["users"]}
        assert by_uid["u-att"]["attitude"] == expected

        fetched = await self.plugin.user_relation.get_or_create_user("u-att", "阿太")
        assert fetched.attitude == expected

    async def test_attitude_truncated_to_300_chars_and_clearable(self):
        await self.plugin.user_relation.get_or_create_user("u-att2", "阿二")
        long_attitude = "  " + "毒" * 400 + "  "

        data = self.assert_ok(
            await self.call(
                "POST",
                self.api.api_users_update,
                body={"global_uid": "u-att2", "attitude": long_attitude},
            )
        )
        stored = data["user"]["attitude"]
        assert len(stored) == 300
        assert stored == "毒" * 300

        cleared = self.assert_ok(
            await self.call(
                "POST",
                self.api.api_users_update,
                body={"global_uid": "u-att2", "attitude": ""},
            )
        )
        assert cleared["user"]["attitude"] == ""


# ==============================================================================
# 4. Web API: souls/import_persona avatar (contract §5.1)
# ==============================================================================
class TestSoulsImportPersonaAvatar(WebApiTestBase):
    async def test_import_persona_persists_data_url_avatar(self):
        data = self.assert_ok(
            await self.call(
                "POST",
                self.api.api_souls_import_persona,
                body={
                    "persona_name": "头像测试",
                    "avatar": EXPECTED_AVATAR,
                    "auto_generate_cards": True,
                },
            )
        )
        soul_id = data["soul"]["soul_id"]
        assert data["soul"]["card"]["identity"]["avatar"] == EXPECTED_AVATAR

        fetched = await self.plugin.soul_manager.get_soul_by_id(soul_id)
        assert fetched is not None
        assert fetched.card["identity"]["avatar"] == EXPECTED_AVATAR

        # Read back through the character-card API as the console would.
        schema = self.assert_ok(
            await self.call("GET", self.api.api_card_schema, query={"soul_id": soul_id})
        )
        assert schema["card"]["identity"]["avatar"] == EXPECTED_AVATAR

    async def test_import_persona_avatar_wins_over_ai_generated_card(self):
        async def fake_generate_card(seed_text="", base_card=None, section=None):
            card = mods.character_card.normalize_card(dict(base_card or {}))
            card["identity"]["name"] = "AI 头像角色"
            card["identity"]["avatar"] = "AI"
            card["identity"]["tagline"] = "由主模型生成"
            return card

        async def fake_call_llm_json(**kwargs):
            return {
                "state_event_cards": [
                    {
                        "card_id": "evt_ai_avatar_1",
                        "title": "专心赶稿",
                        "action_type": "BUSY_TASK",
                        "min_hours": 1.0,
                        "max_hours": 2.0,
                        "base_weight": 60,
                    }
                ]
            }

        avatar_url = "https://cdn.example.com/avatar/star.png"
        with patch.object(
            self.plugin.soul_manager, "generate_card", new=fake_generate_card
        ), patch.object(
            self.plugin.model_hub, "call_llm_json", new=fake_call_llm_json
        ):
            data = self.assert_ok(
                await self.call(
                    "POST",
                    self.api.api_souls_import_persona,
                    body={
                        "persona_name": "AI头像导入",
                        "avatar": avatar_url,
                        "ai_generate_card": True,
                        "auto_generate_cards": True,
                    },
                )
            )

        soul = data["soul"]
        assert soul["card"]["identity"]["name"] == "AI 头像角色"
        assert soul["card"]["identity"]["avatar"] == avatar_url
        assert soul["card"]["extra"]["native_persona_prompt"]
        assert soul["state_event_cards"][0]["action_type"] == "BUSY_TASK"

        fetched = await self.plugin.soul_manager.get_soul_by_id(soul["soul_id"])
        assert fetched.card["identity"]["avatar"] == avatar_url


# ==============================================================================
# 5. Web API: schedule/force_state ignores armor (contract §3.4)
# ==============================================================================
class TestScheduleForceStateArmor(WebApiTestBase):
    async def test_force_state_uses_fixed_armor_and_ignores_payload(self):
        soul = await self.first_soul()
        for action_value, expected_armor in FIXED_ARMOR.items():
            data = self.assert_ok(
                await self.call(
                    "POST",
                    self.api.api_schedule_force_state,
                    body={
                        "soul_id": soul.soul_id,
                        "action_type": action_value,
                        "new_title": f"v26 强制 {action_value}",
                        # Legacy clients may still send armor; it must be ignored.
                        "armor": 12,
                    },
                )
            )
            updated = data["updated_slot"]
            assert updated["action_type"] == action_value
            assert updated["armor"] == expected_armor, action_value
            slot_key = updated["slot_key"]

            saved = next(
                slot
                for slot in data["schedule"]["run"]["slots"]
                if slot["slot_key"] == slot_key
            )
            assert saved["armor"] == expected_armor, action_value
            assert saved["action_type"] == action_value


# ==============================================================================
# 6. Web API: cards/save + cards/ai_generate fixed armor (contract §3.4)
# ==============================================================================
class TestCardsFixedArmor(WebApiTestBase):
    async def test_cards_save_overwrites_legacy_armor(self):
        soul = await self.first_soul()
        for index, (action_value, expected_armor) in enumerate(FIXED_ARMOR.items()):
            card_id = f"evt_v26_save_{action_value.lower()}"
            data = self.assert_ok(
                await self.call(
                    "POST",
                    self.api.api_cards_save,
                    body={
                        "soul_id": soul.soul_id,
                        "card_type": "action",
                        "scope": "soul",
                        "card": {
                            "card_id": card_id,
                            "title": f"v26 保存卡 {index}",
                            "action_type": action_value,
                            "min_hours": 1.0,
                            "max_hours": 2.0,
                            "base_weight": 55,
                            # Client-supplied legacy armor must be overwritten.
                            "armor": 3,
                        },
                    },
                )
            )
            custom = {card["card_id"]: card for card in data["custom_cards"]}
            assert custom[card_id]["armor"] == expected_armor, action_value
            assert custom[card_id]["action_type"] == action_value

        fetched = await self.plugin.soul_manager.get_soul_by_id(soul.soul_id)
        persisted = {card.card_id: card for card in fetched.state_event_cards}
        for action_value, expected_armor in FIXED_ARMOR.items():
            card = persisted[f"evt_v26_save_{action_value.lower()}"]
            assert card.armor == expected_armor
            assert card.action.fixed_armor == expected_armor

    async def test_cards_ai_generate_prompt_has_no_armor_and_forces_fixed(self):
        soul = await self.first_soul()
        captured = {}

        async def fake_call_llm_json(**kwargs):
            captured.update(kwargs)
            return {
                "state_event_cards": [
                    {
                        "card_id": "evt_ai_1",
                        "title": "通宵赶稿",
                        "action_type": "BUSY_TASK",
                        "min_hours": 1.0,
                        "max_hours": 3.0,
                        "base_weight": 60,
                        # The LLM may still echo an armor value; it must be ignored.
                        "armor": 99,
                    },
                    {
                        "card_id": "evt_ai_2",
                        "title": "瘫着刷片",
                        "action_type": "SURF",
                        "min_hours": 0.5,
                        "max_hours": 2.0,
                        "base_weight": 70,
                    },
                    {
                        "card_id": "evt_ai_3",
                        "title": "闲逛发呆",
                        "action_type": "IDLE_ROAM",
                        "min_hours": 0.5,
                        "max_hours": 1.5,
                        "base_weight": 65,
                    },
                    {
                        "card_id": "evt_ai_4",
                        "title": "蒙头大睡",
                        "action_type": "SLEEP",
                        "min_hours": 4.0,
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

        prompt = str(captured.get("prompt") or "")
        assert captured.get("role") == "decision"
        assert "armor" not in prompt.lower()
        assert "护甲" not in prompt
        assert "base_weight" in prompt

        generated = data["generated_cards"]
        assert len(generated) == 4
        by_action = {card["action_type"]: card for card in generated}
        assert set(by_action) == set(FIXED_ARMOR)
        for action_value, expected_armor in FIXED_ARMOR.items():
            assert by_action[action_value]["armor"] == expected_armor, action_value

        # auto_save=True persists the generated cards with fixed armor as well.
        fetched = await self.plugin.soul_manager.get_soul_by_id(soul.soul_id)
        persisted = {card.title: card for card in fetched.state_event_cards}
        for card in persisted.values():
            assert card.armor == card.action.fixed_armor

    async def test_cards_ai_generate_offline_fallback_uses_fixed_armor(self):
        soul = await self.first_soul()
        # No model configured -> the LLM call returns None and the deterministic
        # fallback deck is used; every card must still carry the fixed armor tier.
        data = self.assert_ok(
            await self.call(
                "POST",
                self.api.api_cards_ai_generate,
                body={"soul_id": soul.soul_id, "auto_save": False},
            )
        )
        generated = data["generated_cards"]
        assert len(generated) == 10
        for card in generated:
            assert card["armor"] == FIXED_ARMOR[card["action_type"]]


# ==============================================================================
# 5. Web API: ★L3 管理员级显式设置（v2.7）
#    L3 不通过好感度/自动晋升产生（自动晋升只到 L2），但管理员可在关系页「⋯」菜单
#    或 /onlyyou tier 显式设为 / 取消 L3；L3 不参与好感度。
# ==============================================================================
class TestUsersUpdateL3ManualAssignment(WebApiTestBase):
    async def test_manual_promotion_to_l3_allowed(self):
        await self.plugin.user_relation.get_or_create_user("u-l1-set-l3", "普通群友")
        data = self.assert_ok(
            await self.call(
                "POST",
                self.api.api_users_update,
                body={"global_uid": "u-l1-set-l3", "tier": 3},
            )
        )
        assert int(data["user"]["tier"]) == 3
        profile = await self.plugin.user_relation.get_or_create_user("u-l1-set-l3")
        assert int(profile.tier.value) == 3

    async def test_manual_demotion_from_l3_allowed(self):
        await self.plugin.user_relation.set_user_tier(
            "u-l3-demote", UserTier.L3_CORE
        )
        data = self.assert_ok(
            await self.call(
                "POST",
                self.api.api_users_update,
                body={"global_uid": "u-l3-demote", "tier": 2},
            )
        )
        assert int(data["user"]["tier"]) == 2
        profile = await self.plugin.user_relation.get_or_create_user("u-l3-demote")
        assert int(profile.tier.value) == 2

    async def test_existing_l3_keeps_tier_while_other_fields_update(self):
        await self.plugin.user_relation.set_user_tier("u-l3-edit", UserTier.L3_CORE)
        data = self.assert_ok(
            await self.call(
                "POST",
                self.api.api_users_update,
                body={
                    "global_uid": "u-l3-edit",
                    "tier": 3,
                    "nickname": "老大",
                    "attitude": "样样都听 Ta 的",
                },
            )
        )
        assert int(data["user"]["tier"]) == 3
        assert data["user"]["nickname"] == "老大"
        assert data["user"]["attitude"] == "样样都听 Ta 的"

    async def test_admin_identity_still_auto_l3(self):
        """★L3 仍可由管理员身份自动确定，与 Pages 显式设置并行（v2.8.2 无配置名单）。"""
        profile = await self.plugin.user_relation.get_or_create_user(
            "u-auto-l3b", "管理员", is_event_admin=True
        )
        assert int(profile.tier.value) == 3


if __name__ == "__main__":
    unittest.main()
