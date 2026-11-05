/**
 * OnlyYou Plugin Pages — 关系页 (`views/console/page-relations.js`)
 *
 * 数据源：
 *   GET  `users/list`   → { users: [...], counts: {l3_count,l2_count,l1_count,total} }
 *   POST `users/update` → 更新等级 / 昵称 / 好感度 / 平台账号；action=merge 合并账号；action=delete 删除
 *
 * 页面结构：三列看板（★L3 / ◆L2 / L1），用户卡显示首字母头像、名字、昵称、好感度进度条与
 * 互动次数；HTML5 拖拽卡片到其他列即改等级；点击卡片打开抽屉编辑；「⋯」菜单里合并账号 / 删除。
 *
 * 注册：window.OY_PAGES.relations = { id, title, icon, render(container, ctx), destroy() }
 */
(function () {
  "use strict";

  var TIER_META = {
    3: { key: "l3", label: "★L3 核心用户", short: "L3", color: "var(--tier-l3)", icon: "spark" },
    2: { key: "l2", label: "◆L2 重要熟人", short: "L2", color: "var(--tier-l2)", icon: "heart" },
    1: { key: "l1", label: "L1 普通群友", short: "L1", color: "var(--tier-l1)", icon: "user" }
  };
  var TIER_ORDER = [3, 2, 1];

  var S = {
    app: null,
    container: null,
    destroyed: false,
    users: [],
    counts: { l3_count: 0, l2_count: 0, l1_count: 0, total: 0 },
    dragUid: "",
    suppressClick: false,
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

  function tierMeta(tier) {
    return TIER_META[Number(tier)] || TIER_META[1];
  }

  function initialOf(user) {
    var name = String((user && (user.display_name || user.nickname || user.global_uid)) || "?");
    return name.slice(0, 1).toUpperCase();
  }

  function findUser(uid) {
    for (var i = 0; i < S.users.length; i++) {
      if (S.users[i] && S.users[i].global_uid === uid) return S.users[i];
    }
    return null;
  }

  function computeCounts(users) {
    var counts = { l3_count: 0, l2_count: 0, l1_count: 0, total: users.length };
    users.forEach(function (user) {
      var tier = Number(user && user.tier);
      if (tier === 3) counts.l3_count += 1;
      else if (tier === 2) counts.l2_count += 1;
      else counts.l1_count += 1;
    });
    return counts;
  }

  function usersOfTier(tier) {
    return S.users.filter(function (user) {
      return Number(user && user.tier) === tier;
    });
  }

  function affinityPercent(value) {
    var n = num(value, 0);
    if (n <= 0) return 0;
    return Math.max(2, Math.min(100, n));
  }

  /* ------------------------------------------------------------------ *
   * HTML
   * ------------------------------------------------------------------ */
  function countsChipsHtml() {
    var counts = S.counts || {};
    return (
      '<div class="row wrap" style="gap:6px">' +
      '<span class="tier-pill tier-3">★L3 ' +
      esc(num(counts.l3_count, 0)) +
      "</span>" +
      '<span class="tier-pill tier-2">◆L2 ' +
      esc(num(counts.l2_count, 0)) +
      "</span>" +
      '<span class="tier-pill tier-1">L1 ' +
      esc(num(counts.l1_count, 0)) +
      "</span>" +
      '<span class="tag tag-muted">总计 ' +
      esc(num(counts.total, S.users.length)) +
      "</span>" +
      "</div>"
    );
  }

  function userCardHtml(user) {
    var tier = Number(user.tier) || 1;
    var meta = tierMeta(tier);
    var name = String(user.display_name || user.nickname || user.global_uid || "未命名");
    var nickname = String(user.nickname || "");
    var uid = String(user.global_uid || "");
    var platformCount = Array.isArray(user.platform_ids) ? user.platform_ids.length : 0;
    var pct = affinityPercent(user.affinity);
    var sub = nickname && nickname !== name ? nickname : uid;
    return (
      '<div class="user-card" draggable="true" data-uid="' +
      esc(uid) +
      '" data-tier="' +
      tier +
      '" style="--tier-color:' +
      meta.color +
      '" data-tip="点击编辑资料；拖拽到其他列可改变关系等级" tabindex="0">' +
      '<div class="user-card-top">' +
      '<span class="avatar avatar-sm">' +
      esc(initialOf(user)) +
      "</span>" +
      '<div class="user-card-name"><strong>' +
      esc(name) +
      "</strong><span>" +
      esc(sub) +
      "</span></div>" +
      '<button class="icon-btn" type="button" data-action="user-menu" data-uid="' +
      esc(uid) +
      '" style="width:24px;height:24px" data-tip="更多操作" aria-label="更多操作">' +
      icon("dots", 14) +
      "</button>" +
      "</div>" +
      '<div class="affinity">' +
      '<span class="mini-bar"><i style="width:' +
      pct.toFixed(1) +
      '%"></i></span>' +
      "<span>好感度 " +
      esc(num(user.affinity, 0).toFixed(1)) +
      "</span></div>" +
      '<div class="memory-meta">' +
      "<span>互动 " +
      esc(num(user.interaction_count, 0)) +
      " 次</span>" +
      "<span>" +
      esc(user.tier_label || meta.label) +
      "</span>" +
      (platformCount ? "<span>" + platformCount + " 个平台账号</span>" : "") +
      "</div></div>"
    );
  }

  function columnHtml(tier) {
    var meta = tierMeta(tier);
    var users = usersOfTier(tier);
    var cards = users.length
      ? '<div class="kanban-list">' + users.map(userCardHtml).join("") + "</div>"
      : '<div class="kanban-list"><div class="empty" style="padding:26px 8px">' +
        icon(meta.icon, 26) +
        '<div class="empty-text">这一列还没有用户</div>' +
        "</div></div>";
    return (
      '<div class="kanban-col" data-tier="' +
      tier +
      '"><div class="kanban-head"><div class="title">' +
      icon(meta.icon, 14) +
      "<span>" +
      esc(meta.label) +
      '</span><span class="badge-count">' +
      users.length +
      "</span></div></div>" +
      cards +
      "</div>"
    );
  }

  function pageHtml() {
    return (
      '<div class="page-head"><h1 class="page-title">' +
      icon("relations", 18) +
      "关系" +
      '<span class="card-sub">' +
      esc(S.users.length) +
      " 位用户</span></h1>" +
      '<div class="page-actions">' +
      countsChipsHtml() +
      '<button class="btn btn-ghost btn-sm" type="button" data-action="reload">' +
      icon("refresh", 14) +
      "刷新</button>" +
      "</div></div>" +
      '<div class="card"><div class="card-body tight"><div class="row-between wrap">' +
      '<span class="text-sm text-2">拖拽卡片换列改等级，点击卡片编辑资料</span>' +
      '<span class="hint" data-tip="关系等级：L1 普通群友 / ◆L2 重要熟人 / ★L3 核心用户；「⋯」菜单里可合并账号或删除用户。">等级规则</span>' +
      "</div></div></div>" +
      '<div class="kanban">' +
      TIER_ORDER.map(columnHtml).join("") +
      "</div>"
    );
  }

  function emptyHtml(message) {
    return (
      '<div class="card"><div class="empty">' +
      icon("relations", 32) +
      '<div class="empty-title">' +
      esc(message || "还没有用户档案") +
      "</div>" +
      '<div class="empty-text">在群聊 / 私聊里和 Bot 互动后，用户会自动出现在这里；也可以在聊天中手动提升关系等级。</div>' +
      '<div class="row mt-12"><button class="btn btn-outline" type="button" data-action="reload">重试</button></div>' +
      "</div></div>"
    );
  }

  /* ------------------------------------------------------------------ *
   * 数据加载 / 刷新
   * ------------------------------------------------------------------ */
  async function loadData(app) {
    var data = await app.apiGet("users/list");
    if (!data || typeof data !== "object") throw new Error("GET users/list 返回为空");
    S.users = Array.isArray(data.users) ? data.users : [];
    S.counts = data.counts && typeof data.counts === "object" ? data.counts : computeCounts(S.users);
  }

  function paint() {
    if (!S.container || S.destroyed) return;
    var scroller = S.container.closest ? S.container.closest(".main") : null;
    var top = scroller ? scroller.scrollTop : 0;
    S.container.innerHTML = pageHtml();
    if (scroller) scroller.scrollTop = top;
  }

  function applyUsers(users) {
    if (!Array.isArray(users)) return;
    S.users = users;
    S.counts = computeCounts(users);
  }

  async function reload(showToast) {
    if (!S.app || S.busy) return;
    S.busy = true;
    try {
      await loadData(S.app);
      paint();
      if (showToast) S.app.toast("已刷新用户关系");
    } catch (err) {
      if (S.container) {
        S.container.innerHTML =
          '<div class="page-head"><h1 class="page-title">' +
          icon("relations", 18) +
          "关系</h1></div>" +
          '<div class="card"><div class="empty">' +
          icon("alert", 32) +
          '<div class="empty-title">用户关系加载失败</div>' +
          '<div class="empty-text">' +
          esc((err && (err.message || err)) || "未知错误") +
          "</div>" +
          '<div class="row mt-12"><button class="btn btn-outline" type="button" data-action="reload">重试</button></div>' +
          "</div></div>";
      }
    } finally {
      S.busy = false;
    }
  }

  /* ------------------------------------------------------------------ *
   * 抽屉编辑
   * ------------------------------------------------------------------ */
  function openEditDrawer(uid) {
    var app = S.app;
    var user = findUser(uid);
    if (!app || !user) return;
    var tier = Number(user.tier) || 1;
    var platformIds = Array.isArray(user.platform_ids) ? user.platform_ids.join("\n") : "";
    var body =
      '<div class="field"><span class="field-label">global_uid</span>' +
      '<input class="input mono" type="text" value="' +
      esc(user.global_uid) +
      '" disabled /></div>' +
      '<div class="field-grid">' +
      '<div class="field"><span class="field-label">显示名</span>' +
      '<input class="input" type="text" data-role="display-name" value="' +
      esc(user.display_name || "") +
      '" /></div>' +
      '<div class="field"><span class="field-label">昵称</span>' +
      '<input class="input" type="text" data-role="nickname" value="' +
      esc(user.nickname || "") +
      '" /></div>' +
      '<div class="field"><span class="field-label">关系等级</span>' +
      '<select class="select" data-role="tier">' +
      TIER_ORDER.map(function (value) {
        return (
          '<option value="' +
          value +
          '"' +
          (value === tier ? " selected" : "") +
          ">" +
          esc(tierMeta(value).label) +
          "</option>"
        );
      }).join("") +
      "</select></div>" +
      '<div class="field"><span class="field-label">好感度</span>' +
      '<input class="input" type="number" step="0.5" min="0" data-role="affinity" value="' +
      esc(num(user.affinity, 0)) +
      '" /></div>' +
      "</div>" +
      '<div class="field"><span class="field-label">平台账号 ID（每行一个）</span>' +
      '<textarea class="textarea" rows="3" data-role="platform-ids" placeholder="qq:123456789">' +
      esc(platformIds) +
      "</textarea>" +
      '<div class="field-help">用于把多个平台的账号合并到同一个 global_uid 下；留空表示不修改已有列表。</div></div>';
    app.drawer({
      title: "编辑用户 · " + (user.display_name || user.global_uid),
      body: body,
      actions: [
        { label: "取消" },
        {
          label: "保存",
          primary: true,
          icon: "save",
          onClick: async function (drawerApp, bodyEl) {
            var displayEl = bodyEl.querySelector('[data-role="display-name"]');
            var nicknameEl = bodyEl.querySelector('[data-role="nickname"]');
            var tierEl = bodyEl.querySelector('[data-role="tier"]');
            var affinityEl = bodyEl.querySelector('[data-role="affinity"]');
            var platformEl = bodyEl.querySelector('[data-role="platform-ids"]');
            var displayName = displayEl ? String(displayEl.value || "").trim() : "";
            if (!displayName) {
              drawerApp.toast("显示名不能为空", true);
              return false;
            }
            var platformIds = [];
            var platformText = platformEl ? String(platformEl.value || "").trim() : "";
            if (platformText) {
              platformIds = platformText
                .split(/[\n,，]+/)
                .map(function (item) {
                  return item.trim();
                })
                .filter(Boolean);
            }
            var payload = {
              global_uid: user.global_uid,
              display_name: displayName,
              nickname: nicknameEl ? String(nicknameEl.value || "").trim() : "",
              tier: tierEl ? Number(tierEl.value) : tier,
              affinity: affinityEl ? Number(affinityEl.value) : num(user.affinity, 0)
            };
            /* 留空表示不修改平台账号列表，避免把已有列表清空 */
            if (platformIds.length) payload.platform_ids = platformIds;
            var data = await drawerApp.apiPost("users/update", payload);
            if (data && Array.isArray(data.users)) applyUsers(data.users);
            paint();
            drawerApp.toast("已更新用户 " + displayName);
            return true;
          }
        }
      ]
    });
  }

  /* ------------------------------------------------------------------ *
   * ⋯ 菜单 / 合并 / 删除
   * ------------------------------------------------------------------ */
  function openUserMenu(uid) {
    var app = S.app;
    var user = findUser(uid);
    if (!app || !user) return;
    var body =
      '<div class="stat-desc mb-12">' +
      esc(user.display_name || user.global_uid) +
      ' <span class="mono muted">' +
      esc(user.global_uid) +
      "</span></div>" +
      '<div class="col">' +
      '<button class="btn btn-outline" type="button" data-menu-action="edit">' +
      icon("edit", 14) +
      "编辑资料</button>" +
      '<button class="btn btn-outline" type="button" data-menu-action="merge">' +
      icon("link", 14) +
      "合并账号</button>" +
      '<button class="btn btn-danger" type="button" data-menu-action="delete">' +
      icon("trash", 14) +
      "删除用户</button>" +
      "</div>";
    app.modal({
      title: "用户操作",
      body: body,
      onMount: function (bodyEl, modalApp) {
        bodyEl.querySelectorAll("[data-menu-action]").forEach(function (btn) {
          btn.addEventListener("click", function () {
            var action = btn.getAttribute("data-menu-action");
            modalApp.closeModal();
            if (action === "edit") openEditDrawer(uid);
            else if (action === "merge") openMergeModal(uid);
            else if (action === "delete") confirmDeleteUser(uid);
          });
        });
      }
    });
  }

  function openMergeModal(uid) {
    var app = S.app;
    var user = findUser(uid);
    if (!app || !user) return;
    var body =
      '<div class="field"><span class="field-label">被合并的账号 global_uid <span class="req">*</span></span>' +
      '<input class="input mono" type="text" data-role="secondary-uid" placeholder="另一个平台账号的 global_uid" />' +
      '<div class="field-help">被合并账号的平台 ID、互动次数会并入当前用户，之后该账号从列表移除。</div></div>' +
      '<div class="stat-desc">主账号：<strong>' +
      esc(user.display_name || user.global_uid) +
      '</strong> <span class="mono muted">' +
      esc(user.global_uid) +
      "</span></div>";
    app.modal({
      title: "合并账号",
      body: body,
      actions: [
        { label: "取消" },
        {
          label: "确认合并",
          primary: true,
          icon: "link",
          onClick: async function (modalApp, bodyEl) {
            var input = bodyEl.querySelector('[data-role="secondary-uid"]');
            var secondary = input ? String(input.value || "").trim() : "";
            if (!secondary) {
              modalApp.toast("请填写被合并账号的 global_uid", true);
              return false;
            }
            if (secondary === user.global_uid) {
              modalApp.toast("不能把账号合并到它自己", true);
              return false;
            }
            var data = await modalApp.apiPost("users/update", {
              action: "merge",
              primary_uid: user.global_uid,
              secondary_uid: secondary
            });
            if (data && Array.isArray(data.users)) applyUsers(data.users);
            paint();
            modalApp.toast("已将 " + secondary + " 合并到 " + user.global_uid);
            return true;
          }
        }
      ]
    });
  }

  function confirmDeleteUser(uid) {
    var app = S.app;
    var user = findUser(uid);
    if (!app || !user) return;
    app
      .confirm({
        title: "删除用户",
        message:
          "确定删除用户「" +
          (user.display_name || user.global_uid) +
          "」(" +
          user.global_uid +
          ") 吗？该用户的关系等级与好感度记录会被清除，记忆库中的条目不会被删除。",
        danger: true,
        confirmLabel: "删除",
        onConfirm: async function () {
          var data = await app.apiPost("users/update", {
            action: "delete",
            global_uid: user.global_uid
          });
          if (data && Array.isArray(data.users)) applyUsers(data.users);
          paint();
          app.toast("已删除用户 " + (user.display_name || user.global_uid));
        }
      })
      .then(function () {
        /* 结果已通过 toast 提示，无需额外处理 */
      });
  }

  /* ------------------------------------------------------------------ *
   * 拖拽改等级
   * ------------------------------------------------------------------ */
  async function changeTier(uid, newTier) {
    var app = S.app;
    var user = findUser(uid);
    if (!app || !user) return;
    if (Number(user.tier) === Number(newTier)) return;
    var oldTier = Number(user.tier) || 1;
    S.busy = true;
    try {
      var data = await app.apiPost("users/update", {
        global_uid: uid,
        tier: Number(newTier)
      });
      if (data && Array.isArray(data.users)) applyUsers(data.users);
      paint();
      app.toast(
        "已将 " +
          (user.display_name || uid) +
          " 从 " +
          tierMeta(oldTier).short +
          " 调整为 " +
          tierMeta(newTier).label
      );
    } catch (err) {
      paint();
      app.toast("修改关系等级失败：" + (err.message || err), true);
    } finally {
      S.busy = false;
    }
  }

  function clearDragClasses() {
    if (!S.container) return;
    S.container.querySelectorAll(".user-card.dragging").forEach(function (el) {
      el.classList.remove("dragging");
    });
    S.container.querySelectorAll(".kanban-col.drag-over").forEach(function (el) {
      el.classList.remove("drag-over");
    });
  }

  function handleDragStart(event) {
    var target = event.target;
    if (!target || !target.closest) return;
    var card = target.closest(".user-card");
    if (!card) return;
    S.dragUid = card.getAttribute("data-uid") || "";
    S.suppressClick = true;
    card.classList.add("dragging");
    if (event.dataTransfer) {
      try {
        event.dataTransfer.effectAllowed = "move";
        event.dataTransfer.setData("text/plain", S.dragUid);
      } catch (err) {
        /* ignore */
      }
    }
  }

  function handleDragOver(event) {
    var target = event.target;
    if (!target || !target.closest) return;
    var col = target.closest(".kanban-col");
    if (!col) return;
    event.preventDefault();
    if (event.dataTransfer) event.dataTransfer.dropEffect = "move";
    if (S.container) {
      S.container.querySelectorAll(".kanban-col.drag-over").forEach(function (el) {
        if (el !== col) el.classList.remove("drag-over");
      });
    }
    col.classList.add("drag-over");
  }

  function handleDragLeave(event) {
    var target = event.target;
    if (!target || !target.closest) return;
    var col = target.closest(".kanban-col");
    if (!col) return;
    if (event.relatedTarget && col.contains(event.relatedTarget)) return;
    col.classList.remove("drag-over");
  }

  function handleDrop(event) {
    var target = event.target;
    if (!target || !target.closest) return;
    var col = target.closest(".kanban-col");
    if (!col) return;
    event.preventDefault();
    col.classList.remove("drag-over");
    var uid = S.dragUid;
    if (!uid && event.dataTransfer) {
      try {
        uid = event.dataTransfer.getData("text/plain");
      } catch (err) {
        uid = "";
      }
    }
    var tier = parseInt(col.getAttribute("data-tier"), 10);
    S.dragUid = "";
    if (!uid || !tier) return;
    changeTier(uid, tier);
  }

  function handleDragEnd() {
    clearDragClasses();
    S.dragUid = "";
    setTimeout(function () {
      S.suppressClick = false;
    }, 80);
  }

  /* ------------------------------------------------------------------ *
   * 点击事件
   * ------------------------------------------------------------------ */
  function handleClick(event) {
    var target = event.target;
    if (!target || !target.closest) return;
    var actionEl = target.closest("[data-action]");
    if (actionEl) {
      var action = actionEl.getAttribute("data-action");
      if (action === "reload") reload(true);
      else if (action === "user-menu") openUserMenu(actionEl.getAttribute("data-uid"));
      return;
    }
    var card = target.closest(".user-card");
    if (!card) return;
    if (S.suppressClick) {
      S.suppressClick = false;
      return;
    }
    openEditDrawer(card.getAttribute("data-uid"));
  }

  function bindEvents(container, app) {
    container.addEventListener("click", handleClick);
    container.addEventListener("dragstart", handleDragStart);
    container.addEventListener("dragend", handleDragEnd);
    container.addEventListener("dragover", handleDragOver);
    container.addEventListener("dragleave", handleDragLeave);
    container.addEventListener("drop", handleDrop);
    container.addEventListener("keydown", function (event) {
      var target = event.target;
      if (!target || !target.closest) return;
      /* 焦点在卡片内的按钮 / 链接 / 输入框上时，交给它们自己的键盘行为 */
      if (target.closest("button, a, input, select, textarea")) return;
      var card = target.closest(".user-card");
      if (!card) return;
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        if (S.suppressClick) {
          S.suppressClick = false;
          return;
        }
        openEditDrawer(card.getAttribute("data-uid"));
      }
    });
  }

  /* ------------------------------------------------------------------ *
   * 生命周期
   * ------------------------------------------------------------------ */
  async function render(container, app) {
    S.app = app;
    S.container = container;
    S.destroyed = false;
    S.users = [];
    S.counts = { l3_count: 0, l2_count: 0, l1_count: 0, total: 0 };
    S.dragUid = "";
    S.suppressClick = false;
    S.busy = false;

    container.innerHTML =
      '<div class="loading"><span class="spinner"></span><span>加载用户关系…</span></div>';
    bindEvents(container, app);
    try {
      await loadData(app);
      if (!S.users.length) {
        container.innerHTML =
          '<div class="page-head"><h1 class="page-title">' +
          icon("relations", 18) +
          "关系</h1></div>" +
          emptyHtml("还没有用户档案");
        return;
      }
      paint();
    } catch (err) {
      console.error("[OnlyYou] 关系页渲染失败：", err);
      if (S.container) {
        S.container.innerHTML =
          '<div class="page-head"><h1 class="page-title">' +
          icon("relations", 18) +
          "关系</h1></div>" +
          '<div class="card"><div class="empty">' +
          icon("alert", 32) +
          '<div class="empty-title">用户关系加载失败</div>' +
          '<div class="empty-text">' +
          esc((err && (err.message || err)) || "未知错误") +
          "</div>" +
          '<div class="row mt-12"><button class="btn btn-outline" type="button" data-action="reload">重试</button></div>' +
          "</div></div>";
      }
    }
  }

  function destroy() {
    S.destroyed = true;
    S.app = null;
    S.container = null;
    S.users = [];
    S.dragUid = "";
    S.suppressClick = false;
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
