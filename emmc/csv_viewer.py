#!/usr/bin/env python3
"""
csv_viewer.py — live-refreshing, editable local web view of a CSV file, so you
never have to close/reopen it in a spreadsheet app to see edits land or make
one. Built for pi_emmc_speeds.csv, but works for any CSV passed as an argument.

Run:   python3 csv_viewer.py [path/to/file.csv]     ->  http://127.0.0.1:8772
       (defaults to pi_emmc_speeds.csv next to this script)
"""
import csv
import http.server
import json
import re
import sys
import threading
from pathlib import Path

HOST = "127.0.0.1"
PORT = 8772
SCRIPT_DIR = Path(__file__).resolve().parent
CSV_PATH = Path(sys.argv[1]) if len(sys.argv) > 1 else SCRIPT_DIR / "pi_emmc_speeds.csv"
DESKTOP_PATH = Path.home() / "Desktop" / CSV_PATH.name
_WRITE_LOCK = threading.Lock()   # serialize read-modify-write so two quick edits can't race

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  :root{{
    --bg:#0f1419; --panel:#171d26; --line:#2a3340; --fg:#e6edf3; --mut:#8b98a8; --acc:#ff7a18;
  }}
  *{{box-sizing:border-box}}
  body{{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,Segoe UI,Roboto,sans-serif}}
  header{{display:flex;align-items:center;gap:12px;padding:14px 20px;background:var(--panel);border-bottom:1px solid var(--line)}}
  header h1{{font-size:16px;margin:0;font-weight:600}}
  header .dot{{color:var(--acc)}}
  header .path{{margin-left:auto;font-size:12px;color:var(--mut);font-family:ui-monospace,monospace}}
  header .count{{font-size:12px;padding:4px 10px;border-radius:999px;border:1px solid var(--line);color:var(--mut)}}
  button{{margin-left:12px;background:var(--acc);border:1px solid var(--acc);color:#1a1205;font-weight:600;border-radius:8px;padding:7px 14px;font-size:13px;cursor:pointer}}
  button:hover:not(:disabled){{filter:brightness(1.08)}}
  button:disabled{{opacity:.6;cursor:default}}
  main{{max-width:100%;margin:0;padding:20px}}
  table{{width:100%;table-layout:fixed;border-collapse:collapse;font-size:13px}}
  th,td{{overflow-wrap:break-word}}
  th,td{{text-align:left;padding:8px 12px;border-bottom:1px solid var(--line)}}
  th{{color:var(--mut);font-weight:500;position:sticky;top:0;background:var(--bg)}}
  tr:hover td{{background:var(--panel)}}
  td[contenteditable]{{cursor:text;border-radius:4px}}
  td[contenteditable]:focus{{outline:2px solid var(--acc);background:var(--panel)}}
  td.saved{{animation:flash 1s ease-out}}
  @keyframes flash{{from{{background:#1f3d24}} to{{background:transparent}}}}
  .yes{{color:#3fb950}} .no{{color:#f85149}} .unknown{{color:var(--mut)}}
  .stamp{{color:var(--mut);font-size:12px;padding:0 20px 20px}}
  .stamp .err{{color:#f85149}}
</style>
</head>
<body>
<header><h1><span class="dot">●</span> {title}</h1><span class="count" id="count">— devices</span><span class="path">{path}</span>
  <button id="exportBtn" onclick="exportNow()">💾 Save to Desktop</button></header>
<main><table id="tbl"></table></main>
<div class="stamp" id="stamp">click any cell to edit — saves on Enter or clicking away</div>
<script>
let header = [], rows = [];
let editingCell = null;   // "r_c" of the cell currently focused, so polling doesn't clobber it

function cellClass(v){{
  const s = (v||'').trim().toLowerCase();
  if(s === 'yes') return 'yes';
  if(s === 'no') return 'no';
  if(s === 'unknown') return 'unknown';
  return '';
}}
function esc(s){{ return (s||'').replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c])); }}

function renderTable(){{
  const t = document.getElementById('tbl');
  t.innerHTML = '<thead><tr>' + header.map(h => `<th>${{esc(h)}}</th>`).join('') + '</tr></thead>' +
    '<tbody>' + rows.map((row, r) => '<tr>' + row.map((c, ci) =>
      `<td id="c-${{r}}_${{ci}}" class="${{cellClass(c)}}" contenteditable spellcheck="false"
           onfocus="editingCell='${{r}}_${{ci}}'"
           onblur="editingCell=null; saveCell(${{r}}, ${{ci}}, this)"
           onkeydown="if(event.key==='Enter'){{event.preventDefault(); this.blur();}}">${{esc(c)}}</td>`
    ).join('') + '</tr>').join('') + '</tbody>';
}}

async function saveCell(r, ci, el){{
  const value = el.textContent;
  if(rows[r][ci] === value) return;   // no actual change — nothing to save
  rows[r][ci] = value;
  try{{
    const resp = await fetch('/api/save', {{method:'POST', headers:{{'Content-Type':'application/json'}},
                                            body: JSON.stringify({{row: r, col: ci, value}})}});
    const j = await resp.json();
    if(j.ok){{
      el.classList.add('saved');
      setTimeout(() => el.classList.remove('saved'), 1000);
      document.getElementById('stamp').textContent = 'saved ' + new Date().toLocaleTimeString();
    }} else {{
      document.getElementById('stamp').innerHTML = '<span class="err">save failed: ' + esc(j.error||'unknown') + '</span>';
    }}
  }} catch(e){{
    document.getElementById('stamp').innerHTML = '<span class="err">save failed: ' + esc(String(e)) + '</span>';
  }}
}}

async function exportNow(){{
  const btn = document.getElementById('exportBtn');
  btn.disabled = true;
  const orig = btn.textContent;
  try{{
    const r = await fetch('/api/export', {{method: 'POST'}});
    const j = await r.json();
    document.getElementById('stamp').textContent = j.ok
      ? 'saved to Desktop ' + new Date().toLocaleTimeString()
      : 'export failed: ' + (j.error || 'unknown');
  }} catch(e){{
    document.getElementById('stamp').textContent = 'export failed: ' + e;
  }}
  btn.textContent = '✓ saved';
  setTimeout(() => {{ btn.textContent = orig; btn.disabled = false; }}, 1200);
}}

function countYes(colName){{
  const col = header.indexOf(colName);
  return col === -1 ? 0 : rows.filter(r => (r[col]||'').trim().toLowerCase() === 'yes').length;
}}
function updateCount(){{
  const ready = countYes('Field Ready');
  const emmc = countYes('Booted from eMMC');
  const sd = rows.length - emmc;
  document.getElementById('count').textContent =
    `${{rows.length}} tracked · ${{ready}} field ready · ${{emmc}} from eMMC · ${{sd}} from SD`;
}}

async function poll(){{
  try{{
    const r = await fetch('/api/data');
    const j = await r.json();
    header = j.header;
    if(editingCell === null){{           // don't clobber an in-progress edit mid-keystroke
      rows = j.rows;
      renderTable();
    }}
    updateCount();
  }} catch(e){{ /* transient — next poll retries */ }}
  setTimeout(poll, 1500);
}}
poll();
</script>
</body>
</html>
"""


def _natural_key(name):
    """Sort by name, but numerically on any trailing digits — so vin2 < vin3 <
    vin5 < vin10 < vin12 < vin16, not lexicographic (which would put vin10
    before vin2). Falls back to a plain case-insensitive sort for the rest."""
    m = re.match(r"^(.*?)(\d+)$", name)
    if m:
        return (m.group(1).lower(), int(m.group(2)))
    return (name.lower(), -1)


def _sorted_data_rows():
    """Data rows only (no header), sorted — the single source of truth for
    row ordering used by BOTH read and save, so a row index always means the
    same row on both sides of a save."""
    with open(CSV_PATH, newline="") as f:
        rows = list(csv.reader(f))
    if not rows:
        return [], []
    header, data = rows[0], rows[1:]
    data.sort(key=lambda r: _natural_key(r[0]) if r else ("", -1))
    return header, data


def read_csv():
    header, data = _sorted_data_rows()
    export_to_desktop()   # keep the Desktop copy fresh even for edits made directly
                          # to the file (not through this viewer) — runs every poll
    return {"header": header, "rows": data}


def export_to_desktop():
    """Copy the current (sorted) CSV to the Desktop, so there's always a
    ready-to-share local copy — auto-run on every poll/save, plus a manual
    button for an explicit on-demand copy."""
    header, data = _sorted_data_rows()
    DESKTOP_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(DESKTOP_PATH, "w", newline="") as f:
        csv.writer(f).writerows([header] + data)


def save_cell(row, col, value):
    with _WRITE_LOCK:
        header, data = _sorted_data_rows()
        if not (0 <= row < len(data)) or not (0 <= col < len(data[row])):
            raise IndexError(f"cell ({row}, {col}) out of range")
        data[row][col] = value
        with open(CSV_PATH, "w", newline="") as f:
            csv.writer(f).writerows([header] + data)
    export_to_desktop()


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/data":
            try:
                return self._json(read_csv())
            except OSError as e:
                return self._json({"error": str(e)}, 500)
        body = PAGE.format(title=CSV_PATH.name, path=str(CSV_PATH)).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path == "/api/save":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                save_cell(int(body["row"]), int(body["col"]), str(body["value"]))
                return self._json({"ok": True})
            except (ValueError, KeyError, IndexError, OSError) as e:
                return self._json({"ok": False, "error": str(e)}, 400)
        if self.path == "/api/export":
            try:
                export_to_desktop()
                return self._json({"ok": True, "path": str(DESKTOP_PATH)})
            except OSError as e:
                return self._json({"ok": False, "error": str(e)}, 500)
        return self._json({"error": "not found"}, 404)


def main():
    if not CSV_PATH.is_file():
        print(f"missing file: {CSV_PATH}")
        sys.exit(1)
    srv = http.server.ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"csv_viewer running at http://{HOST}:{PORT}  (watching + editing {CSV_PATH})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
