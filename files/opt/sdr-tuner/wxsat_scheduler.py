#!/usr/bin/env python3
"""Weather-satellite capture scheduler (wxsat).

Long-running daemon that owns the wxsat loop so the broadcast radio keeps the
SDR the vast majority of the time. Each loop it:

  1. refreshes the Meteor-M pass predictions (writes wxsat_passes.json),
  2. sleeps until the next pass's AOS-minus-buffer,
  3. at pass time runs the *listener check* — if anyone is listening to the
     radio stream it SKIPS the capture (radio keeps streaming) and records a
     skip with a human notation; otherwise it captures.

DRY_RUN=1 (the Phase-1 default) does the real loop + the real listener check
but never touches the SDR: it just records "would_capture" / "would_skip".
Phase 2 flips DRY_RUN=0 and wires wxsat_capture.sh.

Outcomes recorded to /var/lib/sdr-streams/wxsat/captures.json:
  image | skipped | failed         (real mode)
  would_capture | would_skip       (dry run)

This script never touches the SDR while DRY_RUN=1.
"""
import argparse
import json
import logging
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

import wxsat_predict as predict

log = logging.getLogger("wxsat.sched")

WXSAT_DIR     = predict.WXSAT_DIR
CAPTURES_PATH = WXSAT_DIR / "captures.json"
STATUS_PATH   = Path("/run/sdr-streams/wxsat_status.json")
AUTH_PATH     = Path("/run/sdr-streams/wxsat_authorized.json")
NOW_PLAYING_PATH = Path("/run/sdr-streams/now_playing.json")
ACTIVE_ENV_PATH  = Path("/etc/sdr-streams/active.env")
ICECAST_STATUS_URL = "http://localhost:8000/status-json.xsl"
MOUNT = "fm.mp3"
CAPTURE_SCRIPT = "/opt/sdr-tuner/wxsat_capture.sh"
# The caption orchestrator pulls the Icecast mount back to PCM, so while it is
# running it shows up as one Icecast listener. Discount that internal consumer
# so the skip-when-listening check only reacts to *human* listeners.
CAPTIONS_SERVICE = "sdr-captions"


# ---------------------------------------------------------------------------
# Listener check (skip-when-listening)
# ---------------------------------------------------------------------------
def icecast_listeners():
    """Return (listeners, ok). `ok` is False if the status query itself failed
    — callers default to capturing in that case rather than blocking a pass."""
    try:
        r = requests.get(ICECAST_STATUS_URL, timeout=5)
        r.raise_for_status()
        src = r.json().get("icestats", {}).get("source")
        if src is None:
            return 0, True
        # Icecast emits a single object for one mount, a list for several.
        sources = src if isinstance(src, list) else [src]
        for s in sources:
            url = str(s.get("listenurl", ""))
            if url.endswith("/" + MOUNT) or url.endswith(MOUNT):
                try:
                    return int(s.get("listeners", 0) or 0), True
                except (TypeError, ValueError):
                    return 0, True
        # Our mount isn't live (stream down?) → nobody listening to it.
        return 0, True
    except (requests.RequestException, json.JSONDecodeError, ValueError) as e:
        log.warning("listener check failed (%s) — defaulting to capture", e)
        return 0, False


def internal_consumers():
    """Count our own non-human consumers of the mount. The caption orchestrator
    holds one connection whenever sdr-captions is running."""
    try:
        active = subprocess.run(["systemctl", "is-active", CAPTIONS_SERVICE],
                                capture_output=True, text=True).stdout.strip()
        return 1 if active == "active" else 0
    except OSError:
        return 0


def human_listeners():
    """(human_listeners, ok) — raw Icecast listeners minus our internal
    consumers. ok=False if the status query failed (callers then capture)."""
    raw, ok = icecast_listeners()
    if not ok:
        return 0, False
    internal = internal_consumers()
    effective = max(0, raw - internal)
    log.info("listeners: raw=%d internal=%d human=%d", raw, internal, effective)
    return effective, True


def _read_active_env():
    out = {}
    try:
        for line in ACTIVE_ENV_PATH.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"')
    except (FileNotFoundError, OSError):
        pass
    return out


def compose_notation():
    """A human note for a skip, e.g. 'listener present, playing KGMO 100.7 FM'.

    Prefers the authoritative FCC call sign over the live RDS PS, which on many
    stations scrolls (KGMO cycles '100.7' / 'KGMO' / 'CLASSIC' ...) and is an
    unreliable station label.
    """
    env = _read_active_env()
    freq = env.get("FREQ", "")
    mode = (env.get("MODE", "") or "").lower()
    band = "AM" if mode == "am" else "FM"
    freq_disp = freq.rstrip("Mk") if freq else ""

    call = None
    try:
        import station_db
        info = (station_db.lookup_am(freq_disp) if band == "AM"
                else station_db.lookup_fm(freq_disp)) if freq_disp else None
        if info:
            call = info.get("call")
    except Exception:
        call = None
    if not call:
        # Fall back to RDS PS, but only if it looks like a name (not the freq).
        try:
            ps = (json.loads(NOW_PLAYING_PATH.read_text()).get("ps") or "").strip()
            if ps and ps != freq_disp:
                call = ps
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass

    # Build "CALL freq BAND", dropping blanks and avoiding a freq/call dupe.
    parts = []
    if call and call != freq_disp:
        parts.append(call)
    if freq_disp:
        parts.append(freq_disp)
    parts.append(band)
    where = " ".join(parts).strip()
    return f"listener present, playing {where}" if where else "listener present"


# ---------------------------------------------------------------------------
# Captures index
# ---------------------------------------------------------------------------
def _slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _load_index():
    try:
        d = json.loads(CAPTURES_PATH.read_text())
        return d.get("captures", []) if isinstance(d, dict) else []
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []


def _save_index(captures):
    WXSAT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CAPTURES_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"captures": captures}))
    os.replace(tmp, CAPTURES_PATH)


def record_outcome(p, outcome, notation=None, reason=None,
                   image=None, thumb=None, listeners=0, authorized=False,
                   outdir=None):
    rec = {
        "id": f"{_slug(p['satellite'])}-{int(p['aos_unix'])}",
        "satellite": p["satellite"],
        "norad": p.get("norad"),
        "aos_unix": int(p["aos_unix"]),
        "los_unix": int(p["los_unix"]),
        "max_elev": p.get("max_elev"),
        "duration_min": p.get("duration_min"),
        "outcome": outcome,
        "notation": notation,
        "reason": reason,
        "image": image,
        "thumb": thumb,
        "listeners": listeners,
        "authorized": authorized,
        # Relative path of the capture dir (holds capture.log + any retained IQ
        # + SatDump products) so a pass can be diagnosed/re-decoded offline.
        "outdir": outdir,
        "created": int(time.time()),
    }
    captures = _load_index()
    # De-dupe by id (a pass should only be recorded once).
    captures = [c for c in captures if c.get("id") != rec["id"]]
    captures.append(rec)
    _save_index(captures)
    log.info("recorded %s for %s (AOS %s, max %.0f deg)%s",
             outcome, p["satellite"], p["aos_iso"], p.get("max_elev") or 0,
             f" — {notation}" if notation else "")
    return rec


# ---------------------------------------------------------------------------
# Live status + listener authorization
#   The radio/wxsat pages read these. Authorization lets a listener pre-approve
#   the next interruption so the capture runs even while they're listening.
# ---------------------------------------------------------------------------
def write_status(cfg, state, next_pass=None, capturing=None):
    payload = {
        "state": state,                # scheduled | capturing | idle
        "updated": int(time.time()),
        "dry_run": cfg["dry_run"],
        "next_pass": next_pass,
        "capturing_pass": capturing,
    }
    try:
        STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATUS_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload))
        os.replace(tmp, STATUS_PATH)
    except OSError as e:
        log.warning("write_status failed: %s", e)


def read_authorized_aos():
    """The AOS the listener authorized for capture, or None."""
    try:
        return int(json.loads(AUTH_PATH.read_text()).get("aos_unix"))
    except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
        return None


def clear_authorization():
    try:
        AUTH_PATH.unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        log.warning("clear_authorization failed: %s", e)


# ---------------------------------------------------------------------------
# Pass handling
# ---------------------------------------------------------------------------
def do_capture(p, cfg, authorized=False):
    """Run a real capture via wxsat_capture.sh (Phase 2). The script stops the
    stream, runs rx_sdr -> satdump, and ALWAYS restarts the stream via trap."""
    out_dir = WXSAT_DIR / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    reldir = out_dir.name  # relative to WXSAT_DIR; capture.log + IQ + products live here
    duration = max(60, int(p["los_unix"] - time.time()) + int(cfg["post_los_s"]))
    env = dict(os.environ, WXSAT_OUT_DIR=str(out_dir), WXSAT_DURATION=str(duration))
    log.info("CAPTURE %s -> %s (%ss)", p["satellite"], out_dir, duration)
    write_status(cfg, "capturing", capturing=p)
    try:
        # Generous headroom: the script stops the stream, captures `duration`s,
        # restarts the stream, then decodes offline (decode can take minutes).
        r = subprocess.run([CAPTURE_SCRIPT], env=env, capture_output=True,
                           text=True, timeout=duration + 600)
    except subprocess.TimeoutExpired:
        return record_outcome(p, "failed", reason="capture timed out",
                              authorized=authorized, outdir=reldir)
    if r.returncode != 0:
        reason = (r.stderr or r.stdout or "capture failed").strip().splitlines()[-1:] or ["capture failed"]
        return record_outcome(p, "failed", reason=f"{reason[0][:180]} (see {reldir}/capture.log)",
                              authorized=authorized, outdir=reldir)
    image, thumb = _best_product(out_dir)
    if not image:
        return record_outcome(p, "failed", reason=f"no image product decoded (see {reldir}/capture.log)",
                              authorized=authorized, outdir=reldir)
    return record_outcome(p, "image", image=image, thumb=thumb or image,
                          authorized=authorized, outdir=reldir)


def _best_product(out_dir):
    """Pick the best PNG from a satdump output dir; return (image, thumb) as
    paths relative to WXSAT_DIR, or (None, None)."""
    try:
        pngs = sorted(out_dir.rglob("*.png"), key=lambda f: f.stat().st_size, reverse=True)
    except OSError:
        return None, None
    if not pngs:
        return None, None
    best = pngs[0].relative_to(WXSAT_DIR).as_posix()
    return best, best


def handle_pass(p, cfg):
    """The capture-vs-skip decision for one pass."""
    listeners, ok = human_listeners()
    # A listener can pre-authorize this pass to capture despite the interruption.
    auth_aos = read_authorized_aos()
    is_auth = auth_aos is not None and abs(auth_aos - int(p["aos_unix"])) <= 120

    try:
        if ok and listeners > 0 and not is_auth:
            notation = compose_notation()
            outcome = "would_skip" if cfg["dry_run"] else "skipped"
            return record_outcome(p, outcome, notation=notation, listeners=listeners)

        # Capture path: 0 human listeners, a stats hiccup, or listener-authorized.
        if is_auth:
            log.info("pass authorized by listener — capturing despite %d listener(s)", listeners)
        if cfg["dry_run"]:
            note = ("authorized by listener — would capture despite listeners" if is_auth
                    else (None if ok else "listener check failed — would capture anyway"))
            return record_outcome(p, "would_capture", reason=note,
                                  listeners=listeners, authorized=is_auth)
        return do_capture(p, cfg, authorized=is_auth)
    finally:
        if is_auth:
            clear_authorization()


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def run(cfg):
    processed = set()  # (norad, aos_unix) already handled this process lifetime
    log.info("wxsat scheduler starting (dry_run=%s, sats=%s, min_elev=%g)",
             cfg["dry_run"], [s["name"] for s in cfg["satellites"]], cfg["min_elev"])
    while True:
        passes = predict.compute_passes(cfg)
        predict.write_passes(passes, cfg)
        now = time.time()

        # A pass currently within its capture window and not yet handled.
        due = [p for p in passes
               if (p["norad"], int(p["aos_unix"])) not in processed
               and p["aos_unix"] - cfg["aos_buffer_s"] <= now <= p["los_unix"]]
        if due:
            p = due[0]
            handle_pass(p, cfg)
            processed.add((p["norad"], int(p["aos_unix"])))
            # Sleep past LOS so we don't re-trigger the same pass.
            time.sleep(max(5, p["los_unix"] + cfg["post_los_s"] - time.time()))
            continue

        upcoming = [p for p in passes
                    if (p["norad"], int(p["aos_unix"])) not in processed
                    and p["aos_unix"] - cfg["aos_buffer_s"] > now]
        if upcoming:
            nxt = min(upcoming, key=lambda p: p["aos_unix"])
            sleep_s = min(cfg["refresh_interval_s"],
                          max(5, nxt["aos_unix"] - cfg["aos_buffer_s"] - now))
            write_status(cfg, "scheduled", next_pass=nxt)
            log.info("next: %s AOS %s (max %.0f deg) — sleeping %.0fs",
                     nxt["satellite"], nxt["aos_iso"], nxt.get("max_elev") or 0, sleep_s)
        else:
            sleep_s = cfg["refresh_interval_s"]
            write_status(cfg, "idle")
            log.info("no upcoming passes >= %g deg in %gh — sleeping %.0fs",
                     cfg["min_elev"], cfg["predict_hours"], sleep_s)
        time.sleep(sleep_s)


def _synthetic_pass(cfg):
    now = int(time.time())
    sat = cfg["satellites"][0]["name"] if cfg["satellites"] else "METEOR-M2 4"
    norad = cfg["satellites"][0]["norad"] if cfg["satellites"] else 59051
    return {
        "satellite": sat, "norad": norad,
        "aos_unix": now, "los_unix": now + 600,
        "aos_iso": datetime.now(timezone.utc).isoformat(),
        "los_iso": datetime.fromtimestamp(now + 600, timezone.utc).isoformat(),
        "max_elev": 0.0, "duration_min": 10.0,
    }


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    # pyorbital chatters an INFO line per pass about parabolic interpolation;
    # it's benign (returns the best guess) — quiet it so the journal stays readable.
    logging.getLogger("pyorbital").setLevel(logging.WARNING)
    ap = argparse.ArgumentParser(description="wxsat capture scheduler")
    ap.add_argument("--capture-now", nargs="?", type=int, const=90, default=None,
                    metavar="SECS",
                    help="REAL capture immediately for SECS seconds (default 90), "
                         "bypassing the schedule AND the listener check, then exit. "
                         "Forces a capture even when DRY_RUN=1 — for testing the "
                         "rx_sdr -> satdump chain and stream restore.")
    ap.add_argument("--test-pass", action="store_true",
                    help="run handle_pass() once on a synthetic now-pass and exit "
                         "(exercises the listener check + record path; honours DRY_RUN)")
    args = ap.parse_args()
    cfg = predict.load_config()

    if args.capture_now is not None:
        secs = max(30, args.capture_now)
        p = _synthetic_pass(cfg)
        p["aos_unix"] = int(time.time())
        p["los_unix"] = int(time.time()) + secs
        log.info("--capture-now: REAL %ss capture of %s (bypassing listener check)",
                 secs, p["satellite"])
        rec = do_capture(p, cfg)
        print(json.dumps(rec, indent=2))
        return

    if args.test_pass:
        log.info("test-pass: synthetic pass, dry_run=%s", cfg["dry_run"])
        rec = handle_pass(_synthetic_pass(cfg), cfg)
        print(json.dumps(rec, indent=2))
        return
    run(cfg)


if __name__ == "__main__":
    main()
