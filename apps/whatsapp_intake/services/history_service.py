"""The single choke point for WhatsApp message status changes.

Every transition goes through transition(), so processing_status and the
immutable history log can never disagree -- the same discipline
mail_intake/services/history_service.py applies, and for the same reason.
"""

from django.db import transaction
from django.utils import timezone

from apps.whatsapp_intake.models import WhatsappProcessingHistory


@transaction.atomic
def transition(*, message, to_status, action, remarks="", error_code="",
               actor=None, processed=False):
    """Move `message` to `to_status` and append the matching history row."""
    from_status = message.processing_status

    history = WhatsappProcessingHistory.objects.create(
        message=message,
        from_status=from_status or "",
        to_status=to_status,
        action=action,
        remarks=remarks[:2000] if remarks else "",
        error_code=error_code or "",
        performed_by=actor if getattr(actor, "pk", None) else None,
    )

    message.processing_status = to_status
    update_fields = ["processing_status", "updated_at"]

    if error_code:
        message.last_error_code = error_code
        message.last_error_message = (remarks or "")[:2000]
        update_fields += ["last_error_code", "last_error_message"]

    if processed:
        message.processed_at = timezone.now()
        update_fields.append("processed_at")

    message.save(update_fields=update_fields)
    return history


@transaction.atomic
def record_attempt(*, message):
    """Count one processing pass over this message."""
    message.processing_attempts = (message.processing_attempts or 0) + 1
    message.last_attempt_at = timezone.now()
    message.save(update_fields=[
        "processing_attempts", "last_attempt_at", "updated_at",
    ])
    return message.processing_attempts
