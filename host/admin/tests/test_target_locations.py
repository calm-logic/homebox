"""Cluster-scoped ("located") homebox deployment targets — linked-accounts
spec D3/D4 (host/docs/linked-accounts.md).

A homebox ServiceTarget row's config may carry exactly one of
{"cluster_id": ...} | {"node_id": ...}; absent = "this homebox" (full legacy
back-compat). Pins:

  1. is_local_homebox / location_is_local truth table (unlinked, cluster
     match, node match, foreign).
  2. _assemble_stack excludes foreign-homebox services from the compose with
     a plan entry {target: "homebox", remote: True, cluster_id|node_id, host}
     — and keeps locally-located + location-less ones.
  3. Retarget homebox@A → homebox@B writes NO cloud-teardown state
     (state.previous) and _teardown_retargeted leaves the row alone — the old
     cluster just drops the service from its stack on its next deploy.
  4. foreign_homebox_hostnames feeds the tunnel/DNS exclusion: the DNS drift
     repair must never repoint a foreign cluster's hostname at OUR tunnel.
  5. PUT /api/services/{id}/target validation: mutual exclusivity (400),
     location without a linked account (412), unknown cluster/node (400),
     valid location accepted with updated_at stamped.
  6. GET /api/services/{id}/targets structured options — linked vs unlinked.
  7. rewrite_cross_target_env resolves a foreign-homebox producer reference
     to its public hostname.
  8. Back-compat: absent location behaves exactly as before.

Runs on in-memory sqlite; routes driven through httpx ASGITransport with auth
and DB dependencies overridden; Cloudflare faked at the module boundary.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.auth import require_session_api  # noqa: E402
from app.db import Base, get_session  # noqa: E402
from app import clusterlib, dissect, targetslib  # noqa: E402
from app.models import (  # noqa: E402
    Domain, Environment, Project, Service, ServiceTarget,
)
from app.routes import services as services_routes  # noqa: E402

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


async def make_session():
    return (await make_sessionmaker())()


async def make_app():
    """Fresh sqlite DB + FastAPI app mounting the services router. Returns
    (client, sessionmaker)."""
    maker = await make_sessionmaker()

    app = FastAPI()
    app.include_router(services_routes.router)

    async def override_session():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[require_session_api] = lambda: "tester"

    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )
    return client, maker


async def set_identity(session, node_id="n-self", cluster_id=None):
    """Pin this install's node id and (optionally) cluster membership."""
    await clusterlib._set_setting(
        session, clusterlib.INSTALL_ID_KEY, {"value": node_id})
    if cluster_id:
        await clusterlib._set_setting(
            session, clusterlib.CLUSTER_KEY, {"cluster_id": cluster_id})
    await session.commit()


async def link_account(session, clusters=(), nodes=()):
    """Simulate a linked account + cached overview (ACCOUNT_OVERVIEW_KEY)."""
    await clusterlib._set_setting(session, clusterlib.ACCOUNT_KEY, {
        "control_plane_url": "https://cp.test",
        "token_encrypted": "x",
        "node_name": "self",
    })
    await clusterlib._set_setting(session, clusterlib.ACCOUNT_OVERVIEW_KEY, {
        "account": {"email": "a@b.c", "plan": "premium"},
        "clusters": list(clusters),
        "nodes": list(nodes),
    })
    await session.commit()


async def seed(session, *, managed=True):
    p = Project(repo_full_name="al/site", name="site", managed=managed)
    session.add(p)
    await session.flush()
    env = Environment(project_id=p.id, name="dev", kind="dev", slug_suffix="--dev")
    web = Service(project_id=p.id, name="app", kind="static", is_public=True)
    api = Service(project_id=p.id, name="api", kind="api", is_public=True,
                  subdomain_label="api")
    session.add_all([env, web, api])
    await session.commit()
    return p, env, web, api


def _detected(name, kind="web", public=True, build_type="static", port=80,
              label="", auto_env=None):
    return dissect.DetectedService(
        name=name, kind=kind, origin="build", is_public=public,
        subdomain_label=label, internal_port=port, build_type=build_type,
        build_dir=".", auto_env=auto_env or {},
        dockerfile="Dockerfile" if build_type == "dockerfile" else None,
    )


# ── 1. locality truth table ───────────────────────────────────────────────────

def test_is_local_homebox_truth_table():
    async def body():
        session = await make_session()
        await set_identity(session, node_id="n-self")  # unlinked, unclustered
        rt = targetslib.ResolvedTarget  # shorthand

        # absent location = "this homebox" — always local (legacy meaning)
        assert await targetslib.is_local_homebox(session, rt())
        # own node id (standalone cluster-of-one) → local
        assert await targetslib.is_local_homebox(
            session, rt(location={"node_id": "n-self"}))
        # someone else's node → foreign
        assert not await targetslib.is_local_homebox(
            session, rt(location={"node_id": "n-other"}))
        # any cluster location while unclustered → foreign
        assert not await targetslib.is_local_homebox(
            session, rt(location={"cluster_id": "c1"}))
        # non-homebox target is never a local homebox
        assert not await targetslib.is_local_homebox(session, rt(target="aws"))
    run(body())


def test_is_local_homebox_cluster_membership():
    async def body():
        session = await make_session()
        await set_identity(session, node_id="n-self", cluster_id="c1")
        rt = targetslib.ResolvedTarget
        assert await targetslib.is_local_homebox(
            session, rt(location={"cluster_id": "c1"}))
        assert not await targetslib.is_local_homebox(
            session, rt(location={"cluster_id": "c2"}))
        # node_id still matches even while clustered
        assert await targetslib.is_local_homebox(
            session, rt(location={"node_id": "n-self"}))
    run(body())


def test_effective_targets_populates_location():
    async def body():
        session = await make_session()
        p, env, web, api = await seed(session)
        session.add(ServiceTarget(service_id=web.id, target="homebox",
                                  config={"cluster_id": "c2"}))
        session.add(ServiceTarget(service_id=api.id, target="homebox", config={}))
        await session.commit()
        got = await targetslib.effective_targets(session, p, env)
        assert got["app"].location == {"cluster_id": "c2"}
        assert got["api"].location is None
        # a cloud target's config never yields a location
        assert targetslib._location_from_config("aws", {"cluster_id": "x"}) is None
    run(body())


def test_location_is_local_none_identity_is_legacy_local():
    # No identity resolved (legacy callers) → everything deploys locally.
    assert targetslib.location_is_local({"cluster_id": "c9"}, None)
    assert targetslib.location_is_local(None, None)


# ── 2. compose exclusion + plan entry shape ──────────────────────────────────

def test_assemble_stack_excludes_foreign_homebox(tmp_path):
    async def body():
        session = await make_session()
        p, env, web, api = await seed(session)
        # app → cluster c2 (foreign), api → cluster c1 (local): only api runs here.
        session.add(ServiceTarget(service_id=web.id, target="homebox",
                                  config={"cluster_id": "c2"}))
        session.add(ServiceTarget(service_id=api.id, target="homebox",
                                  config={"cluster_id": "c1"}))
        await session.commit()
        targets_map = await targetslib.effective_targets(session, p, env)

        from app.deploy import _assemble_stack
        detected = [
            _detected("app", kind="static", build_type="static"),
            _detected("api", kind="api", build_type="dockerfile", port=8000,
                      label="api"),
        ]
        (tmp_path / "Dockerfile").write_text("FROM scratch")
        compose_path, plan = await _assemble_stack(
            tmp_path, p, env, "calmlogic.dev", detected, {}, None,
            base=False, targets_map=targets_map,
            local_identity={"cluster_id": "c1", "node_id": "n-self"},
        )
        import yaml
        data = yaml.safe_load(compose_path.read_text())
        assert "app" not in data["services"]     # foreign: no local container
        assert "api" in data["services"]         # locally-located: unchanged
        assert plan["app"] == {
            "public": True, "host": "site--dev.calmlogic.dev", "path": None,
            "port": 80, "label": "", "target": "homebox", "remote": True,
            "cluster_id": "c2",
        }
        assert not plan["app"].get("cloud")      # never enters the cloud path
        assert "remote" not in plan["api"]
        # no Traefik route for the foreign service leaked into the compose
        assert "site--dev.calmlogic.dev" not in compose_path.read_text()
    run(body())


def test_assemble_stack_node_located_and_backcompat(tmp_path):
    async def body():
        session = await make_session()
        p, env, web, api = await seed(session)
        # app pinned to ANOTHER standalone node; api has NO row (legacy).
        session.add(ServiceTarget(service_id=web.id, target="homebox",
                                  config={"node_id": "n-other"}))
        await session.commit()
        targets_map = await targetslib.effective_targets(session, p, env)

        from app.deploy import _assemble_stack
        detected = [
            _detected("app", kind="static", build_type="static"),
            _detected("api", kind="api", build_type="dockerfile", port=8000,
                      label="api"),
        ]
        (tmp_path / "Dockerfile").write_text("FROM scratch")
        _, plan = await _assemble_stack(
            tmp_path, p, env, "calmlogic.dev", detected, {}, None,
            base=False, targets_map=targets_map,
            local_identity={"cluster_id": None, "node_id": "n-self"},
        )
        assert plan["app"]["remote"] is True and plan["app"]["node_id"] == "n-other"
        assert "remote" not in plan["api"]

        # Back-compat: same call WITHOUT local_identity (legacy callers) keeps
        # every homebox service local — the location is ignored.
        _, plan2 = await _assemble_stack(
            tmp_path, p, env, "calmlogic.dev", detected, {}, None,
            base=False, targets_map=targets_map,
        )
        assert "remote" not in plan2["app"] and "remote" not in plan2["api"]
    run(body())


# ── 3. retarget homebox@A → homebox@B: no cloud teardown, no state ───────────

def test_homebox_to_homebox_retarget_writes_no_teardown_state():
    async def body():
        client, maker = await make_app()
        async with maker() as s:
            p, env, web, api = await seed(s)
            await set_identity(s, node_id="n-self", cluster_id="c1")
            await link_account(
                s,
                clusters=[{"cluster_id": "c1", "name": "Alpha"},
                          {"cluster_id": "c2", "name": "Beta"}],
            )
            web_id = web.id

        # homebox@c1 …
        r = await client.put(f"/api/services/{web_id}/target", json={
            "target": "homebox", "config": {"cluster_id": "c1"}})
        assert r.status_code == 200, r.text
        # … retargeted to homebox@c2
        r = await client.put(f"/api/services/{web_id}/target", json={
            "target": "homebox", "config": {"cluster_id": "c2"}})
        assert r.status_code == 200, r.text

        async with maker() as s:
            row = (await s.execute(select(ServiceTarget).where(
                ServiceTarget.service_id == web_id))).scalar_one()
            assert row.target == "homebox"
            assert row.config == {"cluster_id": "c2"}
            assert "previous" not in (row.state or {})   # NOT a cloud teardown
            assert row.updated_at is not None            # cluster sync stamp

            # _teardown_retargeted finds nothing to destroy and writes nothing.
            from app.deploy import _teardown_retargeted
            p2 = (await s.execute(select(Project))).scalars().first()
            env2 = (await s.execute(select(Environment))).scalars().first()
            tail = await _teardown_retargeted(s, p2, env2)
            assert tail == ""
            await s.refresh(row)
            assert "previous" not in (row.state or {})
        await client.aclose()
    run(body())


# ── 4. tunnel/DNS exclusion for foreign hostnames ────────────────────────────

def test_foreign_homebox_hostnames_registry():
    async def body():
        session = await make_session()
        await set_identity(session, node_id="n-self", cluster_id="c1")
        session.add(Domain(name="calmlogic.dev", is_primary=True))
        p, env, web, api = await seed(session)
        session.add(ServiceTarget(service_id=web.id, target="homebox",
                                  config={"cluster_id": "c2"}))
        session.add(ServiceTarget(service_id=api.id, target="homebox",
                                  config={"cluster_id": "c1"}))  # local: excluded
        await session.commit()
        got = await targetslib.foreign_homebox_hostnames(session)
        assert got == {"site--dev.calmlogic.dev": {
            "cname_target": None, "proxied": True, "target": "homebox",
            "cluster_id": "c2",
        }}
    run(body())


def test_foreign_homebox_hostnames_empty_when_local_or_absent():
    async def body():
        session = await make_session()
        await set_identity(session, node_id="n-self", cluster_id="c1")
        session.add(Domain(name="calmlogic.dev", is_primary=True))
        p, env, web, api = await seed(session)
        session.add(ServiceTarget(service_id=web.id, target="homebox", config={}))
        session.add(ServiceTarget(service_id=api.id, target="homebox",
                                  config={"cluster_id": "c1"}))
        await session.commit()
        assert await targetslib.foreign_homebox_hostnames(session) == {}
    run(body())


class FakeCF:
    """Stands in for the cloudflare module surface _resync_dns uses (same as
    test_target_deploy_engine.FakeCF)."""

    def __init__(self):
        self.upserts: list[tuple[str, str]] = []
        self.records: dict[str, list[dict]] = {}

    def get_token(self, state):
        return "tok"

    def tunnel_target(self, tunnel_id):
        return f"{tunnel_id}.cfargotunnel.com"

    async def list_zones(self, token, account_id=None):
        return [{"id": "z1", "name": "calmlogic.dev"}]

    def resolve_zone_for(self, zones, hostname):
        host = hostname.lower().lstrip("*.")
        for z in zones:
            if host == z["name"] or host.endswith("." + z["name"]):
                return z
        return None

    async def list_dns_records(self, token, zone_id, name=None):
        if name:
            return self.records.get(name.lower(), [])
        return [r for rs in self.records.values() for r in rs]

    async def upsert_cname(self, token, zone_id, host, target, proxied=True):
        self.upserts.append((host, target))

    async def get_tunnel(self, token, account_id, tunnel_id):
        return {"connections": []}


def test_dns_drift_repair_skips_foreign_homebox_hostnames(monkeypatch):
    async def body():
        session = await make_session()
        await set_identity(session, node_id="n-self", cluster_id="c1")
        session.add(Domain(name="calmlogic.dev", is_primary=True,
                           cloudflare_routed=True))
        p, env, web, api = await seed(session)
        session.add(ServiceTarget(service_id=web.id, target="homebox",
                                  config={"cluster_id": "c2"}))
        await session.commit()

        from app.routes import tunnel as tunnel_routes
        fake = FakeCF()
        # The foreign cluster's CNAME: points at ITS tunnel, not ours. The
        # stale-record repair would normally "fix" this (cfargotunnel content,
        # served host, dead-looking tunnel) — the exclusion must stop it.
        fake.records["site--dev.calmlogic.dev"] = [{
            "id": "r1", "type": "CNAME", "name": "site--dev.calmlogic.dev",
            "content": "foreign-tunnel.cfargotunnel.com", "proxied": True,
        }]
        monkeypatch.setattr(tunnel_routes, "cf", fake)

        async def fake_served(session_):
            return {"site--dev.calmlogic.dev"}
        monkeypatch.setattr(tunnel_routes, "_served_hostnames", fake_served)

        state = {"tunnel_id": "tun-1", "account_id": "acc-1"}
        result = await tunnel_routes._resync_dns(state, session)

        upserted = {h for h, _ in fake.upserts}
        assert "calmlogic.dev" in upserted          # apex repaired as usual
        assert "*.calmlogic.dev" in upserted        # wildcard repaired as usual
        assert "site--dev.calmlogic.dev" not in upserted  # foreign: untouched
    run(body())


# ── 5. PUT /target validation ────────────────────────────────────────────────

def test_put_target_location_validation():
    async def body():
        client, maker = await make_app()
        async with maker() as s:
            p, env, web, api = await seed(s)
            await set_identity(s, node_id="n-self", cluster_id="c1")
            web_id = web.id

        # location while UNLINKED → 412
        r = await client.put(f"/api/services/{web_id}/target", json={
            "target": "homebox", "config": {"cluster_id": "c2"}})
        assert r.status_code == 412

        async with maker() as s:
            await link_account(
                s,
                clusters=[{"cluster_id": "c1", "name": "Alpha"},
                          {"cluster_id": "c2", "name": "Beta"}],
                nodes=[{"node_id": "n-solo", "name": "Solo", "cluster_id": None},
                       {"node_id": "n-b", "name": "B", "cluster_id": "c2"}],
            )

        # mutual exclusivity → 400
        r = await client.put(f"/api/services/{web_id}/target", json={
            "target": "homebox",
            "config": {"cluster_id": "c2", "node_id": "n-solo"}})
        assert r.status_code == 400

        # unknown cluster → 400
        r = await client.put(f"/api/services/{web_id}/target", json={
            "target": "homebox", "config": {"cluster_id": "c-nope"}})
        assert r.status_code == 400

        # clustered node is NOT addressable standalone → 400
        r = await client.put(f"/api/services/{web_id}/target", json={
            "target": "homebox", "config": {"node_id": "n-b"}})
        assert r.status_code == 400

        # valid cluster and valid standalone node → 200, config persisted
        r = await client.put(f"/api/services/{web_id}/target", json={
            "target": "homebox", "config": {"cluster_id": "c2"}})
        assert r.status_code == 200
        assert r.json()["target"]["config"] == {"cluster_id": "c2"}
        r = await client.put(f"/api/services/{web_id}/target", json={
            "target": "homebox", "config": {"node_id": "n-solo"}})
        assert r.status_code == 200
        assert r.json()["target"]["config"] == {"node_id": "n-solo"}

        # absent location always fine (legacy), linked or not
        r = await client.put(f"/api/services/{web_id}/target", json={
            "target": "homebox", "config": {}})
        assert r.status_code == 200
        await client.aclose()
    run(body())


# ── 6. structured options ────────────────────────────────────────────────────

def test_targets_options_unlinked():
    async def body():
        client, maker = await make_app()
        async with maker() as s:
            p, env, web, api = await seed(s)
            web_id = web.id
        r = await client.get(f"/api/services/{web_id}/targets")
        assert r.status_code == 200
        options = r.json()["options"]
        by_value = {o["value"]: o for o in options}
        assert list(by_value) == ["homebox", "cloudflare", "aws", "gcp"]
        assert by_value["homebox"]["locations"] == [
            {"kind": "local", "id": None, "name": "This Homebox", "local": True}]
        assert "locations" not in by_value["aws"]
        await client.aclose()
    run(body())


def test_targets_options_linked():
    async def body():
        client, maker = await make_app()
        async with maker() as s:
            p, env, web, api = await seed(s)
            await set_identity(s, node_id="n-self", cluster_id="c1")
            await link_account(
                s,
                clusters=[{"cluster_id": "c1", "name": "Alpha"},
                          {"cluster_id": "c2", "name": "Beta"}],
                nodes=[{"node_id": "n-solo", "name": "Solo", "cluster_id": None},
                       {"node_id": "n-self", "name": "self", "cluster_id": "c1"}],
            )
            web_id = web.id
        r = await client.get(f"/api/services/{web_id}/targets")
        homebox = next(o for o in r.json()["options"] if o["value"] == "homebox")
        assert homebox["label"] == "Homebox"
        assert homebox["locations"] == [
            {"kind": "cluster", "id": "c1", "name": "Alpha", "local": True},
            {"kind": "cluster", "id": "c2", "name": "Beta", "local": False},
            {"kind": "node", "id": "n-solo", "name": "Solo", "local": False},
        ]
        await client.aclose()
    run(body())


# ── 7. cross-target env rewrite to the foreign public host ───────────────────

def test_rewrite_env_url_to_foreign_public_host():
    env = {"API_URL": "http://api:8000/v1",
           "DATABASE_URL": "postgresql://u:p@postgres:5432/db"}
    out = targetslib.rewrite_cross_target_env(
        env, targetslib.ResolvedTarget(),
        {"api": targetslib.ResolvedTarget(
            target="homebox", location={"cluster_id": "c2"}),
         "postgres": targetslib.ResolvedTarget()},
        foreign_hosts={"api": "site-api--dev.calmlogic.dev"},
    )
    assert out["API_URL"] == "http://site-api--dev.calmlogic.dev:8000/v1"
    assert out["DATABASE_URL"] == env["DATABASE_URL"]  # local producer untouched


def test_rewrite_without_foreign_hosts_unchanged():
    env = {"API_URL": "http://api:8000/v1"}
    out = targetslib.rewrite_cross_target_env(
        env, targetslib.ResolvedTarget(),
        {"api": targetslib.ResolvedTarget(target="homebox")})
    assert out == env


# ── 8. reconcile: ownership drift queues a redeploy ──────────────────────────

def test_reconcile_homebox_ownership_drift(monkeypatch):
    async def body():
        from datetime import datetime
        from app.models import Deployment, ServiceInstance

        session = await make_session()
        await set_identity(session, node_id="n-self", cluster_id="c1")
        p, env, web, api = await seed(session)

        # Last deploy ran "app" LOCALLY (has a container)…
        dep = Deployment(environment_id=env.id, status="running",
                         stack_name="homebox-proj-site-dev")
        session.add(dep)
        await session.flush()
        session.add(ServiceInstance(
            deployment_id=dep.id, service_id=web.id, service_name="app",
            container_name="homebox-proj-site-dev-app-1", status="running",
            target="homebox"))
        await session.commit()

        queued: list[int] = []

        async def fake_queue(session_, env_):
            queued.append(env_.id)
        monkeypatch.setattr(clusterlib, "_queue_cluster_deploy", fake_queue)

        # …but the target now says it belongs to cluster c2 (retarget synced
        # in AFTER that deploy) → drift → redeploy queued.
        st = ServiceTarget(service_id=web.id, target="homebox",
                           config={"cluster_id": "c2"},
                           updated_at=datetime.utcnow())
        session.add(st)
        await session.commit()
        got = await targetslib._reconcile_homebox_locations(session, set())
        assert got == {env.id} and queued == [env.id]

        # Once the deploy reflects ownership (instance is 'remote'), no drift.
        queued.clear()
        inst = (await session.execute(select(ServiceInstance))).scalars().one()
        inst.container_name = None
        inst.status = "remote"
        await session.commit()
        assert await targetslib._reconcile_homebox_locations(session, set()) == set()
        assert queued == []

        # And the reverse: newly OURS (location flipped back to c1) while the
        # last deploy still shows it remote → drift again.
        st2 = (await session.execute(select(ServiceTarget))).scalars().one()
        st2.config = {"cluster_id": "c1"}
        st2.updated_at = datetime.utcnow()
        await session.commit()
        got = await targetslib._reconcile_homebox_locations(session, set())
        assert got == {env.id} and queued == [env.id]
    run(body())


# ── 9. serverless db plan skips foreign producers ────────────────────────────

def test_serverless_db_plan_skips_foreign_homebox_producer():
    async def body():
        session = await make_session()
        await set_identity(session, node_id="n-self", cluster_id="c1")
        p = Project(repo_full_name="al/site", name="site", managed=True)
        session.add(p)
        await session.flush()
        env = Environment(project_id=p.id, name="dev", kind="dev",
                          slug_suffix="--dev")
        web = Service(project_id=p.id, name="web", kind="web", is_public=True)
        db = Service(project_id=p.id, name="postgres", kind="database")
        session.add_all([env, web, db])
        await session.flush()
        session.add(ServiceTarget(service_id=web.id, target="gcp"))
        session.add(ServiceTarget(service_id=db.id, target="homebox",
                                  config={"cluster_id": "c2"}))  # foreign DB
        await session.commit()
        targets_map = await targetslib.effective_targets(session, p, env)
        detected = {
            "web": _detected("web", kind="web", build_type="dockerfile",
                             port=8080,
                             auto_env={"DATABASE_URL":
                                       "postgresql://u:p@postgres:5432/db"}),
            "postgres": _detected("postgres", kind="database", public=False,
                                  build_type=None),
        }
        plan = await targetslib.serverless_db_plan(
            session, p, env, targets_map, detected, "calmlogic.dev")
        # Foreign producer: OUR tunnel gets no TCP ingress / proxy rules.
        assert plan["tcp_rules"] == []
        assert plan["proxy_rules"] == {}
        assert plan["env_overrides"] == {}
    run(body())
