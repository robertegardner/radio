#!/bin/bash
# channel_pipeline.sh — one FM channel out of the shared wideband IQ stream.
#
# Subscribes to iq_capture's IQ socket, shifts the wanted station to DC,
# decimates to the 250 kHz composite (MPX), FM-demodulates, optionally stereo-
# decodes and/or extracts RDS, encodes to MP3, and sources it to an Icecast
# mount. An extra station is just another instance of this script reading the
# same socket — that is the whole point of the IQ refactor.
#
# Phase 1 runs ONE hardcoded channel (mux.env PHASE1_*). Phase 2's
# mux_supervisor.py launches one of these per channels.json entry, passing args.
#
# Usage: channel_pipeline.sh [FREQ_MHZ] [MOUNT] [STEREO 0|1] [BITRATE] [RDS 0|1] [PRIMARY 0|1]
#   defaults come from mux.env PHASE1_* so Phase 1 needs no args.
#
# The heavy DSP (8 Msps shift/decimate/demod) runs in csdr (C/SIMD); only the
# stereo matrix is Python (vectorized numpy). De-emphasis + resample to 48 kHz
# are ffmpeg's job, same as the legacy mono path. RDS (when enabled) tees the
# composite to redsea -> rds_watcher.py.
set -uo pipefail

source /etc/sdr-streams/mux.env
# ICECAST_PASS lives in active.env (already radio-readable, 0660). Reusing it
# avoids duplicating the source secret into mux.env.
source /etc/sdr-streams/active.env

FREQ_MHZ="${1:-${PHASE1_FREQ_MHZ:-100.7}}"
MOUNT="${2:-${PHASE1_MOUNT:-test.mp3}}"
STEREO="${3:-${PHASE1_STEREO:-1}}"
BITRATE="${4:-${PHASE1_BITRATE:-192k}}"
RDS="${5:-${PHASE1_RDS:-0}}"
PRIMARY="${6:-0}"

CENTER_MHZ="${CENTER_MHZ:-98.0}"
FS="${FS:-8000000}"
# Two-stage decimation 8 Msps -> 250 kHz (/8 then /4). A single /32 stage needs
# an ~800-tap FIR (narrow transition at 8 Msps) and was the top CPU hog at 4
# channels. A cascade is ~2x cheaper: stage 1 decimates the most with a cheap
# WIDE-transition filter (it only has to keep aliases out of the final keep
# band), and the sharp adjacent-channel rejection is done by stage 2 at the low
# rate. DECIM1*DECIM2 must equal the old /32.
DECIM1="${DECIM1:-8}"
DECIM2="${DECIM2:-4}"
TRANS1="${DECIM_TRANS1:-0.05}"
TRANS2="${DECIM_TRANS2:-0.05}"
COMPOSITE_RATE=$(( FS / (DECIM1 * DECIM2) ))   # 250000

# csdr shift_addfast_cc multiplies by e^{+j2π·rate·n}, so to bring a station at
# (FREQ-CENTER) down to DC we shift by the negative of that, normalized to FS.
SHIFT=$(python3 -c "print((${CENTER_MHZ}-${FREQ_MHZ})*1e6/${FS})")

ICECAST_URL="icecast://source:${ICECAST_PASS}@localhost:8000/${MOUNT}"

# now_playing target: primary writes the legacy name (existing UI + captions);
# others write now_playing-<mount>.json (served via /api/now_playing?mount=).
BASE="${MOUNT%.mp3}"
if [[ "$PRIMARY" == "1" ]]; then
  NP_PATH="/run/sdr-streams/now_playing.json"
else
  NP_PATH="/run/sdr-streams/now_playing-${BASE}.json"
fi

echo "channel_pipeline: ${FREQ_MHZ} MHz -> /${MOUNT}  stereo=${STEREO} rds=${RDS} " \
     "primary=${PRIMARY} bitrate=${BITRATE} shift=${SHIFT} composite=${COMPOSITE_RATE}" >&2

# Front end shared by every variant: subscribe → shift → /DECIM decimate → FM demod.
# Output is the real MPX composite at COMPOSITE_RATE (float32).
COMPOSITE="python3 /opt/sdr-tuner/iq_capture.py --subscribe \
  | csdr shift_addfast_cc ${SHIFT} \
  | csdr fir_decimate_cc ${DECIM1} ${TRANS1} HAMMING \
  | csdr fir_decimate_cc ${DECIM2} ${TRANS2} HAMMING \
  | csdr fmdemod_quadri_cf"

# RDS: tee the composite to redsea (needs s16 MPX at COMPOSITE_RATE; the 57 kHz
# subcarrier is well inside the 0-125 kHz composite). rds_watcher writes NP_PATH.
if [[ "$RDS" == "1" ]]; then
  COMPOSITE="${COMPOSITE} | tee >(csdr convert_f_s16 \
    | redsea -r ${COMPOSITE_RATE} --output json 2>/dev/null \
    | NOW_PLAYING_PATH='${NP_PATH}' FREQ='${FREQ_MHZ}M' python3 /opt/sdr-tuner/rds_watcher.py >/dev/null 2>&1)"
fi

if [[ "$STEREO" == "1" ]]; then
  # Composite → stereo matrix (s16le stereo @ COMPOSITE_RATE) → ffmpeg deemph+resample.
  exec bash -c "${COMPOSITE} \
    | python3 /opt/sdr-tuner/stereo_decode.py --scale ${STEREO_SCALE:-3.0} --pilot-floor ${STEREO_PILOT_FLOOR:-0.003} \
    | ffmpeg -hide_banner -loglevel warning -f s16le -ar ${COMPOSITE_RATE} -ac 2 -i - \
             -af 'aemphasis=mode=reproduction:type=75fm,lowpass=15000' \
             -ar 48000 -ac 2 \
             -c:a libmp3lame -b:a ${BITRATE} -content_type audio/mpeg \
             -f mp3 '${ICECAST_URL}'"
else
  # Mono: feed the raw composite straight to ffmpeg (lowpass 15k extracts L+R).
  # volume lifts csdr's small fmdemod output to a sane level (tune with MONO_SCALE).
  exec bash -c "${COMPOSITE} \
    | ffmpeg -hide_banner -loglevel warning -f f32le -ar ${COMPOSITE_RATE} -ac 1 -i - \
             -af 'volume=${MONO_SCALE:-4.0},aemphasis=mode=reproduction:type=75fm,lowpass=15000' \
             -ar 48000 -ac 1 \
             -c:a libmp3lame -b:a ${BITRATE} -content_type audio/mpeg \
             -f mp3 '${ICECAST_URL}'"
fi
