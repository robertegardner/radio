# Homelab Radio / SDR Project — Project Memory

Context document for Claude Projects AND the repo. Any new conversation in
this project should read this first. It captures who I am, what I'm building,
the current state, and the plans — so I don't have to re-establish context
every time.

> **Keep this file updated.** It is a snapshot, not a live feed. At the end
> of any meaningful conversation where a decision was made, a build step
> completed, or hardware changed, refresh this file (edit and re-upload to
> the Claude Project, and commit to the repo). Stale project memory is worse
> than none — it makes Claude confidently wrong. If a conversation ends with
> "we decided X" or "I finished Y", that is the signal to update.

Last meaningful update: 2026-06-02 — **FM stream self-heal on SDRplay device
loss** (radio PR #1). The sibling scanner project's SDRTrunk was enumerating our
RSPdx-R2 on startup and knocking `rx_fm` off the device ("Device has been
removed"), and this rx_fm build loops that error forever without exiting so
systemd never restarted it. Added `device_loss_guard.sh` (watches rx_fm stderr,
kills the service on the marker → systemd restarts and re-acquires). The
persistent contention is prevented on the scanner side by restricting
`/usr/local/lib/libsdrplay_api.so*` to the `radio` group (in the scanner's
bootstrap.sh) — **re-apply after any SDRplay API reinstall**. Radio FM + scanner
MOSWIN now run simultaneously. Full record:
`notes/2026-06-02-fm-device-loss-selfheal.md`.

Prior: 2026-05-27 — dx-R2 fully deployed and AM stack rebuilt (HDR + Python
demodulator); local switching-supply RFI identified as the cap on AM audio
quality; diagnostic tooling shipped. See `notes/2026-05-27-am-rfi-discovery.md`.

---

## Who I am

- Bob Gardner. Based near Cape Girardeau, Missouri (lat ~37.31, lon ~-89.55).
- Run a homelab: Proxmox cluster, several Pis, network-attached services.
- Comfortable with Linux, Python, through-hole soldering, 3D printing
  (Bambu/Prusa-class printer, 0.4mm nozzle, 250mm+ bed).
- This project is a hobby build, not production. The hardware *is* the test
  environment.

## What I'm building

A multi-part software-defined radio (SDR) setup. Three related projects:

### 1. The radio project — `github.com/robertegardner/radio`

A live-tuning FM/AM broadcast receiver on a Raspberry Pi 5 in my attic.
Streams via Icecast, decodes RDS metadata, looks up synced lyrics, runs
Whisper-based live captions for talk content, and serves a car-stereo-style
web tuner UI.

- **Live deployment:** `https://radio.rg2.io` (admin UI) and
  `https://radio.rg2.io/radio` (stereo-style listener UI).
- **Stream:** `https://icecast.rg2.io/fm.mp3`, reverse-proxied via NPMplus.
- **Pi:** hostname `radio`, user `rgardner`, in the attic, PoE-powered via a
  UCTRONICS U627803 PoE HAT.
- **Repo layout:** code in `files/opt/sdr-tuner/`, deploys to `/opt/sdr-tuner/`
  via `deploy.sh`. Git checkout lives at `/srv/radio` on the Pi.
- **Project memory for Claude Code:** the repo has its own `CLAUDE.md` and a
  gitignored `CLAUDE.local.md`. Claude Code on the Pi reads those.

### 2. The scanner project — `github.com/robertegardner/scanner`

A planned multi-purpose secondary SDR scanner — currently a documented
skeleton, no code yet. Will time-slice one SDR across EMS/public-safety
scanning, NOAA weather satellite imagery, AIS marine tracking, and optionally
ACARS. Runs on the same attic Pi 5 but is a separate codebase. Build happens
later, with Claude Code on the Pi.

### 3. (Existing, separate) ADS-B flight tracker

A Pi 4 outside the house, two SDRs, 1090 MHz + 978 MHz UAT antenna, feeding
FlightAware / Flightradar24 / ADSBexchange. 700+ day uptime streak — do NOT
suggest changes that risk it. Future interest: anomaly alerting (read-only
analysis of its data). Not part of the radio or scanner repos.

## The PRIMARY use case

**Listening to St. Louis Cardinals baseball games over the radio.** This is
the main reason the project exists. Everything else (music, captions, lyrics,
HD) is secondary. Key Cardinals stations:

- **KMOX 1120 AM** — St. Louis, 50 kW clear-channel flagship. Designed to
  cover most of the central US at night; receivable in Cape Girardeau.
- **KZYM 1230 AM** — Cape Girardeau local Cardinals affiliate.
- **95.7 FM** and other FM affiliates also carry games.

Because the Cardinals network is fundamentally AM-based, **good AM reception
matters more than premium FM** for the core goal.

## Hardware — current state

- **Raspberry Pi 5** in the attic, UCTRONICS U627803 PoE HAT (the HAT has a
  cutout exposing 36 GPIO pins).
- **SDRplay RSPdx-R2** (serial 24051FAF70) — primary SDR for the radio
  project. 14-bit ADC, three software-selectable antenna inputs, HDR mode
  engaged for any MW tune. SoapySDR / SoapySDRPlay3 driver, API 3.150000.
- **Shakespeare 5120** — 5-foot marine FM whip antenna, FM-resonant
  (88–108 MHz). Connected to **dx-R2 Antenna A (SMA)** for FM and HD modes.
- **Cat 5 long-wire AM antenna** — built, strung through the attic.
  Counterpoise in same Cat 5 bundle. Feeds a 9:1 unun balun → RG-58 → **dx-R2
  Antenna C (BNC, HiZ-optimized)** for AM.
- **dx-R2 Antenna B** — unused / spare.
- **Nooelec NESDR SMArt v5** (RTL2832U + R820T2) — back in service with a
  dipole antenna, available to nrsc5 via the RTL-SDR API (`-d 0`) for HD
  Radio. nrsc5 doesn't support SoapySDR so HD path can only use the Nooelec.
- **GPU host** — separate homelab machine running a Whisper FastAPI service
  in Docker, used for live captions. Token-authenticated.

| SDR | Antenna(s) | Purpose |
|-----|-----------|---------|
| **RSPdx-R2** | Shakespeare 5120 → Antenna A; Cat 5 long-wire → Antenna C | Radio project: FM (Antenna A) + AM (Antenna C); software-selected per tune |
| **Nooelec NESDR SMArt v5** | Dipole | HD Radio via nrsc5; scanner project (planned) |

Antenna selection on the dx-R2 is a SoapySDR API call — no relay hardware.
GPIO relay plan from earlier in the project is DEAD (still documented in
`hardware/` but those sections are obsolete).

## Key decisions made (so we don't re-litigate them)

1. **RSPdx-R2 chosen over RSP1B, RSPduo, and Airspy HF+ Discovery.** Reasons:
   three software-selectable antenna inputs (eliminates the antenna-switching
   relay we'd otherwise need), HDR mode genuinely improves AM dynamic range,
   wide bandwidth supports HD Radio, and the cost delta over the alternatives
   buys real capability. The RSPduo was specifically ruled out — its dual
   tuners don't help a single-stream use case.

2. **The GPIO relay antenna-switching plan is DEAD.** It was designed for a
   single-antenna-input SDR. The dx-R2's three antenna ports make antenna
   selection a software API call instead. No relay, no driver board, no boost
   converter, no relay case. (A full relay build guide was written and lives
   in the repo's `hardware/` directory — its *antenna-assembly* sections are
   still valid; ignore its relay/case sections.)

3. **FM was overloading the Nooelec.** With the Shakespeare 5120 delivering
   strong signal, the Nooelec's 8-bit front end saturated — KGMO 100.7
   disappeared from scans at GAIN=30, needed GAIN=5 to listen cleanly. The
   dx-R2's 14-bit ADC is expected to fix this entirely. This overload problem
   is *why* the SDR upgrade happened.

4. **FM stereo + HD Radio path:** use `nrsc5` for both. nrsc5 already handles
   HD Radio in the codebase; it also has an analog mode that decodes FM
   stereo. Single tool for both. (HD Radio is implemented and field-tested;
   no HD stations exist in the Cape Girardeau market, but the analog
   fallback path works. Nearest HD market is St. Louis ~115 mi.)

5. **Two separate repos, not one.** Radio and scanner are independent
   codebases that happen to share the attic Pi. Different `/srv` directories,
   different system users, different SDRs, different antennas.

## The AM antenna — installed

Cat 5 long-wire is up. Reference facts kept for any future rebuild:

- **Wire:** ~100 ft of Cat 5. Far end is all 8 conductors twisted into one
  bundle.
- **Near end:** splits into two 4-conductor leads — orange + green pair =
  antenna lead (to ANTENNA terminal of the balun); blue + brown pair =
  counterpoise lead (to GROUND terminal). Balun terminals are not labeled,
  so this color convention is the only record.
- **Balun:** 9:1 unun ("balun one nine"). Radio side feeds RG-58 to dx-R2
  Antenna C (BNC).
- **Strung:** through the attic as linearly as framing allowed, draped from
  rafters above blown-cellulose insulation, away from AC lines and the
  attic PoE camera switches.

**Settled antenna facts:**
- Linear run is essential — a coiled wire becomes an inductor and is
  electrically useless for AM. Cut excess, don't spool. (Coax feedline can
  be coiled — it's shielded.)
- Counterpoise in the same Cat 5 jacket as the radiator is a mild compromise
  but still worth connecting; better than leaving GROUND empty.
- Attic is RF-favorable here: pine/plywood/asphalt-shingle, no foil radiant
  barrier, no metal roof, blown cellulose (RF-transparent). Outdoor install
  is not possible.

## Project status snapshot

- **Radio project:** running live on the dx-R2. FM uses rx_fm via SoapySDR
  on Antenna A. AM uses a Python demodulator (`am_stream.py`) on Antenna C
  — HDR mode + DAB notch engaged, PLL-based synchronous detection,
  block-rate normalization (per-sample EMA was reverted during 2026-05-27
  bisection, see "Recent investigation" below). HD Radio falls back to
  analog FM (no HD stations in market).
- **AM RFI hunt (open):** local AM is currently noise-floor-limited, not
  software-limited. Two strong wide RFI signals at ~1186 and ~1340 kHz are
  ~20 dB louder than legitimate local stations. Next step is portable AM
  radio walk-through to locate the source devices. See
  `notes/2026-05-27-am-rfi-discovery.md`.
- **FCC CDBS station database:** implemented and current. Replaced an
  earlier RadioBrowser approach. Real transmitter coordinates.
- **Admin UI** at `https://radio.rg2.io`, **stereo UI** at `/radio`. RFI
  banner added this session as a check-engine light for the noise-floor
  problem.
- **Scanner project:** skeleton repo only, build pending. Nooelec available
  for it.

## Recent investigation (2026-05-27 session)

After the HDR+per-sample-EMA changes deployed earlier the same day,
symptom was "all AM stations sound illegible" including strong locals.
Bisected:

1. Reverted per-sample EMA → no improvement
2. Spectrum-scanned the AM band with HDR on, all notches off → found
   environmental RFI dominating the band
3. A/B/C scan of each notch settled a long-standing driver quirk:
   `rfnotch_ctrl` on this driver build is empirically a broadcast-band
   notch covering 540–1700 kHz (despite its name and description claiming
   "RF Notch Filter Control"). Live config doesn't enable it — fortunate.

Outcome: no demod or filter changes; the live tuner config was confirmed
empirically correct. Diagnostic plumbing was added so the RFI condition
is now visible in journal and in the admin UI banner. Full record in
`notes/2026-05-27-am-rfi-discovery.md`. Driver quirks are documented in
CLAUDE.md's "Known driver quirks" section.

## Open / planned work (radio project)

Captured in the repo's CLAUDE.md, summarized here:

- **Physical RFI hunt (highest priority for AM quality)** — portable AM
  radio walk-through to find what's broadcasting on 1185 and 1340 kHz.
  Common culprits: cheap LED bulbs, USB phone chargers, powerline
  network adapters, network switches. Re-run `am_diag_scan.py` after
  each removal to verify the noise floor dropped.
- **Write `hardware/RFI_HUNT.md`** — methodology + log of identified
  culprits. The admin UI banner already references it.
- **Reintroduce per-sample EMA normalization** in `am_stream.py` after
  the RFI environment is clean enough that demod-quality differences
  are visible above the noise floor.
- **FM stereo via nrsc5** — analog FM is mono today; nrsc5 analog mode adds
  stereo. Bump MP3 bitrate to 192–256k once stereo lands.
- **NPMplus auth on admin endpoints** — `/radio` and read-only APIs stay
  public; `/`, `/tune`, `/scan-*`, `/settings` should require auth.
- **Favorites sync/export** — presets are per-browser localStorage today;
  add URL-based export/import for cross-device sync.
- **Stream recording** — capture current stream to timestamped MP3.
- **Weekly cron** for the FCC station database refresh.
- **Scan-and-listen mode**, **wake-up alarm** — minor UI/timer features.

## How to work with me on this

- The **repos are the source of truth.** `CLAUDE.md` in each repo is the
  authoritative project memory for Claude Code sessions. This document is
  the higher-level memory for design/planning conversations.
- For **implementation work**, Claude Code on the Pi is the right tool — it
  reads files and runs commands directly. For **design, troubleshooting,
  and planning**, a chat conversation is the right tool.
- If you need to see current repo state, ask me to fetch a specific GitHub
  URL (paste it), or paste `cat`/`git log` output from the Pi. I cannot
  autonomously poll the repo between messages.
- I prefer complete rewritten files over diffs when handing over code.
- I test on the live deployment; there is no CI.

## Things that have bitten this project before

- **DVB kernel driver** auto-claims RTL-SDR dongles — blacklisted, needs a
  reboot to take effect. Symptom: `usb_claim_interface error -6`. (Still
  applies for the Nooelec serving HD Radio via nrsc5.)
- **AM on RTL-SDR** needs direct sampling (`-E direct2`); the R820T tuner
  can't go below ~24 MHz natively. (Moot for the radio project's AM path
  now — runs on the dx-R2 which does AM natively.)
- **`active.env` must be writable by `radio:radio`** or tuning returns 500.
- **Trixie uses `/usr/bin/systemctl`**, not `/bin/systemctl` — matters for
  the sudoers rules.
- **The Pi is headless** — `aplay` fails (no audio device). Test audio by
  publishing to Icecast and listening via the web UI.
- **FM front-end overload** on the 8-bit Nooelec with a strong antenna —
  the reason for the dx-R2 upgrade.
- **`rfnotch_ctrl` on dx-R2 is a broadcast-band notch, not FM-only.**
  Enabling it attenuates 540–1700+ kHz by 30–44 dB. Live `am_stream.py`
  leaves it at the driver's cold-start init (which is empirically
  `false`, not the `default='true'` metadata claims). See CLAUDE.md
  "Known driver quirks" and `notes/2026-05-27-am-rfi-discovery.md`.
- **`SoapySDR.writeSetting` silently no-ops unknown keys** on
  SoapySDRPlay3. Always pair writes with `readSetting` and compare.
- **`getSettingInfo().value` (driver-reported default) can disagree with
  the actual cold-start init.** Verify state with `readSetting` after open.
- **USB over-current on the Pi disconnects the dx-R2.** Observed during
  the 2026-05-27 session — the dx-R2 fell off the bus and `sdr-fm@active`
  crash-looped for hours emitting "Device has been removed" while
  Icecast kept its mount open against a dead pipe. The device
  re-enumerated cleanly on USB, but the long-running stream process held
  a stale handle. Recovery: `sudo systemctl restart sdr-fm@active`. Root
  cause of the over-current event itself is not yet known.
- **Local AM RFI swamps the noise floor.** Two strong wide signals at
  ~1186 kHz and ~1340 kHz, ~20 dB louder than any legitimate local
  station. Locating them is a hardware walk-through, not a software
  fix.

---

## Maintenance reminder

This file drifts out of date the moment something changes. Treat updating it
as part of finishing any session. Triggers to update:

- A hardware change (received, installed, swapped, retired)
- A design decision made or reversed
- A build milestone completed (antenna strung, dx-R2 integrated, etc.)
- A new planned feature added or an existing one finished
- Anything in "Project status snapshot" no longer being true

When updating: revise the relevant section, bump the "Last meaningful update"
line near the top, re-upload to the Claude Project, and commit to the repo.
