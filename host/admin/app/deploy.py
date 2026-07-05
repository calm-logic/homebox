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


async def sync_source(project: Project, env: Environment, token: str) -> str:
    """Clone (or fetch+reset) the env's branch into repo_dir. Returns HEAD sha."""
    rd = repo_dir(project.name, env.name)
    url = f"https://x-access-token:{token}@github.com/{project.repo_full_name}.git"
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
RUN printf 'server {{\\n  listen 80;\\n  root /usr/share/nginx/html;\\n  location / {{ try_files $uri $uri/ /index.html; }}\\n}}\\n' > /etc/nginx/conf.d/default.conf
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
    *, dedicated: bool = False,
) -> tuple[Path, dict[str, dict]]:
    """Build a single compose for the (project, env): compose-origin backing
    services reused as-is (ports stripped, joined to traefik-net), build-origin
    app services generated/built from source. Public services get Traefik labels
    routing their derived host. With a cluster_ctx and a homebox.yaml cluster
    opt-in, Postgres services are transformed for active-active replication
    (app/cluster_db.py). Returns (compose_path, plan)."""
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

    # Entry host: on a DEDICATED domain the domain root (or <env>.<domain>) is
    # the single hostname for the whole env — non-main public services are
    # path-proxied under it (infinitescroll.io/api). On a wildcard domain each
    # service gets its own derived hostname, with /api additionally path-routed
    # on the main host (path_prefix).
    entry_host = urls.full_host(project.name, "", env.slug_suffix, domain_name, dedicated=dedicated)
    main_host = entry_host if any(x.is_public and not x.subdomain_label for x in detected) else None

    for d in detected:
        path: str | None = None
        if not d.is_public:
            host = None
        elif dedicated:
            host = entry_host
            if d.subdomain_label:
                path = d.path_prefix or f"/{d.subdomain_label}"
        else:
            host = urls.full_host(project.name, d.subdomain_label, env.slug_suffix, domain_name)
        port = d.internal_port or 80

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
        _merge_env(svc, d.auto_env)
        _merge_env(svc, user_env.get(d.name, {}))
        if d.is_public and host:
            router = _router_name(project.name, d.subdomain_label, env.name)
            if path:
                # Dedicated domain: this service lives at <entry host><path>.
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
            if not dedicated and d.path_prefix and main_host and main_host != host:
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


async def verify_instances(session: AsyncSession, deployment_id: int, *, attempts: int = 3) -> bool:
    """Probe each public instance URL end-to-end and mark it running or
    unreachable. ANY response from the app counts as up — an API with no
    route at / legitimately 404s (e.g. it only serves /api/*). Down means:
    network/edge errors, 5xx, Traefik's no-router 404 fallback (exact body
    "404 page not found"), or the Cloudflare tunnel ingress catch-all's
    EMPTY-body 404 (the hostname isn't in the tunnel config at all — the app
    was previously reported green while unreachable from any browser).
    Returns True when every URL answered."""
    rows = (await session.execute(
        select(ServiceInstance).where(ServiceInstance.deployment_id == deployment_id)
    )).scalars().all()
    all_ok = True
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
        for inst in rows:
            if not inst.url:
                continue
            ok = False
            for i in range(attempts):
                try:
                    r = await client.get(inst.url)
                    body = r.text.strip()
                    infra_404 = r.status_code == 404 and (
                        body == "404 page not found"  # traefik: no router
                        or body == ""                 # tunnel ingress catch-all
                    )
                    if r.status_code < 500 and not infra_404:
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
                await _do_deploy(session, dep, project, env)
            except DeployError as e:
                _touch(dep, status="failed", error=str(e)[:8000])
                await session.commit()
                return
            except Exception as e:  # noqa: BLE001 — never let a deploy crash the task
                _touch(dep, status="failed", error=f"Unexpected error: {e}"[:8000])
                await session.commit()
                return
        # Fan the deploy out to cluster peers — but never re-fan a deploy that
        # itself arrived from a peer (that's how loops would start).
        if dep.trigger != "cluster":
            asyncio.get_event_loop().create_task(
                clusterlib.fanout_deploy(project.name, env.name, dep.commit_sha)
            )


async def _do_deploy(session: AsyncSession, dep: Deployment, project: Project, env: Environment) -> None:
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
    dedicated = bool(effective_domain and effective_domain.mode == "dedicated")
    if not domain_name:
        raise DeployError("No domain configured. Set a primary domain (Routes) or assign one to this project.")
    if not project.integration_id:
        raise DeployError("Project has no source-control integration.")
    integration = await _load_integration(session, project)
    if not integration:
        raise DeployError("Source-control integration not found.")

    _touch(dep, status="cloning", error=None)
    await session.commit()

    token = decrypted_token(integration)
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

    # Assemble one stack: compose backing services + apps built from source
    # (Nixpacks/Dockerfile/static). Raises if no public app was detected.
    compose_path, plan = await _assemble_stack(
        rd, project, env, domain_name, detected, user_env, cluster_ctx,
        dedicated=dedicated,
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

    # Record per-service instances (container + URL).
    containers = await _discover_containers(stack)
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
            status="running",
        ))
    await session.commit()
    await verify_instances(session, dep.id)

    # Wire replicated DBs into the cluster mesh (repset membership + peer
    # subscriptions). Best-effort here — the cluster reconcile loop retries.
    if cluster_ctx:
        for name, info in plan.items():
            if not info.get("cluster_db"):
                continue
            try:
                res = await cluster_db.ensure_replication(
                    stack=stack, info=info["cluster_db"],
                    state=cluster_ctx["state"], self_node_id=cluster_ctx["node_id"],
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
    (no -v) so data survives a redeploy. Best-effort."""
    stack = f"homebox-proj-{project_name}-{env_name}".lower()
    code, out = await _run(["docker", "compose", "-p", stack, "down", "--remove-orphans"], timeout=120)
    return code == 0, out[-2000:]
