#!/usr/bin/env python3
"""
HD Radio streaming with automatic analog FM fallback.

Waits up to PROBE_SECS for nrsc5 to produce audio (= HD lock confirmed).
  Lock:    bridges nrsc5 → ffmpeg → Icecast indefinitely.
  No lock: writes hd_unavailable to status file, exec's analog FM pipeline.
"""
import json, os, select, subprocess, sys, time
from pathlib import Path

GAIN         = os.environ.get("GAIN", "30")
FREQ         = os.environ.get("FREQ", "100.7M")
BITRATE      = os.environ.get("BITRATE", "128k")
ICECAST_PASS = os.environ.get("ICECAST_PASS", "changeme")
MOUNT        = os.environ.get("MOUNT", "fm.mp3")
SUBCHANNEL   = os.environ.get("SUBCHANNEL", "0")

FREQ_MHZ    = FREQ.rstrip("M")
ICECAST_URL = f"icecast://source:{ICECAST_PASS}@localhost:8000/{MOUNT}"
HD_STATUS   = Path("/run/sdr-streams/hd_status.json")
PROBE_SECS  = 15   # seconds to wait for first audio byte before giving up

HD_STATUS.unlink(missing_ok=True)
HD_STATUS.write_text(json.dumps({"hd_probing": True, "freq": FREQ_MHZ}))

nrsc5 = subprocess.Popen(
    ["nrsc5", "-d", "0", "-g", GAIN, "-o", "-", FREQ_MHZ, SUBCHANNEL],
    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
)

ready, _, _ = select.select([nrsc5.stdout], [], [], PROBE_SECS)

if not ready:
    nrsc5.terminate()
    nrsc5.wait()
    HD_STATUS.write_text(json.dumps({
        "hd_unavailable": True, "freq": FREQ_MHZ, "ts": int(time.time()),
    }))
    # Replace this process with analog FM + RDS pipeline
    os.execvp("bash", ["bash", "-c",
        f"rtl_fm -M fm -l 0 -A std -s 171000 -g {GAIN} -f {FREQ} -F 9 - | "
        f"tee >(redsea -r 171000 --output json 2>/dev/null | "
        f"FREQ='{FREQ}' /opt/sdr-tuner/rds_watcher.py) | "
        f"ffmpeg -hide_banner -loglevel warning -f s16le -ar 171000 -ac 1 -i - "
        f"-af 'aemphasis=mode=reproduction:type=75fm,lowpass=15000' "
        f"-ar 48000 -ac 1 "
        f"-c:a libmp3lame -b:a {BITRATE} -content_type audio/mpeg "
        f"-f mp3 '{ICECAST_URL}'"
    ])

# HD locked — update status and bridge nrsc5 → ffmpeg
HD_STATUS.write_text(json.dumps({
    "hd_locked": True, "freq": FREQ_MHZ, "subchannel": int(SUBCHANNEL),
}))

ffmpeg = subprocess.Popen(
    ["ffmpeg", "-hide_banner", "-loglevel", "warning",
     "-f", "wav", "-i", "pipe:0",
     "-c:a", "libmp3lame", "-b:a", BITRATE,
     "-content_type", "audio/mpeg", "-f", "mp3", ICECAST_URL],
    stdin=subprocess.PIPE, bufsize=0,
)

try:
    while True:
        chunk = nrsc5.stdout.read(65536)
        if not chunk:
            break
        ffmpeg.stdin.write(chunk)
except (BrokenPipeError, OSError):
    pass
finally:
    try: nrsc5.terminate()
    except OSError: pass
    try: ffmpeg.stdin.close()
    except OSError: pass
    nrsc5.wait()
    ffmpeg.wait()
