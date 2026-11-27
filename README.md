# OnlyYou (只有你) —— AstrBot 跨平台 AI 网友与深度拟人陪伴插件

> **“在各个群聊里，我是能和大家接梗互损、会顺手往群里丢沙雕视频的鲜活 AI 网友；我每天按自己的作息曲线（早睡早起 / 规律 / 夜猫子）排出一整条 24 小时动态时间轴——睡觉卡完全静默、繁忙卡带着 0%→100% 进度条闷头干活并在结束时随机结算一句今日成果、冲浪卡真的去刷 B站/抖音、空闲游荡卡则翻群吃瓜或发呆想起旧事；我还会根据你的消息或刷到的视频实时切分 / 延展时间块。”**
>
> **当前版本：v3.1.0（Minor）**：主模型作为基模，对话 / 决策 / 视觉默认继承；决策模型支持通用 JSON 与 Jev / Decisions 判断模型；模型页改为点击「应用」/「保存全部」生效。3.0 的 Pages「设置」配置统一入口与控制台 9 页保持不变。
>
> **v2.12 是「记忆与主动对话质量」版本**：长期记忆升级为 **RAG 三段式**——写入侧四类 `memory_type`（人物画像 / 关系共识 / 近期状态 / 短期情节）+ Embedding 语义去重 + `superseded_by` 版本替代 + 真实事件时间 `event_at`；读取侧「上下文感知 Query → 向量 + BM25 + 分层保底的 Top-24 粗排 → Rerank 精排 → 最低相关性门闸（不相关绝不硬塞）→ 计入时间标签的预算裁剪 → True-Hit 强化」；T2→T3 只允许 `profile` / `relationship` 且重要度 ≥ 8、跨天有效命中 ≥ 8 次。用户身份新增**曾用名 / 多昵称归一**（`canonical_name` + `aliases` + 平台账号自动归并记忆），**只有真实回复才结算好感**（180s 冷却 / 每日 +5 上限 / L1→L2 双门槛 AND）。主动对话改为**低频 + 概率 + 每日上限 + 70% 全新人设话题**，并用「最近 5 条主动消息」做反反复读约束，彻底告别「草…想起你那天聊骚」式循环复读。
>
> **v2.8 是「配置与运行态可信化」版本**：每个角色的完整配置（角色卡 / 行动卡池 / 模型（对话 / 主 / 决策 / 视觉）/ 机器人接口绑定 / 冲浪开关）落盘为 `<数据目录>/souls/<soul_id>.json` **独立 JSON（唯一真源，SQLite 仅作索引）**，配合 `revision` 单调版本号 + 内容哈希冲突裁决 + 原子写与自愈、写失败显式告警；一个角色可绑定多个**机器人接口（platform）**、同一接口严格互斥，聊天按「平台绑定 → 旧 UMO 兼容 → 默认角色 → 最近更新角色」路由，心跳按角色独立驱动；冲浪分享新增五道门禁（非兜底 + 未分享过 + 评分门槛 + 分享冷却 + 闲逛冷却，且须存在 ★L3 私聊目标）与按角色总开关；记忆新增 T1 去重 / 每角色上限 / 48h 实时清理、`MEMORY_MUSE` 三桶轮转与繁忙成果单次消费；`_shared_video_ids`、冷却时间戳、忙碌成果消费记录、★L3 私聊目标等运行态持久化到 `plugin_settings["runtime_state_v1"]`，**重启不再重复分享同一视频 / 重复聊同一条繁忙成果，冷却不会重置**。同时保留 **10 分区 × 36 字段的详细角色卡** 与 **灰白 + 青绿配色的 8 页可视化控制台（v2.8 新版关系页 / 会话页）**。

---

## 1. 核心设计总览

**`astrbot_plugin_only_you`（OnlyYou）** 旨在解决传统 AstrBot 机器人“被动客服感”、“日程死板千篇一律”、“群聊乱接话”以及“人设单薄、模型配置与平台强绑定”等痛点，围绕以下核心模型构建：

### ① 三态门禁 × 四类可执行行动卡（`SLEEP` / `SURF` / `IDLE_ROAM` / `BUSY_TASK`）
底层仍由 `RESTING` / `BUSY` / `IDLE` 三态控制**回消息门禁**，但日程表上的每一块时间都同时归属**四类可执行行动卡**之一——不再有“只有标题、没有实际行动”的静态事件卡：

| 行动卡 (`action_type`) | 绑定状态 | 贴合任意虚构角色的示例 | 回消息门禁 | 产生的真实后台行动 |
| :--- | :--- | :--- | :--- | :--- |
| **① 睡觉卡 (`SLEEP`)** | **💤 `RESTING`** | 现代网友*“戴眼罩呼呼大睡”* / 修仙者*“闭关调息入定”* / 机娘*“机能休眠充电”* | **完全静默**，仅 ★L3 私聊强呼叫可击穿护甲唤醒 | **什么都不做**，完全静默 |
| **② 冲浪卡 (`SURF`)** | **☕ `IDLE`** | *“瘫在懒人沙发上刷B站”* / *“用玉简偷看凡间短视频”* | **全量开放**（L1/L2/L3 读空气接话） | **真实刷 B站/抖音**：拉取推荐流与热评 → `vision` 模型看片打分 → 生成第一人称吐槽并分享给 ★L3 / 群聊（v2.8 加入分享门禁，见 ⑫） |
| **③ 空闲游荡卡 (`IDLE_ROAM`)** | **☕ `IDLE`** | *“泡杯热茶看窗外发呆”* / *“御剑在后山兜风”* | **全量开放**（L1/L2/L3 读空气接话） | **翻群吃瓜**（扫描近期群聊滑窗插话 / 私聊 L3 吐槽）或 **发呆记忆联想**（v2.8：优先单次消费今日繁忙成果，再由 T1/T2/T3 三桶抽样旧记忆，见 ⑬） |
| **④ 角色繁忙卡 (`BUSY_TASK`)** | **🔥 `BUSY`** | *“赶商业插画线稿”* / *“在丹房炼制回春丹”* / *“编写渗透脚本”* | **熟人特权过滤**：忽略 L1，仅抽空简短回复 ◆L2 / ★L3 | **极简三要素**：① 注入当前行为记忆 ② 随时间推进 `0%→100%` 进度条 ③ 走完或被中断时随机生成一句今日【完成结果】写入 **Tier 1 瞬时记忆** |

### ② 24h 动态弹性时间轴 + 角色专属行动卡池（取代旧固定 6 时段 + 奇遇卡）
- **角色专属卡池（重抽固定 9 张；导入种子卡池 8~12 张）**：导入 / 创建角色卡时，由主模型一次性生成贴合其职业、爱好与作息的四类行动卡；卡片名称固定在池中，可在控制台随时增删改。
- **每日随机排轴**：每天无需再调大模型造卡，直接按 **角色作息类型（`rhythm.chronotype`：早睡早起 / 规律 / 夜猫子）曲线 × 今日基调卡 × 相邻连携** 从卡池加权抽卡，并在每张卡的 `[min_hours, max_hours]` 内按 `0.5h` 步长随机掷出时长，让游标从 `0.0` 无缝推进到 `24.0`，自然形成 **7~11 个长短不一的弹性时间块**。
- **每天都不同的繁忙成果**：`BUSY_TASK` 每次走完 100%（或中途被打断）都会由主模型生成一句**当天专属、带随机小细节的完成结果**（如*“回春丹炼成了但最后一炉差点炸炉”*），直接写入 Tier 1 短时记忆，彻底替代旧版奇遇卡作为聊天谈资。
- **旧数据边界（v2.9）**：旧 6 时段 / 过夜 / 非连续日程会被自动重抽；缺少合法 `action_type` 的旧行动卡会被丢弃并由兜底卡补齐四类行动（不再按标题猜测类型）；`reroll` 只重抽卡池，今日时间轴保持不变、次日生效。
- **内置兜底卡最小化**：通用兜底只保留四类行动各 **1 张中性卡**（闭眼安静睡一会儿 / 专注处理手头正事 / 窝着刷会儿视频 / 闲下来发会儿呆），且只按自定义池**缺失的行动类型各补 1 张**，不会把整套默认卡并入卡池，避免稀释自定义卡的抽中概率。
- **未读消息补回**：睡觉 / 繁忙时被门禁拦下的消息会进入独立未读队列（读空气判“不回复”的不算）；醒来或忙完后，**私聊主动补回一句自然回复**，**群聊先读空气决策再决定是否插话**，消费即清空，可 `/onlyyou unread [list|flush|clear]` 调试。**未读队列是内存态，重启会清空（既定产品语义，见 ⑭）**。
- **读空气概率/预算门闸（v2.10）**：在「BUSY 特判 + @/唤醒/引用快速放行」之后、「防抖等待 + 读空气决策模型」之前，对**非 @ 普通群消息**先按 `reply_gate_probability`（默认 **80%**）抽样、再过 `reply_gate_hourly_limit`（同一群聊每小时判定上限，默认 **0 = 不限**）；未通过直接放弃，**不调用决策模型、不进入防抖、不算未读**。@ / 唤醒 / 引用、`BUSY` 特判与私聊完全不受影响；关闭门闸或概率调 100% 即与旧版一致（见 ⑰）。

### ③ 输入驱动的受限日程微调（动态切分 / 延展真实时间块）
- **无输入不变卦**：AI 绝不凭空乱改计划，只有收到高冲击力的真实外部输入——即 **读取到的用户消息**（如 ★L3核心用户找它、或 L2熟人群聊爆大瓜）或 **刚刷到的 B站/抖音高分视频** 时，才会击穿当前时间块护甲并实时改写时间轴：
  - **繁忙半途结算**：`BUSY_TASK` 被击穿时按当前进度一分为二——前半段立即结算一条“被打断”的 Tier 1 记忆，后半段切为 `IDLE_ROAM` 陪你摸鱼；
  - **睡觉被吵醒**：`SLEEP` 被 ★L3 私聊强呼叫击穿时，从当前时刻切出一小段 `IDLE_ROAM` 清醒窗，其余睡眠段保留；
  - **刷片上头加钟**：`SURF / IDLE_ROAM` 期间刷到高分视频或正热聊时，当前块延长 `0.5h` 并顺延 / 吞并下一块。
- **三重防失控限制**：受 **每日最大微调次数（默认 2 次）**、**微调冷却锁（120 分钟）** 与 **时间块定力护甲（`Impact > Armor`，四类行动固定 25 / 40 / 65 / 80）** 严格限制，且每次微调后自动重排块编号、保证 24h 时间轴连续无缝。

### ④ 三级用户关系体系（L1 普通 / L2 重要用户 / ★L3 核心用户）与群聊档案
- **L1 普通用户**：仅在 Bot 处于 `空闲中` 时通过群聊读空气互动；`繁忙中` 直接忽略。
- **L2 重要用户（群聊熟人，支持好感度自动晋升）**：即便 Bot 处于 `繁忙中` 也会抽空回复；在 `空闲中` 刷到对口视频会在群里主动 `@` 他。
- **★ L3 核心用户（管理员 / OnlyYou）**：最高优先级，消息最容易击穿 `繁忙` 护甲让它停下手头事陪你；独享高频私聊陪伴与“群聊吃瓜，私聊耳语”。**★L3 为管理员级：不参与好感度计算、不会被自动晋升产生**，可由 AstrBot `admins_id` 自动确定，也可在关系页详情弹窗中显式设置（用户等级与名单由 Pages 管理，插件普通配置不再维护）。
- **群聊档案（v2.8）**：`GroupProfile` 记录群名、备注、所属平台、绑定角色、最近活跃时间与滑窗 / 未读计数，存于 `plugin_settings["group_profiles"]`；关系页「💬 群聊」分区可查看并维护群备注、清空该群内存滑窗与未读、移除群聊记录。
- **长期记忆 RAG（v2.12）**：四类 `memory_type` + Embedding 语义去重 + `superseded_by` 版本替代 + 真实事件时间；读取侧向量 / BM25 / 分层保底粗排 → Rerank 精排 → 最低相关性门闸（默认 0.38 / 0.25）；T3 晋升只认 `profile`/`relationship` 跨天 8 次有效命中；模型页新增向量 / 重排角色，均可关闭并自动降级（详见 ⑲）。
- **身份归一与主动对话重设计（v2.12）**：曾用名 / 多昵称归一 + 孤儿记忆自动归并；只有真实回复才结算好感（180s 冷却、每日 +5、L1→L2 AND 门槛）；`IDLE_ROAM` 改为 2h 冷却 + 35% 概率 + 每日 3 条 + 70% 全新人设话题 + 最近 5 条反反复读（详见 ⑳）。
- **按会话主动回复开关（v2.11）**：用户 / 群聊详情弹窗可单独关闭「主动回复」——只停 Bot 主动发起的未读补回、翻群冒泡、发呆联想与视频分享，被动回复不受影响；按角色隔离、默认开启，列表行标记 `🤐 不主动`，详见 ⑱。

### ⑤ 精简版分级记忆、定时自动遗忘与上下文预算管理
- **参考并精简自 `livingmemory`**：剔除笨重的 2D/3D 图谱看板与高延迟的 Agent Tool 调用，采用轻量化单表 SQLite + 「关键词 + 时间新鲜度 + 重要度」混合打分召回（不使用 Embedding / Rerank）。
- **三级记忆与定时遗忘（Forgetting GC）**：**Tier 3 核心记忆**（永久保留重要事实与约定）、**Tier 2 近期记忆**（按重要度与访问次数动态衰减）、**Tier 1 瞬时记忆**（今日繁忙卡的随机完成结果、刚看的视频与翻群吃瓜见闻，活 48h）。v2.8 起 T1 增加「同角色内容去重 + 每角色上限（默认 30）+ 启动 / 心跳实时清理 + 跨重启每日 GC 只跑一次」；v2.12 起保留期由 `tier1_since` 锚定，从 T2/T3 **降级**为 T1 时重新计算 48h，过期的 T1 也会在列表读取前即时回收。
- **记忆时间锚点（v2.10）**：每条记忆的注入行都带「形成于 2026-11-05（星期四，10 天前）」时间锚点，并附 `MEMORY_TIME_CONTEXT_PROMPT` 教模型把正文里的「这周末 / 明天 / 昨天」按形成时间解冻、判断是否已过期；记忆提炼的滑窗也带「当前真实时间 + 窗口首尾时间范围 + 每条消息发送时间」，冷群跨天窗口不再无法定位。**记忆正文不被改写、无数据库结构变更**（见 ⑯）。
- **上下文预算管理器**：完全接管平台原生历史（`req.contexts.clear()` + 插件滑窗），并严格限制单次注入记忆的最大条数（默认 `<= 6 条`）与总字数预算（默认 `<= 400 字`），杜绝上下文窗口超载与 Token 费用失控。

### ⑥ 详细角色卡（10 分区 × 36 字段）：人设的单一数据源
- **结构**：基础身份、外貌形象、性格内核、背景故事、兴趣喜好、说话方式、社交关系（群中的角色定位）、生活作息、示例对话、补充设定（含原生人设原文）共 **10 个分区 / 36 个字段**（`core/character_card.py` 的 `CARD_SCHEMA`）。v2.6 已删除 `social` 分区里按等级的三条固定态度（改由用户档案 `attitude` 承载），因此不再是旧文档里的 39 字段。
- **完全接管人设 Prompt**：`ContextBudgetManager._takeover_persona` 用角色卡渲染结果替换 AstrBot 原生 `# Persona Instructions` 段（精确匹配 → 正则兜底 → 前置三分支），AstrBot 原生 Persona 只作为**一次性导入来源**。
- **Persona 导入两种模式**：可把原生 Persona 导入为**新角色**，也可**重新生成当前角色卡**——后者只覆盖角色卡本身，该角色的模型配置、平台绑定、冲浪设置、行动卡池与今日日程保持不变（未上传新头像时保留原头像）。
- **AI 造卡**：支持整卡生成与单分区补全（主模型），结果作为草稿预览、采用后才保存；旧的 4 字段扁平档案会自动迁移。
- **渲染预算（可调）**：`render_persona_prompt` 只渲染非空字段，优先保留身份 / 说话方式 / 性格 / 社交，超预算时按优先级丢弃低价值分区，硬保证不超预算；默认预算 **4000 字**，可在「设置 → 记忆与上下文 → 人设 Prompt 预算」调整（500~20000）。角色卡页会显示完整渲染字数与预算占用，超预算时弹出告警并在表单顶部常驻提示；图片头像（Data URL 等）**绝不注入** Persona / System Prompt。

### ⑦ 插件自治多角色模型（v2.8：按 Soul 独立配置；v2.12 扩为 6 角色）
- **角色拆分**：**主模型（`main`，基模 / 必填；承担 AI 造卡 / 记忆提炼 / 日程微调文案 / 繁忙成果 / 主动消息等后台生成）**、**对话（`dialogue`，只生成聊天回复，默认继承主模型）**、**决策（`decision`，只做读空气 / 是否接话判断，默认继承主模型）**、**视频解析（`vision`，默认继承主模型）**、**语音（`tts`）**；v2.12 另增 **向量（`embedding`）** 与 **重排（`rerank`）** 两个 RAG 角色（`off | provider | source` 三档，不参与继承，关闭即自动降级）。`dialogue` / `decision` / `vision` 均可改为独立配置；关闭继承且留空即停用该角色（不回退基模）；`tts` 不继承，`mode="off"` 即关闭。
- **决策双模式（`decision.mode`）**：`chat` = 普通聊天模型输出 JSON 判定；`judge` = OpenRouter Decisions API 的 Jev 等 System-One 判断模型，返回 `REPLY` / `IGNORE` 概率，按 `decision.threshold`（默认 0.5）裁决。判断模型与其他模型一样必须选择提供商源（Jev 需 OpenRouter 或兼容网关），源 / 模型留空即未配置。
- **按角色而不是按平台**：模型配置存放在**每个 Soul 自己的 JSON** 的 `model_config` 字段中；`ModelHub` 在 `main.py` 接线 `attach_soul_manager()` 后，读写 / 解析 / 测试 / TTS 接口全部支持可选 `soul_id`（缺省 = 默认角色，传 `umo` 时先按 UMO 路由出角色）。
- **不重复配置密钥**：`core/provider_factory.py` 从 AstrBot 提供商源 + 模型名构建实例；平台已有同源同模型且启用的实例则直接复用。
- **对话模型仍走原生管线**：门禁放行后执行 `event.set_extra("selected_provider", dialogue_provider_id)`，AstrBot `_select_provider()` 会解析该 extra（`provider_manager.inst_map`），分段回复、流式输出、工具调用全部保留；插件自建主模型注册为 **`onlyyou::main::<soul_id>`**（旧全局路径 `onlyyou::main` 保留兼容），不出现在平台模型列表，换模型 / 卸载时注销对应实例。
- **TTS 插件自治**：三种来源（关闭 / 提供商源自建 / AstrBot TTS）、触发概率、生效范围（私聊 / L3 / 全部）、语音 + 文字双发；`on_decorating_result` 钩子**先于原生分段回复与原生 TTS** 执行，不会重复播报。
- **不重造轮子、不与平台配置打架**：黑白名单 100% 遵循平台；被动消息通过 `event.should_call_llm(True/False)` 协同原生管线；后台主动消息复用平台 `segmented_reply` 参数；用户发图走平台原生多模态/识图管线。

### ⑧ 灰白 + 青绿的可视化控制台（AstrBot Plugin Pages，9 页）
- **布局**：48px 精简顶栏（品牌 / 当前角色 / 刷新 / 主题）+ 216px 侧栏（运行：概览、日程；角色：角色卡；关系；记忆；冲浪；系统：模型、会话）+ 主工作区。
- **页面（v2.8 新版）**：**概览**（状态环形表 + 24h 弹性日程轴 + 实时繁忙进度条 + 今日繁忙成果/配额/关系/记忆/模型小卡）、**日程**（24h 动态时间轴 + 四类行动卡网格与编辑器）、**角色卡**（10 分区动态表单 + AI 造卡 + 图片头像 + 人设 Prompt 预算告警；未保存修改离开/切角色前会弹窗确认）、**关系与群聊**（筛选 chips + 即时搜索 + ★L3→◆L2→L1 分组横向列表 + 居中宽幅详情弹窗；群聊分区卡片与详情弹窗）、**记忆**（T3/T2/T1 分层列表 + T1 48h 倒计时与上限提示 + 提炼/GC）、**冲浪**（视频卡片墙 + 顶部按角色的赛博冲浪总开关与后台视频源 B站/抖音选择）、**模型**（**按当前角色**配置主模型基模 / 对话 / 决策 / 视觉（含判断模型）+ 测试 + TTS 试听，点击「应用」或「保存全部」后生效）、**会话 / 机器人接口**（当前角色绑定列表 + 手动绑定 + 默认角色兜底开关 + 管辖会话内存概览）。页面级未保存修改守卫（`isDirty`）与深/浅色主题保留。
- **视觉**：灰白底 + 青绿主色（浅色 `#fafafa` / 深色 `#0f1115`，主色 `#0d9488` / `#2dd4bf`）；三态色 `IDLE #10b981` / `BUSY #f59e0b` / `RESTING #6366f1`；内联 SVG 图标替代 emoji，长说明改为悬浮提示；无构建 classic script，支持浅/深色。

### ⑨ Soul 独立 JSON：角色配置的唯一真源（v2.8 架构核心）
- **每个角色一份文件**：`<db 所在目录>/souls/<soul_id>.json`（`souls_dir` 由 `db.db_path` 推导，生产路径见 §2），UTF-8 + `ensure_ascii=False` + 2 空格缩进；`soul_id / card / state_event_cards / model_config / bound_platforms / settings / updated_at / revision` 一把梭。
- **SQLite 只做索引**：`souls` 表仍保存 `soul_id / persona_hash / profile_json / updated_at / revision` 供级联更新与旧库兼容；**文件才是真源**，索引写失败也会在下次读取时按文件重建。
- **冲突裁决与自愈**：`revision` 大者胜；同 `revision` 时比较「内容哈希」——相同以文件为准、不同仍以**文件**为准（尊重单真源与手工编辑）并重建索引；索引 revision 更高说明上次写文件失败 / 被回滚，以索引胜出并**回写文件自愈**（日志 warning）。损坏 JSON 降级回退索引、不会崩，下次成功写入自愈。
- **原子写与显式降级**：唯一临时文件（pid + uuid）+ `flush` + `os.fsync` + `os.replace`，失败自动重试一次，最终失败写 `ERROR` 日志并记录 `SoulManager.last_file_write_error`；保存仍会写 SQLite 索引（数据不丢），`souls/save` API 返回 `data.storage = {file_written, degraded, error}` 并在 `message` 追加中文告警。恢复目录权限后，下一次读取 / 保存会自动把 JSON 补回来。目录树与 JSON 样例见 §2。
- **手工改文件**：建议在插件停止 / 热重载后编辑 `souls/*.json`，改完在 Pages 刷新一次（`list_souls()` 会以文件为准重建索引）。

### ⑩ 机器人接口（platform）绑定与路由优先级
- **绑定模型**：一个 Soul 可绑定多个机器人接口（如 `aiocqhttp`、`telegram_bot_1`）；**同一接口同一时刻只属于一个 Soul**，冲突时 API 精确返回「该机器人接口已被角色『XX』绑定，请先在角色 XX 下解绑」。`platform_id` 会被校验为非空、不含 `:`、长度 ≤ 64。
- **路由优先级（v2.8 废除单会话 UMO 绑定主导）**：① UMO 第一段 == `platform_id` 的平台绑定 → ② 旧 `umo_soul_bindings` 精确绑定（兼容迁移）→ ③ `plugin_settings["default_soul_id"]` → ④ 最近更新的角色 → ⑤ 自动创建默认角色。
- **心跳按 Soul 驱动**：每 15 分钟对「有绑定的 Soul + 默认兜底 Soul」各自独立执行时间轴结算与行动分发；有绑定的 Soul 只处理自身平台范围内的未读 / 群聊，默认兜底 Soul 处理未被任何角色绑定的机器人接口（未设置默认角色时，聊天路由按优先级落到最近更新的角色；心跳层无任何绑定时退化为单角色处理全部会话）。解绑时同步清理该平台的内存滑窗 / 未读 / 冷却缓存（**不删持久化记忆**；「清理 vs 保留」完整语义见 [docs/UPGRADE.md](docs/UPGRADE.md) 第 9 节）。

### ⑪ 模型多角色按 Soul 独立配置
- 每个角色一套 `main / dialogue / decision / vision / tts`（另含 RAG 角色），存于 Soul JSON 的 `model_config`；对话模型的 `onlyyou::main::<soul_id>` 实例化与释放按角色进行。
- 旧版全局 `plugin_settings["model_config"]` 保留兼容：首次启动一次性迁移到「基模为空」的角色（flag `model_config_migrated_to_souls`，不删旧键）；`ModelHub` 未接线 SoulManager 时保持旧全局行为。旧角色布局（main=对话、decision=混合）由 `model_role_layout_migrated_v2` 一次性迁移为「主模型基模」布局。
- Pages「模型」页顶部展示当前角色，读取 / 保存均带 `?soul_id=` / `{soul_id, model_config}`，切换角色即重新加载该角色的模型状态；决策卡可切换「通用模型（JSON）/ 判断模型（Jev / Decisions API）」。

### ⑫ 赛博冲浪分享五道门禁 + 按角色总开关（v2.8）
一次 SURF 视频要**同时**满足以下五道门禁、且存在 ★L3 私聊目标，才会主动分享；否则只写入看片记忆、不打扰用户：
1. `VideoReviewResult` **不是视觉兜底**（视觉模型失败 / 无法解析时 `score=60` 且 `is_fallback=True`，永不分享）；
2. 该 `video_id` **未被本角色分享过**（`_shared_video_ids[soul_id]` 去重）；
3. 视觉评分 `>= surfing_config.surf_share_min_score`（默认 80）；
4. 距上次冲浪分享 `>= surfing_config.surf_share_cooldown_minutes`（默认 90 分钟）；
5. 距上次 `IDLE_ROAM` 主动搭话 `>= roguelite_schedule_config.idle_roam_cooldown_minutes`（默认 45 分钟）。
**分享成功才记录**视频 id 与时间戳（失败保留重试机会）。冲浪总开关与**后台视频源**（B站 / 抖音）完全由 Pages「冲浪」页按角色管理（`settings.surfing_enabled` / `settings.surfing_sources`，缺省开启且两者都启用；插件普通配置不再提供全局开关或平台开关）；关闭时心跳 SURF 时段自动降级为空闲闲逛、`/onlyyou surf` 提示先到 Pages 冲浪页开启。v2.8 还修复了「候选视频都看过时误回退第一个」的问题——候选耗尽直接跳过。

### ⑬ 记忆工程化：T1 去重 / 上限 / 清理 + MEMORY_MUSE 三层分桶
- **T1 去重**：同一角色 + 完全相同内容已存在时刷新其 `last_accessed_at` 并返回原 id，不新增；
- **T1 上限**：每角色默认最多 30 条（`memory_and_context_config.max_tier1_memories`），插入后超限按 `COALESCE(NULLIF(tier1_since,0), created_at) ASC, id ASC` 淘汰最旧；T2/T3 行为不变；
- **实时清理**：`cleanup_tier1()` 删除 `tier1_since + 48h` 已过的 T1 与超上限 T1，启动、每次心跳与记忆列表读取前调用；降级为 T1 时 `tier1_since` 重置，旧数据回退 `created_at`；每日 `run_forgetting_gc()` 由 `plugin_settings["last_forgetting_gc_date"]` 保证**跨重启每天只跑一次**；
- **MEMORY_MUSE 三层分桶**：`sample_muse_memories()` 对 T1/T2/T3 各自独立查询（最久未访问优先），过滤最近用过的 id 后按层级 / 重要度加权抽 ≤2 条；发送成功才更新 `last_accessed_at`（不加 `access_count`、不改活力 / 层级），并写入每角色 30 条的轮转环形表；
- **繁忙成果单次消费**：`IDLE_ROAM` 发呆联想优先消费 `run.settled_busy_results()` 中**本角色还没聊过**的繁忙成果（`_mused_busy_keys[soul_id]`），同一成果最多主动聊 1 次，发送成功才记录；用完才轮到三桶记忆抽样。
- Pages 记忆页 T1 行显示 `⏳ 剩余 Xh 清除` 与「上限 N 条」，T1 不再展示活力条。

### ⑭ 运行态持久化：重启不重复分享 / 不重复聊同一成果 / 冷却保留
- v2.8 新增 `core/runtime_state.py`（`RuntimeStateStore`），把下列运行态存进 `plugin_settings["runtime_state_v1"]`（`version: 1`）：
  `shared_video_ids`（每角色 ≤200，FIFO）、`last_surf_share_ts`、`mused_busy_keys`（每角色 ≤100）、`mused_memory_ids`（每角色 ≤30）、`last_idle_roam_ts`、`last_group_lurk_ts`、`last_private_catchup_ts`（冷却时间戳仅保留最近 7 天）、`known_l3_umos`（≤100，MRU）。
- `initialize()` 加载、状态变更点（分享成功 / 联想发送成功 / 冷却更新 / ★L3 私聊 / 解绑清空后）best-effort 落盘、`terminate()` 关闭 DB 前再保存；存储层异常只记 warning，绝不影响聊天主流程。
- **语义边界**：重启后未读 backlog 仍会清空（未读队列本身不持久化，属于既定产品语义）；但**主动消息去重、冷却、★L3 私聊目标不会丢**，因此不会出现「重启后重复分享同一视频 / 重复聊同一条繁忙成果」。

### ⑯ 设置页（v3.0，配置统一入口）

- **设置页**（`page-settings.js`）：6 大分组 Tab（运行节奏 / 主动搭话 / 读空气 / 记忆与 RAG / 关系成长 / 冲浪抓取），每组「常规配置」默认展开、「高级参数」默认折叠；支持搜索过滤、修改对比、恢复本组 / 全部默认。
- **热更新**：保存后写入 SQLite `plugin_settings["runtime_config"]` 并原地刷新共享配置字典，触发心跳唤醒与模块 `reload_config`，**不需要重载插件**；心跳 / GC 周期修改会立即按新节拍运行。
- **旧配置迁移**：首次启动自动把旧 AstrBotConfig 中改过的非默认值迁入新配置中心；`_conf_schema.json` 已删除，原生插件配置面板不再出现 OnlyYou 配置。

### ⑮ 控制台 v2.8 新版关系页与会话页
- **关系与群聊页**（`page-relations.js` 重写）：顶部筛选 chips（全部 / ★L3 / ◆L2 / L1 / 💬 群聊）+ 即时搜索 + 按 `★L3 → ◆L2 → L1` 分组的 `.relation-row` 横向列表；点行打开**居中宽幅弹窗**编辑显示名 / 专属称呼 / 等级 / 好感度 / 专属态度 / 平台账号 / 合并账号 / 删除；群聊分区卡片 + 群聊详情弹窗（群名、备注、清空该群内存、移除记录）。旧三列看板 / 拖拽 / 「⋯」菜单已移除。
- **会话 / 机器人接口页**（`page-sessions.js` 重写）：当前角色绑定列表（已绑定 → 解绑并清理该平台内存；被其他角色占用 → 禁绑提示；未绑定 → 绑定到当前角色）、手动输入 `platform_id` 绑定、默认角色兜底开关（`sessions/default_soul`）、当前角色管辖活跃会话内存概览（单会话 / 整平台清空）。旧「默认角色 + UMO 绑定表」UI 已移除（后端 UMO 绑定 API 仅为兼容保留）。
- **★L3 管理员级**：不展示 / 不可编辑好感度，不参与自动晋升；拖拽改级只在 L1 / L2 之间生效。
- **模型页**按当前角色读写配置；**记忆页** T1 倒计时 / 上限；**冲浪页**顶部「赛博冲浪总开关」+「后台视频源（B站/抖音）」（均乐观更新 + 失败回滚）；`mock.js` 同步提供 `sessions/platforms`、`groups`、`tier1_limit`、`toggle_surfing` 等 Mock 形状，`views/console/index.html?mock=1` 可离线预览。

### ⑯ 记忆时间标注：相对时间解冻 + 跨天滑窗定位（v2.10）
- **注入即标注**：记忆块逐条写成 `- [核心记忆 · 形成于 2026-11-05（星期四，10 天前）] 正文`，并在块前注入 `MEMORY_TIME_CONTEXT_PROMPT`，说明该时间是**记忆形成时间（记忆锚点）**而非当前时间：正文里的「这周末 / 明天 / 昨天」按锚点换算成绝对日期，已过去的视为历史 / 可能过期，未到的才作为未来安排。**记忆正文保持原样，不做日期改写**。
- **提炼窗口带时间**：`MEMORY_EXTRACT_AND_GRADE_PROMPT` 注入「当前真实时间（含星期）」，滑窗 transcript 带 `【窗口时间范围】首尾时间（跨度 N 天），共 N 条消息` 头部与逐条 `[YYYY-MM-DD HH:MM]` 发送时间——冷群跨多天的 15 条窗口也能定位每一天与「今天」。
- **可读时间贯穿**：`MemoryItem.to_dict()` 增 `created_at_text / last_accessed_at_text`，Pages 记忆页展示「形成于 …」绝对时间；`/onlyyou memory list` 与 `IDLE_ROAM(MEMORY_MUSE)` 素材同样带形成时间。
- **兼容边界**：无数据库结构变更、不改 GC / T1 去重语义、不改 `req.contexts` 聊天滑窗格式；新增 `core/time_utils.py` 统一本地时间格式化（固定中文星期、非法/未来时间戳安全降级）。

### ⑰ 读空气概率 / 预算门闸：抑制 AI 过于活跃（v2.10）
- **位置**：`AtmosphereReader.evaluate_should_reply` 中「BUSY 特判 + @/唤醒/引用快速放行」之后、「防抖等待 + 读空气决策模型」之前。
- **只作用于非 @ 普通群消息**：先按 `reply_gate_probability`（默认 **80%**）抽样，再按 `reply_gate_hourly_limit`（同一群聊滚动 1 小时判定上限，默认 **0 = 不限**）限流；未通过则直接放弃——**不调用决策模型、不进入防抖、不进入未读队列**，日志输出 `[OnlyYou] Reply gate dropped non-@ group message (...)`。
- **旁路完全不受影响**：私聊、@ / 唤醒 / 引用 Bot、`BUSY` 的 L2/L3 特判、`takeover_group_chat=false`；心跳「群未读回放决策」走独立冷却，不经过门闸。
- **兼容**：`reply_gate_enabled=false` 或概率 `100%` 时行为与旧版一致；概率 `0%` 时普通群消息完全不读空气（@ 仍会回复）。三项配置均在 Pages 控制台「设置 → 群聊读空气」中可见、可调、可关闭。

### ⑱ 按角色 / 会话的「主动回复」开关（v2.11）
- **位置**：控制台「关系与群聊」页——打开任一**用户**或**群聊**的详情弹窗，即可看到「允许「当前角色」主动回复 Ta / 允许在当前群主动回复」开关；列表行对关闭项显示 `🤐 不主动` 标记。
- **只控 Bot 主动发起的消息**：私聊未读补回、`IDLE_ROAM(GROUP_LURK)` 翻群冒泡、`IDLE_ROAM(MEMORY_MUSE)` 发呆联想私聊、`SURF` 视频分享。用户/群友主动发来的私聊、@ 与读空气接话等**被动回复完全不受影响**。
- **按角色隔离**：用户开关存在 `user_relations(soul_id, global_uid).proactive_enabled`（旧 `users` 表列作无 Soul 回退），群聊开关存在 `GroupProfile.proactive_enabled`（`plugin_settings["group_profiles"]`）；默认 **开启**，旧数据升级后行为不变。
- **失败保留重试**：关闭的私聊用户不会消耗未读（不 ACK、不调决策模型）；关闭的群聊会被 `GROUP_LURK` 候选列表直接跳过，不会挤占其他群；冲浪分享会自动跳过关闭的 ★L3 目标，改投下一位允许的 ★L3。

### ⑲ 记忆 RAG：四类分类 + 语义去重 / 版本替代 + 混合召回（v2.12）
- **写入侧**：每条记忆带 `memory_type`（`profile` / `relationship` / `state` / `episode`）与真实事件时间 `event_at`；`add_memory` 对 **T1/T2/T3 全层级**做语义去重（有向量：余弦 ≥ 0.85；无向量：字符 Bigram Jaccard ≥ 0.65），重复只合并关键词 / 刷新时间，不新增行；同 `state` 的旧状态或显式 `supersedes_id` 会被新记忆 `superseded_by` 归档，不再参与召回与主动联想。
- **读取侧**：Query = 当前消息 + 该用户最近 2 条发言；候选池仅取未归档记忆，按「向量余弦 + `content`/`keywords` BM25 + T3/T2/T1 分桶保底」粗排 Top-24；有 Rerank 角色时精排，综合分 `0.55×语义 + 0.25×新鲜度(event_at) + 0.20×重要度`；**低于 0.38 / 语义低于 0.25 直接丢弃**；预算计入时间标签，超长项 `continue` 让后面更短的相关记忆仍能注入；10 分钟内刚提炼且仍在滑窗内的记忆不重复注入。
- **强化与晋升**：只有本次语义 ≥ 0.55 且距上次强化 > 6h 才 `access_count + 1`；T3 晋升硬性要求 `memory_type IN (profile, relationship)`、`importance ≥ 8`、`access_count ≥ 8` 且记忆至少 1 天，日常情节与阶段状态留在 T2 自然代谢。
- **提炼**：单批最多 3 条；Prompt 注入【参与者身份归一表】与同角色已有 T2/T3 记忆（含 ID），要求高抽象度、去台词化、拒绝临时口头支票，并可返回 `supersedes_id`；直出 T3 仅限 `profile` / `relationship` 且 `importance ≥ 9`。提炼失败时该批消息进入单批重试缓冲，不再永久丢失。
- **模型**：模型页新增「向量模型 (Embedding) / 重排模型 (Rerank)」两张卡片，支持 `关闭 / 提供商源 / AstrBot 提供商` 三种模式与一键测试；两者皆关闭时自动降级为增强版词法检索（零报错）。

### ⑳ 身份归一与主动对话重设计（v2.12）
- **身份归一**：`users.aliases_json` 记录曾用名 / 多端昵称；`canonical_name` 优先角色专属称呼 → 主显示名 → `global_uid`。滑窗、记忆提炼与关系上下文统一使用规范称呼，并在 Prompt 注入「所有名称均指代同一人」说明；修改 `platform_ids` 或合并用户时，旧账号 / 曾用名下的孤儿记忆自动归并到主身份；启动时还会把历史 `memories.user_id` 里的裸平台 uid 一次性折入规范 `global_uid`（歧义行保持原样）。
- **好感曲线**：`on_any_message` 只解析身份（`resolve_user` 默认不加分）；只有 `on_llm_response_hook` 真正生成回复后才 `record_effective_interaction`——180s 冷却、普通群聊 +0.4 / 直接或私聊 +0.6、好感 ≥ 60 减速、每日 +5.0 封顶；L1→◆L2 必须同时满足互动次数与好感度（默认 40 / 30，均可配）。
- **主动对话节奏**：`IDLE_ROAM` 冷却默认 120 分钟、触发概率 35%、每日上限 3 条；话题模式 70% 走全新 `PERSONA_TOPIC`（6 个灵感切面：当下行动 / 兴趣癖好 / 居所氛围 / 主业吐槽 / 好奇提问 / 世界观日常），30% 走 `MEMORY_MUSE`（只抽 1 条、72h 内聊过跳过；抽不到就无缝切人设话题，**绝不再回退全量记忆列表硬抽**）；最近 5 条主动消息写入运行态并作为负面约束注入 Prompt，禁止重复话题与相同句首 / 句尾用字。

---

## 2. 数据与磁盘架构（v2.8）

### 2.1 数据目录（生产环境）

插件通过 `StarTools.get_data_dir("astrbot_plugin_only_you")` 获取数据目录，SQLite 为 `only_you.db`，Soul 文件放在其同级 `souls/`：

```text
/AstrBot/data/plugin_data/astrbot_plugin_only_you/   # AstrBot 数据目录（容器内路径）
├── only_you.db                                # SQLite：souls 索引 / 用户 / 记忆 / 日程 / plugin_settings
└── souls/                                     # v2.8：每个角色的独立 JSON（唯一真源）
    ├── default_soul.json
    ├── 酒狐.json
    └── 画师_小满__1a2b3c4d.json               # soul_id 含非法文件名字符时的安全名 + sha1 前 8 位
```

> 路径**不写死**：`SoulManager.souls_dir = os.path.join(os.path.dirname(db.db_path), "souls")`。生产 Docker 部署时宿主机对应 `/root/astrbot/data/plugin_data/astrbot_plugin_only_you/`。

### 2.2 `souls/*.json` 文件样例

```json
{
  "soul_id": "酒狐",
  "card": { "...": "10 分区 × 36 字段的完整角色卡（identity / appearance / ... / extra）" },
  "state_event_cards": [
    { "card_id": "evt_1001", "title": "赶插画线稿", "action_type": "BUSY_TASK", "state": "BUSY", "min_hours": 1.5, "max_hours": 3.0, "base_weight": 60, "armor": 65 }
  ],
  "model_config": {
    "main":     { "source_id": "openai", "model": "gpt-4.1" },
    "dialogue": { "inherit": true, "inherit_from": "main", "source_id": "", "model": "" },
    "decision": { "inherit": true, "inherit_from": "main", "source_id": "", "model": "", "mode": "chat", "threshold": 0.5, "endpoint": "" },
    "vision":   { "inherit": true, "inherit_from": "main", "source_id": "", "model": "" },
    "tts":      { "mode": "off", "source_id": "", "model": "", "voice": "", "provider_id": "", "probability": 1.0, "scope": "private", "dual_output": false }
  },
  "bound_platforms": ["aiocqhttp", "telegram_bot_1"],
  "settings": { "surfing_enabled": true },
  "updated_at": 1762840000,
  "revision": 7
}
```

- 文件名规则：非法字符 `\ / : * ? " < > |` 与控制字符替换为 `_`；发生替换时追加 `__<sha1 前 8 位>` 防撞。正常 `soul_id`（如 `default_soul` / `酒狐`）直接即 `<soul_id>.json`。
- `card` 由 `normalize_card()` 归一化后写入；`model_config` 为多角色归一化结构（含 Embedding / Rerank）；`settings` 至少含 `surfing_enabled`，未知键原样保留。

### 2.3 一致性规则（文件真源 + 索引自愈）

| 场景 | 裁决 / 行为 |
| :--- | :--- |
| 仅 JSON 存在 | 以文件为准，重建 SQLite 索引 |
| 仅 SQLite 行存在（旧库） | 以索引为准，并**自动落盘** `souls/*.json` |
| 两边 revision 不同 | revision 大者胜；索引胜出时**回写文件自愈**（上次写文件失败/被回滚），日志 warning |
| revision 相同、内容哈希不同 | **文件胜**（尊重手工编辑），重建索引并 warning |
| revision 相同、内容哈希相同 | 文件为准（无动作） |
| JSON 损坏 / 不可读 | warning 后回退索引，不崩溃，下次成功写入自愈 |
| `souls/` 目录不可写 | 降级 SQLite-only：数据不丢，`last_file_write_error` + `souls/save` 响应显式告警；恢复权限后读取/保存自动回填 |
| 保存时传入副本 revision 低于磁盘 | 说明文件被手工改过 / 被其他进程写过 → warning「收到过期副本，本次保存将以更新 revision 覆盖」，不阻塞保存 |
| 时钟回拨 | `revision = max(文件, 索引, 缓存) + 1`；`updated_at = max(now, 已有+1)`，版本与时间戳均不倒退 |

- **原子写**：唯一临时文件名（pid + uuid）→ 写入 + `flush` + `os.fsync` → `os.replace` → best-effort fsync 目录；失败重试 1 次并清理临时文件，最终失败记 `ERROR`。
- **SQLite `souls` 索引表**：`soul_id / persona_hash / profile_json / updated_at / revision`；旧库缺 `revision` 列时 `db.initialize()` 用 `PRAGMA table_info` + `ALTER TABLE` 幂等补列。

### 2.4 `plugin_settings` 中的关键键（KV 表，JSON 序列化）

| 键 | 内容 |
| :--- | :--- |
| `default_soul_id` | 默认角色（无平台绑定会话的兜底路由） |
| `model_config` | **旧全局模型配置**，仅作一次性迁移来源保留（不删）；旧 `main` → `dialogue`、旧 `decision` → `main`/`decision` |
| `model_config_migrated_to_souls` | 一次性迁移 flag：旧全局配置 → 主模型为空的 Soul（**全部成功才写入**） |
| `legacy_umo_bindings_migrated` | 一次性迁移 flag：旧 UMO 绑定 → 平台绑定（**全部成功才写入**，失败下次启动重试） |
| `group_profiles` | 群聊档案列表（群名 / 备注 / 平台 / 绑定角色 / 最近活跃） |
| `runtime_state_v1` | v2.8 运行态快照（分享去重 / 各类冷却 / 忙碌成果消费 / ★L3 目标） |
| `last_forgetting_gc_date` | 最近一次每日遗忘 GC 的日期，保证跨重启每天只跑一次 |

---

## 3. 项目结构

```text
OnlyYou/
├── metadata.yaml                # 插件元数据（version: 3.1.0）
├── requirements.txt             # Python 依赖
├── main.py                      # 插件入口：三态门禁 / selected_provider / LLM 钩子 / on_decorating_result TTS / 后台任务 / 指令
├── core/                        # 核心逻辑
│   ├── config_manager.py        # 运行配置中心（6 大分组 / 常规+高级 / 热更新 / 旧配置迁移）
│   ├── state_machine.py         # 三态门禁控制器 + 四类行动卡枚举 (ActionType，含固定护甲)
│   ├── roguelite_deck.py        # 24h 动态积木时间轴引擎（专属卡池 / 作息曲线 / 弹性块拼接）
│   ├── schedule_mutator.py      # 输入驱动的受限微调（繁忙半途结算 / 睡眠唤醒 / 加钟延展）
│   ├── character_card.py        # 详细角色卡（CARD_SCHEMA 10 分区 / 36 字段 + Prompt 渲染）
│   ├── soul_manager.py          # Soul 独立 JSON 真源 / 索引自愈 / 平台绑定路由 / AI 造卡 / Persona 导入
│   ├── runtime_state.py         # v2.8 运行态持久化（去重 / 冷却 / ★L3 目标 → plugin_settings）
│   ├── user_relation.py         # 三级用户关系 + 单用户态度 + 曾用名/规范称呼 + 好感曲线 + 群聊档案
│   ├── memory_manager.py        # RAG 分级记忆：四类 memory_type + 语义去重 / Supersede + 混合召回 + 强化门闸 + Forgetting GC
│   ├── context_budget.py        # 上下文滑窗 + 混合召回 / Rerank / 相关性门闸 + 身份归一注入 + Persona 接管
│   ├── provider_factory.py      # AstrBot Provider 封装（复用/自建、TTS、Embedding、Rerank）
│   ├── model_hub.py             # 插件自治多角色模型网关（四对话角色 + Embedding/Rerank，按 Soul 读写）
│   ├── platform_bridge.py       # 平台协同桥（唤醒 / 分段 / 主动语音消息 / 机器人接口探测）
│   ├── web_api.py               # 34 条 Plugin Pages Web API
│   ├── time_utils.py            # v2.10 共享本地时间格式化（星期 / 形成时间锚点 / 窗口跨度）
│   └── atmosphere_reader.py     # 群聊读空气 / 解线团 / v2.10 概率与每小时预算门闸
├── views/console/               # 控制台前端（无构建 classic script）
│   ├── index.html / style.css   # 外壳 + 设计令牌（浅色 / 深色）
│   ├── app.js / icons.js / charts.js / mock.js
│   └── page-*.js                # 概览 / 日程 / 角色卡 / 关系与群聊 / 记忆 / 冲浪 / 模型 / 机器人接口 / 设置 共 9 页
├── surfing/                     # B站 / 抖音冲浪管线与适配器（含 is_fallback 兜底标记）
├── storage/db.py                # aiosqlite 持久化（含 plugin_settings KV 表；v2.8 souls.revision 迁移）
├── prompts/templates.py         # 集中管理 Prompt 模板
├── tests/                       # 离线 unittest 测试套件（零网络）
└── docs/                        # 设计文档（ARCHITECTURE 架构设计 / UPGRADE 升级迁移）
```

> 生产数据目录（不入 Git）：`data/plugin_data/astrbot_plugin_only_you/{only_you.db, souls/*.json}`，详见 §2。

---

## 4. 文档套件导航

| 文档路径 | 文档名称 | 核心内容概述 |
| :--- | :--- | :--- |
| **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** | **技术架构设计 (v3.1)** | 三态门禁 × 四类行动卡、24h 动态时间轴与作息曲线、输入驱动的受限微调、**RAG 分级记忆（四类 memory_type / 语义去重 / Supersede / 混合召回 / 最低相关性门闸）**、身份归一（曾用名 / 规范称呼）、好感曲线、主动对话节奏（概率 / 每日上限 / 人设话题 / 反反复读）、上下文预算、**Persona 接管**、Soul JSON 唯一真源与 revision 裁决、多角色模型（含 Embedding / Rerank）与 `selected_provider`、读空气概率/预算门闸、冲浪分享门禁、运行态持久化、配置分层、34 条 Web API 与控制台结构。 |
| **[docs/UPGRADE.md](docs/UPGRADE.md)** | **生产升级与迁移指南 (v3.1)** | 自动迁移清单与幂等 flag → 备份 → 部署 / 热重载 → 日志与数据校验 → Pages / 功能抽检 → 回滚；含**解绑 / 清空类操作的「清理 vs 保留」语义表**、重启语义、v2.8.2~v2.11 版本迁移条目与 FAQ。 |
| **AGENTS.md（本地工程手册，GitHub 不可见）** | **开发、部署与联调规范** | 目录与模块职责、敏感信息安全规范、**AstrBot v4 底层机制与避坑指南**（`selected_provider`、Persona 段替换、`on_decorating_result`、`plugin_settings`、Provider 实例化）、**24h 时间轴不变量与繁忙结算契约**、Docker 部署、SSH 隧道 + OpenAPI 真机联调流程。⚠️ **该文件已在 `.gitignore` 中忽略，仅本地工程维护使用，GitHub 仓库不可见（因此本表不提供 Markdown 链接）**；GitHub 可见文档为 `README.md` + `docs/ARCHITECTURE.md` / `docs/UPGRADE.md`。 |

---

## 5. 快速开始与升级

1. **安装**：将插件目录放入 AstrBot 的 `data/plugins/astrbot_plugin_only_you`，安装 `requirements.txt` 依赖，重启 / 热重载插件（要求 AstrBot `>= v4.5.7`）。
2. **配置**：
   - 在 Pages「设置」页统一调整 Roguelite / 主动搭话 / 读空气 / 记忆与 RAG / 关系成长 / 冲浪抓取（保存后热更新即时生效；角色总开关、后台视频源、用户等级名单仍由各业务页 / Soul JSON 管理）；
   - 打开插件 Pages 控制台，在「模型」页为**当前角色**配置 **主模型（基模 · 必填）/ 对话 / 决策 / 视频解析 / TTS**（配置写入该角色 `souls/<soul_id>.json` 的 `model_config`）；
   - 在「角色卡」页手填或 AI 生成角色卡；在「会话 / 机器人接口」页把机器人接口绑定到角色并设置默认角色兜底。
3. **从旧版本升级到 v3.1**：**先备份 `only_you.db`（以及整个插件数据目录），再阅读 [docs/UPGRADE.md](docs/UPGRADE.md) 执行升级与迁移校验**。首次启动会自动把旧 SQLite 角色数据落盘为 `souls/*.json`、迁移旧 UMO 绑定与全局模型配置，并把 `users` 关系列按角色拆分到 `user_relations`；v2.11 另会幂等补充 `proactive_enabled` 列（一次性 flag 全部成功才写入，失败下次启动自动重试）。
4. **本地离线测试（零网络、无需安装 AstrBot / pytest）**：

   ```bash
   python3 -m unittest discover -s tests -v
   python3 -m compileall -q core storage surfing prompts main.py
   node --check views/console/*.js
   ```

5. **离线预览控制台**：直接打开 `views/console/index.html?mock=1`（`mock.js` 提供全套 Mock 数据，可截图验收浅色 / 深色主题）。
6. **真机联调凭据**：远程服务器 SSH、AstrBot OpenAPI Key、Dashboard 登录账号等统一放在本地 `.env`（已 gitignore），变量清单与联调命令见 `AGENTS.md` 第二、四节（仅本地可见）。

---

## 6. 界面预览（本地 Mock 截图）

`docs/screenshots/` 收录了 v2.8 控制台 8 个页面在无后端 Mock 模式下的浅色 / 深色全量截图（共 16 张，Chrome headless + `?mock=1` 生成，1440×1000）。其中「关系」为 v2.8 重写的「关系与群聊」页、「会话」为 v2.8 重写的「机器人接口」页，均与当前 `views/console/index.html?mock=1` 运行结果一致。

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
