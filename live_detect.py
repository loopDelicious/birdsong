#!/usr/bin/env python3
"""Live bird-song detection PoC.

Continuously records short windows from the USB mic and prints BirdNET
detections above a confidence threshold. Runs on the Raspberry Pi.

    python live_detect.py --min-conf 0.5
    python live_detect.py --lat 37.77 --lon -122.42   # location-filtered

Ctrl-C to stop.
"""
import argparse
import contextlib
import datetime
import io
import os
import subprocess
import sys
import tempfile
import warnings

warnings.filterwarnings("ignore")


def record_chunk(path, seconds, device, rate):
    """Capture `seconds` of mono audio to `path` via ALSA arecord."""
    subprocess.run(
        ["arecord", "-D", device, "-f", "S16_LE", "-r", str(rate),
         "-c", "1", "-d", str(seconds), path],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def main():
    ap = argparse.ArgumentParser(description="Live BirdNET listener")
    ap.add_argument("--device", default="plughw:2,0", help="ALSA capture device")
    ap.add_argument("--seconds", type=int, default=3, help="window length (BirdNET uses 3s)")
    ap.add_argument("--rate", type=int, default=48000)
    ap.add_argument("--min-conf", type=float, default=0.5)
    ap.add_argument("--lat", type=float, default=None, help="latitude for species filtering")
    ap.add_argument("--lon", type=float, default=None, help="longitude for species filtering")
    args = ap.parse_args()

    # birdnetlib is chatty on import/load/analyze; mute its stdout.
    with contextlib.redirect_stdout(io.StringIO()):
        from birdnetlib import Recording
        from birdnetlib.analyzer import Analyzer
        analyzer = Analyzer()

    loc = ""
    if args.lat is not None and args.lon is not None:
        loc = f", location ({args.lat},{args.lon})"
    print(f"🐦 Listening on {args.device} | {args.seconds}s windows | "
          f"min_conf={args.min_conf}{loc}", flush=True)
    print("Ctrl-C to stop.\n", flush=True)

    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name
    try:
        while True:
            record_chunk(tmp, args.seconds, args.device, args.rate)
            kwargs = {"min_conf": args.min_conf}
            if args.lat is not None and args.lon is not None:
                kwargs.update(lat=args.lat, lon=args.lon, date=datetime.datetime.now())
            with contextlib.redirect_stdout(io.StringIO()):
                rec = Recording(analyzer, tmp, **kwargs)
                rec.analyze()
            now = datetime.datetime.now().strftime("%H:%M:%S")
            if rec.detections:
                for d in rec.detections:
                    print(f"[{now}]  {d['common_name']:28s} "
                          f"({d['scientific_name']})  conf={d['confidence']:.2f}",
                          flush=True)
            else:
                print(f"[{now}]  …", end="\r", flush=True)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        os.unlink(tmp)


if __name__ == "__main__":
    main()
