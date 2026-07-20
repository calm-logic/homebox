"""Cloudflare Containers target — stateless web/api containers on Cloudflare's
Workers-attached container runtime (variant "cf_containers").

Cloudflare publishes NO headless REST API for Containers (verified 2026-07-15:
the documented path is wrangler, which builds the image, pushes it to the
account's managed registry, and uploads a supervisor Worker whose Durable
Object class starts/routes to container instances). So this target drives
`wrangler deploy` non-interactively: we generate a scratch project (supervisor
Worker JS + wrangler config) next to the service's build context and run
wrangler with CLOUDFLARE_API_TOKEN/CLOUDFLARE_ACCOUNT_ID. Requirements and v1
limits, surfaced as precise TargetErrors:

  - node/npx must be available in the admin runtime (wrangler is fetched via
    `npx wrangler@4`). The admin image gains this in the polish phase; until
    then the error says exactly what's missing.
  - the service must be dockerfile- or image-origin (wrangler builds from a
    Dockerfile; there's no nixpacks hook).
  - v1 routes via workers.dev (endpoint fallback, like Cloud Run's run.app) —
    Workers custom domains manage their own DNS records and would fight the
    drift-repair model; revisit with the polish phase.

Destroy/probe use the documented Workers Scripts REST API (no wrangler
needed): DELETE/GET /accounts/{a}/workers/services/{name}.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import httpx

from ..cloudflare import API, CloudflareError, _headers, _unwrap
from .base import DeployTarget, TargetDeployCtx, TargetError, TargetResult

CONTAINER_CLASS = "HomeboxContainer"
COMPAT_DATE = "2026-01-01"

# Supervisor Worker: every request starts (if needed) and proxies to one
# container instance. Plain runtime APIs only — no npm dependencies, so
# wrangler can bundle it standalone.
WORKER_JS = """\
import {{ DurableObject }} from "cloudflare:workers";

export class {cls} extends DurableObject {{
  async fetch(request) {{
    if (!this.ctx.container) {{
      return new Response("container runtime unavailable", {{ status: 503 }});
    }}
    if (!this.ctx.container.running) {{
      this.ctx.container.start({{ env: {env_json} }});
    }}
    return this.ctx.container.getTcpPort({port}).fetch(request);
  }}
}}

export default {{
  async fetch(request, env) {{
    const id = env.CONTAINER.idFromName("singleton");
    return env.CONTAINER.get(id).fetch(request);
  }}
}};
"""


def _worker_name(ctx: TargetDeployCtx) -> str:
    """Workers service names: lowercase alphanumeric + dashes, ≤63."""
    name = "".join(c if c.isalnum() or c == "-" else "-"
                   for c in ctx.resource_name.lower())
    while "--" in name:
        name = name.replace("--", "-")
    return name.strip("-")[:63].rstrip("-")


def render_project(ctx: TargetDeployCtx, dockerfile: str) -> tuple[str, str]:
    """(wrangler_config_json, worker_js) for this service. Pure — tested
    without wrangler."""
    name = _worker_name(ctx)
    port = ctx.internal_port or 8080
    config = {
        "name": name,
        "main": "worker.js",
        "compatibility_date": COMPAT_DATE,
        "containers": [{
            "class_name": CONTAINER_CLASS,
            "image": dockerfile,
            "max_instances": int(ctx.config.get("max_instances", 1)),
        }],
        "durable_objects": {
            "bindings": [{"name": "CONTAINER", "class_name": CONTAINER_CLASS}],
        },
        "migrations": [{"tag": "v1", "new_sqlite_classes": [CONTAINER_CLASS]}],
        "workers_dev": True,
    }
    worker = WORKER_JS.format(
        cls=CONTAINER_CLASS, port=port,
        env_json=json.dumps(ctx.env_vars or {}),
    )
    return json.dumps(config, indent=2), worker


class CfContainersTarget(DeployTarget):
    provider = "cloudflare"
    variant = "cf_containers"

    def __init__(self, *, creds: dict[str, Any], config: dict[str, Any],
                 state: dict[str, Any], transport=None) -> None:
        self.token = creds.get("token") or ""
        self.account_id = creds.get("account_id") or ""
        self.config = config or {}
        self.state = state or {}
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=30, transport=self._transport)

    async def validate(self) -> None:
        if not self.token or not self.account_id:
            raise TargetError("Cloudflare is not connected (token/account missing).")
        async with self._client() as c:
            r = await c.get(f"{API}/accounts/{self.account_id}/workers/services",
                            headers=_headers(self.token))
        if r.status_code in (401, 403):
            raise TargetError(
                "Your Cloudflare token can't manage Workers — re-scope it with "
                "'Workers Scripts: Edit' to use the Containers target.")

    async def deploy(self, ctx: TargetDeployCtx) -> TargetResult:
        from ..deploy import _run

        if shutil.which("npx") is None:
            raise TargetError(
                "The Cloudflare Containers target needs node/npx (for wrangler) "
                "in the Homebox admin image — it isn't installed yet. This lands "
                "with the polish phase; until then use Cloud Run or App Runner "
                "for this service.")

        # wrangler builds from a Dockerfile; find the service's.
        build_dir = ctx.rd / (ctx.config.get("build_dir") or ".")
        dockerfile = ctx.config.get("dockerfile") or ctx.state.get("dockerfile")
        if not dockerfile:
            # ctx carries the detected service via config in the orchestrator;
            # fall back to a conventional Dockerfile in the build context.
            candidate = build_dir / "Dockerfile"
            if candidate.exists():
                dockerfile = "Dockerfile"
        if not dockerfile or not (build_dir / dockerfile).exists():
            raise TargetError(
                "Cloudflare Containers needs a Dockerfile (wrangler builds the "
                "image itself — nixpacks-built services can't target it yet). "
                "Add a Dockerfile or pick Cloud Run/App Runner.")

        name = _worker_name(ctx)
        proj = ctx.rd / ".homebox" / f"cf-containers-{ctx.service_name}"
        proj.mkdir(parents=True, exist_ok=True)
        cfg_json, worker_js = render_project(ctx, str((build_dir / dockerfile).resolve()))
        (proj / "wrangler.json").write_text(cfg_json)
        (proj / "worker.js").write_text(worker_js)

        await ctx.emit(f"[cf-containers] wrangler deploy {name} …")
        import os
        env = {**os.environ,
               "CLOUDFLARE_API_TOKEN": self.token,
               "CLOUDFLARE_ACCOUNT_ID": self.account_id,
               "WRANGLER_SEND_METRICS": "false",
               "CI": "true"}
        code, out = await _run(
            ["npx", "--yes", "wrangler@4", "deploy",
             "--config", str(proj / "wrangler.json")],
            timeout=1800, env=env,
        )
        if code:
            raise TargetError(f"wrangler deploy failed:\n{out[-2000:]}")

        endpoint = await self._workers_dev_host(name)
        return TargetResult(
            endpoint=endpoint,
            cname_target=None,   # workers.dev in v1 — no DNS record of ours
            proxied=True,
            state={"worker_name": name, "account_id": self.account_id,
                   "endpoint": endpoint},
        )

    async def _workers_dev_host(self, name: str) -> str:
        async with self._client() as c:
            r = await c.get(f"{API}/accounts/{self.account_id}/workers/subdomain",
                            headers=_headers(self.token))
        try:
            sub = (_unwrap(r) or {}).get("subdomain") or ""
        except CloudflareError:
            sub = ""
        return f"{name}.{sub}.workers.dev" if sub else f"{name}.workers.dev"

    async def destroy(self, state: dict[str, Any]) -> None:
        name = state.get("worker_name")
        if not name:
            return
        account = state.get("account_id") or self.account_id
        async with self._client() as c:
            r = await c.delete(
                f"{API}/accounts/{account}/workers/services/{name}",
                headers=_headers(self.token), params={"force": "true"},
            )
        if r.status_code not in (200, 404):
            try:
                _unwrap(r)
            except CloudflareError as e:
                raise TargetError(f"could not delete worker {name}: {e}") from e

    async def probe(self, state: dict[str, Any]) -> bool:
        name = state.get("worker_name")
        if not name:
            return False
        account = state.get("account_id") or self.account_id
        async with self._client() as c:
            r = await c.get(
                f"{API}/accounts/{account}/workers/services/{name}",
                headers=_headers(self.token),
            )
        return r.status_code == 200
