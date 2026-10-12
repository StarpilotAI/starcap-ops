/*
 * charts.js: the StarCap Tracker chart kit. Plain SVG, no dependencies.
 *
 * Every chart draws at its container's real pixel width (so 11 px text stays 11 px at any
 * window size) and redraws when that width changes. Each one is a single tab stop:
 * arrow keys walk the data with the same tooltip the mouse gets, and the figure's
 * "Table" button swaps the drawing for a plain table, so no value is gated behind
 * hover or color.
 *
 * Colors come from CSS custom properties in app.css. The take-bucket and series steps
 * there were checked with the dataviz skill's validate_palette.js against --panel
 * (#171d26): every pair of bucket colors stays apart under protan/deutan simulation,
 * because an empty bucket drops out of a stack and any two can end up touching.
 */

const live = new Set();          // mounted charts: resized together, disposed per view
const GAP = 2;                   // surface gap between touching fills
const CHAR_W = 6.2;              // 11 px system-ui, close enough to size gutters

export const BUCKETS = [
  { key: "good",        label: "good",        color: "var(--b-good)" },
  { key: "fps30",       label: "30 fps",      color: "var(--b-fps30)" },
  { key: "timing_fail", label: "timing fail", color: "var(--b-timing)" },
  { key: "untimed",     label: "untimed",     color: "var(--b-untimed)" },
  { key: "broken",      label: "broken",      color: "var(--b-broken)" },
  { key: "short",       label: "short",       color: "var(--b-short)" },
  { key: "pending",     label: "pending",     color: "var(--b-pending)", hatch: true },
];
export const BUCKET_HELP = {
  good: "60 fps, passes the p99 timing rule, clip of 30 s or more",
  fps30: "camera fell back to USB 2 and recorded at 30 fps",
  timing_fail: "more than 1 % of frame gaps outside ±10 ms",
  untimed: "analysed, but no frame timing to judge",
  broken: "unrecoverable fault or no video",
  short: "under 30 s, doesn't count as a clip",
  pending: "not analysed yet",
};

export const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const textW = (s) => String(s).length * CHAR_W;
const r1 = (v) => Math.round(v * 10) / 10;
const fill = (c, hatch) => (hatch ? "fill:url(#hatch-pending)" : `fill:${c}`);

/* ---------------------------------------------------------------- tooltip ---- */

let tipEl = null;
let tipOwner = null;

function tipNode() {
  if (!tipEl) {
    tipEl = document.getElementById("tip");
    if (!tipEl) {
      tipEl = document.createElement("div");
      tipEl.id = "tip";
      tipEl.className = "tip";
      document.body.appendChild(tipEl);
    }
  }
  return tipEl;
}

/**
 * Show the one tooltip. content = string, or {title, rows: [{value, label, color,
 * hatch, kind: "line"|"rect"|"dash"}], note}. Everything goes in as textContent:
 * names come from the API and are never parsed as HTML.
 */
export function showTip(content, pageX, pageY, owner = null) {
  const el = tipNode();
  tipOwner = owner;
  el.replaceChildren();
  if (typeof content === "string") {
    el.textContent = content;
  } else {
    if (content.title) {
      const t = document.createElement("div");
      t.className = "tip-title";
      t.textContent = content.title;
      el.appendChild(t);
    }
    for (const r of content.rows || []) {
      const row = document.createElement("div");
      row.className = "tip-row";
      const key = document.createElement("i");
      key.className = "tip-key " + (r.kind || "line") + (r.hatch ? " hatch" : "");
      if (r.color) key.style.setProperty("--k", r.color);
      if (!r.color && !r.hatch) key.classList.add("none");
      const v = document.createElement("b");
      v.textContent = r.value;
      const l = document.createElement("span");
      l.textContent = r.label || "";
      row.append(key, v, l);
      el.appendChild(row);
    }
    if (content.note) {
      const n = document.createElement("div");
      n.className = "tip-note";
      n.textContent = content.note;
      el.appendChild(n);
    }
  }
  el.hidden = false;
  // place beside the point, flipped to stay inside the viewport
  const pad = 14, vw = document.documentElement.clientWidth;
  const r = el.getBoundingClientRect();
  let x = pageX + pad, y = pageY + pad;
  if (x - window.scrollX + r.width > vw - 8) x = pageX - pad - r.width;
  if (y - window.scrollY + r.height > window.innerHeight - 8) y = pageY - pad - r.height;
  el.style.left = `${Math.max(window.scrollX + 4, x)}px`;
  el.style.top = `${Math.max(window.scrollY + 4, y)}px`;
}

export function hideTip(owner = null) {
  if (owner && tipOwner && owner !== tipOwner) return;
  const el = tipNode();
  el.hidden = true;
  tipOwner = null;
}

function announce(content) {
  const live = document.getElementById("sr-live");
  if (!live) return;
  if (typeof content === "string") { live.textContent = content; return; }
  live.textContent = [content.title, ...(content.rows || []).map((r) => `${r.label} ${r.value}`), content.note]
    .filter(Boolean).join(", ");
}

/** Tooltips for plain HTML: any element with data-tip (text, or JSON for rows). */
export function bindTips(root = document) {
  const read = (el) => {
    const raw = el.getAttribute("data-tip");
    if (!raw) return null;
    if (raw[0] === "{") { try { return JSON.parse(raw); } catch (_) { return raw; } }
    return raw;
  };
  const at = (el) => {
    const b = el.getBoundingClientRect();
    return [b.left + window.scrollX + Math.min(b.width, 40), b.bottom + window.scrollY - 6];
  };
  root.addEventListener("pointerover", (e) => {
    const el = e.target.closest?.("[data-tip]");
    if (!el) return;
    const c = read(el);
    if (c) showTip(c, e.pageX, e.pageY, el);
  });
  root.addEventListener("pointermove", (e) => {
    const el = e.target.closest?.("[data-tip]");
    if (el && tipOwner === el) {
      const c = read(el);
      if (c) showTip(c, e.pageX, e.pageY, el);
    }
  });
  root.addEventListener("pointerout", (e) => {
    const el = e.target.closest?.("[data-tip]");
    if (el && !el.contains(e.relatedTarget)) hideTip(el);
  });
  root.addEventListener("focusin", (e) => {
    const el = e.target.closest?.("[data-tip]");
    if (!el) return;
    const c = read(el);
    if (c) { const [x, y] = at(el); showTip(c, x, y, el); }
  });
  root.addEventListener("focusout", (e) => {
    const el = e.target.closest?.("[data-tip]");
    if (el) hideTip(el);
  });
}

/* ----------------------------------------------------------------- scales ---- */

function niceStep(raw) {
  const p = Math.pow(10, Math.floor(Math.log10(raw)));
  const f = raw / p;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * p;
}

/** Round ticks covering [lo, hi]: 0 / 10 / 20, never 0 / 13.7 / 27.4. */
export function niceTicks(lo, hi, count = 4) {
  if (!(hi > lo)) hi = lo + 1;
  const step = niceStep((hi - lo) / count);
  const min = Math.floor(lo / step) * step;
  const max = Math.ceil(hi / step - 1e-9) * step;
  const ticks = [];
  for (let v = min; v <= max + step / 2; v += step) ticks.push(Math.round(v * 1e6) / 1e6);
  return { ticks, min, max, step };
}

export function fmtTick(v) {
  const a = Math.abs(v);
  if (a >= 1000) return `${r1(v / 1000)}K`.replace(".0K", "K");
  return Number.isInteger(v) ? String(v) : String(r1(v));
}

/** A column path: 4 px rounded data end on top, square at the baseline. */
function colPath(x, y, w, h, r) {
  r = Math.max(0, Math.min(r, w / 2, h));
  if (!r) return `M${x},${y + h}V${y}H${x + w}V${y + h}Z`;
  return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`;
}

/** A horizontal segment, rounded on the right (its data end) when r > 0. */
function rowPath(x, y, w, h, r) {
  r = Math.max(0, Math.min(r, h / 2, w));
  if (!r) return `M${x},${y}H${x + w}V${y + h}H${x}Z`;
  return `M${x},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h - r}Q${x + w},${y + h} ${x + w - r},${y + h}H${x}Z`;
}

/* ------------------------------------------------------------ mount/resize ---- */

let uid = 0;

function mount(el, spec, draw, table) {
  const ch = { el, spec, draw, table, w: 0, idx: -1, id: `ch${++uid}`, showTable: false, geo: null };
  el.classList.add("chart");
  const redraw = () => {
    if (ch.showTable) return;
    const w = Math.floor(el.clientWidth);
    if (w > 0 && w !== ch.w) { ch.w = w; draw(ch); }
  };
  ch.ro = new ResizeObserver(() => requestAnimationFrame(redraw));
  ch.ro.observe(el);
  live.add(ch);
  ch.w = Math.floor(el.clientWidth);
  draw(ch);
  // the figure's Table/Chart toggle, if it has one
  const btn = el.closest("figure")?.querySelector("[data-tbl]");
  if (btn && table) {
    btn.hidden = false;
    btn.onclick = () => {
      ch.showTable = !ch.showTable;
      btn.textContent = ch.showTable ? "Chart" : "Table";
      btn.setAttribute("aria-pressed", String(ch.showTable));
      if (ch.showTable) { hideTip(); el.innerHTML = table(spec); } else { ch.w = 0; redraw(); }
    };
  }
  return ch;
}

/** Drop every mounted chart (called before a view replaces the page). */
export function disposeAll() {
  for (const ch of live) ch.ro.disconnect();
  live.clear();
  hideTip();
}

function svgOpen(ch, W, H) {
  const { spec, id } = ch;
  const t = esc(spec.title || "Chart"), d = esc(spec.desc || "");
  return `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" tabindex="0"
    aria-labelledby="${id}t ${id}d"><title id="${id}t">${t}</title><desc id="${id}d">${d}${d ? " " : ""}Use the arrow keys to read each value, or the Table button.</desc>`;
}

/** Shared hover + keyboard for charts read column by column (x = index). */
function wireIndex(ch, { n, xAt, indexAt, tipAt, focusY, onIndex }) {
  const svg = ch.el.querySelector("svg");
  if (!svg) return;
  const show = (i, px, py) => {
    ch.idx = i;
    onIndex(i);
    const c = tipAt(i);
    if (!c) { hideTip(svg); return; }
    if (px == null) {
      const b = svg.getBoundingClientRect();
      px = b.left + window.scrollX + xAt(i);
      py = b.top + window.scrollY + focusY(i);
    }
    showTip(c, px, py, svg);
    return c;
  };
  svg.addEventListener("pointermove", (e) => {
    const b = svg.getBoundingClientRect();
    const i = indexAt(e.clientX - b.left, e.clientY - b.top);
    if (i < 0) { onIndex(-1); hideTip(svg); return; }
    show(i, e.pageX, e.pageY);
  });
  svg.addEventListener("pointerleave", () => { ch.idx = -1; onIndex(-1); hideTip(svg); });
  svg.addEventListener("focus", () => { const c = show(ch.idx >= 0 ? ch.idx : n - 1); if (c) announce(c); });
  svg.addEventListener("blur", () => { onIndex(-1); hideTip(svg); });
  svg.addEventListener("keydown", (e) => {
    const k = e.key;
    let i = ch.idx < 0 ? n - 1 : ch.idx;
    if (k === "ArrowLeft") i = Math.max(0, i - 1);
    else if (k === "ArrowRight") i = Math.min(n - 1, i + 1);
    else if (k === "Home") i = 0;
    else if (k === "End") i = n - 1;
    else if (k === "Escape") { onIndex(-1); hideTip(svg); return; }
    else return;
    e.preventDefault();
    const c = show(i);
    if (c) announce(c);
  });
}

/** Which x labels to print: anchored on the latest point, spaced so none collide. */
function labelIndices(n, plotW, labelW) {
  if (n <= 0) return [];
  const room = Math.max(1, Math.floor(plotW / (labelW + 14)));
  const step = Math.max(1, Math.ceil(n / room));
  const out = [];
  for (let i = n - 1; i >= 0; i -= step) out.push(i);
  return out;
}

function xAxis(spec, data, n, X, y, plotW, gutter, W) {
  const lab = (d, i) => (spec.xLabel ? spec.xLabel(d, i) : String(i));
  const lw = Math.max(...data.map((d, i) => textW(lab(d, i))), 10);
  let s = "";
  for (const i of labelIndices(n, plotW, lw)) {
    const t = lab(data[i], i);
    const x = X(i);
    let anchor = "middle";
    if (x - textW(t) / 2 < gutter - 4) anchor = "start";
    else if (x + textW(t) / 2 > W - 2) anchor = "end";
    const ax = anchor === "start" ? Math.max(2, x - 6) : anchor === "end" ? Math.min(W - 2, x + 6) : x;
    s += `<text class="xl" x="${ax}" y="${y}" text-anchor="${anchor}">${esc(t)}</text>`;
  }
  return s;
}

/**
 * Gridlines and y tick labels. A reference value (target, capacity, the 99% rule) is
 * printed in the gutter in strong ink, never inside the plot where it would sit on top
 * of the bars; ordinary ticks that would crowd it are dropped. Its name is in the legend.
 */
function yAxis(ticks, Y, fy, gutter, right, refs, baseTick) {
  let s = "";
  const refYs = refs.map((r) => Y(r.value));
  for (const t of ticks) {
    const y = Math.round(Y(t)) + 0.5;
    s += `<line class="${t === baseTick ? "base" : "grid"}" x1="${gutter}" x2="${right}" y1="${y}" y2="${y}"/>`;
    if (refYs.some((ry) => Math.abs(ry - Y(t)) < 11)) continue;
    s += `<text class="tick" x="${gutter - 6}" y="${y + 3.5}" text-anchor="end">${esc(fy(t))}</text>`;
  }
  refs.forEach((r, i) => {
    s += `<text class="refv" x="${gutter - 6}" y="${Math.round(refYs[i]) + 4}" text-anchor="end">${esc(r.tick ?? fy(r.value))}</text>`;
  });
  return s;
}

function refLines(refs, Y, gutter, right, ph, topPad) {
  let s = "";
  for (const r of refs) {
    const y = Math.round(Y(r.value)) + 0.5;
    if (y < topPad - 1 || y > topPad + ph + 1) continue;
    s += `<line class="ref" x1="${gutter}" x2="${right}" y1="${y}" y2="${y}"/>`;
    if (r.inside && r.label) s += `<text class="refl" x="${right - 2}" y="${y - 4}" text-anchor="end">${esc(r.label)}</text>`;
  }
  return s;
}

/* ----------------------------------------------------------------- columns ---- */

/**
 * Vertical bars over an ordered x (days), stacked when there are several series,
 * with optional lines and dashed reference lines on the same y scale.
 *
 * spec: {data, title, desc, height, xLabel(d,i), xTitle(d), series: [{key, label, color,
 *   hatch, value(d)}], lines: [{key, label, color, value(d)}], refs: [{value, label}],
 *   partial(d), peak(d) -> label | null, yFormat, valueFormat, tipExtra(d) -> rows,
 *   table: {columns: [{label, value(d)}]}}
 */
export function columns(el, spec) {
  return mount(el, spec, drawColumns, tableOf);
}

function drawColumns(ch) {
  const { el, spec } = ch;
  const data = spec.data || [];
  const n = data.length;
  const series = spec.series || [], lines = spec.lines || [], refs = spec.refs || [];
  const W = Math.max(180, ch.w), H = spec.height || 180;
  const val = (f, d) => { const v = Number(f(d)); return Number.isFinite(v) ? v : 0; };
  const totals = data.map((d) => series.reduce((a, s) => a + Math.max(0, val(s.value, d)), 0));
  const hi = Math.max(0, ...totals, ...lines.flatMap((l) => data.map((d) => val(l.value, d))),
    ...refs.map((r) => r.value * 1.12));
  const { ticks, max } = niceTicks(0, hi || 1, spec.ticks || 4);
  const fy = spec.yFormat || fmtTick;
  const gutter = Math.ceil(Math.max(...ticks.map((t) => textW(fy(t))), ...refs.map((r) => textW(r.tick ?? fy(r.value)) + 2))) + 10;
  const peaks = spec.peak ? data.map((d) => spec.peak(d)) : [];
  const topPad = peaks.some(Boolean) ? 20 : 10, botPad = 22, right = W - 4;
  const pw = right - gutter, ph = H - topPad - botPad;
  const band = pw / Math.max(1, n);
  const bw = Math.max(2, Math.min(24, Math.round(band * 0.62)));
  const X = (i) => gutter + band * i + band / 2;
  const Y = (v) => topPad + ph - (Math.max(0, v) / max) * ph;
  let s = svgOpen(ch, W, H);
  s += yAxis(ticks, Y, fy, gutter, right, refs, 0);
  s += `<rect class="hoverband" x="0" y="${topPad}" width="${Math.max(1, band)}" height="${ph}" visibility="hidden"/>`;
  data.forEach((d, i) => {
    const part = spec.partial && spec.partial(d);
    const segs = [];
    let acc = 0;
    for (const sr of series) {
      const v = Math.max(0, val(sr.value, d));
      if (v > 0) { segs.push({ sr, v0: acc, v1: acc + v }); acc += v; }
    }
    const x = Math.round(X(i) - bw / 2);
    segs.forEach((g, k) => {
      const yTop = Y(g.v1);
      const yBot = Y(g.v0) - (k > 0 ? GAP : 0);
      const h = yBot - yTop;
      if (h < 0.6) return;
      const p = colPath(x, yTop, bw, h, k === segs.length - 1 ? 4 : 0);
      s += `<path class="mk" d="${p}" style="${fill(g.sr.color, g.sr.hatch)}"/>`;
      if (part) s += `<path class="partial" d="${p}"/>`;
    });
  });
  // a day that has a status but no height (went out, got nothing) still needs a mark
  if (spec.baseMark) {
    data.forEach((d, i) => {
      const m = spec.baseMark(d);
      if (m) s += `<circle class="dot" cx="${X(i)}" cy="${Y(0) - 4}" r="4" style="fill:${m.color}"/>`;
    });
  }
  s += refLines(refs, Y, gutter, right, ph, topPad);
  for (const l of lines) {
    const pts = data.map((d, i) => [X(i), Y(val(l.value, d))]);
    const lastFull = spec.partial ? data.map((d) => !spec.partial(d)).lastIndexOf(true) : n - 1;
    const solid = pts.slice(0, lastFull + 1);
    if (solid.length > 1) s += `<path class="ln" d="M${solid.map((p) => p.join(",")).join("L")}" style="stroke:${l.color}"/>`;
    if (lastFull < n - 1 && lastFull >= 0) {
      s += `<path class="ln dash" d="M${pts.slice(lastFull).map((p) => p.join(",")).join("L")}" style="stroke:${l.color}"/>`;
    }
    const [lx, ly] = pts[Math.max(0, lastFull)] || [0, 0];
    if (pts.length) s += `<circle class="dot" cx="${lx}" cy="${ly}" r="4" style="fill:${l.color}"/>`;
  }
  peaks.forEach((t, i) => {
    if (!t) return;
    const y = Y(totals[i]) - 6;
    const tw = textW(t);
    let x = X(i), anchor = "middle";
    if (x + tw / 2 > right) { anchor = "end"; x = Math.min(right, X(i) + bw / 2); }
    else if (x - tw / 2 < gutter) { anchor = "start"; x = X(i) - bw / 2; }
    s += `<text class="peak" x="${x}" y="${Math.max(11, y)}" text-anchor="${anchor}">${esc(t)}</text>`;
  });
  s += `<line class="cross" x1="0" x2="0" y1="${topPad}" y2="${topPad + ph}" visibility="hidden"/>`;
  s += xAxis(spec, data, n, X, H - 6, pw, gutter, W);
  s += "</svg>";
  el.innerHTML = s;
  const svg = el.querySelector("svg");
  const hb = svg.querySelector(".hoverband");
  const fv = spec.valueFormat || ((v) => fmtTick(r1(v)));
  wireIndex(ch, {
    n,
    xAt: (i) => X(i),
    focusY: (i) => Y(totals[i] || 0),
    indexAt: (x) => (x < gutter || x > right ? -1 : Math.min(n - 1, Math.floor((x - gutter) / band))),
    onIndex: (i) => {
      if (i < 0) { hb.setAttribute("visibility", "hidden"); return; }
      hb.setAttribute("x", gutter + band * i);
      hb.setAttribute("visibility", "visible");
    },
    tipAt: (i) => {
      const d = data[i];
      if (spec.tip) return spec.tip(d, i);
      const rows = [];
      for (const sr of [...series].reverse()) rows.push({ value: fv(val(sr.value, d)), label: sr.label, color: sr.color, hatch: sr.hatch, kind: "rect" });
      for (const l of lines) rows.push({ value: fv(val(l.value, d)), label: l.label, color: l.color, kind: "line" });
      for (const r of spec.tipExtra ? spec.tipExtra(d) : []) rows.push(r);
      return { title: spec.xTitle ? spec.xTitle(d) : String(i), rows, note: spec.partial && spec.partial(d) ? "today, still filling in" : "" };
    },
  });
}

/* ------------------------------------------------------------------- lines ---- */

/**
 * Lines over the same day axis as columns (points sit on band centres, so small
 * multiples line up by date). spec: {data, series: [{key, label, color, value(d),
 * area}], refs, yDomain: [lo, hi] | null (auto from 0), flag(d) -> bool (status ring),
 * flagColor, partial(d), xLabel, xTitle, valueFormat, height}
 */
export function lines(el, spec) {
  return mount(el, spec, drawLines, tableOf);
}

function drawLines(ch) {
  const { el, spec } = ch;
  const data = spec.data || [];
  const n = data.length;
  const series = spec.series || [], refs = spec.refs || [];
  const W = Math.max(180, ch.w), H = spec.height || 160;
  const val = (f, d) => { const v = f(d); return v == null || !Number.isFinite(Number(v)) ? null : Number(v); };
  const all = series.flatMap((s) => data.map((d) => val(s.value, d))).filter((v) => v != null);
  let lo = 0, hi = Math.max(1, ...all, ...refs.map((r) => r.value * 1.05));
  if (spec.yDomain) [lo, hi] = spec.yDomain;
  const nt = niceTicks(lo, hi, spec.ticks || 4);
  const fy = spec.yFormat || fmtTick;
  const gutter = Math.ceil(Math.max(...nt.ticks.map((t) => textW(fy(t))), ...refs.map((r) => textW(r.tick ?? fy(r.value)) + 2))) + 10;
  const topPad = 10, botPad = 22, right = W - 6;
  const pw = right - gutter, ph = H - topPad - botPad;
  const band = pw / Math.max(1, n);
  const X = (i) => gutter + band * i + band / 2;
  const Y = (v) => topPad + ph - ((Math.min(nt.max, Math.max(nt.min, v)) - nt.min) / (nt.max - nt.min)) * ph;
  let s = svgOpen(ch, W, H);
  s += yAxis(nt.ticks, Y, fy, gutter, right, refs, nt.min);
  s += refLines(refs, Y, gutter, right, ph, topPad);
  const lastFull = spec.partial ? data.map((d) => !spec.partial(d)).lastIndexOf(true) : n - 1;
  for (const sr of series) {
    const pts = data.map((d, i) => { const v = val(sr.value, d); return v == null ? null : [X(i), Y(v)]; });
    const solid = pts.slice(0, lastFull + 1).filter(Boolean);
    if (sr.area && solid.length > 1) {
      const base = Y(nt.min);
      s += `<path class="area" d="M${solid[0][0]},${base}L${solid.map((p) => p.join(",")).join("L")}L${solid[solid.length - 1][0]},${base}Z" style="fill:${sr.color}"/>`;
    }
    if (solid.length > 1) s += `<path class="ln" d="M${solid.map((p) => p.join(",")).join("L")}" style="stroke:${sr.color}"/>`;
    // today: a level (open tickets) is real now, so it joins the line dashed; a running
    // total (hours, caps recorded) is a fraction of a day and only gets a hollow marker,
    // so a half-filled morning never draws a cliff
    const tail = pts.slice(Math.max(0, lastFull)).filter(Boolean);
    if (lastFull < n - 1 && tail.length > 1) {
      if (spec.partialConnect) s += `<path class="ln dash" d="M${tail.map((p) => p.join(",")).join("L")}" style="stroke:${sr.color}"/>`;
      const [px, py] = tail[tail.length - 1];
      s += `<circle class="dot hollow${spec.partialConnect ? "" : " faint"}" cx="${px}" cy="${py}" r="3.5" style="stroke:${sr.color}"/>`;
    }
    if (spec.flag) {
      data.forEach((d, i) => {
        if (i > lastFull || !spec.flag(d) || !pts[i]) return;
        s += `<circle class="dot flag" cx="${pts[i][0]}" cy="${pts[i][1]}" r="4" style="fill:${spec.flagColor || "var(--bad)"}"/>`;
      });
    }
    const end = pts[lastFull];
    if (end) s += `<circle class="dot" cx="${end[0]}" cy="${end[1]}" r="4" style="fill:${sr.color}"/>`;
  }
  s += `<line class="cross" x1="0" x2="0" y1="${topPad}" y2="${topPad + ph}" visibility="hidden"/>`;
  s += `<g class="hoverdots"></g>`;
  s += xAxis(spec, data, n, X, H - 6, pw, gutter, W);
  s += "</svg>";
  el.innerHTML = s;
  const svg = el.querySelector("svg");
  const cross = svg.querySelector(".cross"), hd = svg.querySelector(".hoverdots");
  const fv = spec.valueFormat || ((v) => fmtTick(r1(v)));
  wireIndex(ch, {
    n,
    xAt: (i) => X(i),
    focusY: (i) => Math.min(...series.map((sr) => { const v = val(sr.value, data[i]); return v == null ? H : Y(v); })),
    indexAt: (x) => (x < gutter - band / 2 || x > right + 2 ? -1 : Math.max(0, Math.min(n - 1, Math.round((x - gutter - band / 2) / band)))),
    onIndex: (i) => {
      if (i < 0) { cross.setAttribute("visibility", "hidden"); hd.innerHTML = ""; return; }
      const x = Math.round(X(i)) + 0.5;
      cross.setAttribute("x1", x); cross.setAttribute("x2", x);
      cross.setAttribute("visibility", "visible");
      hd.innerHTML = series.map((sr) => {
        const v = val(sr.value, data[i]);
        return v == null ? "" : `<circle class="dot" cx="${X(i)}" cy="${Y(v)}" r="4" style="fill:${sr.color}"/>`;
      }).join("");
    },
    tipAt: (i) => {
      const d = data[i];
      if (spec.tip) return spec.tip(d, i);
      const rows = series.map((sr) => { const v = val(sr.value, d); return { value: v == null ? "—" : fv(v), label: sr.label, color: sr.color, kind: "line" }; });
      for (const r of spec.tipExtra ? spec.tipExtra(d) : []) rows.push(r);
      const notes = [];
      if (spec.flag && spec.flag(d)) notes.push(spec.flagNote || "flagged");
      if (spec.partial && spec.partial(d)) notes.push("today, still filling in");
      return { title: spec.xTitle ? spec.xTitle(d) : String(i), rows, note: notes.join(" · ") };
    },
  });
}

/* ------------------------------------------------------------------ hstack ---- */

/**
 * One horizontal stacked bar: part-to-whole for a single day or cap.
 * spec: {parts: [{key, label, color, hatch, value}], total, height, title, desc,
 *   format(v) -> "512.3 h", share: true}
 */
export function hstack(el, spec) {
  return mount(el, spec, drawHstack, (sp) => tableOf({
    data: sp.parts, table: { columns: [{ label: "", value: (p) => p.label }, { label: "Value", value: (p) => (sp.format || String)(p.value) }] },
  }));
}

function drawHstack(ch) {
  const { el, spec } = ch;
  const parts = (spec.parts || []).filter((p) => p.value > 0);
  const total = spec.total ?? parts.reduce((a, p) => a + p.value, 0);
  const W = Math.max(60, ch.w), H = spec.height || 16;
  const fmt = spec.format || ((v) => String(v));
  let s = svgOpen(ch, W, H);
  s += `<rect class="track" x="0" y="0" width="${W}" height="${H}" rx="3"/>`;
  const geo = [];
  let x = 0;
  const usable = W - GAP * Math.max(0, parts.length - 1);
  parts.forEach((p, k) => {
    const w = total > 0 ? (p.value / total) * usable : 0;
    if (w <= 0) return;
    const last = k === parts.length - 1;
    const d = rowPath(x, 0, Math.max(1, w), H, last ? 4 : 0);
    s += `<path class="mk seg" data-k="${k}" d="${d}" style="${fill(p.color, p.hatch)}"/>`;
    geo.push({ p, x0: x, x1: x + w });
    x += w + GAP;
  });
  s += "</svg>";
  el.innerHTML = s;
  const svg = el.querySelector("svg");
  const segs = [...svg.querySelectorAll(".seg")];
  wireIndex(ch, {
    n: geo.length,
    xAt: (i) => (geo[i].x0 + geo[i].x1) / 2,
    focusY: () => H,
    indexAt: (px) => geo.findIndex((g) => px >= g.x0 - 1 && px <= g.x1 + 1),
    onIndex: (i) => segs.forEach((sg, k) => sg.classList.toggle("dim", i >= 0 && k !== i)),
    tipAt: (i) => {
      const g = geo[i];
      if (!g) return null;
      const share = total > 0 ? ` · ${r1((100 * g.p.value) / total)} %` : "";
      return { title: g.p.label, rows: [{ value: fmt(g.p.value) + (spec.share === false ? "" : share), label: g.p.note || "", color: g.p.color, hatch: g.p.hatch, kind: "rect" }] };
    },
  });
}

/* --------------------------------------------------------- html helpers ---- */

/**
 * A legend that mirrors the marks: rect swatch for bars, a short stroke for lines,
 * a dashed stroke for reference lines. items: [{label, value, color, hatch, kind, title}]
 */
export function legend(items, cls = "") {
  return `<ul class="legend ${cls}">${items.map((it) => `<li${it.title ? ` data-tip="${esc(it.title)}" tabindex="0"` : ""}>
    <i class="sw ${it.kind || "rect"}${it.hatch ? " hatch" : ""}" style="--k:${it.color || "transparent"}"></i>
    ${it.value != null ? `<b>${esc(it.value)}</b>` : ""}<span>${esc(it.label)}</span></li>`).join("")}</ul>`;
}

/**
 * A mini stacked bar for a table cell (HTML, so 90 rows stay cheap). Width is scaled
 * to `scaleMax` so rows compare; segments keep a 1 px surface gap.
 */
export function miniStack(hours, scaleMax, width = 72) {
  const total = hours?.total || 0;
  if (!(total > 0)) return `<span class="mstack-wrap" style="width:${width}px"><span class="mstack-none"></span></span>`;
  const w = Math.max(3, Math.round((Math.min(total, scaleMax) / scaleMax) * width));
  const segs = BUCKETS.filter((b) => (hours[b.key] || 0) > 0)
    .map((b) => `<i class="${b.hatch ? "hatch" : ""}" style="flex:${hours[b.key]} 1 0;background:${b.hatch ? "" : b.color}"></i>`).join("");
  const tip = { title: `${total.toFixed(1)} h`, rows: BUCKETS.filter((b) => (hours[b.key] || 0) > 0)
    .map((b) => ({ value: hours[b.key] < 0.05 ? "<0.1 h" : `${hours[b.key].toFixed(1)} h`, label: b.label, color: b.color, hatch: b.hatch, kind: "rect" })) };
  return `<span class="mstack-wrap" style="width:${width}px" data-tip="${esc(JSON.stringify(tip))}"><span class="mstack" style="width:${w}px">${segs}</span></span>`;
}

/** The table twin of a chart: spec.table.columns = [{label, value(d), num}]. */
export function tableOf(spec) {
  const cols = spec.table?.columns || [];
  const rows = spec.data || [];
  return `<div class="tablewrap mini"><table class="t"><thead><tr>${cols.map((c) => `<th${c.num ? ' class="num"' : ""}>${esc(c.label)}</th>`).join("")}</tr></thead>
    <tbody>${rows.map((d) => `<tr>${cols.map((c) => `<td${c.num ? ' class="num"' : ""}>${esc(c.value(d))}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
}
