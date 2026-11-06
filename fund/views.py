from datetime import timedelta
from decimal import Decimal
from functools import wraps

from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.conf import settings
from django.db import transaction
from django.db.models import Sum
from django.core.exceptions import PermissionDenied
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .forms import (
    CatchUpPaymentForm,
    ContributionProofForm,
    DecisionExecutionForm,
    DecisionForm,
    DisbursementForm,
    ExitRequestForm,
    LoanRequestForm,
    PenaltyCollectionForm,
    RepaymentForm,
    SharedDocumentForm,
    SignUpForm,
    VoteForm,
)
from .calendar_utils import nepali_month_start
from .models import (
    CatchUpPayment,
    ContributionPenalty,
    Decision,
    ExitRequest,
    LedgerEntry,
    LoanApplication,
    MemberProfile,
    MonthlyContribution,
    SharedDocument,
)
from .services import (
    ZERO,
    active_loan_for,
    catch_up_balance,
    cast_vote,
    collect_contribution_penalty,
    contribution_total,
    disburse_loan,
    fund_snapshot,
    loan_limit,
    loan_request_blocker,
    loan_priority_key,
    member_exit_payout,
    monthly_interest_due,
    member_exit_settlement,
    record_catch_up_verification,
    record_contribution_verification,
    record_loan_repayment,
    refresh_monthly_ledger,
)


def current_member(user):
    try:
        profile = user.member_profile
    except MemberProfile.DoesNotExist:
        return None
    return profile


def active_member_required(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        member = current_member(request.user)
        if request.user.is_superuser:
            if not member or member.role != MemberProfile.Role.ADMIN:
                return redirect("admin:index")
            request.member_profile = member
            return view(request, *args, **kwargs)
        if member and member.role == MemberProfile.Role.CLIENT:
            return redirect("client_overview")
        if not member or not member.is_active_member:
            return redirect("pending")
        request.member_profile = member
        return view(request, *args, **kwargs)

    return login_required(wrapped)


def member_only(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if request.member_profile.role != MemberProfile.Role.MEMBER:
            raise PermissionDenied
        return view(request, *args, **kwargs)

    return wrapped


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapped(request, *args, **kwargs):
        if not request.user.is_superuser:
            raise PermissionDenied
        return view(request, *args, **kwargs)

    return wrapped


@login_required
def post_login(request):
    member = current_member(request.user)
    if request.user.is_superuser:
        if member and member.role == MemberProfile.Role.ADMIN:
            return redirect("dashboard")
        return redirect("admin:index")
    if member and member.role == MemberProfile.Role.CLIENT:
        return redirect("client_overview")
    if member and member.is_active_member:
        return redirect("dashboard")
    return redirect("pending")


@login_required
def client_overview(request):
    member = current_member(request.user)
    if request.user.is_superuser:
        return post_login(request)
    if not member or member.role != MemberProfile.Role.CLIENT:
        return post_login(request)
    return render(
        request,
        "registration/client_overview.html",
        {"contact_phone": "9818123456"},
    )


def _decision_threshold(category):
    if category in (
        Decision.Category.EMERGENCY_PAYOUT,
        Decision.Category.INVESTMENT,
        Decision.Category.MEMBER_ADDITION,
    ):
        return Decision.Threshold.UNANIMOUS
    return Decision.Threshold.TWO_THIRDS


def register(request):
    if request.user.is_authenticated:
        return post_login(request)
    form = SignUpForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            user = form.save()
            MemberProfile.objects.create(user=user)
            messages.info(request, "Your membership request is registered. An administrator must approve it.")
        login(request, user)
        return redirect("pending")
    return render(request, "registration/register.html", {"form": form})


@login_required
def pending(request):
    profile = current_member(request.user)
    if request.user.is_superuser:
        return post_login(request)
    if profile and profile.role == MemberProfile.Role.CLIENT:
        return redirect("client_overview")
    if profile and profile.is_active_member:
        return redirect("dashboard")
    return render(
        request,
        "registration/pending.html",
        {"profile": profile},
    )


@active_member_required
def dashboard(request):
    refresh_monthly_ledger()
    snapshot = fund_snapshot()
    member = request.member_profile
    today = timezone.localdate()
    current_period = nepali_month_start(today)
    contribution = MonthlyContribution.objects.filter(member=member, period=current_period).first()
    current_statement = SharedDocument.objects.filter(
        category=SharedDocument.Category.BANK_STATEMENT,
        reporting_period=current_period,
    ).exists()
    member_loan = active_loan_for(member)
    pending_contributions = MonthlyContribution.objects.filter(
        status=MonthlyContribution.Status.SUBMITTED
    ).count()
    open_decisions = Decision.objects.filter(status=Decision.Status.OPEN).count()
    latest_entries = LedgerEntry.objects.select_related("member__user", "created_by")[:6]
    return render(
        request,
        "fund/dashboard.html",
        {
            "snapshot": snapshot,
            "member": member,
            "contribution": contribution,
            "member_loan": member_loan,
            "current_period": current_period,
            "bank_statement_missing": today.day >= 10 and not current_statement,
            "pending_contributions": pending_contributions,
            "open_decisions": open_decisions,
            "latest_entries": latest_entries,
            "catch_up_due": catch_up_balance(member),
        },
    )


@active_member_required
def ledger(request):
    entries = LedgerEntry.objects.select_related("member__user", "created_by")
    return render(request, "fund/ledger.html", {"entries": entries[:250]})


@active_member_required
def contributions(request):
    refresh_monthly_ledger()
    member = request.member_profile
    records = MonthlyContribution.objects.select_related("member__user")
    if not request.user.is_superuser:
        records = records.filter(member=member)
    catch_up_payments = CatchUpPayment.objects.select_related("member__user")
    penalties = ContributionPenalty.objects.select_related(
        "contribution__member__user", "paid_by"
    ).order_by("-charged_at")
    if not request.user.is_superuser:
        catch_up_payments = catch_up_payments.filter(member=member)
        penalties = penalties.filter(contribution__member=member)
    return render(
        request,
        "fund/contributions.html",
        {
            "contributions": records[:240],
            "catch_up_payments": catch_up_payments[:100],
            "penalties": penalties[:240],
            "can_verify": request.user.is_superuser,
            "proof_form": ContributionProofForm(),
            "catch_up_form": CatchUpPaymentForm(),
        },
    )


@active_member_required
@member_only
@require_POST
def submit_contribution_proof(request, contribution_id):
    contribution = get_object_or_404(
        MonthlyContribution, pk=contribution_id, member=request.member_profile
    )
    if contribution.status == MonthlyContribution.Status.VERIFIED:
        messages.error(request, "This contribution has already been verified.")
        return redirect("contributions")
    form = ContributionProofForm(request.POST, request.FILES, instance=contribution)
    if form.is_valid():
        contribution = form.save(commit=False)
        contribution.status = MonthlyContribution.Status.SUBMITTED
        contribution.submitted_at = timezone.now()
        contribution.save()
        messages.success(request, "Payment proof submitted for review.")
    else:
        messages.error(request, "Please provide a valid screenshot or transaction reference.")
    return redirect("contributions")


@active_member_required
def contribution_receipt(request, contribution_id):
    contribution = get_object_or_404(
        MonthlyContribution.objects.select_related("member__user", "verified_by"),
        pk=contribution_id,
        status=MonthlyContribution.Status.VERIFIED,
    )
    member = request.member_profile
    if not request.user.is_superuser and contribution.member_id != member.pk:
        raise Http404
    return render(request, "fund/receipt.html", {"contribution": contribution})


@active_member_required
@member_only
@require_POST
def submit_catch_up_proof(request):
    member = request.member_profile
    if member.catch_up_paid or member.catch_up_amount <= ZERO:
        messages.error(request, "No catch-up payment is currently due.")
        return redirect("contributions")
    form = CatchUpPaymentForm(request.POST, request.FILES)
    if form.is_valid():
        pending_amount = sum(
            (
                payment.amount
                for payment in member.catch_up_payments.filter(
                    status=CatchUpPayment.Status.SUBMITTED
                )
            ),
            start=ZERO,
        )
        amount_due = max(catch_up_balance(member) - pending_amount, ZERO)
        if amount_due <= ZERO:
            messages.info(request, "The full catch-up amount is already submitted or verified.")
            return redirect("contributions")
        payment = form.save(commit=False)
        payment.member = member
        payment.amount = amount_due
        payment.save()
        messages.success(request, "Catch-up payment proof submitted for review.")
    else:
        messages.error(request, "Please provide a valid screenshot or transaction reference.")
    return redirect("contributions")


@admin_required
@require_POST
def verify_contribution(request, contribution_id):
    contribution = get_object_or_404(MonthlyContribution, pk=contribution_id)
    try:
        record_contribution_verification(contribution, request.user)
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Contribution verified and added to the shared ledger.")
    return redirect("contributions")


@admin_required
@require_POST
def reject_contribution(request, contribution_id):
    contribution = get_object_or_404(
        MonthlyContribution, pk=contribution_id, status=MonthlyContribution.Status.SUBMITTED
    )
    contribution.status = MonthlyContribution.Status.REJECTED
    contribution.save(update_fields=("status",))
    messages.info(request, "Payment proof rejected. The member can submit a corrected proof.")
    return redirect("contributions")


@admin_required
@require_POST
def collect_penalty(request, penalty_id):
    penalty = get_object_or_404(
        ContributionPenalty, pk=penalty_id, paid_at__isnull=True
    )
    form = PenaltyCollectionForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Enter the payment reference for this penalty.")
    else:
        try:
            collect_contribution_penalty(
                penalty, request.user, form.cleaned_data["reference"]
            )
        except ValueError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, "Late penalty collection recorded in the ledger.")
    return redirect("contributions")


@admin_required
@require_POST
def verify_catch_up_payment(request, payment_id):
    payment = get_object_or_404(CatchUpPayment, pk=payment_id)
    try:
        record_catch_up_verification(payment, request.user)
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Catch-up contribution verified and recorded.")
    return redirect("contributions")


@active_member_required
def loans(request):
    records = list(
        LoanApplication.objects.select_related("borrower__user", "decision")
        .prefetch_related("tranches", "repayments")
        .all()
    )
    records.sort(key=loan_priority_key)
    return render(
        request,
        "fund/loans.html",
        {
            "loans": records,
            "snapshot": fund_snapshot(),
            "member_loan": active_loan_for(request.member_profile),
        },
    )


@active_member_required
@member_only
def request_loan(request):
    member = request.member_profile
    form = LoanRequestForm(request.POST or None, member=member)
    if request.method == "POST" and form.is_valid():
        if loan_request_blocker(member):
            form.add_error(None, "You already have an active loan or an open loan request.")
        elif not member.catch_up_paid:
            form.add_error(None, "Your approved new-member catch-up contribution must be paid first.")
        else:
            amount = form.cleaned_data["amount"]
            emergency = form.cleaned_data["emergency"]
            medical = form.cleaned_data["medical_emergency"]
            with transaction.atomic():
                decision = Decision.objects.create(
                    title=f"Loan request: {member}",
                    category=Decision.Category.LOAN,
                    description=(
                        f"{form.cleaned_data['reason']}\n\n"
                        f"Affordability declared: monthly income NPR "
                        f"{form.cleaned_data['monthly_income']:,.2f}; monthly expenses NPR "
                        f"{form.cleaned_data['monthly_expenses']:,.2f}."
                    ),
                    amount=amount,
                    threshold=Decision.Threshold.TWO_THIRDS,
                    requested_by=member,
                    applicant=member,
                )
                loan = LoanApplication.objects.create(
                    borrower=member,
                    decision=decision,
                    requested_amount=amount,
                    emergency=emergency,
                    medical_emergency=medical,
                    guarantor_details=form.cleaned_data["guarantor_details"],
                    monthly_income=form.cleaned_data["monthly_income"],
                    monthly_expenses=form.cleaned_data["monthly_expenses"],
                )
            messages.success(request, "Loan request submitted for member voting.")
            return redirect("decision_detail", decision_id=decision.pk)
    return render(request, "fund/loan_request.html", {"form": form})


@admin_required
@require_POST
def disburse(request, loan_id):
    loan = get_object_or_404(LoanApplication, pk=loan_id)
    form = DisbursementForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Enter a valid disbursement amount.")
    else:
        try:
            disburse_loan(
                loan,
                form.cleaned_data["amount"],
                request.user,
                form.cleaned_data["transaction_charge_applicable"],
            )
        except ValueError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, "Loan disbursement recorded in the ledger.")
    return redirect("loans")


@admin_required
@require_POST
def repay_loan(request, loan_id):
    loan = get_object_or_404(LoanApplication, pk=loan_id)
    form = RepaymentForm(request.POST)
    if form.is_valid():
        try:
            repayment = record_loan_repayment(
                loan,
                form.cleaned_data["principal"],
                request.user,
                form.cleaned_data["reference"],
            )
        except ValueError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(
                request,
                f"Repayment recorded; NPR {repayment.interest:,.2f} interest was added.",
            )
    else:
        messages.error(request, "Enter a valid repayment amount.")
    return redirect("loans")


@active_member_required
def decisions(request):
    form = DecisionForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        category = form.cleaned_data["category"]
        Decision.objects.create(
            title=form.cleaned_data["title"],
            category=category,
            description=form.cleaned_data["description"],
            amount=form.cleaned_data["amount"],
            threshold=_decision_threshold(category),
            requested_by=request.member_profile,
        )
        messages.success(request, "Decision submitted for member voting.")
        return redirect("decisions")
    decision_list = Decision.objects.select_related(
        "requested_by__user", "applicant__user", "candidate__user"
    ).prefetch_related("votes")
    return render(
        request,
        "fund/decisions.html",
        {"form": form, "decisions": decision_list, "member": request.member_profile},
    )


@active_member_required
def decision_detail(request, decision_id):
    decision = get_object_or_404(
        Decision.objects.select_related("requested_by__user", "applicant__user", "candidate__user"),
        pk=decision_id,
    )
    form = VoteForm()
    execution_form = DecisionExecutionForm()
    return render(
        request,
        "fund/decision_detail.html",
        {
            "decision": decision,
            "form": form,
            "execution_form": execution_form,
            "member": request.member_profile,
            "existing_vote": decision.votes.filter(member=request.member_profile).first(),
        },
    )


@active_member_required
@member_only
@require_POST
def vote(request, decision_id):
    decision = get_object_or_404(Decision, pk=decision_id)
    form = VoteForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Select approve or reject.")
        return redirect("decision_detail", decision_id=decision.pk)
    try:
        updated = cast_vote(
            decision,
            request.member_profile,
            form.cleaned_data["approve"] == "yes",
            form.cleaned_data["comment"],
        )
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"Vote recorded. Decision status: {updated.get_status_display()}.")
    return redirect("decision_detail", decision_id=decision.pk)


@active_member_required
@member_only
@require_POST
def second_signoff(request, decision_id):
    decision = get_object_or_404(Decision, pk=decision_id)
    member = request.member_profile
    if decision.status != Decision.Status.APPROVED:
        messages.error(request, "Only an approved transaction can be signed.")
    elif not member.bank_signatory:
        messages.error(request, "Your account is not a designated bank signatory.")
    elif not member.is_active_member:
        messages.error(request, "Only active members may sign a transfer.")
    elif decision.requested_by_id == member.pk:
        messages.error(request, "The proposer cannot also act as the second bank signatory.")
    else:
        decision.second_signatory = member
        decision.save(update_fields=("second_signatory",))
        messages.success(request, "Second bank signatory approval recorded.")
    return redirect("decision_detail", decision_id=decision.pk)


@admin_required
@require_POST
def execute_decision(request, decision_id):
    decision = get_object_or_404(Decision, pk=decision_id)
    form = DecisionExecutionForm(request.POST)
    if decision.status != Decision.Status.APPROVED:
        messages.error(request, "Only an approved decision can be executed.")
    elif not form.is_valid():
        messages.error(request, "Enter a valid transaction.")
    else:
        amount = form.cleaned_data["amount"]
        entry_type = form.cleaned_data["entry_type"]
        category_type = {
            Decision.Category.EMERGENCY_PAYOUT: "payout",
            Decision.Category.MEMBER_EXIT: "payout",
            Decision.Category.INVESTMENT: "investment",
        }.get(decision.category)
        allowed_types = {
            Decision.Category.EMERGENCY_PAYOUT: {"payout"},
            Decision.Category.MEMBER_EXIT: {"payout"},
            Decision.Category.INVESTMENT: {"investment"},
            Decision.Category.ADMINISTRATIVE: {"admin_cost"},
            Decision.Category.OTHER: {"payout", "admin_cost", "investment"},
        }
        snapshot = fund_snapshot()
        amount_allowed = decision.amount is None or amount <= decision.amount
        if decision.category == Decision.Category.MEMBER_EXIT and decision.exit_request_id:
            settlement = member_exit_settlement(decision.exit_request.member)
            amount_allowed = amount == settlement["payout"] == decision.amount
        if entry_type != "payout" and amount <= ZERO:
            amount_allowed = False
        if (
            entry_type not in allowed_types.get(decision.category, set())
            or (category_type and entry_type != category_type)
            or not amount_allowed
            or (decision.amount is not None and amount != decision.amount)
            or amount > max(snapshot["bank_cash"] - snapshot["reserve"], ZERO)
            or not decision.second_signatory_id
            or (
                current_member(request.user)
                and decision.second_signatory_id == current_member(request.user).pk
            )
        ):
            messages.error(request, "This transaction needs a distinct second bank signatory and must match the approved amount and reserve-safe cash.")
        else:
            with transaction.atomic():
                entry_types = {
                    "payout": LedgerEntry.Type.PAYOUT,
                    "admin_cost": LedgerEntry.Type.ADMIN_COST,
                    "investment": LedgerEntry.Type.INVESTMENT,
                }
                fund_delta = ZERO if entry_type == "investment" else -amount
                member = decision.applicant or decision.candidate
                entry_amount = amount
                cash_amount = amount
                if decision.category == Decision.Category.MEMBER_EXIT:
                    settlement = member_exit_settlement(decision.exit_request.member)
                    entry_amount = settlement["gross"]
                    cash_amount = settlement["payout"]
                    fund_delta = -settlement["gross"]
                    loan = active_loan_for(decision.exit_request.member)
                    if loan and settlement["loan_principal"]:
                        from .models import LoanRepayment

                        LoanRepayment.objects.create(
                            loan=loan,
                            principal=settlement["loan_principal"],
                            interest=settlement["loan_interest"],
                            recorded_by=request.user,
                            reference=f"exit:{decision.exit_request_id}:loan-setoff",
                        )
                        LedgerEntry.objects.create(
                            entry_type=LedgerEntry.Type.PRINCIPAL_REPAYMENT,
                            amount=settlement["loan_principal"],
                            member=decision.exit_request.member,
                            description="Loan principal settled from member exit balance",
                            reference=f"exit:{decision.exit_request_id}:principal",
                            created_by=request.user,
                        )
                        loan.status = LoanApplication.Status.REPAID
                        loan.save(update_fields=("status",))
                    if settlement["loan_interest"]:
                        LedgerEntry.objects.create(
                            entry_type=LedgerEntry.Type.INTEREST,
                            amount=settlement["loan_interest"],
                            fund_delta=settlement["loan_interest"],
                            member=decision.exit_request.member,
                            description="Accrued loan interest settled from member exit balance",
                            reference=f"exit:{decision.exit_request_id}:interest",
                            created_by=request.user,
                        )
                entry = None
                if entry_amount > ZERO or cash_amount > ZERO:
                    entry = LedgerEntry.objects.create(
                        entry_type=entry_types[entry_type],
                        amount=entry_amount,
                        fund_delta=fund_delta,
                        cash_delta=-cash_amount,
                        member=member,
                        description=form.cleaned_data["description"],
                        reference=f"decision:{decision.pk}",
                        created_by=request.user,
                    )
                decision.status = Decision.Status.EXECUTED
                decision.executed_by = request.user
                decision.save(update_fields=("status", "executed_by"))
                if decision.category == Decision.Category.MEMBER_EXIT:
                    exit_request = decision.exit_request
                    exit_request.status = ExitRequest.Status.PAID
                    exit_request.save(update_fields=("status",))
                    exit_request.member.status = MemberProfile.Status.EXITED
                    exit_request.member.save(update_fields=("status",))
            if entry:
                messages.success(request, f"Transaction #{entry.pk} was recorded.")
            else:
                messages.success(request, "The zero-balance exit was completed.")
    return redirect("decision_detail", decision_id=decision.pk)


@active_member_required
def documents(request):
    member = request.member_profile
    form = SharedDocumentForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        category = form.cleaned_data["category"]
        permitted = {
            SharedDocument.Category.BANK_STATEMENT: False,
            SharedDocument.Category.MONTHLY_SUMMARY: False,
            SharedDocument.Category.RECONCILIATION: False,
            SharedDocument.Category.MEETING_MINUTES: True,
            SharedDocument.Category.RULEBOOK: False,
            SharedDocument.Category.OTHER: False,
        }
        if request.user.is_superuser:
            permitted = dict.fromkeys(permitted, True)
        if not permitted.get(category, False):
            form.add_error("category", "Your role cannot upload this document category.")
        else:
            document = form.save(commit=False)
            document.uploaded_by = request.user
            document.save()
            messages.success(request, "Document uploaded and shared with group members.")
            return redirect("documents")
    return render(
        request,
        "fund/documents.html",
        {
            "form": form,
            "documents": SharedDocument.objects.select_related("uploaded_by")[:100],
            "member": member,
        },
    )


@active_member_required
def private_file(request, kind, object_id):
    member = request.member_profile
    if kind == "shared":
        record = get_object_or_404(SharedDocument, pk=object_id)
        uploaded_file = record.file
    elif kind == "contribution":
        record = get_object_or_404(MonthlyContribution, pk=object_id)
        if not request.user.is_superuser and record.member_id != member.pk:
            raise Http404
        uploaded_file = record.proof
    elif kind == "catchup":
        record = get_object_or_404(CatchUpPayment, pk=object_id)
        if not request.user.is_superuser and record.member_id != member.pk:
            raise Http404
        uploaded_file = record.proof
    else:
        raise Http404
    if not uploaded_file:
        raise Http404
    return FileResponse(uploaded_file.open("rb"), as_attachment=False)


@active_member_required
def rulebook(request):
    path = settings.BASE_DIR / "Group Savings Fund Rule Book.pdf"
    if not path.is_file():
        raise Http404
    return FileResponse(path.open("rb"), as_attachment=False, filename=path.name)


@active_member_required
@member_only
def exit_request(request):
    member = request.member_profile
    existing = ExitRequest.objects.filter(member=member).exclude(
        status__in=(ExitRequest.Status.PAID, ExitRequest.Status.CANCELLED)
    ).first()
    form = ExitRequestForm(request.POST or None)
    if request.method == "POST" and form.is_valid() and not existing:
        ExitRequest.objects.create(member=member)
        messages.success(request, "Your 30-day exit notice has been recorded.")
        return redirect("exit_request")
    payout = member_exit_payout(member)
    notice_ready = existing and timezone.localdate() >= existing.notice_date + timedelta(days=30)
    if request.method == "POST" and existing and notice_ready and existing.status == ExitRequest.Status.NOTICE:
        loan = active_loan_for(member)
        if loan and contribution_total(member) < loan.outstanding_principal + monthly_interest_due(loan):
            messages.error(request, "Your recorded contributions must cover the active loan, accrued interest, and penalties before exit.")
            return redirect("exit_request")
        with transaction.atomic():
            decision = Decision.objects.create(
                title=f"Exit payout for {member}",
                category=Decision.Category.MEMBER_EXIT,
                description=f"Member exit payout after written notice. Calculated payout: NPR {payout:,.2f}.",
                amount=payout,
                threshold=Decision.Threshold.TWO_THIRDS,
                requested_by=member,
                applicant=member,
            )
            existing.decision = decision
            existing.status = ExitRequest.Status.PENDING
            existing.save(update_fields=("decision", "status"))
        messages.success(request, "Your exit payout request is now open for a two-thirds vote.")
        return redirect("decision_detail", decision_id=decision.pk)
    return render(
        request,
        "fund/exit.html",
        {"form": form, "exit_request": existing, "payout": payout, "notice_ready": notice_ready},
    )


def not_found(request, exception):
    return render(request, "404.html", status=404)
