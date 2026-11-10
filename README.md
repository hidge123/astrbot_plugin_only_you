# OnlyYou (只有你) —— AstrBot 跨平台 AI 网友与深度拟人陪伴插件

> **“在各个群聊里，我是能和大家接梗互损、会顺手往群里丢沙雕视频的鲜活 AI 网友；我每天按自己的作息曲线（早睡早起 / 规律 / 夜猫子）排出一整条 24 小时动态时间轴——睡觉卡完全静默、繁忙卡带着 0%→100% 进度条闷头干活并在结束时随机结算一句今日成果、冲浪卡真的去刷 B站/抖音、空闲游荡卡则翻群吃瓜或发呆想起旧事；我还会根据你的消息或刷到的视频实时切分 / 延展时间块。”**
>
> v2.5 起，日程引擎升级为 **「角色专属四类可执行行动卡（`SLEEP` / `SURF` / `IDLE_ROAM` / `BUSY_TASK`）× 基于作息曲线的 24h 动态积木时间轴 × 每日随机繁忙完成结果短时记忆」**，彻底移除旧的固定 6 时段与奇遇谈资卡；同时保留 **10 分区 × 39 字段的详细角色卡**（可手填 / AI 生成，完全接管说话方式）、插件自治的 **四角色模型（对话 / 决策 / 视频解析 / TTS）** 与 **灰白 + 青绿配色的可视化控制台**。

---

## 1. 核心设计总览

**`astrbot_plugin_only_you`（OnlyYou）** 旨在解决传统 AstrBot 机器人“被动客服感”、“日程死板千篇一律”、“群聊乱接话”以及“人设单薄、模型配置与平台强绑定”等痛点，围绕以下核心模型构建：

### ① 三态门禁 × 四类可执行行动卡（`SLEEP` / `SURF` / `IDLE_ROAM` / `BUSY_TASK`）
底层仍由 `RESTING` / `BUSY` / `IDLE` 三态控制**回消息门禁**，但日程表上的每一块时间都同时归属**四类可执行行动卡**之一——不再有“只有标题、没有实际行动”的静态事件卡：

| 行动卡 (`action_type`) | 绑定状态 | 贴合任意虚构角色的示例 | 回消息门禁 | 产生的真实后台行动 |
| :--- | :--- | :--- | :--- | :--- |
| **① 睡觉卡 (`SLEEP`)** | **💤 `RESTING`** | 现代网友*“戴眼罩呼呼大睡”* / 修仙者*“闭关调息入定”* / 机娘*“机能休眠充电”* | **完全静默**，仅 ★L3 私聊强呼叫可击穿护甲唤醒 | **什么都不做**，完全静默 |
| **② 冲浪卡 (`SURF`)** | **☕ `IDLE`** | *“瘫在懒人沙发上刷B站”* / *“用玉简偷看凡间短视频”* | **全量开放**（L1/L2/L3 读空气接话） | **真实刷 B站/抖音**：拉取推荐流与热评 → `vision` 模型看片打分 → 生成第一人称吐槽并分享给 ★L3 / 群聊 |
| **③ 空闲游荡卡 (`IDLE_ROAM`)** | **☕ `IDLE`** | *“泡杯热茶看窗外发呆”* / *“御剑在后山兜风”* | **全量开放**（L1/L2/L3 读空气接话） | **翻群吃瓜**（扫描近期群聊滑窗插话 / 私聊 L3 吐槽）或 **发呆记忆联想**（抽 1~2 条旧记忆 / 今日繁忙成果主动搭话） |
| **④ 角色繁忙卡 (`BUSY_TASK`)** | **🔥 `BUSY`** | *“赶商业插画线稿”* / *“在丹房炼制回春丹”* / *“编写渗透脚本”* | **熟人特权过滤**：忽略 L1，仅抽空简短回复 ◆L2 / ★L3 | **极简三要素**：① 注入当前行为记忆 ② 随时间推进 `0%→100%` 进度条 ③ 走完或被中断时随机生成一句今日【完成结果】写入 **Tier 1 瞬时记忆** |

### ② 24h 动态弹性时间轴 + 角色专属行动卡池（取代旧固定 6 时段 + 奇遇卡）
- **角色专属卡池（8~12 张）**：导入 / 创建角色卡时，由决策模型一次性生成贴合其职业、爱好与作息的四类行动卡；卡片名称固定在池中，可在控制台随时增删改。
- **每日随机排轴**：每天无需再调大模型造卡，直接按 **角色作息类型（`rhythm.chronotype`：早睡早起 / 规律 / 夜猫子）曲线 × 今日基调卡 × 相邻连携** 从卡池加权抽卡，并在每张卡的 `[min_hours, max_hours]` 内按 `0.5h` 步长随机掷出时长，让游标从 `0.0` 无缝推进到 `24.0`，自然形成 **7~11 个长短不一的弹性时间块**。
- **每天都不同的繁忙成果**：`BUSY_TASK` 每次走完 100%（或中途被打断）都会由决策模型生成一句**当天专属、带随机小细节的完成结果**（如*“回春丹炼成了但最后一炉差点炸炉”*），直接写入 Tier 1 短时记忆，彻底替代旧版奇遇卡作为聊天谈资。
- **旧数据平滑迁移**：旧 6 时段 / 过夜日程会被自动重抽；旧事件卡按 `state + title` 自动推导 `action_type` 与合理时长区间，无需手工改库。
- **未读消息补回**：睡觉 / 繁忙时被门禁拦下的消息会进入独立未读队列（读空气判“不回复”的不算）；醒来或忙完后，**私聊主动补回一句自然回复**，**群聊先读空气决策再决定是否插话**，消费即清空，可 `/onlyyou unread [list|flush|clear]` 调试。

### ③ 输入驱动的受限日程微调（动态切分 / 延展真实时间块）
- **无输入不变卦**：AI 绝不凭空乱改计划，只有收到高冲击力的真实外部输入——即 **读取到的用户消息**（如 ★L3核心用户找它、或 L2熟人群聊爆大瓜）或 **刚刷到的 B站/抖音高分视频** 时，才会击穿当前时间块护甲并实时改写时间轴：
  - **繁忙半途结算**：`BUSY_TASK` 被击穿时按当前进度一分为二——前半段立即结算一条“被打断”的 Tier 1 记忆，后半段切为 `IDLE_ROAM` 陪你摸鱼；
  - **睡觉被吵醒**：`SLEEP` 被 ★L3 私聊强呼叫击穿时，从当前时刻切出一小段 `IDLE_ROAM` 清醒窗，其余睡眠段保留；
  - **刷片上头加钟**：`SURF / IDLE_ROAM` 期间刷到高分视频或正热聊时，当前块延长 `0.5h` 并顺延 / 吞并下一块。
- **三重防失控限制**：受 **每日最大微调次数（默认 2 次）**、**微调冷却锁（120 分钟）** 与 **时间块定力护甲（`Impact > Armor`）** 严格限制，且每次微调后自动重排块编号、保证 24h 时间轴连续无缝。

### ④ 三级用户关系体系（L1 普通 / L2 重要用户 / ★L3 核心用户）
- **L1 普通用户**：仅在 Bot 处于 `空闲中` 时通过群聊读空气互动；`繁忙中` 直接忽略。
- **L2 重要用户（群聊熟人，支持好感度自动晋升）**：即便 Bot 处于 `繁忙中` 也会抽空回复；在 `空闲中` 刷到对口视频会在群里主动 `@` 他。
- **★ L3 核心用户（管理员 / OnlyYou）**：最高优先级，消息最容易击穿 `繁忙` 护甲让它停下手头事陪你；独享高频私聊陪伴与“群聊吃瓜，私聊耳语”。

### ⑤ 精简版分级记忆、定时自动遗忘与上下文预算管理
- **参考并精简自 `livingmemory`**：剔除笨重的 2D/3D 图谱看板与高延迟的 Agent Tool 调用，采用轻量化单表 SQLite + 「关键词 + 时间新鲜度 + 重要度」混合打分召回（不使用 Embedding / Rerank）。
- **三级记忆与定时遗忘（Forgetting GC）**：**Tier 3 核心记忆**（永久保留重要事实与约定）、**Tier 2 近期记忆**（按重要度与访问次数动态衰减）、**Tier 1 瞬时记忆**（今日繁忙卡的随机完成结果、刚看的视频与翻群吃瓜见闻，活 48h）。每日凌晨自动结算活力值并**物理清理**不重要的过期记忆。
- **上下文预算管理器**：完全接管平台原生历史（`req.contexts.clear()` + 插件滑窗），并严格限制单次注入记忆的最大条数（默认 `<= 6 条`）与总字数预算（默认 `<= 400 字`），杜绝上下文窗口超载与 Token 费用失控。

### ⑥ 详细角色卡（10 分区 × 39 字段）：人设的单一数据源
- **结构**：基础身份、外貌形象、性格内核、背景故事、兴趣喜好、说话方式、社交关系（对 L1/L2/L3 的态度）、生活作息、示例对话、补充设定（含原生人设原文）共 **10 个分区 / 39 个字段**（`core/character_card.py` 的 `CARD_SCHEMA`）。
- **完全接管人设 Prompt**：`ContextBudgetManager._takeover_persona` 用角色卡渲染结果替换 AstrBot 原生 `# Persona Instructions` 段（精确匹配 → 正则兜底 → 前置三分支），AstrBot 原生 Persona 只作为**一次性导入来源**。
- **AI 造卡**：支持整卡生成与单分区补全（主模型），结果作为草稿预览、采用后才保存；旧的 4 字段扁平档案会自动迁移。
- **渲染预算**：`render_persona_prompt(card, max_chars=2000)` 只渲染非空字段，优先保留身份 / 说话方式 / 性格 / 社交，超预算时按优先级丢弃低价值分区，硬保证不超预算。

### ⑦ 插件自治四角色模型（`ProviderFactory` + `ModelHub` + `selected_provider`）
- **四角色**：**对话（`main`，必填）**、**决策（`decision`，读空气 / 记忆提炼 / 日程微调 / 造卡）**、**视频解析（`vision`）**、**语音（`tts`）**；`decision` / `vision` 留空自动继承 `main`，`tts` 不继承。配置存放在 SQLite `plugin_settings.model_config`（不写进 `_conf_schema.json`，避免平台 schema 检查删键）。
- **不重复配置密钥**：`core/provider_factory.py` 从 AstrBot 提供商源 + 模型名构建实例；平台已有同源同模型且启用的实例则直接复用。
- **主模型仍走原生管线**：门禁放行后执行 `event.set_extra("selected_provider", main_provider_id)`，AstrBot `_select_provider()` 会解析该 extra（`provider_manager.inst_map`），分段回复、流式输出、工具调用全部保留；插件自建主模型注册为 `onlyyou::main`，不出现在平台模型列表。
- **TTS 插件自治**：三种来源（关闭 / 提供商源自建 / AstrBot TTS）、触发概率、生效范围（私聊 / L3 / 全部）、语音 + 文字双发；`on_decorating_result` 钩子**先于原生分段回复与原生 TTS** 执行，不会重复播报。
- **不重造轮子、不与平台配置打架**：黑白名单 100% 遵循平台；被动消息通过 `event.should_call_llm(True/False)` 协同原生管线；后台主动消息复用平台 `segmented_reply` 参数；用户发图走平台原生多模态/识图管线。

### ⑧ 灰白 + 青绿的可视化控制台（AstrBot Plugin Pages）
- **布局**：48px 精简顶栏（品牌 / 当前角色 / 刷新 / 主题）+ 216px 侧栏（运行：概览、日程；角色：角色卡；关系；记忆；冲浪；系统：模型、会话）+ 主工作区。
- **页面**：**概览**（状态环形表 + 24h 弹性日程轴 + 实时繁忙进度条 + 今日繁忙成果/配额/关系/记忆/模型小卡）、**日程**（24h 动态时间轴 + 四类行动卡网格与编辑器）、**角色卡**（10 分区动态表单 + AI 造卡 + 图片头像；未保存修改离开/切角色前会弹窗确认）、**关系**（三列看板，L1/L2 拖拽改级；★L3 管理员级不参与好感度/自动晋升，可在「⋯」菜单显式设置或由管理员身份自动确定）、**记忆**（Tier 分层列表 + 提炼/GC）、**冲浪**（视频卡片墙）、**模型**（四角色配置 + 测试 + TTS 试听，修改即时生效自动保存）、**会话**（默认角色 + UMO 绑定）。
- **视觉**：灰白底 + 青绿主色（浅色 `#fafafa` / 深色 `#0f1115`，主色 `#0d9488` / `#2dd4bf`）；三态色 `IDLE #10b981` / `BUSY #f59e0b` / `RESTING #6366f1`；内联 SVG 图标替代 emoji，长说明改为悬浮提示；无构建 classic script，支持浅/深色。

---

## 2. 项目结构

```text
OnlyYou/
├── metadata.yaml                # 插件元数据 (name: astrbot_plugin_only_you)
├── _conf_schema.json            # AstrBot WebUI 配置 Schema（仅运行参数；无模型绑定）
├── requirements.txt             # Python 依赖
├── main.py                      # 插件入口：三态门禁 / selected_provider / LLM 钩子 / on_decorating_result TTS / 后台任务 / 指令
├── core/                        # 核心逻辑
│   ├── state_machine.py         # 三态门禁控制器 + 四类行动卡枚举 (ActionType)
│   ├── roguelite_deck.py        # 24h 动态积木时间轴引擎（专属卡池 / 作息曲线 / 弹性块拼接）
│   ├── schedule_mutator.py      # 输入驱动的受限微调（繁忙半途结算 / 睡眠唤醒 / 加钟延展）
│   ├── character_card.py        # 详细角色卡（CARD_SCHEMA 10 分区 / 39 字段 + Prompt 渲染）
│   ├── soul_manager.py          # Soul 管理 / 会话路由 / AI 造卡 / Persona 导入
│   ├── user_relation.py         # 三级用户关系
│   ├── memory_manager.py        # 分级记忆 + Forgetting GC
│   ├── context_budget.py        # 上下文/记忆预算 + Persona 接管
│   ├── provider_factory.py      # AstrBot Provider 内部结构封装（复用/自建实例、TTS）
│   ├── model_hub.py             # 插件自治四角色模型网关
│   ├── platform_bridge.py       # 平台协同桥（唤醒 / 分段 / 主动语音消息）
│   ├── web_api.py               # 27 条 Plugin Pages Web API
│   └── atmosphere_reader.py     # 群聊读空气 / 解线团
├── views/console/               # 控制台前端（无构建 classic script）
│   ├── index.html / style.css   # 外壳 + 设计令牌（浅色 / 深色）
│   ├── app.js / icons.js / charts.js / mock.js
│   └── page-*.js                # 概览 / 日程 / 角色卡 / 关系 / 记忆 / 冲浪 / 模型 / 会话 共 8 页
├── surfing/                     # B站 / 抖音冲浪管线与适配器
├── storage/db.py                # aiosqlite 持久化（含 plugin_settings KV 表）
├── prompts/templates.py         # 集中管理 Prompt 模板
├── tests/                       # 离线 unittest 测试套件
└── docs/                        # 设计文档
```

---

## 3. 文档套件导航

| 文档路径 | 文档名称 | 核心内容概述 |
| :--- | :--- | :--- |
| **[docs/PRD.md](docs/PRD.md)** | **产品需求文档 (PRD v2.5)** | 三态门禁 × 四类可执行行动卡、24h 动态弹性时间轴与繁忙结果短时记忆、输入驱动的块切分微调、分级记忆与定时遗忘、**详细角色卡（10 分区）**、**插件自治四角色模型**与平台协同边界。 |
| **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** | **技术架构与 AstrBot 实现方案 (v2.5)** | `BotState` 三态门禁 + `ActionType` 四类行动卡、24h 动态时间轴拼接与作息曲线、繁忙半途结算与 IDLE_ROAM 调度、`MemoryManager` 活力衰减与物理清理、`ContextBudgetManager` 裁剪与 **Persona 接管**、**`ProviderFactory` / `ModelHub` / `selected_provider`**、`plugin_settings` 表与最终 `_conf_schema.json`。 |
| **[docs/WEBUI_DESIGN.md](docs/WEBUI_DESIGN.md)** | **Plugin Pages 控制台方案 (v2.5)** | 48px 顶栏 + 216px 侧栏布局、灰白+青绿设计令牌、8 个页面设计要点（含 24h 弹性轴 / 四类卡编辑器 / 繁忙成果卡）、Bridge 通信封装、**27 条 Web API 契约**。 |
| **[docs/PROMPTS_AND_FLOWS.md](docs/PROMPTS_AND_FLOWS.md)** | **Prompt 工程与交互时序 (v2.5)** | 角色卡整卡生成 / 分区补全 Prompt、四类专属行动卡池生成、繁忙完成结果与翻群 / 联想主动搭话 Prompt、受限状态微调、读空气、记忆提炼、**Persona 接管与插件 TTS 时序**。 |
| **[AGENTS.md](AGENTS.md)** | **开发、部署与联调规范** | 目录与模块职责、敏感信息安全规范、**AstrBot v4 底层机制与避坑指南**（`selected_provider`、Persona 段替换、`on_decorating_result`、`plugin_settings`、Provider 实例化）、**24h 时间轴不变量与繁忙结算契约**、Docker 部署、SSH 隧道 + OpenAPI 真机联调流程。 |

---

## 4. 快速开始与本地测试

1. **安装**：将插件目录放入 AstrBot 的 `data/plugins/astrbot_plugin_only_you`，安装 `requirements.txt` 依赖，重启 / 热重载插件。
2. **配置**：
   - 在 AstrBot 插件配置面板设置 Roguelite / 关系 / 读空气 / 冲浪 / 记忆阈值（`_conf_schema.json` 只含这些运行参数）；
   - 打开插件 Pages 控制台，在「模型」页为 **对话（必填）/ 决策 / 视频解析 / TTS** 选择提供商源与模型名（配置自动存入 `plugin_settings`）；
   - 在「角色卡」页手填或 AI 生成角色卡，在「会话」页设置默认角色与 UMO 绑定。
3. **本地离线测试（零网络、无需安装 AstrBot / pytest）**：

   ```bash
   python3 -m unittest discover -s tests -v
   python3 -m compileall -q core storage surfing prompts main.py
   node --check views/console/*.js
   ```

4. **离线预览控制台**：直接打开 `views/console/index.html?mock=1`（`mock.js` 提供全套 Mock 数据，可截图验收浅色 / 深色主题）。
5. **真机联调凭据**：远程服务器 SSH、AstrBot OpenAPI Key、Dashboard 登录账号等统一放在本地 `.env`（已 gitignore），变量清单与联调命令见 [AGENTS.md](AGENTS.md) 第二、四节。

---

## 5. 界面预览（本地 Mock 截图）

`docs/screenshots/` 收录了 8 个页面在无后端 Mock 模式下的浅色 / 深色截图（Chrome headless 生成）：

| 页面 | 浅色 | 深色 |
| :--- | :--- | :--- |
| 概览 | [overview-light.png](docs/screenshots/overview-light.png) | [overview-dark.png](docs/screenshots/overview-dark.png) |
| 日程 | [schedule-light.png](docs/screenshots/schedule-light.png) | [schedule-dark.png](docs/screenshots/schedule-dark.png) |
| 角色卡 | [character-light.png](docs/screenshots/character-light.png) | [character-dark.png](docs/screenshots/character-dark.png) |
| 关系 | [relations-light.png](docs/screenshots/relations-light.png) | [relations-dark.png](docs/screenshots/relations-dark.png) |
| 记忆 | [memory-light.png](docs/screenshots/memory-light.png) | [memory-dark.png](docs/screenshots/memory-dark.png) |
| 冲浪 | [surfing-light.png](docs/screenshots/surfing-light.png) | [surfing-dark.png](docs/screenshots/surfing-dark.png) |
| 模型 | [models-light.png](docs/screenshots/models-light.png) | [models-dark.png](docs/screenshots/models-dark.png) |
| 会话 | [sessions-light.png](docs/screenshots/sessions-light.png) | [sessions-dark.png](docs/screenshots/sessions-dark.png) |
