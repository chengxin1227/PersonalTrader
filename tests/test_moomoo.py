import pytest

from monitor.moomoo_client import (
    afterhours_leaders,
    completed_daily_changes,
    day_gain_exceeded,
    is_circuit_breaker,
    is_test_name,
    is_upward_halt,
    quote_from_filter,
    quote_from_rank,
    quote_from_snapshot,
    regular_session_uptrend,
    total_market_cap,
    yesterday_cap,
)


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
