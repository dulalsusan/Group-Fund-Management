from django.contrib import admin

from .forms import MemberRoleForm
from .models import (
    CatchUpPayment,
    ContributionPenalty,
    Decision,
    ExitRequest,
    FundSettings,
    LedgerEntry,
    LoanApplication,
    LoanRepayment,
    LoanTranche,
    MemberProfile,
    MonthlyContribution,
    ReminderLog,
    SharedDocument,
    Vote,
)


@admin.register(MemberProfile)
class MemberProfileAdmin(admin.ModelAdmin):
    form = MemberRoleForm
    list_display = ("user", "role", "status", "joined_at", "catch_up_paid", "voting_suspended")
    list_filter = ("role", "status", "voting_suspended")
    search_fields = ("user__username", "user__first_name", "user__last_name")


@admin.register(FundSettings)
class FundSettingsAdmin(admin.ModelAdmin):
    list_display = ("opening_fund", "opening_cash", "monthly_contribution", "bank_account_label")

    def has_add_permission(self, request):
        return not FundSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(LedgerEntry)
class LedgerEntryAdmin(admin.ModelAdmin):
    list_display = ("created_at", "entry_type", "member", "amount", "fund_delta", "cash_delta", "created_by")
    list_filter = ("entry_type", "created_at")
    search_fields = ("description", "reference", "member__user__username")
    readonly_fields = (
        "entry_type", "amount", "fund_delta", "cash_delta", "member",
        "description", "reference", "created_by", "created_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(MonthlyContribution)
class MonthlyContributionAdmin(admin.ModelAdmin):
    list_display = ("member", "period", "amount", "status", "submitted_at", "verified_by")
    list_filter = ("status", "period")


@admin.register(Decision)
class DecisionAdmin(admin.ModelAdmin):
    list_display = ("title", "category", "status", "threshold", "requested_by", "created_at")
    list_filter = ("category", "status", "threshold")
    search_fields = ("title", "description")


@admin.register(LoanApplication)
class LoanApplicationAdmin(admin.ModelAdmin):
    list_display = ("borrower", "requested_amount", "approved_amount", "status", "requested_at")
    list_filter = ("status", "emergency", "medical_emergency")


admin.site.register(
    (
        CatchUpPayment,
        ContributionPenalty,
        ExitRequest,
        LoanRepayment,
        LoanTranche,
        ReminderLog,
        SharedDocument,
        Vote,
    )
)
