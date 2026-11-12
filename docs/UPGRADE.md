# OnlyYou v2.8 生产升级与迁移验证指南

> 适用对象：从 **v2.7.x（或更早 v2.6 / v2.5）** 升级到 **v2.8** 的 AstrBot 部署。
> 本文只描述升级 / 迁移 / 校验 / 回滚流程与排障语义，**不包含任何真实服务器地址、密钥或密码**；命令中的 `$SERVER_IP`、`$ASTRBOT_URL`、`<数据目录>`、`/path/to/OnlyYou` 均为占位符，请按实际环境替换。
>
> 版本口径：插件产品版本 **v2.8**，`metadata.yaml` → `version: 2.8.2`；Web API 路由 **31 条**；角色卡 **10 分区 / 36 字段**。文档不写死测试用例数量，验收以实际运行结果为准。

---

## 0. v2.8 升级会做什么（先理解再操作）

v2.8 是**配置与运行态可信化**版本。升级后首次启动 / 热重载时，插件会**自动、幂等**地完成以下迁移：

1. **Soul 独立 JSON 落盘**：把 SQLite `souls` 表中只有行、没有 JSON 文件的旧角色，逐个写为 `<数据目录>/souls/<soul_id>.json`；此后 **JSON 文件是唯一真源**，SQLite `souls` 表降级为索引。
2. **旧 UMO 绑定 → 机器人接口（platform）绑定**：读取旧 `umo_soul_bindings`，按 UMO 第一段（`platform_id`）迁移为角色文件的 `bound_platforms`；一次性 flag `legacy_umo_bindings_migrated`。
3. **旧全局模型配置 → 各角色 `model_config`**：把旧 `plugin_settings["model_config"]` 复制到「主模型为空」的已有角色（不删旧全局键）；一次性 flag `model_config_migrated_to_souls`。
4. **SQLite `souls` 表补 `revision` 列**（`PRAGMA table_info` + `ALTER TABLE`，幂等）。
5. **运行态快照初始化**：`plugin_settings["runtime_state_v1"]` 首次读取缺省为空快照，后续在状态变更点自动写入。
6. 关系页 / 会话页等 Pages 前端更新到 v2.8（无需数据库迁移）。

> **关键原则**：两个一次性迁移 flag **只有在全部条目迁移成功后才会写入**；任一条失败则保留 flag 未写状态并记 warning「下次启动重试」，迁移逻辑本身幂等（已迁移项跳过）。因此**失败可重试，重试不会重复数据**。

---

## 1. 升级前检查清单

| 检查项 | 要求 / 说明 |
| :--- | :--- |
| 当前版本 | 记录当前 `metadata.yaml` 的 `version`（v2.7.x / v2.6 / v2.5…）与 `git rev-parse --short HEAD` |
| AstrBot 版本 | 建议 `>= v4.5.7`（与 PRD 口径一致） |
| 插件状态 | 升级前确认插件当前**可用**：Dashboard 能打开 Pages、日志无 Traceback |
| 数据备份 | **必须**完成 §2 的 `only_you.db` 与 `souls/` 备份；生产建议连同整个插件数据目录一起打包 |
| 磁盘权限 | 插件数据目录（容器内 `/AstrBot/data/plugin_data/astrbot_plugin_only_you/`）对 AstrBot 进程**可读写**；`souls/` 可由插件自动创建 |
| 维护窗口 | 热重载通常秒级完成；如遇旧版 `@register` 装饰器缓存或依赖变更，可安排 `docker restart` 窗口 |
| 回滚预案 | 明确回滚命令（§8）与旧代码包位置 |

> **不要在插件运行中手工编辑 `souls/*.json`**；如需人工修改，先停插件 / 热重载前操作（见 §9.4）。

---

## 2. 备份（必做）

插件数据目录（容器内路径）为 `/AstrBot/data/plugin_data/astrbot_plugin_only_you/`；宿主机通常对应 `/root/astrbot/data/plugin_data/astrbot_plugin_only_you/`（以实际挂载为准）。

```bash
# ① 进入插件数据目录（容器内或宿主机挂载目录）
cd /AstrBot/data/plugin_data/astrbot_plugin_only_you

# ② 备份 SQLite（推荐先用 sqlite3 .backup 生成一致性快照，再压缩归档）
sqlite3 only_you.db ".backup 'only_you.db.bak-$(date +%Y%m%d%H%M%S)'" \
  || cp only_you.db "only_you.db.bak-$(date +%Y%m%d%H%M%S)"

# ③ 备份整个数据目录（含 souls/、可能有旧备份文件；*.db 已在 .gitignore，不会提交）
cd /AstrBot/data/plugin_data
tar -czf "/path/to/onlyyou_data_backup-$(date +%Y%m%d%H%M%S).tar.gz" astrbot_plugin_only_you

# ④ 可选：备份旧版插件代码包（便于回滚）
#    在本地项目根目录执行：
#    tar --exclude='.git' --exclude='.env*' --exclude='*.pem' --exclude='__pycache__' \
#        --exclude='.DS_Store' --exclude='*.db' -czf /path/to/onlyyou_plugin_v2.7.tar.gz -C /path/to/OnlyYou .
```

> ⚠️ 数据库文件 `only_you.db` 含用户关系 / 记忆等数据，**严禁提交到 Git 或写入任何文档**。

---

## 3. 部署与热重载

### 3.1 上传新版本代码

```bash
# 本地项目根目录打包（排除敏感文件、缓存与数据库）
tar --exclude='.git' --exclude='.env*' --exclude='*.pem' --exclude='__pycache__' \
    --exclude='data' --exclude='.DS_Store' --exclude='ssh-key*' --exclude='*.db' \
    -czf /tmp/onlyyou_plugin.tar.gz -C /path/to/OnlyYou .

scp -i "$SSH_KEY_PATH" /tmp/onlyyou_plugin.tar.gz "$SERVER_USER@$SERVER_IP:/tmp/"

ssh -i "$SSH_KEY_PATH" "$SERVER_USER@$SERVER_IP" \
  "mkdir -p /root/astrbot/data/plugins/astrbot_plugin_only_you && \
   tar -xzf /tmp/onlyyou_plugin.tar.gz -C /root/astrbot/data/plugins/astrbot_plugin_only_you && \
   find /root/astrbot/data/plugins/astrbot_plugin_only_you -name __pycache__ -type d -prune -exec rm -rf {} + && \
   rm -f /tmp/onlyyou_plugin.tar.gz"
```

> macOS 打包注意：解压后务必清理 `__pycache__`，并删除 `.astrbot-plugin/i18n/._*.json` 这类 AppleDouble 资源文件，避免插件加载日志出现 i18n 告警。

### 3.2 触发插件热重载

**方式 A：Dashboard UI** —— AstrBot Dashboard → 插件管理 → `astrbot_plugin_only_you` → 重载。

**方式 B：Dashboard API**（先登录拿 JWT，再调用插件重载接口）：

```bash
# 登录（示例；实际地址/账号以环境为准，凭据只放本地 .env）
curl -s -c /tmp/oy_cookies.txt -X POST "$ASTRBOT_URL/api/v1/auth/login" \
  -H "Content-Type: application/json" \
  -d "{\"username\":\"$ASTRBOT_DASHBOARD_USERNAME\",\"password\":\"$ASTRBOT_DASHBOARD_PASSWORD\"}"

# 热重载插件
curl -s -b /tmp/oy_cookies.txt -X POST "$ASTRBOT_URL/api/plugin/reload" \
  -H "Content-Type: application/json" \
  -d '{"name": "astrbot_plugin_only_you"}'
```

**方式 C：完整重启容器**（热重载异常或升级了依赖时）：

```bash
ssh -i "$SSH_KEY_PATH" "$SERVER_USER@$SERVER_IP" "docker restart astrbot"
```

---

## 4. 日志校验（升级是否成功的第一步）

查看插件最近日志（按实际容器名 / 日志路径调整）：

```bash
ssh -i "$SSH_KEY_PATH" "$SERVER_USER@$SERVER_IP" \
  "docker logs --tail 300 astrbot 2>&1 | grep -E 'OnlyYou|Traceback'"
```

| # | 期望日志 | 含义 / 不满足时怎么办 |
| :--- | :--- | :--- |
| 1 | `[OnlyYou] Registered 31 Web API routes for Plugin Pages.` | Web API 注册成功且数量正确（v2.7 是 27 条）→ 若出现其他数字，说明新旧文件混装 / `core/web_api.py` 未更新 |
| 2 | `[OnlyYou] Plugin initialized successfully.` | 插件初始化完成 → 缺失则检查是否有初始化异常 |
| 3 | `[OnlyYou] Migrated N legacy UMO bindings to platform bindings.` | 旧 UMO 绑定迁移成功（N 为迁移条数；无旧绑定时不出现属正常） |
| 4 | `[OnlyYou] Migrated legacy global model_config into N Soul(s).` | 旧全局模型配置迁移成功（无旧配置 / 各角色已有配置时不出现属正常） |
| 5 | `[OnlyYou] Migrated legacy SQLite soul '<soul_id>' to .../souls/<file>.json` | 旧 SQLite-only 角色落盘成功（每个旧角色一条） |
| 6 | `[OnlyYou] Created default soul '<soul_id>' (...)` | 空库自动创建默认角色（首次安装或全新数据目录） |
| 7 | **无 `Traceback`** | 出现 Traceback 必须停下来排查（常见原因：依赖缺失 / 数据目录权限 / 旧 `__pycache__` / 网络异常） |

**允许出现、但需要关注含义的 warning**：

| 日志 | 含义 | 处理 |
| :--- | :--- | :--- |
| `[OnlyYou] write soul file failed after retries (...); falling back to the SQLite index` | `souls/` 目录不可写 / 磁盘满，已降级为 SQLite-only（数据未丢） | 修权限 / 空间后，下一次读取或保存会自动回填 JSON（§9.3） |
| `[OnlyYou] Restored soul '...' JSON file from the newer SQLite index` | 上次写文件失败后自愈，索引内容回写 JSON | 无需处理，确认 JSON 已出现即可 |
| `[OnlyYou] Soul '...' JSON file differs from the SQLite index at the same revision` | 检测到 `souls/*.json` 被手工修改，以文件为准重建索引 | 符合预期，核对文件内容即可 |
| `[OnlyYou] per-soul model_config migration incomplete` / `migrate legacy umo binding failed` | 迁移有条目失败，flag 未写，**下次启动自动重试** | 排查失败原因（权限 / 锁定 / 数据异常），重启后复查 flag |

---

## 5. 数据校验（升级后必须逐项确认）

### 5.1 `souls/*.json` 已生成且字段齐全

```bash
cd /AstrBot/data/plugin_data/astrbot_plugin_only_you

# 列出从旧库迁移出来的角色文件
ls -l souls/

# 逐文件确认关键字段（需要 jq；无 jq 可用 python3 -m json.tool 人工核对）
for f in souls/*.json; do
  echo "== $f"
  python3 - "$f" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
required = ("soul_id", "card", "model_config", "bound_platforms", "settings", "updated_at", "revision")
missing = [k for k in required if k not in data]
print("missing:", missing)
print("soul_id:", data.get("soul_id"), "| revision:", data.get("revision"),
      "| bound_platforms:", data.get("bound_platforms"), "| surfing:", (data.get("settings") or {}).get("surfing_enabled"))
PY
done
```

期望：
- 每个文件 `missing: []`，且 `revision` 为 ≥ 1 的整数；
- `card` 含 10 个分区（`identity / appearance / personality / background / interests / speech / social / rhythm / examples / extra`）；
- `bound_platforms` 为旧 UMO 第一段迁移来的平台列表（可能为空数组）；
- `settings.surfing_enabled` 为布尔（默认 `true`）。

### 5.2 SQLite 索引与一次性 flag

```bash
cd /AstrBot/data/plugin_data/astrbot_plugin_only_you

# ① 索引表应含 revision 列，且每个角色一行
sqlite3 only_you.db "PRAGMA table_info(souls);"
sqlite3 only_you.db "SELECT soul_id, revision, updated_at FROM souls ORDER BY updated_at DESC;"

# ② 两个一次性迁移 flag 应为 true
sqlite3 only_you.db \
  "SELECT key, value_json FROM plugin_settings WHERE key IN ('legacy_umo_bindings_migrated','model_config_migrated_to_souls');"

# ③ 运行态快照与每日 GC 日期（存在即正常；不存在会在下一次触发时自动写入）
sqlite3 only_you.db \
  "SELECT key, substr(value_json,1,120) FROM plugin_settings WHERE key IN ('runtime_state_v1','last_forgetting_gc_date','default_soul_id');"
```

期望：
- `PRAGMA table_info(souls)` 中存在 `revision INTEGER NOT NULL DEFAULT 0`；
- `legacy_umo_bindings_migrated` / `model_config_migrated_to_souls` 的 `value_json` 为 `true`；
  - 若为 `false` / 查不到：说明迁移曾失败或本轮无迁移项。无旧数据时属于正常；有旧数据时查看 §4 的 warning，重启插件后复查。
- `plugin_settings` 中不再依赖 `model_config` 作为运行时真源（旧键仅为迁移来源保留，可忽略）。

> **键名说明**：旧 UMO 迁移 flag 的真实 key 是 **`legacy_umo_bindings_migrated`**（代码 `_SETTING_LEGACY_UMO_MIGRATED`）。排障时不要按口语简写 `legacy_umo_migrated` 去查库；两个 flag 的完整、准确键名见本表 §9.1。

### 5.3 Pages 验收（8 页）

1. 打开 AstrBot Dashboard → 插件 `astrbot_plugin_only_you` → Pages 控制台；
2. 逐个打开 8 个页面：**概览 / 日程 / 角色卡 / 关系与群聊 / 记忆 / 冲浪 / 模型 / 会话（机器人接口）**；
3. 确认：
   - 角色下拉能列出迁移后的全部角色（含旧库角色）；
   - **关系页**已是最新版「关系与群聊」（筛选 chips + 搜索 + 分组横向列表 + 居中宽幅弹窗 + 群聊分区），**不是**旧三列看板；
   - **会话页**已是最新版「机器人接口」（绑定列表 / 手动输入 `platform_id` / 默认角色兜底开关 / 会话内存概览），**不是**旧「默认角色 + UMO 绑定表」；
   - **模型页**顶部显示当前角色，四角色配置能读取；切换角色后数据随之切换；
   - **冲浪页**顶部有「赛博冲浪总开关」；
   - **记忆页** T1 行显示 `⏳ 剩余 Xh 清除` 与「上限 N 条」。

### 5.4 旧 UMO 绑定出现在「机器人接口」页

升级前若存在按会话（UMO）绑定的角色，迁移后应体现为**机器人接口绑定**：

- 会话页 → 当前角色 → 已绑定接口列表中出现旧 UMO 对应的 `platform_id`（如 `aiocqhttp`）；
- 若同一 `platform_id` 出现在多个角色下，以最近更新的角色胜出（严格互斥），其余角色可在会话页解绑 / 重新绑定；
- 旧 `UMO 精确绑定` 仍保留为**兼容路由兜底**（路由优先级第 ② 层），但会话页不再提供按 UMO 绑定 UI。

---

## 6. 功能验收清单（建议逐项勾选）

| # | 场景 | 预期 |
| :--- | :--- | :--- |
| 1 | 角色路由 | 在某个已绑定机器人接口的平台发消息 → 路由到绑定角色；未被绑定的平台 → 默认角色兜底 |
| 2 | 平台互斥 | 会话页把已被角色 A 绑定的接口「绑定到当前角色 B」→ 按钮禁用 / API 返回「该机器人接口已被角色『A』绑定，请先在角色 A 下解绑」 |
| 3 | 按角色模型 | 角色 A / B 分别配置不同 `main` 模型，各自聊天日志显示走了对应模型；自建实例 id 为 `onlyyou::main::<soul_id>` |
| 4 | 冲浪总开关 | 冲浪页关闭当前角色总开关 → 下次 SURF 时段日志显示 `SURF disabled for soul=...; degrading to IDLE_ROAM`；`/onlyyou surf` 回复总开关已关闭提示 |
| 5 | 分享门禁 | 低分 / 视觉兜底 / 已分享过 / 冷却未到的视频不主动分享；`is_fallback=True` 时日志出现 `SURF share skipped: vision fallback result` |
| 6 | MEMORY_MUSE | IDLE_ROAM 时段日志出现 `IDLE_ROAM(MEMORY_MUSE) ... source=BUSY_RESULT` 或 `source=MEMORY`；同一繁忙成果不会被主动聊第二次 |
| 7 | T1 规则 | 记忆页 T1 显示倒计时与上限；连续写入相同 T1 内容不会重复计数；T1 数量不超 `max_tier1_memories`（默认 30） |
| 8 | 跨重启去重 | 分享过某视频后重启插件 → 同一视频不会再被主动分享；近期冲浪分享冷却仍然生效（日志 `SURF share skipped: surf-share cooldown`） |
| 9 | 每日 GC 不重复 | 同一天内重启插件 → 日志不会重复出现完整 GC（`last_forgetting_gc_date` 已为今天） |
| 10 | 解绑清理 | 解绑机器人接口后：该平台会话内存清空，但关系页用户 / 记忆页记忆 / 冲浪历史仍在 |
| 11 | 角色配置热生效 | 模型页修改配置后约 0.7s 自动保存（或 `Ctrl/Cmd+S`），新消息即走新模型 |
| 12 | 手工改 JSON | 停插件后修改 `souls/<soul_id>.json` 中角色名 → 重启后 Pages 显示新名称（文件胜出并重建索引） |

---

## 7. 升级后仍不生效的排查顺序

1. **确认文件真的换新了**：检查插件目录 `metadata.yaml` 的 `version: 2.8.2`、`core/runtime_state.py` 是否存在、`core/web_api.py` 路由注册处是否为 31 条；
2. **确认没有旧字节码 / 混装**：删除插件目录下所有 `__pycache__` 后再次热重载；确认没有把压缩包解压到旧目录的子目录里；
3. **确认日志**：`Registered 31 Web API routes` 是否出现；`Plugin initialized successfully` 是否出现；
4. **确认数据目录**：`souls/` 是否在 `only_you.db` 的同级目录；是否存在权限问题（§9.3）；
5. **确认 Pages 缓存**：浏览器强制刷新（或清理 Dashboard 静态资源缓存）后再打开 8 个页面；
6. **确认没有回滚残留**：若此前回滚过旧版本，可能残留在 `plugin_settings` 中的旧 flag / 旧全局模型配置；对照 §5.2 检查。

---

## 8. 回滚

**触发条件**：升级后出现持续 Traceback、数据异常、Pages 无法打开且短时间无法修复。

```bash
# ① 停止插件（或在容器内停止 AstrBot，按实际管理方式）
#    简单做法：docker stop astrbot（或先在 Dashboard 禁用插件）

# ② 恢复 SQLite 备份（在插件数据目录内）
cd /AstrBot/data/plugin_data/astrbot_plugin_only_you
cp -a only_you.db.bak-YYYYmmddHHMMSS only_you.db

# ③ 恢复 souls/ 目录（如升级前已有可回滚备份）
#    v2.8 新生成的 souls/ 是新增目录，回退 v2.7 时可直接保留（旧版不读它）
#    若需完全回到升级前状态：删除 v2.8 新生成的文件 / 恢复整目录备份
tar -xzf /path/to/onlyyou_data_backup-YYYYmmddHHMMSS.tar.gz -C /AstrBot/data/plugin_data

# ④ 回退插件代码到旧版本包
rm -rf /root/astrbot/data/plugins/astrbot_plugin_only_you
tar -xzf /path/to/onlyyou_plugin_v2.7.tar.gz -C /root/astrbot/data/plugins/astrbot_plugin_only_you

# ⑤ 重启 / 重新加载插件，确认旧版日志与 Pages 正常
```

**回滚兼容性说明**：
- v2.8 每次保存都会同步维护 SQLite `souls` 索引（含 `profile_json`），因此**回退到 v2.7 仍可读取角色数据**；v2.7 不认识的新字段（`revision` / `model_config` / `bound_platforms` / `settings`）会被忽略。
- v2.8 的 `souls/*.json` 是新增文件，旧版不会读取，保留即可；如确认不再升级回 v2.8，可自行归档删除。
- 回滚后建议再次备份当前状态，避免后续升级覆盖现场。

---

## 9. 幂等 / 失败重试 / 降级语义

### 9.1 一次性迁移 flag 只在全部成功后写入

| flag（实际键名） | 负责迁移 | 失败语义 |
| :--- | :--- | :--- |
| `legacy_umo_bindings_migrated` | 旧 UMO 绑定 → `bound_platforms` | 任一条失败则不写 flag、log warning「下次启动重试」；重试时已迁移的平台绑定会跳过（幂等） |
| `model_config_migrated_to_souls` | 旧全局 `model_config` → 各角色 | 任一角色失败则不写 flag；重试时「已有有效配置」的角色跳过（幂等） |

> 重试方式：再次热重载 / 重启插件即可；无需手工改库。确认全部成功后 flag 才会变为 `true`。

### 9.2 旧库无 `revision` 列

`db.initialize()` 用 `PRAGMA table_info` + `ALTER TABLE souls ADD COLUMN revision INTEGER NOT NULL DEFAULT 0` 幂等补列；旧角色首次读取时自动升级为 `revision >= 1` 并落盘 JSON。

### 9.3 `souls/` 目录不可写时的降级（重要）

- **行为**：JSON 写失败重试 1 次后放弃，但**仍写 SQLite 索引**（数据不丢，不会静默丢配置）；同时 `souls/save` API 返回 `data.storage = {file_written: false, degraded: true, error: "..."}`，`message` 带「⚠️ Soul JSON 写入失败，已降级写入 SQLite 索引，重启/读取时会自动重试落盘」。
- **恢复**：修复目录权限 / 磁盘空间后，**下一次读取或保存会自动回填 JSON**（索引 revision 更高 → 回写文件自愈），无需手工导出。
- **自检**：
  ```bash
  ls -ld /AstrBot/data/plugin_data/astrbot_plugin_only_you/souls
  # 确认 AstrBot 进程用户可写
  ```

### 9.4 手工编辑 `souls/*.json` 的正确姿势

1. 先停插件（或至少停止写入：关闭 Dashboard 自动保存操作）；
2. 编辑 `souls/<soul_id>.json`（保持 JSON 合法、`soul_id` 不变）；
3. 重启 / 热重载插件，并在 Pages 刷新一次；
4. 插件将以 **文件为准** 重建 SQLite 索引；若与索引同 revision 但内容不同，会 log warning 后保留文件内容。

> 不要把 `revision` 手工改小（会被自愈逻辑覆盖），也不要在插件运行中删文件（可能导致索引重建后回写）。

---

## 10. 「解绑 / 清空」排障语义表（清理 vs 保留）

| 操作 | 入口 | 清理什么（内存态） | 保留什么（持久化） | 重启后果 |
| :--- | :--- | :--- | :--- | :--- |
| **解绑机器人接口** | 会话页 → 「解绑（清理该平台内存滑窗）」；或 `POST sessions/unbind_platform` | 该平台所有 UMO 的会话滑窗 / evicted / 未读；`_known_group_umos` / `_known_l3_umos` 中该平台项；该平台各类冷却时间戳 | 角色 JSON 的 `bound_platforms` 仅移除该平台；`memories / users / surfing_history / group_profiles` **不删** | 需重新绑定；持久化记忆 / 关系仍在；运行态冷却会同步持久层（不会复活） |
| **清空单个会话内存** | 会话页 → 单会话「清空」；或 `POST sessions/clear_memory {umo}` | 指定 UMO 的滑窗 / evicted / 未读 | 该会话的持久化记忆 / 用户档案 / 群档案 | 该会话上下文从空白开始，关系等级与记忆仍保留 |
| **清空整个平台内存** | 会话页 → 「清空整个平台」；或 `POST sessions/clear_memory {platform_id}` | 该平台所有 UMO 的滑窗 / evicted / 未读 / 冷却缓存 | 绑定关系与全部持久化数据 | 同上；需要重新积累上下文 |
| **清空群聊内存** | 关系页 → 群聊详情 → 「清空该群内存滑窗与未读」；或 `users/update action="clear_group_memory"` | 该群 UMO 的滑窗 / 未读 | 群档案（群名 / 备注 / 绑定角色 / 最近活跃）仍在 | 群聊上下文从空白开始，群档案仍可见 |
| **移除群聊记录** | 关系页 → 群聊详情 → 「移除群聊记录」；或 `users/update action="delete_group"` | 该群内存态 + `plugin_settings["group_profiles"]` 中的档案记录 | `memories / users` **不删**；该群之后若再有发言会重新建档 | 群聊不再出现在关系页「💬 群聊」分区；重新发言后重新出现 |

> **统一原则**：**「清空」只清内存会话态；「删除 / 移除」才动档案索引；持久化记忆、用户关系、冲浪历史默认永远不删。** 若用户反馈「解绑后记忆没了」，先按本表核对是否实际执行了「移除群聊记录」或手工删库。

---

## 11. 重启语义边界（避免把正常行为当 bug）

| 状态 | 重启后 | 说明 |
| :--- | :--- | :--- |
| **未读消息 backlog** | **清空** | 未读队列本身不持久化，这是既定产品语义：重启后不会补回旧未读 |
| 主动消息去重（已分享视频 / 已聊繁忙成果） | **保留** | 由 `runtime_state_v1` 恢复，不会重复分享 / 重复聊 |
| 冲浪分享 / 闲逛 / 翻群 / 私聊补回冷却 | **保留** | 冷却时间戳持久化（仅保留最近 7 天） |
| ★L3 私聊目标（`known_l3_umos`） | **保留** | 最多 100 个，MRU 淘汰 |
| 当日动态时间轴 / 繁忙结算 | 保留 | 由 SQLite `daily_schedules` 承载；已结算块不会重复结算 |
| 每日遗忘 GC | 不重复跑 | `last_forgetting_gc_date` 为今天时只做 T1 实时清理 |
| 解绑后的平台内存 / 冷却缓存 | 不复活 | `clear_platform_memory_state` 会同步持久层 |

---

## 12. 常见问题（FAQ）

**Q1：升级后 Pages 打开空白 / 404？**
A：先确认日志出现 `Registered 31 Web API routes`；再检查插件目录是否被旧 `__pycache__` 覆盖、是否解压到了错误子目录；最后强刷浏览器缓存。

**Q2：`souls/` 目录不存在？**
A：首次启动 / 首次保存时自动创建（`SoulManager._ensure_souls_dir`）。查看日志是否有 `create souls dir failed`——通常是权限或磁盘问题。目录可稍后自动补建。

**Q3：`legacy_umo_bindings_migrated` 一直是 false？**
A：说明迁移有失败项（日志有 `migrate legacy umo binding failed`），插件会在下次启动重试；排查权限 / 数据异样后重启。若本来就没有旧绑定，flag 可能不会被写入，属正常。

**Q4：升级后角色名 / 模型配置不对？**
A：检查对应 `souls/<soul_id>.json` 内容与 `revision`；如果之前手工改过文件，文件会胜出；如果怀疑是旧索引覆盖，查看日志是否有 `Restored soul ... from the newer SQLite index`（自愈）或 `keeping the JSON file (manual edit)`。必要时先备份再让插件重新读取。

**Q5：解绑机器人接口后用户关系 / 记忆还在吗？**
A：在。按 §10 语义，解绑只清内存会话态；`memories / users / surfing_history / group_profiles` 不删。

**Q6：重启后机器人又分享了昨天分享过的视频？**
A：确认升级后的运行态持久化是否生效：查 `plugin_settings["runtime_state_v1"]` 中 `shared_video_ids` 是否包含该 `video_id`。只有**分享成功**的才记录；升级前旧版本分享过的视频没有历史记录，不纳入去重（升级后第一次可能重复一次属预期）。

**Q7：什么时候需要完整重启而不是热重载？**
A：修改了依赖（`requirements.txt`）、升级涉及 AstrBot 配置结构、或热重载后日志异常时，使用 `docker restart astrbot`。

---

## 13. 附录：升级检查速查表

```text
[ ] 已备份 only_you.db（sqlite3 .backup 或 cp）与 souls/ / 整数据目录
[ ] 已备份旧版插件代码包
[ ] 新代码已上传并清理 __pycache__ / AppleDouble 文件
[ ] 热重载或重启完成
[ ] 日志：Registered 31 Web API routes for Plugin Pages.
[ ] 日志：Plugin initialized successfully.
[ ] 日志：迁移日志（UMO / model_config / SQLite soul 落盘）符合预期；无 Traceback
[ ] souls/*.json 存在且含 soul_id/card/model_config/bound_platforms/settings/updated_at/revision
[ ] souls 表含 revision 列，每个角色一行
[ ] plugin_settings：legacy_umo_bindings_migrated / model_config_migrated_to_souls = true（有旧数据时）
[ ] Pages 8 页可打开；关系页 / 会话页为 v2.8 新版
[ ] 旧 UMO 绑定出现在会话页「机器人接口」
[ ] 某个角色聊天路由 / 模型按角色生效
[ ] 冲浪总开关、分享门禁、T1 上限、重启去重抽检通过
[ ] 普通配置项与 Pages 无重复（无全局冲浪开关 / 用户等级名单）
[ ] 记录升级时间、版本号、备份路径与回滚步骤
```

---

## 14. v2.8.2 配置去重（普通配置 ↔ Pages）升级注意

v2.8.2 起，插件「普通配置」（AstrBot 插件配置面板 / `_conf_schema.json`）只保留**运行参数**，用户级 / 角色级开关与名单全部交给 Pages：

1. **角色冲浪总开关**：普通配置中的全局 `surfing_config.surfing_enabled` 已删除；每个角色在 Pages「冲浪」页用 `settings.surfing_enabled` 独立管理（缺省开启）。升级后旧配置中的 `surfing_enabled` 即使残留也不会被读取，AstrBot 也可能按新 schema 丢弃；若之前全局关闭，需要在 Pages 里逐角色确认开关。
2. **用户等级 / 名单**：普通配置中的 `relationship_config.core_user_ids` / `important_user_ids` 已删除；用户等级存 SQLite `users.tier`，统一在 Pages「关系与群聊」页管理；★L3 仍会由 AstrBot `admins_id` 自动确定。旧名单不再自动 seed，已存在的用户等级不受影响。
3. **分级记忆开关统一**：`memory_and_context_config.enable_tiered_memory=false` 时，除记忆注入 / 召回 / 提炼外，`add_memory` 也直接 no-op（`/onlyyou memory add`、Pages 新增记忆、繁忙成果写入均不会落库），命令与 Web API 会显式提示。
4. **配置标题简化**：所有配置项标题缩短，详细说明移入 `hint`；升级后请在 AstrBot 插件配置页确认显示正常（必要时刷新页面）。
5. **Pages 记忆页引导**：注入预算提示条新增「前往 AstrBot 插件配置修改预算」，预算 / 滑窗 / 提炼阈值等仍在普通配置中修改。

以上均**不涉及数据库迁移**，热重载或重启即可生效。
