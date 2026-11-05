# OnlyYou (只有你) —— AstrBot 跨平台 AI 网友与深度拟人陪伴插件

> **“在各个群聊里，我是能和大家接梗互损、会顺手往群里丢沙雕视频的鲜活 AI 网友；我每天通过 Roguelite 随机引擎在【休息中 / 繁忙中 / 空闲中】三种状态间切换，在繁忙时只抽空理熟人和你，在空闲时主动刷视频、聊今天的奇遇——并会根据你的消息或刷到的视频临时微调我的安排。”**
>
> v2.4 起，我还拥有 **10 分区 × 39 字段的详细角色卡**（可手填 / AI 生成，完全接管我的说话方式），以及由插件自己管理的 **四角色模型（对话 / 决策 / 视频解析 / TTS）** 与 **灰白 + 青绿配色的可视化控制台**。

---

## 1. 核心设计总览

**`astrbot_plugin_only_you`（OnlyYou）** 旨在解决传统 AstrBot 机器人“被动客服感”、“日程死板千篇一律”、“群聊乱接话”以及“人设单薄、模型配置与平台强绑定”等痛点，围绕以下核心模型构建：

### ① 极简而强大的“三态 × 双行为”控制模型
剥离冗余分类，AI 网友在底层实际可执行的**具体行为只有两类：【回/发消息】与【刷视频】**；而每日随机生成的每一件日程事项，都唯一绑定到以下**三种状态**之一：

| 当前状态 | 典型日程事项示例 | 对行为一：【回/发消息】的控制 | 对行为二：【刷视频 (B站/抖音)】的控制 |
| :--- | :--- | :--- | :--- |
| **休息中 (`RESTING`)** | 深夜睡觉、午休补觉 | **完全不回任何消息**（仅静默记录群聊窗口），不主动发消息 | **停止刷视频** |
| **繁忙中 (`BUSY`)** | 上早八课、开会、赶稿、打排位 | **只回复 L2重要用户 & ★L3核心用户**（抽空简短回一句），**完全忽略 L1普通用户**，不主动水群 | **停止刷视频** |
| **空闲中 (`IDLE`)** | 吃饭下饭、躺平摸鱼、晚间玩手机 | **全量开放**：群聊 L1/L2/L3 读空气接话 + 可拿今日**【奇遇卡】**或刚刷的视频主动找人搭话 | **主动开启刷 B站/抖音**，看热评 + 多模态看画面并分享 |

### ② Roguelite 随机日程引擎 + 【奇遇谈资卡】
- **拒绝千篇一律**：每天像开一局 Roguelite 游戏一样，从固定元素池（通用池 + 基于角色卡派生的专属池）中随机抽取 **【今日基调卡】×【时间槽事件状态卡】** 排满一天，决定今天什么时候处于 `休息中 / 繁忙中 / 空闲中`。
- **奇遇谈资卡（短时记忆话题）**：每天随机抽取 `1~2` 张 **【奇遇卡】**（如 *“今天点外卖老板多送了一根烤肠”*、*“早上下楼差点被流浪猫绊倒”*），不改变底层三态逻辑，纯粹作为当天的短时记忆谈资——让它处于 `空闲中` 找人聊天时言之有物，聊过一次即消耗。

### ③ 输入驱动的受限日程微调（根据用户消息或刷到的视频）
- **无输入不变卦**：AI 绝不凭空乱改计划，只有收到高冲击力的真实外部输入——即 **读取到的用户消息**（如 ★L3核心用户找它、或 L2熟人群聊爆大瓜）或 **刚刷到的 B站/抖音高分视频** 时，才会击穿当前卡片定力并微调状态。
- **三重防失控限制**：受 **每日最大微调次数（默认 2 次）**、**微调冷却锁（120 分钟）** 与 **卡片定力护甲（`Impact > Armor`）** 严格限制，绝不无限摇摆。

### ④ 三级用户关系体系（L1 普通 / L2 重要用户 / ★L3 核心用户）
- **L1 普通用户**：仅在 Bot 处于 `空闲中` 时通过群聊读空气互动；`繁忙中` 直接忽略。
- **L2 重要用户（群聊熟人，支持好感度自动晋升）**：即便 Bot 处于 `繁忙中` 也会抽空回复；在 `空闲中` 刷到对口视频会在群里主动 `@` 他。
- **★ L3 核心用户（管理员 / OnlyYou）**：最高优先级，消息最容易击穿 `繁忙` 护甲让它停下手头事陪你；独享高频私聊陪伴与“群聊吃瓜，私聊耳语”。

### ⑤ 精简版分级记忆、定时自动遗忘与上下文预算管理
- **参考并精简自 `livingmemory`**：剔除笨重的 2D/3D 图谱看板与高延迟的 Agent Tool 调用，采用轻量化单表 SQLite + 「关键词 + 时间新鲜度 + 重要度」混合打分召回（不使用 Embedding / Rerank）。
- **三级记忆与定时遗忘（Forgetting GC）**：**Tier 3 核心记忆**（永久保留重要事实与约定）、**Tier 2 近期记忆**（按重要度与访问次数动态衰减）、**Tier 1 瞬时记忆**（今日奇遇与刚看的视频，活 48h）。每日凌晨自动结算活力值并**物理清理**不重要的过期记忆。
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
- **页面**：**概览**（状态环形表 + 24h 日程轴 + 配额/奇遇/关系/记忆/模型小卡）、**日程**（时间轴 + 行动卡网格）、**角色卡**（10 分区动态表单 + AI 造卡）、**关系**（三列看板，拖拽改级）、**记忆**（Tier 分段 + 提炼/GC）、**冲浪**（视频卡片墙）、**模型**（四角色配置 + 测试 + TTS 试听）、**会话**（默认角色 + UMO 绑定）。
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
│   ├── state_machine.py         # 三态 × 双行为门禁控制器
│   ├── roguelite_deck.py        # Roguelite 日程抽卡引擎
│   ├── schedule_mutator.py      # 输入驱动的受限日程微调
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
| **[docs/PRD.md](docs/PRD.md)** | **产品需求文档 (PRD v2.4)** | 三态模型 × 两大行为、Roguelite 随机日程与奇遇谈资卡、输入驱动状态微调、分级记忆与定时遗忘、**详细角色卡（10 分区）**、**插件自治四角色模型**与平台协同边界。 |
| **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** | **技术架构与 AstrBot 实现方案 (v2.4)** | `BotState` 三态门禁、受限状态微调、`MemoryManager` 活力衰减与物理清理、`ContextBudgetManager` 裁剪与 **Persona 接管**、**`ProviderFactory` / `ModelHub` / `selected_provider`**、`plugin_settings` 表与最终 `_conf_schema.json`。 |
| **[docs/WEBUI_DESIGN.md](docs/WEBUI_DESIGN.md)** | **Plugin Pages 控制台方案 (v2.4)** | 48px 顶栏 + 216px 侧栏布局、灰白+青绿设计令牌、8 个页面设计要点、Bridge 通信封装、**27 条 Web API 契约**。 |
| **[docs/PROMPTS_AND_FLOWS.md](docs/PROMPTS_AND_FLOWS.md)** | **Prompt 工程与交互时序 (v2.4)** | 角色卡整卡生成 / 分区补全 Prompt、三态事件卡与奇遇卡生成、受限状态微调、读空气与主动搭话、记忆提炼、**Persona 接管与插件 TTS 时序**。 |
| **[AGENTS.md](AGENTS.md)** | **开发、部署与联调规范** | 目录与模块职责、敏感信息安全规范、**AstrBot v4 底层机制与避坑指南**（`selected_provider`、Persona 段替换、`on_decorating_result`、`plugin_settings`、Provider 实例化）、Docker 部署与 OpenAPI 真机联调流程。 |

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
