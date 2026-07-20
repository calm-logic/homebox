"""Tests for the Cloudflare Containers target (wrangler-driven; see the module
docstring for why there's no REST deploy path). Everything here runs without
wrangler, node, docker, or network.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.targets.base import TargetDeployCtx, TargetError  # noqa: E402
from app.targets.cloudflare_containers import (  # noqa: E402
    CfContainersTarget, CONTAINER_CLASS, _worker_name, render_project,
)


def run(coro):
    return asyncio.run(coro)


def _ctx(tmp_path, **over):
    kw = dict(project_name="listless", env_name="dev", service_name="api",
              kind="api", rd=tmp_path, hostname="listless-api--dev.calmlogic.dev",
              env_vars={"DATABASE_URL": "postgresql://u:p@10.77.0.1:5432/db"},
              internal_port=3000, config={}, state={})
    kw.update(over)
    return TargetDeployCtx(**kw)


def _target(transport=None):
    return CfContainersTarget(
        creds={"token": "tok", "account_id": "acc-1", "config": {}},
        config={}, state={}, transport=transport,
    )


# ── pure generation ──────────────────────────────────────────────────────────

def test_worker_name_sanitization(tmp_path):
    ctx = _ctx(tmp_path, project_name="My_App!", env_name="dev",
               service_name="api" + "x" * 80)
    name = _worker_name(ctx)
    assert len(name) <= 63
    assert name == name.lower()
    assert "--" not in name and not name.startswith("-") and not name.endswith("-")
    assert all(c.isalnum() or c == "-" for c in name)


def test_render_project_shape(tmp_path):
    ctx = _ctx(tmp_path)
    cfg_json, worker = render_project(ctx, "/repo/Dockerfile")
    cfg = json.loads(cfg_json)
    assert cfg["containers"][0]["image"] == "/repo/Dockerfile"
    assert cfg["containers"][0]["class_name"] == CONTAINER_CLASS
    assert cfg["durable_objects"]["bindings"][0]["class_name"] == CONTAINER_CLASS
    assert cfg["migrations"][0]["new_sqlite_classes"] == [CONTAINER_CLASS]
    assert cfg["workers_dev"] is True
    # worker proxies the service's port and bakes env vars into container start
    assert "getTcpPort(3000)" in worker
    assert '"DATABASE_URL"' in worker
    assert "postgresql://u:p@10.77.0.1:5432/db" in worker


# ── deploy guards ────────────────────────────────────────────────────────────

def test_deploy_requires_npx(tmp_path, monkeypatch):
    import app.targets.cloudflare_containers as mod
    monkeypatch.setattr(mod.shutil, "which", lambda _: None)
    (tmp_path / "Dockerfile").write_text("FROM scratch")
    with pytest.raises(TargetError, match="npx"):
        run(_target().deploy(_ctx(tmp_path)))


def test_deploy_requires_dockerfile(tmp_path, monkeypatch):
    import app.targets.cloudflare_containers as mod
    monkeypatch.setattr(mod.shutil, "which", lambda _: "/usr/bin/npx")
    with pytest.raises(TargetError, match="Dockerfile"):
        run(_target().deploy(_ctx(tmp_path)))   # no Dockerfile in rd


# ── REST destroy/probe/validate ──────────────────────────────────────────────

def _mock(handler):
    return httpx.MockTransport(handler)


def test_destroy_tolerates_404():
    def handler(request):
        assert request.method == "DELETE"
        assert "/workers/services/hb-api" in str(request.url)
        return httpx.Response(404, json={"success": False, "errors": [
            {"code": 10007, "message": "not found"}]})
    t = _target(transport=_mock(handler))
    run(t.destroy({"worker_name": "hb-api", "account_id": "acc-1"}))  # no raise


def test_destroy_noop_without_state():
    t = _target(transport=_mock(lambda r: httpx.Response(500)))
    run(t.destroy({}))  # never calls out


def test_probe_paths():
    def up(request):
        return httpx.Response(200, json={"success": True, "result": {}})
    def down(request):
        return httpx.Response(404, json={"success": False, "errors": []})
    assert run(_target(transport=_mock(up)).probe({"worker_name": "x"})) is True
    assert run(_target(transport=_mock(down)).probe({"worker_name": "x"})) is False
    assert run(_target(transport=_mock(up)).probe({})) is False


def test_validate_auth_error_mentions_scope():
    def handler(request):
        return httpx.Response(403, json={"success": False, "errors": [
            {"code": 10000, "message": "Authentication error"}]})
    with pytest.raises(TargetError, match="Workers Scripts"):
        run(_target(transport=_mock(handler)).validate())
