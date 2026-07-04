"""Helpers for reading/writing the mounted /opt/homebox tree from inside the
admin container. The host's /opt/homebox is bind-mounted at /host/homebox.
Also exposes a thin Docker-socket client for inspecting/restarting our own
infrastructure containers."""

import json
import socket
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from .config import settings


BASE = settings.homebox_base_dir
DOMAINS_FILE = BASE / "base-infrastructure" / "domains.json"
INFRA_ENV_FILE = BASE / "base-infrastructure" / ".env"
RUNNER_DIR = BASE / "actions-runner"
TRAEFIK_DYNAMIC = BASE / "traefik" / "dynamic_conf.yml"
PROJECTS_DIR = BASE / "projects"


def read_domains() -> list[dict[str, Any]]:
    if not DOMAINS_FILE.exists():
        return []
    try:
        return json.loads(DOMAINS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return []


def write_domains(domains: list[dict[str, Any]]) -> None:
    DOMAINS_FILE.parent.mkdir(parents=True, exist_ok=True)
    DOMAINS_FILE.write_text(json.dumps(domains, indent=2) + "\n")


def write_infra_env(values: dict[str, str]) -> None:
    INFRA_ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{k}={v}" for k, v in values.items()]
    INFRA_ENV_FILE.write_text("\n".join(lines) + "\n")


def runner_status() -> dict[str, Any]:
    info: dict[str, Any] = {"installed": False}
    runner_marker = RUNNER_DIR / ".runner"
    if runner_marker.exists():
        info["installed"] = True
        try:
            info["registration"] = json.loads(runner_marker.read_text())
        except (json.JSONDecodeError, OSError):
            info["registration"] = {}
    return info


DOCKER_SOCK = "/var/run/docker.sock"


def _docker_request(method: str, path: str) -> tuple[int, bytes]:
    """Tiny Docker socket client. Returns (status, body)."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(DOCKER_SOCK)
        req = (
            f"{method} {path} HTTP/1.1\r\n"
            "Host: docker\r\n"
            "Accept: application/json\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode()
        sock.sendall(req)
        chunks = []
        while True:
            data = sock.recv(8192)
            if not data:
                break
            chunks.append(data)
    finally:
        sock.close()
    raw = b"".join(chunks)
    head, _, body = raw.partition(b"\r\n\r\n")
    status_line = head.split(b"\r\n", 1)[0].decode("ascii", "ignore")
    parts = status_line.split(" ", 2)
    status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    return status, body


def container_status(name: str) -> dict[str, Any]:
    """Returns {state, status, exists}. state is 'running'|'exited'|'missing'."""
    try:
        code, body = _docker_request("GET", f"/containers/{quote(name)}/json")
    except (FileNotFoundError, ConnectionError, OSError):
        return {"exists": False, "state": "unknown", "status": "docker socket unavailable"}
    if code == 404:
        return {"exists": False, "state": "missing", "status": "not deployed"}
    if code != 200:
        return {"exists": False, "state": "unknown", "status": f"docker {code}"}
    # body is chunked-transfer-encoded — find first '{' and last '}'.
    try:
        text = body.decode("utf-8", "ignore")
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end < 0:
            return {"exists": True, "state": "unknown", "status": "parse error"}
        data = json.loads(text[start : end + 1])
    except (ValueError, json.JSONDecodeError):
        return {"exists": True, "state": "unknown", "status": "parse error"}
    state = (data.get("State") or {}).get("Status") or "unknown"
    return {
        "exists": True,
        "state": state,
        "status": (data.get("State") or {}).get("Status", state),
        "running": state == "running",
        "started_at": (data.get("State") or {}).get("StartedAt"),
        "image": (data.get("Config") or {}).get("Image"),
    }


def container_stats(name: str) -> dict[str, Any] | None:
    """One-shot resource sample for a container via Docker's stats API
    (?stream=false returns a single object with precpu populated, so CPU% is
    computable). Returns {cpu_pct, mem_used, mem_limit, net_rx, net_tx} or None
    if the container is missing/unreadable. net_* are cumulative byte counters."""
    try:
        code, body = _docker_request("GET", f"/containers/{quote(name)}/stats?stream=false")
    except (FileNotFoundError, ConnectionError, OSError):
        return None
    if code != 200:
        return None
    try:
        text = body.decode("utf-8", "ignore")
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end < 0:
            return None
        data = json.loads(text[start : end + 1])
    except (ValueError, json.JSONDecodeError):
        return None

    cpu = data.get("cpu_stats") or {}
    precpu = data.get("precpu_stats") or {}
    cpu_total = (cpu.get("cpu_usage") or {}).get("total_usage", 0)
    pre_total = (precpu.get("cpu_usage") or {}).get("total_usage", 0)
    cpu_delta = cpu_total - pre_total
    system_delta = (cpu.get("system_cpu_usage", 0) or 0) - (precpu.get("system_cpu_usage", 0) or 0)
    online = cpu.get("online_cpus") or len((cpu.get("cpu_usage") or {}).get("percpu_usage") or []) or 1
    cpu_pct = (cpu_delta / system_delta) * online * 100.0 if system_delta > 0 and cpu_delta > 0 else 0.0

    mem = data.get("memory_stats") or {}
    mem_used = mem.get("usage", 0) or 0
    # Subtract page cache when reported (matches `docker stats`).
    cache = (mem.get("stats") or {}).get("cache", 0) or 0
    mem_used = max(mem_used - cache, 0)
    mem_limit = mem.get("limit", 0) or 0

    net_rx = net_tx = 0
    for iface in (data.get("networks") or {}).values():
        net_rx += iface.get("rx_bytes", 0) or 0
        net_tx += iface.get("tx_bytes", 0) or 0

    return {
        "cpu_pct": round(cpu_pct, 2),
        "mem_used": mem_used,
        "mem_limit": mem_limit,
        "net_rx": net_rx,
        "net_tx": net_tx,
    }


def restart_container(name: str, timeout_seconds: int = 10) -> tuple[bool, str]:
    try:
        code, body = _docker_request("POST", f"/containers/{quote(name)}/restart?t={timeout_seconds}")
    except (FileNotFoundError, ConnectionError, OSError) as e:
        return False, f"docker socket unavailable: {e}"
    if code in (204, 200):
        return True, "restarted"
    return False, f"docker returned {code}: {body[:200]!r}"


def remove_container(name: str, force: bool = True) -> tuple[bool, str]:
    try:
        code, body = _docker_request("DELETE", f"/containers/{quote(name)}?force={'1' if force else '0'}&v=1")
    except (FileNotFoundError, ConnectionError, OSError) as e:
        return False, f"docker socket unavailable: {e}"
    if code in (204, 200, 404):
        return True, "removed"
    return False, f"docker returned {code}: {body[:200]!r}"


def _docker_request_json(method: str, path: str, payload: dict | None = None) -> tuple[int, bytes]:
    """Same as _docker_request but with optional JSON body."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(DOCKER_SOCK)
        body_bytes = b""
        if payload is not None:
            body_bytes = json.dumps(payload).encode("utf-8")
        req = (
            f"{method} {path} HTTP/1.1\r\n"
            "Host: docker\r\n"
            "Accept: application/json\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body_bytes)}\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode() + body_bytes
        sock.sendall(req)
        chunks = []
        while True:
            data = sock.recv(8192)
            if not data:
                break
            chunks.append(data)
    finally:
        sock.close()
    raw = b"".join(chunks)
    head, _, body = raw.partition(b"\r\n\r\n")
    status_line = head.split(b"\r\n", 1)[0].decode("ascii", "ignore")
    parts = status_line.split(" ", 2)
    status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    return status, body


def list_runner_containers() -> list[dict[str, Any]]:
    """List running/stopped homebox-runner-* containers."""
    try:
        code, body = _docker_request("GET", '/containers/json?all=1&filters={"name":["homebox-runner-"]}')
    except (FileNotFoundError, ConnectionError, OSError):
        return []
    if code != 200:
        return []
    text = body.decode("utf-8", "ignore")
    start = text.find("[")
    end = text.rfind("]")
    if start < 0 or end < 0:
        return []
    try:
        data = json.loads(text[start : end + 1])
    except (ValueError, json.JSONDecodeError):
        return []
    out = []
    for c in data:
        names = c.get("Names") or []
        name = (names[0] if names else "").lstrip("/")
        if not name.startswith("homebox-runner-"):
            continue
        out.append({
            "name": name,
            "org": name[len("homebox-runner-"):],
            "state": c.get("State"),
            "running": c.get("State") == "running",
            "image": c.get("Image"),
            "started_at": None,
        })
    return out


def run_runner_container(
    *,
    name: str,
    org: str,
    runner_token: str,
    runner_name: str,
    labels: list[str],
    image: str = "myoung34/github-runner:latest",
) -> tuple[bool, str]:
    """Create + start a github-runner container. The runner image takes its
    config via env vars; once it has REGISTRATION_TOKEN it self-registers."""
    payload = {
        "Image": image,
        "Env": [
            f"REPO_URL=https://github.com/{org}",
            f"RUNNER_NAME={runner_name}",
            f"RUNNER_TOKEN={runner_token}",
            f"RUNNER_WORKDIR=/tmp/runner",
            f"RUNNER_GROUP=Default",
            f"LABELS={','.join(labels)}",
            "ORG_RUNNER=true",
            f"ORG_NAME={org}",
            "EPHEMERAL=false",
            "DISABLE_AUTO_UPDATE=true",
        ],
        "HostConfig": {
            "RestartPolicy": {"Name": "unless-stopped"},
            "Binds": ["/var/run/docker.sock:/var/run/docker.sock"],
            "NetworkMode": "traefik-net",
        },
    }
    # Pull image first (best-effort).
    try:
        _docker_request_json("POST", f"/images/create?fromImage={quote(image)}", None)
    except OSError:
        pass

    code, body = _docker_request_json("POST", f"/containers/create?name={quote(name)}", payload)
    if code not in (200, 201):
        # If it exists already, remove and retry once
        if code == 409:
            remove_container(name)
            code, body = _docker_request_json("POST", f"/containers/create?name={quote(name)}", payload)
            if code not in (200, 201):
                return False, f"create failed: {code} {body[:200]!r}"
        else:
            return False, f"create failed: {code} {body[:200]!r}"

    code, body = _docker_request("POST", f"/containers/{quote(name)}/start")
    if code in (204, 304):
        return True, "started"
    return False, f"start failed: {code} {body[:200]!r}"


def run_cloudflared_remote(
    connector_token: str,
    *,
    image: str = "cloudflare/cloudflared:latest",
    container_name: str = "homebox-cloudflared",
) -> tuple[bool, str]:
    """(Re)create the homebox-cloudflared container in remotely-managed mode:
    no config volume, just `cloudflared tunnel run --token <token>`. Removes
    any existing container with the same name first."""
    remove_container(container_name)

    payload = {
        "Image": image,
        "Cmd": [
            "tunnel", "--no-autoupdate", "run",
            "--token", connector_token,
        ],
        "User": "0:0",
        "HostConfig": {
            "RestartPolicy": {"Name": "unless-stopped"},
            "NetworkMode": "traefik-net",
        },
    }

    # Best-effort image pull.
    try:
        _docker_request_json("POST", f"/images/create?fromImage={quote(image)}", None)
    except OSError:
        pass

    code, body = _docker_request_json(
        "POST", f"/containers/create?name={quote(container_name)}", payload
    )
    if code not in (200, 201):
        return False, f"create failed: {code} {body[:200]!r}"

    code, body = _docker_request("POST", f"/containers/{quote(container_name)}/start")
    if code in (204, 304):
        return True, "started"
    return False, f"start failed: {code} {body[:200]!r}"


# Internal hostname LAN peers use to reach this node's admin (the peer API)
# through Traefik's already-exposed :80 — no extra published ports. Requests
# carry this as their Host header; it resolves nowhere publicly.
PEER_HOST = "homebox-peer.internal"


def write_traefik_dynamic(routes: list[dict[str, Any]]) -> Path:
    """Rewrite /opt/homebox/traefik/dynamic_conf.yml with the admin route plus
    any additional `routes` from the admin DB. Each route is
    {host, service_url, name}. The cluster peer-API route is always included."""
    routes = [r for r in routes if r["name"] != "homebox-peer"] + [
        {"name": "homebox-peer", "host": PEER_HOST, "service_url": "http://homebox-admin:8000"},
    ]
    dyn = settings.homebox_base_dir / "traefik" / "dynamic_conf.yml"
    dyn.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Managed by Homebox admin — regenerated on changes.",
        "http:",
        "  routers:",
    ]
    for r in routes:
        rname = r["name"]
        lines.append(f"    {rname}:")
        lines.append(f"      rule: \"Host(`{r['host']}`)\"")
        lines.append("      entryPoints:")
        lines.append("        - web")
        lines.append(f"      service: {rname}")
    lines.append("  services:")
    for r in routes:
        rname = r["name"]
        lines.append(f"    {rname}:")
        lines.append("      loadBalancer:")
        lines.append("        servers:")
        lines.append(f"          - url: \"{r['service_url']}\"")
    content = "\n".join(lines) + "\n"
    try:
        unchanged = dyn.read_text() == content
    except OSError:
        unchanged = False
    if unchanged:
        return dyn
    dyn.write_text(content)
    # Traefik watches this file, but single-file bind mounts don't deliver
    # fsnotify events on macOS (VirtioFS) — the routes silently never load.
    # A restart is cheap (<1s) and only happens when the content changed.
    restart_container("homebox-traefik")
    return dyn


