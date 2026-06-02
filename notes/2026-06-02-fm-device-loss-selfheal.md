# 2026-06-02 — FM stream self-heal on SDRplay device loss

PR #1 (`fix-fm-device-loss-selfheal`). Companion to scanner work that made the
scanner's MOSWIN P25 coexist with the radio.

## What broke, and why
The FM stream (`sdr-fm@active.service`) was found dead with `rx_fm` looping
`[ERROR] Device has been removed. Stopping.` since the prior day. Root cause: the
sibling **scanner** project's SDRTrunk loads `libsdrplay_api.so` and enumerates
our RSPdx-R2 on startup (even though it only uses its own Nooelec), which yanks
the SDRplay out from under our `rx_fm`.

Two compounding defects:
1. **Contention:** SDRTrunk shouldn't touch our dongle at all, but its sdrplay
   enumeration does. (Fixed on the scanner side — see below.)
2. **No recovery:** this rx_fm/SoapySDR build *loops* the device-removal error
   forever without exiting, so `Restart=always` never fired and the stream stayed
   dead until a manual restart.

## Fix (this repo)
`files/opt/sdr-tuner/device_loss_guard.sh` — reads rx_fm's stderr, forwards it to
the journal, and on the device-removal marker SIGTERMs the service's main PID so
systemd (`Restart=always`, `KillMode=control-group`) restarts the pipeline and
re-acquires the device. Wired into the fm/wbfm branch of `stream.sh`:

```
rx_fm ... - 2> >(/opt/sdr-tuner/device_loss_guard.sh $$) | tee … | ffmpeg …
```

Only the rx_fm (FM/WBFM) path needed this — `am_stream.py` and `hd_stream.py` are
Python and exit on error already. This is the safety net for **transient** losses
(e.g. a USB glitch); the persistent SDRTrunk contention is prevented on the
scanner side.

## Verified
- Guard kills its target on the exact marker (standalone test); forwards other
  lines unchanged.
- Live pipeline runs the guard with the service MainPID as its target.
- FM stream unaffected in normal operation (no regression).
- Not exercised: a real device-removal event (can't safely unplug hardware).

## Coexistence fix (scanner side, but you must know about it)
The scanner's `bootstrap.sh` restricts `/usr/local/lib/libsdrplay_api.so*` to
`root:radio 750` so SDRTrunk (user `scanner`) can't load it and skips our RSP;
we (user `radio`, group `radio`) keep access. **Re-apply after any SDRplay API
reinstall — it resets the perms to 644 and the conflict returns.** With it in
place, the radio FM stream and the scanner's always-on MOSWIN run at the same
time on their separate dongles (verified).
