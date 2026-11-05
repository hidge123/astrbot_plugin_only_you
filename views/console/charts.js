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
 */
(function () {
  "use strict";

  var STATE_LABEL = { IDLE: "空闲", BUSY: "繁忙", RESTING: "休息" };
  var STATE_CLASS = { IDLE: "st-idle", BUSY: "st-busy", RESTING: "st-resting" };

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
    var hour = Math.floor(num(h, 0)) % 24;
    return pad2(hour) + ":00";
  }

  /* ------------------------------------------------------------------ *
   * 1) 环形护甲表：0–100 圆弧 + 中心文字
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
   * 2) 24 小时日程轴：彩色时段块 + 现在指针
   * ------------------------------------------------------------------ */
  function slotRanges(slot) {
    var start = num(slot.start_hour, 0);
    var end = num(slot.end_hour, 24);
    if (end === 0) end = 24;
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
      var left = (seg.start / 24) * 100;
      var width = ((seg.end - seg.start) / 24) * 100;
      var state = String(seg.slot.state || "").toUpperCase();
      var isCurrent = !!currentKey && seg.slot.slot_key === currentKey;
      var tip =
        (seg.slot.label || seg.slot.slot_key || "") +
        " · " +
        stateLabel(state) +
        " · " +
        (seg.slot.title || "") +
        " · 护甲 " +
        num(seg.slot.armor, 0) +
        " · " +
        fmtHour(seg.slot.start_hour) +
        "–" +
        fmtHour(seg.slot.end_hour);
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
        esc(seg.slot.slot_key || "") +
        '" data-state="' +
        esc(state) +
        '">';
      if (width >= 8 && seg.end - seg.start >= 2) {
        html += '<span class="sched-seg-name">' + esc(seg.slot.label || seg.slot.slot_key) + "</span>";
        html += '<span class="sched-seg-meta">' + stateLabel(state) + (seg.slot.mutated ? " · 已微调" : "") + "</span>";
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
    esc: esc
  };
})();
