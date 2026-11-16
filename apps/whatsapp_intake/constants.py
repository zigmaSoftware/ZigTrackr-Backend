"""WhatsApp intake vocabulary.

Mirrors apps/mail_intake/constants.py deliberately: the same status names are
used wherever the concept exists, so a future unified intake view can filter on
one shared set of strings with no translation table.
"""

from django.db import models


class EventStatus(models.TextChoices):
    """Lifecycle of one raw webhook POST."""

    PENDING = "PENDING", "Pending"
    PARSED = "PARSED", "Parsed"
    IGNORED = "IGNORED", "Ignored"
    FAILED = "FAILED", "Failed"


class WhatsappProcessingStatus(models.TextChoices):
    RECEIVED = "RECEIVED", "Received"
    VALIDATING = "VALIDATING", "Validating"
    REJECTED = "REJECTED", "Rejected"
    DUPLICATE = "DUPLICATE", "Duplicate"
    THREAD_UPDATE = "THREAD_UPDATE", "Thread Update"
    AWAITING_MEDIA = "AWAITING_MEDIA", "Awaiting Media"
    TICKET_CREATED = "TICKET_CREATED", "Ticket Created"
    PROCESSING_FAILED = "PROCESSING_FAILED", "Processing Failed"


# Reached a resting place; the retry sweep leaves these alone.
TERMINAL_WHATSAPP_STATUSES = (
    WhatsappProcessingStatus.REJECTED,
    WhatsappProcessingStatus.DUPLICATE,
    WhatsappProcessingStatus.THREAD_UPDATE,
    WhatsappProcessingStatus.TICKET_CREATED,
)

# Mid-pipeline, so a later run can resume them from persisted state.
RETRYABLE_WHATSAPP_STATUSES = (
    WhatsappProcessingStatus.RECEIVED,
    WhatsappProcessingStatus.VALIDATING,
    WhatsappProcessingStatus.AWAITING_MEDIA,
    WhatsappProcessingStatus.PROCESSING_FAILED,
)


class WhatsappMessageType(models.TextChoices):
    TEXT = "TEXT", "Text"
    IMAGE = "IMAGE", "Image"
    DOCUMENT = "DOCUMENT", "Document"
    AUDIO = "AUDIO", "Audio"
    VIDEO = "VIDEO", "Video"
    STICKER = "STICKER", "Sticker"
    LOCATION = "LOCATION", "Location"
    CONTACTS = "CONTACTS", "Contacts"
    INTERACTIVE = "INTERACTIVE", "Interactive Reply"
    BUTTON = "BUTTON", "Button Reply"
    REACTION = "REACTION", "Reaction"
    ORDER = "ORDER", "Order"
    SYSTEM = "SYSTEM", "System"
    UNSUPPORTED = "UNSUPPORTED", "Unsupported"


# Types that carry a media id needing a Graph API download.
MEDIA_MESSAGE_TYPES = frozenset({
    WhatsappMessageType.IMAGE,
    WhatsappMessageType.DOCUMENT,
    WhatsappMessageType.AUDIO,
    WhatsappMessageType.VIDEO,
    WhatsappMessageType.STICKER,
})

# Types that are stored for the record but never become a ticket.
#
# A thumbs-up reaction appending "(no text)" to a bug timeline is noise, and a
# sticker-only message is almost never a support request. The row is kept rather
# than dropped so "did they reply?" has a truthful answer.
DISCARD_TYPES = frozenset({
    WhatsappMessageType.REACTION,
    WhatsappMessageType.STICKER,
    WhatsappMessageType.ORDER,
    WhatsappMessageType.SYSTEM,
    WhatsappMessageType.UNSUPPORTED,
})


class MediaStatus(models.TextChoices):
    NOT_APPLICABLE = "NOT_APPLICABLE", "Not Applicable"
    PENDING = "PENDING", "Pending"
    FETCHING = "FETCHING", "Fetching"
    STORED = "STORED", "Stored"
    FAILED = "FAILED", "Failed"


class WhatsappErrorCode(models.TextChoices):
    # ---- PERMANENT: retrying cannot change the outcome ----
    NO_SENDER = "NO_SENDER", "No sender"
    SENDER_NOT_ALLOWED = "SENDER_NOT_ALLOWED", "Sender not in the allow-list"
    UNSUPPORTED_TYPE = "UNSUPPORTED_TYPE", "Unsupported message type"
    EMPTY_CONTENT = "EMPTY_CONTENT", "No usable content"
    DUPLICATE = "DUPLICATE", "Already processed"
    UNMAPPED_PHONE_NUMBER_ID = "UNMAPPED_PHONE_NUMBER_ID", "Unmapped business number"
    MESSAGE_TOO_LARGE = "MESSAGE_TOO_LARGE", "Message exceeds size limit"
    ATTACHMENT_REJECTED = "ATTACHMENT_REJECTED", "Attachment failed validation"
    PARSE_ERROR = "PARSE_ERROR", "Message could not be parsed"
    # The media id itself is gone (Graph 404). The URL expiring is temporary;
    # this is not.
    MEDIA_EXPIRED = "MEDIA_EXPIRED", "Media no longer available"

    # ---- TEMPORARY: the same message may succeed later ----
    GRAPH_API_ERROR = "GRAPH_API_ERROR", "Graph API problem"
    MEDIA_DOWNLOAD_ERROR = "MEDIA_DOWNLOAD_ERROR", "Media download problem"
    STORAGE_ERROR = "STORAGE_ERROR", "File storage problem"
    DB_ERROR = "DB_ERROR", "Database problem"
    UNEXPECTED_ERROR = "UNEXPECTED_ERROR", "Unexpected error"


# Splitting these as frozensets next to the enum is what makes "distinguish
# temporary from permanent" enforceable rather than aspirational.
PERMANENT_ERROR_CODES = frozenset({
    WhatsappErrorCode.NO_SENDER,
    WhatsappErrorCode.SENDER_NOT_ALLOWED,
    WhatsappErrorCode.UNSUPPORTED_TYPE,
    WhatsappErrorCode.EMPTY_CONTENT,
    WhatsappErrorCode.DUPLICATE,
    WhatsappErrorCode.UNMAPPED_PHONE_NUMBER_ID,
    WhatsappErrorCode.MESSAGE_TOO_LARGE,
    WhatsappErrorCode.ATTACHMENT_REJECTED,
    WhatsappErrorCode.PARSE_ERROR,
    WhatsappErrorCode.MEDIA_EXPIRED,
})

TEMPORARY_ERROR_CODES = frozenset({
    WhatsappErrorCode.GRAPH_API_ERROR,
    WhatsappErrorCode.MEDIA_DOWNLOAD_ERROR,
    WhatsappErrorCode.STORAGE_ERROR,
    WhatsappErrorCode.DB_ERROR,
    WhatsappErrorCode.UNEXPECTED_ERROR,
})

# Delivery receipts for messages WE sent. Phase 1 sends nothing, but Meta
# subscribes the "messages" field as a bundle and these arrive anyway. They must
# never become tickets.
STATUS_EVENT_KINDS = ("sent", "delivered", "read", "failed")

MAX_BODY_CHARS = 4096          # WhatsApp's own text limit
TITLE_MAX_CHARS = 120          # a preview, not the whole message
STRUCTURED_MAX_BYTES = 16_000
