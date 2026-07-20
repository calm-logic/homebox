"""End-to-end vault integration: the node-side vault engine (vaultlib, real
crypto, sqlite) talking to the REAL control-plane FastAPI app over an
in-process ASGI transport — no mocked _cp, no FakeControlPlane. Catches wire
drift between host/admin/app/vaultlib.py and cloud/control-plane/main.py:
payload shapes, auth, 404/409 handling, Fernet/b64 formats, and proves
encryption at rest (plaintext secrets never appear in the CP database).

Runs inside the normal host/admin suite; the control plane is imported from
cloud/control-plane with its own throwaway sqlite file.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

REPO_ROOT = Path(__file__).resolve().parents[3]
CP_MAIN = REPO_ROOT / "cloud" / "control-plane" / "main.py"

# Control-plane env must be pinned before the module executes (it opens its
# DB and derives its signing/master keys at import time).
os.environ["CONTROL_PLANE_DB"] = tempfile.mktemp(suffix=".sqlite3")
os.environ.setdefault("ACCOUNTS_MODE", "open")
for _k in ("GCS_BUCKET", "STRIPE_SECRET_KEY", "VAULT_MASTER_KEY"):
    os.environ.pop(_k, None)

_spec = importlib.util.spec_from_file_location("cp_main_e2e", CP_MAIN)
cp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cp)

from app import clusterlib, crypto, vaultlib  # noqa: E402
from app.models import Integration, Project  # noqa: E402

from test_vaultlib import (  # noqa: E402
    KEY_A, KEY_B, T1, link_account, make_session, run, seed_cluster_a, use_key,
)

TOKEN = "acct-token"  # matches link_account()'s stored token


@pytest.fixture(autouse=True)
def real_cp(monkeypatch):
    """Route every clusterlib._cp HTTP call into the real CP app, and give it
    a clean account each test."""
    prev_mode = cp.ACCOUNTS_MODE
    cp.ACCOUNTS_MODE = "open"
    with cp.db() as conn:
        conn.execute("DELETE FROM accounts")
        conn.execute("DELETE FROM account_vaults")
        conn.execute("DELETE FROM account_keys")
        conn.execute(
            "INSERT INTO accounts (account_hash, plan, created_at, updated_at)"
            " VALUES (?,?,?,?)",
            (cp._hash(TOKEN), "free", 0, 0),
        )

    real_client = httpx.AsyncClient

    def client_via_asgi(**kw):
        kw.pop("transport", None)
        return real_client(transport=httpx.ASGITransport(app=cp.app), **kw)

    monkeypatch.setattr(clusterlib.httpx, "AsyncClient", client_via_asgi)
    yield
    cp.ACCOUNTS_MODE = prev_mode


def _cp_db_bytes() -> bytes:
    return Path(os.environ["CONTROL_PLANE_DB"]).read_bytes()


def test_two_clusters_converge_through_real_control_plane():
    async def body():
        # ── Cluster A (KEY_A): seed real config, link, push ──────────────
        sa = await make_session()
        with use_key(KEY_A):
            await link_account(sa)
            plain = await seed_cluster_a(sa)
            adk_a = await vaultlib.ensure_adk(sa)
            tick = await vaultlib.vault_tick(sa)
        assert tick and tick.get("pushed"), f"cluster A never pushed: {tick}"

        # ADK really escrowed on the CP (wrapped, not raw).
        with cp.db() as conn:
            row = conn.execute("SELECT adk_wrapped FROM account_keys").fetchone()
        assert row and adk_a not in row["adk_wrapped"]

        # Encryption at rest: none of the plaintext secrets are anywhere in
        # the CP database file — the blob is opaque Fernet ciphertext.
        raw = _cp_db_bytes()
        for secret in plain.values():
            assert secret.encode() not in raw

        # ── Fresh install B (KEY_B): link + restore ──────────────────────
        sb = await make_session()
        with use_key(KEY_B):
            await link_account(sb)
            result = await vaultlib.restore_on_link(sb)
            assert result and result.get("imported", {}).get("added")

            projects = (await sb.execute(select(Project))).scalars().all()
            assert [p.name for p in projects] == ["listless"]
            integ = (await sb.execute(
                select(Integration).where(Integration.provider == "github")
            )).scalar_one()
            assert crypto.decrypt(integ.secret_encrypted) == plain["pat"]

            # ── Edit on B, tick B → CP ────────────────────────────────────
            projects[0].description = "edited on B"
            projects[0].updated_at = T1 + timedelta(hours=3)
            await sb.commit()
            tick_b = await vaultlib.vault_tick(sb)
            assert tick_b and tick_b.get("pushed")

        # ── Tick A → pulls B's edit ───────────────────────────────────────
        with use_key(KEY_A):
            tick_a = await vaultlib.vault_tick(sa)
            assert tick_a
            proj_a = (await sa.execute(select(Project))).scalar_one()
            await sa.refresh(proj_a)
            assert proj_a.description == "edited on B"

        # B's KEY_B ciphertext never leaked into A's rows: A can still
        # decrypt its integration under KEY_A.
        with use_key(KEY_A):
            integ_a = (await sa.execute(
                select(Integration).where(Integration.provider == "github")
            )).scalar_one()
            assert crypto.decrypt(integ_a.secret_encrypted) == plain["pat"]
    run(body())


def test_adk_survives_and_vault_version_advances():
    async def body():
        sa = await make_session()
        with use_key(KEY_A):
            await link_account(sa)
            await seed_cluster_a(sa)
            adk1 = await vaultlib.ensure_adk(sa)
            await vaultlib.vault_tick(sa)

        # A second node fetches the SAME escrowed ADK (no remint), and the
        # CP reports a monotonically advancing vault version.
        sb = await make_session()
        with use_key(KEY_B):
            await link_account(sb)
            adk2 = await vaultlib.ensure_adk(sb)
        assert adk1 == adk2

        with cp.db() as conn:
            v = conn.execute("SELECT version FROM account_vaults").fetchone()
        assert v and v["version"] >= 1
    run(body())
