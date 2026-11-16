"""The testing allow-list.

During the personal-number test phase, only a known sender should open tickets.
The failure that matters: a number written three different ways
(+91 6379088711, 916379088711, 6379088711) must all resolve to the same sender,
because that is how it will actually be configured versus how Meta delivers it.
"""

from django.test import TestCase, override_settings

from apps.tickets.models import SupportTicket
from apps.whatsapp_intake.constants import WhatsappErrorCode, WhatsappProcessingStatus
from apps.whatsapp_intake.models import WhatsappEvent, WhatsappMessage
from apps.whatsapp_intake.services.intake_orchestrator import run_whatsapp_intake
from apps.whatsapp_intake.services.payload_parser import parse_webhook_payload
from apps.whatsapp_intake.services.intake_orchestrator import (
    WhatsappRunResult, process_parsed_message,
)
from apps.whatsapp_intake.tests.factories import make_company, make_message, make_payload

ALLOWED = "916379088711"       # as WhatsApp delivers it: country code + number
LOCAL = "6379088711"           # as a human writes it


@override_settings(WHATSAPP_INTAKE_ENABLED=True)
class AllowedSenderTests(TestCase):
    def setUp(self):
        self.company = make_company(whatsapp_number_id="PNID1")

    def _ingest(self, wa_id, wamid="wamid.S1"):
        payload = make_payload(
            wa_id=wa_id,
            messages=[make_message(wamid=wamid, wa_id=wa_id, text="Test message")],
        )
        parsed = parse_webhook_payload(payload)
        result = WhatsappRunResult()
        message = process_parsed_message(parsed.messages[0], result=result)
        message.refresh_from_db()
        return message, result

    @override_settings(WHATSAPP_ALLOWED_SENDERS=[ALLOWED])
    def test_allowed_sender_creates_a_ticket(self):
        message, result = self._ingest(ALLOWED)
        self.assertEqual(result.tickets_created, 1)
        self.assertIsNotNone(message.linked_ticket_id)

    @override_settings(WHATSAPP_ALLOWED_SENDERS=[ALLOWED])
    def test_other_sender_is_stored_but_opens_no_ticket(self):
        message, result = self._ingest("919999999999", wamid="wamid.OTHER")
        self.assertEqual(result.tickets_created, 0)
        self.assertEqual(result.rejected, 1)
        # Stored, not lost -- visible in the Inbox as filtered.
        self.assertEqual(message.processing_status,
                         WhatsappProcessingStatus.REJECTED)
        self.assertEqual(message.last_error_code,
                         WhatsappErrorCode.SENDER_NOT_ALLOWED)
        self.assertIsNone(message.linked_ticket_id)
        self.assertEqual(SupportTicket.objects.count(), 0)

    @override_settings(WHATSAPP_ALLOWED_SENDERS=[LOCAL])
    def test_local_number_in_config_matches_country_prefixed_wa_id(self):
        """6379088711 configured, 916379088711 delivered -> same sender."""
        message, result = self._ingest(ALLOWED)
        self.assertEqual(result.tickets_created, 1)

    @override_settings(WHATSAPP_ALLOWED_SENDERS=[ALLOWED])
    def test_full_number_in_config_matches_the_delivered_wa_id(self):
        message, result = self._ingest(ALLOWED)
        self.assertEqual(result.tickets_created, 1)

    @override_settings(WHATSAPP_ALLOWED_SENDERS=[])
    def test_empty_allow_list_permits_everyone(self):
        """Production behaviour: the gate is a no-op when unset."""
        message, result = self._ingest("919999999999")
        self.assertEqual(result.tickets_created, 1)

    @override_settings(WHATSAPP_ALLOWED_SENDERS=[ALLOWED])
    def test_a_full_different_number_sharing_a_suffix_is_not_matched(self):
        """An exact 12-digit config entry must not match a different number."""
        # ALLOWED is 916379088711; a number ending 6379088711 but with a
        # different country code must still be rejected.
        message, result = self._ingest("926379088711", wamid="wamid.DIFF")
        self.assertEqual(result.tickets_created, 0)
        self.assertEqual(message.last_error_code,
                         WhatsappErrorCode.SENDER_NOT_ALLOWED)
