"""Minimal GCP REST client: service-account OAuth2 + thin API wrappers.

No google-auth / google-cloud SDK — same hand-rolled httpx style as
app/cloudflare.py. Auth follows Google's documented service-account flow
(https://developers.google.com/identity/protocols/oauth2/service-account):
build an RS256-signed JWT from the service-account key file, exchange it at
the token endpoint for a short-lived Bearer access token, cache until just
before expiry. The RS256 signature is done directly with `cryptography`
(PKCS#1 v1.5 + SHA-256), which the app already depends on for crypto.py.

Used by the gcp_* deploy targets (Cloud Run / GCS / GCE) — this module is
transport + auth only; resource logic lives in the target modules.
"""

from __future__ import annotations

import base64
import json as jsonlib
import time
from typing import Any

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

TOKEN_URL = "https://oauth2.googleapis.com/token"
DEFAULT_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
JWT_GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"

CRM_API = "https://cloudresourcemanager.googleapis.com/v1/"
RUN_API = "https://run.googleapis.com/v2/"
STORAGE_API = "https://storage.googleapis.com/storage/v1/"
STORAGE_UPLOAD_API = "https://storage.googleapis.com/upload/storage/v1/"
COMPUTE_API = "https://compute.googleapis.com/compute/v1/"

# Refresh the cached access token when it has less than this long to live.
TOKEN_SLACK = 60


def _now() -> float:
    """Wall clock, split out so tests can freeze time."""
    return time.time()


class GcpError(Exception):
    """A Google API call failed. Carries the HTTP/apierror status code and the
    human message parsed from Google's JSON error envelope
    ({"error": {"code", "message", "status"}})."""

    def __init__(self, status: int, message: str, reason: str | None = None):
        super().__init__(message)
        self.status = status
        self.reason = reason  # e.g. "PERMISSION_DENIED", "NOT_FOUND"


def _error_from(r: httpx.Response) -> GcpError:
    """Build a GcpError from a non-2xx response. Handles both the standard
    Google API envelope and the OAuth token endpoint's flat
    {"error": "...", "error_description": "..."} shape."""
    try:
        body = r.json()
    except ValueError:
        body = None
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        msg = err.get("message") or f"GCP API error {r.status_code}"
        reason = err.get("status")
        if reason:
            msg = f"{msg} ({reason})"
        return GcpError(int(err.get("code") or r.status_code), msg, reason)
    if isinstance(err, str):  # OAuth token endpoint style
        desc = body.get("error_description") if isinstance(body, dict) else None
        return GcpError(r.status_code, f"{err}: {desc}" if desc else err, err)
    return GcpError(r.status_code, f"GCP API error {r.status_code}")


# ───── Service-account JWT ────────────────────────────────────────────────────


def _b64url(data: bytes) -> str:
    """base64url without padding, per RFC 7515."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def make_assertion(sa: dict, *, scopes: list[str], now: int | None = None) -> str:
    """Build the RS256-signed JWT assertion for Google's OAuth2
    service-account flow. `sa` is the parsed service-account key file
    (needs `client_email` + `private_key` PEM). `now` is injectable for
    deterministic tests; defaults to the current time."""
    if now is None:
        now = int(_now())
    header = {"alg": "RS256", "typ": "JWT"}
    claims = {
        "iss": sa["client_email"],
        "scope": " ".join(scopes),
        "aud": TOKEN_URL,
        "iat": now,
        "exp": now + 3600,
    }
    signing_input = (
        _b64url(jsonlib.dumps(header, separators=(",", ":")).encode("utf-8"))
        + "."
        + _b64url(jsonlib.dumps(claims, separators=(",", ":")).encode("utf-8"))
    )
    key = serialization.load_pem_private_key(
        sa["private_key"].encode("utf-8"), password=None
    )
    signature = key.sign(
        signing_input.encode("ascii"), padding.PKCS1v15(), hashes.SHA256()
    )
    return f"{signing_input}.{_b64url(signature)}"


# ───── Client ─────────────────────────────────────────────────────────────────


class GcpClient:
    """Authenticated async client for Google REST APIs, built from a parsed
    service-account key file (`project_id`, `client_email`, `private_key`).

    `transport` is injectable (httpx.MockTransport) for tests."""

    def __init__(
        self,
        sa_json: dict,
        *,
        scopes: list[str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 30,
    ):
        self._sa = sa_json
        self._scopes = list(scopes) if scopes else [DEFAULT_SCOPE]
        self._transport = transport
        self._timeout = timeout
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    @property
    def project_id(self) -> str:
        return self._sa["project_id"]

    def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self._timeout, transport=self._transport)

    async def token(self) -> str:
        """Access token for the configured scopes; exchanges a fresh JWT
        assertion at the token endpoint and caches the result until
        TOKEN_SLACK seconds before expiry."""
        now = _now()
        if self._token and now < self._token_expires_at - TOKEN_SLACK:
            return self._token
        assertion = make_assertion(self._sa, scopes=self._scopes, now=int(now))
        async with self._http() as c:
            r = await c.post(
                TOKEN_URL,
                data={"grant_type": JWT_GRANT, "assertion": assertion},
            )
        if not (200 <= r.status_code < 300):
            raise _error_from(r)
        body = r.json()
        self._token = body["access_token"]
        self._token_expires_at = now + float(body.get("expires_in", 3600))
        return self._token

    async def request(
        self,
        method: str,
        url: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
        content: bytes | None = None,
        headers: dict | None = None,
    ) -> httpx.Response:
        """Authenticated request against any Google endpoint. Raises GcpError
        (with the parsed error-envelope message) on non-2xx."""
        hdrs = {
            "Authorization": f"Bearer {await self.token()}",
            "User-Agent": "homebox-admin",
        }
        if headers:
            hdrs.update(headers)
        async with self._http() as c:
            r = await c.request(
                method, url, json=json, params=params, content=content, headers=hdrs
            )
        if not (200 <= r.status_code < 300):
            raise _error_from(r)
        return r

    # ── Convenience wrappers (thin; resource logic lives in gcp_* targets) ──

    async def get_project(self) -> dict:
        """Fetch the project via Cloud Resource Manager — the cheap
        credential/permission validation probe used by validate()."""
        r = await self.request("GET", f"{CRM_API}projects/{self.project_id}")
        return r.json()

    async def run(self, method: str, path: str, **kw: Any) -> httpx.Response:
        """Cloud Run Admin API v2 (path is relative, e.g.
        'projects/{p}/locations/{l}/services')."""
        return await self.request(method, RUN_API + path.lstrip("/"), **kw)

    async def storage(self, method: str, path: str, **kw: Any) -> httpx.Response:
        """Cloud Storage JSON API v1 (path is relative, e.g. 'b/{bucket}')."""
        return await self.request(method, STORAGE_API + path.lstrip("/"), **kw)

    async def storage_upload(
        self, bucket: str, name: str, data: bytes, content_type: str
    ) -> dict:
        """Simple (media) upload of one object; returns the object resource."""
        r = await self.request(
            "POST",
            f"{STORAGE_UPLOAD_API}b/{bucket}/o",
            params={"uploadType": "media", "name": name},
            content=data,
            headers={"Content-Type": content_type},
        )
        return r.json()

    async def compute(self, method: str, path: str, **kw: Any) -> httpx.Response:
        """Compute Engine API v1, scoped to the project (path is relative to
        projects/{project_id}/, e.g. 'zones/{zone}/instances')."""
        return await self.request(
            method, f"{COMPUTE_API}projects/{self.project_id}/" + path.lstrip("/"), **kw
        )
