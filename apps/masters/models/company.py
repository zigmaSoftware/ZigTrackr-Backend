from django.db import models

from common.models import NamedMaster


class CompanyMaster(NamedMaster):
    code = models.CharField(max_length=20, unique=True)
    support_email = models.EmailField(null=True, blank=True, unique=True)
    # The Meta phone_number_id of this company's WhatsApp business number --
    # the stable id, not the display number, which is formatted inconsistently
    # across payloads and changes in Business Manager without the id changing.
    # Blank rather than null so the "configured?" test is one truthiness check;
    # not unique for the same reason, since blank repeats across companies.
    support_whatsapp_number_id = models.CharField(
        max_length=50, blank=True, default="", db_index=True,
    )

    class Meta:
        db_table = "company_master"
        ordering = ["name"]
