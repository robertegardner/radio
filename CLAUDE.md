# CLAUDE.local.md

Pi-specific operational notes. This file is gitignored — it's local to the
Pi and contains environment details that shouldn't go in the public repo.

## This machine

- **Hostname:** `radio`
- **OS:** Raspberry Pi OS Lite 64-bit, Trixie / Debian 13
- **Hardware:** Raspberry Pi (4 or 5 — check `cat /proc/cpuinfo`)
- **User:** `rgardner` (interactive), `radio` (system, runs services)
- **Working directory for this project:** `/srv/radio` (git checkout)
- **Deploy location:** `/opt/sdr-tuner/` (pushed via `deploy.sh`)
- **Public hostnames (via NPMplus reverse proxy):**
  - `https://radio.rg2.io` — admin and radio UIs
  - `https://icecast.rg2.io/fm.mp3` — Icecast stream

## SDR hardware

- **Dongle:** Nooelec NESDR SMArt v5 (RTL2832U + R820T2)
- **Serial:** 22012952 (from `rtl_test -t`)

### Current antenna setup (interim)

**Attic TV antenna split to feed the SDR.** This is a known-bad signal chain
for FM and even worse for AM:

- TV antennas are designed for 54–88 MHz, 174–216 MHz, and 470–608 MHz —
  the 88–108 MHz FM band sits in their notched gap, often with explicit
  FM-trap filtering
- A passive splitter costs ~4 dB of signal loss
- The TV antenna's directional pattern is aimed at the TV broadcast cluster,
  not optimized for omnidirectional FM/AM reception
- TV antennas have ~zero response below ~50 MHz, so AM (530–1700 kHz)
  reception is essentially accidental coupling

**Observed symptoms:**

- 100.7 KGMO (50 kW, ~25 mi) comes through clean — punches through anything
- 91.1 KRCU (60 kW, ~10 mi, *closest licensed station to us*) is static
- 97.3 KYRX (6 kW, ~30 mi) is static
- 97.5 KOEA (100 kW, ~70 mi) is static
- AM scan: only a few stations detected, all static

**Conclusion:** The signal chain is the bottleneck. Don't spend time
optimizing scanner thresholds, gain settings, intermod mitigation, or
adjacent-channel filtering against this baseline — the data is compromised
by hardware. Park signal-quality work until the proper antenna is in place.

### Incoming antenna (proper FM)

**Shakespeare 5120 — 5-foot fiberglass marine FM whip, 88–108 MHz resonant,
75 Ω with built-in matching, omnidirectional.** On order.

When it arrives:
- Run dedicated coax from the Shakespeare to the SDR (do NOT split with TV)
- Mount outside if possible (gains 10–20 dB over attic)
- Re-run band scan; results will look dramatically different
- Re-test 97.3 KYRX; KFTK Florissant bleed may or may not remain
- AM reception will *not* meaningfully improve — Shakespeare 5120 is
  resonant for FM; it's electrically tiny at AM wavelengths (a 5-ft whip
  is 0.005 wavelengths at 1000 kHz). For real AM performance we'd need
  a dedicated AM loop antenna or a long-wire (10–20 ft minimum).

## GPU host

The Whisper FastAPI service for captions runs on a separate machine in
this homelab — one of the Beelink/eGPU Proxmox nodes with an RTX 3080 or
RTX 4060. Resolvable as a hostname through AdGuard Home. Token at
`/opt/whisper-svc/.token` on that host; matching value lives in this Pi's
`/etc/sdr-streams/captions.env` as `WHISPER_TOKEN=`.

## Location

For station database fetches and FCC distance filtering:

- **Lat/Lon:** 37.31, -89.55 (Cape Girardeau, MO area)
- **Max radius for `fcc_fetch.py`:** 400 km

These are the defaults in `fcc_fetch.py` already, so running it with no
arguments produces the right result for this Pi.

## Hand-curated overrides

`/etc/sdr-streams/overrides.json` has any local stations where the FCC/
RadioBrowser data is missing or wrong. Edit and click "Reload DB" in the
admin UI — no restart needed.

## Useful one-liners specific to this deployment

```bash
# Pull latest and restart everything
cd /srv/radio && git pull && sudo ./deploy.sh

# If stream.sh / hd_stream.py changed
sudo systemctl restart sdr-fm@active

# Manual test of caption pipeline against current stream
curl -s http://localhost:8000/fm.mp3 | head -c 100 | wc -c
# (should be 100, confirms Icecast is serving)

# Check what's currently tuned
sudo cat /etc/sdr-streams/active.env

# Quick state snapshot
cat /run/sdr-streams/now_playing.json /run/sdr-streams/captions.json \
    /run/sdr-streams/hd_status.json 2>/dev/null | jq -s '.'
```

## SSH / network

- The Pi reaches the GPU host by hostname through AdGuard Home DNS
- NPMplus runs on a different host in the homelab; SSL certs and reverse
  proxy rules live there
- Stream URL in `/etc/sdr-streams/ui.json` is set to
  `https://icecast.rg2.io/fm.mp3` so the radio page works over HTTPS
  without mixed-content warnings

## Things that have bitten us before

1. **Permissions:** `active.env` must be `0660 radio:radio` or `/tune`
   returns 500. The `bootstrap.sh` handles this correctly on fresh
   installs.
2. **Working directory traversal:** `/opt/sdr-tuner` is a real directory
   (not a symlink). Source lives at `/srv/radio/files/opt/sdr-tuner/`
   and is pushed via `deploy.sh`. `/srv` is 0755 root by default — works
   for the `radio` user.
3. **DVB driver:** `usb_claim_interface error -6` means either the
   blacklist hasn't taken effect (reboot) or `sdr-fm@active` is still
   running and holding the dongle.
4. **sudoers path:** Trixie uses `/usr/bin/systemctl`, not `/bin/systemctl`.
   The sudoers rules in this repo are already correct for Trixie.
5. **Audio test scripts need an actual output device.** The Pi is headless
   — `aplay` fails with "Unknown error 524". For test audio, publish to
   the Icecast mount instead and listen via the radio UI.
