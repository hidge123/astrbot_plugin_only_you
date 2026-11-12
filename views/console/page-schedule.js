/**
 * OnlyYou Plugin Pages — 日程页 (`views/console/page-schedule.js`)
 *
 * 数据源：
 *  - 今日日程：ctx.state.activeSchedule（来自 init_summary / souls/detail；缺失时自动 GET souls/detail）
 *  - 卡片池：GET `cards/list?soul_id=...`
 *    （新形状：custom_cards / general_cards / action_types / chronotype，无 encounters）
 *
 * 交互（24h 动态积木时间轴 × 四类行动卡）：
 *  - 顶部时间轴：OYCharts.scheduleAxis 弹性色块；点击块打开右侧抽屉编辑
 *    行动类型 / 标题 → POST schedule/force_state（定力护甲由行动类型自动固定，不向用户暴露）
 *  - 行动卡网格：左边框按绑定状态着色（IDLE 青绿 / BUSY 琥珀 / RESTING 靛蓝），
 *    显示四类行动徽章 + 卡名 + 时长胶囊 + 权重；
 *    顶部按四类行动类型筛选（全部 / 睡觉 / 冲浪 / 闲逛 / 繁忙）
 *  - 新建 / 编辑行动卡：title / action_type / min_hours / max_hours / base_weight / scope
 *    → POST cards/save；删除 → POST cards/delete
 *  - 重抽日程卡池 → POST schedule/reroll：删除当前角色全部自定义行动卡并重新生成
 *    一套新卡；今日已排好的 24h 时间轴保持原样，明日 00:00 起从新卡池抽取
 *  - v2.6：隐藏手动护甲设置（行动卡 / 时间块抽屉均不再出现护甲滑条，POST 不再携带 armor）
 *  - 已移除奇遇谈资卡全部 UI 与逻辑
 *
 * 注册：window.OY_PAGES.schedule = { id,title,icon,render(container,ctx),destroy() }
 */
(function () {
  "use strict";

  var pageData = {
    schedule: null,
    cards: null,
    filter: "ALL"
  };

  var STATE_LABELS = { IDLE: "空闲", BUSY: "繁忙", RESTING: "休息" };
  var ACTION_COLOR_VAR = {
    SLEEP: "var(--action-sleep)",
    SURF: "var(--action-surf)",
    IDLE_ROAM: "var(--action-idle-roam)",
    BUSY_TASK: "var(--action-busy-task)"
  };
  var FILTER_LABELS = { SLEEP: "睡觉", SURF: "冲浪", IDLE_ROAM: "闲逛", BUSY_TASK: "繁忙" };
  var CHRONOTYPE_LABELS = { early: "早起型", normal: "正常作息", night_owl: "夜猫子" };

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

  function clamp(value, min, max) {
    var n = Number(value);
    if (!isFinite(n)) n = min;
    return Math.max(min, Math.min(max, n));
  }

  function roundHalf(value) {
    return Math.round(num(value, 0) * 2) / 2;
  }

  function fmtHour(value) {
    return charts().fmtHour ? charts().fmtHour(value) : String(value || 0) + ":00";
  }

  function fmtHours(value) {
    return charts().fmtHours ? charts().fmtHours(value) : num(value, 0).toFixed(1) + "h";
  }

  function chronotypeLabel(value) {
    return CHRONOTYPE_LABELS[String(value || "normal").toLowerCase()] || CHRONOTYPE_LABELS.normal;
  }

  /* ------------------------------------------------------------------ *
   * 四类行动工具（优先用后端下发的 action_types，缺省回退 charts 内置定义）
   * ------------------------------------------------------------------ */
  function actionTypeDefs(cards) {
    var defs = cards && Array.isArray(cards.action_types) ? cards.action_types : null;
    if (!defs || !defs.length) defs = charts().ACTION_TYPES;
    var list = [];
    (defs || []).forEach(function (def) {
      if (!def || typeof def !== "object") return;
      var value = charts().normActionType ? charts().normActionType(def.value) : String(def.value || "").toUpperCase();
      if (!value) return;
      list.push({
        value: value,
        label: def.label || value,
        state: String(def.state || (charts().actionState ? charts().actionState(value) : "") || "IDLE").toUpperCase(),
        min_hours: num(def.min_hours, num(def.min, 0.5)),
        max_hours: num(def.max_hours, num(def.max, 8))
      });
    });
    if (list.length) return list;
    return (charts().ACTION_TYPES || []).map(function (def) {
      return {
        value: def.value,
        label: def.label,
        state: def.state,
        min_hours: num(def.min_hours, 0.5),
        max_hours: num(def.max_hours, 8)
      };
    });
  }

  function shortLabelOf(type) {
    var norm = charts().normActionType ? charts().normActionType(type) : String(type || "").toUpperCase();
    return FILTER_LABELS[norm] || (charts().actionLabel ? charts().actionLabel(norm, true) : "") || norm || "行动";
  }

  function badgeLabelOf(type) {
    return (charts().actionLabel ? charts().actionLabel(type, true) : "") || shortLabelOf(type);
  }

  function actionClassOf(type) {
    return charts().actionClass ? charts().actionClass(type) : "";
  }

  function actionStateOf(type) {
    return charts().actionState ? charts().actionState(type) : "";
  }

  function cardActionType(card) {
    if (!card) return "";
    if (charts().actionTypeOf) return charts().actionTypeOf(card);
    return String(card.action_type || "").toUpperCase();
  }

  function cardState(card) {
    var type = cardActionType(card);
    var state = String((card && card.state) || "").toUpperCase();
    if (state === "IDLE" || state === "BUSY" || state === "RESTING") return state;
    return actionStateOf(type) || "IDLE";
  }

  function actionBadgeHtml(type, text) {
    var cls = actionClassOf(type);
    return (
      '<span class="action-badge' +
      (cls ? " " + cls : "") +
      '" data-action-type="' +
      esc(type || "") +
      '">' +
      esc(text || badgeLabelOf(type)) +
      "</span>"
    );
  }

  /* ------------------------------------------------------------------ *
   * 数据加载
   * ------------------------------------------------------------------ */
  async function loadData(app) {
    var schedule = app.state.activeSchedule;
    if (!schedule || !schedule.run) {
      try {
        var detail = await app.apiGet("souls/detail", { soul_id: app.state.currentSoulId });
        if (detail && detail.schedule) {
          schedule = detail.schedule;
          app.state.activeSchedule = detail.schedule;
        }
      } catch (err) {
        console.warn("[OnlyYou] 日程页加载 souls/detail 失败：", err);
      }
    }
    var cards = await app.apiGet("cards/list", { soul_id: app.state.currentSoulId });
    return { schedule: schedule || null, cards: cards || {} };
  }

  /* ------------------------------------------------------------------ *
   * 片段构建：时间轴 / 行动卡网格
   * ------------------------------------------------------------------ */
  function actionLegendHtml(cards) {
    var rows = actionTypeDefs(cards)
      .map(function (def) {
        var color = ACTION_COLOR_VAR[def.value] || "var(--text-3)";
        return (
          '<span class="legend-item"><span class="legend-dot" style="background:' +
          color +
          '"></span>' +
          esc(shortLabelOf(def.value)) +
          "</span>"
        );
      })
      .join("");
    return rows;
  }

  function filterSegHtml(cards) {
    var defs = actionTypeDefs(cards);
    var items = [{ id: "ALL", label: "全部" }].concat(
      defs.map(function (def) {
        return { id: def.value, label: shortLabelOf(def.value) };
      })
    );
    return items
      .map(function (item) {
        return (
          '<button class="seg-btn' +
          (pageData.filter === item.id ? " active" : "") +
          '" type="button" data-action="filter-cards" data-filter="' +
          esc(item.id) +
          '"' +
          (item.id === "ALL" ? "" : ' data-action-type="' + esc(item.id) + '"') +
          ">" +
          esc(item.label) +
          "</button>"
        );
      })
      .join("");
  }

  function cardHtml(app, card) {
    var type = cardActionType(card);
    var stateId = cardState(card);
    var meta = app.stateMeta(stateId);
    var weight = Math.max(1, Math.min(100, Math.round(num(card.base_weight, 50))));
    var minHours = roundHalf(card.min_hours != null ? card.min_hours : 1);
    var maxHours = roundHalf(card.max_hours != null ? card.max_hours : 2);
    if (minHours > maxHours) {
      var swap = minHours;
      minHours = maxHours;
      maxHours = swap;
    }
    minHours = clamp(minHours, 0.5, 8);
    maxHours = clamp(maxHours, 0.5, 8);
    return (
      '<div class="action-card ' +
      meta.cls +
      '" data-card-id="' +
      esc(card.card_id || "") +
      '" data-scope="' +
      esc(card.scope || "soul") +
      '" data-action-type="' +
      esc(type) +
      '" data-tip="' +
      esc((badgeLabelOf(type) || "行动") + " · 点击编辑该行动卡") +
      '">' +
      '<div class="action-card-top"><div class="action-title">' +
      esc(card.title || "未命名行动卡") +
      "</div>" +
      '<div class="card-hover-actions">' +
      '<button class="icon-btn" type="button" data-action="delete-card" data-card-id="' +
      esc(card.card_id || "") +
      '" data-scope="' +
      esc(card.scope || "soul") +
      '" data-tip="删除卡片" aria-label="删除卡片">' +
      icon("trash", 14) +
      "</button></div></div>" +
      '<div class="action-card-meta">' +
      actionBadgeHtml(type, badgeLabelOf(type)) +
      '<span class="tag tag-scope">' +
      (card.scope === "global" ? "内置" : "自定义") +
      '</span><span class="tag tag-muted" data-tip="抽卡权重">权重 ' +
      weight +
      "</span></div>" +
      '<div class="action-duration">' +
      '<span class="duration-pill" data-tip="该卡在日程中随机抽取的时长区间">' +
      esc(fmtHours(minHours) + " ~ " + fmtHours(maxHours)) +
      '</span><span class="tag tag-muted">时长</span></div>' +
      "</div>"
    );
  }

  function cardsBlock(app, data) {
    var cards = data.cards || {};
    var customCards = Array.isArray(cards.custom_cards) ? cards.custom_cards : [];
    var generalCards = Array.isArray(cards.general_cards) ? cards.general_cards : [];
    var allCards = customCards.concat(generalCards);
    var filter = pageData.filter;
    var filtered =
      filter === "ALL"
        ? allCards
        : allCards.filter(function (card) {
            return cardActionType(card) === filter;
          });
    var filterLabel = filter === "ALL" ? "" : "「" + shortLabelOf(filter) + "」";
    var emptyTitle = filter === "ALL" ? "暂无行动卡" : filterLabel + "下暂无行动卡";
    var body = filtered.length
      ? '<div class="action-grid">' +
        filtered
          .map(function (card) {
            return cardHtml(app, card);
          })
          .join("") +
        "</div>"
      : '<div class="empty">' +
        icon("grid", 30) +
        '<div class="empty-title">' +
        esc(emptyTitle) +
        "</div>" +
        '<div class="empty-text">切换筛选条件，或点击右上角「新建卡片」手动创建一张；也可以点「重抽日程卡池」让 AI 重新生成一整套四类专属行动卡。</div></div>';
    return (
      '<section class="card"><div class="card-head">' +
      '<div class="card-title">' +
      icon("list", 15) +
      "四类行动卡池" +
      '<span class="badge-count">' +
      allCards.length +
      "</span>" +
      '<span class="card-sub">自定义 ' +
      customCards.length +
      " · 内置 " +
      generalCards.length +
      " · 按绑定状态着色</span></div>" +
      '<div class="card-actions">' +
      '<div class="segmented">' +
      filterSegHtml(cards) +
      "</div>" +
      '<button class="btn btn-outline btn-sm" type="button" data-action="new-card">' +
      icon("plus", 13) +
      "新建卡片</button>" +
      "</div></div>" +
      body +
      "</section>"
    );
  }

  function timelineBlock(app, data) {
    var schedule = data.schedule || {};
    var run = schedule.run || {};
    var slots = Array.isArray(run.slots) ? run.slots : [];
    var currentKey =
      (schedule.current_slot && schedule.current_slot.slot_key) ||
      (app.currentSlotKey && app.currentSlotKey()) ||
      "";
    var chronotype = schedule.chronotype || (data.cards && data.cards.chronotype) || "";
    var html = "";
    html += '<section class="card"><div class="card-head">';
    html +=
      '<div class="card-title">' +
      icon("clock", 15) +
      "24 小时动态时间轴" +
      '<span class="card-sub">约 7~11 个弹性块拼接覆盖全天 · 点击时间块编辑行动类型</span></div>';
    html +=
      '<div class="card-actions"><span class="chip" data-tip="今日基调">' +
      icon("spark", 12) +
      esc(run.vigor_title || "今日基调") +
      '</span><span class="chip" data-tip="角色作息类型">' +
      icon("moon", 12) +
      esc(chronotypeLabel(chronotype)) +
      '</span><span class="chip" data-tip="今日已用微调次数">' +
      icon("zap", 12) +
      "微调 " +
      num(run.mutations_used, 0) +
      " / " +
      num(schedule.max_mutations_per_day, 2) +
      "</span></div>";
    html += "</div>";
    html += '<div class="card-body">';
    if (slots.length) {
      html += charts().scheduleAxis({
        slots: slots,
        currentKey: currentKey,
        nowHour: app.nowHour(),
        clickable: true
      });
      html +=
        '<div class="axis-legend mt-12">' +
        actionLegendHtml(data.cards) +
        '<span class="nowrap">现在 ' +
        app.formatClock(Date.now()) +
        " · 共 " +
        slots.length +
        " 个时间块</span></div>";
    } else {
      html += '<div class="stat-empty">今日日程尚未生成，点击右上角「刷新」或发送 /onlyyou status 生成今日时间轴。</div>';
    }
    html += "</div></section>";
    return html;
  }

  function buildHtml(app, data) {
    var html = "";
    html +=
      '<div class="page-head"><h1 class="page-title">' +
      icon("calendar", 18) +
      "日程" +
      "</h1>" +
      '<div class="page-actions">' +
      '<button class="btn btn-ghost btn-sm" type="button" data-action="refresh-page">' +
      icon("refresh", 14) +
      "刷新</button>" +
      '<button class="btn btn-primary btn-sm" type="button" data-action="reroll">' +
      icon("wand", 14) +
      "重抽日程卡池</button>" +
      "</div></div>";
    html += timelineBlock(app, data);
    html += cardsBlock(app, data);
    return html;
  }

  function paint(container, app, data) {
    pageData.schedule = data.schedule || pageData.schedule;
    pageData.cards = data.cards || pageData.cards;
    container.innerHTML = buildHtml(app, data);
    bindEvents(container, app);
  }

  /* ------------------------------------------------------------------ *
   * 交互：时间块抽屉（行动类型四选一 + 活动标题）
   * ------------------------------------------------------------------ */
  function findSlot(schedule, slotKey) {
    var slots = (schedule && schedule.run && schedule.run.slots) || [];
    for (var i = 0; i < slots.length; i++) {
      if (slots[i].slot_key === slotKey) return slots[i];
    }
    if (schedule && schedule.current_slot && schedule.current_slot.slot_key === slotKey) {
      return schedule.current_slot;
    }
    return null;
  }

  function slotActionType(slot) {
    if (!slot) return "IDLE_ROAM";
    var type = charts().actionTypeOf ? charts().actionTypeOf(slot) : "";
    if (type) return type;
    var stateId = String(slot.state || "").toUpperCase();
    if (stateId === "RESTING") return "SLEEP";
    if (stateId === "BUSY") return "BUSY_TASK";
    return "IDLE_ROAM";
  }

  function slotFormHtml(app, slot, cards) {
    var defs = actionTypeDefs(cards);
    var currentType = slotActionType(slot);
    var currentState = actionStateOf(currentType) || (slot && slot.state) || "IDLE";
    var html = "";
    html +=
      '<div class="card-sub mb-12">' +
      esc(fmtHour(slot.start_hour) + "-" + fmtHour(slot.end_hour) + " · " + (slot.slot_key || "")) +
      "</div>";
    html += '<div class="field"><span class="field-label">行动类型</span><div class="segmented" data-role="slot-action-seg">';
    defs.forEach(function (def) {
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
        esc(shortLabelOf(def.value)) +
        "</button>";
    });
    html += "</div>";
    html +=
      '<div class="field-help">绑定状态：<span class="tag" data-role="slot-state-hint">' +
      esc(STATE_LABELS[String(currentState).toUpperCase()] || "空闲") +
      "</span> · 该行动类型自带固定护甲档位（由后端自动应用）</div></div>";
    html +=
      '<div class="field"><span class="field-label">正在做的事（活动标题）</span>' +
      '<input class="input" data-role="slot-title" maxlength="120" placeholder="如：埋头赶稿 / 躺沙发刷 B 站" value="' +
      esc((slot && slot.title) || "") +
      '" />' +
      '<div class="field-help">保存后立即生效（POST schedule/force_state），会覆盖该时间块今日抽到的活动。</div></div>';
    return html;
  }

  function bindSlotForm(body) {
    var seg = body.querySelector('[data-role="slot-action-seg"]');
    var hint = body.querySelector('[data-role="slot-state-hint"]');
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
  }

  function readSlotForm(body) {
    var active = body.querySelector('[data-role="slot-action-seg"] .seg-btn.active');
    var titleEl = body.querySelector('[data-role="slot-title"]');
    var actionType = active ? active.getAttribute("data-action-type") : "IDLE_ROAM";
    return {
      action_type: actionType,
      new_title: titleEl ? String(titleEl.value || "").trim() : ""
    };
  }

  function openSlotDrawer(app, container, slot) {
    if (!slot) {
      app.toast("未找到该时间块数据", true);
      return;
    }
    var cards = pageData.cards || {};
    var body = document.createElement("div");
    body.innerHTML = slotFormHtml(app, slot, cards);
    bindSlotForm(body);
    app.drawer({
      title: "编辑时间块 · " + (slot.label || slot.slot_key || ""),
      body: body,
      actions: [
        { label: "取消" },
        {
          label: "保存",
          primary: true,
          icon: "save",
          onClick: async function () {
            var form = readSlotForm(body);
            /* v2.6：护甲由行动类型固定档位自动应用，POST 不再携带 armor */
            var data = await app.apiPost("schedule/force_state", {
              soul_id: app.state.currentSoulId,
              slot_key: slot.slot_key,
              action_type: form.action_type,
              new_title: form.new_title
            });
            if (data && data.schedule) {
              app.state.activeSchedule = data.schedule;
              pageData.schedule = data.schedule;
            }
            paint(container, app, {
              schedule: pageData.schedule,
              cards: pageData.cards
            });
            app.toast("已更新「" + (slot.label || slot.slot_key) + "」为 " + shortLabelOf(form.action_type));
          }
        }
      ]
    });
  }

  /* ------------------------------------------------------------------ *
   * 交互：重抽日程卡池 / 删除
   * ------------------------------------------------------------------ */
  /**
   * 重抽日程卡池：删除当前角色全部自定义行动卡，由 AI（离线时自动兜底）重新
   * 生成一套新卡。今日已排好的 24h 时间轴保持不变，明天起从新卡池抽取。
   */
  async function doReroll(app, container) {
    await app.confirm({
      title: "确认重抽行动卡池？",
      message:
        "将删除当前角色的所有行动卡并由 AI 重新生成一套新卡片。" +
        "今日已排好的 24h 时间轴保持不变，明天起将从新卡池中抽取日程。",
      confirmLabel: "重抽",
      onConfirm: async function () {
        var btn = container.querySelector('[data-action="reroll"]');
        if (btn) btn.classList.add("is-loading");
        try {
          var res = await app.apiPost("schedule/reroll", {
            soul_id: app.state.currentSoulId
          });
          /* 今日时间轴保持不变：直接用后端返回的 schedule 覆盖（内容与重抽前一致） */
          if (res && res.schedule) {
            app.state.activeSchedule = res.schedule;
            pageData.schedule = res.schedule;
          }
          /* 下方行动卡网格用新卡池刷新；action_types / chronotype 等元数据沿用旧值 */
          var generated = res && Array.isArray(res.cards) ? res.cards : [];
          if (generated.length) {
            var base = pageData.cards || {};
            pageData.cards = Object.assign({}, base, {
              custom_cards: generated.map(function (card) {
                var copy = Object.assign({}, card);
                copy.scope = "soul";
                return copy;
              })
            });
          }
          paint(container, app, {
            schedule: pageData.schedule,
            cards: pageData.cards
          });
          app.toast(
            "已重抽 " +
              (generated.length || 0) +
              " 张行动卡（今日时间轴保持不变，明日生效）"
          );
        } catch (err) {
          app.toast("重抽行动卡池失败：" + (err.message || err), true);
        } finally {
          if (btn) btn.classList.remove("is-loading");
        }
      }
    });
  }

  function findCardById(cardId) {
    var all = []
      .concat((pageData.cards && pageData.cards.custom_cards) || [])
      .concat((pageData.cards && pageData.cards.general_cards) || []);
    for (var i = 0; i < all.length; i++) {
      if (all[i].card_id === cardId) return all[i];
    }
    return null;
  }

  async function doDeleteCard(app, container, cardId, scope) {
    var card = findCardById(cardId);
    await app.confirm({
      title: "删除行动卡",
      message: "确定删除「" + ((card && card.title) || cardId) + "」吗？此操作不可撤销。",
      confirmLabel: "删除",
      danger: true,
      onConfirm: async function () {
        var data = await app.apiPost("cards/delete", {
          soul_id: app.state.currentSoulId,
          card_type: "action",
          scope: scope || "soul",
          card_id: cardId
        });
        if (data && (Array.isArray(data.custom_cards) || Array.isArray(data.general_cards))) {
          pageData.cards = data;
        }
        paint(container, app, {
          schedule: pageData.schedule,
          cards: pageData.cards
        });
        app.toast("已删除行动卡");
      }
    });
  }

  /* ------------------------------------------------------------------ *
   * 交互：行动卡编辑器（title / action_type / min~max / weight / scope）
   * ------------------------------------------------------------------ */
  function openCardDrawer(app, container, card) {
    var isNew = !card;
    var defs = actionTypeDefs(pageData.cards);
    var defaultType = "IDLE_ROAM";
    var source = card || {
      card_id: "",
      title: "",
      action_type: defaultType,
      min_hours: 1.0,
      max_hours: 2.5,
      base_weight: 50,
      scope: "soul",
      synergy_tags: {}
    };
    var currentType = cardActionType(source) || defaultType;
    var currentState = actionStateOf(currentType) || cardState(source);
    var minHours = roundHalf(source.min_hours != null ? source.min_hours : 1.0);
    var maxHours = roundHalf(source.max_hours != null ? source.max_hours : 2.5);
    if (minHours > maxHours) {
      var swap = minHours;
      minHours = maxHours;
      maxHours = swap;
    }
    var weight = Math.max(1, Math.min(100, Math.round(num(source.base_weight, 50))));

    var stateOptions = (defs.length ? defs : charts().ACTION_TYPES || [])
      .map(function (def) {
        return (
          '<option value="' +
          esc(def.value) +
          '"' +
          (def.value === currentType ? " selected" : "") +
          ">" +
          esc(def.label + "（" + (STATE_LABELS[def.state] || def.state) + "）") +
          "</option>"
        );
      })
      .join("");

    var body = document.createElement("div");
    body.innerHTML =
      '<div class="field"><span class="field-label">卡片标题（正在做的事）</span>' +
      '<input class="input" data-role="card-title" maxlength="120" placeholder="如：窝在沙发里刷短视频 / 赶稿画商稿" value="' +
      esc(source.title || "") +
      '" /></div>' +
      '<div class="field"><span class="field-label">行动类型</span>' +
      '<select class="select" data-role="card-action-type">' +
      stateOptions +
      "</select>" +
      '<div class="field-help">行动类型自动绑定三态：<span class="state-badge ' +
      app.stateMeta(currentState).cls +
      '" data-role="card-bound-state">' +
      (STATE_LABELS[currentState] || "空闲") +
      "</span> · 睡觉→休息 / 冲浪·闲逛→空闲 / 繁忙主业→繁忙</div></div>" +
      '<div class="field-grid">' +
      '<div class="field"><span class="field-label">最短时长（小时）</span>' +
      '<input class="input" type="number" min="0.5" max="8" step="0.5" data-role="card-min-hours" value="' +
      minHours.toFixed(1) +
      '" /></div>' +
      '<div class="field"><span class="field-label">最长时长（小时）</span>' +
      '<input class="input" type="number" min="0.5" max="8" step="0.5" data-role="card-max-hours" value="' +
      maxHours.toFixed(1) +
      '" /></div></div>' +
      '<div class="field-help">时长范围 0.5 ~ 8.0 小时，0.5 步进；最短 ≤ 最长。</div>' +
      '<div class="field"><span class="field-label">抽取权重</span><div class="slider-row">' +
      '<input class="slider" type="range" min="1" max="100" step="1" data-role="card-weight" value="' +
      weight +
      '" /><span class="slider-value" data-role="card-weight-value">' +
      weight +
      "</span></div></div>" +
      '<div class="field"><span class="field-label">作用域</span>' +
      '<select class="select" data-role="card-scope" ' +
      (isNew ? "" : "disabled") +
      '><option value="soul"' +
      (source.scope !== "global" ? " selected" : "") +
      ">当前角色专属卡</option><option value=\"global\"" +
      (source.scope === "global" ? " selected" : "") +
      ">全局内置卡池（所有角色共用）</option></select>" +
      (isNew
        ? ""
        : '<div class="field-help">已存在卡片的作用域固定；如需移动请新建后再删除旧卡。</div>') +
      "</div>";

    var typeSelect = body.querySelector('[data-role="card-action-type"]');
    var boundState = body.querySelector('[data-role="card-bound-state"]');
    if (typeSelect && boundState) {
      typeSelect.addEventListener("change", function () {
        var type = typeSelect.value;
        var stateId = actionStateOf(type) || "IDLE";
        var meta = app.stateMeta(stateId);
        boundState.textContent = STATE_LABELS[stateId] || meta.label;
        boundState.className = "state-badge " + meta.cls;
        boundState.setAttribute("data-role", "card-bound-state");
      });
    }
    function bindSlider(role, valueRole) {
      var slider = body.querySelector('[data-role="' + role + '"]');
      var valueEl = body.querySelector('[data-role="' + valueRole + '"]');
      if (slider && valueEl) {
        slider.addEventListener("input", function () {
          valueEl.textContent = slider.value;
        });
      }
    }
    bindSlider("card-weight", "card-weight-value");

    app.drawer({
      title: isNew ? "新增行动卡" : "编辑行动卡",
      body: body,
      actions: [
        { label: "取消" },
        {
          label: "保存",
          primary: true,
          icon: "save",
          onClick: async function () {
            var titleInput = body.querySelector('[data-role="card-title"]');
            var title = titleInput ? String(titleInput.value || "").trim() : "";
            if (!title) throw new Error("卡片标题不能为空");
            var typeEl = body.querySelector('[data-role="card-action-type"]');
            var actionType = typeEl ? typeEl.value : defaultType;
            var minEl = body.querySelector('[data-role="card-min-hours"]');
            var maxEl = body.querySelector('[data-role="card-max-hours"]');
            var minValue = roundHalf(minEl ? minEl.value : 1.0);
            var maxValue = roundHalf(maxEl ? maxEl.value : 2.5);
            if (!isFinite(minValue) || !isFinite(maxValue)) {
              throw new Error("时长必须是数字（0.5 ~ 8.0 小时）");
            }
            if (minValue < 0.5 || maxValue > 8.0) {
              throw new Error("时长范围必须在 0.5 ~ 8.0 小时之间");
            }
            if (minValue > maxValue) {
              throw new Error("最短时长不能大于最长时长");
            }
            var weightEl = body.querySelector('[data-role="card-weight"]');
            var scopeEl = body.querySelector('[data-role="card-scope"]');
            var scope = isNew && scopeEl ? scopeEl.value || "soul" : source.scope || "soul";
            var payload = {
              soul_id: app.state.currentSoulId,
              card_type: "action",
              scope: scope,
              card: {
                card_id: source.card_id || "",
                title: title,
                action_type: actionType,
                state: actionStateOf(actionType) || "IDLE",
                min_hours: minValue,
                max_hours: maxValue,
                base_weight: weightEl ? Number(weightEl.value) : 50,
                synergy_tags: source.synergy_tags || {}
              }
            };
            var data = await app.apiPost("cards/save", payload);
            if (data && (Array.isArray(data.custom_cards) || Array.isArray(data.general_cards))) {
              pageData.cards = data;
            }
            paint(container, app, {
              schedule: pageData.schedule,
              cards: pageData.cards
            });
            app.toast(isNew ? "已新增行动卡" : "已保存行动卡");
          }
        }
      ]
    });
  }

  /* ------------------------------------------------------------------ *
   * 事件绑定 / 渲染 / 生命周期
   * ------------------------------------------------------------------ */
  function bindEvents(container, app) {
    container.onclick = function (event) {
      var target = event.target;
      if (!target || !target.closest) return;

      var actionEl = target.closest("[data-action]");
      if (actionEl) {
        var action = actionEl.getAttribute("data-action");
        if (action === "refresh-page") {
          render(container, app);
        } else if (action === "reroll") {
          doReroll(app, container);
        } else if (action === "filter-cards") {
          pageData.filter = actionEl.getAttribute("data-filter") || "ALL";
          paint(container, app, {
            schedule: pageData.schedule,
            cards: pageData.cards
          });
        } else if (action === "new-card") {
          openCardDrawer(app, container, null);
        } else if (action === "delete-card") {
          doDeleteCard(
            app,
            container,
            actionEl.getAttribute("data-card-id"),
            actionEl.getAttribute("data-scope")
          );
        }
        return;
      }

      var seg = target.closest(".sched-seg.is-clickable");
      if (seg) {
        var slot = findSlot(pageData.schedule, seg.getAttribute("data-slot-key"));
        openSlotDrawer(app, container, slot);
        return;
      }

      var cardEl = target.closest(".action-card");
      if (cardEl) {
        var cardId = cardEl.getAttribute("data-card-id");
        var scope = cardEl.getAttribute("data-scope");
        var card = findCardById(cardId);
        if (card) openCardDrawer(app, container, card);
        else openCardDrawer(app, container, { card_id: cardId, scope: scope });
      }
    };
  }

  async function render(container, app) {
    try {
      container.innerHTML =
        '<div class="loading"><span class="spinner"></span><span>加载日程数据…</span></div>';
      var data = await loadData(app);
      pageData.schedule = data.schedule;
      pageData.cards = data.cards;
      container.innerHTML = buildHtml(app, data);
      bindEvents(container, app);
    } catch (err) {
      console.error("[OnlyYou] 日程页渲染失败：", err);
      container.innerHTML =
        '<div class="card"><div class="empty">' +
        icon("alert", 32) +
        '<div class="empty-title">日程数据加载失败</div>' +
        '<div class="empty-text">' +
        esc((err && (err.message || err)) || "未知错误") +
        "</div>" +
        '<div class="row mt-12"><button class="btn btn-outline" type="button" data-action="retry-schedule">重试</button></div></div></div>';
      container.onclick = function (event) {
        var target = event.target;
        if (target && target.closest && target.closest('[data-action="retry-schedule"]')) {
          render(container, app);
        }
      };
    }
  }

  function destroy() {
    pageData.schedule = null;
    pageData.cards = null;
    pageData.filter = "ALL";
  }

  window.OY_PAGES = window.OY_PAGES || {};
  window.OY_PAGES.schedule = {
    id: "schedule",
    title: "日程",
    icon: "calendar",
    render: render,
    destroy: destroy
  };
})();
