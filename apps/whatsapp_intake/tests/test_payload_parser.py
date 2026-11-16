"""Parser tests.

The rule that matters most here: value.statuses[] must never yield a message.
Everything else is about coping with shapes Meta may send without warning.
"""

from datetime import timezone as dt_timezone

from django.test import SimpleTestCase

from apps.whatsapp_intake.constants import WhatsappMessageType
from apps.whatsapp_intake.services.payload_parser import parse_webhook_payload
from apps.whatsapp_intake.tests.factories import (
    make_message, make_payload, parse_one,
)


class StatusEventTests(SimpleTestCase):
    def test_status_only_payload_yields_no_messages(self):
        """Delivery receipts are not inbound requests and never become tickets."""
        payload = make_payload(statuses=[
            {"id": "wamid.OUT1", "status": "delivered", "timestamp": "1762500001",
             "recipient_id": "919876543210"},
            {"id": "wamid.OUT2", "status": "read", "timestamp": "1762500002",
             "recipient_id": "919876543210"},
        ])
        result = parse_webhook_payload(payload)
        self.assertEqual(result.messages, [])
        self.assertEqual(result.status_count, 2)

    def test_statuses_alongside_a_message_do_not_inflate_the_message_count(self):
        payload = make_payload(
            messages=[make_message()],
            statuses=[{"id": "wamid.OUT1", "status": "sent"}],
        )
        result = parse_webhook_payload(payload)
        self.assertEqual(len(result.messages), 1)
        self.assertEqual(result.status_count, 1)


class EnvelopeTests(SimpleTestCase):
    def test_non_message_field_is_skipped(self):
        """Meta pushes account and template updates down the same webhook."""
        result = parse_webhook_payload(
            make_payload(field="phone_number_quality_update")
        )
        self.assertEqual(result.messages, [])

    def test_non_whatsapp_product_is_skipped(self):
        result = parse_webhook_payload(make_payload(messaging_product="sms"))
        self.assertEqual(result.messages, [])

    def test_unknown_object_is_skipped(self):
        payload = make_payload()
        payload["object"] = "instagram"
        self.assertEqual(parse_webhook_payload(payload).messages, [])

    def test_message_without_an_id_is_skipped(self):
        bad = make_message()
        del bad["id"]
        self.assertEqual(parse_webhook_payload(make_payload(messages=[bad])).messages, [])

    def test_empty_and_malformed_payloads_do_not_raise(self):
        for payload in ({}, {"entry": None}, {"entry": [{"changes": "nope"}]},
                        {"entry": [{"changes": [{"field": "messages",
                                                 "value": {"messages": "nope"}}]}]}):
            self.assertEqual(parse_webhook_payload(payload).messages, [])


class MessageTypeTests(SimpleTestCase):
    def test_text(self):
        parsed = parse_one()
        self.assertEqual(parsed.message_type, WhatsappMessageType.TEXT)
        self.assertEqual(parsed.body_text, "Printer on 2nd floor is not working")
        self.assertEqual(parsed.wa_id, "919876543210")

    def test_sender_name_comes_from_the_sibling_contacts_array(self):
        """The name is not on the message object; it is matched by wa_id."""
        self.assertEqual(parse_one(name="Ravi Kumar").from_name, "Ravi Kumar")

    def test_missing_contacts_leaves_the_name_blank(self):
        self.assertEqual(parse_one(name=None).from_name, "")

    def test_image_with_caption_carries_media(self):
        parsed = parse_one(messages=[make_message(
            msg_type="image", extra={"image": {
                "id": "MEDIA1", "mime_type": "image/jpeg",
                "sha256": "abc", "caption": "Error screen",
            }},
        )])
        self.assertEqual(parsed.message_type, WhatsappMessageType.IMAGE)
        self.assertEqual(parsed.body_text, "Error screen")
        self.assertEqual(len(parsed.media), 1)
        self.assertEqual(parsed.media[0].media_id, "MEDIA1")

    def test_document_keeps_its_filename(self):
        parsed = parse_one(messages=[make_message(
            msg_type="document", extra={"document": {
                "id": "MEDIA2", "mime_type": "application/pdf",
                "filename": "invoice.pdf",
            }},
        )])
        self.assertEqual(parsed.structured_payload["filename"], "invoice.pdf")
        self.assertEqual(parsed.media[0].file_name, "invoice.pdf")

    def test_voice_note_is_flagged(self):
        parsed = parse_one(messages=[make_message(
            msg_type="audio",
            extra={"audio": {"id": "MEDIA3", "mime_type": "audio/ogg", "voice": True}},
        )])
        self.assertTrue(parsed.media[0].voice)
        self.assertTrue(parsed.structured_payload["voice"])

    def test_location_is_rendered_as_readable_text(self):
        parsed = parse_one(messages=[make_message(
            msg_type="location", extra={"location": {
                "latitude": 12.97, "longitude": 77.59,
                "name": "Head Office", "address": "MG Road",
            }},
        )])
        self.assertEqual(parsed.message_type, WhatsappMessageType.LOCATION)
        self.assertIn("Head Office", parsed.body_text)
        self.assertEqual(parsed.structured_payload["latitude"], 12.97)

    def test_contacts_render_the_first_three_then_a_count(self):
        contacts = [
            {"name": {"formatted_name": f"Person {i}"},
             "phones": [{"phone": f"+9190000000{i}"}]}
            for i in range(5)
        ]
        parsed = parse_one(messages=[make_message(
            msg_type="contacts", extra={"contacts": contacts},
        )])
        self.assertIn("Person 0", parsed.body_text)
        self.assertIn("+2 more", parsed.body_text)

    def test_interactive_reply_uses_the_chosen_title(self):
        parsed = parse_one(messages=[make_message(
            msg_type="interactive", extra={"interactive": {
                "type": "button_reply",
                "button_reply": {"id": "opt-1", "title": "Reset my password"},
            }},
        )])
        self.assertEqual(parsed.body_text, "Reset my password")

    def test_button_reply_uses_its_text(self):
        parsed = parse_one(messages=[make_message(
            msg_type="button",
            extra={"button": {"text": "Yes, still broken", "payload": "P1"}},
        )])
        self.assertEqual(parsed.body_text, "Yes, still broken")

    def test_reaction_is_recognised_rather_than_unsupported(self):
        parsed = parse_one(messages=[make_message(
            msg_type="reaction",
            extra={"reaction": {"emoji": "\U0001f44d", "message_id": "wamid.X"}},
        )])
        self.assertEqual(parsed.message_type, WhatsappMessageType.REACTION)

    def test_unknown_type_falls_back_to_unsupported(self):
        parsed = parse_one(messages=[make_message(msg_type="hologram")])
        self.assertEqual(parsed.message_type, WhatsappMessageType.UNSUPPORTED)


class ContextAndTimestampTests(SimpleTestCase):
    def test_reply_context_is_captured(self):
        parsed = parse_one(messages=[make_message(context_id="wamid.PARENT")])
        self.assertEqual(parsed.context_wamid, "wamid.PARENT")

    def test_forwarded_flag_is_captured(self):
        parsed = parse_one(messages=[make_message(
            extra={"context": {"forwarded": True}},
        )])
        self.assertTrue(parsed.is_forwarded)

    def test_timestamp_is_parsed_as_utc_not_local(self):
        """A naive parse would shift every message by the TIME_ZONE offset."""
        parsed = parse_one(messages=[make_message(timestamp="1762500000")])
        self.assertEqual(parsed.sent_at.tzinfo, dt_timezone.utc)
        self.assertEqual(int(parsed.sent_at.timestamp()), 1762500000)

    def test_unparseable_timestamp_does_not_raise(self):
        parsed = parse_one(messages=[make_message(timestamp="not-a-number")])
        self.assertIsNotNone(parsed.sent_at)
