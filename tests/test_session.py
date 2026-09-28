from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from monitor.models import MarketClock, Quote
from monitor.session import (
    is_afterhours,
    is_premarket,
    is_trading_day,
    premarket_mark,
    seconds_until_extended_open,
    trade_is_premarket,
)

ET = ZoneInfo("America/New_York")


def test_premarket_window():
    assert is_premarket(datetime(2026, 9, 23, 4, 0, tzinfo=ET))
    assert is_premarket(datetime(2026, 9, 23, 6, 50, tzinfo=ET))
    assert not is_premarket(datetime(2026, 9, 23, 9, 30, tzinfo=ET))
    assert not is_premarket(datetime(2026, 9, 23, 15, 0, tzinfo=ET))
    assert not is_premarket(datetime(2026, 9, 26, 6, 0, tzinfo=ET))  # Saturday


def test_afterhours_window():
    assert is_afterhours(datetime(2026, 9, 23, 16, 0, tzinfo=ET))
    assert is_afterhours(datetime(2026, 9, 23, 19, 59, tzinfo=ET))
    assert not is_afterhours(datetime(2026, 9, 23, 15, 59, tzinfo=ET))
    assert not is_afterhours(datetime(2026, 9, 23, 20, 0, tzinfo=ET))
    assert not is_afterhours(datetime(2026, 9, 26, 17, 0, tzinfo=ET))


def test_seconds_until_next_extended_session():
    sunday_evening = datetime(2026, 9, 27, 17, 52, tzinfo=ET)
    assert seconds_until_extended_open(sunday_evening) == pytest.approx(10 * 3600 + 8 * 60)
    assert seconds_until_extended_open(datetime(2026, 9, 28, 4, 0, tzinfo=ET)) == 0
    regular = datetime(2026, 9, 28, 10, 0, tzinfo=ET)
    assert seconds_until_extended_open(regular) == pytest.approx(6 * 3600)


def test_trade_must_be_today_premarket():
    now = datetime(2026, 9, 23, 6, 50, tzinfo=ET)
    assert trade_is_premarket(datetime(2026, 9, 23, 5, 10, tzinfo=ET), now)
    assert not trade_is_premarket(datetime(2026, 9, 22, 20, 10, tzinfo=ET), now)
    assert not trade_is_premarket(datetime(2026, 9, 23, 16, 10, tzinfo=ET), now)


def test_premarket_mark_falls_back_to_today_quote():
    now = datetime(2026, 9, 23, 6, 50, tzinfo=ET)
    quote = Quote(
        symbol="ABCD",
        price=1.0,
        bid=2.1,
        ask=2.3,
        prev_close=2.0,
        trade_at=datetime(2026, 9, 22, 20, 0, tzinfo=ET),
        quote_at=datetime(2026, 9, 23, 5, 15, tzinfo=ET),
    )
    price, change, stamped = premarket_mark(quote, now)
    assert price == 2.2
    assert change == pytest.approx(10.0)
    assert stamped == quote.quote_at


def test_trading_day_uses_next_open():
    morning = datetime(2026, 9, 23, 6, 50, tzinfo=ET)
    clock = MarketClock(
        is_open=False,
        next_open=datetime(2026, 9, 23, 9, 30, tzinfo=ET),
    )
    assert is_trading_day(clock, morning)
    weekend = MarketClock(
        is_open=False,
        next_open=datetime(2026, 9, 28, 9, 30, tzinfo=ET),
    )
    assert not is_trading_day(weekend, datetime(2026, 9, 26, 10, 0, tzinfo=ET))
