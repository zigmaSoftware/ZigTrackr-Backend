"""Webhook authenticity: the entire security boundary of the public endpoint.

The webhook is unauthenticated by necessity -- Meta cannot present a JWT cookie
-- so these two checks are the only thing standing between the open internet
and the intake pipeline. Both use hmac.compare_digest rather than ==, because
both compare a secret and == leaks length and content through timing.
"""

import hashlib
import hmac
import logging

logger = logging.getLogger(__name__)

SIGNATURE_PREFIX = "sha256="


def compute_event_id(raw_body):
    """Stable id for one webhook delivery: sha256 of the exact bytes received.

    Meta re-POSTs the BYTE-IDENTICAL body when a delivery is not acknowledged,
    so hashing the bytes gives a redelivery guard that needs no parsing.
    """
    return hashlib.sha256(raw_body).hexdigest()


def verify_signature(*, raw_body, header, app_secret):
    """Constant-time HMAC-SHA256 check of Meta's X-Hub-Signature-256.

    raw_body MUST be request.body -- the exact bytes Meta sent. Re-serialising
    request.data changes key order and whitespace, and the digest will then
    never match. That is the single most common way this check is broken.

    Returns False rather than raising on every failure mode, including a
    missing secret: a misconfigured deployment must reject traffic, never
    fail open and accept it.
    """
    if not app_secret:
        logger.error(
            "WHATSAPP_APP_SECRET is not configured; rejecting webhook delivery"
        )
        return False
    if not raw_body:
        return False
    if not header or not header.startswith(SIGNATURE_PREFIX):
        return False

    received = header[len(SIGNATURE_PREFIX):].strip()
    if not received:
        return False

    expected = hmac.new(
        app_secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, received)


def verify_token(*, received, expected):
    """Constant-time check of Meta's one-time subscription verify token."""
    if not expected or not received:
        return False
    return hmac.compare_digest(str(received), str(expected))
