from __future__ import annotations

from datetime import date, timedelta

# CME quarterly month codes: Mar / Jun / Sep / Dec.
_QUARTERS: tuple[tuple[int, str], ...] = ((3, "H"), (6, "M"), (9, "U"), (12, "Z"))


def _third_friday(year: int, month: int) -> date:
    first = date(year, month, 1)
    first_friday = first + timedelta(days=(4 - first.weekday()) % 7)
    return first_friday + timedelta(weeks=2)


def cme_front_month_symbol(root: str, when: date | None = None) -> str:
    """Return the CME quarterly front-month symbol (e.g. ``ESU6``).

    Rolls on the third Friday of the expiry month. Used as a fallback when
    Rithmic's ``get_front_month_contract`` is unavailable.
    """
    when = when or date.today()
    year, month = when.year, when.month
    for qmonth, code in _QUARTERS:
        expiry = _third_friday(year, qmonth)
        if month < qmonth or (month == qmonth and when <= expiry):
            return f"{root}{code}{year % 10}"
    return f"{root}H{(year + 1) % 10}"
