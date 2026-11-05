/**
 * OnlyYou Plugin Pages — 会话页 (`views/console/page-sessions.js`)
 *
 * 内容：
 *  ① 插件默认角色：POST `sessions/default_soul`（{soul_id}）→ {default_soul_id, souls, umo_bindings}
 *  ② UMO 绑定表：列出 known_umos（会话滑窗 / L3 私聊 / 已绑定），逐行绑定或解绑。
 *     绑定复用 `souls/save` 的 `umo_binding: {umo, soul_id}` 字段（后端只提供 souls/save 时也兼容），
 *     请求中同时回传该角色的已有字段 / card，避免旧后端把档案覆盖成默认值。
 *
 * 注册：window.OY_PAGES.sessions = { id,title,icon,render(container,ctx),destroy() }
 */
(function () {
  "use strict";

  function esc(str) {
    return window.OY_ESC ? window.OY_ESC(str) : String(str == null ? "" : str);
  }

  function icon(name, size) {
    return typeof window.icon === "function" ? window.icon(name, size || 16) : "";
  }

  function findSoul(app, soulId) {
    var souls = app.state.souls || [];
    for (var i = 0; i < souls.length; i++) {
      if (souls[i] && souls[i].soul_id === soulId) return souls[i];
    }
    return null;
  }

  /** 构造兼容新旧后端的 souls/save 请求体（保留已有档案字段，避免被覆盖） */
  function soulSavePayload(app, soulId, extra) {
    var soul = findSoul(app, soulId) || app.state.currentSoul || {};
    var payload = {
      soul_id: soul.soul_id || soulId || app.state.currentSoulId
    };
    if (soul.name != null) payload.name = soul.name;
    if (soul.archetype != null) payload.archetype = soul.archetype;
    if (soul.speaking_style != null) payload.speaking_style = soul.speaking_style;
    if (Array.isArray(soul.surfing_preferences)) payload.surfing_preferences = soul.surfing_preferences;
    if (soul.native_persona_prompt != null) payload.native_persona_prompt = soul.native_persona_prompt;
    if (soul.card && typeof soul.card === "object") payload.card = soul.card;
    var extraKeys = Object.keys(extra || {});
    for (var i = 0; i < extraKeys.length; i++) {
      payload[extraKeys[i]] = extra[extraKeys[i]];
    }
    return payload;
  }

  function umoRows(app) {
    var rows = [];
    var seen = {};
    var known = Array.isArray(app.state.knownUmos) ? app.state.knownUmos : [];
    known.forEach(function (item) {
      if (!item || !item.umo || seen[item.umo]) return;
      seen[item.umo] = true;
      rows.push({
        umo: item.umo,
        bound_soul_id: item.bound_soul_id || (app.state.umoBindings || {})[item.umo] || "",
        window_messages: Number(item.window_messages) || 0,
        is_l3_private: !!item.is_l3_private
      });
    });
    var bindings = app.state.umoBindings || {};
    Object.keys(bindings).forEach(function (umo) {
      if (!umo || seen[umo]) return;
      seen[umo] = true;
      rows.push({
        umo: umo,
        bound_soul_id: bindings[umo] || "",
        window_messages: 0,
        is_l3_private: false
      });
    });
    rows.sort(function (a, b) {
      return a.umo < b.umo ? -1 : a.umo > b.umo ? 1 : 0;
    });
    return rows;
  }

  function umoTypeLabel(row) {
    if (row.is_l3_private) return '<span class="tag tag-primary">L3 私聊</span>';
    var text = String(row.umo || "").toLowerCase();
    if (text.indexOf("group") >= 0) return '<span class="tag">群聊</span>';
    if (text.indexOf("private") >= 0 || text.indexOf("friend") >= 0) return '<span class="tag">私聊</span>';
    return '<span class="tag tag-muted">未知</span>';
  }

  function soulOptions(app, selectedId, withFollow) {
    var souls = app.state.souls || [];
    var html = "";
    if (withFollow) html += '<option value="">跟随默认角色</option>';
    souls.forEach(function (soul) {
      html +=
        '<option value="' +
        esc(soul.soul_id) +
        '"' +
        (soul.soul_id === selectedId ? " selected" : "") +
        ">" +
        esc((soul.name || soul.soul_id) + " · " + soul.soul_id) +
        "</option>";
    });
    return html;
  }

  function defaultCardHtml(app) {
    var souls = app.state.souls || [];
    var explicit = app.state.defaultSoulId || "";
    var selected = explicit || app.state.currentSoulId || (souls[0] && souls[0].soul_id) || "";
    var selectedSoul = findSoul(app, selected);
    var label = explicit
      ? (selectedSoul ? selectedSoul.name + "（" + explicit + "）" : explicit)
      : "未显式设置 → 兜底使用最近更新的角色";
    return (
      '<section class="card"><div class="card-head">' +
      '<div class="card-title">' +
      icon("character", 15) +
      '默认角色<span class="card-sub">新会话兜底</span></div>' +
      '<span class="tag ' +
      (explicit ? "tag-primary" : "tag-muted") +
      '">' +
      (explicit ? "已设置" : "自动") +
      "</span></div>" +
      '<div class="card-body">' +
      '<div class="field"><span class="field-label">插件默认角色</span>' +
      '<div class="row"><select class="select" data-role="default-soul">' +
      soulOptions(app, selected, false) +
      "</select>" +
      '<button class="btn btn-primary btn-sm" type="button" data-action="save-default" disabled>' +
      icon("save", 13) +
      "保存</button></div>" +
      '<div class="field-help">聊天路由优先级：UMO 绑定 → 插件默认角色 → 最近更新的角色；不再读取 AstrBot 的 default_personality。</div>' +
      "</div>" +
      '<div class="stat-desc mt-12" data-role="default-hint">当前默认：' +
      esc(label) +
      "</div></div></section>"
    );
  }

  function routingCardHtml(app) {
    var rows = umoRows(app);
    var bound = rows.filter(function (row) {
      return !!row.bound_soul_id;
    }).length;
    return (
      '<section class="card"><div class="card-head"><div class="card-title">' +
      icon("info", 15) +
      '路由规则</div></div><div class="card-body">' +
      '<div class="key-value">' +
      "<dt>已知会话</dt><dd>" +
      rows.length +
      " 个（绑定 " +
      bound +
      " 个）</dd>" +
      "<dt>优先级 1</dt><dd>UMO 绑定表（下方表格显式指定）</dd>" +
      "<dt>优先级 2</dt><dd>插件默认角色（左侧保存）</dd>" +
      "<dt>优先级 3</dt><dd>最近更新的角色</dd>" +
      "<dt>注入方式</dt><dd>角色卡 → 替换 System Prompt 的 Persona Instructions 段</dd>" +
      "</div></div></section>"
    );
  }

  function bindingsCardHtml(app) {
    var rows = umoRows(app);
    var souls = app.state.souls || [];
    var body = "";
    if (!souls.length) {
      body =
        '<div class="empty">' +
        icon("character", 30) +
        '<div class="empty-title">还没有任何角色</div>' +
        '<div class="empty-text">请先在「角色卡」页创建/导入角色，再绑定会话。</div></div>';
    } else if (!rows.length) {
      body =
        '<div class="empty">' +
        icon("sessions", 30) +
        '<div class="empty-title">还没有已知会话</div>' +
        '<div class="empty-text">在群聊/私聊里和 Bot 说句话，会话 UMO 会出现在这里；也可以手动输入 UMO 绑定。</div></div>';
    } else {
      var trs = rows
        .map(function (row) {
          return (
            '<tr data-umo="' +
            esc(row.umo) +
            '"><td><div class="umo-cell"><span class="umo-name">' +
            esc(row.umo) +
            '</span><span class="umo-meta">' +
            (row.window_messages ? "滑窗 " + row.window_messages + " 条消息" : "暂无滑窗消息") +
            "</span></div></td>" +
            "<td>" +
            row.window_messages +
            "</td>" +
            "<td>" +
            umoTypeLabel(row) +
            "</td>" +
            '<td><select class="select-sm" data-role="row-soul" data-umo="' +
            esc(row.umo) +
            '">' +
            soulOptions(app, row.bound_soul_id, true) +
            "</select></td>" +
            '<td class="cell-actions">' +
            '<button class="btn btn-outline btn-sm" type="button" data-action="save-binding" data-umo="' +
            esc(row.umo) +
            '">保存</button>' +
            (row.bound_soul_id
              ? '<button class="btn btn-ghost btn-sm" type="button" data-action="unbind" data-umo="' +
                esc(row.umo) +
                '">解绑</button>'
              : "") +
            "</td></tr>"
          );
        })
        .join("");
      body =
        '<div class="table-wrap" style="border:0;border-radius:0"><table class="table"><thead><tr>' +
        "<th>会话 UMO</th><th>滑窗消息</th><th>类型</th><th>绑定角色</th><th style=\"text-align:right\">操作</th>" +
        "</tr></thead><tbody>" +
        trs +
        "</tbody></table></div>";
    }
    return (
      '<section class="card"><div class="card-head">' +
      '<div class="card-title">' +
      icon("sessions", 15) +
      'UMO 绑定表<span class="badge-count">' +
      rows.length +
      '</span><span class="card-sub">为指定群聊 / 私聊固定角色</span></div>' +
      '<div class="card-actions"><button class="btn btn-ghost btn-sm" type="button" data-action="refresh-page">' +
      icon("refresh", 13) +
      "刷新</button></div></div>" +
      (souls.length
        ? '<div class="card-body tight"><div class="row wrap">' +
          '<input class="input inline-input grow" type="text" data-role="add-umo-input" placeholder="手动输入 UMO，如 qq:GroupMessage:123456" />' +
          '<span class="select-inline"><select class="select" data-role="add-soul-select">' +
          soulOptions(app, app.state.currentSoulId, false) +
          "</select></span>" +
          '<button class="btn btn-outline btn-sm" type="button" data-action="add-binding">' +
          icon("link", 13) +
          "绑定会话</button></div>" +
          '<div class="field-help mt-4">UMO 格式：平台 ID + 消息类型 + 会话 ID（AstrBot 会话标识，可在日志/平台适配器里找到）。</div></div>'
        : "") +
      body +
      "</section>"
    );
  }

  function buildHtml(app) {
    return (
      '<div class="page-head"><h1 class="page-title">' +
      icon("sessions", 18) +
      "会话" +
      "</h1>" +
      '<div class="page-actions">' +
      '<button class="btn btn-ghost btn-sm" type="button" data-action="refresh-page">' +
      icon("refresh", 14) +
      "重新加载</button>" +
      "</div></div>" +
      '<div class="grid-2">' +
      defaultCardHtml(app) +
      routingCardHtml(app) +
      "</div>" +
      bindingsCardHtml(app)
    );
  }

  /* ------------------------------------------------------------------ *
   * 交互
   * ------------------------------------------------------------------ */
  async function saveDefault(app, container) {
    var select = container.querySelector('[data-role="default-soul"]');
    if (!select) return;
    var soulId = select.value;
    if (!soulId) {
      app.toast("请选择默认角色", true);
      return;
    }
    var btn = container.querySelector('[data-action="save-default"]');
    if (btn) btn.classList.add("is-loading");
    try {
      var data = await app.apiPost("sessions/default_soul", { soul_id: soulId });
      if (data) {
        app.state.defaultSoulId =
          data.default_soul_id != null ? data.default_soul_id : soulId;
        if (Array.isArray(data.souls)) app.applySouls(data.souls);
        if (data.umo_bindings) app.applyUmoBindings(data.umo_bindings);
      } else {
        app.state.defaultSoulId = soulId;
      }
      container.innerHTML = buildHtml(app);
      bindEvents(container, app);
      app.toast("已设置插件默认角色");
    } catch (err) {
      app.toast("保存默认角色失败：" + (err.message || err), true);
    } finally {
      if (btn) btn.classList.remove("is-loading");
    }
  }

  async function saveBinding(app, container, umo, soulId) {
    if (!umo) return;
    var targetSoulId = soulId || "";
    var saveSoulId = targetSoulId || app.state.currentSoulId || (app.state.souls[0] && app.state.souls[0].soul_id);
    if (!saveSoulId) {
      app.toast("没有可用的角色，无法保存绑定", true);
      return;
    }
    var payload = soulSavePayload(app, saveSoulId, {
      umo_binding: { umo: umo, soul_id: targetSoulId }
    });
    try {
      var data = await app.apiPost("souls/save", payload);
      if (data) {
        if (Array.isArray(data.souls)) app.applySouls(data.souls);
        if (data.umo_bindings) app.applyUmoBindings(data.umo_bindings, data.known_umos);
      } else if (targetSoulId) {
        app.state.umoBindings[umo] = targetSoulId;
      } else {
        delete app.state.umoBindings[umo];
      }
      container.innerHTML = buildHtml(app);
      bindEvents(container, app);
      app.toast(targetSoulId ? "已将会话绑定到指定角色" : "已解除该会话的绑定");
    } catch (err) {
      app.toast("保存绑定失败：" + (err.message || err), true);
      throw err;
    }
  }

  function collectRowSoul(container, umo) {
    var select = container.querySelector('[data-role="row-soul"][data-umo="' + umo.replace(/"/g, '\\"') + '"]');
    return select ? select.value : "";
  }

  async function doSaveRow(app, container, umo, unbind) {
    var soulId = unbind ? "" : collectRowSoul(container, umo);
    await saveBinding(app, container, umo, soulId);
  }

  async function doAddBinding(app, container) {
    var input = container.querySelector('[data-role="add-umo-input"]');
    var select = container.querySelector('[data-role="add-soul-select"]');
    var umo = input ? String(input.value || "").trim() : "";
    var soulId = select ? select.value : "";
    if (!umo) {
      app.toast("请先填写会话 UMO", true);
      if (input) input.focus();
      return;
    }
    if (!soulId) {
      app.toast("请选择要绑定的角色", true);
      return;
    }
    await saveBinding(app, container, umo, soulId);
  }

  function bindEvents(container, app) {
    var defaultSelect = container.querySelector('[data-role="default-soul"]');
    if (defaultSelect) {
      defaultSelect.addEventListener("change", function () {
        var btn = container.querySelector('[data-action="save-default"]');
        if (btn) btn.disabled = false;
      });
    }

    container.onclick = function (event) {
      var target = event.target;
      if (!target || !target.closest) return;
      var actionEl = target.closest("[data-action]");
      if (!actionEl) return;
      var action = actionEl.getAttribute("data-action");
      if (action === "refresh-page") {
        app.refresh();
      } else if (action === "save-default") {
        saveDefault(app, container);
      } else if (action === "save-binding") {
        doSaveRow(app, container, actionEl.getAttribute("data-umo"), false);
      } else if (action === "unbind") {
        doSaveRow(app, container, actionEl.getAttribute("data-umo"), true);
      } else if (action === "add-binding") {
        doAddBinding(app, container);
      }
    };
  }

  async function render(container, app) {
    container.innerHTML = buildHtml(app);
    bindEvents(container, app);
  }

  function destroy() {
    /* 会话页无定时器 / 无外部订阅，无需清理 */
  }

  window.OY_PAGES = window.OY_PAGES || {};
  window.OY_PAGES.sessions = {
    id: "sessions",
    title: "会话",
    icon: "sessions",
    render: render,
    destroy: destroy
  };
})();
