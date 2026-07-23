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
  // Validate an ng_ token against /v1/whoami and, if good, save it as a session
  // and make it active. Shared by the Settings add-token form and the first-run
  // onboarding overlay. Returns { ok, error?, me? }. Never mints a key.
  async function activateToken(rawToken, label) {
    let token = (rawToken || "").replace(/^Bearer\s+/i, "").replace(/^["']|["']$/g, "").trim();
    if (!token) return { ok: false, error: "token required" };
    if (!token.startsWith("ng_")) {
      const looksLikeUuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(token);
      return { ok: false, error: looksLikeUuid
        ? "that's a key_id (UUID) — the token is the longer value that starts with ng_…"
        : "expected ng_… format (got " + token.slice(0, 8) + "…)" };
    }
    try {
      const res = await fetch("/v1/whoami", { headers: { Authorization: "Bearer " + token } });
      if (res.status === 401) return { ok: false, error: "401 — token rejected" };
      if (!res.ok) return { ok: false, error: "validation failed: " + res.status };
      const me = await res.json();
      const sessions = loadSessions();
      const existing = sessions.find((s) => s.token === token);
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
      if (!getActiveSessionId()) setActiveSessionId(sessions[sessions.length - 1].id);
      renderAuth(); renderSessions(); refreshActive();
      return { ok: true, me };
    } catch (e) {
      return { ok: false, error: "error: " + e.message };
    }
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
  // Click-to-sort state for the sessions table. key=null → localStorage order.
  let sessionSort = { key: null, dir: 1 };

  // Dashboard-local: hide auto-discovered sessions idle longer than this many
  // days so the list doesn't grow to hundreds of pages. Saved token sessions
  // and the active session are never hidden.
  function archiveDays() {
    const v = parseInt(localStorage.getItem("ng_session_archive_days") || "30", 10);
    return Number.isFinite(v) && v > 0 ? v : 30;
  }
  function withinArchiveWindow(sessions, activeId) {
    const cutoff = Date.now() - archiveDays() * 86400000;
    return sessions.filter(s =>
      s.id === activeId ||          // never hide the active session
      !s.last_seen_at ||            // never-used session (e.g. just-added token)
      new Date(s.last_seen_at).getTime() >= cutoff
    );
  }

  function sessionSortVal(s, key) {
    if (key === "label") return (s.label || s.agent_id || "").toLowerCase();
    if (key === "agent") return (s.agent_id || "").toLowerCase();
    if (key === "last_seen_at") return s.last_seen_at ? new Date(s.last_seen_at).getTime() : 0;
    return "";
  }
  function sortSessions(arr) {
    if (!sessionSort.key) return arr;
    const { key, dir } = sessionSort;
    return [...arr].sort((a, b) => {
      const va = sessionSortVal(a, key), vb = sessionSortVal(b, key);
      return va < vb ? -dir : va > vb ? dir : 0;
    });
  }
  function sortHead(key, label) {
    const active = sessionSort.key === key;
    const caret = active ? (sessionSort.dir === 1 ? " ▲" : " ▼") : "";
    return `<th class="sess-sort" data-sort="${key}" style="cursor:pointer;user-select:none">${label}${caret}</th>`;
  }

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
    const rawSessions = loadSessions();
    const activeId = getActiveSessionId() || (rawSessions[0]?.id ?? "");
    const allSessions = withinArchiveWindow(rawSessions, activeId);

    if (!allSessions.length) {
      list.innerHTML = '<p class="hint">No saved sessions. Add one below — paste a bearer token (ng_…) and optionally label it.</p>';
      renderSessionsPager(0);
      return;
    }

    const maxPage = Math.max(0, Math.ceil(allSessions.length / sessionPageSize) - 1);
    if (sessionPage > maxPage) sessionPage = maxPage;
    const sorted = sortSessions(allSessions);
    const sessions = sorted.slice(sessionPage * sessionPageSize, sessionPage * sessionPageSize + sessionPageSize);
    renderSessionsPager(allSessions.length);

    list.innerHTML = `<table class="sessions-table"><thead><tr><th></th>${sortHead("label", "label")}${sortHead("agent", "agent")}<th>token</th>${sortHead("last_seen_at", "last used")}<th></th></tr></thead><tbody>`
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

    // Click a sortable header → set/toggle sort, reset to first page.
    list.querySelectorAll("[data-sort]").forEach(th => th.addEventListener("click", () => {
      const k = th.getAttribute("data-sort");
      if (sessionSort.key === k) sessionSort.dir *= -1;
      else sessionSort = { key: k, dir: 1 };
      sessionPage = 0;
      renderSessions();
    }));

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
      const res = await fetch("/v1/agents/discovered?hours=" + (archiveDays() * 24),
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
    const r = await activateToken(
      document.getElementById("add-token").value,
      document.getElementById("add-label").value.trim(),
    );
    if (!r.ok) { errorEl.textContent = r.error; return; }
    document.getElementById("add-token").value = "";
    document.getElementById("add-label").value = "";
    document.getElementById("sessions-add").open = false;
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

  // Bind every sidebar item that declares a top-level tab — this includes the
  // footer (Settings) which lives outside <nav>. Scoped to `.sidebar` so the
  // Settings sub-nav (data-subtab, in page content) is never captured here.
  document.querySelectorAll(".sidebar a[data-tab]").forEach((a) => {
    a.addEventListener("click", (ev) => {
      ev.preventDefault();
      activateTab(a.dataset.tab);
      closeNavDrawer();  // collapse the mobile drawer after picking a page
    });
  });

  // --- Mobile nav drawer (collapsible sidebar) --------------------------
  const _shell = document.querySelector(".app-shell");
  function closeNavDrawer() { _shell && _shell.classList.remove("nav-open"); }
  document.getElementById("nav-toggle")?.addEventListener("click", () => {
    _shell && _shell.classList.toggle("nav-open");
  });
  document.getElementById("nav-backdrop")?.addEventListener("click", closeNavDrawer);
  // Esc closes the drawer too.
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeNavDrawer(); });

  // Header page title + one-line subtitle per tab (sidebar chrome).
  const PAGE_META = {
    overview:  ["Overview", "Live provider status & last-24h traffic"],
    audit:     ["Audit Log", "Every LLM call as it happens"],
    decisions: ["Decisions", "Recent routing decisions · refreshes every 5s"],
    health:    ["Provider Health", "Model availability & the in-process health tracker"],
    scorecard: ["Model Scorecard", "The brain layer — per-model trust scores"],
    behavior:  ["Behavior", "Did the model do what you asked?"],
    drift:     ["Behavior Drift", "Silent provider-side change detection"],
    quality:   ["Quality", "LLM-as-judge evaluations over the audit log"],
    probe:     ["LLM Probing", "Provenance & degradation monitoring"],
    insights:  ["Insights", "Counterfactuals, SPC & efficiency — the research view"],
    experiments: ["Experiments", "Champion–challenger evidence · blind-judged on real traffic"],
    modelhealth: ["Model Health", "One model, one verdict — trust, drift, behavior & probes"],
    improve:   ["Improvements", "Prompt coaching — learn from your own calls"],
    tooling:   ["Tooling", "What connected MCPs cost to carry & save in discovery"],
    bench:     ["Bench", "Same task, N models — behavior, tools, tokens & cost side by side"],
    reports:   ["Reports", "Print-ready usage & governance audits"],
    cost:      ["Cost", "Spend & subscription savings"],
    cache:     ["Prompt Cache", "Prompt-cache accounting & leak detector"],
    privacy:   ["Privacy", "Lighthouse-style audit of recent prompts"],
    models:    ["Models", "Routes from config/routing.yaml"],
    settings:  ["Settings", "Profile, memory ingest, quality eval, backups & keys"],
  };
  function setPageHeading(name) {
    const meta = PAGE_META[name] || [name, ""];
    const t = document.getElementById("page-title");
    const s = document.getElementById("page-subtitle");
    if (t) t.textContent = meta[0];
    if (s) s.textContent = meta[1];
  }

  // Per-page primary action button shown in the header (mock places these
  // top-right). Functions are hoisted so referencing them here is safe.
  function configureHeaderAction(name) {
    const btn = document.getElementById("header-action");
    if (!btn) return;
    const actions = {
      drift: { label: "📄 Generate report", fn: () => _generateDriftReport(false) },
      probe: { label: "▶ Run probe now", fn: () => runProbeNow() },
    };
    const a = actions[name];
    if (a) { btn.hidden = false; btn.textContent = a.label; btn.onclick = a.fn; }
    else { btn.hidden = true; btn.onclick = null; }
  }

  function activateTab(name) {
    activeTab = name;
    document.querySelectorAll(".sidebar a[data-tab]").forEach((a) =>
      a.classList.toggle("active", a.dataset.tab === name)
    );
    document.querySelectorAll(".tab").forEach((s) =>
      s.classList.toggle("active", s.id === "tab-" + name)
    );
    setPageHeading(name);
    configureHeaderAction(name);
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
    else if (activeTab === "insights") loadInsights();
    else if (activeTab === "experiments") loadExperiments();
    else if (activeTab === "modelhealth") loadModelHealth();
    else if (activeTab === "improve") loadImprove();
    else if (activeTab === "tooling") loadTooling();
    else if (activeTab === "bench") loadBench();
    else if (activeTab === "reports") { /* on-demand page — nothing to auto-load */ }
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

  let overviewWindow = { hours: 24, bucket: "hour" };
  document.querySelectorAll("#overview-window button").forEach((b) => {
    b.addEventListener("click", () => {
      overviewWindow = { hours: Number(b.dataset.hours), bucket: b.dataset.bucket || "day" };
      document.querySelectorAll("#overview-window button").forEach((x) => x.classList.toggle("active", x === b));
      loadOverview();
    });
  });

  async function loadOverview() {
    // Sessions section gets re-rendered every Overview load so the
    // last-used timestamps stay fresh as the active session makes calls.
    renderSessions();
    loadProviderStatus();
    loadIntelStrip();  // fire-and-forget — strip fills in when the aggregate lands
    // Render the layout with placeholders first so the page is never blank
    // (e.g. before a session is added, or while the first fetch is in flight).
    const kpisEl = document.getElementById("overview-kpis");
    if (kpisEl && !kpisEl.children.length) {
      NG.statRow(kpisEl, ["Requests", "Empty rate", "p50 latency", "p95 latency"].map(
        (label) => NG.statCard({ label, value: "—" })));
      renderOverviewBars("overview-tier-card", "Requests by tier", null);
    }
    try {
      const s = await api("/v1/stats?hours=" + overviewWindow.hours);
      const kpis = document.getElementById("overview-kpis");
      const total = s.requests_total ?? 0;
      if (kpis) NG.statRow(kpis, [
        NG.statCard({ label: "Requests", value: total.toLocaleString() }),
        NG.statCard({ label: "Empty rate", value: pct(s.empty_rate), sub: `${s.empty_count || 0} of ${total.toLocaleString()} calls` }),
        NG.statCard({ label: "p50 latency", value: ms(s.latency_ms?.p50 ?? s.latency_ms?.avg) }),
        NG.statCard({ label: "p95 latency", value: ms(s.latency_ms?.p95) }),
      ]);
      renderOverviewBars("overview-tier-card", "Requests by tier", s.requests_by_tier);
      renderOverviewRequestsChart();
    } catch (e) {
      // Silently leave dashes; auth state above will explain.
    }
  }

  // Requests-over-time area chart, derived by summing per-provider call
  // counts from the cost timeseries (no dedicated requests-series endpoint).
  async function renderOverviewRequestsChart() {
    const mount = document.getElementById("overview-requests-card");
    if (!mount) return;
    const body = NG.el("div");
    const chartEl = NG.el("div", { class: "v2-chart", html: '<p class="hint" style="padding:12px">loading…</p>' });
    body.appendChild(chartEl);
    mount.innerHTML = "";
    mount.appendChild(NG.card({ title: "Requests over time", body }));
    try {
      const ts = await api(`/v1/cost/timeseries?hours=${overviewWindow.hours}&bucket=${overviewWindow.bucket}`);
      const series = (ts && ts.series) || [];
      const allTs = new Set();
      series.forEach((sv) => (sv.points || []).forEach((p) => allTs.add(p.ts)));
      const labels = Array.from(allTs).sort();
      const x = labels.map((iso) => Math.floor(new Date(iso).getTime() / 1000));
      const totals = labels.map((t) => series.reduce((sum, sv) => {
        const pt = (sv.points || []).find((p) => p.ts === t);
        return sum + (pt ? (pt.calls || 0) : 0);
      }, 0));
      chartEl.innerHTML = "";
      if (x.length < 2) { chartEl.innerHTML = '<div class="v2-chart-fallback">Not enough traffic in this window.</div>'; return; }
      NG.chart(chartEl, {
        type: "area", x, height: 220,
        series: [{ label: "requests", values: totals, color: "#7C9BFF" }],
        fmtY: (v) => fmtNum(v),
        fmtX: (e) => overviewWindow.bucket === "day"
          ? new Date(e * 1000).toLocaleDateString([], { month: "short", day: "numeric" })
          : new Date(e * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }),
      });
    } catch (_e) { chartEl.innerHTML = '<div class="v2-chart-fallback">No traffic data.</div>'; }
  }

  // Ranked-bar card for an Overview {label: count} distribution.
  function renderOverviewBars(mountId, title, dict) {
    const mount = document.getElementById(mountId);
    if (!mount) return;
    const items = Object.entries(dict || {})
      .map(([k, v]) => ({ label: k, value: v }))
      .sort((a, b) => b.value - a.value);
    const body = items.length
      ? NG.rankedBars({ items, fmt: (v) => (v || 0).toLocaleString() })
      : NG.el("div", { class: "hint" }, "No traffic in this window.");
    mount.innerHTML = "";
    mount.appendChild(NG.card({ title, body }));
  }

  async function loadProviderStatus() {
    if (!getToken()) return;
    try {
      renderProviderStatus(await api("/v1/health/providers"));
    } catch (e) { /* leave prior state */ }
  }

  // Header pill + sidebar live dot, fed from the same /v1/health/providers
  // payload. Shows the single worst provider so the operator sees trouble
  // from any tab. Polled globally every 60s (loadGlobalStatus).
  const STATUS_RANK = { down: 3, degraded: 2, up: 1, "no-data": 0 };
  function renderHeaderStatus(d) {
    const pill = document.getElementById("header-status");
    const navDot = document.getElementById("nav-health-dot");
    if (!pill) return;
    const providers = (d && d.providers) || [];
    const clsMap = { up: "up", degraded: "degraded", down: "down", "no-data": "nodata" };
    let worst = null;
    for (const p of providers) {
      if (!worst || (STATUS_RANK[p.status] || 0) > (STATUS_RANK[worst.status] || 0)) worst = p;
    }
    const labelEl = document.getElementById("header-status-label");
    const detailEl = document.getElementById("header-status-detail");
    if (!worst) {
      pill.className = "header-status nodata";
      if (labelEl) labelEl.textContent = "No data";
      if (detailEl) detailEl.textContent = "";
    } else if (worst.status === "up") {
      pill.className = "header-status up";
      if (labelEl) labelEl.textContent = "All providers up";
      if (detailEl) detailEl.textContent = providers.length > 1 ? `· ${providers.length} live` : "";
    } else {
      pill.className = "header-status " + (clsMap[worst.status] || "nodata");
      if (labelEl) labelEl.textContent = `${worst.label} ${worst.status}`;
      const pctOv = worst.overload_pct ? `· ${(worst.overload_pct * 100).toFixed(0)}%` : "";
      if (detailEl) detailEl.textContent = pctOv;
    }
    // Sidebar Provider Health dot mirrors the worst status.
    if (navDot) {
      if (!worst || worst.status === "no-data") {
        navDot.hidden = true;
      } else {
        navDot.hidden = false;
        navDot.className = "nav-dot " + (clsMap[worst.status] || "nodata");
      }
    }
  }

  // Global pollers for chrome that must stay live on every tab.
  async function loadGlobalStatus() {
    if (!getToken()) return;
    try { renderHeaderStatus(await api("/v1/health/providers")); } catch (_e) {}
  }
  async function loadDriftBadge() {
    if (!getToken()) return;
    const badge = document.getElementById("nav-drift-badge");
    if (!badge) return;
    try {
      const d = await api("/v1/drift");
      const open = (d.alerts || []).filter((a) => a.is_open).length;
      if (open > 0) { badge.hidden = false; badge.textContent = String(open); }
      else { badge.hidden = true; }
    } catch (_e) {}
  }

  function renderProviderStatus(d) {
    renderHeaderStatus(d);
    const strip = document.getElementById("provider-strip");
    if (!strip) return;
    const providers = (d && d.providers) || [];
    strip.innerHTML = "";
    if (!providers.length) {
      strip.appendChild(NG.el("span", { class: "hint" }, "No provider data yet."));
      return;
    }
    const clsMap = { up: "up", degraded: "degraded", down: "down", "no-data": "nodata" };
    providers.forEach((p) => {
      let detail;
      if (p.status === "degraded" || p.status === "down") {
        const bits = [];
        if (p.overload_pct > 0) bits.push(`${(p.overload_pct * 100).toFixed(0)}% overloaded`);
        else bits.push(p.status);
        if (p.retries_absorbed) bits.push(`${p.retries_absorbed} absorbed`);
        if (p.rate_limited) bits.push(`${p.rate_limited}× 429`);
        detail = bits.join(" · ");
      } else if (p.status === "up") {
        detail = p.heartbeat && p.heartbeat.latency_ms != null ? `up · ${p.heartbeat.latency_ms}ms` : (p.total ? `up · ${p.success}/${p.total} ok` : "up");
      } else {
        detail = p.heartbeat && p.heartbeat.status === "no-cred" ? "no credential" : "no recent calls";
      }
      const card = NG.el("div", { class: "prov-card prov-" + (clsMap[p.status] || "nodata") });
      card.appendChild(NG.el("span", { class: "prov-dot" }));
      const nameWrap = NG.el("div", { class: "prov-name" });
      nameWrap.appendChild(NG.el("span", null, p.label));
      if (/anthropic/i.test(p.label) && p.max_subscription) nameWrap.appendChild(NG.el("span", { class: "prov-tag" }, "(Max)"));
      card.appendChild(nameWrap);
      card.appendChild(NG.el("span", { class: "prov-detail" }, detail));
      strip.appendChild(card);
    });
  }

  document.getElementById("sessions-prev")?.addEventListener("click", () => { sessionPage--; renderSessions(); });
  document.getElementById("sessions-next")?.addEventListener("click", () => { sessionPage++; renderSessions(); });
  document.getElementById("sessions-page-size")?.addEventListener("change", (e) => {
    sessionPageSize = Number(e.target.value) || 10; sessionPage = 0; renderSessions();
  });

  // --- Decisions ----------------------------------------------------------

  let decTable = null;
  async function loadDecisions() {
    if (!getToken()) return;
    const mount = document.getElementById("dec-card");
    if (!mount) return;
    try {
      const r = await api("/v1/decisions/recent?limit=50");
      const rows = r.data || [];
      if (!decTable) {
        decTable = NG.DataTable(mount, {
          title: "Recent decisions",
          countLabel: (n) => `${n} call${n === 1 ? "" : "s"}`,
          searchPlaceholder: "Filter…",
          defaultSort: { key: "ts", dir: "desc" },
          emptyText: "No decisions yet.",
          rows,
          onRowClick: (d) => openDetail(d.decision_id),
          columns: [
            { key: "ts", label: "Time", render: (d) => tsShort(d.ts), sortValue: (d) => d.ts || "" },
            { key: "inbound_format", label: "Fmt", render: (d) => d.inbound_format || "—", sortValue: (d) => d.inbound_format || "" },
            { key: "tier", label: "Tier", render: (d) => NG.tierPill(d.tier || "—"), sortValue: (d) => d.tier || "" },
            { key: "score", label: "Score", align: "right", render: (d) => (d.score ?? 0).toFixed(2), sortValue: (d) => d.score || 0 },
            { key: "sensitivity", label: "Sens", sortable: false, render: (d) => sensTag(d.sensitivity) },
            { key: "provider", label: "Provider", render: (d) => NG.providerTag(d.provider), sortValue: (d) => d.provider || "" },
            { key: "model", label: "Model", render: (d) => shortModelName(d.model), sortValue: (d) => d.model || "" },
            { key: "status_code", label: "Status", align: "right", render: (d) => NG.el("span", { class: statusClass(d.status_code) }, String(d.status_code ?? "—")), sortValue: (d) => d.status_code || 0 },
            { key: "ms", label: "ms", align: "right", render: (d) => (d.duration_ms ?? "—"), sortValue: (d) => d.duration_ms || 0 },
            { key: "tok", label: "Tokens", align: "right", render: (d) => tokens(d), sortValue: (d) => (d.prompt_tokens || 0) + (d.completion_tokens || 0) },
            { key: "cost", label: "Cost", align: "right", render: (d) => costShort(d), sortValue: (d) => d.cost_usd || 0 },
          ],
        });
      } else {
        decTable.setRows(rows);
      }
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
    // The findings scan is slow (server-side scan of up to 500 decisions),
    // so show a spinner and hide stale content while it runs.
    const loadEl = document.getElementById("privacy-loading");
    const hero = document.querySelector("#tab-privacy .lh-hero");
    const cats = document.getElementById("lh-categories");
    if (loadEl) { loadEl.hidden = false; loadEl.innerHTML = ""; loadEl.appendChild(NG.spinner("Scanning recent prompts…")); }
    if (hero) hero.style.opacity = "0.35";
    if (cats) cats.style.opacity = "0.35";
    try {
      const r = await api(`/v1/findings/summary?hours=${privacyWindow}&scan_limit=500`);
      renderPrivacy(r);
    } catch (e) {
      /* swallow */
    } finally {
      if (loadEl) { loadEl.hidden = true; loadEl.innerHTML = ""; }
      if (hero) hero.style.opacity = "";
      if (cats) cats.style.opacity = "";
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

  // ── Session pulse — one bar per call, chronological. Height = output+thinking
  // tokens (sqrt scale), color = actual model, lighter cap = thinking share,
  // red = error, amber ring = fallback. Click = open the call in the drawer.
  const PULSE_COLORS = ["var(--series-1)", "var(--series-2)", "var(--series-3)", "var(--series-4)"];
  function renderAuditPulse(rows) {
    const host = document.getElementById("audit-pulse");
    if (!host) return;
    if (rows.length < 2) { host.innerHTML = ""; return; }
    const chrono = rows.slice().reverse();
    const modelColor = new Map();
    const colorFor = (m) => {
      if (!modelColor.has(m)) modelColor.set(m, PULSE_COLORS[modelColor.size % PULSE_COLORS.length]);
      return modelColor.get(m);
    };
    const maxTok = Math.max(1, ...chrono.map((r) => (r.completion_tokens || 0) + (r.reasoning_tokens || 0)));
    const bars = chrono.map((r) => {
      const out = r.completion_tokens || 0;
      const think = r.reasoning_tokens || 0;
      const h = Math.max(4, Math.round(Math.sqrt((out + think) / maxTok) * 56));
      const model = r.actual_model || r.model || "?";
      const isErr = r.status_code && r.status_code >= 400;
      const color = isErr ? "var(--bad)" : colorFor(model);
      const thinkPct = out + think > 0 ? Math.round((think / (out + think)) * 100) : 0;
      const fb = r.used_fallback || (r.fallback_count && r.fallback_count > 0);
      const substituted = r.actual_model && r.model && r.actual_model !== r.model;
      const tip = [
        shortModelName(model) + (substituted ? " (⇄ asked: " + shortModelName(r.model) + ")" : ""),
        `${out} out` + (think ? ` + ${think} thinking (${thinkPct}%)` : ""),
        r.first_byte_ms != null ? `first byte ${r.first_byte_ms}ms` : null,
        r.duration_ms != null ? `${r.duration_ms}ms total` : null,
        r.cost_usd != null ? usd(r.cost_usd) : null,
        fb ? `⚠ fell back ×${r.fallback_count || 1}` : null,
        isErr ? `HTTP ${r.status_code}` : null,
        tsShort(r.ts),
      ].filter(Boolean).join(" · ");
      const thinkCap = think > 0
        ? `<div class="pulse-think" style="height:${Math.max(1, Math.round(h * think / (out + think)))}px"></div>`
        : "";
      const marks = (fb ? '<span class="pulse-mark">▲</span>' : "") + (substituted ? '<span class="pulse-mark">⇄</span>' : "");
      return `<div class="pulse-slot${auditExpandedId === r.decision_id ? " selected" : ""}" data-decision="${esc(r.decision_id)}" title="${esc(tip)}">
          ${marks}<div class="pulse-bar" style="height:${h}px;background:${color}">${thinkCap}</div>
        </div>`;
    }).join("");
    const legend = [...modelColor.entries()].map(([m, c]) =>
      `<span><span class="swatch" style="background:${c}"></span>${esc(shortModelName(m))}</span>`).join("");
    host.innerHTML = `
      <div class="pulse-strip">${bars}</div>
      <div class="audit-legend pulse-legend">${legend}
        <span class="hint">bar = output tokens · light cap = thinking · ▲ fallback · ⇄ substituted model</span>
      </div>`;
    host.querySelectorAll(".pulse-slot").forEach((el) => {
      el.addEventListener("click", () => toggleAuditDetail(el.dataset.decision));
    });
  }

  function renderAudit(rows, agentId) {
    renderAuditPulse(rows);
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
          </div>`;
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
            // Re-open to refresh the detail panel with the fresh eval/coach data.
            auditExpandedId = null;
            toggleAuditDetail(did);
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
    // Re-mark the selected row + repopulate the detail panel after a live refresh.
    if (auditExpandedId) {
      const row = document.querySelector(`.audit-row[data-decision="${CSS.escape(auditExpandedId)}"]`);
      if (row) row.classList.add("selected");
      const panel = document.getElementById("audit-detail-panel");
      if (panel && auditDetailCache.has(auditExpandedId)) {
        panel.innerHTML = renderAuditDetail(auditDetailCache.get(auditExpandedId));
      }
    }
  }

  // Inline split-view: render the decision detail into the right-hand panel.
  async function toggleAuditDetail(decisionId) {
    const panel = document.getElementById("audit-detail-panel");
    if (!panel) return;
    if (auditExpandedId === decisionId) {
      auditExpandedId = null;
      document.querySelectorAll(".audit-row.selected, .pulse-slot.selected").forEach((r) => r.classList.remove("selected"));
      panel.innerHTML = '<p class="hint" style="padding:18px">Select a call to inspect its token anatomy, prompt &amp; response.</p>';
      return;
    }
    auditExpandedId = decisionId;
    document.querySelectorAll(".audit-row").forEach((r) => r.classList.toggle("selected", r.dataset.decision === decisionId));
    document.querySelectorAll(".pulse-slot").forEach((r) => r.classList.toggle("selected", r.dataset.decision === decisionId));
    panel.innerHTML = '<p class="hint" style="padding:18px">loading…</p>';
    try {
      const scope = getActiveAgentScope();
      const url = "/v1/decisions/" + encodeURIComponent(decisionId)
        + (scope ? "?agent_id=" + encodeURIComponent(scope) : "");
      const d = await api(url);
      auditDetailCache.set(decisionId, d);
      panel.innerHTML = renderAuditDetail(d);
    } catch (e) {
      panel.innerHTML = '<p class="hint" style="padding:18px">failed to load</p>';
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
    // Coach on top — the judge's verdict is the first thing you want on a call.
    html += `
      <div style="display:flex;align-items:center;justify-content:space-between;gap:8px">
        <div class="section-title" style="margin:0">Analysis</div>
        <button class="ghost audit-call-report" data-decision="${esc(d.decision_id || "")}" title="Standalone HTML report for this call — overlay, open in tab, or download">📄 report</button>
        <button class="ghost audit-call-flow" data-decision="${esc(d.decision_id || "")}" title="Routing flow for this call — client → lane → decision → provider → model actually served">🔀 flow</button>
      </div>
      <details class="coach-accordion" data-decision="${esc(d.decision_id || "")}">
        <summary>▸ Coach <span class="hint">(judge eval, click to load)</span></summary>
        <div class="coach-body"><p class="hint">loading…</p></div>
      </details>`;
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
    if (d.used_fallback || (d.fallback_count && d.fallback_count > 0)) {
      html += grid("Degraded", `<span style="color:var(--warn)">yes · ${d.fallback_count || 1} fallback${(d.fallback_count || 1) > 1 ? "s" : ""}</span>`);
    }
    html += "</div>";

    // Fallback chain — the route the request would degrade through, with the
    // actually-used leg implied by decision → actual above.
    if (Array.isArray(d.fallback_chain) && d.fallback_chain.length > 1) {
      html += '<div class="section-title">Fallback chain</div>';
      html += '<div class="hint" style="padding:2px 0 6px">'
        + d.fallback_chain.map((f) => esc(typeof f === "string" ? f : (f.model || f.provider || JSON.stringify(f)))).join(" → ")
        + "</div>";
    }

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

  // ── Per-call HTML report — drift-style overlay, built client-side from the
  // detail row + judge eval already available to the dashboard. No bodies in
  // the report: it stays screenshot/share-safe like the drift share view.
  document.addEventListener("click", async (ev) => {
    const btn = ev.target && ev.target.closest ? ev.target.closest(".audit-call-report") : null;
    if (!btn) return;
    const did = btn.dataset.decision;
    if (!did) return;
    btn.disabled = true;
    const prev = btn.textContent;
    btn.textContent = "building…";
    try {
      let d = auditDetailCache.get(did);
      if (!d) {
        const scope = getActiveAgentScope();
        d = await api("/v1/decisions/" + encodeURIComponent(did) + (scope ? "?agent_id=" + encodeURIComponent(scope) : ""));
        auditDetailCache.set(did, d);
      }
      let evalRow = null;
      try {
        const r = await fetch("/v1/quality/evaluation/" + encodeURIComponent(did), {
          headers: { Authorization: "Bearer " + getToken() },
        });
        if (r.ok) evalRow = await r.json();
      } catch (_e) { /* report renders without eval */ }
      _showCallReportModal(d, evalRow);
    } catch (e) {
      btn.textContent = "✗ failed";
      setTimeout(() => { btn.textContent = prev; }, 2000);
      btn.disabled = false;
      return;
    }
    btn.disabled = false;
    btn.textContent = prev;
  });

  function _buildCallReportHtml(d, ev) {
    const rubric = (ev && ev.rubric) || {};
    const kv = (k, v) => `<div class="kv"><span class="k">${esc(k)}</span><span class="v">${v}</span></div>`;
    const score = (label, v) => `
      <div class="score"><div class="sl">${esc(label)}</div><div class="sv">${v == null ? "—" : v + "<small>/5</small>"}</div></div>`;
    const t = d.token_estimate || { system: 0, tools: 0, history: 0, user: 0 };
    const actual = d.actual_model && d.actual_model !== d.decision_model
      ? ` → <b>${esc(d.actual_model)}</b>${d.actual_provider ? " (" + esc(d.actual_provider) + ")" : ""}` : "";
    const degraded = d.used_fallback || (d.fallback_count && d.fallback_count > 0)
      ? `<span class="warn">yes · ×${d.fallback_count || 1}</span>` : "no";
    const share = typeof rubric.irrelevant_share === "number" ? rubric.irrelevant_share : null;
    const cats = Array.isArray(rubric.data_categories_shared) ? rubric.data_categories_shared : [];
    const items = Array.isArray(rubric.irrelevant_items) ? rubric.irrelevant_items : [];
    const tags = (ev && ev.failure_tags) || [];
    const signals = (d.classified_signals || [])
      .map((s) => `<li>${esc(s.rule_id)} · ${esc(s.severity || s.sensitivity || "")} · ×${s.count || 1}</li>`).join("");
    const bloat = (d.bloat_findings || [])
      .map((f) => `<li><b>${esc(f.type)}</b> [${esc(f.severity || "info")}] — ${esc(f.detail || "")}</li>`).join("");
    const judgeBlock = ev ? `
      <div class="scores">
        ${score("Task understanding", rubric.task_understanding)}
        ${score("Task completion", rubric.task_completion)}
        ${score("Reasoning efficiency", rubric.reasoning_efficiency)}
        ${score("Action compliance", rubric.action_compliance)}
        ${score("Prompt clarity", rubric.prompt_clarity)}
      </div>
      ${tags.length ? `<p>${tags.map((x) => `<span class="tag">${esc(String(x).replace(/_/g, " "))}</span>`).join(" ")}</p>` : ""}
      ${cats.length || share != null ? `
        <h3>Data shared &amp; relevance</h3>
        ${cats.length ? `<p>${cats.map((c) => `<span class="tag">${esc(String(c).replace(/_/g, " "))}</span>`).join(" ")}</p>` : ""}
        ${share != null ? `<p><b class="${share >= 50 ? "bad" : share >= 20 ? "warn" : "good"}">${share}%</b> of the payload judged irrelevant to the task</p>` : ""}
        ${items.length ? `<ul>${items.map((i) => `<li>${esc(String(i))}</li>`).join("")}</ul>` : ""}` : ""}
      ${ev.suggested_prompt ? `<h3>Suggested better prompt</h3><pre>${esc(ev.suggested_prompt)}</pre>` : ""}
      ${ev.coach_notes ? `<p class="dim"><b>Notes:</b> ${esc(ev.coach_notes)}</p>` : ""}
      <p class="dim small">judge: ${esc(ev.judge_provider || "")}/${esc(ev.judge_model || "")} · trigger: ${esc(ev.trigger || "?")}${ev.judge_cost_usd != null ? " · $" + Number(ev.judge_cost_usd).toFixed(4) : ""}</p>`
      : '<p class="dim">No judge evaluation for this call — run one from the Coach panel in the audit drawer.</p>';
    return `<!doctype html><html><head><meta charset="utf-8"><title>NautGate call report</title><style>
      body { background:#0A0D12; color:#E6EBF2; font: 13px/1.55 ui-monospace,"SF Mono",Menlo,monospace; margin:0; padding:32px; }
      .wrap { max-width: 760px; margin: 0 auto; }
      h1 { font-size:16px; color:#C3CE1F; margin:0 0 2px; } h3 { font-size:12px; color:#8893A4; text-transform:uppercase; letter-spacing:.08em; margin:22px 0 8px; border-bottom:1px solid #232B36; padding-bottom:4px; }
      .sub { color:#8893A4; font-size:11px; margin-bottom:20px; }
      .grid { display:grid; grid-template-columns:1fr 1fr; gap:4px 24px; }
      .kv { display:flex; justify-content:space-between; gap:12px; border-bottom:1px dotted #232B36; padding:3px 0; }
      .kv .k { color:#5C6675; } .kv .v { text-align:right; }
      .scores { display:flex; gap:10px; flex-wrap:wrap; margin:10px 0; }
      .score { background:#12161F; border:1px solid #232B36; border-radius:6px; padding:8px 12px; min-width:104px; }
      .sl { color:#5C6675; font-size:10px; } .sv { font-size:20px; } .sv small { color:#5C6675; font-size:11px; }
      .tag { background:#1A2029; border:1px solid #232B36; border-radius:10px; padding:2px 8px; font-size:11px; }
      pre { background:#12161F; border:1px solid #232B36; border-radius:6px; padding:10px; white-space:pre-wrap; }
      ul { margin:6px 0; padding-left:18px; } li { margin:2px 0; }
      .good { color:#3FB950; } .warn { color:#D6A100; } .bad { color:#E5484D; } .dim { color:#8893A4; } .small { font-size:11px; }
      .foot { margin-top:28px; color:#5C6675; font-size:10px; border-top:1px solid #232B36; padding-top:8px; }
    </style></head><body><div class="wrap">
      <h1>NautGate · Call report</h1>
      <div class="sub">${esc(d.ts || "")} · decision ${esc(d.decision_id || "")}</div>
      <h3>Call</h3>
      <div class="grid">
        ${kv("Endpoint", esc(inboundEndpoint(d.inbound_format)))}
        ${kv("Upstream", esc((d.decision_provider || "—") + " / " + (d.decision_model || "—")) + actual)}
        ${kv("Status", String(d.status_code ?? "—"))}
        ${kv("Degraded", degraded)}
        ${kv("First byte", (d.first_byte_ms ?? "—") + " ms")}
        ${kv("Duration", (d.duration_ms ?? "—") + " ms")}
        ${kv("Input tokens", String(d.prompt_tokens ?? "—"))}
        ${kv("Output tokens", String(d.completion_tokens ?? "—"))}
        ${kv("Reasoning tokens", String(d.reasoning_tokens ?? "—"))}
        ${kv("Cost", d.cost_usd != null ? "$" + Number(d.cost_usd).toFixed(4) : "—")}
        ${kv("Tier · Score", esc((d.classified_tier || "—") + " · " + (d.classified_score ?? 0).toFixed(2)))}
        ${kv("Sensitivity", esc(d.classified_sensitivity || "none"))}
        ${kv("Messages · Tools", (d.messages_count ?? "—") + " · " + (d.tools_count ?? "—"))}
        ${kv("Request size", d.request_size_bytes != null ? (d.request_size_bytes / 1024).toFixed(1) + " KB" : "—")}
      </div>
      <h3>Payload split (est. tokens)</h3>
      <div class="grid">
        ${kv("System", String(t.system))} ${kv("Tools", String(t.tools))}
        ${kv("History", String(t.history))} ${kv("User", String(t.user))}
      </div>
      <h3>Judge verdict</h3>
      ${judgeBlock}
      ${signals ? `<h3>Sensitivity findings</h3><ul>${signals}</ul>` : ""}
      ${bloat ? `<h3>Bloat findings — score −${(d.bloat_score || 0).toFixed(3)}${d.estimated_waste_usd ? " · est. waste $" + d.estimated_waste_usd.toFixed(4) : ""}</h3><ul>${bloat}</ul>` : ""}
      <div class="foot">Generated by NautGate · bodies excluded — this report is safe to share/screenshot</div>
    </div></body></html>`;
  }

  function _showCallReportModal(d, evalRow) {
    document.getElementById("audit-report-modal")?.remove();
    const html = _buildCallReportHtml(d, evalRow);
    const wrap = document.createElement("div");
    wrap.id = "audit-report-modal";
    wrap.className = "dr-report-modal";
    wrap.innerHTML = `
      <div class="dr-report-content">
        <div class="dr-report-head">
          <span>Call report · ${esc(shortModelName(d.actual_model || d.decision_model || "?"))} · ${esc(tsShort(d.ts))}</span>
          <div class="dr-report-actions">
            <button class="ghost" id="audit-report-tab" title="Open in a new tab">🖼 open tab</button>
            <button class="ghost" id="audit-report-download">💾 download</button>
            <button class="ghost" id="audit-report-close">✕ close</button>
          </div>
        </div>
        <iframe class="audit-report-frame" sandbox=""></iframe>
      </div>`;
    document.body.appendChild(wrap);
    wrap.querySelector(".audit-report-frame").srcdoc = html;
    const blobUrl = () => URL.createObjectURL(new Blob([html], { type: "text/html" }));
    document.getElementById("audit-report-close").addEventListener("click", () => wrap.remove());
    document.getElementById("audit-report-tab").addEventListener("click", () => {
      window.open(blobUrl(), "_blank", "noopener");
    });
    document.getElementById("audit-report-download").addEventListener("click", () => {
      const a = document.createElement("a");
      a.href = blobUrl();
      a.download = `nautgate-call-${(d.decision_id || "report").slice(0, 8)}.html`;
      document.body.appendChild(a);
      a.click();
      a.remove();
    });
    wrap.addEventListener("click", (e) => { if (e.target === wrap) wrap.remove(); });
  }

  // ── Per-call ROUTING FLOW — the hop-by-hop path a request actually took.
  // Exists because "which model answered?" cannot be settled by asking the
  // model: the client's system prompt dictates the identity it claims (Claude
  // Code tells any routed model it is Fable 5). Only the upstream-reported
  // `actual_model` is attested, so the flow makes requested-vs-served explicit.
  document.addEventListener("click", async (ev) => {
    const btn = ev.target && ev.target.closest ? ev.target.closest(".audit-call-flow") : null;
    if (!btn) return;
    const did = btn.dataset.decision;
    if (!did) return;
    btn.disabled = true;
    const prev = btn.textContent;
    btn.textContent = "building…";
    try {
      let d = auditDetailCache.get(did);
      if (!d) {
        const scope = getActiveAgentScope();
        d = await api("/v1/decisions/" + encodeURIComponent(did) + (scope ? "?agent_id=" + encodeURIComponent(scope) : ""));
        auditDetailCache.set(did, d);
      }
      _showCallFlowModal(d);
    } catch (e) {
      NG.toast?.("flow failed: " + (e.message || e));
    } finally {
      btn.disabled = false;
      btn.textContent = prev;
    }
  });

  function _buildCallFlowHtml(d) {
    const S = (v) => esc(v == null ? "—" : String(v));
    const kb = (b) => (b == null ? "—" : (b / 1024).toFixed(1) + " KB");
    const ms = (v) => (v == null ? "—" : v < 1000 ? v + " ms" : (v / 1000).toFixed(1) + " s");
    const req = d.model_requested || "—";
    const served = d.actual_model || d.decision_model || "—";
    // Substitution = the client asked for one model and a different one answered.
    // Compare loosely: served ids often carry a provider namespace prefix.
    const bare = (m) => String(m || "").split("/").pop().toLowerCase();
    const substituted = req !== "—" && served !== "—" && bare(req) !== bare(served);
    const lane = d.inbound_format || "—";
    const oauthLane = /oauth|_ws$/.test(lane);

    // Upstream hop. actual_provider is only known once the outcome lands (and
    // for OpenRouter it names the sub-host that actually ran the model, e.g.
    // "Inceptron"). Until then, derive the network the call goes out over from
    // the model namespace — "openrouter/moonshotai/kimi-k2.6" → openrouter.
    // Never fall back to decision_provider: that just mirrors the DECISION node
    // and makes an unknown look like a fact.
    const ns = (m) => {
      const s = String(m || "");
      return s.includes("/") ? s.split("/")[0] : "";
    };
    const route = ns(d.decision_model) || ns(d.actual_model)
      || (/^claude/.test(d.decision_model || "") ? "anthropic" : "")
      || (/^(gpt-|o[134])/.test(d.decision_model || "") ? "openai" : "");
    const pending = d.status_code == null && d.duration_ms == null;
    const upstreamTitle = route || d.actual_provider || "—";
    const upstreamSub = pending
      ? "no outcome recorded yet"
      : d.used_fallback ? `via fallback (${d.fallback_count})`
      : (d.actual_provider && d.actual_provider !== route)
        ? `served by ${d.actual_provider}` : "direct";

    const node = (kind, title, sub, rows) => `
      <div class="node ${kind}">
        <div class="node-kind">${S(kind.toUpperCase())}</div>
        <div class="node-title">${S(title)}</div>
        ${sub ? `<div class="node-sub">${S(sub)}</div>` : ""}
        ${rows && rows.length ? `<dl>${rows.map(([k, v]) => `<dt>${S(k)}</dt><dd>${S(v)}</dd>`).join("")}</dl>` : ""}
      </div>`;
    const arrow = (label, warn) => `
      <div class="arrow${warn ? " arrow-warn" : ""}">
        <div class="arrow-line"></div>
        ${label ? `<div class="arrow-label">${S(label)}</div>` : ""}
      </div>`;

    const chain = [
      node("client", d.agent_id || "unknown client",
        [d.source_hostname, d.source_ip].filter(Boolean).join(" · "),
        [["asked for", req], ["messages", d.messages_count], ["tools sent", d.tools_count]]),
      arrow(d.stream_flag ? "stream" : "single"),
      node("inbound", lane, oauthLane ? "subscription lane" : "api-key lane",
        [["request", kb(d.request_size_bytes)], ["tier", d.classified_tier],
         ["sensitivity", d.classified_sensitivity]]),
      arrow("decide"),
      node("decision", d.decision_provider || "—", d.decision_reason || "",
        [["target", d.decision_model]]),
      arrow(substituted ? "SUBSTITUTED" : "forward", substituted),
      node("upstream", upstreamTitle, upstreamSub,
        [["ttfb", ms(d.first_byte_ms)], ["total", ms(d.duration_ms)], ["status", d.status_code]]),
      arrow("served"),
      node("served", served.split("/").pop(),
        pending ? "requested target — outcome not recorded yet"
                : d.actual_model ? "upstream-reported — attested, not echoed"
                                 : "routing target — upstream did not report a model",
        [["route", served.includes("/") ? served : route || "direct"],
         ["in", d.prompt_tokens], ["out", d.completion_tokens],
         ["reasoning", d.reasoning_tokens],
         ["cost", d.cost_usd == null ? "unpriced" : "$" + Number(d.cost_usd).toFixed(4)]]),
    ].join("");

    const banner = substituted
      ? `<div class="banner warn"><b>Model substituted.</b> The client asked for
           <code>${S(req)}</code> and <code>${S(served)}</code>
           ${pending ? "was routed to (no outcome recorded yet)" : "answered"}.
           ${S(d.decision_reason || "")}</div>`
      : `<div class="banner ok">Served by the model the client requested — no substitution.</div>`;

    return `<!doctype html><html><head><meta charset="utf-8">
      <title>NautGate routing flow</title><style>
      :root{--bg:#0A0D12;--card:#12161F;--raised:#1A2029;--line:#232B36;--tx:#E6EBF2;
            --dim:#8893A4;--lb:#5C6675;--ac:#C3CE1F;--good:#3FB950;--warn:#D6A100;--bad:#E5484D;
            --mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace}
      *{box-sizing:border-box}
      body{margin:0;background:var(--bg);color:var(--tx);
           font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;padding:22px}
      h1{font-size:17px;margin:0 0 2px}
      .meta{color:var(--lb);font-size:12px;font-family:var(--mono);margin-bottom:16px}
      .banner{border-radius:8px;padding:10px 13px;margin-bottom:18px;font-size:13px}
      .banner.warn{background:rgba(214,161,0,.12);border:1px solid rgba(214,161,0,.45)}
      .banner.ok{background:rgba(63,185,80,.10);border:1px solid rgba(63,185,80,.35)}
      .banner code{font-family:var(--mono);color:var(--ac)}
      .flow{display:flex;align-items:stretch;gap:0;overflow-x:auto;padding-bottom:8px}
      .node{flex:0 0 190px;background:var(--card);border:1px solid var(--line);
            border-radius:10px;padding:11px 12px}
      .node-kind{font-size:9.5px;letter-spacing:.09em;color:var(--lb)}
      .node-title{font-weight:600;font-size:13px;color:var(--ac);margin-top:3px;
                  word-break:break-word}
      .node-sub{font-size:10.5px;color:var(--dim);margin-top:2px;word-break:break-word}
      .node dl{display:grid;grid-template-columns:auto 1fr;gap:2px 8px;margin:9px 0 0}
      .node dt{font-size:10px;color:var(--lb)}
      .node dd{margin:0;font-family:var(--mono);font-size:10.5px;text-align:right}
      .node.served{border-color:rgba(128,128,0,.5)}
      .arrow{flex:0 0 62px;display:flex;flex-direction:column;justify-content:center;
             align-items:center;gap:4px}
      .arrow-line{width:100%;height:2px;background:var(--line);position:relative}
      .arrow-line:after{content:"";position:absolute;right:0;top:-3px;border:4px solid transparent;
                        border-left-color:var(--line)}
      .arrow-label{font-size:9px;color:var(--lb);font-family:var(--mono);text-align:center}
      .arrow-warn .arrow-line{background:var(--warn)}
      .arrow-warn .arrow-line:after{border-left-color:var(--warn)}
      .arrow-warn .arrow-label{color:var(--warn);font-weight:700}
      .note{margin-top:18px;border-top:1px solid var(--line);padding-top:12px;
            font-size:11.5px;color:var(--dim);max-width:820px}
      .note b{color:var(--tx)}
      .foot{margin-top:14px;color:var(--lb);font-size:10.5px}
      </style></head><body>
      <h1>Routing flow</h1>
      <div class="meta">${S(d.decision_id)} · ${S(d.ts)}</div>
      ${banner}
      <div class="flow">${chain}</div>
      <div class="note"><b>Why the served model is trustworthy:</b> it is parsed from the
        upstream response, not copied from the request — so it reflects what actually
        generated the tokens. <b>Asking the model who it is does not work:</b> the client's
        system prompt asserts an identity, so any routed model will repeat it.</div>
      <div class="foot">Generated by NautGate · no prompt or response bodies included — safe to share</div>
      </body></html>`;
  }

  function _showCallFlowModal(d) {
    document.getElementById("audit-flow-modal")?.remove();
    const html = _buildCallFlowHtml(d);
    const wrap = document.createElement("div");
    wrap.id = "audit-flow-modal";
    wrap.className = "dr-report-modal";
    wrap.innerHTML = `
      <div class="dr-report-content">
        <div class="dr-report-head">
          <span>Routing flow · ${esc(shortModelName(d.model_requested || "?"))} → ${esc(shortModelName(d.actual_model || d.decision_model || "?"))}</span>
          <div class="dr-report-actions">
            <button class="ghost" id="audit-flow-tab" title="Open in a new tab">🖼 open tab</button>
            <button class="ghost" id="audit-flow-download">💾 download</button>
            <button class="ghost" id="audit-flow-close">✕ close</button>
          </div>
        </div>
        <iframe class="audit-report-frame" sandbox=""></iframe>
      </div>`;
    document.body.appendChild(wrap);
    wrap.querySelector(".audit-report-frame").srcdoc = html;
    const blobUrl = () => URL.createObjectURL(new Blob([html], { type: "text/html" }));
    document.getElementById("audit-flow-close").addEventListener("click", () => wrap.remove());
    document.getElementById("audit-flow-tab").addEventListener("click", () => {
      window.open(blobUrl(), "_blank", "noopener");
    });
    document.getElementById("audit-flow-download").addEventListener("click", () => {
      const a = document.createElement("a");
      a.href = blobUrl();
      a.download = `nautgate-flow-${(d.decision_id || "flow").slice(0, 8)}.html`;
      document.body.appendChild(a);
      a.click();
      a.remove();
    });
    wrap.addEventListener("click", (e) => { if (e.target === wrap) wrap.remove(); });
  }

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
    // Data & relevance — what the prompt shipped and how much of it the task
    // didn't need. Older evals won't have these keys; render nothing then.
    let relevance = "";
    const cats = Array.isArray(rubric.data_categories_shared) ? rubric.data_categories_shared : [];
    const share = typeof rubric.irrelevant_share === "number" ? rubric.irrelevant_share : null;
    if (cats.length || share != null) {
      const catChips = cats.map((c) => `<span class="audit-tool-chip">${esc(String(c).replace(/_/g, " "))}</span>`).join(" ");
      const shareColor = share == null ? "" : share >= 50 ? "var(--bad)" : share >= 20 ? "var(--warn, #e8a33d)" : "var(--ok, #4caf7d)";
      const shareBit = share != null
        ? `<div style="margin-top:6px"><b style="color:${shareColor}">${share}%</b> <span class="hint">of the payload judged irrelevant to the task</span></div>`
        : "";
      const items = Array.isArray(rubric.irrelevant_items) && rubric.irrelevant_items.length
        ? '<ul class="hint" style="margin:4px 0 0 16px;padding:0">'
          + rubric.irrelevant_items.map((i) => `<li>${esc(String(i))}</li>`).join("")
          + "</ul>"
        : "";
      relevance = `
        <div class="coach-notes">
          <div class="coach-section-label">Data shared &amp; relevance</div>
          ${catChips ? `<div style="margin-top:4px">${catChips}</div>` : ""}
          ${shareBit}
          ${items}
        </div>`;
    }
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
      ${relevance}
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

  function scScoreColor(s) {
    return s < 0.30 ? "var(--bad)" : s <= 0.55 ? "var(--warn)" : "var(--good)";
  }
  function shortModelName(m) {
    if (!m) return "—";
    return String(m).replace(/^(openrouter\/anthropic\/|openrouter\/openai\/|openrouter\/|anthropic\/|openai\/)/, "");
  }
  function scStatus(r) {
    if (r.is_demoted) return NG.chip("Demoted", "demoted");
    if (r.score > 0.55) return NG.chip("Healthy", "healthy");
    return NG.chip("Watch", "watch");
  }

  async function loadScorecard() {
    if (!getToken()) return;
    const kpis = document.getElementById("sc-kpis");
    const barsCard = document.getElementById("sc-bars-card");
    const tableCard = document.getElementById("sc-table-card");
    if (!tableCard) return;
    tableCard.innerHTML = '<div class="v2-card"><p class="hint">loading…</p></div>';
    try {
      const data = await api("/v1/scorecard");
      const items = data.items || [];
      if (!items.length) {
        if (kpis) kpis.innerHTML = "";
        if (barsCard) barsCard.innerHTML = "";
        tableCard.innerHTML = '<div class="v2-card"><p class="hint">No scorecard data yet — make a few requests via /v1/chat/completions and they\'ll show up here.</p></div>';
        return;
      }

      // KPIs — Models scored / Demoted (red) / Avg score / Incidents 24h
      const demoted = items.filter((r) => r.is_demoted).length;
      const avg = items.reduce((s, r) => s + (r.score || 0), 0) / items.length;
      const incidents24h = items.reduce((s, r) => s + ((r.recent_incidents || []).length), 0);
      if (kpis) NG.statRow(kpis, [
        NG.statCard({ label: "Models scored", value: items.length }),
        NG.statCard({ label: "Demoted", value: demoted, tone: demoted ? "bad" : null, sub: demoted ? "below 0.30 threshold" : "none demoted" }),
        NG.statCard({ label: "Avg score", value: avg.toFixed(2) }),
        NG.statCard({ label: "Incidents 24h", value: incidents24h }),
      ]);

      // Scores by model — vertical bars colored by health + 0.30 threshold line
      if (barsCard) {
        const bars = NG.verticalBars({
          max: 1,
          threshold: 0.30,
          thresholdLabel: "demotion threshold 0.30",
          height: 180,
          items: items.slice().sort((a, b) => b.score - a.score).map((r) => ({
            label: shortModelName(r.model), value: r.score, color: scScoreColor(r.score),
          })),
          fmt: (v) => v.toFixed(2),
        });
        barsCard.innerHTML = "";
        barsCard.appendChild(NG.card({ title: "Scores by model", body: bars }));
      }

      // Detail table — model·provider, tier, inline score bar, samples, waste, status
      NG.DataTable(tableCard, {
        title: "Scores", meta: "per provider · model · tier",
        countLabel: (n) => `${n} model${n === 1 ? "" : "s"}`,
        searchPlaceholder: "Filter…",
        defaultSort: { key: "score", dir: "desc" },
        rowClass: (r) => (r.is_demoted ? "v2-row-bad" : null),
        rows: items,
        onRowClick: (r) => { const inc = (r.recent_incidents || [])[0]; if (inc) openDecisionDetail(inc.decision_id); },
        columns: [
          { key: "model", label: "Model", render: (r) => { const w = NG.el("div", { class: "v2-cell-stack" }); w.appendChild(NG.el("span", { class: "v2-strong" }, r.model)); w.appendChild(NG.el("span", { class: "v2-cell-sub" }, r.provider || "")); return w; }, sortValue: (r) => r.model || "" },
          { key: "tier", label: "Tier", render: (r) => NG.tierPill(r.tier), sortValue: (r) => r.tier || "" },
          { key: "score", label: "Score", render: (r) => scScoreCell(r.score), sortValue: (r) => r.score },
          { key: "samples", label: "Samples", align: "right", render: (r) => (r.sample_size || 0).toLocaleString(), sortValue: (r) => r.sample_size || 0 },
          { key: "waste", label: "Waste", align: "right", render: (r) => (r.total_waste_usd > 0 ? usd(r.total_waste_usd) : "—"), sortValue: (r) => r.total_waste_usd || 0 },
          { key: "status", label: "Status", render: (r) => scStatus(r), sortable: false },
        ],
      });
    } catch (e) {
      tableCard.innerHTML = `<div class="v2-card"><p class="hint">load failed: ${esc(e.message || e)}</p></div>`;
    }
  }

  // inline score bar cell — bar then value (matches mock)
  function scScoreCell(score) {
    const wrap = NG.el("div", { class: "sc-score-cell" });
    const track = NG.el("div", { class: "sc-score-track" });
    track.appendChild(NG.el("div", { class: "sc-score-fill", style: { width: Math.max(2, Math.min(100, score * 100)) + "%", background: scScoreColor(score) } }));
    wrap.appendChild(track);
    wrap.appendChild(NG.el("span", { class: "sc-score-num", style: { color: scScoreColor(score) } }, score.toFixed(2)));
    return wrap;
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

  // === Improvements — prompt coaching + outcome simulation ===================

  let impDays = 30;
  document.querySelectorAll("#imp-window button").forEach((b) => {
    b.addEventListener("click", () => {
      impDays = parseInt(b.dataset.days, 10);
      document.querySelectorAll("#imp-window button").forEach((x) => x.classList.toggle("active", x === b));
      loadImprove();
    });
  });
  document.getElementById("imp-reload")?.addEventListener("click", loadImprove);

  async function loadImprove() {
    if (!getToken()) return;
    const summary = document.getElementById("imp-summary");
    const list = document.getElementById("imp-list");
    if (!summary || !list) return;
    if (!list.dataset.loaded) list.appendChild(NG.spinner("loading…"));
    try {
      const scope = getActiveAgentScope();
      const d = await api("/v1/improvements?days=" + impDays
        + (scope ? "&agent_id=" + encodeURIComponent(scope) : ""));
      summary.innerHTML = "";
      list.innerHTML = "";
      list.dataset.loaded = "1";
      renderImpSummary(summary, d);
      renderImpList(list, d);
    } catch (e) {
      list.innerHTML = `<p class="hint" style="color:var(--bad)">load failed: ${esc(e.message || e)}</p>`;
    }
  }

  function renderImpSummary(mount, d) {
    const trend = d.clarity_trend || [];
    const habits = d.habits || [];
    // Clarity trend — simple inline SVG line (weeks are few; uPlot is overkill).
    let trendSvg = "";
    if (trend.length >= 2) {
      const W = 420, H = 90, padX = 30, padY = 12;
      const X = (i) => padX + (i / (trend.length - 1)) * (W - padX * 2);
      const Y = (v) => padY + (1 - v / 5) * (H - padY * 2 - 14);
      const pts = trend.map((t, i) => `${X(i).toFixed(1)},${Y(t.clarity).toFixed(1)}`).join(" ");
      const dots = trend.map((t, i) => `<circle cx="${X(i).toFixed(1)}" cy="${Y(t.clarity).toFixed(1)}" r="3.5" fill="var(--accent)"><title>${esc(t.week)} · clarity ${t.clarity.toFixed(2)}/5 · ${t.n} evals</title></circle>`).join("");
      const labels = trend.map((t, i) => `<text x="${X(i).toFixed(1)}" y="${H - 2}" text-anchor="middle" class="in-axis">${esc(t.week.slice(5))}</text>`).join("");
      trendSvg = `<svg viewBox="0 0 ${W} ${H}" style="width:100%;max-width:460px">
        <text x="${padX - 6}" y="${Y(5) + 4}" text-anchor="end" class="in-axis">5</text>
        <text x="${padX - 6}" y="${Y(0) + 4}" text-anchor="end" class="in-axis">0</text>
        <line x1="${padX}" y1="${Y(0)}" x2="${W - padX}" y2="${Y(0)}" stroke="var(--border)"/>
        <polyline points="${pts}" fill="none" stroke="var(--accent)" stroke-width="2"/>${dots}${labels}</svg>`;
    }
    const habitRows = habits.map((h) => `
      <div class="imp-habit" title="${esc(h.example_fix || "")}">
        <span class="imp-habit-n">×${h.n}</span>
        <span>${esc(h.anti_pattern)}</span>
      </div>`).join("");
    mount.appendChild(NG.card({
      title: "Your writing habits",
      meta: `last ${d.days}d · patterns from calls where the judge rated the PROMPT (not the model) as the problem · clarity trend by week`,
      body: NG.el("div", {
        html: `<div class="imp-summary-grid">
          <div><div class="in-trial-col-head">Prompt clarity over time</div>${trendSvg || '<p class="hint">Not enough weeks yet.</p>'}</div>
          <div><div class="in-trial-col-head">Recurring patterns to unlearn</div>${habitRows || '<p class="hint">No recurring prompt problems found. Nice.</p>'}</div>
        </div>`,
      }),
    }));
  }

  function renderImpList(mount, d) {
    const prompts = d.prompts || [];
    if (!prompts.length) {
      mount.appendChild(NG.card({
        title: "Coachable prompts",
        body: NG.el("p", { class: "hint", style: { padding: "12px" } },
          "No judged prompts with a suggested rewrite in this window. Evals accumulate as you use the gateway (or 👎 a call in the Audit Log)."),
      }));
      return;
    }
    const clarityBadge = (v) => v == null ? "" :
      `<span class="imp-clarity imp-clarity-${v <= 2 ? "bad" : v <= 3 ? "warn" : "good"}" title="prompt clarity">${v}/5</span>`;
    const rows = prompts.map((p) => {
      const sim = p.sim_verdict
        ? `<span class="in-verdict in-verdict-${esc(p.sim_verdict === "challenger" ? "challenger" : p.sim_verdict)}" title="${esc(p.sim_reason || "")}">${p.sim_verdict === "challenger" ? "improved won" : p.sim_verdict === "champion" ? "original won" : esc(p.sim_verdict)}</span>`
        : `<button class="ghost imp-simulate" data-decision="${esc(p.decision_id)}">▶ simulate</button>`;
      return `
      <div class="imp-card" data-decision="${esc(p.decision_id)}">
        <div class="imp-card-head">
          ${clarityBadge(p.prompt_clarity)}
          <span class="imp-orig">${esc((p.prompt_excerpt || "(no excerpt captured)").slice(0, 160))}</span>
          <span class="hint" style="white-space:nowrap">${esc(shortModelName(p.decision_model || ""))} · ${tsShort(p.ts)}${p.cost_usd != null ? " · " + usd(p.cost_usd) : ""}</span>
        </div>
        ${p.anti_pattern ? `<div class="imp-anti">⚠ ${esc(p.anti_pattern)}</div>` : ""}
        ${p.coach_notes ? `<div class="hint imp-notes">${esc(p.coach_notes)}</div>` : ""}
        <div class="imp-suggest">
          <div class="in-trial-col-head">Suggested rewrite</div>
          <pre class="body-block imp-suggest-text">${esc(p.suggested_prompt || "")}</pre>
          <div class="imp-actions">
            <button class="ghost imp-copy" data-text="${esc(p.suggested_prompt || "")}">📋 copy</button>
            ${sim}
          </div>
          <div class="imp-sim-result" hidden></div>
        </div>
      </div>`;
    }).join("");
    mount.appendChild(NG.card({
      title: "Coachable prompts",
      meta: `${prompts.length} judged calls with a concrete rewrite · ▶ simulate runs the rewrite on the same model and blind-judges both answers`,
      body: NG.el("div", { html: rows }),
    }));
    mount.querySelectorAll(".imp-copy").forEach((b) => {
      b.addEventListener("click", async () => {
        try { await navigator.clipboard.writeText(b.dataset.text); } catch (_e) {}
        const t = b.textContent; b.textContent = "✓ copied";
        setTimeout(() => { b.textContent = t; }, 1500);
      });
    });
    mount.querySelectorAll(".imp-simulate").forEach((b) => {
      b.addEventListener("click", async () => {
        const did = b.dataset.decision;
        b.disabled = true;
        b.textContent = "⏳ running (~10s)…";
        const box = b.closest(".imp-suggest").querySelector(".imp-sim-result");
        try {
          const res = await fetch("/v1/improve/simulate/" + encodeURIComponent(did), {
            method: "POST",
            headers: { Authorization: "Bearer " + getToken() },
          });
          const r = await res.json();
          if (!res.ok || r.error) throw new Error(r.error || ("http_" + res.status));
          box.hidden = false;
          const won = r.verdict === "challenger";
          const priceBit = r.original_cost_usd != null && r.improved_cost_usd != null
            ? `<span class="hint">cost ${usd(r.original_cost_usd)} → <b style="color:${r.improved_cost_usd <= r.original_cost_usd ? "var(--good)" : "var(--warn)"}">${usd(r.improved_cost_usd)}</b></span>` : "";
          box.innerHTML = `
            <div class="imp-sim-head">
              <span class="in-verdict in-verdict-${won ? "challenger" : r.verdict === "tie" ? "tie" : "champion"}">${won ? "✓ improved prompt won" : r.verdict === "tie" ? "tie — equally good" : "original won"}</span>
              <span class="hint">${esc(r.judge_reason || "")}</span> ${priceBit}
            </div>
            <div class="imp-sim-cols">
              <div><div class="in-trial-col-head">Original answer</div><pre class="body-block in-trial-body">${esc((r.original_response || "").slice(0, 1500))}</pre></div>
              <div class="${won ? "in-trial-won" : ""}"><div class="in-trial-col-head">With improved prompt${won ? " 🏆" : ""}</div><pre class="body-block in-trial-body">${esc((r.improved_response || "").slice(0, 1500))}</pre></div>
            </div>`;
          b.remove();
        } catch (e) {
          b.disabled = false;
          b.textContent = "▶ simulate";
          box.hidden = false;
          box.innerHTML = `<p class="hint" style="color:var(--bad)">simulation failed: ${esc(e.message || e)}</p>`;
        }
      });
    });
  }

  // === Experiments page — the shadow panel, promoted to a destination ========

  async function loadExperiments() {
    if (!getToken()) return;
    const mount = document.getElementById("exp-panel");
    if (!mount) return;
    if (!mount.dataset.loaded) mount.appendChild(NG.spinner("loading…"));
    try {
      const data = await api("/v1/shadow?days=30");
      mount.innerHTML = "";
      mount.dataset.loaded = "1";
      renderInShadow(mount, data);
      const live = (data.experiments || []).filter((e) => e.n > 0).length;
      const badge = document.getElementById("nav-exp-badge");
      if (badge) { badge.textContent = String(live); badge.hidden = !live; }
    } catch (e) {
      mount.innerHTML = `<p class="hint" style="color:var(--bad)">load failed: ${esc(e.message || e)}</p>`;
    }
  }

  // === Model Health — trust · drift · behavior · probes, one hub =============

  async function loadModelHealth() {
    if (!getToken()) return;
    const cards = document.getElementById("mh-cards");
    const spc = document.getElementById("mh-spc");
    const signals = document.getElementById("mh-signals");
    if (!cards) return;
    if (!cards.dataset.loaded) cards.appendChild(NG.spinner("loading…"));
    const [sc, spcData, drift, subst] = await Promise.all([
      api("/v1/scorecard").catch(() => null),
      api("/v1/insights/spc?hours=168&metric=" + insightsSpcMetric).catch(() => null),
      api("/v1/drift").catch(() => null),
      api("/v1/insights/substitution").catch(() => null),
    ]);
    cards.dataset.loaded = "1";
    // --- model cards: driven by REAL traffic (SPC top models), enriched with
    // the brain-layer trust score when one exists. Scorecard-first was wrong:
    // it only scores routed tiers, so OAuth passthrough (most Claude traffic)
    // barely registers there.
    cards.innerHTML = "";
    const scByModel = {};
    ((sc || {}).items || []).forEach((r) => {
      const key = shortModelName(r.model);
      const prev = scByModel[key];
      if (!prev || (r.sample_size || 0) > (prev.sample_size || 0)) scByModel[key] = r;
    });
    const models = ((spcData || {}).models || []).slice(0, 4);
    if (models.length) {
      cards.innerHTML = '<div class="mh-cards">' + models.map((m) => {
        const name = shortModelName(m.model);
        const r = scByModel[name];
        const viol = m.violations.length;
        const status = r && r.is_demoted && (r.sample_size || 0) >= 20
          ? '<span class="mh-status" style="color:var(--bad)">● DEMOTED</span>'
          : viol
          ? `<span class="mh-status" style="color:var(--warn)">● ${viol} SHIFT${viol > 1 ? "S" : ""}</span>`
          : '<span class="mh-status" style="color:var(--good)">● IN CONTROL</span>';
        const score = r
          ? `<span class="mh-score-num" style="color:${scScoreColor(r.score)}">${(r.score ?? 0).toFixed(2)}</span><span class="hint">trust score · n=${r.sample_size}</span>`
          : `<span class="mh-score-num" style="color:var(--text)">${m.calls.toLocaleString()}</span><span class="hint">calls 7d · unscored (passthrough)</span>`;
        const meta = [
          r ? `${m.calls.toLocaleString()} calls 7d` : null,
          m.mean != null ? `μ ${fmtNum(m.mean)}` : null,
          m.sd != null ? `σ ${fmtNum(m.sd)}` : null,
          viol ? "behavior shifted" : "drift clear 7d",
        ].filter(Boolean).join(" · ");
        return `
          <div class="mh-card">
            <div class="mh-card-head"><span class="mh-name">${esc(name)}</span>${status}</div>
            <div class="mh-score">${score}</div>
            <div class="hint">${esc(meta)}</div>
          </div>`;
      }).join("") + "</div>";
    } else {
      cards.innerHTML = '<p class="hint">Not enough hourly traffic yet to profile models.</p>';
    }
    // --- control chart (shared renderer with Insights) ----------------------
    if (spc) {
      spc.innerHTML = "";
      if (spcData) renderInSpc(spc, spcData);
    }
    // --- open signals: drift alerts + significant substitutions -------------
    if (signals) {
      const rows = [];
      ((drift || {}).alerts || []).slice(0, 4).forEach((a) => {
        rows.push({
          chip: "DRIFT", color: "var(--bad)",
          text: `${shortModelName(a.model || "?")} · ${a.metric_name || "metric"} ${a.direction || ""} · peak z=${a.peak_z_score != null ? Number(a.peak_z_score).toFixed(1) : "—"}`,
          action: "investigate →", tab: "drift",
        });
      });
      ((subst || {}).pairs || []).filter((p) => p.p_value != null && p.p_value < 0.05 && p.delta < 0)
        .slice(0, 3).forEach((p) => {
          rows.push({
            chip: "SUBST", color: "var(--warn)",
            text: `${shortModelName(p.asked)} → ${shortModelName(p.served)} scores ${p.delta} on task completion · n=${p.n_substituted} · p=${p.p_value < 0.001 ? "<0.001" : p.p_value}`,
            action: "see pairs →", tab: "insights",
          });
        });
      signals.innerHTML = "";
      signals.appendChild(NG.card({
        title: "Open signals",
        meta: "drift alerts & substitution evidence that want a look · detailed views: Drift · Scorecard · Behavior · Probing live on under the hood",
        body: NG.el("div", {
          html: rows.length
            ? rows.map((r) => `
              <div class="mh-signal" data-tab-link="${esc(r.tab)}">
                <span class="mh-chip" style="color:${r.color};border-color:${r.color}">${r.chip}</span>
                <span class="mh-signal-text">${esc(r.text)}</span>
                <span class="mh-signal-action">${esc(r.action)}</span>
              </div>`).join("")
            : '<p class="hint" style="padding:10px 14px">All clear — no drift alerts and no significant substitution deltas in the window.</p>',
        }),
      }));
      signals.querySelectorAll(".mh-signal").forEach((el) => {
        el.addEventListener("click", () => activateTab(el.dataset.tabLink));
      });
    }
  }

  // === Overview intelligence strip ===========================================

  async function loadIntelStrip() {
    const mount = document.getElementById("overview-intel");
    if (!mount || !getToken()) return;
    try {
      const d = await api("/v1/insights/headline");
      const tile = (label, valueHtml, sub, tab) => `
        <div class="intel-tile" data-tab-link="${tab}">
          <div class="intel-label">${label}</div>
          <div class="intel-value">${valueHtml}</div>
          <div class="hint">${sub}</div>
        </div>`;
      const eff = d.efficiency || {};
      const sav = d.savings;
      const exp = d.experiments || {};
      const gov = d.governance || {};
      const effColor = eff.score == null ? "var(--text-dim)" : eff.score >= 70 ? "var(--good)" : eff.score >= 40 ? "var(--warn)" : "var(--bad)";
      mount.innerHTML = `
        <div class="intel-head"><span>Intelligence</span><span class="hint">what the gateway learned from your traffic · last 7d</span></div>
        <div class="intel-cards">
          ${tile("Efficiency index",
            `<span style="color:${effColor}">${eff.score ?? "—"}</span><span class="intel-unit">/100</span>`,
            `across ${eff.agents || 0} agents`, "insights")}
          ${tile("Savings identified",
            sav ? `<span style="color:var(--good)">$${Number(sav.weekly_usd).toLocaleString()}</span><span class="intel-unit">/wk</span>` : '<span style="color:var(--text-dim)">—</span>',
            sav ? `counterfactual: all-${esc(sav.policy)}` : "no counterfactual yet", "insights")}
          ${tile("Experiments",
            `${exp.count || 0} ${exp.running ? '<span class="intel-live">LIVE</span>' : ""}`,
            exp.proven ? `${exp.proven} proven non-inferior` : "gathering evidence", "experiments")}
          ${tile("Data shipped",
            gov.secrets ? `<span style="color:var(--bad)">${gov.secrets} secrets</span>` : '0 <span class="intel-unit" style="color:var(--good)">secrets</span>',
            `${gov.pii || 0} PII calls · bodies policy-gated`, "privacy")}
        </div>`;
      mount.querySelectorAll(".intel-tile").forEach((el) => {
        el.addEventListener("click", () => activateTab(el.dataset.tabLink));
      });
      const badge = document.getElementById("nav-exp-badge");
      if (badge) { badge.textContent = String(exp.count || 0); badge.hidden = !exp.count; }
    } catch (_e) { /* strip is progressive enhancement — overview works without it */ }
  }

  // === Bench — same task, N models, side by side ==============================

  // Preferred default picks, in slot order. Only entries the gateway reports as
  // routable are preselected — the full option list comes from the server, so
  // this list going stale can no longer make a model unpickable.
  const BENCH_PRESETS = [
    "anthropic/claude-fable-5", "openai/gpt-5.6-sol",
    "anthropic/claude-opus-4-8", "openrouter/moonshotai/kimi-k2.6",
  ];

  async function loadBench() {
    if (!getToken()) return;
    renderBenchForm();
    const workingEl = document.getElementById("bn-working");
    const histEl = document.getElementById("bn-history");
    try {
      const scope = getActiveAgentScope();
      const qs = "hours=24" + (scope ? "&agent_id=" + encodeURIComponent(scope) : "");
      const d = await api("/v1/bench?" + qs);
      fillBenchModels(d.available_models);
      renderBenchWorking(workingEl, d.working);
      renderBenchHistory(histEl, d.runs || []);
      // Head-to-head is a separate query so a slow/empty pairing never blocks
      // the rest of the page.
      api("/v1/bench/head-to-head?" + qs)
        .then((h) => renderBenchH2H(document.getElementById("bn-h2h"), h))
        .catch(() => {});
    } catch (e) {
      if (workingEl) workingEl.innerHTML = `<p class="hint" style="color:var(--bad)">load failed: ${esc(e.message || e)}</p>`;
    }
  }

  function renderBenchForm() {
    const mount = document.getElementById("bn-form");
    if (!mount || mount.dataset.built) return;
    mount.dataset.built = "1";
    // Real <select>s, not a datalist on a text input — the datalist gave no
    // visible affordance, so the models looked unpickable. Options are filled
    // by fillBenchModels() once /v1/bench reports what's actually routable.
    const modelInputs = [0, 1, 2, 3].map((i) =>
      `<select class="bn-model" data-slot="${i}" aria-label="model ${i + 1}">
         <option value="">— model ${i + 1}${i > 1 ? " (optional)" : ""} —</option>
       </select>`).join("");
    mount.innerHTML = `
      <div class="v2-card" style="padding:16px">
        <textarea id="bn-prompt" class="bn-prompt" rows="3" placeholder="The task — every model gets exactly this prompt…">A user's payment webhook fails intermittently with 502 errors. What is your first debugging step? If tools are available, use one.</textarea>
        <div class="bn-models-row">${modelInputs}</div>
        <div class="bn-actions">
          <label class="bn-tools-toggle"><input type="checkbox" id="bn-tools" checked /> give models a sample tool set (read_file, search_code, run_command) — calls are captured, never executed</label>
          <span style="flex:1"></span>
          <button id="bn-run" class="exp-promote">▶ Run bench</button>
        </div>
      </div>`;
    document.getElementById("bn-run").addEventListener("click", runBench);
  }

  // Populate the model dropdowns from what the gateway can actually route.
  // Preselects the first BENCH_PRESETS entries that survived, so the form is
  // runnable without picking anything.
  function fillBenchModels(models) {
    const list = (models && models.length ? models : BENCH_PRESETS).slice();
    const defaults = BENCH_PRESETS.filter((m) => list.includes(m));
    document.querySelectorAll("select.bn-model").forEach((sel) => {
      if (sel.dataset.filled) return;
      sel.dataset.filled = "1";
      const slot = Number(sel.dataset.slot);
      const keep = sel.options[0].outerHTML;
      sel.innerHTML = keep + list.map((m) =>
        `<option value="${esc(m)}">${esc(shortModelName(m))}</option>`).join("");
      const want = defaults[slot];
      if (want) sel.value = want;
    });
  }

  async function runBench() {
    const btn = document.getElementById("bn-run");
    const results = document.getElementById("bn-results");
    const prompt = document.getElementById("bn-prompt").value.trim();
    const models = [...document.querySelectorAll(".bn-model")].map((i) => i.value.trim()).filter(Boolean);
    if (!prompt || !models.length) return;
    btn.disabled = true;
    const prev = btn.textContent;
    btn.textContent = "⏳ running " + models.length + " models…";
    results.innerHTML = "";
    results.appendChild(NG.spinner("all models running in parallel…"));
    try {
      const res = await fetch("/v1/bench/run", {
        method: "POST",
        headers: { Authorization: "Bearer " + getToken(), "Content-Type": "application/json" },
        body: JSON.stringify({
          prompt, models,
          tools: document.getElementById("bn-tools").checked ? "sample" : null,
        }),
      });
      if (!res.ok) throw new Error("http_" + res.status + ": " + (await res.text()).slice(0, 160));
      const run = await res.json();
      results.innerHTML = "";
      renderBenchRun(results, run);
      loadBench();  // refresh history
    } catch (e) {
      results.innerHTML = `<p class="hint" style="color:var(--bad)">bench failed: ${esc(e.message || e)}</p>`;
    } finally {
      btn.disabled = false;
      btn.textContent = prev;
    }
  }

  function renderBenchRun(mount, run) {
    const rs = run.results || [];
    const minCost = Math.min(...rs.filter((r) => r.cost_usd != null).map((r) => r.cost_usd), Infinity);
    const minLat = Math.min(...rs.filter((r) => r.latency_ms != null).map((r) => r.latency_ms), Infinity);
    const cards = rs.map((r) => {
      const ok = r.status && r.status >= 200 && r.status < 300;
      const tools = (r.tool_calls || []).map((t) =>
        `<span class="audit-tool-chip" title="${esc(t.arguments || "")}">${esc(t.name)}</span>`).join(" ");
      const stat = (label, v, best) =>
        `<div class="bn-stat"><span class="bn-stat-label">${label}</span><span class="bn-stat-val"${best ? ' style="color:var(--good)"' : ""}>${v}</span></div>`;
      return `
        <div class="bn-card${ok ? "" : " bn-card-err"}">
          <div class="bn-card-head"><b>${esc(shortModelName(r.model))}</b>
            ${r.via_fallback ? '<span class="hint">via openrouter</span>' : ""}
            <span class="mh-status" style="color:${ok ? "var(--good)" : "var(--bad)"}">${ok ? "● " + r.status : "✗ " + (r.status || "ERR")}</span></div>
          <div class="bn-stats">
            ${stat("latency", r.latency_ms != null ? r.latency_ms + "ms" : "—", r.latency_ms === minLat)}
            ${stat("in / out", (r.prompt_tokens ?? "—") + " / " + (r.completion_tokens ?? "—"))}
            ${stat("cost", r.cost_usd != null ? "$" + r.cost_usd.toFixed(5) : "—", r.cost_usd === minCost)}
            ${stat("tool calls", String((r.tool_calls || []).length))}
          </div>
          ${tools ? `<div class="bn-tools">${tools}</div>` : ""}
          ${r.error ? `<div class="hint" style="color:var(--bad)">${esc(r.error)}</div>` : ""}
          <details class="bn-response"><summary>response ${r.text ? "· " + r.text.length + " chars" : ""}</summary>
            <pre class="body-block in-trial-body">${esc(r.text || "(no text — tool call only)")}</pre></details>
        </div>`;
    }).join("");
    mount.innerHTML = `<div class="section-title">Results — ${esc(tsShort(run.ts || new Date().toISOString()))}</div>
      <div class="bn-grid">${cards}</div>`;
  }

  // Same task, N models, real traffic — paired so the comparison is
  // like-for-like. `working` above averages a model across ALL its traffic,
  // which mixes a 57-tool builder session with a 5-tool opinion call and makes
  // tool-schema overhead look like model inefficiency.
  function renderBenchH2H(mount, h) {
    if (!mount) return;
    mount.innerHTML = "";
    const tasks = (h || {}).tasks || [];
    if (!tasks.length) {
      mount.appendChild(NG.card({
        title: "Head-to-head — same task, real calls",
        meta: `last ${(h || {}).hours || 24}h · no paired tasks yet`,
        body: NG.el("div", { html: '<p class="hint" style="padding:10px 14px">Nothing to compare yet — this fills in when two models answer the same prompt (e.g. a Fusion <code>/opinion</code> run).</p>' }),
      }));
      return;
    }
    const num = (v, f) => (v == null ? "—" : f(v));
    const blocks = tasks.map((t) => {
      // Best (lowest) value per metric wins the highlight; nulls never win.
      const best = (key, lower = true) => {
        const vals = t.models.map((m) => m[key]).filter((v) => v != null);
        if (!vals.length) return null;
        return lower ? Math.min(...vals) : Math.max(...vals);
      };
      const bIO = best("in_per_out"), bDur = best("duration_ms"), bTtfb = best("ttfb_ms");
      const cols = t.models.map((m) => {
        const win = (v, b) => (v != null && b != null && v === b ? ' class="h2h-win"' : "");
        const cost = m.unpriced
          ? '<span class="h2h-unpriced" title="No pricing.yaml entry for this model — cost is unknown, not zero">unpriced</span>'
          : num(m.cost_usd, (v) => "$" + v.toFixed(4));
        const tools = m.tool_names.length
          ? `<div class="h2h-tools">${m.tool_names.slice(0, 8).map((n) => `<code>${esc(n)}</code>`).join(" ")}${m.tool_names.length > 8 ? " +" + (m.tool_names.length - 8) : ""}</div>`
          : "";
        return `<div class="h2h-col">
          <div class="h2h-model">${esc(shortModelName(m.model))}</div>
          <div class="h2h-sub">${m.calls} call${m.calls === 1 ? "" : "s"}${m.tools_offered ? " · " + Math.round(m.tools_offered) + " tools offered" : ""}</div>
          <dl class="h2h-metrics">
            <dt>context in : out</dt><dd${win(m.in_per_out, bIO)}>${num(m.in_per_out, (v) => v + " : 1")}</dd>
            <dt>tokens</dt><dd>${(m.tokens_in || 0).toLocaleString()} in / ${(m.tokens_out || 0).toLocaleString()} out${m.tokens_reasoning ? ` <span class="h2h-sub">(${m.tokens_reasoning.toLocaleString()} reasoning)</span>` : ""}</dd>
            <dt>cost</dt><dd>${cost}</dd>
            <dt>first byte</dt><dd${win(m.ttfb_ms, bTtfb)}>${num(m.ttfb_ms, (v) => Math.round(v) + "ms")}</dd>
            <dt>total time</dt><dd${win(m.duration_ms, bDur)}>${num(m.duration_ms, (v) => (v / 1000).toFixed(1) + "s")}</dd>
            <dt>tool calls</dt><dd>${m.tool_calls}</dd>
          </dl>${tools}
        </div>`;
      }).join("");
      return `<div class="h2h-task">
        <div class="h2h-task-head"><span class="h2h-when">${t.first_ts ? esc(new Date(t.first_ts).toLocaleTimeString()) : ""}</span>
          <span class="h2h-excerpt">${esc(t.excerpt || "").slice(0, 140)}</span></div>
        <div class="h2h-cols">${cols}</div>
      </div>`;
    }).join("");
    mount.appendChild(NG.card({
      title: "Head-to-head — same task, real calls",
      meta: `last ${(h || {}).hours || 24}h · ${tasks.length} paired task${tasks.length === 1 ? "" : "s"} · lower context ratio and faster time are highlighted · unpriced models show as unknown, never $0`,
      body: NG.el("div", { html: `<div class="h2h-wrap">${blocks}</div>` }),
    }));
  }

  function renderBenchWorking(mount, w) {
    if (!mount) return;
    mount.innerHTML = "";
    const models = (w || {}).models || [];
    const rows = models.map((m) => `<tr>
        <td><b>${esc(shortModelName(m.model))}</b></td>
        <td class="num">${m.calls}</td>
        <td class="num">${m.tool_calls_per_turn != null ? m.tool_calls_per_turn.toFixed(2) : "—"}</td>
        <td class="num">${m.fresh_in_per_call != null ? Math.round(m.fresh_in_per_call).toLocaleString() : "—"}</td>
        <td class="num">${m.out_per_call != null ? Math.round(m.out_per_call).toLocaleString() : "—"}</td>
        <td class="num">${m.thinking_share != null ? Math.round(m.thinking_share * 100) + "%" : "—"}</td>
        <td class="num">${m.cost_per_call != null ? "$" + m.cost_per_call.toFixed(4) : "—"}</td>
        <td class="num">${m.p50_ms != null ? Math.round(m.p50_ms) + "ms" : "—"}</td>
      </tr>`).join("");
    mount.appendChild(NG.card({
      title: "While you work — real traffic compared",
      meta: `last ${(w || {}).hours || 24}h · ${(w || {}).agent_id ? "session " + esc(w.agent_id) : "all sessions"} · switch models mid-work and the difference shows up here, no synthetic calls`,
      body: NG.el("div", {
        html: rows
          ? `<table class="in-table"><thead><tr><th>model</th><th class="num">calls</th><th class="num">tools/turn</th><th class="num">fresh in/call</th><th class="num">out/call</th><th class="num">thinking</th><th class="num">cost/call</th><th class="num">p50</th></tr></thead><tbody>${rows}</tbody></table>`
          : '<p class="hint" style="padding:10px 14px">No real traffic in the window yet.</p>',
      }),
    }));
  }

  function renderBenchHistory(mount, runs) {
    if (!mount) return;
    mount.innerHTML = "";
    if (!runs.length) return;
    const rows = runs.map((r, i) => `
      <div class="in-trial-row" data-run="${i}">
        <span style="width:96px;flex-shrink:0;font-family:var(--mono);font-size:11px;color:var(--text-dim)">${esc(tsShort(r.ts))}</span>
        <span class="in-trial-prompt">${esc((r.prompt || "").slice(0, 110))}</span>
        <span class="hint" style="white-space:nowrap">${(r.results || []).map((x) => esc(shortModelName(x.model))).join(" · ")}</span>
      </div>`).join("");
    mount.appendChild(NG.card({
      title: "Previous runs",
      body: NG.el("div", { html: rows }),
    }));
    mount.querySelectorAll(".in-trial-row").forEach((el) => {
      el.addEventListener("click", () => {
        const run = runs[parseInt(el.dataset.run, 10)];
        const results = document.getElementById("bn-results");
        results.innerHTML = "";
        renderBenchRun(results, run);
        results.scrollIntoView({ behavior: "smooth" });
      });
    });
  }

  // === Tooling — MCP carrying cost & discovery savings ========================

  async function loadTooling() {
    if (!getToken()) return;
    const serversEl = document.getElementById("tl-servers");
    const trendEl = document.getElementById("tl-trend");
    if (!serversEl) return;
    if (!serversEl.dataset.loaded) serversEl.appendChild(NG.spinner("loading…"));
    try {
      const scope = getActiveAgentScope();
      // Two lenses: everything through the gateway, and just the active session.
      const [all, scoped] = await Promise.all([
        api("/v1/insights/tooling?days=30"),
        scope ? api("/v1/insights/tooling?days=30&agent_id=" + encodeURIComponent(scope)) : Promise.resolve(null),
      ]);
      serversEl.dataset.loaded = "1";
      renderToolingServers(serversEl, all);
      trendEl.innerHTML = "";
      if (scoped) {
        const h1 = NG.el("div", { class: "section-title" }, "Active session · " + scope);
        trendEl.appendChild(h1);
        renderToolingTrend(trendEl, scoped, { append: true, noHint: true });
      }
      trendEl.appendChild(NG.el("div", { class: "section-title", style: { marginTop: scoped ? "18px" : "0" } }, "All sessions"));
      renderToolingTrend(trendEl, all, { append: true });
    } catch (e) {
      serversEl.innerHTML = `<p class="hint" style="color:var(--bad)">load failed: ${esc(e.message || e)}</p>`;
    }
  }

  function renderToolingServers(mount, d) {
    const rows = (d.servers || []).map((s) => {
      const chips = (s.tools_used || []).slice(0, 5).map((t) =>
        `<span class="audit-tool-chip" title="${t.n} calls">${esc(t.tool.replace(/^mcp__[^_]*(?:_[^_]+)*__/, "").replace(/^mcp__.*?__/, ""))} ×${t.n}</span>`).join(" ");
      const schema = s.schema_tokens != null
        ? `${s.schema_tokens.toLocaleString()} tok <span class="hint">· ${s.tool_count} tools · carried by ${s.agents_carrying} agent${s.agents_carrying > 1 ? "s" : ""}</span>`
        : '<span class="hint">schema not yet captured</span>';
      return `<tr>
        <td><b>${esc(s.server)}</b></td>
        <td class="num">${s.invocations.toLocaleString()}</td>
        <td>${schema}</td>
        <td>${chips || '<span class="hint">—</span>'}</td>
      </tr>`;
    }).join("");
    mount.innerHTML = "";
    mount.appendChild(NG.card({
      title: "Connected tool servers",
      meta: `last ${d.days}d · ${d.agent_id ? "scoped to session " + esc(d.agent_id) : "all sessions"} · schema tokens = payload every carrying request ships (mostly cache-read priced while stable; re-written at the 25% premium whenever a schema changes)`,
      body: NG.el("div", {
        html: `<table class="in-table">
          <thead><tr><th>server</th><th class="num">invocations</th><th>carrying cost / request</th><th>top tools</th></tr></thead>
          <tbody>${rows}</tbody></table>`,
      }),
    }));
  }

  function renderToolingTrend(mount, d, opts) {
    if (!mount) return;
    if (!(opts && opts.append)) mount.innerHTML = "";
    const daily = (d.daily || []).filter((x) => x.calls > 0);
    if (daily.length < 2) {
      mount.appendChild(NG.card({ title: "Discovery mix", body: NG.el("p", { class: "hint", style: { padding: "12px" } }, "Not enough daily traffic yet.") }));
      return;
    }
    const x = daily.map((r) => Math.floor(new Date(r.day + "T12:00:00Z").getTime() / 1000));
    const wrap = NG.el("div", { class: "v2-grid-2" });
    const mixCard = NG.el("div", { class: "v2-card" });
    mixCard.appendChild(NG.el("div", { class: "v2-card-head" }, [
      NG.el("span", { class: "v2-card-title" }, "Discovery mix — filesystem crawling vs MCP answers"),
    ]));
    const mixMount = NG.el("div", { style: { padding: "0 8px 8px" } });
    mixCard.appendChild(mixMount);
    const tokCard = NG.el("div", { class: "v2-card" });
    tokCard.appendChild(NG.el("div", { class: "v2-card-head" }, [
      NG.el("span", { class: "v2-card-title" }, "Fresh input tokens per call (cache-excluded)"),
    ]));
    const tokMount = NG.el("div", { style: { padding: "0 8px 8px" } });
    tokCard.appendChild(tokMount);
    wrap.appendChild(mixCard);
    wrap.appendChild(tokCard);
    mount.appendChild(wrap);
    NG.chart(mixMount, {
      type: "line", x, height: 200,
      series: [
        { label: "Read/Grep/Glob calls", values: daily.map((r) => r.fs_calls), color: "#8893A4" },
        { label: "MCP calls", values: daily.map((r) => r.mcp_calls), color: "#7C9BFF" },
      ],
      fmtY: (v) => fmtNum(v),
      fmtX: (e) => new Date(e * 1000).toLocaleDateString([], { month: "short", day: "numeric" }),
    });
    NG.chart(tokMount, {
      type: "area", x, height: 200,
      series: [{ label: "avg fresh input tok/call", values: daily.map((r) => Math.round(r.avg_prompt_tokens || 0)), color: "#4C8DFF" }],
      fmtY: (v) => fmtNum(v),
      fmtX: (e) => new Date(e * 1000).toLocaleDateString([], { month: "short", day: "numeric" }),
    });
    if (!(opts && opts.noHint)) {
      mount.appendChild(NG.el("p", { class: "hint", style: { marginTop: "8px" } },
        "The claim to watch: as MCP calls (amber) replace filesystem crawling (grey), fresh input tokens per call should trend down — one graph answer instead of five file dumps in context."));
    }
  }

  // === Reports — print-ready usage & governance audits =======================

  let reportDays = 30;
  document.querySelectorAll("#report-window button").forEach((b) => {
    b.addEventListener("click", () => {
      reportDays = parseInt(b.dataset.days, 10);
      document.querySelectorAll("#report-window button").forEach((x) => x.classList.toggle("active", x === b));
    });
  });
  document.getElementById("report-generate")?.addEventListener("click", async () => {
    const btn = document.getElementById("report-generate");
    const status = document.getElementById("report-status");
    btn.disabled = true;
    const prev = btn.textContent;
    btn.textContent = "aggregating…";
    try {
      const data = await api("/v1/reports/audit?days=" + reportDays);
      _showAuditReportModal(data);
      status.innerHTML = "";
    } catch (e) {
      status.innerHTML = `<p class="hint" style="color:var(--bad)">report failed: ${esc(e.message || e)}</p>`;
    } finally {
      btn.disabled = false;
      btn.textContent = prev;
    }
  });

  function _buildAuditReportHtml(d) {
    const t = d.totals || {};
    const fUsd = (v) => "$" + Number(v || 0).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    const fNum = (v) => Number(v || 0).toLocaleString("en-US");
    const pct = (a, b) => (b ? Math.round((a / b) * 100) : 0);
    const genAt = new Date().toISOString().slice(0, 16).replace("T", " ");
    const period = `last ${d.days} days`;
    const okPct = pct(t.ok, t.calls);
    const sens = d.sensitivity || {};
    const sensTotal = Object.values(sens).reduce((a, b) => a + b, 0) || 1;
    const subsCovered = Math.max(0, (t.notional_usd || 0) - (t.metered_usd || 0));

    // --- inline SVG charts (no libs — the report is a standalone document) --
    const PAL = ["#7C9BFF", "#3B6FD4", "#1a7f37", "#8E63CE", "#b35b1e", "#4C8DBB", "#946200", "#767E8B"];
    // Daily grouped bars: calls (series-1, left axis) + spend line (blue, right axis).
    const dailyChart = (daily) => {
      if (!daily || daily.length < 2) return "";
      const W = 720, H = 170, padL = 46, padR = 52, padT = 12, padB = 26;
      const iw = W - padL - padR;
      const maxCalls = Math.max(...daily.map((x) => x.calls), 1);
      const maxSpend = Math.max(...daily.map((x) => x.spend_usd), 0.01);
      const bw = Math.min(26, (iw / daily.length) * 0.66);
      const X = (i) => padL + (i + 0.5) * (iw / daily.length);
      const Yc = (v) => padT + (1 - v / maxCalls) * (H - padT - padB);
      const Ys = (v) => padT + (1 - v / maxSpend) * (H - padT - padB);
      const bars = daily.map((x, i) =>
        `<rect x="${(X(i) - bw / 2).toFixed(1)}" y="${Yc(x.calls).toFixed(1)}" width="${bw.toFixed(1)}" height="${(H - padB - Yc(x.calls)).toFixed(1)}" fill="#F0B285"/>
         ${x.errors ? `<rect x="${(X(i) - bw / 2).toFixed(1)}" y="${(H - padB - Math.max(2, (x.errors / maxCalls) * (H - padT - padB))).toFixed(1)}" width="${bw.toFixed(1)}" height="${Math.max(2, (x.errors / maxCalls) * (H - padT - padB)).toFixed(1)}" fill="#c62828"/>` : ""}`).join("");
      const spendLine = daily.map((x, i) => `${X(i).toFixed(1)},${Ys(x.spend_usd).toFixed(1)}`).join(" ");
      const labStep = Math.ceil(daily.length / 8);
      const xLabels = daily.map((x, i) => i % labStep ? "" :
        `<text x="${X(i).toFixed(1)}" y="${H - 8}" text-anchor="middle" class="ax">${x.day.slice(5)}</text>`).join("");
      return `<svg viewBox="0 0 ${W} ${H}" style="width:100%">
        <line x1="${padL}" y1="${H - padB}" x2="${W - padR}" y2="${H - padB}" stroke="#d7dbe0"/>
        <text x="${padL - 6}" y="${padT + 10}" text-anchor="end" class="ax">${fNum(maxCalls)}</text>
        <text x="${padL - 6}" y="${H - padB}" text-anchor="end" class="ax">0</text>
        <text x="${W - padR + 6}" y="${padT + 10}" class="ax" fill="#3B6FD4">${fUsd(maxSpend)}</text>
        ${bars}
        <polyline points="${spendLine}" fill="none" stroke="#3B6FD4" stroke-width="2"/>
        ${xLabels}
        <text x="${padL}" y="${padT}" class="ax">calls/day (bars, red = errors) · spend/day (line)</text>
      </svg>`;
    };
    // Horizontal bars with labels; values pre-sorted desc.
    const hbars = (items, fmtV) => {
      if (!items.length) return "";
      const W = 720, rowH = 22, labW = 210, valW = 84;
      const H = items.length * rowH + 4;
      const maxV = Math.max(...items.map((x) => x.value), 1e-9);
      const rows = items.map((x, i) => {
        const y = i * rowH + 3;
        const w = Math.max(2, (x.value / maxV) * (W - labW - valW - 12));
        return `<text x="${labW - 8}" y="${y + 13}" text-anchor="end" class="ax">${esc(String(x.label).slice(0, 34))}</text>
          <rect x="${labW}" y="${y}" width="${w.toFixed(1)}" height="${rowH - 7}" rx="2" fill="${x.color || PAL[i % PAL.length]}"/>
          <text x="${labW + w + 6}" y="${y + 13}" class="ax">${fmtV(x.value)}</text>`;
      }).join("");
      return `<svg viewBox="0 0 ${W} ${H}" style="width:100%">${rows}</svg>`;
    };
    // Donut with legend to the right.
    const donut = (slices) => {
      const total = slices.reduce((a, s) => a + s.value, 0);
      if (!total) return "";
      const R = 54, r = 32, cx = 70, cy = 70;
      let a0 = -Math.PI / 2;
      const paths = slices.filter((s) => s.value > 0).map((s) => {
        const frac = s.value / total;
        const a1 = a0 + frac * 2 * Math.PI;
        const large = frac > 0.5 ? 1 : 0;
        const p = `M ${cx + R * Math.cos(a0)} ${cy + R * Math.sin(a0)}
          A ${R} ${R} 0 ${large} 1 ${cx + R * Math.cos(a1 - 0.001)} ${cy + R * Math.sin(a1 - 0.001)}
          L ${cx + r * Math.cos(a1 - 0.001)} ${cy + r * Math.sin(a1 - 0.001)}
          A ${r} ${r} 0 ${large} 0 ${cx + r * Math.cos(a0)} ${cy + r * Math.sin(a0)} Z`;
        a0 = a1;
        return `<path d="${p}" fill="${s.color}"/>`;
      }).join("");
      const legend = slices.map((s, i) =>
        `<rect x="160" y="${28 + i * 22}" width="10" height="10" rx="2" fill="${s.color}"/>
         <text x="176" y="${37 + i * 22}" class="ax">${esc(s.label)} — ${fNum(s.value)} (${pct(s.value, total)}%)</text>`).join("");
      return `<svg viewBox="0 0 420 140" style="width:min(420px,100%)">${paths}${legend}</svg>`;
    };

    const agentSpendChart = hbars(
      (d.agents || []).slice(0, 10).map((a) => ({ label: a.agent_id, value: a.spend_usd, color: "#7C9BFF" })),
      (v) => fUsd(v));
    const modelMixChart = hbars(
      (d.models || []).slice(0, 8).map((m, i) => ({ label: shortModelName(m.model || "?"), value: m.calls, color: PAL[i % PAL.length] })),
      (v) => fNum(v));
    const sensDonut = donut([
      { label: "clean", value: sens.none || 0, color: "#9fb3a8" },
      { label: "PII", value: sens.pii || 0, color: "#b35b1e" },
      { label: "secrets", value: sens.secret || 0, color: "#c62828" },
    ]);

    const agentRows = (d.agents || []).slice(0, 15).map((a) => {
      const eff = (d.efficiency || {})[a.agent_id];
      return `<tr>
        <td>${esc(a.agent_id)}</td>
        <td class="num">${fNum(a.calls)}</td>
        <td class="num">${fUsd(a.spend_usd)}</td>
        <td class="num">${fNum(a.prompt_tokens)} / ${fNum(a.completion_tokens)}</td>
        <td>${(a.models || []).map((m) => esc(shortModelName(m || ""))).join(", ")}</td>
        <td class="num">${a.sensitive_calls > 0 ? `<b class="warn">${a.sensitive_calls}</b>` : "0"} / ${a.findings}</td>
        <td class="num">${a.quality != null ? a.quality.toFixed(1) + "/5" : "—"}</td>
        <td class="num">${eff && eff.score != null ? eff.score : "—"}</td>
      </tr>`;
    }).join("");
    const moreAgents = (d.agents || []).length > 15
      ? `<p class="dim small">+ ${(d.agents || []).length - 15} more agents below the traffic threshold — full list available in the dashboard.</p>` : "";

    const modelRows = (d.models || []).map((m) => `<tr>
        <td>${esc(shortModelName(m.model || "?"))}</td>
        <td class="num">${fNum(m.calls)}</td>
        <td class="num">${pct(m.calls, t.calls)}%</td>
        <td class="num">${fUsd(m.spend_usd)}</td>
      </tr>`).join("");

    const flowRows = ((d.dataflow || {}).category_to_provider || []).map((l) =>
      `<tr><td>${esc(l.source)}</td><td>${esc(l.target)}</td><td class="num">${fNum(l.value)}</td></tr>`).join("");

    const tagRows = (d.failure_tags || []).map((x) =>
      `<span class="chip">${esc(String(x.tag).replace(/_/g, " "))} × ${x.n}</span>`).join(" ");

    const shadowRows = ((d.shadow || {}).experiments || []).filter((e) => e.n > 0).map((e) => {
      const kind = e.trial_type === "prompt_diet"
        ? `prompt diet <b>${esc(e.diet_strategy || "")}</b> on ${esc(shortModelName(e.champion))}`
        : `${esc(shortModelName(e.champion))} vs ${esc(shortModelName(e.challenger))}`;
      const verdict = e.non_inferior === true ? `<b class="good">non-inferior (p=${e.p_value})</b>`
        : e.non_inferior === false ? `<span class="warn">not proven (p=${e.p_value ?? "—"})</span>`
        : `gathering evidence (${e.n}/10)`;
      const extra = [
        e.avg_reduction != null ? `payload −${Math.round(e.avg_reduction * 100)}%` : null,
        e.projected_monthly_saving_usd != null ? `projected ${fUsd(e.projected_monthly_saving_usd)}/mo` : null,
      ].filter(Boolean).join(" · ");
      return `<tr><td>${kind}</td><td class="num">${e.wins}W/${e.ties}T/${e.losses}L</td><td>${verdict}</td><td>${extra}</td></tr>`;
    }).join("");

    return `<!doctype html><html><head><meta charset="utf-8"><title>NautGate — LLM Usage & Governance Audit</title><style>
      @page { size: A4; margin: 18mm; }
      body { background: #fff; color: #1a1f28; font: 13px/1.55 -apple-system, "Segoe UI", Helvetica, Arial, sans-serif; margin: 0; padding: 40px; }
      .wrap { max-width: 780px; margin: 0 auto; }
      h1 { font-size: 22px; margin: 0; letter-spacing: -0.01em; }
      h2 { font-size: 14px; text-transform: uppercase; letter-spacing: 0.08em; color: #5F6600; border-bottom: 2px solid #808000; padding-bottom: 4px; margin: 30px 0 10px; page-break-after: avoid; }
      .sub { color: #6b7482; margin: 4px 0 24px; font-size: 13px; }
      .kpis { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin: 18px 0; }
      .kpi { border: 1px solid #e3e6ea; border-radius: 8px; padding: 12px 14px; }
      .kpi .v { font-size: 20px; font-weight: 700; } .kpi .k { color: #6b7482; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; }
      table { width: 100%; border-collapse: collapse; font-size: 12px; margin: 8px 0; page-break-inside: avoid; }
      th { text-align: left; color: #6b7482; font-size: 10px; text-transform: uppercase; letter-spacing: 0.06em; border-bottom: 1.5px solid #d7dbe0; padding: 6px 8px; }
      td { padding: 6px 8px; border-bottom: 1px solid #edeff2; vertical-align: top; }
      .num { text-align: right; font-variant-numeric: tabular-nums; }
      .good { color: #1a7f37; } .warn { color: #b35b1e; } .bad { color: #c62828; } .dim { color: #6b7482; } .small { font-size: 11px; }
      .chip { display: inline-block; border: 1px solid #e3e6ea; border-radius: 10px; padding: 2px 9px; font-size: 11px; margin: 2px; }
      ul { margin: 6px 0; padding-left: 20px; } li { margin: 3px 0; }
      .foot { margin-top: 34px; color: #8a92a0; font-size: 10px; border-top: 1px solid #e3e6ea; padding-top: 10px; }
      svg { page-break-inside: avoid; display: block; margin: 6px 0 10px; }
      .ax { fill: #6b7482; font-size: 10px; font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif; }
      .chartrow { display: flex; gap: 24px; align-items: flex-start; flex-wrap: wrap; }
    </style></head><body><div class="wrap">
      <h1>LLM Usage &amp; Governance Audit</h1>
      <div class="sub">NautGate gateway · period: ${esc(period)} · generated ${esc(genAt)} UTC · all traffic observed at the gateway, prompt/response bodies excluded from this document</div>

      <div class="kpis">
        <div class="kpi"><div class="v">${fNum(t.calls)}</div><div class="k">LLM calls</div></div>
        <div class="kpi"><div class="v">${fNum(t.agents)}</div><div class="k">Agents / users</div></div>
        <div class="kpi"><div class="v">${fUsd((t.metered_usd || 0) + subsCovered)}</div><div class="k">Usage value</div></div>
        <div class="kpi"><div class="v">${okPct}%</div><div class="k">Success rate</div></div>
      </div>

      ${dailyChart(d.daily) ? `<h2>Traffic over the period</h2>${dailyChart(d.daily)}` : ""}

      <h2>Executive summary</h2>
      <ul>
        <li><b>${fNum(t.calls)}</b> LLM calls by <b>${fNum(t.agents)}</b> agents across <b>${fNum(t.models)}</b> models; ${fNum(t.prompt_tokens)} input / ${fNum(t.completion_tokens)} output tokens.</li>
        <li>Usage value <b>${fUsd((t.metered_usd || 0) + subsCovered)}</b> — of which <b>${fUsd(t.metered_usd)}</b> billed (metered APIs) and <b>${fUsd(subsCovered)}</b> covered by flat-rate subscriptions.</li>
        <li><b>${fNum(sens.pii || 0)}</b> calls carried PII and <b>${fNum(sens.secret || 0)}</b> carried secrets (${(100 - pct((sens.none || 0), sensTotal))}% of traffic flagged); secret-classified prompt bodies are never stored or forwarded to evaluation models.</li>
        <li>Detected waste (payload shipped beyond task needs): <b>${fUsd(t.waste_usd)}</b>. ${fNum(t.retries_absorbed)} provider overloads were absorbed by gateway retries; ${fNum(t.errors)} calls errored; ${fNum(d.drift_alerts)} provider-drift alerts fired.</li>
      </ul>

      <h2>Traffic &amp; spend by agent</h2>
      ${agentSpendChart}
      <table><thead><tr><th>Agent</th><th class="num">Calls</th><th class="num">Spend*</th><th class="num">Tokens in/out</th><th>Models</th><th class="num">Sensitive / findings</th><th class="num">Quality</th><th class="num">Efficiency</th></tr></thead>
      <tbody>${agentRows}</tbody></table>${moreAgents}
      <p class="dim small">*Spend = metered cost where billed, otherwise metered-equivalent (notional) value of subscription usage. Quality = mean LLM-judge task-completion score on evaluated calls. Efficiency = composite 0–100 index (quality, relevance, waste, cache reuse, payload discipline).</p>

      <h2>Model mix</h2>
      ${modelMixChart}
      <table><thead><tr><th>Model</th><th class="num">Calls</th><th class="num">Share</th><th class="num">Spend*</th></tr></thead><tbody>${modelRows}</tbody></table>

      <h2>Data governance</h2>
      ${sensDonut}
      <p>Sensitivity classification of all prompts in the period: <b>${fNum(sens.none || 0)}</b> clean · <b class="warn">${fNum(sens.pii || 0)}</b> PII · <b class="bad">${fNum(sens.secret || 0)}</b> secrets. Sensitive-content classification gates body capture: secret prompts are recorded as metadata only.</p>
      ${flowRows ? `<table><thead><tr><th>Data category</th><th>Sent to provider</th><th class="num">Findings</th></tr></thead><tbody>${flowRows}</tbody></table>` : '<p class="good">No sensitive-data findings shipped to any provider in this period.</p>'}

      <h2>Response quality</h2>
      <p>${tagRows ? "Most frequent failure modes flagged by the LLM judge:" : "No failure tags recorded in this period."}</p>
      ${tagRows ? `<p>${tagRows}</p>` : ""}

      ${shadowRows ? `<h2>Optimization experiments</h2>
      <table><thead><tr><th>Experiment</th><th class="num">Trials</th><th>Verdict</th><th>Impact</th></tr></thead><tbody>${shadowRows}</tbody></table>
      <p class="dim small">Champion–challenger trials mirror a sample of real traffic to an alternative (cheaper model, or the same model with a pruned prompt); a blind judge compares answers without knowing which is which. "Non-inferior" = one-sided binomial test against a 90% as-good-or-better bar, α = 0.05.</p>` : ""}

      <div class="foot">
        Methodology: every LLM call routed through the NautGate gateway is recorded with routing decision, outcome, token usage and cost. Costs for flat-rate subscription traffic are stated at metered-equivalent list prices. Quality scores come from an independent LLM judge on a sampled subset. This report contains aggregate metadata only — no prompt or response content. Generated by NautGate.
      </div>
    </div></body></html>`;
  }

  function _showAuditReportModal(data) {
    document.getElementById("audit-report-page-modal")?.remove();
    const html = _buildAuditReportHtml(data);
    const wrap = document.createElement("div");
    wrap.id = "audit-report-page-modal";
    wrap.className = "dr-report-modal";
    wrap.innerHTML = `
      <div class="dr-report-content" style="width:min(980px,94vw);height:min(88vh,1000px)">
        <div class="dr-report-head">
          <span>LLM Usage &amp; Governance Audit · last ${data.days}d · ${(data.totals || {}).calls || 0} calls</span>
          <div class="dr-report-actions">
            <button class="ghost" id="audit-rp-tab" title="Open in a new tab — print from there for the paper version">🖼 open tab</button>
            <button class="ghost" id="audit-rp-download">💾 download</button>
            <button class="ghost" id="audit-rp-close">✕ close</button>
          </div>
        </div>
        <iframe class="audit-report-frame" style="background:#fff" sandbox=""></iframe>
      </div>`;
    document.body.appendChild(wrap);
    wrap.querySelector(".audit-report-frame").srcdoc = html;
    const blobUrl = () => URL.createObjectURL(new Blob([html], { type: "text/html" }));
    document.getElementById("audit-rp-close").addEventListener("click", () => wrap.remove());
    document.getElementById("audit-rp-tab").addEventListener("click", () => window.open(blobUrl(), "_blank", "noopener"));
    document.getElementById("audit-rp-download").addEventListener("click", () => {
      const a = document.createElement("a");
      a.href = blobUrl();
      a.download = `nautgate-audit-${data.days}d-${new Date().toISOString().slice(0, 10)}.html`;
      document.body.appendChild(a); a.click(); a.remove();
    });
    wrap.addEventListener("click", (e) => { if (e.target === wrap) wrap.remove(); });
  }

  // === Insights — counterfactuals, SPC, efficiency ==========================

  let insightsSpcMetric = "completion_tokens";

  async function loadInsights() {
    if (!getToken()) return;
    const panels = [
      ["in-efficiency", "efficiency?days=7", renderInEfficiency],
      ["in-simulator", "simulator?hours=168", renderInSimulator],
      ["in-substitution", "substitution", renderInSubstitution],
      ["in-spc", "spc?hours=168&metric=" + insightsSpcMetric, renderInSpc],
      ["in-overthinking", "overthinking", renderInOverthinking],
      ["in-dataflow", "dataflow?days=7", renderInDataflow],
    ];
    await Promise.all(panels.map(async ([mountId, path, render]) => {
      const mount = document.getElementById(mountId);
      if (!mount) return;
      if (!mount.dataset.loaded) mount.appendChild(NG.spinner("loading…"));
      try {
        const data = await api(path.startsWith("/") ? path : "/v1/insights/" + path);
        mount.innerHTML = "";
        mount.dataset.loaded = "1";
        render(mount, data);
      } catch (e) {
        mount.innerHTML = `<p class="hint" style="color:var(--bad)">load failed: ${esc(e.message || e)}</p>`;
      }
    }));
  }

  function renderInShadow(mount, data) {
    const cfg = data.config || {};
    // --- config controls -------------------------------------------------
    const toggle = NG.el("button", { class: "ghost" },
      cfg.enabled ? "⏸ pause shadowing" : "▶ start shadowing");
    const rateSel = NG.el("select", { class: "in-metric-sel", title: "share of eligible traffic mirrored" });
    [0.05, 0.1, 0.25, 0.5, 1.0].forEach((r) => {
      const o = NG.el("option", { value: String(r) }, Math.round(r * 100) + "%");
      if (Math.abs(r - (cfg.sample_rate || 0.1)) < 1e-9) o.selected = true;
      rateSel.appendChild(o);
    });
    const chalSel = NG.el("select", { class: "in-metric-sel", title: "challenger model" });
    [["openrouter", "openrouter/openai/gpt-4o-mini", "gpt-4o-mini"],
     ["openrouter", "openrouter/google/gemini-flash", "gemini-flash"],
     ["openrouter", "openrouter/deepseek/deepseek-v4-flash", "deepseek-flash"],
     ["anthropic", "claude-haiku-4-5", "haiku-4-5"]].forEach(([prov, model, label]) => {
      const o = NG.el("option", { value: prov + "|" + model }, label);
      if (model === cfg.challenger_model) o.selected = true;
      chalSel.appendChild(o);
    });
    async function patchCfg(patch) {
      const res = await fetch("/v1/shadow/config", {
        method: "PUT",
        headers: { Authorization: "Bearer " + getToken(), "Content-Type": "application/json" },
        body: JSON.stringify(patch),
      });
      if (!res.ok) throw new Error("http_" + res.status);
      const m = document.getElementById("in-shadow");
      m.innerHTML = "";
      renderInShadow(m, { ...data, config: await res.json() });
    }
    toggle.addEventListener("click", () => patchCfg({ enabled: !cfg.enabled }).catch((e) => alert(e.message)));
    const dietToggle = NG.el("button", { class: "ghost", title: "Prompt-diet trials: same model, pruned prompt (keep system + last 6 messages), blind-judged" },
      cfg.diet_enabled ? "⏸ diet trials" : "🥗 diet trials");
    dietToggle.addEventListener("click", () => patchCfg({ diet_enabled: !cfg.diet_enabled }).catch((e) => alert(e.message)));
    rateSel.addEventListener("change", () => patchCfg({ sample_rate: parseFloat(rateSel.value) }).catch((e) => alert(e.message)));
    chalSel.addEventListener("change", () => {
      const [prov, model] = chalSel.value.split("|");
      patchCfg({ challenger_provider: prov, challenger_model: model }).catch((e) => alert(e.message));
    });

    // --- experiments (hero verdict cards — Paper design) --------------------
    const exps = data.experiments || [];
    const dietApplied = (cfg.diet_apply || {})["*"];
    const TARGET_N = 10;  // evidence bar denominator until the verdict math engages
    const expHtml = exps.map((e) => {
      const isDiet = e.trial_type === "prompt_diet";
      const badge = e.non_inferior === true
        ? '<span class="in-shadow-badge good">✓ non-inferior · p=' + e.p_value + "</span>"
        : e.non_inferior === false
        ? '<span class="in-shadow-badge bad">not proven · p=' + (e.p_value ?? "—") + "</span>"
        : '<span class="in-shadow-badge dim">gathering</span>';
      const cost = e.champ_avg_cost != null && e.chall_avg_cost != null && e.champ_avg_cost > 0
        ? `cost <b style="color:var(--good)">${Math.round((e.chall_avg_cost / e.champ_avg_cost) * 100)}%</b> of champion`
        : "cost ratio pending";
      const saving = e.projected_monthly_saving_usd != null
        ? `projected <b style="color:var(--good)">$${e.projected_monthly_saving_usd.toFixed(2)}/mo</b>`
        : "";
      const tag = isDiet
        ? `PROMPT DIET · ${esc((e.diet_strategy || "").toUpperCase())}`
        : "MODEL ARBITRAGE";
      const head = isDiet
        ? `<b>${esc(shortModelName(e.champion))}</b> <span class="hint">with pruned prompt</span>`
        : `<b>${esc(shortModelName(e.champion))}</b> <span class="hint">vs</span> <b>${esc(shortModelName(e.challenger))}</b>`;
      const hero = isDiet && e.avg_reduction != null
        ? `−${Math.round(e.avg_reduction * 100)}%`
        : (e.ok_pct != null ? Math.round(e.ok_pct * 100) + "%" : "—");
      const heroSub = isDiet
        ? `payload · answers as good in ${e.ok_pct != null ? Math.round(e.ok_pct * 100) + "%" : "—"} of trials`
        : `as good or better · ${e.wins}W / ${e.ties}T / ${e.losses}L${e.errors ? " · " + e.errors + " err" : ""}`;
      const evidencePct = Math.min(100, Math.round((e.n / TARGET_N) * 100));
      const barColor = e.non_inferior === true ? "var(--good)" : e.non_inferior === false ? "var(--bad)" : "var(--warn)";
      let promote = "";
      if (isDiet && e.non_inferior === true && dietApplied !== e.diet_strategy) {
        promote = `<button class="exp-promote in-diet-promote" data-strategy="${esc(e.diet_strategy || "")}">⬆ Promote — trim live traffic</button>`;
      } else if (isDiet && dietApplied === e.diet_strategy) {
        promote = `<span class="in-shadow-badge good">LIVE on real traffic</span><button class="ghost in-diet-demote">stop trimming</button>`;
      }
      return `
        <div class="exp-card">
          <div class="in-shadow-head">${head}<span class="exp-tag">${tag}</span>${badge}</div>
          <div class="exp-hero"><span class="exp-hero-num" style="color:${isDiet ? "var(--text)" : "var(--good)"}">${hero}</span><span class="hint">${heroSub}</span></div>
          <div class="exp-evidence">
            <div class="exp-evidence-row"><span class="hint">evidence · ${e.n} of ${Math.max(TARGET_N, e.n)} paired trials</span><span style="font-family:var(--mono);font-size:11px;color:${barColor}">${e.non_inferior === true ? "✓ non-inferior" : e.non_inferior === false ? "✗ not proven" : "gathering"}</span></div>
            <div class="exp-bar"><div class="exp-bar-fill" style="width:${evidencePct}%;background:${barColor}"></div></div>
          </div>
          <div class="exp-foot"><span class="hint">${cost}</span>${saving ? `<span class="hint">${saving}</span>` : ""}<span style="flex:1"></span>${promote}</div>
        </div>`;
    }).join("");

    // --- recent trials -----------------------------------------------------
    const vChip = (v) => `<span class="in-verdict in-verdict-${esc(v || "error")}">${esc(v || "?")}</span>`;
    const trialsHtml = (data.recent_trials || []).map((t) => `
      <div class="in-trial-row" data-trial="${esc(t.id)}">
        ${vChip(t.verdict)}
        <span class="in-trial-prompt">${t.trial_type === "prompt_diet" ? "🥗 " : ""}${esc((t.prompt_excerpt || "").slice(0, 90))}</span>
        <span class="hint">${esc(t.judge_reason || "")}</span>
        <span class="hint" style="white-space:nowrap">${tsShort(t.ts)}</span>
      </div>`).join("");

    mount.appendChild(NG.card({
      title: "Shadow trials — champion vs challenger",
      meta: cfg.enabled
        ? `LIVE · mirroring ${Math.round((cfg.sample_rate || 0) * 100)}% of eligible traffic (tool-free, non-sensitive) to the challenger · blind-judged pairs · cap $${cfg.daily_cost_cap_usd}/day`
        : "paused — hit start to begin mirroring a sample of real traffic to a cheaper model and blind-judging the pairs",
      actions: [chalSel, rateSel, toggle, dietToggle],
      body: NG.el("div", {
        html: (expHtml ? `<div class="exp-cards">${expHtml}</div>` : '<p class="hint" style="padding:8px 12px">No trials yet. Enable shadowing and use the gateway normally — eligible calls (no tools, body captured, non-secret) get mirrored automatically.</p>')
          + (trialsHtml ? `<div class="section-title" style="padding:12px 12px 0">Recent paired trials — click one for the proof view</div>${trialsHtml}` : ""),
      }),
    }));
    mount.querySelectorAll(".in-trial-row").forEach((row) => {
      row.addEventListener("click", () => _showTrialModal(row.dataset.trial));
    });
    mount.querySelectorAll(".in-diet-promote").forEach((b) => {
      b.addEventListener("click", () =>
        patchCfg({ diet_apply: { "*": b.dataset.strategy } }).catch((e) => alert(e.message)));
    });
    mount.querySelectorAll(".in-diet-demote").forEach((b) => {
      b.addEventListener("click", () => patchCfg({ diet_apply: {} }).catch((e) => alert(e.message)));
    });
  }

  async function _showTrialModal(trialId) {
    let t;
    try {
      t = await api("/v1/shadow/trial/" + encodeURIComponent(trialId));
    } catch (e) {
      alert("trial load failed: " + (e.message || e));
      return;
    }
    document.getElementById("shadow-trial-modal")?.remove();
    const wrap = document.createElement("div");
    wrap.id = "shadow-trial-modal";
    wrap.className = "dr-report-modal";
    const isDiet = t.trial_type === "prompt_diet";
    const dietBits = isDiet && t.original_bytes
      ? ` · 🥗 ${esc(t.diet_strategy || "")} pruned payload ${t.original_bytes}B → ${t.pruned_bytes}B (−${Math.round((1 - t.pruned_bytes / t.original_bytes) * 100)}%)`
      : "";
    const side = (label, model, text, won) => `
      <div class="in-trial-col${won ? " in-trial-won" : ""}">
        <div class="in-trial-col-head">${esc(label)} · ${esc(shortModelName(model))}${won ? " 🏆" : ""}</div>
        <pre class="body-block in-trial-body">${esc(text || "(no answer)")}</pre>
      </div>`;
    wrap.innerHTML = `
      <div class="dr-report-content" style="width:min(1200px,95vw)">
        <div class="dr-report-head">
          <span>Paired trial · ${esc(tsShort(t.ts))} · verdict: <b>${esc(t.verdict || "?")}</b> <span class="hint">${esc(t.judge_reason || "")}${dietBits}</span></span>
          <div class="dr-report-actions"><button class="ghost" id="shadow-trial-close">✕ close</button></div>
        </div>
        <div class="in-trial-grid">
          <div class="in-trial-promptbox">
            <div class="in-trial-col-head">Prompt (real traffic)</div>
            <pre class="body-block in-trial-body">${esc((t.prompt_text || "").slice(0, 4000))}</pre>
          </div>
          ${side(isDiet ? "Original prompt" : "Champion", t.champion_model, t.champion_response, t.verdict === "champion")}
          ${side(isDiet ? "Pruned prompt" : "Challenger", t.challenger_model, t.challenger_response, t.verdict === "challenger")}
        </div>
        <p class="hint" style="padding:0 16px 12px">The judge saw both answers in random order without model names — the verdict above is blind. ${t.champion_cost_usd != null && t.challenger_cost_usd != null ? `Cost: ${usd(t.champion_cost_usd)} vs ${usd(t.challenger_cost_usd)}.` : ""}</p>
      </div>`;
    document.body.appendChild(wrap);
    document.getElementById("shadow-trial-close").addEventListener("click", () => wrap.remove());
    wrap.addEventListener("click", (e) => { if (e.target === wrap) wrap.remove(); });
  }

  function _inScoreColor(v) {
    return v == null ? "var(--text-dim)" : v >= 70 ? "var(--good)" : v >= 40 ? "var(--warn)" : "var(--bad)";
  }

  function renderInEfficiency(mount, data) {
    const agents = data.agents || [];
    const compBar = (label, v) => v == null ? "" : `
      <div class="in-comp" title="${esc(label)}: ${v}/100">
        <span class="in-comp-label">${esc(label)}</span>
        <span class="in-comp-track"><span class="in-comp-fill" style="width:${v}%;background:${_inScoreColor(v)}"></span></span>
        <span class="in-comp-val">${v}</span>
      </div>`;
    const rows = agents.map((a) => `
      <div class="in-eff-row">
        <div class="in-eff-score" style="color:${_inScoreColor(a.score)}">${a.score ?? "—"}</div>
        <div class="in-eff-agent">
          <div>${esc(a.agent_id)}</div>
          <div class="hint">${a.calls} calls · ${usd(a.cost_usd)} notional</div>
        </div>
        <div class="in-eff-comps">
          ${compBar("quality", a.components.quality)}
          ${compBar("relevance", a.components.relevance)}
          ${compBar("waste", a.components.waste)}
          ${compBar("cache", a.components.cache)}
          ${compBar("bloat", a.components.bloat)}
        </div>
      </div>`).join("");
    mount.appendChild(NG.card({
      title: "Gateway Efficiency Index",
      meta: `last ${data.days}d · composite of judged quality, relevance, waste, cache reuse & bloat — weights renormalize over available signals`,
      body: NG.el("div", { html: rows || '<p class="hint">No agents with ≥5 calls in the window.</p>' }),
    }));
  }

  function renderInSimulator(mount, data) {
    const pols = data.policies || [];
    const cur = data.current_quality;
    const rows = pols.map((p) => {
      const q = p.target_quality;
      const qBit = q
        ? `${q.quality.toFixed(2)}/5 <span class="hint">(n=${q.n})</span>`
        : '<span class="hint">never judged</span>';
      const savings = p.savings_usd;
      return `<tr>
        <td>${esc(p.policy)}</td>
        <td class="num">${usd(p.simulated_usd)}</td>
        <td class="num" style="color:${savings > 0 ? "var(--good)" : "var(--bad)"}">${savings > 0 ? "−" : "+"}${usd(Math.abs(savings))}</td>
        <td class="num">${qBit}</td>
        <td class="num hint">${p.priced_calls}/${p.calls}</td>
      </tr>`;
    }).join("");
    mount.appendChild(NG.card({
      title: "Counterfactual routing simulator",
      meta: `last ${Math.round(data.hours / 24)}d · every successful call repriced at the target model's rates · actual (notional) spend: `
        + usd(pols[0] ? pols[0].actual_usd : 0)
        + (cur != null ? ` · current judged quality ${cur}/5 over ${data.evaluated_calls} evals` : ""),
      body: NG.el("div", {
        html: `<table class="in-table">
          <thead><tr><th>route everything to…</th><th class="num">would cost</th><th class="num">Δ spend</th><th class="num">judged quality there</th><th class="num">priced</th></tr></thead>
          <tbody>${rows}</tbody></table>
        <p class="hint">Estimate: same token counts at the target's price sheet — real completions would differ in length. Quality column is the judge's average on calls that model actually served.</p>`,
      }),
    }));
  }

  function renderInSubstitution(mount, data) {
    const pairs = data.pairs || [];
    const rows = pairs.map((p) => `<tr>
        <td>${esc(shortModelName(p.asked))} → <b>${esc(shortModelName(p.served))}</b></td>
        <td class="num" style="color:${p.delta < 0 ? "var(--bad)" : "var(--good)"}">${p.delta > 0 ? "+" : ""}${p.delta.toFixed(2)}</td>
        <td class="num">${p.mean_substituted.toFixed(2)} vs ${p.mean_as_asked.toFixed(2)}</td>
        <td class="num">${p.n_substituted} / ${p.n_as_asked}</td>
        <td class="num">${p.p_value == null ? "—" : p.p_value < 0.001 ? "<0.001" : p.p_value.toFixed(3)}${p.p_value != null && p.p_value < 0.05 ? " ✓" : ""}</td>
      </tr>`).join("");
    mount.appendChild(NG.card({
      title: "Silent substitution impact",
      meta: `judged task-completion when a different model served than was asked · ${data.judged_calls} judged calls · date-snapshot aliases excluded`,
      body: NG.el("div", {
        html: rows
          ? `<table class="in-table"><thead><tr><th>asked → served</th><th class="num">Δ score</th><th class="num">sub vs as-asked</th><th class="num">n</th><th class="num">p (Welch)</th></tr></thead><tbody>${rows}</tbody></table>`
          : '<p class="hint">No substitution pairs with ≥5 judged calls on both sides yet.</p>',
      }),
    }));
  }

  function renderInSpc(mount, data) {
    const metricSel = NG.el("select", { class: "in-metric-sel" });
    [["completion_tokens", "output tokens"], ["reasoning_share", "thinking share"],
     ["tool_calls", "tool calls / turn"],
     ["first_byte_ms", "first byte ms"], ["empty_rate", "empty-response rate"]].forEach(([v, label]) => {
      const o = NG.el("option", { value: v }, label);
      if (v === insightsSpcMetric) o.selected = true;
      metricSel.appendChild(o);
    });
    metricSel.addEventListener("change", async () => {
      insightsSpcMetric = metricSel.value;
      const m = mount;  // re-render in place — works on Insights AND Model Health
      m.innerHTML = "";
      m.appendChild(NG.spinner("recomputing…"));
      try {
        const d = await api("/v1/insights/spc?hours=168&metric=" + insightsSpcMetric);
        m.innerHTML = "";
        renderInSpc(m, d);
      } catch (e) { m.innerHTML = `<p class="hint" style="color:var(--bad)">${esc(e.message || e)}</p>`; }
    });
    const body = NG.el("div");
    (data.models || []).forEach((m) => {
      const points = m.buckets.map((ts, i) => ({
        ts: ts * 1000,
        observed: m.ewma[i],
        mean: m.mean,
        stddev: m.sd > 0 ? (m.ucl[i] - m.mean) / 3 : 0,
        z: m.sd > 0 && (m.ucl[i] - m.mean) > 0 ? (3 * (m.ewma[i] - m.mean)) / (m.ucl[i] - m.mean) : 0,
      }));
      const viol = m.violations.length;
      body.appendChild(NG.el("div", { class: "in-spc-model" }, [
        NG.el("div", { class: "in-spc-head" }, [
          NG.el("b", {}, shortModelName(m.model)),
          NG.el("span", { class: "hint" }, ` ${m.calls} calls · μ=${fmtNum(m.mean)} σ=${fmtNum(m.sd)}`),
          viol ? NG.el("span", { style: { color: "var(--bad)", marginLeft: "8px" } }, `⚠ ${viol} out-of-control point${viol > 1 ? "s" : ""}`) : NG.el("span", { style: { color: "var(--good)", marginLeft: "8px" } }, "in control"),
        ]),
        NG.anomalyBand({ points, height: 120, fmtV: (v) => fmtNum(v) }),
      ]));
    });
    if (!(data.models || []).length) body.appendChild(NG.el("p", { class: "hint" }, "Not enough hourly buckets yet."));
    const card = NG.card({
      title: "Behavior control charts (EWMA)",
      meta: "hourly means per model, EWMA λ=0.2 with ±3σ limits — a dot outside the band is a statistically real behavior shift, not noise",
      actions: [metricSel],
      body,
    });
    mount.appendChild(card);
  }

  function renderInOverthinking(mount, data) {
    const pts = data.points || [];
    const withReasoning = pts.filter((p) => p.reasoning_share > 0);
    const models = [...new Set(pts.map((p) => p.model))].slice(0, 4);
    const colorOf = (m) => PULSE_COLORS[models.indexOf(m) % PULSE_COLORS.length] || "var(--text-dim)";
    const W = 1000, H = 240, padX = 46, padY = 20;
    const X = (share) => padX + share * (W - padX * 2);
    const Y = (score) => padY + (1 - score / 5) * (H - padY * 2);
    const dots = pts.filter((p) => models.includes(p.model)).map((p) =>
      `<circle cx="${X(p.reasoning_share).toFixed(1)}" cy="${Y(p.score).toFixed(1)}" r="3.5"
         fill="${colorOf(p.model)}" fill-opacity="0.55"><title>${esc(shortModelName(p.model))} · thinking ${(p.reasoning_share * 100).toFixed(0)}% · score ${p.score}/5</title></circle>`).join("");
    const axis = `
      <line x1="${padX}" y1="${Y(0)}" x2="${W - padX}" y2="${Y(0)}" stroke="var(--border)"/>
      <line x1="${padX}" y1="${padY}" x2="${padX}" y2="${Y(0)}" stroke="var(--border)"/>
      ${[0, 1, 2, 3, 4, 5].map((s) => `<text x="${padX - 8}" y="${Y(s) + 4}" text-anchor="end" class="in-axis">${s}</text>`).join("")}
      ${[0, 0.25, 0.5, 0.75, 1].map((x) => `<text x="${X(x)}" y="${H - 2}" text-anchor="middle" class="in-axis">${x * 100}%</text>`).join("")}`;
    const legend = models.map((m) => `<span><span class="swatch" style="background:${colorOf(m)}"></span>${esc(shortModelName(m))}</span>`).join("");
    mount.appendChild(NG.card({
      title: "The overthinking curve",
      meta: `judged task-completion vs share of tokens spent thinking · ${pts.length} judged calls (${withReasoning.length} with reasoning)`,
      body: NG.el("div", {
        html: `<svg viewBox="0 0 ${W} ${H}" class="in-scatter" preserveAspectRatio="none">${axis}${dots}</svg>
          <div class="audit-legend" style="margin-top:6px">${legend}<span class="hint">x = thinking share of output · y = judge score</span></div>`,
      }),
    }));
  }

  function renderInDataflow(mount, data) {
    const left = data.agent_to_category || [];
    const right = data.category_to_provider || [];
    if (!left.length && !right.length) {
      mount.appendChild(NG.card({
        title: "Sensitive data flow",
        meta: `last ${data.days}d`,
        body: NG.el("p", { class: "hint" }, "No classified signals in the window — nothing sensitive shipped. That's the good outcome."),
      }));
      return;
    }
    // Three columns: agents, categories, providers. Node heights ∝ flow volume.
    const CAT_COLOR = { credentials: "var(--bad)", secrets: "var(--warn)", pii: "var(--info)", infrastructure: "var(--series-4)", other: "var(--text-dim)" };
    const sum = (m) => Object.values(m).reduce((a, b) => a + b, 0);
    const tally = (links, key) => links.reduce((m, l) => (m[l[key]] = (m[l[key]] || 0) + l.value, m), {});
    const agents = tally(left, "source"), cats = tally(left, "target"), provs = tally(right, "target");
    const W = 1000, H = Math.max(200, 40 * Math.max(Object.keys(agents).length, Object.keys(cats).length, Object.keys(provs).length)), GAP = 8, COLW = 12;
    const colX = [40, W / 2 - COLW / 2, W - 40 - COLW];
    const layout = (m, total) => {
      let y = 10; const out = {};
      for (const [name, v] of Object.entries(m).sort((a, b) => b[1] - a[1])) {
        const h = Math.max(14, (v / total) * (H - 20 - GAP * Object.keys(m).length));
        out[name] = { y, h, v };
        y += h + GAP;
      }
      return out;
    };
    const totL = sum(agents) || 1, totC = sum(cats) || 1, totP = sum(provs) || 1;
    const A = layout(agents, totL), C = layout(cats, totC), P = layout(provs, totP);
    const link = (x1, n1, x2, n2, value, total, color) => {
      const w = Math.max(2, (value / total) * 24);
      const y1 = n1.y + n1.h / 2, y2 = n2.y + n2.h / 2;
      const mx = (x1 + x2) / 2;
      return `<path d="M ${x1 + COLW} ${y1} C ${mx} ${y1} ${mx} ${y2} ${x2} ${y2}" fill="none" stroke="${color}" stroke-width="${w}" stroke-opacity="0.35"><title>${value} finding${value > 1 ? "s" : ""}</title></path>`;
    };
    const links =
      left.map((l) => A[l.source] && C[l.target] ? link(colX[0], A[l.source], colX[1], C[l.target], l.value, totC, CAT_COLOR[l.target] || "var(--text-dim)") : "").join("")
      + right.map((l) => C[l.source] && P[l.target] ? link(colX[1], C[l.source], colX[2], P[l.target], l.value, totC, CAT_COLOR[l.source] || "var(--text-dim)") : "").join("");
    const nodes = (m, x, colorFn, anchor) => Object.entries(m).map(([name, n]) => `
      <rect x="${x}" y="${n.y}" width="${COLW}" height="${n.h}" rx="2" fill="${colorFn(name)}"/>
      <text x="${anchor === "end" ? x - 6 : x + COLW + 6}" y="${n.y + n.h / 2 + 4}" text-anchor="${anchor}" class="in-axis">${esc(name)} (${n.v})</text>`).join("");
    mount.appendChild(NG.card({
      title: "Sensitive data flow",
      meta: `last ${data.days}d · agent → data category → provider, from the classifier's per-call findings`,
      body: NG.el("div", {
        html: `<svg viewBox="0 0 ${W} ${H}" class="in-sankey" style="height:${Math.min(H, 340)}px">${links}
          ${nodes(agents, colX[0], () => "var(--accent)", "end")}
          ${nodes(cats, colX[1], (n) => CAT_COLOR[n] || "var(--text-dim)", "start")}
          ${nodes(provs, colX[2], () => "var(--series-2)", "end")}</svg>`,
      }),
    }));
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
    if (!getToken()) return;
    const alertsEl = document.getElementById("dr-alerts");
    if (!alertsEl) return;
    alertsEl.innerHTML = '<p class="hint">loading…</p>';
    try {
      const data = await api("/v1/drift");
      const alerts = data.alerts || [];
      const baselines = data.baselines || [];

      const open = alerts.filter((a) => a.is_open);

      // Compact alert callouts (match mock)
      alertsEl.innerHTML = "";
      if (!open.length) {
        alertsEl.appendChild(NG.el("p", { class: "hint" }, "No open alerts. Drift detection needs ~10 samples per (provider, model, metric) to warm up."));
      } else {
        const row = NG.el("div", { class: "dr-callouts" });
        open.forEach((a) => {
          const crit = Math.abs(a.peak_z_score || 0) > 3 || a.peak_z_score === -99;
          const c = NG.el("div", { class: "dr-callout " + (crit ? "dr-callout-bad" : "dr-callout-warn") });
          c.appendChild(NG.el("span", { class: "dr-callout-icon", html: '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M12 3l9 16H3z"/><path d="M12 10v4"/><circle cx="12" cy="17" r=".6" fill="currentColor"/></svg>' }));
          const mid = NG.el("div", { class: "dr-callout-mid" });
          mid.appendChild(NG.el("div", { class: "dr-callout-title" }, `${shortModelName(a.model)} · ${a.metric}`));
          mid.appendChild(NG.el("div", { class: "dr-callout-sub" }, `${a.sample_count || 0} samples · started ${fmtAge(a.started_at)}`));
          c.appendChild(mid);
          const z = a.peak_z_score === -99 ? "compaction" : `z = ${(a.peak_z_score || 0).toFixed(1)} ${a.direction === "up" ? "▲" : "▼"}`;
          c.appendChild(NG.el("span", { class: "dr-callout-z" }, z));
          row.appendChild(c);
        });
        alertsEl.appendChild(row);
      }

      // Anomaly-band chart — defaults to the primary open alert, updates when
      // any alert row is clicked.
      renderDriftBand(open[0] || alerts[0]);

      // All-alerts history → DataTable (10/page, click a row to chart it)
      const histCard = document.getElementById("dr-history-card");
      if (histCard) {
        NG.DataTable(histCard, {
          title: "All alerts", meta: "open + resolved · click a row to chart it",
          countLabel: (n) => `${n} alert${n === 1 ? "" : "s"}`,
          searchPlaceholder: "Filter…",
          defaultSort: { key: "started", dir: "desc" },
          emptyText: "No alerts yet.",
          rows: alerts,
          pageSize: 10,
          pageSizeOptions: [10, 25, 50],
          onRowClick: (r) => renderDriftBand(r),
          rowClass: (r) => (r.is_open ? "v2-row-bad" : null),
          columns: [
            { key: "provider", label: "Provider", render: (r) => NG.providerTag(r.provider), sortValue: (r) => r.provider || "" },
            { key: "model", label: "Model", render: (r) => NG.el("span", { class: "v2-strong" }, r.model), sortValue: (r) => r.model || "" },
            { key: "metric", label: "Metric", mono: true, render: (r) => r.metric, sortValue: (r) => r.metric || "" },
            { key: "direction", label: "Dir", render: (r) => (r.direction === "up" ? "↑" : "↓"), sortValue: (r) => r.direction || "" },
            { key: "peak_z", label: "Peak z", align: "right", render: (r) => (r.peak_z_score === -99 ? "compaction" : (r.peak_z_score != null ? r.peak_z_score.toFixed(2) : "—")), sortValue: (r) => (r.peak_z_score === -99 ? 99 : Math.abs(r.peak_z_score || 0)) },
            { key: "peak_obs", label: "Peak observed", align: "right", render: (r) => fmtNum(r.peak_observed), sortValue: (r) => r.peak_observed || 0 },
            { key: "baseline", label: "Baseline", align: "right", render: (r) => fmtNum(r.baseline_at_alert), sortValue: (r) => r.baseline_at_alert || 0 },
            { key: "samples", label: "Samples", align: "right", render: (r) => r.sample_count || 0, sortValue: (r) => r.sample_count || 0 },
            { key: "started", label: "Started", render: (r) => fmtAge(r.started_at), sortValue: (r) => r.started_at || "" },
            { key: "status", label: "Status", sortable: false, render: (r) => NG.chip(r.is_open ? "Open" : "Resolved", r.is_open ? "open" : "resolved") },
          ],
        });
      }

      // Baselines → DataTable
      const baseCard = document.getElementById("dr-baselines-card");
      if (baseCard) {
        NG.DataTable(baseCard, {
          title: "Baselines",
          countLabel: (n) => `${n} baseline${n === 1 ? "" : "s"}`,
          searchPlaceholder: "Filter…",
          defaultSort: { key: "lastz", dir: "desc" },
          emptyText: "No baselines yet — make some requests through /v1/chat/completions.",
          rows: baselines,
          columns: [
            { key: "provider", label: "Provider", render: (r) => NG.providerTag(r.provider), sortValue: (r) => r.provider || "" },
            { key: "model", label: "Model", render: (r) => NG.el("span", { class: "v2-strong" }, r.model), sortValue: (r) => r.model || "" },
            { key: "metric", label: "Metric", mono: true, render: (r) => r.metric, sortValue: (r) => r.metric || "" },
            { key: "mean", label: "Mean", align: "right", render: (r) => fmtNum(r.mean), sortValue: (r) => r.mean || 0 },
            { key: "stddev", label: "Stddev", align: "right", render: (r) => fmtNum(r.stddev), sortValue: (r) => r.stddev || 0 },
            { key: "samples", label: "Samples", align: "right", render: (r) => r.sample_count || 0, sortValue: (r) => r.sample_count || 0 },
            { key: "last_obs", label: "Last observed", align: "right", render: (r) => fmtNum(r.last_observed), sortValue: (r) => r.last_observed || 0 },
            { key: "lastz", label: "Last z", align: "right", render: (r) => driftZCell(r.last_z_score), sortValue: (r) => Math.abs(r.last_z_score || 0) },
            { key: "updated", label: "Updated", render: (r) => fmtAge(r.updated_at), sortValue: (r) => r.updated_at || "" },
          ],
        });
      }
    } catch (e) {
      alertsEl.innerHTML = `<p class="hint">load failed: ${esc(e.message || e)}</p>`;
    }
  }

  function driftZCell(z) {
    if (z == null) return "—";
    const color = Math.abs(z) > 3 ? "var(--bad)" : Math.abs(z) > 2 ? "var(--warn)" : "var(--text)";
    return NG.el("span", { style: { color } }, z.toFixed(2));
  }

  // What each drift metric means + how to format its value.
  const DRIFT_METRIC_META = {
    input_tokens_per_byte: { label: "input tokens / byte", fmt: (v) => v == null ? "—" : v.toFixed(4), means: "How densely your prompt tokenizes. A shift means the provider changed its tokenizer or cache behaviour — same text, different token count (and cost)." },
    response_size_bytes: { label: "response size (bytes)", fmt: (v) => v == null ? "—" : fmtNum(v) + " B", means: "How long the model's replies are. Drift here is verbosity change — the model getting chattier or terser for the same kind of request." },
    first_byte_ms: { label: "time to first byte", fmt: (v) => v == null ? "—" : Math.round(v) + " ms", means: "Latency until the first token streams back. Upward drift = the provider getting slower to start." },
    duration_ms: { label: "total duration", fmt: (v) => v == null ? "—" : Math.round(v) + " ms", means: "End-to-end call latency. Upward drift = slower completions overall." },
    messages_count_delta: { label: "messages-count delta", fmt: (v) => v == null ? "—" : String(v), means: "Sudden change in how many messages the client sends — a sharp drop usually means a context compaction event fired." },
  };
  function driftMetricFmt(metric) {
    const m = DRIFT_METRIC_META[metric];
    return m ? m.fmt : ((v) => v == null ? "—" : (typeof v === "number" ? v.toFixed(2) : String(v)));
  }
  function zInterpretation(z) {
    if (z == null) return "no z-score";
    const a = Math.abs(z);
    if (a > 3) return `${z.toFixed(2)}σ from baseline — a real anomaly (beyond ±3σ)`;
    if (a > 2) return `${z.toFixed(2)}σ from baseline — worth watching`;
    return `${z.toFixed(2)}σ from baseline — within normal range`;
  }

  // Click a drift data-point → explain the sample in the right drawer + show
  // the actual request that produced it.
  async function openDriftSampleDetail(p, alert) {
    const meta = DRIFT_METRIC_META[alert.metric] || { label: alert.metric, fmt: driftMetricFmt(alert.metric), means: "" };
    const fmt = meta.fmt;
    document.getElementById("detail-id").textContent = `${shortModelName(alert.model)} · ${alert.metric}`;
    const band = (p.mean != null && p.stddev != null) ? `${fmt(p.mean - 3 * p.stddev)} … ${fmt(p.mean + 3 * p.stddev)}` : "—";
    const anomaly = Math.abs(p.z || 0) > 3;
    let html = "";
    html += '<div class="section-title">What you\'re looking at</div>';
    html += `<p class="hint" style="line-height:1.5">This is one <b>${esc(meta.label)}</b> sample for <b>${esc(shortModelName(alert.model))}</b>, taken ${esc(new Date(p.ts).toLocaleString())}. ${esc(meta.means)}</p>`;
    html += '<div class="kv">';
    html += `<div class="k">observed</div><div class="v" style="color:${anomaly ? "var(--bad)" : "var(--text)"}">${esc(fmt(p.observed))}</div>`;
    html += `<div class="k">baseline μ</div><div class="v">${esc(fmt(p.mean))}</div>`;
    html += `<div class="k">normal band (±3σ)</div><div class="v">${esc(band)}</div>`;
    html += `<div class="k">z-score</div><div class="v" style="color:${anomaly ? "var(--bad)" : Math.abs(p.z||0) > 2 ? "var(--warn)" : "var(--text)"}">${esc(zInterpretation(p.z))}</div>`;
    html += "</div>";
    if (p.decisionId) {
      html += '<div class="section-title">The request that produced this sample</div>';
      html += '<p class="hint" id="drift-req-loading">loading request…</p>';
    } else {
      html += '<p class="hint">No linked request id for this sample.</p>';
    }
    document.getElementById("detail-body").innerHTML = html;
    drawer.classList.remove("hidden");
    if (p.decisionId) {
      try {
        const scope = getActiveAgentScope();
        const d = await api("/v1/decisions/" + encodeURIComponent(p.decisionId) + (scope ? "?agent_id=" + encodeURIComponent(scope) : ""));
        const slot = document.getElementById("drift-req-loading");
        if (slot) slot.outerHTML = renderAuditDetail(d);
      } catch (_e) {
        const slot = document.getElementById("drift-req-loading");
        if (slot) slot.textContent = "Couldn't load the linked request.";
      }
    }
  }

  // Render the anomaly-band chart for one alert (provider/model/metric).
  // Called for the primary alert on load + whenever an alert row is clicked.
  async function renderDriftBand(alert) {
    const bandCard = document.getElementById("dr-band-card");
    if (!bandCard) return;
    bandCard.innerHTML = "";
    if (!alert) {
      bandCard.appendChild(NG.card({ title: "Anomaly band", body: NG.el("div", { class: "hint" }, "No alerts to chart yet.") }));
      return;
    }
    const body = NG.el("div");
    const chartEl = NG.el("div", { html: '<p class="hint" style="padding:12px">loading…</p>' });
    body.appendChild(chartEl);
    const legend = [
      NG.el("span", { class: "v2-chart-legend-item", html: '<span style="display:inline-block;width:14px;height:8px;background:rgba(76,141,255,0.25);border:1px solid rgba(76,141,255,0.4);border-radius:2px"></span> ±3σ band' }),
      NG.el("span", { class: "v2-chart-legend-item", html: '<span class="v2-legend-dot" style="background:#7C9BFF"></span> observed' }),
    ];
    bandCard.appendChild(NG.card({ title: `${shortModelName(alert.model)} — ${alert.metric}`, meta: alert.is_open ? "open" : "resolved", actions: legend, body }));
    try {
      const anom = await api(`/v1/drift/${encodeURIComponent(alert.provider)}/${alert.model}/anomalies?metric=${encodeURIComponent(alert.metric)}&limit=80`);
      const points = (anom.items || []).map((it) => ({
        ts: it.ts, observed: it.observed_value, mean: it.baseline_mean, stddev: it.baseline_stddev, z: it.z_score, decisionId: it.decision_id,
      }));
      chartEl.innerHTML = "";
      chartEl.appendChild(NG.anomalyBand({
        points, height: 220,
        fmtV: driftMetricFmt(alert.metric),
        onPointClick: (pt) => openDriftSampleDetail(pt, alert),
      }));
      const hint = NG.el("p", { class: "hint", style: { margin: "8px 2px 0" } }, "Click any point to see what produced it.");
      chartEl.appendChild(hint);
    } catch (_e) {
      chartEl.innerHTML = '<div class="v2-chart-fallback">No anomaly samples available for this metric.</div>';
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
      renderCache(summary, prefixes);
    } catch (e) {
      /* swallow; auth chip explains */
    }
  }

  // Click a cache prefix row → explain it in the right drawer.
  function openCachePrefixDetail(r) {
    const reuse = r.reuse_ratio == null ? "—" : r.reuse_ratio.toFixed(1) + "×";
    document.getElementById("detail-id").textContent = shortHash(r.prefix_hash) + (r._leaky ? " · leaky" : " · reused");
    let html = "";
    html += '<div class="section-title">What you\'re looking at</div>';
    html += `<p class="hint" style="line-height:1.5">A <b>prompt-cache prefix</b> is the stable head of your requests (system prompt + tool defs) the provider can serve from cache. This one ran on <b>${esc(shortModelName(r.model) || "—")}</b>.</p>`;
    html += '<div class="kv">';
    html += `<div class="k">prefix hash</div><div class="v" style="word-break:break-all">${esc(r.prefix_hash || "—")}</div>`;
    html += `<div class="k">reads</div><div class="v">${(r.reads || 0).toLocaleString()}</div>`;
    html += `<div class="k">writes</div><div class="v" style="color:${r._leaky ? "var(--bad)" : "var(--text)"}">${(r.writes || 0).toLocaleString()}</div>`;
    html += `<div class="k">reuse ratio</div><div class="v" style="color:${r._leaky ? "var(--bad)" : "var(--good)"}">${esc(reuse)}</div>`;
    html += `<div class="k">calls</div><div class="v">${(r.calls || 0).toLocaleString()}</div>`;
    html += "</div>";
    html += '<div class="section-title">Verdict</div>';
    if (r._leaky) {
      html += '<p class="hint" style="line-height:1.5"><b style="color:var(--bad)">Leaky.</b> Written to cache far more than read back (reuse &lt; 1). Something tiny is mutating the supposedly-stable head — usually an injected <b>timestamp, request id, or random value</b> in the system prompt or tool defs. Each call writes a fresh entry (Anthropic charges a 25% write premium) that\'s never reused, so you pay the premium with none of the discount. Find and pin the changing value.</p>';
    } else {
      html += '<p class="hint" style="line-height:1.5"><b style="color:var(--good)">Reused.</b> Caching is working — this stable prefix is read back many times, so you pay the cheap cache-read rate (~1/10th of fresh input) on the bulk of these tokens.</p>';
    }
    document.getElementById("detail-body").innerHTML = html;
    drawer.classList.remove("hidden");
  }

  function renderCache(s, p) {
    const t = (s && s.totals) || {};
    const leakyCount = ((p && p.leaky) || []).length;

    // KPIs — hit rate / saved (hl, off→on sub) / write:read / leaky (red)
    const kpis = document.getElementById("cache-kpis");
    if (kpis) NG.statRow(kpis, [
      NG.statCard({ label: "Hit rate", value: pct(t.hit_rate), sub: "of input served from cache" }),
      NG.statCard({ label: "Saved by caching", value: usd(t.saved_usd), sub: t.cache_off_usd != null ? `${usd(t.cache_off_usd)} → ${usd(t.cache_on_usd)}` : "read discount − write premium", highlight: true }),
      NG.statCard({ label: "Write : read", value: t.write_read_ratio == null ? "—" : t.write_read_ratio.toFixed(3), sub: "low = healthy reuse" }),
      NG.statCard({ label: "Leaky prefixes", value: leakyCount, tone: leakyCount > 0 ? "bad" : null, sub: leakyCount ? "write-heavy, low reuse" : "none detected" }),
    ]);

    // Populate the model filter once (preserve current selection).
    const sel = document.getElementById("cache-model-filter");
    if (sel && sel.options.length <= 1) {
      const cur = sel.value;
      for (const r of (s.by_model || [])) {
        const o = document.createElement("option");
        o.value = r.model; o.textContent = r.model;
        sel.appendChild(o);
      }
      sel.value = cur;
    }

    // Prefix reuse — reused + leaky merged; leaky flagged red; All/Leaky toggle.
    const prefixCard = document.getElementById("cache-prefix-card");
    if (prefixCard) {
      const reused = ((p && p.top_reused) || []).map((r) => ({ ...r, _leaky: false }));
      const leaky = ((p && p.leaky) || []).map((r) => ({ ...r, _leaky: true }));
      const all = leaky.concat(reused);
      NG.DataTable(prefixCard, {
        title: "Prefix reuse", meta: "cacheable prefixes · find the leaks",
        countLabel: (n) => `${n} prefix${n === 1 ? "" : "es"}`,
        search: false,
        columnsMenu: false,
        segments: { options: [
          { label: "All", predicate: null },
          { label: "Leaky", predicate: (r) => r._leaky },
        ] },
        defaultSort: { key: "reuse", dir: "desc" },
        emptyText: "No cacheable prefixes seen yet.",
        rows: all,
        onRowClick: (r) => openCachePrefixDetail(r),
        rowClass: (r) => (r._leaky ? "v2-row-bad" : null),
        columns: [
          { key: "prefix_hash", label: "Prefix", mono: true, render: (r) => NG.el("code", { title: r.prefix_hash }, shortHash(r.prefix_hash)), sortValue: (r) => r.prefix_hash || "" },
          { key: "model", label: "Model", render: (r) => shortModelName(r.model), sortValue: (r) => r.model || "" },
          { key: "reads", label: "Reads", align: "right", render: (r) => (r.reads || 0).toLocaleString(), sortValue: (r) => r.reads || 0 },
          { key: "writes", label: "Writes", align: "right", render: (r) => NG.el("span", { style: r._leaky ? { color: "var(--bad)" } : null }, (r.writes || 0).toLocaleString()), sortValue: (r) => r.writes || 0 },
          { key: "reuse", label: "Reuse", align: "right", render: (r) => NG.el("span", { style: { color: r._leaky ? "var(--bad)" : "var(--good)" } }, r.reuse_ratio == null ? "—" : r.reuse_ratio.toFixed(1) + "×"), sortValue: (r) => r.reuse_ratio || 0 },
          { key: "status", label: "Status", render: (r) => NG.chip(r._leaky ? "Leaky" : "Reused", r._leaky ? "leaky" : "reused"), sortable: false },
        ],
      });
    }
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

      // Pivot each target's legs into sub-vs-metered comparison rows.
      const targets = s.targets || [];
      const openAlerts = (s.alerts || []).filter((a) => !a.resolved_at);
      const isSub = (leg, name) => /oauth|sub|anthropic|chatgpt|max/i.test((leg && leg.via) || name || "");
      const cmpRows = targets.map((t) => {
        const entries = Object.entries(t.legs || {});
        let sub = null, met = null;
        for (const [name, leg] of entries) {
          if (sub == null && isSub(leg, name)) sub = leg;
          else if (met == null) met = leg;
        }
        if (!sub && entries[0]) sub = entries[0][1];
        if (!met && entries[1]) met = entries[1][1];
        const modelMism = sub && sub.observed_model && !modelsLooseMatch(t.model, sub.observed_model);
        const st = sub && sub.tokens_per_byte, mt = met && met.tokens_per_byte;
        const tpbMism = st != null && mt ? Math.abs((st - mt) / mt) > 0.10 : false;
        return { model: t.model, sub: sub || {}, met: met || {}, mismatch: modelMism || tpbMism };
      });
      let divergent = cmpRows.filter((r) => r.mismatch).length;
      lastProbeSummary = { count: targets.length, divergent, alerts: openAlerts.length, rows: cmpRows };

      // KPIs — integrity / models probed / open alerts / last cycle
      const lastRun = cfg.last_run_at ? fmtAge(cfg.last_run_at) : "never";
      const kpis = document.getElementById("probe-kpis");
      if (kpis) NG.statRow(kpis, [
        NG.statCard({ label: "Integrity", value: divergent ? `${divergent} divergence${divergent === 1 ? "" : "s"}` : "clean", tone: divergent ? "bad" : "good", sub: "subscription vs metered" }),
        NG.statCard({ label: "Models probed", value: targets.length }),
        NG.statCard({ label: "Open alerts", value: openAlerts.length, tone: openAlerts.length ? "bad" : null }),
        NG.statCard({ label: "Last cycle", value: lastRun, sub: cfg.interval_hours ? `every ${cfg.interval_hours}h` : "" }),
      ]);

      // Fingerprint over cycles — tokens/byte, subscription vs metered, for the first target
      const fpCard = document.getElementById("probe-fingerprint-card");
      if (fpCard) {
        fpCard.innerHTML = "";
        const firstModel = targets[0] && targets[0].model;
        const body = NG.el("div");
        const chartEl = NG.el("div", { class: "v2-chart", html: '<p class="hint" style="padding:12px">' + (firstModel ? "loading…" : "No targets configured yet.") + "</p>" });
        body.appendChild(chartEl);
        const legend = [
          NG.el("span", { class: "v2-chart-legend-item", html: '<span class="v2-legend-dot" style="background:#7C9BFF"></span>subscription' }),
          NG.el("span", { class: "v2-chart-legend-item", html: '<span class="v2-legend-dot" style="background:#4C8DFF"></span>metered' }),
        ];
        fpCard.appendChild(NG.card({ title: "Tokenizer fingerprint over cycles", actions: firstModel ? legend : [], body }));
        if (firstModel) {
          try {
            const hist = await api(`/v1/probe/history?model=${encodeURIComponent(firstModel)}&hours=720`);
            const runs = (hist.runs || []).filter((r) => r.tokens_per_byte != null);
            // group by ts, split sub/metered by via
            const byTs = {};
            runs.forEach((r) => {
              const k = r.ts;
              byTs[k] = byTs[k] || { ts: k, sub: null, met: null };
              if (isSub(r)) byTs[k].sub = r.tokens_per_byte; else byTs[k].met = r.tokens_per_byte;
            });
            const rowsT = Object.values(byTs).sort((a, b) => (a.ts < b.ts ? -1 : 1));
            const x = rowsT.map((r) => Math.floor(new Date(r.ts).getTime() / 1000));
            chartEl.innerHTML = "";
            if (x.length < 2) { chartEl.innerHTML = '<div class="v2-chart-fallback">Need ≥2 probe cycles to chart the fingerprint.</div>'; }
            else NG.chart(chartEl, {
              type: "line", x, height: 220,
              series: [
                { label: "subscription", values: rowsT.map((r) => r.sub), color: "#7C9BFF" },
                { label: "metered", values: rowsT.map((r) => r.met), color: "#4C8DFF" },
              ],
              fmtY: (v) => (v == null ? "" : Number(v).toFixed(3)),
              fmtX: (e) => new Date(e * 1000).toLocaleString(),
            });
          } catch (_e) { chartEl.innerHTML = '<div class="v2-chart-fallback">No probe history yet.</div>'; }
        }
      }

      // Cross-path comparison — one row per model
      const compareCard = document.getElementById("probe-compare-card");
      if (compareCard) {
        const tpbDelta = (sub, met) => {
          if (sub == null || met == null || !met) return "";
          const d = ((sub - met) / met) * 100;
          if (Math.abs(d) < 1) return "";
          return ` ${d > 0 ? "+" : ""}${d.toFixed(0)}%`;
        };
        NG.DataTable(compareCard, {
          title: "Cross-path comparison", meta: "subscription vs metered · latest cycle",
          countLabel: (n) => `${n} model${n === 1 ? "" : "s"}`,
          searchPlaceholder: "Filter models…",
          defaultSort: { key: "model", dir: "asc" },
          emptyText: "No probe cycle yet — set targets, enable, and Run now.",
          rows: cmpRows,
          rowClass: (r) => (r.mismatch ? "v2-row-bad" : null),
          columns: [
            { key: "model", label: "Model", render: (r) => { const w = NG.el("div", { class: "v2-cell-stack" }); w.appendChild(NG.el("span", { class: "v2-strong" }, shortModelName(r.model))); w.appendChild(NG.el("span", { class: "v2-cell-sub" }, `${r.sub.via || "sub"} · ${r.met.via || "metered"}`)); return w; }, sortValue: (r) => r.model || "" },
            { key: "observed", label: "Observed (sub)", mono: true, render: (r) => r.sub.observed_model || (r.sub.error ? `— (${r.sub.status_code || "err"})` : "—"), sortValue: (r) => r.sub.observed_model || "" },
            { key: "tpb", label: "Tokens/byte", align: "right", render: (r) => { const sub = r.sub.tokens_per_byte, met = r.met.tokens_per_byte; const base = `${sub != null ? sub.toFixed(2) : "—"} / ${met != null ? met.toFixed(2) : "—"}`; const d = tpbDelta(sub, met); const w = NG.el("span", null, base); if (d) w.appendChild(NG.el("span", { class: "v2-chip v2-chip-" + (Math.abs((sub - met) / met) > 0.1 ? "bad" : "neutral"), style: { marginLeft: "6px" } }, d.trim())); return w; }, sortValue: (r) => r.sub.tokens_per_byte || 0 },
            { key: "quality", label: "Quality", align: "right", render: (r) => `${r.sub.quality_score != null ? r.sub.quality_score : "—"} / ${r.met.quality_score != null ? r.met.quality_score : "—"}`, sortValue: (r) => r.sub.quality_score || 0 },
            { key: "verdict", label: "Verdict", sortable: false, render: (r) => NG.chip(r.mismatch ? "Divergent" : "Match", r.mismatch ? "divergent" : "match") },
          ],
        });
      }
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

  let lastProbeSummary = null;

  function renderProbeRunning(targets) {
    const chips = targets.map((t) => `<span class="probe-target-chip pending"><span class="dot"></span>${esc(shortModelName(t))}</span>`).join("");
    return `<div class="probe-run running">
      <div class="probe-run-head"><span class="v2-spinner"></span> Running probe cycle…</div>
      <div class="probe-run-sub">Probing ${targets.length} target${targets.length === 1 ? "" : "s"} on <b>both</b> the subscription (OAuth) and metered paths, then judging each. This takes a few seconds.</div>
      <div class="probe-run-bar"><div></div></div>
      <div class="probe-run-targets">${chips}</div>
    </div>`;
  }

  function renderProbeResult(ok, targets, summary, err) {
    if (!ok) {
      return `<div class="probe-run bad">
        <div class="probe-run-head" style="color:var(--bad)">✗ Probe run failed</div>
        <div class="probe-run-sub">${esc(err || "unknown error")}${/no targets/i.test(err || "") ? " — add <code>provider/model</code> lines in Config below." : / 401| 403|auth/i.test(err || "") ? " — the OAuth/API credentials in deploy/.env are missing or expired." : ""}</div>
      </div>`;
    }
    const s = summary || { count: targets.length, divergent: 0, alerts: 0, rows: [] };
    const verdictByModel = {};
    (s.rows || []).forEach((r) => { verdictByModel[r.model] = r.mismatch ? "divergent" : "match"; });
    const chips = targets.map((t) => {
      const v = verdictByModel[t] || "match";
      return `<span class="probe-target-chip ${v}"><span class="dot"></span>${esc(shortModelName(t))} · ${v === "divergent" ? "divergent" : "match"}</span>`;
    }).join("");
    const sub = `${s.count} target${s.count === 1 ? "" : "s"} probed · <b style="color:${s.divergent ? "var(--bad)" : "var(--good)"}">${s.divergent} divergent</b> · ${s.alerts} open alert${s.alerts === 1 ? "" : "s"}. Detail in the comparison + fingerprint below.`;
    return `<div class="probe-run ok">
      <div class="probe-run-head" style="color:var(--good)">✓ Probe cycle complete</div>
      <div class="probe-run-sub">${sub}</div>
      <div class="probe-run-targets">${chips}</div>
    </div>`;
  }

  async function runProbeNow() {
    // Visible feedback: the header action on the Probe tab, else the config button.
    const headerBtn = document.getElementById("header-action");
    const cfgBtn = document.getElementById("probe-run-now");
    const btn = (headerBtn && !headerBtn.hidden) ? headerBtn : cfgBtn;
    const orig = btn ? btn.textContent : "";
    const statusEl = document.getElementById("probe-run-status");
    const targets = (document.getElementById("probe-targets")?.value || "")
      .split("\n").map((s) => s.trim()).filter(Boolean);
    if (statusEl) { statusEl.hidden = false; statusEl.innerHTML = renderProbeRunning(targets); statusEl.scrollIntoView({ block: "nearest" }); }
    if (btn) { btn.disabled = true; btn.textContent = "⏳ Running…"; }
    try {
      await saveProbeConfig();
      const t = getToken();
      const res = await fetch("/v1/probe/run", {
        method: "POST", headers: { Authorization: "Bearer " + t, "Content-Type": "application/json" },
        body: "{}",
      });
      if (!res.ok) {
        let detail = "";
        try { detail = (await res.json()).detail || ""; } catch (_e) { /* ignore */ }
        throw new Error(detail || ("http_" + res.status));
      }
      if (btn) btn.textContent = "✓ Done";
      await loadProbe();                    // refreshes comparison/fingerprint + lastProbeSummary
      if (statusEl) statusEl.innerHTML = renderProbeResult(true, targets, lastProbeSummary);
      setTimeout(() => { if (btn) { btn.textContent = orig; btn.disabled = false; } }, 1500);
    } catch (e) {
      if (btn) btn.textContent = "✗ Failed";
      if (statusEl) { statusEl.hidden = false; statusEl.innerHTML = renderProbeResult(false, targets, null, e.message || String(e)); }
      setTimeout(() => { if (btn) { btn.textContent = orig; btn.disabled = false; } }, 3000);
    }
  }

  document.getElementById("probe-reload")?.addEventListener("click", loadProbe);
  document.getElementById("probe-save")?.addEventListener("click", saveProbeConfig);
  document.getElementById("probe-run-now")?.addEventListener("click", runProbeNow);

  function renderCostSummary(s) {
    const tokens = (s.total_prompt_tokens || 0) + (s.total_completion_tokens || 0);
    const avg = s.total_cost_usd && s.total_calls ? s.total_cost_usd / s.total_calls : null;
    const emptySub = [];
    if (s.empty_count != null) emptySub.push(`${s.empty_count} empty`);
    if (s.rate_limited_count) emptySub.push(`${s.rate_limited_count}× 429`);
    const kpis = document.getElementById("cost-kpis");
    if (kpis) {
      NG.statRow(kpis, [
        NG.statCard({ label: "Metered spend", value: usd(s.total_cost_usd), sub: `${(s.total_calls ?? 0).toLocaleString()} calls` }),
        NG.statCard({ label: "Subscription saved", value: usd(s.subscription_savings_usd), sub: "notional · covered by subscription", highlight: true }),
        NG.statCard({ label: "Requests", value: (s.total_calls ?? 0).toLocaleString(), sub: emptySub.join(" · ") || "no anomalies" }),
        NG.statCard({ label: "Avg / call", value: usd(avg), sub: `${fmtNum(tokens)} tokens` }),
      ]);
    }

    renderCostProviderCard(s.by_provider);
    renderCostModelTable(s.by_model);
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

  const COST_DONUT_COLORS = ["#7C9BFF", "#B98CF0", "#4FC7C3", "#E38FB4", "#8893A4"];
  function renderCostProviderCard(rows) {
    const mount = document.getElementById("cost-provider-card");
    if (!mount) return;
    rows = (rows || []).slice().sort((a, b) => (b.cost_usd || 0) - (a.cost_usd || 0));
    const total = rows.reduce((s, r) => s + (r.cost_usd || 0), 0);
    const segs = rows.map((r, i) => ({ label: r.key || "—", value: r.cost_usd || 0, color: COST_DONUT_COLORS[i % COST_DONUT_COLORS.length] }));
    const split = NG.el("div", { class: "v2-provider-split" });
    if (!rows.length || total <= 0) {
      split.appendChild(NG.el("div", { class: "hint" }, "No spend in this window."));
    } else {
      split.appendChild(NG.donut({ segments: segs, centerValue: usd(total), centerLabel: "total", size: 150, stroke: 22 }));
      split.appendChild(NG.legend(segs.map((sg) => ({
        label: sg.label, color: sg.color,
        value: total ? Math.round((sg.value / total) * 100) + "%" : "—",
      }))));
    }
    mount.innerHTML = "";
    mount.appendChild(NG.card({ title: "Cost by provider", body: split }));
  }

  function renderCostModelTable(rows) {
    const mount = document.getElementById("cost-model-card");
    if (!mount) return;
    rows = rows || [];
    const table = NG.DataTable(mount, {
      title: "By model",
      countLabel: (n) => `${n} model${n === 1 ? "" : "s"}`,
      search: true,
      searchPlaceholder: "Filter models…",
      defaultSort: { key: "spend", dir: "desc" },
      emptyText: "No spend in this window.",
      rows: rows,
      onRowClick: (r) => jumpToAuditForModel(r.key),
      columns: [
        { key: "key", label: "Model", render: (r) => NG.el("span", { class: "v2-strong" }, r.key || "—"), sortValue: (r) => r.key || "" },
        { key: "provider", label: "Provider", render: (r) => NG.providerTag(r.provider || r.key_provider || "—"), sortValue: (r) => r.provider || "", required: false },
        { key: "spend", label: "Spend", align: "right", render: (r) => usd(r.cost_usd), sortValue: (r) => r.cost_usd || 0 },
        { key: "notional", label: "Notional", align: "right", render: (r) => (r.notional_cost_usd != null ? usd(r.notional_cost_usd) : "—"), sortValue: (r) => r.notional_cost_usd || 0 },
        { key: "calls", label: "Calls", align: "right", render: (r) => (r.calls || 0).toLocaleString(), sortValue: (r) => r.calls || 0 },
        { key: "percall", label: "$/call", align: "right", render: (r) => { const c = r.calls || 1; const d = (r.cost_usd || 0) / c; return d > 0 ? "$" + d.toFixed(4) : "—"; }, sortValue: (r) => (r.cost_usd || 0) / (r.calls || 1) },
        { key: "tokens", label: "Tokens in / out", align: "right", render: (r) => `${fmtNum(r.prompt_tokens || 0)} / ${fmtNum(r.completion_tokens || 0)}`, sortValue: (r) => (r.prompt_tokens || 0) + (r.completion_tokens || 0) },
        { key: "latency", label: "Avg latency", align: "right", render: (r) => (r.avg_latency_ms != null ? Math.round(r.avg_latency_ms).toLocaleString() + " ms" : "—"), sortValue: (r) => r.avg_latency_ms || 0 },
        { key: "empty", label: "Empty", align: "right", render: (r) => { const c = r.calls || 1; const p = ((r.empty_count || 0) / c * 100).toFixed(0); return `${r.empty_count || 0} (${p}%)`; }, sortValue: (r) => r.empty_count || 0 },
      ],
    });
    return table;
  }

  function jumpToAuditForModel(model) {
    if (!model) return;
    activateTab("audit");
    const hintEl = document.querySelector("#tab-audit .hint");
    if (hintEl) {
      const orig = hintEl.textContent;
      hintEl.textContent = `Filtered hint: showing recent calls. Look for model=${model}.`;
      setTimeout(() => { hintEl.textContent = orig; }, 4000);
    }
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
    const mount = document.getElementById("cost-spend-card");
    if (!mount) return;
    const series = (ts && ts.series) || [];

    // Build a unified x-axis (all unique bucket timestamps, sorted → epoch s).
    const allTs = new Set();
    series.forEach((s) => (s.points || []).forEach((p) => allTs.add(p.ts)));
    const labels = Array.from(allTs).sort();
    const x = labels.map((iso) => Math.floor(new Date(iso).getTime() / 1000));
    const chartSeries = series.map((s, i) => {
      const byTs = Object.fromEntries((s.points || []).map((p) => [p.ts, p.cost_usd]));
      return {
        label: s.provider || "—",
        values: labels.map((t) => byTs[t] ?? 0),
        color: COST_DONUT_COLORS[i % COST_DONUT_COLORS.length],
      };
    });

    // Inline legend + chart inside one card.
    const body = NG.el("div");
    const chartEl = NG.el("div", { class: "v2-chart" });
    body.appendChild(chartEl);
    const legendChips = chartSeries.map((s) => {
      const c = NG.el("span", { class: "v2-chart-legend-item" });
      c.appendChild(NG.el("span", { class: "v2-legend-dot", style: { background: s.color } }));
      c.appendChild(NG.el("span", null, s.label));
      return c;
    });
    mount.innerHTML = "";
    mount.appendChild(NG.card({ title: "Spend over time", actions: legendChips, body }));

    if (!x.length) { chartEl.innerHTML = '<div class="v2-chart-fallback">No spend in this window.</div>'; return; }
    NG.chart(chartEl, {
      type: "area",
      x,
      series: chartSeries,
      height: 236,
      fmtY: (v) => "$" + Number(v).toFixed(Number(v) < 1 ? 2 : 0),
      fmtX: (epoch) => shortLabel(new Date(epoch * 1000).toISOString()),
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

  async function loadExternalStatus() {
    // Official provider status pages + OpenRouter per-model availability.
    const spEl = document.getElementById("health-statuspages");
    const orEl = document.getElementById("health-ormodels");
    if (!spEl || !getToken()) return;
    try {
      const [sp, or_] = await Promise.all([
        api("/v1/health/statuspages"),
        api("/v1/health/openrouter-models"),
      ]);
      const indClr = (i) => i === "none" ? "var(--good)" : i === "minor" ? "var(--warn)" : i === "unknown" ? "var(--text-dim)" : "var(--bad)";
      spEl.innerHTML = "";
      spEl.appendChild(NG.card({
        title: "Official provider status",
        meta: "status.claude.com & status.openai.com · refreshed every 5 min",
        body: NG.el("div", {
          html: (sp.pages || []).map((p) => `
            <div class="mh-signal" style="cursor:default">
              <span class="mh-chip" style="color:${indClr(p.indicator)};border-color:${indClr(p.indicator)}">${esc(p.provider.toUpperCase())}</span>
              <span class="mh-signal-text">${esc(p.description || p.error || "unreachable")}</span>
              <span class="hint">${(p.degraded || []).map((c) => esc(c.name + " (" + c.status + ")")).join(" · ") || "all components operational"}</span>
              <a class="mh-signal-action" href="${esc(p.url)}" target="_blank" rel="noopener">open →</a>
            </div>`).join(""),
        }),
      }));
      if (orEl) {
        const upClr = (u) => u == null ? "var(--text-dim)" : u >= 99 ? "var(--good)" : u >= 95 ? "var(--warn)" : "var(--bad)";
        const rows = (or_.models || []).map((m) => `<tr>
            <td><b>${esc(m.model)}</b></td>
            <td class="num">${m.listed === false ? '<span style="color:var(--bad)">delisted</span>'
              : m.best_uptime_30m != null ? `<span style="color:${upClr(m.best_uptime_30m)}">${m.best_uptime_30m}%</span>` : "—"}</td>
            <td class="num">${m.providers ?? "—"}</td>
            <td class="num">${m.deranked ? `<span style="color:var(--warn)">${m.deranked}</span>` : "0"}</td>
          </tr>`).join("");
        orEl.innerHTML = "";
        orEl.appendChild(NG.card({
          title: "OpenRouter model availability",
          meta: "models this gateway used in the last 7d · uptime over the last 30 min per serving provider — 'listed but unusable' shows up here",
          body: NG.el("div", {
            html: `<table class="in-table"><thead><tr><th>model</th><th class="num">best uptime 30m</th><th class="num">providers</th><th class="num">deranked</th></tr></thead><tbody>${rows}</tbody></table>`,
          }),
        }));
      }
    } catch (e) {
      spEl.innerHTML = `<p class="hint" style="color:var(--bad)">status fetch failed: ${esc(e.message || e)}</p>`;
    }
  }

  async function loadModels() {
    if (!getToken()) return;
    loadExternalStatus();  // fire-and-forget — cards fill in as feeds answer
    try {
      const r = await api("/v1/models");
      // Models tab shows the JSON.
      const mj = document.getElementById("models-json");
      if (mj) mj.textContent = JSON.stringify(r, null, 2);
      // Provider Health tab shows a kit DataTable.
      const healthCard = document.getElementById("health-card");
      if (healthCard) {
        const rows = (r.data || []).filter((m) => m.id !== "auto");
        NG.DataTable(healthCard, {
          title: "Provider health",
          countLabel: (n) => `${n} model${n === 1 ? "" : "s"}`,
          searchPlaceholder: "Filter models…",
          defaultSort: { key: "provider", dir: "asc" },
          emptyText: "No models reported.",
          rows,
          rowClass: (m) => (m.nautgate_unhealthy ? "v2-row-bad" : null),
          columns: [
            { key: "provider", label: "Provider", render: (m) => NG.providerTag(m.nautgate_provider), sortValue: (m) => m.nautgate_provider || "" },
            { key: "model", label: "Model", render: (m) => NG.el("span", { class: "v2-strong" }, m.id), sortValue: (m) => m.id || "" },
            { key: "tiers", label: "Tiers", render: (m) => (m.nautgate_tiers || []).join(", ") || "—", sortValue: (m) => (m.nautgate_tiers || []).join(",") },
            { key: "status", label: "Status", sortable: false, render: (m) => NG.chip(m.nautgate_unhealthy ? "Unhealthy" : "Healthy", m.nautgate_unhealthy ? "bad" : "good") },
          ],
        });
      }
    } catch (e) {
      /* swallow */
    }
  }

  // --- Engram-OSS ingest (Settings → Engram-OSS sub-tab) -----------------
  // (Backend keys remain `sb_ingest` / `sb_memory.py` — UI rename only.)

  document.getElementById("sb-save")?.addEventListener("click", saveSBConfig);
  document.getElementById("sb-test")?.addEventListener("click", testSBConfig);

  // Offline mode — the air-gapped demo switch. Reads/writes the same
  // /v1/config document; the schedulers poll it each tick so flipping it takes
  // effect without a restart.
  document.getElementById("offline-save")?.addEventListener("click", saveOfflineMode);

  async function saveOfflineMode() {
    const stateEl = document.getElementById("offline-state");
    const on = document.getElementById("offline-mode").checked;
    stateEl.textContent = "saving…";
    try {
      const res = await fetch("/v1/config", {
        method: "PUT",
        headers: { Authorization: "Bearer " + getToken(), "Content-Type": "application/json" },
        body: JSON.stringify({ offline: on }),
      });
      if (!res.ok) throw new Error("http_" + res.status);
      stateEl.textContent = on
        ? "✓ offline — outbound calls stop within a minute"
        : "✓ online — provider monitoring resumed";
      setTimeout(() => { stateEl.textContent = ""; }, 5000);
    } catch (e) {
      stateEl.textContent = "✗ save failed: " + (e.message || e);
    }
  }

  async function loadSBConfig() {
    try {
      const cfg = await api("/v1/config");
      const offlineEl = document.getElementById("offline-mode");
      if (offlineEl) offlineEl.checked = !!(cfg && cfg.offline);
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
    if (!getToken()) return;
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

      // Heatmap — failure rate by model × complexity bucket
      const buckets = ["0_2", "2_4", "4_6", "6_8", "8_10"];
      const heatCard = document.getElementById("q-heatmap-card");
      if (heatCard) {
        const hmRows = (s.heatmap || []).map((r) => ({
          label: r.model,
          cells: buckets.map((b) => {
            const v = r.buckets ? r.buckets[b] : null;
            const c = r.counts ? r.counts[b] : 0;
            return { value: v, display: v == null ? "—" : (v * 100).toFixed(0) + "%", title: c ? `${c} evals` : "no evals" };
          }),
        }));
        heatCard.innerHTML = "";
        const body = hmRows.length
          ? NG.heatmap({ rowLabel: "Model", colLabels: ["0–2", "2–4", "4–6", "6–8", "8–10"], rows: hmRows })
          : NG.el("div", { class: "hint" }, "No evaluations yet — they'll start landing within seconds of your next LLM call.");
        heatCard.appendChild(NG.card({ title: "Failure rate by model & task complexity", meta: "judged on classified_score 0–10", body }));
      }

      // Failure modes — share of judged calls tagged (percentages, colored)
      const fmCard = document.getElementById("q-failuremodes-card");
      if (fmCard) {
        const pctCell = (count, evals) => {
          if (!evals) return NG.el("span", { class: "hint" }, "—");
          const r = count / evals;
          const color = r > 0.25 ? "var(--bad)" : r > 0.12 ? "var(--warn)" : r > 0 ? "#C2CAD6" : "var(--text-dim)";
          return NG.el("span", { style: { color } }, (r * 100).toFixed(0) + "%");
        };
        const fmCol = (key, label) => ({ key, label, align: "right", render: (r) => pctCell(r[key] || 0, r.evaluations || 0), sortValue: (r) => (r.evaluations ? (r[key] || 0) / r.evaluations : 0) });
        NG.DataTable(fmCard, {
          title: "Failure modes by model", meta: "share of judged calls tagged",
          countLabel: (n) => `${n} model${n === 1 ? "" : "s"}`,
          searchPlaceholder: "Filter models…",
          defaultSort: { key: "over_thinking", dir: "desc" },
          emptyText: "No failure-mode data yet.",
          rows: s.failure_modes || [],
          columns: [
            { key: "model", label: "Model", render: (r) => NG.el("span", { class: "v2-strong" }, r.model), sortValue: (r) => r.model || "" },
            fmCol("over_thinking", "Over-thinking"),
            fmCol("off_task", "Off-task"),
            fmCol("looped", "Looped"),
            fmCol("hallucination", "Hallucination"),
            fmCol("partial_answer", "Partial"),
          ],
        });
      }

      // Worst recent calls
      const worstCard = document.getElementById("q-worst-card");
      if (worstCard) {
        NG.DataTable(worstCard, {
          title: "Worst recent calls",
          countLabel: (n) => `${n} call${n === 1 ? "" : "s"}`,
          searchPlaceholder: "Filter…",
          defaultSort: { key: "completion", dir: "asc" },
          emptyText: "No failures detected in this window.",
          rows: s.worst_recent || [],
          onRowClick: (r) => { activateTab("audit"); setTimeout(() => { auditExpandedId = null; toggleAuditDetail(r.decision_id); }, 200); },
          columns: [
            { key: "ts", label: "Time", render: (r) => tsShort(r.ts), sortValue: (r) => r.ts || "" },
            { key: "model", label: "Model", render: (r) => r.model || "—", sortValue: (r) => r.model || "" },
            { key: "tier", label: "Tier", render: (r) => NG.tierPill(r.tier || "—"), sortValue: (r) => r.tier || "" },
            { key: "completion", label: "Completion", align: "right", render: (r) => (r.completion != null ? r.completion.toFixed(1) : "—"), sortValue: (r) => r.completion == null ? 99 : r.completion },
            { key: "tags", label: "Tags", sortable: false, render: (r) => {
                const w = NG.el("span", { class: "sc-incidents" });
                (r.failure_tags || []).forEach((tg) => w.appendChild(NG.el("span", { class: "failure-tag failure-tag-" + tg }, tg.replace(/_/g, " "))));
                return w.children.length ? w : "—";
              } },
            { key: "coach", label: "Coach note", sortable: false, render: (r) => r.coach_notes || "" },
          ],
        });
      }

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
    if (!getToken()) return;
    const mount = document.getElementById("behavior-permodel-card");
    if (!mount) return;
    try {
      const r = await api("/v1/behavior/per-model?hours=" + behaviorWindowH);
      const rows = r.data || [];
      // rate cell: higher = worse → amber/red tint
      const rate = (v) => {
        if (v == null) return NG.el("span", { class: "hint" }, "—");
        const color = v > 0.25 ? "var(--bad)" : v > 0.1 ? "var(--warn)" : "#C2CAD6";
        return NG.el("span", { style: { color } }, (v * 100).toFixed(1) + "%");
      };
      const score = (v) => (v == null ? NG.el("span", { class: "hint" }, "—") : v.toFixed(2));
      NG.DataTable(mount, {
        title: "Per-model behavioral profile",
        countLabel: (n) => `${n} model${n === 1 ? "" : "s"}`,
        searchPlaceholder: "Filter models…",
        defaultSort: { key: "evals", dir: "desc" },
        emptyText: "No quality evals in window.",
        rows,
        columns: [
          { key: "model", label: "Model", render: (m) => NG.el("span", { class: "v2-strong" }, m.model || "—"), sortValue: (m) => m.model || "" },
          { key: "evals", label: "Evals", align: "right", render: (m) => m.evals || 0, sortValue: (m) => m.evals || 0 },
          { key: "ac", label: "Action compliance", align: "right", render: (m) => score(m.avg_action_compliance), sortValue: (m) => m.avg_action_compliance || 0 },
          { key: "tc", label: "Task completion", align: "right", render: (m) => score(m.avg_task_completion), sortValue: (m) => m.avg_task_completion || 0 },
          { key: "eff", label: "Efficiency", align: "right", render: (m) => score(m.avg_reasoning_efficiency), sortValue: (m) => m.avg_reasoning_efficiency || 0 },
          { key: "dur", label: "Duration", align: "right", render: (m) => fmtMs(m.avg_duration_ms), sortValue: (m) => m.avg_duration_ms || 0 },
          { key: "skip", label: "Skipped doc", align: "right", render: (m) => rate(m.skipped_doc_rate), sortValue: (m) => m.skipped_doc_rate || 0 },
          { key: "edit", label: "Edit w/o read", align: "right", render: (m) => rate(m.edit_without_read_rate), sortValue: (m) => m.edit_without_read_rate || 0 },
          { key: "prem", label: "Premature action", align: "right", render: (m) => rate(m.premature_action_rate), sortValue: (m) => m.premature_action_rate || 0 },
          { key: "loop", label: "Retry loop", align: "right", render: (m) => rate(m.retry_loop_rate), sortValue: (m) => m.retry_loop_rate || 0 },
        ],
      });
      return;
    } catch (e) {
      mount.innerHTML = `<div class="v2-card"><p class="hint" style="color:#ff5c5c">load failed: ${esc(e.message || String(e))}</p></div>`;
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

  // Session archive window (dashboard-local). Populate + persist on change.
  const archiveInput = document.getElementById("session-archive-days");
  if (archiveInput) {
    archiveInput.value = String(archiveDays());
    archiveInput.addEventListener("change", () => {
      const v = parseInt(archiveInput.value, 10);
      if (Number.isFinite(v) && v > 0) {
        localStorage.setItem("ng_session_archive_days", String(v));
        sessionPage = 0;
        renderSessions();
      } else {
        archiveInput.value = String(archiveDays());
      }
    });
  }

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
    // NautGate API keys (ng_…) — full management.
    loadKeys();
    // Provider keys: read-only env hint (different concept).
    const ks = document.getElementById("keys-status");
    if (ks) ks.textContent =
      [
        "Provider keys live as env vars on the gateway:",
        "  ANTHROPIC_API_KEY   OPENAI_API_KEY   GEMINI_API_KEY",
        "  OPENROUTER_API_KEY  LMSTUDIO_BASE_URL",
        "",
        "Set them in deploy/.env and `docker compose up -d` to rotate.",
      ].join("\n");
  }

  // --- NautGate API key management (Settings → Keys) --------------------
  async function populateKeyModelPicker() {
    const sel = document.getElementById("key-model");
    if (!sel || sel.dataset.loaded) return;
    try {
      const r = await api("/v1/models");
      (r.data || []).filter((m) => m.id && m.id !== "auto").forEach((m) => {
        sel.appendChild(NG.el("option", { value: m.id }, m.id));
      });
      sel.dataset.loaded = "1";
    } catch (_e) { /* picker stays auto-only */ }
  }

  async function loadKeys() {
    const mount = document.getElementById("keys-card");
    if (!mount || !getToken()) return;
    populateKeyModelPicker();
    try {
      const r = await api("/v1/keys");
      const rows = r.keys || [];
      NG.DataTable(mount, {
        title: "Keys", countLabel: (n) => `${n} key${n === 1 ? "" : "s"}`,
        searchPlaceholder: "Filter…",
        defaultSort: { key: "created", dir: "desc" },
        emptyText: "No keys yet — create one above.",
        rows,
        rowClass: (k) => (k.status === "revoked" || k.status === "expired" ? "v2-row-bad" : null),
        columns: [
          { key: "name", label: "Name", render: (k) => NG.el("span", { class: "v2-strong" }, k.name || "—"), sortValue: (k) => k.name || "" },
          { key: "agent_id", label: "Agent", render: (k) => k.agent_id || "—", sortValue: (k) => k.agent_id || "" },
          { key: "pinned", label: "Model", sortable: false, render: (k) => k.override_model ? NG.el("span", { class: "audit-tool-chip", title: "pinned model" }, shortModelName(k.override_model)) : NG.el("span", { class: "hint" }, "auto") },
          { key: "created", label: "Created", render: (k) => (k.created_at ? fmtAge(k.created_at) : "—"), sortValue: (k) => k.created_at || "" },
          { key: "last_used", label: "Last used", render: (k) => (k.last_used_at ? fmtAge(k.last_used_at) : "never"), sortValue: (k) => k.last_used_at || "" },
          { key: "expires", label: "Expires", render: (k) => (k.expires_at ? fmtAge(k.expires_at) : "never"), sortValue: (k) => k.expires_at || "" },
          { key: "status", label: "Status", sortable: false, render: (k) => NG.chip(k.status, k.status === "active" ? "good" : k.status === "expired" ? "warn" : "bad") },
          { key: "act", label: "", sortable: false, render: (k) => {
              if (k.status !== "active") return "";
              const b = NG.el("button", { class: "v2-pg-btn" }, "Revoke");
              b.addEventListener("click", (e) => { e.stopPropagation(); revokeKey(k.id, k.name); });
              return b;
            } },
        ],
      });
    } catch (e) {
      mount.innerHTML = `<div class="v2-card"><p class="hint">load failed: ${esc(e.message || e)}</p></div>`;
    }
  }

  async function createKey() {
    const name = document.getElementById("key-name").value.trim();
    const agent = document.getElementById("key-agent").value.trim();
    const ttl = Number(document.getElementById("key-ttl").value) || 30;
    const overrideModel = (document.getElementById("key-model")?.value || "").trim();
    const stateEl = document.getElementById("key-create-state");
    const out = document.getElementById("key-created");
    if (!name || !agent) { if (stateEl) stateEl.textContent = "name + agent id required"; return; }
    if (stateEl) stateEl.textContent = "creating…";
    try {
      const t = getToken();
      const res = await fetch("/v1/keys", {
        method: "POST", headers: { Authorization: "Bearer " + t, "Content-Type": "application/json" },
        body: JSON.stringify({ name, agent_id: agent, ttl_days: ttl, override_model: overrideModel || null }),
      });
      if (!res.ok) { let d = ""; try { d = (await res.json()).detail || ""; } catch (_e) {} throw new Error(d || ("http_" + res.status)); }
      const k = await res.json();
      if (stateEl) stateEl.textContent = "";
      if (out) {
        out.hidden = false;
        out.innerHTML = "";
        out.appendChild(NG.el("div", { class: "v2-card-meta", style: { marginBottom: "8px" } }, `Key “${name}” created — copy it now, it won't be shown again:`));
        const row = NG.el("div", { class: "key-created-row" });
        const code = NG.el("code", null, k.token);
        const copy = NG.el("button", { class: "v2-pg-btn" }, "Copy");
        copy.addEventListener("click", () => { navigator.clipboard?.writeText(k.token); copy.textContent = "Copied ✓"; setTimeout(() => copy.textContent = "Copy", 1500); });
        row.appendChild(code); row.appendChild(copy);
        out.appendChild(row);
        out.appendChild(NG.el("div", { class: "key-created-warn" }, k.expires_at ? `Expires ${new Date(k.expires_at).toLocaleString()}.` : "No expiry."));
        // NAUTGATE-3: pinned key → paste-ready alias snippet. Launch Claude Code
        // with these env vars and every request is served by the pinned model,
        // whatever model name Claude Code's picker sends.
        if (k.override_model) {
          const base = location.origin;
          const snippet = `ANTHROPIC_BASE_URL=${base} ANTHROPIC_API_KEY=${k.token} claude`;
          out.appendChild(NG.el("div", { class: "v2-card-meta", style: { margin: "10px 0 4px" } },
            `Pinned to ${shortModelName(k.override_model)} — launch Claude Code with this alias:`));
          const arow = NG.el("div", { class: "key-created-row" });
          const acode = NG.el("code", null, snippet);
          const acopy = NG.el("button", { class: "v2-pg-btn" }, "Copy");
          acopy.addEventListener("click", () => { navigator.clipboard?.writeText(snippet); acopy.textContent = "Copied ✓"; setTimeout(() => acopy.textContent = "Copy", 1500); });
          arow.appendChild(acode); arow.appendChild(acopy);
          out.appendChild(arow);
        }
      }
      document.getElementById("key-name").value = "";
      loadKeys();
    } catch (e) {
      if (stateEl) stateEl.textContent = "failed: " + (e.message || e);
    }
  }

  async function revokeKey(id, name) {
    const stateEl = document.getElementById("key-create-state");
    try {
      const t = getToken();
      const res = await fetch("/v1/keys/" + encodeURIComponent(id) + "/revoke", {
        method: "POST", headers: { Authorization: "Bearer " + t },
      });
      if (!res.ok) throw new Error("http_" + res.status);
      if (stateEl) stateEl.textContent = `revoked “${name || id}”`;
      loadKeys();
    } catch (e) {
      if (stateEl) stateEl.textContent = "revoke failed: " + (e.message || e);
    }
  }
  document.getElementById("key-create")?.addEventListener("click", createKey);

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

  // First-run onboarding: a fresh browser has no token, so the dashboard can't
  // authenticate and every panel is empty. Show a welcome overlay that helps the
  // operator paste the first-run key NautGate printed to its log. It only ever
  // *validates and saves an existing* key (no minting, no new endpoint), so it
  // adds no attack surface — the key is retrievable only by whoever can read the
  // container log, i.e. whoever owns the box.
  (function firstRunOnboarding() {
    const overlay = document.getElementById("firstrun");
    if (!overlay) return;
    const show = () => { overlay.hidden = !!getToken(); };
    const input = document.getElementById("firstrun-token");
    const err = document.getElementById("firstrun-err");
    const activate = async () => {
      err.textContent = "";
      const r = await activateToken(input.value);
      if (!r.ok) { err.textContent = r.error; return; }
      overlay.hidden = true;
    };
    document.getElementById("firstrun-activate")?.addEventListener("click", activate);
    input?.addEventListener("keydown", (e) => { if (e.key === "Enter") activate(); });
    document.getElementById("firstrun-dismiss")?.addEventListener("click", (e) => {
      e.preventDefault(); overlay.hidden = true;
    });
    show();
  })();

  // Auto-discover OAuth-derived agents (claude-oauth-…, codex-…) and merge
  // them into the session picker so they show up without manual setup.
  // Runs once on load + every 60s thereafter so new logins appear within
  // a minute of their first request.
  discoverAgents();
  setInterval(discoverAgents, 60_000);
  // Start notification poller — runs every 60s while the tab is open.
  loadNotifications();
  setInterval(loadNotifications, 60_000);

  // Global chrome pollers (header status pill, sidebar live dots/badges).
  // These run regardless of active tab so trouble is visible everywhere.
  loadGlobalStatus();
  setInterval(loadGlobalStatus, 60_000);
  loadDriftBadge();
  setInterval(loadDriftBadge, 60_000);

  // ⌘K / Ctrl-K focuses the header search.
  const searchInput = document.getElementById("global-search-input");
  document.getElementById("global-search")?.addEventListener("click", () => searchInput?.focus());
  document.addEventListener("keydown", (ev) => {
    if ((ev.metaKey || ev.ctrlKey) && (ev.key === "k" || ev.key === "K")) {
      ev.preventDefault();
      searchInput?.focus();
    }
  });

  // Help & Ask — slide-in panel is wired in Phase 4. For now the entry
  // points are live but the panel is a no-op placeholder.
  function openHelp() {
    // Phase 4: the @48nauts/help-module is React; wiring it into this vanilla
    // dashboard is deferred. Give visible feedback so the click isn't silent.
    searchInput?.blur();
    let toast = document.getElementById("ng-help-toast");
    if (!toast) {
      toast = document.createElement("div");
      toast.id = "ng-help-toast";
      toast.className = "ng-toast";
      document.body.appendChild(toast);
    }
    toast.textContent = "Help & Ask — coming soon (chat assistant).";
    toast.classList.add("show");
    clearTimeout(openHelp._t);
    openHelp._t = setTimeout(() => toast.classList.remove("show"), 2600);
  }
  document.getElementById("nav-help")?.addEventListener("click", (ev) => { ev.preventDefault(); openHelp(); });
  document.getElementById("header-help-btn")?.addEventListener("click", (ev) => { ev.preventDefault(); openHelp(); });
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
