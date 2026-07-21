"""Cloudflare token-connect scope probing.

Covers POST /api/tunnel/token (_validate_and_store_token): the required scopes
(Tunnel/Zone) gate the token, while the OPTIONAL Cloudflare-deploy-target scopes
(Cloudflare Pages, Workers Scripts) are probed *non-blocking* and recorded as
pages_ok / workers_ok so the UI can show a re-scope hint. A token whose Workers
probe 403s must still connect, with workers_ok=False.

The Cloudflare REST calls run through the real app.cloudflare client functions,
with httpx.MockTransport standing in for api.cloudflare.com — no network, no
credentials. We also assert the frontend's pre-filled "create token" URL decodes
to the expected six permission groups and is byte-identical across all three
copies (there is no JS test harness, so this lives here).
"""
from __future__ import annotations

import asyncio
import functools
import json
import re
import sys
import urllib.parse
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import cloudflare as cf  # noqa: E402
from app.auth import require_session_api  # noqa: E402
from app.db import Base, get_session  # noqa: E402
from app.routes import tunnel as tunnel_routes  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

_ENGINES: list = []
FRONTEND = Path(__file__).resolve().parents[1] / "frontend" / "src" / "pages"


def run(coro):
    async def main():
        try:
            return await coro
        finally:
            while _ENGINES:
                await _ENGINES.pop().dispose()
    return asyncio.run(main())


def ok(result) -> httpx.Response:
    return httpx.Response(200, json={"success": True, "errors": [], "result": result})


def auth_err() -> httpx.Response:
    return httpx.Response(403, json={
        "success": False, "result": None,
        "errors": [{"code": 10000, "message": "Authentication error"}],
    })


class FakeCF:
    """Routes MockTransport requests like the Cloudflare API. The optional
    deploy-target endpoints (pages/projects, workers/scripts) can be flipped to
    403 to simulate a token that lacks those scopes."""

    ACCOUNT = "acct-1"

    def __init__(self, *, pages_403: bool = False, workers_403: bool = False):
        self.pages_403 = pages_403
        self.workers_403 = workers_403
        self.paths: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.paths.append(path)
        assert request.headers["authorization"] == "Bearer test-token"
        if path == "/client/v4/user/tokens/verify":
            return ok({"id": "tok", "status": "active"})
        if path == "/client/v4/accounts":
            return ok([{"id": self.ACCOUNT, "name": "Acme"}])
        if path == f"/client/v4/accounts/{self.ACCOUNT}/cfd_tunnel":
            return ok([])
        if path == "/client/v4/zones":
            return ok([])
        if path == f"/client/v4/accounts/{self.ACCOUNT}/pages/projects":
            return auth_err() if self.pages_403 else ok([])
        if path == f"/client/v4/accounts/{self.ACCOUNT}/workers/scripts":
            return auth_err() if self.workers_403 else ok([])
        raise AssertionError(f"unexpected request: {request.method} {path}")


async def make_app(fake: FakeCF, monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite://")
    _ENGINES.append(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    app = FastAPI()
    app.include_router(tunnel_routes.router)

    async def override_session():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[require_session_api] = lambda: "tester"

    # Route the real cf.* client functions at the fake CF API.
    patched = functools.partial(httpx.AsyncClient, transport=httpx.MockTransport(fake.handler))
    monkeypatch.setattr(cf.httpx, "AsyncClient", patched)

    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    return client, maker


async def connect(client, token: str = "test-token"):
    return await client.post("/api/tunnel/token", json={"token": token})


# ── probes stored & exposed ───────────────────────────────────────────────────


def test_connect_records_pages_and_workers_ok(monkeypatch):
    async def body():
        fake = FakeCF()  # both optional probes succeed
        client, maker = await make_app(fake, monkeypatch)
        r = await connect(client)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["account_id"] == FakeCF.ACCOUNT
        # Response exposes the optional-target capabilities for the UI checklist.
        assert data["pages_ok"] is True
        assert data["workers_ok"] is True
        # Both optional endpoints were actually probed.
        assert f"/client/v4/accounts/{FakeCF.ACCOUNT}/pages/projects" in fake.paths
        assert f"/client/v4/accounts/{FakeCF.ACCOUNT}/workers/scripts" in fake.paths
        # …and persisted in the stored state.
        async with maker() as s:
            state = await cf.load_state(s)
            assert state["pages_ok"] is True
            assert state["workers_ok"] is True
    run(body())


def test_workers_probe_403_is_non_blocking(monkeypatch):
    async def body():
        fake = FakeCF(workers_403=True)  # Workers Scripts scope absent
        client, maker = await make_app(fake, monkeypatch)
        r = await connect(client)
        # Token still connects — the missing Workers scope is optional.
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["account_id"] == FakeCF.ACCOUNT
        assert data["pages_ok"] is True
        assert data["workers_ok"] is False
        async with maker() as s:
            state = await cf.load_state(s)
            assert cf.get_token(state) == "test-token"  # token was persisted
            assert state["workers_ok"] is False
    run(body())


def test_pages_probe_403_is_non_blocking(monkeypatch):
    async def body():
        fake = FakeCF(pages_403=True)
        client, _ = await make_app(fake, monkeypatch)
        r = await connect(client)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["pages_ok"] is False
        assert data["workers_ok"] is True
    run(body())


# ── frontend pre-filled URL: decodes to the six permission groups ─────────────

EXPECTED_PERMS = [
    {"key": "argo_tunnel", "type": "edit"},
    {"key": "account_settings", "type": "read"},
    {"key": "dns", "type": "edit"},
    {"key": "zone", "type": "edit"},
    {"key": "page", "type": "edit"},
    {"key": "workers_scripts", "type": "edit"},
]

# The canonical link is duplicated (byte-identical) in these three files.
_URL_FILES = ["Onboarding.tsx", "Integrations.tsx", "IntegrationDetail.tsx"]
_URL_RE = re.compile(r'"(https://dash\.cloudflare\.com/profile/api-tokens\?[^"]+)"')


def _extract_url(name: str) -> str:
    text = (FRONTEND / name).read_text()
    m = _URL_RE.search(text)
    assert m, f"no CF token URL found in {name}"
    return m.group(1)


def test_prefill_url_decodes_to_six_permission_groups():
    url = _extract_url("Onboarding.tsx")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    perms = json.loads(q["permissionGroupKeys"][0])
    assert perms == EXPECTED_PERMS
    assert q["name"] == ["Homebox Admin"]
    assert q["accountId"] == ["*"]
    assert q["zoneId"] == ["all"]


def test_prefill_url_identical_across_all_three_files():
    urls = {name: _extract_url(name) for name in _URL_FILES}
    assert len(set(urls.values())) == 1, f"CF token URLs drifted: {urls}"
