#!/usr/bin/env python3
"""Birdsong TFT companion.

Drives an Adafruit Mini PiTFT 1.3" (240x240, ST7789) on the Pi's GPIO header.

  - Button A (GPIO 23): short press = toggle mode (desk / on-the-go);
                        long press (~1.5s) = safe shutdown.
  - Button B (GPIO 24): short press = cycle views within the mode.

Reads live data from the local birdsong web app (app.py). Run alongside it.

Pi 5 notes: needs `dtparam=spi=on` (real /dev/spidev0.0) and uses hardware
chip-select (cs=None) since the kernel owns CE0.
"""
import io
import socket
import subprocess
import time

import board
import digitalio
import requests
from adafruit_rgb_display import st7789
from PIL import Image, ImageDraw, ImageFont

API = "http://localhost:8000"
W = H = 240
BG = (11, 15, 20)
FG = (244, 239, 232)
ACCENT = (159, 227, 197)
MUTED = (140, 150, 163)
FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


def font(size, bold=True):
    path = FONT_PATHS[0 if bold else 1]
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()


# ---- display ----
dc = digitalio.DigitalInOut(board.D25)
bl = digitalio.DigitalInOut(board.D22)
# rotation=180 + y_offset=80 is the correct window for the Mini PiTFT 1.3"
# (the 240x240 panel sits 80 rows into the ST7789's 240x320 memory).
# Needs spidev.bufsiz >= 115200 in cmdline.txt so a full frame is one transfer.
disp = st7789.ST7789(board.SPI(), width=W, height=H, rotation=180,
                     x_offset=0, y_offset=80, baudrate=24000000, cs=None, dc=dc)
bl.switch_to_output()
bl.value = True

# ---- buttons (active low, internal pull-ups) ----
btnA = digitalio.DigitalInOut(board.D23)
btnA.switch_to_input(pull=digitalio.Pull.UP)
btnB = digitalio.DigitalInOut(board.D24)
btnB.switch_to_input(pull=digitalio.Pull.UP)

img_cache = {}  # photo url -> cropped PIL image


# -------------------------------------------------------------------- helpers
def online():
    try:
        socket.setdefaulttimeout(2)
        s = socket.socket()
        s.connect(("1.1.1.1", 53))
        s.close()
        return True
    except Exception:
        return False


def get_json(path):
    try:
        return requests.get(API + path, timeout=2).json()
    except Exception:
        return None


def crop_cover(im, w, h):
    sw, sh = im.size
    scale = max(w / sw, h / sh)
    im = im.resize((max(1, int(sw * scale)), max(1, int(sh * scale))))
    nw, nh = im.size
    x, y = (nw - w) // 2, (nh - h) // 2
    return im.crop((x, y, x + w, y + h))


def load_photo(url):
    if not url:
        return None
    if url in img_cache:
        return img_cache[url]
    try:
        r = requests.get(url, timeout=5)
        im = Image.open(io.BytesIO(r.content)).convert("RGB")
        img_cache[url] = crop_cover(im, W, H)
    except Exception:
        img_cache[url] = None
    return img_cache[url]


def fit_font(d, txt, max_w, max_size, min_size=16, bold=True):
    s = max_size
    while s > min_size:
        f = font(s, bold)
        if d.textlength(txt, font=f) <= max_w:
            return f
        s -= 2
    return font(min_size, bold)


def ctext(d, y, txt, f, fill):
    w = d.textlength(txt, font=f)
    d.text(((W - w) / 2, y), txt, font=f, fill=fill)


def footer(d, label, n_views, view):
    d.text((8, H - 18), label, font=font(13, False), fill=MUTED)
    for i in range(n_views):
        x = W - 14 - (n_views - 1 - i) * 14
        on = i == view
        d.ellipse((x, H - 13, x + 7, H - 6),
                  fill=ACCENT if on else (50, 58, 68))


def make_gradient(top, bot):
    """Vertical gradient as a WxH image (built once, cheap to .copy())."""
    strip = Image.new("RGB", (1, H))
    for y in range(H):
        t = y / (H - 1)
        strip.putpixel((0, y), tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3)))
    return strip.resize((W, H))


# desk mode gets a soft teal gradient so it reads as "desk" at a glance;
# on-the-go stays flat dark (utilitarian).
DESK_BG = make_gradient((18, 52, 47), (8, 11, 16))


# --------------------------------------------------------------------- views
def render_desk(d, img, view, state, today, beat):
    if view == 0:  # current bird (with photo) or idle
        birds = (state or {}).get("birds") or []
        if (state or {}).get("mode") == "bird" and birds:
            b = birds[0]
            photo = load_photo(b.get("image_url"))
            if photo:
                img.paste(photo, (0, 0))
                ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
                od = ImageDraw.Draw(ov)
                od.rectangle((0, 150, W, H), fill=(8, 10, 14, 200))
                img.paste(Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB"), (0, 0))
                d = ImageDraw.Draw(img)
            nf = fit_font(d, b["common"], W - 16, 30)
            d.text((8, 160), b["common"], font=nf, fill=FG)
            d.text((8, 196), b["scientific"], font=font(15, False), fill=ACCENT)
            d.text((8, 216), "last heard " + b.get("time", "").lower(),
                   font=font(13, False), fill=MUTED)
        else:
            ctext(d, 86, "Listening", font(30), ACCENT)
            n = (today or {}).get("species_count", 0)
            ctext(d, 128, f"{n} species today" if n else "no birds yet",
                  font(18, False), MUTED)
    elif view == 1:  # today's tally
        d.text((8, 8), "Today", font=font(20), fill=ACCENT)
        sp = (today or {}).get("species", [])[:5]
        y = 44
        if not sp:
            d.text((8, y), "nothing yet", font=font(16, False), fill=MUTED)
        for s in sp:
            d.text((8, y), str(s["count"]), font=font(18), fill=ACCENT)
            nf = fit_font(d, s["common"], W - 52, 18, 12)
            d.text((44, y), s["common"], font=nf, fill=FG)
            y += 34
    else:  # clock
        ctext(d, 84, (state or {}).get("clock", "--:--"), font(40), FG)
        ctext(d, 140, "listening", font(16, False), MUTED)
    footer(d, "desk", 3, view)


def render_go(d, img, view, state, today, beat):
    # heartbeat dot top-right
    if beat:
        d.ellipse((W - 18, 8, W - 10, 16), fill=ACCENT)
    if view == 0:  # status
        d.text((8, 8), "ON THE GO", font=font(13, False), fill=MUTED)
        birds = (state or {}).get("birds") or []
        if (state or {}).get("mode") == "bird" and birds:
            b = birds[0]
            nf = fit_font(d, b["common"], W - 16, 30)
            d.text((8, 96), b["common"], font=nf, fill=FG)
            d.text((8, 134), "heard " + b.get("time", "").lower(),
                   font=font(15, False), fill=MUTED)
        else:
            ctext(d, 98, "Listening", font(30), ACCENT)
    elif view == 1:  # stats
        det = (today or {}).get("detections", 0)
        spc = (today or {}).get("species_count", 0)
        ctext(d, 40, str(det), font(48), FG)
        ctext(d, 96, "detections today", font(15, False), MUTED)
        ctext(d, 132, str(spc), font(36), ACCENT)
        ctext(d, 180, "species", font(15, False), MUTED)
    else:  # location (live GPS from /state)
        d.text((8, 8), "LOCATION", font=font(13, False), fill=MUTED)
        gps = (state or {}).get("gps") or {}
        if gps.get("fix") and gps.get("lat") is not None:
            ctext(d, 60, f"{gps['lat']:.5f}", font(24), FG)
            ctext(d, 92, f"{gps['lon']:.5f}", font(24), FG)
            sats = gps.get("sats") or 0
            spd_ms = gps.get("speed")
            kmh = (spd_ms * 3.6) if spd_ms else 0
            ctext(d, 140, f"{kmh:0.1f} km/h", font(18), ACCENT)
            ctext(d, 172, f"{sats} sats", font(13, False), MUTED)
        elif gps.get("ts"):
            ctext(d, 100, "searching", font(26), MUTED)
            sats = gps.get("sats") or 0
            ctext(d, 140, f"{sats} sats visible", font(13, False), MUTED)
        else:
            ctext(d, 100, "no GPS", font(28), MUTED)
            ctext(d, 140, "(plug in a USB GPS)", font(13, False), MUTED)
    footer(d, "on-the-go", 3, view)


# ---------------------------------------------------------------------- main
def safe_shutdown(d_img):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    ctext(d, 100, "shutting down", font(22), ACCENT)
    disp.image(img)
    subprocess.run(["sudo", "-n", "poweroff"], check=False)


def clear_screen():
    try:
        disp.image(Image.new("RGB", (W, H), BG))
        bl.value = False
    except Exception:
        pass


def main():
    import signal
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(SystemExit()))
    mode = "desk" if online() else "go"
    view = 0
    beat = False
    state = today = None
    last_fetch = 0
    a_down = None
    a_prev = b_prev = True  # released
    need_render = True
    try:
      while True:
        now = time.time()
        # --- buttons (active low: value False == pressed) ---
        a, b = btnA.value, btnB.value
        if a_prev and not a:          # A pressed
            a_down = now
        if (not a_prev) and a:        # A released
            held = now - (a_down or now)
            if held >= 1.5:
                safe_shutdown(None)
            else:
                mode = "go" if mode == "desk" else "desk"
                view = 0
            a_down = None
            need_render = True
        if b_prev and not b:          # B pressed -> cycle view
            view = (view + 1) % 3
            need_render = True
        a_prev, b_prev = a, b

        # --- data refresh (~1.5s) + heartbeat ---
        if now - last_fetch > 1.5:
            state = get_json("/state")
            today = get_json("/today")
            beat = not beat
            last_fetch = now
            need_render = True

        if not need_render:
            time.sleep(0.04)
            continue
        need_render = False
        # --- render ---
        img = DESK_BG.copy() if mode == "desk" else Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(img)
        if mode == "desk":
            render_desk(d, img, view, state, today, beat)
        else:
            render_go(d, img, view, state, today, beat)
        disp.image(img)
        time.sleep(0.05)
    finally:
        clear_screen()


if __name__ == "__main__":
    main()
