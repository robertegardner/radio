# 2026-05-27 — AM RFI environment discovery

Follow-up to `2026-05-27-hdr-and-31hz-analysis.md` and `2026-05-26-am-audio-tuning.md`.
Started with the symptom "all AM stations sound illegible" — including local
strong stations (KGIR 1220, KZYM 1230, KSIM 960), not just the long-haul
KMOX 1120. Ended with a confirmed environmental cause, a controlled-experiment
record, and diagnostic plumbing landed in the live tuner so the condition is
visible going forward.

## TL;DR

- **Root cause of "all AM illegible" is local RFI on Antenna C, not a
  software bug.** Two strong wide signals dominate the AM band:
  - ~1186 kHz, ~6 kHz wide, peak −13 dB, ~22 dB above the noise floor
  - ~1340 kHz, ~16 kHz wide, peak −10 dB, ~20 dB above the noise floor
- Both are constant-amplitude (low peak-to-median dB across a 15-min average)
  and at least one is off the standard 10 kHz AM channel grid. Signature is
  consistent with local switching-supply RFI.
- **Local stations are buried.** With every front-end setting empirically
  shown to pass MW through (HDR on, no notches), local KGIR 1220 (250 W,
  12 mi) sits at only ~7 dB SNR; KZYM 1230 (1 kW, 12 mi) is at the noise
  floor; KSIM 960 (5 kW) is at ~5 dB SNR. Healthy local AM reception
  typically needs 30+ dB SNR. The demod cannot recover signal that has
  been buried in the analog domain.
- **`rfnotch_ctrl` was confirmed (by controlled scan) to be a broadcast-band
  notch on the dx-R2, not the FM-only notch its name suggests.** Enabling
  it drops the entire 540–1700+ kHz band by 30–44 dB. We leave it off.
  See CLAUDE.md "Known driver quirks" for the canonical statement.
- **Cold-start init of `rfnotch_ctrl` is `false`, not the `default='true'`
  the metadata advertises.** `am_stream.py` does not explicitly write this
  key and reads back `'false'`, which is the good state. So rfnotch was
  not the cause of "AM illegible."
- **No demod, filter, or live SDR-setting changes** during this session,
  per explicit instruction once the cause was confirmed environmental.
- **Diagnostic plumbing shipped** so the next session (and the listener)
  can see the RFI condition without re-running this whole investigation.

## Why we got here

The previous session (HDR + 31 Hz, see notes from earlier 2026-05-27)
deployed several changes:
- HDR mode + DAB notch engaged
- `LO_OFFSET = 0` (HDR removes the DC spike)
- Per-sample dual EMA normalization (replacing block-rate)

After deploy, the symptom became "all AM illegible." Initial hypothesis tree:
1. Per-sample EMA introduced a bug
2. HDR mode misconfigured (wrong center, wrong band)
3. A notch is engaging silently
4. Front-end gain is wrong
5. Something at the antenna / RF environment

We did a bisection in that order. Steps 1 (reverted per-sample EMA) and
2 (verified HDR) did not change the symptom. The investigation then
pivoted to a spectrum-scan A/B of the front end, which surfaced the
actual cause — environmental RFI — that no demod change could fix.

## Methodology

Built `files/opt/sdr-tuner/am_diag_scan.py`: single-LO PSD scan at
1100.5 kHz, 2 MHz sample rate, ~1.95 kHz FFT bin width, covers
500–1700 kHz in one shot. Each invocation writes:
- `.csv` — per-second PSD (full time-series for later analysis)
- `.report.txt` — top-30 peaks, named-station check, noise floor, 940–980
  kHz neighborhood probe (where the previous "wide carrier at 960" was
  reported)
- `.state.txt` — full `getSettingInfo()` + `readSetting()` driver-state
  dump, gain elements, antenna, sample rate, frequency, bandwidth.

Three controlled 15-minute scans, all with HDR=on, gain=20, Antenna C,
LO=1100.5 kHz, AGC off — only the targeted notch differed:

| Scan | rfnotch | dabnotch | basename |
|------|---------|----------|----------|
| ctrl | off     | off      | `am-scan-20260527T143231-ctrl` |
| rf   | **on**  | off      | `am-scan-20260527T144733-rf`   |
| dab  | off     | **on**   | `am-scan-20260527T150235-dab`  |

Each scan's state dump verified that only the targeted notch toggled.
Sample rate, freq, bandwidth, antenna, all gain elements, biasT, agc
mode were identical across all three.

All scans + the comprehensive summary live in `/var/lib/sdr-streams/diag/`
on the Pi (not git-tracked):
- `am-debug-summary-20260527.md`
- `am-scan-20260527T143231-ctrl.{csv,report.txt,state.txt}`
- `am-scan-20260527T144733-rf.{csv,report.txt,state.txt}`
- `am-scan-20260527T150235-dab.{csv,report.txt,state.txt}`
- Plus two earlier 30-min scans `am-scan-20260527T125310.*` (rfnotch=off)
  and `am-scan-20260527T132314.*` (rfnotch=on) that gave the initial signal.

## Spectral results

### Noise floor (off-grid bins, 545–1695 kHz)

| Scan | median NF | max NF (loudest off-grid bin) |
|------|-----------|-------------------------------|
| ctrl | **−36.35 dB** | −13.73 @ 1185 kHz |
| rf   | **−71.13 dB** | −31.67 @ 545 kHz  |
| dab  | **−36.57 dB** | −15.84 @ 1185 kHz |

### Named target peaks (dB)

| Freq    | Station            | ctrl  | rf    | dab   | Δ(rf−ctrl) | Δ(dab−ctrl) |
|---------|--------------------|-------|-------|-------|------------|-------------|
| 960 kHz | KSIM Sikeston      | −31.0 | **−70.1** | −31.3 | **−39.1** | −0.3 |
| 1120    | KMOX St. Louis 50 kW | −37.7 | **−70.6** | −39.0 | **−32.8** | −1.3 |
| 1220    | KGIR local 250 W   | −29.0 | **−71.1** | −29.4 | **−42.1** | −0.3 |
| 1230    | KZYM local 1 kW    | −39.6 | **−71.4** | −39.0 | **−31.8** | +0.6 |
| 555     | below-band ref     | −34.3 | −37.6 | −34.4 | −3.3       | −0.1 |
| 1665    | above-band ref     | −27.5 | **−71.2** | −27.1 | **−43.7** | +0.4 |

### Unidentified wide signals (the actual RFI)

| Scan | 1186 peak | 1340 peak |
|------|-----------|-----------|
| ctrl | −13.3 dB  | −9.9 dB   |
| rf   | (at NF)   | (at NF)   |
| dab  | −13.3 dB  | −9.8 dB   |

Both behave like in-band MW signals as far as the front end is concerned
(absorbed by rfnotch, unaffected by dabnotch). Source not yet
identified — that's a hardware step.

## Findings

1. **`rfnotch_ctrl=true` is a band-wide MW attenuator on this driver/HW.**
   30–44 dB drop across 540–1700+ kHz. Driver name and description are
   misleading. Leave off for AM listening.
2. **`dabnotch_ctrl` has no observable MW effect.** Difference vs ctrl is
   <0.6 dB everywhere — within scan-to-scan variation.
3. **HDR stayed engaged across all scans.** `hdr_ctrl='true' [ok]` verified
   each time; the API log printed `rspDxParams.hdrEnable=1`. The 30–44 dB
   notch is not HDR turning off.
4. **Cold-start `readSetting` for `rfnotch_ctrl` returns `false`** even
   though `getSettingInfo().value` advertises `default='true'`. Verified
   by stopping `sdr-fm@active`, restarting `sdrplay.service` (clears
   retained API state), then restarting the stream and capturing
   `am_stream.py`'s state dump. Live AM listening is in the same state
   as our ctrl scan — not the broken rf-on state.
5. **Local AM SNR is anomalously low in the good config.** Local stations
   only 0–7 dB above the noise floor. Should be 30+ dB. The two
   unidentified wide RFI signals are ~20+ dB stronger than any named
   station. This is the actual demod-quality cap.

## Diagnostic plumbing shipped this session

To make the RFI condition visible without re-running this whole
investigation:

- **`startup_rfi_scan()` in `am_stream.py`** — 5-second pre-streaming PSD
  measurement on every AM stream restart. Logs the noise floor (median
  of off-grid AM bins) and any off-grid bin >15 dB above that floor (RFI
  candidates), plus station SNR at the tuned frequency. Adds ~5 s to AM
  stream startup. Skipped for non-MW tunes. Writes
  `/run/sdr-streams/rfi_status.json`.

- **Full driver state dump in `am_stream.py`** — `getSettingInfo()` +
  `readSetting()` of every key the driver exposes, plus all gain
  elements and the achieved sample rate / freq / bandwidth, logged at
  every SDR open. Catches silent driver-state side effects.

- **`/api/rfi_status` in `app.py`** — exposes the JSON to the UI. Returns
  `{available: false}` when no scan has run yet (FM/HD tunes, fresh boot
  before first AM stream). `clear_runtime_state()` unlinks the JSON on
  retune so stale data doesn't persist.

- **Amber `#rfi-banner` in `templates/index.html`** — admin UI surfaces
  RFI candidates as a check-engine light. Hidden when `rfi_candidates`
  is empty (clean environment) or the JSON is absent (FM/HD mode).
  References `hardware/RFI_HUNT.md` (file does not exist yet —
  placeholder for the hardware step).

- **`am_diag_scan.py`** — the standalone scan tool that produced this
  session's data. Reusable for any future verification (e.g. did the
  RFI source removal actually drop the noise floor). Runs as `radio`
  user, requires `sdr-fm@active` stopped first.

## Open follow-ups

- **Locate and remove/replace the RFI sources** with a battery-powered
  portable AM radio. Tune 1185 and 1340 kHz on the portable and walk
  the house. Common culprits: cheap LED bulbs, USB phone chargers,
  powerline network adapters, network switches. After cleanup, re-run
  `am_diag_scan.py` to verify noise floor dropped and named stations
  now stand above it.
- **Write `hardware/RFI_HUNT.md`** with the methodology and a log of
  identified culprits. Banner already links to it.
- **Reintroduce per-sample EMA normalization** (from fab5b08) after the
  RFI environment is clean enough that demod-quality differences are
  visible above the noise floor. Currently moot — current normalization
  is the block-rate version from e45da31, since the per-sample EMA was
  reverted during this session's bisection.
- **Reconsider 31 Hz envelope rhythm** only after RFI cleanup. Currently
  reducing a −14 dBc artifact when local stations are at 0–7 dB SNR is
  not a useful direction.

## Commit

`13eb103` — "AM diagnostics: spectrum scan tool, startup RFI scan, admin UI banner"
