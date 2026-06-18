#!/bin/bash
# ATC scheduled recorder. The tick (atc-rec-tick) writes record.env (URL + FILE)
# then start/stops atc-record.service. -c copy just remuxes the icecast MP3 to a
# file (no re-encode); reconnect rides out brief mount blips during the window.
set -euo pipefail
: "${URL:?URL not set}"
: "${FILE:?FILE not set}"
# The mount only appears once the R2 source bounce completes and monitor.service
# connects to icecast — until then it 404s and ffmpeg would die on the initial
# open. Wait (up to ~40s) for the mount to go live before recording.
for _ in $(seq 1 20); do
  [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 4 "${URL}" || true)" = "200" ] && break
  sleep 2
done
exec ffmpeg -hide_banner -loglevel warning -nostdin -y \
  -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 \
  -i "${URL}" -c copy -f mp3 "${FILE}"
