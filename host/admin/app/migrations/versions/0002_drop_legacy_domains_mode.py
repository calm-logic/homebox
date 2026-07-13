"""drop the legacy domains.mode column

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-13

Pre-rework installs had a NOT NULL domains.mode (container|base) that the
domains/projects rework moved to projects.domain_mode. The legacy adopter in
app.migrate only ADDs missing columns, so on old installs the orphaned column
survived adoption — and, having no default and no model field, made every
INSERT INTO domains fail with a NotNullViolation ("Add domain" → 500).

IF EXISTS keeps this a no-op on fresh databases (0001 never had the column).
No downgrade: the column is legacy and its data lives on projects.domain_mode.
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE domains DROP COLUMN IF EXISTS mode")


def downgrade() -> None:
    pass
