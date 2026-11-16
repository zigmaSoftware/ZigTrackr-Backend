"""Downloading WhatsApp media from the Graph API.

The bytes are not in the webhook. The payload carries a media id, and getting
the file takes two authenticated requests:

    Step 1  GET {graph}/{version}/{media_id}
            -> {"url": "https://lookaside.fbsbx.com/...", "mime_type": ...,
                "sha256": ..., "file_size": ...}
    Step 2  GET <that url>  WITH the Authorization header still attached

Two things about step 2 catch people out. The URL expires in roughly five
minutes, so it is never persisted -- a retry redoes step 1, because the media
id itself stays valid for about thirty days. And despite looking like a CDN
link, it is not public: omitting the bearer token returns 401.

This is the only outbound HTTP in the codebase, so every call carries an
explicit timeout. A hung peer must not stall the worker.
"""

import logging
import uuid

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from rest_framework import serializers as drf_serializers

from apps.whatsapp_intake.constants import MediaStatus, WhatsappErrorCode
from common.validators.files import sanitize_filename, validate_upload

logger = logging.getLogger(__name__)


class MediaFetchError(Exception):
    """A download failed. `permanent` decides whether a retry could help."""

    def __init__(self, message, *, code=WhatsappErrorCode.MEDIA_DOWNLOAD_ERROR,
                 permanent=False):
        super().__init__(message)
        self.code = code
        self.permanent = permanent


def _first_error(exc):
    detail = getattr(exc, "detail", None)
    if isinstance(detail, dict):
        for value in detail.values():
            if isinstance(value, (list, tuple)) and value:
                return str(value[0])
            return str(value)
    if isinstance(detail, (list, tuple)) and detail:
        return str(detail[0])
    return str(exc)


def _session():
    try:
        import requests
    except ImportError as exc:  # pragma: no cover - deployment guard
        raise MediaFetchError(
            "The 'requests' package is required for WhatsApp media download.",
            code=WhatsappErrorCode.GRAPH_API_ERROR, permanent=True,
        ) from exc
    return requests


def resolve_media_url(media_id, *, timeout=None):
    """Step 1: media id -> a short-lived download URL plus its metadata."""
    requests = _session()
    timeout = timeout or settings.WHATSAPP_MEDIA_TIMEOUT
    url = (
        f"{settings.WHATSAPP_GRAPH_BASE_URL.rstrip('/')}"
        f"/{settings.WHATSAPP_API_VERSION}/{media_id}"
    )
    try:
        response = requests.get(
            url,
            headers={"Authorization": f"Bearer {settings.WHATSAPP_ACCESS_TOKEN}"},
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001
        raise MediaFetchError(f"Graph lookup failed: {exc}") from exc

    if response.status_code == 404:
        # The media id is gone, not merely unreachable. No retry can fix it.
        raise MediaFetchError(
            "Media no longer available.",
            code=WhatsappErrorCode.MEDIA_EXPIRED, permanent=True,
        )
    if response.status_code >= 400:
        raise MediaFetchError(
            f"Graph lookup returned {response.status_code}.",
            code=WhatsappErrorCode.GRAPH_API_ERROR,
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise MediaFetchError("Graph lookup returned invalid JSON.") from exc

    if not payload.get("url"):
        raise MediaFetchError("Graph lookup returned no download URL.")
    return payload


def download_media_bytes(url, *, timeout=None, max_bytes=None):
    """Step 2: fetch the bytes, enforcing the size cap while streaming.

    Capped during the stream rather than after, so an oversized file is
    abandoned mid-transfer instead of being held in memory first. Meta reports
    a file_size in step 1, but that value is advisory.
    """
    requests = _session()
    timeout = timeout or settings.WHATSAPP_MEDIA_TIMEOUT
    max_bytes = max_bytes or (settings.ATTACHMENT_MAX_SIZE_MB * 1024 * 1024)

    try:
        response = requests.get(
            url,
            # Still bearer-authenticated. This URL is not public, however much
            # it looks like a CDN link.
            headers={"Authorization": f"Bearer {settings.WHATSAPP_ACCESS_TOKEN}"},
            timeout=timeout,
            stream=True,
        )
    except Exception as exc:  # noqa: BLE001
        raise MediaFetchError(f"Media download failed: {exc}") from exc

    if response.status_code >= 400:
        raise MediaFetchError(
            f"Media download returned {response.status_code}.",
            code=WhatsappErrorCode.GRAPH_API_ERROR,
        )

    chunks = []
    total = 0
    for chunk in response.iter_content(chunk_size=65536):
        if not chunk:
            continue
        total += len(chunk)
        if total > max_bytes:
            raise MediaFetchError(
                f"Media exceeds the {settings.ATTACHMENT_MAX_SIZE_MB} MB limit.",
                code=WhatsappErrorCode.MESSAGE_TOO_LARGE, permanent=True,
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _extension_hint(attachment, payload):
    """A filename the validator can read an extension from.

    WhatsApp supplies a filename only for documents; everything else is
    identified by mime type, so one is synthesised. The extension is a hint for
    the allow-list check -- the stored file_type always comes from sniffing the
    leading bytes.
    """
    if attachment.file_name and "." in attachment.file_name:
        return sanitize_filename(attachment.file_name)

    mime = (payload.get("mime_type") or attachment.mime_type_declared or "")
    subtype = mime.split("/")[-1].split(";")[0].strip().lower()
    mapping = {
        "jpeg": "jpg", "svg+xml": "svg", "plain": "txt",
        "mpeg": "mp3", "quicktime": "mov", "x-wav": "wav",
        "vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
        "vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
        "msword": "doc", "vnd.ms-excel": "xls",
    }
    extension = mapping.get(subtype, subtype or "bin")
    return f"{attachment.media_id or uuid.uuid4().hex}.{extension}"


def _reject(attachment, reason):
    attachment.is_rejected = True
    attachment.rejection_reason = reason[:255]
    attachment.download_status = MediaStatus.FAILED
    attachment.last_error_message = reason[:2000]
    attachment.save(update_fields=[
        "is_rejected", "rejection_reason", "download_status",
        "last_error_message",
    ])
    return {"attachment": attachment.pk, "rejected": True, "reason": reason}


def download_attachment(attachment_id):
    """Fetch, validate and store one attachment's bytes.

    Runs outside any transaction, deliberately: an orphaned file on disk is
    sweepable, whereas a row pointing at a file that was never written is not.
    """
    from apps.whatsapp_intake.models import WhatsappAttachment

    attachment = WhatsappAttachment.objects.filter(pk=attachment_id).first()
    if attachment is None:
        logger.warning("WhatsApp attachment %s no longer exists", attachment_id)
        return {"attachment": attachment_id, "missing": True}

    if attachment.download_status == MediaStatus.STORED or attachment.is_rejected:
        return {"attachment": attachment.pk, "skipped": True}

    attachment.download_status = MediaStatus.FETCHING
    attachment.fetch_attempts = (attachment.fetch_attempts or 0) + 1
    attachment.save(update_fields=["download_status", "fetch_attempts"])

    try:
        payload = resolve_media_url(attachment.media_id)
        content = download_media_bytes(payload["url"])
    except MediaFetchError as exc:
        if exc.permanent:
            return _reject(attachment, str(exc))
        attachment.download_status = MediaStatus.FAILED
        attachment.last_error_message = str(exc)[:2000]
        attachment.save(update_fields=["download_status", "last_error_message"])
        logger.warning(
            "WhatsApp media %s download failed: %s", attachment.media_id, exc,
        )
        return {"attachment": attachment.pk, "failed": True, "reason": str(exc)}

    display_name = sanitize_filename(_extension_hint(attachment, payload))
    candidate = SimpleUploadedFile(
        name=display_name,
        content=content,
        content_type=payload.get("mime_type") or "application/octet-stream",
    )

    try:
        extension, mime, checksum = validate_upload(candidate)
    except drf_serializers.ValidationError as exc:
        return _reject(attachment, _first_error(exc))

    declared = (attachment.sha256_declared or payload.get("sha256") or "").strip()
    if declared and declared != checksum:
        # Logged, not rejected: Meta transcodes some media, so a mismatch is
        # suspicious rather than proof of tampering.
        logger.warning(
            "WhatsApp media %s checksum differs from the declared value",
            attachment.media_id,
        )

    stored_name = f"{uuid.uuid4().hex}.{extension}"
    message = attachment.message
    # Keyed on unique_id rather than the sequential pk, so the directory is not
    # guessable. MEDIA_ROOT is not served statically anyway; downloads go
    # through a permission-gated view.
    path = (
        f"whatsapp_media/{message.sent_at:%Y/%m}/{message.unique_id}/{stored_name}"
    )

    try:
        saved_path = default_storage.save(path, ContentFile(content))
    except OSError as exc:
        attachment.download_status = MediaStatus.FAILED
        attachment.last_error_message = str(exc)[:2000]
        attachment.save(update_fields=["download_status", "last_error_message"])
        logger.exception("Failed to store WhatsApp media %s", attachment.media_id)
        raise

    attachment.file_name = display_name
    attachment.stored_file_name = stored_name
    attachment.file_path = saved_path
    attachment.file_type = mime
    attachment.file_extension = extension
    attachment.file_size = len(content)
    attachment.checksum_sha256 = checksum
    attachment.download_status = MediaStatus.STORED
    attachment.fetched_at = timezone.now()
    attachment.last_error_message = ""
    attachment.save(update_fields=[
        "file_name", "stored_file_name", "file_path", "file_type",
        "file_extension", "file_size", "checksum_sha256", "download_status",
        "fetched_at", "last_error_message",
    ])

    _refresh_message_media_status(message)
    return {"attachment": attachment.pk, "stored": True, "bytes": len(content)}


def _refresh_message_media_status(message):
    """Roll the per-attachment states up to the message.

    The UI reads this to show "1 attachment downloading..." without counting
    rows on every render.
    """
    states = set(
        message.attachments.values_list("download_status", flat=True)
    )
    if not states:
        new_status = MediaStatus.NOT_APPLICABLE
    elif states <= {MediaStatus.STORED}:
        new_status = MediaStatus.STORED
    elif MediaStatus.PENDING in states or MediaStatus.FETCHING in states:
        new_status = MediaStatus.PENDING
    else:
        new_status = MediaStatus.FAILED

    if message.media_status != new_status:
        message.media_status = new_status
        message.save(update_fields=["media_status", "updated_at"])
