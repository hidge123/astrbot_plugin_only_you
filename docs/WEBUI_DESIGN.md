# OnlyYou (只有你) —— Plugin Pages 控制台设计方案 (v2.6)

| 文档版本 | 适用机制 | 页面入口路径 | 通信桥接对象 |
| :--- | :--- | :--- | :--- |
| v2.6 | AstrBot Plugin Pages (`views/`) | `views/console/index.html` | `window.AstrBotPluginPage`（兼容 `window.AstrBotPluginView`）+ `context.register_web_api` |

> 本文档描述 v2.6 的控制台设计：**48px 顶栏 + 216px 侧栏 + 8 个页面**的冷静极简风格（灰白 + 青绿），页面围绕 **24h 动态弹性时间轴 / 四类行动卡编辑器 / 繁忙进度条与今日繁忙成果** 组织，以及基于 `core/web_api.py` 的 27 条 RESTful 路由契约。v2.6 变更：模型页「模型来源模式」可选继承、关系页专属态度编辑、护甲全面隐藏改为固定档位、记忆页分层结构化列表、角色卡图片头像（Data URL）上传与渲染。

---

## 一、 为什么使用 AstrBot Pages 而不是普通 `_conf_schema.json`？

AstrBot 默认的 `_conf_schema.json` 只适合静态、扁平的基础键值对配置；而 **OnlyYou (`astrbot_plugin_only_you`)** 的核心数据是动态演化的角色生命体数据，且模型选择需要「提供商源 + 模型名」两级联动，静态表单无法承载：

1. **详细角色卡（10 分区 × 36 字段）**：需要按 schema 动态生成表单，支持标签输入、对话对编辑、AI 整卡生成 / 单分区补全、Prompt 预览、图片头像上传（v2.6）。
2. **角色专属四类行动卡池**：每个角色拥有 8~12 张 `SLEEP` / `SURF` / `IDLE_ROAM` / `BUSY_TASK` 行动卡（含 `min_hours ~ max_hours` 时长区间），需要卡片网格编辑与 AI 生成；不再有奇遇卡池。
3. **今日三态日程实况**：需要 24 小时时间轴、当前时段进度环形表与手动切换行动类型（v2.6 起护甲不再展示/可调，按行动类型固定档位）。
4. **四角色模型配置**：需要选择 AstrBot 提供商源、拉取模型名、测试连通性、试听 TTS——这些都需要联网交互，不能写死为配置项。
5. **三级关系 / 分级记忆 / 冲浪视频池**：需要看板、拖拽、搜索、提炼与 GC 等交互式管理；v2.6 关系页支持为单个用户设置专属态度 `attitude`，记忆页改为「层级概览条 + 按 Tier 分组的结构化列表」。

### 配置职责切分（三层）

| 层 | 位置 | 职责 |
| :--- | :--- | :--- |
| **交互式管理** | `views/console/`（本页） | 角色卡、行动卡池、今日日程、用户关系、分级记忆、冲浪视频池、**四角色模型配置**、默认角色与会话绑定 |
| **运行参数** | `_conf_schema.json`（AstrBot 插件配置面板） | Roguelite 阈值、三级关系名单、读空气、冲浪、记忆与上下文预算等**静态参数**。v2.4 起**不再包含任何模型绑定**（模型配置已迁移至 SQLite `plugin_settings`） |
| **插件自治设置** | SQLite `plugin_settings` 表 | `model_config`（四角色模型）、`default_soul_id`（默认角色）、`legacy_model_migrated`（迁移标记）。不放进 AstrBotConfig，避免 schema 完整性检查删键 |

---

## 二、 整体布局与视觉规范

### 2.1 布局骨架

```text
+-----------------------------------------------------------------------+
| ◉ OnlyYou   当前角色 [ 酒狐 ▾ ]                         ⟳（刷新） ◐（主题）|  48px 顶栏
+-----------+-----------------------------------------------------------+
| 运行       |                                                           |
|   概览     |                                                           |
|   日程     |                                                           |
| 角色       |               主工作区（按 hash 路由渲染）                  |
|   角色卡   |                                                           |
| 关系       |                                                           |
| 记忆       |                                                           |
| 冲浪       |                                                           |
| 系统       |                                                           |
|   模型     |  ← 主模型未配置时显示红点                                  |
|   会话     |                                                           |
+-----------+-----------------------------------------------------------+
```

- **48px 精简顶栏**：左侧品牌标记 + `OnlyYou` 文字；中间是「当前角色」下拉（`#soul-select`，切换后全局生效）；右侧只有两个图标按钮——刷新（`data-tip="刷新数据"`）与明暗主题切换。
- **216px 侧栏**：由 `app.js` 从 `NAV_CONFIG` + `window.OY_PAGES` 自动生成，分组顺序固定为：
  - **运行**：概览、日程
  - **角色**：角色卡
  - **（无分组）**：关系、记忆、冲浪
  - **系统**：模型、会话
  - 「模型」项在主模型未配置时显示红点提示。
- **主工作区**：`<main class="main"><div id="view">`，按 hash 路由（`#/overview`、`#/schedule`…）渲染；首次加载显示 spinner「正在连接 OnlyYou…」。
- **浮层容器**：`#toast-container`（轻提示）、`#modal-root`（弹窗）、`#drawer-root`（右侧抽屉）。
- **不再有「顶部状态条」**：原设计的全局实时状态条已移除，状态环形表、护甲、微调配额、下一时段等信息全部迁入「概览」页主卡。

### 2.2 设计令牌（Design Tokens）

浅色主题（`:root`）：

| 令牌 | 取值 | 用途 |
| :--- | :--- | :--- |
| `--bg` | `#fafafa` | 页面底色 |
| `--bg-soft` | `#f4f5f7` | 次级底色 / 悬停 |
| 卡片 | `#ffffff` | 卡片 / 弹窗背景 |
| 描边 | `#e5e7eb` | 边框、分割线 |
| `--primary` | `#0d9488` | 主色（青绿）：主按钮、选中态、强调 |
| `--primary-hover` | `#0f766e` | 主色悬停 |
| `--primary-soft` / `--primary-ring` | `rgba(13,148,136,.10)` / `rgba(13,148,136,.20)` | 浅色背景 / 焦点环 |

深色主题（`html.dark` / `html[data-theme="dark"]`）：

| 令牌 | 取值 |
| :--- | :--- |
| `--bg` | `#0f1115` |
| `--bg-soft` | `#121419` |
| 卡片 | `#16181d` |
| `--primary` | `#2dd4bf` |
| `--primary-hover` | `#5eead4` |

三态语义色（低饱和，用于状态名、环形表、时间轴色块）：

| 状态 | 颜色 |
| :--- | :--- |
| 空闲中 `IDLE` | `#10b981` |
| 繁忙中 `BUSY` | `#f59e0b` |
| 休息中 `RESTING` | `#6366f1` |

### 2.3 排版、图标与交互规范

1. **正文 13–14px**；标签 / 辅助信息用灰色小字；不写长标题与长副标题，说明性文字统一放进 **`data-tip` 悬浮提示（ⓘ）**，由 `style.css` 的 `[data-tip]` 统一实现。
2. **按钮不使用 emoji**，全部使用 `icons.js` 的内联 SVG 线性图标（`stroke="currentColor"`，默认 16px）；图标也可用于标题、空状态与操作菜单。
3. **每页只保留 1 个实心主按钮**（`btn-primary`，青绿实心）；其余操作使用描边按钮（`btn-outline`）、幽灵按钮（`btn-ghost`）或图标按钮（`icon-btn`）。
4. **组件**：卡片、输入框、下拉、分段控件（segmented）、标签/胶囊、进度条、环形表、时间轴、看板、抽屉、弹窗、toast、悬浮提示、空状态，全部由 `style.css` 提供统一样式。
5. **状态反馈**：loading（spinner）/ empty（空状态文案 + 引导操作）/ toast（成功、失败）；危险操作（删除、GC、删除角色）必须二次确认。
6. **主题**：默认跟随系统 `prefers-color-scheme`，并监听 AstrBot Dashboard 的 `getContext()` / `onContext()`；用户手动切换后写入 `localStorage["onlyyou.console.theme"]`，同时给 `<html>` 加 `dark` class 与 `data-theme` 属性。

### 2.4 组件与交互约定

- **页面标题**使用单行小标题（16px），右侧放该页唯一的实心主操作。
- **列表 / 网格**：角色卡、关系、记忆、冲浪均使用卡片网格；悬停显示操作菜单（⋯）。
- **表单**：分区卡片式布局；下拉用原生 `select` 美化；标签类用胶囊输入（逗号 / 顿号 / 斜杠分隔）；对话对用「用户 / 角色」两栏编辑器。
- **AI 草稿**：改动字段用青绿描边高亮，顶部提供「采用草稿 / 放弃草稿」，未保存时底部固定保存栏显示「未保存修改」。
- **抽屉**用于编辑单个时段、单个用户、单张视频；**弹窗**用于确认、AI 生成、Prompt 预览、试听。

---

## 三、 页面清单与设计要点

| 页面 | 路由 | 侧栏分组 | 主要接口 |
| :--- | :--- | :--- | :--- |
| 概览 | `#/overview` | 运行 | `dashboard/overview` |
| 日程 | `#/schedule` | 运行 | `souls/detail`、`cards/*`、`schedule/*` |
| 角色卡 | `#/character` | 角色 | `card/*`、`souls/*` |
| 关系 | `#/relations` | — | `users/list`、`users/update` |
| 记忆 | `#/memory` | — | `memories/list`、`memories/mutate` |
| 冲浪 | `#/surfing` | — | `surfing/list_and_trigger` |
| 模型 | `#/models` | 系统 | `models/*` |
| 会话 | `#/sessions` | 系统 | `sessions/default_soul`、`souls/save` |

### 3.1 概览（`page-overview.js`）

替代原「顶部状态条」，一屏看清当前运行态：

1. **状态主卡（v2.6 改为当前时间块进度）**：左侧 SVG 环形表（`OYCharts.ringGauge`）展示 **当前时间块进度 `progress_pct`**（sublabel 形如「进度 X%」），颜色随三态变化，**不再展示护甲数值**；中间显示状态名 + 行动卡类型标签（`SLEEP / SURF / IDLE_ROAM / BUSY_TASK`）+ 当前活动标题；下方是「当前时间块完成进度条 (`0% -> 100%`)」（当处于 `BUSY_TASK` 繁忙卡时直观展示当前主业推进百分比，完成后结算为一条 Tier 1 短时记忆）；右侧预告下一时间块（名称、起止时间、状态色，不再带「护甲 X」）。
2. **24 小时动态积木日程轴**（`OYCharts.scheduleAxis`）：按当日随机拼接生成的 7~11 个可变时长 `ScheduleBlock`（`start_hour ~ end_hour`）等比例画成弹性色块，带「现在」指针；**点击任意时间块**弹出行动类型切换弹窗（四类行动类型 + 标题输入，**已无护甲滑条**，护甲由行动类型固定档位自动应用），提交 `schedule/force_state`。
3. **统计小卡**：今日基调卡（标题 / 描述 + 当前角色作息类型 `chronotype`）、微调配额（`OYCharts.dots` 的 ●●○ + 冷却倒计时）、今日繁忙完成结果（由 `BUSY_TASK` 走完 100% 或被打断时随机结算生成的 Tier 1 记忆列表）、关系环形图（L1/L2/L3，`OYCharts.donut`）、记忆分级条形图（`OYCharts.bars`）、模型健康指示灯（四角色状态）。模型未配置时指示灯变红并跳转「模型」页。

### 3.2 日程（`page-schedule.js`）

1. **上方 24 小时动态时间轴**：同概览的 `scheduleAxis`，展示由角色作息类型（`early / normal / night_owl`）与角色专属四类行动卡池动态拼接而成的 24h 时间块序列；若某 `BUSY_TASK` 块已走完 `100%`，悬浮提示展示其结算生成的一句话随机完成结果记忆；点击时间块打开右侧抽屉编辑并保存走 `schedule/force_state`。
2. **下方四类行动卡网格（已移除冗余奇遇卡面板，v2.6 已移除迷你护甲条）**：左侧 3px 边框颜色表示绑定状态（`IDLE` 青绿 / `BUSY` 琥珀 / `RESTING` 靛蓝），卡片展示行动类型徽章（`SLEEP 睡觉` / `SURF 冲浪` / `IDLE_ROAM 闲逛` / `BUSY_TASK 繁忙主业`）、卡名、时长范围胶囊（如 `1.5h ~ 3.0h`）与抽中权重。顶部筛选分段控件支持按三态或四类行动类型筛选。
3. **操作**：重抽今日 24h 动态日程（`schedule/reroll`）、新建 / 编辑 / 删除专属行动卡（`cards/save` / `cards/delete`）、AI 生成一批贴合角色设定的四类新卡（`cards/ai_generate`，调用决策模型生成 8~12 张专属行动卡）。
4. 卡片编辑器字段：`title`、`action_type`（`SLEEP / SURF / IDLE_ROAM / BUSY_TASK`，自动联动绑定 `state`）、`min_hours` & `max_hours`（`0.5h ~ 8.0h` 动态时长区间）、`base_weight`（`1–100`）、`scope`（当前角色 / 全局）。**v2.6 删除 `armor` 字段**，时间块与卡片的护甲由后端按行动类型固定档位写入（`SLEEP=80 / BUSY_TASK=65 / SURF=40 / IDLE_ROAM=25`）。

### 3.3 角色卡（`page-character.js`）

1. **左栏**：头像 + 名字 + 一句话定位；分区导航，每项显示完成度圆点（● 已填 / ◐ 部分 / ○ 空）。v2.6 起左侧画像卡大号头像可点击上传 / 更换图片（悬浮提示「点击上传或更换头像图片」），未设置图片时回退 Emoji / 首字母。操作：
   - **AI 生成整卡**（`card/ai_generate`，主模型）；
   - **从 AstrBot 导入**（`souls/import_persona`，可选 AI 扩写整卡与行动卡池；v2.6 弹窗内可上传「角色头像」并随 body 的 `avatar` 一起提交）；
   - **Prompt 预览**（`card/preview_prompt`，显示渲染结果与字数）；
   - **⋯ 菜单**：新建角色、修改 Soul ID（`souls/rename`）、删除角色（`souls/delete`）。
2. **右栏**：按 `GET card/schema` 下发的 `CARD_SCHEMA` 动态渲染 10 个分区卡片，支持 `text / textarea / tags / list / select / dialogs` 六种字段类型；每个分区标题旁有「AI 补全」（`card/ai_generate` + `section`）。
3. **草稿机制**：AI 结果作为草稿填入，改动字段青绿描边；「采用草稿 / 放弃草稿」；底部固定保存栏显示「未保存修改」，保存走 `card/save`（服务端 `normalize_card` 归一化）。
4. **原生人设原文**（`extra.native_persona_prompt`）只读展示，仅记录导入来源，不参与 Prompt 渲染。
5. **图片头像（v2.6）**：`identity.avatar` 字段改为图文混合控件——缩略图预览 + 「上传图片」/「清除图片」按钮 + 文本输入框（Emoji / 短字符 / 图片 URL）；当值为 `data:image/` 开头时输入框显示只读标签「已设置本地上传图片」，**不展示 Base64**。前端用 `<canvas>` 居中裁剪并压缩为 160×160 Data URL（优先 `image/webp@0.86`，不支持时回退 `image/jpeg@0.9`），随 `card/save` 写入 SQLite；导入 Persona 弹窗与新建角色弹窗同样提供头像上传 + 预览。头像绝不进入 Prompt。

### 3.4 关系（`page-relations.js`）

1. **三列看板**：★L3 核心 / ◆L2 重要 / L1 普通，用户卡显示首字母头像、名字、好感度进度条、平台账号数；v2.6 起若该用户设置了专属态度 `attitude`，卡片额外展示一行带引号的「态度：…」摘要。
2. **L1 / L2 拖拽换列即改等级**：拖拽后调用 `users/update`（`action="update"` + `tier`）；点击卡片打开抽屉编辑昵称、称呼、好感度、平台 ID 列表，以及 v2.6 新增的「角色对 Ta 的专属态度 (`attitude`)」多行输入框（`maxlength=300`，placeholder 例如「把 Ta 当成最信任的损友，说话可以毒舌一点但关键时刻很护短」），随 `users/update` 一起提交。
3. **★L3 = 管理员级（v2.7）**：L3 不通过好感度产生（好感度自动晋升只到 L2），也不参与好感度展示。入口有两种：① 管理员身份 / `relationship_config.core_user_ids` 自动确定；② 关系页用户卡「⋯ → ★ 设为 L3 核心用户 / 取消 ★L3」显式设置（二次确认，`users/update` 写入 tier=3/2），或 `/onlyyou tier <uid> 3`。为避免误触，拖拽仍只在 L1 / L2 之间生效（L3 列不接受拖入、L3 卡片不可拖拽）；L3 用户卡显示「管理员级 · 无需好感度」，编辑抽屉中关系等级只读、无好感度输入。
3. **⋯ 菜单**：合并跨平台账号（`users/update`，`action="merge"` + `primary_uid` / `secondary_uid`）、删除用户（`action="delete"`）。

### 3.5 记忆（`page-memory.js`，v2.6 分层结构化列表）

废弃原 `.memory-grid` 多列卡片矩阵，改为「顶部层级概览条 + 按 Tier 分组的结构化横排列表」，保留全部既有能力（新增 / 编辑 / 删除 / 提炼 / GC / 搜索防抖 / 关联用户筛选 / 刷新 / 注入预算提示 / 空态）：

1. **层级分布概览条 `.memory-summary-strip`**：全部 / T3 核心 / T2 近期 / T1 瞬时 计数 + 彩色分段占比条；点击即切换单层筛选（与工具栏筛选合并为一个，不重复）。
2. **分组列表 `.memory-group`**：默认按 `T3 → T2 → T1` 纵向排列，分组头显示图标标题 + 计数 + 说明（T3 永久保留免疫 GC / T2 按活力衰减可晋升 / T1 48 小时过期），点击可折叠展开；空分组渲染 `.memory-group-empty` 并默认收起。
3. **结构化记忆行 `.memory-row`**（`grid-template-columns: 76px minmax(0,1fr) 190px auto`，窄屏响应式折行）：列1 `T3/T2/T1` 徽章 + 重要度；列2 正文 + `#关键词` 胶囊；列3 关联用户标签 + 活力条（T3 显示「永久保存」）+ 召回次数/时间；列4 仅显示**当前层级以外**的升降级按钮 + 编辑 + 删除。
4. **工具栏**：关联用户下拉（`users/list`）、关键词搜索（400ms 防抖）、上下文注入预算提示，以及页头 **提炼**（`memories/mutate`，`action="extract"`，对最近滑窗执行一次决策模型记忆提炼）、**GC**（`action="gc"`，手动触发遗忘清理）、**刷新**、**新增记忆**（`action="add"`）。数据仍来自 `memories/list`（`{memories, stats}`），后端接口结构不变。

### 3.6 冲浪（`page-surfing.js`）

1. 视频卡片墙：封面、评分徽章、标题、UP 主、AI 吐槽文案；分享与删除收进卡片悬浮菜单。
2. 工具栏：立即刷一次 B 站 / 抖音（`surfing/list_and_trigger`，`action="surf"`，可勾选忽略状态门禁用于调试）。
3. 分享弹窗：选择目标会话（`known_umos`）后 `action="share"`；删除走 `action="delete"`。

### 3.7 模型（`page-models.js`）

0. **顶栏控件提示**：顶栏（当前角色 / 刷新 / 主题切换）贴近视口顶部，其 `[data-tip]` 气泡统一向下弹出、右侧操作按钮右对齐，避免被视口裁切。
1. **4 张角色卡片**：**对话（main，必填）/ 决策（decision，读空气）/ 视频解析（vision）/ 语音 TTS（tts）**。
2. 每张卡片：提供商源下拉（`models/sources`）、模型名输入 + 候选列表（`models/list`，可远程拉取）、v2.6「**模型来源模式**」下拉（decision：独立配置 / 继承自主对话模型 `main`；vision：独立配置 / 继承 `main` / 继承自轻量决策模型 `decision`）、状态灯（未配置 / 继承自父角色·来源快照 / 复用实例 / 插件自建）、「测试」按钮（`models/test`，显示耗时与回复）。选择「独立配置」时展开自己的源 + 模型选择器（允许留空 = 该角色停用，**不再回退 main**）；选择继承时折叠独立选择器并展示父角色当前 `source_id / model` 快照，未配置则提示「未配置，该角色将停用」。状态徽章按后端 `status.configured / inherited / inherit_from` 渲染。
3. **TTS 卡片**：来源分段（关闭 / 提供商源 / AstrBot TTS）、模型名、音色、触发概率滑条、生效范围（私聊 / L3 / 全部）、「语音 + 文字双发」开关、「试听」按钮（`models/test` 返回 base64，用 `<audio>` 播放）。
4. **即时生效（v2.7）**：任何模型配置修改（来源模式 / 提供商源 / 模型名 / TTS 各项）停止输入约 0.7s 后自动 `POST models/config`（写入 `plugin_settings` 并热重建实例），状态灯即时刷新。v2.7 起**已移除底部固定保存条**——无需手动保存，`Ctrl/Cmd + S` 仍可强制立即保存（无按钮）；保存 payload 必须带 `inherit` 与 `inherit_from`（保持 `{model_config: {...}}` 形状不变）；主模型未配置时页面顶部红字提示。

### 3.8 会话（`page-sessions.js`）

1. **插件默认角色**下拉（`sessions/default_soul`）：用于未绑定 UMO 的会话路由。
2. **UMO 绑定表**：列出已捕获的群聊 / 私聊会话（`known_umos`），可选择绑定的 Soul（保存走 `souls/save` 的 `umo_binding` 字段），支持清空绑定。
3. 显示路由优先级说明：**UMO 绑定 → 插件默认角色 → 最近更新的角色 → 创建默认角色**。

---

## 四、 前端实现架构与桥接方式

### 4.1 文件清单与加载顺序（无构建 classic script）

```text
views/console/
├── index.html      # 外壳：48px 顶栏 / 216px 侧栏 / main#view / toast / modal / drawer 容器
├── style.css       # 设计令牌 + 全部组件样式（浅色 / 深色）
├── icons.js        # ≥20 个内联 SVG 线性图标（window.OY_ICONS / window.icon）
├── charts.js       # 纯 SVG 图表（window.OYCharts）
├── app.js          # bridge 封装 / 轻量状态 / hash 路由 / toast / modal / drawer / 主题
├── page-overview.js / page-schedule.js / page-character.js / page-relations.js
├── page-memory.js / page-surfing.js / page-models.js / page-sessions.js
└── mock.js         # ?mock=1 或 file:// 打开时的本地 Mock bridge（离线预览 / 截图）
```

`index.html` 中的加载顺序固定：

```html
<script src="/api/plugin/page/bridge-sdk.js"></script>
<script src="icons.js"></script>
<script src="charts.js"></script>
<script src="app.js"></script>
<!-- page-*.js 共 8 个，顺序见 index.html -->
<script src="mock.js"></script>
```

所有 JS 都是 **classic script、全局变量、无 import/export、无外部 CDN、无 emoji 图标**；`app.js` 必须早于所有 `page-*.js`。

### 4.2 Bridge 通信封装（`app.js`）

```javascript
var bridge = window.AstrBotPluginPage || window.AstrBotPluginView;

// 主题握手：ready()（1.5s 超时后回退 getContext()）+ onContext(cb)
var ctx = await bridge.ready();          // { isDark: boolean } 或 { theme: "dark"|"light" }
bridge.onContext(function (nextCtx) { /* 再次同步主题 */ });

// 业务请求：endpoint 不带 /astrbot_plugin_only_you 前缀
var data = await app.apiGet("dashboard/overview", { soul_id: "default_soul" });
var result = await app.apiPost("card/save", { soul_id: "default_soul", card: {...} });
```

- `apiGet(endpoint, params)` / `apiPost(endpoint, body)` 会**自动解开后端 `{status, message, data}` 信封**：`status="ok"` 返回 `data`，`status="error"` 抛出后端 `message`。
- **无 bridge 兜底**：直接 `fetch("/api/plug/astrbot_plugin_only_you/{endpoint}")`（GET 拼 query，POST 发 JSON，`credentials: "include"`），用于本地 / Pages 服务降级。
- **Mock 模式**：`file://` 直接打开或带 `?mock=1` 时，`mock.js` 安装本地 mock bridge，前端无需后端即可渲染全部页面（用于截图验收）。
- 主题持久化：`localStorage["onlyyou.console.theme"]`；当前角色持久化：`localStorage["onlyyou.console.soulId"]`。

### 4.3 `app.js` 全局约定

```js
window.app = new OnlyYouConsoleApp();   // DOMContentLoaded 自动 init

app.apiGet(endpoint, params) / app.apiPost(endpoint, body)
app.state = { souls, currentSoulId, currentSoul, overview, modelStatus, slotDefinitions,
              defaultSoulId, nativePersonas, umoBindings, knownUmos, activeSchedule }
app.icon(name, size)                    // 取 icons.js 的 SVG 字符串
app.toast(msg, isError)
app.modal({ title, body, actions }) / app.closeModal()
app.drawer({ title, body, actions }) / app.closeDrawer()
app.refresh()                           // 重新拉当前路由数据并渲染
app.navigate(routeId)                   // hash 路由，如 #/overview
app.theme / app.applyTheme(isDark) / app.toggleTheme()
app.esc(str)                            // HTML 转义
app.isImageAvatar(value)                // v2.6：data:image / http(s) / 站内路径判定
app.renderAvatarInner(avatar, name)     // v2.6：图片 -> <img class="avatar-img">；否则 Emoji / 首字
app.pickAvatarImage({ size: 160 })      // v2.6：Promise<string>，选图 + Canvas 居中裁剪压缩为 Data URL
```

页面模块通过全局注册表接入：

```js
window.OY_PAGES["overview"] = {
  id: "overview",
  title: "概览",
  icon: "gauge",
  async render(container, ctx) { /* 渲染页面，ctx 即 app */ },
  async destroy() {}
};
```

### 4.4 图标与图表模块

- **`icons.js`**：`window.OY_ICONS = { gauge, calendar, character, relations, memory, surf, models, sessions, refresh, moon, sun, plus, trash, edit, check, close, info, wand, play, save, chevron, ... }`（≥20 个）；`window.icon(name, size = 16)` 返回内联 SVG 字符串（`stroke="currentColor"`）。
- **`charts.js`**：`window.OYCharts` 提供纯 SVG、无依赖的图表，全部返回 HTML 字符串由页面 `innerHTML` 注入：
  - `ringGauge({ value, max, label, sublabel, color, size })`：通用环形表（v2.6 概览页用于「当前时间块进度」，不再展示护甲）；
  - `scheduleAxis({ slots, currentKey, nowHour, clickable })`：24h 时段色块 + 现在指针，tooltip 显示行动类型与进度（v2.6 删除「护甲 X」行）；
  - `donut({ items, size, centerValue, centerLabel })`：关系环形图；
  - `bars({ items })`：记忆分级条形图；
  - `dots({ used, max })`：微调配额 ●●○。

---

## 五、 后端 Web API 契约（27 条）

所有路由前缀为 `/astrbot_plugin_only_you`，由 `main.py` 初始化时调用 `self.web_api.register_routes()` 注册（`context.register_web_api`）。统一响应信封：

```json
{ "status": "ok | error", "message": "提示文案", "data": { } }
```

前端 `apiGet/apiPost` 已自动解包；`status="error"` 时 `data` 通常为空、`message` 为错误原因。

### 5.1 现有 17 条路由（v2.5 保留兼容）

| # | 路由（前缀后） | 方法 | 入参 | `data` 返回 |
| :--- | :--- | :--- | :--- | :--- |
| 1 | `init_summary` | GET | - | `{ souls, native_personas, umo_bindings, known_umos, active_soul_id, active_schedule, default_soul_id, model_status, action_types }`；`souls` 每项为 `SoulProfile.to_dict()`（含 `card` 与旧扁平字段），`model_status` 为四角色状态（`main/decision/vision/tts`），`action_types` 为四类行动定义 |
| 2 | `souls/detail` | GET | `?soul_id=` | `{ soul, card_schema, schedule, umo_bindings, known_umos }` |
| 3 | `souls/import_persona` | POST | `{ persona_name, target_soul_id?, auto_generate_cards?, ai_generate_card?, native_prompt?, avatar? }`（v2.6 `avatar` 为可选图片 Data URL / URL / Emoji） | `{ soul, schedule, souls }` |
| 4 | `souls/save` | POST | `{ soul_id, card?, umo_binding? }`（也兼容旧扁平字段） | `{ soul, souls, umo_bindings, known_umos }` |
| 5 | `souls/rename` | POST | `{ old_soul_id, new_soul_id }` | `{ soul, souls, schedule }`，级联更新关联表 |
| 6 | `souls/delete` | POST | `{ soul_id }` | `{ deleted_soul_id, souls, active_soul_id }` |
| 7 | `schedule/reroll` | POST | `{ soul_id? }` | `{ schedule }`（`run/current_slot/next_slot/chronotype/busy_results/配额与冷却`；重新随机拼接 24h 动态时间轴） |
| 8 | `schedule/force_state` | POST | `{ soul_id?, slot_key?, action_type?(SLEEP/SURF/IDLE_ROAM/BUSY_TASK), new_state?(RESTING/BUSY/IDLE，兼容旧入参), new_title? }`（v2.6 `armor` 入参已废弃，传了也会被忽略） | `{ updated_slot, schedule }`；`action_type` 优先并自动绑定 state，`updated_slot.armor` 为固定档位 |
| 9 | `cards/list` | GET | `?soul_id=` | `{ soul_id, custom_cards, general_cards, action_types, chronotype }`；卡片含 `action_type/min_hours/max_hours/armor/base_weight`，其中 `armor` 恒为 action_type 固定档位；`action_types` 每项含 `armor` / `armor_tier` |
| 10 | `cards/save` | POST | `{ soul_id?, card_type:"action", scope:"soul"\|"global", card }`（card 含 `action_type/min_hours/max_hours`；v2.6 传 `armor` 会被固定档位覆盖；encounter 类型已移除并返回错误） | 同 `cards/list` 的卡片池结构 |
| 11 | `cards/delete` | POST | `{ soul_id?, card_type:"action", scope, card_id }` | 同 `cards/list` 的卡片池结构 |
| 12 | `cards/ai_generate` | POST | `{ soul_id?, auto_save? }` | 卡片池结构 + `{ generated_cards }`（决策模型生成 8~12 张四类专属行动卡；v2.6 Prompt 不再要求 armor，落库时按固定档位写入） |
| 13 | `users/list` | GET | `?tier=1\|2\|3` | `{ users, counts }`；`users` 每项含 `global_uid/display_name/nickname/attitude/tier/affinity/platform_ids` 等（v2.6 新增 `attitude`） |
| 14 | `users/update` | POST | `{ action:"update"\|"merge"\|"delete", global_uid?, tier?, affinity?, display_name?, nickname?, attitude?(≤300 字), platform_ids?, primary_uid?, secondary_uid? }` | update：`{ user, users }`；merge：`{ merged_user, users }`；delete：`{ users }`；v2.6 `attitude` 去空白后截断 300 字持久化 |
| 15 | `memories/list` | GET | `?soul_id&memory_tier=1\|2\|3&user_id&keyword` | `{ memories, stats }`；`stats` 含 `tier3_count/tier2_count/tier1_count/total` 与上下文预算参数 |
| 16 | `memories/mutate` | POST | `{ action:"add"\|"edit"\|"delete"\|"pin_core"\|"extract"\|"gc", soul_id, id?, content?, keywords?, importance?, memory_tier?, vitality?, user_id?, umo? }` | 各 action 返回对应结果（`memories` / `memory` / `gc_stats` / `extracted_count` 等） |
| 17 | `surfing/list_and_trigger` | GET / POST | GET：`?soul_id=`；POST：`{ action:"surf"\|"share"\|"delete", soul_id?, platform?, ignore_state_gate?, video_id?, target_umo?, id? }` | GET：`{ videos, known_umos }`；surf：`{ review, videos, schedule }`；share：`{ sent, target_umo, share_text }`；delete：`{ videos }` |

### 5.2 新增 10 条路由（v2.4 Pages 改版）

| # | 路由（前缀后） | 方法 | 入参 | `data` 返回 |
| :--- | :--- | :--- | :--- | :--- |
| 18 | `dashboard/overview` | GET | `?soul_id=` | `{ soul, vigor, state, schedule, quota, encounters(空数组兼容), busy_results, counts, models }`（见下；进入前会惰性结算已结束繁忙块） |
| 19 | `card/schema` | GET | `?soul_id=` | `{ schema: CARD_SCHEMA, card, soul_id }` |
| 20 | `card/save` | POST | `{ soul_id, card }` | `{ soul, souls }`（`normalize_card` 归一化后持久化） |
| 21 | `card/ai_generate` | POST | `{ soul_id?, seed_text?, section? }` | `{ draft_card, section }`（**不落库**；整卡或单分区草稿） |
| 22 | `card/preview_prompt` | POST | `{ soul_id? \| card?, max_chars? }` | `{ prompt, chars }`（渲染后的人格 Prompt 与字数） |
| 23 | `models/sources` | GET | - | `{ sources, tts_providers }`；`sources` 项为 `{id,type,provider_type,provider,api_base,has_key,enable}`，**绝不含 key** |
| 24 | `models/list` | GET | `?source_id=` | `{ source_id, models }`；`models` 项为 `{model,id,enable,modalities,source:"config"\|"remote"}` |
| 25 | `models/config` | GET / POST | GET：-；POST：`{ model_config }`（或直接顶层 `{main,decision,vision,tts}`；decision / vision 需带 `inherit` 与 `inherit_from`） | `{ model_config, status }`；`model_config` 为归一化后的四键结构（`inherit` 与 `inherit_from` 恒同步），`status` 每个角色含 `configured/inherited/inherit/inherit_from/source_id/model/provider_id/mode/label/ok`；保存后热重建实例 |
| 26 | `models/test` | POST | `{ role: "main"\|"decision"\|"vision"\|"tts" }` | 聊天角色：`{ status, role, provider_id, model, latency_ms, reply }`；TTS：`{ status, role, latency_ms, audio_path, audio_base64 }` |
| 27 | `sessions/default_soul` | POST | `{ soul_id }` | `{ default_soul_id, souls, umo_bindings }` |

`dashboard/overview` 的 `data` 结构：

```json
{
  "soul": {"soul_id": "", "name": "", "avatar": "🌙 或 data:image/...", "tagline": ""},
  "vigor": {"title": "", "desc": "", "chronotype": "night_owl"},
  "state": {"state": "IDLE", "label": "", "title": "", "armor": 65, "slot_key": "",
            "action_type": "BUSY_TASK", "action_type_label": "繁忙主业",
            "start_hour": 12.5, "end_hour": 14.0, "remaining_seconds": 1234,
            "progress": 0.42, "progress_pct": 42, "completion_result": "",
            "busy_settled": false, "mutated": false, "next_slot": {}},
  "schedule": {"run": {"slots": [{"start_hour": 0.0, "end_hour": 7.5, "action_type": "SLEEP", "progress_pct": 100}]},
               "current_slot": {}, "next_slot": {}, "chronotype": "night_owl",
               "busy_results": [], "max_mutations_per_day": 2, "mutation_cooldown_minutes": 120},
  "quota": {"used": 1, "max": 2, "remaining": 1, "cooldown_remaining_seconds": 0},
  "encounters": [],
  "busy_results": [{"slot_key": "block_2", "title": "在丹房炼制回春丹",
                     "completion_result": "回春丹炼成了，最后一炉差点炸炉",
                     "progress_pct": 100, "start_hour": 9.0, "end_hour": 12.0,
                     "start_time": "09:00", "end_time": "12:00"}],
  "counts": {"relations": {"l1": 0, "l2": 0, "l3": 0, "total": 0},
             "memories": {"tier1": 0, "tier2": 0, "tier3": 0, "total": 0},
             "videos": 0},
  "models": {}
}
```

> v2.6 说明：`state.armor` 仍保留在 payload 中以兼容旧前端，但恒为行动类型固定档位（`SLEEP=80 / BUSY_TASK=65 / SURF=40 / IDLE_ROAM=25`）；`models` 每个角色额外带 `inherit / inherit_from / inherited`（见路由 25）。

---

## 六、 开发与验收约定

1. **语法检查**：`node --check views/console/*.js` 全部通过后再交付。
2. **离线预览**：直接打开 `views/console/index.html?mock=1`（或 `file://` 打开），在无 AstrBot 后端时页面应能渲染全部 8 个页面并完成主要交互（Mock bridge 覆盖全部 API）。v2.6 Mock 已同步：models 带 `inherit/inherit_from` 且关闭继承即停用、users 带 `attitude`、`action_types`/时间块带固定 `armor`/`armor_tier`、角色卡 schema 为 10 分区 / 36 字段、`identity.avatar` 含一个极小 `data:image/png;base64` 示例、记忆列表/统计结构保持 `memories/list` 原形状。
3. **视觉验收**：浅色 / 深色主题各截图一次；确认无 emoji 图标、无长副标题、悬浮提示可读、主操作按钮唯一。
4. **接口一致性**：页面调用的 endpoint 必须全部出现在 §五 的 27 条路由中；新增路由需同步 `core/web_api.py` 的 `register_routes()` 与本文档。
