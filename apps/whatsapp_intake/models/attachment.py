"""Media attached to an inbound WhatsApp message.

Field names mirror MailAttachment (and through it bugs.BugAttachment) so that
copying a WhatsApp attachment onto a bug later is a field-for-field map and one
download view can serve all three.

WhatsApp media differs from a mail part in one important way: the bytes are not
in the webhook. The payload carries a media_id that must be fetched from the
Graph API in two authenticated steps, so a row exists before its file does.
"""

import uuid

from django.db import models

from apps.whatsapp_intake.constants import MediaStatus


class WhatsappAttachment(models.Model):
    id = models.BigAutoField(primary_key=True)
    unique_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)

    message = models.ForeignKey(
        "whatsapp_intake.WhatsappMessage", on_delete=models.PROTECT,
        related_name="attachments", db_column="whatsapp_message_id",
    )

    # ---- PROVIDER SIDE ----
    # Valid for roughly 30 days, unlike the download URL it yields, which
    # expires in about five minutes. This is what a retry re-resolves.
    media_id = models.CharField(max_length=100, blank=True, default="")
    # What Meta claimed. Kept for comparison only -- file_type below is what we
    # actually sniffed and what any response must use.
    mime_type_declared = models.CharField(max_length=100, blank=True, default="")
    # Meta publishes a sha256. Comparing it against our own post-download
    # checksum is a free integrity check; a mismatch is logged, not rejected,
    # because Meta transcodes some media.
    sha256_declared = models.CharField(max_length=64, blank=True, default="")

    # ---- STORED FILE ----
    # Sanitised for display only. The sender's filename is never used to build a
    # path; stored_file_name is a generated UUID.
    file_name = models.CharField(max_length=255)
    stored_file_name = models.CharField(max_length=255, blank=True, default="")
    file_path = models.CharField(max_length=500, blank=True, default="")
    # Sniffed from the leading bytes, never the declared mime type.
    file_type = models.CharField(max_length=100, blank=True, default="")
    file_extension = models.CharField(max_length=10, blank=True, default="")
    file_size = models.PositiveBigIntegerField(default=0)
    checksum_sha256 = models.CharField(max_length=64, blank=True, default="")

    # audio.voice: a voice note rather than an attached audio file. Worth
    # distinguishing because the UI plays one inline and offers the other.
    voice = models.BooleanField(default=False)

    # A rejected attachment still gets a row, with no bytes stored: silently
    # dropping it leaves a reviewer wondering why the message mentions a
    # screenshot that is not there.
    is_rejected = models.BooleanField(default=False)
    rejection_reason = models.CharField(max_length=255, blank=True, default="")

    download_status = models.CharField(
        max_length=20, choices=MediaStatus.choices, default=MediaStatus.PENDING,
    )
    fetch_attempts = models.PositiveSmallIntegerField(default=0)
    fetched_at = models.DateTimeField(null=True, blank=True)
    last_error_message = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "whatsapp_attachment"
        ordering = ["id"]
        indexes = [
            models.Index(fields=["message"], name="idx_waatt_message"),
            models.Index(fields=["media_id"], name="idx_waatt_media_id"),
            models.Index(fields=["stored_file_name"], name="idx_waatt_stored"),
            models.Index(fields=["download_status"], name="idx_waatt_dl_status"),
        ]

    def __str__(self):
        return self.file_name
