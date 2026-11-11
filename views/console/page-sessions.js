/**
 * OnlyYou Plugin Pages — 机器人接口页 (`views/console/page-sessions.js`)
 *
 * v2.8 重写：从「默认角色 + UMO 绑定表」改为「当前角色的机器人接口管理」。
 * 路由优先级（v2.8）：平台绑定（umo 第一段）→ 旧 UMO 兼容绑定 → 默认角色 → 最近更新角色。
 *
 * 数据源（契约 §6.2）：
 *   GET  `sessions/platforms?soul_id=` → {current_soul_id, default_soul_id, souls, platforms, sessions}
 *   POST `sessions/bind_platform`   {soul_id, platform_id} → {soul_id, platform_id, souls, platforms}
 *   POST `sessions/unbind_platform` {soul_id, platform_id} → {platform_id, previous_soul_id, cleared, souls, platforms}
 *   POST `sessions/clear_memory`    {soul_id, umo} / {soul_id, platform_id} → {cleared, sessions}
 *   POST `sessions/default_soul`    {soul_id} → {default_soul_id, souls, umo_bindings}
 *
 * 页面结构：
 *  区块 A 机器人接口列表：已绑定当前角色（绿色标签 + 解绑并清理内存）/ 已被其他角色占用（灰色、禁用）/ 未绑定（绑定到当前角色）；
 *  区块 B 手动输入 platform_id 绑定；
 *  区块 C 默认兜底角色（把当前角色设为 default_soul_id）；
 *  区块 D 当前角色管辖的活跃会话内存概览（单会话清空 / 整个平台清空）。
 *
 * 注册：window.OY_PAGES.sessions = { id, title, icon, render(container, ctx), destroy() }
 */
(function () {
  "use strict";

  var S = {
    app: null,
    container: null,
    destroyed: false,
    data: null,
    manualPlatform: "",
    busy: false
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
    if (!ts) return "—";
    var now = Math.floor(Date.now() / 1000);
    var diff = now - ts;
    if (diff < 0) diff = 0;
    if (diff < 60) return "刚刚";
    if (diff < 3600) return Math.floor(diff / 60) + " 分钟前";
    if (diff < 86400) return Math.floor(diff / 3600) + " 小时前";
    if (diff < 86400 * 7) return Math.floor(diff / 86400) + " 天前";
    var d = new Date(ts * 1000);
    return (
      d.getFullYear() +
      "-" +
      pad2(d.getMonth() + 1) +
      "-" +
      pad2(d.getDate()) +
      " " +
      pad2(d.getHours()) +
      ":" +
      pad2(d.getMinutes())
    );
  }

  function soulId() {
    return (S.app && S.app.state && S.app.state.currentSoulId) || "";
  }

  function currentSoulLabel() {
    var app = S.app;
    var soul = app && app.state && app.state.currentSoul;
    if (soul && (soul.name || soul.soul_id)) return String(soul.name || soul.soul_id);
    return soulId() || "—";
  }

  function pageData() {
    return S.data && typeof S.data === "object" ? S.data : {};
  }

  function souls() {
    return Array.isArray(pageData().souls) ? pageData().souls : [];
  }

  function platforms() {
    return Array.isArray(pageData().platforms) ? pageData().platforms : [];
  }

  function sessions() {
    return Array.isArray(pageData().sessions) ? pageData().sessions : [];
  }

  function soulNameOf(id) {
    var key = String(id || "");
    if (!key) return "";
    var list = souls();
    for (var i = 0; i < list.length; i++) {
      if (list[i] && String(list[i].soul_id) === key) {
        return String(list[i].name || list[i].soul_id);
      }
    }
    return key;
  }

  /** 平台相对当前角色的绑定状态：mine / other / free */
  function platformState(platform) {
    var bound = String((platform && platform.bound_soul_id) || "");
    var current = soulId() || String(pageData().current_soul_id || "");
    if (bound && current && bound === current) return "mine";
    if (bound) return "other";
    return "free";
  }

  function boundPlatformCount() {
    return platforms().filter(function (platform) {
      return platformState(platform) === "mine";
    }).length;
  }

  function kindHtml(item) {
    if (item && item.is_l3_private) return '<span class="tag tag-primary">★L3 私聊</span>';
    var kind = String((item && item.kind) || "").toLowerCase();
    if (kind === "group") return '<span class="tag">群聊</span>';
    if (kind === "private") return '<span class="tag">私聊</span>';
    var umo = String((item && item.umo) || "").toLowerCase();
    if (umo.indexOf("group") >= 0) return '<span class="tag">群聊</span>';
    if (umo.indexOf("private") >= 0 || umo.indexOf("friend") >= 0) {
      return '<span class="tag">私聊</span>';
    }
    return '<span class="tag tag-muted">未知</span>';
  }

  function runtimeHtml(platform) {
    var enabled = platform.enabled !== false;
    var running = !!platform.running;
    var cls = "";
    var text = "";
    if (!enabled) {
      cls = "light-off";
      text = "未启用";
    } else if (running) {
      cls = "light-ok";
      text = "运行中";
    } else if (platform.enabled === true) {
      cls = "light-warn";
      text = "未运行";
    } else {
      cls = "light-warn";
      text = "已探测";
    }
    return (
      '<span class="row" style="gap:6px"><span class="status-light ' +
      cls +
      '"></span><span class="text-xs">' +
      esc(text) +
      "</span></span>"
    );
  }

  /** 清理统计 → 中文摘要（后端 clear_platform_memory_state 返回 {windows, evicted, unread, messages}） */
  function clearedText(cleared) {
    if (cleared == null) return "";
    if (typeof cleared === "number") return "清理了 " + cleared + " 处内存";
    if (typeof cleared !== "object") return "";
    var parts = [];
    if (cleared.windows != null) parts.push("滑窗 " + num(cleared.windows, 0) + " 个");
    if (cleared.messages != null) parts.push("消息 " + num(cleared.messages, 0) + " 条");
    if (cleared.unread != null) parts.push("未读 " + num(cleared.unread, 0) + " 条");
    if (cleared.evicted != null) parts.push("驱逐 " + num(cleared.evicted, 0) + " 条");
    if (cleared.sessions != null) parts.push("会话 " + num(cleared.sessions, 0) + " 个");
    return parts.join(" · ");
  }

  function toastCleared(app, prefix, cleared) {
    if (!app) return;
    var text = clearedText(cleared);
    app.toast(prefix + (text ? "：" + text : ""));
  }

  /* ------------------------------------------------------------------ *
   * HTML —— 区块 A：机器人接口列表
   * ------------------------------------------------------------------ */
  function platformActionHtml(platform) {
    var state = platformState(platform);
    var pid = esc(platform.platform_id);
    if (state === "mine") {
      return (
        '<button class="btn btn-outline btn-sm" type="button" data-action="unbind-platform" data-platform-id="' +
        pid +
        '" data-tip="解绑只清空该平台的内存滑窗 + 未读队列；保留 SQLite memories / users / surfing_history / 群档案（重启后未读队列会重置）">' +
        icon("link", 13) +
        "解绑（清理该平台内存滑窗）</button>"
      );
    }
    if (state === "other") {
      return (
        '<button class="btn btn-outline btn-sm" type="button" disabled data-tip="一个机器人接口同一时刻只能绑定一个角色，不可跨角色共用">' +
        icon("link", 13) +
        "绑定到当前角色</button>"
      );
    }
    return (
      '<button class="btn btn-primary btn-sm" type="button" data-action="bind-platform" data-platform-id="' +
      pid +
      '">' +
      icon("link", 13) +
      "绑定到当前角色</button>"
    );
  }

  function platformStateHtml(platform) {
    var state = platformState(platform);
    if (state === "mine") {
      return '<span class="tag tag-primary">已绑定当前角色</span>';
    }
    if (state === "other") {
      var otherName =
        String(platform.bound_soul_name || "") || soulNameOf(platform.bound_soul_id);
      return (
        '<span class="tag tag-muted" data-tip="一个机器人接口同一时刻只能绑定一个角色，不可跨角色共用">已被角色「' +
        esc(otherName) +
        "」占用</span>"
      );
    }
    return '<span class="tag tag-muted">未绑定</span>';
  }

  function platformsCardHtml() {
    var list = platforms();
    var current = soulId();
    var body = "";
    if (!current) {
      body =
        '<div class="empty">' +
        icon("character", 30) +
        '<div class="empty-title">还没有当前角色</div>' +
        '<div class="empty-text">请先在「角色卡」页创建角色，再管理机器人接口绑定。</div></div>';
    } else if (!list.length) {
      body =
        '<div class="empty">' +
        icon("sessions", 30) +
        '<div class="empty-title">尚未探测到机器人接口</div>' +
        '<div class="empty-text">当 AstrBot 启用了平台适配器或已收到过消息后，接口会自动出现在这里；也可以在下方的「手动绑定」中直接输入 platform_id 预配。</div></div>';
    } else {
      var trs = list
        .map(function (platform) {
          var boundName = String(platform.bound_soul_name || "") || soulNameOf(platform.bound_soul_id);
          return (
            '<tr data-platform-id="' +
            esc(platform.platform_id) +
            '">' +
            '<td><div class="umo-cell"><span class="umo-name">' +
            esc(platform.platform_id) +
            '</span><span class="umo-meta">' +
            esc((platform.name || platform.platform_type || "") +
              (boundName ? " · 所属角色 " + boundName : "")) +
            "</span></div></td>" +
            "<td>" +
            esc(platform.platform_type || "—") +
            "</td>" +
            "<td>" +
            runtimeHtml(platform) +
            "</td>" +
            "<td>" +
            esc(num(platform.session_count, 0)) +
            " 个</td>" +
            "<td>" +
            platformStateHtml(platform) +
            '</td><td class="cell-actions">' +
            platformActionHtml(platform) +
            "</td></tr>"
          );
        })
        .join("");
      body =
        '<div class="table-wrap" style="border:0;border-radius:0"><table class="table"><thead><tr>' +
        "<th>机器人接口</th><th>类型</th><th>运行状态</th><th>活跃会话</th><th>绑定状态</th><th style=\"text-align:right\">操作</th>" +
        "</tr></thead><tbody>" +
        trs +
        "</tbody></table></div>";
    }
    return (
      '<section class="card" data-role="platform-card"><div class="card-head">' +
      '<div class="card-title">' +
      icon("sessions", 15) +
      "机器人接口<span class=\"badge-count\">" +
      esc(boundPlatformCount()) +
      '</span><span class="card-sub">一个接口同一时刻只属于一个角色</span></div>' +
      '<div class="card-actions"><button class="btn btn-ghost btn-sm" type="button" data-action="refresh">' +
      icon("refresh", 13) +
      "刷新</button></div></div>" +
      body +
      '<div class="card-body tight" style="border-top:1px solid var(--border)"><div class="field-help">绑定后该机器人接口收到的消息由当前角色接管；已被其他角色占用的接口不可跨角色共用，请先到对应角色下解绑。</div></div>' +
      "</section>"
    );
  }

  /* ------------------------------------------------------------------ *
   * HTML —— 区块 B：手动绑定
   * ------------------------------------------------------------------ */
  function manualCardHtml() {
    var current = soulId();
    return (
      '<section class="card"><div class="card-head"><div class="card-title">' +
      icon("link", 15) +
      '手动绑定<span class="card-sub">离线预配 / 接口尚未被探测到</span></div></div>' +
      '<div class="card-body"><div class="field"><span class="field-label">platform_id</span>' +
      '<div class="row"><input class="input inline-input grow" type="text" data-role="manual-platform" value="' +
      esc(S.manualPlatform) +
      '" placeholder="例如 aiocqhttp / telegram_bot_1" />' +
      '<button class="btn btn-primary btn-sm" type="button" data-action="bind-manual"' +
      (current ? "" : ' disabled data-tip="请先选择当前角色"') +
      ">" +
      icon("link", 13) +
      "绑定到当前角色</button></div>" +
      '<div class="field-help">platform_id 取 UMO 的第一段：如 <span class="mono">aiocqhttp:GroupMessage:123456</span> 的平台 ID 是 <span class="mono">aiocqhttp</span>。不能包含冒号，长度 ≤64。</div></div></div></section>'
    );
  }

  /* ------------------------------------------------------------------ *
   * HTML —— 区块 C：默认兜底角色
   * ------------------------------------------------------------------ */
  function defaultCardHtml() {
    var current = soulId();
    var fallback =
      String(pageData().default_soul_id || "") ||
      String((S.app && S.app.state && S.app.state.defaultSoulId) || "");
    var isDefault = !!fallback && !!current && fallback === current;
    var fallbackLabel = fallback
      ? soulNameOf(fallback) + "（" + fallback + "）"
      : "未显式设置（兜底使用最近更新的角色）";
    return (
      '<section class="card"><div class="card-head"><div class="card-title">' +
      icon("character", 15) +
      '默认兜底角色<span class="card-sub">未绑定接口的会话由它接管</span></div>' +
      '<span class="tag ' +
      (isDefault ? "tag-primary" : "tag-muted") +
      '">' +
      (isDefault ? "当前角色已是兜底" : fallback ? "已设置" : "自动") +
      "</span></div>" +
      '<div class="card-body"><div class="key-value">' +
      "<dt>当前默认</dt><dd>" +
      esc(fallbackLabel) +
      "</dd>" +
      "<dt>当前角色</dt><dd>" +
      esc(current ? currentSoulLabel() + "（" + current + "）" : "—") +
      "</dd></div>" +
      '<div class="row mt-12"><button class="btn btn-outline btn-sm" type="button" data-action="set-default"' +
      (isDefault || !current ? " disabled" : "") +
      ">" +
      icon("check", 13) +
      "将当前角色设为默认兜底</button></div>" +
      '<div class="field-help mt-8">路由优先级：平台绑定 → 旧 UMO 兼容绑定 → 默认角色 → 最近更新的角色。修改默认角色只影响未被任何角色绑定的接口与新会话的兜底路由。</div></div></section>'
    );
  }

  /* ------------------------------------------------------------------ *
   * HTML —— 区块 D：活跃会话内存概览
   * ------------------------------------------------------------------ */
  function sessionRowHtml(item) {
    var umo = String((item && item.umo) || "");
    var platformId = String((item && item.platform_id) || "");
    return (
      '<tr data-umo="' +
      esc(umo) +
      '"><td><div class="umo-cell"><span class="umo-name">' +
      esc(umo) +
      '</span><span class="umo-meta">' +
      esc(platformId ? "平台 " + platformId : "") +
      "</span></div></td>" +
      "<td>" +
      kindHtml(item || {}) +
      "</td>" +
      "<td>" +
      esc(num((item && item.window_messages), 0)) +
      " 条</td>" +
      "<td>" +
      esc(fmtAgo(item && item.last_active_at)) +
      '</td><td class="cell-actions">' +
      '<button class="btn btn-ghost btn-sm" type="button" data-action="clear-session" data-umo="' +
      esc(umo) +
      '" data-tip="清空该会话的内存滑窗 + 未读队列；保留 SQLite memories / users / surfing_history / 群档案（重启后未读队列会重置）">清空</button></td></tr>'
    );
  }

  function sessionGroupsHtml() {
    var list = sessions();
    if (!list.length) {
      return (
        '<div class="empty">' +
        icon("sessions", 30) +
        '<div class="empty-title">当前角色暂无活跃会话内存</div>' +
        '<div class="empty-text">在已绑定的机器人接口上发条消息，Bot 会记录会话滑窗，随后可在这里查看与清空。</div></div>'
      );
    }
    var order = [];
    var byPlatform = {};
    list.forEach(function (item) {
      var platformId = String((item && item.platform_id) || "") || "未标注平台";
      if (!byPlatform[platformId]) {
        byPlatform[platformId] = [];
        order.push(platformId);
      }
      byPlatform[platformId].push(item);
    });
    var html = order
      .map(function (platformId) {
        var items = byPlatform[platformId];
        var groupCount = items.filter(function (item) {
          return String((item && item.kind) || "").toLowerCase() === "group";
        }).length;
        var privateCount = items.length - groupCount;
        var tableRows = items.map(sessionRowHtml).join("");
        return (
          '<div class="row-between wrap mt-12" data-platform-group="' +
          esc(platformId) +
          '"><div class="row wrap" style="gap:8px">' +
          '<span class="mono text-sm">' +
          esc(platformId) +
          "</span>" +
          '<span class="tag">群聊 ' +
          esc(groupCount) +
          " · 私聊 " +
          esc(privateCount) +
          "</span></div>" +
          '<button class="btn btn-ghost btn-sm" type="button" data-action="clear-platform" data-platform-id="' +
          esc(platformId === "未标注平台" ? "" : platformId) +
          '" data-tip="清空该平台下所有会话的内存滑窗 + 未读队列；保留 SQLite memories / users / surfing_history / 群档案（重启后未读队列会重置）">' +
          icon("trash", 13) +
          "清空全部</button></div>" +
          '<div class="table-wrap mt-4"><table class="table"><thead><tr>' +
          "<th>会话 UMO</th><th>类型</th><th>滑窗</th><th>最近活跃</th><th style=\"text-align:right\">操作</th>" +
          "</tr></thead><tbody>" +
          tableRows +
          "</tbody></table></div>"
        );
      })
      .join("");
    return html;
  }

  function sessionsCardHtml() {
    var list = sessions();
    return (
      '<section class="card" data-role="sessions-card"><div class="card-head">' +
      '<div class="card-title">' +
      icon("activity", 15) +
      '活跃会话内存<span class="badge-count">' +
      esc(list.length) +
      '</span><span class="card-sub">当前角色管辖的会话滑窗</span></div>' +
      '<div class="card-actions"><span class="text-xs muted" data-tip="清空：该平台/会话的内存滑窗 + 未读队列；保留：SQLite memories / users / surfing_history / 群档案（重启后未读队列会重置）">仅清内存，不删记忆</span></div></div>' +
      '<div class="card-body tight">' +
      sessionGroupsHtml() +
      "</div></section>"
    );
  }

  /* ------------------------------------------------------------------ *
   * 页面 HTML
   * ------------------------------------------------------------------ */
  function pageHeadHtml() {
    var current = soulId();
    return (
      '<div class="page-head"><h1 class="page-title">' +
      icon("sessions", 18) +
      "机器人接口" +
      '<span class="card-sub">当前角色：' +
      esc(current ? currentSoulLabel() + "（" + current + "）" : "—") +
      " · 接口绑定 + 活跃会话内存</span></h1>" +
      '<div class="page-actions">' +
      '<button class="btn btn-ghost btn-sm" type="button" data-action="refresh">' +
      icon("refresh", 14) +
      "重新加载</button></div></div>" +
      '<div class="grid-2">' +
      manualCardHtml() +
      defaultCardHtml() +
      "</div>"
    );
  }

  function pageHtml() {
    return (
      pageHeadHtml() +
      platformsCardHtml() +
      '<div class="mt-12"></div>' +
      sessionsCardHtml()
    );
  }

  function paint() {
    if (!S.container || S.destroyed) return;
    var scroller = S.container.closest ? S.container.closest(".main") : null;
    var top = scroller ? scroller.scrollTop : 0;
    S.container.innerHTML = pageHtml();
    if (scroller) scroller.scrollTop = top;
  }

  function paintError(err) {
    if (!S.container || S.destroyed) return;
    S.container.innerHTML =
      '<div class="page-head"><h1 class="page-title">' +
      icon("sessions", 18) +
      "机器人接口</h1></div>" +
      '<div class="card"><div class="empty">' +
      icon("alert", 32) +
      '<div class="empty-title">机器人接口数据加载失败</div>' +
      '<div class="empty-text">' +
      esc((err && (err.message || err)) || "未知错误") +
      "</div>" +
      '<div class="row mt-12"><button class="btn btn-outline" type="button" data-action="refresh">重试</button></div>' +
      "</div></div>";
  }

  /* ------------------------------------------------------------------ *
   * 数据加载
   * ------------------------------------------------------------------ */
  async function fetchData(app) {
    var sid = soulId();
    var params = sid ? { soul_id: sid } : {};
    var data = await app.apiGet("sessions/platforms", params);
    if (!data || typeof data !== "object") throw new Error("GET sessions/platforms 返回为空");
    S.data = data;
    if (data.default_soul_id != null) app.state.defaultSoulId = data.default_soul_id;
    /* 把响应里的 bound_platforms 合入外壳角色列表（不替换掉完整角色档案） */
    if (Array.isArray(data.souls) && Array.isArray(app.state.souls)) {
      data.souls.forEach(function (item) {
        if (!item || !item.soul_id) return;
        for (var i = 0; i < app.state.souls.length; i++) {
          if (app.state.souls[i] && app.state.souls[i].soul_id === item.soul_id) {
            app.state.souls[i].bound_platforms = item.bound_platforms || [];
            break;
          }
        }
      });
    }
    return data;
  }

  async function reload(showToast) {
    var app = S.app;
    if (!app || S.busy) return;
    S.busy = true;
    try {
      await fetchData(app);
      paint();
      if (showToast) app.toast("已刷新机器人接口与会话");
    } catch (err) {
      app.toast("刷新失败：" + (err.message || err), true);
    } finally {
      S.busy = false;
    }
  }

  /* ------------------------------------------------------------------ *
   * 操作：绑定 / 解绑 / 默认角色 / 清空内存
   * ------------------------------------------------------------------ */
  async function bindPlatform(platformId) {
    var app = S.app;
    if (!app || S.busy) return;
    var pid = String(platformId == null ? "" : platformId).trim();
    var sid = soulId();
    if (!sid) {
      app.toast("没有当前角色，无法绑定机器人接口", true);
      return;
    }
    if (!pid) {
      app.toast("请先填写 platform_id", true);
      return;
    }
    S.busy = true;
    try {
      await app.apiPost("sessions/bind_platform", { soul_id: sid, platform_id: pid });
      S.manualPlatform = "";
      await fetchData(app);
      paint();
      app.toast("已将机器人接口「" + pid + "」绑定到当前角色");
    } catch (err) {
      /* 互斥冲突时后端 message 精确为「该机器人接口已被角色『X』绑定，请先在角色 X 下解绑」 */
      app.toast((err && (err.message || err)) || "绑定失败", true);
    } finally {
      S.busy = false;
    }
  }

  function confirmUnbind(platform) {
    var app = S.app;
    if (!app || !platform) return;
    var pid = String(platform.platform_id || "");
    app
      .confirm({
        title: "解绑机器人接口",
        message:
          "确定解绑「" +
          pid +
          "」吗？解绑后该平台的会话将改由默认角色兜底接管。" +
          "清空：该平台所有 UMO 的内存滑窗 + 未读队列，以及该平台的 ★L3 私聊目标 / 主动消息冷却缓存。" +
          "保留：SQLite memories / users / surfing_history / 群档案；重启后未读队列本身会重置。",
        danger: true,
        confirmLabel: "解绑并清理",
        onConfirm: async function () {
          var data = await app.apiPost("sessions/unbind_platform", {
            soul_id: soulId(),
            platform_id: pid
          });
          var prev = data && data.previous_soul_id ? "（原绑定：" + data.previous_soul_id + "）" : "";
          toastCleared(app, "已解绑「" + pid + "」" + prev, data && data.cleared);
          await reload(false);
        }
      })
      .then(function () {
        /* 结果已通过 toast 提示 */
      });
  }

  async function setDefault() {
    var app = S.app;
    var sid = soulId();
    if (!app || !sid) return;
    try {
      var data = await app.apiPost("sessions/default_soul", { soul_id: sid });
      var next = data && data.default_soul_id != null ? data.default_soul_id : sid;
      app.state.defaultSoulId = next;
      if (S.data) S.data.default_soul_id = next;
      paint();
      app.toast("已将当前角色设为默认兜底角色");
    } catch (err) {
      app.toast("设置默认角色失败：" + (err.message || err), true);
    }
  }

  function confirmClearSession(item) {
    var app = S.app;
    if (!app || !item) return;
    var umo = String(item.umo || "");
    app
      .confirm({
        title: "清空会话内存",
        message:
          "确定清空会话「" +
          umo +
          "」吗？清空：该会话的内存滑窗 + 未读队列。" +
          "保留：SQLite memories / users / surfing_history / 群档案；重启后未读队列本身会重置。",
        danger: true,
        confirmLabel: "清空",
        onConfirm: async function () {
          var data = await app.apiPost("sessions/clear_memory", {
            soul_id: soulId(),
            umo: umo
          });
          toastCleared(app, "已清空会话内存", data && data.cleared);
          await reload(false);
        }
      })
      .then(function () {
        /* 结果已通过 toast 提示 */
      });
  }

  function confirmClearPlatform(platformId) {
    var app = S.app;
    if (!app) return;
    var pid = String(platformId || "");
    if (!pid) {
      app.toast("缺少 platform_id，无法清空平台内存", true);
      return;
    }
    app
      .confirm({
        title: "清空整个平台的会话内存",
        message:
          "确定清空「" +
          pid +
          "」平台下所有会话吗？清空：该平台的内存滑窗 + 未读队列，以及该平台的 ★L3 私聊目标 / 主动消息冷却缓存。" +
          "保留：SQLite memories / users / surfing_history / 群档案；重启后未读队列本身会重置。",
        danger: true,
        confirmLabel: "清空全部",
        onConfirm: async function () {
          var data = await app.apiPost("sessions/clear_memory", {
            soul_id: soulId(),
            platform_id: pid
          });
          toastCleared(app, "已清空平台「" + pid + "」会话内存", data && data.cleared);
          await reload(false);
        }
      })
      .then(function () {
        /* 结果已通过 toast 提示 */
      });
  }

  /* ------------------------------------------------------------------ *
   * 事件
   * ------------------------------------------------------------------ */
  function findPlatform(platformId) {
    var list = platforms();
    for (var i = 0; i < list.length; i++) {
      if (list[i] && String(list[i].platform_id) === String(platformId)) return list[i];
    }
    return null;
  }

  function findSession(umo) {
    var list = sessions();
    for (var i = 0; i < list.length; i++) {
      if (list[i] && String(list[i].umo) === String(umo)) return list[i];
    }
    return null;
  }

  function handleClick(event) {
    var target = event.target;
    if (!target || !target.closest) return;
    var actionEl = target.closest("[data-action]");
    if (!actionEl) return;
    var app = S.app;
    if (!app) return;
    var action = actionEl.getAttribute("data-action");
    if (action === "refresh") {
      reload(true);
    } else if (action === "bind-platform") {
      bindPlatform(actionEl.getAttribute("data-platform-id"));
    } else if (action === "unbind-platform") {
      confirmUnbind(findPlatform(actionEl.getAttribute("data-platform-id")));
    } else if (action === "bind-manual") {
      var input = S.container
        ? S.container.querySelector('[data-role="manual-platform"]')
        : null;
      bindPlatform(input ? input.value : S.manualPlatform);
    } else if (action === "set-default") {
      setDefault();
    } else if (action === "clear-session") {
      confirmClearSession(findSession(actionEl.getAttribute("data-umo")));
    } else if (action === "clear-platform") {
      confirmClearPlatform(actionEl.getAttribute("data-platform-id"));
    }
  }

  function handleInput(event) {
    var target = event.target;
    if (!target || !target.getAttribute) return;
    if (target.getAttribute("data-role") === "manual-platform") {
      S.manualPlatform = String(target.value || "");
    }
  }

  function bindEvents(container, app) {
    container.addEventListener("click", handleClick);
    container.addEventListener("input", handleInput);
    container.addEventListener("keydown", function (event) {
      var target = event.target;
      if (!target || !target.getAttribute) return;
      if (target.getAttribute("data-role") !== "manual-platform") return;
      if (event.key !== "Enter") return;
      event.preventDefault();
      bindPlatform(target.value);
    });
  }

  /* ------------------------------------------------------------------ *
   * 生命周期
   * ------------------------------------------------------------------ */
  async function render(container, app) {
    S.app = app;
    S.container = container;
    S.destroyed = false;
    S.data = null;
    S.manualPlatform = "";
    S.busy = false;

    container.innerHTML =
      '<div class="loading"><span class="spinner"></span><span>加载机器人接口与会话…</span></div>';
    bindEvents(container, app);
    try {
      await fetchData(app);
      if (S.destroyed) return;
      paint();
    } catch (err) {
      console.error("[OnlyYou] 机器人接口页渲染失败：", err);
      if (!S.destroyed) paintError(err);
    }
  }

  function destroy() {
    S.destroyed = true;
    S.app = null;
    S.container = null;
    S.data = null;
    S.manualPlatform = "";
    S.busy = false;
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
