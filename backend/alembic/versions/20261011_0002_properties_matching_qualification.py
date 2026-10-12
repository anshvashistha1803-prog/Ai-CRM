"""Properties, lead_property_matches, and lead_qualification_proposals

Revision ID: 20261011_0002
Revises: 20261011_0001
Create Date: 2026-10-11 00:30:00.000000
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20261011_0002"
down_revision = "20261011_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. Properties ─────────────────────────────────────────────────────────
    op.create_table(
        "properties",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("property_type", sa.String(length=50), nullable=False),
        sa.Column("bhk", sa.Integer(), nullable=True),
        sa.Column("price", sa.BigInteger(), nullable=False),
        sa.Column("currency_code", sa.String(length=3), nullable=False, server_default="INR"),
        sa.Column("location_text", sa.String(length=255), nullable=False),
        sa.Column("latitude", sa.Numeric(precision=10, scale=7), nullable=True),
        sa.Column("longitude", sa.Numeric(precision=10, scale=7), nullable=True),
        sa.Column("area_sqft", sa.Integer(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("amenities", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="AVAILABLE"),
        sa.Column("added_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("photos", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_properties_tenant_id", "properties", ["tenant_id"])
    op.create_index("ix_properties_status", "properties", ["status"])

    # ── 2. Lead Property Matches ──────────────────────────────────────────────
    op.create_table(
        "lead_property_matches",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("lead_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("leads.id", ondelete="CASCADE"), nullable=False),
        sa.Column("property_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("properties.id", ondelete="CASCADE"), nullable=False),
        sa.Column("match_score", sa.Numeric(precision=5, scale=2), nullable=False),
        sa.Column("match_reasons", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="SUGGESTED"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("lead_id", "property_id", name="uq_lead_property_match"),
    )
    op.create_index("ix_lead_property_matches_tenant_id", "lead_property_matches", ["tenant_id"])
    op.create_index("ix_lead_property_matches_lead_id", "lead_property_matches", ["lead_id"])
    op.create_index("ix_lead_property_matches_property_id", "lead_property_matches", ["property_id"])

    # ── 3. Lead Qualification Proposals ───────────────────────────────────────
    op.create_table(
        "lead_qualification_proposals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("lead_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("leads.id", ondelete="CASCADE"), nullable=False),
        sa.Column("proposed_by", sa.String(length=50), nullable=False, server_default="AI"),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="PENDING_REVIEW"),
        sa.Column("budget_min", sa.BigInteger(), nullable=True),
        sa.Column("budget_max", sa.BigInteger(), nullable=True),
        sa.Column("property_type", sa.String(length=50), nullable=True),
        sa.Column("bhk", sa.SmallInteger(), nullable=True),
        sa.Column("preferred_location", sa.String(length=255), nullable=True),
        sa.Column("purpose", sa.String(length=50), nullable=True),
        sa.Column("move_in_timeline", sa.String(length=50), nullable=True),
        sa.Column("confidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("llm_model", sa.String(length=100), nullable=True),
        sa.Column("llm_prompt_hash", sa.String(length=64), nullable=True),
        sa.Column("raw_llm_output", sa.Text(), nullable=True),
        sa.Column("reviewed_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_proposals_tenant_id", "lead_qualification_proposals", ["tenant_id"])
    op.create_index("ix_proposals_lead_id", "lead_qualification_proposals", ["lead_id"])
    op.create_index("ix_proposals_status", "lead_qualification_proposals", ["status"])


def downgrade() -> None:
    op.drop_table("lead_qualification_proposals")
    op.drop_table("lead_property_matches")
    op.drop_table("properties")
