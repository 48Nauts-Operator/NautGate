// NautGate dashboard — vanilla JS, no build step.
// Hits /v1/stats, /v1/decisions/recent, /v1/models, /v1/profile with the bearer
// token stored in localStorage. Tab routing via URL hash.

(() => {
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
    const active = getActiveSession();
    if (active) return active.token;
    // Fallback: server-injected meta token (single-token install).
    const meta = document.querySelector('meta[name="nautgate-token"]');
    return meta ? meta.getAttribute("content") || "" : "";
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

  function renderSessions() {
    const list = document.getElementById("sessions-list");
    if (!list) return;
    const sessions = loadSessions();
    const activeId = getActiveSessionId() || (sessions[0]?.id ?? "");

    if (!sessions.length) {
      list.innerHTML = '<p class="hint">No saved sessions. Add one below — paste a bearer token (ng_…) and optionally label it.</p>';
      return;
    }

    list.innerHTML = '<table class="sessions-table"><thead><tr><th></th><th>label</th><th>agent</th><th>token</th><th>last used</th><th></th></tr></thead><tbody>'
      + sessions.map(s => {
        const isActive = s.id === activeId;
        const tail = (s.token || "").slice(-6);
        const labelText = s.label || (s.agent_id || "(unlabeled)");
        const agentText = s.agent_id ? esc(s.agent_id) : '<span class="hint">unknown — click Verify</span>';
        return `
          <tr class="${isActive ? "session-row-active" : ""}">
            <td>${isActive ? '<span class="sess-active-dot" title="active session"></span>' : ''}</td>
            <td><b>${esc(labelText)}</b></td>
            <td>${agentText}</td>
            <td><code>ng_…${esc(tail)}</code></td>
            <td>${fmtAgo(s.last_seen_at)}</td>
            <td>
              ${isActive
                ? '<span class="hint">active</span>'
                : `<button data-sess-activate="${esc(s.id)}">Activate</button>`}
              <button data-sess-verify="${esc(s.id)}" class="ghost">Verify</button>
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

  document.querySelectorAll("nav a").forEach((a) => {
    a.addEventListener("click", () => {
      const tab = a.dataset.tab;
      activateTab(tab);
    });
  });

  function activateTab(name) {
    activeTab = name;
    document.querySelectorAll("nav a").forEach((a) =>
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
    }
  }

  function refreshActive() {
    if (activeTab === "overview") loadOverview();
    else if (activeTab === "audit") loadAudit();
    else if (activeTab === "cost") loadCost();
    else if (activeTab === "privacy") loadPrivacy();
    else if (activeTab === "decisions") loadDecisions();
    else if (activeTab === "scorecard") loadScorecard();
    else if (activeTab === "drift") loadDrift();
    else if (activeTab === "health" || activeTab === "models") loadModels();
    else if (activeTab === "settings") loadSettings();
  }

  // --- Overview -----------------------------------------------------------

  async function loadOverview() {
    // Sessions section gets re-rendered every Overview load so the
    // last-used timestamps stay fresh as the active session makes calls.
    renderSessions();
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
      const d = await api("/v1/decisions/" + encodeURIComponent(decisionId));
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
    if (!getToken()) return;
    try {
      const r = await api("/v1/decisions/recent?limit=50");
      renderAudit(r.data || []);
    } catch (e) {
      /* swallow */
    }
  }

  function renderAudit(rows) {
    const list = document.getElementById("audit-list");
    if (!rows.length) {
      list.innerHTML = '<p class="hint">No requests yet. Send one and it\'ll appear here within 5s.</p>';
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
              <div class="audit-cost">${cost}</div>
              <div>${latency} · ${tsShort(r.ts)}</div>
            </div>
          </div>
          <div class="audit-detail" id="audit-detail-${esc(r.decision_id)}"></div>`;
      })
      .join("");

    document.querySelectorAll(".audit-row").forEach((row) => {
      row.addEventListener("click", () => toggleAuditDetail(row.dataset.decision));
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
      const d = await api("/v1/decisions/" + encodeURIComponent(decisionId));
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

  document.querySelectorAll(".window-buttons button").forEach((b) => {
    b.addEventListener("click", () => {
      costWindow = { hours: Number(b.dataset.window), bucket: b.dataset.bucket };
      document
        .querySelectorAll(".window-buttons button")
        .forEach((x) => x.classList.toggle("active", x === b));
      loadCost();
    });
  });

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
    return `
      <div class="dr-alert dr-alert-${esc(a.direction)}">
        <div class="dr-alert-head">${arrow} ${headline}</div>
        <div class="dr-alert-detail">${detail}</div>
      </div>`;
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

  async function loadCost() {
    if (!getToken()) return;
    try {
      const [summary, ts] = await Promise.all([
        api(`/v1/cost/summary?hours=${costWindow.hours}`),
        api(
          `/v1/cost/timeseries?hours=${costWindow.hours}&bucket=${costWindow.bucket}`
        ),
      ]);
      renderCostSummary(summary);
      renderCostChart(ts);
    } catch (e) {
      /* swallow; auth chip explains */
    }
  }

  function renderCostSummary(s) {
    document.getElementById("c-total").textContent = usd(s.total_cost_usd);
    document.getElementById("c-calls").textContent = s.total_calls ?? 0;
    const avg =
      s.total_cost_usd && s.total_calls
        ? s.total_cost_usd / s.total_calls
        : null;
    document.getElementById("c-avg").textContent = usd(avg);
    document.getElementById("c-tokens").textContent =
      ((s.total_prompt_tokens || 0) + (s.total_completion_tokens || 0)).toLocaleString();

    fillCostTable("cost-provider", s.by_provider, ["key", "cost_usd", "calls"]);
    fillCostTable("cost-model", s.by_model, ["key", "cost_usd", "calls"]);
    fillCostTierTable("cost-tier", s.by_tier);
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
    const palette = ["#6ea5ff", "#10b981", "#f59e0b", "#ef4444", "#a78bfa", "#22d3ee"];
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
    if (costWindow.bucket === "day") {
      return d.toISOString().substr(5, 5); // MM-DD
    }
    return d.toISOString().substr(11, 5); // HH:MM
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
  function tsShort(ts) {
    if (!ts) return "-";
    const d = new Date(ts);
    return d.toISOString().substr(11, 8);
  }
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

  // Start tab from URL hash, default to overview.
  const initial = (location.hash || "#overview").slice(1);
  if (document.getElementById("tab-" + initial)) {
    activateTab(initial);
  } else {
    activateTab("overview");
  }
  renderAuth();
  renderSessions();

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
