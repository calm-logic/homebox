"""Manage the ~/.homebox.json configuration file."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

CONFIG_PATH = Path.home() / ".homebox.json"

# Defaults that match the host-provisioner layout
DEFAULT_TRAEFIK_CONF = "/opt/homebox/traefik/dynamic_conf.yml"
DEFAULT_PROJECTS_DIR = "/opt/homebox/projects"


@dataclass
class HomeboxConfig:
    host_ip: str = ""
    ssh_user: str = ""
    domain: str = ""
    ssh_key_path: str = str(Path.home() / ".ssh" / "id_rsa")
    traefik_conf_path: str = DEFAULT_TRAEFIK_CONF
    projects_dir: str = DEFAULT_PROJECTS_DIR
    projects: dict[str, dict] = field(default_factory=dict)

    # -- persistence --------------------------------------------------------

    def save(self) -> None:
        CONFIG_PATH.write_text(json.dumps(asdict(self), indent=2) + "\n")

    @classmethod
    def load(cls) -> "HomeboxConfig":
        if not CONFIG_PATH.exists():
            raise SystemExit(
                f"Config not found at {CONFIG_PATH}. Run `homebox init` first."
            )
        data = json.loads(CONFIG_PATH.read_text())
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
