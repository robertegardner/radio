# 2026-05-27 — HDR engagement and 31 Hz envelope investigation

Follow-up to `2026-05-26-am-audio-tuning.md`. Two task areas:
1. Engage dx-R2 MW-band features (HDR, FM/MW notch, DAB notch, MW notch).
2. Fix the persistent 31 Hz envelope rhythm using a per-sample EMA normalization.

## TL;DR

- **HDR mode and DAB notch now engaged for any MW tune.** Confirmed at the
  driver layer (`rspDxParams.hdrEnable=1`) and via SoapySDR readback of every
  setting on startup.
- **`rfnotch_ctrl` deliberately left OFF for MW.** On the RSPdx-R2 it's a
  combined MW+FM broadcast notch — enabling it during MW listening would
  attenuate the band we want. The brief's "FM-band notch ENABLED" cannot be
  satisfied without also notching MW on this hardware.
- **MW notch is not separately exposed by SoapySDRPlay3 0.5.2 / API 3.15.**
  The only notch settings the driver surfaces are `rfnotch_ctrl` and
  `dabnotch_ctrl`. The brief's "MW notch DISABLED" is therefore automatic
  (it's not a thing).
- **Dropped the +500 kHz LO offset for AM streaming.** HDR's signal path
  eliminates the dx-R2's DC spike, so we now place the carrier at DC and let
  the PLL FFT search cover the small residual TX/LO tolerance. NCO mix-back
  becomes identity (`nco_omega = 0`).
- **HDR mode reduces ADC peak utilization from ~21% to ~5%** at GAIN=20.
  Sweeping setGain across 0..20 with HDR on showed peak stays flat at ~5%
  and `rfgain_sel` pinned at 0 — Soapy's gain control can't compensate
  HDR's fixed signal-path attenuation. This is by design (HDR trades peak
  utilization for better DR/linearity on the desired channel). Kept GAIN=20;
  the 14-bit ADC still has >70 dB headroom above the signal.
- **The 31 Hz envelope rhythm is NOT a block-rate processing artifact.**
  Sweeping the channelizer block size (8k, 16k, 32k, 64k, no-blocks) all
  produce the same 31.01 Hz peak at the same -14.6 dBc — so it's intrinsic
  to the captured signal (broadcast audio or RF environment), not our DSP.
- **Replaced per-block envelope-mean normalization with per-sample dual
  EMA** anyway (sig_dc tracks real_part for DC removal, sig_amp tracks
  |y2| for amplitude norm). Live measurement shows 31 Hz dropped 2 dB
  across all harmonics (31/62/93/124 Hz). The remaining 31 Hz is
  signal-intrinsic and likely cannot be removed via demod-side DSP.
- **Strong suspect for the residual 31 Hz: Nielsen PPM audio watermarking.**
  Nielsen Portable People Meter encoding embeds a sub-audible audio code at
  ~30 Hz frame rate in the 1–3 kHz audio band. All three of our test
  stations (KMOX, WBBM, KFI) are major commercial broadcasters that almost
  certainly use Nielsen ratings. This would explain why all three exhibit
  the identical ~31 Hz envelope rhythm despite different programs.

## What the SoapySDRPlay3 driver actually exposes

`SoapySDRUtil --probe="driver=sdrplay"` (driver v3.150000, hwVer=7):

| Key | Type | Default | Purpose on RSPdx-R2 |
|---|---|---|---|
| `hdr_ctrl` | bool | true | RSPdx HDR mode enable |
| `rfnotch_ctrl` | bool | true | **Combined** MW+FM broadcast notch |
| `dabnotch_ctrl` | bool | true | DAB band notch (170–240 MHz) |
| `biasT_ctrl` | bool | true | 4.7 V bias-T on selected antenna |
| `iqcorr_ctrl` | bool | true | IQ DC correction |
| `agc_setpoint` | int | -30 | AGC target dBfs |
| `rfgain_sel` | str | 4 | LNA gain index (0..27) |

The "default=true" reported by the probe is just the SoapySDR setting-catalog
default — it does NOT mean the device boots with those values active. On a
fresh device handle, all the bool toggles are observed as `'false'` until
explicitly written. The brief's assumption that HDR was already on was
incorrect; we confirmed it via readback before/after writeSetting.

There is **no MW notch setting** in this driver version. There is no AM-port
selector either — Antenna C (HiZ BNC) is the AM input by hardware wiring.

**Practical detection of writeSetting failures:** The driver silently no-ops
unknown keys. After every writeSetting, readSetting and compare. Bool values
must be the literal strings `"true"` or `"false"` (lowercase, case-sensitive
match against `"false"` only — anything else is interpreted as true).

## HDR is engageable but doesn't snap the LO

Contrary to older SDRplay docs, this driver+API combo accepts arbitrary
`setFrequency` values with HDR engaged. setFrequency(1120 kHz) returns
1120 kHz, and the carrier behaves consistent with LO=1120 kHz.

Comparing IQ captures at LO=1120 kHz with HDR off vs on:
- DC region: +50.5 dB → +23.5 dB (the dx-R2's DC spike disappears with HDR)
- Peak |IQ|: 21.3% → 5.2% (HDR signal path has fixed attenuation)
- Strong carriers move from on-grid (NA AM 10 kHz multiples) to consistently
  +5 kHz off-grid. This is unexplained and worth investigating — possibly
  HDR introduces an IF shift, possibly the +5-kHz carriers are HD Radio
  digital subcarriers becoming dominant relative to the now-attenuated
  analog carriers. Doesn't appear to affect MW tuning.

## The 31 Hz: where it isn't, and what we think it is

Block-size sweep table (KFI 640 capture, post-channel-filter envelope, 1 Hz
resolution):

| Block size | Expected per-pair subharmonic | Measured 31 Hz peak |
|---|---|---|
| 8000  | 125.0 Hz | 31.01 Hz @ -14.6 dBc |
| 16000 | 62.5 Hz  | 31.01 Hz @ -14.6 dBc |
| 32000 | 31.2 Hz  | 31.01 Hz @ -14.6 dBc |
| 64000 | 15.6 Hz  | 31.01 Hz @ -14.6 dBc |
| no blocks (one-big-convolve) | — | 31.01 Hz @ -14.6 dBc |

The 31 Hz tracks the signal, not the processing. The brief's hypothesis
(block-rate normalization → subharmonic from block-to-block correlation)
is refuted by this data.

The most plausible remaining source is **Nielsen PPM audio watermarking**:
- 30 Hz code frame rate matches the observed 31.01 Hz peak (small variance
  consistent with Nielsen's exact frame rate)
- Present on all three commercial 50 kW clear-channels we tested
- Embedded by the broadcaster, not by the propagation path
- Not removable without harming the legitimate audio at 1–3 kHz

If Bob ever tests a non-commercial station (NPR/public radio AM), absence
of the 31 Hz would strongly support the Nielsen PPM hypothesis.

## What the per-sample EMA does and doesn't do

The new normalization (Variant C):
```python
real_part = mixed.real
env_abs = |y2|
for n in range(N):
    sig_dc = (1-α)·sig_dc + α·real_part[n]   # DC tracking (= carrier)
    sig_amp = (1-α)·sig_amp + α·env_abs[n]   # |env| tracking, always positive
audio = real_part − sig_dc_trace
output = clip(audio / max(sig_amp_trace, FLOOR) · 0.7)
```

With α = 1/(3·OUT_RATE) ≈ 6.7e-6, the EMA time constant is 3 seconds.
The trace varies smoothly sample-to-sample — no step changes at block
boundaries — so the normalization gain doesn't introduce block-rate
artifacts. **What this fix does well:** prevents the demod from creating
block-rate artifacts. **What it doesn't do:** remove 31 Hz that's already
present in the audio modulation.

Variant B (using `|carrier|` from the same EMA as the denom) appeared to
drop 31 Hz by 34 dB in offline tests, but post-analysis showed this was an
artifact of saturation: when the slow EMA of real_part is near zero (which
happens if the PLL picks the wrong sign or during fast fades), `|carrier|`
floors at FLOOR=0.001, the denominator collapses, output saturates at ±1,
and the clipping distorts the envelope FFT making the 31 Hz "look" lower.
Variant C avoids this entirely by using |y2| (always positive, never
collapses) for the normalization.

## Open questions

1. **Can the +5 kHz carrier offset in HDR mode be explained?** All strong
   carriers move +5 kHz off the NA 10 kHz grid when HDR is on. Could be HD
   Radio subcarriers becoming dominant; could be an IF offset in HDR's
   signal chain. Doesn't affect MW listening but worth understanding.
2. **What would PPM-confirming evidence look like?** Tune a non-commercial
   AM station (NPR translator, etc.) and check the envelope at 31 Hz. Or
   test the same channel at different times of day to see if the 31 Hz
   correlates with active broadcast (Nielsen-enrolled) vs dead air.
3. **Can the 31 Hz be notched out without harming voice?** A 31 Hz narrow
   notch in the audio chain would remove the PPM tones in their envelope
   contribution but leave the actual audio mostly intact (voice has no
   meaningful content at 31 Hz). Risk: harms music with low-frequency
   content. Worth trying if Bob accepts the trade-off.
4. **dynaudnorm amplification.** Live post-ffmpeg 31 Hz is at -13.5 dBc;
   pre-ffmpeg is at ~-17 dBc per offline testing. The 3-4 dB gap suggests
   dynaudnorm amplifies what's there. Replacing dynaudnorm with `loudnorm`
   (LUFS-targeted) or `acompressor` with longer release would help. The
   brief explicitly forbade this in this session.

## Files changed this session

- `files/opt/sdr-tuner/am_stream.py`:
  - Added `MW_SETTINGS` tuple — list of (key, value) writeSetting pairs
  - Added engagement+readback loop after device setup
  - `LO_OFFSET = 0` (HDR makes the DC dodge unnecessary)
  - Replaced per-block sig_amp+audio_dc updates with per-sample dual EMA
  - Updated docstring and comments to reflect the new pipeline

## Files for future analysis (uncommitted, in /tmp/)

- `iq_kfi640_hdr.iq` — 10 sec KFI 640 IQ capture with HDR on
- `probe_kmox_hdr.iq` — short KMOX 1120 IQ capture with HDR on
- `test_demod_variants.py` — A/B harness for normalization variants
- `find_31hz_source.py` — chain-stage envelope analysis
- `test_block_artifact.py` — block-size sweep test
- `test_variantC.py` — final Variant C verifier
- `probe_hdr_compare.py` — HDR-on vs HDR-off comparison
- `probe_gain_sweep.py` — gain sweep at fixed HDR settings
- `capture_and_analyze.py` — live-stream envelope analyzer
