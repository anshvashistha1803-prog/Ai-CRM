"""
Celery configuration for background job processing and periodic tasks.
"""

from celery import Celery

from app.core.config import get_settings

# Ensure all ORM models are registered in SQLAlchemy mapper registry before Celery worker tasks execute
import app.integrations.google.models  # noqa: F401
import app.leads.models  # noqa: F401
import app.matching.models  # noqa: F401
import app.messaging.models  # noqa: F401
import app.notifications.models  # noqa: F401
import app.properties.models  # noqa: F401
import app.qualification.models  # noqa: F401
import app.tenants.models  # noqa: F401
import app.users.models  # noqa: F401
import app.visits.models  # noqa: F401

settings = get_settings()

celery_app = Celery(
    "ai_real_estate_crm",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["app.tasks.celery_tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_track_started=True,
    task_default_queue="default",
    task_routes={
        "app.tasks.celery_tasks.process_outbox_batch": {"queue": "messages"},
        "app.tasks.celery_tasks.dispatch_outbox_message": {"queue": "messages"},
        "app.tasks.celery_tasks.detect_stalled_leads_batch": {"queue": "default"},
        "app.tasks.celery_tasks.sync_all_active_google_sheets_inventory": {"queue": "default"},
    },
    # Periodic tasks (Celery Beat)
    beat_schedule={
        "process-outbox-every-30s": {
            "task": "app.tasks.celery_tasks.process_outbox_batch",
            "schedule": settings.outbox_poll_interval_seconds,
            "options": {"queue": "messages"},
        },
        "detect-stalled-leads-daily": {
            "task": "app.tasks.celery_tasks.detect_stalled_leads_batch",
            "schedule": 86400.0,  # once a day
            "options": {"queue": "default"},
        },
        "sync-google-sheets-inventory-hourly": {
            "task": "app.tasks.celery_tasks.sync_all_active_google_sheets_inventory",
            "schedule": float(settings.google_sheets_sync_interval_seconds),
            "options": {"queue": "default"},
        },
    },
)
