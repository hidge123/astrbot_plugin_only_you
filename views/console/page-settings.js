/**
 * OnlyYou Plugin Pages — 设置页 (`views/console/page-settings.js`)
 *
 * 数据源：GET `settings/get`（core/config_manager.py 的 6 大分组 Schema + 当前值）
 * 保存：POST `settings/save`（后端 Schema 校验 / 范围钳制 → SQLite runtime_config
 *       → 原地热更新共享配置 → 心跳唤醒，**无需重启插件**）
 * 重置：POST `settings/reset`（按分组或全部恢复默认值）
 *
 * 交互：
 *  - 左侧分组 Tab（日程 / 主动搭话 / 读空气 / 记忆 / 关系 / 冲浪），支持 `#/settings?section=memory`
 *    深链（业务页页头的「设置」快捷入口通过 app.openSettings(section) 跳转）。
 *  - 每组内「常规配置」默认展开、「高级参数」默认折叠；可一键展开 / 折叠。
 *  - 顶部搜索框实时过滤字段（标题 / 键名 / 说明）。
 *  - 每个被改动的字段显示「已修改」小点与默认值对比；底部常驻保存条。
 *  - 「恢复本组默认」/「恢复全部默认」二次确认后生效。
 *  - 离开页面 / 切换角色 / 刷新时有未保存修改会触发 app 的 isDirty 守卫。
 *
 * 注册：window.OY_PAGES.settings = { id,title,icon,render(container,ctx),destroy(),isDirty() }
 */
(function () {
  "use strict";

  var S = {
    app: null,
    container: null,
    data: null,
    values: {},
    baseline: {},
    activeSection: "schedule",
    search: "",
    showAdvanced: {},
    dirty: false,
    saving: false
  };

  function esc(str) {
    return window.OY_ESC ? window.OY_ESC(str) : String(str == null ? "" : str);
  }

  function icon(name, size) {
    return typeof window.icon === "function" ? window.icon(name, size || 16) : "";
  }

  function clone(obj) {
    try {
      return JSON.parse(JSON.stringify(obj));
    } catch (err) {
      return obj;
    }
  }

  function deepEqual(a, b) {
    return JSON.stringify(a) === JSON.stringify(b);
  }

  /* ------------------------------------------------------------------ *
   * 数据加载
   * ------------------------------------------------------------------ */
  async function loadData(app, showToast) {
    var data = await app.apiGet("settings/get");
    if (!data || !Array.isArray(data.sections)) {
      throw new Error("settings/get 返回数据为空");
    }
    S.data = data;
    S.values = {};
    S.baseline = {};
    data.sections.forEach(function (section) {
      var values = {};
      (section.fields || []).forEach(function (field) {
        values[field.key] = clone(field.value);
      });
      S.values[section.id] = values;
      S.baseline[section.id] = clone(values);
    });
    S.dirty = false;
    if (showToast) app.toast("运行配置已刷新");
    return data;
  }

  function sectionById(sectionId) {
    var sections = (S.data && S.data.sections) || [];
    for (var i = 0; i < sections.length; i++) {
      if (sections[i].id === sectionId) return sections[i];
    }
    return null;
  }

  /* ------------------------------------------------------------------ *
   * 字段渲染
   * ------------------------------------------------------------------ */
  function fieldDirty(sectionId, field) {
    var draft = (S.values[sectionId] || {})[field.key];
    return !deepEqual(draft, field.value);
  }

  function collectDirtySections() {
    var payload = {};
    ((S.data && S.data.sections) || []).forEach(function (section) {
      var fields = section.fields || [];
      var changed = {};
      var has = false;
      fields.forEach(function (field) {
        if (!fieldDirty(section.id, field)) return;
        changed[field.key] = S.values[section.id][field.key];
        has = true;
      });
      if (has) payload[section.id] = changed;
    });
    return payload;
  }

  function refreshDirtyState() {
    var payload = collectDirtySections();
    S.dirty = Object.keys(payload).length > 0;
    if (S.app) S.app._renderTopbarIcons();
    var bar = S.container ? S.container.querySelector("[data-role='settings-savebar']") : null;
    if (bar) {
      bar.classList.toggle("is-dirty", S.dirty);
      var status = bar.querySelector("[data-role='settings-save-status']");
      if (status) {
        status.textContent = S.dirty ? "有未保存的修改" : "所有修改已保存";
        status.className =
          "settings-save-status" + (S.dirty ? " is-dirty" : " is-saved");
      }
    }
    var count = 0;
    Object.keys(payload).forEach(function (key) {
      count += Object.keys(payload[key]).length;
    });
    var badge = S.container ? S.container.querySelector("[data-role='settings-dirty-count']") : null;
    if (badge) {
      badge.textContent = S.dirty ? "已修改 " + count + " 项" : "";
      badge.style.display = S.dirty ? "" : "none";
    }
  }

  function defaultHint(field) {
    if (field.type === "secret") return "";
    var def = field["default"];
    if (def === "" || def == null) return "默认：空";
    if (Array.isArray(def)) return "默认：" + def.slice(0, 4).join(" / ") + (def.length > 4 ? " …" : "");
    if (typeof def === "object") return "默认：已内置";
    return "默认：" + String(def) + (field.unit ? " " + field.unit : "");
  }

  function fieldMetaHtml(sectionId, field) {
    return (
      '<div class="settings-field-meta">' +
      '<span class="settings-key mono">' +
      esc(field.key) +
      "</span>" +
      (defaultHint(field)
        ? '<span class="settings-default">' + esc(defaultHint(field)) + "</span>"
        : "") +
      '<span class="settings-modified" data-role="modified-' +
      esc(field.key) +
      '" style="display:' +
      (fieldDirty(sectionId, field) ? "inline-flex" : "none") +
      '">已修改</span></div>'
    );
  }

  function numberInputHtml(sectionId, field, value, step) {
    return (
      '<input class="input settings-input" type="number" data-section="' +
      esc(sectionId) +
      '" data-key="' +
      esc(field.key) +
      '"' +
      (field.min != null ? ' min="' + field.min + '"' : "") +
      (field.max != null ? ' max="' + field.max + '"' : "") +
      ' step="' +
      (step || (field.type === "float" ? "0.01" : "1")) +
      '" value="' +
      esc(value == null ? "" : value) +
      '" />' +
      (field.unit ? '<span class="settings-unit">' + esc(field.unit) + "</span>" : "")
    );
  }

  function objectInputHtml(sectionId, field, value) {
    var current = value && typeof value === "object" ? value : {};
    var subs = Array.isArray(field.fields) ? field.fields : [];
    if (!subs.length) {
      return '<textarea class="textarea settings-textarea" rows="3" data-section="' + esc(sectionId) +
        '" data-key="' + esc(field.key) + '" data-value-type="json">' +
        esc(JSON.stringify(current, null, 2)) + "</textarea>";
    }
    return (
      '<div class="settings-object-grid">' +
      subs
        .map(function (sub) {
          var subValue = current[sub.key] != null ? current[sub.key] : 0;
          return (
            '<label class="settings-object-item"><span>' +
            esc(sub.label || sub.key) +
            "</span>" +
            '<input class="input settings-input" type="number" min="0" max="100" step="1" data-section="' +
            esc(sectionId) +
            '" data-key="' +
            esc(field.key) +
            '" data-subkey="' +
            esc(sub.key) +
            '" value="' +
            esc(subValue) +
            '" /></label>'
          );
        })
        .join("") +
      "</div>"
    );
  }

  function ratiosInputHtml(sectionId, field, value) {
    var current = value && typeof value === "object" ? value : {};
    var labels = { "3": "T3 核心", "2": "T2 近期", "1": "T1 瞬时" };
    return (
      '<div class="settings-ratio-grid">' +
      ["3", "2", "1"]
        .map(function (key) {
          return (
            '<label class="settings-ratio-item"><span>' +
            esc(labels[key]) +
            "</span>" +
            '<input class="input settings-input" type="number" min="0" max="100" step="1" data-section="' +
            esc(sectionId) +
            '" data-key="' +
            esc(field.key) +
            '" data-subkey="' +
            key +
            '" value="' +
            esc(current[key] != null ? current[key] : 0) +
            '" /><span class="settings-unit">%</span></label>'
          );
        })
        .join("") +
      "</div>"
    );
  }

  function fieldControlHtml(section, field) {
    var sectionId = section.id;
    var value = (S.values[sectionId] || {})[field.key];
    if (field.type === "bool") {
      return (
        '<label class="settings-switch"><input type="checkbox" data-section="' +
        esc(sectionId) +
        '" data-key="' +
        esc(field.key) +
        '" data-value-type="bool"' +
        (value ? " checked" : "") +
        ' /><span class="settings-switch-track" aria-hidden="true"></span><span class="settings-switch-text" data-role="bool-text-' +
        esc(field.key) +
        '">' +
        (value ? "已开启" : "已关闭") +
        "</span></label>"
      );
    }
    if (field.type === "int" || field.type === "float") {
      return '<div class="settings-number">' + numberInputHtml(sectionId, field, value) + "</div>";
    }
    if (field.type === "secret") {
      return (
        '<div class="settings-secret">' +
        '<input class="input settings-input" type="password" autocomplete="new-password" data-section="' +
        esc(sectionId) +
        '" data-key="' +
        esc(field.key) +
        '" value="' +
        esc(value || "") +
        '" placeholder="未设置" />' +
        '<button class="btn btn-ghost btn-sm" type="button" data-action="toggle-secret" data-target-key="' +
        esc(field.key) +
        '">显示</button></div>'
      );
    }
    if (field.type === "list") {
      var list = Array.isArray(value) ? value : [];
      return (
        '<textarea class="textarea settings-textarea" rows="2" data-section="' +
        esc(sectionId) +
        '" data-key="' +
        esc(field.key) +
        '" data-value-type="list" placeholder="用逗号分隔，例如：急, 救命, 大瓜">' +
        esc(list.join(", ")) +
        "</textarea>"
      );
    }
    if (field.type === "object") return objectInputHtml(sectionId, field, value);
    if (field.type === "ratios") return ratiosInputHtml(sectionId, field, value);
    return (
      '<input class="input settings-input" type="text" data-section="' +
      esc(sectionId) +
      '" data-key="' +
      esc(field.key) +
      '" value="' +
      esc(value || "") +
      '" />'
    );
  }

  function fieldMatchesSearch(field, keyword) {
    if (!keyword) return true;
    var haystack = [field.label, field.key, field.hint, field.unit || ""].join(" ").toLowerCase();
    return haystack.indexOf(keyword) >= 0;
  }

  function fieldHtml(section, field, keyword) {
    if (!fieldMatchesSearch(field, keyword)) return "";
    return (
      '<div class="settings-field' +
      (field.advanced ? " is-advanced" : "") +
      '" data-field-key="' +
      esc(field.key) +
      '"><div class="settings-field-head"><span class="settings-label">' +
      esc(field.label) +
      "</span>" +
      fieldMetaHtml(section.id, field) +
      "</div>" +
      '<div class="settings-control">' +
      fieldControlHtml(section, field) +
      "</div>" +
      (field.hint ? '<div class="settings-hint">' + esc(field.hint) + "</div>" : "") +
      "</div>"
    );
  }

  function fieldsBlock(section, keyword) {
    var regular = [];
    var advanced = [];
    (section.fields || []).forEach(function (field) {
      if (!fieldMatchesSearch(field, keyword)) return;
      (field.advanced ? advanced : regular).push(field);
    });
    var html = "";
    if (regular.length) {
      html +=
        '<div class="settings-fields-group">' +
        '<div class="settings-group-head"><span>常规配置</span><span class="card-sub">' +
        regular.length +
        " 项</span></div>" +
        regular
          .map(function (field) {
            return fieldHtml(section, field, keyword);
          })
          .join("") +
        "</div>";
    }
    if (advanced.length) {
      var open = !!S.showAdvanced[section.id] || !!keyword;
      html +=
        '<div class="settings-fields-group' +
        (open ? " is-open" : "") +
        '" data-role="advanced-group"><button class="settings-group-head is-collapsible" type="button" data-action="toggle-advanced" data-section="' +
        esc(section.id) +
        '"><span>' +
        icon("sliders", 14) +
        "高级参数<span class=\"card-sub\">" +
        advanced.length +
        ' 项 · 默认折叠</span></span><span class="settings-caret">' +
        (open ? "▾" : "▸") +
        "</span></button>" +
        '<div class="settings-advanced-body">' +
        advanced
          .map(function (field) {
            return fieldHtml(section, field, keyword);
          })
          .join("") +
        "</div></div>";
    }
    if (!html) {
      html =
        '<div class="empty"><div class="empty-title">没有匹配的配置项</div><div class="empty-text">换个关键词，或清空搜索框查看全部分组。</div></div>';
    }
    return html;
  }

  function tabsHtml() {
    var sections = (S.data && S.data.sections) || [];
    return sections
      .map(function (section) {
        var changed = 0;
        (section.fields || []).forEach(function (field) {
          if (fieldDirty(section.id, field)) changed += 1;
        });
        return (
          '<button class="settings-tab' +
          (section.id === S.activeSection ? " is-active" : "") +
          '" type="button" data-action="select-section" data-section="' +
          esc(section.id) +
          '">' +
          icon(section.icon, 15) +
          "<span>" +
          esc(section.title) +
          "</span>" +
          (changed ? '<span class="settings-tab-dot" title="已修改">' + changed + "</span>" : "") +
          "</button>"
        );
      })
      .join("");
  }

  function sectionHtml() {
    var section = sectionById(S.activeSection) || ((S.data && S.data.sections) || [])[0];
    if (!section) return '<div class="empty"><div class="empty-title">没有可配置的分组</div></div>';
    S.activeSection = section.id;
    var keyword = S.search.trim().toLowerCase();
    return (
      '<div class="card settings-section"><div class="card-head"><div class="card-title">' +
      icon(section.icon, 16) +
      esc(section.title) +
      '<span class="card-sub">' +
      esc(section.description || "") +
      "</span></div>" +
      '<div class="card-actions">' +
      '<button class="btn btn-outline btn-sm" type="button" data-action="toggle-all-advanced">' +
      icon("sliders", 13) +
      "展开/折叠高级" +
      "</button>" +
      '<button class="btn btn-ghost btn-sm" type="button" data-action="reset-section" data-section="' +
      esc(section.id) +
      '">恢复本组默认</button></div></div>' +
      '<div class="card-body">' +
      fieldsBlock(section, keyword) +
      "</div></div>"
    );
  }

  function buildHtml() {
    var sections = (S.data && S.data.sections) || [];
    var html = "";
    html +=
      '<div class="page-head"><h1 class="page-title">' +
      icon("settings", 18) +
      "设置" +
      "</h1>" +
      '<div class="page-actions">' +
      '<span class="badge badge-warn" data-role="settings-dirty-count" style="display:none"></span>' +
      '<button class="btn btn-outline btn-sm" type="button" data-action="reset-all">' +
      icon("refresh", 13) +
      "恢复全部默认</button>" +
      '<button class="btn btn-primary btn-sm" type="button" data-action="save-settings">' +
      icon("save", 13) +
      "保存修改</button></div></div>";
    html +=
      '<div class="card settings-toolbar"><div class="settings-search">' +
      icon("search", 14) +
      '<input class="input" type="search" data-role="settings-search" placeholder="搜索配置项（标题 / 键名 / 说明）" value="' +
      esc(S.search) +
      '" /></div>' +
      '<div class="settings-toolbar-hint">' +
      icon("info", 13) +
      "配置保存在插件数据库中，保存后立即生效，无需重启 AstrBot。</div></div>";
    html += '<div class="settings-layout">';
    html += '<nav class="settings-tabs" aria-label="设置分组">' + tabsHtml() + "</nav>";
    html += '<div class="settings-main">' + sectionHtml() + "</div>";
    html += "</div>";
    html +=
      '<div class="settings-savebar" data-role="settings-savebar">' +
      '<span class="settings-save-status is-saved" data-role="settings-save-status">所有修改已保存</span>' +
      '<span class="settings-save-note">恢复默认只影响当前运行配置，不会删除角色 / 记忆 / 关系数据。</span></div>';
    return html;
  }

  /* ------------------------------------------------------------------ *
   * 事件处理
   * ------------------------------------------------------------------ */
  function markFieldModified(sectionId, key) {
    var section = sectionById(sectionId);
    if (!section) return;
    var field = null;
    (section.fields || []).forEach(function (item) {
      if (item.key === key) field = item;
    });
    if (!field) return;
    var isDirty = fieldDirty(sectionId, field);
    var badge = S.container
      ? S.container.querySelector('[data-role="modified-' + key + '"]')
      : null;
    if (badge) badge.style.display = isDirty ? "inline-flex" : "none";
    var text = S.container ? S.container.querySelector('[data-role="bool-text-' + key + '"]') : null;
    if (text) text.textContent = S.values[sectionId][key] ? "已开启" : "已关闭";
    refreshDirtyState();
  }

  function readControlValue(el) {
    var type = el.getAttribute("data-value-type");
    if (el.type === "checkbox") return !!el.checked;
    if (type === "bool") return !!el.checked;
    if (type === "list") {
      return String(el.value || "")
        .split(/[,，、\n]+/)
        .map(function (part) {
          return part.trim();
        })
        .filter(Boolean);
    }
    if (type === "json") {
      try {
        return JSON.parse(el.value);
      } catch (err) {
        return el.value;
      }
    }
    if (el.type === "number") {
      var number = Number(el.value);
      return isFinite(number) ? number : 0;
    }
    return String(el.value == null ? "" : el.value);
  }

  function bindContainerEvents(container, app) {
    container.oninput = function (event) {
      var target = event.target;
      if (!target) return;
      if (target.getAttribute("data-role") === "settings-search") {
        S.search = target.value || "";
        render(container, app);
        var search = container.querySelector('[data-role="settings-search"]');
        if (search) {
          search.focus();
          try {
            search.setSelectionRange(S.search.length, S.search.length);
          } catch (err) {
            /* ignore */
          }
        }
        return;
      }
      var key = target.getAttribute("data-key");
      var sectionId = target.getAttribute("data-section");
      if (!key || !sectionId) return;
      if (target.getAttribute("data-subkey")) {
        var subKey = target.getAttribute("data-subkey");
        var current = S.values[sectionId][key];
        if (!current || typeof current !== "object") current = {};
        current[subKey] = target.type === "number" ? Number(target.value) : target.value;
        S.values[sectionId][key] = current;
      } else {
        S.values[sectionId][key] = readControlValue(target);
      }
      markFieldModified(sectionId, key);
    };
    container.onchange = function (event) {
      var target = event.target;
      if (!target) return;
      var key = target.getAttribute("data-key");
      var sectionId = target.getAttribute("data-section");
      if (!key || !sectionId) return;
      /* 部分控件（checkbox / select）只会稳定派发 change，这里兜底同步取值 */
      if (target.getAttribute("data-subkey")) {
        var subKey = target.getAttribute("data-subkey");
        var current = S.values[sectionId][key];
        if (!current || typeof current !== "object") current = {};
        current[subKey] =
          target.type === "number" ? Number(target.value) : target.value;
        S.values[sectionId][key] = current;
      } else {
        S.values[sectionId][key] = readControlValue(target);
      }
      markFieldModified(sectionId, key);
    };
    container.onclick = function (event) {
      var target = event.target;
      if (!target || !target.closest) return;
      var actionEl = target.closest("[data-action]");
      if (!actionEl) return;
      var action = actionEl.getAttribute("data-action");
      if (action === "select-section") {
        S.activeSection = actionEl.getAttribute("data-section") || S.activeSection;
        paint(container, app);
      } else if (action === "toggle-advanced") {
        var sectionId = actionEl.getAttribute("data-section");
        S.showAdvanced[sectionId] = !S.showAdvanced[sectionId];
        paint(container, app);
      } else if (action === "toggle-all-advanced") {
        var section = sectionById(S.activeSection);
        var hasAdvanced = section && (section.fields || []).some(function (field) {
          return field.advanced;
        });
        S.showAdvanced[S.activeSection] = hasAdvanced ? !S.showAdvanced[S.activeSection] : false;
        paint(container, app);
      } else if (action === "toggle-secret") {
        var key = actionEl.getAttribute("data-target-key");
        var input = container.querySelector(
          '.settings-secret input[data-key="' + key + '"]'
        );
        if (input) {
          var shown = input.type === "text";
          input.type = shown ? "password" : "text";
          actionEl.textContent = shown ? "显示" : "隐藏";
        }
      } else if (action === "save-settings") {
        saveSettings(app, container);
      } else if (action === "reset-section") {
        resetConfig(app, container, actionEl.getAttribute("data-section"));
      } else if (action === "reset-all") {
        resetConfig(app, container, "");
      }
    };
    container.onkeydown = function (event) {
      if ((event.metaKey || event.ctrlKey) && String(event.key).toLowerCase() === "s") {
        event.preventDefault();
        saveSettings(app, container);
      }
    };
  }

  async function saveSettings(app, container) {
    if (S.saving) return;
    var payload = collectDirtySections();
    if (!Object.keys(payload).length) {
      app.toast("没有需要保存的修改");
      return;
    }
    S.saving = true;
    var btn = container.querySelector('[data-action="save-settings"]');
    if (btn) {
      btn.disabled = true;
      btn.classList.add("is-loading");
    }
    try {
      var data = await app.apiPost("settings/save", { config: payload });
      if (data && Array.isArray(data.sections)) {
        S.data.sections = data.sections;
        data.sections.forEach(function (section) {
          var serverValues = {};
          (section.fields || []).forEach(function (field) {
            serverValues[field.key] = clone(field.value);
          });
          S.baseline[section.id] = clone(serverValues);
          S.values[section.id] = clone(serverValues);
        });
      }
      S.dirty = false;
      paint(container, app);
      app.toast("设置已保存并即时生效");
    } catch (err) {
      app.toast("保存设置失败：" + (err.message || err), true);
    } finally {
      S.saving = false;
      if (btn) {
        btn.disabled = false;
        btn.classList.remove("is-loading");
      }
    }
  }

  async function resetConfig(app, container, sectionId) {
    var section = sectionId ? sectionById(sectionId) : null;
    var scopeLabel = section ? "「" + section.title + "」" : "全部设置分组";
    var ok = await app.confirm({
      title: "恢复默认配置？",
      message: "将把" + scopeLabel + "恢复为内置默认值并立即生效。角色、记忆、关系数据不受影响。",
      danger: true,
      confirmLabel: "恢复默认",
      onConfirm: async function () {
        var data = await app.apiPost("settings/reset", sectionId ? { section: sectionId } : {});
        if (data && Array.isArray(data.sections)) {
          S.data.sections = data.sections;
          data.sections.forEach(function (item) {
            var serverValues = {};
            (item.fields || []).forEach(function (field) {
              serverValues[field.key] = clone(field.value);
            });
            S.baseline[item.id] = clone(serverValues);
            S.values[item.id] = clone(serverValues);
          });
        }
        S.dirty = false;
        paint(container, app);
        app.toast("已恢复默认配置");
      }
    });
    if (!ok) return;
  }

  /* ------------------------------------------------------------------ *
   * 渲染 / 生命周期
   * ------------------------------------------------------------------ */
  function paint(container, app) {
    container.innerHTML = buildHtml();
    bindContainerEvents(container, app);
    refreshDirtyState();
  }

  async function render(container, app) {
    S.app = app;
    S.container = container;
    try {
      container.innerHTML =
        '<div class="loading"><span class="spinner"></span><span>加载运行配置…</span></div>';
      await loadData(app, false);
      var requested = "";
      try {
        requested = app.consumeSettingsSection ? app.consumeSettingsSection() : "";
      } catch (err) {
        requested = "";
      }
      if (requested && sectionById(requested)) S.activeSection = requested;
      paint(container, app);
    } catch (err) {
      console.error("[OnlyYou] 设置页渲染失败：", err);
      container.innerHTML =
        '<div class="card"><div class="empty">' +
        icon("alert", 32) +
        '<div class="empty-title">运行配置加载失败</div>' +
        '<div class="empty-text">' +
        esc((err && (err.message || err)) || "未知错误") +
        "</div>" +
        '<div class="row mt-12"><button class="btn btn-outline" type="button" data-action="retry-settings">重试</button></div>' +
        "</div></div>";
      container.onclick = function (event) {
        var target = event.target;
        if (target && target.closest && target.closest('[data-action="retry-settings"]')) {
          render(container, app);
        }
      };
    }
  }

  function destroy() {
    S.container = null;
    S.data = null;
    S.app = null;
    S.values = {};
    S.baseline = {};
    S.search = "";
    S.dirty = false;
  }

  window.OY_PAGES = window.OY_PAGES || {};
  window.OY_PAGES.settings = {
    id: "settings",
    title: "设置",
    icon: "settings",
    render: render,
    destroy: destroy,
    isDirty: function () {
      return !!S.dirty;
    }
  };
})();
