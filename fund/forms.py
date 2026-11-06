from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User
from decimal import Decimal
import re

from .calendar_utils import nepali_period

from .models import (
    CatchUpPayment,
    Decision,
    FundSettings,
    MemberProfile,
    MonthlyContribution,
    SharedDocument,
)
from .services import loan_limit


class SignUpForm(UserCreationForm):
    first_name = forms.CharField(max_length=150)
    last_name = forms.CharField(max_length=150)
    email = forms.EmailField()

    class Meta:
        model = User
        fields = ("username", "email", "first_name", "last_name")


class ContributionProofForm(forms.ModelForm):
    class Meta:
        model = MonthlyContribution
        fields = ("proof", "transaction_reference")
        widgets = {
            "transaction_reference": forms.TextInput(
                attrs={"placeholder": "Bank reference or transaction ID"}
            )
        }

    def clean(self):
        cleaned_data = super().clean()
        if not cleaned_data.get("proof") and not cleaned_data.get("transaction_reference"):
            raise forms.ValidationError("Upload a payment screenshot or enter a transaction reference.")
        return cleaned_data


class CatchUpPaymentForm(forms.ModelForm):
    class Meta:
        model = CatchUpPayment
        fields = ("proof", "transaction_reference")

    def clean(self):
        cleaned_data = super().clean()
        if not cleaned_data.get("proof") and not cleaned_data.get("transaction_reference"):
            raise forms.ValidationError("Upload a payment screenshot or enter a transaction reference.")
        return cleaned_data


class LoanRequestForm(forms.Form):
    amount = forms.DecimalField(
        min_value=5000,
        max_digits=12,
        decimal_places=2,
        label="Amount (NPR)",
    )
    emergency = forms.BooleanField(required=False)
    medical_emergency = forms.BooleanField(required=False)
    monthly_income = forms.DecimalField(
        min_value=0, max_digits=12, decimal_places=2, label="Monthly take-home income (NPR)"
    )
    monthly_expenses = forms.DecimalField(
        min_value=0, max_digits=12, decimal_places=2, label="Monthly expenses (NPR)"
    )
    can_afford = forms.BooleanField(
        required=True,
        label="I confirm the repayment is affordable alongside my monthly contribution.",
    )
    reason = forms.CharField(widget=forms.Textarea(attrs={"rows": 4}))
    guarantor_details = forms.CharField(max_length=240, required=False)

    def __init__(self, *args, member, **kwargs):
        self.member = member
        super().__init__(*args, **kwargs)

    def clean(self):
        cleaned_data = super().clean()
        amount = cleaned_data.get("amount")
        if amount is not None:
            maximum = loan_limit(self.member, cleaned_data.get("emergency", False))
            if amount > maximum:
                self.add_error("amount", f"Your current maximum is NPR {maximum:,.2f}.")
            income = cleaned_data.get("monthly_income")
            expenses = cleaned_data.get("monthly_expenses")
            if income is not None and expenses is not None:
                monthly_contribution = FundSettings.objects.get_or_create(pk=1)[0].monthly_contribution
                estimated_payment = amount / Decimal("12") + amount * Decimal("0.005")
                required_surplus = estimated_payment + monthly_contribution
                if income - expenses < required_surplus:
                    self.add_error(
                        "monthly_expenses",
                        "Your reported monthly surplus does not cover the estimated repayment plus your contribution.",
                    )
        return cleaned_data


class DecisionForm(forms.Form):
    category = forms.ChoiceField(
        choices=(
            (Decision.Category.EMERGENCY_PAYOUT, "Emergency outright payout"),
            (Decision.Category.INVESTMENT, "Business investment"),
            (Decision.Category.ADMINISTRATIVE, "Administrative decision"),
            (Decision.Category.OTHER, "Other group decision"),
        )
    )
    title = forms.CharField(max_length=160)
    amount = forms.DecimalField(
        max_digits=12, decimal_places=2, min_value=0, required=False, label="Amount (NPR)"
    )
    description = forms.CharField(widget=forms.Textarea(attrs={"rows": 4}))


class SharedDocumentForm(forms.ModelForm):
    reporting_period = forms.CharField(
        required=False,
        label="Reporting month or quarter (B.S.)",
        help_text="Use Bikram Sambat year-month, for example 2083-07.",
        widget=forms.TextInput(attrs={"placeholder": "2083-07"}),
    )

    class Meta:
        model = SharedDocument
        fields = ("title", "category", "file", "reporting_period", "notes")
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}

    def clean_file(self):
        uploaded = self.cleaned_data["file"]
        allowed = {".pdf", ".png", ".jpg", ".jpeg"}
        extension = "." + uploaded.name.rsplit(".", 1)[-1].lower() if "." in uploaded.name else ""
        if extension not in allowed:
            raise forms.ValidationError("Upload a PDF, PNG, or JPEG document.")
        if uploaded.size > 10 * 1024 * 1024:
            raise forms.ValidationError("The maximum file size is 10 MB.")
        return uploaded

    def clean(self):
        cleaned_data = super().clean()
        category = cleaned_data.get("category")
        reporting_value = cleaned_data.get("reporting_period")
        period_required = {
            SharedDocument.Category.BANK_STATEMENT,
            SharedDocument.Category.MONTHLY_SUMMARY,
            SharedDocument.Category.RECONCILIATION,
        }
        if category in period_required and not reporting_value:
            self.add_error("reporting_period", "Select the month or quarter covered by this document.")
        if reporting_value:
            match = re.fullmatch(r"(\d{4})-(\d{1,2})", reporting_value.strip())
            if not match:
                self.add_error("reporting_period", "Enter a B.S. year and month, for example 2083-07.")
            else:
                try:
                    year, month = int(match.group(1)), int(match.group(2))
                    cleaned_data["reporting_period"] = nepali_period(year, month)
                except ValueError:
                    self.add_error("reporting_period", "Enter a valid Bikram Sambat month.")
                else:
                    if category == SharedDocument.Category.RECONCILIATION and month not in (1, 4, 7, 10):
                        self.add_error("reporting_period", "Quarterly reconciliations must start in Baisakh, Shrawan, Kartik, or Magh.")
        return cleaned_data


class DisbursementForm(forms.Form):
    amount = forms.DecimalField(max_digits=12, decimal_places=2, min_value=0.01)
    transaction_charge_applicable = forms.BooleanField(required=False, initial=True)


class RepaymentForm(forms.Form):
    principal = forms.DecimalField(max_digits=12, decimal_places=2, min_value=0.01)
    reference = forms.CharField(max_length=120, required=False)


class PenaltyCollectionForm(forms.Form):
    reference = forms.CharField(max_length=120, label="Payment reference")


class DecisionExecutionForm(forms.Form):
    amount = forms.DecimalField(max_digits=12, decimal_places=2, min_value=0)
    entry_type = forms.ChoiceField(
        choices=(
            ("payout", "Member / emergency payout"),
            ("admin_cost", "Administrative cost"),
            ("investment", "Investment deployment"),
        )
    )
    description = forms.CharField(max_length=240)


class ExitRequestForm(forms.Form):
    confirm = forms.BooleanField(
        label="I understand that at least 30 days' written notice is required."
    )


class VoteForm(forms.Form):
    approve = forms.ChoiceField(
        choices=(("yes", "Approve"), ("no", "Reject")),
        widget=forms.RadioSelect,
    )
    comment = forms.CharField(max_length=240, required=False)


class MemberRoleForm(forms.ModelForm):
    class Meta:
        model = MemberProfile
        fields = (
            "user", "role", "status", "joined_at", "voting_suspended", "bank_signatory",
            "catch_up_amount", "catch_up_paid",
        )
