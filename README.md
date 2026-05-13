# SDR Radio Stack

A complete software-defined radio (SDR) broadcasting setup for the Raspberry Pi.
Pairs a USB RTL-SDR dongle with Icecast streaming, RDS metadata decoding,
audio-fingerprinted lyrics, Whisper-powered live captions for talk content,
and a stereo-style web tuner.

![flow](docs/flow.txt)

## What it does

- **Live FM and AM streaming** from an RTL-SDR USB dongle, served over Icecast
- **Web admin UI** for tuning, band scanning, configuration
- **Stereo-style "radio" UI** with LCD-effect display, 12 favorites, seek/scan, browser audio playback
- **RDS decoding** for FM station name, artist, and title (via redsea)
- **Synced lyrics** from LRClib, looked up by RDS metadata or audio fingerprint
- **Live captions** for talk content via remote Whisper (GPU) over HTTP
- **Station database** from RadioBrowser, filterable by distance from a configurable origin, with per-frequency overrides

## Project layout

```
sdr-radio-stack/
├── bootstrap.sh              # One-shot installer for the Pi
├── README.md
├── files/                    # Everything that gets installed on the Pi
│   ├── opt/sdr-tuner/        # Application code (deploys to /opt/sdr-tuner)
│   │   ├── stream.sh
│   │   ├── rds_watcher.py
│   │   ├── fm_scan.py
│   │   ├── am_scan.py
│   │   ├── app.py
│   │   ├── caption_orchestrator.py
│   │   ├── station_db.py
│   │   ├── fcc_fetch.py
│   │   ├── ui_settings.py
│   │   └── templates/
│   │       ├── index.html    # Admin UI
│   │       └── radio.html    # Stereo-style UI
│   └── etc/                  # Config + systemd
│       ├── systemd/system/
│       │   ├── sdr-fm@.service
│       │   ├── sdr-tuner.service
│       │   ├── sdr-scan.service
│       │   ├── sdr-am-scan.service
│       │   └── sdr-captions.service
│       ├── sdr-streams/
│       │   ├── active.env.example
│       │   ├── tuner.env.example
│       │   ├── captions.env.example
│       │   └── overrides.json.example
│       └── sudoers.d/sdr-tuner
├── scripts/
│   └── whisper-svc/          # GPU-host transcription service (Docker)
│       ├── Dockerfile
│       ├── docker-compose.yml
│       └── whisper_service.py
└── docs/
```

## Hardware

| Component       | Recommended                                            |
|-----------------|--------------------------------------------------------|
| SBC             | Raspberry Pi 4 (2GB+) or Pi 5 (any)                    |
| OS              | Raspberry Pi OS Lite 64-bit (Trixie / Debian 13)       |
| SDR             | Nooelec NESDR SMArt v5 (RTL2832U + R820T2) or similar  |
| FM antenna      | Telescopic FM whip — straight into the SMA            |
| AM antenna      | Long-wire (10-20 ft) clipped to SMA center pin         |
| GPU host        | Any machine with NVIDIA GPU + Docker for Whisper       |

The GPU host is optional — without it, you lose live talk captions but
keep RDS, lyrics, and all the streaming. The bar for "useful GPU" is low:
an RTX 3060 or even GTX 1660 runs `small.en` faster than realtime.

## Quick start

```bash
# 1. Flash Raspberry Pi OS Lite (64-bit) to an SD card with Pi Imager.
#    Set hostname=radio, enable SSH, configure WiFi.
# 2. SSH in:
ssh rgardner@radio

# 3. Clone this repo:
git clone https://github.com/YOUR_USERNAME/sdr-radio-stack.git
cd sdr-radio-stack

# 4. Run the installer:
sudo ./bootstrap.sh

# 5. Follow the printed REMAINING STEPS to configure Icecast, AcoustID,
#    and the GPU host's Whisper service.
```

Full step-by-step instructions are below.

---

## Installation

### Step 1 — Burn the SD card

Use **Raspberry Pi Imager**:

- OS: Raspberry Pi OS Lite (64-bit) — Trixie
- Hostname: `radio`
- Enable SSH (paste your public key)
- Set username/password
- Configure WiFi if no Ethernet

Card size: 16GB minimum. Class 10/U1 minimum, A1-rated preferred — cheap
cards cause crashes under sustained writes.

### Step 2 — Prep the Pi

```bash
sudo apt update && sudo apt full-upgrade -y
sudo reboot
```

Set a static IP or DHCP reservation. The Pi needs to reach the GPU host
by hostname, and the radio UI is served from this address.

### Step 3 — Clone and bootstrap

```bash
git clone https://github.com/YOUR_USERNAME/sdr-radio-stack.git
cd sdr-radio-stack
sudo ./bootstrap.sh
```

The installer takes 5-10 minutes — most of it building redsea from source.

### Step 4 — Configure Icecast

```bash
sudo dpkg-reconfigure icecast2
sudo systemctl restart icecast2
```

Pick a source password you'll remember. Verify at `http://<pi-ip>:8000`.

### Step 5 — Configure the SDR stack

Edit the Icecast password in two places:

```bash
sudo nano /etc/sdr-streams/active.env
sudo nano /etc/sdr-streams/tuner.env
```

Both files need `ICECAST_PASS=` set to the same password from step 4.

Configure the caption orchestrator:

```bash
sudo nano /etc/sdr-streams/captions.env
```

- `WHISPER_URL` — your GPU host (e.g., `http://gpu-host:8088`)
- `WHISPER_TOKEN` — generated when you set up the Whisper service (see below)
- `ACOUSTID_KEY` — free from https://acoustid.org/login

### Step 6 — Set up the Whisper service on the GPU host

```bash
ssh GPU_HOST
sudo mkdir -p /opt/whisper-svc && sudo chown $USER /opt/whisper-svc

# Copy the scripts/whisper-svc/ contents here
scp -r rgardner@radio:~/sdr-radio-stack/scripts/whisper-svc/* /opt/whisper-svc/
cd /opt/whisper-svc

# Generate a token
openssl rand -hex 32 > .token
echo "WHISPER_TOKEN=$(cat .token)" > .env

# Build and start
docker compose up -d --build
docker compose logs -f whisper
# Wait for "[whisper] ready"
```

Copy the token from `/opt/whisper-svc/.token` into the Pi's
`/etc/sdr-streams/captions.env` as `WHISPER_TOKEN=`.

### Step 7 — Fetch the station database

```bash
sudo -u radio python3 /opt/sdr-tuner/fcc_fetch.py \
  --lat YOUR_LAT --lon YOUR_LON --max-km 400
```

Cape Girardeau, MO defaults are baked in. Adjust `--lat` / `--lon` for
your area. RadioBrowser data covers ~90% of US commercial broadcast
stations.

### Step 8 — Verify the dongle and start

```bash
rtl_test -t
# If usb_claim_interface error -6: sudo reboot
# (the DVB driver blacklist takes effect at next boot)

sudo systemctl start sdr-tuner sdr-fm@active sdr-captions
```

### Step 9 — Use it

- Admin UI: `http://<pi-ip>:8080`
- Radio UI: `http://<pi-ip>:8080/radio`
- Direct stream: `http://<pi-ip>:8000/fm.mp3`

Click "Scan FM" or "Scan AM" to populate the station list. After it
finishes, station call signs and cities should auto-fill from the FCC/
RadioBrowser data.

## Configuration

### Stream URL behind a reverse proxy

If you serve the radio UI over HTTPS (e.g., `https://radio.example.com`),
the stream URL must also be HTTPS or browsers will block it as mixed
content. In the admin UI, expand **Settings** at the bottom and set
**Stream URL** to your proxied HTTPS Icecast endpoint, e.g.,
`https://icecast.example.com/fm.mp3`.

### Hand-curated station overrides

Edit `/etc/sdr-streams/overrides.json` to add or correct station info:

```json
{
  "fm": {
    "100.7": {"call": "KGMO", "city": "Cape Girardeau", "state": "MO"}
  },
  "am": {
    "1240": {"call": "KZIM", "city": "Cape Girardeau", "state": "MO"}
  }
}
```

Click "Reload DB" in the admin UI — no restart needed.

### Lyric timing offsets

If lyrics scroll out of sync, edit `/etc/sdr-streams/captions.env`:

- `RDS_OFFSET_MS` (positive ms) — adjusts RDS-sourced lyrics. Increase if
  lyrics lag the audio; decrease if they lead.
- `LRC_OFFSET_MS` (negative ms) — same idea for fingerprint-sourced lyrics.

Then `sudo systemctl restart sdr-captions`.

## How it works

The streaming pipeline:

```
RTL-SDR dongle
    ↓
rtl_fm (FM: 171k IQ sample rate for RDS subcarrier)
    ↓
tee
  ├──── redsea ──── rds_watcher.py ──── /run/sdr-streams/now_playing.json
  │                                              ↓
  │                                     caption_orchestrator
  │                                       ↓        ↓        ↓
  │                                    LRClib  AcoustID  Whisper (GPU host)
  │                                       ↓        ↓        ↓
  │                                    /run/sdr-streams/captions.json
  ↓
ffmpeg (de-emphasis, lowpass, MP3 encode)
    ↓
Icecast (http://pi:8000/fm.mp3)
    ↓
Browser <audio> element on radio.html
```

The Flask app at port 8080 serves both the admin and stereo UI, and
exposes the JSON APIs that the radio page uses for tuning, station
lists, and live now-playing info.

## Common problems

**`usb_claim_interface error -6` after a fresh install**
DVB kernel driver loaded before the blacklist took effect. Reboot.

**Stream starts then dies with 403 Forbidden**
Old rtl_fm/ffmpeg processes still hold the Icecast mount.
`sudo pkill -9 rtl_fm ffmpeg redsea` and restart `sdr-fm@active`.

**Tune button does nothing / 500 error**
Either the sudoers rule isn't matching (`which systemctl` should be
`/usr/bin/systemctl`), or `/etc/sdr-streams/active.env` isn't writable
by the radio user.

**Captions stuck on "Live Captions" during music**
The station isn't broadcasting Artist/Title via RDS RT, and the FM
audio processing is breaking Chromaprint fingerprints. Check the RT
parsing logic in `rds_watcher.py` against the format your station uses.

**Browser plays nothing when I tap ▶**
Browser autoplay policy. Click the button manually once; subsequent
retunes will resume automatically.

**HTTPS mixed-content warning**
The radio page is served over HTTPS but the stream URL is HTTP. Set
**Stream URL** in admin Settings to the HTTPS-proxied URL.

## Maintenance

```bash
# Pull latest from git
cd ~/sdr-radio-stack && git pull
sudo ./bootstrap.sh           # idempotent — won't overwrite configs

# Refresh station database
sudo -u radio python3 /opt/sdr-tuner/fcc_fetch.py

# Watch all the things
sudo journalctl -u sdr-tuner -u sdr-fm@active -u sdr-captions -f
```

## License

MIT — see LICENSE.

## Credits

Built incrementally on a Pi with the help of Claude (Anthropic). Specific
shoutouts to upstream projects this stack depends on:

- [rtl-sdr](https://osmocom.org/projects/rtl-sdr) for the dongle drivers
  and `rtl_fm`/`rtl_power`/`rtl_test`
- [redsea](https://github.com/windytan/redsea) for RDS decoding
- [LRClib](https://lrclib.net) for synced lyrics
- [AcoustID](https://acoustid.org) and Chromaprint for audio fingerprinting
- [RadioBrowser](https://www.radio-browser.info) for the station catalog
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) for ASR
- [Icecast](https://icecast.org) for streaming
