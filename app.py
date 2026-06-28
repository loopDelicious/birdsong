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
import math
import os
import sqlite3
import subprocess
import tempfile
import threading
import time
import warnings

import numpy as np
import requests
from flask import Flask, jsonify, render_template_string, request

warnings.filterwarnings("ignore")

# ---------------------------------------------------------------- shared state
STATE_LOCK = threading.Lock()
ACTIVE = {}             # scientific_name -> live entry (with last_ts)
TODAY = {}              # scientific_name -> cumulative {common, count, last, image_url}
TODAY_DATE = datetime.date.today()  # calendar day TODAY belongs to
IMG_CACHE = {}          # scientific_name -> image url (or None)
HOLD_SECONDS = 90       # how long a bird stays on screen after last heard
MAX_ON_SCREEN = 6       # cap simultaneous birds shown

CONFIG = {
    "device": "plughw:2,0", "seconds": 3, "rate": 48000,
    "min_conf": 0.5, "lat": 37.77, "lon": -122.42, "use_location": False,
}


def roll_today():
    """Reset the daily tally when the calendar day changes. Caller holds STATE_LOCK."""
    global TODAY_DATE
    today = datetime.date.today()
    if today != TODAY_DATE:
        TODAY.clear()
        TODAY_DATE = today


# ----------------------------------------------------------------- detection log
# Persistent, keep-forever log of every detection. Text only (no audio), so it
# stays tiny — roughly 50-100 MB per year even in a busy yard.
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "birdsong.db")
DB_LOCK = threading.Lock()


def db():
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def db_init():
    with db() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("""CREATE TABLE IF NOT EXISTS detections(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL, date TEXT NOT NULL,
            scientific TEXT NOT NULL, common TEXT NOT NULL,
            confidence REAL NOT NULL)""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_det_date ON detections(date)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_det_sci ON detections(scientific)")
        # GPS columns added later; old rows keep NULLs and stay valid.
        existing = {r["name"] for r in c.execute("PRAGMA table_info(detections)")}
        for col in ("lat", "lon", "accuracy", "speed"):
            if col not in existing:
                c.execute(f"ALTER TABLE detections ADD COLUMN {col} REAL")
        # Continuous track log for on-the-go rides.
        c.execute("""CREATE TABLE IF NOT EXISTS gps_tracks(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL, date TEXT NOT NULL,
            lat REAL NOT NULL, lon REAL NOT NULL,
            speed REAL, heading REAL, accuracy REAL)""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_track_date ON gps_tracks(date)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_track_ts ON gps_tracks(ts)")


def log_detection(now, common, scientific, confidence,
                  lat=None, lon=None, accuracy=None, speed=None):
    try:
        with DB_LOCK, db() as c:
            c.execute(
                "INSERT INTO detections(ts,date,scientific,common,confidence,"
                "lat,lon,accuracy,speed) VALUES(?,?,?,?,?,?,?,?,?)",
                (now.isoformat(timespec="seconds"), now.date().isoformat(),
                 scientific, common, round(float(confidence), 2),
                 None if lat is None else round(float(lat), 6),
                 None if lon is None else round(float(lon), 6),
                 None if accuracy is None else round(float(accuracy), 2),
                 None if speed is None else round(float(speed), 2)))
    except Exception as e:
        print("db log error:", e, flush=True)


def load_today():
    """Seed the in-memory daily tally from today's logged rows, so the live
    display survives a service restart mid-day."""
    try:
        with db() as c:
            rows = c.execute(
                "SELECT common, scientific, COUNT(*) n, MAX(ts) last_ts"
                " FROM detections WHERE date=? GROUP BY scientific",
                (datetime.date.today().isoformat(),)).fetchall()
        with STATE_LOCK:
            for r in rows:
                TODAY[r["scientific"]] = {
                    "common": r["common"], "scientific": r["scientific"],
                    "count": r["n"], "last": fmt_time(r["last_ts"])}
    except Exception as e:
        print("db load error:", e, flush=True)


def fmt_time(iso):
    try:
        return datetime.datetime.fromisoformat(iso).strftime("%-I:%M %p")
    except Exception:
        return iso


# ----------------------------------------------------------------------- GPS
# Live GPS state from the USB stick. `fix` is True only when we currently have
# a position; if the stick is unplugged or the sky view is bad, lat/lon stay
# stale but we flip fix=False so callers know not to trust them.
GPS_LOCK = threading.Lock()
GPS_STATE = {
    "fix": False, "lat": None, "lon": None, "speed": None, "heading": None,
    "sats": 0, "hdop": None, "ts": None, "device": None,
}
GPS_CONFIG = {"device": "/dev/ttyACM0", "baud": 9600, "enabled": True}


def gps_snapshot():
    """Read-only copy of GPS_STATE for callers that need a consistent view."""
    with GPS_LOCK:
        return dict(GPS_STATE)


def _gps_mark_no_fix():
    with GPS_LOCK:
        GPS_STATE["fix"] = False


def _gps_open_serial():
    """Try the configured device, then fall back to the other common path.
    Returns an open serial.Serial or None."""
    import serial
    candidates = [GPS_CONFIG["device"]]
    # u-blox VK-172 -> /dev/ttyACM0; Prolific BU-353 -> /dev/ttyUSB0. Cover both.
    for alt in ("/dev/ttyACM0", "/dev/ttyUSB0"):
        if alt not in candidates:
            candidates.append(alt)
    for path in candidates:
        if not os.path.exists(path):
            continue
        try:
            s = serial.Serial(path, GPS_CONFIG["baud"], timeout=2)
            with GPS_LOCK:
                GPS_STATE["device"] = path
            return s
        except Exception as e:
            print(f"gps: open {path} failed: {e}", flush=True)
    return None


def gps_reader_loop():
    """Read NMEA from the USB GPS and keep GPS_STATE current. Auto-reconnects."""
    try:
        import pynmea2
        import serial  # noqa: F401  (imported here so the loop fails fast)
    except ImportError as e:
        print(f"gps: pyserial/pynmea2 not installed ({e}); GPS disabled", flush=True)
        return
    print("gps: reader starting", flush=True)
    last_line = 0.0
    while True:
        ser = _gps_open_serial()
        if ser is None:
            _gps_mark_no_fix()
            time.sleep(5)
            continue
        print(f"gps: reading from {GPS_STATE['device']}", flush=True)
        try:
            while True:
                try:
                    raw = ser.readline()
                except Exception as e:
                    print(f"gps: read error: {e}", flush=True)
                    break
                if not raw:
                    # readline timed out: if no sentences for ~10s, drop fix
                    if time.time() - last_line > 10:
                        _gps_mark_no_fix()
                    continue
                last_line = time.time()
                try:
                    line = raw.decode("ascii", errors="ignore").strip()
                    if not line.startswith("$"):
                        continue
                    msg = pynmea2.parse(line)
                except Exception:
                    continue
                _gps_apply(msg)
        finally:
            with contextlib.suppress(Exception):
                ser.close()
        _gps_mark_no_fix()
        time.sleep(2)


def _gps_apply(msg):
    """Fold one parsed NMEA sentence into GPS_STATE."""
    now_iso = datetime.datetime.now().isoformat(timespec="seconds")
    try:
        # RMC = position + speed + course; the recommended minimum fix
        if msg.sentence_type == "RMC":
            valid = getattr(msg, "status", "") == "A"
            lat = getattr(msg, "latitude", None)
            lon = getattr(msg, "longitude", None)
            if valid and lat is not None and lon is not None and (lat or lon):
                speed_knots = getattr(msg, "spd_over_grnd", None)
                speed_ms = float(speed_knots) * 0.514444 if speed_knots else None
                heading = getattr(msg, "true_course", None)
                with GPS_LOCK:
                    GPS_STATE["fix"] = True
                    GPS_STATE["lat"] = round(float(lat), 6)
                    GPS_STATE["lon"] = round(float(lon), 6)
                    GPS_STATE["speed"] = round(speed_ms, 2) if speed_ms is not None else None
                    GPS_STATE["heading"] = float(heading) if heading not in (None, "") else None
                    GPS_STATE["ts"] = now_iso
            elif not valid:
                with GPS_LOCK:
                    GPS_STATE["fix"] = False
                    GPS_STATE["ts"] = now_iso
        # GGA = fix quality + sat count + HDOP
        elif msg.sentence_type == "GGA":
            sats = getattr(msg, "num_sats", None)
            hdop = getattr(msg, "horizontal_dil", None)
            quality = int(getattr(msg, "gps_qual", 0) or 0)
            with GPS_LOCK:
                if sats not in (None, ""):
                    with contextlib.suppress(ValueError):
                        GPS_STATE["sats"] = int(sats)
                if hdop not in (None, ""):
                    with contextlib.suppress(ValueError):
                        GPS_STATE["hdop"] = float(hdop)
                if quality == 0:
                    GPS_STATE["fix"] = False
                GPS_STATE["ts"] = now_iso
    except Exception as e:
        print(f"gps: apply error: {e}", flush=True)


def _haversine_m(lat1, lon1, lat2, lon2):
    """Great-circle distance in meters between two lat/lon points."""
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


# How far apart consecutive logged track points must be (meters), and the
# longest we'll go without logging when a fix is present (seconds). Together
# these keep a stationary Pi near zero rows while a bike ride produces a clean
# point every few seconds.
TRACK_MIN_DIST_M = 8.0
TRACK_MAX_GAP_S = 30.0


def log_track_point(lat, lon, speed, heading, hdop):
    """Append one point to gps_tracks."""
    try:
        now = datetime.datetime.now()
        with DB_LOCK, db() as c:
            c.execute(
                "INSERT INTO gps_tracks(ts,date,lat,lon,speed,heading,accuracy)"
                " VALUES(?,?,?,?,?,?,?)",
                (now.isoformat(timespec="seconds"), now.date().isoformat(),
                 float(lat), float(lon),
                 float(speed) if speed is not None else None,
                 float(heading) if heading is not None else None,
                 float(hdop) if hdop is not None else None))
    except Exception as e:
        print("track log error:", e, flush=True)


def track_writer_loop():
    """Sub-sample GPS_STATE into the gps_tracks table while we have a fix."""
    last_lat = last_lon = None
    last_ts = 0.0
    print("gps: track writer starting", flush=True)
    while True:
        time.sleep(2)
        snap = gps_snapshot()
        if not snap["fix"] or snap["lat"] is None or snap["lon"] is None:
            continue
        now = time.time()
        moved_enough = False
        if last_lat is None:
            moved_enough = True  # first point after fix
        else:
            d = _haversine_m(last_lat, last_lon, snap["lat"], snap["lon"])
            if d >= TRACK_MIN_DIST_M:
                moved_enough = True
        if moved_enough or (now - last_ts) >= TRACK_MAX_GAP_S:
            log_track_point(snap["lat"], snap["lon"], snap["speed"],
                            snap["heading"], snap["hdop"])
            last_lat, last_lon = snap["lat"], snap["lon"]
            last_ts = now


# ----------------------------------------------------------- spectrogram cache
import struct as _struct
import zlib as _zlib

# Spectrogram palette: silence=dark bg → teal → warm peak.
# Higher contrast so frequency structure reads clearly in the small box.
_CMAP = np.array([
    [ 11,  15,  20],   # silence – bg black
    [ 14,  52,  62],   # low     – deep teal-navy
    [ 38, 140, 130],   # mid     – teal
    [159, 227, 197],   # high    – bright teal accent
    [231, 181, 154],   # peak    – warm peach accent
], dtype=np.float32)


def _apply_cmap(v):
    """Map 0-1 float HxW array → HxWx3 uint8 using the app palette."""
    n = len(_CMAP) - 1
    idx = np.clip(v * n, 0, n - 1e-9)
    lo = idx.astype(int)
    frac = (idx - lo)[..., np.newaxis]
    return (_CMAP[lo] + frac * (_CMAP[np.minimum(lo + 1, n)] - _CMAP[lo])).astype(np.uint8)


def _encode_png(rgb):
    """Encode HxWx3 uint8 array as PNG bytes using stdlib only (no Pillow)."""
    h, w = rgb.shape[:2]
    def chunk(tag, data):
        p = tag + data
        return (_struct.pack('>I', len(data)) + p
                + _struct.pack('>I', _zlib.crc32(p) & 0xffffffff))
    rows = b''.join(b'\x00' + rgb[r].tobytes() for r in range(h))
    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', _struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', _zlib.compress(rows, 6))
            + chunk(b'IEND', b''))


SPEC_CACHE = {}        # scientific -> PNG bytes
SPEC_LOCK = threading.Lock()
_SPEC_BUSY = set()
_SPEC_BUSY_LOCK = threading.Lock()


def ensure_spectrogram(scientific, y, sr):
    """Generate a mel spectrogram PNG in a background thread; cache indefinitely."""
    with SPEC_LOCK:
        if scientific in SPEC_CACHE:
            return
    with _SPEC_BUSY_LOCK:
        if scientific in _SPEC_BUSY:
            return
        _SPEC_BUSY.add(scientific)

    def work():
        try:
            import librosa
            mel = librosa.feature.melspectrogram(
                y=y, sr=sr, n_mels=80, fmin=500, fmax=12000, hop_length=256)
            db = librosa.power_to_db(mel, ref=np.max)
            norm = (db - db.min()) / max(float(db.max() - db.min()), 1e-6)
            norm = np.flipud(norm)   # low freq at bottom
            rgb = _apply_cmap(norm)
            png = _encode_png(rgb)
            with SPEC_LOCK:
                SPEC_CACHE[scientific] = png
        except Exception as e:
            print(f"spectrogram error: {e}", flush=True)
        finally:
            with _SPEC_BUSY_LOCK:
                _SPEC_BUSY.discard(scientific)

    threading.Thread(target=work, daemon=True).start()


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
                    # Read audio once for spectrogram generation — fast (<50ms
                    # for a 3-second chunk) and done before the next record.
                    _y_spec = _sr_spec = None
                    try:
                        import soundfile as _sf
                        _y_spec, _sr_spec = _sf.read(tmp, dtype='float32')
                        if _y_spec.ndim > 1:
                            _y_spec = _y_spec.mean(axis=1)
                    except Exception:
                        pass
                    now = datetime.datetime.now()
                    ts = time.time()
                    timestr = now.strftime("%-I:%M %p")
                    # Snapshot GPS once so every detection in this chunk gets
                    # the same coords; cheap (no I/O, just a dict copy).
                    gps = gps_snapshot()
                    has_fix = gps.get("fix") and gps.get("lat") is not None
                    with STATE_LOCK:
                        roll_today()
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
                    # photo / spectrogram / logging happen OUTSIDE the state lock
                    for d in rec.detections:
                        ensure_image(d["common_name"], d["scientific_name"])
                        if _y_spec is not None:
                            ensure_spectrogram(d["scientific_name"], _y_spec, _sr_spec)
                        log_detection(
                            now, d["common_name"], d["scientific_name"],
                            d["confidence"],
                            lat=gps.get("lat") if has_fix else None,
                            lon=gps.get("lon") if has_fix else None,
                            accuracy=gps.get("hdop") if has_fix else None,
                            speed=gps.get("speed") if has_fix else None,
                        )
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
        roll_today()
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
            "gps": gps_snapshot(),
        })


@app.route("/gps")
def gps_endpoint():
    return jsonify(gps_snapshot())


@app.route("/spectrogram")
def spectrogram_endpoint():
    sci = request.args.get("sci", "")
    with SPEC_LOCK:
        png = SPEC_CACHE.get(sci)
    if not png:
        return "", 404
    return png, 200, {"Content-Type": "image/png", "Cache-Control": "no-store"}


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


def _day_species(conn, date):
    rows = conn.execute(
        "SELECT common, scientific, COUNT(*) count, MIN(ts) first_ts,"
        " MAX(ts) last_ts, ROUND(MAX(confidence),2) max_conf"
        " FROM detections WHERE date=? GROUP BY scientific ORDER BY count DESC",
        (date,)).fetchall()
    return [{"common": r["common"], "scientific": r["scientific"],
             "count": r["count"], "first": fmt_time(r["first_ts"]),
             "last": fmt_time(r["last_ts"]), "max_confidence": r["max_conf"]}
            for r in rows]


@app.route("/today")
def today_log():
    d = datetime.date.today().isoformat()
    with db() as c:
        species = _day_species(c, d)
        total = c.execute("SELECT COUNT(*) n FROM detections WHERE date=?",
                          (d,)).fetchone()["n"]
    return jsonify({"date": d, "species_count": len(species),
                    "detections": total, "species": species})


@app.route("/history")
def history_log():
    date = request.args.get("date")
    if date:  # drill into a single day
        with db() as c:
            species = _day_species(c, date)
        return jsonify({"date": date, "species_count": len(species),
                        "species": species})
    try:
        days = max(1, min(365, int(request.args.get("days", 14))))
    except ValueError:
        days = 14
    since = (datetime.date.today() - datetime.timedelta(days=days - 1)).isoformat()
    with db() as c:
        daily = c.execute(
            "SELECT date, COUNT(*) detections, COUNT(DISTINCT scientific) species"
            " FROM detections WHERE date>=? GROUP BY date ORDER BY date DESC",
            (since,)).fetchall()
        top = c.execute(
            "SELECT common, scientific, COUNT(*) count FROM detections"
            " WHERE date>=? GROUP BY scientific ORDER BY count DESC LIMIT 10",
            (since,)).fetchall()
        tot = c.execute(
            "SELECT COUNT(*) detections, COUNT(DISTINCT scientific) species"
            " FROM detections WHERE date>=?", (since,)).fetchone()
    return jsonify({
        "days": days, "from": since, "to": datetime.date.today().isoformat(),
        "totals": {"detections": tot["detections"], "species": tot["species"]},
        "daily": [{"date": r["date"], "detections": r["detections"],
                   "species_count": r["species"]} for r in daily],
        "top_species": [{"common": r["common"], "scientific": r["scientific"],
                         "count": r["count"]} for r in top]})


def _window(default_days=14):
    """Resolve from/to query params into ISO dates. Both optional."""
    today = datetime.date.today()
    to_s = request.args.get("to")
    from_s = request.args.get("from")
    try:
        to_d = datetime.date.fromisoformat(to_s) if to_s else today
    except ValueError:
        to_d = today
    try:
        from_d = (datetime.date.fromisoformat(from_s) if from_s
                  else to_d - datetime.timedelta(days=default_days - 1))
    except ValueError:
        from_d = to_d - datetime.timedelta(days=default_days - 1)
    return from_d.isoformat(), to_d.isoformat()


@app.route("/detections")
def detections_log():
    """Historical detections with GPS coords. Filters: from, to, species, limit.
    Only rows with non-NULL lat/lon are returned (so the map page can plot
    them directly). Pass ?all=1 to include un-geocoded rows."""
    from_d, to_d = _window(default_days=14)
    species = request.args.get("species")
    include_all = request.args.get("all") in ("1", "true", "yes")
    try:
        limit = max(1, min(20000, int(request.args.get("limit", 5000))))
    except ValueError:
        limit = 5000
    where = ["date>=?", "date<=?"]
    params = [from_d, to_d]
    if not include_all:
        where.append("lat IS NOT NULL AND lon IS NOT NULL")
    if species:
        where.append("scientific=?")
        params.append(species)
    sql = ("SELECT ts,scientific,common,confidence,lat,lon,accuracy,speed"
           " FROM detections WHERE " + " AND ".join(where)
           + " ORDER BY ts DESC LIMIT ?")
    params.append(limit)
    with db() as c:
        rows = c.execute(sql, params).fetchall()
    items = [{
        "ts": r["ts"], "common": r["common"], "scientific": r["scientific"],
        "confidence": r["confidence"], "lat": r["lat"], "lon": r["lon"],
        "accuracy": r["accuracy"], "speed": r["speed"],
    } for r in rows]
    return jsonify({"from": from_d, "to": to_d, "count": len(items),
                    "detections": items})


@app.route("/track")
def track_log():
    """GPS track points in time order. Filters: from, to, limit."""
    from_d, to_d = _window(default_days=14)
    try:
        limit = max(1, min(100000, int(request.args.get("limit", 20000))))
    except ValueError:
        limit = 20000
    with db() as c:
        rows = c.execute(
            "SELECT ts,lat,lon,speed FROM gps_tracks"
            " WHERE date>=? AND date<=? ORDER BY ts ASC LIMIT ?",
            (from_d, to_d, limit)).fetchall()
    return jsonify({
        "from": from_d, "to": to_d, "count": len(rows),
        "points": [{"ts": r["ts"], "lat": r["lat"], "lon": r["lon"],
                    "speed": r["speed"]} for r in rows],
    })


def _demo_spectrogram(scientific):
    """Generate a synthetic bird-frequency chirp spectrogram for demo/testing."""
    def work():
        try:
            import librosa
            sr = 48000
            t = np.linspace(0, 3.0, sr * 3, dtype=np.float32)
            # Layered sweeping tones across the bird frequency range (2–8 kHz)
            y = (0.5 * np.sin(2 * np.pi * (3000 + 2000 * np.sin(2 * np.pi * 2 * t)) * t)
                 + 0.3 * np.sin(2 * np.pi * (5000 + 1500 * np.sin(2 * np.pi * 3 * t + 1)) * t)
                 + 0.2 * np.sin(2 * np.pi * (7000 + 800 * t) * t) * np.exp(-t))
            mel = librosa.feature.melspectrogram(
                y=y, sr=sr, n_mels=80, fmin=500, fmax=12000, hop_length=256)
            db = librosa.power_to_db(mel, ref=np.max)
            norm = (db - db.min()) / max(float(db.max() - db.min()), 1e-6)
            rgb = _apply_cmap(np.flipud(norm))
            with SPEC_LOCK:
                SPEC_CACHE[scientific] = _encode_png(rgb)
        except Exception as e:
            print(f"demo spectrogram error: {e}", flush=True)
    threading.Thread(target=work, daemon=True).start()


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
        if sci not in SPEC_CACHE:
            _demo_spectrogram(sci)
    return jsonify({"ok": True, "active": list(ACTIVE)})


@app.route("/")
def index():
    return render_template_string(PAGE)


@app.route("/map")
def map_page():
    return render_template_string(MAP_PAGE,
                                  default_lat=CONFIG["lat"],
                                  default_lon=CONFIG["lon"])


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
    align-items:center; height:2.4vmin; opacity:.42; transition:opacity .8s ease; }
  #wave.hidden { opacity:0; }
  #wave span { width:.4vmin; height:100%; background:rgba(255,255,255,.7); border-radius:2px;
    transform-origin:center; animation:wv 1.7s ease-in-out infinite; }
  @keyframes wv { 0%,100%{transform:scaleY(.22)} 50%{transform:scaleY(1)} }
  #spec-box { position:absolute; right:6vmin; bottom:6vmin; width:200px; height:104px;
    border-radius:10px; overflow:hidden; opacity:0; transition:opacity 1s ease;
    pointer-events:none; box-shadow:0 2px 16px rgba(0,0,0,.5);
    background:var(--bg); padding:10px; }
  #spec-box.show { opacity:0.78; }
  #spec-box img { width:100%; height:100%; object-fit:fill; display:block; border-radius:4px; }
  #fab { position:fixed; bottom:3.5vmin; right:3.5vmin; z-index:10; width:6.5vmin; height:6.5vmin;
    min-width:46px; min-height:46px; border-radius:50%; background:var(--panel);
    border:1px solid var(--line); color:var(--fg); cursor:pointer; opacity:0; pointer-events:none;
    transition:opacity .35s; display:flex; align-items:center; justify-content:center; }
  body.ui #fab { opacity:.5; pointer-events:auto; }
  #fab:hover { opacity:1; }
  #maplink { position:fixed; bottom:3.5vmin; left:3.5vmin; z-index:10; color:var(--fg);
    text-decoration:none; font-size:14px; letter-spacing:.18em; text-transform:uppercase;
    padding:10px 14px; border:1px solid var(--line); border-radius:999px;
    background:rgba(18,25,34,.6); backdrop-filter:blur(6px); opacity:0; pointer-events:none;
    transition:opacity .35s; }
  body.ui #maplink { opacity:.55; pointer-events:auto; }
  #maplink:hover { opacity:1; }
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
  <div id="spec-box"><img id="spec-img" src="" alt=""></div>
</div>
<a id="maplink" href="/map" title="Bird map">map</a>
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
  clearBtn=document.getElementById('clearBtn'), wave=document.getElementById('wave'),
  specBox=document.getElementById('spec-box'), specImg=document.getElementById('spec-img');

let specSci=null;
function loadSpec(scientific){
  if(specSci===scientific && specBox.classList.contains('show')) return;
  if(specSci!==scientific){ specSci=scientific; specBox.classList.remove('show'); specImg.src=''; }
  const probe=new Image();
  probe.onload=()=>{
    if(specSci===scientific){
      specImg.src=probe.src; specBox.classList.add('show'); wave.classList.add('hidden');
    }
  };
  probe.src='/spectrogram?sci='+encodeURIComponent(scientific)+'&_t='+Date.now();
}
function clearSpec(){
  specSci=null; specBox.classList.remove('show'); specImg.src=''; wave.classList.remove('hidden');
}

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
    loadSpec(hero.scientific);
  } else {
    info.classList.add('hide'); photos.classList.add('resting'); idle.classList.add('show'); heroKey=null;
    clearSpec();
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


# Historical map: detections (with GPS) as clustered markers, plus the bike-
# ride trail as a polyline. Vanilla Leaflet + markercluster from CDN, no build.
MAP_PAGE = r"""
<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Birdsong · Map</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
  integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=" crossorigin="">
<link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.css">
<link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.Default.css">
<style>
  :root { --bg:#0b0f14; --fg:#f4efe8; --muted:#aeb8c2; --accent:#9fe3c5;
    --panel:#121922; --line:#243040; }
  * { box-sizing:border-box; margin:0; padding:0; }
  html,body { height:100%; background:var(--bg); color:var(--fg);
    font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif; }
  #app { display:flex; height:100%; }
  #side { width:320px; flex:none; background:var(--panel); border-right:1px solid var(--line);
    overflow:auto; padding:18px; }
  #map { flex:1; }
  .leaflet-popup-content-wrapper, .leaflet-popup-tip { background:#121922; color:#f4efe8;
    box-shadow:0 6px 28px rgba(0,0,0,.5); }
  .pop { font-size:14px; line-height:1.4; }
  .pop b { color:var(--accent); }
  .pop em { color:var(--muted); font-style:italic; }
  h1 { font-size:18px; margin-bottom:14px; display:flex; align-items:center;
    justify-content:space-between; gap:8px; }
  h1 a { color:var(--muted); font-size:12px; text-decoration:none; letter-spacing:.18em;
    text-transform:uppercase; }
  h1 a:hover { color:var(--fg); }
  h3 { font-size:11px; text-transform:uppercase; letter-spacing:1px; color:#7f8c99;
    margin:16px 0 8px; }
  .seg { display:flex; gap:6px; margin-bottom:10px; }
  .seg button { flex:1; padding:8px 0; background:#1d2735; border:1px solid var(--line);
    color:var(--fg); font-size:12px; border-radius:8px; cursor:pointer; }
  .seg button.on { background:var(--accent); color:#0b0f14; border-color:var(--accent); }
  .dates { display:flex; gap:6px; }
  .dates input { flex:1; padding:8px; background:#0b0f14; color:var(--fg);
    border:1px solid var(--line); border-radius:8px; font-family:inherit; font-size:13px; }
  .row2 { display:flex; align-items:center; justify-content:space-between; margin:10px 0; }
  .row2 .name { font-size:14px; }
  .toggle { width:38px; height:22px; border-radius:999px; background:#2a3645; position:relative;
    cursor:pointer; transition:.2s; flex:none; }
  .toggle.on { background:var(--accent); }
  .toggle::after { content:""; position:absolute; top:3px; left:3px; width:16px; height:16px;
    border-radius:50%; background:#fff; transition:.2s; }
  .toggle.on::after { left:19px; }
  .stats { font-size:13px; color:var(--muted); margin-bottom:10px; }
  .stats b { color:var(--fg); }
  .specieslist { max-height:50vh; overflow:auto; }
  .sp { display:flex; align-items:center; gap:8px; padding:6px 6px; border-radius:8px;
    cursor:pointer; user-select:none; }
  .sp:hover { background:#1d2735; }
  .sp.off { opacity:.35; }
  .sp .sw { width:12px; height:12px; border-radius:50%; flex:none; }
  .sp .n { flex:1; font-size:13px; }
  .sp .c { font-size:12px; color:var(--muted); font-variant-numeric:tabular-nums; }
  .empty { color:var(--muted); font-size:13px; padding:10px 0; }
  #homepill { position:fixed; bottom:1.6rem; left:1.6rem; z-index:9999;
    color:var(--fg); text-decoration:none; font-size:13px; letter-spacing:.18em;
    text-transform:uppercase; padding:9px 16px; border:1px solid var(--line);
    border-radius:999px; background:rgba(18,25,34,.85); backdrop-filter:blur(6px);
    opacity:.75; transition:opacity .25s; }
  #homepill:hover { opacity:1; }
  @media (max-width: 720px) {
    #app { flex-direction:column; }
    #side { width:100%; max-height:46vh; border-right:none; border-bottom:1px solid var(--line); }
    #homepill { bottom:1rem; left:1rem; }
  }
</style></head><body>
<a id="homepill" href="/" title="Back to live display">live</a>
<div id="app">
  <aside id="side">
    <h1>Bird map <a href="/">live ›</a></h1>
    <h3>Range</h3>
    <div class="seg" id="seg">
      <button data-d="1">today</button>
      <button data-d="7" class="on">7d</button>
      <button data-d="30">30d</button>
      <button data-d="365">year</button>
    </div>
    <div class="dates">
      <input type="date" id="from"><input type="date" id="to">
    </div>
    <div class="row2"><div class="name">Show ride trail</div>
      <div class="toggle on" id="trailToggle"></div></div>
    <div class="stats" id="stats">loading…</div>
    <h3>Species</h3>
    <div class="specieslist" id="specieslist"></div>
  </aside>
  <div id="map"></div>
</div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
  integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" crossorigin=""></script>
<script src="https://unpkg.com/leaflet.markercluster@1.5.3/dist/leaflet.markercluster.js"></script>
<script>
const DEFAULT_LAT = {{ default_lat|tojson }};
const DEFAULT_LON = {{ default_lon|tojson }};
const map = L.map('map', {zoomControl:true}).setView([DEFAULT_LAT, DEFAULT_LON], 13);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  maxZoom: 19, attribution: '© OpenStreetMap'
}).addTo(map);

const cluster = L.markerClusterGroup({chunkedLoading:true, disableClusteringAtZoom:18});
map.addLayer(cluster);
let trailLayer = L.layerGroup().addTo(map);

// 12 well-spaced hues for the top species; everything else uses MUTED.
const PALETTE = ['#e7b59a','#9fe3c5','#9ec1ff','#ffd479','#ff9c9c','#c79cff',
                 '#7ad7d1','#ffc99c','#a9e89a','#f29eff','#ff7a7a','#7affc1'];
const OTHER = '#8b96a3';

const fromEl = document.getElementById('from'), toEl = document.getElementById('to');
const trailToggle = document.getElementById('trailToggle');
const statsEl = document.getElementById('stats');
const speciesEl = document.getElementById('specieslist');
let speciesColor = new Map();  // scientific -> hex
let speciesOff = new Set();    // scientific names hidden by user

function isoDate(d){ return d.toISOString().slice(0,10); }
function setRange(days){
  const to = new Date();
  const from = new Date(); from.setDate(from.getDate() - (days - 1));
  fromEl.value = isoDate(from); toEl.value = isoDate(to);
  refresh();
}
document.querySelectorAll('#seg button').forEach(btn => {
  btn.onclick = () => {
    document.querySelectorAll('#seg button').forEach(b => b.classList.remove('on'));
    btn.classList.add('on');
    setRange(parseInt(btn.dataset.d, 10));
  };
});
fromEl.onchange = toEl.onchange = refresh;
trailToggle.onclick = () => { trailToggle.classList.toggle('on'); drawTrail(lastTrack); };

function mkIcon(color){
  return L.divIcon({
    className:'',
    html:`<div style="width:14px;height:14px;border-radius:50%;background:${color};
      border:2px solid #0b0f14;box-shadow:0 0 0 1px ${color}88;"></div>`,
    iconSize:[14,14], iconAnchor:[7,7],
  });
}

let lastDetections = [], lastTrack = [];

function refresh(){
  const q = `from=${fromEl.value}&to=${toEl.value}`;
  statsEl.textContent = 'loading…';
  Promise.all([
    fetch('/detections?' + q).then(r => r.json()),
    fetch('/track?' + q).then(r => r.json()),
  ]).then(([dets, track]) => {
    lastDetections = dets.detections || [];
    lastTrack = track.points || [];
    rebuildSpecies(lastDetections);
    drawMarkers();
    drawTrail(lastTrack);
    fitBounds();
    statsEl.innerHTML = `<b>${lastDetections.length}</b> detection${lastDetections.length===1?'':'s'} · `
      + `<b>${lastTrack.length}</b> track point${lastTrack.length===1?'':'s'}`;
  }).catch(e => { statsEl.textContent = 'error loading: ' + e; });
}

function rebuildSpecies(dets){
  const counts = new Map();
  for(const d of dets){
    if(!counts.has(d.scientific)) counts.set(d.scientific, {common:d.common, count:0});
    counts.get(d.scientific).count++;
  }
  const sorted = [...counts.entries()].sort((a,b) => b[1].count - a[1].count);
  speciesColor = new Map();
  sorted.forEach(([sci, _], i) => {
    speciesColor.set(sci, i < PALETTE.length ? PALETTE[i] : OTHER);
  });
  speciesEl.innerHTML = '';
  if(!sorted.length){ speciesEl.innerHTML = '<div class="empty">no detections with GPS yet</div>'; return; }
  for(const [sci, info] of sorted){
    const row = document.createElement('div');
    row.className = 'sp' + (speciesOff.has(sci) ? ' off' : '');
    const c = speciesColor.get(sci);
    row.innerHTML = `<span class="sw" style="background:${c}"></span>`
      + `<span class="n">${info.common}</span><span class="c">${info.count}</span>`;
    row.onclick = () => {
      if(speciesOff.has(sci)) speciesOff.delete(sci); else speciesOff.add(sci);
      row.classList.toggle('off');
      drawMarkers();
    };
    speciesEl.appendChild(row);
  }
}

function drawMarkers(){
  cluster.clearLayers();
  const fmt = ts => { try{ return new Date(ts).toLocaleString(); }catch(e){ return ts; } };
  for(const d of lastDetections){
    if(speciesOff.has(d.scientific)) continue;
    if(d.lat == null || d.lon == null) continue;
    const c = speciesColor.get(d.scientific) || OTHER;
    const m = L.marker([d.lat, d.lon], {icon: mkIcon(c)});
    m.bindPopup(`<div class="pop"><b>${d.common}</b><br>`
      + `<em>${d.scientific}</em><br>${fmt(d.ts)}<br>`
      + `confidence ${d.confidence}</div>`);
    cluster.addLayer(m);
  }
}

function drawTrail(points){
  trailLayer.clearLayers();
  if(!trailToggle.classList.contains('on') || points.length < 2) return;
  // Split the polyline whenever consecutive points are >5 minutes apart, so
  // separate rides don't get joined with a giant straight line.
  let seg = [], gap = 5 * 60 * 1000;
  let lastT = null;
  for(const p of points){
    const t = new Date(p.ts).getTime();
    if(lastT !== null && t - lastT > gap && seg.length){
      L.polyline(seg, {color:'#9fe3c5', weight:3, opacity:.65}).addTo(trailLayer);
      seg = [];
    }
    seg.push([p.lat, p.lon]);
    lastT = t;
  }
  if(seg.length >= 2) L.polyline(seg, {color:'#9fe3c5', weight:3, opacity:.65}).addTo(trailLayer);
}

function fitBounds(){
  const pts = [];
  for(const d of lastDetections) if(d.lat != null) pts.push([d.lat, d.lon]);
  for(const p of lastTrack) pts.push([p.lat, p.lon]);
  if(pts.length) map.fitBounds(L.latLngBounds(pts).pad(0.15));
}

setRange(7);
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
    ap.add_argument("--gps-device", default="/dev/ttyACM0",
                    help="USB GPS serial device (falls back to /dev/ttyUSB0)")
    ap.add_argument("--gps-baud", type=int, default=9600)
    ap.add_argument("--no-gps", action="store_true",
                    help="disable the GPS reader entirely")
    args = ap.parse_args()
    CONFIG.update(device=args.device, seconds=args.seconds, rate=args.rate,
                  min_conf=args.min_conf, lat=args.lat, lon=args.lon,
                  use_location=args.location)
    GPS_CONFIG.update(device=args.gps_device, baud=args.gps_baud,
                      enabled=not args.no_gps)

    db_init()
    load_today()
    threading.Thread(target=detection_loop, daemon=True).start()
    if GPS_CONFIG["enabled"]:
        threading.Thread(target=gps_reader_loop, daemon=True).start()
        threading.Thread(target=track_writer_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=args.port, threaded=True)


if __name__ == "__main__":
    main()
