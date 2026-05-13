#!/usr/bin/env python3
"""SDR Tuner Flask UI."""
import json
import os
import subprocess
import time
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, url_for

import station_db
import ui_settings

# ---------------------------------------------------------------------------
# Paths & config
# ---------------------------------------------------------------------------
STATIONS_FM_PATH = Path("/var/lib/sdr-streams/stations.json")
STATIONS_AM_PATH = Path("/var/lib/sdr-streams/stations_am.json")
ENV_PATH         = Path("/etc/sdr-streams/active.env")
NOW_PLAYING_PATH = Path("/run/sdr-streams/now_playing.json")
CAPTIONS_PATH    = Path("/run/sdr-streams/captions.json")
HD_STATUS_PATH   = Path("/run/sdr-streams/hd_status.json")

SERVICE          = "sdr-fm@active"
SCAN_FM_SERVICE  = "sdr-scan.service"
SCAN_AM_SERVICE  = "sdr-am-scan.service"
ICECAST_PASS     = os.environ.get("ICECAST_PASS", "changeme")

app = Flask(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _load_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def load_stations(path: Path):
    data = _load_json(path)
    if not data:
        return {"stations": [], "scanned_at": None}
    return data


def annotate_fm(stations):
    for s in stations:
        info = station_db.lookup_fm(s.get("freq_mhz"))
        if info:
            s["call"]  = info.get("call")
            s["city"]  = info.get("city")
            s["state"] = info.get("state")
            s["label"] = station_db.label(info)
        hd = station_db.hd_subchannels(s.get("freq_mhz"))
        if hd:
            s["hd_programs"] = hd
        if s.get("ps") and not s.get("call"):
            s["call"] = s["ps"]
    return stations


def annotate_am(stations):
    for s in stations:
        info = station_db.lookup_am(s.get("freq_khz"))
        if info:
            s["call"]  = info.get("call")
            s["city"]  = info.get("city")
            s["state"] = info.get("state")
            s["label"] = station_db.label(info)
    return stations


def write_env(freq: str, band: str = "fm", hd: bool = False, subchannel: int = 0):
    if band == "am":
        mode, samp, extra, freq_val = "am", "12000", "-E direct2", f"{freq}k"
    elif hd:
        mode, samp, extra, freq_val = "hd", "200000", "", f"{freq}M"
    else:
        mode, samp, extra, freq_val = "wbfm", "200000", "", f"{freq}M"

    ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    content = (
        f"MODE={mode}\n"
        f"FREQ={freq_val}\n"
        f"SAMP={samp}\n"
        f"GAIN=30\n"
        f"BITRATE=128k\n"
        f"MOUNT=fm.mp3\n"
        f'EXTRA_FLAGS="{extra}"\n'
        f"ICECAST_PASS={ICECAST_PASS}\n"
    )
    if hd:
        content += f"SUBCHANNEL={subchannel}\n"
    ENV_PATH.write_text(content)


def clear_runtime_state():
    for p in (NOW_PLAYING_PATH, CAPTIONS_PATH, HD_STATUS_PATH):
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            app.logger.warning("could not remove %s: %s", p, e)


def sysctl(action: str, unit: str = SERVICE):
    return subprocess.run(
        ["sudo", "systemctl", action, unit],
        capture_output=True, text=True,
    )


def is_active(unit: str) -> str:
    return subprocess.run(
        ["systemctl", "is-active", unit],
        capture_output=True, text=True,
    ).stdout.strip()


def current_tune():
    """Return (freq, band, is_hd, subchannel) from active.env."""
    if not ENV_PATH.exists():
        return None, None, False, 0
    freq = mode = None
    subchannel = 0
    for line in ENV_PATH.read_text().splitlines():
        if line.startswith("FREQ="):
            freq = line.split("=", 1)[1].strip().strip('"')
        elif line.startswith("MODE="):
            mode = line.split("=", 1)[1].strip().strip('"')
        elif line.startswith("SUBCHANNEL="):
            try:
                subchannel = int(line.split("=", 1)[1].strip())
            except ValueError:
                subchannel = 0
    is_hd = mode == "hd"
    band = "am" if mode == "am" else "fm"
    if freq:
        if freq.endswith("M"):   freq = freq[:-1]
        elif freq.endswith("k"): freq = freq[:-1]
    return freq, band, is_hd, subchannel


# ---------------------------------------------------------------------------
# UI routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    fm = load_stations(STATIONS_FM_PATH)
    am = load_stations(STATIONS_AM_PATH)
    freq, band, is_hd, subchannel = current_tune()

    current_label = None
    if freq and band == "fm":
        current_label = station_db.label(station_db.lookup_fm(freq))
    elif freq and band == "am":
        current_label = station_db.label(station_db.lookup_am(freq))

    settings = ui_settings.load()
    return render_template(
        "index.html",
        fm_stations=annotate_fm(fm["stations"]),
        am_stations=annotate_am(am["stations"]),
        fm_scanned_at=fm.get("scanned_at"),
        am_scanned_at=am.get("scanned_at"),
        current=freq,
        current_band=band,
        current_hd=is_hd,
        current_subchannel=subchannel,
        current_label=current_label,
        status=is_active(SERVICE),
        settings=settings,
        resolved_stream_url=ui_settings.stream_url_for(request.host),
    )


@app.route("/radio")
def radio():
    """Stereo-style listening UI."""
    settings = ui_settings.load()
    return render_template(
        "radio.html",
        stream_url=ui_settings.stream_url_for(request.host),
        site_title=settings["site_title"],
    )


@app.route("/tune", methods=["POST"])
def tune():
    freq = request.form["freq"]
    band = request.form.get("band", "fm")
    hd   = request.form.get("hd", "").lower() in ("1", "true", "yes")
    try:
        subchannel = int(request.form.get("subchannel", "0") or "0")
    except ValueError:
        subchannel = 0
    try:
        write_env(freq, band, hd, subchannel)
    except OSError as e:
        app.logger.error("write_env failed: %s", e)
        return f"Could not save tune setting: {e}", 500
    clear_runtime_state()
    sysctl("restart")
    return redirect(url_for("index"))


@app.route("/stop", methods=["POST"])
def stop():
    sysctl("stop")
    clear_runtime_state()
    return redirect(url_for("index"))


@app.route("/scan-fm", methods=["POST"])
def scan_fm():
    sysctl("start", SCAN_FM_SERVICE)
    return redirect(url_for("index"))


@app.route("/scan-am", methods=["POST"])
def scan_am():
    sysctl("start", SCAN_AM_SERVICE)
    return redirect(url_for("index"))


@app.route("/reload-stations", methods=["POST"])
def reload_stations():
    station_db.reload()
    return redirect(url_for("index"))


@app.route("/settings", methods=["POST"])
def settings_save():
    """Update UI settings (stream URL, site title, etc.) from the admin form."""
    try:
        ui_settings.save({
            "stream_url":   request.form.get("stream_url", ""),
            "stream_label": request.form.get("stream_label", ""),
            "site_title":   request.form.get("site_title", ""),
        })
    except OSError as e:
        app.logger.error("settings save failed: %s", e)
        return f"Could not save settings: {e}", 500
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------
@app.route("/api/status")
def api_status():
    freq, band, is_hd, subchannel = current_tune()
    return jsonify({
        "current_freq":      freq,
        "current_band":      band,
        "current_hd":        is_hd,
        "current_subchannel": subchannel,
        "status":            is_active(SERVICE),
    })


@app.route("/api/scan_status")
def api_scan_status():
    fm_state = is_active(SCAN_FM_SERVICE)
    am_state = is_active(SCAN_AM_SERVICE)
    fm_running = fm_state in ("active", "activating")
    am_running = am_state in ("active", "activating")
    return jsonify({
        "running":  fm_running or am_running,
        "band":     "fm" if fm_running else ("am" if am_running else None),
        "fm_state": fm_state,
        "am_state": am_state,
    })


@app.route("/api/now_playing")
def api_now_playing():
    rds      = _load_json(NOW_PLAYING_PATH) or {}
    cap      = _load_json(CAPTIONS_PATH)    or {}
    hd_state = _load_json(HD_STATUS_PATH)   or {}

    caption_updated = cap.get("caption_updated", 0) or 0
    age_s = round(time.time() - caption_updated, 1) if caption_updated else None

    freq, band, is_hd, subchannel = current_tune()
    station_info = None
    if freq and band == "fm":
        station_info = station_db.lookup_fm(freq)
    elif freq and band == "am":
        station_info = station_db.lookup_am(freq)

    return jsonify({
        "available":      bool(rds) or bool(cap) or bool(station_info),
        "rds":            rds,
        "fcc":            station_info,
        "freq":           freq,
        "band":           band,
        "hd":             is_hd,
        "subchannel":     subchannel,
        "hd_probing":     hd_state.get("hd_probing", False),
        "hd_locked":      hd_state.get("hd_locked", False),
        "hd_unavailable": hd_state.get("hd_unavailable", False),
        "mode":      cap.get("mode", "idle"),
        "caption": {
            "text":    cap.get("caption_text", ""),
            "updated": caption_updated,
            "age_s":   age_s,
        },
        "lyrics": {
            "song":  cap.get("song"),
            "lines": cap.get("lyrics_lines", []),
            "index": cap.get("lyrics_index", -1),
        },
    })


@app.route("/api/stations")
def api_stations():
    fm = load_stations(STATIONS_FM_PATH)
    am = load_stations(STATIONS_AM_PATH)
    return jsonify({
        "fm": annotate_fm(fm["stations"]),
        "am": annotate_am(am["stations"]),
        "fm_scanned_at": fm.get("scanned_at"),
        "am_scanned_at": am.get("scanned_at"),
    })


@app.route("/api/tune", methods=["POST"])
def api_tune():
    payload = request.get_json(silent=True) or {}
    freq = payload.get("freq")
    band = payload.get("band", "fm")
    hd   = bool(payload.get("hd", False))
    try:
        subchannel = int(payload.get("subchannel", 0) or 0)
    except (TypeError, ValueError):
        subchannel = 0
    if not freq:
        return jsonify({"ok": False, "error": "missing freq"}), 400
    try:
        write_env(str(freq), band, hd, subchannel)
    except OSError as e:
        app.logger.error("api_tune write_env failed: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500
    clear_runtime_state()
    sysctl("restart")
    return jsonify({"ok": True, "freq": freq, "band": band, "hd": hd, "subchannel": subchannel})


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
