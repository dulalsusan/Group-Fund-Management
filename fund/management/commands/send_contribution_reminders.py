from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError, transaction
from django.utils import timezone

from fund.calendar_utils import nepali_month_start, to_nepali_date
from fund.models import MemberProfile, MonthlyContribution, ReminderLog
from fund.services import refresh_monthly_ledger


class Command(BaseCommand):
    help = "Send B.S. contribution reminders on the 10th and 20th to active members."

    def handle(self, *args, **options):
        today = timezone.localdate()
        bs_today = to_nepali_date(today)
        if bs_today.day not in (10, 20):
            self.stdout.write("No contribution reminder is due today.")
            return

        period = nepali_month_start(today)
        reminder_type = (
            ReminderLog.Type.DUE if bs_today.day == 10 else ReminderLog.Type.GRACE
        )
        refresh_monthly_ledger(today)
        message = (
            "Your NPR 5,000 Group Savings contribution is due today, the 10th of the "
            "Bikram Sambat month. Please submit your payment proof after payment."
            if reminder_type == ReminderLog.Type.DUE
            else "The grace period for your NPR 5,000 Group Savings contribution ends "
            "today, the 20th of the Bikram Sambat month. A late fee applies after today."
        )
        sent = 0
        missing_email = []
        for member in MemberProfile.objects.filter(
            role=MemberProfile.Role.MEMBER, status=MemberProfile.Status.ACTIVE
        ).select_related("user"):
            contribution = MonthlyContribution.objects.filter(
                member=member, period=period
            ).first()
            if not contribution or contribution.status == MonthlyContribution.Status.VERIFIED:
                continue
            if not member.user.email:
                missing_email.append(member.user.get_username())
                continue
            try:
                with transaction.atomic():
                    ReminderLog.objects.create(
                        member=member, period=period, reminder_type=reminder_type
                    )
                    sent_count = send_mail(
                        subject=f"Group Savings contribution reminder — B.S. {bs_today.year}/{bs_today.month:02d}",
                        message=f"Dear {member},\n\n{message}\n\nThank you.",
                        from_email=settings.DEFAULT_FROM_EMAIL,
                        recipient_list=[member.user.email],
                        fail_silently=False,
                    )
                    if sent_count != 1:
                        raise CommandError(
                            f"Reminder email could not be sent to {member.user.email}."
                        )
            except IntegrityError:
                continue
            sent += 1
        self.stdout.write(self.style.SUCCESS(f"Sent {sent} contribution reminder(s)."))
        if missing_email:
            raise CommandError(
                "Active members need an email address to receive reminders: "
                + ", ".join(missing_email)
            )
