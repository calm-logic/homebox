"""Optional `homebox.yaml` — explicit service declaration.

Everything is optional. When present, manifest services override anything
auto-detected of the same name (the manifest is the highest-priority source in
app/dissect.py). A minimal example for a Vite frontend + Rust API + the
backing services from docker-compose.yml:

    name: infinitescroll          # optional; defaults to the project slug
    services:
      web:
        build: static             # build the SPA and serve the static output
        build_command: npm run build
        static_dir: dist
        public: true              # bare host: <project>.<domain>
      api:
        build: nixpacks           # auto-detect the build (Rust here)
        dir: server
        port: 8080
        public: true
        subdomain: api            # <project>-api.<domain>
        depends_on: [postgres, redis]
      # postgres/redis are still picked up from docker-compose.yml — no need to
      # redeclare them unless you want to override something.

build values: dockerfile | nixpacks | image | static. Backing services
(postgres/redis/…) are normally left to the compose scan.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

MANIFEST_NAMES = ("homebox.yaml", "homebox.yml", ".homebox.yaml", ".homebox.yml")
_VALID_BUILD = ("dockerfile", "nixpacks", "image", "static")


@dataclass
class ManifestService:
    name: str
    kind: str | None = None
    build: str | None = None
    dir: str = "."
    dockerfile: str | None = None
    image: str | None = None
    static_dir: str | None = None
    build_command: str | None = None
    build_image: str | None = None
    command: str | None = None
    port: int | None = None
    public: bool | None = None
    subdomain: str | None = None
    # Also route <main host><path_prefix> to this service (e.g. "/api" so the
    # SPA can call same-origin /api). "" disables the auto heuristic.
    path_prefix: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)


@dataclass
class Manifest:
    services: dict[str, ManifestService]
    name: str | None = None


def find_manifest(rd: Path) -> Path | None:
    for name in MANIFEST_NAMES:
        if (rd / name).is_file():
            return rd / name
    return None


def _coerce_service(name: str, raw: dict[str, Any]) -> ManifestService:
    build = (raw.get("build") or None)
    if isinstance(build, str):
        build = build.strip().lower()
        if build not in _VALID_BUILD:
            build = None
    env = raw.get("env") or {}
    if not isinstance(env, dict):
        env = {}
    deps = raw.get("depends_on") or []
    if isinstance(deps, dict):
        deps = list(deps.keys())
    elif not isinstance(deps, list):
        deps = []
    port = raw.get("port")
    try:
        port = int(port) if port is not None else None
    except (TypeError, ValueError):
        port = None
    return ManifestService(
        name=name,
        kind=(raw.get("kind") or None),
        build=build,
        dir=str(raw.get("dir") or "."),
        dockerfile=raw.get("dockerfile"),
        image=raw.get("image"),
        static_dir=raw.get("static_dir"),
        build_command=raw.get("build_command"),
        build_image=raw.get("build_image"),
        command=raw.get("command"),
        port=port,
        public=raw.get("public"),
        subdomain=raw.get("subdomain"),
        path_prefix=(str(raw["path_prefix"]) if raw.get("path_prefix") is not None else None),
        env={str(k): "" if v is None else str(v) for k, v in env.items()},
        depends_on=[str(d) for d in deps],
    )


def parse_manifest(rd: Path) -> Manifest | None:
    """Parse homebox.yaml if present and well-formed, else None (auto-detect)."""
    path = find_manifest(rd)
    if not path:
        return None
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except (yaml.YAMLError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    raw_services = data.get("services") or {}
    if not isinstance(raw_services, dict):
        return None
    services: dict[str, ManifestService] = {}
    for name, raw in raw_services.items():
        if isinstance(raw, dict):
            services[str(name)] = _coerce_service(str(name), raw)
    return Manifest(services=services, name=data.get("name"))
