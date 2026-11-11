/**
 * OnlyYou Plugin Pages — 记忆页 (`views/console/page-memory.js`)
 *
 * 数据源：
 *   GET  `memories/list`   → { memories: [...], stats: {...} }（支持 memory_tier / keyword / user_id / soul_id）
 *   GET  `users/list`      → { users: [...] }（关联用户筛选下拉；失败时降级为使用记忆里的 user_id）
 *   POST `memories/mutate` → action = add / edit / delete / pin_core / extract / gc
 *
 * 页面结构（v2.6 Phase 4「分层分组 + 结构化列表」）：
 *  ① 顶部层级分布概览条 .memory-summary-strip：全部 / T3 / T2 / T1 计数 + 彩色占比条，点击即切换筛选；
 *  ② 工具栏：关联用户筛选 + 搜索（400ms 防抖）+ 上下文注入预算提示；
 *  ③ 按 T3 → T2 → T1 分组的 .memory-group（分组头可点击折叠，空分组显示 .memory-group-empty 并默认收起）；
 *     每条记忆为四列 .memory-row：
 *       列1 层级/重要度（.tier-badge + .memory-importance）
 *       列2 内容/关键词（.memory-row-content + .memory-row-keywords > .kw）
 *       列3 关联用户/活力/召回（.memory-user + .memory-vitality + .memory-recall）
 *       列4 升降级 + 编辑 + 删除（.tier-switch 只显示当前层级以外的按钮）
 *  旧 .memory-grid 卡片矩阵已废弃，但保留全部既有能力：新增 / 编辑 / 删除 / 提炼 / GC /
 *  搜索防抖 / 关联用户筛选 / 刷新 / 注入预算提示 / 空态。
 *
 * 注册：window.OY_PAGES.memory = { id, title, icon, render(container, ctx), destroy() }
 */
(function () {
  "use strict";

  var TIER_META = {
    3: {
      label: "T3 核心",
      short: "T3",
      title: "💎 Tier 3 · 核心长效记忆",
      desc: "核心永久记忆（永久保留）",
      cls: "tier-3",
      hint: "永久保留 · 免疫每日遗忘清理 GC"
    },
    2: {
      label: "T2 近期",
      short: "T2",
      title: "📅 Tier 2 · 近期情节记忆",
      desc: "近期记忆（会随时间衰减）",
      cls: "tier-2",
      hint: "按活力值衰减 · 高频召回可晋升 T3"
    },
    1: {
      label: "T1 瞬时",
      short: "T1",
      title: "🕒 Tier 1 · 瞬时短时记忆",
      desc: "瞬时记忆（48 小时后过期）",
      cls: "tier-1",
      hint: "48 小时过期 · 今日繁忙成果/短期碎片快速代谢"
    }
  };
  /* 分组渲染顺序：T3 → T2 → T1 */
  var TIER_ORDER = [3, 2, 1];

  var S = {
    app: null,
    container: null,
    destroyed: false,
    memories: [],
    stats: null,
    users: [],
    /* v2.8：T1 瞬时记忆条数上限（来自 memories/list 的 data.tier1_limit，默认 30） */
    tier1Limit: 30,
    tier: "",
    keyword: "",
    userId: "",
    busy: false,
    searchTimer: null,
    /* 分组折叠状态：{ "3": true } 表示用户手动折叠该分组；未记录时非空分组默认展开、空分组默认收起 */
    collapsed: {}
  };

  /* ------------------------------------------------------------------ *
   * 工具
   * ------------------------------------------------------------------ */
  function esc(str) {
    return window.OY_ESC ? window.OY_ESC(str) : String(str == null ? "" : str);
  }

  function icon(name, size) {
    return typeof window.icon === "function" ? window.icon(name, size || 16) : "";
  }

  function num(value, fallback) {
    var n = Number(value);
    return isFinite(n) ? n : fallback || 0;
  }

  function pad2(n) {
    return n < 10 ? "0" + n : String(n);
  }

  function fmtAgo(seconds) {
    var ts = num(seconds, 0);
    if (!ts) return "";
    var now = Math.floor(Date.now() / 1000);
    var diff = now - ts;
    if (diff < 0) diff = 0;
    if (diff < 60) return "刚刚";
    if (diff < 3600) return Math.floor(diff / 60) + " 分钟前";
    if (diff < 86400) return Math.floor(diff / 3600) + " 小时前";
    if (diff < 86400 * 30) return Math.floor(diff / 86400) + " 天前";
    var d = new Date(ts * 1000);
    return d.getFullYear() + "-" + pad2(d.getMonth() + 1) + "-" + pad2(d.getDate());
  }

  function tierMeta(tier) {
    return TIER_META[Number(tier)] || TIER_META[2];
  }

  function splitKeywords(keywords) {
    return String(keywords == null ? "" : keywords)
      .split(/[,，、/;；|]+/)
      .map(function (item) {
        return item.trim();
      })
      .filter(Boolean);
  }

  function vitalityColor(value) {
    var vitality = num(value, 0);
    if (vitality >= 60) return "var(--primary)";
    if (vitality >= 30) return "var(--warning)";
    return "var(--danger)";
  }

  function soulId() {
    return (S.app && S.app.state && S.app.state.currentSoulId) || "";
  }

  /* ------------------------------------------------------------------ *
   * 统计 / 概览条
   * ------------------------------------------------------------------ */
  /** 层级计数：优先使用后端 data.stats（全局计数），缺失时按当前 memories 统计 */
  function statCounts() {
    var stats = S.stats || {};
    var local = { 3: 0, 2: 0, 1: 0 };
    S.memories.forEach(function (memory) {
      var tier = Number(memory.memory_tier) || 2;
      if (local[tier] != null) local[tier] += 1;
    });
    var tier3 = stats.tier3_count != null ? num(stats.tier3_count, 0) : local[3];
    var tier2 = stats.tier2_count != null ? num(stats.tier2_count, 0) : local[2];
    var tier1 = stats.tier1_count != null ? num(stats.tier1_count, 0) : local[1];
    var total =
      stats.total != null ? num(stats.total, 0) : tier3 + tier2 + tier1 || S.memories.length;
    return { "3": tier3, "2": tier2, "1": tier1, total: total };
  }

  function summaryItemHtml(value, label, count) {
    var current = value === "all" ? "" : String(value);
    var active = current === String(S.tier);
    return (
      '<button type="button" class="memory-summary-item' +
      (active ? " is-active" : "") +
      '" data-action="set-tier" data-tier="' +
      esc(value) +
      '" aria-pressed="' +
      (active ? "true" : "false") +
      '"><span>' +
      esc(label) +
      "</span><b>" +
      esc(count) +
      "</b></button>"
    );
  }

  function summaryBarHtml(counts) {
    var total = num(counts["3"], 0) + num(counts["2"], 0) + num(counts["1"], 0);
    if (total <= 0) {
      return '<div class="memory-summary-bar is-empty" aria-hidden="true"></div>';
    }
    var items = [
      { cls: "seg-t3", value: num(counts["3"], 0) },
      { cls: "seg-t2", value: num(counts["2"], 0) },
      { cls: "seg-t1", value: num(counts["1"], 0) }
    ];
    return (
      '<div class="memory-summary-bar" aria-hidden="true">' +
      items
        .map(function (item) {
          if (item.value <= 0) return "";
          var width = (item.value / total) * 100;
          return (
            '<i class="seg ' + item.cls + '" style="width:' + width.toFixed(2) + '%"></i>'
          );
        })
        .join("") +
      "</div>"
    );
  }

  function summaryStripHtml() {
    var counts = statCounts();
    return (
      '<div class="memory-summary-strip" data-role="memory-summary">' +
      '<div class="memory-summary-items">' +
      summaryItemHtml("all", "全部", counts.total) +
      summaryItemHtml("3", "💎 T3 核心", counts["3"]) +
      summaryItemHtml("2", "📅 T2 近期", counts["2"]) +
      summaryItemHtml("1", "🕒 T1 瞬时", counts["1"]) +
      "</div>" +
      summaryBarHtml(counts) +
      "</div>"
    );
  }

  /* ------------------------------------------------------------------ *
   * 工具栏（关联用户筛选 / 搜索 / 注入预算）
   * ------------------------------------------------------------------ */
  function userNameOf(uid) {
    var key = String(uid || "").trim();
    if (!key) return "";
    for (var i = 0; i < S.users.length; i++) {
      var user = S.users[i] || {};
      var userKey = String(user.global_uid || user.user_id || user.id || "").trim();
      if (userKey !== key) continue;
      return String(user.display_name || user.nickname || "").trim();
    }
    return "";
  }

  function userOptionsHtml() {
    var options = '<option value="">全部用户</option>';
    var known = {};
    S.users.forEach(function (user) {
      user = user || {};
      var uid = String(user.global_uid || user.user_id || user.id || "").trim();
      if (!uid || known[uid]) return;
      known[uid] = true;
      var name = String(user.display_name || user.nickname || "").trim() || uid;
      options +=
        '<option value="' +
        esc(uid) +
        '"' +
        (uid === S.userId ? " selected" : "") +
        ">" +
        esc(name + "（" + uid + "）") +
        "</option>";
    });
    /* 记忆里出现、但用户表里没有的 user_id 也补进下拉，保证筛选能力不丢 */
    S.memories.forEach(function (memory) {
      var uid = String((memory && memory.user_id) || "").trim();
      if (!uid || known[uid]) return;
      known[uid] = true;
      options +=
        '<option value="' +
        esc(uid) +
        '"' +
        (uid === S.userId ? " selected" : "") +
        ">" +
        esc(uid) +
        "</option>";
    });
    return options;
  }

  function injectionBudgetHint() {
    var stats = S.stats || {};
    var injected = num(stats.max_injected_memories, 5);
    var budget = num(stats.max_memory_chars_budget, 400);
    var windowSize = num(stats.sliding_window_size, 12);
    return (
      '<span class="hint" data-tip="上下文预算：每次对话最多注入 ' +
      injected +
      " 条记忆 / " +
      budget +
      " 字；会话滑窗保留最近 " +
      windowSize +
      ' 条消息">每次注入 ≤' +
      esc(injected) +
      " 条 · " +
      esc(budget) +
      " 字</span>"
    );
  }

  function toolbarHtml() {
    return (
      '<div class="card" data-role="memory-toolbar"><div class="card-body tight">' +
      '<div class="row wrap grow">' +
      '<div class="row" style="gap:6px;flex:0 0 auto">' +
      icon("filter", 13) +
      '<select class="select input-sm memory-user-select" data-role="user-filter" data-tip="按关联用户筛选记忆">' +
      userOptionsHtml() +
      "</select>" +
      "</div>" +
      '<div class="row grow" style="min-width:180px;max-width:340px">' +
      icon("search", 14) +
      '<input class="input input-sm" type="text" data-role="search-input" placeholder="搜索记忆内容 / 关键词，回车立即搜索" value="' +
      esc(S.keyword) +
      '" />' +
      "</div>" +
      '<div class="row grow" style="justify-content:flex-end">' +
      injectionBudgetHint() +
      "</div>" +
      "</div>" +
      '<div class="row-between mt-8"><span class="text-xs muted" data-tip="Tier 1 瞬时记忆创建 48 小时后过期且每角色最多保留 ' +
      esc(num(S.tier1Limit, 30)) +
      ' 条；Tier 2 近期记忆活力值随时间衰减；Tier 3 核心记忆永久保留并优先注入。">T1 48h 过期 · 上限 ' +
      esc(num(S.tier1Limit, 30)) +
      " 条 · T2 活力衰减 · T3 永久保留</span>" +
      '<span class="text-xs muted">分组头可点击折叠 / 展开</span>' +
      "</div></div></div>"
    );
  }

  /* ------------------------------------------------------------------ *
   * 分层分组列表
   * ------------------------------------------------------------------ */
  function userLabelHtml(memory) {
    var uid = String((memory && memory.user_id) || "").trim();
    if (!uid) {
      return (
        '<span class="memory-user" data-tip="无关联用户（全局 / 自身记忆）">🌐 全局/自身</span>'
      );
    }
    var name = userNameOf(uid) || uid;
    return (
      '<span class="memory-user" data-tip="' +
      esc("关联用户 " + uid) +
      '">👤 ' +
      esc(name) +
      "</span>"
    );
  }

  /**
   * T1 瞬时记忆不再展示活力条：按 created_at 计算 48h 过期倒计时（契约 §5.6）。
   * 服务端在遗忘 GC 中删除创建超过 48h 的 T1，这里展示「⏳ 剩余 Xh 清除」。
   */
  function tier1CountdownHtml(memory) {
    var created = num(memory && memory.created_at, 0);
    if (!created) {
      return (
        '<span class="memory-vitality" data-tip="Tier 1 瞬时记忆创建后 48 小时过期，由遗忘清理 GC 删除">⏳ 48h 内清除</span>'
      );
    }
    var elapsed = Date.now() / 1000 - created;
    if (elapsed < 0) elapsed = 0;
    var remain = Math.max(0, 48 - elapsed / 3600);
    var text = remain >= 10 ? String(Math.round(remain)) : remain.toFixed(1);
    return (
      '<span class="memory-vitality" data-tip="' +
      esc("Tier 1 瞬时记忆创建后 48 小时过期，剩余约 " + text + " 小时由遗忘清理 GC 删除") +
      '">⏳ 剩余 ' +
      esc(text) +
      "h 清除</span>"
    );
  }

  function vitalityHtml(memory, tier) {
    var tierNum = Number(tier) || 2;
    if (tierNum === 3) {
      return (
        '<span class="memory-vitality is-permanent" data-tip="Tier 3 核心记忆永久保留，免疫每日遗忘清理">永久保存</span>'
      );
    }
    if (tierNum === 1) return tier1CountdownHtml(memory);
    var vitality = Math.max(0, Math.min(100, num(memory.vitality, 0)));
    return (
      '<span class="memory-vitality" data-tip="活力值：随时间衰减，低于 0 会被遗忘清理 GC 物理删除">' +
      '<span class="mini-bar"><i style="width:' +
      vitality.toFixed(1) +
      "%;background:" +
      vitalityColor(vitality) +
      '"></i></span><span class="memory-vitality-pct">' +
      Math.round(vitality) +
      "%</span></span>"
    );
  }

  function tierSwitchHtml(memory, tier) {
    var current = Number(tier) || 2;
    var buttons = TIER_ORDER.filter(function (value) {
      return value !== current;
    })
      .sort(function (a, b) {
        return a - b;
      })
      .map(function (value) {
        var meta = tierMeta(value);
        var direction = value > current ? " is-up" : " is-down";
        var tip = value > current ? "提升为 " + meta.label + " 记忆" : "降级为 " + meta.label + " 记忆";
        return (
          '<button type="button" class="tier-btn' +
          direction +
          '" data-action="switch-tier" data-id="' +
          esc(memory.id) +
          '" data-tier="' +
          esc(value) +
          '" data-tip="' +
          esc(tip) +
          '" aria-label="' +
          esc(tip) +
          '">' +
          esc(meta.short) +
          "</button>"
        );
      })
      .join("");
    return '<span class="tier-switch" data-role="tier-switch">' + buttons + "</span>";
  }

  function memoryRowHtml(memory) {
    var tier = Number(memory.memory_tier) || 2;
    var meta = tierMeta(tier);
    var importance = Math.max(1, Math.min(10, Math.round(num(memory.importance, 5))));
    var content = String(memory.content == null ? "" : memory.content);
    var keywords = splitKeywords(memory.keywords);
    var keywordHtml = keywords
      .map(function (keyword) {
        return '<span class="kw">#' + esc(keyword) + "</span>";
      })
      .join("");
    var recall = Math.max(0, Math.round(num(memory.access_count, 0)));
    var when = fmtAgo(memory.last_accessed_at) || fmtAgo(memory.created_at);
    var recallText = "召回 " + recall + " 次" + (when ? " · " + when : "");
    return (
      '<div class="memory-row" data-memory-id="' +
      esc(memory.id) +
      '" data-tier="' +
      esc(tier) +
      '">' +
      /* 列1：层级 + 重要度 */
      '<div class="memory-row-tier">' +
      '<span class="tier-badge ' +
      meta.cls +
      '">' +
      esc(meta.short) +
      "</span>" +
      '<span class="memory-importance" data-tip="重要度 1-10，影响记忆注入优先级">★ ' +
      esc(importance) +
      "/10</span></div>" +
      /* 列2：正文 + 关键词 */
      '<div class="memory-row-main">' +
      '<p class="memory-row-content" data-tip="' +
      esc(content) +
      '">' +
      esc(content) +
      "</p>" +
      (keywordHtml
        ? '<div class="memory-row-keywords">' + keywordHtml + "</div>"
        : "") +
      "</div>" +
      /* 列3：关联用户 + 活力 + 召回 */
      '<div class="memory-row-meta">' +
      userLabelHtml(memory) +
      vitalityHtml(memory, tier) +
      '<span class="memory-recall">' +
      esc(recallText) +
      "</span></div>" +
      /* 列4：升降级 + 编辑 + 删除 */
      '<div class="memory-row-actions">' +
      tierSwitchHtml(memory, tier) +
      '<button class="icon-btn" type="button" data-action="edit-memory" data-id="' +
      esc(memory.id) +
      '" data-tip="编辑记忆" aria-label="编辑记忆">' +
      icon("edit", 13) +
      "</button>" +
      '<button class="icon-btn" type="button" data-action="delete-memory" data-id="' +
      esc(memory.id) +
      '" data-tip="删除记忆" aria-label="删除记忆">' +
      icon("trash", 13) +
      "</button>" +
      "</div></div>"
    );
  }

  function isGroupCollapsed(tier, rows) {
    var key = String(tier);
    if (Object.prototype.hasOwnProperty.call(S.collapsed, key)) {
      return S.collapsed[key] === true;
    }
    /* 空分组默认收起；非空分组默认展开 */
    return !rows || rows.length === 0;
  }

  /** 分组头提示：T1 额外展示「上限 N 条」（v2.8 §5.6） */
  function groupHintText(tier) {
    var meta = tierMeta(tier);
    var hint = String(meta.hint || "");
    if (Number(tier) === 1) {
      hint += " · 上限 " + num(S.tier1Limit, 30) + " 条";
    }
    return hint;
  }

  function groupHtml(tier) {
    var meta = tierMeta(tier);
    var counts = statCounts();
    var rows = S.memories.filter(function (memory) {
      return (Number(memory.memory_tier) || 2) === Number(tier);
    });
    var count = counts[String(tier)] != null ? counts[String(tier)] : rows.length;
    var collapsed = isGroupCollapsed(tier, rows);
    var body = rows.length
      ? rows.map(memoryRowHtml).join("")
      : '<div class="memory-group-empty">暂无 ' +
        esc(meta.label) +
        " 记忆" +
        (S.keyword || S.userId ? "（当前筛选条件下）" : "") +
        "</div>";
    return (
      '<div class="memory-group' +
      (collapsed ? " is-collapsed" : "") +
      '" data-tier="' +
      esc(tier) +
      '">' +
      '<button type="button" class="memory-group-head" data-role="group-toggle" data-tier="' +
      esc(tier) +
      '" aria-expanded="' +
      (collapsed ? "false" : "true") +
      '">' +
      '<span class="memory-group-caret" aria-hidden="true"></span>' +
      '<span class="memory-group-title">' +
      esc(meta.title) +
      "</span>" +
      '<span class="memory-group-count">' +
      esc(count) +
      "</span>" +
      '<span class="memory-group-hint">' +
      esc(groupHintText(tier)) +
      "</span></button>" +
      '<div class="memory-group-body">' +
      body +
      "</div></div>"
    );
  }

  function emptyStateHtml() {
    var filtering = !!(S.keyword || S.tier || S.userId);
    return (
      '<div class="card" data-role="memory-empty"><div class="empty">' +
      icon("memory", 32) +
      '<div class="empty-title">' +
      (filtering ? "没有符合筛选条件的记忆" : "还没有任何记忆") +
      "</div>" +
      '<div class="empty-text">' +
      (filtering
        ? "试试切换层级 / 关联用户，或清空搜索关键词。"
        : "可以在会话页点「提炼」让决策模型从聊天滑窗里总结记忆，也可以手动新增一条。") +
      "</div>" +
      '<div class="row mt-12">' +
      '<button class="btn btn-outline" type="button" data-action="extract">' +
      icon("wand", 13) +
      "从会话提炼</button>" +
      '<button class="btn btn-outline" type="button" data-action="create">' +
      icon("plus", 13) +
      "新增记忆</button></div>" +
      "</div></div>"
    );
  }

  function listHtml() {
    if (!S.memories.length) return emptyStateHtml();
    var tiers = S.tier ? [Number(S.tier)] : TIER_ORDER;
    return (
      '<div class="memory-groups" data-role="memory-groups">' +
      tiers
        .filter(function (tier) {
          return TIER_META[tier] != null;
        })
        .map(groupHtml)
        .join("") +
      "</div>"
    );
  }

  function pageHeadHtml() {
    var total = statCounts().total;
    return (
      '<div class="page-head"><h1 class="page-title">' +
      icon("memory", 18) +
      "记忆" +
      '<span class="card-sub">' +
      esc(total) +
      " 条分级记忆</span></h1>" +
      '<div class="page-actions">' +
      '<button class="btn btn-outline btn-sm" type="button" data-action="extract">' +
      icon("wand", 13) +
      "提炼</button>" +
      '<button class="btn btn-outline btn-sm" type="button" data-action="gc" data-tip="运行每日遗忘清理：T1 过期删除 / T2 活力衰减 / T3 访问强化晋升">' +
      icon("refresh", 13) +
      "GC</button>" +
      '<button class="btn btn-outline btn-sm" type="button" data-action="reload">' +
      icon("refresh", 13) +
      "刷新</button>" +
      '<button class="btn btn-primary btn-sm" type="button" data-action="create">' +
      icon("plus", 13) +
      "新增</button>" +
      "</div></div>"
    );
  }

  function pageHtml() {
    return (
      pageHeadHtml() +
      '<div data-role="memory-summary-wrap">' +
      summaryStripHtml() +
      "</div>" +
      toolbarHtml() +
      '<div data-role="memory-list-wrap">' +
      listHtml() +
      "</div>"
    );
  }

  /* ------------------------------------------------------------------ *
   * 数据加载 / 局部刷新
   * ------------------------------------------------------------------ */
  async function fetchMemories(app) {
    var params = {};
    if (soulId()) params.soul_id = soulId();
    if (S.tier) params.memory_tier = S.tier;
    if (S.keyword) params.keyword = S.keyword;
    if (S.userId) params.user_id = S.userId;
    var data = await app.apiGet("memories/list", params);
    if (!data || typeof data !== "object") throw new Error("GET memories/list 返回为空");
    S.memories = Array.isArray(data.memories) ? data.memories : [];
    S.stats = data.stats && typeof data.stats === "object" ? data.stats : null;
    /* v2.8：T1 每角色上限（配置 memory_and_context_config.max_tier1_memories，默认 30） */
    S.tier1Limit = data.tier1_limit != null ? Math.max(0, num(data.tier1_limit, 30)) : 30;
  }

  /** 关联用户下拉数据源：失败时静默降级（用记忆里的 user_id 充当选项） */
  async function fetchUsers(app) {
    try {
      if (!app || typeof app.apiGet !== "function") return;
      var data = await app.apiGet("users/list", {});
      S.users = data && Array.isArray(data.users) ? data.users : [];
    } catch (err) {
      S.users = [];
      console.warn("[OnlyYou] 记忆页加载用户列表失败（关联用户筛选降级为 uid）：", err);
    }
  }

  function refreshView() {
    if (!S.container || S.destroyed) return;
    var summaryWrap = S.container.querySelector('[data-role="memory-summary-wrap"]');
    var listWrap = S.container.querySelector('[data-role="memory-list-wrap"]');
    if (!summaryWrap && !listWrap) {
      /* 加载失败态下重试成功：容器里还没有列表骨架，直接整页重绘 */
      paint();
      return;
    }
    if (summaryWrap) summaryWrap.innerHTML = summaryStripHtml();
    if (listWrap) listWrap.innerHTML = listHtml();
    var titleSub = S.container.querySelector(".page-head .card-sub");
    if (titleSub) titleSub.textContent = statCounts().total + " 条分级记忆";
    var userSelect = S.container.querySelector('[data-role="user-filter"]');
    if (userSelect) userSelect.value = S.userId;
  }

  function paint() {
    if (!S.container || S.destroyed) return;
    var scroller = S.container.closest ? S.container.closest(".main") : null;
    var top = scroller ? scroller.scrollTop : 0;
    S.container.innerHTML = pageHtml();
    if (scroller) scroller.scrollTop = top;
  }

  async function reloadMemories(showToast) {
    if (!S.app) return;
    try {
      await fetchMemories(S.app);
      refreshView();
      if (showToast) S.app.toast("记忆列表已刷新");
    } catch (err) {
      S.app.toast("刷新记忆失败：" + (err.message || err), true);
    }
  }

  /* ------------------------------------------------------------------ *
   * 表单：新增 / 编辑
   * ------------------------------------------------------------------ */
  function formHtml(memory) {
    var isEdit = !!memory;
    var content = isEdit ? memory.content || "" : "";
    var keywords = isEdit ? memory.keywords || "" : "";
    var importance = isEdit ? num(memory.importance, 8) : 8;
    var tier = isEdit ? Number(memory.memory_tier) || 3 : 3;
    return (
      '<div class="field"><span class="field-label">记忆内容 <span class="req">*</span></span>' +
      '<textarea class="textarea" rows="4" data-role="content" placeholder="例如：阿泽严重海鲜过敏，聚餐千万别点海鲜">' +
      esc(content) +
      "</textarea>" +
      '<div class="field-help">按核心记忆设计，内容会持久化为单条 ≤60 字的事实短句；更长的说明请拆成多条。</div></div>' +
      '<div class="field"><span class="field-label">关键词</span>' +
      '<input class="input" type="text" data-role="keywords" value="' +
      esc(keywords) +
      '" placeholder="逗号分隔，例如：海鲜过敏,饮食,健康" />' +
      '<div class="field-help">关键词用于记忆召回匹配，建议 2-4 个。</div></div>' +
      '<div class="field-grid">' +
      '<div class="field"><span class="field-label">重要度</span>' +
      '<div class="slider-row"><input class="slider" type="range" min="1" max="10" step="1" data-role="importance" value="' +
      importance +
      '" /><span class="slider-value" data-role="importance-value">' +
      importance +
      "</span></div></div>" +
      '<div class="field"><span class="field-label">记忆分级</span>' +
      '<select class="select" data-role="memory-tier">' +
      [3, 2, 1]
        .map(function (value) {
          var meta = tierMeta(value);
          return (
            '<option value="' +
            value +
            '"' +
            (value === tier ? " selected" : "") +
            ">" +
            esc(meta.label + " · " + meta.desc) +
            "</option>"
          );
        })
        .join("") +
      "</select></div></div>"
    );
  }

  function readForm(bodyEl) {
    var contentEl = bodyEl.querySelector('[data-role="content"]');
    var keywordsEl = bodyEl.querySelector('[data-role="keywords"]');
    var importanceEl = bodyEl.querySelector('[data-role="importance"]');
    var tierEl = bodyEl.querySelector('[data-role="memory-tier"]');
    var content = contentEl ? String(contentEl.value || "").trim() : "";
    return {
      content: content,
      keywords: keywordsEl ? String(keywordsEl.value || "").trim() : "",
      importance: importanceEl ? Number(importanceEl.value) : 8,
      memory_tier: tierEl ? Number(tierEl.value) : 3
    };
  }

  function bindFormSliders(bodyEl) {
    var importanceEl = bodyEl.querySelector('[data-role="importance"]');
    var valueEl = bodyEl.querySelector('[data-role="importance-value"]');
    if (importanceEl && valueEl) {
      importanceEl.addEventListener("input", function () {
        valueEl.textContent = importanceEl.value;
      });
    }
  }

  function openCreateModal() {
    var app = S.app;
    if (!app) return;
    app.modal({
      title: "新增记忆",
      body: formHtml(null),
      actions: [
        { label: "取消" },
        {
          label: "保存",
          primary: true,
          icon: "save",
          onClick: async function (modalApp, bodyEl) {
            var form = readForm(bodyEl);
            if (!form.content) {
              modalApp.toast("记忆内容不能为空", true);
              return false;
            }
            var data = await modalApp.apiPost("memories/mutate", {
              action: "add",
              soul_id: soulId(),
              content: form.content,
              keywords: form.keywords,
              importance: form.importance,
              memory_tier: form.memory_tier
            });
            if (data && data.new_id != null) {
              modalApp.toast("已新增记忆 #" + data.new_id);
            } else {
              modalApp.toast("已新增记忆");
            }
            await reloadMemories(false);
            return true;
          }
        }
      ],
      onMount: function (bodyEl) {
        bindFormSliders(bodyEl);
      }
    });
  }

  function findMemory(id) {
    for (var i = 0; i < S.memories.length; i++) {
      if (String(S.memories[i].id) === String(id)) return S.memories[i];
    }
    return null;
  }

  function openEditModal(memoryId) {
    var app = S.app;
    var memory = findMemory(memoryId);
    if (!app || !memory) return;
    app.modal({
      title: "编辑记忆 #" + memory.id,
      body: formHtml(memory),
      actions: [
        { label: "取消" },
        {
          label: "保存",
          primary: true,
          icon: "save",
          onClick: async function (modalApp, bodyEl) {
            var form = readForm(bodyEl);
            if (!form.content) {
              modalApp.toast("记忆内容不能为空", true);
              return false;
            }
            await modalApp.apiPost("memories/mutate", {
              action: "edit",
              id: memory.id,
              soul_id: soulId(),
              content: form.content,
              keywords: form.keywords,
              importance: form.importance,
              memory_tier: form.memory_tier
            });
            modalApp.toast("已保存记忆 #" + memory.id);
            await reloadMemories(false);
            return true;
          }
        }
      ],
      onMount: function (bodyEl) {
        bindFormSliders(bodyEl);
      }
    });
  }

  /* ------------------------------------------------------------------ *
   * 提炼 / GC / 改级 / 删除
   * ------------------------------------------------------------------ */
  function openExtractModal() {
    var app = S.app;
    if (!app) return;
    var knownUmos = (app.state && app.state.knownUmos) || [];
    var options = '<option value="">自动选择最近活跃会话</option>';
    knownUmos.forEach(function (item) {
      var umo = item && item.umo ? String(item.umo) : "";
      if (!umo) return;
      var tags = [];
      if (item.is_l3_private) tags.push("L3 私聊");
      if (item.bound_soul_id) tags.push("已绑定 " + item.bound_soul_id);
      options +=
        '<option value="' +
        esc(umo) +
        '">' +
        esc(umo + (tags.length ? "（" + tags.join(" · ") + "）" : "")) +
        "</option>";
    });
    var body =
      '<div class="field"><span class="field-label">从哪个会话滑窗提炼</span>' +
      '<select class="select" data-role="umo">' +
      options +
      "</select>" +
      '<div class="field-help">调用决策模型从最近 15 轮对话中提炼值得长期记住的事实与偏好；不指定 UMO 时自动选择最近活跃会话。</div></div>' +
      '<div class="card" style="background:var(--surface-2);border:0"><div class="card-body tight"><div class="text-xs muted">提示：提炼只读取会话滑窗，不会重复写入已有记忆；结果可以在列表里继续编辑、升降级或删除。</div></div></div>';
    app.modal({
      title: "从会话滑窗提炼记忆",
      body: body,
      actions: [
        { label: "取消" },
        {
          label: "开始提炼",
          primary: true,
          icon: "wand",
          onClick: async function (modalApp, bodyEl) {
            var selectEl = bodyEl.querySelector('[data-role="umo"]');
            var umo = selectEl ? String(selectEl.value || "").trim() : "";
            modalApp.toast("正在提炼记忆，请稍候…");
            try {
              var data = await modalApp.apiPost("memories/mutate", {
                action: "extract",
                soul_id: soulId(),
                umo: umo
              });
              var count = data && data.extracted_count != null ? Number(data.extracted_count) : 0;
              modalApp.toast("提炼完成：新增 " + count + " 条记忆");
            } catch (err) {
              modalApp.toast("提炼失败：" + (err.message || err), true);
              return false;
            }
            await reloadMemories(false);
            return true;
          }
        }
      ]
    });
  }

  function confirmGc() {
    var app = S.app;
    if (!app) return;
    app
      .confirm({
        title: "运行遗忘清理 (GC)",
        message:
          "将执行一次每日遗忘清理：Tier 1 超过 48 小时的记忆会被删除，Tier 2 按重要度与新鲜度衰减活力值（活力归零则物理删除），访问次数达到阈值的记忆晋升为 Tier 3。此操作不可撤销，是否继续？",
        confirmLabel: "开始 GC",
        onConfirm: async function () {
          try {
            var data = await app.apiPost("memories/mutate", {
              action: "gc",
              soul_id: soulId()
            });
            var stats = (data && data.gc_stats) || {};
            app.toast(
              "GC 完成：清理 T1 " +
                num(stats.deleted_tier1, 0) +
                " 条 / 删除 T2 " +
                num(stats.deleted_tier2, 0) +
                " 条 / 晋升 T3 " +
                num(stats.promoted_to_tier3, 0) +
                " 条"
            );
            await reloadMemories(false);
          } catch (err) {
            app.toast("GC 失败：" + (err.message || err), true);
            throw err;
          }
        }
      })
      .then(function () {
        /* 结果已通过 toast 提示 */
      });
  }

  /** 升降级：改级沿用既有 memories/mutate API（升 T3 走 pin_core，降级走 edit.memory_tier） */
  async function switchTier(memoryId, tier, buttonEl) {
    var app = S.app;
    if (!app) return;
    var targetTier = Number(tier);
    if (targetTier !== 1 && targetTier !== 2 && targetTier !== 3) return;
    var memory = findMemory(memoryId);
    var meta = tierMeta(targetTier);
    if (buttonEl) buttonEl.classList.add("is-loading");
    try {
      if (targetTier === 3) {
        await app.apiPost("memories/mutate", {
          action: "pin_core",
          id: Number(memoryId),
          soul_id: soulId()
        });
      } else {
        await app.apiPost("memories/mutate", {
          action: "edit",
          id: Number(memoryId),
          soul_id: soulId(),
          memory_tier: targetTier
        });
      }
      app.toast("已将记忆 #" + memoryId + " 调整为 " + meta.label);
      await reloadMemories(false);
    } catch (err) {
      app.toast("调整记忆分级失败：" + (err.message || err), true);
      if (buttonEl) buttonEl.classList.remove("is-loading");
      if (memory) console.warn("[OnlyYou] 调整记忆分级失败：", memory);
    }
  }

  function confirmDeleteMemory(id) {
    var app = S.app;
    var memory = findMemory(id);
    if (!app) return;
    var preview = memory ? String(memory.content || "").slice(0, 60) : "";
    app
      .confirm({
        title: "删除记忆",
        message: "确定物理删除这条记忆吗？删除后不可恢复。\n\n「" + preview + (preview.length >= 60 ? "…" : "") + "」",
        danger: true,
        confirmLabel: "删除",
        onConfirm: async function () {
          await app.apiPost("memories/mutate", {
            action: "delete",
            id: Number(id),
            soul_id: soulId()
          });
          app.toast("已删除记忆 #" + id);
          await reloadMemories(false);
        }
      })
      .then(function () {
        /* 结果已通过 toast 提示 */
      });
  }

  /* ------------------------------------------------------------------ *
   * 事件
   * ------------------------------------------------------------------ */
  function toggleGroup(tier) {
    if (!S.container || S.destroyed) return;
    var group = S.container.querySelector('.memory-group[data-tier="' + tier + '"]');
    if (!group) return;
    var collapsed = group.classList.toggle("is-collapsed");
    var head = group.querySelector('[data-role="group-toggle"]');
    if (head) head.setAttribute("aria-expanded", collapsed ? "false" : "true");
    S.collapsed[String(tier)] = collapsed;
  }

  function handleClick(event) {
    var target = event.target;
    if (!target || !target.closest) return;
    var app = S.app;
    if (!app) return;

    var groupHead = target.closest('[data-role="group-toggle"]');
    if (groupHead) {
      toggleGroup(groupHead.getAttribute("data-tier"));
      return;
    }

    var actionEl = target.closest("[data-action]");
    if (!actionEl) return;
    var action = actionEl.getAttribute("data-action");
    if (action === "reload") {
      reloadMemories(true);
    } else if (action === "create") {
      openCreateModal();
    } else if (action === "extract") {
      openExtractModal();
    } else if (action === "gc") {
      confirmGc();
    } else if (action === "set-tier") {
      setTier(actionEl.getAttribute("data-tier"));
    } else if (action === "edit-memory") {
      openEditModal(actionEl.getAttribute("data-id"));
    } else if (action === "delete-memory") {
      confirmDeleteMemory(actionEl.getAttribute("data-id"));
    } else if (action === "switch-tier") {
      switchTier(
        actionEl.getAttribute("data-id"),
        actionEl.getAttribute("data-tier"),
        actionEl
      );
    }
  }

  async function setTier(tier) {
    var next = tier == null || tier === "all" ? "" : String(tier);
    if (next === String(S.tier)) return;
    S.tier = next;
    S.collapsed = {};
    if (!S.app) return;
    try {
      await fetchMemories(S.app);
      refreshView();
    } catch (err) {
      S.app.toast("切换记忆分级失败：" + (err.message || err), true);
    }
  }

  function scheduleSearch() {
    if (S.searchTimer) {
      clearTimeout(S.searchTimer);
      S.searchTimer = null;
    }
    S.searchTimer = setTimeout(function () {
      S.searchTimer = null;
      if (!S.app || S.destroyed) return;
      fetchMemories(S.app)
        .then(refreshView)
        .catch(function (err) {
          if (S.app) S.app.toast("搜索记忆失败：" + (err.message || err), true);
        });
    }, 400);
  }

  function applyUserFilter(app) {
    if (!app || S.destroyed) return;
    fetchMemories(app)
      .then(refreshView)
      .catch(function (err) {
        app.toast("按关联用户筛选失败：" + (err.message || err), true);
      });
  }

  function bindEvents(container, app) {
    container.addEventListener("click", handleClick);
    container.addEventListener("input", function (event) {
      var target = event.target;
      if (!target || !target.getAttribute) return;
      if (target.getAttribute("data-role") === "search-input") {
        S.keyword = String(target.value || "").trim();
        scheduleSearch();
      }
    });
    container.addEventListener("change", function (event) {
      var target = event.target;
      if (!target || !target.getAttribute) return;
      if (target.getAttribute("data-role") !== "user-filter") return;
      S.userId = String(target.value || "").trim();
      applyUserFilter(app);
    });
    container.addEventListener("keydown", function (event) {
      var target = event.target;
      if (!target || !target.getAttribute) return;
      if (target.getAttribute("data-role") !== "search-input") return;
      if (event.key !== "Enter") return;
      event.preventDefault();
      S.keyword = String(target.value || "").trim();
      if (S.searchTimer) {
        clearTimeout(S.searchTimer);
        S.searchTimer = null;
      }
      if (!S.app) return;
      fetchMemories(S.app)
        .then(refreshView)
        .catch(function (err) {
          if (S.app) S.app.toast("搜索记忆失败：" + (err.message || err), true);
        });
    });
  }

  /* ------------------------------------------------------------------ *
   * 生命周期
   * ------------------------------------------------------------------ */
  async function render(container, app) {
    S.app = app;
    S.container = container;
    S.destroyed = false;
    S.memories = [];
    S.stats = null;
    S.users = [];
    S.tier1Limit = 30;
    S.tier = "";
    S.keyword = "";
    S.userId = "";
    S.busy = false;
    S.collapsed = {};
    if (S.searchTimer) {
      clearTimeout(S.searchTimer);
      S.searchTimer = null;
    }

    container.innerHTML =
      '<div class="loading"><span class="spinner"></span><span>加载记忆库…</span></div>';
    bindEvents(container, app);
    try {
      await Promise.all([fetchMemories(app), fetchUsers(app)]);
      if (S.destroyed) return;
      paint();
    } catch (err) {
      console.error("[OnlyYou] 记忆页渲染失败：", err);
      container.innerHTML =
        '<div class="page-head"><h1 class="page-title">' +
        icon("memory", 18) +
        "记忆</h1></div>" +
        '<div class="card"><div class="empty">' +
        icon("alert", 32) +
        '<div class="empty-title">记忆库加载失败</div>' +
        '<div class="empty-text">' +
        esc((err && (err.message || err)) || "未知错误") +
        "</div>" +
        '<div class="row mt-12"><button class="btn btn-outline" type="button" data-action="reload">重试</button></div>' +
        "</div></div>";
    }
  }

  function destroy() {
    S.destroyed = true;
    if (S.searchTimer) {
      clearTimeout(S.searchTimer);
      S.searchTimer = null;
    }
    S.app = null;
    S.container = null;
    S.memories = [];
    S.stats = null;
    S.users = [];
    S.tier1Limit = 30;
    S.busy = false;
    S.keyword = "";
    S.tier = "";
    S.userId = "";
    S.collapsed = {};
  }

  window.OY_PAGES = window.OY_PAGES || {};
  window.OY_PAGES.memory = {
    id: "memory",
    title: "记忆",
    icon: "memory",
    render: render,
    destroy: destroy
  };
})();
