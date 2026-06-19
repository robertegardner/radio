#!/bin/bash
# Deploy updated files from the repo to the installed stack and restart services.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo ./deploy.sh" >&2
  exit 1
fi

REPO="$(cd "$(dirname "$0")" && pwd)"
SRC="$REPO/files"

# --rack: deploy to radio-compute (.84), where FM DSP runs as wbfm_stream.py over
# the remote dx-R2 (V2 cutover 2026-06-14). Installs the APP payload only and
# leaves the audio stream (sdr-fm@active) running. Deliberately SKIPS:
#   - stream.sh  : the rack variant is platform-managed (calls wbfm_stream.py, not
#                  rx_fm); this repo's stream.sh is the Pi/local driver=sdrplay one
#                  and would break V2 audio.
#   - Pi-only units (sdr-iq-capture/sdr-mux) + sudoers : rack sudoers is
#                  platform-managed; FM-multistation units are Pi-side.
# Run ON .84 (root) from a checkout/synced tree: sudo ./deploy.sh --rack
if [[ "${1:-}" == "--rack" ]]; then
  echo "Deploying APP payload to the rack (radio-compute, .84) — stream.sh + Pi-only units skipped..."
  for f in app.py station_db.py hd_stream.py rds_watcher.py fcc_fetch.py fm_scan.py \
           am_scan.py am_scan_merge.py am_scan_all.sh am_stream.py am_diag_scan.py \
           caption_orchestrator.py ui_settings.py wbfm_stream.py stereo_decode.py; do
    install -m 0755 -o radio -g radio "$SRC/opt/sdr-tuner/$f" "/opt/sdr-tuner/$f"
  done
  install -d -m 0755 -o radio -g radio /opt/sdr-tuner/templates
  for t in radio.html index.html wxsat.html multi.html; do
    [[ -f "$SRC/opt/sdr-tuner/templates/$t" ]] && \
      install -m 0644 -o radio -g radio "$SRC/opt/sdr-tuner/templates/$t" "/opt/sdr-tuner/templates/$t"
  done
  install -d -m 0755 -o radio -g radio /opt/sdr-tuner/static
  install -m 0644 -o radio -g radio "$SRC"/opt/sdr-tuner/static/*.js /opt/sdr-tuner/static/
  systemctl restart sdr-tuner.service
  systemctl list-unit-files sdr-captions.service >/dev/null 2>&1 && \
    systemctl restart sdr-captions.service 2>/dev/null || true
  echo "Rack deploy done (audio sdr-fm@active left running)."
  echo "UI: http://$(hostname -I | awk '{print $1}'):8080/radio"
  exit 0
fi

echo "Deploying SDR Radio stack changes..."

install -m 0755 -o radio -g radio "$SRC/opt/sdr-tuner/stream.sh"              /opt/sdr-tuner/stream.sh
install -m 0755 -o radio -g radio "$SRC/opt/sdr-tuner/app.py"                 /opt/sdr-tuner/app.py
install -m 0755 -o radio -g radio "$SRC/opt/sdr-tuner/station_db.py"          /opt/sdr-tuner/station_db.py
install -m 0755 -o radio -g radio "$SRC/opt/sdr-tuner/hd_stream.py"           /opt/sdr-tuner/hd_stream.py
install -m 0755 -o radio -g radio "$SRC/opt/sdr-tuner/rds_watcher.py"         /opt/sdr-tuner/rds_watcher.py
install -m 0755 -o radio -g radio "$SRC/opt/sdr-tuner/fcc_fetch.py"           /opt/sdr-tuner/fcc_fetch.py
install -m 0755 -o radio -g radio "$SRC/opt/sdr-tuner/fm_scan.py"             /opt/sdr-tuner/fm_scan.py
install -m 0755 -o radio -g radio "$SRC/opt/sdr-tuner/am_scan.py"             /opt/sdr-tuner/am_scan.py
install -m 0755 -o radio -g radio "$SRC/opt/sdr-tuner/am_stream.py"           /opt/sdr-tuner/am_stream.py
install -m 0755 -o radio -g radio "$SRC/opt/sdr-tuner/am_diag_scan.py"        /opt/sdr-tuner/am_diag_scan.py
install -m 0755 -o radio -g radio "$SRC/opt/sdr-tuner/caption_orchestrator.py" /opt/sdr-tuner/caption_orchestrator.py
install -m 0755 -o radio -g radio "$SRC/opt/sdr-tuner/ui_settings.py"         /opt/sdr-tuner/ui_settings.py
install -m 0755 -o radio -g radio "$SRC/opt/sdr-tuner/wxsat_predict.py"       /opt/sdr-tuner/wxsat_predict.py
install -m 0755 -o radio -g radio "$SRC/opt/sdr-tuner/wxsat_scheduler.py"     /opt/sdr-tuner/wxsat_scheduler.py
install -m 0755 -o radio -g radio "$SRC/opt/sdr-tuner/wxsat_live.py"          /opt/sdr-tuner/wxsat_live.py
install -m 0755 -o radio -g radio "$SRC/opt/sdr-tuner/wxsat_rebuild.py"       /opt/sdr-tuner/wxsat_rebuild.py
install -m 0755 -o radio -g radio "$SRC/opt/sdr-tuner/wxsat_cn_check.py"      /opt/sdr-tuner/wxsat_cn_check.py
install -m 0755 -o radio -g radio "$SRC/opt/sdr-tuner/iq_capture.py"          /opt/sdr-tuner/iq_capture.py
install -m 0755 -o radio -g radio "$SRC/opt/sdr-tuner/stereo_decode.py"       /opt/sdr-tuner/stereo_decode.py
install -m 0755 -o radio -g radio "$SRC/opt/sdr-tuner/channel_pipeline.sh"    /opt/sdr-tuner/channel_pipeline.sh
# mux_supervisor.py ships with FM-multistation Phase 2; install only if present.
[[ -f "$SRC/opt/sdr-tuner/mux_supervisor.py" ]] && \
  install -m 0755 -o radio -g radio "$SRC/opt/sdr-tuner/mux_supervisor.py"    /opt/sdr-tuner/mux_supervisor.py
# wxsat_capture.sh ships in Phase 2 (real capture); install only if present.
[[ -f "$SRC/opt/sdr-tuner/wxsat_capture.sh" ]] && \
  install -m 0755 -o radio -g radio "$SRC/opt/sdr-tuner/wxsat_capture.sh"     /opt/sdr-tuner/wxsat_capture.sh
install -m 0644 -o radio -g radio "$SRC/opt/sdr-tuner/templates/radio.html"   /opt/sdr-tuner/templates/radio.html
install -m 0644 -o radio -g radio "$SRC/opt/sdr-tuner/templates/index.html"   /opt/sdr-tuner/templates/index.html
install -m 0644 -o radio -g radio "$SRC/opt/sdr-tuner/templates/wxsat.html"   /opt/sdr-tuner/templates/wxsat.html
install -m 0644 -o radio -g radio "$SRC/opt/sdr-tuner/templates/multi.html"   /opt/sdr-tuner/templates/multi.html

# Vendored client-side JS (Butterchurn visualizer) served by Flask at /static.
install -d -m 0755 -o radio -g radio /opt/sdr-tuner/static
install -m 0644 -o radio -g radio "$SRC"/opt/sdr-tuner/static/*.js            /opt/sdr-tuner/static/

# FM-multistation units (opt-in; NOT started here — legacy mono stays default).
install -m 0644 -o root -g root "$SRC/etc/systemd/system/sdr-iq-capture.service" /etc/systemd/system/sdr-iq-capture.service
[[ -f "$SRC/etc/systemd/system/sdr-mux.service" ]] && \
  install -m 0644 -o root -g root "$SRC/etc/systemd/system/sdr-mux.service" /etc/systemd/system/sdr-mux.service
# sudoers may have gained new verbs (sdr-iq-capture / sdr-mux).
install -m 0440 -o root -g root "$SRC/etc/sudoers.d/sdr-tuner" /etc/sudoers.d/sdr-tuner
visudo -cf /etc/sudoers.d/sdr-tuner
systemctl daemon-reload

echo "Restarting services..."
systemctl restart sdr-tuner.service
systemctl restart sdr-fm@active.service
# Restart the wxsat scheduler only if it's installed (Phase 1+).
systemctl list-unit-files wxsat-scheduler.service >/dev/null 2>&1 && \
  systemctl restart wxsat-scheduler.service 2>/dev/null || true

echo "Done. UI: http://$(hostname -I | awk '{print $1}'):8080/radio"
