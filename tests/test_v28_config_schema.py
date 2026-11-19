"""v2.13 配置中心回归：配置职责全部收拢到 ``PluginConfigManager``。

冻结以下契约：

1. 仓库不再有 ``_conf_schema.json`` —— AstrBot 原生插件配置面板退场，Pages
   「设置」页是唯一配置入口（保存后原地热更新，不重载插件）；
2. 6 大数据分组齐全（schedule / proactive / atmosphere / memory / relationship /
   surfing），每组都有「常规 + 高级」两层字段；
3. 每个字段都有短标题（``label``）、完整说明（``hint``）、合法默认值与类型；
4. 不当开关不回流：``surfing_config`` 全局冲浪开关、``relationship_config`` 用户名单
   仍由 Pages / Soul JSON 唯一管理。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _helpers import load_plugin_modules  # noqa: E402

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
mods = load_plugin_modules()
config_manager = mods.config_manager

#: 普通配置中禁止再出现的、与 Pages / Soul JSON 职责重叠的键。
FORBIDDEN_PLUGIN_CONFIG_KEYS = {
    "surfing_config": {
        "surfing_enabled",
        "enable_bili_surfing",
        "enable_douyin_surfing",
    },
    "relationship_config": {"core_user_ids", "important_user_ids"},
}


class TestRuntimeConfigSchema(unittest.TestCase):
    def test_legacy_conf_schema_file_is_gone(self):
        assert not os.path.exists(os.path.join(PROJECT_ROOT, "_conf_schema.json"))

    def test_six_sections_and_two_tiers(self):
        sections = config_manager.SECTIONS
        assert [item.id for item in sections] == [
            "schedule",
            "proactive",
            "atmosphere",
            "memory",
            "relationship",
            "surfing",
        ]
        for section in sections:
            assert section.title and section.icon and section.description
            assert section.fields
            assert any(not field.advanced for field in section.fields), section.id
            assert any(field.advanced for field in section.fields), section.id

    def test_storage_groups_keep_legacy_nesting(self):
        """The shared config dict stays compatible with every existing reader."""
        groups = {section.storage_group for section in config_manager.SECTIONS}
        assert groups == {
            "roguelite_schedule_config",
            "atmosphere_config",
            "memory_and_context_config",
            "relationship_config",
            "surfing_config",
        }
        # Group B (proactive / unread) intentionally shares the roguelite group.
        proactive = config_manager.SECTION_MAP["proactive"]
        assert proactive.storage_group == "roguelite_schedule_config"

    def test_no_pages_overlapping_switches_in_runtime_config(self):
        defaults = config_manager.default_values()
        for group, forbidden in FORBIDDEN_PLUGIN_CONFIG_KEYS.items():
            assert not (set(defaults.get(group) or {}) & forbidden), (
                f"{group} 不应包含 {sorted(set(defaults.get(group) or {}) & forbidden)}："
                "这些能力由 Pages / Soul JSON 唯一管理"
            )

    def test_auto_promotion_curve_is_behavior_only(self):
        fields = config_manager.SECTION_MAP["relationship"].field_map()
        assert "enable_auto_promotion_l2" in fields
        assert fields["enable_auto_promotion_l2"].type == "bool"
        assert fields["auto_promote_l2_min_interactions"].default == 40
        assert fields["auto_promote_l2_min_affinity"].default == 30.0
        assert not ({"core_user_ids", "important_user_ids"} & set(fields))

    def test_every_item_has_label_hint_and_legal_default(self):
        for section in config_manager.SECTIONS:
            for field in section.fields:
                assert field.label, field.key
                assert len(field.label) <= 20, field.label
                assert field.hint, field.key
                assert field.type in config_manager.FIELD_TYPES, field.key
                value = config_manager.coerce_field_value(field, field.default)
                assert value == field.default, field.key

    def test_advanced_ranges_are_clamped(self):
        field = config_manager.SECTION_MAP["schedule"].field_map()["heartbeat_interval_minutes"]
        assert config_manager.coerce_field_value(field, 0) == 5
        assert config_manager.coerce_field_value(field, 999) == 60
        assert config_manager.coerce_field_value(field, "20") == 20
        ratio = config_manager.SECTION_MAP["memory"].field_map()["recall_bucket_ratios"]
        assert config_manager.coerce_field_value(ratio, {"3": 50, "2": 50, "1": 0}) == {
            "3": 50,
            "2": 50,
            "1": 0,
        }
        assert config_manager.coerce_field_value(ratio, {}) == dict(
            config_manager.DEFAULT_RECALL_BUCKET_RATIOS
        )
        keywords = config_manager.SECTION_MAP["schedule"].field_map()["urgent_keywords"]
        assert config_manager.coerce_field_value(keywords, "急, 救命，大瓜") == [
            "急",
            "救命",
            "大瓜",
        ]

    def test_impact_table_merges_defaults(self):
        merged = config_manager.normalize_impact_table(
            {"sleep_wake_urgent": 120, "busy_l1": -5}
        )
        assert merged["sleep_wake_urgent"] == 100
        assert merged["busy_l1"] == 0
        assert merged["prelude_l3"] == 75


if __name__ == "__main__":
    unittest.main()
