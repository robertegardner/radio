#!/usr/bin/env bash
# device_loss_guard.sh — self-heal for SDR device loss.
#
# Reads an SDR process's stderr on stdin, forwards every line to the journal,
# and when the device-removal marker appears, SIGTERMs the stream service's
# main process so systemd (Restart=always, KillMode=control-group) tears the
# pipeline down and restarts it, re-acquiring the device.
#
# Why this exists: this rx_fm / SoapySDR build, on SDRplay USB loss, prints
# "[ERROR] Device has been removed. Stopping." in an infinite loop WITHOUT
# exiting. The pipeline therefore never dies, so systemd never restarts it and
# the stream stays dead until a manual restart. (Most often triggered when
# another process — e.g. the scanner's SDRTrunk — enumerates the SDRplay.)
#
# Usage, in the streaming pipeline:
#     rx_fm ... - 2> >(/opt/sdr-tuner/device_loss_guard.sh "$$") | ... | ffmpeg ...
# where "$$" is the PID of the service's main shell to terminate on loss.
set -u

TARGET_PID="${1:-$PPID}"

while IFS= read -r line; do
    printf '%s\n' "$line" >&2          # keep forwarding rx_fm logs to journal
    case "$line" in
        *"Device has been removed"*|*"Device not found"*|*"No supported devices found"*)
            echo "[device_loss_guard] SDR device lost — terminating PID ${TARGET_PID} so systemd restarts the stream" >&2
            kill -TERM "$TARGET_PID" 2>/dev/null || true
            exit 1
            ;;
    esac
done
