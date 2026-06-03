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
import collections, json, os, re, subprocess, sys, tempfile, threading, time, wave
from pathlib import Path

import requests

ICECAST_URL    = os.environ["ICECAST_URL"]
WHISPER_URL    = os.environ["WHISPER_URL"]
WHISPER_TOKEN  = os.environ["WHISPER_TOKEN"]
ACOUSTID_KEY   = os.environ.get("ACOUSTID_KEY", "")
GENIUS_TOKEN   = os.environ.get("GENIUS_TOKEN", "")
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
FINGERPRINT_DUR_SEC  = 20      # longer sample -> more robust fingerprint on FM
RDS_POLL_SEC         = 2
RING_SEC             = 60

# Confidence-ranked identification. A candidate replaces what we show only when
# it improves on it: a higher-confidence read of the same song (upgrade) or a
# stronger / non-stale different song. So RDS (authoritative) is never clobbered
# by a weaker fingerprint guess, while weak guesses get corrected as better data
# arrives.
CONF_RDS             = 1.0     # station-provided artist/title is authoritative
CONF_LYRICS          = 0.70    # Whisper transcript -> lyric search (when enabled)
ACOUSTID_FLOOR       = 0.50    # accept fingerprints >= this (was a hard 0.6 cut)
ACOUSTID_GOOD        = 0.85    # at/above this we stop re-fingerprinting to upgrade

# Lyric-based ID: Whisper transcript -> Genius search -> LRClib verification.
LYRIC_ID_EVERY       = 15      # seconds between lyric-ID attempts
LYRIC_WINDOW_SEC     = 45      # how much recent transcript to consider
LYRIC_MIN_WORDS      = 6       # need at least this much transcript to bother
LYRIC_QUERY_WORDS    = 20      # most-recent words used as the Genius query
LYRIC_VERIFY_MIN     = 0.15    # min transcript/lyrics trigram overlap to trust a hit

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

# Rolling Whisper transcript (timestamp, text) for lyric-based identification.
transcript_buf = collections.deque(maxlen=16)
transcript_lock = threading.Lock()


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
        with transcript_lock:
            transcript_buf.append((time.time(), text))


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


_art_cache = {}


def fetch_art(artist, title):
    """Best-effort cover art + album for a track, via the iTunes Search API
    (no key needed). Returns (art_url, album) or (None, None). Cached by track
    so we don't re-hit the network on every re-match of the same song."""
    key = f"{artist}\t{title}"
    if key in _art_cache:
        return _art_cache[key]
    art_url = album = None
    try:
        r = requests.get(
            "https://itunes.apple.com/search",
            params={"term": f"{artist} {title}", "media": "music",
                    "entity": "song", "limit": 1},
            timeout=8, headers={"User-Agent": "sdr-tuner/1.0"})
        if r.ok:
            results = r.json().get("results") or []
            if results:
                raw = results[0].get("artworkUrl100")
                if raw:
                    # iTunes returns a 100px thumb; request a larger render.
                    art_url = raw.replace("100x100bb", "600x600bb")
                album = results[0].get("collectionName")
    except (requests.RequestException, ValueError) as e:
        print(f"[art] {e}", file=sys.stderr)
    _art_cache[key] = (art_url, album)
    return art_url, album


# ---------------------------------------------------------------------------
# Lyric-based identification (Whisper transcript -> Genius -> LRClib verify)
# ---------------------------------------------------------------------------
_WORD_RE = re.compile(r"[a-z0-9']+")


def _words(s):
    return _WORD_RE.findall((s or "").lower())


def _trigrams(words):
    return {tuple(words[i:i + 3]) for i in range(len(words) - 2)}


def recent_transcript():
    """Recent Whisper transcript within LYRIC_WINDOW_SEC, consecutive dups dropped."""
    cutoff = time.time() - LYRIC_WINDOW_SEC
    with transcript_lock:
        texts = [t for (ts, t) in transcript_buf if ts >= cutoff]
    out = []
    for t in texts:
        if not out or out[-1] != t:
            out.append(t)
    return " ".join(out).strip()


def lyrics_overlap(transcript, lyrics):
    """Fraction of the transcript's word-trigrams that appear in the candidate
    song's lyrics — our guard against Whisper-mishears matching the wrong song."""
    tg = _trigrams(_words(transcript))
    if not tg:
        return 0.0
    lg = _trigrams(_words(lyrics))
    if not lg:
        return 0.0
    return len(tg & lg) / len(tg)


def genius_search(query):
    if not GENIUS_TOKEN:
        return None
    try:
        r = requests.get(
            "https://api.genius.com/search",
            params={"q": query}, timeout=10,
            headers={"Authorization": f"Bearer {GENIUS_TOKEN}",
                     "User-Agent": "sdr-tuner/1.0"})
    except requests.RequestException as e:
        print(f"[genius] {e}", file=sys.stderr)
        return None
    if not r.ok:
        print(f"[genius] HTTP {r.status_code}", file=sys.stderr)
        return None
    try:
        hits = r.json().get("response", {}).get("hits", [])
    except ValueError:
        return None
    for h in hits:
        if h.get("type") != "song":
            continue
        res = h.get("result") or {}
        artist = (res.get("primary_artist") or {}).get("name")
        title = res.get("title")
        if artist and title:
            return artist.strip(), title.strip()
    return None


def _song_key(artist, title):
    return ((artist or "").strip().lower(), (title or "").strip().lower())


def consider(artist, title, source, confidence, duration=None, score=None):
    """Confidence-ranked resolver — the single entry point every identification
    source funnels through. Decides whether a candidate should replace what we
    currently show:

      - same song, higher confidence  -> upgrade in place (keeps lyrics running)
      - different song, higher confidence or current one is stale (past its
        duration) -> switch
      - otherwise -> ignore (weaker than what we already have)

    This lets sources run continuously and the answer improve as better data
    arrives, without a weak guess clobbering a strong one (e.g. RDS at 1.0).
    """
    artist, title = (artist or "").strip(), (title or "").strip()
    if not (artist and title):
        return
    now = time.time()
    with slock:
        cur = state.get("song")
        cur_key  = _song_key(cur.get("artist"), cur.get("title")) if cur else None
        cur_conf = cur.get("confidence", 0.0) if cur else 0.0
        cur_at   = cur.get("matched_at", 0.0) if cur else 0.0
        cur_dur  = cur.get("duration") if cur else None
    cand_key = _song_key(artist, title)

    if cur_key == cand_key:
        if confidence > cur_conf:
            _upgrade_same(artist, title, source, score, confidence)
        return
    stale = bool(cur_dur) and (now - cur_at) > cur_dur
    if cur_key is None or stale or confidence > cur_conf:
        _apply_new(artist, title, duration, source, score, confidence)


def _apply_new(artist, title, duration, source, score, confidence):
    """Full match for a newly-identified song: synced lyrics + cover art."""
    lrc = lrclib_get(artist, title, duration)
    lines = parse_lrc(lrc.get("syncedLyrics")) if lrc else []
    has_lyrics = bool(lines)
    art_url, album = fetch_art(artist, title)
    print(f"[match:{source}] {artist} - {title} "
          f"(conf={confidence:.2f}, score={score}, "
          f"lyrics={'yes' if has_lyrics else 'no'}, "
          f"art={'yes' if art_url else 'no'})",
          file=sys.stderr)
    with slock:
        state["song"] = {
            "artist":     artist,
            "title":      title,
            "album":      album,
            "art_url":    art_url,
            "duration":   duration or (lrc.get("duration") if lrc else None),
            "matched_at": time.time(),
            "source":     source,
            "score":      score,
            "confidence": confidence,
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


def _upgrade_same(artist, title, source, score, confidence):
    """Raise confidence/source for the song we're already showing (e.g. RDS
    confirming a fingerprint guess). Fills in cover art if we didn't have it.
    Does not disturb the running lyrics."""
    with slock:
        cur = state.get("song")
        need_art = bool(cur) and not cur.get("art_url")
    art_url = album = None
    if need_art:
        art_url, album = fetch_art(artist, title)
    print(f"[upgrade:{source}] {artist} - {title} conf={confidence:.2f}", file=sys.stderr)
    with slock:
        cur = state.get("song")
        if not cur or _song_key(cur.get("artist"), cur.get("title")) != _song_key(artist, title):
            return  # song changed underneath us
        cur["source"]     = source
        cur["score"]      = score
        cur["confidence"] = confidence
        if art_url and not cur.get("art_url"):
            cur["art_url"] = art_url
            if album:
                cur["album"] = album
    write_state()


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
        try:
            consider(artist, title, source="rds", confidence=CONF_RDS)
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
            elapsed  = time.time() - song["matched_at"]
            conf     = song.get("confidence", 0.0)
            near_end = bool(song.get("duration")) and elapsed >= song["duration"] - 30
            # Rest only when we already have a solid ID that isn't ending. A
            # low-confidence ID keeps getting re-fingerprinted to improve it.
            if conf >= ACOUSTID_GOOD and not near_end:
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
        if match["score"] < ACOUSTID_FLOOR:
            print(f"[acoustid] low score {match['score']:.2f} for "
                  f"{match['artist']} - {match['title']}, ignoring",
                  file=sys.stderr)
            continue
        try:
            consider(match["artist"], match["title"],
                     source="acoustid",
                     confidence=match["score"],
                     duration=match.get("duration"),
                     score=match["score"])
        except Exception as e:
            print(f"[fingerprint] {e}", file=sys.stderr)


def lyric_id_loop():
    """Identify the song from the Whisper transcript when nothing better has.
    Genius finds a candidate; LRClib lyrics verify it before we trust it."""
    if not GENIUS_TOKEN:
        print("[lyric] disabled (no GENIUS_TOKEN)", file=sys.stderr)
        return
    while True:
        time.sleep(LYRIC_ID_EVERY)
        now = time.time()
        with slock:
            song = state.get("song")
            conf = song.get("confidence", 0.0) if song else 0.0
            stale = bool(song and song.get("duration")) and \
                (now - song.get("matched_at", 0.0)) > (song.get("duration") or 0)
        # Already have an ID at least as good as a lyric match, and it's not
        # past its length (i.e. probably still the same song) -> nothing to do.
        if song and conf >= CONF_LYRICS and not stale:
            continue

        transcript = recent_transcript()
        words = _words(transcript)
        if len(words) < LYRIC_MIN_WORDS:
            continue
        cand = genius_search(" ".join(words[-LYRIC_QUERY_WORDS:]))
        if not cand:
            continue
        artist, title = cand

        lrc = lrclib_get(artist, title)
        lyrics_text = (lrc.get("plainLyrics") or lrc.get("syncedLyrics") or "") if lrc else ""
        ov = lyrics_overlap(transcript, lyrics_text) if lyrics_text else 0.0
        if ov < LYRIC_VERIFY_MIN:
            print(f"[lyric] unverified {artist} - {title} (overlap={ov:.2f})", file=sys.stderr)
            continue
        print(f"[lyric] verified {artist} - {title} (overlap={ov:.2f})", file=sys.stderr)
        try:
            consider(artist, title, source="lyrics", confidence=CONF_LYRICS,
                     duration=lrc.get("duration") if lrc else None)
        except Exception as e:
            print(f"[lyric] {e}", file=sys.stderr)


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
            with transcript_lock:
                transcript_buf.clear()   # stale transcript would misidentify the new station
            write_state()
        last_seen = exists


def main():
    write_state()
    threads = [
        threading.Thread(target=reader_loop,        daemon=True),
        threading.Thread(target=transcribe_loop,    daemon=True),
        threading.Thread(target=rds_lyrics_loop,    daemon=True),
        threading.Thread(target=fingerprint_loop,   daemon=True),
        threading.Thread(target=lyric_id_loop,      daemon=True),
        threading.Thread(target=lyrics_tick_loop,   daemon=True),
        threading.Thread(target=tune_watcher_loop,  daemon=True),
    ]
    for t in threads:
        t.start()
    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
