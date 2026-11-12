# OnlyYou (只有你) —— Plugin Pages 控制台设计方案 (v2.8)

| 文档版本 | 适用机制 | 页面入口路径 | 通信桥接对象 |
| :--- | :--- | :--- | :--- |
| v2.8（当前） | AstrBot Plugin Pages (`views/`) | `views/console/index.html` | `window.AstrBotPluginPage`（兼容 `window.AstrBotPluginView`）+ `context.register_web_api` |
| v2.7 / v2.6 | 同上 | 同上 | 同上（历史版本，保留于本表） |

> 本文档描述 v2.8 的控制台设计：**48px 顶栏 + 216px 侧栏 + 8 个页面**的冷静极简风格（灰白 + 青绿），页面围绕 **24h 动态弹性时间轴 / 四类行动卡编辑器 / 繁忙进度条与今日繁忙成果** 组织，以及基于 `core/web_api.py` 的 **31 条** RESTful 路由契约。v2.8 变更：**关系页重写为「关系与群聊」**（筛选 chips / 搜索 / 分组 relation-row / 居中宽幅 modal / 群聊分区与详情弹窗，移除旧 kanban + 拖拽 + ⋯ 菜单）；**会话页重写为「机器人接口」**（接口绑定/解绑并清理平台内存、手动预配 `platform_id`、默认兜底角色、活跃会话内存概览，移除旧 UMO 绑定表）；模型页按当前角色读写；记忆页 T1 倒计时/上限；冲浪页新增赛博冲浪总开关；新增 4 条 `sessions/*` API 并扩展多条既有路由。v2.7 的页面 isDirty 守卫、模型页 0.7s 防抖自动保存、顶栏 tooltip 继续有效。

---

## 一、 为什么使用 AstrBot Pages 而不是普通 `_conf_schema.json`？

AstrBot 默认的 `_conf_schema.json` 只适合静态、扁平的基础键值对配置；而 **OnlyYou (`astrbot_plugin_only_you`)** 的核心数据是动态演化的角色生命体数据，且模型选择需要「提供商源 + 模型名」两级联动，静态表单无法承载：

1. **详细角色卡（10 分区 × 36 字段）**：需要按 schema 动态生成表单，支持标签输入、对话对编辑、AI 整卡生成 / 单分区补全、Prompt 预览、图片头像上传（v2.6）。
2. **角色专属四类行动卡池**：每个角色拥有 8~12 张 `SLEEP` / `SURF` / `IDLE_ROAM` / `BUSY_TASK` 行动卡（含 `min_hours ~ max_hours` 时长区间），需要卡片网格编辑与 AI 生成；不再有奇遇卡池。
3. **今日三态日程实况**：需要 24 小时时间轴、当前时段进度环形表与手动切换行动类型（v2.6 起护甲不再展示/可调，按行动类型固定档位）。
4. **四角色模型配置**：需要选择 AstrBot 提供商源、拉取模型名、测试连通性、试听 TTS——这些都需要联网交互，不能写死为配置项。
5. **三级关系 / 分级记忆 / 冲浪视频池 / 群聊档案**：需要筛选、搜索、弹窗编辑、提炼与 GC 等交互式管理；v2.8 关系页为「筛选 chips + 搜索 + 分组横向列表 + 居中宽幅详情弹窗 + 群聊分区/详情弹窗」，不再使用三列看板与拖拽。
6. **机器人接口绑定（v2.8）**：需要把 AstrBot 的机器人接口（`platform_id`）绑定到具体角色、解绑时清理该平台内存、手动预配接口、设置默认兜底角色、查看/清空单会话与整平台的内存——这些是运行时状态感知操作，静态配置表单无法承载。

### 配置职责切分（三层）

| 层 | 位置 | 职责 |
| :--- | :--- | :--- |
| **交互式管理** | `views/console/`（本页） | 角色卡、行动卡池、今日日程、用户关系与群聊档案、分级记忆、冲浪视频池 / 总开关 / 后台视频源、**四角色模型配置（按当前角色）**、机器人接口绑定与活跃会话内存 |
| **运行参数** | `_conf_schema.json`（AstrBot 插件配置面板） | Roguelite 阈值、读空气、冲浪接口与分享门槛/冷却、记忆与上下文预算（含 `max_tier1_memories`）等**静态参数**。用户等级 / 名单、按角色冲浪总开关与后台视频源由 Pages 管理；v2.4 起**不再包含任何模型绑定** |
| **Soul 独立文件** | `souls/<安全文件名>.json`（v2.8 唯一真源） | 角色卡 10 分区、四类行动卡池、`model_config`、`bound_platforms`、`settings`（至少 `surfing_enabled`；v2.8.3 可选 `surfing_sources`）、`revision`、`updated_at`。SQLite `souls` 仅索引 |
| **插件自治设置** | SQLite `plugin_settings` 表 | `default_soul_id`（默认兜底角色）、`runtime_state_v1`（运行态快照）、`group_profiles`（群聊档案）、两个一次性迁移 flag、`last_forgetting_gc_date`、v2.7 兼容的全局 `model_config`。不放进 AstrBotConfig，避免 schema 完整性检查删键 |

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
- **列表 / 网格**：角色卡、记忆、冲浪使用卡片网格；**关系页（v2.8）使用筛选 chips + 分组的 `.relation-row` 横向列表 + 💬 群聊卡片行**，不再使用看板 / 拖拽 / 悬停「⋯」菜单。
- **表单**：分区卡片式布局；下拉用原生 `select` 美化；标签类用胶囊输入（逗号 / 顿号 / 斜杠分隔）；对话对用「用户 / 角色」两栏编辑器。
- **AI 草稿**：改动字段用青绿描边高亮，顶部提供「采用草稿 / 放弃草稿」，未保存时底部固定保存栏显示「未保存修改」。
- **抽屉**用于编辑单个时段、单张视频；**弹窗**用于确认、AI 生成、Prompt 预览、试听。**v2.8 新增宽幅弹窗**：`app.modal({ title, body, actions, wide: true })` 生成 960px 级居中弹窗（`.is-wide`），供关系页「用户详情」与「群聊详情」使用。
- **未保存修改守卫（v2.7，v2.8 沿用）**：有本地编辑态的页面在 `window.OY_PAGES.<id>.isDirty()` 暴露状态；`app.js` 在切换菜单 / 切换角色 / `beforeunload` 时弹确认。纯展示页或无本地编辑态的页面（如 v2.8 关系页）可不实现。
- **危险操作**（解绑机器人接口、清空会话/平台内存、移除群聊记录、删除用户/角色、GC）一律二次确认，并明确列出「清理 vs 保留」范围。

---

## 三、 页面清单与设计要点

| 页面 | 侧栏标题 / 页内标题 | 路由 | 侧栏分组 | 主要接口 |
| :--- | :--- | :--- | :--- | :--- |
| 概览 | 概览 | `#/overview` | 运行 | `dashboard/overview` |
| 日程 | 日程 | `#/schedule` | 运行 | `souls/detail`、`cards/*`、`schedule/*` |
| 角色卡 | 角色卡 | `#/character` | 角色 | `card/*`、`souls/*` |
| 关系 | 关系（页内「关系与群聊」） | `#/relations` | — | `users/list`（含 `groups`）、`users/update`（含群档案 action） |
| 记忆 | 记忆 | `#/memory` | — | `memories/list`（含 `tier1_limit`）、`memories/mutate` |
| 冲浪 | 冲浪 | `#/surfing` | — | `surfing/list_and_trigger`（含 `toggle_surfing` / `set_surfing_sources`） |
| 模型 | 模型 | `#/models` | 系统 | `models/*`（按当前角色带 `soul_id`） |
| 会话 | 会话（页内「机器人接口」） | `#/sessions` | 系统 | `sessions/platforms`、`sessions/bind_platform`、`sessions/unbind_platform`、`sessions/clear_memory`、`sessions/default_soul` |

> 侧栏标题沿用 `PAGE_META` 的短名（「关系」「会话」）；页内标题在 v2.8 改为「关系与群聊」「机器人接口」。切换顶部「当前角色」会重渲染当前页并按新的 `soul_id` 重新拉取数据。

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

### 3.4 关系与群聊（`page-relations.js`，v2.8 重写）

> **v2.8 重设计**：废弃 v2.6 的「三列看板 + HTML5 拖拽改级 + 编辑抽屉 + 「⋯」小菜单」，改为「筛选 chips + 即时搜索 + 分组横向列表 + 居中宽幅详情弹窗 + 💬 群聊分区」。数据源仍为 `users/list`（v2.8 新增 `groups`）与 `users/update`（v2.8 新增群档案 action）。

1. **页头**：标题「关系与群聊」+ 副标题（关系条数与群聊数）；右侧「刷新」按钮。数据加载中显示 spinner「加载关系与群聊…」。
2. **筛选 chips**：`[全部] [★L3 核心 (n)] [◆L2 重要 (n)] [L1 普通 (n)] [💬 群聊 (n)]`——点击切换列表内容；选「💬 群聊」时只显示群聊分区，选「全部」时用户区之后显示群聊分区。计数来自 `users/list` 的 `counts` 与 `groups.length`。
3. **即时搜索**：占位 `🔍 搜索用户名称、昵称、UID、平台账号、专属态度或群号...`，前端即时过滤（用户匹配名称 / 昵称 / `global_uid` / `platform_ids` / `attitude`；群聊匹配群名 / `group_id` / `umo` / `platform_id` / 备注 / 绑定角色）。空结果渲染空状态 + 「清除搜索」。
4. **用户区**：按 `★L3 → ◆L2 → L1` 分组，每组标题带等级色与计数；每行 `.relation-row`（横向，整行 `role="button"`）：头像 + 显示名/昵称 + 等级徽章 + 平台标签 + 好感度进度条（★L3 显示「管理员级 · 不参与好感度计算」）+ 互动次数 + 专属态度摘要。**点击整行打开详情弹窗**；不再拖拽、不再有列内「⋯」菜单。
5. **用户详情弹窗（居中宽幅 `app.modal({ wide: true })`）**：
   - 身份头：头像、`global_uid`、等级徽章、互动次数、`platform_ids` 提取的平台标签；
   - 「显示名称」/「角色对 Ta 的专属称呼」输入；
   - 关系等级三选一（★L3 / ◆L2 / L1），切到 ★L3 时醒目提示「★L3 为管理员级，不参与好感度计算与自动晋升」；L1/L2 显示可编辑好感度（0~100），★L3 置灰「管理员级无需好感度」；
   - 「专属态度」textarea（`maxlength=300`，说明会注入 System Prompt；placeholder 例如「把 Ta 当成最信任的损友，说话可以毒舌一点但关键时刻很护短」）；
   - 平台账号编辑（逗号/换行分隔的 `platform:uid`）+「合并其他用户账号到此档案」（`users/update action="merge"`，`primary_uid` / `secondary_uid`）；
   - 底部：左「删除用户」（危险二次确认，`users/update action="delete"`）、右「取消」「保存修改」（`users/update` 默认 update，提交 `display_name / nickname / tier / affinity / attitude / platform_ids`）。
6. **★L3 = 管理员级（v2.7 契约在 v2.8 继续有效）**：L3 不通过好感度产生（好感度自动晋升只到 L2），也不展示好感度。入口 ① `admins_id` 自动确定（普通配置不再维护用户等级名单）；② 用户详情弹窗显式设为 L3（二次确认，`users/update` 写入 `tier=3`），或 `/onlyyou tier <uid> 3`。
7. **群聊分区（v2.8 新增，数据来自 `users/list.data.groups`）**：
   - 横向卡片行：群名 + `💬 群聊` 标签、`platform_id`、`滑窗 N 条 · 未读 M`、`绑定:{soul_name}`、最近活跃时间、备注、`group_id`、详情入口；
   - 点击打开**群聊详情弹窗**：`group_id`、完整 `umo`、`platform_id`、管辖 `soul_id`、滑窗/未读条数；可编辑「群显示名称」「群备注」（`users/update action="update_group"`）；「清空该群内存滑窗与未读」（`action="clear_group_memory"`，二次确认）、「移除群聊记录」（`action="delete_group"`，二次确认）。
8. **文案口径**：清空群内存只清**内存滑窗 / 未读**，不删记忆 / 用户档案 / 群档案；「移除群聊记录」才会删群档案（群下次来消息时重新采集）。

### 3.5 记忆（`page-memory.js`，v2.6 分层结构化列表 + v2.8 T1 规则）

废弃原 `.memory-grid` 多列卡片矩阵，改为「顶部层级概览条 + 按 Tier 分组的结构化横排列表」，保留全部既有能力（新增 / 编辑 / 删除 / 提炼 / GC / 搜索防抖 / 关联用户筛选 / 刷新 / 注入预算提示 / 空态）：

1. **层级分布概览条 `.memory-summary-strip`**：全部 / T3 核心 / T2 近期 / T1 瞬时 计数 + 彩色分段占比条；点击即切换单层筛选（与工具栏筛选合并为一个，不重复）。
2. **分组列表 `.memory-group`**：默认按 `T3 → T2 → T1` 纵向排列，分组头显示图标标题 + 计数 + 说明（T3 永久保留免疫 GC / T2 按活力衰减可晋升 / T1 48 小时过期），点击可折叠展开；空分组渲染 `.memory-group-empty` 并默认收起。
3. **结构化记忆行 `.memory-row`**（`grid-template-columns: 76px minmax(0,1fr) 190px auto`，窄屏响应式折行）：列1 `T3/T2/T1` 徽章 + 重要度；列2 正文 + `#关键词` 胶囊；列3 关联用户标签 + 活力条（T3 显示「永久保存」）+ 召回次数/时间；列4 仅显示**当前层级以外**的升降级按钮 + 编辑 + 删除。
4. **工具栏**：关联用户下拉（`users/list`）、关键词搜索（400ms 防抖）、上下文注入预算提示（v2.8.2 在提示条追加「前往 AstrBot 插件配置修改预算」引导，避免用户找不到预算/滑窗修改入口），以及页头 **提炼**（`memories/mutate`，`action="extract"`，对最近滑窗执行一次决策模型记忆提炼）、**GC**（`action="gc"`，手动触发遗忘清理）、**刷新**、**新增记忆**（`action="add"`）。数据仍来自 `memories/list`（`{memories, stats}`）。
5. **T1 行显示（v2.8）**：T1 不再显示活力百分比条，改为 `⏳ 剩余 Xh 清除`（`X = max(0, 48 - (now-created_at)/3600)`，悬浮提示「Tier 1 瞬时记忆创建后 48 小时过期，剩余约 X 小时由遗忘清理 GC 删除」）；T1 分组头与工具栏提示「上限 N 条」（`N = data.tier1_limit`，来自 `memory_config.max_tier1_memories`，默认 30）。T2 保持活力条与到期衰减说明，T3 显示「永久保存」。

### 3.6 冲浪（`page-surfing.js`，v2.8.3 总开关 + 后台视频源）

1. **顶部「赛博冲浪总开关」（v2.8）**：switch 绑定当前角色 `settings.surfing_enabled`（GET `surfing/list_and_trigger` 返回）；切换时**乐观更新 UI + 失败回滚**，POST `action="toggle_surfing"`（body `{soul_id, enabled}`）。关闭时顶部显示说明「已关闭：SURF 时段将自动降级为空闲闲逛，不再主动刷视频」；手动冲浪按钮保留，后端关闭时返回错误则 toast 展示。
2. **「后台视频源」B站 / 抖音开关（v2.8.3）**：同属总开关卡片，绑定当前角色 `settings.surfing_sources`（GET 返回 `surfing_sources`）；POST `action="set_surfing_sources"`（body `{soul_id, sources}`），乐观更新 + 失败回滚；至少保留一个源（后端拒绝空列表）。仅影响 SURF 心跳自动抓取，手动触发仍可用工具栏「平台筛选」显式指定平台。
3. 视频卡片墙：封面、评分徽章、标题、UP 主、AI 吐槽文案；分享与删除收进卡片悬浮菜单。
4. 工具栏：立即刷一次 B 站 / 抖音（`surfing/list_and_trigger`，`action="surf"`，可勾选忽略状态门禁用于调试；平台筛选会覆盖后台视频源偏好）。
5. 分享弹窗：选择目标会话（`known_umos`）后 `action="share"`；删除走 `action="delete"`。

### 3.7 模型（`page-models.js`，v2.8 按当前角色）

0. **顶栏控件提示**：顶栏（当前角色 / 刷新 / 主题切换）贴近视口顶部，其 `[data-tip]` 气泡统一向下弹出、右侧操作按钮右对齐，避免被视口裁切。
1. **按当前角色读写（v2.8）**：页面顶部显示当前角色名；加载走 `Promise.all([models/sources, models/config?soul_id=当前角色])`；保存 / 自动保存 body 增加 `soul_id`，保存后按响应 `data.soul_id` 回填；切换顶部角色后 `app.js` 重渲染本页并按新角色重新拉取配置。
2. **4 张角色卡片**：**对话（main，必填）/ 决策（decision，读空气）/ 视频解析（vision）/ 语音 TTS（tts）**。
3. 每张卡片：提供商源下拉（`models/sources`）、模型名输入 + 候选列表（`models/list`，可远程拉取）、v2.6「**模型来源模式**」下拉（decision：独立配置 / 继承自主对话模型 `main`；vision：独立配置 / 继承 `main` / 继承自轻量决策模型 `decision`）、状态灯（未配置 / 继承自父角色·来源快照 / 复用实例 / 插件自建）、「测试」按钮（`models/test`，显示耗时与回复）。选择「独立配置」时展开自己的源 + 模型选择器（允许留空 = 该角色停用，**不再回退 main**）；选择继承时折叠独立选择器并展示父角色当前 `source_id / model` 快照，未配置则提示「未配置，该角色将停用」。状态徽章按后端 `status.configured / inherited / inherit_from` 渲染。
4. **TTS 卡片**：来源分段（关闭 / 提供商源 / AstrBot TTS）、模型名、音色、触发概率滑条、生效范围（私聊 / L3 / 全部）、「语音 + 文字双发」开关、「试听」按钮（`models/test` 返回 base64，用 `<audio>` 播放）。
5. **即时生效（v2.7，v2.8 沿用）**：任何模型配置修改（来源模式 / 提供商源 / 模型名 / TTS 各项）停止输入约 0.7s 后自动 `POST models/config`（v2.8 写入**该角色 Soul JSON 的 `model_config`** 并热重建该角色实例），状态灯即时刷新。**已移除底部固定保存条**——无需手动保存，`Ctrl/Cmd + S` 仍可强制立即保存（无按钮）；保存 payload 必须带 `inherit` 与 `inherit_from`（保持 `{model_config: {...}}` 形状不变）；主模型未配置时页面顶部红字提示；本页实现 `isDirty()` 供路由守卫使用。

### 3.8 机器人接口（`page-sessions.js`，v2.8 重写）

> **v2.8 重设计**：从「默认角色 + UMO 绑定表」改为「当前角色的机器人接口管理」。数据 `GET sessions/platforms?soul_id=当前角色` → `{ current_soul_id, default_soul_id, souls, platforms, sessions }`。删除旧 UMO 绑定表 UI（后端 UMO binding API 仅兼容保留）。

1. **区块 A —— 机器人接口列表**（表格：接口 / 类型 / 运行状态 / 活跃会话 / 绑定状态 / 操作）：
   - **已绑定当前角色** → 绿色「已绑定当前角色」标签 + 「解绑（清理该平台内存滑窗）」按钮（POST `sessions/unbind_platform {platform_id}`，二次确认后 toast 展示清理统计并刷新）；
   - **已被其他角色占用** → 灰色「已被角色「XX」占用」标签，按钮禁用（`data-tip` 提示「一个机器人接口同一时刻只能绑定一个角色，不可跨角色共用」）；
   - **未绑定** → 「绑定到当前角色」按钮（POST `sessions/bind_platform {soul_id, platform_id}`）。互斥冲突时后端 `message` 精确为「该机器人接口已被角色『X』绑定，请先在角色 X 下解绑」，页面直接 toast。
   - 接口来源：AstrBot 运行时实例 / 配置条目 / 历史 UMO 观察推断；未探测到接口时显示空状态，提示可通过「手动绑定」预配。
2. **区块 B —— 手动绑定**：输入 `platform_id`（取 UMO 第一段，如 `aiocqhttp:GroupMessage:123` 的 `aiocqhttp`；不能含冒号、长度 ≤64）+ 「绑定到当前角色」。
3. **区块 C —— 默认兜底角色**：把当前角色设为 `plugin_settings.default_soul_id`（POST `sessions/default_soul`）；未绑定任何接口的会话由它接管。
4. **区块 D —— 活跃会话内存概览**（`sessions`）：UMO、群/私聊、滑窗条数、所属平台、是否 ★L3 私聊、最近活跃；每行「清空」单会话（POST `sessions/clear_memory {umo}`），平台分组标题处「清空全部」整平台（`{platform_id}`），均二次确认。
5. **文案口径**：解绑 / 清空只清**内存会话滑窗与未读队列**（Web API 响应的 `cleared` 统一为 `{windows, evicted, unread, messages}` 四个计数；`main.clear_platform_memory_state()` 内部还会清 `groups / l3 / caches` 缓存，但不单独返回），**不删**持久化 `memories / users / surfing_history / group_profiles`；重启后未读队列本身不保留（既定语义）。
6. **路由优先级说明（v2.8）**：页面提示当前路由顺序为 **机器人接口绑定 → 旧 UMO 绑定（兼容）→ 默认兜底角色 → 最近更新的角色 → 自动创建默认角色**。

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
              defaultSoulId, nativePersonas, platformBindings, umoBindings, knownUmos,
              activeSchedule }
app.icon(name, size)                    // 取 icons.js 的 SVG 字符串
app.toast(msg, isError)
app.modal({ title, body, actions, wide }) / app.closeModal()   // v2.8: wide=true -> 居中宽幅 .is-wide
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
  async destroy() {},
  isDirty() { return false; }   // 可选：有本地编辑态的页面返回未保存状态（v2.7 起）
};
```

> **v2.8 页面数据约定**：页面渲染以 `app.state.currentSoulId` 为当前角色；切换角色后 `app.js` 重渲染当前页，页面 `render()` 必须按新的 `currentSoulId` 重新拉取数据（模型页 / 会话页 / 冲浪页 / 记忆页均如此）。关系页与群聊数据不区分角色（全局用户与群档案），但群档案的 `bound_soul_id` 由接口绑定推导。

### 4.4 图标与图表模块

- **`icons.js`**：`window.OY_ICONS = { gauge, calendar, character, relations, memory, surf, models, sessions, refresh, moon, sun, plus, trash, edit, check, close, info, wand, play, save, chevron, ... }`（≥20 个）；`window.icon(name, size = 16)` 返回内联 SVG 字符串（`stroke="currentColor"`）。
- **`charts.js`**：`window.OYCharts` 提供纯 SVG、无依赖的图表，全部返回 HTML 字符串由页面 `innerHTML` 注入：
  - `ringGauge({ value, max, label, sublabel, color, size })`：通用环形表（v2.6 概览页用于「当前时间块进度」，不再展示护甲）；
  - `scheduleAxis({ slots, currentKey, nowHour, clickable })`：24h 时段色块 + 现在指针，tooltip 显示行动类型与进度（v2.6 删除「护甲 X」行）；
  - `donut({ items, size, centerValue, centerLabel })`：关系环形图；
  - `bars({ items })`：记忆分级条形图；
  - `dots({ used, max })`：微调配额 ●●○。

---

## 五、 后端 Web API 契约（31 条）

所有路由前缀为 `/astrbot_plugin_only_you`，由 `main.py` 初始化时调用 `self.web_api.register_routes()` 注册（`context.register_web_api`；日志 `[OnlyYou] Registered 31 Web API routes for Plugin Pages.`）。统一响应信封：

```json
{ "status": "ok | error", "message": "提示文案", "data": { } }
```

前端 `apiGet/apiPost` 已自动解包；`status="error"` 时 `data` 通常为空、`message` 为错误原因（v2.8 新增 `sessions/bind_platform` 的互斥冲突等业务错误也走这个信封）。

> **v2.8 计数口径**：§5.1 的 17 条（v2.5 兼容路由）+ §5.2 的 10 条（v2.4 Pages 路由）+ §5.3 的 4 条（v2.8 机器人接口路由）= **31 条**。§5.4 汇总 v2.8 对既有路由的扩展参数。

### 5.1 现有 17 条路由（v2.5 保留兼容；v2.8 扩展见 §5.4）

| # | 路由（前缀后） | 方法 | 入参 | `data` 返回 |
| :--- | :--- | :--- | :--- | :--- |
| 1 | `init_summary` | GET | - | `{ souls, native_personas, umo_bindings, platform_bindings, known_umos, active_soul_id, active_schedule, default_soul_id, model_status, action_types }`；**v2.8 新增 `platform_bindings: {platform_id: soul_id}`**；`souls` 每项为 `SoulProfile.to_dict()`（自带 `model_config / bound_platforms / settings / revision / updated_at`），`model_status` 为四角色状态（`main/decision/vision/tts`），`action_types` 为四类行动定义 |
| 2 | `souls/detail` | GET | `?soul_id=` | `{ soul, card_schema, schedule, umo_bindings, platform_bindings, known_umos }`（v2.8 新增 `platform_bindings`） |
| 3 | `souls/import_persona` | POST | `{ persona_name, target_soul_id?, auto_generate_cards?, ai_generate_card?, native_prompt?, avatar? }`（v2.6 `avatar` 为可选图片 Data URL / URL / Emoji） | `{ soul, schedule, souls }` |
| 4 | `souls/save` | POST | `{ soul_id, card?, umo_binding?, model_config?, bound_platforms?, settings? }`（也兼容旧扁平字段） | `{ soul, souls, umo_bindings, known_umos, storage }`；`storage = {file_written, degraded, error}`，JSON 写失败时 `degraded=true` 且 `message` 追加中文告警（v2.8） |
| 5 | `souls/rename` | POST | `{ old_soul_id, new_soul_id }` | `{ soul, souls, schedule }`，级联更新关联表 |
| 6 | `souls/delete` | POST | `{ soul_id }` | `{ deleted_soul_id, souls, active_soul_id }` |
| 7 | `schedule/reroll` | POST | `{ soul_id? }` | `{ schedule }`（`run/current_slot/next_slot/chronotype/busy_results/配额与冷却`；重新随机拼接 24h 动态时间轴） |
| 8 | `schedule/force_state` | POST | `{ soul_id?, slot_key?, action_type?(SLEEP/SURF/IDLE_ROAM/BUSY_TASK), new_state?(RESTING/BUSY/IDLE，兼容旧入参), new_title? }`（v2.6 `armor` 入参已废弃，传了也会被忽略） | `{ updated_slot, schedule }`；`action_type` 优先并自动绑定 state，`updated_slot.armor` 为固定档位 |
| 9 | `cards/list` | GET | `?soul_id=` | `{ soul_id, custom_cards, general_cards, action_types, chronotype }`；卡片含 `action_type/min_hours/max_hours/armor/base_weight`，其中 `armor` 恒为 action_type 固定档位；`action_types` 每项含 `armor` / `armor_tier` |
| 10 | `cards/save` | POST | `{ soul_id?, card_type:"action", scope:"soul"\|"global", card }`（card 含 `action_type/min_hours/max_hours`；v2.6 传 `armor` 会被固定档位覆盖；encounter 类型已移除并返回错误） | 同 `cards/list` 的卡片池结构 |
| 11 | `cards/delete` | POST | `{ soul_id?, card_type:"action", scope, card_id }` | 同 `cards/list` 的卡片池结构 |
| 12 | `cards/ai_generate` | POST | `{ soul_id?, auto_save? }` | 卡片池结构 + `{ generated_cards }`（决策模型生成 8~12 张四类专属行动卡；v2.6 Prompt 不再要求 armor，落库时按固定档位写入） |
| 13 | `users/list` | GET | `?tier=1\|2\|3` | `{ users, counts, groups }`；`users` 每项含 `global_uid/display_name/nickname/attitude/tier/affinity/platform_ids` 等（v2.6 新增 `attitude`）；**v2.8 新增 `groups: [GroupItem]`（见 §5.4）** |
| 14 | `users/update` | POST | `{ action:"update"\|"merge"\|"delete"\|"update_group"\|"clear_group_memory"\|"delete_group", ... }`（v2.8 群档案 action body `{umo, display_name?, notes?}`） | update：`{ user, users }`；merge：`{ merged_user, users }`；delete：`{ users }`；**v2.8 群档案 action：`{ groups, counts }`**；v2.6 `attitude` 去空白后截断 300 字持久化 |
| 15 | `memories/list` | GET | `?soul_id&memory_tier=1\|2\|3&user_id&keyword` | `{ memories, stats, tier1_limit }`；`stats` 含 `tier3_count/tier2_count/tier1_count/total` 与上下文预算参数；**v2.8 新增 `tier1_limit`（默认 30，来自 `memory_and_context_config.max_tier1_memories`）** |
| 16 | `memories/mutate` | POST | `{ action:"add"\|"edit"\|"delete"\|"pin_core"\|"extract"\|"gc", soul_id, id?, content?, keywords?, importance?, memory_tier?, vitality?, user_id?, umo? }` | 各 action 返回对应结果（`memories` / `memory` / `gc_stats` / `extracted_count` 等） |
| 17 | `surfing/list_and_trigger` | GET / POST | GET：`?soul_id=`；POST：`{ action:"surf"\|"share"\|"delete"\|"toggle_surfing"\|"set_surfing_sources", soul_id?, enabled?, sources?, ... }` | GET：`{ videos, known_umos, surfing_enabled, surfing_sources, soul_id }`（v2.8 新增总开关，v2.8.3 新增后台视频源）；surf：`{ review, videos, schedule }`；share：`{ sent, target_umo, share_text }`；delete：`{ videos }`；**v2.8 `toggle_surfing`：`{ soul_id, surfing_enabled, soul }`；v2.8.3 `set_surfing_sources`：`{ soul_id, surfing_sources, soul }`** |

### 5.2 Pages 路由 10 条（v2.4 引入；v2.8 扩展见 §5.4）

| # | 路由（前缀后） | 方法 | 入参 | `data` 返回 |
| :--- | :--- | :--- | :--- | :--- |
| 18 | `dashboard/overview` | GET | `?soul_id=` | `{ soul, vigor, state, schedule, quota, encounters(空数组兼容), busy_results, counts, models }`（见下；进入前会惰性结算已结束繁忙块） |
| 19 | `card/schema` | GET | `?soul_id=` | `{ schema: CARD_SCHEMA, card, soul_id }` |
| 20 | `card/save` | POST | `{ soul_id, card }` | `{ soul, souls }`（`normalize_card` 归一化后持久化） |
| 21 | `card/ai_generate` | POST | `{ soul_id?, seed_text?, section? }` | `{ draft_card, section }`（**不落库**；整卡或单分区草稿） |
| 22 | `card/preview_prompt` | POST | `{ soul_id? \| card?, max_chars? }` | `{ prompt, chars }`（渲染后的人格 Prompt 与字数） |
| 23 | `models/sources` | GET | - | `{ sources, tts_providers }`；`sources` 项为 `{id,type,provider_type,provider,api_base,has_key,enable}`，**绝不含 key** |
| 24 | `models/list` | GET | `?source_id=` | `{ source_id, models }`；`models` 项为 `{model,id,enable,modalities,source:"config"\|"remote"}` |
| 25 | `models/config` | GET / POST | GET：`?soul_id=`；POST：`{ soul_id?, model_config }`（或直接顶层 `{main,decision,vision,tts}`；decision / vision 需带 `inherit` 与 `inherit_from`） | `{ soul_id, model_config, status }`（v2.8 新增 `soul_id`）；`model_config` 为归一化后的四键结构，`status` 每个角色含 `configured/inherited/inherit/inherit_from/source_id/model/provider_id/mode/label/ok`；v2.8 保存到该 Soul JSON 并热重建该角色实例 |
| 26 | `models/test` | POST | `{ role: "main"\|"decision"\|"vision"\|"tts", soul_id? }`（v2.8 可选 `soul_id`） | 聊天角色：`{ status, role, provider_id, model, latency_ms, reply }`；TTS：`{ status, role, latency_ms, audio_path, audio_base64 }` |
| 27 | `sessions/default_soul` | POST | `{ soul_id }` | `{ default_soul_id, souls, umo_bindings }`；v2.8 语义 = 设置**默认兜底角色**（未绑定机器人接口的会话由它接管） |

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

### 5.3 v2.8 新增 4 条路由（机器人接口 / 会话内存）

> 全部沿用统一信封 `{status, message, data}`；`status="error"` 时 `message` 为可直接展示的中文错误。绑定互斥冲突 message 精确为：`该机器人接口已被角色『{name}』绑定，请先在角色 {name} 下解绑`。

#### 28）`GET sessions/platforms`

| 项 | 内容 |
| :--- | :--- |
| 入参 | `?soul_id=`（可选；缺省 = 默认角色） |
| `data` | `{ current_soul_id, default_soul_id, souls, platforms, sessions }` |
| 用途 | 机器人接口页一次拉齐「接口列表 + 绑定关系 + 当前角色管辖的活跃会话内存概览」 |

```json
{
  "current_soul_id": "酒狐",
  "default_soul_id": "酒狐",
  "souls": [{"soul_id": "酒狐", "name": "酒狐", "bound_platforms": ["aiocqhttp"]}],
  "platforms": [{
    "platform_id": "aiocqhttp", "platform_type": "aiocqhttp", "name": "aiocqhttp",
    "enabled": true, "running": true, "source": "runtime",
    "bound_soul_id": "酒狐", "bound_soul_name": "酒狐", "session_count": 3
  }],
  "sessions": [{
    "umo": "aiocqhttp:GroupMessage:123456", "kind": "group", "platform_id": "aiocqhttp",
    "bound_soul_id": "酒狐", "window_messages": 15, "is_l3_private": false,
    "last_active_at": 1762840000
  }]
}
```

- `platforms` 由 `PlatformBridge.list_platform_instances(observed_umos)` 的运行时 / 配置 / 观察来源合并，再叠加 Soul 绑定关系与活跃会话数；`source ∈ {"runtime","config","observed"}`。
- `sessions` 只包含**当前角色管辖**的 UMO：有绑定时过滤 `platform_id ∈ bound_platforms`；无绑定的默认兜底角色只显示未被其他角色占用的平台会话；排序 `last_active_at DESC`。

#### 29）`POST sessions/bind_platform`

| 项 | 内容 |
| :--- | :--- |
| 入参 | `{ soul_id, platform_id }`（`platform_id` 非空、不含 `:`、长度 ≤64） |
| `data` | `{ soul_id, platform_id, souls, platforms }` |
| `message` | 成功：`已将机器人接口 {platform_id} 绑定到角色 {name}`；冲突：`该机器人接口已被角色『{name}』绑定，请先在角色 {name} 下解绑` |
| 互斥 | 同一接口同一时刻只属于一个角色；已绑定当前角色重复提交为幂等成功 |

#### 30）`POST sessions/unbind_platform`

| 项 | 内容 |
| :--- | :--- |
| 入参 | `{ platform_id, soul_id? }`（`soul_id` 省略则按当前 owner 解绑） |
| `data` | `{ platform_id, previous_soul_id, cleared, souls, platforms }` |
| `cleared` | `{ windows, evicted, unread, messages }`（Web API 统一四键；`main.clear_platform_memory_state()` 内部另清 `groups / l3 / caches` 缓存） |
| 副作用 | 解绑后同步调用 `plugin.clear_platform_memory_state(platform_id)`；**不删** SQLite `memories / users / surfing_history / group_profiles` |

#### 31）`POST sessions/clear_memory`

| 项 | 内容 |
| :--- | :--- |
| 入参 | `{ umo }`（清单会话）或 `{ platform_id }`（清整平台）；可带 `soul_id?` 用于返回该角色的会话列表 |
| `data` | `{ cleared, sessions }` |
| `cleared` | 同路由 30；单会话清理返回 `{windows, evicted, unread, messages}` 口径 |
| 用途 | 会话页「单会话清空」/「整平台清空」；只清内存滑窗与未读 |

### 5.4 v2.8 既有路由扩展参数汇总

| 路由 | v2.8 扩展 | 说明 |
| :--- | :--- | :--- |
| `init_summary` | `platform_bindings` | `{platform_id: soul_id}`；`souls[]` 自带 Soul JSON 字段 |
| `souls/detail` | `platform_bindings` | 同上 |
| `souls/save` | `model_config / bound_platforms / settings`（可选透传）+ `data.storage` | 用于兼容旧页面的 Soul 级配置保存；`storage` 显式暴露 JSON 写降级 |
| `users/list` | `groups` | `GroupItem = {umo, group_id, platform_id, display_name, notes, bound_soul_id, bound_soul_name, window_count, unread_count, last_active_at}` |
| `users/update` | `update_group / clear_group_memory / delete_group` | `update_group` 改群名/备注；`clear_group_memory` 只清该群内存；`delete_group` 删群档案。三者均返回 `{groups, counts}` |
| `memories/list` | `tier1_limit` | 默认 30，来自 `memory_and_context_config.max_tier1_memories`；记忆页 T1「上限 N 条」 |
| `surfing/list_and_trigger` | GET `surfing_enabled / surfing_sources / soul_id`；POST `action="toggle_surfing" / "set_surfing_sources"` | 总开关与后台视频源读写；总开关关闭时 SURF 时段降级 IDLE_ROAM |
| `models/config` | `?soul_id=` / body `{soul_id, model_config}`；data `soul_id` | 按当前角色读写四角色配置 |
| `models/test` | body 可选 `soul_id` | 测试当前角色的指定模型角色 |
| `souls/delete` / `souls/rename` | 文件名联动 | 删除 / 重命名会同步删除 / 重命名 `souls/*.json` 并级联 SQLite 索引与绑定关系 |

---

## 六、 开发与验收约定

1. **语法检查**：`node --check views/console/*.js` 全部通过后再交付。
2. **离线预览**：直接打开 `views/console/index.html?mock=1`（或 `file://` 打开），在无 AstrBot 后端时页面应能渲染全部 8 个页面并完成主要交互（Mock bridge 覆盖全部 31 条 API）。**v2.8 Mock 已同步**：
   - `sessions/platforms` 返回 `{current_soul_id, default_soul_id, souls, platforms, sessions}`，`sessions/bind_platform` / `sessions/unbind_platform` / `sessions/clear_memory` 具备内存态模拟；
   - 每个 soul 带 `model_config / bound_platforms / settings / updated_at`，`souls/save` 支持 `model_config / bound_platforms / settings` 透传；
   - `users/list` 带 `groups`（GroupItem），`users/update` 支持 `update_group / clear_group_memory / delete_group`；
   - `memories/list` 带 `tier1_limit`（30）；
   - `surfing/list_and_trigger` GET 带 `surfing_enabled / surfing_sources / soul_id`，POST 支持 `action="toggle_surfing"` / `"set_surfing_sources"`；
   - v2.6 既有 mock 形状保留：models 带 `inherit/inherit_from`、users 带 `attitude`、`action_types`/时间块带固定 `armor`/`armor_tier`、角色卡 schema 为 10 分区 / 36 字段、`identity.avatar` 含极小 `data:image/png;base64` 示例。
3. **视觉验收**：浅色 / 深色主题各截图一次；确认无 emoji 图标、无长副标题、悬浮提示可读、主操作按钮唯一；v2.8 额外检查关系页宽幅弹窗在窄屏（≤720px）下不溢出、chips 可横向滚动、群聊卡片不换行错位。
4. **交互验收（v2.8）**：
   - 关系页：chips 切换 / 搜索即时过滤 / 点击行打开宽幅弹窗 / 保存后列表即时刷新 / 群聊详情「清空群内存」与「移除群聊记录」二次确认；
   - 会话页：绑定互斥提示（他人占用按钮禁用）/ 解绑后 toast 显示清理统计 / 手动 `platform_id` 绑定 / 默认兜底角色开关 / 单会话与整平台清空；
   - 模型页：切换角色后顶部角色名与四角色配置同步刷新；自动保存 body 带 `soul_id`；
   - 记忆页：T1 行显示「⏳ 剩余 Xh 清除」与「上限 N 条」；冲浪页：总开关乐观更新 + 失败回滚、关闭提示出现。
5. **接口一致性**：页面调用的 endpoint 必须全部出现在 §五 的 **31 条**路由中；新增路由需同步 `core/web_api.py` 的 `register_routes()` 与本文档。
6. **页面未保存守卫**：任何新增带本地编辑态的页面（或在既有页面增加草稿态）必须实现 `window.OY_PAGES.<id>.isDirty()`；v2.8 关系页详情弹窗属于即时提交，不需要 `isDirty`。
7. **截图说明**：`docs/screenshots/` 已更新为 v2.8 截图（8 页 × 浅/深共 16 张，Chrome headless + `mock=1` 生成，1440×1000）。
