"""Tests for the account-flow work (demo gaps G3/G3b/G4/G6):

- G3: password-only first login (username optional; explicit username still
  accepted) + the unauthenticated GET /api/auth/login-options probe.
- G4: provider access tokens persisted ENCRYPTED on successful OAuth flows;
  POST /api/cluster/account/link-silent re-auths from a stored token (412
  when none usable; a CP-rejected token is deleted).
- G3b: the post-link pipeline auto-creates an enabled Identity for the
  account's verified email — and never before/without a link.
- G6: the pipeline founds a NEW empty cluster after the restore (deduped
  machine name), stays standalone on CP 402, and NEVER auto-joins.

Runs on in-memory sqlite (aiosqlite) with clusterlib._cp monkeypatched —
no network, no Postgres. Harness patterns follow test_directives.py /
test_vaultlib.py.
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bcrypt  # noqa: E402
from fastapi import FastAPI, HTTPException, Response  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.db import Base, get_session  # noqa: E402
from app.auth import require_session_api  # noqa: E402
from app import clusterlib, crypto, vaultlib  # noqa: E402
from app.config import settings  # noqa: E402
from app.models import Identity, Setting  # noqa: E402
from app.routes import auth as auth_routes  # noqa: E402
from app.routes import cluster as cluster_routes  # noqa: E402
from app.routes import oauth as oauth_routes  # noqa: E402

T0 = datetime(2026, 7, 1, 12, 0, 0)

_ENGINES: list = []


def run(coro):
    async def main():
        try:
            return await coro
        finally:
            while _ENGINES:
                await _ENGINES.pop().dispose()
    return asyncio.run(main())


async def make_session():
    engine = create_async_engine("sqlite+aiosqlite://")
    _ENGINES.append(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)()


async def link_account(session, cp_url="https://cp.test"):
    session.add(Setting(key=clusterlib.ACCOUNT_KEY, value={
        "control_plane_url": cp_url,
        "token_encrypted": crypto.encrypt("acct-token"),
        "node_name": "test-node", "peer_url": "http://n1",
        "linked_at": T0.isoformat(),
    }))
    await session.commit()


# ───── G3: password-only login + login-options ───────────────────────────────


PLAIN_PW = "printed-pw"


def make_auth_client(tmp_path, monkeypatch, *, setup=None, providers=None):
    """TestClient over the auth router with a temp secrets.json and a mocked
    oauth-proxy providers probe (no network)."""
    secrets_file = tmp_path / "secrets.json"
    secrets_file.write_text(json.dumps({"admin": {
        "username": "homebox",
        "password_hash": bcrypt.hashpw(
            PLAIN_PW.encode(), bcrypt.gensalt(rounds=4)).decode(),
    }}))
    monkeypatch.setattr(settings, "homebox_secrets_path", secrets_file)

    async def fake_providers():
        return dict(providers or {"github": True, "google": False})
    monkeypatch.setattr(auth_routes, "_proxy_providers", fake_providers)

    holder: dict = {}

    async def _get_session():
        if "maker" not in holder:
            engine = create_async_engine("sqlite+aiosqlite://")
            holder["engine"] = engine
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            holder["maker"] = async_sessionmaker(engine, expire_on_commit=False)
            if setup is not None:
                async with holder["maker"]() as s:
                    await setup(s)
        async with holder["maker"]() as s:
            yield s

    app = FastAPI()
    app.include_router(auth_routes.router)
    app.dependency_overrides[get_session] = _get_session
    return TestClient(app)


def test_login_password_only(tmp_path, monkeypatch):
    with make_auth_client(tmp_path, monkeypatch) as client:
        r = client.post("/api/auth/login", json={"password": PLAIN_PW})
        assert r.status_code == 200
        assert r.json() == {"ok": True, "username": "homebox"}
        assert settings.session_cookie in r.cookies


def test_login_explicit_username_backcompat(tmp_path, monkeypatch):
    with make_auth_client(tmp_path, monkeypatch) as client:
        r = client.post("/api/auth/login",
                        json={"username": "homebox", "password": PLAIN_PW})
        assert r.status_code == 200 and r.json()["username"] == "homebox"
        # A wrong explicit username is still rejected (no silent fallback).
        r = client.post("/api/auth/login",
                        json={"username": "root", "password": PLAIN_PW})
        assert r.status_code == 401


def test_login_wrong_password_401_both_shapes(tmp_path, monkeypatch):
    with make_auth_client(tmp_path, monkeypatch) as client:
        assert client.post("/api/auth/login",
                           json={"password": "nope"}).status_code == 401
        assert client.post("/api/auth/login",
                           json={"username": "homebox", "password": "nope"}).status_code == 401


def test_login_options_flags(tmp_path, monkeypatch):
    with make_auth_client(tmp_path, monkeypatch,
                          providers={"github": True, "google": True}) as client:
        r = client.get("/api/auth/login-options")
        assert r.status_code == 200
        assert r.json() == {"oauth_providers": ["github", "google"],
                            "has_identities": False}


def test_login_options_true_with_enabled_identity(tmp_path, monkeypatch):
    async def setup(s):
        s.add(Identity(email="al@test", enabled=True))
        await s.commit()
    with make_auth_client(tmp_path, monkeypatch, setup=setup) as client:
        data = client.get("/api/auth/login-options").json()
        assert data == {"oauth_providers": ["github"], "has_identities": True}


def test_login_options_ignores_disabled_identities(tmp_path, monkeypatch):
    async def setup(s):
        s.add(Identity(email="al@test", enabled=False))
        await s.commit()
    with make_auth_client(tmp_path, monkeypatch, setup=setup) as client:
        assert client.get("/api/auth/login-options").json()["has_identities"] is False


# ───── G4: provider token persisted encrypted on OAuth login ─────────────────


def test_finish_login_persists_encrypted_provider_token(monkeypatch):
    async def body():
        session = await make_session()
        session.add(Identity(email="al@test", enabled=True))
        await session.commit()

        async def fake_resolve(provider, token):
            return "al@test"
        monkeypatch.setattr(oauth_routes, "_resolve_verified_email", fake_resolve)

        res = await oauth_routes._finish_login(
            "github", "gh-token-123", Response(), session)
        assert res["ok"] is True

        row = (await session.execute(select(Setting).where(
            Setting.key == clusterlib.PROVIDER_TOKENS_KEY))).scalar_one()
        entry = row.value["github:al@test"]
        assert crypto.decrypt(entry["token_encrypted"]) == "gh-token-123"
        assert entry["saved_at"]
        # Never plaintext at rest.
        assert "gh-token-123" not in json.dumps(row.value)
    run(body())


def test_finish_login_pre_link_rejected_and_creates_nothing(monkeypatch):
    """Before any link, the whitelist stands: unknown emails are 403'd, no
    Identity is created, and no provider token is stored."""
    async def body():
        session = await make_session()

        async def fake_resolve(provider, token):
            return "stranger@test"
        monkeypatch.setattr(oauth_routes, "_resolve_verified_email", fake_resolve)

        try:
            await oauth_routes._finish_login("github", "tok", Response(), session)
            assert False, "expected 403"
        except HTTPException as e:
            assert e.status_code == 403
        assert (await session.execute(select(Identity))).scalars().all() == []
        assert await clusterlib.load_provider_tokens(session) == {}
    run(body())


# ───── link-silent route ─────────────────────────────────────────────────────


def make_cluster_client(monkeypatch, setup=None):
    """(TestClient over the cluster router, holder) — holder exposes the maker
    so tests can inspect DB state afterwards. schedule_post_link is always
    stubbed (recorded) so no background task touches the real app DB."""
    holder: dict = {"scheduled": []}

    def fake_schedule(*, provider=None, email=None):
        holder["scheduled"].append({"provider": provider, "email": email})
    monkeypatch.setattr(vaultlib, "schedule_post_link", fake_schedule)
    # routes/cluster.py calls vaultlib.schedule_post_link via the module attr,
    # so the stub above covers it.

    async def _get_session():
        if "maker" not in holder:
            engine = create_async_engine("sqlite+aiosqlite://")
            holder["engine"] = engine
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            holder["maker"] = async_sessionmaker(engine, expire_on_commit=False)
            if setup is not None:
                async with holder["maker"]() as s:
                    await setup(s)
        async with holder["maker"]() as s:
            yield s

    app = FastAPI()
    app.include_router(cluster_routes.router)
    app.dependency_overrides[get_session] = _get_session
    app.dependency_overrides[require_session_api] = lambda: "tester@test"
    return TestClient(app), holder


class CPRecorder:
    def __init__(self, monkeypatch, responses=None):
        self.calls: list[tuple[str, str, dict | None]] = []
        self.responses = responses or {}

        async def _cp(method, base, path, *, token=None, body=None):
            self.calls.append((method, path, body))
            key = (method, path)
            if key in self.responses:
                resp = self.responses[key]
                if isinstance(resp, Exception):
                    raise resp
                return resp
            return {}
        monkeypatch.setattr(clusterlib, "_cp", _cp)


async def seed_provider_token(s, provider="github", email="al@test",
                              token="gh-tok", saved_at="2026-07-17T00:00:00"):
    tokens = {}
    row = (await s.execute(select(Setting).where(
        Setting.key == clusterlib.PROVIDER_TOKENS_KEY))).scalar_one_or_none()
    if row is not None:
        tokens = dict(row.value)
    tokens[f"{provider}:{email}"] = {
        "token_encrypted": crypto.encrypt(token), "saved_at": saved_at}
    if row is None:
        s.add(Setting(key=clusterlib.PROVIDER_TOKENS_KEY, value=tokens))
    else:
        row.value = tokens
    await s.commit()


def test_link_silent_happy_path(monkeypatch):
    cp = CPRecorder(monkeypatch, responses={
        ("POST", "/v1/accounts/register"): {"account_token": "acct-tok",
                                            "email": "al@test"},
    })

    async def setup(s):
        await seed_provider_token(s)
    client, holder = make_cluster_client(monkeypatch, setup)
    with client:
        r = client.post("/api/cluster/account/link-silent", json={})
        assert r.status_code == 200
        assert r.json() == {"ok": True, "provider": "github", "email": "al@test"}

        # It registered with the STORED token (decrypted just-in-time).
        reg = next(b for m, p, b in cp.calls if p == "/v1/accounts/register")
        assert reg["provider"] == "github" and reg["access_token"] == "gh-tok"
        # ... and linked the node with the minted account token.
        assert any(p == "/v1/accounts/nodes" for _, p, _b in cp.calls)
        # The same post-link pipeline as the OAuth flow was scheduled.
        assert holder["scheduled"] == [{"provider": "github", "email": "al@test"}]

    async def check():
        async with holder["maker"]() as s:
            acct = await clusterlib.load_account(s)
            assert acct and crypto.decrypt(acct["token_encrypted"]) == "acct-tok"
    run(check())


def test_link_silent_412_when_no_stored_token(monkeypatch):
    CPRecorder(monkeypatch)
    client, holder = make_cluster_client(monkeypatch)
    with client:
        r = client.post("/api/cluster/account/link-silent", json={})
        assert r.status_code == 412
        assert r.json()["detail"] == "no stored provider token"
        assert holder["scheduled"] == []


def test_link_silent_412_when_no_matching_provider(monkeypatch):
    CPRecorder(monkeypatch)

    async def setup(s):
        await seed_provider_token(s, provider="github")
    client, _holder = make_cluster_client(monkeypatch, setup)
    with client:
        r = client.post("/api/cluster/account/link-silent", json={"provider": "google"})
        assert r.status_code == 412


def test_link_silent_invalid_token_deleted_then_412(monkeypatch):
    cp = CPRecorder(monkeypatch, responses={
        ("POST", "/v1/accounts/register"): clusterlib.ControlPlaneError(
            "bad token", status_code=401, detail="provider token rejected"),
    })

    async def setup(s):
        await seed_provider_token(s)
    client, holder = make_cluster_client(monkeypatch, setup)
    with client:
        r = client.post("/api/cluster/account/link-silent", json={})
        assert r.status_code == 412
        assert cp.calls  # it did try the CP
        assert holder["scheduled"] == []

    async def check():
        async with holder["maker"]() as s:
            assert await clusterlib.load_provider_tokens(s) == {}  # deleted
            assert await clusterlib.load_account(s) is None        # not linked
    run(check())


def test_link_silent_uses_freshest_token_and_falls_through_401(monkeypatch):
    """Two stored tokens: the fresher one is tried first; when the CP rejects
    it (401) it's deleted and the older one succeeds."""
    rejected = {"count": 0}

    class _CP:
        def __init__(self, mp):
            self.calls = []

            async def _cp(method, base, path, *, token=None, body=None):
                self.calls.append((method, path, body))
                if path == "/v1/accounts/register":
                    if body["access_token"] == "fresh-tok":
                        rejected["count"] += 1
                        raise clusterlib.ControlPlaneError(
                            "bad", status_code=401, detail="rejected")
                    return {"account_token": "acct-tok", "email": "old@test"}
                return {}
            mp.setattr(clusterlib, "_cp", _cp)
    cp = _CP(monkeypatch)

    async def setup(s):
        await seed_provider_token(s, email="old@test", token="old-tok",
                                  saved_at="2026-07-01T00:00:00")
        await seed_provider_token(s, email="fresh@test", token="fresh-tok",
                                  saved_at="2026-07-17T00:00:00")
    client, holder = make_cluster_client(monkeypatch, setup)
    with client:
        r = client.post("/api/cluster/account/link-silent", json={})
        assert r.status_code == 200 and r.json()["email"] == "old@test"
        assert rejected["count"] == 1
        tried = [b["access_token"] for m, p, b in cp.calls
                 if p == "/v1/accounts/register"]
        assert tried == ["fresh-tok", "old-tok"]  # freshest first

    async def check():
        async with holder["maker"]() as s:
            tokens = await clusterlib.load_provider_tokens(s)
            assert list(tokens) == ["github:old@test"]  # rejected one deleted
    run(check())


def test_link_silent_passes_through_cp_plan_gate(monkeypatch):
    CPRecorder(monkeypatch, responses={
        ("POST", "/v1/accounts/register"): clusterlib.ControlPlaneError(
            "gate", status_code=402, detail="upgrade required"),
    })

    async def setup(s):
        await seed_provider_token(s)
    client, _holder = make_cluster_client(monkeypatch, setup)
    with client:
        r = client.post("/api/cluster/account/link-silent", json={})
        assert r.status_code == 402
        assert r.json()["detail"] == "upgrade required"


# ───── post-link pipeline (G3b identity + G6 default cluster) ────────────────


class FakeCP:
    """In-memory control plane covering everything the pipeline touches:
    ADK escrow, vault CAS, /accounts/me, /accounts/topology, cluster create.
    Never serves a join — and records every call so tests can prove no join
    was ever attempted."""

    def __init__(self, *, email="al@test", features=("cluster",),
                 clusters=None, create_status: int | None = None):
        self.adk_b64: str | None = None
        self.vault: dict | None = None
        self.calls: list[tuple[str, str, dict | None]] = []
        self.email = email
        self.features = list(features)
        self.clusters = clusters or []
        self.create_status = create_status
        self.created: list[dict] = []

    def install(self, monkeypatch):
        async def _cp(method, base, path, *, token=None, body=None):
            return self.handle(method, path, body)
        monkeypatch.setattr(clusterlib, "_cp", _cp)
        # create_cluster_flow side effects that touch the host filesystem.
        monkeypatch.setattr(clusterlib, "_write_cluster_keys", lambda *a, **k: None)

        async def no_route(session):
            return None
        monkeypatch.setattr(clusterlib, "ensure_peer_route", no_route)
        from app import licenselib

        async def no_verify(session, state, cp_url):
            return True, "test"
        monkeypatch.setattr(licenselib, "record_license_verification", no_verify)
        return self

    def handle(self, method, path, body):
        self.calls.append((method, path, body))
        if path == "/v1/accounts/keys/adk":
            if method == "GET":
                if not self.adk_b64:
                    raise clusterlib.ControlPlaneError("no adk", status_code=404, detail="no adk")
                return {"adk_b64": self.adk_b64}
            if method == "PUT":
                self.adk_b64 = body["adk_b64"]
                return {"ok": True}
        if path == "/v1/accounts/vault":
            if method == "GET":
                if self.vault is None:
                    raise clusterlib.ControlPlaneError("no vault", status_code=404, detail="no vault")
                return dict(self.vault)
            if method == "PUT":
                current = (self.vault or {}).get("version", 0)
                if int(body.get("version_expected") or 0) != current:
                    raise clusterlib.ControlPlaneError("conflict", status_code=409, detail="conflict")
                self.vault = {"version": current + 1, "blob_b64": body["blob_b64"]}
                return {"version": self.vault["version"]}
        if (method, path) == ("GET", "/v1/accounts/me"):
            return {"email": self.email, "plan": "premium", "features": self.features}
        if (method, path) == ("GET", "/v1/accounts/topology"):
            return {"clusters": list(self.clusters), "standalone_nodes": []}
        if (method, path) == ("POST", "/v1/clusters"):
            if self.create_status:
                raise clusterlib.ControlPlaneError(
                    "plan gate", status_code=self.create_status,
                    detail="Your plan doesn't include clustering.")
            self.created.append(body)
            return {"cluster_id": "c-new", "name": body["name"],
                    "node_token": "ntok",
                    "nodes": [{"node_id": body["node_id"], "ordinal": 1,
                               "role": "peer", "online": True}],
                    "license": {"plan": "premium", "features": ["cluster"]}}
        if method == "POST" and path.startswith("/v1/accounts/"):
            return {}  # nodes register / poll / backup — irrelevant here
        if method == "PUT" and path.startswith("/v1/accounts/nodes/"):
            return {}
        raise clusterlib.ControlPlaneError(
            f"unexpected CP call {method} {path}", status_code=500)

    def join_calls(self):
        """Any call that would join an existing cluster."""
        return [(m, p) for m, p, _b in self.calls
                if m == "POST" and p.startswith("/v1/clusters/")]


def test_pipeline_creates_identity_and_default_cluster_deduped(monkeypatch):
    async def body():
        cp = FakeCP(clusters=[{"cluster_id": "c0", "name": "test-node"}]).install(monkeypatch)
        session = await make_session()
        await link_account(session)

        final = await vaultlib.post_link_pipeline(session, provider="github")
        assert final["stage"] == "done"

        # G3b: identity auto-created, enabled, from the account email.
        ident = (await session.execute(select(Identity))).scalar_one()
        assert ident.email == "al@test" and ident.enabled is True
        assert final["identity_email"] == "al@test"
        assert final["identity_created"] is True

        # G6: a NEW cluster was founded, named after the machine but deduped
        # against the account's existing "test-node" cluster.
        state = await clusterlib.load_cluster(session)
        assert state and state["cluster_id"] == "c-new"
        assert cp.created[0]["name"] == "test-node-2"
        assert final["cluster_created"] is True
        assert final["cluster_name"] == "test-node-2"

        # ... and no join was ever attempted (POST /v1/clusters is the only
        # cluster call; /v1/clusters/{id}/nodes would be a join).
        assert cp.join_calls() == []

        # Restore ran (vault seeded from this node's state).
        assert final["restore_ok"] is True
        assert cp.vault and cp.vault["version"] >= 1
    run(body())


def test_pipeline_oauth_login_works_after_link(monkeypatch):
    """The demo beat: GitHub admin login works right after linking."""
    async def body():
        FakeCP().install(monkeypatch)
        session = await make_session()
        await link_account(session)
        await vaultlib.post_link_pipeline(session, provider="github")

        async def fake_resolve(provider, token):
            return "al@test"
        monkeypatch.setattr(oauth_routes, "_resolve_verified_email", fake_resolve)
        res = await oauth_routes._finish_login("github", "tok", Response(), session)
        assert res == {"ok": True, "purpose": "login", "redirect": "/"}
    run(body())


def test_pipeline_standalone_on_402_never_joins(monkeypatch):
    async def body():
        cp = FakeCP(create_status=402,
                    clusters=[{"cluster_id": "c0", "name": "home"}]).install(monkeypatch)
        session = await make_session()
        await link_account(session)

        final = await vaultlib.post_link_pipeline(session)
        assert final["stage"] == "done"
        assert final["cluster_created"] is False
        assert final["standalone"] is True
        assert await clusterlib.load_cluster(session) is None
        # Free plan + an existing cluster on the account — still no join.
        assert cp.join_calls() == []
        # Identity still landed (independent of the plan gate).
        assert (await session.execute(select(Identity))).scalar_one().enabled is True
    run(body())


def test_pipeline_skips_cluster_step_when_already_clustered(monkeypatch):
    async def body():
        cp = FakeCP().install(monkeypatch)
        session = await make_session()
        await link_account(session)
        await clusterlib.save_cluster(session, {
            "cluster_id": "c1", "control_plane_url": "https://cp.test",
            "roster": [], "initial_sync_done": True,
        })
        await session.commit()

        final = await vaultlib.post_link_pipeline(session)
        assert final["cluster_created"] is False
        assert not any(p == "/v1/clusters" for _m, p, _b in cp.calls)
        assert cp.join_calls() == []
        state = await clusterlib.load_cluster(session)
        assert state["cluster_id"] == "c1"  # untouched
    run(body())


def test_pipeline_no_cluster_when_restore_fails(monkeypatch):
    """G6 runs strictly AFTER a completed restore — a failed restore leaves
    the node unclustered (retry comes with the next link/loop)."""
    async def body():
        cp = FakeCP().install(monkeypatch)
        session = await make_session()
        await link_account(session)

        async def boom(s):
            raise vaultlib.VaultError("vault down")
        monkeypatch.setattr(vaultlib, "restore_on_link", boom)

        final = await vaultlib.post_link_pipeline(session)
        assert final["restore_ok"] is False
        assert "vault down" in final["restore_error"]
        assert final["cluster_created"] is False
        assert await clusterlib.load_cluster(session) is None
        assert not any(p == "/v1/clusters" for _m, p, _b in cp.calls)
    run(body())


def test_pipeline_state_transitions(monkeypatch):
    async def body():
        FakeCP().install(monkeypatch)
        session = await make_session()
        await link_account(session)

        stages: list[str] = []
        orig = vaultlib._save_post_link

        async def spy(s, **kw):
            if kw.get("stage"):
                stages.append(kw["stage"])
            return await orig(s, **kw)
        monkeypatch.setattr(vaultlib, "_save_post_link", spy)

        await vaultlib.post_link_pipeline(session, provider="github")
        assert stages == ["restoring", "identity", "cluster", "done"]

        st = await vaultlib.get_post_link_state(session)
        assert st["stage"] == "done"
        assert st["started_at"] and st["finished_at"] and st["updated_at"]
        assert st["provider"] == "github"
        assert st["error"] is None
    run(body())


def test_account_status_exposes_post_link(monkeypatch):
    async def setup(s):
        await link_account(s)
        s.add(Setting(key=vaultlib.POST_LINK_KEY, value={
            "stage": "cluster", "restore_ok": True,
            "identity_email": "al@test", "updated_at": "2026-07-18T00:00:00"}))
        await s.commit()
    CPRecorder(monkeypatch)
    client, _holder = make_cluster_client(monkeypatch, setup)
    with client:
        r = client.get("/api/cluster/account")
        assert r.status_code == 200
        data = r.json()
        assert data["linked"] is True
        assert data["post_link"]["stage"] == "cluster"
        assert data["post_link"]["identity_email"] == "al@test"
