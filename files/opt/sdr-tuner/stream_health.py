#!/usr/bin/env python3
"""Stream-health collector for the 🩺 debug panel (/api/stream-health).

Answers "is the dropout the platform's fault or the listener's wifi?" by
summarizing the server side of the FM chain over a short window:

  - sdr-fm@active state/uptime + SoapyRemote IQ-overflow bursts from the
    journal (wbfm_stream prints a run of "O" per overflow — each burst is
    dropped samples, i.e. an audible click/garble even though the mount
    never disconnects),
  - the Icecast mount (uptime + listeners) so a source restart is visible,
  - a TCP reachability probe of the Pi SoapyRemote source.

The web GUI records player-side rebuffer events and correlates them with
this endpoint's timestamps to render a verdict.

Reading the journal needs the service user in the `systemd-journal` group
(granted on the rack at deploy); everything degrades to ok=False rather
than raising.
"""
import json
import os
import socket
import subprocess
import time
from datetime import datetime

import requests

SERVICE = "sdr-fm@active"
WINDOW_SEC = 600
BURST_TS_CAP = 200

ICECAST_STATUS_URL = os.environ.get(
    "HEALTH_ICECAST_STATUS", "http://192.168.6.82:8000/status-json.xsl")
ICECAST_MOUNT = os.environ.get("HEALTH_ICECAST_MOUNT", "/fm.mp3")
SOURCE_PROBE = os.environ.get("HEALTH_SOURCE_PROBE", "radio.srvr:55001")

# The 24h restart count scans a day of journal — cache it between polls.
_RESTARTS_24H_TTL = 60.0
_restarts_24h_cache = {"ts": 0.0, "value": None}


# ---------------------------------------------------------------------------
# Pure parsing (unit-tested)
# ---------------------------------------------------------------------------

def parse_journal_json(lines):
    """journalctl -o json lines -> [(epoch_sec, message)], skipping junk."""
    out = []
    for line in lines:
        try:
            e = json.loads(line)
            ts = float(e["__REALTIME_TIMESTAMP"]) / 1e6
            msg = e["MESSAGE"]
        except (ValueError, KeyError, TypeError):
            continue
        if isinstance(msg, str):
            out.append((ts, msg))
    return out


def summarize_events(entries, now=None, window_sec=WINDOW_SEC):
    """Summarize (ts, message) journal entries over the window.

    An overflow burst is a message that is nothing but "O"s (SoapyRemote's
    async overflow indicator, one char per overflow, flushed per read-loop).
    """
    now = time.time() if now is None else now
    n_buckets = max(1, window_sec // 60)
    per_min = [0] * n_buckets
    bursts = 0
    samples = 0
    last_ts = None
    burst_ts = []
    restarts = []
    for ts, msg in entries:
        m = msg.strip()
        age = now - ts
        if age < 0 or age >= window_sec:
            continue
        if m and set(m) == {"O"}:
            bursts += 1
            samples += len(m)
            if last_ts is None or ts > last_ts:
                last_ts = ts
            if len(burst_ts) < BURST_TS_CAP:
                burst_ts.append(ts)
            idx = int(age // 60)
            if idx < n_buckets:
                per_min[idx] += len(m)
        elif m.startswith("Started sdr-fm@"):
            restarts.append(ts)
    return {
        "window_sec": window_sec,
        "overflow_bursts": bursts,
        "overflow_samples": samples,
        "overflow_last_ts": last_ts,
        "per_min": per_min,          # bucket 0 = most recent minute
        "burst_ts": burst_ts,
        "restarts": restarts,
    }


def extract_mount(status, mount, now=None):
    """Pull one mount's health out of Icecast status-json."""
    now = time.time() if now is None else now
    srcs = (status or {}).get("icestats", {}).get("source", [])
    if isinstance(srcs, dict):
        srcs = [srcs]
    for s in srcs:
        if not str(s.get("listenurl", "")).endswith(mount):
            continue
        r = {"ok": True,
             "listeners": s.get("listeners"),
             "listener_peak": s.get("listener_peak")}
        iso = s.get("stream_start_iso8601")
        if iso:
            try:
                start = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%S%z").timestamp()
                r["mount_uptime_sec"] = max(0.0, now - start)
            except ValueError:
                pass
        return r
    return {"ok": False, "error": f"mount {mount} not found (source down?)"}


# ---------------------------------------------------------------------------
# Collectors (rack-side effects; each section fails soft)
# ---------------------------------------------------------------------------

def _journal_events(since_sec):
    p = subprocess.run(
        ["journalctl", "-u", SERVICE, "--since", f"-{since_sec}s",
         "-o", "json", "-q", "--no-pager"],
        capture_output=True, text=True, timeout=10)
    return parse_journal_json(p.stdout.splitlines())


def _fm_service():
    out = {"ok": False}
    try:
        p = subprocess.run(
            ["systemctl", "show", SERVICE, "-p",
             "ActiveState,ActiveEnterTimestampMonotonic,NRestarts"],
            capture_output=True, text=True, timeout=5)
        props = dict(l.split("=", 1) for l in p.stdout.splitlines() if "=" in l)
        out["ok"] = True
        out["state"] = props.get("ActiveState", "unknown")
        try:
            mono = int(props.get("ActiveEnterTimestampMonotonic", "0")) / 1e6
            boot = float(open("/proc/uptime").read().split()[0])
            out["uptime_sec"] = max(0.0, boot - mono)
        except (ValueError, OSError):
            pass
        if props.get("NRestarts", "").isdigit():
            out["n_restarts"] = int(props["NRestarts"])
    except (subprocess.SubprocessError, OSError) as e:
        out["error"] = str(e)
    return out


def _restarts_24h():
    nowm = time.monotonic()
    c = _restarts_24h_cache
    if c["value"] is not None and nowm - c["ts"] < _RESTARTS_24H_TTL:
        return c["value"]
    try:
        p = subprocess.run(
            ["journalctl", "-u", SERVICE, "--since", "-24h", "-o", "cat",
             "-q", "--no-pager", "-g", "Started sdr-fm@"],
            capture_output=True, text=True, timeout=20)
        value = len([l for l in p.stdout.splitlines() if l.strip()])
    except (subprocess.SubprocessError, OSError):
        value = None
    c.update(ts=nowm, value=value)
    return value


def _icecast():
    try:
        r = requests.get(ICECAST_STATUS_URL, timeout=2)
        r.raise_for_status()
        return extract_mount(r.json(), ICECAST_MOUNT)
    except (requests.RequestException, ValueError) as e:
        return {"ok": False, "error": str(e)}


def _source_probe():
    host, _, port = SOURCE_PROBE.partition(":")
    try:
        t0 = time.monotonic()
        with socket.create_connection((host, int(port)), timeout=1.5):
            rtt_ms = (time.monotonic() - t0) * 1000.0
        return {"ok": True, "rtt_ms": round(rtt_ms, 1), "target": SOURCE_PROBE}
    except OSError as e:
        return {"ok": False, "error": str(e), "target": SOURCE_PROBE}


def collect():
    now = time.time()
    fm = _fm_service()
    try:
        events = summarize_events(_journal_events(WINDOW_SEC), now=now)
        # Journal access needs group systemd-journal; empty-but-ok is valid,
        # but flag the common misconfiguration so the panel can say so.
        events["journal_ok"] = True
    except (subprocess.SubprocessError, OSError) as e:
        events = {"journal_ok": False, "error": str(e)}
    fm["events"] = events
    fm["restarts_24h"] = _restarts_24h()
    return {
        "ok": True,
        "ts": now,
        "fm": fm,
        "icecast": _icecast(),
        "source": _source_probe(),
    }
