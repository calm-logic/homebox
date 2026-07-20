"""Tests for the serverless → homebox DB path (phase 4 of cloud targets):

  tunnel TCP ingress   — cf.build_ingress tcp_rules prepended, catch-all last,
                         and the _push_ingress/_push_remote_ingress call sites
                         passing targetslib.all_tunnel_tcp_rules through;
  Access service token — created once, secret encrypted into state, cached;
  Access TCP app       — idempotent create (matched by domain, policy by name);
  plan derivation      — targetslib.serverless_db_plan and its persisted-state
                         twin all_tunnel_tcp_rules agree via db_tunnel_rule;
  wrapper image        — artifacts.render_wrapper / wrap_with_access_proxy.

Runs on in-memory sqlite (aiosqlite); HTTP via httpx.MockTransport, docker via
a monkeypatched deploy._run — no network, no docker daemon.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.db import Base  # noqa: E402
from app import cloudflare as cf  # noqa: E402
from app import targetslib, urls  # noqa: E402
from app.crypto import decrypt  # noqa: E402
from app.models import (  # noqa: E402
    Domain, Environment, Project, Service, ServiceEnvVar, ServiceTarget,
)
from app.targets import artifacts  # noqa: E402
from app.targets.base import ProxyRule, TargetError  # noqa: E402

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


async def seed_serverless(session):
    """A managed project whose web service targets GCP (variant cloud_run)
    with an auto-wired DATABASE_URL pointing at a homebox postgres sibling."""
    session.add(Domain(name="calmlogic.dev", is_primary=True))
    p = Project(repo_full_name="al/app", name="app", managed=True)
    session.add(p)
    await session.flush()
    env = Environment(project_id=p.id, name="production", kind="production")
    web = Service(project_id=p.id, name="web", kind="web", is_public=True)
    pg = Service(project_id=p.id, name="postgres", kind="database")
    cache = Service(project_id=p.id, name="cache", kind="cache")
    session.add_all([env, web, pg, cache])
    await session.flush()
    session.add(ServiceTarget(service_id=web.id, target="gcp", config={}))
    session.add(ServiceEnvVar(
        service_id=web.id, environment_id=None, key="DATABASE_URL",
        value="postgresql://u:p@postgres:5432/app", source="auto"))
    await session.commit()
    return p, env, web, pg, cache


def _expected_pg_rule(p, env):
    return targetslib.db_tunnel_rule(
        p.name, env.name, "postgres", "database", "calmlogic.dev",
        urls.stack_name(p, env))


# ── cf.build_ingress ─────────────────────────────────────────────────────────

def test_build_ingress_tcp_rules_precede_domains():
    tcp = [
        {"hostname": "db-app-postgres-production.calmlogic.dev",
         "service": "tcp://homebox-proj-app-production-postgres-1:5432"},
        # duplicate hostname must be deduped, first wins
        {"hostname": "db-app-postgres-production.calmlogic.dev",
         "service": "tcp://dup:1"},
    ]
    rules = cf.build_ingress([{"name": "calmlogic.dev"}], tcp_rules=tcp)
    assert rules[0] == tcp[0]
    assert [r["hostname"] for r in rules[1:3]] == \
        ["calmlogic.dev", "*.calmlogic.dev"]
    assert all(r["service"] == "http://traefik:80" for r in rules[1:3])
    assert rules[-1] == {"service": "http_status:404"}
    assert len(rules) == 4


def test_build_ingress_backward_compatible_without_tcp_rules():
    base = cf.build_ingress([{"name": "calmlogic.dev"}])
    assert base == cf.build_ingress([{"name": "calmlogic.dev"}], tcp_rules=None)
    assert base == cf.build_ingress([{"name": "calmlogic.dev"}], tcp_rules=[])
    assert base == [
        {"hostname": "calmlogic.dev", "service": "http://traefik:80"},
        {"hostname": "*.calmlogic.dev", "service": "http://traefik:80"},
        {"service": "http_status:404"},
    ]


# ── Access service token ─────────────────────────────────────────────────────

_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _patch_http(monkeypatch, handler):
    """Route every httpx.AsyncClient the cloudflare module opens through a
    MockTransport (the module builds clients inline, no transport injection)."""
    transport = httpx.MockTransport(handler)
    def client(*args, **kwargs):
        return _REAL_ASYNC_CLIENT(transport=transport)
    monkeypatch.setattr(cf.httpx, "AsyncClient", client)


def _no_http(request):
    raise AssertionError(f"unexpected HTTP call: {request.method} {request.url}")


def test_access_service_token_created_once_and_cached(monkeypatch):
    async def body():
        session = await make_session()
        calls = []

        def handler(request):
            calls.append(request)
            assert request.method == "POST"
            assert request.url.path == \
                "/client/v4/accounts/acc-1/access/service_tokens"
            payload = json.loads(request.content)
            assert payload == {"name": "homebox-db-access", "duration": "8760h"}
            return httpx.Response(200, json={"success": True, "result": {
                "id": "svctok-row-1", "client_id": "cid.access",
                "client_secret": "s3cret"}})

        _patch_http(monkeypatch, handler)
        state = {"account_id": "acc-1"}
        cf.store_token(state, "api-token")

        cid, secret = await cf.ensure_access_service_token(session, state)
        assert (cid, secret) == ("cid.access", "s3cret")
        assert len(calls) == 1

        # persisted (encrypted) through save_state → load_state round trip
        persisted = await cf.load_state(session)
        blob = persisted["db_access_token"]
        assert blob["token_id"] == "svctok-row-1"
        assert blob["client_id"] == "cid.access"
        assert blob["client_secret_encrypted"] != "s3cret"
        assert decrypt(blob["client_secret_encrypted"]) == "s3cret"

        # second call: cached pair returned, zero HTTP
        _patch_http(monkeypatch, _no_http)
        cid2, secret2 = await cf.ensure_access_service_token(session, persisted)
        assert (cid2, secret2) == ("cid.access", "s3cret")
    run(body())


# ── Access TCP app idempotency ───────────────────────────────────────────────

def _ok(result):
    return httpx.Response(200, json={"success": True, "result": result})


def _access_router(apps, policies, posts):
    def handler(request):
        path = request.url.path
        if request.method == "GET" and path.endswith("/access/apps"):
            return _ok(list(apps))
        if request.method == "POST" and path.endswith("/access/apps"):
            posts.append("app")
            payload = json.loads(request.content)
            assert payload["type"] == "self_hosted"
            app = {"id": "app-new", "domain": payload["domain"]}
            apps.append(app)
            return _ok(app)
        if request.method == "GET" and path.endswith("/policies"):
            return _ok(list(policies))
        if request.method == "POST" and path.endswith("/policies"):
            posts.append("policy")
            payload = json.loads(request.content)
            assert payload["decision"] == "non_identity"
            assert payload["include"] == \
                [{"service_token": {"token_id": "svctok-row-1"}}]
            policies.append({"name": payload["name"]})
            return _ok({})
        raise AssertionError(f"unexpected: {request.method} {path}")
    return handler


def test_access_tcp_app_existing_reused(monkeypatch):
    async def body():
        posts = []
        # matched by domain (case-insensitively), policy already present
        _patch_http(monkeypatch, _access_router(
            apps=[{"id": "app-1", "domain": "DB-App-Postgres.Calmlogic.dev"}],
            policies=[{"name": "homebox-db-token"}], posts=posts))
        app_id = await cf.ensure_access_tcp_app(
            "tok", "acc-1", "db-app-postgres.calmlogic.dev", "svctok-row-1")
        assert app_id == "app-1"
        assert posts == []           # nothing created
    run(body())


def test_access_tcp_app_created_when_missing(monkeypatch):
    async def body():
        posts = []
        _patch_http(monkeypatch, _access_router(apps=[], policies=[], posts=posts))
        app_id = await cf.ensure_access_tcp_app(
            "tok", "acc-1", "db-app-postgres.calmlogic.dev", "svctok-row-1")
        assert app_id == "app-new"
        assert posts == ["app", "policy"]
    run(body())


def test_access_tcp_app_policy_added_only_when_missing(monkeypatch):
    async def body():
        posts = []
        _patch_http(monkeypatch, _access_router(
            apps=[{"id": "app-1", "domain": "db-x.calmlogic.dev"}],
            policies=[], posts=posts))
        app_id = await cf.ensure_access_tcp_app(
            "tok", "acc-1", "db-x.calmlogic.dev", "svctok-row-1")
        assert app_id == "app-1"
        assert posts == ["policy"]   # app reused, policy created
    run(body())


# ── serverless_db_plan ───────────────────────────────────────────────────────

def _detected_map():
    return {
        "web": SimpleNamespace(name="web", kind="web", auto_env={
            "DATABASE_URL": "postgresql://u:p@postgres:5432/app",
            "REDIS_URL": "redis://cache:6379/0",
        }),
        "postgres": SimpleNamespace(name="postgres", kind="database", auto_env={}),
        "cache": SimpleNamespace(name="cache", kind="cache", auto_env={}),
    }


def test_serverless_db_plan_cloud_run_consumer():
    async def body():
        session = await make_session()
        p, env, *_ = await seed_serverless(session)
        targets_map = await targetslib.effective_targets(session, p, env)
        assert targets_map["web"].variant == "cloud_run"

        plan = await targetslib.serverless_db_plan(
            session, p, env, targets_map, _detected_map(), "calmlogic.dev")

        stack = urls.stack_name(p, env)
        pg_rule = _expected_pg_rule(p, env)
        cache_rule = targetslib.db_tunnel_rule(
            p.name, env.name, "cache", "cache", "calmlogic.dev", stack)
        assert pg_rule["service"] == f"tcp://{stack}-postgres-1:5432"
        assert cache_rule["service"] == f"tcp://{stack}-cache-1:6379"
        assert plan["tcp_rules"] == [pg_rule, cache_rule]

        # deterministic local ports: name-sorted producers from BASE_PORT
        base = targetslib.DB_PROXY_BASE_PORT
        assert plan["proxy_rules"]["web"] == [
            ProxyRule(hostname=pg_rule["hostname"], local_port=base + 1),
            ProxyRule(hostname=cache_rule["hostname"], local_port=base),
        ]
        assert plan["env_overrides"]["web"] == {
            "DATABASE_URL": f"postgresql://u:p@127.0.0.1:{base + 1}/app",
            "REDIS_URL": f"redis://127.0.0.1:{base}/0",
        }
    run(body())


def test_serverless_db_plan_homebox_consumer_is_empty():
    async def body():
        session = await make_session()
        p, env, web, *_ = await seed_serverless(session)
        # retarget the consumer back to homebox → no serverless path needed
        st = (await session.get(ServiceTarget, 1))
        st.target = "homebox"
        await session.commit()
        targets_map = await targetslib.effective_targets(session, p, env)
        plan = await targetslib.serverless_db_plan(
            session, p, env, targets_map, _detected_map(), "calmlogic.dev")
        assert plan == {"proxy_rules": {}, "env_overrides": {}, "tcp_rules": []}
    run(body())


def test_serverless_db_plan_excludes_cloud_vm_producer():
    async def body():
        session = await make_session()
        p, env, web, pg, cache = await seed_serverless(session)
        # postgres moves to an AWS DB VM → reached via mesh, NOT the tunnel
        session.add(ServiceTarget(service_id=pg.id, target="aws", config={}))
        await session.commit()
        targets_map = await targetslib.effective_targets(session, p, env)
        plan = await targetslib.serverless_db_plan(
            session, p, env, targets_map, _detected_map(), "calmlogic.dev")
        hostnames = [r["hostname"] for r in plan["tcp_rules"]]
        assert hostnames == [targetslib.db_tunnel_rule(
            p.name, env.name, "cache", "cache", "calmlogic.dev",
            urls.stack_name(p, env))["hostname"]]
        assert "DATABASE_URL" not in plan["env_overrides"].get("web", {})
        assert [r.hostname for r in plan["proxy_rules"]["web"]] == hostnames
    run(body())


# ── all_tunnel_tcp_rules (persisted-state derivation) ────────────────────────

def test_all_tunnel_tcp_rules_matches_db_tunnel_rule():
    async def body():
        session = await make_session()
        p, env, *_ = await seed_serverless(session)
        rules = await targetslib.all_tunnel_tcp_rules(session)
        assert rules == [_expected_pg_rule(p, env)]
    run(body())


def test_all_tunnel_tcp_rules_empty_without_serverless_targets():
    async def body():
        session = await make_session()
        session.add(Domain(name="calmlogic.dev", is_primary=True))
        await session.commit()
        assert await targetslib.all_tunnel_tcp_rules(session) == []
    run(body())


# ── render_wrapper ───────────────────────────────────────────────────────────

def test_render_wrapper_pins_cloudflared_binary():
    df, _ = artifacts.render_wrapper(["/entry"], None,
                                     [ProxyRule("db-x.calmlogic.dev", 15432)])
    sha = artifacts.CLOUDFLARED_SHA256["amd64"]
    url = artifacts.CLOUDFLARED_URL.format(
        version=artifacts.CLOUDFLARED_VERSION, arch="amd64")
    assert artifacts.CLOUDFLARED_VERSION in df
    assert f"ADD --checksum=sha256:{sha} {url} /usr/local/bin/cloudflared" in df
    assert "chmod +x /usr/local/bin/cloudflared" in df
    assert 'ENTRYPOINT ["/homebox-entrypoint.sh"]' in df
    assert "ARG BASE_IMAGE" in df and "FROM ${BASE_IMAGE}" in df


def test_render_wrapper_one_proxy_line_per_rule():
    rules = [ProxyRule("db-a.calmlogic.dev", 15432),
             ProxyRule("db-b.calmlogic.dev", 15433)]
    _, sh = artifacts.render_wrapper(
        ["/docker-entrypoint.sh"], ["postgres", "-c", "opt with space"], rules)
    lines = sh.splitlines()
    proxies = [l for l in lines if l.startswith("cloudflared access tcp")]
    assert len(proxies) == 2
    assert "--hostname db-a.calmlogic.dev --url 127.0.0.1:15432" in proxies[0]
    assert "--hostname db-b.calmlogic.dev --url 127.0.0.1:15433" in proxies[1]
    for line in proxies:
        assert '--service-token-id "$TUNNEL_SERVICE_TOKEN_ID"' in line
        assert '--service-token-secret "$TUNNEL_SERVICE_TOKEN_SECRET"' in line
        assert line.endswith("&")
    # exec line: entrypoint + cmd composed, shell-quoted, LAST
    assert lines[-1] == \
        "exec /docker-entrypoint.sh postgres -c 'opt with space'"


def test_render_wrapper_entrypoint_or_cmd_only():
    rules = [ProxyRule("db.calmlogic.dev", 15432)]
    _, sh = artifacts.render_wrapper(["/entry", "--flag"], None, rules)
    assert sh.splitlines()[-1] == "exec /entry --flag"
    _, sh = artifacts.render_wrapper(None, ["node", "server.js"], rules)
    assert sh.splitlines()[-1] == "exec node server.js"


def test_render_wrapper_requires_some_command():
    rules = [ProxyRule("db.calmlogic.dev", 15432)]
    with pytest.raises(TargetError):
        artifacts.render_wrapper(None, None, rules)
    with pytest.raises(TargetError):
        artifacts.render_wrapper([], [], rules)


# ── wrap_with_access_proxy (docker faked) ────────────────────────────────────

def test_wrap_with_access_proxy_builds_wrapper(monkeypatch, tmp_path):
    async def body():
        from app import deploy
        calls = []

        async def fake_run(cmd, cwd=None, env=None, timeout=None):
            calls.append(cmd)
            if cmd[:2] == ["docker", "inspect"]:
                return 0, '["docker-entrypoint.sh"] ["postgres"]\n'
            if cmd[:2] == ["docker", "build"]:
                return 0, "built"
            raise AssertionError(cmd)

        monkeypatch.setattr(deploy, "_run", fake_run)
        tag = await artifacts.wrap_with_access_proxy(
            "myimg:latest", [ProxyRule("db-x.calmlogic.dev", 15432)],
            "App", "Dev", "Web", tmp_path)
        assert tag == "homebox-wrapped-app-dev-web:latest"

        # files written from render_wrapper output
        df = (tmp_path / "Dockerfile.homebox-wrapper").read_text()
        sh = (tmp_path / "homebox-entrypoint.sh").read_text()
        assert "ARG BASE_IMAGE" in df
        assert sh.rstrip().endswith("exec docker-entrypoint.sh postgres")
        assert "--hostname db-x.calmlogic.dev --url 127.0.0.1:15432" in sh

        build = calls[1]
        assert build[:2] == ["docker", "build"]
        assert "BASE_IMAGE=myimg:latest" in build
        assert tag in build
    run(body())


def test_wrap_with_access_proxy_error_paths(monkeypatch, tmp_path):
    async def body():
        from app import deploy

        async def no_command(cmd, cwd=None, env=None, timeout=None):
            return 0, "null null\n"   # image with neither ENTRYPOINT nor CMD

        monkeypatch.setattr(deploy, "_run", no_command)
        with pytest.raises(TargetError, match="ENTRYPOINT nor CMD"):
            await artifacts.wrap_with_access_proxy(
                "img", [ProxyRule("db.x", 15432)], "p", "e", "s", tmp_path)

        async def inspect_fails(cmd, cwd=None, env=None, timeout=None):
            return 1, "No such image"

        monkeypatch.setattr(deploy, "_run", inspect_fails)
        with pytest.raises(TargetError, match="inspect"):
            await artifacts.wrap_with_access_proxy(
                "img", [ProxyRule("db.x", 15432)], "p", "e", "s", tmp_path)
    run(body())


# ── ingress push call sites pass tcp rules through ───────────────────────────

class FakeCFModule:
    """Stands in for `cf` inside the route modules; delegates build_ingress to
    the real implementation so the pushed config is the genuine article."""

    def __init__(self):
        self.state = {"account_id": "acc-1", "tunnel_id": "tun-1"}
        self.build_calls: list[tuple[list, list]] = []
        self.put_calls: list[list] = []

    def get_token(self, state):
        return "tok"

    async def load_state(self, session):
        return dict(self.state)

    def build_ingress(self, domains, service_url="http://traefik:80", tcp_rules=None):
        self.build_calls.append((list(domains), list(tcp_rules or [])))
        return cf.build_ingress(domains, service_url, tcp_rules)

    async def put_tunnel_config(self, token, account_id, tunnel_id, ingress):
        self.put_calls.append(ingress)


def test_push_ingress_passes_tcp_rules(monkeypatch):
    async def body():
        session = await make_session()
        p, env, *_ = await seed_serverless(session)
        expected = _expected_pg_rule(p, env)

        from app.routes import tunnel as tunnel_routes
        fake = FakeCFModule()
        monkeypatch.setattr(tunnel_routes, "cf", fake)
        await tunnel_routes._push_ingress(dict(fake.state), session)

        assert len(fake.build_calls) == 1
        domains_arg, tcp_arg = fake.build_calls[0]
        assert domains_arg == [{"name": "calmlogic.dev"}]
        assert tcp_arg == [expected]
        assert fake.put_calls[0][0] == expected          # tcp rule first
        assert fake.put_calls[0][-1] == {"service": "http_status:404"}
    run(body())


def test_push_ingress_unchanged_without_serverless_targets(monkeypatch):
    async def body():
        session = await make_session()
        session.add(Domain(name="calmlogic.dev", is_primary=True))
        await session.commit()

        from app.routes import tunnel as tunnel_routes
        fake = FakeCFModule()
        monkeypatch.setattr(tunnel_routes, "cf", fake)
        await tunnel_routes._push_ingress(dict(fake.state), session)

        assert fake.build_calls[0][1] == []              # no tcp rules
        assert fake.put_calls[0] == cf.build_ingress([{"name": "calmlogic.dev"}])
    run(body())


def test_push_remote_ingress_passes_tcp_rules(monkeypatch):
    async def body():
        session = await make_session()
        p, env, *_ = await seed_serverless(session)
        expected = _expected_pg_rule(p, env)

        from app.routes import domains as domains_routes
        fake = FakeCFModule()
        monkeypatch.setattr(domains_routes, "cf", fake)
        await domains_routes._push_remote_ingress(session)

        assert fake.build_calls[0][1] == [expected]
        assert fake.put_calls[0][0] == expected
        assert fake.put_calls[0][-1] == {"service": "http_status:404"}
    run(body())
