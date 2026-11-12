"""v2.8.2 配置职责切分回归：普通配置只留运行参数，用户级 / 角色级能力交给 Pages。

冻结以下契约（防止以后又把与 Pages 冲突的开关加回 AstrBot 插件配置）：

1. ``surfing_config`` 中不存在全局 ``surfing_enabled`` —— 冲浪总开关由 Pages
   「冲浪」页按角色写入 Soul JSON ``settings.surfing_enabled``，是唯一真源；
2. ``relationship_config`` 中不存在 ``core_user_ids`` / ``important_user_ids`` ——
   用户等级 / 名单存 SQLite ``users.tier``，由 Pages「关系与群聊」页管理，L3 仅由
   平台 ``admins_id`` 自动确定；仅保留 ``enable_auto_promotion_l2`` 行为开关；
3. 每个配置项都有**短标题**（``description``）与完整说明（``hint``）——标题过长会
   让插件普通配置面板拥挤（v2.8.2 全部改写）。
"""

import json
import os
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEMA_PATH = os.path.join(PROJECT_ROOT, "_conf_schema.json")

#: 普通配置中禁止再出现的、与 Pages / Soul JSON 职责重叠的键。
FORBIDDEN_PLUGIN_CONFIG_KEYS = {
    "surfing_config": {"surfing_enabled"},
    "relationship_config": {"core_user_ids", "important_user_ids"},
}


class TestPluginConfigSchema(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(SCHEMA_PATH, "r", encoding="utf-8") as handle:
            cls.schema = json.load(handle)

    def test_no_pages_overlapping_switches_in_plugin_config(self):
        for section, forbidden in FORBIDDEN_PLUGIN_CONFIG_KEYS.items():
            items = (self.schema.get(section) or {}).get("items") or {}
            assert not (set(items) & forbidden), (
                f"{section} 不应包含 {sorted(set(items) & forbidden)}："
                "这些能力由 Pages / Soul JSON 唯一管理"
            )

    def test_auto_promotion_switch_remains_behavior_only(self):
        items = self.schema["relationship_config"]["items"]
        assert set(items) == {"enable_auto_promotion_l2"}
        assert items["enable_auto_promotion_l2"]["type"] == "bool"

    def test_every_item_has_short_description_and_hint(self):
        for section, section_cfg in self.schema.items():
            assert section_cfg.get("type") == "object", section
            for key, item in (section_cfg.get("items") or {}).items():
                description = str(item.get("description") or "").strip()
                hint = str(item.get("hint") or "").strip()
                assert description, f"{section}.{key} 缺少 title（description）"
                assert len(description) <= 24, (
                    f"{section}.{key} 标题过长（{len(description)} 字符）："
                    "详细说明应放入 hint"
                )
                assert hint, f"{section}.{key} 缺少 hint（详细说明）"

    def test_memory_switch_has_documented_add_semantics(self):
        """启用/关闭分级记忆的语义必须在 hint 里可被用户读到。"""
        item = self.schema["memory_and_context_config"]["items"]["enable_tiered_memory"]
        hint = str(item.get("hint") or "")
        assert "写入" in hint, hint
        assert item["default"] is True


if __name__ == "__main__":
    unittest.main()
