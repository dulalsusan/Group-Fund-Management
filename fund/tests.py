from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core import mail
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django.core.files.uploadedfile import SimpleUploadedFile

from .models import (
    ContributionPenalty,
    Decision,
    FundSettings,
    LedgerEntry,
    LoanApplication,
    LoanTranche,
    MemberProfile,
    MonthlyContribution,
    ReminderLog,
    SharedDocument,
)
from .services import (
    cast_vote,
    disburse_loan,
    fund_snapshot,
    loan_limit,
    member_exit_payout,
    monthly_interest_due,
    refresh_monthly_ledger,
    required_vote_count,
)
from .calendar_utils import (
    from_nepali_date,
    nepali_day,
    nepali_month_offset,
    nepali_month_start,
    nepali_period,
    to_nepali_date,
)


class FundWorkflowTests(TestCase):
    def setUp(self):
        self.settings = FundSettings.objects.create(
            pk=1,
            opening_fund=Decimal("100000.00"),
            opening_cash=Decimal("100000.00"),
        )
        self.admin = User.objects.create_superuser(
            username="admin",
            email="admin@example.com",
            password="Admin-password-839!",
        )
        self.other_member = self.create_member("other-member")
        self.signatory = self.create_member("signatory", bank_signatory=True)
        self.member = self.create_member("member")

    def create_member(self, username, role=MemberProfile.Role.MEMBER, bank_signatory=False):
        user = User.objects.create_user(username=username, password="Safe-test-pass-943!")
        return MemberProfile.objects.create(
            user=user,
            role=role,
            status=MemberProfile.Status.ACTIVE,
            joined_at=timezone.localdate() - timedelta(days=400),
            bank_signatory=bank_signatory,
            catch_up_paid=True,
        )

    def create_loan(self, borrower, amount=Decimal("20000.00"), tranche_date=None):
        decision = Decision.objects.create(
            title="Test loan",
            category=Decision.Category.LOAN,
            description="Test",
            amount=amount,
            threshold=Decision.Threshold.TWO_THIRDS,
            requested_by=borrower,
            applicant=borrower,
            status=Decision.Status.APPROVED,
            second_signatory=self.signatory,
        )
        loan = LoanApplication.objects.create(
            borrower=borrower,
            decision=decision,
            requested_amount=amount,
            approved_amount=amount,
            status=LoanApplication.Status.APPROVED,
        )
        if tranche_date:
            LoanTranche.objects.create(
                loan=loan,
                principal=amount,
                disbursed_at=tranche_date,
                service_charge=Decimal("100.00"),
                transaction_charge=Decimal("10.00"),
            )
            loan.status = LoanApplication.Status.DISBURSED
            loan.save(update_fields=("status",))
        return loan

    def test_dashboard_and_member_pages_render(self):
        self.client.force_login(self.member.user)
        for url_name in ("dashboard", "ledger", "contributions", "loans", "decisions", "documents"):
            with self.subTest(page=url_name):
                response = self.client.get(reverse(url_name))
                self.assertEqual(response.status_code, 200)

    def test_admin_login_and_workspace_access(self):
        response = self.client.post(
            reverse("login"),
            {"username": "admin", "password": "Admin-password-839!"},
        )
        self.assertRedirects(response, reverse("post_login"), fetch_redirect_response=False)
        self.assertRedirects(
            self.client.get(reverse("post_login")),
            reverse("admin:index"),
            fetch_redirect_response=False,
        )
        self.assertEqual(self.client.get(reverse("admin:index")).status_code, 200)
        for admin_url in ("admin:auth_user_add", "admin:fund_memberprofile_add"):
            with self.subTest(admin_page=admin_url):
                self.assertEqual(self.client.get(reverse(admin_url)).status_code, 200)

        MemberProfile.objects.create(
            user=self.admin,
            role=MemberProfile.Role.ADMIN,
            status=MemberProfile.Status.ACTIVE,
        )
        for url_name in ("dashboard", "contributions", "loans", "documents"):
            with self.subTest(page=url_name):
                self.assertEqual(self.client.get(reverse(url_name)).status_code, 200)

    def test_client_login_is_limited_to_general_information(self):
        user = User.objects.create_user(
            username="client",
            password="Client-password-839!",
        )
        client_profile = MemberProfile.objects.create(
            user=user,
            role=MemberProfile.Role.CLIENT,
            status=MemberProfile.Status.ACTIVE,
            joined_at=timezone.localdate(),
        )
        self.assertFalse(client_profile.is_active_member)
        refresh_monthly_ledger()
        self.assertFalse(MonthlyContribution.objects.filter(member=client_profile).exists())

        response = self.client.post(
            reverse("login"),
            {"username": "client", "password": "Client-password-839!"},
        )
        self.assertRedirects(response, reverse("post_login"), fetch_redirect_response=False)
        self.assertRedirects(
            self.client.get(reverse("post_login")),
            reverse("client_overview"),
            fetch_redirect_response=False,
        )
        response = self.client.get(reverse("client_overview"))
        self.assertContains(response, "Group Savings Fund")
        self.assertContains(response, "9818123456")
        self.assertEqual(self.client.get(reverse("admin:index")).status_code, 302)
        self.assertRedirects(
            self.client.get(reverse("dashboard")),
            reverse("client_overview"),
            fetch_redirect_response=False,
        )

    def test_calendar_periods_follow_the_rule_books_bikram_sambat_month(self):
        gregorian_date = date(2026, 11, 5)
        period = nepali_month_start(gregorian_date)
        self.assertEqual(to_nepali_date(period).day, 1)
        self.assertEqual(to_nepali_date(nepali_day(period, 20)).day, 20)
        self.assertEqual(nepali_month_start(gregorian_date), nepali_period(2083, 7))

    def test_reserve_and_lendable_pool_limit_free_cash(self):
        loan = self.create_loan(self.member, amount=Decimal("40000.00"))
        LoanTranche.objects.create(
            loan=loan,
            principal=Decimal("40000.00"),
            service_charge=Decimal("100.00"),
            transaction_charge=Decimal("10.00"),
        )
        snapshot = fund_snapshot()
        self.assertEqual(snapshot["total_fund"], Decimal("100000.00"))
        self.assertEqual(snapshot["lendable_pool"], Decimal("80000.00"))
        self.assertEqual(snapshot["reserve"], Decimal("20000.00"))
        self.assertEqual(snapshot["outstanding_loans"], Decimal("40000.00"))
        self.assertEqual(snapshot["free_cash"], Decimal("40000.00"))

    def test_loan_transfer_requires_second_signatory_and_preserves_reserve(self):
        loan = self.create_loan(self.member)
        tranche = disburse_loan(loan, Decimal("20000.00"), self.admin)
        self.assertEqual(tranche.net_paid, Decimal("19890.00"))
        snapshot = fund_snapshot()
        self.assertEqual(snapshot["total_fund"], Decimal("100110.00"))
        self.assertEqual(snapshot["bank_cash"], Decimal("80110.00"))
        self.assertGreaterEqual(snapshot["bank_cash"], snapshot["reserve"])
        self.assertEqual(snapshot["outstanding_loans"], Decimal("20000.00"))

    def test_contribution_penalties_are_recorded_once_and_verification_is_ledgered(self):
        period = nepali_month_offset(nepali_month_start(timezone.localdate()), -2)
        contribution = MonthlyContribution.objects.create(
            member=self.member,
            period=period,
            amount=Decimal("5000.00"),
        )
        late_day = nepali_day(period, 20) + timedelta(days=1)
        self.member.joined_at = period
        self.member.save(update_fields=("joined_at",))
        for member in (self.other_member, self.signatory):
            member.joined_at = nepali_month_offset(late_day, 1)
            member.save(update_fields=("joined_at",))
        refresh_monthly_ledger(late_day)
        refresh_monthly_ledger(late_day)
        self.assertEqual(
            ContributionPenalty.objects.filter(contribution=contribution).count(), 1
        )
        self.assertEqual(
            LedgerEntry.objects.filter(
                entry_type=LedgerEntry.Type.PENALTY,
                reference__startswith=f"contribution:{contribution.pk}:",
            ).count(),
            1,
        )
        snapshot = fund_snapshot()
        self.assertEqual(snapshot["total_fund"], Decimal("100050.00"))
        self.assertEqual(snapshot["bank_cash"], Decimal("100000.00"))
        self.client.force_login(self.member.user)
        response = self.client.post(reverse("verify_contribution", args=(contribution.pk,)))
        self.assertEqual(response.status_code, 403)
        self.client.force_login(self.admin)
        response = self.client.post(reverse("verify_contribution", args=(contribution.pk,)))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            LedgerEntry.objects.filter(
                entry_type=LedgerEntry.Type.CONTRIBUTION,
                reference=f"contribution:{contribution.pk}",
            ).count(),
            1,
        )
        penalty = ContributionPenalty.objects.get(contribution=contribution)
        response = self.client.post(
            reverse("collect_penalty", args=(penalty.pk,)),
            {"reference": "PENALTY-PAID-1"},
        )
        self.assertEqual(response.status_code, 302)
        snapshot = fund_snapshot()
        self.assertEqual(snapshot["total_fund"], Decimal("105050.00"))
        self.assertEqual(snapshot["bank_cash"], Decimal("105050.00"))

    def test_loan_vote_excludes_applicant_and_requires_two_thirds(self):
        applicant = self.member
        voters = [self.other_member, self.signatory, self.create_member("other")]
        decision = Decision.objects.create(
            title="Loan vote",
            category=Decision.Category.LOAN,
            description="",
            threshold=Decision.Threshold.TWO_THIRDS,
            requested_by=applicant,
            applicant=applicant,
        )
        loan = LoanApplication.objects.create(
            borrower=applicant,
            decision=decision,
            requested_amount=Decimal("5000.00"),
        )
        self.assertEqual(required_vote_count(decision), 3)
        with self.assertRaisesMessage(ValueError, "not eligible to vote"):
            cast_vote(decision, applicant, True)
        for voter in voters[:2]:
            cast_vote(decision, voter, True)
        decision.refresh_from_db()
        self.assertEqual(decision.status, Decision.Status.OPEN)
        cast_vote(decision, voters[2], True)
        decision.refresh_from_db()
        loan.refresh_from_db()
        self.assertEqual(decision.status, Decision.Status.APPROVED)
        self.assertEqual(loan.approved_amount, Decimal("5000.00"))

    def test_new_member_loan_cap_is_eighty_percent_of_own_contributions(self):
        recent_member = self.create_member("recent")
        recent_member.joined_at = timezone.localdate() - timedelta(days=60)
        recent_member.save(update_fields=("joined_at",))
        LedgerEntry.objects.create(
            entry_type=LedgerEntry.Type.CONTRIBUTION,
            amount=Decimal("10000.00"),
            fund_delta=Decimal("10000.00"),
            cash_delta=Decimal("10000.00"),
            member=recent_member,
            description="Test contributions",
        )
        self.assertEqual(loan_limit(recent_member), Decimal("8000.00"))

    def test_loan_request_requires_affordable_repayment_and_creates_vote(self):
        self.client.force_login(self.member.user)
        response = self.client.post(
            reverse("request_loan"),
            {
                "amount": "10000.00",
                "monthly_income": "15000.00",
                "monthly_expenses": "5000.00",
                "can_afford": "on",
                "reason": "Urgent education expense",
                "guarantor_details": "Group guarantor",
            },
        )
        self.assertEqual(response.status_code, 302)
        loan = LoanApplication.objects.get(borrower=self.member)
        self.assertEqual(loan.monthly_income, Decimal("15000.00"))
        self.assertEqual(loan.decision.threshold, Decision.Threshold.TWO_THIRDS)
        self.assertEqual(loan.decision.applicant, self.member)
        second_response = self.client.post(
            reverse("request_loan"),
            {
                "amount": "10000.00",
                "monthly_income": "15000.00",
                "monthly_expenses": "5000.00",
                "can_afford": "on",
                "reason": "Another request",
            },
        )
        self.assertEqual(second_response.status_code, 200)
        self.assertEqual(LoanApplication.objects.filter(borrower=self.member).count(), 1)

    def test_loan_request_rejects_unaffordable_application(self):
        self.client.force_login(self.member.user)
        response = self.client.post(
            reverse("request_loan"),
            {
                "amount": "10000.00",
                "monthly_income": "6000.00",
                "monthly_expenses": "1000.00",
                "can_afford": "on",
                "reason": "Urgent expense",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(LoanApplication.objects.filter(borrower=self.member).exists())

    def test_members_cannot_upload_financial_summaries(self):
        self.client.force_login(self.member.user)
        response = self.client.post(
            reverse("documents"),
            {
                "title": "Monthly summary",
                "category": "monthly_summary",
                "reporting_period": "2083-07",
                "notes": "Test upload",
                "file": SimpleUploadedFile("summary.pdf", b"%PDF-1.4 test", content_type="application/pdf"),
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(SharedDocument.objects.filter(title="Monthly summary").exists())

    def test_monthly_interest_uses_actual_tranche_dates_and_diminishing_balance(self):
        today = timezone.localdate()
        first_date = today - timedelta(days=100)
        loan = self.create_loan(self.member, amount=Decimal("10000.00"), tranche_date=timezone.make_aware(
            timezone.datetime.combine(first_date, timezone.datetime.min.time())
        ))
        LoanTranche.objects.create(
            loan=loan,
            principal=Decimal("5000.00"),
            disbursed_at=timezone.now() - timedelta(days=5),
            service_charge=Decimal("100.00"),
            transaction_charge=Decimal("10.00"),
        )
        self.assertEqual(monthly_interest_due(loan, today), Decimal("150.00"))

    def test_exit_payout_excludes_profit_before_three_years_and_shares_after(self):
        LedgerEntry.objects.create(
            entry_type=LedgerEntry.Type.CONTRIBUTION,
            amount=Decimal("10000.00"),
            fund_delta=Decimal("10000.00"),
            cash_delta=Decimal("10000.00"),
            member=self.member,
            description="Member contribution",
        )
        self.settings.opening_fund = Decimal("1000.00")
        self.settings.opening_cash = Decimal("1000.00")
        self.settings.save()
        self.assertEqual(member_exit_payout(self.member), Decimal("10000.00"))
        self.member.joined_at = timezone.localdate() - timedelta(days=365 * 4)
        self.member.save(update_fields=("joined_at",))
        self.assertEqual(member_exit_payout(self.member), Decimal("10800.00"))

    def test_rulebook_is_available_only_to_active_members(self):
        response = self.client.get(reverse("rulebook"))
        self.assertEqual(response.status_code, 302)
        self.client.force_login(self.member.user)
        response = self.client.get(reverse("rulebook"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")

    def test_registration_creates_membership_pending_admin_approval(self):
        response = self.client.post(
            reverse("register"),
            {
                "username": "new-applicant",
                "first_name": "New",
                "last_name": "Member",
                "email": "new-member@example.com",
                "password1": "A-valid-password-839!W",
                "password2": "A-valid-password-839!W",
            },
        )
        self.assertEqual(response.status_code, 302)
        profile = MemberProfile.objects.get(user__username="new-applicant")
        self.assertEqual(profile.status, MemberProfile.Status.PENDING)
        self.assertEqual(self.client.get(reverse("pending")).status_code, 200)
        self.assertFalse(Decision.objects.filter(candidate=profile).exists())

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_reminder_command_sends_once_on_b_s_tenth(self):
        for member in (self.other_member, self.signatory, self.member):
            member.user.email = f"{member.user.username}@example.com"
            member.user.save(update_fields=("email",))
        reminder_day = from_nepali_date(2083, 7, 10)
        with patch(
            "fund.management.commands.send_contribution_reminders.timezone.localdate",
            return_value=reminder_day,
        ):
            call_command("send_contribution_reminders", verbosity=0)
            call_command("send_contribution_reminders", verbosity=0)
        self.assertEqual(len(mail.outbox), 3)
        self.assertEqual(ReminderLog.objects.count(), 3)

    def test_verified_contribution_receipt_is_printable_and_private(self):
        contribution = MonthlyContribution.objects.create(
            member=self.member,
            period=nepali_month_start(timezone.localdate()),
            amount=Decimal("5000.00"),
            status=MonthlyContribution.Status.VERIFIED,
            verified_at=timezone.now(),
            verified_by=self.admin,
            transaction_reference="TXN-RECEIPT-1",
        )
        self.client.force_login(self.member.user)
        response = self.client.get(
            reverse("contribution_receipt", args=(contribution.pk,))
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "TXN-RECEIPT-1")
