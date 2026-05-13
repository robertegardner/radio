#!/usr/bin/env python3
"""Read redsea JSON from stdin, maintain current state in
/run/sdr-streams/now_playing.json"""
import json, os, re, sys, time
from pathlib import Path

OUT = Path("/run/sdr-streams/now_playing.json")
OUT.parent.mkdir(parents=True, exist_ok=True)

state = {
    "pi": None, "ps": None, "rt": None,
    "prog_type": None, "artist": None, "title": None,
    "freq_mhz": os.environ.get("FREQ", "").rstrip("M"),
    "started_at": time.time(),
    "last_update": time.time(),
}


def write_state():
    state["last_update"] = time.time()
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(OUT)


PREFIX_RE = re.compile(
    r'^(?:Now Playing:?|Now:|NP:|On Air:?|Currently Playing:?|Playing:?)\s*',
    re.I,
)
STATION_TAIL_RE = re.compile(
    r'\s*(?:on|@|[|\-])\s*(?:\d+\.?\d*\s*[A-Z][A-Z0-9 .]{1,15}'
    r'|[A-Z]{3,5}\s*\d+\.?\d*'
    r'|\d+\.?\d*\s*FM)\s*$',
    re.I,
)


def parse_rt(rt):
    """Best-effort extract artist/title from unstructured RT.

    Handles:
      - "Artist - Title"
      - "Now Playing: Artist - Title"
      - "Artist with Title on STATION"
      - "Artist | Title"  /  "Artist / Title"
      - "Title by Artist"
    """
    if not rt:
        return None, None
    s = PREFIX_RE.sub('', rt.strip())

    if re.search(r'\bwith\b', s, re.I):
        idx_with = re.search(r'\s+with\s+', s, re.I)
        if idx_with:
            artist = s[:idx_with.start()].strip()
            rest   = s[idx_with.end():].strip()
            idx_on = rest.lower().rfind(' on ')
            title  = rest[:idx_on].strip() if idx_on > 0 else rest
            if artist and title:
                return artist, title

    s = STATION_TAIL_RE.sub('', s).strip()

    for sep in (' - ', ' | ', ' / ', ' :: '):
        if sep in s:
            a, t = s.split(sep, 1)
            a, t = a.strip(), t.strip()
            if a and t:
                return a, t

    m = re.match(r'^(.+?)\s+by\s+(.+)$', s, re.I)
    if m:
        return m.group(2).strip(), m.group(1).strip()

    return None, None


write_state()

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        rec = json.loads(line)
    except json.JSONDecodeError:
        continue

    changed = False
    for k in ("pi", "ps", "prog_type"):
        v = rec.get(k)
        if v and state.get(k) != v:
            state[k] = v.strip() if isinstance(v, str) else v
            changed = True

    rt = rec.get("rt") or rec.get("radiotext")
    if rt and rt.strip() and state.get("rt") != rt.strip():
        state["rt"] = rt.strip()
        a, t = parse_rt(rt)
        state["artist"] = a
        state["title"]  = t
        changed = True

    rtplus = rec.get("radiotext_plus") or rec.get("rt_plus")
    if rtplus:
        for tag in rtplus.get("tags", []):
            ct = tag.get("content-type") or tag.get("content_type")
            data = (tag.get("data") or "").strip()
            if not data:
                continue
            if ct == "item.title" and data != state.get("title"):
                state["title"] = data
                changed = True
            elif ct == "item.artist" and data != state.get("artist"):
                state["artist"] = data
                changed = True

    if changed:
        write_state()
