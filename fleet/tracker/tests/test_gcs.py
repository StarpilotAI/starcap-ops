"""gcs.py: V4 signed read links, checked against the signing spec with a throwaway key."""
import hashlib
import json
import os
import sys
import unittest
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import padding, rsa  # noqa: E402

import gcs  # noqa: E402
import sources  # noqa: E402

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                        serialization.NoEncryption()).decode()
EMAIL = "sheets@proj.iam.gserviceaccount.com"
NOW = 1792130000.0           # 2026-10-16 05:53:20 UTC


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def signer(clock=None, **kw):
    return gcs.Signer(EMAIL, PEM, bucket="starcap-videos-eu", link_s=3600, clock=clock or Clock(NOW), **kw)


class Sign(unittest.TestCase):
    def test_the_link_is_a_valid_v4_signature(self):
        url = signer().url("analysis/pi-f5/rec_starcap184_a7_5240/contactsheet.jpg")
        u = urllib.parse.urlsplit(url)
        self.assertEqual((u.scheme, u.netloc, u.path),
                         ("https", "storage.googleapis.com", "/starcap-videos-eu/analysis/pi-f5/rec_starcap184_a7_5240/contactsheet.jpg"))
        q = urllib.parse.parse_qs(u.query)
        self.assertEqual(q["X-Goog-Algorithm"], ["GOOG4-RSA-SHA256"])
        self.assertEqual(q["X-Goog-Credential"], [f"{EMAIL}/20261016/auto/storage/goog4_request"])
        self.assertEqual((q["X-Goog-Date"], q["X-Goog-Expires"], q["X-Goog-SignedHeaders"]),
                         (["20261016T055320Z"], ["3600"], ["host"]))
        # rebuild what Google rebuilds, and check the signature against the public key
        query, _, sig = u.query.rpartition("&X-Goog-Signature=")
        request = "\n".join(["GET", u.path, query, "host:storage.googleapis.com\n", "host", "UNSIGNED-PAYLOAD"])
        to_sign = "\n".join(["GOOG4-RSA-SHA256", "20261016T055320Z", "20261016/auto/storage/goog4_request",
                             hashlib.sha256(request.encode()).hexdigest()])
        KEY.public_key().verify(bytes.fromhex(sig), to_sign.encode(), padding.PKCS1v15(), hashes.SHA256())
        self.assertIn("X-Goog-Credential=sheets%40proj.iam.gserviceaccount.com%2F20261016%2Fauto", query)

    def test_odd_characters_in_the_path_are_encoded(self):
        u = urllib.parse.urlsplit(signer().url("analysis/card 1/run+2/contactsheet.jpg"))
        self.assertEqual(u.path, "/starcap-videos-eu/analysis/card%201/run%2B2/contactsheet.jpg")

    def test_no_path_no_link(self):
        self.assertIsNone(signer().url(None))
        self.assertIsNone(signer().url(""))

    def test_a_link_is_reused_until_a_quarter_of_its_life_is_left(self):
        clock = Clock(NOW)
        s = signer(clock)
        first = s.url("a.jpg")
        clock.t = NOW + 2600                         # 1000 s left of 3600: still handed out
        self.assertEqual(s.url("a.jpg"), first)
        clock.t = NOW + 2800                         # 800 s left: a fresh one
        self.assertNotEqual(s.url("a.jpg"), first)


class FromEnv(unittest.TestCase):
    def test_reads_the_dashboard_key_and_hides_it(self):
        sa = {"type": "service_account", "client_email": EMAIL, "private_key": PEM, "private_key_id": "abc123def456"}
        s = gcs.Signer.from_env({gcs.KEY_VAR: json.dumps(sa)}, link_s=60)
        self.assertEqual((s.email, s.link_s), (EMAIL, 60))
        self.assertNotIn("abc123def456", sources.scrub("key abc123def456 leaked"))
        self.assertNotIn(PEM.splitlines()[1], sources.scrub(PEM))

    def test_no_or_bad_key_says_why(self):
        with self.assertRaisesRegex(gcs.NoKey, "not in the env file"):
            gcs.Signer.from_env({})
        for bad in ("not json", json.dumps({"client_email": EMAIL}), json.dumps({"client_email": EMAIL, "private_key": "nope"})):
            with self.assertRaisesRegex(gcs.NoKey, "not a usable"):
                gcs.Signer.from_env({gcs.KEY_VAR: bad})


if __name__ == "__main__":
    unittest.main()
