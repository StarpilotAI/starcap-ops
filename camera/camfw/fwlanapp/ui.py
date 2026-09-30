#!/usr/bin/env python3
"""Camera Firmware LAN — browser front end for dockscan.py.

The LAN half of the pair. The other app (Camera Firmware, port 8093) grades ONE
camera plugged into this laptop's USB; this one grades every camera attached to
every recorder docked on the cable, by SSHing in and reading the pi's sysfs.

It never opens a camera, on the laptop or on a pi, so a recorder mid-take is safe
to scan. That is the whole reason it can do 48 boards instead of one.

    ./ui.py            -> http://127.0.0.1:8094
"""
import json
import os
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
DOCKSCAN = os.path.join(HERE, "..", "dockscan.py")
PORT = int(os.environ.get("PORT", "8094"))

LOCK = threading.Lock()
STATE = {"running": None, "log": [], "rows": [], "iface": None, "when": None}


def _pump(proc, label):
    for line in iter(proc.stdout.readline, ""):
        STATE["log"].append(line.rstrip("\n"))
        del STATE["log"][:-500]
    proc.wait()
    STATE["log"].append(f"[{label} finished, exit {proc.returncode}]")
    STATE["running"] = None


def start(args, label):
    if not LOCK.acquire(blocking=False):
        return False
    try:
        if STATE["running"]:
            return False
        STATE["log"] = [f"$ dockscan.py {' '.join(args)}", ""]
        STATE["running"] = label
        proc = subprocess.Popen([sys.executable, DOCKSCAN, *args],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1)
        threading.Thread(target=_pump, args=(proc, label), daemon=True).start()
        return True
    finally:
        LOCK.release()


def sweep():
    """Structured scan. Separate from the streamed run so the table has real rows
    rather than scraped text."""
    p = subprocess.run([sys.executable, DOCKSCAN, "--json"],
                       capture_output=True, text=True, timeout=900)
    if p.returncode != 0:
        return None, (p.stdout or p.stderr or "failed").strip().splitlines()[-1][:200]
    try:
        return json.loads(p.stdout), None
    except json.JSONDecodeError:
        return None, (p.stdout or "no output").strip()[:200]


def link_state():
    """What the cable looks like, so 'nothing answered' can say WHY."""
    try:
        out = subprocess.run(["ip", "-o", "-4", "addr", "show"],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception:                                          # noqa: BLE001
        return {"up": False, "detail": "could not read interfaces"}
    for line in out.splitlines():
        if " enp" in line or " eth" in line:
            parts = line.split()
            ip = next((p for p in parts if "." in p and "/" in p), "")
            iface = parts[1]
            try:
                speed = open(f"/sys/class/net/{iface}/speed").read().strip()
            except OSError:
                speed = "?"
            return {"up": True, "iface": iface, "addr": ip, "speed": speed}
    return {"up": False, "detail": "no wired interface has an address — "
                                   "plug the dock switch into this laptop"}


PAGE = r"""<!doctype html><html><head><meta charset=utf-8>
<title>Camera Firmware LAN</title><style>
*{box-sizing:border-box} body{margin:0;background:#14161a;color:#e8eaed;
 font:14px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:22px}
h1{font-size:19px;margin:0 0 2px} .sub{color:#9aa0a6;font-size:12px;margin-bottom:18px}
.card{background:#1c1f25;border:1px solid #272b33;border-radius:10px;padding:16px;margin-bottom:14px}
.card h2{font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:#9aa0a6;margin:0 0 12px}
button{border:0;border-radius:8px;padding:11px 18px;font-size:14px;font-weight:600;
 cursor:pointer;color:#fff;background:#343a45}
button:hover{filter:brightness(1.18)} button:disabled{opacity:.4;cursor:not-allowed;filter:none}
.primary{background:#2563eb} .go{background:#16a34a} .ghost{background:#2a2f38}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
pre{background:#0e1014;border:1px solid #23272e;border-radius:8px;padding:12px;
 font:12px/1.55 ui-monospace,monospace;max-height:280px;overflow:auto;white-space:pre-wrap;margin:0}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.05em;
 color:#9aa0a6;font-weight:600;padding:7px 9px;border-bottom:1px solid #272b33}
td{padding:8px 9px;border-bottom:1px solid #1f232a;font-family:ui-monospace,monospace}
tr.jit td{background:#241618} tr.ok td{background:#13201a}
.good{color:#4ade80;font-weight:700} .bad{color:#f87171;font-weight:700}
.warn{color:#fbbf24} .mut{color:#9aa0a6}
.pill{display:inline-block;padding:2px 9px;border-radius:99px;font-size:11px;
 background:#262b33;color:#cbd2dc}
.big{font-size:26px;font-weight:700;font-family:ui-monospace,monospace}
.tiles{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:14px}
.tile{flex:1;min-width:140px;background:#171a1f;border:1px solid #23272e;
 border-radius:9px;padding:12px 14px}
.tile .k{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:#9aa0a6}
.note{color:#9aa0a6;font-size:12px;margin-top:10px}
.banner{display:none;background:#7f1d1d;border:1px solid #b91c1c;border-radius:8px;
 padding:11px 14px;margin-bottom:14px;font-weight:600}
</style></head><body><div class=wrap>
<h1>Camera Firmware &mdash; LAN</h1>
<div class=sub>Every recorder docked on the cable, and whether the camera attached to it
 is on the build that jitters. Nothing is opened on the pi, so a board mid-take is safe to scan.</div>

<div id=banner class=banner></div>

<div class=card>
  <h2>The cable</h2>
  <div id=link class=mut>checking&hellip;</div>
</div>

<div class=card>
  <h2>Docked recorders</h2>
  <div class=row>
    <button class=primary onclick="sweep()">Scan the cable</button>
    <button class=go onclick="push()">Scan &amp; send to dashboard</button>
    <span id=runstate class=pill>idle</span>
    <span id=when class=mut style=font-size:12px></span>
  </div>
  <div class=note>Reads two sysfs files per camera over SSH &mdash; <code>bcdDevice</code> and the
   raw USB descriptor blob. A board still booting answers TCP and then drops; those are retried
   once and otherwise listed as no-answer, so re-scan after a minute and they usually appear.</div>
  <div id=tiles class=tiles style=margin-top:14px></div>
  <div id=rows></div>
  <div id=skipped class=mut style="font-size:11px;margin-top:8px"></div>
</div>

<div class=card>
  <h2>Output</h2>
  <pre id=log class=mut>nothing run yet</pre>
</div>
</div>
<script>
const $=i=>document.getElementById(i);
const esc=s=>String(s==null?'':s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
let DOWN=false;
async function call(url,opts){
  try{
    const r=await fetch(url,opts); if(!r.ok) throw new Error('HTTP '+r.status);
    const j=await r.json();
    if(DOWN){DOWN=false;$('banner').style.display='none';}
    return j;
  }catch(e){
    DOWN=true; $('banner').style.display='block';
    $('banner').innerHTML='Not running &mdash; double-click <b>Camera Firmware LAN</b> on the '
      +'Desktop to start it again, then reload this page.';
    return null;
  }
}
function num(p){const m=/Camera\s+(\d+(?:\.\d+)?)\s*$/.exec((p||'').trim());return m?m[1]:'?';}
const BUILDS={'dither-16626':{v:'JITTERS',c:'bad',row:'jit'},
              'static-16640':{v:'clean',c:'good',row:'ok'}};

function draw(rows){
  if(!rows){ return; }
  let clean=0,jit=0,unk=0,nocam=0,bad=0,skip=0;
  let h='<table><tr><th>starcap</th><th>ip</th><th>camera</th><th>build</th>'
       +'<th>verdict</th><th>recorder</th></tr>';
  const sorted=[...rows].sort((a,b)=>String(a.name||a.ip).localeCompare(String(b.name||b.ip)));
  for(const r of sorted){
    if(r.not_recorder){ skip++; continue; }
    const who=esc(r.name||r.ip);
    if(r.error){ bad++;
      h+='<tr><td>'+who+'</td><td>'+esc(r.ip)+'</td><td>—</td><td>—</td>'
        +'<td class=warn>no answer</td><td class=mut>'+esc(r.error)+'</td></tr>'; continue; }
    if(!r.cameras||!r.cameras.length){ nocam++;
      h+='<tr><td>'+who+'</td><td>'+esc(r.ip)+'</td><td>—</td><td>—</td>'
        +'<td class=warn>NO CAMERA</td><td class=mut>'+esc(r.version||'')+'</td></tr>'; continue; }
    for(const c of r.cameras){
      const b=c.build, spec=BUILDS[b];
      if(b==='static-16640') clean++; else if(b==='dither-16626') jit++; else unk++;
      h+='<tr class='+(spec?spec.row:'')+'><td>'+who+'</td><td>'+esc(r.ip)+'</td>'
        +'<td>'+esc(num(c.product))+'</td><td>'+esc(b||'—')+'</td>'
        +'<td class="'+(spec?spec.c:'warn')+'">'+(spec?spec.v:'UNRECOGNISED')+'</td>'
        +'<td class=mut>'+esc(r.version||'')+'</td></tr>';
    }
  }
  $('rows').innerHTML=h+'</table>';
  const t=(k,v,cls)=>'<div class=tile><div class=k>'+k+'</div><div class="big '+(cls||'')+'">'+v+'</div></div>';
  $('tiles').innerHTML=t('clean',clean,'good')+t('jitters',jit,'bad')
    +(unk?t('unrecognised',unk,'warn'):'')+(nocam?t('no camera',nocam,'warn'):'')
    +(bad?t('no answer',bad,'mut'):'');
  /* The switch's management interface is on this segment permanently and is not a
     result. It was a counted tile and read as something going wrong on every single
     scan; one quiet line under the table is all it is worth. */
  $('skipped').textContent = skip
    ? skip+' non-recorder device on the cable (switch management interface) — skipped'
    : '';
}

async function link(){
  const l=await call('/api/link'); if(!l) return;
  $('link').innerHTML = l.up
    ? '<span class=good>●</span> '+esc(l.iface)+' &nbsp; '+esc(l.addr)
      +' &nbsp; <span class=mut>'+esc(l.speed)+' Mb/s</span>'
    : '<span class=bad>●</span> '+esc(l.detail);
}
async function sweep(){
  $('rows').innerHTML='<div class=mut>sweeping the cable…</div>';
  const r=await call('/api/sweep'); if(!r) return;
  if(r.error){ $('rows').innerHTML='<div class=bad>'+esc(r.error)+'</div>'; return; }
  draw(r.rows); $('when').textContent='scanned '+new Date().toLocaleTimeString();
}
async function push(){
  const r=await call('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({push:true})});
  if(r&&!r.ok) alert(r.error||'busy');
  poll();
}
async function poll(){
  const s=await call('/api/state'); if(!s) return;
  $('runstate').textContent=s.running?('running: '+s.running):'idle';
  $('runstate').style.background=s.running?'#7c2d12':'#262b33';
  for(const b of document.querySelectorAll('button')) b.disabled=!!s.running;
  if(s.log&&s.log.length){ $('log').textContent=s.log.join('\n'); $('log').className=''; }
  if(s.rows&&s.rows.length) draw(s.rows);
}
setInterval(poll,1500); setInterval(link,10000); link(); poll(); sweep();
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
        if self.path == "/api/link":
            return self._send(json.dumps(link_state()))
        if self.path == "/api/sweep":
            rows, err = sweep()
            if err:
                return self._send(json.dumps({"error": err}))
            STATE["rows"] = rows
            return self._send(json.dumps({"rows": rows}))
        self.send_error(404)

    def do_POST(self):
        if self.path != "/api/run":
            return self.send_error(404)
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or "{}")
        args = ["--push"] if body.get("push") else []
        ok = start(args, "scan+push" if args else "scan")
        self._send(json.dumps({"ok": ok, "error": None if ok else "a scan is already running"}))


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    url = f"http://127.0.0.1:{PORT}"
    print(f"camera firmware LAN -> {url}")
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
