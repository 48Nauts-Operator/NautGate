/* ======================================================================
   NautGate dashboard v2 — component kit
   Vanilla helpers (no framework, no bundler). Exposed on window.NG.
   Consumed by app.js load*() render functions.
   ====================================================================== */
(function () {
  "use strict";

  const NG = {};

  // --- tiny utils -----------------------------------------------------
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  NG.esc = esc;

  // el("div", {class:"x", onclick:fn, dataset:{k:v}}, child, child, "text")
  function el(tag, attrs, ...children) {
    const n = document.createElement(tag);
    if (attrs) {
      for (const [k, v] of Object.entries(attrs)) {
        if (v == null || v === false) continue;
        if (k === "class") n.className = v;
        else if (k === "dataset") Object.assign(n.dataset, v);
        else if (k === "style" && typeof v === "object") Object.assign(n.style, v);
        else if (k.startsWith("on") && typeof v === "function") n.addEventListener(k.slice(2), v);
        else if (k === "html") n.innerHTML = v;
        else n.setAttribute(k, v);
      }
    }
    for (const c of children.flat()) {
      if (c == null || c === false) continue;
      n.appendChild(typeof c === "string" || typeof c === "number" ? document.createTextNode(String(c)) : c);
    }
    return n;
  }
  NG.el = el;

  // --- stat cards -----------------------------------------------------
  // statCard({label, value, delta, deltaDir, sub, highlight})
  function statCard(o) {
    const card = el("div", { class: "v2-stat" + (o.highlight ? " v2-stat-hl" : "") });
    card.appendChild(el("div", { class: "v2-stat-label" }, o.label || ""));
    const valStyle = o.tone === "bad" ? { color: "var(--bad)" } : o.tone === "good" ? { color: "var(--good)" } : null;
    card.appendChild(el("div", { class: "v2-stat-value", style: valStyle }, o.value == null ? "—" : String(o.value)));
    if (o.delta != null && o.delta !== "") {
      const dir = o.deltaDir; // "up" | "down" | "good" | "bad" | null
      let cls = "v2-stat-delta";
      let arrow = "";
      if (dir === "up" || dir === "good") { cls += " up"; arrow = "▲ "; }
      else if (dir === "down" || dir === "bad") { cls += " down"; arrow = "▼ "; }
      card.appendChild(el("div", { class: cls }, arrow + o.delta));
    } else if (o.sub != null) {
      card.appendChild(el("div", { class: "v2-stat-sub" }, o.sub));
    }
    return card;
  }
  NG.statCard = statCard;

  // statRow(mountEl, [cardConfig, ...]) — replaces mount contents
  function statRow(mount, cards) {
    const row = el("div", { class: "v2-stat-row" });
    cards.forEach((c) => row.appendChild(c instanceof Node ? c : statCard(c)));
    mount.innerHTML = "";
    mount.appendChild(row);
    return row;
  }
  NG.statRow = statRow;

  // --- loading spinner ------------------------------------------------
  function spinner(text) {
    const wrap = el("div", { class: "v2-loading" });
    wrap.appendChild(el("span", { class: "v2-spinner" }));
    if (text) wrap.appendChild(el("span", { class: "v2-loading-text" }, text));
    return wrap;
  }
  NG.spinner = spinner;

  // --- generic card ---------------------------------------------------
  // card({title, meta, actions:[Node], body:Node}) → section.v2-card
  function card(o) {
    const c = el("div", { class: "v2-card" });
    if (o.title || o.actions) {
      const head = el("div", { class: "v2-card-head" });
      const titleWrap = el("div", { class: "v2-card-title-wrap" });
      if (o.title) titleWrap.appendChild(el("span", { class: "v2-card-title" }, o.title));
      if (o.meta != null) titleWrap.appendChild(el("span", { class: "v2-card-meta" }, o.meta));
      head.appendChild(titleWrap);
      if (o.actions) {
        const actWrap = el("div", { class: "v2-card-actions" });
        o.actions.forEach((a) => actWrap.appendChild(a));
        head.appendChild(actWrap);
      }
      c.appendChild(head);
    }
    if (o.body) c.appendChild(o.body instanceof Node ? o.body : el("div", { html: o.body }));
    return c;
  }
  NG.card = card;

  // --- badges / chips -------------------------------------------------
  const STATUS_CLS = { up: "up", degraded: "degraded", down: "down", "no-data": "nodata", nodata: "nodata" };
  function statusBadge(status, label, detail) {
    const cls = STATUS_CLS[status] || "nodata";
    const b = el("div", { class: "v2-badge v2-badge-" + cls });
    b.appendChild(el("span", { class: "v2-badge-dot" }));
    b.appendChild(el("span", { class: "v2-badge-label" }, label || status));
    if (detail) b.appendChild(el("span", { class: "v2-badge-detail" }, detail));
    return b;
  }
  NG.statusBadge = statusBadge;

  // chip(text, kind) — kind: good|bad|warn|info|neutral|accent
  const CHIP_ALIAS = {
    match: "good", healthy: "good", reused: "good", resolved: "neutral", up: "good",
    divergent: "bad", demoted: "bad", leaky: "bad", down: "bad", open: "bad",
    watch: "warn", degraded: "warn",
  };
  function chip(text, kind) {
    const k = CHIP_ALIAS[kind] || kind || "neutral";
    return el("span", { class: "v2-chip v2-chip-" + k }, text);
  }
  NG.chip = chip;

  function tierPill(tier) {
    return el("span", { class: "v2-tier" }, tier);
  }
  NG.tierPill = tierPill;

  // provider dot + name inline (for table cells)
  const PROVIDER_COLOR = {
    anthropic: "var(--series-1)", openrouter: "var(--series-2)", openai: "var(--series-4)",
    deepseek: "var(--series-3)", ollama: "var(--text-dim)", lmstudio: "var(--text-dim)",
  };
  function providerTag(name) {
    const key = String(name || "").toLowerCase();
    const color = PROVIDER_COLOR[key] || "var(--text-dim)";
    const w = el("span", { class: "v2-provider" });
    w.appendChild(el("span", { class: "v2-provider-dot", style: { background: color } }));
    w.appendChild(el("span", null, name || "—"));
    return w;
  }
  NG.providerTag = providerTag;

  // --- sparkline (inline SVG) ----------------------------------------
  function sparkline(values, opts) {
    opts = opts || {};
    const w = opts.width || 64, h = opts.height || 22, pad = 2;
    const v = (values || []).filter((x) => x != null && !isNaN(x));
    if (v.length < 2) return el("span", { class: "v2-spark-empty" }, "—");
    const min = Math.min(...v), max = Math.max(...v), span = max - min || 1;
    const step = (w - pad * 2) / (v.length - 1);
    const pts = v.map((y, i) => {
      const x = pad + i * step;
      const yy = h - pad - ((y - min) / span) * (h - pad * 2);
      return `${x.toFixed(1)},${yy.toFixed(1)}`;
    }).join(" ");
    const color = opts.color || "var(--series-1)";
    const ns = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(ns, "svg");
    svg.setAttribute("width", w); svg.setAttribute("height", h);
    svg.setAttribute("viewBox", `0 0 ${w} ${h}`); svg.setAttribute("class", "v2-spark");
    const pl = document.createElementNS(ns, "polyline");
    pl.setAttribute("points", pts); pl.setAttribute("fill", "none");
    pl.setAttribute("stroke", color); pl.setAttribute("stroke-width", "1.5");
    pl.setAttribute("stroke-linecap", "round"); pl.setAttribute("stroke-linejoin", "round");
    svg.appendChild(pl);
    return svg;
  }
  NG.sparkline = sparkline;

  // --- donut (inline SVG) --------------------------------------------
  // donut({segments:[{label,value,color}], centerValue, centerLabel, size})
  function donut(o) {
    const size = o.size || 96, stroke = o.stroke || 14, r = (size - stroke) / 2;
    const cx = size / 2, cy = size / 2, circ = 2 * Math.PI * r;
    const total = o.segments.reduce((s, x) => s + (x.value || 0), 0) || 1;
    const ns = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(ns, "svg");
    svg.setAttribute("width", size); svg.setAttribute("height", size);
    svg.setAttribute("viewBox", `0 0 ${size} ${size}`); svg.setAttribute("class", "v2-donut");
    const track = document.createElementNS(ns, "circle");
    track.setAttribute("cx", cx); track.setAttribute("cy", cy); track.setAttribute("r", r);
    track.setAttribute("fill", "none"); track.setAttribute("stroke", "var(--bg-raised)");
    track.setAttribute("stroke-width", stroke);
    svg.appendChild(track);
    let offset = 0;
    o.segments.forEach((seg) => {
      const frac = (seg.value || 0) / total;
      if (frac <= 0) return;
      const arc = document.createElementNS(ns, "circle");
      arc.setAttribute("cx", cx); arc.setAttribute("cy", cy); arc.setAttribute("r", r);
      arc.setAttribute("fill", "none"); arc.setAttribute("stroke", seg.color);
      arc.setAttribute("stroke-width", stroke);
      arc.setAttribute("stroke-dasharray", `${(frac * circ).toFixed(2)} ${circ.toFixed(2)}`);
      arc.setAttribute("stroke-dashoffset", (-offset * circ).toFixed(2));
      arc.setAttribute("transform", `rotate(-90 ${cx} ${cy})`);
      svg.appendChild(arc);
      offset += frac;
    });
    const wrap = el("div", { class: "v2-donut-wrap" });
    wrap.appendChild(svg);
    if (o.centerValue != null) {
      const center = el("div", { class: "v2-donut-center" });
      center.appendChild(el("div", { class: "v2-donut-value" }, o.centerValue));
      if (o.centerLabel) center.appendChild(el("div", { class: "v2-donut-label" }, o.centerLabel));
      wrap.appendChild(center);
    }
    return wrap;
  }
  NG.donut = donut;

  // legend list to pair with a donut: [{label,value,pct,color}]
  function legend(items) {
    const l = el("div", { class: "v2-legend" });
    items.forEach((it) => {
      const row = el("div", { class: "v2-legend-row" });
      row.appendChild(el("span", { class: "v2-legend-dot", style: { background: it.color } }));
      row.appendChild(el("span", { class: "v2-legend-label" }, it.label));
      if (it.value != null) row.appendChild(el("span", { class: "v2-legend-value" }, it.value));
      l.appendChild(row);
    });
    return l;
  }
  NG.legend = legend;

  // --- uPlot chart wrapper -------------------------------------------
  // chart(el, {type:'area'|'line', x:[...epoch_s], series:[{label,values,color,fill}], opts})
  // Returns the uPlot instance. Handles dark theme + responsive width.
  const SERIES_COLORS = ["#808000", "#4C8DFF", "#3FB950", "#9A6CE0"];
  function chart(mount, cfg) {
    if (!window.uPlot) { mount.innerHTML = '<div class="v2-chart-fallback">uPlot not loaded</div>'; return null; }
    mount.innerHTML = "";
    const isArea = cfg.type !== "line";
    const data = [cfg.x, ...cfg.series.map((s) => s.values)];
    const uSeries = [{}].concat(cfg.series.map((s, i) => {
      const color = s.color || SERIES_COLORS[i % SERIES_COLORS.length];
      return {
        label: s.label, stroke: color, width: 2,
        fill: isArea ? (s.fill || hexToRgba(color, 0.13)) : undefined,
        points: { show: false },
      };
    }));
    const fmtY = cfg.fmtY || ((v) => v);
    const opts = Object.assign({
      width: mount.clientWidth || 600,
      height: cfg.height || 220,
      padding: [12, 8, 4, 8],
      cursor: { y: false, points: { show: true } },
      legend: { show: false },
      scales: { x: { time: cfg.time !== false } },
      axes: [
        axisX(),
        axisY(fmtY),
      ],
      series: uSeries,
    }, cfg.opts || {});
    const u = new window.uPlot(opts, data, mount);
    // responsive
    const ro = new ResizeObserver(() => u.setSize({ width: mount.clientWidth, height: opts.height }));
    ro.observe(mount);
    u._ro = ro;
    if (cfg.tooltip !== false) attachTooltip(u, mount, cfg);
    return u;
  }
  NG.chart = chart;

  function axisX() {
    return {
      stroke: "#5C6675", grid: { stroke: "#1A2029", width: 1 },
      ticks: { stroke: "#1A2029", width: 1 },
      font: "11px ui-sans-serif, system-ui",
      size: 34,
    };
  }
  function axisY(fmtY) {
    return {
      stroke: "#5C6675", grid: { stroke: "#1A2029", width: 1 },
      ticks: { show: false },
      font: "11px ui-sans-serif, system-ui",
      size: 48,
      values: (u, splits) => splits.map(fmtY),
    };
  }
  function hexToRgba(hex, a) {
    const m = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(hex);
    if (!m) return hex;
    return `rgba(${parseInt(m[1], 16)},${parseInt(m[2], 16)},${parseInt(m[3], 16)},${a})`;
  }
  NG.hexToRgba = hexToRgba;

  // minimal crosshair tooltip plugin
  function attachTooltip(u, mount, cfg) {
    const tip = el("div", { class: "v2-chart-tip" });
    tip.style.display = "none";
    mount.style.position = "relative";
    mount.appendChild(tip);
    u.over.addEventListener("mouseleave", () => { tip.style.display = "none"; });
    const origDraw = u.hooks; // no-op; we use setCursor
    u.hooks.setCursor = (u.hooks.setCursor || []);
    // use a periodic read on mousemove via cursor
    u.over.addEventListener("mousemove", () => {
      const idx = u.cursor.idx;
      if (idx == null) { tip.style.display = "none"; return; }
      const xv = u.data[0][idx];
      const rows = cfg.series.map((s, i) => {
        const yv = u.data[i + 1][idx];
        const color = s.color || SERIES_COLORS[i % SERIES_COLORS.length];
        const val = cfg.fmtY ? cfg.fmtY(yv) : yv;
        return `<div class="v2-tip-row"><span class="v2-tip-dot" style="background:${color}"></span>${esc(s.label)}<b>${esc(val)}</b></div>`;
      }).join("");
      const xlabel = cfg.fmtX ? cfg.fmtX(xv) : (cfg.time !== false ? new Date(xv * 1000).toLocaleString() : xv);
      tip.innerHTML = `<div class="v2-tip-x">${esc(xlabel)}</div>${rows}`;
      tip.style.display = "block";
      const left = u.valToPos(xv, "x");
      tip.style.left = Math.min(left + 12, mount.clientWidth - tip.offsetWidth - 8) + "px";
      tip.style.top = "10px";
    });
  }

  // --- ranked horizontal bars + optional threshold line --------------
  // rankedBars({items:[{label,value,color}], max, threshold, fmt})
  function rankedBars(o) {
    const wrap = el("div", { class: "v2-rbars" });
    const max = o.max || Math.max(...o.items.map((i) => i.value), 1);
    o.items.forEach((it) => {
      const row = el("div", { class: "v2-rbar" });
      row.appendChild(el("span", { class: "v2-rbar-label" }, it.label));
      const track = el("div", { class: "v2-rbar-track" });
      const pct = Math.max(0, Math.min(100, (it.value / max) * 100));
      track.appendChild(el("div", { class: "v2-rbar-fill", style: { width: pct + "%", background: it.color || "var(--series-1)" } }));
      if (o.threshold != null) {
        const tpct = Math.max(0, Math.min(100, (o.threshold / max) * 100));
        track.appendChild(el("div", { class: "v2-rbar-thresh", style: { left: tpct + "%" } }));
      }
      row.appendChild(track);
      row.appendChild(el("span", { class: "v2-rbar-value" }, o.fmt ? o.fmt(it.value) : it.value));
      wrap.appendChild(row);
    });
    return wrap;
  }
  NG.rankedBars = rankedBars;

  // --- heatmap (model × bucket grid, green→red) ----------------------
  // heatmap({rowLabel, colLabels:[...], rows:[{label, cells:[{value(0..1)|null, display, title}]}]})
  // value 0 = good (green), 1 = bad (red). null = no data (track color).
  function heatColor(v) {
    if (v == null) return "var(--bg-raised)";
    v = Math.max(0, Math.min(1, v));
    // muted instrument palette: deep green → olive → oxblood (low light/sat)
    const hue = v < 0.5 ? 135 - (v / 0.5) * 90 : 45 - ((v - 0.5) / 0.5) * 45;
    const sat = 38 + v * 22;            // 38%→60%
    const light = 16 + (1 - v) * 6;     // 22%(good)→16%(bad), kept dark
    return `hsl(${hue.toFixed(0)}, ${sat.toFixed(0)}%, ${light.toFixed(0)}%)`;
  }
  // text color that reads on the muted cell
  function heatText(v) {
    if (v == null) return "var(--text-dim)";
    v = Math.max(0, Math.min(1, v));
    const hue = v < 0.5 ? 135 - (v / 0.5) * 90 : 45 - ((v - 0.5) / 0.5) * 45;
    return `hsl(${hue.toFixed(0)}, 70%, 78%)`;
  }
  NG.heatText = heatText;

  // --- vertical bar chart (score columns + threshold line) ------------
  // verticalBars({items:[{label,value,color}], max, threshold, fmt, height})
  function verticalBars(o) {
    const max = o.max || Math.max(...o.items.map((i) => i.value), 1);
    const H = o.height || 170;
    const wrap = el("div", { class: "v2-vbars" });
    const row = el("div", { class: "v2-vbars-row", style: { height: H + "px" } });
    if (o.threshold != null) {
      const top = (1 - o.threshold / max) * 100;
      const line = el("div", { class: "v2-vbars-thresh", style: { top: top + "%" } });
      line.appendChild(el("span", { class: "v2-vbars-thresh-label" }, (o.thresholdLabel || ("threshold " + o.threshold))));
      row.appendChild(line);
    }
    o.items.forEach((it) => {
      const col = el("div", { class: "v2-vbar-col" });
      const h = Math.max(2, (it.value / max) * 100);
      col.appendChild(el("div", { class: "v2-vbar", style: { height: h + "%", background: it.color || "var(--series-1)" }, title: (o.fmt ? o.fmt(it.value) : String(it.value)) }));
      row.appendChild(col);
    });
    wrap.appendChild(row);
    const labels = el("div", { class: "v2-vbars-labels" });
    o.items.forEach((it) => labels.appendChild(el("div", { class: "v2-vbar-label" }, it.label)));
    wrap.appendChild(labels);
    return wrap;
  }
  NG.verticalBars = verticalBars;

  // --- anomaly-band chart (μ ±3σ band + observed line + anomaly dots) --
  // anomalyBand({points:[{ts, observed, mean, stddev, z}], height, fmtY})
  // Hand-rolled SVG with a fixed viewBox, scaled to 100% width.
  function anomalyBand(o) {
    const pts = (o.points || []).filter((p) => p.observed != null).slice().sort((a, b) => (a.ts < b.ts ? -1 : 1));
    if (pts.length < 2) return el("div", { class: "v2-chart-fallback" }, "Not enough samples yet for a band.");
    const W = 1000, H = o.height || 220, padX = 40, padY = 16;
    const uppers = pts.map((p) => (p.mean ?? p.observed) + 3 * (p.stddev || 0));
    const lowers = pts.map((p) => (p.mean ?? p.observed) - 3 * (p.stddev || 0));
    const ys = pts.map((p) => p.observed).concat(uppers, lowers);
    let yMin = Math.min(...ys), yMax = Math.max(...ys);
    if (yMin === yMax) { yMin -= 1; yMax += 1; }
    const span = yMax - yMin;
    const X = (i) => padX + (i / (pts.length - 1)) * (W - padX * 2);
    const Y = (v) => padY + (1 - (v - yMin) / span) * (H - padY * 2);
    const ns = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(ns, "svg");
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`); svg.setAttribute("class", "v2-anom"); svg.setAttribute("preserveAspectRatio", "none");
    const mk = (t, a) => { const e = document.createElementNS(ns, t); for (const k in a) e.setAttribute(k, a[k]); return e; };
    // band polygon (uppers forward, lowers back)
    const bandPts = pts.map((p, i) => `${X(i).toFixed(1)},${Y(uppers[i]).toFixed(1)}`)
      .concat(pts.map((p, i) => `${X(pts.length - 1 - i).toFixed(1)},${Y(lowers[pts.length - 1 - i]).toFixed(1)}`)).join(" ");
    svg.appendChild(mk("polygon", { points: bandPts, fill: "rgba(76,141,255,0.12)", stroke: "rgba(76,141,255,0.3)", "stroke-width": "1" }));
    // mean dashed line
    svg.appendChild(mk("polyline", { points: pts.map((p, i) => `${X(i).toFixed(1)},${Y(p.mean ?? p.observed).toFixed(1)}`).join(" "), fill: "none", stroke: "#5C6675", "stroke-width": "1", "stroke-dasharray": "4 4" }));
    // observed line
    svg.appendChild(mk("polyline", { points: pts.map((p, i) => `${X(i).toFixed(1)},${Y(p.observed).toFixed(1)}`).join(" "), fill: "none", stroke: "#808000", "stroke-width": "2", "vector-effect": "non-scaling-stroke" }));
    // observed points — every sample is a hoverable/clickable dot (anomalies emphasised)
    const fmtTs = (ts) => { try { return new Date(ts).toLocaleString(); } catch (_e) { return String(ts); } };
    const fmtV = o.fmtV || ((v) => (v == null ? "—" : (typeof v === "number" ? v.toFixed(4) : String(v))));
    pts.forEach((p, i) => {
      const anomaly = Math.abs(p.z || 0) > 3;
      const clickable = !!(o.onPointClick && p.decisionId);
      const c = mk("circle", {
        cx: X(i), cy: Y(p.observed), r: anomaly ? "5" : "3.4",
        fill: anomaly ? "#E5484D" : "#808000",
        class: "v2-anom-dot" + (clickable ? " v2-anom-dot-click" : ""),
      });
      const title = document.createElementNS(ns, "title");
      const zTxt = p.z != null ? ` · z=${p.z.toFixed(2)}` : "";
      const meanTxt = p.mean != null ? ` · μ=${fmtV(p.mean)}` : "";
      title.textContent = `${fmtTs(p.ts)}\nobserved ${fmtV(p.observed)}${meanTxt}${zTxt}${anomaly ? " ⚠ anomaly" : ""}${clickable ? "\nclick → request detail" : ""}`;
      c.appendChild(title);
      if (clickable) c.addEventListener("click", () => o.onPointClick(p));
      svg.appendChild(c);
    });
    const wrap = el("div", { class: "v2-anom-wrap", style: { height: H + "px" } });
    wrap.appendChild(svg);
    return wrap;
  }
  NG.anomalyBand = anomalyBand;
  NG.heatColor = heatColor;
  function heatmap(o) {
    const table = el("table", { class: "v2-heatmap" });
    const thead = el("thead"), htr = el("tr");
    htr.appendChild(el("th", null, o.rowLabel || ""));
    (o.colLabels || []).forEach((c) => htr.appendChild(el("th", { class: "ta-center" }, c)));
    thead.appendChild(htr); table.appendChild(thead);
    const tbody = el("tbody");
    (o.rows || []).forEach((row) => {
      const tr = el("tr");
      tr.appendChild(el("td", { class: "v2-heatmap-rowlabel" }, row.label));
      (row.cells || []).forEach((cell) => {
        const td = el("td", {
          class: "v2-heatmap-cell",
          style: { background: heatColor(cell.value), color: heatText(cell.value) },
          title: cell.title || "",
        }, cell.display != null ? cell.display : (cell.value == null ? "—" : Math.round(cell.value * 100) + "%"));
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    return table;
  }
  NG.heatmap = heatmap;

  // --- DataTable ------------------------------------------------------
  // DataTable(mount, {
  //   title, columns:[{key,label,align,sortable,width,mono,render(row),sortValue(row),sparkline}],
  //   rows, search, columnsMenu, view:{table:true, chart(rows)->Node},
  //   rowClass(row), onRowClick(row), pageSize, emptyText, defaultSort:{key,dir}
  // })
  function DataTable(mount, cfg) {
    const state = {
      rows: cfg.rows || [],
      sortKey: cfg.defaultSort ? cfg.defaultSort.key : null,
      sortDir: cfg.defaultSort ? cfg.defaultSort.dir : "desc",
      filter: "",
      hidden: new Set(),
      page: 0,
      view: "table",
      segment: 0,
      pageSize: cfg.pageSize || 0,
    };
    const api = { state };

    function visibleCols() { return cfg.columns.filter((c) => !state.hidden.has(c.key)); }

    function filtered() {
      let rows = state.rows;
      if (cfg.segments && cfg.segments.options[state.segment]) {
        const pred = cfg.segments.options[state.segment].predicate;
        if (pred) rows = rows.filter(pred);
      }
      if (state.filter) {
        const q = state.filter.toLowerCase();
        rows = rows.filter((r) => cfg.columns.some((c) => {
          const v = c.filterValue ? c.filterValue(r) : (c.sortValue ? c.sortValue(r) : r[c.key]);
          return String(v == null ? "" : v).toLowerCase().includes(q);
        }));
      }
      if (state.sortKey) {
        const col = cfg.columns.find((c) => c.key === state.sortKey);
        if (col) {
          const sv = col.sortValue || ((r) => r[col.key]);
          rows = rows.slice().sort((a, b) => {
            const va = sv(a), vb = sv(b);
            let cmp;
            if (typeof va === "number" && typeof vb === "number") cmp = va - vb;
            else cmp = String(va == null ? "" : va).localeCompare(String(vb == null ? "" : vb));
            return state.sortDir === "asc" ? cmp : -cmp;
          });
        }
      }
      return rows;
    }

    function render() {
      mount.innerHTML = "";
      const c = el("div", { class: "v2-card v2-dt" });

      // toolbar
      const bar = el("div", { class: "v2-dt-bar" });
      const left = el("div", { class: "v2-dt-bar-left" });
      if (cfg.title) left.appendChild(el("span", { class: "v2-card-title" }, cfg.title));
      const rowsNow = filtered();
      left.appendChild(el("span", { class: "v2-card-meta" }, (cfg.countLabel ? cfg.countLabel(rowsNow.length) : `${rowsNow.length} ${rowsNow.length === 1 ? "row" : "rows"}`)));
      bar.appendChild(left);

      const right = el("div", { class: "v2-dt-bar-right" });
      if (cfg.segments) {
        const seg = el("div", { class: "v2-seg" });
        cfg.segments.options.forEach((opt, i) => {
          const b = el("button", { class: "v2-seg-btn" + (state.segment === i ? " active" : "") }, opt.label);
          b.addEventListener("click", () => { state.segment = i; state.page = 0; render(); });
          seg.appendChild(b);
        });
        right.appendChild(seg);
      }
      if (cfg.search !== false) {
        const search = el("div", { class: "v2-dt-search" });
        search.appendChild(el("span", { class: "v2-dt-search-icon", html: '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.7"><circle cx="11" cy="11" r="6"/><path d="M20 20l-4-4"/></svg>' }));
        const inp = el("input", { type: "text", placeholder: cfg.searchPlaceholder || "Filter rows…", value: state.filter });
        inp.addEventListener("input", () => { state.filter = inp.value; state.page = 0; rerenderBody(); });
        search.appendChild(inp);
        right.appendChild(search);
      }
      if (cfg.columnsMenu !== false && cfg.columns.length > 3) right.appendChild(columnsMenu());
      if (cfg.view && cfg.view.chart) right.appendChild(viewToggle());
      bar.appendChild(right);
      c.appendChild(bar);

      // body container (table or chart)
      const bodyWrap = el("div", { class: "v2-dt-bodywrap" });
      c.appendChild(bodyWrap);
      mount.appendChild(c);

      api._bodyWrap = bodyWrap;
      api._bar = bar;
      rerenderBody();
    }

    function rerenderBody() {
      const bodyWrap = api._bodyWrap;
      if (!bodyWrap) return;
      // refresh count
      const meta = api._bar.querySelector(".v2-card-meta");
      const rows = filtered();
      if (meta) meta.textContent = cfg.countLabel ? cfg.countLabel(rows.length) : `${rows.length} ${rows.length === 1 ? "row" : "rows"}`;

      bodyWrap.innerHTML = "";
      if (state.view === "chart" && cfg.view && cfg.view.chart) {
        bodyWrap.appendChild(cfg.view.chart(rows));
        return;
      }

      const cols = visibleCols();
      const table = el("table", { class: "v2-table" });
      const thead = el("thead");
      const htr = el("tr");
      cols.forEach((col) => {
        const th = el("th", {
          class: (col.align === "right" ? "ta-right " : "") + (col.sortable !== false ? "sortable" : ""),
          style: col.width ? { width: col.width } : null,
        });
        th.appendChild(document.createTextNode(col.label));
        if (state.sortKey === col.key) {
          th.appendChild(el("span", { class: "v2-sort-ind" }, state.sortDir === "asc" ? " ▲" : " ▼"));
          th.classList.add("active");
        }
        if (col.sortable !== false) {
          th.addEventListener("click", () => {
            if (state.sortKey === col.key) state.sortDir = state.sortDir === "asc" ? "desc" : "asc";
            else { state.sortKey = col.key; state.sortDir = col.defaultDir || "desc"; }
            rerenderBody();
          });
        }
        htr.appendChild(th);
      });
      thead.appendChild(htr);
      table.appendChild(thead);

      const tbody = el("tbody");
      let pageRows = rows;
      const ps = state.pageSize || 0;
      let pager = null;
      if (ps > 0) {
        const pages = Math.max(1, Math.ceil(rows.length / ps));
        if (state.page >= pages) state.page = pages - 1;
        pageRows = rows.slice(state.page * ps, state.page * ps + ps);
      }

      if (!pageRows.length) {
        const tr = el("tr");
        tr.appendChild(el("td", { class: "v2-dt-empty", colspan: String(cols.length) }, cfg.emptyText || "No rows."));
        tbody.appendChild(tr);
      } else {
        pageRows.forEach((row) => {
          const tr = el("tr", { class: cfg.rowClass ? cfg.rowClass(row) : null });
          if (cfg.onRowClick) { tr.classList.add("v2-row-click"); tr.addEventListener("click", () => cfg.onRowClick(row)); }
          cols.forEach((col) => {
            const td = el("td", { class: (col.align === "right" ? "ta-right " : "") + (col.mono ? "mono" : "") });
            const content = col.render ? col.render(row) : (row[col.key] == null ? "—" : row[col.key]);
            if (content instanceof Node) td.appendChild(content);
            else td.innerHTML = (typeof content === "string" && content.includes("<")) ? content : esc(String(content));
            tr.appendChild(td);
          });
          tbody.appendChild(tr);
        });
      }
      table.appendChild(tbody);
      bodyWrap.appendChild(table);

      if (ps > 0 && (rows.length > ps || cfg.pageSizeOptions)) {
        const pages = Math.max(1, Math.ceil(rows.length / ps));
        pager = el("div", { class: "v2-dt-pager" });
        const prev = el("button", { class: "v2-pg-btn", disabled: state.page === 0 || null }, "‹ prev");
        prev.addEventListener("click", () => { if (state.page > 0) { state.page--; rerenderBody(); } });
        const next = el("button", { class: "v2-pg-btn", disabled: state.page >= pages - 1 || null }, "next ›");
        next.addEventListener("click", () => { if (state.page < pages - 1) { state.page++; rerenderBody(); } });
        pager.appendChild(prev);
        pager.appendChild(el("span", { class: "v2-pg-range" }, rows.length ? `${state.page * ps + 1}–${Math.min((state.page + 1) * ps, rows.length)} of ${rows.length}` : "0 of 0"));
        pager.appendChild(next);
        if (cfg.pageSizeOptions) {
          const sel = el("select", { class: "v2-pg-select" });
          cfg.pageSizeOptions.forEach((n) => {
            const o = el("option", { value: String(n) }, `${n} / page`);
            if (n === state.pageSize) o.selected = true;
            sel.appendChild(o);
          });
          sel.addEventListener("change", () => { state.pageSize = Number(sel.value) || ps; state.page = 0; rerenderBody(); });
          pager.appendChild(sel);
        }
        bodyWrap.appendChild(pager);
      }
    }

    function columnsMenu() {
      const wrap = el("div", { class: "v2-dt-menu" });
      const btn = el("button", { class: "v2-dt-menu-btn", html: '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M4 6h16M4 12h16M4 18h10"/></svg> Columns' });
      const panel = el("div", { class: "v2-dt-menu-panel", hidden: true });
      cfg.columns.forEach((col) => {
        if (col.required) return;
        const lab = el("label", { class: "v2-dt-menu-item" });
        const cb = el("input", { type: "checkbox" });
        cb.checked = !state.hidden.has(col.key);
        cb.addEventListener("change", () => {
          if (cb.checked) state.hidden.delete(col.key); else state.hidden.add(col.key);
          render();
        });
        lab.appendChild(cb);
        lab.appendChild(document.createTextNode(col.label));
        panel.appendChild(lab);
      });
      btn.addEventListener("click", (e) => { e.stopPropagation(); panel.hidden = !panel.hidden; });
      document.addEventListener("click", () => { panel.hidden = true; });
      wrap.appendChild(btn); wrap.appendChild(panel);
      return wrap;
    }

    function viewToggle() {
      const seg = el("div", { class: "v2-seg" });
      const tBtn = el("button", { class: "v2-seg-btn" + (state.view === "table" ? " active" : "") }, "Table");
      const cBtn = el("button", { class: "v2-seg-btn" + (state.view === "chart" ? " active" : "") }, "Chart");
      tBtn.addEventListener("click", () => { state.view = "table"; tBtn.classList.add("active"); cBtn.classList.remove("active"); rerenderBody(); });
      cBtn.addEventListener("click", () => { state.view = "chart"; cBtn.classList.add("active"); tBtn.classList.remove("active"); rerenderBody(); });
      seg.appendChild(tBtn); seg.appendChild(cBtn);
      return seg;
    }

    api.setRows = function (rows) { state.rows = rows || []; rerenderBody(); };
    api.render = render;
    render();
    return api;
  }
  NG.DataTable = DataTable;

  window.NG = NG;
})();
