"""Which company does this message belong to?

Mail resolves company from CompanyMaster.support_email matching the mailbox
address. The WhatsApp analogue keys on metadata.phone_number_id -- the stable
id of the business number that received the message, not display_phone_number,
which is formatted inconsistently across payloads and can change in Business
Manager without the id changing.
"""

import logging

from django.conf import settings

logger = logging.getLogger(__name__)


def resolve_company(phone_number_id):
    """phone_number_id -> CompanyMaster, or None.

    Returns None rather than guessing when a configured deployment receives an
    unknown number. An unattributed message is then visible only to Super Admin
    via scope_company_queryset, which is the correct outcome: silently
    defaulting it into the fallback company would leak one company's requests
    into another's queue.

    The fallback applies ONLY when no company has a WhatsApp number configured
    at all -- the single-tenant pilot case. The moment one company is mapped,
    an unknown number resolves to nothing.
    """
    from apps.masters.models import CompanyMaster

    phone_number_id = str(phone_number_id or "").strip()

    if phone_number_id:
        company = CompanyMaster.objects.filter(
            support_whatsapp_number_id=phone_number_id,
            is_deleted=False,
        ).first()
        if company is not None:
            return company

    any_mapped = CompanyMaster.objects.filter(
        is_deleted=False,
    ).exclude(support_whatsapp_number_id="").exists()

    if any_mapped:
        logger.warning(
            "WhatsApp number %s is not mapped to any company; leaving "
            "the message unattributed", phone_number_id or "(missing)",
        )
        return None

    fallback = CompanyMaster.objects.filter(
        code=settings.WHATSAPP_DEFAULT_COMPANY_CODE, is_deleted=False,
    ).first()
    if fallback is None:
        logger.warning(
            "No WhatsApp number is mapped and fallback company %s does not exist",
            settings.WHATSAPP_DEFAULT_COMPANY_CODE,
        )
    return fallback
