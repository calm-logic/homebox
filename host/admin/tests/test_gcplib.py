"""Unit tests for app.targets.gcplib — the hand-rolled GCP auth/API client.

No network: every HTTP exchange goes through httpx.MockTransport, and the
service-account "key file" is a throwaway RSA key generated in-test. The JWT
test verifies the signature with the real public key, so the RS256 path is
exercised end to end, not just string-compared.
"""
from __future__ import annotations

import asyncio
import base64
import json
import sys
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

# Make the `app` package importable (tests/ sits beside app/).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.targets import gcplib  # noqa: E402
from app.targets.gcplib import GcpClient, GcpError, make_assertion  # noqa: E402

NOW = 1_700_000_000


@pytest.fixture(scope="module")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def sa(rsa_key):
    """A fake parsed service-account key file with a real private key."""
    pem = rsa_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    return {
        "type": "service_account",
        "project_id": "proj-1",
        "client_email": "deployer@proj-1.iam.gserviceaccount.com",
        "private_key": pem,
    }


def _b64url_decode(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def _token_response(n: int = 1) -> httpx.Response:
    return httpx.Response(200, json={"access_token": f"tok-{n}", "expires_in": 3600})


# ── make_assertion ────────────────────────────────────────────────────────────


def test_make_assertion_structure_and_signature(sa, rsa_key):
    scopes = ["https://www.googleapis.com/auth/cloud-platform"]
    jwt = make_assertion(sa, scopes=scopes, now=NOW)

    head_b64, claims_b64, sig_b64 = jwt.split(".")
    header = json.loads(_b64url_decode(head_b64))
    claims = json.loads(_b64url_decode(claims_b64))

    assert header == {"alg": "RS256", "typ": "JWT"}
    assert claims == {
        "iss": "deployer@proj-1.iam.gserviceaccount.com",
        "scope": "https://www.googleapis.com/auth/cloud-platform",
        "aud": "https://oauth2.googleapis.com/token",
        "iat": NOW,
        "exp": NOW + 3600,
    }
    # No padding chars anywhere (base64url, RFC 7515).
    assert "=" not in jwt

    # Real RS256: the public key must verify the signature over header.claims.
    rsa_key.public_key().verify(
        _b64url_decode(sig_b64),
        f"{head_b64}.{claims_b64}".encode("ascii"),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )


def test_make_assertion_joins_scopes(sa):
    jwt = make_assertion(sa, scopes=["scope-a", "scope-b"], now=NOW)
    claims = json.loads(_b64url_decode(jwt.split(".")[1]))
    assert claims["scope"] == "scope-a scope-b"


# ── token() exchange + caching ────────────────────────────────────────────────


def test_token_exchange_caching_and_refresh(sa, monkeypatch):
    clock = [float(NOW)]
    monkeypatch.setattr(gcplib, "_now", lambda: clock[0])

    token_calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://oauth2.googleapis.com/token"
        token_calls.append(request)
        return _token_response(len(token_calls))

    client = GcpClient(sa, transport=httpx.MockTransport(handler))

    assert asyncio.run(client.token()) == "tok-1"
    # Correct grant type + a well-formed assertion in the form body.
    form = parse_qs(token_calls[0].content.decode("ascii"))
    assert form["grant_type"] == ["urn:ietf:params:oauth:grant-type:jwt-bearer"]
    assert form["assertion"][0].count(".") == 2

    # Cached: a second call inside the validity window makes no HTTP hit.
    assert asyncio.run(client.token()) == "tok-1"
    assert len(token_calls) == 1

    # Still cached just outside the 60s slack boundary...
    clock[0] = NOW + 3600 - 61
    assert asyncio.run(client.token()) == "tok-1"
    assert len(token_calls) == 1

    # ...refreshed once within 60s of expiry.
    clock[0] = NOW + 3600 - 59
    assert asyncio.run(client.token()) == "tok-2"
    assert len(token_calls) == 2


def test_token_exchange_error_raises_gcperror(sa):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"error": "invalid_grant", "error_description": "Invalid JWT"}
        )

    client = GcpClient(sa, transport=httpx.MockTransport(handler))
    with pytest.raises(GcpError) as ei:
        asyncio.run(client.token())
    assert ei.value.status == 400
    assert "invalid_grant" in str(ei.value)
    assert "Invalid JWT" in str(ei.value)


# ── request() + wrappers ──────────────────────────────────────────────────────


def _api_transport(handler):
    """MockTransport that answers the token endpoint itself and hands every
    other request to `handler`."""

    def route(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return _token_response()
        return handler(request)

    return httpx.MockTransport(route)


def test_request_attaches_bearer_and_get_project_url(sa):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"projectId": "proj-1", "lifecycleState": "ACTIVE"})

    client = GcpClient(sa, transport=_api_transport(handler))
    assert client.project_id == "proj-1"

    project = asyncio.run(client.get_project())
    assert project["projectId"] == "proj-1"
    assert seen["auth"] == "Bearer tok-1"
    assert seen["url"] == "https://cloudresourcemanager.googleapis.com/v1/projects/proj-1"


def test_request_error_envelope_becomes_gcperror(sa):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "error": {
                    "code": 403,
                    "message": "Permission denied on resource project proj-1.",
                    "status": "PERMISSION_DENIED",
                }
            },
        )

    client = GcpClient(sa, transport=_api_transport(handler))
    with pytest.raises(GcpError) as ei:
        asyncio.run(client.get_project())
    assert ei.value.status == 403
    assert ei.value.reason == "PERMISSION_DENIED"
    assert "Permission denied on resource project proj-1." in str(ei.value)


def test_api_prefixes(sa):
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        return httpx.Response(200, json={})

    client = GcpClient(sa, transport=_api_transport(handler))
    asyncio.run(client.run("GET", "projects/proj-1/locations/us-central1/services"))
    asyncio.run(client.storage("GET", "b/my-bucket"))
    asyncio.run(client.compute("GET", "zones/us-central1-a/instances"))
    assert urls == [
        "https://run.googleapis.com/v2/projects/proj-1/locations/us-central1/services",
        "https://storage.googleapis.com/storage/v1/b/my-bucket",
        "https://compute.googleapis.com/compute/v1/projects/proj-1/zones/us-central1-a/instances",
    ]


def test_storage_upload_url_and_content_type(sa):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["scheme_host_path"] = f"{request.url.scheme}://{request.url.host}{request.url.path}"
        seen["params"] = dict(request.url.params)
        seen["content_type"] = request.headers.get("Content-Type")
        seen["body"] = request.content
        return httpx.Response(200, json={"bucket": "my-bucket", "name": "site/index.html"})

    client = GcpClient(sa, transport=_api_transport(handler))
    obj = asyncio.run(
        client.storage_upload("my-bucket", "site/index.html", b"<html>", "text/html")
    )
    assert obj["name"] == "site/index.html"
    assert seen["method"] == "POST"
    assert seen["scheme_host_path"] == "https://storage.googleapis.com/upload/storage/v1/b/my-bucket/o"
    assert seen["params"] == {"uploadType": "media", "name": "site/index.html"}
    assert seen["content_type"] == "text/html"
    assert seen["body"] == b"<html>"
