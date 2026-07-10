from pathlib import Path
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    database_url: str = "postgresql+asyncpg://homebox_admin:homebox_admin@db:5432/homebox_admin"
    app_secret: str = "dev-secret-change-me"
    encryption_key: str = "0" * 64
    homebox_base_dir: Path = Path("/host/homebox")
    # Deployed-project working tree. Bind-mounted at the SAME path on host and
    # inside the admin container so docker-compose bind mounts resolve identically
    # on both sides (the daemon interprets host paths). See app/deploy.py.
    projects_host_dir: Path = Path("/opt/homebox/projects")
    homebox_secrets_path: Path = Path("/host/secrets/secrets.json")
    homebox_admin_username: str = "homebox"
    dashboard_auth: str = ""
    homebox_domain: str = ""        # primary root domain seeded at install
    admin_domain: str = ""          # full FQDN of the admin (homebox.<root>)
    session_cookie: str = "homebox_session"
    session_max_age_seconds: int = 60 * 60 * 24 * 14  # 14 days
    homebox_oauth_proxy_url: str = "https://oauth.homebox.sh"
    homebox_control_plane_url: str = "https://control.homebox.sh"
    homebox_site_url: str = "https://homebox.sh"  # marketing/cloud site (env HOMEBOX_SITE_URL)
    # This node's cluster role. "peer" nodes serve app traffic active-active and
    # count toward the license max_nodes. "mirror" nodes run drained (standby),
    # stay hot via replication + peer deploys, and auto-promote to serving when
    # every non-mirror peer goes unhealthy. Mirrors don't count toward max_nodes.
    node_role: str = "peer"        # peer | mirror  (env HOMEBOX_NODE_ROLE)

    @field_validator("node_role")
    @classmethod
    def _valid_node_role(cls, v: str) -> str:
        v = (v or "peer").strip().lower()
        if v not in ("peer", "mirror"):
            raise ValueError("HOMEBOX_NODE_ROLE must be 'peer' or 'mirror'")
        return v


settings = Settings()


# ── Cluster key override ──────────────────────────────────────────────────────
# When this node joins a cluster it adopts the CLUSTER's encryption key and app
# secret (so encrypted blobs and login sessions work identically on every node).
# The container's env still carries the install-time values, and recreating the
# container from compose would resurrect them — so the join flow writes the
# cluster keys to a file under the (host-mounted, RW) admin dir and we override
# the env here on every boot. A plain `docker restart` is enough to apply.
CLUSTER_KEYS_FILE = settings.homebox_base_dir / "admin" / "cluster-keys.json"


def _apply_cluster_keys() -> None:
    try:
        import json
        data = json.loads(CLUSTER_KEYS_FILE.read_text())
    except (FileNotFoundError, ValueError, OSError):
        return
    if isinstance(data, dict):
        if data.get("encryption_key"):
            settings.encryption_key = str(data["encryption_key"])
        if data.get("app_secret"):
            settings.app_secret = str(data["app_secret"])


_apply_cluster_keys()
