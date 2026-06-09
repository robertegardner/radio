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
#   LRPT_PIPELINE_FALLBACK  second pipeline to try if the first yields no frames
#                           (default: the other of 72k/80k Meteor LRPT)
#   WXSAT_KEEP_IQ_ON_FAIL   1 = retain the raw baseband when NO pipeline syncs,
#                           for offline post-mortem (default 1); 0 = always drop.
#   WXSAT_KEEP_IQ_ALWAYS    1 = retain the raw baseband on EVERY pass, even on a
#                           successful decode (debug mode — lets you re-decode /
#                           spectrum-inspect a good pass too). Default 0. Each
#                           retained IQ is large (~4 MB/s of capture); turn this
#                           off once we have a confirmed-good capture.
#
# Every pass writes a full $OUT/capture.log (rx_sdr + SatDump output + this
# script's trace) so the decode can be diagnosed offline regardless of outcome.
#
# Decode strategy: M2-4 has been seen at both 72k and 80k symbol rates, and a
# rate mismatch looks identical to "no signal" (flat 0 dB SNR, Viterbi NOSYNC).
# So we decode with the primary pipeline, and if it produces an empty CADU we
# retry with the fallback rate before declaring failure. On total failure we
# keep the IQ so the pass can be re-decoded / spectrum-inspected later (the
# scheduler's free-space prune reclaims these dirs when disk gets tight).
#
# Exit codes: 0 ok; 11 rx_sdr failed; 12 no IQ; 13 no pipeline synced.

set -uo pipefail

OUT="${WXSAT_OUT_DIR:?WXSAT_OUT_DIR required}"
DUR="${WXSAT_DURATION:?WXSAT_DURATION required}"
FREQ_MHZ="${FREQ_MHZ:-137.9}"
ANTENNA="${ANTENNA:-Antenna B}"
SAMPLERATE="${SAMPLERATE:-1000000}"
GAIN="${GAIN:-40}"
LRPT_PIPELINE="${LRPT_PIPELINE:-meteor_m2-x_lrpt}"
KEEP_IQ_ON_FAIL="${WXSAT_KEEP_IQ_ON_FAIL:-1}"
KEEP_IQ_ALWAYS="${WXSAT_KEEP_IQ_ALWAYS:-0}"
MIN_FREE_GB="${WXSAT_MIN_FREE_GB:-2}"
export HOME="${HOME:-/var/lib/sdr-streams/wxsat}"
TLE_DIR="/var/lib/sdr-streams/wxsat/tle"
# Parent of all per-pass capture dirs (the scheduler creates OUT as
# WXSAT_DIR/<timestamp>); used to reclaim old IQ across passes.
WXSAT_DIR="$(dirname "$OUT")"

# Whole-GB free space on the filesystem holding the capture dir. Used as a hard
# floor: retaining a multi-GB IQ must never push the SD card to full (a full
# root breaks the Flask app's state writes — see CLAUDE.md disk-pressure notes).
free_gb() { df -BG --output=avail "$OUT" 2>/dev/null | tail -1 | tr -dc '0-9'; }

# Fallback pipeline: explicit env wins, else swap 72k<->80k automatically.
FALLBACK_PIPELINE="${LRPT_PIPELINE_FALLBACK:-}"
if [[ -z "$FALLBACK_PIPELINE" ]]; then
  if [[ "$LRPT_PIPELINE" == *_80k ]]; then
    FALLBACK_PIPELINE="meteor_m2-x_lrpt"
  else
    FALLBACK_PIPELINE="meteor_m2-x_lrpt_80k"
  fi
fi

IQ="$OUT/baseband.cs16"
# KEEP_IQ gates whether the trap/end preserves the raw baseband. It starts at
# KEEP_IQ_ALWAYS (debug mode keeps every pass) and is force-set to 1 on a decode
# failure so a no-sync pass is always retained for post-mortem.
KEEP_IQ="$KEEP_IQ_ALWAYS"
mkdir -p "$OUT"

# Tee everything (this script's trace, rx_sdr stderr, SatDump's full decode log)
# to a per-pass log so any pass can be diagnosed offline regardless of outcome.
# Output still flows to our stdout, so the scheduler's capture_output is intact.
LOG="$OUT/capture.log"
exec > >(tee -a "$LOG") 2>&1
echo "wxsat: capture starting $(date -u +%Y-%m-%dT%H:%M:%SZ) — keep_iq_always=${KEEP_IQ_ALWAYS} keep_iq_on_fail=${KEEP_IQ_ON_FAIL}"

# ALWAYS bring the broadcast stream back (rule #1). Drop the bulky raw IQ too —
# UNLESS a decode failure asked us to keep it for post-mortem. Runs on every
# exit path (normal, error, signal, or SatDump hang/kill).
cleanup() {
  # Stop the live-telemetry sidecar (best-effort; it never holds the SDR).
  [[ -n "${LIVE_PID:-}" ]] && kill "$LIVE_PID" 2>/dev/null || true
  # Hard disk floor: even when asked to keep the IQ, drop it if doing so would
  # leave the filesystem under MIN_FREE_GB. The capture.log + decoded products
  # (both small) always survive — only the multi-GB baseband is sacrificed.
  if [[ "$KEEP_IQ" == "1" && -s "$IQ" ]]; then
    free="$(free_gb)"
    if [[ -n "$free" && "$free" -lt "$MIN_FREE_GB" ]]; then
      echo "wxsat: only ${free}G free (< ${MIN_FREE_GB}G floor) — dropping IQ despite keep request" >&2
      KEEP_IQ=0
    fi
  fi
  if [[ "$KEEP_IQ" != "1" ]]; then
    rm -f "$IQ"
  fi
  sudo /usr/bin/systemctl start sdr-fm@active >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

# Decode the recorded IQ with one pipeline. Returns 0 only if SatDump synced
# frames (non-empty <pipeline>.cadu); a 0-byte CADU means NOSYNC / wrong rate.
decode_with() {
  local pl="$1"
  echo "wxsat: decoding $(du -h "$IQ" | cut -f1) baseband with ${pl}"
  satdump "$pl" baseband "$IQ" "$OUT" \
    --samplerate "$SAMPLERATE" --baseband_format cs16
  local cadu="$OUT/${pl}.cadu"
  if [[ -s "$cadu" ]]; then
    echo "wxsat: ${pl} synced $(stat -c%s "$cadu") bytes of CADUs"
    return 0
  fi
  echo "wxsat: ${pl} produced no frames (empty CADU)" >&2
  return 1
}

# Seed SatDump's TLE file from our cache so georeferenced products have fresh
# elements. (SatDump's own celestrak fetch is dead on this Pi — see CLAUDE.md;
# the /etc/hosts redirect makes it fast-fail and proceed.)
mkdir -p "$HOME/.config/satdump"
if ls "$TLE_DIR"/*.tle >/dev/null 2>&1; then
  cat "$TLE_DIR"/*.tle > "$HOME/.config/satdump/satdump_tles.txt"
fi

# Make room before capturing: rx_sdr will write ~SAMPLERATE*4 bytes/s, and a
# full SD card breaks the Flask app. Reclaim space by deleting the OLDEST
# *retained* baseband.cs16 files first (small capture.logs + decoded products
# are always preserved) until we have headroom for this capture plus the floor.
need_gb=$(( (DUR * SAMPLERATE * 4 / 1000000000) + MIN_FREE_GB + 1 ))
while [[ "$(free_gb)" =~ ^[0-9]+$ && "$(free_gb)" -lt "$need_gb" ]]; do
  oldest="$(ls -1tr "$WXSAT_DIR"/*/baseband.cs16 2>/dev/null | grep -vF -- "$IQ" | head -1)"
  [[ -z "$oldest" ]] && { echo "wxsat: low disk ($(free_gb)G < ${need_gb}G) and no old IQ to reclaim" >&2; break; }
  echo "wxsat: low disk ($(free_gb)G < ${need_gb}G) — reclaiming old IQ: $oldest" >&2
  rm -f "$oldest"
done

# Live per-pass telemetry for the /wxsat page (spectrum/level while recording,
# decode progress/SNR while decoding). Best-effort: failure here never affects
# the capture. Runs for our whole lifetime; the cleanup trap kills it.
python3 /opt/sdr-tuner/wxsat_live.py &
LIVE_PID=$!

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

# Try the primary symbol rate; on no-sync, retry the fallback rate before giving
# up. Both decode into $OUT — a successful retry's products simply win.
if decode_with "$LRPT_PIPELINE"; then
  :
elif [[ "$FALLBACK_PIPELINE" != "$LRPT_PIPELINE" ]] && decode_with "$FALLBACK_PIPELINE"; then
  echo "wxsat: primary ${LRPT_PIPELINE} found nothing; fallback ${FALLBACK_PIPELINE} synced"
else
  if [[ "$KEEP_IQ_ON_FAIL" == "1" ]]; then
    KEEP_IQ=1
    echo "wxsat: no pipeline synced — retaining IQ for post-mortem: $IQ" >&2
  else
    echo "wxsat: no pipeline synced (IQ not retained)" >&2
  fi
  exit 13
fi

if [[ "$KEEP_IQ_ALWAYS" == "1" ]]; then
  echo "wxsat: decode OK — retaining IQ (debug mode): $IQ"
else
  rm -f "$IQ"
fi
echo "wxsat: capture complete -> $OUT"
