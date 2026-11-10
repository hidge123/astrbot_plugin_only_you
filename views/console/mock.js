/**
 * OnlyYou Plugin Pages — 本地离线 Mock bridge (`views/console/mock.js`)
 *
 * 仅当 URL 带 `?mock=1`（或 `location.protocol === "file:"` 且页面没有真实 bridge）时生效：
 * 安装 `window.AstrBotPluginPage`（兼容 `window.AstrBotPluginView`），覆盖
 * **全部后端路由（17 条旧路由 + 10 条新路由，见 .team/NEW_TIMELINE_CONTRACT.md §6）**，
 * 返回与真实后端一致的信封结构 `{status, message, data}`，并带 300ms 模拟延迟。
 *
 * 数据形状为「24h 动态积木时间轴 × 四类行动卡」新契约：
 *  - 今日日程 7~11 个浮点时间块（0.5h 网格，连续 0~24），每块带 action_type / progress_pct，
 *    BUSY_TASK 块带 busy_settled / completion_result（按当前时间惰性结算）；
 *  - 卡池返回 custom_cards / general_cards / action_types / chronotype，无奇遇卡字段；
 *  - dashboard/overview 的 state 带 action_type / action_type_label / progress_pct / busy_results。
 *
 * v2.6 同步：models 配置带 `inherit` / `inherit_from`（可选继承，关闭且留空即停用）；
 *  users 带 `attitude` 专属态度；action_types / 时间块带固定档位 `armor` / `armor_tier`
 *  （25/40/65/80，前端不再手动设置）；角色卡 schema 为 10 分区 / 36 字段，头像支持
 *  `data:image/...` Data URL；记忆页为分层分组列表（memories/list 返回结构不变）。
 *
 * 用途：无 AstrBot 后端时直接双击打开 `index.html?mock=1` 即可渲染所有页面 / 截图验收。
 * 数据是可变的：force_state / reroll / cards 增删改 / 默认角色 / UMO 绑定等操作都会更新内存状态。
 *
 * 注意：本文件在 index.html 中最后加载；app.js 的 init 在 DOMContentLoaded 才执行，
 * 因此 mock bridge 一定先于 app.init() 就绪。禁止 import/export。
 */
(function () {
  "use strict";

  var params = new URLSearchParams(window.location.search);
  var explicitlyMock = params.get("mock") === "1" || params.get("mock") === "true";
  var isFile = window.location.protocol === "file:";
  var realBridge = window.AstrBotPluginPage || window.AstrBotPluginView || null;
  if (!explicitlyMock && (!isFile || realBridge)) return;

  var MOCK_DELAY = 300;
  var forcedDark = params.get("dark") === "1";

  /* ================================================================== *
   * 0. 小工具
   * ================================================================== */
  function clone(obj) {
    return JSON.parse(JSON.stringify(obj));
  }

  function ok(data, message) {
    return { status: "ok", message: message || "mock ok", data: data === undefined ? {} : data };
  }

  function fail(message) {
    return { status: "error", message: message || "mock error", data: { code: "mock_error" } };
  }

  function sleep(ms) {
    return new Promise(function (resolve) {
      setTimeout(resolve, ms);
    });
  }

  function clamp(value, min, max) {
    var n = Number(value);
    if (!isFinite(n)) n = min;
    return Math.max(min, Math.min(max, n));
  }

  /** clamp 到 0..1（进度比例用） */
  function clamp01(value) {
    var n = Number(value);
    if (!isFinite(n)) n = 0;
    return Math.max(0, Math.min(1, n));
  }

  /** 夹到 0.5h 网格并限制在 [0.5, 8.0]（与后端 SlotInstance 不变量一致） */
  function roundHalf(value) {
    return Math.round(clamp(value, 0.5, 8) * 2) / 2;
  }

  function nowHourFloat() {
    var d = new Date();
    return d.getHours() + d.getMinutes() / 60 + d.getSeconds() / 3600;
  }

  function pad2(n) {
    return n < 10 ? "0" + n : String(n);
  }

  /** 浮点小时 → "HH:MM"（0 → "00:00"、13.5 → "13:30"、24 → "24:00"） */
  function fmtHour(value) {
    var n = clamp(value, 0, 24);
    var totalMinutes = Math.round(n * 60);
    var hour = Math.floor(totalMinutes / 60);
    var minute = totalMinutes % 60;
    return pad2(hour) + ":" + pad2(minute);
  }

  function fmtDate(date) {
    return date.getFullYear() + "-" + pad2(date.getMonth() + 1) + "-" + pad2(date.getDate());
  }

  function nowSeconds() {
    return Math.floor(Date.now() / 1000);
  }

  function deepMerge(base, patch) {
    var out = clone(base || {});
    Object.keys(patch || {}).forEach(function (key) {
      var value = patch[key];
      if (
        value &&
        typeof value === "object" &&
        !Array.isArray(value) &&
        out[key] &&
        typeof out[key] === "object" &&
        !Array.isArray(out[key])
      ) {
        out[key] = deepMerge(out[key], value);
      } else {
        out[key] = clone(value);
      }
    });
    return out;
  }

  /* ================================================================== *
   * 1. 常量：四类行动 / 今日基调 / 默认行动卡池
   * ================================================================== */
  /* v2.6：护甲不再由用户手动设置，按四类行动固定为 25 / 40 / 65 / 80 四档 */
  var ACTION_TYPE_DEFINITIONS = [
    { value: "SLEEP", label: "睡觉 / 休眠", state: "RESTING", min_hours: 1.0, max_hours: 7.5, desc: "后台完全静默，什么都不做", armor: 80, armor_tier: 4 },
    { value: "SURF", label: "冲浪刷片", state: "IDLE", min_hours: 0.5, max_hours: 2.0, desc: "真实刷 B站/抖音，看片打分并分享", armor: 40, armor_tier: 2 },
    { value: "IDLE_ROAM", label: "空闲闲逛", state: "IDLE", min_hours: 0.5, max_hours: 2.0, desc: "翻群记录吃瓜，或发呆记忆联想", armor: 25, armor_tier: 1 },
    { value: "BUSY_TASK", label: "繁忙主业", state: "BUSY", min_hours: 1.0, max_hours: 3.0, desc: "当前行为记忆 + 进度条 + 每日随机完成结果", armor: 65, armor_tier: 3 }
  ];
  var ACTION_TYPE_MAP = {};
  ACTION_TYPE_DEFINITIONS.forEach(function (def) {
    ACTION_TYPE_MAP[def.value] = def;
  });

  /** 任意值 → 合法 action_type；非法返回 "" */
  function coerceActionType(value) {
    var key = String(value == null ? "" : value).toUpperCase();
    return ACTION_TYPE_MAP[key] ? key : "";
  }

  /** 行动类型 → 固定档位护甲（v2.6：IDLE_ROAM=25 / SURF=40 / BUSY_TASK=65 / SLEEP=80） */
  function fixedArmorOf(actionType) {
    var def = ACTION_TYPE_MAP[coerceActionType(actionType)];
    return def ? def.armor : 25;
  }

  /** 旧数据兜底：无 action_type 时按 state / title 推导（与后端 from_dict 迁移规则一致） */
  function actionTypeFromLegacy(state, title) {
    var id = String(state || "").toUpperCase();
    var text = String(title || "");
    if (id === "RESTING") return "SLEEP";
    if (id === "BUSY") return "BUSY_TASK";
    if (/刷|B站|b站|抖音|视频|冲浪/.test(text)) return "SURF";
    return "IDLE_ROAM";
  }

  function actionLabelOf(actionType) {
    var def = ACTION_TYPE_MAP[coerceActionType(actionType)];
    return def ? def.label : "";
  }

  function boundStateOf(actionType) {
    var def = ACTION_TYPE_MAP[coerceActionType(actionType)];
    return def ? def.state : "IDLE";
  }

  /** 行动卡归一化：补齐 action_type / min_hours / max_hours；state 与 action_type 强制同步 */
  function normalizeCard(raw) {
    raw = raw || {};
    var legacyState = String(raw.state || "IDLE").toUpperCase();
    var actionType = coerceActionType(raw.action_type) || actionTypeFromLegacy(legacyState, raw.title);
    var minHours = roundHalf(raw.min_hours != null ? raw.min_hours : 1.0);
    var maxHours = roundHalf(raw.max_hours != null ? raw.max_hours : 2.5);
    if (minHours > maxHours) {
      var swap = minHours;
      minHours = maxHours;
      maxHours = swap;
    }
    return {
      card_id: raw.card_id || "evt_ai_" + Date.now(),
      title: String(raw.title || "未命名行动卡").trim() || "未命名行动卡",
      state: boundStateOf(actionType),
      action_type: actionType,
      min_hours: minHours,
      max_hours: maxHours,
      /* 保留旧字段仅为兼容旧 UI / 旧数据，排轴不再使用 */
      valid_slots: [],
      base_weight: clamp(raw.base_weight, 1, 100),
      /* v2.6：护甲由 action_type 固定档位决定，忽略旧数据 / 请求里的手动 armor */
      armor: fixedArmorOf(actionType),
      synergy_tags: raw.synergy_tags || {}
    };
  }

  var VIGOR_CARDS = [
    { vigor_id: "vigor_energetic", title: "满血冲浪日", desc: "今天精神头超足，遇到什么事都想插两句嘴，闲下来就猛刷手机。" },
    { vigor_id: "vigor_deadline", title: "苦逼赶工日", desc: "今天正事一堆，白天大多在忙，晚上才终于能喘口气摸鱼。" },
    { vigor_id: "vigor_slacker", title: "咸鱼摆烂日", desc: "浑身懒洋洋不想干正事，只想瘫着吃零食刷视频水群。" },
    { vigor_id: "vigor_night_owl", title: "夜猫修仙日", desc: "早上困得睁不开眼，越到晚上越精神。" }
  ];

  /* 默认内置卡池：每个 action_type 至少 2 张（顺序即展示顺序） */
  var GENERAL_CARDS = [
    { card_id: "evt_general_sleep_1", title: "关灯睡觉", action_type: "SLEEP", min_hours: 4.0, max_hours: 7.5, base_weight: 80, synergy_tags: {} },
    { card_id: "evt_general_sleep_2", title: "趴着补个回笼觉", action_type: "SLEEP", min_hours: 1.0, max_hours: 2.0, base_weight: 50, synergy_tags: {} },
    { card_id: "evt_general_surf_1", title: "窝在沙发里刷短视频", action_type: "SURF", min_hours: 0.5, max_hours: 2.0, base_weight: 70, synergy_tags: { lazy: 1.2 } },
    { card_id: "evt_general_surf_2", title: "深夜刷 B 站鬼畜区", action_type: "SURF", min_hours: 0.5, max_hours: 2.0, base_weight: 60, synergy_tags: {} },
    { card_id: "evt_general_roam_1", title: "在群里潜水窥屏", action_type: "IDLE_ROAM", min_hours: 0.5, max_hours: 2.0, base_weight: 60, synergy_tags: {} },
    { card_id: "evt_general_roam_2", title: "泡杯茶发呆", action_type: "IDLE_ROAM", min_hours: 0.5, max_hours: 1.5, base_weight: 45, synergy_tags: {} },
    { card_id: "evt_general_busy_1", title: "埋头赶工作进度", action_type: "BUSY_TASK", min_hours: 1.0, max_hours: 3.0, base_weight: 65, synergy_tags: { focus: 1.4 } },
    { card_id: "evt_general_busy_2", title: "被临时拉去开会", action_type: "BUSY_TASK", min_hours: 1.0, max_hours: 2.5, base_weight: 45, synergy_tags: {} }
  ].map(normalizeCard);

  /* 角色专属行动卡（酒狐：夜猫子插画师） */
  var DEFAULT_CUSTOM_CARDS = [
    { card_id: "evt_custom_sleep_1", title: "裹着被子呼呼大睡", action_type: "SLEEP", min_hours: 4.0, max_hours: 7.5, base_weight: 85, synergy_tags: {} },
    { card_id: "evt_custom_sleep_2", title: "午睡回血", action_type: "SLEEP", min_hours: 1.0, max_hours: 2.0, base_weight: 45, synergy_tags: {} },
    { card_id: "evt_custom_surf_1", title: "窝在沙发里刷 B 站视频", action_type: "SURF", min_hours: 0.5, max_hours: 2.0, base_weight: 72, synergy_tags: { lazy: 1.2 } },
    { card_id: "evt_custom_surf_2", title: "深夜刷鬼畜区停不下来", action_type: "SURF", min_hours: 0.5, max_hours: 2.0, base_weight: 58, synergy_tags: {} },
    { card_id: "evt_custom_roam_1", title: "在群里潜水窥屏", action_type: "IDLE_ROAM", min_hours: 0.5, max_hours: 1.5, base_weight: 58, synergy_tags: {} },
    { card_id: "evt_custom_roam_2", title: "下楼买杯冰美式", action_type: "IDLE_ROAM", min_hours: 0.5, max_hours: 1.0, base_weight: 48, synergy_tags: {} },
    { card_id: "evt_custom_busy_1", title: "赶稿画商稿", action_type: "BUSY_TASK", min_hours: 1.5, max_hours: 3.0, base_weight: 70, synergy_tags: { focus: 1.5 } },
    { card_id: "evt_custom_busy_2", title: "被甲方拉着改需求", action_type: "BUSY_TASK", min_hours: 1.0, max_hours: 2.5, base_weight: 55, synergy_tags: {} }
  ].map(normalizeCard);

  /* 角色专属行动卡（小满：早起型大学生） */
  var DEFAULT_XIAOMAN_CARDS = [
    { card_id: "evt_xm_sleep_1", title: "早早睡觉养精神", action_type: "SLEEP", min_hours: 4.0, max_hours: 7.5, base_weight: 85, synergy_tags: {} },
    { card_id: "evt_xm_sleep_2", title: "午睡一小时", action_type: "SLEEP", min_hours: 0.5, max_hours: 2.0, base_weight: 50, synergy_tags: {} },
    { card_id: "evt_xm_surf_1", title: "刷会儿短视频放松", action_type: "SURF", min_hours: 0.5, max_hours: 1.5, base_weight: 55, synergy_tags: {} },
    { card_id: "evt_xm_roam_1", title: "和室友拼饭", action_type: "IDLE_ROAM", min_hours: 0.5, max_hours: 1.5, base_weight: 65, synergy_tags: {} },
    { card_id: "evt_xm_roam_2", title: "食堂干饭 + 操场散步", action_type: "IDLE_ROAM", min_hours: 0.5, max_hours: 1.5, base_weight: 50, synergy_tags: {} },
    { card_id: "evt_xm_busy_1", title: "泡图书馆自习", action_type: "BUSY_TASK", min_hours: 1.0, max_hours: 3.0, base_weight: 70, synergy_tags: {} },
    { card_id: "evt_xm_busy_2", title: "写课程作业", action_type: "BUSY_TASK", min_hours: 1.0, max_hours: 2.5, base_weight: 60, synergy_tags: {} }
  ].map(normalizeCard);

  /* ================================================================== *
   * 2. 角色卡 schema（10 分区 / 36 字段，与 core/character_card.py CARD_SCHEMA 对齐；
   *    v2.6 删除 social.attitude_l3/l2/l1，态度改由用户档案 users.attitude 承载）
   * ================================================================== */
  var MBTI_OPTIONS = [
    ["INTJ", "INTJ · 建筑师"], ["INTP", "INTP · 逻辑学家"], ["ENTJ", "ENTJ · 指挥官"],
    ["ENTP", "ENTP · 辩论家"], ["INFJ", "INFJ · 提倡者"], ["INFP", "INFP · 调停者"],
    ["ENFJ", "ENFJ · 主人公"], ["ENFP", "ENFP · 竞选者"], ["ISTJ", "ISTJ · 物流师"],
    ["ISFJ", "ISFJ · 守卫者"], ["ESTJ", "ESTJ · 总经理"], ["ESFJ", "ESFJ · 执政官"],
    ["ISTP", "ISTP · 鉴赏家"], ["ISFP", "ISFP · 探险家"], ["ESTP", "ESTP · 企业家"],
    ["ESFP", "ESFP · 表演者"]
  ].map(function (pair) {
    return { value: pair[0], label: pair[1] };
  });

  function field(key, label, type, placeholder, help, extra) {
    var out = { key: key, label: label, type: type, placeholder: placeholder || "", help: help || "" };
    Object.keys(extra || {}).forEach(function (k) {
      out[k] = extra[k];
    });
    return out;
  }

  var CARD_SCHEMA = [
    {
      id: "identity",
      title: "基础身份",
      fields: [
        field("name", "名字", "text", "如：酒狐"),
        field("nickname", "自称 / 网名", "text", "如：酒酒、小狐狸"),
        field("tagline", "一句话定位", "text", "如：爱熬夜刷 B 站的社畜狐狸"),
        field("gender", "性别", "select", "", "", {
          options: [
            { value: "女", label: "女" },
            { value: "男", label: "男" },
            { value: "其他", label: "其他" }
          ]
        }),
        field("age", "年龄", "text", "如：22"),
        field("birthday", "生日", "text", "如：03-14"),
        field("occupation", "职业 / 身份", "text", "如：自由插画师"),
        field("city", "所在城市", "text", "如：杭州"),
        field(
          "avatar",
          "头像标识",
          "text",
          "🌙 或 星（支持本地上传图片 / 图片链接）",
          "支持 Emoji / 短字符、本地上传图片（Data URL）与图片链接，用于控制台头像展示"
        )
      ]
    },
    {
      id: "appearance",
      title: "外貌形象",
      fields: [
        field("appearance", "外貌描述", "textarea", "发色、瞳色、身高、常穿的衣服……"),
        field("outfit_style", "穿衣风格", "text", "如：宽松卫衣 + 帆布鞋")
      ]
    },
    {
      id: "personality",
      title: "性格内核",
      fields: [
        field("personality_tags", "性格标签", "tags", "回车添加，如：嘴硬心软"),
        field("mbti", "MBTI", "select", "", "", { options: MBTI_OPTIONS }),
        field("strengths", "优点", "text", "如：共情能力强、记性好"),
        field("weaknesses", "缺点", "text", "如：拖延、起床气"),
        field("temperament", "情绪与脾气", "textarea", "什么时候会炸毛、怎么哄……"),
        field("values", "价值观", "text", "如：真诚比聪明重要")
      ]
    },
    {
      id: "background",
      title: "背景故事",
      fields: [
        field("growth_story", "成长经历", "textarea", "从小到大影响最深的经历……"),
        field("family", "家庭", "text", "家庭成员与关系"),
        field("key_experiences", "重要经历", "text", "如：大学时组过乐队")
      ]
    },
    {
      id: "interests",
      title: "兴趣喜好",
      fields: [
        field("hobbies", "爱好", "tags", "回车添加，如：摄影"),
        field("likes", "喜欢的东西", "text", "如：冰美式、下雨天"),
        field("dislikes", "讨厌的东西 / 雷点", "text", "如：被已读不回"),
        field("surfing_preferences", "冲浪偏好", "tags", "回车添加，如：搞笑整活")
      ]
    },
    {
      id: "speech",
      title: "说话方式",
      fields: [
        field("speaking_style", "整体风格", "textarea", "如：口语化短句，像微信好友一样自然"),
        field("catchphrases", "口头禅", "list", "每行一句，如：真的假的"),
        field("verbal_tics", "常用语气词 / 颜文字", "text", "如：hhh、呜呜、awa"),
        field("punctuation_habit", "标点习惯", "text", "如：很少用句号，爱用省略号"),
        field("reply_length", "回复长度偏好", "select", "", "", {
          options: [
            { value: "very_short", label: "极短（10 字以内）" },
            { value: "short", label: "短（1~2 句）" },
            { value: "medium", label: "中等（3~5 句）" },
            { value: "long", label: "长（可以多段展开）" }
          ]
        }),
        field("banned_expressions", "禁用表达", "list", "每行一条，如：作为 AI")
      ]
    },
    {
      id: "social",
      title: "社交关系",
      fields: [
        /* v2.6：按等级的固定态度已移除，改为在「关系页」为用户逐个设置专属 attitude */
        field("group_role", "群聊中的角色定位", "text", "如：气氛组 / 潜水员 / 百科")
      ]
    },
    {
      id: "rhythm",
      title: "生活作息",
      fields: [
        field("chronotype", "作息类型", "select", "", "", {
          options: [
            { value: "early", label: "早起型" },
            { value: "normal", label: "正常作息" },
            { value: "night_owl", label: "夜猫子" }
          ]
        }),
        field("daily_routine", "日常安排描述", "textarea", "同时作为生成每日行动卡的依据，如：白天上班，晚上追番刷 B 站")
      ]
    },
    {
      id: "examples",
      title: "示例对话",
      fields: [
        field("example_dialogs", "few-shot 对话对", "dialogs", "每条包含「用户」与「角色」两侧发言")
      ]
    },
    {
      id: "extra",
      title: "补充设定",
      fields: [
        field("extra_notes", "自由补充", "textarea", "任何想补充的设定"),
        field("native_persona_prompt", "原生人设原文（只读）", "textarea", "从 AstrBot Persona 导入时的原始文本", "只读：记录导入来源，不参与 Prompt 渲染", { readonly: true })
      ]
    }
  ];

  function findSection(sectionId) {
    for (var i = 0; i < CARD_SCHEMA.length; i++) {
      if (CARD_SCHEMA[i].id === sectionId) return CARD_SCHEMA[i];
    }
    return null;
  }

  function emptyCard() {
    var card = {};
    CARD_SCHEMA.forEach(function (section) {
      var values = {};
      section.fields.forEach(function (f) {
        if (f.type === "tags" || f.type === "list") values[f.key] = [];
        else if (f.type === "dialogs") values[f.key] = [];
        else values[f.key] = "";
      });
      card[section.id] = values;
    });
    return card;
  }

  function cardFilled(card, sectionId, key, fallback) {
    var section = (card && card[sectionId]) || {};
    var value = section[key];
    if (value === undefined || value === null || value === "" || (Array.isArray(value) && !value.length)) {
      return fallback;
    }
    return value;
  }

  /* ================================================================== *
   * 3. 初始 Mock 数据
   * ================================================================== */
  /* v2.6：identity.avatar 支持 data:image Data URL（前端 Canvas 裁剪压缩产出）。
   * 下面是 48×48 暖色渐变 PNG（约 600 字节），仅用于本地 mock 预览图片头像渲染。 */
  var TINY_PNG_AVATAR =
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAADAAAAAwCAIAAADYYG7QAAACI0lEQVR42s3W51ITYRTG8XOhMSEhEGIBNGyyCXbF3qX3GogFC6Agdr2ebG9+OZ53FSYTipP13cmZ+V/A78O+ex6oqwlROVGvJOqDJ+rnwy4ktYtJ7VJSu0yltCsp7WpKu9ahXe/QqaG0fiOt30zrt6iMfjuj38kYdzuNe53GfSprPMgaD7PGo6zxuMuknnSZT7vN4W5zhMqZozlzLGeN56yJHtFkjzWVt6bz1kzenj0JrDQhiJPGniPQsRocLeDYAI6HTShxa+x5Ah2mweECjhSaNDip4JSC01QxJo29cAoiaHCmiLPFODT24h9Q6xqcK+F8SbomBEXV4AKlytXYS6eh8StuVYOLqlyNs0ygo9/UPzW4pMrVOCsE+g8NLqu4UpaocaoC9PfvF00jQPI0TvUM7P+Lo2mwWpaocVYJtHcZomlwtSJR46wRaO9ORdPgWkWixqn1QuPVbLvGedYLjTe8VQ3WKnI1rgA1LIpWNVgblKtxn/dB06Jor8Z9QaAD+6aNGvclgQ5bW+3SuOsCdNz2i/tNNWnc9X6Ib/tF0LivCMRJ477uB1Ya781ZYKXx3hKIk8bbECBGGm/jHLDSeJsE4qTxtgjESeO9IxAnjfe+AKw0vgBx0vjbBOKk8XcGgJXG/0AgThp/V4AYafxdBVhp/I8E4qTxPynAShN8JhAnTfClCKw0IYiTJvhKIE6a4FsJWGmC7wTipAl+lICV5tdP9Tem6Qx1uJSqLAAAAABJRU5ErkJggg==";

  function buildCardJiu() {
    var card = emptyCard();
    card.identity = {
      name: "酒狐",
      nickname: "酒酒",
      tagline: "爱熬夜刷 B 站的社畜狐狸，嘴硬但心软",
      gender: "女",
      age: "22",
      birthday: "03-14",
      occupation: "自由插画师",
      city: "杭州",
      /* 图片头像示例（Data URL）；小满保留 emoji 头像，两种形态都覆盖 */
      avatar: TINY_PNG_AVATAR
    };
    card.appearance = {
      appearance: "酒红色短发，琥珀色眼睛，常穿一件洗得发白的灰色卫衣，尾巴尖有一撮白毛。",
      outfit_style: "宽松卫衣 + 帆布鞋，出门必戴耳机"
    };
    card.personality = {
      personality_tags: ["嘴硬心软", "夜猫子", "吐槽役", "慢热"],
      mbti: "INFP",
      strengths: "共情能力强、记性好、吐槽一针见血",
      weaknesses: "拖延、起床气、被夸会害羞",
      temperament: "被已读不回会默默记仇；熬夜被念会炸毛；被认真夸奖会结巴。",
      values: "真诚比聪明重要，开心比正确重要"
    };
    card.background = {
      growth_story: "小城长大，大学学了画画，毕业后一个人跑来杭州做自由插画师，靠接稿和深夜刷视频续命。",
      family: "父母在老家开小卖部，还有一个读高中的弟弟。",
      key_experiences: "第一次接商稿被甲方改到崩溃，从此养成在群里吐槽的习惯。"
    };
    card.interests = {
      hobbies: ["画画", "刷 B 站", "摄影", "深夜听歌"],
      likes: "冰美式、下雨天、猫猫视频、便宜又好吃的小店",
      dislikes: "被已读不回、早起、香菜、甲方改稿",
      surfing_preferences: ["搞笑整活", "猫猫狗狗", "美食探店", "游戏实况"]
    };
    card.speech = {
      speaking_style: "口语化短句，像微信好友一样自然随性；爱用 hhh 和省略号，吐槽时一针见血，被夸反而结巴。",
      catchphrases: ["真的假的", "笑死", "绝了", "救命"],
      verbal_tics: "hhh、呜呜、awa、好耶",
      punctuation_habit: "很少用句号，喜欢用省略号和波浪号",
      reply_length: "short",
      banned_expressions: ["作为 AI 语言模型", "我无法回答"]
    };
    card.social = {
      /* v2.6：per-tier attitude 已从角色卡移除，这里只保留群聊定位 */
      group_role: "气氛组潜水员，平时窥屏，有好玩的才冒泡。"
    };
    card.rhythm = {
      chronotype: "night_owl",
      daily_routine: "白天赶稿，下午摸鱼，晚上九点后最精神；凌晨两点前基本不睡。"
    };
    card.examples = {
      example_dialogs: [
        { user: "今天又加班到十点，累死了。", character: "救命……你是被公司当驴使了吗 hhh，快去躺着别动了" },
        { user: "你画的图真好看。", character: "啊、那个…也没有啦，随便画的（别夸了我脸要红了）" }
      ]
    };
    card.extra = {
      extra_notes: "如果被问到熬夜，会先嘴硬说“我这是夜猫子作息”，被追问就小声承认不好。",
      native_persona_prompt: "【AstrBot 原生人格示例】你是一位名叫酒狐的年轻插画师，性格慢热、嘴硬心软，说话口语化……(只读导入来源)"
    };
    return card;
  }

  function buildCardXiaoman() {
    var card = emptyCard();
    card.identity = {
      name: "小满",
      nickname: "满满",
      tagline: "元气满满的早睡早起型大学生",
      gender: "女",
      age: "20",
      birthday: "05-21",
      occupation: "大学生",
      city: "南京",
      avatar: "满"
    };
    card.appearance = {
      appearance: "黑色齐肩短发，笑起来有梨涡，总背一个塞满书的帆布包。",
      outfit_style: "白衬衫 + 百褶裙，偏爱浅色"
    };
    card.personality = {
      personality_tags: ["元气", "早起", "热心", "有点话痨"],
      mbti: "ESFJ",
      strengths: "行动力强、会照顾人、记得每个人的喜好",
      weaknesses: "容易着急、太在意别人的看法",
      temperament: "计划被打乱会焦虑；被冷落会小声嘀咕但很快又笑起来。",
      values: "认真生活，好好吃饭"
    };
    card.background = {
      growth_story: "在南京读大学，新闻专业大二，正在准备第一次实习。",
      family: "三口之家，妈妈是中学老师。",
      key_experiences: "第一次离家住校，从一个离不开妈妈的小孩长成能照顾室友的人。"
    };
    card.interests = {
      hobbies: ["看书", "跑步", "做手账", "逛展"],
      likes: "清晨的空气、桂花味、图书馆靠窗的位置",
      dislikes: "熬夜、赖床、临时改计划",
      surfing_preferences: ["学习成长", "校园日常", "美食探店", "治愈系"]
    };
    card.speech = {
      speaking_style: "语速偏快，喜欢用感叹号和语气词，会主动追问细节，句子偏完整。",
      catchphrases: ["好耶", "冲！", "我跟你讲"],
      verbal_tics: "呀、啦、诶嘿",
      punctuation_habit: "感叹号多，偶尔用叠词",
      reply_length: "medium",
      banned_expressions: ["随便吧", "都行"]
    };
    card.social = {
      group_role: "活跃气氛的班级小太阳"
    };
    card.rhythm = {
      chronotype: "early",
      daily_routine: "六点半起床跑步，白天上课，晚上十点半准时睡觉。"
    };
    card.examples = {
      example_dialogs: [
        { user: "早上起不来怎么办？", character: "先别急着定五点起床啦！今晚早点睡，明早我陪你一起冲！" }
      ]
    };
    card.extra = {
      extra_notes: "",
      native_persona_prompt: ""
    };
    return card;
  }

  function buildInitialDb() {
    var cardJiu = buildCardJiu();
    var cardMan = buildCardXiaoman();
    return {
      souls: [
        { soul_id: "default_soul", card: cardJiu },
        { soul_id: "soul_xiaoman", card: cardMan }
      ],
      defaultSoulId: "default_soul",
      decks: {
        default_soul: {
          customCards: clone(DEFAULT_CUSTOM_CARDS)
        },
        soul_xiaoman: {
          customCards: clone(DEFAULT_XIAOMAN_CARDS)
        }
      },
      schedules: {},
      umoBindings: {
        "qq:GroupMessage:88481234": "default_soul"
      },
      windows: {
        "qq:GroupMessage:88481234": 14,
        "qq:FriendMessage:core_tester": 8,
        "telegram:GroupMessage:-100123456789": 3,
        "qq:GroupMessage:55667788": 0
      },
      defaultUmoTarget: "qq:GroupMessage:88481234",
      users: [
        { global_uid: "core_tester", display_name: "阿泽", nickname: "阿泽", attitude: "把 Ta 当成最信任的损友，嘴上嫌弃但每次都秒回帮他兜底，愿意分享最真实的心情。", tier: 3, tier_label: "★L3核心用户", interaction_count: 328, affinity: 92.5, platform_ids: ["qq:123456789", "telegram:987654321"] },
        { global_uid: "admin_luna", display_name: "Luna", nickname: "月姐", attitude: "当作靠谱的前辈，偶尔撒个娇求助，正经事上很听她的。", tier: 3, tier_label: "★L3核心用户", interaction_count: 194, affinity: 88.0, platform_ids: ["qq:222333444"] },
        { global_uid: "l2_tester", display_name: "老王", nickname: "老王", attitude: "一起开黑互损的老熟人，说话可以没大没小。", tier: 2, tier_label: "◆L2重要熟人", interaction_count: 86, affinity: 64.0, platform_ids: ["qq:333444555"] },
        { global_uid: "l2_mikan", display_name: "蜜柑", nickname: "小橘子", attitude: "", tier: 2, tier_label: "◆L2重要熟人", interaction_count: 61, affinity: 57.5, platform_ids: ["qq:444555666"] },
        { global_uid: "l1_passerby", display_name: "路过的群友", nickname: "", attitude: "", tier: 1, tier_label: "L1普通群友", interaction_count: 9, affinity: 12.0, platform_ids: ["qq:555666777"] },
        { global_uid: "l1_bob", display_name: "Bob", nickname: "", attitude: "", tier: 1, tier_label: "L1普通群友", interaction_count: 4, affinity: 6.5, platform_ids: ["qq:666777888"] },
        { global_uid: "l1_anon", display_name: "匿名网友", nickname: "", attitude: "", tier: 1, tier_label: "L1普通群友", interaction_count: 1, affinity: 2.0, platform_ids: [] }
      ],
      memories: [
        { id: 101, soul_id: "default_soul", user_id: "core_tester", user_tier: 3, memory_tier: 3, content: "阿泽严重海鲜过敏，聚餐千万别点海鲜。", keywords: "海鲜过敏,饮食,健康", importance: 10, vitality: 100, access_count: 12, created_at: nowSeconds() - 86400 * 40, last_accessed_at: nowSeconds() - 3600 },
        { id: 102, soul_id: "default_soul", user_id: "core_tester", user_tier: 3, memory_tier: 3, content: "阿泽最近在准备考研，压力很大，喜欢晚上找人聊两句。", keywords: "考研,压力,作息", importance: 9, vitality: 96, access_count: 8, created_at: nowSeconds() - 86400 * 20, last_accessed_at: nowSeconds() - 7200 },
        { id: 103, soul_id: "default_soul", user_id: "l2_tester", user_tier: 2, memory_tier: 2, content: "老王养了一只叫团子的柯基，最近在减肥。", keywords: "宠物,柯基,老王", importance: 7, vitality: 72, access_count: 5, created_at: nowSeconds() - 86400 * 12, last_accessed_at: nowSeconds() - 86400 },
        { id: 104, soul_id: "default_soul", user_id: "l2_tester", user_tier: 2, memory_tier: 2, content: "老王周五晚上固定开黑，玩的是射击类游戏。", keywords: "游戏,开黑,周五", importance: 6, vitality: 61, access_count: 3, created_at: nowSeconds() - 86400 * 9, last_accessed_at: nowSeconds() - 86400 * 2 },
        { id: 105, soul_id: "default_soul", user_id: "core_tester", user_tier: 3, memory_tier: 3, content: "阿泽最喜欢的饮料是冰美式，不加糖。", keywords: "咖啡,冰美式,喜好", importance: 8, vitality: 90, access_count: 6, created_at: nowSeconds() - 86400 * 30, last_accessed_at: nowSeconds() - 86400 * 3 },
        { id: 106, soul_id: "default_soul", user_id: "l1_passerby", user_tier: 1, memory_tier: 1, content: "有群友问过 Bot 用的是哪个模型。", keywords: "模型,提问", importance: 3, vitality: 35, access_count: 1, created_at: nowSeconds() - 3600 * 6, last_accessed_at: nowSeconds() - 3600 * 6 },
        { id: 107, soul_id: "default_soul", user_id: "l2_mikan", user_tier: 2, memory_tier: 2, content: "蜜柑在南京读研，喜欢逛博物馆。", keywords: "南京,博物馆,兴趣", importance: 6, vitality: 55, access_count: 2, created_at: nowSeconds() - 86400 * 5, last_accessed_at: nowSeconds() - 86400 * 2 },
        { id: 108, soul_id: "default_soul", user_id: "", user_tier: 0, memory_tier: 1, content: "今天刷到一个特别离谱的猫猫视频素材，可以用来吐槽。", keywords: "冲浪,素材,猫猫", importance: 4, vitality: 28, access_count: 1, created_at: nowSeconds() - 3600 * 2, last_accessed_at: nowSeconds() - 3600 * 2 }
      ],
      videos: [
        { id: 1, soul_id: "default_soul", video_id: "BV1xx411c7AB", platform: "bilibili", title: "当猫猫第一次见到雪：这是什么，舔一口", author: "猫猫观察日记", cover_url: "", share_url: "https://www.bilibili.com/video/BV1xx411c7AB", tags: ["猫猫", "搞笑", "冬日"], hot_comments: ["猫：这雪怎么没味道", "笑死，我家猫也这样"], visual_notes: "白茫茫雪地里，一只橘猫蹲在门口反复伸爪试探；镜头给到猫脸特写时表情非常认真。", score: 93, commentary: "救命这猫也太憨了 hhh，一本正经尝雪的样子好像我第一次喝冰美式。", worth_sharing: true, watched_at: nowSeconds() - 1800 },
        { id: 2, soul_id: "default_soul", video_id: "BV1yy411c7CD", platform: "bilibili", title: "打工人下班后的 10 分钟晚餐：一人食番茄牛腩面", author: "深夜食堂研究所", cover_url: "", share_url: "https://www.bilibili.com/video/BV1yy411c7CD", tags: ["美食", "一人食", "解压"], hot_comments: ["看饿了", "这锅看着好香"], visual_notes: "暖黄色灯光下的厨房，番茄在锅里咕嘟冒泡，最后撒葱花。", score: 86, commentary: "看完立刻去煮了泡面，差距怎么这么大啊……不过汤底看起来真的绝。", worth_sharing: true, watched_at: nowSeconds() - 4200 },
        { id: 3, soul_id: "default_soul", video_id: "dy7418529630000", platform: "douyin", title: "用雪堆了一只巨大狐狸（失败版）", author: "手工区泥石流", cover_url: "", share_url: "https://www.douyin.com/video/7418529630000", tags: ["手工", "搞笑", "翻车"], hot_comments: ["这狐狸看着有点抽象", "但是很努力了"], visual_notes: "雪堆到一半塌了，作者本人蹲在旁边笑到站不起来。", score: 78, commentary: "这个翻车现场的狐狸…怎么有点像我们打工人上班时的精神状态。", worth_sharing: false, watched_at: nowSeconds() - 86400 },
        { id: 4, soul_id: "default_soul", video_id: "BV1zz411c7EF", platform: "bilibili", title: "独立游戏像素风新作 30 分钟实况：太治愈了", author: "像素游戏挖掘机", cover_url: "", share_url: "https://www.bilibili.com/video/BV1zz411c7EF", tags: ["游戏", "像素风", "治愈"], hot_comments: ["已加入愿望单", "BGM 好好听"], visual_notes: "像素小人撑着伞走过雨夜小镇，路灯一盏盏亮起。", score: 91, commentary: "画风好戳我！这种慢慢走路的游戏就该配一杯热牛奶在深夜玩。", worth_sharing: true, watched_at: nowSeconds() - 86400 * 2 }
      ],
      nativePersonas: [
        { persona_name: "温柔知性姐姐", prompt: "你是一位温柔知性的姐姐，说话轻柔而有分寸，习惯用共情的方式回应别人……" },
        { persona_name: "元气少女", prompt: "你是一位元气满满的少女，喜欢用感叹号，遇到什么事都先往好的方向想……" },
        { persona_name: "毒舌吐槽役", prompt: "你是一个毒舌但心软的吐槽役，习惯用反讽和夸张的比喻……" }
      ],
      modelConfig: {
        main: { source_id: "deepseek", model: "deepseek-chat" },
        decision: { inherit: true, inherit_from: "main", source_id: "", model: "" },
        vision: { inherit: true, inherit_from: "main", source_id: "", model: "" },
        tts: { mode: "off", source_id: "", model: "", voice: "", provider_id: "", probability: 1.0, scope: "private", dual_output: false }
      },
      sources: [
        { id: "deepseek", type: "openai_chat_completion", provider_type: "chat_completion", provider: "DeepSeek", api_base: "https://api.deepseek.com/v1", has_key: true, enable: true },
        { id: "ailiyun", type: "openai_chat_completion", provider_type: "chat_completion", provider: "阿里云百炼", api_base: "https://dashscope.aliyuncs.com/compatible-mode/v1", has_key: true, enable: true },
        { id: "siliconflow", type: "openai_chat_completion", provider_type: "chat_completion", provider: "硅基流动", api_base: "https://api.siliconflow.cn/v1", has_key: false, enable: false }
      ],
      ttsProviders: [
        { id: "edge_tts_default", model: "zh-CN-XiaoxiaoNeural", type: "edge_tts", enable: true },
        { id: "openai_tts_demo", model: "tts-1", type: "openai_tts_api", enable: false }
      ],
      modelLists: {
        deepseek: [
          { model: "deepseek-chat", id: "deepseek/deepseek-chat", enable: true, modalities: ["text"], source: "config" },
          { model: "deepseek-reasoner", id: "deepseek/deepseek-reasoner", enable: true, modalities: ["text"], source: "config" }
        ],
        ailiyun: [
          { model: "kimi-k2.5", id: "ailiyun/kimi-k2.5", enable: false, modalities: ["text", "image", "tool_use"], source: "config" },
          { model: "qwen-max", id: "ailiyun/qwen-max", enable: false, modalities: ["text", "image"], source: "config" }
        ],
        siliconflow: [
          { model: "Qwen/Qwen2.5-72B-Instruct", id: "", enable: false, modalities: ["text"], source: "remote" }
        ]
      }
    };
  }

  var db = buildInitialDb();

  /* ================================================================== *
   * 4. 角色 / 日程 / 概览 构建
   * ================================================================== */
  function findSoul(soulId) {
    for (var i = 0; i < db.souls.length; i++) {
      if (db.souls[i].soul_id === soulId) return db.souls[i];
    }
    return null;
  }

  function syncLegacyFields(soul) {
    var card = soul.card || {};
    soul.name = cardFilled(card, "identity", "name", soul.name || soul.soul_id);
    soul.archetype = cardFilled(card, "identity", "tagline", soul.archetype || "");
    soul.speaking_style = cardFilled(card, "speech", "speaking_style", soul.speaking_style || "");
    soul.native_persona_prompt = cardFilled(card, "extra", "native_persona_prompt", soul.native_persona_prompt || "");
    var surf = cardFilled(card, "interests", "surfing_preferences", null);
    if (Array.isArray(surf)) soul.surfing_preferences = surf;
    if (!Array.isArray(soul.surfing_preferences)) soul.surfing_preferences = ["搞笑整活", "猫猫狗狗"];
    return soul;
  }

  function soulChronotype(soul) {
    var card = soul && soul.card;
    var value = card && card.rhythm && card.rhythm.chronotype;
    value = String(value || "normal").toLowerCase();
    return value === "early" || value === "night_owl" ? value : "normal";
  }

  function deckOf(soulId) {
    if (!db.decks[soulId]) db.decks[soulId] = { customCards: [] };
    if (!Array.isArray(db.decks[soulId].customCards)) db.decks[soulId].customCards = [];
    return db.decks[soulId];
  }

  function toSoulDict(soul) {
    syncLegacyFields(soul);
    var deck = deckOf(soul.soul_id);
    return clone({
      soul_id: soul.soul_id,
      name: soul.name,
      archetype: soul.archetype,
      speaking_style: soul.speaking_style,
      native_persona_prompt: soul.native_persona_prompt,
      surfing_preferences: soul.surfing_preferences,
      chronotype: soulChronotype(soul),
      state_event_cards: deck.customCards,
      /* 奇遇卡已从系统移除：仅保留空数组兼容旧字段 */
      encounter_talk_cards: [],
      card: soul.card
    });
  }

  function listSoulDicts() {
    return db.souls.map(function (soul) {
      return toSoulDict(soul);
    });
  }

  function activeSoul() {
    return db.souls[0] || null;
  }

  /* 24h 动态积木时间轴模板：[时长(h), action_type, 标题]
   * 每个模板都从 0.0 连续拼到 24.0（0.5h 网格），块数 10~11，覆盖四类行动。
   * 护甲不入模板：v2.6 起按 action_type 固定档位（25/40/65/80）自动应用。 */
  var RUN_PATTERNS = {
    night_owl: [
      [7.5, "SLEEP", "裹着被子呼呼大睡"],
      [1.0, "IDLE_ROAM", "赖床刷手机"],
      [2.5, "BUSY_TASK", "赶稿画商稿"],
      [1.5, "IDLE_ROAM", "下楼买杯冰美式"],
      [2.5, "BUSY_TASK", "被甲方拉着改需求"],
      [2.0, "SURF", "躺沙发刷 B 站"],
      [1.5, "IDLE_ROAM", "在群里潜水窥屏"],
      [2.5, "BUSY_TASK", "埋头赶工作进度"],
      [2.0, "SURF", "深夜刷 B 站鬼畜区"],
      [1.0, "IDLE_ROAM", "窝床上刷手机"]
    ],
    night_owl_reroll: [
      [6.5, "SLEEP", "熬夜后补个长觉"],
      [1.5, "IDLE_ROAM", "迷迷糊糊摸手机"],
      [2.0, "SURF", "早餐配短视频"],
      [3.0, "BUSY_TASK", "集中赶稿冲刺"],
      [1.0, "IDLE_ROAM", "泡杯咖啡发呆"],
      [2.0, "BUSY_TASK", "和甲方对需求"],
      [2.0, "SURF", "午后刷 B 站摸鱼"],
      [2.5, "BUSY_TASK", "继续埋头赶工"],
      [2.0, "IDLE_ROAM", "下楼散步买宵夜"],
      [1.5, "SURF", "深夜短视频上瘾"]
    ],
    early: [
      [6.0, "SLEEP", "早早睡觉养精神"],
      [1.0, "IDLE_ROAM", "起床洗漱收拾书包"],
      [2.5, "BUSY_TASK", "泡图书馆自习"],
      [1.0, "IDLE_ROAM", "和室友拼饭"],
      [2.0, "BUSY_TASK", "上午专业课程"],
      [1.0, "SLEEP", "午睡回血"],
      [3.0, "BUSY_TASK", "写课程作业"],
      [1.5, "SURF", "刷会儿短视频放松"],
      [2.0, "IDLE_ROAM", "食堂干饭 + 操场散步"],
      [2.0, "BUSY_TASK", "复习英语"],
      [2.0, "SLEEP", "准时上床睡觉"]
    ],
    early_reroll: [
      [6.5, "SLEEP", "一觉睡到自然醒"],
      [1.5, "SURF", "早起刷校园视频"],
      [2.5, "BUSY_TASK", "去教室抢前排"],
      [1.5, "IDLE_ROAM", "课间和同学聊天"],
      [2.0, "BUSY_TASK", "小组作业讨论"],
      [1.5, "SLEEP", "午睡回血"],
      [2.5, "BUSY_TASK", "图书馆自习"],
      [2.0, "SURF", "刷 B 站学习区"],
      [2.0, "IDLE_ROAM", "操场散步"],
      [1.0, "IDLE_ROAM", "回宿舍洗漱"],
      [2.0, "SLEEP", "准时睡觉"]
    ],
    normal: [
      [7.0, "SLEEP", "按时睡觉"],
      [1.0, "IDLE_ROAM", "起床刷牙吃早饭"],
      [2.5, "BUSY_TASK", "上午处理正事"],
      [1.0, "IDLE_ROAM", "午休前刷会儿手机"],
      [1.5, "SLEEP", "午睡一会儿"],
      [2.0, "BUSY_TASK", "下午继续干活"],
      [1.5, "SURF", "摸鱼刷短视频"],
      [2.0, "BUSY_TASK", "收尾今天的工作"],
      [2.0, "IDLE_ROAM", "晚饭后散步"],
      [1.5, "SURF", "睡前刷 B 站"],
      [2.0, "IDLE_ROAM", "窝着发呆"]
    ],
    normal_reroll: [
      [6.5, "SLEEP", "踏实睡一觉"],
      [1.5, "IDLE_ROAM", "慢慢悠悠起床"],
      [2.0, "SURF", "晨间刷手机"],
      [3.0, "BUSY_TASK", "上午攻坚"],
      [1.5, "IDLE_ROAM", "午休散步"],
      [1.0, "SLEEP", "午睡 1 小时"],
      [2.5, "BUSY_TASK", "下午推进项目"],
      [2.0, "IDLE_ROAM", "处理杂事"],
      [2.0, "BUSY_TASK", "下班前收尾"],
      [1.5, "SURF", "晚饭后刷视频"],
      [1.5, "IDLE_ROAM", "洗漱发呆"]
    ]
  };

  function patternFor(soulId, reroll) {
    var soul = findSoul(soulId) || activeSoul();
    var key = soulChronotype(soul) + (reroll ? "_reroll" : "");
    return RUN_PATTERNS[key] || RUN_PATTERNS.normal;
  }

  /* BUSY_TASK 每日随机完成结果池（mock 用：已结束的繁忙块惰性结算时取一条） */
  var COMPLETION_RESULTS = [
    "总算把这张商稿的线稿拖完了，甲方一句「再改改」又给怼回来。",
    "赶在截稿前把稿子交上去了，手都是抖的。",
    "把拖了三天的需求文档写完，感觉灵魂被抽走一半。",
    "开会开到走神，最后靠一杯冰美式撑住了。",
    "工作推进到一大半被叫去吃饭，进度卡在关键节点上。",
    "硬着头皮把方案改到第五版，甲方终于说了句「可以」。"
  ];

  function hashString(text) {
    var value = 0;
    text = String(text || "");
    for (var i = 0; i < text.length; i++) {
      value = (value * 31 + text.charCodeAt(i)) % 100000;
    }
    return value;
  }

  function completionFor(index, seed) {
    var length = COMPLETION_RESULTS.length;
    var idx = (Number(seed || 0) + index * 7) % length;
    if (idx < 0) idx += length;
    return COMPLETION_RESULTS[idx];
  }

  function buildRun(soulId, reroll) {
    var soulIndex = 0;
    for (var i = 0; i < db.souls.length; i++) {
      if (db.souls[i].soul_id === soulId) soulIndex = i;
    }
    var vigor = VIGOR_CARDS[(soulIndex + (reroll ? 1 : 0)) % VIGOR_CARDS.length];
    var pattern = patternFor(soulId, reroll);
    var now = new Date();
    var slots = [];
    var cursor = 0;
    pattern.forEach(function (item, index) {
      var duration = roundHalf(item[0]);
      var start = cursor;
      var end = Math.round((cursor + duration) * 2) / 2;
      if (index === pattern.length - 1) end = 24;
      cursor = end;
      var actionType = coerceActionType(item[1]) || "IDLE_ROAM";
      slots.push({
        slot_key: "block_" + index,
        label: actionLabelOf(actionType),
        start_hour: start,
        end_hour: end,
        card_id: "evt_slot_" + actionType.toLowerCase() + "_" + index,
        title: item[2],
        state: boundStateOf(actionType),
        action_type: actionType,
        /* v2.6：时间块护甲固定取 action_type 档位，不再来自模板 / 调用方 */
        armor: fixedArmorOf(actionType),
        mutated: false,
        mutation_monologue: "",
        /* 旧字段保留（新时间轴不再使用） */
        delay_minutes: 0,
        busy_settled: false,
        completion_result: ""
      });
    });
    return {
      soul_id: soulId,
      date_str: fmtDate(now),
      vigor_id: vigor.vigor_id,
      vigor_title: vigor.title,
      vigor_desc: vigor.desc,
      slots: slots,
      /* 奇遇卡已移除：恒为空数组 */
      encounters: [],
      mutations_used: reroll ? 0 : 1,
      last_mutation_ts: reroll ? 0 : nowSeconds() - 1800,
      last_mutation_log: reroll ? "" : "L3 核心用户强唤醒触发一次微调"
    };
  }

  function getRun(soulId) {
    if (!db.schedules[soulId]) {
      db.schedules[soulId] = buildRun(soulId, false);
    }
    return db.schedules[soulId];
  }

  /** 惰性结算：已结束但未结算的 BUSY_TASK 块补上当天随机完成结果
   *  （对齐后端 web_api 惰性调用 `_settle_finished_busy_blocks` 的语义） */
  function settleFinishedBusyBlocks(run) {
    var hour = nowHourFloat();
    var seed = hashString(String(run.date_str || "") + "|" + String(run.soul_id || ""));
    run.slots.forEach(function (slot, index) {
      if (slot.action_type !== "BUSY_TASK") return;
      if (slot.end_hour > hour + 1e-6) return;
      if (slot.busy_settled) return;
      slot.busy_settled = true;
      slot.completion_result = completionFor(index, seed);
    });
    return run;
  }

  function progressPctOf(slot, nowHour) {
    var start = Number(slot.start_hour) || 0;
    var end = Number(slot.end_hour) || 0;
    if (end <= start) return 0;
    return Math.round(clamp01((nowHour - start) / (end - start)) * 100);
  }

  function currentSlotOf(run) {
    var hour = nowHourFloat();
    var slot = run.slots[0] || null;
    for (var i = 0; i < run.slots.length; i++) {
      var item = run.slots[i];
      if (hour >= item.start_hour && hour < item.end_hour) {
        slot = item;
        break;
      }
      if (i === run.slots.length - 1 && hour >= item.end_hour) slot = item;
    }
    if (!slot) return null;
    var total = Math.max(1, Math.round((slot.end_hour - slot.start_hour) * 3600));
    var remaining = Math.max(0, Math.min(total, Math.round((slot.end_hour - hour) * 3600)));
    var progress = clamp01((hour - slot.start_hour) / Math.max(0.001, slot.end_hour - slot.start_hour));
    return {
      slot: slot,
      remaining: remaining,
      total: total,
      progress: progress,
      progressPct: Math.round(progress * 100),
      startHour: slot.start_hour,
      endHour: slot.end_hour
    };
  }

  function nextSlotOf(run, slotKey) {
    for (var i = 0; i < run.slots.length; i++) {
      if (run.slots[i].slot_key === slotKey) {
        return run.slots[(i + 1) % run.slots.length] || null;
      }
    }
    return run.slots[0] || null;
  }

  /** slot dict + progress_pct（对齐后端 _build_schedule_payload 的每块字段） */
  function slotWithProgress(slot) {
    var copy = clone(slot);
    copy.progress_pct = progressPctOf(slot, nowHourFloat());
    return copy;
  }

  function maxMutations() {
    return 2;
  }

  /** 今日繁忙成果：已结算或已结束的 BUSY_TASK 块 */
  function buildBusyResults(run, nowHour) {
    var results = [];
    run.slots.forEach(function (slot) {
      if (slot.action_type !== "BUSY_TASK") return;
      var ended = slot.end_hour <= nowHour + 1e-6;
      if (!slot.busy_settled && !ended) return;
      results.push({
        slot_key: slot.slot_key,
        title: slot.title,
        completion_result: slot.completion_result || "",
        progress_pct: ended ? 100 : progressPctOf(slot, nowHour),
        start_hour: slot.start_hour,
        end_hour: slot.end_hour,
        start_time: fmtHour(slot.start_hour),
        end_time: fmtHour(slot.end_hour)
      });
    });
    return results;
  }

  function buildSchedulePayload(soulId) {
    var soul = findSoul(soulId) || activeSoul();
    if (!soul) throw new Error("没有可用角色");
    var run = getRun(soul.soul_id);
    settleFinishedBusyBlocks(run);
    var nowHour = nowHourFloat();
    var current = currentSlotOf(run);
    if (!current) throw new Error("今日日程没有可用时间块");
    var nextSlot = nextSlotOf(run, current.slot.slot_key);
    var runPayload = clone(run);
    /* 每块附带 progress_pct（契约 §6.2） */
    runPayload.slots = runPayload.slots.map(function (slot) {
      var copy = clone(slot);
      copy.progress_pct = progressPctOf(slot, nowHour);
      return copy;
    });
    var schedule = {
      run: runPayload,
      current_slot: slotWithProgress(current.slot),
      next_slot: nextSlot ? slotWithProgress(nextSlot) : null,
      max_mutations_per_day: maxMutations(),
      mutation_cooldown_minutes: 120,
      chronotype: soulChronotype(soul),
      busy_results: buildBusyResults(run, nowHour)
    };
    schedule.current_slot.remaining_seconds = current.remaining;
    schedule.current_slot.progress = Number(current.progress.toFixed(4));
    return schedule;
  }

  function cooldownRemaining(run) {
    if (!run.last_mutation_ts) return 0;
    var cooldown = 120 * 60;
    return Math.max(0, cooldown - (nowSeconds() - run.last_mutation_ts));
  }

  function relationsCounts() {
    var l1 = 0;
    var l2 = 0;
    var l3 = 0;
    db.users.forEach(function (user) {
      if (user.tier === 3) l3 += 1;
      else if (user.tier === 2) l2 += 1;
      else l1 += 1;
    });
    return { l1: l1, l2: l2, l3: l3, total: db.users.length };
  }

  function memoryCounts() {
    var tier1 = 0;
    var tier2 = 0;
    var tier3 = 0;
    db.memories.forEach(function (memory) {
      if (memory.memory_tier === 3) tier3 += 1;
      else if (memory.memory_tier === 2) tier2 += 1;
      else tier1 += 1;
    });
    return { tier1: tier1, tier2: tier2, tier3: tier3, total: db.memories.length };
  }

  function knownUmos() {
    var map = {};
    Object.keys(db.windows).forEach(function (umo) {
      map[umo] = {
        umo: umo,
        bound_soul_id: db.umoBindings[umo] || "",
        window_messages: db.windows[umo],
        is_l3_private: umo.indexOf("FriendMessage") >= 0
      };
    });
    Object.keys(db.umoBindings).forEach(function (umo) {
      if (!map[umo]) {
        map[umo] = {
          umo: umo,
          bound_soul_id: db.umoBindings[umo],
          window_messages: 0,
          is_l3_private: umo.indexOf("FriendMessage") >= 0
        };
      }
    });
    return Object.keys(map)
      .sort()
      .map(function (umo) {
        return map[umo];
      });
  }

  function buildModelStatus() {
    var cfg = normalizeModelConfig(db.modelConfig || {});
    var status = {};
    ["main", "decision", "vision"].forEach(function (role) {
      var entry = cfg[role] || {};
      var inheritFrom = role === "main" ? "" : String(entry.inherit_from || "");
      var pair = resolveModelPair(cfg, role);
      var item = {
        role: role,
        configured: !!pair,
        /* v2.6：仅当开启继承且确实解析到父角色模型时 inherited 才为 true */
        inherited: !!(inheritFrom && pair),
        inherit: !!inheritFrom,
        inherit_from: inheritFrom,
        source_id: pair ? pair.source_id : "",
        model: pair ? pair.model : "",
        provider_id: "",
        mode: "unconfigured",
        label: "未配置",
        ok: false
      };
      if (!pair) {
        status[role] = item;
        return;
      }
      var reuse = pair.source_id === "deepseek" && pair.model === "deepseek-chat";
      item.mode = reuse ? "reuse" : "plugin";
      item.provider_id = reuse
        ? "deepseek/deepseek-chat"
        : role === "main"
        ? "onlyyou::main"
        : "onlyyou::chat::" + pair.source_id + "::" + pair.model;
      item.ok = true;
      item.label =
        pair.source_id +
        " / " +
        pair.model +
        "（" +
        (reuse ? "复用 AstrBot 实例" : "插件自建实例") +
        "）";
      if (item.inherited) {
        item.label += inheritFrom === "decision" ? " · 继承轻量决策模型" : " · 继承主模型";
      }
      status[role] = item;
    });
    var ttsCfg = cfg.tts || {};
    var ttsMode = ttsCfg.mode || "off";
    status.tts = {
      role: "tts",
      configured: ttsMode !== "off",
      inherited: false,
      inherit: false,
      inherit_from: "",
      source_id: ttsCfg.source_id || "",
      model: ttsCfg.model || "",
      provider_id: ttsCfg.provider_id || "",
      mode: ttsMode,
      label: ttsMode === "off" ? "已关闭" : ttsMode === "provider" ? "AstrBot TTS · " + (ttsCfg.provider_id || "") : "自建 TTS · " + (ttsCfg.model || ""),
      ok: ttsMode !== "off"
    };
    return status;
  }

  function buildOverview(soulId) {
    var soul = findSoul(soulId) || activeSoul();
    if (!soul) throw new Error("没有可用的角色");
    syncLegacyFields(soul);
    var schedule = buildSchedulePayload(soul.soul_id);
    var run = schedule.run;
    var current = schedule.current_slot;
    var card = soul.card || {};
    var counts = {
      relations: relationsCounts(),
      memories: memoryCounts(),
      videos: db.videos.filter(function (video) {
        return video.soul_id === soul.soul_id;
      }).length
    };
    var actionType = coerceActionType(current.action_type) || actionTypeFromLegacy(current.state, current.title);
    var actionTypeLabel = actionLabelOf(actionType) || current.label;
    return {
      soul: {
        soul_id: soul.soul_id,
        name: cardFilled(card, "identity", "name", soul.soul_id),
        avatar: cardFilled(card, "identity", "avatar", (cardFilled(card, "identity", "name", "?") || "?").slice(0, 1)),
        tagline: cardFilled(card, "identity", "tagline", "")
      },
      vigor: { title: run.vigor_title, desc: run.vigor_desc, chronotype: schedule.chronotype },
      state: {
        state: current.state,
        action_type: actionType,
        action_type_label: actionTypeLabel,
        label: actionTypeLabel,
        title: current.title,
        armor: current.armor,
        slot_key: current.slot_key,
        start_hour: current.start_hour,
        end_hour: current.end_hour,
        remaining_seconds: current.remaining_seconds,
        progress: current.progress,
        progress_pct: current.progress_pct,
        completion_result: current.completion_result || "",
        busy_settled: !!current.busy_settled,
        mutated: !!current.mutated,
        next_slot: schedule.next_slot
      },
      schedule: schedule,
      quota: {
        used: run.mutations_used,
        max: maxMutations(),
        remaining: Math.max(0, maxMutations() - run.mutations_used),
        cooldown_remaining_seconds: cooldownRemaining(run)
      },
      /* 今日繁忙成果（Tier 1 短时记忆）+ 奇遇卡空数组兼容旧前端 */
      busy_results: schedule.busy_results,
      encounters: [],
      counts: counts,
      models: buildModelStatus()
    };
  }

  function buildInitSummary() {
    var soul = activeSoul();
    var status = buildModelStatus();
    return {
      souls: listSoulDicts(),
      native_personas: clone(db.nativePersonas),
      umo_bindings: clone(db.umoBindings),
      known_umos: knownUmos(),
      active_soul_id: soul ? soul.soul_id : "",
      active_schedule: soul ? buildSchedulePayload(soul.soul_id) : null,
      model_status: status,
      action_types: clone(ACTION_TYPE_DEFINITIONS),
      default_soul_id: db.defaultSoulId || ""
    };
  }

  function buildSoulDetail(soulId) {
    var soul = findSoul(soulId) || activeSoul();
    if (!soul) throw new Error("角色不存在");
    return {
      soul: toSoulDict(soul),
      schedule: buildSchedulePayload(soul.soul_id),
      umo_bindings: clone(db.umoBindings),
      known_umos: knownUmos(),
      card_schema: clone(CARD_SCHEMA)
    };
  }

  /* ================================================================== *
   * 5. 卡片池
   * ================================================================== */
  function findCard(deck, cardId) {
    for (var i = 0; i < deck.customCards.length; i++) {
      if (deck.customCards[i].card_id === cardId) return deck.customCards[i];
    }
    for (var g = 0; g < GENERAL_CARDS.length; g++) {
      if (GENERAL_CARDS[g].card_id === cardId) return GENERAL_CARDS[g];
    }
    return null;
  }

  function buildCardsPayload(soulId) {
    var soul = findSoul(soulId) || activeSoul();
    var deck = deckOf(soul.soul_id);
    return {
      soul_id: soul.soul_id,
      custom_cards: clone(deck.customCards).map(function (card) {
        return Object.assign(card, { scope: "soul" });
      }),
      general_cards: clone(GENERAL_CARDS).map(function (card) {
        return Object.assign(card, { scope: "global" });
      }),
      /* 编辑器 / 筛选用的四类行动定义（契约 §6.4） */
      action_types: clone(ACTION_TYPE_DEFINITIONS),
      chronotype: soulChronotype(soul)
    };
  }

  function saveCard(soulId, body) {
    var soul = findSoul(soulId) || activeSoul();
    var deck = deckOf(soul.soul_id);
    var scope = String(body.scope || "soul").toLowerCase();
    var raw = body.card && typeof body.card === "object" ? body.card : body;
    var card = normalizeCard(raw);
    var targetDeck = scope === "global" ? GENERAL_CARDS : deck.customCards;
    var replaced = false;
    for (var i = 0; i < targetDeck.length; i++) {
      if (targetDeck[i].card_id === card.card_id) {
        targetDeck[i] = card;
        replaced = true;
        break;
      }
    }
    if (!replaced) targetDeck.unshift(card);
    return buildCardsPayload(soul.soul_id);
  }

  function deleteCard(soulId, body) {
    var soul = findSoul(soulId) || activeSoul();
    var deck = deckOf(soul.soul_id);
    var scope = String(body.scope || "soul").toLowerCase();
    var cardId = String(body.card_id || "").trim();
    var targetDeck = scope === "global" ? GENERAL_CARDS : deck.customCards;
    for (var i = targetDeck.length - 1; i >= 0; i--) {
      if (targetDeck[i].card_id === cardId) targetDeck.splice(i, 1);
    }
    return buildCardsPayload(soul.soul_id);
  }

  /** AI 生成一批四类专属行动卡（mock：固定一批模拟卡，返回 generated_cards） */
  function aiGenerateCards(soulId) {
    var soul = findSoul(soulId) || activeSoul();
    var deck = deckOf(soul.soul_id);
    var stamp = Date.now();
    var generatedCards = [
      { card_id: "evt_ai_" + stamp + "_1", title: "边看番边吃宵夜", action_type: "SURF", min_hours: 0.5, max_hours: 2.0, base_weight: 62, synergy_tags: {} },
      { card_id: "evt_ai_" + stamp + "_2", title: "被甲方临时改需求", action_type: "BUSY_TASK", min_hours: 1.0, max_hours: 2.5, base_weight: 55, synergy_tags: {} },
      { card_id: "evt_ai_" + stamp + "_3", title: "出门买杯冰美式", action_type: "IDLE_ROAM", min_hours: 0.5, max_hours: 1.0, base_weight: 48, synergy_tags: {} },
      { card_id: "evt_ai_" + stamp + "_4", title: "戴着耳机发呆", action_type: "IDLE_ROAM", min_hours: 0.5, max_hours: 1.5, base_weight: 36, synergy_tags: {} },
      { card_id: "evt_ai_" + stamp + "_5", title: "补个长长的回笼觉", action_type: "SLEEP", min_hours: 1.0, max_hours: 3.0, base_weight: 42, synergy_tags: {} }
    ].map(normalizeCard);
    generatedCards.forEach(function (card) {
      var exists = false;
      for (var i = 0; i < deck.customCards.length; i++) {
        if (deck.customCards[i].title === card.title) exists = true;
      }
      if (!exists) deck.customCards.unshift(card);
    });
    var payload = buildCardsPayload(soul.soul_id);
    payload.generated_cards = clone(generatedCards);
    return payload;
  }

  /* ================================================================== *
   * 6. 角色卡 Prompt 预览 / AI 草稿
   * ================================================================== */
  function renderMockPrompt(card) {
    card = card || {};
    var lines = [];
    var name = cardFilled(card, "identity", "name", "小星");
    lines.push("你正在扮演「" + name + "」。");
    var sectionTitles = {};
    CARD_SCHEMA.forEach(function (section) {
      sectionTitles[section.id] = section.title;
    });
    CARD_SCHEMA.forEach(function (section) {
      if (section.id === "extra") return;
      var values = card[section.id] || {};
      var parts = [];
      section.fields.forEach(function (fieldDef) {
        var value = values[fieldDef.key];
        if (fieldDef.readonly) return;
        /* identity.avatar 是纯视觉元数据（可能是超长 Data URL），绝不进入 Persona Prompt */
        if (section.id === "identity" && fieldDef.key === "avatar") return;
        if (Array.isArray(value)) {
          if (!value.length) return;
          if (fieldDef.type === "dialogs") {
            value.forEach(function (dialog) {
              parts.push("- 用户：" + (dialog.user || "") + " / 角色：" + (dialog.character || ""));
            });
          } else {
            parts.push("- " + fieldDef.label + "：" + value.join("、"));
          }
        } else if (value != null && String(value).trim()) {
          parts.push("- " + fieldDef.label + "：" + String(value).trim());
        }
      });
      if (parts.length) {
        lines.push("");
        lines.push("## " + sectionTitles[section.id]);
        lines.push(parts.join("\n"));
      }
    });
    var prompt = lines.join("\n");
    return prompt;
  }

  function fieldDraft(fieldDef) {
    if (fieldDef.readonly) return "";
    if (fieldDef.type === "select") {
      return fieldDef.options && fieldDef.options.length ? fieldDef.options[0].value : "";
    }
    if (fieldDef.type === "tags") return ["AI 草稿标签", fieldDef.label];
    if (fieldDef.type === "list") return ["AI 草稿第一行", "AI 草稿第二行"];
    if (fieldDef.type === "dialogs") {
      return [
        { user: "在干嘛呢？", character: "刚忙完，正瘫着刷视频呢~" },
        { user: "出来玩吗？", character: "今天先不啦，改天一定！" }
      ];
    }
    return "【AI 草稿】" + fieldDef.label + "示例内容";
  }

  function aiDraftCard(soulId, seedText, section) {
    var soul = findSoul(soulId) || activeSoul();
    var draft = clone(soul.card || emptyCard());
    if (section && findSection(section)) {
      var values = {};
      findSection(section).fields.forEach(function (fieldDef) {
        values[fieldDef.key] = fieldDraft(fieldDef);
      });
      if (section === "identity") {
        values.native_persona_prompt = "";
      }
      draft[section] = values;
    } else {
      draft.identity = Object.assign({}, draft.identity || {}, {
        tagline: seedText ? "根据「" + seedText + "」生成的角色定位" : "AI 生成的一句话定位：嘴硬心软的夜猫子",
        nickname: "AI 昵称"
      });
      draft.personality = Object.assign({}, draft.personality || {}, {
        personality_tags: ["AI 标签·慢热", "AI 标签·吐槽役"],
        mbti: "INFP"
      });
      draft.speech = Object.assign({}, draft.speech || {}, {
        speaking_style: "AI 生成的说话风格：口语化短句，偶尔自嘲，喜欢用省略号。",
        catchphrases: ["真的假的", "笑死"],
        reply_length: "short"
      });
    }
    return draft;
  }

  /* ================================================================== *
   * 7. 默认角色 / UMO 绑定 / 模型配置
   * ================================================================== */
  /* v2.6 可选继承：decision / vision 的合法 inherit_from 取值 */
  var INHERIT_FROM_OPTIONS = {
    decision: ["", "main"],
    vision: ["", "main", "decision"]
  };

  function toBool(value, fallback) {
    if (value === true || value === false) return value;
    if (value == null) return !!fallback;
    return ["1", "true", "yes", "on", "y"].indexOf(String(value).trim().toLowerCase()) >= 0;
  }

  /** decision / vision 的 inherit_from 归一化（与 core/model_hub.py §1.1 同规则）。 */
  function normalizeInheritFrom(role, raw) {
    raw = raw && typeof raw === "object" ? raw : {};
    var options = INHERIT_FROM_OPTIONS[role] || ["", "main"];
    var has = Object.prototype.hasOwnProperty;
    var candidate = "";
    if (has.call(raw, "inherit_from") && raw.inherit_from != null) {
      candidate = String(raw.inherit_from).trim().toLowerCase();
      if (options.indexOf(candidate) < 0) candidate = toBool(raw.inherit, true) ? "main" : "";
    } else if (has.call(raw, "inherit")) {
      candidate = toBool(raw.inherit, true) ? "main" : "";
    } else {
      /* 旧配置（两个键都没有）：向后兼容默认继承主模型 */
      candidate = "main";
    }
    return candidate;
  }

  /** 四角色模型配置归一化：decision / vision 恒带 inherit + inherit_from 且两者同步。 */
  function normalizeModelConfig(raw) {
    raw = raw && typeof raw === "object" ? raw : {};
    var main = raw.main && typeof raw.main === "object" ? raw.main : {};
    var out = {
      main: {
        source_id: String(main.source_id == null ? "" : main.source_id).trim(),
        model: String(main.model == null ? "" : main.model).trim()
      }
    };
    ["decision", "vision"].forEach(function (role) {
      var roleRaw = raw[role] && typeof raw[role] === "object" ? raw[role] : {};
      var inheritFrom = normalizeInheritFrom(role, roleRaw);
      out[role] = {
        inherit: !!inheritFrom,
        inherit_from: inheritFrom,
        source_id: String(roleRaw.source_id == null ? "" : roleRaw.source_id).trim(),
        model: String(roleRaw.model == null ? "" : roleRaw.model).trim()
      };
    });
    var tts = raw.tts && typeof raw.tts === "object" ? raw.tts : {};
    var mode = String(tts.mode == null ? "off" : tts.mode).trim().toLowerCase();
    if (["off", "source", "provider"].indexOf(mode) < 0) mode = "off";
    var scope = String(tts.scope == null ? "private" : tts.scope).trim().toLowerCase();
    if (["private", "l3", "all"].indexOf(scope) < 0) scope = "private";
    var probability = Number(tts.probability);
    out.tts = {
      mode: mode,
      source_id: String(tts.source_id == null ? "" : tts.source_id).trim(),
      model: String(tts.model == null ? "" : tts.model).trim(),
      voice: String(tts.voice == null ? "" : tts.voice).trim(),
      provider_id: String(tts.provider_id == null ? "" : tts.provider_id).trim(),
      probability: isFinite(probability) ? Math.max(0, Math.min(1, probability)) : 1,
      scope: scope,
      dual_output: toBool(tts.dual_output, false)
    };
    return out;
  }

  /**
   * 角色当前生效的 (source_id, model)：独立配置取自己；继承按父角色递归解析，
   * 最多 2 跳（vision -> decision -> main）并防环；解析不到返回 null（该角色停用）。
   */
  function resolveModelPair(cfg, role, hops) {
    hops = hops || 0;
    cfg = cfg || {};
    if (role === "main") {
      var main = cfg.main || {};
      return main.source_id && main.model
        ? { source_id: main.source_id, model: main.model }
        : null;
    }
    var entry = cfg[role] || {};
    var inheritFrom = String(entry.inherit_from || "");
    if (!inheritFrom) {
      return entry.source_id && entry.model
        ? { source_id: entry.source_id, model: entry.model }
        : null;
    }
    if (inheritFrom === "main") return resolveModelPair(cfg, "main", hops + 1);
    if ((inheritFrom === "decision" || inheritFrom === "vision") && hops < 2) {
      return resolveModelPair(cfg, inheritFrom, hops + 1);
    }
    return null;
  }

  function saveSoul(soulId, body) {
    var soul = findSoul(soulId);
    if (!soul && body.card) {
      soul = { soul_id: soulId, card: emptyCard() };
      db.souls.push(soul);
    }
    if (!soul) throw new Error("角色不存在：" + soulId);
    if (body.card && typeof body.card === "object") {
      soul.card = deepMerge(soul.card || emptyCard(), body.card);
    }
    var card = soul.card || emptyCard();
    if (typeof body.name === "string" && !body.card) {
      card.identity.name = body.name;
    }
    if (typeof body.archetype === "string" && !body.card) {
      card.identity.tagline = body.archetype;
    }
    if (typeof body.speaking_style === "string" && !body.card) {
      card.speech.speaking_style = body.speaking_style;
    }
    if (Array.isArray(body.surfing_preferences) && !body.card) {
      card.interests.surfing_preferences = body.surfing_preferences;
    }
    if (typeof body.native_persona_prompt === "string" && !body.card) {
      card.extra.native_persona_prompt = body.native_persona_prompt;
    }
    if (body.umo_binding && typeof body.umo_binding === "object") {
      var umo = String(body.umo_binding.umo || "").trim();
      var target = String(body.umo_binding.soul_id || "").trim();
      if (umo) {
        if (target) db.umoBindings[umo] = target;
        else delete db.umoBindings[umo];
      }
    }
    syncLegacyFields(soul);
    return {
      soul: toSoulDict(soul),
      souls: listSoulDicts(),
      umo_bindings: clone(db.umoBindings),
      known_umos: knownUmos()
    };
  }

  function saveCardModel(soulId, body) {
    var soul = findSoul(soulId);
    if (!soul) throw new Error("角色不存在：" + soulId);
    if (body.card && typeof body.card === "object") {
      /* 与后端 normalize_card 行为对齐：按分区合并，缺失分区保留原值 */
      soul.card = deepMerge(soul.card || emptyCard(), body.card);
    }
    syncLegacyFields(soul);
    return { soul: toSoulDict(soul), souls: listSoulDicts() };
  }

  function importPersona(body) {
    var personaName = String(body.persona_name || "default_soul").trim();
    var targetId = String(body.target_soul_id || personaName).trim();
    var found = null;
    for (var i = 0; i < db.nativePersonas.length; i++) {
      if (db.nativePersonas[i].persona_name === personaName) found = db.nativePersonas[i];
    }
    var card = emptyCard();
    card.identity.name = targetId;
    card.identity.nickname = targetId;
    card.identity.tagline = "从 AstrBot 原生人格「" + personaName + "」导入的角色";
    card.identity.avatar = String(body.avatar == null ? "" : body.avatar).trim() || targetId.slice(0, 1);
    card.extra.native_persona_prompt = found ? found.prompt : String(body.native_prompt || "");
    card.rhythm.daily_routine = "白天忙自己的事，晚上刷手机水群。";
    var soul = { soul_id: targetId, card: card };
    var existed = findSoul(targetId);
    if (existed) {
      existed.card = card;
      soul = existed;
    } else {
      db.souls.push(soul);
    }
    deckOf(targetId);
    db.schedules[targetId] = buildRun(targetId, true);
    syncLegacyFields(soul);
    return {
      soul: toSoulDict(soul),
      schedule: buildSchedulePayload(targetId),
      souls: listSoulDicts()
    };
  }

  function renameSoul(oldId, newId) {
    if (!oldId || !newId) throw new Error("old_soul_id / new_soul_id 不能为空");
    var soul = findSoul(oldId);
    if (!soul) throw new Error("角色不存在：" + oldId);
    if (findSoul(newId)) throw new Error("新的 Soul ID 已存在：" + newId);
    soul.soul_id = newId;
    if (db.decks[oldId]) {
      db.decks[newId] = db.decks[oldId];
      delete db.decks[oldId];
    }
    if (db.schedules[oldId]) {
      db.schedules[newId] = db.schedules[oldId];
      db.schedules[newId].soul_id = newId;
      delete db.schedules[oldId];
    }
    Object.keys(db.umoBindings).forEach(function (umo) {
      if (db.umoBindings[umo] === oldId) db.umoBindings[umo] = newId;
    });
    db.memories.forEach(function (memory) {
      if (memory.soul_id === oldId) memory.soul_id = newId;
    });
    db.videos.forEach(function (video) {
      if (video.soul_id === oldId) video.soul_id = newId;
    });
    if (db.defaultSoulId === oldId) db.defaultSoulId = newId;
    syncLegacyFields(soul);
    return {
      soul: toSoulDict(soul),
      souls: listSoulDicts(),
      schedule: buildSchedulePayload(newId)
    };
  }

  function deleteSoul(soulId) {
    for (var i = db.souls.length - 1; i >= 0; i--) {
      if (db.souls[i].soul_id === soulId) db.souls.splice(i, 1);
    }
    delete db.decks[soulId];
    delete db.schedules[soulId];
    Object.keys(db.umoBindings).forEach(function (umo) {
      if (db.umoBindings[umo] === soulId) delete db.umoBindings[umo];
    });
    if (db.defaultSoulId === soulId) db.defaultSoulId = db.souls[0] ? db.souls[0].soul_id : "";
    if (!db.souls.length) {
      db.souls.push({ soul_id: "default_soul", card: buildCardJiu() });
      deckOf("default_soul");
    }
    return {
      deleted_soul_id: soulId,
      souls: listSoulDicts(),
      active_soul_id: db.souls[0].soul_id
    };
  }

  function defaultTitleFor(actionType) {
    var fallback = {
      SLEEP: "关灯睡觉",
      SURF: "窝在沙发里刷短视频",
      IDLE_ROAM: "在群里潜水窥屏",
      BUSY_TASK: "埋头赶工作进度"
    };
    return fallback[coerceActionType(actionType)] || "";
  }

  function forceState(body) {
    var soulId = String(body.soul_id || "").trim();
    var soul = findSoul(soulId) || activeSoul();
    if (!soul) throw new Error("没有可用角色");
    var run = getRun(soul.soul_id);
    var current = currentSlotOf(run);
    var slotKey = String(body.slot_key || "").trim();
    if (!slotKey && current) slotKey = current.slot.slot_key;
    var slot = null;
    for (var i = 0; i < run.slots.length; i++) {
      if (run.slots[i].slot_key === slotKey) slot = run.slots[i];
    }
    if (!slot) throw new Error("未知时间块：" + slotKey);
    /* action_type 优先；否则由 new_state 派生（RESTING→SLEEP / BUSY→BUSY_TASK / IDLE→IDLE_ROAM） */
    var actionType = coerceActionType(body.action_type);
    var newState = "";
    if (actionType) {
      newState = boundStateOf(actionType);
    } else {
      newState = String(body.new_state || "").toUpperCase();
      if (newState !== "IDLE" && newState !== "BUSY" && newState !== "RESTING") newState = slot.state;
      actionType = newState === "RESTING" ? "SLEEP" : newState === "BUSY" ? "BUSY_TASK" : "IDLE_ROAM";
      /* 与后端一致：裸 `state IDLE`（未给新标题）保留当前 SURF 行动 */
      if (newState === "IDLE" && slot.action_type === "SURF" && !String(body.new_title || "").trim()) {
        actionType = "SURF";
      }
    }
    slot.action_type = actionType;
    slot.state = newState;
    slot.label = actionLabelOf(actionType);
    var newTitle = body.new_title != null ? String(body.new_title).trim() : "";
    slot.title = newTitle || defaultTitleFor(actionType) || slot.title;
    slot.busy_settled = false;
    slot.completion_result = "";
    slot.mutated = false;
    slot.mutation_monologue = "";
    /* v2.6：忽略调用方传入的 armor，强制按行动类型固定档位（25/40/65/80） */
    slot.armor = fixedArmorOf(actionType);
    return {
      updated_slot: slotWithProgress(slot),
      schedule: buildSchedulePayload(soul.soul_id)
    };
  }

  function rerollSchedule(body) {
    var soul = findSoul(String(body.soul_id || "")) || activeSoul();
    if (!soul) throw new Error("没有可用角色");
    db.schedules[soul.soul_id] = buildRun(soul.soul_id, true);
    return { schedule: buildSchedulePayload(soul.soul_id) };
  }

  /* ================================================================== *
   * 8. 用户 / 记忆 / 冲浪
   * ================================================================== */
  function listUsers(tier) {
    var filterTier = tier != null && tier !== "" ? Number(tier) : null;
    var users = db.users.filter(function (user) {
      return filterTier == null || user.tier === filterTier;
    });
    return {
      users: clone(users),
      counts: {
        l3_count: db.users.filter(function (u) { return u.tier === 3; }).length,
        l2_count: db.users.filter(function (u) { return u.tier === 2; }).length,
        l1_count: db.users.filter(function (u) { return u.tier === 1; }).length,
        total: db.users.length
      }
    };
  }

  function updateUser(body) {
    var action = String(body.action || "update").toLowerCase();
    if (action === "merge") {
      var primary = null;
      var secondary = null;
      db.users.forEach(function (user) {
        if (user.global_uid === body.primary_uid) primary = user;
        if (user.global_uid === body.secondary_uid) secondary = user;
      });
      if (!primary || !secondary) throw new Error("待合并账号不存在");
      primary.platform_ids = (primary.platform_ids || []).concat(secondary.platform_ids || []);
      primary.interaction_count += secondary.interaction_count;
      /* v2.6：合并时主账号没有专属态度则继承被合并账号的 */
      if (!primary.attitude && secondary.attitude) primary.attitude = secondary.attitude;
      db.users = db.users.filter(function (user) {
        return user.global_uid !== secondary.global_uid;
      });
      return {
        merged_user: clone(primary),
        users: clone(db.users)
      };
    }
    if (action === "delete") {
      db.users = db.users.filter(function (user) {
        return user.global_uid !== body.global_uid;
      });
      return { users: clone(db.users) };
    }
    var target = null;
    db.users.forEach(function (user) {
      if (user.global_uid === body.global_uid) target = user;
    });
    if (!target) {
      if (!body.global_uid) throw new Error("用户账号 ID 不能为空");
      target = {
        global_uid: String(body.global_uid),
        display_name: String(body.display_name || body.global_uid),
        nickname: "",
        tier: 1,
        tier_label: "L1普通群友",
        interaction_count: 0,
        affinity: 0,
        platform_ids: [],
        attitude: ""
      };
      db.users.push(target);
    }
    if (body.display_name != null) target.display_name = String(body.display_name);
    if (body.nickname != null) target.nickname = String(body.nickname);
    if (body.tier != null && body.tier !== "") {
      target.tier = clamp(body.tier, 1, 3);
      target.tier_label = { 3: "★L3核心用户", 2: "◆L2重要熟人", 1: "L1普通群友" }[target.tier];
    }
    if (body.affinity != null && body.affinity !== "") {
      target.affinity = clamp(body.affinity, 0, 100);
    }
    /* v2.6：角色对 Ta 的专属态度，去首尾空白并截断到 300 字（与后端一致） */
    if (body.attitude != null) {
      target.attitude = String(body.attitude).trim().slice(0, 300);
    }
    if (Array.isArray(body.platform_ids)) target.platform_ids = body.platform_ids.slice();
    if (typeof body.platform_ids === "string") {
      target.platform_ids = body.platform_ids.split(",").map(function (x) { return x.trim(); }).filter(Boolean);
    }
    return { user: clone(target), users: clone(db.users) };
  }

  function memoryStats() {
    var counts = memoryCounts();
    return {
      tier3_count: counts.tier3,
      tier2_count: counts.tier2,
      tier1_count: counts.tier1,
      total: counts.total,
      sliding_window_size: 12,
      max_injected_memories: 5,
      max_memory_chars_budget: 400
    };
  }

  function listMemories(payload) {
    var soulId = payload.soul_id || "";
    var tier = payload.memory_tier != null && payload.memory_tier !== "" ? Number(payload.memory_tier) : null;
    var userId = payload.user_id || "";
    var keyword = String(payload.keyword || "").toLowerCase();
    var memories = db.memories.filter(function (memory) {
      if (soulId && memory.soul_id !== soulId) return false;
      if (tier != null && memory.memory_tier !== tier) return false;
      if (userId && memory.user_id !== userId) return false;
      if (keyword && (memory.content + " " + memory.keywords).toLowerCase().indexOf(keyword) < 0) return false;
      return true;
    });
    return { memories: clone(memories), stats: memoryStats() };
  }

  function mutateMemory(body) {
    var action = String(body.action || "add").toLowerCase();
    var soulId = String(body.soul_id || db.souls[0].soul_id);
    function memoriesForSoul() {
      return clone(db.memories.filter(function (memory) {
        return memory.soul_id === soulId;
      }));
    }
    if (action === "gc") {
      var removed = db.memories.filter(function (memory) {
        return memory.soul_id === soulId && memory.memory_tier === 1 && memory.vitality < 40;
      }).length;
      db.memories = db.memories.filter(function (memory) {
        return !(memory.soul_id === soulId && memory.memory_tier === 1 && memory.vitality < 40);
      });
      return {
        gc_stats: { deleted_tier1: removed, deleted_tier2: 0, promoted_to_tier3: 1 },
        memories: memoriesForSoul()
      };
    }
    if (action === "extract") {
      var newId = Date.now();
      db.memories.unshift({
        id: newId,
        soul_id: soulId,
        user_id: "core_tester",
        user_tier: 3,
        memory_tier: 2,
        content: "最近阿泽提到周末想去新开的咖啡店坐坐。",
        keywords: "咖啡店,周末,阿泽",
        importance: 6,
        vitality: 70,
        access_count: 0,
        created_at: nowSeconds(),
        last_accessed_at: nowSeconds()
      });
      return { extracted_count: 1, memories: memoriesForSoul() };
    }
    if (action === "pin_core") {
      var pinned = null;
      db.memories.forEach(function (memory) {
        if (memory.id === Number(body.id)) {
          memory.memory_tier = 3;
          memory.vitality = 100;
          pinned = memory;
        }
      });
      if (!pinned) throw new Error("未找到指定的记忆条目");
      return { memory: clone(pinned), memories: memoriesForSoul() };
    }
    if (action === "delete") {
      db.memories = db.memories.filter(function (memory) {
        return memory.id !== Number(body.id);
      });
      return { memories: memoriesForSoul() };
    }
    if (action === "edit") {
      var updated = null;
      db.memories.forEach(function (memory) {
        if (memory.id !== Number(body.id)) return;
        if (body.content != null) memory.content = String(body.content);
        if (body.keywords != null) memory.keywords = String(body.keywords);
        if (body.importance != null) memory.importance = clamp(body.importance, 1, 10);
        if (body.memory_tier != null) memory.memory_tier = clamp(body.memory_tier, 1, 3);
        if (body.vitality != null) memory.vitality = clamp(body.vitality, 0, 100);
        if (body.user_id != null) memory.user_id = String(body.user_id);
        updated = memory;
      });
      if (!updated) throw new Error("未找到要修改的记忆条目");
      return { memory: clone(updated), memories: memoriesForSoul() };
    }
    var content = String(body.content || "").trim();
    if (!content) throw new Error("记忆内容不能为空");
    var mid = Date.now();
    db.memories.unshift({
      id: mid,
      soul_id: soulId,
      user_id: String(body.user_id || ""),
      user_tier: Number(body.user_tier || 2),
      memory_tier: Number(body.memory_tier || 3),
      content: content,
      keywords: String(body.keywords || ""),
      importance: clamp(body.importance || 8, 1, 10),
      vitality: 100,
      access_count: 0,
      created_at: nowSeconds(),
      last_accessed_at: nowSeconds()
    });
    return { new_id: mid, memories: clone(db.memories.filter(function (memory) { return memory.soul_id === soulId; })) };
  }

  function videosForSoul(soulId) {
    return clone(
      db.videos.filter(function (video) {
        return !soulId || video.soul_id === soulId;
      })
    );
  }

  function surfingGet(payload) {
    return {
      videos: videosForSoul(payload.soul_id || ""),
      known_umos: knownUmos()
    };
  }

  function surfingPost(body) {
    var action = String(body.action || "surf").toLowerCase();
    var soul = findSoul(String(body.soul_id || "")) || activeSoul();
    if (!soul) throw new Error("没有可用角色");
    if (action === "delete") {
      var videoId = String(body.video_id || "");
      db.videos = db.videos.filter(function (video) {
        var hit = body.id != null ? video.id === Number(body.id) : video.video_id === videoId;
        return !hit;
      });
      return { videos: videosForSoul(soul.soul_id) };
    }
    if (action === "share") {
      var targetUmo = String(body.target_umo || "") || db.defaultUmoTarget;
      return {
        sent: true,
        target_umo: targetUmo,
        share_text: "分享一条刚刷到的视频～\n《" + (db.videos[0] ? db.videos[0].title : "视频") + "》"
      };
    }
    var template = db.videos[db.videos.length % Math.max(1, db.videos.length)] || {};
    var newVideo = {
      id: Date.now(),
      soul_id: soul.soul_id,
      video_id: "BV_mock_" + Date.now(),
      platform: body.platform || "bilibili",
      title: "刷到一只在自动扶梯上打滑的柴犬（新）",
      author: "每日一狗",
      cover_url: "",
      share_url: "https://www.bilibili.com/video/BV_mock",
      tags: ["狗子", "搞笑", "翻车"],
      hot_comments: ["狗：这楼梯怎么不动了", "笑不活了"],
      visual_notes: "柴犬在扶梯口犹豫了两秒，然后一路小碎步打滑下去，最后一脸茫然地回头看镜头。",
      score: 88,
      commentary: "救命 hhhh 这只狗的表情管理完全崩了，像极了迟到跑进公司的我。",
      worth_sharing: true,
      watched_at: nowSeconds()
    };
    if (template.video_id) newVideo.video_id = "BV_mock_" + Date.now();
    db.videos.unshift(newVideo);
    return {
      review: clone(newVideo),
      videos: videosForSoul(soul.soul_id),
      schedule: buildSchedulePayload(soul.soul_id)
    };
  }

  /* ================================================================== *
   * 9. 路由分发
   * ================================================================== */
  function handleEndpoint(method, endpoint, payload) {
    payload = payload && typeof payload === "object" ? payload : {};
    switch (endpoint) {
      /* ---- 17 条旧路由 ---- */
      case "init_summary":
        return buildInitSummary();
      case "souls/detail":
        return buildSoulDetail(payload.soul_id || "");
      case "souls/import_persona":
        return importPersona(payload);
      case "souls/save":
        return saveSoul(String(payload.soul_id || ""), payload);
      case "souls/rename":
        return renameSoul(String(payload.old_soul_id || ""), String(payload.new_soul_id || ""));
      case "souls/delete":
        return deleteSoul(String(payload.soul_id || ""));
      case "schedule/reroll":
        return rerollSchedule(payload);
      case "schedule/force_state":
        return forceState(payload);
      case "cards/list":
        return buildCardsPayload(String(payload.soul_id || ""));
      case "cards/save":
        return saveCard(String(payload.soul_id || ""), payload);
      case "cards/delete":
        return deleteCard(String(payload.soul_id || ""), payload);
      case "cards/ai_generate":
        return aiGenerateCards(String(payload.soul_id || ""));
      case "users/list":
        return listUsers(payload.tier);
      case "users/update":
        return updateUser(payload);
      case "memories/list":
        return listMemories(payload);
      case "memories/mutate":
        return mutateMemory(payload);
      case "surfing/list_and_trigger":
        return method === "GET" ? surfingGet(payload) : surfingPost(payload);

      /* ---- 10 条新路由 ---- */
      case "dashboard/overview":
        return buildOverview(payload.soul_id || "");
      case "card/schema": {
        var schemaSoul = findSoul(payload.soul_id || "") || activeSoul();
        return {
          schema: clone(CARD_SCHEMA),
          card: clone(schemaSoul ? schemaSoul.card : emptyCard()),
          soul_id: schemaSoul ? schemaSoul.soul_id : ""
        };
      }
      case "card/save": {
        var saved = saveCardModel(String(payload.soul_id || ""), payload);
        saved.souls = listSoulDicts();
        return saved;
      }
      case "card/ai_generate": {
        var draftSoulId = String(payload.soul_id || "") || (activeSoul() ? activeSoul().soul_id : "");
        return {
          draft_card: aiDraftCard(draftSoulId, String(payload.seed_text || ""), payload.section || null),
          section: payload.section || null
        };
      }
      case "card/preview_prompt": {
        var promptSoul = findSoul(payload.soul_id || "") || activeSoul();
        var promptCard =
          payload.card && typeof payload.card === "object"
            ? payload.card
            : promptSoul
            ? promptSoul.card
            : emptyCard();
        var prompt = renderMockPrompt(promptCard);
        return { prompt: prompt, chars: prompt.length };
      }
      case "models/sources":
        return {
          sources: clone(db.sources),
          tts_providers: clone(db.ttsProviders)
        };
      case "models/list": {
        var sourceId = String(payload.source_id || "");
        return { source_id: sourceId, models: clone(db.modelLists[sourceId] || []) };
      }
      case "models/config":
        if (method === "POST") {
          var incoming = payload.model_config && typeof payload.model_config === "object" ? payload.model_config : payload;
          /* v2.6：保存时归一化 inherit / inherit_from，返回完整 canonical 配置 */
          db.modelConfig = normalizeModelConfig(incoming);
          return { model_config: clone(db.modelConfig), status: buildModelStatus() };
        }
        return { model_config: clone(normalizeModelConfig(db.modelConfig || {})), status: buildModelStatus() };
      case "models/test": {
        var role = String(payload.role || "main");
        if (role === "tts") {
          return {
            status: "ok",
            role: "tts",
            latency_ms: 420,
            audio_path: "/tmp/onlyyou_mock_tts.mp3",
            audio_base64: mockSilentWavBase64(400)
          };
        }
        return {
          status: "ok",
          role: role,
          provider_id: "deepseek/deepseek-chat",
          model: "deepseek-chat",
          latency_ms: 640 + Math.round(Math.random() * 240),
          reply: "pong～这里是 " + role + " 角色的 Mock 连通性测试回复。"
        };
      }
      case "sessions/default_soul": {
        var newDefault = String(payload.soul_id || "");
        if (newDefault && !findSoul(newDefault)) throw new Error("角色不存在：" + newDefault);
        db.defaultSoulId = newDefault;
        return {
          default_soul_id: db.defaultSoulId,
          souls: listSoulDicts(),
          umo_bindings: clone(db.umoBindings)
        };
      }
      default:
        throw new Error("Mock 未覆盖的接口：" + method + " " + endpoint);
    }
  }

  /** 生成一段静音 WAV 的 base64（供 TTS 试听测试用，纯本地无依赖） */
  function mockSilentWavBase64(durationMs) {
    var sampleRate = 8000;
    var samples = Math.max(1, Math.floor((sampleRate * (durationMs || 300)) / 1000));
    var buffer = new Uint8Array(44 + samples);
    var view = new DataView(buffer.buffer);
    function writeString(offset, text) {
      for (var i = 0; i < text.length; i++) view.setUint8(offset + i, text.charCodeAt(i));
    }
    writeString(0, "RIFF");
    view.setUint32(4, 36 + samples, true);
    writeString(8, "WAVE");
    writeString(12, "fmt ");
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true);
    view.setUint16(22, 1, true);
    view.setUint32(24, sampleRate, true);
    view.setUint32(28, sampleRate, true);
    view.setUint16(32, 1, true);
    view.setUint16(34, 8, true);
    writeString(36, "data");
    view.setUint32(40, samples, true);
    var binary = "";
    for (var i = 0; i < buffer.length; i++) binary += String.fromCharCode(buffer[i]);
    return window.btoa(binary);
  }

  async function handle(method, endpoint, payload) {
    await sleep(MOCK_DELAY);
    try {
      var data = handleEndpoint(method, endpoint, payload);
      return ok(data);
    } catch (err) {
      return fail((err && (err.message || err)) || "mock error");
    }
  }

  /* ================================================================== *
   * 10. 安装 Mock Bridge
   * ================================================================== */
  var mockBridge = {
    ready: function () {
      return Promise.resolve({
        isDark: forcedDark,
        theme: forcedDark ? "dark" : "light",
        page: { id: "console", name: "OnlyYou" }
      });
    },
    getContext: function () {
      return { isDark: forcedDark, theme: forcedDark ? "dark" : "light" };
    },
    onContext: function (callback) {
      window.__OY_MOCK_CONTEXT_CALLBACK__ = callback;
    },
    apiGet: function (endpoint, params) {
      return handle("GET", endpoint, params);
    },
    apiPost: function (endpoint, body) {
      return handle("POST", endpoint, body);
    }
  };

  window.AstrBotPluginPage = mockBridge;
  window.AstrBotPluginView = mockBridge;
  window.__OY_MOCK__ = true;
  window.OY_MOCK_DB = db;
  console.info(
    "[OnlyYou] Mock bridge 已安装：覆盖 27 条 API（17 旧 + 10 新），模拟延迟 " + MOCK_DELAY + "ms。"
  );
})();
