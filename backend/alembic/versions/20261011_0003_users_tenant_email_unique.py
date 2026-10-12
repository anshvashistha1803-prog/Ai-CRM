"""Enforce unique email per tenant constraint on users table (partial index)

Revision ID: 20261011_0003
Revises: 20261011_0002
Create Date: 2026-10-11 01:00:00.000000

The constraint is a PARTIAL unique index that only applies to non-deleted users,
so that soft-deleted users do not block re-registration with the same email.
"""

from alembic import op

revision = "20261011_0003"
down_revision = "20261011_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Partial unique index: only non-soft-deleted users counted for uniqueness.
    # This allows an email to be re-registered after the prior user is soft-deleted.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_users_tenant_email
        ON users (tenant_id, email_normalized)
        WHERE is_deleted IS FALSE
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_users_tenant_email")
