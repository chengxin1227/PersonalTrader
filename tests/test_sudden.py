import asyncio
from unittest.mock import AsyncMock

from monitor.config import ROOT
from monitor.engine import MonitorEngine
from monitor.models import Condition, ConditionType, Quote, Rule
from monitor.rules import hour_change_pct, load_rules, price_age_seconds_ago, trim_price_samples


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


def _sudden_rule(session: str) -> Rule:
    group = {"premarket": "盘前", "regular": "盘中", "afterhours": "盘后"}[session]
    return Rule(
        id=f"{session}-sudden-10",
        name=f"{session} sudden",
        description="test",
        group=group,
        symbol="*",
        cooldown_minutes=30,
        condition=Condition(
            type=ConditionType.SUDDEN_GAIN,
            value=10,
            pool_min_change=10,
            sudden_window_seconds=60,
            late_session=session,
            min_price=0.1,
            max_market_cap=1_000_000_000,
            min_volume=100_000,
        ),
    )


def _engine() -> MonitorEngine:
    engine = MonitorEngine.__new__(MonitorEngine)
    engine._sudden_pool = {}
    engine._sudden_prices = {}
    engine._sudden_session = {}
    engine._sudden_discovered_at = {}
    engine._sudden_day = None
    engine._drain_client_problem = AsyncMock()
    return engine


def test_sudden_discover_uses_afterhours_rank():
    engine = _engine()
    client = AsyncMock()
    client.scan_afterhours = AsyncMock(
        return_value=[
            (Quote(symbol="DKI", price=2.0, change_pct=35.0, volume=200_000), None),
            (Quote(symbol="THIN", price=2.0, change_pct=20.0, volume=1_000), None),
            (Quote(symbol="CHEAP", price=0.05, change_pct=40.0, volume=200_000), None),
        ]
    )
    client.smallcap_page = AsyncMock()

    async def run() -> None:
        await engine._discover_sudden_pool(_sudden_rule("afterhours"), client, "afterhours")

    asyncio.run(run())
    client.scan_afterhours.assert_awaited_once_with(10, 1_000_000_000)
    client.smallcap_page.assert_not_called()
    assert engine._sudden_pool["afterhours-sudden-10"] == {"DKI"}


def test_sudden_discover_uses_premarket_rank():
    engine = _engine()
    client = AsyncMock()
    client.scan_premarket = AsyncMock(
        return_value=[
            (Quote(symbol="AAA", price=3.0, change_pct=12.0, volume=150_000), None),
        ]
    )

    async def run() -> None:
        await engine._discover_sudden_pool(_sudden_rule("premarket"), client, "premarket")

    asyncio.run(run())
    client.scan_premarket.assert_awaited_once_with(10, 1_000_000_000)
    assert engine._sudden_pool["premarket-sudden-10"] == {"AAA"}


def test_sudden_discover_uses_regular_gainers():
    engine = _engine()
    client = AsyncMock()
    client.regular_gainers = AsyncMock(
        return_value=[Quote(symbol="BBB", price=4.0, change_pct=15.0, volume=120_000)]
    )

    async def run() -> None:
        await engine._discover_sudden_pool(_sudden_rule("regular"), client, "regular")

    asyncio.run(run())
    client.regular_gainers.assert_awaited_once_with(
        10, max_market_cap=1_000_000_000, min_volume=100_000
    )
    assert engine._sudden_pool["regular-sudden-10"] == {"BBB"}


def test_sudden_discover_seeds_price_samples():
    engine = _engine()
    client = AsyncMock()
    client.scan_afterhours = AsyncMock(
        return_value=[(Quote(symbol="CRIS", price=0.95, change_pct=40.0, volume=200_000), None)]
    )

    async def run() -> None:
        await engine._discover_sudden_pool(_sudden_rule("afterhours"), client, "afterhours")

    asyncio.run(run())
    samples = engine._sudden_prices["afterhours-sudden-10"]["CRIS"]
    assert len(samples) == 1
    assert samples[0][1] == 0.95


def test_sudden_match_keeps_pool_when_quote_missing():
    engine = _engine()
    engine._sudden_day = "2026-11-03"
    engine._sudden_pool["afterhours-sudden-10"] = {"CRIS", "DKI"}
    engine._sudden_prices["afterhours-sudden-10"] = {
        "CRIS": [(100.0, 1.0)],
        "DKI": [(100.0, 2.0)],
    }
    engine._sudden_discovered_at["afterhours-sudden-10"] = 10**12  # skip rediscover
    client = AsyncMock()
    engine._trend_moomoo = client
    engine.moomoo = client
    engine.verbose = False
    # Only DKI comes back this round — CRIS must stay in the pool with samples.
    client.after_hours_quotes = AsyncMock(
        return_value={"DKI": Quote(symbol="DKI", price=2.1, change_pct=12.0, volume=200_000)}
    )

    async def run() -> None:
        matched = await engine._match_sudden_rule(_sudden_rule("afterhours"))
        assert matched == []

    asyncio.run(run())
    assert engine._sudden_pool["afterhours-sudden-10"] == {"CRIS", "DKI"}
    assert "CRIS" in engine._sudden_prices["afterhours-sudden-10"]
