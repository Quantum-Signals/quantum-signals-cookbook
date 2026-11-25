"""US market holidays and early closes, computed from NYSE's published rules.

Positions must be closed while the market is open. On an early-close day the
normal session end (15:55 New York) comes hours after the market shut, so the
close would fail and leave positions open overnight. One-off closures (such as
a national day of mourning) are not covered.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")
# NYSE closes at 13:00 on these days; CME equity index futures at 13:15.
EARLY_CLOSE = time(12, 55)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _last_monday(year: int, month: int) -> date:
    d = date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)
    return d - timedelta(days=d.weekday())


def _easter(year: int) -> date:
    """Gregorian Easter Sunday (anonymous algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = divmod(b, 4)
    g = (8 * b + 13) // 25
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    m = (32 + 2 * e + 2 * i - h - k) % 7
    n = (a + 11 * h + 22 * m) // 451
    month, day = divmod(h + m - 7 * n + 114, 31)
    return date(year, month, day + 1)


def _observed(d: date) -> date:
    """Saturday holidays move to Friday and Sunday ones to Monday."""
    return d + timedelta(days={5: -1, 6: 1}.get(d.weekday(), 0))


def holidays(year: int) -> set[date]:
    """Full-day NYSE holidays that fall in *year*."""
    new_year = date(year, 1, 1)
    days = {
        _nth_weekday(year, 1, 0, 3),  # Martin Luther King Jr. Day
        _nth_weekday(year, 2, 0, 3),  # Washington's Birthday
        _easter(year) - timedelta(days=2),  # Good Friday
        _last_monday(year, 5),  # Memorial Day
        _observed(date(year, 6, 19)),  # Juneteenth
        _observed(date(year, 7, 4)),  # Independence Day
        _nth_weekday(year, 9, 0, 1),  # Labor Day
        _nth_weekday(year, 11, 3, 4),  # Thanksgiving
        _observed(date(year, 12, 25)),  # Christmas
    }
    if new_year.weekday() != 5:  # a Saturday New Year is not moved to December 31
        days.add(_observed(new_year))
    return days


def is_holiday(d: date) -> bool:
    return d in holidays(d.year)


def early_close(d: date) -> datetime | None:
    """New York time by which positions must be closed on *d*, if it is a half day."""
    candidates = {
        _nth_weekday(d.year, 11, 3, 4) + timedelta(days=1),
        date(d.year, 7, 3),
        date(d.year, 12, 24),
    }
    if d in candidates and d.weekday() < 5 and not is_holiday(d):
        return datetime.combine(d, EARLY_CLOSE, tzinfo=NEW_YORK)
    return None
