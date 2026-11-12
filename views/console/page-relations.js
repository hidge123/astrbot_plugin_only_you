/**
 * OnlyYou Plugin Pages — 关系与群聊页 (`views/console/page-relations.js`)
 *
 * v2.8 重设计（契约 §7.1 / §7.2）：废弃三列看板 / HTML5 拖拽 / 编辑抽屉 / 「⋯」小菜单，
 * 改为「筛选 chips + 即时搜索 + ★L3→◆L2→L1 横向列表行 + 居中宽幅详情弹窗 + 💬群聊卡片」。
 *
 * 数据源：
 *   GET  `users/list`   → {
 *     users: [UserProfile],
 *     counts: { l3_count, l2_count, l1_count, total },
 *     groups: [GroupItem]   // v2.8 新增（契约 §6.2）
 *   }
 *   POST `users/update` → 默认 action 更新用户（display_name / nickname / tier / affinity / attitude / platform_ids）；
 *                         action="merge" 合并其他账号；action="delete" 删除用户；
 *                         action="update_group" / "clear_group_memory" / "delete_group" 维护群聊档案。
 *
 * ★L3 = 管理员级：不参与好感度计算，也不会被好感度自动晋升（自动晋升只到 L2）。
 * 由 AstrBot admins_id / relationship_config.core_user_ids 自动确定，也可在详情弹窗里显式设为 L3。
 *
 * 注册：window.OY_PAGES.relations = { id, title, icon, render(container, ctx), destroy() }
 */
(function () {
  "use strict";

  var TIER_META = {
    3: {
      label: "★L3 核心用户",
      short: "★L3 核心",
      color: "var(--tier-l3)",
      icon: "spark",
      tip: "管理员级：免好感度 · 不参与自动晋升"
    },
    2: {
      label: "◆L2 重要熟人",
      short: "◆L2 重要",
      color: "var(--tier-l2)",
      icon: "heart",
      tip: "重要熟人：好感度自动晋升只到 L2"
    },
    1: {
      label: "L1 普通",
      short: "L1 普通",
      color: "var(--tier-l1)",
      icon: "user",
      tip: "普通群友：互动变多会自动晋升 L2"
    }
  };
  var TIER_ORDER = [3, 2, 1];
  var FILTER_CHIPS = [
    { key: "", label: "全部" },
    { key: "3", label: "★L3 核心" },
    { key: "2", label: "◆L2 重要" },
    { key: "1", label: "L1 普通" },
    { key: "groups", label: "💬 群聊" }
  ];
  var ATTITUDE_MAX = 300;
  var L3_WARNING = "★L3 为管理员级，不参与好感度计算与自动晋升";

  var S = {
    app: null,
    container: null,
    destroyed: false,
    users: [],
    groups: [],
    counts: { l3_count: 0, l2_count: 0, l1_count: 0, total: 0 },
    filter: "",
    keyword: "",
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

  function safeList(value) {
    return Array.isArray(value) ? value : [];
  }

  /** 统一把秒级时间戳归一为秒（兼容毫秒 / 空值）。 */
  function toSeconds(value) {
    var n = Number(value);
    if (!isFinite(n) || n <= 0) return 0;
    if (n > 1e12) n = Math.floor(n / 1000);
    return n;
  }

  /** 相对时间：刚刚 / N 分钟前 / N 小时前 / N 天前 / 日期；0 / 空 → 未知。 */
  function fmtAgo(value) {
    var ts = toSeconds(value);
    if (!ts) return "未知";
    var diff = Math.max(0, Math.floor(Date.now() / 1000) - ts);
    if (diff < 60) return "刚刚";
    if (diff < 3600) return Math.floor(diff / 60) + " 分钟前";
    if (diff < 86400) return Math.floor(diff / 3600) + " 小时前";
    if (diff < 86400 * 30) return Math.floor(diff / 86400) + " 天前";
    var date = new Date(ts * 1000);
    var pad = function (n) {
      return n < 10 ? "0" + n : String(n);
    };
    return date.getFullYear() + "-" + pad(date.getMonth() + 1) + "-" + pad(date.getDate());
  }

  function tierMeta(tier) {
    return TIER_META[Number(tier)] || TIER_META[1];
  }

  function computeCounts(users) {
    var counts = { l3_count: 0, l2_count: 0, l1_count: 0, total: users.length };
    users.forEach(function (user) {
      var tier = Number(user && user.tier) || 1;
      if (tier === 3) counts.l3_count += 1;
      else if (tier === 2) counts.l2_count += 1;
      else counts.l1_count += 1;
    });
    return counts;
  }

  function nameOf(user) {
    if (!user) return "未命名用户";
    return String(user.display_name || user.nickname || user.global_uid || "未命名用户");
  }

  function nicknameOf(user) {
    return String((user && user.nickname) || "");
  }

  function findUser(uid) {
    var wanted = String(uid == null ? "" : uid);
    for (var i = 0; i < S.users.length; i++) {
      if (S.users[i] && String(S.users[i].global_uid) === wanted) return S.users[i];
    }
    return null;
  }

  function normalizeGroup(raw) {
    var group = raw && typeof raw === "object" ? raw : {};
    var umo = String(group.umo || "");
    var groupId = String(group.group_id || "");
    if (!groupId) {
      var parts = umo.split(":");
      groupId = parts.length > 2 ? parts[parts.length - 1] : umo;
    }
    return {
      umo: umo,
      group_id: groupId,
      platform_id: String(group.platform_id || umo.split(":")[0] || ""),
      display_name: String(group.display_name || ("群聊 " + (groupId || "-"))),
      notes: String(group.notes == null ? "" : group.notes),
      bound_soul_id: String(group.bound_soul_id || ""),
      bound_soul_name: String(group.bound_soul_name || ""),
      window_count: num(group.window_count, 0),
      unread_count: num(group.unread_count, 0),
      last_active_at: num(group.last_active_at, 0)
    };
  }

  function normalizeGroups(list) {
    return safeList(list).map(normalizeGroup);
  }

  function findGroup(umo) {
    var wanted = String(umo == null ? "" : umo);
    for (var i = 0; i < S.groups.length; i++) {
      if (S.groups[i] && S.groups[i].umo === wanted) return S.groups[i];
    }
    return null;
  }

  /** 头像内部 HTML：优先使用 app.renderAvatarInner（图片 / Emoji / 首字回退）。 */
  function avatarInner(avatarVal, fallbackName) {
    if (S.app && typeof S.app.renderAvatarInner === "function") {
      return S.app.renderAvatarInner(avatarVal, fallbackName);
    }
    var raw = String(avatarVal == null ? "" : avatarVal).trim();
    if (raw) return esc(raw);
    var name = String(fallbackName == null ? "" : fallbackName).trim();
    return esc(name ? name.slice(0, 1) : "?");
  }

  function affinityPercent(value) {
    var n = num(value, 0);
    if (n <= 0) return 0;
    return Math.max(2, Math.min(100, n));
  }

  /** 平台标签：从 platform_ids 里按平台聚合（qq:123 / qq:456 → `qq ×2`）。
   *  v2.8.4：不含冒号的旧版裸 ID 不是平台账号，单独渲染为「旧格式」告警标签。 */
  function platformTagsHtml(platformIds, limit) {
    var ids = safeList(platformIds);
    if (!ids.length) return "";
    var order = [];
    var counter = {};
    var legacy = 0;
    ids.forEach(function (item) {
      var text = String(item == null ? "" : item).trim();
      if (!text) return;
      if (text.indexOf(":") < 0) {
        legacy += 1;
        return;
      }
      var platform = text.split(":")[0] || "账号";
      if (!Object.prototype.hasOwnProperty.call(counter, platform)) {
        counter[platform] = 0;
        order.push(platform);
      }
      counter[platform] += 1;
    });
    if (!order.length && !legacy) return "";
    var max = limit || 3;
    var html = order
      .slice(0, max)
      .map(function (platform) {
        var count = counter[platform] > 1 ? " ×" + counter[platform] : "";
        return (
          '<span class="tag rel-platform-tag" data-tip="' +
          esc(platform + " 平台账号") +
          '">' +
          esc(platform + count) +
          "</span>"
        );
      })
      .join("");
    if (order.length > max) {
      html += '<span class="tag tag-muted rel-platform-more">+' + (order.length - max) + "</span>";
    }
    if (legacy) {
      html +=
        '<span class="tag rel-platform-tag is-legacy" data-tip="旧版数据：只有 global_uid、缺少 platform_id 前缀。打开详情补全平台或删除该行。">' +
        icon("alert", 11) +
        " 旧格式 " +
        legacy +
        "</span>";
    }
    return '<span class="rel-platform-tags">' + html + "</span>";
  }

  /** 已知机器人接口（来自角色绑定 / 历史会话 UMO），用于平台账号输入联想。 */
  function knownPlatformIds(app) {
    var order = [];
    var seen = {};
    function add(pid) {
      var text = String(pid == null ? "" : pid).trim();
      if (!text || seen[text]) return;
      seen[text] = 1;
      order.push(text);
    }
    var state = app && app.state ? app.state : {};
    safeList(state.souls).forEach(function (soul) {
      safeList(soul && soul.bound_platforms).forEach(add);
    });
    safeList(state.knownUmos).forEach(function (item) {
      var umo = item && item.umo ? item.umo : item;
      if (typeof umo === "string" && umo.indexOf(":") > 0) add(umo.split(":")[0]);
    });
    Object.keys(state.umoBindings || {}).forEach(function (umo) {
      if (typeof umo === "string" && umo.indexOf(":") > 0) add(umo.split(":")[0]);
    });
    return order;
  }

  /* ------------------------------------------------------------------ *
   * 客户端筛选 / 搜索（即时，与 chips 组合）
   * ------------------------------------------------------------------ */
  function keywordText() {
    return String(S.keyword || "").trim().toLowerCase();
  }

  function userMatches(user) {
    var kw = keywordText();
    if (!kw) return true;
    var text = [
      user.display_name,
      user.nickname,
      user.global_uid,
      safeList(user.platform_ids).join(" "),
      user.attitude,
      user.tier_label
    ]
      .map(function (item) {
        return String(item == null ? "" : item);
      })
      .join(" ")
      .toLowerCase();
    return text.indexOf(kw) >= 0;
  }

  function groupMatches(group) {
    var kw = keywordText();
    if (!kw) return true;
    var text = [
      group.display_name,
      group.group_id,
      group.umo,
      group.platform_id,
      group.notes,
      group.bound_soul_name,
      group.bound_soul_id
    ]
      .map(function (item) {
        return String(item == null ? "" : item);
      })
      .join(" ")
      .toLowerCase();
    return text.indexOf(kw) >= 0;
  }

  function filteredUsers() {
    return S.users.filter(userMatches);
  }

  function filteredGroups() {
    return S.groups.filter(groupMatches);
  }

  /** 当前筛选 chip 下需要展示的用户 / 群聊（搜索已经在内部生效）。 */
  function visibleEntities() {
    if (S.filter === "groups") return { users: [], groups: filteredGroups() };
    if (S.filter) {
      var tier = Number(S.filter);
      return {
        users: filteredUsers().filter(function (user) {
          return (Number(user.tier) || 1) === tier;
        }),
        groups: []
      };
    }
    return { users: filteredUsers(), groups: filteredGroups() };
  }

  /* ------------------------------------------------------------------ *
   * 空态
   * ------------------------------------------------------------------ */
  function emptyStateHtml() {
    var title = "还没有任何关系或群聊档案";
    var text =
      "在群聊 / 私聊里和 Bot 互动后，用户与群聊会自动出现在这里；也可以在「会话」页查看机器人接口绑定。";
    var actions = "";
    if (S.keyword) {
      title = "没有找到匹配的关系或群聊";
      text =
        "关键词「" +
        S.keyword +
        "」没有命中用户名称、昵称、UID、平台账号、专属态度、群名、群号或 UMO。试试换个关键词。";
      actions =
        '<button class="btn btn-outline" type="button" data-action="clear-search">' +
        icon("close", 13) +
        "清空搜索</button>";
    } else if (S.filter === "groups") {
      title = "还没有群聊档案";
      text =
        "Bot 在群聊里收到消息后会自动记录群聊档案（群名、滑窗条数、未读数与最近活跃时间）。";
    } else if (S.filter) {
      var meta = tierMeta(Number(S.filter));
      title = "还没有 " + meta.label + " 用户";
      text =
        Number(S.filter) === 3
          ? "★L3 为管理员级，可由 AstrBot admins_id / core_user_ids 自动确定，也可以在用户详情里显式设为 L3。"
          : "在群聊 / 私聊里和 Bot 互动后，用户会自动出现在这里。";
    }
    return (
      '<div class="card rel-empty-card"><div class="empty">' +
      icon("relations", 32) +
      '<div class="empty-title">' +
      esc(title) +
      "</div>" +
      '<div class="empty-text">' +
      esc(text) +
      "</div>" +
      '<div class="row mt-12">' +
      actions +
      '<button class="btn btn-outline" type="button" data-action="reload">' +
      icon("refresh", 13) +
      "刷新</button></div>" +
      "</div></div>"
    );
  }

  function sectionEmptyHtml(message) {
    return '<div class="rel-section-empty">' + esc(message) + "</div>";
  }

  /* ------------------------------------------------------------------ *
   * 列表行
   * ------------------------------------------------------------------ */
  function affinityCellHtml(user, tier) {
    if (tier === 3) {
      return (
        '<div class="rel-row-affinity is-admin">' +
        '<span class="tag tag-primary rel-admin-tag">管理员级 · 免好感</span>' +
        '<span class="rel-row-affinity-label">不参与好感度计算</span>' +
        "</div>"
      );
    }
    var affinity = num(user.affinity, 0);
    return (
      '<div class="rel-row-affinity">' +
      '<span class="rel-row-affinity-label"><span>好感度</span><span>' +
      esc(affinity.toFixed(1)) +
      "</span></span>" +
      '<div class="mini-bar"><i style="width:' +
      affinityPercent(affinity).toFixed(1) +
      '%"></i></div></div>'
    );
  }

  function userRowHtml(user) {
    var tier = Number(user.tier) || 1;
    if (!TIER_META[tier]) tier = 1;
    var meta = TIER_META[tier];
    var name = nameOf(user);
    var nickname = nicknameOf(user);
    var uid = String(user.global_uid || "");
    var attitude = String(user.attitude == null ? "" : user.attitude).trim();
    var attitudeSnippet = attitude
      ? attitude.length > 42
        ? attitude.slice(0, 42) + "…"
        : attitude
      : "";
    return (
      '<div class="rel-row" data-uid="' +
      esc(uid) +
      '" data-tier="' +
      tier +
      '" style="--tier-color:' +
      meta.color +
      '" role="button" tabindex="0" aria-label="查看用户 ' +
      esc(name) +
      ' 的详情">' +
      '<span class="avatar rel-avatar">' +
      avatarInner(user.avatar, name) +
      "</span>" +
      '<div class="rel-row-main">' +
      '<div class="rel-row-title">' +
      '<strong class="rel-row-name" title="' +
      esc(name) +
      '">' +
      esc(name) +
      "</strong>" +
      '<span class="tier-pill tier-' +
      tier +
      '">' +
      esc(meta.short) +
      "</span>" +
      platformTagsHtml(user.platform_ids, 3) +
      "</div>" +
      '<div class="rel-row-sub">' +
      (uid ? '<span class="rel-row-uid mono" title="global_uid">' + esc(uid) + "</span>" : "") +
      (nickname && nickname !== name
        ? '<span class="rel-row-nickname">专属称呼：' + esc(nickname) + "</span>"
        : "") +
      (attitudeSnippet
        ? '<span class="rel-attitude-snippet" title="' +
          esc(attitude) +
          '">态度：「' +
          esc(attitudeSnippet) +
          "」</span>"
        : "") +
      "</div></div>" +
      affinityCellHtml(user, tier) +
      '<div class="rel-row-metrics"><span>互动 <strong>' +
      esc(num(user.interaction_count, 0)) +
      "</strong> 次</span></div>" +
      '<div class="rel-row-action"><button class="btn btn-outline btn-sm" type="button" data-action="detail-user" data-uid="' +
      esc(uid) +
      '">' +
      "详情" +
      "</button></div></div>"
    );
  }

  function groupRowHtml(group) {
    var unread = num(group.unread_count, 0);
    var bound = group.bound_soul_name || group.bound_soul_id || "-";
    var notes = String(group.notes == null ? "" : group.notes).trim();
    var notesSnippet = notes
      ? notes.length > 48
        ? notes.slice(0, 48) + "…"
        : notes
      : "";
    return (
      '<div class="rel-row is-group" data-umo="' +
      esc(group.umo) +
      '" role="button" tabindex="0" aria-label="查看群聊 ' +
      esc(group.display_name) +
      ' 的详情">' +
      '<span class="avatar rel-avatar rel-group-avatar">' +
      icon("sessions", 18) +
      "</span>" +
      '<div class="rel-row-main">' +
      '<div class="rel-row-title">' +
      '<strong class="rel-row-name" title="' +
      esc(group.display_name) +
      '">' +
      esc(group.display_name) +
      "</strong>" +
      '<span class="tag tag-primary rel-group-tag">💬 群聊</span>' +
      (group.platform_id
        ? '<span class="tag rel-platform-tag">' + esc(group.platform_id) + "</span>"
        : "") +
      "</div>" +
      '<div class="rel-row-sub">' +
      (group.group_id
        ? '<span class="mono" title="group_id">' + esc(group.group_id) + "</span>"
        : "") +
      "<span>滑窗 <strong>" +
      esc(num(group.window_count, 0)) +
      "</strong> 条</span>" +
      '<span class="rel-group-unread' +
      (unread > 0 ? " is-hot" : "") +
      '">未读 <strong>' +
      esc(unread) +
      "</strong></span>" +
      "<span>绑定：" +
      esc(bound) +
      "</span>" +
      "<span>最近活跃 " +
      esc(fmtAgo(group.last_active_at)) +
      "</span></div>" +
      (notesSnippet
        ? '<div class="rel-row-sub"><span class="rel-attitude-snippet" title="' +
          esc(notes) +
          '">备注：' +
          esc(notesSnippet) +
          "</span></div>"
        : "") +
      "</div>" +
      '<div class="rel-row-action"><button class="btn btn-outline btn-sm" type="button" data-action="detail-group" data-umo="' +
      esc(group.umo) +
      '">' +
      "详情" +
      "</button></div></div>"
    );
  }

  function userSectionHtml(tier, rows) {
    var meta = tierMeta(tier);
    /* 全部筛选下不展示空的用户分组，避免三个空标题占位 */
    if (!S.filter && !rows.length) return "";
    var body = rows.length
      ? '<div class="rel-rows">' + rows.map(userRowHtml).join("") + "</div>"
      : sectionEmptyHtml("该等级下暂无用户");
    return (
      '<section class="rel-section" data-tier-section="' +
      tier +
      '" style="--tier-color:' +
      meta.color +
      '">' +
      '<div class="rel-section-head">' +
      '<span class="rel-section-title">' +
      icon(meta.icon, 14) +
      "<span>" +
      esc(meta.label) +
      "</span></span>" +
      '<span class="rel-section-count">' +
      esc(rows.length) +
      "</span>" +
      '<span class="rel-section-tip">' +
      esc(meta.tip) +
      "</span></div>" +
      body +
      "</section>"
    );
  }

  function groupSectionHtml(rows) {
    var body = rows.length
      ? '<div class="rel-rows">' + rows.map(groupRowHtml).join("") + "</div>"
      : sectionEmptyHtml("没有匹配的群聊档案");
    return (
      '<section class="rel-section rel-group-section" data-group-section="1">' +
      '<div class="rel-section-head">' +
      '<span class="rel-section-title">' +
      icon("sessions", 14) +
      "<span>💬 群聊</span></span>" +
      '<span class="rel-section-count">' +
      esc(rows.length) +
      "</span>" +
      '<span class="rel-section-tip">滑窗 / 未读为内存态 · 群档案持久保存</span></div>' +
      body +
      "</section>"
    );
  }

  function listHtml() {
    var visible = visibleEntities();
    if (!visible.users.length && !visible.groups.length) return emptyStateHtml();
    if (S.filter === "groups") return groupSectionHtml(visible.groups);
    if (S.filter) return userSectionHtml(Number(S.filter), visible.users);
    var html = "";
    TIER_ORDER.forEach(function (tier) {
      var rows = visible.users.filter(function (user) {
        return (Number(user.tier) || 1) === tier;
      });
      html += userSectionHtml(tier, rows);
    });
    if (visible.groups.length) html += groupSectionHtml(visible.groups);
    return html;
  }

  /* ------------------------------------------------------------------ *
   * 页面骨架 / 工具栏
   * ------------------------------------------------------------------ */
  function chipHtml(item) {
    var count = null;
    if (item.key === "3") count = num(S.counts.l3_count, 0);
    else if (item.key === "2") count = num(S.counts.l2_count, 0);
    else if (item.key === "1") count = num(S.counts.l1_count, 0);
    else if (item.key === "groups") count = S.groups.length;
    var active = S.filter === item.key;
    return (
      '<button class="rel-chip' +
      (active ? " is-active" : "") +
      '" type="button" data-action="filter" data-filter="' +
      esc(item.key) +
      '" aria-pressed="' +
      (active ? "true" : "false") +
      '">' +
      '<span class="rel-chip-label">' +
      esc(item.label) +
      "</span>" +
      (count == null ? "" : '<span class="rel-chip-count">' + esc(count) + "</span>") +
      "</button>"
    );
  }

  function pageHeadHtml() {
    var totalUsers = num(S.counts.total, S.users.length);
    return (
      '<div class="page-head"><h1 class="page-title">' +
      icon("relations", 18) +
      "关系与群聊" +
      '<span class="card-sub">' +
      esc(totalUsers) +
      " 位用户 · " +
      esc(S.groups.length) +
      " 个群聊</span></h1></div>"
    );
  }

  function toolbarHtml() {
    return (
      '<div class="card rel-toolbar"><div class="card-body tight">' +
      '<div class="rel-toolbar-row">' +
      '<div class="rel-chips" role="group" aria-label="关系筛选">' +
      FILTER_CHIPS.map(chipHtml).join("") +
      "</div>" +
      '<div class="rel-search">' +
      icon("search", 14) +
      '<input class="input" type="text" data-role="relations-search" autocomplete="off" placeholder="🔍 搜索用户名称、昵称、UID、平台账号、专属态度或群号..." aria-label="搜索关系与群聊" value="' +
      esc(S.keyword) +
      '" />' +
      "</div>" +
      '<button class="btn btn-outline btn-sm rel-refresh-btn" type="button" data-action="reload" data-tip="重新拉取用户与群聊档案">' +
      icon("refresh", 13) +
      "刷新</button>" +
      "</div>" +
      '<div class="rel-toolbar-hint">' +
      "<span>★L3 管理员级不参与好感度计算与自动晋升；自动晋升只到 L2</span>" +
      "<span>群聊滑窗 / 未读为内存态：清空只清内存滑窗 + 未读；保留 SQLite memories / users / surfing_history / 群档案（重启后未读队列会重置）</span>" +
      "</div></div></div>"
    );
  }

  function pageHtml() {
    return (
      pageHeadHtml() +
      toolbarHtml() +
      '<div data-role="relations-list">' +
      listHtml() +
      "</div>"
    );
  }

  /* ------------------------------------------------------------------ *
   * 数据加载 / 局部刷新
   * ------------------------------------------------------------------ */
  function applyUsersPayload(data) {
    if (!data || typeof data !== "object") return;
    if (Array.isArray(data.users)) S.users = data.users;
    S.counts =
      data.counts && typeof data.counts === "object" ? data.counts : computeCounts(S.users);
    if (!isFinite(Number(S.counts.total))) S.counts.total = S.users.length;
    if (Array.isArray(data.groups)) S.groups = normalizeGroups(data.groups);
  }

  function applyGroupsPayload(data) {
    if (!data || typeof data !== "object") return;
    if (Array.isArray(data.groups)) S.groups = normalizeGroups(data.groups);
    if (data.counts && typeof data.counts === "object") S.counts = data.counts;
  }

  async function loadData(app) {
    var data = await app.apiGet("users/list");
    if (!data || typeof data !== "object") throw new Error("GET users/list 返回为空");
    applyUsersPayload(data);
  }

  function listEl() {
    return S.container ? S.container.querySelector('[data-role="relations-list"]') : null;
  }

  function paintList() {
    var el = listEl();
    if (!el) {
      paint();
      return;
    }
    el.innerHTML = listHtml();
  }

  function paintChips() {
    if (!S.container) return;
    S.container.querySelectorAll(".rel-chip[data-action='filter']").forEach(function (chip) {
      var active = chip.getAttribute("data-filter") === S.filter;
      chip.classList.toggle("is-active", active);
      chip.setAttribute("aria-pressed", active ? "true" : "false");
    });
  }

  function setFilter(key) {
    var next = key == null ? "" : String(key);
    if (S.filter === next) return;
    S.filter = next;
    paintChips();
    paintList();
  }

  function paint() {
    if (!S.container || S.destroyed) return;
    var scroller = S.container.closest ? S.container.closest(".main") : null;
    var top = scroller ? scroller.scrollTop : 0;
    var active = document.activeElement;
    var searchFocused = !!(
      active &&
      active.getAttribute &&
      active.getAttribute("data-role") === "relations-search"
    );
    S.container.innerHTML = pageHtml();
    if (scroller) scroller.scrollTop = top;
    if (searchFocused) {
      var input = S.container.querySelector('[data-role="relations-search"]');
      if (input) {
        try {
          input.focus();
          input.setSelectionRange(input.value.length, input.value.length);
        } catch (err) {
          /* ignore */
        }
      }
    }
  }

  function showLoadError(err) {
    if (!S.container) return;
    S.container.innerHTML =
      pageHeadHtml() +
      '<div class="card"><div class="empty">' +
      icon("alert", 32) +
      '<div class="empty-title">关系与群聊加载失败</div>' +
      '<div class="empty-text">' +
      esc((err && (err.message || err)) || "未知错误") +
      "</div>" +
      '<div class="row mt-12"><button class="btn btn-outline" type="button" data-action="reload">重试</button></div>' +
      "</div></div>";
  }

  async function reload(showToast) {
    if (!S.app || S.busy) return;
    S.busy = true;
    try {
      await loadData(S.app);
      paint();
      if (showToast) S.app.toast("已刷新关系与群聊");
    } catch (err) {
      showLoadError(err);
      if (S.app) S.app.toast("刷新失败：" + ((err && (err.message || err)) || "未知错误"), true);
    } finally {
      S.busy = false;
    }
  }

  /* ------------------------------------------------------------------ *
   * 弹窗公共片段
   * ------------------------------------------------------------------ */
  function userIdentityHtml(user, tier) {
    // v2.8.4: global_uid is an internal archive id, not a platform account.
    var meta = tierMeta(tier);
    var name = nameOf(user);
    var nickname = nicknameOf(user);
    var tags = platformTagsHtml(user.platform_ids, 8);
    return (
      '<div class="rel-identity" style="--tier-color:' +
      meta.color +
      '">' +
      '<span class="avatar rel-identity-avatar">' +
      avatarInner(user.avatar, name) +
      "</span>" +
      '<div class="rel-identity-main">' +
      '<div class="rel-identity-name">' +
      esc(name) +
      '<span class="tier-pill tier-' +
      tier +
      '">' +
      esc(meta.label) +
      "</span></div>" +
      '<div class="rel-identity-meta">' +
      '<span class="mono">' +
      esc(user.global_uid || "") +
      "</span>" +
      "<span>互动 " +
      esc(num(user.interaction_count, 0)) +
      " 次</span>" +
      (nickname && nickname !== name
        ? "<span>专属称呼：" + esc(nickname) + "</span>"
        : "") +
      "</div>" +
      '<div class="rel-identity-tags">' +
      (tags || '<span class="text-xs muted">暂无平台账号</span>') +
      "</div></div></div>"
    );
  }

  function tierOptionHtml(value, current) {
    var meta = tierMeta(value);
    var selected = Number(current) === Number(value);
    var label = value === 3 ? "核心用户 · 管理员级" : value === 2 ? "重要熟人" : "普通群友";
    return (
      '<label class="rel-tier-option' +
      (selected ? " is-selected" : "") +
      '" data-tier-option="' +
      esc(value) +
      '">' +
      '<input type="radio" name="rel-tier" value="' +
      esc(value) +
      '"' +
      (selected ? " checked" : "") +
      " />" +
      '<span class="tier-pill tier-' +
      value +
      '">' +
      esc(meta.short) +
      "</span>" +
      '<span class="rel-tier-option-label">' +
      esc(label) +
      "</span></label>"
    );
  }

  function platformRowHtml(value) {
    var text = String(value == null ? "" : value).trim();
    var isLegacy = !!text && text.indexOf(":") < 0;
    return (
      '<div class="rel-platform-row' +
      (isLegacy ? " is-legacy" : "") +
      '" data-role="platform-row">' +
      '<input class="input input-sm mono" type="text" list="rel-platform-options" data-role="platform-input" ' +
      'placeholder="platform:uid，例如 qq bot:123456789" value="' +
      esc(text) +
      '" />' +
      (isLegacy
        ? '<span class="rel-platform-legacy-tag" data-tip="旧版数据只有裸 ID：请在左侧补全为 platform:uid（如 qq bot:123456789），或直接移除该行。">' +
          icon("alert", 12) +
          " 旧格式</span>"
        : "") +
      '<button class="icon-btn" type="button" data-action="remove-platform" data-tip="移除该平台账号" aria-label="移除该平台账号">' +
      icon("trash", 13) +
      "</button></div>"
    );
  }

  function collectPlatformIds(bodyEl) {
    var ids = [];
    bodyEl.querySelectorAll('[data-role="platform-input"]').forEach(function (input) {
      String(input.value || "")
        .split(/[\n,，;；]+/)
        .forEach(function (part) {
          var value = part.trim();
          if (value && ids.indexOf(value) < 0) ids.push(value);
        });
    });
    return ids;
  }

  /** 关系等级三选一的联动：★L3 → 好感度输入置灰并提示「管理员级无需好感度」。 */
  function syncTierUi(bodyEl, tier) {
    var isL3 = Number(tier) === 3;
    bodyEl.querySelectorAll("[data-tier-option]").forEach(function (option) {
      option.classList.toggle(
        "is-selected",
        Number(option.getAttribute("data-tier-option")) === Number(tier)
      );
    });
    var warn = bodyEl.querySelector('[data-role="tier-warn"]');
    if (warn) warn.hidden = !isL3;
    var field = bodyEl.querySelector('[data-role="affinity-field"]');
    if (field) field.classList.toggle("is-disabled", isL3);
    var input = bodyEl.querySelector('[data-role="affinity"]');
    if (input) {
      input.disabled = isL3;
      input.placeholder = isL3 ? "管理员级无需好感度" : "0 ~ 100";
    }
    var help = bodyEl.querySelector('[data-role="affinity-help"]');
    if (help) {
      help.textContent = isL3
        ? "管理员级无需好感度：★L3 不参与好感度计算与自动晋升。"
        : "L1 / L2 可手动调整好感度（0 ~ 100）；留空按 0 处理。";
    }
  }

  function bindUserModalEvents(app, bodyEl, user) {
    syncTierUi(bodyEl, Number(user.tier) || 1);
    bodyEl.addEventListener("change", function (event) {
      var target = event.target;
      if (!target || target.name !== "rel-tier") return;
      syncTierUi(bodyEl, Number(target.value));
    });
    bodyEl.addEventListener("input", function (event) {
      var target = event.target;
      if (!target || !target.getAttribute) return;
      if (target.getAttribute("data-role") !== "attitude") return;
      var counter = bodyEl.querySelector('[data-role="attitude-count"]');
      if (counter) counter.textContent = String(String(target.value || "").length);
    });
    bodyEl.addEventListener("click", function (event) {
      var target = event.target;
      if (!target || !target.closest) return;
      var btn = target.closest("[data-action]");
      if (!btn) return;
      var action = btn.getAttribute("data-action");
      if (action === "add-platform") {
        var rows = bodyEl.querySelector('[data-role="platform-rows"]');
        if (!rows) return;
        rows.insertAdjacentHTML("beforeend", platformRowHtml(""));
        var inputs = rows.querySelectorAll('[data-role="platform-input"]');
        var last = inputs[inputs.length - 1];
        if (last) last.focus();
      } else if (action === "remove-platform") {
        var row = btn.closest(".rel-platform-row");
        if (row && row.parentNode) row.parentNode.removeChild(row);
      } else if (action === "merge-account") {
        app.closeModal();
        openMergeModal(user.global_uid);
      }
    });
  }

  /* ------------------------------------------------------------------ *
   * 用户详情弹窗
   * ------------------------------------------------------------------ */
  function openUserModal(uid) {
    var app = S.app;
    var user = findUser(uid);
    if (!app || !user) return;
    var tier = Number(user.tier) || 1;
    if (!TIER_META[tier]) tier = 1;
    var nickname = nicknameOf(user);
    var attitude = String(user.attitude == null ? "" : user.attitude);
    var platformIds = safeList(user.platform_ids);
    var rows = platformIds.length
      ? platformIds.map(platformRowHtml).join("")
      : platformRowHtml("");
    var affinityValue = String(num(user.affinity, 0));
    var body =
      userIdentityHtml(user, tier) +
      '<div class="rel-form-grid">' +
      '<label class="field"><span class="field-label">显示名称 <span class="req">*</span></span>' +
      '<input class="input" type="text" data-role="display-name" maxlength="64" value="' +
      esc(user.display_name || nameOf(user)) +
      '" /></label>' +
      '<label class="field"><span class="field-label">角色对 Ta 的专属称呼 <span class="mono muted">nickname</span></span>' +
      '<input class="input" type="text" data-role="nickname" maxlength="64" value="' +
      esc(nickname) +
      '" /></label>' +
      '<div class="field is-full"><span class="field-label">关系等级</span>' +
      '<div class="rel-tier-options" data-role="tier-options">' +
      TIER_ORDER.map(function (value) {
        return tierOptionHtml(value, tier);
      }).join("") +
      "</div>" +
      '<div class="rel-tier-warn" data-role="tier-warn"' +
      (tier === 3 ? "" : " hidden") +
      ">" +
      icon("alert", 13) +
      "<span>" +
      esc(L3_WARNING) +
      "</span></div></div>" +
      '<div class="field is-full' +
      (tier === 3 ? " is-disabled" : "") +
      '" data-role="affinity-field"><span class="field-label">好感度</span>' +
      '<input class="input" type="number" step="0.5" min="0" max="100" data-role="affinity" placeholder="' +
      (tier === 3 ? "管理员级无需好感度" : "0 ~ 100") +
      '" value="' +
      esc(affinityValue) +
      '"' +
      (tier === 3 ? " disabled" : "") +
      " />" +
      '<div class="field-help" data-role="affinity-help">' +
      (tier === 3
        ? "管理员级无需好感度：★L3 不参与好感度计算与自动晋升。"
        : "L1 / L2 可手动调整好感度（0 ~ 100）；留空按 0 处理。") +
      "</div></div>" +
      '<div class="field is-full"><span class="field-label">专属态度 <span class="mono muted">attitude</span></span>' +
      '<textarea class="input textarea" rows="3" maxlength="' +
      ATTITUDE_MAX +
      '" data-role="attitude" placeholder="例如：把 Ta 当成最信任的损友，说话可以毒舌一点但关键时刻很护短">' +
      esc(attitude) +
      "</textarea>" +
      '<div class="field-help">最多 ' +
      ATTITUDE_MAX +
      ' 字，会随关系上下文注入 System Prompt，让角色按这里的设定区别对待 Ta；留空则按人设自然相处。（<span class="rel-attitude-count" data-role="attitude-count">' +
      esc(attitude.length) +
      "</span>/" +
      ATTITUDE_MAX +
      " 字）</div></div>" +
      '<div class="field is-full"><span class="field-label">平台账号</span>' +
      '<div class="rel-platform-editor">' +
      '<div class="rel-platform-rows" data-role="platform-rows">' +
      rows +
      "</div>" +
      '<div class="rel-platform-actions">' +
      '<button class="btn btn-outline btn-sm" type="button" data-action="add-platform">' +
      icon("plus", 13) +
      "添加账号</button>" +
      '<button class="btn btn-ghost btn-sm" type="button" data-action="merge-account">' +
      icon("link", 13) +
      "合并其他用户账号到此档案</button>" +
      "</div>" +
      '<datalist id="rel-platform-options">' +
      knownPlatformIds(app)
        .map(function (pid) {
          return '<option value="' + esc(pid + ":") + '"></option>';
        })
        .join("") +
      "</datalist>" +
      '<div class="rel-platform-hint">格式 <code>platform:uid</code>（例如 <code>qq bot:123456789</code>；可从下拉选择接口后补 UID）；一行一个，同一平台多个账号用逗号分隔。<b>旧格式</b>（只有裸 ID、无冒号）的行请补全平台后再保存，或直接删除该行。</div>' +
      "</div></div>" +
      "</div>";
    app.modal({
      title: "用户详情 · " + nameOf(user),
      wide: true,
      body: body,
      actions: [
        {
          label: "删除用户",
          danger: true,
          icon: "trash",
          onClick: function () {
            /* 打开二次确认会替换当前弹窗，返回 false 避免再次关闭确认框 */
            confirmDeleteUser(user.global_uid);
            return false;
          }
        },
        { label: "取消" },
        {
          label: "保存修改",
          primary: true,
          icon: "save",
          onClick: function (modalApp, bodyEl) {
            return saveUserChanges(user.global_uid, modalApp, bodyEl);
          }
        }
      ],
      onMount: function (bodyEl) {
        bindUserModalEvents(app, bodyEl, user);
      }
    });
  }

  async function saveUserChanges(uid, modalApp, bodyEl) {
    var app = S.app || modalApp;
    var user = findUser(uid) || { global_uid: uid };
    var displayEl = bodyEl.querySelector('[data-role="display-name"]');
    var nicknameEl = bodyEl.querySelector('[data-role="nickname"]');
    var attitudeEl = bodyEl.querySelector('[data-role="attitude"]');
    var tierEl = bodyEl.querySelector('input[name="rel-tier"]:checked');
    var affinityEl = bodyEl.querySelector('[data-role="affinity"]');
    var displayName = displayEl ? String(displayEl.value || "").trim() : "";
    if (!displayName) {
      app.toast("显示名称不能为空", true);
      return false;
    }
    var tier = tierEl ? Number(tierEl.value) : Number(user.tier) || 1;
    if (TIER_ORDER.indexOf(tier) < 0) tier = 1;
    var platformIds = collectPlatformIds(bodyEl);
    for (var i = 0; i < platformIds.length; i++) {
      if (platformIds[i].indexOf(":") < 0) {
        app.toast(
          "平台账号格式应为 platform:uid（例如 qq:123456789）：" + platformIds[i],
          true
        );
        return false;
      }
    }
    var payload = {
      global_uid: uid,
      display_name: displayName,
      nickname: nicknameEl ? String(nicknameEl.value || "").trim() : "",
      tier: tier,
      attitude: attitudeEl
        ? String(attitudeEl.value || "").trim().slice(0, ATTITUDE_MAX)
        : "",
      platform_ids: platformIds
    };
    /* ★L3 管理员级：不提交好感度，避免把无意义数值写回档案 */
    if (tier !== 3) {
      var affinity = affinityEl ? Number(affinityEl.value) : num(user.affinity, 0);
      if (!isFinite(affinity)) affinity = 0;
      payload.affinity = Math.max(0, Math.min(100, affinity));
    }
    var data = await app.apiPost("users/update", payload);
    applyUsersPayload(data);
    paint();
    app.toast("已保存用户「" + displayName + "」");
    return true;
  }

  function confirmDeleteUser(uid) {
    var app = S.app;
    var user = findUser(uid);
    if (!app) return;
    var name = user ? nameOf(user) : String(uid);
    app
      .confirm({
        title: "删除用户",
        message:
          "确定删除用户「" +
          name +
          "」(" +
          uid +
          ") 吗？该用户的关系等级、好感度与专属态度会被清除；记忆库与群聊档案不会被删除。",
        danger: true,
        confirmLabel: "删除",
        onConfirm: async function () {
          var data = await app.apiPost("users/update", { action: "delete", global_uid: uid });
          applyUsersPayload(data);
          paint();
          app.toast("已删除用户：" + name);
        }
      })
      .then(function () {
        /* 结果已通过 toast 提示，无需额外处理 */
      });
  }

  /* ------------------------------------------------------------------ *
   * 合并其他账号
   * ------------------------------------------------------------------ */
  function openMergeModal(primaryUid) {
    var app = S.app;
    var user = findUser(primaryUid);
    if (!app || !user) return;
    var primaryName = nameOf(user);
    var candidates = S.users
      .filter(function (item) {
        return item && String(item.global_uid) !== String(primaryUid);
      })
      .slice(0, 80);
    var datalist = candidates.length
      ? '<datalist id="rel-merge-candidates">' +
        candidates
          .map(function (item) {
            return (
              '<option value="' +
              esc(item.global_uid) +
              '">' +
              esc(nameOf(item)) +
              "</option>"
            );
          })
          .join("") +
        "</datalist>"
      : "";
    var body =
      '<div class="rel-merge-primary">' +
      '<span class="avatar rel-avatar" style="--tier-color:var(--tier-l3)">' +
      avatarInner(user.avatar, primaryName) +
      "</span>" +
      '<div class="rel-row-main"><strong>' +
      esc(primaryName) +
      '</strong><span class="text-xs muted mono">' +
      esc(user.global_uid || "") +
      " · 主档案</span></div></div>" +
      '<div class="field"><span class="field-label">被合并账号 global_uid <span class="req">*</span></span>' +
      '<input class="input mono" type="text" data-role="merge-secondary" list="rel-merge-candidates" placeholder="输入另一个平台账号的 global_uid" />' +
      datalist +
      '<div class="field-help">被合并账号的平台 ID、互动次数会并入当前用户档案，之后该账号从列表移除；主档案的专属态度优先。</div></div>';
    app.modal({
      title: "合并其他用户账号",
      body: body,
      actions: [
        { label: "取消" },
        {
          label: "确认合并",
          primary: true,
          icon: "link",
          onClick: async function (modalApp, bodyEl) {
            var input = bodyEl.querySelector('[data-role="merge-secondary"]');
            var secondary = input ? String(input.value || "").trim() : "";
            if (!secondary) {
              app.toast("请填写被合并账号的 global_uid", true);
              return false;
            }
            if (secondary === String(primaryUid)) {
              app.toast("不能把账号合并到它自己", true);
              return false;
            }
            var data = await app.apiPost("users/update", {
              action: "merge",
              primary_uid: primaryUid,
              secondary_uid: secondary
            });
            applyUsersPayload(data);
            paint();
            app.toast("已将 " + secondary + " 合并到 " + primaryName);
            return true;
          }
        }
      ]
    });
  }

  /* ------------------------------------------------------------------ *
   * 群聊详情弹窗
   * ------------------------------------------------------------------ */
  function openGroupModal(umo) {
    var app = S.app;
    var group = findGroup(umo);
    if (!app || !group) return;
    var unread = num(group.unread_count, 0);
    var windowCount = num(group.window_count, 0);
    var bound = group.bound_soul_name || group.bound_soul_id || "-";
    var body =
      '<div class="rel-identity rel-identity-group">' +
      '<span class="avatar rel-identity-avatar rel-group-avatar">' +
      icon("sessions", 20) +
      "</span>" +
      '<div class="rel-identity-main">' +
      '<div class="rel-identity-name">' +
      esc(group.display_name) +
      '<span class="tag tag-primary rel-group-tag">💬 群聊</span></div>' +
      '<div class="rel-identity-meta"><span>最近活跃 ' +
      esc(fmtAgo(group.last_active_at)) +
      "</span><span>绑定角色：" +
      esc(bound) +
      "</span></div>" +
      '<div class="rel-identity-tags">' +
      '<span class="tag">滑窗 ' +
      esc(windowCount) +
      " 条</span>" +
      '<span class="tag' +
      (unread > 0 ? " tag-danger" : " tag-muted") +
      '">未读 ' +
      esc(unread) +
      "</span>" +
      (group.platform_id
        ? '<span class="tag rel-platform-tag">' + esc(group.platform_id) + "</span>"
        : "") +
      "</div></div></div>" +
      '<div class="rel-info-grid">' +
      '<div class="rel-info-item"><span class="rel-info-label">group_id</span><span class="rel-info-value mono">' +
      esc(group.group_id || "-") +
      "</span></div>" +
      '<div class="rel-info-item"><span class="rel-info-label">platform_id</span><span class="rel-info-value mono">' +
      esc(group.platform_id || "-") +
      "</span></div>" +
      '<div class="rel-info-item"><span class="rel-info-label">管辖角色 soul_id</span><span class="rel-info-value mono">' +
      esc(group.bound_soul_id || "-") +
      "</span></div>" +
      '<div class="rel-info-item"><span class="rel-info-label">内存滑窗 / 未读</span><span class="rel-info-value">' +
      esc(windowCount) +
      " 条 / " +
      esc(unread) +
      " 条</span></div>" +
      '<div class="rel-info-item is-full"><span class="rel-info-label">完整 UMO</span><span class="rel-info-value mono">' +
      esc(group.umo || "-") +
      "</span></div></div>" +
      '<div class="rel-form-grid">' +
      '<label class="field"><span class="field-label">群显示名称</span>' +
      '<input class="input" type="text" data-role="group-name" maxlength="64" value="' +
      esc(group.display_name) +
      '" /></label>' +
      '<label class="field"><span class="field-label">群备注 <span class="mono muted">notes</span></span>' +
      '<input class="input" type="text" data-role="group-notes" maxlength="200" value="' +
      esc(group.notes) +
      '" placeholder="例如：酒狐的主场群，晚上最热闹" /></label>' +
      "</div>" +
      '<div class="rel-group-danger-hint">清空该群内存：只清该群的内存滑窗 + 未读队列；保留 SQLite memories / users / surfing_history / 群档案（重启后未读队列会重置）。移除群聊记录：会删除该群档案（下次收到群消息会重新记录），长期记忆与用户档案仍保留。</div>';
    app.modal({
      title: "群聊详情 · " + group.display_name,
      wide: true,
      body: body,
      actions: [
        {
          label: "移除群聊记录",
          danger: true,
          icon: "trash",
          onClick: function () {
            confirmDeleteGroup(group.umo);
            return false;
          }
        },
        {
          label: "清空该群内存滑窗与未读",
          icon: "refresh",
          onClick: function () {
            confirmClearGroupMemory(group.umo);
            return false;
          }
        },
        { label: "取消" },
        {
          label: "保存修改",
          primary: true,
          icon: "save",
          onClick: function (modalApp, bodyEl) {
            return saveGroupChanges(group.umo, modalApp, bodyEl);
          }
        }
      ]
    });
  }

  async function saveGroupChanges(umo, modalApp, bodyEl) {
    var app = S.app || modalApp;
    var group = findGroup(umo);
    var nameEl = bodyEl.querySelector('[data-role="group-name"]');
    var notesEl = bodyEl.querySelector('[data-role="group-notes"]');
    var displayName = nameEl ? String(nameEl.value || "").trim() : "";
    if (!displayName) {
      app.toast("群显示名称不能为空", true);
      return false;
    }
    var data = await app.apiPost("users/update", {
      action: "update_group",
      umo: umo,
      display_name: displayName,
      notes: notesEl ? String(notesEl.value || "").trim() : ""
    });
    applyGroupsPayload(data);
    if (!data || !Array.isArray(data.groups)) {
      if (group) {
        group.display_name = displayName;
        group.notes = notesEl ? String(notesEl.value || "").trim() : "";
      }
    }
    paint();
    app.toast("已更新群聊「" + displayName + "」");
    return true;
  }

  function confirmClearGroupMemory(umo) {
    var app = S.app;
    var group = findGroup(umo);
    if (!app) return;
    var name = group ? group.display_name : umo;
    app
      .confirm({
        title: "清空该群内存滑窗与未读",
        message:
          "确定清空群聊「" +
          name +
          "」吗？清空：该群的内存滑窗 + 未读队列。" +
          "保留：SQLite memories / users / surfing_history / 群档案；重启后未读队列本身会重置。",
        danger: true,
        confirmLabel: "清空内存",
        onConfirm: async function () {
          var data = await app.apiPost("users/update", {
            action: "clear_group_memory",
            umo: umo
          });
          applyGroupsPayload(data);
          paint();
          app.toast("已清空群聊「" + name + "」的内存滑窗与未读");
        }
      })
      .then(function () {
        /* 结果已通过 toast 提示 */
      });
  }

  function confirmDeleteGroup(umo) {
    var app = S.app;
    var group = findGroup(umo);
    if (!app) return;
    var name = group ? group.display_name : umo;
    app
      .confirm({
        title: "移除群聊记录",
        message:
          "确定移除群聊「" +
          name +
          "」的档案吗？会删除该群档案并同时清空该群的内存滑窗 + 未读；" +
          "保留 SQLite memories / users / surfing_history（长期记忆与用户档案不受影响）；下次收到群消息会重新记录，重启后未读队列本身会重置。",
        danger: true,
        confirmLabel: "移除记录",
        onConfirm: async function () {
          var data = await app.apiPost("users/update", {
            action: "delete_group",
            umo: umo
          });
          applyGroupsPayload(data);
          paint();
          app.toast("已移除群聊记录：" + name);
        }
      })
      .then(function () {
        /* 结果已通过 toast 提示 */
      });
  }

  /* ------------------------------------------------------------------ *
   * 事件（全部走委托，无内联事件处理器）
   * ------------------------------------------------------------------ */
  function handleClick(event) {
    var target = event.target;
    if (!target || !target.closest) return;
    var actionEl = target.closest("[data-action]");
    if (actionEl) {
      var action = actionEl.getAttribute("data-action");
      if (action === "reload") {
        reload(true);
      } else if (action === "filter") {
        setFilter(actionEl.getAttribute("data-filter") || "");
      } else if (action === "clear-search") {
        S.keyword = "";
        var input = S.container
          ? S.container.querySelector('[data-role="relations-search"]')
          : null;
        if (input) input.value = "";
        paintList();
      } else if (action === "detail-user") {
        openUserModal(actionEl.getAttribute("data-uid"));
      } else if (action === "detail-group") {
        openGroupModal(actionEl.getAttribute("data-umo"));
      }
      return;
    }
    var groupRow = target.closest(".rel-row.is-group");
    if (groupRow) {
      openGroupModal(groupRow.getAttribute("data-umo"));
      return;
    }
    var row = target.closest(".rel-row");
    if (row) openUserModal(row.getAttribute("data-uid"));
  }

  function handleInput(event) {
    var target = event.target;
    if (!target || !target.getAttribute) return;
    if (target.getAttribute("data-role") !== "relations-search") return;
    S.keyword = String(target.value || "").trim();
    paintList();
  }

  function handleKeydown(event) {
    var target = event.target;
    if (!target || !target.closest) return;
    if (target.closest("button, a, input, select, textarea")) return;
    var row = target.closest(".rel-row");
    if (!row) return;
    if (event.key !== "Enter" && event.key !== " ") return;
    event.preventDefault();
    if (row.classList.contains("is-group")) openGroupModal(row.getAttribute("data-umo"));
    else openUserModal(row.getAttribute("data-uid"));
  }

  function bindEvents(container, app) {
    container.addEventListener("click", handleClick);
    container.addEventListener("input", handleInput);
    container.addEventListener("keydown", handleKeydown);
  }

  /* ------------------------------------------------------------------ *
   * 生命周期
   * ------------------------------------------------------------------ */
  async function render(container, app) {
    S.app = app;
    S.container = container;
    S.destroyed = false;
    S.users = [];
    S.groups = [];
    S.counts = { l3_count: 0, l2_count: 0, l1_count: 0, total: 0 };
    S.filter = "";
    S.keyword = "";
    S.busy = false;

    container.innerHTML =
      '<div class="loading"><span class="spinner"></span><span>加载关系与群聊…</span></div>';
    bindEvents(container, app);
    try {
      await loadData(app);
      if (S.destroyed) return;
      paint();
    } catch (err) {
      console.error("[OnlyYou] 关系页渲染失败：", err);
      if (!S.destroyed) showLoadError(err);
    }
  }

  function destroy() {
    S.destroyed = true;
    S.app = null;
    S.container = null;
    S.users = [];
    S.groups = [];
    S.counts = { l3_count: 0, l2_count: 0, l1_count: 0, total: 0 };
    S.filter = "";
    S.keyword = "";
    S.busy = false;
  }

  window.OY_PAGES = window.OY_PAGES || {};
  window.OY_PAGES.relations = {
    id: "relations",
    title: "关系",
    icon: "relations",
    render: render,
    destroy: destroy
  };
})();
