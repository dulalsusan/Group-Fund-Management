from decimal import Decimal, ROUND_HALF_UP
import math

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from .calendar_utils import (
    iter_nepali_months,
    nepali_month_offset,
    nepali_month_start,
    nepali_month_label,
    nepali_months_between,
    to_nepali_date,
)
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
)


CENT = Decimal("0.01")
ZERO = Decimal("0.00")


def money(value):
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def month_start(value):
    return nepali_month_start(value)


def month_offset(value, count):
    return nepali_month_offset(value, count)


def fund_snapshot():
    settings_record, _ = FundSettings.objects.get_or_create(pk=1)
    totals = LedgerEntry.objects.aggregate(fund=Sum("fund_delta"), cash=Sum("cash_delta"))
    total_fund = money(settings_record.opening_fund + (totals["fund"] or ZERO))
    bank_cash = money(settings_record.opening_cash + (totals["cash"] or ZERO))
    outstanding = ZERO
    for loan in LoanApplication.objects.prefetch_related("tranches", "repayments"):
        outstanding += loan.outstanding_principal
    lendable_pool = min(max(total_fund * Decimal("0.80"), ZERO), max(total_fund, ZERO))
    reserve = max(total_fund * Decimal("0.20"), ZERO)
    free_cash = min(
        max(lendable_pool - outstanding, ZERO),
        max(bank_cash - reserve, ZERO),
    )
    return {
        "total_fund": total_fund,
        "bank_cash": bank_cash,
        "reserve": money(reserve),
        "outstanding_loans": money(outstanding),
        "lendable_pool": money(lendable_pool),
        "free_cash": money(free_cash),
        "settings": settings_record,
    }


def all_months(start, end):
    yield from iter_nepali_months(start, end)


@transaction.atomic
def refresh_monthly_ledger(today=None):
    today = today or timezone.localdate()
    settings_record, _ = FundSettings.objects.get_or_create(pk=1)
    active_members = MemberProfile.objects.filter(
        role=MemberProfile.Role.MEMBER,
        status=MemberProfile.Status.ACTIVE,
        joined_at__isnull=False,
    )
    for member in active_members:
        for period in all_months(member.joined_at, today):
            MonthlyContribution.objects.get_or_create(
                member=member,
                period=period,
                defaults={"amount": settings_record.monthly_contribution},
            )

    current_nepali = to_nepali_date(today)
    for contribution in MonthlyContribution.objects.filter(
        status__in=(MonthlyContribution.Status.DUE, MonthlyContribution.Status.REJECTED),
        period__lte=month_start(today),
    ).select_related("member"):
        period_nepali = to_nepali_date(contribution.period)
        if (current_nepali.year, current_nepali.month, current_nepali.day) <= (
            period_nepali.year,
            period_nepali.month,
            20,
        ):
            continue
        months_late = (
            (current_nepali.year - period_nepali.year) * 12
            + current_nepali.month
            - period_nepali.month
            + int(current_nepali.day > 20)
        )
        for offset in range(months_late):
            penalty_month = month_offset(contribution.period, offset)
            penalty, created = ContributionPenalty.objects.get_or_create(
                contribution=contribution,
                penalty_month=penalty_month,
                defaults={"amount": Decimal("50.00")},
            )
            if created:
                LedgerEntry.objects.create(
                    entry_type=LedgerEntry.Type.PENALTY,
                    amount=penalty.amount,
                    fund_delta=penalty.amount,
                    cash_delta=ZERO,
                    member=contribution.member,
                    description=f"Late contribution penalty for {nepali_month_label(contribution.period)}",
                    reference=f"contribution:{contribution.pk}:{penalty_month:%Y-%m}",
                )


def contribution_total(member):
    total = LedgerEntry.objects.filter(
        member=member,
        entry_type__in=(LedgerEntry.Type.CONTRIBUTION, LedgerEntry.Type.CATCH_UP),
    ).aggregate(total=Sum("amount"))["total"]
    return money(total or ZERO)


def catch_up_balance(member):
    verified = member.catch_up_payments.filter(
        status=CatchUpPayment.Status.VERIFIED
    ).aggregate(total=Sum("amount"))["total"] or ZERO
    return money(max(member.catch_up_amount - verified, ZERO))


def calculate_catch_up_contribution():
    members = list(
        MemberProfile.objects.filter(
            role=MemberProfile.Role.MEMBER, status=MemberProfile.Status.ACTIVE
        ).order_by("pk")
    )
    if not members:
        return ZERO
    contributions = {
        member.pk: contribution_total(member)
        for member in members
    }
    all_contributions = sum(contributions.values(), start=ZERO)
    snapshot = fund_snapshot()
    accumulated_profit = max(snapshot["total_fund"] - all_contributions, ZERO)
    return money(
        all_contributions / Decimal(len(members))
        + accumulated_profit / Decimal(len(members))
    )


def _months_since(start, end):
    return nepali_months_between(start, end)


def _voting_members(decision):
    members = MemberProfile.objects.filter(
        role=MemberProfile.Role.MEMBER,
        status=MemberProfile.Status.ACTIVE,
        voting_suspended=False,
    ).select_related("user")
    if decision.applicant_id:
        members = members.exclude(pk=decision.applicant_id)
    current_date = timezone.localdate()
    return [
        member
        for member in members
        if member.joined_at and _months_since(member.joined_at, current_date) >= 1
    ]


def required_vote_count(decision):
    electorate_count = len(_voting_members(decision))
    if electorate_count == 0:
        return 0
    if decision.threshold == Decision.Threshold.UNANIMOUS:
        return electorate_count
    if decision.threshold == Decision.Threshold.SIMPLE_MAJORITY:
        return electorate_count // 2 + 1
    required = math.ceil(2 * electorate_count / 3)
    if decision.category in (Decision.Category.LOAN, Decision.Category.MEMBER_EXIT):
        all_active = MemberProfile.objects.filter(
            role=MemberProfile.Role.MEMBER, status=MemberProfile.Status.ACTIVE
        ).count()
        required = max(required, math.ceil(2 * all_active / 3))
    return required


@transaction.atomic
def cast_vote(decision, member, approve, comment=""):
    decision = Decision.objects.select_for_update().get(pk=decision.pk)
    if decision.status != Decision.Status.OPEN:
        raise ValueError("Voting for this decision is closed.")
    if member not in _voting_members(decision):
        raise ValueError("You are not eligible to vote on this decision.")
    vote, created = decision.votes.get_or_create(
        member=member,
        defaults={"approve": approve, "comment": comment},
    )
    if not created:
        vote.approve = approve
        vote.comment = comment
        vote.save(update_fields=("approve", "comment"))

    required = required_vote_count(decision)
    if required == 0:
        raise ValueError("There are no eligible voters for this decision.")
    approvals = decision.votes.filter(approve=True).count()
    rejections = decision.votes.filter(approve=False).count()
    electorate_count = len(_voting_members(decision))
    if approvals >= required:
        decision.status = Decision.Status.APPROVED
        decision.decided_at = timezone.now()
        decision.save(update_fields=("status", "decided_at"))
        if decision.category == Decision.Category.LOAN:
            loan = decision.loan_application
            loan.approved_amount = loan.requested_amount
            loan.status = LoanApplication.Status.APPROVED
            loan.save(update_fields=("approved_amount", "status"))
        elif decision.category == Decision.Category.MEMBER_ADDITION and decision.candidate_id:
            candidate = decision.candidate
            catch_up_amount = calculate_catch_up_contribution()
            candidate.status = MemberProfile.Status.ACTIVE
            candidate.joined_at = timezone.localdate()
            candidate.catch_up_amount = catch_up_amount
            candidate.catch_up_paid = catch_up_amount == ZERO
            candidate.save(
                update_fields=("status", "joined_at", "catch_up_amount", "catch_up_paid")
            )
        elif decision.category == Decision.Category.MEMBER_EXIT:
            exit_request = decision.exit_request
            exit_request.status = ExitRequest.Status.APPROVED
            exit_request.save(update_fields=("status",))
    elif rejections > electorate_count - required:
        decision.status = Decision.Status.REJECTED
        decision.decided_at = timezone.now()
        decision.save(update_fields=("status", "decided_at"))
        if decision.category == Decision.Category.LOAN:
            LoanApplication.objects.filter(decision=decision).update(
                status=LoanApplication.Status.REJECTED
            )
    return decision


def loan_limit(member, emergency=False):
    snapshot = fund_snapshot()
    share = Decimal("0.60") if emergency else Decimal("0.50")
    limit = snapshot["lendable_pool"] * share
    if member.joined_at and _months_since(member.joined_at, timezone.localdate()) < 6:
        limit = min(limit, contribution_total(member) * Decimal("0.80"))
    return money(limit)


def active_loan_for(member):
    for loan in LoanApplication.objects.filter(borrower=member).prefetch_related(
        "tranches", "repayments"
    ):
        if loan.outstanding_principal > ZERO:
            return loan
    return None


def loan_request_blocker(member):
    for loan in LoanApplication.objects.filter(borrower=member).prefetch_related(
        "tranches", "repayments"
    ):
        if loan.outstanding_principal > ZERO:
            return loan
        if loan.status in (LoanApplication.Status.PENDING, LoanApplication.Status.APPROVED):
            return loan
    return None


def loan_priority_key(loan):
    waiting = loan.waiting_balance > ZERO
    emergency_priority = loan.medical_emergency
    last_borrowed = (
        LoanTranche.objects.filter(loan__borrower=loan.borrower)
        .exclude(loan=loan)
        .order_by("-disbursed_at")
        .values_list("disbursed_at", flat=True)
        .first()
    )
    return (
        0 if emergency_priority else 1,
        0 if waiting else 1,
        last_borrowed or timezone.make_aware(timezone.datetime.min),
        loan.requested_at,
    )


@transaction.atomic
def disburse_loan(loan, amount, user, transaction_charge_applicable=True):
    loan = LoanApplication.objects.select_for_update().get(pk=loan.pk)
    if loan.status not in (LoanApplication.Status.APPROVED, LoanApplication.Status.DISBURSED):
        raise ValueError("Only an approved loan can be disbursed.")
    if loan.decision.status not in (Decision.Status.APPROVED, Decision.Status.EXECUTED):
        raise ValueError("The required member approval has not been recorded.")
    try:
        executor_profile = user.member_profile
    except MemberProfile.DoesNotExist:
        executor_profile = None
    if not user.is_superuser and (
        not executor_profile or not executor_profile.is_active_member
    ):
        raise ValueError("Disbursements must be recorded by an active member or Admin.")
    if not loan.decision.second_signatory_id:
        raise ValueError("A second designated bank signatory must confirm the transfer.")
    if executor_profile and loan.decision.second_signatory_id == executor_profile.pk:
        raise ValueError("The second bank signatory must be different from the disbursing user.")
    if (
        not loan.decision.second_signatory.bank_signatory
        or not loan.decision.second_signatory.is_active_member
    ):
        raise ValueError("The recorded second signatory is not an active designated bank signatory.")
    amount = money(amount)
    if amount <= ZERO or amount > loan.waiting_balance:
        raise ValueError("Disbursement must be positive and cannot exceed the approved balance.")
    eligible_balance = max(loan_limit(loan.borrower, loan.emergency) - loan.principal_disbursed, ZERO)
    if amount > eligible_balance:
        raise ValueError("This disbursement exceeds the borrower's current loan limit.")
    snapshot = fund_snapshot()
    if amount > snapshot["free_cash"]:
        raise ValueError("This disbursement would breach the reserve or lendable-pool limit.")
    service_charge = Decimal("100.00")
    transaction_charge = Decimal("10.00") if transaction_charge_applicable else ZERO
    if amount <= service_charge + transaction_charge:
        raise ValueError("The disbursement must be greater than its deductions.")
    tranche = LoanTranche.objects.create(
        loan=loan,
        principal=amount,
        service_charge=service_charge,
        transaction_charge=transaction_charge,
        transaction_charge_applicable=transaction_charge_applicable,
        disbursed_by=user,
    )
    LedgerEntry.objects.create(
        entry_type=LedgerEntry.Type.LOAN_DISBURSEMENT,
        amount=tranche.net_paid,
        cash_delta=-tranche.net_paid,
        member=loan.borrower,
        description=f"Loan disbursement to {loan.borrower}",
        reference=f"loan:{loan.pk}:tranche:{tranche.pk}",
        created_by=user,
    )
    fees = service_charge + transaction_charge
    if fees:
        LedgerEntry.objects.create(
            entry_type=LedgerEntry.Type.FEE,
            amount=fees,
            fund_delta=fees,
            member=loan.borrower,
            description="Loan service and transaction charges",
            reference=f"loan:{loan.pk}:tranche:{tranche.pk}:fees",
            created_by=user,
        )
    loan.status = LoanApplication.Status.DISBURSED
    loan.save(update_fields=("status",))
    return tranche


def monthly_interest_due(loan, today=None):
    today = today or timezone.localdate()
    tranches = list(loan.tranches.order_by("disbursed_at"))
    if not tranches or loan.outstanding_principal <= ZERO:
        return ZERO
    first_date = timezone.localtime(tranches[0].disbursed_at).date()
    last_event = loan.repayments.order_by("-paid_at").values_list("paid_at", flat=True).first()
    accrual_start = timezone.localtime(last_event).date() if last_event else first_date
    completed_months = _months_since(first_date, today)
    repayments = [
        (timezone.localtime(item.paid_at).date(), item.principal)
        for item in loan.repayments.order_by("paid_at")
    ]
    interest = ZERO
    for month_number in range(1, completed_months + 1):
        due_date = month_offset(first_date, month_number)
        if due_date <= accrual_start or due_date > today:
            continue
        principal_disbursed = sum(
            (
                tranche.principal
                for tranche in tranches
                if timezone.localtime(tranche.disbursed_at).date() <= due_date
            ),
            start=ZERO,
        )
        principal_repaid = sum(
            (principal for paid_date, principal in repayments if paid_date < due_date),
            start=ZERO,
        )
        balance = max(principal_disbursed - principal_repaid, ZERO)
        if month_number <= 13:
            monthly_rate = Decimal("0.005")
        else:
            annual_rate = Decimal("0.08") + Decimal(
                max(0, (month_number - 13) // 12)
            ) / 100
            monthly_rate = annual_rate / 12
        interest += balance * monthly_rate
    return money(interest)


@transaction.atomic
def record_loan_repayment(loan, principal, user, reference=""):
    loan = LoanApplication.objects.select_for_update().get(pk=loan.pk)
    principal = money(principal)
    outstanding = loan.outstanding_principal
    if principal <= ZERO or principal > outstanding:
        raise ValueError("Repayment principal must be positive and no greater than the balance.")
    interest = monthly_interest_due(loan)
    repayment = LoanRepayment.objects.create(
        loan=loan,
        principal=principal,
        interest=interest,
        recorded_by=user,
        reference=reference,
    )
    LedgerEntry.objects.create(
        entry_type=LedgerEntry.Type.PRINCIPAL_REPAYMENT,
        amount=principal,
        cash_delta=principal,
        member=loan.borrower,
        description=f"Loan principal repayment from {loan.borrower}",
        reference=reference or f"loan:{loan.pk}:repayment:{repayment.pk}",
        created_by=user,
    )
    if interest:
        LedgerEntry.objects.create(
            entry_type=LedgerEntry.Type.INTEREST,
            amount=interest,
            fund_delta=interest,
            cash_delta=interest,
            member=loan.borrower,
            description=f"Loan interest from {loan.borrower}",
            reference=reference or f"loan:{loan.pk}:interest:{repayment.pk}",
            created_by=user,
        )
    if loan.outstanding_principal <= ZERO:
        loan.status = LoanApplication.Status.REPAID
        loan.save(update_fields=("status",))
    return repayment


@transaction.atomic
def record_contribution_verification(contribution, user):
    contribution = MonthlyContribution.objects.select_for_update().get(pk=contribution.pk)
    if contribution.status == MonthlyContribution.Status.VERIFIED:
        raise ValueError("This contribution has already been verified.")
    contribution.status = MonthlyContribution.Status.VERIFIED
    contribution.verified_at = timezone.now()
    contribution.verified_by = user
    contribution.save(update_fields=("status", "verified_at", "verified_by"))
    LedgerEntry.objects.create(
        entry_type=LedgerEntry.Type.CONTRIBUTION,
        amount=contribution.amount,
        fund_delta=contribution.amount,
        cash_delta=contribution.amount,
        member=contribution.member,
        description=f"Contribution for {contribution.period:%B %Y}",
        reference=f"contribution:{contribution.pk}",
        created_by=user,
    )
    return contribution


@transaction.atomic
def record_catch_up_verification(payment, user):
    payment = CatchUpPayment.objects.select_for_update().get(pk=payment.pk)
    if payment.status != CatchUpPayment.Status.SUBMITTED:
        raise ValueError("Only a submitted catch-up proof can be verified.")
    payment.status = CatchUpPayment.Status.VERIFIED
    payment.verified_by = user
    payment.save(update_fields=("status", "verified_by"))
    LedgerEntry.objects.create(
        entry_type=LedgerEntry.Type.CATCH_UP,
        amount=payment.amount,
        fund_delta=payment.amount,
        cash_delta=payment.amount,
        member=payment.member,
        description="New-member catch-up contribution",
        reference=f"catch-up:{payment.pk}",
        created_by=user,
    )
    verified_total = payment.member.catch_up_payments.filter(
        status=CatchUpPayment.Status.VERIFIED
    ).aggregate(total=Sum("amount"))["total"] or ZERO
    if verified_total >= payment.member.catch_up_amount:
        payment.member.catch_up_paid = True
        payment.member.save(update_fields=("catch_up_paid",))
    return payment


@transaction.atomic
def collect_contribution_penalty(penalty, user, reference=""):
    penalty = ContributionPenalty.objects.select_for_update().get(pk=penalty.pk)
    if penalty.paid_at:
        raise ValueError("This late penalty has already been marked as paid.")
    penalty.paid_at = timezone.now()
    penalty.paid_by = user
    penalty.payment_reference = reference
    penalty.save(update_fields=("paid_at", "paid_by", "payment_reference"))
    LedgerEntry.objects.create(
        entry_type=LedgerEntry.Type.PENALTY,
        amount=penalty.amount,
        cash_delta=penalty.amount,
        member=penalty.contribution.member,
        description="Collection of an assessed late contribution penalty",
        reference=reference or f"contribution-penalty:{penalty.pk}:collection",
        created_by=user,
    )
    return penalty


def member_exit_settlement(member, today=None):
    today = today or timezone.localdate()
    contributions = contribution_total(member)
    all_contributions = LedgerEntry.objects.filter(
        entry_type__in=(LedgerEntry.Type.CONTRIBUTION, LedgerEntry.Type.CATCH_UP)
    ).aggregate(total=Sum("amount"))["total"] or ZERO
    profit = max(fund_snapshot()["total_fund"] - all_contributions, ZERO)
    attributable_profit = (
        profit * contributions / all_contributions if all_contributions else ZERO
    )
    tenure_months = _months_since(member.joined_at, today) if member.joined_at else 0
    profit_share = ZERO
    if tenure_months >= 36:
        profit_share = money(attributable_profit * Decimal("0.80"))
    gross = money(contributions + profit_share)
    outstanding_loan = active_loan_for(member)
    loan_principal = outstanding_loan.outstanding_principal if outstanding_loan else ZERO
    loan_interest = monthly_interest_due(outstanding_loan, today) if outstanding_loan else ZERO
    unpaid_penalties = ContributionPenalty.objects.filter(
        contribution__member=member,
        paid_at__isnull=True,
    ).aggregate(total=Sum("amount"))["total"] or ZERO
    return {
        "contributions": contributions,
        "profit_share": money(profit_share),
        "gross": gross,
        "loan_principal": money(loan_principal),
        "loan_interest": money(loan_interest),
        "penalties": money(unpaid_penalties),
        "payout": money(max(gross - loan_principal - loan_interest - unpaid_penalties, ZERO)),
    }


def member_exit_payout(member, today=None):
    return member_exit_settlement(member, today)["payout"]
