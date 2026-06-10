#!/bin/bash
# /opt/sdr-tuner/stream.sh
# Streaming pipeline: HD Radio (nrsc5), FM+RDS (rx_fm+redsea), AM/other (rx_fm).
# Uses SDRplay RSPdx-R2 via SoapySDR (driver=sdrplay).
# Antenna A (SMA) = Shakespeare 5120 FM/HD; Antenna C (SMA) = long-wire AM.
set -euo pipefail
source /etc/sdr-streams/active.env

# Publish host is env-able (active.env ICECAST_HOST): since the 2026-06-10
# distribution cutover the public icecast lives on the rack (192.168.6.82);
# V1 DSP on the Pi publishes there. Default stays localhost for safety.
ICECAST_URL="icecast://source:${ICECAST_PASS}@${ICECAST_HOST:-localhost}:8000/${MOUNT}"

# Reset RDS state on tune (caption orchestrator watches for this).
: > /run/sdr-streams/now_playing.json

if [[ "$MODE" == "hd" ]]; then
  # HD Radio: probe for lock, stream if found, fall back to analog FM if not.
  exec python3 /opt/sdr-tuner/hd_stream.py

elif [[ "$MODE" == "wbfm" || "$MODE" == "fm" ]]; then
  # FM with RDS: 250k output → 2 MSps hardware (8× oversampling, supported by dx-R2). Antenna A = FM.
  # rx_fm's stderr is routed through device_loss_guard.sh: this rx_fm build loops
  # "Device has been removed." forever instead of exiting on SDRplay USB loss, so
  # the guard kills this shell ($$) to let systemd (Restart=always) recover us.
  exec bash -c "rx_fm -d 'driver=sdrplay' -a 'Antenna A' -M fm -l 0 -A std -s 250000 -g ${GAIN} -f ${FREQ} -F 9 - 2> >(/opt/sdr-tuner/device_loss_guard.sh \$\$) | \
    tee >(redsea -r 250000 --output json 2>/dev/null | FREQ='${FREQ}' /opt/sdr-tuner/rds_watcher.py) | \
    ffmpeg -hide_banner -loglevel warning -f s16le -ar 250000 -ac 1 -i - \
           -af 'aemphasis=mode=reproduction:type=75fm,lowpass=15000' \
           -ar 48000 -ac 1 \
           -c:a libmp3lame -b:a ${BITRATE} -content_type audio/mpeg \
           -f mp3 '${ICECAST_URL}'"

else
  # AM, NFM, etc. Antenna C = long-wire AM antenna.
  #
  # am_stream.py replaces rx_fm for AM. rx_fm under SoapySDR has no channel
  # filter (the -r audio rate flag is silently ignored), so its envelope
  # detector mixes the entire IF bandwidth — the strongest station in the
  # band dominates the audio regardless of where you tune. am_stream.py
  # captures IQ from the dx-R2 directly, mixes to baseband, decimates with
  # a narrow FIR (final channel bandwidth ~6 kHz), envelope-detects, and
  # outputs 50 kHz mono PCM to stdout.
  # Filter chain (must run at 48k — biquad cutoffs near sample rate misbehave at 50k):
  #   highpass×2 @300 — 4-pole highpass to crush 60/120 Hz mains hum. AM antennas
  #                     near power lines pick this up at +60 dB; without filtering
  #                     it dominates the audio and dynaudnorm normalizes to the
  #                     hum instead of the voice. 4-pole @300 puts 60 Hz down 56 dB.
  #   lowpass=3800   — cleans up residue above the am_stream.py 3.5 kHz channel filter
  #   dynaudnorm     — maxgain=6 (~16 dB) gives weak stations enough headroom
  #                    to come up to comfortable loudness. Long framelen+gausssize
  #                    means it averages over ~5 seconds so it can't react to
  #                    syllable-rate dynamics and pump on speech rhythm.
  exec bash -c "python3 /opt/sdr-tuner/am_stream.py | \
    ffmpeg -hide_banner -loglevel warning -f s16le -ar 50000 -ac 1 -i - \
           -af 'aresample=48000,highpass=f=300:p=2,highpass=f=300:p=2,lowpass=f=3800,dynaudnorm=framelen=500:gausssize=11:maxgain=6' \
           -ar 48000 -ac 1 \
           -c:a libmp3lame -b:a ${BITRATE} -content_type audio/mpeg \
           -f mp3 '${ICECAST_URL}'"
fi
