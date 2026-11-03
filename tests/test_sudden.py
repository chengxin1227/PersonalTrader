from monitor.rules import hour_change_pct, price_age_seconds_ago, trim_price_samples
from monitor.rules import load_rules
from monitor.config import ROOT


def test_price_age_picks_newest_sample_old_enough():
    samples = [(100.0, 1.0), (140.0, 1.1), (160.0, 1.2), (200.0, 1.5)]
    prior = price_age_seconds_ago(samples, now=200.0, window_seconds=60)
    assert prior == (140.0, 1.1)
    assert hour_change_pct(prior[1], 1.5) == 36.3636


def test_price_age_needs_a_full_window():
    samples = [(170.0, 1.0), (200.0, 1.2)]
    assert price_age_seconds_ago(samples, now=200.0, window_seconds=60) is None


def test_trim_keeps_recent_samples_only():
    samples = [(100.0, 1.0), (150.0, 1.1), (190.0, 1.2)]
    assert trim_price_samples(samples, now=200.0, keep_seconds=60) == [
        (150.0, 1.1),
        (190.0, 1.2),
    ]


def test_live_rules_include_sudden_gain_for_each_session():
    config = load_rules(ROOT / "config" / "rules.yaml")
    sudden = [rule for rule in config.rules if rule.condition.type.value == "sudden_gain"]
    sessions = {rule.condition.late_session for rule in sudden}
    assert sessions == {"premarket", "regular", "afterhours"}
    for rule in sudden:
        assert rule.condition.value == 10
        assert rule.condition.pool_min_change == 10
        assert rule.condition.sudden_window_seconds == 60
