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
        live.sort(key=lambda e: e["last_ts"], reverse=True)  # most-recent first (hero)
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


@app.route("/demo", methods=["POST"])
def demo():
    """Inject species into the live display (for screenshots / demos). Body:
    {"species": [{"common": ..., "scientific": ..., "confidence": ...}, ...]}"""
    data = request.get_json(force=True, silent=True) or {}
    now = datetime.datetime.now().strftime("%-I:%M %p")
    ts = time.time()
    for i, s in enumerate(data.get("species", [])):
        sci = s["scientific"]
        fetch_image(s["common"], sci)  # synchronous so the photo is ready for the shot
        with STATE_LOCK:
            ACTIVE[sci] = {"common": s["common"], "scientific": sci,
                           "confidence": round(float(s.get("confidence", 0.8)), 2),
                           "time": s.get("time", now),
                           "first_ts": ts + i * 0.001, "last_ts": ts}
    return jsonify({"ok": True, "active": list(ACTIVE)})


@app.route("/")
def index():
    return render_template_string(PAGE)


PAGE = r"""
<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Birdsong</title>
<style>
  :root { --bg:#0b0f14; --fg:#f4efe8; --muted:#aeb8c2; --accent:#e7b59a;
    --panel:#121922; --line:#243040; --serif:Georgia,'Times New Roman',serif; }
  * { box-sizing:border-box; margin:0; padding:0; }
  html,body { height:100%; background:var(--bg); color:var(--fg);
    font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif; overflow:hidden; }
  body.hidecursor { cursor:none; }
  #stage { position:fixed; inset:0; transition:filter 2s ease; }
  #photos { position:absolute; inset:0; background:var(--bg); }
  .photo { position:absolute; inset:0; opacity:0; transition:opacity 1.6s ease; }
  .photo.on { opacity:1; }
  .haze { position:absolute; inset:0; background-size:cover; background-position:center;
    transform:scale(1.6); filter:blur(30px) brightness(.82) saturate(1.12);
    animation:pan 50s ease-in-out infinite alternate; }
  @keyframes pan { from{transform:scale(1.54) translate(-1.5%,-1%)}
    to{transform:scale(1.7) translate(1.5%,-2.5%)} }
  .subject { position:absolute; inset:0; background-size:cover; background-position:center;
    opacity:1; transition:opacity 1.6s ease;
    -webkit-mask-image:radial-gradient(ellipse 66% 74% at 50% 47%,#000 34%,transparent 86%);
    mask-image:radial-gradient(ellipse 66% 74% at 50% 47%,#000 34%,transparent 86%); }
  #photos.resting .subject { opacity:0; }
  #photos.resting .haze { filter:blur(42px) brightness(.6) saturate(1.05); }
  #scrim { position:absolute; inset:0; background:linear-gradient(56deg,
    rgba(7,9,12,.85) 0%, rgba(7,9,12,.28) 40%, rgba(7,9,12,0) 64%); }
  #info { position:absolute; left:0; bottom:0; padding:6vmin 6.5vmin; max-width:74vw;
    transition:opacity 1.2s ease; }
  #info.hide { opacity:0; }
  #common { font-family:var(--serif); font-size:5.6vmin; line-height:1.04;
    text-shadow:0 2px 24px rgba(0,0,0,.55); }
  #sci { font-style:italic; color:var(--accent); font-size:2.5vmin; margin-top:1vmin; }
  #meta { color:var(--muted); font-size:2.2vmin; margin-top:1.6vmin; }
  #also { color:#8b96a3; font-size:2vmin; margin-top:1.2vmin; }
  #idle { position:absolute; inset:0; display:flex; flex-direction:column; align-items:center;
    justify-content:center; text-align:center; gap:2.4vmin; opacity:0;
    transition:opacity 1.4s ease; pointer-events:none; }
  #idle.show { opacity:1; }
  #idle .dot { width:1.5vmin; height:1.5vmin; border-radius:50%; background:#9fe3c5;
    animation:breathe 4s ease-in-out infinite; }
  #idle .qlabel { color:#c4cdd6; font-size:2.6vmin; letter-spacing:.4vmin; }
  #idle .qstat { font-family:var(--serif); font-size:3.6vmin; }
  #idle .qlist { color:#8b96a3; font-size:2.2vmin; max-width:80vw; }
  @keyframes breathe { 0%,100%{opacity:.5;transform:scale(1)} 50%{opacity:.95;transform:scale(1.14)} }
  #wave { position:absolute; right:6vmin; bottom:6vmin; display:flex; gap:.5vmin;
    align-items:center; height:2.4vmin; opacity:.42; }
  #wave span { width:.4vmin; height:100%; background:rgba(255,255,255,.7); border-radius:2px;
    transform-origin:center; animation:wv 1.7s ease-in-out infinite; }
  @keyframes wv { 0%,100%{transform:scaleY(.22)} 50%{transform:scaleY(1)} }
  #fab { position:fixed; bottom:3.5vmin; right:3.5vmin; z-index:10; width:6.5vmin; height:6.5vmin;
    min-width:46px; min-height:46px; border-radius:50%; background:var(--panel);
    border:1px solid var(--line); color:var(--fg); cursor:pointer; opacity:0; pointer-events:none;
    transition:opacity .35s; display:flex; align-items:center; justify-content:center; }
  body.ui #fab { opacity:.5; pointer-events:auto; }
  #fab:hover { opacity:1; }
  #panel { position:fixed; bottom:12vmin; right:3.5vmin; z-index:10; width:340px; max-width:80vw;
    background:var(--panel); border:1px solid var(--line); border-radius:16px; padding:18px 18px 14px;
    box-shadow:0 18px 50px rgba(0,0,0,.6); transform:translateY(12px) scale(.98); opacity:0;
    pointer-events:none; transition:.2s ease; }
  #panel.open { transform:none; opacity:1; pointer-events:auto; }
  #panel h3 { font-size:13px; text-transform:uppercase; letter-spacing:1px; color:#7f8c99; margin-bottom:14px; }
  .row { display:flex; align-items:center; justify-content:space-between; padding:11px 0;
    border-top:1px solid var(--line); }
  .row:first-of-type { border-top:none; }
  .row .name { font-size:15px; }
  .row .sub { font-size:12px; color:#7f8c99; margin-top:2px; }
  .toggle { width:46px; height:26px; border-radius:999px; background:#2a3645; position:relative;
    cursor:pointer; transition:.2s; flex:none; }
  .toggle.on { background:#9fe3c5; }
  .toggle::after { content:""; position:absolute; top:3px; left:3px; width:20px; height:20px;
    border-radius:50%; background:#fff; transition:.2s; }
  .toggle.on::after { left:23px; }
  .stepper { display:flex; align-items:center; gap:10px; }
  .stepper button { width:30px; height:30px; border-radius:8px; background:#1d2735;
    border:1px solid var(--line); color:var(--fg); font-size:18px; cursor:pointer; }
  .stepper .val { min-width:42px; text-align:center; font-variant-numeric:tabular-nums; }
  .clearbtn { width:100%; margin-top:14px; padding:11px; border-radius:10px; background:#1d2735;
    border:1px solid var(--line); color:var(--fg); font-size:14px; cursor:pointer; }
</style></head><body class="hidecursor">
<div id="stage">
  <div id="photos">
    <div class="photo on" id="layerA"><div class="haze"></div><div class="subject"></div></div>
    <div class="photo" id="layerB"><div class="haze"></div><div class="subject"></div></div>
  </div>
  <div id="scrim"></div>
  <div id="info" class="hide">
    <div id="common"></div><div id="sci"></div><div id="meta"></div><div id="also"></div>
  </div>
  <div id="idle">
    <div class="dot"></div>
    <div class="qlabel" id="qlabel">listening</div>
    <div class="qstat" id="qstat"></div>
    <div class="qlist" id="qlist"></div>
  </div>
  <div id="wave"></div>
</div>
<button id="fab" title="Controls" aria-label="Controls">
  <svg viewBox="0 0 24 24" width="46%" height="46%" fill="none" stroke="currentColor"
    stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
    <circle cx="12" cy="12" r="3"/>
    <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>
  </svg>
</button>
<div id="panel">
  <h3>Controls</h3>
  <div class="row"><div><div class="name">Local filter</div>
    <div class="sub">Only birds found near San Francisco</div></div>
    <div class="toggle" id="locToggle"></div></div>
  <div class="row"><div><div class="name">Min confidence</div>
    <div class="sub">Higher = fewer false positives</div></div>
    <div class="stepper"><button id="confDown">−</button>
    <div class="val" id="confVal">0.50</div><button id="confUp">+</button></div></div>
  <button class="clearbtn" id="clearBtn">Clear → resting</button>
</div>
<script>
const photos=document.getElementById('photos'),
  layers=[document.getElementById('layerA'),document.getElementById('layerB')],
  info=document.getElementById('info'), common=document.getElementById('common'),
  sci=document.getElementById('sci'), meta=document.getElementById('meta'),
  also=document.getElementById('also'), idle=document.getElementById('idle'),
  qlabel=document.getElementById('qlabel'), qstat=document.getElementById('qstat'),
  qlist=document.getElementById('qlist'), stage=document.getElementById('stage'),
  fab=document.getElementById('fab'), panel=document.getElementById('panel'),
  locToggle=document.getElementById('locToggle'), confVal=document.getElementById('confVal'),
  confUp=document.getElementById('confUp'), confDown=document.getElementById('confDown'),
  clearBtn=document.getElementById('clearBtn'), wave=document.getElementById('wave');

let cfg={min_conf:0.5, use_location:true};
fab.onclick=()=>panel.classList.toggle('open');
function applyCfg(c){ cfg=c; confVal.textContent=Number(c.min_conf).toFixed(2);
  locToggle.classList.toggle('on', !!c.use_location); }
async function post(u,b){ const r=await fetch(u,{method:'POST',
  headers:{'Content-Type':'application/json'}, body:JSON.stringify(b||{})}); return r.json(); }
locToggle.onclick=async()=>applyCfg(await post('/control',{use_location:!cfg.use_location}));
confUp.onclick=async()=>applyCfg(await post('/control',{min_conf:Math.min(0.95,cfg.min_conf+0.05)}));
confDown.onclick=async()=>applyCfg(await post('/control',{min_conf:Math.max(0.10,cfg.min_conf-0.05)}));
clearBtn.onclick=async()=>{ await post('/clear'); tick(); };

let curTimer;
function activity(){ document.body.classList.remove('hidecursor'); document.body.classList.add('ui');
  clearTimeout(curTimer); curTimer=setTimeout(()=>{ document.body.classList.add('hidecursor');
    document.body.classList.remove('ui'); }, 3500); }
window.addEventListener('mousemove',activity); window.addEventListener('touchstart',activity);

for(let i=0;i<26;i++){ const s=document.createElement('span');
  s.style.animationDelay=(-Math.random()*1.7).toFixed(2)+'s'; wave.appendChild(s); }

function nightDim(){ const d=new Date(), h=d.getHours()+d.getMinutes()/60; let b;
  if(h>=7&&h<19) b=1; else if(h>=19&&h<22) b=1-(h-19)/3*0.4;
  else if(h>=22||h<5) b=0.6; else b=0.6+(h-5)/2*0.4;
  stage.style.filter='brightness('+b.toFixed(2)+')'; }

let active=0, heroKey=null, heroImg=null;
function setLayer(el,url){ const hz=el.querySelector('.haze'), sb=el.querySelector('.subject');
  const v = url?('url("'+url+'")'):''; hz.style.backgroundImage=v; sb.style.backgroundImage=v; }
function crossfade(url){ const next=active^1; setLayer(layers[next],url);
  layers[next].classList.add('on'); layers[active].classList.remove('on'); active=next; }
function partOfDay(){ const h=new Date().getHours();
  return h>=5&&h<12?'morning':h>=12&&h<17?'afternoon':h>=17&&h<21?'evening':'night'; }

async function tick(){
  let s; try{ s=await(await fetch('/state',{cache:'no-store'})).json(); }catch(e){ return; }
  if(s.config) applyCfg(s.config);
  nightDim();
  if(s.mode==='bird' && s.birds.length){
    photos.classList.remove('resting'); idle.classList.remove('show'); info.classList.remove('hide');
    let hero=s.birds.find(b=>b.scientific===heroKey)||s.birds[0];
    if(hero.scientific!==heroKey){ heroKey=hero.scientific; heroImg=hero.image_url||null;
      crossfade(heroImg); common.textContent=hero.common; sci.textContent=hero.scientific; }
    else if(hero.image_url && hero.image_url!==heroImg){ heroImg=hero.image_url;
      setLayer(layers[active],heroImg); }
    meta.textContent='last heard at '+(hero.time||'').toLowerCase();
    const others=s.birds.filter(b=>b.scientific!==hero.scientific).map(b=>b.common);
    also.textContent=others.length?'also now · '+others.slice(0,3).join(' · '):'';
  } else {
    info.classList.add('hide'); photos.classList.add('resting'); idle.classList.add('show'); heroKey=null;
    qlabel.textContent='a quiet '+partOfDay();
    qstat.textContent=s.species_today?(s.species_today+(s.species_today===1?' visitor':' visitors')+' today')
      :'no visitors yet today';
    qlist.textContent=s.today.slice(0,6).map(t=>t.common).join('   ·   ');
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
