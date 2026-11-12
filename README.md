# OnlyYou (只有你) —— AstrBot 跨平台 AI 网友与深度拟人陪伴插件

> **“在各个群聊里，我是能和大家接梗互损、会顺手往群里丢沙雕视频的鲜活 AI 网友；我每天按自己的作息曲线（早睡早起 / 规律 / 夜猫子）排出一整条 24 小时动态时间轴——睡觉卡完全静默、繁忙卡带着 0%→100% 进度条闷头干活并在结束时随机结算一句今日成果、冲浪卡真的去刷 B站/抖音、空闲游荡卡则翻群吃瓜或发呆想起旧事；我还会根据你的消息或刷到的视频实时切分 / 延展时间块。”**
>
> **v2.8 是「配置与运行态可信化」版本**：每个角色的完整配置（角色卡 / 行动卡池 / 四角色模型 / 机器人接口绑定 / 冲浪开关）落盘为 `<数据目录>/souls/<soul_id>.json` **独立 JSON（唯一真源，SQLite 仅作索引）**，配合 `revision` 单调版本号 + 内容哈希冲突裁决 + 原子写与自愈、写失败显式告警；一个角色可绑定多个**机器人接口（platform）**、同一接口严格互斥，聊天按「平台绑定 → 旧 UMO 兼容 → 默认角色 → 最近更新角色」路由，心跳按角色独立驱动；冲浪分享新增五道门禁（非兜底 + 未分享过 + 评分门槛 + 分享冷却 + 闲逛冷却，且须存在 ★L3 私聊目标）与按角色总开关；记忆新增 T1 去重 / 每角色上限 / 48h 实时清理、`MEMORY_MUSE` 三桶轮转与繁忙成果单次消费；`_shared_video_ids`、冷却时间戳、忙碌成果消费记录、★L3 私聊目标等运行态持久化到 `plugin_settings["runtime_state_v1"]`，**重启不再重复分享同一视频 / 重复聊同一条繁忙成果，冷却不会重置**。同时保留 **10 分区 × 36 字段的详细角色卡** 与 **灰白 + 青绿配色的 8 页可视化控制台（v2.8 新版关系页 / 会话页）**。

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
- **角色专属卡池（8~12 张）**：导入 / 创建角色卡时，由决策模型一次性生成贴合其职业、爱好与作息的四类行动卡；卡片名称固定在池中，可在控制台随时增删改。
- **每日随机排轴**：每天无需再调大模型造卡，直接按 **角色作息类型（`rhythm.chronotype`：早睡早起 / 规律 / 夜猫子）曲线 × 今日基调卡 × 相邻连携** 从卡池加权抽卡，并在每张卡的 `[min_hours, max_hours]` 内按 `0.5h` 步长随机掷出时长，让游标从 `0.0` 无缝推进到 `24.0`，自然形成 **7~11 个长短不一的弹性时间块**。
- **每天都不同的繁忙成果**：`BUSY_TASK` 每次走完 100%（或中途被打断）都会由决策模型生成一句**当天专属、带随机小细节的完成结果**（如*“回春丹炼成了但最后一炉差点炸炉”*），直接写入 Tier 1 短时记忆，彻底替代旧版奇遇卡作为聊天谈资。
- **旧数据平滑迁移**：旧 6 时段 / 过夜日程会被自动重抽；旧事件卡按 `state + title` 自动推导 `action_type` 与合理时长区间，无需手工改库。
- **未读消息补回**：睡觉 / 繁忙时被门禁拦下的消息会进入独立未读队列（读空气判“不回复”的不算）；醒来或忙完后，**私聊主动补回一句自然回复**，**群聊先读空气决策再决定是否插话**，消费即清空，可 `/onlyyou unread [list|flush|clear]` 调试。**未读队列是内存态，重启会清空（既定产品语义，见 ⑭）**。

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

### ⑤ 精简版分级记忆、定时自动遗忘与上下文预算管理
- **参考并精简自 `livingmemory`**：剔除笨重的 2D/3D 图谱看板与高延迟的 Agent Tool 调用，采用轻量化单表 SQLite + 「关键词 + 时间新鲜度 + 重要度」混合打分召回（不使用 Embedding / Rerank）。
- **三级记忆与定时遗忘（Forgetting GC）**：**Tier 3 核心记忆**（永久保留重要事实与约定）、**Tier 2 近期记忆**（按重要度与访问次数动态衰减）、**Tier 1 瞬时记忆**（今日繁忙卡的随机完成结果、刚看的视频与翻群吃瓜见闻，活 48h）。v2.8 起 T1 增加「同角色内容去重 + 每角色上限（默认 30）+ 启动 / 心跳实时清理 + 跨重启每日 GC 只跑一次」。
- **上下文预算管理器**：完全接管平台原生历史（`req.contexts.clear()` + 插件滑窗），并严格限制单次注入记忆的最大条数（默认 `<= 6 条`）与总字数预算（默认 `<= 400 字`），杜绝上下文窗口超载与 Token 费用失控。

### ⑥ 详细角色卡（10 分区 × 36 字段）：人设的单一数据源
- **结构**：基础身份、外貌形象、性格内核、背景故事、兴趣喜好、说话方式、社交关系（群中的角色定位）、生活作息、示例对话、补充设定（含原生人设原文）共 **10 个分区 / 36 个字段**（`core/character_card.py` 的 `CARD_SCHEMA`）。v2.6 已删除 `social` 分区里按等级的三条固定态度（改由用户档案 `attitude` 承载），因此不再是旧文档里的 39 字段。
- **完全接管人设 Prompt**：`ContextBudgetManager._takeover_persona` 用角色卡渲染结果替换 AstrBot 原生 `# Persona Instructions` 段（精确匹配 → 正则兜底 → 前置三分支），AstrBot 原生 Persona 只作为**一次性导入来源**。
- **AI 造卡**：支持整卡生成与单分区补全（主模型），结果作为草稿预览、采用后才保存；旧的 4 字段扁平档案会自动迁移。
- **渲染预算**：`render_persona_prompt(card, max_chars=2000)` 只渲染非空字段，优先保留身份 / 说话方式 / 性格 / 社交，超预算时按优先级丢弃低价值分区，硬保证不超预算；图片头像（Data URL 等）**绝不注入** Persona / System Prompt。

### ⑦ 插件自治四角色模型（v2.8：按 Soul 独立配置）
- **四角色**：**对话（`main`，必填）**、**决策（`decision`，读空气 / 记忆提炼 / 日程微调 / 造卡）**、**视频解析（`vision`）**、**语音（`tts`）**；`decision` / `vision` 支持「独立配置」或显式继承（`main` / `decision`），关闭继承且留空即停用该角色（不回退 `main`）；`tts` 不继承，`mode="off"` 即关闭。
- **按角色而不是按平台**：v2.8 起四角色配置存放在**每个 Soul 自己的 JSON** 的 `model_config` 字段中；`ModelHub` 在 `main.py` 接线 `attach_soul_manager()` 后，读写 / 解析 / 测试 / TTS 接口全部支持可选 `soul_id`（缺省 = 默认角色，传 `umo` 时先按 UMO 路由出角色）。
- **不重复配置密钥**：`core/provider_factory.py` 从 AstrBot 提供商源 + 模型名构建实例；平台已有同源同模型且启用的实例则直接复用。
- **主模型仍走原生管线**：门禁放行后执行 `event.set_extra("selected_provider", main_provider_id)`，AstrBot `_select_provider()` 会解析该 extra（`provider_manager.inst_map`），分段回复、流式输出、工具调用全部保留；插件自建主模型注册为 **`onlyyou::main::<soul_id>`**（旧全局路径 `onlyyou::main` 保留兼容），不出现在平台模型列表，换模型 / 卸载时注销对应实例。
- **TTS 插件自治**：三种来源（关闭 / 提供商源自建 / AstrBot TTS）、触发概率、生效范围（私聊 / L3 / 全部）、语音 + 文字双发；`on_decorating_result` 钩子**先于原生分段回复与原生 TTS** 执行，不会重复播报。
- **不重造轮子、不与平台配置打架**：黑白名单 100% 遵循平台；被动消息通过 `event.should_call_llm(True/False)` 协同原生管线；后台主动消息复用平台 `segmented_reply` 参数；用户发图走平台原生多模态/识图管线。

### ⑧ 灰白 + 青绿的可视化控制台（AstrBot Plugin Pages，8 页）
- **布局**：48px 精简顶栏（品牌 / 当前角色 / 刷新 / 主题）+ 216px 侧栏（运行：概览、日程；角色：角色卡；关系；记忆；冲浪；系统：模型、会话）+ 主工作区。
- **页面（v2.8 新版）**：**概览**（状态环形表 + 24h 弹性日程轴 + 实时繁忙进度条 + 今日繁忙成果/配额/关系/记忆/模型小卡）、**日程**（24h 动态时间轴 + 四类行动卡网格与编辑器）、**角色卡**（10 分区动态表单 + AI 造卡 + 图片头像；未保存修改离开/切角色前会弹窗确认）、**关系与群聊**（筛选 chips + 即时搜索 + ★L3→◆L2→L1 分组横向列表 + 居中宽幅详情弹窗；群聊分区卡片与详情弹窗）、**记忆**（T3/T2/T1 分层列表 + T1 48h 倒计时与上限提示 + 提炼/GC）、**冲浪**（视频卡片墙 + 顶部按角色的赛博冲浪总开关）、**模型**（**按当前角色**配置四角色 + 测试 + TTS 试听，修改 0.7s 防抖自动保存）、**会话 / 机器人接口**（当前角色绑定列表 + 手动绑定 + 默认角色兜底开关 + 管辖会话内存概览）。页面级未保存修改守卫（`isDirty`）与深/浅色主题保留。
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
- **心跳按 Soul 驱动**：每 15 分钟对「有绑定的 Soul + 默认兜底 Soul」各自独立执行时间轴结算与行动分发；有绑定的 Soul 只处理自身平台范围内的未读 / 群聊，默认兜底 Soul 处理未被任何角色绑定的机器人接口（未设置默认角色时，聊天路由按优先级落到最近更新的角色；心跳层无任何绑定时退化为单角色处理全部会话）。解绑时同步清理该平台的内存滑窗 / 未读 / 冷却缓存（**不删持久化记忆**；「清理 vs 保留」完整语义见 [docs/UPGRADE.md](docs/UPGRADE.md) 第 10 节）。

### ⑪ 模型四角色按 Soul 独立配置
- 每个角色一套 `main / decision / vision / tts`，存于 Soul JSON 的 `model_config`；`onlyyou::main::<soul_id>` 实例化与释放按角色进行。
- 旧版全局 `plugin_settings["model_config"]` 保留兼容：首次启动一次性迁移到「主模型为空」的角色（flag `model_config_migrated_to_souls`，不删旧键）；`ModelHub` 未接线 SoulManager 时保持旧全局行为。
- Pages「模型」页顶部展示当前角色，读取 / 保存均带 `?soul_id=` / `{soul_id, model_config}`，切换角色即重新加载该角色的模型状态。

### ⑫ 赛博冲浪分享五道门禁 + 按角色总开关（v2.8）
一次 SURF 视频要**同时**满足以下五道门禁、且存在 ★L3 私聊目标，才会主动分享；否则只写入看片记忆、不打扰用户：
1. `VideoReviewResult` **不是视觉兜底**（视觉模型失败 / 无法解析时 `score=60` 且 `is_fallback=True`，永不分享）；
2. 该 `video_id` **未被本角色分享过**（`_shared_video_ids[soul_id]` 去重）；
3. 视觉评分 `>= surfing_config.surf_share_min_score`（默认 80）；
4. 距上次冲浪分享 `>= surfing_config.surf_share_cooldown_minutes`（默认 90 分钟）；
5. 距上次 `IDLE_ROAM` 主动搭话 `>= roguelite_schedule_config.idle_roam_cooldown_minutes`（默认 45 分钟）。
**分享成功才记录**视频 id 与时间戳（失败保留重试机会）。冲浪总开关完全由 Pages「冲浪」页按角色管理（`settings.surfing_enabled`，缺省开启；插件普通配置不再提供全局开关）；关闭时心跳 SURF 时段自动降级为空闲闲逛、`/onlyyou surf` 提示先到 Pages 冲浪页开启。v2.8 还修复了「候选视频都看过时误回退第一个」的问题——候选耗尽直接跳过。

### ⑬ 记忆工程化：T1 去重 / 上限 / 清理 + MEMORY_MUSE 三层分桶
- **T1 去重**：同一角色 + 完全相同内容已存在时刷新其 `last_accessed_at` 并返回原 id，不新增；
- **T1 上限**：每角色默认最多 30 条（`memory_and_context_config.max_tier1_memories`），插入后超限按 `created_at ASC, id ASC` 淘汰最旧；T2/T3 行为不变；
- **实时清理**：`cleanup_tier1()` 删除 >48h 的 T1 与超上限 T1，启动与每次心跳调用；每日 `run_forgetting_gc()` 由 `plugin_settings["last_forgetting_gc_date"]` 保证**跨重启每天只跑一次**；
- **MEMORY_MUSE 三层分桶**：`sample_muse_memories()` 对 T1/T2/T3 各自独立查询（最久未访问优先），过滤最近用过的 id 后按层级 / 重要度加权抽 ≤2 条；发送成功才更新 `last_accessed_at`（不加 `access_count`、不改活力 / 层级），并写入每角色 30 条的轮转环形表；
- **繁忙成果单次消费**：`IDLE_ROAM` 发呆联想优先消费 `run.settled_busy_results()` 中**本角色还没聊过**的繁忙成果（`_mused_busy_keys[soul_id]`），同一成果最多主动聊 1 次，发送成功才记录；用完才轮到三桶记忆抽样。
- Pages 记忆页 T1 行显示 `⏳ 剩余 Xh 清除` 与「上限 N 条」，T1 不再展示活力条。

### ⑭ 运行态持久化：重启不重复分享 / 不重复聊同一成果 / 冷却保留
- v2.8 新增 `core/runtime_state.py`（`RuntimeStateStore`），把下列运行态存进 `plugin_settings["runtime_state_v1"]`（`version: 1`）：
  `shared_video_ids`（每角色 ≤200，FIFO）、`last_surf_share_ts`、`mused_busy_keys`（每角色 ≤100）、`mused_memory_ids`（每角色 ≤30）、`last_idle_roam_ts`、`last_group_lurk_ts`、`last_private_catchup_ts`（冷却时间戳仅保留最近 7 天）、`known_l3_umos`（≤100，MRU）。
- `initialize()` 加载、状态变更点（分享成功 / 联想发送成功 / 冷却更新 / ★L3 私聊 / 解绑清空后）best-effort 落盘、`terminate()` 关闭 DB 前再保存；存储层异常只记 warning，绝不影响聊天主流程。
- **语义边界**：重启后未读 backlog 仍会清空（未读队列本身不持久化，属于既定产品语义）；但**主动消息去重、冷却、★L3 私聊目标不会丢**，因此不会出现「重启后重复分享同一视频 / 重复聊同一条繁忙成果」。

### ⑮ 控制台 v2.8 新版关系页与会话页
- **关系与群聊页**（`page-relations.js` 重写）：顶部筛选 chips（全部 / ★L3 / ◆L2 / L1 / 💬 群聊）+ 即时搜索 + 按 `★L3 → ◆L2 → L1` 分组的 `.relation-row` 横向列表；点行打开**居中宽幅弹窗**编辑显示名 / 专属称呼 / 等级 / 好感度 / 专属态度 / 平台账号 / 合并账号 / 删除；群聊分区卡片 + 群聊详情弹窗（群名、备注、清空该群内存、移除记录）。旧三列看板 / 拖拽 / 「⋯」菜单已移除。
- **会话 / 机器人接口页**（`page-sessions.js` 重写）：当前角色绑定列表（已绑定 → 解绑并清理该平台内存；被其他角色占用 → 禁绑提示；未绑定 → 绑定到当前角色）、手动输入 `platform_id` 绑定、默认角色兜底开关（`sessions/default_soul`）、当前角色管辖活跃会话内存概览（单会话 / 整平台清空）。旧「默认角色 + UMO 绑定表」UI 已移除（后端 UMO 绑定 API 仅为兼容保留）。
- **★L3 管理员级**：不展示 / 不可编辑好感度，不参与自动晋升；拖拽改级只在 L1 / L2 之间生效。
- **模型页**按当前角色读写配置；**记忆页** T1 倒计时 / 上限；**冲浪页**顶部「赛博冲浪总开关」（乐观更新 + 失败回滚）；`mock.js` 同步提供 `sessions/platforms`、`groups`、`tier1_limit`、`toggle_surfing` 等 Mock 形状，`views/console/index.html?mock=1` 可离线预览。

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
    "decision": { "inherit": true, "inherit_from": "main", "source_id": "", "model": "" },
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
- `card` 由 `normalize_card()` 归一化后写入；`model_config` 为四角色归一化结构；`settings` 至少含 `surfing_enabled`，未知键原样保留。

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
| `model_config` | **v2.7 及以前的全局四角色配置**，仅作一次性迁移来源保留（不删） |
| `model_config_migrated_to_souls` | 一次性迁移 flag：旧全局配置 → 主模型为空的 Soul（**全部成功才写入**） |
| `legacy_umo_bindings_migrated` | 一次性迁移 flag：旧 UMO 绑定 → 平台绑定（**全部成功才写入**，失败下次启动重试） |
| `group_profiles` | 群聊档案列表（群名 / 备注 / 平台 / 绑定角色 / 最近活跃） |
| `runtime_state_v1` | v2.8 运行态快照（分享去重 / 各类冷却 / 忙碌成果消费 / ★L3 目标） |
| `last_forgetting_gc_date` | 最近一次每日遗忘 GC 的日期，保证跨重启每天只跑一次 |

---

## 3. 项目结构

```text
OnlyYou/
├── metadata.yaml                # 插件元数据（version: 2.8.2）
├── _conf_schema.json            # AstrBot WebUI 配置 Schema（仅运行参数；无模型绑定）
├── requirements.txt             # Python 依赖
├── main.py                      # 插件入口：三态门禁 / selected_provider / LLM 钩子 / on_decorating_result TTS / 后台任务 / 指令
├── core/                        # 核心逻辑
│   ├── state_machine.py         # 三态门禁控制器 + 四类行动卡枚举 (ActionType，含固定护甲)
│   ├── roguelite_deck.py        # 24h 动态积木时间轴引擎（专属卡池 / 作息曲线 / 弹性块拼接）
│   ├── schedule_mutator.py      # 输入驱动的受限微调（繁忙半途结算 / 睡眠唤醒 / 加钟延展）
│   ├── character_card.py        # 详细角色卡（CARD_SCHEMA 10 分区 / 36 字段 + Prompt 渲染）
│   ├── soul_manager.py          # Soul 独立 JSON 真源 / 索引自愈 / 平台绑定路由 / AI 造卡 / Persona 导入
│   ├── runtime_state.py         # v2.8 运行态持久化（去重 / 冷却 / ★L3 目标 → plugin_settings）
│   ├── user_relation.py         # 三级用户关系 + 用户专属态度 + 群聊档案（group_profiles）
│   ├── memory_manager.py        # 分级记忆 + T1 去重/上限/清理 + MEMORY_MUSE 三桶抽样 + Forgetting GC
│   ├── context_budget.py        # 上下文/记忆预算 + Persona 接管
│   ├── provider_factory.py      # AstrBot Provider 内部结构封装（复用/自建实例、TTS）
│   ├── model_hub.py             # 插件自治四角色模型网关（v2.8 支持按 Soul 读写）
│   ├── platform_bridge.py       # 平台协同桥（唤醒 / 分段 / 主动语音消息 / 机器人接口探测）
│   ├── web_api.py               # 31 条 Plugin Pages Web API
│   └── atmosphere_reader.py     # 群聊读空气 / 解线团
├── views/console/               # 控制台前端（无构建 classic script）
│   ├── index.html / style.css   # 外壳 + 设计令牌（浅色 / 深色）
│   ├── app.js / icons.js / charts.js / mock.js
│   └── page-*.js                # 概览 / 日程 / 角色卡 / 关系与群聊 / 记忆 / 冲浪 / 模型 / 会话 共 8 页
├── surfing/                     # B站 / 抖音冲浪管线与适配器（含 is_fallback 兜底标记）
├── storage/db.py                # aiosqlite 持久化（含 plugin_settings KV 表；v2.8 souls.revision 迁移）
├── prompts/templates.py         # 集中管理 Prompt 模板
├── tests/                       # 离线 unittest 测试套件（零网络）
└── docs/                        # 设计文档（PRD / ARCHITECTURE / PROMPTS_AND_FLOWS / WEBUI_DESIGN / UPGRADE）
```

> 生产数据目录（不入 Git）：`data/plugin_data/astrbot_plugin_only_you/{only_you.db, souls/*.json}`，详见 §2。

---

## 4. 文档套件导航

| 文档路径 | 文档名称 | 核心内容概述 |
| :--- | :--- | :--- |
| **[docs/PRD.md](docs/PRD.md)** | **产品需求文档 (PRD v2.8)** | 三态门禁 × 四类可执行行动卡、24h 动态弹性时间轴与繁忙结果短时记忆、输入驱动的块切分微调、分级记忆与定时遗忘、**详细角色卡（10 分区 / 36 字段）**、**插件自治四角色模型**与平台协同边界；v2.8 增补 **Soul 独立 JSON 一致性语义、机器人接口绑定路由、按 Soul 模型、冲浪分享门禁与总开关、T1/MEMORY_MUSE 记忆规则、运行态持久化、31 条 Web API 与解绑清空语义**。 |
| **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** | **技术架构与 AstrBot 实现方案 (v2.8)** | `BotState` 三态门禁 + `ActionType` 四类行动卡、24h 动态时间轴拼接与作息曲线、繁忙半途结算与 IDLE_ROAM 调度、`MemoryManager` 活力衰减与物理清理、`ContextBudgetManager` 裁剪与 **Persona 接管**、**`ProviderFactory` / `ModelHub` / `selected_provider`**、`plugin_settings` 表与最终 `_conf_schema.json`；v2.8 增补 **Soul JSON 持久化层 / revision 冲突裁决 / 运行时快照 / 31 条 API 清单**。 |
| **[docs/WEBUI_DESIGN.md](docs/WEBUI_DESIGN.md)** | **Plugin Pages 控制台方案 (v2.8)** | 48px 顶栏 + 216px 侧栏布局、灰白+青绿设计令牌、8 个页面设计要点（含 24h 弹性轴 / 四类卡编辑器 / 繁忙成果卡；v2.8 新版 **关系与群聊页 / 机器人接口页**、模型按角色、记忆 T1 倒计时、冲浪总开关）、Bridge 通信封装、**31 条 Web API 契约**。 |
| **[docs/PROMPTS_AND_FLOWS.md](docs/PROMPTS_AND_FLOWS.md)** | **Prompt 工程与交互时序 (v2.8)** | 角色卡整卡生成 / 分区补全 Prompt、四类专属行动卡池生成、繁忙完成结果与翻群 / 联想主动搭话 Prompt、受限状态微调、读空气、记忆提炼、**Persona 接管与插件 TTS 时序**；v2.8 增补 **按 Soul 模型解析、平台绑定路由、SURF 分享门禁、MEMORY_MUSE 三桶抽样**。 |
| **[docs/UPGRADE.md](docs/UPGRADE.md)** | **生产升级与迁移验证指南 (v2.8)** | v2.7 → v2.8 升级步骤：备份 `only_you.db` → 部署 / 热重载 → 日志校验（`Registered 31 Web API routes` / `Plugin initialized successfully` / 迁移日志 / 无 Traceback）→ `souls/*.json` 与一次性 flag 数据校验 → Pages 验收 → 回滚；含**解绑 / 清空类操作的「清理 vs 保留」排障语义表**、幂等重试与降级语义。 |
| **AGENTS.md（本地工程手册，GitHub 不可见）** | **开发、部署与联调规范** | 目录与模块职责、敏感信息安全规范、**AstrBot v4 底层机制与避坑指南**（`selected_provider`、Persona 段替换、`on_decorating_result`、`plugin_settings`、Provider 实例化）、**24h 时间轴不变量与繁忙结算契约**、Docker 部署、SSH 隧道 + OpenAPI 真机联调流程。⚠️ **该文件已在 `.gitignore` 中忽略，仅本地工程维护使用，GitHub 仓库不可见（因此本表不提供 Markdown 链接）**；GitHub 可见文档为 `README.md` + `docs/PRD.md` / `docs/ARCHITECTURE.md` / `docs/PROMPTS_AND_FLOWS.md` / `docs/WEBUI_DESIGN.md` / `docs/UPGRADE.md`。 |

---

## 5. 快速开始与升级

1. **安装**：将插件目录放入 AstrBot 的 `data/plugins/astrbot_plugin_only_you`，安装 `requirements.txt` 依赖，重启 / 热重载插件（要求 AstrBot `>= v4.5.7`）。
2. **配置**：
   - 在 AstrBot 插件配置面板设置 Roguelite / 关系 / 读空气 / 冲浪（含总开关）/ 记忆阈值（`_conf_schema.json` 只含这些运行参数）；
   - 打开插件 Pages 控制台，在「模型」页为**当前角色**配置 **对话（必填）/ 决策 / 视频解析 / TTS**（配置写入该角色 `souls/<soul_id>.json` 的 `model_config`）；
   - 在「角色卡」页手填或 AI 生成角色卡；在「会话 / 机器人接口」页把机器人接口绑定到角色并设置默认角色兜底。
3. **从 v2.7 升级到 v2.8**：**先备份 `only_you.db`（以及整个插件数据目录），再阅读 [docs/UPGRADE.md](docs/UPGRADE.md) 执行升级与迁移校验**。v2.8 首次启动会自动把旧 SQLite 角色数据落盘为 `souls/*.json`、迁移旧 UMO 绑定与全局模型配置（一次性 flag 全部成功才写入，失败下次启动自动重试）。
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
