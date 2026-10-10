# Security Plan — AI Real Estate CRM

**Version:** 0.1-draft  
**Date:** 2026-10-10  

---

## 1. Threat Model Summary

| Threat | Mitigations |
|--------|------------|
| Unauthorised access to leads (competitor agency) | Tenant isolation middleware; JWT; RBAC |
| Agent accessing another agency's data | tenant_id injected from JWT, never from request body |
| AI hallucinating property prices / booking confirmations | AI returns proposals only; backend validates all writes |
| WhatsApp webhook spoofing | HMAC-SHA256 signature verification on every inbound request |
| Replay attacks on webhooks | Idempotency key + webhook_events dedup table |
| Leaked secrets in code or logs | Secrets via env vars; PII scrubbed from logs |
| SQL injection | SQLAlchemy parameterised queries; no raw SQL with user input |
| XSS via stored content | React escapes output by default; CSP header set |
| CSRF | SameSite=Strict cookie; CSRF token for non-GET mutations |
| Brute-force on login | Rate limiting (slowapi); account lockout after N failures |
| Privilege escalation | Role check at service layer (not just router decorator) |
| Data breach (DB dump) | PII encrypted at rest; DB password not in code |
| Denial of service | Rate limiting; webhook payload size cap; Nginx limits |

---

## 2. Authentication

### 2.1 JWT Tokens
```
Access Token:
  - Algorithm: RS256 (asymmetric, so public key can verify without private key exposure)
  - Expiry: 15 minutes
  - Claims: sub (user_id), tenant_id, role, jti (unique token ID)
  - Stored: in-memory (JavaScript) — NOT in localStorage (XSS risk)

Refresh Token:
  - Algorithm: RS256
  - Expiry: 7 days (sliding window on use)
  - Stored: HttpOnly, Secure, SameSite=Strict cookie
  - Rotated on every use (old refresh token invalidated immediately)
  - Stored hash in DB (refresh_tokens table) for revocation
```

### 2.2 Refresh Token Revocation
- DB table `refresh_tokens`: id, user_id, token_hash, expires_at, revoked_at.
- Logout revokes the current refresh token.
- Password change revokes ALL refresh tokens for the user.
- Account deactivation revokes ALL tokens.

### 2.3 Password Policy
- bcrypt with cost factor 12.
- Minimum 10 characters; at least one uppercase, one digit, one special character.
- No password stored in any log or audit record.

---

## 3. Role-Based Access Control (RBAC)

### 3.1 Roles

| Role | Scope | Permissions |
|------|-------|-------------|
| SUPER_ADMIN | Platform-wide | All tenants; tenant CRUD; billing flags |
| AGENCY_ADMIN | Own tenant | User management; property catalog; settings |
| MANAGER | Own tenant | All leads; reassign; override AI; analytics |
| AGENT | Own tenant | Assigned leads only (by default); create/update leads |

### 3.2 Permission Matrix (MVP)

| Action | SUPER_ADMIN | AGENCY_ADMIN | MANAGER | AGENT |
|--------|:-----------:|:------------:|:-------:|:-----:|
| View all tenant data | ✓ | — | — | — |
| Create/edit tenant | ✓ | — | — | — |
| Invite users | ✓ | ✓ | — | — |
| Delete users | ✓ | ✓ | — | — |
| View all leads (tenant) | ✓ | ✓ | ✓ | own |
| Create lead | ✓ | ✓ | ✓ | ✓ |
| Reassign lead | ✓ | ✓ | ✓ | — |
| Accept AI proposal | ✓ | ✓ | ✓ | ✓ |
| Override stage backwards | ✓ | ✓ | ✓ | — |
| Delete lead (soft) | ✓ | ✓ | ✓ | — |
| Manage property catalog | ✓ | ✓ | — | — |
| View analytics | ✓ | ✓ | ✓ | own |
| View audit log | ✓ | ✓ | ✓ | — |
| Configure WhatsApp | ✓ | ✓ | — | — |

### 3.3 Enforcement
- **Router level:** `Depends(require_role("MANAGER"))` decorator on FastAPI routes.
- **Service level:** every service method re-checks caller role and tenant_id. Router-level is defence-in-depth only.
- **Data level:** all ORM queries include `tenant_id` filter. A missing filter raises an exception in test mode.

---

## 4. Tenant Isolation

```python
# core/dependencies.py
async def get_current_tenant(token: str = Depends(oauth2_scheme)) -> TenantContext:
    payload = decode_jwt(token)
    return TenantContext(
        tenant_id=UUID(payload["tenant_id"]),
        user_id=UUID(payload["sub"]),
        role=payload["role"],
    )

# All service methods signature:
async def get_leads(ctx: TenantContext, filters: LeadFilters) -> list[Lead]:
    return await db.query(Lead).filter(
        Lead.tenant_id == ctx.tenant_id,   # MANDATORY — enforced by linter rule
        Lead.deleted_at.is_(None),
    ).all()
```

- A custom SQLAlchemy `TenantSession` wrapper is used that automatically appends `tenant_id` to every query on tenant-scoped models.
- Integration tests assert that cross-tenant data leakage is impossible.

---

## 5. Input Validation

- All API request bodies validated with **Pydantic v2** schemas.
- Query parameters validated with FastAPI's built-in type system.
- File uploads (property photos): MIME type check + max size (10 MB).
- Phone numbers normalized and validated (E.164 format via `phonenumbers` library).
- Free-text fields: max length enforced at Pydantic layer before reaching DB.
- SQL injection: SQLAlchemy ORM only; raw SQL prohibited except in migration scripts with no user input.

---

## 6. WhatsApp Webhook Security

```
1. Meta sends X-Hub-Signature-256: sha256=<HMAC>
2. Backend computes HMAC-SHA256(body_bytes, APP_SECRET)
3. Compare using hmac.compare_digest() (constant-time)
4. Reject if mismatch (HTTP 403)
5. Parse body only after signature is verified
6. Check webhook_events table for duplicate event_id → skip if already processed
7. Respond HTTP 200 within 5s or Meta will retry → use async processing
```

Verify token for subscription confirmation: stored in env var `WHATSAPP_VERIFY_TOKEN`, never in code.

---

## 7. Secrets Management

| Secret | Storage | Access |
|--------|---------|--------|
| DB password | Environment variable | App process only |
| JWT private key | Environment variable (PEM) | Auth service only |
| WhatsApp tokens (per tenant) | DB, encrypted with AES-256-GCM | Tenant-scoped decrypt |
| LLM API keys | Environment variable | qualification service only |
| SMTP credentials | DB, encrypted | messaging service only |
| Redis URL | Environment variable | Core config only |

**Encryption key** (`APP_ENCRYPTION_KEY`) is stored in environment variable / secrets manager.  
Never committed. Never logged. Rotate annually or on suspected compromise.

### 7.1 .env File
```
# .env.example (committed — template only, no real values)
DATABASE_URL=postgresql+asyncpg://user:password@localhost/crm
REDIS_URL=redis://localhost:6379/0
JWT_PRIVATE_KEY_PATH=/path/to/private.pem
JWT_PUBLIC_KEY_PATH=/path/to/public.pem
APP_ENCRYPTION_KEY=change-me-32-bytes-minimum
LLM_PROVIDER=openai
OPENAI_API_KEY=sk-...
WHATSAPP_APP_SECRET=...
WHATSAPP_VERIFY_TOKEN=...
ENVIRONMENT=development
```

`.env` is in `.gitignore`. CI uses GitHub Secrets / environment secrets. Never hardcode.

---

## 8. Rate Limiting

| Endpoint | Limit | Scope |
|----------|-------|-------|
| POST /auth/login | 10 req/min | per IP |
| POST /auth/refresh | 30 req/min | per user |
| POST /api/v1/leads (web form) | 5 req/min | per IP |
| POST /webhooks/whatsapp | 1000 req/min | global (Meta's volume) |
| GET /api/v1/* | 300 req/min | per authenticated user |
| POST /api/v1/* | 60 req/min | per authenticated user |

Implemented with `slowapi` (FastAPI rate limiter backed by Redis).

---

## 9. CORS Policy

```python
# Allowed origins: explicit list from settings
CORS_ORIGINS = settings.ALLOWED_ORIGINS  # e.g. ["https://app.agency.com"]
# Never: allow_origins=["*"] in production
```

---

## 10. Security Headers (via Nginx or middleware)

```
Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'
X-Frame-Options: DENY
X-Content-Type-Options: nosniff
Referrer-Policy: strict-origin-when-cross-origin
Permissions-Policy: geolocation=(), microphone=()
Strict-Transport-Security: max-age=31536000; includeSubDomains
```

---

## 11. Data Privacy (GDPR / DPDP-India)

- **Consent:** recorded in `consent_records` table at point of capture.
- **Right to access:** export endpoint (Admin only) produces JSON of all data for a contact.
- **Right to erasure:** anonymise PII fields — set `phone_e164`, `email`, `full_name` to irreversibly hashed placeholders. Preserve referential integrity.
- **Do-Not-Contact:** any STOP keyword on WhatsApp → `do_not_contact = TRUE` + `automation_status = OPTED_OUT`. Enforced before any outbound send.
- **Data retention:** configurable per tenant. Background job purges expired data.
- **Cross-border data:** deployment region selected by tenant; document in privacy notice.

---

## 12. Audit Log

- Every create/update/delete on business entities is logged in `audit_log`.
- Audit log is append-only at DB permission level (`crm_app` role has no UPDATE/DELETE on `audit_log`).
- Audit entries include: actor, action, resource, old_value, new_value, IP, request_id.
- AI actions logged with `actor_type = 'AI'`.
- Accessible to MANAGER+ role through `/api/v1/audit-log` endpoint (read-only).

---

## 13. Penetration Testing Plan (Pre-Launch)

1. OWASP ZAP automated scan on staging environment.
2. Manual review of auth flows (JWT, refresh, logout, account lockout).
3. Cross-tenant data access attempt (automated integration test).
4. WhatsApp webhook signature bypass attempt.
5. SQL injection via all user-input endpoints.
6. File upload abuse (oversized, wrong MIME type, path traversal).
