#!/usr/bin/env python3
"""SDR Tuner Flask UI."""
import json
import os
import shutil
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

SERVICE          = "sdr-fm@active"
SCAN_FM_SERVICE  = "sdr-scan.service"
SCAN_AM_SERVICE  = "sdr-am-scan.service"
ICECAST_PASS     = os.environ.get("ICECAST_PASS", "changeme")

# MP3 stream bitrates the user may pick from the radio UI. The value lands
# in active.env (BITRATE=) and is passed straight to ffmpeg, so it must be
# validated against this allowlist — never trust the raw request value.
ALLOWED_BITRATES = ["64k", "96k", "128k", "192k", "256k"]
DEFAULT_BITRATE  = "128k"


def current_bitrate() -> str:
    """Persisted stream bitrate, clamped to the allowlist."""
    br = ui_settings.load().get("bitrate", DEFAULT_BITRATE)
    return br if br in ALLOWED_BITRATES else DEFAULT_BITRATE

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
        f"MOUNT=fm.mp3\n"
        f"ICECAST_PASS={ICECAST_PASS}\n"
    )
    if hd:
        content += f"SUBCHANNEL={subchannel}\n"
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


@app.route("/api/wxsat/captures")
def api_wxsat_captures():
    caps = sorted(_wxsat_captures(), key=lambda c: c.get("aos_unix", 0), reverse=True)
    try:
        recent = int(request.args.get("recent", "0"))
    except ValueError:
        recent = 0
    if recent > 0:
        caps = caps[:recent]
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

    # Remove the product directory (first path component of the image), guarding
    # strictly against escaping WXSAT_DIR.
    img = rec.get("image") or rec.get("thumb")
    if img:
        top = str(img).split("/", 1)[0]
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
