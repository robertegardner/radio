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
