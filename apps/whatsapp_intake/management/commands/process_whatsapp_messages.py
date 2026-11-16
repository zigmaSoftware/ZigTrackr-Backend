"""Run the WhatsApp intake pipeline by hand.

Mirrors process_support_mail: the same flags, the same advisory lock name as
the Beat task so the two genuinely exclude each other, and the same treatment
of "nothing to do" and "someone else is running" as successes rather than
errors.
"""

from django.core.management.base import BaseCommand, CommandError

from common.db.locks import advisory_lock

LOCK_NAME = "whatsapp_intake_run"


class Command(BaseCommand):
    help = "Process stored WhatsApp webhook deliveries into tickets."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=None,
                            help="Maximum deliveries to process this run.")
        parser.add_argument("--dry-run", action="store_true",
                            help="Report what would be processed, change nothing.")
        parser.add_argument("--retry-only", action="store_true",
                            help="Resume stalled messages; read no new deliveries.")
        parser.add_argument("--no-lock", action="store_true",
                            help="Skip the advisory lock. For debugging only.")

    def handle(self, *args, **options):
        from django.conf import settings

        if not settings.WHATSAPP_INTAKE_ENABLED:
            self.stdout.write(self.style.WARNING(
                "WHATSAPP_INTAKE_ENABLED is false; nothing to do."
            ))
            return

        if options["dry_run"]:
            return self._dry_run()

        if options["no_lock"]:
            return self._run(options)

        with advisory_lock(LOCK_NAME) as acquired:
            if not acquired:
                self.stdout.write(self.style.WARNING(
                    "Another run holds the lock; exiting."
                ))
                return
            return self._run(options)

    def _dry_run(self):
        from apps.whatsapp_intake.constants import (
            EventStatus, RETRYABLE_WHATSAPP_STATUSES,
        )
        from apps.whatsapp_intake.models import WhatsappEvent, WhatsappMessage

        pending = WhatsappEvent.objects.filter(
            processing_status=EventStatus.PENDING,
        ).count()
        stalled = WhatsappMessage.objects.filter(
            processing_status__in=RETRYABLE_WHATSAPP_STATUSES,
        ).count()
        self.stdout.write(
            f"Would process {pending} pending delivery(ies) "
            f"and resume {stalled} stalled message(s)."
        )

    def _run(self, options):
        from apps.whatsapp_intake.services.intake_orchestrator import (
            WhatsappRunResult, retry_pending, run_whatsapp_intake,
        )

        try:
            if options["retry_only"]:
                result = WhatsappRunResult()
                retry_pending(limit=options["limit"] or 50, result=result)
            else:
                result = run_whatsapp_intake(limit=options["limit"])
        except Exception as exc:  # noqa: BLE001
            # The message only -- a traceback in an operator's terminal is
            # noise, and the exception is already logged with its stack.
            raise CommandError(f"WhatsApp intake failed: {exc}") from exc

        summary = result.as_dict()
        if not any(summary.values()):
            self.stdout.write("Nothing to process.")
            return

        self.stdout.write(self.style.SUCCESS(
            " ".join(f"{k}={v}" for k, v in summary.items() if v)
        ))
