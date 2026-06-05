# 2026-06-03 — Multi-source song identification + cover art

Added discrete song identification and cover art for client (Android app)
consumption, all in `caption_orchestrator.py` + `app.py`.

## API
`/api/now_playing` now exposes a top-level **`track`** object —
`{artist, title, album, art_url, source, confidence, duration, score}` or null.
It mirrors `lyrics.song` (kept for the web UI). `source` ∈ `rds | acoustid | lyrics`.

## Identification: confidence-ranked resolver
Every source funnels through `consider(artist, title, source, confidence, …)`,
which only replaces the shown track when a candidate improves on it (higher
confidence, or different song when the current is stale). So a weak guess never
clobbers a strong one, and the answer upgrades as better data arrives.

Sources:
- **RDS** (`rds_watcher.py` parses RT → artist/title) — authoritative, conf 1.0.
- **AcoustID** fingerprint — conf = score, floor 0.50, sample 20s, re-tries while
  confidence is low. NOTE: returns **zero results on processed FM audio** (tested
  on KGMO), so effectively dead on FM — kept for completeness.
- **Lyric match** — rolling Whisper transcript → Genius `/search` → **verified**
  against the candidate's LRClib lyrics (word-trigram overlap ≥ 0.15) to block
  Whisper-mishear false positives. conf 0.70. Needs `GENIUS_TOKEN` in
  `captions.env` (no-op without it).

Cover art: `fetch_art()` queries the iTunes Search API by artist+title (cached),
fills `art_url`/`album`.

Whisper tuning that mattered: `WHISPER_WINDOW_SEC` 6→10 (sparse fragments → full
lines, which the lyric path needs); hallucination filter drops Whisper's stock
non-speech phrases ("thank you", etc.) from captions + the lyric query.

## Open problem: song-boundary lifecycle
The track lands **late** (mid-song) and **persists through commercial breaks into
the next song** until something re-identifies — so it's wrong at the start of the
next song. Root cause: no reliable "song ended" signal, and duration-based expiry
is unreliable because `matched_at` is when we identified (often mid-song), not the
true song start.

Improvement ideas (not yet built; B+D look most promising):
- A. Estimate true song start (`rds.started_at` / when RDS first appeared /
  synced-lyric position) and expire at start+duration, not matched_at+duration.
- B. Treat an RDS artist/title change-or-clear as a song boundary; clear a
  non-RDS track (KGMO updates RDS per song; clearing to slogan ≈ song end).
- C. Detect ads/talk (transcript is speech / no lyric overlap with current song
  for N s) and clear the track during non-music.
- D. Continuous re-confirmation: periodically re-check the current track still
  matches (re-fingerprint / fresh transcript still overlaps its lyrics); clear if
  it stops matching for N s — catches back-to-back songs with no ad gap.
- F. Confidence decay since last confirmation.
