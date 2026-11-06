from datetime import date

import nepali_datetime


NepaliDate = nepali_datetime.date


def to_nepali_date(value):
    return NepaliDate.from_datetime_date(value)


def from_nepali_date(year, month, day):
    return NepaliDate(year, month, day).to_datetime_date()


def nepali_month_start(value):
    current = to_nepali_date(value)
    return from_nepali_date(current.year, current.month, 1)


def nepali_period(year, month):
    return from_nepali_date(year, month, 1)


def nepali_month_offset(value, count):
    current = to_nepali_date(value)
    month_index = current.year * 12 + current.month - 1 + count
    year, month = divmod(month_index, 12)
    year, month = year, month + 1
    day = current.day
    while day:
        try:
            return from_nepali_date(year, month, day)
        except ValueError:
            day -= 1
    raise ValueError(f"No valid Bikram Sambat date for {year}/{month}.")


def nepali_months_between(start, end):
    first = to_nepali_date(start)
    second = to_nepali_date(end)
    return max(
        0,
        (second.year - first.year) * 12
        + second.month
        - first.month
        - int(second.day < first.day),
    )


def nepali_month_label(value):
    return to_nepali_date(value).strftime("%B %Y")


def nepali_day(value, day):
    current = to_nepali_date(value)
    return from_nepali_date(current.year, current.month, day)


def iter_nepali_months(start, end):
    first = to_nepali_date(start)
    last = to_nepali_date(end)
    year, month = first.year, first.month
    while (year, month) <= (last.year, last.month):
        yield nepali_period(year, month)
        if month == 12:
            year += 1
            month = 1
        else:
            month += 1
