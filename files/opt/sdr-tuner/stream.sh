#!/bin/bash
# /opt/sdr-tuner/stream.sh
# Streaming pipeline: HD Radio (nrsc5), FM+RDS (rx_fm+redsea), AM/other (rx_fm).
# Uses SDRplay RSPdx-R2 via SoapySDR (driver=sdrplay).
# Antenna A (SMA) = Shakespeare 5120 FM/HD; Antenna C (SMA) = long-wire AM.
set -euo pipefail
source /etc/sdr-streams/active.env

ICECAST_URL="icecast://source:${ICECAST_PASS}@localhost:8000/${MOUNT}"

# Reset RDS state on tune (caption orchestrator watches for this).
: > /run/sdr-streams/now_playing.json

if [[ "$MODE" == "hd" ]]; then
  # HD Radio: probe for lock, stream if found, fall back to analog FM if not.
  exec python3 /opt/sdr-tuner/hd_stream.py

elif [[ "$MODE" == "wbfm" || "$MODE" == "fm" ]]; then
  # FM with RDS: 250k output → 2 MSps hardware (8× oversampling, supported by dx-R2). Antenna A = FM.
  exec bash -c "rx_fm -d 'driver=sdrplay' -a 'Antenna A' -M fm -l 0 -A std -s 250000 -g ${GAIN} -f ${FREQ} -F 9 - | \
    tee >(redsea -r 250000 --output json 2>/dev/null | FREQ='${FREQ}' /opt/sdr-tuner/rds_watcher.py) | \
    ffmpeg -hide_banner -loglevel warning -f s16le -ar 250000 -ac 1 -i - \
           -af 'aemphasis=mode=reproduction:type=75fm,lowpass=15000' \
           -ar 48000 -ac 1 \
           -c:a libmp3lame -b:a ${BITRATE} -content_type audio/mpeg \
           -f mp3 '${ICECAST_URL}'"

else
  # AM, NFM, etc. Antenna C = long-wire AM antenna.
  #
  # rx_fm with SDRplay applies a fixed +500 kHz LO offset (DC-spike avoidance). At SAMP=1000000
  # the desired signal falls right at the Nyquist edge and gets filtered out. SAMP=2000000 puts
  # it 500 kHz inside the passband where it demodulates cleanly. Do not lower SAMP below 2000000.
  # rx_fm outputs at SAMP Hz (-r flag is silently ignored with SoapySDR); ffmpeg resamples to 48k.
  #
  # Audio filter chain:
  #   highpass=300   — cut hum and low rumble below voice range
  #   lowpass=5000   — cut noise above AM broadcast audio band (~5 kHz max)
  #   dynaudnorm     — level-normalize so quiet signals are listenable without clipping loud ones
  exec bash -c "rx_fm -d 'driver=sdrplay' -a 'Antenna C' -M ${MODE} -f ${FREQ} -s ${SAMP} -g ${GAIN} - | \
    ffmpeg -hide_banner -loglevel warning -f s16le -ar ${SAMP} -ac 1 -i - \
           -af 'aresample=48000,highpass=f=300,lowpass=f=5000,dynaudnorm=framelen=150:gausssize=3:maxgain=50' \
           -ar 48000 -ac 1 \
           -c:a libmp3lame -b:a ${BITRATE} -content_type audio/mpeg \
           -f mp3 '${ICECAST_URL}'"
fi
