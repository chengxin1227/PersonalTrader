from datetime import datetime, timezone

from monitor.models import Condition, ConditionType, Quote, Rule
from monitor.rules import evaluate_rule, render_message


def _quote(price: float, change_pct: float | None = 1.0) -> Quote:
    return Quote(
        symbol="AAPL",
        price=price,
        change_pct=change_pct,
        prev_close=100.0,
        updated_at=datetime.now(timezone.utc),
    )


def _rule(condition_type: ConditionType, value: float) -> Rule:
    return Rule(
        id="test",
        name="Test rule",
        symbol="AAPL",
        condition=Condition(type=condition_type, value=value),
    )


def test_price_above_matches_at_or_over_threshold():
    rule = _rule(ConditionType.PRICE_ABOVE, 200)
    assert evaluate_rule(rule, _quote(200), None).matched
    assert evaluate_rule(rule, _quote(201), None).matched
    assert not evaluate_rule(rule, _quote(199.99), None).matched


def test_price_below_matches_at_or_under_threshold():
    rule = _rule(ConditionType.PRICE_BELOW, 150)
    assert evaluate_rule(rule, _quote(150), None).matched
    assert evaluate_rule(rule, _quote(149), None).matched
    assert not evaluate_rule(rule, _quote(151), None).matched


def test_percent_change_respects_sign():
    down = _rule(ConditionType.PERCENT_CHANGE, -2.0)
    up = _rule(ConditionType.PERCENT_CHANGE, 3.0)
    assert evaluate_rule(down, _quote(98, -2.1), None).matched
    assert not evaluate_rule(down, _quote(99, -1.5), None).matched
    assert evaluate_rule(up, _quote(104, 3.0), None).matched
    assert not evaluate_rule(up, _quote(102, 2.0), None).matched


def test_crosses_need_previous_price():
    above = _rule(ConditionType.CROSSES_ABOVE, 100)
    below = _rule(ConditionType.CROSSES_BELOW, 100)
    assert not evaluate_rule(above, _quote(101), None).matched
    assert evaluate_rule(above, _quote(101), 99).matched
    assert not evaluate_rule(above, _quote(101), 100.5).matched
    assert evaluate_rule(below, _quote(99), 101).matched
    assert not evaluate_rule(below, _quote(99), 98).matched


def test_render_message_uses_template_fields():
    rule = _rule(ConditionType.PRICE_ABOVE, 200)
    rule.slack_message = "{symbol} {price:.2f} {change_pct:+.1f}"
    assert render_message(rule, _quote(210.5, 2.25)) == "AAPL 210.50 +2.2\nTest rule"
