"""Tests for the Cloudflare Pages target (app/targets/cloudflare_pages.py).

The wrangler-compatible content hash is pinned against fixed vectors computed
with hashlib.blake2b (digest_size=16 → 32 hex chars). All API behavior —
create-or-adopt project, direct-upload flow (upload-token / check-missing /
upload / upsert-hashes / multipart deployment), custom domains, destroy,
probe — runs against httpx.MockTransport. No network, no real credentials.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.targets.base import TargetDeployCtx, TargetError  # noqa: E402
from app.targets.cloudflare_pages import (  # noqa: E402
    PagesTarget,
    _pages_hash,
    _project_name,
)

ACCOUNT = "acct-1"
NAME = "homebox-listless-dev-web"
BASE = f"/client/v4/accounts/{ACCOUNT}/pages/projects"

HTML = b"<!doctype html><h1>hi</h1>"
JS = b"console.log(1)"
HTML_HASH = "39d1d69634874b87b2e5603ae91e2dad"
JS_HASH = "359ba85b0eb1a3633084533b3a3d48f3"


def run(coro):
    return asyncio.run(coro)


def ok(result) -> httpx.Response:
    return httpx.Response(200, json={"success": True, "errors": [], "result": result})


class FakePages:
    """Routes MockTransport requests like the Cloudflare Pages API."""

    def __init__(self, *, exists: bool = False, missing: list[str] | None = None,
                 domains: list[dict] | None = None):
        self.exists = exists
        self.missing = missing if missing is not None else []
        self.domains = domains if domains is not None else []
        self.requests: list[tuple[str, str]] = []
        self.upload_batches: list[list[dict]] = []
        self.deployment_content_type = ""
        self.deployment_body = b""
        self.domain_posts: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        self.requests.append((method, path))
        if method == "GET" and path == f"{BASE}/{NAME}":
            if self.exists:
                return ok({"name": NAME, "latest_deployment": {"id": "dep-0"}})
            return httpx.Response(404, json={
                "success": False, "result": None,
                "errors": [{"code": 8000007, "message": "Project not found"}],
            })
        if method == "POST" and path == BASE:
            self.exists = True
            return ok({"name": NAME})
        if method == "POST" and path == f"{BASE}/{NAME}/upload-token":
            return ok({"jwt": "jwt-token"})
        if method == "POST" and path == "/client/v4/pages/assets/check-missing":
            assert request.headers["authorization"] == "Bearer jwt-token"
            return ok(self.missing)
        if method == "POST" and path == "/client/v4/pages/assets/upload":
            assert request.headers["authorization"] == "Bearer jwt-token"
            self.upload_batches.append(json.loads(request.content))
            return ok(None)
        if method == "POST" and path == "/client/v4/pages/assets/upsert-hashes":
            assert request.headers["authorization"] == "Bearer jwt-token"
            return ok(True)
        if method == "POST" and path == f"{BASE}/{NAME}/deployments":
            assert request.headers["authorization"] == "Bearer test-token"
            self.deployment_content_type = request.headers.get("content-type", "")
            self.deployment_body = request.content
            return ok({"id": "dep-1", "url": f"https://abc.{NAME}.pages.dev"})
        if method == "GET" and path == f"{BASE}/{NAME}/domains":
            return ok(self.domains)
        if method == "POST" and path == f"{BASE}/{NAME}/domains":
            self.domain_posts.append(json.loads(request.content))
            return ok({"name": "attached"})
        raise AssertionError(f"unexpected request: {method} {path}")


def target(fake: FakePages, account: str = ACCOUNT) -> PagesTarget:
    return PagesTarget(
        creds={"token": "test-token", "account_id": account, "config": {}},
        config={}, state={},
        transport=httpx.MockTransport(fake.handler),
    )


def make_ctx(tmp_path: Path, hostname: str | None = None):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True, exist_ok=True)
    (dist / "index.html").write_bytes(HTML)
    (dist / "assets" / "app.js").write_bytes(JS)
    lines: list[str] = []

    async def log(line: str) -> None:
        lines.append(line)

    ctx = TargetDeployCtx(
        project_name="listless", env_name="dev", service_name="web",
        kind="static", rd=tmp_path, hostname=hostname, static_dir=dist, log=log,
    )
    return ctx, lines


# ───── content hash ───────────────────────────────────────────────────────────


def test_pages_hash_matches_blake2b_vectors():
    assert _pages_hash(HTML, "html") == HTML_HASH
    assert _pages_hash(JS, "js") == JS_HASH
    # Definition: blake2b(digest_size=16) over base64(content) + extension.
    expected = hashlib.blake2b(
        (base64.b64encode(HTML).decode("ascii") + "html").encode("ascii"),
        digest_size=16,
    ).hexdigest()
    assert _pages_hash(HTML, "html") == expected
    assert len(expected) == 32
    # Stable, and the extension participates in the digest.
    assert _pages_hash(HTML, "html") == _pages_hash(HTML, "html")
    assert _pages_hash(HTML, "js") != _pages_hash(HTML, "html")


# ───── deploy: fresh project, direct-upload flow ──────────────────────────────


def test_deploy_creates_project_and_uploads_missing(tmp_path):
    fake = FakePages(exists=False, missing=[JS_HASH])
    ctx, lines = make_ctx(tmp_path, hostname="listless.example.com")
    result = run(target(fake).deploy(ctx))

    # Project GET 404 → create POST, then the direct-upload sequence in order.
    seq = fake.requests
    assert seq[0] == ("GET", f"{BASE}/{NAME}")
    assert seq[1] == ("POST", BASE)
    assert seq[2] == ("POST", f"{BASE}/{NAME}/upload-token")
    assert seq[3] == ("POST", "/client/v4/pages/assets/check-missing")
    assert seq[4] == ("POST", "/client/v4/pages/assets/upload")
    assert seq[5] == ("POST", "/client/v4/pages/assets/upsert-hashes")
    assert seq[6] == ("POST", f"{BASE}/{NAME}/deployments")
    assert seq[7] == ("GET", f"{BASE}/{NAME}/domains")
    assert seq[8] == ("POST", f"{BASE}/{NAME}/domains")

    # Only the missing blob is uploaded, base64-encoded, with a content type.
    assert len(fake.upload_batches) == 1
    (entry,) = fake.upload_batches[0]
    assert entry["key"] == JS_HASH
    assert entry["base64"] is True
    assert base64.b64decode(entry["value"]) == JS
    assert "javascript" in entry["metadata"]["contentType"]

    # Deployment is a multipart form whose manifest maps both paths.
    assert fake.deployment_content_type.startswith("multipart/form-data")
    body = fake.deployment_body
    assert b'name="manifest"' in body
    assert b"/index.html" in body and b"/assets/app.js" in body
    assert HTML_HASH.encode() in body and JS_HASH.encode() in body

    # Custom domain attached.
    assert fake.domain_posts == [{"name": "listless.example.com"}]

    # Result contract.
    assert result.endpoint == f"{NAME}.pages.dev"
    assert result.cname_target == f"{NAME}.pages.dev"
    assert result.proxied is True
    assert result.state == {
        "pages_project": NAME, "deployment_id": "dep-1", "account_id": ACCOUNT,
    }
    assert any("uploading 2 files (1 new)" in line for line in lines)


def test_deploy_second_run_is_idempotent(tmp_path):
    # Project already exists, store already has every blob, domain attached.
    fake = FakePages(exists=True, missing=[],
                     domains=[{"name": "listless.example.com"}])
    ctx, _ = make_ctx(tmp_path, hostname="listless.example.com")
    result = run(target(fake).deploy(ctx))

    assert ("POST", BASE) not in fake.requests               # no re-create
    assert ("POST", "/client/v4/pages/assets/upload") not in fake.requests
    assert ("POST", f"{BASE}/{NAME}/domains") not in fake.requests
    assert ("POST", f"{BASE}/{NAME}/deployments") in fake.requests
    assert result.state["pages_project"] == NAME


def test_deploy_without_hostname_skips_domains(tmp_path):
    fake = FakePages(exists=True, missing=[])
    ctx, _ = make_ctx(tmp_path, hostname=None)
    run(target(fake).deploy(ctx))
    assert not any("/domains" in path for _, path in fake.requests)


# ───── validate ───────────────────────────────────────────────────────────────


def test_validate_auth_failure_mentions_token_scope():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={
            "success": False, "result": None,
            "errors": [{"code": 9109, "message":
                        "Unauthorized to access requested resource"}],
        })

    t = PagesTarget(creds={"token": "t", "account_id": ACCOUNT, "config": {}},
                    transport=httpx.MockTransport(handler))
    with pytest.raises(TargetError) as exc:
        run(t.validate())
    assert "Cloudflare Pages: Edit" in str(exc.value)


def test_validate_ok():
    fake = FakePages(exists=True)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == BASE and request.method == "GET"
        return ok([])

    t = PagesTarget(creds={"token": "t", "account_id": ACCOUNT, "config": {}},
                    transport=httpx.MockTransport(handler))
    run(t.validate())  # no raise


# ───── destroy / probe ────────────────────────────────────────────────────────


def test_destroy_tolerates_404_and_uses_state_account():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"], seen["path"] = request.method, request.url.path
        return httpx.Response(404, json={
            "success": False, "result": None,
            "errors": [{"code": 8000007, "message": "Project not found"}],
        })

    # creds carry a different account: state's account_id must win.
    t = PagesTarget(creds={"token": "t", "account_id": "other", "config": {}},
                    transport=httpx.MockTransport(handler))
    run(t.destroy({"pages_project": NAME, "account_id": ACCOUNT}))  # no raise
    assert seen["method"] == "DELETE"
    assert seen["path"] == f"{BASE}/{NAME}"


def test_destroy_without_state_is_a_noop():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    t = target(FakePages())
    run(PagesTarget(creds={"token": "t", "account_id": ACCOUNT},
                    transport=httpx.MockTransport(handler)).destroy({}))
    del t


def test_probe_true_when_project_has_live_deployment():
    fake = FakePages(exists=True)
    assert run(target(fake).probe({"pages_project": NAME})) is True


def test_probe_false_when_project_gone():
    fake = FakePages(exists=False)
    assert run(target(fake).probe({"pages_project": NAME})) is False


# ───── project-name sanitizer ─────────────────────────────────────────────────


def name_ctx(project: str, env: str = "dev", service: str = "web") -> TargetDeployCtx:
    return TargetDeployCtx(project_name=project, env_name=env,
                           service_name=service, kind="static",
                           rd=Path("/nonexistent"), hostname=None)


def test_project_name_passthrough():
    assert _project_name(name_ctx("listless")) == NAME


def test_project_name_strips_invalid_chars_and_case():
    assert _project_name(name_ctx("My_App!! (v2)")) == "homebox-my-app-v2-dev-web"


def test_project_name_truncates_to_58():
    n = _project_name(name_ctx("x" * 80))
    assert len(n) == 58
    assert n == "homebox-" + "x" * 50


def test_project_name_no_trailing_dash_after_truncation():
    # Char 58 of the sanitized name lands exactly on a dash.
    n = _project_name(name_ctx("x" * 49 + "-tail"))
    assert len(n) <= 58
    assert not n.endswith("-") and not n.startswith("-")


def test_project_name_always_valid():
    for raw in ("-weird-", "UPPER case", "dots.and.slashes/here", "a" * 200):
        n = _project_name(name_ctx(raw))
        assert 0 < len(n) <= 58
        assert re.fullmatch(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?", n), n
        assert "--" not in n
