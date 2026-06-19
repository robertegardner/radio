#!/usr/bin/env python3
"""ATC recording scheduler tick — run every minute by atc-rec.timer (User=radio).

Reconciles the schedule (written by the radio app's /api/atc-rec/* endpoints)
against the wall clock:
  * during a job's window: tune the single-tuner R2 to ATC on the job's freq
    (via the coordinator) and record /scanner-atc.mp3 to one file per job;
  * when no job is active: stop recording and return the R2 to its NOAA default;
  * prune recordings older than the retention window.

One recording at a time (the R2 is single-tuner). The recorder is a separate
systemd unit (atc-record.service) so it outlives this oneshot tick.
"""
import fcntl
import json
import subprocess
import time
import urllib.request
from pathlib import Path

REC_DIR    = Path("/var/lib/sdr-streams/atc-rec")
SCHED      = REC_DIR / "schedule.json"
CFG        = REC_DIR / "config.json"
STATE      = REC_DIR / "state.json"
RECORD_ENV = REC_DIR / "record.env"
LOCK       = REC_DIR / ".tick.lock"
ATC_MOUNT  = "https://icecast.rg2.io/scanner-atc.mp3"
RADIO_API  = "http://127.0.0.1:8080"


def load(p, default):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return default


def save(p, obj):
    REC_DIR.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=2))


def log(msg):
    print(f"atc-rec-tick: {msg}", flush=True)


def is_recording():
    return subprocess.run(["systemctl", "is-active", "--quiet",
                           "atc-record.service"]).returncode == 0


def start_recording(job):
    RECORD_ENV.write_text(f"URL={ATC_MOUNT}\nFILE={REC_DIR / (job['id'] + '.mp3')}\n")
    subprocess.run(["sudo", "systemctl", "restart", "atc-record.service"], check=False)


def stop_recording():
    subprocess.run(["sudo", "systemctl", "stop", "atc-record.service"], check=False)


def r2(mode, freq=None):
    """Drive the R2-mode coordinator through the radio app's scanner gateway."""
    body = {"mode": mode}
    if freq:
        body.update({"freq": f"{freq}M", "audio_mode": "am"})
    try:
        req = urllib.request.Request(
            f"{RADIO_API}/api/scanner/r2/mode", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=25).read()
    except Exception as e:  # noqa: BLE001
        log(f"r2({mode}) failed: {e}")


def finalize(jobs, jid, now):
    for j in jobs:
        if j["id"] == jid and j.get("status") == "recording":
            j["status"] = "done"
            j["actual_end"] = now


def main():
    REC_DIR.mkdir(parents=True, exist_ok=True)
    lock = open(LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return                                   # another tick is mid-run

    now = int(time.time())
    jobs = load(SCHED, {}).get("jobs", [])
    state = load(STATE, {})
    retention = int(load(CFG, {}).get("retention_days", 14))

    active = sorted([j for j in jobs
                     if j.get("status") in ("scheduled", "recording")
                     and j["start"] <= now < j["end"]],
                    key=lambda j: j["start"])
    want = active[0] if active else None
    cur_id = state.get("job_id")
    recording = is_recording()

    # Reconcile orphans: a job still marked 'recording' that isn't the live
    # recording (recorder died, or state was lost) would otherwise wedge the
    # queue + block deletion. Finalize it (done if it captured audio, else error).
    for j in jobs:
        if j is want:
            continue
        if j.get("status") == "recording" and not (recording and cur_id == j["id"]):
            f = REC_DIR / f"{j['id']}.mp3"
            j["status"] = "done" if (f.exists() and f.stat().st_size > 0) else "error"
            j.setdefault("actual_end", now)

    if want:
        if not recording or cur_id != want["id"]:
            if recording:                        # switching jobs
                stop_recording()
                finalize(jobs, cur_id, now)
            log(f"START {want['id']} {want['label']} {want['freq']} MHz")
            r2("atc", want["freq"])              # preempt the R2 onto ATC
            time.sleep(8)                        # let the source bounce + retune settle
            start_recording(want)
            want["status"] = "recording"
            want.setdefault("actual_start", now)
            state = {"job_id": want["id"], "tuned": True}
    else:
        if recording:
            log(f"STOP {cur_id}")
            stop_recording()
            finalize(jobs, cur_id, now)
        if state.get("tuned"):
            r2("noaa")                           # back to the 24/7 default
        state = {}

    for j in jobs:                               # window passed, never recorded
        if j.get("status") == "scheduled" and j["end"] <= now:
            j["status"] = "missed"

    cutoff = now - retention * 86400             # prune past retention
    keep = []
    for j in jobs:
        if j.get("status") in ("done", "cancelled", "missed") and j["end"] < cutoff:
            try:
                (REC_DIR / f"{j['id']}.mp3").unlink(missing_ok=True)
            except OSError:
                pass
            log(f"PRUNE {j['id']}")
            continue
        keep.append(j)

    save(SCHED, {"jobs": keep})
    save(STATE, state)


if __name__ == "__main__":
    main()
