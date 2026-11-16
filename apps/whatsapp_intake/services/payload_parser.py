"""Webhook JSON -> ParsedWhatsappMessage.

Defensive at every level. Meta adds fields and whole event kinds without
notice, so an unseen shape must be ignored rather than crash the run: every
lookup defaults, and every iteration tolerates a non-list.

The single most important rule here is that value.statuses[] -- delivery and
read receipts for messages WE sent -- must never become a message row, and
therefore never a ticket. Phase 1 sends nothing at all, but Meta subscribes the
"messages" field as a bundle and these arrive regardless.
"""

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from apps.whatsapp_intake.constants import (
    MAX_BODY_CHARS,
    STRUCTURED_MAX_BYTES,
    WhatsappMessageType,
)

logger = logging.getLogger(__name__)

# Meta "type" string -> our enum. Anything absent here is UNSUPPORTED.
_TYPE_MAP = {
    "text": WhatsappMessageType.TEXT,
    "image": WhatsappMessageType.IMAGE,
    "document": WhatsappMessageType.DOCUMENT,
    "audio": WhatsappMessageType.AUDIO,
    "video": WhatsappMessageType.VIDEO,
    "sticker": WhatsappMessageType.STICKER,
    "location": WhatsappMessageType.LOCATION,
    "contacts": WhatsappMessageType.CONTACTS,
    "interactive": WhatsappMessageType.INTERACTIVE,
    "button": WhatsappMessageType.BUTTON,
    "reaction": WhatsappMessageType.REACTION,
    "order": WhatsappMessageType.ORDER,
    "system": WhatsappMessageType.SYSTEM,
}

# Types whose payload carries a media id to download later.
_MEDIA_KEYS = {
    WhatsappMessageType.IMAGE: "image",
    WhatsappMessageType.DOCUMENT: "document",
    WhatsappMessageType.AUDIO: "audio",
    WhatsappMessageType.VIDEO: "video",
    WhatsappMessageType.STICKER: "sticker",
}


@dataclass
class ParsedMedia:
    media_id: str = ""
    mime_type: str = ""
    sha256: str = ""
    file_name: str = ""
    voice: bool = False


@dataclass
class ParsedWhatsappMessage:
    wamid: str = ""
    wa_id: str = ""
    from_name: str = ""
    phone_number_id: str = ""
    display_phone_number: str = ""
    waba_id: str = ""
    message_type: str = WhatsappMessageType.UNSUPPORTED
    body_text: str = ""
    structured_payload: dict = field(default_factory=dict)
    context_wamid: str = ""
    is_forwarded: bool = False
    sent_at: datetime = None
    media: list = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    @property
    def body_hash(self):
        return hashlib.sha256((self.body_text or "").encode("utf-8")).hexdigest()


@dataclass
class ParseResult:
    messages: list = field(default_factory=list)
    status_count: int = 0
    ignored: list = field(default_factory=list)


def _as_list(value):
    """Meta sends lists; a malformed payload might not. Never iterate a str."""
    if isinstance(value, list):
        return value
    return []


def _as_dict(value):
    return value if isinstance(value, dict) else {}


def parse_timestamp(raw):
    """Meta's timestamp is unix SECONDS as a string.

    Parsed as explicit UTC. A naive parse would be interpreted in TIME_ZONE
    (Asia/Kolkata here) and silently shift every message by 5.5 hours.
    """
    try:
        return datetime.fromtimestamp(int(raw), tz=timezone.utc)
    except (TypeError, ValueError):
        return datetime.now(tz=timezone.utc)


def _truncate_structured(payload):
    """Keep the evidence, but never let one message balloon a row."""
    if not payload:
        return {}
    import json

    try:
        encoded = json.dumps(payload)
    except (TypeError, ValueError):
        return {}
    if len(encoded) <= STRUCTURED_MAX_BYTES:
        return payload
    return {"_truncated": True, "_preview": encoded[:STRUCTURED_MAX_BYTES]}


def _render_location(node):
    name = (node.get("name") or "").strip()
    address = (node.get("address") or "").strip()
    lat, lng = node.get("latitude"), node.get("longitude")
    label = ", ".join(p for p in (name, address) if p)
    coords = f"({lat}, {lng})" if lat is not None and lng is not None else ""
    return " ".join(p for p in (f"Location: {label}" if label else "Location:", coords) if p)


def _render_contacts(nodes):
    """First three contacts, then a count. A 40-contact share is not a title."""
    rendered = []
    for node in nodes[:3]:
        name = _as_dict(node.get("name")).get("formatted_name") or "Unknown"
        phones = [
            p.get("phone", "") for p in _as_list(node.get("phones")) if p.get("phone")
        ]
        rendered.append(f"{name} {phones[0]}" if phones else name)
    suffix = f" +{len(nodes) - 3} more" if len(nodes) > 3 else ""
    return f"Shared contact: {'; '.join(rendered)}{suffix}" if rendered else "Shared contact"


def _parse_one_message(msg, *, contacts, metadata, waba_id):
    raw_type = (msg.get("type") or "").lower()
    message_type = _TYPE_MAP.get(raw_type, WhatsappMessageType.UNSUPPORTED)

    wa_id = str(msg.get("from") or "")
    context = _as_dict(msg.get("context"))

    parsed = ParsedWhatsappMessage(
        wamid=str(msg.get("id") or ""),
        wa_id=wa_id,
        # The sender's name is NOT on the message object -- it is in the
        # sibling contacts[] array, matched by wa_id.
        from_name=contacts.get(wa_id, ""),
        phone_number_id=str(metadata.get("phone_number_id") or ""),
        display_phone_number=str(metadata.get("display_phone_number") or ""),
        waba_id=str(waba_id or ""),
        message_type=message_type,
        context_wamid=str(context.get("id") or ""),
        is_forwarded=bool(context.get("forwarded")),
        sent_at=parse_timestamp(msg.get("timestamp")),
        raw=msg,
    )

    node = _as_dict(msg.get(raw_type))

    if message_type == WhatsappMessageType.TEXT:
        parsed.body_text = (node.get("body") or "")[:MAX_BODY_CHARS]

    elif message_type in _MEDIA_KEYS:
        parsed.body_text = (node.get("caption") or "")[:MAX_BODY_CHARS]
        parsed.media.append(ParsedMedia(
            media_id=str(node.get("id") or ""),
            mime_type=str(node.get("mime_type") or ""),
            sha256=str(node.get("sha256") or ""),
            file_name=str(node.get("filename") or ""),
            voice=bool(node.get("voice")),
        ))
        if message_type == WhatsappMessageType.DOCUMENT and node.get("filename"):
            parsed.structured_payload = {"filename": node.get("filename")}
        elif message_type == WhatsappMessageType.AUDIO:
            parsed.structured_payload = {"voice": bool(node.get("voice"))}

    elif message_type == WhatsappMessageType.LOCATION:
        parsed.body_text = _render_location(node)
        parsed.structured_payload = {
            "latitude": node.get("latitude"), "longitude": node.get("longitude"),
            "name": node.get("name", ""), "address": node.get("address", ""),
        }

    elif message_type == WhatsappMessageType.CONTACTS:
        nodes = _as_list(msg.get("contacts"))
        parsed.body_text = _render_contacts(nodes)
        parsed.structured_payload = {"contacts": nodes}

    elif message_type == WhatsappMessageType.INTERACTIVE:
        kind = node.get("type") or ""
        reply = _as_dict(node.get("list_reply")) or _as_dict(node.get("button_reply"))
        parsed.body_text = (reply.get("title") or "")[:MAX_BODY_CHARS]
        parsed.structured_payload = {
            "kind": kind, "id": reply.get("id", ""), "title": reply.get("title", ""),
            "description": reply.get("description", ""),
        }

    elif message_type == WhatsappMessageType.BUTTON:
        parsed.body_text = (node.get("text") or "")[:MAX_BODY_CHARS]
        parsed.structured_payload = {"payload": node.get("payload", "")}

    elif message_type == WhatsappMessageType.REACTION:
        parsed.structured_payload = {
            "emoji": node.get("emoji", ""), "message_id": node.get("message_id", ""),
        }

    else:
        # Unknown or non-request kinds (order, system). Keep the whole node as
        # evidence; the orchestrator rejects these without creating a ticket.
        parsed.structured_payload = {"raw_type": raw_type, "node": node}

    parsed.structured_payload = _truncate_structured(parsed.structured_payload)
    return parsed


def parse_webhook_payload(payload):
    """Walk the envelope and return every inbound message worth storing."""
    result = ParseResult()
    payload = _as_dict(payload)

    if payload.get("object") and payload.get("object") != "whatsapp_business_account":
        result.ignored.append(f"object={payload.get('object')}")
        return result

    for entry in _as_list(payload.get("entry")):
        entry = _as_dict(entry)
        waba_id = entry.get("id", "")

        for change in _as_list(entry.get("changes")):
            change = _as_dict(change)
            # Meta also pushes account_update, phone_number_quality_update and
            # template_status_update down this same webhook.
            if change.get("field") != "messages":
                result.ignored.append(f"field={change.get('field')}")
                continue

            value = _as_dict(change.get("value"))
            if value.get("messaging_product") not in (None, "", "whatsapp"):
                result.ignored.append("non-whatsapp product")
                continue

            # Delivery/read receipts. Counted, never turned into rows.
            statuses = _as_list(value.get("statuses"))
            if statuses:
                result.status_count += len(statuses)
                logger.debug("Ignoring %s WhatsApp status event(s)", len(statuses))

            for err in _as_list(value.get("errors")):
                logger.warning("WhatsApp webhook reported an error: %s", err)

            metadata = _as_dict(value.get("metadata"))
            contacts = {
                str(c.get("wa_id")): _as_dict(c.get("profile")).get("name", "")
                for c in _as_list(value.get("contacts"))
                if c and c.get("wa_id")
            }

            for msg in _as_list(value.get("messages")):
                msg = _as_dict(msg)
                if not msg.get("id"):
                    result.ignored.append("message without id")
                    continue
                try:
                    result.messages.append(_parse_one_message(
                        msg, contacts=contacts, metadata=metadata, waba_id=waba_id,
                    ))
                except Exception:
                    logger.exception("Failed to parse one WhatsApp message")
                    result.ignored.append("parse error")

    return result
