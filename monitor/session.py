from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from monitor.models import MarketClock, Quote

ET = ZoneInfo("America/New_York")
PREMARKET_START = time(4, 0)
PREMARKET_END = time(9, 30)
AFTERHOURS_START = time(16, 0)
AFTERHOURS_END = time(20, 0)
REGULAR_START = time(9, 30)
REGULAR_END = time(16, 0)


def now_et(moment: datetime | None = None) -> datetime:
    current = moment or datetime.now(tz=ET)
    if current.tzinfo is None:
        current = current.replace(tzinfo=ET)
    return current.astimezone(ET)


def is_premarket(moment: datetime | None = None) -> bool:
    current = now_et(moment)
    if current.weekday() >= 5:
        return False
    return PREMARKET_START <= current.time() < PREMARKET_END


def is_afterhours(moment: datetime | None = None) -> bool:
    current = now_et(moment)
    if current.weekday() >= 5:
        return False
    return AFTERHOURS_START <= current.time() < AFTERHOURS_END


def regular_close_approach(moment: datetime | None = None, minutes: int = 10) -> bool:
    """True during the last `minutes` of the regular session, before 16:00 ET."""
    current = now_et(moment)
    if not is_regular_session(current):
        return False
    close = datetime.combine(current.date(), AFTERHOURS_START, tzinfo=current.tzinfo)
    return current >= close - timedelta(minutes=minutes)


def afterhours_just_opened(moment: datetime | None = None, seconds: int = 45) -> bool:
    """True in the first `seconds` after 16:00 ET, while the close is still the regular close."""
    current = now_et(moment)
    if not is_afterhours(current):
        return False
    start = datetime.combine(current.date(), AFTERHOURS_START, tzinfo=current.tzinfo)
    return current < start + timedelta(seconds=seconds)


def afterhours_stable_due(
    moment: datetime | None,
    minutes: int,
    grace_seconds: int = 20,
) -> bool:
    """True only just after the opening window, so a late check does not use a longer range."""
    current = now_et(moment)
    if current.weekday() >= 5:
        return False
    end = datetime.combine(current.date(), AFTERHOURS_START, tzinfo=current.tzinfo) + timedelta(
        minutes=minutes
    )
    return end <= current < end + timedelta(seconds=grace_seconds)


def afterhours_stable_missed(
    moment: datetime | None,
    minutes: int,
    grace_seconds: int = 20,
) -> bool:
    current = now_et(moment)
    if current.weekday() >= 5 or current.time() < AFTERHOURS_START:
        return False
    end = datetime.combine(current.date(), AFTERHOURS_START, tzinfo=current.tzinfo) + timedelta(
        minutes=minutes, seconds=grace_seconds
    )
    return current >= end


def afterhours_board_slot(moment: datetime | None = None) -> datetime | None:
    """The current half-hour mark during after-hours, from 16:00 through 19:30 ET."""
    current = now_et(moment)
    if not is_afterhours(current):
        return None
    minute = 0 if current.minute < 30 else 30
    return current.replace(minute=minute, second=0, microsecond=0)


def last_hour_phase(
    moment: datetime | None = None,
    hours: int = 1,
    session: str = "regular",
) -> str | None:
    """Record prices for 15 minutes, then alert until the session ends.

    `hours` is the alert window before the session end. The baseline is the
    15 minutes just before that. Regular two hours start at 14:00. Premarket
    two hours start at 7:30. After-hours two hours start at 18:00.
    """
    current = now_et(moment)
    if current.weekday() >= 5:
        return None
    bounds = {
        "regular": (REGULAR_START, REGULAR_END),
        "premarket": (PREMARKET_START, PREMARKET_END),
        "afterhours": (AFTERHOURS_START, AFTERHOURS_END),
    }.get(session)
    if bounds is None:
        return None
    start_bound, end = bounds
    clock = current.time()
    if clock < start_bound or clock >= end:
        return None
    close = datetime.combine(current.date(), end, tzinfo=current.tzinfo)
    alert_start = close - timedelta(hours=hours)
    baseline_start = alert_start - timedelta(minutes=15)
    if current >= alert_start:
        return "alert"
    if current >= baseline_start:
        return "baseline"
    return None


def is_regular_session(moment: datetime | None = None) -> bool:
    current = now_et(moment)
    if current.weekday() >= 5:
        return False
    return REGULAR_START <= current.time() < REGULAR_END


def extended_session_open(moment: datetime | None = None) -> bool:
    return is_premarket(moment) or is_afterhours(moment)


def seconds_until_extended_open(moment: datetime | None = None) -> float:
    """Seconds until the next pre-market or after-hours window. Zero when one is open."""
    current = now_et(moment)
    if extended_session_open(current):
        return 0.0
    for day_offset in range(0, 8):
        day = (current + timedelta(days=day_offset)).date()
        if day.weekday() >= 5:
            continue
        for start in (PREMARKET_START, AFTERHOURS_START):
            stamp = datetime.combine(day, start, tzinfo=ET)
            if stamp > current:
                return (stamp - current).total_seconds()
    return 3600.0


def recent_trading_days(end: date, count: int, *, include_end: bool) -> list[str]:
    """Weekdays ending at `end`. Premarket leaves out the unfinished session."""
    if count < 1:
        return []
    cursor = end if include_end else end - timedelta(days=1)
    found: list[str] = []
    while len(found) < count:
        if cursor.weekday() < 5:
            found.append(cursor.isoformat())
        cursor -= timedelta(days=1)
    return found


def is_trading_day(clock: MarketClock | None, moment: datetime | None = None) -> bool:
    current = now_et(moment)
    if current.weekday() >= 5:
        return False
    if clock is None:
        return True
    if clock.is_open:
        return True
    if clock.next_open is None:
        return True
    return clock.next_open.astimezone(ET).date() == current.date()


def premarket_mark(
    quote: Quote, moment: datetime | None = None
) -> tuple[float | None, float | None, datetime | None]:
    """Price, change vs previous close, and timestamp if that print is today's pre-market."""
    price = None
    stamped = None
    if trade_is_premarket(quote.trade_at, moment) and quote.price is not None:
        price = quote.price
        stamped = quote.trade_at
    elif trade_is_premarket(quote.quote_at, moment) and quote.bid is not None and quote.ask is not None:
        price = (quote.bid + quote.ask) / 2
        stamped = quote.quote_at
    if price is None or quote.prev_close in (None, 0):
        return None, None, stamped
    change = ((price - quote.prev_close) / quote.prev_close) * 100
    return price, change, stamped


def trade_is_premarket(traded_at: datetime | None, moment: datetime | None = None) -> bool:
    if traded_at is None:
        return False
    current = now_et(moment)
    traded = traded_at.astimezone(ET)
    if traded.date() != current.date():
        return False
    return PREMARKET_START <= traded.time() < PREMARKET_END
