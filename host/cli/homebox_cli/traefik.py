"""Read / write Traefik dynamic_conf.yml on the remote Host."""

from __future__ import annotations

import yaml
import paramiko

from homebox_cli.ssh import read_remote_file, write_remote_file


def _ensure_structure(conf: dict) -> dict:
    """Guarantee the top-level http.routers / http.services keys exist."""
    http = conf.setdefault("http", {})
    if http.get("routers") is None:
        http["routers"] = {}
    if http.get("services") is None:
        http["services"] = {}
    return conf


def set_dev_route(
    client: paramiko.SSHClient,
    conf_path: str,
    *,
    project: str,
    domain: str,
    devbox_ip: str,
    port: int,
) -> None:
    """Point *project.domain* at the Devbox IP in the file provider config."""
    raw = read_remote_file(client, conf_path)
    conf = _ensure_structure(yaml.safe_load(raw) or {})

    conf["http"]["routers"][project] = {
        "rule": f"Host(`{project}.{domain}`)",
        "service": project,
        "entryPoints": ["web"],
    }
    conf["http"]["services"][project] = {
        "loadBalancer": {
            "servers": [{"url": f"http://{devbox_ip}:{port}"}],
        },
    }

    write_remote_file(client, conf_path, yaml.dump(conf, default_flow_style=False))


def set_pub_route(
    client: paramiko.SSHClient,
    conf_path: str,
    *,
    project: str,
) -> None:
    """Remove the file-provider route for *project* so the Docker-label
    route on the publisher container takes precedence."""
    raw = read_remote_file(client, conf_path)
    conf = _ensure_structure(yaml.safe_load(raw) or {})

    conf["http"]["routers"].pop(project, None)
    conf["http"]["services"].pop(project, None)

    write_remote_file(client, conf_path, yaml.dump(conf, default_flow_style=False))
