from __future__ import annotations

from pathlib import Path

import yaml

from monitor.models import ConditionType, Quote, Rule, RuleEvaluation, RulesConfig


def load_rules(path: Path) -> RulesConfig:
    if not path.exists():
        raise FileNotFoundError(
            f"Rules file not found: {path}. Copy config/rules.example.yaml to config/rules.yaml."
        )
    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    return RulesConfig.model_validate(raw)


def save_rules(path: Path, config: RulesConfig) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = config.model_dump(mode="json", exclude_none=True)
    with path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(payload, handle, sort_keys=False)


def _reject_without_earlier_spike(rule: Rule, quote: Quote, session_name: str) -> RuleEvaluation | None:
    minimum = rule.condition.min_prior_change
    if minimum is None:
        return None
    if quote.change_pct is None or quote.change_pct <= rule.condition.value:
        shown = "n/a" if quote.change_pct is None else f"{quote.change_pct:.2f}%"
        return RuleEvaluation(
            matched=False,
            reason=f"{session_name} {shown} <= {rule.condition.value:.2f}%",
        )
    if quote.prior_session_spike is not True:
        lag = rule.condition.prior_change_lag_minutes
        return RuleEvaluation(
            matched=False,
            reason=f"no {session_name} gain > {minimum:.2f}% before the last {lag}m",
        )
    return None


def _reject_low_volume(rule: Rule, quote: Quote) -> RuleEvaluation | None:
    minimum = rule.condition.min_volume
    if minimum is None:
        return None
    volume = quote.volume
    if volume is None or volume <= minimum:
        shown = volume if volume is not None else 0
        return RuleEvaluation(matched=False, reason=f"volume {shown:g} <= {minimum:g}")
    return None


def hour_change_pct(baseline: float | None, price: float | None) -> float | None:
    if baseline is None or price is None or baseline <= 0 or price <= 0:
        return None
    return round((price - baseline) / baseline * 100, 4)


def evaluate_last_hour_scan(
    rule: Rule,
    quote: Quote,
    *,
    in_last_hour: bool,
    market_cap: float | None,
    cap_filtered: bool = False,
) -> RuleEvaluation:
    if not in_last_hour:
        return RuleEvaluation(matched=False, reason="not the last regular hour")
    change = quote.change_pct
    if change is None:
        return RuleEvaluation(matched=False, reason="no last-hour change")
    if change <= rule.condition.value:
        return RuleEvaluation(
            matched=False,
            reason=f"last hour {change:.2f}% <= {rule.condition.value:.2f}%",
        )
    low_volume = _reject_low_volume(rule, quote)
    if low_volume is not None:
        return low_volume
    max_cap = rule.condition.max_market_cap if rule.condition.max_market_cap is not None else 100_000_000
    if market_cap is None:
        if cap_filtered:
            return RuleEvaluation(
                matched=True,
                reason=(
                    f"last hour {change:.2f}% > {rule.condition.value:.2f}% "
                    f"and total cap <= ${max_cap:,.0f}"
                ),
            )
        return RuleEvaluation(matched=False, reason="no total market cap")
    if market_cap > max_cap:
        return RuleEvaluation(
            matched=False,
            reason=f"total cap ${market_cap:,.0f} > ${max_cap:,.0f}",
        )
    return RuleEvaluation(
        matched=True,
        reason=f"last hour {change:.2f}% > {rule.condition.value:.2f}% and total cap ${market_cap:,.0f}",
    )


def evaluate_premarket_scan(
    rule: Rule,
    quote: Quote,
    *,
    market_cap: float | None,
    in_premarket: bool,
    cap_filtered: bool = False,
) -> RuleEvaluation:
    if not in_premarket:
        return RuleEvaluation(matched=False, reason="not pre-market")
    if quote.change_pct is None:
        return RuleEvaluation(matched=False, reason="no pre-market change")
    prior = _reject_without_earlier_spike(rule, quote, "pre-market")
    if prior is not None:
        return prior
    day_min = rule.condition.min_recent_day_change
    if rule.condition.min_prior_change is None and day_min is None and quote.change_pct < rule.condition.value:
        return RuleEvaluation(
            matched=False,
            reason=f"change {quote.change_pct:.2f}% < {rule.condition.value:.2f}%",
        )
    if day_min is not None and quote.change_pct <= rule.condition.value:
        return RuleEvaluation(
            matched=False,
            reason=f"pre-market {quote.change_pct:.2f}% <= {rule.condition.value:.2f}%",
        )
    low_volume = _reject_low_volume(rule, quote)
    if low_volume is not None:
        return low_volume
    if day_min is not None and quote.recent_day_spike is not True:
        return RuleEvaluation(
            matched=False,
            reason=(
                f"no day gain > {day_min:.2f}% in the last {rule.condition.recent_day_count} days"
            ),
        )
    max_cap = rule.condition.max_market_cap if rule.condition.max_market_cap is not None else 100_000_000
    if market_cap is None:
        if cap_filtered:
            return RuleEvaluation(
                matched=True,
                reason=(
                    f"pre-market {quote.change_pct:.2f}% >= {rule.condition.value:.2f}% "
                    f"and total cap <= ${max_cap:,.0f}"
                ),
            )
        return RuleEvaluation(matched=False, reason="no total market cap")
    if market_cap > max_cap:
        return RuleEvaluation(
            matched=False,
            reason=f"total cap ${market_cap:,.0f} > ${max_cap:,.0f}",
        )
    return RuleEvaluation(
        matched=True,
        reason=(
            f"pre-market {quote.change_pct:.2f}% >= {rule.condition.value:.2f}% "
            f"and total cap ${market_cap:,.0f}"
        ),
    )


def evaluate_afterhours_scan(
    rule: Rule,
    quote: Quote,
    *,
    market_cap: float | None,
    in_afterhours: bool,
    cap_filtered: bool = False,
) -> RuleEvaluation:
    if not in_afterhours:
        return RuleEvaluation(matched=False, reason="not after-hours")
    if quote.change_pct is None:
        return RuleEvaluation(matched=False, reason="no after-hours change")
    prior = _reject_without_earlier_spike(rule, quote, "after-hours")
    if prior is not None:
        return prior
    regular_min = rule.condition.min_regular_change
    day_min = rule.condition.min_recent_day_change
    strict_session = regular_min is not None or day_min is not None
    if rule.condition.min_prior_change is None and not strict_session:
        if quote.change_pct < rule.condition.value:
            return RuleEvaluation(
                matched=False,
                reason=f"change {quote.change_pct:.2f}% < {rule.condition.value:.2f}%",
            )
    elif rule.condition.min_prior_change is None and quote.change_pct <= rule.condition.value:
        return RuleEvaluation(
            matched=False,
            reason=f"after-hours {quote.change_pct:.2f}% <= {rule.condition.value:.2f}%",
        )
    if regular_min is not None:
        regular = quote.regular_change_pct
        if regular is None or regular <= regular_min:
            shown = "n/a" if regular is None else f"{regular:.2f}%"
            return RuleEvaluation(
                matched=False,
                reason=f"regular change {shown} <= {regular_min:.2f}%",
            )
    low_volume = _reject_low_volume(rule, quote)
    if low_volume is not None:
        return low_volume
    regular_volume_min = rule.condition.min_regular_volume
    if regular_volume_min is not None:
        regular_volume = quote.regular_volume
        if regular_volume is None or regular_volume <= regular_volume_min:
            shown = regular_volume if regular_volume is not None else 0
            return RuleEvaluation(
                matched=False,
                reason=f"regular volume {shown:g} <= {regular_volume_min:g}",
            )
    if rule.condition.require_uptrend and quote.session_uptrend is not True:
        return RuleEvaluation(matched=False, reason="regular session trend is not up")
    if rule.condition.require_circuit_breaker and quote.session_circuit_breaker is not True:
        return RuleEvaluation(matched=False, reason="no regular-session circuit breaker")
    if day_min is not None and quote.recent_day_spike is not True:
        return RuleEvaluation(
            matched=False,
            reason=f"no day gain > {day_min:.2f}% in the last {rule.condition.recent_day_count} days",
        )
    if rule.condition.skip_market_cap or (
        rule.condition.require_circuit_breaker and rule.condition.max_market_cap is None
    ):
        trend = " and regular-session trend up" if rule.condition.require_uptrend else ""
        breaker = (
            " and regular-session circuit breaker" if rule.condition.require_circuit_breaker else ""
        )
        return RuleEvaluation(
            matched=True,
            reason=(
                f"after-hours {quote.change_pct:.2f}% > {rule.condition.value:.2f}%"
                f"{trend}{breaker}"
            ),
        )
    max_cap = rule.condition.max_market_cap if rule.condition.max_market_cap is not None else 100_000_000
    if market_cap is None:
        if cap_filtered:
            return RuleEvaluation(
                matched=True,
                reason=(
                    f"after-hours {quote.change_pct:.2f}% >= {rule.condition.value:.2f}% "
                    f"and total cap <= ${max_cap:,.0f}"
                ),
            )
        return RuleEvaluation(matched=False, reason="no total market cap")
    if market_cap > max_cap:
        return RuleEvaluation(
            matched=False,
            reason=f"total cap ${market_cap:,.0f} > ${max_cap:,.0f}",
        )
    return RuleEvaluation(
        matched=True,
        reason=(
            f"after-hours {quote.change_pct:.2f}% >= {rule.condition.value:.2f}% "
            f"and total cap ${market_cap:,.0f}"
        ),
    )


def evaluate_rule(
    rule: Rule,
    quote: Quote,
    previous_price: float | None,
) -> RuleEvaluation:
    if quote.price is None:
        return RuleEvaluation(matched=False, reason="no price")

    condition = rule.condition
    price = quote.price
    threshold = condition.value

    if condition.type is ConditionType.PRICE_ABOVE:
        matched = price >= threshold
        return RuleEvaluation(
            matched=matched,
            reason=f"price {price:.4f} {'>=' if matched else '<'} {threshold:.4f}",
        )

    if condition.type is ConditionType.PRICE_BELOW:
        matched = price <= threshold
        return RuleEvaluation(
            matched=matched,
            reason=f"price {price:.4f} {'<=' if matched else '>'} {threshold:.4f}",
        )

    if condition.type is ConditionType.PERCENT_CHANGE:
        if quote.change_pct is None:
            return RuleEvaluation(matched=False, reason="no daily change")
        change = quote.change_pct
        matched = change <= threshold if threshold < 0 else change >= threshold
        comparator = "<=" if threshold < 0 else ">="
        return RuleEvaluation(
            matched=matched,
            reason=f"change {change:.2f}% {comparator} {threshold:.2f}%",
        )

    if condition.type is ConditionType.CROSSES_ABOVE:
        if previous_price is None:
            return RuleEvaluation(matched=False, reason="no previous price")
        matched = previous_price < threshold <= price
        return RuleEvaluation(
            matched=matched,
            reason=(
                f"prev {previous_price:.4f} -> {price:.4f} "
                f"{'crossed' if matched else 'did not cross'} {threshold:.4f}"
            ),
        )

    if condition.type is ConditionType.CROSSES_BELOW:
        if previous_price is None:
            return RuleEvaluation(matched=False, reason="no previous price")
        matched = previous_price > threshold >= price
        return RuleEvaluation(
            matched=matched,
            reason=(
                f"prev {previous_price:.4f} -> {price:.4f} "
                f"{'crossed' if matched else 'did not cross'} {threshold:.4f}"
            ),
        )

    if condition.type in {
        ConditionType.PREMARKET_GAIN,
        ConditionType.AFTERHOURS_GAIN,
        ConditionType.LATE_SESSION_GAIN,
    }:
        return RuleEvaluation(matched=False, reason="scanner rule")

    return RuleEvaluation(matched=False, reason="unknown condition")


def leaderboard_line(quote: Quote) -> str:
    change = quote.change_pct if quote.change_pct is not None else 0.0
    return f"{quote.symbol}{FIELD_GAP}{change:+.1f}%{FIELD_GAP}{format_price(quote.price or 0.0)}"


def format_price(price: float) -> str:
    if price >= 1:
        return f"${price:,.2f}"
    text = f"{price:.4f}".rstrip("0").rstrip(".")
    return f"${text}"


def format_volume(volume: float) -> str:
    number = int(volume)
    if number >= 100_000_000:
        return f"{number / 100_000_000:.2f}亿"
    if number >= 10_000:
        wan = number / 10_000
        if abs(wan - round(wan)) < 0.05:
            return f"{wan:.0f}万"
        return f"{wan:.1f}万"
    return f"{number:,}"


_MOVE_LABELS = ("盘后涨幅",)
FIELD_GAP = "\u00a0" * 4


def split_alert_name(prefix: str) -> tuple[str, str]:
    """Keep the company name separate from a session label before the percent."""
    text = prefix.rstrip()
    for label in _MOVE_LABELS:
        if text.endswith(label):
            name = text[: -len(label)].strip()
            if name:
                return name, label
    return text.strip(), ""


def _move_text(rule: Rule, quote: Quote) -> str:
    if rule.condition.type is ConditionType.AFTERHOURS_GAIN:
        value = quote.regular_change_pct
    elif rule.condition.type is ConditionType.PREMARKET_GAIN:
        value = quote.after_hours_change_pct
    else:
        return ""
    if value is None:
        return ""
    return f"{value:+.1f}%{FIELD_GAP}"


def render_message(rule: Rule, quote: Quote, extra: dict | None = None) -> str:
    price = quote.price or 0.0
    change = quote.change_pct if quote.change_pct is not None else 0.0
    values = {
        "id": rule.id,
        "name": rule.name,
        "symbol": quote.symbol or rule.symbol,
        "price": price,
        "change_pct": change,
        "threshold": rule.condition.value,
        "condition": rule.condition.type.value,
        "market_cap": 0.0,
        "market_cap_m": 0.0,
        "volume": quote.volume or 0,
        "stock_name": (quote.company or "").strip() or (quote.symbol or rule.symbol),
    }
    if extra:
        values.update(extra)
    values["field_gap"] = FIELD_GAP
    values["move_text"] = _move_text(rule, quote)
    values["price_text"] = format_price(float(values["price"]))
    values["volume_text"] = format_volume(float(values["volume"]))
    template = rule.slack_message or "{name}: {symbol} is ${price:.2f} ({change_pct:+.2f}% today)"
    try:
        body = template.format(**values)
    except (KeyError, ValueError):
        body = f"{rule.name}: {values['symbol']} is ${price:.2f}"
    reason = rule.name.strip()
    if reason and reason not in body:
        return f"{body}\n{reason}"
    return body
