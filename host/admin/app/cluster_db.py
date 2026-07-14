"""Active-active app databases via pgEdge Spock.

Projects opt in through homebox.yaml:

    cluster:
      enabled: true

At deploy time (deploy._assemble_stack), every compose-origin Postgres service
in an opted-in project is transformed:

  - image swapped to the pgEdge Postgres build (Spock + snowflake + lolor
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

Large objects: native pg_largeobject is a system catalog and never travels
over logical replication, so the lolor extension is installed everywhere —
it transparently reroutes the lo_* API into replicable lolor.* tables (added
to the default repset by ensure_replication), with new LO oids node-encoded
via `lolor.node = <spock ordinal>` so concurrent creates can't collide.
Caveats inherited from lolor: ALTER/GRANT/COMMENT ON LARGE OBJECT are
unsupported, and LOs created BEFORE the extension landed stay in the native
catalog (node-local) until migrated.
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


def db_replication_mode(rd: Path) -> str:
    """'auto' or 'off'. Replication is ON BY DEFAULT for clustered nodes —
    apps need no config to run correctly in either mode (a non-replicated
    Postgres behind a shared tunnel splits its data across nodes, which is
    never what anyone wants). homebox.yaml opts OUT:

        cluster:
          enabled: false        # or
          database: none        # or database: {replication: none}
    """
    block = read_cluster_manifest(rd)
    if block.get("enabled") is False:
        return "off"
    dbb = block.get("database")
    if isinstance(dbb, str) and dbb.lower() in ("none", "off", "false"):
        return "off"
    if isinstance(dbb, dict) and str(dbb.get("replication", "")).lower() in ("none", "off", "false"):
        return "off"
    return "auto"


def cluster_db_enabled(rd: Path) -> bool:
    return db_replication_mode(rd) != "off"


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
    """Roster sorted by join time."""
    return sorted(state.get("roster") or [], key=lambda n: (n.get("joined_at") or 0, n.get("node_id")))


def node_ordinal(state: dict[str, Any], node_id: str) -> int:
    """This node's PERMANENT ordinal (spock node name n<ordinal>,
    snowflake.node_id). The control plane assigns it at registration and
    never reuses it — positional fallback only for pre-ordinal rosters."""
    for i, n in enumerate(ordered_roster(state)):
        if n.get("node_id") == node_id:
            return int(n.get("ordinal") or (i + 1))
    return 1


def roster_peer_ordinals(state: dict[str, Any], self_node_id: str) -> dict[int, dict[str, Any]]:
    """ordinal → peer roster entry, for everyone but this node."""
    out: dict[int, dict[str, Any]] = {}
    for i, n in enumerate(ordered_roster(state)):
        if n.get("node_id") == self_node_id:
            continue
        out[int(n.get("ordinal") or (i + 1))] = n
    return out


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
  # spock validates it during sub_create. The host's LAN address is not
  # (hairpin NAT fails on WSL2); peers never read this DSN (subscriptions
  # carry an explicit provider_dsn built from the cluster roster).
  psql -v ON_ERROR_STOP=1 -U "$ADMIN" -d "$DB" -c "SELECT spock.node_create(node_name := '${NODE_NAME}', dsn := 'host=localhost port=5432 dbname=${DB} user=${REPL_USER} password=${REPL_PASS}');"
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

    # Remember the plain-postgres data volume (if any) BEFORE rewriting: it's
    # how the deploy engine detects a single-node → cluster transition that
    # must migrate existing data (see deploy.py). Binary reuse is off the
    # table — alpine(musl) data dirs aren't index-safe under glibc images.
    legacy_volume = None
    for v in (svc.get("volumes") or []):
        if isinstance(v, str) and ":/var/lib/postgresql/data" in v and not v.startswith("/"):
            legacy_volume = v.split(":", 1)[0]

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
        "legacy_volume": legacy_volume,
    }


async def residual_transform(svc: dict[str, Any], svc_name: str, stack: str,
                             top_volumes: dict[str, Any]) -> bool:
    """Cluster → single-node continuity: after a node leaves a cluster, its
    replicated data lives in the pgedge volume. Deploying with the app's
    original plain-postgres image would silently resurrect the stale legacy
    volume — instead keep the pgEdge image (it's plain Postgres plus unused
    extensions) on the pgedge volume: same data, no ports published, no
    replication. Returns True when applied."""
    image = str(svc.get("image") or "")
    if not image or not is_postgres_image(image) or image.startswith("ghcr.io/pgedge/"):
        return False
    vol_name = f"{svc_name}-pgedge"
    if not await volume_exists(f"{stack}_{vol_name}"):
        return False
    svc["image"] = PGEDGE_IMAGE.format(major=_pg_major(image))
    vols = [v for v in (svc.get("volumes") or [])
            if not (isinstance(v, str) and ":/var/lib/postgresql/data" in v)]
    vols.append(f"{vol_name}:/var/lib/pgsql")
    svc["volumes"] = vols
    top_volumes.setdefault(vol_name, {})
    return True


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

    # 1. spock node must exist (init script creates it on first boot). Our
    # sub names derive from the DATABASE's baked node name, not the roster:
    # a node that left and rejoined holds a fresh roster ordinal, but its
    # data volume keeps the spock identity it was initialized with.
    code, out = await _psql(container, admin, pw, db,
                            "SELECT node_name FROM spock.node LIMIT 1;")
    if code != 0 or not out:
        result["errors"].append(f"spock node not ready on {container}: {out[:200]}")
        return result
    local_node_name = out.strip().splitlines()[0]
    try:
        info = {**info, "ordinal": int(local_node_name.lstrip("n"))}
    except ValueError:
        pass

    # 1b. keep the local replication role's password at the CURRENT derived
    # value — the cluster secret can change (cluster re-created / migrated),
    # and the init script only sets it on first boot. Peers derive the same
    # value, so their subscription DSNs authenticate once this converges.
    code, out = await _psql(
        container, admin, pw, db,
        f"ALTER ROLE \"{info['repl_user']}\" WITH LOGIN REPLICATION SUPERUSER "
        f"PASSWORD '{info['repl_password']}';",
    )
    if code != 0:
        result["errors"].append(f"repl role rotate: {out[:200]}")

    # 1c. sequence safety: nextval-style defaults collide across nodes. Bigint
    # sequences are converted to snowflake (node-tagged 64-bit ids — the
    # conversion rewrites the column default to snowflake.nextval). int4
    # serials can't hold snowflake ids — those get a loud warning instead of
    # silent breakage. First make sure the node GUC is set (init scripts
    # written before 2026-07-04 used the wrong name for this extension
    # version; ALTER SYSTEM self-heals existing volumes).
    result.setdefault("warnings", [])
    ord_from_db = info["ordinal"]
    await _psql(container, admin, pw, db,
                f"ALTER SYSTEM SET snowflake.node = '{ord_from_db}';")
    await _psql(container, admin, pw, db, "SELECT pg_reload_conf();")
    code, out = await _psql(container, admin, pw, db, """
        SELECT c.relname || '|' || a.attname || '|' || a.atttypid::regtype::text
               || '|' || COALESCE(pg_get_serial_sequence(quote_ident(n.nspname) || '.' || quote_ident(c.relname), a.attname), '')
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
        LEFT JOIN pg_attrdef d ON d.adrelid = c.oid AND d.adnum = a.attnum
        WHERE n.nspname = 'public' AND c.relkind = 'r'
          AND (pg_get_expr(d.adbin, d.adrelid) LIKE 'nextval(%' OR a.attidentity <> '')
    """)
    if code == 0 and out:
        for line in (x for x in out.splitlines() if x.strip()):
            table, col, coltype, seq = (line.split("|") + ["", "", ""])[:4]
            if coltype in ("bigint", "int8") and seq:
                code, cout = await _psql(
                    container, admin, pw, db,
                    f"SELECT snowflake.convert_sequence_to_snowflake('{seq}');",
                )
                if code == 0:
                    result.setdefault("sequences_converted", []).append(seq)
                    log.info("cluster db: %s converted to snowflake on %s", seq, container)
                # Errors here are usually "already converted" — not worth noise.
            elif coltype in ("integer", "int4", "smallint", "int2"):
                result["warnings"].append(
                    f"{table}.{col} is a 32-bit auto-increment — snowflake ids don't fit, "
                    f"so cross-node inserts CAN collide. Migrate it to bigint or uuid."
                )

    # 1d. lolor: large objects. Native pg_largeobject is a system catalog, so
    # logical replication (and thus Spock) silently skips it — apps storing
    # files as LOs would replicate everything EXCEPT the files. lolor reroutes
    # the lo_* API into replicable lolor.* tables. Init scripts handle fresh
    # volumes; this self-heals databases initialized before lolor support.
    # Skipped quietly on images that don't ship the extension.
    code, out = await _psql(container, admin, pw, db,
                            "SELECT 1 FROM pg_available_extensions WHERE name='lolor';")
    if code == 0 and out.strip() == "1":
        code, out = await _psql(container, admin, pw, db,
                                "CREATE EXTENSION IF NOT EXISTS lolor;")
        if code != 0:
            result["errors"].append(f"lolor extension: {out[:200]}")
        # The node GUC must live in postgresql.conf: ALTER SYSTEM refuses the
        # prefix unless the module is loaded in the issuing backend, and psql
        # runs multi-statement -c strings in one transaction (where ALTER
        # SYSTEM is forbidden), so LOAD-then-ALTER can't ride one _psql call.
        # sed-or-append keeps the value converged with the node's current
        # spock ordinal (leave/rejoin changes it).
        conf_cmd = (
            'if grep -q "^lolor.node" "$PGDATA/postgresql.conf"; then '
            f'sed -i "s|^lolor.node.*|lolor.node = \'{ord_from_db}\'|" "$PGDATA/postgresql.conf"; '
            f'else echo "lolor.node = \'{ord_from_db}\'" >> "$PGDATA/postgresql.conf"; fi'
        )
        code, out = await _docker(["exec", container, "bash", "-c", conf_cmd], timeout=30)
        if code != 0:
            result["errors"].append(f"lolor.node conf: {out[:200]}")
        await _psql(container, admin, pw, db, "SELECT pg_reload_conf();")
    else:
        result["warnings"].append(
            "lolor extension unavailable in this Postgres image — large objects "
            "will NOT replicate; redeploy with a current pgEdge image."
        )

    # 2. add public tables to the replication set (PK tables → default,
    #    PK-less tables → insert-only). Migration-bookkeeping tables NEVER
    #    replicate: every node runs its own migrations (identical rows arise
    #    independently), and copying them breaks a new node's initial sync
    #    with duplicate-key errors. Also evict any that slipped in earlier.
    code, out = await _psql(container, admin, pw, db,
                            "SELECT relname FROM spock.tables "
                            "WHERE nspname='public' AND set_name IS NOT NULL "
                            "AND relname LIKE '%migrations%';")
    if code == 0 and out:
        for table in (x.strip() for x in out.splitlines() if x.strip()):
            await _psql(container, admin, pw, db,
                        f"SELECT spock.repset_remove_table('default', 'public.{table}');")
            await _psql(container, admin, pw, db,
                        f"SELECT spock.repset_remove_table('default_insert_only', 'public.{table}');")
    # spock.tables lists every table; set_name is NULL until it joins a repset.
    code, out = await _psql(container, admin, pw, db,
                            "SELECT relname FROM spock.tables "
                            "WHERE nspname='public' AND set_name IS NULL "
                            "AND relname NOT LIKE '%migrations%';")
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

    # 2b. lolor's storage tables live in schema `lolor`, so the public-schema
    # walk above never sees them — add them explicitly. Both carry PKs
    # (verified against lolor 1.2.2) → multi-writer 'default' repset.
    code, out = await _psql(container, admin, pw, db,
                            "SELECT relname FROM spock.tables "
                            "WHERE nspname='lolor' AND set_name IS NULL;")
    if code == 0 and out:
        for table in (x.strip() for x in out.splitlines() if x.strip()):
            code, aout = await _psql(
                container, admin, pw, db,
                f"SELECT spock.repset_add_table('default', 'lolor.{table}', false);",
            )
            if code == 0:
                result["tables_added"].append(f"lolor.{table}→default")
            else:
                result["errors"].append(f"repset add lolor.{table}: {aout[:200]}")

    # 3. subscriptions to every CURRENT peer …
    code, out = await _psql(container, admin, pw, db, "SELECT sub_name FROM spock.subscription;")
    existing_subs = set(x.strip() for x in out.splitlines() if x.strip()) if code == 0 else set()
    my_ord = info["ordinal"]
    peers = roster_peer_ordinals(state, self_node_id)
    expected = {f"sub_n{my_ord}_n{p_ord}" for p_ord in peers}
    # Prefer the WireGuard overlay for peers whose tunnel is up: across NAT the
    # peer's LAN IP isn't reachable and its Postgres port isn't public, so the
    # 10.77.x.y overlay is the only working replication path. Peers without a
    # live tunnel fall back to their advertised LAN/public host (single-network
    # clusters, where the direct address works, are unaffected).
    from . import meshlib
    overlay_up = await meshlib.mesh_up_ordinals(state)
    # A first subscription made while THIS database is still empty copies the
    # provider's existing rows (synchronize_data) — that's how a fresh node
    # (or a transition peer) receives data that predates the subscription.
    # Non-empty databases subscribe plain: they already share history.
    local_has_data = False
    code, out = await _psql(container, admin, pw, db,
                            "SELECT EXISTS (SELECT 1 FROM pg_stat_user_tables "
                            "WHERE schemaname = 'public' "  # spock's catalogs also count as "user tables"
                            "AND n_live_tup > 0 AND relname NOT LIKE '%migrations%');")
    if code == 0 and out.strip() == "t":
        local_has_data = True
    for p_ord, peer in peers.items():
        if p_ord in overlay_up:
            host, via = meshlib.mesh_ip(p_ord), "mesh"
        else:
            host, via = peer_host(peer), "direct"
        if not host:
            continue
        sub = f"sub_n{my_ord}_n{p_ord}"
        if sub in existing_subs:
            continue
        dsn = (f"host={host} port={info['port']} dbname={db} "
               f"user={info['repl_user']} password={info['repl_password']}")
        sync_clause = "" if local_has_data else ", synchronize_data := true"
        code, sout = await _psql(
            container, admin, pw, db,
            f"SELECT spock.sub_create(subscription_name := '{sub}', provider_dsn := '{dsn}'{sync_clause});",
            timeout=120,
        )
        if code == 0:
            result["subs_created"].append(sub)
            log.info("cluster db: created %s on %s → %s:%s (%s)", sub, container, host, info["port"], via)
        else:
            # Peer likely not deployed/reachable yet — the reconcile loop retries.
            result["errors"].append(f"{sub}: {sout[:200]}")

    # 4. … and NONE to departed ones. Dropping the sub is what stops pulling
    # from a gone peer; peers dropping THEIR subs to us is what releases our
    # WAL slots. Ordinals are permanent, so a stale name can never collide
    # with a live peer.
    for sub in existing_subs:
        if sub.startswith(f"sub_n{my_ord}_") and sub not in expected:
            code, sout = await _psql(container, admin, pw, db,
                                     f"SELECT spock.sub_drop('{sub}');", timeout=60)
            if code == 0:
                result.setdefault("subs_dropped", []).append(sub)
                log.info("cluster db: dropped %s on %s (peer left the roster)", sub, container)
            else:
                result["errors"].append(f"drop {sub}: {sout[:200]}")
    return result


async def _docker(args: list[str], timeout: int = 600, stdin_path: str | None = None,
                  stdout_path: str | None = None) -> tuple[int, str]:
    stdin_f = open(stdin_path, "rb") if stdin_path else None
    stdout_f = open(stdout_path, "wb") if stdout_path else None
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", *args,
            stdin=stdin_f, stdout=stdout_f or asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT if not stdout_f else asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return 124, "docker command timed out"
        blob = (out or b"") + (err or b"")
        return proc.returncode or 0, blob.decode("utf-8", "replace")[-2000:]
    finally:
        if stdin_f:
            stdin_f.close()
        if stdout_f:
            stdout_f.close()


async def volume_exists(name: str) -> bool:
    code, _ = await _docker(["volume", "inspect", name], timeout=15)
    return code == 0


async def dump_database(*, container: str, admin_user: str, admin_password: str,
                        db: str, out_path: Path) -> tuple[bool, str]:
    """pg_dump (custom format) streamed out of the container to a host file —
    no intermediate copies, no tools needed in the admin image."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    code, out = await _docker(
        ["exec", "-e", f"PGPASSWORD={admin_password}", container,
         "pg_dump", "-U", admin_user, "-Fc", db],
        timeout=3600, stdout_path=str(out_path),
    )
    if code != 0 or out_path.stat().st_size == 0:
        return False, out
    return True, f"{out_path.stat().st_size} bytes"


async def restore_database(*, container: str, admin_user: str, admin_password: str,
                           db: str, dump_path: Path) -> tuple[bool, str]:
    """Data-only restore into a freshly-migrated database: truncate app tables
    (replicates to peers), then stream the dump in. --disable-triggers keeps
    FK ordering out of the picture; the COPYs flow through logical decoding,
    so cluster peers receive every row."""
    code, out = await _psql(container, admin_user, admin_password, db, """
        DO $$ DECLARE t text;
        BEGIN
          FOR t IN SELECT tablename FROM pg_tables WHERE schemaname='public'
          LOOP EXECUTE format('TRUNCATE TABLE %I CASCADE', t); END LOOP;
        END $$;
    """, timeout=300)
    if code != 0:
        return False, f"truncate failed: {out[:300]}"
    code, out = await _docker(
        ["exec", "-i", "-e", f"PGPASSWORD={admin_password}", container,
         "pg_restore", "-U", admin_user, "-d", db,
         "--data-only", "--disable-triggers", "--no-owner", "--single-transaction"],
        timeout=3600, stdin_path=str(dump_path),
    )
    if code != 0:
        return False, out[:500]
    return True, "restored"


async def drop_subscriptions(
    *, stack: str, info: dict[str, Any], to_ordinal: int | None = None,
) -> list[str]:
    """Drop this stack's subscriptions — all of them (local side of a full
    leave) or just those pulling from one departed peer's ordinal. Best-effort;
    returns the dropped names."""
    container = f"{stack}-{info['service']}-1"
    admin, pw, db = info["admin_user"], info["admin_password"], info["db"]
    code, out = await _psql(container, admin, pw, db, "SELECT sub_name FROM spock.subscription;")
    if code != 0:
        return []
    dropped = []
    for sub in (x.strip() for x in out.splitlines() if x.strip()):
        if to_ordinal is not None and sub.rsplit("_n", 1)[-1] != str(to_ordinal):
            continue
        code, _ = await _psql(container, admin, pw, db,
                              f"SELECT spock.sub_drop('{sub}');", timeout=60)
        if code == 0:
            dropped.append(sub)
    return dropped
