# Database Entity Model — AI Real Estate CRM

**Version:** 0.1-draft  
**Date:** 2026-10-10  
**Engine:** PostgreSQL 15+  
**ORM:** SQLAlchemy 2.x (mapped dataclasses or declarative)  
**Migrations:** Alembic  

---

## 1. Conventions

- All primary keys are `UUID` (default `gen_random_uuid()`).
- Every table has `created_at TIMESTAMPTZ DEFAULT now()` and `updated_at TIMESTAMPTZ`.
- Every table (except `tenants`) has `tenant_id UUID NOT NULL REFERENCES tenants(id)`.
- Soft-delete via `deleted_at TIMESTAMPTZ NULL` (NULL = not deleted).
- Monetary values: `INTEGER` (smallest currency unit) + `VARCHAR(3)` currency code.
- Phone numbers stored normalized to E.164 format.
- Sensitive PII (phone, email) optionally encrypted with `pgcrypto` — field-level encryption via SQLAlchemy TypeDecorator.

---

## 2. Entity Relationship Overview

```
tenants ──< users ──< lead_assignments
         │
         ├──< properties
         │
         ├──< leads ──< lead_qualification_proposals
         │          ──< lead_activities
         │          ──< lead_property_matches
         │          ──< lead_score_snapshots
         │          ──< visits
         │          ──< consent_records
         │
         ├──< outbox_messages
         ├──< audit_log (append-only)
         └──< notification_feed
```

---

## 3. Table Definitions

### 3.1 tenants
```sql
CREATE TABLE tenants (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            VARCHAR(255) NOT NULL,
    slug            VARCHAR(100) NOT NULL UNIQUE,      -- URL-safe identifier
    whatsapp_phone_number_id  VARCHAR(50),             -- Meta WABA phone number ID
    whatsapp_access_token_enc TEXT,                    -- Encrypted; never in plaintext column
    whatsapp_verify_token_enc TEXT,
    email_from      VARCHAR(255),
    smtp_host       VARCHAR(255),
    smtp_port       INTEGER,
    smtp_creds_enc  TEXT,                              -- Encrypted JSON
    timezone        VARCHAR(50) NOT NULL DEFAULT 'UTC',
    currency_code   VARCHAR(3) NOT NULL DEFAULT 'INR',
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    settings        JSONB NOT NULL DEFAULT '{}',       -- Feature flags per tenant
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at      TIMESTAMPTZ
);
```

### 3.2 users
```sql
CREATE TABLE users (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id),
    email           VARCHAR(255) NOT NULL,
    email_normalized VARCHAR(255) NOT NULL,            -- lowercase, trimmed
    password_hash   VARCHAR(255) NOT NULL,             -- bcrypt
    full_name       VARCHAR(255) NOT NULL,
    role            VARCHAR(50) NOT NULL               -- SUPER_ADMIN | AGENCY_ADMIN | MANAGER | AGENT
                    CHECK (role IN ('SUPER_ADMIN','AGENCY_ADMIN','MANAGER','AGENT')),
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    invited_by      UUID REFERENCES users(id),
    last_login_at   TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at      TIMESTAMPTZ,
    UNIQUE (tenant_id, email_normalized)
);
CREATE INDEX idx_users_tenant ON users(tenant_id) WHERE deleted_at IS NULL;
```

### 3.3 leads
```sql
CREATE TABLE leads (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id),
    external_id         VARCHAR(100),                  -- e.g. form submission ID
    source              VARCHAR(50) NOT NULL           -- WEB_FORM | WHATSAPP | MANUAL | API
                        CHECK (source IN ('WEB_FORM','WHATSAPP','MANUAL','API')),

    -- Contact info (PII — store encrypted)
    full_name           VARCHAR(255) NOT NULL,
    phone_e164          VARCHAR(20),                   -- E.164, encrypted
    email               VARCHAR(255),                  -- encrypted
    preferred_contact   VARCHAR(20) DEFAULT 'WHATSAPP'
                        CHECK (preferred_contact IN ('WHATSAPP','EMAIL','PHONE')),

    -- Qualification (editable by agent; proposed by AI)
    budget_min          BIGINT,                        -- smallest currency unit
    budget_max          BIGINT,
    currency_code       VARCHAR(3) DEFAULT 'INR',
    property_type       VARCHAR(50),                   -- APARTMENT | VILLA | PLOT | COMMERCIAL
    bhk                 SMALLINT,                      -- bedrooms
    preferred_location  VARCHAR(255),
    purpose             VARCHAR(50)                    -- BUY | RENT | INVEST
                        CHECK (purpose IN ('BUY','RENT','INVEST')),
    move_in_timeline    VARCHAR(50),                   -- IMMEDIATE | 3_MONTHS | 6_MONTHS | YEAR_PLUS

    -- State machine
    stage               VARCHAR(50) NOT NULL DEFAULT 'NEW'
                        CHECK (stage IN ('NEW','QUALIFYING','QUALIFIED','SITE_VISIT_SCHEDULED',
                                         'NEGOTIATION','WON','LOST','STALLED',
                                         'DISQUALIFIED','DO_NOT_CONTACT')),
    lead_health         VARCHAR(20) NOT NULL DEFAULT 'UNKNOWN'
                        CHECK (lead_health IN ('HOT','WARM','COLD','UNKNOWN')),
    intent_score        SMALLINT CHECK (intent_score BETWEEN 0 AND 100),
    automation_status   VARCHAR(30) NOT NULL DEFAULT 'ACTIVE'
                        CHECK (automation_status IN ('ACTIVE','PAUSED','HUMAN_TAKEOVER','OPTED_OUT')),

    -- Assignment
    assigned_agent_id   UUID REFERENCES users(id),

    -- Flags
    do_not_contact      BOOLEAN NOT NULL DEFAULT FALSE,
    is_duplicate_of     UUID REFERENCES leads(id),    -- if merged

    -- Metadata
    last_activity_at    TIMESTAMPTZ,
    score_breakdown     JSONB,                         -- transparent scoring detail
    raw_message_text    TEXT,                          -- original inbound text (WhatsApp/form)
    notes               TEXT,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at          TIMESTAMPTZ
);
CREATE INDEX idx_leads_tenant_stage ON leads(tenant_id, stage) WHERE deleted_at IS NULL;
CREATE INDEX idx_leads_phone ON leads(tenant_id, phone_e164) WHERE deleted_at IS NULL;
CREATE INDEX idx_leads_agent ON leads(tenant_id, assigned_agent_id) WHERE deleted_at IS NULL;
CREATE INDEX idx_leads_stalled ON leads(tenant_id, last_activity_at) WHERE stage NOT IN ('WON','LOST','DO_NOT_CONTACT') AND deleted_at IS NULL;
```

### 3.4 lead_qualification_proposals
```sql
-- AI proposes; agent reviews; backend applies on confirm
CREATE TABLE lead_qualification_proposals (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id),
    lead_id         UUID NOT NULL REFERENCES leads(id),
    proposed_by     VARCHAR(50) NOT NULL DEFAULT 'AI'   -- AI | AGENT
                    CHECK (proposed_by IN ('AI','AGENT')),
    status          VARCHAR(30) NOT NULL DEFAULT 'PENDING_REVIEW'
                    CHECK (status IN ('PENDING_REVIEW','ACCEPTED','REJECTED','SUPERSEDED')),

    -- Proposed field values (nullable — only populated fields are proposed)
    budget_min      BIGINT,
    budget_max      BIGINT,
    property_type   VARCHAR(50),
    bhk             SMALLINT,
    preferred_location VARCHAR(255),
    purpose         VARCHAR(50),
    move_in_timeline VARCHAR(50),

    -- Confidence scores (0.0-1.0 per field)
    confidence      JSONB NOT NULL DEFAULT '{}',        -- {"budget_min": 0.92, "bhk": 0.85, ...}

    llm_model       VARCHAR(100),                       -- which model generated this
    llm_prompt_hash VARCHAR(64),                        -- SHA-256 of prompt (for audit)
    raw_llm_output  TEXT,                               -- full LLM response (never shown to user)

    reviewed_by     UUID REFERENCES users(id),
    reviewed_at     TIMESTAMPTZ,
    review_notes    TEXT,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_proposals_lead ON lead_qualification_proposals(lead_id, status);
```

### 3.5 lead_score_snapshots
```sql
-- Immutable record every time score changes
CREATE TABLE lead_score_snapshots (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id),
    lead_id         UUID NOT NULL REFERENCES leads(id),
    intent_score    SMALLINT NOT NULL,
    lead_health     VARCHAR(20) NOT NULL,
    score_breakdown JSONB NOT NULL,                    -- {"budget_match": 20, "timeline": 15, ...}
    triggered_by    VARCHAR(50) NOT NULL,              -- LEAD_UPDATED | PROPOSAL_ACCEPTED | MANUAL
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_score_snapshots_lead ON lead_score_snapshots(lead_id, created_at DESC);
```

### 3.6 lead_activities
```sql
-- Append-only activity timeline
CREATE TABLE lead_activities (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id),
    lead_id         UUID NOT NULL REFERENCES leads(id),
    actor_id        UUID REFERENCES users(id),          -- NULL = system/bot
    actor_type      VARCHAR(20) NOT NULL DEFAULT 'USER' -- USER | SYSTEM | AI
                    CHECK (actor_type IN ('USER','SYSTEM','AI')),
    activity_type   VARCHAR(50) NOT NULL,               -- NOTE | STAGE_CHANGE | CALL | WHATSAPP_SENT | etc.
    description     TEXT NOT NULL,
    metadata        JSONB DEFAULT '{}',                 -- e.g. old_stage, new_stage
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_activities_lead ON lead_activities(lead_id, created_at DESC);
```

### 3.7 properties
```sql
CREATE TABLE properties (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id),
    title           VARCHAR(255) NOT NULL,
    property_type   VARCHAR(50) NOT NULL
                    CHECK (property_type IN ('APARTMENT','VILLA','PLOT','COMMERCIAL')),
    bhk             SMALLINT,
    price           BIGINT NOT NULL,                   -- listing price (smallest currency unit)
    currency_code   VARCHAR(3) NOT NULL DEFAULT 'INR',
    location_text   VARCHAR(255) NOT NULL,
    latitude        NUMERIC(10,7),
    longitude       NUMERIC(10,7),
    area_sqft       INTEGER,
    description     TEXT,
    amenities       JSONB DEFAULT '[]',
    status          VARCHAR(30) NOT NULL DEFAULT 'AVAILABLE'
                    CHECK (status IN ('AVAILABLE','SOLD','RENTED','UNAVAILABLE','DRAFT')),
    added_by        UUID REFERENCES users(id),
    photos          JSONB DEFAULT '[]',                -- [{url, caption, order}]
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at      TIMESTAMPTZ
);
CREATE INDEX idx_properties_tenant_status ON properties(tenant_id, status) WHERE deleted_at IS NULL;
```

### 3.8 lead_property_matches
```sql
CREATE TABLE lead_property_matches (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id),
    lead_id         UUID NOT NULL REFERENCES leads(id),
    property_id     UUID NOT NULL REFERENCES properties(id),
    match_score     NUMERIC(5,2) NOT NULL,              -- 0-100
    match_reasons   JSONB NOT NULL DEFAULT '{}',        -- {"budget": true, "bhk": true, ...}
    status          VARCHAR(30) NOT NULL DEFAULT 'SUGGESTED'
                    CHECK (status IN ('SUGGESTED','SHORTLISTED','REJECTED','VISITED')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (lead_id, property_id)
);
```

### 3.9 visits (site-visit appointments)
```sql
CREATE TABLE visits (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id),
    lead_id         UUID NOT NULL REFERENCES leads(id),
    property_id     UUID NOT NULL REFERENCES properties(id),
    agent_id        UUID NOT NULL REFERENCES users(id),
    scheduled_at    TIMESTAMPTZ NOT NULL,
    duration_minutes INTEGER NOT NULL DEFAULT 60,
    status          VARCHAR(30) NOT NULL DEFAULT 'SCHEDULED'
                    CHECK (status IN ('SCHEDULED','CONFIRMED','COMPLETED','CANCELLED','NO_SHOW')),
    cancellation_reason TEXT,
    notes           TEXT,
    created_by      UUID REFERENCES users(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_visits_agent_time ON visits(agent_id, scheduled_at) WHERE status NOT IN ('CANCELLED');
CREATE INDEX idx_visits_lead ON visits(lead_id);
```

### 3.10 consent_records
```sql
CREATE TABLE consent_records (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id),
    lead_id         UUID NOT NULL REFERENCES leads(id),
    consent_type    VARCHAR(50) NOT NULL,               -- MARKETING | DATA_PROCESSING | WHATSAPP_OPT_IN
    granted         BOOLEAN NOT NULL,
    channel         VARCHAR(50) NOT NULL,               -- WHATSAPP | WEB_FORM | VERBAL | EMAIL
    ip_address      INET,
    user_agent      TEXT,
    consent_text    TEXT,                              -- exact wording shown/sent
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_consent_lead ON consent_records(lead_id, consent_type, created_at DESC);
```

### 3.11 outbox_messages (Transactional Outbox)
```sql
CREATE TABLE outbox_messages (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id),
    channel         VARCHAR(20) NOT NULL CHECK (channel IN ('WHATSAPP','EMAIL','SMS')),
    recipient       VARCHAR(255) NOT NULL,              -- phone or email
    template_name   VARCHAR(100),
    payload         JSONB NOT NULL,                    -- full message payload
    status          VARCHAR(20) NOT NULL DEFAULT 'PENDING'
                    CHECK (status IN ('PENDING','SENDING','DELIVERED','FAILED','DEAD_LETTERED')),
    provider_message_id VARCHAR(255),                  -- returned by WhatsApp/email provider
    retry_count     SMALLINT NOT NULL DEFAULT 0,
    max_retries     SMALLINT NOT NULL DEFAULT 3,
    next_retry_at   TIMESTAMPTZ,
    error_detail    TEXT,
    correlation_id  VARCHAR(255),                      -- e.g. visit_id, lead_id for tracing
    idempotency_key VARCHAR(255) UNIQUE,               -- prevents double-send
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_outbox_pending ON outbox_messages(status, next_retry_at) WHERE status IN ('PENDING','FAILED');
```

### 3.12 webhook_events (Idempotency Log)
```sql
CREATE TABLE webhook_events (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID REFERENCES tenants(id),
    provider        VARCHAR(30) NOT NULL,               -- WHATSAPP | STRIPE | etc.
    event_id        VARCHAR(255) NOT NULL,              -- provider's unique event ID
    event_type      VARCHAR(100) NOT NULL,
    payload         JSONB NOT NULL,
    processed       BOOLEAN NOT NULL DEFAULT FALSE,
    processed_at    TIMESTAMPTZ,
    error_detail    TEXT,
    received_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (provider, event_id)
);
```

### 3.13 audit_log (Append-Only)
```sql
CREATE TABLE audit_log (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID REFERENCES tenants(id),
    actor_id        UUID REFERENCES users(id),
    actor_type      VARCHAR(20) NOT NULL DEFAULT 'USER',
    action          VARCHAR(100) NOT NULL,              -- LEAD_CREATED | STAGE_CHANGED | etc.
    resource_type   VARCHAR(50) NOT NULL,              -- leads | properties | users | etc.
    resource_id     UUID,
    old_value       JSONB,
    new_value       JSONB,
    ip_address      INET,
    request_id      VARCHAR(64),                       -- trace ID
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- NO UPDATE OR DELETE permissions on this table for application role
CREATE INDEX idx_audit_tenant_time ON audit_log(tenant_id, created_at DESC);
CREATE INDEX idx_audit_resource ON audit_log(resource_type, resource_id);
```

### 3.14 notification_feed
```sql
CREATE TABLE notification_feed (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id),
    user_id         UUID NOT NULL REFERENCES users(id),
    notification_type VARCHAR(50) NOT NULL,
    title           VARCHAR(255) NOT NULL,
    body            TEXT,
    link            VARCHAR(500),
    is_read         BOOLEAN NOT NULL DEFAULT FALSE,
    metadata        JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_notif_user_unread ON notification_feed(user_id, created_at DESC) WHERE is_read = FALSE;
```

---

## 4. Alembic Migration Strategy

- One migration per logical change; never edit a committed migration.
- Migrations must be reversible (`downgrade()` implemented).
- CI runs `alembic upgrade head` against a test DB before any PR merge.
- Seed data (lookup values, demo tenant) via a separate `alembic/seeds/` script — never in migrations.
- Column renames use a 3-step deploy: add new column → backfill → drop old column.

---

## 5. Database Roles & Permissions

```sql
-- Application role (used by FastAPI)
CREATE ROLE crm_app WITH LOGIN PASSWORD '...';
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO crm_app;
REVOKE DELETE ON audit_log FROM crm_app;   -- audit_log is append-only
REVOKE UPDATE ON audit_log FROM crm_app;

-- Read-only role (used by analytics queries / BI tool)
CREATE ROLE crm_readonly WITH LOGIN PASSWORD '...';
GRANT SELECT ON ALL TABLES IN SCHEMA public TO crm_readonly;

-- Migration role (used by Alembic only)
CREATE ROLE crm_migrations WITH LOGIN PASSWORD '...';
GRANT ALL PRIVILEGES ON DATABASE crm TO crm_migrations;
```

---

## 6. Index Strategy

| Table | Index | Rationale |
|-------|-------|-----------|
| leads | (tenant_id, stage) | Funnel queries |
| leads | (tenant_id, phone_e164) | Deduplication lookup |
| leads | (tenant_id, assigned_agent_id) | Agent inbox |
| leads | (tenant_id, last_activity_at) | Stalled lead sweep |
| outbox_messages | (status, next_retry_at) | Outbox poller |
| audit_log | (tenant_id, created_at DESC) | Audit history |
| visits | (agent_id, scheduled_at) | Double-booking check |

---

## 7. Data Retention & Privacy

- Leads with `do_not_contact = TRUE` are excluded from all outbound automation.
- GDPR erasure: anonymise PII fields (set to NULL / hash) rather than hard delete, to preserve referential integrity in audit_log.
- Audit_log retained for 7 years (configurable per tenant).
- Raw LLM outputs retained for 90 days then purged.
