"""Onboarding fast path — "Log in with Homebox" (demo-video gap G7).

Pins:

  1. Fresh install: /api/onboarding/state keeps its manual-path shape
     (complete/steps unchanged) and gains account={linked, restoring} plus
     steps.cloudflare_token.synced — all falsy without an account.
  2. A Cloudflare Integration row imported by the vault restore (no manual
     token POST ever) flips steps.cloudflare_token.done — and is reported
     synced=true (vault pulled_at >= the row's preserved updated_at).
  3. A manual token paste (cf.save_state stamps updated_at=now) is NOT
     reported as synced even when a vault pull happened earlier.
  4. state.account.linked mirrors the clusterlib account setting; restoring
     mirrors vault_state.restoring.
  5. POST /api/onboarding/auto-tunnel delegates to routes.tunnel.connect_tunnel
     with the default name, and short-circuits idempotently ("already") once a
     tunnel is configured; 400 without a token.
  6. complete flips true with synced integration + tunnel, without any manual
     endpoint call.

Runs on in-memory sqlite; routes driven through httpx ASGITransport with auth
and DB dependencies overridden; the tunnel-connect flow mocked at the exact
function onboarding calls.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app import cloudflare as cf  # noqa: E402
from app import clusterlib, crypto, vaultlib  # noqa: E402
from app.auth import require_session_api  # noqa: E402
from app.db import Base, get_session  # noqa: E402
from app.models import Integration  # noqa: E402
from app.routes import onboarding as onboarding_routes  # noqa: E402

_ENGINES: list = []


def run(coro):
    async def main():
        try:
            return await coro
        finally:
            while _ENGINES:
                await _ENGINES.pop().dispose()
    return asyncio.run(main())


async def make_sessionmaker():
    engine = create_async_engine("sqlite+aiosqlite://")
    _ENGINES.append(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


async def make_app():
    """Fresh sqlite DB + FastAPI app mounting the onboarding router. Returns
    (client, sessionmaker)."""
    maker = await make_sessionmaker()

    app = FastAPI()
    app.include_router(onboarding_routes.router)

    async def override_session():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[require_session_api] = lambda: "tester"

    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )
    return client, maker


# ───── fixtures-in-functions ──────────────────────────────────────────────────


async def link_account(session) -> None:
    """Minimal linked-account blob, as clusterlib.link_account_flow writes."""
    await clusterlib._set_setting(session, clusterlib.ACCOUNT_KEY, {
        "control_plane_url": "https://control.homebox.sh",
        "token_encrypted": crypto.encrypt("acct-token"),
        "node_name": "testbox",
        "peer_url": "http://localhost:7765",
        "linked_at": datetime.utcnow().isoformat(),
    })
    await session.commit()


async def set_vault_state(session, **fields) -> None:
    await clusterlib._set_setting(session, vaultlib.VAULT_STATE_KEY, fields)
    await session.commit()


async def add_synced_cloudflare_integration(session, *, tunnel: bool = False) -> None:
    """A Cloudflare Integration row exactly as the vault import writes it:
    config carries the (re-encrypted) token, updated_at preserved from the
    exporting node (i.e. OLDER than the vault pull timestamp)."""
    config = {
        "token_encrypted": crypto.encrypt("cf-token"),
        "account_id": "acc-1",
        "account_name": "Synced Account",
    }
    if tunnel:
        config["tunnel_id"] = "tun-1"
        config["tunnel_name"] = "homebox"
    session.add(Integration(
        provider="cloudflare",
        name="Synced Account",
        account_id="acc-1",
        account_login="Synced Account",
        config=config,
        secret_encrypted=config["token_encrypted"],
        status="connected",
        updated_at=datetime.utcnow() - timedelta(days=3),
    ))
    await session.commit()


# ───── 1. fresh install: shape regression ─────────────────────────────────────


def test_state_shape_fresh_install():
    async def t():
        client, _ = await make_app()
        r = await client.get("/api/onboarding/state")
        assert r.status_code == 200
        body = r.json()

        # Manual-path contract, unchanged.
        assert body["complete"] is False
        assert body["steps"]["cloudflare_token"]["done"] is False
        assert body["steps"]["cloudflare_token"]["account_name"] is None
        assert body["steps"]["tunnel"] == {"done": False, "tunnel_name": None}
        assert body["steps"]["admin_domain"] == {"done": False, "hostname": None}

        # New additive fields, falsy without an account.
        assert body["account"] == {"linked": False, "restoring": False}
        assert body["steps"]["cloudflare_token"]["synced"] is False
        await client.aclose()
    run(t())


# ───── 2. synced integration completes step 1 without any manual call ─────────


def test_cloudflare_step_done_from_synced_integration():
    async def t():
        client, maker = await make_app()
        async with maker() as session:
            await link_account(session)
            await add_synced_cloudflare_integration(session)
            await set_vault_state(session, pulled_at=datetime.utcnow().isoformat())

        r = await client.get("/api/onboarding/state")
        body = r.json()
        step = body["steps"]["cloudflare_token"]
        assert step["done"] is True
        assert step["synced"] is True
        assert step["account_name"] == "Synced Account"
        assert body["account"]["linked"] is True
        assert body["complete"] is False  # tunnel still missing
        await client.aclose()
    run(t())


def test_restoring_flag_surfaced():
    async def t():
        client, maker = await make_app()
        async with maker() as session:
            await link_account(session)
            await set_vault_state(session, restoring=True)
        r = await client.get("/api/onboarding/state")
        assert r.json()["account"] == {"linked": True, "restoring": True}
        await client.aclose()
    run(t())


# ───── 3. manual paste is not "synced" ────────────────────────────────────────


def test_manual_token_not_reported_synced():
    async def t():
        client, maker = await make_app()
        async with maker() as session:
            await link_account(session)
            # Vault pulled earlier (nothing in it), THEN the user pastes a
            # token manually — save_state stamps updated_at=now > pulled_at.
            await set_vault_state(
                session,
                pulled_at=(datetime.utcnow() - timedelta(hours=1)).isoformat(),
            )
            state = {}
            cf.store_token(state, "pasted-token")
            state["account_name"] = "Manual Account"
            await cf.save_state(session, state)

        r = await client.get("/api/onboarding/state")
        step = r.json()["steps"]["cloudflare_token"]
        assert step["done"] is True
        assert step["synced"] is False
        await client.aclose()
    run(t())


def test_no_vault_pull_means_not_synced():
    async def t():
        client, maker = await make_app()
        async with maker() as session:
            await add_synced_cloudflare_integration(session)  # no vault_state at all
        step = (await client.get("/api/onboarding/state")).json()["steps"]["cloudflare_token"]
        assert step["done"] is True
        assert step["synced"] is False
        await client.aclose()
    run(t())


# ───── 4. auto-tunnel: delegates to connect_tunnel, idempotent ────────────────


def test_auto_tunnel_calls_connect_and_is_idempotent(monkeypatch):
    calls: list = []

    async def fake_connect_tunnel(body, user=None, session=None):
        calls.append(body)
        # Persist a tunnel like the real flow does, so the next call
        # short-circuits on state["tunnel_id"].
        state = await cf.load_state(session)
        state["tunnel_id"] = "tun-new"
        state["tunnel_name"] = body.name
        await cf.save_state(session, state)
        return {"ok": True, "tunnel_id": "tun-new", "tunnel_name": body.name, "adopted": False}

    monkeypatch.setattr(onboarding_routes, "connect_tunnel", fake_connect_tunnel)

    async def t():
        client, maker = await make_app()
        async with maker() as session:
            await add_synced_cloudflare_integration(session)

        r1 = await client.post("/api/onboarding/auto-tunnel")
        assert r1.status_code == 200
        assert r1.json()["ok"] is True
        assert len(calls) == 1
        assert calls[0].name == "homebox"  # default ConnectTunnelBody name

        # Second call: tunnel already configured → no second connect.
        r2 = await client.post("/api/onboarding/auto-tunnel")
        assert r2.status_code == 200
        assert r2.json() == {
            "ok": True, "already": True,
            "tunnel_id": "tun-new", "tunnel_name": "homebox",
        }
        assert len(calls) == 1

        # And the state probe now reports step 2 done + complete.
        body = (await client.get("/api/onboarding/state")).json()
        assert body["steps"]["tunnel"] == {"done": True, "tunnel_name": "homebox"}
        assert body["complete"] is True
        await client.aclose()
    run(t())


def test_auto_tunnel_without_token_is_400(monkeypatch):
    async def boom(body, user=None, session=None):  # pragma: no cover
        raise AssertionError("connect_tunnel must not be called without a token")

    monkeypatch.setattr(onboarding_routes, "connect_tunnel", boom)

    async def t():
        client, _ = await make_app()
        r = await client.post("/api/onboarding/auto-tunnel")
        assert r.status_code == 400
        await client.aclose()
    run(t())


# ───── 5. complete flips with synced integration + tunnel, zero manual calls ──


def test_complete_from_fully_synced_state():
    async def t():
        client, maker = await make_app()
        async with maker() as session:
            await link_account(session)
            await add_synced_cloudflare_integration(session, tunnel=True)
            await set_vault_state(session, pulled_at=datetime.utcnow().isoformat())

        body = (await client.get("/api/onboarding/state")).json()
        assert body["complete"] is True
        assert body["steps"]["cloudflare_token"]["synced"] is True
        assert body["steps"]["tunnel"] == {"done": True, "tunnel_name": "homebox"}
        await client.aclose()
    run(t())


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
