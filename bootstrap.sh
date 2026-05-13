#!/bin/bash
# bootstrap.sh - install the SDR radio stack on a fresh Raspberry Pi OS.
#
# Run from the unpacked sdr-radio-stack/ directory:
#   sudo ./bootstrap.sh
#
# Idempotent: safe to re-run. Will not overwrite existing config files
# in /etc/sdr-streams/ (only the .example versions are written there).

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "This script must be run as root (use sudo)." >&2
  exit 1
fi

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
SRC="$REPO_DIR/files"

echo
echo "=========================================="
echo "  SDR Radio Stack Installer"
echo "=========================================="
echo

# ---------------------------------------------------------------------------
# 1. APT packages
# ---------------------------------------------------------------------------
echo "[1/9] Installing apt packages..."
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y \
  rtl-sdr \
  ffmpeg \
  icecast2 \
  python3 python3-flask python3-requests \
  build-essential git \
  meson ninja-build pkg-config \
  libsndfile1-dev libliquid-dev nlohmann-json3-dev \
  libchromaprint-tools \
  jq curl

# ---------------------------------------------------------------------------
# 2. Blacklist DVB driver
# ---------------------------------------------------------------------------
echo "[2/9] Blacklisting kernel DVB driver..."
cat >/etc/modprobe.d/blacklist-rtl.conf <<'EOF'
blacklist dvb_usb_rtl28xxu
blacklist rtl2832
blacklist rtl2830
EOF
modprobe -r dvb_usb_rtl28xxu 2>/dev/null || true
modprobe -r rtl2832 2>/dev/null || true

# ---------------------------------------------------------------------------
# 3. Build redsea from source (Meson build system)
# ---------------------------------------------------------------------------
if ! command -v redsea >/dev/null; then
  echo "[3/9] Building redsea (Meson)..."
  TMP=$(mktemp -d)
  git clone --depth 1 https://github.com/windytan/redsea.git "$TMP/redsea"
  (
    cd "$TMP/redsea"
    meson setup build
    cd build
    meson compile -j 2
    meson install
  )
  ldconfig
  rm -rf "$TMP"
else
  echo "[3/9] redsea already installed at $(which redsea), skipping build."
fi

# ---------------------------------------------------------------------------
# 4. Create radio user + directories
# ---------------------------------------------------------------------------
echo "[4/9] Setting up radio user and directories..."
if ! id radio >/dev/null 2>&1; then
  useradd --system --create-home --shell /bin/bash --groups plugdev radio
else
  usermod -aG plugdev radio
fi
if [[ -n "${SUDO_USER:-}" && "$SUDO_USER" != "root" ]]; then
  usermod -aG plugdev "$SUDO_USER" || true
fi

install -d -m 0755 -o radio -g radio /opt/sdr-tuner
install -d -m 0755 -o radio -g radio /opt/sdr-tuner/templates
install -d -m 0755 -o radio -g radio /var/lib/sdr-streams
install -d -m 0775 -o root  -g radio /etc/sdr-streams

# ---------------------------------------------------------------------------
# 5. Install application files
# ---------------------------------------------------------------------------
echo "[5/9] Installing application files..."
for f in stream.sh hd_stream.py rds_watcher.py fm_scan.py am_scan.py app.py \
         caption_orchestrator.py station_db.py fcc_fetch.py ui_settings.py; do
  install -m 0755 -o radio -g radio "$SRC/opt/sdr-tuner/$f" "/opt/sdr-tuner/$f"
done
for t in index.html radio.html; do
  install -m 0644 -o radio -g radio "$SRC/opt/sdr-tuner/templates/$t" "/opt/sdr-tuner/templates/$t"
done

# ---------------------------------------------------------------------------
# 6. Config templates
# ---------------------------------------------------------------------------
echo "[6/9] Installing config templates..."
# active.env needs to be writable by the radio user (Flask updates it)
install -m 0660 -o radio -g radio "$SRC/etc/sdr-streams/active.env.example" /etc/sdr-streams/active.env.example
if [[ ! -f /etc/sdr-streams/active.env ]]; then
  install -m 0660 -o radio -g radio "$SRC/etc/sdr-streams/active.env.example" /etc/sdr-streams/active.env
fi

# tuner.env + captions.env are read-only at runtime
for f in tuner.env captions.env; do
  install -m 0640 -o root -g radio "$SRC/etc/sdr-streams/${f}.example" "/etc/sdr-streams/${f}.example"
  if [[ ! -f "/etc/sdr-streams/$f" ]]; then
    install -m 0640 -o root -g radio "$SRC/etc/sdr-streams/${f}.example" "/etc/sdr-streams/$f"
  fi
done

# overrides.json template
install -m 0664 -o root -g radio "$SRC/etc/sdr-streams/overrides.json.example" /etc/sdr-streams/overrides.json.example
if [[ ! -f /etc/sdr-streams/overrides.json ]]; then
  echo '{"fm": {}, "am": {}}' > /etc/sdr-streams/overrides.json
  chown root:radio /etc/sdr-streams/overrides.json
  chmod 0664 /etc/sdr-streams/overrides.json
fi

# ---------------------------------------------------------------------------
# 7. systemd units + sudoers
# ---------------------------------------------------------------------------
echo "[7/9] Installing systemd units and sudoers..."
for unit in sdr-fm@.service sdr-tuner.service sdr-scan.service \
            sdr-am-scan.service sdr-captions.service; do
  install -m 0644 -o root -g root "$SRC/etc/systemd/system/$unit" "/etc/systemd/system/$unit"
done

install -m 0440 -o root -g root "$SRC/etc/sudoers.d/sdr-tuner" /etc/sudoers.d/sdr-tuner
visudo -cf /etc/sudoers.d/sdr-tuner

systemctl daemon-reload

# ---------------------------------------------------------------------------
# 8. Icecast
# ---------------------------------------------------------------------------
echo "[8/9] Icecast..."
systemctl enable icecast2
systemctl start  icecast2 || true

# ---------------------------------------------------------------------------
# 9. Enable our services (but don't start until configs are filled in)
# ---------------------------------------------------------------------------
echo "[9/9] Enabling SDR services..."
systemctl enable sdr-tuner.service
systemctl enable sdr-fm@active.service
systemctl enable sdr-captions.service

cat <<'EOF'

==========================================
  Install complete.
==========================================

REMAINING STEPS:

  1. Configure Icecast passwords:
       sudo dpkg-reconfigure icecast2
       sudo systemctl restart icecast2

  2. Edit /etc/sdr-streams/active.env and tuner.env, setting
     ICECAST_PASS to match Icecast's source password from step 1.

  3. Edit /etc/sdr-streams/captions.env with WHISPER_URL, WHISPER_TOKEN
     (from your GPU host), and ACOUSTID_KEY (free from acoustid.org).

  4. Fetch station database (optional but recommended):
       sudo -u radio python3 /opt/sdr-tuner/fcc_fetch.py \
         --lat YOUR_LAT --lon YOUR_LON --max-km 400

  5. Verify the dongle:
       rtl_test -t

  6. Start the stack:
       sudo systemctl start sdr-tuner sdr-fm@active sdr-captions

  7. Open the admin UI:    http://<pi-ip>:8080
     Open the radio UI:    http://<pi-ip>:8080/radio
     Click "Scan FM" to populate the station list.

  See README.md for full setup and configuration details.

EOF
