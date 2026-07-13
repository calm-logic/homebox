"""Schema-parity tests for the Alembic adoption path.

The invariant that keeps auto-adoption safe: a database built fresh by
`alembic upgrade head` must be schema-identical to a legacy (pre-Alembic)
database that gets adopted — reconciled to the 0001 baseline, stamped, and
upgraded. If they diverge, a stamped legacy node would drift from a fresh one
and later migrations would misbehave.

Requires a Postgres to talk to. Set TEST_DATABASE_URL to a psycopg URL whose
role may CREATE/DROP DATABASE, e.g.:

    TEST_DATABASE_URL=postgresql+psycopg://test:test@localhost:5599/postgres

The test creates throwaway databases (hb_mig_fresh / hb_mig_legacy) and drops
them at the end. Skipped entirely if TEST_DATABASE_URL is unset.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

# Make the `app` package importable (tests/ sits beside app/).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.models  # noqa: E402,F401  (registers tables on Base.metadata)
from app import migrate  # noqa: E402
from app.db import Base  # noqa: E402

MAINT_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not MAINT_URL, reason="TEST_DATABASE_URL not set")

# The columns the pre-Alembic bootstrap added after the original tables existed.
# A legacy DB is simulated by creating the full baseline then dropping these,
# reproducing the oldest install shape (e.g. the Mac mini missing domain_mode).
_TRANSITIONAL = [
    ("environments", "domain_id"),
    ("projects", "require_checks"),
    ("environments", "promotion_gate"),
    ("environments", "e2e_workflow"),
    ("environments", "promote_from_env_id"),
    ("domains", "zone_status"),
    ("domains", "zone_id"),
    ("domains", "name_servers"),
    ("deployments", "node_id"),
    ("projects", "domain_mode"),
]


def _admin_url_for(dbname: str) -> str:
    base = MAINT_URL.rsplit("/", 1)[0]
    return f"{base}/{dbname}"


def _recreate_db(name: str) -> None:
    eng = create_engine(MAINT_URL, isolation_level="AUTOCOMMIT")
    with eng.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    eng.dispose()


def _drop_db(name: str) -> None:
    eng = create_engine(MAINT_URL, isolation_level="AUTOCOMMIT")
    with eng.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    eng.dispose()


def _dump_schema(url: str) -> dict:
    """Reflect the comparable parts of a schema into plain dicts/sets."""
    eng = create_engine(url, future=True)
    out: dict = {"columns": {}, "indexes": set(), "constraints": set()}
    with eng.connect() as conn:
        rows = conn.execute(text(
            "SELECT table_name, column_name, data_type, is_nullable, "
            "character_maximum_length, column_default "
            "FROM information_schema.columns "
            "WHERE table_schema='public' ORDER BY table_name, column_name"
        )).all()
        for t, c, dtype, nullable, maxlen, default in rows:
            if t == "alembic_version":
                continue
            out["columns"][(t, c)] = (dtype, nullable, maxlen, default)

        idx = conn.execute(text(
            "SELECT tablename, indexname, indexdef FROM pg_indexes "
            "WHERE schemaname='public'"
        )).all()
        for tbl, name, ddl in idx:
            if tbl == "alembic_version":
                continue
            # Normalize away the concrete DB name; keep structure.
            out["indexes"].add(ddl.split(" ON ", 1)[1] if " ON " in ddl else ddl)

        # Exclude auto-generated NOT NULL CHECKs: their names embed per-table
        # OIDs (e.g. 2200_16896_4_not_null) and so always differ between two
        # physical DBs. Column nullability is already compared above.
        cons = conn.execute(text(
            "SELECT table_name, constraint_type, constraint_name "
            "FROM information_schema.table_constraints "
            "WHERE table_schema='public' AND constraint_type <> 'CHECK'"
        )).all()
        for t, ctype, name in cons:
            if t == "alembic_version":
                continue
            out["constraints"].add((t, ctype, name))
    eng.dispose()
    return out


def test_fresh_matches_adopted_legacy():
    fresh, legacy = "hb_mig_fresh", "hb_mig_legacy"
    _recreate_db(fresh)
    _recreate_db(legacy)
    try:
        fresh_url = _admin_url_for(fresh)
        legacy_url = _admin_url_for(legacy)

        # --- FRESH: brand-new DB built by the adopter's upgrade path. ---
        migrate.run_migrations(fresh_url)

        # --- LEGACY: simulate a pre-Alembic DB. Build the full baseline, seed a
        # couple of rows, then drop the transitional columns and the version
        # table so it looks like an old install missing them. ---
        eng = create_engine(migrate.sync_url(legacy_url), future=True)
        with eng.begin() as conn:
            Base.metadata.create_all(conn)
            conn.exec_driver_sql(
                "INSERT INTO domains (name, is_primary, cloudflare_routed, "
                "zone_status, created_at) VALUES "
                "('legacy.example', false, false, 'active', now())"
            )
            conn.exec_driver_sql(
                "INSERT INTO projects (repo_full_name, name, default_branch, "
                "domain_mode, managed, auto_deploy, require_checks, "
                "detected_stack, created_at) VALUES "
                "('org/legacy', 'legacy', 'main', 'container', true, true, true, "
                "'{}', now())"
            )
            for tbl, col in _TRANSITIONAL:
                conn.exec_driver_sql(f"ALTER TABLE {tbl} DROP COLUMN {col}")
        eng.dispose()

        # Adopt: reconcile → stamp 0001 → upgrade head.
        migrate.run_migrations(legacy_url)

        a = _dump_schema(migrate.sync_url(fresh_url))
        b = _dump_schema(migrate.sync_url(legacy_url))

        # Column-level parity (name, type, nullability, length, default).
        assert a["columns"] == b["columns"], _diff(a["columns"], b["columns"])
        assert a["indexes"] == b["indexes"], (a["indexes"] ^ b["indexes"])
        assert a["constraints"] == b["constraints"], (a["constraints"] ^ b["constraints"])

        # And the legacy DB really did get re-adopted (columns restored + stamped).
        with create_engine(migrate.sync_url(legacy_url)).connect() as conn:
            ver = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
            assert ver == "0001"
            assert conn.execute(text(
                "SELECT domain_mode FROM projects WHERE name='legacy'"
            )).scalar() == "container"
    finally:
        _drop_db(fresh)
        _drop_db(legacy)


def _diff(a: dict, b: dict) -> str:
    keys = set(a) | set(b)
    lines = []
    for k in sorted(keys):
        if a.get(k) != b.get(k):
            lines.append(f"{k}: fresh={a.get(k)} legacy={b.get(k)}")
    return "COLUMN MISMATCH:\n" + "\n".join(lines)


if __name__ == "__main__":
    if not MAINT_URL:
        print("set TEST_DATABASE_URL"); sys.exit(2)
    test_fresh_matches_adopted_legacy()
    print("OK: fresh == adopted-legacy schema parity")
