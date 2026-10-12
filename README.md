# AI Real Estate CRM

Multi-tenant, AI-assisted CRM for real estate agencies.

---

## Quick Start (Development)

### Prerequisites

- Python 3.11+
- Node.js 22+
- PostgreSQL 15 (local or Supabase)
- Redis (local or Upstash)

### 1. Clone and setup environment

```powershell
cd "AI Real Estate CRM"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install uv
```

### 2. Install backend dependencies

```powershell
cd backend
uv pip install -e ".[dev]"
```

### 3. Configure environment

```powershell
Copy-Item ..\env.example .env
# Edit .env with your DB URL, Redis URL, and API keys
```

### 4. Generate JWT keys (one time)

```powershell
python ..\scripts\generate_keys.py
# Update JWT_PRIVATE_KEY_PATH and JWT_PUBLIC_KEY_PATH in .env
```

### 5. Create PostgreSQL databases

```sql
-- Connect as superuser and run:
CREATE DATABASE crm_dev;
CREATE DATABASE crm_test;
CREATE ROLE crm_app WITH LOGIN PASSWORD 'yourpassword';
GRANT ALL PRIVILEGES ON DATABASE crm_dev TO crm_app;
GRANT ALL PRIVILEGES ON DATABASE crm_test TO crm_app;
```

### 6. Run migrations

```powershell
alembic upgrade head
```

### 7. Start the backend

```powershell
uvicorn app.main:app --reload --port 8000
```

API docs: http://localhost:8000/docs  
Health: http://localhost:8000/health

### 8. Install and start the frontend

```powershell
cd ..\frontend
npm install
npm run dev
```

Frontend: http://localhost:5173

---

## Running Tests

```powershell
cd backend
pytest                          # all tests
pytest tests/unit/              # unit tests only (no DB required)
pytest tests/integration/       # integration tests (requires test DB)
pytest --cov=app --cov-report=html  # coverage report
```

---

## Project Structure

```
backend/app/
├── core/         # config, db, security, middleware, deps, exceptions
├── auth/         # JWT, refresh tokens, RBAC
├── tenants/      # agency management
├── users/        # user profiles
├── leads/        # state machine, dedup, activities
├── qualification/ # AI extraction, proposals
├── scoring/      # deterministic scoring engine
├── properties/   # property catalog
├── matching/     # lead↔property matching
├── visits/       # site-visit booking
├── messaging/    # WhatsApp + email, outbox
├── notifications/ # in-app feed
├── analytics/    # funnel and metrics
├── webhooks/     # inbound WhatsApp
└── tasks/        # Celery async tasks
```

See `docs/` for full architecture, database schema, security plan, and implementation roadmap.

---

## Documentation

| Document | Description |
|----------|-------------|
| [docs/PRD.md](docs/PRD.md) | Product requirements, MVP scope |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | System design, module boundaries |
| [docs/DATABASE.md](docs/DATABASE.md) | Entity model, all table definitions |
| [docs/SECURITY.md](docs/SECURITY.md) | Threat model, auth, RBAC, GDPR |
| [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md) | Milestones, acceptance criteria |
