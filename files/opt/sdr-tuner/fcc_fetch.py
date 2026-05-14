#!/usr/bin/env python3
"""Fetch broadcast station data from FCC CDBS and write fcc.json.

Downloads four CDBS bulk files to build an authoritative station database
with real transmitter coordinates, then filters by distance from the origin.

Data sources:
  facility.dat      - call signs, frequencies, cities, states (all services)
  fm_eng_data.dat   - FM transmitter lat/lon (joined by facility_id)
  am_ant_sys.dat    - AM transmitter lat/lon (joined via application_id)
  application.dat   - application_id → facility_id map (for AM join)

CDBS files are at: https://transition.fcc.gov/Bureaus/MB/Databases/cdbs/
Note: CDBS was frozen for new applications in October 2023 (FCC moved new
filings to LMS). All existing licensed stations remain in CDBS with
accurate engineering data. New stations licensed after Oct 2023 may be
absent; for a hobby project in a small market this is acceptable.

Output format is compatible with station_db.py.
"""

import argparse
import io
import json
import math
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

CDBS_BASE = "https://transition.fcc.gov/ftp/Bureaus/MB/Databases/cdbs"
UA = "sdr-tuner/1.0"
CACHE_MAX_AGE_DAYS = 6

_FILES = {
    "facility":    "facility.zip",
    "fm_eng":      "fm_eng_data.zip",
    "am_ant":      "am_ant_sys.zip",
    "application": "application.zip",
}


def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _fetch(url, dest, force):
    if not force and dest.exists():
        age = (datetime.now() - datetime.fromtimestamp(dest.stat().st_mtime)).days
        if age < CACHE_MAX_AGE_DAYS:
            print(f"[cdbs] cache ok ({age}d old): {dest.name}", file=sys.stderr)
            return
    print(f"[cdbs] downloading {url}", file=sys.stderr)
    req = Request(url, headers={"User-Agent": UA})
    try:
        with urlopen(req, timeout=120) as r:
            data = r.read()
    except (URLError, OSError) as e:
        raise RuntimeError(f"download failed: {url}: {e}") from e
    dest.write_bytes(data)
    print(f"[cdbs]   {dest.name}: {len(data) // 1024} KB", file=sys.stderr)


def _rows(zip_path):
    """Yield field lists for each record in the first file of a CDBS zip.

    CDBS format: pipe-delimited rows ending with |^| (record-end marker).
    No header row — schema is positional per the CDBS readme.
    """
    with zipfile.ZipFile(zip_path) as zf:
        inner = zf.namelist()[0]
        with zf.open(inner) as f:
            for line in io.TextIOWrapper(f, encoding="latin-1"):
                row = line.rstrip("\n\r").rstrip("|^")
                if row:
                    yield row.split("|")


def _biased(blat_str, blon_str):
    """Decode CDBS biased coordinates to WGS84 (lat, lon) decimal degrees.

    CDBS stores: biased_lat = lat + 90, biased_long = 180 + west_longitude.
    Invert: lat = biased_lat - 90; lon = 180 - biased_long (negative = West).
    """
    try:
        lat = float(blat_str) - 90.0
        lon = 180.0 - float(blon_str)
        if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
            return round(lat, 4), round(lon, 4)
    except (ValueError, TypeError):
        pass
    return None, None


def _load_facilities(path):
    """Return {facility_id: record} for US FM/AM licensed stations.

    facility.dat columns (0-indexed, no header):
      0=comm_city  1=comm_state  5=fac_callsign  7=fac_city
      8=fac_country  9=fac_frequency  10=fac_service  11=fac_state
      14=facility_id  16=fac_status
    """
    result = {}
    for p in _rows(path):
        if len(p) < 17:
            continue
        if p[8] != "US":
            continue
        service = p[10]
        if service not in ("FM", "AM"):
            continue
        if p[16] != "LICEN":
            continue
        try:
            fac_id = int(p[14])
            freq = float(p[9])
        except ValueError:
            continue
        if fac_id <= 0 or freq <= 0:
            continue
        city = (p[0] or p[7]).strip().title()
        state = (p[1] or p[11]).strip().upper()
        call = p[5].strip()
        rec = {"call": call, "city": city, "state": state, "service": service}
        if service == "FM":
            rec["freq_mhz"] = round(freq, 1)
        else:
            rec["freq_khz"] = int(freq)
        result[fac_id] = rec
    return result


def _load_fm_coords(path):
    """Return {facility_id: (lat, lon)} from fm_eng_data.dat.

    Columns: 11=biased_lat  12=biased_long  19=eng_record_type  20=facility_id
    Only current records (eng_record_type='C').
    """
    coords = {}
    for p in _rows(path):
        if len(p) < 21 or p[19] != "C":
            continue
        try:
            fac_id = int(p[20])
        except ValueError:
            continue
        if fac_id in coords:
            continue
        lat, lon = _biased(p[11], p[12])
        if lat is not None:
            coords[fac_id] = (lat, lon)
    return coords


def _load_app_map(path):
    """Return {application_id: facility_id} from application.dat.

    Columns: 2=application_id  3=facility_id
    """
    mapping = {}
    for p in _rows(path):
        if len(p) < 4:
            continue
        try:
            app_id, fac_id = int(p[2]), int(p[3])
        except ValueError:
            continue
        if app_id > 0 and fac_id > 0:
            mapping[app_id] = fac_id
    return mapping


def _load_am_coords(path, app_map):
    """Return {facility_id: (lat, lon)} from am_ant_sys.dat.

    Columns: 2=application_id  27=eng_record_type  28=biased_lat  29=biased_long
    Only current records (eng_record_type='C').
    """
    coords = {}
    for p in _rows(path):
        if len(p) < 30 or p[27] != "C":
            continue
        try:
            app_id = int(p[2])
        except ValueError:
            continue
        fac_id = app_map.get(app_id)
        if fac_id is None or fac_id in coords:
            continue
        lat, lon = _biased(p[28], p[29])
        if lat is not None:
            coords[fac_id] = (lat, lon)
    return coords


def main():
    ap = argparse.ArgumentParser(
        description="Build fcc.json from FCC CDBS bulk data files."
    )
    ap.add_argument("--lat", type=float, default=37.31,
                    help="Origin latitude (default: Cape Girardeau, MO)")
    ap.add_argument("--lon", type=float, default=-89.55,
                    help="Origin longitude")
    ap.add_argument("--max-km", type=float, default=400)
    ap.add_argument("--out", default="/var/lib/sdr-streams/fcc.json")
    ap.add_argument("--cache-dir", default="/var/lib/sdr-streams/cdbs-cache",
                    help="Directory to cache downloaded CDBS zip files")
    ap.add_argument("--no-cache", action="store_true",
                    help="Re-download CDBS files even if cached and fresh")
    args = ap.parse_args()

    origin = (args.lat, args.lon)
    cache = Path(args.cache_dir)
    cache.mkdir(parents=True, exist_ok=True)

    paths = {}
    for key, filename in _FILES.items():
        dest = cache / filename
        _fetch(f"{CDBS_BASE}/{filename}", dest, force=args.no_cache)
        paths[key] = dest

    print("[cdbs] parsing facility.dat ...", file=sys.stderr)
    facilities = _load_facilities(paths["facility"])
    print(f"[cdbs]   {len(facilities)} US FM/AM licensed stations", file=sys.stderr)

    print("[cdbs] parsing fm_eng_data.dat ...", file=sys.stderr)
    fm_coords = _load_fm_coords(paths["fm_eng"])
    print(f"[cdbs]   {len(fm_coords)} FM transmitter locations", file=sys.stderr)

    print("[cdbs] parsing application.dat ...", file=sys.stderr)
    app_map = _load_app_map(paths["application"])
    print(f"[cdbs]   {len(app_map)} application records", file=sys.stderr)

    print("[cdbs] parsing am_ant_sys.dat ...", file=sys.stderr)
    am_coords = _load_am_coords(paths["am_ant"], app_map)
    print(f"[cdbs]   {len(am_coords)} AM transmitter locations", file=sys.stderr)

    fm = {}
    am = {}
    no_coords = 0
    too_far = 0

    for fac_id, info in facilities.items():
        coord_map = fm_coords if info["service"] == "FM" else am_coords
        if fac_id not in coord_map:
            no_coords += 1
            continue
        lat, lon = coord_map[fac_id]
        dist = haversine_km(origin[0], origin[1], lat, lon)
        if dist > args.max_km:
            too_far += 1
            continue
        rec = {
            "call":    info["call"],
            "city":    info["city"],
            "state":   info["state"],
            "lat":     lat,
            "lon":     lon,
            "dist_km": round(dist, 1),
            "service": info["service"],
        }
        if info["service"] == "FM":
            key = f"{info['freq_mhz']:.1f}"
            existing = fm.get(key)
            if not existing or existing["dist_km"] > dist:
                fm[key] = rec
        else:
            key = str(info["freq_khz"])
            existing = am.get(key)
            if not existing or existing["dist_km"] > dist:
                am[key] = rec

    print(
        f"[cdbs] result: FM={len(fm)} AM={len(am)} "
        f"(skipped {too_far} too far, {no_coords} no coords)",
        file=sys.stderr,
    )

    out = {
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "origin":     list(origin),
        "max_km":     args.max_km,
        "source":     "cdbs",
        "am":         am,
        "fm":         fm,
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out, indent=2))
    tmp.replace(out_path)
    print(f"[cdbs] wrote {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
