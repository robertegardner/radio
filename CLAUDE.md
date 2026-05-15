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

## Hardware upgrade in progress (2026-05)

The Nooelec NESDR SMArt v5 (RTL2832U) is being replaced with an **SDRplay
RSPdx-R2**. The dx-R2 has three software-selectable antenna inputs (A/B/C),
which eliminates the GPIO relay design outlined in the hardware buildkit —
antenna switching becomes a software API call instead of a hardware relay. The
Cat 5 long-wire AM antenna plan still stands.

The Nooelec is being repurposed for a separate scanner project and will not be
retained as a backup for this Pi. The radio runs exclusively on the dx-R2
going forward.

**Why the upgrade:** Front-end overload was confirmed on the Nooelec at
`GAIN=30` with the Shakespeare 5120 antenna — 100.7 FM disappeared from scans;
gain had to drop to ~5 to listen cleanly. The dx-R2's 14-bit ADC and better
dynamic range should eliminate this entirely.

**Primary listening targets (highest-priority use case: Cardinals baseball):**
- KGMO 100.7 FM — primary FM station (Cape Girardeau)
- KMOX 1120 AM — Cardinals play-by-play (St. Louis, 50 kW clear-channel)
- KZYM 1230 AM — local talk/sports (Cape Girardeau)

## Where to find things

Application code lives in `files/opt/sdr-tuner/`. On the Pi it deploys to
`/opt/sdr-tuner/` via `deploy.sh` (see "When making changes" below).

| File | What it does |
|------|--------------|
| `stream.sh` | Bash wrapper around `rtl_fm` / `redsea` / `ffmpeg`. Reads `/etc/sdr-streams/active.env`. Branches: `hd` → `hd_stream.py`; `wbfm`/`fm` → rtl_fm + redsea + ffmpeg; AM/other → rtl_fm + ffmpeg. |
| `hd_stream.py` | HD Radio pipeline. Starts nrsc5, waits up to 15 s for audio lock. Lock: bridges nrsc5 → ffmpeg → Icecast. No lock: writes `hd_status.json` and exec's into analog FM+RDS fallback. Never crash-loops. |
| `rds_watcher.py` | Reads JSON from `redsea` on stdin, parses station name + artist/title from the unstructured RT field, writes `/run/sdr-streams/now_playing.json`. Knows several RT formats (Artist - Title, "Artist with Title on STATION", etc.) |
| `fm_scan.py` | rtl_power band sweep of 87.9–108.1 MHz, identifies stations above noise floor, optionally probes each one for RDS PS name. Writes `/var/lib/sdr-streams/stations.json`. |
| `am_scan.py` | Walks the AM band (540–1700 kHz) with rtl_fm one channel at a time using direct-sampling mode (-E direct2). Slower than rtl_power but actually works on cheap dongles. Writes `stations_am.json`. |
| `app.py` | Flask app on port 8080. Two UIs (admin at `/`, stereo at `/radio`) plus JSON APIs. `write_env` / `current_tune` / tune endpoints all carry `hd` + `subchannel` fields. `/api/now_playing` exposes `hd_probing`, `hd_locked`, `hd_unavailable` from `hd_status.json`. |
| `caption_orchestrator.py` | Decodes Icecast audio back to PCM, sends 6s chunks to Whisper for captions, fingerprints with Chromaprint/AcoustID for lyrics, looks up synced lyrics on LRClib. Writes `/run/sdr-streams/captions.json`. |
| `station_db.py` | Lookup helper: maps frequencies to call signs/cities by consulting `fcc.json` and `overrides.json`. `hd_subchannels(mhz)` returns known HD program indices from `hd_programs` in the station record. |
| `fcc_fetch.py` | Downloads FCC CDBS bulk files (facility, FM engineering, AM antenna, application tables) and joins them to produce `fcc.json` with real transmitter coordinates. Caches zip files in `/var/lib/sdr-streams/cdbs-cache/` (6-day TTL; use `--no-cache` to force refresh). FCC downloads can time out from the Pi — if so, download the four zip files on a laptop and scp to the cache dir. CDBS was frozen for new applications in Oct 2023 (existing licensed stations are complete). |
| `ui_settings.py` | Persists user-configurable UI settings (stream URL, site title) to `/etc/sdr-streams/ui.json`. |
| `templates/index.html` | Admin/control UI: stations table, scan buttons, settings, RDS now-playing, captions/lyrics view. Playing pill shows "HD1/HD2" suffix when in HD mode. FM rows show "HD1" tune button for stations with known `hd_programs`. |
| `templates/radio.html` | Stereo-style UI: amber-LCD frequency display (tap to direct-tune), HD LED + subchannel badge, HD toggle button, HD1–HD4 subchannel selector row, HD rows in station modal, 12 favorites (localStorage, HD-aware), seek/scan, direct-tune modal (⌨ button or tap freq display), browser audio playback. Handles `hd_probing` / `hd_locked` / `hd_unavailable` state from the API. |

The systemd units in `files/etc/systemd/system/` are:

- `sdr-fm@.service` — templated; `sdr-fm@active.service` is the running stream
- `sdr-tuner.service` — Flask web UI
- `sdr-scan.service` / `sdr-am-scan.service` — one-shot band scans, auto-stop and resume the stream around themselves via `ExecStartPre`/`ExecStartPost`
- `sdr-captions.service` — caption orchestrator

Config files in `files/etc/sdr-streams/` are `.example` files (real ones are
on the Pi, not in git):

- `active.env` — current tune; written by Flask on every `/tune`. Fields: `MODE` (`wbfm`, `am`, `hd`), `FREQ`, `SAMP`, `GAIN`, `BITRATE`, `MOUNT`, `EXTRA_FLAGS`, `ICECAST_PASS`. HD mode also adds `SUBCHANNEL=N` (0-indexed).
- `tuner.env` — Flask config (just Icecast password)
- `captions.env` — Whisper URL, AcoustID key, lyric offsets
- `overrides.json` — hand-curated station overrides. FM entries can include `"hd_programs": [0, 1]` to declare known HD subchannels (0-indexed), which surfaces HD rows in the station browser and the "HD1" tune button in admin.
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

**Analog FM (MODE=wbfm):**
```
RTL-SDR dongle
    ↓
rtl_fm (171kHz IQ — wide enough for 57kHz RDS subcarrier)
    ↓
tee
  ├── redsea ──→ rds_watcher.py ──→ /run/sdr-streams/now_playing.json
  ↓                                          ↓
ffmpeg (de-emphasis, lowpass,            caption_orchestrator
        MP3 encode, ac 1 → MONO)         (separate service, reads Icecast)
    ↓                                          ↓
Icecast :8000/fm.mp3                /run/sdr-streams/captions.json
    ↓
NPMplus → https://icecast.rg2.io/fm.mp3 → Browser <audio>
```

The analog FM pipeline is **mono today**. `rtl_fm -M fm` is a mono
demodulator (rtl_fm has no stereo mode), and ffmpeg downmixes with `-ac 1`.
See "Planned features" for the FM-stereo-via-nrsc5 work item.

**HD Radio (MODE=hd):**
```
RTL-SDR dongle
    ↓
nrsc5 (2 MHz IQ, OFDM decode, AAC decode → WAV to stdout)
  [hd_stream.py waits up to 15s for first byte — no byte = fall back to analog FM]
    ↓
ffmpeg (WAV → MP3 encode)
    ↓
Icecast :8000/fm.mp3 → Browser <audio>
```
hd_stream.py writes `/run/sdr-streams/hd_status.json` (`hd_probing` → `hd_locked` or `hd_unavailable`). The radio UI polls this and shows the state or reverts to analog automatically.

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

# Restart streaming pipeline (e.g. after stream.sh / hd_stream.py changes)
sudo systemctl restart sdr-fm@active

# Re-scan FM band (also runnable from admin UI)
sudo systemctl start sdr-scan

# Check current state
cat /run/sdr-streams/now_playing.json | jq
cat /run/sdr-streams/captions.json | jq
cat /run/sdr-streams/hd_status.json | jq   # hd_probing / hd_locked / hd_unavailable

# Probe a frequency for HD Radio (dongle must be free)
sudo systemctl stop sdr-fm@active
timeout 20 nrsc5 -d 0 -g 49.6 -o /dev/null 100.7 0   # "Synchronized" = HD present

# Fresh station database fetch (defaults already set for Cape Girardeau)
sudo -u radio python3 /opt/sdr-tuner/fcc_fetch.py
# Force re-download of CDBS files (otherwise uses 6-day cache)
sudo -u radio python3 /opt/sdr-tuner/fcc_fetch.py --no-cache
# If Pi times out downloading from FCC, scp files from laptop first:
#   scp facility.zip fm_eng_data.zip am_ant_sys.zip application.zip \
#       radio:/var/lib/sdr-streams/cdbs-cache/
#   (files from https://transition.fcc.gov/ftp/Bureaus/MB/Databases/cdbs/)
```

## Things to know about the dongle

> **Note:** The Nooelec RTL2832U is being replaced by an SDRplay RSPdx-R2.
> The caveats below are RTL-specific; the dx-R2 uses a different driver
> (`sdrplay` / `SoapySDR`) and has native AM coverage — no direct-sampling hack needed.

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

### HD Radio field notes

Tested 2026-05-13. Cape Girardeau, MO is a small market with no HD Radio
stations. Probed the 5 strongest FM signals (90.9, 97.5, 102.9, 106.1,
96.3 MHz) — nrsc5 ran 15 s on each with no `Synchronized` output. The
`hd_stream.py` fallback-to-analog path was verified to work correctly.
Nearest HD market is St. Louis (~115 miles). The feature is ready and
waiting for a signal.

### Planned features

- **FM stereo via nrsc5.** The analog FM path is mono today because
  `rtl_fm -M fm` is a mono demodulator. nrsc5 has an analog mode
  (`--analog` / `-N`) that outputs proper stereo PCM by decoding the
  19 kHz pilot and 38 kHz L-R subcarrier. Since nrsc5 is already
  installed for HD Radio and `hd_stream.py` already manages it, the
  cleanest path is to extend `hd_stream.py` (or add a sibling
  `analog_stream.py`) to run nrsc5 in analog mode for `MODE=wbfm`,
  replacing the rtl_fm + ffmpeg leg.

  Why nrsc5 over csdr for this:
  - csdr is lighter on CPU but less actively maintained
  - csdr's pipeline doesn't easily branch IQ for parallel decoders, which
    complicates the redsea integration we'd have to preserve
  - One tool, one set of CPU/code-complexity costs, already deployed

  Open question: where does RDS come from in this path? nrsc5 emits RDS
  natively for analog FM, which could replace redsea entirely for the FM
  branch (simpler) — or we keep redsea running in parallel against a
  separate `rtl_fm` instance just for RDS (more code, but isolates the
  stereo upgrade from the RDS parser we already trust). Decide once we
  see what nrsc5's analog RDS output actually looks like.

  Once stereo lands, consider bumping `BITRATE=128k` (the analog default)
  to 192k or 256k in `active.env`. 128k stereo MP3 has audibly narrowed
  imaging vs. 192k+. Trade-off is Icecast bandwidth (~30 MB/hour/listener
  at 256k).

- **HD Radio PAD metadata.** nrsc5 logs station name, artist, and title
  to stderr as it decodes. Capturing this (via a pipe on stderr) and
  writing it to `now_playing.json` would give the radio UI track info
  on HD channels without relying on RDS. Would need an `hd_watcher.py`
  parallel to `rds_watcher.py`.

- **HD station auto-detection during FM scan.** After `fm_scan.py`
  identifies a station, probe it with nrsc5 for a few seconds. If it
  locks, add `hd_programs: [0]` (or more) to the scan result. This
  removes the need for manual `overrides.json` entries for HD stations.

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
  timer that runs `fcc_fetch.py --no-cache` weekly and calls `/reload-stations`.

- **Scan-and-listen mode.** Cycle through scanned stations for ~15s
  each. Pure-JS change in `radio.html`.

- **Wake-up alarm.** Tune to a specific preset at a configured time.
  Implementation: a systemd timer that `curl -X POST`s to `/api/tune`.

## When making changes

Source lives in `/srv/radio/files/opt/sdr-tuner/`. The installed copy is
`/opt/sdr-tuner/` (owned by `radio:radio`). They are **not** symlinked —
use `deploy.sh` to push changes:

```bash
sudo /srv/radio/deploy.sh   # copies changed files + restarts both services
```

For surgical restarts after deploy:
1. `app.py`, `station_db.py`, `ui_settings.py`, or templates → `sudo systemctl restart sdr-tuner` + hard-refresh browser (Ctrl+Shift+R)
2. `stream.sh` or `hd_stream.py` → `sudo systemctl restart sdr-fm@active`
3. `caption_orchestrator.py` → `sudo systemctl restart sdr-captions`
4. systemd unit files → `sudo systemctl daemon-reload` then restart each affected service
5. `bootstrap.sh` changes (new deps, new units) → re-run `sudo ./bootstrap.sh`

**Test on the live deployment before committing.** This is a hobby project,
not a system with CI. The Pi is the test environment.

## What's not in git (ignored)

Anything user-specific or secret:

- `*.env` files (real configs with passwords)
- `fcc.json`, `stations.json`, `stations_am.json` (regenerable)
- `now_playing.json`, `captions.json`, `hd_status.json` (transient state)
- `overrides.json` (per-user curation)
- `ui.json` (per-deployment settings)
- `.token` (Whisper auth secret)

See `.gitignore` for the full list.
