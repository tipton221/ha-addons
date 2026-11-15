"use strict";

// Insights charts: hand-drawn SVG (no chart library, so nothing loads from outside the app).
// Data comes from the JSON block the page embeds.

(() => {
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const NS = "http://www.w3.org/2000/svg";
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(`--${name}`).trim() || "#8e8e93";
  const color = (c) => (c && c.startsWith("#") ? c : css(c || "gray"));
  const money = (v, cents = true) => {
    const s = Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: cents ? 2 : 0, maximumFractionDigits: cents ? 2 : 0 });
    return `${v < 0 ? "-" : ""}$${s}`;
  };
  const compact = (v) => {
    const a = Math.abs(v);
    const s = a >= 1e6 ? `${(a / 1e6).toFixed(1)}M` : a >= 1e3 ? `${Math.round(a / 1e3)}k` : `${Math.round(a)}`;
    return `${v < 0 ? "-" : ""}$${s}`;
  };
  const monthLabel = (k, long) => `${MONTHS[+k.slice(5) - 1]}${long ? " " + k.slice(0, 4) : " '" + k.slice(2, 4)}`;
  const el = (tag, attrs = {}, parent) => {
    const n = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
    if (parent) parent.appendChild(n);
    return n;
  };

  // Rounded "nice" axis ticks covering lo..hi.
  function ticks(lo, hi, count = 4) {
    if (lo === hi) { lo -= 1; hi += 1; }
    const raw = (hi - lo) / count;
    const mag = 10 ** Math.floor(Math.log10(raw));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw);
    const out = [];
    for (let v = Math.floor(lo / step) * step; v <= hi + step * 0.001; v += step) out.push(Math.round(v * 100) / 100);
    if (out[out.length - 1] < hi) out.push(out[out.length - 1] + step);
    return out;
  }

  // A chart frame: the SVG, scales, a y axis with gridlines, and a hover tooltip.
  function frame(host, n, lo, hi, height) {
    host.innerHTML = "";
    const width = Math.max(host.clientWidth, 260);
    const pad = { l: 46, r: 8, t: 10, b: 24 };
    const svg = el("svg", { viewBox: `0 0 ${width} ${height}`, width, height, class: "chart-svg" }, host);
    const tk = ticks(Math.min(lo, 0), Math.max(hi, 0));
    const y0 = tk[0], y1 = tk[tk.length - 1];
    const y = (v) => pad.t + (height - pad.t - pad.b) * (1 - (v - y0) / (y1 - y0 || 1));
    const band = (width - pad.l - pad.r) / Math.max(n, 1);
    const x = (i) => pad.l + band * (i + 0.5);
    for (const v of tk) {
      el("line", { x1: pad.l, x2: width - pad.r, y1: y(v), y2: y(v), class: v === 0 ? "grid zero" : "grid" }, svg);
      const t = el("text", { x: pad.l - 8, y: y(v) + 4, class: "axis", "text-anchor": "end" }, svg);
      t.textContent = compact(v);
    }
    const tip = document.createElement("div");
    tip.className = "chart-tip";
    tip.hidden = true;
    host.appendChild(tip);
    return { svg, width, height, pad, x, y, band, tip };
  }

  function xLabels(f, keys) {
    const every = Math.max(1, Math.ceil(keys.length / Math.floor(f.width / 64)));
    keys.forEach((k, i) => {
      if ((keys.length - 1 - i) % every) return;
      const t = el("text", { x: f.x(i), y: f.height - 6, class: "axis", "text-anchor": "middle" }, f.svg);
      t.textContent = monthLabel(k);
    });
  }

  function showTip(f, i, html) {
    f.tip.innerHTML = html;
    f.tip.hidden = false;
    const w = f.tip.offsetWidth;
    f.tip.style.left = `${Math.min(Math.max(f.x(i) - w / 2, 0), f.width - w)}px`;
  }

  // Hover and touch: find the nearest month and call back with its index.
  function track(f, n, onMove, onLeave) {
    const hit = el("rect", { x: f.pad.l, y: 0, width: f.width - f.pad.l - f.pad.r, height: f.height, fill: "transparent" }, f.svg);
    const at = (ev) => {
      const r = f.svg.getBoundingClientRect();
      const px = (ev.clientX - r.left) * (f.width / r.width);
      return Math.min(n - 1, Math.max(0, Math.floor((px - f.pad.l) / f.band)));
    };
    hit.addEventListener("pointermove", (ev) => onMove(at(ev)));
    hit.addEventListener("pointerdown", (ev) => onMove(at(ev)));
    hit.addEventListener("pointerleave", () => onLeave());
    return hit;
  }

  // ---------- net worth: line with area, assets and debts in the tooltip ----------

  function netWorth(host, points) {
    if (!points.length) return;
    const lo = Math.min(...points.map((p) => p.net)), hi = Math.max(...points.map((p) => p.net));
    const f = frame(host, points.length, lo, hi, 220);
    const pts = points.map((p, i) => [f.x(i), f.y(p.net)]);
    const line = pts.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join("");
    const defs = el("defs", {}, f.svg);
    const g = el("linearGradient", { id: "nw-fill", x1: 0, x2: 0, y1: 0, y2: 1 }, defs);
    el("stop", { offset: "0%", "stop-color": css("blue"), "stop-opacity": 0.35 }, g);
    el("stop", { offset: "100%", "stop-color": css("blue"), "stop-opacity": 0 }, g);
    const base = f.y(Math.max(Math.min(0, hi), lo > 0 ? ticks(lo, hi)[0] : 0));
    el("path", { d: `${line}L${pts[pts.length - 1][0]},${base}L${pts[0][0]},${base}Z`, fill: "url(#nw-fill)" }, f.svg);
    el("path", { d: line, class: "nw-line" }, f.svg);
    xLabels(f, points.map((p) => p.m));
    const cross = el("line", { y1: f.pad.t, y2: f.height - f.pad.b, class: "cross", visibility: "hidden" }, f.svg);
    const dot = el("circle", { r: 4.5, class: "nw-dot", visibility: "hidden" }, f.svg);
    track(f, points.length, (i) => {
      const p = points[i];
      cross.setAttribute("x1", pts[i][0]); cross.setAttribute("x2", pts[i][0]); cross.setAttribute("visibility", "visible");
      dot.setAttribute("cx", pts[i][0]); dot.setAttribute("cy", pts[i][1]); dot.setAttribute("visibility", "visible");
      const prev = points[i - 1];
      const ch = prev ? p.net - prev.net : null;
      showTip(f, i, `<b>${monthLabel(p.m, true)}</b><span>${money(p.net)}</span>` +
        `<small>Assets ${money(p.assets, false)} · Debts ${money(p.debts, false)}</small>` +
        (ch === null ? "" : `<small class="${ch >= 0 ? "up" : "down"}">${ch >= 0 ? "+" : ""}${money(ch)} from ${monthLabel(prev.m)}</small>`));
    }, () => { f.tip.hidden = true; cross.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); });
  }

  // ---------- spending: monthly bars (tap to pick a month), donut, and category list ----------

  function spendBars(host, months, selected, onPick) {
    const vals = months.map((m) => m.spending);
    const avg = vals.slice(0, -1).filter((v) => v > 0);
    const average = avg.length ? avg.reduce((a, b) => a + b, 0) / avg.length : 0;
    const f = frame(host, months.length, Math.min(0, ...vals), Math.max(...vals, average), host.clientWidth > 600 ? 260 : 210);
    const w = Math.min(44, f.band * 0.6);
    months.forEach((m, i) => {
      const top = f.y(Math.max(m.spending, 0)), bottom = f.y(Math.min(m.spending, 0));
      el("rect", { x: f.x(i) - w / 2, y: top, width: w, height: Math.max(bottom - top, 1), rx: Math.min(6, w / 3),
                   class: `bar${i === selected ? " on" : ""}` }, f.svg);
    });
    if (average) {
      el("line", { x1: f.pad.l, x2: f.width - f.pad.r, y1: f.y(average), y2: f.y(average), class: "avg" }, f.svg);
    }
    xLabels(f, months.map((m) => m.m));
    const hit = track(f, months.length, (i) => {
      showTip(f, i, `<b>${monthLabel(months[i].m, true)}</b><span>${money(months[i].spending)}</span>` +
        (average ? `<small>Average ${money(average, false)}</small>` : ""));
    }, () => { f.tip.hidden = true; });
    hit.addEventListener("click", (ev) => {
      const r = f.svg.getBoundingClientRect();
      const px = (ev.clientX - r.left) * (f.width / r.width);
      onPick(Math.min(months.length - 1, Math.max(0, Math.floor((px - f.pad.l) / f.band))));
    });
    hit.style.cursor = "pointer";
    return average;
  }

  function donut(host, slices) {
    host.innerHTML = "";
    const size = 150, r = 62, sw = 20;
    const svg = el("svg", { viewBox: `0 0 ${size} ${size}`, width: size, height: size }, host);
    const total = slices.reduce((a, s) => a + s.value, 0);
    if (total <= 0) { el("circle", { cx: size / 2, cy: size / 2, r, class: "donut-empty", "stroke-width": sw }, svg); return; }
    let a0 = -Math.PI / 2;
    const gap = slices.length > 1 ? 0.025 : 0;
    for (const s of slices) {
      const a1 = a0 + (s.value / total) * Math.PI * 2;
      const s0 = a0 + gap / 2, s1 = Math.max(a1 - gap / 2, s0 + 0.001);
      const p = (a) => `${(size / 2 + r * Math.cos(a)).toFixed(2)},${(size / 2 + r * Math.sin(a)).toFixed(2)}`;
      const large = s1 - s0 > Math.PI ? 1 : 0;
      el("path", { d: `M${p(s0)}A${r},${r} 0 ${large} 1 ${p(s1)}`, stroke: s.color, "stroke-width": sw, fill: "none",
                   "stroke-linecap": "butt" }, svg);
      a0 = a1;
    }
  }

  // The month's spending transactions, largest first, as the same editable rows as the Inbox.
  const purchases = {};
  async function loadPurchases(month) {
    const top = document.getElementById("spend-top");
    const all = document.getElementById("spend-all");
    const allList = document.getElementById("spend-all-list");
    if (!top) return;
    if (!purchases[month]) {
      try {
        const r = await fetch(`/api/insights/spending?m=${month}`, { credentials: "same-origin" });
        purchases[month] = r.ok ? await r.text() : "";
      } catch { purchases[month] = ""; }
    }
    if (document.getElementById("spend-month").dataset.month !== month) return;  // switched again meanwhile
    const tpl = document.createElement("template");
    tpl.innerHTML = purchases[month];
    const rows = [...tpl.content.querySelectorAll(".txn-item")];
    top.replaceChildren(...rows.slice(0, 3));
    allList.replaceChildren(...rows.slice(3));
    document.getElementById("spend-biggest").hidden = !rows.length;
    all.hidden = rows.length <= 3;
    document.getElementById("spend-all-label").textContent = `See all ${rows.length}`;
    if (restoring && restoring.allOpen) { all.open = true; }
    if (restoring) { window.scrollTo(0, restoring.y); restoring = null; }
  }

  function spending(data) {
    const months = data.months;
    if (!months.length) return;
    const shown = months.slice(-12);
    let sel = shown.length - 1;
    if (restoring && restoring.month) {
      const i = shown.findIndex((m) => m.m === restoring.month);
      if (i >= 0) sel = i;
    }
    const cats = data.categories;
    const render = () => {
      const m = shown[sel];
      const label = document.getElementById("spend-month");
      label.textContent = monthLabel(m.m, true);
      label.dataset.month = m.m;
      state.month = m.m;
      document.getElementById("spend-prev").disabled = sel === 0;
      document.getElementById("spend-next").disabled = sel === shown.length - 1;
      document.getElementById("spend-total").textContent = money(m.spending);
      const average = spendBars(document.getElementById("spend-chart"), shown, sel, (i) => { sel = i; render(); });
      const vs = document.getElementById("spend-vs");
      const partial = sel === shown.length - 1;
      if (average) {
        const d = m.spending - average;
        vs.textContent = `${partial ? "So far, " : ""}${money(Math.abs(d), false)} ${d >= 0 ? "above" : "below"} your ${money(average, false)} average`;
        vs.className = `stat-change ${partial ? "" : d > 0 ? "down" : "up"}`;
      } else vs.textContent = "";
      const rows = Object.entries(m.cats).map(([id, v]) => ({ id, v, cat: cats[id] || { name: "Other", color: "gray" } }))
        .sort((a, b) => b.v - a.v);
      const positive = rows.filter((r) => r.v > 0);
      const total = positive.reduce((a, r) => a + r.v, 0);
      donut(document.getElementById("spend-donut"), positive.map((r) => ({ value: r.v, color: color(r.cat.color) })));
      const ul = document.getElementById("spend-cats");
      ul.innerHTML = "";
      const last = new Date(+m.m.slice(0, 4), +m.m.slice(5), 0).getDate();
      for (const r of rows) {
        const li = document.createElement("li");
        const a = document.createElement("a");
        const q = new URLSearchParams({ start: `${m.m}-01`, end: `${m.m}-${String(last).padStart(2, "0")}` });
        if (r.id !== "None") q.append("category", r.id);
        a.href = `/search?${q}`;
        const pct = total > 0 && r.v > 0 ? Math.round((100 * r.v) / total) : 0;
        a.innerHTML = `<span class="cb-name"></span><span class="cb-amt"></span>` +
          `<span class="cb-track"><span class="cb-fill"></span></span><span class="cb-pct"></span>`;
        a.querySelector(".cb-name").textContent = r.cat.name;
        a.querySelector(".cb-amt").textContent = money(r.v);
        a.querySelector(".cb-pct").textContent = r.v > 0 ? (pct < 1 ? "<1%" : `${pct}%`) : "refund";
        const fill = a.querySelector(".cb-fill");
        fill.style.width = `${Math.max(pct, r.v > 0 ? 1 : 0)}%`;
        fill.style.background = color(r.cat.color);
        li.appendChild(a);
        ul.appendChild(li);
      }
      loadPurchases(m.m);
    };
    document.getElementById("spend-prev").addEventListener("click", () => { if (sel > 0) { sel--; render(); } });
    document.getElementById("spend-next").addEventListener("click", () => { if (sel < shown.length - 1) { sel++; render(); } });
    render();
    return render;
  }

  // ---------- cash flow: money in and out side by side ----------

  function cashFlow(host, months) {
    const shown = months.slice(-12);
    const outs = shown.map((m) => m.spending + m.debt);
    const f = frame(host, shown.length, Math.min(0, ...outs), Math.max(...shown.map((m) => m.income), ...outs), 170);
    const w = Math.min(14, f.band * 0.32);
    shown.forEach((m, i) => {
      for (const [v, cls, dx] of [[m.income, "bar-in", -w / 2 - 1], [outs[i], "bar-out", w / 2 + 1]]) {
        const top = f.y(Math.max(v, 0)), bottom = f.y(Math.min(v, 0));
        el("rect", { x: f.x(i) + dx - w / 2, y: top, width: w, height: Math.max(bottom - top, 1), rx: Math.min(4, w / 3), class: cls }, f.svg);
      }
    });
    xLabels(f, shown.map((m) => m.m));
    track(f, shown.length, (i) => {
      const m = shown[i];
      showTip(f, i, `<b>${monthLabel(m.m, true)}</b><small>In ${money(m.income, false)}</small>` +
        `<small>Out ${money(outs[i], false)}</small><small class="${m.kept >= 0 ? "up" : "down"}">Kept ${money(m.kept, false)}</small>`);
    }, () => { f.tip.hidden = true; });
  }

  // ---------- coming back: after following a link (a category, a transaction) or saving, return to
  // the same place, month, and range instead of the top ----------

  const KEY = "insights-return";
  const state = { month: null, range: 0 };
  let restoring = null;
  let leaving = false;
  try {
    const saved = JSON.parse(sessionStorage.getItem(KEY) || "null");
    sessionStorage.removeItem(KEY);
    if (saved && Date.now() - saved.at < 30 * 60 * 1000) restoring = saved;
  } catch { /* storage blocked */ }
  document.addEventListener("click", (ev) => {
    const a = ev.target.closest("a[href]");
    if ((a && !a.closest(".tabbar")) || ev.target.closest(".sheet button:not(.sheet-close)")) leaving = true;
  }, true);
  document.addEventListener("submit", () => { leaving = true; }, true);
  window.addEventListener("pagehide", () => {
    if (!leaving) return;
    try {
      sessionStorage.setItem(KEY, JSON.stringify({
        at: Date.now(), y: window.scrollY, month: state.month, range: state.range,
        allOpen: !!document.getElementById("spend-all")?.open,
        byMonth: !!document.querySelector("#networth details")?.open,
      }));
    } catch { /* storage blocked */ }
  });

  // ---------- page ----------

  function init() {
    const raw = document.getElementById("insights-data");
    if (!raw) return;
    const data = JSON.parse(raw.textContent);
    const nwHost = document.getElementById("nw-chart");
    let range = restoring ? restoring.range || 0 : 0;
    if (restoring) {
      document.querySelectorAll("#networth [data-range]").forEach((x) => x.classList.toggle("on", +x.dataset.range === range));
      const d = document.querySelector("#networth details");
      if (d && restoring.byMonth) d.open = true;
    }
    const drawNetWorth = () => {
      state.range = range;
      if (!nwHost) return;
      const pts = range ? data.networth.slice(-range - 1) : data.networth;
      netWorth(nwHost, pts);
      const change = document.getElementById("nw-change");
      if (pts.length > 1) {
        const d = pts[pts.length - 1].net - pts[0].net;
        change.textContent = `${d >= 0 ? "+" : ""}${money(d, false)} since ${monthLabel(pts[0].m, true)}`;
        change.className = `stat-change ${d >= 0 ? "up" : "down"}`;
      }
    };
    document.querySelectorAll("#networth [data-range]").forEach((b) => b.addEventListener("click", () => {
      document.querySelectorAll("#networth [data-range]").forEach((x) => x.classList.toggle("on", x === b));
      range = +b.dataset.range;
      drawNetWorth();
    }));
    drawNetWorth();
    const redrawSpending = spending(data);
    if (restoring && !document.getElementById("spend-top")) { window.scrollTo(0, restoring.y); restoring = null; }
    const flowHost = document.getElementById("flow-chart");
    if (flowHost) cashFlow(flowHost, data.months);
    let width = window.innerWidth;
    window.addEventListener("resize", () => {
      if (Math.abs(window.innerWidth - width) < 4) return;
      width = window.innerWidth;
      drawNetWorth();
      if (redrawSpending) redrawSpending();
      if (flowHost) cashFlow(flowHost, data.months);
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
