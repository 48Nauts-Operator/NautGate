// NautGate dashboard — vanilla JS, no build step.
// Hits /v1/stats, /v1/decisions/recent, /v1/models, /v1/profile with the bearer
// token stored in localStorage. Tab routing via URL hash.

(() => {
  const TOKEN_KEY = "nautgate.token";
  const REFRESH_MS = 5000;

  let refreshTimer = null;
  let activeTab = "overview";

  // --- Auth ---------------------------------------------------------------

  const tokenInput = document.getElementById("token-input");
  const tokenSave = document.getElementById("token-save");
  const authState = document.getElementById("auth-state");

  function getToken() {
    return localStorage.getItem(TOKEN_KEY) || "";
  }
  function setToken(t) {
    if (t) localStorage.setItem(TOKEN_KEY, t);
    else localStorage.removeItem(TOKEN_KEY);
    renderAuth();
  }
  function renderAuth() {
    const t = getToken();
    if (t) {
      authState.textContent = "saved";
      authState.classList.add("ok");
      authState.classList.remove("bad");
      tokenInput.value = t.slice(0, 12) + "…";
    } else {
      authState.textContent = "no token";
      authState.classList.remove("ok");
      authState.classList.add("bad");
    }
  }

  tokenSave.addEventListener("click", () => {
    const v = tokenInput.value.trim();
    if (v.endsWith("…")) return; // user didn't change it
    setToken(v);
    refreshActive();
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
    // Restart auto-refresh only on the decisions tab (the live one).
    clearInterval(refreshTimer);
    if (name === "decisions") {
      refreshTimer = setInterval(loadDecisions, REFRESH_MS);
    }
  }

  function refreshActive() {
    if (activeTab === "overview") loadOverview();
    else if (activeTab === "decisions") loadDecisions();
    else if (activeTab === "health" || activeTab === "models") loadModels();
    else if (activeTab === "settings") loadSettings();
  }

  // --- Overview -----------------------------------------------------------

  async function loadOverview() {
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
        <tr>
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
          <td>${d.was_empty ? "✓" : ""}</td>
        </tr>`
        )
        .join("");
    } catch (e) {
      /* swallow; auth state explains */
    }
  }

  document.getElementById("dec-reload").addEventListener("click", loadDecisions);

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
})();
