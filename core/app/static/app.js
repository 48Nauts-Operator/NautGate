// NautGate dashboard — vanilla JS, no build step.
// Hits /v1/stats, /v1/decisions/recent, /v1/models, /v1/profile with the bearer
// token stored in localStorage. Tab routing via URL hash.

(async () => {
  // ── Timestamp formatters (must be declared before any function that
  // ── transitively uses them via render closures). All UI timestamps
  // ── render in CET / CEST (Europe/Berlin); the backend stores UTC.
  const _TZ = "Europe/Berlin";
  const _TS_FMT = new Intl.DateTimeFormat("en-GB", {
    timeZone: _TZ,
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  });
  const _DATE_FMT = new Intl.DateTimeFormat("en-GB", {
    timeZone: _TZ,
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hour12: false,
  });
  const _CHART_HOUR_FMT = new Intl.DateTimeFormat("en-GB", {
    timeZone: _TZ, hour: "2-digit", minute: "2-digit", hour12: false,
  });
  const _CHART_DAY_FMT = new Intl.DateTimeFormat("en-GB", {
    timeZone: _TZ, month: "2-digit", day: "2-digit",
  });

  function tsShort(ts) {
    if (!ts) return "-";
    try { return _TS_FMT.format(new Date(ts)); } catch (e) { return "-"; }
  }
  function tsFull(ts) {
    if (!ts) return "-";
    try { return _DATE_FMT.format(new Date(ts)); } catch (e) { return "-"; }
  }

  // Legacy single-token key — migrated into sessions on first run.
  const TOKEN_KEY = "nautgate.token";
  // Sessions: array of { id, label, token, agent_id, key_id, last_seen_at }.
  const SESSIONS_KEY = "nautgate.sessions";
  const ACTIVE_SESSION_KEY = "nautgate.active_session";
  const REFRESH_MS = 5000;

  let refreshTimer = null;
  let activeTab = "overview";

  // --- Sessions (multi-token) --------------------------------------------

  const authState = document.getElementById("auth-state");
  const sessionPill = document.getElementById("session-pill");

  function loadSessions() {
    try {
      const raw = localStorage.getItem(SESSIONS_KEY);
      if (raw) return JSON.parse(raw);
    } catch (e) { /* fall through to migration */ }
    // Migration: if there's a single-token entry from the old layout, fold it in.
    const legacy = localStorage.getItem(TOKEN_KEY);
    if (legacy) {
      const sessions = [{ id: cryptoId(), label: "imported", token: legacy, agent_id: null, key_id: null, last_seen_at: null }];
      saveSessions(sessions);
      localStorage.setItem(ACTIVE_SESSION_KEY, sessions[0].id);
      // Don't delete the legacy key right away; remove on next save instead.
      return sessions;
    }
    return [];
  }
  function saveSessions(sessions) {
    localStorage.setItem(SESSIONS_KEY, JSON.stringify(sessions));
    // Migration cleanup: drop the old single-token key once we have sessions.
    if (sessions.length) localStorage.removeItem(TOKEN_KEY);
  }
  function cryptoId() {
    if (window.crypto?.randomUUID) return crypto.randomUUID();
    return "s_" + Math.random().toString(36).slice(2, 12);
  }
  function getActiveSessionId() {
    return localStorage.getItem(ACTIVE_SESSION_KEY) || "";
  }
  function setActiveSessionId(id) {
    if (id) localStorage.setItem(ACTIVE_SESSION_KEY, id);
    else localStorage.removeItem(ACTIVE_SESSION_KEY);
  }
  function getActiveSession() {
    const sessions = loadSessions();
    const id = getActiveSessionId();
    return sessions.find(s => s.id === id) || sessions[0] || null;
  }

  function getToken() {
    // Working auth token. Prefers the active session's own token; falls
    // back to any owned ng_ token in the list so a "discovered" session
    // (no token of its own — e.g. claude-oauth-…) can still authenticate
    // and view its traffic via the ?agent_id=… scope override.
    const active = getActiveSession();
    if (active?.token) return active.token;
    const owned = loadSessions().find(s => s.token);
    if (owned) return owned.token;
    const meta = document.querySelector('meta[name="nautgate-token"]');
    return meta ? meta.getAttribute("content") || "" : "";
  }

  function getActiveAgentScope() {
    // When the active session is a discovered (token-less) entry, return
    // its agent_id so callers can pass ?agent_id=… to scope queries to
    // that agent without owning its token. Returns null otherwise —
    // server then naturally scopes to the caller's own agent_id.
    const active = getActiveSession();
    if (active && active.discovered && active.agent_id) return active.agent_id;
    return null;
  }

  function renderAuth() {
    const active = getActiveSession();
    if (active) {
      const label = active.label || active.agent_id || "session";
      const id4 = (active.token || "").slice(-4);
      authState.textContent = `${label} · …${id4}`;
      authState.classList.add("ok");
      authState.classList.remove("bad");
    } else {
      authState.textContent = "no session";
      authState.classList.remove("ok");
      authState.classList.add("bad");
    }
  }

  // Pill click → switch to Overview tab and focus the sessions list.
  sessionPill?.addEventListener("click", () => {
    location.hash = "#overview";
    setTimeout(() => document.getElementById("sessions-list")?.scrollIntoView({ behavior: "smooth", block: "start" }), 50);
  });

  // --- Sessions UI on the Overview tab -----------------------------------

  function fmtAgo(iso) {
    if (!iso) return "—";
    const ms = Date.now() - new Date(iso).getTime();
    if (ms < 60000) return Math.floor(ms / 1000) + "s ago";
    if (ms < 3600000) return Math.floor(ms / 60000) + "m ago";
    if (ms < 86400000) return Math.floor(ms / 3600000) + "h ago";
    return Math.floor(ms / 86400000) + "d ago";
  }

  let sessionPage = 0;
  let sessionPageSize = 10;

  function renderSessionsPager(total) {
    const pager = document.getElementById("sessions-pager");
    const range = document.getElementById("sessions-range");
    if (!pager || !range) return;
    if (total <= sessionPageSize) { pager.style.display = "none"; return; }
    pager.style.display = "flex";
    const start = sessionPage * sessionPageSize;
    const end = Math.min(start + sessionPageSize, total);
    range.textContent = `${start + 1}–${end} of ${total}`;
    const prev = document.getElementById("sessions-prev");
    const next = document.getElementById("sessions-next");
    if (prev) prev.disabled = sessionPage === 0;
    if (next) next.disabled = end >= total;
  }

  function renderSessions() {
    const list = document.getElementById("sessions-list");
    if (!list) return;
    const allSessions = loadSessions();
    const activeId = getActiveSessionId() || (allSessions[0]?.id ?? "");

    if (!allSessions.length) {
      list.innerHTML = '<p class="hint">No saved sessions. Add one below — paste a bearer token (ng_…) and optionally label it.</p>';
      renderSessionsPager(0);
      return;
    }

    const maxPage = Math.max(0, Math.ceil(allSessions.length / sessionPageSize) - 1);
    if (sessionPage > maxPage) sessionPage = maxPage;
    const sessions = allSessions.slice(sessionPage * sessionPageSize, sessionPage * sessionPageSize + sessionPageSize);
    renderSessionsPager(allSessions.length);

    list.innerHTML = '<table class="sessions-table"><thead><tr><th></th><th>label</th><th>agent</th><th>token</th><th>last used</th><th></th></tr></thead><tbody>'
      + sessions.map(s => {
        const isActive = s.id === activeId;
        const labelText = s.label || (s.agent_id || "(unlabeled)");
        const agentText = s.agent_id ? esc(s.agent_id) : '<span class="hint">unknown — click Verify</span>';
        const tokenCell = s.discovered
          ? '<span class="hint" title="OAuth-derived; no ng_ token. Scope-only.">discovered</span>'
          : `<code>ng_…${esc((s.token || "").slice(-6))}</code>`;
        const verifyBtn = s.discovered
          ? ''
          : `<button data-sess-verify="${esc(s.id)}" class="ghost">Verify</button>`;
        return `
          <tr class="${isActive ? "session-row-active" : ""}">
            <td>${isActive ? '<span class="sess-active-dot" title="active session"></span>' : ''}</td>
            <td><b>${esc(labelText)}</b></td>
            <td>${agentText}</td>
            <td>${tokenCell}</td>
            <td>${fmtAgo(s.last_seen_at)}</td>
            <td>
              ${isActive
                ? '<span class="hint">active</span>'
                : `<button data-sess-activate="${esc(s.id)}">Activate</button>`}
              ${verifyBtn}
              <button data-sess-delete="${esc(s.id)}" class="ghost danger">×</button>
            </td>
          </tr>`;
      }).join("")
      + '</tbody></table>';

    // Wire up actions.
    list.querySelectorAll("[data-sess-activate]").forEach(b => b.addEventListener("click", () => {
      setActiveSessionId(b.getAttribute("data-sess-activate"));
      renderAuth(); renderSessions(); refreshActive();
    }));
    list.querySelectorAll("[data-sess-delete]").forEach(b => b.addEventListener("click", () => {
      const id = b.getAttribute("data-sess-delete");
      if (!confirm("Delete this session?")) return;
      const sessions = loadSessions().filter(s => s.id !== id);
      saveSessions(sessions);
      if (getActiveSessionId() === id) {
        setActiveSessionId(sessions[0]?.id || "");
      }
      renderAuth(); renderSessions(); refreshActive();
    }));
    list.querySelectorAll("[data-sess-verify]").forEach(b => b.addEventListener("click", async () => {
      const id = b.getAttribute("data-sess-verify");
      await verifySession(id);
      renderSessions();
    }));
  }

  async function discoverAgents() {
    // Pulls /v1/agents/discovered and auto-adds any agent_id that's not
    // already in the localStorage session list. New entries are flagged
    // {discovered: true, token: null} so the rest of the UI knows they're
    // scope-only — when active, audit queries go through with
    // ?agent_id=<x> using a fallback owned token for auth.
    if (!getToken()) return;
    try {
      const res = await fetch("/v1/agents/discovered?hours=168",
        { headers: { Authorization: "Bearer " + getToken() } });
      if (!res.ok) return;
      const r = await res.json();
      const sessions = loadSessions();
      let changed = false;
      let freshlyAddedId = null;
      let freshlyAddedSeenAt = null;
      for (const agent of (r.data || [])) {
        const existing = sessions.find(s => s.agent_id === agent.agent_id);
        if (existing) {
          if (!existing.last_seen_at
              || agent.last_seen_at > existing.last_seen_at) {
            existing.last_seen_at = agent.last_seen_at;
            changed = true;
          }
        } else {
          const id = cryptoId();
          sessions.push({
            id,
            label: agent.agent_id,
            token: null,
            agent_id: agent.agent_id,
            key_id: null,
            last_seen_at: agent.last_seen_at,
            discovered: true,
            request_count: agent.request_count,
          });
          changed = true;
          // Track the freshly-added entry with the most recent activity so
          // we can auto-activate it below.
          if (!freshlyAddedSeenAt || agent.last_seen_at > freshlyAddedSeenAt) {
            freshlyAddedId = id;
            freshlyAddedSeenAt = agent.last_seen_at;
          }
        }
      }
      if (changed) {
        saveSessions(sessions);
        // Auto-activate the freshest newly-discovered session — covers the
        // common "I just ran claudeps, show me my new traffic" case. We
        // also auto-activate on the boot-with-no-active path. Existing
        // sessions whose last_seen merely got bumped don't trigger a
        // switch — the user keeps control of which view they're on.
        if (freshlyAddedId) {
          setActiveSessionId(freshlyAddedId);
        } else if (!getActiveSessionId() && sessions.length) {
          setActiveSessionId(sessions[0].id);
        }
        renderAuth();
        renderSessions();
      }
    } catch (e) {
      console.warn("agent discovery failed", e);
    }
  }

  async function verifySession(id) {
    const sessions = loadSessions();
    const s = sessions.find(x => x.id === id);
    if (!s) return;
    try {
      const res = await fetch("/v1/whoami", { headers: { Authorization: "Bearer " + s.token } });
      if (!res.ok) throw new Error("status_" + res.status);
      const me = await res.json();
      s.agent_id = me.agent_id || null;
      s.key_id = me.key_id || null;
      s.last_seen_at = new Date().toISOString();
      if (!s.label) s.label = me.agent_id || "session";
      saveSessions(sessions);
    } catch (e) {
      s.agent_id = null;
      s.last_seen_at = new Date().toISOString();
      saveSessions(sessions);
    }
  }

  // Add-session form
  document.getElementById("add-save")?.addEventListener("click", async () => {
    const errorEl = document.getElementById("add-error");
    errorEl.textContent = "";
    let token = document.getElementById("add-token").value.trim();
    // Be forgiving: strip "Bearer " prefix and any quotes a copy-paste may add.
    token = token.replace(/^Bearer\s+/i, "").replace(/^["']|["']$/g, "").trim();
    const label = document.getElementById("add-label").value.trim();
    if (!token) { errorEl.textContent = "token required"; return; }
    if (!token.startsWith("ng_")) {
      // Common mistake: pasting the key_id UUID instead of the full token.
      const looksLikeUuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(token);
      errorEl.textContent = looksLikeUuid
        ? "that looks like a key_id (UUID). The token is on the next line of the issue_key.py output — starts with ng_…"
        : "expected ng_… format (got " + token.slice(0, 8) + "…)";
      return;
    }
    // Validate by hitting whoami before saving.
    try {
      const res = await fetch("/v1/whoami", { headers: { Authorization: "Bearer " + token } });
      if (res.status === 401) { errorEl.textContent = "401 — token rejected"; return; }
      if (!res.ok) { errorEl.textContent = "validation failed: " + res.status; return; }
      const me = await res.json();
      const sessions = loadSessions();
      // Dedupe by token.
      const existing = sessions.find(s => s.token === token);
      if (existing) {
        existing.agent_id = me.agent_id;
        existing.key_id = me.key_id;
        existing.last_seen_at = new Date().toISOString();
        if (label) existing.label = label;
      } else {
        sessions.push({
          id: cryptoId(),
          label: label || me.agent_id || "session",
          token,
          agent_id: me.agent_id,
          key_id: me.key_id,
          last_seen_at: new Date().toISOString(),
        });
      }
      saveSessions(sessions);
      // If this was the first session, make it active.
      if (!getActiveSessionId()) setActiveSessionId(sessions[sessions.length - 1].id);
      // Reset form.
      document.getElementById("add-token").value = "";
      document.getElementById("add-label").value = "";
      document.getElementById("sessions-add").open = false;
      renderAuth(); renderSessions(); refreshActive();
    } catch (e) {
      errorEl.textContent = "error: " + e.message;
    }
  });

  // --- API helpers --------------------------------------------------------

  async function api(path) {
    const t = getToken();
    if (!t) throw new Error("no_token");
    const res = await fetch(path, { headers: { Authorization: "Bearer " + t } });
    if (res.status === 401) {
      authState.textContent = "401 — bad token";
      authState.classList.add("bad");
      authState.classList.remove("ok");
      throw new Error("unauthorized");
    }
    if (!res.ok) throw new Error("http_" + res.status);
    return res.json();
  }

  async function apiPut(path, body) {
    const t = getToken();
    if (!t) throw new Error("no_token");
    const res = await fetch(path, {
      method: "PUT",
      headers: {
        Authorization: "Bearer " + t,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error("http_" + res.status);
    return res.json();
  }

  // --- Tab routing --------------------------------------------------------

  // Only bind to anchors that declare a top-level tab via data-tab. This
  // matters because nested <nav> elements (e.g. the Settings sub-nav) would
  // otherwise be captured by `nav a` and call activateTab(undefined),
  // wiping the page until the next refresh.
  document.querySelectorAll("nav a[data-tab]").forEach((a) => {
    a.addEventListener("click", () => {
      const tab = a.dataset.tab;
      activateTab(tab);
    });
  });

  function activateTab(name) {
    activeTab = name;
    document.querySelectorAll("nav a[data-tab]").forEach((a) =>
      a.classList.toggle("active", a.dataset.tab === name)
    );
    document.querySelectorAll(".tab").forEach((s) =>
      s.classList.toggle("active", s.id === "tab-" + name)
    );
    refreshActive();
    // Auto-refresh on the live tabs.
    clearInterval(refreshTimer);
    if (name === "decisions") {
      refreshTimer = setInterval(loadDecisions, REFRESH_MS);
    } else if (name === "audit") {
      refreshTimer = setInterval(loadAudit, REFRESH_MS);
    } else if (name === "overview") {
      // Live provider status without re-pulling the whole overview.
      refreshTimer = setInterval(loadProviderStatus, 60000);
    }
  }

  // --- Global notification strip --------------------------------------
  async function loadNotifications() {
    const stripEl = document.getElementById("notif-strip");
    if (!stripEl) return;
    try {
      const data = await api("/v1/notifications");
      const items = (data && data.items) || [];
      if (!items.length) {
        stripEl.hidden = true;
        stripEl.innerHTML = "";
        return;
      }
      stripEl.hidden = false;
      stripEl.innerHTML = items.map((n) => {
        const cls = "notif-" + (n.level || "info");
        const icon = ({warning: "⚠", error: "✗", success: "✓", info: "ℹ"})[n.level] || "•";
        const href = n.href ? `href="${esc(n.href)}"` : "";
        const tag = n.href ? "a" : "span";
        return `<${tag} class="notif ${cls}" ${href}><span class="notif-icon">${icon}</span>${esc(n.text)}</${tag}>`;
      }).join("");
      // Wire any hash-based hrefs to actually switch tabs.
      stripEl.querySelectorAll('a[href^="#"]').forEach((a) => {
        a.addEventListener("click", (ev) => {
          ev.preventDefault();
          const tab = a.getAttribute("href").slice(1);
          if (document.getElementById("tab-" + tab)) activateTab(tab);
        });
      });
    } catch (_e) {
      stripEl.hidden = true;
    }
  }

  function refreshActive() {
    if (activeTab === "overview") loadOverview();
    else if (activeTab === "audit") loadAudit();
    else if (activeTab === "cost") loadCost();
    else if (activeTab === "cache") loadCache();
    else if (activeTab === "privacy") loadPrivacy();
    else if (activeTab === "decisions") loadDecisions();
    else if (activeTab === "scorecard") loadScorecard();
    else if (activeTab === "drift") loadDrift();
    else if (activeTab === "probe") loadProbe();
    else if (activeTab === "quality") loadQuality();
    else if (activeTab === "behavior") loadBehavior();
    else if (activeTab === "health" || activeTab === "models") loadModels();
    else if (activeTab === "settings") loadSettings();
  }

  // --- Settings sub-tabs (horizontal nav within #tab-settings) ----------
  const SETTINGS_SUBTAB_KEY = "nautgate-settings-subtab";
  function showSettingsSubtab(name) {
    if (!name) name = "profile";
    document.querySelectorAll("#settings-subnav a").forEach((a) =>
      a.classList.toggle("active", a.dataset.subtab === name)
    );
    document.querySelectorAll("#tab-settings .settings-pane").forEach((p) => {
      p.hidden = p.dataset.pane !== name;
    });
    try { localStorage.setItem(SETTINGS_SUBTAB_KEY, name); } catch (_e) {}
  }
  document.querySelectorAll("#settings-subnav a").forEach((a) => {
    a.addEventListener("click", (ev) => {
      ev.preventDefault();
      showSettingsSubtab(a.dataset.subtab);
    });
  });
  // Restore last-active sub-tab on page load.
  try {
    const saved = localStorage.getItem(SETTINGS_SUBTAB_KEY);
    if (saved) showSettingsSubtab(saved);
  } catch (_e) {}

  // --- Overview -----------------------------------------------------------

  async function loadOverview() {
    // Sessions section gets re-rendered every Overview load so the
    // last-used timestamps stay fresh as the active session makes calls.
    renderSessions();
    loadProviderStatus();
    try {
      const s = await api("/v1/stats?hours=24");
      document.getElementById("m-total").textContent = s.requests_total ?? "0";
      document.getElementById("m-empty").textContent = pct(s.empty_rate);
      document.getElementById("m-p50").textContent = ms(s.latency_ms?.p50);
      document.getElementById("m-p95").textContent = ms(s.latency_ms?.p95);
      renderBars("tier-bars", s.requests_by_tier);
      renderBars("format-bars", s.requests_by_inbound_format);
    } catch (e) {
      // Silently leave dashes; auth state above will explain.
    }
  }

  async function loadProviderStatus() {
    if (!getToken()) return;
    try {
      renderProviderStatus(await api("/v1/health/providers"));
    } catch (e) { /* leave prior state */ }
  }

  function renderProviderStatus(d) {
    const strip = document.getElementById("provider-strip");
    if (!strip) return;
    const dot = { up: "up", degraded: "degraded", down: "down", "no-data": "nodata" };
    strip.innerHTML = (d.providers || []).map(p => {
      const cls = dot[p.status] || "nodata";
      let detail;
      if (p.status === "degraded" || p.status === "down") {
        const pctOv = (p.overload_pct * 100).toFixed(0);
        const bits = [];
        if (p.overload_pct > 0) bits.push(`${pctOv}% overloaded`);
        if (p.retries_absorbed) bits.push(`${p.retries_absorbed} absorbed`);
        if (p.rate_limited) bits.push(`${p.rate_limited}× 429`);
        detail = bits.join(" · ") || "errors";
      } else if (p.status === "up") {
        detail = p.total ? `${p.success}/${p.total} ok (10m)` : "reachable";
      } else {
        detail = p.heartbeat && p.heartbeat.status === "no-cred" ? "no credential" : "no recent calls";
      }
      const hb = p.heartbeat && p.heartbeat.latency_ms != null ? ` · ${p.heartbeat.latency_ms}ms` : "";
      return `<div class="status-badge ${cls}">
        <span class="status-dot"></span>
        <span class="status-label">${esc(p.label)}</span>
        <span class="status-detail">${esc(detail)}${hb}</span>
      </div>`;
    }).join("") || '<span class="hint">No provider data yet.</span>';
  }

  document.getElementById("sessions-prev")?.addEventListener("click", () => { sessionPage--; renderSessions(); });
  document.getElementById("sessions-next")?.addEventListener("click", () => { sessionPage++; renderSessions(); });
  document.getElementById("sessions-page-size")?.addEventListener("change", (e) => {
    sessionPageSize = Number(e.target.value) || 10; sessionPage = 0; renderSessions();
  });

  function renderBars(id, dict) {
    const el = document.getElementById(id);
    el.innerHTML = "";
    const max = Math.max(...Object.values(dict || {}), 1);
    Object.entries(dict || {})
      .sort((a, b) => b[1] - a[1])
      .forEach(([k, n]) => {
        const w = Math.round((n / max) * 400) + "px";
        const row = document.createElement("div");
        row.className = "bar";
        row.innerHTML = `<span class="name">${k}</span><span class="fill" style="width:${w}"></span><span class="count">${n}</span>`;
        el.appendChild(row);
      });
  }

  // --- Decisions ----------------------------------------------------------

  async function loadDecisions() {
    try {
      const r = await api("/v1/decisions/recent?limit=50");
      const tbody = document.getElementById("dec-tbody");
      tbody.innerHTML = r.data
        .map(
          (d) => `
        <tr data-decision="${esc(d.decision_id)}">
          <td>${tsShort(d.ts)}</td>
          <td>${esc(d.inbound_format)}</td>
          <td><span class="tag tier">${esc(d.tier || "-")}</span></td>
          <td>${(d.score ?? 0).toFixed(2)}</td>
          <td>${sensTag(d.sensitivity)}</td>
          <td>${esc(d.provider)}</td>
          <td>${esc(d.model)}</td>
          <td class="${statusClass(d.status_code)}">${d.status_code ?? "-"}</td>
          <td>${d.duration_ms ?? "-"}</td>
          <td>${tokens(d)}</td>
          <td>${costShort(d)}</td>
        </tr>`
        )
        .join("");
      tbody.querySelectorAll("tr").forEach((row) => {
        row.addEventListener("click", () => openDetail(row.dataset.decision));
      });
    } catch (e) {
      /* swallow; auth state explains */
    }
  }

  document.getElementById("dec-reload").addEventListener("click", loadDecisions);

  // --- Decision detail drawer --------------------------------------------

  const drawer = document.getElementById("detail-drawer");
  document.getElementById("detail-close").addEventListener("click", () =>
    drawer.classList.add("hidden")
  );
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") drawer.classList.add("hidden");
  });

  async function openDetail(decisionId) {
    if (!decisionId) return;
    drawer.classList.remove("hidden");
    document.getElementById("detail-id").textContent = decisionId;
    document.getElementById("detail-body").innerHTML =
      '<p class="hint">loading…</p>';
    try {
      const scope = getActiveAgentScope();
      const url = "/v1/decisions/" + encodeURIComponent(decisionId)
        + (scope ? "?agent_id=" + encodeURIComponent(scope) : "");
      const d = await api(url);
      document.getElementById("detail-body").innerHTML = renderDetail(d);
    } catch (e) {
      document.getElementById("detail-body").innerHTML =
        '<p class="hint">failed to load</p>';
    }
  }

  function renderDetail(d) {
    const kv = (k, v) => `<div class="k">${esc(k)}</div><div class="v">${v}</div>`;
    const kvDim = (k, v) =>
      `<div class="k">${esc(k)}</div><div class="v dim">${v}</div>`;

    let html = "";

    // Identity / routing
    html += '<div class="section-title">Routing</div>';
    html += '<div class="kv">';
    html += kv("ts", esc(d.ts));
    html += kv("agent", esc(d.agent_id));
    html += kv("inbound", esc(d.inbound_format));
    html += kv("model_requested", esc(d.model_requested));
    html += kv(
      "decision",
      `${esc(d.decision_provider)} / ${esc(d.decision_model)}`
    );
    if (d.decision_reason) html += kv("reason", esc(d.decision_reason));
    html += "</div>";

    // Classification
    html += '<div class="section-title">Classification</div>';
    html += '<div class="kv">';
    html += kv(
      "tier",
      `<span class="tag tier">${esc(d.classified_tier || "-")}</span>`
    );
    html += kv(
      "score",
      d.classified_score != null ? d.classified_score.toFixed(4) : "—"
    );
    html += kv("sensitivity", sensTag(d.classified_sensitivity) || "none");
    html += "</div>";
    if (d.classified_signals && d.classified_signals.length) {
      html += '<div class="section-title">Sensitivity signals</div>';
      html +=
        '<pre class="body-block">' +
        esc(JSON.stringify(d.classified_signals, null, 2)) +
        "</pre>";
    }
    if (d.brain_hints) {
      html += '<div class="section-title">Brain hints</div>';
      html +=
        '<pre class="body-block">' +
        esc(JSON.stringify(d.brain_hints, null, 2)) +
        "</pre>";
    }

    // Outcome / cost
    html += '<div class="section-title">Outcome</div>';
    html += '<div class="kv">';
    html += kv(
      "status",
      `<span class="${statusClass(d.status_code)}">${d.status_code ?? "—"}</span>`
    );
    html += kv("duration_ms", d.duration_ms ?? "—");
    if (d.first_byte_ms != null) html += kv("first_byte_ms", d.first_byte_ms);
    html += kv(
      "tokens",
      `${d.prompt_tokens ?? "?"} → ${d.completion_tokens ?? "?"}` +
        (d.reasoning_tokens ? ` (+${d.reasoning_tokens} reasoning)` : "")
    );
    html += kv("cost_usd", d.cost_usd != null ? usd(d.cost_usd) : "—");
    if (d.was_empty) html += kv("was_empty", "true");
    if (d.was_truncated) html += kv("was_truncated", "true");
    if (d.client_disconnected)
      html += kv("client_disconnected", "true");
    html += "</div>";

    // Prompt body (capture-policy gated server-side)
    html += '<div class="section-title">Prompt</div>';
    if (d.prompt_body) {
      html += '<pre class="body-block">' + esc(d.prompt_body) + "</pre>";
      if (d.prompt_body_truncated_at_byte) {
        html += `<p class="hint">truncated at ${d.prompt_body_truncated_at_byte} bytes</p>`;
      }
    } else if (d.prompt_excerpt) {
      html += '<pre class="body-block">' + esc(d.prompt_excerpt) + "</pre>";
      html +=
        '<p class="hint">excerpt only (sensitivity gate suppressed full body)</p>';
    } else {
      html += '<p class="hint">no body captured</p>';
    }

    // Response body
    html += '<div class="section-title">Response</div>';
    if (d.response_body) {
      html += '<pre class="body-block">' + esc(d.response_body) + "</pre>";
      if (d.response_body_truncated_at_byte) {
        html += `<p class="hint">truncated at ${d.response_body_truncated_at_byte} bytes</p>`;
      }
    } else {
      html += '<p class="hint">no response body captured</p>';
    }

    return html;
  }

  function costShort(d) {
    if (d.cost_usd == null) return "—";
    return usd(d.cost_usd);
  }

  // --- Privacy / Lighthouse audit ----------------------------------------

  let privacyWindow = 168;

  document.querySelectorAll('#tab-privacy .window-buttons button').forEach((b) => {
    b.addEventListener('click', () => {
      privacyWindow = Number(b.dataset.window);
      document
        .querySelectorAll('#tab-privacy .window-buttons button')
        .forEach((x) => x.classList.toggle('active', x === b));
      loadPrivacy();
    });
  });

  async function loadPrivacy() {
    if (!getToken()) return;
    try {
      const r = await api(`/v1/findings/summary?hours=${privacyWindow}&scan_limit=500`);
      renderPrivacy(r);
    } catch (e) {
      /* swallow */
    }
  }

  const LH_CAT_ORDER = ["credentials", "secrets", "pii", "infrastructure"];
  const LH_CAT_META = {
    credentials: { icon: "🔑", label: "Credentials" },
    secrets: { icon: "🔒", label: "Secrets" },
    pii: { icon: "👤", label: "PII" },
    infrastructure: { icon: "🖥", label: "Infrastructure" },
  };

  function renderPrivacy(r) {
    drawLhScore(r.overall);
    document.getElementById("lh-verdict").textContent = r.verdict || "—";
    document.getElementById("lh-verdict").style.color = lhScoreColor(r.overall);
    document.getElementById("lh-explain").textContent = r.verdict_explain || "";
    document.getElementById("lh-scanned").textContent = `Scanned ${r.scanned_count} recent decisions.`;

    // Category cards.
    const cats = document.getElementById("lh-categories");
    cats.innerHTML = LH_CAT_ORDER.map((cat) => {
      const score = r.cat_scores?.[cat] ?? 100;
      const counts = r.cat_counts?.[cat] || { critical: 0, warning: 0, info: 0 };
      const total = counts.critical + counts.warning + counts.info;
      return `
        <div class="lh-cat">
          <span class="lh-cat-icon">${LH_CAT_META[cat].icon}</span>
          <div style="flex:1">
            <div class="lh-cat-label">${LH_CAT_META[cat].label}</div>
            <div class="lh-cat-score" style="color:${lhScoreColor(score)}">${score}</div>
            <div class="lh-cat-detail">${total} finding${total === 1 ? "" : "s"} · ${counts.critical}c / ${counts.warning}w / ${counts.info}i</div>
          </div>
        </div>`;
    }).join("");

    // Hosts table.
    const hostsBody = document.querySelector("#lh-hosts tbody");
    if ((r.host_matrix || []).length === 0) {
      hostsBody.innerHTML = `<tr><td colspan="7" class="hint">No findings.</td></tr>`;
    } else {
      hostsBody.innerHTML = r.host_matrix
        .map(
          (h) => `
          <tr>
            <td>${esc(h.agent_id)}</td>
            <td class="${h.credentials > 0 ? "lh-cell-crit" : ""}">${h.credentials}</td>
            <td class="${h.secrets > 0 ? "lh-cell-crit" : ""}">${h.secrets}</td>
            <td class="${h.pii > 0 ? "lh-cell-warn" : ""}">${h.pii}</td>
            <td class="${h.infrastructure > 0 ? "lh-cell-warn" : ""}">${h.infrastructure}</td>
            <td><b>${h.total}</b></td>
            <td>${tsShort(h.lastSeen)}</td>
          </tr>`
        )
        .join("");
    }

    // Types table with expandable detail.
    const typesBody = document.querySelector("#lh-types tbody");
    if ((r.type_matrix || []).length === 0) {
      typesBody.innerHTML = `<tr><td colspan="6" class="hint">No findings.</td></tr>`;
    } else {
      typesBody.innerHTML = r.type_matrix
        .map((t, i) => {
          const sev = `<span class="lh-sev-${esc(t.severity)}">${esc(t.severity)}</span>`;
          const sample = (t.samples && t.samples[0]) || "—";
          return `
            <tr data-lh-row="${i}">
              <td>${esc(t.display)}</td>
              <td>${sev}</td>
              <td><b>${t.count}</b></td>
              <td>${esc((t.agents || []).join(", "))}</td>
              <td>${tsShort(t.lastSeen)}</td>
              <td><code>${esc(sample)}</code></td>
            </tr>
            <tr class="lh-detail-row" data-lh-detail="${i}" style="display:none">
              <td colspan="6">
                <div class="lh-block">
                  <div class="lh-block-title">What happened</div>
                  <div>${esc(t.description || "")}</div>
                </div>
                <div class="lh-block">
                  <div class="lh-block-title">How to prevent it</div>
                  <div>${esc(t.remediation || "")}</div>
                </div>
                <div class="lh-block">
                  <div class="lh-block-title">All matched samples (${(t.samples || []).length})</div>
                  ${(t.samples || []).map((s) => `<div class="lh-sample">${esc(s)}</div>`).join("") || '<div class="hint">No samples captured (body suppressed by sensitivity gate).</div>'}
                </div>
              </td>
            </tr>`;
        })
        .join("");
      typesBody.querySelectorAll("tr[data-lh-row]").forEach((row) => {
        row.addEventListener("click", () => {
          const i = row.dataset.lhRow;
          const detail = typesBody.querySelector(`tr[data-lh-detail="${i}"]`);
          if (detail) detail.style.display = detail.style.display === "none" ? "table-row" : "none";
        });
      });
    }
  }

  function drawLhScore(score) {
    const canvas = document.getElementById("lh-score");
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    const dpr = window.devicePixelRatio || 1;
    canvas.width = 160 * dpr;
    canvas.height = 160 * dpr;
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, 160, 160);

    const cx = 80, cy = 80, r = 64, lw = 12;
    ctx.beginPath();
    ctx.arc(cx, cy, r, 0, Math.PI * 2);
    ctx.strokeStyle = "rgba(255,255,255,0.06)";
    ctx.lineWidth = lw;
    ctx.stroke();

    const color = lhScoreColor(score);
    const end = -Math.PI / 2 + ((score || 0) / 100) * Math.PI * 2;
    ctx.beginPath();
    ctx.arc(cx, cy, r, -Math.PI / 2, end);
    ctx.strokeStyle = color;
    ctx.lineWidth = lw;
    ctx.lineCap = "round";
    ctx.stroke();

    ctx.fillStyle = color;
    ctx.font = "bold 36px ui-monospace, monospace";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(String(score ?? 0), cx, cy);
  }

  function lhScoreColor(score) {
    if (score >= 90) return "#10b981";
    if (score >= 70) return "#6ea5ff";
    if (score >= 50) return "#f59e0b";
    return "#ef4444";
  }

  // --- Audit log live feed ------------------------------------------------

  document.getElementById("audit-reload").addEventListener("click", () => loadAudit());

  let auditExpandedId = null;
  const auditDetailCache = new Map();

  async function loadAudit() {
    const list = document.getElementById("audit-list");
    if (!getToken()) {
      list.innerHTML = '<p class="hint">No active session — pick one in Overview → Sessions.</p>';
      return;
    }
    try {
      const scope = getActiveAgentScope();
      const url = "/v1/decisions/recent?limit=50"
        + (scope ? "&agent_id=" + encodeURIComponent(scope) : "");
      const r = await api(url);
      renderAudit(r.data || [], r.agent_id);
    } catch (e) {
      list.innerHTML = `<p class="hint" style="color:#ff5c5c">load failed: ${esc(e.message || String(e))}</p>`;
      console.error("loadAudit failed", e);
    }
  }

  function renderAudit(rows, agentId) {
    const list = document.getElementById("audit-list");
    if (!rows.length) {
      const who = agentId ? `agent_id <code>${esc(agentId)}</code>` : "the active session";
      list.innerHTML = `<p class="hint">No requests for ${who} yet. The audit log is scoped to whichever session is active — switch sessions on the Overview tab to see another agent's traffic.</p>`;
      return;
    }
    list.innerHTML = rows
      .map((r) => {
        const dot = r.was_empty || r.client_disconnected
          ? "warn"
          : r.status_code && r.status_code >= 400
          ? "bad"
          : r.status_code === 200
          ? "ok"
          : "dim";
        const total = r.prompt_tokens || 0;
        const bar = `<div class="audit-bar"><div class="audit-seg-user" style="width:100%"></div></div>`;
        const source = r.source_hostname || (r.source_ip ? r.source_ip : "—");
        const cost = r.cost_usd != null ? usd(r.cost_usd) : "—";
        const latency = r.duration_ms != null ? r.duration_ms + "ms" : "—";
        const calls = (r.tool_calls_made || []).map((t) => `<span class="audit-tool-chip">${esc(t.name || "?")}</span>`).join("");
        const callsLine = calls ? `<div class="audit-tools-called">${calls}</div>` : "";
        // Bloat chip — show when this request triggered findings.
        let bloatChip = "";
        if (r.bloat_score && r.bloat_score > 0) {
          const sev = r.bloat_score >= 0.06 ? "crit" : r.bloat_score >= 0.02 ? "warn" : "info";
          const wasteText = r.estimated_waste_usd && r.estimated_waste_usd > 0
            ? ` · $${r.estimated_waste_usd.toFixed(4)}`
            : "";
          const reqKB = r.request_size_bytes ? Math.round(r.request_size_bytes / 1024) + "KB" : "";
          bloatChip = `<span class="bloat-chip bloat-${sev}" title="bloat score ${r.bloat_score.toFixed(3)}">⚠ ${reqKB}${wasteText}</span>`;
        }
        // Show "decision → actual" when they differ (e.g. openrouter/auto → google/gemini-2.5-flash).
        const decided = r.model || r.model_requested || "—";
        const actual = r.actual_model && r.actual_model !== decided ? r.actual_model : null;
        const actualBit = actual
          ? ` <span class="audit-source">→ ${esc(actual)}${r.actual_provider ? ' <span style="color:var(--text-dim)">(' + esc(r.actual_provider) + ')</span>' : ''}</span>`
          : "";
        return `
          <div class="audit-row" data-decision="${esc(r.decision_id)}">
            <div class="audit-dot ${dot}"></div>
            <div>
              <div class="audit-model">${esc(decided)}${actualBit} <span class="audit-source">· ${esc(source)} · ${esc(r.inbound_format || "")}</span> ${bloatChip}</div>
              ${callsLine}
            </div>
            <div>${bar}<div class="audit-source" style="margin-top:2px">${total} tokens · ${(r.request_size_bytes || 0) >= 1024 ? Math.round(r.request_size_bytes / 1024) + "KB" : (r.request_size_bytes || 0) + "B"} req</div></div>
            <div class="audit-meta-right">
              <div class="audit-cost-row">
                <button class="audit-thumbs-down" data-decision="${esc(r.decision_id)}" title="Bad call — run an immediate quality eval">👎</button>
                <div class="audit-cost">${cost}</div>
              </div>
              <div>${latency} · ${tsShort(r.ts)}</div>
            </div>
          </div>
          <div class="audit-detail" id="audit-detail-${esc(r.decision_id)}"></div>`;
      })
      .join("");

    document.querySelectorAll(".audit-row").forEach((row) => {
      row.addEventListener("click", (ev) => {
        // Don't toggle the drawer when the user clicks the thumbs-down icon.
        if (ev.target && ev.target.classList.contains("audit-thumbs-down")) return;
        toggleAuditDetail(row.dataset.decision);
      });
    });
    document.querySelectorAll(".audit-thumbs-down").forEach((btn) => {
      btn.addEventListener("click", async (ev) => {
        ev.stopPropagation();
        const did = btn.dataset.decision;
        const prev = btn.textContent;
        btn.textContent = "⏳";
        btn.disabled = true;
        try {
          const res = await fetch("/v1/quality/evaluate/" + encodeURIComponent(did), {
            method: "POST",
            headers: { Authorization: "Bearer " + getToken(), "Content-Type": "application/json" },
            body: JSON.stringify({ trigger: "thumbs_down" }),
          });
          if (!res.ok) throw new Error("http_" + res.status);
          const row = await res.json();
          // Invalidate cached detail so reopen pulls fresh data; also refresh
          // coach panel if drawer is already open for this row.
          auditDetailCache.delete(did);
          if (auditExpandedId === did) {
            const el = document.getElementById("audit-detail-" + did);
            if (el) toggleAuditDetail(did), toggleAuditDetail(did);
          }
          btn.textContent = "✓";
          btn.title = "Evaluated · open the row to see the Coach";
          setTimeout(() => { btn.textContent = prev; btn.disabled = false; }, 2500);
        } catch (e) {
          btn.textContent = "✗";
          btn.title = "Eval failed: " + (e.message || e);
          setTimeout(() => { btn.textContent = prev; btn.disabled = false; }, 2500);
        }
      });
    });
    if (auditExpandedId) {
      const el = document.getElementById("audit-detail-" + auditExpandedId);
      if (el) {
        el.classList.add("open");
        if (auditDetailCache.has(auditExpandedId)) {
          el.innerHTML = renderAuditDetail(auditDetailCache.get(auditExpandedId));
        }
      }
    }
  }

  async function toggleAuditDetail(decisionId) {
    if (auditExpandedId === decisionId) {
      const el = document.getElementById("audit-detail-" + decisionId);
      if (el) el.classList.remove("open");
      auditExpandedId = null;
      return;
    }
    if (auditExpandedId) {
      const prev = document.getElementById("audit-detail-" + auditExpandedId);
      if (prev) prev.classList.remove("open");
    }
    auditExpandedId = decisionId;
    const el = document.getElementById("audit-detail-" + decisionId);
    if (!el) return;
    el.classList.add("open");
    el.innerHTML = '<p class="hint">loading…</p>';
    try {
      const scope = getActiveAgentScope();
      const url = "/v1/decisions/" + encodeURIComponent(decisionId)
        + (scope ? "?agent_id=" + encodeURIComponent(scope) : "");
      const d = await api(url);
      auditDetailCache.set(decisionId, d);
      el.innerHTML = renderAuditDetail(d);
    } catch (e) {
      el.innerHTML = '<p class="hint">failed to load</p>';
    }
  }

  function renderAuditDetail(d) {
    const grid = (k, v) =>
      `<div><div class="k">${esc(k)}</div><div class="v">${v}</div></div>`;

    const t = d.token_estimate || { system: 0, tools: 0, history: 0, user: 0 };
    const total = (t.system + t.tools + t.history + t.user) || 1;
    const bar = `
      <div class="audit-bar" style="margin-bottom:8px">
        ${segPct(t.system, total, "system")}
        ${segPct(t.tools, total, "tools")}
        ${segPct(t.history, total, "history")}
        ${segPct(t.user, total, "user")}
      </div>
      <div class="audit-legend">
        <span><span class="swatch audit-seg-system"></span>System ${t.system}</span>
        <span><span class="swatch audit-seg-tools"></span>Tools ${t.tools}</span>
        <span><span class="swatch audit-seg-history"></span>History ${t.history}</span>
        <span><span class="swatch audit-seg-user"></span>User ${t.user}</span>
      </div>`;

    const reqKB = d.request_size_bytes != null ? (d.request_size_bytes / 1024).toFixed(1) + " KB" : "—";
    const respKB = d.response_size_bytes != null ? (d.response_size_bytes / 1024).toFixed(1) + " KB" : "—";

    let html = "";
    html += '<div class="section-title">What got sent</div>';
    html += bar;
    html += '<div class="audit-grid">';
    html += grid("Endpoint", esc(inboundEndpoint(d.inbound_format)));
    const actualLine = d.actual_model && d.actual_model !== d.decision_model
      ? ` → <b>${esc(d.actual_model)}</b>${d.actual_provider ? ' (' + esc(d.actual_provider) + ')' : ''}`
      : "";
    html += grid("Upstream", esc(d.decision_provider || "—") + " / " + esc(d.decision_model || "—") + actualLine);
    html += grid("Source", esc(d.source_hostname || d.source_ip || "—"));
    html += grid("Messages", d.messages_count ?? "—");
    html += grid("Tools", d.tools_count ?? "—");
    html += grid("Stream", d.stream_flag ? "Yes" : "No");
    html += grid("Input tokens", d.prompt_tokens ?? "—");
    html += grid("Output tokens", d.completion_tokens ?? "—");
    html += grid("Request size", reqKB);
    html += grid("Response size", respKB);
    html += grid("Status", `<span class="${statusClass(d.status_code)}">${d.status_code ?? "—"}</span>`);
    html += grid("Latency", (d.duration_ms ?? "—") + "ms");
    html += grid("Cost", d.cost_usd != null ? usd(d.cost_usd) : "—");
    html += grid("Tier · Score", `${esc(d.classified_tier || "—")} · ${(d.classified_score ?? 0).toFixed(2)}`);
    html += grid("Sensitivity", sensTag(d.classified_sensitivity) || "none");
    html += "</div>";

    // Findings inline
    if (d.classified_signals && d.classified_signals.length) {
      html += '<div class="section-title">Findings</div>';
      html += d.classified_signals
        .map((s) => `<div class="lh-sample">${esc(s.rule_id)} · ${esc(s.severity || s.sensitivity || "")} · ×${s.count || 1}</div>`)
        .join("");
    }

    // Payload Anatomy — what *actually* gets shipped beyond the user's prompt
    if (d.payload_anatomy) {
      html += '<div class="section-title">Payload Anatomy — what shipped upstream</div>';
      html += renderPayloadAnatomy(d.payload_anatomy);
    }

    // Bloat findings — brain layer's per-finding evaluation of this request
    if (d.bloat_findings && d.bloat_findings.length) {
      html += '<div class="section-title">Bloat Findings <span class="hint">— score penalty: −' + (d.bloat_score || 0).toFixed(3);
      if (d.estimated_waste_usd) html += ' · est. wasted spend: $' + d.estimated_waste_usd.toFixed(4);
      html += '</span></div>';
      html += '<div class="bloat-findings">';
      html += d.bloat_findings.map(f => `
        <div class="bloat-finding bloat-${esc(f.severity || "info")}">
          <div class="bloat-finding-head">
            <span class="bloat-sev-badge bloat-${esc(f.severity || "info")}">${esc(f.severity)}</span>
            <b>${esc(f.type)}</b>
            <span class="hint">−${(f.penalty || 0).toFixed(3)}</span>
          </div>
          <div class="bloat-finding-detail">${esc(f.detail || "")}</div>
        </div>`).join("");
      html += '</div>';
    }

    // Prompt body — block-style (one msg-block per message)
    html += '<div class="section-title">Prompt — raw message blocks</div>';
    if (d.prompt_body) {
      html += renderMessageBlocks(d.prompt_body);
      if (d.prompt_body_truncated_at_byte) {
        html += `<p class="hint">truncated at ${d.prompt_body_truncated_at_byte} bytes</p>`;
      }
    } else if (d.prompt_excerpt) {
      html += '<pre class="body-block">' + esc(d.prompt_excerpt) + "</pre>";
      html += '<p class="hint">excerpt only — sensitivity gate suppressed full body</p>';
    } else {
      html += '<p class="hint">no body captured</p>';
    }

    // Response — block-style with text + tool_use blocks
    html += '<div class="section-title">Response — what came back</div>';
    html += renderResponseBlocks(d);

    // Coach — judge's verdict, lazy-loaded on first expand.
    html += `
      <details class="coach-accordion" data-decision="${esc(d.decision_id || "")}">
        <summary>▸ Coach <span class="hint">(judge eval, click to load)</span></summary>
        <div class="coach-body"><p class="hint">loading…</p></div>
      </details>`;
    return html;
  }

  // Wire coach accordions after the drawer paints (event delegation on body).
  document.addEventListener("toggle", async (ev) => {
    const det = ev.target;
    if (!(det && det.classList && det.classList.contains("coach-accordion"))) return;
    if (!det.open) return;
    const did = det.dataset.decision;
    const body = det.querySelector(".coach-body");
    if (!did || !body || body.dataset.loaded === "1") return;
    body.dataset.loaded = "1";
    try {
      const res = await fetch("/v1/quality/evaluation/" + encodeURIComponent(did), {
        headers: { Authorization: "Bearer " + getToken() },
      });
      if (res.status === 404) {
        body.innerHTML = `
          <p class="hint">No evaluation for this call yet.</p>
          <button class="ghost coach-run-now" data-decision="${esc(did)}">Run eval now</button>`;
        body.querySelector(".coach-run-now").addEventListener("click", async (e) => {
          e.stopPropagation();
          const btn = e.target;
          btn.disabled = true;
          btn.textContent = "running…";
          try {
            const r = await fetch("/v1/quality/evaluate/" + encodeURIComponent(did), {
              method: "POST",
              headers: { Authorization: "Bearer " + getToken(), "Content-Type": "application/json" },
              body: JSON.stringify({ trigger: "manual" }),
            });
            if (!r.ok) throw new Error("http_" + r.status);
            const row = await r.json();
            body.innerHTML = renderCoachBody(row);
          } catch (err) {
            btn.disabled = false;
            btn.textContent = "Run eval now";
            body.insertAdjacentHTML("beforeend",
              `<p class="hint" style="color:#ff5c5c">eval failed: ${esc(err.message || err)}</p>`);
          }
        }, { once: true });
        return;
      }
      if (!res.ok) throw new Error("http_" + res.status);
      const row = await res.json();
      body.innerHTML = renderCoachBody(row);
    } catch (e) {
      body.innerHTML = `<p class="hint">failed to load eval: ${esc(e.message || e)}</p>`;
    }
  }, true);  // useCapture so it fires for nested <details>

  function renderCoachBody(row) {
    const rubric = row.rubric || {};
    const scoreCell = (label, v) => `
      <div class="coach-score">
        <div class="coach-score-label">${esc(label)}</div>
        <div class="coach-score-value">${v == null ? "—" : v + "/5"}</div>
      </div>`;
    const tags = (row.failure_tags || []).map(
      (t) => `<span class="failure-tag failure-tag-${esc(t)}">${esc(t.replace(/_/g, " "))}</span>`
    ).join(" ");
    const suggested = row.suggested_prompt
      ? `<div class="coach-suggest">
           <div class="coach-section-label">Suggested better prompt</div>
           <pre class="coach-suggest-text">${esc(row.suggested_prompt)}</pre>
           <button class="ghost coach-copy" data-text="${esc(row.suggested_prompt)}">Copy</button>
         </div>`
      : "";
    const notes = row.coach_notes
      ? `<div class="coach-notes"><b>Notes:</b> ${esc(row.coach_notes)}</div>`
      : "";
    const meta = `
      <div class="hint coach-meta">
        judge: ${esc(row.judge_provider || "")}/${esc(row.judge_model || "")}
        · ${row.judge_cost_usd != null ? "$" + Number(row.judge_cost_usd).toFixed(4) : "$—"}
        · trigger: ${esc(row.trigger || "?")}
        · ${row.judge_latency_ms != null ? row.judge_latency_ms + " ms" : ""}
      </div>`;
    const html = `
      <div class="coach-scores">
        ${scoreCell("Task understanding", rubric.task_understanding)}
        ${scoreCell("Task completion", rubric.task_completion)}
        ${scoreCell("Reasoning efficiency", rubric.reasoning_efficiency)}
        ${scoreCell("Prompt clarity", rubric.prompt_clarity)}
      </div>
      ${tags ? `<div class="coach-tags">${tags}</div>` : ""}
      ${suggested}
      ${notes}
      ${meta}`;
    // Hook up Copy after innerHTML lands; the caller assigns innerHTML
    // synchronously, so attach via a microtask.
    queueMicrotask(() => {
      document.querySelectorAll(".coach-copy").forEach((btn) => {
        if (btn.dataset.wired === "1") return;
        btn.dataset.wired = "1";
        btn.addEventListener("click", (ev) => {
          ev.stopPropagation();
          navigator.clipboard.writeText(btn.dataset.text || "");
          const t = btn.textContent;
          btn.textContent = "✓ copied";
          setTimeout(() => { btn.textContent = t; }, 1500);
        });
      });
    });
    return html;
  }

  function fmtBytes(n) {
    if (n == null) return "—";
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
    return (n / (1024 * 1024)).toFixed(2) + " MB";
  }

  function renderPayloadAnatomy(pa) {
    if (!pa || !pa.totals) return '<p class="hint">no payload captured</p>';
    const total = pa.totals.bytes || 1;
    const userPct = (pa.totals.user_pct * 100).toFixed(2);

    // Stacked bar — by bytes, the unit that actually matters for what gets shipped.
    const bar = `
      <div class="audit-bar" style="margin-bottom:8px">
        ${segPct(pa.system.bytes, total, "system")}
        ${segPct(pa.tools.bytes, total, "tools")}
        ${segPct(pa.history.bytes, total, "history")}
        ${segPct(pa.user.bytes, total, "user")}
      </div>`;

    const summary = `
      <div class="anatomy-summary">
        <div><b>${fmtBytes(pa.totals.bytes)}</b> shipped · ~${pa.totals.tokens} tokens</div>
        <div class="hint">your typed bytes: <b>${userPct}%</b> of payload — the rest is system prompt, tool definitions, and conversation history</div>
      </div>`;

    const sections = [
      renderAnatomySection("System prompt", "system", pa.system, "agent's hidden instructions"),
      renderAnatomySection("Tool definitions", "tools", pa.tools, "schemas the model sees on every turn"),
      renderAnatomySection("Conversation history", "history", pa.history, "previous turns re-shipped each request"),
      renderAnatomySection("Current user turn", "user", pa.user, "what you actually typed"),
    ].join("");

    return bar + summary + '<div class="anatomy-sections">' + sections + "</div>";
  }

  function renderAnatomySection(label, key, section, sublabel) {
    const pct = ((section.bytes / Math.max(1, section.bytes + 1)) * 100).toFixed(1);
    const items = section.items || [];
    const detail = items.length ? items.map((it, i) => renderAnatomyItem(key, it, i)).join("") : '<div class="hint" style="padding:8px">empty</div>';
    return `
      <details class="anatomy-section">
        <summary>
          <span class="anatomy-swatch audit-seg-${key}"></span>
          <b>${esc(label)}</b>
          <span class="anatomy-meta">${fmtBytes(section.bytes)} · ${section.tokens} tok · ${section.count} item${section.count === 1 ? "" : "s"}</span>
          <span class="hint">${esc(sublabel)}</span>
        </summary>
        <div class="anatomy-items">${detail}</div>
      </details>`;
  }

  function renderAnatomyItem(key, it, idx) {
    if (key === "tools") {
      const schema = it.schema ? '<pre class="anatomy-content">' + esc(JSON.stringify(it.schema, null, 2)) + '</pre>' : "";
      return `
        <details class="anatomy-item">
          <summary><b>${esc(it.name)}</b> <span class="anatomy-meta">${fmtBytes(it.bytes)} · ${it.tokens} tok</span></summary>
          ${it.description ? '<div class="anatomy-desc">' + esc(it.description) + '</div>' : ""}
          ${schema}
        </details>`;
    }
    // system / history / user — content is text
    const role = it.role || "—";
    return `
      <details class="anatomy-item">
        <summary><span class="msg-role ${esc(role)}">${esc(role)}</span> <span class="anatomy-meta">#${idx} · ${fmtBytes(it.bytes)} · ${it.tokens} tok</span></summary>
        <pre class="anatomy-content">${esc(it.content || "")}</pre>
      </details>`;
  }

  function renderMessageBlocks(prompt_body) {
    let messages;
    try {
      const parsed = JSON.parse(prompt_body);
      messages = Array.isArray(parsed) ? parsed : parsed.messages;
    } catch (e) {
      return '<pre class="body-block">' + esc(prompt_body) + "</pre>";
    }
    if (!Array.isArray(messages)) return '<pre class="body-block">' + esc(prompt_body) + "</pre>";
    return messages.map(renderOneMessage).join("");
  }

  function renderOneMessage(msg, idx) {
    const role = msg.role || "?";
    const head = `<div class="msg-head"><span class="msg-role ${esc(role)}">${esc(role)}</span><span class="msg-meta">#${idx}</span></div>`;
    const blocks = renderContentBlocks(msg.content, msg);
    return `<div class="msg-block">${head}${blocks}</div>`;
  }

  function renderContentBlocks(content, msg) {
    // String content → one text block.
    if (typeof content === "string") {
      const chunks = [`<div class="block-text">${esc(content)}</div>`];
      // OpenAI Chat: assistant turns can also carry tool_calls separately.
      if (msg && Array.isArray(msg.tool_calls)) {
        for (const tc of msg.tool_calls) chunks.push(renderToolCallBlock(tc));
      }
      // OpenAI Chat: tool role messages have content + tool_call_id.
      if (msg && msg.role === "tool" && msg.tool_call_id) {
        return [renderToolResultBlock(msg.tool_call_id, content)].join("");
      }
      return chunks.join("");
    }
    if (!Array.isArray(content)) {
      return `<div class="block-text">${esc(JSON.stringify(content))}</div>`;
    }
    return content.map((blk) => {
      if (!blk || typeof blk !== "object") return "";
      const t = blk.type;
      if (t === "text" || t === "input_text" || t === "output_text") {
        return `<div class="block-text">${esc(blk.text || "")}</div>`;
      }
      if (t === "tool_use") {
        return renderToolCallBlock({ id: blk.id, function: { name: blk.name, arguments: JSON.stringify(blk.input || {}) } });
      }
      if (t === "tool_result") {
        const tx = typeof blk.content === "string" ? blk.content : JSON.stringify(blk.content);
        return renderToolResultBlock(blk.tool_use_id || blk.tool_call_id, tx);
      }
      if (t === "image" || t === "image_url" || t === "input_image") {
        const src = (blk.source && blk.source.media_type) || "image";
        return `<div class="block-image">[image · ${esc(src)}]</div>`;
      }
      return `<div class="block-text">${esc(JSON.stringify(blk))}</div>`;
    }).join("");
  }

  function renderToolCallBlock(tc) {
    const name = tc?.function?.name || tc?.name || "?";
    const args = tc?.function?.arguments || tc?.arguments || "";
    const id = tc?.id || "";
    return `<div class="block-tool">
      <div class="block-tool-head">→ tool_call · ${esc(name)}${id ? ' <span style="font-weight:400;color:var(--text-dim)">· ' + esc(id) + '</span>' : ''}</div>
      <div class="block-tool-args">${esc(args)}</div>
    </div>`;
  }

  function renderToolResultBlock(tool_id, content) {
    return `<div class="block-tool-result">
      <div class="block-tool-head">← tool_result${tool_id ? ' · <span style="font-weight:400;color:var(--text-dim)">' + esc(tool_id) + '</span>' : ''}</div>
      ${esc(content || "")}
    </div>`;
  }

  function renderResponseBlocks(d) {
    // Streaming responses store the assembled text in response_body and the
    // structured tool_calls separately. Non-streaming stores the full JSON
    // response — we parse and render its message.content + tool_calls.
    const calls = d.tool_calls_made || [];
    const head = `<div class="msg-head"><span class="msg-role assistant">assistant</span><span class="msg-meta">${esc(d.decision_provider || "")} / ${esc(d.decision_model || "")}</span></div>`;
    const inner = [];

    let textContent = "";
    if (d.response_body) {
      // Try to parse as JSON (non-streaming). If that yields a chat completion,
      // pull the message. Otherwise treat as plain text (streaming case).
      try {
        const parsed = JSON.parse(d.response_body);
        if (parsed && Array.isArray(parsed.choices) && parsed.choices[0]?.message) {
          const m = parsed.choices[0].message;
          textContent = typeof m.content === "string" ? m.content : "";
          if (Array.isArray(m.tool_calls) && !calls.length) {
            for (const tc of m.tool_calls) inner.push(renderToolCallBlock(tc));
          }
        } else {
          textContent = d.response_body;
        }
      } catch (e) {
        textContent = d.response_body;
      }
    }
    if (textContent) inner.unshift(`<div class="block-text">${esc(textContent)}</div>`);
    for (const tc of calls) {
      inner.push(renderToolCallBlock({
        id: tc.id,
        function: { name: tc.name, arguments: tc.arguments || "" },
      }));
    }
    if (!inner.length) return '<p class="hint">no response body captured</p>';
    return `<div class="msg-block">${head}${inner.join("")}</div>`;
  }

  function segPct(value, total, kind) {
    if (!value) return "";
    const pct = Math.max(2, (value / total) * 100);
    return `<div class="audit-seg-${kind}" style="width:${pct}%" title="${kind}: ${value}"></div>`;
  }

  function inboundEndpoint(fmt) {
    if (fmt === "openai_chat") return "POST /v1/chat/completions";
    if (fmt === "anthropic") return "POST /v1/messages";
    if (fmt === "openai_responses") return "POST /v1/responses";
    return fmt || "—";
  }

  // --- Cost ---------------------------------------------------------------

  let costChart = null;
  let costWindow = { hours: 24, bucket: "hour" };
  // Selected agent for the Cost tab; "*" = aggregate across all agents.
  let costAgent = "*";
  // Selected project for the Cost tab; "*" = no project filter.
  let costProject = "*";

  // Scope the time-window buttons to the Cost tab only so we don't capture
  // the Privacy tab's buttons too.
  document.querySelectorAll("#tab-cost .window-buttons button").forEach((b) => {
    b.addEventListener("click", () => {
      costWindow = { hours: Number(b.dataset.window), bucket: b.dataset.bucket };
      document
        .querySelectorAll("#tab-cost .window-buttons button")
        .forEach((x) => x.classList.toggle("active", x === b));
      loadCost();
    });
  });

  document.getElementById("cost-agent-select")?.addEventListener("change", (e) => {
    costAgent = e.target.value || "*";
    loadCost();
  });

  document.getElementById("cost-project-select")?.addEventListener("change", (e) => {
    costProject = e.target.value || "*";
    loadCost();
  });

  async function loadCostAgentOptions() {
    const sel = document.getElementById("cost-agent-select");
    if (!sel) return;
    try {
      const data = await api("/v1/agents");
      const items = data.items || [];
      // Preserve current selection across reloads.
      const current = sel.value || "*";
      const allOpt = '<option value="*">All agents</option>';
      const opts = items.map(a => {
        const label = `${esc(a.agent_id)} (${a.call_count_30d} calls / 30d)`;
        return `<option value="${esc(a.agent_id)}">${label}</option>`;
      }).join("");
      sel.innerHTML = allOpt + opts;
      sel.value = current;
      costAgent = sel.value || "*";
    } catch (e) {
      /* keep default */
    }
  }

  async function loadCostProjectOptions() {
    const sel = document.getElementById("cost-project-select");
    if (!sel) return;
    try {
      const data = await api("/v1/projects");
      const items = data.items || [];
      const current = sel.value || "*";
      const allOpt = '<option value="*">All projects</option>';
      const opts = items.map(p => {
        const cost = (p.total_cost_usd_30d || 0).toFixed(3);
        const agents = (p.agents || []).join("+");
        const label = `${esc(p.project_id)} · ${p.key_count}k · ${agents} · $${cost}/30d`;
        return `<option value="${esc(p.project_id)}">${label}</option>`;
      }).join("");
      sel.innerHTML = allOpt + opts;
      sel.value = current;
      costProject = sel.value || "*";
    } catch (e) {
      /* keep default */
    }
  }

  // --- Scorecard (brain layer) -------------------------------------------

  document.getElementById("sc-reload").addEventListener("click", () => loadScorecard());

  async function loadScorecard() {
    const tbody = document.getElementById("sc-tbody");
    tbody.innerHTML = '<tr><td colspan="8" class="hint">loading…</td></tr>';
    try {
      const data = await api("/v1/scorecard");
      const items = data.items || [];
      if (!items.length) {
        tbody.innerHTML = '<tr><td colspan="8" class="hint">No scorecard data yet — make a few requests via /v1/chat/completions and they\'ll show up here.</td></tr>';
        return;
      }
      tbody.innerHTML = items.map(renderScorecardRow).join("");
      // Wire up incident click → audit detail.
      tbody.querySelectorAll("[data-incident-decision]").forEach(el => {
        el.addEventListener("click", (e) => {
          e.stopPropagation();
          const did = el.getAttribute("data-incident-decision");
          openDecisionDetail(did);
        });
      });
    } catch (e) {
      tbody.innerHTML = `<tr><td colspan="8" class="hint">load failed: ${esc(e.message || e)}</td></tr>`;
    }
  }

  function renderScorecardRow(r) {
    const score = r.score.toFixed(3);
    const scoreColor = r.score < 0.30 ? "#ff5c5c" : r.score < 0.45 ? "#f0b132" : r.score > 0.55 ? "#4caf50" : "#888";
    const statusBadge = r.is_demoted
      ? '<span class="sc-badge sc-demoted">demoted</span>'
      : r.score > 0.55
        ? '<span class="sc-badge sc-trusted">trusted</span>'
        : '<span class="sc-badge sc-neutral">neutral</span>';
    const waste = r.total_waste_usd > 0
      ? '<span class="sc-waste">$' + r.total_waste_usd.toFixed(4) + '</span>'
      : '<span class="hint">$0</span>';
    const incidents = (r.recent_incidents || []).length === 0
      ? '<span class="hint">none</span>'
      : r.recent_incidents.map(i => {
          const sev = i.severity || "info";
          return `<span class="sc-incident sc-sev-${esc(sev)}" data-incident-decision="${esc(i.decision_id)}" title="penalty -${i.score_penalty.toFixed(3)} · waste $${(i.estimated_waste_usd || 0).toFixed(4)} · ${i.ts}">${esc(i.finding_type)}</span>`;
        }).join(" ");
    return `
      <tr>
        <td>${esc(r.provider)}</td>
        <td><b>${esc(r.model)}</b></td>
        <td>${esc(r.tier)}</td>
        <td><span style="color:${scoreColor};font-family:var(--mono);font-weight:600">${score}</span></td>
        <td>${r.sample_size}</td>
        <td>${waste}</td>
        <td>${statusBadge}</td>
        <td>${incidents}</td>
      </tr>`;
  }

  // Open the decision detail drawer for a decision_id (used by scorecard click-through).
  async function openDecisionDetail(decisionId) {
    document.getElementById("detail-id").textContent = decisionId;
    document.getElementById("detail-body").innerHTML = '<p class="hint">loading…</p>';
    drawer.classList.remove("hidden");
    try {
      const d = await api(`/v1/decisions/${decisionId}`);
      document.getElementById("detail-body").innerHTML = renderDetail(d);
    } catch (e) {
      document.getElementById("detail-body").innerHTML = `<p class="hint">load failed: ${esc(e.message || e)}</p>`;
    }
  }

  // --- Drift (behavior-change detection) ---------------------------------

  document.getElementById("dr-reload").addEventListener("click", () => loadDrift());
  document.getElementById("dr-report-btn")?.addEventListener("click", () => _generateDriftReport(false));

  async function _generateDriftReport(forceRerun) {
    const btn = document.getElementById("dr-report-btn");
    if (!btn) return;
    btn.disabled = true;
    const prev = btn.textContent;
    btn.textContent = "running canaries…";
    try {
      const res = await fetch("/v1/drift/report", {
        method: "POST",
        headers: { Authorization: "Bearer " + getToken(), "Content-Type": "application/json" },
        body: JSON.stringify({ force_rerun: !!forceRerun }),
      });
      if (!res.ok) {
        const txt = await res.text();
        throw new Error("http_" + res.status + ": " + txt.slice(0, 200));
      }
      const data = await res.json();
      _showReportModal(data);
    } catch (e) {
      alert("Report generation failed: " + (e.message || e));
    } finally {
      btn.disabled = false;
      btn.textContent = prev;
    }
  }

  function _showReportModal(data) {
    document.getElementById("dr-report-modal")?.remove();
    const md = data.markdown || "";
    const cost = data.total_canary_cost_usd != null
      ? "$" + Number(data.total_canary_cost_usd).toFixed(4)
      : "$0.0000";
    const wrap = document.createElement("div");
    wrap.id = "dr-report-modal";
    wrap.className = "dr-report-modal";
    wrap.innerHTML = `
      <div class="dr-report-content">
        <div class="dr-report-head">
          <span>Drift report · ${(data.items || []).length} models probed · canary cost ${esc(cost)}</span>
          <div class="dr-report-actions">
            <button class="ghost dr-share-btn" id="dr-report-share" title="Open the Twitter-ready HTML report in a new tab — screenshot it from there">🖼 share view (HTML)</button>
            <button class="ghost" id="dr-report-rerun" title="Re-run canaries (ignore cached 24h investigations)">↻ rerun</button>
            <button class="ghost" id="dr-report-copy">📋 copy markdown</button>
            <button class="ghost" id="dr-report-download">💾 download</button>
            <button class="ghost" id="dr-report-close">✕ close</button>
          </div>
        </div>
        <textarea class="dr-report-md" readonly>${esc(md)}</textarea>
      </div>`;
    document.body.appendChild(wrap);
    document.getElementById("dr-report-close").addEventListener("click", () => wrap.remove());
    document.getElementById("dr-report-copy").addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(md); } catch (_e) {}
      const b = document.getElementById("dr-report-copy");
      const t = b.textContent;
      b.textContent = "✓ copied";
      setTimeout(() => { b.textContent = t; }, 1500);
    });
    document.getElementById("dr-report-download").addEventListener("click", () => {
      const blob = new Blob([md], { type: "text/markdown" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      const stamp = (data.generated_at || "").replace(/[:T]/g, "-").slice(0, 16);
      a.download = `nautgate-drift-report-${stamp}.md`;
      document.body.appendChild(a);
      a.click();
      a.remove();
    });
    document.getElementById("dr-report-rerun").addEventListener("click", () => {
      wrap.remove();
      _generateDriftReport(true);
    });
    document.getElementById("dr-report-share").addEventListener("click", () => {
      const token = getToken();
      const url = "/v1/drift/report.html?token=" + encodeURIComponent(token);
      window.open(url, "_blank", "noopener");
    });
    wrap.addEventListener("click", (e) => { if (e.target === wrap) wrap.remove(); });
  }

  function fmtNum(v) {
    if (v == null) return "—";
    if (Math.abs(v) >= 1000) return v.toFixed(0);
    if (Math.abs(v) >= 1) return v.toFixed(2);
    if (Math.abs(v) >= 0.001) return v.toFixed(4);
    return v.toExponential(2);
  }

  function fmtAge(iso) {
    if (!iso) return "—";
    const dt = new Date(iso);
    const ageMs = Date.now() - dt.getTime();
    const sec = Math.floor(ageMs / 1000);
    if (sec < 60) return sec + "s ago";
    if (sec < 3600) return Math.floor(sec / 60) + "m ago";
    if (sec < 86400) return Math.floor(sec / 3600) + "h ago";
    return Math.floor(sec / 86400) + "d ago";
  }

  async function loadDrift() {
    const alertsEl = document.getElementById("dr-alerts");
    const histTbody = document.getElementById("dr-alerts-tbody");
    const baseTbody = document.getElementById("dr-baselines-tbody");
    alertsEl.innerHTML = '<p class="hint">loading…</p>';
    histTbody.innerHTML = '<tr><td colspan="10" class="hint">loading…</td></tr>';
    baseTbody.innerHTML = '<tr><td colspan="9" class="hint">loading…</td></tr>';
    try {
      const data = await api("/v1/drift");
      const alerts = data.alerts || [];
      const baselines = data.baselines || [];

      // Open alerts hero panel
      const open = alerts.filter(a => a.is_open);
      if (!open.length) {
        alertsEl.innerHTML = '<p class="hint">No open alerts. Drift detection needs ~10 samples per (provider, model, metric) to warm up.</p>';
      } else {
        alertsEl.innerHTML = open.map(renderOpenAlert).join("");
        _wireInvestigateButtons();
        // Auto-load the latest investigation for EVERY open alert (incl.
        // compaction events). Without this, results disappear when you
        // switch tabs and come back.
        _autoLoadLatestInvestigations(open.map(a => a.id));
      }

      // History table — all alerts.
      if (!alerts.length) {
        histTbody.innerHTML = '<tr><td colspan="10" class="hint">No alerts yet.</td></tr>';
      } else {
        histTbody.innerHTML = alerts.map(renderAlertHistoryRow).join("");
      }

      // Baselines table.
      if (!baselines.length) {
        baseTbody.innerHTML = '<tr><td colspan="9" class="hint">No baselines yet — make some requests through /v1/chat/completions.</td></tr>';
      } else {
        baseTbody.innerHTML = baselines.map(renderBaselineRow).join("");
      }
    } catch (e) {
      alertsEl.innerHTML = `<p class="hint">load failed: ${esc(e.message || e)}</p>`;
    }
  }

  function renderOpenAlert(a) {
    const arrow = a.direction === "up" ? "↑" : "↓";
    const compaction = a.metric === "messages_count_delta";
    const headline = compaction
      ? `<b>${esc(a.provider)}/${esc(a.model)}</b> compacted history (turns dropped by ${Math.abs(a.peak_observed)})`
      : `<b>${esc(a.provider)}/${esc(a.model)}</b> · <code>${esc(a.metric)}</code> ${arrow} drift (peak z = ${a.peak_z_score.toFixed(2)})`;
    const detail = compaction
      ? `${a.sample_count} compaction event${a.sample_count === 1 ? "" : "s"} since ${fmtAge(a.started_at)}`
      : `observed <b>${fmtNum(a.peak_observed)}</b> vs baseline <b>${fmtNum(a.baseline_at_alert)}</b> · ${a.sample_count} samples since ${fmtAge(a.started_at)}`;
    const aid = esc(a.id || "");
    const provider = esc(a.provider || "");
    const model = esc(a.model || "");
    const metric = esc(a.metric || "");
    // Compaction events get a tagged note instead of an Investigate button.
    // Compaction events are client-side, but the Investigate button can
    // still reveal routing changes / tokenizer drift on the same model —
    // so we always render it, with a tag when compaction is the trigger.
    const compactionTag = compaction
      ? '<span class="dr-tag">client-side event</span> '
      : "";
    const actions = `${compactionTag}<button class="dr-investigate ghost" data-alert="${aid}" data-provider="${provider}" data-model="${model}" data-metric="${metric}">🔍 Investigate</button>`;
    return `
      <div class="dr-alert dr-alert-${esc(a.direction)}" data-alert-id="${aid}">
        <div class="dr-alert-head">${arrow} ${headline}</div>
        <div class="dr-alert-detail">${detail}</div>
        <div class="dr-alert-actions">${actions}</div>
        <div class="dr-investigation-slot" id="dr-inv-slot-${aid}"></div>
      </div>`;
  }

  // After alerts render, attach click handlers + auto-load recent investigations.
  function _wireInvestigateButtons() {
    document.querySelectorAll(".dr-investigate").forEach((btn) => {
      btn.addEventListener("click", async (ev) => {
        ev.stopPropagation();
        const aid = btn.dataset.alert;
        const provider = btn.dataset.provider;
        const model = btn.dataset.model;
        const metric = btn.dataset.metric;
        const slot = document.getElementById("dr-inv-slot-" + aid);
        btn.disabled = true;
        btn.textContent = "running…";
        slot.innerHTML = '<div class="dr-investigation loading">Running canary suite — usually 5-15 seconds…</div>';
        try {
          const res = await fetch("/v1/drift/investigate", {
            method: "POST",
            headers: { Authorization: "Bearer " + getToken(), "Content-Type": "application/json" },
            body: JSON.stringify({ alert_id: aid, provider, model, metric_name: metric }),
          });
          if (!res.ok) {
            const txt = await res.text();
            throw new Error("http_" + res.status + ": " + txt.slice(0, 120));
          }
          const j = await res.json();
          await _pollInvestigation(j.investigation_id, slot);
        } catch (e) {
          slot.innerHTML = `<div class="dr-investigation error">eval failed: ${esc(e.message || e)}</div>`;
        } finally {
          btn.disabled = false;
          btn.textContent = "🔍 Investigate again";
        }
      });
    });
  }

  async function _pollInvestigation(iid, slot) {
    for (let i = 0; i < 40; i++) {  // up to ~60s
      try {
        const inv = await api("/v1/drift/investigations/" + encodeURIComponent(iid));
        if (inv.status === "complete" || inv.status === "failed" || inv.status === "skipped") {
          slot.innerHTML = _renderInvestigation(inv);
          return;
        }
      } catch (_e) {}
      await new Promise(r => setTimeout(r, 1500));
    }
    slot.innerHTML = '<div class="dr-investigation error">timed out waiting for verdict</div>';
  }

  function _renderInvestigation(inv) {
    if (inv.status === "skipped") {
      return `<div class="dr-investigation skipped">Skipped: ${esc(inv.skip_reason || "unknown")}</div>`;
    }
    if (inv.status === "failed") {
      return `<div class="dr-investigation error">Failed: ${esc(inv.verdict_text || "")}</div>`;
    }
    const findings = inv.findings || {};
    const canaries = findings.canaries || [];
    const labelClass = (inv.verdict_label || "").startsWith("matches_baseline")
      ? "ok"
      : (inv.verdict_label || "") === "inconclusive" ? "neutral" : "bad";
    let extras = "";
    if (findings.tokenizer) {
      const t = findings.tokenizer;
      const base = findings.baseline_tokens_per_byte;
      extras += `
        <div class="dr-finding-row">
          <span class="dr-finding-label">tokens/byte (now)</span>
          <span class="dr-finding-value">${t.current.toFixed(3)}</span>
        </div>
        <div class="dr-finding-row">
          <span class="dr-finding-label">tokens/byte (baseline)</span>
          <span class="dr-finding-value">${base != null ? base.toFixed(3) : "—"}</span>
        </div>`;
      if (findings.delta_pct != null) {
        extras += `
          <div class="dr-finding-row">
            <span class="dr-finding-label">change</span>
            <span class="dr-finding-value">${findings.delta_pct >= 0 ? "+" : ""}${findings.delta_pct.toFixed(1)}%</span>
          </div>`;
      }
    }
    if (findings.verbosity) {
      extras += `
        <div class="dr-finding-row">
          <span class="dr-finding-label">avg response (bytes)</span>
          <span class="dr-finding-value">${findings.verbosity.avg_response_bytes.toFixed(0)}</span>
        </div>`;
    }
    if (findings.refusal) {
      extras += `
        <div class="dr-finding-row">
          <span class="dr-finding-label">refusal rate</span>
          <span class="dr-finding-value">${(findings.refusal.refusal_rate * 100).toFixed(0)}% (${findings.refusal.refused_count}/${findings.refusal.samples})</span>
        </div>`;
    }
    if (findings.latency) {
      const l = findings.latency;
      extras += `
        <div class="dr-finding-row">
          <span class="dr-finding-label">first byte p50/p95</span>
          <span class="dr-finding-value">${l.first_byte_ms_p50}ms / ${l.first_byte_ms_p95}ms</span>
        </div>`;
    }
    if (findings.routing) {
      extras += '<div class="dr-finding-row"><span class="dr-finding-label">routing comparison</span><span class="dr-finding-value">';
      for (const [via, stats] of Object.entries(findings.routing)) {
        extras += `<div>${esc(via)}: tokens/byte ${stats.avg_tokens_per_byte != null ? stats.avg_tokens_per_byte.toFixed(3) : "—"}</div>`;
      }
      extras += "</span></div>";
    }
    const canaryRows = canaries.slice(0, 6).map(c => `
      <tr>
        <td>${esc(c.canary)}</td>
        <td>${esc(c.via)}</td>
        <td>${c.prompt_bytes || "—"}</td>
        <td>${c.prompt_tokens != null ? c.prompt_tokens : "—"}</td>
        <td>${c.completion_tokens != null ? c.completion_tokens : "—"}</td>
        <td>${c.duration_ms != null ? c.duration_ms + "ms" : "—"}</td>
        <td>${c.cost_usd != null ? "$" + c.cost_usd.toFixed(4) : (c.via.endsWith("oauth") ? "$0 (Max)" : "—")}</td>
        <td title="${esc(c.error || c.response_excerpt || "")}">${c.error ? "✗" : "✓"}</td>
      </tr>`).join("");
    return `
      <div class="dr-investigation complete">
        <div class="dr-verdict dr-verdict-${labelClass}">
          <div class="dr-verdict-label">${esc(inv.verdict_label || "inconclusive")}</div>
          <div class="dr-verdict-text">${esc(inv.verdict_text || "")}</div>
        </div>
        <div class="dr-findings">${extras}</div>
        <details class="dr-canary-details"><summary>canary runs (${canaries.length})</summary>
          <table class="dr-canary-table">
            <thead><tr><th>canary</th><th>via</th><th>bytes</th><th>in tk</th><th>out tk</th><th>dur</th><th>cost</th><th></th></tr></thead>
            <tbody>${canaryRows}</tbody>
          </table>
        </details>
        <div class="hint dr-investigation-meta">
          suite: ${esc(inv.canary_suite || "")} · trigger: ${esc(inv.triggered_by || "")} · total cost: ${inv.total_cost_usd != null ? "$" + inv.total_cost_usd.toFixed(4) : "$0.0000"}
        </div>
      </div>`;
  }

  async function _autoLoadLatestInvestigations(alertIds) {
    if (!alertIds.length) return;
    for (const aid of alertIds) {
      try {
        const data = await api("/v1/drift/investigations?alert_id=" + encodeURIComponent(aid) + "&limit=1");
        if (!data.items || !data.items.length) continue;
        const latest = data.items[0];
        if (latest.status === "complete") {
          // Fetch full row for findings.
          const full = await api("/v1/drift/investigations/" + encodeURIComponent(latest.id));
          const slot = document.getElementById("dr-inv-slot-" + aid);
          if (slot) slot.innerHTML = _renderInvestigation(full);
        }
      } catch (_e) {}
    }
  }

  function renderAlertHistoryRow(a) {
    const status = a.is_open
      ? '<span class="dr-status dr-open">OPEN</span>'
      : '<span class="dr-status dr-resolved">resolved</span>';
    const arrow = a.direction === "up" ? "↑" : "↓";
    return `
      <tr>
        <td>${esc(a.provider)}</td>
        <td>${esc(a.model)}</td>
        <td><code>${esc(a.metric)}</code></td>
        <td>${arrow}</td>
        <td>${a.peak_z_score === -99 ? "compaction" : a.peak_z_score.toFixed(2)}</td>
        <td>${fmtNum(a.peak_observed)}</td>
        <td>${fmtNum(a.baseline_at_alert)}</td>
        <td>${a.sample_count}</td>
        <td>${fmtAge(a.started_at)}</td>
        <td>${status}</td>
      </tr>`;
  }

  function renderBaselineRow(b) {
    const zClass = b.last_z_score == null
      ? ""
      : Math.abs(b.last_z_score) > 3 ? "style=\"color:#ff5c5c\""
        : Math.abs(b.last_z_score) > 2 ? "style=\"color:#f0b132\"" : "";
    return `
      <tr>
        <td>${esc(b.provider)}</td>
        <td>${esc(b.model)}</td>
        <td><code>${esc(b.metric)}</code></td>
        <td>${fmtNum(b.mean)}</td>
        <td>${fmtNum(b.stddev)}</td>
        <td>${b.sample_count}</td>
        <td>${fmtNum(b.last_observed)}</td>
        <td ${zClass}>${b.last_z_score == null ? "—" : b.last_z_score.toFixed(2)}</td>
        <td>${fmtAge(b.updated_at)}</td>
      </tr>`;
  }

  // --- OpenRouter credits (Cost tab) ---------------------------------
  document.getElementById("or-credits-reload")?.addEventListener("click", () => loadOpenRouterCredits());

  async function loadOpenRouterCredits() {
    const card = document.getElementById("or-credits-card");
    if (!card) return;
    try {
      const data = await api("/v1/cost/openrouter-balance");
      if (data.error) {
        card.hidden = true;
        return;
      }
      card.hidden = false;
      const remaining = Number(data.remaining_usd || 0);
      const total = Number(data.total_credits || 0);
      const burn = data.daily_burn_usd != null ? Number(data.daily_burn_usd) : null;
      const days = data.days_left_at_current_burn != null ? Number(data.days_left_at_current_burn) : null;
      document.getElementById("or-credits-remaining").textContent = "$" + remaining.toFixed(2);
      document.getElementById("or-credits-remaining-sub").textContent =
        total > 0 ? `remaining of $${total.toFixed(2)} purchased` : "remaining";
      document.getElementById("or-credits-burn").textContent =
        burn != null ? "$" + burn.toFixed(2) + "/d" : "—";
      const daysEl = document.getElementById("or-credits-days-left");
      if (days != null && isFinite(days)) {
        daysEl.textContent = days >= 1
          ? Math.round(days) + " days"
          : "<1 day";
      } else {
        daysEl.textContent = "—";
      }
      // Severity colouring on the days-left + bar.
      const ratio = total > 0 ? (remaining / total) : 0;
      const fill = document.getElementById("or-credits-bar-fill");
      fill.style.width = (ratio * 100).toFixed(1) + "%";
      let level = "ok";
      if (days != null && days < 7) level = "warn";
      if (days != null && days < 3) level = "critical";
      if (ratio < 0.1) level = "critical";
      card.dataset.level = level;
    } catch (_e) {
      const card = document.getElementById("or-credits-card");
      if (card) card.hidden = true;
    }
  }

  async function loadCost() {
    if (!getToken()) return;
    // Refresh dropdowns on every load so new keys/projects appear.
    await Promise.all([loadCostAgentOptions(), loadCostProjectOptions()]);
    // Fire-and-forget: OpenRouter balance refreshes independently.
    loadOpenRouterCredits();
    const scope = costAgent || "*";
    const project = costProject || "*";
    const qs = `&agent_id=${encodeURIComponent(scope)}&project_id=${encodeURIComponent(project)}`;
    try {
      const [summary, ts] = await Promise.all([
        api(`/v1/cost/summary?hours=${costWindow.hours}${qs}`),
        api(
          `/v1/cost/timeseries?hours=${costWindow.hours}&bucket=${costWindow.bucket}${qs}`
        ),
      ]);
      renderCostSummary(summary);
      renderCostChart(ts);
    } catch (e) {
      /* swallow; auth chip explains */
    }
  }

  // --- Cache tab ---------------------------------------------------------
  let cacheWindow = 24;

  function pct(n) {
    if (n === null || n === undefined) return "—";
    return (n * 100).toFixed(1) + "%";
  }
  function shortHash(h) {
    return h ? h.slice(0, 10) : "—";
  }
  function ms(v) {
    return v === null || v === undefined ? "—" : Math.round(v).toLocaleString() + " ms";
  }

  async function loadCache() {
    if (!getToken()) return;
    const model = document.getElementById("cache-model-filter")?.value || "*";
    const mq = model && model !== "*" ? `&model=${encodeURIComponent(model)}` : "";
    try {
      const [summary, prefixes] = await Promise.all([
        api(`/v1/cache/summary?hours=${cacheWindow}${mq}`),
        api(`/v1/cache/prefixes?hours=${cacheWindow}`),
      ]);
      renderCacheSummary(summary);
      renderCachePrefixes(prefixes);
    } catch (e) {
      /* swallow; auth chip explains */
    }
  }

  function renderCacheSummary(s) {
    const t = s.totals || {};
    document.getElementById("ch-hitrate").textContent = pct(t.hit_rate);
    const savedEl = document.getElementById("ch-saved");
    savedEl.textContent = usd(t.saved_usd);
    savedEl.title =
      t.cache_off_usd != null
        ? `Cache off: ${usd(t.cache_off_usd)} → cache on: ${usd(t.cache_on_usd)} (input-side). Saved = the difference.`
        : "Read discount minus the cache-write premium. Positive = caching is a net win.";
    document.getElementById("ch-split").textContent =
      `${(t.fresh_tokens || 0).toLocaleString()} / ${(t.cache_read_tokens || 0).toLocaleString()} / ${(t.cache_write_tokens || 0).toLocaleString()}`;
    document.getElementById("ch-ratio").textContent =
      t.write_read_ratio == null ? "—" : t.write_read_ratio.toFixed(2);

    // Populate the model filter once (preserve current selection).
    const sel = document.getElementById("cache-model-filter");
    if (sel && sel.options.length <= 1) {
      const cur = sel.value;
      for (const r of s.by_model) {
        const o = document.createElement("option");
        o.value = r.model;
        o.textContent = r.model;
        sel.appendChild(o);
      }
      sel.value = cur;
    }

    const tbody = document.querySelector("#cache-model tbody");
    tbody.innerHTML = (s.by_model || []).map((r) => `
      <tr>
        <td><b>${esc(r.model || "—")}</b></td>
        <td>${pct(r.hit_rate)}</td>
        <td>${(r.fresh_tokens || 0).toLocaleString()}</td>
        <td>${(r.cache_read_tokens || 0).toLocaleString()}</td>
        <td>${(r.cache_write_tokens || 0).toLocaleString()}</td>
        <td class="cost-notional">${usd(r.cache_off_usd)}</td>
        <td>${usd(r.cache_on_usd)}</td>
        <td class="cache-saved">${usd(r.saved_usd)}</td>
        <td>${r.calls || 0}</td>
      </tr>`).join("") || `<tr><td colspan="9" class="hint">No cached calls in this window.</td></tr>`;
  }

  function reuseLabel(reads, writes) {
    if (!writes) return reads ? "read-only" : "—";
    return (reads / writes).toFixed(1) + "×";
  }

  function renderCachePrefixes(p) {
    const reused = document.querySelector("#cache-reused tbody");
    reused.innerHTML = (p.top_reused || []).map((r) => `
      <tr>
        <td><code title="${esc(r.prefix_hash)}">${shortHash(r.prefix_hash)}</code></td>
        <td>${esc(r.model || "—")}</td>
        <td>${(r.reads || 0).toLocaleString()}</td>
        <td>${(r.writes || 0).toLocaleString()}</td>
        <td>${r.reuse_ratio == null ? "—" : r.reuse_ratio.toFixed(1) + "×"}</td>
        <td>${r.calls || 0}</td>
      </tr>`).join("") || `<tr><td colspan="6" class="hint">No cacheable prefixes seen yet.</td></tr>`;

    const leaky = document.querySelector("#cache-leaky tbody");
    leaky.innerHTML = (p.leaky || []).map((r) => `
      <tr class="leak-row">
        <td><code title="${esc(r.prefix_hash)}">${shortHash(r.prefix_hash)}</code></td>
        <td>${esc(r.model || "—")}</td>
        <td>${(r.writes || 0).toLocaleString()}</td>
        <td>${(r.reads || 0).toLocaleString()}</td>
        <td>${r.reuse_ratio == null ? "0×" : r.reuse_ratio.toFixed(1) + "×"}</td>
        <td>${r.calls || 0}</td>
      </tr>`).join("") || `<tr><td colspan="6" class="hint">No leaks detected — prefixes are reused well.</td></tr>`;

    const warmth = document.querySelector("#cache-warmth tbody");
    warmth.innerHTML = (p.latency || []).map((r) => {
      // Flag a wide spread relative to the median as a cold/thrashing cache.
      const cold = r.ttft_p50_ms && r.ttft_spread_ms != null && r.ttft_spread_ms > r.ttft_p50_ms;
      return `
      <tr${cold ? ' class="leak-row"' : ""}>
        <td><code title="${esc(r.prefix_hash)}">${shortHash(r.prefix_hash)}</code></td>
        <td>${esc(r.model || "—")}</td>
        <td>${ms(r.ttft_p50_ms)}</td>
        <td>${ms(r.ttft_spread_ms)}</td>
        <td>${ms(r.ttft_min_ms)}</td>
        <td>${ms(r.ttft_max_ms)}</td>
        <td>${r.ttft_n || 0}</td>
      </tr>`;
    }).join("") || `<tr><td colspan="7" class="hint">No timed prefixes yet (need ≥2 streamed calls sharing a prefix).</td></tr>`;
  }

  document.querySelectorAll("#tab-cache .window-buttons button").forEach((b) => {
    b.addEventListener("click", () => {
      cacheWindow = Number(b.dataset.window);
      document
        .querySelectorAll("#tab-cache .window-buttons button")
        .forEach((x) => x.classList.toggle("active", x === b));
      loadCache();
    });
  });
  document.getElementById("cache-model-filter")?.addEventListener("change", loadCache);
  document.getElementById("cache-reload")?.addEventListener("click", loadCache);

  // --- LLM Probing -------------------------------------------------------
  async function loadProbe() {
    if (!getToken()) return;
    try {
      const s = await api("/v1/probe/summary?hours=168");
      const cfg = s.config || {};
      document.getElementById("probe-enabled").checked = !!cfg.enabled;
      document.getElementById("probe-interval").value = cfg.interval_hours || 6;
      const tEl = document.getElementById("probe-targets");
      if (document.activeElement !== tEl) tEl.value = (cfg.targets || []).join("\n");
      document.getElementById("probe-state").textContent =
        cfg.last_run_at ? "last run " + new Date(cfg.last_run_at).toLocaleString() : "never run";

      const al = document.querySelector("#probe-alerts tbody");
      al.innerHTML = (s.alerts || []).filter(a => !a.resolved_at).map(a => `
        <tr class="${a.severity === 'critical' ? 'leak-row' : ''}">
          <td>${new Date(a.ts).toLocaleString()}</td>
          <td>${esc(a.model)}</td>
          <td><b>${esc(a.alert_type)}</b></td>
          <td>${esc(a.severity)}</td>
          <td><code>${esc(JSON.stringify(a.detail || {}))}</code></td>
        </tr>`).join("") || `<tr><td colspan="5" class="hint">No open alerts.</td></tr>`;

      const tb = document.querySelector("#probe-targets-tbl tbody");
      const rows = [];
      for (const t of s.targets || []) {
        const legs = Object.values(t.legs || {});
        if (!legs.length) continue;
        legs.forEach((leg, i) => {
          const mism = leg.observed_model && !modelsLooseMatch(t.model, leg.observed_model);
          rows.push(`<tr class="${mism ? 'leak-row' : ''}">
            <td>${i === 0 ? '<b>' + esc(t.model) + '</b>' : ''}</td>
            <td>${esc(leg.via)}</td>
            <td>${esc(leg.observed_model || (leg.error ? '— (' + (leg.status_code||'err') + ')' : '—'))}</td>
            <td>${leg.tokens_per_byte != null ? leg.tokens_per_byte.toFixed(4) : '—'}</td>
            <td>${leg.first_byte_ms != null ? leg.first_byte_ms + ' ms' : '—'}</td>
            <td>${leg.quality_score != null ? leg.quality_score + '/5' : '—'}</td>
            <td>${leg.refused ? '⚠' : ''}</td>
          </tr>`);
        });
      }
      tb.innerHTML = rows.join("") || `<tr><td colspan="7" class="hint">No probe cycle yet — set targets, enable, and Run now.</td></tr>`;
    } catch (e) { /* auth chip explains */ }
  }

  function modelsLooseMatch(req, obs) {
    if (!obs) return true;
    const core = (m) => {
      m = m.toLowerCase();
      for (const p of ["openrouter/anthropic/", "openrouter/openai/", "openrouter/", "anthropic/", "openai/"])
        if (m.startsWith(p)) m = m.slice(p.length);
      return m;
    };
    const a = core(req), b = core(obs);
    return a.includes(b) || b.includes(a);
  }

  async function saveProbeConfig() {
    const targets = document.getElementById("probe-targets").value
      .split("\n").map(s => s.trim()).filter(Boolean);
    const body = {
      enabled: document.getElementById("probe-enabled").checked,
      interval_hours: Number(document.getElementById("probe-interval").value) || 6,
      targets,
    };
    document.getElementById("probe-state").textContent = "saving…";
    try {
      await apiPut("/v1/probe/config", body);
      document.getElementById("probe-state").textContent = "saved";
    } catch (e) {
      document.getElementById("probe-state").textContent = "save failed";
    }
  }

  async function runProbeNow() {
    const btn = document.getElementById("probe-run-now");
    btn.disabled = true; btn.textContent = "running…";
    try {
      await saveProbeConfig();
      const t = getToken();
      const res = await fetch("/v1/probe/run", {
        method: "POST", headers: { Authorization: "Bearer " + t, "Content-Type": "application/json" },
        body: "{}",
      });
      if (!res.ok) throw new Error("http_" + res.status);
      await loadProbe();
    } catch (e) {
      document.getElementById("probe-state").textContent = "run failed (" + e.message + ")";
    } finally {
      btn.disabled = false; btn.textContent = "▶ Run now";
    }
  }

  document.getElementById("probe-reload")?.addEventListener("click", loadProbe);
  document.getElementById("probe-save")?.addEventListener("click", saveProbeConfig);
  document.getElementById("probe-run-now")?.addEventListener("click", runProbeNow);

  function renderCostSummary(s) {
    document.getElementById("c-total").textContent = usd(s.total_cost_usd);
    const savingsEl = document.getElementById("c-savings");
    if (savingsEl) {
      savingsEl.textContent = usd(s.subscription_savings_usd);
      savingsEl.title = "What Anthropic/OpenAI metered billing WOULD have cost — covered by your Max subscription via OAuth passthrough.";
    }
    document.getElementById("c-calls").textContent = s.total_calls ?? 0;
    const avg =
      s.total_cost_usd && s.total_calls
        ? s.total_cost_usd / s.total_calls
        : null;
    document.getElementById("c-avg").textContent = usd(avg);
    document.getElementById("c-tokens").textContent =
      ((s.total_prompt_tokens || 0) + (s.total_completion_tokens || 0)).toLocaleString();
    const emptyEl = document.getElementById("c-empty");
    if (emptyEl) emptyEl.textContent = s.empty_count != null ? String(s.empty_count) : "—";
    const rlEl = document.getElementById("c-ratelimit");
    if (rlEl) rlEl.textContent = s.rate_limited_count != null ? String(s.rate_limited_count) : "—";

    fillCostProviderTable(s.by_provider);
    fillCostModelTable(s.by_model);
    fillCostTierTable("cost-tier", s.by_tier);

    // by_agent table — only rendered when we asked for the aggregate view.
    const byAgent = s.by_agent || [];
    const showByAgent = byAgent.length > 0;
    document.getElementById("cost-by-agent-title").style.display = showByAgent ? "" : "none";
    document.getElementById("cost-agent").style.display = showByAgent ? "" : "none";
    if (showByAgent) {
      const tbody = document.querySelector("#cost-agent tbody");
      tbody.innerHTML = byAgent.map(r => `
        <tr>
          <td><b>${esc(r.key || "—")}</b></td>
          <td>${usd(r.cost_usd)}</td>
          <td>${r.calls || 0}</td>
          <td>${(r.prompt_tokens || 0).toLocaleString()} / ${(r.completion_tokens || 0).toLocaleString()}</td>
        </tr>`).join("");
    }

    // by_project table — appears when not filtered to a single project.
    const byProject = s.by_project || [];
    const showByProject = byProject.length > 0;
    document.getElementById("cost-by-project-title").style.display = showByProject ? "" : "none";
    document.getElementById("cost-project-tbl").style.display = showByProject ? "" : "none";
    if (showByProject) {
      const tbody = document.querySelector("#cost-project-tbl tbody");
      tbody.innerHTML = byProject.map(r => `
        <tr>
          <td><b>${esc(r.key || "—")}</b></td>
          <td>${usd(r.cost_usd)}</td>
          <td>${r.calls || 0}</td>
          <td>${(r.prompt_tokens || 0).toLocaleString()} / ${(r.completion_tokens || 0).toLocaleString()}</td>
        </tr>`).join("");
    }
  }

  function fillCostTable(id, rows, fields) {
    const tbody = document.querySelector("#" + id + " tbody");
    tbody.innerHTML = (rows || [])
      .map(
        (r) =>
          `<tr><td>${esc(r[fields[0]] || "—")}</td><td>${usd(r[fields[1]])}</td><td>${r[fields[2]] || 0}</td></tr>`
      )
      .join("");
  }

  function fillCostProviderTable(rows) {
    const tbody = document.querySelector("#cost-provider tbody");
    tbody.innerHTML = (rows || [])
      .map((r) => `
        <tr>
          <td>${esc(r.key || "—")}</td>
          <td>${usd(r.cost_usd)}</td>
          <td class="cost-notional">${r.notional_cost_usd != null ? usd(r.notional_cost_usd) : "—"}</td>
          <td>${r.calls || 0}</td>
        </tr>`)
      .join("");
  }

  function fillCostModelTable(rows) {
    const tbody = document.querySelector("#cost-model tbody");
    tbody.innerHTML = (rows || [])
      .map((r) => {
        const totalCalls = r.calls || 1;
        const dollarPerCall = (r.cost_usd || 0) / totalCalls;
        const latency = r.avg_latency_ms != null ? Math.round(r.avg_latency_ms) + " ms" : "—";
        const emptyPct = totalCalls > 0
          ? ((r.empty_count || 0) / totalCalls * 100).toFixed(0) + "%"
          : "—";
        return `
          <tr class="cost-model-row" data-model="${esc(r.key)}">
            <td><b>${esc(r.key || "—")}</b></td>
            <td>${usd(r.cost_usd)}</td>
            <td class="cost-notional">${r.notional_cost_usd != null ? usd(r.notional_cost_usd) : "—"}</td>
            <td>${totalCalls}</td>
            <td>${dollarPerCall > 0 ? "$" + dollarPerCall.toFixed(4) : "—"}</td>
            <td>${(r.prompt_tokens || 0).toLocaleString()} / ${(r.completion_tokens || 0).toLocaleString()}</td>
            <td>${latency}</td>
            <td>${r.empty_count || 0} (${emptyPct})</td>
          </tr>`;
      })
      .join("");
    // Wire row clicks → jump to Audit Log filtered by model.
    document.querySelectorAll(".cost-model-row").forEach((row) => {
      row.addEventListener("click", () => {
        const model = row.dataset.model;
        if (!model) return;
        activateTab("audit");
        // The audit tab doesn't expose a model filter yet; show a hint
        // toast so the user knows where to look. Click-through is best-effort
        // until the Audit tab supports model filtering directly.
        const hintEl = document.querySelector("#tab-audit .hint");
        if (hintEl) {
          const orig = hintEl.textContent;
          hintEl.textContent = `Filtered hint: showing recent calls. Look for model=${model}.`;
          setTimeout(() => { hintEl.textContent = orig; }, 4000);
        }
      });
    });
  }

  function fillCostTierTable(id, rows) {
    const tbody = document.querySelector("#" + id + " tbody");
    tbody.innerHTML = (rows || [])
      .map(
        (r) =>
          `<tr><td><span class="tag tier">${esc(r.key || "—")}</span></td><td>${usd(r.cost_usd)}</td><td>${r.calls || 0}</td><td>${(r.prompt_tokens || 0).toLocaleString()} / ${(r.completion_tokens || 0).toLocaleString()}</td></tr>`
      )
      .join("");
  }

  function renderCostChart(ts) {
    const canvas = document.getElementById("cost-chart");
    if (!canvas || !window.Chart) return;

    // Build a unified x-axis (all unique bucket timestamps, sorted).
    const allTs = new Set();
    ts.series.forEach((s) => s.points.forEach((p) => allTs.add(p.ts)));
    const labels = Array.from(allTs).sort();

    // Convert each series to a value array aligned to `labels`.
    const palette = ["#c2410c", "#10b981", "#f59e0b", "#ef4444", "#a78bfa", "#22d3ee"];
    const datasets = ts.series.map((s, i) => {
      const byTs = Object.fromEntries(s.points.map((p) => [p.ts, p.cost_usd]));
      return {
        label: s.provider,
        data: labels.map((t) => byTs[t] ?? 0),
        borderColor: palette[i % palette.length],
        backgroundColor: palette[i % palette.length] + "33",
        tension: 0.3,
        fill: false,
      };
    });

    if (costChart) costChart.destroy();
    costChart = new Chart(canvas, {
      type: "line",
      data: { labels: labels.map(shortLabel), datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { labels: { color: "#d8e0ec" } },
          tooltip: {
            callbacks: {
              label: (ctx) => `${ctx.dataset.label}: ${usd(ctx.parsed.y)}`,
            },
          },
        },
        scales: {
          x: { ticks: { color: "#7a8595" }, grid: { color: "#1f2630" } },
          y: {
            ticks: {
              color: "#7a8595",
              callback: (v) => "$" + Number(v).toFixed(4),
            },
            grid: { color: "#1f2630" },
          },
        },
      },
    });
  }

  function shortLabel(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    if (costWindow.bucket === "day") return _CHART_DAY_FMT.format(d);
    return _CHART_HOUR_FMT.format(d);
  }

  function usd(n) {
    if (n === null || n === undefined) return "—";
    if (n < 0.01) return "$" + n.toFixed(6);
    if (n < 1) return "$" + n.toFixed(4);
    return "$" + n.toFixed(2);
  }

  // --- Models / Provider health ------------------------------------------

  async function loadModels() {
    try {
      const r = await api("/v1/models");
      // Models tab shows the JSON.
      document.getElementById("models-json").textContent = JSON.stringify(r, null, 2);
      // Health tab shows a table.
      const tbody = document.getElementById("health-tbody");
      tbody.innerHTML = (r.data || [])
        .filter((m) => m.id !== "auto")
        .map((m) => {
          const tag = m.nautgate_unhealthy
            ? '<span class="tag unhealthy">unhealthy</span>'
            : '<span class="tag healthy">ok</span>';
          const tiers = (m.nautgate_tiers || []).join(", ");
          return `<tr><td>${esc(m.nautgate_provider)}</td><td>${esc(m.id)}</td><td>${esc(tiers)}</td><td>${tag}</td></tr>`;
        })
        .join("");
    } catch (e) {
      /* swallow */
    }
  }

  // --- Engram-OSS ingest (Settings → Engram-OSS sub-tab) -----------------
  // (Backend keys remain `sb_ingest` / `sb_memory.py` — UI rename only.)

  document.getElementById("sb-save")?.addEventListener("click", saveSBConfig);
  document.getElementById("sb-test")?.addEventListener("click", testSBConfig);

  async function loadSBConfig() {
    try {
      const cfg = await api("/v1/config");
      const sb = (cfg && cfg.sb_ingest) || {};
      document.getElementById("sb-enabled").checked = !!sb.enabled;
      document.getElementById("sb-host").value = sb.host || "";
      document.getElementById("sb-port").value = sb.port ?? 5433;
      document.getElementById("sb-database").value = sb.database || "agents_memory";
      document.getElementById("sb-user").value = sb.user || "agents";
    } catch (e) { /* leave defaults */ }
  }

  async function saveSBConfig() {
    const stateEl = document.getElementById("sb-state");
    stateEl.textContent = "saving…";
    const body = {
      sb_ingest: {
        enabled: document.getElementById("sb-enabled").checked,
        host: document.getElementById("sb-host").value.trim(),
        port: Number(document.getElementById("sb-port").value) || 5433,
        database: document.getElementById("sb-database").value.trim() || "agents_memory",
        user: document.getElementById("sb-user").value.trim() || "agents",
      },
    };
    try {
      const res = await fetch("/v1/config", {
        method: "PUT",
        headers: { Authorization: "Bearer " + getToken(), "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!res.ok) throw new Error("http_" + res.status);
      stateEl.textContent = "✓ saved";
      setTimeout(() => { stateEl.textContent = ""; }, 3000);
    } catch (e) {
      stateEl.textContent = "✗ save failed: " + (e.message || e);
    }
  }

  async function testSBConfig() {
    const stateEl = document.getElementById("sb-state");
    stateEl.textContent = "testing… (uses *saved* config — Save first if you changed values)";
    try {
      const res = await fetch("/v1/config/sb-ingest/test", {
        method: "POST",
        headers: { Authorization: "Bearer " + getToken() },
      });
      const data = await res.json();
      if (data.ok) {
        stateEl.innerHTML = '<span style="color:#4caf50">✓ ' + esc(data.detail) + '</span>';
      } else {
        stateEl.innerHTML = '<span style="color:#ff5c5c">✗ ' + esc(data.detail) + '</span>';
      }
    } catch (e) {
      stateEl.innerHTML = '<span style="color:#ff5c5c">✗ ' + esc(e.message || e) + '</span>';
    }
  }

  // --- Quality eval (Settings → Quality eval section) -------------------

  document.getElementById("qe-save")?.addEventListener("click", saveQualityConfig);
  document.getElementById("qe-models-reload")?.addEventListener("click", (e) => {
    e.preventDefault();
    const prov = document.getElementById("qe-judge-provider").value;
    loadQualityModels({ provider: prov, force: true });
  });
  // When the provider dropdown changes, refresh the base URL hint AND the
  // model list. We don't persist anything until Save.
  document.getElementById("qe-judge-provider")?.addEventListener("change", (e) => {
    const prov = e.target.value;
    const defaults = {
      openrouter: "https://openrouter.ai/api",
      openai: "https://api.openai.com",
      lmstudio: "http://host.docker.internal:1234",
      custom: "",
    };
    const baseEl = document.getElementById("qe-judge-base-url");
    if (defaults[prov] !== undefined) baseEl.value = defaults[prov];
    loadQualityModels({ provider: prov });
  });

  // Cache the last-fetched model list so re-opening Settings doesn't refetch.
  let _qualityModelsCache = { provider: null, models: [] };

  async function loadQualityModels({ provider, force = false } = {}) {
    const sel = document.getElementById("qe-judge-model");
    const stateEl = document.getElementById("qe-state");
    const currentValue = sel.value;
    if (!force && _qualityModelsCache.provider === provider && _qualityModelsCache.models.length) {
      _renderQualityModels(_qualityModelsCache.models, currentValue);
      return;
    }
    sel.innerHTML = '<option>loading…</option>';
    stateEl.textContent = `fetching ${provider || "?"} models…`;
    try {
      const qs = provider ? `?provider=${encodeURIComponent(provider)}` : "";
      const res = await fetch("/v1/quality/models" + qs, {
        headers: { Authorization: "Bearer " + getToken() },
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || data.error || ("http_" + res.status));
      _qualityModelsCache = { provider: provider, models: data.models || [] };
      _renderQualityModels(data.models || [], currentValue);
      stateEl.textContent = `${data.model_count} models available from ${data.provider}`;
      setTimeout(() => { if (stateEl.textContent.includes("available")) stateEl.textContent = ""; }, 4000);
    } catch (e) {
      sel.innerHTML = '<option value="">— couldn\'t load —</option>';
      stateEl.innerHTML = '<span style="color:#ff5c5c">✗ fetch models failed: ' + esc(e.message || e) + '</span>';
    }
  }

  function _renderQualityModels(models, preferred) {
    const sel = document.getElementById("qe-judge-model");
    sel.innerHTML = "";
    if (!models.length) {
      sel.innerHTML = '<option value="">— no models —</option>';
      return;
    }
    // If the previously-saved value isn't in the new list, prepend it so
    // the dropdown still shows the user's actual setting.
    const ids = new Set(models.map((m) => m.id));
    if (preferred && !ids.has(preferred)) {
      const o = document.createElement("option");
      o.value = preferred;
      o.textContent = preferred + " (not in catalogue)";
      sel.appendChild(o);
    }
    for (const m of models) {
      const o = document.createElement("option");
      o.value = m.id;
      const p = m.prompt_price_per_m;
      const c = m.completion_price_per_m;
      const price = (p != null && c != null)
        ? ` — $${p}/M in · $${c}/M out`
        : (p != null ? ` — $${p}/M in` : "");
      o.textContent = `${m.id}${price}`;
      sel.appendChild(o);
    }
    if (preferred && ids.has(preferred)) {
      sel.value = preferred;
    }
  }

  async function loadQualityConfig() {
    try {
      const cfg = await api("/v1/config");
      const qe = (cfg && cfg.quality_eval) || {};
      document.getElementById("qe-enabled").checked = qe.enabled !== false;
      document.getElementById("qe-judge-provider").value = qe.judge_provider || "openrouter";
      document.getElementById("qe-judge-base-url").value =
        qe.judge_base_url || "https://openrouter.ai/api";
      const rate = qe.sample_rate != null ? qe.sample_rate : 0.10;
      document.getElementById("qe-sample-rate").value = Math.round(rate * 100);
      document.getElementById("qe-daily-cap").value =
        qe.daily_cost_cap_usd != null ? qe.daily_cost_cap_usd : 5.00;
      // Pull the model list for the configured provider; preselect the saved model.
      const savedModel = qe.judge_model || "openai/gpt-4o-mini";
      await loadQualityModels({ provider: qe.judge_provider || "openrouter" });
      // After models loaded, force-select the saved model (it'll be added
      // as a fallback option if missing from the catalogue).
      const sel = document.getElementById("qe-judge-model");
      const opts = Array.from(sel.options).map((o) => o.value);
      if (!opts.includes(savedModel)) {
        const o = document.createElement("option");
        o.value = savedModel;
        o.textContent = savedModel + " (not in catalogue)";
        sel.insertBefore(o, sel.firstChild);
      }
      sel.value = savedModel;
    } catch (e) { /* leave defaults */ }
  }

  async function saveQualityConfig() {
    const stateEl = document.getElementById("qe-state");
    stateEl.textContent = "saving…";
    const ratePct = Number(document.getElementById("qe-sample-rate").value);
    const provider = document.getElementById("qe-judge-provider").value || "openrouter";
    const model = document.getElementById("qe-judge-model").value || "openai/gpt-4o-mini";
    const body = {
      quality_eval: {
        enabled: document.getElementById("qe-enabled").checked,
        judge_provider: provider,
        judge_model: model,
        judge_base_url: document.getElementById("qe-judge-base-url").value.trim()
          || "https://openrouter.ai/api",
        sample_rate: isFinite(ratePct) ? Math.max(0, Math.min(100, ratePct)) / 100 : 0.10,
        daily_cost_cap_usd: Number(document.getElementById("qe-daily-cap").value) || 0,
      },
    };
    try {
      const res = await fetch("/v1/config", {
        method: "PUT",
        headers: { Authorization: "Bearer " + getToken(), "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!res.ok) throw new Error("http_" + res.status);
      stateEl.textContent = "✓ saved";
      setTimeout(() => { stateEl.textContent = ""; }, 3000);
    } catch (e) {
      stateEl.textContent = "✗ save failed: " + (e.message || e);
    }
  }

  // --- Quality tab -------------------------------------------------------

  let qualityWindow = { hours: 24 };
  let qualityModelFilter = "*";

  document.querySelectorAll("#tab-quality .window-buttons button").forEach((b) => {
    b.addEventListener("click", () => {
      qualityWindow = { hours: Number(b.dataset.window) };
      document
        .querySelectorAll("#tab-quality .window-buttons button")
        .forEach((x) => x.classList.toggle("active", x === b));
      loadQuality();
    });
  });
  document.getElementById("quality-model-filter")?.addEventListener("change", (e) => {
    qualityModelFilter = e.target.value || "*";
    loadQuality();
  });

  function _heatmapColor(rate) {
    if (rate == null) return "transparent";
    const r = Math.max(0, Math.min(1, rate));
    // green -> amber -> red
    const hue = (1 - r) * 120;
    return `hsla(${hue}, 70%, 35%, ${0.25 + r * 0.55})`;
  }

  // ---- Judge health card --------------------------------------------------
  document.getElementById("qe-health-reload")?.addEventListener("click", () => loadQualityHealth());

  async function loadQualityHealth() {
    const card = document.getElementById("qe-health-card");
    if (!card) return;
    try {
      const h = await api("/v1/quality/health");
      card.hidden = false;
      const last24 = h.last_24h || {};
      const ok = h.enabled && h.api_key_configured && (last24.success_rate == null || last24.success_rate >= 0.9);
      const noKey = h.enabled && !h.api_key_configured;
      const disabled = !h.enabled;
      let statusLabel = "● healthy";
      let level = "ok";
      if (disabled) { statusLabel = "○ disabled"; level = "warn"; }
      else if (noKey) { statusLabel = "✗ no API key"; level = "bad"; }
      else if (last24.success_rate != null && last24.success_rate < 0.9) {
        statusLabel = `! ${(last24.success_rate * 100).toFixed(0)}% success`;
        level = "warn";
      }
      const pill = document.getElementById("qe-health-status");
      pill.textContent = statusLabel;
      pill.dataset.level = level;
      document.getElementById("qe-health-judge").textContent =
        (h.judge_provider || "?") + "/" + (h.judge_model || "?");
      document.getElementById("qe-health-rate").textContent =
        ((h.sample_rate || 0) * 100).toFixed(0) + "%";
      document.getElementById("qe-health-attempts").textContent = (last24.attempts || 0).toLocaleString();
      document.getElementById("qe-health-success").textContent =
        last24.success_rate != null ? (last24.success_rate * 100).toFixed(0) + "%" : "—";
      document.getElementById("qe-health-latency").textContent =
        last24.avg_latency_ms != null ? last24.avg_latency_ms + "ms" : "—";
      document.getElementById("qe-health-spend").textContent =
        "$" + Number(h.spend_today_usd || 0).toFixed(4);
      document.getElementById("qe-health-cap").textContent =
        "$" + Number(h.daily_cost_cap_usd || 0).toFixed(2);
      document.getElementById("qe-health-total").textContent =
        (h.total_evaluations_ever || 0).toLocaleString();
    } catch (_e) {
      card.hidden = true;
    }
  }

  // ---- Anti-pattern leaderboard ------------------------------------------
  document.getElementById("qe-anti-reload")?.addEventListener("click", () => loadAntiPatterns());
  document.getElementById("qe-anti-copy")?.addEventListener("click", () => copyAntiPatternsMarkdown());

  let _antiPatternsCache = null;

  async function loadAntiPatterns() {
    const list = document.getElementById("qe-anti-list");
    if (!list) return;
    list.innerHTML = '<p class="hint">loading…</p>';
    try {
      const data = await api("/v1/quality/anti-patterns?days=30");
      _antiPatternsCache = data;
      if (!data.items || !data.items.length) {
        list.innerHTML = '<p class="hint">No anti-patterns yet — once you have ~50 quality evals across a few prompts, patterns will start to surface here.</p>';
        return;
      }
      list.innerHTML = data.items.slice(0, 20).map((it, idx) => {
        const score = it.avg_completion != null ? it.avg_completion.toFixed(1) : "—";
        const sev = (it.avg_completion != null && it.avg_completion < 2.0) ? "bad"
                  : (it.avg_completion != null && it.avg_completion < 3.0) ? "warn"
                  : "neutral";
        const rewrite = (it.sample_rewrites && it.sample_rewrites[0]) || null;
        const promptEx = (it.sample_prompts && it.sample_prompts[0]) || "";
        const modelTags = (it.sample_models || []).slice(0, 2)
          .map(m => `<span class="qe-anti-model">${esc(m)}</span>`).join("");
        return `
          <details class="qe-anti-item qe-anti-${sev}">
            <summary>
              <span class="qe-anti-rank">${idx + 1}</span>
              <span class="qe-anti-count">${it.occurrences}×</span>
              <span class="qe-anti-pattern">${esc(it.anti_pattern)}</span>
              <span class="qe-anti-score">avg ${score}/5</span>
            </summary>
            <div class="qe-anti-body">
              <div class="qe-anti-models">seen with: ${modelTags}</div>
              ${promptEx ? `
                <div class="qe-anti-section">
                  <div class="qe-anti-label">Example prompt</div>
                  <div class="qe-anti-text qe-anti-prompt">${esc(promptEx)}</div>
                </div>` : ""}
              ${rewrite ? `
                <div class="qe-anti-section">
                  <div class="qe-anti-label">Suggested rewrite</div>
                  <div class="qe-anti-text qe-anti-rewrite">${esc(rewrite)}</div>
                  <button class="ghost qe-anti-copy-btn" data-text="${esc(rewrite)}">📋 copy</button>
                </div>` : ""}
            </div>
          </details>`;
      }).join("");
      // Wire per-item copy buttons.
      list.querySelectorAll(".qe-anti-copy-btn").forEach((btn) => {
        btn.addEventListener("click", async (ev) => {
          ev.stopPropagation();
          try { await navigator.clipboard.writeText(btn.dataset.text || ""); } catch (_e) {}
          const t = btn.textContent;
          btn.textContent = "✓ copied";
          setTimeout(() => { btn.textContent = t; }, 1200);
        });
      });
    } catch (e) {
      list.innerHTML = `<p class="hint">load failed: ${esc(e.message || e)}</p>`;
    }
  }

  function copyAntiPatternsMarkdown() {
    if (!_antiPatternsCache || !_antiPatternsCache.items) return;
    const items = _antiPatternsCache.items.slice(0, 15);
    const lines = [
      "# Prompt Anti-Patterns",
      "",
      `*Aggregated from ${_antiPatternsCache.window_days} days of LLM call evaluations · NautGate Quality Eval*`,
      "",
      "| # | Frequency | Avg score | Anti-pattern | Suggested rewrite |",
      "|---:|---:|---:|---|---|",
    ];
    items.forEach((it, idx) => {
      const score = it.avg_completion != null ? it.avg_completion.toFixed(1) : "—";
      const rewrite = (it.sample_rewrites && it.sample_rewrites[0])
        ? it.sample_rewrites[0].replace(/\|/g, "\\|").slice(0, 200)
        : "—";
      const pat = it.anti_pattern.replace(/\|/g, "\\|");
      lines.push(`| ${idx + 1} | ${it.occurrences}× | ${score}/5 | ${pat} | ${rewrite} |`);
    });
    const md = lines.join("\n");
    navigator.clipboard.writeText(md).then(() => {
      const b = document.getElementById("qe-anti-copy");
      const t = b.textContent;
      b.textContent = "✓ copied";
      setTimeout(() => { b.textContent = t; }, 1500);
    });
  }

  // ---- Per-agent, per-session, edge-graph views -------------------------
  document.getElementById("qe-byagent-reload")?.addEventListener("click", () => loadAntiPatternsByAgent());
  document.getElementById("qe-bysession-reload")?.addEventListener("click", () => loadAntiPatternsBySession());
  document.getElementById("qe-edges-reload")?.addEventListener("click", () => loadDelegationEdges());

  async function loadAntiPatternsByAgent() {
    const list = document.getElementById("qe-byagent-list");
    if (!list) return;
    list.innerHTML = '<p class="hint">loading…</p>';
    try {
      const d = await api("/v1/quality/anti-patterns-by-agent?days=30");
      if (!d.items || !d.items.length) {
        list.innerHTML = '<p class="hint">No per-agent data yet.</p>';
        return;
      }
      list.innerHTML = d.items.map((it) => {
        const sev = (it.avg_completion != null && it.avg_completion < 2.0) ? "bad"
                  : (it.avg_completion != null && it.avg_completion < 3.0) ? "warn"
                  : "neutral";
        const score = it.avg_completion != null ? it.avg_completion.toFixed(1) : "—";
        const topRows = (it.top_patterns || []).map(p =>
          `<div class="qe-agent-pattern-row">
             <span class="qe-agent-pattern-count">${p.count}×</span>
             <span class="qe-agent-pattern-name">${esc(p.pattern)}</span>
           </div>`).join("");
        return `
          <details class="qe-byagent-item qe-anti-${sev}">
            <summary>
              <span class="qe-agent-id"><code>${esc(it.agent_id)}</code></span>
              <span class="qe-agent-total">${it.total_anti_patterns}× total</span>
              <span class="qe-agent-score">avg ${score}/5</span>
            </summary>
            <div class="qe-anti-body">${topRows}</div>
          </details>`;
      }).join("");
    } catch (e) {
      list.innerHTML = `<p class="hint">load failed: ${esc(e.message || e)}</p>`;
    }
  }

  async function loadAntiPatternsBySession() {
    const list = document.getElementById("qe-bysession-list");
    if (!list) return;
    list.innerHTML = '<p class="hint">loading…</p>';
    try {
      const d = await api("/v1/quality/anti-patterns-by-session?days=30&min_calls=5");
      if (!d.items || !d.items.length) {
        list.innerHTML = '<p class="hint">No sessions with ≥5 anti-patterns yet.</p>';
        return;
      }
      list.innerHTML = `
        <table class="qe-session-table">
          <thead><tr>
            <th>session</th><th>agent</th><th>anti-patterns</th>
            <th>avg score</th><th>top pattern</th><th>span</th>
          </tr></thead>
          <tbody>
            ${d.items.map(it => {
              const sev = (it.avg_completion != null && it.avg_completion < 2.0) ? "bad"
                        : (it.avg_completion != null && it.avg_completion < 3.0) ? "warn"
                        : "neutral";
              const score = it.avg_completion != null ? it.avg_completion.toFixed(1) : "—";
              const top = (it.top_patterns && it.top_patterns[0])
                ? `${it.top_patterns[0].count}× ${esc(it.top_patterns[0].pattern)}` : "—";
              const span = (it.first_seen && it.last_seen)
                ? `${tsShort(it.first_seen)} → ${tsShort(it.last_seen)}` : "—";
              return `
                <tr class="qe-session-row qe-anti-${sev}">
                  <td><code>${esc((it.session_id || "").slice(0, 16))}</code></td>
                  <td>${esc(it.agent_id || "—")}</td>
                  <td><b>${it.anti_pattern_count}</b></td>
                  <td>${score}/5</td>
                  <td>${top}</td>
                  <td class="hint">${span}</td>
                </tr>`;
            }).join("")}
          </tbody>
        </table>`;
    } catch (e) {
      list.innerHTML = `<p class="hint">load failed: ${esc(e.message || e)}</p>`;
    }
  }

  async function loadDelegationEdges() {
    const list = document.getElementById("qe-edges-list");
    if (!list) return;
    list.innerHTML = '<p class="hint">loading…</p>';
    try {
      const d = await api("/v1/quality/delegation-edges?days=30");
      if (!d.edges || !d.edges.length) {
        list.innerHTML = '<p class="hint">No delegation calls detected (looks for coms_send / Task / dispatch tool calls in your audit log).</p>';
        return;
      }
      list.innerHTML = `
        <table class="qe-edges-table">
          <thead><tr>
            <th>master</th><th></th><th>sub-agent</th><th>calls</th>
            <th>avg score</th><th>failure rate</th><th>top issue</th>
          </tr></thead>
          <tbody>
            ${d.edges.map(e => {
              const fr = e.failure_rate;
              const sev = (fr != null && fr > 0.5) ? "bad"
                        : (fr != null && fr > 0.25) ? "warn"
                        : "neutral";
              const score = e.avg_completion != null ? e.avg_completion.toFixed(1) : "—";
              const frStr = fr != null ? (fr * 100).toFixed(0) + "%" : "—";
              const top = (e.top_anti_patterns && e.top_anti_patterns[0])
                ? `${e.top_anti_patterns[0].count}× ${esc(e.top_anti_patterns[0].pattern)}` : "—";
              return `
                <tr class="qe-edges-row qe-anti-${sev}">
                  <td><code>${esc(e.source)}</code></td>
                  <td class="qe-edge-arrow">→</td>
                  <td><code>${esc(e.target)}</code></td>
                  <td><b>${e.calls}</b></td>
                  <td>${score}/5</td>
                  <td><b class="qe-edge-failure">${frStr}</b></td>
                  <td>${top}</td>
                </tr>`;
            }).join("")}
          </tbody>
        </table>`;
    } catch (e) {
      list.innerHTML = `<p class="hint">load failed: ${esc(e.message || e)}</p>`;
    }
  }

  async function loadQuality() {
    // Health + anti-patterns load independently of the summary.
    loadQualityHealth();
    loadAntiPatterns();
    loadAntiPatternsByAgent();
    loadAntiPatternsBySession();
    loadDelegationEdges();
    try {
      const qs = `?hours=${qualityWindow.hours}` +
        (qualityModelFilter && qualityModelFilter !== "*" ? `&model=${encodeURIComponent(qualityModelFilter)}` : "");
      const s = await api("/v1/quality/summary" + qs);
      const t = s.totals || {};
      document.getElementById("q-total").textContent = (t.evaluations || 0).toLocaleString();
      document.getElementById("q-completion").textContent =
        t.avg_task_completion != null ? Number(t.avg_task_completion).toFixed(2) : "—";
      document.getElementById("q-failure-rate").textContent =
        t.failure_rate != null ? (Number(t.failure_rate) * 100).toFixed(1) + "%" : "—";
      document.getElementById("q-judge-cost").textContent =
        t.judge_spend_usd != null ? "$" + Number(t.judge_spend_usd).toFixed(4) : "$—";

      // Heatmap
      const buckets = ["0_2", "2_4", "4_6", "6_8", "8_10"];
      const heatBody = document.querySelector("#quality-heatmap tbody");
      heatBody.innerHTML = (s.heatmap || []).map((r) => {
        const cells = buckets.map((b) => {
          const v = r.buckets ? r.buckets[b] : null;
          const c = r.counts ? r.counts[b] : 0;
          const display = v == null ? "—" : (v * 100).toFixed(0) + "%";
          const title = c ? `${c} evals` : "no evals";
          return `<td style="background:${_heatmapColor(v)}" title="${title}">${display}</td>`;
        }).join("");
        return `<tr><td><span class="tag">${esc(r.model)}</span></td>${cells}</tr>`;
      }).join("") || `<tr><td colspan="6" class="hint">no evaluations yet — they'll start landing within seconds of your next LLM call</td></tr>`;

      // Failure modes
      const fmBody = document.querySelector("#quality-failure-modes tbody");
      fmBody.innerHTML = (s.failure_modes || []).map((r) => `
        <tr>
          <td>${esc(r.model)}</td>
          <td>${r.evaluations || 0}</td>
          <td>${r.over_thinking || 0}</td>
          <td>${r.off_task || 0}</td>
          <td>${r.looped || 0}</td>
          <td>${r.hallucination || 0}</td>
          <td>${r.partial_answer || 0}</td>
          <td>${r.refusal || 0}</td>
          <td>${r.tool_misuse || 0}</td>
        </tr>`).join("") || `<tr><td colspan="9" class="hint">no failure-mode data yet</td></tr>`;

      // Worst recent
      const wBody = document.querySelector("#quality-worst tbody");
      wBody.innerHTML = (s.worst_recent || []).map((r) => {
        const tags = (r.failure_tags || []).map(
          (t) => `<span class="failure-tag failure-tag-${esc(t)}">${esc(t.replace(/_/g, " "))}</span>`
        ).join(" ");
        return `
          <tr class="worst-row" data-decision="${esc(r.decision_id)}">
            <td>${tsShort(r.ts)}</td>
            <td>${esc(r.model)}</td>
            <td><span class="tag tier">${esc(r.tier || "—")}</span></td>
            <td>${r.completion != null ? r.completion.toFixed(1) : "—"}</td>
            <td>${tags}</td>
            <td>${esc(r.coach_notes || "")}</td>
          </tr>`;
      }).join("") || `<tr><td colspan="6" class="hint">no failures detected in this window</td></tr>`;

      // Click a worst-row to jump to the Audit Log filtered to that call.
      document.querySelectorAll(".worst-row").forEach((row) => {
        row.addEventListener("click", () => {
          activateTab("audit");
          setTimeout(() => {
            auditExpandedId = null;
            toggleAuditDetail(row.dataset.decision);
          }, 200);
        });
      });

      // Populate model filter from observed evaluations on first paint.
      const sel = document.getElementById("quality-model-filter");
      if (sel && sel.children.length <= 1 && Array.isArray(s.by_model)) {
        for (const m of s.by_model) {
          const opt = document.createElement("option");
          opt.value = m.model;
          opt.textContent = `${m.model} · ${m.evaluations} evals`;
          sel.appendChild(opt);
        }
      }
    } catch (e) {
      console.error("loadQuality failed", e);
    }
  }

  // --- Behavior tab — prompt→action compliance ---------------------------
  // Mirrors the cowboy analysis: per-model action_compliance + the four
  // agentic anti-pattern rates, plus a one-decision trace inspector.

  let behaviorWindowH = 168;

  function fmt5(v) {
    if (v === null || v === undefined) return '<span class="hint">—</span>';
    return (Math.round(v * 100) / 100).toFixed(2);
  }
  function fmtPct(v) {
    if (v === null || v === undefined) return '<span class="hint">—</span>';
    return (v * 100).toFixed(1) + "%";
  }
  function fmtMs(v) {
    if (v === null || v === undefined) return '<span class="hint">—</span>';
    if (v < 1000) return Math.round(v) + "ms";
    return (v / 1000).toFixed(1) + "s";
  }

  async function loadBehavior() {
    const tbody = document.querySelector("#behavior-per-model tbody");
    if (!tbody) return;
    tbody.innerHTML = '<tr><td colspan="10" class="hint">loading…</td></tr>';
    try {
      const r = await api("/v1/behavior/per-model?hours=" + behaviorWindowH);
      const rows = r.data || [];
      if (!rows.length) {
        tbody.innerHTML = '<tr><td colspan="10" class="hint">No quality evals in window.</td></tr>';
        return;
      }
      tbody.innerHTML = rows.map(m => `
        <tr>
          <td><b>${esc(m.model || "—")}</b></td>
          <td>${m.evals}</td>
          <td>${fmt5(m.avg_action_compliance)}</td>
          <td>${fmt5(m.avg_task_completion)}</td>
          <td>${fmt5(m.avg_reasoning_efficiency)}</td>
          <td>${fmtMs(m.avg_duration_ms)}</td>
          <td>${fmtPct(m.skipped_doc_rate)}</td>
          <td>${fmtPct(m.edit_without_read_rate)}</td>
          <td>${fmtPct(m.premature_action_rate)}</td>
          <td>${fmtPct(m.retry_loop_rate)}</td>
        </tr>
      `).join("");
    } catch (e) {
      tbody.innerHTML = `<tr><td colspan="10" class="hint" style="color:#ff5c5c">load failed: ${esc(e.message || String(e))}</td></tr>`;
      console.error("loadBehavior failed", e);
    }
    // Also surface any prior comparison run so the user doesn't have to click reload.
    loadBehaviorComparisonLatest().catch(() => {});
  }

  document.querySelectorAll("[data-bh-window]").forEach(btn => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("[data-bh-window]").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      behaviorWindowH = parseInt(btn.getAttribute("data-bh-window"), 10) || 168;
      loadBehavior();
    });
  });

  async function loadBehaviorTrace() {
    const idInput = document.getElementById("behavior-trace-id");
    const out = document.getElementById("behavior-trace-result");
    if (!idInput || !out) return;
    const id = (idInput.value || "").trim();
    if (!id) { out.innerHTML = '<p class="hint">paste a decision_id first</p>'; return; }
    out.innerHTML = '<p class="hint">loading…</p>';
    try {
      const t = await api("/v1/behavior/trace/" + encodeURIComponent(id));
      out.innerHTML = renderBehaviorTrace(t);
    } catch (e) {
      out.innerHTML = `<p class="hint" style="color:#ff5c5c">load failed: ${esc(e.message || String(e))}</p>`;
    }
  }
  document.getElementById("behavior-trace-load")?.addEventListener("click", loadBehaviorTrace);
  document.getElementById("behavior-trace-id")?.addEventListener("keydown", e => {
    if (e.key === "Enter") loadBehaviorTrace();
  });

  // --- Comparison runner -----------------------------------------------
  async function runBehaviorComparison() {
    const status = document.getElementById("bh-compare-status");
    const result = document.getElementById("bh-compare-result");
    const input = document.getElementById("bh-compare-models");
    const btn = document.getElementById("bh-compare-run");
    if (!status || !result || !input || !btn) return;
    const models = (input.value || "")
      .split(",").map(s => s.trim()).filter(Boolean);
    if (!models.length) {
      status.textContent = "need at least one model";
      return;
    }
    btn.disabled = true;
    status.textContent = "running (≈30s)…";
    result.innerHTML = "";
    try {
      const t = getToken();
      const res = await fetch("/v1/behavior/compare", {
        method: "POST",
        headers: {
          "Authorization": "Bearer " + t,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({models}),
      });
      if (!res.ok) {
        const text = await res.text();
        status.textContent = "failed: " + text.slice(0, 200);
        return;
      }
      status.textContent = "done — fetching results…";
      await loadBehaviorComparisonLatest();
      status.textContent = "complete.";
    } catch (e) {
      status.textContent = "failed: " + (e.message || String(e));
    } finally {
      btn.disabled = false;
    }
  }

  async function loadBehaviorComparisonLatest() {
    const result = document.getElementById("bh-compare-result");
    if (!result) return;
    try {
      const r = await api("/v1/behavior/compare/latest");
      result.innerHTML = renderBehaviorComparison(r);
    } catch (e) {
      result.innerHTML = `<p class="hint" style="color:#ff5c5c">load failed: ${esc(e.message || String(e))}</p>`;
    }
  }

  function renderBehaviorComparison(r) {
    if (!r || !r.comparison_id) {
      return '<p class="hint">No comparison runs yet. Click "Run comparison now".</p>';
    }
    const blocks = (r.canaries || []).map(c => {
      const models = c.results.map(res => {
        const ru = res.rubric || {};
        const tags = Array.isArray(res.failure_tags) ? res.failure_tags : [];
        const err = res.error
          ? `<div style="color:#ff5c5c">error: ${esc(String(res.error).slice(0, 200))}</div>`
          : "";
        return `
          <div style="border:1px solid rgba(255,255,255,0.08); padding:10px; border-radius:4px;">
            <div><b>${esc(res.target_model)}</b> <span class="hint">${fmtMs(res.duration_ms)} · ${res.prompt_tokens ?? "—"}→${res.completion_tokens ?? "—"} tok</span></div>
            <div style="margin-top:6px; font-size:13px;">
              action_compliance: <b>${fmt5(ru.action_compliance)}</b> ·
              task_completion: <b>${fmt5(ru.task_completion)}</b> ·
              efficiency: <b>${fmt5(ru.reasoning_efficiency)}</b>
            </div>
            ${tags.length ? `<div style="margin-top:4px">tags: ${tags.map(t => `<code style="margin-right:6px">${esc(t)}</code>`).join("")}</div>` : ""}
            ${res.coach_notes ? `<div style="margin-top:6px; font-style:italic; color:#bbb">${esc(res.coach_notes)}</div>` : ""}
            ${err}
            <details style="margin-top:8px"><summary class="hint">response (click to expand)</summary>
              <pre style="white-space:pre-wrap; max-height:200px; overflow:auto; font-size:12px; margin-top:6px">${esc(res.response_text || "(empty)")}</pre>
            </details>
          </div>
        `;
      }).join("");
      return `
        <div style="margin-bottom:16px">
          <h4 style="margin-bottom:6px">${esc(c.name)}</h4>
          <div style="display:grid; grid-template-columns: repeat(${c.results.length}, 1fr); gap:12px">${models}</div>
        </div>
      `;
    }).join("");
    return `
      <p class="hint">Comparison <code>${esc(r.comparison_id.slice(0, 8))}…</code> · ${tsFull(r.ts)}</p>
      ${blocks || '<p class="hint">no canaries in this run</p>'}
    `;
  }

  document.getElementById("bh-compare-run")?.addEventListener("click", runBehaviorComparison);
  document.getElementById("bh-compare-reload")?.addEventListener("click", loadBehaviorComparisonLatest);

  function renderBehaviorTrace(t) {
    // Extract user prompt — prompt_body is JSON of messages; pull the last user message text.
    let userText = "";
    try {
      const msgs = typeof t.prompt_body === "string" ? JSON.parse(t.prompt_body) : t.prompt_body;
      if (Array.isArray(msgs)) {
        const lastUser = [...msgs].reverse().find(m => m.role === "user");
        if (lastUser) {
          if (typeof lastUser.content === "string") userText = lastUser.content;
          else if (Array.isArray(lastUser.content)) {
            userText = lastUser.content
              .filter(b => b && b.type === "text")
              .map(b => b.text || "")
              .join("\n");
          }
        }
      }
    } catch (e) { userText = "(prompt_body not parseable as messages JSON)"; }

    const tools = Array.isArray(t.tool_calls_made) ? t.tool_calls_made : [];
    const seqHtml = tools.length
      ? tools.map((c, i) => {
          let target = "";
          try {
            const a = typeof c.arguments === "string" ? JSON.parse(c.arguments) : (c.arguments || {});
            target = a.file_path || a.path || a.pattern || a.query || a.url
              || (a.command ? a.command.slice(0, 100) : "");
          } catch (e) {
            // Tool arg JSON is truncated at 200 bytes — degrade gracefully.
            const raw = typeof c.arguments === "string" ? c.arguments : "";
            const m = raw.match(/"(?:file_path|path|command|pattern)"\s*:\s*"([^"]{1,100})"/);
            if (m) target = m[1];
          }
          return `<li><b>${i + 1}.</b> <code>${esc(c.name || "?")}</code> ${target ? `→ <span class="hint">${esc(target)}</span>` : ""}</li>`;
        }).join("")
      : '<li class="hint">no tool calls captured</li>';

    const rubric = t.rubric || {};
    const tags = Array.isArray(t.failure_tags) ? t.failure_tags : [];
    const verdictHtml = (t.rubric || tags.length || t.coach_notes) ? `
      <div style="margin-top:12px; padding:8px; border-left:3px solid #888; background:rgba(255,255,255,0.03)">
        <div><b>Judge verdict</b></div>
        <div>action_compliance: <b>${fmt5(rubric.action_compliance)}</b> · task_completion: <b>${fmt5(rubric.task_completion)}</b> · efficiency: <b>${fmt5(rubric.reasoning_efficiency)}</b></div>
        ${tags.length ? `<div>tags: ${tags.map(t => `<code style="margin-right:6px">${esc(t)}</code>`).join("")}</div>` : ""}
        ${t.coach_notes ? `<div style="margin-top:6px"><i>${esc(t.coach_notes)}</i></div>` : ""}
        ${t.suggested_prompt ? `<div style="margin-top:6px"><b>Suggested prompt:</b> <code>${esc(t.suggested_prompt)}</code></div>` : ""}
      </div>
    ` : '<p class="hint" style="margin-top:8px">no quality eval has run on this decision yet</p>';

    return `
      <div class="behavior-trace" style="display:grid; grid-template-columns: 1fr 1fr; gap:16px">
        <div>
          <h4>USER PROMPT</h4>
          <pre style="white-space:pre-wrap; word-break:break-word; max-height:400px; overflow:auto; padding:8px; background:rgba(255,255,255,0.04)">${esc(userText || "(empty)")}</pre>
          <div class="hint">model: <code>${esc(t.model || "?")}</code> · ${tools.length} tool calls · ${fmtMs(t.duration_ms)} · reasoning: ${t.reasoning_tokens ?? "—"} tok</div>
        </div>
        <div>
          <h4>MODEL TOOL SEQUENCE</h4>
          <ol style="padding-left:24px; line-height:1.7">${seqHtml}</ol>
        </div>
      </div>
      ${verdictHtml}
    `;
  }

  // --- Backup (Settings → Backup section) --------------------------------

  document.getElementById("bk-reload")?.addEventListener("click", () => {
    loadBackupConfig(); loadBackupList();
  });
  document.getElementById("bk-save-config")?.addEventListener("click", saveBackupConfig);
  document.getElementById("bk-create-now")?.addEventListener("click", createBackupNow);

  async function loadBackupConfig() {
    try {
      const cfg = await api("/v1/backups/config");
      document.getElementById("bk-enabled").checked = !!cfg.enabled;
      document.getElementById("bk-interval").value = cfg.interval_hours ?? 3;
      document.getElementById("bk-retention").value = cfg.retention_count ?? 20;
      const next = cfg.next_run_at ? `next: ${tsFull(cfg.next_run_at)}` : "no run scheduled";
      const last = cfg.last_run_at ? ` · last: ${tsFull(cfg.last_run_at)}` : "";
      document.getElementById("bk-config-state").textContent = next + last;
    } catch (e) { /* swallow */ }
  }

  async function saveBackupConfig() {
    const stateEl = document.getElementById("bk-config-state");
    stateEl.textContent = "saving…";
    const body = {
      enabled: document.getElementById("bk-enabled").checked,
      interval_hours: Number(document.getElementById("bk-interval").value) || 3,
      retention_count: Number(document.getElementById("bk-retention").value) || 20,
    };
    try {
      const res = await fetch("/v1/backups/config", {
        method: "PUT",
        headers: { Authorization: "Bearer " + getToken(), "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!res.ok) throw new Error("http_" + res.status);
      await loadBackupConfig();
    } catch (e) {
      stateEl.textContent = "save failed: " + (e.message || e);
    }
  }

  async function createBackupNow() {
    const stateEl = document.getElementById("bk-create-state");
    stateEl.textContent = "running pg_dump…";
    try {
      const res = await fetch("/v1/backups", {
        method: "POST",
        headers: { Authorization: "Bearer " + getToken() },
      });
      if (!res.ok) {
        const detail = await res.text();
        throw new Error(`${res.status} ${detail.slice(0, 200)}`);
      }
      const row = await res.json();
      stateEl.textContent = `✓ created ${humanBytes(row.size_bytes)}`;
      await loadBackupList();
      setTimeout(() => { stateEl.textContent = ""; }, 4000);
    } catch (e) {
      stateEl.textContent = "✗ " + (e.message || e);
    }
  }

  async function loadBackupList() {
    const tbody = document.getElementById("bk-tbody");
    if (!tbody) return;
    try {
      const data = await api("/v1/backups");
      const items = data.items || [];
      if (!items.length) {
        tbody.innerHTML = '<tr><td colspan="6" class="hint">No backups yet. Click "Create backup now" or wait for the scheduler.</td></tr>';
        return;
      }
      tbody.innerHTML = items.map(b => {
        const counts = b.table_counts || {};
        const summary = Object.entries(counts).map(([k,v]) => `${k}:${v}`).join(" · ") || '<span class="hint">—</span>';
        const statusBadge = b.status === "ok"
          ? '<span class="bk-status bk-ok">ok</span>'
          : b.status === "failed"
            ? `<span class="bk-status bk-failed" title="${esc(b.error_message || "")}">failed</span>`
            : '<span class="bk-status bk-progress">in progress</span>';
        const restoreBtn = b.status === "ok" && b.exists_on_disk
          ? `<button class="bk-restore" data-bk-id="${esc(b.id)}">Restore</button>`
          : "";
        return `
          <tr>
            <td>${tsFull(b.ts)}</td>
            <td>${humanBytes(b.size_bytes)}</td>
            <td><span class="bk-via bk-via-${esc(b.created_via)}">${esc(b.created_via)}</span></td>
            <td class="bk-counts">${summary}</td>
            <td>${statusBadge}</td>
            <td>
              ${restoreBtn}
              <button class="ghost bk-delete" data-bk-id="${esc(b.id)}">×</button>
            </td>
          </tr>`;
      }).join("");
      tbody.querySelectorAll(".bk-restore").forEach(b => b.addEventListener("click", () => restoreBackup(b.getAttribute("data-bk-id"))));
      tbody.querySelectorAll(".bk-delete").forEach(b => b.addEventListener("click", () => deleteBackup(b.getAttribute("data-bk-id"))));
    } catch (e) {
      tbody.innerHTML = `<tr><td colspan="6" class="hint">load failed: ${esc(e.message || e)}</td></tr>`;
    }
  }

  async function deleteBackup(id) {
    if (!confirm("Delete this backup? The file on disk will be removed.")) return;
    try {
      const res = await fetch("/v1/backups/" + encodeURIComponent(id), {
        method: "DELETE",
        headers: { Authorization: "Bearer " + getToken() },
      });
      if (!res.ok) throw new Error("http_" + res.status);
      await loadBackupList();
    } catch (e) {
      alert("delete failed: " + (e.message || e));
    }
  }

  async function restoreBackup(id) {
    if (!confirm(
      "RESTORE will DROP the entire nautgate schema and reload it from this backup.\n\n" +
      "All data not in this backup (newer audit rows, scorecard changes, drift baselines, etc.) will be PERMANENTLY LOST.\n\n" +
      "Continue?"
    )) return;
    const stateEl = document.getElementById("bk-create-state");
    stateEl.textContent = "restoring…";
    try {
      const res = await fetch("/v1/backups/" + encodeURIComponent(id) + "/restore", {
        method: "POST",
        headers: { Authorization: "Bearer " + getToken(), "Content-Type": "application/json" },
        body: JSON.stringify({ confirm: true }),
      });
      if (!res.ok) {
        const detail = await res.text();
        throw new Error(`${res.status} ${detail.slice(0, 200)}`);
      }
      stateEl.textContent = "✓ restored";
      await loadBackupList();
    } catch (e) {
      stateEl.textContent = "✗ restore failed: " + (e.message || e);
    }
  }

  function humanBytes(n) {
    if (!n) return "—";
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
    if (n < 1024 * 1024 * 1024) return (n / (1024 * 1024)).toFixed(2) + " MB";
    return (n / (1024 * 1024 * 1024)).toFixed(2) + " GB";
  }

  // --- Settings -----------------------------------------------------------

  document.getElementById("prefs-save").addEventListener("click", async () => {
    const banned = csv(document.getElementById("banned-models").value);
    const preferred = csv(document.getElementById("preferred-models").value);
    const notes = document.getElementById("notes").value.trim() || null;
    const state = document.getElementById("prefs-state");
    try {
      await apiPut("/v1/profile", {
        banned_models: banned,
        preferred_models: preferred,
        notes,
      });
      state.textContent = "saved";
      state.classList.add("ok");
      state.classList.remove("bad");
    } catch (e) {
      state.textContent = "save failed";
      state.classList.add("bad");
      state.classList.remove("ok");
    }
  });

  async function loadSettings() {
    try {
      const p = await api("/v1/profile");
      document.getElementById("banned-models").value =
        (p.banned_models || []).join(", ");
      document.getElementById("preferred-models").value =
        (p.preferred_models || []).join(", ");
      document.getElementById("notes").value = p.notes || "";
    } catch (e) {
      /* swallow */
    }
    // Refresh the Backup, SB-ingest and Quality-eval sections every time Settings opens.
    await Promise.all([
      loadBackupConfig(), loadBackupList(), loadSBConfig(), loadQualityConfig(),
    ]);
    // Provider keys: read-only env hint. We don't have an endpoint that
    // exposes which keys are set (and shouldn't, for security). Hint at the
    // env-var contract instead.
    document.getElementById("keys-status").textContent =
      [
        "Provider keys live as env vars on the gateway:",
        "  ANTHROPIC_API_KEY",
        "  OPENAI_API_KEY",
        "  GEMINI_API_KEY",
        "  OPENROUTER_API_KEY",
        "  LMSTUDIO_BASE_URL",
        "",
        "Set them in deploy/.env and `docker compose up -d` to rotate.",
        "UI-managed key rotation is on the v2 roadmap.",
      ].join("\n");
  }

  // --- Helpers -----------------------------------------------------------

  function esc(s) {
    if (s === undefined || s === null) return "-";
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }
  function pct(x) {
    if (x === null || x === undefined) return "—";
    return (x * 100).toFixed(1) + "%";
  }
  function ms(x) {
    if (x === null || x === undefined) return "—";
    return Math.round(x) + "ms";
  }
  // (Timestamp formatters moved to the top of the IIFE — used by render
  // functions that may run before this line during boot.)
  function statusClass(code) {
    if (!code) return "";
    if (code >= 500) return "status-5xx";
    if (code >= 400) return "status-4xx";
    return "status-2xx";
  }
  function tokens(d) {
    const p = d.prompt_tokens;
    const c = d.completion_tokens;
    if (p == null && c == null) return "-";
    return (p ?? "?") + "/" + (c ?? "?");
  }
  function sensTag(s) {
    if (!s || s === "none") return "";
    return `<span class="tag sens-${esc(s)}">${esc(s)}</span>`;
  }
  function csv(s) {
    return s
      .split(",")
      .map((x) => x.trim())
      .filter(Boolean);
  }

  // --- Boot --------------------------------------------------------------

  // --- Auto-import sessions from URL fragment ---------------------------
  // The mint shell aliases (justclaude / justpi / etc.) open the dashboard
  // with #import=<base64-of-{token,label,agent_id}> to hand off a freshly-
  // minted token. The fragment is browser-only, never sent to the server.
  async function consumeImportFragment() {
    const m = (location.hash || "").match(/^#?import=([A-Za-z0-9+/=_-]+)/);
    if (!m) return null;
    let payload;
    try {
      const json = atob(m[1].replace(/-/g, "+").replace(/_/g, "/"));
      payload = JSON.parse(json);
    } catch (e) {
      console.warn("nautgate import: failed to decode fragment", e);
      return null;
    }
    if (!payload || !payload.token || !String(payload.token).startsWith("ng_")) return null;
    // Verify the token against /v1/whoami before saving.
    try {
      const res = await fetch("/v1/whoami", { headers: { Authorization: "Bearer " + payload.token } });
      if (!res.ok) {
        console.warn("nautgate import: /v1/whoami rejected the token", res.status);
        return null;
      }
      const me = await res.json();
      const sessions = loadSessions();
      const existing = sessions.find(s => s.token === payload.token);
      const label = payload.label || me.agent_id || "session";
      if (existing) {
        existing.label = label;
        existing.agent_id = me.agent_id;
        existing.key_id = me.key_id;
        existing.last_seen_at = new Date().toISOString();
      } else {
        sessions.push({
          id: cryptoId(),
          label,
          token: payload.token,
          agent_id: me.agent_id,
          key_id: me.key_id,
          last_seen_at: new Date().toISOString(),
        });
      }
      saveSessions(sessions);
      const newest = sessions[sessions.length - 1];
      setActiveSessionId(existing ? existing.id : newest.id);
      // Strip the fragment so a reload doesn't re-import (and so the token
      // isn't visible in the URL bar anymore).
      history.replaceState(null, "", location.pathname + location.search);
      return label;
    } catch (e) {
      console.warn("nautgate import: validation failed", e);
      return null;
    }
  }
  const importedLabel = await consumeImportFragment();

  // Start tab from URL hash, default to overview.
  const initial = (location.hash || "#overview").slice(1);
  if (document.getElementById("tab-" + initial)) {
    activateTab(initial);
  } else {
    activateTab("overview");
  }
  renderAuth();
  renderSessions();
  // Auto-discover OAuth-derived agents (claude-oauth-…, codex-…) and merge
  // them into the session picker so they show up without manual setup.
  // Runs once on load + every 60s thereafter so new logins appear within
  // a minute of their first request.
  discoverAgents();
  setInterval(discoverAgents, 60_000);
  // Start notification poller — runs every 60s while the tab is open.
  loadNotifications();
  setInterval(loadNotifications, 60_000);
  if (importedLabel) {
    // Tiny toast: 4s pinned banner on the Overview Sessions section.
    const banner = document.createElement("div");
    banner.style.cssText = "padding:8px 12px;margin:8px 0;border-left:3px solid #4caf50;background:rgba(76,175,80,0.08);font-size:13px";
    banner.textContent = `Session "${importedLabel}" imported and activated.`;
    document.getElementById("sessions-list")?.parentNode?.insertBefore(
      banner, document.getElementById("sessions-list")
    );
    setTimeout(() => banner.remove(), 4000);
  }

  // Auto-verify any session that's missing an agent_id, in the background.
  // This populates labels for legacy-imported sessions on first load.
  setTimeout(async () => {
    const sessions = loadSessions();
    let dirty = false;
    for (const s of sessions) {
      if (!s.agent_id && s.token) {
        await verifySession(s.id);
        dirty = true;
      }
    }
    if (dirty) { renderAuth(); renderSessions(); }
  }, 100);
})();
