"""sdr-tuner access-control guard (platform spec 2026-10-01-radio-authentik-access)."""
import sys
import unittest
from email.message import Message
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "files" / "opt" / "sdr-tuner"))

import authz  # noqa: E402


def _hdrs(**kw):
    m = Message()
    for k, v in kw.items():
        m[k.replace("_", "-")] = v
    return m


class AuthzTest(unittest.TestCase):
    def test_direct_lan_call_without_real_ip_is_trusted(self):
        self.assertFalse(authz.is_forbidden_write("POST", _hdrs()))

    def test_offlan_family_post_forbidden(self):
        h = _hdrs(X_Real_IP="203.0.113.9", X_authentik_username="kid",
                  X_authentik_groups="family|users")
        self.assertTrue(authz.is_forbidden_write("POST", h))

    def test_offlan_family_put_and_delete_forbidden(self):
        h = _hdrs(X_Real_IP="203.0.113.9", X_authentik_groups="family")
        self.assertTrue(authz.is_forbidden_write("PUT", h))
        self.assertTrue(authz.is_forbidden_write("delete", h))

    def test_offlan_admin_post_allowed(self):
        h = _hdrs(X_Real_IP="203.0.113.9", X_authentik_groups="family|homelab-admin")
        self.assertFalse(authz.is_forbidden_write("POST", h))

    def test_lan_ip_with_family_session_is_admin(self):
        h = _hdrs(X_Real_IP="192.168.6.50", X_authentik_groups="family")
        self.assertFalse(authz.is_forbidden_write("POST", h))

    def test_garbage_real_ip_is_untrusted(self):
        self.assertTrue(authz.is_forbidden_write("POST", _hdrs(X_Real_IP="unix:")))

    def test_ipv6_real_ip_parses(self):
        self.assertFalse(authz.auth_context(_hdrs(X_Real_IP="2001:db8::1"))["trusted"])

    def test_get_never_forbidden(self):
        self.assertFalse(authz.is_forbidden_write("GET", _hdrs(X_Real_IP="203.0.113.9")))


if __name__ == "__main__":
    unittest.main()
