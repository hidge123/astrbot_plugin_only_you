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
 * v2.8：模型配置挂在 Soul 上（每个角色一套对话 / 主 / 决策 / 视觉等模型）。页面顶部展示当前角色，
 * 加载 / 自动保存都带 `soul_id`；切换角色后 app.js 会重渲染本页并重新拉取该角色的配置。
 *
 * 页面结构：4 张角色卡片 —— 对话（必填）/ 决策·读空气 / 视频解析 / 语音 TTS。
 * 聊天卡：模型来源模式下拉（独立配置 / 继承自对话模型 / 继承自主模型）+「独立配置」时
 *   展开自己的提供商源与模型选择器、「继承」时折叠并展示父角色 source_id / model 快照；
 *   状态灯按后端 status.configured / inherited / inherit_from 渲染；每张卡带测试按钮。
 * TTS 卡：来源分段（关闭 / 提供商源 / AstrBot TTS）+ 音源 / 模型 / 音色 + 概率 / 范围 / 双发 + 试听。
 * 底部固定保存栏 → POST models/config（payload 携带 inherit 与 inherit_from）。
 *
 * 注册：window.OY_PAGES.models = { id, title, icon, render(container, ctx), destroy() }
 */
(function () {
  "use strict";

  var CHAT_ROLES = ["dialogue", "main", "decision", "vision"];
  var DECISION_MODES = ["chat", "judge"];
  var DECISION_MODEL_SUGGESTIONS = [
    "~typesafe/jev-latest",
    "typesafe/jev-1.14",
    "openai/gpt-6-luna-decisions",
    "cloudflare/clef",
    "liquid/d1",
    "microsoft/microsoft-decision-1.1",
    "upstage/solar-decide"
  ];
  var ROLE_META = {
    dialogue: {
      name: "对话模型",
      desc: "只负责聊天回复（必填）",
      icon: "character",
      help: "插件把它注册为原生管线的 selected_provider；它只生成对话，不承担角色卡 / 记忆 / 主动消息等后台生成。未配置时回退到 AstrBot 平台默认模型。"
    },
    main: {
      name: "主模型",
      desc: "角色卡 / 记忆 / 主动消息生成",
      icon: "wand",
      help: "承担所有后台生成：AI 造卡与补全、行动卡生成、记忆提炼、日程微调文案、繁忙成果、私聊补回与主动分享文案。默认继承对话模型，也可独立配置；留空即停用后台生成（相关功能自动降级）。"
    },
    decision: {
      name: "决策模型",
      desc: "读空气 / 是否接话",
      icon: "activity",
      help: "只做群聊读空气判断。默认继承主模型，也可独立配置通用聊天模型（输出 JSON 判定）；判断模型是可选模式，切换后不会自动改你的提供商源或模型名。"
    },
    vision: {
      name: "视频解析模型",
      desc: "冲浪看片 / 多模态评估",
      icon: "video",
      help: "视频解析与九宫格看片评分使用；默认继承主模型（要求模型支持图片输入），也可独立配置或继承对话模型。"
    },
    tts: {
      name: "语音 TTS",
      desc: "语音合成 / 主动分享语音",
      icon: "volume",
      help: "插件自主 TTS：提供商源模式自建 openai_tts_api 实例；AstrBot TTS 模式复用平台已有 TTS 提供商。"
    },
    embedding: {
      name: "向量模型 (Embedding)",
      desc: "记忆语义检索 / 去重",
      icon: "search",
      help: "用于记忆写入时的语义去重与召回时的向量粗排；可复用 AstrBot 已启用的嵌入提供商（如 BAAI/bge-m3），也可按提供商源自建实例。关闭后自动降级为「content + keywords」词法检索。"
    },
    rerank: {
      name: "重排模型 (Rerank)",
      desc: "召回结果精排",
      icon: "filter",
      help: "对粗排候选做精确相关性重排（综合分 0.55×语义 + 0.25×新鲜度 + 0.20×重要度）；可复用 AstrBot 已启用的重排提供商（如 BAAI/bge-reranker-v2-m3）。关闭后只用向量 / 词法相关性。"
    }
  };
  /* v2.12: RAG 两个角色（三档来源模式：关闭 / 提供商源 / AstrBot 提供商） */
  var VECTOR_ROLES = ["embedding", "rerank"];
  var VECTOR_MODES = [
    { value: "off", label: "关闭" },
    { value: "source", label: "提供商源" },
    { value: "provider", label: "AstrBot 提供商" }
  ];
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
  /* main / decision / vision 的「模型来源模式」可选值（"" = 独立配置） */
  var ROLE_INHERIT_OPTIONS = {
    main: [
      { value: "", label: "独立配置（不继承）" },
      { value: "dialogue", label: "继承自对话模型 (dialogue)" }
    ],
    decision: [
      { value: "", label: "独立配置（不继承）" },
      { value: "main", label: "继承自主模型 (main)" },
      { value: "dialogue", label: "继承自对话模型 (dialogue)" }
    ],
    vision: [
      { value: "", label: "独立配置（不继承）" },
      { value: "main", label: "继承自主模型 (main)" },
      { value: "dialogue", label: "继承自对话模型 (dialogue)" }
    ]
  };

  var S = {
    app: null,
    container: null,
    destroyed: false,
    sources: [],
    ttsProviders: [],
    embeddingProviders: [],
    rerankProviders: [],
    soulId: "",
    cfg: null,
    savedCfg: null,
    status: {},
    candidates: {},
    busy: false,
    autoSaveTimer: null,
    autoSavePending: false,
    autoSaveFailed: false,
    /* #7：文本输入只有 change / blur / Enter / 「应用」才保存 */
    pendingRoles: {},
    saveStates: {},
    saveErrors: {},
    candidateExpanded: {}
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
    var defaultParent = role === "main" ? "dialogue" : "main";
    var inheritFrom = "";
    if (hasOwn(raw, "inherit_from") && raw.inherit_from != null) {
      inheritFrom = str(raw.inherit_from).trim();
      if (legal.indexOf(inheritFrom) < 0) {
        inheritFrom = toBool(raw.inherit, false) ? defaultParent : "";
      }
    } else if (hasOwn(raw, "inherit")) {
      inheritFrom = toBool(raw.inherit, true) ? defaultParent : "";
    } else {
      inheritFrom = defaultParent;
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

  function normalizeVectorRole(raw) {
    raw = raw && typeof raw === "object" ? raw : {};
    var mode = str(raw.mode) || "off";
    if (["off", "source", "provider"].indexOf(mode) < 0) mode = "off";
    return {
      mode: mode,
      provider_id: str(raw.provider_id),
      source_id: str(raw.source_id),
      model: str(raw.model)
    };
  }

  function normalizeDecisionFields(raw) {
    raw = raw && typeof raw === "object" ? raw : {};
    var mode = str(raw.mode) || "chat";
    if (DECISION_MODES.indexOf(mode) < 0) mode = "chat";
    var threshold = clamp(num(raw.threshold, 0.5), 0, 1);
    var endpoint = str(raw.endpoint);
    if (endpoint && !/^https?:\/\//i.test(endpoint)) endpoint = "";
    return { mode: mode, threshold: threshold, endpoint: endpoint };
  }

  function normalizeConfig(raw) {
    raw = raw && typeof raw === "object" ? raw : {};
    /* 兼容旧布局：没有 dialogue 时把旧 main 当作对话模型 */
    var dialogue =
      raw.dialogue && typeof raw.dialogue === "object"
        ? raw.dialogue
        : raw.main && typeof raw.main === "object"
        ? raw.main
        : {};
    var decisionBase = normalizeChatRole("decision", raw.decision);
    var decisionExtra = normalizeDecisionFields(raw.decision);
    decisionBase.mode = decisionExtra.mode;
    decisionBase.threshold = decisionExtra.threshold;
    decisionBase.endpoint = decisionExtra.endpoint;
    var tts = raw.tts && typeof raw.tts === "object" ? raw.tts : {};
    var ttsMode = str(tts.mode) || "off";
    if (["off", "source", "provider"].indexOf(ttsMode) < 0) ttsMode = "off";
    var ttsScope = str(tts.scope) || "private";
    if (["private", "l3", "all"].indexOf(ttsScope) < 0) ttsScope = "private";
    return {
      dialogue: { source_id: str(dialogue.source_id), model: str(dialogue.model) },
      main: normalizeChatRole("main", raw.main),
      decision: decisionBase,
      vision: normalizeChatRole("vision", raw.vision),
      embedding: normalizeVectorRole(raw.embedding),
      rerank: normalizeVectorRole(raw.rerank),
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
    if (VECTOR_ROLES.indexOf(role) >= 0) {
      if (!info.mode || info.mode === "off") {
        return {
          cls: "light-off",
          text: "已关闭",
          tip: info.label || (role === "embedding" ? "未启用向量检索，降级为词法检索" : "未启用精排")
        };
      }
      if (info.configured === false || info.ok === false) {
        return { cls: "light-err", text: "未配置完整", tip: info.label || "请补全提供商源 / 模型" };
      }
      return {
        cls: "light-ok",
        text: info.mode === "provider" ? "复用 AstrBot 实例" : "插件自建实例",
        tip: info.label || "",
        wrap: true
      };
    }
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
    if (role === "decision" && info.decision_mode === "judge") {
      if (info.judge_ready) {
        return {
          cls: "light-ok",
          text: "判断模型",
          tip:
            (info.label || "") +
            (info.model ? " · " + str(info.model) : "") +
            (info.endpoint ? " · " + str(info.endpoint) : "")
        };
      }
      return {
        cls: "light-err",
        text: "判断模型未就绪",
        tip: info.label || "请补全判断模型与 API Key（提供商源或 OPENROUTER_API_KEY）"
      };
    }
    if (info.configured === false || info.mode === "unconfigured") {
      return { cls: "light-err", text: "未配置", tip: info.label || "未配置" };
    }
    if (info.inherited) {
      var cfg = S.cfg && S.cfg[role] ? S.cfg[role] : {};
      var fromRole = str(info.inherit_from) || str(cfg.inherit_from) || "main";
      var pair = pairLabel(info.source_id, info.model);
      var base =
        fromRole === "dialogue"
          ? "继承自对话模型"
          : fromRole === "decision"
          ? "继承自决策模型"
          : "继承自主模型";
      return {
        cls: info.ok === false ? "light-err" : "light-warn",
        text: base,
        tip: (info.label || base) + (pair ? " · " + pair : "")
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

  function sourceOptionsHtml(selectedId, withEmpty, includeAll, providerType) {
    var html = withEmpty ? '<option value="">— 选择提供商源 —</option>' : "";
    var list = includeAll ? S.sources : chatSources();
    if (providerType) {
      list = S.sources.filter(function (source) {
        return str(source.provider_type) === str(providerType);
      });
    }
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
    } else if (role === "dialogue") {
      helper =
        "对话模型只负责聊天回复，建议选稳定、响应快的模型；可直接填写模型名，或点「拉取」读取候选模型，修改后自动保存并即时生效。";
    } else if (role === "main") {
      helper =
        "主模型负责 AI 造卡 / 记忆提炼 / 主动消息等后台生成，建议选能力更强的模型；可直接填写模型名，或点「拉取」读取候选模型。";
    } else {
      helper =
        "独立配置：使用本卡片的提供商源与模型；留空表示停用该角色（不会回退）。可直接填写模型名，或点「拉取」读取候选模型；修改后自动保存并即时生效。";
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
    /* #24：随模型输入框关键词实时过滤；默认只渲染前 40 个并准确说明总数。 */
    var keyword = current.trim().toLowerCase();
    var all = models.filter(function (item) {
      var model = str(item && item.model);
      if (!model) return false;
      return !keyword || model.toLowerCase().indexOf(keyword) >= 0;
    });
    var expanded = !!S.candidateExpanded[role];
    var visible = expanded ? all : all.slice(0, 40);
    var buttons = visible
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
      .join("");
    var footer = all.length
      ? '<span class="hint">' +
        (expanded ? "共 " + all.length : "显示前 " + visible.length + " / 共 " + all.length) +
        " 个候选，点击填入</span>"
      : '<span class="hint">没有匹配「' + esc(current) + "」的候选模型，可以直接手动输入。</span>";
    var toggle =
      all.length > 40
        ? '<button class="btn btn-ghost btn-xs" type="button" data-action="expand-candidates" data-role-key="' +
          esc(role) +
          '">' +
          (expanded ? "收起候选" : "展开全部 " + all.length + " 个") +
          "</button>"
        : "";
    return buttons + footer + toggle;
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
   * 继承则递归取父角色（vision -> main -> dialogue，最多 2 跳，防环）。
   */
  function effectivePair(role, depth) {
    depth = depth || 0;
    if (!S.cfg || depth > 2) return { source_id: "", model: "" };
    if (role === "dialogue") return pairOf(S.cfg.dialogue);
    var entry = S.cfg[role];
    if (!entry) return { source_id: "", model: "" };
    var fromRole = str(entry.inherit_from);
    if (!fromRole) return pairOf(entry);
    if (fromRole === "dialogue") return pairOf(S.cfg.dialogue);
    return effectivePair(fromRole, depth + 1);
  }

  function sourceModeHtml(role) {
    var cfg = S.cfg[role] || {};
    var selected = str(cfg.inherit_from);
    var descMap = {
      main:
        "独立配置：让主模型使用专属的更强模型；继承（默认）：与对话模型保持一致。主模型负责所有后台生成，不影响聊天回复。",
      decision:
        "独立配置：为读空气单独指定模型；继承（默认）：复用主模型。判断模型建议独立配置，避免把聊天模型名当成 Decisions 模型。",
      vision:
        "独立配置：使用本卡片的提供商源与模型，留空即停用该角色；继承：直接复用父角色当前生效的模型（要求模型支持图片输入）。"
    };
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
      esc(descMap[role] || "") +
      "</div></div>"
    );
  }

  function inheritedPreviewHtml(role) {
    var cfg = S.cfg[role] || {};
    var fromRole = str(cfg.inherit_from) || "main";
    var parent = effectivePair(fromRole);
    var configured = !!(parent.source_id && parent.model);
    var parentName =
      fromRole === "dialogue"
        ? "对话模型"
        : fromRole === "decision"
        ? "决策模型"
        : "主模型";
    var chainNote = "";
    var parentCfg = S.cfg[fromRole] || {};
    if (fromRole !== "dialogue" && str(parentCfg.inherit_from)) {
      var grand =
        str(parentCfg.inherit_from) === "dialogue" ? "对话模型" : "主模型";
      chainNote = "（" + parentName + "当前继承自" + grand + "）";
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

  function decisionModeOf() {
    var cfg = (S.cfg && S.cfg.decision) || {};
    return str(cfg.mode) === "judge" ? "judge" : "chat";
  }

  function decisionModeHtml() {
    var mode = decisionModeOf();
    return (
      '<div class="field"><span class="field-label">判断方式</span>' +
      '<div class="segmented" data-role="decision-mode-seg">' +
      [
        { value: "chat", label: "通用模型（JSON 判定）" },
        { value: "judge", label: "判断模型（Jev / Decisions API）" }
      ]
        .map(function (item) {
          return (
            '<button type="button" class="seg-btn' +
            (mode === item.value ? " active" : "") +
            '" data-action="set-decision-mode" data-mode="' +
            esc(item.value) +
            '">' +
            esc(item.label) +
            "</button>"
          );
        })
        .join("") +
      '</div><div class="field-help">通用模型：任意聊天模型，读空气 Prompt 输出 JSON；判断模型：OpenRouter Decisions API（Jev 等 System-One 模型）返回概率，不生成文本。</div></div>'
    );
  }

  function decisionThresholdHtml(role) {
    var cfg = S.cfg[role] || {};
    var threshold = clamp(num(cfg.threshold, 0.5), 0, 1);
    return (
      '<div class="field mt-12"><span class="field-label">回复阈值（概率）</span>' +
      '<div class="slider-row">' +
      '<input class="slider" type="range" min="0" max="1" step="0.05" data-decision-field="threshold" value="' +
      threshold +
      '" />' +
      '<span class="slider-value" data-role="decision-threshold-value">' +
      Math.round(threshold * 100) +
      "%</span></div>" +
      '<div class="field-help">判断模型给出 REPLY 的概率达到该阈值才会接话，默认 50%。调高更克制，调低更活跃。</div></div>'
    );
  }

  function decisionJudgeHtml(role) {
    var cfg = S.cfg[role] || {};
    if (str(cfg.inherit_from)) {
      return (
        inheritedPreviewHtml(role) +
        '<div class="card mt-12" style="background:var(--warning-soft,var(--surface-2));border:0"><div class="card-body tight">' +
        '<div class="text-sm">判断模型需要独立指定模型名（继承来的聊天模型通常不是 Decisions 模型）。</div>' +
        '<button class="btn btn-outline btn-sm mt-8" type="button" data-action="make-decision-independent" data-role-key="' +
        esc(role) +
        '">' +
        icon("link", 13) +
        "改为独立配置</button></div></div>"
      );
    }
    var datalist = DECISION_MODEL_SUGGESTIONS.map(function (model) {
      return '<option value="' + esc(model) + '"></option>';
    }).join("");
    return (
      '<div class="field"><span class="field-label">提供商源（用于 API Key）</span>' +
      '<select class="select" data-model-role="' +
      esc(role) +
      '" data-model-field="source_id">' +
      sourceOptionsHtml(cfg.source_id, true) +
      "</select>" +
      '<div class="field-help">源可留空：留空时读取环境变量 <code>OPENROUTER_API_KEY</code>；Jev 等模型需选择 OpenRouter 或兼容 Decisions API 的网关。判断模型不会创建聊天实例。</div></div>' +
      '<div class="field mt-12"><span class="field-label">判断模型名</span>' +
      '<input class="input" type="text" list="decision-model-options" data-model-role="' +
      esc(role) +
      '" data-model-field="model" value="' +
      esc(cfg.model) +
      '" placeholder="~typesafe/jev-latest" />' +
      '<datalist id="decision-model-options">' +
      datalist +
      "</datalist>" +
      '<div class="field-help">Decisions 模型示例：<code>~typesafe/jev-latest</code>、<code>typesafe/jev-1.14</code>，或 GPT-6 Luna / Clef / D1；通用模型请切回「通用模型（JSON 判定）」，默认决策模型继承主模型。</div></div>' +
      decisionThresholdHtml(role)
    );
  }

  function roleDetailsHtml(role) {
    if (role === "dialogue") return roleControlsHtml(role);
    var cfg = S.cfg[role] || {};
    if (role === "decision" && decisionModeOf() === "judge") {
      return decisionJudgeHtml(role);
    }
    if (str(cfg.inherit_from)) return inheritedPreviewHtml(role);
    return roleControlsHtml(role);
  }

  function saveStateMeta(role) {
    var state = S.saveStates[role] || "saved";
    var pending = !!S.pendingRoles[role];
    if (state === "error") {
      return {
        cls: "is-error",
        text: S.saveErrors[role] || "保存失败 · 点击应用重试"
      };
    }
    if (state === "saving" || S.autoSavePending) {
      return { cls: "is-saving", text: "保存中…" };
    }
    if (pending) return { cls: "is-pending", text: "待应用" };
    return { cls: "is-saved", text: "已生效" };
  }

  function cardSaveToolsHtml(role) {
    var meta = saveStateMeta(role);
    return (
      '<div class="model-save-tools">' +
      '<span class="model-save-badge ' +
      meta.cls +
      '" data-role="model-save-' +
      esc(role) +
      '">' +
      esc(meta.text) +
      "</span>" +
      '<button class="btn btn-outline btn-xs" type="button" data-action="apply-role" data-role-key="' +
      esc(role) +
      '">' +
      icon("check", 12) +
      "应用</button></div>"
    );
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
      (role === "dialogue" ? '<span class="tag tag-danger" style="margin-left:6px">必填</span>' : "") +
      '</span><span class="model-role-desc">' +
      esc(meta.desc) +
      "</span></div></div>" +
      cardSaveToolsHtml(role) +
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
      (role === "decision" ? decisionModeHtml(role) : "") +
      (role === "dialogue" || (role === "decision" && decisionModeOf() === "judge")
        ? ""
        : sourceModeHtml(role)) +
      '<div data-role="role-controls-' +
      esc(role) +
      '"' +
      (role === "dialogue" ? "" : ' class="mt-12"') +
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
   * v2.12 向量 / 重排模型卡片（RAG 记忆检索）
   * ------------------------------------------------------------------ */
  function vectorProviders(role) {
    return role === "embedding" ? S.embeddingProviders : S.rerankProviders;
  }

  function vectorMeta(role) {
    return (S.cfg && S.cfg[role]) || { mode: "off", provider_id: "", source_id: "", model: "" };
  }

  function vectorDetailHtml(role) {
    var cfg = vectorMeta(role);
    var mode = str(cfg.mode) || "off";
    var isEmbedding = role === "embedding";
    var roleName = isEmbedding ? "向量模型" : "重排模型";
    if (mode === "off") {
      return (
        '<div class="card" style="background:var(--surface-2);border:0"><div class="card-body tight">' +
        '<div class="text-sm text-2">' +
        (isEmbedding
          ? "向量模型已关闭：记忆召回自动降级为「content + keywords」词法检索，功能可用但语义理解较弱。"
          : "重排模型已关闭：召回只按向量 / 词法相关性粗排，不调用精排。") +
        "</div></div></div>"
      );
    }
    if (mode === "source") {
      return (
        '<div class="field"><span class="field-label">提供商源（' +
        esc(roleName) +
        '）</span><select class="select" data-vector-role="' +
        esc(role) +
        '" data-vector-field="source_id">' +
        sourceOptionsHtml(cfg.source_id, true, false, role) +
        "</select>" +
        (S.sources.filter(function (source) {
          return str(source.provider_type) === role;
        }).length === 0
          ? '<div class="field-help warn-text">还没有 ' +
            (isEmbedding ? "嵌入 (Embedding)" : "重排 (Rerank)") +
            " 类型的提供商源：请先在 AstrBot WebUI → 服务提供商里添加。</div>"
          : "") +
        "</div>" +
        '<div class="field mt-12"><span class="field-label">模型名</span>' +
        '<div class="row"><input class="input grow" type="text" data-vector-role="' +
        esc(role) +
        '" data-vector-field="model" value="' +
        esc(cfg.model) +
        '" placeholder="' +
        (isEmbedding ? "例如：BAAI/bge-m3" : "例如：BAAI/bge-reranker-v2-m3") +
        '" />' +
        '<button class="btn btn-outline btn-sm" type="button" data-action="pull-models" data-role-key="' +
        esc(role) +
        '">' +
        icon("refresh", 13) +
        "拉取</button></div>" +
        '<div class="row wrap mt-4" data-role="candidates-' +
        esc(role) +
        '">' +
        candidatesHtml(role) +
        "</div>" +
        '<div class="field-help">插件会按 (提供商源, 模型) 复用已加载实例，否则自建 ' +
        esc(role) +
        " 实例；修改后自动保存并即时生效。</div></div>"
      );
    }
    var options = '<option value="">— 选择 AstrBot ' + esc(roleName) + " —</option>";
    var matched = false;
    vectorProviders(role).forEach(function (item) {
      var label =
        (item.id || "") +
        (item.model ? " · " + item.model : "") +
        (item.type ? " · " + item.type : "");
      if (item.enable === false) label += "（未启用）";
      if (String(cfg.provider_id) === String(item.id)) matched = true;
      options +=
        '<option value="' +
        esc(item.id) +
        '"' +
        (String(cfg.provider_id) === String(item.id) ? " selected" : "") +
        ">" +
        esc(label) +
        "</option>";
    });
    if (cfg.provider_id && !matched) {
      options +=
        '<option value="' +
        esc(cfg.provider_id) +
        '" selected>' +
        esc(cfg.provider_id) +
        "（提供商不存在）</option>";
    }
    return (
      '<div class="field"><span class="field-label">AstrBot ' +
      esc(roleName) +
      ' 提供商</span><select class="select" data-vector-role="' +
      esc(role) +
      '" data-vector-field="provider_id">' +
      options +
      "</select>" +
      '<div class="field-help">复用 AstrBot WebUI → 服务提供商里已启用的' +
      (isEmbedding ? "嵌入" : "重排") +
      "实例；关闭卡片即停用该角色。</div></div>"
    );
  }

  function vectorCardHtml(role) {
    var meta = roleMeta(role);
    var info = statusInfo(role);
    var cfg = vectorMeta(role);
    var mode = str(cfg.mode) || "off";
    return (
      '<section class="card model-card" data-role-card="' +
      role +
      '"><div class="card-head">' +
      '<div class="model-role"><span class="model-role-icon">' +
      icon(meta.icon, 15) +
      "</span>" +
      '<div class="model-role-text"><span class="model-role-name">' +
      esc(meta.name) +
      '</span><span class="model-role-desc">' +
      esc(meta.desc) +
      "</span></div></div>" +
      cardSaveToolsHtml(role) +
      '<div class="model-status" data-tip="' +
      esc(info.tip) +
      '"><span class="status-light ' +
      info.cls +
      '"></span><span>' +
      esc(info.text) +
      "</span></div></div>" +
      '<div class="card-body"><div class="field"><span class="field-label">模型来源模式</span>' +
      '<div class="segmented" data-role="vector-mode-seg-' +
      esc(role) +
      '">' +
      VECTOR_MODES.map(function (item) {
        return (
          '<button type="button" class="seg-btn' +
          (mode === item.value ? " active" : "") +
          '" data-action="set-vector-mode" data-vector-role="' +
          esc(role) +
          '" data-mode="' +
          esc(item.value) +
          '">' +
          esc(item.label) +
          "</button>"
        );
      }).join("") +
      '</div><div class="field-help">关闭：降级 / 不调用；提供商源：按 (源, 模型) 自建或复用；AstrBot 提供商：直接复用平台已启用实例。</div></div>' +
      '<div data-role="vector-detail-' +
      esc(role) +
      '" class="mt-12">' +
      vectorDetailHtml(role) +
      "</div>" +
      '<div class="row mt-12"><button class="btn btn-outline btn-sm" type="button" data-action="test-role" data-role-key="' +
      esc(role) +
      '">' +
      icon("zap", 13) +
      "测试连接</button>" +
      '<span class="hint">向量模型返回维度 / 重排模型返回打分，验证 RAG 链路。</span></div>' +
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
      cardSaveToolsHtml("tts") +
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
  function dialogueUnconfigured() {
    var info = statusOf("dialogue");
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
      '<span class="text-xs text-2" style="flex:1;min-width:200px">模型配置按角色独立保存；在顶栏切换角色后会自动加载该角色自己的对话 / 主 / 决策 / 视觉模型配置。</span>' +
      "</div></div></div>"
    );
  }

  function bannerHtml() {
    if (!dialogueUnconfigured()) return "";
    return (
      '<div class="card" style="border-color:var(--danger)"><div class="card-body tight">' +
      '<div class="row wrap">' +
      '<span class="status-light light-err"></span>' +
      '<strong class="text-sm">对话模型未配置</strong>' +
      '<span class="text-sm text-2">插件会回退到 AstrBot 平台默认模型；对话回复仍可用，但按角色绑定对话模型的隔离会失效。请在下方「对话模型」卡片选择提供商源与模型（修改即时生效）。</span>' +
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
      " · 对话 / 主 / 决策 / 视觉模型按角色独立配置 · 修改即时生效</span></h1></div>" +
      soulBannerHtml() +
      bannerHtml() +
      '<div class="model-grid model-grid-chat">' +
      CHAT_ROLES.map(chatCardHtml).join("") +
      "</div>" +
      '<div class="model-grid mt-12">' +
      VECTOR_ROLES.map(vectorCardHtml).join("") +
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
    CHAT_ROLES.concat(["tts"]).concat(VECTOR_ROLES).forEach(function (role) {
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
    refreshSaveBadges();
    S.autoSaveTimer = setTimeout(function () {
      S.autoSaveTimer = null;
      S.autoSavePending = false;
      autoSaveConfig();
    }, 700);
  }

  /** 保存前完整性校验（#7）：选了源却没填模型时不要向后端发送重建请求。 */
  function validateRole(role) {
    if (!S.cfg) return "";
    if (CHAT_ROLES.indexOf(role) >= 0) {
      var cfg = S.cfg[role] || {};
      if (role === "decision" && str(cfg.mode) === "judge") {
        if (!str(cfg.model)) return "请填写判断模型名称（如 ~typesafe/jev-latest）";
        return "";
      }
      if (str(cfg.inherit_from)) return "";
      if (str(cfg.source_id) && !str(cfg.model)) return "请填写或选择模型名称";
      return "";
    }
    if (VECTOR_ROLES.indexOf(role) >= 0) {
      var vector = vectorMeta(role);
      if (str(vector.mode) === "source" && str(vector.source_id) && !str(vector.model)) {
        return "请填写或选择模型名称";
      }
      return "";
    }
    if (role === "tts") {
      var tts = ttsMeta();
      if (str(tts.mode) === "source" && str(tts.source_id) && !str(tts.model)) {
        return "请填写 TTS 模型名称";
      }
      return "";
    }
    return "";
  }

  function firstValidationError() {
    var roles = CHAT_ROLES.concat(VECTOR_ROLES, ["tts"]);
    for (var i = 0; i < roles.length; i++) {
      var error = validateRole(roles[i]);
      if (error) return { role: roles[i], message: error };
    }
    return null;
  }

  function refreshSaveBadges() {
    if (!S.container) return;
    CHAT_ROLES.concat(["tts"])
      .concat(VECTOR_ROLES)
      .forEach(function (role) {
        var el = S.container.querySelector('[data-role="model-save-' + role + '"]');
        if (!el) return;
        var meta = saveStateMeta(role);
        el.className = "model-save-badge " + meta.cls;
        el.textContent = meta.text;
      });
  }

  /** 「应用」按钮 / Enter / change：提交一个角色的文本编辑并触发保存。 */
  function commitRole(role) {
    if (!S.cfg) return;
    var error = validateRole(role);
    if (error) {
      S.saveStates[role] = "error";
      S.saveErrors[role] = error;
      refreshSaveBadges();
      if (S.app) S.app.toast(error, true);
      return false;
    }
    delete S.pendingRoles[role];
    delete S.saveErrors[role];
    S.saveStates[role] = "saved";
    refreshSaveBadges();
    scheduleAutoSave();
    return true;
  }

  async function autoSaveConfig() {
    var app = S.app;
    if (!app || !S.cfg || S.destroyed) return;
    if (S.busy) {
      scheduleAutoSave();
      return;
    }
    var invalid = firstValidationError();
    if (invalid) {
      S.saveStates[invalid.role] = "error";
      S.saveErrors[invalid.role] = invalid.message;
      S.autoSaveFailed = true;
      refreshSaveBadges();
      if (app) app.toast(invalid.message, true);
      return;
    }
    if (!computeDirty()) {
      S.autoSaveFailed = false;
      refreshSaveBadges();
      return;
    }
    var payload = clone(S.cfg);
    var payloadJson = JSON.stringify(normalizeConfig(payload));
    S.busy = true;
    CHAT_ROLES.concat(["tts"]).concat(VECTOR_ROLES).forEach(function (role) {
      S.saveStates[role] = "saving";
    });
    refreshSaveBadges();
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
      /* 已成功落盘的文本编辑不再显示「待应用」 */
      Object.keys(S.pendingRoles || {}).forEach(function (role) {
        if (JSON.stringify(S.cfg[role]) === JSON.stringify(S.savedCfg[role])) {
          delete S.pendingRoles[role];
        }
      });
      if (data && data.soul_id) S.soulId = String(data.soul_id);
      S.status =
        data && data.status && typeof data.status === "object" ? data.status : S.status;
      if (app.applyModelStatus) app.applyModelStatus(S.status);
      S.autoSaveFailed = false;
      CHAT_ROLES.concat(["tts"]).concat(VECTOR_ROLES).forEach(function (role) {
        S.saveStates[role] = "saved";
        delete S.saveErrors[role];
      });
      refreshStatusBadges();
      refreshSaveBadges();
      /* #7：成功不再每次刷屏 toast，状态徽章已经展示「已生效」 */
    } catch (err) {
      S.autoSaveFailed = true;
      CHAT_ROLES.concat(["tts"]).concat(VECTOR_ROLES).forEach(function (role) {
        S.saveStates[role] = "error";
      });
      refreshSaveBadges();
      if (app) app.toast("模型配置保存失败：" + (err.message || err), true);
    } finally {
      S.busy = false;
      if (
        S.app &&
        !S.autoSaveFailed &&
        computeDirty() &&
        !Object.keys(S.pendingRoles).length
      ) {
        scheduleAutoSave();
      }
    }
  }

  /** 父角色表单值变化后，刷新所有处于继承模式的角色来源快照 */
  function refreshInheritPreviews() {
    if (!S.cfg) return;
    ["main", "decision", "vision"].forEach(function (role) {
      var entry = S.cfg[role];
      if (entry && str(entry.inherit_from)) refreshRoleControls(role);
    });
  }

  function refreshVectorDetail(role) {
    if (!S.container) return;
    var detail = S.container.querySelector('[data-role="vector-detail-' + role + '"]');
    if (detail) detail.innerHTML = vectorDetailHtml(role);
    var seg = S.container.querySelector('[data-role="vector-mode-seg-' + role + '"]');
    if (seg) {
      var mode = str(vectorMeta(role).mode) || "off";
      seg.querySelectorAll(".seg-btn").forEach(function (btn) {
        btn.classList.toggle("active", btn.getAttribute("data-mode") === mode);
      });
    }
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
    S.embeddingProviders = Array.isArray(sourcesData.embedding_providers)
      ? sourcesData.embedding_providers
      : [];
    S.rerankProviders = Array.isArray(sourcesData.rerank_providers)
      ? sourcesData.rerank_providers
      : [];
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
    } else if (role === "embedding") {
      html =
        '<div class="model-test-result is-ok">' +
        icon("check", 13) +
        " 向量连通 · 延迟 " +
        esc(num(data.latency_ms, 0)) +
        " ms · 维度 " +
        esc(num(data.dimension, 0)) +
        (data.provider_id ? " · " + esc(str(data.provider_id)) : "") +
        (data.model ? " / " + esc(str(data.model)) : "") +
        "</div>";
    } else if (role === "rerank") {
      var rerankRows = Array.isArray(data.results) ? data.results : [];
      html =
        '<div class="model-test-result is-ok">' +
        icon("check", 13) +
        " 重排连通 · 延迟 " +
        esc(num(data.latency_ms, 0)) +
        " ms" +
        (rerankRows.length
          ? '<div class="text-xs mt-4">' +
            rerankRows
              .map(function (row) {
                return "#" + esc(num(row.index, 0)) + " → " + esc(num(row.score, 0).toFixed(4));
              })
              .join(" · ") +
            "</div>"
          : "") +
        "</div>";
    } else if (role === "decision" && str(data.mode) === "judge") {
      var judgeConfidence = data.confidence == null ? null : Math.round(num(data.confidence, 0) * 100);
      var judgeThreshold = data.threshold == null ? null : Math.round(num(data.threshold, 0) * 100);
      html =
        '<div class="model-test-result is-ok">' +
        icon("check", 13) +
        " 判断模型连通 · 延迟 " +
        esc(num(data.latency_ms, 0)) +
        " ms · 结果 " +
        esc(str(data.decision) || "-") +
        (judgeConfidence == null ? "" : "（" + esc(judgeConfidence) + "%）") +
        (judgeThreshold == null ? "" : " · 阈值 " + esc(judgeThreshold) + "%") +
        (data.model ? '<div class="text-xs mt-4">' + esc(str(data.model)) + (data.provider ? " · " + esc(str(data.provider)) : "") + "</div>" : "") +
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
    } else if (action === "set-vector-mode") {
      var vectorRole = actionEl.getAttribute("data-vector-role");
      var vectorMode = actionEl.getAttribute("data-mode") || "off";
      if (!vectorRole || !S.cfg[vectorRole]) return;
      if (["off", "source", "provider"].indexOf(vectorMode) < 0) vectorMode = "off";
      S.cfg[vectorRole].mode = vectorMode;
      refreshVectorDetail(vectorRole);
      scheduleAutoSave();
    } else if (action === "set-decision-mode") {
      var nextMode = actionEl.getAttribute("data-mode") || "chat";
      if (DECISION_MODES.indexOf(nextMode) < 0) nextMode = "chat";
      if (!S.cfg.decision) return;
      S.cfg.decision.mode = nextMode;
      if (nextMode === "judge" && str(S.cfg.decision.inherit_from)) {
        /* 判断模式必须显式指定判断模型，但绝不自动绑定 OpenRouter 或 Jev：
           清空继承来的聊天模型，交给用户自己选源 / 填模型（源可留空走环境变量）。 */
        S.cfg.decision.inherit = false;
        S.cfg.decision.inherit_from = "";
        S.cfg.decision.source_id = "";
        S.cfg.decision.model = "";
      }
      paint();
      scheduleAutoSave();
    } else if (action === "make-decision-independent") {
      if (!S.cfg.decision) return;
      var inheritedPair = effectivePair("decision");
      S.cfg.decision.inherit = false;
      S.cfg.decision.inherit_from = "";
      /* 保留当前生效的源 / 模型，只解除继承；不擅自换成 Jev / OpenRouter。 */
      if (!str(S.cfg.decision.source_id) && str(inheritedPair.source_id)) {
        S.cfg.decision.source_id = inheritedPair.source_id;
      }
      if (!str(S.cfg.decision.model) && str(inheritedPair.model)) {
        S.cfg.decision.model = inheritedPair.model;
      }
      paint();
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
    } else if (action === "apply-role") {
      var applyRole = actionEl.getAttribute("data-role-key");
      if (applyRole && commitRole(applyRole)) {
        S.app && S.app.toast("已应用「" + roleMeta(applyRole).name + "」配置");
      }
    } else if (action === "expand-candidates") {
      var candRole = actionEl.getAttribute("data-role-key");
      if (candRole) {
        S.candidateExpanded[candRole] = !S.candidateExpanded[candRole];
        var wrap = S.container
          ? S.container.querySelector('[data-role="candidates-' + candRole + '"]')
          : null;
        if (wrap) wrap.innerHTML = candidatesHtml(candRole);
      }
    }
  }

  function isTextControl(el) {
    var tag = String((el && el.tagName) || "").toLowerCase();
    if (tag === "textarea") return true;
    if (tag !== "input") return false;
    var type = String(el.type || "text").toLowerCase();
    return (
      type === "text" ||
      type === "password" ||
      type === "number" ||
      type === "search" ||
      type === "url"
    );
  }

  function rerenderCandidates(role) {
    if (!S.container) return;
    var wrap = S.container.querySelector('[data-role="candidates-' + role + '"]');
    if (wrap) wrap.innerHTML = candidatesHtml(role);
  }

  function handleFieldChange(event) {
    var el = event.target;
    if (!el || !el.getAttribute) return;
    var commit = event.type === "change" || event.type === "blur";
    var textControl = isTextControl(el);

    /* 模型字段（提供商源 / 模型名）用 data-model-role 标记；来源模式用 data-role-key */
    var roleKey =
      el.getAttribute("data-role-key") || el.getAttribute("data-model-role");
    if (el.getAttribute("data-role") === "inherit-from") {
      if (!roleKey || !S.cfg[roleKey]) return;
      var inheritFrom = str(el.value);
      if (legalInheritValues(roleKey).indexOf(inheritFrom) < 0) inheritFrom = "";
      S.cfg[roleKey].inherit_from = inheritFrom;
      S.cfg[roleKey].inherit = !!inheritFrom;
      delete S.pendingRoles[roleKey];
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
        rerenderCandidates(roleKey);
        scheduleAutoSave();
        return;
      }
      if (modelField === "model") {
        rerenderCandidates(roleKey);
        /* #7：文本输入不再边打字边保存，只有 change / blur / Enter / 应用才提交 */
        if (textControl && !commit) {
          S.pendingRoles[roleKey] = true;
          S.saveStates[roleKey] = "pending";
          refreshSaveBadges();
          return;
        }
        refreshInheritPreviews();
        commitRole(roleKey);
        return;
      }
      refreshInheritPreviews();
      scheduleAutoSave();
      return;
    }
    var vectorRole = el.getAttribute("data-vector-role");
    var vectorField = el.getAttribute("data-vector-field");
    if (vectorRole && vectorField && S.cfg[vectorRole]) {
      S.cfg[vectorRole][vectorField] = String(el.value == null ? "" : el.value);
      if (vectorField === "source_id") {
        S.candidates[vectorRole] = null;
        rerenderCandidates(vectorRole);
        scheduleAutoSave();
        return;
      }
      if (vectorField === "model") {
        rerenderCandidates(vectorRole);
        if (textControl && !commit) {
          S.pendingRoles[vectorRole] = true;
          S.saveStates[vectorRole] = "pending";
          refreshSaveBadges();
          return;
        }
        commitRole(vectorRole);
        return;
      }
      scheduleAutoSave();
      return;
    }
    var decisionField = el.getAttribute("data-decision-field");
    if (decisionField === "threshold" && S.cfg.decision) {
      S.cfg.decision.threshold = clamp(el.value, 0, 1);
      var thresholdEl = S.container
        ? S.container.querySelector('[data-role="decision-threshold-value"]')
        : null;
      if (thresholdEl) {
        thresholdEl.textContent = Math.round(S.cfg.decision.threshold * 100) + "%";
      }
      scheduleAutoSave();
      return;
    }
    var ttsField = el.getAttribute("data-tts-field");
    if (ttsField) {
      if (!S.cfg.tts) S.cfg.tts = normalizeConfig({}).tts;
      if (ttsField === "dual_output") {
        S.cfg.tts.dual_output = !!el.checked;
        scheduleAutoSave();
      } else if (ttsField === "probability") {
        S.cfg.tts.probability = clamp(el.value, 0, 1);
        var valueEl = S.container
          ? S.container.querySelector('[data-role="probability-value"]')
          : null;
        if (valueEl) valueEl.textContent = Math.round(S.cfg.tts.probability * 100) + "%";
        if (commit) scheduleAutoSave();
      } else {
        S.cfg.tts[ttsField] = String(el.value == null ? "" : el.value);
        if (textControl && !commit) {
          S.pendingRoles.tts = true;
          S.saveStates.tts = "pending";
          refreshSaveBadges();
          return;
        }
        commitRole("tts");
      }
    }
  }

  function bindEvents(container, app) {
    container.addEventListener("click", handleClick);
    container.addEventListener("input", handleFieldChange);
    container.addEventListener("change", handleFieldChange);
    container.addEventListener("blur", handleFieldChange, true);
    /* Enter = 立即应用当前卡片的文本编辑 */
    container.addEventListener("keydown", function (event) {
      if (event.key !== "Enter") return;
      var el = event.target;
      if (!el || !isTextControl(el)) return;
      var role =
        el.getAttribute("data-model-role") ||
        el.getAttribute("data-vector-role") ||
        (el.getAttribute("data-tts-field") ? "tts" : "");
      if (!role) return;
      event.preventDefault();
      commitRole(role);
    });
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
    S.embeddingProviders = [];
    S.rerankProviders = [];
    S.soulId = String((app && app.state && app.state.currentSoulId) || "");
    S.cfg = null;
    S.savedCfg = null;
    S.status = {};
    S.candidates = {};
    S.busy = false;
    S.autoSaveTimer = null;
    S.autoSavePending = false;
    S.autoSaveFailed = false;
    S.pendingRoles = {};
    S.saveStates = {};
    S.saveErrors = {};
    S.candidateExpanded = {};

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
    S.pendingRoles = {};
    S.saveStates = {};
    S.saveErrors = {};
    S.candidateExpanded = {};
    if (S._keydownHandler) {
      document.removeEventListener("keydown", S._keydownHandler);
      S._keydownHandler = null;
    }
    S.app = null;
    S.container = null;
    S.sources = [];
    S.ttsProviders = [];
    S.embeddingProviders = [];
    S.rerankProviders = [];
    S.soulId = "";
    S.cfg = null;
    S.savedCfg = null;
    S.status = {};
    S.candidates = {};
    S.busy = false;
  }

  window.OY_PAGES = window.OY_PAGES || {};
  function isDirty() {
    /* #7：存在未应用的文本编辑 / 保存中 / 保存失败都提示离开确认。 */
    if (Object.keys(S.pendingRoles || {}).length > 0) return true;
    return !!S.autoSavePending || !!S.autoSaveFailed || computeDirty();
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
