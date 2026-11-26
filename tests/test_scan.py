from datetime import datetime, timezone

from monitor.marketcap import yesterday_market_cap
from monitor.models import Condition, ConditionType, Quote, Rule, RulesConfig
from monitor.rules import (
    FIELD_GAP,
    evaluate_afterhours_scan,
    evaluate_stable_afterhours,
    evaluate_last_hour_scan,
    evaluate_premarket_scan,
    evaluate_regular_day_scan,
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


def test_scanner_rejects_total_cap_at_or_under_min_market_cap():
    rule = Rule(
        id="premarket-min-cap",
        name="min cap",
        symbol="*",
        condition=Condition(
            type=ConditionType.PREMARKET_GAIN,
            value=10,
            min_market_cap=1_000_000,
            max_market_cap=1_000_000_000,
        ),
    )
    quote = _quote()
    assert evaluate_premarket_scan(
        rule, quote, market_cap=1_000_000, in_premarket=True
    ).matched is False
    assert evaluate_premarket_scan(
        rule, quote, market_cap=1_000_001, in_premarket=True
    ).matched
    assert evaluate_afterhours_scan(
        rule.model_copy(
            update={
                "condition": rule.condition.model_copy(
                    update={"type": ConditionType.AFTERHOURS_GAIN}
                )
            }
        ),
        quote,
        market_cap=500_000,
        in_afterhours=True,
    ).matched is False


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


def test_pullback_needs_current_gain_at_or_above_25_and_an_earlier_spike():
    rule = Rule(
        id="premarket-smallcap-10pct",
        name="盘前",
        symbol="*",
        condition=Condition(
            type=ConditionType.PREMARKET_GAIN,
            value=25,
            max_market_cap=100_000_000,
            min_volume=100_000,
            min_prior_change=30,
            prior_change_lag_minutes=15,
        ),
    )
    quote = _quote(price=1.25, prev_close=1.0, volume=100_001)
    quote.change_pct = 25
    quote.prior_session_spike = True
    assert evaluate_premarket_scan(
        rule, quote, market_cap=None, in_premarket=True, cap_filtered=True
    ).matched
    quote.change_pct = 24.9999
    assert not evaluate_premarket_scan(
        rule, quote, market_cap=None, in_premarket=True, cap_filtered=True
    ).matched
    quote.change_pct = 26
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
    quote.regular_price = 1.8
    quote.after_hours_change_pct = 8.5
    quote.after_hours_price = 2.05
    rule = Rule(
        id="afterhours-smallcap-10pct",
        name="盘后",
        symbol="*",
        condition=Condition(type=ConditionType.AFTERHOURS_GAIN, value=20),
        slack_message="{stock_name}{field_gap}{move_text}{price_text}",
    )
    afterhours = render_message(rule, quote)
    trigger = render_message(rule, quote, headline="trigger")
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
    assert afterhours == f"Acme{FIELD_GAP}-3.2%{FIELD_GAP}$1.80\n盘后"
    assert trigger == f"Acme{FIELD_GAP}+10.0%{FIELD_GAP}$2.20\n盘后"
    assert premarket == f"Acme{FIELD_GAP}+8.5%{FIELD_GAP}$2.05\n盘前"
    assert "盘后涨幅" not in premarket
    assert "+10.0%" not in afterhours
    assert "+10.0%" not in premarket
    quote.premarket_change_pct = 4.5
    quote.premarket_price = 2.4
    regular = render_message(
        Rule(
            id="regular-gain80",
            name="盘中",
            symbol="*",
            group="盘中",
            condition=Condition(type=ConditionType.REGULAR_GAIN, value=80),
            slack_message="{stock_name}{field_gap}{move_text}{price_text}",
        ),
        quote,
    )
    assert regular == f"Acme{FIELD_GAP}+4.5%{FIELD_GAP}$2.40\n盘中"
    assert "$2.20" not in regular


def test_leaderboard_line_keeps_ticker_unless_company_requested():
    from monitor.rules import fade_leaderboard_line, peak_leaderboard_line

    quote = _quote(price=1.12, prev_close=1.0)
    quote.company = "Acme"
    quote.change_pct = 18.4
    assert leaderboard_line(quote) == f"ABCD{FIELD_GAP}+18.4%{FIELD_GAP}$1.12"
    assert leaderboard_line(quote, company=True) == f"Acme{FIELD_GAP}+18.4%{FIELD_GAP}$1.12"
    quote.session_high_pct = 45.2
    assert peak_leaderboard_line(quote) == (
        f"ABCD{FIELD_GAP}+18.4%{FIELD_GAP}+45.2%{FIELD_GAP}$1.12"
    )
    assert peak_leaderboard_line(quote, company=True) == (
        f"Acme{FIELD_GAP}+18.4%{FIELD_GAP}+45.2%{FIELD_GAP}$1.12"
    )
    quote.change_pct = 40.0
    quote.session_high_pct = 70.0
    assert fade_leaderboard_line(quote) == (
        f"ABCD{FIELD_GAP}+40.0%{FIELD_GAP}峰值70%回落30pp{FIELD_GAP}$1.12"
    )
    assert fade_leaderboard_line(quote, company=True) == (
        f"Acme{FIELD_GAP}+40.0%{FIELD_GAP}峰值70%回落30pp{FIELD_GAP}$1.12"
    )


def test_peak_fade_score_ranks_bigger_givebacks_first():
    from monitor.engine import peak_fade_score

    assert peak_fade_score(120.0, 40.0) > peak_fade_score(90.0, 80.0)
    assert peak_fade_score(None, 40.0) == float("-inf")
    assert peak_fade_score(120.0, None) == float("-inf")
    names = ["KEEP", "FADE", "MID"]
    scores = {"FADE": peak_fade_score(150.0, 20.0), "MID": peak_fade_score(100.0, 60.0), "KEEP": peak_fade_score(85.0, 82.0)}
    assert sorted(names, key=lambda s: scores[s], reverse=True) == ["FADE", "MID", "KEEP"]


def test_ah50_matches_sort_by_fade_descending():
    from monitor.engine import peak_fade_score

    quotes = [
        _quote(symbol="SMALL", price=1.5, prev_close=1.0, volume=100_001),
        _quote(symbol="BIG", price=1.4, prev_close=1.0, volume=100_001),
        _quote(symbol="MID", price=1.45, prev_close=1.0, volume=100_001),
    ]
    quotes[0].change_pct = 50.0
    quotes[0].session_high_pct = 60.0  # fade 10
    quotes[1].change_pct = 40.0
    quotes[1].session_high_pct = 80.0  # fade 40
    quotes[2].change_pct = 45.0
    quotes[2].session_high_pct = 70.0  # fade 25
    ordered = sorted(
        quotes,
        key=lambda quote: peak_fade_score(quote.session_high_pct, quote.change_pct),
        reverse=True,
    )
    assert [quote.symbol for quote in ordered] == ["BIG", "MID", "SMALL"]


def test_afterhours_ah50_singles_need_volume_after_the_open():
    rule = Rule(
        id="afterhours-ah50",
        name="盘后冲高回落的股票",
        symbol="*",
        condition=Condition(
            type=ConditionType.AFTERHOURS_GAIN,
            value=50,
            min_fade_from_high=20,
            max_market_cap=500_000_000,
            min_volume=100_000,
        ),
        slack_message="{stock_name}{field_gap}{move_text}{price_text}",
    )
    quote = _quote(price=1.12, prev_close=1.0, volume=100_001)
    quote.change_pct = 52.0
    quote.session_high_pct = 75.0
    assert evaluate_afterhours_scan(
        rule, quote, market_cap=None, in_afterhours=True, cap_filtered=True
    ).matched
    quote.volume = 100_000
    assert not evaluate_afterhours_scan(
        rule, quote, market_cap=None, in_afterhours=True, cap_filtered=True
    ).matched


def test_afterhours_ah50_peak_fade_thresholds():
    from monitor.rules import evaluate_regular_peak_fade

    afterhours = Rule(
        id="afterhours-ah50",
        name="盘后冲高回落的股票",
        symbol="*",
        condition=Condition(
            type=ConditionType.AFTERHOURS_GAIN,
            value=50,
            min_fade_from_high=20,
            max_market_cap=500_000_000,
            min_volume=100_000,
        ),
        slack_message="{stock_name}{field_gap}{move_text}{price_text}",
    )
    premarket = Rule(
        id="premarket-ah50",
        name="盘前冲高回落的股票",
        symbol="*",
        condition=Condition(
            type=ConditionType.PREMARKET_GAIN,
            value=50,
            min_fade_from_high=20,
            max_market_cap=500_000_000,
            min_volume=100_000,
        ),
        slack_message="{stock_name}{field_gap}{move_text}{price_text}",
    )
    regular = Rule(
        id="regular-ah50",
        name="盘中冲高回落的股票",
        symbol="*",
        condition=Condition(
            type=ConditionType.REGULAR_GAIN,
            value=50,
            min_fade_from_high=20,
            max_market_cap=500_000_000,
            min_volume=100_000,
        ),
        slack_message="{stock_name}{field_gap}{move_text}{price_text}",
    )
    quote = _quote(price=1.5, prev_close=1.0, volume=100_001)
    # peak 70 / current 50 → fade 20 → hit
    quote.change_pct = 50.0
    quote.session_high_pct = 70.0
    assert evaluate_afterhours_scan(
        afterhours, quote, market_cap=None, in_afterhours=True, cap_filtered=True
    ).matched
    assert evaluate_premarket_scan(
        premarket, quote, market_cap=None, in_premarket=True, cap_filtered=True
    ).matched
    assert evaluate_regular_peak_fade(
        regular, quote, market_cap=None, cap_filtered=True
    ).matched
    # peak 65 / current 50 → fade 15 → miss
    quote.session_high_pct = 65.0
    assert not evaluate_afterhours_scan(
        afterhours, quote, market_cap=None, in_afterhours=True, cap_filtered=True
    ).matched
    assert not evaluate_premarket_scan(
        premarket, quote, market_cap=None, in_premarket=True, cap_filtered=True
    ).matched
    assert not evaluate_regular_peak_fade(
        regular, quote, market_cap=None, cap_filtered=True
    ).matched
    # peak 65 / current 40 → fade 25 → hit even below 50%
    quote.change_pct = 40.0
    quote.session_high_pct = 65.0
    assert evaluate_afterhours_scan(
        afterhours, quote, market_cap=None, in_afterhours=True, cap_filtered=True
    ).matched
    assert evaluate_premarket_scan(
        premarket, quote, market_cap=None, in_premarket=True, cap_filtered=True
    ).matched
    assert evaluate_regular_peak_fade(
        regular, quote, market_cap=None, cap_filtered=True
    ).matched
    # peak never cleared 50 → miss
    quote.change_pct = 30.0
    quote.session_high_pct = 49.0
    assert not evaluate_afterhours_scan(
        afterhours, quote, market_cap=None, in_afterhours=True, cap_filtered=True
    ).matched
    assert not evaluate_premarket_scan(
        premarket, quote, market_cap=None, in_premarket=True, cap_filtered=True
    ).matched
    assert not evaluate_regular_peak_fade(
        regular, quote, market_cap=None, cap_filtered=True
    ).matched


def test_afterhours_halt_or_day80_matches_either_condition():
    rule = Rule(
        id="afterhours-halt-or-day50",
        name="盘中熔断或涨超50%的股票",
        symbol="*",
        condition=Condition(
            type=ConditionType.AFTERHOURS_GAIN,
            value=5,
            max_market_cap=500_000_000,
            min_volume=100_000,
            top_n=5,
            min_regular_touch=50,
            include_circuit_breaker=True,
            require_circuit_breaker=True,
            min_recent_day_change=50,
            recent_day_count=1,
            intraday_touch=True,
        ),
        slack_message="{stock_name}{field_gap}{move_text}{price_text}",
    )
    quote = _quote(price=1.12, prev_close=1.0, volume=100_001)
    quote.change_pct = 6.2
    quote.session_circuit_breaker = True
    quote.recent_day_spike = False
    assert evaluate_afterhours_scan(
        rule, quote, market_cap=None, in_afterhours=True, cap_filtered=True
    ).matched
    quote.session_circuit_breaker = False
    quote.recent_day_spike = True
    assert evaluate_afterhours_scan(
        rule, quote, market_cap=None, in_afterhours=True, cap_filtered=True
    ).matched
    quote.volume = 100_000
    assert not evaluate_afterhours_scan(
        rule, quote, market_cap=None, in_afterhours=True, cap_filtered=True
    ).matched
    quote.volume = 100_001
    quote.recent_day_spike = False
    assert not evaluate_afterhours_scan(
        rule, quote, market_cap=None, in_afterhours=True, cap_filtered=True
    ).matched


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
    quote.regular_price = 1.05
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
        f"Acme{FIELD_GAP}+12.4%{FIELD_GAP}$1.05\n盘中收盘涨幅超10%并触发涨幅熔断，盘后涨幅超5%，市值不超过1亿美元"
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


def test_day_gain_tiers_or_across_50_and_100():
    from monitor.models import DayGainTier

    tiers = [
        DayGainTier(min_recent_day_change=50, value=10),
        DayGainTier(min_recent_day_change=100, value=5),
    ]
    afterhours = Rule(
        id="afterhours-halt-or-5d-boost",
        name="merged",
        symbol="*",
        condition=Condition(
            type=ConditionType.AFTERHOURS_GAIN,
            value=5,
            max_market_cap=1_000_000_000,
            min_volume=10_000,
            day_gain_tiers=tiers,
            recent_day_count=5,
        ),
    )
    premarket = Rule(
        id="premarket-halt-or-5d-boost",
        name="merged",
        symbol="*",
        condition=Condition(
            type=ConditionType.PREMARKET_GAIN,
            value=5,
            max_market_cap=1_000_000_000,
            min_volume=10_000,
            day_gain_tiers=tiers,
            recent_day_count=5,
        ),
    )
    quote = _quote(volume=10_001)
    # 50% day + session 10.1% → hit
    quote.change_pct = 10.1
    quote.recent_day_gain_floors = [50.0]
    assert evaluate_afterhours_scan(
        afterhours, quote, market_cap=None, in_afterhours=True, cap_filtered=True
    ).matched
    assert evaluate_premarket_scan(
        premarket, quote, market_cap=None, in_premarket=True, cap_filtered=True
    ).matched
    # 50% day + session 5.1% → miss (need >10)
    quote.change_pct = 5.1
    assert not evaluate_afterhours_scan(
        afterhours, quote, market_cap=None, in_afterhours=True, cap_filtered=True
    ).matched
    assert not evaluate_premarket_scan(
        premarket, quote, market_cap=None, in_premarket=True, cap_filtered=True
    ).matched
    # 100% day + session 5.1% → hit
    quote.change_pct = 5.1
    quote.recent_day_gain_floors = [100.0]
    assert evaluate_afterhours_scan(
        afterhours, quote, market_cap=None, in_afterhours=True, cap_filtered=True
    ).matched
    assert evaluate_premarket_scan(
        premarket, quote, market_cap=None, in_premarket=True, cap_filtered=True
    ).matched
    # 100% day + session 5% → miss
    quote.change_pct = 5.0
    assert not evaluate_afterhours_scan(
        afterhours, quote, market_cap=None, in_afterhours=True, cap_filtered=True
    ).matched
    assert not evaluate_premarket_scan(
        premarket, quote, market_cap=None, in_premarket=True, cap_filtered=True
    ).matched
    # 100% day + current 3.8% but session high 7.9% → still hit (touch, not only last)
    quote.change_pct = 3.8
    quote.session_high_pct = 7.9
    quote.recent_day_gain_floors = [100.0]
    assert evaluate_afterhours_scan(
        afterhours, quote, market_cap=None, in_afterhours=True, cap_filtered=True
    ).matched
    assert evaluate_premarket_scan(
        premarket, quote, market_cap=None, in_premarket=True, cap_filtered=True
    ).matched


def test_halt_or_5d_boost_breaker_leg_without_tier():
    """Merged rule: breaker + session>5% matches even when no day_gain tier."""
    from monitor.models import DayGainTier

    tiers = [
        DayGainTier(min_recent_day_change=50, value=10),
        DayGainTier(min_recent_day_change=100, value=5),
    ]
    common = dict(
        value=5,
        max_market_cap=1_000_000_000,
        min_volume=10_000,
        min_regular_touch=50,
        include_circuit_breaker=True,
        require_circuit_breaker=True,
        day_gain_tiers=tiers,
        recent_day_count=5,
        intraday_touch=True,
    )
    afterhours = Rule(
        id="afterhours-halt-or-5d-boost",
        name="merged",
        symbol="*",
        condition=Condition(type=ConditionType.AFTERHOURS_GAIN, **common),
    )
    premarket = Rule(
        id="premarket-halt-or-5d-boost",
        name="merged",
        symbol="*",
        condition=Condition(type=ConditionType.PREMARKET_GAIN, **common),
    )
    regular = Rule(
        id="regular-halt-or-5d-boost",
        name="merged",
        symbol="*",
        condition=Condition(type=ConditionType.REGULAR_GAIN, **common),
    )
    quote = _quote(volume=10_001)
    quote.change_pct = 5.1
    quote.session_circuit_breaker = True
    quote.recent_day_gain_floors = []
    quote.recent_day_spike = False
    assert evaluate_afterhours_scan(
        afterhours, quote, market_cap=None, in_afterhours=True, cap_filtered=True
    ).matched
    assert evaluate_premarket_scan(
        premarket, quote, market_cap=None, in_premarket=True, cap_filtered=True
    ).matched
    assert evaluate_regular_day_scan(
        regular, quote, market_cap=None, cap_filtered=True
    ).matched
    # Breaker but session only 3% → miss
    quote.change_pct = 3.0
    assert not evaluate_afterhours_scan(
        afterhours, quote, market_cap=None, in_afterhours=True, cap_filtered=True
    ).matched
    assert not evaluate_premarket_scan(
        premarket, quote, market_cap=None, in_premarket=True, cap_filtered=True
    ).matched
    assert not evaluate_regular_day_scan(
        regular, quote, market_cap=None, cap_filtered=True
    ).matched
    # No breaker, only 100% day + session >5% → hit
    quote.session_circuit_breaker = False
    quote.change_pct = 5.1
    quote.recent_day_gain_floors = [100.0]
    assert evaluate_afterhours_scan(
        afterhours, quote, market_cap=None, in_afterhours=True, cap_filtered=True
    ).matched
    assert evaluate_premarket_scan(
        premarket, quote, market_cap=None, in_premarket=True, cap_filtered=True
    ).matched
    assert evaluate_regular_day_scan(
        regular, quote, market_cap=None, cap_filtered=True
    ).matched


def test_fade_leaders_rank_by_giveback():
    from monitor.engine import MonitorEngine

    quotes = [
        _quote(symbol="SMALL", price=1.5, prev_close=1.0, volume=100_001),
        _quote(symbol="BIG", price=1.4, prev_close=1.0, volume=100_001),
        _quote(symbol="MID", price=1.45, prev_close=1.0, volume=100_001),
        _quote(symbol="THIN", price=1.2, prev_close=1.0, volume=100),
    ]
    quotes[0].change_pct = 50.0
    quotes[0].session_high_pct = 60.0
    quotes[1].change_pct = 40.0
    quotes[1].session_high_pct = 80.0
    quotes[2].change_pct = 45.0
    quotes[2].session_high_pct = 70.0
    quotes[3].change_pct = 10.0
    quotes[3].session_high_pct = 90.0
    ordered = MonitorEngine._fade_leaders(quotes, limit=10, min_volume=10_000, min_fade=0)
    assert [quote.symbol for quote in ordered] == ["BIG", "MID", "SMALL"]


def test_peak_fade_board_is_not_a_single_fade_rule():
    from monitor.engine import (
        MonitorEngine,
        _is_peak_fade_board,
        _is_peak_fade_rule,
        _sorts_ths_by_peak_fade,
    )
    from monitor.rules import regular_halt50_fade_ths_groups

    rule = Rule(
        id="regular-halt50-fade-board",
        name="盘中熔断或50%回落的股票",
        symbol="*",
        condition=Condition(
            type=ConditionType.REGULAR_GAIN,
            value=0,
            top_n=50,
            board_interval_minutes=5,
            min_regular_touch=50,
            include_circuit_breaker=True,
            min_fade_from_high=0,
        ),
    )
    assert _is_peak_fade_board(rule)
    assert not _is_peak_fade_rule(rule)
    assert _sorts_ths_by_peak_fade(rule)
    assert MonitorEngine._ths_group_names_for_rule(rule) == list(
        regular_halt50_fade_ths_groups()
    )


def test_partition_halt50_fade_symbols(tmp_path):
    from datetime import datetime, timezone

    from monitor.engine import MonitorEngine
    from monitor.session import now_et
    from monitor.store import Store

    store = Store(tmp_path)
    day = now_et().date().isoformat()
    seen = datetime(2026, 11, 25, 15, 0, tzinfo=timezone.utc)
    store.note_spike(day, "upward_halt", "HALT", seen)
    store.note_spike(day, "upward_halt", "BOTH", seen)
    engine = MonitorEngine.__new__(MonitorEngine)
    engine.store = store
    halt_syms, fifty_syms = engine._partition_halt50_fade_symbols(
        ["HALT", "FIFTY", "BOTH", "ONLY50"]
    )
    assert halt_syms == ["HALT", "BOTH"]
    assert fifty_syms == ["FIFTY", "ONLY50"]


def test_peak_rebound_rule_classifier():
    from monitor.engine import (
        MonitorEngine,
        _is_peak_fade_board,
        _is_peak_fade_rule,
        _is_peak_rebound_rule,
    )
    from monitor.rules import (
        REGULAR_HALT50_REBOUND_THS_50,
        REGULAR_HALT50_REBOUND_THS_HALT,
        regular_halt50_rebound_ths_group,
        regular_halt50_rebound_ths_groups,
    )

    rule = Rule(
        id="regular-halt50-rebound",
        name="盘中熔断或50%回升的股票",
        symbol="*",
        condition=Condition(
            type=ConditionType.REGULAR_GAIN,
            value=0,
            min_regular_touch=50,
            include_circuit_breaker=True,
            min_rebound_from_trough=10,
            late_session_hours=3.75,
        ),
    )
    assert _is_peak_rebound_rule(rule)
    assert not _is_peak_fade_rule(rule)
    assert not _is_peak_fade_board(rule)
    assert MonitorEngine._ths_group_names_for_rule(rule) == list(
        regular_halt50_rebound_ths_groups()
    )
    assert (
        regular_halt50_rebound_ths_group("HALT", halted={"HALT", "BOTH"})
        == REGULAR_HALT50_REBOUND_THS_HALT
    )
    assert (
        regular_halt50_rebound_ths_group("BOTH", halted={"HALT", "BOTH"})
        == REGULAR_HALT50_REBOUND_THS_HALT
    )
    assert (
        regular_halt50_rebound_ths_group("FIFTY", halted={"HALT", "BOTH"})
        == REGULAR_HALT50_REBOUND_THS_50
    )

    ah = Rule(
        id="afterhours-50-rebound",
        name="盘后涨超50%回升的股票",
        symbol="*",
        condition=Condition(
            type=ConditionType.AFTERHOURS_GAIN,
            value=50,
            min_rebound_from_trough=10,
            late_session="afterhours",
            late_session_hours=2.5,
        ),
    )
    pm = Rule(
        id="premarket-50-rebound",
        name="盘前涨超50%回升的股票",
        symbol="*",
        condition=Condition(
            type=ConditionType.PREMARKET_GAIN,
            value=50,
            min_rebound_from_trough=10,
            late_session="premarket",
            late_session_hours=3.25,
        ),
    )
    assert _is_peak_rebound_rule(ah)
    assert _is_peak_rebound_rule(pm)
    assert not _is_peak_fade_rule(ah)
    assert not _is_peak_fade_rule(pm)


def test_peak_rebound_tracks_trough_before_late_window(tmp_path):
    """Trough updates all session; matches only after late window."""
    import asyncio
    from datetime import datetime
    from unittest.mock import AsyncMock, patch
    from zoneinfo import ZoneInfo

    from monitor.engine import MonitorEngine
    from monitor.models import Quote
    from monitor.store import Store

    ET = ZoneInfo("America/New_York")
    store = Store(tmp_path)
    engine = MonitorEngine.__new__(MonitorEngine)
    engine.store = store
    engine.verbose = False
    engine.moomoo = object()
    engine._trend_moomoo = None
    engine._scan_log = {}

    rule = Rule(
        id="regular-halt50-rebound",
        name="盘中熔断或50%回升的股票",
        symbol="*",
        condition=Condition(
            type=ConditionType.REGULAR_GAIN,
            value=0,
            min_regular_touch=50,
            include_circuit_breaker=True,
            min_rebound_from_trough=10,
            late_session="regular",
            late_session_hours=3.75,
            min_volume=10_000,
        ),
    )
    day = "2026-11-23"
    store.note_day_gains(day, 50, ["BNAI"])

    # prev_close=5, daily_high=9 → touch +80% (matches change_pct peak)
    quote_high = Quote(
        symbol="BNAI",
        price=9.0,
        change_pct=80.0,
        volume=50_000,
        daily_high=9.0,
        prev_close=5.0,
    )
    quote_dip = Quote(
        symbol="BNAI",
        price=7.5,
        change_pct=50.0,
        volume=50_000,
        daily_high=9.0,
        prev_close=5.0,
    )
    quote_lift = Quote(
        symbol="BNAI",
        price=8.5,
        change_pct=70.0,
        volume=50_000,
        daily_high=9.0,
        prev_close=5.0,
    )

    async def run() -> None:
        early = datetime(2026, 11, 23, 11, 40, tzinfo=ET)  # before 12:15
        late = datetime(2026, 11, 23, 13, 30, tzinfo=ET)

        with (
            patch("monitor.engine.now_et", return_value=early),
            patch("monitor.engine.session_late_window", return_value=False),
            patch.object(
                engine,
                "_halt_or_day_pool_symbols",
                return_value=["BNAI"],
            ),
            patch.object(
                engine,
                "_moomoo_snapshots",
                new=AsyncMock(return_value={"BNAI": quote_high}),
            ),
        ):
            matched = await engine._match_peak_rebound_rule(rule)
        assert matched == []
        assert store.peak_trough(day, "BNAI") == (80.0, 80.0)

        with (
            patch("monitor.engine.now_et", return_value=early),
            patch("monitor.engine.session_late_window", return_value=False),
            patch.object(
                engine,
                "_halt_or_day_pool_symbols",
                return_value=["BNAI"],
            ),
            patch.object(
                engine,
                "_moomoo_snapshots",
                new=AsyncMock(return_value={"BNAI": quote_dip}),
            ),
        ):
            matched = await engine._match_peak_rebound_rule(rule)
        assert matched == []
        assert store.peak_trough(day, "BNAI") == (80.0, 50.0)

        with (
            patch("monitor.engine.now_et", return_value=late),
            patch("monitor.engine.session_late_window", return_value=True),
            patch.object(
                engine,
                "_halt_or_day_pool_symbols",
                return_value=["BNAI"],
            ),
            patch.object(
                engine,
                "_moomoo_snapshots",
                new=AsyncMock(return_value={"BNAI": quote_lift}),
            ),
        ):
            matched = await engine._match_peak_rebound_rule(rule)
        assert [q.symbol for q in matched] == ["BNAI"]
        assert store.peak_trough(day, "BNAI") == (80.0, 50.0)

    asyncio.run(run())


def test_near_peak_rule_classifier():
    from monitor.engine import _is_near_peak_rule, _is_peak_fade_rule, _is_peak_rebound_rule

    rule = Rule(
        id="regular-near-peak-35",
        name="盘中涨超35%接近最高的股票",
        symbol="*",
        condition=Condition(
            type=ConditionType.REGULAR_GAIN,
            value=35,
            max_fade_from_high=10,
            late_session_hours=3.75,
            min_volume=10_000,
        ),
    )
    assert _is_near_peak_rule(rule)
    assert not _is_peak_fade_rule(rule)
    assert not _is_peak_rebound_rule(rule)


def test_halt_or_5d_boost_uses_ths_rule_group():
    from monitor.engine import (
        MonitorEngine,
        _is_halt_or_day_rule,
        _skips_ths_rule_group,
        _sorts_ths_by_peak_fade,
    )
    from monitor.models import DayGainTier
    from monitor.rules import halt_or_5d_ah_ths_groups

    rule = Rule(
        id="regular-halt-or-5d-boost",
        name="熔断或5日涨超50%后续超10%或曾涨超100%后续超5%的股票",
        symbol="*",
        condition=Condition(
            type=ConditionType.REGULAR_GAIN,
            value=5,
            min_regular_touch=50,
            include_circuit_breaker=True,
            require_circuit_breaker=True,
            day_gain_tiers=[
                DayGainTier(min_recent_day_change=50, value=10),
                DayGainTier(min_recent_day_change=100, value=5),
            ],
            recent_day_count=5,
            intraday_touch=True,
        ),
    )
    assert _is_halt_or_day_rule(rule)
    assert not _skips_ths_rule_group(rule)
    assert not _sorts_ths_by_peak_fade(rule)
    board = Rule(
        id="afterhours-5d50-board",
        name="5日曾涨超50%的股票",
        symbol="*",
        condition=Condition(
            type=ConditionType.AFTERHOURS_GAIN,
            value=0,
            top_n=50,
            min_recent_day_change=50,
            recent_day_count=5,
        ),
    )
    assert not _skips_ths_rule_group(board)
    ah = Rule(
        id="afterhours-halt-or-5d-boost",
        name="熔断或5日盘后的股票",
        symbol="*",
        condition=Condition(
            type=ConditionType.AFTERHOURS_GAIN,
            value=5,
            min_regular_touch=50,
            include_circuit_breaker=True,
            require_circuit_breaker=True,
            day_gain_tiers=[
                DayGainTier(min_recent_day_change=50, value=10),
                DayGainTier(min_recent_day_change=100, value=5),
            ],
            recent_day_count=5,
            intraday_touch=True,
        ),
    )
    assert MonitorEngine._ths_group_names_for_rule(ah) == list(halt_or_5d_ah_ths_groups())
    assert MonitorEngine._ths_group_names_for_rule(rule) == [rule.name]


def test_halt_or_5d_ths_group_priority():
    from monitor.models import DayGainTier
    from monitor.rules import (
        HALT_OR_5D_AH_THS_PAST_100,
        HALT_OR_5D_AH_THS_PAST_50,
        HALT_OR_5D_AH_THS_TODAY_100_OR_HALT,
        HALT_OR_5D_AH_THS_TODAY_50,
        halt_or_5d_ths_group,
    )

    rule = Rule(
        id="afterhours-halt-or-5d-boost",
        name="熔断或5日盘后的股票",
        symbol="*",
        condition=Condition(
            type=ConditionType.AFTERHOURS_GAIN,
            value=5,
            day_gain_tiers=[
                DayGainTier(min_recent_day_change=50, value=10),
                DayGainTier(min_recent_day_change=100, value=5),
            ],
            recent_day_count=5,
        ),
    )
    # Past 100
    q = Quote(symbol="A", change_pct=6.0, recent_day_gain_floors=[100.0])
    assert halt_or_5d_ths_group(rule, q, today_floors=set()) == HALT_OR_5D_AH_THS_PAST_100
    # Past 50
    q = Quote(symbol="B", change_pct=11.0, recent_day_gain_floors=[50.0])
    assert halt_or_5d_ths_group(rule, q, today_floors=set()) == HALT_OR_5D_AH_THS_PAST_50
    # Today 50
    q = Quote(symbol="C", change_pct=11.0, recent_day_gain_floors=[50.0])
    assert (
        halt_or_5d_ths_group(rule, q, today_floors={50.0}) == HALT_OR_5D_AH_THS_TODAY_50
    )
    # Today 100
    q = Quote(symbol="D", change_pct=6.0, recent_day_gain_floors=[100.0, 50.0])
    assert (
        halt_or_5d_ths_group(rule, q, today_floors={100.0, 50.0})
        == HALT_OR_5D_AH_THS_TODAY_100_OR_HALT
    )
    # Halt only
    q = Quote(
        symbol="E",
        change_pct=6.0,
        session_circuit_breaker=True,
        recent_day_gain_floors=[],
    )
    assert (
        halt_or_5d_ths_group(rule, q, today_floors=set())
        == HALT_OR_5D_AH_THS_TODAY_100_OR_HALT
    )
    # Past 100 beats today 50
    q = Quote(symbol="F", change_pct=12.0, recent_day_gain_floors=[100.0, 50.0])
    assert (
        halt_or_5d_ths_group(rule, q, today_floors={50.0}) == HALT_OR_5D_AH_THS_PAST_100
    )
    # Premarket rule does not split
    pm = rule.model_copy(update={"id": "premarket-halt-or-5d-boost"})
    assert halt_or_5d_ths_group(pm, q, today_floors=set()) is None


def test_session50_singles_use_ths_rule_group_and_live_yaml():
    from pathlib import Path

    from monitor.engine import _skips_ths_rule_group
    from monitor.rules import load_rules

    with_skip = Rule(
        id="afterhours-session50",
        name="盘后涨超50%的股票",
        symbol="*",
        skip_ths_group=True,
        once_per_session=True,
        condition=Condition(
            type=ConditionType.AFTERHOURS_GAIN,
            value=50,
            min_market_cap=1_000_000,
            max_market_cap=1_000_000_000,
            min_volume=10_000,
        ),
    )
    assert _skips_ths_rule_group(with_skip)

    root = Path(__file__).resolve().parent.parent
    config = load_rules(root / "config" / "rules.yaml")
    session50 = [rule for rule in config.rules if rule.id.endswith("-session50")]
    assert session50 == []
    assert {rule.id for rule in config.rules} >= {
        "afterhours-top5",
        "afterhours-halt-or-5d-boost",
        "afterhours-halt50-or-5d50-board",
        "premarket-halt-or-5d-boost",
    }
