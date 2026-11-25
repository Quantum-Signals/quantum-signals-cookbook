"""US holidays and half days, against NYSE's published calendars."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

import trader.app as app
from trader import market_calendar as cal
from trader.config import Settings

NY = ZoneInfo("America/New_York")

PUBLISHED = {
    2021: "01-01 01-18 02-15 04-02 05-31 06-18 07-05 09-06 11-25 12-24",
    2022: "01-17 02-21 04-15 05-30 06-20 07-04 09-05 11-24 12-26",
    2024: "01-01 01-15 02-19 03-29 05-27 06-19 07-04 09-02 11-28 12-25",
    2025: "01-01 01-20 02-17 04-18 05-26 06-19 07-04 09-01 11-27 12-25",
    2026: "01-01 01-19 02-16 04-03 05-25 06-19 07-03 09-07 11-26 12-25",
    2027: "01-01 01-18 02-15 03-26 05-31 06-18 07-05 09-06 11-25 12-24",
}


@pytest.mark.parametrize("year", PUBLISHED)
def test_holidays_match_the_published_calendar(year):
    expected = {date.fromisoformat(f"{year}-{d}") for d in PUBLISHED[year].split()}
    found = cal.holidays(year)
    if year == 2021:
        expected -= {date(2021, 6, 18)}  # NYSE first observed Juneteenth in 2022
        found = found - {date(2021, 6, 18)}
    assert found == expected


@pytest.mark.parametrize(
    "day, half",
    [
        (date(2024, 7, 3), True),
        (date(2024, 11, 29), True),
        (date(2024, 12, 24), True),
        (date(2025, 7, 3), True),
        (date(2026, 11, 27), True),
        (date(2026, 12, 24), True),
        (date(2026, 7, 3), False),  # full holiday: July 4 is a Saturday
        (date(2027, 12, 24), False),  # full holiday: Christmas is a Saturday
        (date(2026, 11, 25), False),
    ],
)
def test_early_closes(day, half):
    assert (cal.early_close(day) is not None) is half


def _window(now, **kw):
    return app._compute_next_session_window(now, Settings(_env_file=None, qs_api_key="k", **kw))


def test_thanksgiving_is_skipped_and_the_next_day_ends_early():
    start, end = _window(datetime(2026, 11, 25, 16, 0, tzinfo=NY))
    assert (start, end) == (datetime(2026, 11, 27, 9, 30, tzinfo=NY), datetime(2026, 11, 27, 12, 55, tzinfo=NY))
    # Once the half day is over, the next session is Monday's.
    assert _window(datetime(2026, 11, 27, 13, 0, tzinfo=NY))[0] == datetime(2026, 11, 30, 9, 30, tzinfo=NY)


def test_early_close_is_converted_to_the_session_timezone():
    paris = ZoneInfo("Europe/Paris")
    _, end = _window(
        datetime(2026, 11, 27, 16, 0, tzinfo=paris),
        session_timezone="Europe/Paris", session_start_time="15:30", session_end_time="21:55",
    )
    assert end == datetime(2026, 11, 27, 18, 55, tzinfo=paris)


def test_session_starting_after_the_early_close_is_skipped():
    start, _ = _window(datetime(2026, 11, 27, 9, 0, tzinfo=NY), session_start_time="14:00")
    assert start.date() == date(2026, 11, 30)


def test_calendar_can_be_turned_off():
    start, end = _window(datetime(2026, 11, 26, 9, 0, tzinfo=NY), us_market_calendar=False)
    assert start.date() == date(2026, 11, 26) and end.hour == 15


def test_impossible_session_times_are_reported():
    with pytest.raises(ValueError, match="No trading session"):
        _window(datetime(2026, 11, 25, 9, 0, tzinfo=NY), session_start_time="16:00", session_end_time="00:00")
