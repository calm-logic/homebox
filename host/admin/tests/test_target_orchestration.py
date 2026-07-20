"""Orchestrator wiring tests for cloud deployment targets (deploy.py side).

Pins the main-session wiring around the provider modules:
  1. _provision_db_vms — the pre-_assemble_stack DB-VM step: mesh identity
     allocated once and persisted BEFORE provisioning, the exact ctx.config
     contract the EC2/GCE providers consume, clustered-only guard,
     coordinator gating.
  2. _teardown_retargeted — retargeting BACK to homebox destroys the previous
     cloud resources with a FLATTENED state view (providers read their
     resource ids at top level; the orchestrator persists them nested under
     state.resource_ids) and drops the per-host CNAME.
  3. _upsert_target_dns — verification-record handling (TXT, and records
     without a CNAME: Cloud Run site verification).
  4. _assemble_stack — DB-VM-targeted services STAY in the local compose
     (additive replica) while consumers' env URLs are rewritten.
  5. targetslib.rewrite_cross_target_env — serverless consumer → DB VM uses
     the public endpoint + sslmode, homebox consumer uses the mesh IP.
  6. targetslib.reconcile_targets — pending Cloud Run domain mappings retry.

Runs on in-memory sqlite; providers/cloudflare faked at module boundaries.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.db import Base  # noqa: E402
from app import crypto, dissect, targetslib  # noqa: E402
from app.models import (  # noqa: E402
    Deployment, Environment, Integration, Project, Service, ServiceTarget,
)
from app.targets.base import TargetError, TargetResult  # noqa: E402

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


def _detected(name, kind="web", origin="build", public=True, port=80,
              auto_env=None, image=None):
    d = dissect.DetectedService(
        name=name, kind=kind, origin=origin, is_public=public,
        internal_port=port, build_type=None if origin == "compose" else "dockerfile",
        build_dir=".", image=image,
    )
    if auto_env:
        d.auto_env.update(auto_env)
    return d


async def _seed_db_vm(session, *, target="aws", config=None, state=None):
    """project + env + postgres service targeted at a cloud DB VM."""
    p = Project(repo_full_name="al/shop", name="shop", managed=True)
    session.add(p)
    await session.flush()
    env = Environment(project_id=p.id, name="dev", kind="dev", slug_suffix="--dev")
    db = Service(project_id=p.id, name="postgres", kind="database", is_public=False)
    session.add_all([env, db])
    await session.flush()
    integ = Integration(provider="aws", account_id="123456789012",
                        secret_encrypted=crypto.encrypt("AKIAX:secretkey"))
    session.add(integ)
    await session.flush()
    st = ServiceTarget(service_id=db.id, target=target, integration_id=integ.id,
                       config=config or {"ami": "ami-0abc"}, state=state or {})
    session.add(st)
    await session.commit()
    return p, env, db, st


def _write_compose(rd: Path):
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "docker-compose.yml").write_text(
        "services:\n"
        "  postgres:\n"
        "    image: postgres:16-alpine\n"
        "    environment:\n"
        "      POSTGRES_USER: shop\n"
        "      POSTGRES_PASSWORD: pw123\n"
        "      POSTGRES_DB: shopdb\n"
    )


CLUSTER_STATE = {
    "roster": [
        {"node_id": "node-a", "ordinal": 1, "wg_pubkey": "PUBA", "role": "peer"},
        {"node_id": "node-b", "ordinal": 2, "wg_pubkey": "PUBB", "role": "peer"},
    ],
    "initial_sync_done": True,
}
CLUSTER_CTX = {"state": CLUSTER_STATE, "node_id": "node-a", "secret": "s3cret"}


class FakeProvider:
    """Records the (config, state) it was built with and its deploy/destroy
    calls; deploy echoes the mesh identity like the real EC2/GCE targets."""

    instances: list["FakeProvider"] = []

    def __init__(self, *, creds, config, state, fail_destroy=False):
        self.creds, self.config, self.state = creds, config, state
        self.deploys: list = []
        self.destroys: list = []
        self.fail_destroy = fail_destroy
        FakeProvider.instances.append(self)

    async def validate(self):
        pass

    async def deploy(self, ctx):
        self.deploys.append(ctx)
        ordinal = self.config["mesh_ordinal"]
        return TargetResult(
            endpoint="9.9.9.9", cname_target=None, proxied=False,
            state={
                "instance_id": "i-0abc", "sg_id": "sg-1", "public_ip": "9.9.9.9",
                "mesh": {"ordinal": ordinal, "ip": self.config["mesh_ip"],
                         "wg_pubkey": self.config["wg_public_key"],
                         "endpoint": "9.9.9.9:51820"},
                "db": {"port": 5432, "node_name": f"n{ordinal}"},
            },
        )

    async def destroy(self, state):
        self.destroys.append(state)
        if self.fail_destroy:
            raise TargetError("simulated destroy failure")

    async def probe(self, state):
        return True


def _patch_provider(monkeypatch, **provider_kwargs):
    FakeProvider.instances = []
    from app import targets as targets_pkg

    def fake_get_provider(target, kind, *, creds, config, state):
        return FakeProvider(creds=creds, config=config, state=state,
                            **provider_kwargs)
    monkeypatch.setattr(targets_pkg, "get_provider", fake_get_provider)


# ── 1. _provision_db_vms ─────────────────────────────────────────────────────

def test_provision_db_vm_contract_and_state(tmp_path, monkeypatch):
    async def body():
        session = await make_session()
        p, env, db, st = await _seed_db_vm(session)
        _write_compose(tmp_path)
        _patch_provider(monkeypatch)
        from app import meshlib
        mesh_calls = []

        async def fake_ensure_mesh(s, state):
            mesh_calls.append(state)
        monkeypatch.setattr(meshlib, "ensure_mesh", fake_ensure_mesh)

        from app.deploy import _provision_db_vms
        detected = [_detected("postgres", kind="database", origin="compose",
                              public=False, image="postgres:16-alpine")]
        targets_map = await targetslib.effective_targets(session, p, env)
        tail = await _provision_db_vms(
            session, p, env, tmp_path, detected, targets_map,
            CLUSTER_STATE, CLUSTER_CTX, True,
        )

        assert "live at 9.9.9.9" in tail
        prov = FakeProvider.instances[0]
        cfg = prov.config
        # The exact ctx.config contract the EC2/GCE providers consume.
        assert cfg["ami"] == "ami-0abc"                      # user config passthrough
        assert cfg["mesh_ordinal"] == 0xF000                 # first reserved ordinal
        assert cfg["mesh_ip"] == "10.77.240.0"
        assert cfg["wg_private_key"] and cfg["wg_public_key"]
        assert cfg["wg_peers"] == [
            {"public_key": "PUBA", "allowed_ips": "10.77.0.1/32"},
            {"public_key": "PUBB", "allowed_ips": "10.77.0.2/32"},
        ]
        assert cfg["open_pg_public"] is False                # no serverless sibling
        assert "16" in cfg["pg_image"]
        assert cfg["db"] == {
            "db_name": "shopdb", "admin_user": "shop", "admin_password": "pw123",
            "repl_user": "pgedge",
            "repl_password": cfg["db"]["repl_password"],     # derived, non-empty
        }
        assert cfg["db"]["repl_password"]

        await session.refresh(st)
        state = st.state
        assert state["status"] == "live"
        assert state["endpoint"] == "9.9.9.9"
        assert state["mesh"]["ordinal"] == 0xF000
        assert state["mesh"]["endpoint"] == "9.9.9.9:51820"
        assert state["mesh"]["wg_private_key_enc"]           # identity persisted
        assert crypto.decrypt(state["mesh"]["wg_private_key_enc"]) == cfg["wg_private_key"]
        assert state["db"]["node_name"] == "n61440"
        assert state["resource_ids"]["instance_id"] == "i-0abc"
        assert "mesh" not in state["resource_ids"]           # split out, not doubled
        assert mesh_calls                                    # local wg reconciled
        # …and the row now feeds the mesh/replication registries.
        peers = await targetslib.mesh_extra_peers(session)
        assert peers == [{"ordinal": 0xF000, "wg_pubkey": cfg["wg_public_key"],
                          "endpoint": "9.9.9.9:51820", "ip": "10.77.240.0"}]
        extra = await targetslib.db_vm_extra_nodes(session, p, env, "postgres")
        assert extra == [{"ordinal": 0xF000, "host": "10.77.240.0",
                          "port": 5432, "node_name": "n61440"}]
    run(body())


def test_provision_db_vm_requires_cluster(tmp_path, monkeypatch):
    async def body():
        session = await make_session()
        p, env, db, st = await _seed_db_vm(session)
        _write_compose(tmp_path)
        _patch_provider(monkeypatch)
        from app.deploy import _provision_db_vms
        detected = [_detected("postgres", kind="database", origin="compose", public=False)]
        targets_map = await targetslib.effective_targets(session, p, env)
        tail = await _provision_db_vms(
            session, p, env, tmp_path, detected, targets_map, None, None, True)
        assert "FAILED" in tail and "clustered" in tail
        await session.refresh(st)
        assert st.state["status"] == "error"
        assert "clustered" in st.state["error"]
        assert not FakeProvider.instances                    # never reached a provider
    run(body())


def test_provision_db_vm_reuses_persisted_identity(tmp_path, monkeypatch):
    async def body():
        session = await make_session()
        priv, pub = crypto.generate_wg_keypair()
        p, env, db, st = await _seed_db_vm(session, state={
            "mesh": {"ordinal": 0xF007, "wg_pubkey": pub,
                     "wg_private_key_enc": crypto.encrypt(priv)},
        })
        _write_compose(tmp_path)
        _patch_provider(monkeypatch)
        from app import meshlib

        async def fake_ensure_mesh(s, state):
            pass
        monkeypatch.setattr(meshlib, "ensure_mesh", fake_ensure_mesh)
        from app.deploy import _provision_db_vms
        detected = [_detected("postgres", kind="database", origin="compose", public=False)]
        targets_map = await targetslib.effective_targets(session, p, env)
        await _provision_db_vms(
            session, p, env, tmp_path, detected, targets_map,
            CLUSTER_STATE, CLUSTER_CTX, True)
        cfg = FakeProvider.instances[0].config
        assert cfg["mesh_ordinal"] == 0xF007                 # reused, not reallocated
        assert cfg["wg_private_key"] == priv
        assert cfg["wg_public_key"] == pub
        assert cfg["mesh_ip"] == "10.77.240.7"
    run(body())


def test_provision_db_vm_noncoordinator_noop(tmp_path, monkeypatch):
    async def body():
        session = await make_session()
        p, env, db, st = await _seed_db_vm(session)
        _write_compose(tmp_path)
        _patch_provider(monkeypatch)
        from app.deploy import _provision_db_vms
        detected = [_detected("postgres", kind="database", origin="compose", public=False)]
        targets_map = await targetslib.effective_targets(session, p, env)
        tail = await _provision_db_vms(
            session, p, env, tmp_path, detected, targets_map,
            CLUSTER_STATE, CLUSTER_CTX, False)
        assert tail == ""
        await session.refresh(st)
        assert st.state == {}                                # untouched — sync delivers it
        assert not FakeProvider.instances
    run(body())


def test_provision_db_vm_opens_pg_for_serverless_sibling(tmp_path, monkeypatch):
    async def body():
        session = await make_session()
        p, env, db, st = await _seed_db_vm(session)
        api = Service(project_id=p.id, name="api", kind="api", is_public=True)
        session.add(api)
        await session.flush()
        session.add(ServiceTarget(service_id=api.id, target="gcp"))
        await session.commit()
        _write_compose(tmp_path)
        _patch_provider(monkeypatch)
        from app import meshlib

        async def fake_ensure_mesh(s, state):
            pass
        monkeypatch.setattr(meshlib, "ensure_mesh", fake_ensure_mesh)
        from app.deploy import _provision_db_vms
        detected = [_detected("postgres", kind="database", origin="compose", public=False)]
        targets_map = await targetslib.effective_targets(session, p, env)
        await _provision_db_vms(
            session, p, env, tmp_path, detected, targets_map,
            CLUSTER_STATE, CLUSTER_CTX, True)
        assert FakeProvider.instances[0].config["open_pg_public"] is True
    run(body())


# ── 2. _teardown_retargeted ──────────────────────────────────────────────────

async def _seed_retargeted(session, *, prev_target="aws"):
    p = Project(repo_full_name="al/site", name="site", managed=True)
    session.add(p)
    await session.flush()
    env = Environment(project_id=p.id, name="dev", kind="dev", slug_suffix="--dev")
    svc = Service(project_id=p.id, name="app", kind="static", is_public=True)
    session.add_all([env, svc])
    await session.flush()
    session.add(Integration(provider=prev_target, account_id="acct",
                            secret_encrypted=crypto.encrypt(
                                "AKIAX:sk" if prev_target == "aws" else "tok")))
    st = ServiceTarget(service_id=svc.id, target="homebox", state={
        "status": "live", "endpoint": "old.example",
        "resource_ids": {"bucket": "site--dev.example.com", "region": "us-east-1"},
        "dns": {"hostname": "site--dev.example.com", "zone_id": "z1"},
        "previous": {"target": prev_target, "state": {
            "status": "live",
            "resource_ids": {"bucket": "site--dev.example.com", "region": "us-east-1"},
            "dns": {"hostname": "site--dev.example.com", "zone_id": "z1"},
        }},
    })
    session.add(st)
    await session.commit()
    return p, env, svc, st


def test_teardown_retargeted_destroys_flat_state(monkeypatch):
    async def body():
        session = await make_session()
        p, env, svc, st = await _seed_retargeted(session)
        _patch_provider(monkeypatch)
        from app import deploy
        deleted_dns = []

        async def fake_delete_dns(s, dns):
            deleted_dns.append(dns)
        monkeypatch.setattr(deploy, "_delete_target_dns", fake_delete_dns)

        tail = await deploy._teardown_retargeted(session, p, env)
        assert "aws resources destroyed" in tail
        prov = FakeProvider.instances[0]
        # destroy() sees the provider's own keys FLAT (resource_ids merged up).
        assert prov.destroys[0]["bucket"] == "site--dev.example.com"
        assert prov.destroys[0]["region"] == "us-east-1"
        assert deleted_dns[0] == {"hostname": "site--dev.example.com", "zone_id": "z1"}
        await session.refresh(st)
        assert st.state == {"status": "local"}
    run(body())


def test_teardown_failure_keeps_previous_for_retry(monkeypatch):
    async def body():
        session = await make_session()
        p, env, svc, st = await _seed_retargeted(session)
        _patch_provider(monkeypatch, fail_destroy=True)
        from app import deploy
        tail = await deploy._teardown_retargeted(session, p, env)
        assert "WARNING" in tail
        await session.refresh(st)
        assert st.state.get("previous")                      # retried next deploy
    run(body())


def test_teardown_skips_rows_without_previous(monkeypatch):
    async def body():
        session = await make_session()
        p, env, svc, st = await _seed_retargeted(session)
        state = dict(st.state)
        state.pop("previous")
        st.state = state
        await session.commit()
        _patch_provider(monkeypatch)
        from app import deploy
        tail = await deploy._teardown_retargeted(session, p, env)
        assert tail == ""
        assert not FakeProvider.instances
    run(body())


# ── 3. _upsert_target_dns verification records ───────────────────────────────

class FakeCfDns:
    def __init__(self):
        self.cnames: list[tuple] = []
        self.txts: list[tuple] = []

    def get_token(self, state):
        return "tok"

    async def load_state(self, session):
        return {"account_id": "acc"}

    async def list_zones(self, token, account_id=None):
        return [{"id": "z1", "name": "example.com"}]

    def resolve_zone_for(self, zones, hostname):
        return zones[0]

    async def upsert_cname(self, token, zone_id, name, target, *, proxied=True):
        self.cnames.append((name, target, proxied))
        return {}

    async def upsert_txt(self, token, zone_id, name, content):
        self.txts.append((name, content))
        return {}


def _patch_cf_dns(monkeypatch):
    fake = FakeCfDns()
    from app import cloudflare as cf
    for attr in ("get_token", "load_state", "list_zones", "resolve_zone_for",
                 "upsert_cname", "upsert_txt"):
        monkeypatch.setattr(cf, attr, getattr(fake, attr))
    return fake


def test_upsert_dns_txt_without_cname(monkeypatch):
    async def body():
        session = await make_session()
        fake = _patch_cf_dns(monkeypatch)
        from app.deploy import _upsert_target_dns
        result = TargetResult(
            endpoint="x.run.app", cname_target=None, proxied=True,
            state={"extra_dns_records": [
                {"type": "TXT", "name": "api.example.com", "value": "google-site-verification=abc"},
            ]},
        )
        dns = await _upsert_target_dns(session, "api.example.com", result)
        assert dns is None                                   # not cloud-routed yet
        assert fake.txts == [("api.example.com", "google-site-verification=abc")]
        assert fake.cnames == []
    run(body())


def test_upsert_dns_cname_plus_validation_records(monkeypatch):
    async def body():
        session = await make_session()
        fake = _patch_cf_dns(monkeypatch)
        from app.deploy import _upsert_target_dns
        result = TargetResult(
            endpoint="x.run.app", cname_target="ghs.googlehosted.com", proxied=False,
            state={"extra_dns_records": [
                {"type": "CNAME", "name": "_acme.api.example.com", "value": "v.example."},
            ]},
        )
        dns = await _upsert_target_dns(session, "api.example.com", result)
        assert dns == {"hostname": "api.example.com",
                       "cname_target": "ghs.googlehosted.com",
                       "proxied": False, "zone_id": "z1"}
        assert fake.cnames == [("api.example.com", "ghs.googlehosted.com", False),
                               ("_acme.api.example.com", "v.example.", False)]
    run(body())


def test_upsert_dns_nothing_to_do(monkeypatch):
    async def body():
        session = await make_session()
        fake = _patch_cf_dns(monkeypatch)
        from app.deploy import _upsert_target_dns
        result = TargetResult(endpoint="x.run.app", cname_target=None, state={})
        assert await _upsert_target_dns(session, "api.example.com", result) is None
        assert fake.cnames == [] and fake.txts == []
    run(body())


# ── 4. _assemble_stack keeps DB-VM services local ────────────────────────────

def test_db_vm_service_stays_in_compose_with_rewritten_consumers(tmp_path, monkeypatch):
    async def body():
        session = await make_session()
        p, env, db, st = await _seed_db_vm(session, state={
            "status": "live", "endpoint": "9.9.9.9",
            "mesh": {"ordinal": 0xF000, "ip": "10.77.240.0",
                     "wg_pubkey": "P", "endpoint": "9.9.9.9:51820"},
        })
        web = Service(project_id=p.id, name="web", kind="web", is_public=True)
        session.add(web)
        await session.commit()
        _write_compose(tmp_path)
        (tmp_path / "Dockerfile").write_text("FROM scratch")

        from app import cluster_db

        async def fake_residual(svc, name, stack, top_volumes):
            return None
        monkeypatch.setattr(cluster_db, "residual_transform", fake_residual)

        targets_map = await targetslib.effective_targets(session, p, env)
        assert targets_map["postgres"].variant == "ec2_db"

        from app.deploy import _assemble_stack
        detected = [
            _detected("postgres", kind="database", origin="compose", public=False,
                      image="postgres:16-alpine"),
            _detected("web", kind="web", public=True, port=3000, auto_env={
                "DATABASE_URL": "postgres://shop:pw123@postgres:5432/shopdb",
            }),
        ]
        compose_path, plan = await _assemble_stack(
            tmp_path, p, env, "example.com", detected, {},
            None, base=False, targets_map=targets_map,
        )
        import yaml
        data = yaml.safe_load(compose_path.read_text())
        assert "postgres" in data["services"]                # local replica kept
        assert "web" in data["services"]
        assert plan["postgres"].get("cloud") is None         # pre-step owns the VM
        assert plan["postgres"]["db_vm"] is True
        assert plan["postgres"]["target"] == "aws"
        env_block = data["services"]["web"]["environment"]
        env_map = dict(e.split("=", 1) for e in env_block) \
            if isinstance(env_block, list) else env_block
        assert env_map["DATABASE_URL"] == \
            "postgres://shop:pw123@10.77.240.0:5432/shopdb"  # homebox app → mesh IP
    run(body())


# ── 5. env rewrite matrix: serverless consumer → DB VM ───────────────────────

def test_rewrite_serverless_consumer_uses_public_endpoint_with_ssl():
    vm = targetslib.ResolvedTarget(
        target="aws", variant="ec2_db",
        state={"endpoint": "9.9.9.9",
               "mesh": {"ip": "10.77.240.0"}},
    )
    auto = {"DATABASE_URL": "postgres://u:p@postgres:5432/db"}
    serverless = targetslib.ResolvedTarget(target="gcp", variant="cloud_run")
    out = targetslib.rewrite_cross_target_env(auto, serverless, {"postgres": vm})
    assert out["DATABASE_URL"] == "postgres://u:p@9.9.9.9:5432/db?sslmode=require"

    homebox_app = targetslib.ResolvedTarget()                # local consumer → mesh
    out = targetslib.rewrite_cross_target_env(auto, homebox_app, {"postgres": vm})
    assert out["DATABASE_URL"] == "postgres://u:p@10.77.240.0:5432/db"


def test_rewrite_serverless_ssl_not_doubled():
    vm = targetslib.ResolvedTarget(target="aws", variant="ec2_db",
                                   state={"endpoint": "9.9.9.9"})
    auto = {"DATABASE_URL": "postgres://u:p@postgres:5432/db?sslmode=disable"}
    serverless = targetslib.ResolvedTarget(target="gcp", variant="cloud_run")
    out = targetslib.rewrite_cross_target_env(auto, serverless, {"postgres": vm})
    assert out["DATABASE_URL"].count("sslmode") == 1         # user's choice respected


# ── 6. reconcile retries pending Cloud Run domain mappings ───────────────────

def test_reconcile_retries_pending_domain_mapping(monkeypatch):
    async def body():
        session = await make_session()
        p = Project(repo_full_name="al/site", name="site", managed=True)
        session.add(p)
        await session.flush()
        env = Environment(project_id=p.id, name="dev", kind="dev", slug_suffix="--dev")
        svc = Service(project_id=p.id, name="api", kind="api", is_public=True)
        session.add_all([env, svc])
        await session.flush()
        st = ServiceTarget(
            service_id=svc.id, target="gcp",
            state={"status": "live", "endpoint": "x.run.app",
                   "resource_ids": {"domain_mapping": "pending_verification"}},
        )
        st.state_updated_at = datetime.utcnow() - timedelta(hours=1)
        session.add(st)
        session.add(Deployment(environment_id=env.id, status="running",
                               stack_name="homebox-proj-site-dev"))
        await session.commit()

        queued = []
        from app import clusterlib

        async def fake_queue(s, e):
            queued.append(e.id)
        monkeypatch.setattr(clusterlib, "_queue_cluster_deploy", fake_queue)

        n = await targetslib.reconcile_targets(session, None)
        assert n == 1 and queued == [env.id]

        # A live target with a READY mapping is left alone.
        st.state = {"status": "live",
                    "resource_ids": {"domain_mapping": "ready"}}
        st.state_updated_at = datetime.utcnow() - timedelta(hours=1)
        await session.commit()
        queued.clear()
        assert await targetslib.reconcile_targets(session, None) == 0
    run(body())
