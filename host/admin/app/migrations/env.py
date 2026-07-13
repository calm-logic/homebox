"""Alembic environment for the Homebox admin DB.

The URL comes from the app settings (or ALEMBIC_DB_URL / the Config's
sqlalchemy.url when set programmatically), always rewritten to the *sync*
psycopg driver — Alembic runs its migrations synchronously even though the app
itself talks to Postgres over asyncpg.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# Make the `app` package importable when Alembic is invoked via the CLI from
# host/admin/ (the runtime adopter already runs inside the app, where it is).
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.db import Base  # noqa: E402
import app.models  # noqa: E402,F401  (registers every table on Base.metadata)
from app.config import settings  # noqa: E402

config = context.config
target_metadata = Base.metadata


def _sync_url() -> str:
    url = (
        config.get_main_option("sqlalchemy.url")
        or os.environ.get("ALEMBIC_DB_URL")
        or settings.database_url
    )
    # Alembic is synchronous; the app's asyncpg URL must become psycopg.
    return url.replace("+asyncpg", "+psycopg")


def run_migrations_offline() -> None:
    context.configure(
        url=_sync_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = _sync_url()
    connectable = engine_from_config(
        section, prefix="sqlalchemy.", poolclass=pool.NullPool
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
