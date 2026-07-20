"""Shared bootstrap rendering for cloud database VMs (EC2 / GCE).

A "db VM" target runs the SAME pgEdge Postgres container the homebox cluster
uses (cluster_db.PGEDGE_IMAGE) on a dedicated cloud VM that joins the
cluster's WireGuard mesh. Everything interesting happens in the user-data
script rendered here:

  - docker (get.docker.com) + wireguard tools
  - /etc/wireguard/wg0.conf with the mesh address the orchestrator allocated
    (10.77.x.y from the ordinal) and one [Peer] per homebox node. The peers
    carry NO address to dial: only the VM has a stable public IP, so homebox
    nodes dial the VM (state.mesh.endpoint → targetslib.mesh_extra_peers →
    meshlib), and the VM's PersistentKeepalive holds the tunnels open.
  - the pgEdge init scripts — a verbatim template of app/cluster_db's
    _INIT_SCRIPTS semantics for a standalone `docker run` — bind-mounted
    into /docker-entrypoint-initdb.d
  - `docker run` of the pgEdge image; homebox nodes then subscribe to it via
    spock exactly like any peer (targetslib.db_vm_extra_nodes →
    cluster_db.ensure_replication).

Pure string rendering, no I/O — deterministic so tests can golden-assert.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from typing import Any

from ..cluster_db import DEFAULT_MAJOR, PGEDGE_IMAGE
from .base import TargetError

WG_PORT = 51820
PG_PORT = 5432
INIT_DIR = "/opt/homebox-db/init"
CONTAINER_NAME = "homebox-db"
READY_MARKER = "/var/lib/homebox-db-ready"


def mesh_ip_for(ordinal: int) -> str:
    """The VM's mesh address for its (reserved-range) ordinal.

    MUST stay in lockstep with meshlib.mesh_ip — the 3-line formula is
    duplicated here rather than imported because meshlib sits in the admin
    app layer and importing it from a provider module risks an import cycle.
    """
    return f"10.77.{(ordinal >> 8) & 0xff}.{ordinal & 0xff}"


@dataclass
class DbVmSpec:
    """Everything render_cloud_init needs. Built from ctx.config by
    spec_from_config; the orchestrator (deploy.py) supplies the values."""

    ordinal: int                 # mesh ordinal (>= targetslib.MESH_ORDINAL_BASE)
    mesh_ip: str                 # 10.77.x.y — derived from the ordinal
    wg_private_key: str          # the VM's own WireGuard key
    wg_peers: list[dict[str, Any]] = field(default_factory=list)
    #                            ^ [{public_key, allowed_ips}] — homebox nodes
    pg_image: str = ""           # cluster_db.PGEDGE_IMAGE, formatted
    db_name: str = "postgres"
    admin_user: str = "postgres"
    admin_password: str = ""
    repl_user: str = "pgedge"
    repl_password: str = ""
    node_name: str = ""          # spock node name, f"n{ordinal}"
    open_pg_public: bool = False  # publish 5432 beyond the mesh (serverless)


def spec_from_config(config: dict[str, Any]) -> DbVmSpec:
    """Build the DbVmSpec from a service_targets.config dict. The deploy
    orchestrator must have allocated the mesh identity and derived the DB
    credentials first — missing pieces are a clear TargetError, not a
    half-provisioned VM."""
    config = config or {}
    try:
        ordinal = int(config["mesh_ordinal"])
    except (KeyError, TypeError, ValueError):
        raise TargetError(
            "database VM config is missing mesh_ordinal — the deploy "
            "orchestrator must allocate a mesh ordinal before provisioning."
        ) from None
    wg_private = str(config.get("wg_private_key") or "")
    wg_public = str(config.get("wg_public_key") or "")
    if not wg_private or not wg_public:
        raise TargetError(
            "database VM config is missing its WireGuard keypair "
            "(wg_private_key / wg_public_key)."
        )
    db = config.get("db") or {}
    admin_user = str(db.get("admin_user") or "postgres")
    db_name = str(db.get("db_name") or db.get("name") or admin_user)
    admin_password = str(db.get("admin_password") or "")
    repl_user = str(db.get("repl_user") or "pgedge")
    repl_password = str(db.get("repl_password") or "")
    if not admin_password or not repl_password:
        raise TargetError(
            "database VM config.db must include admin_password and "
            "repl_password (derive_repl_password) — refusing to boot an "
            "openly-reachable Postgres without them."
        )
    pg_image = str(config.get("pg_image") or "") or PGEDGE_IMAGE.format(
        major=str(config.get("pg_major") or DEFAULT_MAJOR)
    )
    return DbVmSpec(
        ordinal=ordinal,
        mesh_ip=str(config.get("mesh_ip") or "") or mesh_ip_for(ordinal),
        wg_private_key=wg_private,
        wg_peers=list(config.get("wg_peers") or []),
        pg_image=pg_image,
        db_name=db_name,
        admin_user=admin_user,
        admin_password=admin_password,
        repl_user=repl_user,
        repl_password=repl_password,
        node_name=f"n{ordinal}",
        open_pg_public=bool(config.get("open_pg_public")),
    )


def vm_state_entries(
    spec: DbVmSpec, *, wg_public_key: str, public_ip: str
) -> dict[str, Any]:
    """THE single derivation of the state halves the cluster wiring reads:
    targetslib.mesh_extra_peers consumes state["mesh"] and
    targetslib.db_vm_extra_nodes consumes state["db"] — keep in lockstep."""
    return {
        "mesh": {
            "ordinal": spec.ordinal,
            "ip": spec.mesh_ip,
            "wg_pubkey": wg_public_key,
            "endpoint": f"{public_ip}:{WG_PORT}",
        },
        "db": {"port": PG_PORT, "node_name": spec.node_name},
    }


# ───── pgEdge init scripts ────────────────────────────────────────────────────
#
# A verbatim template of app/cluster_db._INIT_SCRIPTS (keep in lockstep —
# same preload libs, same postgresql.conf block, same extension/role/node
# bootstrap) with two differences for the standalone-VM case:
#   - ${NODE_ORDINAL} / ${NODE_NAME} are baked in at render time (no compose
#     environment to expand them; POSTGRES_* / PGEDGE_* stay container env)
#   - the spock node DSN advertises localhost:5432 (identical semantics:
#     peers never read it; subscriptions carry an explicit provider_dsn
#     built from state.mesh by cluster_db.ensure_replication).
# Note lolor.node MUST land in postgresql.conf — ALTER SYSTEM rejects the
# prefix until the module is loaded in the issuing backend.

_INIT_SCRIPT_TEMPLATES: dict[str, str] = {
    "10-preload.sh": """#!/usr/bin/env bash
set -Eeo pipefail
PGCONF="$PGDATA/postgresql.conf"
LIBS="pg_stat_statements,snowflake,spock"
if grep -q '^[ ]*shared_preload_libraries' "$PGCONF"; then
  sed -i "s|^[ ]*shared_preload_libraries.*|shared_preload_libraries = '$LIBS'|" "$PGCONF"
else
  echo "shared_preload_libraries = '$LIBS'" >> "$PGCONF"
fi
""",
    "20-pgconf.sh": """#!/usr/bin/env bash
set -Eeo pipefail
PGCONF="$PGDATA/postgresql.conf"
cat >> "$PGCONF" <<EOF
listen_addresses = '*'
wal_level = 'logical'
max_worker_processes = 16
max_replication_slots = 16
max_wal_senders = 16
track_commit_timestamp = 'on'
spock.conflict_resolution = 'last_update_wins'
spock.save_resolutions = 'on'
# Homebox clusters: every node deploys + migrates the same app itself, so
# schemas converge by construction. Only DML replicates.
spock.enable_ddl_replication = 'off'
# GUC name varies across snowflake extension versions — set both.
snowflake.node = '${NODE_ORDINAL}'
snowflake.node_id = '${NODE_ORDINAL}'
# lolor (large-object logical replication): new LO oids are node-encoded from
# this value, so concurrent lo_create on different nodes can't collide. Must
# live in postgresql.conf — ALTER SYSTEM rejects the prefix until the module
# is loaded in the issuing backend.
lolor.node = '${NODE_ORDINAL}'
EOF
""",
    "30-restart.sh": """#!/usr/bin/env bash
set -Eeo pipefail
pg_ctl -D "$PGDATA" -m fast restart
""",
    "40-extensions.sh": """#!/usr/bin/env bash
set -Eeo pipefail
# lolor reroutes the lo_* API into replicable lolor.* tables (native
# pg_largeobject is a catalog and never travels over logical replication).
for EXT in spock snowflake lolor; do
  psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" \\
    -c "CREATE EXTENSION IF NOT EXISTS \\"$EXT\\";"
done
""",
    "50-hba.sh": """#!/usr/bin/env bash
set -Eeo pipefail
echo "host all all 0.0.0.0/0 md5" >> "$PGDATA/pg_hba.conf"
echo "host all all ::/0 md5"      >> "$PGDATA/pg_hba.conf"
pg_ctl -D "$PGDATA" -m fast reload
""",
    "60-node.sh": """#!/usr/bin/env bash
set -Eeo pipefail
DB="${POSTGRES_DB:-postgres}"
ADMIN="${POSTGRES_USER:-postgres}"
export PGPASSWORD="${POSTGRES_PASSWORD}"
REPL_USER="${PGEDGE_USER:-pgedge}"
REPL_PASS="${PGEDGE_PASSWORD:?PGEDGE_PASSWORD not set}"

# SUPERUSER: spock's synchronize_data path calls pause/resume_apply_workers
# and drops/creates slots on the PROVIDER as this role — plain REPLICATION +
# read/write-all is not enough. Container-scoped database, container-scoped
# blast radius.
EXISTS=$(psql -tA -U "$ADMIN" -d "$DB" -c "SELECT 1 FROM pg_roles WHERE rolname='$REPL_USER' LIMIT 1;" || true)
if [ "$EXISTS" != "1" ]; then
  psql -v ON_ERROR_STOP=1 -U "$ADMIN" -d "$DB" -c "CREATE ROLE \\"$REPL_USER\\" LOGIN REPLICATION SUPERUSER PASSWORD '$REPL_PASS';"
else
  psql -v ON_ERROR_STOP=1 -U "$ADMIN" -d "$DB" -c "ALTER ROLE \\"$REPL_USER\\" LOGIN REPLICATION SUPERUSER PASSWORD '$REPL_PASS';"
fi

NODE_EXISTS=$(psql -tA -U "$ADMIN" -d "$DB" -c "SELECT 1 FROM spock.node WHERE node_name='${NODE_NAME}' LIMIT 1;" || true)
if [ "$NODE_EXISTS" != "1" ]; then
  # The node's own interface DSN must be dialable FROM THIS CONTAINER —
  # spock validates it during sub_create. Peers never read this DSN
  # (subscriptions carry an explicit provider_dsn built from state.mesh).
  psql -v ON_ERROR_STOP=1 -U "$ADMIN" -d "$DB" -c "SELECT spock.node_create(node_name := '${NODE_NAME}', dsn := 'host=localhost port=5432 dbname=${DB} user=${REPL_USER} password=${REPL_PASS}');"
fi
""",
}


# ───── rendering ──────────────────────────────────────────────────────────────


def _wg_conf(spec: DbVmSpec) -> str:
    blocks = [
        "[Interface]\n"
        f"Address = {spec.mesh_ip}/16\n"
        f"ListenPort = {WG_PORT}\n"
        f"PrivateKey = {spec.wg_private_key}\n"
    ]
    for peer in spec.wg_peers:
        allowed = peer.get("allowed_ips") or ""
        if isinstance(allowed, (list, tuple)):
            allowed = ", ".join(str(a) for a in allowed)
        public_key = peer.get("public_key") or peer.get("wg_pubkey") or ""
        blocks.append(
            "\n[Peer]\n"
            "# a homebox node — nodes dial this VM (only the VM has a stable\n"
            "# public address), so no peer address is pinned here.\n"
            f"PublicKey = {public_key}\n"
            f"AllowedIPs = {allowed}\n"
            "PersistentKeepalive = 25\n"
        )
    return "".join(blocks)


def render_cloud_init(spec: DbVmSpec) -> str:
    """The VM's user-data: a plain `#!/bin/bash` script (cloud-init runs it
    on first boot on both EC2 user data and GCE `user-data` metadata).
    Deterministic — same spec, identical string. Re-runs converge: every
    step is guarded or idempotent, and the script ends by touching
    READY_MARKER."""
    q = shlex.quote

    init_writes: list[str] = []
    for fname, template in _INIT_SCRIPT_TEMPLATES.items():
        content = (
            template
            .replace("${NODE_ORDINAL}", str(spec.ordinal))
            .replace("${NODE_NAME}", spec.node_name)
        )
        init_writes.append(
            f"cat > {INIT_DIR}/{fname} <<'HB_INIT'\n{content}HB_INIT\n"
        )

    env_pairs = [
        ("POSTGRES_USER", spec.admin_user),
        ("POSTGRES_PASSWORD", spec.admin_password),
        ("POSTGRES_DB", spec.db_name),
        ("PGEDGE_USER", spec.repl_user),
        ("PGEDGE_PASSWORD", spec.repl_password),
        # Baked into the init scripts too — kept in the container env for
        # parity with the compose transform and for debuggability.
        ("NODE_NAME", spec.node_name),
        ("NODE_ORDINAL", str(spec.ordinal)),
    ]
    env_flags = " \\\n    ".join(f"-e {q(f'{k}={v}')}" for k, v in env_pairs)

    header = (
        "#!/bin/bash\n"
        "# =============================================================================\n"
        "# Homebox cloud DB VM bootstrap (rendered by app/targets/db_vm_common.py)\n"
        "# =============================================================================\n"
        "# Boots the same pgEdge Postgres container the homebox cluster runs (see\n"
        "# app/cluster_db.py) and joins the cluster's WireGuard mesh. Idempotent:\n"
        "# re-running converges. Logged to /var/log/homebox-db-bootstrap.log.\n"
        "set -Eeuo pipefail\n"
        "exec > >(tee -a /var/log/homebox-db-bootstrap.log) 2>&1\n"
        "echo \"=== homebox-db bootstrap $(date -u +%FT%TZ) ===\"\n"
        "\n"
    )

    packages = (
        "# ── 1. docker + wireguard tools ──────────────────────────────────────────────\n"
        "if ! command -v docker >/dev/null 2>&1; then\n"
        "  curl -fsSL https://get.docker.com | sh\n"
        "fi\n"
        "systemctl enable --now docker 2>/dev/null || true\n"
        "if ! command -v wg >/dev/null 2>&1; then\n"
        "  if command -v apt-get >/dev/null 2>&1; then\n"
        "    export DEBIAN_FRONTEND=noninteractive\n"
        "    apt-get update -qq || true\n"
        "    apt-get install -y wireguard-tools\n"
        "  elif command -v dnf >/dev/null 2>&1; then\n"
        "    dnf install -y wireguard-tools\n"
        "  else\n"
        "    yum install -y wireguard-tools\n"
        "  fi\n"
        "fi\n"
        "\n"
    )

    wireguard = (
        "# ── 2. join the homebox WireGuard mesh ───────────────────────────────────────\n"
        "umask 077\n"
        "mkdir -p /etc/wireguard\n"
        "cat > /etc/wireguard/wg0.conf <<'HB_WG'\n"
        f"{_wg_conf(spec)}"
        "HB_WG\n"
        "umask 022\n"
        "systemctl enable wg-quick@wg0\n"
        "systemctl restart wg-quick@wg0\n"
        "\n"
    )

    init_scripts = (
        "# ── 3. pgEdge init scripts (template of app/cluster_db._INIT_SCRIPTS) ───────\n"
        f"mkdir -p {INIT_DIR}\n"
        f"chmod 700 {INIT_DIR.rsplit('/', 1)[0]}\n"
        + "".join(init_writes)
        + f"chmod 0755 {INIT_DIR}/*.sh\n"
        "\n"
    )

    docker_run = (
        "# ── 4. the database container ────────────────────────────────────────────────\n"
        "# 5432 binds 0.0.0.0 on purpose: reachability is the security group /\n"
        "# firewall's job (mesh-only by default, public for serverless consumers).\n"
        f"if ! docker inspect {CONTAINER_NAME} >/dev/null 2>&1; then\n"
        "  docker volume create pgdata >/dev/null\n"
        f"  docker run -d --restart unless-stopped --name {CONTAINER_NAME} \\\n"
        f"    -p {PG_PORT}:5432 \\\n"
        "    -v pgdata:/var/lib/pgsql \\\n"
        f"    -v {INIT_DIR}:/docker-entrypoint-initdb.d \\\n"
        f"    {env_flags} \\\n"
        f"    {q(spec.pg_image)}\n"
        "fi\n"
        "\n"
        f"touch {READY_MARKER}\n"
        "echo \"=== homebox-db bootstrap complete ===\"\n"
    )

    return header + packages + wireguard + init_scripts + docker_run
