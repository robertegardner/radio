#!/usr/bin/env python3
"""SDR Tuner Flask UI."""
import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from flask import (Flask, Response, jsonify, redirect, render_template, request,
                   send_from_directory, url_for)

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
RFI_STATUS_PATH  = Path("/run/sdr-streams/rfi_status.json")

# Weather-satellite (wxsat) state. Persistent products + captures index live in
# WXSAT_DIR; the upcoming-pass list is regenerated into tmpfs.
WXSAT_DIR          = Path("/var/lib/sdr-streams/wxsat")
WXSAT_CAPTURES_PATH = WXSAT_DIR / "captures.json"
WXSAT_PASSES_PATH   = Path("/run/sdr-streams/wxsat_passes.json")
WXSAT_STATUS_PATH   = Path("/run/sdr-streams/wxsat_status.json")
WXSAT_AUTH_PATH     = Path("/run/sdr-streams/wxsat_authorized.json")
WXSAT_LIVE_PATH     = Path("/run/sdr-streams/wxsat_live.json")
# wxsat is radio-domain but inherently Pi-side: the scheduler needs the SDR, so
# the capture products + captures.json only ever exist on the Pi. The rack tuner
# (.84, the public radio.rg2.io backend) has no captures of its own, so set
# WXSAT_UPSTREAM to the Pi tuner (e.g. http://192.168.6.18:8080) and every
# /api/wxsat/* call is proxied there — display, image bytes, and the mutate
# routes (delete/rebuild, which MUST run on the Pi where the IQ + satdump live).
# Unset on the Pi itself, where these routes serve local files directly.
WXSAT_UPSTREAM      = os.environ.get("WXSAT_UPSTREAM", "").rstrip("/")

SERVICE          = "sdr-fm@active"
SCAN_FM_SERVICE  = "sdr-scan.service"
SCAN_AM_SERVICE  = "sdr-am-scan.service"
ICECAST_PASS     = os.environ.get("ICECAST_PASS", "changeme")

# FM-multistation (mux) mode. Opt-in; legacy mono (SERVICE) stays the boot
# default. The mux owns mux_status.json; Flask writes channels.json and signals.
MUX_ENV_PATH       = Path("/etc/sdr-streams/mux.env")
CHANNELS_PATH      = Path("/etc/sdr-streams/channels.json")
MUX_STATUS_PATH    = Path("/run/sdr-streams/mux_status.json")
IQ_STATUS_PATH     = Path("/run/sdr-streams/iq_capture.json")
PILOT_STATUS_PATH  = Path("/run/sdr-streams/pilot.json")
PILOT_MAX_AGE_S    = 5.0   # stereo_decode writes ~2x/sec; older than this = mono
MUX_SERVICE        = "sdr-mux"
IQ_CAPTURE_SERVICE = "sdr-iq-capture"
# Clean ceiling on this Pi 5 alongside the scanner (3 channels = 0 drops; the 4th
# glitches). Keep in sync with mux_supervisor.MAX_CHANNELS.
MAX_MUX_CHANNELS   = 3

# MP3 stream bitrates the user may pick from the radio UI. The value lands
# in active.env (BITRATE=) and is passed straight to ffmpeg, so it must be
# validated against this allowlist — never trust the raw request value.
ALLOWED_BITRATES = ["64k", "96k", "128k", "192k", "256k"]
DEFAULT_BITRATE  = "128k"


def current_bitrate() -> str:
    """Persisted stream bitrate, clamped to the allowlist."""
    br = ui_settings.load().get("bitrate", DEFAULT_BITRATE)
    return br if br in ALLOWED_BITRATES else DEFAULT_BITRATE


def current_stereo() -> bool:
    """Persisted FM stereo on/off (False = mono). Lands in active.env STEREO=,
    which stream.sh reads to pick the stereo_decode path vs a clean mono encode."""
    return bool(ui_settings.load().get("stereo", True))


def pilot_state() -> dict:
    """True 19 kHz pilot detection (not the selected mode). stereo_decode.py
    publishes pilot.json ~2x/sec while it runs; report locked only when that file
    is FRESH (it isn't written in mono mode / when the decoder is down) AND says
    stereo. Returns {locked, rms, blend}."""
    out = {"locked": False, "rms": None, "blend": None}
    try:
        if time.time() - PILOT_STATUS_PATH.stat().st_mtime > PILOT_MAX_AGE_S:
            return out
    except OSError:
        return out
    d = _load_json(PILOT_STATUS_PATH) or {}
    out["rms"]    = d.get("pilot_rms")
    out["blend"]  = d.get("blend")
    out["locked"] = bool(d.get("stereo"))
    return out


# FM antenna port on the dx-R2 — all three selectable so you can A/B/C them:
#   A = Shakespeare 5120 + FM bandpass (the normal FM antenna)
#   B = 137 MHz dipole + Sawbird LNA (broadband enough to try; LNA may help weak)
#   C = long-wire (the AM antenna; usually poor for FM, but offered for comparison)
ALLOWED_ANTENNAS = ["Antenna A", "Antenna B", "Antenna C"]
DEFAULT_ANTENNA  = "Antenna A"


def current_antenna() -> str:
    """Persisted FM antenna, clamped to the allowlist. Lands in active.env ANTENNA="""
    a = ui_settings.load().get("antenna", DEFAULT_ANTENNA)
    return a if a in ALLOWED_ANTENNAS else DEFAULT_ANTENNA

app = Flask(__name__)


@app.before_request
def _wxsat_upstream_proxy():
    """On the rack tuner (.84), proxy every /api/wxsat/* call to the Pi tuner.

    The wxsat captures live only on the Pi (see WXSAT_UPSTREAM). The /wxsat page
    HTML is served locally from the identical template; only its data + product
    images are remote. Returns a Response to short-circuit the local route, or
    None to let local handling proceed (Pi, or any non-wxsat path)."""
    if not WXSAT_UPSTREAM or not request.path.startswith("/api/wxsat/"):
        return None
    try:
        upstream = requests.request(
            method=request.method,
            url=WXSAT_UPSTREAM + request.full_path.rstrip("?"),
            data=request.get_data(),
            headers={k: v for k, v in request.headers if k.lower() != "host"},
            timeout=30,
            stream=True,
        )
    except requests.RequestException as e:
        return jsonify({"ok": False, "error": f"wxsat upstream unreachable: {e}"}), 502
    # Drop hop-by-hop / length headers — Flask re-derives them for the new body.
    drop = {"content-encoding", "content-length", "transfer-encoding", "connection"}
    headers = [(k, v) for k, v in upstream.raw.headers.items() if k.lower() not in drop]
    return Response(upstream.iter_content(chunk_size=65536),
                    status=upstream.status_code, headers=headers)


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
        mode, samp, freq_val = "am", "2000000", f"{freq}k"
        # am_stream.py disables the hardware AGC and uses this as a fixed manual
        # gain. 20 was empirically the sweet spot: KMOX 1120 (50 kW) peaks at
        # ~0.28 (28% ADC full scale) — plenty of headroom, no compression. At
        # GAIN=30 with AGC off the dx-R2 starts to overload on the strongest
        # local AM carriers.
        gain = 20
    elif hd:
        mode, samp, freq_val = "hd", "200000", f"{freq}M"
        gain = 30
    else:
        mode, samp, freq_val = "wbfm", "200000", f"{freq}M"
        gain = 30

    ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    content = (
        f"MODE={mode}\n"
        f"FREQ={freq_val}\n"
        f"SAMP={samp}\n"
        f"GAIN={gain}\n"
        f"BITRATE={current_bitrate()}\n"
        f"STEREO={'1' if current_stereo() else '0'}\n"
        f"MOUNT=fm.mp3\n"
        f"ICECAST_PASS={ICECAST_PASS}\n"
    )
    if hd:
        content += f"SUBCHANNEL={subchannel}\n"
    if mode == "wbfm":   # FM antenna A/B/C select (am_stream/hd own their antenna).
        # QUOTE it — the value has a space ("Antenna A") and stream.sh sources this
        # file; unquoted, `source` runs "A" as a command (exit 127 → restart loop).
        content += f'ANTENNA="{current_antenna()}"\n'
    ENV_PATH.write_text(content)


def clear_runtime_state():
    for p in (NOW_PLAYING_PATH, CAPTIONS_PATH, HD_STATUS_PATH, RFI_STATUS_PATH):
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


@app.route("/wxsat")
def wxsat():
    """Weather-satellite (Meteor-M LRPT) capture gallery + schedule."""
    settings = ui_settings.load()
    return render_template(
        "wxsat.html",
        site_title=settings["site_title"],
    )


@app.route("/multi")
def multi():
    """FM-multistation control + multi-mount listening UI."""
    settings = ui_settings.load()
    return render_template(
        "multi.html",
        stream_base=ui_settings.stream_url_for(request.host).rsplit("/", 1)[0],
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
        "bitrate":           current_bitrate(),
    })


@app.route("/api/art")
def api_art():
    """Same-origin proxy for album-art images. The radio page loads cover art
    through here instead of hitting Apple's mzstatic CDN directly, which can be
    blocked/slow/TLS-inspected on remote or corporate networks even when the
    site itself loads. Host-allowlisted to mzstatic to avoid an open proxy."""
    url = request.args.get("u", "")
    host = urlparse(url).netloc.lower().split(":")[0]
    if urlparse(url).scheme != "https" or not (
            host == "mzstatic.com" or host.endswith(".mzstatic.com")):
        return "forbidden", 403
    try:
        r = requests.get(url, timeout=8, headers={"User-Agent": "sdr-tuner/1.0"})
    except requests.RequestException as e:
        app.logger.warning("art proxy fetch failed: %s", e)
        return "upstream error", 502
    if not r.ok:
        return "upstream error", 502
    resp = Response(r.content, mimetype=r.headers.get("Content-Type", "image/jpeg"))
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


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


@app.route("/api/rfi_status")
def api_rfi_status():
    # Written once per am_stream.py startup. Absent for FM/HD tunes or when
    # the stream isn't running. UI banner only surfaces RFI when this file
    # exists AND rfi_candidates is non-empty.
    data = _load_json(RFI_STATUS_PATH)
    if not data:
        return jsonify({"available": False})
    data["available"] = True
    return jsonify(data)


@app.route("/api/now_playing")
def api_now_playing():
    # Per-mount RDS for multistation mode: ?mount=m95_7.mp3 reads
    # now_playing-m95_7.json; the primary (fm.mp3) and the no-arg case use the
    # legacy now_playing.json. Captions/station_db below track active.env and are
    # only meaningful for the legacy mono tune (harmless otherwise).
    mount = request.args.get("mount")
    if mount and mount != "fm.mp3":
        base = mount[:-4] if mount.endswith(".mp3") else mount
        np_path = Path(f"/run/sdr-streams/now_playing-{base}.json")
    else:
        np_path = NOW_PLAYING_PATH
    rds      = _load_json(np_path)           or {}
    cap      = _load_json(CAPTIONS_PATH)    or {}
    hd_state = _load_json(HD_STATUS_PATH)   or {}
    _pilot   = pilot_state()

    caption_updated = cap.get("caption_updated", 0) or 0
    age_s = round(time.time() - caption_updated, 1) if caption_updated else None

    freq, band, is_hd, subchannel = current_tune()
    station_info = None
    if freq and band == "fm":
        station_info = station_db.lookup_fm(freq)
    elif freq and band == "am":
        station_info = station_db.lookup_am(freq)

    # Discrete now-playing track for client consumption: the identified song
    # (RDS or AcoustID) plus server-fetched cover art. Null when nothing is
    # identified (e.g. talk content). Mirrors lyrics.song, which is retained
    # for the existing web UI.
    song = cap.get("song")

    return jsonify({
        "available":      bool(rds) or bool(cap) or bool(station_info),
        "rds":            rds,
        "fcc":            station_info,
        "freq":           freq,
        "band":           band,
        "hd":             is_hd,
        "subchannel":     subchannel,
        "fcc_override":   bool(freq) and station_db.has_override(band, freq),
        "stereo":         current_stereo(),     # selected MODE (mono toggle/presets)
        # True pilot lock: stereo mode + FM + a fresh stereo pilot from stereo_decode.
        "pilot":          bool(band == "fm" and current_stereo() and _pilot["locked"]),
        "pilot_rms":      _pilot["rms"],
        "pilot_blend":    _pilot["blend"],
        "antenna":        current_antenna(),
        "hd_probing":     hd_state.get("hd_probing", False),
        "hd_locked":      hd_state.get("hd_locked", False),
        "hd_unavailable": hd_state.get("hd_unavailable", False),
        "mode":      cap.get("mode", "idle"),
        "track":     song,
        "caption": {
            "text":    cap.get("caption_text", ""),
            "updated": caption_updated,
            "age_s":   age_s,
        },
        "lyrics": {
            "song":  song,
            "lines": cap.get("lyrics_lines", []),
            "index": cap.get("lyrics_index", -1),
        },
    })


@app.route("/api/station-override", methods=["POST"])
def api_station_override():
    """Pin (or clear) a hand-corrected station name for a frequency. RDS PS / FCC
    data are often wrong — this wins over both in the UI. Empty name clears it.
    No stream restart: just rewrites overrides.json and reloads station_db."""
    payload = request.get_json(silent=True) or {}
    band = payload.get("band", "fm")
    freq = payload.get("freq")
    if band not in ("fm", "am") or not freq:
        return jsonify({"ok": False, "error": "band (fm|am) and freq required"}), 400
    try:
        info = station_db.set_override(
            band, freq,
            str(payload.get("name", "")).strip(),
            str(payload.get("city", "")).strip(),
            str(payload.get("state", "")).strip(),
        )
    except (ValueError, OSError) as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "band": band, "freq": freq, "override": info})


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
    # Optional per-preset audio settings: persist BEFORE write_env so it picks them
    # up — recalling a preset (freq + stereo + antenna) is then ONE restart, not
    # three. Omitted keys leave the current setting untouched (back-compat).
    try:
        if "stereo" in payload:
            ui_settings.save({"stereo": bool(payload.get("stereo"))})
        if payload.get("antenna") in ALLOWED_ANTENNAS:
            ui_settings.save({"antenna": payload["antenna"]})
        write_env(str(freq), band, hd, subchannel)
    except OSError as e:
        app.logger.error("api_tune write_env failed: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500
    clear_runtime_state()
    sysctl("restart")
    return jsonify({"ok": True, "freq": freq, "band": band, "hd": hd, "subchannel": subchannel,
                    "stereo": current_stereo(), "antenna": current_antenna()})


@app.route("/api/bitrate", methods=["POST"])
def api_bitrate():
    """Set the MP3 stream bitrate, then re-encode the current tune at it."""
    payload = request.get_json(silent=True) or {}
    br = str(payload.get("bitrate", "")).strip()
    if br not in ALLOWED_BITRATES:
        return jsonify({"ok": False, "error": f"invalid bitrate {br!r}"}), 400
    # Re-write active.env (which now picks up the new bitrate) and restart the
    # stream so ffmpeg re-encodes at it. Skip the restart if nothing is tuned.
    # Both writes can fail on a full/read-only disk — return JSON, not an HTML
    # 500 the UI can't parse.
    freq, band, is_hd, subchannel = current_tune()
    restarted = False
    try:
        ui_settings.save({"bitrate": br})
        if freq:
            write_env(freq, band, is_hd, subchannel)
    except OSError as e:
        app.logger.error("api_bitrate save/write_env failed: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500
    if freq:
        clear_runtime_state()
        sysctl("restart")
        restarted = True
    return jsonify({"ok": True, "bitrate": br, "restarted": restarted})


@app.route("/api/stereo", methods=["POST"])
def api_stereo():
    """Toggle FM stereo vs mono, then restart so stream.sh re-picks the path.
    Mono skips stereo_decode entirely (no 38 kHz L-R subcarrier) — much cleaner
    on weak/talk stations. Persisted in ui_settings; lands in active.env STEREO=."""
    payload = request.get_json(silent=True) or {}
    on = bool(payload.get("stereo", True))
    freq, band, is_hd, subchannel = current_tune()
    restarted = False
    try:
        ui_settings.save({"stereo": on})
        if freq:
            write_env(freq, band, is_hd, subchannel)
    except OSError as e:
        app.logger.error("api_stereo save/write_env failed: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500
    if freq:
        clear_runtime_state()
        sysctl("restart")
        restarted = True
    return jsonify({"ok": True, "stereo": on, "restarted": restarted})


@app.route("/api/antenna", methods=["POST"])
def api_antenna():
    """Select the FM antenna port (A = Shakespeare, B = dipole+LNA), then restart
    so wbfm_stream re-opens on it. Persisted in ui_settings; lands in active.env
    ANTENNA=. FM-only — has no effect on AM/HD."""
    payload = request.get_json(silent=True) or {}
    ant = str(payload.get("antenna", "")).strip()
    if ant not in ALLOWED_ANTENNAS:
        return jsonify({"ok": False, "error": f"invalid antenna {ant!r}"}), 400
    freq, band, is_hd, subchannel = current_tune()
    restarted = False
    try:
        ui_settings.save({"antenna": ant})
        if freq:
            write_env(freq, band, is_hd, subchannel)
    except OSError as e:
        app.logger.error("api_antenna save/write_env failed: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500
    if freq:
        clear_runtime_state()
        sysctl("restart")
        restarted = True
    return jsonify({"ok": True, "antenna": ant, "restarted": restarted})


# ---------------------------------------------------------------------------
# FM-multistation (mux) APIs
#   Mux mode is opt-in and mutually exclusive with the legacy mono stream, AM,
#   and wxsat (one device). Flask only writes channels.json and signals the mux;
#   it never touches the SDR. start/stop are admin controls (NPMplus auth).
# ---------------------------------------------------------------------------
def mux_window() -> tuple:
    """(lo, hi) MHz advertised tuning window, from mux.env."""
    lo, hi = 95.0, 101.0
    try:
        for line in MUX_ENV_PATH.read_text().splitlines():
            line = line.strip()
            if line.startswith("WINDOW_LO_MHZ="):
                lo = float(line.split("=", 1)[1].strip().strip('"'))
            elif line.startswith("WINDOW_HI_MHZ="):
                hi = float(line.split("=", 1)[1].strip().strip('"'))
    except (OSError, ValueError):
        pass
    return lo, hi


def mux_reload_signal() -> bool:
    """SIGHUP the running mux so it reloads channels.json. Same user (radio),
    so no sudo needed — the pid comes from mux_status.json."""
    doc = _load_json(MUX_STATUS_PATH) or {}
    pid = doc.get("pid")
    if not pid:
        return False
    try:
        os.kill(int(pid), signal.SIGHUP)
        return True
    except (ProcessLookupError, PermissionError, ValueError):
        return False


@app.route("/api/mux/status")
def api_mux_status():
    lo, hi = mux_window()
    status = _load_json(MUX_STATUS_PATH) or {}
    iq = _load_json(IQ_STATUS_PATH) or {}
    return jsonify({
        "mux_active":     is_active(MUX_SERVICE),
        "capture_active": is_active(IQ_CAPTURE_SERVICE),
        "window":         {"lo": lo, "hi": hi},
        "max_channels":   MAX_MUX_CHANNELS,
        "status":         status,
        "capture":        iq,
    })


@app.route("/api/mux/channels", methods=["GET", "POST"])
def api_mux_channels():
    if request.method == "GET":
        return jsonify(_load_json(CHANNELS_PATH) or {"channels": []})

    payload = request.get_json(silent=True) or {}
    chans = payload.get("channels")
    if not isinstance(chans, list):
        return jsonify({"ok": False, "error": "channels must be a list"}), 400
    if len(chans) > MAX_MUX_CHANNELS:
        return jsonify({"ok": False,
                        "error": f"at most {MAX_MUX_CHANNELS} channels"}), 400
    lo, hi = mux_window()
    clean, primaries = [], 0
    for ch in chans:
        try:
            freq = round(float(ch.get("freq")), 1)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": f"bad freq {ch.get('freq')!r}"}), 400
        if not (lo <= freq <= hi):
            return jsonify({"ok": False,
                            "error": f"{freq} outside window {lo}-{hi}"}), 400
        br = str(ch.get("bitrate", "192k"))
        if br not in ALLOWED_BITRATES:
            return jsonify({"ok": False, "error": f"invalid bitrate {br!r}"}), 400
        primary = bool(ch.get("primary"))
        primaries += int(primary)
        clean.append({"freq": freq, "stereo": bool(ch.get("stereo", True)),
                      "rds": bool(ch.get("rds", False)), "primary": primary,
                      "bitrate": br})
    if primaries > 1:
        return jsonify({"ok": False, "error": "only one channel may be primary"}), 400
    if clean and primaries == 0:
        clean[0]["primary"] = True  # ensure /fm.mp3 + now_playing.json exist
    try:
        CHANNELS_PATH.write_text(json.dumps({"channels": clean}, indent=2))
    except OSError as e:
        app.logger.error("api_mux_channels write failed: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500
    reloaded = mux_reload_signal() if is_active(MUX_SERVICE) == "active" else False
    return jsonify({"ok": True, "channels": clean, "reloaded": reloaded})


@app.route("/api/mux/start", methods=["POST"])
def api_mux_start():
    """Engage FM-multistation mode. Starting sdr-mux pulls in sdr-iq-capture
    (Requires=) which Conflicts=sdr-fm@active, so legacy mono stops automatically."""
    r = sysctl("start", MUX_SERVICE)
    if r.returncode != 0:
        return jsonify({"ok": False, "error": r.stderr.strip() or "start failed"}), 500
    return jsonify({"ok": True, "mux_active": is_active(MUX_SERVICE)})


@app.route("/api/mux/stop", methods=["POST"])
def api_mux_stop():
    """Disengage mux mode and roll back to known-good legacy mono."""
    sysctl("stop", MUX_SERVICE)
    sysctl("stop", IQ_CAPTURE_SERVICE)
    clear_runtime_state()
    r = sysctl("start", SERVICE)
    if r.returncode != 0:
        return jsonify({"ok": False, "error": r.stderr.strip() or "mono restart failed"}), 500
    return jsonify({"ok": True, "mono_active": is_active(SERVICE)})


# ---------------------------------------------------------------------------
# Weather-satellite (wxsat) APIs
#   GETs are public-safe like /radio. The delete POST mutates and is grouped
#   with the admin routes for NPMplus auth (see CLAUDE.md).
# ---------------------------------------------------------------------------
def _wxsat_captures():
    data = _load_json(WXSAT_CAPTURES_PATH)
    if not data:
        return []
    return data.get("captures", []) if isinstance(data, dict) else []


def _pass_snapshot_path(outdir):
    """Path to a capture's reviewable pass-panel snapshot, or None if the outdir
    is missing/unsafe. Our outdirs are single timestamp components."""
    outdir = str(outdir or "")
    if not outdir or "/" in outdir or ".." in outdir:
        return None
    return WXSAT_DIR / outdir / "pass.json"


@app.route("/api/wxsat/captures")
def api_wxsat_captures():
    caps = sorted(_wxsat_captures(), key=lambda c: c.get("aos_unix", 0), reverse=True)
    try:
        recent = int(request.args.get("recent", "0"))
    except ValueError:
        recent = 0
    if recent > 0:
        caps = caps[:recent]
    # Flag which captures have a replayable panel on disk ("Pass view"), and
    # which still have a retained baseband.cs16 that can be rebuilt from
    # ("Rebuild from IQ").
    for c in caps:
        snap = _pass_snapshot_path(c.get("outdir"))
        c["has_panel"] = bool(snap and snap.exists())
        iq = snap.with_name("baseband.cs16") if snap else None
        c["has_iq"] = bool(iq and iq.exists())
    return jsonify({"captures": caps})


@app.route("/api/wxsat/passes")
def api_wxsat_passes():
    data = _load_json(WXSAT_PASSES_PATH)
    if not data:
        return jsonify({"passes": [], "generated_at": None})
    return jsonify(data)


def _wxsat_human_listeners():
    """(count, ok) human listeners on the fm.mp3 mount, discounting the caption
    orchestrator (itself an Icecast consumer). ok=False if the query failed."""
    try:
        r = requests.get("http://localhost:8000/status-json.xsl", timeout=4)
        r.raise_for_status()
        src = r.json().get("icestats", {}).get("source")
        raw = 0
        if src is not None:
            for s in (src if isinstance(src, list) else [src]):
                url = str(s.get("listenurl", ""))
                if url.endswith("/fm.mp3") or url.endswith("fm.mp3"):
                    try:
                        raw = int(s.get("listeners", 0) or 0)
                    except (TypeError, ValueError):
                        raw = 0
                    break
        internal = 1 if is_active("sdr-captions") == "active" else 0
        return max(0, raw - internal), True
    except (requests.RequestException, ValueError) as e:
        app.logger.warning("wxsat listener check failed: %s", e)
        return 0, False


@app.route("/api/wxsat/status")
def api_wxsat_status():
    """Current tuner status + next scheduled pass + authorization + live listener
    count, for the /wxsat indicator and the /radio next-interruption notice."""
    st       = _load_json(WXSAT_STATUS_PATH) or {}
    passes   = (_load_json(WXSAT_PASSES_PATH) or {}).get("passes", [])
    auth     = _load_json(WXSAT_AUTH_PATH) or {}
    sched    = st.get("state")                       # scheduled | capturing | idle
    streaming = is_active(SERVICE) == "active"
    next_pass = st.get("next_pass") or (passes[0] if passes else None)

    # The capture script restarts the stream BEFORE the (offline) decode, so a
    # "capturing" scheduler state with the stream back up means we're decoding.
    if sched == "capturing":
        tuner = "capturing" if not streaming else "decoding"
    elif streaming:
        tuner = "streaming"
    else:
        tuner = "idle"

    authorized_aos = auth.get("aos_unix")
    next_authorized = bool(
        authorized_aos and next_pass
        and abs(int(authorized_aos) - int(next_pass.get("aos_unix", 0))) <= 120)
    listeners, listeners_ok = _wxsat_human_listeners()

    return jsonify({
        "tuner":           tuner,           # streaming | capturing | decoding | idle
        "stream_active":   streaming,
        "state":           sched,
        "dry_run":         st.get("dry_run", False),
        "next_pass":       next_pass,
        "capturing_pass":  st.get("capturing_pass"),
        "authorized_aos":  authorized_aos,
        "next_authorized": next_authorized,
        "listeners":       listeners,
        "listeners_ok":    listeners_ok,
    })


@app.route("/api/wxsat/live")
def api_wxsat_live():
    """Live per-pass telemetry written by wxsat_live.py during a capture
    (spectrum + level while recording, decode progress/SNR while decoding).
    Public-safe read. Returns {live:false} when idle or the frame is stale."""
    data = _load_json(WXSAT_LIVE_PATH)
    if not data or (time.time() - data.get("updated", 0)) > 8:
        return jsonify({"live": False})
    data["live"] = True
    return jsonify(data)


@app.route("/api/wxsat/pass/<cid>")
def api_wxsat_pass(cid):
    """Replayable pass-panel snapshot (waterfall + sky track + decode summary)
    saved by wxsat_live.py, for reviewing a pass after the live view is gone.
    Public-safe read. {available:false} if the pass has no saved panel."""
    rec = next((c for c in _wxsat_captures() if c.get("id") == cid), None)
    snap = _pass_snapshot_path(rec.get("outdir")) if rec else None
    data = _load_json(snap) if snap else None
    if not data:
        return jsonify({"available": False})
    data["available"] = True
    return jsonify(data)


@app.route("/api/wxsat/rebuild", methods=["POST"])
def api_wxsat_rebuild():
    """Reconstruct a pass panel offline from a retained baseband.cs16 (for a pass
    whose live panel was missed). Spawns wxsat_rebuild.py detached so the request
    returns immediately; the gallery picks up has_panel on its next refresh.
    Mutating/expensive — gate behind admin auth like the delete route."""
    payload = request.get_json(silent=True) or {}
    cid = str(payload.get("id", "")).strip()
    rec = next((c for c in _wxsat_captures() if c.get("id") == cid), None)
    if not rec:
        return jsonify({"ok": False, "error": "not found"}), 404
    snap = _pass_snapshot_path(rec.get("outdir"))
    if not snap or not snap.with_name("baseband.cs16").exists():
        return jsonify({"ok": False, "error": "no retained IQ for this pass"}), 400
    try:
        subprocess.Popen(
            ["/usr/bin/python3", "/opt/sdr-tuner/wxsat_rebuild.py", cid],
            cwd="/opt/sdr-tuner",
            env=dict(os.environ, HOME="/var/lib/sdr-streams/wxsat"),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "started": True})


@app.route("/api/wxsat/authorize", methods=["POST"])
def api_wxsat_authorize():
    """Listener pre-approval: capture the given pass even if someone's listening.
    A control action like /api/tune (not a public read). Body: {aos_unix} to set,
    {cancel:true} to clear."""
    payload = request.get_json(silent=True) or {}
    if payload.get("cancel"):
        try:
            WXSAT_AUTH_PATH.unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            return jsonify({"ok": False, "error": str(e)}), 500
        return jsonify({"ok": True, "authorized_aos": None})
    try:
        aos = int(payload.get("aos_unix"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "missing/invalid aos_unix"}), 400
    now = time.time()
    if aos < now - 300 or aos > now + 7 * 86400:
        return jsonify({"ok": False, "error": "aos_unix out of range"}), 400
    try:
        WXSAT_AUTH_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = WXSAT_AUTH_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"aos_unix": aos, "at": int(now)}))
        os.replace(tmp, WXSAT_AUTH_PATH)
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "authorized_aos": aos})


@app.route("/api/wxsat/space")
def api_wxsat_space():
    # Report free space on the filesystem holding the captures.
    for p in (WXSAT_DIR, WXSAT_DIR.parent, Path("/")):
        try:
            u = shutil.disk_usage(p)
            return jsonify({
                "total": u.total, "used": u.used, "free": u.free,
                "pct_used": round(u.used / u.total * 100, 1) if u.total else None,
            })
        except OSError:
            continue
    return jsonify({"total": None, "used": None, "free": None, "pct_used": None})


@app.route("/api/wxsat/image/<path:relpath>")
def api_wxsat_image(relpath):
    # send_from_directory rejects traversal (../) and absolute escapes.
    resp = send_from_directory(WXSAT_DIR, relpath)
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@app.route("/api/wxsat/delete", methods=["POST"])
def api_wxsat_delete():
    """Delete a capture (its product dir, if any) and drop it from the index.
    Mutating route — protect via NPMplus auth alongside the other admin routes."""
    payload = request.get_json(silent=True) or {}
    cid = str(payload.get("id", "")).strip()
    if not cid:
        return jsonify({"ok": False, "error": "missing id"}), 400
    caps = _wxsat_captures()
    rec = next((c for c in caps if c.get("id") == cid), None)
    if rec is None:
        return jsonify({"ok": False, "error": "not found"}), 404

    # Remove the per-pass capture directory (product images, capture.log, and any
    # retained baseband.cs16). Prefer the canonical `outdir`; fall back to the
    # image's top path component for legacy records that predate outdir. Keying
    # off `image` alone orphaned the multi-GB IQ of *failed* captures (no image
    # but a retained baseband.cs16) — using outdir reclaims it. Guard strictly
    # against escaping WXSAT_DIR.
    img = rec.get("image") or rec.get("thumb")
    top = rec.get("outdir") or (str(img).split("/", 1)[0] if img else "")
    if top:
        target = (WXSAT_DIR / top).resolve()
        try:
            base = WXSAT_DIR.resolve()
            if target == base or base not in target.parents:
                raise ValueError("refusing to delete outside wxsat dir")
            if target.is_dir():
                shutil.rmtree(target)
        except (OSError, ValueError) as e:
            app.logger.error("wxsat delete %s: %s", cid, e)
            return jsonify({"ok": False, "error": str(e)}), 500

    remaining = [c for c in caps if c.get("id") != cid]
    try:
        WXSAT_DIR.mkdir(parents=True, exist_ok=True)
        tmp = WXSAT_CAPTURES_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"captures": remaining}))
        os.replace(tmp, WXSAT_CAPTURES_PATH)
    except OSError as e:
        app.logger.error("wxsat delete index rewrite %s: %s", cid, e)
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "id": cid})


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
