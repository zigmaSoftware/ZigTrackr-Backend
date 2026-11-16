"""Storing one raw webhook delivery.

Separated from the view so the view stays a thin security wrapper: verify,
store, acknowledge. Nothing here parses the payload beyond counting what is in
it, because the webhook must return 200 in milliseconds -- Meta retries any
delivery it does not see acknowledged.
"""

import json
import logging

from django.db import IntegrityError, transaction

from apps.whatsapp_intake.constants import EventStatus
from apps.whatsapp_intake.models import WhatsappEvent

logger = logging.getLogger(__name__)


def _shallow_counts(payload):
    """Count messages and statuses without trusting the shape.

    Deliberately tolerant: a malformed payload still gets a row with zero
    counts, so the bytes are preserved for diagnosis rather than discarded.
    """
    messages = statuses = 0
    try:
        for entry in payload.get("entry") or []:
            for change in entry.get("changes") or []:
                value = change.get("value") or {}
                messages += len(value.get("messages") or [])
                statuses += len(value.get("statuses") or [])
    except (AttributeError, TypeError):
        return 0, 0
    return messages, statuses


def store_raw_event(*, raw_body, event_id, signature_header=""):
    """Persist one delivery. Returns (event, created).

    INSERT-first against the unique index on event_id, catching IntegrityError
    -- the same approach duplicate_service uses and for the same reason: the
    race is two workers inserting the same NEW row, and SELECT FOR UPDATE
    cannot lock a row that does not exist.

    Meta re-POSTs the byte-identical body on a non-2xx or a timeout, so this is
    what makes a redelivery free.
    """
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        # A valid signature over invalid JSON means Meta really sent this and
        # something upstream is wrong. Keep the bytes; do not make Meta retry.
        logger.warning("WhatsApp webhook delivery %s is not valid JSON", event_id[:16])
        payload = {}

    if not isinstance(payload, dict):
        payload = {}

    message_count, status_count = _shallow_counts(payload)

    existing = WhatsappEvent.objects.filter(event_id=event_id).first()
    if existing is not None:
        logger.info("Duplicate WhatsApp webhook delivery %s ignored", event_id[:16])
        return existing, False

    try:
        with transaction.atomic():
            event = WhatsappEvent.objects.create(
                event_id=event_id,
                payload=payload,
                signature_header=signature_header[:255],
                raw_size_bytes=len(raw_body),
                processing_status=EventStatus.PENDING,
                message_count=message_count,
                status_count=status_count,
            )
        return event, True
    except IntegrityError:
        logger.info("Concurrent WhatsApp delivery %s lost the race", event_id[:16])
        existing = WhatsappEvent.objects.filter(event_id=event_id).first()
        if existing is None:
            raise
        return existing, False
