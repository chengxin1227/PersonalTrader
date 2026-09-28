from datetime import datetime, timezone

from monitor.marketcap import yesterday_market_cap
from monitor.models import Condition, ConditionType, Quote, Rule, RulesConfig
from monitor.rules import evaluate_afterhours_scan, evaluate_premarket_scan, render_message


def _scan_rule() -> Rule:
    return Rule(
        id="premarket-smallcap-10pct",
        name="盘前涨幅超10% 且总市值不超过1亿美元",
        symbol="*",
        condition=Condition(
            type=ConditionType.PREMARKET_GAIN,
            value=10,
            max_market_cap=100_000_000,
            min_volume=10_000,
        ),
        slack_message="{symbol} 盘前 {change_pct:+.1f}%  ${price:.2f}",
    )


def _quote(
    symbol: str = "ABCD",
    price: float = 2.2,
    prev_close: float = 2.0,
    volume: int = 20_000,
) -> Quote:
    return Quote(
        symbol=symbol,
        price=price,
        prev_close=prev_close,
        change_pct=((price - prev_close) / prev_close) * 100,
        volume=volume,
        updated_at=datetime.now(timezone.utc),
    )


def test_scanner_requires_gain_and_total_cap_at_or_under_the_limit():
    rule = _scan_rule()
    quote = _quote()
    assert evaluate_premarket_scan(rule, quote, market_cap=100_000_000, in_premarket=True).matched
    assert not evaluate_premarket_scan(rule, quote, market_cap=100_000_000, in_premarket=False).matched
    assert not evaluate_premarket_scan(rule, quote, market_cap=100_000_001, in_premarket=True).matched
    assert evaluate_premarket_scan(
        rule, quote, market_cap=None, in_premarket=True, cap_filtered=True
    ).matched
    small_move = _quote(price=2.05, prev_close=2.0)
    assert not evaluate_premarket_scan(
        rule, small_move, market_cap=45_000_000, in_premarket=True
    ).matched
    thin = _quote(volume=10_000)
    assert not evaluate_premarket_scan(
        rule, thin, market_cap=None, in_premarket=True, cap_filtered=True
    ).matched
    assert evaluate_premarket_scan(
        rule, _quote(volume=10_001), market_cap=None, in_premarket=True, cap_filtered=True
    ).matched


def test_afterhours_scan_allows_cap_at_the_limit():
    rule = Rule(
        id="afterhours-smallcap-10pct",
        name="盘后涨幅超10% 且总市值不超过1亿美元",
        symbol="*",
        condition=Condition(type=ConditionType.AFTERHOURS_GAIN, value=10, max_market_cap=100_000_000),
        slack_message="{symbol} 盘后 {change_pct:+.1f}%  ${price:.2f}  总市值 ${market_cap_m:.1f}M",
    )
    quote = _quote()
    assert evaluate_afterhours_scan(rule, quote, market_cap=100_000_000, in_afterhours=True).matched
    assert not evaluate_afterhours_scan(rule, quote, market_cap=100_000_001, in_afterhours=True).matched
    assert not evaluate_afterhours_scan(rule, quote, market_cap=45_000_000, in_afterhours=False).matched
    assert rule.is_scanner


def test_yesterday_market_cap_prefers_shares_times_prev_close():
    assert yesterday_market_cap(
        prev_close=2.0,
        price=2.2,
        shares_outstanding=20_000_000,
        current_market_cap=50_000_000,
    ) == 40_000_000
    estimated = yesterday_market_cap(
        prev_close=2.0,
        price=2.2,
        shares_outstanding=None,
        current_market_cap=44_000_000,
    )
    assert estimated == 40_000_000


def test_watchlist_skips_scanner_wildcard():
    config = RulesConfig(
        watchlist=["AAPL"],
        rules=[_scan_rule(), Rule(id="spy", name="SPY", symbol="SPY", condition=Condition(type=ConditionType.PRICE_ABOVE, value=1))],
    )
    assert config.all_symbols() == ["AAPL", "SPY"]
    assert _scan_rule().is_scanner


def test_render_includes_market_cap():
    rule = Rule(
        id="afterhours-smallcap-10pct",
        name="盘后",
        symbol="*",
        condition=Condition(type=ConditionType.AFTERHOURS_GAIN, value=10, max_market_cap=100_000_000),
        slack_message="{symbol} 盘后 {change_pct:+.1f}%  ${price:.2f}  总市值 ${market_cap_m:.1f}M",
    )
    message = render_message(rule, _quote(), {"market_cap": 45_000_000, "market_cap_m": 45})
    assert "ABCD" in message
    assert "45.0M" in message
