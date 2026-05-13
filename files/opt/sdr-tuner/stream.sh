#!/bin/bash
# /opt/sdr-tuner/stream.sh
# rtl_fm streaming pipeline. Branches FM (with RDS) vs AM/other.
set -euo pipefail
source /etc/sdr-streams/active.env

ICECAST_URL="icecast://source:${ICECAST_PASS}@localhost:8000/${MOUNT}"

# Reset RDS state on tune (caption orchestrator watches for this).
: > /run/sdr-streams/now_playing.json

if [[ "$MODE" == "wbfm" || "$MODE" == "fm" ]]; then
  # FM with RDS: 171k sample rate (3x the 57kHz subcarrier).
  exec bash -c "rtl_fm -M fm -l 0 -A std -s 171000 -g ${GAIN} -f ${FREQ} -F 9 - | \
    tee >(redsea -r 171000 --output json 2>/dev/null | FREQ='${FREQ}' /opt/sdr-tuner/rds_watcher.py) | \
    ffmpeg -hide_banner -loglevel warning -f s16le -ar 171000 -ac 1 -i - \
           -af 'aemphasis=mode=reproduction:type=75fm,lowpass=15000' \
           -ar 48000 -ac 1 \
           -c:a libmp3lame -b:a ${BITRATE} -content_type audio/mpeg \
           -f mp3 '${ICECAST_URL}'"
else
  # AM, NFM, etc. No RDS branch.
  exec bash -c "rtl_fm -M ${MODE} -f ${FREQ} -s ${SAMP} -r 48000 -g ${GAIN} ${EXTRA_FLAGS:-} - | \
    ffmpeg -hide_banner -loglevel warning -f s16le -ar 48000 -ac 1 -i - \
           -c:a libmp3lame -b:a ${BITRATE} -content_type audio/mpeg \
           -f mp3 '${ICECAST_URL}'"
fi
