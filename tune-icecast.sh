#!/bin/bash
# tune-icecast.sh - deepen Icecast's client jitter buffer.
#
# Default Icecast bursts only 64K (~4s @128k) on connect, which is a thin
# cushion for listeners on weak/remote networks: the browser <audio> element
# underruns on a wifi blip and chops. Bumping the burst hands each (re)connect
# a much deeper buffer to coast on, and a bigger queue keeps Icecast from
# dropping a laggy client before it catches up.
#
#   burst-size  64K -> 256K  (~16s of instant buffer per connect @128k)
#   queue-size 512K -> 1M
#
# Trade-off: ~16s more latency, which is fine for radio.
#
# Idempotent: safe to re-run. In particular re-run it after
# `dpkg-reconfigure icecast2` (which rewrites the passwords but leaves these
# values alone) and after any fresh Icecast reinstall / SD-card migration.
#
#   sudo ./tune-icecast.sh [/path/to/icecast.xml]

set -euo pipefail

XML=${1:-/etc/icecast2/icecast.xml}
BURST=262144     # 256 KiB
QUEUE=1048576    # 1 MiB

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo ./tune-icecast.sh" >&2
  exit 1
fi
if [[ ! -f "$XML" ]]; then
  echo "Icecast config not found: $XML" >&2
  exit 1
fi

BAK="$XML.bak-$(date +%Y%m%d%H%M%S)"
cp -a "$XML" "$BAK"

# Replace only the FIRST occurrence of each tag — the one in <limits>. The
# package default also ships a second <burst-size> inside a commented-out
# <mount> example, which we deliberately leave untouched.
sed -i \
  -e "0,/<burst-size>[0-9]*<\/burst-size>/s//<burst-size>${BURST}<\/burst-size>/" \
  -e "0,/<queue-size>[0-9]*<\/queue-size>/s//<queue-size>${QUEUE}<\/queue-size>/" \
  "$XML"

# Refuse to leave a broken config behind.
if ! python3 -c "import xml.dom.minidom; xml.dom.minidom.parse('$XML')" 2>/dev/null; then
  echo "ERROR: edit produced invalid XML; restoring $BAK" >&2
  cp -a "$BAK" "$XML"
  exit 1
fi

echo "Tuned $XML:"
grep -nE "<burst-size>|<queue-size>" "$XML" | head

if systemctl is-active --quiet icecast2; then
  systemctl restart icecast2 && echo "Restarted icecast2."
else
  echo "icecast2 not running — values take effect on next start."
fi
