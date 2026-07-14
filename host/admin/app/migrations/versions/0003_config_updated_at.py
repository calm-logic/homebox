"""updated_at on projects/environments/domains for cluster-sync newer-wins

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-14

Cluster config sync had no timestamp to compare on these rows, so any
overwrite-mode import (initial join, deploy fan-out pull) replaced local rows
with the exporter's copy wholesale — a stale peer snapshot silently reverted
user edits (e.g. a project's dedicated-domain setting reset to the default on
the next deploy). These columns let import_state apply newer-wins in every
mode. NULL means "never user-edited" and loses to any timestamped copy.
"""
import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

_TABLES = ("projects", "environments", "domains")


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(table, sa.Column("updated_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    for table in _TABLES:
        op.drop_column(table, "updated_at")
