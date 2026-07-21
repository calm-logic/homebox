"""GET /api/domains/{id} — the domain usage drilldown (routes/domains.py).

Pins:
  1. Hostname derivation matches app/urls.py for both domain modes:
     container-mode services get name-prefixed hosts per env; base-mode
     services share the entry host with non-main services path-suffixed.
  2. Effective-domain resolution: env override → project domain → primary
     fallback; only managed projects; only public services.
  3. Target/location resolution: implicit homebox → local pseudo-location;
     a homebox row pinned to a cluster/node surfaces that id (name falls
     back to the id without an account overview); cloud rows surface the
     provider and their machine state status.
  4. Status comes from the LATEST deployment's instances.
  5. dns_overrides are filtered to the requested domain.
  6. Unknown domain id → 404.

Runs on in-memory sqlite; route coroutines are called directly (no HTTP).
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.db import Base  # noqa: E402
from app import targetslib  # noqa: E402
from app.models import (  # noqa: E402
    Deployment, Domain, Environment, Project, Service, ServiceInstance,
    ServiceTarget,
)
from app.routes import domains as domains_routes  # noqa: E402

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


async def usage(session, domain_id):
    return await domains_routes.domain_usage(domain_id, user="t", session=session)


async def seed_container_project(session, *, name="box", managed=True,
                                 project_domain_id=None):
    p = Project(repo_full_name=f"al/{name}", name=name, managed=managed,
                domain_id=project_domain_id)
    session.add(p)
    await session.flush()
    prod = Environment(project_id=p.id, name="production", kind="production",
                       slug_suffix="")
    dev = Environment(project_id=p.id, name="dev", kind="dev", slug_suffix="--dev")
    main = Service(project_id=p.id, name="main", kind="web", is_public=True,
                   subdomain_label="")
    api = Service(project_id=p.id, name="api", kind="api", is_public=True,
                  subdomain_label="api")
    db = Service(project_id=p.id, name="db", kind="database", is_public=False,
                 subdomain_label="db")
    session.add_all([prod, dev, main, api, db])
    await session.commit()
    return p, prod, dev, main, api, db


# ── 1+2. hostnames, visibility, effective-domain resolution ──────────────────

def test_container_mode_hostnames_on_primary():
    async def body():
        session = await make_session()
        d = Domain(name="x100.dev", is_primary=True, cloudflare_routed=True)
        session.add(d)
        await session.commit()
        await seed_container_project(session)

        out = await usage(session, d.id)
        assert out["name"] == "x100.dev" and out["is_primary"] is True
        hosts = {c["hostname"] for c in out["connections"]}
        assert hosts == {"box.x100.dev", "box-api.x100.dev",
                         "box--dev.x100.dev", "box-api--dev.x100.dev"}
        # private service never appears
        assert all(c["service_name"] != "db" for c in out["connections"])
        # container mode: no paths, url derived from the host
        by_host = {c["hostname"]: c for c in out["connections"]}
        assert by_host["box-api--dev.x100.dev"]["path"] is None
        assert by_host["box-api--dev.x100.dev"]["url"] == "https://box-api--dev.x100.dev"
        # implicit homebox target → local pseudo-location
        c = by_host["box.x100.dev"]
        assert c["target"] == "homebox"
        assert c["location"]["kind"] == "local"
        assert c["location"]["name"] == "This Homebox"
        assert c["status"] is None and c["deploy_status"] is None
    run(body())


def test_unmanaged_projects_excluded():
    async def body():
        session = await make_session()
        d = Domain(name="x100.dev", is_primary=True)
        session.add(d)
        await session.commit()
        await seed_container_project(session, name="ghost", managed=False)
        out = await usage(session, d.id)
        assert out["connections"] == []
    run(body())


def test_effective_domain_precedence():
    async def body():
        session = await make_session()
        primary = Domain(name="x100.dev", is_primary=True)
        own = Domain(name="box.io")
        session.add_all([primary, own])
        await session.commit()
        p, prod, dev, *_ = await seed_container_project(
            session, project_domain_id=own.id)
        # env-level override: dev goes back to the primary domain
        dev.domain_id = primary.id
        await session.commit()

        own_out = await usage(session, own.id)
        prim_out = await usage(session, primary.id)
        own_envs = {c["environment_name"] for c in own_out["connections"]}
        prim_envs = {c["environment_name"] for c in prim_out["connections"]}
        assert own_envs == {"production"}          # project domain
        assert prim_envs == {"dev"}                # env override wins
        assert {c["hostname"] for c in own_out["connections"]} \
            == {"box.box.io", "box-api.box.io"}
        assert {c["hostname"] for c in prim_out["connections"]} \
            == {"box--dev.x100.dev", "box-api--dev.x100.dev"}
    run(body())


def test_base_mode_paths():
    async def body():
        session = await make_session()
        d = Domain(name="infinitescroll.io", is_primary=True)
        session.add(d)
        await session.commit()
        p, *_ = await seed_container_project(session, name="site")
        p.domain_mode = "base"
        await session.commit()

        out = await usage(session, d.id)
        rows = {(c["environment_name"], c["service_name"]): c
                for c in out["connections"]}
        # every service shares the env's entry host
        assert rows[("production", "main")]["hostname"] == "infinitescroll.io"
        assert rows[("production", "main")]["path"] is None
        assert rows[("production", "api")]["hostname"] == "infinitescroll.io"
        assert rows[("production", "api")]["path"] == "/api"
        assert rows[("production", "api")]["url"] == "https://infinitescroll.io/api"
        assert rows[("dev", "main")]["hostname"] == "dev.infinitescroll.io"
        assert rows[("dev", "api")]["hostname"] == "dev.infinitescroll.io"
        assert rows[("dev", "api")]["path"] == "/api"
    run(body())


# ── 3. target + location resolution ───────────────────────────────────────────

def test_cloud_and_pinned_homebox_locations():
    async def body():
        session = await make_session()
        d = Domain(name="x100.dev", is_primary=True)
        session.add(d)
        await session.commit()
        p, prod, dev, main, api, _db = await seed_container_project(session)
        # api → AWS everywhere (service-wide default row), with machine state
        session.add(ServiceTarget(service_id=api.id, environment_id=None,
                                  target="aws",
                                  state={"status": "deployed",
                                         "endpoint": "https://x.awsapprunner.com"}))
        # main → homebox pinned at another cluster
        session.add(ServiceTarget(service_id=main.id, environment_id=None,
                                  target="homebox",
                                  config={"cluster_id": "c-other"}))
        await session.commit()

        out = await usage(session, d.id)
        by_key = {(c["service_name"], c["environment_name"]): c
                  for c in out["connections"]}
        aws = by_key[("api", "production")]
        assert aws["target"] == "aws"
        assert aws["location"] == {"kind": "cloud", "id": "aws", "name": "AWS"}
        assert aws["status"] == "deployed"          # cloud state, not instances
        pinned = by_key[("main", "dev")]
        assert pinned["target"] == "homebox"
        assert pinned["location"]["kind"] == "cluster"
        # no account overview cached → name falls back to the id
        assert pinned["location"]["id"] == "c-other"
        assert pinned["location"]["name"] == "c-other"
    run(body())


# ── 4. status from the LATEST deployment ──────────────────────────────────────

def test_status_from_latest_deployment_instances():
    async def body():
        session = await make_session()
        d = Domain(name="x100.dev", is_primary=True)
        session.add(d)
        await session.commit()
        p, prod, dev, main, api, _db = await seed_container_project(session)

        old = Deployment(environment_id=prod.id, status="failed", stack_name="s",
                         created_at=datetime(2026, 1, 1))
        new = Deployment(environment_id=prod.id, status="running", stack_name="s",
                         created_at=datetime(2026, 2, 1))
        session.add_all([old, new])
        await session.flush()
        session.add_all([
            ServiceInstance(deployment_id=old.id, service_name="main",
                            status="exited"),
            ServiceInstance(deployment_id=new.id, service_name="main",
                            status="running", url="https://box.x100.dev"),
        ])
        await session.commit()

        out = await usage(session, d.id)
        by_key = {(c["service_name"], c["environment_name"]): c
                  for c in out["connections"]}
        assert by_key[("main", "production")]["status"] == "running"
        assert by_key[("main", "production")]["deploy_status"] == "running"
        # api has no instance row in the latest deploy → status None
        assert by_key[("api", "production")]["status"] is None
        # dev env has no deployments at all
        assert by_key[("main", "dev")]["deploy_status"] is None
    run(body())


# ── 5. dns_overrides filtered to the domain ───────────────────────────────────

def test_dns_overrides_filtered():
    async def body():
        session = await make_session()
        d = Domain(name="x100.dev", is_primary=True)
        other = Domain(name="other.dev")
        session.add_all([d, other])
        await session.commit()
        meta = {"domain": "x100.dev", "zone_id": "z1",
                "cname_target": "tun-b.cfargotunnel.com", "proxied": True,
                "project": "site", "env": "dev", "service": "app",
                "created_at": "2026-07-18T00:00:00"}
        await targetslib.save_dns_overrides(session, {
            "site--dev.x100.dev": meta,
            "app.other.dev": {**meta, "domain": "other.dev"},
        })

        out = await usage(session, d.id)
        assert [o["hostname"] for o in out["dns_overrides"]] \
            == ["site--dev.x100.dev"]
        o = out["dns_overrides"][0]
        assert o["cname_target"] == "tun-b.cfargotunnel.com"
        assert o["project"] == "site" and o["env"] == "dev" and o["service"] == "app"

        out2 = await usage(session, other.id)
        assert [o["hostname"] for o in out2["dns_overrides"]] == ["app.other.dev"]
    run(body())


# ── 7. system block: admin hostname + tunnel + served_by ─────────────────────

async def _set(session, key, value):
    from app import clusterlib
    await clusterlib._set_setting(session, key, value)
    await session.commit()


def test_admin_hostname_belongs_to_this_domain():
    async def body():
        session = await make_session()
        d = Domain(name="x100.dev", is_primary=True, cloudflare_routed=True)
        other = Domain(name="other.dev", cloudflare_routed=True)
        session.add_all([d, other])
        await session.commit()
        await _set(session, "admin_domain", "admin.x100.dev")

        out = await usage(session, d.id)
        hosts = out["system"]["hostnames"]
        assert [h["hostname"] for h in hosts] == ["admin.x100.dev"]
        assert hosts[0]["kind"] == "admin"
        assert hosts[0]["label"] == "Homebox admin UI"
        # unclustered → served locally
        assert hosts[0]["served_by"] == {"kind": "local", "name": "This Homebox"}

        # the admin hostname does NOT leak onto a different domain
        out2 = await usage(session, other.id)
        assert out2["system"]["hostnames"] == []
    run(body())


def test_admin_hostname_equal_to_domain():
    async def body():
        session = await make_session()
        d = Domain(name="admin.x100.dev", cloudflare_routed=True)
        session.add(d)
        await session.commit()
        await _set(session, "admin_domain", "admin.x100.dev")
        out = await usage(session, d.id)
        assert [h["hostname"] for h in out["system"]["hostnames"]] == ["admin.x100.dev"]
    run(body())


def test_tunnel_block_local_when_unclustered():
    async def body():
        from app import cloudflare as cf
        session = await make_session()
        d = Domain(name="x100.dev", is_primary=True, cloudflare_routed=True)
        session.add(d)
        await session.commit()
        await cf.save_state(session, {
            "account_id": "acc-1",
            "tunnel_id": "tun-xyz",
            "tunnel_name": "homebox-home",
        })

        out = await usage(session, d.id)
        t = out["system"]["tunnel"]
        assert t is not None
        assert t["apex"] == "x100.dev"
        assert t["wildcard"] == "*.x100.dev"
        assert t["cname_target"] == "tun-xyz.cfargotunnel.com"
        assert t["tunnel_id"] == "tun-xyz"
        assert t["tunnel_name"] == "homebox-home"
        assert t["served_by"] == {"kind": "local", "name": "This Homebox"}
    run(body())


def test_tunnel_served_by_cluster_name():
    async def body():
        from app import cloudflare as cf, clusterlib
        session = await make_session()
        d = Domain(name="x100.dev", is_primary=True, cloudflare_routed=True)
        session.add(d)
        await session.commit()
        await cf.save_state(session, {"tunnel_id": "tun-1", "tunnel_name": "t"})
        await clusterlib.save_cluster(session, {"cluster_id": "c1", "name": "beta"})
        await session.commit()

        out = await usage(session, d.id)
        assert out["system"]["tunnel"]["served_by"] == {"kind": "cluster", "name": "beta"}
    run(body())


def test_tunnel_served_by_cluster_default_home():
    async def body():
        from app import cloudflare as cf, clusterlib
        session = await make_session()
        d = Domain(name="x100.dev", is_primary=True, cloudflare_routed=True)
        session.add(d)
        await session.commit()
        await cf.save_state(session, {"tunnel_id": "tun-1", "tunnel_name": "t"})
        # clustered but no cluster name set → 'home' default
        await clusterlib.save_cluster(session, {"cluster_id": "c1"})
        await session.commit()

        out = await usage(session, d.id)
        assert out["system"]["tunnel"]["served_by"] == {"kind": "cluster", "name": "home"}
    run(body())


def test_tunnel_null_when_domain_not_routed():
    async def body():
        from app import cloudflare as cf
        session = await make_session()
        d = Domain(name="x100.dev", is_primary=True, cloudflare_routed=False)
        session.add(d)
        await session.commit()
        await cf.save_state(session, {"tunnel_id": "tun-1", "tunnel_name": "t"})
        out = await usage(session, d.id)
        assert out["system"]["tunnel"] is None
    run(body())


def test_system_block_graceful_without_integration_or_admin():
    async def body():
        session = await make_session()
        d = Domain(name="x100.dev", is_primary=True, cloudflare_routed=True)
        session.add(d)
        await session.commit()
        # no cloudflare integration, no admin_domain setting
        out = await usage(session, d.id)
        assert out["system"] == {"tunnel": None, "hostnames": []}
    run(body())


# ── 6. 404 ────────────────────────────────────────────────────────────────────

def test_unknown_domain_404():
    async def body():
        session = await make_session()
        with pytest.raises(HTTPException) as exc:
            await usage(session, 999)
        assert exc.value.status_code == 404
    run(body())
