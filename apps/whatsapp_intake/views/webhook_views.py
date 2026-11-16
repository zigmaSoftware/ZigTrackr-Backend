"""The public WhatsApp webhook: the only unauthenticated endpoint in this app.

Three rules shape it:

  1. Verify, store, acknowledge. Nothing else. Meta retries any delivery it
     does not see acknowledged within seconds, so ticket work happens later in
     the Beat task, never inline.
  2. The HMAC is the entire security boundary. A request that fails it is
     refused with 403 and leaves no trace in the pipeline.
  3. Status codes are chosen for what Meta does with them: 500 when we want the
     retry (our database failed), 403 when we never want it (a forgery), and
     200 for everything else, because retrying an unparseable or unmappable
     payload cannot change the outcome and Meta would keep trying for hours.
"""

import logging

from django.conf import settings
from django.http import HttpResponse
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from rest_framework.permissions import AllowAny
from rest_framework.renderers import JSONRenderer
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from apps.whatsapp_intake.services.event_service import store_raw_event
from apps.whatsapp_intake.services.signature import (
    compute_event_id, verify_signature, verify_token,
)

logger = logging.getLogger(__name__)


class WhatsappWebhookThrottle(AnonRateThrottle):
    scope = "whatsapp_webhook"


@method_decorator(csrf_exempt, name="dispatch")
@method_decorator(never_cache, name="dispatch")
class WhatsappWebhookView(APIView):
    # An EMPTY list, not AllowAny with the default authenticators. An empty
    # authentication_classes means CookieJWTAuthentication never runs, so it
    # never calls _enforce_csrf() -- that is what actually exempts this
    # endpoint. csrf_exempt covers Django's middleware layer. Both are needed;
    # neither alone is sufficient.
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [WhatsappWebhookThrottle]
    # NOT the project's EnvelopeJSONRenderer. Meta ignores the body, but the
    # envelope's exception path could reshape a status code, and the status is
    # the only thing that matters here.
    renderer_classes = [JSONRenderer]

    def get(self, request, *args, **kwargs):
        """Meta's one-time subscription handshake."""
        if not settings.WHATSAPP_INTAKE_ENABLED:
            return HttpResponse(status=404)

        mode = request.GET.get("hub.mode")
        token = request.GET.get("hub.verify_token", "")
        challenge = request.GET.get("hub.challenge", "")

        if mode == "subscribe" and verify_token(
            received=token, expected=settings.WHATSAPP_VERIFY_TOKEN
        ):
            # Plain text, echoed verbatim. Meta compares byte-for-byte, so a
            # JSON envelope or a trailing newline fails the handshake with a
            # uselessly generic error in the dashboard.
            return HttpResponse(challenge, content_type="text/plain", status=200)

        logger.warning("WhatsApp webhook verification failed (mode=%s)", mode)
        return HttpResponse(status=403)

    def post(self, request, *args, **kwargs):
        """Accept one delivery."""
        if not settings.WHATSAPP_INTAKE_ENABLED:
            return HttpResponse(status=404)

        raw = request.body
        if len(raw) > settings.WHATSAPP_MAX_PAYLOAD_BYTES:
            # ACK rather than refuse: making Meta retry an oversized body
            # achieves nothing but repeating the problem.
            logger.warning("Oversized WhatsApp webhook payload: %s bytes", len(raw))
            return HttpResponse(status=200)

        signature = request.headers.get("X-Hub-Signature-256", "")
        if not verify_signature(
            raw_body=raw, header=signature, app_secret=settings.WHATSAPP_APP_SECRET
        ):
            # 403, never 200. A forgery is not something to acknowledge, and
            # genuine Meta retries always carry a valid signature, so this
            # cannot cause a retry storm.
            logger.warning(
                "WhatsApp webhook signature rejected from %s",
                request.META.get("REMOTE_ADDR", "unknown"),
            )
            return HttpResponse(status=403)

        event_id = compute_event_id(raw)
        try:
            store_raw_event(
                raw_body=raw, event_id=event_id, signature_header=signature,
            )
        except Exception:
            # The one failure we genuinely want retried: Meta's redelivery is
            # our durability backstop when the database is unavailable.
            logger.exception("Failed to store WhatsApp delivery %s", event_id[:16])
            return HttpResponse(status=500)

        return HttpResponse(status=200)
