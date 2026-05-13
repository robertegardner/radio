# CLAUDE.md

Project memory for [Claude Code](https://code.claude.com). Read this first when
starting a new session.

## What this project is

A software-defined radio (SDR) broadcasting setup running on a Raspberry Pi.
Pairs a USB RTL-SDR dongle with Icecast streaming so you can listen to local
FM/AM radio from any browser on your network. Beyond basic streaming, the
stack decodes RDS metadata, looks up synced lyrics, runs Whisper-based live
captions for talk content, and presents a car-stereo-style web tuner UI.

**The live deployment is at `https://radio.rg2.io`** (admin) and
`https://radio.rg2.io/radio` (listener UI). Icecast is proxied behind
`https://icecast.rg2.io/fm.mp3`. All three are reverse-proxied via NPMplus.

## Where to find things

Application code lives in `files/opt/sdr-tuner/`. On the Pi it deploys to
`/opt/sdr-tuner/` (via symlink to `/srv/radio/files/opt/sdr-tuner/` so git
pulls go live immediately).

| File | What it does |
|------|--------------|
| `stream.sh` | Bash wrapper around `rtl_fm` / `redsea` / `ffmpeg`. Reads `/etc/sdr-streams/active.env` to know what to tune and how. Branches FM (with RDS) vs AM. |
| `rds_watcher.py` | Reads JSON from `redsea` on stdin, parses station name + artist/title from the unstructured RT field, writes `/run/sdr-streams/now_playing.json`. Knows several RT formats (Artist - Title, "Artist with Title on STATION", etc.) |
| `fm_scan.py` | rtl_power band sweep of 87.9–108.1 MHz, identifies stations above noise floor, optionally probes each one for RDS PS name. Writes `/var/lib/sdr-streams/stations.json`. |
| `am_scan.py` | Walks the AM band (540–1700 kHz) with rtl_fm one channel at a time using direct-sampling mode (-E direct2). Slower than rtl_power but actually works on cheap dongles. Writes `stations_am.json`. |
| `app.py` | Flask app on port 8080. Two UIs (admin at `/`, stereo at `/radio`) plus JSON APIs. |
| `caption_orchestrator.py` | Decodes Icecast audio back to PCM, sends 6s chunks to Whisper for captions, fingerprints with Chromaprint/AcoustID for lyrics, looks up synced lyrics on LRClib. Writes `/run/sdr-streams/captions.json`. |
| `station_db.py` | Lookup helper: maps frequencies to call signs/cities by consulting `fcc.json` and `overrides.json`. |
| `fcc_fetch.py` | Currently fetches from RadioBrowser (community-curated internet-radio catalog). Quality is poor — see "Known issues" below. |
| `ui_settings.py` | Persists user-configurable UI settings (stream URL, site title) to `/etc/sdr-streams/ui.json`. |
| `templates/index.html` | Admin/control UI: stations table, scan buttons, settings, RDS now-playing, captions/lyrics view. |
| `templates/radio.html` | Stereo-style UI: amber-LCD frequency display, 12 favorites (localStorage), seek/scan, browser audio playback. |

The systemd units in `files/etc/systemd/system/` are:

- `sdr-fm@.service` — templated; `sdr-fm@active.service` is the running stream
- `sdr-tuner.service` — Flask web UI
- `sdr-scan.service` / `sdr-am-scan.service` — one-shot band scans, auto-stop and resume the stream around themselves via `ExecStartPre`/`ExecStartPost`
- `sdr-captions.service` — caption orchestrator

Config files in `files/etc/sdr-streams/` are `.example` files (real ones are
on the Pi, not in git):

- `active.env` — current tune; written by Flask on every `/tune`
- `tuner.env` — Flask config (just Icecast password)
- `captions.env` — Whisper URL, AcoustID key, lyric offsets
- `overrides.json` — hand-curated station call-sign/city overrides
- `ui.json` — user-configurable UI settings (created on first save in admin)

## Design conventions

- **Python:** stdlib + Flask + requests. No frameworks beyond Flask. We try to keep dependencies minimal because this runs on a Pi.
- **State files use atomic writes:** write to `.tmp`, then `replace()`. Multiple processes read these concurrently.
- **`/run/sdr-streams/*`** is transient (tmpfs); persistent state goes in `/var/lib/sdr-streams/`.
- **The Flask app never touches the dongle directly.** It writes env files and `sudo systemctl restart`s the stream service. Hardware access lives in `stream.sh` and the scanners.
- **The `radio` user runs all services.** It has passwordless sudo for exactly these systemctl operations via `/etc/sudoers.d/sdr-tuner`. Path matters — Trixie uses `/usr/bin/systemctl`, not `/bin/systemctl`.
- **`active.env` must be writable by `radio:radio`** (mode 0660). Other env files are root-owned, read-only.
- **HTML templates over SPAs.** The admin page is server-rendered Jinja; the radio page is server-rendered shell with vanilla JS polling JSON APIs. No build step.

## How the streaming pipeline works

```
RTL-SDR dongle (USB)
    ↓
rtl_fm (FM: 171kHz IQ for RDS subcarrier; AM: 12kHz direct-sampling)
    ↓
tee (FM only)
  ├── redsea ──→ rds_watcher.py ──→ /run/sdr-streams/now_playing.json
  ↓                                          ↓
ffmpeg (de-emphasis, lowpass,            caption_orchestrator
        MP3 encode)                       (separate service, reads Icecast)
    ↓                                          ↓
Icecast (http://localhost:8000/fm.mp3)   /run/sdr-streams/captions.json
    ↓
NPMplus reverse proxy → https://icecast.rg2.io/fm.mp3
    ↓
Browser <audio> element in radio.html
```

The Flask app on port 8080 serves both UIs and the JSON APIs. The radio UI
polls `/api/now_playing` every ~1s for current RDS/captions/lyrics, and
`/api/stations` every 30s for the scanned station list. Tuning is a JSON
POST to `/api/tune` which writes `active.env` and restarts `sdr-fm@active`.

## Caption pipeline

The caption orchestrator (`caption_orchestrator.py`) is a separate service.
It pulls the Icecast stream back to PCM via ffmpeg and runs three loops in
parallel:

1. **Whisper transcription** — every 6 seconds, sends the most recent audio
   chunk to a remote Whisper FastAPI service on a GPU host. Used for talk
   content.
2. **RDS-driven lyric lookup** — watches `now_playing.json` for new
   artist/title and queries LRClib. This is the primary lyrics path because
   FM broadcast audio processing breaks Chromaprint matches.
3. **Audio fingerprinting** — fallback when RDS doesn't provide track info.
   `fpcalc` → AcoustID → LRClib. Often unreliable on FM due to compression.

State flips between three modes:
- `idle` — nothing to show
- `captions` — Whisper transcript displayed
- `lyrics` — synced LRC scrolling line-by-line

The orchestrator pauses Whisper while in `lyrics` mode (we already know what
the song is, no point transcribing it).

## Common operations

```bash
# Watch all SDR services
sudo journalctl -u sdr-tuner -u sdr-fm@active -u sdr-captions -f

# Reload code after editing
sudo systemctl restart sdr-tuner

# Restart streaming pipeline (e.g. after stream.sh changes)
sudo systemctl restart sdr-fm@active

# Re-scan FM band (also runnable from admin UI)
sudo systemctl start sdr-scan

# Check current state
cat /run/sdr-streams/now_playing.json | jq
cat /run/sdr-streams/captions.json | jq

# Fresh station database fetch
sudo -u radio python3 /opt/sdr-tuner/fcc_fetch.py --lat 37.31 --lon -89.55 --max-km 400
```

## Things to know about the dongle

The RTL2832U is fundamentally a DVB-T television tuner that the SDR community
repurposed. Two recurring footguns:

1. **The kernel's `dvb_usb_rtl28xxu` driver auto-claims the device.** It's
   blacklisted in `/etc/modprobe.d/blacklist-rtl.conf` but only takes effect
   after reboot. If you see `usb_claim_interface error -6`, that's it.
2. **AM requires direct sampling** because the R820T tuner can't go below
   ~24 MHz. Use `-E direct2` (Q-branch) in rtl_fm; this is what `am_scan.py`
   and AM tuning do. `rtl_power`'s `-D` flag in apt's build is a boolean
   that defaults to I-branch only and can't be made to do Q-branch — which
   is why we use the slower `am_scan.py` walking approach.

## The GPU host

A separate machine (NOT the Pi) runs the Whisper FastAPI service in Docker.
Source is in `scripts/whisper-svc/`. It needs an NVIDIA GPU + nvidia-container-runtime.
Token-authenticated; the Pi has the matching token in `/etc/sdr-streams/captions.env`.
This is outside the Pi's git checkout — those scripts are kept in the repo
purely for backup/disaster recovery.

If captions stop working, first check is `curl http://gpu-host:8088/health`
from the Pi.

## Known issues and open work

### High priority

- **Station database quality is poor.** `fcc_fetch.py` currently uses
  RadioBrowser, which is a catalog of internet-radio streams, not broadcast
  stations. Many entries have stream-server coordinates rather than
  transmitter coordinates, so distance-filtering produces nonsense results
  (e.g. Louisville KY stations showing up for a Cape Girardeau location).
  Real fix: rewrite against the FCC CDBS Public Database, which has
  authoritative transmitter coordinates. **This is the next non-feature task.**

### Planned features

- **HD Radio (IBOC) support** via `nrsc5`. Design agreed: per-station
  toggle (one dongle), subchannels (HD1/HD2/HD3/HD4) as first-class
  tunable stations. New `MODE=hdfm` in `active.env` plus a `HD_PROGRAM`
  field. New `hd_watcher.py` parallel to `rds_watcher.py`. HD presence
  detection added to scan results. Unknowns include Pi 5 CPU load and
  whether the FM whip antenna is strong enough to decode HD at our QTH.

- **NPMplus auth on admin endpoints.** The `/radio`, `/api/now_playing`,
  and `/api/stations` routes are public-safe. Everything else (`/`,
  `/tune`, `/scan-*`, `/settings`, `/reload-stations`) should require
  basic auth via NPMplus's Access List feature. No app changes needed.

- **Favorites sync/export.** Right now presets are in browser localStorage,
  per-device. Add export-to-URL and import-from-URL for cross-device sync.
  Implementation idea: base64-encoded JSON in a hash fragment, a "share"
  button on the radio page that copies the URL.

- **Stream recording.** Capture the current Icecast stream to a
  timestamped MP3 with a button on the admin UI. ffmpeg already in the
  toolchain. Add `/recordings/` directory served read-only by Flask.

- **Weekly cron for `fcc_fetch.py`.** Stations come and go. systemd
  timer that runs `fcc_fetch.py` weekly and calls `/reload-stations`.
  Wait until after the FCC rewrite though.

- **Scan-and-listen mode.** Cycle through scanned stations for ~15s
  each. Pure-JS change in `radio.html`.

- **Wake-up alarm.** Tune to a specific preset at a configured time.
  Implementation: a systemd timer that `curl -X POST`s to `/api/tune`.

## When making changes

1. The Pi at `/srv/radio` is a symlink-mounted git checkout. **Edits are
   live the moment the relevant service restarts.** No bootstrap re-run
   needed unless `bootstrap.sh` or systemd unit files changed.
2. After editing Python in `/opt/sdr-tuner/`: `sudo systemctl restart sdr-tuner`
   (for app.py) or `sudo systemctl restart sdr-captions` (for caption_orchestrator.py).
3. After editing HTML templates: `sudo systemctl restart sdr-tuner` and
   hard-refresh the browser (Ctrl+Shift+R).
4. After editing `stream.sh`: `sudo systemctl restart sdr-fm@active`.
5. After editing systemd units: `sudo systemctl daemon-reload` then restart
   each affected service.
6. **Test on the live deployment before committing.** This is a hobby
   project, not a system with CI. The Pi is the test environment.
7. Commit, push, done. The Pi pulls from the same git repo it runs from.

## What's not in git (ignored)

Anything user-specific or secret:

- `*.env` files (real configs with passwords)
- `fcc.json`, `stations.json`, `stations_am.json` (regenerable)
- `now_playing.json`, `captions.json` (transient state)
- `overrides.json` (per-user curation)
- `ui.json` (per-deployment settings)
- `.token` (Whisper auth secret)

See `.gitignore` for the full list.
