#!/bin/bash
# Deploy updated files from the repo to the installed stack and restart services.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo ./deploy.sh" >&2
  exit 1
fi

REPO="$(cd "$(dirname "$0")" && pwd)"
SRC="$REPO/files"

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
# wxsat_capture.sh ships in Phase 2 (real capture); install only if present.
[[ -f "$SRC/opt/sdr-tuner/wxsat_capture.sh" ]] && \
  install -m 0755 -o radio -g radio "$SRC/opt/sdr-tuner/wxsat_capture.sh"     /opt/sdr-tuner/wxsat_capture.sh
install -m 0644 -o radio -g radio "$SRC/opt/sdr-tuner/templates/radio.html"   /opt/sdr-tuner/templates/radio.html
install -m 0644 -o radio -g radio "$SRC/opt/sdr-tuner/templates/index.html"   /opt/sdr-tuner/templates/index.html
install -m 0644 -o radio -g radio "$SRC/opt/sdr-tuner/templates/wxsat.html"   /opt/sdr-tuner/templates/wxsat.html

echo "Restarting services..."
systemctl restart sdr-tuner.service
systemctl restart sdr-fm@active.service
# Restart the wxsat scheduler only if it's installed (Phase 1+).
systemctl list-unit-files wxsat-scheduler.service >/dev/null 2>&1 && \
  systemctl restart wxsat-scheduler.service 2>/dev/null || true

echo "Done. UI: http://$(hostname -I | awk '{print $1}'):8080/radio"
