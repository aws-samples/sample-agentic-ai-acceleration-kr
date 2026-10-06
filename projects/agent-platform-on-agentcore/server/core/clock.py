"""The calendar the usage rollup counts days in.

**Every date in this feature is a *business* date, not a UTC one.** The counters
are keyed by `YYYY-MM-DD` (`repositories/usage_repository`), so whatever timezone
that string is computed in becomes the day boundary the dashboard's axis, its
daily trend and its half-window delta are all divided on. Left as `utcnow()` — as
it was — the boundary is UTC 00:00, which is 09:00 in Seoul: a Korean morning's
turns land on the previous label and a daily report taken from this page names the
wrong day for them.

`USAGE_TIMEZONE` defaults to `UTC`, so the default behaviour is unchanged and no
existing partition shifts under a redeploy. Setting it is a deliberate act with a
consequence worth stating: **days written before the change keep their old
boundary.** Nothing can re-bucket them — the stored item *is* the day — so a
window spanning the switch mixes two calendars for one day. Set it once, when the
table is young, or accept that one day.

The label travels to the client with every response, because a date axis whose
timezone is unstated is a date axis a reader will assume is theirs.
"""
from datetime import datetime, timedelta, tzinfo
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core.config import USAGE_TIMEZONE

_UTC = ZoneInfo("UTC")


def _zone(name: str) -> tzinfo:
    """The configured zone, or UTC when the name is not one.

    Never raises. A typo in an environment variable must not stop the server from
    counting turns — it degrades to the previous behaviour, which is UTC.
    """
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return _UTC


ZONE = _zone(USAGE_TIMEZONE)
# What the client renders next to the axis. The configured name rather than an
# offset, because an offset is wrong twice a year in half the world's zones.
ZONE_NAME = str(getattr(ZONE, "key", "UTC"))


def now() -> datetime:
    """The current time in the business zone, tz-aware."""
    return datetime.now(ZONE)


def today() -> str:
    """Today's `YYYY-MM-DD` in the business zone — the counter's date key."""
    return now().strftime("%Y-%m-%d")


def date_of(moment: Optional[datetime]) -> str:
    """A moment's business date. Naive input is read as UTC, which is what it is.

    Every naive timestamp this platform stores was written by `utcnow()`, so
    attaching UTC is a correction rather than an assumption.
    """
    if moment is None:
        return today()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_UTC)
    return moment.astimezone(ZONE).strftime("%Y-%m-%d")


def dates(start_date: str, end_date: str) -> list:
    """Every business date from `start_date` to `end_date`, inclusive.

    The axis of this feature is a calendar, and a consumer that infers it from the
    days that happened to be written gets it wrong whenever the platform was idle.
    Empty for a reversed pair rather than looping forever.
    """
    try:
        cursor = datetime.strptime(start_date, "%Y-%m-%d")
        last = datetime.strptime(end_date, "%Y-%m-%d")
    except (TypeError, ValueError):
        return []
    out = []
    while cursor <= last:
        out.append(cursor.strftime("%Y-%m-%d"))
        cursor += timedelta(days=1)
    return out


def window(days: int) -> dict:
    """The last `days` business dates, and the instants that bound them.

    `start`/`end` are tz-aware and exist for CloudWatch, which takes instants.
    `start_date`/`end_date` are the business dates the counters are keyed by.

    `partial_day` is the end date itself, always: the window ends *now*, so its
    last day is still being written. It is returned rather than inferred, because
    every consumer that draws or subtracts the last point has to know — the
    half-window delta was reading a third of a day against three whole ones and
    reporting the difference as a change in usage.
    """
    end = now()
    start = (end - timedelta(days=days - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return {
        "days": days,
        "start_date": start.strftime("%Y-%m-%d"),
        "end_date": end.strftime("%Y-%m-%d"),
        "start": start,
        "end": end,
        "timezone": ZONE_NAME,
        "partial_day": end.strftime("%Y-%m-%d"),
    }
