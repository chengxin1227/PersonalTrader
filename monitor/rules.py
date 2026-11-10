from __future__ import annotations

from pathlib import Path

import yaml

from monitor.models import (
    ConditionType,
    DayGainTier,
    Quote,
    Rule,
    RuleEvaluation,
    RulesConfig,
)


def day_gain_tiers_for(rule: Rule) -> list[DayGainTier]:
    """OR branches for prior-day + session gain; falls back to a single-tier rule."""
    if rule.condition.day_gain_tiers:
        return list(rule.condition.day_gain_tiers)
    if rule.condition.min_recent_day_change is not None:
        return [
            DayGainTier(
                min_recent_day_change=rule.condition.min_recent_day_change,
                value=rule.condition.value,
            )
        ]
    return []


def matched_day_gain_tier(rule: Rule, quote: Quote) -> DayGainTier | None:
    """First tier whose prior-day floor and session gain both clear."""
    tiers = day_gain_tiers_for(rule)
    if not tiers:
        return None
    floors = {float(item) for item in (quote.recent_day_gain_floors or [])}
    if not floors and quote.recent_day_spike is True and rule.condition.min_recent_day_change is not None:
        floors.add(float(rule.condition.min_recent_day_change))
    if quote.change_pct is None:
        return None
    for tier in tiers:
        if tier.min_recent_day_change not in floors:
            continue
        if quote.change_pct > tier.value:
            return tier
    return None


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
    if quote.change_pct is None or quote.change_pct < rule.condition.value:
        shown = "n/a" if quote.change_pct is None else f"{quote.change_pct:.2f}%"
        return RuleEvaluation(
            matched=False,
            reason=f"{session_name} {shown} < {rule.condition.value:.2f}%",
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


def price_age_seconds_ago(
    samples: list[tuple[float, float]],
    *,
    now: float,
    window_seconds: int,
) -> tuple[float, float] | None:
    """Newest sample at least `window_seconds` old: (timestamp, price)."""
    cutoff = now - window_seconds
    chosen: tuple[float, float] | None = None
    for stamp, price in samples:
        if stamp <= cutoff and price > 0:
            chosen = (stamp, price)
    return chosen


def trim_price_samples(
    samples: list[tuple[float, float]],
    *,
    now: float,
    keep_seconds: int,
) -> list[tuple[float, float]]:
    floor = now - keep_seconds
    return [(stamp, price) for stamp, price in samples if stamp >= floor]


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
    min_price = rule.condition.min_price
    if min_price is not None and (quote.price is None or quote.price <= min_price):
        shown = 0 if quote.price is None else quote.price
        return RuleEvaluation(matched=False, reason=f"price {shown:g} <= {min_price:g}")
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
    fade = _reject_without_peak_fade(rule, quote)
    if fade is not None:
        return fade
    day_tiers = day_gain_tiers_for(rule)
    tiered_day = bool(rule.condition.day_gain_tiers)
    day_min = rule.condition.min_recent_day_change
    matched_tier = matched_day_gain_tier(rule, quote) if day_tiers else None
    if rule.condition.min_fade_from_high is None and not tiered_day:
        if (
            rule.condition.min_prior_change is None
            and day_min is None
            and quote.change_pct < rule.condition.value
        ):
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
    if tiered_day:
        if matched_tier is None:
            parts = [
                f"day>{tier.min_recent_day_change:g}% & PM>{tier.value:g}%"
                for tier in day_tiers
            ]
            return RuleEvaluation(
                matched=False,
                reason="no tier matched (" + " or ".join(parts) + ")",
            )
    elif day_min is not None and quote.recent_day_spike is not True:
        return RuleEvaluation(
            matched=False,
            reason=(
                f"no day gain > {day_min:.2f}% in the last {rule.condition.recent_day_count} days"
            ),
        )
    max_cap = rule.condition.max_market_cap if rule.condition.max_market_cap is not None else 100_000_000
    if (
        rule.condition.min_fade_from_high is not None
        and quote.session_high_pct is not None
        and quote.change_pct is not None
    ):
        peak = quote.session_high_pct
        fade_pp = peak - quote.change_pct
        session_text = (
            f"pre-market peak {peak:.2f}% (≥{rule.condition.value:g}%) "
            f"faded {fade_pp:.2f}pp to {quote.change_pct:.2f}%"
        )
    else:
        session_text = (
            f"pre-market {quote.change_pct:.2f}% > {matched_tier.value:g}% "
            f"(day>{matched_tier.min_recent_day_change:g}%)"
            if matched_tier is not None
            else f"pre-market {quote.change_pct:.2f}% >= {rule.condition.value:.2f}%"
        )
    if market_cap is None:
        if cap_filtered:
            return RuleEvaluation(
                matched=True,
                reason=f"{session_text} and total cap <= ${max_cap:,.0f}",
            )
        return RuleEvaluation(matched=False, reason="no total market cap")
    if market_cap > max_cap:
        return RuleEvaluation(
            matched=False,
            reason=f"total cap ${market_cap:,.0f} > ${max_cap:,.0f}",
        )
    return RuleEvaluation(
        matched=True,
        reason=f"{session_text} and total cap ${market_cap:,.0f}",
    )


def evaluate_regular_peak_fade(
    rule: Rule,
    quote: Quote,
    *,
    market_cap: float | None = None,
    cap_filtered: bool = False,
) -> RuleEvaluation:
    """Regular-session peak-fade: once-high >= value and peak − current >= fade pp."""
    if quote.change_pct is None:
        return RuleEvaluation(matched=False, reason="no regular change")
    fade = _reject_without_peak_fade(rule, quote)
    if fade is not None:
        return fade
    low_volume = _reject_low_volume(rule, quote)
    if low_volume is not None:
        return low_volume
    max_cap = rule.condition.max_market_cap if rule.condition.max_market_cap is not None else 100_000_000
    peak = quote.session_high_pct
    fade_pp = (peak - quote.change_pct) if peak is not None else 0.0
    session_text = (
        f"regular peak {peak:.2f}% (≥{rule.condition.value:g}%) "
        f"faded {fade_pp:.2f}pp to {quote.change_pct:.2f}%"
        if peak is not None
        else f"regular {quote.change_pct:.2f}%"
    )
    if market_cap is None:
        if cap_filtered:
            return RuleEvaluation(
                matched=True,
                reason=f"{session_text} and total cap <= ${max_cap:,.0f}",
            )
        return RuleEvaluation(matched=False, reason="no total market cap")
    if market_cap > max_cap:
        return RuleEvaluation(
            matched=False,
            reason=f"total cap ${market_cap:,.0f} > ${max_cap:,.0f}",
        )
    return RuleEvaluation(
        matched=True,
        reason=f"{session_text} and total cap ${market_cap:,.0f}",
    )


def _reject_without_peak_fade(rule: Rule, quote: Quote) -> RuleEvaluation | None:
    """Peak-fade rules: once-high >= value and peak − current >= min_fade_from_high."""
    fade_min = rule.condition.min_fade_from_high
    if fade_min is None:
        return None
    peak = quote.session_high_pct
    if peak is None:
        return RuleEvaluation(matched=False, reason="no session peak")
    if peak < rule.condition.value:
        return RuleEvaluation(
            matched=False,
            reason=f"peak {peak:.2f}% < {rule.condition.value:.2f}%",
        )
    if quote.change_pct is None:
        return RuleEvaluation(matched=False, reason="no session change")
    fade = peak - quote.change_pct
    if fade < fade_min:
        return RuleEvaluation(
            matched=False,
            reason=f"fade {fade:.2f}pp < {fade_min:g}pp (peak {peak:.2f}% → {quote.change_pct:.2f}%)",
        )
    return None


def _afterhours_match_reason(
    rule: Rule,
    quote: Quote,
    *,
    suffix: str = "",
    tier: DayGainTier | None = None,
) -> str:
    fade_min = rule.condition.min_fade_from_high
    if fade_min is not None and quote.session_high_pct is not None and quote.change_pct is not None:
        peak = quote.session_high_pct
        fade = peak - quote.change_pct
        return (
            f"after-hours peak {peak:.2f}% (≥{rule.condition.value:g}%) "
            f"faded {fade:.2f}pp to {quote.change_pct:.2f}%{suffix}"
        )
    if tier is not None and quote.change_pct is not None:
        return (
            f"after-hours {quote.change_pct:.2f}% > {tier.value:g}% "
            f"after day gain > {tier.min_recent_day_change:g}%{suffix}"
        )
    return (
        f"after-hours {quote.change_pct:.2f}% >= {rule.condition.value:.2f}%{suffix}"
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
    fade = _reject_without_peak_fade(rule, quote)
    if fade is not None:
        return fade
    prior = _reject_without_earlier_spike(rule, quote, "after-hours")
    if prior is not None:
        return prior
    regular_min = rule.condition.min_regular_change
    day_tiers = day_gain_tiers_for(rule)
    tiered_day = bool(rule.condition.day_gain_tiers)
    day_min = rule.condition.min_recent_day_change
    matched_tier = matched_day_gain_tier(rule, quote) if day_tiers else None
    strict_session = regular_min is not None or day_min is not None or tiered_day
    if rule.condition.min_fade_from_high is None and not tiered_day:
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
    wants_breaker = rule.condition.require_circuit_breaker
    wants_day = day_min is not None or tiered_day
    has_breaker = quote.session_circuit_breaker is True
    has_day = matched_tier is not None if tiered_day else quote.recent_day_spike is True
    if tiered_day and matched_tier is None and not wants_breaker:
        parts = [
            f"day>{tier.min_recent_day_change:g}% & AH>{tier.value:g}%"
            for tier in day_tiers
        ]
        return RuleEvaluation(
            matched=False,
            reason="no tier matched (" + " or ".join(parts) + ")",
        )
    if wants_breaker and wants_day:
        if not has_breaker and not has_day:
            return RuleEvaluation(
                matched=False,
                reason=(
                    "no regular-session circuit breaker and no day gain "
                    f"> {day_min:.2f}% in the last {rule.condition.recent_day_count} days"
                    if day_min is not None
                    else "no regular-session circuit breaker and no day-gain tier matched"
                ),
            )
    elif wants_breaker and not has_breaker:
        return RuleEvaluation(matched=False, reason="no regular-session circuit breaker")
    elif wants_day and not has_day:
        if tiered_day:
            parts = [
                f"day>{tier.min_recent_day_change:g}% & AH>{tier.value:g}%"
                for tier in day_tiers
            ]
            return RuleEvaluation(
                matched=False,
                reason="no tier matched (" + " or ".join(parts) + ")",
            )
        return RuleEvaluation(
            matched=False,
            reason=f"no day gain > {day_min:.2f}% in the last {rule.condition.recent_day_count} days",
        )
    if rule.condition.skip_market_cap or (
        rule.condition.require_circuit_breaker and rule.condition.max_market_cap is None
    ):
        trend = " and regular-session trend up" if rule.condition.require_uptrend else ""
        if wants_breaker and wants_day:
            if has_breaker and has_day:
                session = " and circuit breaker or day gain"
            elif has_breaker:
                session = " and regular-session circuit breaker"
            else:
                session = (
                    f" and day gain > {day_min:.2f}% in the last "
                    f"{rule.condition.recent_day_count} days"
                    if day_min is not None
                    else " and day-gain tier matched"
                )
        elif wants_breaker:
            session = " and regular-session circuit breaker"
        else:
            session = ""
        return RuleEvaluation(
            matched=True,
            reason=_afterhours_match_reason(
                rule, quote, suffix=f"{trend}{session}", tier=matched_tier
            ),
        )
    max_cap = rule.condition.max_market_cap if rule.condition.max_market_cap is not None else 100_000_000
    if market_cap is None:
        if cap_filtered:
            return RuleEvaluation(
                matched=True,
                reason=_afterhours_match_reason(
                    rule,
                    quote,
                    suffix=f" and total cap <= ${max_cap:,.0f}",
                    tier=matched_tier,
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
        reason=_afterhours_match_reason(
            rule,
            quote,
            suffix=f" and total cap ${market_cap:,.0f}",
            tier=matched_tier,
        ),
    )


def evaluate_stable_afterhours(
    rule: Rule,
    quote: Quote,
    *,
    cap_filtered: bool = False,
) -> RuleEvaluation:
    """Match when the after-hours high and low stayed inside the band."""
    low = rule.condition.stable_low
    high = rule.condition.stable_high
    minutes = rule.condition.stable_minutes
    if low is None or high is None or minutes is None:
        return RuleEvaluation(matched=False, reason="no stable band")
    if quote.change_pct is None:
        return RuleEvaluation(matched=False, reason="no after-hours change")
    band_low = quote.session_low_pct
    band_high = quote.session_high_pct
    if band_low is None or band_high is None:
        return RuleEvaluation(matched=False, reason="no after-hours range")
    if (
        band_low < low
        or band_high > high
        or quote.change_pct < low
        or quote.change_pct > high
    ):
        return RuleEvaluation(
            matched=False,
            reason=(
                f"after-hours range {band_low:.2f}%..{band_high:.2f}% "
                f"outside {low:.2f}%..{high:.2f}%"
            ),
        )
    regular_min = rule.condition.min_regular_change
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
    max_cap = rule.condition.max_market_cap if rule.condition.max_market_cap is not None else 100_000_000
    if not cap_filtered and rule.condition.max_market_cap is not None:
        return RuleEvaluation(matched=False, reason="no total market cap")
    return RuleEvaluation(
        matched=True,
        reason=(
            f"after-hours range {band_low:.2f}%..{band_high:.2f}% "
            f"stayed within {low:.2f}%..{high:.2f}% for {minutes:g}m "
            f"and total cap <= ${max_cap:,.0f}"
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
        ConditionType.SUDDEN_GAIN,
        ConditionType.REGULAR_CLOSE,
        ConditionType.REGULAR_GAIN,
    }:
        return RuleEvaluation(matched=False, reason="scanner rule")

    return RuleEvaluation(matched=False, reason="unknown condition")


def display_name(quote: Quote, fallback: str = "") -> str:
    """Company name for alert bodies; fall back to the ticker."""
    return (quote.company or "").strip() or (quote.symbol or fallback).strip()


def leaderboard_line(quote: Quote, *, company: bool = False) -> str:
    """Half-hour board line. Digest keeps the ticker; stored body can use the company name."""
    change = quote.change_pct if quote.change_pct is not None else 0.0
    name = display_name(quote) if company else (quote.symbol or "")
    return f"{name}{FIELD_GAP}{change:+.1f}%{FIELD_GAP}{format_price(quote.price or 0.0)}"


def peak_leaderboard_line(quote: Quote, *, company: bool = False) -> str:
    """Half-hour board line ranked by session peak gain, with current gain."""
    change = quote.change_pct if quote.change_pct is not None else 0.0
    peak = quote.session_high_pct
    if peak is None:
        peak = change
    name = display_name(quote) if company else (quote.symbol or "")
    return (
        f"{name}{FIELD_GAP}{peak:+.1f}%{FIELD_GAP}"
        f"{change:+.1f}%{FIELD_GAP}{format_price(quote.price or 0.0)}"
    )


def fade_leaderboard_line(quote: Quote, *, company: bool = False) -> str:
    """Half-hour fade board line: current %, peak, and giveback in percentage points."""
    change = quote.change_pct if quote.change_pct is not None else 0.0
    peak = quote.session_high_pct
    name = display_name(quote) if company else (quote.symbol or "")
    price = format_price(quote.price or 0.0)
    if peak is None:
        return f"{name}{FIELD_GAP}{change:+.1f}%{FIELD_GAP}{price}"
    fade = peak - change
    return (
        f"{name}{FIELD_GAP}{change:+.1f}%{FIELD_GAP}"
        f"峰值{peak:.0f}%回落{fade:.0f}pp{FIELD_GAP}{price}"
    )


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


def _prior_session(rule: Rule, quote: Quote) -> tuple[float | None, float | None]:
    """Change and price from the session before this alert.

    After-hours shows the regular close, premarket shows the previous day's
    after-hours close, and the regular session shows the premarket close.
    Title / stored alert fields still use the trigger move and price.
    """
    session = rule.group or rule.condition_session
    if session == "盘后":
        return quote.regular_change_pct, quote.regular_price
    if session == "盘前":
        return quote.after_hours_change_pct, quote.after_hours_price
    if session == "盘中":
        return quote.premarket_change_pct, quote.premarket_price
    return None, None


def _headline_move(
    rule: Rule, quote: Quote, *, headline: str
) -> tuple[str, str]:
    """Return ``(move_text, price_text)`` for the alert headline.

    ``prior``: previous session (AH → regular close). ``trigger``: this alert's move.
    """
    if headline == "trigger":
        change = quote.change_pct
        price = quote.price
        move = "" if change is None else f"{change:+.1f}%{FIELD_GAP}"
        price_text = "" if price is None or price <= 0 else format_price(price)
        return move, price_text
    change, price = _prior_session(rule, quote)
    move = "" if change is None else f"{change:+.1f}%{FIELD_GAP}"
    if price is not None and price > 0:
        price_text = format_price(price)
    elif change is not None:
        price_text = ""
    else:
        live = quote.price or 0.0
        price_text = "" if live <= 0 else format_price(live)
    return move, price_text


def render_message(
    rule: Rule,
    quote: Quote,
    extra: dict | None = None,
    *,
    headline: str = "prior",
) -> str:
    price = quote.price or 0.0
    change = quote.change_pct if quote.change_pct is not None else 0.0
    values = {
        "id": rule.id,
        "name": rule.name,
        "description": rule.summary,
        "symbol": quote.symbol or rule.symbol,
        "price": price,
        "change_pct": change,
        "threshold": rule.condition.value,
        "condition": rule.condition.type.value,
        "market_cap": 0.0,
        "market_cap_m": 0.0,
        "volume": quote.volume or 0,
        "stock_name": display_name(quote, quote.symbol or rule.symbol),
    }
    if extra:
        values.update(extra)
    values["field_gap"] = FIELD_GAP
    move_text, price_text = _headline_move(rule, quote, headline=headline)
    values["move_text"] = move_text
    values["price_text"] = price_text
    values["volume_text"] = format_volume(float(values["volume"]))
    template = rule.slack_message or "{name}: {symbol} is ${price:.2f} ({change_pct:+.2f}% today)"
    try:
        body = template.format(**values)
    except (KeyError, ValueError):
        body = f"{rule.name}: {values['symbol']} is ${price:.2f}"
    reason = rule.summary
    if reason and reason not in body:
        return f"{body}\n{reason}"
    return body
