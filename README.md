# 🐦 Birdsong

A Raspberry Pi listens to a USB microphone, identifies bird songs in real time
with [BirdNET](https://github.com/kahst/BirdNET-Analyzer), and shows the bird —
photo, name, and confidence — on a full-screen display (e.g. a projector).

- **`app.py`** — the real thing: runs continuous detection in a background
  thread *and* serves a live full-screen web page with a multi-bird grid and a
  runtime control panel.
- **`live_detect.py`** — a minimal terminal-only version that just prints
  detections. Handy for quick checks.

## What it looks like

The display is designed as calm, ambient technology. A single bird emerges from a
soft colour haze derived from its own photo, with its name lower-left and the time
it was last heard — here, an American Robin heard at dawn:

![Birdsong calm display showing an American Robin](docs/display.png)

When no birds are calling it settles into a quiet "listening" state with the day's
tally, so the wall always glows gently rather than going blank:

![Birdsong quiet listening state](docs/display-quiet.png)

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

## Detection log & HTTP API

Every detection is logged to a local SQLite database (`birdsong.db`, next to
`app.py`). It's text-only (no audio), so it stays tiny — roughly 50–100 MB per
year — and is kept indefinitely. The app exposes read-only JSON endpoints for
stats; query them from any browser or script on your network.

### `GET /today`

Today's species with counts, first/last heard, and peak confidence.

```bash
curl http://birdpi.local:8000/today
```
```json
{
  "date": "2026-06-17",
  "species_count": 3,
  "detections": 6,
  "species": [
    {"common": "American Robin", "scientific": "Turdus migratorius",
     "count": 3, "first": "6:31 AM", "last": "8:14 AM", "max_confidence": 0.85}
  ]
}
```

### `GET /history`

Rolling-window summary. Optional `days` (default 14, max 365):

```bash
curl "http://birdpi.local:8000/history?days=7"
```
```json
{
  "days": 7, "from": "2026-06-11", "to": "2026-06-17",
  "totals": {"detections": 8, "species": 4},
  "daily": [{"date": "2026-06-17", "detections": 6, "species_count": 3}],
  "top_species": [{"common": "American Robin",
                   "scientific": "Turdus migratorius", "count": 4}]
}
```

Pass `date=YYYY-MM-DD` instead to drill into one day's species breakdown
(same shape as `/today`):

```bash
curl "http://birdpi.local:8000/history?date=2026-06-16"
```

### `GET /state`

The live display feed (current bird(s), today's tally, clock, config) — polled
by the kiosk page every 1.5 s. Returned fields: `mode` (`bird`/`idle`), `birds`,
`today`, `species_today`, `clock`, `config`.

> Write endpoints also exist for the control panel — `POST /control`
> (`min_conf`, `use_location`), `POST /clear`, and `POST /demo` (inject species
> for screenshots). These change live state but are not part of the read API.

## GPS (on-the-go)

Plug a USB GPS stick (e.g. u-blox VK-172) into the Pi and every detection gets
tagged with `lat`/`lon`. A second background thread also logs a continuous
trail of points to `gps_tracks` while the stick has a fix — distance-thresholded
(~8 m or 30 s) so a stationary Pi at home produces near-zero rows, but a bike
ride produces one point every few seconds. Open **http://birdpi.local:8000/map**
to see it: clustered markers per species over OpenStreetMap, your ride drawn
as a polyline, with date-range and per-species filters.

### Hardware setup

- u-blox-based sticks (VK-172, VK-162) show up at `/dev/ttyACM0`.
- Prolific PL2303-based sticks (e.g. BU-353S4) show up at `/dev/ttyUSB0`.
- Either way, no extra driver is needed on Raspberry Pi OS — they're class-
  compliant USB serial. Confirm with `dmesg | tail` after plugging in, and
  `cat /dev/ttyACM0` should print `$GPxxx`/`$GNxxx` NMEA lines (Ctrl-C out).

### Software setup

```bash
uv pip install -r requirements.txt   # already pulls in pyserial + pynmea2
```

The user running `app.py` must be able to read the serial device. On Pi OS
that's the `dialout` group:

```bash
sudo usermod -a -G dialout "$USER"   # log out + back in to pick up
```

### Runtime flags

```
--gps-device /dev/ttyACM0   # override the serial path
--gps-baud   9600           # most NMEA sticks default to 9600
--no-gps                    # skip the GPS reader entirely
```

If the configured device is missing, `app.py` falls back to the other common
path before giving up — so you can leave the default alone on most sticks.

### Endpoints

- `GET /gps` — current fix: `{fix, lat, lon, speed (m/s), heading, sats, hdop, ts, device}`.
- `GET /detections?from=YYYY-MM-DD&to=YYYY-MM-DD&species=<sci>` — historical
  detections with GPS coords (only rows with lat/lon by default; pass `all=1`
  to include un-geocoded ones). Default window: last 14 days.
- `GET /track?from=...&to=...` — the GPS trail (points in time order).
- `GET /state` now also includes a `gps` field so the kiosk/TFT can show live
  fix status.
- `GET /map` — the Leaflet page itself.

### Storage & privacy

GPS coords live in the same local `birdsong.db` as the detection log; nothing
leaves the Pi. The track table adds roughly 0.1–0.2 MB per hour of riding —
still microscopic next to the bird log. If you share screenshots or the DB
file, your home location is in it. Delete the `gps_tracks` table or set
`lat/lon = NULL` in `detections` to scrub.

## TFT companion screen (optional)

`tft.py` drives an **Adafruit Mini PiTFT 1.3"** (240×240, ST7789) on the GPIO
header — a small status display. Two modes: **desk** (teal gradient background,
shows the current bird with photo / today's tally / clock) and **on-the-go**
(flat dark, shows last bird + a listening heartbeat / detection stats / live
GPS coords from the USB stick — see [GPS](#gps-on-the-go) below). Mode
auto-selects by network at boot (online → desk).

- **Button A** (GPIO 23): short press = toggle desk ⇄ on-the-go; long press
  (~1.5 s) = safe shutdown (needs passwordless `poweroff` in sudoers to work).
- **Button B** (GPIO 24): short press = cycle views.

### Setup (Raspberry Pi 5)

```bash
uv pip install -r requirements-tft.txt
```

Enable SPI on the header and make the buffer big enough for a full frame:

```bash
# 1. uncomment dtparam=spi=on  (gives a real /dev/spidev0.0 on the header)
sudo sed -i 's/^#dtparam=spi=on/dtparam=spi=on/' /boot/firmware/config.txt
# 2. let one 115,200-byte frame transfer in one go (else you get banding)
grep -q spidev.bufsiz /boot/firmware/cmdline.txt || \
  sudo sed -i 's/$/ spidev.bufsiz=131072/' /boot/firmware/cmdline.txt
sudo reboot
```

Then install the service:

```bash
cp birdsong-tft.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now birdsong-tft.service
```

### Pi 5 display gotchas (hard-won)

- **`dtparam=spi=on` is required** — without it GPIO 9/10/11 aren't muxed to SPI
  and the screen stays dark (the code "runs" against an internal bus that isn't
  wired to the header).
- **`spidev.bufsiz` must be ≥ 115200** (a full 240×240×2 frame), or chunked
  transfers corrupt at fixed boundaries → banding in the same rows.
- **`cs=None`** — with SPI enabled the kernel owns CE0, so let hardware drive
  chip-select; claiming it in software gives "GPIO busy".
- **`rotation=180, y_offset=80`** — the 240×240 panel sits 80 rows into the
  ST7789's 240×320 memory; without the offset the top band shows uninitialized
  noise.
- The board uses **24 pins** (seat it on pins 1–24 at the power/SD-card corner).
- The screen and the official **active cooler** compete for space — use a 2×20
  GPIO stacking header to keep both, or a low-profile heatsink.

## Notes & gotchas

- Run only one instance — two processes contend for the single USB mic and
  detection stalls. Use the systemd service; don't launch by hand alongside it.
- Don't `kill -9` the recording process — it can lock the USB capture device.
  Use `systemctl stop` / `pkill -TERM`.

## License

MIT (see `LICENSE`), or choose your own.
