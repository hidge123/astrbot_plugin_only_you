/**
 * OnlyYou Plugin Pages — 模型页 (`views/console/page-models.js`)
 *
 * 数据源（v2.8 按角色独立配置）：
 *   GET  `models/sources` → { sources: [...], tts_providers: [...] }
 *   GET  `models/list?source_id=` → { source_id, models: [...] }
 *   GET  `models/config?soul_id=`  → { soul_id, model_config, status }
 *   POST `models/config` { soul_id, model_config } → { soul_id, model_config, status }（保存后热重建该角色实例）
 *   POST `models/test` { role, soul_id? } → { status: "ok"|"error", role, latency_ms, reply | audio_base64 | error }
 *
 * v2.8：模型配置挂在 Soul 上（每个角色一套四角色模型）。页面顶部展示当前角色，
 * 加载 / 自动保存都带 `soul_id`；切换角色后 app.js 会重渲染本页并重新拉取该角色的配置。
 *
 * 页面结构：4 张角色卡片 —— 对话（必填）/ 决策·读空气 / 视频解析 / 语音 TTS。
 * 聊天卡：模型来源模式下拉（独立配置 / 继承自主对话模型 / 继承自轻量决策模型）+「独立配置」时
 *   展开自己的提供商源与模型选择器、「继承」时折叠并展示父角色 source_id / model 快照；
 *   状态灯按后端 status.configured / inherited / inherit_from 渲染；每张卡带测试按钮。
 * TTS 卡：来源分段（关闭 / 提供商源 / AstrBot TTS）+ 音源 / 模型 / 音色 + 概率 / 范围 / 双发 + 试听。
 * 底部固定保存栏 → POST models/config（payload 携带 inherit 与 inherit_from）。
 *
 * 注册：window.OY_PAGES.models = { id, title, icon, render(container, ctx), destroy() }
 */
(function () {
  "use strict";

  var CHAT_ROLES = ["main", "decision", "vision"];
  var ROLE_META = {
    main: {
      name: "对话模型",
      desc: "主回复 / 角色卡生成（必填）",
      icon: "character",
      help: "插件会把它注册为原生管线的 selected_provider；未配置时回退到 AstrBot 平台默认模型。"
    },
    decision: {
      name: "决策模型",
      desc: "读空气 / 记忆提炼 / 日程微调",
      icon: "activity",
      help: "读空气、记忆提炼、日程微调与造卡等后台任务使用；默认继承主模型，也可切「独立配置」（留空即停用该角色）。"
    },
    vision: {
      name: "视频解析模型",
      desc: "冲浪看片 / 多模态评估",
      icon: "video",
      help: "视频解析与九宫格看片评分使用；默认继承主模型（要求模型支持图片输入），也可独立配置或继承轻量决策模型。"
    },
    tts: {
      name: "语音 TTS",
      desc: "语音合成 / 主动分享语音",
      icon: "volume",
      help: "插件自主 TTS：提供商源模式自建 openai_tts_api 实例；AstrBot TTS 模式复用平台已有 TTS 提供商。"
    }
  };
  var TTS_MODES = [
    { value: "off", label: "关闭" },
    { value: "source", label: "提供商源" },
    { value: "provider", label: "AstrBot TTS" }
  ];
  var TTS_SCOPES = [
    { value: "private", label: "私聊" },
    { value: "l3", label: "仅L3" },
    { value: "all", label: "全部" }
  ];
  /* decision / vision 的「模型来源模式」可选值（"" = 独立配置，其余为继承的父角色） */
  var ROLE_INHERIT_OPTIONS = {
    decision: [
      { value: "", label: "独立配置（不继承）" },
      { value: "main", label: "继承自主对话模型 (main)" }
    ],
    vision: [
      { value: "", label: "独立配置（不继承）" },
      { value: "main", label: "继承自主对话模型 (main)" },
      { value: "decision", label: "继承自轻量决策模型 (decision)" }
    ]
  };

  var S = {
    app: null,
    container: null,
    destroyed: false,
    sources: [],
    ttsProviders: [],
    soulId: "",
    cfg: null,
    savedCfg: null,
    status: {},
    candidates: {},
    busy: false,
    autoSaveTimer: null,
    autoSavePending: false,
    autoSaveFailed: false
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

  function str(value) {
    return value == null ? "" : String(value);
  }

  function clamp(value, min, max) {
    var n = num(value, min);
    return Math.max(min, Math.min(max, n));
  }

  function toBool(value, fallback) {
    if (typeof value === "boolean") return value;
    if (value == null) return !!fallback;
    var text = String(value).trim().toLowerCase();
    if (["1", "true", "yes", "on", "y"].indexOf(text) >= 0) return true;
    if (["0", "false", "no", "off", "n"].indexOf(text) >= 0) return false;
    return !!fallback;
  }

  function hasOwn(obj, key) {
    return Object.prototype.hasOwnProperty.call(obj, key);
  }

  /** 当前管理角色（app.state 是唯一真源；切换角色后页面会重渲染） */
  function currentSoulId() {
    return String((S.app && S.app.state && S.app.state.currentSoulId) || "");
  }

  function currentSoulLabel() {
    var app = S.app;
    var sid = currentSoulId();
    var soul = app && app.state && app.state.currentSoul;
    if (soul && String(soul.soul_id || "") === sid && (soul.name || soul.soul_id)) {
      return String(soul.name || soul.soul_id);
    }
    return sid || "未选择角色";
  }

  /** 保存请求体：按角色保存，缺 soul_id 时由后端回退默认角色 */
  function configBody(modelConfig) {
    var body = { model_config: modelConfig };
    var sid = S.soulId || currentSoulId();
    if (sid) body.soul_id = sid;
    return body;
  }

  function inheritOptions(role) {
    return ROLE_INHERIT_OPTIONS[role] || ROLE_INHERIT_OPTIONS.decision;
  }

  function legalInheritValues(role) {
    return inheritOptions(role).map(function (item) {
      return item.value;
    });
  }

  /**
   * 归一化 decision / vision 的「模型来源模式」。
   * 优先级：显式 inherit_from → 旧字段 inherit → 默认继承主模型（向后兼容旧配置）。
   * 不变量：inherit === Boolean(inherit_from)，两者永远同步。
   */
  function normalizeInheritFrom(role, raw) {
    raw = raw && typeof raw === "object" ? raw : {};
    var legal = legalInheritValues(role);
    var inheritFrom = "";
    if (hasOwn(raw, "inherit_from") && raw.inherit_from != null) {
      inheritFrom = str(raw.inherit_from).trim();
      if (legal.indexOf(inheritFrom) < 0) inheritFrom = toBool(raw.inherit, false) ? "main" : "";
    } else if (hasOwn(raw, "inherit")) {
      inheritFrom = toBool(raw.inherit, true) ? "main" : "";
    } else {
      inheritFrom = "main";
    }
    if (legal.indexOf(inheritFrom) < 0) inheritFrom = "";
    return { inherit: !!inheritFrom, inherit_from: inheritFrom };
  }

  function normalizeChatRole(role, raw) {
    raw = raw && typeof raw === "object" ? raw : {};
    var source = normalizeInheritFrom(role, raw);
    return {
      inherit: source.inherit,
      inherit_from: source.inherit_from,
      source_id: str(raw.source_id),
      model: str(raw.model)
    };
  }

  function normalizeConfig(raw) {
    raw = raw && typeof raw === "object" ? raw : {};
    var main = raw.main && typeof raw.main === "object" ? raw.main : {};
    var tts = raw.tts && typeof raw.tts === "object" ? raw.tts : {};
    var ttsMode = str(tts.mode) || "off";
    if (["off", "source", "provider"].indexOf(ttsMode) < 0) ttsMode = "off";
    var ttsScope = str(tts.scope) || "private";
    if (["private", "l3", "all"].indexOf(ttsScope) < 0) ttsScope = "private";
    return {
      main: { source_id: str(main.source_id), model: str(main.model) },
      decision: normalizeChatRole("decision", raw.decision),
      vision: normalizeChatRole("vision", raw.vision),
      tts: {
        mode: ttsMode,
        source_id: str(tts.source_id),
        model: str(tts.model),
        voice: str(tts.voice),
        provider_id: str(tts.provider_id),
        probability: clamp(tts.probability == null ? 1 : tts.probability, 0, 1),
        scope: ttsScope,
        dual_output: !!tts.dual_output
      }
    };
  }

  function computeDirty() {
    if (!S.cfg || !S.savedCfg) return false;
    try {
      return JSON.stringify(S.cfg) !== JSON.stringify(S.savedCfg);
    } catch (err) {
      return true;
    }
  }

  function roleMeta(role) {
    return ROLE_META[role] || { name: role, desc: "", icon: "models", help: "" };
  }

  function ttsMeta() {
    return S.cfg && S.cfg.tts ? S.cfg.tts : {};
  }

  function statusOf(role) {
    return (S.status && S.status[role]) || {};
  }

  function pairLabel(sourceId, model) {
    var sid = str(sourceId);
    var modelName = str(model);
    if (sid && modelName) return sid + " / " + modelName;
    return sid || modelName;
  }

  function statusInfo(role) {
    var info = statusOf(role);
    if (role === "tts") {
      if (!info.mode || info.mode === "off") {
        return { cls: "light-off", text: "已关闭", tip: info.label || "TTS 已关闭" };
      }
      return {
        cls: info.ok === false ? "light-err" : "light-ok",
        text: info.mode === "provider" ? "AstrBot TTS" : "插件自建 TTS",
        tip: info.label || ""
      };
    }
    if (info.configured === false || info.mode === "unconfigured") {
      return { cls: "light-err", text: "未配置", tip: info.label || "未配置" };
    }
    if (info.inherited) {
      var cfg = S.cfg && S.cfg[role] ? S.cfg[role] : {};
      var fromRole = str(info.inherit_from) || str(cfg.inherit_from) || "main";
      var pair = pairLabel(info.source_id, info.model);
      var base = fromRole === "decision" ? "继承自轻量决策模型" : "继承自主对话模型";
      var text = pair ? base + " · " + pair : base;
      return {
        cls: info.ok === false ? "light-err" : "light-warn",
        text: text,
        tip: info.label || text,
        wrap: true
      };
    }
    return {
      cls: info.ok === false ? "light-err" : "light-ok",
      text: info.mode === "reuse" ? "复用 AstrBot 实例" : info.mode === "plugin" ? "插件自建实例" : "已配置",
      tip: info.label || ""
    };
  }

  function chatSources() {
    return S.sources.filter(function (source) {
      var providerType = str(source.provider_type);
      return !providerType || providerType === "chat_completion";
    });
  }

  function sourceOptionsHtml(selectedId, withEmpty, includeAll) {
    var html = withEmpty ? '<option value="">— 选择提供商源 —</option>' : "";
    var list = includeAll ? S.sources : chatSources();
    var matched = false;
    list.forEach(function (source) {
      var label = (source.provider || source.id) + " · " + source.id;
      if (source.enable === false) label += "（源未启用）";
      if (String(selectedId) === String(source.id)) matched = true;
      html +=
        '<option value="' +
        esc(source.id) +
        '"' +
        (String(selectedId) === String(source.id) ? " selected" : "") +
        ">" +
        esc(label) +
        "</option>";
    });
    /* 配置里引用了已被删除的提供商源时保留旧值，避免静默清空 */
    if (selectedId && !matched) {
      html +=
        '<option value="' +
        esc(selectedId) +
        '" selected>' +
        esc(selectedId) +
        "（提供商源不存在）</option>";
    }
    return html;
  }

  /* ------------------------------------------------------------------ *
   * 聊天角色卡片
   * ------------------------------------------------------------------ */
  function roleControlsHtml(role) {
    var cfg = S.cfg[role] || {};
    var helper;
    if (chatSources().length === 0) {
      helper = "还没有可用的提供商源：请先在 AstrBot WebUI → 服务提供商里添加一个聊天模型源。";
    } else if (role === "main") {
      helper = "可直接填写模型名，或点「拉取」从提供商源读取候选模型；修改后自动保存并即时生效。";
    } else {
      helper =
        "独立配置：使用本卡片的提供商源与模型；留空表示停用该角色（不会回退主模型）。可直接填写模型名，或点「拉取」读取候选模型；修改后自动保存并即时生效。";
    }
    return (
      '<div class="field"><span class="field-label">提供商源</span>' +
      '<select class="select" data-model-role="' +
      role +
      '" data-model-field="source_id">' +
      sourceOptionsHtml(cfg.source_id, true) +
      "</select></div>" +
      '<div class="field mt-12"><span class="field-label">模型名</span>' +
      '<div class="row">' +
      '<input class="input grow" type="text" data-model-role="' +
      role +
      '" data-model-field="model" value="' +
      esc(cfg.model) +
      '" placeholder="例如：deepseek-chat" />' +
      '<button class="btn btn-outline btn-sm" type="button" data-action="pull-models" data-role-key="' +
      role +
      '">' +
      icon("refresh", 13) +
      "拉取</button></div>" +
      '<div class="row wrap mt-4" data-role="candidates-' +
      esc(role) +
      '">' +
      candidatesHtml(role) +
      "</div>" +
      '<div class="field-help">' +
      esc(helper) +
      "</div></div>"
    );
  }

  function candidatesHtml(role) {
    var models = S.candidates[role];
    if (!Array.isArray(models)) return "";
    if (!models.length) {
      return '<span class="hint">没有拉取到候选模型，请手动输入模型名。</span>';
    }
    var current = S.cfg[role] ? String(S.cfg[role].model || "") : "";
    return models
      .slice(0, 40)
      .map(function (item) {
        var model = str(item && item.model);
        if (!model) return "";
        var badges = [];
        if (item.source === "remote") badges.push("远程");
        else if (item.enable) badges.push("已启用");
        else badges.push("未启用");
        var modalities = Array.isArray(item.modalities) ? item.modalities : [];
        if (modalities.indexOf("image") >= 0) badges.push("视觉");
        return (
          '<button class="btn btn-ghost btn-xs' +
          (model === current ? " is-selected" : "") +
          '"' +
          (model === current
            ? ' style="background:var(--primary-soft);color:var(--primary)"'
            : "") +
          ' type="button" data-action="pick-model" data-role-key="' +
          esc(role) +
          '" data-model="' +
          esc(model) +
          '" data-tip="' +
          esc(model + " · " + badges.join(" / ")) +
          '">' +
          esc(model) +
          "</button>"
        );
      })
      .join("") +
      '<span class="hint">共 ' +
      esc(models.length) +
      " 个候选，点击填入</span>";
  }

  function testResultHtml(role) {
    return '<div class="model-test-result" data-role="test-result-' + esc(role) + '" hidden></div>';
  }

  /* ------------------------------------------------------------------ *
   * 模型来源模式（独立配置 / 继承父角色）
   * ------------------------------------------------------------------ */
  function pairOf(entry) {
    var raw = entry && typeof entry === "object" ? entry : {};
    return { source_id: str(raw.source_id), model: str(raw.model) };
  }

  /**
   * 计算某个角色「保存后会生效」的 (source_id, model)：独立配置取自己的，
   * 继承则递归取父角色（vision -> decision -> main，最多 2 跳，防环）。
   */
  function effectivePair(role) {
    if (!S.cfg) return { source_id: "", model: "" };
    if (role === "main") return pairOf(S.cfg.main);
    var entry = S.cfg[role];
    if (!entry) return { source_id: "", model: "" };
    var fromRole = str(entry.inherit_from);
    if (!fromRole) return pairOf(entry);
    if (fromRole === "main") return pairOf(S.cfg.main);
    if (fromRole === "decision") {
      var decision = S.cfg.decision || {};
      var decisionFrom = str(decision.inherit_from);
      if (!decisionFrom) return pairOf(decision);
      if (decisionFrom === "main") return pairOf(S.cfg.main);
    }
    return { source_id: "", model: "" };
  }

  function sourceModeHtml(role) {
    var cfg = S.cfg[role] || {};
    var selected = str(cfg.inherit_from);
    var desc =
      role === "vision"
        ? "独立配置：使用本卡片的提供商源与模型，留空即停用该角色；继承：直接复用父角色当前生效的模型。修改后会自动保存并即时生效。"
        : "独立配置：使用本卡片的提供商源与模型，留空即停用该角色（不再回退主模型）；继承：与主对话模型保持一致。修改后会自动保存并即时生效。";
    return (
      '<div class="field"><span class="field-label">模型来源模式</span>' +
      '<select class="select" data-role="inherit-from" data-role-key="' +
      esc(role) +
      '">' +
      inheritOptions(role)
        .map(function (item) {
          return (
            '<option value="' +
            esc(item.value) +
            '"' +
            (item.value === selected ? " selected" : "") +
            ">" +
            esc(item.label) +
            "</option>"
          );
        })
        .join("") +
      "</select>" +
      '<div class="field-help">' +
      esc(desc) +
      "</div></div>"
    );
  }

  function inheritedPreviewHtml(role) {
    var cfg = S.cfg[role] || {};
    var fromRole = str(cfg.inherit_from) || "main";
    var parentRole = fromRole === "decision" ? "decision" : "main";
    var parent = effectivePair(parentRole);
    var configured = !!(parent.source_id && parent.model);
    var parentName = fromRole === "decision" ? "轻量决策模型" : "主对话模型";
    var chainNote = "";
    if (role === "vision" && fromRole === "decision") {
      var decisionCfg = S.cfg.decision || {};
      if (str(decisionCfg.inherit_from) === "main") {
        chainNote = "（轻量决策模型当前继承自主对话模型）";
      }
    }
    return (
      '<div class="field" data-role="inherit-preview-' +
      esc(role) +
      '"><span class="field-label">继承来源快照</span>' +
      '<div class="card" style="background:var(--surface-2);border:0"><div class="card-body tight">' +
      '<div class="row wrap" style="gap:8px">' +
      '<span class="status-light ' +
      (configured ? "light-ok" : "light-err") +
      '"></span>' +
      '<strong class="text-sm">' +
      esc(parentName) +
      "</strong>" +
      '<span class="mono text-sm">' +
      esc(configured ? pairLabel(parent.source_id, parent.model) : "未配置，该角色将停用") +
      "</span></div>" +
      '<div class="field-help mt-8">' +
      esc(
        "继承模式下该角色直接复用父角色当前生效的提供商源与模型，独立选择器已折叠；切回「独立配置」即可单独指定（留空表示停用该角色）。" +
          chainNote
      ) +
      "</div></div></div></div>"
    );
  }

  function roleDetailsHtml(role) {
    if (role === "main") return roleControlsHtml(role);
    var cfg = S.cfg[role] || {};
    if (str(cfg.inherit_from)) return inheritedPreviewHtml(role);
    return roleControlsHtml(role);
  }

  function chatCardHtml(role) {
    var meta = roleMeta(role);
    var info = statusInfo(role);
    var statusStyle = info.wrap
      ? ' style="white-space:normal;text-align:right;max-width:58%"'
      : "";
    return (
      '<section class="card model-card" data-role-card="' +
      role +
      '"><div class="card-head">' +
      '<div class="model-role"><span class="model-role-icon">' +
      icon(meta.icon, 15) +
      "</span>" +
      '<div class="model-role-text"><span class="model-role-name">' +
      esc(meta.name) +
      (role === "main" ? '<span class="tag tag-danger" style="margin-left:6px">必填</span>' : "") +
      '</span><span class="model-role-desc">' +
      esc(meta.desc) +
      "</span></div></div>" +
      '<div class="model-status"' +
      statusStyle +
      ' data-tip="' +
      esc(info.tip) +
      '"><span class="status-light ' +
      info.cls +
      '"></span><span>' +
      esc(info.text) +
      "</span></div></div>" +
      '<div class="card-body">' +
      (role === "main" ? "" : sourceModeHtml(role)) +
      '<div data-role="role-controls-' +
      esc(role) +
      '"' +
      (role === "main" ? "" : ' class="mt-12"') +
      ">" +
      roleDetailsHtml(role) +
      "</div>" +
      '<div class="row mt-12"><button class="btn btn-outline btn-sm" type="button" data-action="test-role" data-role-key="' +
      esc(role) +
      '">' +
      icon("zap", 13) +
      "测试连接</button>" +
      '<span class="hint">测试使用当前已即时生效的配置（如刚修改会自动先保存）。</span></div>' +
      '<div data-role="test-wrap-' +
      esc(role) +
      '">' +
      testResultHtml(role) +
      "</div>" +
      '<div class="field-help mt-8">' +
      esc(meta.help) +
      "</div></div></section>"
    );
  }

  /* ------------------------------------------------------------------ *
   * TTS 卡片
   * ------------------------------------------------------------------ */
  function ttsDetailHtml() {
    var tts = ttsMeta();
    var mode = str(tts.mode) || "off";
    if (mode === "off") {
      return (
        '<div class="card" style="background:var(--surface-2);border:0"><div class="card-body tight">' +
        '<div class="text-sm text-2">语音已关闭：Bot 只用文字回复，不会合成或发送语音。</div>' +
        "</div></div>"
      );
    }
    var detail = "";
    if (mode === "source") {
      detail =
        '<div class="field"><span class="field-label">提供商源（OpenAI 兼容 TTS）</span>' +
        '<select class="select" data-tts-field="source_id">' +
        sourceOptionsHtml(tts.source_id, true, true) +
        "</select></div>" +
        '<div class="field mt-12"><span class="field-label">TTS 模型名</span>' +
        '<input class="input" type="text" data-tts-field="model" value="' +
        esc(tts.model) +
        '" placeholder="例如：tts-1 / cosyvoice-v1" /></div>' +
        '<div class="field mt-12"><span class="field-label">音色 voice</span>' +
        '<input class="input" type="text" data-tts-field="voice" value="' +
        esc(tts.voice) +
        '" placeholder="例如：alloy / zh-CN-XiaoxiaoNeural" />' +
        '<div class="field-help">插件会用该提供商源的 api_base / key 自建 openai_tts_api 实例，音色写入 openai-tts-voice。</div></div>';
    } else if (mode === "provider") {
      var options = '<option value="">— 选择 AstrBot TTS 提供商 —</option>';
      var providerMatched = false;
      S.ttsProviders.forEach(function (item) {
        var label = (item.id || "") + (item.model ? " · " + item.model : "") + (item.type ? " · " + item.type : "");
        if (item.enable === false) label += "（未启用）";
        if (String(tts.provider_id) === String(item.id)) providerMatched = true;
        options +=
          '<option value="' +
          esc(item.id) +
          '"' +
          (String(tts.provider_id) === String(item.id) ? " selected" : "") +
          ">" +
          esc(label) +
          "</option>";
      });
      if (tts.provider_id && !providerMatched) {
        options +=
          '<option value="' +
          esc(tts.provider_id) +
          '" selected>' +
          esc(tts.provider_id) +
          "（提供商不存在）</option>";
      }
      detail =
        '<div class="field"><span class="field-label">AstrBot TTS 提供商</span>' +
        '<select class="select" data-tts-field="provider_id">' +
        options +
        "</select>" +
        '<div class="field-help">直接复用 AstrBot 已安装的 TTS 提供商实例（edge_tts / dashscope_tts 等）。</div></div>';
    }
    return (
      detail +
      '<div class="field mt-12"><span class="field-label">随机触发概率</span>' +
      '<div class="slider-row">' +
      '<input class="slider" type="range" min="0" max="1" step="0.05" data-tts-field="probability" value="' +
      num(tts.probability, 1) +
      '" />' +
      '<span class="slider-value" data-role="probability-value">' +
      Math.round(clamp(tts.probability, 0, 1) * 100) +
      "%</span></div>" +
      '<div class="field-help">每次回复按该概率决定是否把文字转成语音发送。</div></div>' +
      '<div class="field mt-12"><span class="field-label">生效范围</span>' +
      '<div class="segmented" data-role="tts-scope-seg">' +
      TTS_SCOPES.map(function (item) {
        return (
          '<button type="button" class="seg-btn' +
          (String(tts.scope) === item.value ? " active" : "") +
          '" data-action="set-tts-scope" data-scope="' +
          esc(item.value) +
          '">' +
          esc(item.label) +
          "</button>"
        );
      }).join("") +
      '</div><div class="field-help">私聊 = 仅好友私聊；仅L3 = 只给 ★L3 核心用户；全部 = 任何会话。</div></div>' +
      '<div class="switch-row"><div class="switch-row-text">' +
      '<span class="switch-row-title">语音 + 文字双发</span>' +
      '<span class="switch-row-desc">开启后同时保留原文字消息；关闭则只发语音。若在 AstrBot 里也开了原生 TTS，请勿开启双发以免重复播报。</span></div>' +
      '<label class="switch"><input type="checkbox" data-tts-field="dual_output"' +
      (tts.dual_output ? " checked" : "") +
      ' /><span class="switch-track"></span></label></div>'
    );
  }

  function ttsCardHtml() {
    var meta = roleMeta("tts");
    var info = statusInfo("tts");
    var tts = ttsMeta();
    var mode = str(tts.mode) || "off";
    return (
      '<section class="card model-card" data-role-card="tts"><div class="card-head">' +
      '<div class="model-role"><span class="model-role-icon">' +
      icon(meta.icon, 15) +
      "</span>" +
      '<div class="model-role-text"><span class="model-role-name">' +
      esc(meta.name) +
      '</span><span class="model-role-desc">' +
      esc(meta.desc) +
      "</span></div></div>" +
      '<div class="model-status" data-tip="' +
      esc(info.tip) +
      '"><span class="status-light ' +
      info.cls +
      '"></span><span>' +
      esc(info.text) +
      "</span></div></div>" +
      '<div class="card-body"><div class="grid-2">' +
      '<div><div class="field"><span class="field-label">语音来源</span>' +
      '<div class="segmented" data-role="tts-mode-seg">' +
      TTS_MODES.map(function (item) {
        return (
          '<button type="button" class="seg-btn' +
          (mode === item.value ? " active" : "") +
          '" data-action="set-tts-mode" data-mode="' +
          esc(item.value) +
          '">' +
          esc(item.label) +
          "</button>"
        );
      }).join("") +
      '</div><div class="field-help">关闭：不合成语音；提供商源：插件自建 OpenAI 兼容 TTS；AstrBot TTS：复用平台提供商。</div></div>' +
      '<div data-role="tts-detail">' +
      ttsDetailHtml() +
      "</div></div>" +
      '<div><div class="card" style="background:var(--surface-2);border:0"><div class="card-body">' +
      '<div class="panel-title">' +
      icon("volume", 13) +
      "语音测试与说明</div>" +
      '<div class="row mt-8"><button class="btn btn-outline btn-sm" type="button" data-action="test-role" data-role-key="tts">' +
      icon("play", 13) +
      "试听</button>" +
      '<span class="hint">使用当前配置合成「你好呀，我是 OnlyYou」。</span></div>' +
      '<div data-role="test-wrap-tts">' +
      testResultHtml("tts") +
      "</div>" +
      '<div class="field-help mt-8">' +
      esc(meta.help) +
      "</div></div></div></div>" +
      "</div></div></section>"
    );
  }

  /* ------------------------------------------------------------------ *
   * 页面
   * ------------------------------------------------------------------ */
  function mainUnconfigured() {
    var info = statusOf("main");
    return info.configured === false || info.mode === "unconfigured" || !info.label;
  }

  function soulBannerHtml() {
    var sid = currentSoulId();
    if (!sid) return "";
    return (
      '<div class="card" data-role="soul-banner"><div class="card-body tight">' +
      '<div class="row wrap" style="gap:8px">' +
      '<span class="tag tag-primary">当前角色</span>' +
      '<strong class="text-sm">' +
      esc(currentSoulLabel()) +
      "</strong>" +
      '<span class="mono text-xs muted">' +
      esc(sid) +
      "</span>" +
      '<span class="text-xs text-2" style="flex:1;min-width:200px">模型配置按角色独立保存；在顶栏切换角色后会自动加载该角色自己的四角色模型配置。</span>' +
      "</div></div></div>"
    );
  }

  function bannerHtml() {
    if (!mainUnconfigured()) return "";
    return (
      '<div class="card" style="border-color:var(--danger)"><div class="card-body tight">' +
      '<div class="row wrap">' +
      '<span class="status-light light-err"></span>' +
      '<strong class="text-sm">对话主模型未配置</strong>' +
      '<span class="text-sm text-2">插件会回退到 AstrBot 平台默认模型；角色卡 AI 生成、读空气、记忆提炼等能力可能不可用。请先在下方「对话模型」卡片选择提供商源与模型（修改即时生效）。</span>' +
      "</div></div></div>"
    );
  }

  function pageHtml() {
    return (
      '<div class="page-head"><h1 class="page-title">' +
      icon("models", 18) +
      "模型" +
      '<span class="card-sub">当前角色：' +
      esc(currentSoulLabel()) +
      " · 四角色模型按角色独立配置 · 修改即时生效</span></h1>" +
      '<div class="page-actions">' +
      '<button class="btn btn-ghost btn-sm" type="button" data-action="reload">' +
      icon("refresh", 14) +
      "重新读取</button></div></div>" +
      soulBannerHtml() +
      bannerHtml() +
      '<div class="model-grid">' +
      CHAT_ROLES.map(chatCardHtml).join("") +
      "</div>" +
      ttsCardHtml()
    );
  }

  function paint() {
    if (!S.container || S.destroyed) return;
    var scroller = S.container.closest ? S.container.closest(".main") : null;
    var top = scroller ? scroller.scrollTop : 0;
    S.container.innerHTML = pageHtml();
    if (scroller) scroller.scrollTop = top;
  }

  function refreshRoleControls(role) {
    if (!S.container) return;
    var wrap = S.container.querySelector('[data-role="role-controls-' + role + '"]');
    if (!wrap) return;
    wrap.innerHTML = roleDetailsHtml(role);
  }

  /** 只刷新四张卡片右上角的状态灯，不重绘表单，避免打断输入。 */
  function refreshStatusBadges() {
    if (!S.container) return;
    ["main", "decision", "vision", "tts"].forEach(function (role) {
      var card = S.container.querySelector('[data-role-card="' + role + '"]');
      if (!card) return;
      var el = card.querySelector(".model-status");
      if (!el) return;
      var info = statusInfo(role);
      el.setAttribute("data-tip", info.tip || "");
      el.style.whiteSpace = info.wrap ? "normal" : "";
      el.style.textAlign = info.wrap ? "right" : "";
      el.style.maxWidth = info.wrap ? "58%" : "";
      el.innerHTML =
        '<span class="status-light ' + info.cls + '"></span><span>' + esc(info.text) + "</span>";
    });
  }

  /**
   * 防抖自动保存：任何模型配置修改停下约 0.7s 后自动 POST models/config，
   * 让「选源 / 模型 / 来源模式 / TTS」修改即时生效，无需手动点保存。
   */
  function scheduleAutoSave() {
    if (S.destroyed) return;
    if (S.autoSaveTimer) clearTimeout(S.autoSaveTimer);
    S.autoSaveTimer = null;
    S.autoSavePending = true;
    S.autoSaveTimer = setTimeout(function () {
      S.autoSaveTimer = null;
      S.autoSavePending = false;
      autoSaveConfig();
    }, 700);
  }

  async function autoSaveConfig() {
    var app = S.app;
    if (!app || !S.cfg || S.destroyed) return;
    if (S.busy) {
      scheduleAutoSave();
      return;
    }
    if (!computeDirty()) {
      S.autoSaveFailed = false;
      return;
    }
    var payload = clone(S.cfg);
    var payloadJson = JSON.stringify(normalizeConfig(payload));
    S.busy = true;
    try {
      var data = await app.apiPost("models/config", configBody(payload));
      if (S.destroyed || !S.app) return;
      var saved =
        data && data.model_config && typeof data.model_config === "object"
          ? normalizeConfig(data.model_config)
          : normalizeConfig(payload);
      /* 关键：savedCfg 必须是深拷贝，否则与 S.cfg 共享引用，
         后续编辑会同时改到 savedCfg，computeDirty() 永远为 false（自动保存失效）。 */
      S.savedCfg = clone(saved);
      /* 保存期间用户若继续编辑，保留其新改动继续下一轮自动保存 */
      if (JSON.stringify(S.cfg) === payloadJson) S.cfg = saved;
      if (data && data.soul_id) S.soulId = String(data.soul_id);
      S.status =
        data && data.status && typeof data.status === "object" ? data.status : S.status;
      if (app.applyModelStatus) app.applyModelStatus(S.status);
      S.autoSaveFailed = false;
      refreshStatusBadges();
      app.toast("模型配置已即时生效");
    } catch (err) {
      S.autoSaveFailed = true;
      if (app) app.toast("模型配置自动保存失败：" + (err.message || err), true);
    } finally {
      S.busy = false;
      if (S.app && !S.autoSaveFailed && computeDirty()) scheduleAutoSave();
    }
  }

  /** 父角色表单值变化后，刷新所有处于继承模式的角色来源快照 */
  function refreshInheritPreviews() {
    if (!S.cfg) return;
    ["decision", "vision"].forEach(function (role) {
      var entry = S.cfg[role];
      if (entry && str(entry.inherit_from)) refreshRoleControls(role);
    });
  }

  function refreshTtsDetail() {
    if (!S.container) return;
    var detail = S.container.querySelector('[data-role="tts-detail"]');
    if (detail) detail.innerHTML = ttsDetailHtml();
    var modeSeg = S.container.querySelector('[data-role="tts-mode-seg"]');
    if (modeSeg) {
      var mode = str(ttsMeta().mode) || "off";
      modeSeg.querySelectorAll(".seg-btn").forEach(function (btn) {
        btn.classList.toggle("active", btn.getAttribute("data-mode") === mode);
      });
    }
    var scopeSeg = S.container.querySelector('[data-role="tts-scope-seg"]');
    if (scopeSeg) {
      var scope = str(ttsMeta().scope) || "private";
      scopeSeg.querySelectorAll(".seg-btn").forEach(function (btn) {
        btn.classList.toggle("active", btn.getAttribute("data-scope") === scope);
      });
    }
    var probValue = S.container.querySelector('[data-role="probability-value"]');
    if (probValue) probValue.textContent = Math.round(clamp(ttsMeta().probability, 0, 1) * 100) + "%";
  }

  /* ------------------------------------------------------------------ *
   * 数据加载
   * ------------------------------------------------------------------ */
  async function loadData(app) {
    var sid = currentSoulId();
    var params = sid ? { soul_id: sid } : {};
    var results = await Promise.all([
      app.apiGet("models/sources"),
      app.apiGet("models/config", params)
    ]);
    var sourcesData = results[0] || {};
    var configData = results[1] || {};
    S.sources = Array.isArray(sourcesData.sources) ? sourcesData.sources : [];
    S.ttsProviders = Array.isArray(sourcesData.tts_providers) ? sourcesData.tts_providers : [];
    S.soulId = configData.soul_id != null && configData.soul_id !== "" ? String(configData.soul_id) : sid;
    S.cfg = normalizeConfig(configData.model_config);
    S.savedCfg = clone(S.cfg);
    S.status = configData.status && typeof configData.status === "object" ? configData.status : {};
    S.candidates = {};
    if (app.applyModelStatus) app.applyModelStatus(S.status);
  }

  async function reload(showToast) {
    if (!S.app || S.busy) return;
    S.busy = true;
    try {
      await loadData(S.app);
      paint();
      if (showToast) S.app.toast("已重新读取模型配置");
    } catch (err) {
      S.app.toast("读取模型配置失败：" + (err.message || err), true);
    } finally {
      S.busy = false;
    }
  }

  /* ------------------------------------------------------------------ *
   * 拉取候选 / 测试 / 保存
   * ------------------------------------------------------------------ */
  async function pullModels(role, buttonEl) {
    var app = S.app;
    if (!app || !S.cfg[role]) return;
    var sourceId = str(S.cfg[role].source_id);
    if (!sourceId) {
      app.toast("请先选择提供商源", true);
      return;
    }
    if (buttonEl) buttonEl.classList.add("is-loading");
    try {
      var data = await app.apiGet("models/list", { source_id: sourceId });
      S.candidates[role] = data && Array.isArray(data.models) ? data.models : [];
      var wrap = S.container ? S.container.querySelector('[data-role="candidates-' + role + '"]') : null;
      if (wrap) wrap.innerHTML = candidatesHtml(role);
      app.toast("已拉取 " + S.candidates[role].length + " 个候选模型");
    } catch (err) {
      app.toast("拉取模型列表失败：" + (err.message || err), true);
    } finally {
      if (buttonEl) buttonEl.classList.remove("is-loading");
    }
  }

  function renderTestResult(role, data) {
    if (!S.container) return;
    var wrap = S.container.querySelector('[data-role="test-wrap-' + role + '"]');
    if (!wrap) return;
    var isError = !data || data.status === "error";
    var html = "";
    if (isError) {
      html =
        '<div class="model-test-result is-err">' +
        icon("alert", 13) +
        " 测试失败：" +
        esc((data && data.error) || "未知错误") +
        "</div>";
    } else if (role === "tts") {
      var audio = str(data.audio_base64);
      html =
        '<div class="model-test-result is-ok">' +
        icon("check", 13) +
        " 合成成功 · 延迟 " +
        esc(num(data.latency_ms, 0)) +
        " ms" +
        (audio
          ? '<audio controls autoplay style="width:100%;margin-top:8px" src="data:audio/wav;base64,' +
            esc(audio) +
            '"></audio>'
          : '<div class="text-xs mt-4">后端未返回音频数据（audio_base64 为空），请检查 TTS 提供商日志。</div>') +
        "</div>";
    } else {
      html =
        '<div class="model-test-result is-ok">' +
        icon("check", 13) +
        " 连接成功 · 延迟 " +
        esc(num(data.latency_ms, 0)) +
        " ms · " +
        esc(str(data.provider_id) || "unknown") +
        " / " +
        esc(str(data.model) || "unknown") +
        (data.reply
          ? '<div class="text-xs mt-4" style="white-space:pre-wrap">回复：' + esc(data.reply) + "</div>"
          : "") +
        "</div>";
    }
    wrap.innerHTML = html;
  }

  async function testRole(role, buttonEl) {
    var app = S.app;
    if (!app) return;
    if (computeDirty()) {
      await autoSaveConfig();
    }
    if (buttonEl) buttonEl.classList.add("is-loading");
    renderTestResult(role, null);
    var wrap = S.container ? S.container.querySelector('[data-role="test-wrap-' + role + '"]') : null;
    if (wrap) {
      wrap.innerHTML =
        '<div class="model-test-result"><span class="spinner" style="width:12px;height:12px;display:inline-block;vertical-align:middle"></span> 测试中…</div>';
    }
    var body = { role: role };
    var sid = S.soulId || currentSoulId();
    if (sid) body.soul_id = sid;
    try {
      var data = await app.apiPost("models/test", body);
      renderTestResult(role, data || { status: "error", error: "接口返回为空" });
    } catch (err) {
      renderTestResult(role, { status: "error", error: err.message || String(err) });
    } finally {
      if (buttonEl) buttonEl.classList.remove("is-loading");
    }
  }

  async function saveConfig() {
    var app = S.app;
    if (!app || S.busy) return;
    if (!computeDirty()) {
      app.toast("模型配置已即时生效，无需手动保存");
      return;
    }
    S.busy = true;
    var saveBtn = S.container ? S.container.querySelector('[data-action="save-config"]') : null;
    if (saveBtn) saveBtn.classList.add("is-loading");
    try {
      var data = await app.apiPost("models/config", configBody(S.cfg));
      if (S.destroyed || !S.app) return;
      if (data && data.model_config && typeof data.model_config === "object") {
        S.cfg = normalizeConfig(data.model_config);
      }
      if (data && data.soul_id) S.soulId = String(data.soul_id);
      S.savedCfg = clone(S.cfg);
      S.autoSaveFailed = false;
      S.status = data && data.status && typeof data.status === "object" ? data.status : S.status;
      if (app.applyModelStatus) app.applyModelStatus(S.status);
      paint();
      app.toast("模型配置已保存并即时生效");
    } catch (err) {
      app.toast("保存模型配置失败：" + (err.message || err), true);
    } finally {
      S.busy = false;
      var nextBtn = S.container ? S.container.querySelector('[data-action="save-config"]') : null;
      if (nextBtn) nextBtn.classList.remove("is-loading");
    }
  }

  /* ------------------------------------------------------------------ *
   * 事件
   * ------------------------------------------------------------------ */
  function handleClick(event) {
    var target = event.target;
    if (!target || !target.closest) return;
    var actionEl = target.closest("[data-action]");
    if (!actionEl) return;
    var action = actionEl.getAttribute("data-action");
    var app = S.app;
    if (!app) return;
    if (action === "reload") {
      reload(true);
    } else if (action === "save-config") {
      saveConfig();
    } else if (action === "test-role") {
      testRole(actionEl.getAttribute("data-role-key"), actionEl);
    } else if (action === "pull-models") {
      pullModels(actionEl.getAttribute("data-role-key"), actionEl);
    } else if (action === "pick-model") {
      var role = actionEl.getAttribute("data-role-key");
      var model = actionEl.getAttribute("data-model");
      if (!S.cfg[role]) return;
      S.cfg[role].model = model;
      var input = S.container
        ? S.container.querySelector(
            '[data-model-role="' + role + '"][data-model-field="model"]'
          )
        : null;
      if (input) input.value = model;
      var wrap = S.container ? S.container.querySelector('[data-role="candidates-' + role + '"]') : null;
      if (wrap) wrap.innerHTML = candidatesHtml(role);
      refreshInheritPreviews();
      scheduleAutoSave();
    } else if (action === "set-tts-mode") {
      var mode = actionEl.getAttribute("data-mode") || "off";
      if (!S.cfg.tts) S.cfg.tts = normalizeConfig({}).tts;
      S.cfg.tts.mode = mode;
      refreshTtsDetail();
      scheduleAutoSave();
    } else if (action === "set-tts-scope") {
      var scope = actionEl.getAttribute("data-scope") || "private";
      if (!S.cfg.tts) S.cfg.tts = normalizeConfig({}).tts;
      S.cfg.tts.scope = scope;
      refreshTtsDetail();
      scheduleAutoSave();
    }
  }

  function handleFieldChange(event) {
    var el = event.target;
    if (!el || !el.getAttribute) return;
    /* 模型字段（提供商源 / 模型名）用 data-model-role 标记；来源模式用 data-role-key */
    var roleKey =
      el.getAttribute("data-role-key") || el.getAttribute("data-model-role");
    if (el.getAttribute("data-role") === "inherit-from") {
      if (!roleKey || !S.cfg[roleKey]) return;
      var inheritFrom = str(el.value);
      if (legalInheritValues(roleKey).indexOf(inheritFrom) < 0) inheritFrom = "";
      S.cfg[roleKey].inherit_from = inheritFrom;
      S.cfg[roleKey].inherit = !!inheritFrom;
      refreshRoleControls(roleKey);
      scheduleAutoSave();
      return;
    }
    var modelField = el.getAttribute("data-model-field");
    if (roleKey && modelField && S.cfg[roleKey]) {
      S.cfg[roleKey][modelField] = String(el.value == null ? "" : el.value);
      if (modelField === "source_id") {
        /* 切换提供商源后清空旧候选，避免误选到别的源的模型 */
        S.candidates[roleKey] = null;
        var candidatesWrap = S.container
          ? S.container.querySelector('[data-role="candidates-' + roleKey + '"]')
          : null;
        if (candidatesWrap) candidatesWrap.innerHTML = candidatesHtml(roleKey);
      }
      refreshInheritPreviews();
      scheduleAutoSave();
      return;
    }
    var ttsField = el.getAttribute("data-tts-field");
    if (ttsField) {
      if (!S.cfg.tts) S.cfg.tts = normalizeConfig({}).tts;
      if (ttsField === "dual_output") {
        S.cfg.tts.dual_output = !!el.checked;
      } else if (ttsField === "probability") {
        S.cfg.tts.probability = clamp(el.value, 0, 1);
        var valueEl = S.container
          ? S.container.querySelector('[data-role="probability-value"]')
          : null;
        if (valueEl) valueEl.textContent = Math.round(S.cfg.tts.probability * 100) + "%";
      } else {
        S.cfg.tts[ttsField] = String(el.value == null ? "" : el.value);
      }
      scheduleAutoSave();
    }
  }

  function bindEvents(container, app) {
    container.addEventListener("click", handleClick);
    container.addEventListener("input", handleFieldChange);
    container.addEventListener("change", handleFieldChange);
    /* Ctrl/Cmd + S 保存 */
    S._keydownHandler = function (event) {
      if (!(event.metaKey || event.ctrlKey) || String(event.key).toLowerCase() !== "s") return;
      if (!S.container || S.destroyed) return;
      if (S.app && S.app.route !== "models") return;
      event.preventDefault();
      saveConfig();
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
    S.sources = [];
    S.ttsProviders = [];
    S.soulId = String((app && app.state && app.state.currentSoulId) || "");
    S.cfg = null;
    S.savedCfg = null;
    S.status = {};
    S.candidates = {};
    S.busy = false;
    S.autoSaveTimer = null;
    S.autoSavePending = false;
    S.autoSaveFailed = false;

    container.innerHTML =
      '<div class="loading"><span class="spinner"></span><span>加载当前角色的模型配置…</span></div>';
    bindEvents(container, app);
    try {
      await loadData(app);
      if (S.destroyed) return;
      paint();
    } catch (err) {
      console.error("[OnlyYou] 模型页渲染失败：", err);
      container.innerHTML =
        '<div class="page-head"><h1 class="page-title">' +
        icon("models", 18) +
        "模型</h1></div>" +
        '<div class="card"><div class="empty">' +
        icon("alert", 32) +
        '<div class="empty-title">模型配置加载失败</div>' +
        '<div class="empty-text">' +
        esc((err && (err.message || err)) || "未知错误") +
        "</div>" +
        '<div class="row mt-12"><button class="btn btn-outline" type="button" data-action="reload">重试</button></div>' +
        "</div></div>";
    }
  }

  function destroy() {
    S.destroyed = true;
    if (S.autoSaveTimer) {
      clearTimeout(S.autoSaveTimer);
      S.autoSaveTimer = null;
    }
    S.autoSavePending = false;
    S.autoSaveFailed = false;
    if (S._keydownHandler) {
      document.removeEventListener("keydown", S._keydownHandler);
      S._keydownHandler = null;
    }
    S.app = null;
    S.container = null;
    S.sources = [];
    S.ttsProviders = [];
    S.soulId = "";
    S.cfg = null;
    S.savedCfg = null;
    S.status = {};
    S.candidates = {};
    S.busy = false;
  }

  window.OY_PAGES = window.OY_PAGES || {};
  function isDirty() {
    /* 等待自动保存的改动视为「将会生效」，只有自动保存失败时才提示离开确认。 */
    if (S.autoSavePending && !S.autoSaveFailed) return false;
    return computeDirty();
  }

  window.OY_PAGES.models = {
    id: "models",
    title: "模型",
    icon: "models",
    render: render,
    destroy: destroy,
    isDirty: isDirty
  };
})();
