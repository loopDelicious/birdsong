#!/usr/bin/env python3
"""Birdsong display app.

One process that (1) continuously listens on the USB mic and runs BirdNET in a
background thread, and (2) serves a full-screen kiosk web page that shows the
bird(s) currently being heard, with live photos and a runtime control panel.

    python app.py --min-conf 0.5

Open http://birdpi.local:8000  (Chromium kiosk on the Pi, or any LAN browser).
"""
import argparse
import contextlib
import datetime
import io
import os
import subprocess
import tempfile
import threading
import time
import warnings

import requests
from flask import Flask, jsonify, render_template_string, request

warnings.filterwarnings("ignore")

# ---------------------------------------------------------------- shared state
STATE_LOCK = threading.Lock()
ACTIVE = {}             # scientific_name -> live entry (with last_ts)
TODAY = {}              # scientific_name -> cumulative {common, count, last, image_url}
IMG_CACHE = {}          # scientific_name -> image url (or None)
HOLD_SECONDS = 90       # how long a bird stays on screen after last heard
MAX_ON_SCREEN = 6       # cap simultaneous birds shown

CONFIG = {
    "device": "plughw:2,0", "seconds": 3, "rate": 48000,
    "min_conf": 0.5, "lat": 37.77, "lon": -122.42, "use_location": False,
}


# ------------------------------------------------------------------ bird photo
def fetch_image(common, scientific):
    if scientific in IMG_CACHE:
        return IMG_CACHE[scientific]
    url = None
    for title in (common.replace(" ", "_"), scientific.replace(" ", "_")):
        try:
            r = requests.get(
                f"https://en.wikipedia.org/api/rest_v1/page/summary/{title}",
                timeout=6, headers={"User-Agent": "birdsong-pi/0.1"})
            if r.ok:
                j = r.json()
                url = (j.get("originalimage", {}).get("source")
                       or j.get("thumbnail", {}).get("source"))
                if url:
                    break
        except Exception:
            pass
    IMG_CACHE[scientific] = url
    return url


FETCHING = set()
FETCH_LOCK = threading.Lock()


def ensure_image(common, scientific):
    """Fetch a photo in a background thread if not cached. Never blocks the
    detection loop or the web server on the network."""
    if scientific in IMG_CACHE:
        return
    with FETCH_LOCK:
        if scientific in FETCHING:
            return
        FETCHING.add(scientific)

    def work():
        try:
            fetch_image(common, scientific)
        finally:
            with FETCH_LOCK:
                FETCHING.discard(scientific)

    threading.Thread(target=work, daemon=True).start()


# ----------------------------------------------------------- local species set
SPECIES_LIST = None
SPECIES_CACHE = {"key": None, "set": set()}


def local_scientific_set():
    """Scientific names plausible at CONFIG's location this week, cached daily.
    Returns None on failure so callers can fall back to clearing everything."""
    global SPECIES_LIST
    key = (round(CONFIG["lat"], 2), round(CONFIG["lon"], 2),
           datetime.date.today().isoformat())
    if SPECIES_CACHE["key"] == key:
        return SPECIES_CACHE["set"]
    try:
        from birdnetlib.species import SpeciesList
        with contextlib.redirect_stdout(io.StringIO()):
            if SPECIES_LIST is None:
                SPECIES_LIST = SpeciesList()
            lst = SPECIES_LIST.return_list(lat=CONFIG["lat"], lon=CONFIG["lon"],
                                           date=datetime.datetime.now(),
                                           threshold=0.03)
        s = {d.get("scientific_name") for d in lst}
        SPECIES_CACHE.update(key=key, set=s)
        return s
    except Exception as e:
        print("species list error:", e, flush=True)
        return None


# -------------------------------------------------------------- detection loop
def record_chunk(path):
    subprocess.run(
        ["arecord", "-D", CONFIG["device"], "-f", "S16_LE",
         "-r", str(CONFIG["rate"]), "-c", "1", "-d", str(CONFIG["seconds"]), path],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def detection_loop():
    with contextlib.redirect_stdout(io.StringIO()):
        from birdnetlib import Recording
        from birdnetlib.analyzer import Analyzer
        analyzer = Analyzer()
    print("🐦 detector ready", flush=True)

    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name
    try:
        while True:
            try:
                record_chunk(tmp)
                kwargs = {"min_conf": CONFIG["min_conf"]}
                if CONFIG["use_location"]:
                    kwargs.update(lat=CONFIG["lat"], lon=CONFIG["lon"],
                                  date=datetime.datetime.now())
                with contextlib.redirect_stdout(io.StringIO()):
                    # birdnetlib leaves the location allow-list set on the reused
                    # analyzer; clear it when location is off or filtering sticks.
                    if not CONFIG["use_location"]:
                        analyzer.custom_species_list = []
                    rec = Recording(analyzer, tmp, **kwargs)
                    rec.analyze()
                if rec.detections:
                    now = datetime.datetime.now()
                    ts = time.time()
                    timestr = now.strftime("%-I:%M %p")
                    with STATE_LOCK:
                        for d in rec.detections:
                            sci = d["scientific_name"]
                            prev = ACTIVE.get(sci)
                            ACTIVE[sci] = {
                                "common": d["common_name"], "scientific": sci,
                                "confidence": round(d["confidence"], 2),
                                "time": timestr,
                                "first_ts": prev["first_ts"] if prev else ts,
                                "last_ts": ts,
                            }
                            t = TODAY.get(sci)
                            if t:
                                t["count"] += 1
                                t["last"] = timestr
                            else:
                                TODAY[sci] = {"common": d["common_name"],
                                              "scientific": sci, "count": 1,
                                              "last": timestr}
                        names = sorted({d["common_name"] for d in rec.detections})
                    # photo lookup happens OUTSIDE the lock in a worker thread
                    for d in rec.detections:
                        ensure_image(d["common_name"], d["scientific_name"])
                    print(f"[{timestr}] {', '.join(names)}", flush=True)
            except Exception as e:
                print("loop error:", e, flush=True)
                time.sleep(1)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)


# -------------------------------------------------------------------- web app
app = Flask(__name__)


@app.after_request
def _nocache(resp):
    resp.headers["Cache-Control"] = "no-store"  # always serve fresh page/JS
    return resp


@app.route("/state")
def state():
    now = time.time()
    with STATE_LOCK:
        live = [e for e in ACTIVE.values() if now - e["last_ts"] <= HOLD_SECONDS]
        live.sort(key=lambda e: e["first_ts"])  # stable order: longest-active first
        live = live[:MAX_ON_SCREEN]
        birds = [{"common": e["common"], "scientific": e["scientific"],
                  "confidence": e["confidence"], "time": e["time"],
                  "image_url": IMG_CACHE.get(e["scientific"])} for e in live]
        today_list = sorted(TODAY.values(), key=lambda x: x["count"], reverse=True)
        return jsonify({
            "mode": "bird" if birds else "idle",
            "birds": birds,
            "today": today_list,
            "species_today": len(today_list),
            "clock": datetime.datetime.now().strftime("%-I:%M %p"),
            "config": {"min_conf": CONFIG["min_conf"],
                       "use_location": CONFIG["use_location"]},
        })


@app.route("/control", methods=["POST"])
def control():
    data = request.get_json(force=True, silent=True) or {}
    toggled_on = False
    with STATE_LOCK:
        if "min_conf" in data:
            CONFIG["min_conf"] = max(0.1, min(0.95, float(data["min_conf"])))
        if "use_location" in data:
            newv = bool(data["use_location"])
            toggled_on = newv and newv != CONFIG["use_location"]
            CONFIG["use_location"] = newv
    # turning the filter ON: prune only the non-local birds (keep local ones).
    # Computed outside the lock since it may load the meta model.
    if toggled_on:
        local = local_scientific_set()
        with STATE_LOCK:
            if local is None:
                ACTIVE.clear()  # fallback if species list unavailable
            else:
                for sci in [s for s in ACTIVE if s not in local]:
                    ACTIVE.pop(sci, None)
    return jsonify({"min_conf": CONFIG["min_conf"],
                    "use_location": CONFIG["use_location"]})


@app.route("/clear", methods=["POST"])
def clear():
    with STATE_LOCK:
        ACTIVE.clear()
    return jsonify({"ok": True})


@app.route("/")
def index():
    return render_template_string(PAGE)


PAGE = r"""
<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Birdsong</title>
<style>
  :root { --bg:#0b0f14; --fg:#f2f5f7; --muted:#7f8c99; --accent:#9fe3c5;
    --panel:#121922; --line:#243040; }
  * { box-sizing:border-box; margin:0; padding:0; }
  html,body { height:100%; background:var(--bg); color:var(--fg);
    font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
    overflow:hidden; }
  body.hidecursor { cursor:none; }
  #clock { position:fixed; top:4vmin; right:5vmin; z-index:6;
    font-size:2.4vmin; color:var(--muted); text-shadow:0 1px 8px #000; }

  /* ---- multi-bird grid ---- */
  #grid { position:fixed; inset:0; display:grid; gap:2px; z-index:1; }
  .card { position:relative; overflow:hidden; background:#0f141b; }
  .card .img { position:absolute; inset:0; background-size:cover;
    background-position:center; transition:opacity 1.2s ease; opacity:0; }
  .card .scrim { position:absolute; inset:0; background:linear-gradient(180deg,
    rgba(11,15,20,.05) 40%, rgba(11,15,20,.9) 100%); }
  .card .label { position:absolute; left:0; right:0; bottom:0;
    padding:3vmin 3.5vmin; z-index:2; }
  .card .common { font-weight:700; line-height:1.05;
    text-shadow:0 2px 16px rgba(0,0,0,.7); }
  .card .sci { font-style:italic; color:var(--accent); margin-top:.6vmin; }
  .card .meta { color:var(--muted); margin-top:1vmin; }
  @keyframes fade { from{opacity:0} to{opacity:1} }

  /* ---- idle ---- */
  #idle { position:fixed; inset:0; z-index:2; display:flex; flex-direction:column;
    align-items:center; justify-content:center; text-align:center; gap:2.5vmin;
    opacity:0; transition:opacity 1s ease; pointer-events:none; }
  #idle.show { opacity:1; }
  #idle .pulse { width:13vmin; height:13vmin; color:var(--accent);
    animation:breathe 3.5s ease-in-out infinite; }
  #idle .label { font-size:3.2vmin; color:var(--muted); letter-spacing:.5vmin;
    text-transform:uppercase; }
  #idle .stats { font-size:2.6vmin; }
  #idle .chips { display:flex; flex-wrap:wrap; gap:1.4vmin; justify-content:center;
    max-width:80vw; margin-top:2vmin; }
  .chip { background:var(--panel); border:1px solid var(--line); border-radius:999px;
    padding:1vmin 2.4vmin; font-size:2.2vmin; color:#cdd6df; }
  .chip b { color:var(--accent); }
  @keyframes breathe { 0%,100%{transform:scale(1);opacity:.85}
    50%{transform:scale(1.12);opacity:1} }

  /* ---- control panel ---- */
  #fab { position:fixed; bottom:3.5vmin; right:3.5vmin; z-index:10; width:7vmin;
    height:7vmin; min-width:46px; min-height:46px; border-radius:50%;
    background:var(--panel); border:1px solid var(--line); color:var(--fg);
    font-size:3vmin; cursor:pointer; opacity:.5; transition:opacity .25s;
    display:flex; align-items:center; justify-content:center; }
  #fab:hover { opacity:1; }
  #panel { position:fixed; bottom:13vmin; right:3.5vmin; z-index:10; width:340px;
    max-width:80vw; background:var(--panel); border:1px solid var(--line);
    border-radius:16px; padding:18px 18px 14px; box-shadow:0 18px 50px rgba(0,0,0,.6);
    transform:translateY(12px) scale(.98); opacity:0; pointer-events:none;
    transition:.2s ease; }
  #panel.open { transform:none; opacity:1; pointer-events:auto; }
  #panel h3 { font-size:13px; text-transform:uppercase; letter-spacing:1px;
    color:var(--muted); margin-bottom:14px; }
  .row { display:flex; align-items:center; justify-content:space-between;
    padding:11px 0; border-top:1px solid var(--line); }
  .row:first-of-type { border-top:none; }
  .row .name { font-size:15px; }
  .row .sub { font-size:12px; color:var(--muted); margin-top:2px; }
  /* toggle */
  .toggle { width:46px; height:26px; border-radius:999px; background:#2a3645;
    position:relative; cursor:pointer; transition:.2s; flex:none; }
  .toggle.on { background:var(--accent); }
  .toggle::after { content:""; position:absolute; top:3px; left:3px; width:20px;
    height:20px; border-radius:50%; background:#fff; transition:.2s; }
  .toggle.on::after { left:23px; }
  /* stepper */
  .stepper { display:flex; align-items:center; gap:10px; }
  .stepper button { width:30px; height:30px; border-radius:8px; background:#1d2735;
    border:1px solid var(--line); color:var(--fg); font-size:18px; cursor:pointer; }
  .stepper button:hover { background:#26344a; }
  .stepper .val { min-width:42px; text-align:center; font-variant-numeric:tabular-nums; }
  .clearbtn { width:100%; margin-top:14px; padding:11px; border-radius:10px;
    background:#1d2735; border:1px solid var(--line); color:var(--fg); font-size:14px;
    cursor:pointer; }
  .clearbtn:hover { background:#26344a; }
</style></head><body class="hidecursor">
<div id="clock"></div>
<div id="grid"></div>
<div id="idle"><svg class="pulse" viewBox="0 0 100 100" fill="currentColor" aria-hidden="true">
    <ellipse cx="44" cy="60" rx="27" ry="21"/>
    <circle cx="68" cy="40" r="15"/>
    <path d="M22 60 L1 51 L19 67 Z"/>
    <path d="M81 37 L98 35 L81 47 Z"/>
    <circle cx="72" cy="37" r="2.6" fill="var(--bg)"/>
  </svg><div class="label">Listening</div>
  <div class="stats" id="idleStats"></div><div class="chips" id="chips"></div></div>

<button id="fab" title="Controls">⚙</button>
<div id="panel">
  <h3>Controls</h3>
  <div class="row">
    <div><div class="name">Local filter</div>
      <div class="sub">Only birds found near San Francisco</div></div>
    <div class="toggle" id="locToggle"></div>
  </div>
  <div class="row">
    <div><div class="name">Min confidence</div>
      <div class="sub">Higher = fewer false positives</div></div>
    <div class="stepper"><button id="confDown">−</button>
      <div class="val" id="confVal">0.50</div><button id="confUp">+</button></div>
  </div>
  <button class="clearbtn" id="clearBtn">Clear screen → Listening</button>
</div>

<script>
const grid=document.getElementById('grid'), idle=document.getElementById('idle'),
  idleStats=document.getElementById('idleStats'), chips=document.getElementById('chips'),
  clock=document.getElementById('clock'), fab=document.getElementById('fab'),
  panel=document.getElementById('panel'), locToggle=document.getElementById('locToggle'),
  confVal=document.getElementById('confVal'), confUp=document.getElementById('confUp'),
  confDown=document.getElementById('confDown'), clearBtn=document.getElementById('clearBtn');

// ---- control panel ----
let cfg={min_conf:0.5, use_location:false};
fab.onclick=()=>panel.classList.toggle('open');
function applyCfg(c){ cfg=c; confVal.textContent=Number(c.min_conf).toFixed(2);
  locToggle.classList.toggle('on', !!c.use_location); }
async function post(url, body){ const r=await fetch(url,{method:'POST',
  headers:{'Content-Type':'application/json'}, body:JSON.stringify(body||{})});
  return r.json(); }
locToggle.onclick=async()=>applyCfg(await post('/control',{use_location:!cfg.use_location}));
confUp.onclick=async()=>applyCfg(await post('/control',{min_conf:Math.min(0.95,cfg.min_conf+0.05)}));
confDown.onclick=async()=>applyCfg(await post('/control',{min_conf:Math.max(0.10,cfg.min_conf-0.05)}));
clearBtn.onclick=async()=>{ await post('/clear'); tick(); };

// ---- auto-hide cursor ----
let curTimer; window.addEventListener('mousemove',()=>{
  document.body.classList.remove('hidecursor'); clearTimeout(curTimer);
  curTimer=setTimeout(()=>document.body.classList.add('hidecursor'),3000); });

// ---- render multi-bird grid ----
const cards=new Map();
function makeCard(b){ const el=document.createElement('div'); el.className='card';
  el.innerHTML=`<div class="img"></div><div class="scrim"></div>
    <div class="label"><div class="common"></div><div class="sci"></div>
    <div class="meta"></div></div>`;
  if(b.image_url){ const im=new Image(); im.onload=()=>{ const d=el.querySelector('.img');
    d.style.backgroundImage=`url("${b.image_url}")`; d.style.opacity=1; }; im.src=b.image_url; }
  return el; }
function render(birds){
  // stable alphabetical order so cards never jump around between polls
  birds=birds.slice().sort((a,b)=> a.scientific<b.scientific?-1:1);
  const keys=new Set(birds.map(b=>b.scientific));
  for(const [k,el] of cards){ if(!keys.has(k)){ el.remove(); cards.delete(k); } }
  const n=birds.length, cols=Math.ceil(Math.sqrt(n)), rows=Math.ceil(n/cols);
  grid.style.gridTemplateColumns=`repeat(${cols},1fr)`;
  grid.style.gridTemplateRows=`repeat(${rows},1fr)`;
  const nameSize=Math.max(2.4, 8/cols), sciSize=Math.max(1.5,3.2/cols),
    metaSize=Math.max(1.3,2.4/cols);
  birds.forEach(b=>{ let el=cards.get(b.scientific);
    // only NEW cards touch the DOM tree; existing cards stay put (no blink)
    if(!el){ el=makeCard(b); cards.set(b.scientific,el); grid.appendChild(el); }
    const c=el.querySelector('.common'); if(c.textContent!==b.common) c.textContent=b.common;
    c.style.fontSize=nameSize+'vmin';
    const sc=el.querySelector('.sci'); if(sc.textContent!==b.scientific) sc.textContent=b.scientific;
    sc.style.fontSize=sciSize+'vmin';
    const m=el.querySelector('.meta'); m.style.fontSize=metaSize+'vmin';
    const meta=`heard at ${b.time} · ${Math.round(b.confidence*100)}% confidence`;
    if(m.textContent!==meta) m.textContent=meta;
  });
}

async function tick(){
  let s; try{ s=await(await fetch('/state',{cache:'no-store'})).json(); }catch(e){ return; }
  clock.textContent=s.clock;
  if(s.config) applyCfg(s.config);
  if(s.mode==='bird' && s.birds.length){
    idle.classList.remove('show'); grid.style.display='grid'; render(s.birds);
  } else {
    grid.style.display='none'; for(const [k,el] of cards){ el.remove(); } cards.clear();
    idle.classList.add('show');
    idleStats.textContent=s.species_today? `${s.species_today} species heard today`
      : 'No birds heard yet today';
    chips.innerHTML=''; s.today.slice(0,8).forEach(t=>{ const d=document.createElement('div');
      d.className='chip'; d.innerHTML=`${t.common} <b>×${t.count}</b>`; chips.appendChild(d); });
  }
}
tick(); setInterval(tick,1500);
</script>
</body></html>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default="plughw:2,0")
    ap.add_argument("--seconds", type=int, default=3)
    ap.add_argument("--rate", type=int, default=48000)
    ap.add_argument("--min-conf", type=float, default=0.5)
    ap.add_argument("--lat", type=float, default=37.77)
    ap.add_argument("--lon", type=float, default=-122.42)
    ap.add_argument("--location", action="store_true", help="start with local filter on")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    CONFIG.update(device=args.device, seconds=args.seconds, rate=args.rate,
                  min_conf=args.min_conf, lat=args.lat, lon=args.lon,
                  use_location=args.location)

    threading.Thread(target=detection_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=args.port, threaded=True)


if __name__ == "__main__":
    main()
