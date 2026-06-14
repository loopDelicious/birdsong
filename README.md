# 🐦 Birdsong

A Raspberry Pi listens to a USB microphone, identifies bird songs in real time
with [BirdNET](https://github.com/kahst/BirdNET-Analyzer), and shows the bird —
photo, name, and confidence — on a full-screen display (e.g. a projector).

- **`app.py`** — the real thing: runs continuous detection in a background
  thread *and* serves a live full-screen web page with a multi-bird grid and a
  runtime control panel.
- **`live_detect.py`** — a minimal terminal-only version that just prints
  detections. Handy for quick checks.

## Recently identified

A few of the birds the display has actually picked up in San Francisco (location
filter on):

| <img src="https://upload.wikimedia.org/wikipedia/commons/thumb/a/a1/Nycticorax_nycticorax_457953189.jpg/330px-Nycticorax_nycticorax_457953189.jpg" width="240" alt="Black-crowned Night-Heron"> | <img src="https://upload.wikimedia.org/wikipedia/commons/thumb/f/f1/House_finch_%2833688%292.jpg/330px-House_finch_%2833688%292.jpg" width="240" alt="House Finch"> | <img src="https://upload.wikimedia.org/wikipedia/commons/thumb/9/97/American_robin_%2871307%29.jpg/330px-American_robin_%2871307%29.jpg" width="240" alt="American Robin"> |
|:--:|:--:|:--:|
| **Black-crowned Night-Heron**<br>*Nycticorax nycticorax* · heard ×4 | **House Finch**<br>*Haemorhous mexicanus* · heard ×2 | **American Robin**<br>*Turdus migratorius* · heard ×1 |

<sub>Bird photos via [Wikimedia Commons](https://commons.wikimedia.org).</sub>

## Hardware

- Raspberry Pi 5 (a Pi 4 also works; no AI accelerator needed — BirdNET runs on
  the CPU in ~0.2 s per 3 s window)
- USB microphone (any class-compliant USB audio input; the Pi has no analog mic in)
- A display via HDMI (Pi 5 uses **micro-HDMI** → you need a micro-HDMI→HDMI cable)

## Software setup (on the Pi)

Debian 13 (trixie) ships Python 3.13, but the BirdNET stack needs **Python 3.11**
(no good `tflite-runtime` aarch64 wheel for 3.13). We use [`uv`](https://github.com/astral-sh/uv)
to provide 3.11 and create the venv.

```bash
# 1. install uv (user-local, no system changes)
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"

# 2. project + Python 3.11 venv
mkdir -p ~/birdsong && cd ~/birdsong
# (copy app.py, live_detect.py, requirements.txt here)
uv venv --python 3.11

# 3. install deps (numpy<2 is critical — see requirements.txt)
uv pip install -r requirements.txt
```

> **Why `numpy<2`?** `tflite-runtime` 2.14.0 is compiled against NumPy 1.x and
> crashes under NumPy 2 (`_ARRAY_API not found`). The pin in `requirements.txt`
> keeps the whole stack consistent.

## Find your microphone

```bash
arecord -l        # note the card number, e.g. "card 2"
```

`app.py` defaults to ALSA device `plughw:2,0`. Override with `--device` if yours
differs.

## Run

```bash
# quick terminal test
.venv/bin/python live_detect.py --min-conf 0.5

# the full display app
.venv/bin/python app.py --min-conf 0.5            # testing: no location filter
.venv/bin/python app.py --min-conf 0.5 --location # real install: filter to your area
```

Then open **http://<pi-hostname>.local:8000** — in a browser on your network to
preview, or in Chromium kiosk mode on the Pi for the projector.

Set your location in `app.py` (`--lat` / `--lon`, defaults to San Francisco) so
the filter knows which species are plausible.

## Run as a service (survives crashes + reboot)

```bash
mkdir -p ~/.config/systemd/user
cp birdsong.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now birdsong.service
sudo loginctl enable-linger "$USER"   # start on boot without an active login
```

Check it: `systemctl --user status birdsong.service` and `curl localhost:8000/state`.

## Kiosk on a projector / TV (HDMI)

On Raspberry Pi OS (Wayland/labwc desktop with autologin), boot straight into a
full-screen browser showing the display:

```bash
cp kiosk/labwc-autostart ~/.config/labwc/autostart
sudo reboot
```

On boot the Pi autologins, waits for the web server, and launches Chromium in
kiosk mode (auto-restarted via `lwrespawn`). The connected display (e.g. an
Anker Nebula) must be **powered on and set to its HDMI input** when the Pi boots
so it gets detected. Remove `~/.config/labwc/autostart` to restore the normal
desktop.

> Pi 5 has **micro-HDMI** ports; the one nearest the USB-C jack is `HDMI0`.

## The display

- **Multi-bird grid** — when several species are heard within the hold window,
  the screen splits responsively (2 = side by side, up to 6 in a grid).
- **Ambient mode** — fades to a calm "Listening" screen with today's tally when
  it's quiet.
- **Control panel (⚙, bottom-right)**:
  - **Local filter** — only show species plausible at your location/season.
  - **Min confidence** — raise to cut false positives.
  - **Clear** — reset the screen to Listening.

### Confidence & the location filter

- **Higher confidence** = fewer false positives. `0.5` is a good default.
- **Local filter ON** = only birds that actually occur at your location are shown
  — the best false-positive killer for a real installation. (It will also reject
  recordings of out-of-area birds you play for testing, which is expected.)
- For the wall display, run with `--location` and leave it on.

## Notes & gotchas

- Run only one instance — two processes contend for the single USB mic and
  detection stalls. Use the systemd service; don't launch by hand alongside it.
- Don't `kill -9` the recording process — it can lock the USB capture device.
  Use `systemctl stop` / `pkill -TERM`.

## License

MIT (see `LICENSE`), or choose your own.
