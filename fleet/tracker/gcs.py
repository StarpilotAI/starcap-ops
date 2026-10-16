"""
gcs.py: read links to contact sheets, signed on this laptop.

A take's contact sheet is a JPEG in the footage bucket (recordings.data.analysis.sheet_path,
"analysis/<card>/<run>/contactsheet.jpg", the after-cut sheet). The bucket is private, so
the drawer gets a V4 signed URL: GET only, one object, SHEET_LINK_S long. Signing is local
RSA with the dashboard's own service-account key (GOOGLE_SERVICE_ACCOUNT_JSON_INLINE, what
its /api/video-url signs with): no network call, nothing written. The key stays in this
process and is registered with sources.scrub so no error message can carry it.

A link is reused until a quarter of its life is left, so reopening a drawer hands the
browser the same URL and the image comes from its cache instead of the bucket again.
"""
import hashlib
import json
import threading
import time
import urllib.parse
from datetime import datetime, timezone

import config
import sources

HOST = "storage.googleapis.com"
KEY_VAR = "GOOGLE_SERVICE_ACCOUNT_JSON_INLINE"
CACHE_MAX = 5000


class NoKey(Exception):
    """No usable service-account key: the drawer shows no sheets, the rest works."""


class Signer:
    def __init__(self, client_email, private_key_pem, bucket=None, link_s=None, clock=time.time):
        from cryptography.hazmat.primitives import serialization
        self.email = client_email
        self._key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
        self.bucket = bucket or config.SHEET_BUCKET
        self.link_s = int(link_s or config.SHEET_LINK_S)
        self._clock = clock
        self._links = {}                  # path -> (url, expires_at)
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls, env=None, **kw):
        env = sources.load_env() if env is None else env
        raw = env.get(KEY_VAR)
        if not raw:
            raise NoKey(f"{KEY_VAR} is not in the env file")
        try:
            sa = json.loads(raw)
            sources._remember_secrets([sa.get("private_key"), sa.get("private_key_id")])
            return cls(sa["client_email"], sa["private_key"], **kw)
        except ImportError:
            raise NoKey("python3-cryptography is not installed") from None
        except (ValueError, KeyError, TypeError, AttributeError):
            raise NoKey(f"{KEY_VAR} is not a usable service-account key") from None

    def url(self, path):
        """A signed GET link to `path` in the sheet bucket, or None for no path."""
        if not path:
            return None
        now = self._clock()
        with self._lock:
            hit = self._links.get(path)
            if hit and hit[1] - now > self.link_s / 4:
                return hit[0]
            if len(self._links) >= CACHE_MAX:
                self._links = {p: v for p, v in self._links.items() if v[1] > now}
            url = self._sign(path, now)
            self._links[path] = (url, now + self.link_s)
            return url

    def _sign(self, path, now):
        """Google's V4 query-string signing (GOOG4-RSA-SHA256), host the only signed header."""
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        t = datetime.fromtimestamp(now, timezone.utc)
        stamp = t.strftime("%Y%m%dT%H%M%SZ")
        scope = t.strftime("%Y%m%d") + "/auto/storage/goog4_request"
        resource = f"/{self.bucket}/{urllib.parse.quote(path, safe='/~')}"
        query = "&".join(f"{_q(k)}={_q(v)}" for k, v in sorted({
            "X-Goog-Algorithm": "GOOG4-RSA-SHA256", "X-Goog-Credential": f"{self.email}/{scope}",
            "X-Goog-Date": stamp, "X-Goog-Expires": str(self.link_s), "X-Goog-SignedHeaders": "host",
        }.items()))
        request = "\n".join(["GET", resource, query, f"host:{HOST}\n", "host", "UNSIGNED-PAYLOAD"])
        to_sign = "\n".join(["GOOG4-RSA-SHA256", stamp, scope, hashlib.sha256(request.encode()).hexdigest()])
        signature = self._key.sign(to_sign.encode(), padding.PKCS1v15(), hashes.SHA256()).hex()
        return f"https://{HOST}{resource}?{query}&X-Goog-Signature={signature}"


def _q(s):
    return urllib.parse.quote(s, safe="~")
