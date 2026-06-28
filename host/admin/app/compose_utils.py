"""Shared helpers for reading docker-compose files — used by both the
dissection engine (app/dissect.py) and the deploy engine (app/deploy.py)."""

from pathlib import Path
from typing import Any

COMPOSE_NAMES = ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")

# Services likely to be the public HTTP entrypoint, in preference order.
WEB_NAME_HINTS = ("web", "app", "frontend", "ui", "client", "site")
API_NAME_HINTS = ("api", "server", "backend", "gateway")
# image substring -> service kind
_DB_IMAGES = ("postgres", "postgis", "mysql", "mariadb", "mongo", "cockroach", "timescale")
_CACHE_IMAGES = ("redis", "memcached", "valkey", "keydb")
_QUEUE_IMAGES = ("rabbitmq", "kafka", "nats", "zookeeper")
_WORKER_NAME_HINTS = ("worker", "celery", "queue", "sidekiq", "scheduler", "beat", "cron", "consumer")


def find_compose(rd: Path) -> Path | None:
    for name in COMPOSE_NAMES:
        if (rd / name).is_file():
            return rd / name
    return None


def label_value(svc: dict[str, Any], key: str) -> str | None:
    """Read a compose label by key, tolerating both the dict and list forms."""
    labels = svc.get("labels")
    if isinstance(labels, dict):
        v = labels.get(key)
        return str(v) if v is not None else None
    if isinstance(labels, list):
        for item in labels:
            if isinstance(item, str) and item.startswith(f"{key}="):
                return item.split("=", 1)[1]
    return None


def detect_port(svc: dict[str, Any]) -> int:
    """Best guess at the container port a service listens on."""
    explicit = label_value(svc, "homebox.port")
    if explicit and explicit.isdigit():
        return int(explicit)
    for expose in (svc.get("expose") or []):
        s = str(expose).split("/")[0]
        if s.isdigit():
            return int(s)
    for p in (svc.get("ports") or []):
        s = str(p).rsplit(":", 1)[-1].split("/")[0]
        if s.isdigit():
            return int(s)
    return 80


def env_map(svc: dict[str, Any]) -> dict[str, str]:
    """Normalize a compose service's `environment` (dict or list form) to a dict."""
    env = svc.get("environment")
    out: dict[str, str] = {}
    if isinstance(env, dict):
        for k, v in env.items():
            out[str(k)] = "" if v is None else str(v)
    elif isinstance(env, list):
        for item in env:
            if isinstance(item, str) and "=" in item:
                k, v = item.split("=", 1)
                out[k] = v
            elif isinstance(item, str):
                out[item] = ""
    return out


def depends_on_list(svc: dict[str, Any]) -> list[str]:
    """Normalize compose `depends_on` (list or map form) to a list of names."""
    dep = svc.get("depends_on")
    if isinstance(dep, list):
        return [str(d) for d in dep]
    if isinstance(dep, dict):
        return [str(d) for d in dep.keys()]
    return []


def image_of(svc: dict[str, Any]) -> str:
    return str(svc.get("image") or "").lower()


def classify_kind(name: str, svc: dict[str, Any]) -> str:
    """Classify a compose service into a Homebox service kind."""
    n = name.lower()
    img = image_of(svc)
    explicit = (label_value(svc, "homebox.kind") or "").lower()
    if explicit in ("web", "api", "database", "cache", "worker", "static", "other"):
        return explicit
    if any(d in img or d in n for d in _DB_IMAGES):
        return "database"
    if any(c in img or c in n for c in _CACHE_IMAGES):
        return "cache"
    if any(q in img or q in n for q in _QUEUE_IMAGES):
        return "worker"
    if any(w in n for w in _WORKER_NAME_HINTS):
        return "worker"
    if any(a in n for a in API_NAME_HINTS):
        return "api"
    if any(w == n or w in n for w in WEB_NAME_HINTS):
        return "web"
    # Has a build/Dockerfile and exposes a port → assume an HTTP app.
    if (svc.get("build") or svc.get("ports") or svc.get("expose")):
        return "web"
    return "other"
