#!/bin/bash
# Full AM antenna survey for the rack tuner: dx-R2 ports A/B/C + the HF+ YouLoop,
# merged into stations_am.json (the per-antenna SNR drives the admin table's
# A/B/C/HF+ columns). Runs as the sdr-am-scan.service ExecStart; FM is stopped by
# the unit's ExecStartPre and restored by its ExecStopPost, so this script only
# does the two sweeps + merge. am_scan can only target one device per run, hence
# the two passes.
set -uo pipefail
RUN=/run/sdr-streams
OUT=/var/lib/sdr-streams/stations_am.json
DXR2="$RUN/scan-dxr2.json"
HFP="$RUN/scan-hfplus.json"
GAIN=30   # matches the 2026-06-17 bring-up survey so SNRs stay comparable
mkdir -p "$RUN"
rm -f "$DXR2" "$HFP"

# Let the dx-R2 source server settle after FM released it.
sleep 3

echo "am-scan-all: sweeping dx-R2 ports A/B/C" >&2
python3 /opt/sdr-tuner/am_scan.py --antennas "Antenna A,Antenna B,Antenna C" \
  --gain "$GAIN" --out "$DXR2" || true

# HF+ YouLoop (its own device, :55002). Free it from wx-stream if that's running
# (best-effort — needs a sudoers grant; WX is offline during the AM-survey era).
WX=$(systemctl is-active wx-stream.service 2>/dev/null || true)
if [ "$WX" = active ]; then sudo systemctl stop wx-stream.service 2>/dev/null || true; fi
echo "am-scan-all: sweeping HF+ YouLoop" >&2
python3 /opt/sdr-tuner/am_scan.py \
  --device-args driver=remote,remote=radio.srvr:55002,remote:driver=airspyhf \
  --rate 912000 --antennas RX --gain "$GAIN" --out "$HFP" || true
if [ "$WX" = active ]; then sudo systemctl start wx-stream.service 2>/dev/null || true; fi

echo "am-scan-all: merging" >&2
python3 /opt/sdr-tuner/am_scan_merge.py "$DXR2" "$HFP" "$OUT"
