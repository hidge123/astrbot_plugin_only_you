/**
 * OnlyYou (只有你) — AstrBot Plugin Pages Console Frontend (`views/console/app.js`)
 * Implements the 5-module interactive workspace designed in `docs/WEBUI_DESIGN.md`:
 *  1. 🗂️ Soul & Persona Studio (Import native Persona, rename Soul ID, UMO routing bindings)
 *  2. 🃏 Action Cards & Schedule (Live 6-slot 3-state timeline, reroll, force state, card deck & AI generator)
 *  3. 💖 Relationships (L1/L2/L3 user tiers, affinity, nicknames, cross-platform GlobalUser merge)
 *  4. 🧠 Memory Vault (Tier 1/2/3 memories, Vitality bar, one-click pin to CORE, Forgetting GC)
 *  5. 📱 Surfing Pool (Bilibili/Douyin watched video wall, multimodal visual notes, one-click surf & share)
 */

const bridge = window.AstrBotPluginPage || window.AstrBotPluginView || null;

function escHtml(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function unwrapApiPayload(response) {
  const wrapped =
    response && ["ok", "error"].includes(response.status) && response.data !== undefined
      ? response
      : response &&
        response.data &&
        ["ok", "error"].includes(response.data.status) &&
        response.data.data !== undefined
      ? response.data
      : null;

  if (wrapped && wrapped.status === "error") {
    throw new Error(wrapped.reason || wrapped.message || "请求处理失败");
  }
  return wrapped ? wrapped.data : response;
}

class OnlyYouConsoleApp {
  constructor() {
    this.souls = [];
    this.nativePersonas = [];
    this.currentSoulId = "default_soul";
    this.currentSoul = null;
    this.currentSchedule = null;
    this.umoBindings = {};
    this.knownUmos = [];
    this.modelStatus = {};
    this.slotDefinitions = [];

    // Module 2 state
    this.customCards = [];
    this.generalCards = [];
    this.customEncounters = [];
    this.generalEncounters = [];
    this.cardFilter = "ALL";

    // Module 3 state
    this.users = [];
    this.userCounts = { l3_count: 0, l2_count: 0, l1_count: 0, total: 0 };
    this.userTierFilter = "";

    // Module 4 state
    this.memories = [];
    this.memoryStats = {};
    this.memoryTierFilter = "";

    // Module 5 state
    this.surfingVideos = [];

    // Modal state
    this.soulIdModalMode = "rename"; // "rename" | "create"
  }

  async apiGet(endpoint, params = {}) {
    if (bridge && typeof bridge.apiGet === "function") {
      const res = await bridge.apiGet(endpoint, params);
      return unwrapApiPayload(res);
    }
    const qs = new URLSearchParams(params).toString();
    const url = `/api/plug/astrbot_plugin_only_you/${endpoint}${qs ? "?" + qs : ""}`;
    const resp = await fetch(url, { credentials: "include" });
    const json = await resp.json();
    return unwrapApiPayload(json);
  }

  async apiPost(endpoint, body = {}) {
    if (bridge && typeof bridge.apiPost === "function") {
      const res = await bridge.apiPost(endpoint, body);
      return unwrapApiPayload(res);
    }
    const url = `/api/plug/astrbot_plugin_only_you/${endpoint}`;
    const resp = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "include",
      body: JSON.stringify(body),
    });
    const json = await resp.json();
    return unwrapApiPayload(json);
  }

  showToast(message, isError = false) {
    const container = document.getElementById("toast-container");
    if (!container) return;
    const item = document.createElement("div");
    item.className = "toast" + (isError ? " error" : "");
    item.textContent = message;
    container.appendChild(item);
    setTimeout(() => {
      item.remove();
    }, 3200);
  }

  applyTheme(isDark) {
    document.documentElement.classList.toggle("dark", !!isDark);
    document.documentElement.setAttribute("data-theme", isDark ? "dark" : "light");
  }

  toggleTheme() {
    const isDark = !document.documentElement.classList.contains("dark");
    this.applyTheme(isDark);
  }

  async init() {
    try {
      // 1. Handshake with AstrBot Dashboard and sync light/dark theme
      if (bridge && typeof bridge.ready === "function") {
        const ctx = await Promise.race([
          bridge.ready(),
          new Promise((resolve) => setTimeout(() => resolve(bridge.getContext?.() || {}), 1200)),
        ]);
        if (ctx && typeof ctx.isDark === "boolean") {
          this.applyTheme(ctx.isDark);
        }
        if (typeof bridge.onContext === "function") {
          bridge.onContext((nextCtx) => {
            if (nextCtx && typeof nextCtx.isDark === "boolean") {
              this.applyTheme(nextCtx.isDark);
            }
          });
        }
      }

      // 2. Load initial summary (Souls, Native Personas, Active Schedule, Model Status)
      const initData = await this.apiGet("init_summary");
      this.souls = initData.souls || [];
      this.nativePersonas = initData.native_personas || [];
      this.umoBindings = initData.umo_bindings || {};
      this.knownUmos = initData.known_umos || [];
      this.modelStatus = initData.model_status || {};
      this.slotDefinitions = initData.slot_definitions || [];
      this.currentSoulId = initData.active_soul_id || this.souls[0]?.soul_id || "default_soul";

      this.renderGlobalSoulSelector();
      this.renderModelStatus();
      await this.loadSoulWorkspace(this.currentSoulId);
    } catch (err) {
      this.showToast("初始化控制台失败: " + err.message, true);
    }
  }

  renderGlobalSoulSelector() {
    const sel = document.getElementById("global-soul-select");
    const bindSel = document.getElementById("bind-soul-select");
    if (!sel) return;

    const optionsHtml = this.souls
      .map(
        (s) =>
          `<option value="${escHtml(s.soul_id)}" ${
            s.soul_id === this.currentSoulId ? "selected" : ""
          }>${escHtml(s.name)} (${escHtml(s.soul_id)})</option>`
      )
      .join("");
    sel.innerHTML = optionsHtml;

    if (bindSel) {
      bindSel.innerHTML =
        `<option value="">-- 取消固定绑定 (跟随平台默认) --</option>` + optionsHtml;
    }
  }

  async onSwitchSoul(soulId) {
    if (!soulId) return;
    this.currentSoulId = soulId;
    await this.loadSoulWorkspace(soulId);
  }

  async refreshCurrentWorkspace() {
    await this.loadSoulWorkspace(this.currentSoulId);
    this.showToast("已刷新当前角色工作台数据");
  }

  async loadSoulWorkspace(soulId) {
    try {
      const detail = await this.apiGet("souls/detail", { soul_id: soulId });
      this.currentSoul = detail.soul;
      this.currentSoulId = this.currentSoul.soul_id;
      this.currentSchedule = detail.schedule;
      this.umoBindings = detail.umo_bindings || {};
      this.knownUmos = detail.known_umos || [];

      this.renderGlobalSoulSelector();
      this.renderLiveStatusBanner();
      this.renderSoulStudioForm();
      this.renderUmoBindingsTable();
      this.renderScheduleTimeline();

      // Load cards, users, memories, and surfing history in parallel
      await Promise.all([
        this.loadCards(this.currentSoulId),
        this.loadUsers(),
        this.loadMemories(),
        this.loadSurfingPool(),
      ]);
    } catch (err) {
      this.showToast("加载角色数据失败: " + err.message, true);
    }
  }

  switchTab(tabId) {
    document.querySelectorAll(".nav-item").forEach((btn) => {
      btn.classList.toggle("active", btn.getAttribute("data-tab") === tabId);
    });
    document.querySelectorAll(".tab-pane").forEach((pane) => {
      pane.classList.toggle("active", pane.id === tabId);
    });
  }

  // ==========================================================================
  // Top Status Strip & Schedule Controls
  // ==========================================================================
  stateLabelMap(state) {
    if (state === "RESTING") return "💤 休息中 (RESTING)";
    if (state === "BUSY") return "🔥 繁忙中 (BUSY)";
    return "☕ 空闲中 (IDLE)";
  }

  renderLiveStatusBanner() {
    if (!this.currentSchedule) return;
    const cur = this.currentSchedule.current_slot || {};
    const run = this.currentSchedule.run || {};
    const maxMut = this.currentSchedule.max_mutations_per_day ?? 2;

    const badge = document.getElementById("live-state-badge");
    if (badge) {
      badge.className = `state-pill state-${cur.state || "IDLE"}`;
      badge.textContent = this.stateLabelMap(cur.state || "IDLE");
    }
    const titleEl = document.getElementById("live-slot-title");
    if (titleEl) {
      titleEl.textContent = `[${cur.label || ""}] ${cur.title || "日常在线"}${
        cur.mutated ? " ⚡已微调" : ""
      }`;
    }
    const armorEl = document.getElementById("live-armor-chip");
    if (armorEl) armorEl.textContent = `🛡️ 护甲: ${cur.armor ?? 45}`;
    const vigorEl = document.getElementById("live-vigor-chip");
    if (vigorEl) vigorEl.textContent = `🎲 今日基调: ${run.vigor_title || "元气满满"}`;
    const mutEl = document.getElementById("live-mutation-chip");
    if (mutEl) mutEl.textContent = `⚡ 今日微调: ${run.mutations_used ?? 0}/${maxMut}`;
  }

  // ==========================================================================
  // MODULE 1: 🗂️ Soul & Persona Studio
  // ==========================================================================
  renderSoulStudioForm() {
    const s = this.currentSoul;
    if (!s) return;
    document.getElementById("soul-id-input").value = s.soul_id || "";
    document.getElementById("soul-name-input").value = s.name || "";
    document.getElementById("soul-archetype-input").value = s.archetype || "";
    document.getElementById("soul-style-input").value = s.speaking_style || "";
    document.getElementById("soul-prompt-input").value = s.native_persona_prompt || "";
    this.renderPrefTags();
  }

  renderPrefTags() {
    const container = document.getElementById("soul-prefs-tags");
    if (!container || !this.currentSoul) return;
    const prefs = this.currentSoul.surfing_preferences || [];
    if (!prefs.length) {
      container.innerHTML = `<span class="form-hint">暂无标签，请在下方输入添加</span>`;
      return;
    }
    container.innerHTML = prefs
      .map(
        (tag, idx) =>
          `<span class="tag-pill">${escHtml(tag)} <button type="button" onclick="app.removePrefTag(${idx})">&times;</button></span>`
      )
      .join("");
  }

  addPrefTag() {
    const input = document.getElementById("new-pref-tag-input");
    if (!input || !this.currentSoul) return;
    const val = input.value.trim();
    if (!val) return;
    if (!this.currentSoul.surfing_preferences) {
      this.currentSoul.surfing_preferences = [];
    }
    if (!this.currentSoul.surfing_preferences.includes(val)) {
      this.currentSoul.surfing_preferences.push(val);
      this.renderPrefTags();
    }
    input.value = "";
  }

  removePrefTag(idx) {
    if (!this.currentSoul || !Array.isArray(this.currentSoul.surfing_preferences)) return;
    this.currentSoul.surfing_preferences.splice(idx, 1);
    this.renderPrefTags();
  }

  async saveSoulProfile() {
    if (!this.currentSoul) return;
    try {
      const payload = {
        soul_id: this.currentSoul.soul_id,
        name: document.getElementById("soul-name-input").value.trim(),
        archetype: document.getElementById("soul-archetype-input").value.trim(),
        speaking_style: document.getElementById("soul-style-input").value.trim(),
        native_persona_prompt: document.getElementById("soul-prompt-input").value.trim(),
        surfing_preferences: this.currentSoul.surfing_preferences || [],
      };
      const res = await this.apiPost("souls/save", payload);
      this.currentSoul = res.soul;
      this.souls = res.souls || this.souls;
      this.renderGlobalSoulSelector();
      this.renderSoulStudioForm();
      this.showToast(`已保存角色档案：${this.currentSoul.name} (${this.currentSoul.soul_id})`);
    } catch (err) {
      this.showToast("保存角色失败: " + err.message, true);
    }
  }

  renderUmoBindingsTable() {
    const tbody = document.getElementById("umo-bindings-tbody");
    const surfUmoSelect = document.getElementById("surf-share-umo-select");
    if (!tbody) return;

    const list = this.knownUmos || [];
    if (surfUmoSelect) {
      surfUmoSelect.innerHTML =
        `<option value="">📤 默认分享目标 (核心用户/当前活跃会话)</option>` +
        list
          .map(
            (u) =>
              `<option value="${escHtml(u.umo)}">${escHtml(u.umo)}${
                u.is_l3_private ? " (★L3私聊)" : ""
              }</option>`
          )
          .join("");
    }

    if (!list.length) {
      tbody.innerHTML = `<tr><td colspan="4" class="form-hint">当前尚无活跃会话记录，可在上方手动输入会话 UMO 进行显式绑定。</td></tr>`;
      return;
    }

    tbody.innerHTML = list
      .map(
        (item) => `
        <tr>
          <td><code>${escHtml(item.umo)}</code> ${
          item.is_l3_private ? '<span class="tier-badge tier-3">★L3私聊</span>' : ""
        }</td>
          <td>${
            item.bound_soul_id
              ? `<strong>${escHtml(item.bound_soul_id)}</strong>`
              : '<span class="form-hint">跟随平台默认 Persona</span>'
          }</td>
          <td>${item.window_messages ?? 0} 条</td>
          <td>
            <button class="btn btn-sm" onclick="app.quickBindUmo('${escHtml(item.umo)}', '${escHtml(
          this.currentSoulId
        )}')">绑定当前Soul</button>
            ${
              item.bound_soul_id
                ? `<button class="btn btn-danger btn-sm" onclick="app.quickBindUmo('${escHtml(
                    item.umo
                  )}', '')">解除绑定</button>`
                : ""
            }
          </td>
        </tr>
      `
      )
      .join("");
  }

  async bindUmoToSoul() {
    const umo = document.getElementById("bind-umo-input").value.trim();
    const soulId = document.getElementById("bind-soul-select").value.trim();
    if (!umo) {
      this.showToast("请输入要绑定的会话 UMO", true);
      return;
    }
    await this.quickBindUmo(umo, soulId);
    document.getElementById("bind-umo-input").value = "";
  }

  async quickBindUmo(umo, soulId) {
    try {
      const res = await this.apiPost("souls/save", {
        soul_id: this.currentSoulId,
        umo_binding: { umo, soul_id: soulId },
      });
      this.umoBindings = res.umo_bindings || {};
      this.knownUmos = res.known_umos || [];
      this.renderUmoBindingsTable();
      this.showToast(soulId ? `已将会话 ${umo} 绑定至 ${soulId}` : `已解除会话 ${umo} 的固定绑定`);
    } catch (err) {
      this.showToast("绑定会话失败: " + err.message, true);
    }
  }

  renderModelStatus() {
    const grid = document.getElementById("model-status-grid");
    if (!grid) return;
    const ms = this.modelStatus || {};
    const items = [
      { label: "① 对话主模型 (Main LLM)", val: ms.main_llm || "继承平台配置" },
      { label: "② 小脑轻量模型 (Small LLM)", val: ms.small_llm || "回退至主模型" },
      { label: "③ 多模态视觉模型 (Vision)", val: ms.vision_llm || "继承平台识图/主模型" },
      { label: "④ 向量检索 (Embedding)", val: ms.embedding || "关键词+新鲜度混合检索" },
    ];
    grid.innerHTML = items
      .map(
        (it) => `
        <div class="stat-box">
          <div class="stat-label">${escHtml(it.label)}</div>
          <div style="font-weight:600; margin-top:4px; font-size:13px;">${escHtml(it.val)}</div>
        </div>`
      )
      .join("");
  }

  // Import Native Persona (Exact signature from docs/WEBUI_DESIGN.md Section 3.2)
  async importNativePersona(personaName, targetSoulId, autoGenerateCards = true, nativePrompt = "") {
    return await this.apiPost("souls/import_persona", {
      persona_name: personaName,
      target_soul_id: targetSoulId,
      auto_generate_cards: autoGenerateCards,
      native_prompt: nativePrompt,
    });
  }

  openImportPersonaModal() {
    const sel = document.getElementById("import-persona-select");
    if (sel) {
      sel.innerHTML = this.nativePersonas
        .map(
          (p) =>
            `<option value="${escHtml(p.persona_name)}">${escHtml(p.persona_name)}</option>`
        )
        .join("");
      const first = this.nativePersonas[0];
      if (first) {
        document.getElementById("import-target-soul-id").value = first.persona_name;
        document.getElementById("import-persona-prompt").value = first.prompt || "";
      }
    }
    this.openModal("modal-import-persona");
  }

  onSelectNativePersona(personaName) {
    const found = this.nativePersonas.find((p) => p.persona_name === personaName);
    document.getElementById("import-target-soul-id").value = personaName;
    if (found) {
      document.getElementById("import-persona-prompt").value = found.prompt || "";
    }
  }

  async submitImportPersona() {
    const personaName = document.getElementById("import-persona-select").value.trim() || "default_soul";
    const targetSoulId = document.getElementById("import-target-soul-id").value.trim() || personaName;
    const nativePrompt = document.getElementById("import-persona-prompt").value.trim();
    const mode = document.querySelector('input[name="import-mode"]:checked')?.value || "ai";
    const autoGen = mode === "ai";

    try {
      this.showToast(autoGen ? "正在导入并调用 AI 扩写角色卡与三态卡池..." : "正在直接继承导入人格...");
      const res = await this.importNativePersona(personaName, targetSoulId, autoGen, nativePrompt);
      this.closeModal("modal-import-persona");
      this.souls = res.souls || this.souls;
      this.currentSoulId = res.soul.soul_id;
      await this.loadSoulWorkspace(this.currentSoulId);
      this.showToast(`成功导入角色：${res.soul.name} (${res.soul.soul_id})`);
    } catch (err) {
      this.showToast("导入人格失败: " + err.message, true);
    }
  }

  // Rename Soul ID (Exact signature from docs/WEBUI_DESIGN.md Section 3.2)
  async renameSoulId(oldSoulId, newSoulId) {
    return await this.apiPost("souls/rename", {
      old_soul_id: oldSoulId,
      new_soul_id: newSoulId,
    });
  }

  openRenameSoulModal() {
    this.soulIdModalMode = "rename";
    document.getElementById("modal-soul-id-title").textContent = `✏️ 修改角色 Soul ID (当前: ${this.currentSoulId})`;
    document.getElementById("modal-soul-id-label").textContent = "输入新的 Soul ID";
    document.getElementById("modal-soul-id-input").value = this.currentSoulId;
    document.getElementById("modal-soul-id-hint").textContent =
      "系统将在 SQLite 事务中级联更新该角色关联的所有日程、卡片、会话绑定与记忆外键。";
    this.openModal("modal-soul-id");
  }

  openCreateSoulModal() {
    this.soulIdModalMode = "create";
    document.getElementById("modal-soul-id-title").textContent = "➕ 新建空白角色 (Soul)";
    document.getElementById("modal-soul-id-label").textContent = "新角色 Soul ID";
    document.getElementById("modal-soul-id-input").value = `soul_${Math.floor(Date.now() / 1000) % 10000}`;
    document.getElementById("modal-soul-id-hint").textContent = "创建后可在角色档案编辑器中自定义设定与专属三态行动卡。";
    this.openModal("modal-soul-id");
  }

  async submitSoulIdModal() {
    const targetId = document.getElementById("modal-soul-id-input").value.trim();
    if (!targetId) {
      this.showToast("Soul ID 不能为空", true);
      return;
    }
    try {
      if (this.soulIdModalMode === "rename") {
        const res = await this.renameSoulId(this.currentSoulId, targetId);
        this.souls = res.souls || this.souls;
        this.currentSoulId = res.soul.soul_id;
        this.closeModal("modal-soul-id");
        await this.loadSoulWorkspace(this.currentSoulId);
        this.showToast(`已成功修改 Soul ID 为 ${this.currentSoulId}`);
      } else {
        const res = await this.apiPost("souls/save", {
          soul_id: targetId,
          name: targetId,
          archetype: "有自己生活节律与喜怒哀乐的数字网友",
          speaking_style: "口语化短句，自然随性",
          surfing_preferences: ["搞笑整活", "萌宠日常", "美食探店"],
        });
        this.souls = res.souls || this.souls;
        this.currentSoulId = res.soul.soul_id;
        this.closeModal("modal-soul-id");
        await this.loadSoulWorkspace(this.currentSoulId);
        this.showToast(`已创建新角色 ${this.currentSoulId}`);
      }
    } catch (err) {
      this.showToast("操作失败: " + err.message, true);
    }
  }

  async deleteCurrentSoul() {
    if (this.souls.length <= 1) {
      this.showToast("至少需要保留一个默认角色，无法删除最后一个角色", true);
      return;
    }
    if (!confirm(`确定要删除角色 [${this.currentSoulId}] 及其关联日程和记忆吗？`)) {
      return;
    }
    try {
      const res = await this.apiPost("souls/delete", { soul_id: this.currentSoulId });
      this.souls = res.souls || [];
      this.currentSoulId = res.active_soul_id || this.souls[0]?.soul_id || "default_soul";
      await this.loadSoulWorkspace(this.currentSoulId);
      this.showToast("已删除角色");
    } catch (err) {
      this.showToast("删除角色失败: " + err.message, true);
    }
  }

  // ==========================================================================
  // MODULE 2: 🃏 Roguelite Schedule & Action Card Deck Editor
  // ==========================================================================
  renderScheduleTimeline() {
    const grid = document.getElementById("schedule-timeline-grid");
    const encList = document.getElementById("today-encounters-list");
    const sub = document.getElementById("schedule-subtitle");
    if (!grid || !this.currentSchedule) return;

    const run = this.currentSchedule.run || {};
    const curSlotKey = this.currentSchedule.current_slot?.slot_key || "";
    const slots = run.slots || [];

    if (sub) {
      sub.textContent = `日期: ${run.date_str || ""} | 今日基调: 【${run.vigor_title || ""}】${
        run.vigor_desc || ""
      } | 点击任意时间段卡片可手动干预该槽位状态`;
    }

    grid.innerHTML = slots
      .map((s) => {
        const isCurrent = s.slot_key === curSlotKey;
        const timeStr = `${String(s.start_hour).padStart(2, "0")}:00-${String(s.end_hour).padStart(
          2,
          "0"
        )}:00`;
        return `
        <div class="slot-card ${isCurrent ? "current-slot" : ""}" onclick="app.openForceStateModal('${escHtml(
          s.slot_key
        )}', '${escHtml(s.state)}', '${escHtml(s.title)}', ${s.armor ?? 45})">
          <div class="slot-time">
            <span>${escHtml(timeStr)} · ${escHtml(s.label)}</span>
            ${isCurrent ? '<span class="tier-badge tier-3">当前时段</span>' : ""}
          </div>
          <div>
            <span class="state-pill state-${escHtml(s.state)}">${this.stateLabelMap(s.state)}</span>
            ${s.mutated ? '<span class="tier-badge tier-2">⚡已微调</span>' : ""}
            ${s.delay_minutes ? `<span class="meta-chip">+${s.delay_minutes}m</span>` : ""}
          </div>
          <div class="slot-title">${escHtml(s.title)}</div>
          <div class="armor-bar-wrap">
            <div class="armor-bar-label">
              <span>🛡️ 定力护甲</span>
              <strong>${s.armor ?? 45}/100</strong>
            </div>
            <div class="progress-track">
              <div class="progress-fill" style="width:${Math.min(100, Math.max(0, s.armor ?? 45))}%;"></div>
            </div>
          </div>
        </div>`;
      })
      .join("");

    if (encList) {
      const encounters = run.encounters || [];
      if (!encounters.length) {
        encList.innerHTML = `<div class="form-hint">今日暂无抽取的奇遇谈资卡</div>`;
      } else {
        encList.innerHTML = encounters
          .map(
            (e) => `
            <div class="encounter-item">
              <span>🃏 ${escHtml(e.content)}</span>
              <span class="tier-badge ${e.used ? "tier-1" : "tier-2"}">${
              e.used ? "已聊消耗" : "未聊待触发"
            }</span>
            </div>`
          )
          .join("");
      }
    }
  }

  async rerollTodaySchedule() {
    try {
      const res = await this.apiPost("schedule/reroll", { soul_id: this.currentSoulId });
      this.currentSchedule = res.schedule;
      this.renderLiveStatusBanner();
      this.renderScheduleTimeline();
      this.showToast("🎲 已重新抽取今日 Roguelite 日程与奇遇谈资卡！");
    } catch (err) {
      this.showToast("重抽日程失败: " + err.message, true);
    }
  }

  openForceStateModal(slotKey = "", state = "", title = "", armor = 40) {
    const cur = this.currentSchedule?.current_slot || {};
    document.getElementById("force-slot-select").value = slotKey || "";
    const targetState = state || cur.state || "IDLE";
    document.querySelectorAll('input[name="force-state-radio"]').forEach((r) => {
      r.checked = r.value === targetState;
    });
    document.getElementById("force-title-input").value = title || cur.title || "";
    const targetArmor = armor ?? cur.armor ?? 40;
    document.getElementById("force-armor-input").value = targetArmor;
    document.getElementById("force-armor-val").textContent = targetArmor;
    this.openModal("modal-force-state");
  }

  async submitForceState() {
    const slotKey = document.getElementById("force-slot-select").value;
    const newState =
      document.querySelector('input[name="force-state-radio"]:checked')?.value || "IDLE";
    const newTitle = document.getElementById("force-title-input").value.trim();
    const armor = parseInt(document.getElementById("force-armor-input").value, 10);

    try {
      const res = await this.apiPost("schedule/force_state", {
        soul_id: this.currentSoulId,
        slot_key: slotKey || null,
        new_state: newState,
        new_title: newTitle || null,
        armor: isNaN(armor) ? null : armor,
      });
      this.currentSchedule = res.schedule;
      this.closeModal("modal-force-state");
      this.renderLiveStatusBanner();
      this.renderScheduleTimeline();
      this.showToast(`已将时段切换为 ${this.stateLabelMap(newState)}`);
    } catch (err) {
      this.showToast("切换状态失败: " + err.message, true);
    }
  }

  async loadCards(soulId) {
    const data = await this.apiGet("cards/list", { soul_id: soulId || this.currentSoulId });
    this.customCards = data.custom_cards || [];
    this.generalCards = data.general_cards || [];
    this.customEncounters = data.custom_encounters || [];
    this.generalEncounters = data.general_encounters || [];
    this.renderCardsDeckGrid();
  }

  setCardFilter(filter) {
    this.cardFilter = filter;
    document.querySelectorAll("#card-filter-tabs .pill-tab").forEach((btn) => {
      btn.classList.toggle("active", btn.getAttribute("data-filter") === filter);
    });
    this.renderCardsDeckGrid();
  }

  renderCardsDeckGrid() {
    const grid = document.getElementById("deck-cards-grid");
    const countHint = document.getElementById("card-count-hint");
    if (!grid) return;

    if (this.cardFilter === "ENCOUNTER") {
      const allEnc = [...this.customEncounters, ...this.generalEncounters];
      if (countHint) countHint.textContent = `共 ${allEnc.length} 张奇遇谈资卡`;
      grid.innerHTML = allEnc
        .map(
          (e, idx) => `
          <div class="deck-card">
            <div class="deck-card-header">
              <span class="tier-badge ${e.scope === "soul" ? "tier-3" : "tier-1"}">${
            e.scope === "soul" ? "🌟 角色专属奇遇" : "🌐 全局通用奇遇"
          }</span>
            </div>
            <div class="deck-card-title">🃏 ${escHtml(e.content)}</div>
            <div class="btn-group" style="justify-content:flex-end;">
              <button class="btn btn-sm" onclick="app.editEncounterCard(${idx}, '${escHtml(
            e.scope
          )}')">✏️ 编辑</button>
              <button class="btn btn-danger btn-sm" onclick="app.deleteEncounterCard(${idx}, '${escHtml(
            e.scope
          )}')">🗑️ 删除</button>
            </div>
          </div>`
        )
        .join("");
      return;
    }

    const allCards = [...this.customCards, ...this.generalCards].filter((c) =>
      this.cardFilter === "ALL" ? true : c.state === this.cardFilter
    );
    if (countHint) countHint.textContent = `共 ${allCards.length} 张三态行动卡`;

    const slotLabelMap = {
      morning: "早间",
      forenoon: "上午",
      noon: "午间",
      afternoon: "下午",
      evening: "晚间",
      night: "深夜",
    };

    grid.innerHTML = allCards
      .map(
        (c) => `
        <div class="deck-card">
          <div>
            <div class="deck-card-header">
              <span class="state-pill state-${escHtml(c.state)}">${this.stateLabelMap(
          c.state
        )}</span>
              <span class="tier-badge ${c.scope === "soul" ? "tier-3" : "tier-1"}">${
          c.scope === "soul" ? "专属卡" : "通用卡"
        }</span>
            </div>
            <div class="deck-card-title" style="margin-top:8px;">${escHtml(c.title)}</div>
          </div>
          <div>
            <div class="slot-badges" style="margin-bottom:8px;">
              ${(c.valid_slots || [])
                .map(
                  (sk) =>
                    `<span class="slot-mini-badge">${escHtml(slotLabelMap[sk] || sk)}</span>`
                )
                .join("")}
            </div>
            <div class="armor-bar-wrap">
              <div class="armor-bar-label">
                <span>🛡️ 护甲: ${c.armor ?? 45}</span>
                <span>⚖️ 权重: ${c.base_weight ?? 50}</span>
              </div>
              <div class="progress-track">
                <div class="progress-fill" style="width:${Math.min(
                  100,
                  Math.max(0, c.armor ?? 45)
                )}%;"></div>
              </div>
            </div>
            <div class="btn-group" style="justify-content:flex-end; margin-top:10px;">
              <button class="btn btn-sm" onclick="app.editActionCard('${escHtml(
                c.card_id
              )}', '${escHtml(c.scope)}')">✏️ 编辑</button>
              <button class="btn btn-danger btn-sm" onclick="app.deleteActionCard('${escHtml(
                c.card_id
              )}', '${escHtml(c.scope)}')">🗑️</button>
            </div>
          </div>
        </div>`
      )
      .join("");
  }

  // Save or Create 3-State Action Card (Exact signature from docs/WEBUI_DESIGN.md Section 3.2)
  async saveActionCard(soulId, cardData, scope = "soul") {
    return await this.apiPost("cards/save", {
      soul_id: soulId,
      card_type: "action",
      scope,
      card: cardData,
    });
  }

  updateCardArmorHint(val) {
    const v = parseInt(val, 10);
    document.getElementById("card-armor-label").textContent = v;
    const hint = document.getElementById("card-armor-hint");
    if (!hint) return;
    if (v <= 35) hint.textContent = "<=35 极易被群友/消息拉去摸鱼";
    else if (v >= 75) hint.textContent = ">=75 雷打不动，仅★L3或暴击事件可破";
    else hint.textContent = "正常定力门槛";
  }

  openActionCardModal(card = null, scope = "soul") {
    document.getElementById("card-id-input").value = card ? card.card_id : "";
    document.getElementById("card-title-input").value = card ? card.title : "";
    const targetState = card ? card.state : "IDLE";
    document.querySelectorAll('input[name="card-state-radio"]').forEach((r) => {
      r.checked = r.value === targetState;
    });
    const validSlots = card ? card.valid_slots || [] : ["noon", "afternoon", "evening"];
    document.querySelectorAll("#card-slots-checkboxes input[type='checkbox']").forEach((cb) => {
      cb.checked = validSlots.includes(cb.value);
    });
    const armor = card ? card.armor ?? 45 : 45;
    document.getElementById("card-armor-slider").value = armor;
    this.updateCardArmorHint(armor);
    const weight = card ? card.base_weight ?? 50 : 50;
    document.getElementById("card-weight-slider").value = weight;
    document.getElementById("card-weight-label").textContent = weight;
    document.getElementById("card-scope-select").value = scope || "soul";
    this.openModal("modal-action-card");
  }

  editActionCard(cardId, scope) {
    const pool = scope === "global" ? this.generalCards : this.customCards;
    const found = pool.find((c) => c.card_id === cardId);
    if (found) this.openActionCardModal(found, scope);
  }

  async submitSaveActionCard() {
    const title = document.getElementById("card-title-input").value.trim();
    if (!title) {
      this.showToast("请填写卡片名称", true);
      return;
    }
    const cardId =
      document.getElementById("card-id-input").value.trim() ||
      `evt_${Date.now() % 100000}`;
    const state =
      document.querySelector('input[name="card-state-radio"]:checked')?.value || "IDLE";
    const validSlots = [];
    document.querySelectorAll("#card-slots-checkboxes input[type='checkbox']:checked").forEach((cb) => {
      validSlots.push(cb.value);
    });
    if (!validSlots.length) validSlots.push("noon", "evening");

    const armor = parseInt(document.getElementById("card-armor-slider").value, 10) || 45;
    const baseWeight = parseInt(document.getElementById("card-weight-slider").value, 10) || 50;
    const scope = document.getElementById("card-scope-select").value || "soul";

    try {
      const res = await this.saveActionCard(
        this.currentSoulId,
        {
          card_id: cardId,
          title,
          state,
          valid_slots: validSlots,
          armor,
          base_weight: baseWeight,
        },
        scope
      );
      this.customCards = res.custom_cards || [];
      this.generalCards = res.general_cards || [];
      this.closeModal("modal-action-card");
      this.renderCardsDeckGrid();
      this.showToast(`已保存行动卡：${title}`);
    } catch (err) {
      this.showToast("保存卡片失败: " + err.message, true);
    }
  }

  async deleteActionCard(cardId, scope) {
    try {
      const res = await this.apiPost("cards/delete", {
        soul_id: this.currentSoulId,
        card_type: "action",
        scope,
        card_id: cardId,
      });
      this.customCards = res.custom_cards || [];
      this.generalCards = res.general_cards || [];
      this.renderCardsDeckGrid();
      this.showToast("已删除该行动卡");
    } catch (err) {
      this.showToast("删除卡片失败: " + err.message, true);
    }
  }

  openEncounterCardModal(content = "", scope = "soul") {
    document.getElementById("encounter-old-content").value = content;
    document.getElementById("encounter-content-input").value = content;
    document.getElementById("encounter-scope-select").value = scope;
    this.openModal("modal-encounter-card");
  }

  editEncounterCard(idx, scope) {
    const allEnc = [...this.customEncounters, ...this.generalEncounters];
    const item = allEnc[idx];
    if (item) this.openEncounterCardModal(item.content, item.scope);
  }

  async submitSaveEncounterCard() {
    const content = document.getElementById("encounter-content-input").value.trim();
    const oldContent = document.getElementById("encounter-old-content").value.trim();
    const scope = document.getElementById("encounter-scope-select").value || "soul";
    if (!content) {
      this.showToast("奇遇内容不能为空", true);
      return;
    }
    try {
      const res = await this.apiPost("cards/save", {
        soul_id: this.currentSoulId,
        card_type: "encounter",
        scope,
        content,
        old_content: oldContent,
      });
      this.customEncounters = res.custom_encounters || [];
      this.generalEncounters = res.general_encounters || [];
      this.closeModal("modal-encounter-card");
      this.renderCardsDeckGrid();
      this.showToast("已保存奇遇谈资卡");
    } catch (err) {
      this.showToast("保存奇遇卡失败: " + err.message, true);
    }
  }

  async deleteEncounterCard(idx, scope) {
    const allEnc = [...this.customEncounters, ...this.generalEncounters];
    const item = allEnc[idx];
    if (!item) return;
    try {
      const res = await this.apiPost("cards/delete", {
        soul_id: this.currentSoulId,
        card_type: "encounter",
        scope,
        content: item.content,
      });
      this.customEncounters = res.custom_encounters || [];
      this.generalEncounters = res.general_encounters || [];
      this.renderCardsDeckGrid();
      this.showToast("已删除奇遇谈资卡");
    } catch (err) {
      this.showToast("删除奇遇卡失败: " + err.message, true);
    }
  }

  async aiGenerateCards() {
    try {
      this.showToast("🤖 正在根据当前角色人设生成专属三态行动卡与奇遇谈资卡...");
      const res = await this.apiPost("cards/ai_generate", {
        soul_id: this.currentSoulId,
        auto_save: true,
      });
      this.customCards = res.custom_cards || [];
      this.generalCards = res.general_cards || [];
      this.customEncounters = res.custom_encounters || [];
      this.generalEncounters = res.general_encounters || [];
      this.renderCardsDeckGrid();
      this.showToast(
        `✨ 已生成并入库 ${(res.generated_cards || []).length} 张行动卡与 ${
          (res.generated_encounters || []).length
        } 张奇遇卡！`
      );
    } catch (err) {
      this.showToast("AI 造卡失败: " + err.message, true);
    }
  }

  // ==========================================================================
  // MODULE 3: 💖 Three-Tier Relationships & Cross-Platform Identity Merger
  // ==========================================================================
  async loadUsers() {
    const data = await this.apiGet("users/list", { tier: this.userTierFilter });
    this.users = data.users || [];
    this.userCounts = data.counts || { l3_count: 0, l2_count: 0, l1_count: 0, total: 0 };
    this.renderUsersModule();
  }

  setUserFilter(tierStr) {
    this.userTierFilter = tierStr;
    document.querySelectorAll("#user-filter-tabs .pill-tab").forEach((btn) => {
      btn.classList.toggle("active", btn.getAttribute("data-tier") === tierStr);
    });
    this.loadUsers();
  }

  renderUsersModule() {
    const statsEl = document.getElementById("user-stats-grid");
    if (statsEl) {
      statsEl.innerHTML = `
        <div class="stat-box"><div class="stat-value">${this.userCounts.l3_count ?? 0}</div><div class="stat-label">★ L3 核心用户 (OnlyYou)</div></div>
        <div class="stat-box"><div class="stat-value">${this.userCounts.l2_count ?? 0}</div><div class="stat-label">◆ L2 重要熟人 (繁忙可回)</div></div>
        <div class="stat-box"><div class="stat-value">${this.userCounts.l1_count ?? 0}</div><div class="stat-label">○ L1 普通群友</div></div>
        <div class="stat-box"><div class="stat-value">${this.userCounts.total ?? 0}</div><div class="stat-label">已记录总用户数</div></div>
      `;
    }

    const tbody = document.getElementById("users-tbody");
    const pSel = document.getElementById("merge-primary-select");
    const sSel = document.getElementById("merge-secondary-select");

    if (pSel && sSel) {
      const uOpts = this.users
        .map(
          (u) =>
            `<option value="${escHtml(u.global_uid)}">${escHtml(u.display_name)} (${escHtml(
              u.global_uid
            )} · ${escHtml(u.tier_label)})</option>`
        )
        .join("");
      pSel.innerHTML = uOpts || `<option value="">暂无可合并用户</option>`;
      sSel.innerHTML = uOpts || `<option value="">暂无可合并用户</option>`;
      if (this.users.length >= 2) {
        sSel.selectedIndex = 1;
      }
    }

    if (!tbody) return;
    if (!this.users.length) {
      tbody.innerHTML = `<tr><td colspan="6" class="form-hint">当前筛选条件下暂无用户记录，可点击右上角“➕ 新增 / 指定用户关系”手动添加。</td></tr>`;
      return;
    }

    tbody.innerHTML = this.users
      .map(
        (u) => `
        <tr>
          <td>
            <strong>${escHtml(u.global_uid)}</strong>
            ${
              u.platform_ids && u.platform_ids.length > 1
                ? `<div class="form-hint">已合并: ${escHtml(u.platform_ids.join(", "))}</div>`
                : ""
            }
          </td>
          <td>
            ${escHtml(u.display_name)}
            ${u.nickname ? `<span class="tag-pill">外号: ${escHtml(u.nickname)}</span>` : ""}
          </td>
          <td>
            <span class="tier-badge tier-${u.tier}">${escHtml(u.tier_label)}</span>
          </td>
          <td>❤️ ${u.affinity ?? 0}</td>
          <td>💬 ${u.interaction_count ?? 0} 次</td>
          <td>
            <div class="btn-group">
              <button class="btn btn-sm" onclick="app.quickSetUserTier('${escHtml(
                u.global_uid
              )}', 3)" title="提拔为★L3核心用户">★L3</button>
              <button class="btn btn-sm" onclick="app.quickSetUserTier('${escHtml(
                u.global_uid
              )}', 2)" title="设为◆L2重要熟人">◆L2</button>
              <button class="btn btn-sm" onclick="app.quickSetUserTier('${escHtml(
                u.global_uid
              )}', 1)" title="设为○L1普通用户">L1</button>
              <button class="btn btn-sm" onclick="app.editUser('${escHtml(
                u.global_uid
              )}')">✏️ 编辑</button>
              <button class="btn btn-danger btn-sm" onclick="app.deleteUser('${escHtml(
                u.global_uid
              )}')">🗑️</button>
            </div>
          </td>
        </tr>`
      )
      .join("");
  }

  async quickSetUserTier(uid, tier) {
    try {
      await this.apiPost("users/update", {
        action: "update",
        global_uid: uid,
        tier,
      });
      await this.loadUsers();
      this.showToast(`已将用户 ${uid} 关系等级更新为 L${tier}`);
    } catch (err) {
      this.showToast("修改用户等级失败: " + err.message, true);
    }
  }

  openUserModal(user = null) {
    document.getElementById("user-uid-input").value = user ? user.global_uid : "";
    document.getElementById("user-name-input").value = user ? user.display_name : "";
    document.getElementById("user-nickname-input").value = user ? user.nickname || "" : "";
    document.getElementById("user-tier-select").value = user ? String(user.tier) : "2";
    const aff = user ? Math.round(user.affinity ?? 20) : 50;
    document.getElementById("user-affinity-slider").value = aff;
    document.getElementById("user-affinity-label").textContent = aff;
    this.openModal("modal-user-edit");
  }

  editUser(uid) {
    const found = this.users.find((u) => u.global_uid === uid);
    if (found) this.openUserModal(found);
  }

  async submitSaveUser() {
    const uid = document.getElementById("user-uid-input").value.trim();
    if (!uid) {
      this.showToast("用户账号 ID 不能为空", true);
      return;
    }
    const displayName = document.getElementById("user-name-input").value.trim();
    const nickname = document.getElementById("user-nickname-input").value.trim();
    const tier = parseInt(document.getElementById("user-tier-select").value, 10) || 1;
    const affinity = parseFloat(document.getElementById("user-affinity-slider").value) || 0;

    try {
      await this.apiPost("users/update", {
        action: "update",
        global_uid: uid,
        display_name: displayName || uid,
        nickname,
        tier,
        affinity,
      });
      this.closeModal("modal-user-edit");
      await this.loadUsers();
      this.showToast(`已保存用户 ${uid} 的关系设置`);
    } catch (err) {
      this.showToast("保存用户失败: " + err.message, true);
    }
  }

  async deleteUser(uid) {
    try {
      await this.apiPost("users/update", { action: "delete", global_uid: uid });
      await this.loadUsers();
      this.showToast(`已删除用户 ${uid}`);
    } catch (err) {
      this.showToast("删除用户失败: " + err.message, true);
    }
  }

  async mergeCrossPlatformUsers() {
    const primaryUid = document.getElementById("merge-primary-select").value;
    const secondaryUid = document.getElementById("merge-secondary-select").value;
    if (!primaryUid || !secondaryUid || primaryUid === secondaryUid) {
      this.showToast("请选择两个不同的账号进行合并", true);
      return;
    }
    try {
      await this.apiPost("users/update", {
        action: "merge",
        primary_uid: primaryUid,
        secondary_uid: secondaryUid,
      });
      await this.loadUsers();
      await this.loadMemories();
      this.showToast(`🔗 已成功将 ${secondaryUid} 合并至主身份 ${primaryUid}`);
    } catch (err) {
      this.showToast("合并账号失败: " + err.message, true);
    }
  }

  // ==========================================================================
  // MODULE 4: 🧠 Tiered Memory Vault & Forgetting GC
  // ==========================================================================
  async loadMemories() {
    const keyword = document.getElementById("memory-search-input")?.value.trim() || "";
    const data = await this.apiGet("memories/list", {
      soul_id: this.currentSoulId,
      memory_tier: this.memoryTierFilter,
      keyword,
    });
    this.memories = data.memories || [];
    this.memoryStats = data.stats || {};
    this.renderMemoriesModule();
  }

  setMemoryTierFilter(mtier) {
    this.memoryTierFilter = mtier;
    document.querySelectorAll("#memory-filter-tabs .pill-tab").forEach((btn) => {
      btn.classList.toggle("active", btn.getAttribute("data-mtier") === mtier);
    });
    this.loadMemories();
  }

  renderMemoriesModule() {
    const statsEl = document.getElementById("memory-stats-grid");
    const st = this.memoryStats || {};
    if (statsEl) {
      statsEl.innerHTML = `
        <div class="stat-box"><div class="stat-value">${st.tier3_count ?? 0}</div><div class="stat-label">💎 Tier 3 核心永久记忆</div></div>
        <div class="stat-box"><div class="stat-value">${st.tier2_count ?? 0}</div><div class="stat-label">📅 Tier 2 近期情节记忆</div></div>
        <div class="stat-box"><div class="stat-value">${st.tier1_count ?? 0}</div><div class="stat-label">🕒 Tier 1 瞬时短时记忆</div></div>
        <div class="stat-box"><div class="stat-value">&le;${st.max_injected_memories ?? 6}条 / ${
        st.max_memory_chars_budget ?? 400
      }字</div><div class="stat-label">🛡️ 单次注入硬性字符预算</div></div>
      `;
    }

    const tbody = document.getElementById("memories-tbody");
    if (!tbody) return;
    if (!this.memories.length) {
      tbody.innerHTML = `<tr><td colspan="6" class="form-hint">当前暂无匹配的记忆条目，可点击右上角“➕ 手动新增记忆”添加。</td></tr>`;
      return;
    }

    const tierLabel = { 3: "💎 T3核心", 2: "📅 T2近期", 1: "🕒 T1瞬时" };
    tbody.innerHTML = this.memories
      .map(
        (m) => `
        <tr>
          <td>
            <strong>#${m.id}</strong>
            <div><span class="tier-badge tier-${m.memory_tier}">${
          tierLabel[m.memory_tier] || "T2近期"
        }</span></div>
          </td>
          <td>
            <div style="font-weight:500;">${escHtml(m.content)}</div>
            <div class="slot-badges" style="margin-top:4px;">
              ${(m.keywords || "")
                .split(",")
                .filter(Boolean)
                .map((k) => `<span class="slot-mini-badge">#${escHtml(k.trim())}</span>`)
                .join("")}
            </div>
          </td>
          <td>${m.user_id ? `<code>${escHtml(m.user_id)}</code>` : '<span class="form-hint">Bot自身/公共</span>'}</td>
          <td>⭐ ${m.importance}/10</td>
          <td style="min-width:140px;">
            <div class="armor-bar-label">
              <span>活力: ${m.vitality ?? 100}%</span>
              <span>召回: ${m.access_count ?? 0}次</span>
            </div>
            <div class="progress-track">
              <div class="progress-fill" style="width:${Math.min(
                100,
                Math.max(0, m.vitality ?? 100)
              )}%;"></div>
            </div>
          </td>
          <td>
            <div class="btn-group">
              ${
                m.memory_tier < 3
                  ? `<button class="btn btn-sm" onclick="app.pinMemoryToCore(${m.id})" title="一键锁定为永久核心记忆">📌 钉选核心</button>`
                  : ""
              }
              <button class="btn btn-sm" onclick="app.editMemory(${m.id})">✏️</button>
              <button class="btn btn-danger btn-sm" onclick="app.deleteMemory(${m.id})">🗑️</button>
            </div>
          </td>
        </tr>`
      )
      .join("");
  }

  openMemoryModal(mem = null) {
    document.getElementById("memory-id-input").value = mem ? mem.id : "";
    document.getElementById("memory-content-input").value = mem ? mem.content : "";
    document.getElementById("memory-keywords-input").value = mem ? mem.keywords : "";
    document.getElementById("memory-user-input").value = mem ? mem.user_id || "" : "";
    document.getElementById("memory-tier-select").value = mem ? String(mem.memory_tier) : "3";
    const imp = mem ? mem.importance : 8;
    document.getElementById("memory-imp-slider").value = imp;
    document.getElementById("memory-imp-label").textContent = imp;
    this.openModal("modal-memory-edit");
  }

  editMemory(id) {
    const found = this.memories.find((m) => m.id === id);
    if (found) this.openMemoryModal(found);
  }

  async submitSaveMemory() {
    const mid = document.getElementById("memory-id-input").value.trim();
    const content = document.getElementById("memory-content-input").value.trim();
    if (!content) {
      this.showToast("记忆内容不能为空", true);
      return;
    }
    const keywords = document.getElementById("memory-keywords-input").value.trim();
    const userId = document.getElementById("memory-user-input").value.trim();
    const memoryTier = parseInt(document.getElementById("memory-tier-select").value, 10) || 3;
    const importance = parseInt(document.getElementById("memory-imp-slider").value, 10) || 8;

    try {
      await this.apiPost("memories/mutate", {
        action: mid ? "edit" : "add",
        id: mid ? parseInt(mid, 10) : undefined,
        soul_id: this.currentSoulId,
        content,
        keywords,
        user_id: userId || null,
        memory_tier: memoryTier,
        importance,
      });
      this.closeModal("modal-memory-edit");
      await this.loadMemories();
      this.showToast(mid ? `已更新记忆 #${mid}` : "已新增记忆条目");
    } catch (err) {
      this.showToast("保存记忆失败: " + err.message, true);
    }
  }

  async pinMemoryToCore(id) {
    try {
      await this.apiPost("memories/mutate", {
        action: "pin_core",
        id,
        soul_id: this.currentSoulId,
      });
      await this.loadMemories();
      this.showToast(`📌 已将记忆 #${id} 永久锁定为 Tier 3 核心记忆！`);
    } catch (err) {
      this.showToast("锁定记忆失败: " + err.message, true);
    }
  }

  async deleteMemory(id) {
    try {
      await this.apiPost("memories/mutate", {
        action: "delete",
        id,
        soul_id: this.currentSoulId,
      });
      await this.loadMemories();
      this.showToast(`已物理删除记忆 #${id}`);
    } catch (err) {
      this.showToast("删除记忆失败: " + err.message, true);
    }
  }

  async triggerForgettingGC() {
    try {
      const res = await this.apiPost("memories/mutate", {
        action: "gc",
        soul_id: this.currentSoulId,
      });
      const st = res.gc_stats || {};
      await this.loadMemories();
      this.showToast(
        `🗑️ 遗忘清理完成：清理T1(${st.deleted_tier1 ?? 0})、淘汰T2(${
          st.deleted_tier2 ?? 0
        })、强化晋升T3(${st.promoted_to_tier3 ?? 0})`
      );
    } catch (err) {
      this.showToast("执行遗忘清理失败: " + err.message, true);
    }
  }

  async triggerMemoryExtract() {
    try {
      const res = await this.apiPost("memories/mutate", {
        action: "extract",
        soul_id: this.currentSoulId,
      });
      await this.loadMemories();
      this.showToast(`⚡ 已从当前会话滑窗提炼入库 ${res.extracted_count ?? 0} 条分级记忆`);
    } catch (err) {
      this.showToast("提炼记忆失败: " + err.message, true);
    }
  }

  // ==========================================================================
  // MODULE 5: 📱 Cyber Surfing Video Pool
  // ==========================================================================
  async loadSurfingPool() {
    const data = await this.apiGet("surfing/list_and_trigger", {
      soul_id: this.currentSoulId,
    });
    this.surfingVideos = data.videos || [];
    this.renderSurfingWall();
  }

  renderSurfingWall() {
    const wall = document.getElementById("surfing-video-wall");
    if (!wall) return;
    if (!this.surfingVideos.length) {
      wall.innerHTML = `<div class="form-hint">当前冲浪视频池为空，点击右上角“🚀 立即刷一次 B站 / 抖音”即可体验多模态看片与第一人称吐槽生成！</div>`;
      return;
    }

    wall.innerHTML = this.surfingVideos
      .map(
        (v) => `
        <div class="video-card">
          <div class="deck-card-header">
            <span class="tier-badge ${v.platform === "bilibili" ? "tier-3" : "tier-2"}">${
          v.platform === "bilibili" ? "📺 哔哩哔哩 Bilibili" : "🎵 抖音 Douyin"
        }</span>
            <span class="state-pill ${v.score >= 88 ? "state-IDLE" : "state-BUSY"}">🎯 趣味分: ${
          v.score
        }/100${v.score >= 88 ? " (暴击上头)" : ""}</span>
          </div>
          <div class="deck-card-title">
            <a href="${escHtml(v.share_url)}" target="_blank" rel="noopener noreferrer" style="color:var(--primary); text-decoration:none;">
              《${escHtml(v.title)}》
            </a>
            <div class="form-hint">UP主 / 作者：${escHtml(v.author)}</div>
          </div>
          ${
            v.visual_notes
              ? `<div class="video-quote"><strong>👁️ 《看片画面笔记》：</strong>${escHtml(
                  v.visual_notes
                )}</div>`
              : ""
          }
          <div class="video-quote">
            <strong>💬 拟人观后吐槽：</strong>“${escHtml(v.commentary)}”
          </div>
          ${
            v.hot_comments && v.hot_comments.length
              ? `<div class="form-hint">🔥 前排神评：${escHtml(
                  v.hot_comments.slice(0, 2).join(" | ")
                )}</div>`
              : ""
          }
          <div class="btn-group" style="justify-content:flex-end; margin-top:4px;">
            <button class="btn btn-primary btn-sm" onclick="app.shareVideoNow('${escHtml(
              v.video_id
            )}')">📤 立即分享</button>
            <button class="btn btn-danger btn-sm" onclick="app.deleteSurfedVideo(${
              v.id
            })">🗑️</button>
          </div>
        </div>`
      )
      .join("");
  }

  async triggerSurfOnce(platform) {
    try {
      this.showToast(`🚀 正在从 ${platform === "bilibili" ? "B站" : "抖音"} 拉取推荐流并多模态看片...`);
      const res = await this.apiPost("surfing/list_and_trigger", {
        action: "surf",
        soul_id: this.currentSoulId,
        platform,
        ignore_state_gate: true,
      });
      this.surfingVideos = res.videos || [];
      if (res.schedule) {
        this.currentSchedule = res.schedule;
        this.renderLiveStatusBanner();
        this.renderScheduleTimeline();
      }
      this.renderSurfingWall();
      await this.loadMemories();
      this.showToast(`📱 冲浪完成：《${res.review?.title || ""}》(评分: ${res.review?.score ?? 80}/100)`);
    } catch (err) {
      this.showToast("冲浪失败: " + err.message, true);
    }
  }

  async shareVideoNow(videoId) {
    const targetUmo = document.getElementById("surf-share-umo-select")?.value || "";
    try {
      const res = await this.apiPost("surfing/list_and_trigger", {
        action: "share",
        soul_id: this.currentSoulId,
        video_id: videoId,
        target_umo: targetUmo,
      });
      this.showToast(`📤 已将视频吐槽推送至会话 (${res.target_umo})`);
    } catch (err) {
      this.showToast("分享视频失败: " + err.message, true);
    }
  }

  async deleteSurfedVideo(id) {
    try {
      const res = await this.apiPost("surfing/list_and_trigger", {
        action: "delete",
        soul_id: this.currentSoulId,
        id,
      });
      this.surfingVideos = res.videos || [];
      this.renderSurfingWall();
      this.showToast("已从视频池移除");
    } catch (err) {
      this.showToast("移除视频失败: " + err.message, true);
    }
  }

  // ==========================================================================
  // Modal Helpers
  // ==========================================================================
  openModal(id) {
    const el = document.getElementById(id);
    if (el) el.classList.add("open");
  }

  closeModal(id) {
    const el = document.getElementById(id);
    if (el) el.classList.remove("open");
  }
}

const app = new OnlyYouConsoleApp();
window.app = app;
window.addEventListener("DOMContentLoaded", () => {
  app.init();
});
