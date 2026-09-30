#!/usr/bin/env python3
"""Camera firmware — browser front end for fwident.py.

Same shape as the jitter bench, camrig and starcapfinder: stdlib only, a local
HTTP server, one page. No pip install, nothing left running between sessions.

Two speeds, and the page is built around the difference:

  SCAN     reads USB descriptors and v4l2 control ranges only. Milliseconds, and
           it tells you the build for free every time a camera is plugged in.
  MEASURE  captures ~11 s of frames per camera and reads the frame length out of
           the sensor's own timestamps. Slower, and it is the AUTHORITY — a scan
           describes the build a camera may have shipped with, a measurement
           describes the one it is running.

Only a measured grading is pushed to the dashboard by default, because the fleet
acts on what lands there.

    ./ui.py            -> http://127.0.0.1:8093
"""
import json
import os
import re
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
FWIDENT = os.path.join(HERE, "..", "fwident.py")
PORT = int(os.environ.get("PORT", "8093"))
RESULTS = os.path.join(HERE, ".last-run.json")

# One job at a time: two would fight over /dev/video*, and the loser's timing
# would read as a camera fault rather than as contention.
LOCK = threading.Lock()
STATE = {"running": None, "log": [], "cameras": [], "fleet": None}


def _pump(proc, label):
    for line in iter(proc.stdout.readline, ""):
        STATE["log"].append(line.rstrip("\n"))
        del STATE["log"][:-400]
    proc.wait()
    STATE["log"].append(f"[{label} finished, exit {proc.returncode}]")
    # The run wrote its own structured results on the way out, so the table can show
    # what it concluded without a second capture — re-running --measure to refresh the
    # page would cost another 11 s per camera and fire any --push a second time.
    try:
        with open(RESULTS) as f:
            STATE["cameras"] = json.load(f)
    except (OSError, json.JSONDecodeError):
        pass
    STATE["running"] = None


def _run_json(args, timeout=600):
    """fwident --json, parsed. Returns (rows, error)."""
    try:
        p = subprocess.run([sys.executable, FWIDENT, "--json", *args],
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, "timed out"
    if p.returncode != 0:
        return None, (p.stderr or p.stdout or "failed").strip()
    try:
        return json.loads(p.stdout), None
    except json.JSONDecodeError:
        return None, (p.stdout or "no output").strip()


_FWIDENT_MOD = None


def _fwident():
    """fwident.py as a module, for its dashboard client. Loaded once — it was being
    re-executed on every poll, which re-read the credential files each time."""
    global _FWIDENT_MOD
    if _FWIDENT_MOD is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("fwident", FWIDENT)
        m = importlib.util.module_from_spec(spec)
        saved, sys.argv = sys.argv, ["fwident"]
        try:
            spec.loader.exec_module(m)
        finally:
            sys.argv = saved
        _FWIDENT_MOD = m
    return _FWIDENT_MOD


def start(args, label):
    """Run fwident in the background, streaming its human output to the log."""
    if not LOCK.acquire(blocking=False):
        return False
    try:
        if STATE["running"]:
            return False
        STATE["log"] = [f"$ fwident.py {' '.join(args)}", ""]
        STATE["running"] = label
        proc = subprocess.Popen([sys.executable, FWIDENT, *args, "--json-out", RESULTS],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1)
        threading.Thread(target=_pump, args=(proc, label), daemon=True).start()
        return True
    finally:
        LOCK.release()


PAGE = r"""<!doctype html><html><head><meta charset=utf-8>
<title>Camera Firmware</title><style>
*{box-sizing:border-box} body{margin:0;background:#14161a;color:#e8eaed;
 font:14px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
.wrap{max-width:1000px;margin:0 auto;padding:22px}
h1{font-size:19px;margin:0 0 2px} .sub{color:#9aa0a6;font-size:12px;margin-bottom:18px}
.card{background:#1c1f25;border:1px solid #272b33;border-radius:10px;padding:16px;margin-bottom:14px}
.card h2{font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:#9aa0a6;margin:0 0 12px}
button{border:0;border-radius:8px;padding:11px 18px;font-size:14px;font-weight:600;
 cursor:pointer;color:#fff;background:#343a45}
button:hover{filter:brightness(1.18)} button:disabled{opacity:.4;cursor:not-allowed;filter:none}
.primary{background:#2563eb} .go{background:#16a34a} .ghost{background:#2a2f38}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
pre{background:#0e1014;border:1px solid #23272e;border-radius:8px;padding:12px;
 font:12px/1.55 ui-monospace,monospace;max-height:300px;overflow:auto;white-space:pre-wrap;margin:0}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.05em;
 color:#9aa0a6;font-weight:600;padding:7px 9px;border-bottom:1px solid #272b33}
td{padding:8px 9px;border-bottom:1px solid #1f232a;font-family:ui-monospace,monospace}
.good{color:#4ade80;font-weight:700} .bad{color:#f87171;font-weight:700}
.warn{color:#fbbf24} .mut{color:#9aa0a6}
.pill{display:inline-block;padding:2px 9px;border-radius:99px;font-size:11px;
 background:#262b33;color:#cbd2dc}
.big{font-size:26px;font-weight:700;font-family:ui-monospace,monospace}
.tiles{display:flex;gap:12px;flex-wrap:wrap}
.tile{flex:1;min-width:150px;background:#171a1f;border:1px solid #23272e;
 border-radius:9px;padding:12px 14px}
.tile .k{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:#9aa0a6}
.note{color:#9aa0a6;font-size:12px;margin-top:10px}
.note code{background:#12141a;padding:1px 5px;border-radius:4px;font-size:11px}
.banner{display:none;background:#7f1d1d;border:1px solid #b91c1c;border-radius:8px;
 padding:11px 14px;margin-bottom:14px;font-weight:600}
.pick{display:flex;gap:8px;flex-wrap:wrap}
.pick label{display:flex;align-items:center;gap:7px;background:#171a1f;border:1px solid #23272e;
 border-radius:8px;padding:9px 13px;cursor:pointer;font-size:13px;color:#e8eaed;margin:0}
.pick label:hover{border-color:#3a4150}
.pick input{accent-color:#4f8cff;margin:0}
.pick label.sel{border-color:#4f8cff;background:#1b2333}
</style></head><body><div class=wrap>
<h1>Camera Firmware</h1>
<div class=sub>Which build a docked camera is running &mdash; and whether it is the one
 that fails the customer&rsquo;s frame-timing spec.</div>

<div id=banner class=banner></div>

<div class=card>
  <h2>Docked cameras</h2>
  <div id=picker class=mut>looking for cameras&hellip;</div>
  <div class=row style=margin-top:14px>
    <button class=primary onclick="scan()">Re-read</button>
    <button class=go onclick="go(['--measure'],'measure')">Measure &amp; confirm</button>
    <button class=go onclick="go(['--measure','--push'],'measure+push')">Measure &amp; send to dashboard</button>
    <span id=runstate class=pill>idle</span>
  </div>
  <div class=note>
   <b>Re-read</b> takes milliseconds and looks at what the camera <i>declares</i>: its
   <code>bcdDevice</code>, the USB bandwidth ladder it advertises, and the gain/gamma/brightness
   ranges it exposes. The two builds differ in all five, so this identifies the build without
   capturing a single frame &mdash; but it describes the build a camera may have <i>shipped</i>
   with, not necessarily the one it is running.
   <br><br>
   <b>Measure</b> captures ~11&nbsp;s of video per camera and reads the frame length out of the
   timestamps the sensor embeds in every frame. That is the authority, and only measured
   gradings are sent to the dashboard.</div>
  <div id=cams style=margin-top:14px></div>
</div>

<div class=card>
  <h2>Fleet</h2>
  <div class=row>
    <button class=ghost onclick="fleet()">Refresh from dashboard</button>
    <a href="https://starcap.dev/cameras" target=_blank
       style="color:#4f8cff;font-size:13px;text-decoration:none">open /cameras &rarr;</a>
  </div>
  <div id=fleetbox style=margin-top:14px class=mut>not loaded</div>
</div>

<div class=card>
  <h2>Output</h2>
  <pre id=log class=mut>nothing run yet</pre>
</div>
</div>
<script>
const $=i=>document.getElementById(i);
const esc=s=>String(s==null?'':s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));

/* Calibration is a NOTE here, not a verdict. This tool answers one question — is the
   firmware the one that jitters — and that answer owns the only colour in the row. The
   calibration state rides along so nobody has to open /cameras to find out what else the
   unit still needs, but a red "no white balance" next to a green firmware verdict reads
   as the firmware check having failed, which it has not. So: muted, always. */
let CALIB={};
const CAL_TEXT={
  ready:        ['calibrated','both files, current method, evidence PASS'],
  unverified:   ['missing verification','both files, but the evidence bundle is incomplete or not a clean pass'],
  legacy_method:['missing recalibration (legacy bench)','stereo was measured on the retired bench — not field ready'],
  wb_missing:   ['missing white balance','geometry done, colour not'],
  stereo_missing:['missing intrinsics/extrinsics','white balance only — undeliverable'],
  none:         ['missing both calibrations','neither file on record'],
};
function calib(cam){
  if(cam==null) return '<span class=mut>—</span>';
  const base=String(cam).split('.')[0];
  const c=CALIB[base];
  if(!c) return '<span class=mut>…</span>';
  /* An older dashboard build returns only the two booleans and no `state`. Fall back to
     what those can honestly support — files present or not — and say it is unconfirmed,
     rather than printing a green "ready" the endpoint never claimed. */
  if(!c.state){
    const both=c.calibration&&c.whitebalance;
    return '<span class="'+(both?'mut':'warn')+'" title="this dashboard build does not '
      +'report the calibration STATE — only whether the files exist">'
      +(both?'calibrated':c.calibration?'missing white balance':c.whitebalance
        ?'missing intrinsics/extrinsics':'missing both calibrations')
      +' (unconfirmed)</span>';
  }
  const t=CAL_TEXT[c.state]||['unknown state',''];
  const fit=c.fitted_lens?' (lens '+esc(c.fitted_lens)+')':'';
  /* Amber only when something is actually outstanding. A finished camera stays grey —
     if every row is coloured, the colour stops meaning anything. Never red: red in this
     table is reserved for the firmware verdict, which is the question this tool answers. */
  const cls = c.state === 'ready' ? 'mut' : 'warn';
  return '<span class="'+cls+'" title="'+esc(t[1])+'">'+t[0]+fit+'</span>';
}
async function loadCalib(){
  const r=await call('/api/calib');
  if(r&&!r.error){ CALIB=r.cameras||{}; if(STATE_ROWS.length) drawCams(STATE_ROWS); }
}

function verdict(b){
  if(b==='static-16640') return '<span class=good>LATEST &mdash; no jitter</span>';
  if(b==='dither-16626') return '<span class=bad>OLD &mdash; causes the jitter</span>';
  if(!b) return '<span class=warn>unrecognised</span>';
  return '<span class=warn>'+esc(b)+' &mdash; unknown build</span>';
}

function drawCams(rows){
  if(!rows||!rows.length){ $('cams').innerHTML='<div class=mut>no cameras docked</div>'; return; }
  let h='<table><tr><th>camera</th><th>build</th><th>verdict</th>'
       +'<th>calibration</th><th>timing</th><th>dashboard</th></tr>';
  for(const r of rows){
    const b=r.measured_build||r.descriptor_build;
    const how=r.measured_build?'measured':'descriptors only';
    h+='<tr><td>'+esc(r.camera||'—')+'<div class=mut style=font-size:11px>'+esc(r.product)+'</div></td>'
      +'<td>'+esc(b||'—')+'<div class=mut style=font-size:11px>'+how+'</div></td>'
      +'<td>'+verdict(b)+'</td>'
      +'<td class=mut>'+esc(r.markers||'')+'</td>'
      +'<td class=mut>'+esc(r.measured_detail||'—')+'</td>'
      +'<td>'+(r.push?esc(r.push):'<span class=mut>—</span>')+'</td></tr>';
    if(r.agrees===false) h+='<tr><td colspan=6 class=warn>&#9888; descriptors and timing '
      +'DISAGREE on this camera &mdash; trust the timing. A reflash does not rewrite the descriptors.</td></tr>';
  }
  $('cams').innerHTML=h+'</table>';
}

/* Every call goes through here. A dead backend used to leave the page sitting on
   "scanning…" forever with no way to tell that nothing was listening — the app had been
   closed, and the open tab looked like a hung scan. */
let DOWN=false;
async function call(url,opts){
  try{
    const r=await fetch(url,opts);
    if(!r.ok) throw new Error('HTTP '+r.status);
    const j=await r.json();
    if(DOWN){ DOWN=false; $('banner').style.display='none'; }
    return j;
  }catch(e){
    DOWN=true;
    $('banner').style.display='block';
    $('banner').innerHTML='Not running &mdash; double-click <b>Camera Firmware</b> on the '
      +'Desktop to start it again, then reload this page.';
    return null;
  }
}

/* Which camera the buttons act on. null = every docked camera. */
let PICK=null;

function drawPicker(rows){
  if(!rows||!rows.length){
    $('picker').innerHTML='<span class=mut>no cameras docked &mdash; plug one in and press '
      +'<b>Re-read</b></span>';
    return;
  }
  if(PICK && !rows.some(r=>r.bus_id===PICK)) PICK=null;
  let h='<div class=pick>';
  h+='<label class="'+(PICK===null?'sel':'')+'"><input type=radio name=pick '
    +(PICK===null?'checked':'')+' onchange="setPick(null)">All ('+rows.length+')</label>';
  for(const r of rows){
    const b=r.measured_build||r.descriptor_build;
    const dot=b==='static-16640'?'<span class=good>●</span>'
             :b==='dither-16626'?'<span class=bad>●</span>':'<span class=warn>●</span>';
    h+='<label class="'+(PICK===r.bus_id?'sel':'')+'"><input type=radio name=pick '
      +(PICK===r.bus_id?'checked':'')+' onchange="setPick(\''+esc(r.bus_id)+'\')">'
      +dot+' camera '+esc(r.camera||'?')
      +' <span class=mut style=font-size:11px>'+esc(r.device||'')+'</span></label>';
  }
  $('picker').innerHTML=h+'</div>';
}
function setPick(v){ PICK=v; drawPicker(STATE_ROWS); }

let STATE_ROWS=[];
async function scan(){
  $('picker').innerHTML='<span class=mut>reading USB descriptors and control ranges…</span>';
  const r=await call('/api/scan');
  if(!r) return;
  if(r.error){ $('picker').innerHTML='<div class=bad>'+esc(r.error)+'</div>'; return; }
  STATE_ROWS=r.cameras||[];
  drawPicker(STATE_ROWS); drawCams(STATE_ROWS);
}

async function go(args,label){
  /* The picker is a --only flag, so a measure on one camera costs 11 s instead of
     11 s x however many happen to be plugged in. */
  const full=PICK?['--only',PICK,...args]:args;
  const r=await call('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({args:full,label:label+(PICK?' ('+PICK+')':' (all)')})});
  if(r&&!r.ok) alert(r.error||'busy');
  poll();
}

async function fleet(){
  $('fleetbox').innerHTML='<div class=mut>loading…</div>';
  const r=await call('/api/fleet');
  if(!r) { $('fleetbox').innerHTML='<span class=mut>not loaded</span>'; return; }
  if(r.error){ $('fleetbox').innerHTML='<div class=bad>'+esc(r.error)+'</div>'; return; }
  const n=r.numbers||{}, c=r.counts||{};
  const list=a=>(a&&a.length)?a.join(', '):'—';
  let h='<div class=tiles>'
   +'<div class=tile><div class=k>on latest</div><div class="big good">'+(c.latest||0)+'</div>'
   +'<div class=mut style=font-size:11px>'+esc(list(n.latest))+'</div></div>'
   +'<div class=tile><div class=k>on old &mdash; reflash these</div><div class="big bad">'+(c.old||0)+'</div>'
   +'<div class=mut style=font-size:11px>'+esc(list(n.old))+'</div></div>'
   +'<div class=tile><div class=k>graded in all</div><div class=big>'+(c.graded||0)+'</div>'
   +'<div class=mut style=font-size:11px>latest build is '+esc(r.latest)+'</div></div>';
  if(c.unknown) h+='<div class=tile><div class=k>unrecognised build</div>'
   +'<div class="big warn">'+c.unknown+'</div><div class=mut style=font-size:11px>'
   +esc(list(n.unknown))+'</div></div>';
  $('fleetbox').innerHTML=h+'</div>';
}

let WAS_RUNNING=null;
async function poll(){
  const s=await call('/api/state');
  if(!s) return;
  /* The fleet numbers change the moment a push lands, and having to press Refresh to
     see your own write is the kind of thing you stop trusting. Re-read on the
     running->idle edge, once, rather than polling the dashboard on a timer. */
  if(WAS_RUNNING && !s.running){ fleet(); loadCalib(); }
  WAS_RUNNING = s.running;
  $('runstate').textContent=s.running?('running: '+s.running):'idle';
  $('runstate').style.background=s.running?'#7c2d12':'#262b33';
  for(const b of document.querySelectorAll('button')) b.disabled=!!s.running;
  if(s.log&&s.log.length){ $('log').textContent=s.log.join('\n'); $('log').className=''; }
  if(s.cameras&&s.cameras.length){ STATE_ROWS=s.cameras; drawCams(s.cameras); drawPicker(s.cameras); }
}
setInterval(poll,1200); poll(); scan(); fleet(); loadCalib();
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, ctype="application/json"):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            return self._send(PAGE, "text/html; charset=utf-8")
        if self.path == "/api/state":
            return self._send(json.dumps(STATE))
        if self.path == "/api/scan":
            rows, err = _run_json([], timeout=60)
            if err:
                return self._send(json.dumps({"error": err}))
            STATE["cameras"] = rows
            return self._send(json.dumps({"cameras": rows}))
        if self.path == "/api/calib":
            # Same verdict /cameras paints, so the operator does not have to open the page
            # to find out whether the camera in their hand is finished.
            try:
                m = _fwident()
                r = m.dash("GET", "/api/ingest/camident/status")
            except Exception as e:                                  # noqa: BLE001
                r = {"ok": False, "error": str(e)}
            if not r.get("ok"):
                return self._send(json.dumps({"error": r.get("error", "failed")}))
            by = {str(c["camera"]): c for c in r.get("cameras") or []}
            return self._send(json.dumps({"cameras": by}))
        if self.path == "/api/fleet":
            try:
                r = _fwident().dash("GET", "/api/ingest/camera-firmware")
            except SystemExit as e:
                r = {"ok": False, "error": str(e)}
            except Exception as e:                                  # noqa: BLE001
                r = {"ok": False, "error": str(e)}
            if not r.get("ok"):
                return self._send(json.dumps({"error": r.get("error", "failed")}))
            STATE["fleet"] = r
            return self._send(json.dumps(r))
        self.send_error(404)

    def do_POST(self):
        if self.path != "/api/run":
            return self.send_error(404)
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or "{}")
        # Allowlist, not passthrough: this is a local page, but it hands a list straight
        # to a subprocess, so only the flags the UI actually offers get through — plus one
        # --only value, validated as a camera/node/bus handle rather than free text.
        raw = [str(x) for x in body.get("args", [])]
        args, i = [], 0
        while i < len(raw):
            a = raw[i]
            if a in ("--measure", "--push"):
                args.append(a)
            elif a == "--only" and i + 1 < len(raw):
                v = raw[i + 1]
                if not re.fullmatch(r"[0-9]+(\.[0-9]+)?|/dev/video[0-9]+|[0-9]+-[0-9.]+", v):
                    # Refuse rather than drop it. Dropping turns "measure THIS camera" into
                    # "measure every camera", which is the opposite of what was asked and
                    # costs 11 s a unit.
                    return self._send(json.dumps(
                        {"ok": False, "error": f"not a camera handle: {v!r}"}))
                args += ["--only", v]
                i += 1
            i += 1
        ok = start(args, body.get("label", "run"))
        self._send(json.dumps({"ok": ok, "error": None if ok else "a job is already running"}))


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    url = f"http://127.0.0.1:{PORT}"
    print(f"camera firmware -> {url}")
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
