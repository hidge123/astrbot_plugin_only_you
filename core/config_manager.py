"""集中式运行配置管理器 (`core/config_manager.py`).

OnlyYou 的唯一运行配置真源：SQLite ``plugin_settings`` 表的 ``runtime_config`` 键。
AstrBot 原生插件配置（``_conf_schema.json``）已退场，Pages「设置」页成为唯一入口。

设计要点
--------
1. **单一真源**：所有常规 / 高级运行参数都存在 ``plugin_settings["runtime_config"]``，
   按原 ``self.config`` 的嵌套分组结构组织，读写通过 :class:`PluginConfigManager`。
2. **无缝迁移**：首次启动把旧版 AstrBotConfig 里被用户改过的非默认值合并进来，并写入
   ``plugin_settings["legacy_astrbot_config_migrated"] = True``（一次成功后不再重跑）。
3. **原地热更新**：保存时先做 Schema 校验 / 范围钳制（Clamp），落盘 SQLite 后**原地**
   更新共享配置字典（``dict.clear/pop/update``），因此所有持有 ``plugin_config`` 引用的
   子模块无需重建即可读到新值；同时触发子模块 ``reload_config()`` 与插件心跳唤醒。
4. **常规 / 高级双层**：每个字段带 ``advanced`` 标记，Pages 设置页默认折叠高级参数。

六大数据分组（前端 section id → 存储分组）：

==========================  =====================================
section                      storage group
==========================  =====================================
``schedule``                 ``roguelite_schedule_config``
``proactive``                ``roguelite_schedule_config`` （历史兼容）
``atmosphere``               ``atmosphere_config``
``memory``                   ``memory_and_context_config``
``relationship``             ``relationship_config``
``surfing``                  ``surfing_config``
==========================  =====================================
"""

from __future__ import annotations

import inspect
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

try:
    from astrbot.api import logger
except ImportError:  # pragma: no cover - offline unit tests / no AstrBot container
    import logging

    logger = logging.getLogger("onlyyou.config_manager")


#: ``plugin_settings`` 键：全量运行配置。
RUNTIME_CONFIG_KEY = "runtime_config"

#: ``plugin_settings`` 键：旧版 AstrBotConfig 一次性迁移标记。
LEGACY_MIGRATED_KEY = "legacy_astrbot_config_migrated"

#: 配置结构版本（后续字段升级时可在 ``migrate_values`` 中按版本补齐）。
CONFIG_VERSION = 1

#: 字段类型白名单。
FIELD_TYPES = ("bool", "int", "float", "text", "secret", "list", "object", "ratios")

#: 默认紧急关键词表（原 core/schedule_mutator.URGENT_KEYWORDS）。
DEFAULT_URGENT_KEYWORDS = [
    "急",
    "快看",
    "大瓜",
    "救命",
    "睡了吗",
    "醒醒",
    "在不在",
    "难受",
    "离谱",
    "大事",
    "！！",
    "!!",
]

#: 默认冲击力表（原 core/schedule_mutator.compute_message_impact 中的硬编码分值）。
DEFAULT_MUTATION_IMPACT_TABLE: dict[str, int] = {
    "sleep_wake_urgent": 88,
    "sleep_wake_normal": 45,
    "busy_l1": 10,
    "busy_l3_direct": 78,
    "busy_l3_ambient": 62,
    "busy_l3_urgent_bonus": 12,
    "busy_l2_burst": 52,
    "busy_l2_single": 38,
    "busy_l2_urgent_bonus": 15,
    "prelude_l3": 75,
    "prelude_l2": 68,
}

#: 默认分层召回保底配额（T3 / T2 / T1，百分比）。
DEFAULT_RECALL_BUCKET_RATIOS: dict[str, int] = {"3": 30, "2": 40, "1": 30}

#: ``mutation_impact_table`` 子项范围（全部 0~100）。
_IMPACT_KEYS = tuple(DEFAULT_MUTATION_IMPACT_TABLE.keys())


@dataclass(frozen=True)
class ConfigField:
    """单个配置字段的 Schema 定义。"""

    key: str
    label: str
    type: str
    default: Any
    hint: str = ""
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    unit: str = ""
    advanced: bool = False
    #: bool / int / float / text / secret / list / object / ratios
    options: tuple[dict[str, Any], ...] = ()
    #: ``object`` 字段允许的子键（未列出时按默认对象合并钳制）。
    fields: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class ConfigSection:
    """前端「设置」页的数据分组。"""

    id: str
    title: str
    icon: str
    description: str
    storage_group: str
    fields: tuple[ConfigField, ...] = ()

    def field_map(self) -> dict[str, ConfigField]:
        return {item.key: item for item in self.fields}


def _f(
    key: str,
    label: str,
    type_: str,
    default: Any,
    hint: str = "",
    minimum: Optional[float] = None,
    maximum: Optional[float] = None,
    unit: str = "",
    advanced: bool = False,
    options: tuple[dict[str, Any], ...] = (),
    fields: tuple[dict[str, Any], ...] = (),
) -> ConfigField:
    return ConfigField(
        key=key,
        label=label,
        type=type_,
        default=default,
        hint=hint,
        minimum=minimum,
        maximum=maximum,
        unit=unit,
        advanced=advanced,
        options=options,
        fields=fields,
    )


def build_sections() -> tuple[ConfigSection, ...]:
    """返回 6 大分组的完整 Schema（顺序即前端展示顺序）。"""
    schedule = ConfigSection(
        id="schedule",
        title="运行节奏",
        icon="calendar",
        description="后台心跳节拍、24h 动态时间轴与日程微调的全局参数。",
        storage_group="roguelite_schedule_config",
        fields=(
            _f("enable_roguelite_schedule", "启用随机日程", "bool", True,
               "关闭后不再按角色作息曲线拼接 24h 动态时间轴，改用固定时段日程。"),
            _f("heartbeat_interval_minutes", "心跳间隔", "int", 15,
               "后台心跳主节拍（繁忙成果结算 / 冲浪 / 闲逛 / 未读补回）。保存后立即按新周期运行。",
               5, 60, "分钟"),
            _f("max_mutations_per_day", "每日微调上限", "int", 2,
               "根据用户消息或高分视频临时改变日程的每日次数上限。", 0, 20, "次"),
            _f("mutation_cooldown_minutes", "微调冷却", "int", 120,
               "两次日程微调之间的最短间隔，避免状态频繁横跳。", 0, 1440, "分钟"),
            _f("allow_l3_wake_from_resting", "允许 ★L3 唤醒", "bool", True,
               "★L3 核心用户私聊强呼叫时，允许在休息中切出清醒窗并回应。"),
            _f("gc_check_interval_hours", "GC 巡检周期", "int", 24,
               "记忆遗忘清理与冲浪历史容量裁剪的巡检周期。", 1, 168, "小时"),
            _f("urgent_keywords", "紧急关键词", "list", list(DEFAULT_URGENT_KEYWORDS),
               "命中任意词视为高冲击力的「紧急呼叫」，可按角色在角色卡高级项覆盖。", advanced=True),
            _f("surf_mutation_min_score", "刷片加钟分数线", "int", 88,
               "视觉看片评分达到该值以上时，触发「刷片上头」加钟微调。", 60, 100, "分", True),
            _f("burst_window_seconds", "连发判定窗口", "int", 300,
               "判定用户连续突发呼叫的时间窗口。", 60, 1800, "秒", True),
            _f("mutation_impact_table", "冲击力分值表", "object",
               dict(DEFAULT_MUTATION_IMPACT_TABLE),
               "各状态与关系等级的基础冲击力 / 连发加成，可按角色定力覆盖。",
               advanced=True,
               fields=tuple({"key": key, "label": key} for key in _IMPACT_KEYS)),
        ),
    )

    proactive = ConfigSection(
        id="proactive",
        title="主动搭话",
        icon="message",
        description="闲逛主动搭话、翻群吃瓜与私聊未读补回的概率、冷却与预算。",
        storage_group="roguelite_schedule_config",
        fields=(
            _f("idle_roam_cooldown_minutes", "闲逛搭话冷却", "int", 120,
               "空闲游荡（翻群吃瓜 / 人设话题 / 记忆联想）主动搭话的冷却时间；0 = 不冷却。",
               0, 1440, "分钟"),
            _f("idle_roam_trigger_probability", "闲逛触发概率", "int", 35,
               "冷却结束后按该概率决定是否真的主动搭话；0 = 不主动搭话。", 0, 100, "%"),
            _f("idle_roam_daily_max_count", "每日搭话上限", "int", 3,
               "每个角色每天最多主动搭话几次；0 = 不限制（仍受冷却与概率约束）。", 0, 50, "次"),
            _f("idle_roam_persona_topic_ratio", "人设话题占比", "int", 70,
               "主动搭话走「人设随机日常话题」的比例，其余走旧记忆联想。", 0, 100, "%"),
            _f("unread_private_reply_enabled", "私聊未读补回", "bool", True,
               "空闲时主动补回休息 / 繁忙期间错过的私聊消息。"),
            _f("unread_private_cooldown_minutes", "私聊补回冷却", "int", 60,
               "同一私聊会话两次主动补回的最小间隔；0 = 不冷却。", 0, 1440, "分钟"),
            _f("unread_private_ttl_minutes", "私聊未读有效期", "int", 720,
               "超过该时间的私聊未读不再主动补回。", 60, 4320, "分钟"),
            _f("unread_group_cooldown_minutes", "翻群冷却", "int", 60,
               "同一个群两次翻群吃瓜的最小间隔；0 = 不冷却。", 0, 1440, "分钟"),
            _f("unread_group_ttl_minutes", "群未读有效期", "int", 120,
               "超过该时间的群未读不再翻群吃瓜。", 30, 1440, "分钟"),
            _f("unread_max_per_chat", "单会话未读上限", "int", 20,
               "每个群 / 私聊会话最多保留的未读消息条数，超出时丢弃最旧。", 5, 100, "条"),
            _f("unread_private_catchup_per_tick", "单次补回会话数", "int", 2,
               "每次心跳最多补回几个私聊会话。", 1, 10, "个", True),
            _f("unread_private_scan_limit", "私聊扫描候选数", "int", 5,
               "每次私聊补回扫描的候选会话数。", 1, 20, "个", True),
            _f("unread_private_context_turns", "私聊补回上下文轮数", "int", 8,
               "私聊补回注入的最近对话轮数。", 3, 30, "轮", True),
            _f("group_lurk_scan_limit", "翻群扫描候选数", "int", 10,
               "翻群吃瓜每次扫描的候选群数量。", 1, 50, "个", True),
            _f("group_lurk_transcript_turns", "翻群上下文轮数", "int", 12,
               "翻群吃瓜读取的群聊上下文轮数。", 5, 40, "轮", True),
            _f("proactive_anti_repeat_count", "防复读注入条数", "int", 5,
               "主动搭话防复读注入的最近主动消息条数。", 1, 15, "条", True),
            _f("mused_memory_ring_size", "记忆去重队列容量", "int", 30,
               "每个角色已聊记忆 ID 循环去重队列容量。", 10, 200, "条", True),
            _f("muse_pick_count", "记忆联想抽样数", "int", 3,
               "记忆联想每次抽样的候选条数。", 1, 10, "条", True),
            _f("muse_exclude_hours", "记忆冷却时长", "int", 72,
               "已被主动提及或刚访问过的记忆在多少小时内不再用于联想。", 12, 720, "小时", True),
            _f("unread_batch_max_chars", "未读摘要字数预算", "int", 800,
               "未读消息批次摘要拼接的最大字数。", 200, 3000, "字", True),
        ),
    )

    atmosphere = ConfigSection(
        id="atmosphere",
        title="群聊读空气",
        icon="wind",
        description="接管群聊自由接话、防抖等待与概率 / 频控门闸。",
        storage_group="atmosphere_config",
        fields=(
            _f("takeover_group_chat", "接管群聊回复", "bool", True,
               "结合当前三态与五维读空气决定是否接话；关闭后仅在 @ / 唤醒 / 引用时回复。"),
            _f("debounce_seconds", "防抖等待", "float", 4.0,
               "群聊消息后等待该时间再判断是否接话；等待期间有新消息则由新消息处理。",
               0.5, 30.0, "秒"),
            _f("reply_gate_enabled", "概率门闸", "bool", True,
               "在防抖与读空气决策前，先按概率筛掉一部分非 @ 普通群消息。"),
            _f("reply_gate_probability", "门闸通过概率", "int", 80,
               "只有该比例的非 @ 普通群消息会继续进入读空气决策；100 = 不筛选。", 0, 100, "%"),
            _f("reply_gate_hourly_limit", "每小时判定上限", "int", 0,
               "同一群聊每小时最多允许多少条非 @ 普通消息进入读空气判定；0 = 不限制。",
               0, 500, "条"),
            _f("atmosphere_transcript_turns", "读空气上下文轮数", "int", 12,
               "五维读空气器读取的群聊最近对话轮数。", 5, 30, "轮", True),
        ),
    )

    memory = ConfigSection(
        id="memory",
        title="记忆与上下文",
        icon="memory",
        description="三级记忆、语义去重、混合召回权重与上下文注入预算。",
        storage_group="memory_and_context_config",
        fields=(
            _f("enable_tiered_memory", "启用分级记忆", "bool", True,
               "关闭后不写入新记忆，也不向 Prompt 注入记忆，仅保留会话滑窗。"),
            _f("sliding_window_size", "会话滑窗条数", "int", 15,
               "插件接管上下文后每次对话注入的最近消息条数。", 3, 60, "条"),
            _f("extract_every_n_messages", "提炼触发条数", "int", 15,
               "滑出窗口每累计多少条有效消息触发一次后台记忆提炼。", 5, 60, "条"),
            _f("max_injected_memories", "单次注入条数", "int", 6,
               "每次对话最多注入的记忆条数。", 1, 20, "条"),
            _f("max_memory_chars_budget", "单次注入字数", "int", 400,
               "每次对话注入记忆的总字数预算，用于控制 Token 费用。", 50, 3000, "字"),
            _f("memory_content_max_chars", "单条记忆字数上限", "int", 60,
               "单条记忆正文的最大字数上限（新建 / 编辑记忆时前后端联动校验）。",
               30, 300, "字"),
            _f("max_tier1_memories", "T1 数量上限", "int", 30,
               "每个角色 Tier1 瞬时记忆数量上限，超出后 FIFO 淘汰最旧。", 5, 200, "条"),
            _f("tier1_ttl_hours", "T1 存活时长", "int", 48,
               "Tier 1 瞬时记忆的存活时长。", 6, 720, "小时"),
            _f("max_recall_candidates", "粗排候选数", "int", 24,
               "混合召回进入重排阶段的候选记忆条数。", 6, 100, "条"),
            _f("min_recall_score", "最低综合得分", "float", 0.38,
               "综合得分低于该值的记忆不会注入。", 0.0, 1.0),
            _f("min_semantic_score", "最低语义相关", "float", 0.25,
               "语义相关性低于该值的记忆不会注入。", 0.0, 1.0),
            _f("recall_semantic_weight", "语义权重", "float", 0.55,
               "综合召回打分中的语义相关性权重（三项自动归一化）。", 0.0, 1.0),
            _f("recall_recency_weight", "新鲜度权重", "float", 0.25,
               "综合召回打分中的时间新鲜度权重。", 0.0, 1.0),
            _f("recall_importance_weight", "重要度权重", "float", 0.20,
               "综合召回打分中的重要度权重。", 0.0, 1.0),
            _f("recent_duplicate_window_minutes", "新记忆防重复窗口", "int", 10,
               "刚提炼的新记忆在多少分钟内抑制重复注入。", 0, 120, "分钟", True),
            _f("recall_query_context_turns", "召回 Query 上下文条数", "int", 2,
               "构建召回 Query 时额外拼接的最近用户发言条数。", 0, 6, "条", True),
            _f("reinforce_min_semantic", "记忆强化语义门槛", "float", 0.55,
               "触发记忆活力强化（access_count + 1）所需的最低语义相关性。",
               0.1, 1.0, advanced=True),
            _f("reinforce_cooldown_hours", "记忆强化冷却", "int", 6,
               "同一条记忆两次有效召回强化之间的冷却时间。", 0, 168, "小时", True),
            _f("promote_t3_min_hits", "T2→T3 最低命中", "int", 8,
               "T2 晋升 T3 永久记忆所需的最低有效命中次数。", 2, 50, "次", True),
            _f("promote_t3_min_importance", "T2→T3 最低重要度", "int", 8,
               "T2 晋升 T3 所需的最低重要度。", 5, 10, "", True),
            _f("promote_t3_min_age_hours", "T2→T3 最短沉淀", "int", 24,
               "T2 晋升 T3 所需的最短沉淀时长。", 0, 720, "小时", True),
            _f("dedupe_cosine", "写入去重余弦阈值", "float", 0.85,
               "写入记忆时的跨层语义去重余弦阈值。", 0.3, 0.99, advanced=True),
            _f("dedupe_jaccard", "写入去重字面阈值", "float", 0.65,
               "无向量时按字符二元组重合度去重的阈值。", 0.3, 0.99, advanced=True),
            _f("supersede_cosine", "状态替代余弦阈值", "float", 0.72,
               "同类 state 记忆自动旧版替代（Supersede）的余弦阈值。", 0.3, 0.99, advanced=True),
            _f("supersede_jaccard", "状态替代字面阈值", "float", 0.50,
               "同类 state 记忆自动旧版替代的字面重合度阈值。", 0.3, 0.99, advanced=True),
            _f("recall_bucket_ratios", "分层召回配额", "ratios",
               dict(DEFAULT_RECALL_BUCKET_RATIOS),
               "粗排候选池中 T3 / T2 / T1 的分层保底配额（百分比，自动归一化）。",
               advanced=True),
            _f("recall_pool_limit", "召回扫描池", "int", 400,
               "混合召回的 SQL 候选扫描池大小。", 50, 2000, "条", True),
            _f("dedupe_pool_limit", "去重扫描池", "int", 400,
               "写入去重的 SQL 候选扫描池大小。", 50, 2000, "条", True),
        ),
    )

    relationship = ConfigSection(
        id="relationship",
        title="用户关系",
        icon="relations",
        description="L1→L2 自动晋升、好感度增益曲线与档案字段上限。",
        storage_group="relationship_config",
        fields=(
            _f("enable_auto_promotion_l2", "L1 自动晋升 L2", "bool", True,
               "允许普通用户自动晋升为 ◆L2 重要熟人（仅到 L2；★L3 由管理员身份或 Pages 显式设置）。"),
            _f("auto_promote_l2_min_interactions", "晋升互动阈值", "int", 40,
               "L1→L2 所需的有效互动回合数（与好感阈值同时满足）。", 1, 500, "回合"),
            _f("auto_promote_l2_min_affinity", "晋升好感阈值", "float", 30.0,
               "L1→L2 所需的好感度（AND 条件）。", 1.0, 100.0),
            _f("affinity_cooldown_seconds", "好感冷却", "int", 180,
               "同一用户连续发多条消息时只结算一次好感的最小间隔；0 = 关闭冷却。",
               0, 3600, "秒"),
            _f("affinity_daily_cap", "每日好感上限", "float", 5.0,
               "每个用户每天最多获得的好感度；0 = 不限制。", 0.0, 50.0),
            _f("affinity_gain_group", "群聊单次增益", "float", 0.4,
               "群聊普通接话单次好感增益。", 0.05, 5.0),
            _f("affinity_gain_direct", "直呼单次增益", "float", 0.6,
               "私聊或群内 @ / 引用呼叫单次好感增益。", 0.05, 5.0),
            _f("affinity_slowdown_threshold", "好感减速门槛", "float", 60.0,
               "好感度达到该值后单次增益减半。", 10.0, 100.0),
            _f("group_note_max_chars", "群档案备注字数上限", "int", 500,
               "群聊关系档案备注的字数上限。", 100, 2000, "字", True),
            _f("user_max_aliases", "曾用名数量上限", "int", 10,
               "单用户保留的历史曾用名 / 群名片数量上限。", 3, 30, "个", True),
        ),
    )

    surfing = ConfigSection(
        id="surfing",
        title="冲浪抓取",
        icon="surf",
        description="视频源抓取、视觉看片评分与主动分享门槛（按角色的源开关在冲浪页设置）。",
        storage_group="surfing_config",
        fields=(
            _f("bili_cookie", "B站 Cookie", "secret", "",
               "未登录 B 站时填写的 Cookie 可提高推荐流可用性；只保存在本地插件数据中。"),
            _f("douyin_api_base", "抖音解析接口", "text", "https://api.douyin.wtf",
               "抖音无水印视频解析服务地址。"),
            _f("send_raw_video", "直发无水印视频", "bool", True,
               "主动分享时优先直接发送无水印视频文件；关闭则只发链接 / 文本。"),
            _f("surf_share_min_score", "分享评分门槛", "int", 80,
               "视觉看片评分低于该值时只记录视频、不主动分享。", 0, 100, "分"),
            _f("surf_share_cooldown_minutes", "分享冷却", "int", 90,
               "两次冲浪视频主动分享的最小间隔。", 5, 1440, "分钟"),
            _f("surf_bili_candidates", "B站抓取条数", "int", 4,
               "单次冲浪从 B站抓取的候选视频条数。", 1, 15, "条", True),
            _f("surf_douyin_candidates", "抖音抓取条数", "int", 3,
               "单次冲浪从抖音抓取的候选视频条数。", 1, 15, "条", True),
            _f("surf_http_timeout_seconds", "视频源超时", "int", 8,
               "视频源 HTTP 请求超时秒数。", 3, 30, "秒", True),
            _f("surf_hot_comments_limit", "热评抓取条数", "int", 3,
               "每条视频附带抓取的热评条数。", 0, 10, "条", True),
            _f("vision_fallback_score", "看片兜底分", "int", 60,
               "视觉模型离线 / 解析失败时的兜底评分（兜底结果永不主动分享）。",
               0, 100, "分", True),
            _f("vision_default_score", "看片缺省分", "int", 78,
               "视觉模型返回 JSON 缺少评分时的缺省分。", 0, 100, "分", True),
            _f("surf_history_max_per_soul", "冲浪历史保留上限", "int", 200,
               "每个角色最多保留的冲浪历史记录条数，每日 GC 自动裁剪超限旧记录。",
               30, 2000, "条", True),
        ),
    )

    return (schedule, proactive, atmosphere, memory, relationship, surfing)


SECTIONS: tuple[ConfigSection, ...] = build_sections()
SECTION_MAP: dict[str, ConfigSection] = {item.id: item for item in SECTIONS}


# ==============================================================================
# 类型归一化 / 钳制
# ==============================================================================
def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("1", "true", "yes", "on", "enable", "enabled", "开启"):
            return True
        if text in ("0", "false", "no", "off", "disable", "disabled", "关闭", ""):
            return False
    return default


def _as_number(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return number


def _clamp_number(value: Any, default: float, minimum: Optional[float], maximum: Optional[float]) -> float:
    number = _as_number(value, default)
    if minimum is not None:
        number = max(float(minimum), number)
    if maximum is not None:
        number = min(float(maximum), number)
    return number


def _as_text(value: Any, default: str, max_chars: int = 4000) -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text[:max_chars]


def _as_str_list(value: Any) -> list[str]:
    if isinstance(value, str):
        raw = [part for part in value.replace("，", ",").replace("、", ",").replace("\n", ",").split(",")]
    elif isinstance(value, (list, tuple, set)):
        raw = list(value)
    else:
        raw = []
    result: list[str] = []
    for item in raw:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text[:40])
    return result[:40]


def normalize_impact_table(value: Any, default: Any = None) -> dict[str, int]:
    """合并并钳制 ``mutation_impact_table``（缺省键回退默认分值）。"""
    base = dict(DEFAULT_MUTATION_IMPACT_TABLE)
    source = value if isinstance(value, dict) else {}
    for key in _IMPACT_KEYS:
        if key not in source:
            continue
        base[key] = int(round(_clamp_number(source.get(key), base[key], 0, 100)))
    return base


def normalize_recall_bucket_ratios(value: Any) -> dict[str, int]:
    """归一化 ``recall_bucket_ratios``（T3/T2/T1 百分比，全 0 时回退默认）。"""
    base = dict(DEFAULT_RECALL_BUCKET_RATIOS)
    source = value if isinstance(value, dict) else {}
    result: dict[str, int] = {}
    for key in ("3", "2", "1"):
        raw = source.get(key, source.get(int(key), base[key]))
        result[key] = int(round(_clamp_number(raw, base[key], 0, 100)))
    if sum(result.values()) <= 0:
        return dict(DEFAULT_RECALL_BUCKET_RATIOS)
    return result


def coerce_field_value(config_field: ConfigField, value: Any) -> Any:
    """按 Schema 归一化并钳制单个字段值（永远返回合法值）。"""
    default = config_field.default
    kind = config_field.type
    if kind == "bool":
        return _as_bool(value, bool(default))
    if kind == "int":
        return int(round(_clamp_number(
            value, _as_number(default, 0), config_field.minimum, config_field.maximum
        )))
    if kind == "float":
        return round(_clamp_number(
            value, _as_number(default, 0.0), config_field.minimum, config_field.maximum
        ), 4)
    if kind == "text":
        return _as_text(value, str(default or ""), 2000)
    if kind == "secret":
        return _as_text(value, str(default or ""), 4000)
    if kind == "list":
        items = _as_str_list(value)
        return items if items else list(default or [])
    if kind == "ratios":
        return normalize_recall_bucket_ratios(value)
    if kind == "object":
        return normalize_impact_table(value)
    return value


def default_values() -> dict[str, dict[str, Any]]:
    """按存储分组返回全套默认值（深拷贝，避免默认对象被共享修改）。"""
    result: dict[str, dict[str, Any]] = {}
    for section in SECTIONS:
        group = result.setdefault(section.storage_group, {})
        for config_field in section.fields:
            group[config_field.key] = _clone_default(config_field.default)
    return result


def _clone_default(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): v for k, v in value.items()}
    if isinstance(value, list):
        return list(value)
    return value


def _legacy_is_customized(key: str, value: Any, config_field: ConfigField) -> bool:
    """旧 AstrBotConfig 里的值是否被用户改过（等价于非默认值）。"""
    if value is None:
        return False
    coerced = coerce_field_value(config_field, value)
    default = coerce_field_value(config_field, config_field.default)
    return coerced != default


# ==============================================================================
# PluginConfigManager
# ==============================================================================
class PluginConfigManager:
    """OnlyYou 运行配置的唯一入口（读写 SQLite + 原地热更新共享字典）。"""

    def __init__(self, db: Any, shared_config: dict[str, Any]):
        self.db = db
        self.shared_config = shared_config if shared_config is not None else {}
        self._values: dict[str, dict[str, Any]] = default_values()
        self._updated_at: int = 0
        self._loaded = False
        self._change_hooks: list[Callable[..., Any]] = []
        #: 旧版 AstrBotConfig（插件构造时注入的原始配置），仅作一次性迁移来源。
        self.legacy_config: dict[str, Any] = {}
        try:
            self.legacy_config = dict(shared_config or {})
        except Exception:  # noqa: BLE001 - AstrBotConfig may be non-mapping
            self.legacy_config = {}

    # ------------------------------------------------------------------
    # Schema / 快照
    # ------------------------------------------------------------------
    @property
    def sections(self) -> tuple[ConfigSection, ...]:
        return SECTIONS

    def section(self, section_id: str) -> Optional[ConfigSection]:
        return SECTION_MAP.get(str(section_id or ""))

    def all_field_map(self) -> dict[str, tuple[ConfigSection, ConfigField]]:
        """key → (section, field)；同名字段以先注册的为准。"""
        result: dict[str, tuple[ConfigSection, ConfigField]] = {}
        for section in SECTIONS:
            for config_field in section.fields:
                result.setdefault(config_field.key, (section, config_field))
        return result

    def defaults(self) -> dict[str, dict[str, Any]]:
        return default_values()

    def values(self) -> dict[str, dict[str, Any]]:
        """存储分组 → {key: value}（深拷贝快照）。"""
        return {
            group: {key: _clone_default(value) for key, value in items.items()}
            for group, items in self._values.items()
        }

    def get(self, key: str, default: Any = None) -> Any:
        """按字段键读取当前生效值（跨分组查找；未知键返回 default）。"""
        for group, items in self._values.items():
            if key in items:
                return items[key]
        return default

    def group_values(self, storage_group: str) -> dict[str, Any]:
        return dict(self._values.get(storage_group) or {})

    # ------------------------------------------------------------------
    # 生命周期：加载 / 旧配置迁移
    # ------------------------------------------------------------------
    async def load(self) -> dict[str, Any]:
        """从 SQLite 恢复配置；首次启动合并旧版 AstrBotConfig。"""
        stored: Any = None
        migrated = True
        try:
            stored = await self.db.get_setting(RUNTIME_CONFIG_KEY, None)
        except Exception as exc:  # noqa: BLE001 - DB failure degrades to defaults
            logger.warning(f"[OnlyYou] load runtime_config failed: {exc}")
            stored = None
        try:
            migrated = bool(await self.db.get_setting(LEGACY_MIGRATED_KEY, False))
        except Exception:  # noqa: BLE001
            migrated = True

        values = default_values()
        stored_groups: Any = None
        if isinstance(stored, dict):
            # 新版结构 {version, updated_at, groups: {storage_group: {...}}}；
            # 兼容早期直接按存储分组嵌套的结构。
            stored_groups = stored.get("groups") if isinstance(stored.get("groups"), dict) else stored
        stored_keys: set[str] = set()
        if isinstance(stored_groups, dict):
            self._merge_values(values, stored_groups, only_known=True)
            for items in stored_groups.values():
                if isinstance(items, dict):
                    stored_keys.update(str(key) for key in items.keys())
        migrated_now = False
        if not migrated:
            migrated_now = self._merge_legacy(values, skip_keys=stored_keys)
            try:
                await self.db.set_setting(LEGACY_MIGRATED_KEY, True)
            except Exception as exc:  # noqa: BLE001 - retry on next start
                logger.warning(f"[OnlyYou] persist legacy migration flag failed: {exc}")
                migrated_now = False
        self._values = values
        self._loaded = True
        await self._persist()
        self.apply_to_shared_config()
        if migrated_now:
            logger.info(
                "[OnlyYou] Migrated legacy AstrBot plugin config into "
                "plugin_settings[runtime_config]."
            )
        return self.snapshot()

    def _merge_values(
        self,
        target: dict[str, dict[str, Any]],
        source: Any,
        only_known: bool = True,
    ) -> list[str]:
        """把 ``{storage_group: {key: value}}`` 合并进 target，返回变更键。"""
        changed: list[str] = []
        if not isinstance(source, dict):
            return changed
        field_map = self.all_field_map()
        for group, items in source.items():
            if not isinstance(items, dict):
                continue
            bucket = target.setdefault(str(group), {})
            for key, value in items.items():
                entry = field_map.get(str(key))
                if entry is None:
                    if not only_known:
                        bucket[str(key)] = value
                    continue
                section, config_field = entry
                if section.storage_group != str(group):
                    continue
                coerced = coerce_field_value(config_field, value)
                if bucket.get(key) != coerced:
                    bucket[key] = coerced
                    changed.append(str(key))
        return changed

    def _merge_legacy(
        self, values: dict[str, dict[str, Any]], skip_keys: Optional[set[str]] = None
    ) -> bool:
        """合并旧版 AstrBotConfig 中用户改过的非默认值。

        ``skip_keys`` 是已经由 SQLite ``runtime_config`` 显式配置过的字段，旧配置
        不得覆盖它们（升级时以 Pages 保存过的新值为准）。
        """
        legacy = self.legacy_config or {}
        touched = False
        for section in SECTIONS:
            group = section.storage_group
            bucket = values.setdefault(group, {})
            legacy_group = legacy.get(group)
            if not isinstance(legacy_group, dict):
                continue
            # 第二分组（proactive）与 schedule 共用存储分组，必须逐个字段判定。
            for config_field in section.fields:
                if config_field.key not in legacy_group:
                    continue
                if skip_keys and config_field.key in skip_keys:
                    continue
                raw = legacy_group.get(config_field.key)
                if not _legacy_is_customized(config_field.key, raw, config_field):
                    continue
                coerced = coerce_field_value(config_field, raw)
                if bucket.get(config_field.key) != coerced:
                    bucket[config_field.key] = coerced
                    touched = True
        return touched

    # ------------------------------------------------------------------
    # 写入：保存 / 重置
    # ------------------------------------------------------------------
    def normalize_payload(self, payload: Any) -> dict[str, dict[str, Any]]:
        """把前端提交的多种形态归一为 ``{storage_group: {key: value}}``。

        支持：
        * ``{"section": "memory", "values": {...}}``（settings 页单分组保存）
        * ``{"memory_and_context_config": {...}}``（按存储分组批量保存）
        * ``{"memory": {...}}``（按 section id 批量保存）
        * ``{"key": value}``（平铺键值，自动按字段归属分组）
        """
        result: dict[str, dict[str, Any]] = {}
        if not isinstance(payload, dict):
            return result
        field_map = self.all_field_map()

        def put(section: ConfigSection, key: str, value: Any) -> None:
            result.setdefault(section.storage_group, {})[key] = value

        body = payload.get("config")
        if isinstance(body, dict) and "section" not in payload and "values" not in payload:
            payload = body

        section_id = str(payload.get("section") or "").strip()
        if section_id and isinstance(payload.get("values"), dict):
            section = SECTION_MAP.get(section_id)
            if section is None:
                return result
            valid = section.field_map()
            for key, value in payload["values"].items():
                if str(key) in valid:
                    put(section, str(key), value)
            return result

        for group_key, items in payload.items():
            if not isinstance(items, dict):
                continue
            section = SECTION_MAP.get(str(group_key))
            if section is not None:
                valid = section.field_map()
                for key, value in items.items():
                    if str(key) in valid:
                        put(section, str(key), value)
                continue
            # 存储分组形态
            found = False
            for candidate in SECTIONS:
                if candidate.storage_group != str(group_key):
                    continue
                valid = candidate.field_map()
                for key, value in items.items():
                    if str(key) in valid:
                        put(candidate, str(key), value)
                        found = True
            if found:
                continue
            # 平铺键值形态：{"heartbeat_interval_minutes": 20}
            for key, value in items.items():
                entry = field_map.get(str(key))
                if entry is not None:
                    put(entry[0], str(key), value)
        # 顶层平铺键值：{"heartbeat_interval_minutes": 20, ...}
        for key, value in payload.items():
            if isinstance(value, (dict, list)):
                continue
            entry = field_map.get(str(key))
            if entry is not None:
                put(entry[0], str(key), value)
        return result

    async def save(self, payload: Any) -> dict[str, Any]:
        """校验 / 钳制后落盘 SQLite，并原地热更新共享配置字典。"""
        normalized = self.normalize_payload(payload)
        changed: list[str] = []
        for group, items in normalized.items():
            bucket = self._values.setdefault(group, {})
            field_map = self.all_field_map()
            for key, raw in items.items():
                entry = field_map.get(key)
                if entry is None or entry[0].storage_group != group:
                    continue
                coerced = coerce_field_value(entry[1], raw)
                if bucket.get(key) != coerced:
                    bucket[key] = coerced
                    changed.append(key)
        if changed:
            await self._persist()
            self.apply_to_shared_config()
            await self._notify_changed(changed)
        return {
            "changed": changed,
            "values": self.values(),
            "sections": self.schema(),
            "updated_at": self._updated_at,
        }

    async def reset(self, payload: Any = None) -> dict[str, Any]:
        """恢复默认值：``{"section": "memory"}`` / ``{"group": "..."}`` / 整体。"""
        body = payload if isinstance(payload, dict) else {}
        section_id = str(body.get("section") or body.get("section_id") or "").strip()
        group_id = str(body.get("group") or body.get("storage_group") or "").strip()
        defaults = default_values()
        changed: list[str] = []
        for section in SECTIONS:
            if section_id and section.id != section_id:
                continue
            if group_id and section.storage_group != group_id:
                continue
            target = self._values.setdefault(section.storage_group, {})
            for config_field in section.fields:
                default = _clone_default(defaults[section.storage_group][config_field.key])
                if target.get(config_field.key) != default:
                    target[config_field.key] = default
                    changed.append(config_field.key)
        if changed:
            await self._persist()
            self.apply_to_shared_config()
            await self._notify_changed(changed)
        return {
            "changed": changed,
            "values": self.values(),
            "sections": self.schema(),
            "updated_at": self._updated_at,
        }

    async def _persist(self) -> None:
        """把当前值写入 ``plugin_settings[runtime_config]``（失败仅告警）。"""
        self._updated_at = int(time.time())
        payload = {
            "version": CONFIG_VERSION,
            "updated_at": self._updated_at,
            "groups": self.values(),
        }
        try:
            await self.db.set_setting(RUNTIME_CONFIG_KEY, payload)
        except Exception as exc:  # noqa: BLE001 - hot values still work in memory
            logger.warning(f"[OnlyYou] persist runtime_config failed: {exc}")

    # ------------------------------------------------------------------
    # 热更新
    # ------------------------------------------------------------------
    def apply_to_shared_config(self) -> None:
        """把当前值**原地**写进插件共享配置字典（所有子模块共享同一引用）。"""
        shared = self.shared_config
        if shared is None:
            return
        for group, items in self._values.items():
            bucket = shared.get(group)
            if not isinstance(bucket, dict):
                bucket = {}
                shared[group] = bucket
            for key, value in items.items():
                bucket[key] = _clone_default(value)

    def bind_change_hook(self, hook: Callable[..., Any]) -> None:
        if callable(hook):
            self._change_hooks.append(hook)

    async def _notify_changed(self, changed: list[str]) -> None:
        for hook in list(self._change_hooks):
            try:
                result = hook(changed)
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:  # noqa: BLE001 - a bad hook must not break saving
                logger.warning(f"[OnlyYou] config change hook failed: {exc}")

    # ------------------------------------------------------------------
    # 对外 Schema / 摘要
    # ------------------------------------------------------------------
    def schema(self) -> list[dict[str, Any]]:
        """前端设置页使用的完整 Schema（字段内联当前值 / 默认值）。"""
        sections: list[dict[str, Any]] = []
        for section in SECTIONS:
            group_values = self._values.get(section.storage_group, {})
            fields: list[dict[str, Any]] = []
            for config_field in section.fields:
                current = group_values.get(config_field.key, config_field.default)
                item = {
                    "key": config_field.key,
                    "label": config_field.label,
                    "type": config_field.type,
                    "hint": config_field.hint,
                    "advanced": bool(config_field.advanced),
                    "value": _clone_default(current),
                    "default": _clone_default(config_field.default),
                    "unit": config_field.unit,
                }
                if config_field.minimum is not None:
                    item["min"] = config_field.minimum
                if config_field.maximum is not None:
                    item["max"] = config_field.maximum
                if config_field.type == "secret":
                    item["secret"] = True
                if config_field.fields:
                    item["fields"] = [dict(sub) for sub in config_field.fields]
                fields.append(item)
            sections.append(
                {
                    "id": section.id,
                    "title": section.title,
                    "icon": section.icon,
                    "description": section.description,
                    "group": section.storage_group,
                    "fields": fields,
                }
            )
        return sections

    def snapshot(self) -> dict[str, Any]:
        return {
            "version": CONFIG_VERSION,
            "updated_at": self._updated_at,
            "sections": self.schema(),
        }

    def summary(self) -> dict[str, Any]:
        """``init_summary`` 用的轻量摘要（不带全部字段值）。"""
        return {
            "version": CONFIG_VERSION,
            "updated_at": self._updated_at,
            "sections": [
                {
                    "id": section.id,
                    "title": section.title,
                    "icon": section.icon,
                    "field_count": len(section.fields),
                }
                for section in SECTIONS
            ],
        }

    # ------------------------------------------------------------------
    # 角色级覆盖（urgent_keywords / mutation_impact_table）
    # ------------------------------------------------------------------
    @staticmethod
    def _soul_settings(soul: Any) -> dict[str, Any]:
        if soul is None:
            return {}
        settings = getattr(soul, "settings", None)
        if isinstance(settings, dict):
            return settings
        if isinstance(soul, dict):
            nested = soul.get("settings")
            return nested if isinstance(nested, dict) else soul
        return {}

    def urgent_keywords(self, soul: Any = None) -> list[str]:
        """角色 ``settings.urgent_keywords`` 优先，否则用全局值。"""
        override = self._soul_settings(soul).get("urgent_keywords")
        if isinstance(override, (list, tuple, str)):
            items = _as_str_list(override)
            if items:
                return items
        values = _as_str_list(self.get("urgent_keywords", DEFAULT_URGENT_KEYWORDS))
        return values or list(DEFAULT_URGENT_KEYWORDS)

    def mutation_impact_table(self, soul: Any = None) -> dict[str, int]:
        """角色 ``settings.mutation_impact`` 优先，否则用全局值。"""
        settings = self._soul_settings(soul)
        override = settings.get("mutation_impact") or settings.get("mutation_impact_table")
        base = normalize_impact_table(self.get("mutation_impact_table"))
        if isinstance(override, dict):
            merged = dict(base)
            for key in _IMPACT_KEYS:
                if key in override:
                    merged[key] = int(round(_clamp_number(override.get(key), merged[key], 0, 100)))
            return merged
        return base


__all__ = [
    "CONFIG_VERSION",
    "ConfigField",
    "ConfigSection",
    "DEFAULT_MUTATION_IMPACT_TABLE",
    "DEFAULT_RECALL_BUCKET_RATIOS",
    "DEFAULT_URGENT_KEYWORDS",
    "LEGACY_MIGRATED_KEY",
    "PluginConfigManager",
    "RUNTIME_CONFIG_KEY",
    "SECTIONS",
    "SECTION_MAP",
    "build_sections",
    "coerce_field_value",
    "default_values",
    "normalize_impact_table",
    "normalize_recall_bucket_ratios",
]
