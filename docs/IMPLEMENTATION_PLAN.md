# Implementation Plan — AI Real Estate CRM

**Version:** 0.1-draft  
**Date:** 2026-10-10  

---

## 1. Repository Layout

```
ai-real-estate-crm/
│
├── backend/                          # Python FastAPI application
│   ├── alembic/                      # Database migrations
│   │   ├── versions/
│   │   ├── seeds/                    # Seed data scripts (separate from migrations)
│   │   └── env.py
│   ├── app/
│   │   ├── main.py                   # FastAPI app factory, middleware registration
│   │   ├── core/
│   │   │   ├── config.py             # Pydantic Settings (env-var based)
│   │   │   ├── database.py           # AsyncEngine, session factory
│   │   │   ├── security.py           # JWT encode/decode, password hashing
│   │   │   ├── dependencies.py       # FastAPI Depends: current_user, tenant_ctx
│   │   │   ├── middleware.py         # Trace ID, audit log, CORS, rate limit
│   │   │   ├── exceptions.py         # Custom exception classes + handlers
│   │   │   ├── models/               # SQLAlchemy base model, mixins
│   │   │   │   ├── base.py
│   │   │   │   └── mixins.py         # TenantMixin, TimestampMixin, SoftDeleteMixin
│   │   │   └── schemas/              # Shared Pydantic schemas (pagination, errors)
│   │   │
│   │   ├── auth/
│   │   │   ├── models.py             # RefreshToken
│   │   │   ├── schemas.py            # LoginRequest, TokenResponse, InviteRequest
│   │   │   ├── service.py            # AuthService
│   │   │   └── router.py             # /auth/login, /auth/refresh, /auth/logout
│   │   │
│   │   ├── tenants/
│   │   │   ├── models.py
│   │   │   ├── schemas.py
│   │   │   ├── service.py
│   │   │   └── router.py
│   │   │
│   │   ├── users/
│   │   │   ├── models.py
│   │   │   ├── schemas.py
│   │   │   ├── service.py
│   │   │   └── router.py
│   │   │
│   │   ├── leads/
│   │   │   ├── models.py             # Lead, LeadActivity, ConsentRecord
│   │   │   ├── schemas.py
│   │   │   ├── service.py            # LeadService (CRUD, dedup, state machine)
│   │   │   ├── state_machine.py      # LEAD_TRANSITIONS, validate_transition()
│   │   │   ├── deduplication.py      # normalize_phone(), find_duplicate()
│   │   │   └── router.py
│   │   │
│   │   ├── qualification/
│   │   │   ├── models.py             # LeadQualificationProposal
│   │   │   ├── schemas.py
│   │   │   ├── service.py            # AIQualificationService
│   │   │   ├── llm_adapter.py        # LLMAdapter Protocol + implementations
│   │   │   └── router.py             # /proposals/{id}/accept|reject
│   │   │
│   │   ├── scoring/
│   │   │   ├── engine.py             # ScoringEngine (pure functions, no LLM)
│   │   │   ├── rules.py              # Scoring rule definitions
│   │   │   ├── models.py             # LeadScoreSnapshot
│   │   │   └── schemas.py
│   │   │
│   │   ├── properties/
│   │   │   ├── models.py
│   │   │   ├── schemas.py
│   │   │   ├── service.py
│   │   │   └── router.py
│   │   │
│   │   ├── matching/
│   │   │   ├── engine.py             # MatchEngine (pure functions)
│   │   │   ├── models.py             # LeadPropertyMatch
│   │   │   ├── schemas.py
│   │   │   └── router.py
│   │   │
│   │   ├── visits/
│   │   │   ├── models.py             # Visit
│   │   │   ├── schemas.py
│   │   │   ├── service.py            # VisitService (booking, conflict check)
│   │   │   └── router.py
│   │   │
│   │   ├── messaging/
│   │   │   ├── models.py             # OutboxMessage
│   │   │   ├── schemas.py
│   │   │   ├── service.py            # OutboxService, send_whatsapp(), send_email()
│   │   │   ├── whatsapp_adapter.py   # WhatsApp Cloud API client
│   │   │   └── email_adapter.py      # SMTP / SendGrid adapter
│   │   │
│   │   ├── notifications/
│   │   │   ├── models.py             # NotificationFeed
│   │   │   ├── schemas.py
│   │   │   ├── service.py
│   │   │   └── router.py
│   │   │
│   │   ├── analytics/
│   │   │   ├── service.py            # Aggregation queries
│   │   │   └── router.py
│   │   │
│   │   ├── webhooks/
│   │   │   ├── models.py             # WebhookEvent
│   │   │   ├── whatsapp.py           # Inbound webhook handler
│   │   │   └── router.py
│   │   │
│   │   └── tasks/
│   │       ├── celery_app.py         # Celery application instance
│   │       ├── qualify.py            # task: run_qualification(lead_id)
│   │       ├── score.py              # task: rescore_lead(lead_id)
│   │       ├── match.py              # task: run_matching(lead_id)
│   │       ├── outbox.py             # task: process_outbox() [periodic]
│   │       └── stalled.py            # task: detect_stalled_leads() [periodic]
│   │
│   ├── tests/
│   │   ├── conftest.py               # pytest fixtures, test DB, mock adapters
│   │   ├── factories/                # Test data factories (factory_boy)
│   │   ├── unit/
│   │   │   ├── test_state_machine.py
│   │   │   ├── test_scoring_engine.py
│   │   │   ├── test_match_engine.py
│   │   │   ├── test_deduplication.py
│   │   │   └── test_llm_adapter.py
│   │   ├── integration/
│   │   │   ├── test_lead_api.py
│   │   │   ├── test_auth_api.py
│   │   │   ├── test_webhooks.py
│   │   │   ├── test_visit_booking.py
│   │   │   └── test_tenant_isolation.py  # CRITICAL: cross-tenant access attempts
│   │   └── e2e/                      # Playwright-driven E2E (Phase 2)
│   │
│   ├── pyproject.toml                # Dependencies, pytest config, ruff config
│   ├── alembic.ini
│   └── .env.example
│
├── frontend/                         # React + TypeScript (Vite)
│   ├── src/
│   │   ├── api/                      # Axios/fetch wrappers per module
│   │   ├── components/               # Shared UI components
│   │   ├── pages/
│   │   │   ├── Dashboard/
│   │   │   ├── Leads/
│   │   │   ├── Properties/
│   │   │   ├── Visits/
│   │   │   ├── Analytics/
│   │   │   └── Settings/
│   │   ├── store/                    # Zustand state stores
│   │   ├── hooks/                    # Custom React hooks
│   │   ├── types/                    # TypeScript interfaces (generated from OpenAPI)
│   │   └── utils/
│   ├── tests/
│   │   ├── unit/                     # Vitest + React Testing Library
│   │   └── e2e/                      # Playwright
│   ├── package.json
│   ├── vite.config.ts
│   └── tsconfig.json
│
├── docs/
│   ├── PRD.md
│   ├── ARCHITECTURE.md
│   ├── DATABASE.md
│   ├── SECURITY.md
│   ├── IMPLEMENTATION_PLAN.md        # This file
│   └── diagrams/                     # ASCII / Mermaid diagrams
│
├── docker-compose.yml                # Local dev: postgres + redis
├── .env.example
├── .gitignore
└── README.md
```

---

## 2. Technology Stack (Pinned)

### Backend
```toml
[project]
python = ">=3.11"
dependencies = [
  "fastapi>=0.115",
  "uvicorn[standard]>=0.30",
  "sqlalchemy[asyncio]>=2.0",
  "alembic>=1.13",
  "asyncpg>=0.29",                   # async PostgreSQL driver
  "pydantic>=2.11",
  "pydantic-settings>=2.0",
  "python-jose[cryptography]>=3.3",  # JWT
  "passlib[bcrypt]>=1.7",
  "celery[redis]>=5.4",
  "redis>=5.0",
  "httpx>=0.27",                     # async HTTP client (WhatsApp API)
  "litellm>=1.74",                   # LLM provider adapter (already installed)
  "phonenumbers>=8.13",              # phone normalization
  "slowapi>=0.1",                    # rate limiting
  "structlog>=24.0",                 # structured logging
  "cryptography>=42.0",              # field-level encryption
  "factory-boy>=3.3",                # test factories
]

[project.optional-dependencies]
test = [
  "pytest>=8.0",
  "pytest-asyncio>=0.23",
  "pytest-cov>=5.0",
  "httpx>=0.27",                     # TestClient
]
```

### Frontend
```json
{
  "dependencies": {
    "react": "^18",
    "react-dom": "^18",
    "react-router-dom": "^6",
    "zustand": "^4",
    "axios": "^1",
    "@tanstack/react-query": "^5",
    "react-hook-form": "^7",
    "zod": "^3"
  },
  "devDependencies": {
    "typescript": "^5",
    "vite": "^5",
    "vitest": "^1",
    "@testing-library/react": "^14",
    "playwright": "^1"
  }
}
```

---

## 3. Implementation Milestones

### Milestone 1 — Foundation (Week 1-2)
**Goal:** Repository compiles, tests run, DB migrates, auth works.

#### Tasks
- [ ] Initialize git repository + .gitignore
- [ ] Create `docker-compose.yml` (PostgreSQL 15 + Redis)
- [ ] Initialize FastAPI app with `pyproject.toml`
- [ ] Implement `core/config.py` (Pydantic Settings from env)
- [ ] Implement `core/database.py` (async SQLAlchemy engine + session)
- [ ] Implement base models + mixins (TenantMixin, TimestampMixin, SoftDeleteMixin)
- [ ] Alembic setup + first migration (tenants + users tables)
- [ ] `auth/` module: password hashing, JWT issue/verify, login endpoint, refresh endpoint
- [ ] `tenants/` module: CRUD (SUPER_ADMIN only)
- [ ] `users/` module: invitation flow, profile endpoint
- [ ] Tenant isolation middleware (inject tenant_id from JWT)
- [ ] RBAC dependency (require_role)
- [ ] Structured logging middleware (trace ID per request)
- [ ] /health endpoint
- [ ] pytest conftest.py (test DB, mock adapters)
- [ ] Unit tests: auth, JWT, RBAC
- [ ] Integration tests: login flow, tenant isolation

#### Acceptance Criteria
- `pytest` passes with ≥ 90% coverage on auth module.
- `alembic upgrade head` runs cleanly against a fresh DB.
- Attempting to access tenant A's data with tenant B's JWT returns 403/404.
- `/health` returns 200 with DB and Redis status.
- No secrets committed to git.

---

### Milestone 2 — Lead Core (Week 3-4)
**Goal:** Leads can be created, deduplicated, staged, and audited.

#### Tasks
- [ ] `leads/` module: Lead model + all fields
- [ ] Lead state machine (`LEAD_TRANSITIONS` dict + `validate_transition()`)
- [ ] Deduplication: `normalize_phone()`, `find_duplicate()` (exact match)
- [ ] LeadService: create, update, stage transition (validated), soft delete
- [ ] Audit log middleware: auto-log on create/update via SQLAlchemy event listeners
- [ ] Lead activity timeline (lead_activities)
- [ ] Consent record creation on lead capture
- [ ] Lead assignment (manual, round-robin)
- [ ] Full CRUD API: `/api/v1/leads`
- [ ] Filtering + pagination on list endpoint
- [ ] Unit tests: state machine (all invalid transitions rejected), deduplication
- [ ] Integration tests: full lead lifecycle via API

#### Acceptance Criteria
- Invalid stage transitions return 422 with clear error.
- Duplicate lead (same phone + tenant) returns 409 with existing lead ID.
- Every field change has an audit_log entry.
- Agent cannot change another tenant's lead.
- Agent cannot access leads outside their assignment (when restricted).

---

### Milestone 3 — Properties & Matching (Week 5)
**Goal:** Property catalog is live; matching engine links leads to properties.

#### Tasks
- [ ] `properties/` module: CRUD (AGENCY_ADMIN+), availability status
- [ ] `matching/` module: MatchEngine (pure function, no LLM)
- [ ] Match criteria: budget tolerance ±20%, property_type, bhk, location text match
- [ ] Match score breakdown stored in `lead_property_matches`
- [ ] API: suggest matches for lead, agent shortlist/reject action
- [ ] Unit tests: MatchEngine with various criteria combinations

#### Acceptance Criteria
- MatchEngine tested with 10+ scenarios; returns correct ranked list.
- Agent shortlisting a property does not change its availability (non-destructive).
- Match results always reference properties from the same tenant.

---

### Milestone 4 — AI Qualification (Week 6-7)
**Goal:** LLM extracts qualification fields; agent reviews and confirms.

#### Tasks
- [ ] LLMAdapter protocol + LiteLLM implementation
- [ ] MockAdapter for tests (deterministic, no API calls)
- [ ] Structured prompt with few-shot examples
- [ ] Pydantic output validation for LLM response (reject hallucinated fields)
- [ ] `QualificationProposal` CRUD
- [ ] Celery task: `qualify_lead(lead_id)` → calls LLMAdapter
- [ ] API: `/proposals/{id}/accept` and `/proposals/{id}/reject`
- [ ] On accept: apply proposal fields to lead + re-score + re-match
- [ ] Confidence score stored per field
- [ ] Unit tests: MockAdapter, proposal validation, field application
- [ ] Integration test: full qualification flow (mock LLM)

#### Acceptance Criteria
- LLM response with invalid property_type is rejected by Pydantic; lead goes to human review.
- Accepting a proposal updates the lead and creates an audit log entry.
- LLM is never called in unit tests (MockAdapter injected).
- Raw LLM output is stored but never returned in any API response.
- Swapping LLM provider (env var change) requires zero code changes.

---

### Milestone 5 — Scoring Engine (Week 7)
**Goal:** Transparent, deterministic scoring runs after every lead update.

#### Tasks
- [ ] Define scoring rules in `scoring/rules.py` (documented, version-controlled)
- [ ] `ScoringEngine.score(lead)` → returns `{intent_score, lead_health, breakdown_dict}`
- [ ] `LeadScoreSnapshot` persisted on every score run
- [ ] Celery task: `rescore_lead(lead_id)` triggered after lead update or proposal accept
- [ ] Score breakdown returned in lead API response
- [ ] Unit tests: every scoring rule has at least one positive and one negative test case

#### Acceptance Criteria
- Score is reproducible: given the same lead fields, score is always the same.
- Score breakdown explains every point awarded.
- Score is NOT computed by LLM under any code path.
- Changing scoring rules creates a new git commit (version-controlled business logic).

---

### Milestone 6 — WhatsApp Integration (Week 8-9)
**Goal:** Inbound WhatsApp messages create leads; outbound messages are sent reliably.

#### Tasks
- [ ] WhatsApp Cloud API adapter (httpx async client)
- [ ] Inbound webhook: HMAC-SHA256 verification, idempotency check
- [ ] Parse inbound message → find/create lead → enqueue qualification
- [ ] Opt-out handling (STOP keyword → do_not_contact)
- [ ] OutboxMessage table + OutboxService
- [ ] Celery periodic task: `process_outbox()` every 30s
- [ ] Retry with exponential backoff (max 3 retries → DEAD_LETTERED)
- [ ] WhatsApp status callback handler (delivered/read/failed)
- [ ] Unit tests: HMAC verification, idempotency, opt-out
- [ ] Integration test: webhook → lead creation → outbox → mock send

#### Acceptance Criteria
- Webhook with invalid HMAC returns 403 (never processed).
- Duplicate webhook event_id is silently ignored (200 returned, no duplicate lead).
- STOP message sets `do_not_contact = TRUE` and `automation_status = OPTED_OUT`.
- Outbox message is not lost even if worker crashes mid-send (test with mock crash).
- No real WhatsApp API called in tests.

---

### Milestone 7 — Visit Booking & Notifications (Week 10)
**Goal:** Site visits can be booked, confirmed, and notified reliably.

#### Tasks
- [ ] Visit model + VisitService
- [ ] Double-booking check (agent + property + time window)
- [ ] Property availability check (status must be AVAILABLE)
- [ ] Visit status state machine (SCHEDULED → CONFIRMED → COMPLETED/CANCELLED/NO_SHOW)
- [ ] On visit creation: enqueue outbox messages (WhatsApp + email to lead + agent)
- [ ] Notification feed entries for assigned agent
- [ ] Unit tests: double-booking detection, state machine
- [ ] Integration test: full booking flow with notification assertions

#### Acceptance Criteria
- Double-booking attempt returns 409.
- Visit on SOLD property returns 422.
- Visit confirmation triggers WhatsApp outbox entry (verifiable in DB without sending).
- Cancellation of visit updates lead stage appropriately.

---

### Milestone 8 — Stalled Lead Recovery (Week 11)
**Goal:** Stalled leads are detected and recovery is triggered automatically.

#### Tasks
- [ ] Per-stage `stall_threshold_days` configuration (tenant-level setting)
- [ ] Celery beat task: `detect_stalled_leads()` daily
- [ ] Sets `stage = STALLED` + creates lead_activity + notifies assigned agent
- [ ] Follow-up task scheduler: configurable follow-up messages per stage
- [ ] Integration test: advance mock clock, verify stalled detection

#### Acceptance Criteria
- Lead with no activity for > threshold days is marked STALLED.
- Agent receives notification and in-app alert.
- Do-not-contact leads are never included in stalled-lead follow-up.

---

### Milestone 9 — Frontend (Week 12-14)
**Goal:** React app covers core agent and admin workflows.

#### Tasks
- [ ] Vite + React + TypeScript scaffold
- [ ] Auth: login page, JWT refresh, protected routes
- [ ] Lead list with filters (stage, health, agent, search)
- [ ] Lead detail: timeline, qualification proposal review, shortlisted properties
- [ ] Property catalog CRUD
- [ ] Visit booking UI with calendar picker
- [ ] Analytics dashboard: funnel chart, lead source breakdown
- [ ] Settings: user management, WhatsApp configuration
- [ ] OpenAPI spec generated from FastAPI → TypeScript types auto-generated
- [ ] Vitest unit tests: lead state machine display, proposal accept/reject
- [ ] Playwright E2E: login → create lead → accept proposal → book visit

#### Acceptance Criteria
- All API calls include tenant-scoped JWT (no hardcoded IDs).
- Agent cannot see UI routes for admin-only features.
- TypeScript strict mode; no `any` types in business logic.
- Playwright E2E test passes against local backend.

---

### Milestone 10 — Analytics & Polish (Week 15)
**Goal:** Analytics are accurate; system is observable and production-ready.

#### Tasks
- [ ] Analytics service: funnel query, agent performance, source attribution
- [ ] Prometheus metrics endpoint
- [ ] Sentry error tracking integration (optional, env-gated)
- [ ] /metrics endpoint (Celery task counts, queue depth)
- [ ] API rate limiting (slowapi)
- [ ] Security headers (CSP, HSTS, etc.)
- [ ] GDPR erasure endpoint
- [ ] Audit log viewer API
- [ ] Load test (locust): 100 concurrent users, measure p95
- [ ] Full test suite run with coverage report

#### Acceptance Criteria
- Test coverage ≥ 80% backend, ≥ 70% frontend.
- No critical/high OWASP findings in ZAP scan.
- p95 latency < 500 ms on non-AI endpoints under 100 concurrent users.
- GDPR erasure removes all PII for a given contact.

---

## 4. First Development Step — Commands

After approval of this specification, run these commands exactly to bootstrap the project:

```powershell
# 1. Initialize Git repository
cd "c:\Users\vashi\OneDrive\Desktop\AI Real Estate CRM"
git init -b main
git add docs/
git commit -m "chore: add specification documents"

# 2. Create directory structure
New-Item -ItemType Directory -Force backend, backend/app, backend/alembic, backend/tests, frontend, docs

# 3. Create docker-compose.yml for local infrastructure
# (to be created in next step — provides PostgreSQL 15 + Redis)

# 4. Initialize Python project
# Install uv (fast package manager) then create venv
pip install uv
cd backend
uv init --python 3.11
uv add fastapi uvicorn[standard] sqlalchemy[asyncio] alembic asyncpg pydantic pydantic-settings

# 5. Initialize React frontend
cd ../frontend
npx -y create-vite@latest . --template react-ts

# 6. Start local infrastructure
cd ..
docker-compose up -d   # once docker-compose.yml is created

# 7. Run first migration
cd backend
alembic upgrade head

# 8. Start backend
uvicorn app.main:app --reload

# 9. Start frontend
cd ../frontend
npm install
npm run dev
```

---

## 5. Definition of Done (per PR)

- [ ] Passing tests (`pytest` + `vitest`)
- [ ] No new linting errors (`ruff check` + `mypy` strict)
- [ ] No secrets in code
- [ ] Migration is reversible (downgrade implemented)
- [ ] Audit log entry created for all business mutations
- [ ] PR description references acceptance criteria

---

## 6. Risks

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|-----------|
| Meta WABA approval delay | Medium | High | Start with test WABA (sandbox mode); develop all other features first |
| LLM hallucination in qualification | High | Medium | Pydantic validation on all LLM output; confidence threshold; human review queue |
| Redis unavailable in production | Low | High | APScheduler fallback in config; health check alerts |
| PostgreSQL connection exhaustion | Medium | High | AsyncPG connection pool sizing; pgBouncer in production |
| Cross-tenant data leak bug | Low | Critical | Dedicated integration test suite; TenantSession wrapper |
| Scope creep from agency clients | High | Medium | MVP scope locked in PRD; use phase gate reviews |
