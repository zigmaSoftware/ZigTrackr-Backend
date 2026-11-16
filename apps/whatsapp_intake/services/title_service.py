"""Deriving a ticket title and description from a message with no subject.

Mail has a subject line to fall back on; WhatsApp has nothing, so the title is
built from the body. It matters more than it looks: the unassigned queue is a
list of titles, and a queue of "WhatsApp message" rows is unscannable.
"""

import re
import unicodedata

from apps.whatsapp_intake.constants import TITLE_MAX_CHARS, WhatsappMessageType

# Zero-width and bidi-control characters. Stripped because the text is
# user-supplied and an RTL override in a ticket title is a real spoofing
# vector -- it can make a title render as something other than what it says.
_INVISIBLE_RE = re.compile(
    r"[​-‏‪-‮⁦-⁩﻿]"
)
_WHITESPACE_RE = re.compile(r"\s+")

MEDIA_TITLE_TEMPLATES = {
    WhatsappMessageType.IMAGE: "WhatsApp image from {sender}",
    WhatsappMessageType.VIDEO: "WhatsApp video from {sender}",
    WhatsappMessageType.AUDIO: "WhatsApp voice note from {sender}",
    WhatsappMessageType.DOCUMENT: "WhatsApp document from {sender}",
    WhatsappMessageType.LOCATION: "WhatsApp location from {sender}",
    WhatsappMessageType.CONTACTS: "WhatsApp contact shared by {sender}",
    WhatsappMessageType.STICKER: "WhatsApp sticker from {sender}",
}

DEFAULT_TITLE = "WhatsApp message from {sender}"


def clean_text(value):
    """Strip invisibles and collapse whitespace, preserving emoji.

    The database connection is utf8mb4, so 4-byte characters are safe to keep;
    normalising them away would lose meaning the sender intended.
    """
    if not value:
        return ""
    text = unicodedata.normalize("NFC", str(value))
    text = _INVISIBLE_RE.sub("", text)
    return _WHITESPACE_RE.sub(" ", text).strip()


def mask_number(wa_id):
    """Last four digits only.

    A full phone number in a ticket title reaches notification emails, exports
    and anything else that renders a title, so it never goes there.
    """
    digits = re.sub(r"\D", "", str(wa_id or ""))
    return f"...{digits[-4:]}" if len(digits) >= 4 else (digits or "unknown")


def sender_label(message):
    """The name if WhatsApp gave us one, otherwise a masked number."""
    name = clean_text(getattr(message, "from_name", ""))
    return name or mask_number(getattr(message, "wa_id", ""))


def _first_line(text):
    """The first non-empty line.

    A line, not the first N characters: people press enter mid-thought, and
    "Printer not working" is a better title than
    "Printer not working It's the one in".
    """
    for line in (text or "").splitlines():
        cleaned = clean_text(line)
        if cleaned:
            return cleaned
    return ""


def _truncate(text, limit=TITLE_MAX_CHARS):
    if len(text) <= limit:
        return text
    # Break on a word boundary when one is reasonably close to the limit.
    cut = text[:limit]
    space = cut.rfind(" ")
    if space > limit * 0.6:
        cut = cut[:space]
    return cut.rstrip() + "..."


def build_title(message):
    """A scannable one-line summary of this message."""
    sender = sender_label(message)

    line = _first_line(getattr(message, "body_text", ""))
    if line:
        return _truncate(line)

    template = MEDIA_TITLE_TEMPLATES.get(
        getattr(message, "message_type", ""), DEFAULT_TITLE,
    )
    filename = clean_text(
        (getattr(message, "structured_payload", None) or {}).get("filename", "")
    )
    if filename and message.message_type == WhatsappMessageType.DOCUMENT:
        return _truncate(f"WhatsApp document: {filename}")
    return _truncate(template.format(sender=sender))


def build_description(message):
    """The full message, plus whatever structure the body text cannot carry.

    A reviewer must be able to answer "what did they actually send?" from the
    ticket alone, without opening the raw payload.
    """
    sender = clean_text(getattr(message, "from_name", "")) or "Unknown sender"
    wa_id = getattr(message, "wa_id", "")
    parts = [f"Received via WhatsApp from {sender} ({wa_id})."]

    body = clean_text(getattr(message, "body_text", ""))
    if body:
        parts.append("")
        parts.append(body)

    payload = getattr(message, "structured_payload", None) or {}
    message_type = getattr(message, "message_type", "")

    if message_type == WhatsappMessageType.LOCATION and payload:
        parts += ["", "Location details:",
                  f"  Latitude: {payload.get('latitude')}",
                  f"  Longitude: {payload.get('longitude')}"]
        if payload.get("name"):
            parts.append(f"  Name: {clean_text(payload['name'])}")
        if payload.get("address"):
            parts.append(f"  Address: {clean_text(payload['address'])}")

    elif message_type == WhatsappMessageType.CONTACTS and payload.get("contacts"):
        parts += ["", "Shared contacts:"]
        for contact in payload["contacts"][:10]:
            name = (contact.get("name") or {}).get("formatted_name", "Unknown")
            phones = ", ".join(
                p.get("phone", "") for p in (contact.get("phones") or [])
            )
            parts.append(f"  {clean_text(name)}: {phones}" if phones
                         else f"  {clean_text(name)}")

    elif message_type == WhatsappMessageType.INTERACTIVE and payload:
        parts += ["", f"Selected option: {clean_text(payload.get('title', ''))}"]
        if payload.get("description"):
            parts.append(f"  {clean_text(payload['description'])}")

    if getattr(message, "is_forwarded", False):
        parts += ["", "(This message was forwarded.)"]

    return "\n".join(parts).strip()
