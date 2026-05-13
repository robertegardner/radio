#!/usr/bin/env python3
"""Fetch broadcast station data from RadioBrowser and write fcc.json.

RadioBrowser (https://www.radio-browser.info) is a community-maintained
catalog of radio stations. Many entries are simulcasts of real broadcast
stations and include the broadcast frequency in their tags or name.

The output file is named fcc.json for compatibility with station_db.py;
the data source is RadioBrowser.
"""
import argparse
import json
import math
import re
import socket
import sys
from datetime import datetime
from pathlib import Path
from urllib.error import URLError, HTTPError
from urllib.request import Request, urlopen

UA = "sdr-tuner/1.0"
HARDCODED_SERVERS = [
    "de1.api.radio-browser.info",
    "at1.api.radio-browser.info",
    "fi1.api.radio-browser.info",
    "nl1.api.radio-browser.info",
]

FM_RE = re.compile(
    r'(?:^|[^\d.])(\d{2,3}\.\d)\s*(?:FM\b|MHz\b)|'
    r'\bFM\s*(\d{2,3}\.\d)|'
    r'(?:^|[^\d.])(8[7-9]|9\d|10[0-7])\.([0-9])(?:\D|$)',
    re.I,
)
AM_RE = re.compile(
    r'(?:^|[^\d.])(\d{3,4})\s*(?:AM\b|kHz\b)|'
    r'\bAM\s*(\d{3,4})\b',
    re.I,
)


def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = (math.sin(dp / 2) ** 2 +
         math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))


def resolve_servers():
    servers = []
    try:
        for entry in socket.getaddrinfo("all.api.radio-browser.info", None,
                                        proto=socket.IPPROTO_TCP):
            ip = entry[4][0]
            try:
                host = socket.gethostbyaddr(ip)[0]
                if host not in servers:
                    servers.append(host)
            except (socket.herror, OSError):
                pass
    except (socket.gaierror, OSError) as e:
        print(f"[radio-browser] DNS lookup failed: {e}", file=sys.stderr)
    if not servers:
        servers = HARDCODED_SERVERS[:]
    print(f"[radio-browser] candidate servers: {servers}", file=sys.stderr)
    return servers


def fetch_json(url, timeout=30):
    req = Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_us_stations(servers):
    path = "/json/stations/search?countrycode=US&hasGeoInfo=true&limit=100000"
    last_err = None
    for srv in servers:
        url = f"https://{srv}{path}"
        try:
            print(f"[radio-browser] GET {url}", file=sys.stderr)
            data = fetch_json(url, timeout=60)
            if isinstance(data, list) and data:
                print(f"[radio-browser]   got {len(data)} stations",
                      file=sys.stderr)
                return data
            print(f"[radio-browser]   empty response, trying next",
                  file=sys.stderr)
        except (URLError, HTTPError, socket.timeout, json.JSONDecodeError,
                TimeoutError) as e:
            last_err = e
            print(f"[radio-browser]   failed: {e}", file=sys.stderr)
            continue
    raise RuntimeError(f"all RadioBrowser servers failed; last error: {last_err}")


def extract_call(name):
    if not name:
        return None
    m = re.search(r'\b([KW][A-Z]{2,3})\b', name.upper())
    return m.group(1) if m else None


def extract_freqs(name, tags):
    blob = f"{name or ''} {tags or ''}"
    mhz = khz = None

    for m in FM_RE.finditer(blob):
        if m.group(1):
            try:
                v = float(m.group(1))
                if 87.0 <= v <= 108.0:
                    mhz = round(v, 1); break
            except ValueError: pass
        elif m.group(2):
            try:
                v = float(m.group(2))
                if 87.0 <= v <= 108.0:
                    mhz = round(v, 1); break
            except ValueError: pass
        elif m.group(3) and m.group(4):
            try:
                v = float(f"{m.group(3)}.{m.group(4)}")
                if 87.0 <= v <= 108.0:
                    mhz = round(v, 1); break
            except ValueError: pass

    for m in AM_RE.finditer(blob):
        for g in (m.group(1), m.group(2)):
            if not g:
                continue
            try:
                v = int(g)
                if 530 <= v <= 1710:
                    khz = v; break
            except ValueError:
                continue
        if khz:
            break

    return mhz, khz


def _state_to_abbrev(name):
    table = {
        "alabama":"AL","alaska":"AK","arizona":"AZ","arkansas":"AR",
        "california":"CA","colorado":"CO","connecticut":"CT","delaware":"DE",
        "florida":"FL","georgia":"GA","hawaii":"HI","idaho":"ID",
        "illinois":"IL","indiana":"IN","iowa":"IA","kansas":"KS",
        "kentucky":"KY","louisiana":"LA","maine":"ME","maryland":"MD",
        "massachusetts":"MA","michigan":"MI","minnesota":"MN",
        "mississippi":"MS","missouri":"MO","montana":"MT","nebraska":"NE",
        "nevada":"NV","new hampshire":"NH","new jersey":"NJ",
        "new mexico":"NM","new york":"NY","north carolina":"NC",
        "north dakota":"ND","ohio":"OH","oklahoma":"OK","oregon":"OR",
        "pennsylvania":"PA","rhode island":"RI","south carolina":"SC",
        "south dakota":"SD","tennessee":"TN","texas":"TX","utah":"UT",
        "vermont":"VT","virginia":"VA","washington":"WA",
        "west virginia":"WV","wisconsin":"WI","wyoming":"WY",
        "district of columbia":"DC",
    }
    return table.get((name or "").strip().lower(), "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lat", type=float, default=37.31,
                    help="Origin latitude (default: Cape Girardeau, MO)")
    ap.add_argument("--lon", type=float, default=-89.55,
                    help="Origin longitude")
    ap.add_argument("--max-km", type=float, default=400)
    ap.add_argument("--out", default="/var/lib/sdr-streams/fcc.json")
    args = ap.parse_args()

    origin = (args.lat, args.lon)
    print(f"[radio-browser] origin={origin}, max_km={args.max_km}",
          file=sys.stderr)

    servers = resolve_servers()
    raw = fetch_us_stations(servers)

    am = {}; fm = {}
    skipped_no_freq = 0
    skipped_far = 0

    for s in raw:
        name = s.get("name", "")
        tags = s.get("tags", "")
        state = s.get("state", "") or ""
        try:
            lat = float(s.get("geo_lat") or 0)
            lon = float(s.get("geo_long") or 0)
        except (TypeError, ValueError):
            continue
        if not (lat or lon):
            continue

        dist = haversine_km(origin[0], origin[1], lat, lon)
        if dist > args.max_km:
            skipped_far += 1
            continue

        mhz, khz = extract_freqs(name, tags)
        if not mhz and not khz:
            skipped_no_freq += 1
            continue

        call = extract_call(name)
        city = ""
        m = re.search(r'\b([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)*),\s*([A-Z]{2})\b',
                      name)
        if m:
            city = m.group(1)
            if not state:
                state = m.group(2)

        st_abbrev = state if len(state) == 2 else _state_to_abbrev(state)

        record = {
            "call":  call or "",
            "city":  city,
            "state": st_abbrev,
            "name":  name.strip(),
            "lat":   lat, "lon": lon,
            "dist_km": round(dist, 1),
        }

        if mhz:
            key = f"{mhz:.1f}"
            existing = fm.get(key)
            if not existing or existing["dist_km"] > dist:
                fm[key] = {**record, "service": "FM"}
        if khz:
            key = str(khz)
            existing = am.get(key)
            if not existing or existing["dist_km"] > dist:
                am[key] = {**record, "service": "AM"}

    print(f"[radio-browser] kept FM={len(fm)} AM={len(am)} "
          f"(skipped {skipped_far} far, {skipped_no_freq} no-freq)",
          file=sys.stderr)

    out = {
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "origin":     list(origin),
        "max_km":     args.max_km,
        "source":     "radio-browser",
        "am": am,
        "fm": fm,
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=2))
    print(f"[radio-browser] wrote {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
