"""Pins the lolor (large-object logical replication) wiring in cluster_db.

Native pg_largeobject is a system catalog — logical replication (and thus
Spock) silently skips it, so an app storing files as large objects would
replicate everything EXCEPT the files. The fix routes lo_* through the lolor
extension. These tests pin the three wiring points so a refactor can't
silently drop one:

  1. init scripts (fresh volumes): lolor.node GUC in postgresql.conf +
     CREATE EXTENSION in the initdb hook
  2. reconcile (existing volumes): self-heal block in ensure_replication
  3. repset membership: lolor-schema tables joined to the 'default' repset

The runtime behaviour (GUC placement, PKs on lolor tables, transparent lo_*)
was verified against a live ghcr.io/pgedge/pgedge-postgres:16-spock5-standard
container with lolor 1.2.2 on 2026-07-14.
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import cluster_db  # noqa: E402


def test_init_conf_sets_lolor_node_from_ordinal():
    conf = cluster_db._INIT_SCRIPTS["20-pgconf.sh"]
    assert "lolor.node = '${NODE_ORDINAL}'" in conf
    # snowflake's node GUCs must still be there — same collision-avoidance job.
    assert "snowflake.node = '${NODE_ORDINAL}'" in conf


def test_init_creates_lolor_extension():
    ext = cluster_db._INIT_SCRIPTS["40-extensions.sh"]
    assert "lolor" in ext.split("for EXT in", 1)[1].splitlines()[0]


def test_conf_lines_survive_compose_dollar_escaping():
    # transform_db_service ships init scripts as compose configs with `$`
    # escaped to `$$` so compose doesn't interpolate — after that escaping the
    # ordinal placeholder must still be recoverable by the shell.
    escaped = cluster_db._INIT_SCRIPTS["20-pgconf.sh"].replace("$", "$$")
    assert "lolor.node = '$${NODE_ORDINAL}'" in escaped


def test_reconcile_self_heals_lolor():
    src = inspect.getsource(cluster_db.ensure_replication)
    # extension created idempotently, gated on availability
    assert "CREATE EXTENSION IF NOT EXISTS lolor;" in src
    assert "pg_available_extensions WHERE name='lolor'" in src
    # GUC written to postgresql.conf (ALTER SYSTEM can't set the prefix
    # before the module is loaded — verified empirically), then reloaded
    assert 'grep -q "^lolor.node"' in src
    assert "pg_reload_conf" in src
    # lolor-schema tables joined to the multi-writer repset
    assert "nspname='lolor'" in src
    assert "spock.repset_add_table('default', 'lolor.{table}'" in src
    # images without the extension degrade to a warning, not a crash
    assert "lolor extension unavailable" in src
