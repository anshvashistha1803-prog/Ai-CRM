# Production Deployment & Operations Guide — AI Real Estate CRM

## 1. System Architecture Overview

The AI Real Estate CRM runs as a containerized modular monolith with asynchronous background workers:

```
[ Clients / Mobile / Browsers ]
               │
               ▼
   [ Nginx Reverse Proxy (Frontend Container) ]
         │ (HTTP / SPA)           │ (/api/* & /health)
         ▼                        ▼
[ Static React Bundle ]     [ FastAPI Application (crm-backend) ]
                                  │
         ┌────────────────────────┼────────────────────────┐
         ▼                        ▼                        ▼
[ PostgreSQL 15 ]          [ Redis 7 ]             [ Meta Cloud API ]
(Business Source of Truth) (Broker & Cache)        (WhatsApp Webhooks & Outbox)
         ▲                        ▲
         │                        │
  [ Celery Worker ]        [ Celery Beat ]
  (Outbox & AI Pipelines)  (Periodic Scheduler)
```

### Components:
1. **Frontend (`crm-frontend`)**: React 19 + TypeScript + Vite served via high-performance Nginx with Gzip compression and API reverse proxying.
2. **Backend (`crm-backend`)**: Python 3.11 + FastAPI async application running under Uvicorn with tenant-isolated database sessions.
3. **Database (`crm-postgres`)**: PostgreSQL 15 with composite tenant indices, partial uniqueness constraints, and foreign key cascades.
4. **Cache & Broker (`crm-redis`)**: Redis 7 managing session caching, rate-limiting counters, and Celery broker queues (`default`, `messages`, `qualification`).
5. **Worker (`crm-celery-worker`)**: Outbox dispatcher with exponential backoff, dead-letter recovery, and stalled lead monitor.
6. **Beat (`crm-celery-beat`)**: Periodic scheduler triggering outbox batch scans every 30s and stalled lead audits daily.

---

## 2. Pre-Deployment Configuration & Security

### 2.1 Cryptographic Key Generation

1. **RSA Keypair for JWT Authentication**:
   ```bash
   mkdir -p backend/keys
   openssl genrsa -out backend/keys/private.pem 2048
   openssl rsa -in backend/keys/private.pem -pubout -out backend/keys/public.pem
   chmod 600 backend/keys/private.pem
   chmod 644 backend/keys/public.pem
   ```

2. **Field Encryption Key (AES-256-GCM)**:
   Generate a 32-character high-entropy secret for `APP_ENCRYPTION_KEY`:
   ```bash
   python -c "import secrets; print(secrets.token_hex(32))"
   ```

3. **Application Secret Key**:
   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(64))"
   ```

### 2.2 Environment Variables Matrix

| Variable | Description | Production Requirement |
|---|---|---|
| `ENVIRONMENT` | Deployment tier | Must be `production` |
| `DATABASE_URL` | Asyncpg PostgreSQL URI | `postgresql+asyncpg://<user>:<pwd>@<host>:5432/<db>` |
| `DATABASE_URL_SYNC`| Psycopg2 URI for Alembic | `postgresql+psycopg2://<user>:<pwd>@<host>:5432/<db>` |
| `REDIS_URL` | Redis cache URI | `redis://<host>:6379/0` |
| `CELERY_BROKER_URL`| Celery broker URI | `redis://<host>:6379/1` |
| `CELERY_RESULT_BACKEND` | Celery task status URI | `redis://<host>:6379/2` |
| `SECRET_KEY` | Flask/FastAPI cookie secret | Min 32 chars random |
| `APP_ENCRYPTION_KEY` | AES-256 column encryption | Min 32 chars random |
| `LLM_PROVIDER` | AI adapter choice | `openai` (or `gemini`) |
| `OPENAI_API_KEY` | OpenAI API Secret | `sk-...` |
| `WHATSAPP_APP_SECRET` | Meta App Secret for HMAC | From Meta App Dashboard |
| `WHATSAPP_VERIFY_TOKEN` | Webhook verification token | Random high-entropy string |
| `ALLOWED_ORIGINS` | CORS whitelist | Comma-separated agency domains |

---

## 3. Docker Compose Deployment Procedure

### 3.1 First-Time Setup

1. Clone repository to server:
   ```bash
   git clone <repo_url> /opt/ai-real-estate-crm
   cd /opt/ai-real-estate-crm
   ```

2. Configure environment:
   ```bash
   cp .env.example .env
   # Edit .env with production credentials
   nano .env
   ```

3. Build and launch all containers:
   ```bash
   docker compose up -d --build
   ```

4. Verify running health status:
   ```bash
   docker compose ps
   curl -f http://localhost:8000/health
   # Returns: {"status":"healthy","database":"ok","redis":"ok",...}
   ```

---

## 4. Database Migrations Runbook

### 4.1 Applying Migrations
Alembic migrations are automatically applied on container startup via `docker-compose.yml`:
```bash
docker compose exec backend alembic upgrade head
```

### 4.2 Verifying Migration History
```bash
docker compose exec backend alembic current
docker compose exec backend alembic history --verbose
```

### 4.3 Safe Rollback Procedure
If a migration failure occurs, revert to the previous revision:
```bash
# Preview rollback SQL before running
docker compose exec backend alembic upgrade <target_rev> --sql

# Execute rollback
docker compose exec backend alembic downgrade -1
```

---

## 5. PostgreSQL Backup & Disaster Recovery

### 5.1 Automated Daily Backup
Schedule a cron job on the host machine (`/etc/cron.daily/crm-pg-backup`):

```bash
#!/bin/bash
BACKUP_DIR="/var/backups/crm-postgres"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
mkdir -p "$BACKUP_DIR"

docker compose exec -T postgres pg_dump -U crm_app -d crm_dev -Fc > "$BACKUP_DIR/backup_${TIMESTAMP}.dump"

# Retain last 14 daily backups
find "$BACKUP_DIR" -type f -name "*.dump" -mtime +14 -delete
```

### 5.2 Database Restoration
To restore a snapshot into a clean database:
```bash
# 1. Stop backend and workers to prevent writes
docker compose stop backend celery-worker celery-beat

# 2. Restore dump into PostgreSQL
docker compose exec -T postgres pg_restore -U crm_app -d crm_dev --clean --if-exists < /var/backups/crm-postgres/backup_20261011_000000.dump

# 3. Restart application
docker compose start backend celery-worker celery-beat
```

---

## 6. Celery Workers & Outbox Queue Operations

### 6.1 Worker Commands
- **Check active worker status**:
  ```bash
  docker compose exec celery-worker celery -A app.tasks.celery_app status
  ```

- **Inspect active tasks**:
  ```bash
  docker compose exec celery-worker celery -A app.tasks.celery_app inspect active
  ```

- **Inspect queue backlog**:
  ```bash
  docker compose exec redis redis-cli -n 1 llen default
  docker compose exec redis redis-cli -n 1 llen messages
  ```

### 6.2 Dead-Letter Recovery (Failed Outbox Messages)
When an outbound WhatsApp message exceeds 3 retries, its state becomes `FAILED`.
To view failed messages:
```sql
SELECT id, tenant_id, destination, retry_count, error_reason, created_at
FROM outbox_messages
WHERE status = 'FAILED'
ORDER BY created_at DESC;
```
To re-queue messages after fixing downstream connectivity:
```sql
UPDATE outbox_messages
SET status = 'PENDING', retry_count = 0, error_reason = NULL, next_retry_at = CURRENT_TIMESTAMP
WHERE status = 'FAILED';
```

---

## 7. Meta WhatsApp Cloud API Production Setup

1. **Create Meta Business App**:
   - Go to [developers.facebook.com](https://developers.facebook.com).
   - Create App -> Business -> WhatsApp.
2. **Obtain Permanent System User Access Token**:
   - In Meta Business Manager, go to Business Settings -> System Users.
   - Add a System User with Admin permissions.
   - Generate token with `whatsapp_business_management` and `whatsapp_business_messaging` scopes.
3. **Configure Webhook**:
   - Webhook URL: `https://<your-crm-domain>/api/v1/webhooks/whatsapp`
   - Verify Token: Matches `WHATSAPP_VERIFY_TOKEN` in `.env`.
   - Webhook Fields Subscription: Select `messages`.
4. **Agency Onboarding**:
   - Each agency inputs their `phone_number_id`, `waba_id`, and `permanent_access_token` into CRM Agency Settings (encrypted via AES-256).
