"""The webhook is the security boundary; these tests are what hold it.

Every assertion here is about a status code or a row count, because those are
the only two things Meta reacts to and the only two things an attacker can
observe.
"""

import hashlib
import hmac
import json

from django.test import Client, TestCase, override_settings

from apps.whatsapp_intake.models import WhatsappEvent

SECRET = "test-app-secret"
TOKEN = "test-verify-token"


def _body(**overrides):
    payload = {
        "object": "whatsapp_business_account",
        "entry": [{"id": "WABA1", "changes": [{"field": "messages", "value": {
            "messaging_product": "whatsapp",
            "metadata": {"display_phone_number": "918012345678",
                         "phone_number_id": "PNID1"},
            "contacts": [{"profile": {"name": "Ravi Kumar"},
                          "wa_id": "919876543210"}],
            "messages": [{"from": "919876543210", "id": "wamid.TEST0001",
                          "timestamp": "1762500000", "type": "text",
                          "text": {"body": "Printer on 2nd floor is not working"}}],
        }}]}],
    }
    payload.update(overrides)
    return json.dumps(payload).encode()


STATUS_BODY = json.dumps({
    "object": "whatsapp_business_account",
    "entry": [{"id": "WABA1", "changes": [{"field": "messages", "value": {
        "messaging_product": "whatsapp",
        "metadata": {"display_phone_number": "918012345678",
                     "phone_number_id": "PNID1"},
        "statuses": [{"id": "wamid.OUT1", "status": "delivered",
                      "timestamp": "1762500001", "recipient_id": "919876543210"}],
    }}]}],
}).encode()


def sign(body, secret=SECRET):
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@override_settings(
    WHATSAPP_INTAKE_ENABLED=True,
    WHATSAPP_APP_SECRET=SECRET,
    WHATSAPP_VERIFY_TOKEN=TOKEN,
)
class WhatsappWebhookTests(TestCase):
    url = "/webhooks/whatsapp/"

    def setUp(self):
        self.client = Client()

    # ---- GET: Meta's subscription handshake ----

    def test_valid_token_echoes_challenge_as_plain_text(self):
        response = self.client.get(self.url, {
            "hub.mode": "subscribe", "hub.verify_token": TOKEN,
            "hub.challenge": "test123",
        })
        self.assertEqual(response.status_code, 200)
        # Byte-for-byte: Meta compares exactly, so an envelope or a trailing
        # newline fails the handshake.
        self.assertEqual(response.content, b"test123")
        self.assertTrue(response["Content-Type"].startswith("text/plain"))

    def test_wrong_verify_token_is_refused(self):
        response = self.client.get(self.url, {
            "hub.mode": "subscribe", "hub.verify_token": "wrong",
            "hub.challenge": "test123",
        })
        self.assertEqual(response.status_code, 403)

    # ---- POST: signature ----

    def test_valid_signature_stores_one_event(self):
        body = _body()
        response = self.client.post(
            self.url, data=body, content_type="application/json",
            HTTP_X_HUB_SIGNATURE_256=sign(body),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(WhatsappEvent.objects.count(), 1)
        event = WhatsappEvent.objects.get()
        self.assertEqual(event.message_count, 1)
        self.assertEqual(event.status_count, 0)

    def test_redelivery_of_identical_body_stores_no_second_row(self):
        body = _body()
        for _ in range(3):
            response = self.client.post(
                self.url, data=body, content_type="application/json",
                HTTP_X_HUB_SIGNATURE_256=sign(body),
            )
            self.assertEqual(response.status_code, 200)
        self.assertEqual(WhatsappEvent.objects.count(), 1)

    def test_bad_signature_is_refused_and_stores_nothing(self):
        body = _body()
        response = self.client.post(
            self.url, data=body, content_type="application/json",
            HTTP_X_HUB_SIGNATURE_256="sha256=deadbeef",
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(WhatsappEvent.objects.count(), 0)

    def test_missing_signature_is_refused(self):
        response = self.client.post(
            self.url, data=_body(), content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(WhatsappEvent.objects.count(), 0)

    def test_tampered_body_under_a_valid_signature_is_refused(self):
        body = _body()
        tampered = body.replace(b"Printer", b"Printor")
        response = self.client.post(
            self.url, data=tampered, content_type="application/json",
            HTTP_X_HUB_SIGNATURE_256=sign(body),
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(WhatsappEvent.objects.count(), 0)

    def test_signature_from_the_wrong_secret_is_refused(self):
        body = _body()
        response = self.client.post(
            self.url, data=body, content_type="application/json",
            HTTP_X_HUB_SIGNATURE_256=sign(body, secret="not-our-secret"),
        )
        self.assertEqual(response.status_code, 403)

    @override_settings(WHATSAPP_APP_SECRET="")
    def test_unconfigured_secret_rejects_rather_than_failing_open(self):
        body = _body()
        response = self.client.post(
            self.url, data=body, content_type="application/json",
            HTTP_X_HUB_SIGNATURE_256=sign(body),
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(WhatsappEvent.objects.count(), 0)

    # ---- POST: payload handling ----

    def test_status_only_delivery_records_no_messages(self):
        """Delivery receipts must never look like inbound requests."""
        response = self.client.post(
            self.url, data=STATUS_BODY, content_type="application/json",
            HTTP_X_HUB_SIGNATURE_256=sign(STATUS_BODY),
        )
        self.assertEqual(response.status_code, 200)
        event = WhatsappEvent.objects.get()
        self.assertEqual(event.message_count, 0)
        self.assertEqual(event.status_count, 1)

    def test_malformed_json_is_acknowledged_and_kept(self):
        """A valid signature over bad JSON means Meta really sent it.

        Retrying cannot fix it, so ACK and keep the bytes for diagnosis.
        """
        bad = b'{"object": "whatsapp_business_account", OOPS'
        response = self.client.post(
            self.url, data=bad, content_type="application/json",
            HTTP_X_HUB_SIGNATURE_256=sign(bad),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(WhatsappEvent.objects.count(), 1)

    @override_settings(WHATSAPP_MAX_PAYLOAD_BYTES=64)
    def test_oversized_payload_is_acknowledged_but_dropped(self):
        body = _body()
        response = self.client.post(
            self.url, data=body, content_type="application/json",
            HTTP_X_HUB_SIGNATURE_256=sign(body),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(WhatsappEvent.objects.count(), 0)


@override_settings(WHATSAPP_INTAKE_ENABLED=False, WHATSAPP_APP_SECRET=SECRET)
class WhatsappWebhookDisabledTests(TestCase):
    def test_disabled_feature_hides_the_endpoint(self):
        body = _body()
        response = Client().post(
            "/webhooks/whatsapp/", data=body, content_type="application/json",
            HTTP_X_HUB_SIGNATURE_256=sign(body),
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(WhatsappEvent.objects.count(), 0)
