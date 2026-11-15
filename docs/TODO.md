# OnlyYou 项目 TODO

> 状态约定：`待办` → `进行中` → `已完成`（已完成条目保留，不做删除，便于回溯）
> 当前版本：**v2.10.0**（2026-11-15）。T1 / T2 均已实现并通过离线回归（`python3 -m unittest discover -s tests`：717 tests 全绿）。

---

## 一、待办（未开始）

_（暂无）_

---

## 二、进行中

_（暂无）_

---

## 三、已完成

### T1 记忆时间标注：解决相对时间冻结与跨天窗口无法定位（已完成 2026-11-15，v2.10.0）

**涉及模块**：记忆引擎（三级记忆 T1/T2/T3）、记忆提炼、上下文滑窗注入、主动回复链路。

**现状问题**：

1. **记忆没有标注时间**：插件在主动抽取记忆、并基于记忆回复用户时，写入/注入的记忆条目不带时间信息。
2. **相对时间会冻结**：T3 核心记忆永久保留，其中的「这周末 / 明天 / 昨天」等相对时间表述在几周后仍原样出现，模型无法判断该表述所指的日期，也无法判断这条记忆是否已过期。
3. **跨天窗口无法定位**：15 条消息的会话滑窗在冷群里可能横跨好几天，提炼模型拿到窗口时不知道「今天」是哪一天，无法把窗口内的时间表述锚定到真实日期。

**目标要求**：

1. **记忆带时间**：每条被抽取的记忆都携带可追溯的时间基准（至少包含该记忆形成/被抽取的时刻），使模型能够还原其中相对时间表述的绝对指向。
2. **相对时间可解冻**：模型在朗读/复用记忆时，能把「这周末 / 明天 / 昨天」解析为具体日期，并据此判断该记忆是否已经过期，避免过期表述被当成当前事实使用。
3. **滑窗带时间基准**：会话滑窗在注入提炼模型时，需附带「当前日期/时间」以及窗口内各条消息的时间信息，使模型能定位窗口覆盖的日期范围、判断消息的先后与新鲜度。
4. **时间信息贯穿注入链路**：上述时间信息需随记忆与滑窗一起进入 Prompt，而不仅是留在存储层。

**实现落点（v2.10.0）**：

- **新增 `core/time_utils.py`**：纯函数本地时间格式化（`format_local_datetime` / `format_transcript_stamp` / `describe_time_anchor` / `current_datetime_text` / `describe_time_span`）；固定中文星期映射、非法/缺失/未来时间戳全部安全降级。
- **记忆存储**：`MemoryItem.to_dict()` 新增 `created_at_text` / `last_accessed_at_text`（`memories.created_at` 仍是唯一权威时间基准，**无数据库结构变更**）；Pages「记忆」页行展示「形成于 …」绝对时间 tooltip（`views/console/page-memory.js`）。
- **滑窗带时间**：`SessionBuffer.format_extraction_transcript()` / `get_extraction_transcript()` 为提炼模型生成带 `【窗口时间范围】首尾时间 ~ 尾端时间（跨度 N 天），共 N 条消息` 头部 + 每条消息 `[YYYY-MM-DD HH:MM]` 时间戳的 transcript（滑窗溢出回调、`/onlyyou memory extract`、Pages「记忆 → 提炼」统一走该入口）。
- **提炼 Prompt**：`MEMORY_EXTRACT_AND_GRADE_PROMPT` 新增 `{current_datetime}`（含星期）与「时间基准」说明，模型据此判断消息先后与新鲜度；**不要求把相对时间改写为绝对日期**，记忆正文保持原样（50 字上限不变）。
- **注入即标注**：`ContextBudgetManager` 注入每一条回忆记忆时写为 `- [层级 · 形成于 2026-11-05（星期四，10 天前）] 正文`，并在记忆块前拼接新增的 `MEMORY_TIME_CONTEXT_PROMPT`：说明标注是**形成时间（记忆锚点）**、相对时间按锚点解冻、指向日期已过去即视为历史/可能过期、未到才是未来安排；严格预算仍只统计正文（`injected_chars`）。
- **主动链路**：`IDLE_ROAM(MEMORY_MUSE)` 的记忆素材同样带形成时间；`/onlyyou memory list` 展示每条记忆的形成时间。
- **边界**：不改写记忆正文、不改 `created_at` 的 GC 语义、不改 `req.contexts` 聊天滑窗格式；T1 去重/上限/48h 清理/Forgetting GC/MEMORY_MUSE 抽样行为保持不变（既有回归断言未改而通过）。

**验收标准（自检口径）**：

- [x] 一条包含「这周末」的 T3 记忆，在其所指日期过去之后，被召回时能被模型识别为过期/历史信息，而不是继续当作近期安排。（注入块同时给出形成日期 + 「N 天前」+ 今日日期 + 过期判断规则）
- [x] 冷群跨多天的 15 条滑窗，提炼模型能正确说出窗口首尾消息各属于哪一天，以及「今天」是哪一天。（`【窗口时间范围】… 跨度 N 天` 头部 + 逐条时间戳 + `当前真实时间：…（星期…）`）
- [x] 记忆条目在存储与注入两个环节都能看到时间标注，且不破坏既有记忆的读写与 GC 行为。（`to_dict().created_at_text` / Pages 展示 / Prompt 逐条标注；既有记忆测试全部通过）

### T2 读空气新增「概率/预算」门闸：在决策模型之前做概率筛选，抑制 AI 过于活跃（已完成 2026-11-15，v2.10.0）

**涉及模块**：群聊读空气链路（气氛读取器 / 五维读空气决策）、行为参数配置。

**现状问题**：

目前读空气在调用决策模型之前**不做任何概率筛选**：消息只要通过前置条件，就会进入读空气决策模型判定。对「非 @ 普通群消息」而言，判定密度过高，容易让机器人显得过于活跃、抢话。

**目标要求**：

1. **新增门闸**：在「读空气」链路中增加一个「概率 / 预算」筛选门闸，按概率（或预算消耗）决定本次消息是否值得继续进入读空气判定，未通过则直接放弃。
2. **插入位置**：「直接提及（@ / 唤醒）判定」与「BUSY 特判」**之后**、「防抖等待」**之前**。即消息先经过前两个前置判定，未被直接放行/特判的普通消息再进入该门闸，通过后才进入防抖与读空气决策模型。
3. **生效范围**：**只对「非 @ 普通消息」生效**；@ 消息、唤醒指令、BUSY 特判路径均不受该门闸影响。
4. **可配置**：门闸的开关与概率/预算参数需可配置（如触发概率、单位时间预算等），并在插件配置中可见、可调、可关闭。
5. **目的**：降低无意义判定与主动搭话的触发频率，避免 AI 过于活跃。

**实现落点（v2.10.0）**：

- **`core/atmosphere_reader.py`**：新增 `reply_gate_enabled` / `reply_gate_probability`（0~100，默认 **80**）/ `reply_gate_hourly_limit`（默认 **0 = 不限**）三项实时配置属性与 `_passes_reply_gate(umo)`；插在 `evaluate_should_reply` 的「BUSY 特判 + 直接提及快速放行」之后、「防抖 `seq` + `asyncio.sleep(debounce_seconds)`」之前。概率用 `random.random() * 100 < probability`（100 恒通过 / 0 恒拦截）；每小时上限按 UMO 滚动 1 小时窗口记账（`_gate_pass_ts`，每次检查顺带裁剪），超限直接放弃。
- **可观测**：概率/预算拦截分别输出 `[OnlyYou] Reply gate dropped non-@ group message (probability=…%, umo=…)` / `(hourly limit=…, umo=…)`；被拦截消息不进入防抖、不调用决策模型、**不算未读**（已通过三态门禁，只是不参与读空气）。
- **配置**：`_conf_schema.json → atmosphere_config` 新增 3 项（`reply_gate_enabled` / `reply_gate_probability` / `reply_gate_hourly_limit`），标题 ≤24 字、均带 `hint`，非法/越界值在代码侧 clamp。
- **不受影响路径**：私聊、`takeover_group_chat=false`、@ / 唤醒 / 引用 Bot、`BUSY`（L2/L3 特判）均在门闸之前返回；心跳里的「群未读回放决策」有独立冷却，不走该门闸。
- **测试**：`tests/test_v210_atmosphere_gate.py` 冻结默认值/clamp、关闭与 100% 的向后兼容、0% 零决策模型调用、概率采样、@ · BUSY · 私聊旁路、每小时预算滚动与 `clear_gate_budget`、拦截时跳过防抖、配置 schema 契约。

**验收标准（自检口径）**：

- [x] 门闸关闭（或概率为 100%）时，行为与现状一致，即向后兼容。（`test_disabled_gate_keeps_legacy_behaviour` / `test_probability_100_keeps_legacy_behaviour`）
- [x] 概率为 0% 时，非 @ 普通群消息不再进入读空气决策模型；@ 消息与 BUSY 特判路径不受影响。（`test_probability_0_never_calls_decision_model` / `TestGateScopeBypasses`）
- [x] 门闸位于两个前置判定之后、防抖之前，可从日志/调用行为确认被拦截的消息不产生决策模型调用。（代码位置 + `test_gate_rejection_skips_debounce_window` + 拦截日志）
- [x] 配置项已加入插件配置并被正确读取，修改后即时生效。（`_conf_schema.json` 3 项 + `TestReplyGateSchema`；属性每次调用实时读取 `atmosphere_config`）

---

## 变更记录

| 日期 | 变更 |
| :--- | :--- |
| 2026-11-15 | 建立 TODO 文档；录入第一个目标 T1（记忆时间标注）。 |
| 2026-11-15 | 录入第二个目标 T2（读空气概率/预算门闸）。 |
| 2026-11-15 | **v2.10.0：完成 T1** —— `core/time_utils.py` 时间工具 + 记忆形成时间标注（`to_dict` / Pages / 注入 Prompt / MEMORY_MUSE）+ 提炼滑窗时间戳与 `【窗口时间范围】` + `MEMORY_TIME_CONTEXT_PROMPT`。 |
| 2026-11-15 | **v2.10.0：完成 T2** —— `AtmosphereReader` 概率/每小时预算门闸（默认开启 80% / 不限次数，可关闭或调 0~100%）+ `_conf_schema.json` 3 项配置 + 日志可观测。 |
