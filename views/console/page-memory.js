/**
 * OnlyYou Plugin Pages — 记忆页 (`views/console/page-memory.js`)
 *
 * 数据源：
 *   GET  `memories/list`   → { memories: [...], stats: {...} }（支持 memory_tier / keyword / soul_id）
 *   POST `memories/mutate` → action = add / edit / delete / pin_core / extract / gc
 *
 * 页面结构：顶部工具栏（Tier 分段 + 搜索 + 提炼 / GC / 新增 / 刷新），下方记忆卡片网格。
 * 卡片显示内容、关键词标签、重要度、活力值进度条、召回次数；操作：编辑、钉选核心、删除。
 *
 * 注册：window.OY_PAGES.memory = { id, title, icon, render(container, ctx), destroy() }
 */
(function () {
  "use strict";

  var TIER_META = {
    3: { label: "T3 核心", desc: "核心永久记忆", cls: "tier-3" },
    2: { label: "T2 近期", desc: "近期记忆（会随时间衰减）", cls: "tier-2" },
    1: { label: "T1 瞬时", desc: "瞬时记忆（48 小时后过期）", cls: "tier-1" }
  };
  var TIER_FILTERS = [
    { value: "", label: "全部" },
    { value: "3", label: "T3 核心" },
    { value: "2", label: "T2 近期" },
    { value: "1", label: "T1 瞬时" }
  ];

  var S = {
    app: null,
    container: null,
    destroyed: false,
    memories: [],
    stats: null,
    tier: "",
    keyword: "",
    busy: false,
    searchTimer: null
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
    var diff = now - ts;
    if (diff < 0) diff = 0;
    if (diff < 60) return "刚刚";
    if (diff < 3600) return Math.floor(diff / 60) + " 分钟前";
    if (diff < 86400) return Math.floor(diff / 3600) + " 小时前";
    if (diff < 86400 * 30) return Math.floor(diff / 86400) + " 天前";
    var d = new Date(ts * 1000);
    return d.getFullYear() + "-" + pad2(d.getMonth() + 1) + "-" + pad2(d.getDate());
  }

  function tierMeta(tier) {
    return TIER_META[Number(tier)] || TIER_META[2];
  }

  function splitKeywords(keywords) {
    return String(keywords == null ? "" : keywords)
      .split(/[,，、/;；|]+/)
      .map(function (item) {
        return item.trim();
      })
      .filter(Boolean);
  }

  function vitalityColor(value) {
    var vitality = num(value, 0);
    if (vitality >= 60) return "var(--primary)";
    if (vitality >= 30) return "var(--warning)";
    return "var(--danger)";
  }

  function soulId() {
    return (S.app && S.app.state && S.app.state.currentSoulId) || "";
  }

  /* ------------------------------------------------------------------ *
   * HTML
   * ------------------------------------------------------------------ */
  function tierSegmentedHtml() {
    return (
      '<div class="segmented" data-role="tier-seg">' +
      TIER_FILTERS.map(function (item) {
        var active = String(item.value) === String(S.tier);
        return (
          '<button type="button" class="seg-btn' +
          (active ? " active" : "") +
          '" data-action="set-tier" data-tier="' +
          esc(item.value) +
          '">' +
          esc(item.label) +
          "</button>"
        );
      }).join("") +
      "</div>"
    );
  }

  function statsTagsHtml() {
    var stats = S.stats || {};
    return (
      '<div class="row wrap" data-role="stats-tags" style="gap:6px">' +
      '<span class="tier-pill tier-3">T3 ' +
      esc(num(stats.tier3_count, 0)) +
      "</span>" +
      '<span class="tier-pill tier-2">T2 ' +
      esc(num(stats.tier2_count, 0)) +
      "</span>" +
      '<span class="tier-pill tier-1">T1 ' +
      esc(num(stats.tier1_count, 0)) +
      "</span>" +
      '<span class="tag tag-muted">共 ' +
      esc(num(stats.total, S.memories.length)) +
      " 条</span>" +
      "</div>"
    );
  }

  function injectionBudgetHint() {
    var stats = S.stats || {};
    var injected = num(stats.max_injected_memories, 5);
    var budget = num(stats.max_memory_chars_budget, 400);
    var windowSize = num(stats.sliding_window_size, 12);
    return (
      '<span class="hint" data-tip="上下文预算：每次对话最多注入 ' +
      injected +
      " 条记忆 / " +
      budget +
      " 字；会话滑窗保留最近 " +
      windowSize +
      ' 条消息">每次注入 ≤' +
      esc(injected) +
      " 条 · " +
      esc(budget) +
      " 字</span>"
    );
  }

  function pageHeadHtml() {
    var total = S.stats ? num(S.stats.total, S.memories.length) : S.memories.length;
    return (
      '<div class="page-head"><h1 class="page-title">' +
      icon("memory", 18) +
      "记忆" +
      '<span class="card-sub">' +
      esc(total) +
      " 条分级记忆</span></h1>" +
      '<div class="page-actions">' +
      '<button class="btn btn-outline btn-sm" type="button" data-action="extract">' +
      icon("wand", 13) +
      "提炼</button>" +
      '<button class="btn btn-outline btn-sm" type="button" data-action="gc" data-tip="运行每日遗忘清理：T1 过期删除 / T2 活力衰减 / T3 访问强化晋升">' +
      icon("refresh", 13) +
      "GC</button>" +
      '<button class="btn btn-outline btn-sm" type="button" data-action="reload">' +
      icon("refresh", 13) +
      "刷新</button>" +
      '<button class="btn btn-primary btn-sm" type="button" data-action="create">' +
      icon("plus", 13) +
      "新增</button>" +
      "</div></div>"
    );
  }

  function toolbarHtml() {
    return (
      '<div class="card"><div class="card-body tight">' +
      '<div class="row wrap grow">' +
      tierSegmentedHtml() +
      '<div class="row grow" style="min-width:180px;max-width:330px">' +
      icon("search", 14) +
      '<input class="input input-sm" type="text" data-role="search-input" placeholder="搜索记忆内容 / 关键词，回车立即搜索" value="' +
      esc(S.keyword) +
      '" />' +
      "</div>" +
      '<div class="row grow" style="justify-content:flex-end">' +
      statsTagsHtml() +
      "</div>" +
      "</div>" +
      '<div class="row-between mt-8"><span class="text-xs muted" data-tip="Tier 1 瞬时记忆 48 小时后过期；Tier 2 近期记忆活力值随时间衰减；Tier 3 核心记忆永久保留并优先注入。">T1 48h 过期 · T2 活力衰减 · T3 永久保留</span>' +
      injectionBudgetHint() +
      "</div></div></div>"
    );
  }

  function memoryCardHtml(memory) {
    var tier = Number(memory.memory_tier) || 2;
    var meta = tierMeta(tier);
    var keywords = splitKeywords(memory.keywords);
    var keywordHtml = keywords
      .slice(0, 6)
      .map(function (keyword) {
        return '<span class="tag">' + esc(keyword) + "</span>";
      })
      .join("");
    var moreKeywords =
      keywords.length > 6 ? '<span class="tag tag-muted">+' + (keywords.length - 6) + "</span>" : "";
    var vitality = Math.max(0, Math.min(100, num(memory.vitality, 0)));
    var userLabel = memory.user_id ? String(memory.user_id) : "全局记忆";
    return (
      '<div class="memory-card" data-id="' +
      esc(memory.id) +
      '" data-tier="' +
      tier +
      '">' +
      '<div class="memory-card-head">' +
      '<div class="row wrap" style="gap:6px">' +
      '<span class="tier-pill ' +
      meta.cls +
      '">' +
      esc(meta.label) +
      "</span>" +
      '<span class="tag tag-muted" data-tip="重要度 1-10，影响记忆注入优先级">重要度 ' +
      esc(num(memory.importance, 5)) +
      "</span>" +
      '<span class="tag tag-scope">' +
      esc(userLabel) +
      "</span>" +
      "</div>" +
      '<div class="row" style="gap:2px">' +
      '<button class="icon-btn" type="button" style="width:24px;height:24px" data-action="edit-memory" data-id="' +
      esc(memory.id) +
      '" data-tip="编辑记忆" aria-label="编辑记忆">' +
      icon("edit", 13) +
      "</button>" +
      (tier !== 3
        ? '<button class="icon-btn" type="button" style="width:24px;height:24px" data-action="pin-memory" data-id="' +
          esc(memory.id) +
          '" data-tip="钉选为核心记忆（Tier 3）" aria-label="钉选核心">' +
          icon("spark", 13) +
          "</button>"
        : "") +
      '<button class="icon-btn" type="button" style="width:24px;height:24px" data-action="delete-memory" data-id="' +
      esc(memory.id) +
      '" data-tip="删除记忆" aria-label="删除记忆">' +
      icon("trash", 13) +
      "</button>" +
      "</div></div>" +
      '<div class="memory-content" data-tip="' +
      esc(memory.content) +
      '">' +
      esc(memory.content) +
      "</div>" +
      (keywordHtml
        ? '<div class="row wrap" style="gap:4px">' + keywordHtml + moreKeywords + "</div>"
        : "") +
      '<div class="vitality">' +
      '<span class="mini-bar"><i style="width:' +
      vitality.toFixed(1) +
      "%;background:" +
      vitalityColor(vitality) +
      '"></i></span>' +
      "<span>活力 " +
      esc(vitality.toFixed(1)) +
      "</span>" +
      "<span>召回 " +
      esc(num(memory.access_count, 0)) +
      " 次</span>" +
      '<span class="grow" style="text-align:right">' +
      esc(fmtAgo(memory.created_at)) +
      "</span></div></div>"
    );
  }

  function gridHtml() {
    if (!S.memories.length) {
      return (
        '<div class="card"><div class="empty">' +
        icon("memory", 32) +
        '<div class="empty-title">' +
        (S.keyword || S.tier ? "没有符合筛选条件的记忆" : "还没有任何记忆") +
        "</div>" +
        '<div class="empty-text">' +
        (S.keyword || S.tier
          ? "试试切换 Tier 或清空搜索关键词。"
          : "可以在会话页点「提炼」让决策模型从聊天滑窗里总结记忆，也可以手动新增一条。") +
        "</div>" +
        '<div class="row mt-12">' +
        '<button class="btn btn-outline" type="button" data-action="extract">' +
        icon("wand", 13) +
        "从会话提炼</button>" +
        '<button class="btn btn-outline" type="button" data-action="create">' +
        icon("plus", 13) +
        "新增记忆</button></div>" +
        "</div></div>"
      );
    }
    return (
      '<div class="memory-grid" data-role="memory-grid">' +
      S.memories.map(memoryCardHtml).join("") +
      "</div>"
    );
  }

  function pageHtml() {
    return (
      pageHeadHtml() +
      toolbarHtml() +
      '<div data-role="memory-grid-wrap">' +
      gridHtml() +
      "</div>"
    );
  }

  /* ------------------------------------------------------------------ *
   * 数据加载 / 局部刷新
   * ------------------------------------------------------------------ */
  async function fetchMemories(app) {
    var params = {};
    if (soulId()) params.soul_id = soulId();
    if (S.tier) params.memory_tier = S.tier;
    if (S.keyword) params.keyword = S.keyword;
    var data = await app.apiGet("memories/list", params);
    if (!data || typeof data !== "object") throw new Error("GET memories/list 返回为空");
    S.memories = Array.isArray(data.memories) ? data.memories : [];
    S.stats = data.stats && typeof data.stats === "object" ? data.stats : null;
  }

  function refreshGrid() {
    if (!S.container || S.destroyed) return;
    var wrap = S.container.querySelector('[data-role="memory-grid-wrap"]');
    if (wrap) wrap.innerHTML = gridHtml();
    var statsEl = S.container.querySelector('[data-role="stats-tags"]');
    if (statsEl) statsEl.outerHTML = statsTagsHtml();
    var titleSub = S.container.querySelector(".page-head .card-sub");
    if (titleSub) {
      var total = S.stats ? num(S.stats.total, S.memories.length) : S.memories.length;
      titleSub.textContent = total + " 条分级记忆";
    }
    var seg = S.container.querySelector('[data-role="tier-seg"]');
    if (seg) {
      seg.querySelectorAll(".seg-btn").forEach(function (btn) {
        btn.classList.toggle("active", String(btn.getAttribute("data-tier")) === String(S.tier));
      });
    }
  }

  function paint() {
    if (!S.container || S.destroyed) return;
    var scroller = S.container.closest ? S.container.closest(".main") : null;
    var top = scroller ? scroller.scrollTop : 0;
    S.container.innerHTML = pageHtml();
    if (scroller) scroller.scrollTop = top;
  }

  async function reloadMemories(showToast) {
    if (!S.app) return;
    try {
      await fetchMemories(S.app);
      refreshGrid();
      if (showToast) S.app.toast("记忆列表已刷新");
    } catch (err) {
      S.app.toast("刷新记忆失败：" + (err.message || err), true);
    }
  }

  /* ------------------------------------------------------------------ *
   * 表单：新增 / 编辑
   * ------------------------------------------------------------------ */
  function formHtml(memory) {
    var isEdit = !!memory;
    var content = isEdit ? memory.content || "" : "";
    var keywords = isEdit ? memory.keywords || "" : "";
    var importance = isEdit ? num(memory.importance, 8) : 8;
    var tier = isEdit ? Number(memory.memory_tier) || 3 : 3;
    return (
      '<div class="field"><span class="field-label">记忆内容 <span class="req">*</span></span>' +
      '<textarea class="textarea" rows="4" data-role="content" placeholder="例如：阿泽严重海鲜过敏，聚餐千万别点海鲜">' +
      esc(content) +
      "</textarea>" +
      '<div class="field-help">按核心记忆设计，内容会持久化为单条 ≤60 字的事实短句；更长的说明请拆成多条。</div></div>' +
      '<div class="field"><span class="field-label">关键词</span>' +
      '<input class="input" type="text" data-role="keywords" value="' +
      esc(keywords) +
      '" placeholder="逗号分隔，例如：海鲜过敏,饮食,健康" />' +
      '<div class="field-help">关键词用于记忆召回匹配，建议 2-4 个。</div></div>' +
      '<div class="field-grid">' +
      '<div class="field"><span class="field-label">重要度</span>' +
      '<div class="slider-row"><input class="slider" type="range" min="1" max="10" step="1" data-role="importance" value="' +
      importance +
      '" /><span class="slider-value" data-role="importance-value">' +
      importance +
      "</span></div></div>" +
      '<div class="field"><span class="field-label">记忆分级</span>' +
      '<select class="select" data-role="memory-tier">' +
      [3, 2, 1]
        .map(function (value) {
          var meta = tierMeta(value);
          return (
            '<option value="' +
            value +
            '"' +
            (value === tier ? " selected" : "") +
            ">" +
            esc(meta.label + " · " + meta.desc) +
            "</option>"
          );
        })
        .join("") +
      "</select></div></div>"
    );
  }

  function readForm(bodyEl) {
    var contentEl = bodyEl.querySelector('[data-role="content"]');
    var keywordsEl = bodyEl.querySelector('[data-role="keywords"]');
    var importanceEl = bodyEl.querySelector('[data-role="importance"]');
    var tierEl = bodyEl.querySelector('[data-role="memory-tier"]');
    var content = contentEl ? String(contentEl.value || "").trim() : "";
    return {
      content: content,
      keywords: keywordsEl ? String(keywordsEl.value || "").trim() : "",
      importance: importanceEl ? Number(importanceEl.value) : 8,
      memory_tier: tierEl ? Number(tierEl.value) : 3
    };
  }

  function bindFormSliders(bodyEl) {
    var importanceEl = bodyEl.querySelector('[data-role="importance"]');
    var valueEl = bodyEl.querySelector('[data-role="importance-value"]');
    if (importanceEl && valueEl) {
      importanceEl.addEventListener("input", function () {
        valueEl.textContent = importanceEl.value;
      });
    }
  }

  function openCreateModal() {
    var app = S.app;
    if (!app) return;
    app.modal({
      title: "新增记忆",
      body: formHtml(null),
      actions: [
        { label: "取消" },
        {
          label: "保存",
          primary: true,
          icon: "save",
          onClick: async function (modalApp, bodyEl) {
            var form = readForm(bodyEl);
            if (!form.content) {
              modalApp.toast("记忆内容不能为空", true);
              return false;
            }
            var data = await modalApp.apiPost("memories/mutate", {
              action: "add",
              soul_id: soulId(),
              content: form.content,
              keywords: form.keywords,
              importance: form.importance,
              memory_tier: form.memory_tier
            });
            if (data && data.new_id != null) {
              modalApp.toast("已新增记忆 #" + data.new_id);
            } else {
              modalApp.toast("已新增记忆");
            }
            await reloadMemories(false);
            return true;
          }
        }
      ],
      onMount: function (bodyEl) {
        bindFormSliders(bodyEl);
      }
    });
  }

  function findMemory(id) {
    for (var i = 0; i < S.memories.length; i++) {
      if (String(S.memories[i].id) === String(id)) return S.memories[i];
    }
    return null;
  }

  function openEditModal(memoryId) {
    var app = S.app;
    var memory = findMemory(memoryId);
    if (!app || !memory) return;
    app.modal({
      title: "编辑记忆 #" + memory.id,
      body: formHtml(memory),
      actions: [
        { label: "取消" },
        {
          label: "保存",
          primary: true,
          icon: "save",
          onClick: async function (modalApp, bodyEl) {
            var form = readForm(bodyEl);
            if (!form.content) {
              modalApp.toast("记忆内容不能为空", true);
              return false;
            }
            await modalApp.apiPost("memories/mutate", {
              action: "edit",
              id: memory.id,
              soul_id: soulId(),
              content: form.content,
              keywords: form.keywords,
              importance: form.importance,
              memory_tier: form.memory_tier
            });
            modalApp.toast("已保存记忆 #" + memory.id);
            await reloadMemories(false);
            return true;
          }
        }
      ],
      onMount: function (bodyEl) {
        bindFormSliders(bodyEl);
      }
    });
  }

  /* ------------------------------------------------------------------ *
   * 提炼 / GC / 钉选 / 删除
   * ------------------------------------------------------------------ */
  function openExtractModal() {
    var app = S.app;
    if (!app) return;
    var knownUmos = (app.state && app.state.knownUmos) || [];
    var options = '<option value="">自动选择最近活跃会话</option>';
    knownUmos.forEach(function (item) {
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
      '<div class="field"><span class="field-label">从哪个会话滑窗提炼</span>' +
      '<select class="select" data-role="umo">' +
      options +
      "</select>" +
      '<div class="field-help">调用决策模型从最近 15 轮对话中提炼值得长期记住的事实与偏好；不指定 UMO 时自动选择最近活跃会话。</div></div>' +
      '<div class="card" style="background:var(--surface-2);border:0"><div class="card-body tight"><div class="text-xs muted">提示：提炼只读取会话滑窗，不会重复写入已有记忆；结果可以在列表里继续编辑或删除。</div></div></div>';
    app.modal({
      title: "从会话滑窗提炼记忆",
      body: body,
      actions: [
        { label: "取消" },
        {
          label: "开始提炼",
          primary: true,
          icon: "wand",
          onClick: async function (modalApp, bodyEl) {
            var selectEl = bodyEl.querySelector('[data-role="umo"]');
            var umo = selectEl ? String(selectEl.value || "").trim() : "";
            modalApp.toast("正在提炼记忆，请稍候…");
            try {
              var data = await modalApp.apiPost("memories/mutate", {
                action: "extract",
                soul_id: soulId(),
                umo: umo
              });
              var count = data && data.extracted_count != null ? Number(data.extracted_count) : 0;
              modalApp.toast("提炼完成：新增 " + count + " 条记忆");
            } catch (err) {
              modalApp.toast("提炼失败：" + (err.message || err), true);
              return false;
            }
            await reloadMemories(false);
            return true;
          }
        }
      ]
    });
  }

  function confirmGc() {
    var app = S.app;
    if (!app) return;
    app
      .confirm({
        title: "运行遗忘清理 (GC)",
        message:
          "将执行一次每日遗忘清理：Tier 1 超过 48 小时的记忆会被删除，Tier 2 按重要度与新鲜度衰减活力值（活力归零则物理删除），访问次数达到阈值的记忆晋升为 Tier 3。此操作不可撤销，是否继续？",
        confirmLabel: "开始 GC",
        onConfirm: async function () {
          try {
            var data = await app.apiPost("memories/mutate", {
              action: "gc",
              soul_id: soulId()
            });
            var stats = (data && data.gc_stats) || {};
            app.toast(
              "GC 完成：清理 T1 " +
                num(stats.deleted_tier1, 0) +
                " 条 / 删除 T2 " +
                num(stats.deleted_tier2, 0) +
                " 条 / 晋升 T3 " +
                num(stats.promoted_to_tier3, 0) +
                " 条"
            );
            await reloadMemories(false);
          } catch (err) {
            app.toast("GC 失败：" + (err.message || err), true);
            throw err;
          }
        }
      })
      .then(function () {
        /* 结果已通过 toast 提示 */
      });
  }

  async function pinMemory(id) {
    var app = S.app;
    if (!app) return;
    var button = S.container
      ? S.container.querySelector('[data-action="pin-memory"][data-id="' + id + '"]')
      : null;
    if (button) button.classList.add("is-loading");
    try {
      await app.apiPost("memories/mutate", {
        action: "pin_core",
        id: Number(id),
        soul_id: soulId()
      });
      app.toast("已将该记忆锁定为 Tier 3 核心记忆");
      await reloadMemories(false);
    } catch (err) {
      app.toast("钉选核心失败：" + (err.message || err), true);
      if (button) button.classList.remove("is-loading");
    }
  }

  function confirmDeleteMemory(id) {
    var app = S.app;
    var memory = findMemory(id);
    if (!app) return;
    var preview = memory ? String(memory.content || "").slice(0, 60) : "";
    app
      .confirm({
        title: "删除记忆",
        message: "确定物理删除这条记忆吗？删除后不可恢复。\n\n「" + preview + (preview.length >= 60 ? "…" : "") + "」",
        danger: true,
        confirmLabel: "删除",
        onConfirm: async function () {
          await app.apiPost("memories/mutate", {
            action: "delete",
            id: Number(id),
            soul_id: soulId()
          });
          app.toast("已删除记忆 #" + id);
          await reloadMemories(false);
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
      reloadMemories(true);
    } else if (action === "create") {
      openCreateModal();
    } else if (action === "extract") {
      openExtractModal();
    } else if (action === "gc") {
      confirmGc();
    } else if (action === "edit-memory") {
      openEditModal(actionEl.getAttribute("data-id"));
    } else if (action === "delete-memory") {
      confirmDeleteMemory(actionEl.getAttribute("data-id"));
    } else if (action === "pin-memory") {
      pinMemory(actionEl.getAttribute("data-id"));
    } else if (action === "set-tier") {
      setTier(actionEl.getAttribute("data-tier"));
    }
  }

  async function setTier(tier) {
    var next = tier == null ? "" : String(tier);
    if (next === String(S.tier)) return;
    S.tier = next;
    if (!S.app) return;
    try {
      await fetchMemories(S.app);
      refreshGrid();
    } catch (err) {
      S.app.toast("切换记忆分级失败：" + (err.message || err), true);
    }
  }

  function scheduleSearch() {
    if (S.searchTimer) {
      clearTimeout(S.searchTimer);
      S.searchTimer = null;
    }
    S.searchTimer = setTimeout(function () {
      S.searchTimer = null;
      if (!S.app || S.destroyed) return;
      fetchMemories(S.app)
        .then(refreshGrid)
        .catch(function (err) {
          if (S.app) S.app.toast("搜索记忆失败：" + (err.message || err), true);
        });
    }, 400);
  }

  function bindEvents(container, app) {
    container.addEventListener("click", handleClick);
    container.addEventListener("input", function (event) {
      var target = event.target;
      if (!target || !target.getAttribute) return;
      if (target.getAttribute("data-role") === "search-input") {
        S.keyword = String(target.value || "").trim();
        scheduleSearch();
      }
    });
    container.addEventListener("keydown", function (event) {
      var target = event.target;
      if (!target || !target.getAttribute) return;
      if (target.getAttribute("data-role") !== "search-input") return;
      if (event.key !== "Enter") return;
      event.preventDefault();
      S.keyword = String(target.value || "").trim();
      if (S.searchTimer) {
        clearTimeout(S.searchTimer);
        S.searchTimer = null;
      }
      if (!S.app) return;
      fetchMemories(S.app)
        .then(refreshGrid)
        .catch(function (err) {
          if (S.app) S.app.toast("搜索记忆失败：" + (err.message || err), true);
        });
    });
  }

  /* ------------------------------------------------------------------ *
   * 生命周期
   * ------------------------------------------------------------------ */
  async function render(container, app) {
    S.app = app;
    S.container = container;
    S.destroyed = false;
    S.memories = [];
    S.stats = null;
    S.tier = "";
    S.keyword = "";
    S.busy = false;
    if (S.searchTimer) {
      clearTimeout(S.searchTimer);
      S.searchTimer = null;
    }

    container.innerHTML =
      '<div class="loading"><span class="spinner"></span><span>加载记忆库…</span></div>';
    bindEvents(container, app);
    try {
      await fetchMemories(app);
      paint();
    } catch (err) {
      console.error("[OnlyYou] 记忆页渲染失败：", err);
      container.innerHTML =
        '<div class="page-head"><h1 class="page-title">' +
        icon("memory", 18) +
        "记忆</h1></div>" +
        '<div class="card"><div class="empty">' +
        icon("alert", 32) +
        '<div class="empty-title">记忆库加载失败</div>' +
        '<div class="empty-text">' +
        esc((err && (err.message || err)) || "未知错误") +
        "</div>" +
        '<div class="row mt-12"><button class="btn btn-outline" type="button" data-action="reload">重试</button></div>' +
        "</div></div>";
    }
  }

  function destroy() {
    S.destroyed = true;
    if (S.searchTimer) {
      clearTimeout(S.searchTimer);
      S.searchTimer = null;
    }
    S.app = null;
    S.container = null;
    S.memories = [];
    S.stats = null;
    S.busy = false;
    S.keyword = "";
    S.tier = "";
  }

  window.OY_PAGES = window.OY_PAGES || {};
  window.OY_PAGES.memory = {
    id: "memory",
    title: "记忆",
    icon: "memory",
    render: render,
    destroy: destroy
  };
})();
