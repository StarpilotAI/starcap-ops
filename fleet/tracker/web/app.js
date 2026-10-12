/*
 * app.js: the StarCap Tracker page. Vanilla ES modules, no build, no CDN.
 *
 * Reads the local tracker API (SPEC §4) and renders one view per hash route. With
 * ?demo in the URL it reads tests/fixtures/api/<name>.json from /demo/ instead and
 * fakes the few writes in memory, so the page can be reviewed without a backend.
 *
 * Rendering is template strings into <main>; every string that came from the API goes
 * through esc(). Charts are mounted afterwards by charts.js into placeholder divs.
 */
import * as C from "./charts.js";

const DEMO = new URLSearchParams(location.search).has("demo");
const TZ = "Africa/Cairo";
const esc = C.esc;
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const main = $("#main");

const S = {
  status: null,
  skew: 0,              // server clock minus this browser's; in demo it pins "now" to the fixture
  cache: new Map(),     // cache key -> data
  route: null,
  syncing: false,
  units: null,
  day: { filter: "all", q: "", sort: null },
  queueAll: false,
  demo: { notes: [], deleted: new Set(), written: null, nextId: 9000, syncUntil: 0 },
};

/* ================================================================= format == */

// Names are spelled out here rather than taken from Intl, whose en-GB says "Sept".
const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const WD = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const FP = new Intl.DateTimeFormat("en-CA", { timeZone: TZ, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
/** Cairo wall-clock parts of a timestamp (Egypt moves between +02 and +03, so never add an offset) */
function cParts(iso) {
  const d = new Date(iso);
  if (!iso || isNaN(d)) return null;
  const o = {};
  for (const x of FP.formatToParts(d)) o[x.type] = x.value;
  return o;
}
const nowMs = () => Date.now() + S.skew;
const isDay = (s) => /^\d{4}-\d{2}-\d{2}$/.test(s || "");
const dayObj = (day) => { const [y, m, d] = day.split("-").map(Number); return new Date(Date.UTC(y, m - 1, d, 12)); };
const shiftDay = (day, n) => { const t = dayObj(day); t.setUTCDate(t.getUTCDate() + n); return t.toISOString().slice(0, 10); };
const daysBetween = (a, b) => Math.round((dayObj(b) - dayObj(a)) / 864e5);
const today = () => S.status?.today || cDay(new Date(nowMs()).toISOString());
const yesterday = () => S.status?.yesterday || shiftDay(today(), -1);
/** "Sun 11 Oct" for a calendar day string */
const fDay = (day) => { if (!isDay(day)) return "—"; const d = dayObj(day); return `${WD[d.getUTCDay()]} ${d.getUTCDate()} ${MON[d.getUTCMonth()]}`; };
const fDayShort = (day) => { if (!isDay(day)) return "—"; const d = dayObj(day); return `${d.getUTCDate()} ${MON[d.getUTCMonth()]}`; };
const wdName = (day) => (isDay(day) ? WD[dayObj(day).getUTCDay()] : "");
/** Cairo calendar day of a timestamp */
function cDay(iso) { const p = cParts(iso); return p ? `${p.year}-${p.month}-${p.day}` : null; }
/** "14:05" in Cairo */
function fHM(iso) { const p = cParts(iso); return p ? `${p.hour}:${p.minute}` : "—"; }
/** "Sun 11 Oct 14:05" in Cairo */
function fTs(iso) { const d = cDay(iso); return d ? `${fDay(d)} ${fHM(iso)}` : "—"; }
/** Short and relative to today: "14:05", "yesterday 14:05", "Sat 10 Oct 14:05" */
function fWhen(iso) {
  if (!iso) return "—";
  const d = cDay(iso);
  if (d === today()) return `today ${fHM(iso)}`;
  if (d === yesterday()) return `yesterday ${fHM(iso)}`;
  return fTs(iso);
}
function rel(iso) {
  if (!iso) return "—";
  const s = (Date.parse(iso) - nowMs()) / 1000;
  const a = Math.abs(s);
  if (a < 45) return "just now";
  const t = a < 3600 ? `${Math.round(a / 60)} min` : a < 86400 ? `${Math.round(a / 3600)} h` : `${Math.round(a / 86400)} d`;
  return s < 0 ? `${t} ago` : `in ${t}`;
}
const relDays = (n) => (n === 0 ? "today" : n === 1 ? "tomorrow" : n === -1 ? "yesterday" : n > 0 ? `in ${n} d` : `${-n} d ago`);
const h1 = (v) => (v == null ? "—" : Number(v).toFixed(1));
const hU = (v) => (v == null ? "—" : `${Number(v).toFixed(1)} h`);
/** reprojection RMSE in px, always 2 decimals */
const px = (v) => (v == null || !Number.isFinite(Number(v)) ? "—" : Number(v).toFixed(2));
/** hours for a legend: a sliver like 0.01 h reads "<0.1 h", not "0.0 h" */
const hUs = (v) => (v > 0 && v < 0.05 ? "<0.1 h" : hU(v));
const pct = (v, d = 1) => (v == null ? "—" : `${Number(v).toFixed(d)}%`);
const int = (v) => (v == null ? "—" : Number(v).toLocaleString("en-US"));
const dur = (s) => {
  if (s == null) return "—";
  s = Math.round(s);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(x).padStart(2, "0")}` : `${m}:${String(x).padStart(2, "0")}`;
};
const gb = (b) => (b == null ? "—" : `${(b / 1e9).toFixed(1)} GB`);
const plural = (n, w, ws = w + "s") => `${int(n)} ${n === 1 ? w : ws}`;
const natural = (a, b) => String(a).localeCompare(String(b), "en", { numeric: true });
const statusOf = (x) => (x == null ? null : typeof x === "string" ? x : x.status || null);

/* ==================================================================== api == */

class ApiError extends Error {
  constructor(msg, status) { super(msg); this.status = status; }
}

const qs = (o) => {
  const p = Object.entries(o || {}).filter(([, v]) => v != null && v !== "");
  return p.length ? "?" + p.map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join("&") : "";
};

function apiUrl(name, p = {}) {
  if (DEMO) return `/demo/${name}.json`;
  switch (name) {
    case "status": return "/api/status";
    case "day": return "/api/day" + qs({ date: p.date });
    case "day_cap": return "/api/day/cap" + qs({ date: p.date, cap: p.cap });
    case "calibration": return "/api/calibration" + qs({ date: p.date });
    case "maintenance": return "/api/maintenance" + qs({ date: p.date });
    case "field": return "/api/field";
    case "pipeline": return "/api/pipeline";
    case "trends": return "/api/trends?days=30";
    case "cap": return `/api/cap/${encodeURIComponent(p.name)}`;
    case "camera": return `/api/camera/${encodeURIComponent(p.id)}`;
    case "notes": return "/api/notes" + qs({ subject_type: p.subject_type, subject_id: p.subject_id });
    case "summary": return "/api/summary" + qs({ date: p.date });
    case "units": return "/api/units";
    default: throw new Error(`unknown endpoint ${name}`);
  }
}
const cacheKey = (name, p) => `${name}${qs(p)}`;

async function get(name, p = {}) {
  let r;
  try {
    r = await fetch(apiUrl(name, p), { cache: "no-store" });
  } catch (e) {
    throw new ApiError("the tracker server isn't answering", 0);
  }
  let j = null;
  try { j = await r.json(); } catch (_) { /* not JSON */ }
  if (!r.ok) throw new ApiError(j?.error || `HTTP ${r.status}`, r.status);
  if (DEMO) j = demoPatch(name, p, j);
  S.cache.set(cacheKey(name, p), j);
  return j;
}

async function post(path, body) {
  if (DEMO) return demoPost(path, body || {});
  try {
    const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
    let j = {};
    try { j = await r.json(); } catch (_) { j = {}; }
    return { status: r.status, ok: r.ok, data: j };
  } catch (e) {
    return { status: 0, ok: false, data: { error: "the tracker server isn't answering" } };
  }
}

/* demo mode: the fixtures never change, so writes live in memory for this tab */
function demoPatch(name, p, j) {
  const d = S.demo;
  if (name === "status") {
    const running = Date.now() < d.syncUntil;
    j = structuredClone(j);
    j.sync.running = running;
    if (d.lastSync) j.sync.last_ok_at = d.lastSync;
  }
  if (["notes", "cap", "camera", "maintenance"].includes(name)) {
    j = structuredClone(j);
    const keep = (n) => !d.deleted.has(n.id);
    const mine = (st, id) => d.notes.filter((n) => (!st || n.subject_type === st) && (!id || n.subject_id === id));
    if (name === "notes") j.notes = [...mine(p.subject_type, p.subject_id), ...j.notes.filter(keep)];
    if (name === "cap") j.notes = [...mine("cap", j.cap), ...j.notes.filter(keep)];
    if (name === "camera") j.notes = [...mine("camera", j.camera), ...j.notes.filter(keep)];
    if (name === "maintenance") j.notes_recent = [...mine(), ...j.notes_recent.filter(keep)].slice(0, 12);
  }
  if (name === "summary" && d.written) j = { ...j, written_at: d.written };
  return j;
}

function demoPost(path, body) {
  const d = S.demo;
  const at = new Date(nowMs()).toISOString();
  const ok = (data) => Promise.resolve({ status: 200, ok: true, data });
  if (path === "/api/sync") {
    if (Date.now() < d.syncUntil) return ok({ started: false, running: true });
    d.syncUntil = Date.now() + 2600;
    setTimeout(() => { d.lastSync = new Date(nowMs()).toISOString(); }, 2500);
    return ok({ started: true });
  }
  if (path === "/api/notes") {
    const n = { id: ++d.nextId, subject_type: body.subject_type, subject_id: body.subject_id, created_at: at, text: body.text, status: body.status || null };
    d.notes.unshift(n);
    return ok(n);
  }
  const del = /^\/api\/notes\/(\d+)\/delete$/.exec(path);
  if (del) {
    d.deleted.add(Number(del[1]));
    d.notes = d.notes.filter((n) => n.id !== Number(del[1]));
    return ok({ ok: true });
  }
  if (path === "/api/summary/write") {
    if (d.written && !body.force) return Promise.resolve({ status: 409, ok: false, data: { error: "already written for this date" } });
    d.written = at;
    return ok({ ok: true, path: "~/Desktop/slack messages.md", written_at: at });
  }
  return Promise.resolve({ status: 404, ok: false, data: { error: "not in demo" } });
}

/* ================================================================= status == */

async function loadStatus() {
  const st = await get("status");
  S.status = st;
  const srv = Date.parse(st.now);
  if (!isNaN(srv)) S.skew = srv - Date.now();
  renderSyncChip();
  if (st.sync?.running && !S.syncing && !S.watching) watchSync();
  return st;
}

/* a sync someone else started (the 10-minute loop, another tab): follow it to the end */
async function watchSync() {
  S.watching = true;
  const before = S.status?.sync?.last_ok_at;
  try {
    for (let i = 0; i < 400 && S.status?.sync?.running; i++) {
      await new Promise((res) => setTimeout(res, 2000));
      const st = await get("status");
      S.status = st;
      renderSyncChip();
    }
  } catch (e) { renderSyncChip(e.message); }
  S.watching = false;
  if (S.status?.sync?.last_ok_at !== before) refresh();
}

function renderSyncChip(err) {
  const chip = $("#sync-chip");
  const st = S.status;
  const sy = st?.sync || {};
  chip.classList.remove("run", "err", "stale");
  let text, tip;
  if (err) {
    chip.classList.add("err");
    text = "server not answering";
    tip = String(err);
  } else if (S.syncing || sy.running) {
    chip.classList.add("run");
    text = "syncing…";
  } else if (sy.last_error) {
    chip.classList.add("err");
    text = `sync failed${sy.last_ok_at ? ` · last ok ${rel(sy.last_ok_at)}` : ""}`;
  } else if (sy.last_ok_at) {
    const age = (nowMs() - Date.parse(sy.last_ok_at)) / 60000;
    if (age > 30) chip.classList.add("stale");
    text = `synced ${rel(sy.last_ok_at)}`;
  } else {
    text = st?.has_data === false ? "no data yet" : "never synced";
  }
  chip.querySelector(".stext").textContent = text;
  if (!tip) {
    const rows = Object.entries(sy.sources || {}).map(([k, v]) => ({
      value: v.ok ? `${int(v.rows)} rows` : "failed", label: `${k}${v.ok ? ` · ${v.took_s ?? "?"} s` : ` · ${v.error || "error"}`}`,
      color: v.ok ? "var(--ok)" : "var(--bad)", kind: "rect",
    }));
    const notes = [];
    if (sy.last_error) notes.push(`Error: ${sy.last_error}`);
    if (sy.next_at && !sy.running) notes.push(`Next sync ${rel(sy.next_at)}. Click to sync now.`);
    tip = { title: sy.last_ok_at ? `Last good sync ${fTs(sy.last_ok_at)}` : "Sync", rows, note: notes.join(" ") };
  }
  chip.setAttribute("data-tip", typeof tip === "string" ? tip : JSON.stringify(tip));
  chip.setAttribute("aria-label", `${text}. Click to sync now.`);
}

async function startSync() {
  if (S.syncing) return;
  S.syncing = true;
  renderSyncChip();
  const r = await post("/api/sync");
  if (!r.ok && r.status !== 409) {
    S.syncing = false;
    renderSyncChip(r.data?.error || "couldn't start a sync");
    return;
  }
  const before = S.status?.sync?.last_ok_at;
  for (let i = 0; i < 400; i++) {
    await new Promise((res) => setTimeout(res, 1500));
    try { await loadStatus(); } catch (e) { break; }
    if (!S.status.sync.running) break;
  }
  S.syncing = false;
  renderSyncChip();
  if (S.status?.sync?.last_ok_at !== before) refresh();
}

/* every minute: keep "4 min ago" honest and pick up background syncs */
setInterval(async () => {
  if (S.syncing) return;
  const before = S.status?.sync?.last_ok_at;
  try { await loadStatus(); } catch (e) { renderSyncChip(e.message); return; }
  if (S.status?.sync?.last_ok_at !== before) refresh();
}, 60000);

/* ================================================================= router == */

const VIEWS = {};

function parseHash() {
  const h = location.hash.replace(/^#\/?/, "");
  const [name, ...rest] = h.split("/");
  const arg = rest.length ? decodeURIComponent(rest.join("/")) : null;
  if (!VIEWS[name]) return { name: "day", arg: null };
  if (name === "day" && arg && !isDay(arg)) return { name: "day", arg: null };
  if ((name === "cap" || name === "camera") && !arg) return { name: "day", arg: null };
  return { name, arg };
}

function setTab(name) {
  for (const a of $$(".tabs a")) {
    const on = a.dataset.tab === name;
    a.classList.toggle("on", on);
    if (on) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  }
}

let routeSeq = 0;
async function route({ keepScroll = false, background = false } = {}) {
  const r = parseHash();
  const seq = ++routeSeq;
  const sameView = S.route && S.route.name === r.name && S.route.arg === r.arg;
  S.route = r;
  setTab(VIEWS[r.name].tab);
  if (!background) closeDrawer();      // a refresh after a sync re-renders behind an open drawer
  const v = VIEWS[r.name];
  document.title = `${v.title(r.arg)} · StarCap Tracker`;
  if (!S.status) {
    main.innerHTML = skeleton(v.skel);
    try { await loadStatus(); } catch (e) {
      renderSyncChip(e.message);
      main.innerHTML = errorBox("Couldn't reach the tracker", e.message);
      return;
    }
  }
  if (S.status.has_data === false) { main.innerHTML = firstRun(); return; }
  const cached = v.cached ? v.cached(r.arg) : null;
  if (cached) {
    paint(v, r, cached, keepScroll || sameView);
  } else {
    C.disposeAll();
    main.innerHTML = skeleton(v.skel);
  }
  let slow = null;
  if (cached) slow = setTimeout(() => main.classList.add("refreshing"), 300);
  try {
    const data = await v.load(r.arg);
    if (seq !== routeSeq) return;
    if (!cached || JSON.stringify(data) !== JSON.stringify(cached)) paint(v, r, data, keepScroll || sameView || !!cached);
  } catch (e) {
    if (seq !== routeSeq) return;
    if (!cached) {
      C.disposeAll();
      main.innerHTML = errorBox(`Couldn't load ${v.title(r.arg)}`, e.message);
    }
  } finally {
    clearTimeout(slow);
    main.classList.remove("refreshing");
  }
}

function paint(v, r, data, keepScroll) {
  const y = window.scrollY;
  C.disposeAll();
  main.innerHTML = v.render(data, r.arg);
  v.mount?.(data, r.arg);
  if (keepScroll) window.scrollTo(0, y); else window.scrollTo(0, 0);
}

function refresh() { route({ keepScroll: true, background: true }); }

window.addEventListener("hashchange", () => route());

/* ================================================================= pieces == */

function skeleton(kind) {
  const tiles = `<div class="tiles t8">${'<div class="sk" style="height:84px"></div>'.repeat(8)}</div>`;
  const head = `<div class="page-head"><div style="width:260px"><div class="sk sk-line" style="width:180px;height:24px"></div><div class="sk sk-line" style="width:240px;margin-top:8px"></div></div></div>`;
  const block = (h) => `<div class="sk" style="height:${h}px;margin-top:14px"></div>`;
  if (kind === "grid") return head + `<div class="multiples">${'<div class="sk" style="height:200px"></div>'.repeat(8)}</div>`;
  return head + tiles + block(110) + block(360);
}

function errorBox(title, msg) {
  return `<div class="errbox" role="alert"><div><b>${esc(title)}.</b> ${esc(msg || "")}</div>
    <button class="ghost" type="button" data-act="retry">Retry</button></div>`;
}

function firstRun() {
  return `<div class="empty"><b>No data yet.</b> The first sync from production is running; it takes a minute or two.
    This page fills in on its own.</div>`;
}

function empty(msg, cls = "") { return `<div class="empty ${cls}">${msg}</div>`; }

/** A stat tile. o: {k, v, unit, cls, d, sub, tip, href} */
function tile(o) {
  const tag = o.href ? "a" : "div";
  return `<${tag} class="tile${o.alert ? " alert" : ""}"${o.href ? ` href="${esc(o.href)}"` : ""}${o.tip ? ` data-tip="${esc(o.tip)}" tabindex="0"` : ""}>
    <div class="k">${esc(o.k)}</div>
    <div class="v ${o.cls || ""}">${o.v}${o.unit ? `<small>${esc(o.unit)}</small>` : ""}</div>
    ${o.d ? `<div class="d">${o.d}</div>` : ""}${o.sub ? `<div class="sub">${o.sub}</div>` : ""}${o.why ? `<div class="why">${o.why}</div>` : ""}</${tag}>`;
}

const FLAG_ORDER = { bad: 0, warn: 1, info: 2 };
function pills(flags) {
  if (!flags?.length) return "";
  return `<div class="pills">${[...flags].sort((a, b) => FLAG_ORDER[a.severity] - FLAG_ORDER[b.severity])
    .map((f) => `<span class="pill ${esc(f.severity)}" data-tip="${esc(f.detail || f.label)}" tabindex="0">${esc(f.label)}</span>`).join("")}</div>`;
}

const STATUS = {
  ok: { label: "footage in", short: "in", cls: "ok" },
  out: { label: "still out", short: "out", cls: "out" },
  waiting: { label: "waiting for copy", short: "waiting", cls: "waiting" },
  footage_missing: { label: "footage missing", short: "missing", cls: "bad" },
  no_footage: { label: "no footage", short: "none", cls: "bad" },
};
const stBadge = (s) => { const x = STATUS[s] || { label: s || "—", cls: "" }; return `<span class="st ${x.cls}"><i></i><span class="lg">${esc(x.label)}</span><span class="sh" aria-hidden="true">${esc(x.short || x.label)}</span></span>`; };
const myTag = (s) => (s ? `<span class="tag st-${esc(s)}">${esc(s)}</span>` : "");
const capLink = (c) => (c ? `<a href="#/cap/${encodeURIComponent(c)}">${esc(c)}</a>` : "—");
const camLink = (c) => (c ? `<a class="mono" href="#/camera/${encodeURIComponent(c)}">${esc(c)}</a>` : "—");

/** Sortable table header cells. cols: [{key, label, num, sort:false}] */
function thead(cols, sort, table) {
  return `<thead><tr>${cols.map((c) => {
    if (c.sort === false) return `<th${c.num ? ' class="num"' : ""}>${esc(c.label)}</th>`;
    const on = sort && sort.key === c.key;
    const ar = on ? (sort.dir > 0 ? "▲" : "▼") : "";
    const aria = on ? (sort.dir > 0 ? "ascending" : "descending") : "none";
    return `<th class="sortable${c.num ? " num" : ""}" aria-sort="${aria}"><button class="sorter" type="button" data-act="sort" data-table="${table}" data-key="${c.key}">${esc(c.label)}<span class="ar">${ar}</span></button></th>`;
  }).join("")}</tr></thead>`;
}

function figure(id, title, { val = "", legend = "", note = "" } = {}) {
  return `<figure class="chart-card"><figcaption><span class="ttl">${esc(title)}</span>${val ? `<span class="val">${val}</span>` : ""}
    <span class="right">${note}<button class="tbl-btn" type="button" data-tbl hidden aria-pressed="false">Table</button></span></figcaption>
    ${legend}<div id="${id}"></div></figure>`;
}

const bucketLegend = (hours, total, nonzero = false) => C.legend(C.BUCKETS.filter((b) => !nonzero || (hours?.[b.key] || 0) > 0).map((b) => ({
  label: b.label, value: hUs(hours?.[b.key] || 0), color: b.color, hatch: b.hatch, kind: "rect",
  title: `${b.label}: ${C.BUCKET_HELP[b.key]}${total ? ` · ${((100 * (hours?.[b.key] || 0)) / total).toFixed(1)}% of hours` : ""}`,
})));
const bucketParts = (hours) => C.BUCKETS.map((b) => ({ key: b.key, label: b.label, color: b.color, hatch: b.hatch, value: hours?.[b.key] || 0, note: C.BUCKET_HELP[b.key] }));
const dayAxis = (end, n = 30) => Array.from({ length: n }, (_, i) => shiftDay(end, i - n + 1));

/* ==================================================================== day == */

const KPI_UP_GOOD = {
  caps_out: true, caps_recorded: true, caps_no_footage: false, hours_total: true, hours_good: true,
  good_pct: true, timing_pass_pct: true, fps30_hours: false, fps30_caps: false, expired_cal_hours: false,
};
const KPI_RATIO = new Set(["good_pct", "timing_pass_pct"]);

function delta(key, k, kind, prevDay, partial) {
  if (!k || k.value == null) return "";
  if (k.prev == null) return `<span class="flat">no ${esc(prevDay)} to compare</span>`;
  const dv = k.value - k.prev;
  const eps = kind === "int" ? 0.5 : 0.05;
  if (Math.abs(dv) < eps) return `<span class="flat">same as ${esc(prevDay)}</span>`;
  const up = dv > 0;
  // Hours and counts only grow as footage arrives, so on a partial day a rise is already
  // final in direction; only a drop can still change. caps_out is complete at check-out.
  const muted = partial && !KPI_RATIO.has(key) && key !== "caps_out" && dv < 0;
  const cls = muted ? "flat" : KPI_UP_GOOD[key] === up ? "good" : "badd";
  const mag = Math.abs(dv);
  const txt = kind === "int" ? int(Math.round(mag)) : kind === "h" ? `${mag.toFixed(1)} h` : `${mag.toFixed(1)} pt`;
  const tip = muted ? ` data-tip="${esc(`Still growing: footage for this day is still arriving, so this isn't a final comparison with ${prevDay}.`)}"` : "";
  return `<span class="${cls}"${tip}><span class="${up ? "up" : "down"}">${up ? "▲" : "▼"}</span> ${txt}</span> vs ${esc(prevDay)}`;
}

VIEWS.day = {
  tab: "day", skel: "day",
  title: (arg) => (arg ? fDay(arg) : "Day"),
  cached: (arg) => S.cache.get(cacheKey("day", { date: arg || yesterday() })),
  load: (arg) => get("day", { date: arg || yesterday() }),
  render: renderDay,
  mount: mountDay,
};

function renderDay(day, arg) {
  const date = day.date;
  const yd = yesterday(), before = shiftDay(yd, -1);
  const prevDay = wdName(shiftDay(date, -1));
  const a = day.arrival || {};
  const notIn = (a.still_out || 0) + (a.waiting || 0);
  const partial = notIn > 0 || day.is_today;
  const which = date === today() ? "Today" : date === yd ? "Yesterday" : date === before ? "Day before" : `${daysBetween(date, today())} days ago`;
  const k = day.kpis || {};
  const v = (key) => k[key]?.value;
  const nothing = (a.no_footage || 0) + (a.footage_missing || 0);
  const seg = `<div class="seg" role="group" aria-label="Pick a day">
      <a href="#/day" class="${date === yd ? "on" : ""}">Yesterday</a>
      <a href="#/day/${before}" class="${date === before ? "on" : ""}">Day before</a>
      <label class="${date !== yd && date !== before ? "on" : ""}"><span class="sr-only">Pick a date</span>
        <input type="date" id="day-pick" value="${esc(date)}" max="${esc(today())}"></label></div>`;
  const head = `<div class="page-head"><div><h1>${esc(fDay(date))}</h1>
      <div class="sub">${esc(which)} · ${partial ? `footage still arriving` : "all footage in, day is final"}${day.latest_version ? ` · latest firmware ${esc(day.latest_version)}` : ""}</div></div>
      <div class="ctrls">${seg}</div></div>`;
  if (!a.caps_out && !(day.caps || []).length) {
    return head + empty(`<b>No check-outs on this day.</b> Nothing went out on ${esc(fDay(date))}, so there is nothing to count.`);
  }
  const line = notIn || nothing
    ? `<b>${int(a.arrived)} of ${int(a.caps_out)}</b> caps' footage in${a.still_out ? ` · <b>${int(a.still_out)}</b> still out` : ""}${a.waiting ? ` · <b>${int(a.waiting)}</b> waiting for copy` : ""}${nothing ? ` · <b>${int(nothing)}</b> got nothing` : ""}`
    : `<b>All ${int(a.caps_out)}</b> caps' footage is in`;
  const arrival = `<section class="panel arrival" aria-label="Footage arrival">
      <div class="line">${line}</div>
      <div class="pct">${int(a.pct)}%<small>${partial ? "in so far" : "final"}</small></div>
      <div class="bar chart bar" id="c-arrival"></div>
      ${partial ? `<div class="note">Hours and counts keep growing until the rest is copied, so a drop vs ${esc(prevDay)} stays gray until the day is final.</div>` : ""}
      ${day.events_complete === false ? `<div class="note">Device logs here start ${day.events_from ? esc(fDay(day.events_from.slice(0, 10))) : "later"}: for this day the reboot, camera-drop and storage flags can't show, and a cap that came back empty reads "no footage" either way.</div>` : ""}</section>`;
  const nothingCaps = (day.caps || []).filter((r) => r.status === "no_footage" || r.status === "footage_missing").map((r) => r.cap);
  const T = (key, label, kind, extra = {}) => tile({ k: label, d: delta(key, k[key], kind, prevDay, partial), ...extra });
  const tiles = `<section class="tiles t8" aria-label="Key numbers">
      ${T("caps_out", "Caps out", "int", { v: int(v("caps_out")) })}
      ${T("caps_recorded", "Recorded", "int", { v: int(v("caps_recorded")), unit: "caps" })}
      ${T("caps_no_footage", "Got nothing", "int", { v: int(v("caps_no_footage")), cls: v("caps_no_footage") > 0 ? "bad" : "", sub: esc(nothingCaps.join(", ")), tip: `${int(a.no_footage || 0)} with no footage, ${int(a.footage_missing || 0)} recorded but nothing arrived` })}
      ${T("hours_total", "Hours recorded", "h", { v: h1(v("hours_total")), unit: "h" })}
      ${T("good_pct", "Good share", "pt", { v: pct(v("good_pct")), sub: `${h1(v("hours_good"))} h good`, tip: "Share of hours that are good: 60 fps, p99 frame timing within ±10 ms, clips of 30 s or more" })}
      ${T("timing_pass_pct", "Timing pass", "pt", { v: pct(v("timing_pass_pct")), tip: "Good hours over good + timing-fail hours (OpenAI p99 rule: ≤ 1% of frame gaps outside ±10 ms)" })}
      ${T("fps30_hours", "At 30 fps", "h", { v: h1(v("fps30_hours")), unit: "h", cls: v("fps30_hours") > 0 ? "bad" : "", sub: `on ${plural(v("fps30_caps") || 0, "cap")}`, tip: "Camera fell back to USB 2 and recorded at 30 fps" })}
      ${T("expired_cal_hours", "Cal expired", "h", { v: h1(v("expired_cal_hours")), unit: "h", cls: v("expired_cal_hours") > 0 ? "warn" : "", tip: "Hours shot on a camera whose calibration was 30+ days old at take start" })}
    </section>`;
  const hrs = day.hours || {};
  const hoursPanel = `<section class="panel" style="margin-top:14px">
      <div class="panel-h"><h3>Hours by bucket</h3><span class="right"><b class="tnum" style="color:var(--fg)">${hU(hrs.total)}</b> recorded · ${pct(hrs.total ? (100 * (hrs.good || 0)) / hrs.total : null)} good</span></div>
      <div class="chart bar" id="c-hours"></div>
      <div style="margin-top:10px">${bucketLegend(hrs, hrs.total)}</div></section>`;
  const ot = day.off_trip;
  const foot = ot && ot.takes ? `<p class="footnote">Off-trip, not counted above: ${plural(ot.takes, "take")} · ${hU(ot.hours)} on ${plural(ot.caps, "cap")} (bench, QC, office), counted by the day they were copied.</p>` : "";
  return `${head}${arrival}${tiles}${hoursPanel}
    <h2 class="sec">Caps <span class="sub">${plural((day.caps || []).length, "cap")} checked out · click a row for its takes and events</span></h2>
    <div class="toolbar"><div class="chips" role="group" aria-label="Filter caps" id="cap-chips"></div>
      <input class="search" type="search" id="cap-q" placeholder="Find a cap or camera" aria-label="Find a cap or camera" value="${esc(S.day.q)}"></div>
    <div class="tablewrap" id="caps-wrap"></div>${foot}`;
}

const CAP_FILTERS = [
  { key: "all", label: "All", test: () => true },
  { key: "problems", label: "Problems", test: (r) => r.severity === "bad" || r.severity === "warn" },
  { key: "nothing", label: "No footage", test: (r) => r.status === "no_footage" || r.status === "footage_missing" },
  { key: "out", label: "Still out", test: (r) => r.status === "out" },
  { key: "fps30", label: "30 fps", test: (r) => (r.flags || []).some((f) => f.code === "fps30") },
];
const STATUS_RANK = { no_footage: 0, footage_missing: 1, out: 2, waiting: 3, ok: 4 };
const SEV_RANK = { bad: 0, warn: 1, info: 2 };
const CAP_COLS = [
  { key: "cap", label: "Cap", get: (r) => r.cap, cmp: natural },
  { key: "camera", label: "Camera", get: (r) => r.camera || "", cmp: natural },
  { key: "status", label: "Status", get: (r) => STATUS_RANK[r.status] ?? 9 },
  { key: "hours", label: "Hours", num: true, get: (r) => r.hours?.total || 0, desc: true },
  { key: "good_pct", label: "Good %", num: true, get: (r) => r.good_pct ?? -1, desc: true },
  { key: "timing", label: "Timing", num: true, get: (r) => r.timing_pass_pct ?? -1, desc: true },
  { key: "fps", label: "FPS", num: true, get: (r) => r.fps ?? -1, desc: true },
  { key: "takes", label: "Takes", num: true, get: (r) => r.takes || 0, desc: true },
  { key: "flags", label: "Flags", get: (r) => (r.flags?.length ? SEV_RANK[r.severity] * 100 - r.flags.length : 999) },
];

function mountDay(day) {
  const a = day.arrival || {};
  if ($("#c-arrival")) {
    C.hstack($("#c-arrival"), {
      title: "Footage arrival", desc: `${a.arrived} of ${a.caps_out} caps' footage is in.`, height: 12, share: true,
      format: (v) => plural(v, "cap"),
      parts: [
        { label: "footage in", value: a.arrived || 0, color: "var(--s1)", note: "copied to the station" },
        { label: "got nothing", value: (a.no_footage || 0) + (a.footage_missing || 0), color: "var(--b-fps30)", note: `${a.no_footage || 0} no footage, ${a.footage_missing || 0} footage missing` },
        { label: "waiting for copy", value: a.waiting || 0, hatch: true, note: "returned, not copied yet" },
        { label: "still out", value: a.still_out || 0, color: "var(--line2)", note: "not returned yet" },
      ],
    });
  }
  if ($("#c-hours")) {
    const hrs = day.hours || {};
    C.hstack($("#c-hours"), {
      title: "Hours by bucket", desc: `${hU(hrs.total)} recorded, ${hU(hrs.good)} good.`, height: 14,
      format: hU, total: hrs.total, parts: bucketParts(hrs),
    });
  }
  $("#day-pick")?.addEventListener("change", (e) => { if (isDay(e.target.value)) location.hash = `#/day/${e.target.value}`; });
  const q = $("#cap-q");
  q?.addEventListener("input", () => { S.day.q = q.value; renderCapsTable(day); });
  renderCapsTable(day);
}

function renderCapsTable(day) {
  const rows = day.caps || [];
  const chips = $("#cap-chips");
  if (!chips) return;
  if (!CAP_FILTERS.some((f) => f.key === S.day.filter)) S.day.filter = "all";
  chips.innerHTML = CAP_FILTERS.map((f) => {
    const n = rows.filter(f.test).length;
    return `<button class="chip${S.day.filter === f.key ? " on" : ""}" type="button" data-act="cap-filter" data-key="${f.key}" aria-pressed="${S.day.filter === f.key}"${n || f.key === "all" ? "" : " disabled"}>${esc(f.label)} <b>${n}</b></button>`;
  }).join("");
  const f = CAP_FILTERS.find((x) => x.key === S.day.filter);
  const q = S.day.q.trim().toLowerCase();
  let list = rows.filter(f.test).filter((r) => !q || r.cap.toLowerCase().includes(q) || (r.cameras || [r.camera]).some((c) => String(c).toLowerCase().includes(q)));
  const sort = S.day.sort;
  if (sort) {
    const col = CAP_COLS.find((c) => c.key === sort.key);
    const cmp = col.cmp || ((x, y) => x - y);
    list = [...list].sort((x, y) => sort.dir * cmp(col.get(x), col.get(y)) || natural(x.cap, y.cap));
  }
  const maxH = Math.max(1, ...rows.map((r) => r.hours?.total || 0));
  const latest = day.latest_version;
  const body = list.map((r) => {
    const extraCams = (r.cameras || []).filter((c) => c !== r.camera);
    const ms = statusOf(r.my_status);
    const fpsCls = r.nominal_fps === 30 ? "cbad" : "";
    return `<tr class="click" tabindex="0" data-act="open-cap" data-cap="${esc(r.cap)}">
      <td><div class="capcell"><b>${esc(r.cap)}</b>${myTag(ms)}${r.open_tickets ? `<span class="tag" data-tip="${esc(plural(r.open_tickets, "open ticket"))}">${r.open_tickets} ticket${r.open_tickets > 1 ? "s" : ""}</span>` : ""}</div></td>
      <td class="mono">${esc(r.camera || "—")}${extraCams.length ? ` <span class="tag" data-tip="${esc("Also used: " + extraCams.join(", "))}">+${extraCams.length}</span>` : ""}</td>
      <td>${stBadge(r.status)}</td>
      <td class="num"><div class="hcell">${C.miniStack(r.hours, maxH, 76)}<span class="hv">${r.hours?.total ? h1(r.hours.total) : "—"}</span></div></td>
      <td class="num">${r.good_pct == null ? '<span class="mut">—</span>' : h1(r.good_pct)}</td>
      <td class="num">${r.timing_pass_pct == null ? '<span class="mut">—</span>' : h1(r.timing_pass_pct)}</td>
      <td class="num ${fpsCls}"${r.nominal_fps === 30 ? ' data-tip="Mostly 30 fps: camera on USB 2"' : ""}>${r.fps == null ? '<span class="mut">—</span>' : h1(r.fps)}</td>
      <td class="num">${r.takes ? int(r.takes) : '<span class="mut">—</span>'}</td>
      <td>${pills(r.flags)}${!r.flags?.length && r.version && latest && r.version !== latest ? "" : ""}</td></tr>`;
  }).join("");
  const wrap = $("#caps-wrap");
  const filtered = list.length !== rows.length;
  wrap.innerHTML = `<table class="t" id="caps-t">${thead(CAP_COLS, sort, "caps")}<tbody>${body ||
    `<tr><td colspan="9" class="mut" style="text-align:center;padding:22px">No caps match${q ? ` "${esc(S.day.q)}"` : ""}.</td></tr>`}</tbody></table>
    ${filtered || sort ? `<div class="tfoot"><span>${list.length} of ${rows.length} caps${sort ? ` · sorted by ${esc(CAP_COLS.find((c) => c.key === sort.key).label.toLowerCase())}` : ""}</span>
      <button class="link" type="button" data-act="caps-reset">Reset to server order</button></div>` : ""}`;
}

/* ------------------------------------------------------------- drawer -- */

const drawer = $("#drawer");
function closeDrawer() { if (drawer.open) drawer.close(); }
drawer.addEventListener("close", () => { C.hideTip(); drawerCharts.forEach((c) => c.ro.disconnect()); drawerCharts = []; });
drawer.addEventListener("click", (e) => { if (e.target === drawer) closeDrawer(); });
let drawerCharts = [];

const EVENT = {
  power_on: { label: "Power on", cls: "blu" },
  unclean_boot: { label: "Unclean reboot", cls: "warn" },
  camera_lost: { label: "Camera dropped", cls: "warn" },
  rec_dead: { label: "Recorder died", cls: "bad" },
  rec_corrupt: { label: "Corrupt take", cls: "warn" },
  camera_usb2: { label: "Camera on USB 2", cls: "bad" },
  disk_full: { label: "Disk full", cls: "bad" },
  rec_refused: { label: "Take refused", cls: "bad" },
  storage_stalled: { label: "Storage stalled", cls: "bad" },
  storage_lost: { label: "Storage lost", cls: "bad" },
  storage: { label: "Storage", cls: "bad" },
  rec_start: { label: "Take started", cls: "rec" },
  rec_stop: { label: "Take stopped", cls: "rec" },
};

async function openCapDrawer(cap) {
  const date = S.route?.name === "day" ? (S.route.arg || yesterday()) : yesterday();
  const row = (S.cache.get(cacheKey("day", { date }))?.caps || []).find((r) => r.cap === cap);
  const inner = $("#drawer-in");
  inner.innerHTML = `<div class="dr-head"><div class="row1"><h2 id="drawer-title">${esc(cap)}</h2>
      <button class="icon-btn x" type="button" data-act="close-drawer" aria-label="Close">✕</button></div>
      <div class="facts"><span>${esc(fDay(date))}</span></div></div>
    <div class="dr-body"><div class="sk" style="height:84px;margin-top:16px"></div><div class="sk" style="height:220px;margin-top:14px"></div></div>`;
  if (!drawer.open) drawer.showModal();
  inner.scrollTop = 0;
  try {
    const d = await get("day_cap", { date, cap });
    if (!drawer.open) return;
    inner.innerHTML = drawerHTML(d, row);
    mountDrawer(d);
  } catch (e) {
    inner.querySelector(".dr-body").innerHTML = `<div style="margin-top:16px">${errorBox(`Couldn't load ${cap}`, e.message)}</div>`;
  }
}

function drawerHTML(d, fallbackRow) {
  const r = d.row || fallbackRow || {};
  const takes = d.takes || [];
  const cams = (r.cameras?.length ? r.cameras : [r.camera]).filter(Boolean);
  const facts = [
    `<span>${esc(fDay(d.date))}</span>`,
    cams.length ? `<span>camera ${cams.map(camLink).join(", ")}</span>` : "",
    r.board ? `<span>${esc(r.board)}</span>` : "",
    r.version ? `<span>v<b>${esc(r.version)}</b></span>` : "",
    r.checkout_at ? `<span>out <b>${esc(fHM(r.checkout_at))}</b> → ${r.returned_at ? `back <b>${esc(cDay(r.returned_at) === cDay(r.checkout_at) ? fHM(r.returned_at) : fWhen(r.returned_at))}</b>` : "<b>not back</b>"}</span>` : "",
    r.task ? `<span>${esc(r.task)}</span>` : "",
    r.boots != null ? `<span>${plural(r.boots, "boot")}</span>` : "",
  ].join("");
  const hrs = r.hours || {};
  const ok = takes.filter((t) => t.bucket !== "short").length;
  const stats = `<div class="tiles t4" style="margin-top:16px">
      ${tile({ k: "Hours", v: h1(hrs.total), unit: "h", sub: `${pct(r.good_pct)} good` })}
      ${tile({ k: "Timing pass", v: pct(r.timing_pass_pct), sub: r.p99_dev_ms_max != null ? `worst p99 ${r.p99_dev_ms_max} ms` : "" })}
      ${tile({ k: "FPS", v: r.fps == null ? "—" : h1(r.fps), cls: r.nominal_fps === 30 ? "bad" : "", sub: r.nominal_fps ? `mostly ${r.nominal_fps} fps` : "" })}
      ${tile({ k: "Calibration", v: r.cal_days_left == null ? "—" : r.cal_days_left <= 0 ? `${-r.cal_days_left} d` : `${r.cal_days_left} d`, cls: r.cal_days_left != null && r.cal_days_left <= 0 ? "bad" : "", sub: r.cal_days_left == null ? "" : r.cal_days_left <= 0 ? "overdue" : "left on the fit" })}
    </div>`;
  const flags = (r.flags || []).length ? `<h2 class="sec">Flags</h2><ul class="flaglist">${[...r.flags].sort((a, b) => FLAG_ORDER[a.severity] - FLAG_ORDER[b.severity])
    .map((f) => `<li><span class="pill ${esc(f.severity)}">${esc(f.label)}</span><span>${esc(f.detail || "")}</span></li>`).join("")}</ul>` : "";
  const bucketCell = (t) => {
    const b = C.BUCKETS.find((x) => x.key === t.bucket);
    const extra = [t.fault ? `fault: ${t.fault}` : "", (t.codes || []).length ? `codes: ${t.codes.join(", ")}` : ""].filter(Boolean).join(" · ");
    return `<span class="st"${extra ? ` data-tip="${esc(extra)}"` : ""}><i class="sw rect${b?.hatch ? " hatch" : ""}" style="--k:${b?.color || "var(--mut)"};width:9px;height:9px;border-radius:2px"></i>${esc(b?.label || t.bucket)}</span>`;
  };
  const takeRows = takes.map((t) => `<tr>
      <td class="num">${esc(fHM(t.started_at))}${t.clock_fixed ? ` <span class="tag" data-tip="Start time corrected after NTP synced">NTP</span>` : ""}</td>
      <td class="num">${dur(t.recorded_s)}</td>
      <td>${bucketCell(t)}</td>
      <td class="num${t.nominal_fps === 30 ? " cbad" : ""}">${t.fps == null ? "—" : Number(t.fps).toFixed(2)}</td>
      <td class="num">${t.score == null ? '<span class="mut">—</span>' : Number(t.score).toFixed(2)}</td>
      <td class="num">${t.p99_dev_ms == null ? '<span class="mut">—</span>' : `${Number(t.p99_dev_ms).toFixed(1)}`}</td>
      <td class="mono">${esc(t.boot || "—")}${t.camera && !cams.includes(t.camera) ? ` <span class="tag">cam ${esc(t.camera)}</span>` : ""}</td>
      <td>${t.uploaded_at ? `<span class="cok" aria-hidden="true">✓</span> ${esc(fWhen(t.uploaded_at))}` : '<span class="mut">not yet</span>'}</td></tr>`).join("");
  const takesTable = takes.length ? `<div class="tablewrap mini"><table class="t"><thead><tr><th class="num">Start</th><th class="num">Length</th><th>Bucket</th><th class="num">FPS</th>
      <th class="num" title="Share of frame gaps inside ±10 ms">Timing %</th><th class="num">p99 ms</th><th>Boot</th><th>Uploaded</th></tr></thead><tbody>${takeRows}</tbody></table></div>`
    : empty(r.status === "out" ? "Still out. Takes show up once the cap is back and copied." : r.status === "waiting" ? "Back, waiting for the copy station." : "No takes for this cap on this day.", "sm");
  return `<div class="dr-head"><div class="row1"><h2 id="drawer-title">${esc(d.cap || r.cap)}</h2>${stBadge(r.status)}${myTag(statusOf(r.my_status))}
      <button class="icon-btn x" type="button" data-act="close-drawer" aria-label="Close">✕</button></div>
      <div class="facts">${facts}</div></div>
    <div class="dr-body">${stats}
      ${hrs.total ? `<div class="chart bar" id="c-dr-hours" style="margin-top:14px"></div><div style="margin-top:8px">${bucketLegend(hrs, hrs.total, true)}</div>` : ""}
      ${flags}
      <h2 class="sec">Takes <span class="sub">${plural(takes.length, "take")}${takes.length ? ` · ${ok} clips, ${takes.length - ok} short` : ""}</span></h2>${takesTable}
      <h2 class="sec">Events <span class="sub">times in Cairo</span></h2>${eventsTimeline(d.events || [])}
      <p class="footnote" style="display:flex;gap:16px;flex-wrap:wrap">${capLink(d.cap || r.cap).replace(">", ">Cap page: ")} ${cams.map((c) => `<a href="#/camera/${encodeURIComponent(c)}">Camera page: ${esc(c)}</a>`).join(" ")}</p>
    </div>`;
}

function eventsTimeline(events) {
  if (!events.length) return empty("No recorder events for this window.", "sm");
  // fold each take's start/stop into one quiet line
  const out = [];
  for (let i = 0; i < events.length; i++) {
    const e = events[i];
    if (e.type === "rec_start") {
      const j = events.findIndex((x, k) => k > i && x.type === "rec_stop");
      const between = j > 0 ? events.slice(i + 1, j).some((x) => x.type !== "rec_start" && x.type !== "rec_stop") : true;
      if (j > 0 && !between) {
        const s = (Date.parse(events[j].at) - Date.parse(e.at)) / 1000;
        out.push({ at: e.at, type: "take", msg: `${dur(s)}, until ${fHM(events[j].at)}` });
        events = events.slice(0, j).concat(events.slice(j + 1));
        continue;
      }
    }
    out.push(e);
  }
  return `<ul class="tl">${out.map((e) => {
    const m = e.type === "take" ? { label: "Take", cls: "rec" } : EVENT[e.type] || { label: e.type, cls: "" };
    return `<li class="${e.type === "power_on" ? "boot" : ""}"><span class="tm">${esc(fHM(e.at))}</span><span class="mk ${m.cls}"></span>
      <span class="ev"><b${m.cls === "rec" ? ' style="font-weight:500;color:var(--mut)"' : ""}>${esc(m.label)}</b>${e.msg && e.type !== "take" ? ` <span class="m">· ${esc(e.msg)}</span>` : e.type === "take" ? ` <span class="m">${esc(e.msg)}</span>` : ""}</span></li>`;
  }).join("")}</ul>`;
}

function mountDrawer(d) {
  const r = d.row || {};
  const el = $("#c-dr-hours");
  if (el && r.hours?.total) {
    drawerCharts.push(C.hstack(el, { title: `${d.cap} hours by bucket`, desc: `${hU(r.hours.total)} recorded.`, height: 16, format: hU, total: r.hours.total, parts: bucketParts(r.hours) }));
  }
}

/* ============================================================ calibration == */

VIEWS.calibration = {
  tab: "calibration", skel: "day",
  title: () => "Calibration",
  cached: () => S.cache.get(cacheKey("calibration", {})),
  load: () => get("calibration", {}),
  render: renderCal,
  mount: mountCal,
};

function whyText(q, { plan = true } = {}) {
  const bits = [];
  if (q.state === "overdue") bits.push(`<b class="cbad">overdue ${-q.days_left} d</b>`);
  else if (q.days_left === 0) bits.push(`<b>due today</b>`);
  else bits.push(`<b>due ${esc(relDays(q.days_left))}</b>`);
  if (q.out_now) bits.push(`out now on ${capLink(q.on_cap)}`);
  if (q.hours_7d >= 1) bits.push(`${Math.round(q.hours_7d)} h this week`);
  else if (!q.out_now) bits.push("idle this week");
  if (plan && q.late && q.state !== "overdue") bits.push(`<span class="cwarn">planned after its due date</span>`);
  return bits.join(" · ");
}

function renderCal(cal) {
  const t = cal.today || {}, c = cal.counts || {};
  const target = cal.target || S.status?.config?.cal_per_day || 10;
  const left = Math.max(0, target - (t.done || 0));
  const met = (t.done || 0) >= target;
  const tomorrow = shiftDay(cal.date, 1);
  const tmr = (cal.queue || []).filter((q) => q.plan_day === tomorrow);
  const fc = cal.forecast || [];
  const spike = (cal.spikes || [])[0];
  const ov = c.overdue || 0;
  const horizon = fc.length || 30;
  // clear_by = first day the plan ends with nothing overdue; behind_from = first day after
  // that when something is overdue again. A backlog that clears is calm, not red.
  const banners = [];
  if (ov > 0 && cal.clear_by) banners.push(`<div class="callout warn"><span><b>${int(ov)} overdue now</b> · at ${target} a day you're caught up by <b>${esc(fDay(cal.clear_by))}</b>.</span></div>`);
  else if (ov > 0) banners.push(`<div class="callout bad"><span><b>${int(ov)} overdue</b>, and ${target} a day doesn't catch up within ${horizon} days.</span></div>`);
  if (cal.behind_from) banners.push(`<div class="callout bad"><span><b>Falls behind again from ${esc(fDay(cal.behind_from))}.</b> More cameras come due than ${target} a day can take.</span></div>`);
  if (!banners.length) banners.push(`<div class="callout ok"><span>Nothing overdue, and at ${target} a day the plan keeps up for the next ${horizon} days.</span></div>`);
  const spikeLine = spike ? `<div class="callout warn"><span><b>Heads up:</b> ${int(spike.due)} cameras come due on ${esc(fDay(spike.date))}, ${esc(relDays(daysBetween(cal.date, spike.date)))}.${cal.spikes.length > 1 ? ` ${cal.spikes.length - 1} more spike day${cal.spikes.length > 2 ? "s" : ""} after that.` : ""}</span></div>` : "";
  const hero = `<section class="panel"><div class="hero2">
      <div class="top"><span class="big" aria-label="${t.done} of ${target} calibrated today">${int(t.done)}<small> / ${int(target)}</small></span>
        <span class="lbl">Calibrated today</span><span class="togo">${met ? '<span class="cok">✓ target met</span>' : `<b>${left}</b> to go`}</span></div>
      <div class="chart bar" id="c-today"></div>
      <div class="bk"><b>${int(t.recal)}</b> recal · <b>${int(t.new)}</b> first fit · <b>${int(t.imu)}</b> camera-IMU</div>
    </div></section>`;
  const slip = (cal.queue || []).filter((q) => q.state === "due_soon" && q.late).length;
  const tiles = `<div class="tiles t4">
      ${tile({ k: "Overdue", v: int(c.overdue), cls: c.overdue ? "bad" : "", sub: "30+ days since the fit" })}
      ${tile({ k: "Due within 7 days", v: int(c.due_soon), cls: c.due_soon ? "warn" : "", sub: slip ? `<span class="cwarn">${plural(slip, "camera")} will slip past due</span>` : "the plan reaches all in time",
        tip: `${int(c.late_in_plan || 0)} cameras in the plan are calibrated after their due date, ${int(c.overdue)} of them already overdue` })}
      ${tile({ k: "Never calibrated", v: int(c.never), sub: "not planned, see below" })}
      ${tile({ k: "Active cameras", v: int(c.active), sub: `${int(c.ok)} in date · ${int(c.broken)} broken · ${int(c.retired)} retired` })}
    </div>`;
  const todo = (list) => `<ol class="todo">${list.map((q) => `<li><a class="cam" href="#/camera/${encodeURIComponent(q.camera)}">${esc(q.camera)}</a>
      <span class="why">${whyText(q)}</span></li>`).join("")}</ol>`;
  const chips = (list) => `<div class="tmr">${list.map((q) => `<a class="${q.state === "overdue" ? "over" : ""}" href="#/camera/${encodeURIComponent(q.camera)}"
      data-tip="${esc(whyText(q).replace(/<[^>]+>/g, ""))}">${esc(q.camera)}</a>`).join("")}</div>`;
  const cwb = cal.catch_when_back || [];
  const cwbOver = cwb.filter((q) => q.days_left <= 0);
  const cwbSoon = cwb.filter((q) => q.days_left > 0);
  const catchTip = (q) => [q.state === "overdue" || q.days_left <= 0 ? `overdue ${-q.days_left} d` : `due ${relDays(q.days_left)}`,
    q.hours_7d >= 1 ? `${Math.round(q.hours_7d)} h this week` : "", q.plan_day ? `in the plan for ${fDay(q.plan_day)} if it's back` : ""].filter(Boolean).join(" · ");
  const catchChip = (q) => `<span class="cchip${q.days_left <= 0 ? " over" : ""}" data-tip="${esc(catchTip(q))}">${camLink(q.camera)}<span class="on">on</span>${capLink(q.on_cap)}</span>`;
  const catchBlock = cwb.length ? `<div class="todo-h">Catch when back · ${plural(cwbOver.length, "overdue camera")} out in the field</div>
      ${cwbOver.length ? `<div class="tmr">${cwbOver.map(catchChip).join("")}</div>` : '<div class="mut" style="font-size:12.5px">None of them is overdue yet.</div>'}
      ${cwbSoon.length ? `<details class="more tight"><summary>+${cwbSoon.length} due within 7 days</summary><div class="tmr">${cwbSoon.map(catchChip).join("")}</div></details>` : ""}` : "";
  const planN = (cal.plan_today || []).length;
  const idleMsg = met ? `<b>Done for today.</b> ${int(t.done)} calibrated, the target is ${target}.`
    : cwbOver.length ? "Nothing on the bench now: the overdue cameras are out in the field." : "Nothing due or pullable today.";
  const doToday = `<section class="panel">
      <div class="panel-h"><h3>Do today</h3><span class="right">${planN ? `${plural(planN, "camera")} left in today's plan` : met ? "target met" : "nothing on the bench"}</span></div>
      ${planN ? todo(cal.plan_today) : empty(idleMsg, "sm")}
      ${tmr.length ? `<div class="todo-h">Then tomorrow · ${esc(fDay(tomorrow))} · ${plural(tmr.length, "camera")}</div>${chips(tmr)}` : ""}
      ${catchBlock}
      <p class="footnote" style="margin-top:10px">Overdue cameras in use first (busiest first), then the idle ones, then the next due. Cameras out in the field wait until they're back.</p></section>`;
  const forecast = figure("c-forecast", "Coming due, next 30 days", {
      legend: C.legend([{ label: "cameras coming due", color: "var(--s1)" }, { label: "overdue at day end, with the plan", color: "var(--bad)", kind: "line" }, { label: `capacity ${target} a day`, color: "var(--ref)", kind: "dash" }]),
    });
  const all = cal.queue || [];
  const shown = S.queueAll ? all : all.slice(0, 12);
  const qRows = shown.map((q, i) => `<tr>
      <td class="num mut">${i + 1}</td><td>${camLink(q.camera)}</td>
      <td>${q.state === "overdue" ? `<span class="pill bad">overdue ${-q.days_left} d</span>` : `<span class="pill warn">${q.days_left === 0 ? "due today" : `due in ${q.days_left} d`}</span>`}</td>
      <td class="num nw">${esc(fDayShort(q.due_on))}</td>
      <td class="nw">${esc(fWhen(q.last_cal_at))}</td><td class="nw">${esc(q.bench || "—")}</td>
      <td class="nw">${q.out_now ? `out on ${capLink(q.on_cap)}` : '<span class="mut">in office</span>'}</td>
      <td class="num">${q.hours_7d ? h1(q.hours_7d) : '<span class="mut">0</span>'}</td>
      <td>${q.plan_day ? `${esc(q.plan_day === cal.date ? "today" : q.plan_day === tomorrow ? "tomorrow" : fDay(q.plan_day))}${q.late && q.state !== "overdue" ? ' <span class="pill warn">late</span>' : ""}` : '<span class="mut">not in 30 d</span>'}</td></tr>`).join("");
  const queue = all.length ? `<div class="tablewrap"><table class="t"><thead><tr><th class="num">#</th><th>Camera</th><th>State</th><th class="num">Due</th><th>Last fit</th><th>Last bench</th><th>Where</th><th class="num">Hours 7 d</th><th>Planned</th></tr></thead>
      <tbody>${qRows}</tbody></table>${all.length > 12 ? `<div class="tfoot"><span>${S.queueAll ? `all ${all.length}` : `12 of ${all.length}`} in plan order</span><button class="link" type="button" data-act="queue-all">${S.queueAll ? "Show fewer" : `Show all ${all.length}`}</button></div>` : ""}</div>`
    : empty("<b>Nothing overdue or due within a week.</b>");
  const fits = t.fits || [];
  const gap = (f) => (f.previous_at ? `${Math.round((Date.parse(f.calibrated_at) - Date.parse(f.previous_at)) / 864e5)} d` : "first");
  const fitRows = fits.map((f) => `<tr><td class="num">${esc(fHM(f.calibrated_at))}</td><td class="nw">${camLink(f.camera)}${f.first_ever ? ' <span class="pill blu">first fit</span>' : ""}</td>
      <td class="nw">${esc(f.bench || "—")}</td><td class="num">${px(f.rmse_left)}</td><td class="num">${px(f.rmse_right)}</td><td class="num">${px(f.rmse_stereo)}</td>
      <td class="num">${f.baseline_mm == null ? "—" : Number(f.baseline_mm).toFixed(2)}</td>
      <td>${f.verdict === "PASS" ? '<span class="pill ok">PASS</span>' : f.verdict ? `<span class="pill bad">${esc(f.verdict)}</span>` : '<span class="mut">—</span>'}</td>
      <td>${f.evidence === "complete" ? '<span class="mut">complete</span>' : f.evidence ? `<span class="cwarn">${esc(f.evidence)}</span>` : '<span class="mut">—</span>'}</td>
      <td class="num mut"${f.previous_at ? ` data-tip="${esc(`previous fit ${fTs(f.previous_at)}`)}"` : ""}>${esc(gap(f))}</td></tr>`).join("");
  const imuFits = (t.imu_fits || []).map((f) => `<tr><td class="num">${esc(fHM(f.calibrated_at))}</td><td>${camLink(f.camera)} <span class="pill blu">camera-IMU</span></td><td colspan="8" class="mut">camera-IMU fit</td></tr>`).join("");
  const fitsTable = fits.length || imuFits ? `<div class="tablewrap"><table class="t"><thead><tr><th class="num">Time</th><th>Camera</th><th>Bench</th>
      <th class="num" data-tip="Left eye reprojection RMSE, px">L px</th><th class="num" data-tip="Right eye reprojection RMSE, px">R px</th><th class="num" data-tip="Stereo RMSE, px">Stereo</th>
      <th class="num">Baseline mm</th><th>Verdict</th><th>Evidence</th><th class="num" data-tip="Days since this camera's previous fit">Gap</th></tr></thead><tbody>${fitRows}${imuFits}</tbody></table></div>`
    : empty("No fits yet today.", "sm");
  const cov = cal.coverage || {};
  const coverage = `<div class="tiles t3">
      ${tile({ k: "In-date calibration", v: pct(cov.valid_pct), sub: `${int(cov.valid)} of ${int(cov.active)} active cameras` })}
      ${tile({ k: "Camera-IMU calibration", v: `${int(cov.imu)}`, unit: `of ${int(cov.active)}`, cls: (cov.imu_pct || 0) < 100 ? "warn" : "ok", alert: (cov.imu_pct || 0) < 100,
        why: cov.imu ? `${int(cov.imu)} (${pct(cov.imu_pct)}) have one. OpenAI requires a camera-IMU calibration for every camera.`
          : "None have one yet. OpenAI requires a camera-IMU calibration for every camera." })}
      ${tile({ k: "Camera-IMU fit older than stereo", v: int(cov.imu_outdated), cls: cov.imu_outdated ? "warn" : "", sub: "re-run after a stereo recal" })}
    </div>`;
  const nev = cal.never || [];
  const never = nev.length ? `<div class="tablewrap"><table class="t"><thead><tr><th>Camera</th><th>Added</th><th class="num">Hours 7 d</th><th>Where</th></tr></thead><tbody>
      ${nev.map((n) => `<tr><td>${camLink(n.camera)}</td><td>${esc(fWhen(n.created_at))}</td><td class="num">${n.hours_7d ? h1(n.hours_7d) : '<span class="mut">0</span>'}</td>
        <td>${n.out_now ? '<span class="pill bad">out now, uncalibrated</span>' : '<span class="mut">in office</span>'}</td></tr>`).join("")}</tbody></table></div>`
    : empty("Every active camera has a fit.", "sm");
  const broken = (cal.broken || []).length ? `<div class="panel"><div>${[...cal.broken].sort(natural).map((b) => `<a class="chiplink" href="#/camera/${encodeURIComponent(b)}">${esc(b)}</a>`).join("")}</div>
      <p class="footnote" style="margin:4px 0 0">Marked broken in maintenance; never planned.</p></div>` : empty("No broken cameras.", "sm");
  return `<div class="page-head"><div><h1>Calibration</h1><div class="sub">${esc(fDay(cal.date))} · target ${target} a day · fits stay valid ${S.status?.config?.recal_days || 30} days</div></div></div>
    <div class="g48 stretch">${hero}${tiles}</div>
    <div class="banners">${banners.join("")}${spikeLine}</div>
    <div class="g57 stretch" style="margin-top:14px">${doToday}${forecast}</div>
    <h2 class="sec">Queue <span class="sub">overdue and due within 7 days, in plan order</span></h2>${queue}
    <div class="g75">
      <div><h2 class="sec">Today's fits <span class="sub">${plural(fits.length, "fit")}</span></h2>${fitsTable}</div>
      <div><h2 class="sec">Fits per day <span class="sub">last 30 days</span></h2>${figure("c-hist", "Fits per day", {
        legend: C.legend([{ label: "recal", color: "var(--s1)" }, { label: "first fit", color: "var(--s2)" }, { label: "camera-IMU", color: "var(--s3)" }, { label: `target ${target}`, color: "var(--ref)", kind: "dash" }]),
      })}</div>
    </div>
    <h2 class="sec">Coverage</h2>${coverage}
    <div class="grid2"><div><h2 class="sec">Never calibrated <span class="sub">${plural(nev.length, "camera")}</span></h2>${never}</div>
      <div><h2 class="sec">Broken <span class="sub">${plural((cal.broken || []).length, "camera")}</span></h2>${broken}</div></div>`;
}

function mountCal(cal) {
  const t = cal.today || {};
  const target = cal.target || 10;
  C.hstack($("#c-today"), {
    title: "Today's calibrations", desc: `${t.done} of ${target}.`, height: 10, share: false,
    total: Math.max(target, t.done || 0), format: (v) => plural(v, "camera"),
    parts: [
      { label: "recal", value: t.recal || 0, color: "var(--s1)" },
      { label: "first fit", value: t.new || 0, color: "var(--s2)" },
      { label: "camera-IMU", value: t.imu || 0, color: "var(--s3)" },
      { label: "still to do", value: Math.max(0, target - (t.done || 0)), color: "var(--line2)" },
    ],
  });
  const spikes = new Set((cal.spikes || []).map((s) => s.date));
  C.columns($("#c-forecast"), {
    title: "Cameras coming due, next 30 days",
    desc: `Bars: cameras coming due each day. Line: overdue at the end of each day if ${target} a day are done in plan order. Dashed: capacity.`,
    data: cal.forecast || [], height: 220,
    xLabel: (d) => fDayShort(d.date), xTitle: (d) => fDay(d.date),
    series: [{ key: "due", label: "coming due", color: "var(--s1)", value: (d) => d.due }],
    lines: [{ key: "overdue_end", label: "overdue at day end", color: "var(--bad)", value: (d) => d.overdue_end }],
    refs: [{ value: target, label: `capacity ${target}` }],
    peak: (d) => (spikes.has(d.date) ? `${d.due} due ${fDayShort(d.date)}` : null),
    tipExtra: (d) => [{ value: int(d.planned), label: "in the plan that day", kind: "dash", color: "var(--ref)" }],
    table: { columns: [{ label: "Day", value: (d) => fDay(d.date) }, { label: "Coming due", value: (d) => d.due, num: true }, { label: "Planned", value: (d) => d.planned, num: true }, { label: "Overdue at end", value: (d) => d.overdue_end, num: true }] },
  });
  const td = cal.date;
  C.columns($("#c-hist"), {
    title: "Fits per day, last 30 days", desc: `Stacked recal, first fit and camera-IMU fits per day, against a target of ${target}.`,
    data: cal.history || [], height: 220,
    xLabel: (d) => fDayShort(d.date), xTitle: (d) => fDay(d.date),
    series: [
      { key: "recal", label: "recal", color: "var(--s1)", value: (d) => d.recal },
      { key: "new", label: "first fit", color: "var(--s2)", value: (d) => d.new },
      { key: "imu", label: "camera-IMU", color: "var(--s3)", value: (d) => d.imu },
    ],
    refs: [{ value: target, label: `target ${target}` }],
    partial: (d) => d.date === td,
    peak: (d) => { const n = d.recal + d.new + d.imu; return n >= target * 2 ? String(n) : null; },
    table: { columns: [{ label: "Day", value: (d) => fDay(d.date) }, { label: "Recal", value: (d) => d.recal, num: true }, { label: "First fit", value: (d) => d.new, num: true }, { label: "Camera-IMU", value: (d) => d.imu, num: true }] },
  });
}

/* ============================================================ maintenance == */

VIEWS.maintenance = {
  tab: "maintenance", skel: "day",
  title: () => "Maintenance",
  cached: () => S.cache.get(cacheKey("maintenance", {})),
  load: () => get("maintenance", {}),
  render: renderMaint,
  mount: mountMaint,
};

const MY = ["repair", "dead", "lost", "watch"];

function renderMaint(m) {
  const st = m.stats || {};
  const dn = wdName(m.date);
  const links = m.links || {};
  const tiles = `<div class="tiles t8">
      ${tile({ k: "Open now", v: int(st.open), tip: "Open right now; the trend below shows each day's end" })}
      ${tile({ k: "Critical", v: int(st.critical), cls: st.critical ? "bad" : "" })}
      ${tile({ k: `Opened ${dn}`, v: int(st.opened) })}
      ${tile({ k: `Resolved ${dn}`, v: int(st.resolved) })}
      ${tile({ k: "Median to resolve", v: st.median_resolve_days == null ? "—" : h1(st.median_resolve_days), unit: "d", sub: "resolved in the last 30 d" })}
      ${tile({ k: "Oldest open", v: st.oldest_open_days == null ? "—" : int(Math.round(st.oldest_open_days)), unit: "d" })}
      ${tile({ k: "Blocked caps", v: int(st.blocked_caps), tip: (m.blocked || []).join(", ") || "none" })}
      ${tile({ k: "Broken cameras", v: int(st.broken_cams), tip: (m.broken_cams || []).join(", ") || "none" })}
    </div>`;
  const na = m.needs_attention || [];
  // a dense table, not cards: real days put ~30 caps here, and it is a work list to go down
  const d3 = [shiftDay(m.date, -2), shiftDay(m.date, -1), m.date];
  const nBad = na.filter((a) => a.severity === "bad").length;
  const attn = na.length ? `<div class="tablewrap"><table class="t attn-t"><thead><tr><th>Cap</th><th>Camera</th><th>Flags <span class="mut" style="font-weight:400">· hover for details</span></th>
      <th class="d3" data-tip="Which of the last 3 days it was flagged">${d3.map((d) => `<span>${esc(wdName(d))}</span>`).join("")}</th><th class="nw"></th></tr></thead><tbody>
      ${na.map((a) => `<tr class="sev-${esc(a.severity || "")}">
        <td class="nw"><a class="capb" href="#/cap/${encodeURIComponent(a.cap)}">${esc(a.cap)}</a></td>
        <td>${camLink(a.camera)}</td>
        <td>${pills(a.flags)}</td>
        <td class="d3" aria-label="${esc(`flagged ${a.days.map(fDay).join(", ")}`)}">${d3.map((d) => `<i class="${a.days.includes(d) ? "on" : ""}" title="${esc(fDay(d))}"></i>`).join("")}</td>
        <td class="nw acts">${links.file ? `<a class="act" href="${esc(links.file)}" target="_blank" rel="noopener">File ticket ↗</a>` : ""}<button class="act" type="button" data-act="note-prefill" data-subj="${esc(a.cap)}">Add note</button></td></tr>`).join("")}
      </tbody></table><div class="tfoot"><span>${plural(na.length, "cap")} · ${int(nBad)} with a bad flag · worst first</span></div></div>`
    : empty("<b>Nothing needs attention.</b> Every flagged cap has a ticket or a status.", "sm");
  const byStatus = Object.fromEntries(MY.map((s) => [s, []]));
  for (const x of m.my_status || []) if (byStatus[x.status]) byStatus[x.status].push(x);
  const board = `<div class="board">${MY.map((s) => `<div class="bcol"><h4>${esc(s)} <b>${byStatus[s].length}</b></h4>${byStatus[s].length
      ? `<ul>${byStatus[s].map((x) => `<li>${x.subject_type === "camera" ? camLink(x.subject_id) : capLink(x.subject_id)} <span class="mut">${esc(rel(x.since))}</span>
          <span class="t" data-tip="${esc(x.text || "")}">${esc(x.text || "")}</span></li>`).join("")}</ul>` : '<div class="none">none</div>'}</div>`).join("")}</div>`;
  const open = m.open || [];
  const shownT = S.ticketsAll ? open : open.slice(0, 15);
  const tRows = shownT.map((t) => `<tr>
      <td>${t.critical ? '<span class="pill bad">critical</span>' : '<span class="pill info">normal</span>'}</td>
      <td class="nw">${t.subject === "camera" ? `camera ${camLink(t.unit)}` : capLink(t.unit)}${t.subject !== "camera" && t.camera ? ` <span class="mut">· cam ${esc(t.camera)}</span>` : ""}</td>
      <td><span class="tag">${esc(t.category || "—")}</span></td>
      <td class="desc"><span class="clamp" data-tip="${esc(t.description || "")}">${esc(t.description || "")}</span></td>
      <td class="mut">${esc(t.reporter || "—")}</td>
      <td class="num${t.age_days >= 14 ? " cwarn" : ""}">${int(t.age_days)} d</td>
      <td class="num mut">${t.photos ? int(t.photos) : ""}</td></tr>`).join("");
  const tickets = open.length ? `<div class="tablewrap"><table class="t"><thead><tr><th>Priority</th><th>Unit</th><th>Category</th><th>Description</th><th>Reporter</th><th class="num">Age</th><th class="num" data-tip="Photos attached">Ph.</th></tr></thead>
      <tbody>${tRows}</tbody></table>${open.length > 15 ? `<div class="tfoot"><span>${S.ticketsAll ? `all ${open.length}` : `15 of ${open.length}`}, critical first</span>
      <button class="link" type="button" data-act="tickets-all">${S.ticketsAll ? "Show fewer" : `Show all ${open.length}`}</button></div>` : ""}</div>` : empty("<b>No open tickets.</b>");
  const rep = m.repeat || [];
  const repeat = rep.length ? `<div class="tablewrap"><table class="t"><thead><tr><th>Unit</th><th class="num">Tickets 30 d</th><th class="num">Open</th><th>Last</th></tr></thead><tbody>
      ${rep.map((r) => `<tr><td>${r.subject === "camera" ? `camera ${camLink(r.unit)}` : capLink(r.unit)}</td><td class="num">${int(r.tickets_30d)}</td><td class="num">${int(r.open)}</td><td class="mut">${esc(rel(r.last_at))}</td></tr>`).join("")}
      </tbody></table></div>` : empty("No unit has more than one ticket this month.", "sm");
  const cats = m.by_category || [];
  const cmax = Math.max(1, ...cats.map((c) => c.open));
  const catList = cats.length ? `<div class="panel"><ul class="catbars">${cats.map((c) => `<li data-tip="${esc(`${c.category}: ${c.open} open · ${c.last_30d} in the last 30 days`)}">
      <span>${esc(c.category)}</span><span class="tr"><i style="width:${(100 * c.open) / cmax}%"></i></span><span class="n">${int(c.open)}</span></li>`).join("")}</ul>
      <p class="footnote" style="margin:10px 0 0">Open tickets by category; hover for the 30-day count.</p></div>` : empty("No categories.", "sm");
  const res = m.recent_resolved || [];
  const resolved = res.length ? `<div class="panel" style="padding:4px 16px"><ul class="notes">${res.map((r) => `<li><div class="meta">${r.subject === "camera" ? camLink(r.unit) : capLink(r.unit)}<span>${esc(fWhen(r.resolved_at))}</span><span class="tag">${esc(r.category)}</span></div>
      <div class="txt">${esc(r.resolution_note || r.description || "")}</div></li>`).join("")}</ul></div>` : empty("Nothing resolved lately.", "sm");
  return `<div class="page-head"><div><h1>Maintenance</h1><div class="sub">${esc(fDay(m.date))} · tickets from starcap.dev, notes and status are yours (kept on this laptop)</div></div>
      <div class="ctrls">${links.tickets ? `<a class="btnlike" style="padding:6px 13px;border-radius:9px" href="${esc(links.tickets)}" target="_blank" rel="noopener">All tickets on starcap.dev ↗</a>` : ""}</div></div>
    ${tiles}
    <h2 class="sec">Needs attention <span class="sub">a bad flag, or warnings on 2 of the last 3 days · no ticket, no status from you</span></h2>${attn}
    <h2 class="sec">Open tickets <span class="sub">critical first, then oldest</span>${links.tickets ? `<a class="right" href="${esc(links.tickets)}" target="_blank" rel="noopener">starcap.dev/tickets ↗</a>` : ""}</h2>${tickets}
    <div class="g3">
      <div><h2 class="sec">Repeat offenders <span class="sub">2+ tickets in 30 d</span></h2>${repeat}</div>
      <div><h2 class="sec">By category</h2>${catList}</div>
      <div><h2 class="sec">Recently resolved</h2>${resolved}</div>
    </div>
    <h2 class="sec">My status board <span class="sub">your latest status per unit; "ok" clears it</span></h2>${board}
    <div class="g57">
      <div><h2 class="sec">Add a note</h2><div class="panel">${noteForm(null)}</div></div>
      <div><h2 class="sec">Recent notes</h2><div class="panel" style="padding:4px 16px">${notesList(m.notes_recent || [], true)}</div></div>
    </div>
    <h2 class="sec">Open tickets, last 30 days</h2>${figure("c-mtrend", "Open tickets at day end", { val: `<b>${int(st.open)}</b> now` })}`;
}

function mountMaint(m) {
  const tr = m.trend || [];
  if ($("#c-mtrend")) {
    C.lines($("#c-mtrend"), {
      title: "Open tickets at day end, last 30 days", desc: `Open tickets went from ${tr[0]?.open ?? "?"} to ${tr[tr.length - 1]?.open ?? "?"}.`,
      data: tr, height: 170, xLabel: (d) => fDayShort(d.date), xTitle: (d) => fDay(d.date),
      series: [{ key: "open", label: "open", color: "var(--s1)", value: (d) => d.open, area: true }],
      tipExtra: (d) => [{ value: `+${d.opened}`, label: "opened", kind: "none" }, { value: `−${d.resolved}`, label: "resolved", kind: "none" }],
      valueFormat: int,
      table: { columns: [{ label: "Day", value: (d) => fDay(d.date) }, { label: "Open", value: (d) => d.open, num: true }, { label: "Opened", value: (d) => d.opened, num: true }, { label: "Resolved", value: (d) => d.resolved, num: true }] },
    });
  }
  wireNoteForm(null);
}

/* ------------------------------------------------------------------ notes -- */

function notesList(notes, withSubject) {
  if (!notes.length) return `<div class="mut" style="padding:12px 0;font-size:13px">No notes yet.</div>`;
  return `<ul class="notes">${notes.map((n) => `<li data-note="${n.id}">
      <div class="meta">${withSubject ? (n.subject_type === "camera" ? `camera ${camLink(n.subject_id)}` : capLink(n.subject_id)) : ""}${myTag(n.status)}
        <span data-tip="${esc(fTs(n.created_at))}">${esc(fWhen(n.created_at))}</span></div>
      <div class="txt">${esc(n.text)}</div>
      <button class="icon-btn del" type="button" data-act="note-del" data-id="${n.id}" aria-label="Delete this note">✕</button></li>`).join("")}</ul>`;
}

/** subject null = free picker (maintenance); else {type, id} fixed (cap/camera page) */
function noteForm(subject) {
  return `<form class="form" id="note-form" autocomplete="off" novalidate>
    <div class="row">
      ${subject ? "" : `<label class="f">Cap or camera<div class="combo"><input class="inp" id="nf-subj" role="combobox" aria-expanded="false" aria-autocomplete="list"
          aria-controls="nf-list" placeholder="starcap12 or 43.1" spellcheck="false"><span class="hint" id="nf-kind"></span><ul id="nf-list" role="listbox" hidden></ul></div></label>`}
      <label class="f">Status<select class="inp" id="nf-status"><option value="">no status</option><option value="watch">watch</option><option value="repair">repair</option>
        <option value="dead">dead</option><option value="lost">lost</option><option value="ok">ok (clears the status)</option></select></label>
    </div>
    <label class="f">Note<textarea class="inp" id="nf-text" rows="2" placeholder="What happened, what's next"></textarea></label>
    <div class="row"><button type="submit">Add note</button><span class="msg" id="nf-msg" role="status"></span></div></form>`;
}

async function ensureUnits() {
  if (S.units) return S.units;
  try { S.units = await get("units"); } catch (e) { S.units = { caps: [], cameras: [] }; }
  return S.units;
}

function subjectOf(v) {
  v = (v || "").trim();
  if (!v) return null;
  const u = S.units || { caps: [], cameras: [] };
  if (u.caps.includes(v)) return { type: "cap", id: v };
  if (u.cameras.includes(v)) return { type: "camera", id: v };
  if (/^starcap\d+$/i.test(v)) return { type: "cap", id: v.toLowerCase(), unknown: true };
  if (/^\d+(\.\d+)?$/.test(v)) return { type: "camera", id: v, unknown: true };
  return null;
}

function wireNoteForm(subject) {
  const form = $("#note-form");
  if (!form) return;
  const msg = $("#nf-msg");
  const inp = $("#nf-subj");
  // the view re-renders after a save; keep its confirmation on screen for a moment
  if (S.noteFlash && Date.now() - S.noteFlash.at < 6000) { msg.className = "msg ok"; msg.textContent = S.noteFlash.text; }
  if (inp) {
    const list = $("#nf-list"), kind = $("#nf-kind");
    let items = [], active = -1;
    const close = () => { list.hidden = true; inp.setAttribute("aria-expanded", "false"); inp.removeAttribute("aria-activedescendant"); active = -1; };
    const paint = () => {
      list.innerHTML = items.map((it, i) => `<li role="option" id="nf-o${i}" aria-selected="${i === active}" data-v="${esc(it.v)}">${esc(it.v)}<span class="k">${it.k}</span></li>`).join("");
      if (active >= 0) inp.setAttribute("aria-activedescendant", `nf-o${active}`);
    };
    const update = async () => {
      await ensureUnits();
      const q = inp.value.trim().toLowerCase();
      const s = subjectOf(inp.value);
      kind.textContent = s ? (s.unknown ? `unknown ${s.type}` : s.type) : "";
      if (!q) { close(); return; }
      const u = S.units;
      const pool = [...u.caps.map((v) => ({ v, k: "cap" })), ...u.cameras.map((v) => ({ v, k: "camera" }))];
      const starts = pool.filter((x) => x.v.toLowerCase().startsWith(q));
      const has = pool.filter((x) => !x.v.toLowerCase().startsWith(q) && x.v.toLowerCase().includes(q));
      items = [...starts.sort((a, b) => natural(a.v, b.v)), ...has].slice(0, 8);
      active = items.length ? 0 : -1;
      if (!items.length || (items.length === 1 && items[0].v.toLowerCase() === q)) { close(); return; }
      list.hidden = false; inp.setAttribute("aria-expanded", "true"); paint();
    };
    const pick = (v) => { inp.value = v; close(); kind.textContent = subjectOf(v)?.type || ""; $("#nf-text").focus(); };
    inp.addEventListener("focus", ensureUnits);
    inp.addEventListener("input", update);
    inp.addEventListener("keydown", (e) => {
      if (list.hidden) return;
      if (e.key === "ArrowDown") { active = Math.min(items.length - 1, active + 1); paint(); e.preventDefault(); }
      else if (e.key === "ArrowUp") { active = Math.max(0, active - 1); paint(); e.preventDefault(); }
      else if (e.key === "Enter" && active >= 0) { pick(items[active].v); e.preventDefault(); }
      else if (e.key === "Escape") { close(); }
    });
    inp.addEventListener("blur", () => setTimeout(close, 150));
    list.addEventListener("mousedown", (e) => { const li = e.target.closest("li[data-v]"); if (li) { e.preventDefault(); pick(li.dataset.v); } });
  }
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const subj = subject || subjectOf(inp?.value);
    const text = $("#nf-text").value.trim();
    const status = $("#nf-status").value || null;
    msg.className = "msg";
    if (!subj) { msg.classList.add("bad"); msg.textContent = "Pick a cap or a camera first."; inp?.focus(); return; }
    if (!text && !status) { msg.classList.add("bad"); msg.textContent = "Write a note or pick a status."; $("#nf-text").focus(); return; }
    const btn = form.querySelector("button[type=submit]");
    btn.disabled = true;
    const r = await post("/api/notes", { subject_type: subj.type, subject_id: subj.id, text, status });
    btn.disabled = false;
    if (!r.ok) { msg.classList.add("bad"); msg.textContent = r.data?.error || "Couldn't save the note."; return; }
    msg.classList.add("ok");
    msg.textContent = `Saved on ${subj.id}.`;
    S.noteFlash = { text: msg.textContent, at: Date.now() };
    $("#nf-text").value = "";
    $("#nf-status").value = "";
    for (const k of [...S.cache.keys()]) if (/^(maintenance|cap|camera|notes)/.test(k)) S.cache.delete(k);
    setTimeout(refresh, 400);
  });
}

async function deleteNote(id) {
  if (!confirm("Delete this note? It disappears from every view.")) return;
  const r = await post(`/api/notes/${encodeURIComponent(id)}/delete`);
  if (!r.ok) { alert(r.data?.error || "Couldn't delete the note."); return; }
  $$(`li[data-note="${CSS.escape(String(id))}"]`).forEach((li) => li.remove());
  for (const k of [...S.cache.keys()]) if (/^(maintenance|cap|camera|notes)/.test(k)) S.cache.delete(k);
  refresh();
}

/* ================================================================== field == */

VIEWS.field = {
  tab: "field", skel: "day",
  title: () => "Field",
  cached: () => S.cache.get(cacheKey("field", {})),
  load: () => get("field", {}),
  render: renderField,
};

function fieldTable(rows, { sortBy } = {}) {
  if (!rows.length) return null;
  return `<div class="tablewrap"><table class="t"><thead><tr><th>Cap</th><th>Camera</th><th>Operator</th><th>Task</th><th>Checked out</th><th class="num">Days out</th><th>Last heard</th><th class="num">Silent</th></tr></thead><tbody>
    ${rows.map((r) => `<tr><td>${capLink(r.cap)}</td><td>${camLink(r.camera)}</td><td>${esc(r.operator || "—")}</td><td class="mut">${esc(r.task || "—")}</td>
      <td>${esc(fWhen(r.checkout_at))}</td>
      <td class="num${r.days_out > (S.status?.config?.long_trip_days || 3) ? " cbad" : ""}">${h1(r.days_out)}</td>
      <td>${r.last_heard ? esc(rel(r.last_heard)) : '<span class="mut">never since check-out</span>'}</td>
      <td class="num${r.silent_h > 24 ? " cwarn" : ""}">${r.silent_h == null ? "—" : `${r.silent_h >= 48 ? `${h1(r.silent_h / 24)} d` : `${Math.round(r.silent_h)} h`}`}</td></tr>`).join("")}
  </tbody></table></div>`;
}

function renderField(f) {
  const fl = f.fleet || {};
  const tiles = `<div class="tiles t7">
      ${tile({ k: "Fleet", v: int(fl.total), unit: "caps" })}
      ${tile({ k: "Out now", v: int(fl.out) })}
      ${tile({ k: "In the office", v: int(fl.office) })}
      ${tile({ k: "Online", v: int(fl.online), sub: "at the last sync" })}
      ${tile({ k: "Blocked", v: int(fl.blocked), sub: "held back by maintenance" })}
      ${tile({ k: "Out over 3 days", v: int((f.long_out || []).length), cls: (f.long_out || []).length ? "bad" : "" })}
      ${tile({ k: "Silent 24 h+", v: int((f.silent || []).length), cls: (f.silent || []).length ? "warn" : "" })}
    </div>`;
  const outN = (f.out || []).length;
  return `<div class="page-head"><div><h1>Field</h1><div class="sub">Who has a cap right now · operator names show only here, to chase a missing cap</div></div></div>
    ${tiles}
    <h2 class="sec">Out over 3 days <span class="sub">longest first</span></h2>${fieldTable(f.long_out || []) || empty("<b>No cap has been out more than 3 days.</b>", "sm")}
    <h2 class="sec">Silent 24 h+ <span class="sub">no heartbeat since check-out or since they were last heard</span></h2>${fieldTable(f.silent || []) || empty("<b>Every cap out has been heard from in the last day.</b>", "sm")}
    <details class="more"><summary>All ${int(outN)} caps out now</summary>${fieldTable(f.out || []) || empty("Nothing is out.", "sm")}</details>`;
}

/* =============================================================== pipeline == */

VIEWS.pipeline = {
  tab: "pipeline", skel: "day",
  title: () => "Pipeline",
  cached: () => S.cache.get(cacheKey("pipeline", {})),
  load: () => get("pipeline", {}),
  render: renderPipe,
};

/** hours as an age: "30.2 h" under two days, then "29 d" */
const ageH = (h) => (h == null ? "—" : h < 48 ? `${h1(h)} h` : `${Math.round(h / 24)} d`);
const capsWaiting = (n) => `${int(n)} ${n === 1 ? "cap's" : "caps'"} footage waiting`;
const human = (x) => String(x || "").replace(/_/g, " ");
function seen(x) { return `<span class="seen" data-tip="${esc(`last heard ${fTs(x.last_seen)}`)}">seen ${esc(rel(x.last_seen))}</span>`; }
const since = (iso) => (cDay(iso) === today() ? fHM(iso) : fWhen(iso));

/*
 * The copy station only runs 20:00-04:00, while caps come back, so a quiet station with
 * nothing waiting is normal ("idle", neutral). warn/bad mean it is silent while footage
 * waits. An uploader's detection `level` describes the drives it sees, not its health:
 * "no StarCap recordings found" is how CELL-2 idles, so only `status` picks a color.
 */
function stationCard(s) {
  const st = s.status || "idle";
  const w = s.waiting_caps || 0;
  const tone = st === "idle" ? "idle" : st;
  let pill, line;
  if (s.error) {
    pill = `<span class="pill bad">error</span>`;
    line = `<b>${esc(human(s.state) || "error")}</b> · ${esc(s.message || "")}`;
  } else if (st === "idle") {
    pill = `<span class="pill info">idle</span>`;
    line = `Idle since ${esc(since(s.last_seen))} · ${w ? capsWaiting(w) : "nothing waiting"}`;
  } else if (st === "warn" || st === "bad") {
    pill = `<span class="pill ${st}">${w ? `${int(w)} waiting` : "silent"}</span>`;
    line = `<b>Silent since ${esc(since(s.last_seen))}</b>${w ? ` with <b>${capsWaiting(w)}</b>` : ""}`;
  } else {
    pill = `<span class="pill ok">running</span>`;
    line = `<b>${esc(human(s.state) || "running")}</b>${s.message ? ` · ${esc(s.message)}` : ""}${w ? ` · ${capsWaiting(w)}` : ""}`;
  }
  const own = st !== "ok" && !s.error && s.message ? `<div class="sub2">Station says: ${esc(s.message)}</div>` : "";
  return `<div class="pcard ${esc(tone)}">
      <div class="row1"><span class="sdot2 ${esc(tone)}" aria-hidden="true"></span><span class="nm">${esc(s.name || s.id)}</span><span class="kind">copy station</span>${pill}${seen(s)}</div>
      <div class="msgline">${line}</div>${own}
      <div class="runs"><span>staging free <b>${s.staging_free_gb == null ? "—" : `${h1(s.staging_free_gb)} GB`}</b></span>
        <span><b>${int(s.pending_review || 0)}</b> pending review</span><span><b>${int(w)}</b> ${w === 1 ? "cap" : "caps"} waiting</span></div>
      ${s.error ? `<div class="callout bad">${esc(s.error)}</div>` : ""}
      <div class="foot">Copies run 20:00–04:00 while caps come back${st === "idle" || st === "ok" ? "; quiet in the day is normal" : ""}.</div></div>`;
}

function uploaderCard(u) {
  const st = u.status || "ok";
  const cur = u.current;
  const pctv = cur ? (cur.pct ?? (cur.total ? (100 * cur.sent) / cur.total : null)) : null;
  const label = u.stuck ? `stuck ${int(u.stuck_min)} min` : u.error ? "error" : st === "ok" ? "online" : st === "warn" ? "stale" : "down";
  return `<div class="pcard ${esc(st)}">
      <div class="row1"><span class="sdot2 ${esc(st)}" aria-hidden="true"></span><span class="nm">${esc(u.name || u.id)}</span><span class="kind">uploader</span>
        <span class="pill ${st === "ok" ? "ok" : esc(st)}">${esc(label)}</span>${seen(u)}</div>
      <div class="msgline"><b>${esc(human(u.state) || "idle")}</b>${u.message ? ` <span class="mut">· ${esc(u.message)}</span>` : ""}</div>
      ${cur ? `<div><div class="prog${u.stuck ? " stuck" : ""}" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(pctv || 0)}" aria-label="Current upload"><i style="width:${Math.max(0, Math.min(100, pctv || 0))}%"></i></div>
        <div class="proglbl"><span class="mono" style="font-size:11.5px">${esc(cur.run || "")}</span><span>${gb(cur.sent)} of ${gb(cur.total)} · ${pct(pctv, 0)}${cur.rate_mbps != null ? ` · ${h1(cur.rate_mbps)} Mbps` : ""}</span></div></div>`
        : `<div class="sub2">No upload running.</div>`}
      <div class="runs">${Object.entries(u.runs || {}).map(([k, v]) => `<span><b class="${k === "error" && v ? "cbad" : ""}">${int(v)}</b> ${esc(k)}</span>`).join("")}</div>
      ${u.error ? `<div class="callout bad">${esc(u.error)}</div>` : ""}</div>`;
}

function renderPipe(p) {
  const b = p.backlog || {}, l = p.lag || {}, lab = p.label || {};
  const o24 = b.over_24h || { takes: 0, hours: 0 };
  const lag = (x) => (x ? `${h1(x.p50)}<small>h p50</small>` : "—");
  const alerts = [];
  if (o24.takes > 0) {
    alerts.push(`<div class="callout bad big"><span><b>${plural(o24.takes, "take")} · ${hU(o24.hours)}</b> were copied over a day ago and still aren't in the cloud.
      Oldest ${esc(ageH(b.oldest_h))}.</span></div>`);
  }
  const loud = [...(p.stations || []), ...(p.uploaders || [])].filter((x) => x.status === "warn" || x.status === "bad" || x.error);
  for (const x of loud) {
    const isSt = (p.stations || []).includes(x);
    alerts.push(`<div class="callout ${x.status === "bad" || x.error ? "bad" : "warn"}"><span><b>${esc(x.name || x.id)}</b> ${isSt && x.waiting_caps
      ? `has been silent since ${esc(since(x.last_seen))} with ${esc(capsWaiting(x.waiting_caps))}.` : x.stuck ? `has sent nothing for ${int(x.stuck_min)} min.` : "needs a look."}</span></div>`);
  }
  if (!alerts.length) alerts.push(`<div class="callout ok"><span>Everything is moving.</span></div>`);
  const tiles = `<div class="tiles t5">
      ${tile({ k: "Copied 24 h+ ago, not in cloud", v: int(o24.takes), unit: "takes", cls: o24.takes ? "bad" : "ok", sub: o24.takes ? `${hU(o24.hours)} · oldest ${esc(ageH(b.oldest_h))}` : "nothing stuck",
        tip: "Takes copied to the station over a day ago that still have no upload time. This is the real stuck number." })}
      ${tile({ k: "Not in the cloud yet", v: int(b.takes), unit: "takes", sub: `${hU(b.hours)} · oldest ${esc(ageH(b.oldest_h))}`, tip: "Every take in the last 45 days without an upload time, including today's normal queue" })}
      ${tile({ k: "Check-out → in cloud", v: lag(l.checkout_to_cloud_h), sub: l.checkout_to_cloud_h ? `p90 ${hU(l.checkout_to_cloud_h.p90)} · ${int(l.checkout_to_cloud_h.n)} takes, 7 d` : "",
        tip: "How long after a cap goes out its footage is in the cloud. Why yesterday fills in during the day." })}
      ${tile({ k: "Copy → cloud", v: lag(l.copy_to_cloud_h), sub: l.copy_to_cloud_h ? `p90 ${hU(l.copy_to_cloud_h.p90)} · ${int(l.copy_to_cloud_h.n)} takes, 7 d` : "" })}
      ${tile({ k: "Label tick", v: lab.age_min == null ? "—" : `${int(lab.age_min)}<small>min ago</small>`, cls: lab.ok === false ? "bad" : (lab.age_min || 0) > 30 ? "warn" : "",
        sub: `${lab.ok === false ? "last run failed" : "last run ok"} · ${int(lab.waiting)} waiting for labels` })}
    </div>`;
  const stations = (p.stations || []).map(stationCard).join("");
  const ups = (p.uploaders || []).map(uploaderCard).join("");
  return `<div class="page-head"><div><h1>Pipeline</h1><div class="sub">Copy station → uploader → cloud → labels</div></div></div>
    <div class="alerts">${alerts.join("")}</div>
    ${tiles}
    <h2 class="sec">Machines</h2>
    <div class="grid2 stretch">${stations || empty("No copy station has reported.", "sm")}${ups || empty("No uploader has reported.", "sm")}</div>`;
}

/* ================================================================= trends == */

VIEWS.trends = {
  tab: "trends", skel: "grid",
  title: () => "Trends",
  cached: () => S.cache.get(cacheKey("trends", {})),
  load: () => get("trends", {}),
  render: renderTrends,
  mount: mountTrends,
};

const TRENDS = [
  { id: "caps", title: "Caps out vs recorded", type: "lines", series: [["caps_out", "out", "var(--ctx)"], ["caps_recorded", "recorded", "var(--s1)"]], val: (d) => `<b>${int(d.caps_out)}</b> out · <b>${int(d.caps_recorded)}</b> recorded`, fmt: int },
  { id: "hours", title: "Hours, total vs good", type: "lines", series: [["hours_total", "total", "var(--ctx)"], ["hours_good", "good", "var(--b-good)"]], val: (d) => `<b>${h1(d.hours_total)}</b> h · <b>${h1(d.hours_good)}</b> good`, fmt: hU },
  { id: "timing", title: "Timing pass", type: "lines", series: [["timing_pass_pct", "timing pass", "var(--s1)"]], ref: [99, "99% rule"], val: (d) => `<b>${pct(d.timing_pass_pct)}</b>`, fmt: (v) => pct(v), flag: (d) => d.timing_pass_pct != null && d.timing_pass_pct < 99, flagNote: "below the 99% rule" },
  { id: "fps30", title: "Hours at 30 fps", type: "columns", series: [["fps30_hours", "at 30 fps", "var(--b-fps30)"]], val: (d) => `<b>${h1(d.fps30_hours)}</b> h`, fmt: hU },
  { id: "expired", title: "Hours on expired calibration", type: "columns", series: [["expired_cal_hours", "expired cal", "var(--s1)"]], val: (d) => `<b>${h1(d.expired_cal_hours)}</b> h`, fmt: hU },
  { id: "fits", title: "Calibrations per day", type: "columns", series: [["cal_fits", "fits", "var(--s1)"]], ref: ["target", "target"], val: (d) => `<b>${int(d.cal_fits)}</b>`, fmt: int },
  { id: "tickets", title: "Open tickets", type: "lines", level: true, series: [["open_tickets", "open", "var(--s1)"]], area: true, val: (d) => `<b>${int(d.open_tickets)}</b> now`, fmt: int },
  { id: "overdue", title: "Overdue cameras", type: "lines", level: true, series: [["overdue_cameras", "overdue", "var(--bad)"]], area: true, val: (d) => `<b>${int(d.overdue_cameras)}</b> now`, fmt: int },
];

function renderTrends(t) {
  const days = t.days || [];
  if (!days.length) return `<div class="page-head"><div><h1>Trends</h1></div></div>${empty("No history yet.")}`;
  const last = days[days.length - 1];
  const full = [...days].reverse().find((d) => !d.partial) || last;
  const target = S.status?.config?.cal_per_day || 10;
  const cards = TRENDS.map((c) => {
    const items = c.series.length > 1 ? c.series.map(([, label, color]) => ({ label, color, kind: c.type === "lines" ? "line" : "rect" })) : [];
    if (c.ref) items.push({ label: c.ref[0] === "target" ? `target ${target}` : c.ref[1], color: "var(--ref)", kind: "dash" });
    if (c.flag) items.push({ label: c.flagNote, color: "var(--bad)", kind: "ring" });
    return `<figure class="chart-card"><figcaption><span class="ttl">${esc(c.title)}</span>
        <span class="right"><button class="tbl-btn" type="button" data-tbl hidden aria-pressed="false">Table</button></span>
        <div class="line2"><span class="val">${c.val(c.level ? last : full)}</span>${items.length ? C.legend(items) : ""}</div></figcaption>
      <div id="c-t-${c.id}"></div></figure>`;
  }).join("");
  return `<div class="page-head"><div><h1>Trends</h1><div class="sub">Last ${days.length} days, ${esc(fDay(days[0].date))} to ${esc(fDay(last.date))} · headline numbers are ${esc(fDay(full.date))}${last.partial ? ` · today is still filling in, drawn hatched or dashed` : ""}</div></div></div>
    <div class="multiples">${cards}</div>`;
}

function mountTrends(t) {
  const days = t.days || [];
  const target = S.status?.config?.cal_per_day || 10;
  for (const c of TRENDS) {
    const el = $(`#c-t-${c.id}`);
    if (!el) continue;
    const series = c.series.map(([key, label, color]) => ({ key, label, color, value: (d) => d[key], area: c.area }));
    const ref = c.ref ? [{ value: c.ref[0] === "target" ? target : c.ref[0], label: c.ref[0] === "target" ? `target ${target}` : c.ref[1] }] : [];
    const spec = {
      title: c.title, desc: `${c.title}, last ${days.length} days.`, data: days, height: 150,
      xLabel: (d) => fDayShort(d.date), xTitle: (d) => fDay(d.date), series, refs: ref,
      partial: (d) => d.partial, partialConnect: !!c.level, valueFormat: c.fmt,
      table: { columns: [{ label: "Day", value: (d) => fDay(d.date) + (d.partial ? " (so far)" : "") }, ...c.series.map(([key, label]) => ({ label, value: (d) => (d[key] == null ? "—" : c.fmt(d[key])), num: true }))] },
    };
    if (c.id === "timing") {
      const vals = days.map((d) => d.timing_pass_pct).filter((v) => v != null);
      spec.yDomain = [Math.min(98, Math.floor(Math.min(...vals) - 0.2)), 100];
      spec.yFormat = (v) => `${v}%`;
      ref[0].tick = "99%";
      spec.flag = c.flag; spec.flagNote = c.flagNote;
    }
    if (c.type === "lines") C.lines(el, spec); else C.columns(el, spec);
  }
}

/* ===================================================================== cap == */

VIEWS.cap = {
  tab: null, skel: "day",
  title: (arg) => arg || "Cap",
  cached: (arg) => S.cache.get(cacheKey("cap", { name: arg })),
  load: (arg) => get("cap", { name: arg }),
  render: renderCap,
  mount: mountCap,
};

function backLink() { return `<a class="crumb" href="#/day" data-act="back">← Back</a>`; }

function renderCap(c, arg) {
  if (DEMO && arg && c.cap !== arg) c = { ...c };
  const ms = c.my_status;
  const msS = statusOf(ms);
  const t = c.totals_30d || {};
  const on = c.out_now;
  const facts = [
    c.online ? '<span class="pill ok">online</span>' : `<span class="pill info">offline</span>`,
    on ? `<span class="pill blu">out ${h1(on.days_out)} d</span>` : '<span class="pill info">in the office</span>',
    msS ? myTag(msS) : "",
  ].join(" ");
  const sub = [c.board, c.version ? `v${c.version}` : null, c.camera_now ? `camera <a href="#/camera/${encodeURIComponent(c.camera_now)}">${esc(c.camera_now)}</a>` : null,
    c.last_heard ? `heard ${rel(c.last_heard)}` : "never heard", c.cpu_id ? `<span class="mono" data-tip="${esc(c.cpu_id)}">cpu ${esc(c.cpu_id.slice(0, 10))}</span>` : null]
    .filter(Boolean).map((x) => (x.startsWith("<") || x.includes("<a") ? x : esc(x))).join(" · ");
  const tiles = `<div class="tiles t4">
      ${tile({ k: "Days out, 30 d", v: int(t.days_out) })}
      ${tile({ k: "Hours, 30 d", v: h1(t.hours), unit: "h" })}
      ${tile({ k: "Good hours", v: h1(t.good), unit: "h", sub: t.hours ? `${pct((100 * t.good) / t.hours)} of hours` : "" })}
      ${tile({ k: "Days with no footage", v: int(t.no_footage_days), cls: t.no_footage_days ? "bad" : "" })}
    </div>`;
  const days = c.days || [];
  const dmax = Math.max(1, ...days.map((d) => d.hours?.total || 0));
  const dayRows = days.map((d) => `<tr class="click" tabindex="0" data-act="goto" data-href="#/day/${esc(d.date)}">
      <td>${esc(fDay(d.date))}</td><td>${stBadge(d.status)}</td><td class="mono">${esc(d.camera || "—")}</td>
      <td class="num"><div class="hcell">${C.miniStack(d.hours, dmax, 76)}<span class="hv">${d.hours?.total ? h1(d.hours.total) : "—"}</span></div></td>
      <td class="num">${d.takes ? int(d.takes) : '<span class="mut">—</span>'}</td><td>${pills(d.flags)}</td></tr>`).join("");
  const daysTable = days.length ? `<div class="tablewrap"><table class="t"><thead><tr><th>Day</th><th>Status</th><th>Camera</th><th class="num">Hours</th><th class="num">Takes</th><th>Flags</th></tr></thead><tbody>${dayRows}</tbody></table></div>`
    : empty("Not out in the last 30 days.", "sm");
  return `${backLink()}<div class="page-head"><div><h1>${esc(c.cap)} <span style="vertical-align:4px">${facts}</span></h1><div class="sub">${sub}</div></div></div>
    ${msS && ms?.text ? `<div class="callout info" style="margin-bottom:14px">${myTag(msS)}<span>${esc(ms.text)}${ms.since ? ` <span class="mut">· ${esc(fWhen(ms.since))}</span>` : ""}</span></div>` : ""}
    ${tiles}
    <div style="margin-top:16px">${figure("c-cap30", "Hours by bucket, last 30 days", { legend: C.legend([...C.BUCKETS.map((b) => ({ label: b.label, color: b.color, hatch: b.hatch })), { label: "went out, got nothing", color: "var(--bad)", kind: "ring" }]) })}</div>
    <h2 class="sec">Days out <span class="sub">newest first · click a day to open it</span></h2>${daysTable}
    <div class="grid2">
      <div><h2 class="sec">Tickets</h2>${ticketList(c.tickets || [])}</div>
      <div><h2 class="sec">Camera history</h2>${historyList(c.camera_history || [], "camera")}</div>
    </div>
    <div class="g57"><div><h2 class="sec">Add a note</h2><div class="panel">${noteForm({ type: "cap", id: c.cap })}</div></div>
      <div><h2 class="sec">Notes</h2><div class="panel" style="padding:4px 16px">${notesList(c.notes || [], false)}</div></div></div>`;
}

function ticketList(ts) {
  if (!ts.length) return empty("No tickets.", "sm");
  return `<div class="panel" style="padding:4px 16px"><ul class="notes">${ts.map((t) => `<li><div class="meta">${t.critical ? '<span class="pill bad">critical</span>' : '<span class="pill info">normal</span>'}
      <span class="tag">${esc(t.category || "—")}</span><span>${esc(fWhen(t.created_at))}</span>${t.resolved_at ? `<span class="cok">resolved ${esc(fWhen(t.resolved_at))}</span>` : '<span class="cwarn">open</span>'}</div>
      <div class="txt">${esc(t.description || "")}${t.resolution_note ? `<span class="mut"> → ${esc(t.resolution_note)}</span>` : ""}</div></li>`).join("")}</ul></div>`;
}

function historyList(hs, kind) {
  if (!hs.length) return empty(kind === "camera" ? "No camera swaps recorded." : "No cap history recorded.", "sm");
  return `<div class="panel" style="padding:8px 16px"><ul class="tl">${hs.map((h) => {
    const att = /attach/.test(h.action || "");
    const who = kind === "camera" ? camLink(h.camera) : capLink(h.cap || h.headset);
    return `<li><span class="tm" style="width:auto">${esc(fDayShort(cDay(h.at)))}</span><span class="mk ${att ? "blu" : ""}"></span>
      <span class="ev"><b>${att ? "Attached" : "Detached"}</b> ${who}${h.replaced ? ` <span class="m">· replaced ${esc(h.replaced)}</span>` : ""}</span></li>`;
  }).join("")}</ul></div>`;
}

function mountCap(c) {
  const byDay = new Map((c.days || []).map((d) => [d.date, d]));
  const axis = dayAxis(today());
  const data = axis.map((date) => ({ date, d: byDay.get(date) || null }));
  const el = $("#c-cap30");
  if (el) {
    C.columns(el, {
      title: `${c.cap} hours by bucket, last 30 days`, desc: `${h1(c.totals_30d?.hours)} hours over ${c.totals_30d?.days_out} days out.`,
      data, height: 180, xLabel: (x) => fDayShort(x.date), xTitle: (x) => fDay(x.date),
      series: C.BUCKETS.map((b) => ({ key: b.key, label: b.label, color: b.color, hatch: b.hatch, value: (x) => x.d?.hours?.[b.key] || 0 })),
      partial: (x) => x.date === today(),
      baseMark: (x) => (x.d && (x.d.status === "no_footage" || x.d.status === "footage_missing") ? { color: "var(--bad)" } : null),
      valueFormat: (v) => `${v.toFixed(1)} h`,
      tip: (x) => {
        if (!x.d) return { title: fDay(x.date), rows: [], note: "not out" };
        const rows = C.BUCKETS.filter((b) => (x.d.hours?.[b.key] || 0) > 0).map((b) => ({ value: hU(x.d.hours[b.key]), label: b.label, color: b.color, hatch: b.hatch, kind: "rect" }));
        return { title: `${fDay(x.date)} · ${STATUS[x.d.status]?.label || x.d.status}`, rows, note: (x.d.flags || []).map((f) => f.label).join(", ") };
      },
      table: { columns: [{ label: "Day", value: (x) => fDay(x.date) }, { label: "Status", value: (x) => (x.d ? STATUS[x.d.status]?.label || x.d.status : "not out") }, { label: "Hours", value: (x) => (x.d ? h1(x.d.hours?.total) : "—"), num: true }, { label: "Good", value: (x) => (x.d ? h1(x.d.hours?.good) : "—"), num: true }] },
    });
  }
  wireNoteForm({ type: "cap", id: c.cap });
}

/* ================================================================== camera == */

VIEWS.camera = {
  tab: null, skel: "day",
  title: (arg) => (arg ? `Camera ${arg}` : "Camera"),
  cached: (arg) => S.cache.get(cacheKey("camera", { id: arg })),
  load: (arg) => get("camera", { id: arg }),
  render: renderCamera,
  mount: mountCamera,
};

const STATE = {
  overdue: (d) => `<span class="pill bad">overdue ${-d} d</span>`,
  due_soon: (d) => `<span class="pill warn">${d === 0 ? "due today" : `due in ${d} d`}</span>`,
  ok: (d) => `<span class="pill ok">in date · ${d} d left</span>`,
  never: () => `<span class="pill bad">never calibrated</span>`,
};

function renderCamera(c) {
  const st = (STATE[c.state] || (() => `<span class="pill info">${esc(c.state || "unknown")}</span>`))(c.days_left);
  const flags = [st, c.retired ? '<span class="pill info">retired</span>' : "", c.broken ? '<span class="pill bad">broken</span>' : ""].join(" ");
  const imu = c.imu;
  const usage = c.usage || [];
  const tot = usage.reduce((a, u) => a + (u.hours || 0), 0);
  const tiles = `<div class="tiles t4">
      ${tile({ k: "Last fit", v: c.last_cal_at ? esc(fDayShort(cDay(c.last_cal_at))) : "—", sub: c.last_cal_at ? esc(rel(c.last_cal_at)) : "never" })}
      ${tile({ k: "Due", v: c.due_on ? esc(fDayShort(c.due_on)) : "—", cls: c.state === "overdue" ? "bad" : c.state === "due_soon" ? "warn" : "", sub: c.days_left == null ? "" : c.days_left <= 0 ? `${-c.days_left} d overdue` : `${c.days_left} d left` })}
      ${tile({ k: "Camera-IMU", v: imu ? esc(fDayShort(cDay(imu.calibrated_at))) : "none", cls: imu ? (imu.outdated ? "warn" : "") : "warn", sub: imu ? (imu.outdated ? "older than the stereo fit" : "current") : "OpenAI requires one" })}
      ${tile({ k: "Hours, 30 d", v: h1(tot), unit: "h", sub: `${plural(usage.length, "day")} in use` })}
    </div>`;
  const fits = c.fits || [];
  const fitRows = fits.map((f, i) => `<tr><td>${esc(fTs(f.calibrated_at))}${i === 0 ? ' <span class="tag">current</span>' : ""}</td><td class="mono">${esc(f.camera)}</td><td>${esc(f.bench || "—")}</td>
      <td class="num">${px(f.rmse_left)}</td><td class="num">${px(f.rmse_right)}</td><td class="num">${px(f.rmse_stereo)}</td><td class="num">${f.baseline_mm == null ? "—" : Number(f.baseline_mm).toFixed(2)}</td>
      <td>${f.verdict === "PASS" ? '<span class="pill ok">PASS</span>' : f.verdict ? `<span class="pill bad">${esc(f.verdict)}</span>` : '<span class="mut">no verdict</span>'}</td>
      <td class="mut">${esc(f.evidence || "—")}</td></tr>`).join("");
  const fitsTable = fits.length ? `<div class="tablewrap"><table class="t"><thead><tr><th>Calibrated</th><th>Lens</th><th>Bench</th><th class="num">L px</th><th class="num">R px</th><th class="num">Stereo</th><th class="num">Baseline mm</th><th>Verdict</th><th>Evidence</th></tr></thead><tbody>${fitRows}</tbody></table></div>`
    : empty("No fits on record.", "sm");
  return `${backLink()}<div class="page-head"><div><h1>Camera ${esc(c.camera)} <span style="vertical-align:4px">${flags}</span></h1>
      <div class="sub">base ${esc(c.base)}${c.rev ? ` · lens rev ${esc(c.rev)}` : " · original lens"}${usage.length ? ` · last on ${capLink(usage[usage.length - 1].caps?.[0])}` : ""}</div></div></div>
    ${tiles}
    <div style="margin-top:16px">${figure("c-cam30", "Hours recorded, last 30 days")}</div>
    <h2 class="sec">Fits <span class="sub">newest first</span></h2>${fitsTable}
    <div class="grid2"><div><h2 class="sec">Cap history</h2>${historyList(c.cap_history || [], "cap")}</div>
      <div><h2 class="sec">Tickets</h2>${ticketList(c.tickets || [])}</div></div>
    <div class="g57"><div><h2 class="sec">Add a note</h2><div class="panel">${noteForm({ type: "camera", id: c.camera })}</div></div>
      <div><h2 class="sec">Notes</h2><div class="panel" style="padding:4px 16px">${notesList(c.notes || [], false)}</div></div></div>`;
}

function mountCamera(c) {
  const by = new Map((c.usage || []).map((u) => [u.date, u]));
  const data = dayAxis(today()).map((date) => ({ date, u: by.get(date) || null }));
  const el = $("#c-cam30");
  if (el) {
    C.columns(el, {
      title: `Camera ${c.camera} hours per day, last 30 days`, desc: `${plural((c.usage || []).length, "day")} in use.`,
      data, height: 170, xLabel: (x) => fDayShort(x.date), xTitle: (x) => fDay(x.date),
      series: [{ key: "hours", label: "hours", color: "var(--s1)", value: (x) => x.u?.hours || 0 }],
      partial: (x) => x.date === today(), valueFormat: hU,
      tipExtra: (x) => (x.u?.caps?.length ? [{ value: x.u.caps.join(", "), label: "on", kind: "none" }] : []),
      table: { columns: [{ label: "Day", value: (x) => fDay(x.date) }, { label: "Hours", value: (x) => (x.u ? h1(x.u.hours) : "—"), num: true }, { label: "Caps", value: (x) => (x.u?.caps || []).join(", ") || "—" }] },
    });
  }
  wireNoteForm({ type: "camera", id: c.camera });
}

/* ================================================================ summary == */

const dlg = $("#summary-dlg");
async function openSummary() {
  const date = yesterday();
  $("#sum-date").textContent = fDay(date);
  $("#sum-text").textContent = "Loading…";
  setSumMsg("");
  $("#sum-write").textContent = "Add to Slack file";
  $("#sum-write").dataset.force = "";
  $("#sum-write").disabled = true;
  if (!dlg.open) dlg.showModal();
  try {
    const s = await get("summary", { date });
    $("#sum-text").textContent = s.text || "(empty)";
    $("#sum-write").disabled = false;
    if (s.written_at) {
      setSumMsg(`Already in the Slack file (${fWhen(s.written_at)}).`, "warn");
      $("#sum-write").textContent = "Write again";
      $("#sum-write").dataset.force = "1";
    }
  } catch (e) {
    $("#sum-text").textContent = "";
    setSumMsg(`Couldn't build the summary: ${e.message}`, "bad");
  }
}
function setSumMsg(t, cls = "") { const m = $("#sum-msg"); m.className = `msg ${cls}`; m.textContent = t; }
$("#summary-btn").addEventListener("click", openSummary);
dlg.addEventListener("click", (e) => { if (e.target === dlg || e.target.closest("[data-close]")) dlg.close(); });
$("#sum-copy").addEventListener("click", async () => {
  const text = $("#sum-text").textContent;
  try {
    await navigator.clipboard.writeText(text);
  } catch (_) {
    const r = document.createRange();
    r.selectNodeContents($("#sum-text"));
    const sel = getSelection(); sel.removeAllRanges(); sel.addRange(r);
    document.execCommand("copy");
    sel.removeAllRanges();
  }
  setSumMsg("Copied.", "ok");
});
$("#sum-write").addEventListener("click", async (e) => {
  const btn = e.currentTarget;
  const force = btn.dataset.force === "1";
  btn.disabled = true;
  const r = await post("/api/summary/write", { date: yesterday(), force });
  btn.disabled = false;
  if (r.ok) {
    setSumMsg(`Added to ${r.data.path || "the Slack file"} at ${fHM(r.data.written_at)}.`, "ok");
    btn.textContent = "Write again";
    btn.dataset.force = "1";
    S.cache.delete(cacheKey("summary", { date: yesterday() }));
  } else if (r.status === 409) {
    setSumMsg("Already written for this day. Write again to append another copy.", "warn");
    btn.textContent = "Write again";
    btn.dataset.force = "1";
  } else {
    setSumMsg(r.data?.error || "Couldn't write the file.", "bad");
  }
});

/* ================================================================ actions == */

const ACTIONS = {
  retry: () => route({ keepScroll: true }),
  back: (el, e) => { if (history.length > 1) { e.preventDefault(); history.back(); } else { e.preventDefault(); location.hash = "#/day"; } },
  "cap-filter": (el) => { S.day.filter = el.dataset.key; renderCapsTable(currentDay()); },
  "caps-reset": () => { S.day.sort = null; S.day.filter = "all"; S.day.q = ""; const q = $("#cap-q"); if (q) q.value = ""; renderCapsTable(currentDay()); },
  sort: (el) => {
    if (el.dataset.table !== "caps") return;
    const col = CAP_COLS.find((c) => c.key === el.dataset.key);
    const first = col.desc ? -1 : 1;
    const s = S.day.sort;
    if (!s || s.key !== col.key) S.day.sort = { key: col.key, dir: first };
    else if (s.dir === first) S.day.sort = { key: col.key, dir: -first };
    else S.day.sort = null;
    renderCapsTable(currentDay());
    $(`#caps-t button[data-key="${col.key}"]`)?.focus();
  },
  "open-cap": (el) => openCapDrawer(el.dataset.cap),
  "close-drawer": () => closeDrawer(),
  "queue-all": () => { S.queueAll = !S.queueAll; refresh(); },
  "tickets-all": () => { S.ticketsAll = !S.ticketsAll; refresh(); },
  "note-del": (el) => deleteNote(el.dataset.id),
  "note-prefill": (el) => {
    const inp = $("#nf-subj");
    if (!inp) return;
    inp.value = el.dataset.subj;
    $("#nf-kind").textContent = "cap";
    inp.closest(".panel")?.scrollIntoView({ behavior: "smooth", block: "center" });
    setTimeout(() => $("#nf-text")?.focus(), 350);
  },
  goto: (el) => { location.hash = el.dataset.href; },
};

function currentDay() {
  const date = S.route?.arg || yesterday();
  return S.cache.get(cacheKey("day", { date })) || { caps: [] };
}

document.addEventListener("click", (e) => {
  const el = e.target.closest("[data-act]");
  if (!el) return;
  // a link or button inside a clickable row keeps its own job
  if (el.tagName === "TR" && e.target.closest("a, button")) return;
  const fn = ACTIONS[el.dataset.act];
  if (fn) fn(el, e);
});
document.addEventListener("keydown", (e) => {
  if ((e.key === "Enter" || e.key === " ") && e.target.matches?.("tr[data-act]")) {
    e.preventDefault();
    ACTIONS[e.target.dataset.act]?.(e.target, e);
  }
  if (e.key === "/" && S.route?.name === "day" && !e.target.closest("input, textarea, select") && !drawer.open) {
    const q = $("#cap-q");
    if (q) { e.preventDefault(); q.focus(); }
  }
});

/* ================================================================== start == */

$("#sync-chip").addEventListener("click", startSync);
if (DEMO) $("#demo-tag").hidden = false;
C.bindTips(document);
route();
