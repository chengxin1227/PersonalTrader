from datetime import date

import pytest

from monitor.models import Quote
from monitor.moomoo_client import (
    _is_opend_limit,
    afterhours_leaders,
    completed_daily_changes,
    day_gain_exceeded,
    is_circuit_breaker,
    is_otc_exchange,
    is_test_name,
    is_upward_halt,
    unchanged_symbols,
    after_hours_range_pct,
    quote_from_after_hours,
    quote_from_premarket,
    quote_from_filter,
    quote_from_rank,
    quote_from_snapshot,
    session_mark,
    recent_daily_window,
    regular_high_change_pct,
    regular_touch_change_pct,
    regular_session_uptrend,
    total_market_cap,
    yesterday_cap,
)


def test_after_hours_range_uses_the_regular_close():
    assert after_hours_range_pct(10.5, 5.0, 10.5, 9.5) == (-5.0, 5.0)
    assert after_hours_range_pct(10.0, 0.0, 10.5, 9.5) == (-5.0, 5.0)
    low, high = after_hours_range_pct(10.0, 0.0, 10.51, 9.49)
    assert low < -5
    assert high > 5
    assert after_hours_range_pct(None, 0.0, 10.0, 9.0) is None
    quote = quote_from_after_hours(
        {
            "code": "US.ABCD",
            "name": "Example",
            "after_price": 10.0,
            "after_change_rate": 0.0,
            "last_price": 9.5,
            "prev_close_price": 10.0,
            "after_high_price": 10.4,
            "after_low_price": 9.7,
            "after_volume": 120000,
        }
    )
    assert quote is not None
    assert quote.change_pct == 0
    assert quote.session_low_pct == -3
    assert quote.session_high_pct == 4
    assert quote.volume == 120000
    assert quote.regular_price == 9.5
    assert quote.after_hours_price == 10.0


def test_after_hours_quote_works_without_high_low_band():
    quote = quote_from_after_hours(
        {
            "code": "US.CRIS",
            "name": "Curis",
            "after_price": 1.16,
            "after_change_rate": 62.5,
            "last_price": 0.71,
            "prev_close_price": 0.66,
            "after_high_price": None,
            "after_low_price": None,
            "after_volume": 200000,
        }
    )
    assert quote is not None
    assert quote.symbol == "CRIS"
    assert quote.price == 1.16
    assert quote.change_pct == 62.5
    assert quote.session_low_pct is None
    assert quote.session_high_pct is None


def test_pink_sheet_names_are_otc():
    assert is_otc_exchange("US_PINK")
    assert not is_otc_exchange("US_NASDAQ")
    assert not is_otc_exchange("US_NYSE")
    assert not is_otc_exchange("US_AMEX")
    assert not is_otc_exchange("")


def test_premarket_quote_uses_the_premarket_print():
    quote = quote_from_premarket(
        {
            "code": "US.MI",
            "name": "NFT Ltd",
            "pre_price": 1.5,
            "pre_change_rate": 6.2,
            "pre_volume": 180000,
            "after_price": 1.4,
            "after_change_rate": 8.5,
        }
    )
    assert quote is not None
    assert quote.symbol == "MI"
    assert quote.change_pct == 6.2
    assert quote.volume == 180000
    assert quote.premarket_price == 1.5
    assert quote.premarket_change_pct == 6.2
    assert quote.after_hours_price == 1.4
    assert quote.after_hours_change_pct == 8.5
    assert quote_from_premarket({"code": "US.MI", "pre_price": 1.5}) is None


def test_unchanged_symbols_keeps_only_a_frozen_price():
    previous = {"ZYBT": 2.5, "WFF": 1.0}
    current, frozen = unchanged_symbols(
        previous,
        [
            Quote(symbol="ZYBT", price=2.5, change_pct=90),
            Quote(symbol="WFF", price=1.1, change_pct=30),
            Quote(symbol="NEW", price=3.0, change_pct=40),
        ],
    )
    assert frozen == ["ZYBT"]
    assert current == {"ZYBT": 2.5, "WFF": 1.1, "NEW": 3.0}
    current, frozen = unchanged_symbols({}, [Quote(symbol="ZYBT", price=2.5)])
    assert frozen == []
    assert current == {"ZYBT": 2.5}


def test_regular_high_change_uses_the_session_high():
    assert regular_high_change_pct(1.9, 1.0) == 90
    assert regular_high_change_pct(1.91, 1.0) > 90
    assert regular_high_change_pct(None, 1.0) is None
    assert regular_high_change_pct(2.0, 0) is None


def test_regular_touch_uses_high_once_it_clears_premarket():
    # Live change still counts.
    assert regular_touch_change_pct(change_pct=85.0, high=1.5, prev_close=1.0) == 85.0
    # Day high above pre-market high counts as a regular-session touch.
    assert (
        regular_touch_change_pct(
            change_pct=22.0, high=0.85, prev_close=0.4479, pre_high=0.8142
        )
        == 89.7745
    )
    # Day high still equal to the pre-market high is not a regular touch by itself.
    assert (
        regular_touch_change_pct(
            change_pct=10.0, high=0.8142, prev_close=0.4479, pre_high=0.8142
        )
        == 10.0
    )
    assert (
        regular_touch_change_pct(
            change_pct=None, high=0.8142, prev_close=0.4479, pre_high=0.8142
        )
        is None
    )


def test_session_mark_uses_the_extended_print():
    assert session_mark({"pre_price": 1.25, "pre_volume": 120000}, "premarket") == (1.25, 120000)
    assert session_mark({"after_price": 0.2, "after_volume": 10}, "afterhours") == (0.2, 10)
    assert session_mark({"pre_price": 0, "pre_volume": 100}, "premarket") is None
    assert session_mark({"after_price": 1.0}, "regular") is None


def test_snapshot_frequency_is_a_limit():
    assert _is_opend_limit(
        "Get Market Snapshot request failed due to high frequency. "
        "Maximum 60 times per 30 seconds."
    )
    assert not _is_opend_limit("Unknown stock US.ABCD")


def test_filter_row_reads_price_and_today_volume():
    class Row:
        stock_name = "Acme"
        stock_code = "US.ABCD"

        def __init__(self) -> None:
            self.cur_price = 1.25
            self.__dict__[("volume", 1)] = 150_000

    quote = quote_from_filter(Row())
    assert quote is not None
    assert quote.symbol == "ABCD"
    assert quote.price == 1.25
    assert quote.volume == 150_000


def test_recent_daily_window_stays_near_the_end_date():
    start, end, limit = recent_daily_window(date(2026, 10, 8), 7)
    assert end == "2026-10-08"
    assert start == "2026-09-10"
    assert limit > 7


def test_day_gain_uses_the_last_five_completed_sessions():
    bars = [
        ("2026-09-25", 80.0),
        ("2026-09-29", 10.0),
        ("2026-09-30", 50.0),
        ("2026-10-01", 10.0),
        ("2026-10-02", 10.0),
        ("2026-10-03", 10.0),
        ("2026-10-06", 4.0),
    ]
    afterhours = completed_daily_changes(bars, today="2026-10-06", include_today=True)
    assert afterhours[-5:] == [50.0, 10.0, 10.0, 10.0, 4.0]
    assert not day_gain_exceeded(afterhours, 50, 5)
    bars[2] = ("2026-09-30", 50.1)
    afterhours = completed_daily_changes(bars, today="2026-10-06", include_today=True)
    assert day_gain_exceeded(afterhours, 50, 5)
    premarket = completed_daily_changes(bars, today="2026-10-07", include_today=False)
    assert 50.1 in premarket[-5:]
    assert day_gain_exceeded(premarket, 50, 5)
    today_only = completed_daily_changes(
        [("2026-10-07", 60.0), ("2026-10-06", 10.0)],
        today="2026-10-07",
        include_today=False,
    )
    assert today_only == [10.0]
    assert not day_gain_exceeded(today_only, 50, 5)


def test_afterhours_leaders_skip_zero_prices():
    from monitor.models import Quote
    from monitor.moomoo_client import afterhours_touch_session

    zeros = [
        Quote(symbol=f"Z{index}", company="Placeholder", price=0, change_pct=0)
        for index in range(5)
    ]
    priced = [
        Quote(symbol="GWHT", company="ESS Tech", price=0.3, change_pct=75.4),
        Quote(symbol="VCIG", company="VCI Global", price=2.07, change_pct=46.7),
    ]
    assert afterhours_leaders(zeros + priced, 5) == priced
    assert afterhours_leaders(zeros, 5) == []
    thin = Quote(symbol="THIN", price=1.2, change_pct=90, volume=100_000)
    thick = Quote(symbol="GWHT", price=0.3, change_pct=75.4, volume=100_001)
    assert afterhours_leaders([thin, thick], 5, min_volume=100_000) == [thick]
    assert afterhours_leaders([thin, thick], 5) == [thin, thick]
    lower = Quote(symbol="LOW", price=1.0, change_pct=10, volume=200_000)
    higher = Quote(symbol="HIGH", price=2.0, change_pct=60, volume=200_000)
    assert [quote.symbol for quote in afterhours_leaders([lower, higher], 5)] == ["HIGH", "LOW"]
    assert afterhours_touch_session(50) == "afterhours_touch_50"


def test_afterhours_peak_leaders_rank_by_peak_desc():
    from monitor.models import Quote
    from monitor.moomoo_client import afterhours_peak_leaders

    faded = Quote(
        symbol="FADE",
        price=1.5,
        change_pct=12.0,
        volume=200_000,
        session_high_pct=80.0,
    )
    hot = Quote(symbol="HOT", price=2.0, change_pct=40.0, volume=200_000)
    thin = Quote(
        symbol="THIN",
        price=1.0,
        change_pct=90.0,
        volume=50_000,
        session_high_pct=95.0,
    )
    leaders = afterhours_peak_leaders(
        [hot, faded, thin],
        {"STORED": 70.0},
        5,
        min_volume=100_000,
    )
    assert [quote.symbol for quote in leaders] == ["FADE", "HOT"]
    assert leaders[0].session_high_pct == 80.0

    stored = Quote(symbol="STORED", price=1.2, change_pct=5.0, volume=150_000)
    leaders = afterhours_peak_leaders(
        [hot, faded, stored],
        {"STORED": 70.0},
        5,
        min_volume=100_000,
    )
    assert [quote.symbol for quote in leaders] == ["FADE", "STORED", "HOT"]
    assert leaders[1].session_high_pct == 70.0


def test_snapshot_uses_last_price_against_previous_close():
    quote = quote_from_snapshot(
        {
            "code": "US.AAPL",
            "last_price": 110,
            "prev_close_price": 100,
            "bid_price": 109.5,
            "ask_price": 110.5,
            "volume": 20,
            "update_time": "2026-09-25 16:00:00",
        }
    )
    assert quote.symbol == "AAPL"
    assert quote.price == 110
    assert quote.change_pct == 10
    assert quote.bid == 109.5
    assert quote.regular_price == 110
    assert quote.regular_change_pct == 10


def test_snapshot_keeps_each_session_close():
    quote = quote_from_snapshot(
        {
            "code": "US.SDST",
            "last_price": 0.026,
            "prev_close_price": 0.0401,
            "pre_price": 0.031,
            "pre_high_price": 0.035,
            "pre_change_rate": -22.7,
            "after_price": 0.046,
            "after_change_rate": 76.9,
        }
    )
    assert quote.regular_price == 0.026
    assert quote.premarket_price == 0.031
    assert quote.premarket_high == 0.035
    assert quote.premarket_change_pct == -22.7
    assert quote.after_hours_price == 0.046
    assert quote.after_hours_change_pct == 76.9


def test_snapshot_keeps_circuit_breaker_status():
    quote = quote_from_snapshot(
        {
            "code": "US.ABCD",
            "last_price": 2.2,
            "prev_close_price": 1.8,
            "sec_status": "RECOVERABLE_CIRCUIT_BREAKER",
        }
    )
    assert quote.sec_status == "RECOVERABLE_CIRCUIT_BREAKER"
    assert is_circuit_breaker(quote.sec_status)
    assert not is_circuit_breaker("NORMAL")
    assert not is_circuit_breaker("SUSPENDED")
    assert not is_circuit_breaker(None)


def test_upward_halt_is_a_pause_at_the_session_high():
    up = quote_from_snapshot(
        {
            "code": "US.ABCD",
            "last_price": 12,
            "prev_close_price": 10,
            "high_price": 12,
            "low_price": 9.5,
            "sec_status": "RECOVERABLE_CIRCUIT_BREAKER",
        }
    )
    down = quote_from_snapshot(
        {
            "code": "US.ABCD",
            "last_price": 8,
            "prev_close_price": 10,
            "high_price": 11,
            "low_price": 8,
            "sec_status": "RECOVERABLE_CIRCUIT_BREAKER",
        }
    )
    running = quote_from_snapshot(
        {
            "code": "US.ABCD",
            "last_price": 12,
            "prev_close_price": 10,
            "high_price": 12,
            "low_price": 9.5,
            "sec_status": "NORMAL",
        }
    )
    assert is_upward_halt(up)
    assert not is_upward_halt(down)
    assert not is_upward_halt(running)


def test_rank_skips_nasdaq_test_symbol():
    assert is_test_name("Nasdaq Test Symbol")
    assert (
        quote_from_rank(
            {
                "security": "US.TESTJ",
                "name": "Nasdaq Test Symbol",
                "pre_market_price": 4.5,
                "pre_market_change_ratio": 400,
                "close_price": 0.9,
            }
        )
        is None
    )


def test_rank_quote_keeps_premarket_change():
    quote = quote_from_rank(
        {
            "security": "US.INLF",
            "name": "INLIF Ltd",
            "pre_market_price": 6.25,
            "pre_market_change_ratio": 111.864,
            "close_price": 5.10,
            "pre_market_volume": 1000,
        }
    )
    assert quote is not None
    assert quote.symbol == "INLF"
    assert quote.company == "INLIF Ltd"
    assert quote.price == 6.25
    assert quote.prev_close == 5.10
    assert quote.change_pct == 111.864


def test_total_market_cap_prefers_moomoo_total():
    assert (
        total_market_cap(total_market_val=11_408_069, shares=1_000_000, last_price=4.47)
        == 11_408_069
    )
    assert total_market_cap(total_market_val=None, shares=1_000_000, last_price=4.47) == 4_470_000


def test_afterhours_rank_quote():
    quote = quote_from_rank(
        {
            "security": "US.MIMI",
            "name": "Mint",
            "after_hours_price": 0.939,
            "after_hours_change_ratio": 21.875,
            "close_price": 0.771,
            "change_ratio": -4.5,
            "after_hours_volume": 100,
        },
        price_key="after_hours_price",
        change_key="after_hours_change_ratio",
        volume_key="after_hours_volume",
    )
    assert quote is not None
    assert quote.symbol == "MIMI"
    assert quote.change_pct == 21.875
    assert quote.regular_change_pct == -4.5
    assert quote.regular_price == 0.771
    assert quote.after_hours_price == 0.939


def test_regular_session_uptrend_needs_a_rising_close_and_enough_bars():
    rising = [(1.0, 1.0 + index * 0.1) for index in range(20)]
    assert regular_session_uptrend(rising)
    fading = [(3.0, 3.0 - index * 0.05) for index in range(20)]
    assert not regular_session_uptrend(fading)
    spiked = [(2.0, 5.0)] + [(2.0, 4.5 - index * 0.08) for index in range(19)]
    assert spiked[-1][1] > spiked[0][0]
    assert not regular_session_uptrend(spiked)
    assert not regular_session_uptrend(rising[:19])


def test_yesterday_cap_is_shares_times_previous_close():
    assert (
        yesterday_cap(
            shares=1_000_000,
            prev_close=2.0,
            total_market_val=2_200_000,
            last_price=2.2,
        )
        == 2_000_000
    )
    estimated = yesterday_cap(
        shares=None,
        prev_close=2.0,
        total_market_val=2_200_000,
        last_price=2.2,
    )
    assert estimated == pytest.approx(2_000_000)
