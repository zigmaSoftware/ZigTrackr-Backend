"""Immutable WhatsApp processing history.

Extends ImmutableHistoryModel, so an UPDATE or DELETE against a recorded
transition raises rather than quietly rewriting the record. Structurally
identical to MailProcessingHistory so one timeline renderer can serve both.
"""

from django.conf import settings
from django.db import models

from apps.whatsapp_intake.constants import WhatsappErrorCode, WhatsappProcessingStatus
from common.models.immutable import ImmutableHistoryModel


class WhatsappProcessingHistory(ImmutableHistoryModel):
    id = models.BigAutoField(primary_key=True)

    message = models.ForeignKey(
        "whatsapp_intake.WhatsappMessage", on_delete=models.PROTECT,
        related_name="processing_history", db_column="whatsapp_message_id",
    )
    # Blank on the opening row: "this is where the record begins".
    from_status = models.CharField(
        max_length=30, choices=WhatsappProcessingStatus.choices,
        blank=True, default="",
    )
    to_status = models.CharField(
        max_length=30, choices=WhatsappProcessingStatus.choices,
    )
    action = models.CharField(max_length=60)
    remarks = models.TextField(blank=True, default="")
    error_code = models.CharField(
        max_length=50, choices=WhatsappErrorCode.choices, blank=True, default="",
    )

    # Nullable SET_NULL: most transitions here are made by the Beat task with no
    # user at all, and recording that honestly matters -- a system action must
    # not impersonate an administrator.
    performed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="whatsapp_history_entries", db_column="performed_by",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "whatsapp_processing_history"
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(
                fields=["message", "-created_at"], name="idx_waph_msg_created"
            ),
            models.Index(fields=["to_status", "created_at"], name="idx_waph_to_status"),
        ]

    def __str__(self):
        return f"{self.from_status or '-'} -> {self.to_status}"
