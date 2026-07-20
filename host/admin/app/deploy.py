"""Project deploy engine (multi-service, multi-environment).

A deploy targets a (project, environment). Each environment runs as its own
docker-compose stack on the shared `traefik-net` network, named
`homebox-proj-<project>-<env>` so production and dev coexist with separate
volumes. Public services are routed by Traefik to their derived hostnames
(app/urls.py); backing services (db, cache, …) stay internal to the stack.

Build source, in priority order (per repo):
  1. compose file  → transform it (host ports stripped, traefik-net + per-public-
                     service labels added, env vars injected)
  2. Dockerfile    → generate a one-service compose that builds it
  3. neither       → Nixpacks infers + builds an image, wrapped in a compose

The repo structure is re-read on every deploy via app/dissect.py, so routing +
auto-wired connection env vars always match the current commit; user-set env
vars (ServiceEnvVar source='user') are layered on top.
"""

import asyncio
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import yaml
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from .config import settings
from .db import SessionLocal
from . import dissect, github as gh, urls
from .compose_utils import find_compose
from .integrations_lib import decrypted_token
from .models import (
    Deployment, Domain, Environment, Project, Service, ServiceEnvVar, ServiceInstance,
)

log = logging.getLogger("homebox.deploy")

GENERATED_COMPOSE = "docker-compose.homebox.yml"
TRAEFIK_NET = "traefik-net"
_BUILD_TIMEOUT = 1800  # 30 min

# One lock per stack so a manual deploy and a webhook deploy can't `compose up`
# the same stack concurrently.
_locks: dict[str, asyncio.Lock] = {}


class DeployError(Exception):
    """A deploy step failed; the message is surfaced to the UI."""


def _lock_for(stack: str) -> asyncio.Lock:
    lock = _locks.get(stack)
    if lock is None:
        lock = _locks[stack] = asyncio.Lock()
    return lock


def env_dir(project_name: str, env_name: str) -> Path:
    return settings.projects_host_dir / project_name / env_name


def repo_dir(project_name: str, env_name: str) -> Path:
    return env_dir(project_name, env_name) / "repo"


def _scrub(text: str, secret: str) -> str:
    return text.replace(secret, "***") if secret else text


async def _run(cmd: list[str], cwd: str | None = None,
               env: dict[str, str] | None = None, timeout: int = _BUILD_TIMEOUT) -> tuple[int, str]:
    """Run a subprocess off the event loop. Returns (returncode, combined output)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, f"command timed out after {timeout}s: {' '.join(cmd[:2])}"
    return proc.returncode or 0, out.decode("utf-8", "replace")


async def _run_streaming(
    cmd: list[str], cwd: str | None, on_output, timeout: int = _BUILD_TIMEOUT,
) -> tuple[int, str]:
    """Like _run, but calls `on_output(text_so_far)` after each output line so
    long steps (docker build) can surface live logs."""
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    chunks: list[str] = []
    deadline = asyncio.get_event_loop().time() + timeout
    try:
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise asyncio.TimeoutError
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
            if not line:
                break
            chunks.append(line.decode("utf-8", "replace"))
            await on_output("".join(chunks))
        await proc.wait()
        return proc.returncode or 0, "".join(chunks)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "".join(chunks) + f"\ncommand timed out after {timeout}s"


def _git_env() -> dict[str, str]:
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0"}


async def sync_source(project: Project, env: Environment, token: str | None) -> str:
    """Clone (or fetch+reset) the env's branch into repo_dir. Returns HEAD sha.
    token=None → anonymous https clone (public repos added by URL)."""
    rd = repo_dir(project.name, env.name)
    url = (f"https://x-access-token:{token}@github.com/{project.repo_full_name}.git"
           if token else f"https://github.com/{project.repo_full_name}.git")
    branch = env.branch or project.default_branch or "main"

    if (rd / ".git").is_dir():
        await _run(["git", "-C", str(rd), "remote", "set-url", "origin", url], env=_git_env())
        code, out = await _run(["git", "-C", str(rd), "fetch", "--depth", "1", "origin", branch], env=_git_env())
        if code:
            raise DeployError(f"git fetch failed:\n{_scrub(out, token)}")
        code, out = await _run(["git", "-C", str(rd), "reset", "--hard", "FETCH_HEAD"], env=_git_env())
        if code:
            raise DeployError(f"git reset failed:\n{_scrub(out, token)}")
    else:
        rd.parent.mkdir(parents=True, exist_ok=True)
        code, out = await _run(
            ["git", "clone", "--depth", "1", "--branch", branch, url, str(rd)], env=_git_env()
        )
        if code:
            raise DeployError(f"git clone failed:\n{_scrub(out, token)}")

    code, sha = await _run(["git", "-C", str(rd), "rev-parse", "HEAD"], env=_git_env())
    return sha.strip() if code == 0 else ""


# ── Compose transform helpers ────────────────────────────────────────────────

def _traefik_labels(router: str, host: str, port: int) -> dict[str, str]:
    return {
        "traefik.enable": "true",
        f"traefik.http.routers.{router}.rule": f"Host(`{host}`)",
        f"traefik.http.routers.{router}.entrypoints": "web",
        f"traefik.http.services.{router}.loadbalancer.server.port": str(port),
    }


def _apply_labels(svc: dict[str, Any], new: dict[str, str]) -> None:
    labels = svc.get("labels")
    if isinstance(labels, dict):
        labels.update(new)
    elif isinstance(labels, list):
        labels.extend(f"{k}={v}" for k, v in new.items())
    else:
        svc["labels"] = dict(new)


def _attach_network(svc: dict[str, Any]) -> None:
    """Add traefik-net to a service WITHOUT dropping its existing networks."""
    nets = svc.get("networks")
    if nets is None:
        names = ["default"]
    elif isinstance(nets, dict):
        names = list(nets.keys())
    else:
        names = list(nets)
    if TRAEFIK_NET not in names:
        names.append(TRAEFIK_NET)
    svc["networks"] = names


def _merge_env(svc: dict[str, Any], extra: dict[str, str]) -> None:
    """Merge env vars into a compose service, normalizing to the dict form."""
    if not extra:
        return
    env = svc.get("environment")
    merged: dict[str, str] = {}
    if isinstance(env, dict):
        merged = {str(k): ("" if v is None else str(v)) for k, v in env.items()}
    elif isinstance(env, list):
        for item in env:
            if isinstance(item, str) and "=" in item:
                k, v = item.split("=", 1)
                merged[k] = v
            elif isinstance(item, str):
                merged[item] = ""
    merged.update(extra)
    svc["environment"] = merged


def _router_name(project_name: str, label: str, env_name: str) -> str:
    """Traefik router/service name — MUST be unique per (project, service, env).
    Both environments' containers sit on the same traefik-net, and the docker
    provider drops a router entirely when two containers define it with
    conflicting rules."""
    base = f"{project_name}-{label}" if label else project_name
    return f"{base}-{env_name}"


_STATIC_DOCKERFILE = """# Generated by Homebox — build the static app, serve it with nginx.
FROM {builder} AS build
WORKDIR /app
COPY . .
RUN {build_command}

FROM nginx:alpine
COPY --from=build /app/{static_dir} /usr/share/nginx/html
# Hashed build assets (Vite/webpack emit content-addressed filenames under
# /assets/) are cached forever; the HTML shell is marked no-cache so a new
# deploy is picked up immediately instead of the browser serving a stale
# index.html that points at an old JS bundle.
RUN printf 'server {{\\n  listen 80;\\n  root /usr/share/nginx/html;\\n  location /assets/ {{\\n    expires 1y;\\n    add_header Cache-Control "public, immutable";\\n  }}\\n  location / {{\\n    try_files $uri $uri/ /index.html;\\n    add_header Cache-Control "no-cache";\\n  }}\\n}}\\n' > /etc/nginx/conf.d/default.conf
EXPOSE 80
"""


def _write_static_dockerfile(rd: Path, d: dissect.DetectedService) -> str:
    """Write a build+nginx Dockerfile for a static (SPA) service into its build
    dir. Returns the Dockerfile name to reference from compose."""
    build_dir = rd / (d.build_dir or ".")
    builder = d.build_image or "node:20-alpine"
    cmd = d.build_command or "npm ci && npm run build"
    static_dir = (d.static_dir or "dist").strip("/")
    name = "Dockerfile.homebox"
    (build_dir / name).write_text(
        _STATIC_DOCKERFILE.format(builder=builder, build_command=cmd, static_dir=static_dir)
    )
    return name


async def _nixpacks_build(rd: Path, project: Project, env: Environment, d: dissect.DetectedService) -> str:
    """Build an image for a build=nixpacks service from its dir. Returns the tag."""
    image = f"homebox-proj-{project.name}-{env.name}-{d.name}:latest".lower()
    ctx = str(rd / (d.build_dir or "."))
    port = d.internal_port or 8080
    code, out = await _run([
        "nixpacks", "build", ctx, "--name", image,
        "--env", f"PORT={port}",
    ])
    if code:
        raise DeployError(
            f"Nixpacks could not build service '{d.name}' ({d.build_dir or '.'}). Add a "
            f"Dockerfile there or declare it in homebox.yaml.\n\n" + out[-4000:]
        )
    return image


async def _assemble_stack(
    rd: Path, project: Project, env: Environment, domain_name: str,
    detected: list[dissect.DetectedService], user_env: dict[str, dict[str, str]],
    cluster_ctx: dict[str, Any] | None = None,
    *, base: bool = False,
    targets_map: dict[str, Any] | None = None,
    local_identity: dict[str, Any] | None = None,
) -> tuple[Path, dict[str, dict]]:
    """Build a single compose for the (project, env): compose-origin backing
    services reused as-is (ports stripped, joined to traefik-net), build-origin
    app services generated/built from source. Public services get Traefik labels
    routing their derived host. With a cluster_ctx and a homebox.yaml cluster
    opt-in, Postgres services are transformed for active-active replication
    (app/cluster_db.py).

    Services whose resolved deployment target is NOT homebox (targets_map from
    targetslib.effective_targets) are excluded from the compose entirely —
    they get a plan entry ({target, cloud: True, host, …}) and are deployed by
    _deploy_cloud_targets on the cloud-coordinator node after the local
    compose comes up.

    Homebox-targeted services whose LOCATION (config cluster_id/node_id —
    linked accounts) is not `local_identity` (targetslib.local_location) are
    excluded the same way, with a plan entry {target: "homebox", remote: True,
    cluster_id|node_id, host} — nothing local deploys or tears them down; the
    owning cluster deploys them from the same synced metadata. Returns
    (compose_path, plan)."""
    from . import targetslib
    apps = [d for d in detected if d.is_app]
    if not any(d.is_public for d in apps):
        raise DeployError(
            "No web service detected to publish. Homebox found only backing "
            "services (database/cache). Add a Dockerfile, a buildable app, or a "
            "homebox.yaml declaring your web/api service(s)."
        )

    compose = find_compose(rd)
    compose_services: dict[str, Any] = {}
    top_volumes: dict[str, Any] = {}
    top_configs: dict[str, Any] = {}
    if compose:
        try:
            cdata = yaml.safe_load(compose.read_text()) or {}
            compose_services = cdata.get("services") or {}
            top_volumes = cdata.get("volumes") or {}
        except (yaml.YAMLError, OSError):
            pass

    services_out: dict[str, Any] = {}
    plan: dict[str, dict] = {}

    # Entry host: on a BASE domain the domain root (or <env>.<domain>) is
    # the single hostname for the whole env — non-main public services are
    # path-proxied under it (infinitescroll.io/api). On a container domain each
    # service gets its own derived hostname, with /api additionally path-routed
    # on the main host (path_prefix).
    entry_host = urls.full_host(project.name, "", env.slug_suffix, domain_name, base=base)
    main_host = entry_host if any(x.is_public and not x.subdomain_label for x in detected) else None

    def _host_path(d: dissect.DetectedService) -> tuple[str | None, str | None]:
        if not d.is_public:
            return None, None
        if base:
            path = (d.path_prefix or f"/{d.subdomain_label}") if d.subdomain_label else None
            return entry_host, path
        return urls.full_host(project.name, d.subdomain_label, env.slug_suffix,
                              domain_name), None

    # Homebox targets located at ANOTHER cluster/node (linked accounts):
    # precompute the set + their public hosts so consumers' auto-wired URLs
    # (rewrite_cross_target_env) can point at the foreign service regardless
    # of iteration order.
    remote_names: set[str] = set()
    foreign_hosts: dict[str, str] = {}
    for d in detected:
        r = targetslib.resolve_for(targets_map, d.name)
        if r.target == "homebox" \
                and not targetslib.location_is_local(r.location, local_identity):
            remote_names.add(d.name)
            h, _ = _host_path(d)
            if h:
                foreign_hosts[d.name] = h

    for d in detected:
        host, path = _host_path(d)
        port = d.internal_port or 80

        resolved = targetslib.resolve_for(targets_map, d.name)
        if resolved.cloud and resolved.variant not in targetslib.DB_VM_VARIANTS:
            # Deploys elsewhere: no compose service, no Traefik route. The
            # cloud coordinator deploys it after the local stack is up; other
            # nodes just record the plan entry (state arrives via cluster sync).
            plan[d.name] = {
                "public": d.is_public, "host": host, "path": path, "port": port,
                "label": d.subdomain_label, "target": resolved.target,
                "cloud": True,
            }
            continue
        if d.name in remote_names:
            # Runs on ANOTHER homebox cluster/node: excluded from the local
            # compose exactly like cloud targets — but NOT deployed by
            # _deploy_cloud_targets (no "cloud" flag), and never subject to
            # cloud teardown (no state written; retarget homebox@A→homebox@B
            # just drops the service from A's stack on its next deploy).
            plan[d.name] = {
                "public": d.is_public, "host": host, "path": path, "port": port,
                "label": d.subdomain_label, "target": "homebox",
                "remote": True, **(resolved.location or {}),
            }
            continue
        # DB-VM targets fall through: the VM (provisioned by the pre-step,
        # deploy._provision_db_vms) is an ADDITIVE Spock replica — the local
        # replicated container stays in the compose so homebox nodes keep
        # active-active copies, while consumers' auto-env URLs are rewritten
        # to the VM's mesh IP below.

        if d.origin == "compose":
            svc = dict(compose_services.get(d.name) or {})
            svc.pop("ports", None)  # never publish host ports
            # PaaS default: stacks must survive daemon restarts. Only an
            # explicit user-set policy overrides this.
            svc.setdefault("restart", "unless-stopped")
        else:
            svc = await _generate_build_service(rd, project, env, d)

        cluster_db_info = None
        if d.kind == "database" and d.origin == "compose":
            from . import cluster_db
            if cluster_ctx:
                cluster_db_info = cluster_db.transform_db_service(
                    svc=svc, svc_name=d.name, rd=rd,
                    project_name=project.name, env_name=env.name,
                    state=cluster_ctx["state"], self_node_id=cluster_ctx["node_id"],
                    cluster_secret=cluster_ctx["secret"], top_volumes=top_volumes,
                    top_configs=top_configs,
                )
            else:
                # Not clustered (anymore) — keep serving replicated-era data.
                await cluster_db.residual_transform(
                    svc, d.name, urls.stack_name(project, env), top_volumes,
                )

        _attach_network(svc)
        # Auto-wired connection URLs use compose service names as hostnames —
        # rewrite the ones whose producer lives on a cloud target (mesh IP /
        # cloud endpoint) or on a FOREIGN homebox cluster/node (public
        # hostname) so cross-target pairs still connect.
        _merge_env(svc, targetslib.rewrite_cross_target_env(
            d.auto_env, resolved, targets_map or {}, foreign_hosts=foreign_hosts))
        _merge_env(svc, user_env.get(d.name, {}))
        if d.is_public and host:
            router = _router_name(project.name, d.subdomain_label, env.name)
            if path:
                # Base domain: this service lives at <entry host><path>.
                labels = {
                    "traefik.enable": "true",
                    f"traefik.http.routers.{router}.rule": f"Host(`{host}`) && PathPrefix(`{path}`)",
                    f"traefik.http.routers.{router}.entrypoints": "web",
                    f"traefik.http.services.{router}.loadbalancer.server.port": str(port),
                }
                _apply_labels(svc, labels)
                services_out[d.name] = svc
                plan[d.name] = {"public": True, "host": host, "path": path, "port": port, "label": d.subdomain_label}
                if cluster_db_info:
                    plan[d.name]["cluster_db"] = cluster_db_info
                continue
            labels = _traefik_labels(router, host, port)
            if not base and d.path_prefix and main_host and main_host != host:
                # Same-origin path route: <main host><prefix> → this service.
                # Traefik prefers the longer (more specific) rule, so this wins
                # over the main service's bare Host rule for matching paths.
                pr = f"{router}-path"
                labels.update({
                    f"traefik.http.routers.{pr}.rule": f"Host(`{main_host}`) && PathPrefix(`{d.path_prefix}`)",
                    f"traefik.http.routers.{pr}.entrypoints": "web",
                    f"traefik.http.routers.{pr}.service": router,
                })
            _apply_labels(svc, labels)

        services_out[d.name] = svc
        plan[d.name] = {"public": d.is_public, "host": host, "port": port, "label": d.subdomain_label}
        if cluster_db_info:
            plan[d.name]["cluster_db"] = cluster_db_info
        if resolved.cloud:
            # DB-VM fall-through (see above): record the target so the
            # instance row shows where the primary copy runs.
            plan[d.name]["target"] = resolved.target
            plan[d.name]["db_vm"] = True

    data: dict[str, Any] = {"services": services_out, "networks": {TRAEFIK_NET: {"external": True}}}
    if top_volumes:
        data["volumes"] = top_volumes
    if top_configs:
        data["configs"] = top_configs

    out = rd / GENERATED_COMPOSE
    out.write_text(yaml.safe_dump(data, sort_keys=False))
    return out, plan


async def _generate_build_service(
    rd: Path, project: Project, env: Environment, d: dissect.DetectedService,
) -> dict[str, Any]:
    """Produce the compose service dict for a build-origin (source) app. Every
    non-static app gets PORT=<assigned> injected and Traefik routes to that same
    port (apps are assumed to read $PORT), so no port guessing is needed."""
    svc: dict[str, Any] = {"restart": "unless-stopped"}
    bt = d.build_type
    if bt == "image" and d.image:
        svc["image"] = d.image
    elif bt == "static":
        dockerfile = _write_static_dockerfile(rd, d)
        svc["build"] = {"context": d.build_dir or ".", "dockerfile": dockerfile}
    elif bt == "dockerfile":
        svc["build"] = {"context": d.build_dir or "."}
        if d.dockerfile:
            svc["build"]["dockerfile"] = d.dockerfile
    else:  # nixpacks — pre-built into an image tag
        svc["image"] = await _nixpacks_build(rd, project, env, d)
    # nginx (static) is fixed on 80; everything else listens on its assigned PORT.
    if bt != "static" and d.internal_port:
        svc["environment"] = {"PORT": str(d.internal_port)}
    if d.command:
        svc["command"] = d.command
    return svc


# ── Container discovery ──────────────────────────────────────────────────────

async def _discover_containers(stack: str) -> dict[str, str]:
    """service name -> container name for a running stack."""
    code, out = await _run(["docker", "compose", "-p", stack, "ps", "--format", "json"], timeout=30)
    mapping: dict[str, str] = {}
    if code == 0 and out.strip():
        rows: list[dict] = []
        try:
            parsed = json.loads(out)
            rows = parsed if isinstance(parsed, list) else [parsed]
        except json.JSONDecodeError:
            for line in out.splitlines():
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        for r in rows:
            if r.get("Service") and r.get("Name"):
                mapping[r["Service"]] = r["Name"]
    return mapping


# ── Orchestration ─────────────────────────────────────────────────────────────

def _touch(dep: Deployment, **fields: Any) -> None:
    for k, v in fields.items():
        setattr(dep, k, v)
    dep.updated_at = datetime.utcnow()


async def _user_env_by_service(session: AsyncSession, project: Project, env: Environment) -> dict[str, dict[str, str]]:
    """User-set env vars (source='user'), keyed by service name, scoped to this
    env or all envs."""
    rows = (await session.execute(
        select(ServiceEnvVar, Service.name)
        .join(Service, ServiceEnvVar.service_id == Service.id)
        .where(
            Service.project_id == project.id,
            ServiceEnvVar.source == "user",
            (ServiceEnvVar.environment_id == None) | (ServiceEnvVar.environment_id == env.id),  # noqa: E711
        )
    )).all()
    out: dict[str, dict[str, str]] = {}
    for var, svc_name in rows:
        out.setdefault(svc_name, {})[var.key] = var.value
    return out


TRAEFIK_INTERNAL = "http://homebox-traefik:80"


async def verify_instances(session: AsyncSession, deployment_id: int, *,
                           attempts: int = 3, cloud_probe: bool = False) -> bool:
    """Probe each public instance and mark it running or unreachable. The probe
    goes through THIS node's Traefik directly (Host header), not the public
    tunnel — so the judgment is purely "is the app container serving?" and is
    immune to the tunnel-catch-all 404 ambiguity that made healthy apps look
    down. (End-to-end tunnel/DNS health is the tunnel monitor's job.)

    ANY HTTP response from the app counts as up — a 404/401/403/405 means the
    service answered, it just doesn't serve that exact path (an API that only
    serves /api/* legitimately 404s its root). Down means: connection failure,
    a gateway 5xx (Traefik has no healthy backend), or Traefik's own no-router
    404 fallback (exact body "404 page not found").

    Cloud-targeted instances have no local container/Traefik route: only the
    cloud coordinator probes them (directly, at their public URL); other nodes
    leave their status alone — they'd add noise, not signal."""
    rows = (await session.execute(
        select(ServiceInstance).where(ServiceInstance.deployment_id == deployment_id)
    )).scalars().all()
    all_ok = True
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
        for inst in rows:
            if not inst.url:
                continue
            if inst.status == "remote":
                # Runs on ANOTHER homebox cluster/node — no local container or
                # Traefik route; the owning cluster verifies it. Leave alone.
                continue
            if inst.target != "homebox":
                if not cloud_probe:
                    continue
                ok = False
                for i in range(attempts):
                    try:
                        r = await client.get(inst.url)
                        if r.status_code < 500:
                            ok = True
                            break
                    except httpx.HTTPError:
                        pass
                    if i < attempts - 1:
                        await asyncio.sleep(4)
                inst.status = "running" if ok else "unreachable"
                all_ok = all_ok and ok
                continue
            # Route the probe at the local Traefik, preserving host + path.
            parsed = httpx.URL(inst.url)
            host = parsed.host
            probe_url = f"{TRAEFIK_INTERNAL}{parsed.raw_path.decode() if parsed.raw_path else '/'}"
            ok = False
            for i in range(attempts):
                try:
                    r = await client.get(probe_url, headers={"Host": host})
                    traefik_no_router = (
                        r.status_code == 404 and r.text.strip() == "404 page not found"
                    )
                    if r.status_code < 500 and not traefik_no_router:
                        ok = True
                        break
                except httpx.HTTPError:
                    pass
                if i < attempts - 1:
                    await asyncio.sleep(4)
            inst.status = "running" if ok else "unreachable"
            all_ok = all_ok and ok
    await session.commit()
    return all_ok


def _integration_creds(integ) -> dict[str, Any]:
    """Decrypt an aws/gcp/cloudflare Integration row into the creds dict the
    provider clients expect."""
    from . import crypto
    if integ is None:
        raise DeployError("Cloud target has no connected integration — "
                          "reconnect the account in Integrations.")
    secret = crypto.decrypt(integ.secret_encrypted or "")
    if integ.provider == "aws":
        key_id, _, key_secret = secret.partition(":")
        return {"key_id": key_id, "secret": key_secret,
                "region": (integ.config or {}).get("region") or "us-east-1",
                "account_id": integ.account_id}
    if integ.provider == "gcp":
        import json as _j
        return {"sa": _j.loads(secret)}
    if integ.provider == "cloudflare":
        return {"token": secret, "account_id": integ.account_id,
                "config": dict(integ.config or {})}
    raise DeployError(f"unsupported target integration provider {integ.provider!r}")


def _provider_state(state: dict | None) -> dict:
    """The state view a provider sees (deploy short-circuits, destroy, probe).
    Providers persist their resource ids FLAT in TargetResult.state and read
    them back flat; the orchestrator stores them nested under
    state["resource_ids"] (next to its own status/endpoint/dns/mesh keys).
    Flatten at the boundary so both sides keep their shape."""
    state = state or {}
    return {**state, **(state.get("resource_ids") or {})}


async def _upsert_target_dns(session: AsyncSession, host: str, result) -> dict | None:
    """Point the service's hostname at the cloud endpoint. A specific-host
    record overrides the domain's wildcard→tunnel CNAME at Cloudflare, so the
    rest of the domain keeps routing through the tunnel untouched. Returns the
    dns state dict recorded on the target row (the DNS drift repair consults
    it as an exclusion — see routes/tunnel.py)."""
    from . import cloudflare as cf
    extra_records = (result.state or {}).get("extra_dns_records") or []
    # A provider may have verification records to write even while there is
    # no CNAME to point yet (Cloud Run's site-verification TXT precedes the
    # domain mapping) — only bail when there is nothing at all to do.
    if not host or (not result.cname_target and not extra_records):
        return None
    state = await cf.load_state(session)
    token = cf.get_token(state)
    if not token:
        raise DeployError("Cloudflare is not connected — cloud targets need it for DNS.")
    zones = await cf.list_zones(token, account_id=state.get("account_id"))
    zone = cf.resolve_zone_for(zones, host)
    if not zone:
        raise DeployError(f"No Cloudflare zone covers {host} — add the domain first.")
    if result.cname_target:
        await cf.upsert_cname(token, zone["id"], host, result.cname_target,
                              proxied=result.proxied)
    # Providers may need auxiliary records (App Runner certificate-validation
    # CNAMEs, Cloud Run site-verification TXTs). Best-effort: a failed
    # validation record surfaces later as a pending cert, not a broken deploy.
    for rec in extra_records:
        try:
            rtype = (rec.get("type") or "CNAME").upper()
            if rtype == "CNAME":
                await cf.upsert_cname(token, zone["id"], rec["name"],
                                      rec["value"], proxied=False)
            elif rtype == "TXT":
                await cf.upsert_txt(token, zone["id"], rec["name"], rec["value"])
            else:
                log.info("target dns: skipping unsupported record type %s for %s",
                         rec.get("type"), rec.get("name"))
        except cf.CloudflareError as e:
            log.warning("target dns: validation record %s failed: %s",
                        rec.get("name"), e)
    if not result.cname_target:
        # No exclusion-registry entry: the hostname is still tunnel-routed
        # (provider-endpoint fallback) and drift repair must keep covering it.
        return None
    return {"hostname": host, "cname_target": result.cname_target,
            "proxied": result.proxied, "zone_id": zone["id"]}


async def _delete_target_dns(session: AsyncSession, dns: dict | None) -> None:
    """Remove a cloud target's per-host CNAME (retarget/teardown) so the
    domain wildcard re-covers the hostname (back to the tunnel)."""
    from . import cloudflare as cf
    if not dns or not dns.get("hostname"):
        return
    state = await cf.load_state(session)
    token = cf.get_token(state)
    if not token:
        return
    try:
        records = await cf.list_dns_records(token, dns.get("zone_id"), name=dns["hostname"])
        for r in records:
            await cf.delete_dns_record(token, dns.get("zone_id"), r["id"])
    except cf.CloudflareError as e:
        log.warning("could not remove CNAME for %s: %s", dns.get("hostname"), e)


# ── cross-cluster domain sharing (per-host DNS overrides, G12) ────────────────
#
# Domains + the Cloudflare integration sync account-wide, but a domain's
# wildcard CNAME points at the tunnel of whichever cluster CONNECTED it — and
# homebox-local deploys rely purely on Host-header routing behind that
# wildcard. A local service deployed HERE under a domain owned by ANOTHER
# cluster would therefore route to the wrong cluster. Fix: upsert a
# specific-host proxied CNAME → THIS cluster's tunnel (a specific record beats
# the wildcard at Cloudflare), record it in the `dns_overrides` setting
# (targetslib.load_dns_overrides) and cover the host in our tunnel ingress.
# The owning cluster never reverts these records — its drift repair excludes
# foreign-homebox hostnames (targetslib.foreign_homebox_hostnames).


async def _delete_override_dns(token: str | None, host: str, meta: dict,
                               our_target: str | None) -> None:
    """Delete `host`'s override CNAME — conservatively: ONLY records pointing
    at OUR tunnel (the target recorded at creation, or the current one if the
    tunnel was re-created since). Anything else at that name belongs to
    someone else and is left alone."""
    from . import cloudflare as cf
    zone_id = meta.get("zone_id")
    if not token or not zone_id:
        return
    allowed = {t.lower().strip(".") for t in
               (meta.get("cname_target") or "", our_target or "") if t}
    for r in await cf.list_dns_records(token, zone_id, name=host):
        content = (r.get("content") or "").strip().lower().strip(".")
        if r.get("type") == "CNAME" and content in allowed:
            await cf.delete_dns_record(token, zone_id, r["id"])


async def _ensure_domain_overrides(
    session: AsyncSession, project: Project, env: Environment,
    plan: dict[str, dict], domain_name: str,
) -> str:
    """Reconcile this (project, env)'s per-host DNS overrides after a deploy.

    - Domain wildcard owned by ANOTHER cluster's tunnel → every LOCAL homebox
      public hostname gets a proxied CNAME → OUR tunnel, plus bookkeeping and
      a per-host ingress rule (via _push_ingress).
    - Domain owned by us → NO per-host records; the wildcard flow stands, and
      any leftover overrides of this env are removed (ownership changed).
    - Ownership indeterminable (API error) → change NOTHING this run.
    - Bookkept hostnames of this env no longer served locally (service
      retargeted away / made private / domain switched) → override record
      deleted, only when it still points at OUR tunnel.

    Coordinator-only (caller gates), idempotent, never fails the deploy —
    returns deploy-log lines."""
    from . import cloudflare as cf
    from . import targetslib

    state = await cf.load_state(session)
    token = cf.get_token(state)
    tunnel_id = state.get("tunnel_id")
    if not token or not tunnel_id:
        return ""  # no tunnel — wildcard routing isn't in play at all

    local_hosts: dict[str, str] = {}
    for name, info in plan.items():
        host = (info.get("host") or "").lower()
        if not host or not info.get("public") \
                or info.get("cloud") or info.get("remote"):
            continue
        if host == domain_name.lower():
            # Base-mode apex: overriding the apex record IS taking the
            # domain's root from its owner — never do that implicitly.
            continue
        local_hosts[host] = name

    overrides = await targetslib.load_dns_overrides(session)
    mine = {h: m for h, m in overrides.items()
            if m.get("project") == project.name and m.get("env") == env.name}
    if not local_hosts and not mine:
        return ""

    try:
        owned = await cf.domain_owned_by_local_tunnel(
            session, domain_name, state=state, strict=True)
    except cf.CloudflareError as e:
        # Fail-safe: without a definite answer we neither create records (the
        # domain may be ours) nor delete existing overrides (they may be
        # load-bearing). The next deploy or reconcile retries.
        return (f"\n[dns-override] WARNING: could not determine wildcard "
                f"ownership of {domain_name} ({e}) — DNS left untouched")

    our_target = cf.tunnel_target(tunnel_id)
    expected = {} if owned else dict(local_hosts)
    tail = ""
    changed = False

    # Stale overrides of THIS env: retargeted away, made private, domain
    # switched, or the domain is (now) ours — drop our record, keep others'.
    for host, meta in mine.items():
        if host in expected:
            continue
        try:
            await _delete_override_dns(token, host, meta, our_target)
            overrides.pop(host, None)
            changed = True
            tail += f"\n[dns-override] {host}: override record removed"
        except cf.CloudflareError as e:
            tail += f"\n[dns-override] WARNING: could not remove {host}: {e}"

    if expected:
        zones = await cf.list_zones(token, account_id=state.get("account_id"))
        zone = cf.resolve_zone_for(zones, domain_name)
        if zone is None:
            return tail + (f"\n[dns-override] WARNING: no Cloudflare zone "
                           f"covers {domain_name} — cannot create override records")
        for host in sorted(expected):
            try:
                await cf.upsert_cname(token, zone["id"], host, our_target,
                                      proxied=True)
            except cf.CloudflareError as e:
                tail += f"\n[dns-override] WARNING: {host}: {e}"
                continue
            prev = overrides.get(host)
            overrides[host] = {
                "domain": domain_name, "zone_id": zone["id"],
                "cname_target": our_target, "proxied": True,
                "project": project.name, "env": env.name,
                "service": expected[host],
                "created_at": (prev or {}).get("created_at")
                or datetime.utcnow().isoformat(),
            }
            if overrides[host] != prev:
                changed = True
            tail += (f"\n[dns-override] {host}: CNAME → {our_target} "
                     f"(wildcard of {domain_name} belongs to another cluster)")

    if changed:
        await targetslib.save_dns_overrides(session, overrides)
        # Belt-and-braces per-host ingress rule: the account-wide Domain rows
        # already put `*.{domain}` in our ingress, but override hosts must
        # keep routing even if that push ever becomes owned-domains-only.
        try:
            from .routes.tunnel import _push_ingress
            await _push_ingress(state, session)
        except Exception as e:  # noqa: BLE001 — ingress push is best-effort here
            tail += f"\n[dns-override] WARNING: ingress push failed: {e}"
    return tail


async def _cleanup_domain_overrides(
    project_name: str, env_name: str, session: AsyncSession | None = None,
) -> None:
    """Drop every per-host DNS override this env holds — teardown_stack path
    (env stopped / project released / node leaving with stacks). Conservative:
    only records still pointing at OUR tunnel are deleted; a failed delete
    keeps its bookkeeping so a later pass retries. Opens its own session when
    the caller (teardown_stack) has none."""
    from . import targetslib
    if session is None:
        async with SessionLocal() as own:
            await _cleanup_domain_overrides(project_name, env_name, session=own)
        return
    from . import cloudflare as cf
    overrides = await targetslib.load_dns_overrides(session)
    mine = [h for h, m in overrides.items()
            if m.get("project") == project_name and m.get("env") == env_name]
    if not mine:
        return
    state = await cf.load_state(session)
    token = cf.get_token(state)
    our_target = cf.tunnel_target(state["tunnel_id"]) \
        if state.get("tunnel_id") else None
    changed = False
    for host in mine:
        try:
            await _delete_override_dns(token, host, overrides[host], our_target)
            overrides.pop(host, None)
            changed = True
        except cf.CloudflareError as e:
            log.warning("dns override cleanup: %s failed: %s", host, e)
    if changed:
        await targetslib.save_dns_overrides(session, overrides)
        try:
            from .routes.tunnel import _push_ingress
            await _push_ingress(state, session)
        except Exception:  # noqa: BLE001 — best-effort ingress trim
            pass


async def _provision_db_vms(
    session: AsyncSession, project: Project, env: Environment, rd: Path,
    detected: list[dissect.DetectedService], targets_map: dict[str, Any],
    cluster_state: dict[str, Any] | None, cluster_ctx: dict[str, Any] | None,
    is_coord: bool,
) -> str:
    """Provision cloud database VMs (EC2/GCE) BEFORE the stack assembles, so
    consumers' rewritten env URLs (targetslib.rewrite_cross_target_env) can
    point at the VM's mesh IP from the first compose up. Coordinator-only;
    peers read the synced state. The VM is an ADDITIVE Spock replica — the
    local replicated container stays in the compose (see _assemble_stack).

    Mesh identity (ordinal + WireGuard keypair) is allocated once and persisted
    into the row's state.mesh BEFORE the VM exists (private key encrypted), so
    a re-run reuses it instead of minting a drifting identity. Failures are
    recorded on the ServiceTarget row and never sink the local deploy — the
    reconcile loop retries. Returns log lines for the deploy tail."""
    from .models import Integration, ServiceTarget
    from .targets import TargetError, get_provider
    from .targets.base import TargetDeployCtx
    from . import cluster_db, crypto, meshlib, targetslib

    if not is_coord:
        return ""
    tail = ""

    # Admin creds/image template come from the service's ORIGINAL compose
    # definition (same source transform_db_service reads at assemble time).
    compose_svcs: dict[str, dict[str, Any]] = {}
    compose = find_compose(rd)
    if compose:
        try:
            cdata = yaml.safe_load(compose.read_text()) or {}
            compose_svcs = {k: v for k, v in (cdata.get("services") or {}).items()
                            if isinstance(v, dict)}
        except (yaml.YAMLError, OSError):
            pass

    provisioned = False
    for d in detected:
        resolved = targetslib.resolve_for(targets_map, d.name)
        if resolved.variant not in targetslib.DB_VM_VARIANTS or resolved.row_id is None:
            continue
        row = await session.get(ServiceTarget, resolved.row_id)
        if row is None:
            continue
        line_prefix = f"\n[target:{resolved.target}] {d.name}: "
        try:
            if not cluster_ctx or not cluster_state:
                raise TargetError(
                    "database VM targets need a clustered install with DB "
                    "replication enabled (v1) — the VM joins the cluster's "
                    "WireGuard mesh and Spock replication."
                )
            svc_def = compose_svcs.get(d.name) or {}
            image = str(svc_def.get("image") or "")
            if not cluster_db.is_postgres_image(image):
                raise TargetError(
                    f"'{d.name}' is not a compose-origin Postgres service — "
                    "database VM targets support Postgres only (v1)."
                )
            env_tpl = cluster_db._norm_env(svc_def)
            admin_user = env_tpl.get("POSTGRES_USER") or "postgres"
            admin_password = env_tpl.get("POSTGRES_PASSWORD") or ""
            db_name = env_tpl.get("POSTGRES_DB") or admin_user

            # Reuse-or-mint the VM's mesh identity, persisted pre-provisioning.
            state = dict(row.state or {})
            mesh = dict(state.get("mesh") or {})
            if not mesh.get("ordinal"):
                mesh["ordinal"] = await targetslib.allocate_mesh_ordinal(session)
            ordinal = int(mesh["ordinal"])
            mesh["ip"] = meshlib.mesh_ip(ordinal)
            wg_priv = crypto.decrypt(mesh["wg_private_key_enc"]) \
                if mesh.get("wg_private_key_enc") else ""
            if not wg_priv or not mesh.get("wg_pubkey"):
                wg_priv, wg_pub = crypto.generate_wg_keypair()
                mesh["wg_private_key_enc"] = crypto.encrypt(wg_priv)
                mesh["wg_pubkey"] = wg_pub
            state["mesh"] = mesh
            row.state = state
            row.state_updated_at = datetime.utcnow()
            await session.commit()

            # Homebox nodes as wg peers — the VM never dials (it has no
            # endpoint for NAT'd nodes); nodes dial the VM's public IP.
            wg_peers = [
                {"public_key": n["wg_pubkey"],
                 "allowed_ips": f"{meshlib.mesh_ip(int(n['ordinal']))}/32"}
                for n in (cluster_state.get("roster") or [])
                if n.get("wg_pubkey") and n.get("ordinal")
            ]
            if not wg_peers:
                raise TargetError(
                    "no cluster nodes have WireGuard identities yet — bring "
                    "the mesh up (cluster page) before targeting a DB VM."
                )
            # 5432 goes public only when a serverless sibling may need it
            # (they can't run WireGuard); otherwise mesh/SG-only.
            open_pg = any(r.variant in targetslib.SERVERLESS_VARIANTS
                          for r in targets_map.values())

            overlay = {
                **resolved.config,
                "mesh_ordinal": ordinal,
                "mesh_ip": mesh["ip"],
                "wg_private_key": wg_priv,
                "wg_public_key": mesh["wg_pubkey"],
                "wg_peers": wg_peers,
                "open_pg_public": open_pg,
                "pg_image": cluster_db.PGEDGE_IMAGE.format(
                    major=cluster_db._pg_major(image)),
                "db": {
                    "db_name": db_name,
                    "admin_user": admin_user,
                    "admin_password": admin_password,
                    "repl_user": "pgedge",
                    "repl_password": cluster_db.derive_repl_password(
                        cluster_ctx["secret"], project.name, env.name, d.name),
                },
            }
            integ = await session.get(Integration, resolved.integration_id) \
                if resolved.integration_id else None
            provider = get_provider(resolved.target, d.kind,
                                    creds=_integration_creds(integ),
                                    config=overlay, state=_provider_state(state))
            ctx = TargetDeployCtx(
                project_name=project.name, env_name=env.name, service_name=d.name,
                kind=d.kind, rd=rd, hostname=None,
                config=overlay, state=_provider_state(state),
            )
            result = await provider.deploy(ctx)

            state = dict(row.state or {})
            rs = dict(result.state or {})
            # Keep the encrypted private key across the provider's mesh echo.
            state["mesh"] = {**mesh, **(rs.pop("mesh", None) or {})}
            state["db"] = rs.pop("db", None) or {"port": 5432, "node_name": f"n{ordinal}"}
            previous = state.pop("previous", None)
            state.update({
                "status": "live", "endpoint": result.endpoint, "error": None,
                "resource_ids": {**state.get("resource_ids", {}), **rs},
            })
            row.state = state
            row.state_updated_at = datetime.utcnow()
            await session.commit()
            provisioned = True
            tail += line_prefix + (f"database VM live at {result.endpoint} "
                                   f"(mesh {state['mesh'].get('ip')}, ordinal {ordinal})")

            if previous and previous.get("target") not in (None, "homebox", resolved.target):
                try:
                    old = get_provider(previous["target"], d.kind,
                                       creds=_integration_creds(integ),
                                       config=resolved.config,
                                       state=_provider_state(previous.get("state")))
                    await old.destroy(_provider_state(previous.get("state")))
                    tail += line_prefix + f"previous {previous['target']} resources destroyed"
                except (TargetError, DeployError) as e:
                    tail += line_prefix + f"WARNING: previous-target teardown failed: {e}"
        except (TargetError, DeployError) as e:
            state = dict(row.state or {})
            state.update({"status": "error", "error": str(e)[:500]})
            row.state = state
            row.state_updated_at = datetime.utcnow()
            await session.commit()
            tail += line_prefix + f"FAILED: {e}"
            log.warning("db vm target %s/%s failed: %s", resolved.target, d.name, e)
        except Exception as e:  # noqa: BLE001 — a provider bug must not sink the local deploy
            state = dict(row.state or {})
            state.update({"status": "error", "error": f"internal: {e}"[:500]})
            row.state = state
            row.state_updated_at = datetime.utcnow()
            await session.commit()
            tail += line_prefix + f"FAILED (internal): {e}"
            log.exception("db vm target %s/%s crashed", resolved.target, d.name)

    if provisioned and cluster_state:
        # This node's wg gains the VM peer right away; peers converge via
        # their own cluster loop (ensure_mesh pulls mesh_extra_peers).
        try:
            await meshlib.ensure_mesh(session, cluster_state)
        except Exception as e:  # noqa: BLE001 — mesh reconcile must not fail the deploy
            tail += f"\n[mesh] WARNING: could not reconcile local mesh: {e}"
    return tail


async def _teardown_retargeted(
    session: AsyncSession, project: Project, env: Environment,
) -> str:
    """Destroy cloud resources for services retargeted BACK to homebox.
    state.previous is normally consumed by _deploy_cloud_targets after the new
    CLOUD target goes live; when the new target is homebox no cloud deploy
    runs for the service, so this coordinator-only sweep (called once the
    local stack is up — i.e. the homebox 'target' is live) destroys the old
    provider resources, drops the per-host CNAME so the domain wildcard
    re-covers the hostname through the tunnel, and resets the row state.
    Failures keep state.previous so the next deploy retries."""
    from .models import Integration, ServiceTarget
    from .targets import TargetError, get_provider

    rows = (await session.execute(
        select(ServiceTarget, Service)
        .join(Service, Service.id == ServiceTarget.service_id)
        .where(Service.project_id == project.id)
        .where(ServiceTarget.target == "homebox")
        .where((ServiceTarget.environment_id == env.id)
               | (ServiceTarget.environment_id.is_(None)))
    )).all()
    tail = ""
    for st, svc in rows:
        state = dict(st.state or {})
        previous = state.get("previous")
        if not previous or previous.get("target") in (None, "homebox"):
            continue
        line_prefix = f"\n[target:homebox] {svc.name}: "
        try:
            integ = (await session.execute(
                select(Integration).where(Integration.provider == previous["target"])
            )).scalars().first()
            old = get_provider(previous["target"], svc.kind,
                               creds=_integration_creds(integ),
                               config=dict(st.config or {}),
                               state=_provider_state(previous.get("state")))
            await old.destroy(_provider_state(previous.get("state")))
            await _delete_target_dns(
                session, (previous.get("state") or {}).get("dns") or state.get("dns"))
            for key in ("previous", "dns", "resource_ids", "endpoint", "error",
                        "mesh", "db", "domain_mapping"):
                state.pop(key, None)
            state["status"] = "local"
            st.state = state
            st.state_updated_at = datetime.utcnow()
            await session.commit()
            tail += line_prefix + f"previous {previous['target']} resources destroyed"
        except (TargetError, DeployError) as e:
            tail += line_prefix + f"WARNING: {previous['target']} teardown failed: {e}"
            log.warning("retarget teardown %s/%s failed: %s",
                        previous["target"], svc.name, e)
        except Exception as e:  # noqa: BLE001 — teardown must not sink the deploy
            tail += line_prefix + f"WARNING: {previous['target']} teardown crashed: {e}"
            log.exception("retarget teardown %s/%s crashed", previous["target"], svc.name)
    return tail


async def _deploy_cloud_targets(
    session: AsyncSession, dep: Deployment, project: Project, env: Environment,
    rd: Path, detected: list[dissect.DetectedService], plan: dict[str, dict],
    targets_map: dict[str, Any], user_env: dict[str, dict[str, str]],
    flush_log, domain_name: str,
) -> str:
    """Deploy every cloud-targeted service of this stack. COORDINATOR ONLY —
    the caller gates on targetslib.is_cloud_coordinator. Failures are recorded
    per service on its ServiceTarget row (status=error) and reported in the
    deploy log; they never fail the local deploy (the reconcile loop retries).
    Returns log lines to append to the deploy tail."""
    from .models import Integration, ServiceTarget
    from .targets import TargetError, artifacts, get_provider
    from .targets.base import TargetDeployCtx
    from . import cloudflare as cf
    from . import targetslib

    tail = ""
    by_name = {d.name: d for d in detected}
    # Services homebox-targeted at ANOTHER cluster/node: cloud consumers still
    # reference them by their public hostname (same rewrite as _assemble_stack).
    foreign_hosts = {n: p["host"] for n, p in plan.items()
                     if p.get("remote") and p.get("host")}

    # Serverless → homebox DB path (phase 4): consumers on Cloud Run /
    # App Runner reaching a homebox-hosted database do so through tunnel TCP
    # ingress + a Cloudflare Access service token + a cloudflared proxy baked
    # into their image. Plan it once for the whole stack, ensure the
    # Cloudflare side (token, Access apps, ingress rules), and hand each
    # consumer its proxy rules + env overrides below.
    sdb = await targetslib.serverless_db_plan(
        session, project, env, targets_map, by_name, domain_name)
    access_env: dict[str, str] = {}
    access_err: str | None = None
    if sdb["tcp_rules"]:
        try:
            cf_state = await cf.load_state(session)
            cf_token = cf.get_token(cf_state)
            if not cf_token or not cf_state.get("account_id"):
                raise cf.CloudflareError(0, "Cloudflare is not connected")
            client_id, client_secret = await cf.ensure_access_service_token(
                session, cf_state)
            token_id = (cf_state.get("db_access_token") or {}).get("token_id") or ""
            for rule in sdb["tcp_rules"]:
                await cf.ensure_access_tcp_app(
                    cf_token, cf_state["account_id"], rule["hostname"], token_id)
            # Re-push the tunnel ingress so the tcp rules route (derived
            # inside _push_ingress via targetslib.all_tunnel_tcp_rules —
            # requires the auto env vars persisted by this deploy).
            from .routes.tunnel import _push_ingress
            await _push_ingress(cf_state, session)
            access_env = {"TUNNEL_SERVICE_TOKEN_ID": client_id,
                          "TUNNEL_SERVICE_TOKEN_SECRET": client_secret}
            tail += ("\n[target:tunnel] db ingress + Access ready: "
                     + ", ".join(r["hostname"] for r in sdb["tcp_rules"]))
        except Exception as e:  # noqa: BLE001 — consumers fail individually below
            access_err = str(e)
            tail += f"\n[target:tunnel] WARNING: serverless-to-DB path setup failed: {e}"
            log.warning("serverless db path setup failed: %s", e)
    for name, info in plan.items():
        if not info.get("cloud"):
            continue
        resolved = targets_map.get(name)
        d = by_name.get(name)
        if resolved is None or resolved.row_id is None or d is None:
            continue
        row = await session.get(ServiceTarget, resolved.row_id)
        if row is None:
            continue
        line_prefix = f"\n[target:{resolved.target}] {name}: "
        try:
            integ = await session.get(Integration, resolved.integration_id) \
                if resolved.integration_id else None
            provider = get_provider(
                resolved.target, d.kind,
                creds=_integration_creds(integ),
                config=resolved.config, state=_provider_state(resolved.state),
            )
            ctx = TargetDeployCtx(
                project_name=project.name, env_name=env.name, service_name=name,
                kind=d.kind, rd=rd, hostname=info.get("host"),
                env_vars={**targetslib.rewrite_cross_target_env(
                    d.auto_env, resolved, targets_map,
                    foreign_hosts=foreign_hosts),
                    **user_env.get(name, {})},
                internal_port=d.internal_port,
                config=resolved.config, state=_provider_state(resolved.state),
            )
            proxy_rules = sdb["proxy_rules"].get(name) or []
            if proxy_rules:
                if not access_env:
                    raise TargetError(
                        "serverless-to-homebox DB path unavailable: "
                        + (access_err or "Cloudflare Access setup failed")
                    )
                ctx.proxy_map = proxy_rules
                # Point the consumer at its in-container proxy ports — but a
                # user-set var beats the derived override, same as everywhere.
                for key, value in (sdb["env_overrides"].get(name) or {}).items():
                    if key not in user_env.get(name, {}):
                        ctx.env_vars[key] = value
                ctx.env_vars.update(access_env)
            if provider.variant in ("pages", "s3", "gcs"):
                ctx.static_dir = await artifacts.extract_static_artifacts(
                    rd, project.name, env.name, d)
            elif provider.variant in ("cloud_run", "app_runner"):
                ctx.image = await artifacts.build_cloud_image(
                    rd, project.name, env.name, d, ctx.env_vars)
                if ctx.proxy_map:
                    scratch = rd / ".homebox" / f"wrap-{name}"
                    scratch.mkdir(parents=True, exist_ok=True)
                    ctx.image = await artifacts.wrap_with_access_proxy(
                        ctx.image, ctx.proxy_map, project.name, env.name, name,
                        scratch)
                    tail += line_prefix + (
                        f"image wrapped with cloudflared access proxy "
                        f"({len(ctx.proxy_map)} db route(s))")
            elif provider.variant == "cf_containers":
                # wrangler builds the image itself — pass the service's build
                # location through instead of pre-building.
                ctx.config = {**ctx.config, "dockerfile": d.dockerfile,
                              "build_dir": d.build_dir}

            result = await provider.deploy(ctx)
            dns_state = await _upsert_target_dns(session, info.get("host"), result) \
                if info.get("public") else None
            # No CNAME (run.app / workers.dev fallback): the derived hostname
            # doesn't serve this target — surface the provider endpoint as the
            # instance URL instead.
            if info.get("public") and not result.cname_target and result.endpoint:
                inst = (await session.execute(
                    select(ServiceInstance).where(
                        ServiceInstance.deployment_id == dep.id,
                        ServiceInstance.service_name == name,
                    ))).scalar_one_or_none()
                if inst:
                    inst.url = f"https://{result.endpoint}"

            state = dict(row.state or {})
            previous = state.pop("previous", None)
            state.update({
                "status": "live", "endpoint": result.endpoint, "error": None,
                "resource_ids": {**state.get("resource_ids", {}), **result.state},
            })
            if dns_state:
                state["dns"] = dns_state
            row.state = state
            row.state_updated_at = datetime.utcnow()
            await session.commit()
            tail += line_prefix + f"live at {result.endpoint}"

            # The new target is live — now tear down what it replaced.
            if previous and previous.get("target") not in (None, "homebox", resolved.target):
                try:
                    old = get_provider(previous["target"], d.kind,
                                       creds=_integration_creds(integ),
                                       config=resolved.config,
                                       state=_provider_state(previous.get("state")))
                    await old.destroy(_provider_state(previous.get("state")))
                    await _delete_target_dns(
                        session, (previous.get("state") or {}).get("dns"))
                    tail += line_prefix + f"previous {previous['target']} resources destroyed"
                except (TargetError, DeployError) as e:
                    tail += line_prefix + f"WARNING: previous-target teardown failed: {e}"
        except (TargetError, DeployError) as e:
            state = dict(row.state or {})
            state.update({"status": "error", "error": str(e)[:500]})
            row.state = state
            row.state_updated_at = datetime.utcnow()
            await session.commit()
            tail += line_prefix + f"FAILED: {e}"
            log.warning("cloud target %s/%s failed: %s", resolved.target, name, e)
        except Exception as e:  # noqa: BLE001 — a provider bug must not sink the local deploy
            state = dict(row.state or {})
            state.update({"status": "error", "error": f"internal: {e}"[:500]})
            row.state = state
            row.state_updated_at = datetime.utcnow()
            await session.commit()
            tail += line_prefix + f"FAILED (internal): {e}"
            log.exception("cloud target %s/%s crashed", resolved.target, name)
    return tail


async def run_deploy(deployment_id: int, *, trigger: str = "manual") -> None:
    """Background entrypoint. Owns its OWN session. Never raises."""
    async with SessionLocal() as session:
        dep = await session.get(Deployment, deployment_id)
        if not dep:
            return
        env = await session.get(Environment, dep.environment_id)
        project = await session.get(Project, env.project_id) if env else None
        if not env or not project:
            _touch(dep, status="failed", error="Environment or project missing.")
            await session.commit()
            return
        from . import clusterlib
        dep.node_id = await clusterlib.get_node_id(session)
        async with _lock_for(dep.stack_name):
            try:
                did_transition = await _do_deploy(session, dep, project, env)
            except DeployError as e:
                _touch(dep, status="failed", error=str(e)[:8000])
                await session.commit()
                return
            except Exception as e:  # noqa: BLE001 — never let a deploy crash the task
                _touch(dep, status="failed", error=f"Unexpected error: {e}"[:8000])
                await session.commit()
                return
        # Fan the deploy out to cluster peers — but never re-fan a deploy that
        # itself arrived from a peer (that's how loops would start). These force
        # peers past their same-sha dedupe: a DB transition, a manual redeploy,
        # or a config-triggered one (env-var change) — all change the running
        # containers without a new commit, so peers must refresh too. Webhook
        # deploys keep the dedupe (new sha).
        if dep.trigger != "cluster":
            force = did_transition or dep.trigger in ("manual", "config")
            asyncio.get_event_loop().create_task(
                clusterlib.fanout_deploy(project.name, env.name, dep.commit_sha, force=force)
            )


async def _do_deploy(session: AsyncSession, dep: Deployment, project: Project, env: Environment) -> bool:
    # Resolve domain + integration explicitly (relationships aren't eagerly
    # loaded on the background-task session, and async can't lazy-load them).
    primary = (await session.execute(
        select(Domain).where(Domain.is_primary == True)  # noqa: E712
    )).scalar_one_or_none()
    # Domain precedence: environment override → project setting → primary.
    env_domain = await session.get(Domain, env.domain_id) if env.domain_id else None
    domain_obj = env_domain or (await session.get(Domain, project.domain_id) if project.domain_id else None)
    effective_domain = domain_obj or primary
    domain_name = effective_domain.name if effective_domain else None
    base = project.domain_mode == "base"
    if not domain_name:
        raise DeployError("No domain configured. Set a primary domain (Routes) or assign one to this project.")
    # Integration-less projects are public repos added by URL — anonymous clone.
    integration = await _load_integration(session, project) if project.integration_id else None
    if project.integration_id and not integration:
        raise DeployError("Source-control integration not found.")

    _touch(dep, status="cloning", error=None)
    await session.commit()

    token = decrypted_token(integration) if integration else None
    sha = await sync_source(project, env, token)
    rd = repo_dir(project.name, env.name)

    _touch(dep, status="dissecting", commit_sha=sha)
    await session.commit()
    detected = dissect.dissect(rd)
    user_env = await _user_env_by_service(session, project, env)

    _touch(dep, status="building")
    await session.commit()

    # Cluster context: when this node is in a synced cluster, Postgres services
    # get the active-active treatment BY DEFAULT (homebox.yaml can opt out —
    # cluster_db.db_replication_mode). Single-node installs deploy plain
    # Postgres from the same unchanged app config.
    from . import cluster_db, clusterlib
    cluster_ctx = None
    cluster_state = await clusterlib.load_cluster(session)
    if cluster_state and cluster_state.get("initial_sync_done") and cluster_db.cluster_db_enabled(rd):
        cluster_ctx = {
            "state": cluster_state,
            "node_id": await clusterlib.get_node_id(session),
            "secret": clusterlib.cluster_secret(cluster_state),
        }

    # Per-service deployment targets: services routed to a cloud target skip
    # the local compose; the cloud-coordinator node deploys them after the
    # local stack is up (other nodes receive state via cluster sync).
    from . import targetslib
    targets_map = await targetslib.effective_targets(session, project, env)
    is_coord = await targetslib.is_cloud_coordinator(session, cluster_state)
    # This install's cluster/node identity — homebox targets LOCATED at a
    # different cluster/node (linked accounts) are excluded like cloud ones.
    local_identity = await targetslib.local_location(session)

    # Database-VM targets provision BEFORE the stack assembles so consumers'
    # rewritten env URLs can point at the VM's mesh IP from the first up.
    if any(targetslib.resolve_for(targets_map, d.name).variant
           in targetslib.DB_VM_VARIANTS for d in detected):
        vm_tail = await _provision_db_vms(
            session, project, env, rd, detected, targets_map,
            cluster_state, cluster_ctx, is_coord,
        )
        if vm_tail:
            _touch(dep, log_tail=((dep.log_tail or "") + vm_tail).lstrip("\n"))
            await session.commit()
        # Re-resolve: the rows now carry state.mesh/endpoint for the rewrite.
        targets_map = await targetslib.effective_targets(session, project, env)

    # Assemble one stack: compose backing services + apps built from source
    # (Nixpacks/Dockerfile/static). Raises if no public app was detected.
    compose_path, plan = await _assemble_stack(
        rd, project, env, domain_name, detected, user_env, cluster_ctx,
        base=base, targets_map=targets_map, local_identity=local_identity,
    )

    _touch(dep, status="starting")
    await session.commit()

    stack = dep.stack_name

    # Single-node → cluster transition: the replicated DB starts on a FRESH
    # volume (an alpine data dir isn't binary-safe under the glibc pgEdge
    # image), so existing data must travel logically. Dump it NOW — compose up
    # is about to replace the old container. Only the deploy coordinator dumps
    # (peers' divergent copies are not the source of truth); peers receive the
    # restored data through replication (their first subscription synchronizes
    # data while their DB is still empty).
    transition_dumps: dict[str, Any] = {}
    if cluster_ctx and dep.trigger != "cluster":
        for name, pinfo in plan.items():
            cdb = pinfo.get("cluster_db")
            if not cdb or not cdb.get("legacy_volume"):
                continue
            old_vol = f"{stack}_{cdb['legacy_volume']}"
            new_vol = f"{stack}_{name}-pgedge"
            if await cluster_db.volume_exists(new_vol) or not await cluster_db.volume_exists(old_vol):
                continue
            old_container = f"{stack}-{name}-1"
            from .host import container_status
            st = container_status(old_container)
            if not st.get("exists"):
                _touch(dep, log_tail=(dep.log_tail or "") +
                       f"\n[cluster] {name}: found legacy volume {old_vol} but no container to "
                       f"dump from — data stays in the volume, restore it manually if needed.")
                continue
            if not st.get("running"):
                await _run(["docker", "start", old_container], timeout=60)
            for _ in range(30):
                code, _out = await _run(["docker", "exec", old_container, "pg_isready",
                                         "-U", cdb["admin_user"], "-d", cdb["db"]], timeout=15)
                if code == 0:
                    break
                await asyncio.sleep(2)
            dump_path = rd / ".homebox" / f"transition-{name}.dump"
            ok, msg = await cluster_db.dump_database(
                container=old_container, admin_user=cdb["admin_user"],
                admin_password=cdb["admin_password"], db=cdb["db"], out_path=dump_path,
            )
            if ok:
                transition_dumps[name] = dump_path
                _touch(dep, log_tail=(dep.log_tail or "") +
                       f"\n[cluster] {name}: transition dump captured ({msg})")
            else:
                raise DeployError(
                    f"Cluster transition: could not dump existing data from {old_container} "
                    f"({msg}). Aborting so the data isn't stranded — fix and redeploy, or opt "
                    f"out with homebox.yaml `cluster: {{database: none}}`."
                )
            await session.commit()

    # header carries any transition notes so the streaming log rewrites below
    # (header + build output) don't erase them.
    header = ((dep.log_tail or "") + "\n$ docker compose up -d --build\n").lstrip("\n")
    _touch(dep, log_tail=header)
    await session.commit()

    # Stream build output into log_tail (throttled) so the deployment log page
    # can follow an active deploy in near-realtime.
    last_flush = 0.0

    async def flush_log(text: str) -> None:
        nonlocal last_flush
        now = asyncio.get_event_loop().time()
        if now - last_flush < 1.5:
            return
        last_flush = now
        _touch(dep, log_tail=(header + text)[-8000:])
        await session.commit()

    local_services = [n for n, p in plan.items()
                      if not p.get("cloud") and not p.get("remote")]
    if local_services:
        code, out = await _run_streaming(
            ["docker", "compose", "-p", stack, "-f", str(compose_path),
             "up", "-d", "--build", "--remove-orphans"],
            cwd=str(rd),
            on_output=flush_log,
        )
        tail = (header + out)[-8000:]
        if code:
            _touch(dep, log_tail=tail)
            raise DeployError(f"docker compose up failed:\n{out[-8000:]}")
    else:
        # Every service is cloud-targeted or runs on another cluster — nothing
        # to run locally. Tear down any previous local containers for this
        # stack (retarget to cloud / to a foreign homebox).
        await _run(["docker", "compose", "-p", stack, "down", "--remove-orphans"],
                   timeout=300)
        tail = (header + "(no services target this homebox — no local containers)\n")[-8000:]

    # Record per-service instances (container + URL). Cloud services have no
    # container; their URL is still the derived public host.
    containers = await _discover_containers(stack) if local_services else {}
    await session.execute(
        ServiceInstance.__table__.delete().where(ServiceInstance.deployment_id == dep.id)
    )
    svc_rows = {
        s.name: s for s in (await session.execute(
            select(Service).where(Service.project_id == project.id)
        )).scalars()
    }
    for name, info in plan.items():
        url = (
            f"https://{info['host']}{info.get('path') or ''}"
            if info.get("public") and info.get("host") else None
        )
        session.add(ServiceInstance(
            deployment_id=dep.id,
            service_id=svc_rows[name].id if name in svc_rows else None,
            service_name=name,
            container_name=containers.get(name),
            url=url,
            # "remote" = homebox-targeted at ANOTHER cluster/node: no local
            # container, nothing to provision here — live status arrives via
            # sync from the owning cluster (read-only display).
            status="provisioning" if info.get("cloud")
            else "remote" if info.get("remote") else "running",
            target=info.get("target") or "homebox",
        ))
    await session.commit()

    # Cloud-targeted services: executed ONCE cluster-wide, on the coordinator.
    # Other nodes leave status as provisioning — cluster sync + the reconcile
    # loop converge them from the coordinator's results.
    if is_coord and any(p.get("cloud") for p in plan.values()):
        cloud_tail = await _deploy_cloud_targets(
            session, dep, project, env, rd, detected, plan, targets_map,
            user_env, flush_log, domain_name,
        )
        tail = (tail + cloud_tail)[-8000:]

    # Services retargeted BACK to homebox are live again the moment the local
    # stack is up — their previous cloud resources can now be torn down.
    if is_coord:
        teardown_tail = await _teardown_retargeted(session, project, env)
        if teardown_tail:
            tail = (tail + teardown_tail)[-8000:]

    # Cross-cluster domain sharing (G12): hostnames this cluster serves under
    # a domain whose wildcard/tunnel belongs to ANOTHER cluster get specific-
    # host CNAMEs → OUR tunnel (specific beats wildcard at Cloudflare); stale
    # overrides (service retargeted away, domain re-owned) are removed here
    # too. Coordinator-only, like every Cloudflare write.
    if is_coord:
        try:
            override_tail = await _ensure_domain_overrides(
                session, project, env, plan, domain_name)
        except Exception as e:  # noqa: BLE001 — DNS upkeep must not sink the deploy
            override_tail = f"\n[dns-override] WARNING: {e}"
            log.warning("domain override upkeep failed: %s", e)
        if override_tail:
            tail = (tail + override_tail)[-8000:]

    await verify_instances(session, dep.id, cloud_probe=is_coord)

    # Wire replicated DBs into the cluster mesh (repset membership + peer
    # subscriptions). Best-effort here — the cluster reconcile loop retries.
    if cluster_ctx:
        for name, info in plan.items():
            if not info.get("cluster_db"):
                continue
            try:
                # Cloud DB VMs replicating this database join as extra Spock
                # nodes; the coordinator also wires the VM's own subscriptions
                # (same contract as the reconcile loop in clusterlib).
                extra_nodes = await targetslib.db_vm_extra_nodes(
                    session, project, env, name)
                res = await cluster_db.ensure_replication(
                    stack=stack, info=info["cluster_db"],
                    state=cluster_ctx["state"], self_node_id=cluster_ctx["node_id"],
                    extra_nodes=extra_nodes, wire_extra=is_coord,
                )
                for kind in ("errors", "warnings"):
                    if res.get(kind):
                        tail = (tail + f"\n[cluster] {name} {kind}: " + "; ".join(res[kind]))[-8000:]
            except Exception as e:  # noqa: BLE001
                tail = (tail + f"\n[cluster] {name}: wiring error {e}")[-8000:]

        # Transition restore: pour the pre-up dump into the fresh replicated
        # DB. App containers pause for the restore (they've already run their
        # migrations during verify) so half-restored state is never served.
        for name, dump_path in transition_dumps.items():
            cdb = plan[name]["cluster_db"]
            new_container = containers.get(name) or f"{stack}-{name}-1"
            app_containers = [c for svc_name, c in containers.items() if svc_name != name]
            for c in app_containers:
                await _run(["docker", "stop", c], timeout=60)
            ok, msg = await cluster_db.restore_database(
                container=new_container, admin_user=cdb["admin_user"],
                admin_password=cdb["admin_password"], db=cdb["db"], dump_path=dump_path,
            )
            for c in app_containers:
                await _run(["docker", "start", c], timeout=60)
            if ok:
                dump_path.rename(dump_path.with_suffix(".dump.imported"))
                tail = (tail + f"\n[cluster] {name}: transition data restored — replicating to peers")[-8000:]
            else:
                tail = (tail + f"\n[cluster] {name}: TRANSITION RESTORE FAILED: {msg} — dump kept at {dump_path}")[-8000:]

    _touch(dep, status="running", log_tail=tail, error=None)
    # This deploy replaced the env's containers — older "running" rows are
    # history now, not live stacks.
    await session.execute(
        update(Deployment)
        .where(
            Deployment.environment_id == env.id,
            Deployment.id != dep.id,
            Deployment.status == "running",
        )
        .values(status="superseded", updated_at=datetime.utcnow())
    )
    await session.commit()

    await _trigger_promotions(session, project, env, dep)
    return bool(transition_dumps)


async def _trigger_promotions(
    session: AsyncSession, project: Project, env: Environment, dep: Deployment,
) -> None:
    """Code → dev → prod: after a successful deploy, advance promotion-gated
    envs sourced from this one. With an e2e workflow configured we dispatch it
    against this env's URL and wait for its workflow_run event; without one the
    target env deploys immediately."""
    if not dep.commit_sha:
        return
    targets = (await session.execute(
        select(Environment).where(
            Environment.project_id == project.id,
            Environment.promotion_gate == True,  # noqa: E712
        )
    )).scalars().all()
    targets = [
        t for t in targets
        if t.id != env.id and (
            t.promote_from_env_id == env.id
            or (t.promote_from_env_id is None and env.kind != "production")
        )
    ]
    if not targets:
        return

    for t in targets:
        pend = (await session.execute(
            select(Deployment).where(
                Deployment.environment_id == t.id,
                Deployment.status == "pending_promotion",
                Deployment.commit_sha == dep.commit_sha,
            ).order_by(Deployment.created_at.desc()).limit(1)
        )).scalar_one_or_none()
        if not pend:
            continue

        if t.e2e_workflow:
            instance_urls = [u for u in (await session.execute(
                select(ServiceInstance.url).where(
                    ServiceInstance.deployment_id == dep.id,
                    ServiceInstance.url.is_not(None),
                )
            )).scalars().all() if u]
            base_url = min(instance_urls, key=len) if instance_urls else ""
            integration = await _load_integration(session, project)
            branch = env.branch or project.default_branch or "main"
            try:
                if not integration:
                    raise DeployError("Project has no source-control integration.")
                token = decrypted_token(integration)
                try:
                    await gh.dispatch_workflow(
                        token, project.repo_full_name, t.e2e_workflow, branch,
                        inputs={"base_url": base_url, "environment": env.name},
                    )
                except Exception:
                    # Workflow may not declare inputs — retry bare.
                    await gh.dispatch_workflow(token, project.repo_full_name, t.e2e_workflow, branch)
                pend.status = "pending_e2e"
                pend.error = None
            except Exception as e:  # noqa: BLE001 — surface, don't crash the deploy
                pend.status = "blocked"
                pend.error = f"Could not dispatch e2e workflow {t.e2e_workflow}: {e}"[:2000]
        else:
            pend.status = "queued"
            pend.error = None
            pend_id = pend.id
            await session.commit()
            asyncio.get_event_loop().create_task(run_deploy(pend_id, trigger="webhook"))
            continue
        await session.commit()


async def _load_integration(session: AsyncSession, project: Project):
    from .models import Integration
    return await session.get(Integration, project.integration_id)


async def teardown_stack(project_name: str, env_name: str) -> tuple[bool, str]:
    """Stop + remove an environment's containers/networks. Keeps named volumes
    (no -v) so data survives a redeploy. Best-effort. Also drops any per-host
    DNS override records this env held on a foreign-owned domain (G12) — a
    stopped env must not keep pulling that hostname's traffic to this cluster."""
    stack = f"homebox-proj-{project_name}-{env_name}".lower()
    code, out = await _run(["docker", "compose", "-p", stack, "down", "--remove-orphans"], timeout=120)
    try:
        await _cleanup_domain_overrides(project_name, env_name)
    except Exception as e:  # noqa: BLE001 — DNS cleanup must not fail the teardown
        log.warning("domain override cleanup for %s/%s failed: %s",
                    project_name, env_name, e)
    return code == 0, out[-2000:]
