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


def _reject_low_volume(rule: Rule, quote: Quote) -> RuleEvaluation | None:
    minimum = rule.condition.min_volume
    if minimum is None:
        return None
    volume = quote.volume
    if volume is None or volume <= minimum:
        shown = volume if volume is not None else 0
        return RuleEvaluation(matched=False, reason=f"volume {shown:g} <= {minimum:g}")
    return None


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
    if quote.change_pct < rule.condition.value:
        return RuleEvaluation(
            matched=False,
            reason=f"change {quote.change_pct:.2f}% < {rule.condition.value:.2f}%",
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
    if quote.change_pct < rule.condition.value:
        return RuleEvaluation(
            matched=False,
            reason=f"change {quote.change_pct:.2f}% < {rule.condition.value:.2f}%",
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

    if condition.type in {ConditionType.PREMARKET_GAIN, ConditionType.AFTERHOURS_GAIN}:
        return RuleEvaluation(matched=False, reason="scanner rule")

    return RuleEvaluation(matched=False, reason="unknown condition")


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
    }
    if extra:
        values.update(extra)
    template = rule.slack_message or "{name}: {symbol} is ${price:.2f} ({change_pct:+.2f}% today)"
    try:
        return template.format(**values)
    except (KeyError, ValueError):
        return f"{rule.name}: {values['symbol']} is ${price:.2f}"
