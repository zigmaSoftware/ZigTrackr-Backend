"""The pipeline assembly.

This is the only module that knows the whole story. Everything below it does
one job and is independently testable.

Transaction boundaries matter here as much as they do in the mail pipeline,
but there are fewer of them. Mail holds four short transactions because it does
network I/O mid-flight (attachment storage, then SMTP). WhatsApp does none:
media is downloaded by a separate task and phase 1 sends no acknowledgement, so
processing one message is pure database work:

    TXN-1  register the message                  -> commit
           (media download tasks enqueued on_commit)
           thread lookup (reads only)
    TXN-2  thread update, or create the ticket   -> commit

A crash between the two leaves the message in a well-defined status that the
retry sweep resumes from -- which is why status is persisted per step rather
than once at the end.
"""

import logging
from dataclasses import dataclass, field

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.whatsapp_intake.constants import (
    DISCARD_TYPES,
    EventStatus,
    RETRYABLE_WHATSAPP_STATUSES,
    WhatsappErrorCode,
    WhatsappProcessingStatus,
)
from apps.whatsapp_intake.models import WhatsappEvent, WhatsappMessage
from apps.whatsapp_intake.services import duplicate_service
from apps.whatsapp_intake.services.company_resolver import resolve_company
from apps.whatsapp_intake.services.error_service import classify_error, should_retry
from apps.whatsapp_intake.services.history_service import record_attempt, transition
from apps.whatsapp_intake.services.payload_parser import parse_webhook_payload
from apps.whatsapp_intake.services.thread_service import (
    apply_thread_update, find_open_whatsapp_ticket,
)

logger = logging.getLogger(__name__)


@dataclass
class WhatsappRunResult:
    events_parsed: int = 0
    messages_registered: int = 0
    duplicates: int = 0
    rejected: int = 0
    statuses_ignored: int = 0
    thread_updates: int = 0
    tickets_created: int = 0
    failed: int = 0
    message_ids: list = field(default_factory=list)

    def as_dict(self):
        return {
            "events_parsed": self.events_parsed,
            "messages_registered": self.messages_registered,
            "duplicates": self.duplicates,
            "rejected": self.rejected,
            "statuses_ignored": self.statuses_ignored,
            "thread_updates": self.thread_updates,
            "tickets_created": self.tickets_created,
            "failed": self.failed,
        }


def _reject(message, error_code, *, remarks="", result=None, actor=None):
    transition(
        message=message,
        to_status=WhatsappProcessingStatus.REJECTED,
        action="REJECTED",
        remarks=remarks or error_code,
        error_code=error_code,
        actor=actor,
        processed=True,
    )
    if result is not None:
        result.rejected += 1
    return message


def _fail(message, exc, *, result=None, actor=None):
    """Record a processing failure without letting it abort the run."""
    error_code, _is_temporary = classify_error(exc)
    retryable = should_retry(
        error_code=error_code,
        attempts=message.processing_attempts,
        max_attempts=settings.WHATSAPP_MAX_ATTEMPTS,
    )
    logger.exception(
        "WhatsApp message %s failed (%s, retryable=%s)",
        message.pk, error_code, retryable,
    )
    transition(
        message=message,
        to_status=(
            WhatsappProcessingStatus.PROCESSING_FAILED if retryable
            else WhatsappProcessingStatus.REJECTED
        ),
        action="FAILED",
        remarks=str(exc)[:2000],
        error_code=error_code,
        actor=actor,
        processed=not retryable,
    )
    if result is not None:
        result.failed += 1
    return message


def _has_content(message):
    """Is there anything here a human could act on?"""
    if (message.body_text or "").strip():
        return True
    if message.structured_payload:
        return True
    return message.attachments.exists()


def _sender_allowed(wa_id):
    """Is this sender permitted to open tickets?

    While WHATSAPP_ALLOWED_SENDERS is empty the gate is open -- production
    behaviour. While it is non-empty (the testing phase), only those numbers
    pass. Matching is on digits only, and a configured number that is 10 digits
    or shorter is treated as a suffix so a bare local number matches its
    country-prefixed wa_id (6379088711 matches 916379088711).
    """
    allowed = getattr(settings, "WHATSAPP_ALLOWED_SENDERS", None) or []
    if not allowed:
        return True

    digits = "".join(ch for ch in (wa_id or "") if ch.isdigit())
    if not digits:
        return False

    for entry in allowed:
        if digits == entry:
            return True
        # A short configured number (a local number without the country code)
        # matches as a suffix; a full one must match exactly.
        if len(entry) <= 10 and digits.endswith(entry):
            return True
    return False


def _continue_processing(message, *, result=None, actor=None, request=None):
    """Carry one registered message to a resting state."""
    record_attempt(message=message)

    try:
        # 0. Sender allow-list. During testing this restricts ticket creation
        #    to a known number; empty in production, where it is a no-op. The
        #    message is still stored, so the Inbox shows it arrived and was
        #    filtered rather than lost.
        if not _sender_allowed(message.wa_id):
            return _reject(
                message, WhatsappErrorCode.SENDER_NOT_ALLOWED,
                remarks=f"Sender {message.wa_id} is not in the testing allow-list.",
                result=result, actor=actor,
            )

        # 1. Type gate. Reactions and stickers are kept as rows so "did they
        #    reply?" has a truthful answer, but never become tickets.
        if message.message_type in DISCARD_TYPES:
            return _reject(
                message, WhatsappErrorCode.UNSUPPORTED_TYPE,
                remarks=f"{message.message_type} messages do not open tickets.",
                result=result, actor=actor,
            )

        # 2. Content gate.
        if not _has_content(message):
            return _reject(
                message, WhatsappErrorCode.EMPTY_CONTENT,
                remarks="Message carries no usable content.",
                result=result, actor=actor,
            )

        # 3. Company gate. An unattributed message is visible only to Super
        #    Admin; guessing a company would leak it into the wrong queue.
        if not message.company_id:
            return _reject(
                message, WhatsappErrorCode.UNMAPPED_PHONE_NUMBER_ID,
                remarks=(
                    f"Business number {message.phone_number_id} is not mapped "
                    "to a company."
                ),
                result=result, actor=actor,
            )

        # 4. Thread rule. Reads only; no transaction held across it.
        match = find_open_whatsapp_ticket(message)
        if match.matched:
            apply_thread_update(
                message=message, ticket=match.ticket, actor=actor, request=request,
            )
            if result is not None:
                result.thread_updates += 1
            return message

        # 5. Create. No classification: needs_review is always True.
        from apps.tickets.services.ticket_service import create_ticket_from_whatsapp

        with transaction.atomic():
            ticket = create_ticket_from_whatsapp(
                message=message, actor=actor, request=request,
            )
            transition(
                message=message,
                to_status=WhatsappProcessingStatus.TICKET_CREATED,
                action="TICKET_CREATED",
                remarks=f"Created {ticket.reference}.",
                actor=actor,
                processed=True,
            )
        if result is not None:
            result.tickets_created += 1
            result.message_ids.append(message.pk)
        return message

    except Exception as exc:  # noqa: BLE001 -- one bad message must not abort the run
        return _fail(message, exc, result=result, actor=actor)


def _queue_media(message, parsed):
    """Create attachment rows and schedule their downloads.

    Enqueued via on_commit: handing a worker a row id before the transaction
    commits is how you get a task racing a row that does not exist yet.
    """
    if not parsed.media:
        return

    from apps.whatsapp_intake.models import WhatsappAttachment

    for item in parsed.media:
        if not item.media_id:
            continue
        attachment = WhatsappAttachment.objects.create(
            message=message,
            media_id=item.media_id,
            mime_type_declared=item.mime_type,
            sha256_declared=item.sha256,
            file_name=item.file_name or item.media_id,
            voice=item.voice,
        )
        attachment_id = attachment.pk

        def _enqueue(pk=attachment_id):
            from apps.whatsapp_intake.tasks import fetch_whatsapp_media

            fetch_whatsapp_media.apply_async(args=[pk], queue="whatsapp-media")

        transaction.on_commit(_enqueue)


def process_parsed_message(parsed, *, event=None, result=None, actor=None,
                           request=None):
    """Register one parsed message and carry it through the pipeline."""
    company = resolve_company(parsed.phone_number_id)

    message, created = duplicate_service.register_message(
        parsed, company=company, event=event,
    )

    if not created:
        if message.processing_status in (
            WhatsappProcessingStatus.RECEIVED,
            WhatsappProcessingStatus.PROCESSING_FAILED,
        ):
            # Registered earlier but never finished; resume rather than skip.
            return _continue_processing(
                message, result=result, actor=actor, request=request,
            )
        if result is not None:
            result.duplicates += 1
        return message

    if result is not None:
        result.messages_registered += 1

    _queue_media(message, parsed)
    return _continue_processing(
        message, result=result, actor=actor, request=request,
    )


def _process_event(event, *, result, actor=None, request=None):
    event.processing_attempts = (event.processing_attempts or 0) + 1
    try:
        parsed = parse_webhook_payload(event.payload)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to parse WhatsApp event %s", event.pk)
        event.processing_status = EventStatus.FAILED
        event.last_error_message = str(exc)[:2000]
        event.processed_at = timezone.now()
        event.save(update_fields=[
            "processing_status", "processing_attempts",
            "last_error_message", "processed_at",
        ])
        return

    result.statuses_ignored += parsed.status_count

    for parsed_message in parsed.messages:
        try:
            process_parsed_message(
                parsed_message, event=event, result=result,
                actor=actor, request=request,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "Failed to register WhatsApp message %s",
                (parsed_message.wamid or "")[:24],
            )
            result.failed += 1

    event.processing_status = (
        EventStatus.PARSED if parsed.messages else EventStatus.IGNORED
    )
    event.message_count = len(parsed.messages)
    event.status_count = parsed.status_count
    event.processed_at = timezone.now()
    event.save(update_fields=[
        "processing_status", "processing_attempts", "message_count",
        "status_count", "processed_at",
    ])
    result.events_parsed += 1


def retry_pending(*, limit=50, actor=None, result=None, request=None):
    """Resume messages left mid-pipeline by an earlier crash."""
    pending = (
        WhatsappMessage.objects
        .filter(
            processing_status__in=RETRYABLE_WHATSAPP_STATUSES,
            processing_attempts__lt=settings.WHATSAPP_MAX_ATTEMPTS,
        )
        .order_by("sent_at")[:limit]
    )
    for message in pending:
        _continue_processing(
            message, result=result, actor=actor, request=request,
        )
    return result


def run_whatsapp_intake(*, limit=None, actor=None, request=None):
    """Turn stored webhook deliveries into tickets.

    Needs no network connection: the payloads are already persisted. That is
    the payoff for having the webhook write raw and acknowledge immediately.
    """
    result = WhatsappRunResult()
    limit = limit or settings.WHATSAPP_BATCH_LIMIT

    retry_pending(limit=limit, actor=actor, result=result, request=request)

    events = (
        WhatsappEvent.objects
        .filter(
            processing_status=EventStatus.PENDING,
            processing_attempts__lt=settings.WHATSAPP_MAX_ATTEMPTS,
        )
        .order_by("id")[:limit]
    )
    for event in events:
        _process_event(event, result=result, actor=actor, request=request)

    return result
