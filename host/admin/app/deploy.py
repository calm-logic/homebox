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

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from .config import settings
from .db import SessionLocal
from . import dissect, urls
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


def _router_name(project_name: str, label: str) -> str:
    return f"{project_name}-{label}" if label else project_name


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
) -> tuple[Path, dict[str, dict]]:
    """Build a single compose for the (project, env): compose-origin backing
    services reused as-is (ports stripped, joined to traefik-net), build-origin
    app services generated/built from source. Public services get Traefik labels
    routing their derived host. Returns (compose_path, plan)."""
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
    if compose:
        try:
            cdata = yaml.safe_load(compose.read_text()) or {}
            compose_services = cdata.get("services") or {}
            top_volumes = cdata.get("volumes") or {}
        except (yaml.YAMLError, OSError):
            pass

    services_out: dict[str, Any] = {}
    plan: dict[str, dict] = {}

    for d in detected:
        host = (
            f"{urls.host_label(project.name, d.subdomain_label, env.slug_suffix)}.{domain_name}"
            if d.is_public else None
        )
        port = d.internal_port or 80

        if d.origin == "compose":
            svc = dict(compose_services.get(d.name) or {})
            svc.pop("ports", None)  # never publish host ports
        else:
            svc = await _generate_build_service(rd, project, env, d)

        _attach_network(svc)
        _merge_env(svc, d.auto_env)
        _merge_env(svc, user_env.get(d.name, {}))
        if d.is_public and host:
            _apply_labels(svc, _traefik_labels(_router_name(project.name, d.subdomain_label), host, port))

        services_out[d.name] = svc
        plan[d.name] = {"public": d.is_public, "host": host, "port": port, "label": d.subdomain_label}

    data: dict[str, Any] = {"services": services_out, "networks": {TRAEFIK_NET: {"external": True}}}
    if top_volumes:
        data["volumes"] = top_volumes

    out = rd / GENERATED_COMPOSE
    out.write_text(yaml.safe_dump(data, sort_keys=False))
    return out, plan


async def _generate_build_service(
    rd: Path, project: Project, env: Environment, d: dissect.DetectedService,
) -> dict[str, Any]:
    """Produce the compose service dict for a build-origin (source) app."""
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
        svc["environment"] = {"PORT": str(d.internal_port or 8080)}
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
        async with _lock_for(dep.stack_name):
            try:
                await _do_deploy(session, dep, project, env)
            except DeployError as e:
                _touch(dep, status="failed", error=str(e)[:8000])
                await session.commit()
            except Exception as e:  # noqa: BLE001 — never let a deploy crash the task
                _touch(dep, status="failed", error=f"Unexpected error: {e}"[:8000])
                await session.commit()


async def _do_deploy(session: AsyncSession, dep: Deployment, project: Project, env: Environment) -> None:
    # Resolve domain + integration explicitly (relationships aren't eagerly
    # loaded on the background-task session, and async can't lazy-load them).
    primary = (await session.execute(
        select(Domain).where(Domain.is_primary == True)  # noqa: E712
    )).scalar_one_or_none()
    domain_obj = await session.get(Domain, project.domain_id) if project.domain_id else None
    domain_name = domain_obj.name if domain_obj else (primary.name if primary else None)
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

    # Assemble one stack: compose backing services + apps built from source
    # (Nixpacks/Dockerfile/static). Raises if no public app was detected.
    compose_path, plan = await _assemble_stack(rd, project, env, domain_name, detected, user_env)

    _touch(dep, status="starting")
    await session.commit()

    stack = dep.stack_name
    code, out = await _run(
        ["docker", "compose", "-p", stack, "-f", str(compose_path),
         "up", "-d", "--build", "--remove-orphans"],
        cwd=str(rd),
    )
    tail = out[-8000:]
    if code:
        raise DeployError(f"docker compose up failed:\n{tail}")

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
        url = f"https://{info['host']}" if info.get("public") and info.get("host") else None
        session.add(ServiceInstance(
            deployment_id=dep.id,
            service_id=svc_rows[name].id if name in svc_rows else None,
            service_name=name,
            container_name=containers.get(name),
            url=url,
            status="running",
        ))
    _touch(dep, status="running", log_tail=tail, error=None)
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
