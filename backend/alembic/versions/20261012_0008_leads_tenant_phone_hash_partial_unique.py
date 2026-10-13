"""Add partial unique index on leads (tenant_id, phone_hash)

Revision ID: 20261012_0008
Revises: 20261011_0007
Create Date: 2026-10-12 23:30:00.000000

Enforces lead deduplication at the database level scoped per tenant.
Applies only to active (non-deleted) leads with non-null phone hashes,
allowing multiple null phone hashes and soft-deleted leads with previously used numbers.
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "20261012_0008"
down_revision = "20261011_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Pre-flight check: Verify no unresolved duplicate active leads exist for any tenant.
    # We do NOT automatically delete, merge, or modify existing leads.
    # If duplicates exist, fail clearly so they can be manually triaged and resolved.
    bind = op.get_bind()
    dup_query = sa.text("""
        SELECT count(*)
        FROM (
            SELECT tenant_id, phone_hash
            FROM leads
            WHERE is_deleted IS FALSE AND phone_hash IS NOT NULL
            GROUP BY tenant_id, phone_hash
            HAVING count(*) > 1
        ) dupes
    """)
    duplicate_count = bind.execute(dup_query).scalar()
    if duplicate_count and duplicate_count > 0:
        raise RuntimeError(
            f"Cannot create partial unique index 'uq_leads_tenant_phone_hash': found {duplicate_count} "
            "tenant/phone_hash group(s) with duplicate active leads. Unresolved duplicates must be "
            "manually triaged and resolved before applying this migration."
        )

    # 2. Create partial unique index on (tenant_id, phone_hash)
    # WHERE is_deleted IS FALSE AND phone_hash IS NOT NULL
    op.create_index(
        "uq_leads_tenant_phone_hash",
        "leads",
        ["tenant_id", "phone_hash"],
        unique=True,
        postgresql_where=sa.text("is_deleted IS FALSE AND phone_hash IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_leads_tenant_phone_hash",
        table_name="leads",
    )
