/**
 * OnlyYou Plugin Pages — 概览页 (`views/console/page-overview.js`)
 *
 * 数据源：GET `dashboard/overview`（新 24h 动态时间轴契约见 .team/NEW_TIMELINE_CONTRACT.md §6.3）。
 * 内容：
 *  ① 状态主卡：SVG 环形护甲表 + 状态名 + 四类行动徽章 + 当前活动 +
 *     BUSY_TASK 实时进度条 / 其它行动剩余时间进度条 + 右侧下一时间块预告；
 *  ② 24h 动态积木日程轴（OYCharts.scheduleAxis），点击色块弹「行动类型 + 标题 + 护甲」并 POST schedule/force_state；
 *  ③ 小卡：今日基调（含作息类型 chronotype）/ 微调配额 / 今日繁忙成果（Tier 1 短时记忆）/
 *     关系环形 / 记忆分级条形 / 模型健康灯。
 *
 * 注册：window.OY_PAGES.overview = { id,title,icon,render(container,ctx),destroy() }，ctx 即 window.app。
 */
(function () {
  "use strict";

  var pageState = {
    timer: null,
    remaining: 0,
    total: 0,
    cooldown: 0,
    data: null
  };

  function esc(str) {
    return window.OY_ESC ? window.OY_ESC(str) : String(str == null ? "" : str);
  }

  function icon(name, size) {
    return typeof window.icon === "function" ? window.icon(name, size || 16) : "";
  }

  function charts() {
    return window.OYCharts || {};
  }

  function num(value, fallback) {
    var n = Number(value);
    return isFinite(n) ? n : fallback || 0;
  }

  function clamp01(n) {
    return Math.max(0, Math.min(1, n));
  }

  /* ------------------------------------------------------------------ *
   * 四类行动（SLEEP / SURF / IDLE_ROAM / BUSY_TASK）共享工具
   * ------------------------------------------------------------------ */
  var CHRONOTYPE_LABELS = { early: "早起型", normal: "正常作息", night_owl: "夜猫子" };
  var STATE_LABELS = { IDLE: "空闲", BUSY: "繁忙", RESTING: "休息" };

  function chronotypeLabel(value) {
    var key = String(value || "normal").toLowerCase();
    return CHRONOTYPE_LABELS[key] || CHRONOTYPE_LABELS.normal;
  }

  function actionTypeOf(slot) {
    if (charts().actionTypeOf) return charts().actionTypeOf(slot);
    return "";
  }

  function actionLabel(type, short) {
    return charts().actionLabel ? charts().actionLabel(type, short) : "";
  }

  function actionClass(type) {
    return charts().actionClass ? charts().actionClass(type) : "";
  }

  function actionStateOf(type) {
    return charts().actionState ? charts().actionState(type) : "";
  }

  function actionBadgeHtml(type, text) {
    var cls = actionClass(type);
    return (
      '<span class="action-badge' +
      (cls ? " " + cls : "") +
      '" data-action-type="' +
      esc(type || "") +
      '">' +
      esc(text || actionLabel(type, false) || "行动") +
      "</span>"
    );
  }

  /** 读取当前 state 的行动类型（缺字段时按 state/title 旧数据兜底） */
  function resolveActionType(state, slot) {
    var type = actionTypeOf(state || {}) || actionTypeOf(slot || {});
    if (type) return type;
    var stateId = String((state && state.state) || (slot && slot.state) || "").toUpperCase();
    if (stateId === "RESTING") return "SLEEP";
    if (stateId === "BUSY") return "BUSY_TASK";
    return "IDLE_ROAM";
  }

  /** 行动徽章文案：BUSY_TASK 追加实时进度（如「繁忙主业 · 65%」），其它只显示行动 label */
  function actionBadgeText(state, type) {
    var derived = actionLabel(type, false);
    var label = (state && state.action_type_label) || derived || actionLabel(type, true) || "行动";
    var pct = null;
    if (
      type === "BUSY_TASK" &&
      state &&
      state.progress_pct != null &&
      state.progress_pct !== ""
    ) {
      pct = Math.max(0, Math.min(100, Math.round(num(state.progress_pct, 0))));
    }
    return pct == null ? label : label + " · " + pct + "%";
  }

  function fmtHour(value) {
    return charts().fmtHour ? charts().fmtHour(value) : String(value || 0) + ":00";
  }

  /* ------------------------------------------------------------------ *
   * 计算辅助
   * ------------------------------------------------------------------ */
  function slotTotalSeconds(slot) {
    slot = slot || {};
    var start = num(slot.start_hour, 0);
    var end = num(slot.end_hour, 24);
    if (end <= start) end += 24;
    return Math.max(1, (end - start) * 3600);
  }

  function remainingRatio(state, slot) {
    var remaining = num(state && state.remaining_seconds, 0);
    var total = slotTotalSeconds(slot);
    return clamp01(1 - remaining / total);
  }

  function formatCooldown(seconds) {
    var total = Math.max(0, Math.round(num(seconds, 0)));
    if (!total) return "可微调";
    var hours = Math.floor(total / 3600);
    var mins = Math.floor((total % 3600) / 60);
    var secs = total % 60;
    function pad(n) {
      return n < 10 ? "0" + n : String(n);
    }
    return (hours ? hours + " 小时 " : "") + pad(mins) + " 分 " + pad(secs) + " 秒";
  }

  function roleLabel(role) {
    return (
      { main: "对话", decision: "决策", vision: "视频解析", tts: "语音" }[role] || role
    );
  }

  function modelHealthInfo(role, info) {
    info = info || {};
    if (role === "tts") {
      if (!info.mode || info.mode === "off") {
        return { cls: "light-off", text: "已关闭", tag: "TTS" };
      }
      return {
        cls: info.ok === false ? "light-err" : "light-ok",
        text: info.label || info.mode,
        tag: info.mode === "provider" ? "AstrBot TTS" : "自建 TTS"
      };
    }
    if (info.configured === false || info.mode === "unconfigured") {
      return { cls: "light-err", text: "未配置", tag: role === "main" ? "必填" : "" };
    }
    return {
      cls: info.ok === false ? "light-err" : info.inherited ? "light-warn" : "light-ok",
      text: info.label || [info.source_id, info.model].filter(Boolean).join(" / ") || "已配置",
      tag: info.inherited ? "继承主模型" : info.mode === "plugin" ? "插件自建" : "复用实例"
    };
  }

  /* ------------------------------------------------------------------ *
   * 片段构建
   * ------------------------------------------------------------------ */
  function ringBlock(state, slot) {
    var meta = getApp().stateMeta(state.state);
    var armor = Math.max(0, Math.min(100, num(state.armor, 0)));
    return charts().ringGauge({
      value: armor,
      max: 100,
      label: meta.label,
      sublabel: "护甲 " + armor,
      color: meta.color,
      size: 132
    });
  }

  var _app = null;
  function getApp() {
    return _app;
  }

  function vigorBlock(vigor, chronotype) {
    return (
      '<div class="stat-main"><span class="stat-value">' +
      esc(vigor.title || "日常一天") +
      '</span><span class="tag tag-primary" data-tip="角色作息类型">' +
      esc(chronotypeLabel(chronotype || vigor.chronotype)) +
      "</span></div>" +
      '<div class="stat-desc">' +
      esc(vigor.desc || "今天没有特别的基调，按部就班地过。") +
      "</div>"
    );
  }

  function quotaBlock(quota) {
    var used = num(quota.used, 0);
    var max = Math.max(1, num(quota.max, 2));
    return (
      '<div class="quota-row">' +
      charts().dots({ used: used, max: max }) +
      '<span class="quota-count">' +
      used +
      " / " +
      max +
      "</span>" +
      "</div>" +
      '<div class="stat-desc" data-role="quota-desc">今天还可微调 <strong>' +
      num(quota.remaining, Math.max(0, max - used)) +
      "</strong> 次 · 冷却 <span data-role=\"cooldown-text\">" +
      esc(formatCooldown(quota.cooldown_remaining_seconds)) +
      "</span></div>"
    );
  }

  /** 今日繁忙成果（BUSY_TASK 结算生成的 Tier 1 短时记忆） */
  function busyResultsBlock(results) {
    var list = (Array.isArray(results) ? results : []).filter(Boolean);
    if (!list.length) {
      return (
        '<div class="stat-empty">今天还没有繁忙成果 · 繁忙卡走完 100%（或被中途打断）时会结算一句话完成结果，写入 Tier 1 短时记忆</div>'
      );
    }
    var rows = list
      .slice(0, 4)
      .map(function (item) {
        var startText = item.start_time || fmtHour(item.start_hour);
        var endText = item.end_time || fmtHour(item.end_hour);
        var range = startText + "-" + endText;
        var pct =
          item.progress_pct != null && item.progress_pct !== ""
            ? Math.max(0, Math.min(100, Math.round(num(item.progress_pct, 0))))
            : null;
        return (
          '<div class="busy-result-item" data-tip="' +
          esc(range + " · " + (item.title || "繁忙事务") + (pct == null ? "" : " · 进度 " + pct + "%")) +
          '">' +
          '<div class="busy-result-head"><span class="busy-result-title">' +
          icon("check", 12) +
          esc(item.title || "繁忙事务") +
          '</span><span class="busy-result-meta">' +
          esc(range + (pct == null ? "" : " · " + pct + "%")) +
          "</span></div>" +
          '<div class="busy-result-text">' +
          esc(item.completion_result || "（今日完成结果待结算）") +
          "</div></div>"
        );
      })
      .join("");
    return (
      '<div class="busy-result-list">' +
      rows +
      "</div>" +
      (list.length > 4
        ? '<div class="stat-desc">共 ' + list.length + " 条已结算成果（Tier 1 短时记忆）</div>"
        : '<div class="stat-desc">每日随机完成结果 · 已写入 Tier 1 瞬时记忆</div>')
    );
  }

  function relationsBlock(counts) {
    var relations = (counts && counts.relations) || {};
    var items = [
      { label: "L3 核心", value: num(relations.l3, 0), color: "var(--tier-l3)" },
      { label: "L2 熟人", value: num(relations.l2, 0), color: "var(--tier-l2)" },
      { label: "L1 普通", value: num(relations.l1, 0), color: "var(--tier-l1)" }
    ];
    var total = items.reduce(function (acc, item) {
      return acc + item.value;
    }, 0);
    var legend = items
      .map(function (item) {
        return (
          '<div class="legend-item"><span class="legend-dot" style="background:' +
          item.color +
          '"></span><span class="legend-label">' +
          esc(item.label) +
          '</span><span class="legend-value">' +
          item.value +
          "</span></div>"
        );
      })
      .join("");
    return (
      '<div class="ring-pair">' +
      charts().donut({
        items: items,
        size: 104,
        centerValue: total,
        centerLabel: "总用户"
      }) +
      '<div class="legend">' +
      legend +
      "</div></div>"
    );
  }

  function memoriesBlock(counts) {
    var memories = (counts && counts.memories) || {};
    var tier3 = num(memories.tier3, 0);
    var tier2 = num(memories.tier2, 0);
    var tier1 = num(memories.tier1, 0);
    var max = Math.max(tier1, tier2, tier3, 1);
    return (
      charts().bars({
        items: [
          { label: "T3 核心", value: tier3, max: max, color: "var(--primary)" },
          { label: "T2 近期", value: tier2, max: max, color: "#6366f1" },
          { label: "T1 瞬时", value: tier1, max: max, color: "var(--border-strong)" }
        ]
      }) +
      '<div class="stat-desc">共 <strong>' +
      num(memories.total, tier1 + tier2 + tier3) +
      "</strong> 条记忆，Tier 3 不受遗忘清理影响</div>"
    );
  }

  function modelsBlock(models) {
    models = models || {};
    var roles = ["main", "decision", "vision", "tts"];
    var rows = roles
      .map(function (role) {
        var info = modelHealthInfo(role, models[role]);
        return (
          '<div class="health-row" data-tip="' +
          esc(roleLabel(role) + " · " + (info.tag || "") + " · " + info.text) +
          '">' +
          '<span class="health-role">' +
          esc(roleLabel(role)) +
          '</span><span class="status-light ' +
          info.cls +
          '"></span><span class="health-name">' +
          esc(info.text) +
          "</span></div>"
        );
      })
      .join("");
    return '<div class="model-health">' + rows + "</div>";
  }

  function nextSlotBlock(nextSlot) {
    if (!nextSlot) {
      return '<div class="next-preview-label">下一时间块</div><div class="next-preview-title muted">暂无预告</div>';
    }
    var meta = getApp().stateMeta(nextSlot.state);
    var nextType = resolveActionType(null, nextSlot);
    return (
      '<div class="next-preview-label">下一时间块</div>' +
      '<div class="next-preview-title">' +
      esc(nextSlot.title || "未知活动") +
      "</div>" +
      '<div class="next-preview-meta">' +
      actionBadgeHtml(nextType, actionLabel(nextType, true)) +
      '<span class="state-badge ' +
      meta.cls +
      '">' +
      meta.label +
      "</span></div>" +
      '<div class="next-preview-meta">' +
      icon("clock", 12) +
      esc(fmtHour(nextSlot.start_hour) + "-" + fmtHour(nextSlot.end_hour)) +
      " · 护甲 " +
      num(nextSlot.armor, 0) +
      (nextSlot.mutated ? " · 已微调" : "") +
      "</div>"
    );
  }

  /* ------------------------------------------------------------------ *
   * 主渲染
   * ------------------------------------------------------------------ */
  function buildHtml(app, data) {
    _app = app;
    var soul = data.soul || {};
    var state = data.state || {};
    var slot = currentSlot(data);
    var stateMeta = app.stateMeta(state.state || (slot && slot.state));
    var armor = Math.max(0, Math.min(100, num(state.armor, num(slot && slot.armor, 0))));
    var remaining = num(state.remaining_seconds, 0);
    var elapsedPct =
      state.progress != null ? clamp01(num(state.progress, 0)) : remainingRatio(state, slot);
    var vigor =
      data.vigor ||
      (data.schedule && data.schedule.run
        ? { title: data.schedule.run.vigor_title, desc: data.schedule.run.vigor_desc }
        : {});
    var quota = data.quota || {};
    var counts = data.counts || {};
    var models = data.models || app.state.modelStatus || {};
    var slots =
      (data.schedule && data.schedule.run && data.schedule.run.slots) || [];
    var nextSlot =
      (state && state.next_slot) || (data.schedule && data.schedule.next_slot) || null;
    var currentKey = state.slot_key || (data.schedule && data.schedule.current_slot && data.schedule.current_slot.slot_key) || "";
    var nowHour = app.nowHour();
    var soulName = soul.name || (app.state.currentSoul && app.state.currentSoul.name) || app.state.currentSoulId;
    var actionType = resolveActionType(state, slot);
    var isBusyTask = actionType === "BUSY_TASK";
    var busyPct = null;
    if (isBusyTask && state.progress_pct != null && state.progress_pct !== "") {
      busyPct = Math.max(0, Math.min(100, Math.round(num(state.progress_pct, 0))));
    }
    var completionResult = String(state.completion_result || (slot && slot.completion_result) || "");
    var startHour = state.start_hour != null ? state.start_hour : slot && slot.start_hour;
    var endHour = state.end_hour != null ? state.end_hour : slot && slot.end_hour;
    var chronotype = vigor.chronotype || (data.schedule && data.schedule.chronotype) || "";
    var busyResults = data.busy_results || (data.schedule && data.schedule.busy_results) || [];
    var elapsedDisplayPct = busyPct == null ? Math.round(elapsedPct * 100) : busyPct;

    var html = "";
    html +=
      '<div class="page-head"><h1 class="page-title">' +
      icon("gauge", 18) +
      "概览" +
      "</h1>" +
      '<div class="page-actions">' +
      '<button class="btn btn-ghost btn-sm" type="button" data-action="refresh-overview">' +
      icon("refresh", 14) +
      "刷新</button>" +
      '<button class="btn btn-primary btn-sm" type="button" data-action="quick-force">' +
      icon("zap", 14) +
      "切换行动" +
      "</button></div></div>";

    /* ① 状态主卡：状态名 + 行动类型徽章 + 当前标题 + 进度条 */
    html += '<section class="card status-card"><div class="status-main">';
    html += '<div class="ring-wrap">' + ringBlock(state, slot) + "</div>";
    html += '<div class="status-info">';
    html +=
      '<div class="status-head"><span class="state-badge ' +
      stateMeta.cls +
      '">' +
      stateMeta.label +
      "</span>" +
      actionBadgeHtml(actionType, actionBadgeText(state, actionType)) +
      '<span class="muted text-sm">' +
      esc(soulName || "") +
      "</span>" +
      (state.mutated ? '<span class="tag tag-primary">已微调</span>' : "") +
      "</div>";
    html +=
      '<div class="status-activity">' +
      esc(state.title || (slot && slot.title) || "正在过自己的小日子") +
      "</div>";
    html +=
      '<div class="status-slot-meta">' +
      icon("clock", 12) +
      esc((state.label || (slot && slot.label) || "当前时间块") + " · " + fmtHour(startHour) + "-" + fmtHour(endHour)) +
      '<span class="tag tag-muted">' +
      esc(slot && slot.slot_key ? slot.slot_key : "") +
      "</span></div>";
    if (isBusyTask) {
      html += '<div class="status-progress">';
      html +=
        '<div class="busy-progress"><span class="busy-progress-fill" data-role="busy-bar" style="width:' +
        elapsedDisplayPct.toFixed(2) +
        '%"></span></div>';
      html +=
        '<div class="progress-meta"><span>今日主业进度 <strong data-role="busy-pct">' +
        elapsedDisplayPct +
        "</strong>% · 剩余 <strong data-role=\"remaining-text\">" +
        esc(app.formatDuration(remaining)) +
        "</strong></span><span>" +
        esc(armor >= 70 ? "护甲很厚" : armor >= 40 ? "护甲一般" : "护甲很薄") +
        "</span></div>";
      if (completionResult) {
        html +=
          '<div class="busy-result-inline">' + icon("check", 12) + esc("完成结果：" + completionResult) + "</div>";
      }
      html += "</div>";
    } else {
      html += '<div class="status-progress">';
      html +=
        '<div class="progress"><span class="progress-fill st-fill" data-role="remaining-bar" style="width:' +
        (elapsedPct * 100).toFixed(2) +
        "%;background:" +
        stateMeta.color +
        '"></span></div>';
      html +=
        '<div class="progress-meta"><span>本时间块剩余 <strong data-role="remaining-text">' +
        esc(app.formatDuration(remaining)) +
        "</strong>（已过 " +
        Math.round(elapsedPct * 100) +
        "%）</span><span>" +
        esc(armor >= 70 ? "护甲很厚" : armor >= 40 ? "护甲一般" : "护甲很薄") +
        "</span></div></div>";
    }
    html +=
      '<div class="card-actions mt-4">' +
      '<button class="btn btn-outline btn-sm" type="button" data-action="quick-force">' +
      icon("zap", 13) +
      "调整该时间块</button>" +
      "</div>";
    html += "</div>";
    html += '<div class="next-preview">' + nextSlotBlock(nextSlot) + "</div>";
    html += "</div></section>";

    /* ② 24h 动态积木日程轴 */
    html += '<section class="card"><div class="card-head">';
    html +=
      '<div class="card-title">' +
      icon("calendar", 15) +
      '24 小时日程轴<span class="card-sub">动态时间块 · 点击色块切换行动类型与护甲</span></div>';
    html +=
      '<div class="axis-legend"><span class="legend-item"><span class="legend-dot" style="background:var(--state-idle)"></span>空闲</span>' +
      '<span class="legend-item"><span class="legend-dot" style="background:var(--state-busy)"></span>繁忙</span>' +
      '<span class="legend-item"><span class="legend-dot" style="background:var(--state-resting)"></span>休息</span>' +
      '<span class="nowrap">现在 ' +
      app.formatClock(Date.now()) +
      "</span></div>";
    html += "</div>";
    html += '<div class="card-body">';
    if (slots.length) {
      html += charts().scheduleAxis({
        slots: slots,
        currentKey: currentKey,
        nowHour: nowHour,
        clickable: true
      });
    } else {
      html += '<div class="stat-empty">日程数据尚未生成</div>';
    }
    html +=
      '<div class="hint mt-8">每个色块宽度代表时间块时长（约 7~11 块弹性拼接覆盖 0–24 点）；繁忙块走完后的完成结果会写入「今日繁忙成果」。</div>';
    html += "</div></section>";

    /* ③ 小卡网格 */
    html += '<section class="small-grid">';

    html +=
      '<div class="card stat-card"><div class="card-head"><div class="card-title">' +
      icon("spark", 14) +
      '今日基调</div><span class="card-sub">Day Vigor</span></div><div class="card-body">' +
      vigorBlock(vigor, chronotype) +
      "</div></div>";

    html +=
      '<div class="card stat-card"><div class="card-head"><div class="card-title">' +
      icon("zap", 14) +
      '微调配额</div><span class="card-sub">Mutation</span></div><div class="card-body">' +
      quotaBlock(quota) +
      "</div></div>";

    html +=
      '<div class="card stat-card"><div class="card-head"><div class="card-title">' +
      icon("list", 14) +
      '今日繁忙成果</div><span class="card-sub">Tier 1 短时记忆</span></div><div class="card-body">' +
      busyResultsBlock(busyResults) +
      "</div></div>";

    html +=
      '<div class="card stat-card"><div class="card-head"><div class="card-title">' +
      icon("relations", 14) +
      '关系分布</div><a class="btn btn-ghost btn-xs" href="#/relations">查看</a></div><div class="card-body">' +
      relationsBlock(counts) +
      "</div></div>";

    html +=
      '<div class="card stat-card"><div class="card-head"><div class="card-title">' +
      icon("memory", 14) +
      '记忆分级</div><a class="btn btn-ghost btn-xs" href="#/memory">查看</a></div><div class="card-body">' +
      memoriesBlock(counts) +
      "</div></div>";

    html +=
      '<div class="card stat-card"><div class="card-head"><div class="card-title">' +
      icon("models", 14) +
      '模型健康</div><a class="btn btn-ghost btn-xs" href="#/models">配置</a></div><div class="card-body">' +
      modelsBlock(models) +
      "</div></div>";

    html += "</section>";
    return html;
  }

  function currentSlot(data) {
    if (data.schedule && data.schedule.current_slot) return data.schedule.current_slot;
    var state = data.state || {};
    return {
      slot_key: state.slot_key || "",
      label: state.label || "",
      title: state.title || "",
      state: state.state || "IDLE",
      action_type: state.action_type || "",
      armor: state.armor,
      start_hour: state.start_hour,
      end_hour: state.end_hour,
      progress_pct: state.progress_pct,
      busy_settled: state.busy_settled,
      completion_result: state.completion_result
    };
  }

  function findSlot(data, slotKey) {
    var slots =
      (data.schedule && data.schedule.run && data.schedule.run.slots) || [];
    for (var i = 0; i < slots.length; i++) {
      if (slots[i].slot_key === slotKey) return slots[i];
    }
    if (data.schedule && data.schedule.current_slot && data.schedule.current_slot.slot_key === slotKey) {
      return data.schedule.current_slot;
    }
    return null;
  }

  /* ------------------------------------------------------------------ *
   * 交互：点击时间块 → 行动类型 + 标题 + 护甲（POST schedule/force_state）
   * ------------------------------------------------------------------ */
  function actionTypeDefs() {
    var defs = charts().ACTION_TYPES;
    return Array.isArray(defs) ? defs : [];
  }

  function actionFormHtml(app, slot) {
    var currentType = resolveActionType(null, slot) || "IDLE_ROAM";
    var currentState = actionStateOf(currentType) || (slot && slot.state) || "IDLE";
    var armor = slot && slot.armor != null ? Math.max(0, Math.min(100, num(slot.armor, 50))) : 50;
    var html = "";
    html += '<div class="field"><span class="field-label">行动类型</span><div class="segmented" data-role="action-seg">';
    actionTypeDefs().forEach(function (def) {
      html +=
        '<button type="button" class="seg-btn' +
        (def.value === currentType ? " active" : "") +
        '" data-action-type="' +
        esc(def.value) +
        '" data-state="' +
        esc(def.state) +
        '" data-tip="' +
        esc(def.label + " · 绑定 " + (STATE_LABELS[def.state] || def.state)) +
        '">' +
        esc(def.short || def.label) +
        "</button>";
    });
    html += "</div>";
    html +=
      '<div class="field-help">绑定状态：<span class="tag" data-role="action-state-hint">' +
      esc(STATE_LABELS[String(currentState).toUpperCase()] || "空闲") +
      "</span></div></div>";
    html +=
      '<div class="field"><span class="field-label">正在做的事（活动标题）</span>' +
      '<input class="input" data-role="activity-input" maxlength="120" placeholder="如：埋头赶稿 / 躺沙发刷 B 站" value="' +
      esc((slot && slot.title) || "") +
      '" /></div>';
    html +=
      '<div class="field"><span class="field-label">定力护甲</span>' +
      '<div class="slider-row"><input class="slider" type="range" min="0" max="100" step="1" data-role="armor-slider" value="' +
      armor +
      '" /><span class="slider-value" data-role="armor-value">' +
      armor +
      "</span></div>" +
      '<div class="field-help">护甲越高越不容易被普通消息打断；L3 核心用户的高 Impact 消息仍可能击穿护甲并触发微调。</div></div>';
    return html;
  }

  function bindActionForm(container) {
    if (!container) return;
    var seg = container.querySelector('[data-role="action-seg"]');
    var hint = container.querySelector('[data-role="action-state-hint"]');
    if (seg) {
      seg.addEventListener("click", function (event) {
        var btn = event.target.closest("[data-action-type]");
        if (!btn) return;
        seg.querySelectorAll(".seg-btn").forEach(function (el) {
          el.classList.remove("active");
        });
        btn.classList.add("active");
        if (hint) {
          var stateId = String(btn.getAttribute("data-state") || "").toUpperCase();
          hint.textContent = STATE_LABELS[stateId] || "空闲";
          hint.className = "tag " + (stateId ? "st-" + stateId.toLowerCase() : "");
        }
      });
    }
    var slider = container.querySelector('[data-role="armor-slider"]');
    var valueEl = container.querySelector('[data-role="armor-value"]');
    if (slider && valueEl) {
      slider.addEventListener("input", function () {
        valueEl.textContent = slider.value;
      });
    }
  }

  function readActionForm(container) {
    var active = container ? container.querySelector('[data-role="action-seg"] .seg-btn.active') : null;
    var slider = container ? container.querySelector('[data-role="armor-slider"]') : null;
    var titleEl = container ? container.querySelector('[data-role="activity-input"]') : null;
    var actionType = active ? active.getAttribute("data-action-type") : "IDLE_ROAM";
    return {
      action_type: actionType,
      new_state: actionStateOf(actionType) || "IDLE",
      armor: slider ? Number(slider.value) : 50,
      new_title: titleEl ? String(titleEl.value || "").trim() : ""
    };
  }

  function openSlotModal(app, slot, onSaved) {
    if (!slot) {
      app.toast("没有找到该时间块的数据", true);
      return;
    }
    var body = document.createElement("div");
    body.innerHTML =
      '<div class="card-sub mb-12">' +
      esc(fmtHour(slot.start_hour) + "-" + fmtHour(slot.end_hour) + " · " + (slot.slot_key || "")) +
      "</div>" +
      actionFormHtml(app, slot) +
      '<div class="hint mt-12">保存后立即生效（POST schedule/force_state），会覆盖该时间块今日抽到的活动。</div>';
    bindActionForm(body);
    app.modal({
      title: "切换行动 · " + (slot.label || slot.slot_key || ""),
      body: body,
      actions: [
        { label: "取消" },
        {
          label: "保存",
          primary: true,
          icon: "save",
          onClick: async function () {
            var form = readActionForm(body);
            var data = await app.apiPost("schedule/force_state", {
              soul_id: app.state.currentSoulId,
              slot_key: slot.slot_key,
              action_type: form.action_type,
              /* new_state 兼容旧后端；新后端优先 action_type */
              new_state: form.new_state,
              armor: form.armor,
              new_title: form.new_title
            });
            if (data && data.schedule) app.state.activeSchedule = data.schedule;
            app.toast("已更新「" + (slot.label || slot.slot_key) + "」为 " + actionLabel(form.action_type, true));
            if (typeof onSaved === "function") await onSaved();
          }
        }
      ]
    });
  }


  function bindEvents(container, app, data) {
    container.onclick = function (event) {
      var target = event.target;
      if (!target || !target.closest) return;

      var seg = target.closest(".sched-seg.is-clickable");
      if (seg) {
        var slotKey = seg.getAttribute("data-slot-key");
        var slot = findSlot(data, slotKey) || (data.schedule && data.schedule.current_slot);
        openSlotModal(app, slot, function () {
          return render(container, app);
        });
        return;
      }

      var actionEl = target.closest("[data-action]");
      if (!actionEl) return;
      var action = actionEl.getAttribute("data-action");
      if (action === "refresh-overview") {
        render(container, app);
      } else if (action === "quick-force") {
        var current = currentSlot(data);
        openSlotModal(app, current, function () {
          return render(container, app);
        });
      }
    };
  }

  function startTimers(container, app, data) {
    stopTimers();
    var state = data.state || {};
    var slot = currentSlot(data);
    var actionType = resolveActionType(state, slot);
    var isBusyTask = actionType === "BUSY_TASK";
    pageState.total = slotTotalSeconds(slot);
    pageState.remaining = num(state.remaining_seconds, 0);
    pageState.cooldown = num((data.quota || {}).cooldown_remaining_seconds, 0);
    if (pageState.remaining <= 0 && pageState.cooldown <= 0) return;
    pageState.timer = setInterval(function () {
      if (pageState.remaining > 0) {
        pageState.remaining -= 1;
        var textEl = container.querySelector('[data-role="remaining-text"]');
        if (textEl) textEl.textContent = app.formatDuration(pageState.remaining);
        var elapsed = pageState.total > 0 ? clamp01(1 - pageState.remaining / pageState.total) : 1;
        /* BUSY_TASK 刷新实时进度条，其它行动刷新剩余时间进度条 */
        var busyBar = container.querySelector('[data-role="busy-bar"]');
        if (busyBar) busyBar.style.width = (elapsed * 100).toFixed(2) + "%";
        var busyPctEl = container.querySelector('[data-role="busy-pct"]');
        if (busyPctEl) busyPctEl.textContent = String(Math.round(elapsed * 100));
        if (!isBusyTask) {
          var barEl = container.querySelector('[data-role="remaining-bar"]');
          if (barEl) barEl.style.width = (elapsed * 100).toFixed(2) + "%";
        }
      }
      if (pageState.cooldown > 0) {
        pageState.cooldown -= 1;
        var cooldownEl = container.querySelector('[data-role="cooldown-text"]');
        if (cooldownEl) cooldownEl.textContent = formatCooldown(pageState.cooldown);
      }
      if (pageState.remaining <= 0 && pageState.cooldown <= 0) stopTimers();
    }, 1000);
  }

  function stopTimers() {
    if (pageState.timer) {
      clearInterval(pageState.timer);
      pageState.timer = null;
    }
  }

  function errorHtml(err) {
    return (
      '<div class="card"><div class="empty">' +
      icon("alert", 32) +
      '<div class="empty-title">概览数据加载失败</div>' +
      '<div class="empty-text">' +
      esc((err && (err.message || err)) || "未知错误") +
      "</div>" +
      '<div class="row mt-12"><button class="btn btn-outline" type="button" data-action="retry-overview">重试</button></div>' +
      "</div></div>"
    );
  }

  async function render(container, app) {
    _app = app;
    stopTimers();
    try {
      container.innerHTML =
        '<div class="loading"><span class="spinner"></span><span>加载概览数据…</span></div>';
      var data = await app.fetchOverview();
      if (!data) throw new Error("dashboard/overview 返回为空");
      pageState.data = data;
      container.innerHTML = buildHtml(app, data);
      bindEvents(container, app, data);
      startTimers(container, app, data);
    } catch (err) {
      console.error("[OnlyYou] 概览页渲染失败：", err);
      container.innerHTML = errorHtml(err);
      container.onclick = function (event) {
        var target = event.target;
        if (target && target.closest && target.closest('[data-action="retry-overview"]')) {
          render(container, app);
        }
      };
    }
  }

  function destroy() {
    stopTimers();
    pageState.data = null;
    _app = null;
  }

  window.OY_PAGES = window.OY_PAGES || {};
  window.OY_PAGES.overview = {
    id: "overview",
    title: "概览",
    icon: "gauge",
    render: render,
    destroy: destroy
  };
})();
