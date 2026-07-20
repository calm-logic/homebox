"""Cross-cluster domain sharing — per-host DNS overrides (G12,
host/docs/demo-video-plan.md).

A domain's apex/wildcard CNAME points at the tunnel of whichever cluster
CONNECTED it, while Domains + the Cloudflare integration sync account-wide. A
homebox-local public service deployed on a DIFFERENT cluster under that domain
must therefore get a specific-host proxied CNAME → the deploying cluster's own
tunnel (specific beats wildcard at Cloudflare). Pins:

  1. cloudflare.domain_owned_by_local_tunnel truth table: our tunnel / other
     tunnel / no wildcard / non-tunnel record / API error → fail-safe owned
     (strict=True re-raises), apex fallback, per-run cache.
  2. deploy._ensure_domain_overrides on a foreign-owned domain upserts exactly
     one proxied CNAME → OUR tunnel per local public hostname, records
     bookkeeping (targetslib dns_overrides setting) and pushes ingress with a
     per-host rule. Base-mode apex hosts are never overridden.
  3. Owned-domain deploy creates NO per-host record (regression) — including
     the no-wildcard-at-all case.
  4. Teardown/retarget: stale overrides delete ONLY records pointing at our
     tunnel; deploy._cleanup_domain_overrides (teardown_stack path) scopes to
     the env. API errors change nothing (fail-safe).
  5. routes/tunnel._dns_report treats override hostnames as expected records
     (target = OUR tunnel) and a foreign-live wildcard as informational, not
     drift; _resync_dns skips the foreign wildcard, keeps repairing DEAD
     foreign tunnels, and re-pins drifted override records.
  6. Bookkeeping round-trip via targetslib.load/save_dns_overrides.

Runs on in-memory sqlite; the Cloudflare module surface is stubbed at the
module boundary (app.cloudflare attributes) so the real ownership/override
logic runs against a fake API.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.db import Base  # noqa: E402
from app import cloudflare as cfmod  # noqa: E402
from app import deploy, targetslib  # noqa: E402
from app.models import Domain, Environment, Project, Service  # noqa: E402

_ENGINES: list = []

OUR = "tun-b"
OUR_TARGET = "tun-b.cfargotunnel.com"
FOREIGN_TARGET = "tun-a.cfargotunnel.com"
HOST = "site--dev.x100.dev"


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
    session.add(Domain(name="x100.dev", is_primary=True, cloudflare_routed=True))
    p = Project(repo_full_name="al/site", name="site", managed=True)
    session.add(p)
    await session.flush()
    env = Environment(project_id=p.id, name="dev", kind="dev", slug_suffix="--dev")
    svc = Service(project_id=p.id, name="app", kind="static", is_public=True)
    session.add_all([env, svc])
    await session.commit()
    return p, env, svc


class CFStub:
    """Fake Cloudflare API state installed over app.cloudflare's raw calls.
    tunnel_target / resolve_zone_for / build_ingress / wildcard_tunnel_cname /
    domain_owned_by_local_tunnel stay REAL — the tests exercise the genuine
    ownership and override logic against this fake record store."""

    def __init__(self, tunnel_id=OUR):
        self.state = {"tunnel_id": tunnel_id, "account_id": "acc-1",
                      "token_encrypted": "enc"}
        self.zones = [{"id": "z1", "name": "x100.dev"}]
        self.records: dict[str, list[dict]] = {}
        self.upserts: list[tuple[str, str, bool]] = []
        self.deletes: list[str] = []
        self.ingress_pushes: list[list[dict]] = []
        self.live: dict[str, list] = {}   # tunnel id -> connections
        self.fail_zones = False           # list_zones raises

    # -- record helpers --------------------------------------------------
    def set_cname(self, name, content, *, rec_id=None, proxied=True):
        self.records[name.lower()] = [{
            "id": rec_id or f"r-{name}", "type": "CNAME", "name": name,
            "content": content, "proxied": proxied,
        }]

    # -- stubbed API surface ----------------------------------------------
    async def load_state(self, session):
        return dict(self.state)

    def get_token(self, state):
        return "tok"

    async def list_zones(self, token, account_id=None):
        if self.fail_zones:
            raise cfmod.CloudflareError(500, "boom")
        return list(self.zones)

    async def list_dns_records(self, token, zone_id, name=None):
        if name:
            return list(self.records.get(name.lower(), []))
        return [r for rs in self.records.values() for r in rs]

    async def upsert_cname(self, token, zone_id, host, target, proxied=True):
        self.upserts.append((host, target, proxied))
        self.set_cname(host, target, proxied=proxied)
        return {}

    async def delete_dns_record(self, token, zone_id, rec_id):
        self.deletes.append(rec_id)
        for name, recs in list(self.records.items()):
            kept = [r for r in recs if r["id"] != rec_id]
            if kept:
                self.records[name] = kept
            elif recs != kept:
                del self.records[name]

    async def put_tunnel_config(self, token, account_id, tunnel_id, ingress):
        self.ingress_pushes.append(ingress)

    async def get_tunnel(self, token, account_id, tunnel_id):
        return {"id": tunnel_id, "connections": list(self.live.get(tunnel_id, []))}

    def install(self, monkeypatch):
        for name in ("load_state", "get_token", "list_zones", "list_dns_records",
                     "upsert_cname", "delete_dns_record", "put_tunnel_config",
                     "get_tunnel"):
            monkeypatch.setattr(cfmod, name, getattr(self, name))
        return self


# ── 1. ownership truth table ──────────────────────────────────────────────────

def test_ownership_truth_table(monkeypatch):
    async def body():
        stub = CFStub().install(monkeypatch)
        state = dict(stub.state)

        async def owned():
            return await cfmod.domain_owned_by_local_tunnel(
                None, "x100.dev", state=state)

        # wildcard → OUR tunnel: owned
        stub.set_cname("*.x100.dev", OUR_TARGET)
        assert await owned() is True
        # wildcard → ANOTHER tunnel: not owned
        stub.set_cname("*.x100.dev", FOREIGN_TARGET)
        assert await owned() is False
        # no wildcard/apex at all: owned (fail-safe — behavior unchanged)
        stub.records.clear()
        assert await owned() is True
        # apex fallback: no wildcard, apex → another tunnel: not owned
        stub.set_cname("x100.dev", FOREIGN_TARGET)
        assert await owned() is False
        # wildcard is NOT a tunnel CNAME (external record): owned
        stub.records.clear()
        stub.set_cname("*.x100.dev", "ghs.googlehosted.com")
        assert await owned() is True
        # zone not in the account: owned
        stub.records.clear()
        stub.zones = [{"id": "z9", "name": "other.dev"}]
        assert await owned() is True
        stub.zones = [{"id": "z1", "name": "x100.dev"}]
        # no tunnel configured here: owned, no API calls needed
        assert await cfmod.domain_owned_by_local_tunnel(
            None, "x100.dev", state={"token_encrypted": "enc"}) is True
        # API error → fail-safe owned; strict=True re-raises
        stub.set_cname("*.x100.dev", FOREIGN_TARGET)
        stub.fail_zones = True
        assert await owned() is True
        with pytest.raises(cfmod.CloudflareError):
            await cfmod.domain_owned_by_local_tunnel(
                None, "x100.dev", state=state, strict=True)
        stub.fail_zones = False
        # per-run cache: the first (foreign) answer sticks
        cache: dict = {}
        assert await cfmod.domain_owned_by_local_tunnel(
            None, "x100.dev", state=state, cache=cache) is False
        stub.set_cname("*.x100.dev", OUR_TARGET)   # record changes mid-run…
        assert await cfmod.domain_owned_by_local_tunnel(
            None, "x100.dev", state=state, cache=cache) is False  # …cache wins
    run(body())


# ── 2. deploy on a foreign-owned domain writes the override ─────────────────

def test_deploy_upserts_override_on_foreign_domain(monkeypatch):
    async def body():
        session = await make_session()
        p, env, _svc = await seed(session)
        stub = CFStub().install(monkeypatch)
        stub.set_cname("*.x100.dev", FOREIGN_TARGET)

        plan = {
            "app": {"public": True, "host": HOST, "port": 80, "label": ""},
            "db": {"public": False, "host": None, "port": 5432, "label": ""},
            # base-mode apex host: never overridden (that IS the owner's apex)
            "root": {"public": True, "host": "x100.dev", "port": 80, "label": ""},
        }
        tail = await deploy._ensure_domain_overrides(
            session, p, env, plan, "x100.dev")

        # exactly ONE proxied CNAME → OUR tunnel, for the subdomain host only
        assert stub.upserts == [(HOST, OUR_TARGET, True)]
        assert HOST in tail

        # bookkeeping recorded
        overrides = await targetslib.load_dns_overrides(session)
        assert set(overrides) == {HOST}
        meta = overrides[HOST]
        assert meta["domain"] == "x100.dev" and meta["zone_id"] == "z1"
        assert meta["cname_target"] == OUR_TARGET and meta["proxied"] is True
        assert meta["project"] == "site" and meta["env"] == "dev"
        assert meta["service"] == "app" and meta["created_at"]

        # ingress pushed with a per-host rule (before the domain rules,
        # catch-all last)
        ingress = stub.ingress_pushes[-1]
        assert {"hostname": HOST, "service": "http://traefik:80"} in ingress
        assert ingress.index({"hostname": HOST, "service": "http://traefik:80"}) \
            < ingress.index({"hostname": "*.x100.dev", "service": "http://traefik:80"})
        assert ingress[-1] == {"service": "http_status:404"}

        # idempotent re-run: same record upserted, but no bookkeeping change →
        # no second ingress push
        pushes = len(stub.ingress_pushes)
        await deploy._ensure_domain_overrides(session, p, env, plan, "x100.dev")
        assert len(stub.ingress_pushes) == pushes
    run(body())


# ── 3. owned domain: NO per-host records (regression) ────────────────────────

def test_owned_domain_creates_no_override(monkeypatch):
    async def body():
        session = await make_session()
        p, env, _svc = await seed(session)
        stub = CFStub().install(monkeypatch)
        plan = {"app": {"public": True, "host": HOST, "port": 80, "label": ""}}

        # wildcard → our own tunnel
        stub.set_cname("*.x100.dev", OUR_TARGET)
        assert await deploy._ensure_domain_overrides(
            session, p, env, plan, "x100.dev") == ""
        assert stub.upserts == []
        assert await targetslib.load_dns_overrides(session) == {}
        assert stub.ingress_pushes == []

        # no wildcard at all (domain not routed yet): also unchanged
        stub.records.clear()
        assert await deploy._ensure_domain_overrides(
            session, p, env, plan, "x100.dev") == ""
        assert stub.upserts == []
        assert await targetslib.load_dns_overrides(session) == {}
    run(body())


def test_api_error_failsafe_changes_nothing(monkeypatch):
    async def body():
        session = await make_session()
        p, env, _svc = await seed(session)
        stub = CFStub().install(monkeypatch)
        # An existing override that a *definite* answer would have removed…
        await targetslib.save_dns_overrides(session, {HOST: {
            "domain": "x100.dev", "zone_id": "z1", "cname_target": OUR_TARGET,
            "proxied": True, "project": "site", "env": "dev", "service": "app",
            "created_at": "2026-07-18T00:00:00"}})
        stub.set_cname(HOST, OUR_TARGET, rec_id="r-ours")
        stub.fail_zones = True

        plan = {"app": {"public": True, "host": HOST, "port": 80, "label": ""}}
        tail = await deploy._ensure_domain_overrides(
            session, p, env, plan, "x100.dev")
        assert "WARNING" in tail and "ownership" in tail
        assert stub.upserts == [] and stub.deletes == []
        assert HOST in await targetslib.load_dns_overrides(session)
    run(body())


# ── 4. teardown / retarget deletes ONLY our record ────────────────────────────

def _meta(**over):
    base = {"domain": "x100.dev", "zone_id": "z1", "cname_target": OUR_TARGET,
            "proxied": True, "project": "site", "env": "dev", "service": "app",
            "created_at": "2026-07-18T00:00:00"}
    base.update(over)
    return base


def test_retarget_away_removes_only_our_record(monkeypatch):
    async def body():
        session = await make_session()
        p, env, _svc = await seed(session)
        stub = CFStub().install(monkeypatch)
        stub.set_cname("*.x100.dev", FOREIGN_TARGET)
        await targetslib.save_dns_overrides(session, {HOST: _meta()})
        # our record + an unrelated CNAME at the same name
        stub.records[HOST] = [
            {"id": "r-ours", "type": "CNAME", "name": HOST,
             "content": OUR_TARGET, "proxied": True},
            {"id": "r-other", "type": "CNAME", "name": HOST,
             "content": "elsewhere.example.com", "proxied": False},
        ]

        # service now homebox-targeted at ANOTHER cluster → plan says remote
        plan = {"app": {"public": True, "host": HOST, "port": 80, "label": "",
                        "target": "homebox", "remote": True}}
        tail = await deploy._ensure_domain_overrides(
            session, p, env, plan, "x100.dev")

        assert stub.deletes == ["r-ours"]                    # only OUR record
        assert [r["id"] for r in stub.records[HOST]] == ["r-other"]
        assert await targetslib.load_dns_overrides(session) == {}
        assert "removed" in tail
        # ingress re-pushed without the per-host rule
        assert {"hostname": HOST, "service": "http://traefik:80"} \
            not in stub.ingress_pushes[-1]
    run(body())


def test_domain_reowned_cleans_up_override(monkeypatch):
    async def body():
        session = await make_session()
        p, env, _svc = await seed(session)
        stub = CFStub().install(monkeypatch)
        stub.set_cname("*.x100.dev", OUR_TARGET)   # we own the wildcard now
        await targetslib.save_dns_overrides(session, {HOST: _meta()})
        stub.set_cname(HOST, OUR_TARGET, rec_id="r-ours")

        plan = {"app": {"public": True, "host": HOST, "port": 80, "label": ""}}
        await deploy._ensure_domain_overrides(session, p, env, plan, "x100.dev")
        assert stub.deletes == ["r-ours"]          # redundant override dropped
        assert await targetslib.load_dns_overrides(session) == {}
        assert stub.upserts == []                  # and no new per-host record
    run(body())


def test_cleanup_domain_overrides_scopes_to_env(monkeypatch):
    async def body():
        session = await make_session()
        stub = CFStub().install(monkeypatch)
        other = "other--prod.x100.dev"
        await targetslib.save_dns_overrides(session, {
            HOST: _meta(),
            other: _meta(env="prod", project="other", service="web"),
        })
        stub.set_cname(HOST, OUR_TARGET, rec_id="r-ours")
        stub.set_cname(other, OUR_TARGET, rec_id="r-keep")

        await deploy._cleanup_domain_overrides("site", "dev", session=session)

        assert stub.deletes == ["r-ours"]
        left = await targetslib.load_dns_overrides(session)
        assert set(left) == {other}                # other env untouched
        assert other in stub.records
    run(body())


# ── 5. drift report / repair ──────────────────────────────────────────────────

def test_dns_report_treats_override_as_expected(monkeypatch):
    async def body():
        session = await make_session()
        await seed(session)
        stub = CFStub().install(monkeypatch)
        from app.routes import tunnel as tunnel_routes

        await targetslib.save_dns_overrides(session, {HOST: _meta()})
        stub.set_cname("x100.dev", FOREIGN_TARGET)
        stub.set_cname("*.x100.dev", FOREIGN_TARGET)
        stub.set_cname(HOST, OUR_TARGET)
        stub.live["tun-a"] = [{"id": "conn-1"}]    # foreign tunnel is LIVE

        state = {"tunnel_id": OUR, "account_id": "acc-1"}
        report = await tunnel_routes._dns_report(state, session)
        by_host = {r["hostname"]: r for r in report["records"]}

        # override host: expected = OUR tunnel, correct record → ok
        assert by_host[HOST]["expected"] == OUR_TARGET
        assert by_host[HOST]["status"] == "ok"
        # foreign-owned apex/wildcard: informational, not drift
        assert by_host["*.x100.dev"]["expected"] == FOREIGN_TARGET
        assert by_host["*.x100.dev"]["status"] == "ok"
        assert by_host["*.x100.dev"]["foreign_cluster"] is True
        assert report["in_sync"] is True

        # a DRIFTED override is real drift
        stub.set_cname(HOST, FOREIGN_TARGET)
        report = await tunnel_routes._dns_report(state, session)
        by_host = {r["hostname"]: r for r in report["records"]}
        assert by_host[HOST]["status"] == "stale"
        assert report["in_sync"] is False
    run(body())


def test_resync_skips_foreign_wildcard_and_pins_override(monkeypatch):
    async def body():
        session = await make_session()
        await seed(session)
        stub = CFStub().install(monkeypatch)
        from app.routes import tunnel as tunnel_routes

        await targetslib.save_dns_overrides(session, {HOST: _meta()})
        stub.set_cname("x100.dev", FOREIGN_TARGET)
        stub.set_cname("*.x100.dev", FOREIGN_TARGET)
        stub.set_cname(HOST, FOREIGN_TARGET)       # override drifted
        stub.live["tun-a"] = [{"id": "conn-1"}]    # foreign tunnel is LIVE

        state = {"tunnel_id": OUR, "account_id": "acc-1"}
        result = await tunnel_routes._resync_dns(state, session)

        upserted = {h for h, _, _ in stub.upserts}
        assert "x100.dev" not in upserted          # live foreign wildcard kept
        assert "*.x100.dev" not in upserted
        assert any("another cluster" in (s.get("reason") or "")
                   for s in result["skipped"])
        assert (HOST, OUR_TARGET, True) in stub.upserts   # override re-pinned
        assert HOST in result["updated"]

        # already-correct override: second run writes nothing new for it
        stub.upserts.clear()
        result = await tunnel_routes._resync_dns(state, session)
        assert all(h != HOST for h, _, _ in stub.upserts)
    run(body())


def test_resync_still_repairs_dead_foreign_wildcard(monkeypatch):
    async def body():
        session = await make_session()
        await seed(session)
        stub = CFStub().install(monkeypatch)
        from app.routes import tunnel as tunnel_routes

        # Classic re-created-tunnel drift: wildcard → a DEAD tunnel.
        stub.set_cname("*.x100.dev", "tun-old.cfargotunnel.com")
        state = {"tunnel_id": OUR, "account_id": "acc-1"}
        await tunnel_routes._resync_dns(state, session)
        upserted = {h for h, _, _ in stub.upserts}
        assert {"x100.dev", "*.x100.dev"} <= upserted   # repaired as before
    run(body())


def test_push_ingress_includes_override_hosts(monkeypatch):
    async def body():
        session = await make_session()
        await seed(session)
        stub = CFStub().install(monkeypatch)
        from app.routes import tunnel as tunnel_routes

        state = {"tunnel_id": OUR, "account_id": "acc-1"}
        # no overrides → plain domain ingress
        await tunnel_routes._push_ingress(state, session)
        assert {"hostname": HOST, "service": "http://traefik:80"} \
            not in stub.ingress_pushes[-1]
        # with an override → per-host rule present
        await targetslib.save_dns_overrides(session, {HOST: _meta()})
        await tunnel_routes._push_ingress(state, session)
        assert {"hostname": HOST, "service": "http://traefik:80"} \
            in stub.ingress_pushes[-1]
        assert stub.ingress_pushes[-1][-1] == {"service": "http_status:404"}
    run(body())


# ── 6. bookkeeping round-trip ─────────────────────────────────────────────────

def test_dns_overrides_roundtrip():
    async def body():
        session = await make_session()
        assert await targetslib.load_dns_overrides(session) == {}
        data = {HOST: _meta()}
        await targetslib.save_dns_overrides(session, data)
        assert await targetslib.load_dns_overrides(session) == data
        data2 = {**data, "x.x100.dev": _meta(service="x")}
        await targetslib.save_dns_overrides(session, data2)
        assert await targetslib.load_dns_overrides(session) == data2
        await targetslib.save_dns_overrides(session, {})
        assert await targetslib.load_dns_overrides(session) == {}
    run(body())
