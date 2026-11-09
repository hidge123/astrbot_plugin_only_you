# OnlyYou (只有你) —— 技术架构与 AstrBot 实现方案设计 (v2.5)

> 本文档紧密结合 [AstrBot 插件开发指南 (`Star` 规范)](https://docs.astrbot.app/dev/star/plugin-new.html)，详细定义 `astrbot_plugin_only_you` 的核心工程设计，重点包含：**三态门禁（休息中 / 繁忙中 / 空闲中）× 四类可执行行动卡（`SLEEP` / `SURF` / `IDLE_ROAM` / `BUSY_TASK`）**、**基于作息曲线的 24h 动态弹性时间轴**、**基于外部输入的时间块切分 / 延展微调（含繁忙半途结算）**、参考并精简自 `livingmemory` 的 **分级记忆、定时自动遗忘清理 (`Forgetting GC`) 与简易上下文预算管理器 (`Context Budget Manager`)**，以及 v2.4 引入、v2.5 延续的 **详细角色卡（10 分区 × 39 字段）接管原生 Persona** 与 **插件自治四角色模型体系（`ProviderFactory` + `ModelHub` + `selected_provider`）**。

---

## 一、 核心状态与四类行动卡控制架构图

```mermaid
flowchart TB
    subgraph Roguelite["🎲 1. 24h 动态弹性日程构筑引擎 (core/roguelite_deck.py)"]
        Pool["角色专属四类行动卡池 (8~12张可编辑) + 作息曲线 (rhythm.chronotype)\n① SLEEP(睡觉) | ② SURF(冲浪) | ③ IDLE_ROAM(闲逛) | ④ BUSY_TASK(繁忙主业)"]
        Timeline["今日 24h 动态积木时间轴 (DailyScheduleRun)\n游标 0.0 -> 24.0 随机时长拼接 (7~11 个弹性时间块)\n每块绑定: BotState(RESTING/BUSY/IDLE) + ActionType"]
        TalkPool["今日短时记忆谈资 (Tier 1 Memory)\n繁忙卡每次结算生成的随机【完成结果记忆】+ 冲浪视频记忆"]
        Pool -->|"按 [作息权重曲线 × 今日基调 × 相邻连携] 动态排满 24h"| Timeline
    end

    subgraph StateGate["🚦 2. 三态 × 四类行动卡控制器 (State & Action Controller)"]
        CurBlock{"当前时间块\n(State + ActionType)"}
        Timeline --> CurBlock
    end

    subgraph ExecPipelines["⚡ 3. 四类行动卡的实际后台行为与消息门禁"]
        direction LR
        subgraph ActSleep["💤 ① 睡觉卡 (SLEEP / RESTING)"]
            S_Behavior["• 消息门禁：直接拦截丢弃，不回任何消息\n• 后台行为：什么都不做 (完全静默)"]
        end

        subgraph ActBusy["🔥 ④ 角色繁忙卡 (BUSY_TASK / BUSY)"]
            B_Behavior["• 消息门禁：过滤 L1 普通群友，仅放行 ◆L2 & ★L3\n• 极简三要素（结果每天随机不同）：\n  1. 注入当前行为记忆\n  2. 实时推进进度条 (0% -> 100%)\n  3. 结束/中断时生成一句话随机【完成结果】写入 Tier1 记忆！"]
        end

        subgraph ActSurf["📱 ② 冲浪卡 (SURF / IDLE)"]
            I_Surf["• 消息门禁：L1/L2/L3 全量放行进入读空气\n• 后台行为：激活 B站/抖音冲浪 -> 看热评 -> 视觉看片 -> 分享"]
        end

        subgraph ActRoam["👀 ③ 空闲游荡卡 (IDLE_ROAM / IDLE)"]
            I_Roam["• 消息门禁：L1/L2/L3 全量放行进入读空气\n• 后台行为（二选一）：\n  A. 翻群记录吃瓜 (扫描未读群聊滑窗插话/私聊L3吐槽)\n  B. 发呆记忆联想 (抽1~2条旧记忆/今日繁忙结果主动找人搭话)"]
        end
    end

    CurBlock -->|"RESTING / SLEEP"| ActSleep
    CurBlock -->|"BUSY / BUSY_TASK"| ActBusy
    CurBlock -->|"IDLE / SURF"| ActSurf
    CurBlock -->|"IDLE / IDLE_ROAM"| ActRoam
    ActBusy & ActSurf -->|"写入 Tier1 短时记忆"| TalkPool
    TalkPool -.->|"提供联想谈资"| ActRoam

    subgraph Mutator["🔄 4. 输入驱动的受限日程微调器 (core/schedule_mutator.py)"]
        Gate["三重硬性限制门禁：\n1. 今日微调次数 < max_mutations(2次)\n2. 距上次微调 > cooldown(120min)\n3. 输入冲击力 Impact > 当前卡片定力 Armor"]
    end

    ActBusy & ActSurf & ActRoam -->|"输入源A: 读取到高冲击力用户消息"| Gate
    ActSurf -->|"输入源B: 刷到高分上头视频"| Gate
    Gate -->|"击穿护甲！实时切分或延展动态时间块\n(如 BUSY_TASK 半途结算并转为 IDLE_ROAM，或推迟 SLEEP)"| Timeline
```

---

## 二、 插件标准目录结构

遵循 AstrBot 插件命名规范，仓库与目录命名为 `astrbot_plugin_only_you`：

```text
astrbot_plugin_only_you/
├── metadata.yaml                # 插件元数据声明 (name: astrbot_plugin_only_you)
├── _conf_schema.json            # AstrBot WebUI 配置 Schema（仅运行参数；模型配置迁至 plugin_settings，见 §5.1）
├── requirements.txt             # 依赖库 (httpx, aiosqlite, pillow, imageio-ffmpeg)
├── main.py                      # 插件入口类 OnlyYouPlugin(Star)：三态门禁 + selected_provider + on_decorating_result TTS + 生命周期
├── core/
│   ├── state_machine.py         # 🚦 三态枚举 (RESTING/BUSY/IDLE) + 四类行动卡枚举 ActionType 门禁控制器
│   ├── roguelite_deck.py        # 🎲 24h 动态积木时间轴引擎（角色专属四类卡池 / 作息曲线 / 弹性块拼接 / 繁忙完成结果字段）
│   ├── schedule_mutator.py      # 🔄 输入驱动的受限微调器（BUSY_TASK 半点切分半途结算 / SLEEP 唤醒窗 / IDLE 加钟 + 三重门禁）
│   ├── character_card.py        # 🎭 详细角色卡单一数据源：CARD_SCHEMA(10 分区/39 字段)、normalize/migrate、Persona Prompt 渲染
│   ├── soul_manager.py          # 角色卡(Soul)管理：SoulProfile.card、会话路由、AI 造卡(generate_card)、原生 Persona 导入
│   ├── user_relation.py         # 三级用户关系管理 (L1普通 / L2重要熟人 / ★L3核心用户) 与跨平台映射
│   ├── memory_manager.py        # 🧠 精简版分级记忆引擎 (Tier1瞬时/Tier2近期/Tier3核心 + 定时遗忘GC)
│   ├── context_budget.py        # 🛡️ 上下文/记忆预算管理器 + _takeover_persona 三分支接管原生 Persona 段
│   ├── provider_factory.py      # 🔧 唯一触碰 AstrBot Provider 内部结构的封装（复用/自建实例、inst_map、TTS、dispose）
│   ├── model_hub.py             # 🔌 插件自治四角色模型网关 (main/decision/vision/tts，配置存 plugin_settings.model_config)
│   ├── platform_bridge.py       # 🤝 AstrBot 平台配置协同桥 (原生分段回复 segmented_reply、流式、语音主动消息)
│   ├── web_api.py               # 🖥️ 27 条 Plugin Pages Web API 路由 (17 条兼容旧版 + 10 条新增)
│   └── atmosphere_reader.py     # 群聊主战场：防抖等待窗口、多人对话“解线团”、五维读空气器
├── views/                       # 🎨 AstrBot 插件自定义页面 (Plugin Pages / Views，兼容 pages/)
│   └── console/                 # 灰白+青绿极简控制台（48px 顶栏 + 216px 侧栏，8 个页面）
│       ├── index.html           # 外壳：顶栏、侧栏导航、main#view、toast/modal/drawer 容器
│       ├── style.css            # 设计令牌 + 组件样式（浅/深色）
│       ├── app.js               # bridge 封装、轻量状态、hash 路由、通用 UI（无构建 classic script）
│       ├── icons.js             # ≥20 个内联 SVG 线性图标（替代 emoji）
│       ├── charts.js            # 纯 SVG 环形表 / 24h 弹性动态时间轴（浮点时间 / 行动徽章 / 进度 tooltip）/ 环形图 / 条形图
│       ├── page-overview.js     # 概览：状态环形表 + 24h 弹性轴 + 行动徽章/繁忙进度条/今日繁忙成果(Tier1) + 基调/配额/关系/记忆/模型小卡
│       ├── page-schedule.js     # 日程：24h 弹性时间轴 + 四类行动卡网格/筛选/编辑器（已无奇遇卡） + 重抽/强制切换
│       ├── page-character.js    # 角色卡：10 分区动态表单 + AI 整卡生成/分区补全 + Prompt 预览
│       ├── page-relations.js    # 关系：★L3 / ◆L2 / L1 三列看板 + 拖拽改级 + 合并账号
│       ├── page-memory.js       # 记忆：Tier 分段 + 搜索 + 提炼/GC/钉选核心
│       ├── page-surfing.js      # 冲浪：视频卡片墙 + 分享/删除 + 手动触发冲浪
│       ├── page-models.js       # 模型：四角色卡片 + 源/模型拉取 + 继承开关 + 测试 + TTS 试听
│       ├── page-sessions.js     # 会话：插件默认角色 + UMO 绑定表
│       └── mock.js              # ?mock=1 / file:// 打开时的本地 Mock bridge（离线预览与截图）
├── surfing/
│   ├── pipeline.py              # 📱 赛博冲浪主控流水线（仅在 IDLE 空闲状态下运行）
│   ├── video_vision.py          # 多模态视频预处理器（统一 role="vision" 评分，短视频流直传 / FFmpeg 3x3 抽帧）
│   └── adapters/
│       ├── base.py              # 抽象平台接口 BaseVideoAdapter & VideoItem 模型
│       ├── bilibili.py          # B站适配器（推荐流/热门/UP主动态、热评抓取、CC字幕/流解析）
│       └── douyin.py            # 抖音适配器（热点榜/推荐流、无水印直链解析、热评抓取）
├── storage/
│   └── db.py                    # 基于 aiosqlite 的持久化层（新增 plugin_settings KV 表 + get_setting/set_setting）
├── prompts/
│   └── templates.py             # 集中管理角色卡生成/分区补全、卡池、受限微调、读空气、看片、记忆提炼等 Prompt
├── tests/                       # 离线 unittest 测试套件（provider_factory/model_hub/character_card/persona_takeover/web_api/regressions）
└── docs/                        # 产品与架构文档 (PRD.md / ARCHITECTURE.md / PROMPTS_AND_FLOWS.md / WEBUI_DESIGN.md)
```

---

## 三、 主动决策力核心工程设计

### 3.1 三态枚举、四类行动卡与 24h 动态时间块定义 (`core/roguelite_deck.py`)

为兼顾“清晰的三态门禁”、“真实可执行的后台动作”与“千人千面的虚构角色贴合度”，每张行动卡同时具备 **`state`（三态门禁）**、**`action_type`（四类可执行动作）** 与 **`duration_range`（动态时长范围）**：

```python
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional

class BotState(str, Enum):
    RESTING = "RESTING"   # 💤 休息中：不回任何消息，不刷视频
    BUSY = "BUSY"         # 🔥 繁忙中：只回 L2重要用户 & L3核心用户消息
    IDLE = "IDLE"         # ☕ 空闲中：L1/L2/L3 全量读空气回复，可执行冲浪/翻群吃瓜/记忆联想

class ActionType(str, Enum):
    SLEEP = "SLEEP"           # ① 睡觉卡 (绑定 RESTING)：什么都不做，完全静默
    SURF = "SURF"             # ② 冲浪卡 (绑定 IDLE)：触发 B站/抖音真实刷视频管线
    IDLE_ROAM = "IDLE_ROAM"   # ③ 空闲游荡卡 (绑定 IDLE)：翻群吃瓜 / 发呆记忆联想
    BUSY_TASK = "BUSY_TASK"   # ④ 角色繁忙卡 (绑定 BUSY)：行为记忆 + 进度条 + 完成结果写 Tier1

class UserTier(int, Enum):
    L1_NORMAL = 1         # 普通用户（群友）
    L2_IMPORTANT = 2      # 重要用户（混熟的老水友/熟人）
    L3_CORE = 3           # ★ 核心用户（管理员 / OnlyYou）

@dataclass
class StateEventCard:
    """24h 动态行动卡（兼容旧名 StateEventCard）"""
    card_id: str
    title: str                        # 贴合人设的行动名称，如 "丹房炼制回春丹" / "躺沙发刷B站"
    state: BotState = BotState.IDLE   # 绑定的唯一状态（由 action_type 强制同步）
    valid_slots: list[str] = field(default_factory=list)  # 仅旧数据兼容字段，排轴已不再使用
    base_weight: int = 50             # 基础抽取权重
    armor: int = 50                   # 定力护甲 (0~100)，决定该时间块多难被外部输入改变
    synergy_tags: dict[str, float] = field(default_factory=dict)  # 相邻卡片连携系数
    action_type: Optional[ActionType] = None  # SLEEP | SURF | IDLE_ROAM | BUSY_TASK（显式优先）
    min_hours: float = 1.0            # 随机最短时长（小时，0.5h 网格，0.5 ~ 8.0）
    max_hours: float = 2.5            # 随机最长时长（小时，0.5h 网格）

# from_dict() 旧数据迁移：无 action_type 时按 state + title 推导
#   RESTING -> SLEEP / BUSY -> BUSY_TASK / IDLE 且标题含"刷 B站 抖音 视频 冲浪" -> SURF / 其余 IDLE -> IDLE_ROAM
# 显式 action_type 优先并强制 state = action_type.bound_state；时长夹取到 0.5h 网格且 min <= max。

@dataclass
class SlotInstance:
    """24h 动态时间轴上的单个弹性时间块实例（文档旧称 ScheduleBlock；类名沿用 SlotInstance）"""
    slot_key: str                     # 连续编号 "block_0", "block_1" ...（force_state 用它定位）
    start_hour: float                 # 起始小时 (0.0 ~ 23.5，0.5h 网格)
    end_hour: float                   # 结束小时 (0.5 ~ 24.0)
    state: BotState                   # RESTING | BUSY | IDLE
    card_id: str = "evt_default"
    title: str = "日常摸鱼"           # 当前正在做的事（繁忙卡即作为【当前行为记忆】）
    label: str = ""                   # 行动类型中文标签，如 "繁忙主业"
    action_type: Optional[ActionType] = None  # SLEEP | SURF | IDLE_ROAM | BUSY_TASK
    armor: int = 50                   # 当前护甲
    mutated: bool = False             # 是否因外部输入发生过微调
    mutation_monologue: str = ""      # 微调心理独白
    delay_minutes: int = 0            # 旧字段保留，新时间轴恒为 0
    # --- 仅 BUSY_TASK 使用的极简三要素字段 ---
    busy_settled: bool = False        # 该繁忙块是否已结算完成结果记忆
    completion_result: str = ""       # 100% 完成（或中途被打断）时生成的一句话结果（写入 Tier1 记忆）

    def get_progress_pct(self, now_dt: Optional[datetime] = None) -> int:
        """实时计算当前时间块进度百分比 (0% -> 100%)；now_dt 缺省取 datetime.now()"""
        dt = now_dt or datetime.now()
        now_hour = dt.hour + dt.minute / 60.0 + dt.second / 3600.0
        if now_hour <= self.start_hour:
            return 0
        if now_hour >= self.end_hour:
            return 100
        return int(round(100.0 * (now_hour - self.start_hour) / (self.end_hour - self.start_hour)))

    def format_time_range(self) -> str:
        """浮点小时转 "13:30-15:00"（24.0 -> "24:00"）"""
        return "%02d:%02d-%02d:%02d" % (
            int(self.start_hour), int(round((self.start_hour % 1) * 60)),
            int(self.end_hour), int(round((self.end_hour % 1) * 60)),
        )
```

#### 24h 作息曲线 (`chronotype`) 与动态时长拼接算法
引擎根据角色卡 `rhythm.chronotype`（`early` 早起型 / `normal` 规律型 / `night_owl` 夜猫子）提供小时级状态权重曲线 `get_chronotype_weights(chronotype, hour_t)`，并在每日零点（或重抽时）从 `t = 0.0` 循环拼接到 `24.0`：

```python
def _build_dynamic_slots(
    self,
    chronotype: str,
    vigor: DayVigorCard,
    pool: list[StateEventCard],
    rng: Any = None,
) -> list[SlotInstance]:
    """把角色专属卡池无缝拼成 0.0 -> 24.0 的弹性时间轴（0.5h 网格，最终 7~11 块）"""
    t = 0.0
    blocks: list[SlotInstance] = []
    prev_block: Optional[SlotInstance] = None
    consecutive_busy_hours = 0.0
    hours_since_long_sleep = 99.0

    while t < 24.0 - 1e-6:
        remaining = round(24.0 - t, 1)
        # 1. 查询角色作息类型在时刻 t 的四类行动权重倍率
        state_weights = self.get_chronotype_weights(chronotype, t)
        # 2. 叠加今日基调卡倍率 + 防呆连携（刚睡醒不连睡 / 连续忙 >3.5h 后提高 SURF/IDLE_ROAM 权重）
        card = self._weighted_draw_card(
            pool, remaining, state_weights, vigor, prev_block,
            consecutive_busy_hours, hours_since_long_sleep, rng=rng,
        )
        # 3. 在卡片 [min_hours, max_hours] 内按 0.5h 步长随机掷出时长，并截断到 24.0
        duration = self._roll_duration(card.min_hours, card.max_hours, remaining, rng=rng)
        block = SlotInstance(
            slot_key=f"block_{len(blocks)}",
            start_hour=round(t, 1),
            end_hour=round(t + duration, 1),
            state=card.state,
            card_id=card.card_id,
            title=card.title,
            label=card.action_type.label,
            action_type=card.action_type,
            armor=max(0, min(100, card.armor + vigor.armor_offset)),
        )
        blocks.append(block)
        if block.action_type == ActionType.SLEEP and duration >= 3.0:
            hours_since_long_sleep = 0.0
        else:
            hours_since_long_sleep += duration
        consecutive_busy_hours = (
            consecutive_busy_hours + duration if block.state == BotState.BUSY else 0.0
        )
        prev_block = block
        t = round(block.end_hour, 1)

    # 4. 收尾重排：合并/拆分到 7~11 块、重排连续 slot_key、钉死末块 end_hour = 24.0；
    #    旧日程（6 时段 / 过夜 end<start）由 is_valid_timeline() 校验失败后自动重抽。
    target = int(rng.choice([8, 9, 10, 11]))
    return self._rebalance_blocks(blocks, target=target)
```

---

### 3.2 三态门禁与四类行动卡执行控制器 (`core/state_machine.py` & `main.py`)

#### 1. 对被动消息【回消息】的三态拦截与放行逻辑 (`main.py`)
当 AstrBot 收到任何群聊或私聊消息 `event` 时，第一步直接经过三态门禁（零 Token 消耗完成前置过滤）：

```python
@filter.event_message_type(filter.EventMessageType.ALL, priority=999)
async def on_any_message(self, event: AstrMessageEvent):
    if event.message_str.startswith("/"):
        return  # 放行系统指令

    soul = await self.soul_manager.resolve_soul(event.unified_msg_origin)
    user = await self.user_relation.resolve_user(event)
    current_block = await self.deck_engine.get_current_slot(soul.soul_id)

    # 1. 将消息静默记入群聊/私聊滑动窗口（供后续苏醒或 IDLE_ROAM 翻群吃瓜时读取）
    self.atmosphere_reader.record_message(event, user)

    # 2. 检查是否因高冲击力消息触发【受限日程微调】（如把 BUSY_TASK 半途打断切分为 IDLE_ROAM）
    mutated = await self.schedule_mutator.on_message_input(soul, current_block, event, user)
    if mutated:
        current_block = await self.deck_engine.get_current_slot(soul.soul_id)

    # 3. 🚦 三态核心门禁裁决：通过 PlatformBridge.apply_reply_decision 协同原生管线
    #    被门禁拦截的消息 = Bot 当时不可用而错过的消息 -> 记入【未读队列】
    if current_block.state == BotState.RESTING:
        # 💤 睡觉卡 (SLEEP)：撤回唤醒，不回任何消息；所有未读都进未读队列
        self.session_buffer.mark_unread(umo, buffered_message, is_private=is_private)
        self.platform_bridge.apply_reply_decision(event, False)
        return

    elif current_block.state == BotState.BUSY:
        # 🔥 角色繁忙卡 (BUSY_TASK)：忽略 L1 普通用户；仅放行 ◆L2 与 ★L3，并在 Prompt 注入当前行为 + 进度条 (0~100%)
        if user.tier == UserTier.L1_NORMAL:
            self.session_buffer.mark_unread(umo, buffered_message, is_private=is_private)
            self.platform_bridge.apply_reply_decision(event, False)
            return

    # 4. 群聊走读空气裁决 -> 若决定开口，放行交由 AstrBot 原生管线生成与分段发送
    #    注意：门禁放行后即使读空气判 IGNORE，这条消息也属于「已读」，不进未读队列
    should_reply = await self.atmosphere_reader.evaluate_should_reply(event, soul, user, current_block)
    self.platform_bridge.apply_reply_decision(event, should_reply)
```

> **未读消息定义**：只统计**被三态门禁拦截**的消息（`RESTING` 全部 / `BUSY` 的 `L1`）；被放行后读空气判 `IGNORE` 的消息是“已读但不想回”，不算未读。未读队列由 `SessionBuffer._unread` 独立维护（不依赖 15 条滑窗驱逐、按群聊/私聊分类、TTL/容量/冷却可配）。

#### 2. 四类行动卡 (`SLEEP` / `BUSY_TASK` / `SURF` / `IDLE_ROAM`) 的后台心跳调度
后台异步心跳循环按当前时间块 `current_block.action_type` 驱动真实行为：
1. **当刚跨过一个 `BUSY_TASK` 时间块（进度到达 `100%` 且 `busy_settled == False`）**：
   - 自动结算该繁忙块：调用决策模型（或轻量模板回退）生成**一句话完成结果**（如 *“忙活两小时终于把回春丹炼出来了，成色还不错”*），赋值给 `block.completion_result`，并直接调用 `memory_manager.add_memory(memory_tier=1, ...)` 写入 **Tier 1 短时记忆库**，标记 `busy_settled = True`。
2. **当当前块为 `① SLEEP` (`BotState.RESTING`)**：
   - **什么都不做**！直接返回，完全静默。
3. **当当前块为 `④ BUSY_TASK` (`BotState.BUSY`)**：
   - 不刷视频、不主动找人闲聊；由 `current_block.get_progress_pct(now_dt)` 随时间自动推進 `0% -> 100%` 进度条，供被动回复 ◆L2/★L3 时展示 `[正在忙：{title} | 进度：{progress}%]`。
4. **任意 `IDLE` 块（`SURF` / `IDLE_ROAM`）在驱动自身行为前，先执行【私聊未读补回】**：
   - `_catch_up_private_unread()` 取私聊未读队列，按会话逐条喂给 `UNREAD_PRIVATE_CATCHUP_PROMPT`，生成一句“刚睡醒/刚忙完才看到”的自然补回消息并经 `PlatformBridge.send_proactive_message()` 发送；每个私聊会话受 `unread_private_cooldown_minutes` 冷却限制，单次心跳最多补回 2 个会话，发送成功即 ack 该批未读（失败保留重试）。
5. **当当前块为 `② SURF` (`BotState.IDLE`)**：
   - **触发真实刷视频 (`CyberSurfingPipeline`)**：结合角色卡 `surfing_prefs` 拉取 B站/抖音推荐流与热评，由 `vision` 模型看片打分，写入看片记忆，并可主动分享给 ★L3 核心用户或活跃群聊。
6. **当当前块为 `③ IDLE_ROAM` (`BotState.IDLE`)**：
   - **分支 A —— 翻群记录吃瓜 (`GROUP_LURK`，先决策后回复)**：从【群聊未读队列】按「未读条数 × 新鲜度」排序选一个群 → 先用 `ATMOSPHERE_EVAL_PROMPT` 对“未读批次 + 最近窗口”跑一次决策模型（`REPLY` / `IGNORE`）；只有 `REPLY` 才用 `IDLE_ROAM_PROACTIVE_PROMPT` 生成冒泡/私聊 ★L3 吐槽并发送；`IGNORE` 则静默消费该批未读（不重复评估）。消费即从队列移除，并受 `unread_group_cooldown_minutes` 每群冷却限制；
   - **分支 B —— 发呆记忆联想 (`MEMORY_MUSE`)**：当无群聊未读、或群聊决策为 `IGNORE` 时，从 `MemoryManager` 抽取 1~2 条记忆（优先含今日刚结算完的 `BUSY_TASK` 完成结果短时记忆或历史记忆），生成一段第一人称“发呆突然想起某事”的开场白。

---

### 3.3 🔄 输入驱动的受限日程状态微调器 (`core/schedule_mutator.py`)

机器人如何根据**读取到的用户消息**或**刷到的视频**动态微调自己的 24h 弹性时间轴，同时保证不失控？

#### 1. 触发微调的两类输入与动态时间块切分规则
| 输入来源 | 触发条件（输入冲击力 `Impact > 当前卡片护甲 Armor`） | 动态时间轴微调效果（Block Split / Delay） |
| :--- | :--- | :--- |
| **输入一：读取到的用户消息** | **场景 1（摸鱼突变 + 繁忙半途结算）**：当前为 `🔥 BUSY_TASK`，收到 ★L3 核心用户呼叫，或群内多位 ◆L2 熟人高热度爆发聊大瓜 | 在当前时刻 `now_h` 将原 `BUSY_TASK` 时间块一分为二：<br/>① 前半段（`start_h ~ now_h`）按当前进度百分比结算一条**半途被打断的完成结果记忆**写入 Tier 1；<br/>② 后半段（`now_h ~ end_h`）切分为 **`IDLE_ROAM（空闲中）`**（放下手头正事，专心水群/陪聊） |
| **输入一：读取到的用户消息** | **场景 2（热聊熬夜）**：当前为 `☕ SURF / IDLE_ROAM` 且即将进入下一时间块 `💤 SLEEP`，但正与 ★L3 核心用户或熟人群热聊 | 将当前空闲块的 `end_hour` 延长 `0.5 ~ 1.0 小时`，并将下一块 `SLEEP` 的 `start_hour` 同步向后推迟 |
| **输入一：读取到的用户消息** | **场景 3（核心唤醒）**：当前为 `💤 SLEEP`，★L3 核心用户在私聊连续发消息强呼叫（普通/重要用户无法触发） | 从当前时刻 `now_h` 切分出一段 `0.5 ~ 1.0 小时` 的 **`IDLE_ROAM`**（迷迷糊糊醒来回复核心用户） |
| **输入二：刚刷到的抖音/B站视频** | **场景 4（刷片上头）**：当前在 `☕ SURF` 刷到了多模态评分 `>= 88` 的超搞笑/高能神作，且下一块原本要进入 `BUSY_TASK` 或 `SLEEP` | 触发“再刷亿会儿”：将当前 `SURF` 块延长 `0.5 ~ 1.0 小时`、推迟下一块 `BUSY_TASK / SLEEP` 的开始时间，并立刻把该视频分享出去！ |

#### 2. 三重防失控硬性限制（代码级强约束）
```python
class BoundedScheduleMutator:
    async def can_mutate(self, run: DailyScheduleRun, current_slot: SlotInstance, impact: int) -> bool:
        # 限制 1：每日最大微调次数配额（默认每天最多只允许变卦 2 次）
        if run.mutations_used >= self.config.max_mutations_per_day:
            return False

        # 限制 2：微调冷却时间锁（每次微调后，至少锁定 120 分钟不允许再次改动规划）
        if time.time() - run.last_mutation_ts < self.config.mutation_cooldown_seconds:
            return False

        # 限制 3：定力护甲门槛（输入冲击力必须严格大于当前卡片的定力护甲）
        if impact <= current_slot.armor:
            return False

        return True
```

---

## 四、 精简版分级记忆、定时遗忘与简易上下文管理器设计

参考 [`astrbot_plugin_livingmemory`](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory) 的生命周期思想，本插件**彻底剔除**了复杂的 2D/3D 图谱前端、知识图谱与高延迟的 Agent Tool 函数调用，仅用 **1 张轻量 SQLite 表 + 定时遗忘清理协程 + 上下文预算裁剪器** 实现高性价比记忆管理。

### 4.1 记忆表结构与三级分类模型 (`core/memory_manager.py`)

在 SQLite 中仅维护一张核心表 `memories`（配合 SQLite 原生全文索引或关键词标签匹配）：

```sql
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    soul_id TEXT NOT NULL,           -- 所属 AI 角色卡 ID (支持跨平台共享同一角色记忆)
    user_id TEXT,                    -- 关联的全局用户 ID (NULL 则代表 Bot 自身的繁忙成果/冲浪记忆)
    user_tier INTEGER DEFAULT 1,     -- 产生记忆时的用户等级 (1:普通, 2:重要, 3:核心)
    memory_tier INTEGER NOT NULL,    -- 记忆分级: 1=EPHEMERAL(瞬时), 2=RECENT(近期), 3=CORE(核心永久)
    content TEXT NOT NULL,           -- 精炼的一句话记忆内容 (严格控制在 60 字以内)
    keywords TEXT NOT NULL,          -- 逗号分隔的检索关键词标签 (如 "火锅,海鲜过敏,饮食")
    importance INTEGER DEFAULT 5,    -- 初始重要度评分 (1 ~ 10)
    vitality REAL DEFAULT 100.0,     -- 记忆当前活力值 (0.0 ~ 100.0，<=0 时被定时程序物理清理)
    access_count INTEGER DEFAULT 0,  -- 被召回使用的次数 (用于访问强化与晋升)
    created_at INTEGER NOT NULL,     -- 创建时间戳
    last_accessed_at INTEGER         -- 最后一次被召回的时间戳
);
```

#### 记忆写入与自动分级规则：
1. **Tier 1 (`EPHEMERAL = 1`，瞬时记忆)**：
   - 每日 `BUSY_TASK` 走完 / 被打断时随机结算的一句话【完成结果】、刚看完的 B站/抖音视频、或重要度 `<= 4` 的普通群聊小事。
2. **Tier 2 (`RECENT = 2`，近期记忆)**：
   - 对话累计满 `N` 条（默认 15 条）后，由后台**决策模型**（`role="decision"`）异步提炼出的近期事件（重要度 `5 ~ 7`），初始 `vitality = 100.0`。
3. **Tier 3 (`CORE = 3`，核心长效记忆)**：
   - 重要度 `>= 8`（如 ★L3核心用户 / ◆L2重要用户 的核心偏好、身份设定、重要约定），或由 Tier 2 记忆累计被召回 `access_count >= 4` 次后自动晋升而来。

---

### 4.2 🗑️ 定时遗忘与自动清理程序 (`run_forgetting_gc`)

后台定时任务每 24 小时（每天凌晨）执行一次物理清理，确保数据库绝不无限膨胀：

```python
import math
import time

class MemoryManager:
    async def run_forgetting_gc(self):
        """
        每日定时遗忘清理程序：保留重要记忆与最近记忆，物理删除不重要且衰减殆尽的记忆
        """
        now = int(time.time())
        
        # 1. 清理超过 48 小时的 Tier 1 (瞬时短时记忆)
        await self.db.execute(
            "DELETE FROM memories WHERE memory_tier = 1 AND (? - created_at) > 172800",
            (now,)
        )

        # 2. 结算所有 Tier 2 (近期记忆) 的每日活力衰减
        rows = await self.db.fetchall("SELECT id, importance, user_tier, vitality, access_count FROM memories WHERE memory_tier = 2")
        for row in rows:
            # 用户等级保护系数: L1普通=0.6(忘得快), L2重要=1.2, ★L3核心=2.0(忘得慢)
            tier_factor = {1: 0.6, 2: 1.2, 3: 2.0}.get(row["user_tier"], 1.0)
            reinforce_factor = 1.0 + 0.5 * math.log(1 + row["access_count"])
            
            # 计算当日衰减量 (基础衰减率设为 35.0)
            daily_decay = 35.0 / (max(1, row["importance"]) * tier_factor * reinforce_factor)
            new_vitality = row["vitality"] - daily_decay

            if new_vitality <= 0:
                # 🗑️ 活力归零：彻底遗忘，直接物理删除！
                await self.db.execute("DELETE FROM memories WHERE id = ?", (row["id"],))
            elif row["access_count"] >= 4 and row["importance"] >= 6:
                # 💎 多次被提及强化：从 Tier 2 (近期记忆) 自动晋升为 Tier 3 (核心永久记忆)！
                await self.db.execute("UPDATE memories SET memory_tier = 3, vitality = 100.0 WHERE id = ?", (row["id"],))
            else:
                await self.db.execute("UPDATE memories SET vitality = ? WHERE id = ?", (new_vitality, row["id"]))
```

---

### 4.3 🛡️ 简易上下文与预算管理器 (`core/context_budget.py`)

在 `@filter.on_llm_request()` 钩子中，通过 **`ContextBudgetManager`** 同时管控 **“原生历史对话长度”** 与 **“插入记忆的字数上限”**，防止上下文超载与费用失控：

```python
class ContextBudgetManager:
    async def apply_context_and_memory_budget(self, event: AstrMessageEvent, req, soul_id: str, user: UserProfile):
        # =====================================================================
        # 第一步：完全清空平台原生历史 (req.contexts.clear())，由插件全权接管上下文！
        # =====================================================================
        # 为什么要完全舍弃平台原生 req.contexts？
        # 1. 平台 ConversationManager 不会记录插件通过 context.send_message() 主动发送的视频/搭话消息，用平台上下文会导致 Bot 对自己刚主动发的话“失忆”；
        # 2. 平台群聊历史缺乏[引用回复解线团]与[L1/L2/L3关系等级]标注，且若平台开启了 group_icl_enable 会导致重复注入；
        # 3. 由插件统一维护固定长度滑窗 (滑出窗口即送去提炼分级记忆并销毁)，可形成“滑窗 -> 提炼记忆 -> 定时遗忘”的完美闭环！
        req.contexts.clear()

        # 注入插件自身维护的高信噪比会话滑窗 (含 Bot 主动发出的消息、引用拓扑与关系标记)
        session_window_msgs = self.session_buffer.get_formatted_window(
            umo=event.unified_msg_origin,
            max_turns=self.config.sliding_window_size  # 默认保留最近 15 条
        )
        req.contexts.extend(session_window_msgs)

        # =====================================================================
        # 第二步：按 [关键词匹配 + 时间新鲜度 + 重要度] 检索候选记忆
        # =====================================================================
        query_text = event.message_str
        candidates = await self.memory_manager.retrieve_candidates(soul_id=soul_id, user_id=user.global_uid)
        
        now = time.time()
        scored_memories = []
        for mem in candidates:
            # 1. 关键词命中分 (0.0 ~ 1.0)
            kw_list = [k.strip() for k in mem.keywords.split(",") if k.strip()]
            kw_hits = sum(1 for k in kw_list if k in query_text)
            kw_score = min(1.0, kw_hits * 0.5) if kw_list else 0.0
            
            # 2. 时间新鲜度分 Recency (越近越容易被想起，指数衰减)
            days_old = max(0.0, (now - mem.created_at) / 86400.0)
            recency_score = math.exp(-0.15 * days_old)
            
            # 3. 重要度分 Importance (0.1 ~ 1.0)
            imp_score = mem.importance / 10.0
            
            # 综合得分：保证“重要的(Core)”和“最近的(Recent)”以及“当前聊到的”排在最前
            total_score = 0.40 * kw_score + 0.35 * recency_score + 0.25 * imp_score
            scored_memories.append((total_score, mem))

        scored_memories.sort(key=lambda x: x[0], reverse=True)

        # =====================================================================
        # 第三步：硬性预算裁剪 (Hard Budget Gate：限制最大条数与最大字符数)
        # =====================================================================
        selected_lines = []
        current_chars = 0
        recalled_ids = []

        for score, mem in scored_memories:
            if len(selected_lines) >= self.config.max_injected_memories:  # 默认最多 6 条
                break
            if current_chars + len(mem.content) > self.config.max_memory_chars_budget:  # 默认最多 400 字
                break
            
            tag = "核心记忆" if mem.memory_tier == 3 else "近期记忆"
            selected_lines.append(f"- [{tag}] {mem.content}")
            current_chars += len(mem.content)
            recalled_ids.append(mem.id)

        # 异步更新被选中记忆的访问次数 (触发访问强化，延缓被遗忘)
        if recalled_ids:
            await self.memory_manager.reinforce_memories(recalled_ids)

        # 注入精简后的记忆块到 System Prompt
        if selected_lines:
            req.system_prompt += "\n\n[脑海中的相关记忆 (精简注入)]:\n" + "\n".join(selected_lines)
```

> 完整的 `apply_context_and_memory_budget()` 执行顺序为：**① 角色卡接管 Persona 段（`_takeover_persona`）→ ② `req.contexts.clear()` 并注入插件滑窗 → ③ 记忆混合打分与硬预算裁剪 → ④ 追加三态指令 / 变卦独白 / 记忆块**。其中第 ① 步见 §4.5。

---

### 4.4 🗂️ 插件自治设置表 (`plugin_settings`) 与旧配置迁移

v2.4 起，插件不再把模型选择写入 `AstrBotConfig`（`_conf_schema.json`）：AstrBot 会按 schema 做配置完整性检查，未声明的键可能被删掉，所以 **插件自治配置统一存放在 SQLite 的通用 KV 表**：

```sql
CREATE TABLE IF NOT EXISTS plugin_settings (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);
```

`storage/db.py` 提供 `async get_setting(key, default)`（JSON 反序列化）与 `async set_setting(key, value)`（JSON 序列化 UPSERT）。约定 key：

| key | value 类型 | 说明 |
| :--- | :--- | :--- |
| `model_config` | dict | 四角色模型配置（结构见 §5.1），由 Pages「模型」页读写 |
| `default_soul_id` | str | 插件默认角色（「会话」页可改；UMO 未绑定时使用） |
| `legacy_model_migrated` | bool | 旧版模型配置（`model_hub_config`）是否已尝试迁移，只尝试一次；这是唯一保留旧字段名的迁移说明 |

首次启动时 `ModelHub.load()` 会读取旧插件配置中的 `*_llm_provider_id`，经 `ProviderFactory.find_source_model_by_provider_id()` 反查出 `(source_id, model)` 迁移到 `model_config`。无论成功与否都会写入 `legacy_model_migrated = true`，之后不再重试；旧配置值从此不再生效，需在「模型」页重新确认。

---

### 4.5 🎭 角色卡接管原生 Persona 段 (`_takeover_persona`)

`ContextBudgetManager.apply_context_and_memory_budget()` 在**最开始**调用 `_takeover_persona(req, soul)`，把 AstrBot 原生注入的 `# Persona Instructions` 段整体替换为角色卡渲染结果：

```python
card_block = f"\n# Persona Instructions\n\n{render_persona_prompt(soul.card)}\n"
sp = req.system_prompt or ""

# ① 精确匹配：任一原生人格原文 -> 整段替换
for p in await self.soul_manager.list_native_prompts():
    seg = f"\n# Persona Instructions\n\n{p}\n"
    if seg in sp:
        req.system_prompt = sp.replace(seg, card_block, 1); return

# ② 正则兜底：替换到下一个一级标题或结尾
new_sp, n = re.subn(r"\n# Persona Instructions\n\n.*?(?=\n# |\Z)",
                    lambda _m: card_block, sp, count=1, flags=re.S)

# ③ 没找到 -> 前置角色卡
req.system_prompt = new_sp if n else card_block + sp
```

配套设计：

1. **单一数据源 `CARD_SCHEMA`**（`core/character_card.py`）：10 个分区 / 39 个字段，同时供后端渲染 Prompt 与前端 `GET card/schema` 动态生成表单。
2. **`SoulProfile.card`**：角色卡数据唯一来源；旧的 `name / archetype / speaking_style / surfing_preferences / native_persona_prompt` 退化为读写 `card` 的兼容 `@property`，旧档案经 `migrate_legacy()` 自动迁移。
3. **渲染预算与优先级**（`render_persona_prompt(card, max_chars=2000)`）：只渲染非空字段、按分区输出 Markdown；优先保留 `identity / speech / personality / social`，超预算时按 `examples > background > appearance > extra > rhythm > interests` 顺序丢弃低优先级分区，最后硬截断保证 `len(result) <= max_chars`；`native_persona_prompt` 只作导入来源记录，**不参与**渲染。
4. **会话路由 `resolve_soul(umo)`**：`UMO 绑定 → plugin_settings.default_soul_id → 最近更新的角色 → 创建默认角色`；不再读取平台 `default_personality`、不做 `persona_hash` 匹配，聊天路径上也不再用 LLM 播种卡池。
5. **AI 造卡**：`generate_card(seed_text, base_card, section)` 用**主模型**生成整卡或单个分区草稿（不落库）；`import_native_persona()` 把原生人设原文写入 `card["extra"]["native_persona_prompt"]`，可选 AI 整卡生成；四类专属行动卡池由**决策模型**生成（奇遇卡已移除）。

---

## 五、 插件自治四角色模型体系 (`ModelHub` × `ProviderFactory`) 与 AstrBot 平台协同设计

> **核心工程原则：插件自治模型 + 100% 复用 AstrBot 原生回复管线。**
> v2.4 起，插件不再读取 AstrBot 配置文件里的模型选择，而是通过 `ProviderFactory` 从「提供商源 + 模型名」构建或复用实例，管理 **4 个模型角色（对话 / 决策 / 视频解析 / TTS）**；主模型通过 `event.set_extra("selected_provider", ...)` 交给 AstrBot 原生管线使用，因此分段回复、流式输出、工具调用等原生能力全部保留。

### 5.1 🔌 四角色模型网关 (`core/model_hub.py`) + 提供商工厂 (`core/provider_factory.py`)

#### 1. 四个模型角色与继承规则

| 角色 (`role`) | 用途 | 继承规则 |
| :--- | :--- | :--- |
| `main`（对话） | 原生管线的主对话模型；也是 AI 生成整张角色卡的创作模型 | **必填**；未配置时插件不覆盖平台默认模型 |
| `decision`（决策） | 读空气、记忆提炼、日程微调、四类行动卡池 / 繁忙完成结果 / IDLE_ROAM 搭话生成（`small` 为兼容别名） | 未显式配置 → 继承 `main` |
| `vision`（视频解析） | 赛博冲浪看片的多模态评分；始终使用 `role="vision"`，不再降级到决策模型 | 未显式配置 → 继承 `main` |
| `tts`（语音） | 插件自建语音合成 | **不继承**；`mode="off"` 即关闭 |

模型配置存放在 `plugin_settings["model_config"]`（SQLite KV，见 §4.4），结构：

```json
{
  "main":     {"source_id": "deepseek", "model": "deepseek-flash"},
  "decision": {"inherit": true, "source_id": "", "model": ""},
  "vision":   {"inherit": true, "source_id": "", "model": ""},
  "tts": {"mode": "off|source|provider", "source_id": "", "model": "", "voice": "",
          "provider_id": "", "probability": 1.0, "scope": "private|l3|all", "dual_output": false}
}
```

- `ModelHub.ROLES = ("main", "decision", "vision", "tts")`；`role="small" / "aux"` 统一归一为 `decision`。
- `resolve_role(role)` 返回 `{source_id, model}`（TTS 返回 `{mode, ...}`）；`main` 未配置返回 `None`。
- `call_llm_text()` 直接调用实例的 `inst.text_chat(prompt=..., system_prompt=..., image_urls=...)`，不再经过 `context.llm_generate`；`call_llm_json()` 复用 `extract_json()` 容错解析。
- `save_model_config()` 归一化并写入 `plugin_settings.model_config` 后，会立即 `unregister_main()`，下一次使用按新配置热重建实例。
- **彻底删除**旧回退链：不再读 `context.get_config()` / `get_current_chat_provider_id()` / `default_image_caption_provider_id`；Embedding / Rerank 槽位删除。

#### 2. `ProviderFactory`：唯一触碰 AstrBot Provider 内部结构的模块

```python
class ProviderFactory:
    def list_sources(self) -> list[dict]              # 提供商源（绝不含 key，仅 has_key）
    async def list_models(self, source_id) -> list    # 已配置模型 + 远程 get_models() 拉取结果
    def list_tts_providers(self) -> list[dict]        # AstrBot 已启用 TTS 实例 + 已配置未启用条目
    async def find_source_model_by_provider_id(pid)   # 旧配置迁移：provider id -> (source_id, model)
    async def get_chat_provider(source_id, model)     # 先复用 inst_map 同源同模型且启用的实例，否则自建
    def register_main(inst) -> str                    # inst_map["onlyyou::main"] = inst；不 append 到 provider_insts
    def unregister_main() -> None
    async def get_tts_provider(tts_cfg)               # mode=source 自建 openai_tts_api；mode=provider 取现有实例
    async def dispose() -> None                       # 清理 onlyyou::main 并 terminate 全部自建实例
```

自建实例的关键细节（已核对 AstrBot 4.29.3 源码）：

1. **合并配置**：`{**source, **entry}`（provider 条目优先）；`entry` 形如 `{"id": "onlyyou::chat::<source>::<model>", "provider_source_id": ..., "model": ..., "enable": true, ...}`；`$ENV` / `${ENV}` 形式的 key 会从 `os.environ` 解析后写入配置。
2. **实例化**：`provider_manager.dynamic_import_provider(type)` → `provider_cls_map[type].cls_type(cfg, provider_manager.provider_settings)` → 若实例有 `initialize` 则 `await inst.initialize()`。
3. **实例复用与缓存**：先在 `inst_map` 中寻找 `provider_config.provider_source_id == source_id`、模型名匹配且未禁用的现有实例；找不到才自建。自建实例按 `(source_id, model)` 缓存。
4. **不污染平台模型列表**：插件自建实例**不会**被 append 到 `provider_insts`，只按需写入 `inst_map["onlyyou::main"]`；卸载 / 换模型时由 `dispose()` 统一移除并 terminate。
5. **防御式容错**：所有内部属性访问都是 `getattr` + `try/except`，失败只打 warning 并返回 `None` / `[]`，绝不炸掉插件生命周期。

#### 3. `selected_provider`：让原生管线使用插件选出的主模型

```python
# main.py —— 三态门禁决定放行之后、apply_reply_decision 之前
if should_reply:
    main_provider_id = await self.model_hub.get_main_provider_id()
    if main_provider_id:
        event.set_extra("selected_provider", main_provider_id)
self.platform_bridge.apply_reply_decision(event, should_reply=should_reply)
```

AstrBot 的 `astr_main_agent._select_provider()` 会先读 `event.get_extra("selected_provider")`，再通过 `context.get_provider_by_id()`（即查 `provider_manager.inst_map`）解析实例。`ModelHub.get_main_provider_id()` 的三种返回值：

| 返回值 | 含义 |
| :--- | :--- |
| `""` | 主模型未配置 / 实例创建失败 → 回退 AstrBot 平台默认模型（「模型」页红字提示） |
| 已有 AstrBot provider id | 复用了 AstrBot 已加载且启用的同源同模型实例 |
| `"onlyyou::main"` | 插件自建实例已注册进 `inst_map`（不会出现在平台模型列表） |

其余模型角色（decision / vision / tts）只用于插件内部 `inst.text_chat()` / `get_audio()` 调用，不进入原生回复管线；`main` 的 Persona 段则由角色卡接管（见 §4.5）。

#### 4. 插件自治 TTS（`on_decorating_result` 钩子，先于原生 TTS）

- **三种来源**：`mode="off"` 关闭；`mode="source"` 用提供商源（OpenAI 兼容）自建 `openai_tts_api` 实例（复用源的 `api_base` / `api_key`，音色取 `voice`，默认 `alloy`）；`mode="provider"` 直接使用 AstrBot 现有 TTS 提供商。
- **触发条件**：TTS 开启 **且** `random() <= probability` **且** `scope` 命中（`private` 私聊 / `l3` 当前用户为 ★L3 核心用户 / `all` 任意会话）。
- **流程**：`on_decorating_result` 钩子汇总结果链里所有 `Plain.text` → `model_hub.synthesize(text)` → 把 `Plain` 段替换为 `Record(file=..., url=..., text=...)`；`dual_output=true` 时额外保留一个 `Plain`。
- **为何不会重复播报**：AstrBot `ResultDecorateStage` 先执行 `on_decorating_result` 钩子（约第 160 行），再分段回复（约第 209 行），最后原生 TTS（约第 270 行起）。插件已把文字转成语音后，原生 TTS 拿不到可读的 `Plain` 文本；只有开启「语音 + 文字双发」时才可能与原生 TTS 重复，页面上会提示关闭原生 TTS。
- **主动消息**：`PlatformBridge.send_proactive_message(..., audio_path=...)` 在文字发送后追加一条 `Record` 语音；是否合成由 `ModelHub.get_tts_config()` 的开关 / 概率决定。

---

### 5.2 🤝 插件与 AstrBot 平台原生配置的八大协同法则 (`core/platform_bridge.py`)

为了避免插件功能与 AstrBot 平台自身的配置文件（`cmd_config.json` / `abconf_*.json`）产生冲突或双重执行，制定以下协同边界：

| 平台配置项 | AstrBot 平台原生机制 | ❌ 错误的插件做法（冲突根源） | ✅ **OnlyYou 协同设计方案（平台优先 + 管线复用）** |
| :--- | :--- | :--- | :--- |
| **1. 会话黑白名单与开关**<br/>(`Access Control`) | 平台自带群/私聊黑白名单，且每个配置文件（`abconf`）可独立开关插件 | 插件内部再搞一套群白名单配置，导致用户在两边重复配甚至逻辑冲突 | **插件不设任何黑白名单配置！**<br/>100% 遵循平台访问控制与多配置文件开关：只要消息事件流进插件，即代表平台已放行。 |
| **2. 群聊唤醒与拦截**<br/>(`WakeCheckStage`) | 原生逻辑：群里被 `@` 或命中唤醒词才设置 `should_call_llm=True` | 插件粗暴 `event.stop_event()` 后自己调 `send_message`，导致绕过平台所有后处理管线 | **通过 `event.should_call_llm(bool)` 协同管线！**<br/>• 群友没 `@` Bot，但读空气决定要接话 $\rightarrow$ 调 `event.should_call_llm(True)` 借用原生管线回复！<br/>• 群友 `@` 了 Bot，但处于 `💤休息中` 或 `🔥繁忙中(L1)` 或读空气决定不回 $\rightarrow$ 调 `event.should_call_llm(False)` 静默取消唤醒！ |
| **3. 分段回复**<br/>(`segmented_reply`) | 平台 `RespondStage` 原生支持按标点/正则拆分为多条短气泡并带随机打字延迟发送 | 插件自己切分句子调 `send_message`，或者与平台分段叠加导致一句话被切碎两次 | **全面复用平台 `segmented_reply` 配置！**<br/>• **被动回复**：经由 `should_call_llm(True)` 走原生 `RespondStage`，直接由平台完成分段与打字延迟！<br/>• **后台主动发消息（如刷完视频主动分享）**：无前置 `event` 时，`PlatformBridge` 直接读取 `self.context.get_config(umo)` 中的 `platform_settings.segmented_reply` 正则与间隔参数执行发送，体验与平台 100% 统一！ |
| **4. 流式输出**<br/>(`streaming_response`) | 平台支持边生成边流式推送（与分段回复/TTS 行为互斥或不同） | 插件在 Prompt 里写死要求输出 `<split>` 分段符，结果流式输出把 `<split>` 原样发给用户 | **动态感知自适应**：<br/>在 `@filter.on_llm_request` 中检查 `self.context.get_config(umo)` 的流式开关：若平台开启了流式输出或开启了原生正则分段，插件绝不在 Prompt 中注入自定义分隔符。 |
| **5. 图像理解与文生图**<br/>(`Image Caption / Vision / T2I`) | 1. 平台 `provider_settings.default_image_caption_provider_id` 提供默认识图模型；<br/>2. 原生多模态管线自动将用户发图传给支持视觉的主模型；<br/>3. 文生图通过平台生图插件（LLM Tool）触发 | 插件对群聊图片再调一次识图导致双倍计费；或在插件内重复造文生图轮子 | **用户发图走平台、后台看片走插件 `vision` 角色**：<br/>• **群聊/私聊用户发图**：走原生多模态/识图管线，插件不二次调接口，直接复用转述结果记入群聊窗口。<br/>• **后台刷 B站/抖音看片**：统一使用插件自治的 `vision` 角色（未单独配置时继承 `main`），**不再**读取平台 `default_image_caption_provider_id`。<br/>• **文生图**：直接复用 AstrBot 生态的画图 Tool，插件零侵入。 |
| **6. 语音输入与输出**<br/>(`STT / TTS`) | 1. `provider_stt_settings` 自动将用户语音转文字赋给 `event.message_str`；<br/>2. `provider_tts_settings` 在 `ResultDecorateStage` 中把回复转语音 | 插件自己再配一套 STT/TTS，导致不会处理语音或发出两条重复语音 | **STT 委派平台、TTS 插件自治（先于原生）**：<br/>• **语音输入 (STT)**：平台前置管线已自动转成文字，插件直接读 `event.message_str`。<br/>• **语音输出 (TTS)**：插件通过 `on_decorating_result` 钩子先于平台分段回复与原生 TTS 执行，把 `Plain` 文本合成为 `Record` 语音（`ModelHub` 的 `tts` 角色，来源：关闭 / 提供商源自建 / AstrBot TTS）。插件转完后原生 TTS 拿不到可读文字，不会重复播报；仅开启「语音 + 文字双发」时需在页面提示关闭原生 TTS。 |
| **7. 上下文消息注入与群聊感知**<br/>(`req.contexts` & `group_icl_enable`) | 平台 `ConversationManager` 按 `umo` 维护 `req.contexts`，且有 `group_icl_enable` 注入群聊记录 | 1. **致命缺陷**：插件后台调用 `context.send_message()` 主动发的视频和消息**不会进入平台 `ConversationManager`**，导致用户回复时 Bot 根本不知道自己刚主动发了啥；<br/>2. 平台群聊记录无解线团与等级标记，且与插件滑窗双重注入浪费 Token | **完全舍弃平台 `req.contexts`，由插件 100% 接管上下文管理！**<br/>• 在 `@filter.on_llm_request` 中直接执行 **`req.contexts.clear()`**，彻底丢弃平台塞入的原生历史与 `group_icl` 记录（但保留当前轮次的 `req.prompt` 与 `req.image_urls`）。<br/>• **插件统一滑窗闭环**：无论是用户消息、被动回复、还是 **Bot 后台主动发送的视频/消息**，全部统一写入插件的会话滑窗（`sliding_window_size = 15`）；当消息滑出窗口满 15 条时，触发决策模型提炼为分级记忆后直接销毁原始消息！ |
| **8. 主模型选择与人格注入**<br/>(`selected_provider` / Persona) | 1. `astr_main_agent._select_provider()` 优先读 `event.get_extra("selected_provider")`，再经 `context.get_provider_by_id()`（即查 `provider_manager.inst_map`）解析实例；<br/>2. 人格以 `\n# Persona Instructions\n\n{prompt}\n` 形式注入 System Prompt | 插件自行调 LLM 再 `send_message`，绕过原生分段/流式/工具调用；或直接改平台配置文件导致热重载后失效 | **插件选模型 + 接管人格段**：<br/>• 门禁放行后执行 `event.set_extra("selected_provider", main_provider_id)`，回复仍由 AstrBot 原生管线生成与发送（分段/流式/工具调用全部保留）。<br/>• 角色卡通过 `_takeover_persona` 三分支替换 `# Persona Instructions` 段；AstrBot 原生 Persona 只作为一次性导入来源。 |

---

## 六、 配置体系架构：`_conf_schema.json` + `plugin_settings` + 全功能 `Pages` 可视化控制台

> **设计原则**：本插件采用 **三层配置架构**，动态数据与静态阈值彻底分离：
> 1. **`views/console/`（AstrBot Plugin Pages 可视化控制台）**：承担所有动态交互管理——角色卡、行动卡片池、今日三态日程、用户关系、分级记忆、冲浪视频池，以及 **插件自治的四角色模型配置**（详见 [`docs/WEBUI_DESIGN.md`](WEBUI_DESIGN.md)）。
> 2. **`_conf_schema.json`（全局运行参数）**：只保留 Roguelite / 关系 / 读空气 / 冲浪 / 记忆与上下文的算法阈值和 API 基础设置；**不再包含任何模型绑定**（模型配置见 §5.1 与 §4.4）。
> 3. **SQLite `plugin_settings` 表（插件自治设置，见 §4.4）**：存放 `model_config`、`default_soul_id`、`legacy_model_migrated` 等插件自治数据。**不放进 `AstrBotConfig`**，避免 AstrBot 的 schema 完整性检查把未声明的键删掉导致配置丢失。

### 6.1 三层配置归属对照

| 配置数据 | 存放位置 | 读写入口 |
| :--- | :--- | :--- |
| 四角色模型选择（main / decision / vision / tts） | `plugin_settings["model_config"]` | Pages「模型」页（`models/config`） |
| 插件默认角色 | `plugin_settings["default_soul_id"]` | Pages「会话」页（`sessions/default_soul`） |
| 角色卡 10 分区 / 四类行动卡池 | SQLite `souls` 表 `profile_json` | Pages「角色卡」「日程」页 |
| 用户关系 / 分级记忆 / 冲浪历史 | SQLite `users` / `memories` / `surfing_history` | Pages「关系」「记忆」「冲浪」页 |
| 三态门禁、读空气、冲浪、记忆与上下文阈值 | `_conf_schema.json` | AstrBot 插件配置面板 |

### 6.2 最终 `_conf_schema.json`

以下为 v2.5 最终 schema 全文（不含任何模型绑定，保持 JSON 合法；已移除 `daily_encounter_count`，新增 `idle_roam_cooldown_minutes` 与 6 个未读消息相关配置）：

```json
{
  "roguelite_schedule_config": {
    "description": "🎲 Roguelite 随机日程引擎与三态微调设置",
    "type": "object",
    "items": {
      "enable_roguelite_schedule": {
        "description": "开启每日 24h 动态时间轴自动构筑（按角色作息曲线从四类行动卡池随机拼接弹性时间块）",
        "type": "bool",
        "default": true
      },
      "max_mutations_per_day": {
        "description": "每日日程最大微调次数上限（根据收到的消息或刷到的视频临时改变状态的次数上限）",
        "type": "int",
        "default": 2
      },
      "mutation_cooldown_minutes": {
        "description": "日程状态微调后的冷却锁定时间（分钟，防止频繁横跳）",
        "type": "int",
        "default": 120
      },
      "allow_l3_wake_from_resting": {
        "description": "允许 ★L3核心用户 在【休息中】状态下通过私聊唤醒 Bot",
        "type": "bool",
        "default": true
      },
      "idle_roam_cooldown_minutes": {
        "description": "【空闲游荡卡 (IDLE_ROAM)】翻群吃瓜 / 发呆记忆联想的主动搭话冷却时间（分钟）",
        "type": "int",
        "default": 45
      },
      "unread_private_reply_enabled": {
        "description": "【未读消息】私聊未读主动补回开关：IDLE 空闲时对休息/繁忙期间错过的私聊消息主动补发一条自然回复",
        "type": "bool",
        "default": true
      },
      "unread_group_ttl_minutes": {
        "description": "【未读消息】群聊未读消息的有效期（分钟）：超过后不再翻群吃瓜，避免翻隔夜旧瓜",
        "type": "int",
        "default": 120
      },
      "unread_private_ttl_minutes": {
        "description": "【未读消息】私聊未读消息的有效期（分钟）：醒来/忙完后仍会补回的时限（默认 12 小时）",
        "type": "int",
        "default": 720
      },
      "unread_max_per_chat": {
        "description": "【未读消息】单个群/私聊会话最多保留的未读消息条数（超出丢弃最旧）",
        "type": "int",
        "default": 20
      },
      "unread_group_cooldown_minutes": {
        "description": "【未读消息】同一个群的翻群吃瓜冷却（分钟），防止反复翻同一个群",
        "type": "int",
        "default": 60
      },
      "unread_private_cooldown_minutes": {
        "description": "【未读消息】同一个私聊会话主动补回的冷却（分钟），防止连续补发",
        "type": "int",
        "default": 60
      }
    }
  },
  "relationship_config": {
    "description": "💖 三级用户关系设置 (普通L1 / 重要用户L2 / 核心用户L3)",
    "type": "object",
    "items": {
      "core_user_ids": {
        "description": "★ L3 核心用户 (管理员) 账号列表",
        "type": "list",
        "items": {
          "type": "string"
        },
        "default": []
      },
      "important_user_ids": {
        "description": "◆ 初始指定的 L2 重要用户列表（繁忙状态下依然会回复的对象）",
        "type": "list",
        "items": {
          "type": "string"
        },
        "default": []
      },
      "enable_auto_promotion_l2": {
        "description": "允许普通用户(L1)通过群聊良性互动自动晋升为重要用户(L2)",
        "type": "bool",
        "default": true
      }
    }
  },
  "atmosphere_config": {
    "description": "🌬️ 群聊主战场：解线团与“读空气”引擎设置",
    "type": "object",
    "items": {
      "takeover_group_chat": {
        "description": "全面接管群聊（结合当前三态与读空气决定是否回复）",
        "type": "bool",
        "default": true
      },
      "debounce_seconds": {
        "description": "群聊听话防抖等待窗口（秒）",
        "type": "float",
        "default": 4.0
      }
    }
  },
  "surfing_config": {
    "description": "📱 赛博冲浪：空闲状态下刷 B站 & 抖音设置",
    "type": "object",
    "items": {
      "enable_bili_surfing": {
        "description": "在【空闲中】状态下主动刷哔哩哔哩 (Bilibili)",
        "type": "bool",
        "default": true
      },
      "bili_cookie": {
        "description": "B站 Cookie (选填)",
        "type": "text",
        "secret": true,
        "default": ""
      },
      "enable_douyin_surfing": {
        "description": "在【空闲中】状态下主动刷抖音 (Douyin)",
        "type": "bool",
        "default": true
      },
      "douyin_api_base": {
        "description": "抖音解析 API 服务地址",
        "type": "string",
        "default": "https://api.douyin.wtf"
      },
      "send_raw_video": {
        "description": "优先直发无水印视频文件",
        "type": "bool",
        "default": true
      }
    }
  },
  "memory_and_context_config": {
    "description": "🧠 精简版分级记忆、自动遗忘与上下文预算控制",
    "type": "object",
    "items": {
      "enable_tiered_memory": {
        "description": "开启分级记忆与每日自动遗忘清理 (Forgetting GC)",
        "type": "bool",
        "default": true,
        "hint": "自动提炼核心记忆(永久)与近期记忆(随时间衰减并由定时程序自动清理不重要记忆)。"
      },
      "sliding_window_size": {
        "description": "单会话短期滑动窗口保留消息条数（清空平台 req.contexts 后由插件注入的近期消息上限，滑出窗口即送去提炼记忆）",
        "type": "int",
        "default": 15
      },
      "extract_every_n_messages": {
        "description": "滑出窗口每累计多少条有效消息触发一次后台小脑记忆提炼",
        "type": "int",
        "default": 15
      },
      "max_injected_memories": {
        "description": "单次对话最多允许插入的记忆条数上限",
        "type": "int",
        "default": 6
      },
      "max_memory_chars_budget": {
        "description": "单次对话插入记忆的最大总字数预算（汉字数，防止费用过高）",
        "type": "int",
        "default": 400
      }
    }
  }
}
```

---

## 七、 测试与验证体系

离线 `tests/` 测试套件（stdlib `unittest`，零网络依赖，不要求安装 AstrBot / pytest；当前基线 **189 tests**），覆盖：

| 测试文件 | 覆盖范围 |
| :--- | :--- |
| `tests/test_provider_factory.py` | fake `provider_manager`：复用已启用实例、自建实例、按 `(source, model)` 缓存、`register_main` / `dispose` 清理 `inst_map` |
| `tests/test_model_hub.py` | `decision` / `vision` 继承 `main`、`tts` 未配置时关闭、`get_main_provider_id` 三分支、全程不调用 `context.get_config` / `get_current_chat_provider_id` |
| `tests/test_character_card.py` | 旧档案迁移、扁平属性兼容、`render_persona_prompt` 跳过空字段并遵守预算 |
| `tests/test_persona_takeover.py` | 精确替换 / 正则兜底 / 前置三分支 |
| `tests/test_web_api.py` | 用 `set_mock_request` 调用 27 条路由并校验 `{status, message, data}` 信封；dashboard/cards/force_state/ai_generate 按 v2.5 新 payload 断言（`action_type/progress_pct/chronotype/busy_results/action_types`） |
| `tests/test_regressions.py` | 24h 动态时间轴不变量 / 7~11 块 / 0.5h 网格、chronotype 权重差异、旧卡与旧日程迁移、进度与时间格式化、BUSY_TASK 结算写 Tier1 且不重复、IDLE_ROAM 双分支与冷却、mutator 切分/加钟/唤醒、三态门禁矩阵、Forgetting GC、上下文预算 |
| `tests/test_js_syntax.py` | `node --check views/console/*.js` 前端语法检查 + `index.html` 脚本清单检查（无 node 时自动 skip） |

```bash
# 本地离线测试（项目根目录）
python3 -m unittest discover -s tests -v
# Python 语法 / 前端语法检查
python3 -m compileall -q core storage surfing prompts main.py
node --check views/console/*.js
```
