import pytest

from monitor.moomoo_client import (
    is_test_name,
    quote_from_rank,
    quote_from_snapshot,
    total_market_cap,
    yesterday_cap,
)


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
            "after_hours_volume": 100,
        },
        price_key="after_hours_price",
        change_key="after_hours_change_ratio",
        volume_key="after_hours_volume",
    )
    assert quote is not None
    assert quote.symbol == "MIMI"
    assert quote.change_pct == 21.875


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
