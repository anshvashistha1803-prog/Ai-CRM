# System Architecture — AI Real Estate CRM

**Version:** 0.1-draft  
**Date:** 2026-10-10  

---

## 1. Architectural Style

**Modular Monolith** — a single deployable Python process divided into strongly-bounded modules with explicit internal APIs. No microservices. This minimises operational overhead while keeping the codebase navigable and testable.

Key principles:
- Each module owns its own DB models, services, and routes.
- Cross-module calls happen only through well-defined service interfaces (no direct ORM model imports across module boundaries).
- The AI subsystem is a pure **advisory** layer — it proposes; the backend decides.
- All external integrations (WhatsApp, LLM, Email) are behind adapter interfaces so providers can be swapped without touching business logic.

---

## 2. High-Level Component Diagram

```
┌──────────────────────────────────────────────────────────────────────┐
│                        CLIENT TIER                                   │
│  ┌─────────────────────┐   ┌──────────────────────────────────────┐  │
│  │  React + TypeScript  │   │  WhatsApp Cloud API (Meta servers)   │  │
│  │  (Vite, SPA)         │   │  → POST /webhooks/whatsapp           │  │
│  └─────────┬───────────┘   └─────────────┬────────────────────────┘  │
└────────────│────────────────────────────-│──────────────────────────-┘
             │ HTTPS/REST + WebSocket       │ HTTPS webhook
             ▼                             ▼
┌──────────────────────────────────────────────────────────────────────┐
│                       FASTAPI APPLICATION                            │
│                                                                      │
│  ┌────────────────┐  ┌──────────────┐  ┌──────────────────────────┐ │
│  │  API Routers   │  │  Middleware   │  │  Background Task Dispatch│ │
│  │  /api/v1/...   │  │  Auth (JWT)   │  │  (Celery task.delay())   │ │
│  │                │  │  Tenant guard │  │                          │ │
│  │  leads/        │  │  Rate limit   │  │                          │ │
│  │  properties/   │  │  Trace ID     │  │                          │ │
│  │  agents/       │  │  Audit log    │  │                          │ │
│  │  visits/       │  └──────────────┘  └──────────────────────────┘ │
│  │  webhooks/     │                                                  │
│  │  analytics/    │  ┌──────────────────────────────────────────────┐│
│  │  auth/         │  │          MODULE SERVICES (business logic)    ││
│  └────────────────┘  │                                              ││
│                      │  LeadService  │ ScoringEngine                ││
│                      │  MatchEngine  │ AIQualificationService       ││
│                      │  VisitService │ NotificationService          ││
│                      │  OutboxService│ AuditService                 ││
│                      └──────────────────────────────────────────────┘│
└────────────────────────────────┬─────────────────────────────────────┘
                                 │
             ┌───────────────────┼────────────────────┐
             ▼                   ▼                    ▼
    ┌─────────────────┐  ┌──────────────┐  ┌─────────────────────┐
    │   PostgreSQL 15  │  │    Redis      │  │  External Services  │
    │                  │  │  (Celery      │  │                     │
    │  - All app data  │  │   broker +    │  │  WhatsApp Cloud API │
    │  - Outbox table  │  │   result      │  │  LLM Provider(s)    │
    │  - Audit log     │  │   backend)    │  │  Email (SMTP/SES)   │
    │  - Migrations    │  │               │  │  S3-compatible store │
    │    via Alembic   │  └──────────────┘  └─────────────────────┘
    └─────────────────┘

    ┌──────────────────────────────────────────────────────┐
    │               CELERY WORKER PROCESS                  │
    │                                                      │
    │  - AI qualification extraction                       │
    │  - Lead re-scoring                                   │
    │  - Outbox message delivery (WhatsApp / email)        │
    │  - Stalled-lead detection sweep (periodic)           │
    │  - Follow-up scheduling                              │
    └──────────────────────────────────────────────────────┘
```

---

## 3. Module Boundaries

```
backend/
├── core/               # Cross-cutting: DB session, base models, settings, middleware
├── auth/               # JWT, refresh tokens, RBAC, invitations
├── tenants/            # Tenant (agency) CRUD, config, subscription flags
├── users/              # User profiles, role assignment
├── leads/              # Lead entity, state machine, deduplication, activity log
├── qualification/      # AI extraction service and proposal management
├── scoring/            # Deterministic scoring engine
├── properties/         # Property catalog, availability
├── matching/           # Property-to-lead matching engine
├── visits/             # Site-visit booking, slot management, conflict detection
├── messaging/          # WhatsApp + email adapters, outbox, opt-out
├── notifications/      # In-app notification feed
├── analytics/          # Aggregation queries, funnel reports
├── webhooks/           # Inbound WhatsApp webhook handler (idempotent)
└── tasks/              # Celery task definitions (thin wrappers → service calls)
```

**Cross-module rules:**
1. A module may import from `core/` freely.
2. A module may call another module's **service class** but never its private functions or ORM models.
3. `tasks/` imports service classes only — never ORM models directly.
4. `webhooks/` is a thin handler that delegates to `leads/` or `messaging/` services.

---

## 4. Data Flow — Inbound WhatsApp Lead

```
Meta servers
  │  POST /webhooks/whatsapp  (HMAC-SHA256 signature verified)
  ▼
WebhookRouter
  │  idempotency check: message_id in outbox_events table?
  │  if duplicate → 200 OK, skip
  ▼
WebhookService.handle_inbound()
  │  extract phone, text, timestamp
  ▼
LeadService.find_or_create()
  │  normalize phone → E.164
  │  query leads WHERE phone_e164 = ? AND tenant_id = ?
  │  if exists → update activity timestamp
  │  if new    → create lead (stage=NEW, source=WHATSAPP)
  │              record consent (channel=WHATSAPP, timestamp)
  ▼
tasks.qualify_lead.delay(lead_id)   ← enqueue async
  │
  ▼  [Celery Worker]
AIQualificationService.extract(lead_id)
  │  fetch lead + recent messages
  │  call LLM adapter → structured JSON proposal
  │  validate proposal with Pydantic
  │  store QualificationProposal (status=PENDING_REVIEW)
  │  if all confidence >= threshold → auto-apply rules
  │  else → create Agent task "Review AI proposal"
  ▼
ScoringEngine.score(lead_id)
  │  compute intent_score, health, automation_status
  │  store score_breakdown JSON
  ▼
MatchEngine.match(lead_id)         ← async or on-demand
  │  query property catalog
  │  rank results
  │  store LeadPropertyMatch records
  ▼
NotificationService.notify_agent(assignment)
  │  create in-app notification
  │  optionally send email to assigned agent
```

---

## 5. Lead State Machine

```
                   ┌──────────┐
    form/wa/manual │   NEW    │
   ────────────────►          │
                   └────┬─────┘
                        │ validation passes
                        ▼
                   ┌──────────┐
                   │QUALIFYING│ ← AI extraction running
                   └────┬─────┘
                        │ agent confirms or edits proposal
                        ▼
                   ┌──────────┐
                   │QUALIFIED │
                   └────┬─────┘
                        │ properties shortlisted
                        ▼
                   ┌──────────────┐
                   │SITE_VISIT    │ ← visit booked
                   │_SCHEDULED    │
                   └────┬─────────┘
                        │ visit completed
                        ▼
                   ┌──────────┐
                   │NEGOTIATION│
                   └────┬──────┘
                        │
               ┌────────┴────────┐
               ▼                 ▼
          ┌─────────┐      ┌──────────┐
          │  WON    │      │  LOST    │
          └─────────┘      └──────────┘

  Any stage → STALLED (no activity > configured N days)
  Any stage → DISQUALIFIED (explicit agent action)
  Any stage → DO_NOT_CONTACT (opt-out received)

  Allowed transitions stored in LEAD_TRANSITIONS dict (backend only).
  Frontend cannot skip stages or go backwards without MANAGER role.
```

---

## 6. LLM Adapter Architecture

```python
# Adapter interface — backend/qualification/llm_adapter.py

class LLMAdapter(Protocol):
    async def extract_qualification(
        self, messages: list[str], context: dict
    ) -> QualificationProposal: ...

# Implementations
class LiteLLMAdapter:      # wraps litellm.completion → any provider
class OpenAIDirectAdapter: # direct openai client
class MockAdapter:         # deterministic responses for tests

# Selected via settings.LLM_PROVIDER env var
# Never imported directly in business logic — injected via DI
```

---

## 7. Transactional Outbox Pattern

All external sends (WhatsApp, email) go through an outbox table to guarantee at-least-once delivery:

```
1. Business service writes message to outbox_messages table IN THE SAME TRANSACTION as the lead update.
2. Celery periodic task (every 30s) picks PENDING outbox messages.
3. Calls external API; on success → marks DELIVERED + stores provider_message_id.
4. On failure → increment retry_count; exponential backoff; after MAX_RETRIES → DEAD_LETTERED.
5. Webhook callbacks from WhatsApp update delivery status on existing records.
```

This ensures: if the DB write succeeds, the message will eventually be sent. No silent drops.

---

## 8. Multi-Tenancy Strategy

- **Row-level isolation:** every table (except `tenants`) has a non-nullable `tenant_id UUID` column with a DB-level foreign key.
- **Query guard middleware:** a FastAPI dependency injects `tenant_id` into every request scope. All ORM queries use `.filter(Model.tenant_id == request.tenant_id)`. A missing filter raises a runtime error in debug mode.
- **No shared schema, no shared Redis keyspace** (prefix all Celery task IDs with tenant_id).
- **Super Admin** can query across tenants via a separate, explicitly permissioned service layer.

---

## 9. Security Architecture (Summary)

See `docs/SECURITY.md` for full detail.

- HTTPS only (TLS termination at reverse proxy / load balancer).
- JWT access tokens (15-min expiry) + refresh tokens (7-day, rotated, stored in HttpOnly cookie).
- RBAC enforced at service layer (not just router decorators).
- All user inputs validated with Pydantic before reaching service layer.
- WhatsApp webhook signatures verified (HMAC-SHA256) before payload is parsed.
- Secrets loaded from environment variables (never committed).
- Audit log is append-only (no UPDATE/DELETE on audit_log table).
- PII fields encrypted at rest (phone, email) using PostgreSQL pgcrypto where required.

---

## 10. Observability

| Signal | Tool | Storage |
|--------|------|---------|
| Structured logs | Python `structlog` | stdout → log aggregator |
| Request trace IDs | Middleware generates UUID, propagates in headers | Logs |
| Error tracking | Sentry SDK (optional, free tier) | Sentry |
| Health check | GET /health (DB ping + Redis ping) | Uptime monitor |
| Metrics | Prometheus `/metrics` (FastAPI + Celery) | Phase 2 |

---

## 11. Deployment Topology (MVP)

```
Internet
  │
  ▼
Nginx (reverse proxy, TLS, static file serving)
  │
  ├─► FastAPI (uvicorn, 2-4 workers)
  │
  ├─► Celery Worker (1-2 processes)
  │
  ├─► PostgreSQL 15
  │
  └─► Redis (Celery broker + result backend)
```

All components run on a single server for MVP (VPS / cloud VM). Horizontal scaling is achieved by adding API + worker instances behind Nginx.

Docker Compose file provided for local development. Production deployment via Systemd service files or Docker Compose (no Kubernetes in MVP).
