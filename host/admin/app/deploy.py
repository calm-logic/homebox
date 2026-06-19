"""Project deploy engine.

A "managed" repo is deployed as its own docker-compose stack on the shared
`traefik-net` network. Traefik's docker provider auto-discovers the web
container via labels, routing `<slug>.<primary-domain>` to it. Backing services
(Postgres, Redis, …) stay internal to the stack and never publish host ports, so
two projects can both use 5432 internally without conflict.

Build source, in priority order:
  1. repo has a compose file  → use it (ports stripped, traefik-net + labels added)
  2. repo has only a Dockerfile → generate a one-service compose that builds it
  3. neither                   → Nixpacks infers + builds an image, wrapped in a
                                  generated compose

Everything that touches the Docker daemon runs under `projects_host_dir`, which
is bind-mounted at the SAME path inside the admin container and on the host, so
compose bind mounts resolve identically on both sides.
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

from .config import settings
from .db import SessionLocal
from .models import Deployment, Domain, Organization, Repository
from .orgs import decrypted_pat

GENERATED_COMPOSE = "docker-compose.homebox.yml"
TRAEFIK_NET = "traefik-net"
_BUILD_TIMEOUT = 1800  # 30 min
# Services likely to be the public HTTP entrypoint, in preference order.
_WEB_NAME_HINTS = ("web", "app", "frontend", "api", "server")

# One lock per slug so a manual deploy and a webhook deploy can't `compose up`
# the same stack concurrently.
_locks: dict[str, asyncio.Lock] = {}


class DeployError(Exception):
    """A deploy step failed; the message is surfaced to the UI."""


def _lock_for(slug: str) -> asyncio.Lock:
    lock = _locks.get(slug)
    if lock is None:
        lock = _locks[slug] = asyncio.Lock()
    return lock


def project_dir(slug: str) -> Path:
    return settings.projects_host_dir / slug


def repo_dir(slug: str) -> Path:
    return project_dir(slug) / "repo"


def stack_name(slug: str) -> str:
    return f"homebox-proj-{slug}"


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


async def sync_source(repo: Repository, token: str) -> str:
    """Clone (or fetch+reset) the repo's default branch into repo_dir. Returns HEAD sha."""
    slug = repo.project_slug or ""
    rd = repo_dir(slug)
    url = f"https://x-access-token:{token}@github.com/{repo.full_name}.git"
    branch = repo.default_branch or "main"

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


# ── Build-mode detection + compose generation ────────────────────────────────

_COMPOSE_NAMES = ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")


def _find_compose(rd: Path) -> Path | None:
    for name in _COMPOSE_NAMES:
        if (rd / name).is_file():
            return rd / name
    return None


def detect_build_mode(rd: Path) -> str:
    if _find_compose(rd):
        return "compose"
    if (rd / "Dockerfile").is_file():
        return "dockerfile"
    return "buildpack"


def _label_value(svc: dict[str, Any], key: str) -> str | None:
    labels = svc.get("labels")
    if isinstance(labels, dict):
        v = labels.get(key)
        return str(v) if v is not None else None
    if isinstance(labels, list):
        for item in labels:
            if isinstance(item, str) and item.startswith(f"{key}="):
                return item.split("=", 1)[1]
    return None


def _pick_web_service(services: dict[str, dict]) -> str:
    # 1. explicit opt-in
    for name, svc in services.items():
        if (_label_value(svc or {}, "homebox.expose") or "").lower() == "true":
            return name
    # 2. conventional name
    for hint in _WEB_NAME_HINTS:
        if hint in services:
            return hint
    # 3. first service that publishes/exposes a port
    for name, svc in services.items():
        if (svc or {}).get("ports") or (svc or {}).get("expose"):
            return name
    # 4. first service
    return next(iter(services))


def _detect_port(svc: dict[str, Any]) -> int:
    explicit = _label_value(svc, "homebox.port")
    if explicit and explicit.isdigit():
        return int(explicit)
    # First exposed container port.
    for expose in (svc.get("expose") or []):
        s = str(expose).split("/")[0]
        if s.isdigit():
            return int(s)
    # First published port's container side ("8080:80" -> 80, "80" -> 80).
    for p in (svc.get("ports") or []):
        s = str(p).rsplit(":", 1)[-1].split("/")[0]
        if s.isdigit():
            return int(s)
    return 80


def _traefik_labels(slug: str, web_host: str, port: int) -> dict[str, str]:
    return {
        "traefik.enable": "true",
        f"traefik.http.routers.{slug}.rule": f"Host(`{web_host}`)",
        f"traefik.http.routers.{slug}.entrypoints": "web",
        f"traefik.http.services.{slug}.loadbalancer.server.port": str(port),
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
    """Add traefik-net to a service WITHOUT dropping its existing networks. A
    service with no `networks` key implicitly joins `default`; once we add an
    explicit list it would lose that, so re-add `default` too."""
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


def neutralize_compose(rd: Path, slug: str, web_host: str) -> tuple[Path, str]:
    """Transform the user's compose into a Homebox-routable one. Returns
    (generated_path, web_service_name)."""
    src = _find_compose(rd)
    if not src:
        raise DeployError("no compose file found")
    data = yaml.safe_load(src.read_text()) or {}
    services = data.get("services") or {}
    if not services:
        raise DeployError("compose file declares no services")

    web = _pick_web_service(services)
    port = _detect_port(services[web] or {})

    for svc in services.values():
        if isinstance(svc, dict):
            svc.pop("ports", None)  # never publish host ports — avoids conflicts

    web_svc = services[web]
    _attach_network(web_svc)
    _apply_labels(web_svc, _traefik_labels(slug, web_host, port))

    top = data.get("networks") or {}
    top[TRAEFIK_NET] = {"external": True}
    data["networks"] = top
    data.pop("version", None)  # obsolete; silences a compose warning

    out = rd / GENERATED_COMPOSE
    out.write_text(yaml.safe_dump(data, sort_keys=False))
    return out, web


def _write_generated(rd: Path, data: dict) -> Path:
    out = rd / GENERATED_COMPOSE
    out.write_text(yaml.safe_dump(data, sort_keys=False))
    return out


def generate_compose_for_dockerfile(rd: Path, slug: str, web_host: str, port: int = 8080) -> tuple[Path, str]:
    data = {
        "services": {
            "web": {
                "build": ".",
                "restart": "unless-stopped",
                "environment": {"PORT": str(port)},
                "networks": ["default", TRAEFIK_NET],
                "labels": _traefik_labels(slug, web_host, port),
            }
        },
        "networks": {TRAEFIK_NET: {"external": True}},
    }
    return _write_generated(rd, data), "web"


def generate_compose_for_image(rd: Path, slug: str, image: str, web_host: str, port: int = 8080) -> tuple[Path, str]:
    data = {
        "services": {
            "web": {
                "image": image,
                "restart": "unless-stopped",
                "environment": {"PORT": str(port)},
                "networks": ["default", TRAEFIK_NET],
                "labels": _traefik_labels(slug, web_host, port),
            }
        },
        "networks": {TRAEFIK_NET: {"external": True}},
    }
    return _write_generated(rd, data), "web"


async def buildpack_build(rd: Path, slug: str) -> str:
    """Build an image with Nixpacks. Returns the image tag."""
    image = f"homebox-proj-{slug}-web:latest"
    code, out = await _run(["nixpacks", "build", str(rd), "--name", image])
    if code:
        raise DeployError(
            "Nixpacks could not build this project automatically. Add a "
            "Dockerfile or docker-compose.yml to control the build.\n\n" + out[-4000:]
        )
    return image


# ── Container discovery ──────────────────────────────────────────────────────

async def _discover_web_container(stack: str, service: str) -> str:
    code, out = await _run(["docker", "compose", "-p", stack, "ps", "--format", "json"], timeout=30)
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
            if r.get("Service") == service and r.get("Name"):
                return r["Name"]
    # Fallback to compose's default naming.
    return f"{stack}-{service}-1"


# ── Orchestration ─────────────────────────────────────────────────────────────

def _touch(dep: Deployment, **fields: Any) -> None:
    for k, v in fields.items():
        setattr(dep, k, v)
    dep.updated_at = datetime.utcnow()


async def run_deploy(deployment_id: int, *, trigger: str = "manual") -> None:
    """Background entrypoint. Owns its OWN session — the request-scoped session is
    already closed by the time this runs. Never raises."""
    async with SessionLocal() as session:
        dep = await session.get(Deployment, deployment_id)
        if not dep:
            return
        repo = await session.get(Repository, dep.repository_id)
        org = await session.get(Organization, repo.organization_id) if repo and repo.organization_id else None
        if not repo or not repo.project_slug or not org:
            _touch(dep, status="failed", error="Repository, slug, or organization missing.")
            await session.commit()
            return

        slug = repo.project_slug
        async with _lock_for(slug):
            try:
                await _do_deploy(session, dep, repo, org, slug)
            except DeployError as e:
                _touch(dep, status="failed", error=str(e)[:8000])
                await session.commit()
            except Exception as e:  # noqa: BLE001 — never let a deploy crash the task
                _touch(dep, status="failed", error=f"Unexpected error: {e}"[:8000])
                await session.commit()


async def _do_deploy(session: AsyncSession, dep: Deployment, repo: Repository,
                     org: Organization, slug: str) -> None:
    primary = (await session.execute(
        select(Domain).where(Domain.is_primary == True)  # noqa: E712
    )).scalar_one_or_none()
    if not primary:
        raise DeployError("No primary domain configured. Add one under Domains first.")
    web_host = f"{slug}.{primary.name}"

    _touch(dep, status="cloning", url=f"https://{web_host}", error=None)
    await session.commit()

    sha = await sync_source(repo, decrypted_pat(org))
    rd = repo_dir(slug)
    _touch(dep, status="building", commit_sha=sha)
    await session.commit()

    mode = detect_build_mode(rd)
    if mode == "compose":
        compose_path, web_service = neutralize_compose(rd, slug, web_host)
    elif mode == "dockerfile":
        compose_path, web_service = generate_compose_for_dockerfile(rd, slug, web_host)
    else:
        image = await buildpack_build(rd, slug)
        compose_path, web_service = generate_compose_for_image(rd, slug, image, web_host)

    _touch(dep, status="starting")
    await session.commit()

    stack = stack_name(slug)
    code, out = await _run(
        ["docker", "compose", "-p", stack, "-f", str(compose_path),
         "up", "-d", "--build", "--remove-orphans"],
        cwd=str(rd),
    )
    tail = out[-8000:]
    if code:
        raise DeployError(f"docker compose up failed:\n{tail}")

    web_container = await _discover_web_container(stack, web_service)
    _touch(dep, status="running", web_container=web_container, log_tail=tail, error=None)
    await session.commit()


async def teardown_stack(slug: str) -> tuple[bool, str]:
    """Stop + remove a project's containers and networks. Keeps named volumes
    (no -v) so data survives a reconnect/redeploy. Best-effort."""
    stack = stack_name(slug)
    code, out = await _run(["docker", "compose", "-p", stack, "down", "--remove-orphans"], timeout=120)
    return code == 0, out[-2000:]
