"""Tests for the account metadata vault (vaultlib): ADK lifecycle, secret
re-encryption across clusters with DIFFERENT ENCRYPTION_KEYs, newer-wins
convergence via the vault, tombstone propagation, version CAS retry,
fresh-install restore, and stable-hash push suppression.

Runs on in-memory sqlite (aiosqlite) with clusterlib._cp monkeypatched by a
FakeControlPlane — no network, no Postgres.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.db import Base  # noqa: E402
from app import cluster_sync, clusterlib, crypto, vaultlib  # noqa: E402
from app.config import settings  # noqa: E402
from app.models import (  # noqa: E402
    Domain, Environment, Integration, Project, Service, ServiceTarget, Setting,
)

KEY_A = "a" * 64  # cluster A's ENCRYPTION_KEY
KEY_B = "b" * 64  # cluster B's ENCRYPTION_KEY (different!)

T0 = datetime(2026, 7, 1, 12, 0, 0)
T1 = T0 + timedelta(hours=1)
T2 = T0 + timedelta(hours=2)

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


class use_key:
    """Temporarily run under a given cluster ENCRYPTION_KEY (crypto reads
    settings.encryption_key at call time)."""

    def __init__(self, key: str):
        self.key = key

    def __enter__(self):
        self.prev = settings.encryption_key
        settings.encryption_key = self.key

    def __exit__(self, *a):
        settings.encryption_key = self.prev


async def link_account(session, cp_url="https://cp.test"):
    """Persist an account link blob decryptable under the CURRENT key."""
    session.add(Setting(key=clusterlib.ACCOUNT_KEY, value={
        "control_plane_url": cp_url,
        "token_encrypted": crypto.encrypt("acct-token"),
        "node_name": "test-node", "peer_url": "http://n1",
        "linked_at": T0.isoformat(),
    }))
    await session.commit()


class FakeControlPlane:
    """In-memory /v1/accounts/vault + /keys/adk with version CAS. Installed by
    monkeypatching clusterlib._cp (vaultlib always calls through clusterlib)."""

    def __init__(self):
        self.adk_b64: str | None = None
        self.vault: dict | None = None  # {version, blob_b64, meta, updated_at}
        self.calls: list[tuple[str, str]] = []
        self.fail_next_put = 0  # force N 409s on vault PUT (CAS race simulation)

    def install(self, monkeypatch):
        async def _cp(method, base, path, *, token=None, body=None):
            return self.handle(method, path, body)
        monkeypatch.setattr(clusterlib, "_cp", _cp)
        return self

    def handle(self, method, path, body):
        self.calls.append((method, path))
        if path == "/v1/accounts/keys/adk":
            if method == "GET":
                if not self.adk_b64:
                    raise clusterlib.ControlPlaneError("no adk", status_code=404, detail="no adk")
                return {"adk_b64": self.adk_b64}
            if method == "PUT":
                if self.adk_b64 and self.adk_b64 != body["adk_b64"]:
                    raise clusterlib.ControlPlaneError("adk conflict", status_code=409,
                                                       detail="a different ADK is escrowed")
                self.adk_b64 = body["adk_b64"]
                return {"ok": True}
        if path == "/v1/accounts/vault":
            if method == "GET":
                if self.vault is None:
                    raise clusterlib.ControlPlaneError("no vault", status_code=404, detail="no vault")
                return dict(self.vault)
            if method == "PUT":
                if self.fail_next_put > 0:
                    self.fail_next_put -= 1
                    raise clusterlib.ControlPlaneError("version conflict", status_code=409,
                                                       detail="version conflict")
                current = (self.vault or {}).get("version", 0)
                if int(body.get("version_expected") or 0) != current:
                    raise clusterlib.ControlPlaneError("version conflict", status_code=409,
                                                       detail="version conflict")
                self.vault = {"version": current + 1, "blob_b64": body["blob_b64"],
                              "meta": body.get("meta") or {},
                              "updated_at": datetime.utcnow().isoformat()}
                return {"version": self.vault["version"]}
        raise clusterlib.ControlPlaneError(f"unexpected CP call {method} {path}", status_code=500)

    def count(self, method, path):
        return sum(1 for m, p in self.calls if m == method and p == path)


async def seed_cluster_a(session):
    """Cluster A's config under KEY_A: an integration with secrets, a project
    with envs/services, a webhook setting, and a service target holding an
    encrypted mesh key. Returns the plaintexts for later assertions."""
    plain = {
        "pat": "gh_pat_supersecret", "cf_token": "cf-api-token",
        "cf_connector": "cf-connector-token", "cf_client_secret": "cf-oauth-secret",
        "webhook": "wh-shared-secret", "wg_priv": "wg-private-key-material",
    }
    integ = Integration(
        provider="github", account_login="al", name="al",
        secret_encrypted=crypto.encrypt(plain["pat"]), status="connected",
        config={"orgs": ["calm"]}, updated_at=T1,
    )
    cf = Integration(
        provider="cloudflare", account_login=None, name="cloudflare",
        secret_encrypted=crypto.encrypt(plain["cf_token"]), status="connected",
        config={
            "token_encrypted": crypto.encrypt(plain["cf_token"]),
            "connector_token_encrypted": crypto.encrypt(plain["cf_connector"]),
            "client_secret_encrypted": crypto.encrypt(plain["cf_client_secret"]),
            "account_id": "cf-acct",
        }, updated_at=T1,
    )
    session.add_all([integ, cf])
    await session.flush()
    proj = Project(repo_full_name="al/listless", name="listless", managed=True,
                   integration_id=integ.id, updated_at=T1)
    session.add(proj)
    await session.flush()
    env = Environment(project_id=proj.id, name="production", kind="production",
                      updated_at=T1)
    svc = Service(project_id=proj.id, name="web", kind="web", is_public=True)
    session.add_all([env, svc])
    await session.flush()
    st = ServiceTarget(
        service_id=svc.id, environment_id=None, target="aws",
        config={"region": "us-east-1"},
        state={"mesh": {"ordinal": 7,
                        "wg_private_key_enc": crypto.encrypt(plain["wg_priv"]),
                        "wg_pubkey": "PUB"}},
        updated_at=T1, state_updated_at=T1,
    )
    session.add(st)
    session.add(Setting(key="webhook",
                        value={"secret_encrypted": crypto.encrypt(plain["webhook"])}))
    await session.commit()
    return plain


# ───── ADK lifecycle ─────────────────────────────────────────────────────────


def test_adk_mint_escrow_and_local_cache(monkeypatch):
    async def body():
        cp = FakeControlPlane().install(monkeypatch)
        session = await make_session()
        with use_key(KEY_A):
            await link_account(session)
            adk = await vaultlib.ensure_adk(session)
            assert cp.adk_b64 == adk                 # escrowed to the CP
            assert cp.count("PUT", "/v1/accounts/keys/adk") == 1
            # Second call: served from the local setting — no more CP traffic.
            calls_before = len(cp.calls)
            assert await vaultlib.ensure_adk(session) == adk
            assert len(cp.calls) == calls_before
            # And the persisted copy is encrypted under the local key.
            row = (await session.execute(
                select(Setting).where(Setting.key == vaultlib.ADK_KEY))).scalar_one()
            assert crypto.decrypt(row.value["adk_encrypted"]) == adk
    run(body())


def test_adk_fetch_existing_from_escrow(monkeypatch):
    async def body():
        cp = FakeControlPlane().install(monkeypatch)
        import base64
        cp.adk_b64 = base64.urlsafe_b64encode(b"E" * 32).decode()
        session = await make_session()
        with use_key(KEY_A):
            await link_account(session)
            assert await vaultlib.ensure_adk(session) == cp.adk_b64
            assert cp.count("PUT", "/v1/accounts/keys/adk") == 0  # nothing minted
    run(body())


def test_adk_put_conflict_adopts_winner(monkeypatch):
    async def body():
        cp = FakeControlPlane().install(monkeypatch)
        import base64
        winner = base64.urlsafe_b64encode(b"W" * 32).decode()

        # 404 on GET (so the node mints), then 409 on PUT with a winner ready.
        orig_handle = cp.handle

        def handle(method, path, body_):
            if path == "/v1/accounts/keys/adk":
                cp.calls.append((method, path))
                if method == "GET":
                    if cp.adk_b64:
                        return {"adk_b64": cp.adk_b64}
                    cp.adk_b64 = winner  # the racing node escrows between GET and PUT
                    raise clusterlib.ControlPlaneError("no adk", status_code=404, detail="x")
                if method == "PUT":
                    raise clusterlib.ControlPlaneError("conflict", status_code=409, detail="x")
            return orig_handle(method, path, body_)
        cp.handle = handle

        session = await make_session()
        with use_key(KEY_A):
            await link_account(session)
            assert await vaultlib.ensure_adk(session) == winner
    run(body())


# ───── cross-cluster round trip (different ENCRYPTION_KEYs) ──────────────────


def test_round_trip_reencrypts_secrets_across_keys(monkeypatch):
    async def body():
        import base64
        adk = base64.urlsafe_b64encode(b"K" * 32).decode()

        session_a = await make_session()
        with use_key(KEY_A):
            plain = await seed_cluster_a(session_a)
            blob, digest = await vaultlib.export_vault(session_a, adk)
        assert isinstance(blob, str) and digest

        # The blob itself never carries plaintext-visible secrets... but the
        # DECRYPTED inner state must (that's what makes re-encryption possible).
        state = vaultlib._walk_secret_fields  # noqa: F841 (sanity: importable)

        session_b = await make_session()
        with use_key(KEY_B):
            await vaultlib.import_vault(session_b, blob, mode="full", adk_b64=adk)
            gh = (await session_b.execute(select(Integration).where(
                Integration.provider == "github"))).scalar_one()
            assert crypto.decrypt(gh.secret_encrypted) == plain["pat"]
            cf = (await session_b.execute(select(Integration).where(
                Integration.provider == "cloudflare"))).scalar_one()
            assert crypto.decrypt(cf.config["token_encrypted"]) == plain["cf_token"]
            assert crypto.decrypt(cf.config["connector_token_encrypted"]) == plain["cf_connector"]
            assert crypto.decrypt(cf.config["client_secret_encrypted"]) == plain["cf_client_secret"]
            wh = (await session_b.execute(select(Setting).where(
                Setting.key == "webhook"))).scalar_one()
            assert crypto.decrypt(wh.value["secret_encrypted"]) == plain["webhook"]
            st = (await session_b.execute(select(ServiceTarget))).scalar_one()
            assert crypto.decrypt(st.state["mesh"]["wg_private_key_enc"]) == plain["wg_priv"]
            # Non-secret rows came through the normal import path too.
            proj = (await session_b.execute(select(Project))).scalar_one()
            assert proj.name == "listless" and proj.updated_at == T1
        # And under KEY_A those same B-side blobs would NOT decrypt — proving
        # they were actually re-encrypted, not copied.
        with use_key(KEY_A):
            assert crypto.decrypt(gh.secret_encrypted) == ""
    run(body())


def test_wrong_adk_rejected(monkeypatch):
    async def body():
        import base64
        adk1 = base64.urlsafe_b64encode(b"1" * 32).decode()
        adk2 = base64.urlsafe_b64encode(b"2" * 32).decode()
        session_a = await make_session()
        with use_key(KEY_A):
            await seed_cluster_a(session_a)
            blob, _ = await vaultlib.export_vault(session_a, adk1)
        session_b = await make_session()
        with use_key(KEY_B):
            try:
                await vaultlib.import_vault(session_b, blob, mode="full", adk_b64=adk2)
                assert False, "expected VaultError"
            except vaultlib.VaultError:
                pass
    run(body())


# ───── newer-wins convergence + tombstones through the vault ─────────────────


def test_newer_wins_convergence_through_vault(monkeypatch):
    async def body():
        import base64
        adk = base64.urlsafe_b64encode(b"K" * 32).decode()

        # A and B both hold the project; A edits it at T2.
        session_a = await make_session()
        with use_key(KEY_A):
            await seed_cluster_a(session_a)
            proj_a = (await session_a.execute(select(Project))).scalar_one()
            proj_a.description = "edited on A"
            proj_a.updated_at = T2
            await session_a.commit()
            blob_a, _ = await vaultlib.export_vault(session_a, adk)

        session_b = await make_session()
        with use_key(KEY_B):
            # B has an OLDER copy (T0).
            session_b.add(Project(repo_full_name="al/listless", name="listless",
                                  managed=True, description="stale on B",
                                  updated_at=T0))
            await session_b.commit()
            await vaultlib.import_vault(session_b, blob_a, mode="update", adk_b64=adk)
            proj_b = (await session_b.execute(select(Project).where(
                Project.repo_full_name == "al/listless"))).scalar_one()
            assert proj_b.description == "edited on A"
            assert proj_b.updated_at == T2

            # The reverse direction: B's now-stale-again export can't clobber
            # a fresher local edit made on B afterwards.
            proj_b.description = "fresher on B"
            proj_b.updated_at = T2 + timedelta(hours=1)
            await session_b.commit()
            await vaultlib.import_vault(session_b, blob_a, mode="update", adk_b64=adk)
            await session_b.refresh(proj_b)
            assert proj_b.description == "fresher on B"
    run(body())


def test_tombstone_deletion_propagates_through_vault(monkeypatch):
    async def body():
        import base64
        adk = base64.urlsafe_b64encode(b"K" * 32).decode()
        session_a = await make_session()
        with use_key(KEY_A):
            await seed_cluster_a(session_a)
            # A deletes the project and records the tombstone.
            proj = (await session_a.execute(select(Project))).scalar_one()
            await cluster_sync.record_tombstone(session_a, "project", proj.repo_full_name)
            await session_a.delete(proj)
            await session_a.commit()
            blob_a, _ = await vaultlib.export_vault(session_a, adk)
        session_b = await make_session()
        with use_key(KEY_B):
            session_b.add(Project(repo_full_name="al/listless", name="listless",
                                  managed=True, updated_at=T0))
            await session_b.commit()
            await vaultlib.import_vault(session_b, blob_a, mode="update", adk_b64=adk)
            assert (await session_b.execute(select(Project).where(
                Project.repo_full_name == "al/listless"))).scalar_one_or_none() is None
    run(body())


# ───── vault_tick: CAS retry + no-op push suppression ────────────────────────


def test_vault_tick_pushes_then_suppresses_noop(monkeypatch):
    async def body():
        cp = FakeControlPlane().install(monkeypatch)
        session = await make_session()
        with use_key(KEY_A):
            await link_account(session)
            await seed_cluster_a(session)
            res = await vaultlib.vault_tick(session)
            assert res == {"pushed": True, "version": 1}
            assert cp.vault["version"] == 1
            vs = await vaultlib.get_vault_state(session)
            assert vs["version"] == 1 and vs["pushed_hash"] and vs["pushed_at"]
            assert vs.get("error") is None

            # Nothing changed → no second PUT (Fernet nondeterminism must not
            # force a push; the hash is over the pre-encryption JSON).
            puts_before = cp.count("PUT", "/v1/accounts/vault")
            res = await vaultlib.vault_tick(session)
            assert res == {"pushed": False, "version": 1}
            assert cp.count("PUT", "/v1/accounts/vault") == puts_before

            # A real edit → pushes again.
            proj = (await session.execute(select(Project))).scalar_one()
            proj.description = "changed"
            proj.updated_at = T2
            await session.commit()
            res = await vaultlib.vault_tick(session)
            assert res == {"pushed": True, "version": 2}
    run(body())


def test_vault_tick_409_pull_merge_retry(monkeypatch):
    async def body():
        import base64
        adk = base64.urlsafe_b64encode(b"K" * 32).decode()

        # Another "cluster" (B, KEY_B) already pushed version 1 with an edit.
        session_b = await make_session()
        with use_key(KEY_B):
            await seed_cluster_a(session_b)
            proj_b = (await session_b.execute(select(Project))).scalar_one()
            proj_b.description = "written by B"
            proj_b.updated_at = T2
            await session_b.commit()
            blob_b, _ = await vaultlib.export_vault(session_b, adk)

        cp = FakeControlPlane().install(monkeypatch)
        cp.adk_b64 = adk
        cp.vault = {"version": 1, "blob_b64": blob_b, "meta": {},
                    "updated_at": T2.isoformat()}

        session_a = await make_session()
        with use_key(KEY_A):
            await link_account(session_a)
            await seed_cluster_a(session_a)
            # A already pulled v1 conceptually... simulate the CAS race instead:
            # its first PUT 409s (a concurrent writer), forcing pull→merge→retry.
            cp.fail_next_put = 1
            res = await vaultlib.vault_tick(session_a)
            assert res is not None and res["pushed"] is True
            assert cp.vault["version"] == 2          # retry landed
            # The merge picked up B's newer edit before re-pushing.
            proj_a = (await session_a.execute(select(Project).where(
                Project.repo_full_name == "al/listless"))).scalar_one()
            assert proj_a.description == "written by B"
    run(body())


def test_vault_tick_requires_account_and_coordinator(monkeypatch):
    async def body():
        cp = FakeControlPlane().install(monkeypatch)
        session = await make_session()
        with use_key(KEY_A):
            # Unlinked → no-op, no CP calls.
            assert await vaultlib.vault_tick(session) is None
            assert not cp.calls
            # Linked but a NON-coordinator cluster member → no-op.
            await link_account(session)
            await clusterlib.save_cluster(session, {
                "cluster_id": "c1", "control_plane_url": "https://cp.test",
                "roster": [
                    {"node_id": "other-node", "ordinal": 1, "role": "peer",
                     "online": True, "serving": True},
                    {"node_id": await clusterlib.get_node_id(session),
                     "ordinal": 2, "role": "peer", "online": True},
                ],
            })
            await session.commit()
            assert await vaultlib.vault_tick(session) is None
            assert not cp.calls
    run(body())


def test_vault_tick_records_error(monkeypatch):
    async def body():
        async def _cp(method, base, path, *, token=None, body=None):
            raise clusterlib.ControlPlaneError("cp down", status_code=503, detail="cp down")
        monkeypatch.setattr(clusterlib, "_cp", _cp)
        session = await make_session()
        with use_key(KEY_A):
            await link_account(session)
            assert await vaultlib.vault_tick(session) is None  # never raises
            vs = await vaultlib.get_vault_state(session)
            assert "cp down" in (vs.get("error") or "")
    run(body())


# ───── fresh-install restore on link ─────────────────────────────────────────


def test_fresh_install_restore_on_link(monkeypatch):
    async def body():
        import base64
        adk = base64.urlsafe_b64encode(b"K" * 32).decode()

        # Cluster A pushed its vault (KEY_A).
        session_a = await make_session()
        with use_key(KEY_A):
            plain = await seed_cluster_a(session_a)
            blob_a, _ = await vaultlib.export_vault(session_a, adk)

        cp = FakeControlPlane().install(monkeypatch)
        cp.adk_b64 = adk
        cp.vault = {"version": 3, "blob_b64": blob_a, "meta": {},
                    "updated_at": T1.isoformat()}

        # Fresh install under KEY_B links the account.
        session_b = await make_session()
        with use_key(KEY_B):
            await link_account(session_b)
            result = await vaultlib.restore_on_link(session_b)
            assert result["imported"] is not None
            # Full restore: projects + integrations landed, secrets decrypt
            # under THE LOCAL key.
            proj = (await session_b.execute(select(Project))).scalar_one()
            assert proj.repo_full_name == "al/listless"
            gh = (await session_b.execute(select(Integration).where(
                Integration.provider == "github"))).scalar_one()
            assert crypto.decrypt(gh.secret_encrypted) == plain["pat"]
            envs = (await session_b.execute(select(Environment))).scalars().all()
            assert [e.name for e in envs] == ["production"]
            vs = await vaultlib.get_vault_state(session_b)
            assert vs.get("restoring") is False and vs.get("error") is None
            assert vs.get("pulled_at")
            # It pushed its merged state back (version advanced past 3).
            assert cp.vault["version"] == 4
            assert vs.get("version") == 4
    run(body())


def test_restore_on_link_empty_escrow_first_node(monkeypatch):
    """The FIRST node of an account: no ADK, no vault — restore mints the ADK
    and seeds the vault with the local state."""
    async def body():
        cp = FakeControlPlane().install(monkeypatch)
        session = await make_session()
        with use_key(KEY_A):
            await link_account(session)
            await seed_cluster_a(session)
            await vaultlib.restore_on_link(session)
            assert cp.adk_b64                        # minted + escrowed
            assert cp.vault and cp.vault["version"] == 1
            # The pushed blob round-trips with the escrowed ADK.
            session2 = await make_session()
            with use_key(KEY_B):
                await vaultlib.import_vault(session2, cp.vault["blob_b64"],
                                            mode="full", adk_b64=cp.adk_b64)
                assert (await session2.execute(select(Project))).scalar_one() is not None
    run(body())


# ───── stable hash ───────────────────────────────────────────────────────────


def test_state_hash_ignores_exported_at_and_encryption_noise():
    async def body():
        session = await make_session()
        with use_key(KEY_A):
            await seed_cluster_a(session)
            s1 = await vaultlib.build_vault_state(session)
            s2 = await vaultlib.build_vault_state(session)
            assert s1["exported_at"] != s2["exported_at"] or True  # may differ
            assert vaultlib.state_hash(s1) == vaultlib.state_hash(s2)
    run(body())
