"""Add inventory_staging_items table and owner/source fields to properties

Revision ID: 20261011_0007
Revises: 20261011_0006
Create Date: 2026-10-11 23:45:00.000000
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20261011_0007"
down_revision = "20261011_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Add owner contact and raw source fields to properties
    op.add_column("properties", sa.Column("owner_name", sa.String(255), nullable=True))
    op.add_column("properties", sa.Column("owner_phone", sa.String(50), nullable=True))
    op.add_column(
        "properties",
        sa.Column(
            "source_data",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.create_index(
        "ix_properties_tenant_owner_phone",
        "properties",
        ["tenant_id", "owner_phone"],
    )

    # 2. Create inventory_staging_items table
    op.create_table(
        "inventory_staging_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "integration_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenant_google_integrations.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column("spreadsheet_id", sa.String(255), nullable=True),
        sa.Column("sheet_name", sa.String(100), nullable=True),
        sa.Column("row_index", sa.Integer(), nullable=True),
        sa.Column("external_id", sa.String(255), nullable=True),
        sa.Column(
            "status",
            sa.String(50),
            nullable=False,
            server_default="PENDING_REVIEW",
        ),
        sa.Column(
            "validation_errors",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "raw_data",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "parsed_data",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("source_size", sa.String(255), nullable=True),
        sa.Column("source_area", sa.String(255), nullable=True),
        sa.Column("owner_name", sa.String(255), nullable=True),
        sa.Column("owner_phone", sa.String(50), nullable=True),
        sa.Column(
            "duplicate_matched_property_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("properties.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "promoted_property_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("properties.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "is_deleted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_inventory_staging_tenant_status",
        "inventory_staging_items",
        ["tenant_id", "status"],
    )
    op.create_index(
        "ix_inventory_staging_external_id",
        "inventory_staging_items",
        ["external_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_inventory_staging_external_id", table_name="inventory_staging_items")
    op.drop_index("ix_inventory_staging_tenant_status", table_name="inventory_staging_items")
    op.drop_table("inventory_staging_items")

    op.drop_index("ix_properties_tenant_owner_phone", table_name="properties")
    op.drop_column("properties", "source_data")
    op.drop_column("properties", "owner_phone")
    op.drop_column("properties", "owner_name")
