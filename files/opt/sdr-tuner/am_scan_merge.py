#!/usr/bin/env python3
"""Merge per-device AM scans into one stations_am.json.

am_scan.py can only sweep one device per run, so the full antenna survey is two
runs — the dx-R2 (ports A/B/C) and the HF+ YouLoop (its single RX) — merged here.
Combines by_antenna across the inputs, remaps the HF+'s "RX" key to "HF+", and
picks the best antenna per station. Usage: am_scan_merge.py IN1 [IN2 ...] OUT
"""
import json
import sys
from datetime import datetime


# am_scan keys antennas by the last word of the name ("Antenna B" -> "B",
# "RX" -> "RX"). Normalize the HF+ to "HF+" so the UI + /api/tune agree.
def norm(k):
    return "HF+" if k == "RX" else k


ANT_FULL = {"A": "Antenna A", "B": "Antenna B", "C": "Antenna C", "HF+": "HF+"}


def load(p):
    try:
        return json.loads(open(p).read())
    except Exception as e:  # noqa: BLE001 — a missing/failed partial is non-fatal
        sys.stderr.write(f"am_scan_merge: skip {p}: {e}\n")
        return {"stations": [], "antennas": []}


def main():
    *ins, out = sys.argv[1:]
    merged = {}            # freq_khz -> {ant_key: snr_db}
    seen = []
    for p in ins:
        d = load(p)
        for a in d.get("antennas", []):
            k = norm(a.split()[-1])
            if k not in seen:
                seen.append(k)
        for s in d.get("stations", []):
            f = s.get("freq_khz")
            if f is None:
                continue
            ba = merged.setdefault(f, {})
            if s.get("by_antenna"):
                for k, v in s["by_antenna"].items():
                    ba[norm(k)] = v
            elif s.get("antenna") and s.get("snr_db") is not None:
                ba[norm(s["antenna"].split()[-1])] = s["snr_db"]

    stations = []
    for f, ba in merged.items():
        if not ba:
            continue
        best_key = max(ba, key=lambda k: ba[k])
        stations.append({
            "freq_khz": f,
            "snr_db": round(ba[best_key], 1),
            "antenna": ANT_FULL.get(best_key, best_key),
            "by_antenna": {k: round(v, 1) for k, v in ba.items()},
        })
    stations.sort(key=lambda s: -s["snr_db"])

    payload = {
        "scanned_at": datetime.now().isoformat(timespec="seconds"),
        "antennas": seen,
        "stations": stations,
    }
    with open(out, "w") as fh:
        json.dump(payload, fh, indent=2)
    sys.stderr.write(f"am_scan_merge: {len(stations)} stations across {seen} -> {out}\n")


if __name__ == "__main__":
    main()
