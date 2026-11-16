"""Linking a WhatsApp reply to the ticket it belongs to.

Deliberately NOT a cascade like mail's. Mail can match on a quoted ticket
number, In-Reply-To, References and a provider thread id, and has to rank those
by certainty. WhatsApp has no subject line to quote a reference in and no
References header, so there is exactly one signal available -- this person,
this company, still open -- and it is either true or it is not. There is no
ambiguous case to send to review, which is why this returns a ticket or
nothing rather than mail's certain/uncertain/none.

The grouping rule the product asked for: one ticket per message, EXCEPT when
the sender replies while they already have an open WhatsApp ticket, in which
case the message appends to it.
"""

import logging
from dataclasses import dataclass

from django.db import transaction

from apps.tickets.constants import TERMINAL_TICKET_STATUSES, TicketSource

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WhatsappThreadMatch:
    ticket: object = None
    reason: str = ""

    @property
    def matched(self):
        return self.ticket is not None


def _ticket_from_context(message):
    """The message this one explicitly replied to, if it opened a ticket.

    A swipe-to-reply is the only provider-side thread signal WhatsApp gives,
    and it is more certain than "same sender, still open" because the user
    pointed at the conversation themselves.
    """
    if not message.context_wamid:
        return None

    from apps.whatsapp_intake.models import WhatsappMessage

    parent = (
        WhatsappMessage.objects
        .filter(wamid=message.context_wamid, company_id=message.company_id)
        .exclude(linked_ticket__isnull=True)
        .select_related("linked_ticket")
        .order_by("-id")
        .first()
    )
    if parent is None:
        return None

    ticket = parent.linked_ticket
    if ticket.is_deleted or ticket.status in TERMINAL_TICKET_STATUSES:
        return None
    return ticket


def find_open_whatsapp_ticket(message):
    """Identify the ticket this message continues, if any."""
    from apps.tickets.models import SupportTicket

    if not message.company_id or not message.wa_id:
        return WhatsappThreadMatch(reason="No company or sender to match on.")

    ticket = _ticket_from_context(message)
    if ticket is not None:
        return WhatsappThreadMatch(
            ticket, reason=f"Replied directly to {ticket.reference}.",
        )

    # The sender key is the phone number, held in reported_by_email for
    # WHATSAPP-source rows. from_name is an editable profile name and must
    # never be matched on: the sender can change it between messages, two
    # people can share one, and it is frequently blank.
    open_tickets = (
        SupportTicket.objects
        .filter(
            company_id=message.company_id,
            source=TicketSource.WHATSAPP,
            reported_by_email=message.wa_id,
            is_deleted=False,
        )
        .exclude(status__in=TERMINAL_TICKET_STATUSES)
        .order_by("-created_at", "-id")
    )

    found = list(open_tickets[:2])
    if not found:
        return WhatsappThreadMatch(reason="No open WhatsApp ticket for this sender.")
    if len(found) > 1:
        # Possible when an operator raised one by hand. The newest is the live
        # conversation; say so rather than failing.
        logger.warning(
            "Sender %s has more than one open WhatsApp ticket; using the newest",
            message.wa_id,
        )
    return WhatsappThreadMatch(
        found[0], reason=f"Open WhatsApp ticket {found[0].reference}.",
    )


@transaction.atomic
def apply_thread_update(*, message, ticket, actor=None, request=None):
    """Attach a reply to its ticket without creating anything new."""
    from apps.audit.models import AuditAction
    from apps.tickets.constants import TicketUpdateSource
    from apps.tickets.services.activity_service import record_activity
    from apps.tickets.services.ticket_service import add_ticket_update
    from apps.whatsapp_intake.constants import WhatsappProcessingStatus
    from apps.whatsapp_intake.services.history_service import transition
    from apps.whatsapp_intake.services.title_service import build_description
    from common.exceptions.domain import WorkflowValidationError
    from common.services.audit import record_audit

    if not message.company_id or message.company_id != ticket.company_id:
        raise WorkflowValidationError(
            {"detail": ["Cannot link messages across companies."]}
        )

    excerpt = build_description(message)[:4000] or "(no content)"
    sender = message.from_name or message.wa_id
    note = f"WhatsApp reply from {sender}"

    message.linked_ticket = ticket
    message.is_thread_reply = True
    message.save(update_fields=["linked_ticket", "is_thread_reply", "updated_at"])

    if ticket.bug_id:
        # The ticket is confirmed, so the reply belongs on the bug's timeline.
        # add_update() is the only sanctioned way in: it refreshes the
        # denormalised latest_* columns the dashboard reads.
        from apps.accounts.services.system_user import get_whatsapp_intake_user
        from apps.bugs.services.update_service import add_update

        add_update(
            bug=ticket.bug,
            actor=actor or get_whatsapp_intake_user(),
            update_text=excerpt,
            remarks=note,
            is_system_generated=True,
            request=request,
        )
    else:
        # No bug yet. Without this branch the reply would simply be lost.
        add_ticket_update(
            ticket=ticket,
            update_text=excerpt,
            actor=actor,
            source=TicketUpdateSource.WHATSAPP,
            whatsapp_message=message,
            remarks=note,
            request=request,
        )

    transition(
        message=message,
        to_status=WhatsappProcessingStatus.THREAD_UPDATE,
        action="THREAD_LINKED",
        remarks=f"Linked to {ticket.reference}.",
        actor=actor,
        processed=True,
    )

    from apps.accounts.services.system_user import get_whatsapp_intake_user

    record_audit(
        action=AuditAction.WHATSAPP_THREAD_LINKED,
        entity=ticket,
        actor=actor or get_whatsapp_intake_user(),
        new_value=ticket.reference,
        remarks=note,
        metadata={"source": "SYSTEM",
                  "whatsapp_message_id": str(message.unique_id)},
        request=request,
    )
    record_activity(
        ticket=ticket,
        event_type="TICKET_UPDATE_RECEIVED",
        title="New message from requester",
        description=note,
        public_description="We received your new message.",
        actor_email="",
    )
    return ticket
