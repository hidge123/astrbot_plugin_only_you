/**
 * OnlyYou Plugin Pages — 冲浪页 (`views/console/page-surfing.js`)
 *
 * 数据源：
 *   GET  `surfing/list_and_trigger`              → { videos: [...], known_umos: [...] }
 *   POST `surfing/list_and_trigger` { action }   → surf / share / delete
 *
 * 页面结构：视频卡片墙（封面、标题、UP 主、评分徽章、吐槽、平台标签）；
 * 卡片悬浮菜单：分享（可选目标 UMO）/ 删除；工具栏：平台筛选 + 忽略状态门禁 + 手动冲浪。
 *
 * 注册：window.OY_PAGES.surfing = { id, title, icon, render(container, ctx), destroy() }
 */
(function () {
  "use strict";

  var PLATFORM_LABEL = { bilibili: "B站", douyin: "抖音" };

  var S = {
    app: null,
    container: null,
    destroyed: false,
    videos: [],
    knownUmos: [],
    platform: "",
    ignoreGate: true,
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
    if (!ts) return "";
    var now = Math.floor(Date.now() / 1000);
    var diff = Math.max(0, now - ts);
    if (diff < 60) return "刚刚";
    if (diff < 3600) return Math.floor(diff / 60) + " 分钟前";
    if (diff < 86400) return Math.floor(diff / 3600) + " 小时前";
    if (diff < 86400 * 30) return Math.floor(diff / 86400) + " 天前";
    var d = new Date(ts * 1000);
    return d.getFullYear() + "-" + pad2(d.getMonth() + 1) + "-" + pad2(d.getDate());
  }

  function platformLabel(platform) {
    var key = String(platform || "").toLowerCase();
    return PLATFORM_LABEL[key] || key || "视频";
  }

  function soulId() {
    return (S.app && S.app.state && S.app.state.currentSoulId) || "";
  }

  function videoById(videoId, rowId) {
    for (var i = 0; i < S.videos.length; i++) {
      var item = S.videos[i];
      if (!item) continue;
      if (videoId && String(item.video_id) === String(videoId)) return item;
      if (rowId != null && String(item.id) === String(rowId)) return item;
    }
    return null;
  }

  /* ------------------------------------------------------------------ *
   * HTML
   * ------------------------------------------------------------------ */
  function platformSegmentedHtml() {
    var options = [
      { value: "", label: "不限平台" },
      { value: "bilibili", label: "B站" },
      { value: "douyin", label: "抖音" }
    ];
    return (
      '<div class="segmented" data-role="platform-seg">' +
      options
        .map(function (item) {
          return (
            '<button type="button" class="seg-btn' +
            (String(S.platform) === item.value ? " active" : "") +
            '" data-action="set-platform" data-platform="' +
            esc(item.value) +
            '">' +
            esc(item.label) +
            "</button>"
          );
        })
        .join("") +
      "</div>"
    );
  }

  function statsHtml() {
    var total = S.videos.length;
    var scoreSum = 0;
    var shareCount = 0;
    S.videos.forEach(function (video) {
      scoreSum += num(video.score, 0);
      if (video.worth_sharing) shareCount += 1;
    });
    var avg = total ? Math.round(scoreSum / total) : 0;
    return (
      '<div class="row wrap" style="gap:6px" data-role="surf-stats">' +
      '<span class="tag">共 ' +
      esc(total) +
      " 条</span>" +
      '<span class="tag">平均评分 ' +
      esc(avg) +
      "</span>" +
      '<span class="tag tag-primary">值得分享 ' +
      esc(shareCount) +
      "</span>" +
      "</div>"
    );
  }

  function pageHeadHtml() {
    return (
      '<div class="page-head"><h1 class="page-title">' +
      icon("surf", 18) +
      "冲浪" +
      '<span class="card-sub">' +
      esc(S.videos.length) +
      " 条冲浪记录</span></h1>" +
      '<div class="page-actions">' +
      '<button class="btn btn-ghost btn-sm" type="button" data-action="reload">' +
      icon("refresh", 14) +
      "刷新</button>" +
      '<button class="btn btn-primary btn-sm" type="button" data-action="surf">' +
      icon("play", 13) +
      "手动冲浪</button>" +
      "</div></div>"
    );
  }

  function toolbarHtml() {
    return (
      '<div class="card"><div class="card-body tight"><div class="row-between wrap">' +
      '<div class="row wrap">' +
      platformSegmentedHtml() +
      '<label class="switch-label" data-tip="开启后即使不在 IDLE 空闲状态也会立即抓取视频（调试用）">' +
      '<span class="switch"><input type="checkbox" data-role="ignore-gate"' +
      (S.ignoreGate ? " checked" : "") +
      ' /><span class="switch-track"></span></span>' +
      "忽略状态门禁</label>" +
      "</div>" +
      statsHtml() +
      "</div>" +
      '<div class="text-xs muted mt-8" data-tip="冲浪只在 IDLE 空闲状态下自动触发；这里的手动触发用于调试平台抓取与看片评分流程。">IDLE 空闲时自动冲浪；手动触发用于调试</div>' +
      "</div></div>"
    );
  }

  function videoCardHtml(video) {
    var cover = String(video.cover_url || "").trim();
    var shareUrl = String(video.share_url || "").trim();
    var score = num(video.score, 0);
    var tags = Array.isArray(video.tags) ? video.tags : [];
    var tagHtml = tags
      .slice(0, 4)
      .map(function (tag) {
        return '<span class="tag">' + esc(tag) + "</span>";
      })
      .join("");
    var coverInner = cover
      ? '<img src="' +
        esc(cover) +
        '" alt="' +
        esc(video.title || "视频封面") +
        '" loading="lazy" referrerpolicy="no-referrer" />'
      : '<div class="video-cover-placeholder">' + icon("video", 30) + "</div>";
    var coverBlock =
      '<div class="video-cover">' +
      (shareUrl
        ? '<a href="' +
          esc(shareUrl) +
          '" target="_blank" rel="noopener noreferrer" style="display:block;width:100%;height:100%" aria-label="打开原视频">' +
          coverInner +
          "</a>"
        : coverInner) +
      '<span class="score-badge" data-tip="视频评分（0-100）">' +
      icon("spark", 11) +
      esc(score) +
      "</span>" +
      '<div class="video-menu">' +
      '<button class="icon-btn" type="button" data-action="share-video" data-video-id="' +
      esc(video.video_id) +
      '" data-row-id="' +
      esc(video.id) +
      '" data-tip="分享到会话" aria-label="分享">' +
      icon("link", 13) +
      "</button>" +
      '<button class="icon-btn" type="button" data-action="delete-video" data-video-id="' +
      esc(video.video_id) +
      '" data-row-id="' +
      esc(video.id) +
      '" data-tip="从冲浪池删除" aria-label="删除">' +
      icon("trash", 13) +
      "</button></div></div>";
    var titleHtml = shareUrl
      ? '<a class="video-title" style="color:var(--text)" href="' +
        esc(shareUrl) +
        '" target="_blank" rel="noopener noreferrer">' +
        esc(video.title || "未命名视频") +
        "</a>"
      : '<div class="video-title">' + esc(video.title || "未命名视频") + "</div>";
    return (
      '<div class="video-card" data-video-card="' +
      esc(video.id) +
      '">' +
      coverBlock +
      '<div class="video-card-body">' +
      titleHtml +
      '<div class="row wrap" style="gap:6px">' +
      '<span class="video-author">' +
      icon("user", 11) +
      " " +
      esc(video.author || "未知 UP 主") +
      "</span>" +
      '<span class="tag">' +
      esc(platformLabel(video.platform)) +
      "</span>" +
      (video.worth_sharing ? '<span class="tag tag-primary">值得分享</span>' : "") +
      "</div>" +
      (video.commentary
        ? '<div class="video-commentary" data-tip="' +
          esc(video.commentary) +
          '">' +
          esc(video.commentary) +
          "</div>"
        : "") +
      (tagHtml ? '<div class="row wrap" style="gap:4px">' + tagHtml + "</div>" : "") +
      '<div class="row-between mt-4"><span class="text-xs muted">' +
      esc(fmtAgo(video.watched_at)) +
      "</span>" +
      (video.visual_notes
        ? '<span class="text-xs muted ellipsis" style="max-width:60%" data-tip="' +
          esc(video.visual_notes) +
          '">看片笔记</span>'
        : "") +
      "</div></div></div>"
    );
  }

  function gridHtml() {
    if (!S.videos.length) {
      return (
        '<div class="card"><div class="empty">' +
        icon("surf", 32) +
        '<div class="empty-title">还没有冲浪记录</div>' +
        '<div class="empty-text">在 IDLE 空闲状态下 Bot 会自动刷 B 站 / 抖音热门视频并生成吐槽；也可以点下方按钮立即手动抓取一条。</div>' +
        '<div class="row mt-12"><button class="btn btn-outline" type="button" data-action="surf">' +
        icon("play", 13) +
        "手动冲浪</button></div>" +
        "</div></div>"
      );
    }
    return '<div class="video-grid">' + S.videos.map(videoCardHtml).join("") + "</div>";
  }

  function pageHtml() {
    return pageHeadHtml() + toolbarHtml() + gridHtml();
  }

  function paint() {
    if (!S.container || S.destroyed) return;
    var scroller = S.container.closest ? S.container.closest(".main") : null;
    var top = scroller ? scroller.scrollTop : 0;
    S.container.innerHTML = pageHtml();
    if (scroller) scroller.scrollTop = top;
  }

  /* ------------------------------------------------------------------ *
   * 数据加载
   * ------------------------------------------------------------------ */
  async function loadData(app) {
    var data = await app.apiGet("surfing/list_and_trigger", soulId() ? { soul_id: soulId() } : {});
    if (!data || typeof data !== "object") throw new Error("GET surfing/list_and_trigger 返回为空");
    S.videos = Array.isArray(data.videos) ? data.videos : [];
    S.knownUmos = Array.isArray(data.known_umos) ? data.known_umos : [];
  }

  async function reload(showToast) {
    if (!S.app || S.busy) return;
    S.busy = true;
    try {
      await loadData(S.app);
      paint();
      if (showToast) S.app.toast("冲浪池已刷新");
    } catch (err) {
      S.app.toast("刷新冲浪池失败：" + (err.message || err), true);
    } finally {
      S.busy = false;
    }
  }

  /* ------------------------------------------------------------------ *
   * 操作：手动冲浪 / 分享 / 删除
   * ------------------------------------------------------------------ */
  async function triggerSurf(buttonEl) {
    var app = S.app;
    if (!app || S.busy) return;
    S.busy = true;
    if (buttonEl) buttonEl.classList.add("is-loading");
    var targetBtn = buttonEl;
    try {
      app.toast("正在抓取视频并生成看片吐槽…");
      var body = {
        action: "surf",
        soul_id: soulId(),
        ignore_state_gate: !!S.ignoreGate
      };
      if (S.platform) body.platform = S.platform;
      var data = await app.apiPost("surfing/list_and_trigger", body);
      if (data && Array.isArray(data.videos)) S.videos = data.videos;
      if (data && data.review) {
        app.toast(
          "冲浪完成：《" +
            String(data.review.title || "").slice(0, 24) +
            "》 评分 " +
            num(data.review.score, 0)
        );
      } else {
        app.toast("冲浪完成");
      }
      paint();
    } catch (err) {
      app.toast("手动冲浪失败：" + (err.message || err), true);
      if (targetBtn) targetBtn.classList.remove("is-loading");
    } finally {
      S.busy = false;
    }
  }

  function openShareModal(videoId, rowId) {
    var app = S.app;
    var video = videoById(videoId, rowId);
    if (!app || !video) return;
    var options = '<option value="">自动选择（最近 L3 私聊 / 活跃会话）</option>';
    S.knownUmos.forEach(function (item) {
      var umo = item && item.umo ? String(item.umo) : "";
      if (!umo) return;
      var tags = [];
      if (item.is_l3_private) tags.push("L3 私聊");
      if (item.bound_soul_id) tags.push("已绑定 " + item.bound_soul_id);
      options +=
        '<option value="' +
        esc(umo) +
        '">' +
        esc(umo + (tags.length ? "（" + tags.join(" · ") + "）" : "")) +
        "</option>";
    });
    var body =
      '<div class="card" style="background:var(--surface-2);border:0"><div class="card-body tight">' +
      '<div class="text-sm" style="font-weight:600">' +
      esc(video.title || "未命名视频") +
      "</div>" +
      '<div class="text-xs muted mt-4">' +
      esc(platformLabel(video.platform)) +
      " · " +
      esc(video.author || "未知 UP 主") +
      " · 评分 " +
      esc(num(video.score, 0)) +
      "</div>" +
      (video.commentary
        ? '<div class="text-xs text-2 mt-8">' + esc(video.commentary) + "</div>"
        : "") +
      "</div></div>" +
      '<div class="field mt-12"><span class="field-label">分享目标会话</span>' +
      '<select class="select" data-role="target-umo">' +
      options +
      "</select>" +
      '<div class="field-help">会以角色口吻把吐槽 + 视频标题 + 链接发送到目标会话；不选择时自动寻找最近活跃的 L3 私聊 / 会话。</div></div>';
    app.modal({
      title: "分享视频",
      body: body,
      actions: [
        { label: "取消" },
        {
          label: "分享",
          primary: true,
          icon: "link",
          onClick: async function (modalApp, bodyEl) {
            var selectEl = bodyEl.querySelector('[data-role="target-umo"]');
            var targetUmo = selectEl ? String(selectEl.value || "").trim() : "";
            var payload = {
              action: "share",
              soul_id: soulId(),
              video_id: String(video.video_id || video.id || "")
            };
            if (targetUmo) payload.target_umo = targetUmo;
            var data = await modalApp.apiPost("surfing/list_and_trigger", payload);
            var sentTo = (data && data.target_umo) || targetUmo || "默认会话";
            if (data && data.sent === false) {
              modalApp.toast("分享未送达（目标会话不在线），已记录到默认会话窗口");
            } else {
              modalApp.toast("已分享到 " + sentTo);
            }
            return true;
          }
        }
      ]
    });
  }

  function confirmDeleteVideo(videoId, rowId) {
    var app = S.app;
    var video = videoById(videoId, rowId);
    if (!app || !video) return;
    app
      .confirm({
        title: "删除冲浪记录",
        message:
          "确定从冲浪池删除《" +
          String(video.title || "未命名视频") +
          "》吗？删除后该视频不会再出现在分享列表里（不会影响原平台视频）。",
        danger: true,
        confirmLabel: "删除",
        onConfirm: async function () {
          var payload = {
            action: "delete",
            soul_id: soulId(),
            video_id: String(video.video_id || "")
          };
          if (video.id != null) payload.id = video.id;
          var data = await app.apiPost("surfing/list_and_trigger", payload);
          if (data && Array.isArray(data.videos)) S.videos = data.videos;
          paint();
          app.toast("已从冲浪池删除该视频");
        }
      })
      .then(function () {
        /* 结果已通过 toast 提示 */
      });
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
    } else if (action === "surf") {
      triggerSurf(actionEl);
    } else if (action === "share-video") {
      openShareModal(actionEl.getAttribute("data-video-id"), actionEl.getAttribute("data-row-id"));
    } else if (action === "delete-video") {
      confirmDeleteVideo(actionEl.getAttribute("data-video-id"), actionEl.getAttribute("data-row-id"));
    } else if (action === "set-platform") {
      S.platform = actionEl.getAttribute("data-platform") || "";
      var seg = S.container ? S.container.querySelector('[data-role="platform-seg"]') : null;
      if (seg) {
        seg.querySelectorAll(".seg-btn").forEach(function (btn) {
          btn.classList.toggle("active", (btn.getAttribute("data-platform") || "") === S.platform);
        });
      }
    }
  }

  function bindEvents(container, app) {
    container.addEventListener("click", handleClick);
    container.addEventListener("change", function (event) {
      var target = event.target;
      if (!target || !target.getAttribute) return;
      if (target.getAttribute("data-role") === "ignore-gate") {
        S.ignoreGate = !!target.checked;
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
    S.videos = [];
    S.knownUmos = [];
    S.platform = "";
    S.ignoreGate = true;
    S.busy = false;

    container.innerHTML =
      '<div class="loading"><span class="spinner"></span><span>加载冲浪池…</span></div>';
    bindEvents(container, app);
    try {
      await loadData(app);
      paint();
    } catch (err) {
      console.error("[OnlyYou] 冲浪页渲染失败：", err);
      container.innerHTML =
        '<div class="page-head"><h1 class="page-title">' +
        icon("surf", 18) +
        "冲浪</h1></div>" +
        '<div class="card"><div class="empty">' +
        icon("alert", 32) +
        '<div class="empty-title">冲浪池加载失败</div>' +
        '<div class="empty-text">' +
        esc((err && (err.message || err)) || "未知错误") +
        "</div>" +
        '<div class="row mt-12"><button class="btn btn-outline" type="button" data-action="reload">重试</button></div>' +
        "</div></div>";
    }
  }

  function destroy() {
    S.destroyed = true;
    S.app = null;
    S.container = null;
    S.videos = [];
    S.knownUmos = [];
    S.busy = false;
  }

  window.OY_PAGES = window.OY_PAGES || {};
  window.OY_PAGES.surfing = {
    id: "surfing",
    title: "冲浪",
    icon: "surf",
    render: render,
    destroy: destroy
  };
})();
