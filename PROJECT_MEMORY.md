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

Last meaningful update: AM antenna build in progress; dx-R2 ordered & shipped,
not yet received.

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

## Hardware — current and incoming

### Current
- **Raspberry Pi 5** in the attic, UCTRONICS U627803 PoE HAT (the HAT has a
  cutout exposing 36 GPIO pins).
- **Nooelec NESDR SMArt v5** (RTL2832U + R820T2) — the original SDR. 8-bit
  ADC. **Currently the active SDR for the radio project.** Will move to the
  scanner project once the dx-R2 takes over radio duty.
- **Shakespeare 5120** — 5-foot marine FM whip antenna, FM-resonant
  (88–108 MHz). **Installed in the attic and is the active antenna for the
  radio project right now**, connected to the Nooelec via pigtail.
- **GPU host** — separate homelab machine running a Whisper FastAPI service
  in Docker, used for live captions. Token-authenticated.

### Incoming / planned
- **SDRplay RSPdx-R2** — ordered and shipped, not yet received. Will become
  the dedicated AM/FM broadcast receiver for the radio project. 14-bit ADC,
  three software-selectable antenna inputs, HDR mode for strong AM dynamic
  range, hardware notch filters. Replaces the Nooelec for radio duty.
- **A dipole antenna** — for the scanner project's varied VHF interests,
  paired with the Nooelec.

## Final antenna / SDR assignment plan

Once the dx-R2 is installed, the intended steady state:

| SDR | Antenna(s) | Purpose |
|-----|-----------|---------|
| **RSPdx-R2** | Shakespeare 5120 → Antenna A; Cat 5 AM long-wire → Antenna C (BNC) | Radio project: FM + AM broadcast |
| **Nooelec NESDR SMArt v5** | Dipole antenna | Scanner project: EMS / NOAA APT / AIS / hobby |

- dx-R2 **Antenna A (SMA)** → Shakespeare 5120 (FM)
- dx-R2 **Antenna B (SMA)** → spare / experimental
- dx-R2 **Antenna C (BNC, HF-optimized)** → Cat 5 AM long-wire
- Antenna selection on the dx-R2 is a software API call — no relay hardware.

**Until the dx-R2 arrives:** the radio project keeps running on the Nooelec
with the Shakespeare 5120. The Cat 5 AM antenna can be built and strung now,
but it won't be usable for AM until the dx-R2 is installed (the Nooelec's AM
performance is poor and it's busy on FM).

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

## The AM antenna — current build (in progress)

Building a Cat 5 long-wire AM antenna.

- **Wire:** ~100 ft of Cat 5 on hand. Far end already joined (all 8
  conductors twisted into one bundle).
- **Near end:** splits into two 4-conductor leads — one antenna lead, one
  counterpoise lead — both connecting to a 9:1 unun balun (a "balun one
  nine", already on hand).
- **Color convention (for future reference):** the balun terminals are
  **not labeled**. Convention adopted for this build —
  **orange pair + green pair = antenna lead** (to the ANTENNA side),
  **blue pair + brown pair = counterpoise lead** (to the GROUND side).
  Use this same mapping for any future work on this antenna.
- **Balun:** 9:1 unun. Antenna lead → ANTENNA terminal, counterpoise lead →
  GROUND terminal. Radio side feeds RG-58 coax to the dx-R2's Antenna C (BNC).
- **Install plan:** string it through the attic as linearly as the framing
  allows (gentle L-bends OK, no tight coiling, no spooling excess wire).
  Drape from rafters, above the blown-cellulose ceiling insulation, away
  from AC lines and the attic PoE camera switches. Install on a cool evening.

**Settled antenna facts:**
- Linear run is essential — a coiled wire is electrically near-useless for
  AM (adjacent turns cancel, it becomes an inductor, and it sits in the
  noise). Length must be used as *length*, not spooled. Excess wire should
  be cut, not coiled. (Coiling the *coax* feedline is fine — coax is shielded.)
- The counterpoise being in the same Cat 5 jacket as the radiator is a mild
  compromise but still worth connecting — better than leaving the balun's
  GROUND terminal empty. Bob is not running a separate counterpoise wire.
- Bob's attic is RF-favorable: pine/plywood/asphalt-shingle construction, no
  foil radiant barrier, no metal roof, blown-cellulose insulation (RF-
  transparent). Network noise sources (servers, WiFi) are in the basement
  and main level — well separated. Only the attic PoE camera switches are
  nearby; give them a few feet of berth.
- Outdoor installation is not possible at Bob's location; attic is the
  chosen compromise and is a good one given the construction.

## Project status snapshot

- **Radio project:** running live on the Nooelec + Shakespeare 5120. HD Radio
  implemented. FCC CDBS station database implemented (replaced an earlier
  RadioBrowser approach that gave bad results). Admin UI + stereo `/radio` UI
  both built. Code is current; the repo and the Pi are in sync.
- **AM antenna:** under construction now. Far end joined; near end to be
  wired to the balun; stringing pending a cool evening.
- **dx-R2:** ordered and shipped; not yet received or installed.
- **Scanner project:** skeleton repo only, build pending.

## Open / planned work (radio project)

Captured in the repo's CLAUDE.md, summarized here:

- **dx-R2 integration** — new SoapySDR/sdrplay driver path; antenna selection
  by API call; this is the next big code effort once the hardware arrives.
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
  reboot to take effect. Symptom: `usb_claim_interface error -6`.
- **AM on RTL-SDR** needs direct sampling (`-E direct2`); the R820T tuner
  can't go below ~24 MHz natively. (Moot once the dx-R2 takes over — it does
  AM natively.)
- **`active.env` must be writable by `radio:radio`** or tuning returns 500.
- **Trixie uses `/usr/bin/systemctl`**, not `/bin/systemctl` — matters for
  the sudoers rules.
- **The Pi is headless** — `aplay` fails (no audio device). Test audio by
  publishing to Icecast and listening via the web UI.
- **FM front-end overload** on the 8-bit Nooelec with a strong antenna —
  the reason for the dx-R2 upgrade.

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
