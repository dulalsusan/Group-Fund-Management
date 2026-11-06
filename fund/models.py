import uuid
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone

from .calendar_utils import nepali_month_offset

def private_upload_path(instance, filename):
    suffix = Path(filename).suffix.lower()
    if isinstance(instance, MonthlyContribution):
        return f"contributions/{uuid.uuid4().hex}{suffix}"
    if isinstance(instance, CatchUpPayment):
        return f"catch-up/{uuid.uuid4().hex}{suffix}"
    return f"shared/{uuid.uuid4().hex}{suffix}"


class MemberProfile(models.Model):
    class Role(models.TextChoices):
        ADMIN = "admin", "Admin"
        MEMBER = "member", "Member"
        CLIENT = "client", "Client"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending group approval"
        ACTIVE = "active", "Active"
        EXITED = "exited", "Exited"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="member_profile"
    )
    role = models.CharField(max_length=16, choices=Role.choices, default=Role.MEMBER)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    joined_at = models.DateField(null=True, blank=True)
    voting_suspended = models.BooleanField(default=False)
    bank_signatory = models.BooleanField(default=False)
    catch_up_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    catch_up_paid = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("user__first_name", "user__last_name", "user__username")

    def clean(self):
        if (
            self.role == self.Role.ADMIN
            and self.user_id
            and not self.user.is_superuser
        ):
            raise ValidationError(
                {"user": "An Admin profile must be linked to a Django superuser."}
            )

    def __str__(self):
        return self.user.get_full_name() or self.user.get_username()

    @property
    def is_active_member(self):
        return self.role == self.Role.MEMBER and self.status == self.Status.ACTIVE


class FundSettings(models.Model):
    opening_fund = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    opening_cash = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    monthly_contribution = models.DecimalField(max_digits=10, decimal_places=2, default=5000)
    bank_account_label = models.CharField(max_length=120, blank=True)

    def __str__(self):
        return "Group fund settings"


class LedgerEntry(models.Model):
    class Type(models.TextChoices):
        CONTRIBUTION = "contribution", "Member contribution"
        CATCH_UP = "catch_up", "New-member catch-up contribution"
        INTEREST = "interest", "Loan interest"
        PENALTY = "penalty", "Late penalty"
        FEE = "fee", "Service / transaction charge"
        INVESTMENT_RETURN = "investment_return", "Investment return"
        LOAN_DISBURSEMENT = "loan_disbursement", "Loan disbursement"
        PRINCIPAL_REPAYMENT = "principal_repayment", "Loan principal repayment"
        PAYOUT = "payout", "Member / emergency payout"
        ADMIN_COST = "admin_cost", "Administrative cost"
        INVESTMENT = "investment", "Investment deployment"
        LOSS = "loss", "Investment loss"
        OPENING_ADJUSTMENT = "opening_adjustment", "Opening-balance adjustment"

    entry_type = models.CharField(max_length=24, choices=Type.choices)
    amount = models.DecimalField(
        max_digits=12, decimal_places=2, validators=[MinValueValidator(0.01)]
    )
    fund_delta = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    cash_delta = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    member = models.ForeignKey(
        MemberProfile, on_delete=models.SET_NULL, null=True, blank=True, related_name="ledger_entries"
    )
    description = models.CharField(max_length=240)
    reference = models.CharField(max_length=120, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ("-created_at", "-pk")

    def __str__(self):
        return f"{self.get_entry_type_display()} - NPR {self.amount}"


class MonthlyContribution(models.Model):
    class Status(models.TextChoices):
        DUE = "due", "Due"
        SUBMITTED = "submitted", "Proof submitted"
        VERIFIED = "verified", "Verified"
        REJECTED = "rejected", "Proof rejected"

    member = models.ForeignKey(
        MemberProfile, on_delete=models.CASCADE, related_name="contributions"
    )
    period = models.DateField(help_text="Gregorian date corresponding to the first day of this B.S. contribution month.")
    amount = models.DecimalField(max_digits=10, decimal_places=2, default=5000)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DUE)
    proof = models.FileField(upload_to=private_upload_path, blank=True)
    transaction_reference = models.CharField(max_length=120, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="verified_contributions",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-period", "member__user__username")
        constraints = [
            models.UniqueConstraint(fields=("member", "period"), name="unique_member_contribution_period")
        ]

    def __str__(self):
        return f"{self.member} - {self.period:%B %Y}"


class ContributionPenalty(models.Model):
    contribution = models.ForeignKey(
        MonthlyContribution, on_delete=models.CASCADE, related_name="penalties"
    )
    penalty_month = models.DateField()
    amount = models.DecimalField(max_digits=8, decimal_places=2, default=50)
    charged_at = models.DateTimeField(default=timezone.now)
    paid_at = models.DateTimeField(null=True, blank=True)
    paid_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    payment_reference = models.CharField(max_length=120, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("contribution", "penalty_month"), name="unique_contribution_penalty_month"
            )
        ]


class ReminderLog(models.Model):
    class Type(models.TextChoices):
        DUE = "due", "Contribution due"
        GRACE = "grace", "Contribution grace deadline"

    member = models.ForeignKey(
        MemberProfile, on_delete=models.CASCADE, related_name="reminder_logs"
    )
    period = models.DateField()
    reminder_type = models.CharField(max_length=8, choices=Type.choices)
    sent_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("member", "period", "reminder_type"),
                name="unique_member_contribution_reminder",
            )
        ]


class CatchUpPayment(models.Model):
    class Status(models.TextChoices):
        SUBMITTED = "submitted", "Proof submitted"
        VERIFIED = "verified", "Verified"
        REJECTED = "rejected", "Proof rejected"

    member = models.ForeignKey(
        MemberProfile, on_delete=models.CASCADE, related_name="catch_up_payments"
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    proof = models.FileField(upload_to=private_upload_path, blank=True)
    transaction_reference = models.CharField(max_length=120, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.SUBMITTED)
    submitted_at = models.DateTimeField(auto_now_add=True)
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )


class Decision(models.Model):
    class Category(models.TextChoices):
        LOAN = "loan", "Member loan"
        EMERGENCY_PAYOUT = "emergency_payout", "Emergency payout"
        INVESTMENT = "investment", "Business investment"
        MEMBER_ADDITION = "member_addition", "New-member approval"
        MEMBER_EXIT = "member_exit", "Member exit payout"
        ADMINISTRATIVE = "administrative", "Administrative decision"
        OTHER = "other", "Other group decision"

    class Threshold(models.TextChoices):
        TWO_THIRDS = "two_thirds", "Two-thirds"
        UNANIMOUS = "unanimous", "Unanimous"
        SIMPLE_MAJORITY = "simple_majority", "Simple majority"

    class Status(models.TextChoices):
        OPEN = "open", "Voting open"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        EXECUTED = "executed", "Executed"

    title = models.CharField(max_length=160)
    category = models.CharField(max_length=24, choices=Category.choices)
    description = models.TextField()
    amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    threshold = models.CharField(max_length=20, choices=Threshold.choices)
    requested_by = models.ForeignKey(
        MemberProfile, on_delete=models.PROTECT, related_name="decisions_requested"
    )
    applicant = models.ForeignKey(
        MemberProfile, on_delete=models.PROTECT, null=True, blank=True, related_name="decisions_as_applicant"
    )
    candidate = models.ForeignKey(
        MemberProfile, on_delete=models.PROTECT, null=True, blank=True, related_name="membership_decisions"
    )
    second_signatory = models.ForeignKey(
        MemberProfile,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="decisions_seconded",
    )
    executed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="executed_fund_decisions",
    )
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.OPEN)
    created_at = models.DateTimeField(auto_now_add=True)
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return self.title


class Vote(models.Model):
    decision = models.ForeignKey(Decision, on_delete=models.CASCADE, related_name="votes")
    member = models.ForeignKey(MemberProfile, on_delete=models.CASCADE, related_name="votes")
    approve = models.BooleanField()
    comment = models.CharField(max_length=240, blank=True)
    cast_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("decision", "member"), name="unique_member_decision_vote")
        ]


class LoanApplication(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Awaiting votes"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        DISBURSED = "disbursed", "Partially / fully disbursed"
        REPAID = "repaid", "Repaid"
        CANCELLED = "cancelled", "Cancelled"

    borrower = models.ForeignKey(
        MemberProfile, on_delete=models.PROTECT, related_name="loan_applications"
    )
    decision = models.OneToOneField(
        Decision, on_delete=models.PROTECT, related_name="loan_application"
    )
    requested_amount = models.DecimalField(max_digits=12, decimal_places=2)
    approved_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    monthly_income = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    monthly_expenses = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    emergency = models.BooleanField(default=False)
    medical_emergency = models.BooleanField(default=False)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    guarantor_details = models.CharField(max_length=240, blank=True)
    requested_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-requested_at",)

    @property
    def principal_disbursed(self):
        return sum((tranche.principal for tranche in self.tranches.all()), start=0)

    @property
    def outstanding_principal(self):
        disbursed = sum((tranche.principal for tranche in self.tranches.all()), start=0)
        repaid = sum((repayment.principal for repayment in self.repayments.all()), start=0)
        return max(disbursed - repaid, 0)

    @property
    def waiting_balance(self):
        return max(self.approved_amount - self.principal_disbursed, 0)

    @property
    def is_active(self):
        return self.outstanding_principal > 0

    @property
    def monthly_principal_target(self):
        return (self.approved_amount / Decimal("12.00")).quantize(Decimal("0.01"))

    @property
    def next_due_date(self):
        first_tranche = self.tranches.order_by("disbursed_at").first()
        if not first_tranche:
            return None
        due_day = timezone.localtime(first_tranche.disbursed_at).date()
        today = timezone.localdate()
        while due_day <= today:
            due_day = nepali_month_offset(due_day, 1)
        return due_day


class LoanTranche(models.Model):
    loan = models.ForeignKey(LoanApplication, on_delete=models.PROTECT, related_name="tranches")
    principal = models.DecimalField(max_digits=12, decimal_places=2)
    service_charge = models.DecimalField(max_digits=8, decimal_places=2, default=100)
    transaction_charge = models.DecimalField(max_digits=8, decimal_places=2, default=10)
    transaction_charge_applicable = models.BooleanField(default=True)
    disbursed_at = models.DateTimeField(default=timezone.now)
    disbursed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )

    @property
    def net_paid(self):
        transaction_charge = self.transaction_charge if self.transaction_charge_applicable else 0
        return max(self.principal - self.service_charge - transaction_charge, 0)


class LoanRepayment(models.Model):
    loan = models.ForeignKey(LoanApplication, on_delete=models.PROTECT, related_name="repayments")
    principal = models.DecimalField(max_digits=12, decimal_places=2)
    interest = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    paid_at = models.DateTimeField(default=timezone.now)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    reference = models.CharField(max_length=120, blank=True)


class ExitRequest(models.Model):
    class Status(models.TextChoices):
        NOTICE = "notice", "Notice period"
        PENDING = "pending", "Awaiting approval"
        APPROVED = "approved", "Approved"
        PAID = "paid", "Paid"
        CANCELLED = "cancelled", "Cancelled"

    member = models.ForeignKey(MemberProfile, on_delete=models.PROTECT, related_name="exit_requests")
    decision = models.OneToOneField(
        Decision, on_delete=models.PROTECT, null=True, blank=True, related_name="exit_request"
    )
    notice_date = models.DateField(default=timezone.localdate)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.NOTICE)
    created_at = models.DateTimeField(auto_now_add=True)


class SharedDocument(models.Model):
    class Category(models.TextChoices):
        BANK_STATEMENT = "bank_statement", "Monthly bank statement"
        MONTHLY_SUMMARY = "monthly_summary", "Monthly summary report"
        RECONCILIATION = "reconciliation", "Quarterly reconciliation"
        MEETING_MINUTES = "meeting_minutes", "Meeting minutes"
        RULEBOOK = "rulebook", "Rule book / policy"
        OTHER = "other", "Other shared document"

    title = models.CharField(max_length=160)
    category = models.CharField(max_length=24, choices=Category.choices)
    file = models.FileField(upload_to=private_upload_path)
    reporting_period = models.DateField(
        null=True, blank=True, help_text="Gregorian date corresponding to the first day of the B.S. reporting month."
    )
    notes = models.TextField(blank=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-uploaded_at",)

    def __str__(self):
        return self.title
