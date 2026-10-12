"""Add tenant_google_integrations table and properties.external_id

Revision ID: 20261011_0006
Revises: 20261011_0005
Create Date: 2026-10-11 18:00:00.000000
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20261011_0006"
down_revision = "20261011_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Add external_id to properties for stable Google Sheets syncing
    op.add_column("properties", sa.Column("external_id", sa.String(255), nullable=True))
    op.create_index("ix_properties_external_id", "properties", ["external_id"])

    # 2. Create tenant_google_integrations table
    op.create_table(
        "tenant_google_integrations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
            index=True,
        ),
        sa.Column(
            "connected_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("google_email", sa.String(255), nullable=True),
        sa.Column("google_user_id", sa.String(255), nullable=True),
        sa.Column("refresh_token_enc", sa.Text(), nullable=True),
        sa.Column("access_token_enc", sa.Text(), nullable=True),
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scopes", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("spreadsheet_id", sa.String(255), nullable=True),
        sa.Column("properties_sheet_name", sa.String(100), nullable=False, server_default="Properties"),
        sa.Column("incoming_leads_sheet_name", sa.String(100), nullable=False, server_default="Incoming Leads"),
        sa.Column("qualified_leads_sheet_name", sa.String(100), nullable=False, server_default="Qualified Leads"),
        sa.Column("column_mappings", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("sync_mode", sa.String(50), nullable=False, server_default="MERGE_VALIDATED"),
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sync_status", sa.String(50), nullable=True),
        sa.Column("last_sync_error", sa.Text(), nullable=True),
        sa.Column("last_sync_stats", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_google_integrations_tenant_active",
        "tenant_google_integrations",
        ["tenant_id", "is_active"],
    )


def downgrade() -> None:
    op.drop_index("ix_google_integrations_tenant_active", table_name="tenant_google_integrations")
    op.drop_table("tenant_google_integrations")
    op.drop_index("ix_properties_external_id", table_name="properties")
    op.drop_column("properties", "external_id")
