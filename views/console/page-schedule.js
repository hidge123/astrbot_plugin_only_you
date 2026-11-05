/**
 * OnlyYou Plugin Pages — 日程页 (`views/console/page-schedule.js`)
 *
 * 数据源：
 *  - 今日日程：ctx.state.activeSchedule（来自 init_summary / souls/detail；缺失时自动 GET souls/detail）
 *  - 卡片池：GET `cards/list?soul_id=...`
 *
 * 交互：
 *  - 24h 时间轴点击 → 右侧抽屉编辑（三态分段 + 护甲滑条）→ POST schedule/force_state
 *  - 行动卡网格（左边框状态色 / 迷你护甲条 / 6 时段小圆点 / 三态筛选分段）
 *  - 点击卡片 → 抽屉编辑 → POST cards/save；悬浮删除 → POST cards/delete
 *  - 重抽日程 → POST schedule/reroll
 *  - AI 生成卡片 → POST cards/ai_generate
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

  function padHour(hour) {
    var h = num(hour, 0) % 24;
    return (h < 10 ? "0" + h : String(h)) + ":00";
  }

  function slotDefsOf(app, cards) {
    var defs = (cards && cards.slot_definitions) || app.state.slotDefinitions || [];
    return Array.isArray(defs) ? defs : [];
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
   * 片段构建
   * ------------------------------------------------------------------ */
  function filterSegHtml() {
    var filters = [
      { id: "ALL", label: "全部" },
      { id: "IDLE", label: "空闲" },
      { id: "BUSY", label: "繁忙" },
      { id: "RESTING", label: "休息" }
    ];
    return filters
      .map(function (item) {
        return (
          '<button class="seg-btn' +
          (pageData.filter === item.id ? " active" : "") +
          '" type="button" data-action="filter-cards" data-filter="' +
          item.id +
          '"' +
          (item.id !== "ALL" ? ' data-state="' + item.id + '"' : "") +
          ">" +
          esc(item.label) +
          "</button>"
        );
      })
      .join("");
  }

  function cardHtml(app, card, slotDefs) {
    var meta = app.stateMeta(card.state);
    var validSlots = Array.isArray(card.valid_slots) ? card.valid_slots : [];
    var armor = Math.max(0, Math.min(100, num(card.armor, 0)));
    var dots = slotDefs
      .map(function (def) {
        var on = validSlots.indexOf(def.slot_key) >= 0;
        return (
          '<span class="slot-dot' +
          (on ? " on" : "") +
          '" data-tip="' +
          esc((def.label || def.slot_key) + "（" + padHour(def.start_hour) + "–" + padHour(def.end_hour) + "）" + (on ? " · 可抽取" : " · 不适用")) +
          '"></span>'
        );
      })
      .join("");
    var weight = Math.max(1, Math.min(100, num(card.base_weight, 50)));
    return (
      '<div class="action-card ' +
      meta.cls +
      '" data-card-id="' +
      esc(card.card_id || "") +
      '" data-scope="' +
      esc(card.scope || "soul") +
      '" data-tip="点击编辑该行动卡">' +
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
      '<div class="action-card-meta"><span class="state-badge ' +
      meta.cls +
      '">' +
      meta.label +
      '</span><span class="tag tag-scope">' +
      (card.scope === "global" ? "内置" : "自定义") +
      '</span><span class="tag tag-muted" data-tip="抽卡权重">权重 ' +
      weight +
      "</span></div>" +
      '<div class="action-armor"><span>护甲</span><span class="mini-bar"><i style="width:' +
      armor +
      '%"></i></span><span class="action-armor-value">' +
      armor +
      "</span></div>" +
      '<div class="slot-dots">' +
      dots +
      "</div></div>"
    );
  }

  function cardsBlock(app, data) {
    var cards = data.cards || {};
    var slotDefs = slotDefsOf(app, cards);
    var allCards = []
      .concat(Array.isArray(cards.custom_cards) ? cards.custom_cards : [])
      .concat(Array.isArray(cards.general_cards) ? cards.general_cards : []);
    var filter = pageData.filter;
    var filtered =
      filter === "ALL"
        ? allCards
        : allCards.filter(function (card) {
            return String(card.state || "").toUpperCase() === filter;
          });
    var body = filtered.length
      ? '<div class="action-grid">' +
        filtered
          .map(function (card) {
            return cardHtml(app, card, slotDefs);
          })
          .join("") +
        "</div>"
      : '<div class="empty">' +
        icon("grid", 30) +
        '<div class="empty-title">该状态下暂无行动卡</div>' +
        '<div class="empty-text">切换筛选条件，或点击右上角「新增」创建一张；也可以让 AI 根据角色卡自动生成。</div></div>';
    return (
      '<section class="card"><div class="card-head">' +
      '<div class="card-title">' +
      icon("list", 15) +
      "行动卡池" +
      '<span class="badge-count">' +
      allCards.length +
      "</span>" +
      '<span class="card-sub">自定义 ' +
      (cards.custom_cards || []).length +
      " · 内置 " +
      (cards.general_cards || []).length +
      "</span></div>" +
      '<div class="card-actions">' +
      '<div class="segmented">' +
      filterSegHtml() +
      "</div>" +
      '<button class="btn btn-outline btn-sm" type="button" data-action="new-card">' +
      icon("plus", 13) +
      "新增</button>" +
      "</div></div>" +
      body +
      "</section>"
    );
  }

  function encountersBlock(app, data) {
    var cards = data.cards || {};
    var custom = Array.isArray(cards.custom_encounters) ? cards.custom_encounters : [];
    var general = Array.isArray(cards.general_encounters) ? cards.general_encounters : [];
    function row(item, scope) {
      var content = item && item.content ? item.content : String(item || "");
      return (
        '<div class="encounter-item" data-tip="' +
        esc(content) +
        '">' +
        '<span class="tag tag-scope">' +
        (scope === "global" ? "内置" : "自定义") +
        "</span>" +
        '<span class="encounter-text">' +
        esc(content) +
        "</span>" +
        (scope === "soul"
          ? '<button class="icon-btn" type="button" data-action="delete-encounter" data-content="' +
            esc(content) +
            '" data-tip="删除奇遇卡" aria-label="删除奇遇卡">' +
            icon("trash", 13) +
            "</button>"
          : "") +
        "</div>"
      );
    }
    var rows = custom
      .map(function (item) {
        return row(item, "soul");
      })
      .concat(
        general.slice(0, 4).map(function (item) {
          return row(item, "global");
        })
      )
      .join("");
    return (
      '<section class="card"><div class="card-head">' +
      '<div class="card-title">' +
      icon("spark", 15) +
      "奇遇谈资卡池" +
      '<span class="badge-count">' +
      (custom.length + general.length) +
      "</span>" +
      '<span class="card-sub">聊天中聊到对应话题后标记为「已聊」</span></div>' +
      '<div class="card-actions"><button class="btn btn-outline btn-sm" type="button" data-action="new-encounter">' +
      icon("plus", 13) +
      "新增</button></div></div>" +
      '<div class="card-body"><div class="encounter-list">' +
      (rows || '<div class="stat-empty">还没有奇遇谈资卡</div>') +
      "</div></div></section>"
    );
  }

  function buildHtml(app, data) {
    var schedule = data.schedule || {};
    var run = schedule.run || {};
    var slots = Array.isArray(run.slots) ? run.slots : [];
    var currentKey =
      (schedule.current_slot && schedule.current_slot.slot_key) || "";
    var cards = data.cards || {};
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
      '<button class="btn btn-ghost btn-sm" type="button" data-action="ai-generate">' +
      icon("wand", 14) +
      "AI 生成卡片</button>" +
      '<button class="btn btn-primary btn-sm" type="button" data-action="reroll">' +
      icon("spark", 14) +
      "重抽日程</button>" +
      "</div></div>";

    /* 24h 时间轴 */
    html += '<section class="card"><div class="card-head">';
    html +=
      '<div class="card-title">' +
      icon("clock", 15) +
      "24 小时时间轴" +
      '<span class="card-sub">点击时段打开右侧抽屉编辑</span></div>';
    html +=
      '<div class="card-actions"><span class="chip" data-tip="今日基调">' +
      icon("spark", 12) +
      esc(run.vigor_title || "今日基调") +
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
        '<div class="axis-legend mt-12"><span class="legend-item"><span class="legend-dot" style="background:var(--state-idle)"></span>空闲</span>' +
        '<span class="legend-item"><span class="legend-dot" style="background:var(--state-busy)"></span>繁忙</span>' +
        '<span class="legend-item"><span class="legend-dot" style="background:var(--state-resting)"></span>休息</span>' +
        '<span class="nowrap">现在 ' +
        app.formatClock(Date.now()) +
        " · 共 " +
        slots.length +
        " 个时段</span></div>";
    } else {
      html += '<div class="stat-empty">今日日程尚未生成，点击右上角「重抽日程」抽取。</div>';
    }
    html += "</div></section>";

    html += cardsBlock(app, data);
    html += encountersBlock(app, data);
    return html;
  }

  function paint(container, app, data) {
    pageData.schedule = data.schedule || pageData.schedule;
    pageData.cards = data.cards || pageData.cards;
    container.innerHTML = buildHtml(app, data);
    bindEvents(container, app);
  }

  /* ------------------------------------------------------------------ *
   * 交互：时间轴抽屉 / 重抽 / AI 生成
   * ------------------------------------------------------------------ */
  function findSlot(schedule, slotKey) {
    var slots =
      (schedule && schedule.run && schedule.run.slots) || [];
    for (var i = 0; i < slots.length; i++) {
      if (slots[i].slot_key === slotKey) return slots[i];
    }
    if (schedule && schedule.current_slot && schedule.current_slot.slot_key === slotKey) {
      return schedule.current_slot;
    }
    return null;
  }

  function openSlotDrawer(app, container, slot) {
    if (!slot) {
      app.toast("未找到该时段数据", true);
      return;
    }
    var body = document.createElement("div");
    body.innerHTML =
      '<div class="card-sub mb-12">' +
      esc((slot.label || slot.slot_key) + " · " + padHour(slot.start_hour) + " – " + padHour(slot.end_hour)) +
      "</div>" +
      app.slotStateFormHtml(slot) +
      '<div class="hint mt-12">保存后立即生效（POST schedule/force_state），会覆盖该时段今日抽到的活动。</div>';
    app.bindSlotStateForm(body);
    app.drawer({
      title: "编辑时段 · " + (slot.label || slot.slot_key || ""),
      body: body,
      actions: [
        { label: "取消" },
        {
          label: "保存",
          primary: true,
          icon: "save",
          onClick: async function () {
            var form = app.readSlotStateForm(body);
            await app.forceState({
              slot_key: slot.slot_key,
              new_state: form.new_state,
              armor: form.armor,
              new_title: form.new_title
            });
            app.toast("已更新「" + (slot.label || slot.slot_key) + "」");
            paint(container, app, {
              schedule: app.state.activeSchedule,
              cards: pageData.cards
            });
          }
        }
      ]
    });
  }

  async function doReroll(app, container) {
    await app.confirm({
      title: "重抽今日日程",
      message: "将重新抽取今日 6 个时段的状态与奇遇谈资卡，当前时段的活动会被覆盖。",
      confirmLabel: "重抽",
      onConfirm: async function () {
        var data = await app.apiPost("schedule/reroll", {
          soul_id: app.state.currentSoulId
        });
        if (data && data.schedule) app.state.activeSchedule = data.schedule;
        paint(container, app, {
          schedule: app.state.activeSchedule,
          cards: pageData.cards
        });
        app.toast("已重新抽取今日日程");
      }
    });
  }

  async function doAiGenerate(app, container) {
    var btn = container.querySelector('[data-action="ai-generate"]');
    if (btn) btn.classList.add("is-loading");
    try {
      var data = await app.apiPost("cards/ai_generate", {
        soul_id: app.state.currentSoulId,
        auto_save: true
      });
      var generatedCards = (data && Array.isArray(data.generated_cards)) ? data.generated_cards : [];
      var generatedEncounters =
        (data && Array.isArray(data.generated_encounters)) ? data.generated_encounters : [];
      paint(container, app, {
        schedule: pageData.schedule,
        cards: data && data.custom_cards ? data : pageData.cards
      });
      app.toast(
        "AI 已生成 " + generatedCards.length + " 张行动卡与 " + generatedEncounters.length + " 张奇遇卡"
      );
    } catch (err) {
      app.toast("AI 生成失败：" + (err.message || err), true);
    } finally {
      if (btn) btn.classList.remove("is-loading");
    }
  }

  async function doDeleteCard(app, container, cardId, scope) {
    var card = null;
    var all = []
      .concat((pageData.cards && pageData.cards.custom_cards) || [])
      .concat((pageData.cards && pageData.cards.general_cards) || []);
    for (var i = 0; i < all.length; i++) {
      if (all[i].card_id === cardId) {
        card = all[i];
        break;
      }
    }
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
        paint(container, app, {
          schedule: pageData.schedule,
          cards: data && data.custom_cards ? data : pageData.cards
        });
        app.toast("已删除行动卡");
      }
    });
  }

  /* ------------------------------------------------------------------ *
   * 交互：行动卡抽屉编辑
   * ------------------------------------------------------------------ */
  function openCardDrawer(app, container, card) {
    var isNew = !card;
    var slotDefs = slotDefsOf(app, pageData.cards);
    var source = card || {
      card_id: "",
      title: "",
      state: "IDLE",
      valid_slots: slotDefs.map(function (def) {
        return def.slot_key;
      }),
      armor: 45,
      base_weight: 50,
      scope: "soul"
    };
    var meta = app.stateMeta(source.state);
    var validSlots = Array.isArray(source.valid_slots) ? source.valid_slots : [];
    var body = document.createElement("div");
    body.innerHTML =
      '<div class="field"><span class="field-label">卡片标题（正在做的事）</span>' +
      '<input class="input" data-role="card-title" maxlength="120" placeholder="如：窝在沙发里刷短视频" value="' +
      esc(source.title || "") +
      '" /></div>' +
      '<div class="field"><span class="field-label">状态</span><div class="segmented" data-role="card-state">' +
      ["IDLE", "BUSY", "RESTING"]
        .map(function (id) {
          var m = app.stateMeta(id);
          return (
            '<button class="seg-btn' +
            (meta.id === id ? " active" : "") +
            '" type="button" data-state="' +
            id +
            '">' +
            m.label +
            "</button>"
          );
        })
        .join("") +
      "</div></div>" +
      '<div class="field"><span class="field-label">有效时段</span><div class="row wrap" data-role="card-slots" style="gap:8px 16px">' +
      slotDefs
        .map(function (def) {
          var checked = validSlots.indexOf(def.slot_key) >= 0;
          return (
            '<label class="switch-label"><input type="checkbox" data-slot="' +
            esc(def.slot_key) +
            '"' +
            (checked ? " checked" : "") +
            " /> " +
            esc(def.label || def.slot_key) +
            "（" +
            padHour(def.start_hour) +
            "–" +
            padHour(def.end_hour) +
            "）</label>"
          );
        })
        .join("") +
      "</div></div>" +
      '<div class="field"><span class="field-label">定力护甲</span><div class="slider-row">' +
      '<input class="slider" type="range" min="0" max="100" step="1" data-role="card-armor" value="' +
      num(source.armor, 45) +
      '" /><span class="slider-value" data-role="card-armor-value">' +
      num(source.armor, 45) +
      "</span></div></div>" +
      '<div class="field"><span class="field-label">抽取权重</span><div class="slider-row">' +
      '<input class="slider" type="range" min="1" max="100" step="1" data-role="card-weight" value="' +
      num(source.base_weight, 50) +
      '" /><span class="slider-value" data-role="card-weight-value">' +
      num(source.base_weight, 50) +
      "</span></div></div>" +
      '<div class="field"><span class="field-label">作用域</span>' +
      '<select class="select" data-role="card-scope" ' +
      (isNew ? "" : "disabled") +
      "><option value=\"soul\"" +
      (source.scope !== "global" ? " selected" : "") +
      ">当前角色专属卡</option><option value=\"global\"" +
      (source.scope === "global" ? " selected" : "") +
      ">全局内置卡池（所有角色共用）</option></select>" +
      (isNew
        ? ""
        : '<div class="field-help">已存在卡片的作用域固定；如需移动请新建后再删除旧卡。</div>') +
      "</div>";

    var stateSeg = body.querySelector('[data-role="card-state"]');
    if (stateSeg) {
      stateSeg.addEventListener("click", function (event) {
        var btn = event.target.closest("[data-state]");
        if (!btn) return;
        stateSeg.querySelectorAll(".seg-btn").forEach(function (el) {
          el.classList.remove("active");
        });
        btn.classList.add("active");
      });
    }
    var armorSlider = body.querySelector('[data-role="card-armor"]');
    var armorValue = body.querySelector('[data-role="card-armor-value"]');
    if (armorSlider && armorValue) {
      armorSlider.addEventListener("input", function () {
        armorValue.textContent = armorSlider.value;
      });
    }
    var weightSlider = body.querySelector('[data-role="card-weight"]');
    var weightValue = body.querySelector('[data-role="card-weight-value"]');
    if (weightSlider && weightValue) {
      weightSlider.addEventListener("input", function () {
        weightValue.textContent = weightSlider.value;
      });
    }

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
            var activeState = body.querySelector('[data-role="card-state"] .seg-btn.active');
            var checkedSlots = Array.prototype.slice
              .call(body.querySelectorAll('[data-role="card-slots"] input[type="checkbox"]:checked'))
              .map(function (el) {
                return el.getAttribute("data-slot");
              });
            if (!checkedSlots.length) throw new Error("至少选择一个有效时段");
            var armor = body.querySelector('[data-role="card-armor"]');
            var weight = body.querySelector('[data-role="card-weight"]');
            var scopeEl = body.querySelector('[data-role="card-scope"]');
            var payload = {
              soul_id: app.state.currentSoulId,
              card_type: "action",
              scope: source.scope || "soul",
              card: {
                card_id: source.card_id || "",
                title: title,
                state: activeState ? activeState.getAttribute("data-state") : "IDLE",
                valid_slots: checkedSlots,
                armor: armor ? Number(armor.value) : 45,
                base_weight: weight ? Number(weight.value) : 50,
                synergy_tags: source.synergy_tags || {}
              }
            };
            if (isNew && scopeEl) payload.scope = scopeEl.value || "soul";
            var data = await app.apiPost("cards/save", payload);
            pageData.cards = data && data.custom_cards ? data : pageData.cards;
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
   * 交互：奇遇卡
   * ------------------------------------------------------------------ */
  function openEncounterModal(app, container) {
    var body = document.createElement("div");
    body.innerHTML =
      '<div class="field"><span class="field-label">奇遇小事（作为当天短时记忆提供主动聊天话题）</span>' +
      '<textarea class="textarea" data-role="encounter-content" rows="3" maxlength="120" placeholder="如：今天中午点外卖老板居然多送了一根烤肠！"></textarea></div>';
    app.modal({
      title: "新增奇遇谈资卡",
      body: body,
      actions: [
        { label: "取消" },
        {
          label: "保存",
          primary: true,
          icon: "save",
          onClick: async function () {
            var input = body.querySelector('[data-role="encounter-content"]');
            var content = input ? String(input.value || "").trim() : "";
            if (!content) throw new Error("奇遇内容不能为空");
            var data = await app.apiPost("cards/save", {
              soul_id: app.state.currentSoulId,
              card_type: "encounter",
              scope: "soul",
              content: content
            });
            paint(container, app, {
              schedule: pageData.schedule,
              cards: data && data.custom_encounters ? data : pageData.cards
            });
            app.toast("已新增奇遇谈资卡");
          }
        }
      ]
    });
  }

  async function doDeleteEncounter(app, container, content) {
    await app.confirm({
      title: "删除奇遇谈资卡",
      message: "确定删除「" + content + "」吗？",
      confirmLabel: "删除",
      danger: true,
      onConfirm: async function () {
        var data = await app.apiPost("cards/delete", {
          soul_id: app.state.currentSoulId,
          card_type: "encounter",
          scope: "soul",
          content: content
        });
        paint(container, app, {
          schedule: pageData.schedule,
          cards: data && data.custom_encounters ? data : pageData.cards
        });
        app.toast("已删除奇遇谈资卡");
      }
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
        } else if (action === "ai-generate") {
          doAiGenerate(app, container);
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
        } else if (action === "new-encounter") {
          openEncounterModal(app, container);
        } else if (action === "delete-encounter") {
          doDeleteEncounter(app, container, actionEl.getAttribute("data-content"));
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
        var card = null;
        var all = []
          .concat((pageData.cards && pageData.cards.custom_cards) || [])
          .concat((pageData.cards && pageData.cards.general_cards) || []);
        for (var i = 0; i < all.length; i++) {
          if (all[i].card_id === cardId) {
            card = all[i];
            break;
          }
        }
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
