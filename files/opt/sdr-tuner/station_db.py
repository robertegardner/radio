"""Station-name lookup for AM/FM frequencies.

Reads two sources, in priority order:
  1. /etc/sdr-streams/overrides.json - hand-curated, takes precedence
  2. /var/lib/sdr-streams/fcc.json   - bulk station data

Both are loaded once at import time. Call reload() to pick up changes.
"""
import json
from pathlib import Path

OVERRIDES_PATH = Path("/etc/sdr-streams/overrides.json")
FCC_PATH       = Path("/var/lib/sdr-streams/fcc.json")

_data = {"am": {}, "fm": {}, "overrides_am": {}, "overrides_fm": {}}


def _load_json(p):
    try:
        return json.loads(p.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def reload():
    fcc = _load_json(FCC_PATH) or {}
    ov  = _load_json(OVERRIDES_PATH) or {}
    _data["am"]            = fcc.get("am", {})
    _data["fm"]            = fcc.get("fm", {})
    _data["overrides_am"]  = ov.get("am", {})
    _data["overrides_fm"]  = ov.get("fm", {})


reload()


def _norm_am(khz):
    try:
        return str(int(float(khz)))
    except (TypeError, ValueError):
        return None


def _norm_fm(mhz):
    try:
        return f"{float(mhz):.1f}"
    except (TypeError, ValueError):
        return None


def lookup_am(khz):
    key = _norm_am(khz)
    if not key:
        return None
    return _data["overrides_am"].get(key) or _data["am"].get(key)


def lookup_fm(mhz):
    key = _norm_fm(mhz)
    if not key:
        return None
    return _data["overrides_fm"].get(key) or _data["fm"].get(key)


def hd_subchannels(mhz):
    """Return list of HD program indices (0-based) for a frequency, if known."""
    key = _norm_fm(mhz)
    if not key:
        return []
    info = _data["overrides_fm"].get(key) or _data["fm"].get(key)
    if not info:
        return []
    return list(info.get("hd_programs", []))


def label(info):
    if not info:
        return None
    call = info.get("call") or ""
    city = info.get("city") or ""
    st   = info.get("state") or ""
    if call and city and st:
        return f"{call} ({city}, {st})"
    if call:
        return call
    return None


# --- user-editable overrides (hand-corrected station IDs) --------------------
# RDS PS / the FCC bulk data are often wrong or missing; these let the UI pin a
# correct name per frequency. Written to OVERRIDES_PATH, which lookup_* already
# prefer over the bulk fcc.json.

def _norm(band, freq):
    return _norm_fm(freq) if band == "fm" else _norm_am(freq)


def has_override(band, freq) -> bool:
    key = _norm(band, freq)
    return bool(key) and key in _data.get(f"overrides_{band}", {})


def set_override(band, freq, call, city="", state=""):
    """Pin a station name for freq (band 'fm'/'am'). Persists + reloads. Empty
    call clears the override. Returns the stored info dict (or None if cleared)."""
    if band not in ("fm", "am"):
        raise ValueError(f"bad band {band!r}")
    key = _norm(band, freq)
    if not key:
        raise ValueError(f"bad freq {freq!r}")
    ov = _load_json(OVERRIDES_PATH) or {}
    ov.setdefault("am", {})
    ov.setdefault("fm", {})
    call = (call or "").strip()
    if not call:
        ov[band].pop(key, None)
        info = None
    else:
        info = {"call": call}
        if (city or "").strip():  info["city"]  = city.strip()
        if (state or "").strip(): info["state"] = state.strip()
        # keep any existing hd_programs so the override doesn't drop HD info
        prev = ov[band].get(key) or {}
        if prev.get("hd_programs"):
            info["hd_programs"] = prev["hd_programs"]
        ov[band][key] = info
    OVERRIDES_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = OVERRIDES_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(ov, indent=2))
    tmp.replace(OVERRIDES_PATH)
    reload()
    return info


def clear_override(band, freq):
    return set_override(band, freq, "")
