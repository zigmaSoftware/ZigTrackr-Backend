"""Test helpers for WhatsApp intake.

Plain functions, matching apps/mail_intake/tests/factories.py.

make_payload() builds the genuine Meta envelope shape rather than a convenient
flattening of it, because the parser's whole job is to cope with the real
nesting -- entry[].changes[].value.messages[], with the sender's name in a
sibling contacts[] array.
"""

import json

from apps.whatsapp_intake.services.payload_parser import parse_webhook_payload

DEFAULT_WA_ID = "919876543210"
DEFAULT_PHONE_NUMBER_ID = "PNID1"
DEFAULT_WABA_ID = "WABA1"


def make_message(
    *, wamid="wamid.TEST0001", wa_id=DEFAULT_WA_ID, msg_type="text",
    text="Printer on 2nd floor is not working", timestamp="1762500000",
    context_id=None, extra=None,
):
    """One entry in value.messages[]."""
    message = {"from": wa_id, "id": wamid, "timestamp": timestamp, "type": msg_type}
    if msg_type == "text":
        message["text"] = {"body": text}
    if context_id:
        message["context"] = {"id": context_id}
    if extra:
        message.update(extra)
    return message


def make_payload(
    *, messages=None, statuses=None, contacts=None,
    wa_id=DEFAULT_WA_ID, name="Ravi Kumar",
    phone_number_id=DEFAULT_PHONE_NUMBER_ID, waba_id=DEFAULT_WABA_ID,
    field="messages", messaging_product="whatsapp",
):
    """A full webhook envelope."""
    value = {
        "messaging_product": messaging_product,
        "metadata": {
            "display_phone_number": "918012345678",
            "phone_number_id": phone_number_id,
        },
    }
    if contacts is None and name is not None:
        contacts = [{"profile": {"name": name}, "wa_id": wa_id}]
    if contacts:
        value["contacts"] = contacts
    if messages is None and statuses is None:
        messages = [make_message(wa_id=wa_id)]
    if messages:
        value["messages"] = messages
    if statuses:
        value["statuses"] = statuses

    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": waba_id, "changes": [{"field": field, "value": value}]}],
    }


def make_payload_bytes(**kwargs):
    return json.dumps(make_payload(**kwargs)).encode()


def parse_one(**kwargs):
    """Parse a payload and return its single message, for parser tests."""
    result = parse_webhook_payload(make_payload(**kwargs))
    assert len(result.messages) == 1, f"expected 1 message, got {len(result.messages)}"
    return result.messages[0]


def make_company(code="ZIGMA", name="Zigma Global", whatsapp_number_id=""):
    """Get or create a company, since a seed migration already ships ZIGMA."""
    from apps.masters.models import CompanyMaster

    company, _ = CompanyMaster.objects.get_or_create(
        code=code, defaults={"name": name},
    )
    if company.support_whatsapp_number_id != whatsapp_number_id:
        company.support_whatsapp_number_id = whatsapp_number_id
        company.save(update_fields=["support_whatsapp_number_id"])
    return company


def register(parsed, *, company=None, event=None):
    """Persist a parsed message, as the orchestrator would."""
    from apps.whatsapp_intake.services.duplicate_service import register_message

    return register_message(parsed, company=company, event=event)
