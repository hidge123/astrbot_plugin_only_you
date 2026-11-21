# OnlyYou 生产升级与迁移指南 (v3.0)

> 适用对象：从 **v2.6.x ~ v2.13.x** 升级到 **v3.0.0** 的 AstrBot 部署（更早版本按同一流程升级，但旧行动卡 / 日程会在 v2.9 清理规则下被重抽或丢弃）。
> 本文只写升级、迁移、校验、回滚与排障语义，不含真实服务器地址或密钥；命令中的 `$SERVER_IP`、`<数据目录>`、`/path/to/OnlyYou` 均为占位符。
> 版本口径：插件 `v3.0.0`；Web API **34 条**（含 `settings/*` 三条）；控制台 **9 页**（含「设置」）；角色卡 **10 分区 / 36 字段**。
> **版本基线**：3.0.0 起以 Major 作为配置 / 交互破坏性变更的分界；`_conf_schema.json` 退场、Pages「设置」成为唯一配置入口的契约从该版本起正式生效。从 v2.13.0 升级无额外迁移（代码一致，仅版本号不同）；从 v2.12.x 及更早升级会自动执行下表的旧配置迁移。

---

## 0. 升级会自动做什么

首次启动 / 热重载时，插件会**自动、幂等**执行以下迁移，无需手工改库：

| # | 迁移 | 触发条件 | 幂等标记 |
| :--- | :--- | :--- | :--- |
| 1 | 旧 SQLite 角色 → `souls/<soul_id>.json` 独立文件 | 索引有行、文件不存在 | 无（以文件存在为准） |
| 2 | `souls` 表补 `revision` 列 | 旧库缺列 | 无（`PRAGMA table_info` + `ALTER TABLE`） |
| 3 | 旧 UMO 绑定 → 机器人接口（`platform_id`）绑定 | 存在 `umo_soul_bindings` | `legacy_umo_bindings_migrated` |
| 4 | 旧全局 `model_config` → 各角色 `model_config` | 角色配置为空且有旧全局配置 | `model_config_migrated_to_souls` |
| 5 | 旧全局 provider id → `(source_id, model)` | 极旧配置 | `legacy_model_migrated` |
| 6 | 裸用户 ID → `platform_id:native_uid` | `users.platform_ids` 含裸 ID | `legacy_user_identity_migrated_v2` |
| 7 | `users` 关系列 → 按角色 `user_relations` | 首次启动且存在角色 | `user_relations_migrated_v1` |
| 8 | 运行态快照初始化 | 首次读取 | 无（缺省空快照） |
| 9 | `user_relations` / `users` 补 `proactive_enabled` 列（v2.11，纯加法） | 旧库缺列 | 无（幂等 `ALTER TABLE`，默认 1） |
| 10 | `memories` 补 `memory_type / event_at / embedding_json / superseded_by / last_reinforced_at`（v2.12，纯加法） | 旧库缺列 | 无（幂等 `ALTER TABLE`；旧行 `memory_type='episode'`、`event_at=0`、无向量、未归档） |
| 11 | `users` 补 `aliases_json`（v2.12，纯加法） | 旧库缺列 | 无（幂等 `ALTER TABLE`，默认 `[]`） |
| 12 | 旧 `memories.user_id` 裸平台 uid / 曾用名归并到 `global_uid`（v2.12） | 存在可唯一归属的裸 ID | `legacy_memory_user_ids_migrated_v1`（全部成功才写入；歧义行保持原样） |
| 13 | `memories` 补 `tier1_since`（v2.12，纯加法） | 旧库缺列 | 无（幂等 `ALTER TABLE`，默认 `0`；旧 T1 回退 `created_at`） |
| 14 | 旧 AstrBotConfig 非默认值 → `plugin_settings["runtime_config"]`（v3.0） | 首次启动且标记缺失 | `legacy_astrbot_config_migrated`（成功后写入；已由 Pages 保存的新值优先） |

**关键原则**：一次性 flag 只有在**全部条目迁移成功**后才写入；失败保留未写入状态并记 warning「下次启动重试」，迁移逻辑本身幂等（已迁移项跳过，不会重复插入）。

v3.0 的破坏性变化（配置系统统一）：

- **`_conf_schema.json` 已删除**。AstrBot 原生「插件配置」面板不再出现 OnlyYou 的旧配置项，
  Pages 控制台「设置」页成为唯一入口。升级后首次启动会把旧配置里的**非默认值**自动迁入
  SQLite `plugin_settings["runtime_config"]`；此前通过 Pages 保存过的值优先，不会被旧配置覆盖。
- **新增 `settings/get|save|reset` 三条 Web API**（总数 31 → 34）。保存后原地热更新配置、
  唤醒后台心跳，不需要重载插件；修改心跳 / GC 周期会立即重排后台任务节拍。
- **配置项扩容**：原代码里的 35+ 项硬编码参数（心跳周期、GC 周期、记忆字数上限、RAG 权重、
  好感增益、冲击力表、看片兜底分、冲浪历史保留上限等）全部开放到「设置」页的常规 / 高级两层。
- **回滚**：回退到 v2.13.0 即可（与 3.0.0 代码一致，仅版本号不同）；更早的 v2.12 会忽略
  `runtime_config` / 新增迁移标记并重新读取 `_conf_schema.json`（需从 Git 恢复该文件），
  角色、记忆、关系与日程数据不受影响。

v2.9 另有两点**非迁移**的行为变化：

- 旧行动卡缺少合法 `action_type` 时会被**丢弃**并记 warning（不再按标题关键词猜测类型）；卡池不足时由内置默认卡补齐四类行动。
- 不符合 24h 时间轴关键约束（首块 `0.0`、末块 `24.0`、块与块首尾相连）的旧日程会被**重抽**，不做旧格式兼容；v2.4 的「7:00 起排 + 过夜回绕」6 时段格式即属此类。

v2.10 与 v2.11 另有**非迁移**的行为变化：

- **v2.10**：非 @ 普通群消息默认先过读空气概率门闸（`reply_gate_probability=80`）；记忆注入与提炼新增时间锚点。无数据库结构变更、无 Web API 路由变化。
- **v2.11**：用户 / 群聊新增「主动回复」开关（旧数据默认开启）；`user_relations` / `users` 幂等补 `proactive_enabled` 列，群档案缺键按 `true` 处理。无破坏性迁移。
- **v2.12**：记忆系统升级为 RAG + 动态演进；主动对话默认更安静；L1→L2 与 T2→T3 门槛大幅提高。**纯加法迁移**，旧记忆 / 用户 / 关系全部保留；但以下行为变化需要知晓（详见 §11.8）。

---

## 1. 升级前检查

| 检查项 | 要求 |
| :--- | :--- |
| 当前版本 | 记录 `metadata.yaml` 的 `version` 与 `git rev-parse --short HEAD` |
| AstrBot 版本 | 建议 `>= v4.5.7`（需支持 Plugin Pages / `register_web_api`） |
| 插件状态 | 升级前 Dashboard 能打开 Pages，日志无 Traceback |
| 数据备份 | **必须**完成 §2 的 `only_you.db` 与 `souls/` 备份 |
| 目录权限 | `<数据目录>` 对 AstrBot 进程可读写；`souls/` 可由插件自动创建 |
| 回滚预案 | 明确旧代码包位置与回滚命令（§7） |

> 不要在插件运行中手工编辑 `souls/*.json`；需要人工修改时先停插件（见 §8.4）。

---

## 2. 备份（必做）

插件数据目录（容器内）为 `/AstrBot/data/plugin_data/astrbot_plugin_only_you/`，宿主机通常对应 `/root/astrbot/data/plugin_data/astrbot_plugin_only_you/`。

```bash
cd /AstrBot/data/plugin_data/astrbot_plugin_only_you

# ① 一致性备份 SQLite
sqlite3 only_you.db ".backup 'only_you.db.bak-$(date +%Y%m%d%H%M%S)'" \
  || cp only_you.db "only_you.db.bak-$(date +%Y%m%d%H%M%S)"

# ② 备份整个数据目录（含 souls/）
cd /AstrBot/data/plugin_data
tar -czf "/path/to/onlyyou_data_backup-$(date +%Y%m%d%H%M%S).tar.gz" astrbot_plugin_only_you

# ③ 可选：备份旧版插件代码，便于回滚
tar --exclude='.git' --exclude='.env*' --exclude='*.pem' --exclude='__pycache__' \
    --exclude='.DS_Store' --exclude='*.db' \
    -czf /path/to/onlyyou_plugin_old.tar.gz -C /path/to/OnlyYou .
```

---

## 3. 部署与热重载

1. 上传新代码到插件目录，覆盖后**务必清理缓存与 macOS 资源文件**：

   ```bash
   find <插件目录> -name __pycache__ -type d -prune -exec rm -rf {} +
   find <插件目录> -name '._*' -delete
   ```

   `<插件目录>` 指 `data/plugins/astrbot_plugin_only_you`；`<数据目录>` 指 `data/plugin_data/astrbot_plugin_only_you`，两者不要混用。
2. 热重载插件（Dashboard API 或插件管理页）：

   ```bash
   curl -X POST http://127.0.0.1:6185/api/plugin/reload \
     -H "Content-Type: application/json" -d '{"name":"astrbot_plugin_only_you"}'
   ```
3. 修改了依赖（`requirements.txt`）或热重载后日志异常时，再执行 `docker restart astrbot`。

---

## 4. 日志校验

升级后按顺序确认：

| 日志 | 含义 |
| :--- | :--- |
| `[OnlyYou] Registered 31 Web API routes for Plugin Pages.` | 控制台后端路由挂载成功 |
| `[OnlyYou] Plugin initialized successfully.` | 初始化完成 |
| `[OnlyYou] Migrated legacy SQLite soul 'xxx' to ...` | 旧角色已落盘为 JSON |
| `[OnlyYou] Migrated N legacy UMO bindings to platform bindings.` | 旧绑定迁移完成 |
| `[OnlyYou] Migrated legacy global model_config into N Soul(s).` | 模型配置迁移完成 |
| `[OnlyYou] Migrated M legacy user platform id(s) into ...` | 裸用户 ID 规范化完成 |
| `[OnlyYou] Migrated legacy user relationships into per-Soul user_relations: {...}` | 关系拆分完成 |
| 无 `Traceback` | 必查项 |

v2.10 / v2.11 没有新增迁移日志：`proactive_enabled` 补列为静默幂等 `ALTER TABLE`，可用 §5.2 的 `PRAGMA table_info` 确认。

若某项 migration incomplete：

```text
[OnlyYou] legacy UMO binding migration incomplete (...) ; keeping the migration flag unset ...
```

说明该项失败、flag 未写入，修复权限 / 数据后重启即可重试，不需要手工改库。

---

## 5. 数据校验

### 5.1 `souls/*.json`

```bash
ls -l <数据目录>/souls/
python3 - <<'EOF'
import glob, json
for path in glob.glob('<数据目录>/souls/*.json'):
    data = json.load(open(path, encoding='utf-8'))
    assert data['soul_id'], path
    for key in ('card', 'model_config', 'bound_platforms', 'settings', 'updated_at', 'revision'):
        assert key in data, (path, key)
    print(path, data['soul_id'], data['revision'], len(data.get('state_event_cards', [])))
EOF
```

预期：每个角色一份文件；`revision >= 1`；`state_event_cards` 中每张卡都有 `action_type`。

### 5.2 SQLite

```bash
sqlite3 <数据目录>/only_you.db <<'SQL'
.tables
PRAGMA table_info(souls);
PRAGMA table_info(user_relations);
PRAGMA table_info(memories);
PRAGMA table_info(users);
SELECT soul_id, revision FROM souls;
SELECT soul_id, COUNT(*), SUM(proactive_enabled) FROM user_relations GROUP BY soul_id;
SELECT COUNT(*), SUM(proactive_enabled) FROM users;
SQL
```

预期：

- `souls` 含 `revision` 列，且每个角色一行；
- `user_relations` 含 `proactive_enabled` 列（默认 1），每个角色一组计数；`users` 身份数与升级前一致；
- v2.12：`memories` 含 `memory_type / event_at / embedding_json / superseded_by / last_reinforced_at`，`users` 含 `aliases_json`；旧记忆 `memory_type='episode'`、`event_at=0`（升级后第一次召回按 `created_at` 锚点）；
- `daily_schedules` 的旧日程会在访问时自动重抽，无需手工删。

### 5.3 迁移 flag

```bash
sqlite3 <数据目录>/only_you.db \
  "SELECT key, value_json FROM plugin_settings WHERE key LIKE '%migrated%' OR key='default_soul_id';"
```

有旧数据时，`legacy_umo_bindings_migrated` / `model_config_migrated_to_souls` / `legacy_user_identity_migrated_v2` / `user_relations_migrated_v1` 应为 `true`。本来就没有旧数据时 flag 可能不写入，属正常。

### 5.4 Pages 验收（9 页）

| 页面 | 重点检查 |
| :--- | :--- |
| 概览 | 当前角色、行动徽章、24h 时间轴、繁忙进度、今日繁忙成果 |
| 日程 | 7~11 块连续时间轴（静态模式为 3 块）；四类行动卡与固定护甲；「重抽日程卡池」提示今日不变 |
| 角色卡 | 10 分区表单可读写；头像可上传；离开未保存有确认 |
| 关系与群聊 | 按当前角色加载；L3/L2/L1 分组；群聊分区；合并只选当前角色用户；用户 / 群聊详情有「主动回复」开关，关闭项显示 `🤐 不主动` |
| 记忆 | T3/T2/T1 分层；T1 显示剩余时间与上限；每行带「形成于 …」时间；提炼 / GC 可用 |
| 冲浪 | 总开关与后台视频源按角色保存；视频卡片墙与手动冲浪 |
| 模型 | 四角色按角色读写；继承模式；测试与 TTS 试听 |
| 机器人接口 | 平台绑定 / 解绑；默认角色兜底；会话内存清理 |

---

## 6. 功能抽检

| # | 操作 | 预期 |
| :--- | :--- | :--- |
| 1 | `/onlyyou status` | 返回角色、作息、7~11 块时间轴、当前进度、微调配额与今日成果 |
| 2 | `/onlyyou reroll` | 今日块数与逐块内容不变；提示新卡池明日生效 |
| 3 | `RESTING` 下用 L1/L2 发消息 | 空回复、零 Token；L3 私聊强呼叫可击穿并切出 `IDLE_ROAM` 清醒窗 |
| 4 | `BUSY` 下 L1 / L2 发消息 | L1 被拦截并进未读；L2 获准且回复克制 |
| 5 | `IDLE` 下 `/onlyyou surf` | 返回真实视频、`vision` 评分与第一人称吐槽 |
| 6 | 等待 15 分钟心跳 | `SURF` / `IDLE_ROAM` 日志出现对应行为，且受冷却限制 |
| 7 | 两个角色分别设置模型 | 各自回复走各自的 `main`；互不影响 |
| 8 | 同一用户切换角色看关系页 | 等级 / 好感 / 称呼 / 态度按角色隔离，身份信息全局共享 |
| 9 | 重启插件 | 不重复分享已分享视频、不重复聊同一条繁忙成果；冷却保留；未读清空 |
| 10 | 修改角色卡口头禅并聊天 | 回复体现该口头禅；还原后无残留 |
| 11 | 把 `reply_gate_probability` 临时设为 0，在 `IDLE` 群聊发普通消息 | 不出现读空气决策调用，日志出现 `Reply gate dropped non-@ group message (...)`；@ Bot / 私聊仍正常回复 |
| 12 | 查看 System Prompt 或 Pages 记忆页 | 每条回忆记忆带「形成于 YYYY-MM-DD（星期，N 天前）」；记忆页展示绝对时间；记忆正文未被改写 |
| 13 | 关系页关闭某用户 / 群聊的「主动回复」后等待心跳 | 该目标不再收到未读补回 / 翻群 / 联想 / 视频分享；被动消息与 @ 不受影响；重新开启后恢复 |
| 14 | 模型页把「向量模型 / 重排模型」设为 AstrBot 已启用实例并点测试 | 向量返回维度、重排返回打分；关闭时 `/onlyyou status` 与聊天仍正常（自动降级词法检索） |
| 15 | 给同一用户连续改两次群名片再聊几句触发提炼 | 记忆只按一个规范称呼记录；提炼 Prompt 含「参与者身份归一表」，不会出现「A 对 B 还在啃数电」式身份分裂 |
| 16 | 连续发 20 条普通群消息（Bot 只回复几条） | 好感只按真实回复结算，同用户 180s 内只加一次；不会再「随便聊几句就升 L2」 |
| 17 | 在 `IDLE_ROAM` 心跳观察 1~2 小时 | 默认 2h 冷却 + 35% 概率 + 每日 3 条上限；`PERSONA_TOPIC` 与 `MEMORY_MUSE` 按 7:3 出现，不会连续复读同一条旧记忆 |

---

## 7. 回滚

```bash
# ① 停插件 / 停止写入
docker stop astrbot

# ② 恢复升级前备份
cd /AstrBot/data/plugin_data/astrbot_plugin_only_you
cp only_you.db.bak-<时间戳> only_you.db
# 如 souls/ 也需要回退：
# rm -rf souls && tar -xzf /path/to/onlyyou_data_backup-<时间戳>.tar.gz

# ③ 恢复旧版插件代码并启动
docker start astrbot
```

回滚注意：

- 回退到 v2.8 时，`user_relations` 表可保留（旧版不读取）；但 v2.9 期间按角色修改的关系**不会回写** `users` 旧关系列。
- 回退到 v2.9 / v2.10 时，v2.11 新增的 `proactive_enabled` 列会被旧版忽略，行为恢复为「所有会话都允许主动回复」；关系 / 记忆数据不丢，开关值保留在库中，重新升级后恢复。
- 回退到 v2.7 及更早时，新版 `souls/*.json` 中的 `revision` / `model_config` / `bound_platforms` / `settings` 字段会被旧版忽略，但旧版不读取 JSON 文件，需依赖 SQLite 索引与旧代码逻辑。
- `souls/*.json` 不是回滚的替代品：**回滚必须以升级前的 `only_you.db` 为准**。

---

## 8. 幂等、失败重试与降级

### 8.1 一次性 flag 语义

| flag（实际键名） | 负责迁移 | 失败语义 |
| :--- | :--- | :--- |
| `legacy_umo_bindings_migrated` | 旧 UMO 绑定 → `bound_platforms` | 任一条失败则不写 flag，下次启动重试；已迁移项跳过 |
| `model_config_migrated_to_souls` | 旧全局模型配置 → 各角色 | 任一角色失败则不写 flag；已有有效配置的角色跳过 |
| `legacy_model_migrated` | 旧 provider id → `(source_id, model)` | 只尝试一次；解析失败也会置位（不回滚、不重试） |
| `legacy_user_identity_migrated_v2` | 裸用户 ID → 规范账号 | 证据唯一才改写；歧义项保留待 Pages 手工补全 |
| `user_relations_migrated_v1` | `users` 关系列 → `user_relations` | 失败 / 无角色时不写 flag，下次启动重试 |

> v2.10 无新增 flag、无数据库迁移；v2.11 的 `proactive_enabled` 是幂等 `ALTER TABLE` 纯加法，不依赖 flag，失败时下次启动会自动重试补列。

### 8.2 旧库缺 `revision`

`db.initialize()` 用 `PRAGMA table_info` + `ALTER TABLE ... ADD COLUMN revision INTEGER NOT NULL DEFAULT 0` 幂等补列；旧角色首次读取时升级为 `revision >= 1` 并落盘。

### 8.3 `souls/` 不可写时的降级

- JSON 写失败重试 1 次后放弃，但**仍写 SQLite 索引**（数据不丢）；`souls/save` 返回 `data.storage = {file_written:false, degraded:true, error}` 并在 `message` 追加中文告警。
- 修复权限 / 磁盘后，下一次读取或保存会自动把 JSON 补回来（索引 revision 更高 → 回写文件自愈）。
- 自检：`ls -ld <数据目录>/souls`，确认 AstrBot 进程用户可写。

### 8.4 手工编辑 `souls/*.json`

1. 停插件或确保没有自动保存操作；
2. 编辑文件（保持 JSON 合法、`soul_id` 不变）；
3. 重启 / 热重载并在 Pages 刷新一次；
4. 文件优先：同 revision 但内容不同会记 warning 后保留文件内容，并重建索引。

不要把 `revision` 手工改小（会被自愈逻辑覆盖），也不要在运行中删文件。

---

## 9. 「解绑 / 清空」语义（清理 vs 保留）

| 操作 | 入口 | 清理（内存态） | 保留（持久化） |
| :--- | :--- | :--- | :--- |
| 解绑机器人接口 | 机器人接口页 / `sessions/unbind_platform` | 该平台所有会话滑窗、未读、群/L3 缓存、冷却时间戳、读空气门闸预算 | `bound_platforms` 仅移除该平台；`memories / users / user_relations / surfing_history / group_profiles` 不删 |
| 清空单会话 | `sessions/clear_memory {umo}` | 指定 UMO 的滑窗 / evicted / 未读 | 记忆、用户档案、群档案 |
| 清空整个平台 | `sessions/clear_memory {platform_id}` | 该平台所有 UMO 的滑窗 / 未读 / 冷却 | 绑定关系与全部持久化数据 |
| 清空群聊内存 | 关系页群聊详情 / `users/update action="clear_group_memory"` | 该群滑窗与未读 | 群档案（群名 / 备注 / 绑定角色） |
| 移除群聊记录 | 关系页群聊详情 / `users/update action="delete_group"` | 内存态 + `group_profiles` 档案 | `memories / users / user_relations` 不删 |
| 删除用户 | 关系页用户详情 / `users/update action="delete"` | — | 默认只删**当前角色**关系行；所有角色都无关系行时才删全局身份；记忆不删 |
| 关闭主动回复 | 关系页用户 / 群聊详情开关（`users/update` 的 `proactive_enabled`） | 该目标的主动消息（补回 / 翻群 / 联想 / 分享）；未读不 ACK、不消耗 | 记忆、关系、群档案、开关值；被动回复不受影响 |

> 统一原则：**「清空」只清内存会话态；「删除 / 移除」才动档案索引；持久化记忆、关系与冲浪历史默认永不删除。**

---

## 10. 重启语义

| 状态 | 重启后 | 说明 |
| :--- | :--- | :--- |
| 未读消息队列 | **清空** | 未读不持久化，既定产品语义 |
| 已分享视频去重 / 已聊繁忙成果 | 保留 | `runtime_state_v1` |
| 冲浪分享、闲逛、翻群、私聊补回冷却 | 保留 | 时间戳持久化，仅保留最近 7 天 |
| ★L3 私聊目标 | 保留 | 最多 100 个，MRU 淘汰 |
| 当日时间轴 / 繁忙结算 | 保留 | `daily_schedules`；已结算块不重复结算 |
| 每日遗忘 GC | 不重复跑 | `last_forgetting_gc_date` |
| 主动回复开关 | 保留 | `user_relations.proactive_enabled` / `GroupProfile.proactive_enabled` |
| 读空气概率 / 每小时预算 | 重置 | `_gate_pass_ts` 为内存态，重启后从空开始 |

---

## 11. 版本迁移条目

### 11.1 v2.8.2：普通配置与 Pages 去重

- 删除 `surfing_config.surfing_enabled`、`relationship_config.core_user_ids / important_user_ids` 等重复项。
- 旧配置值不会被读取；升级后请在 Pages「冲浪」「关系与群聊」页逐角色确认。
- `enable_tiered_memory=false` 时记忆写入、召回、提炼全部 no-op（含命令与 API）。
- 仅配置变化，无数据库迁移。

### 11.2 v2.8.3：后台视频源迁移到 Pages

- 后台视频源改存 Soul JSON 的 `settings.surfing_sources`（`bilibili` / `douyin` 子集，缺省两者都启用）；普通配置中的 `enable_bili_surfing` / `enable_douyin_surfing` 旧键一并删除。
- 旧全局开关不继承；需要在 Pages 逐角色重新选择。
- 只影响 SURF 心跳自动抓取；手动冲浪仍可临时指定平台。
- 无数据库迁移。

### 11.3 v2.8.4：用户平台身份规范化

- 新用户直接写入 `platform_id:native_uid`。
- 旧裸 ID 在收到消息时自动升级；启动时用持久化私聊 UMO 证据做一次性迁移（`legacy_user_identity_migrated_v2`），歧义项保留。
- Pages 关系页对旧格式显示「⚠️ 旧格式」，可用「补全平台」或在详情中补全；`users/update action="normalize_identity"` 同样可用。
- 只改 `users.platform_ids_json`，不动记忆与群档案。

### 11.4 v2.9：用户身份全局化 + 关系按角色隔离

- `users` 只保留全局身份；`user_relations(soul_id, global_uid)` 保存 `tier / affinity / interaction_count / nickname / attitude`。
- 关系页只显示当前角色；同一用户在不同角色下可有不同等级与态度。
- 合并只能从当前角色关系列表下拉选择；删除默认只删当前角色关系。
- 迁移按平台归属拆分，裸 ID 先按持久化 UMO 证据推断，再回退默认角色；失败不写 flag，下次启动重试。
- `users` 旧关系列保留作回退，**不要手工删除**。

### 11.5 v2.9：行动卡与时间轴清理

- 旧行动卡无合法 `action_type` 直接丢弃（日志 warning），不再猜测类型。
- 卡池按 `SLEEP 2 / BUSY_TASK 3 / SURF 2 / IDLE_ROAM 2` 生成；不足由兜底卡补齐。
- `reroll` / `cards/ai_generate(auto_save=true)` 只换卡池，今日时间轴冻结不变，新池明日生效。
- 旧 6 时段 / 过夜回绕 / 非连续日程不满足首块 `0.0`、末块 `24.0` 与首尾相连，会在加载时被重抽。
- 无需手工清库；若想立即换卡池，执行 `/onlyyou reroll`。

### 11.6 v2.10：记忆时间锚点 + 读空气概率/预算门闸

- **无数据库迁移、无 Web API 路由变化**；替换插件文件并热重载即可。
- **读空气概率门闸默认开启 80%**：普通（非 @）群消息约 20% 不再进入读空气决策模型。想保持旧行为把 `atmosphere_config.reply_gate_probability` 改为 `100` 或关闭 `reply_gate_enabled`；`reply_gate_hourly_limit`（默认 0 = 不限）可按群限制每小时判定次数。
- **记忆时间标注**：新增 `core/time_utils.py`；注入行带「形成于 …（星期，N 天前）」+ `MEMORY_TIME_CONTEXT_PROMPT`，提炼 transcript 带窗口范围与逐条时间戳。记忆正文、50 字上限、GC / 去重语义均不变。
- **Pages**：记忆页展示形成时间 tooltip；无需重建数据。

### 11.7 v2.11：按角色 / 会话的「主动回复」开关

- **纯加法迁移**：`user_relations.proactive_enabled` 与 `users.proactive_enabled` 由 `db.initialize()` 幂等补列（默认 1）；旧 `group_profiles` 缺键按 `true`。
- **行为变化**：关系页用户 / 群聊详情新增开关；关闭后只停 Bot 主动发起的未读补回、`GROUP_LURK`、`MEMORY_MUSE` 与视频分享。被动回复、@ 与读空气接话不变。
- **合并语义**：账号合并时 `proactive_enabled` 按「双方都允许才开启」的 AND 语义；删除用户 / 群聊不因开关状态删除记忆或关系。
- **v2.11.1**：修复群聊开关保存后重新打开又显示开启的前端字段丢失问题（仅 `views/console/page-relations.js` 的 `normalizeGroup` 白名单；后端与落库在 v2.11.0 即正确）。升级仅需覆盖前端静态资源并强刷浏览器缓存。

### 11.8 v2.12：RAG 记忆、身份归一与主动对话节奏

- **纯加法数据库迁移**：见 §0 表 #10 / #11；无需手工改库、无需清空记忆。旧记忆不会自动补向量，首次被召回 / 编辑时按需补算。
- **行为变化（非破坏性，但可感知）**：
  - **主动消息更少**：`idle_roam_cooldown_minutes` 45 → **120**，新增 35% 触发概率 / 每日 3 条上限 / 70% 人设新话题占比。想恢复高频可把冷却调回 45、概率调 100、上限调 0。
  - **好感曲线更慢**：只在 Bot 真正回复后结算，180s 冷却、+0.4/+0.6、每日 +5.0 封顶；L1→L2 改为互动 ≥ 40 **且** 好感 ≥ 30 的 AND 门槛（可在 `relationship_config` 调整）。已存在的 L2/L3 不受影响。
  - **T3 更难产生**：只有 `profile` / `relationship` 且 importance ≥ 8、有效命中 ≥ 8 次并跨天，才从 T2 晋升；`episode` / `state` 留在 T2。已存在的 T3 原样保留。
  - **语义去重对所有层级生效**：重复事实只刷新旧记忆、不再新增；旧库中已有的重复行不会自动删除，可在记忆页手工清理。
  - **提炼单批最多 3 条**：需要 decision 模型可用；失败时该批消息进入单批重试缓冲，下次提炼自动重试一次，不会永久丢失。
- **历史裸 ID 归并**：`memories.user_id` 里旧的裸平台 uid（如 `"42"`）或曾用名会在启动时一次性改写到 `webchat:42` 这类规范 `global_uid`；同一 native uid 属于多个用户的歧义行保持原样，可在关系页手工合并。旧记忆不强制补向量，被召回后后台惰性补算。
- **Embedding / Rerank 为可选**：不配置时自动降级为「content + keywords」词法检索，零报错；推荐在模型页绑定 AstrBot 已启用的 `BAAI/bge-m3` 与 `BAAI/bge-reranker-v2-m3`。
- **回滚**：回退到 v2.11 时，新增列会被旧版忽略（旧版 SQL 不读它们），记忆 / 用户 / 关系数据不丢；已 `superseded_by` 归档的行在旧版里会重新参与召回，但不会造成数据损坏。回滚后建议把 `idle_roam_*` 等新配置从旧版 AstrBot 插件配置面板之外的手工配置中移除（旧版不识别，会被 schema 完整性检查忽略）。

### 11.9 v2.12：T1 保留期与回收修复

- **问题**：一条形成于 12 天前的 T2 被手动降级为 T1 后，前端仍按 `created_at` 计算 48h 倒计时，于是立刻显示「剩余 0h」；服务端要等下一次心跳（≤15 分钟）的 `cleanup_tier1()` 才真正删除，造成「已过期却还在列表里」的观感。
- **修复**：
  - 新增 `memories.tier1_since`：新建 T1 取创建时刻；**从 T2/T3 降级为 T1 时重置为降级时刻**，保留期重新计算 48h。旧 T1（`tier1_since=0`）继续回退 `created_at`，不改变既有过期节奏。
  - `cleanup_tier1()` / `run_forgetting_gc()` / T1 FIFO 上限统一按 `COALESCE(NULLIF(tier1_since,0), created_at)` 判断与排序，降级来的新 T1 不会先被裁掉。
  - `memories/list` 与 `/onlyyou memory list` 在读取前先跑一次 `cleanup_tier1()`，过期项不必再等心跳；前端对真正过期的行显示「待回收（≤15 分钟）」而不是「剩余 0h」。
- **验证**：降级后 47h 仍保留、49h 删除；旧库 T1 过期即删；重复编辑已是 T1 的记忆不重置 48h；上限淘汰按新的保留期起点排序。离线回归 812 tests 全绿。
- **回滚**：回退到不含该修复的旧版插件时 `tier1_since` 被忽略，T1 又按 `created_at` 计时；不丢数据，但降级项会重新表现为「已过期」，属预期。

---

## 12. FAQ

**Q1：升级后 Pages 空白 / 404？**
确认日志有 `Registered 31 Web API routes`；检查是否解压到错误子目录、是否有旧 `__pycache__`；最后强刷浏览器缓存。

**Q2：`souls/` 目录不存在？**
首次启动或首次保存时自动创建。若日志出现 `create souls dir failed`，检查权限或磁盘；目录可稍后自动补建。

**Q3：`user_relations` 为空？**
确认存在至少一个角色、`user_relations_migrated_v1` 是否已写入；若日志有 `user_relations migration failed`，修复后重启重试。无旧关系数据时为空属正常。

**Q4：升级后行动卡变少了？**
v2.9 会丢弃无 `action_type` 的旧卡，并用兜底卡补齐四类行动。执行 `/onlyyou reroll` 可生成一整池新卡（今日时间轴不变）。

**Q5：解绑机器人接口后记忆 / 关系还在吗？**
在。按 §9 语义，解绑只清内存会话态；持久化记忆与关系不删。

**Q6：重启后又分享了昨天分享过的视频？**
检查 `runtime_state_v1.shared_video_ids`。只有**分享成功**的才记录；升级前旧版本分享过的视频没有记录，升级后首次可能重复一次，属预期。

**Q7：什么时候需要完整重启而不是热重载？**
修改依赖、AstrBot 配置结构变化，或热重载后日志异常时，使用 `docker restart astrbot`。

**Q8：升级后群里突然安静了？**
v2.10 起非 @ 普通群消息默认只按 80% 概率进入读空气，且可能被每小时预算拦截。检查 `atmosphere_config.reply_gate_enabled` / `reply_gate_probability` / `reply_gate_hourly_limit`；@ 与私聊不受影响。

**Q9：记忆里的「这周末」还是原样？**
v2.10 只注入「形成时间」供模型解冻相对时间，**不会改写记忆正文**（避免篡改事实）。若模型仍误用过期记忆，可在 Pages 记忆页手动编辑或删除该条。

**Q10：关闭主动回复后对方还会收到回复吗？**
会。该开关只停 Bot 主动发起（未读补回 / 翻群 / 联想 / 分享）；对方发消息、@ Bot 与被读空气选中的接话都照常。

**Q11：群聊开关保存后又变回开启？**
这是 v2.11.0 的前端字段白名单问题，v2.11.1 已修复。确认 `page-relations.js` 已更新并强刷浏览器缓存。

**Q12：新角色为什么会沿用旧角色给用户起的昵称 / 专属态度？**
旧版在首次遇到新角色时，会把 `users.nickname / users.attitude`（v2.9 之前的全局单角色列）原样播种进该角色的 `user_relations` 行。该继承已停止：新角色只继承可搬运的关系状态（等级 / 互动次数 / 好感 / 主动回复开关），专属称呼与专属态度从空开始。升级前已被播种的历史行不会自动清除（无法与管理员手动设置区分），在关系页切到对应角色、打开该用户详情清空「专属称呼 / 专属态度」即可；不带 `soul_id` 的旧全局读取路径仍显示旧值，属 legacy 兼容语义。

---

## 13. 升级速查表

```text
[ ] 已备份 only_you.db（.backup 或 cp）与 souls/ / 整个数据目录
[ ] 已备份旧版插件代码包
[ ] 新代码已上传，__pycache__ / ._* 已清理
[ ] 热重载或重启完成
[ ] 日志：Registered 31 Web API routes / Plugin initialized successfully
[ ] 日志：各迁移项符合预期，无 Traceback
[ ] souls/*.json 存在，含 card / model_config / bound_platforms / settings / updated_at / revision
[ ] souls 表含 revision 列；user_relations 每角色有行且含 proactive_enabled
[ ] plugin_settings 迁移 flag 状态符合预期
[ ] Pages 9 页可打开（含「设置」），关系 / 机器人接口页按角色工作
[ ] 记忆页显示形成时间；关系页主动回复开关可保存并刷新保持
[ ] 非 @ 群消息门闸符合预期（默认 80%，可调 0~100 或关闭）
[ ] 两个角色各自模型与关系互不影响
[ ] 重启不重复分享视频 / 不重复聊繁忙成果；未读按语义清空
[ ] 记录升级时间、版本号、备份路径与回滚步骤
```
