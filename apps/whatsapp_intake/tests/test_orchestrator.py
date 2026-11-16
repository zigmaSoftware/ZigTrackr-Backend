"""End-to-end pipeline tests: raw payload in, ticket in the unassigned queue out."""

from django.test import TestCase, override_settings

from apps.tickets.constants import (
    ClassificationStatus, TicketSource, TicketStatus, TicketType,
)
from apps.tickets.models import SupportTicket
from apps.whatsapp_intake.constants import (
    EventStatus, WhatsappErrorCode, WhatsappProcessingStatus,
)
from apps.whatsapp_intake.models import WhatsappEvent, WhatsappMessage
from apps.whatsapp_intake.services.intake_orchestrator import (
    WhatsappRunResult, process_parsed_message, run_whatsapp_intake,
)
from apps.whatsapp_intake.services.payload_parser import parse_webhook_payload
from apps.whatsapp_intake.tests.factories import (
    make_company, make_message, make_payload,
)


@override_settings(WHATSAPP_INTAKE_ENABLED=True)
class PipelineTests(TestCase):
    def setUp(self):
        self.company = make_company(whatsapp_number_id="PNID1")

    def _run(self, payload):
        WhatsappEvent.objects.create(
            event_id=f"evt{WhatsappEvent.objects.count()}",
            payload=payload, processing_status=EventStatus.PENDING,
        )
        return run_whatsapp_intake()

    def test_a_text_message_becomes_an_unassigned_ticket(self):
        result = self._run(make_payload())

        self.assertEqual(result.tickets_created, 1)
        ticket = SupportTicket.objects.get()
        self.assertEqual(ticket.source, TicketSource.WHATSAPP)
        self.assertEqual(ticket.ticket_type, TicketType.UNKNOWN)
        self.assertTrue(ticket.needs_review)
        self.assertEqual(ticket.classification_status,
                         ClassificationStatus.NEEDS_REVIEW)
        self.assertEqual(ticket.status, TicketStatus.NEEDS_REVIEW)
        self.assertIsNone(ticket.owner_id)
        self.assertIsNone(ticket.ticket_no)
        self.assertTrue(ticket.ref_no.startswith("REF-"))

    def test_the_ticket_lands_in_the_shared_unassigned_queue(self):
        """The queue is a filter on owner, not a table, so this is the test."""
        self._run(make_payload())
        self.assertTrue(
            SupportTicket.objects
            .filter(owner__isnull=True, is_deleted=False,
                    source=TicketSource.WHATSAPP)
            .exists()
        )

    def test_the_title_is_the_message_text(self):
        self._run(make_payload())
        self.assertEqual(
            SupportTicket.objects.get().title,
            "Printer on 2nd floor is not working",
        )

    def test_the_reporter_is_the_system_user_with_the_sender_alongside(self):
        self._run(make_payload())
        ticket = SupportTicket.objects.get()
        self.assertEqual(ticket.reported_by.username, "whatsapp.intake")
        self.assertEqual(ticket.reported_by_email, "919876543210")
        self.assertEqual(ticket.reported_by_name, "Ravi Kumar")

    def test_history_records_the_transition(self):
        self._run(make_payload())
        message = WhatsappMessage.objects.get()
        transitions = list(
            message.processing_history.values_list("to_status", flat=True)
        )
        self.assertIn(WhatsappProcessingStatus.RECEIVED, transitions)
        self.assertIn(WhatsappProcessingStatus.TICKET_CREATED, transitions)

    def test_an_audit_row_is_written(self):
        from apps.audit.models import AuditAction, AuditLog

        self._run(make_payload())
        self.assertTrue(
            AuditLog.objects.filter(
                action=AuditAction.TICKET_CREATED_FROM_WHATSAPP
            ).exists()
        )

    def test_the_event_is_marked_parsed(self):
        self._run(make_payload())
        self.assertEqual(
            WhatsappEvent.objects.get().processing_status, EventStatus.PARSED,
        )

    # ---- what must NOT become a ticket ----

    def test_a_status_only_delivery_creates_nothing(self):
        result = self._run(make_payload(statuses=[
            {"id": "wamid.OUT1", "status": "delivered"},
        ]))
        self.assertEqual(result.tickets_created, 0)
        self.assertEqual(WhatsappMessage.objects.count(), 0)
        self.assertEqual(SupportTicket.objects.count(), 0)
        self.assertEqual(result.statuses_ignored, 1)
        self.assertEqual(
            WhatsappEvent.objects.get().processing_status, EventStatus.IGNORED,
        )

    def test_a_reaction_is_stored_but_opens_no_ticket(self):
        result = self._run(make_payload(messages=[make_message(
            msg_type="reaction",
            extra={"reaction": {"emoji": "\U0001f44d", "message_id": "wamid.X"}},
        )]))
        self.assertEqual(result.tickets_created, 0)
        self.assertEqual(result.rejected, 1)
        message = WhatsappMessage.objects.get()
        self.assertEqual(message.processing_status,
                         WhatsappProcessingStatus.REJECTED)
        self.assertEqual(message.last_error_code,
                         WhatsappErrorCode.UNSUPPORTED_TYPE)
        self.assertEqual(SupportTicket.objects.count(), 0)

    def test_a_sticker_opens_no_ticket(self):
        result = self._run(make_payload(messages=[make_message(
            msg_type="sticker",
            extra={"sticker": {"id": "M1", "mime_type": "image/webp"}},
        )]))
        self.assertEqual(result.tickets_created, 0)
        self.assertEqual(SupportTicket.objects.count(), 0)

    def test_an_unmapped_business_number_is_not_guessed_into_a_company(self):
        result = self._run(make_payload(phone_number_id="UNKNOWN-PNID"))
        self.assertEqual(result.tickets_created, 0)
        message = WhatsappMessage.objects.get()
        self.assertIsNone(message.company_id)
        self.assertEqual(message.last_error_code,
                         WhatsappErrorCode.UNMAPPED_PHONE_NUMBER_ID)

    def test_an_empty_text_message_is_rejected(self):
        result = self._run(make_payload(messages=[make_message(text="")]))
        self.assertEqual(result.tickets_created, 0)
        self.assertEqual(
            WhatsappMessage.objects.get().last_error_code,
            WhatsappErrorCode.EMPTY_CONTENT,
        )

    # ---- idempotence ----

    def test_reprocessing_the_same_event_creates_no_second_ticket(self):
        payload = make_payload()
        self._run(payload)
        # A second delivery of the same message, in a different envelope.
        WhatsappEvent.objects.create(
            event_id="evt-again", payload=payload,
            processing_status=EventStatus.PENDING,
        )
        result = run_whatsapp_intake()
        self.assertEqual(result.tickets_created, 0)
        self.assertEqual(result.duplicates, 1)
        self.assertEqual(SupportTicket.objects.count(), 1)
        self.assertEqual(WhatsappMessage.objects.count(), 1)

    def test_running_twice_over_one_event_is_safe(self):
        self._run(make_payload())
        run_whatsapp_intake()
        self.assertEqual(SupportTicket.objects.count(), 1)


@override_settings(WHATSAPP_INTAKE_ENABLED=True)
class CompanyResolutionTests(TestCase):
    def test_the_fallback_applies_only_when_nothing_is_mapped(self):
        """A half-configured map must not silently misroute."""
        from apps.whatsapp_intake.services.company_resolver import resolve_company

        make_company(code="ZIGMA", whatsapp_number_id="")
        # Nothing mapped at all -> the single-tenant fallback.
        self.assertIsNotNone(resolve_company("ANY"))

        # Once one number is mapped, an unknown one resolves to nothing.
        make_company(code="ZIGMA", whatsapp_number_id="PNID1")
        self.assertIsNone(resolve_company("OTHER"))
        self.assertIsNotNone(resolve_company("PNID1"))


@override_settings(WHATSAPP_INTAKE_ENABLED=True)
class DedupeTests(TestCase):
    def setUp(self):
        self.company = make_company(whatsapp_number_id="PNID1")

    def _parse(self, **kwargs):
        return parse_webhook_payload(make_payload(**kwargs)).messages[0]

    def test_the_same_wamid_registers_once(self):
        parsed = self._parse()
        result = WhatsappRunResult()
        process_parsed_message(parsed, result=result)
        process_parsed_message(self._parse(), result=result)
        self.assertEqual(WhatsappMessage.objects.count(), 1)
        self.assertEqual(result.duplicates, 1)

    def test_the_same_wamid_under_another_company_is_a_separate_row(self):
        """The key is company-scoped, so remapping a number cannot suppress intake."""
        from apps.whatsapp_intake.services.duplicate_service import register_message

        other = make_company(code="ACME", name="Acme", whatsapp_number_id="PNID2")
        parsed = self._parse()
        register_message(parsed, company=self.company)
        register_message(parsed, company=other)
        self.assertEqual(WhatsappMessage.objects.count(), 2)
