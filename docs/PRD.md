# Product Requirements Document — AI Real Estate CRM

**Version:** 0.1-draft  
**Date:** 2026-10-10  
**Status:** Awaiting approval  

---

## 1. Vision

A multi-tenant, AI-assisted CRM sold as a SaaS platform to real-estate agencies. Each agency (tenant) manages its own leads, agents, property listings, and analytics inside a fully isolated data partition. The AI layer accelerates qualification, scoring, and matching—but every business action is validated and gated by the backend; no AI output is trusted without server-side verification.

---

## 2. Stakeholders

| Role | Responsibilities |
|------|-----------------|
| **Super Admin** | Platform operator; manages tenant onboarding, billing flags |
| **Agency Admin** | Configures agency settings, user roles, property catalog |
| **Sales Agent** | Works assigned leads; books visits; adds manual notes |
| **Team Lead / Manager** | Views pipeline analytics; reassigns leads; overrides AI actions |
| **Lead (Contact)** | External person whose data is captured and processed |
| **System / Bot** | Automated pipeline steps (AI extraction, scoring, follow-up) |

---

## 3. Business Workflow (End-to-End)

```
[Inbound Channel]
  Web form / WhatsApp Cloud API / Manual entry by agent
        │
        ▼
[1] Lead Capture & Deduplication
  - Normalize phone + email
  - Match against existing contacts (fuzzy + exact)
  - Record consent and opt-in channel
        │
        ▼
[2] Lead Validation
  - Required fields gate (name, contact, at least one preference)
  - Reject or quarantine incomplete leads with reason
        │
        ▼
[3] AI Qualification Extraction (LLM)
  - Extract: budget range, property type, location, BHK, purpose
  - Confidence scores per field; low-confidence → human review queue
  - AI NEVER writes to DB directly; it returns a proposal
        │
        ▼
[4] Transparent Lead Scoring
  - Deterministic scoring rules (backend-computed, auditable)
  - Separate: intent_score (0-100), lead_health (enum), automation_status
  - Score breakdown stored; no black-box numbers
        │
        ▼
[5] Property Matching
  - Query verified property catalog (not LLM-invented)
  - Match on budget ± tolerance, type, location polygon, BHK
  - Return ranked list with match_reason per field
        │
        ▼
[6] Agent Review & Human Takeover
  - All AI proposals visible in UI with confidence
  - Agent may accept, reject, or edit each field
  - "Human takeover" flag pauses all automation for a lead
        │
        ▼
[7] Follow-Up & Stalled Lead Recovery
  - Configurable follow-up schedule per stage
  - Stalled detection (no activity > N days per stage config)
  - Automated WhatsApp/email nudge (agent approves template first)
        │
        ▼
[8] Site-Visit Booking
  - Agent or lead selects slot from availability calendar
  - Backend validates: slot not double-booked, property still active
  - Sends confirmed appointment notifications (WhatsApp + email)
        │
        ▼
[9] Notifications
  - Transactional: lead assigned, visit confirmed, lead stalled
  - Bulk: campaign templates (agency admin must pre-approve)
        │
        ▼
[10] Pipeline & Analytics
  - Funnel conversion rates per stage
  - Agent performance metrics
  - Lead source attribution
  - Revenue forecast (deal closed value)
```

---

## 4. MVP Feature Set (Phase 1)

### 4.1 Core Lead Management
- Lead capture via web form (public endpoint, rate-limited)
- Manual lead creation by agent
- Lead deduplication (phone + email normalization and exact match)
- Lead lifecycle state machine (ENUM stages, backend-enforced transitions)
- Lead health, intent score, automation status as separate fields
- Full audit log for every field change

### 4.2 AI Qualification
- LLM adapter interface (pluggable provider: OpenAI, Gemini, local)
- Structured extraction from free-text (Pydantic output validation)
- Proposal stored separately; agent confirms before applying
- Confidence score per extracted field

### 4.3 Lead Scoring
- Deterministic scoring engine (Python functions, not LLM)
- Score breakdown stored in JSON column (auditable)
- Re-score triggered on lead update

### 4.4 Property Matching
- Property catalog (CRUD by agency admin)
- Match engine: budget, type, location, BHK
- Match results stored; agent reviews and shortlists

### 4.5 Agent Workflow
- Agent assignment (manual + round-robin option)
- Agent task queue / inbox
- Human takeover flag
- Notes and activity timeline

### 4.6 WhatsApp Integration
- Inbound webhook (idempotent, signature-verified)
- Outbound message send via WhatsApp Cloud API
- Message status callbacks (delivered/read)
- Opt-out handling (STOP keyword → mark do_not_contact)

### 4.7 Authentication & Multi-Tenancy
- JWT-based auth (access + refresh tokens)
- Role-based access control (RBAC): SUPER_ADMIN, AGENCY_ADMIN, MANAGER, AGENT
- Tenant row-level isolation (tenant_id on every table)
- Invitation-based user onboarding

### 4.8 Background Jobs
- Async task queue (Celery + Redis OR APScheduler with DB backend)
- Transactional outbox for WhatsApp/email sends
- Retry with exponential backoff
- Dead-letter storage (failed jobs logged to DB)

### 4.9 Notifications
- In-app notification feed (WebSocket or polling)
- Email (SMTP/SendGrid adapter)
- WhatsApp template messages for appointment confirmation

### 4.10 Basic Analytics
- Lead funnel by stage
- Lead source breakdown
- Agent workload summary

---

## 5. Later Features (Phase 2+)

| Feature | Reason Deferred |
|---------|----------------|
| Broker/partner portal | Requires additional role complexity |
| Advanced NLP search over call transcripts | Needs embeddings infra |
| Revenue forecasting (ML) | Needs historical data volume |
| Mobile app | Web first |
| Custom workflow builder per agency | High complexity |
| Bulk WhatsApp campaigns | Regulatory risk; needs opt-in verification first |
| Two-way calendar sync (Google/Outlook) | Third-party OAuth complexity |
| Payment / billing module | Out of scope for CRM core |
| Document e-signing | Third-party integration |
| AI calling (voice) | High complexity, regulatory |

---

## 6. Non-Functional Requirements

| Category | Requirement |
|----------|-------------|
| **Availability** | 99.5% uptime target (MVP); graceful degradation when LLM is down |
| **Latency** | API p95 < 500 ms for non-AI endpoints; AI calls async |
| **Security** | OWASP Top-10 mitigated; secrets in env vars / vault |
| **Compliance** | GDPR / DPDP-India: consent records, right to erasure, data retention |
| **Scalability** | Horizontal scale of API workers; DB connection pooling |
| **Observability** | Structured JSON logs; request trace IDs; /health and /metrics endpoints |
| **Testability** | >= 80% backend unit test coverage on business logic |
| **Portability** | Environment variables only; no hardcoded secrets; Docker-ready |

---

## 7. Open Decisions & Assumptions

| # | Decision | Options | Recommendation | Blocker? |
|---|---------|---------|----------------|---------|
| D1 | Task queue | Celery+Redis vs APScheduler+PostgreSQL | Celery+Redis preferred | No |
| D2 | Email provider | SMTP vs SendGrid vs AWS SES | SMTP adapter first; pluggable | No |
| D3 | Primary LLM provider | OpenAI vs Gemini vs Ollama | LiteLLM adapter wraps all | No |
| D4 | PostgreSQL hosting | Local dev vs Supabase vs managed PG | Local dev now | No |
| D5 | Frontend tests | Vitest+RTL vs Playwright | Both: unit + E2E | No |
| D6 | WhatsApp WABA | Meta-verified WABA required per agency | Must be provided by agency | **Yes** |
| D7 | Calendar | Custom slot vs Google Calendar sync | Custom MVP | No |
| D8 | File storage | Local disk vs S3-compatible | Cloudflare R2 (free egress) | No |
| D9 | Delete strategy | Soft vs hard | Soft delete with deleted_at | No |
| D10 | Redis fallback | Redis required for Celery | Fallback to APScheduler if Redis unavailable | No |

---

## 8. Assumptions

1. Each agency has exactly one WhatsApp Business Account phone number registered with Meta.
2. Property listings managed inside CRM; no MLS/external feed integration in MVP.
3. Lead phone numbers are the primary deduplication key; email is secondary.
4. All monetary values stored as integers (smallest currency unit) with an explicit currency code.
5. The platform is deployed in a single region for MVP.
6. PostgreSQL 15+ is available with pgcrypto and uuid-ossp extensions.
