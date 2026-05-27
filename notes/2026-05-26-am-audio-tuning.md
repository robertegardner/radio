# 2026-05-26 — AM Audio Tuning Session

Session goal: clean up AM audio. KMOX 1120 (the primary station of interest —
Cardinals baseball play-by-play) sounded like "electronic screeching" and KZYM
1220 was hissy. This document captures the diagnostic journey, the decisions
made, the final state, and open issues for review.

## TL;DR

- **Built a proper PLL-based synchronous AM demodulator** (`am_stream.py`).
  Replaces envelope detection. Locks on actual carrier offset (KMOX is ~10 Hz
  off DC) via an initial FFT search, then mixes carrier to true DC with a
  per-sample NCO, takes the real part as audio.
- **Disabled the dx-R2 hardware AGC** and switched to a fixed manual gain.
  The hardware AGC was fighting the software AGC, causing 18× block-rate
  level pumping.
- **Crushed mains hum** with a 4-pole 300 Hz highpass in ffmpeg. AM antenna
  was picking up 60/120 Hz at +60 dB — drowning the voice.
- **Carrier-magnitude based output normalization** (audio / envelope mean)
  replaces peak-tracking AGC. Eliminates pumping.
- Measured improvements on KMOX vs the original chain: **voice-to-noise
  +7 dB → +58 dB**, 6766 Hz envelope-detection harmonic **+79 dB → ~+25 dB
  pre-ffmpeg / +7 dB post-ffmpeg** (essentially inaudible).
- **Open issue**: a 31 Hz rhythmic pulsing persists on KMOX after all
  filtering. Strongly suspected to be intrinsic to KMOX's broadcast audio
  processing (program-correlated, present in raw IQ envelope, not a
  demod-chain artifact). User wants this addressed.
- **Open issue**: 1220 audio level is ~50% of 1120 (RMS 0.14 vs 0.25 in
  the MP3 stream). KZYM uses lighter audio compression so its modulation
  depth is naturally lower; we compensate partially in `dynaudnorm` but
  it's still noticeably quieter to the listener.

## Hardware / signal context

- **SDR**: SDRplay RSPdx-R2 via SoapySDR (`driver=sdrplay`). Antenna C
  (long-wire) for AM. Sample rate 2 MSps.
- **Local AM environment** (Cape Girardeau, MO, antenna position):
  - 1120 kHz KMOX (St. Louis, 50 kW clear-channel, news/talk): carrier ~10 Hz
    off nominal, +5 dB stronger than 960 in the band (not the loudest)
  - 1220 kHz KZYM (Cape Girardeau, ~5 kW local, talk): about 5 dB weaker
    than KMOX in band
  - 960 kHz (unidentified): strongest carrier in the band, +5 dB above KMOX,
    25 kHz wide (looks like AM with HD-style sidebands)
  - The 1120/1220 carrier offsets (~10 Hz on KMOX, ~150 Hz on a different
    capture) come from a mix of TX tolerance and dx-R2 LO drift.
- **`SAMP=2000000` is required** because rx_fm/our DSP chain applies a fixed
  +500 kHz LO offset to dodge the dx-R2's DC spike. At 1 MHz hardware rate
  the desired signal sits at Nyquist and gets filtered out.

## What was wrong (starting state)

Before today, AM streaming used `am_stream.py` (uncommitted at session start)
with:

- Envelope detection (`|y2|`)
- Channel filter: 127 Hamming taps, 6 kHz cutoff at 250 kHz rate
- Peak-tracking software AGC (asymmetric attack/release on per-block peak)
- Hardware AGC **enabled** by default
- `active.env` GAIN=30
- ffmpeg post: `aresample=48000, highpass=f=300, lowpass=f=5000,
  dynaudnorm=framelen=150:gausssize=3:maxgain=50`

Listener report: KMOX "screeching", 1220 hissy.

## Diagnostic findings (and how we got there)

### 1. Massive 60/120 Hz mains hum (+60 dB in raw envelope)

Long-wire AM antenna picks up power-line hum directly. Without aggressive
highpass, hum dominates the audio and `dynaudnorm` normalizes to the **hum**
instead of voice. Fixed by chaining two 2-pole highpass at 300 Hz in ffmpeg
(4-pole total, -56 dB at 60 Hz).

### 2. Hardware AGC + software AGC fighting

Captured live PCM showed **18× block-rate level swing** between 50 ms
blocks. The dx-R2's hardware AGC compresses the IF dynamically; our software
AGC reacts to the moving envelope; the two control loops oscillate against
each other at ~62 Hz block rate.

Fix: `setGainMode(False)` in `am_stream.py` to disable hardware AGC, set
fixed manual gain=20 (empirically verified to give ~28% ADC peak utilization
on KMOX with no compression).

### 3. The 6766 Hz "beeping" tone on KMOX (envelope distortion)

A high-pitched tone pulsing at ~10 Hz was clearly audible on KMOX. Initial
hypothesis: HD Radio IBOC digital sidebands. Wrong — the dx-R2 wasn't
overloading, and tightening the channel filter to Kaiser β=10 / 511 taps /
3.5 kHz cutoff (-105 dB at 6766 Hz) didn't kill the tone.

Real source: **envelope-detection harmonic**. For `|carrier + sideband|`,
nonlinear envelope detection produces a 2nd harmonic of the modulation
sideband frequency. KMOX runs heavy audio processing with strong 3.4 kHz
content; `|·|` makes that 6766 Hz.

### 4. The broken first attempt at synchronous demodulation

First sync demod tried: track the complex carrier as the EMA of `block_mean`
of `y2`, then `audio = Re{y2 · conj(C̄)/|C̄|} - |C̄|`. Worked perfectly in
offline tests against saved IQ. Live: hot/clipped audio, RMS=0.83 even with
OUTPUT_SCALE=0.3.

Root cause: **the carrier doesn't actually sit at DC in `y2`**. A
high-resolution FFT showed KMOX's carrier is at ~**-10.6 Hz**, not 0 Hz.
Block-mean integrates a rotating signal to zero, so `|C̄|` collapses to the
floor and `audio / |C̄|` blows up.

This was the key insight that led to:

### 5. PLL-based sync demod (the actual fix)

`am_stream.py` now:

1. Collects ~0.5 sec of post-channel-filter IQ on startup
2. Runs an FFT, finds the strongest peak within ±200 Hz of DC
3. Sets NCO frequency = `-peak_offset`
4. Per block: generates per-sample NCO `exp(j·2π·f·t + phase)`, mixes
   `y2 · conj(NCO)`, takes real part as audio
5. Subtracts slow-EMA DC (= carrier amplitude)
6. Normalizes by envelope-mean (`mean(|y2|)`, invariant to small lock errors)
7. Clips to [-1, +1] × `OUTPUT_SCALE` (0.7)

Offline measurement on saved KMOX IQ, comparing the old envelope demod vs
the new PLL sync demod (same channel filter):

| Metric | Envelope | PLL sync | Delta |
|---|---|---|---|
| Voice band (300-3000 Hz) avg | 34.0 dB | 44.0 dB | **+10.0** |
| Noise band (5-10 kHz) avg | 27.0 dB | -7.1 dB | **-34.1** |
| Voice-to-noise | +7.1 dB | +51.1 dB | **+44.0** |
| 6766 Hz tone | +79.3 dB | +3.2 dB | **-76.1** |
| Block-rate pumping (50 ms) | 3.2× | 2.3× | better |

Live (post-ffmpeg, via Icecast):

| | 1120 KMOX | 1220 KZYM |
|---|---|---|
| RMS | 0.25 | 0.14 |
| Peak | 0.89 | 0.85 |
| Voice band | +45.8 dB | +42.1 dB |
| **S/N** | **+57.5 dB** | **+83.1 dB** |
| Block pumping | 3.8× | 3.7× |
| 31 Hz envelope rhythm | +36 dB | +16 dB |

### 6. The persistent 31 Hz rhythm on 1120

After all the above fixes, the listener still reports rhythmic
pulsing/noise on KMOX. Measured: 31 Hz (with 62, 93 Hz harmonics) in the
envelope of the post-ffmpeg audio. Present in pre-ffmpeg PCM at lower
level, **amplified by dynaudnorm**, but **not created by it** (it exists in
the IQ envelope before any of our processing).

Reducing `dynaudnorm:maxgain` from 8 → 3 → 6 (final) helped but didn't
eliminate. The user accepts this is likely intrinsic to KMOX's audio
processor (heavy compression with a sub-audio release rhythm — common on
news/talk AM stations) and not fixable downstream.

## Final state (committed)

### `am_stream.py` (new file)

PLL-based synchronous AM demodulator:
- Input: raw IQ from dx-R2 at 2 MSps, Antenna C, manual GAIN, **hardware AGC off**
- NCO mixer +500 kHz to dodge DC spike (gives ±1 MHz channel window)
- Stage 1 LP: 63 Hamming taps, 100 kHz cutoff, decim 8 → 250 kHz
- Stage 2 LP: 511 Kaiser β=10 taps, **3.5 kHz cutoff**, decim 5 → 50 kHz
- PLL: FFT-based initial frequency search in ±200 Hz, then static NCO at
  that offset (no continuous re-lock yet — that's an open follow-up)
- Output: synchronous demod via real part of de-rotated mixed signal,
  envelope-mean normalization, clip to [-1, +1] × 0.7, s16le to stdout

### `stream.sh` (AM branch)

```bash
python3 /opt/sdr-tuner/am_stream.py | \
  ffmpeg -f s16le -ar 50000 -ac 1 -i - \
    -af 'aresample=48000,
         highpass=f=300:p=2, highpass=f=300:p=2,
         lowpass=f=3800,
         dynaudnorm=framelen=500:gausssize=11:maxgain=6' \
    -c:a libmp3lame -b:a 128k -f mp3 icecast://...
```

### `app.py`

`write_env` sets `GAIN=20` for AM (was 30). Comment explains why.

### `deploy.sh`

Adds install line for `am_stream.py`.

## Open follow-ups (where I want a review)

1. **The 31 Hz rhythm on KMOX.** Believed to be in the broadcast itself. Is
   there a clever DSP move to suppress sub-audio amplitude modulation
   without affecting voice? A slow downward expander? Multi-band processor?
   Or just accept it?

2. **1220 loudness.** Carrier-magnitude normalization gives a stable
   modulation-index output but stations with lower modulation depth sound
   quieter. Options I considered:
   - Higher `dynaudnorm:maxgain` (causes more pumping on 1120)
   - Acompressor with slow attack/release in ffmpeg
   - Soft tanh limiter in `am_stream.py` allowing higher OUTPUT_SCALE
   - Per-station calibrated gain (would require app.py changes to track
     per-station preferences)
   - **Loudness-target normalization** instead of peak target — `loudnorm`
     filter in ffmpeg targets LUFS instead of peak; might be exactly right
     for this. Haven't tried yet.

3. **PLL doesn't re-lock.** Currently does a one-shot FFT lock at startup.
   If the carrier drifts during a long listening session (dx-R2 LO is a
   TCXO, ~0.5 ppm — ~0.5 Hz at 1 MHz), eventually the lock degrades.
   Should add periodic re-FFT (every ~30 sec) with an update threshold.

4. **No proper Costas loop.** The FFT-then-static-NCO approach works but
   isn't a true PLL — there's no continuous phase tracking. A proper Costas
   loop would handle drift smoothly and pull in across a wider frequency
   range. Did I make the right tradeoff for simplicity, or is the Costas
   loop worth the complexity (50-100 more lines)?

5. **CPU headroom.** `am_stream.py` consumes ~60% of one Pi 5 core. 511-tap
   filter at 250 kHz input is the dominant cost. Could optimize by
   factoring the filter (e.g., polyphase), or by going through scipy
   (currently not installed; would need `sudo apt install python3-scipy`).
   Is it worth the dependency?

6. **`mean(|y2|)` as amplitude reference is biased.** For modulated AM,
   `mean(|carrier + m(t)|) = C + small_bias_from_m`. The bias depends on
   modulation depth. For carrier-normalized output, this means stations
   with higher modulation depth read as having larger "amplitude" and
   thus get slightly less gain. Is this a real audible problem?

7. **The "AGC pumping pre-ffmpeg" of 2.0× and post-ffmpeg of 3.8× on
   1120.** The PLL fixed this dramatically from before, but ffmpeg's
   `dynaudnorm` is adding back ~2× pumping. We're using framelen=500 (ms)
   and gausssize=11 = ~5.5 sec smoothing window. With maxgain=6 (~16 dB)
   that's still apparently enough to react to speech rhythm. Should we
   replace dynaudnorm with a real compressor (`acompressor`) and explicit
   `loudnorm`?

## Files changed this session

```
M  deploy.sh                       (install am_stream.py)
M  files/opt/sdr-tuner/app.py      (gain=20 for AM, hw AGC off semantics)
M  files/opt/sdr-tuner/stream.sh   (use am_stream.py; new ffmpeg chain)
A  files/opt/sdr-tuner/am_stream.py  (PLL sync demod, all the DSP)
```

## Notes for the reviewer

- All offline test scripts and captures are in `/tmp/` on the Pi (kmox*.iq,
  test_pll.py, debug_demod.py, analyze_pcm.py, etc.) — not committed but
  available for re-running.
- WAVs for A/B comparison were generated and listened to via a temporary
  http.server on port 8090; that's gone now but the analysis scripts will
  regenerate them.
- The user verified each change live by hard-refreshing the radio page
  (https://radio.rg2.io/radio behind NPMplus reverse proxy → Pi:8080) and
  listening on a real device.
