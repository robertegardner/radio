#!/bin/bash
# /opt/sdr-tuner/stream.sh
# Streaming pipeline: HD Radio (nrsc5), FM+RDS (rx_fm+redsea), AM/other (rx_fm).
# Uses SDRplay RSPdx-R2 via SoapySDR (driver=sdrplay).
# Antenna A (SMA) = Shakespeare 5120 FM; Antenna B (SMA) = Cat 5 AM long-wire.
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
  # AM, NFM, etc. Antenna B = AM long-wire. No direct-sampling flag needed (dx-R2 covers AM natively).
  # SAMP is set to 96000 for AM — lowest dx-R2 rate with clean 2:1 decimation to 48k output.
  exec bash -c "rx_fm -d 'driver=sdrplay' -a 'Antenna B' -M ${MODE} -f ${FREQ} -s ${SAMP} -r 48000 -g ${GAIN} - | \
    ffmpeg -hide_banner -loglevel warning -f s16le -ar 48000 -ac 1 -i - \
           -c:a libmp3lame -b:a ${BITRATE} -content_type audio/mpeg \
           -f mp3 '${ICECAST_URL}'"
fi
