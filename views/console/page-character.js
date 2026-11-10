/**
 * OnlyYou Plugin Pages — 角色卡页 (`views/console/page-character.js`)
 *
 * 数据源：
 *   GET  `card/schema`            → { schema: CARD_SCHEMA, card, soul_id }
 *   POST `card/save`              → { soul, souls }
 *   POST `card/ai_generate`       → { draft_card, section }（整卡或单分区，不落库）
 *   POST `card/preview_prompt`    → { prompt, chars }
 *   POST `souls/import_persona`   → { soul, schedule, souls }
 *   POST `souls/save` / `souls/rename` / `souls/delete`
 *
 * 页面结构：
 *   左栏：头像（支持点击上传本地图片）/ 名字 / 一句话定位 / 10 分区完成度导航 / 操作
 *         （AI 生成整卡、从 AstrBot 导入、Prompt 预览、⋯ 新建·改 ID·删除）。
 *   右栏：按 CARD_SCHEMA 动态渲染 10 个分区卡片，支持 text / textarea / tags / list / select /
 *         dialogs 六种编辑器；每个分区「AI 补全」；AI 结果先作为草稿填入（青绿描边），
 *         可「采用 / 放弃草稿」；底部固定保存栏在有改动时显示「未保存修改」。
 *
 * 注册：window.OY_PAGES.character = { id, title, icon, render(container, ctx), destroy() }
 * 约定：classic script / IIFE / 无 import-export / 不使用 emoji 作为图标（用 ctx.icon）。
 */
(function () {
  "use strict";

  /* ------------------------------------------------------------------ *
   * 模块内状态（每次 render 重置）
   * ------------------------------------------------------------------ */
  var S = {
    app: null,
    container: null,
    destroyed: false,
    schema: [],
    card: null,
    savedCard: null,
    soulId: "",
    dirty: false,
    draft: null, // { kind: "card"|"section", sections: [id], before: cardSnapshot }
    activeSection: "",
    busy: false
  };

  /* ------------------------------------------------------------------ *
   * 基础工具
   * ------------------------------------------------------------------ */
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

  function num(value, fallback) {
    var n = Number(value);
    return isFinite(n) ? n : fallback || 0;
  }

  function splitTags(value) {
    return String(value == null ? "" : value)
      .split(/[,，、/;；|]+/)
      .map(function (item) {
        return item.trim();
      })
      .filter(Boolean);
  }

  function splitLines(value) {
    return String(value == null ? "" : value)
      .split(/\r?\n/)
      .map(function (item) {
        return item.trim();
      })
      .filter(Boolean);
  }

  function isBlank(value) {
    if (Array.isArray(value)) return value.length === 0;
    if (value && typeof value === "object") return Object.keys(value).length === 0;
    return String(value == null ? "" : value).trim() === "";
  }

  function fieldType(field) {
    return String((field && field.type) || "text").toLowerCase();
  }

  function isReadonly(field) {
    if (!field) return false;
    if (field.readonly === true) return true;
    /* mock schema 里曾把 {readonly:true} 误塞进 help 字段，这里做兼容判断 */
    var help = field.help;
    if (help && typeof help === "object" && help.readonly === true) return true;
    return false;
  }

  function helpText(field) {
    var help = field ? field.help : "";
    return typeof help === "string" ? help : "";
  }

  /* ------------------------------ 头像工具 ------------------------------ */

  /** 头像值是否为图片（优先走 app.isImageAvatar，保证与全局组件判定一致） */
  function isImageAvatarValue(value) {
    if (S.app && typeof S.app.isImageAvatar === "function") return S.app.isImageAvatar(value);
    var v = String(value == null ? "" : value).trim();
    return /^data:image\//i.test(v) || /^https?:\/\//i.test(v) || /^\/[^\s]/i.test(v);
  }

  /** 头像内部 HTML：图片 → <img class="avatar-img">；否则 → Emoji / 首字文本 */
  function renderAvatarInner(avatarVal, fallbackName) {
    if (S.app && typeof S.app.renderAvatarInner === "function") {
      return S.app.renderAvatarInner(avatarVal, fallbackName);
    }
    var raw = String(avatarVal == null ? "" : avatarVal).trim();
    if (isImageAvatarValue(raw)) {
      return (
        '<img class="avatar-img" src="' +
        esc(raw) +
        '" alt="' +
        esc(fallbackName ? String(fallbackName) + "的头像" : "角色头像") +
        '" loading="lazy" />'
      );
    }
    if (raw) return esc(raw);
    var name = String(fallbackName == null ? "" : fallbackName).trim();
    return esc(name ? name.slice(0, 1) : "?");
  }

  /** 选择本地图片（由 app.pickAvatarImage 统一裁剪压缩），取消 / 失败 resolve("") */
  function pickAvatarImage(options) {
    if (!S.app || typeof S.app.pickAvatarImage !== "function") return Promise.resolve("");
    return S.app.pickAvatarImage(options || {});
  }

  /** 头像展示的兜底名字：identity.name → 角色名 → Soul ID */
  function fallbackAvatarName() {
    var name = identityValue("name");
    if (name) return name;
    var soul = metaSoul();
    if (soul && soul.name) return String(soul.name);
    return S.soulId || "?";
  }

  /** 左侧画像卡头像展示值（角色卡 identity.avatar，未设置时回退名字首字） */
  function displayAvatarValue() {
    return identityValue("avatar");
  }

  /** 头像上传选择器 HTML（导入 / 新建弹窗共用） */
  function avatarPickerHtml(prefix, fallbackName, help) {
    return (
      '<div class="avatar-picker" data-role="' +
      esc(prefix) +
      '-picker">' +
      '<span class="avatar avatar-lg avatar-preview" data-role="' +
      esc(prefix) +
      '-preview">' +
      renderAvatarInner("", fallbackName) +
      "</span>" +
      '<div class="avatar-picker-actions">' +
      '<button class="btn btn-outline btn-sm" type="button" data-role="' +
      esc(prefix) +
      '-upload">' +
      icon("upload", 13) +
      "上传图片</button>" +
      '<button class="btn btn-ghost btn-sm" type="button" data-role="' +
      esc(prefix) +
      '-clear">' +
      icon("close", 13) +
      "清除图片</button>" +
      "</div></div>" +
      (help ? '<div class="field-help">' + esc(help) + "</div>" : "")
    );
  }

  /** 绑定头像选择器（弹窗内使用）：返回 { value, render, picking } 可变状态 */
  function bindAvatarPicker(root, prefix, state, getFallbackName) {
    state = state || { value: "", picking: false };
    state.picking = false;
    var previewEl = root ? root.querySelector('[data-role="' + prefix + '-preview"]') : null;
    var uploadBtn = root ? root.querySelector('[data-role="' + prefix + '-upload"]') : null;
    var clearBtn = root ? root.querySelector('[data-role="' + prefix + '-clear"]') : null;
    state.render = function () {
      if (!previewEl) return;
      var fallback = typeof getFallbackName === "function" ? getFallbackName() : "";
      previewEl.innerHTML = renderAvatarInner(state.value, fallback);
    };
    if (uploadBtn) {
      uploadBtn.addEventListener("click", function () {
        if (state.picking) return;
        state.picking = true;
        pickAvatarImage({ size: 160 })
          .then(function (dataUrl) {
            state.picking = false;
            if (!dataUrl) return; /* 用户取消 */
            if (!/^data:image\//i.test(dataUrl) || dataUrl.length > 120000) {
              if (S.app) S.app.toast("头像图片处理失败或体积过大，请换一张图片重试", true);
              return;
            }
            state.value = dataUrl;
            state.render();
          })
          .catch(function () {
            state.picking = false;
          });
      });
    }
    if (clearBtn) {
      clearBtn.addEventListener("click", function () {
        state.value = "";
        state.render();
      });
    }
    state.render();
    return state;
  }

  function findSection(sectionId) {
    for (var i = 0; i < S.schema.length; i++) {
      if (S.schema[i] && S.schema[i].id === sectionId) return S.schema[i];
    }
    return null;
  }

  function findField(section, fieldKey) {
    var fields = (section && section.fields) || [];
    for (var i = 0; i < fields.length; i++) {
      if (fields[i] && fields[i].key === fieldKey) return fields[i];
    }
    return null;
  }

  /* ------------------------------------------------------------------ *
   * 卡片数据：归一化 / 取值 / 完成度 / 脏检查
   * ------------------------------------------------------------------ */

  /** 把后端 card 与 schema 对齐：补齐缺失分区 / 字段，矫正 tags / list / dialogs 类型。 */
  function normalizeWorkingCard(raw) {
    raw = raw && typeof raw === "object" ? raw : {};
    var out = clone(raw) || {};
    S.schema.forEach(function (section) {
      var sectionId = section.id;
      var rawSection = out[sectionId];
      var target = clone(rawSection && typeof rawSection === "object" ? rawSection : {}) || {};
      (section.fields || []).forEach(function (field) {
        var type = fieldType(field);
        var value = target[field.key];
        if (type === "tags" || type === "list") {
          target[field.key] = Array.isArray(value) ? value.filter(function (item) {
            return String(item == null ? "" : item).trim() !== "";
          }).map(function (item) {
            return String(item).trim();
          }) : splitTags(value);
        } else if (type === "dialogs") {
          target[field.key] = Array.isArray(value) ? value.map(normalizeDialog).filter(Boolean) : [];
        } else if (type === "select" || type === "textarea" || type === "text") {
          target[field.key] = value == null ? "" : String(value);
        } else if (value == null) {
          target[field.key] = "";
        }
      });
      out[sectionId] = target;
    });
    return out;
  }

  function normalizeDialog(item) {
    if (!item || typeof item !== "object") return null;
    var user = item.user != null ? item.user : item.role === "user" ? item.content : "";
    var character =
      item.character != null
        ? item.character
        : item.role === "assistant"
        ? item.content
        : item.reply != null
        ? item.reply
        : "";
    user = String(user == null ? "" : user);
    character = String(character == null ? "" : character);
    if (!user && !character) return null;
    return { user: user, character: character };
  }

  function emptyCardFromSchema() {
    var card = {};
    S.schema.forEach(function (section) {
      var data = {};
      (section.fields || []).forEach(function (field) {
        var type = fieldType(field);
        data[field.key] = type === "tags" || type === "list" || type === "dialogs" ? [] : "";
      });
      card[section.id] = data;
    });
    return card;
  }

  function getValue(sectionId, fieldKey) {
    var section = S.card && S.card[sectionId];
    return section ? section[fieldKey] : "";
  }

  function setValue(sectionId, fieldKey, value) {
    if (!S.card) S.card = {};
    if (!S.card[sectionId] || typeof S.card[sectionId] !== "object") S.card[sectionId] = {};
    S.card[sectionId][fieldKey] = value;
  }

  function completionOf(section) {
    var total = 0;
    var filled = 0;
    (section.fields || []).forEach(function (field) {
      total += 1;
      if (!isBlank(getValue(section.id, field.key))) filled += 1;
    });
    return { filled: filled, total: total };
  }

  function completionClass(stat) {
    if (!stat.total) return "is-empty";
    if (stat.filled >= stat.total) return "is-full";
    if (stat.filled > 0) return "is-half";
    return "is-empty";
  }

  function isFieldChanged(sectionId, fieldKey) {
    if (!S.draft) return false;
    var before = S.draft.before || {};
    var beforeSection = before[sectionId] || {};
    var a = JSON.stringify(getValue(sectionId, fieldKey));
    var b = JSON.stringify(beforeSection[fieldKey]);
    return a !== b;
  }

  function sectionHasDraft(sectionId) {
    return !!(S.draft && S.draft.sections && S.draft.sections.indexOf(sectionId) >= 0);
  }

  function computeDirty() {
    if (!S.card || !S.savedCard) return false;
    try {
      return JSON.stringify(S.card) !== JSON.stringify(S.savedCard);
    } catch (err) {
      return true;
    }
  }

  /* ------------------------------------------------------------------ *
   * HTML 片段构建
   * ------------------------------------------------------------------ */
  function metaSoul() {
    var app = S.app;
    var soul = (app && app.state && app.state.currentSoul) || null;
    if (soul && soul.soul_id === S.soulId) return soul;
    var souls = (app && app.state && app.state.souls) || [];
    for (var i = 0; i < souls.length; i++) {
      if (souls[i] && souls[i].soul_id === S.soulId) return souls[i];
    }
    return soul;
  }

  function identityValue(key) {
    var identity = (S.card && S.card.identity) || {};
    return String(identity[key] == null ? "" : identity[key]).trim();
  }

  function asideHtml() {
    var name = identityValue("name") || (metaSoul() && metaSoul().name) || S.soulId || "未命名角色";
    var tagline = identityValue("tagline") || (metaSoul() && metaSoul().archetype) || "";
    var nav = S.schema
      .map(function (section) {
        var stat = completionOf(section);
        return (
          '<button type="button" class="section-nav-item' +
          (S.activeSection === section.id ? " active" : "") +
          '" data-action="goto-section" data-section="' +
          esc(section.id) +
          '">' +
          '<span class="completion-dot ' +
          completionClass(stat) +
          '"></span>' +
          '<span class="grow">' +
          esc(section.title || section.id) +
          "</span>" +
          '<span class="text-xs muted num">' +
          stat.filled +
          "/" +
          stat.total +
          "</span>" +
          "</button>"
        );
      })
      .join("");

    return (
      '<aside class="char-aside">' +
      '<div class="card char-profile">' +
      '<span class="char-avatar is-clickable" data-action="upload-profile-avatar" data-tip="点击上传或更换头像图片" role="button" tabindex="0" aria-label="点击上传或更换头像图片">' +
      '<span class="avatar avatar-xl char-avatar-face" data-role="profile-avatar">' +
      renderAvatarInner(displayAvatarValue(), fallbackAvatarName()) +
      "</span>" +
      '<span class="char-avatar-hint" aria-hidden="true">更换头像</span>' +
      "</span>" +
      '<div class="char-name" data-role="profile-name">' +
      esc(name) +
      "</div>" +
      '<div class="char-tagline" data-role="profile-tagline">' +
      esc(tagline || "还没有一句话定位，给角色写一句吧") +
      "</div>" +
      '<span class="tag ' +
      (S.dirty ? "tag-primary" : "tag-muted") +
      '" data-role="profile-state">' +
      (S.dirty ? "有未保存修改" : "已同步") +
      "</span>" +
      "</div>" +
      '<div class="card"><div class="card-head"><div class="card-title">' +
      icon("list", 14) +
      '分区完成度<span class="badge-count">' +
      S.schema.length +
      "</span></div></div>" +
      '<div class="card-body tight"><div class="section-nav" data-role="section-nav">' +
      nav +
      "</div></div></div>" +
      '<div class="card"><div class="card-head"><div class="card-title">' +
      icon("wand", 14) +
      "操作</div></div>" +
      '<div class="card-body tight col">' +
      '<button class="btn btn-outline" type="button" data-action="ai-card">' +
      icon("wand", 14) +
      "AI 生成整卡</button>" +
      '<button class="btn btn-outline" type="button" data-action="import-persona">' +
      icon("download", 14) +
      "从 AstrBot 导入</button>" +
      '<button class="btn btn-outline" type="button" data-action="preview-prompt">' +
      icon("list", 14) +
      "Prompt 预览</button>" +
      '<button class="btn btn-ghost" type="button" data-action="more-menu">' +
      icon("dots", 14) +
      "新建 / 改 ID / 删除</button>" +
      "</div></div>" +
      "</aside>"
    );
  }

  function draftBannerHtml() {
    if (!S.draft) return "";
    var label = S.draft.kind === "card" ? "整张角色卡" : "「" + draftSectionTitle() + "」分区";
    return (
      '<div class="card" style="border-color:var(--primary)"><div class="card-body tight">' +
      '<div class="row-between wrap">' +
      '<div class="row">' +
      icon("wand", 15) +
      '<strong class="text-sm">AI 草稿已填入（' +
      esc(label) +
      "）</strong>" +
      '<span class="muted text-xs">青绿描边 = AI 改动过的字段</span>' +
      "</div>" +
      '<div class="btn-group">' +
      '<button class="btn btn-outline btn-sm" type="button" data-action="discard-draft">' +
      icon("close", 13) +
      "放弃草稿</button>" +
      '<button class="btn btn-outline btn-sm" type="button" data-action="adopt-draft">' +
      icon("check", 13) +
      "采用草稿</button>" +
      "</div></div></div></div>"
    );
  }

  function draftSectionTitle() {
    if (!S.draft || !S.draft.sections || !S.draft.sections.length) return "";
    var section = findSection(S.draft.sections[0]);
    return section ? section.title || section.id : S.draft.sections[0];
  }

  /** identity.avatar 图文混合控件：缩略图预览 + 上传 / 清除 + 文本输入框 */
  function avatarControlHtml(section, field, value) {
    var raw = String(value == null ? "" : value).trim();
    /* 仅本地压缩上传的 Data URL 隐藏原文；http(s) / 站点路径仍以文本形式可编辑 */
    var localImage = /^data:image\//i.test(raw);
    var imageMode = isImageAvatarValue(raw);
    var inputHtml;
    if (localImage) {
      /* 本地上传图片：输入框只读展示标签，绝不暴露 Base64 文本 */
      inputHtml =
        '<input class="input avatar-text-input is-image" type="text" value="已设置本地上传图片" readonly aria-label="头像已设置为本地图片" />';
    } else {
      inputHtml =
        '<input class="input avatar-text-input" type="text" data-section="' +
        esc(section.id) +
        '" data-field="' +
        esc(field.key) +
        '" data-field-type="text" maxlength="500" placeholder="' +
        esc(field.placeholder || "🌙 或 星，也可以粘贴图片 URL") +
        '" value="' +
        esc(raw) +
        '" />';
    }
    return (
      '<div class="avatar-picker avatar-field" data-avatar-mode="' +
      (localImage ? "local-image" : imageMode ? "image-url" : "text") +
      '">' +
      '<span class="avatar avatar-lg avatar-preview" data-role="avatar-field-preview">' +
      renderAvatarInner(raw, fallbackAvatarName()) +
      "</span>" +
      '<div class="avatar-field-main">' +
      '<div class="avatar-picker-actions">' +
      '<button class="btn btn-outline btn-sm" type="button" data-action="upload-avatar">' +
      icon("upload", 13) +
      "上传图片</button>" +
      '<button class="btn btn-ghost btn-sm" type="button" data-action="clear-avatar">' +
      icon("close", 13) +
      "清除图片</button>" +
      "</div>" +
      inputHtml +
      '<div class="field-help">支持 Emoji / 首字母 / 图片 URL；上传的本地图片会自动裁剪压缩，仅保存缩略图 Data URL（不展示原始文本）。</div>' +
      "</div></div>"
    );
  }

  function controlHtml(section, field) {
    var type = fieldType(field);
    var value = getValue(section.id, field.key);
    var readonly = isReadonly(field);
    if (section.id === "identity" && field.key === "avatar") {
      return avatarControlHtml(section, field, value);
    }
    var attrs =
      ' data-section="' +
      esc(section.id) +
      '" data-field="' +
      esc(field.key) +
      '" data-field-type="' +
      esc(type) +
      '"';
    var placeholder = field.placeholder ? ' placeholder="' + esc(field.placeholder) + '"' : "";
    var disabled = readonly ? " readonly" : "";

    if (type === "textarea") {
      return (
        '<textarea class="textarea" rows="3"' +
        attrs +
        placeholder +
        disabled +
        ">" +
        esc(value) +
        "</textarea>"
      );
    }
    if (type === "select") {
      var options = Array.isArray(field.options) ? field.options : [];
      var selectedValue = String(value == null ? "" : value);
      var html =
        '<select class="select"' + attrs + (readonly ? " disabled" : "") + ">" +
        '<option value="">— 请选择 —</option>';
      var matched = false;
      options.forEach(function (opt) {
        if (!opt || typeof opt !== "object") return;
        var optValue = String(opt.value == null ? "" : opt.value);
        var isSelected = optValue === selectedValue;
        if (isSelected) matched = true;
        html +=
          '<option value="' +
          esc(optValue) +
          '"' +
          (isSelected ? " selected" : "") +
          ">" +
          esc(opt.label != null ? opt.label : optValue) +
          "</option>";
      });
      /* 后端可能存了不在 options 里的旧值：保留一项，避免保存时被静默清空 */
      if (selectedValue && !matched) {
        html +=
          '<option value="' +
          esc(selectedValue) +
          '" selected>' +
          esc(selectedValue) +
          "（自定义）</option>";
      }
      return html + "</select>";
    }
    if (type === "tags") {
      var tags = Array.isArray(value) ? value : [];
      var chips = tags
        .map(function (tag, index) {
          return (
            '<span class="tag">' +
            esc(tag) +
            '<button class="tag-remove" type="button" data-action="remove-tag" data-section="' +
            esc(section.id) +
            '" data-field="' +
            esc(field.key) +
            '" data-index="' +
            index +
            '" aria-label="删除标签">' +
            icon("close", 9) +
            "</button></span>"
          );
        })
        .join("");
      return (
        '<div class="tag-editor" data-tag-editor="' +
        esc(section.id + "." + field.key) +
        '">' +
        chips +
        '<input class="tag-input" type="text" data-role="tag-input"' +
        attrs +
        (field.placeholder ? ' placeholder="' + esc(field.placeholder) + '"' : ' placeholder="输入后回车添加"') +
        (readonly ? " readonly" : "") +
        " /></div>"
      );
    }
    if (type === "list") {
      var items = Array.isArray(value) ? value : [];
      return (
        '<textarea class="textarea" rows="3" data-role="list-input"' +
        attrs +
        placeholder +
        disabled +
        ">" +
        esc(items.join("\n")) +
        "</textarea>" +
        '<div class="field-help">每行一条，保存时会自动整理为列表</div>'
      );
    }
    if (type === "dialogs") {
      var dialogs = Array.isArray(value) ? value : [];
      var rows = dialogs
        .map(function (dialog, index) {
          return (
            '<div class="dialog-row" data-dialog-index="' +
            index +
            '">' +
            '<input class="input input-sm" type="text" data-dialog="user" data-section="' +
            esc(section.id) +
            '" data-field="' +
            esc(field.key) +
            '" data-index="' +
            index +
            '" placeholder="用户的话" value="' +
            esc(dialog.user) +
            '" />' +
            '<div class="row">' +
            '<input class="input input-sm grow" type="text" data-dialog="character" data-section="' +
            esc(section.id) +
            '" data-field="' +
            esc(field.key) +
            '" data-index="' +
            index +
            '" placeholder="角色的回复" value="' +
            esc(dialog.character) +
            '" />' +
            '<button class="icon-btn btn-sm" type="button" data-action="remove-dialog" data-section="' +
            esc(section.id) +
            '" data-field="' +
            esc(field.key) +
            '" data-index="' +
            index +
            '" data-tip="删除这组对话" aria-label="删除这组对话">' +
            icon("trash", 13) +
            "</button></div></div>"
          );
        })
        .join("");
      return (
        '<div class="col" data-dialogs="' +
        esc(section.id + "." + field.key) +
        '" data-section="' +
        esc(section.id) +
        '" data-field="' +
        esc(field.key) +
        '">' +
        (rows || '<div class="hint">还没有示例对话，点下方按钮添加一组。</div>') +
        '<div><button class="btn btn-ghost btn-sm" type="button" data-action="add-dialog" data-section="' +
        esc(section.id) +
        '" data-field="' +
        esc(field.key) +
        '">' +
        icon("plus", 13) +
        "添加对话</button></div></div>"
      );
    }
    /* text */
    return (
      '<input class="input" type="text"' +
      attrs +
      placeholder +
      ' value="' +
      esc(value) +
      '"' +
      disabled +
      " />"
    );
  }

  function fieldHtml(section, field) {
    var type = fieldType(field);
    var isAvatarField = section.id === "identity" && field.key === "avatar";
    var span2 =
      isAvatarField || type === "textarea" || type === "tags" || type === "dialogs" || type === "list";
    var changed = isFieldChanged(section.id, field.key);
    var help = helpText(field);
    var label =
      '<span class="field-label">' +
      esc(field.label || field.key) +
      (isReadonly(field) ? '<span class="tag tag-muted">只读</span>' : "") +
      (help
        ? '<span class="hint" data-tip="' +
          esc(help) +
          '" tabindex="0" aria-label="字段说明">' +
          icon("info", 12) +
          "</span>"
        : "") +
      "</span>";
    return (
      '<div class="field' +
      (span2 ? " field-span-2" : "") +
      '" data-field-wrap="' +
      esc(section.id + "." + field.key) +
      '"' +
      (changed ? ' style="outline:2px solid var(--primary);outline-offset:3px;border-radius:8px"' : "") +
      ">" +
      label +
      controlHtml(section, field) +
      "</div>"
    );
  }

  function sectionHtml(section) {
    var stat = completionOf(section);
    var fields = (section.fields || []).map(function (field) {
      return fieldHtml(section, field);
    });
    return (
      '<section class="card-section' +
      (sectionHasDraft(section.id) ? " has-draft" : "") +
      '" data-section-card="' +
      esc(section.id) +
      '">' +
      '<div class="card-section-head">' +
      '<div class="card-section-title">' +
      esc(section.title || section.id) +
      '<span class="tag tag-muted" data-role="section-count">' +
      stat.filled +
      "/" +
      stat.total +
      "</span>" +
      (sectionHasDraft(section.id) ? '<span class="tag tag-primary">草稿</span>' : "") +
      "</div>" +
      '<div class="card-actions">' +
      '<button class="btn btn-ghost btn-sm" type="button" data-action="ai-section" data-section="' +
      esc(section.id) +
      '">' +
      icon("wand", 13) +
      "AI 补全</button>" +
      "</div></div>" +
      '<div class="card-section-body"><div class="section-grid">' +
      fields.join("") +
      "</div></div></section>"
    );
  }

  function emptySchemaHtml() {
    return (
      '<div class="card"><div class="empty">' +
      icon("alert", 32) +
      '<div class="empty-title">角色卡 schema 不可用</div>' +
      '<div class="empty-text">后端未返回 CARD_SCHEMA（core/character_card.py 或 GET card/schema 异常），请检查插件日志后刷新。</div>' +
      '<div class="row mt-12"><button class="btn btn-outline" type="button" data-action="reload">重新加载</button></div>' +
      "</div></div>"
    );
  }

  function savebarHtml() {
    return (
      '<div class="char-savebar">' +
      '<div class="row">' +
      '<span class="save-status' +
      (S.dirty ? " is-dirty" : "") +
      '" data-role="save-status">' +
      (S.dirty ? "未保存修改" : "所有更改已保存") +
      "</span>" +
      (S.draft ? '<span class="tag tag-primary">有 AI 草稿待确认</span>' : "") +
      "</div>" +
      '<div class="btn-group">' +
      '<button class="btn btn-ghost btn-sm" type="button" data-action="reload" data-tip="放弃本地修改并从后端重新加载">' +
      icon("refresh", 13) +
      "重新加载</button>" +
      '<button class="btn btn-primary btn-sm" type="button" data-action="save-card"' +
      (S.dirty ? "" : " disabled") +
      ">" +
      icon("save", 13) +
      "保存角色卡</button>" +
      "</div></div>"
    );
  }

  function pageHtml() {
    return (
      '<div class="page-head">' +
      '<h1 class="page-title">' +
      icon("character", 18) +
      "角色卡" +
      '<span class="tag tag-muted mono">' +
      esc(S.soulId || "未选择角色") +
      "</span>" +
      "</h1>" +
      '<div class="page-actions">' +
      '<button class="btn btn-ghost btn-sm" type="button" data-action="reload">' +
      icon("refresh", 14) +
      "重新加载</button>" +
      "</div></div>" +
      '<div class="char-layout">' +
      asideHtml() +
      '<div class="section-stack">' +
      draftBannerHtml() +
      (S.schema.length ? S.schema.map(sectionHtml).join("") : emptySchemaHtml()) +
      "</div></div>" +
      savebarHtml()
    );
  }

  function fieldHtmlByKeys(sectionId, fieldKey) {
    var section = findSection(sectionId);
    var field = findField(section, fieldKey);
    if (!section || !field) return "";
    return fieldHtml(section, field);
  }

  /* ------------------------------------------------------------------ *
   * 渲染 / 局部刷新
   * ------------------------------------------------------------------ */
  function paint() {
    if (!S.container || S.destroyed) return;
    var scroller = S.container.closest ? S.container.closest(".main") : null;
    var top = scroller ? scroller.scrollTop : 0;
    S.container.innerHTML = pageHtml();
    if (scroller) scroller.scrollTop = top;
  }

  function refreshField(sectionId, fieldKey) {
    if (!S.container) return null;
    var wrap = S.container.querySelector(
      '[data-field-wrap="' + sectionId + "." + fieldKey + '"]'
    );
    if (!wrap) return null;
    var container = document.createElement("div");
    container.innerHTML = fieldHtmlByKeys(sectionId, fieldKey);
    var next = container.firstChild;
    if (!next) return null;
    wrap.parentNode.replaceChild(next, wrap);
    return next;
  }

  function refreshNav() {
    if (!S.container) return;
    var nav = S.container.querySelector('[data-role="section-nav"]');
    if (!nav) return;
    var name = identityValue("name") || (metaSoul() && metaSoul().name) || S.soulId || "未命名角色";
    var tagline = identityValue("tagline") || (metaSoul() && metaSoul().archetype) || "";
    nav.innerHTML = asideNavItemsHtml();
    var nameEl = S.container.querySelector('[data-role="profile-name"]');
    if (nameEl) nameEl.textContent = name;
    var taglineEl = S.container.querySelector('[data-role="profile-tagline"]');
    if (taglineEl) taglineEl.textContent = tagline || "还没有一句话定位，给角色写一句吧";
    var avatarEl = S.container.querySelector('[data-role="profile-avatar"]');
    if (avatarEl) {
      avatarEl.innerHTML = renderAvatarInner(displayAvatarValue(), fallbackAvatarName());
    }
    var stateEl = S.container.querySelector('[data-role="profile-state"]');
    if (stateEl) {
      stateEl.textContent = S.dirty ? "有未保存修改" : "已同步";
      stateEl.className = "tag " + (S.dirty ? "tag-primary" : "tag-muted");
    }
    /* 分区卡片头的完成度计数 */
    S.schema.forEach(function (section) {
      var stat = completionOf(section);
      var badge = S.container.querySelector(
        '[data-section-card="' + section.id + '"] [data-role="section-count"]'
      );
      if (badge) {
        badge.textContent = stat.filled + "/" + stat.total;
        var dot = nav.querySelector(
          '[data-action="goto-section"][data-section="' + section.id + '"] .completion-dot'
        );
        if (dot) dot.className = "completion-dot " + completionClass(stat);
      }
    });
  }

  function asideNavItemsHtml() {
    return S.schema
      .map(function (section) {
        var stat = completionOf(section);
        return (
          '<button type="button" class="section-nav-item' +
          (S.activeSection === section.id ? " active" : "") +
          '" data-action="goto-section" data-section="' +
          esc(section.id) +
          '">' +
          '<span class="completion-dot ' +
          completionClass(stat) +
          '"></span>' +
          '<span class="grow">' +
          esc(section.title || section.id) +
          "</span>" +
          '<span class="text-xs muted num">' +
          stat.filled +
          "/" +
          stat.total +
          "</span></button>"
        );
      })
      .join("");
  }

  function refreshSavebar() {
    if (!S.container) return;
    var status = S.container.querySelector('[data-role="save-status"]');
    if (status) {
      status.textContent = S.dirty ? "未保存修改" : "所有更改已保存";
      status.classList.toggle("is-dirty", S.dirty);
    }
    var saveBtn = S.container.querySelector('[data-action="save-card"]');
    if (saveBtn) saveBtn.disabled = !S.dirty;
    refreshNav();
  }

  /** 局部刷新头像预览（画像卡大图 / identity.avatar 缩略图），不重建整个页面 */
  function updateAvatarPreviews() {
    if (!S.container) return;
    var fallback = fallbackAvatarName();
    var profileEl = S.container.querySelector('[data-role="profile-avatar"]');
    if (profileEl) profileEl.innerHTML = renderAvatarInner(displayAvatarValue(), fallback);
    var fieldEl = S.container.querySelector('[data-role="avatar-field-preview"]');
    if (fieldEl) fieldEl.innerHTML = renderAvatarInner(identityValue("avatar"), fallback);
  }

  /** 上传 / 更换角色头像：裁剪压缩后写入 identity.avatar（保存走既有 save-card） */
  function uploadAvatar() {
    var app = S.app;
    if (!app || typeof app.pickAvatarImage !== "function" || S._pickingAvatar) return;
    S._pickingAvatar = true;
    pickAvatarImage({ size: 160 })
      .then(function (dataUrl) {
        S._pickingAvatar = false;
        if (!dataUrl || S.destroyed || !S.container) return; /* 用户取消 / 页面已卸载 */
        if (!/^data:image\//i.test(dataUrl)) {
          app.toast("头像图片处理失败，请换一张图片重试", true);
          return;
        }
        if (dataUrl.length > 120000) {
          app.toast("头像图片体积过大（超过 120000 字符），请换一张更小的图片", true);
          return;
        }
        setValue("identity", "avatar", dataUrl);
        S.dirty = computeDirty();
        paint();
        app.toast("头像已更新，记得点「保存角色卡」");
      })
      .catch(function (err) {
        S._pickingAvatar = false;
        app.toast("头像处理失败：" + ((err && (err.message || err)) || "未知错误"), true);
      });
  }

  /** 清除头像图片（回空字符串，保存后生效） */
  function clearAvatar() {
    if (!S.card) return;
    setValue("identity", "avatar", "");
    S.dirty = computeDirty();
    paint();
    if (S.app) S.app.toast("已清除头像图片，记得点「保存角色卡」");
  }

  function scrollToSection(sectionId) {
    if (!S.container) return;
    var el = S.container.querySelector('[data-section-card="' + sectionId + '"]');
    if (!el) return;
    S.activeSection = sectionId;
    if (S.container.querySelectorAll) {
      S.container.querySelectorAll(".section-nav-item").forEach(function (item) {
        item.classList.toggle("active", item.getAttribute("data-section") === sectionId);
      });
    }
    if (el.scrollIntoView) {
      try {
        el.scrollIntoView({ behavior: "smooth", block: "start" });
      } catch (err) {
        el.scrollIntoView();
      }
    }
  }

  /* ------------------------------------------------------------------ *
   * 数据加载
   * ------------------------------------------------------------------ */
  async function loadData(app) {
    var soulId = (app.state && app.state.currentSoulId) || "";
    var data = await app.apiGet("card/schema", soulId ? { soul_id: soulId } : {});
    if (!data || typeof data !== "object") throw new Error("GET card/schema 返回为空");
    var schema = Array.isArray(data.schema) ? data.schema : [];
    if (!schema.length && app.state && Array.isArray(app.state.cardSchema)) {
      schema = app.state.cardSchema;
    }
    S.schema = schema;
    S.soulId = data.soul_id || soulId;
    S.card = normalizeWorkingCard(data.card || {});
    if (!schema.length) {
      S.card = data.card && typeof data.card === "object" ? clone(data.card) : {};
    }
    S.savedCard = clone(S.card);
    S.dirty = false;
    S.draft = null;
    S.activeSection = schema.length ? schema[0].id : "";
    if (S.soulId) {
      app.state.currentSoulId = S.soulId;
    }
  }

  async function reloadAll(showToast) {
    if (!S.app || S.busy) return;
    if (computeDirty()) {
      var discard = await S.app.confirm({
        title: "放弃未保存修改？",
        message: "当前角色卡有尚未保存的修改，重新加载会从后端覆盖这些修改并丢失。",
        danger: true,
        confirmLabel: "放弃并重新加载",
        cancelLabel: "取消"
      });
      if (!discard) return;
    }
    S.busy = true;
    try {
      paint();
      await loadData(S.app);
      paint();
      if (showToast) S.app.toast("已重新加载角色卡");
    } catch (err) {
      renderError(err);
      if (showToast) S.app.toast("重新加载失败：" + (err.message || err), true);
    } finally {
      S.busy = false;
    }
  }

  function renderError(err) {
    if (!S.container) return;
    S.container.innerHTML =
      '<div class="page-head"><h1 class="page-title">' +
      icon("character", 18) +
      "角色卡</h1></div>" +
      '<div class="card"><div class="empty">' +
      icon("alert", 32) +
      '<div class="empty-title">角色卡加载失败</div>' +
      '<div class="empty-text">' +
      esc((err && (err.message || err)) || "未知错误") +
      "</div>" +
      '<div class="row mt-12"><button class="btn btn-outline" type="button" data-action="reload">重试</button></div>' +
      "</div></div>";
  }

  /* ------------------------------------------------------------------ *
   * AI 生成 / 草稿
   * ------------------------------------------------------------------ */
  async function aiGenerateSection(sectionId, buttonEl) {
    var app = S.app;
    var section = findSection(sectionId);
    if (!app || !section) return;
    if (S.busy) {
      app.toast("上一个操作还在进行中，请稍候", true);
      return;
    }
    S.busy = true;
    if (buttonEl) buttonEl.classList.add("is-loading");
    try {
      var data = await app.apiPost("card/ai_generate", {
        soul_id: S.soulId,
        section: sectionId
      });
      var draftCard = data && data.draft_card;
      var draftSection = draftCard && draftCard[sectionId];
      if (!draftSection || typeof draftSection !== "object") {
        throw new Error("AI 未返回该分区的有效草稿");
      }
      var before = clone(S.card);
      var normalized = normalizeWorkingCard(draftCard);
      var changed = 0;
      (section.fields || []).forEach(function (field) {
        if (!Object.prototype.hasOwnProperty.call(draftSection, field.key)) return;
        var oldValue = JSON.stringify(getValue(sectionId, field.key));
        setValue(sectionId, field.key, normalized[sectionId][field.key]);
        if (JSON.stringify(getValue(sectionId, field.key)) !== oldValue) changed += 1;
      });
      if (!changed) {
        app.toast("AI 返回的「" + (section.title || sectionId) + "」内容与当前一致，未产生改动");
        return;
      }
      S.draft = { kind: "section", sections: [sectionId], before: before };
      S.dirty = computeDirty();
      paint();
      scrollToSection(sectionId);
      app.toast("已生成「" + (section.title || sectionId) + "」分区草稿，请采用或放弃");
    } catch (err) {
      app.toast("AI 补全失败：" + (err.message || err), true);
    } finally {
      S.busy = false;
      if (buttonEl) buttonEl.classList.remove("is-loading");
    }
  }

  function openAiCardModal() {
    var app = S.app;
    if (!app || !S.schema.length) return;
    var body =
      '<div class="field"><span class="field-label">种子描述（可选）</span>' +
      '<textarea class="textarea" rows="4" data-role="seed-text" placeholder="例如：一个住在隔壁、爱熬夜刷 B 站的社畜狐狸，嘴硬心软，说话带点冷幽默"></textarea>' +
      '<div class="field-help">留空则让主模型在当前角色卡基础上自由扩写；生成结果先作为草稿填入，不会立即保存。</div></div>';
    app.modal({
      title: "AI 生成整张角色卡",
      body: body,
      actions: [
        { label: "取消" },
        {
          label: "开始生成",
          primary: true,
          icon: "wand",
          onClick: async function (modalApp, bodyEl) {
            var seedEl = bodyEl.querySelector('[data-role="seed-text"]');
            var seed = seedEl ? String(seedEl.value || "").trim() : "";
            await aiGenerateWholeCard(seed);
          }
        }
      ]
    });
  }

  async function aiGenerateWholeCard(seedText) {
    var app = S.app;
    if (!app) return;
    if (S.busy) {
      app.toast("上一个操作还在进行中，请稍候", true);
      return;
    }
    S.busy = true;
    try {
      var data = await app.apiPost("card/ai_generate", {
        soul_id: S.soulId,
        seed_text: seedText || ""
      });
      if (!data || !data.draft_card || typeof data.draft_card !== "object") {
        throw new Error("AI 未返回有效的整卡草稿");
      }
      var before = clone(S.card);
      var normalized = normalizeWorkingCard(data.draft_card);
      var sections = [];
      S.schema.forEach(function (section) {
        var draftSection = data.draft_card[section.id];
        if (!draftSection || typeof draftSection !== "object") return;
        var changed = false;
        (section.fields || []).forEach(function (field) {
          if (!Object.prototype.hasOwnProperty.call(draftSection, field.key)) return;
          var oldValue = JSON.stringify(getValue(section.id, field.key));
          setValue(section.id, field.key, normalized[section.id][field.key]);
          if (JSON.stringify(getValue(section.id, field.key)) !== oldValue) changed = true;
        });
        if (changed) sections.push(section.id);
      });
      if (!sections.length) {
        app.toast("AI 生成的角色卡内容与当前一致，未产生改动");
        return;
      }
      S.draft = { kind: "card", sections: sections, before: before };
      S.dirty = computeDirty();
      paint();
      scrollToSection(sections[0]);
      app.toast("已生成整卡草稿（" + sections.length + " 个分区有改动），请采用或放弃");
    } catch (err) {
      app.toast("AI 生成整卡失败：" + (err.message || err), true);
      throw err;
    } finally {
      S.busy = false;
    }
  }

  function adoptDraft() {
    if (!S.draft) return;
    S.draft = null;
    S.dirty = computeDirty();
    paint();
    if (S.app) S.app.toast("已采用 AI 草稿，别忘了点「保存角色卡」");
  }

  function discardDraft() {
    if (!S.draft) return;
    var before = S.draft.before || {};
    (S.draft.sections || []).forEach(function (sectionId) {
      if (before[sectionId] && typeof before[sectionId] === "object") {
        S.card[sectionId] = clone(before[sectionId]);
      } else if (S.card) {
        delete S.card[sectionId];
      }
    });
    S.draft = null;
    S.dirty = computeDirty();
    paint();
    if (S.app) S.app.toast("已放弃 AI 草稿");
  }

  /* ------------------------------------------------------------------ *
   * 保存 / 预览
   * ------------------------------------------------------------------ */
  async function saveCard() {
    var app = S.app;
    if (!app) return;
    if (!S.dirty) {
      app.toast("没有需要保存的修改");
      return;
    }
    if (S.busy) {
      app.toast("上一个操作还在进行中，请稍候", true);
      return;
    }
    S.busy = true;
    var container = S.container;
    var saveBtn = container ? container.querySelector('[data-action="save-card"]') : null;
    if (saveBtn) saveBtn.classList.add("is-loading");
    try {
      var data = await app.apiPost("card/save", {
        soul_id: S.soulId,
        card: S.card
      });
      var serverCard = data && data.soul && data.soul.card ? data.soul.card : S.card;
      S.card = normalizeWorkingCard(serverCard);
      S.savedCard = clone(S.card);
      S.draft = null;
      S.dirty = false;
      if (data && Array.isArray(data.souls)) {
        app.applySouls(data.souls, data.soul || null);
      }
      paint();
      app.toast("角色卡已保存");
    } catch (err) {
      app.toast("保存角色卡失败：" + (err.message || err), true);
    } finally {
      S.busy = false;
      var nextBtn = S.container ? S.container.querySelector('[data-action="save-card"]') : null;
      if (nextBtn) nextBtn.classList.remove("is-loading");
    }
  }

  function openPromptPreview() {
    var app = S.app;
    if (!app) return;
    app.apiPost("card/preview_prompt", { soul_id: S.soulId, card: S.card }).then(
      function (data) {
        var prompt = (data && data.prompt) || "";
        var chars = data && data.chars != null ? data.chars : prompt.length;
        var body =
          '<div class="row-between mb-8"><span class="text-sm muted">渲染内容（最终注入 System Prompt 的 Persona 段）</span>' +
          '<span class="tag tag-primary num">' +
          esc(chars) +
          " 字</span></div>" +
          '<div class="code-block prompt-preview"><div class="code-head"><span>render_persona_prompt(card)</span>' +
          '<button class="btn btn-ghost btn-xs" type="button" data-action="copy-prompt">' +
          icon("copy", 12) +
          "复制</button></div>" +
          '<pre class="code-body">' +
          esc(prompt) +
          "</pre></div>";
        app.modal({
          title: "Prompt 预览",
          wide: true,
          body: body,
          actions: [{ label: "关闭" }],
          onMount: function (bodyEl, modalApp) {
            var copyBtn = bodyEl.querySelector('[data-action="copy-prompt"]');
            if (!copyBtn) return;
            copyBtn.addEventListener("click", function () {
              function fallbackCopy() {
                var pre = bodyEl.querySelector(".code-body");
                if (!pre || !window.getSelection || !document.createRange) return;
                var range = document.createRange();
                range.selectNodeContents(pre);
                var sel = window.getSelection();
                sel.removeAllRanges();
                sel.addRange(range);
                try {
                  document.execCommand("copy");
                } catch (err) {
                  /* ignore */
                }
              }
              if (navigator.clipboard && navigator.clipboard.writeText) {
                navigator.clipboard.writeText(prompt).then(
                  function () {
                    modalApp.toast("Prompt 已复制到剪贴板");
                  },
                  fallbackCopy
                );
              } else {
                fallbackCopy();
                modalApp.toast("已选中 Prompt 文本，请手动复制");
              }
            });
          }
        });
      },
      function (err) {
        app.toast("Prompt 预览失败：" + (err.message || err), true);
      }
    );
  }

  /* ------------------------------------------------------------------ *
   * AstrBot Persona 导入
   * ------------------------------------------------------------------ */
  function openImportModal() {
    var app = S.app;
    if (!app) return;
    var personas = (app.state && app.state.nativePersonas) || [];
    if (!personas.length) {
      app.modal({
        title: "从 AstrBot 导入",
        body:
          '<div class="empty">' +
          icon("download", 30) +
          '<div class="empty-title">没有可导入的原生 Persona</div>' +
          '<div class="empty-text">请先在 AstrBot WebUI → 人格配置里创建 Persona，然后刷新本页重试。</div></div>',
        actions: [{ label: "关闭" }]
      });
      return;
    }
    var options = personas
      .map(function (persona) {
        var name = persona && persona.persona_name ? persona.persona_name : "";
        return '<option value="' + esc(name) + '">' + esc(name) + "</option>";
      })
      .join("");
    var first = personas[0] && personas[0].persona_name ? String(personas[0].persona_name) : "";
    var avatarState = { value: "", picking: false };
    var body =
      '<div class="field"><span class="field-label">选择 AstrBot 原生 Persona</span>' +
      '<select class="select" data-role="persona-select">' +
      options +
      "</select>" +
      '<div class="field-help">导入会新建一个角色卡，并把 Persona 原文记录到「补充设定 → 原生人设原文」。</div></div>' +
      '<div class="field"><span class="field-label">角色头像（可选）</span>' +
      avatarPickerHtml(
        "import-avatar",
        first,
        "支持本地图片，自动居中裁剪压缩为 160×160；未上传时显示所选 Persona 名称首字。"
      ) +
      "</div>" +
      '<div class="field"><span class="field-label">目标 Soul ID</span>' +
      '<input class="input mono" type="text" data-role="target-soul" value="' +
      esc(first) +
      '" placeholder="例如：default_soul" />' +
      '<div class="field-help">同 ID 会覆盖已有角色的角色卡，请确认后再导入。</div></div>' +
      '<div class="switch-row"><div class="switch-row-text">' +
      '<span class="switch-row-title">AI 生成完整角色卡</span>' +
      '<span class="switch-row-desc">用主模型把 Persona 原文扩写成 10 分区角色卡（失败时退回基础卡）</span></div>' +
      '<label class="switch"><input type="checkbox" data-role="ai-card" /><span class="switch-track"></span></label></div>' +
      '<div class="switch-row"><div class="switch-row-text">' +
      '<span class="switch-row-title">同时生成行动卡 / 奇遇卡</span>' +
      '<span class="switch-row-desc">用决策模型扩写该角色的 Roguelite 卡池</span></div>' +
      '<label class="switch"><input type="checkbox" data-role="auto-cards" checked /><span class="switch-track"></span></label></div>';
    app.modal({
      title: "从 AstrBot 导入 Persona",
      body: body,
      actions: [
        { label: "取消" },
        {
          label: "导入",
          primary: true,
          icon: "download",
          onClick: async function (modalApp, bodyEl) {
            var selectEl = bodyEl.querySelector('[data-role="persona-select"]');
            var targetEl = bodyEl.querySelector('[data-role="target-soul"]');
            var aiEl = bodyEl.querySelector('[data-role="ai-card"]');
            var autoEl = bodyEl.querySelector('[data-role="auto-cards"]');
            var personaName = selectEl ? String(selectEl.value || "").trim() : "";
            var targetSoulId = targetEl ? String(targetEl.value || "").trim() : "";
            if (!personaName) {
              modalApp.toast("请选择要导入的 Persona", true);
              return false;
            }
            var data = await modalApp.apiPost("souls/import_persona", {
              persona_name: personaName,
              target_soul_id: targetSoulId || personaName,
              auto_generate_cards: !!(autoEl && autoEl.checked),
              ai_generate_card: !!(aiEl && aiEl.checked),
              avatar: String(avatarState.value || "").trim()
            });
            if (data && Array.isArray(data.souls)) {
              modalApp.applySouls(data.souls, data.soul || null);
            }
            modalApp.toast(
              "已导入 Persona：" + personaName + (aiEl && aiEl.checked ? "（AI 整卡生成中）" : "")
            );
            setTimeout(function () {
              modalApp.refresh({ silent: true });
            }, 60);
            return true;
          }
        }
      ],
      onMount: function (bodyEl) {
        var selectEl = bodyEl.querySelector('[data-role="persona-select"]');
        var targetEl = bodyEl.querySelector('[data-role="target-soul"]');
        bindAvatarPicker(bodyEl, "import-avatar", avatarState, function () {
          return selectEl ? String(selectEl.value || "").trim() || first : first;
        });
        if (selectEl && targetEl) {
          selectEl.addEventListener("change", function () {
            targetEl.value = selectEl.value;
            avatarState.render();
          });
        }
      }
    });
  }

  /* ------------------------------------------------------------------ *
   * 新建 / 改 ID / 删除
   * ------------------------------------------------------------------ */
  function openMoreMenu() {
    var app = S.app;
    if (!app) return;
    var soulCount = (app.state && Array.isArray(app.state.souls) && app.state.souls.length) || 0;
    var canDelete = soulCount > 1;
    var body =
      '<div class="col">' +
      '<button class="btn btn-outline" type="button" data-menu-action="create">' +
      icon("plus", 14) +
      "新建角色卡</button>" +
      '<button class="btn btn-outline" type="button" data-menu-action="rename">' +
      icon("edit", 14) +
      "修改 Soul ID</button>" +
      '<button class="btn btn-danger" type="button" data-menu-action="delete"' +
      (canDelete ? "" : " disabled") +
      ' data-tip="' +
      (canDelete ? "删除当前角色的角色卡、日程与关联记忆" : "至少保留一个角色") +
      '">' +
      icon("trash", 14) +
      "删除当前角色</button>" +
      "</div>" +
      '<div class="field-help mt-12">当前 Soul ID：<span class="mono">' +
      esc(S.soulId) +
      "</span>" +
      (canDelete ? "" : '<br />当前只有一个角色，至少保留一个后才能删除。') +
      "</div>";
    app.modal({
      title: "更多操作",
      body: body,
      onMount: function (bodyEl, modalApp) {
        bodyEl.querySelectorAll("[data-menu-action]").forEach(function (btn) {
          btn.addEventListener("click", function () {
            var action = btn.getAttribute("data-menu-action");
            modalApp.closeModal();
            if (action === "create") openCreateModal();
            else if (action === "rename") openRenameModal();
            else if (action === "delete") {
              if (!canDelete) {
                modalApp.toast("至少保留一个角色", true);
                return;
              }
              confirmDeleteSoul();
            }
          });
        });
      }
    });
  }

  function openCreateModal() {
    var app = S.app;
    if (!app) return;
    var avatarState = { value: "", picking: false };
    var body =
      '<div class="field"><span class="field-label">Soul ID <span class="req">*</span></span>' +
      '<input class="input mono" type="text" data-role="new-soul-id" placeholder="例如：soul_jiuhu" />' +
      '<div class="field-help">唯一标识，创建后可通过「修改 Soul ID」重命名；建议使用 ASCII 字符。</div></div>' +
      '<div class="field"><span class="field-label">名字（可选）</span>' +
      '<input class="input" type="text" data-role="new-soul-name" placeholder="例如：酒狐" />' +
      '<div class="field-help">留空则使用 Soul ID 作为名字，之后可在角色卡里修改。</div></div>' +
      '<div class="field"><span class="field-label">角色头像（可选）</span>' +
      avatarPickerHtml(
        "create-avatar",
        "?",
        "支持本地图片，自动居中裁剪压缩为 160×160；未上传时显示名字首字。"
      ) +
      "</div>";
    app.modal({
      title: "新建角色卡",
      body: body,
      actions: [
        { label: "取消" },
        {
          label: "创建",
          primary: true,
          icon: "plus",
          onClick: async function (modalApp, bodyEl) {
            var idEl = bodyEl.querySelector('[data-role="new-soul-id"]');
            var nameEl = bodyEl.querySelector('[data-role="new-soul-name"]');
            var newId = idEl ? String(idEl.value || "").trim() : "";
            var newName = nameEl ? String(nameEl.value || "").trim() : "";
            if (!newId) {
              modalApp.toast("Soul ID 不能为空", true);
              return false;
            }
            if (!/^[^\/\\\s]+$/.test(newId)) {
              modalApp.toast("Soul ID 不能包含空格或斜杠", true);
              return false;
            }
            var card = emptyCardFromSchema();
            card.identity = card.identity || {};
            card.identity.name = newName || newId;
            card.identity.avatar =
              String(avatarState.value || "").trim() || (newName || newId).slice(0, 1);
            var data = await modalApp.apiPost("souls/save", { soul_id: newId, card: card });
            if (data && Array.isArray(data.souls)) {
              modalApp.applySouls(data.souls, data.soul || null);
            }
            modalApp.toast("已创建角色 " + newId);
            setTimeout(function () {
              modalApp.refresh({ silent: true });
            }, 60);
            return true;
          }
        }
      ],
      onMount: function (bodyEl) {
        var nameEl = bodyEl.querySelector('[data-role="new-soul-name"]');
        bindAvatarPicker(bodyEl, "create-avatar", avatarState, function () {
          var name = nameEl ? String(nameEl.value || "").trim() : "";
          return name || "?";
        });
        if (nameEl) {
          nameEl.addEventListener("input", function () {
            avatarState.render();
          });
        }
      }
    });
  }

  function openRenameModal() {
    var app = S.app;
    if (!app) return;
    var body =
      '<div class="field"><span class="field-label">当前 Soul ID</span>' +
      '<input class="input mono" type="text" value="' +
      esc(S.soulId) +
      '" disabled /></div>' +
      '<div class="field"><span class="field-label">新的 Soul ID <span class="req">*</span></span>' +
      '<input class="input mono" type="text" data-role="new-soul-id" value="' +
      esc(S.soulId + "_copy") +
      '" />' +
      '<div class="field-help">重命名会级联更新 UMO 绑定、记忆、冲浪记录与日程缓存。</div></div>';
    app.modal({
      title: "修改 Soul ID",
      body: body,
      actions: [
        { label: "取消" },
        {
          label: "重命名",
          primary: true,
          icon: "save",
          onClick: async function (modalApp, bodyEl) {
            var input = bodyEl.querySelector('[data-role="new-soul-id"]');
            var newId = input ? String(input.value || "").trim() : "";
            if (!newId) {
              modalApp.toast("新的 Soul ID 不能为空", true);
              return false;
            }
            if (newId === S.soulId) {
              modalApp.toast("新的 Soul ID 与当前相同", true);
              return false;
            }
            if (!/^[^\/\\\s]+$/.test(newId)) {
              modalApp.toast("Soul ID 不能包含空格或斜杠", true);
              return false;
            }
            var data = await modalApp.apiPost("souls/rename", {
              old_soul_id: S.soulId,
              new_soul_id: newId
            });
            if (data && Array.isArray(data.souls)) {
              modalApp.applySouls(data.souls, data.soul || null);
            }
            modalApp.toast("Soul ID 已修改为 " + newId);
            setTimeout(function () {
              modalApp.refresh({ silent: true });
            }, 60);
            return true;
          }
        }
      ]
    });
  }

  function confirmDeleteSoul() {
    var app = S.app;
    if (!app) return;
    app
      .confirm({
        title: "删除角色",
        message:
          "确定删除角色「" +
          (identityValue("name") || S.soulId) +
          "」(" +
          S.soulId +
          ") 吗？该角色的角色卡、日程卡池、记忆与冲浪记录都会一并删除，且不可恢复。",
        danger: true,
        confirmLabel: "删除角色",
        onConfirm: async function () {
          var data = await app.apiPost("souls/delete", { soul_id: S.soulId });
          if (data && Array.isArray(data.souls)) {
            app.applySouls(data.souls, data.souls[0] || null);
          }
          app.toast("已删除角色 " + S.soulId);
          setTimeout(function () {
            app.refresh({ silent: true });
          }, 60);
        }
      })
      .then(function (confirmed) {
        if (!confirmed) return;
      });
  }

  /* ------------------------------------------------------------------ *
   * 事件处理
   * ------------------------------------------------------------------ */
  function handleInput(event) {
    var el = event.target;
    if (!el || !el.getAttribute) return;
    if (el.getAttribute("data-dialog")) return; /* dialogs 由 handleDialogInput 处理 */
    var sectionId = el.getAttribute("data-section");
    var fieldKey = el.getAttribute("data-field");
    if (!sectionId || !fieldKey) return;
    var type = el.getAttribute("data-field-type") || "text";
    if (type === "tags") return; /* tags 由 keydown 处理 */
    if (type === "dialogs") return; /* dialogs 由 data-dialog 处理 */
    var value = el.value;
    if (type === "list") value = splitLines(value);
    setValue(sectionId, fieldKey, value);
    S.dirty = computeDirty();
    if (sectionId === "identity" && fieldKey === "avatar") updateAvatarPreviews();
    refreshSavebar();
  }

  function handleDialogInput(event) {
    var el = event.target;
    if (!el || !el.getAttribute) return;
    var dialogRole = el.getAttribute("data-dialog");
    if (!dialogRole) return;
    var sectionId = el.getAttribute("data-section");
    var fieldKey = el.getAttribute("data-field");
    var index = parseInt(el.getAttribute("data-index"), 10);
    var list = getValue(sectionId, fieldKey);
    if (!Array.isArray(list) || !list[index]) return;
    list[index][dialogRole] = String(el.value || "");
    S.dirty = computeDirty();
    refreshSavebar();
  }

  function addTag(inputEl) {
    var sectionId = inputEl.getAttribute("data-section");
    var fieldKey = inputEl.getAttribute("data-field");
    var tags = splitTags(inputEl.value);
    if (!tags.length) return;
    var list = getValue(sectionId, fieldKey);
    if (!Array.isArray(list)) list = [];
    tags.forEach(function (tag) {
      if (list.indexOf(tag) < 0) list.push(tag);
    });
    setValue(sectionId, fieldKey, list);
    S.dirty = computeDirty();
    refreshField(sectionId, fieldKey);
    refreshSavebar();
    var selector =
      '[data-tag-editor="' + sectionId + "." + fieldKey + '"] .tag-input';
    var nextInput = S.container ? S.container.querySelector(selector) : null;
    if (nextInput) {
      try {
        nextInput.focus();
      } catch (err) {
        /* ignore */
      }
    }
  }

  function removeTag(buttonEl) {
    var sectionId = buttonEl.getAttribute("data-section");
    var fieldKey = buttonEl.getAttribute("data-field");
    var index = parseInt(buttonEl.getAttribute("data-index"), 10);
    var list = getValue(sectionId, fieldKey);
    if (!Array.isArray(list)) return;
    list.splice(index, 1);
    setValue(sectionId, fieldKey, list);
    S.dirty = computeDirty();
    refreshField(sectionId, fieldKey);
    refreshSavebar();
  }

  function addDialog(buttonEl) {
    var sectionId = buttonEl.getAttribute("data-section");
    var fieldKey = buttonEl.getAttribute("data-field");
    var list = getValue(sectionId, fieldKey);
    if (!Array.isArray(list)) list = [];
    list.push({ user: "", character: "" });
    setValue(sectionId, fieldKey, list);
    S.dirty = computeDirty();
    var wrap = refreshField(sectionId, fieldKey);
    refreshSavebar();
    if (wrap) {
      var inputs = wrap.querySelectorAll("[data-dialog]");
      if (inputs.length) {
        try {
          inputs[inputs.length - 2].focus();
        } catch (err) {
          /* ignore */
        }
      }
    }
  }

  function removeDialog(buttonEl) {
    var sectionId = buttonEl.getAttribute("data-section");
    var fieldKey = buttonEl.getAttribute("data-field");
    var index = parseInt(buttonEl.getAttribute("data-index"), 10);
    var list = getValue(sectionId, fieldKey);
    if (!Array.isArray(list)) return;
    list.splice(index, 1);
    setValue(sectionId, fieldKey, list);
    S.dirty = computeDirty();
    refreshField(sectionId, fieldKey);
    refreshSavebar();
  }

  function handleKeydown(event) {
    var el = event.target;
    if (!el || !el.getAttribute) return;
    /* 左侧头像可点击上传：键盘 Enter / 空格等价于点击 */
    if (
      (event.key === "Enter" || event.key === " " || event.key === "Spacebar") &&
      el.getAttribute("data-action") === "upload-profile-avatar"
    ) {
      event.preventDefault();
      uploadAvatar();
      return;
    }
    var type = el.getAttribute("data-field-type");
    if (type !== "tags") return;
    var key = event.key;
    if (key === "Enter" || key === "," || key === "，" || key === "、" || key === "/") {
      event.preventDefault();
      addTag(el);
      return;
    }
    if (key === "Backspace" && !el.value) {
      var sectionId = el.getAttribute("data-section");
      var fieldKey = el.getAttribute("data-field");
      var list = getValue(sectionId, fieldKey);
      if (Array.isArray(list) && list.length) {
        event.preventDefault();
        list.pop();
        setValue(sectionId, fieldKey, list);
        S.dirty = computeDirty();
        refreshField(sectionId, fieldKey);
        refreshSavebar();
        var nextInput = S.container
          ? S.container.querySelector(
              '[data-tag-editor="' + sectionId + "." + fieldKey + '"] .tag-input'
            )
          : null;
        if (nextInput) nextInput.focus();
      }
    }
  }

  function handleClick(event) {
    var target = event.target;
    if (!target || !target.closest) return;
    var actionEl = target.closest("[data-action]");
    if (!actionEl) return;
    var action = actionEl.getAttribute("data-action");
    var app = S.app;
    if (!app) return;

    if (action === "reload") {
      reloadAll(true);
    } else if (action === "goto-section") {
      scrollToSection(actionEl.getAttribute("data-section"));
    } else if (action === "ai-section") {
      aiGenerateSection(actionEl.getAttribute("data-section"), actionEl);
    } else if (action === "ai-card") {
      openAiCardModal();
    } else if (action === "import-persona") {
      openImportModal();
    } else if (action === "preview-prompt") {
      openPromptPreview();
    } else if (action === "more-menu") {
      openMoreMenu();
    } else if (action === "save-card") {
      saveCard();
    } else if (action === "adopt-draft") {
      adoptDraft();
    } else if (action === "discard-draft") {
      discardDraft();
    } else if (action === "remove-tag") {
      removeTag(actionEl);
    } else if (action === "add-dialog") {
      addDialog(actionEl);
    } else if (action === "remove-dialog") {
      removeDialog(actionEl);
    } else if (action === "upload-profile-avatar" || action === "upload-avatar") {
      uploadAvatar();
    } else if (action === "clear-avatar") {
      clearAvatar();
    }
  }

  function bindEvents(container, app) {
    container.addEventListener("click", handleClick);
    container.addEventListener("input", function (event) {
      handleInput(event);
      handleDialogInput(event);
    });
    container.addEventListener("change", function (event) {
      var el = event.target;
      if (el && el.getAttribute && el.getAttribute("data-field") && !el.getAttribute("data-dialog")) {
        handleInput(event);
      }
    });
    container.addEventListener("keydown", handleKeydown);
    container.addEventListener(
      "focusout",
      function (event) {
        var el = event.target;
        if (!el || !el.getAttribute || el.getAttribute("data-field-type") !== "tags" || !el.value) {
          return;
        }
        /* 元素已从 DOM 移除（页面重绘 / 保存）时不要再回写数据 */
        if (el.isConnected === false) return;
        /* 在同一个标签编辑器内部点击（例如点 ×）时不要把输入框内容当新标签 */
        var editor = el.closest ? el.closest("[data-tag-editor]") : null;
        if (editor && event.relatedTarget && editor.contains(event.relatedTarget)) return;
        addTag(el);
      },
      true
    );
    /* 保存快捷键 Ctrl/Cmd + S */
    S._keydownHandler = function (event) {
      if (!(event.metaKey || event.ctrlKey) || String(event.key).toLowerCase() !== "s") return;
      if (!S.container || S.destroyed) return;
      if (S.app && S.app.route !== "character") return;
      event.preventDefault();
      if (S.dirty) saveCard();
      else if (S.app) S.app.toast("没有需要保存的修改");
    };
    document.addEventListener("keydown", S._keydownHandler);
  }

  /* ------------------------------------------------------------------ *
   * 生命周期
   * ------------------------------------------------------------------ */
  async function render(container, app) {
    S.app = app;
    S.container = container;
    S.destroyed = false;
    S.busy = false;
    S.draft = null;
    S.schema = [];
    S.card = null;
    S.savedCard = null;
    S.dirty = false;
    S.activeSection = "";
    S._pickingAvatar = false;
    S.soulId = (app.state && app.state.currentSoulId) || "";

    container.innerHTML =
      '<div class="loading"><span class="spinner"></span><span>加载角色卡…</span></div>';
    bindEvents(container, app);
    try {
      await loadData(app);
      paint();
    } catch (err) {
      console.error("[OnlyYou] 角色卡页渲染失败：", err);
      renderError(err);
    }
  }

  function destroy() {
    S.destroyed = true;
    if (S._keydownHandler) {
      document.removeEventListener("keydown", S._keydownHandler);
      S._keydownHandler = null;
    }
    S.app = null;
    S.container = null;
    S.card = null;
    S.savedCard = null;
    S.schema = [];
    S.draft = null;
    S.busy = false;
    S._pickingAvatar = false;
  }

  window.OY_PAGES = window.OY_PAGES || {};
  function isDirty() {
    if (!S.card || !S.savedCard) return false;
    return computeDirty();
  }

  window.OY_PAGES.character = {
    id: "character",
    title: "角色卡",
    icon: "character",
    render: render,
    destroy: destroy,
    /* 供 app.js 的路由守卫判断「未保存修改」 */
    isDirty: isDirty
  };
})();
