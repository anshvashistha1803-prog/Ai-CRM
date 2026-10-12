"""
Celery configuration for background job processing and periodic tasks.
"""

from celery import Celery

from app.core.config import get_settings

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
    # Periodic tasks (Celery Beat)
    beat_schedule={
        "process-outbox-every-30s": {
            "task": "app.tasks.celery_tasks.process_outbox_batch",
            "schedule": settings.outbox_poll_interval_seconds,
        },
        "detect-stalled-leads-daily": {
            "task": "app.tasks.celery_tasks.detect_stalled_leads_batch",
            "schedule": 86400.0,  # once a day
        },
    },
)
