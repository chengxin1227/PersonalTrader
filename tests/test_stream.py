from datetime import datetime, timezone

from monitor.quotes import apply_trade, previous_price
from monitor.stream import parse_stream_messages


def test_parse_trade_and_control():
    events, control = parse_stream_messages(
        [
            {"T": "success", "msg": "authenticated"},
            {"T": "t", "S": "aapl", "p": 210.5, "t": "2026-09-23T10:15:00Z"},
            {"T": "q", "S": "AAPL", "bp": 210.4, "ap": 210.6, "t": "2026-09-23T10:15:01Z"},
        ]
    )
    assert control[0]["msg"] == "authenticated"
    assert events[0].kind == "trade"
    assert events[0].symbol == "AAPL"
    assert events[0].price == 210.5
    assert events[1].kind == "quote"
    assert events[1].bid == 210.4


def test_apply_trade_tracks_previous_and_change():
    first = apply_trade(None, "AAPL", 100, datetime.now(timezone.utc))
    first.prev_close = 100
    second = apply_trade(first, "AAPL", 110, datetime.now(timezone.utc))
    assert second.change_pct == 10
    assert previous_price(second) == 100
