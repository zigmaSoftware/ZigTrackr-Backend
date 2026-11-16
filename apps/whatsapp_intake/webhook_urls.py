"""Public webhook route.

Deliberately NOT under api/v1/: everything there is cookie-JWT and CSRF
territory, and a public endpoint living among those is one careless router
registration away from being mistaken for an internal one.
"""

from django.urls import path

from apps.whatsapp_intake.views.webhook_views import WhatsappWebhookView

urlpatterns = [
    path("", WhatsappWebhookView.as_view(), name="whatsapp-webhook"),
]
