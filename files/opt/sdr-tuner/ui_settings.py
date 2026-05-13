"""User-configurable UI settings.

Persisted at /etc/sdr-streams/ui.json (writable by the radio user) so they
can be edited from the admin page without restarting Flask. The schema is
intentionally tiny — just things end users may want to change.
"""
import json
from pathlib import Path

SETTINGS_PATH = Path("/etc/sdr-streams/ui.json")

# Sensible defaults if the file doesn't exist yet
DEFAULTS = {
    "stream_url":     "",      # empty = build from request host
    "stream_label":   "Local Icecast",
    "site_title":     "Pi Radio",
}


def load():
    """Return current settings merged with defaults."""
    out = dict(DEFAULTS)
    try:
        data = json.loads(SETTINGS_PATH.read_text())
        if isinstance(data, dict):
            out.update({k: v for k, v in data.items() if k in DEFAULTS})
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return out


def save(updates: dict):
    """Update one or more settings; preserves anything not provided."""
    cur = load()
    for k, v in updates.items():
        if k in DEFAULTS:
            cur[k] = (v or "").strip() if isinstance(v, str) else v
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cur, indent=2))
    tmp.replace(SETTINGS_PATH)
    return cur


def stream_url_for(host_header: str) -> str:
    """Resolve the configured URL, falling back to a host-relative default."""
    s = load()
    if s["stream_url"]:
        return s["stream_url"]
    # Fall back to <host>:8000/fm.mp3 (strip any port from host)
    bare_host = host_header.split(":")[0] if host_header else "localhost"
    return f"http://{bare_host}:8000/fm.mp3"
