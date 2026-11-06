from django.core.management.base import BaseCommand

from fund.services import refresh_monthly_ledger


class Command(BaseCommand):
    help = "Create current contribution schedules and apply overdue monthly penalties."

    def handle(self, *args, **options):
        refresh_monthly_ledger()
        self.stdout.write(self.style.SUCCESS("Monthly contribution ledger refreshed."))
