# Unified SDR GUI — design

Status: **design + Phase-0 prototype** (2026-06-18). Built on a branch
(`unified-gui`) for review; nothing deployed live. This is the reference we build
against.

## Goal

One clean, beautified web GUI for the whole stack — tune **AM / FM / NOAA / P25 /
ATC** directly, see **which streams are live**, with a side page for **manual
tunes + antenna selection**. Grows the existing FM tuner (amber-LCD + Butterchurn
viz aesthetic). Designed to absorb the **multichannel-FM branch** without a
rewrite.

## The hard constraint that shapes everything: 3 SDRs

| SDR | Antenna | 24/7 default | On-demand (preempts the default) |
|---|---|---|---|
| **dx-R2** (sdrplay, 1 tuner / 3 ports) | A=Shakespeare, B=AM loop+balun, C=long-wire | **FM** (A) | AM on B/C (preempts FM) — DX a weak station |
| **HF+** (airspyhf, 1 tuner) | YouLoop | **AM** (contention-free) | — |
| **R2** (airspy, 1 tuner) | discone | **NOAA** | **P25**, **ATC** (preempt NOAA) |

Consequences the GUI must encode:
- **AM should live on the HF+/YouLoop** (contention-free) so the **dx-R2 frees up
  for multichannel FM** (which wants the whole FM band full-time). Keep dx-R2 B/C
  as *on-demand, preempts-FM* options for chasing one weak AM station.
- **NOAA owns the R2's 24/7 slot** (alerts are passive + time-critical). **P25 and
  ATC preempt it on demand.** Tradeoff: SAME/EAS isn't decoded while P25/ATC run —
  acceptable for short on-demand sessions. *The unlock for both-24/7 is a 4th
  receiver (cheap RTL-SDR) for P25 off a discone splitter; until then NOAA wins.*
- Every "tune" that preempts another source must **confirm** ("this stops FM" /
  "this stops NOAA alerts"). The preempt mechanics already exist
  (`monitor.service`, AM-preempts-FM); the GUI just needs to know the groups.

## Core abstraction: a **source** is a **stream**

Model the stack as a list of *streams*, each with: `id`, `band`
(fm/am/noaa/p25/atc), `mount` (icecast), `device` (dx-r2/hf-plus/r2),
`antenna`, `state` (live/idle/preempted), `tunable` (freq or fixed), and a
`preempts`/`preempted_by` group. The GUI renders sources from this list and the
player swaps to the selected source's mount.

**Why this matters for multichannel FM:** FM stops being "one tuner" and becomes
*N streams* (one per channel). If the GUI treats FM as a *set of streams* from day
one, the multichannel branch just populates the FM tab with more entries — no UI
rewrite. So: **FM tab = a channel grid, not a single dial,** even today (with one
entry).

## Architecture: extend the radio tuner into a gateway

Don't build a new app. The radio tuner (.84) already proxies the Pi's wxsat API
via a `before_request` hook — reuse that pattern to make it the **single frontend
+ thin gateway** to the scanner (.83) and wx backends. Sub-backends stay put.

```
            ┌─────────────── radio-tuner app (.84) — UNIFIED FRONTEND + GATEWAY
browser ───▶│  /dash         unified shell (source tabs, player, active badges)
            │  /radio        existing FM/AM tuner (kept)
            │  /api/stack-state   aggregates icecast status-json + service states
            │  /api/tune, /api/antenna ...  (FM/AM/NOAA — local)
            │  proxy  /api/scanner/*  ─────▶ scanner-api (.83:8081)  (P25/ATC/monitor)
            │  proxy  /api/wx/*       ─────▶ wx-alert (.84:8090) + wxsat (Pi)
            └────────────────────────────────────────────────────────────
icecast (.82) status-json  ◀── polled by /api/stack-state  ── the "what's live" truth
```

### `/api/stack-state` (Phase 0 — the foundation)

One read-only endpoint the whole GUI keys off. Aggregates:
- **icecast `status-json`** (`.82:8000/status-json.xsl`) → live mounts + listener
  counts → the **active-stream badges** (ground truth, no new plumbing).
- **service/device state** (which SDR is doing what) → the **contention badges**
  ("R2: NOAA — P25 will preempt").

Returns e.g.:
```json
{ "ts": "...",
  "devices": { "dx-r2": {"role":"fm","busy":true}, "hf-plus": {"role":"am"},
               "r2": {"role":"noaa","preemptible":["p25","atc"]} },
  "streams": [ {"id":"fm","mount":"/fm.mp3","live":true,"listeners":2,"device":"dx-r2"},
               {"id":"noaa","mount":"/wx.mp3","live":true,"listeners":0,"device":"r2"},
               {"id":"p25","mount":"/ems.mp3","live":false,"device":"r2","preempts":["noaa"]} ] }
```
The active indicator + contention confirms are pure functions of this. **Built in
this branch** (read-only, safe).

## The unified shell (`/dash`)

- **Top: source tabs** `FM · AM · NOAA · P25 · ATC`. Each shows its current
  stream(s) + a play button; selecting one points the single audio element at
  that mount. Live tabs get a lit badge; preemptible/contended ones a warning dot.
- **Active-streams strip:** small always-visible row of badges from
  `/api/stack-state` — "FM ●live 2", "NOAA ●live", "P25 ○idle". One glance = what's
  running + who's listening.
- **FM tab = channel grid** (multichannel-ready): today one card; the branch adds
  more. Each card: freq, station, viz toggle, play.
- **Reuse the amber-LCD + Butterchurn** chrome from `/radio` for continuity.

## Side page: manual + antennas (`/dash/manual`)

Extends today's admin: direct freq entry per band; **antenna selection**
(A/B/C/HF+ for AM, the FM antenna, discone is fixed); gains/squelch; scans;
the **A/B/C comparison tools** below.

## A/B/C antenna comparison — ideas

The survey + rescan already produce per-antenna SNR (`stations_am.json
by_antenna`). Ideas to make comparison *visceral*, ranked by effort:

1. **Per-station SNR bars in the table (built).** The admin table already shows
   A/B/C/HF+ columns with the winner highlighted. Add tiny inline bar sparklines
   + a "best" trophy. *(cheap polish)*
2. **Day-vs-night overlay.** Persist each rescan (we already snapshot to
   `surveys/`); show two scans side-by-side per station with Δ arrows (the night
   sweep proved winners flip by time-of-day — 1230/1550 → HF+ day, dx-R2 B night).
   A "compare scans" dropdown picks two timestamps. *(medium)*
3. **Live A/B switch (the killer feature).** Two simultaneous mounts on different
   devices (we prototyped this: `/am-youloop.mp3` vs `/am-dxr2.mp3`) → a GUI
   **A/B toggle button** that swaps the player between them instantly for the same
   station. Only works for *cross-device* pairs (HF+ vs dx-R2) since same-device
   needs retune. Auto-spins up the two transient mounts on demand, tears down
   after. *(medium-high — but the "wow")*
4. **Blind A/B.** Hide which antenna is playing; user picks "better", reveal +
   log a preference → builds a per-station antenna preference automatically.
   *(fun, low-stakes)*
5. **Noise-floor / RFI view.** Surface `rfi-scan.py` output: per-antenna noise
   floor + the spur map (the YouLoop's nighttime +8 dB RFI carpet, the 516 kHz
   birdie, the 1540–1640 comb). A "scan RFI" button per antenna → a spectrum
   strip with spurs flagged. Explains *why* an antenna wins/loses. *(medium)*

Recommended first: **#1 (polish, ~done) + #3 (live A/B)** — together they turn the
abstract SNR table into "press a button, hear the difference."

## 24/7 operating model (Phase 4)

- **NOAA = provisioned 24/7 default on the R2** (productize tonight's transient
  `wx-on-r2` → a proper `wx-on-r2.service` in the scanner-compute provisioner;
  optimize `monitor_stream` first — it's 90% of a core at 2.5 Msps).
- **P25 / ATC = scanner-api on-demand actions** that preempt NOAA (ATC already is;
  make P25 the same). Each auto-returns to NOAA on stop.
- **FM 24/7** on the dx-R2 (→ multichannel). **AM 24/7** on the HF+/YouLoop.
- Revisit P25-in-background only when a 4th receiver lands.

## Phasing

| Phase | What | Risk |
|---|---|---|
| **0** | `/api/stack-state` (active + contention truth) | read-only, safe — *prototyped here* |
| **1** | gateway proxies (scanner/atc/wx) + source tabs on the tuner | additive routes |
| **2** | beautified `/dash` shell + active badges + contention confirms | frontend |
| **3** | multichannel FM → FM tab becomes the channel grid (already abstracted) | with the branch |
| **4** | provision NOAA-24/7-on-R2 + P25/ATC on-demand; optimize monitor_stream | touches scanner |

## What's in this branch for review

- This doc.
- `/api/stack-state` in `app.py` (read-only aggregator) + a minimal `/dash`
  shell that renders it (source badges + per-source play). **Not deployed** —
  review, then `deploy.sh --rack` when approved.
