"""Celery entry points for the WhatsApp intake pipeline."""

import logging

from celery import shared_task
from django.conf import settings

logger = logging.getLogger(__name__)


@shared_task(
    bind=True,
    name="apps.whatsapp_intake.tasks.process_whatsapp_events",
    autoretry_for=(ConnectionError, TimeoutError),
    retry_backoff=True,
    retry_backoff_max=300,
    max_retries=3,
)
def process_whatsapp_events(self):
    """Process one bounded batch of stored deliveries.

    The webhook has already persisted the payloads, so this needs no network
    connection -- unlike the mail poller, which must reach IMAP first.
    """
    if not getattr(settings, "WHATSAPP_INTAKE_ENABLED", False):
        logger.info("WhatsApp intake disabled; skipping scheduled run")
        return {"disabled": True}

    from common.db.locks import advisory_lock

    with advisory_lock("whatsapp_intake_run") as acquired:
        if not acquired:
            logger.info("Another WhatsApp intake run holds the lock; skipping")
            return {"skipped": "locked"}

        from apps.whatsapp_intake.services.intake_orchestrator import (
            run_whatsapp_intake,
        )

        result = run_whatsapp_intake(limit=None).as_dict()
        logger.info("Scheduled WhatsApp intake completed: %s", result)
        return result


@shared_task(
    bind=True,
    name="apps.whatsapp_intake.tasks.fetch_whatsapp_media",
    autoretry_for=(ConnectionError, TimeoutError),
    retry_backoff=True,
    retry_backoff_max=300,
    max_retries=3,
    acks_late=True,
)
def fetch_whatsapp_media(self, attachment_id):
    """Download and store one attachment's bytes."""
    if not getattr(settings, "WHATSAPP_INTAKE_ENABLED", False):
        return {"disabled": True}

    from apps.whatsapp_intake.services.media_service import download_attachment

    return download_attachment(attachment_id)


@shared_task(
    bind=True,
    name="apps.whatsapp_intake.tasks.retry_failed_media",
)
def retry_failed_media(self):
    """Sweep media whose Celery retries were exhausted during an outage.

    The media id stays valid for roughly 30 days, so a download that failed
    while Graph was unreachable is still recoverable long after the task's own
    retries ran out.
    """
    if not getattr(settings, "WHATSAPP_INTAKE_ENABLED", False):
        return {"disabled": True}

    from apps.whatsapp_intake.constants import MediaStatus
    from apps.whatsapp_intake.models import WhatsappAttachment
    from apps.whatsapp_intake.services.media_service import download_attachment

    stale = (
        WhatsappAttachment.objects
        .filter(
            download_status=MediaStatus.FAILED,
            is_rejected=False,
            fetch_attempts__lt=settings.WHATSAPP_MEDIA_MAX_ATTEMPTS,
        )
        .order_by("id")[:50]
    )

    retried = 0
    for attachment in stale:
        try:
            download_attachment(attachment.pk)
            retried += 1
        except Exception:  # noqa: BLE001 -- one bad file must not stop the sweep
            logger.exception("Retry failed for attachment %s", attachment.pk)
    return {"retried": retried}
