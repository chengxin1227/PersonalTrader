from datetime import datetime, timezone

from monitor.marketcap import yesterday_market_cap
from monitor.models import Condition, ConditionType, Quote, Rule, RulesConfig
from monitor.rules import (
    FIELD_GAP,
    evaluate_afterhours_scan,
    evaluate_stable_afterhours,
    evaluate_last_hour_scan,
    evaluate_premarket_scan,
    hour_change_pct,
    leaderboard_line,
    render_message,
)


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


def test_pullback_needs_current_gain_above_10_and_an_earlier_spike():
    rule = Rule(
        id="premarket-smallcap-10pct",
        name="盘前",
        symbol="*",
        condition=Condition(
            type=ConditionType.PREMARKET_GAIN,
            value=10,
            max_market_cap=100_000_000,
            min_volume=100_000,
            min_prior_change=20,
            prior_change_lag_minutes=5,
        ),
    )
    quote = _quote(price=1.11, prev_close=1.0, volume=100_001)
    quote.change_pct = 11
    quote.prior_session_spike = True
    assert evaluate_premarket_scan(
        rule, quote, market_cap=None, in_premarket=True, cap_filtered=True
    ).matched
    quote.change_pct = 10
    assert not evaluate_premarket_scan(
        rule, quote, market_cap=None, in_premarket=True, cap_filtered=True
    ).matched
    quote.change_pct = 11
    quote.prior_session_spike = False
    assert not evaluate_premarket_scan(
        rule, quote, market_cap=None, in_premarket=True, cap_filtered=True
    ).matched
    quote.prior_session_spike = None
    assert not evaluate_premarket_scan(
        rule, quote, market_cap=None, in_premarket=True, cap_filtered=True
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


def test_afterhours_followthrough_requires_regular_gain_above_30():
    rule = Rule(
        id="afterhours-day30-ah4",
        name="盘中涨幅超30%，盘后涨幅超4%，市值低于1亿美元",
        symbol="*",
        condition=Condition(
            type=ConditionType.AFTERHOURS_GAIN,
            value=4,
            max_market_cap=100_000_000,
            min_regular_change=30,
        ),
    )
    quote = _quote(volume=1)
    quote.change_pct = 4.1
    quote.regular_change_pct = 30.1
    assert evaluate_afterhours_scan(
        rule, quote, market_cap=None, in_afterhours=True, cap_filtered=True
    ).matched
    quote.regular_change_pct = 30
    assert not evaluate_afterhours_scan(
        rule, quote, market_cap=None, in_afterhours=True, cap_filtered=True
    ).matched
    quote.regular_change_pct = 40
    quote.change_pct = 4
    assert not evaluate_afterhours_scan(
        rule, quote, market_cap=None, in_afterhours=True, cap_filtered=True
    ).matched


def test_day30_stable_band_is_inclusive_through_the_open():
    rule = Rule(
        id="afterhours-day30-ah3",
        name="盘中涨幅超30%，盘后5分钟内涨幅保持在-5%到5%，市值低于1亿美元",
        symbol="*",
        condition=Condition(
            type=ConditionType.AFTERHOURS_GAIN,
            value=5,
            min_regular_change=30,
            max_market_cap=100_000_000,
            min_volume=100_000,
            stable_minutes=5,
            stable_low=-5,
            stable_high=5,
        ),
    )
    quote = _quote(price=10.0, volume=100_001)
    quote.change_pct = 1.2
    quote.regular_change_pct = 30.1
    quote.session_low_pct = -5
    quote.session_high_pct = 5
    assert evaluate_stable_afterhours(rule, quote, cap_filtered=True).matched
    quote.session_high_pct = 5.01
    assert not evaluate_stable_afterhours(rule, quote, cap_filtered=True).matched
    quote.session_high_pct = 5
    quote.session_low_pct = -5.01
    assert not evaluate_stable_afterhours(rule, quote, cap_filtered=True).matched
    quote.session_low_pct = -4
    quote.regular_change_pct = 30
    assert not evaluate_stable_afterhours(rule, quote, cap_filtered=True).matched
    quote.regular_change_pct = 40
    quote.volume = 100_000
    assert not evaluate_stable_afterhours(rule, quote, cap_filtered=True).matched


def test_uptrend_afterhours_rule_uses_regular_volume_and_market_cap():
    rule = Rule(
        id="afterhours-uptrend-day10-ah5",
        name="盘中涨幅超10%且走势向上，成交量超100万，盘后涨幅超5%",
        symbol="*",
        condition=Condition(
            type=ConditionType.AFTERHOURS_GAIN,
            value=5,
            min_regular_change=10,
            min_regular_volume=1_000_000,
            min_volume=100_000,
            max_market_cap=100_000_000,
            require_uptrend=True,
        ),
    )
    quote = _quote(volume=100_001)
    quote.change_pct = 5.1
    quote.regular_change_pct = 10.1
    quote.regular_volume = 1_000_001
    quote.session_uptrend = True
    assert evaluate_afterhours_scan(
        rule, quote, market_cap=100_000_000, in_afterhours=True
    ).matched
    assert not evaluate_afterhours_scan(
        rule, quote, market_cap=100_000_001, in_afterhours=True
    ).matched
    quote.regular_volume = 1_000_000
    assert not evaluate_afterhours_scan(
        rule, quote, market_cap=None, in_afterhours=True
    ).matched
    quote.regular_volume = 1_000_001
    quote.session_uptrend = False
    assert not evaluate_afterhours_scan(
        rule, quote, market_cap=None, in_afterhours=True
    ).matched
    quote.session_uptrend = True
    quote.change_pct = 5
    assert not evaluate_afterhours_scan(
        rule, quote, market_cap=None, in_afterhours=True
    ).matched
    quote.change_pct = 8
    quote.regular_change_pct = 10
    assert not evaluate_afterhours_scan(
        rule, quote, market_cap=None, in_afterhours=True
    ).matched
    quote.regular_change_pct = 10.1
    quote.volume = 100_000
    assert not evaluate_afterhours_scan(
        rule, quote, market_cap=None, in_afterhours=True
    ).matched


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
    quote = _quote()
    quote.company = "Acme"
    message = render_message(rule, quote, {"market_cap": 45_000_000, "market_cap_m": 45})
    assert "ABCD" in message
    assert "45.0M" in message
    named = render_message(
        Rule(
            id="premarket",
            name="盘前",
            symbol="*",
            condition=Condition(type=ConditionType.PREMARKET_GAIN, value=20),
            slack_message="{stock_name}   {change_pct:+.1f}%   {price_text}",
        ),
        quote,
    )
    assert named == "Acme   +10.0%   $2.20\n盘前"


def test_message_uses_the_other_session_change():
    quote = _quote()
    quote.company = "Acme"
    quote.regular_change_pct = -3.2
    quote.after_hours_change_pct = 8.5
    afterhours = render_message(
        Rule(
            id="afterhours-smallcap-10pct",
            name="盘后",
            symbol="*",
            condition=Condition(type=ConditionType.AFTERHOURS_GAIN, value=20),
            slack_message="{stock_name}{field_gap}{move_text}{price_text}",
        ),
        quote,
    )
    premarket = render_message(
        Rule(
            id="premarket-smallcap-10pct",
            name="盘前",
            symbol="*",
            condition=Condition(type=ConditionType.PREMARKET_GAIN, value=20),
            slack_message="{stock_name}{field_gap}{move_text}{price_text}",
        ),
        quote,
    )
    assert afterhours == f"Acme{FIELD_GAP}-3.2%{FIELD_GAP}$2.20\n盘后"
    assert premarket == f"Acme{FIELD_GAP}+8.5%{FIELD_GAP}$2.20\n盘前"
    assert "盘后涨幅" not in premarket
    assert "+10.0%" not in afterhours
    assert "+10.0%" not in premarket


def test_leaderboard_line_shows_the_afterhours_gain_and_price():
    quote = _quote(price=1.12, prev_close=1.0)
    quote.company = "Acme"
    quote.change_pct = 18.4
    assert leaderboard_line(quote) == f"ABCD{FIELD_GAP}+18.4%{FIELD_GAP}$1.12"


def test_afterhours_circuit_breaker_needs_regular_gain_and_a_halt():
    rule = Rule(
        id="afterhours-circuit-breaker-day10-ah5",
        name="盘中收盘涨幅超10%并触发涨幅熔断，盘后涨幅超5%，市值不超过1亿美元",
        symbol="*",
        condition=Condition(
            type=ConditionType.AFTERHOURS_GAIN,
            value=5,
            min_regular_change=10,
            require_circuit_breaker=True,
        ),
        slack_message="{stock_name}{field_gap}{move_text}{price_text}",
    )
    quote = _quote(price=1.12, prev_close=1.0)
    quote.company = "Acme"
    quote.change_pct = 6.2
    quote.regular_change_pct = 12.4
    quote.session_circuit_breaker = True

    assert evaluate_afterhours_scan(
        rule, quote, market_cap=None, in_afterhours=True
    ).matched
    capped = rule.model_copy(deep=True)
    capped.condition.max_market_cap = 100_000_000
    assert evaluate_afterhours_scan(
        capped, quote, market_cap=100_000_000, in_afterhours=True
    ).matched
    assert not evaluate_afterhours_scan(
        capped, quote, market_cap=100_000_001, in_afterhours=True
    ).matched
    assert evaluate_afterhours_scan(
        capped, quote, market_cap=None, in_afterhours=True, cap_filtered=True
    ).matched

    quote.session_circuit_breaker = False
    assert not evaluate_afterhours_scan(
        rule, quote, market_cap=None, in_afterhours=True
    ).matched
    quote.session_circuit_breaker = None
    assert not evaluate_afterhours_scan(
        rule, quote, market_cap=None, in_afterhours=True
    ).matched

    quote.session_circuit_breaker = True
    quote.change_pct = 5
    assert not evaluate_afterhours_scan(
        rule, quote, market_cap=None, in_afterhours=True
    ).matched
    quote.change_pct = 6.2
    quote.regular_change_pct = 10
    assert not evaluate_afterhours_scan(
        rule, quote, market_cap=None, in_afterhours=True
    ).matched
    quote.regular_change_pct = 12.4
    assert not evaluate_afterhours_scan(
        rule, quote, market_cap=None, in_afterhours=False
    ).matched

    message = render_message(rule, quote)
    assert message == (
        f"Acme{FIELD_GAP}+12.4%{FIELD_GAP}$1.12\n盘中收盘涨幅超10%并触发涨幅熔断，盘后涨幅超5%，市值不超过1亿美元"
    )
    assert "+6.2%" not in message

    assert "+12.0%" not in message


def test_last_hour_gain_is_strict_and_needs_volume():
    assert hour_change_pct(1.0, 1.2) == 20
    assert hour_change_pct(1.0, 1.25) == 25
    assert hour_change_pct(0, 1.2) is None
    rule = Rule(
        id="regular-last-hour-20",
        name="盘中收盘前1小时突然涨幅超20%",
        symbol="*",
        condition=Condition(
            type=ConditionType.LATE_SESSION_GAIN,
            value=20,
            max_market_cap=100_000_000,
            min_volume=100_000,
        ),
    )
    quote = _quote(price=1.21, prev_close=1.0, volume=100_001)
    quote.change_pct = 21
    assert evaluate_last_hour_scan(
        rule, quote, in_last_hour=True, market_cap=None, cap_filtered=True
    ).matched
    quote.change_pct = 20
    assert not evaluate_last_hour_scan(
        rule, quote, in_last_hour=True, market_cap=None, cap_filtered=True
    ).matched
    quote.change_pct = 25
    quote.volume = 100_000
    assert not evaluate_last_hour_scan(
        rule, quote, in_last_hour=True, market_cap=None, cap_filtered=True
    ).matched
    quote.volume = 100_001
    assert not evaluate_last_hour_scan(
        rule, quote, in_last_hour=False, market_cap=None, cap_filtered=True
    ).matched
    assert not evaluate_last_hour_scan(
        rule, quote, in_last_hour=True, market_cap=100_000_001
    ).matched
    priced = rule.model_copy(deep=True)
    priced.condition.min_price = 0.1
    quote.price = 0.1
    quote.change_pct = 25
    assert not evaluate_last_hour_scan(
        priced, quote, in_last_hour=True, market_cap=None, cap_filtered=True
    ).matched
    quote.price = 0.11
    assert evaluate_last_hour_scan(
        priced, quote, in_last_hour=True, market_cap=None, cap_filtered=True
    ).matched


def _day_spike_rule(session: ConditionType) -> Rule:
    return Rule(
        id="day50-ext5",
        name="过去5天有一天涨幅超50%",
        symbol="*",
        condition=Condition(
            type=session,
            value=5,
            max_market_cap=100_000_000,
            min_volume=100_000,
            min_recent_day_change=50,
            recent_day_count=5,
        ),
    )


def test_recent_day_spike_requires_session_gain_volume_and_a_prior_day():
    rule = _day_spike_rule(ConditionType.AFTERHOURS_GAIN)
    quote = _quote(volume=100_001)
    quote.change_pct = 5.1
    quote.recent_day_spike = True
    assert evaluate_afterhours_scan(
        rule, quote, market_cap=None, in_afterhours=True, cap_filtered=True
    ).matched
    quote.change_pct = 5
    assert not evaluate_afterhours_scan(
        rule, quote, market_cap=None, in_afterhours=True, cap_filtered=True
    ).matched
    quote.change_pct = 8
    quote.recent_day_spike = False
    assert not evaluate_afterhours_scan(
        rule, quote, market_cap=None, in_afterhours=True, cap_filtered=True
    ).matched
    quote.recent_day_spike = None
    assert not evaluate_afterhours_scan(
        rule, quote, market_cap=None, in_afterhours=True, cap_filtered=True
    ).matched
    quote.recent_day_spike = True
    quote.volume = 100_000
    assert not evaluate_afterhours_scan(
        rule, quote, market_cap=None, in_afterhours=True, cap_filtered=True
    ).matched

    premarket = _day_spike_rule(ConditionType.PREMARKET_GAIN)
    quote.volume = 100_001
    quote.change_pct = 5.1
    assert evaluate_premarket_scan(
        premarket, quote, market_cap=100_000_000, in_premarket=True
    ).matched
    assert not evaluate_premarket_scan(
        premarket, quote, market_cap=100_000_001, in_premarket=True
    ).matched
