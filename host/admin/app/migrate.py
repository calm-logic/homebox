"""Alembic runner + auto-adopter for the Homebox admin DB.

Homebox is self-hosted with no operator to run migrations by hand, and every
cluster node keeps its own (un-replicated) admin DB that may sit at a different
version. So migrations run automatically on startup, and this module adopts the
pre-Alembic databases that already exist in the field.

Three cases `run_migrations()` handles:

  * fresh DB (no tables)            → `upgrade head` builds everything from 0001.
  * pre-Alembic DB (core tables,    → reconcile any column drift up to the 0001
    but no alembic_version)           baseline, `stamp 0001`, then `upgrade head`
                                      applies anything newer.
  * already-Alembic DB              → `upgrade head` applies whatever is pending.

The reconcile step is the historical additive bootstrap that used to live in
app/main.py's lifespan. It is idempotent (ADD COLUMN IF NOT EXISTS) and is used
ONLY to bring a heterogeneous legacy DB up to the frozen 0001 shape before we
stamp it — after adoption, Alembic's version table drives everything.
"""
from __future__ import annotations

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from .config import settings

log = logging.getLogger("homebox.migrate")

BASELINE_REV = "0001"
_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
# Any table guaranteed to exist in a pre-Alembic DB — used to tell "fresh" from
# "existing but unversioned".
_SENTINEL_TABLE = "projects"

# Postgres advisory-lock key (arbitrary, stable). Serializes concurrent runners
# so two processes never migrate the same DB at once. Admin is single-instance
# today, but this makes it safe if that ever changes.
_LOCK_KEY = 0x484F4D4278  # "HOMBx"

# The pre-Alembic additive bootstrap, verbatim from the old app/main.py lifespan.
# Brings a legacy DB up to the 0001 baseline. Order/text must stay in sync with
# what shipped, so any legacy install — whatever subset of columns it has — lands
# exactly on the frozen baseline before we stamp it.
_LEGACY_COLUMN_SYNC = (
    "ALTER TABLE environments ADD COLUMN IF NOT EXISTS domain_id "
    "INTEGER REFERENCES domains(id) ON DELETE SET NULL",
    "ALTER TABLE projects ADD COLUMN IF NOT EXISTS require_checks "
    "BOOLEAN NOT NULL DEFAULT TRUE",
    "ALTER TABLE environments ADD COLUMN IF NOT EXISTS promotion_gate "
    "BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE environments ADD COLUMN IF NOT EXISTS e2e_workflow VARCHAR(255)",
    "ALTER TABLE environments ADD COLUMN IF NOT EXISTS promote_from_env_id "
    "INTEGER REFERENCES environments(id) ON DELETE SET NULL",
    "ALTER TABLE domains ADD COLUMN IF NOT EXISTS zone_status "
    "VARCHAR(16) NOT NULL DEFAULT 'active'",
    "ALTER TABLE domains ADD COLUMN IF NOT EXISTS zone_id VARCHAR(64)",
    "ALTER TABLE domains ADD COLUMN IF NOT EXISTS name_servers JSON",
    "ALTER TABLE deployments ADD COLUMN IF NOT EXISTS node_id VARCHAR(64)",
    "ALTER TABLE projects ADD COLUMN IF NOT EXISTS domain_mode "
    "VARCHAR(32) NOT NULL DEFAULT 'container'",
)


def sync_url(url: str | None = None) -> str:
    """The app talks asyncpg; Alembic runs synchronously over psycopg."""
    return (url or settings.database_url).replace("+asyncpg", "+psycopg")


def _alembic_config(url: str | None = None) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", sync_url(url))
    return cfg


def _adopt_legacy(conn) -> None:
    """Reconcile a pre-Alembic DB to the 0001 baseline and stamp it — all in one
    transaction, so a crash can never leave the version table ahead of the
    schema. We write alembic_version by hand (rather than command.stamp, which
    would open its own connection) to keep it atomic with the reconcile."""
    log.info("adopting pre-Alembic admin DB: reconciling to 0001 baseline")
    for stmt in _LEGACY_COLUMN_SYNC:
        conn.exec_driver_sql(stmt)
    conn.exec_driver_sql(
        "CREATE TABLE IF NOT EXISTS alembic_version ("
        "version_num VARCHAR(32) NOT NULL, "
        "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
    )
    conn.execute(
        text("INSERT INTO alembic_version (version_num) VALUES (:v)"),
        {"v": BASELINE_REV},
    )


def run_migrations(url: str | None = None) -> None:
    """Bring the admin DB to head, auto-adopting a pre-Alembic DB if needed.

    Synchronous (Alembic is sync) — call from async code via asyncio.to_thread.
    A session-level advisory lock is held across the whole run so concurrent
    processes serialize instead of racing create_all/upgrade. `url` overrides the
    configured DB (tests point it at a scratch database).
    """
    cfg = _alembic_config(url)
    engine = create_engine(sync_url(url), future=True)
    lock_conn = engine.connect()
    try:
        # Session-level advisory lock (not released by commit) held for the whole
        # run so concurrent processes serialize. Commit after each step to clear
        # SQLAlchemy's autobegun transaction before the next one.
        lock_conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": _LOCK_KEY})
        lock_conn.commit()

        insp = inspect(lock_conn)
        has_alembic = insp.has_table("alembic_version")
        has_core = insp.has_table(_SENTINEL_TABLE)
        lock_conn.commit()

        if not has_alembic and has_core:
            _adopt_legacy(lock_conn)   # all statements in one autobegun txn…
            lock_conn.commit()         # …committed atomically here
        elif not has_alembic and not has_core:
            log.info("fresh admin DB: building schema from 0001")
        # else: already Alembic-managed — nothing special.

        # Apply 0001 (fresh) and any later revisions. No-op for an adopted or
        # up-to-date DB. Runs on its own connection while we hold the lock.
        command.upgrade(cfg, "head")
    finally:
        try:
            lock_conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _LOCK_KEY})
            lock_conn.commit()
        finally:
            lock_conn.close()
            engine.dispose()


if __name__ == "__main__":  # manual: python -m app.migrate
    logging.basicConfig(level=logging.INFO)
    run_migrations()
