from django import template
from django.utils import timezone

from fund.calendar_utils import nepali_month_label, to_nepali_date
from fund.services import monthly_interest_due

register = template.Library()


@register.filter
def interest_due(loan):
    return monthly_interest_due(loan)


@register.filter
def bs_month(value):
    if not value:
        return ""
    return nepali_month_label(value)


@register.filter
def bs_date(value):
    if not value:
        return ""
    if hasattr(value, "date"):
        value = timezone.localtime(value).date()
    return to_nepali_date(value).strftime("%d %B %Y")


@register.filter
def bs_datetime(value):
    if not value:
        return ""
    local_value = timezone.localtime(value)
    return f"{to_nepali_date(local_value.date()).strftime('%d %B %Y')} · {local_value.strftime('%I:%M %p')}"
