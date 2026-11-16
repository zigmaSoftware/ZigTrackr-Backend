"""One inbound 1:1 WhatsApp message.

Deliberately a plain models.Model rather than a BaseMaster, for the reasons
MailIntake documents: a received message is not a master row, it is never
soft-deleted, never "activated", and has no created_by because nobody in this
system created it.
"""

import uuid

from django.db import models

from apps.whatsapp_intake.constants import (
    MediaStatus,
    WhatsappErrorCode,
    WhatsappMessageType,
    WhatsappProcessingStatus,
)


class WhatsappMessage(models.Model):
    id = models.BigAutoField(primary_key=True)
    unique_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)

    # Nullable precisely so old events can be purged without touching messages.
    event = models.ForeignKey(
        "whatsapp_intake.WhatsappEvent", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="messages",
        db_column="whatsapp_event_id",
    )
    company = models.ForeignKey(
        "masters.CompanyMaster", on_delete=models.PROTECT, null=True, blank=True,
        related_name="whatsapp_messages",
    )

    # ---- PROVIDER IDENTIFIERS ----
    # Meta's message id. Indexed but NOT unique: see dedupe_key below.
    wamid = models.CharField(max_length=255)

    # The actual de-duplication key, and the concurrency guard behind it.
    #
    # Not a unique wamid, for the reason MailIntake gives for its own
    # dedupe_key: the key is company-scoped. If a business number is later
    # remapped to a different company, an old wamid must not suppress new
    # intake. A fixed 64-char sha256 also indexes more uniformly than a
    # variable-length provider id.
    dedupe_key = models.CharField(max_length=64)

    # OUR receiving business number. phone_number_id is the stable id;
    # display_phone_number is formatted inconsistently across payloads and can
    # change in Business Manager without the id changing, so company resolution
    # keys on the former.
    phone_number_id = models.CharField(max_length=50, blank=True, default="")
    display_phone_number = models.CharField(max_length=32, blank=True, default="")
    waba_id = models.CharField(max_length=50, blank=True, default="")

    # ---- SENDER ----
    # The sender's phone, digits only, no '+'. This is the stable sender key the
    # thread rule matches on.
    wa_id = models.CharField(max_length=32)
    # The WhatsApp profile display name. The sender edits this freely and it is
    # frequently blank, so it is for display only -- never match on it.
    from_name = models.CharField(max_length=150, blank=True, default="")

    # ---- CONTENT ----
    message_type = models.CharField(
        max_length=20, choices=WhatsappMessageType.choices,
        default=WhatsappMessageType.UNSUPPORTED,
    )
    body_text = models.TextField(blank=True, default="")
    normalized_body = models.TextField(blank=True, default="")
    body_hash = models.CharField(max_length=64, blank=True, default="")
    # Location lat/lng, the contacts array, the interactive reply id -- keeps
    # the evidence without a column per message type.
    structured_payload = models.JSONField(default=dict, blank=True)

    # ---- CONTEXT ----
    # message.context.id: the wamid the user swiped-to-reply on. Recorded
    # because it is the only provider-side thread signal there is.
    context_wamid = models.CharField(max_length=255, blank=True, default="")
    is_forwarded = models.BooleanField(default=False)

    # ---- TIMING ----
    # From Meta's unix-seconds timestamp, parsed as UTC. A naive parse would
    # silently shift every message by the Asia/Kolkata offset.
    sent_at = models.DateTimeField()
    received_at = models.DateTimeField()

    # ---- PROCESSING STATE ----
    processing_status = models.CharField(
        max_length=30, choices=WhatsappProcessingStatus.choices,
        default=WhatsappProcessingStatus.RECEIVED,
    )
    processing_attempts = models.PositiveSmallIntegerField(default=0)
    last_error_code = models.CharField(
        max_length=50, choices=WhatsappErrorCode.choices, blank=True, default="",
    )
    last_error_message = models.TextField(blank=True, default="")
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    media_status = models.CharField(
        max_length=20, choices=MediaStatus.choices,
        default=MediaStatus.NOT_APPLICABLE,
    )

    linked_ticket = models.ForeignKey(
        "tickets.SupportTicket", on_delete=models.PROTECT, null=True, blank=True,
        related_name="whatsapp_messages", db_column="linked_ticket_id",
    )

    # ---- FLAGS ----
    is_duplicate = models.BooleanField(default=False)
    is_thread_reply = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "whatsapp_message"
        ordering = ["-sent_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["dedupe_key"], name="uq_wa_msg_dedupe_key"
            ),
        ]
        indexes = [
            models.Index(
                fields=["company", "processing_status"],
                name="idx_wamsg_company_state",
            ),
            models.Index(
                fields=["processing_status", "sent_at"],
                name="idx_wamsg_status_sent",
            ),
            # THE thread-rule index. Column order is deliberate: equality,
            # equality, range.
            models.Index(
                fields=["company", "wa_id", "-sent_at"],
                name="idx_wamsg_company_wa_sent",
            ),
            models.Index(fields=["wamid"], name="idx_wamsg_wamid"),
            models.Index(fields=["context_wamid"], name="idx_wamsg_context_wamid"),
            models.Index(fields=["linked_ticket"], name="idx_wamsg_linked_ticket"),
            models.Index(fields=["media_status"], name="idx_wamsg_media_status"),
        ]

    def __str__(self):
        return f"{self.wa_id}: {self.body_text[:60]}"
