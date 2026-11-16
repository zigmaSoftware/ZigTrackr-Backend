"""Registering a WhatsApp message exactly once.

INSERT-first against the UNIQUE index on dedupe_key, catching IntegrityError --
the same strategy mail_intake uses, for the same reasons:

* SELECT ... FOR UPDATE cannot protect a row that does not exist yet. The race
  is two workers inserting the same NEW message; on a missing row InnoDB takes
  a gap lock, a well-known deadlock source under concurrent inserts of
  scattered keys. Wrong tool.
* The unique index is a guard the database already provides, and it is race-free
  by construction: exactly one INSERT wins and the loser re-reads.

Meta retries webhook deliveries aggressively, so this is the layer that makes
retries free. It is the second of three: WhatsappEvent.event_id catches an
identical HTTP redelivery, this catches the same wamid arriving in two
differently-batched envelopes, and create_ticket_from_whatsapp's early return
catches a reprocess.
"""

import hashlib
import logging

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.whatsapp_intake.constants import WhatsappProcessingStatus
from apps.whatsapp_intake.models import WhatsappMessage, WhatsappProcessingHistory

logger = logging.getLogger(__name__)


def compute_dedupe_key(*, wamid, company=None, phone_number_id=""):
    """Company-scoped hash of Meta's message id.

    Scoped rather than a bare unique wamid so that remapping a business number
    to a different company cannot have an old message suppress new intake --
    the same reasoning MailIntake.dedupe_key documents.
    """
    scope = (
        f"company:{company.unique_id}" if company is not None
        else f"unmapped:{phone_number_id or ''}"
    )
    return hashlib.sha256(f"{scope}:{wamid}".encode("utf-8")).hexdigest()


def find_existing(dedupe_key):
    if not dedupe_key:
        return None
    return WhatsappMessage.objects.filter(dedupe_key=dedupe_key).first()


def register_message(parsed, *, company=None, event=None):
    """Persist a newly received message. Returns (message, created).

    Never raises IntegrityError to the caller: a losing race is an ordinary
    outcome here, not an error.
    """
    from apps.whatsapp_intake.constants import MediaStatus, MEDIA_MESSAGE_TYPES

    dedupe_key = compute_dedupe_key(
        wamid=parsed.wamid, company=company,
        phone_number_id=parsed.phone_number_id,
    )

    existing = find_existing(dedupe_key)
    if existing is not None:
        return existing, False

    media_status = (
        MediaStatus.PENDING if parsed.message_type in MEDIA_MESSAGE_TYPES
        else MediaStatus.NOT_APPLICABLE
    )

    try:
        # The inner atomic() is a savepoint, and it is load-bearing: once a
        # statement raises IntegrityError the transaction is marked for
        # rollback, so without this the caller's transaction would be poisoned.
        with transaction.atomic():
            message = WhatsappMessage.objects.create(
                event=event,
                company=company,
                wamid=parsed.wamid,
                dedupe_key=dedupe_key,
                phone_number_id=parsed.phone_number_id,
                display_phone_number=parsed.display_phone_number,
                waba_id=parsed.waba_id,
                wa_id=parsed.wa_id,
                from_name=parsed.from_name,
                message_type=parsed.message_type,
                body_text=parsed.body_text,
                normalized_body=(parsed.body_text or "").strip().lower(),
                body_hash=parsed.body_hash,
                structured_payload=parsed.structured_payload or {},
                context_wamid=parsed.context_wamid,
                is_forwarded=parsed.is_forwarded,
                sent_at=parsed.sent_at or timezone.now(),
                received_at=timezone.now(),
                processing_status=WhatsappProcessingStatus.RECEIVED,
                media_status=media_status,
            )
            WhatsappProcessingHistory.objects.create(
                message=message,
                from_status="",
                to_status=WhatsappProcessingStatus.RECEIVED,
                action="RECEIVED",
                remarks=f"Received from {parsed.wa_id}.",
            )
        return message, True
    except IntegrityError:
        logger.info(
            "Concurrent registration lost for wamid=%s", (parsed.wamid or "")[:24],
        )
        existing = find_existing(dedupe_key)
        if existing is None:
            raise
        return existing, False


@transaction.atomic
def mark_duplicate(*, message, actor=None):
    """Park a message that duplicates one already processed."""
    from apps.whatsapp_intake.constants import WhatsappErrorCode
    from apps.whatsapp_intake.services.history_service import transition

    message.is_duplicate = True
    message.save(update_fields=["is_duplicate", "updated_at"])
    transition(
        message=message,
        to_status=WhatsappProcessingStatus.DUPLICATE,
        action="DUPLICATE",
        remarks="Already processed.",
        error_code=WhatsappErrorCode.DUPLICATE,
        actor=actor,
        processed=True,
    )
    return message
