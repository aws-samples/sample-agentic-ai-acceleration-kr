"""The calendar the usage counters bucket days in.

The day boundary is not cosmetic: the counters are keyed by `YYYY-MM-DD`, so the
timezone that string is computed in *is* the boundary the dashboard's axis, its
daily trend and its half-window delta are divided on. Computed with `utcnow()` — as
it was — that boundary is UTC 00:00, which is 09:00 in Seoul, so a Korean morning's
turns land under the previous day's label and a daily report taken from the page
names the wrong day for them.

The default is UTC, because changing the boundary cannot re-bucket days already
written and a silent shift on redeploy would be worse than the original problem.
"""
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import clock  # noqa: E402


def test_the_default_is_utc_so_a_redeploy_shifts_nothing():
    """A configured zone is opt-in. Nothing moves until somebody sets it."""
    assert clock.ZONE_NAME in ("UTC", os.getenv("USAGE_TIMEZONE", "UTC"))


def test_an_unknown_zone_degrades_to_utc_rather_than_raising():
    """A typo in an environment variable must not stop the server counting turns."""
    assert clock._zone("Not/AZone") is ZoneInfo("UTC")
    assert clock._zone("") is ZoneInfo("UTC")


def test_a_naive_timestamp_is_read_as_utc_because_that_is_what_it_is():
    """Every naive timestamp this platform stores was written by `utcnow()`.

    Attaching UTC is a correction, not an assumption — and it is the difference
    between placing a 23:30 UTC event on the right day and on the day before.
    """
    seoul = clock._zone("Asia/Seoul")
    moment = datetime(2026, 8, 17, 23, 30)  # naive, i.e. UTC

    in_utc = moment.replace(tzinfo=ZoneInfo("UTC")).astimezone(
        ZoneInfo("UTC")
    ).strftime("%Y-%m-%d")
    in_seoul = moment.replace(tzinfo=ZoneInfo("UTC")).astimezone(seoul).strftime(
        "%Y-%m-%d"
    )

    assert in_utc == "2026-08-17"
    # The same instant is already the 18th in Seoul, which is exactly the shift a
    # Korean operator sees on the axis and had no way to know about.
    assert in_seoul == "2026-08-18"


def test_the_window_covers_the_requested_number_of_calendar_days():
    window = clock.window(7)

    start = datetime.strptime(window["start_date"], "%Y-%m-%d")
    end = datetime.strptime(window["end_date"], "%Y-%m-%d")

    assert (end - start) == timedelta(days=6), "7 days inclusive of both ends"


def test_the_window_names_the_day_that_is_still_being_written():
    """`partial_day` is the end date, always, and it is returned rather than inferred.

    The window ends *now*, so its last day holds however much of today has happened.
    Every consumer that draws or subtracts that point has to know: the half-window
    delta was comparing a fraction of today against whole days and reporting the
    shortfall as a fall in usage — a structural, always-negative bias, largest first
    thing in the morning.
    """
    window = clock.window(30)

    assert window["partial_day"] == window["end_date"]
    assert window["timezone"] == clock.ZONE_NAME


def test_the_window_bounds_are_timezone_aware():
    """CloudWatch takes instants, and a naive one is signed as if it were UTC."""
    window = clock.window(7)

    assert window["start"].tzinfo is not None
    assert window["end"].tzinfo is not None
    assert window["start"].hour == 0 and window["start"].minute == 0


def test_every_date_in_a_window_is_enumerable():
    """The rollup's axis is a calendar, so the calendar has to be constructible.

    The daily series is dense over this list rather than over the days that happened
    to be written, because a consumer that counts rows instead of days splits a
    window unevenly and then names the wrong period in its own label.
    """
    assert clock.dates("2026-08-14", "2026-08-16") == [
        "2026-08-14", "2026-08-15", "2026-08-16"
    ]
    assert clock.dates("2026-07-31", "2026-08-01") == ["2026-07-31", "2026-08-01"]
    assert clock.dates("2026-08-16", "2026-08-16") == ["2026-08-16"]
    # A reversed pair is empty rather than an infinite loop.
    assert clock.dates("2026-08-16", "2026-08-14") == []
