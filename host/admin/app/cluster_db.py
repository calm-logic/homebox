"""Active-active app databases via pgEdge Spock.

Projects opt in through homebox.yaml:

    cluster:
      enabled: true

At deploy time (deploy._assemble_stack), every compose-origin Postgres service
in an opted-in project is transformed:

  - image swapped to the pgEdge Postgres build (Spock + snowflake
    preinstalled; multi-arch, so amd64 Linux and arm64 Macs interoperate)
  - init scripts delivered as compose `configs` with inline content (never
    host bind mounts — the admin's in-container paths don't exist on the
    host on macOS, where the base dir is ~/homebox); they configure logical
    replication and create the spock node on first boot, with the node's
    advertised DSN = <this node's LAN IP>:<deterministic port>
  - the DB port is published on the host so peer nodes can subscribe
  - a replication role with a password derived from the cluster secret
    (HMAC — every node derives the same one, nothing to sync)

After `compose up` (and again from clusterlib's reconcile loop, which heals
ordering — e.g. a peer that deployed later), ensure_replication() adds public
tables to the default replication set and creates subscriptions to every
peer's copy of the same database.

Deliberate semantics: spock DDL replication is OFF. Every homebox node deploys
the same code and runs the same migrations, so schemas converge by
construction and only DML replicates — this avoids double-applied DDL
stalling subscriptions. Conflicts resolve last-update-wins. Tables without a
primary key go to the insert-only replication set. Plain serial PKs WILL
collide across nodes — apps should use UUIDs or snowflake sequences (the
extension is installed).
"""

import asyncio
import hashlib
import hmac
import logging
import zlib
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger("homebox.cluster.db")

PGEDGE_IMAGE = "ghcr.io/pgedge/pgedge-postgres:{major}-spock5-standard"
SUPPORTED_MAJORS = ("16", "17", "18")
DEFAULT_MAJOR = "16"
PORT_BASE = 54000
PORT_SPAN = 1000


# ───── manifest / detection ───────────────────────────────────────────────────


def read_cluster_manifest(rd: Path) -> dict[str, Any]:
    """The top-level `cluster:` block of homebox.yaml ({} when absent)."""
    from .manifest import find_manifest
    path = find_manifest(rd)
    if not path:
        return {}
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except (yaml.YAMLError, OSError):
        return {}
    block = data.get("cluster")
    return block if isinstance(block, dict) else {}


def cluster_db_enabled(rd: Path) -> bool:
    return bool(read_cluster_manifest(rd).get("enabled"))


def _norm_env(svc: dict[str, Any]) -> dict[str, str]:
    env = svc.get("environment")
    out: dict[str, str] = {}
    if isinstance(env, dict):
        out = {str(k): "" if v is None else str(v) for k, v in env.items()}
    elif isinstance(env, list):
        for item in env:
            if isinstance(item, str) and "=" in item:
                k, v = item.split("=", 1)
                out[k] = v
    return out


def _pg_major(image: str) -> str:
    """Best-effort major version from an image ref like postgres:16-alpine."""
    tag = image.split(":", 1)[1] if ":" in image else ""
    for major in SUPPORTED_MAJORS:
        if tag.startswith(major):
            return major
    return DEFAULT_MAJOR


def is_postgres_image(image: str) -> bool:
    name = image.split(":", 1)[0].lower()
    return "postgres" in name or "postgis" in name


# ───── deterministic cluster-wide values ──────────────────────────────────────


def db_port(project_name: str, env_name: str, svc_name: str) -> int:
    """Host port for a replicated DB — identical on every node by construction."""
    key = f"{project_name}:{env_name}:{svc_name}".encode()
    return PORT_BASE + (zlib.crc32(key) % PORT_SPAN)


def derive_repl_password(cluster_secret: str, project_name: str, env_name: str, svc_name: str) -> str:
    """Replication-role password every node derives identically from the shared
    cluster secret — nothing extra to sync or store."""
    msg = f"pgedge:{project_name}:{env_name}:{svc_name}".encode()
    return hmac.new(cluster_secret.encode(), msg, hashlib.sha256).hexdigest()[:32]


def ordered_roster(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Roster sorted by join time — the basis for stable node ordinals
    (snowflake.node_id, spock node names n1/n2/…)."""
    return sorted(state.get("roster") or [], key=lambda n: (n.get("joined_at") or 0, n.get("node_id")))


def node_ordinal(state: dict[str, Any], node_id: str) -> int:
    for i, n in enumerate(ordered_roster(state)):
        if n.get("node_id") == node_id:
            return i + 1
    return 1


def peer_host(peer: dict[str, Any]) -> str | None:
    url = (peer.get("peer_url") or "").strip()
    if not url:
        return None
    host = url.split("://", 1)[-1].split("/", 1)[0]
    return host.split(":", 1)[0] or None


# ───── compose transform ──────────────────────────────────────────────────────

_INIT_SCRIPTS = {
    # Runs inside the pgEdge image's initdb hook on FIRST boot (empty volume).
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
snowflake.node_id = '${NODE_ORDINAL}'
EOF
""",
    "30-restart.sh": """#!/usr/bin/env bash
set -Eeo pipefail
pg_ctl -D "$PGDATA" -m fast restart
""",
    "40-extensions.sh": """#!/usr/bin/env bash
set -Eeo pipefail
for EXT in spock snowflake; do
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

EXISTS=$(psql -tA -U "$ADMIN" -d "$DB" -c "SELECT 1 FROM pg_roles WHERE rolname='$REPL_USER' LIMIT 1;" || true)
if [ "$EXISTS" != "1" ]; then
  psql -v ON_ERROR_STOP=1 -U "$ADMIN" -d "$DB" -c "CREATE ROLE \\"$REPL_USER\\" LOGIN REPLICATION PASSWORD '$REPL_PASS';"
else
  psql -v ON_ERROR_STOP=1 -U "$ADMIN" -d "$DB" -c "ALTER ROLE \\"$REPL_USER\\" LOGIN REPLICATION PASSWORD '$REPL_PASS';"
fi
psql -v ON_ERROR_STOP=1 -U "$ADMIN" -d "$DB" -c "GRANT pg_read_all_data TO \\"$REPL_USER\\";"
psql -v ON_ERROR_STOP=1 -U "$ADMIN" -d "$DB" -c "GRANT pg_write_all_data TO \\"$REPL_USER\\";"
psql -v ON_ERROR_STOP=1 -U "$ADMIN" -d "$DB" -c "GRANT CREATE, TEMP ON DATABASE \\"$DB\\" TO \\"$REPL_USER\\";"

NODE_EXISTS=$(psql -tA -U "$ADMIN" -d "$DB" -c "SELECT 1 FROM spock.node WHERE node_name='${NODE_NAME}' LIMIT 1;" || true)
if [ "$NODE_EXISTS" != "1" ]; then
  psql -v ON_ERROR_STOP=1 -U "$ADMIN" -d "$DB" -c "SELECT spock.node_create(node_name := '${NODE_NAME}', dsn := 'host=${NODE_DSN_HOST} port=${NODE_DSN_PORT} dbname=${DB} user=${REPL_USER} password=${REPL_PASS}');"
fi
""",
}


def transform_db_service(
    *,
    svc: dict[str, Any],
    svc_name: str,
    rd: Path,
    project_name: str,
    env_name: str,
    state: dict[str, Any],
    self_node_id: str,
    cluster_secret: str,
    top_volumes: dict[str, Any],
    top_configs: dict[str, Any],
) -> dict[str, Any] | None:
    """Mutate a compose-origin Postgres service dict for active-active
    replication. Returns an info dict (port, creds, container hints) or None
    if the service isn't a Postgres we can transform."""
    image = str(svc.get("image") or "")
    if not image or not is_postgres_image(image):
        return None

    env = _norm_env(svc)
    admin_user = env.get("POSTGRES_USER") or "postgres"
    admin_password = env.get("POSTGRES_PASSWORD") or ""
    db_name = env.get("POSTGRES_DB") or admin_user

    major = _pg_major(image)
    port = db_port(project_name, env_name, svc_name)
    repl_password = derive_repl_password(cluster_secret, project_name, env_name, svc_name)
    ordinal = node_ordinal(state, self_node_id)
    me = next((n for n in ordered_roster(state) if n.get("node_id") == self_node_id), None)
    my_host = peer_host(me or {})
    if not my_host:
        log.warning("cluster db: no LAN host for this node; skipping transform of %s", svc_name)
        return None

    # Init scripts ride as compose configs with inline content — the compose
    # CLI materializes them in the container, so no host paths are involved
    # (bind mounts would break on macOS, where the admin's in-container
    # /opt/homebox path doesn't exist on the host). `$` must be escaped as
    # `$$` or compose interpolates it away before the script ever runs.
    svc_configs = []
    for fname, content in _INIT_SCRIPTS.items():
        cfg_name = f"pgedge-{svc_name}-{fname.split('.', 1)[0]}"
        top_configs[cfg_name] = {"content": content.replace("$", "$$")}
        svc_configs.append({
            "source": cfg_name,
            "target": f"/docker-entrypoint-initdb.d/{fname}",
            "mode": 0o755,
        })
    svc["configs"] = svc_configs

    svc["image"] = PGEDGE_IMAGE.format(major=major)
    env.update({
        "POSTGRES_USER": admin_user,
        "POSTGRES_PASSWORD": admin_password,
        "POSTGRES_DB": db_name,
        "PGEDGE_USER": "pgedge",
        "PGEDGE_PASSWORD": repl_password,
        "NODE_NAME": f"n{ordinal}",
        "NODE_ORDINAL": str(ordinal),
        "NODE_DSN_HOST": my_host,
        "NODE_DSN_PORT": str(port),
    })
    svc["environment"] = env
    # Publish for peer subscriptions (md5-authed; the WireGuard mesh from the
    # design doc later tucks this off the open LAN).
    svc["ports"] = [f"{port}:5432"]
    # pgEdge keeps PGDATA under /var/lib/pgsql — give it a dedicated volume
    # (an existing postgres volume at /var/lib/postgresql/data would be
    # invisible to it anyway; cluster-enabling a project starts a fresh DB).
    vol_name = f"{svc_name}-pgedge"
    vols = svc.get("volumes")
    vols = list(vols) if isinstance(vols, list) else []
    vols = [v for v in vols if not (isinstance(v, str) and v.endswith(":/var/lib/pgsql"))]
    vols.append(f"{vol_name}:/var/lib/pgsql")
    svc["volumes"] = vols
    top_volumes.setdefault(vol_name, {})

    return {
        "service": svc_name,
        "port": port,
        "db": db_name,
        "admin_user": admin_user,
        "admin_password": admin_password,
        "repl_user": "pgedge",
        "repl_password": repl_password,
        "ordinal": ordinal,
        "node_name": f"n{ordinal}",
    }


def infos_from_compose(rd: Path) -> list[dict[str, Any]]:
    """Recover the replication info for already-deployed DBs from the generated
    compose file — lets the reconcile loop re-ensure wiring without redoing the
    whole deploy pipeline."""
    compose = rd / "docker-compose.homebox.yml"
    try:
        data = yaml.safe_load(compose.read_text()) or {}
    except (yaml.YAMLError, OSError, FileNotFoundError):
        return []
    out: list[dict[str, Any]] = []
    for name, svc in (data.get("services") or {}).items():
        if not isinstance(svc, dict):
            continue
        image = str(svc.get("image") or "")
        if not image.startswith("ghcr.io/pgedge/"):
            continue
        env = _norm_env(svc)
        ports = svc.get("ports") or []
        port = None
        for p in ports:
            if isinstance(p, str) and p.endswith(":5432"):
                port = int(p.split(":", 1)[0])
        if not port or not env.get("PGEDGE_PASSWORD"):
            continue
        admin_user = env.get("POSTGRES_USER") or "postgres"
        out.append({
            "service": name,
            "port": port,
            "db": env.get("POSTGRES_DB") or admin_user,
            "admin_user": admin_user,
            "admin_password": env.get("POSTGRES_PASSWORD") or "",
            "repl_user": env.get("PGEDGE_USER") or "pgedge",
            "repl_password": env["PGEDGE_PASSWORD"],
            "ordinal": int(env.get("NODE_ORDINAL") or 1),
            "node_name": env.get("NODE_NAME") or "n1",
        })
    return out


# ───── post-up wiring (also called from the cluster reconcile loop) ───────────


async def _psql(container: str, admin_user: str, admin_password: str, db: str, sql: str,
                timeout: int = 30) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "docker", "exec", "-e", f"PGPASSWORD={admin_password}", container,
        "psql", "-U", admin_user, "-d", db, "-tA", "-v", "ON_ERROR_STOP=1", "-c", sql,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "psql timed out"
    return proc.returncode or 0, out.decode("utf-8", "replace").strip()


async def ensure_replication(
    *,
    stack: str,
    info: dict[str, Any],
    state: dict[str, Any],
    self_node_id: str,
) -> dict[str, Any]:
    """Idempotently wire this node's DB into the cluster mesh: default-repset
    membership for all public tables + one subscription per peer. Safe to call
    every reconcile cycle; failures are reported, not raised (a peer that
    hasn't deployed yet is normal)."""
    container = f"{stack}-{info['service']}-1"
    admin, pw, db = info["admin_user"], info["admin_password"], info["db"]
    result: dict[str, Any] = {"container": container, "tables_added": [], "subs_created": [],
                              "errors": []}

    # 1. spock node must exist (init script creates it on first boot)
    code, out = await _psql(container, admin, pw, db,
                            "SELECT node_name FROM spock.node LIMIT 1;")
    if code != 0 or not out:
        result["errors"].append(f"spock node not ready on {container}: {out[:200]}")
        return result

    # 2. add public tables to the replication set (PK tables → default,
    #    PK-less tables → insert-only)
    # spock.tables lists every table; set_name is NULL until it joins a repset.
    code, out = await _psql(container, admin, pw, db,
                            "SELECT relname FROM spock.tables "
                            "WHERE nspname='public' AND set_name IS NULL;")
    if code == 0 and out:
        for table in [x for x in out.splitlines() if x.strip()]:
            code, hp = await _psql(container, admin, pw, db, f"""
                SELECT 1 FROM pg_index i
                JOIN pg_class c ON c.oid = i.indrelid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname='public' AND c.relname='{table}' AND i.indisprimary LIMIT 1;
            """)
            repset = "default" if (code == 0 and hp.strip() == "1") else "default_insert_only"
            code, aout = await _psql(
                container, admin, pw, db,
                f"SELECT spock.repset_add_table('{repset}', 'public.{table}', false);",
            )
            if code == 0:
                result["tables_added"].append(f"{table}→{repset}")
            else:
                result["errors"].append(f"repset add {table}: {aout[:200]}")

    # 3. subscriptions to every peer
    code, out = await _psql(container, admin, pw, db, "SELECT sub_name FROM spock.subscription;")
    existing_subs = set(out.splitlines()) if code == 0 else set()
    my_ord = info["ordinal"]
    for i, peer in enumerate(ordered_roster(state)):
        if peer.get("node_id") == self_node_id:
            continue
        p_ord = i + 1
        host = peer_host(peer)
        if not host:
            continue
        sub = f"sub_n{my_ord}_n{p_ord}"
        if sub in existing_subs:
            continue
        dsn = (f"host={host} port={info['port']} dbname={db} "
               f"user={info['repl_user']} password={info['repl_password']}")
        code, sout = await _psql(
            container, admin, pw, db,
            f"SELECT spock.sub_create(subscription_name := '{sub}', provider_dsn := '{dsn}');",
            timeout=60,
        )
        if code == 0:
            result["subs_created"].append(sub)
            log.info("cluster db: created %s on %s → %s:%s", sub, container, host, info["port"])
        else:
            # Peer likely not deployed/reachable yet — the reconcile loop retries.
            result["errors"].append(f"{sub}: {sout[:200]}")
    return result
