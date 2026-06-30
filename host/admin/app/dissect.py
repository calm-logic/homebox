"""Deterministic project dissection.

Decompose a checked-out repo into Services and wire their connection env vars,
combining four sources (highest precedence last):

  1. Top-level sub-app scan  — buildable projects in server/, ui/, api/, … dirs.
  2. Repo-root build detection — Nixpacks/Dockerfile/SPA at the repo root.
  3. docker-compose scan       — backing services (db/cache/…) + any app services
                                 declared there with build:/web images.
  4. homebox.yaml (manifest)   — explicit declarations that override the rest.

Key correctness rules:
  - A database/cache/worker is NEVER auto-published.
  - If no app (web/api/static) service is found at all, nothing is published —
    the deploy then fails with a clear "no web service detected" message rather
    than serving a datastore (the old bug).

Each DetectedService carries either origin="compose" (deploy reuses the compose
definition) or origin="build" (deploy generates/builds it). No LLM.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import manifest as mf
from .compose_utils import (
    classify_kind,
    depends_on_list,
    detect_port,
    env_map,
    find_compose,
    image_of,
    label_value,
    API_NAME_HINTS,
    WEB_NAME_HINTS,
)

# Top-level dirs that may hold an independently-buildable sub-app, with the kind
# we assume for one found there.
_SUBAPP_DIRS = {
    "server": "api", "backend": "api", "api": "api", "service": "api", "services": "api",
    "ui": "web", "frontend": "web", "web": "web", "client": "web", "www": "web", "site": "web",
    "app": "web", "apps": "web",
}
# Files that mark a directory as a buildable project, → (stack, default port).
_BUILD_MARKERS: list[tuple[str, str, int]] = [
    ("Dockerfile", "dockerfile", 8080),
    ("Cargo.toml", "rust", 8080),
    ("go.mod", "go", 8080),
    ("pyproject.toml", "python", 8000),
    ("requirements.txt", "python", 8000),
    ("Gemfile", "ruby", 3000),
    ("pom.xml", "java", 8080),
    ("package.json", "node", 3000),
]
_DB_PORTS = [("postgres", 5432), ("postgis", 5432), ("timescale", 5432),
             ("mysql", 3306), ("mariadb", 3306), ("mongo", 27017), ("cockroach", 26257)]


@dataclass
class DetectedService:
    name: str
    kind: str                       # web|api|database|cache|worker|static|other
    origin: str                     # "compose" (reuse compose def) | "build" (generate)
    is_public: bool = False
    subdomain_label: str = ""       # ""→ main UI, "api"→ <proj>-api, …
    internal_port: int | None = None
    depends_on: list[str] = field(default_factory=list)
    env_template: dict[str, str] = field(default_factory=dict)
    auto_env: dict[str, str] = field(default_factory=dict)
    # build info (origin == "build")
    build_type: str | None = None   # dockerfile | nixpacks | image | static
    build_dir: str | None = None    # context dir relative to repo root
    dockerfile: str | None = None
    image: str | None = None
    static_dir: str | None = None   # for static: built-assets dir (relative to build_dir)
    build_command: str | None = None
    build_image: str | None = None  # builder base image for static (default node)
    command: str | None = None

    @property
    def is_app(self) -> bool:
        return self.kind in ("web", "api", "static")

    @property
    def is_backing(self) -> bool:
        return self.kind in ("database", "cache", "worker")


# ── repo / dir build detection ────────────────────────────────────────────────

def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _node_build_command(d: Path, build_script: str = "build") -> str:
    """Install deps with the repo's package manager, then run its build script."""
    if (d / "pnpm-lock.yaml").is_file():
        return f"corepack enable && pnpm install --frozen-lockfile && pnpm run {build_script}"
    if (d / "yarn.lock").is_file():
        return f"corepack enable && yarn install --frozen-lockfile && yarn {build_script}"
    if (d / "package-lock.json").is_file():
        return f"npm ci && npm run {build_script}"
    return f"npm install && npm run {build_script}"


def _node_app(d: Path) -> dict[str, Any] | None:
    """Inspect a package.json dir: returns build hints, distinguishing a static
    SPA (build → static dir) from a server-side Node app (nixpacks)."""
    pkg = _read_json(d / "package.json")
    if not pkg:
        return None
    scripts = pkg.get("scripts") or {}
    deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
    has_build = "build" in scripts
    is_vite = "vite" in deps or (d / "vite.config.ts").exists() or (d / "vite.config.js").exists()
    is_cra = "react-scripts" in deps
    has_server_start = any(s in scripts for s in ("start", "serve")) and not is_vite and not is_cra
    if has_build and (is_vite or is_cra):
        return {"build_type": "static", "kind": "web",
                "static_dir": "dist" if is_vite else "build",
                "build_command": _node_build_command(d), "port": 80}
    # SSR / API node app → let nixpacks build + run it.
    if has_server_start or scripts:
        return {"build_type": "nixpacks", "kind": "web", "port": 3000}
    return None


def _detect_buildable(rd: Path, rel: str) -> DetectedService | None:
    """Return a build DetectedService for a directory, or None if not buildable."""
    d = rd / rel if rel != "." else rd
    if not d.is_dir():
        return None
    name = "web" if rel == "." else Path(rel).name.lower()

    # Node gets special handling (SPA vs server).
    if (d / "package.json").is_file():
        hints = _node_app(d)
        if hints:
            return DetectedService(
                name=name, kind=hints["kind"], origin="build",
                build_type=hints["build_type"], build_dir=rel,
                static_dir=hints.get("static_dir"), build_command=hints.get("build_command"),
                internal_port=hints.get("port"),
            )
    # Dockerfile or another language marker → nixpacks (or dockerfile build).
    for marker, stack, port in _BUILD_MARKERS:
        if (d / marker).is_file():
            if stack == "dockerfile":
                return DetectedService(name=name, kind="web", origin="build",
                                       build_type="dockerfile", build_dir=rel, internal_port=port)
            return DetectedService(name=name, kind="web", origin="build",
                                   build_type="nixpacks", build_dir=rel, internal_port=port)
    return None


def _scan_subapps(rd: Path) -> dict[str, DetectedService]:
    out: dict[str, DetectedService] = {}
    for sub, kind in _SUBAPP_DIRS.items():
        svc = _detect_buildable(rd, sub)
        if svc:
            svc.kind = kind if kind != "web" or svc.kind != "static" else "static"
            # keep static classification for SPA dirs; otherwise use the dir's role
            if svc.build_type != "static":
                svc.kind = kind
            out[svc.name] = svc
    return out


# ── compose scan ──────────────────────────────────────────────────────────────

def _scan_compose(rd: Path) -> dict[str, DetectedService]:
    compose = find_compose(rd)
    if not compose:
        return {}
    try:
        data = yaml.safe_load(compose.read_text()) or {}
    except (yaml.YAMLError, OSError):
        return {}
    services = data.get("services") or {}
    out: dict[str, DetectedService] = {}
    for name, svc in services.items():
        svc = svc or {}
        kind = classify_kind(name, svc)
        out[str(name)] = DetectedService(
            name=str(name), kind=kind, origin="compose",
            internal_port=detect_port(svc),
            depends_on=depends_on_list(svc),
            env_template=env_map(svc),
            # an app service can be declared in compose too (has build: or web image)
            build_type="compose",
        )
    return out


# ── manifest overlay ──────────────────────────────────────────────────────────

def _from_manifest(m: mf.ManifestService) -> DetectedService:
    build_type = m.build or ("image" if m.image else "nixpacks")
    kind = m.kind or ("api" if (m.subdomain or "").lower() == "api" else
                      "static" if build_type == "static" else "web")
    return DetectedService(
        name=m.name, kind=kind, origin="build",
        is_public=bool(m.public) if m.public is not None else False,
        subdomain_label=(m.subdomain or ""),
        internal_port=m.port or (80 if build_type == "static" else None),
        depends_on=m.depends_on, env_template=m.env,
        build_type=build_type, build_dir=m.dir, dockerfile=m.dockerfile,
        image=m.image, static_dir=m.static_dir, build_command=m.build_command,
        build_image=m.build_image, command=m.command,
    )


# ── connection env wiring ─────────────────────────────────────────────────────

def _db_url(svc: DetectedService) -> str | None:
    env = svc.env_template
    name = svc.name
    img = ""  # kind already says database; infer flavor from env keys / name
    blob = (name + " " + " ".join(env.keys())).lower()
    if "mysql" in blob or "maria" in blob:
        user = env.get("MYSQL_USER") or "root"
        pw = env.get("MYSQL_PASSWORD") or env.get("MYSQL_ROOT_PASSWORD") or ""
        db = env.get("MYSQL_DATABASE") or user
        return f"mysql://{user}:{pw}@{name}:3306/{db}"
    if "mongo" in blob:
        user = env.get("MONGO_INITDB_ROOT_USERNAME")
        pw = env.get("MONGO_INITDB_ROOT_PASSWORD")
        db = env.get("MONGO_INITDB_DATABASE") or ""
        auth = f"{user}:{pw}@" if user else ""
        return f"mongodb://{auth}{name}:27017/{db}"
    user = env.get("POSTGRES_USER") or "postgres"
    pw = env.get("POSTGRES_PASSWORD") or "postgres"
    db = env.get("POSTGRES_DB") or user
    return f"postgresql://{user}:{pw}@{name}:5432/{db}"


def _wire_env(services: dict[str, DetectedService]) -> None:
    dbs = [s for s in services.values() if s.kind == "database"]
    caches = [s for s in services.values() if s.kind == "cache"]

    def pick(app: DetectedService, candidates: list[DetectedService]) -> DetectedService | None:
        if not candidates:
            return None
        deps = set(app.depends_on)
        depended = [c for c in candidates if c.name in deps]
        if depended:
            return depended[0]
        return candidates[0] if len(candidates) == 1 else None

    for app in services.values():
        # Static SPAs run in the browser — no server-side connection vars.
        if app.kind not in ("web", "api", "worker", "other") or app.build_type == "static":
            continue
        declared = {k.upper() for k in app.env_template.keys()}
        if "DATABASE_URL" not in declared:
            db = pick(app, dbs)
            if db:
                url = _db_url(db)
                if url:
                    app.auto_env["DATABASE_URL"] = url
        if "REDIS_URL" not in declared:
            cache = pick(app, caches)
            if cache:
                app.auto_env["REDIS_URL"] = f"redis://{cache.name}:6379"


# ── public-service selection ──────────────────────────────────────────────────

def _assign_public(services: dict[str, DetectedService], manifest: mf.Manifest | None) -> None:
    apps = [s for s in services.values() if s.is_app]
    # Honour explicit manifest public flags first.
    manifest_public = {
        n for n, m in (manifest.services.items() if manifest else [])
        if m.public is True
    }
    for s in services.values():
        s.is_public = s.name in manifest_public  # reset; backing stays False

    if not apps:
        return  # nothing to publish — deploy will error clearly

    # Main entrypoint (bare host): a manifest-flagged main, else prefer web/static.
    def rank(s: DetectedService) -> int:
        if s.subdomain_label == "" and s.is_public:
            return 0
        return {"web": 1, "static": 1, "api": 2}.get(s.kind, 3)

    main = sorted(apps, key=lambda s: (rank(s), s.name))[0]
    used: set[str] = set()
    for s in apps:
        s.is_public = True
        if s is main:
            s.subdomain_label = ""
        else:
            label = s.subdomain_label or ("api" if s.kind == "api" else s.name)
            base, i = label, 2
            while label in used:
                label = f"{base}-{i}"; i += 1
            s.subdomain_label = label
        if s.subdomain_label:
            used.add(s.subdomain_label)


# ── entry point ───────────────────────────────────────────────────────────────

def dissect(rd: Path) -> list[DetectedService]:
    services: dict[str, DetectedService] = {}

    # Lowest → highest precedence; later sources overwrite same-named services.
    services.update(_scan_subapps(rd))
    root = _detect_buildable(rd, ".")
    if root:
        services.setdefault(root.name, root)
    services.update(_scan_compose(rd))  # backing services authoritative

    manifest = mf.parse_manifest(rd)
    if manifest:
        for name, m in manifest.services.items():
            services[name] = _from_manifest(m)

    # If the compose declared an app service (web/api with build:), keep it as a
    # compose-origin app. Otherwise apps come from build detection above.

    # No app at all and nothing buildable found, but a compose existed with only
    # backing services → still no app (deploy errors). If there's NOTHING at all,
    # fall back to a single buildpack web from the repo root.
    if not services:
        services["web"] = DetectedService(
            name="web", kind="web", origin="build",
            build_type="nixpacks", build_dir=".", internal_port=8080,
        )

    _wire_env(services)
    _assign_public(services, manifest)
    return list(services.values())
