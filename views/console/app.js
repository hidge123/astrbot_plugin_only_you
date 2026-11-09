/**
 * OnlyYou (只有你) — AstrBot Plugin Pages 控制台外壳 (`views/console/app.js`)
 *
 * 职责（其余页面逻辑在 page-*.js）：
 *  1. AstrBot Dashboard bridge 封装：apiGet / apiPost / ready / getContext / onContext，
 *     无 bridge 时自动 fetch `/api/plug/astrbot_plugin_only_you/{endpoint}` 兜底。
 *  2. 轻量状态管理：state = { souls, currentSoulId, currentSoul, overview, modelStatus,
 *     actionTypes, defaultSoulId, nativePersonas, umoBindings, knownUmos, activeSchedule }。
 *  3. hash 路由（#/overview / #/schedule / ...），从 window.OY_PAGES 自动生成 216px 侧栏。
 *  4. 通用 UI：toast / modal / drawer / confirm；主题初始化与 AstrBot Dashboard 明暗同步。
 *  5. 页面生命周期：render(container, ctx) / destroy()；ctx 就是 app 本身。
 *
 * 无构建 classic script，禁止 import/export。全局暴露 window.app。
 */
(function () {
  "use strict";

  var API_PREFIX = "/astrbot_plugin_only_you";
  var THEME_KEY = "onlyyou.console.theme";
  var SOUL_KEY = "onlyyou.console.soulId";

  /* 侧栏分组顺序（计划 §⑤ 布局）：组内页面未注册时显示 pending 占位。 */
  var NAV_CONFIG = [
    { label: "运行", pages: ["overview", "schedule"] },
    { label: "角色", pages: ["character"] },
    { pages: ["relations"] },
    { pages: ["memory"] },
    { pages: ["surfing"] },
    { label: "系统", pages: ["models", "sessions"] }
  ];

  /* 预期页面元数据（page-*.js 缺失时的占位信息 + 侧栏图标/标题兜底）。 */
  var PAGE_META = {
    overview: { title: "概览", icon: "gauge", file: "page-overview.js" },
    schedule: { title: "日程", icon: "calendar", file: "page-schedule.js" },
    character: { title: "角色卡", icon: "character", file: "page-character.js" },
    relations: { title: "关系", icon: "relations", file: "page-relations.js" },
    memory: { title: "记忆", icon: "memory", file: "page-memory.js" },
    surfing: { title: "冲浪", icon: "surf", file: "page-surfing.js" },
    models: { title: "模型", icon: "models", file: "page-models.js" },
    sessions: { title: "会话", icon: "sessions", file: "page-sessions.js" }
  };

  function esc(str) {
    return String(str == null ? "" : str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function icon(name, size) {
    if (typeof window.icon === "function") return window.icon(name, size || 16);
    return "";
  }

  /**
   * 解开 AstrBot Web API 的 `{status, message, data}` 信封（兼容双层包裹）。
   * status=error 时抛 Error，message 取后端 message。
   */
  function unwrapApiPayload(response) {
    var wrapped = null;
    if (response && typeof response === "object") {
      if (
        (response.status === "ok" || response.status === "error") &&
        response.data !== undefined
      ) {
        wrapped = response;
      } else if (
        response.data &&
        typeof response.data === "object" &&
        (response.data.status === "ok" || response.data.status === "error") &&
        response.data.data !== undefined
      ) {
        wrapped = response.data;
      }
    }
    if (wrapped) {
      if (wrapped.status === "error") {
        var err = new Error(wrapped.message || wrapped.reason || "请求处理失败");
        err.payload = wrapped;
        throw err;
      }
      return wrapped.data;
    }
    return response;
  }

  function cloneDeep(obj) {
    try {
      return JSON.parse(JSON.stringify(obj));
    } catch (err) {
      return obj;
    }
  }

  /* ====================================================================== *
   * OnlyYouConsoleApp
   * ====================================================================== */
  function OnlyYouConsoleApp() {
    this.state = {
      souls: [],
      currentSoulId: "",
      currentSoul: null,
      overview: null,
      modelStatus: {},
      actionTypes: [],
      defaultSoulId: "",
      nativePersonas: [],
      umoBindings: {},
      knownUmos: [],
      activeSchedule: null,
      cardSchema: null
    };
    this.theme = "light";
    this.route = "overview";
    this._page = null;
    this._renderSeq = 0;
    this._started = false;
    this._modalState = null;
    this._drawerState = null;
    this._bound = false;
  }

  /* ------------------------------ 基础工具 ------------------------------ */

  OnlyYouConsoleApp.prototype.esc = esc;

  OnlyYouConsoleApp.prototype.icon = icon;

  OnlyYouConsoleApp.prototype.charts = function () {
    return window.OYCharts || {};
  };

  OnlyYouConsoleApp.prototype.pages = function () {
    return window.OY_PAGES || {};
  };

  OnlyYouConsoleApp.prototype.pageMeta = function (routeId) {
    var page = this.pages()[routeId];
    if (page && page.title) {
      return { title: page.title, icon: page.icon || "grid", file: "page-" + routeId + ".js" };
    }
    return PAGE_META[routeId] || { title: routeId, icon: "grid", file: "page-" + routeId + ".js" };
  };

  /** 三态元信息：{ id, label, cls, color } */
  OnlyYouConsoleApp.prototype.stateMeta = function (state) {
    var id = String(state || "IDLE").toUpperCase();
    if (id !== "IDLE" && id !== "BUSY" && id !== "RESTING") id = "IDLE";
    var labels = { IDLE: "空闲", BUSY: "繁忙", RESTING: "休息" };
    return {
      id: id,
      label: labels[id],
      cls: "st-" + id.toLowerCase(),
      color: "var(--state-" + id.toLowerCase() + ")"
    };
  };

  /** 秒 → 中文时长，如 5025 → "1小时23分" */
  OnlyYouConsoleApp.prototype.formatDuration = function (seconds) {
    var total = Math.max(0, Math.round(Number(seconds) || 0));
    if (total >= 3600) {
      var hours = Math.floor(total / 3600);
      var mins = Math.floor((total % 3600) / 60);
      return hours + "小时" + (mins ? mins + "分" : "");
    }
    if (total >= 60) return Math.floor(total / 60) + "分";
    return total + "秒";
  };

  /** 秒级时间戳 / 毫秒时间戳 → "HH:MM" */
  OnlyYouConsoleApp.prototype.formatClock = function (ts) {
    var ms = Number(ts) || 0;
    if (ms > 0 && ms < 100000000000) ms = ms * 1000;
    var d = new Date(ms || Date.now());
    var hh = d.getHours();
    var mm = d.getMinutes();
    return (hh < 10 ? "0" + hh : String(hh)) + ":" + (mm < 10 ? "0" + mm : String(mm));
  };

  /** 当前小时（含分钟小数），24h 日程轴“现在”指针用 */
  OnlyYouConsoleApp.prototype.nowHour = function () {
    var d = new Date();
    return d.getHours() + d.getMinutes() / 60;
  };

  OnlyYouConsoleApp.prototype.scheduleSlots = function () {
    var schedule = this.state.activeSchedule;
    if (schedule && schedule.run && Array.isArray(schedule.run.slots)) return schedule.run.slots;
    return [];
  };

  OnlyYouConsoleApp.prototype.currentSlotKey = function () {
    var overview = this.state.overview;
    if (overview && overview.state && overview.state.slot_key) return overview.state.slot_key;
    var schedule = this.state.activeSchedule;
    if (schedule && schedule.current_slot && schedule.current_slot.slot_key) {
      return schedule.current_slot.slot_key;
    }
    return "";
  };

  /* ------------------------------ Bridge 封装 ------------------------------ */

  OnlyYouConsoleApp.prototype._bridge = function () {
    return window.AstrBotPluginPage || window.AstrBotPluginView || null;
  };

  OnlyYouConsoleApp.prototype.apiGet = async function (endpoint, params) {
    params = params || {};
    var bridge = this._bridge();
    if (bridge && typeof bridge.apiGet === "function") {
      var res;
      try {
        res = await bridge.apiGet(endpoint, params);
      } catch (err) {
        throw err instanceof Error ? err : new Error(String(err));
      }
      return unwrapApiPayload(res);
    }
    var qs = new URLSearchParams();
    Object.keys(params).forEach(function (key) {
      var value = params[key];
      if (value !== undefined && value !== null && value !== "") qs.set(key, value);
    });
    var url = "/api/plug" + API_PREFIX + "/" + endpoint + (qs.toString() ? "?" + qs.toString() : "");
    var response;
    try {
      response = await fetch(url, {
        credentials: "include",
        headers: { Accept: "application/json" }
      });
    } catch (err) {
      throw new Error("无法连接 OnlyYou 后端：" + (err.message || err));
    }
    if (!response.ok) throw new Error("请求失败 (HTTP " + response.status + ")");
    var json = await response.json();
    return unwrapApiPayload(json);
  };

  OnlyYouConsoleApp.prototype.apiPost = async function (endpoint, body) {
    body = body || {};
    var bridge = this._bridge();
    if (bridge && typeof bridge.apiPost === "function") {
      var res;
      try {
        res = await bridge.apiPost(endpoint, body);
      } catch (err) {
        throw err instanceof Error ? err : new Error(String(err));
      }
      return unwrapApiPayload(res);
    }
    var url = "/api/plug" + API_PREFIX + "/" + endpoint;
    var response;
    try {
      response = await fetch(url, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify(body)
      });
    } catch (err) {
      throw new Error("无法连接 OnlyYou 后端：" + (err.message || err));
    }
    if (!response.ok) throw new Error("请求失败 (HTTP " + response.status + ")");
    var json = await response.json();
    return unwrapApiPayload(json);
  };

  /* ------------------------------ 主题 ------------------------------ */

  OnlyYouConsoleApp.prototype._initTheme = function () {
    var stored = null;
    try {
      stored = localStorage.getItem(THEME_KEY);
    } catch (err) {
      stored = null;
    }
    var prefersDark = false;
    try {
      prefersDark = !!(window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches);
    } catch (err) {
      prefersDark = false;
    }
    this.applyTheme(stored ? stored === "dark" : prefersDark);
  };

  OnlyYouConsoleApp.prototype.applyTheme = function (isDark) {
    this.theme = isDark ? "dark" : "light";
    var root = document.documentElement;
    root.classList.toggle("dark", !!isDark);
    root.setAttribute("data-theme", this.theme);
    try {
      localStorage.setItem(THEME_KEY, this.theme);
    } catch (err) {
      /* ignore */
    }
    this._renderThemeButton();
    try {
      window.dispatchEvent(new CustomEvent("oy:theme", { detail: { theme: this.theme } }));
    } catch (err) {
      /* ignore */
    }
  };

  OnlyYouConsoleApp.prototype.toggleTheme = function () {
    this.applyTheme(this.theme !== "dark");
  };

  OnlyYouConsoleApp.prototype._renderThemeButton = function () {
    var btn = document.getElementById("btn-theme");
    if (!btn) return;
    var dark = this.theme === "dark";
    btn.innerHTML = icon(dark ? "sun" : "moon", 16);
    btn.setAttribute("data-tip", dark ? "切换为浅色主题" : "切换为深色主题");
    btn.setAttribute("aria-label", dark ? "切换为浅色主题" : "切换为深色主题");
  };

  OnlyYouConsoleApp.prototype._syncBridgeTheme = async function () {
    var bridge = this._bridge();
    if (!bridge) return;
    var self = this;
    try {
      if (typeof bridge.ready === "function") {
        var ctx = await Promise.race([
          Promise.resolve().then(function () {
            return bridge.ready();
          }),
          new Promise(function (resolve) {
            setTimeout(function () {
              resolve(null);
            }, 1500);
          })
        ]);
        if (!ctx && typeof bridge.getContext === "function") {
          try {
            ctx = bridge.getContext();
          } catch (err) {
            ctx = null;
          }
        }
        self._applyContextTheme(ctx);
      }
      if (typeof bridge.onContext === "function") {
        bridge.onContext(function (nextCtx) {
          self._applyContextTheme(nextCtx);
        });
      }
    } catch (err) {
      console.warn("[OnlyYou] 同步 AstrBot 主题失败：", err);
    }
  };

  OnlyYouConsoleApp.prototype._applyContextTheme = function (ctx) {
    if (!ctx || typeof ctx !== "object") return;
    if (typeof ctx.isDark === "boolean") {
      this.applyTheme(ctx.isDark);
    } else if (ctx.theme === "dark" || ctx.theme === "light") {
      this.applyTheme(ctx.theme === "dark");
    }
  };

  /* ------------------------------ 初始化 / 数据加载 ------------------------------ */

  OnlyYouConsoleApp.prototype.init = async function () {
    if (this._started) return;
    this._started = true;
    this._bindStaticEvents();
    this._initTheme();
    this._renderThemeButton();
    this._renderTopbarIcons();
    await this._syncBridgeTheme();
    try {
      await this._loadShellData();
    } catch (err) {
      console.error("[OnlyYou] 初始化失败：", err);
      this._renderFatal(err);
      this._renderNav();
      return;
    }
    this.route = this._parseHash();
    /* URL 不带 hash 时补一个，保证刷新后停在当前页面且路由可分享 */
    try {
      if (!window.location.hash) {
        window.history.replaceState(null, "", "#/" + this.route);
      }
    } catch (err) {
      /* ignore */
    }
    this._renderNav();
    this._renderSoulSelect();
    await this._renderCurrentRoute();
  };

  OnlyYouConsoleApp.prototype._loadShellData = async function () {
    var init = await this.apiGet("init_summary");
    if (!init || typeof init !== "object") throw new Error("init_summary 返回数据为空");

    var state = this.state;
    state.souls = Array.isArray(init.souls) ? init.souls : [];
    state.nativePersonas = Array.isArray(init.native_personas) ? init.native_personas : [];
    state.umoBindings = init.umo_bindings || {};
    state.knownUmos = Array.isArray(init.known_umos) ? init.known_umos : [];
    state.modelStatus = init.model_status || {};
    state.actionTypes = Array.isArray(init.action_types) ? init.action_types : [];
    state.defaultSoulId = init.default_soul_id || "";
    state.activeSchedule = init.active_schedule || null;

    var storedSoulId = "";
    try {
      storedSoulId = sessionStorage.getItem(SOUL_KEY) || "";
    } catch (err) {
      storedSoulId = "";
    }
    var wanted =
      state.currentSoulId ||
      storedSoulId ||
      init.active_soul_id ||
      (state.souls[0] && state.souls[0].soul_id) ||
      "";
    var match = null;
    for (var i = 0; i < state.souls.length; i++) {
      if (state.souls[i] && state.souls[i].soul_id === wanted) {
        match = state.souls[i];
        break;
      }
    }
    if (!match && state.souls.length) match = state.souls[0];
    if (match) {
      state.currentSoul = match;
      state.currentSoulId = match.soul_id;
    } else {
      state.currentSoulId = wanted;
    }

    /* 拉取当前角色详情（今日日程 / UMO 绑定 / 角色卡 schema） */
    if (state.currentSoulId) {
      try {
        var detail = await this.apiGet("souls/detail", { soul_id: state.currentSoulId });
        if (detail) {
          if (detail.soul) {
            state.currentSoul = detail.soul;
            state.currentSoulId = detail.soul.soul_id || state.currentSoulId;
          }
          if (detail.schedule) state.activeSchedule = detail.schedule;
          if (detail.umo_bindings) state.umoBindings = detail.umo_bindings;
          if (Array.isArray(detail.known_umos)) state.knownUmos = detail.known_umos;
          if (detail.card_schema) state.cardSchema = detail.card_schema;
        }
      } catch (err) {
        console.warn("[OnlyYou] souls/detail 加载失败（继续使用 init_summary 数据）：", err);
      }
    }
  };

  /** 重新拉取外壳数据并重渲染当前页（顶栏刷新按钮 / 页面保存后调用） */
  OnlyYouConsoleApp.prototype.refresh = async function (opts) {
    opts = opts || {};
    var refreshBtn = document.getElementById("btn-refresh");
    if (refreshBtn) refreshBtn.classList.add("is-loading");
    try {
      await this._loadShellData();
      this._renderNav();
      this._renderSoulSelect();
      await this._renderCurrentRoute();
      if (!opts.silent) this.toast("已刷新");
    } catch (err) {
      console.error("[OnlyYou] 刷新失败：", err);
      this.toast("刷新失败：" + (err.message || err), true);
      if (!this.state.souls.length) this._renderFatal(err);
    } finally {
      if (refreshBtn) refreshBtn.classList.remove("is-loading");
    }
  };

  /** 仅重渲染当前页面（不重新拉 init_summary 外壳数据） */
  OnlyYouConsoleApp.prototype.reloadPage = function () {
    return this._renderCurrentRoute();
  };

  OnlyYouConsoleApp.prototype.fetchOverview = async function (soulId) {
    var data = await this.apiGet("dashboard/overview", {
      soul_id: soulId || this.state.currentSoulId
    });
    this.state.overview = data;
    if (data && data.models) this.state.modelStatus = data.models;
    if (data && data.soul && this.state.currentSoul) {
      var soul = data.soul;
      if (soul.name) this.state.currentSoul.name = soul.name;
      if (soul.avatar) this.state.currentSoul.avatar = soul.avatar;
      if (soul.tagline) this.state.currentSoul.tagline = soul.tagline;
    }
    return data;
  };

  /* ------------------------------ 角色切换 / 数据同步 ------------------------------ */

  OnlyYouConsoleApp.prototype._renderSoulSelect = function () {
    var select = document.getElementById("soul-select");
    if (!select) return;
    var state = this.state;
    var options = state.souls.map(function (soul) {
      var label = (soul.name || soul.soul_id) + " · " + soul.soul_id;
      return (
        '<option value="' +
        esc(soul.soul_id) +
        '"' +
        (soul.soul_id === state.currentSoulId ? " selected" : "") +
        ">" +
        esc(label) +
        "</option>"
      );
    });
    if (!options.length) {
      options = ['<option value="">暂无角色</option>'];
    }
    select.innerHTML = options.join("");
    select.disabled = state.souls.length <= 1;
    select.setAttribute(
      "data-tip",
      state.souls.length > 1 ? "切换当前管理的角色" : "当前只有一个角色"
    );
  };

  OnlyYouConsoleApp.prototype.selectSoul = async function (soulId) {
    if (!soulId || soulId === this.state.currentSoulId) return;
    this.state.currentSoulId = soulId;
    try {
      sessionStorage.setItem(SOUL_KEY, soulId);
    } catch (err) {
      /* ignore */
    }
    try {
      var detail = await this.apiGet("souls/detail", { soul_id: soulId });
      if (detail) {
        if (detail.soul) {
          this.state.currentSoul = detail.soul;
          this.state.currentSoulId = detail.soul.soul_id || soulId;
        } else {
          this.state.currentSoul = this._findSoul(soulId);
        }
        if (detail.schedule) this.state.activeSchedule = detail.schedule;
        if (detail.umo_bindings) this.state.umoBindings = detail.umo_bindings;
        if (Array.isArray(detail.known_umos)) this.state.knownUmos = detail.known_umos;
        if (detail.card_schema) this.state.cardSchema = detail.card_schema;
      }
      this.state.overview = null;
      this._renderSoulSelect();
      this._renderNav();
      await this._renderCurrentRoute();
      var name = this.state.currentSoul ? this.state.currentSoul.name : soulId;
      this.toast("已切换角色：" + name);
    } catch (err) {
      this.toast("切换角色失败：" + (err.message || err), true);
    }
  };

  OnlyYouConsoleApp.prototype._findSoul = function (soulId) {
    for (var i = 0; i < this.state.souls.length; i++) {
      if (this.state.souls[i] && this.state.souls[i].soul_id === soulId) return this.state.souls[i];
    }
    return null;
  };

  /** 页面保存角色后同步外壳状态（page-character.js 用） */
  OnlyYouConsoleApp.prototype.applySouls = function (souls, soul) {
    if (Array.isArray(souls)) {
      this.state.souls = souls;
    }
    if (soul && soul.soul_id) {
      this.state.currentSoul = soul;
      this.state.currentSoulId = soul.soul_id;
      for (var i = 0; i < this.state.souls.length; i++) {
        if (this.state.souls[i] && this.state.souls[i].soul_id === soul.soul_id) {
          this.state.souls[i] = soul;
          break;
        }
      }
    }
    var found = this._findSoul(this.state.currentSoulId);
    if (found) this.state.currentSoul = found;
    this._renderSoulSelect();
    this._renderNav();
  };

  OnlyYouConsoleApp.prototype.applyUmoBindings = function (bindings, knownUmos) {
    if (bindings && typeof bindings === "object") this.state.umoBindings = bindings;
    if (Array.isArray(knownUmos)) this.state.knownUmos = knownUmos;
  };

  OnlyYouConsoleApp.prototype.applyModelStatus = function (modelStatus) {
    if (modelStatus && typeof modelStatus === "object") this.state.modelStatus = modelStatus;
    this._renderNav();
  };

  /* ------------------------------ hash 路由 / 侧栏 ------------------------------ */

  OnlyYouConsoleApp.prototype._parseHash = function () {
    var raw = String(window.location.hash || "").replace(/^#\/?/, "");
    var route = raw.split("?")[0].split("&")[0].trim();
    return route || "overview";
  };

  OnlyYouConsoleApp.prototype.navigate = function (routeId) {
    if (!routeId) return;
    var target = "#/" + routeId;
    if (window.location.hash === target) {
      this.route = routeId;
      this._renderNav();
      this._renderCurrentRoute();
      return;
    }
    this.route = routeId;
    window.location.hash = target;
  };

  OnlyYouConsoleApp.prototype._renderTopbarIcons = function () {
    var refreshBtn = document.getElementById("btn-refresh");
    if (refreshBtn) {
      refreshBtn.innerHTML = icon("refresh", 16);
      refreshBtn.setAttribute("data-tip", "刷新数据");
      refreshBtn.setAttribute("aria-label", "刷新数据");
    }
    this._renderThemeButton();
  };

  OnlyYouConsoleApp.prototype._mainModelUnconfigured = function () {
    var models = this.state.modelStatus || {};
    var main = models.main;
    if (main && typeof main === "object") {
      return main.configured === false || main.mode === "unconfigured";
    }
    return false;
  };

  OnlyYouConsoleApp.prototype._renderNav = function () {
    var nav = document.getElementById("sidebar-nav");
    if (!nav) return;
    var pages = this.pages();
    var self = this;
    var html = "";
    var rendered = {};

    function navItemHtml(id) {
      var meta = self.pageMeta(id);
      var exists = !!pages[id];
      var active = self.route === id ? " active" : "";
      var pending = exists ? "" : " pending";
      var dot = "";
      var tip = "";
      if (id === "models" && self._mainModelUnconfigured()) {
        dot = '<span class="nav-dot" data-tip="主模型未配置"></span>';
      }
      if (!exists) {
        tip = ' data-tip="' + esc("页面模块未加载：" + (meta.file || "")) + '"';
      }
      return (
        '<a class="nav-item' +
        active +
        pending +
        '" href="#/' +
        esc(id) +
        '" data-route="' +
        esc(id) +
        '"' +
        tip +
        ">" +
        '<span class="nav-icon">' +
        icon(meta.icon, 16) +
        "</span>" +
        '<span class="nav-label">' +
        esc(meta.title) +
        "</span>" +
        dot +
        "</a>"
      );
    }

    NAV_CONFIG.forEach(function (group) {
      var items = (group.pages || []).filter(function (id) {
        return !!PAGE_META[id] || !!pages[id];
      });
      if (!items.length) return;
      html += '<div class="nav-group">';
      if (group.label) {
        html += '<div class="nav-group-title">' + esc(group.label) + "</div>";
      }
      items.forEach(function (id) {
        rendered[id] = true;
        html += navItemHtml(id);
      });
      html += "</div>";
    });

    /* 额外注册的页面（例如 Phase2 新页面）自动追加到「更多」组 */
    var extras = Object.keys(pages).filter(function (id) {
      return !PAGE_META[id] && !rendered[id];
    });
    if (extras.length) {
      html += '<div class="nav-group"><div class="nav-group-title">更多</div>';
      extras.forEach(function (id) {
        html += navItemHtml(id);
      });
      html += "</div>";
    }
    nav.innerHTML = html;
  };

  /* ------------------------------ 页面渲染生命周期 ------------------------------ */

  OnlyYouConsoleApp.prototype._renderCurrentRoute = async function () {
    var seq = ++this._renderSeq;
    var view = document.getElementById("view");
    if (!view) return;
    if (this._placeholderTimer) {
      clearTimeout(this._placeholderTimer);
      this._placeholderTimer = null;
    }

    if (this._page && typeof this._page.destroy === "function") {
      try {
        await this._page.destroy();
      } catch (err) {
        console.warn("[OnlyYou] page.destroy 失败：", err);
      }
    }
    this._page = null;

    var route = this.route || this._parseHash();
    this.route = route;
    var page = this.pages()[route];
    this._renderNav();

    if (!page) {
      view.innerHTML = this._placeholderHtml(route);
      this._bindPlaceholder(view, route);
      return;
    }

    var container = document.createElement("div");
    container.className = "page";
    try {
      this._page = page;
      await page.render(container, this);
      if (seq !== this._renderSeq) return;
      view.innerHTML = "";
      view.appendChild(container);
    } catch (err) {
      if (seq !== this._renderSeq) return;
      console.error("[OnlyYou] 页面渲染失败 (" + route + ")：", err);
      view.innerHTML = this._errorHtml(route, err);
      this._bindRetry(view);
      this.toast("页面渲染失败：" + (err.message || err), true);
    }
  };

  OnlyYouConsoleApp.prototype._placeholderHtml = function (route) {
    var meta = PAGE_META[route];
    if (!meta) {
      return (
        '<div class="page"><div class="card"><div class="empty">' +
        icon("alert", 34) +
        '<div class="empty-title">未知页面：' +
        esc(route) +
        "</div>" +
        '<div class="empty-text">hash 路由 #/' +
        esc(route) +
        ' 没有对应的页面模块。可用路由：#/overview、#/schedule、#/sessions。</div>' +
        '<div class="row mt-12"><a class="btn btn-outline" href="#/overview">返回概览</a></div>' +
        "</div></div></div>"
      );
    }
    return (
      '<div class="page"><div class="card"><div class="placeholder-page">' +
      icon(meta.icon, 40) +
      '<div class="placeholder-title">「' +
      esc(meta.title) +
      "」页面模块尚未加载</div>" +
      '<div class="placeholder-text">期望文件 <span class="mono">views/console/' +
      esc(meta.file) +
      "</span> 运行后注册 <span class=\"mono\">window.OY_PAGES[&quot;" +
      esc(route) +
      '&quot;]</span>。若该文件已交付，请检查浏览器 404 / JS 控制台报错后重新加载。</div>' +
      '<div class="row mt-12"><button class="btn btn-outline" type="button" data-action="reload-page">重新加载</button>' +
      '<a class="btn btn-ghost" href="#/overview">返回概览</a></div>' +
      "</div></div></div>"
    );
  };

  OnlyYouConsoleApp.prototype._bindPlaceholder = function (view, route) {
    var self = this;
    view.querySelectorAll('[data-action="reload-page"]').forEach(function (btn) {
      btn.addEventListener("click", function () {
        window.location.reload();
      });
    });
    /* 若页面模块是之后才注册的（例如 Phase2 文件后加载），自动重渲染 */
    if (!this._placeholderTimer && route !== "overview") {
      this._placeholderTimer = setTimeout(function () {
        self._placeholderTimer = null;
        if (self.pages()[route]) self._renderCurrentRoute();
      }, 1200);
    }
  };

  OnlyYouConsoleApp.prototype._errorHtml = function (route, err) {
    return (
      '<div class="page"><div class="card"><div class="empty">' +
      icon("alert", 34) +
      '<div class="empty-title">页面渲染失败</div>' +
      '<div class="empty-text">' +
      esc((err && (err.message || err)) || "未知错误") +
      "</div>" +
      '<div class="row mt-12"><button class="btn btn-outline" type="button" data-action="reload-page">重新渲染</button>' +
      '<button class="btn btn-ghost" type="button" data-action="reload-app">重新加载控制台</button></div>' +
      "</div></div></div>"
    );
  };

  OnlyYouConsoleApp.prototype._bindRetry = function (view) {
    var self = this;
    view.querySelectorAll('[data-action="reload-page"]').forEach(function (btn) {
      btn.addEventListener("click", function () {
        self._renderCurrentRoute();
      });
    });
    view.querySelectorAll('[data-action="reload-app"]').forEach(function (btn) {
      btn.addEventListener("click", function () {
        window.location.reload();
      });
    });
  };

  OnlyYouConsoleApp.prototype._renderFatal = function (err) {
    var view = document.getElementById("view");
    if (!view) return;
    var message = (err && (err.message || err)) || "未知错误";
    view.innerHTML =
      '<div class="page"><div class="card"><div class="empty">' +
      icon("alert", 34) +
      '<div class="empty-title">无法连接 OnlyYou 后端</div>' +
      '<div class="empty-text">' +
      esc(message) +
      "<br />若在本地直接预览，请在 URL 后追加 <span class=\"mono\">?mock=1</span> 使用离线 Mock 数据。</div>" +
      '<div class="row mt-12"><button class="btn btn-outline" type="button" data-action="reload-app">重新加载</button></div>' +
      "</div></div></div>";
    var self = this;
    view.querySelectorAll('[data-action="reload-app"]').forEach(function (btn) {
      btn.addEventListener("click", function () {
        window.location.reload();
      });
    });
  };

  /* ------------------------------ Toast ------------------------------ */

  OnlyYouConsoleApp.prototype.toast = function (message, isError) {
    var container = document.getElementById("toast-container");
    if (!container) return;
    var item = document.createElement("div");
    item.className = "toast" + (isError ? " error" : "");
    item.innerHTML =
      icon(isError ? "alert" : "check", 14) + "<span>" + esc(message == null ? "" : message) + "</span>";
    container.appendChild(item);
    while (container.children.length > 4) {
      container.removeChild(container.firstChild);
    }
    setTimeout(function () {
      item.classList.add("is-leaving");
      setTimeout(function () {
        if (item.parentNode) item.parentNode.removeChild(item);
      }, 220);
    }, 3200);
  };

  /* ------------------------------ Modal / Drawer ------------------------------ */

  OnlyYouConsoleApp.prototype._actionsHtml = function (actions) {
    var html = "";
    (actions || []).forEach(function (action, index) {
      var cls = "btn";
      if (action.primary) cls += " btn-primary";
      else if (action.danger) cls += " btn-danger";
      else cls += " btn-outline";
      if (action.confirmDanger) cls = "btn btn-danger-solid";
      html +=
        '<button class="' +
        cls +
        '" type="button" data-action-index="' +
        index +
        '">' +
        (action.icon ? icon(action.icon, 14) : "") +
        esc(action.label || "确定") +
        "</button>";
    });
    return html;
  };

  OnlyYouConsoleApp.prototype._bindOverlayActions = function (root, stateKey, closeFn) {
    var self = this;
    root.querySelectorAll("[data-overlay-close]").forEach(function (el) {
      el.addEventListener("click", function () {
        closeFn.call(self);
      });
    });
    var foot = root.querySelector(".modal-foot, .drawer-foot");
    if (!foot) return;
    foot.addEventListener("click", function (event) {
      var btn = event.target.closest("[data-action-index]");
      if (!btn) return;
      var state = self[stateKey];
      if (!state) return;
      var index = Number(btn.getAttribute("data-action-index"));
      var action = (state.actions || [])[index];
      if (!action) return;
      if (typeof action.onClick !== "function") {
        closeFn.call(self);
        return;
      }
      btn.disabled = true;
      Promise.resolve()
        .then(function () {
          return action.onClick(self, state.bodyEl);
        })
        .then(function (result) {
          if (result === false) {
            btn.disabled = false;
            return;
          }
          closeFn.call(self, "action");
        })
        .catch(function (err) {
          btn.disabled = false;
          self.toast((err && (err.message || err)) || "操作失败", true);
        });
    });
  };

  OnlyYouConsoleApp.prototype.modal = function (opts) {
    opts = opts || {};
    var root = document.getElementById("modal-root");
    if (!root) return null;
    if (this._modalState) this.closeModal();
    var actions = opts.actions || [];
    root.innerHTML =
      '<div class="overlay-backdrop" data-overlay-close="1"></div>' +
      '<div class="modal-wrap"><div class="modal' +
      (opts.wide ? " is-wide" : "") +
      '" role="dialog" aria-modal="true" aria-label="' +
      esc(opts.title || "对话框") +
      '">' +
      '<div class="modal-head"><div class="modal-title">' +
      esc(opts.title || "") +
      "</div>" +
      '<button class="icon-btn" type="button" data-overlay-close="1" aria-label="关闭">' +
      icon("close", 16) +
      "</button></div>" +
      '<div class="modal-body"></div>' +
      (actions.length ? '<div class="modal-foot">' + this._actionsHtml(actions) + "</div>" : "") +
      "</div></div>";
    var bodyEl = root.querySelector(".modal-body");
    if (opts.body instanceof Node) bodyEl.appendChild(opts.body);
    else bodyEl.innerHTML = opts.body == null ? "" : String(opts.body);
    this._modalState = { actions: actions, bodyEl: bodyEl, opts: opts };
    root.classList.add("open");
    this._bindOverlayActions(root, "_modalState", this.closeModal);
    if (typeof opts.onMount === "function") {
      try {
        opts.onMount(bodyEl, this);
      } catch (err) {
        console.warn("[OnlyYou] modal onMount 失败：", err);
      }
    }
    var focusable = bodyEl.querySelector("input, select, textarea, button");
    if (focusable) {
      setTimeout(function () {
        try {
          focusable.focus();
        } catch (err) {
          /* ignore */
        }
      }, 40);
    }
    return bodyEl;
  };

  OnlyYouConsoleApp.prototype.closeModal = function (reason) {
    var root = document.getElementById("modal-root");
    var state = this._modalState;
    this._modalState = null;
    if (root) {
      root.classList.remove("open");
      root.innerHTML = "";
    }
    if (state && state.opts && typeof state.opts.onClose === "function") {
      try {
        state.opts.onClose(reason || "close");
      } catch (err) {
        console.warn("[OnlyYou] modal onClose 失败：", err);
      }
    }
  };

  OnlyYouConsoleApp.prototype.drawer = function (opts) {
    opts = opts || {};
    var root = document.getElementById("drawer-root");
    if (!root) return null;
    if (this._drawerState) this.closeDrawer();
    var actions = opts.actions || [];
    root.innerHTML =
      '<div class="overlay-backdrop" data-overlay-close="1"></div>' +
      '<div class="drawer" role="dialog" aria-modal="true" aria-label="' +
      esc(opts.title || "侧栏") +
      '">' +
      '<div class="drawer-head"><div class="drawer-title">' +
      esc(opts.title || "") +
      "</div>" +
      '<button class="icon-btn" type="button" data-overlay-close="1" aria-label="关闭">' +
      icon("close", 16) +
      "</button></div>" +
      '<div class="drawer-body"></div>' +
      (actions.length ? '<div class="drawer-foot">' + this._actionsHtml(actions) + "</div>" : "") +
      "</div>";
    var bodyEl = root.querySelector(".drawer-body");
    if (opts.body instanceof Node) bodyEl.appendChild(opts.body);
    else bodyEl.innerHTML = opts.body == null ? "" : String(opts.body);
    this._drawerState = { actions: actions, bodyEl: bodyEl, opts: opts };
    root.classList.add("open");
    this._bindOverlayActions(root, "_drawerState", this.closeDrawer);
    if (typeof opts.onMount === "function") {
      try {
        opts.onMount(bodyEl, this);
      } catch (err) {
        console.warn("[OnlyYou] drawer onMount 失败：", err);
      }
    }
    return bodyEl;
  };

  OnlyYouConsoleApp.prototype.closeDrawer = function (reason) {
    var root = document.getElementById("drawer-root");
    var state = this._drawerState;
    this._drawerState = null;
    if (root) {
      root.classList.remove("open");
      root.innerHTML = "";
    }
    if (state && state.opts && typeof state.opts.onClose === "function") {
      try {
        state.opts.onClose(reason || "close");
      } catch (err) {
        console.warn("[OnlyYou] drawer onClose 失败：", err);
      }
    }
  };

  /** 确认弹窗：返回 Promise<boolean> */
  OnlyYouConsoleApp.prototype.confirm = function (opts) {
    opts = opts || {};
    var self = this;
    return new Promise(function (resolve) {
      var settled = false;
      function finish(value) {
        if (settled) return;
        settled = true;
        resolve(value);
      }
      self.modal({
        title: opts.title || "确认操作",
        body: '<div class="stat-desc">' + esc(opts.message || "") + "</div>",
        actions: [
          {
            label: opts.cancelLabel || "取消",
            onClick: function () {
              finish(false);
            }
          },
          {
            label: opts.confirmLabel || "确认",
            primary: !opts.danger,
            danger: !!opts.danger,
            confirmDanger: !!opts.danger,
            onClick: async function () {
              try {
                if (typeof opts.onConfirm === "function") await opts.onConfirm();
                finish(true);
              } catch (err) {
                /* 弹窗保持打开，由用户重试或取消；外层不再重复 toast */
                self.toast((err && (err.message || err)) || "操作失败", true);
                return false;
              }
            }
          }
        ],
        onClose: function () {
          finish(false);
        }
      });
    });
  };

  /* ------------------------------ 日程状态干预公用逻辑 ------------------------------ */

  /** 生成「状态切换 + 护甲滑条 + 活动标题」表单 HTML（概览页弹窗 / 日程页抽屉共用） */
  OnlyYouConsoleApp.prototype.slotStateFormHtml = function (slot) {
    slot = slot || {};
    var stateMeta = this.stateMeta(slot.state);
    var armor = slot.armor == null ? 50 : Math.max(0, Math.min(100, Number(slot.armor) || 0));
    var html = "";
    html +=
      '<div class="field"><span class="field-label">状态</span><div class="segmented" data-role="state-seg">';
    ["IDLE", "BUSY", "RESTING"].forEach(function (id) {
      var meta = {
        IDLE: { label: "空闲", icon: "zap" },
        BUSY: { label: "繁忙", icon: "activity" },
        RESTING: { label: "休息", icon: "moon" }
      }[id];
      html +=
        '<button type="button" class="seg-btn' +
        (stateMeta.id === id ? " active" : "") +
        '" data-state="' +
        id +
        '">' +
        icon(meta.icon, 13) +
        meta.label +
        "</button>";
    });
    html += "</div></div>";
    html +=
      '<div class="field"><span class="field-label">正在做的事（活动标题）</span>' +
      '<input class="input" data-role="activity-input" maxlength="120" placeholder="如：窝在沙发里刷 B 站 / 苦逼赶稿中" value="' +
      esc(slot.title || "") +
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
  };

  OnlyYouConsoleApp.prototype.bindSlotStateForm = function (container) {
    if (!container) return;
    var seg = container.querySelector('[data-role="state-seg"]');
    if (seg) {
      seg.addEventListener("click", function (event) {
        var btn = event.target.closest("[data-state]");
        if (!btn) return;
        seg.querySelectorAll(".seg-btn").forEach(function (el) {
          el.classList.remove("active");
        });
        btn.classList.add("active");
      });
    }
    var slider = container.querySelector('[data-role="armor-slider"]');
    var valueEl = container.querySelector('[data-role="armor-value"]');
    if (slider && valueEl) {
      slider.addEventListener("input", function () {
        valueEl.textContent = slider.value;
      });
    }
  };

  OnlyYouConsoleApp.prototype.readSlotStateForm = function (container) {
    var active = container ? container.querySelector('[data-role="state-seg"] .seg-btn.active') : null;
    var slider = container ? container.querySelector('[data-role="armor-slider"]') : null;
    var titleEl = container ? container.querySelector('[data-role="activity-input"]') : null;
    return {
      new_state: active ? active.getAttribute("data-state") : "IDLE",
      armor: slider ? Number(slider.value) : 50,
      new_title: titleEl ? String(titleEl.value || "").trim() : ""
    };
  };

  /** POST schedule/force_state，并同步本地 activeSchedule */
  OnlyYouConsoleApp.prototype.forceState = async function (payload) {
    var body = {
      soul_id: this.state.currentSoulId,
      new_state: payload.new_state || "IDLE",
      armor: payload.armor == null ? 50 : Number(payload.armor),
      new_title: payload.new_title || "",
      slot_key: payload.slot_key || ""
    };
    var data = await this.apiPost("schedule/force_state", body);
    if (data && data.schedule) {
      this.state.activeSchedule = data.schedule;
    } else if (data && data.updated_slot) {
      this._applyUpdatedSlot(data.updated_slot);
    }
    if (data && data.updated_slot && data.schedule && data.schedule.run) {
      this._applyUpdatedSlot(data.updated_slot);
    }
    return data;
  };

  OnlyYouConsoleApp.prototype._applyUpdatedSlot = function (updatedSlot) {
    var schedule = this.state.activeSchedule;
    if (!schedule || !schedule.run || !Array.isArray(schedule.run.slots)) return;
    if (!updatedSlot || !updatedSlot.slot_key) return;
    var replaced = false;
    schedule.run.slots = schedule.run.slots.map(function (slot) {
      if (slot.slot_key === updatedSlot.slot_key) {
        replaced = true;
        return Object.assign({}, slot, updatedSlot);
      }
      return slot;
    });
    if (!replaced) schedule.run.slots.push(updatedSlot);
    if (schedule.current_slot && schedule.current_slot.slot_key === updatedSlot.slot_key) {
      schedule.current_slot = Object.assign({}, schedule.current_slot, updatedSlot);
    }
  };

  /* ------------------------------ 事件绑定 ------------------------------ */

  OnlyYouConsoleApp.prototype._bindStaticEvents = function () {
    if (this._bound) return;
    this._bound = true;
    var self = this;

    var refreshBtn = document.getElementById("btn-refresh");
    if (refreshBtn) {
      refreshBtn.addEventListener("click", function () {
        self.refresh();
      });
    }
    var themeBtn = document.getElementById("btn-theme");
    if (themeBtn) {
      themeBtn.addEventListener("click", function () {
        self.toggleTheme();
      });
    }
    var soulSelect = document.getElementById("soul-select");
    if (soulSelect) {
      soulSelect.addEventListener("change", function () {
        self.selectSoul(soulSelect.value);
      });
    }

    window.addEventListener("hashchange", function () {
      self.route = self._parseHash();
      self._renderNav();
      self._renderCurrentRoute();
    });

    var nav = document.getElementById("sidebar-nav");
    if (nav) {
      nav.addEventListener("click", function (event) {
        var item = event.target.closest(".nav-item");
        if (!item) return;
        var route = item.getAttribute("data-route");
        if (!route) return;
        if (route === self.route) {
          event.preventDefault();
          self._renderCurrentRoute();
        }
      });
    }

    window.addEventListener("keydown", function (event) {
      if (event.key !== "Escape") return;
      if (self._drawerState) {
        self.closeDrawer();
      } else if (self._modalState) {
        self.closeModal();
      }
    });
  };

  /* ====================================================================== *
   * 全局注册
   * ====================================================================== */
  window.OY_PAGES = window.OY_PAGES || {};
  window.OY_NAV_CONFIG = NAV_CONFIG;
  window.OY_PAGE_META = PAGE_META;
  window.OY_ESC = esc;
  window.OY_ICON = icon;
  window.unwrapApiPayload = unwrapApiPayload;
  window.OnlyYouConsoleApp = OnlyYouConsoleApp;

  var app = new OnlyYouConsoleApp();
  window.app = app;

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      app.init().catch(function (err) {
        console.error("[OnlyYou] 启动失败：", err);
        app._renderFatal(err);
      });
    });
  } else {
    setTimeout(function () {
      app.init().catch(function (err) {
        console.error("[OnlyYou] 启动失败：", err);
        app._renderFatal(err);
      });
    }, 0);
  }
})();
