"""Temporary versus permanent failure.

The distinction decides whether a message is retried or parked, so it is made
in one place rather than at each call site.
"""

import socket
import ssl

from django.db import DatabaseError, InterfaceError, OperationalError

from apps.whatsapp_intake.constants import (
    PERMANENT_ERROR_CODES,
    WhatsappErrorCode,
)


def classify_error(exc):
    """Return (error_code, is_temporary) for an exception raised while processing.

    IntegrityError is deliberately absent: a unique-key clash is the "another
    worker won the race" path handled in duplicate_service, not a failure.
    """
    if isinstance(exc, (socket.timeout, ssl.SSLError, ConnectionError, TimeoutError)):
        return WhatsappErrorCode.GRAPH_API_ERROR, True

    if isinstance(exc, (OperationalError, InterfaceError, DatabaseError)):
        return WhatsappErrorCode.DB_ERROR, True

    if isinstance(exc, OSError):
        # Storage failures; network errors are caught above.
        return WhatsappErrorCode.STORAGE_ERROR, True

    if isinstance(exc, (UnicodeError, ValueError, TypeError, KeyError, IndexError)):
        return WhatsappErrorCode.PARSE_ERROR, False

    return WhatsappErrorCode.UNEXPECTED_ERROR, True


def is_permanent(error_code):
    return error_code in PERMANENT_ERROR_CODES


def should_retry(*, error_code, attempts, max_attempts):
    """A message is retried only while it can still succeed."""
    if is_permanent(error_code):
        return False
    return (attempts or 0) < max_attempts
