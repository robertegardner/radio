"""Off-LAN write guard for the sdr-tuner API.

Platform spec 2026-10-01-radio-authentik-access. NPMplus always sets X-Real-IP
to the true client ($remote_addr) and, for off-LAN requests, forwards
Authentik's X-authentik-* identity. LAN/tailnet (and direct calls that never
touched NPM — no X-Real-IP) are fully trusted; off-LAN writes need ADMIN_GROUP.
"""
import ipaddress
import os

WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
ADMIN_GROUP = os.environ.get("ADMIN_GROUP", "homelab-admin")
TRUSTED_NETS = tuple(
    ipaddress.ip_network(c.strip())
    for c in os.environ.get("TRUSTED_CIDRS",
                            "192.168.0.0/16,100.64.0.0/10,127.0.0.0/8").split(",")
    if c.strip())


def _ip_trusted(ip):
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in TRUSTED_NETS)


def auth_context(headers):
    real_ip = (headers.get("X-Real-IP") or "").strip()
    user = headers.get("X-authentik-username") or ""
    groups = [g for g in (headers.get("X-authentik-groups") or "").split("|") if g]
    trusted = (not real_ip) or _ip_trusted(real_ip)
    return {"real_ip": real_ip, "user": user, "groups": groups,
            "trusted": trusted, "admin": trusted or ADMIN_GROUP in groups}


def is_forbidden_write(method, headers):
    return method.upper() in WRITE_METHODS and not auth_context(headers)["admin"]
