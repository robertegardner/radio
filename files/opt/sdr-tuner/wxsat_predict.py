#!/usr/bin/env python3
"""Weather-satellite pass prediction for the wxsat feature.

Fetches Meteor-M TLEs from a reachable source (tle.ivanstanojevic.me — note
that celestrak.org is *unreachable* from this Pi, see CLAUDE.md / wxsat notes),
caches them under /var/lib/sdr-streams/wxsat/tle/, and uses pyorbital to compute
the upcoming Meteor-M2-4 LRPT passes above MIN_ELEV_DEG for our QTH.

Run standalone to (re)write /run/sdr-streams/wxsat_passes.json:

    python3 wxsat_predict.py

The scheduler imports compute_passes()/write_passes()/load_config() directly.

This module never touches the SDR.
"""
import calendar
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from pyorbital.orbital import Orbital

log = logging.getLogger("wxsat.predict")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
WXSAT_DIR  = Path("/var/lib/sdr-streams/wxsat")
TLE_DIR    = WXSAT_DIR / "tle"
PASSES_PATH = Path("/run/sdr-streams/wxsat_passes.json")

# ---------------------------------------------------------------------------
# Satellite catalogue. Each entry can be toggled via an env flag. Meteor-M2-4
# is the live LRPT bird (137.9 MHz); Meteor-M2-3 is in storage mode and off by
# default but can be enabled from wxsat.env.
# ---------------------------------------------------------------------------
CATALOG = [
    {"name": "METEOR-M2 4", "norad": 59051, "flag": "M2_4_ENABLED", "default": "1"},
    {"name": "METEOR-M2 3", "norad": 57166, "flag": "M2_3_ENABLED", "default": "0"},
]


def _env(key, default):
    v = os.environ.get(key)
    return v if v not in (None, "") else default


def load_config():
    """Resolve wxsat config from the environment (systemd EnvironmentFile) with
    sensible defaults. Shared by the predictor and the scheduler."""
    sats = [
        dict(s) for s in CATALOG
        if str(_env(s["flag"], s["default"])).lower() in ("1", "true", "yes", "on")
    ]
    return {
        "lat": float(_env("LAT", "37.31")),
        "lon": float(_env("LON", "-89.55")),
        "alt_km": float(_env("ALT_KM", "0.1")),
        "min_elev": float(_env("MIN_ELEV_DEG", "20")),
        "predict_hours": float(_env("PREDICT_HOURS", "48")),
        "tle_url_template": _env("TLE_URL_TEMPLATE",
                                 "https://tle.ivanstanojevic.me/api/tle/{norad}"),
        "tle_ttl_hours": float(_env("TLE_TTL_HOURS", "12")),
        "satellites": sats,
        # capture/scheduler knobs (read here so one loader covers both scripts)
        "aos_buffer_s": float(_env("AOS_BUFFER_S", "45")),
        "post_los_s": float(_env("POST_LOS_S", "15")),
        "refresh_interval_s": float(_env("REFRESH_INTERVAL_S", "1800")),
        "dry_run": str(_env("DRY_RUN", "1")).lower() in ("1", "true", "yes", "on"),
        "min_free_gb": float(_env("WXSAT_MIN_FREE_GB", "2")),
        "freq_mhz": float(_env("FREQ_MHZ", "137.9")),
        "antenna": _env("ANTENNA", "Antenna B"),
        "samplerate": _env("SAMPLERATE", "2048000"),
        "gain": _env("GAIN", "40"),
        "lrpt_pipeline": _env("LRPT_PIPELINE", "meteor_m2-x_lrpt"),
    }


# ---------------------------------------------------------------------------
# TLE fetch + cache
# ---------------------------------------------------------------------------
def _cache_path(norad):
    return TLE_DIR / f"{norad}.tle"


def _read_cache(norad):
    """Return (name, line1, line2, age_hours) from cache, or None."""
    p = _cache_path(norad)
    try:
        lines = p.read_text().splitlines()
        if len(lines) >= 3:
            age_h = (calendar.timegm(datetime.now(timezone.utc).utctimetuple())
                     - p.stat().st_mtime) / 3600.0
            return lines[0].strip(), lines[1].strip(), lines[2].strip(), age_h
    except (FileNotFoundError, OSError):
        pass
    return None


def _write_cache(norad, name, l1, l2):
    TLE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _cache_path(norad).with_suffix(".tle.tmp")
    tmp.write_text(f"{name}\n{l1}\n{l2}\n")
    os.replace(tmp, _cache_path(norad))


def fetch_tle(sat, cfg):
    """Return (name, line1, line2) for a satellite, preferring a fresh network
    fetch but falling back to cache. Returns None only if both fail."""
    norad = sat["norad"]
    cached = _read_cache(norad)
    # Use cache without hitting the network if it is still within TTL.
    if cached and cached[3] <= cfg["tle_ttl_hours"]:
        return cached[0], cached[1], cached[2]

    url = cfg["tle_url_template"].format(norad=norad)
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": "sdr-tuner-wxsat/1.0"})
        r.raise_for_status()
        d = r.json()
        name = d.get("name") or sat["name"]
        l1, l2 = d["line1"], d["line2"]
        if not (l1.startswith("1 ") and l2.startswith("2 ")):
            raise ValueError("malformed TLE lines")
        _write_cache(norad, name, l1, l2)
        log.info("TLE fetched for %s (%s)", name, norad)
        return name, l1, l2
    except (requests.RequestException, ValueError, KeyError, json.JSONDecodeError) as e:
        if cached:
            log.warning("TLE fetch failed for %s (%s); using cached (%.1fh old): %s",
                        sat["name"], norad, cached[3], e)
            return cached[0], cached[1], cached[2]
        log.error("TLE fetch failed for %s (%s) and no cache available: %s",
                  sat["name"], norad, e)
        return None


# ---------------------------------------------------------------------------
# Pass prediction
# ---------------------------------------------------------------------------
def _to_unix(dt_naive_utc):
    """pyorbital returns naive datetimes in UTC; convert to a unix timestamp."""
    return calendar.timegm(dt_naive_utc.timetuple())


def compute_passes(cfg):
    """Compute upcoming passes for all enabled satellites, sorted by AOS.

    Returns a list of pass dicts. Each: satellite, norad, aos_unix, los_unix,
    aos_iso, los_iso, max_elev, duration_min.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)  # pyorbital wants naive UTC
    passes = []
    for sat in cfg["satellites"]:
        tle = fetch_tle(sat, cfg)
        if not tle:
            continue
        name, l1, l2 = tle
        try:
            orb = Orbital(name, line1=l1, line2=l2)
        except Exception as e:  # pyorbital raises bare exceptions on bad elements
            log.error("Orbital() failed for %s: %s", name, e)
            continue
        try:
            raw = orb.get_next_passes(now, int(cfg["predict_hours"]),
                                      cfg["lon"], cfg["lat"], cfg["alt_km"],
                                      horizon=0)
        except Exception as e:
            log.error("get_next_passes failed for %s: %s", name, e)
            continue
        for rise, fall, maxt in raw:
            try:
                _, max_elev = orb.get_observer_look(maxt, cfg["lon"], cfg["lat"],
                                                    cfg["alt_km"])
            except Exception:
                max_elev = 0.0
            if max_elev < cfg["min_elev"]:
                continue
            aos_u, los_u = _to_unix(rise), _to_unix(fall)
            passes.append({
                "satellite": name,
                "norad": sat["norad"],
                "aos_unix": aos_u,
                "los_unix": los_u,
                "aos_iso": rise.replace(tzinfo=timezone.utc).isoformat(),
                "los_iso": fall.replace(tzinfo=timezone.utc).isoformat(),
                "max_elev": round(max_elev, 1),
                "duration_min": round((los_u - aos_u) / 60.0, 1),
            })
    passes.sort(key=lambda p: p["aos_unix"])
    return passes


def write_passes(passes, cfg):
    """Atomically write the upcoming-pass list for the web UI."""
    PASSES_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": int(_to_unix(datetime.now(timezone.utc).replace(tzinfo=None))),
        "location": {"lat": cfg["lat"], "lon": cfg["lon"]},
        "min_elev": cfg["min_elev"],
        "passes": passes,
    }
    tmp = PASSES_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, PASSES_PATH)


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("pyorbital").setLevel(logging.WARNING)
    cfg = load_config()
    passes = compute_passes(cfg)
    write_passes(passes, cfg)
    log.info("wrote %d passes (>= %g deg) to %s", len(passes), cfg["min_elev"], PASSES_PATH)
    for p in passes[:12]:
        log.info("  %s  AOS %s  %.1fmin  max %.1f deg",
                 p["satellite"], p["aos_iso"], p["duration_min"], p["max_elev"])


if __name__ == "__main__":
    main()
