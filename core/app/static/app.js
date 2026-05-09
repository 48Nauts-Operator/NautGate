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
    // Order: explicit user-saved token in localStorage > server-injected meta token.
    const stored = localStorage.getItem(TOKEN_KEY);
    if (stored) return stored;
    const meta = document.querySelector('meta[name="nautgate-token"]');
    return meta ? meta.getAttribute("content") || "" : "";
  }
  function setToken(t) {
    if (t) localStorage.setItem(TOKEN_KEY, t);
    else localStorage.removeItem(TOKEN_KEY);
    renderAuth();
  }
  function renderAuth() {
    const t = getToken();
    const fromMeta =
      !localStorage.getItem(TOKEN_KEY) &&
      !!document.querySelector('meta[name="nautgate-token"]');
    if (t) {
      authState.textContent = fromMeta ? "auto" : "saved";
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
    else if (activeTab === "cost") loadCost();
    else if (activeTab === "privacy") loadPrivacy();
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
})();
