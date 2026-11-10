/**
 * OnlyYou Plugin Pages — 纯 SVG 图表库 (`views/console/charts.js`)
 *
 * 无构建、无依赖、无 Canvas。所有函数返回 HTML 字符串，页面直接 innerHTML 注入；
 * 颜色可用 CSS 变量（如 `var(--state-idle)`），自动跟随浅色 / 深色主题。
 *
 *   OYCharts.ringGauge({ value, max, label, sublabel, color, size })
 *   OYCharts.scheduleAxis({ slots, currentKey, nowHour, clickable })
 *   OYCharts.donut({ items:[{label,value,color}], size, centerValue, centerLabel })
 *   OYCharts.bars({ items:[{label,value,max,color}] })
 *   OYCharts.dots({ used, max })
 *
 * 时间轴为「24h 动态积木」：每个块带 start_hour / end_hour 浮点小时
 * （0.5h 网格，连续覆盖 0~24），宽度按 (end-start)/24 弹性渲染；
 * 块可带 action_type（SLEEP/SURF/IDLE_ROAM/BUSY_TASK）、progress_pct、
 * busy_settled、completion_result 等新字段（旧 6 时段过夜数据自动兜底拆段）。
 * v2.6：tooltip 不再展示定力护甲数值，改为展示当前时间块「进度 X%」（有 progress_pct 时）。
 */
(function () {
  "use strict";

  var STATE_LABEL = { IDLE: "空闲", BUSY: "繁忙", RESTING: "休息" };
  var STATE_CLASS = { IDLE: "st-idle", BUSY: "st-busy", RESTING: "st-resting" };

  /* 四类行动原型（与 core/state_machine.py ACTION_TYPE_DEFINITIONS 对齐；
   * label 为完整中文名（后端 action_types 同形），short 为徽章 / 筛选用的中文简称） */
  var ACTION_TYPES = [
    { value: "SLEEP", label: "睡觉 / 休眠", short: "睡觉", state: "RESTING", min_hours: 1.0, max_hours: 7.5 },
    { value: "SURF", label: "冲浪刷片", short: "冲浪", state: "IDLE", min_hours: 0.5, max_hours: 2.0 },
    { value: "IDLE_ROAM", label: "空闲闲逛", short: "闲逛", state: "IDLE", min_hours: 0.5, max_hours: 2.0 },
    { value: "BUSY_TASK", label: "繁忙主业", short: "繁忙主业", state: "BUSY", min_hours: 1.0, max_hours: 3.0 }
  ];
  var ACTION_MAP = {};
  ACTION_TYPES.forEach(function (item) {
    ACTION_MAP[item.value] = item;
  });

  /** 任意值 → 四类 action_type 之一（大小写宽容）；非法返回 "" */
  function normActionType(value) {
    var key = String(value == null ? "" : value).toUpperCase();
    return ACTION_MAP[key] ? key : "";
  }

  /** 旧数据兜底：无 action_type 时按 state / title 推导（与后端 from_dict 迁移规则一致） */
  function actionTypeOf(slot) {
    if (!slot) return "";
    var explicit = normActionType(slot.action_type);
    if (explicit) return explicit;
    var state = String(slot.state || "").toUpperCase();
    var title = String(slot.title || "");
    if (state === "RESTING") return "SLEEP";
    if (state === "BUSY") return "BUSY_TASK";
    if (/刷|B站|b站|抖音|视频|冲浪/.test(title)) return "SURF";
    if (state === "IDLE") return "IDLE_ROAM";
    return "";
  }

  /** 行动类型 → 中文名（short=true 返回简称，如「睡觉」「繁忙主业」） */
  function actionLabel(actionType, short) {
    var def = ACTION_MAP[normActionType(actionType)];
    if (!def) return "";
    return short ? def.short : def.label;
  }

  /** 行动类型 → 绑定的三态（用于旧数据/缺 state 时补全） */
  function actionState(actionType) {
    var def = ACTION_MAP[normActionType(actionType)];
    return def ? def.state : "";
  }

  /** 行动类型 → CSS 徽章类名：action-sleep / action-surf / action-idle-roam / action-busy-task */
  function actionClass(actionType) {
    var t = normActionType(actionType);
    return t ? "action-" + t.toLowerCase().replace(/_/g, "-") : "";
  }

  function num(v, dflt) {
    var n = Number(v);
    return isFinite(n) ? n : dflt;
  }
  function clamp01(n) {
    return Math.max(0, Math.min(1, n));
  }
  function esc(str) {
    return String(str == null ? "" : str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }
  function stateClass(state) {
    return STATE_CLASS[String(state || "").toUpperCase()] || "st-idle";
  }
  function stateLabel(state) {
    return STATE_LABEL[String(state || "").toUpperCase()] || "空闲";
  }
  function pad2(n) {
    return String(n).length < 2 ? "0" + n : String(n);
  }
  function fmtHour(h) {
    var n = num(h, 0);
    n = Math.max(0, Math.min(24, n));
    var totalMinutes = Math.round(n * 60);
    var hour = Math.floor(totalMinutes / 60);
    var minute = totalMinutes % 60;
    return pad2(hour) + ":" + pad2(minute);
  }

  /** 时长（小时浮点）→ "1.5h" / "3.0h"（时长胶囊用） */
  function fmtHours(h) {
    var n = num(h, 0);
    return n.toFixed(1) + "h";
  }

  /* ------------------------------------------------------------------ *
   * 1) 环形仪表图：0–max 圆弧 + 中心文字（v2.6 概览页用于「当前时间块进度」）
   * ------------------------------------------------------------------ */
  function ringGauge(opts) {
    opts = opts || {};
    var value = num(opts.value, 0);
    var max = num(opts.max, 100) || 100;
    var ratio = clamp01(value / max);
    var size = num(opts.size, 132);
    var stroke = num(opts.stroke, size >= 120 ? 10 : 8);
    var c = size / 2;
    var r = Math.max(4, (size - stroke) / 2 - 2);
    var circ = 2 * Math.PI * r;
    var offset = circ * (1 - ratio);
    var color = opts.color || "var(--primary)";
    var track = opts.trackColor || "var(--ring-track, var(--border))";
    var label = opts.label != null ? opts.label : String(Math.round(value));
    var sublabel = opts.sublabel || "";
    var html =
      '<div class="ring-gauge" style="width:' +
      size +
      "px;height:" +
      size +
      'px">';
    html +=
      '<svg class="ring-gauge-svg" width="' +
      size +
      '" height="' +
      size +
      '" viewBox="0 0 ' +
      size +
      " " +
      size +
      '" role="img" aria-label="' +
      esc(label) +
      '">';
    html +=
      '<circle cx="' + c + '" cy="' + c + '" r="' + r + '" fill="none" stroke="' + track + '" stroke-width="' + stroke + '"/>';
    html +=
      '<circle cx="' +
      c +
      '" cy="' +
      c +
      '" r="' +
      r +
      '" fill="none" stroke="' +
      color +
      '" stroke-width="' +
      stroke +
      '" stroke-linecap="round" stroke-dasharray="' +
      circ.toFixed(2) +
      '" stroke-dashoffset="' +
      offset.toFixed(2) +
      '" transform="rotate(-90 ' +
      c +
      " " +
      c +
      ')"/>';
    html += "</svg>";
    html +=
      '<div class="ring-gauge-center"><div class="ring-gauge-value" style="color:' +
      color +
      '">' +
      esc(label) +
      "</div>" +
      (sublabel ? '<div class="ring-gauge-sub">' + esc(sublabel) + "</div>" : "") +
      "</div>";
    return html + "</div>";
  }

  /* ------------------------------------------------------------------ *
   * 2) 24h 动态日程轴：弹性宽度块 + 行动/状态色 + 现在指针
   * ------------------------------------------------------------------ */
  function slotRanges(slot) {
    var start = num(slot.start_hour, 0);
    var end = num(slot.end_hour, 24);
    if (end === 0) end = 24;
    /* 新时间轴恒为 end > start（0~24 连续无缝）；旧 6 时段过夜块拆成两段兜底 */
    if (end > start) return [[start, end]];
    return [
      [start, 24],
      [0, end]
    ];
  }

  function scheduleAxis(opts) {
    opts = opts || {};
    var slots = opts.slots || [];
    var currentKey = opts.currentKey || "";
    var nowHour = opts.nowHour == null ? null : num(opts.nowHour, null);
    var clickable = opts.clickable !== false;
    var segs = [];
    slots.forEach(function (slot) {
      slotRanges(slot).forEach(function (range) {
        segs.push({ slot: slot, start: range[0], end: range[1] });
      });
    });
    segs.sort(function (a, b) {
      return a.start - b.start;
    });

    var html = '<div class="sched-axis">';

    /* 小时刻度 0 / 6 / 12 / 18 / 24 */
    html += '<div class="sched-axis-hours">';
    [0, 6, 12, 18, 24].forEach(function (h) {
      var left = (h / 24) * 100;
      var style = h === 0 ? "left:0;transform:none" : h === 24 ? "left:100%;transform:translateX(-100%)" : "left:" + left + "%;transform:translateX(-50%)";
      html += '<span class="sched-hour" style="' + style + '">' + (h === 24 ? "24" : h) + "点</span>";
    });
    html += "</div>";

    html += '<div class="sched-axis-track">';
    segs.forEach(function (seg) {
      var slot = seg.slot;
      /* 弹性宽度：每块占 (end-start)/24 的百分比（0.5h 网格 → 最小 ~2.08%） */
      var left = (seg.start / 24) * 100;
      var width = ((seg.end - seg.start) / 24) * 100;
      var spanHours = seg.end - seg.start;
      var actionType = actionTypeOf(slot);
      var state = String(slot.state || actionState(actionType) || "").toUpperCase();
      var isCurrent = !!currentKey && slot.slot_key === currentKey;
      /* v2.6：隐藏护甲数值；时间块进度（后端 _enrich_slot 对每块都会下发 progress_pct） */
      var progressPct = null;
      if (slot.progress_pct != null && slot.progress_pct !== "") {
        progressPct = Math.max(0, Math.min(100, Math.round(num(slot.progress_pct, 0))));
      }
      var busyProgressPct = actionType === "BUSY_TASK" ? progressPct : null;

      var tipParts = [
        slot.label || actionLabel(actionType, false) || slot.slot_key || "",
        stateLabel(state),
        slot.title || "",
        fmtHour(seg.start) + "-" + fmtHour(seg.end)
      ];
      if (progressPct != null) tipParts.push("进度 " + progressPct + "%");
      if (slot.busy_settled && slot.completion_result) {
        tipParts.push("完成：" + slot.completion_result);
      }
      var tip = tipParts.join(" · ");

      var cls =
        "sched-seg " +
        stateClass(state) +
        (isCurrent ? " is-current" : "") +
        (clickable ? " is-clickable" : "");
      html +=
        '<div class="' +
        cls +
        '" style="left:calc(' +
        left.toFixed(4) +
        "% + 1px);width:calc(" +
        width.toFixed(4) +
        "% - 2px)" +
        '" data-tip="' +
        esc(tip) +
        '" data-slot-key="' +
        esc(slot.slot_key || "") +
        '" data-state="' +
        esc(state) +
        '" data-action-type="' +
        esc(actionType) +
        '">';
      /* 宽度足够时显示标题 + 行动 label；过窄只留小色块（避免文字溢出） */
      if (width >= 8 && spanHours >= 1) {
        var metaText = actionLabel(actionType, true) || stateLabel(state);
        if (busyProgressPct != null) metaText += " · " + busyProgressPct + "%";
        if (slot.mutated) metaText += " · 已微调";
        html += '<span class="sched-seg-name">' + esc(slot.title || slot.label || slot.slot_key) + "</span>";
        html += '<span class="sched-seg-meta">' + esc(metaText) + "</span>";
      } else if (width >= 4) {
        html += '<span class="sched-seg-mini"></span>';
      }
      html += "</div>";
    });

    if (nowHour != null) {
      var nowPct = (Math.max(0, Math.min(24, nowHour)) / 24) * 100;
      html += '<div class="sched-axis-now" style="left:' + nowPct.toFixed(3) + '%"><span class="sched-axis-now-dot"></span></div>';
    }
    html += "</div>";
    return html + "</div>";
  }

  /* ------------------------------------------------------------------ *
   * 3) 环形图（关系 L3/L2/L1 等）
   * ------------------------------------------------------------------ */
  function donut(opts) {
    opts = opts || {};
    var items = (opts.items || []).filter(function (it) {
      return num(it.value, 0) > 0;
    });
    var size = num(opts.size, 116);
    var thickness = num(opts.thickness, 13);
    var c = size / 2;
    var r = Math.max(4, (size - thickness) / 2 - 1);
    var circ = 2 * Math.PI * r;
    var total = items.reduce(function (acc, it) {
      return acc + num(it.value, 0);
    }, 0);
    var html = '<div class="donut" style="width:' + size + "px;height:" + size + 'px">';
    html += '<svg class="donut-svg" width="' + size + '" height="' + size + '" viewBox="0 0 ' + size + " " + size + '">';
    html += '<circle cx="' + c + '" cy="' + c + '" r="' + r + '" fill="none" stroke="var(--ring-track, var(--border))" stroke-width="' + thickness + '"/>';
    if (total > 0) {
      var offset = 0;
      items.forEach(function (it) {
        var frac = num(it.value, 0) / total;
        var len = frac * circ;
        html +=
          '<circle class="donut-seg" cx="' +
          c +
          '" cy="' +
          c +
          '" r="' +
          r +
          '" fill="none" stroke="' +
          (it.color || "var(--primary)") +
          '" stroke-width="' +
          thickness +
          '" stroke-dasharray="' +
          len.toFixed(2) +
          " " +
          (circ - len).toFixed(2) +
          '" stroke-dashoffset="' +
          (-offset).toFixed(2) +
          '" transform="rotate(-90 ' +
          c +
          " " +
          c +
          ')"/>';
        offset += len;
      });
    }
    html += "</svg>";
    var centerValue = opts.centerValue != null ? opts.centerValue : total;
    html += '<div class="donut-center"><strong>' + esc(centerValue) + "</strong>";
    if (opts.centerLabel) html += '<span>' + esc(opts.centerLabel) + "</span>";
    html += "</div>";
    return html + "</div>";
  }

  /* ------------------------------------------------------------------ *
   * 4) 水平条形图（记忆分级 / 完成度等）
   * ------------------------------------------------------------------ */
  function bars(opts) {
    opts = opts || {};
    var items = opts.items || [];
    var html = '<div class="bars">';
    items.forEach(function (it) {
      var max = num(it.max, 100) || 100;
      var ratio = clamp01(num(it.value, 0) / max);
      var color = it.color || "var(--primary)";
      var display = it.display != null ? it.display : it.value;
      html += '<div class="bar-row">';
      html += '<span class="bar-label">' + esc(it.label != null ? it.label : "") + "</span>";
      html += '<span class="bar-track"><span class="bar-fill" style="width:' + (ratio * 100).toFixed(2) + "%;background:" + color + '"></span></span>';
      html += '<span class="bar-value">' + esc(display) + "</span>";
      html += "</div>";
    });
    return html + "</div>";
  }

  /* ------------------------------------------------------------------ *
   * 5) 微调配额圆点 ●●○
   * ------------------------------------------------------------------ */
  function dots(opts) {
    opts = opts || {};
    var used = Math.max(0, Math.round(num(opts.used, 0)));
    var max = Math.max(1, Math.round(num(opts.max, 2)));
    var html = '<span class="quota-dots" role="img" aria-label="' + used + " / " + max + '">';
    for (var i = 0; i < max; i++) {
      html += '<span class="q-dot' + (i < used ? " on" : "") + '"></span>';
    }
    html += "</span>";
    return html;
  }

  window.OYCharts = {
    ringGauge: ringGauge,
    scheduleAxis: scheduleAxis,
    donut: donut,
    bars: bars,
    dots: dots,
    stateClass: stateClass,
    stateLabel: stateLabel,
    fmtHour: fmtHour,
    fmtHours: fmtHours,
    /* 四类行动工具的共享出口（page-overview / page-schedule 共用） */
    ACTION_TYPES: ACTION_TYPES,
    actionLabel: actionLabel,
    actionClass: actionClass,
    actionState: actionState,
    actionTypeOf: actionTypeOf,
    normActionType: normActionType,
    esc: esc
  };
})();
