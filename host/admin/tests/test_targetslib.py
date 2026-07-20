"""Unit tests for targetslib: target resolution precedence, cloud-coordinator
election, the DNS exclusion registry, and cross-target env rewriting.

Runs on in-memory sqlite (aiosqlite) with synthetic rosters — no cluster, no
cloud accounts.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.db import Base  # noqa: E402
from app import targetslib  # noqa: E402
from app.models import Environment, Integration, Project, Service, ServiceTarget  # noqa: E402
from app.targets import options_for_kind, variant_for  # noqa: E402

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


async def seed(session):
    p = Project(repo_full_name="al/app", name="app", managed=True)
    session.add(p)
    await session.flush()
    prod = Environment(project_id=p.id, name="production", kind="production")
    dev = Environment(project_id=p.id, name="dev", kind="dev")
    web = Service(project_id=p.id, name="web", kind="web", is_public=True)
    db = Service(project_id=p.id, name="postgres", kind="database")
    session.add_all([prod, dev, web, db])
    await session.commit()
    return p, prod, dev, web, db


# ── resolution precedence ─────────────────────────────────────────────────────

def test_no_rows_means_homebox():
    async def body():
        session = await make_session()
        p, prod, *_ = await seed(session)
        got = await targetslib.effective_targets(session, p, prod)
        assert got == {}
        assert targetslib.resolve_for(got, "web").target == "homebox"
        assert not targetslib.resolve_for(got, "web").cloud
    run(body())


def test_env_row_beats_default_row():
    async def body():
        session = await make_session()
        p, prod, dev, web, _ = await seed(session)
        session.add(ServiceTarget(service_id=web.id, environment_id=None,
                                  target="gcp", config={}))
        session.add(ServiceTarget(service_id=web.id, environment_id=dev.id,
                                  target="homebox", config={}))
        await session.commit()
        prod_map = await targetslib.effective_targets(session, p, prod)
        dev_map = await targetslib.effective_targets(session, p, dev)
        assert prod_map["web"].target == "gcp"           # default row applies
        assert prod_map["web"].variant == "cloud_run"    # derived from kind
        assert dev_map["web"].target == "homebox"        # env override wins
    run(body())


def test_variant_derivation_and_options():
    assert variant_for("cloudflare", "static") == "pages"
    assert variant_for("aws", "static") == "s3"
    assert variant_for("aws", "web") == "app_runner"
    assert variant_for("gcp", "database") == "gce_db"
    assert variant_for("cloudflare", "web") == "cf_containers"
    assert variant_for("cloudflare", "database") is None  # unsupported combo
    assert variant_for("aws", "web", {"variant": "lambda"}) == "lambda"  # override
    assert options_for_kind("static") == ["homebox", "cloudflare", "aws", "gcp"]
    assert options_for_kind("web") == ["homebox", "cloudflare", "aws", "gcp"]
    assert options_for_kind("cache") == ["homebox"]


def test_project_target_materializes_and_preserves_service_override():
    async def body():
        session = await make_session()
        p, prod, _dev, web, db = await seed(session)
        aws = Integration(provider="aws", account_login="123", status="connected")
        session.add(aws)
        await session.flush()
        p.deployment_target = "aws"
        p.deployment_target_integration_id = aws.id
        session.add(ServiceTarget(service_id=web.id, environment_id=prod.id,
                                  target="homebox", config={}))
        await targetslib.sync_project_target_rows(session, p)
        await session.commit()

        rows = list((await session.execute(select(ServiceTarget))).scalars())
        inherited = [r for r in rows if (r.config or {}).get("_project_default")]
        assert {(r.service_id, r.environment_id, r.target) for r in inherited} == {
            (web.id, _dev.id, "aws"),
            (db.id, prod.id, "aws"), (db.id, _dev.id, "aws"),
        }
        prod_map = await targetslib.effective_targets(session, p, prod)
        assert prod_map["web"].target == "homebox"  # explicit env row wins
        assert prod_map["postgres"].target == "aws"
    run(body())


def test_automatic_project_target_keeps_dev_local_and_uses_supported_cloud():
    async def body():
        session = await make_session()
        p, prod, dev, _web, _db = await seed(session)
        session.add(Integration(provider="cloudflare", account_login="acct", status="connected"))
        await session.commit()
        p.deployment_target = "automatic"
        await targetslib.sync_project_target_rows(session, p)
        await session.commit()

        prod_map = await targetslib.effective_targets(session, p, prod)
        dev_map = await targetslib.effective_targets(session, p, dev)
        assert prod_map["web"].target == "cloudflare"
        assert prod_map["postgres"].target == "homebox"  # unsupported by Cloudflare
        assert {resolved.target for resolved in dev_map.values()} == {"homebox"}
    run(body())


# ── coordinator election ──────────────────────────────────────────────────────

def _roster(*entries):
    return {"roster": [
        {"node_id": nid, "ordinal": o, "role": role, "online": True,
         "serving": serving}
        for nid, o, role, serving in entries
    ]}


@pytest.fixture
def as_node(monkeypatch):
    def set_identity(node_id, role="peer"):
        from app import clusterlib
        from app.config import settings
        async def fake_get_node_id(session):
            return node_id
        monkeypatch.setattr(clusterlib, "get_node_id", fake_get_node_id)
        monkeypatch.setattr(settings, "node_role", role)
    return set_identity


def test_single_node_is_coordinator(as_node):
    async def body():
        session = await make_session()
        as_node("n-a")
        assert await targetslib.is_cloud_coordinator(session, None)
        assert await targetslib.is_cloud_coordinator(session, {"roster": []})
    run(body())


def test_lowest_healthy_ordinal_coordinates(as_node):
    async def body():
        session = await make_session()
        roster = _roster(("n-a", 1, "peer", True), ("n-b", 2, "peer", True))
        as_node("n-a")
        assert await targetslib.is_cloud_coordinator(session, roster)
        as_node("n-b")
        assert not await targetslib.is_cloud_coordinator(session, roster)
    run(body())


def test_unhealthy_lowest_is_skipped(as_node):
    async def body():
        session = await make_session()
        roster = {"roster": [
            {"node_id": "n-a", "ordinal": 1, "role": "peer", "online": False,
             "serving": True},   # offline, no last_seen → not fresh
            {"node_id": "n-b", "ordinal": 2, "role": "peer", "online": True,
             "serving": True},
        ]}
        as_node("n-b")
        assert await targetslib.is_cloud_coordinator(session, roster)
    run(body())


def test_mirror_never_coordinates(as_node):
    async def body():
        session = await make_session()
        as_node("n-a", role="mirror")
        assert not await targetslib.is_cloud_coordinator(session, None)
        roster = _roster(("n-a", 1, "peer", True))
        assert not await targetslib.is_cloud_coordinator(session, roster)
    run(body())


def test_mirror_roster_entries_ignored(as_node):
    async def body():
        session = await make_session()
        roster = _roster(("n-m", 1, "mirror", True), ("n-b", 2, "peer", True))
        as_node("n-b")
        assert await targetslib.is_cloud_coordinator(session, roster)
    run(body())


# ── exclusion registry ────────────────────────────────────────────────────────

def test_cloud_routed_hostnames():
    async def body():
        session = await make_session()
        _, _, _, web, db = await seed(session)
        session.add(ServiceTarget(
            service_id=web.id, target="cloudflare",
            state={"dns": {"hostname": "App.calmlogic.dev",
                           "cname_target": "app-123.pages.dev", "proxied": True}}))
        session.add(ServiceTarget(service_id=db.id, target="aws", state={}))  # no dns yet
        await session.commit()
        got = await targetslib.cloud_routed_hostnames(session)
        assert got == {"app.calmlogic.dev": {
            "cname_target": "app-123.pages.dev", "proxied": True,
            "target": "cloudflare"}}
    run(body())


# ── cross-target env rewrite ──────────────────────────────────────────────────

def test_rewrite_leaves_same_target_alone():
    env = {"DATABASE_URL": "postgresql://u:p@postgres:5432/db"}
    out = targetslib.rewrite_cross_target_env(
        env, targetslib.ResolvedTarget(),
        {"postgres": targetslib.ResolvedTarget()})
    assert out == env


def test_rewrite_homebox_consumer_of_cloud_vm_db():
    env = {"DATABASE_URL": "postgresql://u:p@postgres:5432/db",
           "REDIS_URL": "redis://cache:6379"}
    producers = {"postgres": targetslib.ResolvedTarget(
        target="aws", state={"mesh": {"ip": "10.77.240.1"}})}
    out = targetslib.rewrite_cross_target_env(env, targetslib.ResolvedTarget(), producers)
    assert out["DATABASE_URL"] == "postgresql://u:p@10.77.240.1:5432/db"
    assert out["REDIS_URL"] == "redis://cache:6379"  # producer not cloud → untouched


def test_rewrite_without_endpoint_is_noop():
    env = {"DATABASE_URL": "postgresql://u:p@postgres:5432/db"}
    producers = {"postgres": targetslib.ResolvedTarget(target="aws", state={})}
    out = targetslib.rewrite_cross_target_env(env, targetslib.ResolvedTarget(), producers)
    assert out == env
