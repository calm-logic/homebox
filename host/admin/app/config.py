from pathlib import Path
from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", populate_by_name=True)

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
    # NOTE: docker-compose passes HOMEBOX_NODE_ROLE into the container, so the
    # field needs the explicit alias — the bare field name would only bind
    # NODE_ROLE and the role would silently stay "peer".
    node_role: str = Field(
        "peer", validation_alias=AliasChoices("HOMEBOX_NODE_ROLE", "NODE_ROLE"))

    # ── Mirror failover tuning ────────────────────────────────────────────────
    # Fast-probe loop (mirror role only): probe every N seconds, promote after
    # M consecutive failures. Defaults give ~8-10s detection; the slow cluster
    # loop remains as a backstop and owns demotion (which should stay slow so a
    # flapping home connection doesn't bounce traffic).
    mirror_probe_interval: float = Field(
        2.0, validation_alias=AliasChoices("HOMEBOX_MIRROR_PROBE_INTERVAL",
                                           "MIRROR_PROBE_INTERVAL"))
    mirror_probe_failures: int = Field(
        3, validation_alias=AliasChoices("HOMEBOX_MIRROR_PROBE_FAILURES",
                                         "MIRROR_PROBE_FAILURES"))
    # Warm-pool density mode: while drained, keep app containers created but
    # STOPPED (databases keep running — they are live Spock subscribers);
    # promotion starts them before the connector comes up.
    mirror_cold_apps: bool = Field(
        False, validation_alias=AliasChoices("HOMEBOX_MIRROR_COLD_APPS",
                                             "MIRROR_COLD_APPS"))
    # WireGuard port this node ADVERTISES to the control plane. The mesh always
    # listens on 51820 locally; a warm-pool mirror runs inside a container whose
    # host publishes some other UDP port, so it must advertise that one.
    wg_advertise_port: int = Field(
        51820, validation_alias=AliasChoices("HOMEBOX_WG_ADVERTISE_PORT",
                                             "WG_ADVERTISE_PORT"))

    @field_validator("node_role")
    @classmethod
    def _valid_node_role(cls, v: str) -> str:
        v = (v or "peer").strip().lower()
        if v not in ("peer", "mirror"):
            raise ValueError("HOMEBOX_NODE_ROLE must be 'peer' or 'mirror'")
        return v

    @field_validator("mirror_probe_interval")
    @classmethod
    def _valid_probe_interval(cls, v: float) -> float:
        if not (0.5 <= v <= 300):
            raise ValueError("HOMEBOX_MIRROR_PROBE_INTERVAL must be 0.5-300 seconds")
        return v

    @field_validator("mirror_probe_failures")
    @classmethod
    def _valid_probe_failures(cls, v: int) -> int:
        if not (1 <= v <= 100):
            raise ValueError("HOMEBOX_MIRROR_PROBE_FAILURES must be 1-100")
        return v

    @field_validator("wg_advertise_port")
    @classmethod
    def _valid_wg_port(cls, v: int) -> int:
        if not (1 <= v <= 65535):
            raise ValueError("HOMEBOX_WG_ADVERTISE_PORT must be a valid port")
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
