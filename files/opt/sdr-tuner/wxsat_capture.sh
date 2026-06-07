#!/bin/bash
# wxsat_capture.sh — capture + decode a single Meteor-M LRPT pass.
#
# Stops the broadcast stream, records IQ from the SDRplay dx-R2 Port B with
# rx_sdr (rx_tools), then decodes the baseband with SatDump. SatDump's Debian
# build has NO SoapySDR source, so we capture with rx_tools (the same front-end
# stream.sh uses) and decode the recorded baseband offline.
#
# The stream is ALWAYS restarted, on any exit path, via the trap below.
#
# Required env (set by wxsat_scheduler.py):
#   WXSAT_OUT_DIR   product output directory (created if needed)
#   WXSAT_DURATION  capture seconds (rx_sdr runtime)
# Tuning env (from /etc/sdr-streams/wxsat.env):
#   FREQ_MHZ ANTENNA SAMPLERATE GAIN LRPT_PIPELINE
#
# Exit codes: 0 ok; 11 rx_sdr failed; 12 no IQ; 13 satdump decode failed.

set -uo pipefail

OUT="${WXSAT_OUT_DIR:?WXSAT_OUT_DIR required}"
DUR="${WXSAT_DURATION:?WXSAT_DURATION required}"
FREQ_MHZ="${FREQ_MHZ:-137.9}"
ANTENNA="${ANTENNA:-Antenna B}"
SAMPLERATE="${SAMPLERATE:-1000000}"
GAIN="${GAIN:-40}"
LRPT_PIPELINE="${LRPT_PIPELINE:-meteor_m2-x_lrpt}"
export HOME="${HOME:-/var/lib/sdr-streams/wxsat}"
TLE_DIR="/var/lib/sdr-streams/wxsat/tle"

IQ="$OUT/baseband.cs16"
mkdir -p "$OUT"

# ALWAYS bring the broadcast stream back and drop the bulky raw IQ, no matter
# how we exit (normal, error, signal, or SatDump hang/kill). This is rule #1.
cleanup() {
  rm -f "$IQ"
  sudo /usr/bin/systemctl start sdr-fm@active >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

# Seed SatDump's TLE file from our cache so georeferenced products have fresh
# elements. (SatDump's own celestrak fetch is dead on this Pi — see CLAUDE.md;
# the /etc/hosts redirect makes it fast-fail and proceed.)
mkdir -p "$HOME/.config/satdump"
if ls "$TLE_DIR"/*.tle >/dev/null 2>&1; then
  cat "$TLE_DIR"/*.tle > "$HOME/.config/satdump/satdump_tles.txt"
fi

echo "wxsat: stopping stream for a ${DUR}s capture on ${FREQ_MHZ} MHz / ${ANTENNA}"
sudo /usr/bin/systemctl stop sdr-fm@active
sleep 5   # let the SDRplay API release the RSP before we grab it (Phase-0 lesson)

# Record IQ for the pass. `timeout` ends rx_sdr at the deadline → exit 124,
# which is the normal end of a capture, not a failure.
timeout "$DUR" rx_sdr -d "driver=sdrplay" -a "$ANTENNA" \
  -f "${FREQ_MHZ}e6" -s "$SAMPLERATE" -g "$GAIN" -F CS16 "$IQ"
rc=$?
if [[ $rc -ne 0 && $rc -ne 124 ]]; then
  echo "wxsat: rx_sdr failed (rc=$rc)" >&2
  exit 11
fi
if [[ ! -s "$IQ" ]]; then
  echo "wxsat: no IQ captured" >&2
  exit 12
fi

# Decoding is offline and does NOT need the SDR — restart the stream now so the
# radio is down only for the capture window, not the (longer) decode.
sudo /usr/bin/systemctl start sdr-fm@active >/dev/null 2>&1 || true

echo "wxsat: decoding $(du -h "$IQ" | cut -f1) baseband with ${LRPT_PIPELINE}"
satdump "$LRPT_PIPELINE" baseband "$IQ" "$OUT" \
  --samplerate "$SAMPLERATE" --baseband_format cs16
drc=$?
rm -f "$IQ"
if [[ $drc -ne 0 ]]; then
  echo "wxsat: satdump decode failed (rc=$drc)" >&2
  exit 13
fi
echo "wxsat: capture complete -> $OUT"
