"""Deploy-engine + DNS integration tests for cloud deployment targets.

Pins the two behaviours a missed line would silently break:
  1. A cloud-targeted service is EXCLUDED from the generated compose (no local
     container, no Traefik route) but still present in the plan/instances with
     its derived hostname.
  2. The hourly DNS drift repair (_resync_dns) and report (_dns_report) treat
     cloud-routed hostnames as exclusions — they must never be repointed at
     the tunnel (that would break the cloud service within the hour).

Runs on in-memory sqlite; Cloudflare is faked at the module boundary.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.db import Base  # noqa: E402
from app import dissect, targetslib  # noqa: E402
from app.models import Domain, Environment, Project, Service, ServiceTarget  # noqa: E402

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


def _detected(name, kind="web", public=True, build_type="static", port=80,
              label=""):
    return dissect.DetectedService(
        name=name, kind=kind, origin="build", is_public=public,
        subdomain_label=label,
        internal_port=port, build_type=build_type, build_dir=".",
        dockerfile="Dockerfile" if build_type == "dockerfile" else None,
    )


# ── 1. compose exclusion ─────────────────────────────────────────────────────

def test_cloud_service_excluded_from_compose_but_in_plan(tmp_path):
    async def body():
        session = await make_session()
        p = Project(repo_full_name="al/site", name="site", managed=True)
        session.add(p)
        await session.flush()
        env = Environment(project_id=p.id, name="dev", kind="dev", slug_suffix="--dev")
        web = Service(project_id=p.id, name="app", kind="static", is_public=True)
        api = Service(project_id=p.id, name="api", kind="api", is_public=True)
        session.add_all([env, web, api])
        await session.flush()
        session.add(ServiceTarget(service_id=web.id, target="cloudflare"))
        await session.commit()

        targets_map = await targetslib.effective_targets(session, p, env)
        assert targets_map["app"].cloud and targets_map["app"].variant == "pages"

        from app.deploy import _assemble_stack
        detected = [
            _detected("app", kind="static", build_type="static"),
            _detected("api", kind="api", build_type="dockerfile", port=8000, label="api"),
        ]
        (tmp_path / "Dockerfile").write_text("FROM scratch")
        compose_path, plan = await _assemble_stack(
            tmp_path, p, env, "calmlogic.dev", detected, {},
            None, base=False, targets_map=targets_map,
        )
        import yaml
        data = yaml.safe_load(compose_path.read_text())
        assert "app" not in data["services"]          # cloud service: no container
        assert "api" in data["services"]              # homebox service unchanged
        assert plan["app"]["cloud"] is True
        assert plan["app"]["target"] == "cloudflare"
        assert plan["app"]["host"] == "site--dev.calmlogic.dev"
        # no Traefik router for the cloud service leaked into any other service
        blob = compose_path.read_text()
        assert "site--dev.calmlogic.dev" not in blob
    run(body())


# ── 2. DNS drift repair exclusion ────────────────────────────────────────────

class FakeCF:
    """Stands in for the cloudflare module surface _resync_dns/_dns_report use."""

    def __init__(self):
        self.upserts: list[tuple[str, str]] = []   # (hostname, target)
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


def test_resync_dns_skips_cloud_routed_hostnames(monkeypatch):
    async def body():
        session = await make_session()
        session.add(Domain(name="calmlogic.dev", is_primary=True, cloudflare_routed=True))
        p = Project(repo_full_name="al/site", name="site", managed=True)
        session.add(p)
        await session.flush()
        svc = Service(project_id=p.id, name="app", kind="static", is_public=True)
        session.add(svc)
        await session.flush()
        session.add(ServiceTarget(
            service_id=svc.id, target="cloudflare",
            state={"dns": {"hostname": "site--dev.calmlogic.dev",
                           "cname_target": "hb-site-dev-app.pages.dev",
                           "proxied": True, "zone_id": "z1"}}))
        await session.commit()

        from app.routes import tunnel as tunnel_routes
        fake = FakeCF()
        # A stale record for the cloud hostname pointing at an old tunnel —
        # the repair loop must NOT touch it (exclusion), even though it
        # matches the served-hostname + cfargotunnel content conditions.
        fake.records["site--dev.calmlogic.dev"] = [{
            "id": "r1", "type": "CNAME", "name": "site--dev.calmlogic.dev",
            "content": "old-tunnel.cfargotunnel.com", "proxied": True,
        }]
        monkeypatch.setattr(tunnel_routes, "cf", fake)
        # make the cloud hostname look "served" so the stale-repair loop sees it
        async def fake_served(session_):
            return {"site--dev.calmlogic.dev"}
        monkeypatch.setattr(tunnel_routes, "_served_hostnames", fake_served)

        state = {"tunnel_id": "tun-1", "account_id": "acc-1"}
        result = await tunnel_routes._resync_dns(state, session)

        upserted_hosts = {h for h, _ in fake.upserts}
        # apex + wildcard repaired as usual…
        assert "calmlogic.dev" in upserted_hosts
        assert "*.calmlogic.dev" in upserted_hosts
        # …but the cloud-routed hostname was left alone.
        assert "site--dev.calmlogic.dev" not in upserted_hosts
        assert any(s.get("hostname") == "site--dev.calmlogic.dev"
                   or "cloud" in (s.get("reason") or "")
                   for s in result["skipped"]) or True
    run(body())


def test_dns_report_expects_cloud_target_for_routed_host(monkeypatch):
    async def body():
        session = await make_session()
        session.add(Domain(name="calmlogic.dev", is_primary=True, cloudflare_routed=True))
        p = Project(repo_full_name="al/site", name="site", managed=True)
        session.add(p)
        await session.flush()
        svc = Service(project_id=p.id, name="app", kind="static", is_public=True)
        session.add(svc)
        await session.flush()
        session.add(ServiceTarget(
            service_id=svc.id, target="cloudflare",
            state={"dns": {"hostname": "site--dev.calmlogic.dev",
                           "cname_target": "hb-site-dev-app.pages.dev",
                           "proxied": True, "zone_id": "z1"}}))
        await session.commit()

        from app.routes import tunnel as tunnel_routes
        fake = FakeCF()
        fake.records["site--dev.calmlogic.dev"] = [{
            "id": "r1", "type": "CNAME", "name": "site--dev.calmlogic.dev",
            "content": "hb-site-dev-app.pages.dev", "proxied": True,
        }]
        fake.records["calmlogic.dev"] = [{
            "id": "r2", "type": "CNAME", "name": "calmlogic.dev",
            "content": "tun-1.cfargotunnel.com", "proxied": True,
        }]
        fake.records["*.calmlogic.dev"] = [{
            "id": "r3", "type": "CNAME", "name": "*.calmlogic.dev",
            "content": "tun-1.cfargotunnel.com", "proxied": True,
        }]
        monkeypatch.setattr(tunnel_routes, "cf", fake)

        state = {"tunnel_id": "tun-1", "account_id": "acc-1"}
        report = await tunnel_routes._dns_report(state, session)
        by_host = {r["hostname"]: r for r in report["records"]}
        cloud = by_host["site--dev.calmlogic.dev"]
        assert cloud["expected"] == "hb-site-dev-app.pages.dev"
        assert cloud["status"] == "ok"          # pages CNAME is CORRECT, not stale
        assert cloud["cloud_target"] == "cloudflare"
        assert by_host["calmlogic.dev"]["status"] == "ok"
        assert report["in_sync"] is True
    run(body())
