#!/usr/bin/env python3
"""Firmware jitter bench — browser front end.

Same shape as camrig and starcapfinder: stdlib only, a local HTTP server, and a
single page. No pip install, nothing to keep running between sessions.

The page drives fwbench.py as a subprocess and streams its output back, because a
run takes ~4 minutes and puts a FULLSCREEN ramp over the browser while it works —
so progress has to survive the window being covered, and the page has to be able
to pick the run back up when the ramp window goes away.

    ./ui.py            -> http://127.0.0.1:8092
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
BENCH = os.path.join(HERE, "..", "fwbench.py")
RESULTS = os.path.join(HERE, "results")
PORT = int(os.environ.get("PORT", "8092"))

# One bench process at a time: two would fight over /dev/video0 and over the
# screen, and the loser's numbers would look like a camera fault.
LOCK = threading.Lock()
STATE = {"running": None, "log": [], "started": 0.0}


def _pump(proc, label):
    for line in iter(proc.stdout.readline, ""):
        STATE["log"].append(line.rstrip("\n"))
        del STATE["log"][:-400]
    proc.wait()
    STATE["log"].append(f"[{label} finished, exit {proc.returncode}]")
    STATE["running"] = None


def launch(args, label):
    if not LOCK.acquire(blocking=False):
        return False
    try:
        if STATE["running"]:
            return False
        STATE["log"] = [f"[{label}]"]
        STATE["running"] = label
        p = subprocess.Popen([sys.executable, "-u", BENCH] + args, cwd=HERE,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, bufsize=1)
        threading.Thread(target=_pump, args=(p, label), daemon=True).start()
        return True
    finally:
        LOCK.release()


def results():
    out = []
    if os.path.isdir(RESULTS):
        for f in sorted(os.listdir(RESULTS)):
            if f.endswith(".json"):
                try: out.append(json.load(open(os.path.join(RESULTS, f))))
                except Exception: pass                        # noqa: BLE001
    return out


PAGE = r"""<!doctype html><html><head><meta charset=utf-8>
<title>Firmware Jitter Bench</title><style>
*{box-sizing:border-box} body{margin:0;background:#14161a;color:#e8eaed;
 font:14px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:22px}
h1{font-size:19px;margin:0 0 2px} .sub{color:#9aa0a6;font-size:12px;margin-bottom:18px}
.card{background:#1c1f25;border:1px solid #272b33;border-radius:10px;padding:16px;margin-bottom:14px}
.card h2{font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:#9aa0a6;margin:0 0 12px}
label{display:block;font-size:11px;color:#9aa0a6;margin-bottom:4px}
input{background:#12141a;border:1px solid #2f343d;color:#e8eaed;border-radius:7px;
 padding:9px 11px;font-size:15px;width:100%;font-family:ui-monospace,monospace}
input:focus{outline:none;border-color:#4f8cff}
.row{display:flex;gap:12px;flex-wrap:wrap;align-items:flex-end}
.row>div{flex:1;min-width:130px}
button{border:0;border-radius:8px;padding:11px 18px;font-size:14px;font-weight:600;
 cursor:pointer;color:#fff;background:#343a45}
button:hover{filter:brightness(1.18)} button:disabled{opacity:.4;cursor:not-allowed;filter:none}
.primary{background:#2563eb} .go{background:#dc2626} .ghost{background:#2a2f38}
pre{background:#0e1014;border:1px solid #23272e;border-radius:8px;padding:12px;
 font:12px/1.55 ui-monospace,monospace;max-height:260px;overflow:auto;white-space:pre-wrap;margin:0}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.05em;
 color:#9aa0a6;font-weight:600;padding:7px 9px;border-bottom:1px solid #272b33}
td{padding:7px 9px;border-bottom:1px solid #1f232a;font-family:ui-monospace,monospace}
.pass{color:#4ade80;font-weight:700} .fail{color:#f87171;font-weight:700}
.warn{color:#fbbf24} .mut{color:#9aa0a6}
.pill{display:inline-block;padding:2px 8px;border-radius:99px;font-size:11px;
 background:#262b33;color:#cbd2dc}
.banner{display:none;background:#7f1d1d;border:1px solid #b91c1c;border-radius:8px;
 padding:11px 14px;margin-bottom:14px;font-weight:600}
.matrix td,.matrix th{text-align:center} .matrix td:first-child,.matrix th:first-child{text-align:left}
.ok{color:#4ade80} .miss{color:#4b5563}
.hint{font-size:12px;color:#9aa0a6;margin-top:10px}
</style></head><body><div class=wrap>
<h1>Firmware Jitter Bench</h1>
<div class=sub>Does this camera firmware cause the frame-timing fault?</div>

<div class=banner id=banner></div>

<div class=card>
  <h2>Measure</h2>
  <div class=row>
    <div><label>CAMERA (as the camera reports it)</label>
      <input id=cam placeholder="4.1" autocomplete=off></div>
    <div><label>FIRMWARE BUILD NOW ON IT</label>
      <input id=fw placeholder="114" autocomplete=off></div>
    <div style="flex:0 0 auto"><button class=go id=run>Measure &mdash; 2 reps, ~4 min</button></div>
    <div style="flex:0 0 auto"><button class=ghost id=probe>Check aim (15 s)</button></div>
  </div>
  <div class=hint>The screen flashes black/white and covers this window. Don't touch
    the screen or move the camera until it says it's done.</div>
</div>

<div class=card>
  <h2>Progress <span class=pill id=runstate>idle</span></h2>
  <pre id=log>nothing yet</pre>
</div>

<div class=card>
  <h2>Coverage</h2>
  <div id=matrix class=mut>no runs yet</div>
</div>

<div class=card>
  <h2>Runs</h2>
  <div id=table class=mut>no runs yet</div>
</div>

<div class=card>
  <h2>Report</h2>
  <button class=primary id=rep>Write report to Desktop</button>
  <div class=hint id=repmsg></div>
</div>
</div><script>
const $=id=>document.getElementById(id);
let busy=false;
async function post(p,b){return (await fetch(p,{method:'POST',body:JSON.stringify(b||{})})).json()}
function verdict(r){return r.ramp.pass?'<span class=pass>PASS</span>':'<span class=fail>FAIL</span>'}
async function tick(){
  const s=await (await fetch('/api/state')).json();
  busy=!!s.running;
  $('runstate').textContent=s.running||'idle';
  $('runstate').style.background=s.running?'#7c2d12':'#262b33';
  $('log').textContent=s.log.length?s.log.join('\n'):'nothing yet';
  $('log').scrollTop=$('log').scrollHeight;
  for(const b of ['run','probe','rep']) $(b).disabled=busy;

  const R=s.results;
  if(!R.length){$('table').textContent='no runs yet';$('matrix').textContent='no runs yet';}
  else{
    let h='<table><tr><th>camera</th><th>label</th><th>measured build</th><th>rep</th>'
        +'<th>interval</th><th>control</th><th>ramp</th><th>p99</th><th>verdict</th></tr>';
    for(const r of R){
      const flag=(r.stimulus_reached_camera&&r.ramp.trustworthy)?'':' <span class=warn title="excluded from the summary">&#9888;</span>';
      const iv=(r.ramp.nominal_set||[]).join(' / ');
      h+=`<tr><td>${r.camera}${flag}</td><td>${r.firmware_label}</td>
        <td class=mut>${r.family}</td><td>${r.rep}</td><td>${iv}</td>
        <td class=mut>${r.control.jitter_per_s.toFixed(2)}/s</td>
        <td><b>${r.ramp.jitter_per_s.toFixed(2)}/s</b></td>
        <td>${r.ramp.p99_us}</td><td>${verdict(r)}</td></tr>`;
    }
    $('table').innerHTML=h+'</table>';
    const cams=[...new Set(R.map(r=>r.camera))].sort();
    const fws=[...new Set(R.map(r=>String(r.firmware_label)))].sort();
    let m='<table class=matrix><tr><th>camera</th>'+fws.map(f=>`<th>fw ${f}</th>`).join('')+'</tr>';
    for(const c of cams){
      m+=`<tr><td>${c}</td>`;
      for(const f of fws){
        const n=R.filter(r=>r.camera===c&&String(r.firmware_label)===f).length;
        m+=`<td class="${n>=2?'ok':'miss'}">${n>=2?'&#10003; '+n+'/2':n+'/2'}</td>`;
      }
      m+='</tr>';
    }
    $('matrix').innerHTML=m+'</table>';
    const both=cams.filter(c=>fws.every(f=>R.some(r=>r.camera===c&&String(r.firmware_label)===f)));
    $('banner').style.display=(fws.length>1&&!both.length)?'block':'none';
    $('banner').textContent='No camera has been measured on BOTH builds yet — without that, '
      +'camera-to-camera variation is not excluded and the report will say so. '
      +'Flash one unit both ways.';
  }
}
$('run').onclick=async()=>{
  const cam=$('cam').value.trim(), fw=$('fw').value.trim();
  if(!cam||!fw){alert('Camera number and firmware build are both needed.');return}
  await post('/api/run',{camera:cam,fw:fw}); tick();
};
$('probe').onclick=async()=>{await post('/api/probe');tick()};
$('rep').onclick=async()=>{const r=await post('/api/report');
  $('repmsg').textContent=r.ok?('written to '+r.path):('failed: '+(r.error||'')); tick()};
tick(); setInterval(tick,1200);
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, ctype="application/json"):
        b = body if isinstance(body, bytes) else body.encode()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            return self._send(PAGE, "text/html; charset=utf-8")
        if self.path == "/api/state":
            return self._send(json.dumps({"running": STATE["running"],
                                          "log": STATE["log"][-120:],
                                          "results": results()}))
        self.send_error(404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        try: body = json.loads(self.rfile.read(n) or "{}")
        except ValueError: body = {}
        if self.path == "/api/probe":
            return self._send(json.dumps({"ok": launch(["probe"], "aim check")}))
        if self.path == "/api/run":
            cam = str(body.get("camera", "")).strip()
            fw = str(body.get("fw", "")).strip()
            # Both end up in a filename, so keep them to what a unit number and a
            # build label actually look like.
            if not re.fullmatch(r"[\w.\-]{1,24}", cam) or \
               not re.fullmatch(r"[\w.\-]{1,24}", fw):
                return self._send(json.dumps({"ok": False, "error": "bad name"}))
            return self._send(json.dumps({"ok": launch(
                ["run", "--camera", cam, "--fw", fw, "--reps", "2"],
                f"camera {cam} on firmware {fw}")}))
        if self.path == "/api/report":
            out = os.path.expanduser("~/Desktop/firmware_jitter_report.md")
            r = subprocess.run([sys.executable, BENCH, "report", "-o", out],
                               cwd=HERE, capture_output=True, text=True)
            return self._send(json.dumps({"ok": r.returncode == 0, "path": out,
                                          "error": r.stderr[-300:]}))
        self.send_error(404)


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    url = f"http://127.0.0.1:{PORT}"
    print(f"Firmware jitter bench -> {url}   (ctrl-c to stop)")
    if "--no-browser" not in sys.argv:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
