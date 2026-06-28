"""Deterministic project dissection.

Given a checked-out repo, decompose it into Services and figure out how they
connect — i.e. which env vars (DATABASE_URL, REDIS_URL…) each app service needs
to reach its backing services. No LLM; pure heuristics over the compose
file / Dockerfile, reusing app/compose_utils.py.

Output is a list of DetectedService the caller persists as Service +
ServiceEnvVar(source='auto') rows. The deploy engine (app/deploy.py) reads those
back and injects them per environment.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .compose_utils import (
    classify_kind,
    depends_on_list,
    detect_port,
    env_map,
    find_compose,
    image_of,
    label_value,
    WEB_NAME_HINTS,
)


@dataclass
class DetectedService:
    name: str
    kind: str                       # web|api|database|cache|worker|static|other
    source_type: str                # compose|dockerfile|image|buildpack
    source_ref: str | None          # compose svc name / image / "." for Dockerfile
    is_public: bool
    subdomain_label: str            # ""→ main UI, "api"→ <proj>-api, …
    internal_port: int | None
    depends_on: list[str] = field(default_factory=list)
    env_template: dict[str, str] = field(default_factory=dict)
    auto_env: dict[str, str] = field(default_factory=dict)  # wired connection vars


# Default ports by backing-service image family.
_DB_PORTS = [("postgres", 5432), ("postgis", 5432), ("timescale", 5432),
             ("mysql", 3306), ("mariadb", 3306), ("mongo", 27017),
             ("cockroach", 26257)]


def dissect(rd: Path) -> list[DetectedService]:
    """Top-level entry: returns the detected services for a repo dir."""
    compose = find_compose(rd)
    if compose:
        try:
            return _dissect_compose(compose)
        except (yaml.YAMLError, OSError):
            pass  # malformed compose → fall through to single-service modes
    if (rd / "Dockerfile").is_file():
        return [_single_web("dockerfile", ".")]
    return [_single_web("buildpack", None)]


def _single_web(source_type: str, source_ref: str | None) -> DetectedService:
    return DetectedService(
        name="web", kind="web", source_type=source_type, source_ref=source_ref,
        is_public=True, subdomain_label="", internal_port=8080,
    )


def _pick_main(services: dict[str, dict], metas: dict[str, dict]) -> str:
    """Pick the project's primary public entrypoint (gets the bare hostname)."""
    # 1. explicit opt-in
    for name, m in metas.items():
        if m["expose"] and m["kind"] != "database":
            return name
    # 2. a web service by conventional name
    for hint in WEB_NAME_HINTS:
        if hint in services and metas[hint]["kind"] in ("web", "static", "api"):
            return hint
    # 3. first web/static service
    for name, m in metas.items():
        if m["kind"] in ("web", "static"):
            return name
    # 4. first api service
    for name, m in metas.items():
        if m["kind"] == "api":
            return name
    # 5. first service that exposes a port and isn't a backing store
    for name, m in metas.items():
        if m["kind"] not in ("database", "cache") and (m["svc"].get("ports") or m["svc"].get("expose")):
            return name
    # 6. give up — first service
    return next(iter(services))


def _dissect_compose(compose: Path) -> list[DetectedService]:
    data = yaml.safe_load(compose.read_text()) or {}
    services: dict[str, dict] = {n: (s or {}) for n, s in (data.get("services") or {}).items()}
    if not services:
        return [_single_web("buildpack", None)]

    metas: dict[str, dict] = {}
    for name, svc in services.items():
        metas[name] = {
            "kind": classify_kind(name, svc),
            "port": detect_port(svc),
            "depends_on": depends_on_list(svc),
            "env": env_map(svc),
            "expose": (label_value(svc, "homebox.expose") or "").lower() == "true",
            "explicit_label": label_value(svc, "homebox.subdomain"),
            "svc": svc,
        }

    main = _pick_main(services, metas)
    dbs = [n for n, m in metas.items() if m["kind"] == "database"]
    caches = [n for n, m in metas.items() if m["kind"] == "cache"]

    detected: list[DetectedService] = []
    used_labels: set[str] = set()
    for name, m in metas.items():
        kind = m["kind"]
        is_main = name == main
        if is_main:
            is_public = True
            label = ""
        else:
            is_public = m["expose"] or kind in ("web", "api", "static")
            label = m["explicit_label"] or ("api" if kind == "api" else name)
            label = label.strip().lower()
            if label in used_labels:  # de-dupe across public services
                label = name.lower()
            i = 2
            base = label
            while label in used_labels:
                label = f"{base}-{i}"
                i += 1
        if is_public:
            used_labels.add(label)

        auto_env = _connection_env(name, kind, m, metas, dbs, caches)

        detected.append(DetectedService(
            name=name,
            kind=kind,
            source_type="compose" if (m["svc"].get("build")) else ("image" if m["svc"].get("image") else "compose"),
            source_ref=name,
            is_public=is_public,
            subdomain_label=label if is_public else "",
            internal_port=m["port"],
            depends_on=m["depends_on"],
            env_template=m["env"],
            auto_env=auto_env,
        ))
    return detected


def _connection_env(
    name: str, kind: str, meta: dict, metas: dict[str, dict],
    dbs: list[str], caches: list[str],
) -> dict[str, str]:
    """Wire connection env vars for an app service to its backing services."""
    if kind not in ("web", "api", "worker", "other"):
        return {}
    declared = {k.upper() for k in meta["env"].keys()}
    out: dict[str, str] = {}

    # DATABASE_URL — to a depended-on db, or the sole db if unambiguous.
    if "DATABASE_URL" not in declared:
        target = _pick_target(name, meta, dbs)
        if target:
            url = _db_url(target, metas[target])
            if url:
                out["DATABASE_URL"] = url

    # REDIS_URL — same logic for a cache service.
    if "REDIS_URL" not in declared:
        target = _pick_target(name, meta, caches)
        if target:
            out["REDIS_URL"] = f"redis://{target}:6379"

    return out


def _pick_target(app_name: str, meta: dict, candidates: list[str]) -> str | None:
    """Choose which backing service an app connects to: a depended-on one, or
    the sole candidate if there's exactly one."""
    if not candidates:
        return None
    deps = set(meta["depends_on"])
    depended = [c for c in candidates if c in deps]
    if depended:
        return depended[0]
    if len(candidates) == 1:
        return candidates[0]
    return None


def _db_url(svc_name: str, meta: dict) -> str | None:
    img = image_of(meta["svc"])
    env = meta["env"]
    port = next((p for sub, p in _DB_PORTS if sub in img), 5432)
    if "mysql" in img or "mariadb" in img:
        user = env.get("MYSQL_USER") or "root"
        pw = env.get("MYSQL_PASSWORD") or env.get("MYSQL_ROOT_PASSWORD") or ""
        db = env.get("MYSQL_DATABASE") or user
        return f"mysql://{user}:{pw}@{svc_name}:{port}/{db}"
    if "mongo" in img:
        user = env.get("MONGO_INITDB_ROOT_USERNAME")
        pw = env.get("MONGO_INITDB_ROOT_PASSWORD")
        db = env.get("MONGO_INITDB_DATABASE") or ""
        auth = f"{user}:{pw}@" if user else ""
        return f"mongodb://{auth}{svc_name}:{port}/{db}"
    # default: postgres
    user = env.get("POSTGRES_USER") or "postgres"
    pw = env.get("POSTGRES_PASSWORD") or "postgres"
    db = env.get("POSTGRES_DB") or user
    return f"postgresql://{user}:{pw}@{svc_name}:{port}/{db}"
