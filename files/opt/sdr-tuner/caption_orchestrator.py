#!/usr/bin/env python3
"""
Caption orchestrator for SDR tuner.

Lyrics sources, in priority order:
  1. RDS metadata (artist/title parsed by rds_watcher.py from broadcast RDS RT)
     -> direct LRClib lookup. Most reliable for stations that broadcast track info.
  2. Audio fingerprinting (fpcalc -> AcoustID -> LRClib). Fallback for stations
     without RDS track metadata. Less reliable on FM due to broadcast processing.

Captions:
  - Continuously sends short PCM chunks from local Icecast to a remote Whisper
    service. Captions are suppressed while in lyrics mode.
"""
import json, os, re, subprocess, sys, tempfile, threading, time, wave
from pathlib import Path

import requests

ICECAST_URL    = os.environ["ICECAST_URL"]
WHISPER_URL    = os.environ["WHISPER_URL"]
WHISPER_TOKEN  = os.environ["WHISPER_TOKEN"]
ACOUSTID_KEY   = os.environ.get("ACOUSTID_KEY", "")
STATE_PATH     = Path(os.environ["STATE_PATH"])
NOW_PLAYING    = Path(os.environ.get("NOW_PLAYING_PATH",
                       "/run/sdr-streams/now_playing.json"))
LRC_OFFSET_MS  = int(os.environ.get("LRC_OFFSET_MS",     "-6000"))
RDS_OFFSET_MS  = int(os.environ.get("RDS_OFFSET_MS",     "5000"))

SAMPLE_RATE          = 16000
CHANNELS             = 1
BYTES_PER_SAMPLE     = 2
WHISPER_WINDOW_SEC   = 6
FINGERPRINT_EVERY    = 25
FINGERPRINT_DUR_SEC  = 15
RDS_POLL_SEC         = 2
RING_SEC             = 60

state = {
    "mode": "idle",
    "caption_text": "",
    "caption_updated": 0,
    "song": None,
    "lyrics_lines": [],
    "lyrics_index": -1,
    "last_update": 0,
}
slock = threading.Lock()


def write_state():
    with slock:
        snap = dict(state)
        snap["last_update"] = time.time()
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(snap, ensure_ascii=False))
    tmp.replace(STATE_PATH)


def reset_song():
    with slock:
        state["song"] = None
        state["lyrics_lines"] = []
        state["lyrics_index"] = -1
        if state["mode"] == "lyrics":
            state["mode"] = "captions" if state["caption_text"] else "idle"


class Ring:
    def __init__(self, seconds):
        self.cap = seconds * SAMPLE_RATE * BYTES_PER_SAMPLE
        self.buf = bytearray()
        self.lock = threading.Lock()

    def append(self, data):
        with self.lock:
            self.buf.extend(data)
            over = len(self.buf) - self.cap
            if over > 0:
                del self.buf[:over]

    def last(self, seconds):
        n = seconds * SAMPLE_RATE * BYTES_PER_SAMPLE
        with self.lock:
            return bytes(self.buf[-n:]) if len(self.buf) >= n else None

ring = Ring(RING_SEC)


def reader_loop():
    while True:
        proc = None
        try:
            proc = subprocess.Popen(
                ["ffmpeg", "-hide_banner", "-loglevel", "error",
                 "-reconnect", "1", "-reconnect_streamed", "1",
                 "-reconnect_delay_max", "5",
                 "-i", ICECAST_URL,
                 "-ac", str(CHANNELS), "-ar", str(SAMPLE_RATE),
                 "-f", "s16le", "-"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
            while True:
                chunk = proc.stdout.read(SAMPLE_RATE * BYTES_PER_SAMPLE)
                if not chunk:
                    break
                ring.append(chunk)
        except Exception as e:
            print(f"[reader] {e}", file=sys.stderr)
        finally:
            if proc:
                try: proc.kill()
                except Exception: pass
        time.sleep(3)


def transcribe_loop():
    sess = requests.Session()
    while True:
        time.sleep(WHISPER_WINDOW_SEC)
        with slock:
            in_lyrics = state["mode"] == "lyrics"
        if in_lyrics:
            continue
        pcm = ring.last(WHISPER_WINDOW_SEC)
        if not pcm:
            continue
        try:
            r = sess.post(
                f"{WHISPER_URL}/transcribe",
                files={"audio": ("chunk.pcm", pcm, "application/octet-stream")},
                headers={"Authorization": f"Bearer {WHISPER_TOKEN}"},
                timeout=30)
            if not r.ok:
                print(f"[whisper] HTTP {r.status_code}", file=sys.stderr)
                continue
            text = (r.json().get("text") or "").strip()
        except requests.RequestException as e:
            print(f"[whisper] {e}", file=sys.stderr)
            continue
        if not text:
            continue
        with slock:
            state["caption_text"]    = text
            state["caption_updated"] = time.time()
            if state["mode"] == "idle":
                state["mode"] = "captions"
        write_state()


def lrclib_get(artist, title, duration=None):
    params = {"artist_name": artist, "track_name": title}
    if duration:
        params["duration"] = int(duration)
    try:
        r = requests.get("https://lrclib.net/api/get",
                         params=params, timeout=10,
                         headers={"User-Agent": "sdr-tuner/1.0"})
    except requests.RequestException as e:
        print(f"[lrclib] {e}", file=sys.stderr)
        return None
    if r.status_code == 404:
        return None
    if not r.ok:
        print(f"[lrclib] HTTP {r.status_code}", file=sys.stderr)
        return None
    try:
        return r.json()
    except ValueError:
        return None


LRC_RE = re.compile(r"\[(\d+):(\d+)(?:\.(\d+))?\](.*)")
def parse_lrc(synced):
    if not synced:
        return []
    lines = []
    for raw in synced.splitlines():
        m = LRC_RE.match(raw)
        if not m:
            continue
        mm, ss, frac, txt = m.groups()
        ms = int(mm) * 60_000 + int(ss) * 1000
        if frac:
            ms += int(frac.ljust(3, "0")[:3])
        lines.append({"time_ms": ms, "text": txt.strip()})
    lines.sort(key=lambda x: x["time_ms"])
    return lines


def apply_song(artist, title, duration, source, score=None):
    lrc = lrclib_get(artist, title, duration)
    lines = parse_lrc(lrc.get("syncedLyrics")) if lrc else []
    has_lyrics = bool(lines)
    print(f"[match:{source}] {artist} - {title} "
          f"(score={score}, lyrics={'yes' if has_lyrics else 'no'})",
          file=sys.stderr)
    with slock:
        state["song"] = {
            "artist":     artist,
            "title":      title,
            "duration":   duration or (lrc.get("duration") if lrc else None),
            "matched_at": time.time(),
            "source":     source,
            "score":      score,
        }
        state["lyrics_lines"] = lines
        state["lyrics_index"] = -1
        if has_lyrics:
            state["mode"] = "lyrics"
        elif state["caption_text"]:
            state["mode"] = "captions"
        else:
            state["mode"] = "idle"
    write_state()
    return True


def rds_lyrics_loop():
    last_seen = (None, None)
    while True:
        time.sleep(RDS_POLL_SEC)
        try:
            np = json.loads(NOW_PLAYING.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            continue
        artist = (np.get("artist") or "").strip()
        title  = (np.get("title")  or "").strip()
        if not (artist and title):
            continue
        if (artist, title) == last_seen:
            continue
        last_seen = (artist, title)
        with slock:
            cur = state.get("song") or {}
            same = (cur.get("artist") == artist and cur.get("title") == title)
        if same:
            continue
        try:
            apply_song(artist, title, duration=None, source="rds", score=None)
        except Exception as e:
            print(f"[rds_lyrics] {e}", file=sys.stderr)


def fpcalc(wav_path):
    out = subprocess.check_output(
        ["fpcalc", "-json", str(wav_path)], text=True, timeout=15)
    return json.loads(out)


def acoustid_lookup(duration, fp):
    if not ACOUSTID_KEY:
        return None
    try:
        r = requests.get("https://api.acoustid.org/v2/lookup", params={
            "client": ACOUSTID_KEY,
            "meta": "recordings",
            "duration": int(duration),
            "fingerprint": fp,
        }, timeout=10)
    except requests.RequestException as e:
        print(f"[acoustid] {e}", file=sys.stderr)
        return None
    if not r.ok:
        print(f"[acoustid] HTTP {r.status_code}", file=sys.stderr)
        return None
    data = r.json()
    if data.get("status") != "ok":
        return None
    best = None
    for res in data.get("results", []):
        score = res.get("score", 0)
        for rec in res.get("recordings", []) or []:
            t       = rec.get("title")
            artists = rec.get("artists") or []
            if not (t and artists):
                continue
            cand = {
                "artist":   artists[0]["name"],
                "title":    t,
                "duration": rec.get("duration"),
                "score":    score,
            }
            if best is None or cand["score"] > best["score"]:
                best = cand
    return best


def write_wav(pcm_bytes, path):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(CHANNELS)
        w.setsampwidth(BYTES_PER_SAMPLE)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm_bytes)


def fingerprint_loop():
    while True:
        time.sleep(FINGERPRINT_EVERY)
        with slock:
            song = state.get("song")
        if song:
            elapsed = time.time() - song["matched_at"]
            if song.get("duration") and elapsed < song["duration"] - 30:
                continue
            if song.get("source") == "rds" and elapsed < 60:
                continue

        pcm = ring.last(FINGERPRINT_DUR_SEC)
        if not pcm:
            continue
        with tempfile.TemporaryDirectory() as td:
            wav = Path(td) / "chunk.wav"
            try:
                write_wav(pcm, wav)
                fp = fpcalc(wav)
            except Exception as e:
                print(f"[fpcalc] {e}", file=sys.stderr)
                continue

        match = acoustid_lookup(fp.get("duration"), fp.get("fingerprint"))
        if not match:
            continue
        if match["score"] < 0.6:
            print(f"[acoustid] low score {match['score']:.2f} for "
                  f"{match['artist']} - {match['title']}, ignoring",
                  file=sys.stderr)
            continue
        try:
            apply_song(match["artist"], match["title"],
                       duration=match.get("duration"),
                       source="acoustid",
                       score=match["score"])
        except Exception as e:
            print(f"[fingerprint] {e}", file=sys.stderr)


def lyrics_tick_loop():
    while True:
        time.sleep(0.4)
        changed = False
        with slock:
            if state["mode"] == "lyrics":
                song   = state["song"] or {}
                lines  = state["lyrics_lines"]
                source = song.get("source", "acoustid")
                offset = RDS_OFFSET_MS if source == "rds" else LRC_OFFSET_MS
                cur_ms = (time.time() - song.get("matched_at", 0)) * 1000 + offset
                idx = -1
                for i, ln in enumerate(lines):
                    if ln["time_ms"] <= cur_ms:
                        idx = i
                    else:
                        break
                if idx != state["lyrics_index"]:
                    state["lyrics_index"] = idx
                    changed = True
                dur = song.get("duration")
                if dur and (cur_ms / 1000) > dur + 10:
                    state["song"] = None
                    state["lyrics_lines"] = []
                    state["lyrics_index"] = -1
                    state["mode"] = "captions" if state["caption_text"] else "idle"
                    changed = True
        if changed:
            write_state()


def tune_watcher_loop():
    last_seen = NOW_PLAYING.exists()
    while True:
        time.sleep(1)
        exists = NOW_PLAYING.exists()
        if last_seen and not exists:
            print("[tune] now_playing cleared, resetting song state",
                  file=sys.stderr)
            reset_song()
            with slock:
                state["caption_text"] = ""
            write_state()
        last_seen = exists


def main():
    write_state()
    threads = [
        threading.Thread(target=reader_loop,        daemon=True),
        threading.Thread(target=transcribe_loop,    daemon=True),
        threading.Thread(target=rds_lyrics_loop,    daemon=True),
        threading.Thread(target=fingerprint_loop,   daemon=True),
        threading.Thread(target=lyrics_tick_loop,   daemon=True),
        threading.Thread(target=tune_watcher_loop,  daemon=True),
    ]
    for t in threads:
        t.start()
    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
