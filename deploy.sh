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

install -m 0755 -o radio -g radio "$SRC/opt/sdr-tuner/stream.sh"       /opt/sdr-tuner/stream.sh
install -m 0755 -o radio -g radio "$SRC/opt/sdr-tuner/app.py"          /opt/sdr-tuner/app.py
install -m 0755 -o radio -g radio "$SRC/opt/sdr-tuner/station_db.py"   /opt/sdr-tuner/station_db.py
install -m 0755 -o radio -g radio "$SRC/opt/sdr-tuner/hd_stream.py"    /opt/sdr-tuner/hd_stream.py
install -m 0644 -o radio -g radio "$SRC/opt/sdr-tuner/templates/radio.html" /opt/sdr-tuner/templates/radio.html
install -m 0644 -o radio -g radio "$SRC/opt/sdr-tuner/templates/index.html" /opt/sdr-tuner/templates/index.html

echo "Restarting services..."
systemctl restart sdr-tuner.service
systemctl restart sdr-fm@active.service

echo "Done. UI: http://$(hostname -I | awk '{print $1}'):8080/radio"
