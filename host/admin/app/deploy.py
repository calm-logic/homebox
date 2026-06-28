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


def _route_plan(project: Project, env: Environment, domain_name: str,
                detected: list[dissect.DetectedService]) -> dict[str, dict]:
    """Map compose-service-name -> {public, host, port, label} for this env."""
    plan: dict[str, dict] = {}
    for d in detected:
        host = None
        if d.is_public:
            label = d.subdomain_label
            host = f"{urls.host_label(project.name, label, env.slug_suffix)}.{domain_name}"
        plan[d.name] = {
            "public": d.is_public,
            "host": host,
            "port": d.internal_port or 80,
            "label": d.subdomain_label,
        }
    return plan


def _transform_compose(rd: Path, project: Project, env: Environment, domain_name: str,
                       detected: list[dissect.DetectedService],
                       user_env: dict[str, dict[str, str]]) -> tuple[Path, dict[str, dict]]:
    """Rewrite the user's compose into a Homebox-routable one for this env."""
    src = find_compose(rd)
    if not src:
        raise DeployError("no compose file found")
    data = yaml.safe_load(src.read_text()) or {}
    services = data.get("services") or {}
    if not services:
        raise DeployError("compose file declares no services")

    plan = _route_plan(project, env, domain_name, detected)
    auto_by_name = {d.name: d.auto_env for d in detected}

    for name, svc in services.items():
        if not isinstance(svc, dict):
            continue
        svc.pop("ports", None)  # never publish host ports — avoids conflicts
        # inject env: auto-wired connection vars first, then user overrides
        _merge_env(svc, auto_by_name.get(name, {}))
        _merge_env(svc, user_env.get(name, {}))
        info = plan.get(name)
        if info and info["public"] and info["host"]:
            _attach_network(svc)
            _apply_labels(svc, _traefik_labels(_router_name(project.name, info["label"]), info["host"], info["port"]))

    top = data.get("networks") or {}
    top[TRAEFIK_NET] = {"external": True}
    data["networks"] = top
    data.pop("version", None)  # obsolete; silences a compose warning

    out = rd / GENERATED_COMPOSE
    out.write_text(yaml.safe_dump(data, sort_keys=False))
    return out, plan


def _generate_compose(rd: Path, project: Project, env: Environment, domain_name: str,
                      detected: list[dissect.DetectedService],
                      user_env: dict[str, dict[str, str]], *, image: str | None) -> tuple[Path, dict[str, dict]]:
    """Generate a one-service compose for Dockerfile/buildpack repos."""
    d = detected[0]
    port = d.internal_port or 8080
    host = f"{urls.host_label(project.name, '', env.slug_suffix)}.{domain_name}"
    web: dict[str, Any] = {
        "restart": "unless-stopped",
        "environment": {"PORT": str(port)},
        "networks": ["default", TRAEFIK_NET],
        "labels": _traefik_labels(_router_name(project.name, ""), host, port),
    }
    if image:
        web["image"] = image
    else:
        web["build"] = d.source_ref or "."
    _merge_env(web, user_env.get(d.name, {}))
    data = {"services": {d.name: web}, "networks": {TRAEFIK_NET: {"external": True}}}
    out = rd / GENERATED_COMPOSE
    out.write_text(yaml.safe_dump(data, sort_keys=False))
    plan = {d.name: {"public": True, "host": host, "port": port, "label": ""}}
    return out, plan


async def _buildpack_build(rd: Path, project: Project, env: Environment) -> str:
    image = f"homebox-proj-{project.name}-{env.name}:latest".lower()
    code, out = await _run(["nixpacks", "build", str(rd), "--name", image])
    if code:
        raise DeployError(
            "Nixpacks could not build this project automatically. Add a "
            "Dockerfile or docker-compose.yml to control the build.\n\n" + out[-4000:]
        )
    return image


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

    if find_compose(rd):
        compose_path, plan = _transform_compose(rd, project, env, domain_name, detected, user_env)
    elif (rd / "Dockerfile").is_file():
        compose_path, plan = _generate_compose(rd, project, env, domain_name, detected, user_env, image=None)
    else:
        image = await _buildpack_build(rd, project, env)
        compose_path, plan = _generate_compose(rd, project, env, domain_name, detected, user_env, image=image)

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
