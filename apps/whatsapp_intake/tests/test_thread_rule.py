"""The grouping rule, which is the behaviour most likely to be subtly wrong.

One ticket per message, EXCEPT when the sender already has an open WhatsApp
ticket -- then the message appends to it. Every boundary of that rule is
asserted here, because the failure mode (a second ticket per reply, or worse,
two senders' messages merged) is invisible until it happens with real traffic.
"""

from django.test import TestCase, override_settings

from apps.tickets.constants import TicketSource, TicketStatus
from apps.tickets.models import SupportTicket
from apps.whatsapp_intake.constants import WhatsappProcessingStatus
from apps.whatsapp_intake.services.intake_orchestrator import (
    WhatsappRunResult, process_parsed_message,
)
from apps.whatsapp_intake.services.payload_parser import parse_webhook_payload
from apps.whatsapp_intake.services.thread_service import find_open_whatsapp_ticket
from apps.whatsapp_intake.tests.factories import (
    make_company, make_message, make_payload,
)

SENDER = "919876543210"
OTHER_SENDER = "919000000001"


@override_settings(WHATSAPP_INTAKE_ENABLED=True)
class ThreadRuleTests(TestCase):
    def setUp(self):
        self.company = make_company(whatsapp_number_id="PNID1")

    def _ingest(self, *, wamid, text="Hello", wa_id=SENDER, context_id=None,
                phone_number_id="PNID1", msg_type="text", extra=None):
        payload = make_payload(
            wa_id=wa_id, phone_number_id=phone_number_id,
            messages=[make_message(
                wamid=wamid, wa_id=wa_id, text=text, msg_type=msg_type,
                context_id=context_id, extra=extra,
            )],
        )
        parsed = parse_webhook_payload(payload)
        result = WhatsappRunResult()
        message = process_parsed_message(parsed.messages[0], result=result)
        message.refresh_from_db()
        return message, result

    # ---- creating ----

    def test_first_message_from_a_new_sender_creates_a_ticket(self):
        message, result = self._ingest(wamid="wamid.A1")
        self.assertEqual(result.tickets_created, 1)
        self.assertEqual(message.processing_status,
                         WhatsappProcessingStatus.TICKET_CREATED)
        ticket = message.linked_ticket
        self.assertIsNotNone(ticket)
        self.assertEqual(ticket.source, TicketSource.WHATSAPP)
        self.assertTrue(ticket.needs_review)
        # Unassigned by construction: this is what puts it in the shared queue.
        self.assertIsNone(ticket.owner_id)
        # A reference, not yet a number -- review_ticket() mints that.
        self.assertTrue(ticket.ref_no)
        self.assertIsNone(ticket.ticket_no)

    def test_the_phone_number_is_the_sender_key(self):
        message, _ = self._ingest(wamid="wamid.A1")
        self.assertEqual(message.linked_ticket.reported_by_email, SENDER)

    # ---- appending ----

    def test_second_message_appends_instead_of_creating(self):
        first, _ = self._ingest(wamid="wamid.A1", text="Printer is broken")
        second, result = self._ingest(wamid="wamid.A2", text="Still broken")

        self.assertEqual(result.thread_updates, 1)
        self.assertEqual(result.tickets_created, 0)
        self.assertEqual(second.processing_status,
                         WhatsappProcessingStatus.THREAD_UPDATE)
        self.assertTrue(second.is_thread_reply)
        self.assertEqual(second.linked_ticket_id, first.linked_ticket_id)
        self.assertEqual(SupportTicket.objects.count(), 1)

    def test_the_appended_text_reaches_the_ticket_timeline(self):
        first, _ = self._ingest(wamid="wamid.A1")
        self._ingest(wamid="wamid.A2", text="Any update on this?")
        ticket = first.linked_ticket
        texts = " ".join(ticket.updates.values_list("update_text", flat=True))
        self.assertIn("Any update on this?", texts)

    def test_many_replies_still_produce_one_ticket(self):
        self._ingest(wamid="wamid.A1")
        for i in range(2, 6):
            self._ingest(wamid=f"wamid.A{i}", text=f"ping {i}")
        self.assertEqual(SupportTicket.objects.count(), 1)

    # ---- the boundaries ----

    def test_a_closed_ticket_does_not_absorb_the_next_message(self):
        first, _ = self._ingest(wamid="wamid.A1")
        ticket = first.linked_ticket
        ticket.status = TicketStatus.CLOSED
        ticket.save(update_fields=["status"])

        second, result = self._ingest(wamid="wamid.A2", text="New problem")
        self.assertEqual(result.tickets_created, 1)
        self.assertNotEqual(second.linked_ticket_id, ticket.pk)
        self.assertEqual(SupportTicket.objects.count(), 2)

    def test_a_rejected_ticket_does_not_absorb_the_next_message(self):
        first, _ = self._ingest(wamid="wamid.A1")
        ticket = first.linked_ticket
        ticket.status = TicketStatus.REJECTED
        ticket.save(update_fields=["status"])

        _, result = self._ingest(wamid="wamid.A2")
        self.assertEqual(result.tickets_created, 1)

    def test_a_completed_but_unclosed_ticket_still_absorbs_a_reply(self):
        """COMPLETED is not terminal: this is how a requester reopens."""
        first, _ = self._ingest(wamid="wamid.A1")
        ticket = first.linked_ticket
        ticket.status = TicketStatus.COMPLETED
        ticket.save(update_fields=["status"])

        _, result = self._ingest(wamid="wamid.A2")
        self.assertEqual(result.thread_updates, 1)
        self.assertEqual(SupportTicket.objects.count(), 1)

    def test_a_different_sender_gets_their_own_ticket(self):
        self._ingest(wamid="wamid.A1")
        second, result = self._ingest(wamid="wamid.B1", wa_id=OTHER_SENDER)
        self.assertEqual(result.tickets_created, 1)
        self.assertEqual(SupportTicket.objects.count(), 2)
        self.assertEqual(second.linked_ticket.reported_by_email, OTHER_SENDER)

    def test_an_open_email_ticket_does_not_absorb_a_whatsapp_message(self):
        """source is part of the match; channels must not cross-contaminate."""
        from apps.accounts.services.system_user import get_mail_intake_user
        from apps.tickets.services.ticket_number import generate_ref_no
        from common.utils.dates import local_today

        SupportTicket.objects.create(
            company=self.company,
            ref_no=generate_ref_no(local_today()),
            source=TicketSource.EMAIL,
            reported_by=get_mail_intake_user(),
            reported_by_email=SENDER,
            title="Emailed in",
            description="x",
            status=TicketStatus.NEEDS_REVIEW,
        )
        _, result = self._ingest(wamid="wamid.A1")
        self.assertEqual(result.tickets_created, 1)
        self.assertEqual(
            SupportTicket.objects.filter(source=TicketSource.WHATSAPP).count(), 1,
        )

    def test_a_soft_deleted_ticket_does_not_absorb_a_reply(self):
        first, _ = self._ingest(wamid="wamid.A1")
        ticket = first.linked_ticket
        ticket.is_deleted = True
        ticket.save(update_fields=["is_deleted"])

        _, result = self._ingest(wamid="wamid.A2")
        self.assertEqual(result.tickets_created, 1)

    def test_newest_open_ticket_wins_when_there_are_several(self):
        first, _ = self._ingest(wamid="wamid.A1")
        older = first.linked_ticket
        duplicate = SupportTicket.objects.create(
            company=self.company,
            ref_no="REF-9999-9999",
            source=TicketSource.WHATSAPP,
            reported_by=older.reported_by,
            reported_by_email=SENDER,
            title="Raised by hand",
            description="x",
            status=TicketStatus.NEEDS_REVIEW,
        )
        second, _ = self._ingest(wamid="wamid.A2")
        self.assertEqual(second.linked_ticket_id, duplicate.pk)

    # ---- explicit reply context ----

    def test_swipe_to_reply_links_to_the_quoted_message_ticket(self):
        first, _ = self._ingest(wamid="wamid.A1")
        second, result = self._ingest(
            wamid="wamid.A2", wa_id=OTHER_SENDER, context_id="wamid.A1",
        )
        # A different sender, but they pointed at the conversation themselves.
        self.assertEqual(result.thread_updates, 1)
        self.assertEqual(second.linked_ticket_id, first.linked_ticket_id)

    def test_context_pointing_at_an_unknown_message_falls_back(self):
        _, result = self._ingest(wamid="wamid.A1", context_id="wamid.NOPE")
        self.assertEqual(result.tickets_created, 1)


@override_settings(WHATSAPP_INTAKE_ENABLED=True)
class ThreadLookupGuardTests(TestCase):
    def test_a_message_without_a_company_matches_nothing(self):
        from apps.whatsapp_intake.models import WhatsappMessage
        from django.utils import timezone

        message = WhatsappMessage(
            company=None, wa_id=SENDER, wamid="wamid.X", dedupe_key="k",
            sent_at=timezone.now(), received_at=timezone.now(),
        )
        self.assertFalse(find_open_whatsapp_ticket(message).matched)
