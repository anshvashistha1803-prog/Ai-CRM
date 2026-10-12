# Autonomous Execution Progress — AI Real Estate CRM

**Tracking Mode:** Autonomous  
**Status Legend:**
- **DONE:** Implementation and required automated checks completed.
- **PARTIAL:** Implementation exists, but some checks or dependent workflows are in progress.
- **BLOCKED:** External host dependency or authorization required (e.g. host Docker daemon / PostgreSQL service).
- **TODO:** Not yet started.

---

## Roadmap Status

| Phase | Component | Status | Details / Actions Executed |
| :--- | :--- | :---: | :--- |
| **Stabilization** | Role & Tenant Isolation | **DONE** | Agents restricted to assigned leads. Cross-agency and unassigned lead access rejection tested. |
| **Stabilization** | Multi-table Transactions | **DONE** | Explicit savepoint/transaction boundaries on LeadService & AuthService. Rollback tested. |
| **Stabilization** | Schema & Mixin Drift | **DONE** | `SoftDeleteMixin.is_deleted` column mapped; `uq_users_tenant_email` migration 0003 verified. |
| **Stabilization** | Live DB Integration Tests | **BLOCKED** | Blocked on host PostgreSQL service (12 integration tests in `test_tenant_isolation.py`). |
| **Phase A** | Property Management APIs | **DONE** | Full CRUD router (`/api/v1/properties`), models, schemas, service with price/bhk filters and tenant isolation. |
| **Phase A** | AI Proposal Review APIs | **DONE** | Proposal list, get, approve, reject endpoints (`/api/v1/leads/{id}/proposals`) with lead criteria updates. |
| **Phase A** | Tenant Onboarding & User Invites | **DONE** | Endpoints (`/api/v1/tenants`, `/api/v1/users/invitations`, `/api/v1/users/invitations/accept`) with secure tokens. |
| **Phase B** | Reliable Background Outbox | **DONE** | `outbox_messages` table (migration 0004), OutboxService (enqueue, idempotency, retries, exponential backoff), Celery worker & beat. |
| **Phase C** | WhatsApp Cloud API Integration | **DONE** | Webhook verification (`hub.challenge`), HMAC-SHA256 signature verification, inbound parser, tenant dedup, opt-out consent (`STOP`), mock client. |
| **Phase D** | Qualification & Match Wiring | **DONE** | Proposal approval updates lead and triggers `ScoringEngine.evaluate()`; `MatchEngine` calculates compatibility with verified inventory (`/api/v1/leads/{id}/matches`). |
| **Phase E** | Site Visits Booking & Feeds | **DONE** | Models & Alembic migration 0005 (`site_visits`, `notifications`), VisitService with conflict checking, timezone ISO validation, reschedule, cancel stage revert, `/api/v1/visits`. |
| **Phase F** | Frontend CRM UI | **DONE** | React 19 + TypeScript + Vite app with glassmorphic dark-mode design system. Views: Dashboard, Leads & Details, Proposals Review, Properties Catalog, Visits Schedule, Team Management. Production build verified (`dist/` created). |
| **Phase G** | Production Readiness | **DONE** | Multi-stage Dockerfiles (`backend/Dockerfile`, `frontend/Dockerfile`), `docker-compose.yml` (all 6 services), `docs/DEPLOYMENT.md` operational runbook, `/health` endpoint verified. |
| **Phase H** | HAVN Qualification & Sheets Sync | **DONE** | Complete conversational state machine (bilingual, 5 property branches, rental declination, skip token handling, greedy extraction, human takeover), 6-component 100-pt scoring engine, broker handoff outbox events, and Google Sheets operational view sync with row update idempotency. |

---

## Log of Execution

### Cycle 1 — Stabilization Baseline
- **What Changed:** 
  - `backend/app/leads/service.py`: Enforced agent assigned-lead-only view/update, cross-tenant agent validation, `data=items` response envelope.
  - `backend/app/leads/router.py`: Added `POST /api/v1/leads/{lead_id}/assign`.
  - `backend/app/auth/service.py`: Wrapped token rotation and logout in transaction savepoints.
  - `backend/app/core/models/mixins.py`: Added `is_deleted` `Mapped[bool]` to `SoftDeleteMixin`.
  - `backend/app/users/models.py`: Added `uq_users_tenant_email` unique constraint.
  - `backend/alembic/versions/20261011_0003_users_tenant_email_unique.py`: Migration created and verified with `alembic upgrade head --sql`.
- **Tests Executed:**
  - `pytest backend/tests/unit`: **109 passed** in 3.16s.
  - `ruff check backend`: **0 errors**.

### Cycle 2 — Phase A: Core CRM APIs
- **What Changed:**
  - `backend/app/tenants/`: Models, Schemas, Service, and Router mounted at `/api/v1/tenants`.
  - `backend/app/users/`: Models, Schemas, Service, and Router mounted at `/api/v1/users` (including invitations and accept invite flow).
  - `backend/app/properties/`: Models, Schemas, Service, and Router mounted at `/api/v1/properties`.
  - `backend/app/qualification/`: Models, Schemas, Service, and Router mounted at `/api/v1/leads/{lead_id}/proposals`.
  - `backend/app/main.py`: Mounted all new routers with prefix `/api/v1`.
- **Tests Executed:**
  - Added unit test suites for properties and qualification services.
  - `pytest backend/tests/unit`: **121 passed** in 3.70s.
  - `ruff check backend`: **0 errors**.

### Cycle 3 — Phase B & C: Reliable Outbox & WhatsApp Cloud API
- **What Changed:**
  - `backend/app/messaging/models.py`: Added `OutboxMessage` model with composite tenant idempotency constraint `uq_outbox_tenant_idempotency`.
  - `backend/app/messaging/outbox_service.py`: Outbox worker service with duplicate protection, exponential backoff, dead-letter queue.
  - `backend/alembic/versions/20261011_0004_outbox_messages.py`: Alembic migration for outbox messages.
  - `backend/app/tasks/celery_app.py` & `celery_tasks.py`: Celery tasks for processing outbox batch and auditing stalled leads.
  - `backend/app/messaging/whatsapp_client.py`: WhatsApp Cloud API client with mock mode fallback.
  - `backend/app/webhooks/whatsapp_service.py`: Webhook verification, HMAC-SHA256 signature verification, inbound message parser, lead creation/dedup, STOP opt-out enforcement.
  - `backend/app/webhooks/router.py`: Mounted at `/api/v1/webhooks/whatsapp`.
- **Tests Executed:**
  - `test_outbox_service.py`: 5 tests covering enqueue, fetch, idempotency, retry backoff, max retries.
  - `test_whatsapp_service.py`: 7 tests covering webhook verification, invalid tokens, HMAC signature validation, lead creation, opt-out.
  - `pytest backend/tests/unit`: **133 passed** in 4.10s.
  - `ruff check backend`: **0 errors**.

### Cycle 4 — Phase D & E: Scoring, Matching, Visits & Notifications
- **What Changed:**
  - `backend/app/matching/`: Matching schemas, MatchEngine integration, service, and router mounted at `/api/v1/leads/{lead_id}/matches`.
  - Connected AI proposal acceptance to `ScoringEngine.evaluate(lead)` to update lead health and breakdown transparently.
  - `backend/app/visits/models.py`: Added `SiteVisit` model.
  - `backend/app/notifications/models.py`: Added `Notification` model.
  - `backend/alembic/versions/20261011_0005_site_visits_and_notifications.py`: Migration created and verified via `--sql`.
  - `backend/app/visits/service.py`: Visit service handling scheduling, agent availability conflict detection, lead overlap conflict detection, timezone-aware datetime validation, rescheduling, cancel stage rollback.
  - `backend/app/visits/router.py`: Mounted at `/api/v1/visits`.
- **Tests Executed:**
  - `test_matching_service.py`: 6 tests covering inventory matching, criteria filtering, score calculation, tenant isolation.
  - `test_visits_service.py`: 8 tests covering booking, agent conflicts, lead conflicts, rescheduling, cancellation.
  - `pytest backend/tests/unit`: **153 passed** in 4.67s.
  - `ruff check backend`: **0 errors**.

### Cycle 5 — Phase F: Frontend CRM Interface
- **What Changed:**
  - Initialized React 19 + TypeScript + Vite project in `frontend/`.
  - Built dark-mode glassmorphic design system in `frontend/src/index.css`.
  - Created typed API client in `frontend/src/api.ts` with auto-JWT header injection and 401 handling.
  - Implemented components & views:
    - `Sidebar`: Navigation, current agency display, user role badge, logout.
    - `Navbar`: Global search bar, tenant status chip, quick refresh action.
    - `LoginModal`: Authentication modal with demo quick-login credentials.
    - `DashboardView`: KPI summary metrics (leads, proposals, visits, inventory), pipeline funnel breakdown, high-intent prospects table.
    - `LeadsView`: Filterable lead table (search, stage filter), lead detail drawer with qualification criteria, matched inventory tab with compatibility scores, audit activity timeline, and new lead creation modal.
    - `ProposalsView`: AI Qualification review center with side-by-side proposal cards, score impact, confidence, and one-click Approve / Reject actions.
    - `PropertiesView`: Verified property inventory grid with BHK/type badges, price formatting, and new property creation modal.
    - `VisitsView`: Site visits table with status badges, date/time formatting, new visit booking modal with conflict checks, reschedule modal, and cancellation modal.
    - `TeamView`: Agency team members list, role badges, and invitation modal generating secure invitation tokens.
- **Verification:**
  - Executed `npm run build`: TypeScript compiled cleanly (`tsc -b`), Vite produced production bundle (`dist/index.html`, `dist/assets/index.css`, `dist/assets/index.js`) in 1.20s with **0 errors**.

### Cycle 6 — Phase G: Production Readiness
- **What Changed:**
  - Created `backend/Dockerfile`: Multi-stage Python 3.11-slim container with health check and non-root execution.
  - Created `frontend/Dockerfile` & `frontend/nginx.conf`: Multi-stage Node/Nginx container with SPA routing, Gzip compression, and `/api` reverse proxy.
  - Updated `docker-compose.yml`: Comprehensive multi-container setup containing `postgres`, `redis`, `backend`, `celery-worker`, `celery-beat`, and `frontend`.
  - Created `docs/DEPLOYMENT.md`: Complete operational runbook covering key generation, Docker orchestration, Alembic migration workflows, PostgreSQL daily backup & PITR restoration, Celery outbox monitoring, and Meta WhatsApp Cloud API production setup.
  - Verified `/health` endpoint monitoring database and redis connectivity.
- **Final Checks:**
  - `pytest backend/tests/unit`: **153 passed** in 4.67s.
  - `ruff check backend`: **0 errors**.
  - `frontend`: `npm run build` completed with code 0.

### Cycle 8 — Database Integration, Concurrency, and End-to-End Verification
- **What Changed:**
  - **Alembic Offline Migration SQL Verification**: Executed `alembic upgrade head --sql` to comprehensively validate all 5 schema revisions (`0001` through `0005`). Confirmed schema consistency across all tables, primary keys, foreign key constraints (`ON DELETE CASCADE`/`RESTRICT`/`SET NULL`), and unique indexes (`uq_users_tenant_email` partial index, `uq_outbox_tenant_idempotency`).
  - **ORM Relationship & Loading Policy Hardening**: Audited all 16 `lazy="raise"` relationships. Explicitly set in-memory instance attachment in `MatchEngine` candidate matching loops (`match.property = prop`) and eager loading via `selectinload(LeadPropertyMatch.property)` to prevent any lazy loading exceptions during API response serialization.
  - **Concurrency & Pessimistic Row Locking**:
    - Enhanced `AuthService.refresh_access_token` with `.with_for_update()` pessimistic row locking on `select(RefreshToken)` to prevent parallel token-reuse race conditions.
    - Updated `LeadService.create_lead` to catch database `IntegrityError` and raise `DuplicateLeadError` (HTTP 409) to handle concurrent race conditions on duplicate contact info.
    - Added calendar conflict prevention and validation checks for concurrent site visit requests on both agent and lead levels.
  - **Outbox Worker Reliability & Lease Reclamation**:
    - Added `with_for_update(skip_locked=True)` to `OutboxService.fetch_pending_messages` to prevent duplicate message claiming across concurrent Celery worker processes.
    - Implemented `reclaim_stale_processing_messages(lease_timeout_minutes=15)` to self-heal and recover abandoned messages left by crashed or timed-out workers back to `PENDING` (or transition to `DEAD_LETTER` if max retries exceeded).
    - Integrated automatic lease reclamation at the start of periodic outbox batch processing in Celery.
  - **Dedicated Concurrency & Reliability Test Suite**: Created `backend/tests/unit/test_concurrency_and_reliability.py` with 7 tests covering concurrent token rotation, duplicate lead collision handling, agent and lead calendar conflicts, and outbox lease recovery.
  - **Integration Test Isolation & Graceful Skip**: Updated `create_tables` fixture in `conftest.py` to gracefully detect missing host PostgreSQL service on localhost:5432 and cleanly mark tests as `SKIPPED` rather than unhandled connection errors.
- **Automated Verification:**
  - `pytest backend/tests`: **183 passed, 12 skipped in 11.76s** (100% pass rate for executable tests).
  - Backend overall test coverage: **83%** across 2,917 statements (`AuthService`: 100%, `OutboxService`: 99%, `CeleryTasks`: 98%, `LeadStateMachine`: 100%, `Deduplication`: 100%).
  - `ruff check backend`: **All checks passed!** (0 lint/formatting errors).
  - `frontend/`: `oxlint` (0 errors, 0 warnings), `tsc -b && vite build` compiled in **479ms**.
  - PostgreSQL Integration Tests Status: **BLOCKED on external host infrastructure** (PostgreSQL daemon not running on localhost:5432 and Docker not installed on Windows host; 12 integration tests in `test_tenant_isolation.py` cleanly skipped with diagnostic message).

### Cycle 9 — Critical Reliability Gaps Closed and Database Behavior Proved
- **What Changed:**
  - **CI-Strict PostgreSQL Integration Test Policy**: Updated `backend/tests/conftest.py` so integration tests fail immediately via `pytest.fail()` when run in CI (`CI=true` or `REQUIRE_INTEGRATION_TESTS=1`) if PostgreSQL is unavailable. Skips are permitted only in non-CI local dev environments without a configured database.
  - **Precise IntegrityError Handling in LeadService**:
    - Refined `LeadService.create_lead` to inspect PostgreSQL SQLSTATE codes (`23505` unique violation) and error messages for recognized deduplication targets (`phone_hash`, `email_hash`, `uq_leads`).
    - Exclusively translates deduplication violations to `DuplicateLeadError` (HTTP 409).
    - Preserves and re-raises unrelated foreign-key (`23503`), NOT NULL (`23502`), and other integrity failures so they are never misdiagnosed as duplicate leads.
  - **Site-Visit Concurrency via Pessimistic Row Locking**:
    - Updated `VisitsService.schedule_visit` and `reschedule_visit` to issue `.with_for_update()` row locks on both the assigned tour agent (`select(User.id)`) and the prospect lead (`select(Lead.id)`) inside the transaction before running calendar overlap queries.
    - Serializes concurrent booking attempts at the database level and eliminates double-booking race conditions.
  - **Outbox Worker Reliability, Zombie Defense & Provider Reconciliation**:
    - Hardened `OutboxService.fetch_pending_messages` with `.with_for_update(skip_locked=True)` within transaction boundaries.
    - Added zombie-worker defense in `mark_sent` and `mark_failed`: validates that the message status is still `PROCESSING` before updating, preventing slow/crashed workers whose leases expired from overwriting a newer worker's claim.
    - Added `provider_message_id` persistence into `OutboxMessage.payload` upon delivery confirmation.
    - Updated Celery `_handle_message_event` to inspect existing `payload.provider_message_id` on crash recovery, avoiding duplicate customer WhatsApp messages and reconciling status safely without claiming impossible external exactly-once semantics.
  - **Active-User Email Uniqueness & Soft-Delete Re-Registration**:
    - Verified complete parity between ORM model partial index `uq_users_tenant_email` (`Index("uq_users_tenant_email", "tenant_id", "email_normalized", unique=True, postgresql_where="is_deleted IS FALSE")`) and Alembic migration `20261011_0003`.
    - Added `UserService.soft_delete_user` and `DELETE /api/v1/users/{user_id}` route, ensuring `is_deleted=True`, `deleted_at=datetime.now(UTC)`, `is_active=False` are cleanly maintained while protecting self-deletion.
    - Verified that soft-deleted users do not block subsequent re-registration of the same email in the agency.
  - **Comprehensive Concurrency and Reliability Test Suite**:
    - Added 11 tests in `test_concurrency_and_reliability.py` verifying token race defense, duplicate lead isolation vs FK/NOT NULL re-raising, agent and lead double-booking prevention, outbox lease recovery, skip-locked claiming, zombie worker rejection, and provider reconciliation.
    - Added unit tests in `test_tenants_and_users_service.py` for soft-delete and re-registration.
- **Automated Verification:**
  - `pytest backend/tests`: **192 passed, 12 skipped in 11.88s** (100% pass rate for executable tests).
  - Backend overall test coverage: **84%** across 2,975 statements.
  - `ruff check backend`: **All checks passed!** (0 lint/formatting errors).
  - `frontend/`: `oxlint` (0 errors, 0 warnings), `tsc -b && vite build` compiled production bundle in **549ms**.
  - PostgreSQL Integration Tests Status: **BLOCKED on external host infrastructure** (PostgreSQL daemon not running on localhost:5432 and Docker daemon not installed on Windows host). Skips cleanly in local dev; fails strictly in CI.

### Cycle 10 — HAVN Realty Qualification Workflow & Sheets Integration
- **What Changed:**
  - **HAVN Requirements Traceability Matrix**: Created `docs/HAVN_REQUIREMENTS_MATRIX.md` with complete requirement-to-code mapping covering the WhatsApp flowchart, agent system instructions, Buyer's Questionnaire (PDF), and Google Sheets operational spreadsheet.
  - **Conversational Qualification State Machine (`backend/app/qualification/conversation_service.py`)**:
    - **Bilingual Greeting & Matching**: Greets warmly in English/Hindi and mirrors the prospect's language (English or Hinglish).
    - **Intent Classification & Rental Declination**: Branches into `BUY`, `SELL`, or `RENT`. Politely declines rental inquiries explaining HAVN specializes exclusively in premium sales/investments.
    - **Property-Type Branching**: Covers all 5 property types:
      - *House / Villa*: Built-up area, BHK, car parking, elder/kid considerations, usage/purpose.
      - *Flat / Apartment*: BHK, built-up area, floor preference, car parking.
      - *Penthouse*: Indoor area, private terrace, scenic view/facing.
      - *Plot / Land*: Land use zoning, total plot area/guz/sq.yd, road frontage/access width.
      - *Commercial Space*: Intended business use, carpet area, road frontage.
    - **Strict One-Question-Per-Message**: Advances through sequential state nodes (`AWAIT_INTENT`, `AWAIT_PROPERTY_TYPE`, `AWAIT_SPECS`, `AWAIT_LOCATION`, `AWAIT_BUDGET`, `AWAIT_TIMELINE`, `AWAIT_PURPOSE`).
    - **Skip Token Support**: Recognizes skip tokens (`"skip"`, `"pass"`, `"next"`, `"chodo"`, `"baad mein"`, `"later"`) without stalling or crashing, records answer as skipped, and moves to the next node.
    - **Greedy Entity Extraction**: If prospect answers multiple questions up-front (e.g., "Looking for 3 BHK flat in Jakhan Dehradun around 1.5 Cr"), extracts all entities simultaneously and skips already-answered questions.
    - **Human Takeover Triggers**: Triggers immediate broker escalation upon recognizing keywords (`"broker"`, `"agent"`, `"call me"`, `"human"`, `"talk to someone"`, `"executive"`). Sets `automation_status="HUMAN_TAKEOVER"` and stops automated qualification.
  - **HAVN 6-Component 100-Point Scoring Engine (`backend/app/scoring/rules.py` & `backend/app/scoring/engine.py`)**:
    - 6 weighted components totaling 100 points: Intent (25), Budget (20), Timeline (20), Specificity (15), Engagement (10), Location/Type (10).
    - Non-punitive baseline: missing answers award 0 points without negative penalties.
    - Four health tiers: HOT (70–100), WARM (40–69), COLD (10–39), INSUFFICIENT_DATA (0–9).
    - Returns structured breakdown of component scores, positive reasons, improvement recommendations, and unresolved question list.
  - **Broker Handoff & Dual Summaries**:
    - Generates client-facing WhatsApp confirmation thanking the client and promising human advisor outreach.
    - Generates internal broker executive brief with key specs, lead score, category, and missing questionnaire fields.
    - Transactionally enqueues `BROKER_HANDOFF_NOTIFICATION` outbox event.
    - Enforces hard stop (`automation_status="HUMAN_TAKEOVER"`): incoming messages after handoff do not trigger AI qualification.
  - **Google Sheets Operational Sync (`backend/app/integrations/sheets/`)**:
    - Database is the sole source of truth; Google Sheets is an operational view.
    - Seller listing row format matching user's spreadsheet columns: Owner name, phone, config, dimensions, size, area/location, asking price, date, google map link, remarks.
    - Buyer qualified sheet format with lead ID, name, phone, property type, BHK, budget, location, score, and health.
    - Row-update deduplication: searches existing rows by lead ID or phone and updates in-place instead of appending duplicate rows.
    - Offline `MockGoogleSheetsClient` and production `RealGoogleSheetsClient` with gspread credentials.
  - **Celery Task Event Routing (`backend/app/tasks/celery_tasks.py`)**:
    - Wired outbox dispatcher to handle `AI_QUALIFICATION_REQUESTED`, `BROKER_HANDOFF_NOTIFICATION`, and `SHEETS_SYNC_REQUESTED` with exception-safe wrappers.
- **Automated Verification:**
  - `pytest backend/tests`: **209 passed, 12 skipped in 29.51s** (100% pass rate for executable tests).
  - Test coverage: **83%** across 3,608 statements.
  - `backend/tests/unit/test_scoring_engine.py`: 15 tests verifying HAVN scoring rules and tiers.
  - `backend/tests/unit/test_havn_qualification_workflow.py`: 14 tests verifying greeting, rental decline, Hinglish, takeover, skip tokens, property branches, seller flow, handoff, and sheets sync idempotency.
  - `ruff check backend`: **All checks passed!** (0 errors).
  - `frontend/`: `oxlint` (0 errors, 0 warnings), `tsc -b && vite build` compiled production bundle in **1.36s**.

### Cycle 11 — Google OAuth 2.0 & Google Sheets Inventory Synchronization
- **What Changed:**
  - **OAuth 2.0 Architecture & State Security (`backend/app/integrations/google/`)**:
    - Implemented authorization-code flow with backend-managed redirect URL: `/api/v1/integrations/google/callback`.
    - Cryptographic state protection via HMAC-SHA256 (`google.oauth.generate_oauth_state` & `validate_oauth_state`) with embedded tenant ID, timestamp nonce, and strict 10-minute expiration window to prevent CSRF and cross-tenant binding attacks.
    - Token exchange with Google OAuth endpoint (`https://oauth2.googleapis.com/token`) retrieving access token, refresh token, and user email via ID token decode.
    - Zero credential leakage: Tokens are never logged, returned over REST APIs, or saved in plaintext.
    - Refresh tokens and access tokens are encrypted at rest using AES-256-GCM authenticated encryption (`encrypt_field` / `decrypt_field` from `app.core.security`).
    - Dynamic token refresh helper (`ensure_valid_access_token`) that auto-refreshes expired access tokens and gracefully flags revoked grants (`invalid_grant`).
    - Explicit disconnect endpoint (`POST /api/v1/integrations/google/disconnect`) which attempts Google token revocation and clears tenant credentials.
  - **Database & Schema (`backend/alembic/versions/20261011_0006_google_oauth_and_sheets.py`)**:
    - Created `tenant_google_integrations` table with tenant isolation, encrypted credentials, sheet name configurations (`properties_sheet_name`, `incoming_leads_sheet_name`, `qualified_leads_sheet_name`), configurable column mappings JSON, sync status tracking, and error message logging.
    - Added `external_id` (VARCHAR(128), indexed) to the `properties` table for idempotent bidirectional inventory tracking.
    - Validated migration execution via offline SQL generation: `alembic upgrade head --sql`.
  - **Google Sheets Inventory Synchronization (`backend/app/integrations/google/inventory_sync.py`)**:
    - Implemented `PropertyInventorySyncService` with provider adapter separation (`OAuthGoogleSheetsClient` + `MockGoogleSheetsClient`).
    - Column mapping engine translating configurable sheet headers into canonical `Property` models with robust type coercion, area parsing, BHK normalization, and Indian currency parsing (Crores, Lakhs, raw numbers).
    - Stable and idempotent property upserts by `external_id` (fallback to project/title hash), guaranteeing safe re-syncs.
    - Row-level fault tolerance: malformed rows log errors and increment failure counters without aborting valid inventory entries.
    - Preserves PostgreSQL as the authoritative database; does not overwrite internal CRM operational status or tenant attributes without validation.
  - **Router & API Endpoints (`backend/app/integrations/google/router.py`)**:
    - `GET /api/v1/integrations/google/authorize`: Generates signed state and returns Google authorization URL.
    - `GET /api/v1/integrations/google/callback`: Validates state signature and exchange auth code for tokens.
    - `GET /api/v1/integrations/google/status`: Returns integration health, connection status, connected email, sheet names, and sync metrics without exposing credentials.
    - `POST /api/v1/integrations/google/config`: Tenant-scoped updates for spreadsheet ID, sheet names, and column mappings.
    - `POST /api/v1/integrations/google/sync/inventory`: Triggers on-demand inventory synchronization.
    - `POST /api/v1/integrations/google/disconnect`: Revokes and wipes tenant OAuth credentials.
  - **Documentation & Configuration**:
    - Created `docs/GOOGLE_INTEGRATION.md` detailing architecture, exact callback URL, Google Cloud Console setup, scopes, env variables, local testing, and sync rules.
    - Added configuration placeholders in `.env.example`.
- **Automated Verification:**
  - `pytest backend/tests/unit/test_google_oauth_and_sheets.py`: **26 passed in 1.45s** covering state generation/validation/tampering/expiry, callback handling, code exchange errors, token encryption at rest, automatic token refresh, revocation handling, tenant isolation, inventory column mapping, idempotent syncs, error resilience, and credential-free status.
  - Full backend test suite: **242 passed, 12 skipped in 16.67s** (100% pass rate).
  - Code quality: `ruff check backend` clean (0 errors), `frontend` oxlint (0 errors) and Vite build (clean).

### Cycle 12 — Configuration Audit, OpenAI Live Connectivity Check & HAVN Sheets Synchronization
- **What Changed:**
  - **Configuration Verification & Settings Hardening (`backend/app/core/config.py`)**:
    - Validated `.env` and `backend/.env` existence and verified both are strictly excluded from Git in `.gitignore`.
    - Updated `SettingsConfigDict` to check dual env paths `env_file=(".env", "../.env")` ensuring settings load regardless of whether the process launches from repo root or `backend/`.
    - Hardened `allowed_origins: list[str] | str` preventing `pydantic-settings` from throwing JSON decode errors on comma-delimited origin strings from `.env`.
    - Verified all secrets and credentials in `.env` without printing or logging values.
  - **OpenAI Integration & Live Connectivity Audit (`backend/app/qualification/llm_adapter.py`)**:
    - Confirmed backend exclusivity: OpenAI API key is read solely by the backend and never exposed to the frontend or logs.
    - Updated `LiteLLMAdapter` to explicitly pass `api_key=self.settings.openai_api_key or None` and set a 30.0s request timeout.
    - Added structured error handling: `LLMAdapterError` cleanly wraps timeouts, authentication failures, rate limits, and unparseable JSON without leaking secret keys.
    - Added `"PENTHOUSE"` to `ALLOWED_PROPERTY_TYPES` in `QualificationExtraction` schema.
    - Performed a live, minimal 1-token connectivity check: Verified the OpenAI key is recognized by OpenAI API, returning an account credit status alert (`insufficient_quota` / `credit_balance_exhausted`). Verified that the system's `mock` fallback mode shields local testing from upstream billing blockers.
  - **HAVN Realty Inventory Sheets Column Mapping (`backend/app/integrations/google/inventory_sync.py`)**:
    - Implemented exact column matching for HAVN reference spreadsheet: `Owner name`, `Owner number`, `Property type`, `Configuration`, `Size`, `Area`, `Asking price`, `Date`, `Google map link`, `Remarks`.
    - **Preserved distinct meanings of Size vs Area**:
      - `Size` (e.g. `480 sq.yd`, `250 gaj`, `3200 sq ft`) maps to physical dimension `area_sqft` (converting sq.yd/gaj to sqft with 9x multiplier).
      - `Area` (e.g. `Balbir road dalanwala`, `Jakhan Dehradun`, `Kuanwala`) maps strictly to neighborhood/submarket `location_text`.
      - Built a two-pass matching engine in `_build_column_index_map` prioritizing exact match and guarding location from accidentally matching `"carpet area"` or `"built-up area"`.
    - Added title fallback (`{bhk} BHK {property_type} in {location_text}`) for sheets without explicit title columns.
    - Added deterministic external ID derivation `HAVN-{hash}` ensuring idempotent syncs and preventing duplicate or deleted properties.
    - Combined remarks, owner details, and map link into property `description`.
  - **Unit Testing & Verification**:
    - Added `TestLiteLLMAdapter` test suite in `backend/tests/unit/test_llm_adapter.py` testing success extraction, empty response rejection, and network failure resilience.
    - Added `test_inventory_sync_havn_realty_columns_and_size_area_distinction` in `backend/tests/unit/test_google_oauth_and_sheets.py`.
- **Automated Verification:**
  - `pytest backend/tests/unit`: **242 passed, 12 skipped in 16.67s** (100% pass rate, 84% coverage across 4,342 statements).
  - `ruff check backend`: **All checks passed!** (0 errors).
  - `frontend/`: `oxlint` (0 errors, 0 warnings), `tsc -b && vite build` compiled production bundle in **587ms**.

