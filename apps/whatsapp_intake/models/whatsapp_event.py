"""The raw webhook envelope.

Mail has no equivalent, and this exists for one reason: Meta retries any POST
that does not return 200 within seconds, so the webhook must ACK in
milliseconds and cannot afford to parse. One row per HTTP POST -- not per
message, because a single POST can carry several messages[] and several
statuses[].

event_id is sha256 of the raw body. Meta re-POSTs the BYTE-IDENTICAL body on a
non-2xx or a timeout, so hashing the bytes makes a redelivery hit the unique
index and be dropped before any parsing happens. The per-message wamid dedupe
is the second, authoritative layer: the same message can legitimately arrive in
two differently-batched envelopes.
"""

import uuid

from django.db import models

from apps.whatsapp_intake.constants import EventStatus


class WhatsappEvent(models.Model):
    id = models.BigAutoField(primary_key=True)
    unique_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)

    event_id = models.CharField(max_length=64)
    # Stored verbatim. audit_log.metadata and mail_intake.to_emails are the
    # precedent that JSONField works on this MariaDB deployment. Never
    # re-serialise: the event_id hash and the stored copy would diverge.
    payload = models.JSONField(default=dict, blank=True)
    signature_header = models.CharField(max_length=255, blank=True, default="")
    raw_size_bytes = models.PositiveIntegerField(default=0)

    processing_status = models.CharField(
        max_length=20, choices=EventStatus.choices, default=EventStatus.PENDING,
    )
    processing_attempts = models.PositiveSmallIntegerField(default=0)
    last_error_message = models.TextField(blank=True, default="")
    processed_at = models.DateTimeField(null=True, blank=True)

    # Counted at parse time, for operator visibility without re-reading payload.
    message_count = models.PositiveSmallIntegerField(default=0)
    status_count = models.PositiveSmallIntegerField(default=0)

    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "whatsapp_event"
        ordering = ["-id"]
        constraints = [
            models.UniqueConstraint(fields=["event_id"], name="uq_wa_event_id"),
        ]
        indexes = [
            models.Index(
                fields=["processing_status", "received_at"],
                name="idx_waev_status_recvd",
            ),
            models.Index(fields=["received_at"], name="idx_waev_received_at"),
        ]

    def __str__(self):
        return f"{self.event_id[:16]} ({self.processing_status})"
